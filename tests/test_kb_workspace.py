# -*- coding: utf-8 -*-
"""知识库工作台：文件夹树、每库切片参数、操作日志、原始文件与进料任务。

全部走进程内后备存储，不连数据库、不加载模型。
"""
import unittest

from kb import Library, LibraryError

SAMPLE = "".join(
    "第%d段：员工入职需要提交身份证复印件、学历证明和银行卡复印件，由人事部门归档。\n" % i
    for i in range(1, 40)
)


def make_library():
    return Library(mode="bm25")


class FolderTreeTest(unittest.TestCase):
    def setUp(self):
        self.lib = make_library()
        self.coll = self.lib.create_collection("员工手册")
        self.cid = self.coll["id"]

    def test_add_folder_creates_ancestors_and_empty_nodes(self):
        self.lib.add_folder(self.cid, "制度/人事/招聘")
        self.assertEqual(self.lib.folders(self.cid),
                         ["制度", "制度/人事", "制度/人事/招聘"])

    def test_document_folder_appears_in_tree(self):
        self.lib.add_document(self.cid, SAMPLE, title="入职材料",
                              folder="制度/人事")
        self.assertEqual(self.lib.folders(self.cid),
                         ["制度", "制度/人事"])

    def test_rename_moves_subtree_and_documents(self):
        self.lib.add_folder(self.cid, "制度/人事")
        doc = self.lib.add_document(self.cid, SAMPLE, title="入职材料",
                                    folder="制度/人事")
        self.lib.rename_folder(self.cid, "制度/人事", "规章/人事")
        self.assertEqual(self.lib.folders(self.cid),
                         ["制度", "规章", "规章/人事"])
        row = self.lib.get_document(self.cid, doc["id"])
        self.assertEqual(row["folder"], "规章/人事")

    def test_rename_refuses_move_into_itself(self):
        self.lib.add_folder(self.cid, "制度/人事")
        with self.assertRaises(LibraryError):
            self.lib.rename_folder(self.cid, "制度", "制度/下级")

    def test_delete_folder_keeps_documents_and_moves_to_parent(self):
        self.lib.add_folder(self.cid, "制度/人事")
        doc = self.lib.add_document(self.cid, SAMPLE, title="入职材料",
                                    folder="制度/人事")
        self.lib.delete_folder(self.cid, "制度/人事")
        row = self.lib.get_document(self.cid, doc["id"])
        self.assertEqual(row["folder"], "制度")
        self.assertNotIn("制度/人事", self.lib.folders(self.cid))

    def test_move_document_creates_target_folder(self):
        doc = self.lib.add_document(self.cid, SAMPLE, title="入职材料")
        self.lib.move_document(self.cid, doc["id"], "制度/人事")
        self.assertEqual(self.lib.get_document(self.cid, doc["id"])["folder"],
                         "制度/人事")
        self.assertIn("制度/人事", self.lib.folders(self.cid))


class ChunkParamsTest(unittest.TestCase):
    def setUp(self):
        self.lib = make_library()
        self.coll = self.lib.create_collection("员工手册")
        self.cid = self.coll["id"]

    def test_default_params_are_512_and_50(self):
        self.assertEqual(self.coll["chunk_target"], 512)
        self.assertEqual(self.coll["chunk_overlap"], 50)

    def test_smaller_target_produces_more_chunks(self):
        wide = self.lib.add_document(self.cid, SAMPLE, title="宽切片")
        wide_chunks = len(self.lib.document_chunks(self.cid, wide["id"]))
        self.lib.update_collection(self.cid, chunk_target=120, chunk_overlap=10)
        narrow = self.lib.add_document(self.cid, SAMPLE + "\n结尾", title="窄切片")
        narrow_chunks = len(self.lib.document_chunks(self.cid, narrow["id"]))
        self.assertGreater(narrow_chunks, wide_chunks)
        self.assertEqual(self.lib.get_collection(self.cid)["chunk_target"], 120)

    def test_reparse_uses_new_params(self):
        doc = self.lib.add_document(self.cid, SAMPLE, title="资料")
        before = len(self.lib.document_chunks(self.cid, doc["id"]))
        self.lib.update_collection(self.cid, chunk_target=120, chunk_overlap=10)
        self.lib.reparse_document(self.cid, doc["id"])
        after = len(self.lib.document_chunks(self.cid, doc["id"]))
        self.assertGreater(after, before)

    def test_overlap_cannot_exceed_half_target(self):
        with self.assertRaises(LibraryError):
            self.lib.update_collection(self.cid, chunk_target=200,
                                       chunk_overlap=150)


class AuditTest(unittest.TestCase):
    def test_audit_is_scoped_to_collection_and_newest_first(self):
        lib = make_library()
        a = lib.create_collection("甲库")["id"]
        b = lib.create_collection("乙库")["id"]
        lib.audit(a, {"id": "u1", "username": "张三"}, "document.create",
                  "document", "doc-1", "入职材料")
        lib.audit(a, {"id": "u1", "username": "张三"}, "folder.create",
                  "folder", "制度", "")
        lib.audit(b, {"id": "u2", "username": "李四"}, "document.delete",
                  "document", "doc-2", "")
        rows = lib.list_audit(a)
        self.assertEqual([r["action"] for r in rows],
                         ["folder.create", "document.create"])
        self.assertEqual(rows[0]["actor_name"], "张三")


class IngestJobTest(unittest.TestCase):
    def test_raw_file_roundtrip_and_job_lifecycle(self):
        lib = make_library()
        coll = lib.create_collection("上传测试")
        cid = coll["id"]
        doc = lib.create_pending_document(cid, "员工手册", source=".txt")
        data = "员工手册正文".encode("utf-8")
        lib.save_document_file(cid, doc["id"], "员工手册.txt", "text/plain", data)
        self.assertEqual(lib.get_document_file(doc["id"])["data"], data)
        lib.start_ingest_job(cid, doc["id"], "员工手册.txt")
        pending = lib.pending_ingest_jobs()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["document_id"], doc["id"])
        lib.set_ingest_job(doc["id"], "running", bump_attempt=True)
        self.assertEqual(lib.pending_ingest_jobs()[0]["attempts"], 1)
        lib.set_ingest_job(doc["id"], "done")
        self.assertEqual(lib.pending_ingest_jobs(), [])

    def test_deleting_document_removes_file_and_job(self):
        lib = make_library()
        coll = lib.create_collection("上传测试")
        cid = coll["id"]
        doc = lib.create_pending_document(cid, "员工手册")
        lib.save_document_file(cid, doc["id"], "a.txt", "text/plain", b"abc")
        lib.start_ingest_job(cid, doc["id"], "a.txt")
        lib.delete_document(cid, doc["id"])
        self.assertIsNone(lib.get_document_file(doc["id"]))
        self.assertEqual(lib.pending_ingest_jobs(), [])


if __name__ == "__main__":
    unittest.main()
