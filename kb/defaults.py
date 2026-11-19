# -*- coding: utf-8 -*-
"""默认公司知识库：启动时从 data/curated/company/ 灌入示例资料。

资料是普通知识库文档，走和用户上传完全相同的解析、切分、引用校验链路；
已有同名文档不会重复添加，用户改过或删掉的内容也不会被启动时覆盖。
"""
from pathlib import Path
from typing import Dict, List, Optional

from .library import Library, LibraryError

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NAME = "公司知识库"
DEFAULT_DESC = "示例公司知识库；内容仅用于演示引用，可替换为公司真实资料"
DEFAULT_DIR = ROOT / "data" / "curated" / "company"


def load_default_docs(docs_dir: Optional[Path] = None) -> List[Dict[str, str]]:
    """读取默认资料：markdown 第一行标题作为文档标题。"""
    folder = Path(docs_dir or DEFAULT_DIR)
    if not folder.exists():
        return []
    out = []
    for path in sorted(folder.glob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        if len(text) < 20:
            continue
        title = ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#"):
                title = line.lstrip("#").strip()
                break
        out.append({
            "title": title or path.stem,
            "text": text,
            "source": "data/curated/company/%s" % path.name,
        })
    return out


def ensure_default_library(lib: Library, docs_dir: Optional[Path] = None) -> Optional[Dict]:
    """确保「公司知识库」存在并补齐示例资料；返回集合详情。"""
    rows = lib.list_collections()
    coll = next((c for c in rows
                 if c.get("name") == DEFAULT_NAME
                 and (c.get("visibility") or "shared") == "shared"), None)
    if coll is None:
        try:
            lib.create_collection(DEFAULT_NAME, DEFAULT_DESC,
                                  owner_id="", visibility="shared",
                                  created_by="system")
        except LibraryError:
            pass  # 多进程同时启动时可能已被另一个进程创建
        rows = lib.list_collections()
        coll = next((c for c in rows
                     if c.get("name") == DEFAULT_NAME
                     and (c.get("visibility") or "shared") == "shared"), None)
    if coll is None:
        return None

    cid = coll["id"]
    existing = {d.get("title") for d in lib.list_documents(cid)}
    for doc in load_default_docs(docs_dir):
        if doc["title"] in existing:
            continue
        try:
            lib.add_document(cid, doc["text"], title=doc["title"],
                             source=doc["source"],
                             nbytes=len(doc["text"].encode("utf-8")))
            existing.add(doc["title"])
        except LibraryError:
            # 标题不同但正文相同，或单篇超限：跳过，不挡住 API 启动
            continue
    return lib.get_collection(cid)
