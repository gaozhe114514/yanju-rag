# -*- coding: utf-8 -*-
"""用户知识库：资料库 / 文档 / 片段的管理与检索。

方向对标 DeepTutor 的 knowledge base（`deeptutor/knowledge/manager.py`、
`deeptutor/services/rag/pipelines/llamaindex/ingestion.py`）：用户把资料带进来，
切成带出处的片段，问答只在这个库里召回。差别是这里刻意做小：
不接 llama-index，切分和检索复用现有 BM25 + pgvector + 精排 + 拒答链路。

有 Postgres 时落库；没有数据库时退化成进程内 + 一个 JSON 文件，
方便单测和本地开发（文件放 data/work/，不提交）。
"""
import base64
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from db.store import RevisionConflict, vector_literal
from retrieval.chunk import Chunk
from retrieval.rerank import CrossEncoderReranker, LexicalReranker
from retrieval.search import HybridSearcher, build_searcher

from .chunking import split_text

MAX_NAME = 40
MAX_DESC = 200
MAX_TITLE = 80
MAX_DOC_CHARS = 50000
MAX_DOCS = 200
# 附件只随资料存原文件，不进正文与索引；单篇最多 20 个、单个 8MB。
MAX_ASSET_BYTES = 8 * 1024 * 1024
MAX_ASSETS = 20

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex[:10])


def _clean(text: str, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def wiki_slug(title: str) -> str:
    """页面 slug：保留中英文数字，空白和标点压成连字符。"""
    raw = " ".join((title or "").lower().split())
    raw = re.sub(r"[^0-9a-z一-鿿]+", "-", raw).strip("-")
    return (raw or "page")[:60]


def _asset_name(name: str) -> str:
    """只留文件名，去掉路径、控制字符与多余空白；空名回退「附件」。"""
    raw = (name or "").replace("\\", "/").split("/")[-1]
    raw = "".join(ch for ch in raw if ch >= " " and ch != "\x7f").strip()
    raw = " ".join(raw.split())
    return raw[:120] or "附件"


def _unique_asset_name(existing, name: str) -> str:
    """同一篇资料里文件名不重名，[[文件名]] 才不会指到两份附件。"""
    if name not in existing:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    else:
        ext = "." + ext
    i = 2
    while True:
        cand = ("%s (%d)%s" % (stem, i, ext))[:120]
        if cand not in existing:
            return cand
        i += 1


class LibraryError(ValueError):
    """可以直接映射成 400/404 的错误。"""


class Library:
    def __init__(self, store=None, embedder=None, mode: str = "hybrid",
                 k_fuse: int = 10, reranker_model: str = "BAAI/bge-reranker-v2-m3",
                 state_path: Optional[str] = None):
        self.store = store
        self.embedder = embedder
        self.mode = mode
        self.k_fuse = k_fuse
        self.reranker_model = reranker_model
        self._searchers: Dict[str, tuple] = {}
        self._mem = {"collections": [], "documents": [], "chunks": [],
                     "members": [], "document_versions": [],
                     "document_assets": [], "wiki_pages": [],
                     "wiki_versions": [], "wiki_links": []}
        self._state_path = Path(state_path) if (state_path and store is None) else None
        if self._state_path is not None:
            self._load_state()

    # ---------- 进程内后备存储 ----------

    def _load_state(self) -> None:
        try:
            raw = self._state_path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return
        try:
            data = json.loads(raw)
        except ValueError:
            return
        for key in ("collections", "documents", "chunks", "members",
                    "document_versions", "document_assets", "folders",
                    "audit", "wiki_pages", "wiki_versions", "wiki_links"):
            if isinstance(data.get(key), list):
                self._mem[key] = data[key]
        for key in ("files", "ingest_jobs"):
            if isinstance(data.get(key), dict):
                self._mem[key] = data[key]

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._mem, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(self._state_path))

    def _mem_collection(self, cid: str) -> Optional[Dict]:
        for c in self._mem["collections"]:
            if c["id"] == cid:
                return c
        return None

    # ---------- 资料库 ----------

    def list_collections(self) -> List[Dict]:
        if self.store is not None:
            rows = self.store.list_collections()
        else:
            rows = []
            for c in self._mem["collections"]:
                docs = [d for d in self._mem["documents"]
                        if d["collection_id"] == c["id"]]
                chunks = [k for k in self._mem["chunks"]
                          if k["collection_id"] == c["id"]]
                rows.append({**c, "doc_count": len(docs),
                             "chunk_count": len(chunks)})
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "name": r["name"],
                "description": r.get("description") or "",
                "owner_id": r.get("owner_id") or "",
                "created_by": r.get("created_by") or "",
                # 老库没有这两列时按「公司库、无主」处理，只有管理员能改。
                "visibility": r.get("visibility") or "shared",
                "chunk_target": int(r.get("chunk_target") or 512),
                "chunk_overlap": int(r.get("chunk_overlap") or 50),
                "created_at": str(r.get("created_at") or ""),
                "updated_at": str(r.get("updated_at") or ""),
                "documents": int(r.get("doc_count") or 0),
                "chunks": int(r.get("chunk_count") or 0),
            })
        return out

    def get_collection(self, cid: str) -> Optional[Dict]:
        if self.store is not None:
            row = self.store.get_collection(cid)
        else:
            row = self._mem_collection(cid)
        if not row:
            return None
        docs = self.list_documents(cid)
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row.get("description") or "",
            "owner_id": row.get("owner_id") or "",
            "created_by": row.get("created_by") or "",
            "visibility": row.get("visibility") or "shared",
            "chunk_target": int(row.get("chunk_target") or 512),
            "chunk_overlap": int(row.get("chunk_overlap") or 50),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "documents": docs,
        }

    def update_collection(self, cid: str, name: Optional[str] = None,
                          description: Optional[str] = None,
                          chunk_target: Optional[int] = None,
                          chunk_overlap: Optional[int] = None) -> Dict:
        """改名 / 改说明 / 改切片参数；只改传进来的字段。"""
        row = self._mem_collection(cid) if self.store is None \
            else self.store.get_collection(cid)
        if row is None:
            raise LibraryError("资料库不存在")
        if name is not None:
            name = _clean(name, MAX_NAME)
            if not name:
                raise LibraryError("资料库名称不能为空")
            if self._name_taken(name, row.get("owner_id") or "",
                                row.get("visibility") or "shared") \
                    and name != row.get("name"):
                raise LibraryError("已经有同名资料库：%s" % name)
        if description is not None:
            description = _clean(description, MAX_DESC)
        if chunk_target is not None:
            chunk_target = int(chunk_target)
            if not 120 <= chunk_target <= 2000:
                raise LibraryError("切片大小需在 120–2000 字之间")
        if chunk_overlap is not None:
            chunk_overlap = int(chunk_overlap)
            if not 0 <= chunk_overlap <= 1000:
                raise LibraryError("片段重叠需在 0–1000 字之间")
        if chunk_target is not None and chunk_overlap is not None \
                and chunk_overlap > chunk_target // 2:
            raise LibraryError("片段重叠不能超过切片大小的一半")
        if self.store is not None:
            self.store.update_collection(cid, name, description,
                                         chunk_target, chunk_overlap)
            return self.get_collection(cid) or {}
        for c in self._mem["collections"]:
            if c["id"] == cid:
                if name is not None:
                    c["name"] = name
                if description is not None:
                    c["description"] = description
                if chunk_target is not None:
                    c["chunk_target"] = chunk_target
                if chunk_overlap is not None:
                    c["chunk_overlap"] = chunk_overlap
                c["updated_at"] = _now()
                self._save_state()
                break
        return self.get_collection(cid) or {}

    def _chunk_params(self, cid: str):
        row = self._mem_collection(cid) if self.store is None \
            else self.store.get_collection(cid)
        target = int((row or {}).get("chunk_target") or 512)
        overlap = int((row or {}).get("chunk_overlap") or 50)
        return max(120, min(target, 2000)), max(0, min(overlap, target // 2))

    # ---------- 文件夹树 ----------

    def list_folders(self, cid: str) -> List[Dict]:
        if self.store is not None:
            return self.store.list_folders(cid)
        rows = [{"path": f.get("path") or "", "created_at": ""}
                for f in self._mem.get("folders", []) if f.get("collection_id") == cid]
        return rows

    def folders(self, cid: str) -> List[str]:
        """返回排好序的路径列表；同时把资料上的 folder 也算进去。"""
        paths = {str(f.get("path") or "") for f in self.list_folders(cid)}
        for d in self.list_documents(cid):
            folder = str(d.get("folder") or "").strip("/")
            if folder:
                paths.add(folder)
                parts = folder.split("/")
                for i in range(1, len(parts)):
                    paths.add("/".join(parts[:i]))
        return sorted(p for p in paths if p)

    def add_folder(self, cid: str, path: str) -> Dict:
        path = "/".join(p.strip() for p in (path or "").replace("\\", "/").split("/")
                        if p.strip())
        if not path:
            raise LibraryError("文件夹名不能为空")
        if len(path) > 120:
            raise LibraryError("文件夹路径最多 120 个字符")
        if self.store is not None:
            self.store.add_folder(cid, path)
        else:
            parts = path.split("/")
            for i in range(1, len(parts) + 1):
                p = "/".join(parts[:i])
                if not any(f["collection_id"] == cid and f["path"] == p
                           for f in self._mem.setdefault("folders", [])):
                    self._mem["folders"].append(
                        {"collection_id": cid, "path": p})
            self._save_state()
        return {"path": path, "folders": self.folders(cid)}

    def rename_folder(self, cid: str, old: str, new: str) -> Dict:
        old = (old or "").strip("/")
        new = (new or "").strip("/")
        if not old or not new:
            raise LibraryError("原文件夹和新文件夹都不能为空")
        if new == old or new.startswith(old + "/"):
            raise LibraryError("不能把文件夹移动到它自己里面")
        for p in self.folders(cid):
            if (p == new or p.startswith(new + "/")) and \
                    not (p == old or p.startswith(old + "/")):
                raise LibraryError("目标文件夹已经存在：%s" % new)
        if self.store is not None:
            self.store.rename_folder(cid, old, new)
        else:
            for d in self._mem["documents"]:
                if d["collection_id"] != cid:
                    continue
                folder = str(d.get("folder") or "")
                if folder == old or folder.startswith(old + "/"):
                    d["folder"] = new + folder[len(old):]
            folders = self._mem.setdefault("folders", [])
            for f in list(folders):
                if f["collection_id"] != cid:
                    continue
                p = f["path"]
                if p == old or p.startswith(old + "/"):
                    f["path"] = new + p[len(old):]
            parts = new.split("/")
            for i in range(1, len(parts) + 1):
                p = "/".join(parts[:i])
                if not any(f["collection_id"] == cid and f["path"] == p
                           for f in folders):
                    folders.append({"collection_id": cid, "path": p})
            self._save_state()
        self._searchers.pop(cid, None)
        return {"folders": self.folders(cid)}

    def delete_folder(self, cid: str, path: str) -> Dict:
        path = (path or "").strip("/")
        if not path:
            raise LibraryError("文件夹不能为空")
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if self.store is not None:
            self.store.delete_folder(cid, path)
        else:
            for d in self._mem["documents"]:
                if d["collection_id"] != cid:
                    continue
                folder = str(d.get("folder") or "")
                if folder == path or folder.startswith(path + "/"):
                    d["folder"] = parent
            self._mem["folders"] = [
                f for f in self._mem.setdefault("folders", [])
                if not (f["collection_id"] == cid and
                        (f["path"] == path or f["path"].startswith(path + "/")))]
            self._save_state()
        self._searchers.pop(cid, None)
        return {"folders": self.folders(cid)}

    def move_document(self, cid: str, doc_id: str, folder: str) -> Dict:
        folder = "/".join(p.strip() for p in (folder or "").replace("\\", "/").split("/")
                          if p.strip())
        if self.store is not None:
            self.store.set_document_folder(cid, doc_id, folder)
        else:
            hit = None
            for d in self._mem["documents"]:
                if d["id"] == doc_id and d["collection_id"] == cid:
                    d["folder"] = folder
                    d["updated_at"] = _now()
                    hit = d
                    break
            if hit is None:
                raise LibraryError("资料不存在")
            self._save_state()
        if folder:
            self.add_folder(cid, folder)
        return {"id": doc_id, "folder": folder}

    # ---------- 操作日志 ----------

    def audit(self, cid: str, actor: Optional[Dict] = None, action: str = "",
              target_type: str = "", target_id: str = "", detail: str = "") -> None:
        """写一条知识库操作日志；日志失败不影响主流程。"""
        actor = actor or {}
        try:
            if self.store is not None:
                self.store.add_audit(cid, actor.get("id") or "",
                                     actor.get("username") or "",
                                     action, target_type, target_id, detail)
            else:
                self._mem.setdefault("audit", []).append({
                    "id": len(self._mem["audit"]) + 1,
                    "collection_id": cid,
                    "created_at": _now(),
                    "actor_id": actor.get("id") or "",
                    "actor_name": actor.get("username") or "",
                    "action": action, "target_type": target_type,
                    "target_id": target_id, "detail": detail[:500]})
                self._save_state()
        except Exception:
            pass

    def list_audit(self, cid: str, limit: int = 50) -> List[Dict]:
        if self.store is not None:
            rows = self.store.list_audit(cid, limit)
        else:
            rows = [r for r in reversed(self._mem.setdefault("audit", []))
                    if r.get("collection_id") == cid]
        out = []
        for r in rows[:max(1, min(int(limit), 200))]:
            out.append({**r, "created_at": str(r.get("created_at") or "")})
        return out

    # ---------- 自动 Wiki ----------

    def wiki_pages(self, cid: str) -> List[Dict]:
        if self.store is not None:
            rows = self.store.list_wiki_pages(cid)
        else:
            rows = [dict(p) for p in self._mem.setdefault("wiki_pages", [])
                    if p.get("collection_id") == cid]
            rows.sort(key=lambda p: str(p.get("title") or ""))
        return [{**r, "created_at": str(r.get("created_at") or ""),
                 "updated_at": str(r.get("updated_at") or "")} for r in rows]

    def wiki_page(self, cid: str, page_id: str) -> Optional[Dict]:
        if self.store is not None:
            row = self.store.get_wiki_page(cid, page_id)
        else:
            row = next((dict(p) for p in self._mem.setdefault("wiki_pages", [])
                        if p.get("collection_id") == cid
                        and p.get("id") == page_id), None)
        if row is None:
            return None
        row["created_at"] = str(row.get("created_at") or "")
        row["updated_at"] = str(row.get("updated_at") or "")
        return row

    def wiki_page_by_slug(self, cid: str, slug: str) -> Optional[Dict]:
        if self.store is not None:
            return self.store.wiki_page_by_slug(cid, slug)
        return next((dict(p) for p in self._mem.setdefault("wiki_pages", [])
                     if p.get("collection_id") == cid
                     and p.get("slug") == slug), None)

    def replace_wiki_pages(self, cid: str, pages: List[Dict],
                           links: List[Dict]) -> List[Dict]:
        """替换未编辑页面；手动编辑过的同 slug 页面保留。"""
        if self.store is not None:
            self.store.replace_wiki_pages(cid, pages, links)
            return self.wiki_pages(cid)
        old = [p for p in self._mem.setdefault("wiki_pages", [])
               if p.get("collection_id") == cid]
        edited_slugs = {p.get("slug") for p in old if p.get("edited")}
        incoming = [p for p in pages if p.get("slug") not in edited_slugs]
        incoming_slugs = {p.get("slug") for p in incoming}
        kept = [p for p in old if p.get("edited")]
        self._mem["wiki_pages"] = [p for p in self._mem["wiki_pages"]
                                   if p.get("collection_id") != cid] + kept
        for p in incoming:
            exist = next((x for x in self._mem["wiki_pages"]
                          if x.get("collection_id") == cid
                          and x.get("slug") == p.get("slug")), None)
            row = {
                "id": (exist or {}).get("id") or p["id"],
                "collection_id": cid, "slug": p["slug"], "title": p["title"],
                "summary": p.get("summary") or "", "body": p.get("body") or "",
                "kind": p.get("kind") or "概念",
                "aliases": list(p.get("aliases") or []),
                "source_doc_ids": list(p.get("source_doc_ids") or []),
                "source_chunk_ids": list(p.get("source_chunk_ids") or []),
                "generated": True, "edited": False,
                "revision": int((exist or {}).get("revision") or 0),
                "created_at": (exist or {}).get("created_at") or _now(),
                "updated_at": _now(),
            }
            if exist is not None:
                exist.update(row)
            else:
                self._mem["wiki_pages"].append(row)
        # 没被这次生成覆盖的旧生成页删掉（已在上面过滤）
        self._mem["wiki_pages"] = [
            p for p in self._mem["wiki_pages"]
            if p.get("collection_id") != cid
            or p.get("edited") or p.get("slug") in incoming_slugs]
        self._mem["wiki_links"] = [
            e for e in self._mem.setdefault("wiki_links", [])
            if e.get("collection_id") != cid]
        for e in links:
            if e.get("from") and e.get("to") and e["from"] != e["to"]:
                self._mem["wiki_links"].append({
                    "collection_id": cid, "from_page": e["from"],
                    "to_page": e["to"], "kind": e.get("kind") or "related",
                    "weight": int(e.get("weight") or 1)})
        self._save_state()
        return self.wiki_pages(cid)

    def wiki_links(self, cid: str) -> List[Dict]:
        if self.store is not None:
            return self.store.list_wiki_links(cid)
        return [dict(e) for e in self._mem.setdefault("wiki_links", [])
                if e.get("collection_id") == cid]

    def replace_wiki_links(self, cid: str, links: List[Dict]) -> None:
        if self.store is not None:
            self.store.replace_wiki_links(cid, links)
            return
        self._mem["wiki_links"] = [
            e for e in self._mem.setdefault("wiki_links", [])
            if e.get("collection_id") != cid]
        for e in links:
            if e.get("from") and e.get("to") and e["from"] != e["to"]:
                self._mem["wiki_links"].append({
                    "collection_id": cid, "from_page": e["from"],
                    "to_page": e["to"], "kind": e.get("kind") or "related",
                    "weight": int(e.get("weight") or 1)})
        self._save_state()

    def update_wiki_page(self, cid: str, page_id: str, title: str,
                         summary: str, body: str,
                         editor: str = "", kind: Optional[str] = None) -> Optional[Dict]:
        if self.store is not None:
            return self.store.update_wiki_page(cid, page_id, title, summary,
                                               body, editor, kind)
        row = next((p for p in self._mem.setdefault("wiki_pages", [])
                    if p.get("collection_id") == cid
                    and p.get("id") == page_id), None)
        if row is None:
            return None
        self._mem.setdefault("wiki_versions", []).append({
            "id": len(self._mem["wiki_versions"]) + 1,
            "page_id": page_id, "collection_id": cid,
            "title": row.get("title") or "", "summary": row.get("summary") or "",
            "body": row.get("body") or "", "editor": editor or "",
            "created_at": _now()})
        row.update({"title": title, "summary": summary, "body": body,
                    "kind": kind or row.get("kind") or "概念",
                    "edited": True, "revision": int(row.get("revision") or 0) + 1,
                    "updated_at": _now()})
        self._save_state()
        return self.wiki_page(cid, page_id)

    def wiki_versions(self, cid: str, page_id: str,
                      limit: int = 20) -> List[Dict]:
        if self.store is not None:
            rows = self.store.list_wiki_versions(cid, page_id, limit)
        else:
            rows = [r for r in reversed(self._mem.setdefault("wiki_versions", []))
                    if r.get("page_id") == page_id
                    and r.get("collection_id") == cid][:limit]
        return [{**r, "created_at": str(r.get("created_at") or "")}
                for r in rows]

    def restore_wiki_version(self, cid: str, page_id: str, version_id: int,
                             editor: str = "") -> Optional[Dict]:
        if self.store is not None:
            return self.store.restore_wiki_version(cid, page_id, version_id,
                                                   editor)
        row = next((p for p in self._mem.setdefault("wiki_pages", [])
                    if p.get("collection_id") == cid
                    and p.get("id") == page_id), None)
        old = next((v for v in self._mem.setdefault("wiki_versions", [])
                    if int(v.get("id") or 0) == int(version_id)
                    and v.get("page_id") == page_id), None)
        if row is None or old is None:
            return None
        self._mem["wiki_versions"].append({
            "id": len(self._mem["wiki_versions"]) + 1,
            "page_id": page_id, "collection_id": cid,
            "title": row.get("title") or "", "summary": row.get("summary") or "",
            "body": row.get("body") or "", "editor": editor or "",
            "created_at": _now()})
        row.update({"title": old.get("title") or "",
                    "summary": old.get("summary") or "",
                    "body": old.get("body") or "", "edited": True,
                    "revision": int(row.get("revision") or 0) + 1,
                    "updated_at": _now()})
        self._save_state()
        return self.wiki_page(cid, page_id)

    def delete_wiki_page(self, cid: str, page_id: str) -> bool:
        if self.store is not None:
            return self.store.delete_wiki_page(cid, page_id)
        before = len(self._mem.setdefault("wiki_pages", []))
        self._mem["wiki_pages"] = [
            p for p in self._mem["wiki_pages"]
            if not (p.get("collection_id") == cid and p.get("id") == page_id)]
        ok = len(self._mem["wiki_pages"]) != before
        if ok:
            self._save_state()
        return ok

    def rebuild_wiki_links(self, cid: str) -> List[Dict]:
        """按页面正文里的 [[标题]] 和共享来源文档，重建 Wiki 关系边。"""
        pages = self.wiki_pages(cid)
        by_title = {str(p.get("title") or "").strip().lower(): p for p in pages}
        doc_folder = {d["id"]: d.get("folder") or ""
                      for d in self.list_documents(cid)}
        edges = []
        seen = set()

        def add(a, b, kind, weight=1):
            if not a or not b or a == b:
                return
            key = (a, b, kind)
            if key in seen:
                return
            seen.add(key)
            edges.append({"from": a, "to": b, "kind": kind, "weight": weight})

        for p in pages:
            body = p.get("body") or ""
            for name in re.findall(r"\[\[([^\]\n|]+)(?:\|[^\]\n]+)?\]\]", body):
                target = by_title.get(name.strip().lower())
                if target is not None:
                    add(p["id"], target["id"], "link")
            # 模型有时直接写页面名而不加 [[ ]]；正文提到别的页面也算一条关系
            lower = body.lower()
            for q in pages:
                if q["id"] == p["id"]:
                    continue
                name = str(q.get("title") or "").strip().lower()
                if len(name) >= 2 and name in lower:
                    add(p["id"], q["id"], "mention")
        # 共享来源文档：两页至少共用一篇资料就连一条弱关系
        for i, a in enumerate(pages):
            sa = set(a.get("source_doc_ids") or [])
            if not sa:
                continue
            for b in pages[i + 1:]:
                shared = sa & set(b.get("source_doc_ids") or [])
                if shared:
                    add(a["id"], b["id"], "shared", len(shared))
                    add(b["id"], a["id"], "shared", len(shared))
        # 同一文件夹下的页面连一条弱关系，默认库也能看出分组
        for i, a in enumerate(pages):
            fa = next((doc_folder.get(d) for d in (a.get("source_doc_ids") or [])
                       if doc_folder.get(d)), "")
            if not fa:
                continue
            for b in pages[i + 1:]:
                fb = next((doc_folder.get(d) for d in (b.get("source_doc_ids") or [])
                           if doc_folder.get(d)), "")
                if fb == fa:
                    add(a["id"], b["id"], "folder")
                    add(b["id"], a["id"], "folder")
        # 每个页面最多留 6 条出边，图不至于糊成一团
        out = {}
        for e in edges:
            bucket = out.setdefault(e["from"], [])
            if len(bucket) < 6:
                bucket.append(e)
        final = [e for bucket in out.values() for e in bucket]
        self.replace_wiki_links(cid, final)
        return final

    # ---------- 原始文件与持久化进料任务 ----------

    def save_document_file(self, cid: str, doc_id: str, filename: str,
                           mime: str, data: bytes) -> None:
        if self.store is not None:
            self.store.save_document_file(doc_id, cid, filename, mime, data)
        else:
            self._mem.setdefault("files", {})[doc_id] = {
                "document_id": doc_id, "filename": filename, "mime": mime,
                "nbytes": len(data), "data": base64.b64encode(data).decode("ascii")}
            self._save_state()

    def get_document_file(self, doc_id: str) -> Optional[Dict]:
        if self.store is not None:
            return self.store.get_document_file(doc_id)
        hit = self._mem.setdefault("files", {}).get(doc_id)
        if not hit:
            return None
        out = dict(hit)
        out["data"] = base64.b64decode(out.get("data") or "")
        return out

    def mark_document_processing(self, cid: str, doc_id: str) -> None:
        if self.store is not None:
            self.store.set_document_status(cid, doc_id, "processing", error="")
        else:
            for d in self._mem["documents"]:
                if d["id"] == doc_id and d["collection_id"] == cid:
                    d.update({"status": "processing", "error": "",
                              "updated_at": _now()})
                    break
            self._save_state()

    def start_ingest_job(self, cid: str, doc_id: str, filename: str = "") -> None:
        if self.store is not None:
            self.store.upsert_ingest_job(doc_id, cid, filename, "queued")
        else:
            jobs = self._mem.setdefault("ingest_jobs", {})
            jobs[doc_id] = {"document_id": doc_id, "collection_id": cid,
                            "filename": filename, "status": "queued",
                            "attempts": 0, "error": ""}
            self._save_state()

    def set_ingest_job(self, doc_id: str, status: str, error: str = "",
                       bump_attempt: bool = False) -> None:
        if self.store is not None:
            self.store.set_ingest_job(doc_id, status, error, bump_attempt)
        else:
            job = self._mem.setdefault("ingest_jobs", {}).get(doc_id)
            if job is not None:
                job.update({"status": status, "error": error[:500]})
                if bump_attempt:
                    job["attempts"] = int(job.get("attempts") or 0) + 1
                self._save_state()

    def pending_ingest_jobs(self) -> List[Dict]:
        if self.store is not None:
            return self.store.list_pending_ingest_jobs()
        return [dict(j) for j in self._mem.setdefault("ingest_jobs", {}).values()
                if j.get("status") in ("queued", "running")]

    def _name_taken(self, name: str, owner_id: str, visibility: str) -> bool:
        """同名判断按空间范围：个人库只跟自己比，公司库跟公司库比。

        这样两个成员可以各自有一个「机器学习笔记」，但公司库里不能重复。
        """
        rows = (self.store.list_collections() if self.store is not None
                else self._mem["collections"])
        for c in rows:
            if c.get("name") != name:
                continue
            if visibility == "private":
                if (c.get("visibility") or "shared") == "private" \
                        and (c.get("owner_id") or "") == (owner_id or ""):
                    return True
            elif (c.get("visibility") or "shared") == "shared":
                return True
        return False

    def create_collection(self, name: str, description: str = "",
                          owner_id: str = "", visibility: str = "shared",
                          created_by: str = "") -> Dict:
        name = _clean(name, MAX_NAME)
        if not name:
            raise LibraryError("资料库名称不能为空")
        if visibility not in ("shared", "private"):
            raise LibraryError("资料库类型不对")
        if visibility == "private" and not owner_id:
            raise LibraryError("个人知识库需要登录后创建")
        description = _clean(description, MAX_DESC)
        if self._name_taken(name, owner_id, visibility):
            raise LibraryError("已经有同名资料库：%s" % name)
        cid = _new_id("lib")
        now = _now()
        if self.store is not None:
            self.store.create_collection(cid, name, description,
                                         owner_id=owner_id or None,
                                         visibility=visibility,
                                         created_by=created_by or owner_id or None)
        else:
            self._mem["collections"].append({
                "id": cid, "name": name, "description": description,
                "owner_id": owner_id or "", "visibility": visibility,
                "created_by": created_by or owner_id or "",
                "chunk_target": 512, "chunk_overlap": 50,
                "created_at": now, "updated_at": now})
            self._save_state()
        return {"id": cid, "name": name, "description": description,
                "owner_id": owner_id or "", "visibility": visibility,
                "created_by": created_by or owner_id or "",
                "chunk_target": 512, "chunk_overlap": 50,
                "created_at": now, "updated_at": now,
                "documents": [], "chunks": 0}

    def delete_collection(self, cid: str) -> bool:
        if self.store is not None:
            ok = self.store.delete_collection(cid)
        else:
            before = len(self._mem["collections"])
            self._mem["collections"] = [
                c for c in self._mem["collections"] if c["id"] != cid]
            self._mem["documents"] = [
                d for d in self._mem["documents"] if d["collection_id"] != cid]
            self._mem["chunks"] = [
                k for k in self._mem["chunks"] if k["collection_id"] != cid]
            self._mem["members"] = [
                m for m in self._mem["members"] if m["collection_id"] != cid]
            self._mem["document_versions"] = [
                v for v in self._mem["document_versions"]
                if v["collection_id"] != cid]
            self._mem["document_assets"] = [
                a for a in self._mem["document_assets"]
                if a["collection_id"] != cid]
            ok = len(self._mem["collections"]) != before
            if ok:
                self._save_state()
        self._searchers.pop(cid, None)
        return ok

    # ---------- 成员 ----------
    # owner 记在 collections.owner_id 上，这里只存 editor / viewer。

    def collection_member_role(self, cid: str, user_id: str) -> str:
        if not user_id:
            return ""
        if self.store is not None:
            return self.store.get_collection_member(cid, user_id) or ""
        for m in self._mem["members"]:
            if m["collection_id"] == cid and m["user_id"] == user_id:
                return str(m.get("role") or "")
        return ""

    def list_collection_members(self, cid: str) -> List[Dict]:
        if self.store is not None:
            return self.store.list_collection_members(cid)
        rows = [dict(m) for m in self._mem["members"]
                if m["collection_id"] == cid]
        return sorted(rows, key=lambda m: (m.get("created_at") or "",
                                           m.get("user_id") or ""))

    def set_collection_member(self, cid: str, user_id: str, role: str) -> None:
        if role not in ("editor", "viewer"):
            raise LibraryError("成员角色只能是可编辑或只读")
        if not user_id:
            raise LibraryError("成员不能为空")
        if self.store is not None:
            self.store.set_collection_member(cid, user_id, role)
            return
        now = _now()
        for m in self._mem["members"]:
            if m["collection_id"] == cid and m["user_id"] == user_id:
                m["role"] = role
                self._save_state()
                return
        self._mem["members"].append({
            "collection_id": cid, "user_id": user_id, "role": role,
            "created_at": now})
        self._save_state()

    def remove_collection_member(self, cid: str, user_id: str) -> bool:
        if self.store is not None:
            return self.store.remove_collection_member(cid, user_id)
        before = len(self._mem["members"])
        self._mem["members"] = [
            m for m in self._mem["members"]
            if not (m["collection_id"] == cid and m["user_id"] == user_id)]
        ok = len(self._mem["members"]) != before
        if ok:
            self._save_state()
        return ok

    # ---------- 文档 ----------

    def list_documents(self, cid: str) -> List[Dict]:
        if self.store is not None:
            rows = self.store.list_documents(cid)
        else:
            rows = []
            for d in self._mem["documents"]:
                if d["collection_id"] != cid:
                    continue
                n = len([k for k in self._mem["chunks"] if k["document_id"] == d["id"]])
                rows.append({"id": d["id"], "title": d["title"],
                             "sha256": d.get("sha256", ""),
                             "created_at": d["created_at"],
                             "updated_at": d.get("updated_at") or d["created_at"],
                             "status": d.get("status") or "completed",
                             "error": d.get("error") or "",
                             "bytes": int(d.get("bytes") or 0),
                             "source": d.get("source") or "",
                             "folder": d.get("folder") or "",
                             "tags": list(d.get("tags") or []),
                             "chars": len(d["text"]), "chunk_count": n})
        return [{
            "id": r["id"],
            "title": r["title"] or "未命名资料",
            "created_at": str(r.get("created_at") or ""),
            "updated_at": str(r.get("updated_at") or r.get("created_at") or ""),
            "status": r.get("status") or "completed",
            "error": r.get("error") or "",
            "bytes": int(r.get("bytes") or 0),
            "source": r.get("source") or "",
            "folder": r.get("folder") or "",
            "tags": list(r.get("tags") or []),
            "chars": int(r.get("chars") or 0),
            "chunks": int(r.get("chunk_count") or 0),
            "sha256": r.get("sha256") or "",
        } for r in rows]

    def add_document(self, cid: str, text: str, title: str = "",
                     source: str = "", nbytes: int = 0,
                     folder: str = "", tags: Optional[List[str]] = None) -> Dict:
        if self.get_collection(cid) is None:
            raise LibraryError("资料库不存在")
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(text) < 20:
            raise LibraryError("资料太短（至少 20 字），贴一段正文再来")
        if len(text) > MAX_DOC_CHARS:
            raise LibraryError("单篇资料最多 %d 字，当前 %d 字"
                               % (MAX_DOC_CHARS, len(text)))
        docs = self.list_documents(cid)
        if len(docs) >= MAX_DOCS:
            raise LibraryError("单个资料库最多 %d 篇，先删掉一些再加" % MAX_DOCS)
        title = _clean(title, MAX_TITLE) or _clean(text.split("\n")[0], MAX_TITLE) \
            or "未命名资料"
        sha = _sha(text)
        for d in docs:
            if d.get("sha256") == sha:
                raise LibraryError("这篇资料已经在库里了：%s" % d["title"])

        target, overlap = self._chunk_params(cid)
        pieces = split_text(text, target=target, overlap=overlap)
        if not pieces:
            raise LibraryError("没有切出可用片段，检查一下资料内容")
        doc_id = _new_id("doc")
        chunks = [{"id": "%s-%03d" % (doc_id, p["seq"]), "seq": p["seq"],
                   "text": p["text"], "start": p["start"], "end": p["end"]}
                  for p in pieces]

        if self.store is not None:
            vectors: Dict[str, str] = {}
            if self.embedder is not None:
                arr = self.embedder.embed([c["text"] for c in chunks])
                vectors = {c["id"]: vector_literal(v)
                           for c, v in zip(chunks, arr)}
            self.store.add_document(doc_id, cid, title, text, sha, chunks, vectors,
                                    source=source, nbytes=nbytes,
                                    folder=folder, tags=tags)
        else:
            now = _now()
            self._mem["documents"].append({
                "id": doc_id, "collection_id": cid, "title": title,
                "text": text, "sha256": sha, "created_at": now,
                "updated_at": now, "status": "completed", "error": "",
                "source": source, "bytes": int(nbytes or 0), "tables": [],
                "folder": folder or "", "tags": list(tags or [])})
            for c in chunks:
                self._mem["chunks"].append({**c, "collection_id": cid,
                                            "document_id": doc_id})
            coll = self._mem_collection(cid)
            if coll:
                coll["updated_at"] = now
            self._save_state()

        self._searchers.pop(cid, None)
        return {"id": doc_id, "title": title, "chars": len(text),
                "chunks": len(chunks), "created_at": _now()}

    def delete_document(self, cid: str, doc_id: str) -> bool:
        if self.store is not None:
            ok = self.store.delete_document(cid, doc_id)
        else:
            before = len(self._mem["documents"])
            self._mem["documents"] = [
                d for d in self._mem["documents"]
                if not (d["id"] == doc_id and d["collection_id"] == cid)]
            self._mem["chunks"] = [
                k for k in self._mem["chunks"]
                if not (k["document_id"] == doc_id and k["collection_id"] == cid)]
            self._mem["document_versions"] = [
                v for v in self._mem["document_versions"]
                if not (v["document_id"] == doc_id
                        and v["collection_id"] == cid)]
            self._mem["document_assets"] = [
                a for a in self._mem["document_assets"]
                if not (a["document_id"] == doc_id
                        and a["collection_id"] == cid)]
            ok = len(self._mem["documents"]) != before
            if ok:
                self._mem.setdefault("files", {}).pop(doc_id, None)
                self._mem.setdefault("ingest_jobs", {}).pop(doc_id, None)
                self._save_state()
        self._searchers.pop(cid, None)
        return ok

    def get_document(self, cid: str, doc_id: str) -> Optional[Dict]:
        if self.store is not None:
            return self.store.get_document(cid, doc_id)
        for d in self._mem["documents"]:
            if d["id"] == doc_id and d["collection_id"] == cid:
                return d
        return None

    def document_text(self, cid: str, doc_id: str) -> Optional[str]:
        row = self.get_document(cid, doc_id)
        return row["text"] if row else None

    # ---------- 整篇编辑与历史版本 ----------

    MAX_VERSIONS = 20

    def update_document(self, cid: str, doc_id: str, text: str,
                        title: str = "", author: str = "") -> Dict:
        """保存整篇资料：旧版进历史、正文重切分、向量重建、检索器失效。"""
        doc = self.get_document(cid, doc_id)
        if doc is None:
            raise LibraryError("资料不存在")
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(text) < 20:
            raise LibraryError("资料太短（至少 20 字）")
        if len(text) > MAX_DOC_CHARS:
            raise LibraryError("单篇资料最多 %d 字，当前 %d 字"
                               % (MAX_DOC_CHARS, len(text)))
        title = _clean(title, MAX_TITLE) or _clean(text.split("\n")[0], MAX_TITLE) \
            or "未命名资料"
        sha = _sha(text)
        for d in self.list_documents(cid):
            if d["id"] != doc_id and d.get("sha256") == sha:
                raise LibraryError("这篇资料已经在库里了：%s" % d["title"])
        target, overlap = self._chunk_params(cid)
        pieces = split_text(text, target=target, overlap=overlap)
        if not pieces:
            raise LibraryError("没有切出可用片段，检查一下资料内容")
        chunks = [{"id": "%s-%03d" % (doc_id, p["seq"]), "seq": p["seq"],
                   "text": p["text"], "start": p["start"], "end": p["end"]}
                  for p in pieces]
        vectors: Dict[str, str] = {}
        if self.store is not None and self.embedder is not None:
            arr = self.embedder.embed([c["text"] for c in chunks])
            vectors = {c["id"]: vector_literal(v) for c, v in zip(chunks, arr)}

        if self.store is not None:
            row = self.store.update_document(
                cid, doc_id, title, text, sha, chunks, vectors,
                author=author, keep_versions=self.MAX_VERSIONS)
            if row is None:
                raise LibraryError("资料不存在")
            version_id = int(row["version_id"])
        else:
            now = _now()
            versions = [v for v in self._mem["document_versions"]
                        if v["document_id"] == doc_id
                        and v["collection_id"] == cid]
            version_id = max([int(v["id"]) for v in versions] or [0]) + 1
            self._mem["document_versions"].append({
                "id": version_id, "collection_id": cid,
                "document_id": doc_id, "title": doc.get("title") or "",
                "text": doc.get("text") or "",
                "sha256": doc.get("sha256") or "",
                "author": author or "", "created_at": now})
            for d in self._mem["documents"]:
                if d["id"] == doc_id and d["collection_id"] == cid:
                    d.update({"title": title, "text": text, "sha256": sha,
                              "status": "completed", "error": "",
                              "updated_at": now, "tables": []})
            self._mem["chunks"] = [
                k for k in self._mem["chunks"] if k["document_id"] != doc_id]
            for c in chunks:
                self._mem["chunks"].append({**c, "collection_id": cid,
                                            "document_id": doc_id,
                                            "revision": 0})
            keep = {v["id"] for v in self._mem["document_versions"]
                    if v["document_id"] == doc_id and v["collection_id"] == cid}
            ordered = sorted(keep, reverse=True)
            drop = set(ordered[self.MAX_VERSIONS:])
            self._mem["document_versions"] = [
                v for v in self._mem["document_versions"]
                if not (v["document_id"] == doc_id and v["id"] in drop)]
            self._save_state()

        self._searchers.pop(cid, None)
        return {"id": doc_id, "title": title, "chars": len(text),
                "chunks": len(chunks), "version_id": version_id,
                "updated_at": _now()}

    def list_document_versions(self, cid: str, doc_id: str,
                               limit: int = 20) -> List[Dict]:
        limit = max(1, min(int(limit or 20), self.MAX_VERSIONS))
        if self.store is not None:
            return self.store.list_document_versions(cid, doc_id, limit)
        rows = [v for v in self._mem["document_versions"]
                if v["document_id"] == doc_id and v["collection_id"] == cid]
        rows.sort(key=lambda v: int(v["id"]), reverse=True)
        return [{
            "id": int(v["id"]),
            "title": v.get("title") or "",
            "sha256": v.get("sha256") or "",
            "author": v.get("author") or "",
            "created_at": str(v.get("created_at") or ""),
            "chars": len(v.get("text") or ""),
        } for v in rows[:limit]]

    def get_document_version(self, cid: str, doc_id: str,
                             version_id: int) -> Optional[Dict]:
        if self.store is not None:
            return self.store.get_document_version(cid, doc_id, version_id)
        for v in self._mem["document_versions"]:
            if (v["document_id"] == doc_id and v["collection_id"] == cid
                    and int(v["id"]) == int(version_id)):
                out = dict(v)
                out["id"] = int(out["id"])
                return out
        return None

    def restore_document_version(self, cid: str, doc_id: str,
                                 version_id: int, author: str = "") -> Dict:
        version = self.get_document_version(cid, doc_id, version_id)
        if version is None:
            raise LibraryError("历史版本不存在")
        out = self.update_document(cid, doc_id, version.get("text") or "",
                                   title=version.get("title") or "",
                                   author=author)
        out["restored_from"] = int(version["id"])
        return out

    # ---------- 附件：插图与非图片文件 ----------

    def add_document_asset(self, cid: str, doc_id: str, filename: str,
                           mime: str, data: bytes) -> Dict:
        """把原文件挂到一篇资料上。正文不动，也不进检索库。"""
        if self.get_document(cid, doc_id) is None:
            raise LibraryError("资料不存在")
        if not data:
            raise LibraryError("附件是空的")
        if len(data) > MAX_ASSET_BYTES:
            raise LibraryError("单个附件最多 %d MB" % (MAX_ASSET_BYTES // 1048576))
        existing = self.list_document_assets(cid, doc_id)
        if len(existing) >= MAX_ASSETS:
            raise LibraryError("一篇资料最多 %d 个附件，先删掉一些" % MAX_ASSETS)
        name = _unique_asset_name({a["name"] for a in existing},
                                  _asset_name(filename))
        asset_id = _new_id("asset")
        if self.store is not None:
            row = self.store.add_document_asset(asset_id, cid, doc_id, name,
                                                mime or "", data)
            if row is None:
                raise LibraryError("资料不存在")
            return row
        row = {"id": asset_id, "collection_id": cid, "document_id": doc_id,
               "filename": name, "mime": mime or "", "nbytes": len(data),
               "data": base64.b64encode(data).decode("ascii"),
               "created_at": _now()}
        self._mem["document_assets"].append(row)
        self._save_state()
        return {"id": asset_id, "name": name, "mime": mime or "",
                "bytes": len(data), "created_at": row["created_at"]}

    def list_document_assets(self, cid: str, doc_id: str) -> List[Dict]:
        if self.store is not None:
            return self.store.list_document_assets(cid, doc_id)
        return [{
            "id": a["id"], "name": a["filename"], "mime": a.get("mime") or "",
            "bytes": int(a.get("nbytes") or 0),
            "created_at": str(a.get("created_at") or ""),
        } for a in self._mem["document_assets"]
            if a["document_id"] == doc_id and a["collection_id"] == cid]

    def get_document_asset(self, cid: str, doc_id: str,
                           asset_id: str) -> Optional[Dict]:
        if self.store is not None:
            return self.store.get_document_asset(cid, doc_id, asset_id)
        for a in self._mem["document_assets"]:
            if (a["document_id"] == doc_id and a["collection_id"] == cid
                    and a["id"] == asset_id):
                out = dict(a)
                out["name"] = out.pop("filename")
                out["data"] = base64.b64decode(out.get("data") or "")
                out["bytes"] = int(out.get("nbytes") or 0)
                return out
        return None

    def delete_document_asset(self, cid: str, doc_id: str,
                              asset_id: str) -> bool:
        if self.store is not None:
            return self.store.delete_document_asset(cid, doc_id, asset_id)
        before = len(self._mem["document_assets"])
        self._mem["document_assets"] = [
            a for a in self._mem["document_assets"]
            if not (a["id"] == asset_id and a["document_id"] == doc_id
                    and a["collection_id"] == cid)]
        ok = len(self._mem["document_assets"]) != before
        if ok:
            self._save_state()
        return ok

    # ---------- 文件进料：先占位（processing），解析完再切分入库 ----------

    def create_pending_document(self, cid: str, title: str, source: str = "",
                                nbytes: int = 0) -> Dict:
        """上传一落地就建一条 processing 的记录，前端据此显示「解析中」。"""
        if self.get_collection(cid) is None:
            raise LibraryError("资料库不存在")
        if len(self.list_documents(cid)) >= MAX_DOCS:
            raise LibraryError("单个资料库最多 %d 篇，先删掉一些再加" % MAX_DOCS)
        title = _clean(title, MAX_TITLE) or "未命名资料"
        doc_id = _new_id("doc")
        now = _now()
        if self.store is not None:
            self.store.add_document(doc_id, cid, title, "", "", [], {},
                                    status="processing", source=source,
                                    nbytes=nbytes)
        else:
            self._mem["documents"].append({
                "id": doc_id, "collection_id": cid, "title": title,
                "text": "", "sha256": "", "created_at": now, "updated_at": now,
                "status": "processing", "error": "", "source": source,
                "bytes": int(nbytes or 0)})
            self._save_state()
        return {"id": doc_id, "title": title, "status": "processing", "chars": 0,
                "chunks": 0, "source": source, "bytes": int(nbytes or 0)}

    def finish_document(self, cid: str, doc_id: str, text: str,
                        tables: Optional[List[Dict]] = None) -> Optional[Dict]:
        """解析出的正文：查重 → 切分 → 向量化 → 替换片段，状态转 completed。

        tables 是进料时抽出的结构化表格，随文档一起存；统计类问题直接数它。
        重新切分（reparse）不传，保留原有表格。
        """
        doc = self.get_document(cid, doc_id)
        if doc is None:
            return None
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(text) < 20:
            self._fail_document(cid, doc_id, "没提取到足够的文字（至少 20 字）")
            return None
        if len(text) > MAX_DOC_CHARS:
            self._fail_document(cid, doc_id, "提取到 %d 字，超过单篇 %d 字上限，"
                                "请拆分后再上传" % (len(text), MAX_DOC_CHARS))
            return None
        sha = _sha(text)
        for d in self.list_documents(cid):
            if d["id"] != doc_id and d.get("sha256") == sha:
                self._fail_document(cid, doc_id,
                                    "这篇资料已经在库里了：%s" % d["title"])
                return None
        target, overlap = self._chunk_params(cid)
        pieces = split_text(text, target=target, overlap=overlap)
        if not pieces:
            self._fail_document(cid, doc_id, "没有切出可用片段，检查一下资料内容")
            return None

        chunks = [{"id": "%s-%03d" % (doc_id, p["seq"]), "seq": p["seq"],
                   "text": p["text"], "start": p["start"], "end": p["end"]}
                  for p in pieces]
        vectors: Dict[str, str] = {}
        if self.store is not None and self.embedder is not None:
            arr = self.embedder.embed([c["text"] for c in chunks])
            vectors = {c["id"]: vector_literal(v) for c, v in zip(chunks, arr)}

        now = _now()
        if self.store is not None:
            self.store.replace_doc_chunks(cid, doc_id, chunks, vectors,
                                          text=text, status="completed",
                                          tables=tables)
        else:
            for d in self._mem["documents"]:
                if d["id"] == doc_id and d["collection_id"] == cid:
                    d.update({"text": text, "sha256": sha, "status": "completed",
                              "error": "", "updated_at": now})
                    if tables is not None:
                        d["tables"] = list(tables)
            self._mem["chunks"] = [
                k for k in self._mem["chunks"] if k["document_id"] != doc_id]
            for c in chunks:
                self._mem["chunks"].append({**c, "collection_id": cid,
                                            "document_id": doc_id,
                                            "revision": 0})
            self._save_state()
        self._searchers.pop(cid, None)
        return {"id": doc_id, "title": doc.get("title") or "", "status": "completed",
                "chars": len(text), "chunks": len(chunks)}

    def fail_document(self, cid: str, doc_id: str, error: str) -> bool:
        return self._fail_document(cid, doc_id, error)

    def _fail_document(self, cid: str, doc_id: str, error: str) -> bool:
        if self.store is not None:
            return self.store.set_document_status(cid, doc_id, "failed",
                                                  error=error or "解析失败")
        for d in self._mem["documents"]:
            if d["id"] == doc_id and d["collection_id"] == cid:
                d.update({"status": "failed", "error": error or "解析失败",
                          "updated_at": _now()})
                self._save_state()
                return True
        return False

    # ---------- 片段管理：引用校验的最小单位 ----------

    def document_chunks(self, cid: str, doc_id: str) -> List[Dict]:
        if self.store is not None:
            rows = self.store.doc_chunks(cid, doc_id)
        else:
            rows = [k for k in self._mem["chunks"]
                    if k["collection_id"] == cid and k["document_id"] == doc_id]
            rows.sort(key=lambda k: int(k.get("seq") or 0))
        return [{"id": r["id"], "seq": int(r.get("seq") or 0),
                 "text": r.get("text") or "",
                 "chars": len(r.get("text") or ""),
                 "revision": int(r.get("revision") or 0)} for r in rows]

    def collection_chunks(self, cid: str) -> List[Dict]:
        """整个库的片段一次取回（图谱用，避免一篇一个查询）。"""
        if self.store is not None:
            rows = self.store.doc_chunks(cid)
        else:
            rows = [k for k in self._mem["chunks"] if k["collection_id"] == cid]
        out = []
        for r in rows:
            text = r.get("text") or ""
            out.append({
                "id": r["id"],
                "document_id": r.get("document_id") or "",
                "seq": int(r.get("seq") or 0),
                "text": text,
                "start": int(r.get("start_off", r.get("start", 0)) or 0),
                "end": int(r.get("end_off", r.get("end", 0)) or 0),
            })
        return out

    def update_chunk(self, cid: str, doc_id: str, chunk_id: str, text: str,
                     revision: Optional[int] = None) -> Dict:
        text = (text or "").replace("\r\n", "\n").strip()
        if len(text) < 10:
            raise LibraryError("片段至少要 10 个字，太短的片段没法作为引用依据")
        if len(text) > 4000:
            raise LibraryError("单个片段最多 4000 字，长文请拆成多段")
        emb = None
        if self.embedder is not None:
            emb = vector_literal(self.embedder.embed([text])[0])
        if self.store is not None:
            try:
                row = self.store.update_doc_chunk(cid, doc_id, chunk_id, text, emb,
                                                  revision)
            except RevisionConflict:
                raise LibraryError("这段已被别处改过，点「重新加载」再来")
            if row is None:
                raise LibraryError("片段不存在")
            out = {"id": row["id"], "seq": int(row["seq"]), "text": row["text"],
                   "chars": len(row["text"]),
                   "revision": int(row.get("revision") or 0)}
        else:
            target = None
            for k in self._mem["chunks"]:
                if (k["id"] == chunk_id and k["document_id"] == doc_id
                        and k["collection_id"] == cid):
                    target = k
                    break
            if target is None:
                raise LibraryError("片段不存在")
            cur = int(target.get("revision") or 0)
            if revision is not None and int(revision) != cur:
                raise LibraryError("这段已被别处改过，点「重新加载」再来")
            target.update({"text": text, "revision": cur + 1, "start": 0, "end": 0})
            self._save_state()
            out = {"id": chunk_id, "seq": int(target.get("seq") or 0),
                   "text": text, "chars": len(text), "revision": cur + 1}
        self._searchers.pop(cid, None)
        return out

    def delete_chunk(self, cid: str, doc_id: str, chunk_id: str) -> bool:
        if self.store is not None:
            ok = self.store.delete_doc_chunk(cid, doc_id, chunk_id)
        else:
            before = len(self._mem["chunks"])
            self._mem["chunks"] = [
                k for k in self._mem["chunks"]
                if not (k["id"] == chunk_id and k["document_id"] == doc_id
                        and k["collection_id"] == cid)]
            ok = len(self._mem["chunks"]) != before
            if ok:
                self._save_state()
        self._searchers.pop(cid, None)
        return ok

    def reparse_document(self, cid: str, doc_id: str) -> Optional[Dict]:
        """按已提取的正文重新切分；不重读原文件（原文件不在服务端留着）。"""
        doc = self.get_document(cid, doc_id)
        if doc is None:
            raise LibraryError("资料不存在")
        text = (doc.get("text") or "").strip()
        if len(text) < 20:
            raise LibraryError("这篇资料没有可用的正文，重新切分帮不上忙；"
                               "删掉它重新上传一次")
        return self.finish_document(cid, doc_id, text)

    # ---------- 检索 ----------

    def _chunks(self, cid: str) -> List[Dict]:
        if self.store is not None:
            return self.store.doc_chunks(cid)
        # 键名与 db.store.doc_chunks 对齐，后面的检索代码不用分两种存储
        return [{"id": k["id"], "document_id": k["document_id"], "seq": k["seq"],
                 "text": k["text"],
                 "start_off": int(k.get("start_off", k.get("start", 0))),
                 "end_off": int(k.get("end_off", k.get("end", 0)))}
                for k in self._mem["chunks"] if k["collection_id"] == cid]

    def _doc_tables(self, cid: str) -> Dict[str, List[Dict]]:
        """每篇文档的结构化表格：{文档 id: [表格, …]}。没有表格的文档不出现。"""
        if self.store is not None:
            rows = self.store.doc_tables(cid)
        else:
            rows = [{"id": d["id"], "tables": d.get("tables") or []}
                    for d in self._mem["documents"]
                    if d["collection_id"] == cid
                    and d.get("status") == "completed"]
        return {r["id"]: r["tables"] for r in rows if r.get("tables")}

    def searcher(self, cid: str) -> Optional[HybridSearcher]:
        """按库惰性建检索器；库变了就重建。"""
        rows = self._chunks(cid)
        if not rows:
            return None
        version = (len(rows), rows[-1]["id"])
        cached = self._searchers.get(cid)
        if cached and cached[0] == version:
            return cached[1]

        coll = self.get_collection(cid) or {"name": cid}
        # 片段要带上所在文档的标题：内存后备存储的片段行里没有标题，
        # 统一从文档表补，两种存储的行为才一致。
        titles = {d["id"]: d["title"] for d in self.list_documents(cid)}
        chunks = [Chunk(
            id=r["id"],
            seq=int(r["seq"]),
            text=r["text"],
            kind="doc",
            title=r.get("title") or titles.get(r["document_id"], ""),
            subject=coll.get("name") or "",
            source_ref={"document_id": r["document_id"],
                        "document_title": r.get("title")
                        or titles.get(r["document_id"], ""),
                        "start": int(r.get("start_off") or 0),
                        "end": int(r.get("end_off") or 0)},
        ) for r in rows]

        vector_search = None
        if self.store is not None and self.embedder is not None:
            vector_search = lambda vec, k: self.store.vector_search_docs(vec, k, cid)

        reranker = None
        texts = [c.text for c in chunks]
        if self.mode == "hybrid-ce":
            reranker = CrossEncoderReranker(texts, model_name=self.reranker_model)
        elif self.mode == "hybrid-rerank":
            reranker = LexicalReranker(texts)

        searcher = build_searcher(
            chunks, mode=self.mode, k_fuse=self.k_fuse, k_final=5,
            embedder=self.embedder, vector_search=vector_search,
            reranker=reranker)
        # 结构化表格跟着检索器走：统计类问题不用再从正文里反推行列
        searcher.doc_tables = self._doc_tables(cid)
        self._searchers[cid] = (version, searcher)
        return searcher

    def stats(self) -> Dict:
        colls = self.list_collections()
        return {"collections": len(colls),
                "documents": sum(c["documents"] for c in colls),
                "chunks": sum(c["chunks"] for c in colls)}
