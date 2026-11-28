# -*- coding: utf-8 -*-
"""自动 Wiki：生成、来源校验、编辑保留与关系边。"""
import json
import unittest

from kb import Library
from kb.wiki import fallback_pages, generate_wiki

TEXT_A = "".join(
    "第%d条：员工年假需提前三个工作日在系统提交，由直属主管审批。工作满一年享受五天年假。\n" % i
    for i in range(1, 12)
)
TEXT_B = "".join(
    "第%d条：会议室需提前一天在行政系统预订，单次最长两小时，使用后关闭投影和空调。\n" % i
    for i in range(1, 12)
)


class WikiLibraryTest(unittest.TestCase):
    def setUp(self):
        self.lib = Library(mode="bm25")
        self.cid = self.lib.create_collection("员工手册")["id"]
        self.doc_a = self.lib.add_document(self.cid, TEXT_A, title="年假制度")
        self.doc_b = self.lib.add_document(self.cid, TEXT_B, title="会议室规则")

    def chunks(self, doc_id):
        return self.lib.document_chunks(self.cid, doc_id)

    def test_fallback_builds_one_page_per_document_with_sources(self):
        docs = [self.lib.get_document(self.cid, self.doc_a["id"]),
                self.lib.get_document(self.cid, self.doc_b["id"])]
        pages = fallback_pages(self.cid, docs, self.lib.collection_chunks(self.cid))
        self.assertEqual([p["title"] for p in pages], ["年假制度", "会议室规则"])
        for p in pages:
            self.assertTrue(p["source_chunk_ids"])
            self.assertTrue(p["source_doc_ids"])

    def test_replace_keeps_manual_pages_and_links(self):
        docs = [self.lib.get_document(self.cid, self.doc_a["id"])]
        pages = fallback_pages(self.cid, docs, self.lib.collection_chunks(self.cid))
        saved = self.lib.replace_wiki_pages(self.cid, pages, [])
        page = saved[0]
        self.lib.update_wiki_page(self.cid, page["id"], "年假制度（改）",
                                  "手改摘要", "手改正文 [[会议室规则]]", "张三")
        # 再生成一次：手改页保留，不会被覆盖
        again = fallback_pages(self.cid, docs, self.lib.collection_chunks(self.cid))
        saved = self.lib.replace_wiki_pages(self.cid, again, [])
        edited = [p for p in saved if p.get("edited")]
        self.assertEqual(len(edited), 1)
        self.assertEqual(edited[0]["title"], "年假制度（改）")
        self.assertEqual(edited[0]["revision"], 1)

    def test_versions_and_restore(self):
        docs = [self.lib.get_document(self.cid, self.doc_a["id"])]
        pages = fallback_pages(self.cid, docs, self.lib.collection_chunks(self.cid))
        page = self.lib.replace_wiki_pages(self.cid, pages, [])[0]
        old_body = page["body"]
        page = self.lib.update_wiki_page(self.cid, page["id"], "新标题", "新摘要",
                                         "新正文", "张三")
        versions = self.lib.wiki_versions(self.cid, page["id"])
        self.assertEqual(len(versions), 1)
        restored = self.lib.restore_wiki_version(self.cid, page["id"],
                                                 versions[0]["id"], "李四")
        self.assertEqual(restored["body"], old_body)
        self.assertEqual(restored["title"], "年假制度")

    def test_rebuild_links_uses_wiki_syntax_and_shared_docs(self):
        docs = [self.lib.get_document(self.cid, self.doc_a["id"]),
                self.lib.get_document(self.cid, self.doc_b["id"])]
        pages = fallback_pages(self.cid, docs, self.lib.collection_chunks(self.cid))
        saved = self.lib.replace_wiki_pages(self.cid, pages, [])
        a = next(p for p in saved if p["title"] == "年假制度")
        b = next(p for p in saved if p["title"] == "会议室规则")
        self.lib.update_wiki_page(self.cid, a["id"], a["title"], a["summary"],
                                  "参见 [[会议室规则]]", "张三")
        links = self.lib.rebuild_wiki_links(self.cid)
        self.assertTrue(any(e["from"] == a["id"] and e["to"] == b["id"]
                            for e in links))


class _FakeProvider:
    def __init__(self, payload):
        self.payload = payload

    def complete(self, messages, **kw):
        return json.dumps(self.payload, ensure_ascii=False)


class WikiGenerateTest(unittest.TestCase):
    def setUp(self):
        self.lib = Library(mode="bm25")
        self.cid = self.lib.create_collection("员工手册")["id"]
        self.doc = self.lib.add_document(self.cid, TEXT_A, title="年假制度")
        self.chunks = self.lib.collection_chunks(self.cid)

    def test_llm_pages_with_invalid_sources_are_dropped(self):
        good_id = self.chunks[0]["id"]
        provider = _FakeProvider({"pages": [
            {"title": "年假申请", "summary": "申请流程",
             "body": "年假需要提前三个工作日提交，由主管审批。",
             "source_chunk_ids": [good_id]},
            {"title": "编造页", "summary": "没有来源",
             "body": "这是一段没有任何真实来源的正文。",
             "source_chunk_ids": ["chunk-not-exist"]},
        ]})
        pages, source = generate_wiki(provider, self.cid,
                                      [self.lib.get_document(self.cid, self.doc["id"])],
                                      self.chunks)
        self.assertEqual(source, "llm")
        self.assertEqual([p["title"] for p in pages], ["年假申请"])
        self.assertEqual(pages[0]["source_chunk_ids"], [good_id])

    def test_llm_failure_falls_back_to_documents(self):
        class Broken:
            def complete(self, *a, **kw):
                raise RuntimeError("down")
        pages, source = generate_wiki(Broken(), self.cid,
                                      [self.lib.get_document(self.cid, self.doc["id"])],
                                      self.chunks)
        self.assertTrue(pages)
        self.assertEqual(source, "llm_error+fallback")


if __name__ == "__main__":
    unittest.main()
