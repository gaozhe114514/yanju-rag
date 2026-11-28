# -*- coding: utf-8 -*-
"""自动 Wiki：从知识库资料里抽出概念 / 流程，生成带来源的页面。

和问答的区别：问答是「一问一答 + 逐字引用」，Wiki 是「把资料整理成可浏览的
页面」。页面正文允许模型归纳，但每个页面必须挂到真实片段上，来源片段原样
展示；没有来源的页面直接丢掉，不硬编。
"""
import hashlib
import json
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .library import wiki_slug

MAX_PAGES = 10
MAX_CHUNK_CHARS = 700
MAX_PROMPT_CHUNKS = 36
MAX_SOURCE_CHUNKS = 4


def wiki_id(cid: str, slug: str) -> str:
    return "wiki-" + hashlib.sha1(
        (cid + "|" + slug).encode("utf-8")).hexdigest()[:10]


WIKI_SYSTEM = """你在整理一个公司知识库的 Wiki。只依据用户给出的资料片段，不要编造。
输出严格 JSON，格式：
{"pages":[{"title":"页面标题","aliases":["别名"],"summary":"一句话摘要",
"body":"Markdown 正文","source_chunk_ids":["片段 id"]}]}
要求：
1. 5–10 个页面；页面是一个概念、流程、制度、产品或人物，不要把一篇文档原样抄成页面。
2. 每个页面必须引用 1–4 个真实存在的 source_chunk_ids，不能引用不存在的 id。
3. 正文 200–800 字，用 Markdown 小标题和列表；提到另一个页面时写成 [[页面标题]]。
4. 资料没写的内容不要补；不确定就写「资料未说明」。
5. 只输出 JSON，不要代码块，不要解释。"""


def _chunk_label(cid: str, doc_title: str, text: str) -> str:
    text = " ".join((text or "").split())[:MAX_CHUNK_CHARS]
    return "[chunk=%s] 文档《%s》：%s" % (cid, doc_title, text)


def sample_prompt_chunks(docs: Sequence[Dict], chunks: Sequence[Dict],
                         limit: int = MAX_PROMPT_CHUNKS) -> List[Dict]:
    """每篇文档先取前几段，再按顺序补，避免被一篇长文档占满。"""
    titles = {d["id"]: d.get("title") or "未命名资料" for d in docs}
    by_doc: Dict[str, List[Dict]] = {}
    for c in chunks:
        by_doc.setdefault(c.get("document_id") or "", []).append(c)
    picked = []
    for d in docs:
        for c in by_doc.get(d["id"], [])[:4]:
            picked.append(c)
            if len(picked) >= limit:
                return picked
    for c in chunks:
        if c in picked:
            continue
        picked.append(c)
        if len(picked) >= limit:
            break
    for c in picked:
        c["_doc_title"] = titles.get(c.get("document_id") or "", "未命名资料")
    return picked


def _parse_json(text: str):
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
    try:
        return json.loads(raw)
    except ValueError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except ValueError:
            return None
    return None


def _clean_page(cid: str, item: Dict, chunk_map: Dict[str, Dict],
                doc_of_chunk: Dict[str, str]) -> Optional[Dict]:
    if not isinstance(item, dict):
        return None
    title = " ".join(str(item.get("title") or "").split())[:40]
    body = str(item.get("body") or "").strip()
    if not title or len(body) < 20:
        return None
    sources = []
    for cid_value in item.get("source_chunk_ids") or []:
        key = str(cid_value).strip()
        if key in chunk_map and key not in sources:
            sources.append(key)
    if not sources:
        return None
    # 只保留真实片段；模型编的 id 不算来源
    sources = sources[:MAX_SOURCE_CHUNKS]
    doc_ids = []
    for key in sources:
        did = doc_of_chunk.get(key) or ""
        if did and did not in doc_ids:
            doc_ids.append(did)
    aliases = []
    for a in item.get("aliases") or []:
        a = " ".join(str(a).split())[:20]
        if a and a != title and a not in aliases:
            aliases.append(a)
    summary = " ".join(str(item.get("summary") or "").split())[:160]
    slug = wiki_slug(title)
    return {
        "id": wiki_id(cid, slug), "slug": slug, "title": title,
        "summary": summary or body.split("\n")[0][:120],
        "body": body[:6000], "aliases": aliases[:5],
        "source_doc_ids": doc_ids, "source_chunk_ids": sources,
    }


def generate_with_llm(provider, cid: str, docs: Sequence[Dict],
                      chunks: Sequence[Dict]) -> Tuple[List[Dict], str]:
    picked = sample_prompt_chunks(docs, chunks)
    if not picked:
        return [], "empty"
    lines = [_chunk_label(c["id"], c.get("_doc_title") or "", c.get("text") or "")
             for c in picked]
    doc_lines = ["- 《%s》%s" % (d.get("title") or "未命名资料",
                               (" · 文件夹 " + d["folder"]) if d.get("folder") else "")
                 for d in docs]
    messages = [
        {"role": "system", "content": WIKI_SYSTEM},
        {"role": "user", "content": "资料清单：\n" + "\n".join(doc_lines) +
         "\n\n资料片段：\n" + "\n\n".join(lines)},
    ]
    try:
        raw = provider.complete(messages, temperature=0.2,
                                max_tokens=4000, json_mode=True, retries=1)
    except Exception:
        return [], "llm_error"
    data = _parse_json(raw)
    if not isinstance(data, dict):
        return [], "llm_bad_json"
    items = data.get("pages")
    if not isinstance(items, list):
        return [], "llm_bad_json"
    chunk_map = {c["id"]: c for c in picked}
    doc_of_chunk = {c["id"]: c.get("document_id") or "" for c in picked}
    pages, seen = [], set()
    for item in items:
        page = _clean_page(cid, item, chunk_map, doc_of_chunk)
        if page is None or page["slug"] in seen:
            continue
        seen.add(page["slug"])
        pages.append(page)
        if len(pages) >= MAX_PAGES:
            break
    return pages, "llm" if pages else "llm_empty"


def fallback_pages(cid: str, docs: Sequence[Dict],
                   chunks: Sequence[Dict]) -> List[Dict]:
    """没有可用的生成模型时：每篇资料一页，摘要取正文开头，来源挂真实片段。"""
    by_doc: Dict[str, List[Dict]] = {}
    for c in chunks:
        by_doc.setdefault(c.get("document_id") or "", []).append(c)
    pages, seen = [], set()
    for d in docs:
        title = " ".join(str(d.get("title") or "未命名资料").split())[:40]
        slug = wiki_slug(title)
        if slug in seen:
            continue
        text = str(d.get("text") or "").strip()
        if len(text) < 20:
            continue
        source = [c["id"] for c in by_doc.get(d["id"], [])[:MAX_SOURCE_CHUNKS]]
        if not source:
            continue
        seen.add(slug)
        body = re.sub(r"^#{1,6}\s*[^\n]*\n+", "", text)[:1800]
        plain = re.sub(r"^#{1,6}\s*", "", text)
        pages.append({
            "id": wiki_id(cid, slug), "slug": slug, "title": title,
            "summary": " ".join(plain.split())[:120],
            "body": body, "aliases": [],
            "source_doc_ids": [d["id"]], "source_chunk_ids": source,
        })
        if len(pages) >= MAX_PAGES:
            break
    return pages


def generate_wiki(provider, cid: str, docs: Sequence[Dict],
                  chunks: Sequence[Dict]) -> Tuple[List[Dict], str]:
    """先试模型整理，失败或没有模型时退回按文档建页。"""
    pages, source = [], "fallback"
    if provider is not None and hasattr(provider, "complete"):
        pages, source = generate_with_llm(provider, cid, docs, chunks)
    if not pages:
        pages = fallback_pages(cid, docs, chunks)
        source = "fallback" if source in ("empty", "") else source + "+fallback"
    return pages, source
