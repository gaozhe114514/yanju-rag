# -*- coding: utf-8 -*-
"""知识图谱：整份知识库的一张图，不只是 408 真题。

图里有两类东西：

1. 内置 408 语料：考点卡与真题。它们之间的线只来自考点卡 JSONL 里的
   ``related_question_ids``，不按科目、章节或相似度自动补边——图上出现的
   每一条关联都能指回某张卡片的某一项，和问答里「引用必须逐字命中原文」
   是同一套口径。
2. 用户知识库：库 → 文档 → 片段。线就是归属关系，没有推断成分；片段
   带原文起止位置，点开能高亮到原文里的那一句。

节点：
- library：知识库；
- document：资料；
- chunk：切分后的片段；
- knowledge：考点卡，一个考点一个 chunk；
- question：真题，一道题一个 chunk。

过滤规则：
- library 只留一个库时，其它库整块不画；``cs408`` 表示只看内置语料；
- subject / year / orphans 只作用于 408 语料；
- year 只作用于真题，筛选后没有剩下一道题的考点卡一并隐藏；
- 没被任何考点卡引用的真题默认不显示，include_orphans=True 时一起画出来；
- 库里的片段太多时按「每篇最多 MAX_CHUNKS_PER_DOC 段、总共最多
  MAX_CHUNK_NODES 段」截断，截掉多少在 stats 里如实记着，不假装画全了。
"""
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .catalog import subject_label

PRESET_LIBRARY = "cs408"

LIBRARY = "library"
DOCUMENT = "document"
CHUNK = "chunk"
KNOWLEDGE = "knowledge"
QUESTION = "question"

MAX_DOCUMENT_NODES = 500
MAX_CHUNK_NODES = 600
MAX_CHUNKS_PER_DOC = 40


def _text(value) -> str:
    return " ".join(str(value or "").split())


def _clip(text: str, limit: int = 80) -> str:
    text = _text(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _knowledge_node(card: Dict) -> Dict:
    subject = card.get("subject") or ""
    title = _text(card.get("title")) or str(card.get("id") or "")
    return {
        "id": "k:" + str(card.get("id") or ""),
        "ref": str(card.get("id") or ""),
        "kind": KNOWLEDGE,
        "label": title,
        "title": title,
        "subject": subject,
        "subject_label": subject_label(subject),
        "chapter": _text(card.get("chapter")),
        "year": None,
        "seq": None,
        "summary": _clip(card.get("summary"), 90),
        "stem": "",
    }


def _question_node(q: Dict) -> Dict:
    subject = q.get("subject") or ""
    qid = str(q.get("id") or "")
    year = q.get("year")
    seq = q.get("seq")
    if year and isinstance(seq, int):
        label = "%s-%02d" % (year, seq)
        title = "%s 年第 %d 题" % (year, seq)
    else:
        label = qid
        title = qid
    return {
        "id": "q:" + qid,
        "ref": qid,
        "kind": QUESTION,
        "label": label,
        "title": title,
        "subject": subject,
        "subject_label": subject_label(subject),
        "chapter": "",
        "year": int(year) if str(year or "").strip().isdigit() else None,
        "seq": seq if isinstance(seq, int) else None,
        "summary": "",
        "stem": _clip(q.get("stem"), 90),
    }


def _library_node(lid: str, name: str, docs: int, chunks: int) -> Dict:
    return {
        "id": "l:" + lid,
        "ref": lid,
        "kind": LIBRARY,
        "label": name,
        "title": name,
        "subject": "",
        "subject_label": "知识库",
        "chapter": "",
        "year": None,
        "seq": None,
        "summary": "%d 篇资料 · %d 段片段" % (docs, chunks),
        "stem": "",
        "collection": lid,
    }


def _document_node(lid: str, doc: Dict, chunk_total: int) -> Dict:
    did = str(doc.get("id") or "")
    title = _text(doc.get("title")) or "未命名资料"
    chars = int(doc.get("chars") or 0)
    return {
        "id": "d:%s:%s" % (lid, did),
        "ref": did,
        "kind": DOCUMENT,
        "label": _clip(title, 36),
        "title": title,
        "subject": "",
        "subject_label": "资料",
        "chapter": "",
        "year": None,
        "seq": None,
        "summary": "%d 段片段%s" % (
            chunk_total, (" · %d 字" % chars) if chars else ""),
        "stem": "",
        "collection": lid,
    }


def _chunk_node(lid: str, did: str, doc_title: str, chunk: Dict) -> Dict:
    xid = str(chunk.get("id") or "")
    seq = int(chunk.get("seq") or 0)
    text = _text(chunk.get("text"))
    start = chunk.get("start")
    end = chunk.get("end")
    return {
        "id": "x:%s:%s:%s" % (lid, did, xid),
        "ref": xid,
        "kind": CHUNK,
        "label": _clip(text, 14) or ("#%d" % seq),
        "title": "%s · 第 %d 段" % (doc_title, seq),
        "subject": "",
        "subject_label": "片段",
        "chapter": "",
        "year": None,
        "seq": seq,
        "summary": _clip(text, 90),
        "stem": "",
        "collection": lid,
        "document": did,
        "start": int(start) if isinstance(start, int) and start >= 0 else None,
        "end": int(end) if isinstance(end, int) and end > 0 else None,
    }


def _corpus_part(questions: Sequence[Dict], knowledge: Sequence[Dict],
                 subject: str, year: Optional[int],
                 include_orphans: bool) -> Tuple[List[Dict], List[Dict], List[Dict], Dict]:
    """内置 408 语料：考点卡 + 真题。"""
    sub = (subject or "").strip()
    year_key = str(year).strip() if year is not None and str(year).strip() else ""

    questions_by_id: Dict[str, Dict] = {}
    for q in questions:
        qid = str(q.get("id") or "")
        if not qid:
            continue
        if sub and (q.get("subject") or "") != sub:
            continue
        questions_by_id[qid] = q

    cards = []
    for card in knowledge:
        cid = str(card.get("id") or "")
        if not cid:
            continue
        if sub and (card.get("subject") or "") != sub:
            continue
        cards.append(card)

    # 卡片 → 题目：只保留语料里真实存在的题；指向未收录题目的关联单独记下来
    card_links: Dict[str, List[str]] = {}
    missing: List[Dict] = []
    for card in cards:
        cid = str(card.get("id"))
        targets = []
        for qid in _dedupe(str(x) for x in (card.get("related_question_ids") or [])):
            if qid in questions_by_id:
                targets.append(qid)
            else:
                missing.append({"card": cid, "question": qid})
        card_links[cid] = targets

    # 被卡片明确引用的题；其余都是「有题没配考点」的孤立题
    referenced = set()
    for targets in card_links.values():
        referenced.update(targets)

    def _year_of(qid):
        node = _question_node(questions_by_id[qid])
        return node["year"]

    # 年份下拉框按「当前科目下真正能画出来的题」来列，避免出现空年份
    year_source = set(questions_by_id) if include_orphans else referenced
    years = sorted({y for y in (_year_of(qid) for qid in year_source) if y})

    # 年份筛选：只筛题；卡片留不留看它还有没有剩下的题
    kept_questions = set(referenced)
    if year_key:
        kept_questions = {
            qid for qid in kept_questions if str(_year_of(qid) or "") == year_key
        }
    hidden_orphans = 0
    if not include_orphans:
        for qid in questions_by_id:
            if qid in referenced:
                continue
            if year_key and str(_year_of(qid) or "") != year_key:
                continue
            hidden_orphans += 1
    if include_orphans:
        # 开关打开时把没被引用的题也放进图里，它们会和卡片分开漂着
        kept_questions.update(
            qid for qid in questions_by_id
            if not year_key or str(_year_of(qid) or "") == year_key
        )

    kept_links: List[Tuple[str, str]] = []
    used_cards = set()
    for card in cards:
        cid = str(card.get("id"))
        for qid in card_links[cid]:
            if qid in kept_questions:
                kept_links.append((cid, qid))
                used_cards.add(cid)

    nodes: List[Dict] = []
    for card in cards:
        cid = str(card.get("id"))
        if year_key and cid not in used_cards:
            continue
        node = _knowledge_node(card)
        node["degree"] = 0
        node["orphan"] = False
        nodes.append(node)

    shown_questions = set(kept_questions)
    for qid in sorted(shown_questions):
        node = _question_node(questions_by_id[qid])
        node["degree"] = 0
        node["orphan"] = False
        nodes.append(node)

    links = [{"source": "k:" + cid, "target": "q:" + qid}
             for cid, qid in kept_links]

    subject_counts: Dict[str, Dict] = {}
    for node in nodes:
        key = node["subject"]
        row = subject_counts.setdefault(
            key, {"key": key, "label": node["subject_label"],
                  "knowledge": 0, "questions": 0})
        if node["kind"] == KNOWLEDGE:
            row["knowledge"] += 1
        else:
            row["questions"] += 1

    stats = {
        "knowledge": sum(1 for n in nodes if n["kind"] == KNOWLEDGE),
        "questions": sum(1 for n in nodes if n["kind"] == QUESTION),
        "orphans": 0,
        "hidden_orphans": hidden_orphans,
        "missing": len(missing),
        "subjects": list(subject_counts.values()),
        "years": years,
        "total_knowledge": len(cards),
        "total_questions": len(questions_by_id),
    }
    return nodes, links, missing, stats


def _library_part(libraries: Sequence[Dict],
                  selected: str) -> Tuple[List[Dict], List[Dict], List[Dict], Dict]:
    """用户知识库：库 → 资料 → 片段。

    ``selected`` 为空表示全部，``PRESET_LIBRARY`` 表示一个都不画（只看
    408 内置语料），其它值表示只画那一个库。
    """
    nodes: List[Dict] = []
    links: List[Dict] = []
    metas: List[Dict] = []
    if selected == PRESET_LIBRARY:
        selected = ""  # 只看内置语料
        draw_libs = False
    else:
        draw_libs = True

    shown_docs = 0
    shown_chunks = 0
    hidden_docs = 0
    hidden_chunks = 0
    total_docs = 0
    total_chunks = 0
    doc_budget = MAX_DOCUMENT_NODES

    for lib in libraries:
        lid = str(lib.get("id") or "")
        if not lid:
            continue
        name = _text(lib.get("name")) or lid
        docs = lib.get("documents") or []
        doc_total = int(lib.get("documents_count") or len(docs))
        chunk_total = int(lib.get("chunks_count") or 0)
        if not chunk_total:
            chunk_total = sum(
                int(d.get("chunk_total") or len(d.get("chunks") or []))
                for d in docs)
        meta = {
            "id": lid, "name": name, "builtin": bool(lib.get("builtin")),
            "backend": lib.get("backend") or "local",
            "visibility": lib.get("visibility") or "shared",
            "documents": doc_total, "chunks": chunk_total,
            "documents_shown": 0, "chunks_shown": 0,
        }
        metas.append(meta)
        if not draw_libs or (selected and selected != lid):
            continue

        total_docs += doc_total
        total_chunks += chunk_total
        nodes.append(_library_node(lid, name, doc_total, chunk_total))
        lib_docs = 0
        lib_chunks = 0

        for doc in docs:
            did = str(doc.get("id") or "")
            if not did:
                continue
            title = _text(doc.get("title")) or "未命名资料"
            chunks = doc.get("chunks") or []
            doc_chunk_total = int(doc.get("chunk_total") or len(chunks))
            if doc_budget <= 0:
                hidden_docs += 1
                hidden_chunks += doc_chunk_total
                continue

            room = max(0, MAX_CHUNK_NODES - shown_chunks)
            take = chunks[:MAX_CHUNKS_PER_DOC]
            if len(take) > room:
                take = take[:room]
            hidden_chunks += max(0, doc_chunk_total - len(take))

            doc_budget -= 1
            lib_docs += 1
            shown_docs += 1
            nodes.append(_document_node(lid, doc, doc_chunk_total))
            links.append({"source": "l:" + lid,
                          "target": "d:%s:%s" % (lid, did)})
            for chunk in take:
                node = _chunk_node(lid, did, title, chunk)
                nodes.append(node)
                links.append({"source": "d:%s:%s" % (lid, did),
                              "target": node["id"]})
                lib_chunks += 1
                shown_chunks += 1

        meta["documents_shown"] = lib_docs
        meta["chunks_shown"] = lib_chunks

    stats = {
        "documents": shown_docs,
        "chunks": shown_chunks,
        "total_documents": total_docs,
        "total_chunks": total_chunks,
        "hidden_documents": hidden_docs,
        "hidden_chunks": hidden_chunks,
        "library": selected,
    }
    return nodes, links, metas, stats


def build_graph(questions: Sequence[Dict], knowledge: Sequence[Dict],
                subject: str = "", year: Optional[int] = None,
                include_orphans: bool = False,
                libraries: Optional[Sequence[Dict]] = None,
                library: str = "") -> Dict:
    """生成图谱数据。纯函数，不碰数据库，方便单测。"""
    selected = (library or "").strip()
    libs = list(libraries or [])
    known = {str(lib.get("id") or "") for lib in libs}
    if selected and selected != PRESET_LIBRARY and selected not in known:
        selected = ""  # 库被删了或没权限：退回「全部」，别给一张空图
    with_corpus = not selected or selected == PRESET_LIBRARY

    if with_corpus:
        nodes, links, missing, stats = _corpus_part(
            questions, knowledge, subject, year, include_orphans)
    else:
        nodes, links = [], []
        missing = []
        stats = {
            "knowledge": 0, "questions": 0, "orphans": 0,
            "hidden_orphans": 0, "missing": 0, "subjects": [], "years": [],
            "total_knowledge": len(knowledge),
            "total_questions": len(questions),
        }

    lib_nodes, lib_links, lib_metas, lib_stats = _library_part(libs, selected)
    nodes.extend(lib_nodes)
    links.extend(lib_links)
    stats.update(lib_stats)

    preset_meta = {
        "id": PRESET_LIBRARY, "name": "408 题库（真题 + 考点）", "builtin": True,
        "backend": "local", "visibility": "builtin",
        "documents": len(questions) + len(knowledge),
        "chunks": len(questions) + len(knowledge),
        "documents_shown": stats["knowledge"] + stats["questions"]
        if with_corpus else 0,
        "chunks_shown": stats["knowledge"] + stats["questions"]
        if with_corpus else 0,
    }
    stats["libraries"] = [preset_meta] + lib_metas
    stats["total_libraries"] = len(lib_metas)
    stats["links"] = len(links)

    # 度数统一从最终的边上数，不另外维护一份
    degree: Dict[str, int] = {}
    for link in links:
        degree[link["source"]] = degree.get(link["source"], 0) + 1
        degree[link["target"]] = degree.get(link["target"], 0) + 1
    for node in nodes:
        node["degree"] = degree.get(node["id"], 0)
        node["orphan"] = node["kind"] == QUESTION and not node["degree"]
    stats["orphans"] = sum(1 for n in nodes if n["orphan"])

    return {"nodes": nodes, "links": links, "missing": missing, "stats": stats}
