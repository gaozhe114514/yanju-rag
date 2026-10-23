# 408 考研知识库

面向 408（计算机学科专业基础综合）考生的真题问答知识库：**每个结论都能被考生自己验证，答不出来就明说，不硬编。**

> 状态：v0.7。数据管线、检索、拒答、真实生成、引用校验、HTTP 服务与单机 Docker 部署已跑通，并在腾讯云轻量（2核4G）实机验证。这一版先把产品框架搭起来：多轮问答 Agent、题库浏览、练习三个页面 + 会话历史 + 执行过程展示；题库仍只有 2019 卷 47 题（1–40 解析为 AI 生成，待人工校对），2020–2025、答案正确率评测与数据库尚未接入。

![系统架构](docs/fig2_architecture.png)

## 文档

- [架构设计](docs/408-架构设计.md) — 在线链路、分层架构、选型理由、成本与落地顺序
- [数据管线](docs/数据管线.md) — 真题 → 结构化题库的五步流程与验收门槛
- [部署](docs/部署.md) — 云服务器 ECS 上的 Docker + Nginx + 口令 + 限流
- `docs/fig1_online_pipeline.png` — 在线问答主链路
- `docs/fig3_review_ui.png` — 人工校对页

## 快速开始

只依赖 Python 3.9+ 标准库，无需安装任何包。

```bash
# 离线跑通（mock provider，不需要 API key）
py -m pipeline.build --paper data/raw/sample_paper.txt \
                     --answer data/raw/sample_answer.txt \
                     --year 2024 --provider mock

# 接真实模型
set DEEPSEEK_API_KEY=sk-xxxx          # PowerShell: $env:DEEPSEEK_API_KEY="sk-xxxx"
py -m pipeline.build --paper data/raw/2019_paper.txt \
                     --answer data/raw/2019_answer.txt \
                     --year 2019 --provider deepseek

# 校对完成后合并 patch
py -m pipeline.build --year 2019 --apply-patch data/work/review_patch.json
```

产物：

| 路径 | 内容 |
|---|---|
| `data/work/{year}_draft.jsonl` | 抽取草稿 |
| `data/work/{year}_validate.txt` | 机器校验报告 |
| `data/work/{year}_review.html` | 人工校对页（浏览器打开，进度存 localStorage） |
| `data/out/{year}.jsonl` | 定稿题库，只进不改 |

> Windows 上若 `python` 指向 Microsoft Store 的占位符，请使用 `py`。

## 检索链路

检索层不依赖数据库，直接读 JSONL，先用小语料把召回和评测跑顺。需要 `numpy / torch / transformers`：

```bash
py -m pip install -r requirements.txt

# 没有 API key 也能生成 2019 语料（规则抽取，kp_ids 为空）
py -m pipeline.build --paper data/raw/2019_paper.txt \
                     --answer data/raw/2019_answer.txt \
                     --year 2019 --provider mock

# 补全 1–40 的选择题解析（解析已落盘，这步不调模型、不需要 key）
py -m pipeline.analysis --draft data/work/2019_draft.jsonl \
                        --out data/curated/2019_analysis.jsonl \
                        --apply-to data/work/2019_draft.jsonl

# 模型首次运行会自动下载；国内网络先设镜像
set HF_ENDPOINT=https://hf-mirror.com     # PowerShell: $env:HF_ENDPOINT="https://hf-mirror.com"

# 命令行检索
py -m retrieval.cli "二级页式存储 虚拟地址怎么拆成页目录号和页号" \
                    --input data/work/2019_draft.jsonl --mode hybrid

# 批量评测（--show-misses 打印漏召回）
py -m retrieval.eval --queries data/eval/2019_queries_hard.jsonl \
                     --input data/work/2019_draft.jsonl \
                     --mode hybrid --show-misses
```

| 模式 | 说明 |
|---|---|
| `bm25` | 纯词面，中文按字 bigram 切 |
| `hybrid` | BM25 top-50 + 向量 top-50 → RRF → top-k |
| `hybrid-rerank` | 加词面精排（占位实现，只用于对照） |
| `hybrid-ce` | 加 `bge-reranker-v2-m3` Cross-Encoder 精排 |

2019 年 47 道题的基线（`bge-small-zh-v1.5`，k=5）：

| 查询集 | 模式 | Recall@1 | Recall@5 | MRR |
|---|---|---:|---:|---:|
| 47 条贴近原文 | bm25 | 0.979 | 1.000 | 0.989 |
| | hybrid | 1.000 | 1.000 | 1.000 |
| | hybrid-rerank | 0.936 | 1.000 | 0.968 |
| | hybrid-ce（精排 10 候选） | 0.979 | 1.000 | 0.983 |
| 32 条改写提问 | bm25 | 0.750 | 0.938 | 0.820 |
| | hybrid | 0.781 | 0.938 | 0.859 |
| | hybrid-rerank | 0.812 | 0.938 | 0.865 |
| | hybrid-ce（精排 30 候选） | 0.938 | 0.938 | 0.938 |
| | hybrid-ce（精排 10 候选） | 0.938 | 0.969 | 0.945 |

> 47 个 chunk 的语料太小，贴近原文的查询会饱和，**看改写集才有区分度**。Cross-Encoder 把 R@1 从 0.781 拉到 0.938：Q36 在向量召回里排第 1、BM25 得分为 0，RRF 融合后掉到第 10，精排 30 候选时被拉回第 1；精排只留 10 候选时它仍在 top5，且整体 R@5 反而更高。
>
> CPU 延迟（32 条 query）：精排 30 候选约 14 分钟，10 候选约 3.6 分钟。**服务默认用 10**，生产环境建议上 GPU 或 ONNX 量化。
>
> Q23 是评测标签问题：query 问“线程共享哪些资源”，但 2019 年第 23 题只问“线程描述哪项错误”，选项没讲共享资源，CE 给 0.0001 是合理的。检索的 gold 应该是“能回答问题的 chunk”，不是“同知识点的 chunk”。

### 选择题解析的来历

2019 卷 1–40 原来只转写了答案字母。网上能找到的解析要么是扫描 PDF（没有文字层），要么是
第三方版权内容，所以按「不搬运机构资料」的原则，用 DeepSeek 基于「题干 + 选项 + 正确答案」
生成，落在 `data/curated/2019_analysis.jsonl`：

- 36 条 `origin: ai`，4 条 `origin: manual`（Q05/Q10/Q27/Q37 模型算错，人工改写）；
- 合并后 `status` 仍是 `draft`，没有当成人审过的定稿；
- 生成时自检字数、含糊表述和未映射字符；返工用 `--ids ... --replace`，只重跑指定题；
- Q05、Q38 依赖原图，解析只给通用解法，具体数值需对照原卷。

> 解析不进召回文本：`retrieval/chunk.py` 只索引题干和选项，避免学生问概念时被「某年某题
> + 它的解析」占满候选集。解析只在生成阶段给模型看、并作为引用来源。所以补解析后检索指标
> 不变，变化体现在作答率上。

## 在线问答

```bash
# 无 API key 先跑通链路：mock provider 只取 top-1 的答案，不是真生成
KB_MODE=hybrid KB_PROVIDER=mock py -m uvicorn api.app:app --host 127.0.0.1 --port 8000
# 浏览器打开 http://127.0.0.1:8000

# 接 DeepSeek；hybrid-ce 会自动启用标定过的拒答阈值 0.1/0
set DEEPSEEK_API_KEY=sk-xxxx          # PowerShell: $env:DEEPSEEK_API_KEY="sk-xxxx"
set KB_MODE=hybrid-ce
set KB_FUSE_K=10
py -m uvicorn api.app:app --host 0.0.0.0 --port 8000
```

### 产品框架（v0.8）

页面分成四块，共用左侧会话/导航：

- **问答**：多轮对话；每条回答展示引用、召回资料、改写后的问题和执行耗时。真题和考点都能被引用，引用上会标出来源类型。
- **题库**：按科目筛选、按题干/选项/题号搜索，支持分页；题库接口只返回题干和选项，答案在详情里才出现。题目详情会列出关联考点。
- **知识库**：四科 → 章节 → 考点卡片，支持全文搜索和章节筛选。卡片是项目自写的考点摘要，标了 `source: self` / `status: draft`，不冒充教材原文；详情里可跳关联真题。
- **练习**：随机抽题，先作答再展开答案，用“会了/还不会”做本地统计；也可以从考点卡片直接进入“按考点练习”，只练该考点关联的真题。

问答检索同时覆盖真题和考点两个库：BM25 + 向量召回 → RRF → Cross-Encoder 精排，引用校验对两类 chunk 用同一套逐字规则。考点只作为概念解释的补充，涉及具体题目答案时以真题解析为准。

后端在原有单轮链路前加了一层轻量 Agent：

1. 纯寒暄/“你能做什么”不检索，直接回固定说明；
2. 检测到「它 / 这题 / 那为什么 / 还有呢」等指代时，用 DeepSeek 结合最近 4 条消息改写成独立问题；
3. 改写失败或没有 key 时退回规则拼接，不影响答题；
4. 指代实在补不出来时返回“需要补充”，而不是猜。

| 接口 | 说明 |
|---|---|
| `POST /ask` | 单轮问答：`{"query": "..."}`，返回 answer / citations / refused / reason / hits / latency_ms |
| `POST /chat` | 多轮问答：`{"query": "...", "history": [{"role": "user", "content": "..."}]}`，额外返回 `intent`、`rewritten_query`、`trace.steps` |
| `GET /chunks` | 题库列表：`q` / `subject` / `year` / `limit` / `offset`，只返回题干和选项 |
| `GET /chunks/{id}` | 题目详情：题干、选项、答案、解析、`source_ref`、`related_knowledge` |
| `GET /knowledge` | 考点列表：`q` / `subject` / `chapter` / `limit` / `offset`，附带科目计数和章节计数 |
| `GET /knowledge/{id}` | 考点详情：正文、公式、易错点、来源行号、关联真题 |
| `GET /meta` | 题目数、科目分布、年份、考点数、检索模式 |
| `GET /health` | 题目数、考点数、检索模式、provider |
| `GET /` | 四页前端，可直接在手机上打开 |

设置 `KB_ACCESS_TOKEN` 后，内容接口（`/ask`、`/chat`、`/chunks`、`/knowledge`、`/meta`）都必须带
`X-Access-Token`；`/health` 和静态页保持公开。`KB_RATE_MAX` / `KB_RATE_WINDOW` 控制问答接口的每 IP
限额（刷题库/知识库不占额度），`KB_DAILY_MAX` 是全局每日上限（0 为不限）。公网部署见 [docs/部署.md](docs/部署.md)。

考点文件默认读 `data/curated/knowledge.jsonl`，可用 `KB_KNOWLEDGE` 指向别的 JSONL。启动时会校验
`id / subject / chapter / title / body` 必填字段，并检查考点 id 不能和题目 id 冲突；同一 id 后出现的覆盖先出现的。

引用会带 `source_ref`（原文件 + 行号），拒答会写入 `data/work/gap_log.jsonl`（`KB_GAP_LOG` 可改），记录 query、拒答原因和最近命中题，作为补资料的输入。

链路是：理解问题（可选改写）→ 检索 → 拒答闸门 → 生成（强制 JSON）→ 引用逐字校验 → 失败重试一次 → 仍失败降级为拒答。

用户点名题号时（`2019-408-10`、`2019 年第 10 题`），检索会直接锁定该题并给 1.0 分，不依赖关键词召回；如果库里没有这个年份或题号，直接返回空结果拒答，不会拿别的题顶替。

拒答阈值不能拍脑袋。用 32 条正样本 + 16 条资料外负样本扫网格（`--knowledge` 把考点卡片也放进同一语料）：

```bash
py -m answer.eval_gate --positives data/eval/2019_queries_hard.jsonl \
                       --negatives data/eval/out_of_scope.jsonl \
                       --input data/work/2019_draft.jsonl \
                       --knowledge data/curated/knowledge.jsonl \
                       --mode hybrid-ce --fuse-k 10 \
                       --sweep 0.05,0.08,0.1 --sweep-margin 0,0.05
```

实测混合语料上 `min_score=0.1, min_margin=0` 时负样本拒答 93.8%、正样本误拒 9.4%，同拒答率下比 `0.05/0.05` 少 3 条误拒，所以 `hybrid-ce` 默认用这组值；`KB_MIN_SCORE` / `KB_MIN_MARGIN` 可覆盖。分差阈值（`min_margin`）会误伤「多个考点都相关」的问题，混合语料下默认关闭。

> `hybrid` 模式的分数是 RRF（0.016~0.033），绝对阈值没有意义，默认不拒答；要做可信拒答，必须用 `hybrid-ce` 的 Cross-Encoder 分数。

真实生成评测（需要 key，会调用几十次模型）：

```bash
set DEEPSEEK_API_KEY=sk-xxxx
py -m answer.eval_answer --queries data/eval/2019_queries_hard.jsonl \
                         --negatives data/eval/out_of_scope.jsonl \
                         --input data/work/2019_draft.jsonl \
                         --mode hybrid-ce --provider deepseek --fuse-k 10 \
                         --min-score 0.05 --min-margin 0.05 --show
```

输出包括：首次引用通过率、重试后通过数、闸门/模型/引用失败三类拒答、负样本拒答率、平均延迟。`--limit 5` 可先冒烟。

32 条改写 query + 16 条资料外负样本，`hybrid-ce / k_fuse=10 / 0.05 / 0.05`，DeepSeek 真实生成。
下表是补解析前后对比时测的历史数据（当时默认还是 0.05/0.05）；现行默认阈值是 `0.1/0`。
同一套 query 下，只改语料里的解析：

| 指标 | 未补解析（仅 7 题有） | 补全 40 题 |
|---|---:|---:|
| 作答 | 23/32 = 0.719 | 27/32 = 0.844 |
| 一次通过 / 重试通过 | 19 / 4 | 27 / 0 |
| 首次引用通过率 | 0.792 | 1.000 |
| 引用失败而拒答 | 1 | 0 |
| 平均生成尝试（闸门拒答记 0 次） | 1.03 | 0.88 |
| 平均延迟 | 6.5s | 6.6s |
| 负样本拒答 | 0.938 | 0.938 |

> 解析不进召回文本，所以检索指标不动；变化全在生成：模型能从解析里抄到完整句子，而不是只抄选项字母，首次引用通过率 0.792 → 1.000，重试和引用失败拒答一起归零。
>
> 剩下的 5 条拒答：4 条是闸门误拒（Q18/Q23/Q26/Q40，与标定的 12.5% 误拒一致），1 条是 Q32 的术语问题——模型认为「动态分区分配」不等于「连续分配内存的放置策略」。这属于保守拒答，按「宁可拒答不硬编」接受。
>
> 更早一轮（补解析前、且 3 条 query 语义有误时）还测过 prompt 的效果：收紧引用要求把首次引用通过率从 0.545 提到 0.864。那组数字只说明 prompt 的作用，不与上表混用。

## 部署

单机 Docker 部署，公网只暴露 Nginx；应用侧带访问口令、每 IP 滑动窗口限流和每日总额度。

```bash
cp .env.example .env          # 填 DEEPSEEK_API_KEY 和 KB_ACCESS_TOKEN
mkdir -p logs && sudo chown -R 1000:1000 logs
docker compose up -d --build  # 首次 10~30 分钟，含 2.4GB 模型
curl -s http://127.0.0.1:8000/health
```

```bash
TOKEN=$(sed -n 's/^KB_ACCESS_TOKEN=//p' .env)
curl -s http://127.0.0.1:8000/ask \
  -H 'Content-Type: application/json' -H "X-Access-Token: $TOKEN" \
  -d '{"query":"CSMA/CD 最小帧长怎么算"}'
```

完整步骤（安全组、Deploy Key、Nginx、HTTPS、更新与故障排查）见 [docs/部署.md](docs/部署.md)。

## 运行测试

```bash
py -m unittest discover -s tests -t . -v
```

## 目录结构

```
data/
  raw/      原始输入（试卷 txt + 答案 txt）
  curated/  人工整理 / 模型补全的内容（标 origin，待人工校对）
            knowledge.jsonl 为项目自写的 42 张考点卡片（source=self, status=draft）
  eval/     检索评测 query 集（query → gold id）
  work/     中间产物，可随时重跑（已 gitignore）
  out/      定稿 JSONL
pipeline/
  split.py     规则切题 / 切答案
  prompts.py   抽取 prompt
  extract.py   单题块 → JSON（mock / deepseek）
  validate.py  字段级 + 整卷级校验
  review.py    生成校对页、合并 patch
  analysis.py  用模型补选择题解析（单独落盘、可复核、可重跑）
  build.py     全流程 CLI
retrieval/
  chunk.py     JSONL → Chunk，一道题一个 chunk
  tokenize.py  中文 bigram + ASCII 词
  bm25.py      BM25 Okapi（纯 Python）
  vector.py    bge 向量化与暴力余弦索引
  fuse.py      RRF 融合
  rerank.py    词面精排 / bge Cross-Encoder
  search.py    双路召回 → 融合 → 精排
  eval.py      Recall@k / MRR
  cli.py       命令行检索
answer/
  agent.py     多轮意图识别 + 追问改写（LLM / 规则回退）
  gate.py      拒答闸门（分数 + 分差阈值）
  prompt.py    强制 JSON 的 system/user prompt
  providers.py DeepSeek / 离线抽取占位实现
  verify.py    引用逐字校验
  pipeline.py  拒答 → 生成 → 校验 → 重试，并记录分阶段耗时
  eval_gate.py 拒答阈值标定
  eval_answer.py 真实生成评测（一次引用通过率 / 拒答分布 / 延迟）
  cli.py       命令行问答
api/app.py     FastAPI 服务（/ask、/chat、/chunks、/knowledge、/meta、/health、/）
api/catalog.py 题库筛选、列表/详情序列化（纯函数）
api/knowledge.py 考点卡片加载、校验、筛选、题目双向关联
api/guard.py   访问口令 + 按 IP 限流 + 每日额度
api/gap.py     拒答缺口日志
web/index.html 四页前端（问答 / 题库 / 知识库 / 练习），无构建步骤
deploy/nginx.conf  反向代理 + 第二层限流
tools/preload_models.py 构建期预下载模型
Dockerfile / docker-compose.yml / .env.example
tests/      标准库 unittest
docs/       架构、数据管线、部署文档与配图
```

## 硬约束

- **一道题 = 一个 chunk**：题干、选项、答案解析、图永远不拆开。
- **一张考点卡片 = 一个 chunk**：正文写成可直接引用的完整句子；卡片标明 `source: self`，不冒充教材原文。
- **E 级校验错误不归零，不许进检索库。**
- **公式原样保留**，不让模型重写 LaTeX。
- **引用必须逐字命中原文**：校验不过重试一次，仍不过就拒答。
- **拒答优于硬编**：资料未覆盖时明说，并记录缺口日志。

## 版权

仅使用公开流传的真题原文，解析自行整理；不搬运机构付费资料。定位为个人学习交流，如涉版权问题请联系下架。
