# -*- coding: utf-8 -*-
"""HTTP 服务：多轮问答 Agent + 题库浏览 + 健康检查。

启动（仓库根目录）：
  py -m uvicorn api.app:app --host 0.0.0.0 --port 8000

环境变量：
  KB_INPUT          题库 JSONL，逗号分隔，默认 data/work/2019_draft.jsonl
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
"""
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from answer.agent import QueryAgent, chitchat_reply
from answer.gate import RefusalGate
from answer.pipeline import AnswerPipeline
from answer.providers import DeepSeekProvider, ExtractiveProvider, ProviderError
from retrieval.chunk import build_chunk, build_knowledge_chunk, load_questions
from retrieval.search import build_searcher

from .catalog import brief, detail, filter_questions, subject_counts
from .gap import log_gap
from .guard import AccessGuard
from .knowledge import (filter_knowledge, know_detail, knowledge_chapters,
                        knowledge_for_question, knowledge_subject_counts,
                        load_knowledge, related_knowledge_by_question)

ROOT = Path(__file__).resolve().parents[1]

_state = {}
_lock = threading.Lock()
GUARD = AccessGuard.from_env()


class AskRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)


class ChatTurn(BaseModel):
    role: str = Field("user", pattern="^(user|assistant)$")
    content: str = Field("", max_length=4000)


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    history: List[ChatTurn] = Field(default_factory=list)


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
    raw = os.environ.get("KB_INPUT", "data/work/2019_draft.jsonl")
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
    searcher = build_searcher(chunks, mode=mode, k_fuse=fuse_k, k_final=5)

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

    # 阈值来自 answer.eval_gate 在「真题 47 + 考点 42」混合语料上的标定
    # （32 条正样本 + 16 条负样本）：0.1/0 时负样本拒答 93.8%、正样本误拒 9.4%。
    # 分差阈值会误伤「多个考点都相关」的问题，混合语料下默认关闭。
    default_score = "0.1" if mode == "hybrid-ce" else "0"
    default_margin = "0"
    gate = RefusalGate(float(os.environ.get("KB_MIN_SCORE", default_score)),
                       float(os.environ.get("KB_MIN_MARGIN", default_margin)))
    gap_path = Path(os.environ.get("KB_GAP_LOG", "data/work/gap_log.jsonl"))
    if not gap_path.is_absolute():
        gap_path = ROOT / gap_path
    gap_path.parent.mkdir(parents=True, exist_ok=True)

    # 只有真实模型才做追问改写；mock 模式退化为规则拼接，保证离线也能跑。
    agent = QueryAgent(provider if provider_name == "deepseek" else None)

    _state.update({
        "pipeline": AnswerPipeline(searcher, provider, gate=gate),
        "chunks": chunks,
        "questions": questions,
        "questions_by_id": {q["id"]: q for q in questions},
        "knowledge": knowledge,
        "knowledge_by_id": {c["id"]: c for c in knowledge},
        "knowledge_by_question": related_knowledge_by_question(knowledge),
        "mode": mode,
        "fuse_k": fuse_k,
        "provider": provider_name,
        "gap_log": str(gap_path),
        "agent": agent,
    })


@asynccontextmanager
async def lifespan(_: FastAPI):
    _load()
    yield


app = FastAPI(title="408 考研知识库", version="0.7", lifespan=lifespan)


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


def _steps(res, plan=None) -> list:
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

    know_n = len(_state.get("knowledge", []))
    corpus = ("真题 %d + 考点 %d" % (len(_state.get("questions", [])), know_n)
              if know_n else "真题 %d" % len(_state.get("questions", [])))
    steps.append({"name": "检索", "ms": timings.get("retrieve", 0),
                  "detail": "%s：BM25 + 向量双路召回 → RRF → Cross-Encoder 精排" % corpus})

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


def _answer_payload(res, t0: float, plan=None) -> dict:
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
            "steps": _steps(res, plan),
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


def _log_gap(query: str, res) -> None:
    if not res.refused:
        return
    try:
        log_gap(_state["gap_log"], query, res.reason,
                res.hits[0] if res.hits else None)
    except OSError:
        pass  # 日志写失败不能影响答题


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
        "auth_required": GUARD.token_required,
    }


@app.post("/ask")
def ask(req: AskRequest, _: None = Depends(enforce_guard)) -> dict:
    pipeline = _state.get("pipeline")
    if pipeline is None:
        return {"refused": True, "answer": "", "citations": [],
                "reason": "server_not_ready", "hits": [], "latency_ms": 0}
    t0 = time.time()
    # 模型推理是 CPU 密集的，单 worker 内串行，避免线程争抢
    with _lock:
        res = pipeline.answer(req.query)
    _log_gap(req.query, res)
    return _answer_payload(res, t0)


@app.post("/chat")
def chat(req: ChatRequest, _: None = Depends(enforce_guard)) -> dict:
    agent = _state.get("agent")
    pipeline = _state.get("pipeline")
    if agent is None or pipeline is None:
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
    if plan.intent in ("chitchat", "clarify"):
        return _non_kb_payload(plan, t0)

    with _lock:
        res = pipeline.answer(plan.rewritten)
    _log_gap(req.query, res)
    return _answer_payload(res, t0, plan)


@app.get("/meta")
def meta(_: None = Depends(enforce_content)) -> dict:
    questions = _state.get("questions", [])
    years = sorted({int(q.get("year")) for q in questions if q.get("year")})
    knowledge = _state.get("knowledge", [])
    return {
        "chunks": len(questions),
        "subjects": subject_counts(questions),
        "knowledge": len(knowledge),
        "knowledge_subjects": knowledge_subject_counts(knowledge),
        "years": years,
        "mode": _state.get("mode"),
        "provider": _state.get("provider"),
        "auth_required": GUARD.token_required,
    }


@app.get("/knowledge")
def knowledge_list(q: str = "", subject: str = "", chapter: str = "",
                   limit: int = 100, offset: int = 0,
                   _: None = Depends(enforce_content)) -> dict:
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    cards = _state.get("knowledge", [])
    total, rows = filter_knowledge(cards, q, subject, chapter, limit, offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [know_detail(c, _state.get("questions_by_id", {})) for c in rows],
        "subjects": knowledge_subject_counts(cards),
        "chapters": knowledge_chapters(cards, subject),
    }


@app.get("/knowledge/{card_id}")
def knowledge_detail(card_id: str, _: None = Depends(enforce_content)) -> dict:
    card = _state.get("knowledge_by_id", {}).get(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="考点不存在")
    return know_detail(card, _state.get("questions_by_id", {}))


@app.get("/chunks")
def chunks(q: str = "", subject: str = "", year: Optional[int] = None,
           limit: int = 20, offset: int = 0,
           _: None = Depends(enforce_content)) -> dict:
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
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
    row = _state.get("questions_by_id", {}).get(chunk_id)
    if row is None:
        raise HTTPException(status_code=404, detail="题目不存在")
    item = detail(row)
    item["related_knowledge"] = _state.get("knowledge_by_question", {}).get(chunk_id, [])
    return item


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")
