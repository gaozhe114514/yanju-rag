# -*- coding: utf-8 -*-
"""PostgreSQL 存取：语料同步、向量召回、浏览接口、拒答日志。

依赖 psycopg3。为了在没有数据库的机器上也能跑单元测试，
psycopg 只在真正建连接时才导入（见 `_psycopg`）。
"""
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from api.catalog import haystack as question_haystack
from api.knowledge import haystack as knowledge_haystack

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _psycopg():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg, dict_row


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
                        "knowledge": counts.get("knowledge", 0)}

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
                "embedded": len(vectors)}

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


