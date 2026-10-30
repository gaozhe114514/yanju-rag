# -*- coding: utf-8 -*-
"""WeKnora 客户端：用本地假服务验协议形状，不依赖真的 WeKnora。"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kb.weknora import WeKnora, WeKnoraError, WeKnoraSearcher


class _Handler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _record(self):
        type(self).calls.append({
            "method": self.command,
            "path": self.path,
            "key": self.headers.get("X-API-Key"),
        })

    def do_GET(self):
        self._record()
        if self.path.startswith("/api/v1/knowledge-bases/kb-1/knowledge"):
            return self._json(200, {"success": True, "data": [{
                "id": "k-1", "file_name": "操作系统讲义.pdf",
                "parse_status": "completed", "chunk_count": 12,
                "file_size": 2048, "created_at": "2026-10-30T10:00:00Z",
            }], "total": 1})
        if self.path.startswith("/api/v1/knowledge/k-1/preview"):
            body = "进程与线程的区别".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/v1/chunks/k-1"):
            return self._json(200, {"success": True, "data": [{
                "id": "c-1", "seq": 0, "content": "进程是资源分配的单位。",
                "is_enabled": True, "content_revision": 3,
                "chunk_type": "text", "index_status": "ready",
            }], "total": 1})
        if self.path.startswith("/api/v1/knowledge-bases/kb-1"):
            return self._json(200, {"success": True, "data": {
                "id": "kb-1", "name": "操作系统讲义",
                "description": "期末复习", "knowledge_count": 1,
                "chunk_count": 12,
            }})
        if self.path.startswith("/api/v1/knowledge-bases"):
            return self._json(200, {"success": True, "data": [{
                "id": "kb-1", "name": "操作系统讲义",
                "description": "期末复习", "knowledge_count": 1,
                "chunk_count": 12,
            }]})
        if self.path.startswith("/api/v1/missing"):
            return self._json(404, {"success": False, "message": "KB not found"})
        return self._json(500, {"success": False, "message": "boom"})

    def do_POST(self):
        self._record()
        body = self._body()
        if self.path.startswith("/api/v1/knowledge-search"):
            return self._json(200, {"success": True, "data": [{
                "id": "c-9", "content": "线程是调度的基本单位。",
                "knowledge_id": "k-1", "knowledge_title": "操作系统讲义",
                "score": 0.87, "chunk_type": "text",
            }]})
        if self.path.startswith("/api/v1/knowledge-bases/kb-1/knowledge/file"):
            return self._json(200, {"success": True, "data": {
                "id": "k-2", "file_name": "线程.pdf",
                "parse_status": "pending", "chunk_count": 0,
            }})
        if self.path.startswith("/api/v1/knowledge-bases/kb-1/knowledge/manual"):
            return self._json(200, {"success": True, "data": {
                "id": "k-3", "title": "手写笔记", "parse_status": "completed",
            }})
        if self.path.startswith("/api/v1/knowledge-bases"):
            return self._json(201, {"success": True, "data": {
                "id": "kb-2", "name": "新库", "description": "",
            }})
        if self.path.startswith("/api/v1/knowledge/k-1/reparse"):
            return self._json(200, {"success": True, "data": {
                "id": "k-1", "parse_status": "processing",
            }})
        return self._json(500, {"success": False, "message": "boom"})

    def do_PUT(self):
        self._record()
        self._body()
        if self.path.startswith("/api/v1/chunks/k-1/c-1"):
            return self._json(200, {"success": True, "data": {
                "id": "c-1", "seq": 0, "content": "改过的内容。",
                "content_revision": 4,
            }})
        return self._json(500, {"success": False, "message": "boom"})

    def do_DELETE(self):
        self._record()
        if self.path.startswith("/api/v1/chunks/k-1/c-1"):
            return self._json(200, {"success": True, "message": "Chunk deleted"})
        if self.path.startswith("/api/v1/knowledge/k-1"):
            return self._json(200, {"success": True, "message": "Delete task submitted"})
        return self._json(404, {"success": False, "message": "not found"})


class WeKnoraClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.calls = []
        self.client = WeKnora(self.base, "test-key")

    def test_list_collections_normalized(self):
        items = self.client.list_collections()
        self.assertEqual(items[0]["id"], "kb-1")
        self.assertEqual(items[0]["name"], "操作系统讲义")
        self.assertEqual(items[0]["documents"], 1)
        self.assertEqual(items[0]["chunks"], 12)
        self.assertEqual(items[0]["backend"], "weknora")
        self.assertEqual(_Handler.calls[0]["key"], "test-key")

    def test_documents_and_status(self):
        docs = self.client.list_documents("kb-1")
        self.assertEqual(docs[0]["title"], "操作系统讲义.pdf")
        self.assertEqual(docs[0]["status"], "completed")
        self.assertEqual(docs[0]["chunks"], 12)

    def test_add_text_upload_reparse(self):
        doc = self.client.add_text("kb-1", "手写笔记", "正文至少二十个字……")
        self.assertEqual(doc["id"], "k-3")
        up = self.client.upload_document("kb-1", "线程.pdf", b"%PDF-1.4 demo")
        self.assertEqual(up["id"], "k-2")
        self.assertEqual(up["status"], "pending")
        again = self.client.reparse_document("k-1")
        self.assertEqual(again["status"], "processing")

    def test_chunks_crud(self):
        chunks = self.client.list_chunks("k-1")
        self.assertEqual(chunks[0]["text"], "进程是资源分配的单位。")
        self.assertEqual(chunks[0]["revision"], 3)
        edited = self.client.update_chunk("k-1", "c-1", "改过的内容。", 3)
        self.assertEqual(edited["revision"], 4)
        self.assertTrue(self.client.delete_chunk("k-1", "c-1"))

    def test_search_and_searcher(self):
        rows = self.client.search("kb-1", "线程是什么", 5)
        self.assertEqual(rows[0]["text"], "线程是调度的基本单位。")
        self.assertEqual(rows[0]["title"], "操作系统讲义")
        hits = WeKnoraSearcher(self.client, "kb-1", "操作系统讲义").search("线程是什么")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].chunk.kind, "doc")
        self.assertIn("线程", hits[0].chunk.text)
        self.assertEqual(hits[0].chunk.source_ref["document_id"], "k-1")
        self.assertAlmostEqual(hits[0].score, 0.87)

    def test_preview(self):
        self.assertIn("进程", self.client.document_preview("k-1"))

    def test_error_mapping(self):
        with self.assertRaises(WeKnoraError) as ctx:
            self.client._request("GET", "/api/v1/missing")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(WeKnoraError) as ctx:
            self.client._request("GET", "/api/v1/boom")
        self.assertEqual(ctx.exception.status, 502)

    def test_unconfigured(self):
        self.assertFalse(WeKnora("").configured)


if __name__ == "__main__":
    unittest.main()
