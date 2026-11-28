-- 言据 RAG 存储层：知识库、文档、片段、账号、拒答与反馈
-- 由 db.store.Store.init_schema() 执行，{dim} 是 embedding 维度

CREATE EXTENSION IF NOT EXISTS vector;

-- 用户自建知识库（v0.25）：一份资料库 = 若干文档 = 若干可引用片段。
-- 默认知识库是内置只读库；公司库与个人库按成员权限读写。
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

-- v2.17：文档解析状态机。老库没有这几列，用 ADD COLUMN IF NOT EXISTS 补上，
-- 已有的资料按 completed 处理，不会因为升级丢内容。
ALTER TABLE documents ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'completed';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS error text NOT NULL DEFAULT '';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS bytes int NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT '';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
-- v0.63：进料时抽出的结构化表格（xlsx/docx/pdf/html），统计直接数这里的行。
-- 老文档没有这一列的值，按 '[]' 处理，统计仍从正文逐行数，行为不变。
ALTER TABLE documents ADD COLUMN IF NOT EXISTS tables jsonb NOT NULL DEFAULT '[]'::jsonb;
-- v0.81：资料所属文件夹与标签；老资料为空，按「未分组 / 无标签」显示
ALTER TABLE documents ADD COLUMN IF NOT EXISTS folder text NOT NULL DEFAULT '';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS tags text[] NOT NULL DEFAULT '{}';

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

-- 片段编辑的乐观锁：每次改字 revision + 1，冲突回 409，不覆盖别人的修改
ALTER TABLE doc_chunks ADD COLUMN IF NOT EXISTS revision int NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS doc_chunks_collection_idx
    ON doc_chunks (collection_id, document_id, seq);
CREATE INDEX IF NOT EXISTS doc_chunks_embedding_idx
    ON doc_chunks USING hnsw (embedding vector_cosine_ops);

-- v0.71：整篇资料的编辑历史。每次保存前先把旧版本存一行，
-- 恢复旧版也走同一套保存流程（当前版会变成新的一行），可按时间回退。
CREATE TABLE IF NOT EXISTS document_versions (
    id            bigserial PRIMARY KEY,
    collection_id text NOT NULL,
    document_id   text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    title         text NOT NULL DEFAULT '',
    text          text NOT NULL,
    sha256        text NOT NULL DEFAULT '',
    author        text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS document_versions_doc_idx
    ON document_versions (document_id, id DESC);

-- v0.72：资料附件与插图。原文件按字节存，正文里只用 [[文件名]] / ![[文件名]]
-- 引用；删除资料时由外键级联清理。
CREATE TABLE IF NOT EXISTS document_assets (
    id            text PRIMARY KEY,
    collection_id text NOT NULL,
    document_id   text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    filename      text NOT NULL,
    mime          text NOT NULL DEFAULT '',
    nbytes        int NOT NULL DEFAULT 0,
    data          bytea NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS document_assets_doc_idx
    ON document_assets (document_id, created_at);

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

-- 请求日志：每个进到 API 的请求一行，用来回答「谁在什么时候问了什么、
-- 多快、有没有拒答」。只记路径、状态与耗时，不记 IP / UA / 请求头。
CREATE TABLE IF NOT EXISTS request_logs (
    id          bigserial PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    method      text NOT NULL DEFAULT 'GET',
    path        text NOT NULL DEFAULT '',
    status      int NOT NULL DEFAULT 0,
    latency_ms  int NOT NULL DEFAULT 0,
    query       text NOT NULL DEFAULT '',
    client      text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS request_logs_created_idx
    ON request_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS request_logs_path_idx
    ON request_logs (path, created_at DESC);

CREATE TABLE IF NOT EXISTS users (
    id            text PRIMARY KEY,
    username      text NOT NULL,
    username_key  text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash text PRIMARY KEY,
    user_id    text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS user_sessions_user_idx
    ON user_sessions (user_id, expires_at DESC);

-- v0.67：管理员可以停用账号。停用后登录与已有会话都失效，
-- 数据不删，重新启用就恢复。
ALTER TABLE users ADD COLUMN IF NOT EXISTS disabled boolean NOT NULL DEFAULT false;

-- 登录后的个人数据：一个 localStorage 键一行，值与浏览器里一一对应。
-- 访问口令、账号 token、同步元数据不进这张表（服务端也会再挡一次）。
CREATE TABLE IF NOT EXISTS user_data (
    user_id    text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key        text NOT NULL,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);

CREATE INDEX IF NOT EXISTS user_data_user_idx
    ON user_data (user_id, updated_at DESC);

-- 知识库权限（v0.52，B-lite）：全局角色只有 admin / member；
-- 库的可见性 shared（公司） / private（个人）；成员表只给公司库做「谁能改」。
ALTER TABLE users ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'member';
ALTER TABLE collections ADD COLUMN IF NOT EXISTS owner_id text REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE collections ADD COLUMN IF NOT EXISTS created_by text REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE collections ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'shared';
CREATE INDEX IF NOT EXISTS collections_owner_idx
    ON collections (owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS collections_visibility_idx
    ON collections (visibility, created_at DESC);

-- 成员角色：owner 只在 collections.owner_id 上体现，这里只存 editor / viewer，
-- 避免同一份所有权记两处。管理员天然可管所有公司库，不需要逐库加人。
CREATE TABLE IF NOT EXISTS collection_members (
    collection_id text NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    user_id       text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role          text NOT NULL DEFAULT 'viewer',
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (collection_id, user_id)
);

CREATE INDEX IF NOT EXISTS collection_members_user_idx
    ON collection_members (user_id, collection_id);
