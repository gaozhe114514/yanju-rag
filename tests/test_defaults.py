# -*- coding: utf-8 -*-
"""默认公司知识库：示例资料能灌入，重复启动不重复加。"""
import tempfile
import unittest
from pathlib import Path

from kb.defaults import DEFAULT_DIR, ensure_default_library, load_default_docs
from kb.library import Library


class DefaultDocsTest(unittest.TestCase):
    def test_bundled_docs_are_readable(self):
        docs = load_default_docs(DEFAULT_DIR)
        self.assertGreaterEqual(len(docs), 6)
        self.assertTrue(all(len(d["text"]) >= 20 for d in docs))
        self.assertTrue(all(d["title"] for d in docs))

    def test_default_library_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp)
            (docs / "01-示例.md").write_text(
                "# 示例资料\n\n这是一篇用于测试的示例资料，内容足够长。",
                encoding="utf-8")
            lib = Library(mode="bm25")
            first = ensure_default_library(lib, docs)
            self.assertIsNotNone(first)
            self.assertEqual(first["name"], "公司知识库")
            self.assertEqual(len(lib.list_documents(first["id"])), 1)
            again = ensure_default_library(lib, docs)
            self.assertEqual(again["id"], first["id"])
            self.assertEqual(len(lib.list_documents(first["id"])), 1)


if __name__ == "__main__":
    unittest.main()
