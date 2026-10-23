# 408 考研知识库

面向 408（计算机学科专业基础综合）考生的真题问答知识库：**每个结论都能被考生自己验证，答不出来就明说，不硬编。**

> 状态：v0.3。离线数据管线、检索、拒答、引用校验与 HTTP 服务已可跑通；数据库与生产级部署尚未接入。

![系统架构](docs/fig2_architecture.png)

## 文档

- [架构设计](docs/408-架构设计.md) — 在线链路、分层架构、选型理由、成本与落地顺序
- [数据管线](docs/数据管线.md) — 真题 → 结构化题库的五步流程与验收门槛
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

## 在线问答

```bash
# 无 API key 先跑通链路：mock provider 只取 top-1 的答案，不是真生成
KB_MODE=hybrid KB_PROVIDER=mock py -m uvicorn api.app:app --host 127.0.0.1 --port 8000
# 浏览器打开 http://127.0.0.1:8000

# 接 DeepSeek；hybrid-ce 会自动启用标定过的拒答阈值 0.05/0.05
set DEEPSEEK_API_KEY=sk-xxxx          # PowerShell: $env:DEEPSEEK_API_KEY="sk-xxxx"
set KB_MODE=hybrid-ce
set KB_FUSE_K=10
py -m uvicorn api.app:app --host 0.0.0.0 --port 8000
```

| 接口 | 说明 |
|---|---|
| `POST /ask` | 入参 `{"query": "..."}`，返回 answer / citations / refused / reason / hits / latency_ms |
| `GET /health` | chunk 数、检索模式、provider |
| `GET /` | 单页前端，可直接在手机上打开 |

引用会带 `source_ref`（原文件 + 行号），拒答会写入 `data/work/gap_log.jsonl`（`KB_GAP_LOG` 可改），记录 query、拒答原因和最近命中题，作为补资料的输入。

链路是：检索 → 拒答闸门 → 生成（强制 JSON）→ 引用逐字校验 → 失败重试一次 → 仍失败降级为拒答。

拒答阈值不能拍脑袋。用 32 条正样本 + 16 条资料外负样本扫网格：

```bash
py -m answer.eval_gate --positives data/eval/2019_queries_hard.jsonl \
                       --negatives data/eval/out_of_scope.jsonl \
                       --input data/work/2019_draft.jsonl \
                       --mode hybrid-ce --fuse-k 10 \
                       --sweep 0.02,0.05,0.1,0.2 --sweep-margin 0,0.01,0.05
```

实测 `min_score=0.05, min_margin=0.05` 时负样本拒答 93.8%、正样本误拒 12.5%，所以 `hybrid-ce` 模式默认就用这组值；`KB_MIN_SCORE` / `KB_MIN_MARGIN` 可覆盖。

> `hybrid` 模式的分数是 RRF（0.016~0.033），绝对阈值没有意义，默认不拒答；要做可信拒答，必须用 `hybrid-ce` 的 Cross-Encoder 分数。

## 运行测试

```bash
py -m unittest discover -s tests -t . -v
```

## 目录结构

```
data/
  raw/      原始输入（试卷 txt + 答案 txt）
  eval/     检索评测 query 集（query → gold id）
  work/     中间产物，可随时重跑（已 gitignore）
  out/      定稿 JSONL
pipeline/
  split.py     规则切题 / 切答案
  prompts.py   抽取 prompt
  extract.py   单题块 → JSON（mock / deepseek）
  validate.py  字段级 + 整卷级校验
  review.py    生成校对页、合并 patch
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
  gate.py      拒答闸门（分数 + 分差阈值）
  prompt.py    强制 JSON 的 system/user prompt
  providers.py DeepSeek / 离线抽取占位实现
  verify.py    引用逐字校验
  pipeline.py  拒答 → 生成 → 校验 → 重试
  eval_gate.py 拒答阈值标定
  cli.py       命令行问答
api/app.py     FastAPI 服务（/ask、/health、/）
web/index.html 单页前端
tests/      标准库 unittest
docs/       架构与数据管线文档、配图
```

## 硬约束

- **一道题 = 一个 chunk**：题干、选项、答案解析、图永远不拆开。
- **E 级校验错误不归零，不许进检索库。**
- **公式原样保留**，不让模型重写 LaTeX。
- **引用必须逐字命中原文**：校验不过重试一次，仍不过就拒答。
- **拒答优于硬编**：资料未覆盖时明说，并记录缺口日志。

## 版权

仅使用公开流传的真题原文，解析自行整理；不搬运机构付费资料。定位为个人学习交流，如涉版权问题请联系下架。
