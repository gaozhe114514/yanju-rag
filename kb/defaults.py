# -*- coding: utf-8 -*-
"""默认公司知识库：启动时从 data/curated/company/ 灌入示例资料。

资料是普通知识库文档，走和用户上传完全相同的解析、切分、引用校验链路；
已有同名文档不会重复添加，用户改过或删掉的内容也不会被启动时覆盖。
"""
from pathlib import Path
from typing import Dict, List, Optional

from .library import Library, LibraryError

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NAME = "默认知识库"
DEFAULT_DESC = "内置示例资料；无需登录即可试用，只读，可替换为公司真实资料"
DEFAULT_DIR = ROOT / "data" / "curated" / "default"

# 示例资料按用途分组，默认库的 Wiki 树也能看出层级。
DEFAULT_FOLDERS = {
    "00": "开始",
    "01": "产品与使用",
    "02": "产品与使用",
    "03": "产品与使用",
    "04": "协作与会议",
    "05": "常见问题",
}


def default_folder_for(name: str) -> str:
    return DEFAULT_FOLDERS.get(Path(name or "").name[:2], "示例资料")


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
            "source": "data/curated/default/%s" % path.name,
            "folder": default_folder_for(path.name),
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
                                  created_by="")
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
                             folder=doc.get("folder") or "",
                             nbytes=len(doc["text"].encode("utf-8")))
            existing.add(doc["title"])
        except LibraryError:
            # 标题不同但正文相同，或单篇超限：跳过，不挡住 API 启动
            continue
    # 老库里的示例资料没有文件夹：按来源文件名补一次，重复执行无副作用。
    for row in lib.list_documents(cid):
        if row.get("folder"):
            continue
        path = Path(row.get("source") or "").name
        if path[:2] in DEFAULT_FOLDERS:
            try:
                lib.move_document(cid, row["id"], default_folder_for(path))
            except LibraryError:
                pass
    return lib.get_collection(cid)
