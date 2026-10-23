# 408 考研知识库

面向 408（计算机学科专业基础综合）考生的真题问答知识库：**每个结论都能被考生自己验证，答不出来就明说，不硬编。**

> 状态：v0.1。离线数据管线已可跑通（mock 模式无需 API key）；在线问答链路尚在设计阶段。

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

## 运行测试

```bash
py -m unittest discover -s tests -t . -v
```

## 目录结构

```
data/
  raw/      原始输入（试卷 txt + 答案 txt）
  work/     中间产物，可随时重跑（已 gitignore）
  out/      定稿 JSONL
pipeline/
  split.py     规则切题 / 切答案
  prompts.py   抽取 prompt
  extract.py   单题块 → JSON（mock / deepseek）
  validate.py  字段级 + 整卷级校验
  review.py    生成校对页、合并 patch
  build.py     全流程 CLI
tests/      标准库 unittest
docs/       架构与数据管线文档、配图
```

## 硬约束

- **一道题 = 一个 chunk**：题干、选项、答案解析、图永远不拆开。
- **E 级校验错误不归零，不许进检索库。**
- **公式原样保留**，不让模型重写 LaTeX。
- **拒答优于硬编**：资料未覆盖时明说，并记录缺口日志。

## 版权

仅使用公开流传的真题原文，解析自行整理；不搬运机构付费资料。定位为个人学习交流，如涉版权问题请联系下架。
