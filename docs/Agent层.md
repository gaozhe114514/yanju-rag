# Agent 层（v0.76）

这一页讲清 v0.76 新增的 Agent 模式：它解决什么问题、状态图怎么走、工具边界在哪、记忆落在哪，以及哪些事刻意没做。

## 目标与边界

目标不是换掉检索，而是把现有的「检索 → 拒答 → 生成 → 逐字校验」包成一条**可观察、可重试、可恢复**的 Agent 链路，让每一步决策都能在界面上核对。

边界定得很死：

- **不改检索**：BM25、向量召回、RRF、Cross-Encoder 全部沿用；`search_kb` 只是把检索器包一层。
- **不改现有接口**：`/ask`、`/chat`、`/chat/stream` 一行不动；Agent 单独走 `POST /agent/stream`。
- **不放宽硬约束**：引用仍然逐字命中原文；校验失败最多重试一次；仍失败必须拒答并写缺口日志。
- **不做无界工具循环**：最多一次换查询重试，避免模型在 2C 机器上反复检索、成本和延迟不可控。
- **实时数据不进 Agent**：天气 / 时间 / 计算需要标准问答里的工具；`/agent/stream` 直接返回 `agent_live_unsupported`，前端给「切回标准问答再问」的一键退路，不拿知识库里不相关的片段凑答案。
- 天气工具在 Open-Meteo 预报超时时回退 `wttr.in`（线上实测 Open-Meteo 预报不可达、wttr.in 正常）。
- **工具选择是显式的**：知识问题固定走 `search_kb`，`plan` 节点只决定「是否检索 / 检索什么问题」，不让模型自由挑工具。这样引用来源可控、成本可预测；代价是它还不是通用 ReAct，多工具自由选择留到下一版。

## 状态图

```text
START
  │
  ▼
plan ──(寒暄 / 指代不明)──▶ direct ──▶ END
  │
  │(知识问题)
  ▼
search_kb ──▶ gate ──(证据不足)──▶ refuse ──▶ END
                │
                │(通过)
                ▼
             generate
                │
                │(生成异常)──▶ refuse ──▶ END
                ▼
              verify ──(通过)──▶ finalize ──▶ END
                │
                │(第一次未通过：换查询)
                └──────────────▶ search_kb
                │
                │(第二次仍未通过)──▶ refuse ──▶ END
```

`verify → search_kb` 这条边就是唯一的循环，且只允许走一次：`attempt < 2` 才重试。

## 节点职责

| 节点 | 做什么 | 复用 |
|---|---|---|
| `plan` | 识别寒暄 / 指代不明；否则把追问改写成独立检索问题 | `answer/agent.py` 的 `QueryAgent` |
| `search_kb` | 调用工具，拿 top 片段，记录参数、命中数与耗时 | `retrieval/search.py` 现有检索器 |
| `gate` | 拒答闸门；通过后准备相邻片段 / 全表统计 | `answer/gate.py`、`AnswerPipeline.prepare` |
| `generate` | 强制 JSON 生成；流式时推 `delta` | `answer/prompt.py`、`answer/stream.py` |
| `verify` | 逐字引用校验 + 表格数字校验；失败换查询 | `answer/verify.py`、`answer/aggregate.py` |
| `finalize` | 写入助手消息，线程记忆收尾 | — |
| `refuse` | 统一拒答文案与原因，保留召回用于排查 | — |
| `direct` | 寒暄 / 指代不明的固定回复，不检索不生成 | `chitchat_reply` |

## search_kb 工具

```json
{
  "name": "search_kb",
  "description": "在当前知识库里检索资料，返回带 chunk_id 与原文的 top 片段",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {"type": "string"},
      "top_k": {"type": "integer", "minimum": 1, "maximum": 10}
    },
    "required": ["query"]
  }
}
```

实现见 `agent/tools.py`。工具返回完整的 `SearchHit`：

- `chunk.id` 用于引用校验，模型输出里的 `chunk_id` 必须在这批候选里；
- `chunk.text / answer / analysis` 用于生成与逐字比对；
- `source_ref` 随响应回传，前端可以跳回原文并高亮；
- 工具**不生成答案**，也不改分数与排序。

工具按节点显式调用，而不是 LangChain 的 `ToolNode`：检索返回的是项目自己的 `SearchHit` 对象，生成与校验都要原样复用，套一层 LangChain Tool 反而要做两次序列化。

## 重试与拒答

第一次 `verify` 失败时：

1. 用 `agent/prompt.py` 的 `refine_query` 生成新的检索问题：有模型时走一次 JSON 补全，没有模型或失败时把 top-1 片段的标题补进检索词；
2. 回到 `search_kb` 重新检索；
3. 用新的上下文重新生成并校验；
4. 第二次仍失败：`citation_failed` / `stat_failed` 拒答，接口写缺口日志。

这和主链路「校验不过重试一次，仍不过就拒答」是同一条规则，只是重试时换了检索问题，而不是只让模型重写一遍。

## 线程记忆

- 状态里的 `messages` 只存 user / assistant 的短文本（单条截断 1200 字），用带上限的 reducer 只保留最近 20 条；检索片段、上下文、原始输出也都在状态里，但每轮覆盖，不会随轮次无限增长。
- 默认 `InMemorySaver`，进程重启即丢；配置 `DATABASE_URL` 且装了 `langgraph-checkpoint-postgres` 时自动换 `PostgresSaver`，复用同一个 PostgreSQL 16，`setup()` 自动建 `checkpoints` / `checkpoint_blobs` / `checkpoint_writes`，以及记录迁移版本的 `checkpoint_migrations`。
- `thread_id` 由前端给（缺省用对话 id）或服务端生成；同一 `thread_id` 只有同一个 owner 能续写。owner 是登录用户 id，未登录时是客户端 IP 的 SHA-256 前缀。换了账号或换了客户端拿旧线程号来，会新建线程并在响应里标 `thread_reset: true`，不会读出别人的对话。
- `/health` 的 `agent` / `agent_checkpointer` 可以直接看到当前是否启用、记忆落在内存还是 Postgres。
- 单 worker 部署下内存记忆够用；多 worker 各自一份，必须配 `DATABASE_URL` 才能共享线程。

## 流式接口

`POST /agent/stream`，请求体：

```json
{
  "query": "TCP 三次握手为什么不能是两次",
  "thread_id": "可选，64 字以内",
  "history": [{"role": "user", "content": "..."}],
  "style": "可选，≤400 字",
  "collection": "可选，自建知识库 id"
}
```

SSE 事件：

| 事件 | 说明 |
|---|---|
| `status` | `plan` / `search` / `tool` / `gate` / `generate` / `verify` / `retry` / `refuse` / `done` |
| `tool` | `{name, args, hits, ms, chunk_ids}`，本次 `search_kb` 的调用记录 |
| `delta` | 增量 answer 文本（已从 JSON 里剥出） |
| `reset` | 校验未过，前端清空已显示的文字，随后换查询重试 |
| `error` | 生成服务或运行时异常 |
| `done` | 最终 payload：`answer / citations / refused / reason / hits / tool_calls / trace.steps / agent / thread_id` |

最终结果只认 `done`；`reset` 只影响界面上的临时文字。现有 `/chat/stream` 的事件语义不变。

命令行试一下：

```bash
curl -N http://127.0.0.1:8000/agent/stream \
  -H 'Content-Type: application/json' \
  -d '{"query":"TCP 三次握手为什么不能是两次","thread_id":"demo-1"}'
```

## 前端

输入条只保留一个「输入设置」按钮（齿轮 + 当前回答方式），点开是一张设置卡片，三个选择项分别是回答方式（标准问答 / Agent 编排）、资料范围与人格；资料范围与执行方式不再像两个并列模式。选 Agent 后（设备级偏好，不上传）：

- 请求改走 `/agent/stream`，对话 id 作为 `thread_id`；
- 阶段提示会显示「正在调用 search_kb」「检索完成，命中 N 条」「换个问法再查一次」；
- 回答下方多一行 `Agent · N 次 search_kb`，可展开「工具调用」看每次的 query、命中数与耗时；
- 「执行过程」沿用原来的步骤表，能看到理解问题、拒答判断、生成、引用校验各自的耗时；
- 切回标准问答就回到原来的 `/chat/stream`，历史对话不受影响。

README 配图用 `docs/screens/agent-glass-1440.png`（琉璃默认）；本地验收截图（深色主题）见 `data/work/prod-v0764-live-standard-dark-1440.png`（线上标准问答天气）与 `prod-v0764-live-agent-dark-390.png`（线上 Agent 实时问题退路）；线上 v0.76.3 的版本页截图见 `prod-v0763-history-dark-1440.png`；线上 v0.76.2 的部署证据见 `prod-v0762-mode-dark-1440.png` 与 `prod-v0762-agent-dark-390.png`。

## 为什么是 LangGraph 0.6

- v0.76.0 时本机、CI 与线上镜像仍按 Python 3.9 维护；LangGraph 0.7 / LangChain 1.0 起要求 Python 3.10，所以当时钉在 `langgraph>=0.6,<0.7`。
- v0.76.1 已把本机、CI 与镜像统一到 Python 3.11，理论上可以升 1.0；但这轮只做 Python 版本迁移，Agent 层先在 0.6 上验收，`create_agent` 作为单独迁移，不和版本升级混做。
- `requirements.txt` 同时钉了 `langchain-core>=0.3,<1`：LangGraph 0.6 的依赖元数据只写 `langchain-core>=0.1`，在 3.11 上 pip 会挑 1.x，而 0.6 只在 0.3.x 上验证过；不钉会在新环境里装出没测过的组合。
- 显式图更适合面试与审计：每个节点、每条边、重试上限都写在代码里，不需要记框架的隐式 ReAct 循环。
- checkpoint 仍用官方 `InMemorySaver` / `PostgresSaver`，没有自己造一套状态存储。

## 硬约束对照

| 硬约束 | 落点 |
|---|---|
| 引用逐字命中原文 | `verify_node` 调用与主链路相同的 `verify_payload` |
| 校验失败重试一次 | `verify → search_kb` 条件边，`attempt < 2` |
| 仍失败拒答 | `refuse_node` + `_log_gap` 写缺口 |
| 一段资料 = 一个 chunk | 检索层没动，chunk 结构与切分不变 |
| E 级校验错误不进检索库 | 离线管线没动，Agent 只读检索结果 |
| 不硬编答案 | 资料不足走 `refuse`，不生成通用答案 |

## 测试与验收

- 单元测试新增 24 条：`tests/test_agent_graph.py`（成功、闸门拒答、换查询重试成功 / 失败、重试后无命中仍保留尝试次数、模型拒答、生成异常、寒暄 / 澄清、线程记忆、跨 owner 隔离、工作线程冒烟）与 `tests/test_agent_tools.py`（工具 top_k、JSON Schema、状态序列化、线程号清洗、checkpointer 选择、SSE 事件、refine 兜底）。
- 全量：`Ran 667 tests ... OK (skipped=13)`。
- 模拟 CI（不装 fastapi，装 langgraph）：`Ran 665 tests ... OK (skipped=18)`。
- 本机 mock 服务：`/health` 返回 `agent=true / agent_checkpointer=memory`；`/agent/stream` 成功返回工具调用、引用与步骤；同一 `thread_id` 第二轮能带上上一轮历史改写追问。另用 `KB_MODE=hybrid-ce`（与线上同模式）跑通：v0.76.0 在 3.9 + CUDA torch 上检索 723ms；v0.76.1 换 3.11 + CPU torch 后第一次 13.3s、第二次 12.4s（CPU 精排，生产镜像同样是 CPU torch），引用 1 条、`refused=false`，第二轮同样带上历史改写。
- **PostgresSaver 路径没有在本机实库跑过**：本机没有 Docker / PostgreSQL，只按官方 `PostgresSaver(conn) + setup()` 接法实现。部署前要在有数据库的环境确认 `/health` 的 `agent_checkpointer=postgres`，并用同一个 `thread_id` 连问两轮验证跨重启续写。
- 前端 CDP：1440×900 与 390×844 各一张截图；Agent 开关 `aria-pressed=true`，工具调用可展开，横向溢出 0，控制台错误 0。

## 没做的事

- **已部署（v0.76.2）**：线上 `/health` 为 `agent=true / agent_checkpointer=postgres`，`/agent/stream` 两轮通过；生产截图见 `data/work/prod-v0762-mode-dark-1440.png` 与 `prod-v0762-agent-dark-390.png`。
- 没有多 Agent 分工、没有任意工具循环、没有 MCP / CLI 工具沙箱——先把「可引用的 Agent」做扎实。
- Agent 暂未接应答缓存：工具轨迹和线程记忆要一起返回，缓存键需要额外带 `thread_id`，这轮先不做；普通 `/chat` 的缓存不受影响。
- 没有把生成提示改成带完整对话历史的 prompt：对话历史只用于问题改写与线程记忆，生成仍以本轮检索到的资料为唯一事实来源，避免历史回答污染引用。
