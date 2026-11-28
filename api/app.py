# -*- coding: utf-8 -*-
"""HTTP 服务：多轮问答 Agent + 知识库 + 健康检查。

启动（仓库根目录）：
  py -m uvicorn api.app:app --host 0.0.0.0 --port 8000

环境变量：
  DATABASE_URL      PostgreSQL 连接串；设置后知识库、账号与缓存都走数据库
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
  KB_ADMIN_USERS    知识库管理员用户名，逗号分隔；不配时第一个注册的账号兜底
"""
import hashlib
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from answer.agent import QueryAgent, chitchat_reply
from answer.auto import fallback_detail, should_fallback
from answer.daily import (daily_reply, daily_stream, general_reply,
                          general_stream)
from answer import docgen
from answer.pipeline import AnswerPipeline, AnswerResult
from answer.tools import (_url_allowed, run_tools, tool_context, tool_will_run)
from answer.cache import (cache_key, cacheable, cached_payload, namespace,
                          storable_payload, ttl_from_env, with_cache_meta)
from answer.gate import RefusalGate
from answer.providers import DeepSeekProvider, ExtractiveProvider, ProviderError
from answer.stream import sse
from kb import Library, LibraryError
from kb.defaults import ensure_default_library
from kb.parsers import ParseError, extension_of, extract_document
from kb.weknora import WeKnora, WeKnoraError, WeKnoraSearcher
from retrieval.vector import BgeEmbedder

from db.store import Store

from .access import (ASSIGNABLE_ROLES, VISIBILITY_PRIVATE,
                     VISIBILITY_SHARED, can_manage, can_read, can_write,
                     describe, is_admin)
from .accounts import AccountError, AccountService
from .feedback import (feedback_stats as jsonl_feedback_stats,
                       log_feedback as jsonl_log_feedback, normalize_feedback)
from .gap import gap_detail, gap_stats, log_gap
from .guard import AccessGuard
from . import metrics
from .tools_runtime import TOOL_WAIT_S, _live_query, _split_tools, _tool_results
from .version import version_info

ROOT = Path(__file__).resolve().parents[1]

_state = {}
_lock = threading.Lock()
# Agent 流式请求最多两路并发：一路卡住时另一路仍能回答；
# 超过并发直接回「忙」，不要排队到一两分钟。
_agent_slots = threading.Semaphore(2)
GUARD = AccessGuard.from_env()

# 工具调用打的是外部接口，和检索（含精排，2C 机器上 5~7 秒）并行跑，
# 等回答真的降级到通用链路时再取结果，不占关键路径。
_TOOLS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tools")


class AskRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    # 讲解风格（人格预设 / 技能 / 学习记忆），只影响措辞，不改变引用规则
    style: str = Field("", max_length=400)
    # 检索哪个知识库：空字符串 = 默认知识库，其余是用户自建资料库
    collection: str = Field("", max_length=64)


class ChatTurn(BaseModel):
    role: str = Field("user", pattern="^(user|assistant)$")
    content: str = Field("", max_length=4000)


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    history: List[ChatTurn] = Field(default_factory=list)
    style: str = Field("", max_length=400)
    collection: str = Field("", max_length=64)
    # auto（默认）= 先检索，有依据就带引用，没依据改用标明的通用回答；
    # kb = 严格只按资料原文回答，资料不够就拒答（给脚本 / 老客户端用）；
    # chat = 旧版日常对话，不检索、不带引用
    mode: str = Field("auto", max_length=8)
    # 设置里开着的工具名（服务端只认 answer/tools.py 的注册表）；资料模式一律忽略
    tools: List[str] = Field(default_factory=list, max_length=16)
    # 学习进度只存在浏览器里，由前端带一份摘要给「学习进度」工具用
    study: Dict[str, Any] = Field(default_factory=dict)


class AgentRequest(BaseModel):
    """Agent 模式：多轮带线程记忆，工具调用与校验轨迹随 SSE 返回。"""
    query: str = Field(..., min_length=1, max_length=500)
    # 线程号由前端生成或服务端首次返回；同一账号才能续写同一线程
    thread_id: str = Field("", max_length=64)
    history: List[ChatTurn] = Field(default_factory=list)
    style: str = Field("", max_length=400)
    collection: str = Field("", max_length=64)
    # 设置页里开着的工具名；Agent 自己决定用不用
    tools: List[str] = Field(default_factory=list, max_length=16)
    study: Dict[str, Any] = Field(default_factory=dict)


class CollectionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    description: str = Field("", max_length=200)
    # 个人库是更安全的默认；建公司库只有管理员可以。
    visibility: str = Field(VISIBILITY_PRIVATE,
                            pattern="^(private|shared)$")


class CollectionMemberRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    role: str = Field("viewer", pattern="^(editor|viewer)$")


class DocumentRequest(BaseModel):
    # 用户在页面里粘贴的资料；切分后只有片段进检索库，原文整篇留档以便回跳
    text: str = Field(..., min_length=20, max_length=50000)
    title: str = Field("", max_length=80)


class UrlDocumentRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2000)
    title: str = Field("", max_length=80)


class DocumentEditRequest(BaseModel):
    # 整篇编辑：标题可改，正文至少 20 字；保存后重新切分并重建引用
    text: str = Field(..., min_length=20, max_length=50000)
    title: str = Field("", max_length=80)


class ChunkUpdateRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200000)
    # WeKnora 的乐观并发版本号；不传就不做版本校验
    revision: Optional[int] = Field(None, ge=0)


class CreateOutlineRequest(BaseModel):
    topic: str = Field(..., min_length=2, max_length=100)
    kind: str = Field("doc", pattern="^(doc|slides)$")
    points: int = Field(5, ge=3, le=10)
    # 可选补充要点：离线演示模式直接用它排章节，接上模型时当写作材料
    notes: str = Field("", max_length=2000)


class CreateExportRequest(BaseModel):
    format: str = Field(..., pattern="^(md|docx|pptx)$")
    outline: Dict[str, Any] = Field(default_factory=dict)
    # PPT 配色：陶土 / 墨蓝 / 黑白；md、docx 忽略
    style: str = Field("clay", pattern="^(clay|ink|mono)$")


class FeedbackRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    helpful: bool
    reason: str = Field("", max_length=40)
    note: str = Field("", max_length=500)
    answer: str = Field("", max_length=4000)
    citations: List[str] = Field(default_factory=list)
    cached: bool = False


class CredentialsRequest(BaseModel):
    # 最小长度交给账号层判，错误信息才能是中文而不是 pydantic 的 422
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class PasswordChangeRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=1, max_length=128)


class AdminUserRequest(BaseModel):
    # 两个都可选，只改传上来的那个；都不传由服务层回 400
    role: Optional[str] = Field(None, max_length=16)
    disabled: Optional[bool] = None


class AdminResetRequest(BaseModel):
    # 不传就由服务端生成 12 位临时密码
    new_password: Optional[str] = Field(None, max_length=128)


class SyncItemRequest(BaseModel):
    value: Any = None
    updated_at: int = 0


class SyncRequest(BaseModel):
    items: Dict[str, SyncItemRequest] = Field(default_factory=dict)


class DeleteDataRequest(BaseModel):
    keys: List[str] = Field(default_factory=list)
    all: bool = False


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
    """知识库浏览：只校验口令，不占问答额度。"""
    _enforce(request, False)


def _accounts() -> AccountService:
    """账号服务：有数据库就落库，没有就退到进程内存。"""
    svc = _state.get("accounts")
    if svc is None:
        svc = AccountService(_state.get("store"))
        _state["accounts"] = svc
    return svc


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization") or ""
    return raw[7:].strip() if raw[:7].lower() == "bearer " else ""


def current_user(request: Request) -> Dict[str, Any]:
    user = _accounts().authenticate(_bearer(request))
    if user is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return user


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    return GUARD.client_ip(request.headers, peer)


def _resolve(path: str) -> str:
    return str((ROOT / path) if not os.path.isabs(path) else Path(path))


def _init_agent_runtime() -> None:
    """Agent 是可选的：没装 langgraph 或记忆后端连不上时，老接口照常工作。"""
    old = _state.pop("agent_handle", None)
    if old is not None:
        old.close()
    try:
        from agent.checkpoint import build_checkpointer
        from agent.runtime import AgentRuntime

        handle = build_checkpointer(os.environ.get("DATABASE_URL", ""))
        _state["agent_rt"] = AgentRuntime(handle.saver, handle.kind)
        _state["agent_handle"] = handle
        _state.pop("agent_error", None)
        print("Agent 记忆：%s" % handle.kind)
    except Exception as e:
        _state["agent_rt"] = None
        _state["agent_handle"] = None
        _state["agent_error"] = "%s: %s" % (type(e).__name__, e)
        print("Agent 未启用：%s" % _state["agent_error"])


class _EmptySearcher:
    """默认库还没资料时的空检索器：链路照常启动，问题由拒答闸门处理。"""

    embedder = None
    chunks: List[Any] = []

    def search(self, query: str, skip_rerank: bool = False) -> List[Any]:
        return []


def _load() -> None:
    mode = os.environ.get("KB_MODE", "hybrid")
    # CPU 上精排 10 个候选约 7 秒/次；30 个要约 27 秒，R@1 只差 0 点。
    fuse_k = int(os.environ.get("KB_FUSE_K", "10"))

    # 配了 DATABASE_URL 就以 Postgres 为准：知识库、账号与缓存都走数据库；
    # 没有数据库时回退内存，便于单测和离线开发。
    store = Store.from_env()
    if store is not None:
        store.init_schema()
    # 用户资料库是唯一的检索语料；这里只准备向量模型。
    embedder = BgeEmbedder() if mode != "bm25" else None

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

    # 默认阈值沿用知识库语料上的标定：0.1/0 时负样本大多被拒、正样本误拒最低。
    # 分差阈值会误伤「多段资料都相关」的问题，默认关闭。
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

    # 知识库：默认库与自建库共用同一套 BM25/向量/精排/拒答，
    # 没有数据库时状态落在 data/work 下。
    library_state = (None if store is not None
                     else str(ROOT / "data/work/kb/library.json"))
    library = Library(store=store, embedder=embedder, mode=mode,
                      k_fuse=fuse_k, state_path=library_state)

    # 默认资料范围：内置「默认知识库」；示例资料随代码走，换真实资料时直接改库。
    default_coll = ensure_default_library(library)
    default_id = (default_coll or {}).get("id", "")
    default_searcher = library.searcher(default_id) if default_id else None
    default_pipeline = AnswerPipeline(
        default_searcher if default_searcher is not None else _EmptySearcher(),
        provider, gate=gate, prompt_mode="doc")
    print("默认知识库：%s" % ((default_coll or {}).get("name") or "未就绪"))

    # 配了 WeKnora 就把知识库的进料、切片、召回交给它；
    # 引用校验与拒答始终在本项目。
    weknora = WeKnora()
    if weknora.configured:
        print("知识库后端：WeKnora %s" % weknora.base_url)

    accounts = AccountService(
        store, admin_names=[n.strip() for n in
                            os.environ.get("KB_ADMIN_USERS", "").split(",")
                            if n.strip()])
    promoted = accounts.ensure_admin()
    if promoted:
        print("知识库管理员：%s" % "、".join(promoted))

    _state.update({
        "pipeline": default_pipeline,
        "default_collection": default_id,
        "library": library,
        "weknora": weknora if weknora.configured else None,
        "gate": gate,
        "doc_pipelines": {},
        "mode": mode,
        "fuse_k": fuse_k,
        "provider": provider_name,
        "provider_impl": provider,
        "storage": "postgres" if store is not None else "memory",
        "store": store,
        "accounts": accounts,
        "gap_log": str(gap_path),
        "feedback_log": str(feedback_path),
        "agent": agent,
        # 缓存命名空间隔离检索模式、provider、精排候选数和默认库版本：
        # 任一变化都换一套键，旧回答不会串到新配置上。
        "cache_ttl": ttl_from_env(),
        "cache_ns": "%s|%s|k%s|%s" % (
            mode, provider_name, fuse_k,
            ((default_coll or {}).get("updated_at") or "")[:16]),
    })
    _init_agent_runtime()


@asynccontextmanager
async def lifespan(_: FastAPI):
    _load()
    try:
        yield
    finally:
        handle = _state.pop("agent_handle", None)
        if handle is not None:
            handle.close()


app = FastAPI(title="言据 · 可引用知识库", version=version_info()["version"],
              lifespan=lifespan)


# ---------- 请求日志 ----------
# 记所有进到 API 的请求，但只留方法、路径、状态、耗时和问句；
# 不记 IP、UA、请求头。写库放到后台线程，日志失败绝不影响问答。

_QUERY_PATHS = {"/ask", "/chat", "/chat/stream", "/agent/stream"}
_LOG_SKIP = {"/health", "/favicon.ico"}


def _client_tag(request: Request) -> str:
    peer = request.client.host if request.client else ""
    return hashlib.sha256(peer.encode("utf-8")).hexdigest()[:12] if peer else ""


def _write_request_log(method: str, path: str, status: int,
                       latency_ms: int, query: str, client: str) -> None:
    store = _state.get("store")
    if store is None:
        return

    def run():
        try:
            store.log_request(method, path, status, latency_ms, query, client)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


@app.middleware("http")
async def request_log_middleware(request: Request, call_next):
    t0 = time.time()
    query = ""
    if request.method == "POST" and request.url.path in _QUERY_PATHS:
        try:
            payload = json.loads((await request.body()) or b"{}")
            query = str(payload.get("query") or "")[:200]
        except (ValueError, UnicodeDecodeError):
            query = ""
    response = await call_next(request)
    if request.url.path not in _LOG_SKIP:
        _write_request_log(request.method, request.url.path,
                           response.status_code,
                           int((time.time() - t0) * 1000),
                           query, _client_tag(request))
    return response


def _is_preset(collection: str) -> bool:
    default = _state.get("default_collection")
    return not collection or bool(default and collection == default)


def _collection_ref(collection: str) -> str:
    """引用跳转要拿到真实库 id：默认公司库虽然走 preset 管线，也必须有 id。"""
    if _is_preset(collection):
        return _state.get("default_collection") or ""
    return collection or ""


def _default_collection_item(user: Optional[Dict]) -> Optional[Dict]:
    """把内置默认库整理成列表项：未登录也能看到，标 builtin 后只读。"""
    cid = _state.get("default_collection") or ""
    lib = _state.get("library")
    if not cid or lib is None:
        return None
    coll = lib.get_collection(cid)
    if coll is None:
        return None
    coll["builtin"] = True
    return describe(user, coll, "")


def _pipeline_for(collection: str):
    """按知识库取问答管线；用户库各自一份，库里资料变了就重建。

    返回 None 表示资料库存在但还没有资料（应答方要给出明确提示，
    而不是装作「服务器没准备好」）。
    """
    if _is_preset(collection):
        return _state.get("pipeline")
    wk = _weknora()
    if wk is not None:
        coll = wk.get_collection(collection)
        if coll is None:
            raise HTTPException(status_code=404, detail="资料库不存在")
        if wk.document_count(collection) == 0:
            return None
        pipes = _state["doc_pipelines"]
        key = ("weknora", collection, wk.document_count(collection))
        cached = pipes.get(collection)
        if cached and cached[0] == key:
            return cached[1]
        pipe = AnswerPipeline(WeKnoraSearcher(wk, collection, coll["name"]),
                              _state["provider_impl"],
                              gate=_state.get("gate"), prompt_mode="doc")
        pipes[collection] = (key, pipe)
        return pipe
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


def _agent_owner(request: Request) -> str:
    """线程归属：登录用户按 user id，匿名按 IP 哈希；换人续写会新建线程。"""
    token = _bearer(request)
    if token:
        try:
            user = _accounts().authenticate(token)
        except Exception:
            user = None
        if user and user.get("id") is not None:
            return "user:%s" % user["id"]
    # 用 _client_ip 而不是 _client_tag：放在 Nginx 后面时才能拿到真实客户端 IP
    ip = _client_ip(request)
    tag = hashlib.sha256(ip.encode("utf-8")).hexdigest()[:12] if ip else ""
    return "anon:%s" % tag


def _agent_deps(pipeline, tools=None, collection=""):
    """把现有管线、通用对话、工具与路由包成 Agent 依赖。"""
    from agent.nodes import AgentDeps
    from agent.route import route_query
    from answer.daily import daily_reply, daily_stream
    from answer.tools import run_tools, tool_context

    provider = pipeline.provider
    enabled = list(tools or [])

    def overview():
        chunks = list(getattr(pipeline.searcher, "chunks", []) or [])
        titles = []
        for chunk in chunks:
            title = (chunk.title
                     or (chunk.source_ref or {}).get("document_title") or "")
            if title and title not in titles:
                titles.append(title)
        name = "默认知识库"
        if collection and not _is_preset(collection):
            try:
                coll = _library().get_collection(collection) or {}
            except Exception:
                coll = {}
            name = coll.get("name") or name
        return {"name": name, "documents": len(titles),
                "chunks": len(chunks), "titles": titles}

    return AgentDeps(
        search=lambda q, skip_rerank=False: pipeline.searcher.search(
            q, skip_rerank=skip_rerank),
        prepare=pipeline.prepare,
        provider=provider,
        gate=pipeline.gate,
        agent=_state.get("agent"),
        prompt_mode=pipeline.prompt_mode,
        top_k=5,
        tools=enabled,
        router=lambda q, h, c: route_query(q, h, c, provider, enabled),
        chat_reply=lambda q, h, s, tt="": daily_reply(
            provider, q, h, s, tt),
        chat_stream=lambda q, h, s, tt="": daily_stream(
            provider, q, h, s, tt),
        run_tools_fn=lambda names, q, study: run_tools(names, q, study),
        tool_context_fn=tool_context,
        overview=overview,
    )


def _collection_count() -> int:
    wk = _weknora()
    if wk is not None:
        try:
            return len(wk.list_collections())
        except Exception:  # 统计失败不能让健康检查挂掉
            return 0
    lib = _state.get("library")
    if lib is None:
        return 0
    try:
        return len(lib.list_collections())
    except Exception:  # 统计失败不能让健康检查挂掉
        return 0


def _corpus_label(collection: str) -> str:
    if not _is_preset(collection):
        wk = _weknora()
        if wk is not None:
            try:
                coll = wk.get_collection(collection)
            except WeKnoraError:
                coll = None
            if coll:
                return "资料库「%s」" % coll["name"]
        lib = _state.get("library")
        coll = lib.get_collection(collection) if lib is not None else None
        if coll:
            docs = coll.get("documents") or []
            segs = sum(int(d.get("chunks") or 0) for d in docs)
            return "资料库「%s」：%d 篇 / %d 段" % (coll["name"], len(docs), segs)
    return "默认知识库"


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

    if timings.get("rerank_skipped"):
        retrieve_detail = ("%s：实时数据问题，双路召回没有可用证据，跳过精排"
                           % _corpus_label(collection))
    else:
        retrieve_detail = ("%s：BM25 + 向量双路召回 → RRF → Cross-Encoder 精排"
                           % _corpus_label(collection))
    steps.append({"name": "检索", "ms": timings.get("retrieve", 0),
                  "detail": retrieve_detail})

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


def _citation_payload(citations, pool) -> list:
    """把模型给的 chunk_id / quote 补成前端可跳转的引用。"""
    by_id = {h.chunk.id: h.chunk for h in pool}
    out = []
    for c in citations or []:
        if not isinstance(c, dict):
            continue
        cid = c.get("chunk_id")
        ch = by_id.get(cid)
        out.append({
            "chunk_id": cid,
            "quote": c.get("quote"),
            "kind": ch.kind if ch else "question",
            "title": ch.title if ch else "",
            "chapter": ch.chapter if ch else "",
            "subject": ch.subject if ch else "",
            "source_ref": ch.source_ref if ch else {},
        })
    return out


def _answer_payload(res, t0: float, plan=None, collection: str = "") -> dict:
    # 引用可能来自补进来的相邻片段或整表片段，按「实际交给模型的上下文」解析；
    # 召回条数仍按检索结果 res.hits 显示。
    pool = res.context or res.hits
    citations = _citation_payload(res.citations, pool)
    rewritten = plan.rewritten if plan and plan.rewritten != plan.query else ""
    return {
        "intent": plan.intent if plan else "kb",
        "mode": "kb",
        # 前端据此把引用跳回知识库原文；空字符串表示默认知识库
        "collection": _collection_ref(collection),
        "refused": res.refused,
        "answer": res.answer,
        "citations": citations,
        "reason": res.reason,
        "gap_detail": gap_detail(res.reason) if res.refused else "",
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


def _agent_payload(result, t0: float, query: str = "",
                   collection: str = "") -> dict:
    """Agent 的最终响应：字段与 /chat 对齐，另带线程号与工具轨迹。"""
    pool = result.context or result.hits
    citations = _citation_payload(result.citations, pool)
    rewritten = result.rewritten if result.rewritten != query else ""
    steps = list(result.steps or [])
    route = getattr(result, "route", "kb") or "kb"
    tool_results = list(getattr(result, "tools", []) or [])
    return {
        "intent": result.intent,
        "mode": "agent",
        "route": route,
        "collection": _collection_ref(collection),
        "thread_id": result.thread_id,
        "thread_reset": bool(result.thread_reset),
        "refused": result.refused,
        "answer": result.answer,
        "citations": citations,
        "reason": result.reason,
        "gap_detail": gap_detail(result.reason) if result.refused else "",
        "hits": [_hit_payload(h) for h in result.hits],
        "attempts": result.attempts,
        "latency_ms": int((time.time() - t0) * 1000),
        "rewritten_query": rewritten,
        "tool_calls": list(result.tool_calls or []),
        "tools": tool_results,
        "trace": {
            "intent": result.intent,
            "rewritten_query": rewritten,
            "steps": steps,
        },
        "agent": {
            "checkpointer": result.checkpointer,
            "thread_id": result.thread_id,
            "thread_reset": bool(result.thread_reset),
            "attempts": result.attempts,
            "retry": result.attempts > 1,
            "route": route,
            "tool_calls": list(result.tool_calls or []),
            "tools": tool_results,
        },
    }


def _agent_unavailable_payload(t0: float) -> dict:
    """langgraph 没装上时的明确失败，不假装回答。"""
    return {
        "intent": "kb", "mode": "agent", "collection": "",
        "thread_id": "", "thread_reset": False,
        "refused": True, "answer": "", "citations": [],
        "reason": "agent_unavailable", "gap_detail": "",
        "hits": [], "attempts": 0,
        "latency_ms": int((time.time() - t0) * 1000),
        "rewritten_query": "", "tool_calls": [],
        "trace": {"intent": "kb", "rewritten_query": "", "steps": []},
        "agent": {"checkpointer": "none", "thread_id": "",
                  "thread_reset": False, "attempts": 0, "retry": False,
                  "tool_calls": []},
        "notice": "Agent 运行时未启用，请检查服务端依赖与日志",
    }


def _general_payload(res, answer: str, t0: float, plan=None,
                     collection: str = "", tools: Optional[List[dict]] = None) -> dict:
    """资料没有依据时的通用回答：不带引用，但保留召回与缺口，方便核对。"""
    rewritten = plan.rewritten if plan and plan.rewritten != plan.query else ""
    steps = _steps(res, plan, collection)
    steps.append({"name": "通用回答", "ms": 0,
                  "detail": fallback_detail(res.reason)})
    for t in tools or []:
        steps.append({"name": t.get("label", "工具"), "ms": 0,
                      "detail": ("已取到实时数据" if t.get("ok")
                                 else (t.get("detail") or "调用失败"))})
    return {
        "intent": plan.intent if plan else "kb",
        "mode": "general",
        "collection": _collection_ref(collection),
        "refused": False,
        "answer": answer,
        "citations": [],
        "reason": "",
        "fallback_reason": res.reason,
        "gap_detail": gap_detail(res.reason),
        "hits": [_hit_payload(h) for h in res.hits],
        "attempts": res.attempts,
        "tools": tools or [],
        "latency_ms": int((time.time() - t0) * 1000),
        "rewritten_query": rewritten,
        "trace": {
            "intent": plan.intent if plan else "kb",
            "rewritten_query": rewritten,
            "steps": steps,
        },
    }


def _general_error_payload(res, err: ProviderError, t0: float, plan=None,
                           collection: str = "") -> dict:
    """兜底生成也失败：如实拒答，不把空回答当成通用答案。"""
    failed = AnswerResult(True, reason="provider_error: %s" % err,
                          hits=res.hits, attempts=res.attempts)
    return _answer_payload(failed, t0, plan, collection)


def _non_kb_payload(plan, t0: float) -> dict:
    if plan.intent == "chitchat":
        answer = chitchat_reply(plan.query)
    else:
        answer = ("我不确定你指的是哪道题。可以把题号或关键词补上，"
                  "例如「2019 年第 31 题」或「快速排序的时间复杂度」。")
    return {
        "intent": plan.intent,
        "mode": "kb",
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


def _daily_payload(answer: str, t0: float, reason: str = "",
                   tools: Optional[List[dict]] = None) -> dict:
    """日常对话的返回：没有引用、没有命中，也不进缓存和缺口日志。"""
    ms = int((time.time() - t0) * 1000)
    steps = []
    for t in tools or []:
        steps.append({"name": t.get("label", "工具"),
                      "ms": 0,
                      "detail": ("已取到实时数据" if t.get("ok")
                                 else (t.get("detail") or "调用失败"))})
    steps.append({"name": "日常对话", "ms": ms,
                  "detail": ("不检索资料，回答里没有引用" if not reason
                             else "生成服务不可用，没有作答")})
    return {
        "intent": "chat", "mode": "chat", "refused": bool(reason),
        "answer": "" if reason else answer,
        "citations": [], "reason": reason, "hits": [], "attempts": 0,
        "tools": tools or [],
        "latency_ms": ms, "rewritten_query": "",
        "trace": {"intent": "chat", "rewritten_query": "", "steps": steps},
    }


def _history(req: ChatRequest) -> List[dict]:
    return [turn.model_dump() if hasattr(turn, "model_dump") else turn.dict()
            for turn in req.history]


def _log_gap(query: str, res, collection: str = "") -> None:
    if not res.refused:
        return
    # 用户库的缺口单独标出来：reason 里带库 id，看板才能区分
    # 「默认库没覆盖」和「这个人自己的资料没覆盖」。
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
    if not cacheable(query, _is_preset(collection)):
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
    if not cacheable(query, _is_preset(collection)):
        return
    key = cache_key(query, _cache_namespace(style, collection))
    try:
        store.put_answer_cache(key, query, storable_payload(payload), ttl)
    except Exception:
        pass  # 写缓存失败不能影响答题


@app.get("/version")
def version() -> dict:
    """公开的版本信息：页脚、设置页和排障脚本都看这一份。"""
    return version_info()


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": version_info()["version"],
        "mode": _state.get("mode"),
        "fuse_k": _state.get("fuse_k"),
        "provider": _state.get("provider"),
        "storage": _state.get("storage", "memory"),
        "auth_required": GUARD.token_required,
        "cache_ttl_s": _state.get("cache_ttl", 0),
        "collections": _collection_count(),
        "kb_backend": "weknora" if _weknora() is not None else "local",
        "agent": bool(_state.get("agent_rt")),
        "agent_checkpointer": getattr(_state.get("agent_handle"), "kind",
                                      "none"),
    }


@app.post("/auth/register")
def auth_register(req: CredentialsRequest, request: Request,
                  _: None = Depends(enforce_content)) -> dict:
    try:
        return _accounts().register(req.username, req.password,
                                    ip=_client_ip(request))
    except AccountError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@app.post("/auth/login")
def auth_login(req: CredentialsRequest, request: Request,
               _: None = Depends(enforce_content)) -> dict:
    try:
        return _accounts().login(req.username, req.password,
                                 ip=_client_ip(request))
    except AccountError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@app.post("/auth/logout")
def auth_logout(request: Request, _: None = Depends(enforce_content)) -> dict:
    _accounts().logout(_bearer(request))
    return {"ok": True}


@app.post("/auth/password")
def auth_password(req: PasswordChangeRequest, request: Request,
                  user: Dict[str, Any] = Depends(current_user),
                  _: None = Depends(enforce_content)) -> dict:
    try:
        return _accounts().change_password(
            user["id"], req.old_password, req.new_password,
            ip=_client_ip(request), keep_token=_bearer(request))
    except AccountError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@app.get("/auth/me")
def auth_me(user: Dict[str, Any] = Depends(current_user),
            _: None = Depends(enforce_content)) -> dict:
    return {"user": user}


@app.get("/admin/users")
def admin_users(user: Dict[str, Any] = Depends(current_user),
                _: None = Depends(enforce_content)) -> dict:
    try:
        return {"users": _accounts().list_users(user)}
    except AccountError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@app.patch("/admin/users/{user_id}")
def admin_user_update(user_id: str, req: AdminUserRequest,
                      user: Dict[str, Any] = Depends(current_user),
                      _: None = Depends(enforce_content)) -> dict:
    try:
        out = _accounts().update_user(user, user_id, role=req.role,
                                      disabled=req.disabled)
        return {"user": out}
    except AccountError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@app.post("/admin/users/{user_id}/reset-password")
def admin_user_reset_password(user_id: str,
                              req: Optional[AdminResetRequest] = None,
                              user: Dict[str, Any] = Depends(current_user),
                              _: None = Depends(enforce_content)) -> dict:
    try:
        return _accounts().reset_password(
            user, user_id, new_password=(req.new_password if req else None))
    except AccountError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@app.get("/me/data")
def me_data_get(user: Dict[str, Any] = Depends(current_user),
                _: None = Depends(enforce_content)) -> dict:
    return {"items": _accounts().get_data(user["id"])}


@app.put("/me/data")
def me_data_put(req: SyncRequest, user: Dict[str, Any] = Depends(current_user),
                _: None = Depends(enforce_content)) -> dict:
    try:
        items = {k: (v.model_dump() if hasattr(v, "model_dump") else v.dict())
                 for k, v in req.items.items()}
        return _accounts().put_data(user["id"], items)
    except AccountError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@app.delete("/me/data")
def me_data_delete(req: DeleteDataRequest,
                   user: Dict[str, Any] = Depends(current_user),
                   _: None = Depends(enforce_content)) -> dict:
    removed = _accounts().delete_data(user["id"], req.keys,
                                      delete_all=bool(req.all))
    return {"removed": removed}


@app.post("/ask")
def ask(req: AskRequest, request: Request,
        _: None = Depends(enforce_guard)) -> dict:
    _guard_chat_collection(req.collection, request)
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


def _daily_chat(req: ChatRequest) -> dict:
    """日常对话：不检索、不校验引用，也不写缺口日志和缓存。"""
    t0 = time.time()
    metrics.bump("chat_total")
    metrics.bump("chat_daily")
    tools = run_tools(req.tools, req.query, req.study)
    try:
        text = daily_reply(_state.get("provider_impl"), req.query,
                           _history(req), _clean_style(req.style),
                           tool_context(tools))
    except ProviderError as e:
        metrics.bump("chat_refused")
        return with_cache_meta(_daily_payload("", t0, "provider_error: %s" % e, tools))
    metrics.bump("chat_answered")
    return with_cache_meta(_daily_payload(text, t0, "", tools))


def _daily_events(req: ChatRequest):
    """日常对话的 SSE：status(daily) → status(tool) → delta* → done。"""
    t0 = time.time()
    metrics.bump("chat_total")
    metrics.bump("chat_daily")
    yield sse({"type": "status", "stage": "daily"})
    tools = []
    if req.tools and tool_will_run(req.tools, req.query):
        # 先说要查什么，工具调用有时要一两秒，别让界面停着
        yield sse({"type": "status", "stage": "tool", "label": "实时数据"})
        tools = run_tools(req.tools, req.query, req.study)
        yield sse({"type": "status", "stage": "daily"})
    parts = []
    try:
        for piece in daily_stream(_state.get("provider_impl"), req.query,
                                  _history(req), _clean_style(req.style),
                                  tool_context(tools)):
            if not piece:
                continue
            parts.append(piece)
            yield sse({"type": "delta", "text": piece})
    except ProviderError as e:
        metrics.bump("chat_refused")
        reason = "provider_error: %s" % e
        yield sse({"type": "error", "message": "生成服务暂时不可用"})
        yield sse({"type": "done",
                   "data": with_cache_meta(_daily_payload("", t0, reason, tools))})
        return
    metrics.bump("chat_answered")
    yield sse({"type": "done",
               "data": with_cache_meta(_daily_payload("".join(parts), t0, "", tools))})


@app.post("/chat")
def chat(req: ChatRequest, request: Request,
         _: None = Depends(enforce_guard)) -> dict:
    if req.mode == "chat":
        return _daily_chat(req)
    _guard_chat_collection(req.collection, request)
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
    history = _history(req)

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

    fast_tools, slow_tools = _split_tools(req.tools)
    tools_future = None
    if fast_tools and tool_will_run(fast_tools, req.query):
        tools_future = _TOOLS_POOL.submit(run_tools, fast_tools,
                                          req.query, req.study)
    live = _live_query(req.tools, req.query)
    with _lock:
        res = pipeline.answer(plan.rewritten, style, skip_rerank=live)
    _log_gap(req.query, res, req.collection)

    # auto：资料里查不到可直接引用的依据时，改给明说的通用回答；
    # kb（老客户端 / 脚本）保持严格拒答。
    if req.mode != "kb" and should_fallback(res):
        metrics.bump("chat_fallback")
        tools = _tool_results(tools_future) + run_tools(slow_tools, req.query,
                                                        req.study)
        try:
            text = general_reply(_state.get("provider_impl"), plan.rewritten,
                                 history, style, tool_context(tools))
        except ProviderError as e:
            metrics.bump("chat_refused")
            return with_cache_meta(
                _general_error_payload(res, e, t0, plan, req.collection))
        metrics.bump("chat_answered")
        return with_cache_meta(
            _general_payload(res, text, t0, plan, req.collection, tools))

    metrics.bump("chat_refused" if res.refused else "chat_answered")
    payload = with_cache_meta(
        _answer_payload(res, t0, plan, req.collection))
    _cache_store(plan.rewritten, payload, style, req.collection)
    return payload


@app.post("/chat/stream")
def chat_stream(req: ChatRequest, request: Request,
                _: None = Depends(enforce_guard)):
    """流式问答：先按 SSE 吐 answer 文本，收完再校验引用。

    事件顺序：status(plan) → status(retrieve/gate) → delta* →
    status(verify) → done。校验失败会先发 reset 让前端清空，再重试一次；
    最终结果只认 done 里的 payload——引用校验和拒答规则与非流式完全一致。
    """
    if req.mode != "chat":
        _guard_chat_collection(req.collection, request)
    agent = _state.get("agent")
    # 日常对话不碰检索库，连管线都不取，免得一个过期的库 id 把闲聊挡在 404
    pipeline = None if req.mode == "chat" else _pipeline_for(req.collection)

    def events():
        if req.mode == "chat":
            yield from _daily_events(req)
            return
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
        history = _history(req)

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

        # 轻量工具先挂到后台跑，和检索并行；真的降级到通用链路时再取结果。
        fast_tools, slow_tools = _split_tools(req.tools)
        tools_future = None
        if fast_tools and tool_will_run(fast_tools, req.query):
            tools_future = _TOOLS_POOL.submit(run_tools, fast_tools,
                                              req.query, req.study)
        # 天气 / 日期 / 算术这类问的是此刻的事实，资料库里不会有可引用原文：
        # 双路召回没有像样证据时跳过 5~7 秒的精排，直接交给通用链路。
        live = _live_query(req.tools, req.query)

        with _lock:
            for ev in pipeline.answer_stream(plan.rewritten, style,
                                             skip_rerank=live):
                if ev.get("type") != "done":
                    yield sse(ev)
                    continue
                res = ev["result"]
                _log_gap(req.query, res, req.collection)
                if req.mode != "kb" and should_fallback(res):
                    metrics.bump("chat_fallback")
                    # 模型可能在说「资料不足」前已经吐过字，先清空再换成通用回答
                    yield sse({"type": "reset",
                               "note": "资料里没有依据，改用通用知识回答"})
                    yield sse({"type": "status", "stage": "general"})
                    tools = []
                    if tools_future is not None or tool_will_run(slow_tools, req.query):
                        yield sse({"type": "status", "stage": "tool",
                                   "label": "实时数据"})
                        tools = _tool_results(tools_future)
                        # 搜索 / 抓网页不抢跑：真降级了才打，不白花外部额度
                        if slow_tools:
                            tools += run_tools(slow_tools, req.query, req.study)
                        yield sse({"type": "status", "stage": "general"})
                    parts = []
                    try:
                        for piece in general_stream(
                                _state.get("provider_impl"), plan.rewritten,
                                history, style, tool_context(tools)):
                            if not piece:
                                continue
                            parts.append(piece)
                            yield sse({"type": "delta", "text": piece})
                    except ProviderError as e:
                        metrics.bump("chat_refused")
                        yield sse({"type": "error", "message": "生成服务暂时不可用"})
                        yield sse({"type": "done", "data": with_cache_meta(
                            _general_error_payload(res, e, t0, plan,
                                                   req.collection))})
                        return
                    metrics.bump("chat_answered")
                    payload = with_cache_meta(_general_payload(
                        res, "".join(parts), t0, plan, req.collection, tools))
                    yield sse({"type": "done", "data": payload})
                    return
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


@app.post("/agent/stream")
def agent_stream(req: AgentRequest, request: Request,
                 _: None = Depends(enforce_guard)):
    """Agent 流式端点：规划 → search_kb → 闸门 → 生成 → 逐字校验。

    先按 SSE 推 status / tool / delta，校验失败会发 reset 并换查询重试一次；
    最终只认 done 里的 payload。现有 /chat、/chat/stream 完全不动。
    """
    _guard_chat_collection(req.collection, request)
    pipeline = _pipeline_for(req.collection)
    runtime = _state.get("agent_rt")

    def events():
        t0 = time.time()
        if pipeline is None:
            data = (_empty_collection_payload(t0)
                    if not _is_preset(req.collection)
                    else _agent_unavailable_payload(t0))
            yield sse({"type": "done", "data": data})
            return
        if runtime is None:
            yield sse({"type": "done", "data": _agent_unavailable_payload(t0)})
            return

        deps = _agent_deps(pipeline, req.tools, req.collection)
        owner = _agent_owner(request)
        metrics.bump("agent_total")
        if not _agent_slots.acquire(timeout=2):
            yield sse({"type": "error",
                       "message": "服务器正忙，请稍等几秒再试"})
            yield sse({"type": "done", "data": _agent_unavailable_payload(t0)})
            return
        try:
            for ev in runtime.stream(
                    deps, query=req.query, thread_id=req.thread_id,
                    owner=owner, history=_history(req),
                    style=_clean_style(req.style),
                    collection=req.collection,
                    tools=req.tools, study=req.study):
                if ev.get("type") != "final":
                    yield sse(ev)
                    continue
                result = ev["result"]
                metrics.bump("agent_refused" if result.refused
                             else "agent_answered")
                if result.attempts > 1:
                    metrics.bump("agent_retry")
                if result.tool_calls:
                    metrics.bump("agent_tool_calls", len(result.tool_calls))
                if result.tools:
                    metrics.bump("agent_tool_calls", len(result.tools))
                _log_gap(req.query, AnswerResult(
                    result.refused, reason=result.reason, hits=result.hits),
                    req.collection)
                yield sse({"type": "done", "data": with_cache_meta(
                    _agent_payload(result, t0, req.query, req.collection))})
        finally:
            _agent_slots.release()

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
    """质量看板：进程内问答计数 + 缺口统计 + 知识库规模。

    process 里的计数只代表本进程启动以来，重启归零；跨重启的历史看 gaps。
    """
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 100))
    counters = metrics.snapshot()
    asks = counters["ask_total"] + counters["chat_total"]
    lib = _state.get("library")
    docs = 0
    try:
        if lib is not None:
            docs = sum(int(c.get("documents") or 0)
                       for c in lib.list_collections())
    except Exception:
        docs = 0
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
            "collections": _collection_count(),
            "documents": docs,
        },
        "gaps": _gap_stats_data(days, limit),
        "feedback": _feedback_stats_data(days, limit),
    }


@app.get("/stats/requests")
def stats_requests(days: int = 30, limit: int = 20,
                   _: None = Depends(enforce_content)) -> dict:
    """请求日志：按路径、按天聚合；没有数据库时返回空壳而不是报错。"""
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 100))
    store = _state.get("store")
    if store is None:
        return {"storage": "memory", "window_days": days, "total": 0,
                "by_path": [], "by_day": [], "recent": [],
                "note": "没有 DATABASE_URL，请求日志只存在进程计数里"}
    try:
        return store.request_stats(days, limit)
    except Exception as e:
        return {"storage": "postgres", "window_days": days, "total": 0,
                "by_path": [], "by_day": [], "recent": [],
                "error": "读取失败：%s" % e}


# ---------- 知识库：用户自建资料库 ----------

def _weknora():
    return _state.get("weknora")


def _wk_error(e: WeKnoraError) -> HTTPException:
    status = 404 if e.status == 404 else (409 if e.status == 409 else 502)
    return HTTPException(status_code=status, detail=str(e))


def _library() -> Library:
    lib = _state.get("library")
    if lib is None:
        raise HTTPException(status_code=503, detail="资料库未就绪")
    return lib


def _lib_error(e: LibraryError) -> HTTPException:
    msg = str(e)
    if "不存在" in msg:
        status = 404
    elif "同名" in msg or "已经在库里" in msg or "已被别处改过" in msg:
        status = 409
    else:
        status = 400
    return HTTPException(status_code=status, detail=msg)


# ---------- 知识库权限闸门（B-lite） ----------

def _optional_user(request: Request) -> Optional[Dict]:
    return _accounts().authenticate(_bearer(request))


def _require_user(request: Request) -> Dict:
    user = _optional_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录账号")
    return user


def _collection_view(cid: str) -> Dict:
    """统一的库对象：内置 / 本地库 / WeKnora 库都能拿到权限字段。"""
    wk = _weknora()
    if wk is not None:
        try:
            coll = wk.get_collection(cid)
        except WeKnoraError as e:
            raise _wk_error(e)
        if coll is None:
            raise HTTPException(status_code=404, detail="资料库不存在")
        # WeKnora 没有归属概念：一律当管理员维护的公司库，普通成员只读不碰。
        return dict(coll, weknora=True, visibility=VISIBILITY_SHARED,
                    owner_id="", builtin=False)
    coll = _library().get_collection(cid)
    if coll is None:
        raise HTTPException(status_code=404, detail="资料库不存在")
    coll["builtin"] = _is_preset(cid)
    return coll


def _member_role(coll: Dict, user: Optional[Dict]) -> str:
    if not user or coll.get("builtin") or coll.get("weknora"):
        return ""
    return _library().collection_member_role(coll.get("id") or "",
                                             user.get("id") or "")


def _guard_collection(cid: str, request: Request, write: bool = False,
                      manage: bool = False):
    """没权限时：未登录回 401，登录了回 404（不让人拿 id 探库是否存在）。

    write 管资料与片段；manage 管删库与成员，比 write 更严。
    """
    user = _optional_user(request)
    coll = _collection_view(cid)
    if coll.get("builtin"):
        if write or manage:
            raise HTTPException(status_code=403, detail="内置资料库不能修改")
        return user, coll, ""
    role = _member_role(coll, user)
    if manage:
        allowed = can_manage(user, coll)
    elif write:
        allowed = can_write(user, coll, role)
    else:
        allowed = can_read(user, coll, role)
    if not allowed:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录账号")
        raise HTTPException(status_code=404, detail="资料库不存在")
    return user, coll, role


def _guard_chat_collection(collection: str, request: Request) -> None:
    """问答也要过同一道闸门：拿到 id 不等于能读这个库。"""
    if _is_preset(collection):
        return
    _guard_collection(collection, request, write=False)


def _require_shared(coll: Dict) -> None:
    if coll.get("weknora"):
        raise HTTPException(status_code=400,
                            detail="当前知识库后端不支持成员管理")
    if coll.get("visibility") != VISIBILITY_SHARED:
        raise HTTPException(status_code=400, detail="个人知识库没有成员")


def _members_payload(cid: str, coll: Dict) -> Dict:
    rows = _library().list_collection_members(cid)
    items = []
    for row in rows:
        uid = str(row.get("user_id") or "")
        account = _accounts().get_user(uid) or {}
        items.append({
            "user_id": uid,
            "username": row.get("username") or account.get("username") or uid,
            "role": row.get("role") or "viewer",
        })
    owner_id = str(coll.get("owner_id") or "")
    owner = _accounts().get_user(owner_id) or {}
    return {
        "owner": {
            "user_id": owner_id,
            "username": owner.get("username") or "",
            "role": "owner",
        } if owner_id else None,
        "items": items,
    }


@app.get("/collections")
def collections_list(request: Request,
                     _: None = Depends(enforce_content)) -> dict:
    """当前账号能看到的资料库。

    默认知识库是内置只读库，未登录也能看到、能提问、能看引用原文；
    公司库和个人库仍然需要登录。没有登录时只返回默认库，前端据此在
    知识库页提示「登录后可看公司库和个人库」。
    """
    user = _optional_user(request)
    wk = _weknora()
    if wk is not None:
        try:
            raw = wk.list_collections() if is_admin(user) else []
        except WeKnoraError as e:
            raise _wk_error(e)
        items = [describe(user, dict(c, weknora=True, builtin=False,
                                     visibility=VISIBILITY_SHARED,
                                     owner_id=""), "")
                 for c in raw]
        if user is None:
            default = _default_collection_item(user)
            if default is not None:
                items.insert(0, default)
        return {"items": items,
                "storage": "weknora", "backend": "weknora",
                "need_login": user is None,
                "can_create_shared": is_admin(user)}
    lib = _library()
    items = []
    for coll in lib.list_collections():
        coll["builtin"] = _is_preset(coll.get("id") or "")
        if not user:
            # 未登录只放出内置默认库（只读）；公司库和个人库仍要登录。
            if not coll["builtin"]:
                continue
            items.append(describe(user, coll, ""))
            continue
        if coll.get("visibility") == VISIBILITY_PRIVATE \
                and coll.get("owner_id") != user["id"]:
            continue
        role = _member_role(coll, user)
        if not can_read(user, coll, role):
            continue
        items.append(describe(user, coll, role))
    return {"items": items,
            "storage": _state.get("storage", "memory"), "backend": "local",
            "need_login": user is None,
            "can_create_shared": is_admin(user)}


@app.post("/collections")
def collection_create(req: CollectionRequest, request: Request,
                      _: None = Depends(enforce_content)) -> dict:
    user = _require_user(request)
    if req.visibility == VISIBILITY_SHARED and not is_admin(user):
        raise HTTPException(status_code=403,
                            detail="只有管理员能建公司知识库")
    wk = _weknora()
    if wk is not None:
        if not is_admin(user):
            raise HTTPException(status_code=403,
                                detail="当前知识库后端只有管理员能操作")
        if req.visibility == VISIBILITY_PRIVATE:
            raise HTTPException(status_code=400,
                                detail="当前知识库后端不支持个人知识库")
        try:
            return wk.create_collection(req.name, req.description)
        except WeKnoraError as e:
            raise _wk_error(e)
    try:
        coll = _library().create_collection(
            req.name, req.description, owner_id=user["id"],
            visibility=req.visibility, created_by=user["id"])
    except LibraryError as e:
        raise _lib_error(e)
    return describe(user, coll, "")


@app.get("/collections/{cid}")
def collection_detail(cid: str, request: Request,
                      _: None = Depends(enforce_content)) -> dict:
    user, coll, role = _guard_collection(cid, request, write=False)
    wk = _weknora()
    if wk is not None:
        try:
            docs = wk.list_documents(cid)
        except WeKnoraError as e:
            raise _wk_error(e)
        out = describe(user, coll, role)
        out["documents"] = docs
        out["chunks"] = sum(int(d.get("chunks") or 0) for d in docs)
        return out
    docs = coll.get("documents") or []
    out = describe(user, coll, role)
    out["documents"] = docs
    out["chunks"] = sum(int(d.get("chunks") or 0) for d in docs)
    return out


@app.delete("/collections/{cid}")
def collection_delete(cid: str, request: Request,
                      _: None = Depends(enforce_content)) -> dict:
    _guard_collection(cid, request, manage=True)
    wk = _weknora()
    if wk is not None:
        try:
            ok = wk.delete_collection(cid)
        except WeKnoraError as e:
            raise _wk_error(e)
        if not ok:
            raise HTTPException(status_code=404, detail="资料库不存在")
        _state["doc_pipelines"].pop(cid, None)
        return {"ok": True, "id": cid}
    lib = _library()
    try:
        ok = lib.delete_collection(cid)
    except LibraryError as e:
        raise _lib_error(e)
    if not ok:
        raise HTTPException(status_code=404, detail="资料库不存在")
    _state["doc_pipelines"].pop(cid, None)
    return {"ok": True, "id": cid}


@app.get("/collections/{cid}/members")
def collection_members(cid: str, request: Request,
                       _: None = Depends(enforce_content)) -> dict:
    """公司库成员：owner + 被授权的 editor / viewer。个人库没有成员概念。"""
    _, coll, _role = _guard_collection(cid, request, manage=True)
    _require_shared(coll)
    return _members_payload(cid, coll)


@app.post("/collections/{cid}/members")
def collection_member_add(cid: str, req: CollectionMemberRequest,
                          request: Request,
                          _: None = Depends(enforce_content)) -> dict:
    user, coll, _role = _guard_collection(cid, request, manage=True)
    _require_shared(coll)
    target = _accounts().find_user(req.username)
    if target is None:
        raise HTTPException(status_code=404, detail="没有这个用户名")
    if target["id"] == coll.get("owner_id"):
        raise HTTPException(status_code=400, detail="建库人已经是 owner")
    if target["id"] == user.get("id"):
        raise HTTPException(status_code=400, detail="不用把自己加进成员")
    if req.role not in ASSIGNABLE_ROLES:
        raise HTTPException(status_code=400, detail="成员角色不对")
    try:
        _library().set_collection_member(cid, target["id"], req.role)
    except LibraryError as e:
        raise _lib_error(e)
    return _members_payload(cid, coll)


@app.delete("/collections/{cid}/members/{user_id}")
def collection_member_remove(cid: str, user_id: str, request: Request,
                             _: None = Depends(enforce_content)) -> dict:
    _, coll, _role = _guard_collection(cid, request, manage=True)
    _require_shared(coll)
    if user_id == coll.get("owner_id"):
        raise HTTPException(status_code=400, detail="不能移除建库人")
    removed = _library().remove_collection_member(cid, user_id)
    return {"ok": removed, "removed": 1 if removed else 0}


@app.get("/collections/{cid}/search")
def collection_search(cid: str, request: Request, q: str = "", k: int = 8,
                      _: None = Depends(enforce_content)) -> dict:
    """知识库内检索测试：只读，返回片段、分数与出处，不生成答案。"""
    query = (q or "").strip()
    if not query:
        raise HTTPException(status_code=422, detail="检索词不能为空")
    _guard_collection(cid, request, write=False)
    pipeline = _pipeline_for(cid)
    if pipeline is None:
        return {"items": [], "total": 0, "query": query}
    try:
        hits = pipeline.searcher.search(query)
    except Exception as e:
        raise HTTPException(status_code=502,
                            detail="检索失败：%s" % type(e).__name__)
    limit = max(1, min(int(k or 8), 20))
    items = []
    for hit in hits[:limit]:
        chunk = hit.chunk
        items.append({
            "chunk_id": chunk.id,
            "title": chunk.title or (chunk.source_ref or {}).get(
                "document_title") or "",
            "text": chunk.text,
            "score": round(float(hit.score), 4),
            "bm25": round(float(getattr(hit, "bm25", 0.0) or 0.0), 4),
            "vector": round(float(getattr(hit, "vector", 0.0) or 0.0), 4),
            "source_ref": chunk.source_ref or {},
        })
    return {"items": items, "total": len(items), "query": query}


@app.post("/collections/{cid}/documents")
def document_add(cid: str, req: DocumentRequest, request: Request,
                 _: None = Depends(enforce_guard)) -> dict:
    """把一段资料加进知识库：切分、算向量、入库，立即可问答。

    出题接口不落盘；这里是反向的——用户明确要把资料存成可检索的库。
    """
    _guard_collection(cid, request, write=True)
    wk = _weknora()
    if wk is not None:
        try:
            doc = wk.add_text(cid, req.title, req.text)
        except WeKnoraError as e:
            raise _wk_error(e)
        _state["doc_pipelines"].pop(cid, None)
        return doc
    lib = _library()
    try:
        doc = lib.add_document(cid, req.text, req.title)
    except LibraryError as e:
        raise _lib_error(e)
    _state["doc_pipelines"].pop(cid, None)
    return doc


@app.get("/collections/{cid}/documents/{doc_id}")
def document_detail(cid: str, doc_id: str, request: Request,
                    _: None = Depends(enforce_content)) -> dict:
    _guard_collection(cid, request, write=False)
    wk = _weknora()
    if wk is not None:
        try:
            text = wk.document_preview(doc_id)
        except WeKnoraError as e:
            raise _wk_error(e)
        return {"id": doc_id, "title": "", "text": text, "created_at": "",
                "backend": "weknora"}
    lib = _library()
    if lib.get_collection(cid) is None:
        raise HTTPException(status_code=404, detail="资料库不存在")
    row = lib.get_document(cid, doc_id)
    if row is None:
        raise HTTPException(status_code=404, detail="资料不存在")
    return {"id": row["id"], "title": row["title"], "text": row["text"],
            "created_at": str(row.get("created_at") or "")}


@app.patch("/collections/{cid}/documents/{doc_id}")
def document_edit(cid: str, doc_id: str, req: DocumentEditRequest,
                  request: Request,
                  _: None = Depends(enforce_content)) -> dict:
    """整篇改标题 / 正文：保存前先留旧版，保存后重新切分与建索引。"""
    user, coll, _role = _guard_collection(cid, request, write=True)
    if coll.get("weknora"):
        raise HTTPException(status_code=400,
                            detail="这个知识库由 WeKnora 托管，暂不支持在线编辑")
    author = str((user or {}).get("username") or "")
    try:
        out = _library().update_document(cid, doc_id, req.text,
                                         title=req.title, author=author)
    except LibraryError as e:
        raise _lib_error(e)
    _state["doc_pipelines"].pop(cid, None)
    out["author"] = author
    return out


@app.get("/collections/{cid}/documents/{doc_id}/versions")
def document_versions(cid: str, doc_id: str, request: Request,
                      limit: int = 20,
                      _: None = Depends(enforce_content)) -> dict:
    _guard_collection(cid, request, write=False)
    lib = _library()
    if lib.get_document(cid, doc_id) is None:
        raise HTTPException(status_code=404, detail="资料不存在")
    items = lib.list_document_versions(cid, doc_id, limit=limit)
    return {"items": items, "total": len(items)}


@app.get("/collections/{cid}/documents/{doc_id}/versions/{version_id}")
def document_version_detail(cid: str, doc_id: str, version_id: int,
                            request: Request,
                            _: None = Depends(enforce_content)) -> dict:
    _guard_collection(cid, request, write=False)
    lib = _library()
    if lib.get_document(cid, doc_id) is None:
        raise HTTPException(status_code=404, detail="资料不存在")
    row = lib.get_document_version(cid, doc_id, version_id)
    if row is None:
        raise HTTPException(status_code=404, detail="历史版本不存在")
    return {"id": int(row["id"]), "title": row.get("title") or "",
            "text": row.get("text") or "", "author": row.get("author") or "",
            "created_at": str(row.get("created_at") or "")}


@app.post("/collections/{cid}/documents/{doc_id}/versions/{version_id}/restore")
def document_version_restore(cid: str, doc_id: str, version_id: int,
                             request: Request,
                             _: None = Depends(enforce_content)) -> dict:
    user, coll, _role = _guard_collection(cid, request, write=True)
    if coll.get("weknora"):
        raise HTTPException(status_code=400,
                            detail="这个知识库由 WeKnora 托管，暂不支持在线编辑")
    author = str((user or {}).get("username") or "")
    try:
        out = _library().restore_document_version(cid, doc_id, version_id,
                                                  author=author)
    except LibraryError as e:
        raise _lib_error(e)
    _state["doc_pipelines"].pop(cid, None)
    out["author"] = author
    return out


# 能直接内嵌到预览里的图片类型；SVG 能带脚本，一律按附件下载。
ASSET_INLINE_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp",
                     "image/bmp", "image/avif"}
MAX_ASSET = 8 * 1024 * 1024


def _asset_mime(request: Request) -> str:
    raw = (request.headers.get("content-type") or "").split(";")[0].strip()
    return raw.lower()[:80] or "application/octet-stream"


def _asset_payload(row: Dict) -> dict:
    return {"id": row["id"], "name": row["name"], "mime": row["mime"],
            "bytes": int(row.get("bytes") or 0),
            "inline": row["mime"] in ASSET_INLINE_MIME,
            "created_at": str(row.get("created_at") or "")}


@app.get("/collections/{cid}/documents/{doc_id}/assets")
def document_assets(cid: str, doc_id: str, request: Request,
                    _: None = Depends(enforce_content)) -> dict:
    _guard_collection(cid, request, write=False)
    lib = _library()
    if lib.get_document(cid, doc_id) is None:
        raise HTTPException(status_code=404, detail="资料不存在")
    items = [_asset_payload(a) for a in lib.list_document_assets(cid, doc_id)]
    return {"items": items, "total": len(items),
            "max_bytes": MAX_ASSET, "max_items": 20}


@app.post("/collections/{cid}/documents/{doc_id}/assets")
async def document_asset_add(cid: str, doc_id: str, request: Request,
                             _: None = Depends(enforce_content)) -> dict:
    """原始字节放请求体，文件名走 X-Filename（URI 编码），不依赖 multipart。"""
    user, coll, _role = _guard_collection(cid, request, write=True)
    if coll.get("weknora"):
        raise HTTPException(status_code=400,
                            detail="这个知识库由 WeKnora 托管，暂不支持在线附件")
    length = request.headers.get("content-length") or ""
    if length.isdigit() and int(length) > MAX_ASSET:
        raise HTTPException(status_code=413,
                            detail="单个附件最多 %d MB" % (MAX_ASSET // 1048576))
    body = await request.body()
    if len(body) > MAX_ASSET:
        raise HTTPException(status_code=413,
                            detail="单个附件最多 %d MB" % (MAX_ASSET // 1048576))
    if not body:
        raise HTTPException(status_code=400, detail="附件是空的")
    try:
        row = _library().add_document_asset(
            cid, doc_id, _upload_name(request.headers.get("X-Filename", "")),
            _asset_mime(request), body)
    except LibraryError as e:
        raise _lib_error(e)
    return _asset_payload(row)


@app.get("/collections/{cid}/documents/{doc_id}/assets/{asset_id}")
def document_asset_get(cid: str, doc_id: str, asset_id: str, request: Request,
                       _: None = Depends(enforce_content)) -> Response:
    _guard_collection(cid, request, write=False)
    row = _library().get_document_asset(cid, doc_id, asset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="附件不存在")
    inline = row["mime"] in ASSET_INLINE_MIME
    name = urllib.parse.quote(row["name"] or "附件")
    return Response(
        content=row.get("data") or b"",
        media_type=row["mime"] or "application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": ("inline" if inline else "attachment")
            + "; filename*=UTF-8''" + name,
        })


@app.delete("/collections/{cid}/documents/{doc_id}/assets/{asset_id}")
def document_asset_delete(cid: str, doc_id: str, asset_id: str,
                          request: Request,
                          _: None = Depends(enforce_content)) -> dict:
    _guard_collection(cid, request, write=True)
    ok = _library().delete_document_asset(cid, doc_id, asset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="附件不存在")
    return {"ok": True, "id": asset_id}


@app.delete("/collections/{cid}/documents/{doc_id}")
def document_delete(cid: str, doc_id: str, request: Request,
                    _: None = Depends(enforce_content)) -> dict:
    _guard_collection(cid, request, write=True)
    wk = _weknora()
    if wk is not None:
        try:
            ok = wk.delete_document(doc_id)
        except WeKnoraError as e:
            raise _wk_error(e)
        if not ok:
            raise HTTPException(status_code=404, detail="资料不存在")
        _state["doc_pipelines"].pop(cid, None)
        return {"ok": True, "id": doc_id}
    ok = _library().delete_document(cid, doc_id)
    if not ok:
        raise HTTPException(status_code=404, detail="资料不存在")
    _state["doc_pipelines"].pop(cid, None)
    return {"ok": True, "id": doc_id}


MAX_UPLOAD = 20 * 1024 * 1024


def _upload_name(raw: str) -> str:
    name = urllib.parse.unquote(raw or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch not in '\r\n\t"').strip()
    return (name or "upload.bin")[:120]


def _start_parse_job(cid: str, doc_id: str, name: str, data: bytes) -> None:
    """后台解析一份上传的文件：解析 → 切分 → 入库，失败就把原因写回文档。"""

    def work() -> None:
        lib = _state.get("library")
        if lib is None:
            return
        try:
            text, _, tables = extract_document(name, data)
        except ParseError as e:
            lib.fail_document(cid, doc_id, str(e))
            return
        except Exception as e:  # 解析器自己炸了，别把后台线程带崩
            lib.fail_document(cid, doc_id, "解析失败：%s" % e)
            return
        try:
            lib.finish_document(cid, doc_id, text, tables=tables)
        except Exception as e:
            lib.fail_document(cid, doc_id, "入库失败：%s" % e)

    # 单测里同步跑，省掉等线程的 sleep
    if _state.get("sync_parse"):
        work()
    else:
        threading.Thread(target=work, daemon=True).start()


@app.post("/collections/{cid}/url")
def collection_add_url(cid: str, req: UrlDocumentRequest, request: Request,
                       _: None = Depends(enforce_guard)) -> dict:
    """从公开网页导入一篇资料：只取正文，原文与片段照常入库。"""
    user, coll, role = _guard_collection(cid, request, write=True)
    url = req.url.strip()
    if not _url_allowed(url):
        raise HTTPException(status_code=400,
                            detail="只允许导入公网 http/https 网页")
    try:
        ureq = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; yanju/1.0)"})
        with urllib.request.urlopen(ureq, timeout=15) as resp:
            data = resp.read(5 * 1024 * 1024 + 1)
            ctype = (resp.headers.get("Content-Type") or "").lower()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HTTPException(status_code=502,
                            detail="网页抓取失败：%s" % type(e).__name__)
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="网页超过 5MB，先另存后再上传")
    path = urllib.parse.urlsplit(url).path.lower()
    if "pdf" in ctype or path.endswith(".pdf"):
        name = "page.pdf"
    elif "html" in ctype or path.endswith((".html", ".htm")):
        name = "page.html"
    else:
        name = "page.txt"
    try:
        text, _meta, _tables = extract_document(name, data)
    except ParseError as e:
        raise HTTPException(status_code=422, detail=str(e))
    title = req.title.strip()
    if not title:
        host = urllib.parse.urlsplit(url).hostname or "网页"
        title = host[:80]
    try:
        out = _library().add_document(
            cid, text, title=title, source=url, nbytes=len(data))
    except LibraryError as e:
        raise _lib_error(e)
    _state["doc_pipelines"].pop(cid, None)
    return out


@app.post("/collections/{cid}/files")
async def document_upload(cid: str, request: Request,
                          _: None = Depends(enforce_guard)) -> dict:
    """上传原始文件，本地解析成正文后切分入库。

    前端直接把文件放进请求体，文件名走 X-Filename（URI 编码），
    不依赖 python-multipart，也不把文件内容读进模型上下文。
    配了 WeKnora 时仍然交给它解析（可选后端）。
    """
    _guard_collection(cid, request, write=True)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="空文件")
    if len(body) > MAX_UPLOAD:
        raise HTTPException(status_code=413,
                            detail="文件太大，单篇最多 %d MB" % (MAX_UPLOAD // 1024 // 1024))
    name = _upload_name(request.headers.get("X-Filename", ""))
    wk = _weknora()
    if wk is not None:
        try:
            doc = wk.upload_document(cid, name, body)
        except WeKnoraError as e:
            raise _wk_error(e)
        _state["doc_pipelines"].pop(cid, None)
        return doc

    ext = extension_of(name)
    lib = _library()
    try:
        doc = lib.create_pending_document(
            cid, Path(name).stem or name, source=ext, nbytes=len(body))
    except LibraryError as e:
        raise _lib_error(e)
    _state["doc_pipelines"].pop(cid, None)
    _start_parse_job(cid, doc["id"], name, body)
    return doc


@app.get("/collections/{cid}/documents/{doc_id}/chunks")
def document_chunks(cid: str, doc_id: str, request: Request,
                    _: None = Depends(enforce_content)) -> dict:
    """一篇资料切出来的片段；片段是引用校验的最小单位。"""
    user, coll, role = _guard_collection(cid, request, write=False)
    editable = can_write(user, coll, role)
    wk = _weknora()
    if wk is not None:
        try:
            items = wk.list_chunks(doc_id)
        except WeKnoraError as e:
            raise _wk_error(e)
        return {"items": items, "total": len(items), "editable": editable}
    items = _library().document_chunks(cid, doc_id)
    return {"items": items, "total": len(items), "editable": editable}


@app.put("/collections/{cid}/documents/{doc_id}/chunks/{chunk_id}")
def chunk_update(cid: str, doc_id: str, chunk_id: str,
                 req: ChunkUpdateRequest, request: Request,
                 _: None = Depends(enforce_guard)) -> dict:
    _guard_collection(cid, request, write=True)
    wk = _weknora()
    if wk is not None:
        try:
            chunk = wk.update_chunk(doc_id, chunk_id, req.content, req.revision)
        except WeKnoraError as e:
            raise _wk_error(e)
        _state["doc_pipelines"].pop(cid, None)
        return chunk
    try:
        chunk = _library().update_chunk(cid, doc_id, chunk_id,
                                        req.content, req.revision)
    except LibraryError as e:
        raise _lib_error(e)
    _state["doc_pipelines"].pop(cid, None)
    return chunk


@app.delete("/collections/{cid}/documents/{doc_id}/chunks/{chunk_id}")
def chunk_delete(cid: str, doc_id: str, chunk_id: str, request: Request,
                 _: None = Depends(enforce_content)) -> dict:
    _guard_collection(cid, request, write=True)
    wk = _weknora()
    if wk is not None:
        try:
            ok = wk.delete_chunk(doc_id, chunk_id)
        except WeKnoraError as e:
            raise _wk_error(e)
    else:
        ok = _library().delete_chunk(cid, doc_id, chunk_id)
    if not ok:
        raise HTTPException(status_code=404, detail="片段不存在")
    _state["doc_pipelines"].pop(cid, None)
    return {"ok": True, "id": chunk_id}


@app.post("/collections/{cid}/documents/{doc_id}/reparse")
def document_reparse(cid: str, doc_id: str, request: Request,
                     _: None = Depends(enforce_guard)) -> dict:
    _guard_collection(cid, request, write=True)
    wk = _weknora()
    if wk is not None:
        try:
            doc = wk.reparse_document(doc_id)
        except WeKnoraError as e:
            raise _wk_error(e)
        _state["doc_pipelines"].pop(cid, None)
        return doc
    lib = _library()
    try:
        doc = lib.reparse_document(cid, doc_id)
    except LibraryError as e:
        raise _lib_error(e)
    _state["doc_pipelines"].pop(cid, None)
    row = lib.get_document(cid, doc_id) or {}
    return {"id": doc_id,
            "status": "completed" if doc else "failed",
            "chunks": (doc or {}).get("chunks", 0),
            "error": row.get("error") or ""}


@app.post("/create/outline")
def create_outline(req: CreateOutlineRequest,
                   _: None = Depends(enforce_guard)) -> dict:
    """按主题整理成文档 / 演示大纲；没有真实模型时返回明确标注的演示模板。"""
    provider = _state.get("provider_impl")
    demo = _state.get("provider") != "deepseek" or provider is None
    try:
        out = docgen.generate_outline(
            provider, req.topic, req.kind, req.points, req.notes, demo=demo)
    except ProviderError as e:
        raise HTTPException(status_code=502, detail="生成大纲失败：%s" % e)
    except docgen.CreateError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return out


@app.post("/create/export")
def create_export(req: CreateExportRequest,
                  _: None = Depends(enforce_guard)) -> Response:
    """把已确认的大纲导出成 Markdown / Word / PPT，不落盘。"""
    try:
        outline = docgen.normalize_outline(req.outline)
        if req.format == "md":
            body = docgen.render_markdown(outline).encode("utf-8")
            media = "text/markdown; charset=utf-8"
        elif req.format == "docx":
            body = docgen.render_docx(outline)
            media = ("application/vnd.openxmlformats-officedocument."
                     "wordprocessingml.document")
        else:
            body = docgen.render_pptx(outline, style=req.style)
            media = ("application/vnd.openxmlformats-officedocument."
                     "presentationml.presentation")
    except docgen.CreateError as e:
        raise HTTPException(status_code=422, detail=str(e))
    name = docgen.safe_filename(outline["title"], req.format)
    return Response(
        content=body, media_type=media,
        headers={
            "Content-Disposition": "attachment; filename=\"create.%s\"; "
            "filename*=UTF-8''%s" %
            (req.format, urllib.parse.quote(name)),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        })


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")
