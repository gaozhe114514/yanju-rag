# -*- coding: utf-8 -*-
"""PostgreSQL 存取：语料同步、向量召回、浏览接口、拒答日志。

依赖 psycopg3。为了在没有数据库的机器上也能跑单元测试，
psycopg 只在真正建连接时才导入（见 `_psycopg`）。
"""
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _psycopg():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg, dict_row


class RevisionConflict(ValueError):
    """片段被别处改过：调用方回 409，不要拿旧内容覆盖新的。"""

    def __init__(self, current: int = 0):
        super().__init__("这段已被别处改过，重新加载后再改")
        self.current = int(current or 0)


class DuplicateUser(ValueError):
    """用户名已被占用。账号层把它翻成「这个用户名已经被注册了」。"""


def vector_literal(vec: Sequence[float]) -> str:
    """把 numpy/list 向量转成 pgvector 的文本形式。"""
    return "[" + ",".join("%.7f" % float(x) for x in vec) + "]"


class Store:
    def __init__(self, dsn: str, dim: int = 512):
        self.dsn = dsn
        self.dim = int(dim)

    @classmethod
    def from_env(cls) -> Optional["Store"]:
        dsn = os.environ.get("DATABASE_URL", "").strip()
        if not dsn:
            return None
        return cls(dsn, int(os.environ.get("KB_EMBED_DIM", "512")))

    @contextmanager
    def connect(self):
        psycopg, dict_row = _psycopg()
        conn = psycopg.connect(self.dsn, row_factory=dict_row)
        try:
            yield conn
        finally:
            conn.close()

    # ---------- schema ----------

    def init_schema(self) -> None:
        # 不用 str.format：schema 里有 text[] DEFAULT '{}'，会被当成占位符
        sql = SCHEMA_PATH.read_text(encoding="utf-8").replace(
            "{dim}", str(self.dim))
        with self.connect() as conn:
            for stmt in (s.strip() for s in sql.split(";")):
                if stmt:
                    conn.execute(stmt)
            conn.commit()

    def ping(self) -> None:
        with self.connect() as conn:
            conn.execute("SELECT 1")

    # ---------- 用户知识库 ----------

    def create_collection(self, cid: str, name: str, description: str = "",
                          owner_id: Optional[str] = None,
                          visibility: str = "shared",
                          created_by: Optional[str] = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO collections (id, name, description, owner_id,"
                " visibility, created_by) VALUES (%s, %s, %s, %s, %s, %s)",
                (cid, name, description, owner_id or None, visibility,
                 created_by or None))
            conn.commit()

    def list_collections(self) -> List[Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT c.id, c.name, c.description, c.owner_id,"
                " c.created_by, c.visibility, c.created_at, c.updated_at,"
                " c.chunk_target, c.chunk_overlap,"
                " (SELECT count(*)::int FROM documents d"
                "   WHERE d.collection_id = c.id) AS doc_count,"
                " (SELECT count(*)::int FROM doc_chunks k"
                "   WHERE k.collection_id = c.id) AS chunk_count"
                " FROM collections c ORDER BY c.created_at").fetchall()
        return [dict(r) for r in rows]

    def get_collection(self, cid: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, name, description, owner_id, created_by, visibility,"
                " chunk_target, chunk_overlap, created_at, updated_at"
                " FROM collections WHERE id = %s",
                (cid,)).fetchone()
        return dict(row) if row else None

    def update_collection(self, cid: str, name: Optional[str] = None,
                          description: Optional[str] = None,
                          chunk_target: Optional[int] = None,
                          chunk_overlap: Optional[int] = None) -> bool:
        sets = ["updated_at = now()"]
        args = []
        if name is not None:
            sets.append("name = %s")
            args.append(name)
        if description is not None:
            sets.append("description = %s")
            args.append(description)
        if chunk_target is not None:
            sets.append("chunk_target = %s")
            args.append(int(chunk_target))
        if chunk_overlap is not None:
            sets.append("chunk_overlap = %s")
            args.append(int(chunk_overlap))
        args.append(cid)
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE collections SET " + ", ".join(sets) +
                " WHERE id = %s", tuple(args))
            conn.commit()
            return cur.rowcount > 0

    # ---------- 文件夹树 / 操作日志 / 进料任务（v0.83） ----------

    def list_folders(self, cid: str) -> List[Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT path, created_at FROM collection_folders"
                " WHERE collection_id = %s ORDER BY path", (cid,)).fetchall()
        return [dict(r) for r in rows]

    def add_folder(self, cid: str, path: str) -> None:
        """建文件夹，同时补齐上级路径，树里不会出现断层的空节点。"""
        parts = [p for p in path.split("/") if p]
        for i in range(1, len(parts) + 1):
            p = "/".join(parts[:i])
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO collection_folders (collection_id, path)"
                    " VALUES (%s, %s) ON CONFLICT DO NOTHING", (cid, p))
                conn.commit()

    def rename_folder(self, cid: str, old: str, new: str) -> int:
        """改名或移动整棵子树；返回受影响的文件夹行数。"""
        old = old.strip("/")
        new = new.strip("/")
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT path FROM collection_folders"
                " WHERE collection_id = %s AND (path = %s OR path LIKE %s)",
                (cid, old, old + "/%")).fetchall()
            paths = [r["path"] for r in rows]
            for p in paths:
                moved = new + p[len(old):]
                conn.execute(
                    "UPDATE collection_folders SET path = %s"
                    " WHERE collection_id = %s AND path = %s", (moved, cid, p))
                conn.execute(
                    "UPDATE documents SET folder = %s, updated_at = now()"
                    " WHERE collection_id = %s AND (folder = %s OR folder LIKE %s)",
                    (moved, cid, p, p + "/%"))
            # 新路径的上级也补上
            parts = [x for x in new.split("/") if x]
            for i in range(1, len(parts) + 1):
                conn.execute(
                    "INSERT INTO collection_folders (collection_id, path)"
                    " VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (cid, "/".join(parts[:i])))
            conn.commit()
            return len(paths)

    def delete_folder(self, cid: str, path: str) -> int:
        """删文件夹：整棵子树里的资料移回上一级，文件夹行删除。"""
        path = path.strip("/")
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        with self.connect() as conn:
            conn.execute(
                "UPDATE documents SET folder = %s, updated_at = now()"
                " WHERE collection_id = %s AND (folder = %s OR folder LIKE %s)",
                (parent, cid, path, path + "/%"))
            cur = conn.execute(
                "DELETE FROM collection_folders"
                " WHERE collection_id = %s AND (path = %s OR path LIKE %s)",
                (cid, path, path + "/%"))
            conn.commit()
            return int(cur.rowcount or 0)

    # ---------- 自动 Wiki（v0.84） ----------

    def list_wiki_pages(self, cid: str) -> List[Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, slug, title, summary, body, kind, aliases,"
                " source_doc_ids, source_chunk_ids, generated, edited, revision,"
                " created_at, updated_at FROM wiki_pages"
                " WHERE collection_id = %s ORDER BY title", (cid,)).fetchall()
        return [dict(r) for r in rows]

    def get_wiki_page(self, cid: str, page_id: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, collection_id, slug, title, summary, body, kind,"
                " aliases, source_doc_ids, source_chunk_ids, generated, edited,"
                " revision, created_at, updated_at FROM wiki_pages"
                " WHERE collection_id = %s AND id = %s",
                (cid, page_id)).fetchone()
        return dict(row) if row else None

    def wiki_page_by_slug(self, cid: str, slug: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, title FROM wiki_pages"
                " WHERE collection_id = %s AND slug = %s", (cid, slug)).fetchone()
        return dict(row) if row else None

    def replace_wiki_pages(self, cid: str, pages: Sequence[Dict],
                           links: Sequence[Dict]) -> None:
        """用新生成的一批页面替换掉未编辑过的旧页面；手动编辑的保留。"""
        slugs = [p["slug"] for p in pages if p.get("slug")]
        with self.connect() as conn:
            if slugs:
                conn.execute(
                    "DELETE FROM wiki_pages WHERE collection_id = %s"
                    " AND edited = false AND NOT (slug = ANY(%s))",
                    (cid, slugs))
            else:
                conn.execute(
                    "DELETE FROM wiki_pages WHERE collection_id = %s"
                    " AND edited = false", (cid,))
            for p in pages:
                existing = conn.execute(
                    "SELECT id, edited FROM wiki_pages"
                    " WHERE collection_id = %s AND slug = %s",
                    (cid, p["slug"])).fetchone()
                if existing is not None:
                    if existing["edited"]:
                        continue
                    conn.execute(
                        "UPDATE wiki_pages SET title = %s, summary = %s,"
                        " body = %s, kind = %s, aliases = %s,"
                        " source_doc_ids = %s, source_chunk_ids = %s,"
                        " generated = true, updated_at = now()"
                        " WHERE collection_id = %s AND id = %s"
                        " AND edited = false",
                        (p["title"], p.get("summary") or "", p.get("body") or "",
                         p.get("kind") or "概念", list(p.get("aliases") or []),
                         list(p.get("source_doc_ids") or []),
                         list(p.get("source_chunk_ids") or []),
                         cid, existing["id"]))
                    continue
                conn.execute(
                    "INSERT INTO wiki_pages (id, collection_id, slug, title,"
                    " summary, body, kind, aliases, source_doc_ids,"
                    " source_chunk_ids, generated, edited, revision)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, false, 0)",
                    (p["id"], cid, p["slug"], p["title"], p.get("summary") or "",
                     p.get("body") or "", p.get("kind") or "概念",
                     list(p.get("aliases") or []),
                     list(p.get("source_doc_ids") or []),
                     list(p.get("source_chunk_ids") or [])))
            self._replace_wiki_links(conn, cid, links)
            conn.commit()

    def _replace_wiki_links(self, conn, cid: str,
                            links: Sequence[Dict]) -> None:
        conn.execute("DELETE FROM wiki_links WHERE collection_id = %s", (cid,))
        for e in links:
            a, b = e.get("from") or "", e.get("to") or ""
            if not a or not b or a == b:
                continue
            conn.execute(
                "INSERT INTO wiki_links (collection_id, from_page, to_page,"
                " kind, weight) VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT DO NOTHING",
                (cid, a, b, e.get("kind") or "related", int(e.get("weight") or 1)))

    def replace_wiki_entities(self, cid: str,
                              entities: Sequence[Dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM wiki_entities WHERE collection_id = %s",
                         (cid,))
            for e in entities:
                conn.execute(
                    "INSERT INTO wiki_entities (id, collection_id, name, kind,"
                    " summary, aliases, source_doc_ids, source_chunk_ids)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (collection_id, name) DO UPDATE SET"
                    " kind = EXCLUDED.kind, summary = EXCLUDED.summary,"
                    " aliases = EXCLUDED.aliases,"
                    " source_doc_ids = EXCLUDED.source_doc_ids,"
                    " source_chunk_ids = EXCLUDED.source_chunk_ids,"
                    " updated_at = now()",
                    (e["id"], cid, e["name"], e.get("kind") or "概念",
                     e.get("summary") or "", list(e.get("aliases") or []),
                     list(e.get("source_doc_ids") or []),
                     list(e.get("source_chunk_ids") or [])))
            conn.commit()

    def list_wiki_entities(self, cid: str) -> List[Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, name, kind, summary, aliases, source_doc_ids,"
                " source_chunk_ids, created_at, updated_at FROM wiki_entities"
                " WHERE collection_id = %s ORDER BY kind, name", (cid,)).fetchall()
        return [dict(r) for r in rows]

    def get_wiki_entity(self, cid: str, entity_id: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, collection_id, name, kind, summary, aliases,"
                " source_doc_ids, source_chunk_ids, created_at, updated_at"
                " FROM wiki_entities WHERE collection_id = %s AND id = %s",
                (cid, entity_id)).fetchone()
        return dict(row) if row else None

    def replace_wiki_links(self, cid: str, links: Sequence[Dict]) -> None:
        with self.connect() as conn:
            self._replace_wiki_links(conn, cid, links)
            conn.commit()

    def list_wiki_links(self, cid: str) -> List[Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT from_page, to_page, kind, weight FROM wiki_links"
                " WHERE collection_id = %s", (cid,)).fetchall()
        return [dict(r) for r in rows]

    def update_wiki_page(self, cid: str, page_id: str, title: str,
                         summary: str, body: str, editor: str = "",
                         kind: Optional[str] = None) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, title, summary, body FROM wiki_pages"
                " WHERE collection_id = %s AND id = %s",
                (cid, page_id)).fetchone()
            if row is None:
                return None
            conn.execute(
                "INSERT INTO wiki_page_versions (page_id, collection_id, title,"
                " summary, body, editor) VALUES (%s, %s, %s, %s, %s, %s)",
                (page_id, cid, row["title"], row["summary"], row["body"],
                 editor or ""))
            conn.execute(
                "UPDATE wiki_pages SET title = %s, summary = %s, body = %s,"
                " kind = COALESCE(%s, kind), edited = true,"
                " revision = revision + 1, updated_at = now()"
                " WHERE collection_id = %s AND id = %s",
                (title, summary, body, kind, cid, page_id))
            conn.commit()
        return self.get_wiki_page(cid, page_id)

    def list_wiki_versions(self, cid: str, page_id: str,
                           limit: int = 20) -> List[Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, title, summary, body, editor, created_at"
                " FROM wiki_page_versions WHERE page_id = %s"
                " AND collection_id = %s ORDER BY id DESC LIMIT %s",
                (page_id, cid, max(1, min(int(limit), 100)))).fetchall()
        return [dict(r) for r in rows]

    def restore_wiki_version(self, cid: str, page_id: str, version_id: int,
                             editor: str = "") -> Optional[Dict]:
        with self.connect() as conn:
            old = conn.execute(
                "SELECT title, summary, body FROM wiki_page_versions"
                " WHERE id = %s AND page_id = %s AND collection_id = %s",
                (int(version_id), page_id, cid)).fetchone()
            if old is None:
                return None
            cur = conn.execute(
                "SELECT title, summary, body FROM wiki_pages"
                " WHERE collection_id = %s AND id = %s",
                (cid, page_id)).fetchone()
            if cur is None:
                return None
            conn.execute(
                "INSERT INTO wiki_page_versions (page_id, collection_id, title,"
                " summary, body, editor) VALUES (%s, %s, %s, %s, %s, %s)",
                (page_id, cid, cur["title"], cur["summary"], cur["body"],
                 editor or ""))
            conn.execute(
                "UPDATE wiki_pages SET title = %s, summary = %s, body = %s,"
                " edited = true, revision = revision + 1, updated_at = now()"
                " WHERE collection_id = %s AND id = %s",
                (old["title"], old["summary"], old["body"], cid, page_id))
            conn.commit()
        return self.get_wiki_page(cid, page_id)

    def delete_wiki_page(self, cid: str, page_id: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM wiki_pages WHERE collection_id = %s AND id = %s",
                (cid, page_id))
            conn.commit()
            return cur.rowcount > 0

    def add_audit(self, cid: str, actor_id: str = "", actor_name: str = "",
                  action: str = "", target_type: str = "", target_id: str = "",
                  detail: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO kb_audit_logs (collection_id, actor_id, actor_name,"
                " action, target_type, target_id, detail)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (cid, actor_id or "", actor_name or "", action or "",
                 target_type or "", target_id or "", (detail or "")[:500]))
            conn.commit()

    def list_audit(self, cid: str, limit: int = 50) -> List[Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at, actor_id, actor_name, action,"
                " target_type, target_id, detail FROM kb_audit_logs"
                " WHERE collection_id = %s ORDER BY created_at DESC, id DESC"
                " LIMIT %s", (cid, max(1, min(int(limit), 200)))).fetchall()
        return [dict(r) for r in rows]

    def save_document_file(self, doc_id: str, cid: str, filename: str,
                           mime: str, data: bytes) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO document_files"
                " (document_id, filename, mime, nbytes, data)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (document_id) DO UPDATE SET"
                " filename = EXCLUDED.filename, mime = EXCLUDED.mime,"
                " nbytes = EXCLUDED.nbytes, data = EXCLUDED.data",
                (doc_id, filename or "", mime or "", len(data), data))
            conn.commit()

    def get_document_file(self, doc_id: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document_id, filename, mime, nbytes, data"
                " FROM document_files WHERE document_id = %s",
                (doc_id,)).fetchone()
        return dict(row) if row else None

    def upsert_ingest_job(self, doc_id: str, cid: str, filename: str = "",
                          status: str = "queued") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO ingest_jobs (document_id, collection_id, filename,"
                " status) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (document_id) DO UPDATE SET"
                " filename = EXCLUDED.filename, status = EXCLUDED.status,"
                " error = '', updated_at = now()",
                (doc_id, cid, filename or "", status))
            conn.commit()

    def set_ingest_job(self, doc_id: str, status: str, error: str = "",
                       bump_attempt: bool = False) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE ingest_jobs SET status = %s, error = %s,"
                " attempts = attempts + %s, updated_at = now()"
                " WHERE document_id = %s",
                (status, (error or "")[:500], 1 if bump_attempt else 0, doc_id))
            conn.commit()

    def list_pending_ingest_jobs(self) -> List[Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT document_id, collection_id, filename, status, attempts"
                " FROM ingest_jobs WHERE status IN ('queued', 'running')"
                " ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    # ---------- 知识库成员 ----------

    def get_collection_member(self, cid: str, user_id: str) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT role FROM collection_members"
                " WHERE collection_id = %s AND user_id = %s",
                (cid, user_id)).fetchone()
        return str(row["role"]) if row else None

    def list_collection_members(self, cid: str) -> List[Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT m.user_id, m.role, m.created_at, u.username"
                " FROM collection_members m JOIN users u ON u.id = m.user_id"
                " WHERE m.collection_id = %s ORDER BY m.created_at, u.username",
                (cid,)).fetchall()
        return [dict(r) for r in rows]

    def set_collection_member(self, cid: str, user_id: str, role: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO collection_members (collection_id, user_id, role)"
                " VALUES (%s, %s, %s)"
                " ON CONFLICT (collection_id, user_id)"
                " DO UPDATE SET role = EXCLUDED.role",
                (cid, user_id, role))
            conn.commit()

    def remove_collection_member(self, cid: str, user_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "DELETE FROM collection_members"
                " WHERE collection_id = %s AND user_id = %s",
                (cid, user_id))
            conn.commit()
            return row.rowcount > 0

    def delete_collection(self, cid: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM collections WHERE id = %s", (cid,))
            conn.commit()
            return cur.rowcount > 0

    def touch_collection(self, cid: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE collections SET updated_at = now() WHERE id = %s", (cid,))
            conn.commit()

    def add_document(self, doc_id: str, cid: str, title: str, text: str,
                     sha: str, chunks: Sequence[Dict],
                     vectors: Optional[Dict[str, str]] = None,
                     status: str = "completed", error: str = "",
                     source: str = "", nbytes: int = 0,
                     tables: Optional[Sequence[Dict]] = None,
                     folder: str = "", tags: Optional[Sequence[str]] = None) -> None:
        """写入一篇文档及其片段；vectors 是 {chunk_id: pgvector 文本}。

        tables 是进料时抽出的结构化表格；粘贴正文进来的文档没有，存 '[]'。
        """
        vectors = vectors or {}
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO documents (id, collection_id, title, text, sha256,"
                " status, error, bytes, source, tables, folder, tags)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                (doc_id, cid, title, text, sha, status, error, nbytes, source,
                 json.dumps(list(tables or []), ensure_ascii=False),
                 folder or "", list(tags or [])))
            for c in chunks:
                conn.execute(
                    "INSERT INTO doc_chunks (id, collection_id, document_id, seq,"
                    " text, start_off, end_off, embedding)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
                    (c["id"], cid, doc_id, c["seq"], c["text"],
                     c["start"], c["end"], vectors.get(c["id"])))
            conn.execute(
                "UPDATE collections SET updated_at = now() WHERE id = %s", (cid,))
            conn.commit()

    def list_documents(self, cid: str) -> List[Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT d.id, d.title, d.sha256, d.created_at, d.updated_at,"
                " d.status, d.error, d.bytes, d.source, d.folder, d.tags,"
                " length(d.text) AS chars,"
                " (SELECT count(*)::int FROM doc_chunks k"
                "   WHERE k.document_id = d.id) AS chunk_count"
                " FROM documents d WHERE d.collection_id = %s"
                " ORDER BY d.created_at", (cid,)).fetchall()
        return [dict(r) for r in rows]

    def set_document_folder(self, cid: str, doc_id: str, folder: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE documents SET folder = %s, updated_at = now()"
                " WHERE collection_id = %s AND id = %s",
                (folder or "", cid, doc_id))
            conn.commit()
            return cur.rowcount > 0

    def get_document(self, cid: str, doc_id: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, collection_id, title, text, sha256, created_at,"
                " updated_at, status, error, bytes, source, tables, folder, tags"
                " FROM documents WHERE collection_id = %s AND id = %s",
                (cid, doc_id)).fetchone()
        return dict(row) if row else None

    def doc_tables(self, cid: str) -> List[Dict]:
        """整个资料库里每篇文档的结构化表格：{文档 id: [表格, …]}。

        检索器建好时一并带上，统计类问题直接用，不用再从正文里反推。
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, tables FROM documents"
                " WHERE collection_id = %s AND status = 'completed'",
                (cid,)).fetchall()
        return [{"id": r["id"], "tables": r["tables"] or []} for r in rows]

    def set_document_status(self, cid: str, doc_id: str, status: str,
                            error: str = "", text: Optional[str] = None,
                            nbytes: Optional[int] = None) -> bool:
        """只改状态（解析线程用）；有 text 时一并写回提取到的正文。"""
        sets = ["status = %s", "error = %s", "updated_at = now()"]
        args = [status, error]
        if text is not None:
            sets.append("text = %s")
            args.append(text)
        if nbytes is not None:
            sets.append("bytes = %s")
            args.append(int(nbytes))
        args.extend([cid, doc_id])
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE documents SET " + ", ".join(sets) +
                " WHERE collection_id = %s AND id = %s", tuple(args))
            conn.commit()
            return cur.rowcount > 0

    def delete_document(self, cid: str, doc_id: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM documents WHERE collection_id = %s AND id = %s",
                (cid, doc_id))
            conn.commit()
            if cur.rowcount:
                conn.execute(
                    "UPDATE collections SET updated_at = now()"
                    " WHERE id = %s", (cid,))
                conn.commit()
            return cur.rowcount > 0

    def doc_chunks(self, cid: str, document_id: Optional[str] = None) -> List[Dict]:
        sql = ("SELECT k.id, k.document_id, k.seq, k.text, k.start_off, k.end_off,"
               " k.revision, d.title"
               " FROM doc_chunks k JOIN documents d ON d.id = k.document_id"
               " WHERE k.collection_id = %s")
        args: list = [cid]
        if document_id:
            sql += " AND k.document_id = %s"
            args.append(document_id)
        sql += " ORDER BY k.document_id, k.seq"
        with self.connect() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def get_doc_chunk(self, cid: str, doc_id: str, chunk_id: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, document_id, seq, text, start_off, end_off, revision"
                " FROM doc_chunks WHERE collection_id = %s AND document_id = %s"
                " AND id = %s", (cid, doc_id, chunk_id)).fetchone()
        return dict(row) if row else None

    def update_doc_chunk(self, cid: str, doc_id: str, chunk_id: str, text: str,
                         embedding: Optional[str] = None,
                         revision: Optional[int] = None) -> Optional[Dict]:
        """改一段文字并重建它的向量；带 revision 时做乐观锁。

        返回更新后的行；revision 对不上返回 None（调用方回 409）。
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT revision FROM doc_chunks WHERE collection_id = %s"
                " AND document_id = %s AND id = %s",
                (cid, doc_id, chunk_id)).fetchone()
            if row is None:
                return None
            current = int(row["revision"] or 0)
            if revision is not None and int(revision) != current:
                raise _RevisionConflict(current)
            conn.execute(
                "UPDATE doc_chunks SET text = %s, embedding = %s::vector,"
                " revision = revision + 1 WHERE id = %s",
                (text, embedding, chunk_id))
            conn.execute(
                "UPDATE documents SET updated_at = now() WHERE id = %s", (doc_id,))
            conn.commit()
            out = conn.execute(
                "SELECT id, document_id, seq, text, start_off, end_off, revision"
                " FROM doc_chunks WHERE id = %s", (chunk_id,)).fetchone()
        return dict(out) if out else None

    def delete_doc_chunk(self, cid: str, doc_id: str, chunk_id: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM doc_chunks WHERE collection_id = %s"
                " AND document_id = %s AND id = %s", (cid, doc_id, chunk_id))
            conn.commit()
            return cur.rowcount > 0

    def replace_doc_chunks(self, cid: str, doc_id: str, chunks: Sequence[Dict],
                           vectors: Optional[Dict[str, str]] = None,
                           text: Optional[str] = None,
                           status: str = "completed", error: str = "",
                           tables: Optional[Sequence[Dict]] = None) -> None:
        """重新切分：整篇片段换掉、正文与状态一起更新，一步提交。

        tables 只在重新解析原始文件时传；从已有正文重切分时保持原值不动。
        """
        vectors = vectors or {}
        with self.connect() as conn:
            conn.execute("DELETE FROM doc_chunks WHERE document_id = %s", (doc_id,))
            for c in chunks:
                conn.execute(
                    "INSERT INTO doc_chunks (id, collection_id, document_id, seq,"
                    " text, start_off, end_off, embedding)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
                    (c["id"], cid, doc_id, c["seq"], c["text"],
                     c["start"], c["end"], vectors.get(c["id"])))
            sets = ["status = %s", "error = %s", "updated_at = now()"]
            args = [status, error]
            if text is not None:
                sets.append("text = %s")
                args.append(text)
            if tables is not None:
                sets.append("tables = %s::jsonb")
                args.append(json.dumps(list(tables), ensure_ascii=False))
            args.extend([cid, doc_id])
            conn.execute("UPDATE documents SET " + ", ".join(sets) +
                         " WHERE collection_id = %s AND id = %s", tuple(args))
            conn.execute(
                "UPDATE collections SET updated_at = now() WHERE id = %s", (cid,))
            conn.commit()

    def update_document(self, cid: str, doc_id: str, title: str, text: str,
                        sha: str, chunks: Sequence[Dict],
                        vectors: Optional[Dict[str, str]] = None,
                        author: str = "", keep_versions: int = 20) -> Optional[Dict]:
        """整篇编辑：旧版进历史、正文/标题/摘要更新、片段整批替换。

        一次事务提交，检索器看到的要么是旧版，要么是新版。手工编辑后
        导入时抽出的结构化表格不再可信，一并清空，统计改按新正文推断。
        """
        vectors = vectors or {}
        with self.connect() as conn:
            snap = conn.execute(
                "INSERT INTO document_versions"
                " (collection_id, document_id, title, text, sha256, author)"
                " SELECT collection_id, id, title, text, sha256, %s"
                " FROM documents WHERE collection_id = %s AND id = %s"
                " RETURNING id",
                (author, cid, doc_id)).fetchone()
            if snap is None:
                return None
            version_id = int(snap["id"])
            conn.execute("DELETE FROM doc_chunks WHERE document_id = %s",
                         (doc_id,))
            for c in chunks:
                conn.execute(
                    "INSERT INTO doc_chunks (id, collection_id, document_id, seq,"
                    " text, start_off, end_off, embedding)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
                    (c["id"], cid, doc_id, c["seq"], c["text"],
                     c["start"], c["end"], vectors.get(c["id"])))
            conn.execute(
                "UPDATE documents SET title = %s, text = %s, sha256 = %s,"
                " status = 'completed', error = '', tables = '[]'::jsonb,"
                " updated_at = now()"
                " WHERE collection_id = %s AND id = %s",
                (title, text, sha, cid, doc_id))
            conn.execute(
                "DELETE FROM document_versions WHERE document_id = %s"
                " AND id NOT IN (SELECT id FROM document_versions"
                " WHERE document_id = %s ORDER BY id DESC LIMIT %s)",
                (doc_id, doc_id, int(keep_versions)))
            conn.execute(
                "UPDATE collections SET updated_at = now() WHERE id = %s", (cid,))
            conn.commit()
        return {"id": doc_id, "version_id": version_id}

    def list_document_versions(self, cid: str, doc_id: str,
                               limit: int = 20) -> List[Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, title, sha256, author, created_at,"
                " length(text) AS chars"
                " FROM document_versions"
                " WHERE collection_id = %s AND document_id = %s"
                " ORDER BY id DESC LIMIT %s",
                (cid, doc_id, int(limit))).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": int(r["id"]),
                "title": r["title"] or "",
                "sha256": r["sha256"] or "",
                "author": r["author"] or "",
                "created_at": str(r["created_at"] or ""),
                "chars": int(r["chars"] or 0),
            })
        return out

    def get_document_version(self, cid: str, doc_id: str,
                             version_id: int) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, title, text, sha256, author, created_at"
                " FROM document_versions"
                " WHERE collection_id = %s AND document_id = %s AND id = %s",
                (cid, doc_id, int(version_id))).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["id"] = int(out["id"])
        out["created_at"] = str(out.get("created_at") or "")
        return out

    # ---------- 资料附件：插图与非图片文件 ----------

    def add_document_asset(self, asset_id: str, cid: str, doc_id: str,
                           filename: str, mime: str, data: bytes) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "INSERT INTO document_assets"
                " (id, collection_id, document_id, filename, mime, nbytes, data)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " RETURNING id, filename, mime, nbytes, created_at",
                (asset_id, cid, doc_id, filename, mime, len(data), data)
            ).fetchone()
            conn.commit()
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["filename"],
            "mime": row["mime"] or "",
            "bytes": int(row["nbytes"] or 0),
            "created_at": str(row["created_at"] or ""),
        }

    def list_document_assets(self, cid: str, doc_id: str) -> List[Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, filename, mime, nbytes, created_at"
                " FROM document_assets"
                " WHERE collection_id = %s AND document_id = %s"
                " ORDER BY created_at, id",
                (cid, doc_id)).fetchall()
        return [{
            "id": r["id"],
            "name": r["filename"],
            "mime": r["mime"] or "",
            "bytes": int(r["nbytes"] or 0),
            "created_at": str(r["created_at"] or ""),
        } for r in rows]

    def get_document_asset(self, cid: str, doc_id: str,
                           asset_id: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, filename, mime, nbytes, data, created_at"
                " FROM document_assets"
                " WHERE collection_id = %s AND document_id = %s AND id = %s",
                (cid, doc_id, asset_id)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["filename"],
            "mime": row["mime"] or "",
            "bytes": int(row["nbytes"] or 0),
            "data": bytes(row["data"] or b""),
            "created_at": str(row["created_at"] or ""),
        }

    def delete_document_asset(self, cid: str, doc_id: str,
                              asset_id: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM document_assets"
                " WHERE collection_id = %s AND document_id = %s AND id = %s",
                (cid, doc_id, asset_id))
            conn.commit()
            return cur.rowcount > 0

    def vector_search_docs(self, query_vec: Sequence[float], top_k: int,
                           cid: str) -> List[Tuple[str, float]]:
        lit = vector_literal(query_vec)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, 1 - (embedding <=> %s::vector) AS score"
                " FROM doc_chunks WHERE collection_id = %s"
                " AND embedding IS NOT NULL"
                " ORDER BY embedding <=> %s::vector LIMIT %s",
                (lit, cid, lit, int(top_k))).fetchall()
        return [(r["id"], float(r["score"])) for r in rows]

    # ---------- 应答缓存 ----------

    def get_answer_cache(self, cache_key: str, ttl_s: int) -> Optional[Dict]:
        """命中返回 payload 与年龄；过期视为未命中，由后续写入顺带清理。"""
        if ttl_s <= 0:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload,"
                " EXTRACT(EPOCH FROM (now() - created_at)) AS age_s"
                " FROM answer_cache WHERE cache_key = %s",
                (cache_key,)).fetchone()
        if row is None:
            return None
        age_s = float(row["age_s"] or 0)
        if age_s > ttl_s:
            return None
        return {"payload": row["payload"], "age_s": age_s}

    def put_answer_cache(self, cache_key: str, query: str, payload: Dict,
                         ttl_s: int) -> None:
        if ttl_s <= 0:
            return
        body = json.dumps(payload, ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM answer_cache"
                " WHERE created_at < now() - (%s::double precision"
                " * interval '1 second')",
                (float(ttl_s),))
            conn.execute(
                "INSERT INTO answer_cache (cache_key, query, payload, created_at)"
                " VALUES (%s, %s, %s::jsonb, now())"
                " ON CONFLICT (cache_key) DO UPDATE SET"
                " query = EXCLUDED.query, payload = EXCLUDED.payload,"
                " created_at = now()",
                (cache_key, query, body))
            conn.commit()

    # ---------- 回答反馈 ----------

    def log_feedback(self, rec: Dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO answer_feedback (query, answer, helpful, reason,"
                " note, citations, cached)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (rec["query"], rec["answer"], bool(rec["helpful"]),
                 rec["reason"], rec["note"], list(rec["citations"]),
                 bool(rec["cached"])))
            conn.commit()

    def feedback_stats(self, days: int = 30, limit: int = 10) -> Dict:
        """有用率、差评原因和最近差评；正反馈只计数，不占列表。"""
        days = max(1, min(int(days), 365))
        limit = max(1, min(int(limit), 100))
        window = "created_at >= now() - (%s::int * interval '1 day')"
        with self.connect() as conn:
            row = conn.execute(
                "SELECT count(*)::int AS total,"
                " count(*) FILTER (WHERE helpful)::int AS helpful"
                " FROM answer_feedback WHERE " + window, (days,)).fetchone()
            reasons = conn.execute(
                "SELECT reason, count(*)::int AS n FROM answer_feedback"
                " WHERE helpful = false AND " + window +
                " GROUP BY reason ORDER BY n DESC, reason",
                (days,)).fetchall()
            recent = conn.execute(
                "SELECT created_at, query, answer, reason, note, citations, cached"
                " FROM answer_feedback WHERE helpful = false AND " + window +
                " ORDER BY created_at DESC, id DESC LIMIT %s",
                (days, limit)).fetchall()
        total, helpful = int(row["total"]), int(row["helpful"])
        return {
            "storage": "postgres",
            "window_days": days,
            "total": total,
            "helpful": helpful,
            "not_helpful": total - helpful,
            "helpful_rate": round(helpful / total, 4) if total else 0.0,
            "by_reason": [{"reason": r["reason"], "count": r["n"]}
                          for r in reasons],
            "recent": [{
                "ts": r["created_at"].isoformat(timespec="seconds"),
                "query": r["query"],
                "reason": r["reason"],
                "note": r["note"],
                "answer": r["answer"],
                "citations": list(r["citations"] or []),
                "cached": bool(r["cached"]),
            } for r in recent],
        }

    # ---------- 拒答日志 ----------

    def log_gap(self, query: str, reason: str,
                top_chunk: Optional[str] = None,
                top_score: Optional[float] = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO gap_logs (query, reason, top_chunk, top_score)"
                " VALUES (%s, %s, %s, %s)",
                (query, reason or "", top_chunk,
                 None if top_score is None else float(top_score)))
            conn.commit()

    def gap_stats(self, days: int = 30, limit: int = 20) -> Dict:
        """按原因、按天聚合拒答日志，并返回最近若干条。"""
        days = max(1, min(int(days), 365))
        limit = max(1, min(int(limit), 100))
        # 显式 ::int，避免 psycopg 把 Python int 当 int8 传给 make_interval 时
        # 找不到匹配的函数签名。
        window = "created_at >= now() - (%s::int * interval '1 day')"
        with self.connect() as conn:
            total = conn.execute(
                "SELECT count(*)::int AS n FROM gap_logs WHERE " + window,
                (days,)).fetchone()["n"]
            reasons = conn.execute(
                "SELECT COALESCE(NULLIF(btrim(split_part(reason, ':', 1)), ''),"
                " 'unknown') AS reason, count(*)::int AS n"
                " FROM gap_logs WHERE " + window +
                " GROUP BY 1 ORDER BY n DESC, reason",
                (days,)).fetchall()
            by_day = conn.execute(
                "SELECT to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD')"
                " AS day, count(*)::int AS n"
                " FROM gap_logs WHERE " + window +
                " GROUP BY 1 ORDER BY 1",
                (days,)).fetchall()
            recent = conn.execute(
                "SELECT created_at, query, reason, top_chunk, top_score"
                " FROM gap_logs WHERE " + window +
                " ORDER BY created_at DESC, id DESC LIMIT %s",
                (days, limit)).fetchall()
        return {
            "storage": "postgres",
            "window_days": days,
            "total": int(total),
            "by_reason": [{"reason": r["reason"], "count": r["n"]}
                          for r in reasons],
            "by_day": [{"date": r["day"], "count": r["n"]} for r in by_day],
            "recent": [{
                "ts": r["created_at"].isoformat(timespec="seconds"),
                "query": r["query"],
                "reason": r["reason"],
                "top_chunk": r["top_chunk"],
                "top_score": (None if r["top_score"] is None
                              else round(float(r["top_score"]), 4)),
            } for r in recent],
        }

    # ---------- 请求日志 ----------

    def log_request(self, method: str, path: str, status: int,
                    latency_ms: int, query: str = "",
                    client: str = "") -> None:
        """写一行请求日志；调用方负责吞掉异常，日志失败不影响问答。"""
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO request_logs (method, path, status, latency_ms,"
                " query, client) VALUES (%s, %s, %s, %s, %s, %s)",
                (str(method or "GET")[:8], str(path or "")[:200],
                 int(status or 0), max(0, int(latency_ms or 0)),
                 clean_query(query), str(client or "")[:64]))
            conn.commit()

    def request_stats(self, days: int = 30, limit: int = 20) -> Dict:
        """按天、按路径聚合请求，并给出最近若干条。"""
        days = max(1, min(int(days), 365))
        limit = max(1, min(int(limit), 100))
        window = "created_at >= now() - (%s::int * interval '1 day')"
        with self.connect() as conn:
            total = conn.execute(
                "SELECT count(*)::int AS n FROM request_logs WHERE " + window,
                (days,)).fetchone()["n"]
            by_path = conn.execute(
                "SELECT path, count(*)::int AS n,"
                " round(avg(latency_ms))::int AS avg_ms,"
                " count(*) FILTER (WHERE status >= 500)::int AS errors"
                " FROM request_logs WHERE " + window +
                " GROUP BY path ORDER BY n DESC, path LIMIT %s",
                (days, limit)).fetchall()
            by_day = conn.execute(
                "SELECT to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD')"
                " AS day, count(*)::int AS n"
                " FROM request_logs WHERE " + window +
                " GROUP BY 1 ORDER BY 1",
                (days,)).fetchall()
            recent = conn.execute(
                "SELECT created_at, method, path, status, latency_ms, query"
                " FROM request_logs WHERE " + window +
                " ORDER BY created_at DESC, id DESC LIMIT %s",
                (days, limit)).fetchall()
        return {
            "storage": "postgres",
            "window_days": days,
            "total": int(total),
            "by_path": [{"path": r["path"], "count": r["n"],
                         "avg_ms": int(r["avg_ms"] or 0),
                         "errors": int(r["errors"])} for r in by_path],
            "by_day": [{"date": r["day"], "count": r["n"]} for r in by_day],
            "recent": [{
                "ts": r["created_at"].isoformat(timespec="seconds"),
                "method": r["method"],
                "path": r["path"],
                "status": int(r["status"]),
                "latency_ms": int(r["latency_ms"]),
                "query": r["query"],
            } for r in recent],
        }

    # ---------- 账号 ----------

    def create_user(self, user_id: str, username: str, username_key: str,
                    password_hash: str, role: str = "member") -> Dict:
        psycopg, _ = _psycopg()
        try:
            with self.connect() as conn:
                row = conn.execute(
                    "INSERT INTO users (id, username, username_key, password_hash,"
                    " role, last_login_at) VALUES (%s, %s, %s, %s, %s, now())"
                    " RETURNING id, username, role, disabled, created_at, last_login_at",
                    (user_id, username, username_key,
                     password_hash, role)).fetchone()
                conn.commit()
        except psycopg.errors.UniqueViolation:
            raise DuplicateUser("username taken")
        return dict(row)

    def get_user_by_key(self, username_key: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, role, disabled, created_at,"
                " last_login_at FROM users WHERE username_key = %s",
                (username_key,)).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, role, disabled, created_at,"
                " last_login_at FROM users WHERE id = %s", (user_id,)).fetchone()
        return dict(row) if row else None

    def touch_login(self, user_id: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "UPDATE users SET last_login_at = now() WHERE id = %s"
                " RETURNING id, username, role, disabled, created_at, last_login_at",
                (user_id,)).fetchone()
            conn.commit()
        return dict(row) if row else None

    def update_password(self, user_id: str, password_hash: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (password_hash, user_id))
            conn.commit()
            return row.rowcount > 0

    def has_admin(self) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM users WHERE role = 'admin'"
                " AND NOT disabled LIMIT 1").fetchone()
        return row is not None

    def list_users(self) -> List[Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, username, role, disabled, created_at, last_login_at"
                " FROM users ORDER BY created_at, id").fetchall()
        return [dict(r) for r in rows]

    def count_admins(self, exclude_id: str = "") -> int:
        """还能干活的管理员数（停用的不算）。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT count(*)::int AS n FROM users WHERE role = 'admin'"
                " AND NOT disabled AND id <> %s", (exclude_id or "",)).fetchone()
        return int(row["n"])

    def set_user_disabled(self, user_id: str, disabled: bool) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "UPDATE users SET disabled = %s WHERE id = %s",
                (bool(disabled), user_id))
            conn.commit()
            return row.rowcount > 0

    def set_user_role(self, user_id: str, role: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "UPDATE users SET role = %s WHERE id = %s",
                (role, user_id))
            conn.commit()
            return row.rowcount > 0

    def promote_first_admin(self) -> Optional[str]:
        """没有任何管理员时，把最早注册的账号提升为 admin。

        老部署升级后管理员是空的；不自动提一个人，公司知识库就没人能管。
        返回被提升的用户名，调用方可以写一行启动日志。
        """
        with self.connect() as conn:
            row = conn.execute(
                "UPDATE users SET role = 'admin'"
                " WHERE id = (SELECT id FROM users WHERE NOT disabled"
                "             ORDER BY created_at, id LIMIT 1)"
                " AND NOT EXISTS (SELECT 1 FROM users"
                "                 WHERE role = 'admin' AND NOT disabled)"
                " RETURNING username").fetchone()
            conn.commit()
        return str(row["username"]) if row else None

    def promote_admins(self, username_keys: Sequence[str]) -> int:
        keys = [str(k) for k in username_keys if k]
        if not keys:
            return 0
        with self.connect() as conn:
            row = conn.execute(
                "UPDATE users SET role = 'admin'"
                " WHERE username_key = ANY(%s) AND role <> 'admin'", (keys,))
            conn.commit()
            return int(row.rowcount or 0)

    def create_session(self, token_hash: str, user_id: str,
                       expires_at: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO user_sessions (token_hash, user_id, expires_at)"
                " VALUES (%s, %s, %s)",
                (token_hash, user_id, expires_at))
            conn.commit()

    def get_session(self, token_hash: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT u.id, u.username, u.role, u.created_at, u.last_login_at,"
                " s.expires_at"
                " FROM user_sessions s JOIN users u ON u.id = s.user_id"
                " WHERE s.token_hash = %s AND s.expires_at > now()"
                " AND NOT u.disabled",
                (token_hash,)).fetchone()
        return dict(row) if row else None

    def renew_session(self, token_hash: str, expires_at: datetime) -> bool:
        """滑动过期：只往后延，不缩短（v0.67）。"""
        with self.connect() as conn:
            row = conn.execute(
                "UPDATE user_sessions SET expires_at = %s"
                " WHERE token_hash = %s AND expires_at > now()",
                (expires_at, token_hash))
            conn.commit()
            return row.rowcount > 0

    def delete_session(self, token_hash: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "DELETE FROM user_sessions WHERE token_hash = %s",
                (token_hash,))
            conn.commit()
            return row.rowcount > 0

    def delete_user_sessions(self, user_id: str, keep_hash: str = "") -> int:
        """清掉这个账号的会话；``keep_hash`` 是当前设备，改密码时保留它。"""
        with self.connect() as conn:
            if keep_hash:
                row = conn.execute(
                    "DELETE FROM user_sessions WHERE user_id = %s"
                    " AND token_hash <> %s", (user_id, keep_hash))
            else:
                row = conn.execute(
                    "DELETE FROM user_sessions WHERE user_id = %s", (user_id,))
            conn.commit()
            return int(row.rowcount or 0)

    def delete_expired_sessions(self, user_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "DELETE FROM user_sessions WHERE user_id = %s AND expires_at <= now()",
                (user_id,))
            conn.commit()
            return int(row.rowcount or 0)

    def get_user_data(self, user_id: str) -> Dict[str, Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT key, value, updated_at FROM user_data WHERE user_id = %s",
                (user_id,)).fetchall()
        return {r["key"]: {"value": r["value"], "updated_at": r["updated_at"]}
                for r in rows}

    def put_user_data(self, user_id: str, items: Dict[str, Tuple]) -> Dict:
        """按 updated_at 做后写覆盖；旧数据不覆盖新数据，返回被拒的键。"""
        applied = []
        rejected = []
        with self.connect() as conn:
            for key, (value, updated_ms) in items.items():
                row = conn.execute(
                    "INSERT INTO user_data (user_id, key, value, updated_at)"
                    " VALUES (%s, %s, %s::jsonb, %s)"
                    " ON CONFLICT (user_id, key) DO UPDATE"
                    " SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at"
                    " WHERE user_data.updated_at <= EXCLUDED.updated_at"
                    " RETURNING key",
                    (user_id, key, json.dumps(value, ensure_ascii=False),
                     datetime.fromtimestamp(updated_ms / 1000.0,
                                            tz=timezone.utc))).fetchone()
                if row:
                    applied.append(key)
                else:
                    rejected.append(key)
            server = {}
            if rejected:
                rows = conn.execute(
                    "SELECT key, updated_at FROM user_data"
                    " WHERE user_id = %s AND key = ANY(%s)",
                    (user_id, rejected)).fetchall()
                server = {r["key"]: int(r["updated_at"].timestamp() * 1000)
                          for r in rows}
            conn.commit()
        return {"applied": applied, "server": server}

    def delete_user_data(self, user_id: str,
                         keys: Optional[Sequence[str]] = None) -> int:
        """写墓碑而不是物理删：别的设备下次同步会把本地那份也删掉。"""
        from api.accounts import DELETED_MARK  # 延迟导入，避免模块循环
        with self.connect() as conn:
            if keys is None:
                row = conn.execute(
                    "UPDATE user_data SET value = %s::jsonb, updated_at = now()"
                    " WHERE user_id = %s", (json.dumps(DELETED_MARK), user_id))
            else:
                row = conn.execute(
                    "UPDATE user_data SET value = %s::jsonb, updated_at = now()"
                    " WHERE user_id = %s AND key = ANY(%s)",
                    (json.dumps(DELETED_MARK), user_id, list(keys)))
            conn.commit()
            return int(row.rowcount or 0)


def clean_query(query: str, limit: int = 200) -> str:
    """请求日志里的 query：去掉换行、截断，避免把整段对话写进表里。"""
    text = " ".join(str(query or "").split())
    return text[:limit]


