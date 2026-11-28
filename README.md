# 言据 · 可引用知识库与通用 Agent

[![tests](https://github.com/gaozhe114514/yanju-rag/actions/workflows/tests.yml/badge.svg)](https://github.com/gaozhe114514/yanju-rag/actions/workflows/tests.yml)

> 一个会查资料、也能正常聊天的通用 Agent：答得出来就带逐字引用，答不出来就明说。

**当前版本 v0.84.0** ｜ 502 条测试通过 ｜ Python 3.11 · FastAPI · LangGraph · PostgreSQL 16 + pgvector · 单文件前端，无构建步骤

![问答首页（琉璃主题）](docs/screens/home-glass-1440.png)

## 它是什么

言据是一个「先查资料、再回答」的通用 Agent。普通问题直接聊；问到公司资料、产品手册、个人笔记时，它会调用知识库检索，并把答案落在原文上。每条结论都必须逐字命中原文；资料没覆盖的问题直接说没覆盖，并把缺口记下来。

默认知识库内置 6 篇示例资料，**长期公开、不用登录、不用账号**：可以直接提问、点引用看原文。公网部署若要保持免登录，不要设置 `KB_ACCESS_TOKEN`。登录后可以建立公司知识库或个人知识库，上传自己的 PDF / Word / PPT / Excel / Markdown / 图片等资料。

## 核心能力

| 能力 | 说明 |
|---|---|
| 统一 Agent | LangGraph + 原生函数调用：模型自己选 `search_kb`、知识库概览或实时工具，最多两轮；过程与工具调用可见 |
| 可引用问答 | 问题理解 → BM25 + 向量召回 → RRF → Cross-Encoder 精排 → 拒答闸门 → 生成 → 引用逐字校验；校验不过重试一次，仍不过就拒答 |
| 知识库工作台 | 一级导航；左侧库列表 + 右侧工作台；多级文件夹真树、每库独立切片参数、资料搜索/状态筛选、批量重解析/删除、原文预览、检索测试、URL 导入、重建索引、操作动态；上传原始文件与解析任务落库，重启续跑、失败可重试；默认库公开只读、公司库登录可读、个人库仅本人可见 |
| 自动 Wiki | 从已完成资料里抽出概念、流程与制度，生成带真实来源的页面；页面可编辑、历史版本可回滚；关系图展示共用来源与页面内链；模型不可用时按文档建页，离线也能演示 |
| 实时工具 | 天气、时间、计算、代码执行、网页搜索、网页正文；工具失败会明说，不拿资料片段凑答案 |
| 引用可回跳 | 引用带真实资料与片段位置，点开整篇原文并高亮被引用的原句 |
| 账号与同步 | 注册登录、管理员管账号、个人内容跨设备同步；旧设备的 `408-*` 本地键会一次性迁移到 `yanju-*` |
| 创作 | 选文档或演示文稿，写主题 → 确认大纲 → 下载 Word `.docx` / PPT `.pptx` / Markdown `.md` |
| 界面 | 琉璃 / 雪白 / 奶油 / 深色四套主题（琉璃默认），桌面与手机同一套页面 |

| 知识库：默认库免登录可试用 | 设置：四套主题，琉璃为默认 |
|---|---|
| <img src="docs/screens/collection-glass-1440.png" width="100%" alt="知识库"> | <img src="docs/screens/settings-glass-1440.png" width="100%" alt="设置"> |

## Agent 层

输入条只有一个 **输入设置** 入口：资料范围 + 人格。没有模式切换，也没有关键词路由器：
每次提问都把工具定义一起交给 DeepSeek，由模型自己决定直接回答还是调用工具。

- 普通对话（「我不想上班，上班好累」）直接回答；
- 问公司资料 / 产品手册 / 自己的笔记时，调用 `search_kb`，回答带逐字引用；
- 问「这个库有多少资料 / 有哪些文档」时，调用 `kb_overview`，直接读知识库元数据；
- 问天气、空气质量、股价、汇率、IP 归属、时间、计算、单位换算、最新消息、官网时，调用对应工具；已有网址时直接抓网页正文；
- 明确问知识库但资料没有时，明说「资料里没有」，不编答案。

- **标准函数调用循环**：模型返回 `tool_calls` → 服务端执行 → 把结果作为 `tool` 消息回给模型 → 再回答，最多两轮；同一工具同一轮不重复调用。
- **不动检索**：`search_kb` 调用原来的 BM25 + 向量 → RRF → Cross-Encoder，只把 top 片段暴露成工具；一旦调用检索，仍走原拒答闸门与逐字引用校验。
- **硬约束不变**：引用逐字命中原文；失败重试一次后拒答；资料不足写缺口日志。
- **线程记忆**：默认进程内，配 `DATABASE_URL` 后自动切到 PostgreSQL checkpoint；换账号拿旧线程号会新建线程。
- **接口**：`POST /agent/stream`（SSE）；`/chat`、`/chat/stream` 保留给脚本与老客户端。

细节与取舍见 [Agent 层](docs/Agent层.md)。

## 系统架构

![系统架构](docs/fig2_architecture.png)

- **存储**：PostgreSQL + pgvector 保存知识库、文档、片段、向量、账号、缓存与日志；BM25 在应用进程内。
- **在线链路**：函数调用决定直接回答 / 检索 / 工具 → 检索后走拒答闸门 → 生成（强制 JSON）→ 引用逐字校验 → 失败重试 → 仍失败拒答；资料不足时写缺口日志。
- **前端**：`web/index.html` 单文件，hash 路由，流式输出，无构建步骤。

更细的选型与取舍见 [架构设计](docs/架构设计.md)。

## 快速开始

### Docker（含 PostgreSQL）

```bash
cp .env.example .env          # 填 DEEPSEEK_API_KEY / KB_ACCESS_TOKEN / POSTGRES_PASSWORD
mkdir -p logs && sudo chown -R 1000:1000 logs
docker compose up -d --build  # 首次 10~30 分钟，含 2.4GB 模型
curl -s http://127.0.0.1:8000/health
```

### 本地预览（Windows 一键）

双击仓库根目录的 `dev.cmd`，浏览器打开 `http://127.0.0.1:8011/`。改前端保存后 `Ctrl+F5`；改后端会自动重启。

### 本地 mock（不需要 API key）

```bash
py -3.11 -m uvicorn api.app:app --host 127.0.0.1 --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

mock provider 只取 top-1 的答案，用来验证链路，不是真生成。默认知识库的示例资料在 `data/curated/default/`，启动时自动灌入。

> 项目按 Python 3.11 维护；Windows 上用 `py -3.11`（或先激活 3.11 的虚拟环境），不要用 Microsoft Store 的 `python` 占位符。

### 运行测试

```bash
py -3.11 -m unittest discover -s tests -t . -v
```

## 文档

| 文档 | 内容 |
|---|---|
| [设计规范](design.md) | 配色、字体、间距、组件与布局的唯一来源；改界面前先读 |
| [架构设计](docs/架构设计.md) | 在线链路、分层架构、数据模型、选型理由、成本与落地顺序 |
| [功能说明](docs/功能说明.md) | 产品框架、默认 / 公司 / 个人知识库、权限、文件进料、账号同步 |
| [Agent 层](docs/Agent层.md) | LangGraph 状态图、search_kb、线程记忆、SSE 事件与边界 |
| [在线问答与接口](docs/接口.md) | 启动方式、HTTP 接口一览、访问口令、限流与缓存 |
| [存储层](docs/存储层.md) | PostgreSQL + pgvector 的表结构与知识库数据模型 |
| [部署](docs/部署.md) | 云服务器 ECS 上的 Docker + Nginx + 口令 + 限流，含部署记录 |
| [更新记录](docs/更新记录.md) | 按里程碑合并的版本摘要，细节回 git log |

参考资料：[WeKnora 接入](docs/WeKnora接入.md)、[借鉴 WeKnora 的改造方案](docs/借鉴WeKnora-改造方案.md)。

## 目录结构

```
agent/       LangGraph 状态图、工具、线程记忆
answer/      检索后的回答链路：拒答、生成、逐字校验、缓存、创作
api/         FastAPI、账号、权限、缺口与反馈
kb/          知识库、默认资料、文件解析与切分
retrieval/   BM25、向量、RRF、精排
db/          PostgreSQL 存取与 schema
web/         单文件前端
data/
  curated/default/   默认知识库示例资料（内置只读）
  work/              本机运行产物（不入库）
tests/       单元与接口测试
```
