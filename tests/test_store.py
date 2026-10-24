# -*- coding: utf-8 -*-
"""存储层纯函数测试：不需要 PostgreSQL。"""
import unittest

from db.store import corpus_fingerprint, vector_literal


class VectorLiteralTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(vector_literal([0.5, -1, 0.25]), "[0.5000000,-1.0000000,0.2500000]")

    def test_empty(self):
        self.assertEqual(vector_literal([]), "[]")


class FingerprintTest(unittest.TestCase):
    def setUp(self):
        self.qs = [{"id": "2019-408-01", "stem": "题干"}]
        self.ks = [{"id": "ds-avl", "body": "正文"}]

    def test_stable(self):
        a = corpus_fingerprint(self.qs, self.ks, "bge-small", 512)
        b = corpus_fingerprint(self.qs, self.ks, "bge-small", 512)
        self.assertEqual(a, b)

    def test_order_insensitive(self):
        qs = list(reversed(self.qs + [{"id": "2020-408-01", "stem": "另一题"}]))
        a = corpus_fingerprint(self.qs + [{"id": "2020-408-01", "stem": "另一题"}],
                               self.ks, "bge-small", 512)
        self.assertEqual(a, corpus_fingerprint(qs, self.ks, "bge-small", 512))

    def test_content_change(self):
        a = corpus_fingerprint(self.qs, self.ks, "bge-small", 512)
        b = corpus_fingerprint([{"id": "2019-408-01", "stem": "改了"}],
                               self.ks, "bge-small", 512)
        self.assertNotEqual(a, b)

    def test_model_change(self):
        a = corpus_fingerprint(self.qs, self.ks, "bge-small", 512)
        b = corpus_fingerprint(self.qs, self.ks, "bge-m3", 512)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
