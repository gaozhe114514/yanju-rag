# -*- coding: utf-8 -*-
"""用户知识库测试：切分保真、资料库增删、doc 模式 prompt。

全部走进程内后备存储，不连数据库、不加载模型。
"""
import os
import tempfile
import unittest
from unittest import mock

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


class DocumentEditTest(unittest.TestCase):
    def setUp(self):
        self.lib = make_library()
        self.coll = self.lib.create_collection("可编辑库")
        self.doc = self.lib.add_document(self.coll["id"], SAMPLE, "原始资料")

    def test_update_changes_text_title_and_chunks(self):
        new_text = SAMPLE.replace("快速排序", "归并排序")
        out = self.lib.update_document(self.coll["id"], self.doc["id"], new_text,
                                       title="改过的标题", author="alice")
        self.assertEqual(out["title"], "改过的标题")
        self.assertGreater(out["chunks"], 0)
        row = self.lib.get_document(self.coll["id"], self.doc["id"])
        self.assertIn("归并排序", row["text"])
        self.assertEqual(row["title"], "改过的标题")
        self.assertEqual(len(self.lib.document_chunks(self.coll["id"],
                                                      self.doc["id"])),
                         out["chunks"])

    def test_snapshot_keeps_old_text(self):
        old = SAMPLE
        new_text = SAMPLE.replace("快速排序", "堆排序")
        out = self.lib.update_document(self.coll["id"], self.doc["id"], new_text,
                                       author="alice")
        versions = self.lib.list_document_versions(self.coll["id"], self.doc["id"])
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["id"], out["version_id"])
        self.assertEqual(versions[0]["author"], "alice")
        self.assertEqual(versions[0]["chars"], len(old.strip()))
        snap = self.lib.get_document_version(self.coll["id"], self.doc["id"],
                                             out["version_id"])
        self.assertIn("快速排序", snap["text"])

    def test_restore_old_version_and_keep_current_as_history(self):
        current = SAMPLE.replace("快速排序", "冒泡排序")
        first = self.lib.update_document(self.coll["id"], self.doc["id"], current,
                                         author="alice")
        out = self.lib.restore_document_version(self.coll["id"], self.doc["id"],
                                                first["version_id"], author="bob")
        self.assertEqual(out["restored_from"], first["version_id"])
        row = self.lib.get_document(self.coll["id"], self.doc["id"])
        self.assertIn("快速排序", row["text"])
        versions = self.lib.list_document_versions(self.coll["id"], self.doc["id"])
        self.assertEqual(len(versions), 2)
        # 恢复出来的那一版（冒泡排序）也留在历史里，不会再被覆盖
        self.assertIn("冒泡排序",
                      self.lib.get_document_version(self.coll["id"], self.doc["id"],
                                                    versions[0]["id"])["text"])

    def test_duplicate_and_short_rejected(self):
        other_text = SAMPLE.replace("快速排序", "二分查找")
        self.lib.add_document(self.coll["id"], other_text, "另一份资料")
        with self.assertRaises(LibraryError):
            self.lib.update_document(self.coll["id"], self.doc["id"], other_text,
                                     title="想改成和别的资料一样")
        with self.assertRaises(LibraryError):
            self.lib.update_document(self.coll["id"], self.doc["id"], "太短了")
        # 失败时不动原文，也不留历史
        self.assertEqual(self.lib.list_document_versions(self.coll["id"],
                                                         self.doc["id"]), [])

    def test_history_is_capped(self):
        text = SAMPLE
        for i in range(25):
            text = text + "\n第%d次修改。" % i
            self.lib.update_document(self.coll["id"], self.doc["id"], text,
                                     author="alice")
        versions = self.lib.list_document_versions(self.coll["id"], self.doc["id"],
                                                   limit=50)
        self.assertEqual(len(versions), self.lib.MAX_VERSIONS)

    def test_delete_document_drops_versions(self):
        self.lib.update_document(self.coll["id"], self.doc["id"],
                                 SAMPLE.replace("快速排序", "希尔排序"))
        self.assertTrue(self.lib.delete_document(self.coll["id"], self.doc["id"]))
        self.assertEqual(self.lib.list_document_versions(self.coll["id"],
                                                         self.doc["id"]), [])


class DocumentAssetTest(unittest.TestCase):
    def setUp(self):
        self.lib = make_library()
        self.cid = self.lib.create_collection("附件库")["id"]
        self.doc = self.lib.add_document(self.cid, SAMPLE, "带图资料")
        self.doc_id = self.doc["id"]

    def test_roundtrip_keeps_bytes(self):
        data = b"\x89PNG\r\n\x1a\nfake-image-bytes"
        row = self.lib.add_document_asset(self.cid, self.doc_id, "结构图.png",
                                          "image/png", data)
        self.assertTrue(row["id"].startswith("asset-"))
        self.assertEqual(row["name"], "结构图.png")
        self.assertEqual(row["bytes"], len(data))
        listed = self.lib.list_document_assets(self.cid, self.doc_id)
        self.assertEqual([a["name"] for a in listed], ["结构图.png"])
        got = self.lib.get_document_asset(self.cid, self.doc_id, row["id"])
        self.assertEqual(got["data"], data)
        self.assertEqual(got["name"], "结构图.png")
        self.assertTrue(self.lib.delete_document_asset(
            self.cid, self.doc_id, row["id"]))
        self.assertEqual(self.lib.list_document_assets(self.cid, self.doc_id), [])
        self.assertFalse(self.lib.delete_document_asset(
            self.cid, self.doc_id, row["id"]))

    def test_duplicate_name_gets_suffix(self):
        self.lib.add_document_asset(self.cid, self.doc_id, "笔记.txt",
                                    "text/plain", b"one")
        second = self.lib.add_document_asset(self.cid, self.doc_id,
                                             "笔记.txt", "text/plain", b"two")
        self.assertEqual(second["name"], "笔记 (2).txt")

    def test_path_and_blank_name_are_cleaned(self):
        row = self.lib.add_document_asset(
            self.cid, self.doc_id, "C:\\temp\\..\\资料/图 1.png",
            "image/png", b"x")
        self.assertEqual(row["name"], "图 1.png")
        blank = self.lib.add_document_asset(self.cid, self.doc_id, "  ",
                                            "", b"y")
        self.assertEqual(blank["name"], "附件")

    def test_size_and_count_are_capped(self):
        with mock.patch("kb.library.MAX_ASSET_BYTES", 4):
            with self.assertRaises(LibraryError):
                self.lib.add_document_asset(self.cid, self.doc_id, "big.bin",
                                            "", b"12345")
        with mock.patch("kb.library.MAX_ASSETS", 2):
            self.lib.add_document_asset(self.cid, self.doc_id, "a.txt", "", b"a")
            self.lib.add_document_asset(self.cid, self.doc_id, "b.txt", "", b"b")
            with self.assertRaises(LibraryError):
                self.lib.add_document_asset(self.cid, self.doc_id, "c.txt", "", b"c")
        with self.assertRaises(LibraryError):
            self.lib.add_document_asset(self.cid, self.doc_id, "empty.txt", "", b"")

    def test_unknown_document_rejected(self):
        with self.assertRaises(LibraryError):
            self.lib.add_document_asset(self.cid, "doc-0000000000", "x.txt",
                                        "", b"x")

    def test_delete_document_drops_assets(self):
        row = self.lib.add_document_asset(self.cid, self.doc_id, "a.png",
                                          "image/png", b"png")
        self.assertTrue(self.lib.delete_document(self.cid, self.doc_id))
        self.assertIsNone(self.lib.get_document_asset(self.cid, self.doc_id,
                                                      row["id"]))
        self.assertEqual(self.lib.list_document_assets(self.cid, self.doc_id), [])

    def test_delete_collection_drops_assets(self):
        self.lib.add_document_asset(self.cid, self.doc_id, "a.png",
                                    "image/png", b"png")
        self.assertTrue(self.lib.delete_collection(self.cid))
        self.assertEqual(self.lib.list_document_assets(self.cid, self.doc_id), [])

    def test_assets_persist_to_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "library.json")
            lib = make_library(state_path=path)
            cid = lib.create_collection("会保存的附件库")["id"]
            doc_id = lib.add_document(cid, SAMPLE, "排序")["id"]
            row = lib.add_document_asset(cid, doc_id, "图.png", "image/png",
                                         b"\x00\x01binary")
            again = make_library(state_path=path)
            got = again.get_document_asset(cid, doc_id, row["id"])
            self.assertEqual(got["data"], b"\x00\x01binary")
            self.assertEqual(again.list_document_assets(cid, doc_id)[0]["name"],
                             "图.png")


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
            def search(self, query, skip_rerank=False):
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
