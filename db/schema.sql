-- 408 RAG 存储层：题目、考点、向量、拒答日志
-- 由 db.store.Store.init_schema() 执行，{dim} 是 embedding 维度

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
    id                  text PRIMARY KEY,
    kind                text NOT NULL CHECK (kind IN ('question', 'knowledge')),
    year                int,
    seq                 int,
    subject             text NOT NULL DEFAULT '',
    chapter             text NOT NULL DEFAULT '',
    title               text NOT NULL DEFAULT '',
    -- 浏览页搜索用的合并文本，已转小写；与 api/catalog._haystack 保持一致
    search_text         text NOT NULL DEFAULT '',
    -- 参与召回与引用的正文：题干+选项，或考点卡片正文
    text                text NOT NULL,
    answer              text NOT NULL DEFAULT '',
    analysis            text NOT NULL DEFAULT '',
    -- 原始 JSONL 行，浏览接口直接返回给前端
    payload             jsonb NOT NULL,
    related_question_ids text[] NOT NULL DEFAULT '{}',
    embedding           vector({dim}),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chunks_kind_year_seq_idx
    ON chunks (kind, year, seq);
CREATE INDEX IF NOT EXISTS chunks_kind_subject_chapter_idx
    ON chunks (kind, subject, chapter);
CREATE INDEX IF NOT EXISTS chunks_related_idx
    ON chunks USING gin (related_question_ids);
-- 语料只有几百条，HNSW 足够；数据量再大再考虑分区/IVFFlat 参数
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);

CREATE TABLE IF NOT EXISTS gap_logs (
    id         bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    query      text NOT NULL,
    reason     text NOT NULL DEFAULT '',
    top_chunk  text,
    top_score  real
);
