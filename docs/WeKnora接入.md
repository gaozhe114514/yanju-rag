# WeKnora 接入说明

本项目的用户资料库可以整套交给 [腾讯开源 WeKnora](https://github.com/Tencent/WeKnora)：
文档解析、切片、向量化、召回由它负责；**拒答闸门、生成、逐字引用校验仍在本项目**。
内置「408 考研」语料不经过 WeKnora，永远走本地 Postgres。

## 1. 边界：只用它做进料与召回

| 能力 | 谁负责 |
|---|---|
| PDF / Word / PPT / Markdown / 图片 / 网页解析 | WeKnora docreader |
| 切片、向量、rerank、`knowledge-search` | WeKnora |
| 知识库 / 文档 / 片段 CRUD | WeKnora REST API |
| 意图识别、拒答闸门 | 本项目 |
| 生成 | 本项目（DeepSeek） |
| 引用必须逐字命中原句 | 本项目 `answer/verify.py` |
| 资料没覆盖就拒答 | 本项目 |

**不要调用 WeKnora 的 `/knowledge-chat`**：它自己会生成答案，绕过本项目的逐字校验，
「引用必须逐字命中」这条硬约束就失效了。本项目只调用 `/api/v1/knowledge-search`。

## 2. 部署 WeKnora（和本项目同机或另一台机器）

官方仓库自带 `docker-compose.yml`，镜像已发布到 Docker Hub：
`wechatopenai/weknora-app`、`wechatopenai/weknora-ui`、`wechatopenai/weknora-docreader`。
默认栈是 frontend + app + docreader + postgres（paradedb pg17）+ redis，官方建议 4C8G。

本项目线上那台 2C / 3.7G 的轻量服务器**没有余量再跑整套 WeKnora**。可选的两种做法：

1. **另起一台机器**跑 WeKnora，本项目通过 `WEKNORA_BASE_URL` 指过去（推荐）。
2. 同机只起 `app + docreader + postgres + redis`，不起 frontend，并把 app 端口绑回环：
   `127.0.0.1:8080:8080`。这一步会明显挤压本项目的内存，需先做容量评估。

```bash
git clone https://github.com/Tencent/WeKnora.git
cd WeKnora
cp .env.example .env
# 至少填：DB_USER / DB_PASSWORD / DB_NAME / REDIS_PASSWORD
# 只给本机用时把 APP_PORT 改成 127.0.0.1:8080（frontend 同理绑回环）
docker compose up -d
```

首次启动后打开 WeKnora 控制台，完成三件事：

1. **配模型**：在「设置 → 模型」里配一个 embedding 模型（必需，检索要靠它）和一个 LLM
   （WeKnora 自己生成摘要/标题会用到；本项目不调用它的问答）。rerank 模型可选。
2. **建知识库**：记下知识库 id（`/api/v1/knowledge-bases` 返回的 `id`）。
3. **建 API Key**：在 API Key 页面生成，供本项目服务端使用。

## 3. 本项目配置

服务端环境变量（写进 `.env`，不要提交）：

```bash
WEKNORA_BASE_URL=http://127.0.0.1:8080   # 另起一台就填那台的地址
WEKNORA_API_KEY=wk-xxxxxxxx
WEKNORA_TIMEOUT=20
# 只有 WeKnora 侧配好 rerank 模型才打开
# WEKNORA_RERANK=1
```

重启 API 后，`/health` 里的 `kb_backend` 会变成 `weknora`，`/collections` 返回
`{"storage": "weknora", ...}`。没配 `WEKNORA_BASE_URL` 时全部行为与旧版一致。

## 4. 接口对应

| 本项目 | WeKnora |
|---|---|
| `GET /collections` | `GET /api/v1/knowledge-bases` |
| `POST /collections` | `POST /api/v1/knowledge-bases` |
| `POST /collections/{cid}/documents`（粘贴正文） | `POST /api/v1/knowledge-bases/{cid}/knowledge/manual` |
| `POST /collections/{cid}/files`（上传文件） | `POST /api/v1/knowledge-bases/{cid}/knowledge/file`（multipart） |
| `GET /collections/{cid}/documents/{doc_id}` | `GET /api/v1/knowledge/{id}/preview` |
| `GET .../documents/{doc_id}/chunks` | `GET /api/v1/chunks/{knowledge_id}` |
| `PUT .../chunks/{chunk_id}` | `PUT /api/v1/chunks/{knowledge_id}/{chunk_id}` |
| `DELETE .../chunks/{chunk_id}` | `DELETE /api/v1/chunks/{knowledge_id}/{chunk_id}` |
| `POST .../documents/{doc_id}/reparse` | `POST /api/v1/knowledge/{id}/reparse` |
| 检索 | `POST /api/v1/knowledge-search` |

认证头用 `X-API-Key`；响应统一是 `{"success": true, "data": ...}`，客户端会拆掉外层，
失败按 HTTP 状态映射成 404 / 409 / 502。

## 5. 自检

```bash
# 1) 服务活着
curl -s http://127.0.0.1:8080/health

# 2) 密钥与知识库列表（把 KEY 换成自己的）
curl -s -H "X-API-Key: $WEKNORA_API_KEY" \
  http://127.0.0.1:8080/api/v1/knowledge-bases

# 3) 本项目切过去了
curl -s http://127.0.0.1:8000/health | grep kb_backend
```

前端验证：知识库详情页上传一份 PDF → 列表出现「解析中」→ 变成「已索引」→
点「片段」能看到切出来的段落 → 改一段再提问，回答里的引用应该用改后的文字。

## 6. 已知限制

- **解析是异步的**：上传后状态会经历 `pending / processing / completed / failed`，
  前端每 3 秒刷一次、最多 10 次；超时就手动点「重解析」或刷新页面。
- **嵌入模型没配时检索会失败**：这是 WeKnora 侧配置，不是本项目静默降级；
  接口会回 502 并带 WeKnora 的错误信息。
- **片段 revision**：WeKnora 用 `content_revision` 做乐观锁，冲突回 409，界面提示
  「这段已被别处改过，点重新加载再来」，不会覆盖别人的修改。
- **上传大小**：本项目限制单文件 20MB；WeKnora 侧还有自己的 `MAX_FILE_SIZE_MB`。
- 内置 408 语料的题目 / 考点 / 统计不受影响；WeKnora 只接管用户自建资料库。
