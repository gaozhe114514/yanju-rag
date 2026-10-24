# -*- coding: utf-8 -*-
"""存储层集成测试：需要一个空的 PostgreSQL 库。

  TEST_DATABASE_URL=postgresql://rag:...@127.0.0.1:5432/rag_test \
      python -m unittest tests.test_store_pg -v

没设置 TEST_DATABASE_URL 时整组跳过，普通单测不受影响。
表会被清空，别指向线上库。
"""
import os
import unittest

import numpy as np

from db.store import Store
from retrieval.chunk import Chunk, build_chunk, build_knowledge_chunk


class FakeEmbedder:
    """按字符造 8 维向量，只验证存取链路。"""

    model_name = "fake-embedder"

    def embed(self, texts, is_query=False):
        rows = []
        for text in texts:
            vec = np.zeros(8, dtype=np.float32)
            for i, ch in enumerate(text[:8]):
                vec[i] = (ord(ch) % 17) + 1
            norm = np.linalg.norm(vec) or 1.0
            rows.append(vec / norm)
        return np.asarray(rows, dtype=np.float32)


QUESTIONS = [
    {"id": "2019-408-01", "year": 2019, "seq": 1, "subject": "ds",
     "stem": "二叉排序树的中序遍历", "options": {"A": "递增", "B": "递减"},
     "answer": "A", "analysis": "中序遍历二叉排序树得到递增序列。"},
    {"id": "2020-408-02", "year": 2020, "seq": 2, "subject": "os",
     "stem": "进程调度的时机", "options": {"A": "阻塞", "B": "结束"},
     "answer": "A", "analysis": "进程阻塞会触发调度。"},
]
CARDS = [{
    "id": "ds-avl", "subject": "ds", "chapter": "树与二叉树",
    "title": "平衡二叉树", "body": "平衡二叉树是任一结点左右子树高度差不超过 1 的二叉排序树。",
    "_file": "data/curated/knowledge.jsonl", "_line": 3,
    "related_question_ids": ["2019-408-01"],
}]


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "未设置 TEST_DATABASE_URL")
class StorePgTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(os.environ["TEST_DATABASE_URL"], dim=8)
        with cls.store.connect() as conn:
            conn.execute("DROP TABLE IF EXISTS chunks")
            conn.execute("DROP TABLE IF EXISTS meta")
            conn.execute("DROP TABLE IF EXISTS gap_logs")
            conn.execute("DROP TABLE IF EXISTS answer_cache")
            conn.execute("DROP TABLE IF EXISTS answer_feedback")
            conn.commit()
        cls.store.init_schema()
        chunks = [build_chunk(q) for q in QUESTIONS]
        chunks.extend(build_knowledge_chunk(c, c["_file"], c["_line"])
                      for c in CARDS)
        cls.stats = cls.store.sync(QUESTIONS, CARDS, chunks,
                                   embedder=FakeEmbedder(),
                                   model_name=FakeEmbedder.model_name)

    def test_sync_counts(self):
        self.assertTrue(self.stats["reloaded"])
        self.assertEqual(self.stats["questions"], 2)
        self.assertEqual(self.stats["knowledge"], 1)

    def test_second_sync_skips(self):
        chunks = [build_chunk(q) for q in QUESTIONS]
        chunks.extend(build_knowledge_chunk(c, c["_file"], c["_line"])
                      for c in CARDS)
        stats = self.store.sync(QUESTIONS, CARDS, chunks,
                                embedder=FakeEmbedder(),
                                model_name=FakeEmbedder.model_name)
        self.assertFalse(stats["reloaded"])

    def test_vector_search(self):
        qv = FakeEmbedder().embed(["二叉排序树"], is_query=True)[0]
        hits = self.store.vector_search(qv, 3)
        self.assertEqual(len(hits), 3)
        self.assertEqual(hits[0][0], "2019-408-01")

    def test_filter_and_pagination(self):
        total, rows = self.store.query_questions(subject="ds")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["id"], "2019-408-01")
        total, rows = self.store.query_questions(query="阻塞")
        self.assertEqual(total, 1)
        total, rows = self.store.query_questions(year=2020)
        self.assertEqual(total, 1)
        total, rows = self.store.query_questions(limit=1, offset=1)
        self.assertEqual(total, 2)
        self.assertEqual(len(rows), 1)

    def test_detail_and_related(self):
        self.assertEqual(self.store.get_question("2019-408-01")["seq"], 1)
        self.assertIsNone(self.store.get_question("不存在"))
        related = self.store.related_knowledge("2019-408-01")
        self.assertEqual([c["id"] for c in related], ["ds-avl"])

    def test_counts_and_years(self):
        self.assertEqual(self.store.question_subject_counts(), {"ds": 1, "os": 1})
        self.assertEqual(self.store.years(), [2019, 2020])

    def test_gap_log(self):
        self.store.log_gap("资料外的问题", "score_too_low", "2019-408-01", 0.02)
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM gap_logs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["query"], "资料外的问题")
        self.assertEqual(row["top_chunk"], "2019-408-01")

    def test_answer_cache_roundtrip(self):
        key = "cache-key-1"
        self.assertIsNone(self.store.get_answer_cache(key, 60))
        payload = {"answer": "页目录号是 081H。", "refused": False,
                   "citations": [{"chunk_id": "2019-408-01", "quote": "081H"}]}
        self.store.put_answer_cache(key, "页目录号怎么算", payload, 60)
        hit = self.store.get_answer_cache(key, 60)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["payload"]["answer"], payload["answer"])
        self.assertEqual(hit["payload"]["citations"], payload["citations"])
        self.assertGreaterEqual(hit["age_s"], 0)
        self.assertLess(hit["age_s"], 60)

    def test_answer_cache_ttl_disabled_and_expired(self):
        key = "cache-key-2"
        self.store.put_answer_cache(key, "问题", {"answer": "答案"}, 60)
        # TTL=0 表示关闭缓存，既不写也不读
        self.assertIsNone(self.store.get_answer_cache(key, 0))
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE answer_cache SET created_at = now() - interval '2 hours'"
                " WHERE cache_key = %s", (key,))
            conn.commit()
        self.assertIsNone(self.store.get_answer_cache(key, 60))

    def test_answer_cache_upsert(self):
        key = "cache-key-3"
        self.store.put_answer_cache(key, "问题", {"answer": "旧"}, 60)
        self.store.put_answer_cache(key, "问题", {"answer": "新"}, 60)
        hit = self.store.get_answer_cache(key, 60)
        self.assertEqual(hit["payload"]["answer"], "新")
        with self.store.connect() as conn:
            n = conn.execute(
                "SELECT count(*)::int AS n FROM answer_cache"
                " WHERE cache_key = %s", (key,)).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_feedback_log(self):
        rec = {"query": "页目录号怎么算", "answer": "081H", "helpful": False,
               "reason": "bad_citation", "note": "引用对不上",
               "citations": ["2019-408-01"], "cached": True}
        self.store.log_feedback(rec)
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM answer_feedback ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row["query"], "页目录号怎么算")
        self.assertFalse(row["helpful"])
        self.assertEqual(row["citations"], ["2019-408-01"])
        self.assertTrue(row["cached"])

    def test_feedback_stats(self):
        with self.store.connect() as conn:
            conn.execute("DELETE FROM answer_feedback")
            conn.execute(
                "INSERT INTO answer_feedback (created_at, query, answer,"
                " helpful, reason, note, citations, cached) VALUES"
                " (now() - interval '1 hour', '最新差评', '...', false,"
                "  'wrong_answer', '答到别的题上了', '{2019-408-01}', true),"
                " (now() - interval '2 hours', '好评', '...', true, '', '',"
                "  '{}', false),"
                " (now() - interval '3 hours', '次新差评', '...', false,"
                "  'bad_citation', '', '{}', false),"
                " (now() - interval '40 days', '窗口外', '...', false,"
                "  'other', '', '{}', false)")
            conn.commit()
        stats = self.store.feedback_stats(days=30, limit=1)
        self.assertEqual(stats["storage"], "postgres")
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["helpful"], 1)
        self.assertEqual(stats["not_helpful"], 2)
        self.assertAlmostEqual(stats["helpful_rate"], 0.3333, places=4)
        by_reason = {r["reason"]: r["count"] for r in stats["by_reason"]}
        self.assertEqual(by_reason, {"wrong_answer": 1, "bad_citation": 1})
        self.assertEqual([r["query"] for r in stats["recent"]], ["最新差评"])
        self.assertEqual(stats["recent"][0]["citations"], ["2019-408-01"])
        self.assertTrue(stats["recent"][0]["cached"])

    def test_gap_stats(self):
        with self.store.connect() as conn:
            conn.execute("DELETE FROM gap_logs")
            conn.execute(
                "INSERT INTO gap_logs (created_at, query, reason, top_chunk,"
                " top_score) VALUES"
                " (now() - interval '2 hours', '最新的问题', 'low_score',"
                "  '2019-408-01', 0.02),"
                " (now() - interval '3 hours', '次新的问题', 'low_score',"
                "  NULL, 0.0),"
                " (now() - interval '5 hours', '引用失败的问题',"
                "  'citation_failed: 引用过短', '2019-408-31', 0.9),"
                " (now() - interval '40 days', '窗口外的问题', 'no_hits',"
                "  NULL, 0.0)")
            conn.commit()
        stats = self.store.gap_stats(days=30, limit=2)
        self.assertEqual(stats["storage"], "postgres")
        self.assertEqual(stats["total"], 3)
        by_reason = {r["reason"]: r["count"] for r in stats["by_reason"]}
        self.assertEqual(by_reason["low_score"], 2)
        self.assertEqual(by_reason["citation_failed"], 1)
        self.assertNotIn("no_hits", by_reason)
        self.assertEqual(sum(d["count"] for d in stats["by_day"]), 3)
        self.assertEqual([r["query"] for r in stats["recent"]],
                         ["最新的问题", "次新的问题"])
        self.assertIsNone(stats["recent"][1]["top_chunk"])


if __name__ == "__main__":
    unittest.main()
