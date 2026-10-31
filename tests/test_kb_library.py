# -*- coding: utf-8 -*-
"""用户知识库测试：切分保真、资料库增删、doc 模式 prompt。

全部走进程内后备存储，不连数据库、不加载模型。
"""
import os
import tempfile
import unittest

from answer.cache import namespace
from answer.pipeline import AnswerPipeline
from answer.prompt import SYSTEM_PROMPT, SYSTEM_PROMPT_DOC, build_messages, format_hits
from kb import PRESET_COLLECTION, Library, LibraryError
from kb.chunking import split_text
from retrieval.chunk import Chunk
from retrieval.search import SearchHit


SAMPLE = "".join(
    "第%d段：快速排序每一趟选一个枢轴，把序列分成两部分，平均时间复杂度是 O(n log n)。"
    "最坏情况下退化为 O(n^2)，此时序列基本有序。\n" % i
    for i in range(1, 30)
)


def make_library(**kw):
    kw.setdefault("mode", "bm25")
    return Library(**kw)


class SplitTest(unittest.TestCase):
    def test_pieces_are_verbatim_substrings(self):
        pieces = split_text(SAMPLE)
        self.assertGreater(len(pieces), 1)
        for p in pieces:
            self.assertEqual(SAMPLE[p["start"]:p["end"]], p["text"])

    def test_seq_is_continuous_and_text_not_empty(self):
        pieces = split_text(SAMPLE)
        self.assertEqual([p["seq"] for p in pieces],
                         list(range(1, len(pieces) + 1)))
        self.assertTrue(all(len(p["text"]) >= 10 for p in pieces))

    def test_covers_whole_text(self):
        pieces = split_text(SAMPLE)
        # 片段之间有重叠，不能简单拼接比对；改为检查首尾覆盖
        self.assertEqual(pieces[0]["start"], 0)
        self.assertEqual(pieces[-1]["end"], len(SAMPLE.rstrip()))

    def test_long_run_without_punctuation_is_hard_split(self):
        text = "无标点" * 400
        pieces = split_text(text, target=200)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(p["text"]) <= 400 for p in pieces))
        self.assertEqual("".join(p["text"] for p in pieces), text)

    def test_empty_and_short_input(self):
        self.assertEqual(split_text(""), [])
        self.assertEqual(split_text("   \n  "), [])

    def test_crlf_normalized(self):
        pieces = split_text("第一句话写在这里。\r\n第二句话也写在这里。")
        self.assertNotIn("\r", pieces[0]["text"])


class CollectionTest(unittest.TestCase):
    def test_create_list_get(self):
        lib = make_library()
        coll = lib.create_collection("机器学习笔记", "课件整理")
        self.assertTrue(coll["id"].startswith("lib-"))
        self.assertEqual([c["id"] for c in lib.list_collections()], [coll["id"]])
        self.assertEqual(lib.get_collection(coll["id"])["name"], "机器学习笔记")

    def test_duplicate_name_rejected(self):
        lib = make_library()
        lib.create_collection("同名库")
        with self.assertRaises(LibraryError):
            lib.create_collection("同名库")

    def test_empty_name_rejected(self):
        with self.assertRaises(LibraryError):
            make_library().create_collection("   ")

    def test_preset_collection_is_not_a_row(self):
        lib = make_library()
        self.assertIsNone(lib.get_collection(PRESET_COLLECTION))
        with self.assertRaises(LibraryError):
            lib.delete_collection(PRESET_COLLECTION)

    def test_delete_collection_drops_documents_and_searcher(self):
        lib = make_library()
        coll = lib.create_collection("临时库")
        lib.add_document(coll["id"], SAMPLE, "排序笔记")
        self.assertIsNotNone(lib.searcher(coll["id"]))
        self.assertTrue(lib.delete_collection(coll["id"]))
        self.assertEqual(lib.list_collections(), [])
        self.assertIsNone(lib.searcher(coll["id"]))


class OwnershipTest(unittest.TestCase):
    def test_private_collection_keeps_owner_and_visibility(self):
        lib = make_library()
        coll = lib.create_collection("我的笔记", owner_id="u-1",
                                     visibility="private")
        self.assertEqual(coll["owner_id"], "u-1")
        self.assertEqual(coll["visibility"], "private")
        listed = lib.list_collections()[0]
        self.assertEqual(listed["owner_id"], "u-1")
        self.assertEqual(lib.get_collection(coll["id"])["visibility"], "private")

    def test_private_requires_owner(self):
        with self.assertRaises(LibraryError):
            make_library().create_collection("无主个人库", visibility="private")

    def test_same_name_allowed_across_private_owners(self):
        lib = make_library()
        lib.create_collection("笔记", owner_id="u-1", visibility="private")
        lib.create_collection("笔记", owner_id="u-2", visibility="private")
        with self.assertRaises(LibraryError):
            lib.create_collection("笔记", owner_id="u-1", visibility="private")
        # 个人库和公司库可以同名，两者不在一个命名空间
        lib.create_collection("笔记", visibility="shared")
        with self.assertRaises(LibraryError):
            lib.create_collection("笔记", visibility="shared")

    def test_bad_visibility_rejected(self):
        with self.assertRaises(LibraryError):
            make_library().create_collection("怪库", visibility="public")


class CollectionMemberTest(unittest.TestCase):
    def setUp(self):
        self.lib = make_library()
        self.coll = self.lib.create_collection("公司库", visibility="shared")
        self.cid = self.coll["id"]

    def test_set_get_list_remove(self):
        self.assertEqual(self.lib.collection_member_role(self.cid, "u-2"), "")
        self.lib.set_collection_member(self.cid, "u-2", "editor")
        self.assertEqual(self.lib.collection_member_role(self.cid, "u-2"),
                         "editor")
        self.lib.set_collection_member(self.cid, "u-3", "viewer")
        self.lib.set_collection_member(self.cid, "u-2", "viewer")
        rows = self.lib.list_collection_members(self.cid)
        self.assertEqual({r["user_id"]: r["role"] for r in rows},
                         {"u-2": "viewer", "u-3": "viewer"})
        self.assertTrue(self.lib.remove_collection_member(self.cid, "u-2"))
        self.assertFalse(self.lib.remove_collection_member(self.cid, "u-2"))
        self.assertEqual(len(self.lib.list_collection_members(self.cid)), 1)

    def test_role_must_be_editor_or_viewer(self):
        for bad in ("owner", "admin", ""):
            with self.assertRaises(LibraryError):
                self.lib.set_collection_member(self.cid, "u-2", bad)

    def test_delete_collection_drops_members(self):
        self.lib.set_collection_member(self.cid, "u-2", "editor")
        self.assertTrue(self.lib.delete_collection(self.cid))
        self.assertEqual(self.lib.collection_member_role(self.cid, "u-2"), "")

    def test_members_persist_to_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "library.json")
            lib = make_library(state_path=path)
            cid = lib.create_collection("会保存的库", owner_id="u-1",
                                        visibility="private")["id"]
            lib.set_collection_member(cid, "u-2", "editor")
            again = make_library(state_path=path)
            row = again.get_collection(cid)
            self.assertEqual(row["owner_id"], "u-1")
            self.assertEqual(row["visibility"], "private")
            self.assertEqual(again.collection_member_role(cid, "u-2"), "editor")


class DocumentTest(unittest.TestCase):
    def setUp(self):
        self.lib = make_library()
        self.cid = self.lib.create_collection("算法笔记")["id"]

    def test_add_document_returns_chunk_count(self):
        doc = self.lib.add_document(self.cid, SAMPLE, "排序")
        self.assertTrue(doc["id"].startswith("doc-"))
        self.assertGreater(doc["chunks"], 1)
        docs = self.lib.list_documents(self.cid)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["title"], "排序")
        self.assertEqual(docs[0]["chunks"], doc["chunks"])

    def test_title_falls_back_to_first_line(self):
        doc = self.lib.add_document(self.cid, "第一行就是标题\n后面的正文内容足够长了吧")
        self.assertEqual(doc["title"], "第一行就是标题")

    def test_duplicate_text_rejected(self):
        self.lib.add_document(self.cid, SAMPLE, "排序")
        with self.assertRaises(LibraryError):
            self.lib.add_document(self.cid, SAMPLE, "排序副本")

    def test_too_short_rejected(self):
        with self.assertRaises(LibraryError):
            self.lib.add_document(self.cid, "太短了")

    def test_unknown_collection_rejected(self):
        with self.assertRaises(LibraryError):
            self.lib.add_document("lib-0000000000", SAMPLE)

    def test_document_text_roundtrip(self):
        doc = self.lib.add_document(self.cid, SAMPLE, "排序")
        self.assertEqual(self.lib.document_text(self.cid, doc["id"]),
                         SAMPLE.strip())
        self.assertIsNone(self.lib.document_text(self.cid, "doc-0000000000"))

    def test_delete_document(self):
        doc = self.lib.add_document(self.cid, SAMPLE, "排序")
        self.assertTrue(self.lib.delete_document(self.cid, doc["id"]))
        self.assertEqual(self.lib.list_documents(self.cid), [])
        self.assertIsNone(self.lib.searcher(self.cid))
        self.assertFalse(self.lib.delete_document(self.cid, doc["id"]))

    def test_state_persists_to_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "library.json")
            lib = make_library(state_path=path)
            cid = lib.create_collection("会保存的库")["id"]
            lib.add_document(cid, SAMPLE, "排序")
            again = make_library(state_path=path)
            self.assertEqual([c["name"] for c in again.list_collections()],
                             ["会保存的库"])
            self.assertEqual(len(again.list_documents(cid)), 1)


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.lib = make_library()
        self.cid = self.lib.create_collection("算法笔记")["id"]

    def test_empty_collection_has_no_searcher(self):
        self.assertIsNone(self.lib.searcher(self.cid))

    def test_search_finds_added_material(self):
        self.lib.add_document(
            self.cid, "红黑树的插入需要旋转和变色来维持平衡。\n" * 20, "红黑树")
        self.lib.add_document(
            self.cid, "数据库的第三范式要求消除非主属性对码的传递依赖。\n" * 20,
            "范式")
        hits = self.lib.searcher(self.cid).search("红黑树插入怎么维持平衡")
        self.assertTrue(hits)
        self.assertEqual(hits[0].chunk.kind, "doc")
        self.assertEqual(hits[0].chunk.title, "红黑树")
        self.assertEqual(hits[0].chunk.subject, "算法笔记")

    def test_hits_carry_document_offsets(self):
        self.lib.add_document(self.cid, SAMPLE, "排序")
        hit = self.lib.searcher(self.cid).search("平均时间复杂度")[0]
        ref = hit.chunk.source_ref
        self.assertIn("document_id", ref)
        self.assertEqual(ref["document_title"], "排序")
        self.assertEqual(SAMPLE[ref["start"]:ref["end"]], hit.chunk.text)

    def test_searcher_rebuilds_after_add(self):
        self.lib.add_document(self.cid, SAMPLE, "排序")
        first = self.lib.searcher(self.cid)
        self.assertIs(self.lib.searcher(self.cid), first)
        self.lib.add_document(
            self.cid, "二叉树的前序遍历先访问根结点，再访问左子树。\n" * 20, "遍历")
        self.assertIsNot(self.lib.searcher(self.cid), first)

    def test_exact_ref_parsing_is_off(self):
        # 用户资料里没有「2019 年第 31 题」这种题号，不能因此拒答
        self.lib.add_document(
            self.cid, "2023 年第 31 题的答案是先检查页表项是否有效。\n" * 20, "真题")
        hits = self.lib.searcher(self.cid).search("2023 年第 31 题")
        self.assertTrue(hits)


class DocPromptTest(unittest.TestCase):
    def _hit(self):
        ch = Chunk("doc-0123456789-001", 1,
                   "快速排序每一趟选一个枢轴，平均时间复杂度 O(n log n)。",
                   kind="doc", title="排序笔记", subject="算法笔记")
        return SearchHit(ch, 0.9)

    def test_format_hits_shows_document_source(self):
        text = format_hits([self._hit()], "doc")
        self.assertIn("来源=资料库", text)
        self.assertIn("文档=排序笔记", text)
        self.assertNotIn("类型=真题", text)

    def test_doc_mode_uses_doc_system_prompt(self):
        msgs = build_messages("快速排序复杂度", [self._hit()], mode="doc")
        self.assertEqual(msgs[0]["content"], SYSTEM_PROMPT_DOC)
        self.assertNotIn("408", msgs[0]["content"])

    def test_default_mode_keeps_exam_prompt(self):
        msgs = build_messages("快速排序复杂度", [self._hit()])
        self.assertEqual(msgs[0]["content"], SYSTEM_PROMPT)

    def test_citation_still_must_be_verbatim(self):
        class P:
            def generate(self, messages, hits=None):
                return ('{"refused": false, "answer": "x", "citations": '
                        '[{"chunk_id": "doc-0123456789-001", '
                        '"quote": "快速排序是稳定排序"}], "reason": ""}')

        class S:
            def search(self, query):
                return [self_hit]

        self_hit = self._hit()
        pipe = AnswerPipeline(S(), P(), retry=False, prompt_mode="doc")
        res = pipe.answer("快速排序稳定吗")
        self.assertTrue(res.refused)
        self.assertTrue(res.reason.startswith("citation_failed"))


class CacheNamespaceTest(unittest.TestCase):
    """不同知识库不能共用应答缓存，否则甲的答案会喂给乙的资料。"""

    BASE = "hybrid|mock|k10|fp"

    def test_doc_collection_gets_own_namespace(self):
        base = namespace(self.BASE)
        self.assertNotEqual(namespace(self.BASE, "", "lib-aaaaaaaaaa", False), base)
        self.assertNotEqual(namespace(self.BASE, "", "lib-aaaaaaaaaa", False),
                            namespace(self.BASE, "", "lib-bbbbbbbbbb", False))

    def test_preset_keeps_original_namespace(self):
        base = namespace(self.BASE)
        self.assertEqual(namespace(self.BASE, "", PRESET_COLLECTION, True), base)
        self.assertEqual(namespace(self.BASE, "", "", True), base)

    def test_style_fingerprint_isolates_partners(self):
        base = namespace(self.BASE)
        self.assertNotEqual(namespace(self.BASE, "严格一点"), base)
        self.assertEqual(namespace(self.BASE, "严格一点"),
                         namespace(self.BASE, "严格一点"))


if __name__ == "__main__":
    unittest.main()
