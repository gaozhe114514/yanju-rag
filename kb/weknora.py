# -*- coding: utf-8 -*-
"""WeKnora 客户端：把知识库的「进料 / 切片 / 检索」交给腾讯开源的 WeKnora。

只借它的文档解析、切分和召回，不借它的问答生成——引用必须逐字命中、
资料没覆盖必须拒答这两条规则仍由本项目的闸门、生成与校验负责。

配置（都在服务端环境变量里，不下发浏览器）：
  WEKNORA_BASE_URL   例如 http://127.0.0.1:8080
  WEKNORA_API_KEY    WeKnora 里创建的 API Key
  WEKNORA_TIMEOUT    单次请求超时秒数，默认 20
  WEKNORA_RERANK     1 = 检索时让 WeKnora 走 rerank（需要配好重排模型）

没有配置 WEKNORA_BASE_URL 时，本项目继续用内置的 Postgres + BM25 链路。
"""
import json
import os
import uuid
from typing import Dict, List, Optional

import urllib.error
import urllib.parse
import urllib.request

from retrieval.chunk import Chunk
from retrieval.search import SearchHit


class WeKnoraError(ValueError):
    """带 HTTP 状态码的调用失败；接口层据此映射 400/404/502。"""

    def __init__(self, message: str, status: int = 502):
        ValueError.__init__(self, message)
        self.status = status


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first(row: Dict, *keys):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


class WeKnora:
    """最小的 REST 客户端：只用标准库，失败一律抛 WeKnoraError。"""

    def __init__(self, base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 timeout: Optional[float] = None):
        self.base_url = (base_url if base_url is not None
                         else os.environ.get("WEKNORA_BASE_URL", "")).rstrip("/")
        self.api_key = (api_key if api_key is not None
                        else os.environ.get("WEKNORA_API_KEY", ""))
        if timeout is None:
            timeout = float(os.environ.get("WEKNORA_TIMEOUT", "20") or 20)
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    # ---------- HTTP ----------

    def _headers(self, extra: Optional[Dict] = None) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method: str, path: str, body=None,
                 params: Optional[Dict] = None, raw: bool = False):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            message = _error_message(detail) or ("WeKnora 返回 HTTP %s" % e.code)
            raise WeKnoraError(message, e.code if e.code < 500 else 502)
        except (urllib.error.URLError, OSError) as e:
            raise WeKnoraError("连不上 WeKnora（%s）" % type(e).__name__, 502)
        if raw:
            return payload
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise WeKnoraError("WeKnora 返回的不是 JSON", 502)
        if isinstance(parsed, dict) and parsed.get("success") is False:
            raise WeKnoraError(str(parsed.get("message") or "WeKnora 调用失败"), 502)
        return parsed.get("data") if isinstance(parsed, dict) else parsed

    def _upload(self, path: str, filename: str, content: bytes,
                fields: Optional[Dict[str, str]] = None) -> Dict:
        boundary = "----yinzheng" + uuid.uuid4().hex
        chunks = []
        for name, value in (fields or {}).items():
            chunks.append(("--%s\r\n" % boundary).encode("utf-8"))
            chunks.append(('Content-Disposition: form-data; name="%s"\r\n\r\n'
                           % name).encode("utf-8"))
            chunks.append(str(value).encode("utf-8"))
            chunks.append(b"\r\n")
        chunks.append(("--%s\r\n" % boundary).encode("utf-8"))
        chunks.append(('Content-Disposition: form-data; name="file"; filename="%s"\r\n'
                       % filename.replace('"', "_")).encode("utf-8"))
        chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
        chunks.append(content)
        chunks.append(b"\r\n")
        chunks.append(("--%s--\r\n" % boundary).encode("utf-8"))
        data = b"".join(chunks)
        headers = self._headers({
            "Content-Type": "multipart/form-data; boundary=%s" % boundary,
            "Content-Length": str(len(data)),
        })
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 60)) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            message = _error_message(detail) or ("WeKnora 返回 HTTP %s" % e.code)
            raise WeKnoraError(message, e.code if e.code < 500 else 502)
        except (urllib.error.URLError, OSError) as e:
            raise WeKnoraError("上传时连不上 WeKnora（%s）" % type(e).__name__, 502)
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise WeKnoraError("WeKnora 返回的不是 JSON", 502)
        if isinstance(parsed, dict) and parsed.get("success") is False:
            raise WeKnoraError(str(parsed.get("message") or "上传失败"), 502)
        return parsed.get("data") if isinstance(parsed, dict) else parsed

    # ---------- 知识库 ----------

    def health(self) -> Dict:
        try:
            self._request("GET", "/api/v1/knowledge-bases",
                          params={"page": 1, "page_size": 1})
            return {"ok": True}
        except WeKnoraError as e:
            return {"ok": False, "error": str(e)}

    def list_collections(self) -> List[Dict]:
        data = self._request("GET", "/api/v1/knowledge-bases",
                             params={"page": 1, "page_size": 100})
        rows = data if isinstance(data, list) else (data or {}).get("items") or []
        return [normalize_collection(r) for r in rows if isinstance(r, dict)]

    def get_collection(self, cid: str) -> Optional[Dict]:
        try:
            data = self._request("GET", "/api/v1/knowledge-bases/%s" % cid)
        except WeKnoraError as e:
            if e.status == 404:
                return None
            raise
        return normalize_collection(data or {})

    def create_collection(self, name: str, description: str = "") -> Dict:
        data = self._request("POST", "/api/v1/knowledge-bases",
                             {"name": name, "description": description,
                              "type": "document"})
        return normalize_collection(data or {})

    def delete_collection(self, cid: str) -> bool:
        try:
            self._request("DELETE", "/api/v1/knowledge-bases/%s" % cid)
            return True
        except WeKnoraError as e:
            if e.status == 404:
                return False
            raise

    # ---------- 文档 ----------

    def list_documents(self, cid: str, page: int = 1, page_size: int = 100) -> List[Dict]:
        data = self._request("GET", "/api/v1/knowledge-bases/%s/knowledge" % cid,
                             params={"page": page, "page_size": page_size})
        rows = data if isinstance(data, list) else (data or {}).get("items") or []
        return [normalize_document(r) for r in rows if isinstance(r, dict)]

    def document_count(self, cid: str) -> int:
        try:
            data = self._request("GET", "/api/v1/knowledge-bases/%s/knowledge" % cid,
                                 params={"page": 1, "page_size": 1})
        except WeKnoraError:
            return 0
        if isinstance(data, dict):
            total = _as_int(data.get("total"), -1)
            if total >= 0:
                return total
            rows = data.get("items") or []
        else:
            rows = data or []
        return len(rows) if isinstance(rows, list) else 0

    def add_text(self, cid: str, title: str, text: str) -> Dict:
        data = self._request("POST", "/api/v1/knowledge-bases/%s/knowledge/manual" % cid,
                             {"title": title or "未命名资料",
                              "content": text, "status": "publish"})
        return normalize_document(data or {})

    def upload_document(self, cid: str, filename: str, content: bytes,
                        title: str = "") -> Dict:
        fields = {}
        if title:
            fields["fileName"] = title
        data = self._upload("/api/v1/knowledge-bases/%s/knowledge/file" % cid,
                            filename, content, fields)
        return normalize_document(data or {})

    def delete_document(self, knowledge_id: str) -> bool:
        try:
            self._request("DELETE", "/api/v1/knowledge/%s" % knowledge_id)
            return True
        except WeKnoraError as e:
            if e.status == 404:
                return False
            raise

    def reparse_document(self, knowledge_id: str) -> Dict:
        data = self._request("POST", "/api/v1/knowledge/%s/reparse" % knowledge_id,
                             {})
        return normalize_document(data or {})

    def document_preview(self, knowledge_id: str) -> str:
        """解析后的正文（可能是纯文本或 HTML）；拿不到就返回空串。"""
        try:
            raw = self._request("GET", "/api/v1/knowledge/%s/preview" % knowledge_id,
                                raw=True)
        except WeKnoraError as e:
            if e.status == 404:
                return ""
            raise
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        return str(raw or "")

    # ---------- 片段 ----------

    def list_chunks(self, knowledge_id: str, page: int = 1,
                    page_size: int = 100) -> List[Dict]:
        data = self._request("GET", "/api/v1/chunks/%s" % knowledge_id,
                             params={"page": page, "page_size": page_size})
        rows = data if isinstance(data, list) else (data or {}).get("items") or []
        return [normalize_chunk(r, i) for i, r in enumerate(rows)
                if isinstance(r, dict)]

    def update_chunk(self, knowledge_id: str, chunk_id: str, content: str,
                     revision: Optional[int] = None) -> Dict:
        body = {"content": content}
        if revision is not None:
            body["expected_revision"] = int(revision)
        data = self._request(
            "PUT", "/api/v1/chunks/%s/%s" % (knowledge_id, chunk_id), body)
        return normalize_chunk(data or {}, 0)

    def delete_chunk(self, knowledge_id: str, chunk_id: str) -> bool:
        try:
            self._request("DELETE",
                          "/api/v1/chunks/%s/%s" % (knowledge_id, chunk_id))
            return True
        except WeKnoraError as e:
            if e.status == 404:
                return False
            raise

    # ---------- 检索 ----------

    def search(self, cid: str, query: str, limit: int = 8) -> List[Dict]:
        body = {"query": query, "knowledge_base_ids": [cid],
                "match_count": max(1, min(50, int(limit)))}
        if os.environ.get("WEKNORA_RERANK", "").strip() in ("1", "true", "yes"):
            body["rerank"] = {"enabled": True}
        data = self._request("POST", "/api/v1/knowledge-search", body)
        rows = data if isinstance(data, list) else (data or {}).get("items") or []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            out.append({
                "id": str(row.get("id") or row.get("chunk_id") or ""),
                "text": content,
                "score": _score(row.get("score")),
                "knowledge_id": str(row.get("knowledge_id") or ""),
                "title": str(row.get("knowledge_title") or ""),
                "chunk_type": str(row.get("chunk_type") or "text"),
            })
        return out


def _score(value) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score))


def _error_message(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return raw.strip()[:200]
    if isinstance(parsed, dict):
        return str(parsed.get("message") or parsed.get("error")
                   or parsed.get("detail") or "")[:200]
    return ""


def normalize_collection(row: Dict) -> Dict:
    return {
        "id": str(_first(row, "id", "kb_id") or ""),
        "name": str(_first(row, "name", "title") or "未命名知识库"),
        "description": str(row.get("description") or ""),
        "builtin": False,
        "backend": "weknora",
        "documents": _as_int(_first(row, "knowledge_count", "document_count",
                                    "doc_count")),
        "chunks": _as_int(_first(row, "chunk_count", "chunks")),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "embedding_model_id": str(row.get("embedding_model_id") or ""),
    }


def normalize_document(row: Dict) -> Dict:
    status = str(_first(row, "parse_status", "status", "process_status") or "")
    title = str(_first(row, "file_name", "title", "name") or "未命名资料")
    return {
        "id": str(_first(row, "id", "knowledge_id") or ""),
        "title": title,
        "chars": _as_int(_first(row, "char_count", "chars", "text_length")),
        "chunks": _as_int(_first(row, "chunk_count", "chunks")),
        "bytes": _as_int(_first(row, "file_size", "size")),
        "status": status,
        "error": str(row.get("error") or row.get("error_message") or ""),
        "created_at": str(row.get("created_at") or ""),
        "folder": str(row.get("folder_path") or ""),
    }


def normalize_chunk(row: Dict, index: int) -> Dict:
    text = str(_first(row, "content", "text", "content_text") or "")
    return {
        "id": str(_first(row, "id", "chunk_id") or ""),
        "seq": _as_int(_first(row, "seq", "chunk_index", "index"), index),
        "text": text,
        "enabled": bool(row.get("is_enabled", True)),
        "revision": _as_int(_first(row, "content_revision", "revision")),
        "chunk_type": str(row.get("chunk_type") or "text"),
        "status": str(_first(row, "index_status", "status") or ""),
    }


class WeKnoraSearcher:
    """把 WeKnora 的召回结果接成 AnswerPipeline 认识的 searcher。

    AnswerPipeline 只要求 search(query) 返回带 chunk/score 的命中；
    闸门、生成、引用逐字校验一行不改。
    """

    def __init__(self, client: WeKnora, cid: str, name: str = "",
                 limit: int = 8):
        self.client = client
        self.cid = cid
        self.name = name
        self.limit = limit

    def search(self, query: str, skip_rerank: bool = False) -> List[SearchHit]:
        # WeKnora 自己完成召回与排序，没有本地精排开关；参数只为兼容
        # AnswerPipeline / Agent 的统一调用签名，传什么都不影响这里。
        rows = self.client.search(self.cid, query, self.limit)
        hits = []
        for i, row in enumerate(rows):
            text = row.get("text") or ""
            if not text:
                continue
            title = (row.get("title") or "")[:120]
            chunk = Chunk(
                id=row.get("id") or "weknora-%d" % i,
                seq=i,
                text=text,
                kind="doc",
                title=title,
                subject=self.name,
                source_ref={
                    "document_id": row.get("knowledge_id") or "",
                    "document_title": row.get("title") or "",
                    "backend": "weknora",
                },
            )
            hits.append(SearchHit(chunk, float(row.get("score") or 0.0)))
        return hits
