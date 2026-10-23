# 检索评测集

| 文件 | 内容 |
|---|---|
| `2019_queries.jsonl` | 47 条，每题一条，措辞贴近题干原文，用作饱和基线 |
| `2019_queries_hard.jsonl` | 32 条，刻意换词、改用学生口吻，主要看这一组 |

`gold` 是题目 id，对应 `data/work/2019_draft.jsonl`（由离线管线生成，未入库）。

```bash
py -m retrieval.eval --queries data/eval/2019_queries_hard.jsonl \
                     --input data/work/2019_draft.jsonl --mode hybrid --show-misses
```

判分只看 gold 是否进入 top-k，不区分「同一知识点但不同题」。所以新增 query 时，
如果题目间高度相似，请把最匹配的那道题设为 gold，并在 `note` 里写明原因。
