# -*- coding: utf-8 -*-
"""知识图谱测试：节点、关联边、科目/年份过滤与孤立题。"""
import unittest

from api.graph import (MAX_CHUNK_NODES, MAX_CHUNKS_PER_DOC,
                       MAX_DOCUMENT_NODES, build_graph)


def q(qid="2024-408-01", year=2024, seq=1, subject="ds", stem="题干"):
    return {"id": qid, "year": year, "seq": seq, "subject": subject, "stem": stem}


def card(cid="ds-tree", subject="ds", chapter="树与二叉树", title="二叉树的遍历",
         related=("2024-408-01",), summary="总结"):
    return {
        "id": cid, "subject": subject, "chapter": chapter, "title": title,
        "summary": summary, "related_question_ids": list(related),
    }


def ids(graph):
    return sorted(n["id"] for n in graph["nodes"])


def by_id(graph, node_id):
    for node in graph["nodes"]:
        if node["id"] == node_id:
            return node
    raise AssertionError("没有节点 " + node_id)


class BuildTest(unittest.TestCase):
    def test_basic_nodes_and_links(self):
        graph = build_graph(
            [q(), q("2024-408-02", seq=2, subject="os")],
            [card(), card("os-proc", "os", "进程与线程", "进程状态",
                          ["2024-408-02"])])
        self.assertEqual(ids(graph), ["k:ds-tree", "k:os-proc",
                                      "q:2024-408-01", "q:2024-408-02"])
        self.assertEqual(graph["links"], [
            {"source": "k:ds-tree", "target": "q:2024-408-01"},
            {"source": "k:os-proc", "target": "q:2024-408-02"},
        ])
        node = by_id(graph, "q:2024-408-01")
        self.assertEqual(node["kind"], "question")
        self.assertEqual(node["label"], "2024-01")
        self.assertEqual(node["title"], "2024 年第 1 题")
        self.assertEqual(node["degree"], 1)
        self.assertEqual(by_id(graph, "k:ds-tree")["kind"], "knowledge")
        self.assertEqual(by_id(graph, "k:ds-tree")["subject_label"], "数据结构")

    def test_duplicate_related_ids_make_one_edge(self):
        graph = build_graph([q()], [card(related=["2024-408-01", "2024-408-01"])])
        self.assertEqual(len(graph["links"]), 1)
        self.assertEqual(by_id(graph, "k:ds-tree")["degree"], 1)

    def test_missing_question_is_logged_not_drawn(self):
        graph = build_graph([q()], [card(related=["2024-408-01", "1999-408-99"])])
        self.assertEqual(graph["missing"],
                         [{"card": "ds-tree", "question": "1999-408-99"}])
        self.assertEqual(graph["stats"]["missing"], 1)
        self.assertNotIn("q:1999-408-99", ids(graph))
        # 对不上的关联不算连边
        self.assertEqual(len(graph["links"]), 1)

    def test_shared_question_counts_two_degrees(self):
        graph = build_graph(
            [q()],
            [card(), card("ds-sort", "ds", "排序", "排序", ["2024-408-01"])])
        self.assertEqual(by_id(graph, "q:2024-408-01")["degree"], 2)
        self.assertEqual(graph["stats"]["links"], 2)


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.questions = [
            q("2019-408-01", 2019, 1, "ds"),
            q("2024-408-01", 2024, 1, "ds"),
            q("2024-408-02", 2024, 2, "os"),
            q("2024-408-03", 2024, 3, "ds"),  # 没有卡片引用
        ]
        self.cards = [
            card("ds-time", related=["2019-408-01"]),
            card("ds-tree", related=["2024-408-01"]),
            card("os-proc", "os", "进程与线程", "进程", ["2024-408-02"]),
        ]

    def test_subject_filter(self):
        graph = build_graph(self.questions, self.cards, subject="os")
        self.assertEqual(ids(graph), ["k:os-proc", "q:2024-408-02"])
        self.assertEqual(graph["stats"]["years"], [2024])

    def test_year_filter_hides_cards_without_questions(self):
        graph = build_graph(self.questions, self.cards, year=2019)
        self.assertEqual(ids(graph), ["k:ds-time", "q:2019-408-01"])
        # 年份下拉框仍然列出所有画得出来的年份
        self.assertEqual(graph["stats"]["years"], [2019, 2024])

    def test_year_filter_keeps_years_of_other_subjects_out(self):
        graph = build_graph(self.questions, self.cards, subject="ds", year=2024)
        self.assertEqual(ids(graph), ["k:ds-tree", "q:2024-408-01"])

    def test_orphans_hidden_by_default_and_counted(self):
        graph = build_graph(self.questions, self.cards)
        self.assertNotIn("q:2024-408-03", ids(graph))
        self.assertEqual(graph["stats"]["hidden_orphans"], 1)
        self.assertEqual(graph["stats"]["orphans"], 0)

    def test_orphans_shown_when_asked(self):
        graph = build_graph(self.questions, self.cards, include_orphans=True)
        node = by_id(graph, "q:2024-408-03")
        self.assertTrue(node["orphan"])
        self.assertEqual(node["degree"], 0)
        self.assertEqual(graph["stats"]["orphans"], 1)
        self.assertEqual(graph["stats"]["hidden_orphans"], 0)

    def test_orphans_follow_year_filter(self):
        graph = build_graph(self.questions, self.cards, year=2019,
                            include_orphans=True)
        self.assertNotIn("q:2024-408-03", ids(graph))

    def test_subject_counts(self):
        graph = build_graph(self.questions, self.cards)
        counts = {row["key"]: row for row in graph["stats"]["subjects"]}
        self.assertEqual(counts["ds"]["knowledge"], 2)
        self.assertEqual(counts["ds"]["questions"], 2)
        self.assertEqual(counts["os"]["questions"], 1)


class EdgeCaseTest(unittest.TestCase):
    def test_empty_corpus(self):
        graph = build_graph([], [])
        self.assertEqual(graph["nodes"], [])
        self.assertEqual(graph["links"], [])
        self.assertEqual(graph["stats"]["years"], [])
        self.assertEqual(graph["stats"]["links"], 0)

    def test_card_without_links_is_drawn(self):
        graph = build_graph([q()], [card(related=[])])
        self.assertEqual(ids(graph), ["k:ds-tree"])
        self.assertEqual(by_id(graph, "k:ds-tree")["degree"], 0)
        self.assertEqual(graph["stats"]["links"], 0)

    def test_ids_are_prefixed_and_unique(self):
        graph = build_graph([q()], [card()])
        node_ids = [n["id"] for n in graph["nodes"]]
        self.assertEqual(len(node_ids), len(set(node_ids)))
        # 卡片和题目共用同一个 id 也不会撞车
        graph = build_graph([q("same", 2024, 1)],
                            [card("same", related=["same"])])
        self.assertEqual(ids(graph), ["k:same", "q:same"])
        self.assertEqual(len(graph["links"]), 1)

    def test_degree_counts_links(self):
        graph = build_graph(
            [q("2024-408-01"), q("2024-408-02", seq=2)],
            [card(related=["2024-408-01", "2024-408-02"])])
        self.assertEqual(by_id(graph, "k:ds-tree")["degree"], 2)


def doc(did="doc-1", title="讲义", chunks=3, chars=300):
    return {
        "id": did, "title": title, "chars": chars, "chunk_total": chunks,
        "chunks": [{"id": "%s-%03d" % (did, i), "seq": i,
                    "text": "第 %d 段内容" % i,
                    "start": i * 10, "end": i * 10 + 9}
                   for i in range(chunks)],
    }


def lib(cid="lib-1", name="个人知识库", docs=None, visibility="private"):
    return {"id": cid, "name": name, "visibility": visibility,
            "documents": docs or []}


class LibraryTest(unittest.TestCase):
    """整份知识库：库 → 资料 → 片段。"""

    def test_library_docs_and_chunks_are_drawn(self):
        graph = build_graph([], [], libraries=[lib(docs=[doc()])])
        self.assertEqual(ids(graph), [
            "d:lib-1:doc-1", "l:lib-1",
            "x:lib-1:doc-1:doc-1-000", "x:lib-1:doc-1:doc-1-001",
            "x:lib-1:doc-1:doc-1-002"])
        self.assertEqual(len(graph["links"]), 4)
        self.assertEqual(by_id(graph, "l:lib-1")["kind"], "library")
        self.assertEqual(by_id(graph, "l:lib-1")["degree"], 1)
        self.assertEqual(by_id(graph, "d:lib-1:doc-1")["kind"], "document")
        self.assertEqual(by_id(graph, "d:lib-1:doc-1")["degree"], 4)
        chunk = by_id(graph, "x:lib-1:doc-1:doc-1-001")
        self.assertEqual(chunk["kind"], "chunk")
        self.assertEqual(chunk["seq"], 1)
        self.assertEqual(chunk["document"], "doc-1")
        self.assertEqual((chunk["start"], chunk["end"]), (10, 19))
        self.assertEqual(graph["stats"]["documents"], 1)
        self.assertEqual(graph["stats"]["chunks"], 3)
        self.assertEqual(graph["stats"]["hidden_chunks"], 0)

    def test_library_filter_shows_only_that_library(self):
        graph = build_graph(
            [], [], libraries=[
                lib("lib-1", "个人知识库", [doc()]),
                lib("lib-2", "公司知识库", [doc("doc-9", "手册", 2)],
                    visibility="shared")],
            library="lib-2")
        self.assertEqual(ids(graph), [
            "d:lib-2:doc-9", "l:lib-2",
            "x:lib-2:doc-9:doc-9-000", "x:lib-2:doc-9:doc-9-001"])
        # 下拉框仍要列出所有能看的库
        names = [row["name"] for row in graph["stats"]["libraries"]]
        self.assertEqual(names, ["408 题库（真题 + 考点）", "个人知识库", "公司知识库"])
        self.assertEqual(graph["stats"]["documents"], 1)
        self.assertEqual(graph["stats"]["total_documents"], 1)

    def test_corpus_filter_excludes_libraries(self):
        graph = build_graph([q()], [card()], libraries=[lib(docs=[doc()])],
                            library="cs408")
        self.assertEqual(ids(graph), ["k:ds-tree", "q:2024-408-01"])
        self.assertEqual(graph["stats"]["documents"], 0)
        self.assertEqual(graph["stats"]["libraries"][0]["id"], "cs408")

    def test_library_filter_excludes_corpus(self):
        graph = build_graph([q()], [card()], libraries=[lib(docs=[doc()])],
                            library="lib-1")
        self.assertNotIn("k:ds-tree", ids(graph))
        self.assertEqual(graph["stats"]["knowledge"], 0)
        self.assertEqual(graph["stats"]["years"], [])

    def test_unknown_library_falls_back_to_everything(self):
        graph = build_graph([q()], [card()], libraries=[lib(docs=[doc()])],
                            library="lib-gone")
        self.assertIn("k:ds-tree", ids(graph))
        self.assertIn("l:lib-1", ids(graph))
        self.assertEqual(graph["stats"]["library"], "")

    def test_empty_library_still_gets_a_node(self):
        graph = build_graph([], [], libraries=[lib(docs=[])])
        self.assertEqual(ids(graph), ["l:lib-1"])
        self.assertEqual(graph["links"], [])
        self.assertEqual(graph["stats"]["documents"], 0)

    def test_chunks_per_document_are_capped_and_counted(self):
        graph = build_graph([], [], libraries=[lib(docs=[doc(chunks=60)])])
        chunks = [n for n in graph["nodes"] if n["kind"] == "chunk"]
        self.assertEqual(len(chunks), MAX_CHUNKS_PER_DOC)
        self.assertEqual(graph["stats"]["hidden_chunks"], 20)

    def test_total_chunk_budget_is_respected(self):
        docs = [doc("doc-%02d" % i, "资料 %d" % i, 40) for i in range(20)]
        graph = build_graph([], [], libraries=[lib(docs=docs)])
        chunks = [n for n in graph["nodes"] if n["kind"] == "chunk"]
        self.assertEqual(len(chunks), MAX_CHUNK_NODES)
        self.assertEqual(graph["stats"]["hidden_chunks"],
                         20 * 40 - MAX_CHUNK_NODES)
        self.assertEqual(graph["stats"]["documents"], 20)

    def test_chunk_ids_do_not_collide_across_libraries(self):
        graph = build_graph([], [], libraries=[
            lib("lib-1", "甲", [doc()]), lib("lib-2", "乙", [doc()])])
        node_ids = [n["id"] for n in graph["nodes"]]
        self.assertEqual(len(node_ids), len(set(node_ids)))

    def test_library_chunks_searchable_text(self):
        graph = build_graph([], [], libraries=[lib(docs=[doc()])])
        chunk = by_id(graph, "x:lib-1:doc-1:doc-1-002")
        self.assertIn("第 2 段内容", chunk["summary"])


if __name__ == "__main__":
    unittest.main()
