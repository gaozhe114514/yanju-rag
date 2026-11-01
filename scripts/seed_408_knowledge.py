# -*- coding: utf-8 -*-
"""把 408 四科课程笔记写进共享知识库「408 考研知识」。

资料来自 MIT 许可的开源笔记 CS408_Note（William Trouvaille），
已放在 data/curated/408_notes/：四个科目各几篇 markdown，不含真题。

用法：
    python scripts/seed_408_knowledge.py --dry-run   # 只统计，不写库
    python scripts/seed_408_knowledge.py             # 写库，可重复执行

有 DATABASE_URL 时写 Postgres（生产在容器里跑）；没有时写内存 JSON，
方便本地验证。重复执行按正文 sha256 去重，不会把同一篇灌两遍。
"""
import argparse
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kb.library import MAX_DOC_CHARS, Library  # noqa: E402

COLLECTION_NAME = "408 考研知识"
COLLECTION_DESC = (
    "408 四科课程笔记：数据结构、计算机组成原理、操作系统、计算机网络；"
    "不含真题。源自 MIT 许可的开源笔记 WilliamTrouvaille/CS408_Note")
NOTES_DIR = ROOT / "data" / "curated" / "408_notes"
SUBJECTS = (
    ("DataStructure", "数据结构"),
    ("ComputerOrganization", "计算机组成原理"),
    ("OperateSystem", "操作系统"),
    ("ComputerNetwork", "计算机网络"),
)
# 单篇留出余量，正文 ≤ 45000 字，远低于库上限 50000
PART_CHARS = 45000

_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def normalize(text: str) -> str:
    """和 Library.add_document 用同一套换行归一化，保证 sha 对得上。"""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def clean_markdown(text: str) -> str:
    """去掉图片外链：图在 jsdelivr / GitHub，检索用不到，留着只是噪声。"""
    text = normalize(text)
    text = _IMAGE_RE.sub("", text)
    text = _IMG_TAG_RE.sub("", text)
    lines = [line.rstrip() for line in text.split("\n")]
    cleaned = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def first_heading(text: str) -> str:
    m = _H1_RE.search(text)
    return m.group(1).strip() if m else ""


def split_document(text: str, limit: int = PART_CHARS) -> List[str]:
    """按段落累积切分，每段不超过 limit；所有段落原样保留。"""
    text = normalize(text)
    if len(text) <= limit:
        return [text] if text else []
    paras = text.split("\n\n")
    parts: List[str] = []
    buf = ""
    for para in paras:
        para = para.strip("\n")
        if not para:
            continue
        # 单段就超限（几乎没有）：按字符硬切
        while len(para) > limit:
            head, para = para[:limit], para[limit:]
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(head)
        candidate = (buf + "\n\n" + para) if buf else para
        if len(candidate) > limit:
            parts.append(buf)
            buf = para
        else:
            buf = candidate
    if buf:
        parts.append(buf)
    return parts


def build_documents(base: Path = NOTES_DIR) -> List[Dict]:
    """读出所有课程笔记，返回待入库的 {title, text, source} 列表。"""
    docs: List[Dict] = []
    for folder, subject in SUBJECTS:
        subject_dir = base / folder
        if not subject_dir.is_dir():
            continue
        for path in sorted(subject_dir.glob("*.md")):
            raw = path.read_text(encoding="utf-8")
            text = clean_markdown(raw)
            if len(text) < 20:
                continue
            heading = first_heading(text) or path.stem
            parts = split_document(text)
            source = "%s/%s/%s · CS408_Note（MIT）" % (
                "CS408_Note", folder, path.name)
            for seq, part in enumerate(parts, 1):
                title = heading if len(parts) == 1 else "%s（%d/%d）" % (
                    heading, seq, len(parts))
                title = ("%s · %s" % (subject, title))[:80]
                docs.append({"title": title, "text": part,
                             "source": source, "subject": subject,
                             "path": str(path.relative_to(base))})
    license_path = base / "LICENSE.txt"
    if license_path.is_file():
        text = normalize(license_path.read_text(encoding="utf-8"))
        docs.append({
            "title": "来源与许可 · CS408_Note（MIT）",
            "text": "%s\n\n上游仓库：https://github.com/WilliamTrouvaille/CS408_Note\n\n%s"
                    % ("本知识库的课程笔记来自 CS408_Note，按 MIT 许可使用：", text),
            "source": "CS408_Note/LICENSE · MIT",
            "subject": "来源",
            "path": "LICENSE.txt",
        })
    return docs


def sha256_text(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def find_collection(lib: Library) -> Dict:
    for coll in lib.list_collections():
        if coll.get("name") == COLLECTION_NAME \
                and (coll.get("visibility") or "shared") == "shared":
            return coll
    return {}


def seed(lib: Optional[Library], docs: Sequence[Dict],
         dry_run: bool = False) -> Dict:
    """写库并返回统计；按正文 sha 去重，可反复执行。"""
    if dry_run:
        return {"collection_id": "(待建)", "created": True,
                "added": len(docs), "skipped": 0,
                "chars": sum(len(d["text"]) for d in docs)}
    coll = find_collection(lib)
    created = False
    if not coll:
        coll = lib.create_collection(COLLECTION_NAME, COLLECTION_DESC,
                                     visibility="shared")
        created = True
    cid = coll["id"]
    existing = {d.get("sha256") for d in lib.list_documents(cid)}
    added = skipped = chars = 0
    for doc in docs:
        sha = sha256_text(doc["text"])
        if sha in existing:
            skipped += 1
            continue
        if not dry_run:
            lib.add_document(cid, doc["text"], title=doc["title"],
                             source=doc["source"])
        existing.add(sha)
        added += 1
        chars += len(doc["text"])
    return {"collection_id": cid, "created": created, "added": added,
            "skipped": skipped, "chars": chars}


def _make_library() -> Library:
    from db.store import Store

    store = Store.from_env()
    embedder = None
    if store is not None:
        store.init_schema()
        # 与 API 同款本地向量模型；容器里已烘好，不会联网下载
        from retrieval.vector import BgeEmbedder
        embedder = BgeEmbedder()
    state_path = str(ROOT / "data/work/kb/library.json")
    return Library(store=store, embedder=embedder,
                   mode=os.environ.get("KB_MODE", "hybrid"),
                   state_path=state_path)


def main(argv: Sequence[str] = None) -> int:
    ap = argparse.ArgumentParser(description="把 408 课程笔记写进共享知识库")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    ap.add_argument("--notes-dir", default=str(NOTES_DIR))
    args = ap.parse_args(argv)

    docs = build_documents(Path(args.notes_dir))
    if not docs:
        print("没有找到课程笔记：%s" % args.notes_dir, file=sys.stderr)
        return 1
    total = sum(len(d["text"]) for d in docs)
    print("待入库 %d 篇，共 %d 字（上限 %d 字/篇）"
          % (len(docs), total, MAX_DOC_CHARS))
    if args.dry_run:
        stats = seed(None, docs, dry_run=True)
    else:
        lib = _make_library()
        stats = seed(lib, docs)
    print("知识库：%s%s" % (COLLECTION_NAME, "（新建）" if stats["created"] else ""))
    print("新增 %d 篇，跳过 %d 篇（已在库里），新增正文 %d 字"
          % (stats["added"], stats["skipped"], stats["chars"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
