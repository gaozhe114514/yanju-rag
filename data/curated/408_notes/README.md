# 408 四科课程笔记（第三方资料）

这里放的是「408 考研知识」知识库的正文来源，**不含真题**。

- 上游仓库：[WilliamTrouvaille/CS408_Note](https://github.com/WilliamTrouvaille/CS408_Note)
- 上游提交：`cbdb07777409b2e5da106aef1e140944b1b1b12b`（2025-10-25）
- 许可：MIT，原文见同目录 `LICENSE.txt`（Copyright (c) 2025 William Trouvaille）
- 抓取日期：2026-11-01
- 抓取范围：四个科目的 `课程笔记/` 目录，共 26 篇 markdown；未取 `EX-*` 考点总结与 PDF，避免把题目混进「只放学科知识」的库

## 入库时的处理

`scripts/seed_408_knowledge.py` 会：

1. 去掉 markdown 里的图片外链（`![...](https://...)` / `<img>`）。图片托管在 jsdelivr 与 GitHub，图里的信息不参与检索，留在正文里只是噪声；需要看图请回上游仓库。
2. 按段落把超过 45000 字的笔记切成多篇（`第六章 图（1/2）` 这种标记），保证低于单篇 50000 字上限。
3. 标题取正文第一个 `#` 标题，前面加科目名；`source` 记上游路径，便于回溯。

`LICENSE.txt` 与 `SOURCE-README.md` 原样保留，供追溯与署名。
