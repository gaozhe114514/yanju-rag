# -*- coding: utf-8 -*-
"""存储层纯函数测试：不需要 PostgreSQL。"""
import unittest

from db.store import SCHEMA_PATH, vector_literal


class VectorLiteralTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(vector_literal([0.5, -1, 0.25]), "[0.5000000,-1.0000000,0.2500000]")

    def test_empty(self):
        self.assertEqual(vector_literal([]), "[]")


class SchemaTest(unittest.TestCase):
    def test_dim_replaced_and_braces_kept(self):
        sql = SCHEMA_PATH.read_text(encoding="utf-8").replace("{dim}", "512")
        self.assertIn("vector(512)", sql)
        self.assertIn("DEFAULT '{}'", sql)


if __name__ == "__main__":
    unittest.main()
