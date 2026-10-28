# -*- coding: utf-8 -*-
"""HTTP 服务：多轮问答 Agent + 题库浏览 + 健康检查。

启动（仓库根目录）：
  py -m uvicorn api.app:app --host 0.0.0.0 --port 8000

环境变量：
  DATABASE_URL      PostgreSQL 连接串；设置后语料/向量/浏览查询都走数据库
  KB_INPUT          题库 JSONL，逗号分隔，默认 2019/2020/2021/2023/2024 五卷
  KB_MODE           bm25 / hybrid / hybrid-rerank / hybrid-ce，默认 hybrid
  KB_FUSE_K         进入精排的候选数，默认 10
  KB_PROVIDER       mock / deepseek；默认有 DEEPSEEK_API_KEY 就用 deepseek
  KB_MIN_SCORE      拒答阈值；hybrid-ce 默认 0.1，其他模式默认 0
  KB_MIN_MARGIN     top1/top2 最小分差；默认 0（关闭分差过滤）
  KB_ACCESS_TOKEN   访问口令；设置后内容接口必须带 X-Access-Token
  KB_RATE_MAX       每个 IP 在 KB_RATE_WINDOW 秒内最多请求次数，默认 20
  KB_RATE_WINDOW    限流窗口秒数，默认 600
  KB_DAILY_MAX      全局每日请求上限，默认 0（不限）
  KB_TRUST_PROXY    设为 1 时从 X-Forwarded-For 取客户端 IP（放在 Nginx 后面时打开）
  KB_CACHE_TTL      应答缓存有效期（秒），默认 86400；0 表示关闭，拒答不缓存
  KB_FEEDBACK_LOG   没有数据库时反馈写到哪里，默认 data/work/feedback.jsonl
"""
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from answer.agent import QueryAgent, chitchat_reply
from answer.cache import (cache_key, cached_payload, namespace,
                          storable_payload, ttl_from_env, with_cache_meta)
from answer.gate import RefusalGate
from answer.pipeline import AnswerPipeline
from answer.providers import DeepSeekProvider, ExtractiveProvider, ProviderError
from answer.qgen import generate_questions
from answer.stream import sse
from kb import PRESET_COLLECTION, Library, LibraryError
from retrieval.chunk import build_chunk, build_knowledge_chunk, load_questions
from retrieval.search import build_searcher

from db.store import Store

from .catalog import (brief, detail, filter_questions, format_subject_counts,
                      subject_counts)
from .feedback import (feedback_stats as jsonl_feedback_stats,
                       log_feedback as jsonl_log_feedback, normalize_feedback)
from .gap import gap_stats, log_gap
from .guard import AccessGuard
from . import metrics
from .knowledge import (filter_knowledge, know_brief, know_detail,
                        knowledge_chapters, knowledge_for_question,
                        knowledge_subject_counts, knowledge_tree,
                        load_knowledge, related_knowledge_by_question)

ROOT = Path(__file__).resolve().parents[1]

_state = {}
_lock = threading.Lock()
GUARD = AccessGuard.from_env()


class AskRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    # 讲解风格（人格预设 / 技能 / 学习记忆），只影响措辞，不改变引用规则
    style: str = Field("", max_length=400)
    # 检索哪个知识库：空字符串 / cs408 = 内置 408 语料，其余是用户自建资料库
    collection: str = Field("", max_length=64)


class ChatTurn(BaseModel):
    role: str = Field("user", pattern="^(user|assistant)$")
    content: str = Field("", max_length=4000)


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    history: List[ChatTurn] = Field(default_factory=list)
    style: str = Field("", max_length=400)
    collection: str = Field("", max_length=64)


class CollectionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    description: str = Field("", max_length=200)


class DocumentRequest(BaseModel):
    # 用户在页面里粘贴的资料；切分后只有片段进检索库，原文整篇留档以便回跳
    text: str = Field(..., min_length=20, max_length=50000)
    title: str = Field("", max_length=80)


class GenerateQuestionsRequest(BaseModel):
    # 用户粘贴的资料；出题结果只存在浏览器，服务端不落盘
    text: str = Field(..., min_length=80, max_length=20000)
    count: int = Field(5, ge=1, le=10)
    title: str = Field("", max_length=80)


class FeedbackRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    helpful: bool
    reason: str = Field("", max_length=40)
    note: str = Field("", max_length=500)
    answer: str = Field("", max_length=4000)
    citations: List[str] = Field(default_factory=list)
    cached: bool = False


def _enforce(request: Request, limit: bool) -> None:
    peer = request.client.host if request.client else ""
    d = GUARD.check(request.headers, peer, limit=limit)
    if not d.ok:
        raise HTTPException(status_code=d.status, detail=d.message,
                            headers={"Retry-After": str(d.retry_after)}
                            if d.retry_after else None)


def enforce_guard(request: Request) -> None:
    """问答接口：口令 + 限流 + 每日额度。"""
    _enforce(request, True)


def enforce_content(request: Request) -> None:
    """题库浏览：只校验口令，不占问答额度。"""
    _enforce(request, False)


def _resolve(path: str) -> str:
    return str((ROOT / path) if not os.path.isabs(path) else Path(path))


def _load() -> None:
    raw = os.environ.get(
        "KB_INPUT",
        "data/work/2019_draft.jsonl,data/work/2020_draft.jsonl,"
        "data/work/2021_draft.jsonl,data/work/2023_draft.jsonl,"
        "data/work/2024_draft.jsonl")
    paths = [_resolve(p.strip()) for p in raw.split(",") if p.strip()]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise RuntimeError("题库文件不存在：%s" % ", ".join(missing))

    questions = load_questions(paths)

    # 考点卡片默认从仓库里的 curated 目录读；显式设置 KB_KNOWLEDGE 时必须存在。
    know_env = os.environ.get("KB_KNOWLEDGE")
    know_path = _resolve(know_env or "data/curated/knowledge.jsonl")
    if know_env and not os.path.exists(know_path):
        raise RuntimeError("考点文件不存在：%s" % know_path)
    knowledge = (load_knowledge(know_path, "data/curated/knowledge.jsonl")
                 if os.path.exists(know_path) else [])

    chunk_ids = {q["id"] for q in questions}
    clash = [c["id"] for c in knowledge if c["id"] in chunk_ids]
    if clash:
        raise RuntimeError("考点 id 与题目 id 冲突：%s" % ", ".join(clash))

    chunks = [build_chunk(q) for q in questions]
    chunks.extend(build_knowledge_chunk(c, c["_file"], c["_line"]) for c in knowledge)
    mode = os.environ.get("KB_MODE", "hybrid")
    # CPU 上精排 10 个候选约 7 秒/次；30 个要约 27 秒，R@1 只差 0 点。
    fuse_k = int(os.environ.get("KB_FUSE_K", "10"))

    # 配了 DATABASE_URL 就以 Postgres 为准：语料、向量、浏览查询都走数据库，
    # 进程内只留 BM25 和精排模型。没有数据库时回退内存索引，便于单测和离线开发。
    store = Store.from_env()
    if store is not None:
        store.init_schema()
    searcher = build_searcher(
        chunks, mode=mode, k_fuse=fuse_k, k_final=5,
        vector_search=store.vector_search if store is not None else None)
    fingerprint = ""
    if store is not None:
        stats = store.sync(
            questions, knowledge, chunks,
            embedder=searcher.embedder,
            model_name=searcher.embedder.model_name if searcher.embedder else "")
        fingerprint = stats.get("fingerprint", "")
        print("数据库语料%s：题 %d / 考点 %d" % (
            "已重建" if stats["reloaded"] else "无需重建",
            stats["questions"], stats["knowledge"]))

    provider_name = os.environ.get("KB_PROVIDER", "")
    if not provider_name:
        provider_name = "deepseek" if os.environ.get("DEEPSEEK_API_KEY") else "mock"
    if provider_name == "deepseek":
        try:
            provider = DeepSeekProvider()
        except ProviderError:
            provider = ExtractiveProvider()
            provider_name = "mock"
    else:
        provider = ExtractiveProvider()

    # 阈值来自 answer.eval_gate 在「真题 235（2019/2020/2021/2023/2024 五卷）
    # + 考点 42」混合语料上的标定（32 条正样本 + 16 条负样本）：0.1/0 时
    # 负样本闸门拒答 87.5%、正样本误拒 6.2%；端到端（含生成与引用校验）
    # 16 条负样本全部拒答。0.3/0 能把闸门拒答提到 93.8%，但误拒也升到
    # 12.5%，所以仍用 0.1/0。分差阈值会误伤「多个考点都相关」的问题，默认关闭。
    default_score = "0.1" if mode == "hybrid-ce" else "0"
    default_margin = "0"
    gate = RefusalGate(float(os.environ.get("KB_MIN_SCORE", default_score)),
                       float(os.environ.get("KB_MIN_MARGIN", default_margin)))
    gap_path = Path(os.environ.get("KB_GAP_LOG", "data/work/gap_log.jsonl"))
    if not gap_path.is_absolute():
        gap_path = ROOT / gap_path
    gap_path.parent.mkdir(parents=True, exist_ok=True)
    feedback_path = Path(os.environ.get(
        "KB_FEEDBACK_LOG", "data/work/feedback.jsonl"))
    if not feedback_path.is_absolute():
        feedback_path = ROOT / feedback_path
    feedback_path.parent.mkdir(parents=True, exist_ok=True)

    # 只有真实模型才做追问改写；mock 模式退化为规则拼接，保证离线也能跑。
    agent = QueryAgent(provider if provider_name == "deepseek" else None)

    # 用户自建知识库：与 408 预置语料共用同一套 BM25/向量/精排/拒答，
    # 只是换一份片段来源和一段 prompt。没有数据库时状态落在 data/work 下。
    library_state = (None if store is not None
                     else str(ROOT / "data/work/kb/library.json"))
    library = Library(store=store, embedder=searcher.embedder, mode=mode,
                      k_fuse=fuse_k, state_path=library_state)

    _state.update({
        "pipeline": AnswerPipeline(searcher, provider, gate=gate),
        "library": library,
        "gate": gate,
        "doc_pipelines": {},
        "chunks": chunks,
        "questions": questions,
        "questions_by_id": {q["id"]: q for q in questions},
        "knowledge": knowledge,
        "knowledge_by_id": {c["id"]: c for c in knowledge},
        "knowledge_by_question": related_knowledge_by_question(knowledge),
        "mode": mode,
        "fuse_k": fuse_k,
        "provider": provider_name,
        "provider_impl": provider,
        "storage": "postgres" if store is not None else "memory",
        "store": store,
        "gap_log": str(gap_path),
        "feedback_log": str(feedback_path),
        "agent": agent,
        # 缓存命名空间隔离检索模式、provider、精排候选数和语料版本：
        # 任一变化都换一套键，旧回答不会串到新配置上。
        "cache_ttl": ttl_from_env(),
        "cache_ns": "%s|%s|k%s|%s" % (
            mode, provider_name, fuse_k, fingerprint[:16]),
    })


@asynccontextmanager
async def lifespan(_: FastAPI):
    _load()
    yield


app = FastAPI(title="408 考研知识库", version="0.15", lifespan=lifespan)


def _is_preset(collection: str) -> bool:
    return not collection or collection == PRESET_COLLECTION


def _pipeline_for(collection: str):
    """按知识库取问答管线；用户库各自一份，库里资料变了就重建。

    返回 None 表示资料库存在但还没有资料（应答方要给出明确提示，
    而不是装作「服务器没准备好」）。
    """
    if _is_preset(collection):
        return _state.get("pipeline")
    lib = _state.get("library")
    if lib is None or lib.get_collection(collection) is None:
        raise HTTPException(status_code=404, detail="资料库不存在")
    searcher = lib.searcher(collection)
    if searcher is None:
        return None
    pipes = _state["doc_pipelines"]
    cached = pipes.get(collection)
    if cached and cached[0] is searcher:
        return cached[1]
    pipe = AnswerPipeline(searcher, _state["provider_impl"],
                          gate=_state.get("gate"), prompt_mode="doc")
    pipes[collection] = (searcher, pipe)
    return pipe


def _preset_collection() -> dict:
    questions = _state.get("questions", [])
    knowledge = _state.get("knowledge", [])
    return {
        "id": PRESET_COLLECTION,
        "name": "408 考研（内置）",
        "description": "计算机统考真题与考点卡片，只读",
        "builtin": True,
        "documents": len(questions) + len(knowledge),
        "chunks": len(questions) + len(knowledge),
        "questions": len(questions),
        "knowledge": len(knowledge),
        "created_at": "",
        "updated_at": "",
    }


def _collection_count() -> int:
    lib = _state.get("library")
    if lib is None:
        return 0
    try:
        return len(lib.list_collections())
    except Exception:  # 统计失败不能让健康检查挂掉
        return 0


def _corpus_label(collection: str) -> str:
    if not _is_preset(collection):
        lib = _state.get("library")
        coll = lib.get_collection(collection) if lib is not None else None
        if coll:
            docs = coll.get("documents") or []
            segs = sum(int(d.get("chunks") or 0) for d in docs)
            return "资料库「%s」：%d 篇 / %d 段" % (coll["name"], len(docs), segs)
    know_n = len(_state.get("knowledge", []))
    q_n = len(_state.get("questions", []))
    return "真题 %d + 考点 %d" % (q_n, know_n) if know_n else "真题 %d" % q_n


def _empty_collection_payload(t0: float) -> dict:
    return {
        "intent": "kb", "refused": True, "answer": "",
        "citations": [], "reason": "empty_collection", "hits": [],
        "attempts": 0, "latency_ms": int((time.time() - t0) * 1000),
        "rewritten_query": "",
        "trace": {"intent": "kb", "rewritten_query": "", "steps": []},
    }


def _hit_payload(h) -> dict:
    c = h.chunk
    return {
        "chunk_id": c.id,
        "seq": c.seq,
        "score": round(float(h.score), 4),
        "subject": c.subject,
        "kind": c.kind,
        "title": c.title,
        "chapter": c.chapter,
        "stem": (c.title if c.kind == "knowledge" else c.text.split("\n")[0])[:120],
        "answer": c.answer,
        "source_ref": c.source_ref,
    }


def _steps(res, plan=None, collection: str = "") -> list:
    timings = res.timings or {}
    gen_ms = timings.get("generate", 0)
    steps = []
    if plan is not None:
        if plan.intent in ("chitchat", "clarify"):
            detail_text = ("识别为寒暄/元问题，不进入检索" if plan.intent == "chitchat"
                           else (plan.note or "指代不明，需要补充信息"))
            return [{"name": "理解问题", "ms": plan.ms, "detail": detail_text}]
        if plan.source == "llm":
            detail_text = "结合上文改写为：%s" % plan.rewritten
        elif plan.source == "rule":
            detail_text = "按规则补全为：%s" % plan.rewritten
        else:
            detail_text = plan.note or "独立问题，无需改写"
        steps.append({"name": "理解问题", "ms": plan.ms, "detail": detail_text})

    steps.append({"name": "检索", "ms": timings.get("retrieve", 0),
                  "detail": "%s：BM25 + 向量双路召回 → RRF → Cross-Encoder 精排"
                            % _corpus_label(collection)})

    gate_ok = not (res.refused and gen_ms == 0)
    steps.append({"name": "拒答判断", "ms": timings.get("gate", 0),
                  "detail": "通过" if gate_ok else "未通过：%s" % (res.reason or "分数不足")})

    steps.append({"name": "生成", "ms": gen_ms,
                  "detail": "未进入生成" if gen_ms == 0
                  else "强制 JSON，尝试 %d 次" % res.attempts})

    if not res.refused:
        verify_text = "逐字校验通过"
    elif gen_ms == 0:
        verify_text = "未生成"
    else:
        verify_text = res.reason or "未通过"
    steps.append({"name": "引用校验", "ms": timings.get("verify", 0),
                  "detail": verify_text})
    return steps


def _answer_payload(res, t0: float, plan=None, collection: str = "") -> dict:
    by_id = {h.chunk.id: h.chunk for h in res.hits}
    citations = []
    for c in res.citations:
        cid = c.get("chunk_id")
        ch = by_id.get(cid)
        citations.append({
            "chunk_id": cid,
            "quote": c.get("quote"),
            "kind": ch.kind if ch else "question",
            "title": ch.title if ch else "",
            "chapter": ch.chapter if ch else "",
            "subject": ch.subject if ch else "",
            "source_ref": ch.source_ref if ch else {},
        })
    rewritten = plan.rewritten if plan and plan.rewritten != plan.query else ""
    return {
        "intent": plan.intent if plan else "kb",
        # 前端据此把引用跳回知识库原文；空字符串表示内置 408 语料
        "collection": "" if _is_preset(collection) else collection,
        "refused": res.refused,
        "answer": res.answer,
        "citations": citations,
        "reason": res.reason,
        "hits": [_hit_payload(h) for h in res.hits],
        "attempts": res.attempts,
        "latency_ms": int((time.time() - t0) * 1000),
        "rewritten_query": rewritten,
        "trace": {
            "intent": plan.intent if plan else "kb",
            "rewritten_query": rewritten,
            "steps": _steps(res, plan, collection),
        },
    }


def _non_kb_payload(plan, t0: float) -> dict:
    if plan.intent == "chitchat":
        answer = chitchat_reply(plan.query)
    else:
        answer = ("我不确定你指的是哪道题。可以把题号或关键词补上，"
                  "例如「2019 年第 31 题」或「快速排序的时间复杂度」。")
    return {
        "intent": plan.intent,
        "refused": False,
        "answer": answer,
        "citations": [],
        "reason": "",
        "hits": [],
        "attempts": 0,
        "latency_ms": int((time.time() - t0) * 1000),
        "rewritten_query": "",
        "trace": {
            "intent": plan.intent,
            "rewritten_query": "",
            "steps": [{"name": "理解问题", "ms": plan.ms,
                       "detail": ("识别为寒暄/元问题，不进入检索"
                                  if plan.intent == "chitchat"
                                  else (plan.note or "指代不明，需要补充信息"))}],
        },
    }


def _log_gap(query: str, res, collection: str = "") -> None:
    if not res.refused:
        return
    # 用户库的缺口单独标出来：reason 里带库 id，看板才能区分
    # 「408 语料没覆盖」和「这个人的资料没覆盖」。
    reason = (res.reason if _is_preset(collection)
              else "%s|collection:%s" % (res.reason, collection))
    top = res.hits[0] if res.hits else None
    store = _state.get("store")
    if store is not None:
        try:
            store.log_gap(query, reason,
                          top.chunk.id if top else None,
                          float(top.score) if top else None)
        except Exception:  # 日志写失败不能影响答题
            pass
        return
    try:
        log_gap(_state["gap_log"], query, reason, top)
    except OSError:
        pass  # 日志写失败不能影响答题


def _clean_style(style: str) -> str:
    """把风格文本压成单行并截断，避免换行被模型当成新指令。"""
    return " ".join((style or "").split())[:400]


def _cache_namespace(style: str = "", collection: str = "") -> str:
    """缓存命名空间额外带上风格指纹和知识库：换个伙伴、换个库都重新生成。"""
    return namespace(_state.get("cache_ns", ""), style, collection,
                     _is_preset(collection))


def _cache_lookup(query: str, style: str = "",
                  collection: str = "") -> Optional[dict]:
    """按「实际用于检索的问题」查缓存；没有数据库或关闭 TTL 时直接跳过。"""
    store = _state.get("store")
    ttl = int(_state.get("cache_ttl", 0))
    if store is None or ttl <= 0:
        return None
    key = cache_key(query, _cache_namespace(style, collection))
    try:
        return store.get_answer_cache(key, ttl)
    except Exception:  # 缓存故障降级为直接答题
        return None


def _cache_store(query: str, payload: dict, style: str = "",
                 collection: str = "") -> None:
    store = _state.get("store")
    ttl = int(_state.get("cache_ttl", 0))
    if store is None or ttl <= 0 or payload.get("refused"):
        return
    key = cache_key(query, _cache_namespace(style, collection))
    try:
        store.put_answer_cache(key, query, storable_payload(payload), ttl)
    except Exception:
        pass  # 写缓存失败不能影响答题


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        # chunks 保持“题目数”的含义，新增 knowledge 单独计数，旧前端/脚本不受影响
        "chunks": len(_state.get("questions", [])),
        "knowledge": len(_state.get("knowledge", [])),
        "mode": _state.get("mode"),
        "fuse_k": _state.get("fuse_k"),
        "provider": _state.get("provider"),
        "storage": _state.get("storage", "memory"),
        "auth_required": GUARD.token_required,
        "cache_ttl_s": _state.get("cache_ttl", 0),
        "collections": _collection_count(),
    }


@app.post("/ask")
def ask(req: AskRequest, _: None = Depends(enforce_guard)) -> dict:
    pipeline = _pipeline_for(req.collection)
    if pipeline is None:
        if _is_preset(req.collection):
            return {"refused": True, "answer": "", "citations": [],
                    "reason": "server_not_ready", "hits": [], "latency_ms": 0}
        return _empty_collection_payload(time.time())
    t0 = time.time()
    metrics.bump("ask_total")
    style = _clean_style(req.style)
    hit = _cache_lookup(req.query, style, req.collection)
    if hit is not None:
        metrics.bump("cache_hit")
        return cached_payload(hit["payload"], hit["age_s"],
                              int((time.time() - t0) * 1000))
    # 模型推理是 CPU 密集的，单 worker 内串行，避免线程争抢
    with _lock:
        res = pipeline.answer(req.query, style)
    _log_gap(req.query, res, req.collection)
    metrics.bump("ask_refused" if res.refused else "ask_answered")
    payload = with_cache_meta(
        _answer_payload(res, t0, collection=req.collection))
    _cache_store(req.query, payload, style, req.collection)
    return payload


@app.post("/chat")
def chat(req: ChatRequest, _: None = Depends(enforce_guard)) -> dict:
    agent = _state.get("agent")
    pipeline = _pipeline_for(req.collection)
    if agent is None or pipeline is None:
        if pipeline is None and not _is_preset(req.collection):
            return _empty_collection_payload(time.time())
        return {"intent": "kb", "refused": True, "answer": "", "citations": [],
                "reason": "server_not_ready", "hits": [], "attempts": 0,
                "latency_ms": 0, "rewritten_query": "",
                "trace": {"steps": [], "rewritten_query": "", "intent": "kb"}}

    t0 = time.time()
    history = []
    for turn in req.history:
        history.append(turn.model_dump() if hasattr(turn, "model_dump") else turn.dict())

    # 改写只调一次轻量模型，放在锁外；真正的检索/生成仍在锁内串行。
    plan = agent.plan(req.query, history)
    metrics.bump("chat_total")
    if plan.intent in ("chitchat", "clarify"):
        metrics.bump("chat_nonkb")
        return with_cache_meta(_non_kb_payload(plan, t0))

    # 多轮按「改写后的问题」查缓存：同一轮追问得到同一独立问题时复用答案。
    style = _clean_style(req.style)
    hit = _cache_lookup(plan.rewritten, style, req.collection)
    if hit is not None:
        metrics.bump("cache_hit")
        out = cached_payload(hit["payload"], hit["age_s"],
                             int((time.time() - t0) * 1000))
        rewritten = plan.rewritten if plan.rewritten != plan.query else ""
        out["intent"] = plan.intent
        out["rewritten_query"] = rewritten
        out["trace"]["rewritten_query"] = rewritten
        return out

    with _lock:
        res = pipeline.answer(plan.rewritten, style)
    _log_gap(req.query, res, req.collection)
    metrics.bump("chat_refused" if res.refused else "chat_answered")
    payload = with_cache_meta(
        _answer_payload(res, t0, plan, req.collection))
    _cache_store(plan.rewritten, payload, style, req.collection)
    return payload


@app.post("/chat/stream")
def chat_stream(req: ChatRequest, _: None = Depends(enforce_guard)):
    """流式问答：先按 SSE 吐 answer 文本，收完再校验引用。

    事件顺序：status(plan) → status(retrieve/gate) → delta* →
    status(verify) → done。校验失败会先发 reset 让前端清空，再重试一次；
    最终结果只认 done 里的 payload——引用校验和拒答规则与非流式完全一致。
    """
    agent = _state.get("agent")
    pipeline = _pipeline_for(req.collection)

    def events():
        if agent is None or pipeline is None:
            data = (_empty_collection_payload(time.time())
                    if not _is_preset(req.collection) else {
                        "intent": "kb", "refused": True, "answer": "",
                        "citations": [], "reason": "server_not_ready",
                        "hits": [], "attempts": 0, "latency_ms": 0,
                        "rewritten_query": "",
                        "trace": {"steps": [], "rewritten_query": "",
                                  "intent": "kb"}})
            yield sse({"type": "done", "data": data})
            return

        t0 = time.time()
        history = []
        for turn in req.history:
            history.append(turn.model_dump() if hasattr(turn, "model_dump") else turn.dict())

        yield sse({"type": "status", "stage": "plan"})
        plan = agent.plan(req.query, history)
        metrics.bump("chat_total")
        if plan.intent in ("chitchat", "clarify"):
            metrics.bump("chat_nonkb")
            yield sse({"type": "done", "data": with_cache_meta(_non_kb_payload(plan, t0))})
            return

        style = _clean_style(req.style)
        hit = _cache_lookup(plan.rewritten, style, req.collection)
        if hit is not None:
            metrics.bump("cache_hit")
            out = cached_payload(hit["payload"], hit["age_s"],
                                 int((time.time() - t0) * 1000))
            rewritten = plan.rewritten if plan.rewritten != plan.query else ""
            out["intent"] = plan.intent
            out["rewritten_query"] = rewritten
            out["trace"]["rewritten_query"] = rewritten
            yield sse({"type": "status", "stage": "cache"})
            yield sse({"type": "done", "data": out})
            return

        with _lock:
            for ev in pipeline.answer_stream(plan.rewritten, style):
                if ev.get("type") != "done":
                    yield sse(ev)
                    continue
                res = ev["result"]
                _log_gap(req.query, res, req.collection)
                metrics.bump("chat_refused" if res.refused else "chat_answered")
                payload = with_cache_meta(
                    _answer_payload(res, t0, plan, req.collection))
                _cache_store(plan.rewritten, payload, style, req.collection)
                yield sse({"type": "done", "data": payload})
                return

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


def _gap_stats_data(days: int, limit: int) -> dict:
    """缺口统计的公共取数：有库走库，没库读 JSONL。"""
    store = _state.get("store")
    if store is not None:
        return store.gap_stats(days, limit)
    return gap_stats(_state.get("gap_log", ""), days, limit)


def _feedback_stats_data(days: int, limit: int) -> dict:
    store = _state.get("store")
    if store is not None:
        return store.feedback_stats(days, limit)
    return jsonl_feedback_stats(_state.get("feedback_log", ""), days, limit)


@app.post("/feedback")
def feedback(req: FeedbackRequest,
             _: None = Depends(enforce_content)) -> dict:
    """记录用户对一条回答的评价。

    反馈不占问答额度（不调模型），但仍然要求口令，避免公网被灌数据。
    有库入库；写库失败或没配数据库时退到 JSONL，不把用户的操作丢掉。
    """
    try:
        rec = normalize_feedback(req.query, req.helpful, req.reason,
                                 req.note, req.answer, req.citations,
                                 req.cached)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    store = _state.get("store")
    if store is not None:
        try:
            store.log_feedback(rec)
            return {"ok": True, "stored": "postgres"}
        except Exception:  # 入库失败不能影响前端，至少留下 JSONL
            pass
    path = _state.get("feedback_log", "")
    if path:
        try:
            jsonl_log_feedback(path, rec)
            return {"ok": True, "stored": "jsonl"}
        except OSError:
            pass
    return {"ok": False, "stored": ""}


@app.get("/stats/feedback")
def stats_feedback(days: int = 30, limit: int = 10,
                   _: None = Depends(enforce_content)) -> dict:
    """反馈统计：有用率、差评原因、最近差评。"""
    return _feedback_stats_data(max(1, min(int(days), 365)),
                                max(1, min(int(limit), 100)))


@app.get("/stats/gaps")
def stats_gaps(days: int = 30, limit: int = 20,
               _: None = Depends(enforce_content)) -> dict:
    """缺口统计：按原因、按天聚合，并给出最近若干条拒答。"""
    return _gap_stats_data(max(1, min(int(days), 365)),
                           max(1, min(int(limit), 100)))


@app.get("/stats/overview")
def stats_overview(days: int = 30, limit: int = 20,
                   _: None = Depends(enforce_content)) -> dict:
    """质量看板：进程内问答计数 + 缺口统计 + 语料规模。

    process 里的计数只代表本进程启动以来，重启归零；跨重启的历史看 gaps。
    """
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 100))
    counters = metrics.snapshot()
    asks = counters["ask_total"] + counters["chat_total"]
    questions = _state.get("questions", [])
    years = sorted({int(q.get("year")) for q in questions if q.get("year")})
    return {
        "storage": _state.get("storage", "memory"),
        "window_days": days,
        "process": counters,
        "asks": asks,
        "answered": counters["ask_answered"] + counters["chat_answered"],
        "refused": counters["ask_refused"] + counters["chat_refused"],
        "cache_hits": counters["cache_hit"],
        "cache_hit_rate": round(counters["cache_hit"] / asks, 4) if asks else 0.0,
        "corpus": {
            "questions": len(questions),
            "knowledge": len(_state.get("knowledge", [])),
            "years": years,
        },
        "gaps": _gap_stats_data(days, limit),
        "feedback": _feedback_stats_data(days, limit),
    }


@app.get("/meta")
def meta(_: None = Depends(enforce_content)) -> dict:
    questions = _state.get("questions", [])
    knowledge = _state.get("knowledge", [])
    store = _state.get("store")
    if store is not None:
        counts = store.question_subject_counts()
        subjects = format_subject_counts(counts)
        years = store.years()
    else:
        subjects = subject_counts(questions)
        years = sorted({int(q.get("year")) for q in questions if q.get("year")})
    return {
        "chunks": len(questions),
        "subjects": subjects,
        "knowledge": len(knowledge),
        "knowledge_subjects": knowledge_subject_counts(knowledge),
        "years": years,
        "mode": _state.get("mode"),
        "provider": _state.get("provider"),
        "storage": _state.get("storage", "memory"),
        "auth_required": GUARD.token_required,
        "collections": _collection_count(),
    }


@app.get("/knowledge")
def knowledge_list(q: str = "", subject: str = "", chapter: str = "",
                   limit: int = 100, offset: int = 0,
                   _: None = Depends(enforce_content)) -> dict:
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    store = _state.get("store")
    cards = store.all_knowledge() if store is not None else _state.get("knowledge", [])
    total, rows = filter_knowledge(cards, q, subject, chapter, limit, offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [know_detail(c, _state.get("questions_by_id", {})) for c in rows],
        "subjects": knowledge_subject_counts(cards),
        "chapters": knowledge_chapters(cards, subject, _state.get("questions_by_id", {})),
    }


@app.get("/knowledge/tree")
def knowledge_tree_view(subject: str = "", limit: int = 20,
                        _: None = Depends(enforce_content)) -> dict:
    """考点树 + 考频榜。

    必须注册在 /knowledge/{card_id} 之前，否则 "tree" 会被当成卡片 id。
    """
    limit = max(0, min(int(limit), 100))
    store = _state.get("store")
    cards = store.all_knowledge() if store is not None else _state.get("knowledge", [])
    sub = (subject or "").strip()
    if sub:
        cards = [c for c in cards if (c.get("subject") or "") == sub]
    return knowledge_tree(cards, _state.get("questions_by_id", {}), limit=limit)


@app.get("/knowledge/{card_id}")
def knowledge_detail(card_id: str, _: None = Depends(enforce_content)) -> dict:
    store = _state.get("store")
    card = (store.get_knowledge(card_id) if store is not None
            else _state.get("knowledge_by_id", {}).get(card_id))
    if card is None:
        raise HTTPException(status_code=404, detail="考点不存在")
    return know_detail(card, _state.get("questions_by_id", {}))


@app.get("/chunks")
def chunks(q: str = "", subject: str = "", year: Optional[int] = None,
           limit: int = 20, offset: int = 0,
           _: None = Depends(enforce_content)) -> dict:
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    store = _state.get("store")
    if store is not None:
        total, rows = store.query_questions(q, subject, year, limit, offset)
    else:
        total, rows = filter_questions(_state.get("questions", []), q, subject,
                                       year, limit, offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [brief(r) for r in rows],
    }


@app.get("/chunks/{chunk_id}")
def chunk_detail(chunk_id: str, _: None = Depends(enforce_content)) -> dict:
    store = _state.get("store")
    row = (store.get_question(chunk_id) if store is not None
           else _state.get("questions_by_id", {}).get(chunk_id))
    if row is None:
        raise HTTPException(status_code=404, detail="题目不存在")
    item = detail(row)
    if store is not None:
        item["related_knowledge"] = [know_brief(c)
                                     for c in store.related_knowledge(chunk_id)]
    else:
        item["related_knowledge"] = _state.get(
            "knowledge_by_question", {}).get(chunk_id, [])
    return item


# ---------- 知识库：用户自建资料库 ----------

def _library() -> Library:
    lib = _state.get("library")
    if lib is None:
        raise HTTPException(status_code=503, detail="资料库未就绪")
    return lib


def _lib_error(e: LibraryError) -> HTTPException:
    msg = str(e)
    if "不存在" in msg:
        status = 404
    elif "同名" in msg or "已经在库里" in msg:
        status = 409
    else:
        status = 400
    return HTTPException(status_code=status, detail=msg)


@app.get("/collections")
def collections_list(_: None = Depends(enforce_content)) -> dict:
    """所有知识库；内置 408 语料排在第一位，只读。"""
    return {"items": [_preset_collection()] + _library().list_collections(),
            "storage": _state.get("storage", "memory")}


@app.post("/collections")
def collection_create(req: CollectionRequest,
                      _: None = Depends(enforce_content)) -> dict:
    try:
        return _library().create_collection(req.name, req.description)
    except LibraryError as e:
        raise _lib_error(e)


@app.get("/collections/{cid}")
def collection_detail(cid: str, _: None = Depends(enforce_content)) -> dict:
    if cid == PRESET_COLLECTION:
        return _preset_collection()
    coll = _library().get_collection(cid)
    if coll is None:
        raise HTTPException(status_code=404, detail="资料库不存在")
    docs = coll.get("documents") or []
    coll["builtin"] = False
    coll["documents"] = docs
    coll["chunks"] = sum(int(d.get("chunks") or 0) for d in docs)
    return coll


@app.delete("/collections/{cid}")
def collection_delete(cid: str, _: None = Depends(enforce_content)) -> dict:
    lib = _library()
    try:
        ok = lib.delete_collection(cid)
    except LibraryError as e:
        raise _lib_error(e)
    if not ok:
        raise HTTPException(status_code=404, detail="资料库不存在")
    _state["doc_pipelines"].pop(cid, None)
    return {"ok": True, "id": cid}


@app.post("/collections/{cid}/documents")
def document_add(cid: str, req: DocumentRequest,
                 _: None = Depends(enforce_guard)) -> dict:
    """把一段资料加进知识库：切分、算向量、入库，立即可问答。

    出题接口不落盘；这里是反向的——用户明确要把资料存成可检索的库。
    """
    lib = _library()
    try:
        doc = lib.add_document(cid, req.text, req.title)
    except LibraryError as e:
        raise _lib_error(e)
    _state["doc_pipelines"].pop(cid, None)
    return doc


@app.get("/collections/{cid}/documents/{doc_id}")
def document_detail(cid: str, doc_id: str,
                    _: None = Depends(enforce_content)) -> dict:
    lib = _library()
    if lib.get_collection(cid) is None:
        raise HTTPException(status_code=404, detail="资料库不存在")
    row = lib.get_document(cid, doc_id)
    if row is None:
        raise HTTPException(status_code=404, detail="资料不存在")
    return {"id": row["id"], "title": row["title"], "text": row["text"],
            "created_at": str(row.get("created_at") or "")}


@app.delete("/collections/{cid}/documents/{doc_id}")
def document_delete(cid: str, doc_id: str,
                    _: None = Depends(enforce_content)) -> dict:
    ok = _library().delete_document(cid, doc_id)
    if not ok:
        raise HTTPException(status_code=404, detail="资料不存在")
    _state["doc_pipelines"].pop(cid, None)
    return {"ok": True, "id": doc_id}


@app.post("/bank/generate")
def bank_generate(req: GenerateQuestionsRequest,
                  _: None = Depends(enforce_guard)) -> dict:
    """用自己的资料出题：题目与出处只在响应里返回，服务端不保存资料。"""
    provider = _state.get("provider_impl")
    if _state.get("provider") != "deepseek" or provider is None:
        raise HTTPException(status_code=503,
                            detail="当前是离线演示模式，配置 DEEPSEEK_API_KEY 后才能用资料出题")
    try:
        out = generate_questions(provider, req.text, count=req.count, title=req.title)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ProviderError as e:
        raise HTTPException(status_code=502, detail="出题失败：%s" % e)
    if not out["questions"]:
        note = out["notes"][0] if out["notes"] else "模型没有给出可用题目"
        raise HTTPException(status_code=422,
                            detail="没有生成可用题目（%s），换一段更完整的资料试试" % note)
    return out


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")
