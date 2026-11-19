# -*- coding: utf-8 -*-
"""PostgreSQL + pgvector 存储层。

JSONL 是构建产物（语料种子），Postgres 是线上唯一的数据源：
知识库、向量、账号、拒答与反馈日志都从这里走。BM25 仍在应用进程内算，
因为 Postgres 的中文全文检索离不开 zhparser 一类扩展，收益不值这个运维成本。
"""
