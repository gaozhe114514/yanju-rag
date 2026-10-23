# 检索评测集

| 文件 | 内容 |
|---|---|
| `2019_queries.jsonl` | 47 条，每题一条，措辞贴近题干原文，用作饱和基线 |
| `2019_queries_hard.jsonl` | 32 条，刻意换词、改用学生口吻，主要看这一组 |
| `out_of_scope.jsonl` | 16 条资料外问题，用于标定拒答阈值（没有 gold） |

`gold` 是题目 id，对应 `data/work/2019_draft.jsonl`（由离线管线生成，未入库）。

```bash
py -m retrieval.eval --queries data/eval/2019_queries_hard.jsonl \
                     --input data/work/2019_draft.jsonl --mode hybrid --show-misses
```

判分只看 gold 是否进入 top-k。**gold 应当是「能回答该 query 的题」，不是「同知识点的题」**：
如果题库里没有任何一道题包含答案，这条 query 应放进 `out_of_scope.jsonl`，而不是硬指一个 gold。

已知歧义：`2019_queries_hard.jsonl` 里的 Q23 问「线程共享哪些资源」，但 2019 年第 23 题
只问「线程描述哪项错误」，选项没有讲共享资源；Cross-Encoder 判它不相关（0.0001）是正确的。
新增 query 时如遇到类似情况，在 `note` 里写明，别为了让指标好看而改 gold。
