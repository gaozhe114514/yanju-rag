# -*- coding: utf-8 -*-
"""PostgreSQL 存取：语料同步、向量召回、浏览接口、拒答日志。

依赖 psycopg3。为了在没有数据库的机器上也能跑单元测试，
psycopg 只在真正建连接时才导入（见 `_psycopg`）。
"""
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from api.catalog import haystack as question_haystack
from api.knowledge import haystack as knowledge_haystack

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


def corpus_fingerprint(questions: Sequence[Dict], knowledge: Sequence[Dict],
                       model_name: str, dim: int) -> str:
    """语料 + 模型的指纹；变了才重算向量。"""
    h = hashlib.sha256()
    h.update(("model=%s|dim=%s\n" % (model_name, dim)).encode("utf-8"))
    for row in sorted(questions, key=lambda r: r["id"]):
        h.update(json.dumps(row, ensure_ascii=False, sort_keys=True,
                            default=str).encode("utf-8"))
        h.update(b"\n")
    for card in sorted(knowledge, key=lambda r: r["id"]):
        h.update(json.dumps(card, ensure_ascii=False, sort_keys=True,
                            default=str).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


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

    # ---------- 语料同步 ----------

    def _meta(self, conn, key: str) -> str:
        row = conn.execute("SELECT value FROM meta WHERE key = %s", (key,)).fetchone()
        return row["value"] if row else ""

    def _counts(self, conn) -> Dict[str, int]:
        rows = conn.execute(
            "SELECT kind, count(*)::int AS n FROM chunks GROUP BY kind").fetchall()
        return {r["kind"]: r["n"] for r in rows}

    def sync(self, questions: Sequence[Dict], knowledge: Sequence[Dict],
             chunks: Sequence, embedder=None,
             model_name: str = "") -> Dict:
        """把语料写入 Postgres；指纹没变就跳过，避免每次启动重算向量。

        `chunks` 是 retrieval.chunk 构建的检索单元，id 与 questions/knowledge 对齐。
        """
        by_id = {c.id: c for c in chunks}
        if len(by_id) != len(chunks):
            raise ValueError("chunk id 重复")
        dim = self.dim
        vectors: Dict[str, str] = {}
        if embedder is not None:
            texts = [c.text for c in chunks]
            arr = embedder.embed(texts)
            if len(arr) != len(texts):
                raise RuntimeError("向量条数与语料不一致")
            if arr.shape[1] != dim:
                raise RuntimeError(
                    "向量维度 %d 与 schema 的 %d 不一致，改 KB_EMBED_DIM 后要重建表"
                    % (arr.shape[1], dim))
            vectors = {c.id: vector_literal(v) for c, v in zip(chunks, arr)}

        fingerprint = corpus_fingerprint(
            questions, knowledge, model_name or "none", dim)
        expected = {"question": len(questions), "knowledge": len(knowledge)}

        with self.connect() as conn:
            counts = self._counts(conn)
            stored = self._meta(conn, "corpus_fingerprint")
            null_vec = conn.execute(
                "SELECT count(*)::int AS n FROM chunks WHERE embedding IS NULL"
            ).fetchone()["n"]
            need = (stored != fingerprint or counts != expected
                    or (embedder is not None and null_vec > 0))
            if not need:
                return {"reloaded": False, "questions": counts.get("question", 0),
                        "knowledge": counts.get("knowledge", 0),
                        "fingerprint": fingerprint}

            rows = []
            for q in questions:
                chunk = by_id[q["id"]]
                rows.append({
                    "id": q["id"], "kind": "question",
                    "year": int(q.get("year") or 0) or None,
                    "seq": int(q.get("seq") or 0),
                    "subject": q.get("subject") or "",
                    "chapter": "", "title": "",
                    "search_text": question_haystack(q),
                    "text": chunk.text,
                    "answer": q.get("answer") or "",
                    "analysis": q.get("analysis") or "",
                    "payload": json.dumps(q, ensure_ascii=False),
                    "related": [],
                    "embedding": vectors.get(q["id"]),
                })
            for card in knowledge:
                chunk = by_id[card["id"]]
                rows.append({
                    "id": card["id"], "kind": "knowledge",
                    "year": None, "seq": 0,
                    "subject": card.get("subject") or "",
                    "chapter": card.get("chapter") or "",
                    "title": card.get("title") or "",
                    "search_text": knowledge_haystack(card),
                    "text": chunk.text,
                    "answer": "", "analysis": "",
                    "payload": json.dumps(card, ensure_ascii=False),
                    "related": list(card.get("related_question_ids") or []),
                    "embedding": vectors.get(card["id"]),
                })

            ids = [r["id"] for r in rows]
            with conn.cursor() as cur:
                cur.execute("DELETE FROM chunks WHERE NOT (id = ANY(%s))", (ids,))
                cur.executemany(
                    """
                    INSERT INTO chunks (id, kind, year, seq, subject, chapter,
                                        title, search_text, text, answer, analysis,
                                        payload, related_question_ids, embedding,
                                        updated_at)
                    VALUES (%(id)s, %(kind)s, %(year)s, %(seq)s, %(subject)s,
                            %(chapter)s, %(title)s, %(search_text)s, %(text)s,
                            %(answer)s, %(analysis)s, %(payload)s::jsonb,
                            %(related)s, %(embedding)s::vector, now())
                    ON CONFLICT (id) DO UPDATE SET
                        kind = EXCLUDED.kind,
                        year = EXCLUDED.year,
                        seq = EXCLUDED.seq,
                        subject = EXCLUDED.subject,
                        chapter = EXCLUDED.chapter,
                        title = EXCLUDED.title,
                        search_text = EXCLUDED.search_text,
                        text = EXCLUDED.text,
                        answer = EXCLUDED.answer,
                        analysis = EXCLUDED.analysis,
                        payload = EXCLUDED.payload,
                        related_question_ids = EXCLUDED.related_question_ids,
                        embedding = COALESCE(EXCLUDED.embedding, chunks.embedding),
                        updated_at = now()
                    """, rows)
                cur.execute(
                    "INSERT INTO meta (key, value) VALUES ('corpus_fingerprint', %s),"
                    " ('embedding_model', %s), ('embedding_dim', %s)"
                    " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (fingerprint, model_name or "none", str(dim)))
            conn.commit()

        return {"reloaded": True, "questions": len(questions),
                "knowledge": len(knowledge),
                "embedded": len(vectors), "fingerprint": fingerprint}

    # ---------- 读语料 ----------

    def load_questions(self) -> List[Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM chunks WHERE kind = 'question'"
                " ORDER BY year, seq").fetchall()
        return [r["payload"] for r in rows]

    def load_knowledge(self) -> List[Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM chunks WHERE kind = 'knowledge'"
                " ORDER BY subject, chapter, title").fetchall()
        return [r["payload"] for r in rows]

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
                " created_at, updated_at FROM collections WHERE id = %s",
                (cid,)).fetchone()
        return dict(row) if row else None

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
                     source: str = "", nbytes: int = 0) -> None:
        """写入一篇文档及其片段；vectors 是 {chunk_id: pgvector 文本}。"""
        vectors = vectors or {}
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO documents (id, collection_id, title, text, sha256,"
                " status, error, bytes, source)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (doc_id, cid, title, text, sha, status, error, nbytes, source))
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
                " d.status, d.error, d.bytes, d.source,"
                " length(d.text) AS chars,"
                " (SELECT count(*)::int FROM doc_chunks k"
                "   WHERE k.document_id = d.id) AS chunk_count"
                " FROM documents d WHERE d.collection_id = %s"
                " ORDER BY d.created_at", (cid,)).fetchall()
        return [dict(r) for r in rows]

    def get_document(self, cid: str, doc_id: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, collection_id, title, text, sha256, created_at,"
                " updated_at, status, error, bytes, source"
                " FROM documents WHERE collection_id = %s AND id = %s",
                (cid, doc_id)).fetchone()
        return dict(row) if row else None

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
                           status: str = "completed", error: str = "") -> None:
        """重新切分：整篇片段换掉、正文与状态一起更新，一步提交。"""
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
            args.extend([cid, doc_id])
            conn.execute("UPDATE documents SET " + ", ".join(sets) +
                         " WHERE collection_id = %s AND id = %s", tuple(args))
            conn.execute(
                "UPDATE collections SET updated_at = now() WHERE id = %s", (cid,))
            conn.commit()

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

    # ---------- 检索 ----------

    def vector_search(self, query_vec: Sequence[float],
                      top_k: int = 50) -> List[Tuple[str, float]]:
        lit = vector_literal(query_vec)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, 1 - (embedding <=> %s::vector) AS score"
                " FROM chunks WHERE embedding IS NOT NULL"
                " ORDER BY embedding <=> %s::vector LIMIT %s",
                (lit, lit, int(top_k))).fetchall()
        return [(r["id"], float(r["score"])) for r in rows]

    # ---------- 浏览接口 ----------

    def query_questions(self, query: str = "", subject: str = "",
                        year: Optional[int] = None, limit: int = 20,
                        offset: int = 0) -> Tuple[int, List[Dict]]:
        where = ["kind = 'question'"]
        params: List = []
        sub = (subject or "").strip()
        if sub == "none":
            where.append("subject = ''")
        elif sub:
            where.append("subject = %s")
            params.append(sub)
        if year is not None:
            where.append("year = %s")
            params.append(int(year))
        text = (query or "").strip().lower()
        if text:
            where.append("strpos(search_text, %s) > 0")
            params.append(text)
        cond = " WHERE " + " AND ".join(where)
        with self.connect() as conn:
            total = conn.execute(
                "SELECT count(*)::int AS n FROM chunks" + cond,
                tuple(params)).fetchone()["n"]
            rows = conn.execute(
                "SELECT payload FROM chunks" + cond +
                " ORDER BY year, seq LIMIT %s OFFSET %s",
                tuple(params) + (int(limit), int(offset))).fetchall()
        return total, [r["payload"] for r in rows]

    def all_knowledge(self) -> List[Dict]:
        """考点只有几十张，一次取回后在应用层筛选/排序，语义与 JSONL 版完全一致。"""
        return self.load_knowledge()

    def get_question(self, chunk_id: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM chunks WHERE id = %s AND kind = 'question'",
                (chunk_id,)).fetchone()
        return row["payload"] if row else None

    def get_knowledge(self, chunk_id: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM chunks WHERE id = %s AND kind = 'knowledge'",
                (chunk_id,)).fetchone()
        return row["payload"] if row else None

    def related_knowledge(self, question_id: str) -> List[Dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM chunks WHERE kind = 'knowledge'"
                " AND %s = ANY(related_question_ids)"
                " ORDER BY subject, chapter, title", (question_id,)).fetchall()
        return [r["payload"] for r in rows]

    def question_subject_counts(self) -> Dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT subject, count(*)::int AS n FROM chunks"
                " WHERE kind = 'question' GROUP BY subject").fetchall()
        return {r["subject"] or "": r["n"] for r in rows}

    def years(self) -> List[int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT year FROM chunks"
                " WHERE kind = 'question' AND year IS NOT NULL"
                " ORDER BY year").fetchall()
        return [int(r["year"]) for r in rows]

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

    # ---------- 评测结果 ----------

    def save_eval_result(self, name: str, payload: Dict,
                         mode: str = "", total: int = 0,
                         passed: int = 0) -> int:
        body = json.dumps(payload or {}, ensure_ascii=False, default=str)
        with self.connect() as conn:
            row = conn.execute(
                "INSERT INTO eval_results (name, mode, total, passed, payload)"
                " VALUES (%s, %s, %s, %s, %s::jsonb) RETURNING id",
                (str(name or "eval")[:120], str(mode or "")[:40],
                 int(total or 0), int(passed or 0), body)).fetchone()
            conn.commit()
        return int(row["id"])

    def list_eval_results(self, name: str = "", limit: int = 20) -> List[Dict]:
        limit = max(1, min(int(limit), 100))
        with self.connect() as conn:
            if name:
                rows = conn.execute(
                    "SELECT id, created_at, name, mode, total, passed, payload"
                    " FROM eval_results WHERE name = %s"
                    " ORDER BY created_at DESC, id DESC LIMIT %s",
                    (name, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, created_at, name, mode, total, passed, payload"
                    " FROM eval_results ORDER BY created_at DESC, id DESC"
                    " LIMIT %s", (limit,)).fetchall()
        return [{
            "id": int(r["id"]),
            "ts": r["created_at"].isoformat(timespec="seconds"),
            "name": r["name"],
            "mode": r["mode"],
            "total": int(r["total"]),
            "passed": int(r["passed"]),
            "pass_rate": (round(int(r["passed"]) / int(r["total"]), 4)
                          if int(r["total"]) else 0.0),
            "payload": r["payload"],
        } for r in rows]


    # ---------- 账号 ----------

    def create_user(self, user_id: str, username: str, username_key: str,
                    password_hash: str, role: str = "member") -> Dict:
        psycopg, _ = _psycopg()
        try:
            with self.connect() as conn:
                row = conn.execute(
                    "INSERT INTO users (id, username, username_key, password_hash,"
                    " role, last_login_at) VALUES (%s, %s, %s, %s, %s, now())"
                    " RETURNING id, username, role, created_at, last_login_at",
                    (user_id, username, username_key,
                     password_hash, role)).fetchone()
                conn.commit()
        except psycopg.errors.UniqueViolation:
            raise DuplicateUser("username taken")
        return dict(row)

    def get_user_by_key(self, username_key: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, role, created_at,"
                " last_login_at FROM users WHERE username_key = %s",
                (username_key,)).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, username, role, created_at, last_login_at"
                " FROM users WHERE id = %s", (user_id,)).fetchone()
        return dict(row) if row else None

    def touch_login(self, user_id: str) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute(
                "UPDATE users SET last_login_at = now() WHERE id = %s"
                " RETURNING id, username, role, created_at, last_login_at",
                (user_id,)).fetchone()
            conn.commit()
        return dict(row) if row else None

    def has_admin(self) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        return row is not None

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
                " WHERE id = (SELECT id FROM users ORDER BY created_at, id LIMIT 1)"
                " AND NOT EXISTS (SELECT 1 FROM users WHERE role = 'admin')"
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
                "SELECT u.id, u.username, u.role, u.created_at, u.last_login_at"
                " FROM user_sessions s JOIN users u ON u.id = s.user_id"
                " WHERE s.token_hash = %s AND s.expires_at > now()",
                (token_hash,)).fetchone()
        return dict(row) if row else None

    def delete_session(self, token_hash: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "DELETE FROM user_sessions WHERE token_hash = %s",
                (token_hash,))
            conn.commit()
            return row.rowcount > 0

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


