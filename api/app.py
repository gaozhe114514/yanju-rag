# -*- coding: utf-8 -*-
"""HTTP 服务：把检索 + 拒答 + 生成 + 引用校验包成 /ask。

启动（仓库根目录）：
  py -m uvicorn api.app:app --host 0.0.0.0 --port 8000

环境变量：
  KB_INPUT          题库 JSONL，逗号分隔，默认 data/work/2019_draft.jsonl
  KB_MODE           bm25 / hybrid / hybrid-rerank / hybrid-ce，默认 hybrid
  KB_FUSE_K         进入精排的候选数，默认 10
  KB_PROVIDER       mock / deepseek；默认有 DEEPSEEK_API_KEY 就用 deepseek
  KB_MIN_SCORE      拒答阈值；hybrid-ce 默认 0.05，其他模式默认 0（RRF 分数没法用绝对阈值）
  KB_MIN_MARGIN     top1/top2 最小分差；hybrid-ce 默认 0.05，其他模式默认 0
"""
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from answer.gate import RefusalGate
from answer.pipeline import AnswerPipeline
from answer.providers import DeepSeekProvider, ExtractiveProvider, ProviderError
from retrieval.chunk import load_chunks
from retrieval.search import build_searcher

from .gap import log_gap

ROOT = Path(__file__).resolve().parents[1]

_state = {}
_lock = threading.Lock()


class AskRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)


def _load() -> None:
    raw = os.environ.get("KB_INPUT", "data/work/2019_draft.jsonl")
    paths = [p.strip() for p in raw.split(",") if p.strip()]
    paths = [str((ROOT / p) if not os.path.isabs(p) else Path(p)) for p in paths]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise RuntimeError("题库文件不存在：%s" % ", ".join(missing))

    chunks = load_chunks(paths)
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

    # 阈值来自 answer.eval_gate 在 32 条正样本 + 16 条负样本上的标定：
    # 0.05/0.05 时负样本拒答 93.8%，正样本误拒 12.5%。
    default_score = "0.05" if mode == "hybrid-ce" else "0"
    default_margin = "0.05" if mode == "hybrid-ce" else "0"
    gate = RefusalGate(float(os.environ.get("KB_MIN_SCORE", default_score)),
                       float(os.environ.get("KB_MIN_MARGIN", default_margin)))
    gap_path = Path(os.environ.get("KB_GAP_LOG", "data/work/gap_log.jsonl"))
    if not gap_path.is_absolute():
        gap_path = ROOT / gap_path
    gap_path.parent.mkdir(parents=True, exist_ok=True)
    _state.update({
        "pipeline": AnswerPipeline(searcher, provider, gate=gate),
        "chunks": chunks,
        "mode": mode,
        "fuse_k": fuse_k,
        "provider": provider_name,
        "gap_log": str(gap_path),
    })


@asynccontextmanager
async def lifespan(_: FastAPI):
    _load()
    yield


app = FastAPI(title="408 考研知识库", version="0.2", lifespan=lifespan)


def _hit_payload(h) -> dict:
    c = h.chunk
    return {
        "chunk_id": c.id,
        "score": round(float(h.score), 4),
        "subject": c.subject,
        "stem": c.text.split("\n")[0][:120],
        "answer": c.answer,
        "source_ref": c.source_ref,
    }


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "chunks": len(_state.get("chunks", [])),
        "mode": _state.get("mode"),
        "fuse_k": _state.get("fuse_k"),
        "provider": _state.get("provider"),
    }


@app.post("/ask")
def ask(req: AskRequest) -> dict:
    pipeline = _state.get("pipeline")
    if pipeline is None:
        return {"refused": True, "answer": "", "citations": [],
                "reason": "server_not_ready", "hits": [], "latency_ms": 0}
    t0 = time.time()
    # 模型推理是 CPU 密集的，单 worker 内串行，避免线程争抢
    with _lock:
        res = pipeline.answer(req.query)
    if res.refused:
        try:
            log_gap(_state["gap_log"], req.query, res.reason,
                    res.hits[0] if res.hits else None)
        except OSError:
            pass  # 日志写失败不能影响答题

    by_id = {h.chunk.id: h.chunk for h in res.hits}
    citations = []
    for c in res.citations:
        cid = c.get("chunk_id")
        ch = by_id.get(cid)
        citations.append({
            "chunk_id": cid,
            "quote": c.get("quote"),
            "source_ref": ch.source_ref if ch else {},
        })
    return {
        "refused": res.refused,
        "answer": res.answer,
        "citations": citations,
        "reason": res.reason,
        "hits": [_hit_payload(h) for h in res.hits],
        "attempts": res.attempts,
        "latency_ms": int((time.time() - t0) * 1000),
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")
