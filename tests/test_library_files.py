# -*- coding: utf-8 -*-
"""文件进料与片段管理：状态流转、片段改删、重新切分。

全部走进程内后备存储，不连数据库、不加载模型。
"""
import unittest

from kb import Library, LibraryError

SAMPLE = "".join(
    "第%d段：进程是资源分配的单位，线程是调度的基本单位，二者共享同一段地址空间。\n" % i
    for i in range(1, 25)
)


def make_library():
    return Library(mode="bm25")


class PendingFlowTest(unittest.TestCase):
    def setUp(self):
        self.lib = make_library()
        self.coll = self.lib.create_collection("操作系统讲义")

    def test_upload_creates_processing_then_completes(self):
        doc = self.lib.create_pending_document(
            self.coll["id"], "讲义", source=".pdf", nbytes=1024)
        self.assertEqual(doc["status"], "processing")
        rows = self.lib.list_documents(self.coll["id"])
        self.assertEqual(rows[0]["status"], "processing")
        self.assertEqual(rows[0]["bytes"], 1024)

        out = self.lib.finish_document(self.coll["id"], doc["id"], SAMPLE)
        self.assertIsNotNone(out)
        self.assertEqual(out["status"], "completed")
        self.assertGreater(out["chunks"], 0)

        rows = self.lib.list_documents(self.coll["id"])
        self.assertEqual(rows[0]["status"], "completed")
        self.assertGreater(rows[0]["chars"], 100)
        self.assertEqual(rows[0]["source"], ".pdf")

    def test_failed_parse_keeps_reason(self):
        doc = self.lib.create_pending_document(self.coll["id"], "扫描件")
        self.lib.fail_document(self.coll["id"], doc["id"],
                               "PDF 里没有文字层（多半是扫描件）")
        row = self.lib.list_documents(self.coll["id"])[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("扫描件", row["error"])

    def test_duplicate_file_is_refused_not_silently_added(self):
        first = self.lib.create_pending_document(self.coll["id"], "a")
        self.lib.finish_document(self.coll["id"], first["id"], SAMPLE)
        second = self.lib.create_pending_document(self.coll["id"], "b")
        out = self.lib.finish_document(self.coll["id"], second["id"], SAMPLE)
        self.assertIsNone(out)
        rows = {r["id"]: r for r in self.lib.list_documents(self.coll["id"])}
        self.assertEqual(rows[second["id"]]["status"], "failed")
        self.assertIn("已经在库里", rows[second["id"]]["error"])


class ChunkManageTest(unittest.TestCase):
    def setUp(self):
        self.lib = make_library()
        self.coll = self.lib.create_collection("操作系统讲义")
        doc = self.lib.create_pending_document(self.coll["id"], "讲义")
        self.lib.finish_document(self.coll["id"], doc["id"], SAMPLE)
        self.doc = doc

    def chunks(self):
        return self.lib.document_chunks(self.coll["id"], self.doc["id"])

    def test_chunks_are_numbered_from_one(self):
        chunks = self.chunks()
        self.assertGreater(len(chunks), 1)
        self.assertEqual([c["seq"] for c in chunks],
                         list(range(1, len(chunks) + 1)))

    def test_edit_chunk_updates_text_and_revision(self):
        first = self.chunks()[0]
        out = self.lib.update_chunk(self.coll["id"], self.doc["id"], first["id"],
                                    "进程是资源分配的单位，线程是调度的基本单位。",
                                    first["revision"])
        self.assertEqual(out["revision"], first["revision"] + 1)
        again = self.chunks()[0]
        self.assertEqual(again["text"], "进程是资源分配的单位，线程是调度的基本单位。")
        self.assertEqual(again["revision"], out["revision"])

    def test_stale_revision_is_refused(self):
        first = self.chunks()[0]
        self.lib.update_chunk(self.coll["id"], self.doc["id"], first["id"],
                              "第一次改：进程是资源分配的单位。", first["revision"])
        with self.assertRaises(LibraryError) as ctx:
            self.lib.update_chunk(self.coll["id"], self.doc["id"], first["id"],
                                  "第二次改：线程是调度的基本单位。", first["revision"])
        self.assertIn("已被别处改过", str(ctx.exception))

    def test_short_chunk_is_refused(self):
        first = self.chunks()[0]
        with self.assertRaises(LibraryError) as ctx:
            self.lib.update_chunk(self.coll["id"], self.doc["id"], first["id"],
                                  "太短", first["revision"])
        self.assertIn("10 个字", str(ctx.exception))

    def test_delete_chunk_removes_it_from_retrieval(self):
        first = self.chunks()[0]
        self.assertTrue(self.lib.delete_chunk(self.coll["id"], self.doc["id"],
                                              first["id"]))
        left = {c["id"] for c in self.chunks()}
        self.assertNotIn(first["id"], left)
        self.assertFalse(self.lib.delete_chunk(self.coll["id"], self.doc["id"],
                                               first["id"]))

    def test_reparse_restores_original_chunks(self):
        first = self.chunks()[0]
        self.lib.update_chunk(self.coll["id"], self.doc["id"], first["id"],
                              "改过的片段：进程是资源分配的单位。", first["revision"])
        out = self.lib.reparse_document(self.coll["id"], self.doc["id"])
        self.assertEqual(out["status"], "completed")
        self.assertGreater(out["chunks"], 1)
        texts = [c["text"] for c in self.chunks()]
        self.assertFalse(any("改过的片段" in t for t in texts))

    def test_reparse_failed_document_explains(self):
        doc = self.lib.create_pending_document(self.coll["id"], "空的")
        self.lib.fail_document(self.coll["id"], doc["id"], "没有文字层")
        with self.assertRaises(LibraryError) as ctx:
            self.lib.reparse_document(self.coll["id"], doc["id"])
        self.assertIn("重新上传", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
