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

-- 用户自建知识库（v0.25）：一份资料库 = 若干文档 = 若干可引用片段。
-- 与 408 预置语料（chunks 表）物理隔离：内置语料只读，用户资料可增删。
CREATE TABLE IF NOT EXISTS collections (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    description text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS documents (
    id            text PRIMARY KEY,
    collection_id text NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    title         text NOT NULL DEFAULT '',
    text          text NOT NULL,
    sha256        text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS documents_collection_idx
    ON documents (collection_id, created_at DESC);
CREATE INDEX IF NOT EXISTS documents_sha_idx
    ON documents (collection_id, sha256);

-- 片段保留原文起止位置，引用块据此跳回文档里的原句
CREATE TABLE IF NOT EXISTS doc_chunks (
    id            text PRIMARY KEY,
    collection_id text NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    document_id   text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq           int NOT NULL,
    text          text NOT NULL,
    start_off     int NOT NULL DEFAULT 0,
    end_off       int NOT NULL DEFAULT 0,
    embedding     vector({dim}),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS doc_chunks_collection_idx
    ON doc_chunks (collection_id, document_id, seq);
CREATE INDEX IF NOT EXISTS doc_chunks_embedding_idx
    ON doc_chunks USING hnsw (embedding vector_cosine_ops);

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

CREATE INDEX IF NOT EXISTS gap_logs_created_idx
    ON gap_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS gap_logs_reason_idx
    ON gap_logs (reason);

-- 用户对回答的反馈：👎 的原因和补充说明是补资料/改检索的直接输入。
-- query/answer 最多各留 500/2000 字，便于定位；不存 IP、UA 等标识。
CREATE TABLE IF NOT EXISTS answer_feedback (
    id          bigserial PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    query       text NOT NULL,
    answer      text NOT NULL DEFAULT '',
    helpful     boolean NOT NULL,
    reason      text NOT NULL DEFAULT '',
    note        text NOT NULL DEFAULT '',
    citations   text[] NOT NULL DEFAULT '{}',
    cached      boolean NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS answer_feedback_created_idx
    ON answer_feedback (created_at DESC);
CREATE INDEX IF NOT EXISTS answer_feedback_helpful_idx
    ON answer_feedback (helpful, created_at DESC);

-- 应答缓存：只存已作答的回答，拒答不入库。
-- cache_key 由 answer.cache.cache_key() 生成，命名空间里带检索模式、
-- provider 与语料指纹，语料更新后旧缓存自然失效。
CREATE TABLE IF NOT EXISTS answer_cache (
    cache_key  text PRIMARY KEY,
    query      text NOT NULL DEFAULT '',
    payload    jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS answer_cache_created_idx
    ON answer_cache (created_at);
