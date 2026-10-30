# 借鉴 WeKnora：知识库上传与片段管理改造方案（v0.49）

> 结论先说：**不再把 WeKnora 当运行时后端，只借它的交互与进料思路。**
> 本项目的检索、闸门、生成、逐字引用校验与拒答原样保留，换掉的只是「文件怎么进来」。

## 1. 为什么不继续接 WeKnora 做后端

v0.48 把用户资料库整套指给 WeKnora（`WEKNORA_BASE_URL`）。实测与评估后放弃作为默认：

| 事实 | 影响 |
|---|---|
| 官方建议 4C8G，最小 DeepWiki 形态也要 2C4G | 线上是 2C / 3.7G，`app + docreader + postgres + redis` 挤不下 |
| docreader 镜像自带 LibreOffice + Playwright | 常驻内存远超本机余量；单机部署随时被 OOM 杀掉 |
| Lite 版缺 PDF / Word 解析 | 想要 Office 解析就得上完整栈，正好是最重的那部分 |
| 多一跳网络 + 多一套数据库 | 上传→解析的链路更长，状态与错误要跨两个系统对账 |

而本项目自己的 `collections / documents / doc_chunks`、`HybridSearcher`、引用校验已经完整，
缺的只是「把文件变成文字」这一小步。补齐这一小步，收益远大于维护一整套 WeKnora。

## 2. 借什么，不借什么

| WeKnora 的做法 | 本项目 | 理由 |
|---|---|---|
| 拖拽上传区 + 上传任务队列（单个进度、状态、可重试） | **照搬** | 这是它界面里最实用的部分：用户一眼知道文件在传到哪、解析完了没有 |
| 文档状态机 `processing → completed / failed`，失败留原因 | **照搬** | 解析是后台线程，界面不能假装已经好了；失败必须说清是扫描件、加密 PDF 还是格式不支持 |
| 片段（chunk）列表、逐段改正文、删除、重新切分 | **照搬** | 改错字、删噪声段直接决定引用质量，属于产品能力而非调优 |
| 片段编辑的乐观锁（revision） | **照搬** | 两个窗口同时改同一段，后保存的不能悄悄覆盖 |
| docreader（LibreOffice / Playwright 容器） | **不借** | 太重；改用纯 Python：pypdf + 标准库 zipfile 解析 docx/pptx + bs4 解析 HTML |
| 它的向量库、切片策略、rerank | **不借** | 本项目 512 字 / 50 重叠切片 + bge-small + RRF + Cross-Encoder 已跑通，换过去只会失去引用偏移 |
| `/knowledge-chat` 等生成能力 | **不借** | 会绕过逐字引用校验，硬约束失守 |
| 知识图谱、Agent、多模态（OCR） | **暂不做** | 图片 OCR 需要额外模型与内存；界面明确写「图片暂不支持 OCR」，不假装支持 |

## 3. 落地清单（本次已实现）

### 3.1 解析（`kb/parsers.py`）

- 支持：`.txt .md .markdown .csv .json .html .htm .pdf .docx .pptx`。
- 编码：`utf-8-sig / utf-8 / gb18030 / big5` 依次试，仍失败再用 chardet 猜；GBK 老讲义不乱码。
- PDF：pypdf 逐页取文字；无文字层（扫描件）明确报「没有文字层，多半是扫描件」；加密 PDF 报「有密码保护」。
- docx：`word/document.xml`，按 `<w:p>` 分段，段间换行。
- pptx：按 `slide1.xml, slide2.xml…` 数字排序，每页前加「第 N 页」。
- HTML：bs4 去掉 `script / style` 再取正文。
- 拒绝而不是硬凑：图片（无 OCR）、旧版 `.doc/.ppt/.xls`、未知扩展名、空文件、>20MB、>50000 字（提示拆分）。
- 所有错误都是给用户看的中文句子，直接显示在上传任务行里。

### 3.2 状态机与接口

- `documents` 新增 `status / error / bytes / source / updated_at`；`doc_chunks` 新增 `revision`。
- `POST /collections/{cid}/files`：先建 `processing` 文档，返回后由后台守护线程解析；
  `KB_SYNC_PARSE=1` 时同步解析（测试用）。
- 解析成功 → 切分 → 向量 → `replace_doc_chunks` → `completed`；
  失败 → `failed` + 原因。文本去重命中已存在的文档时同样落 `failed`，理由写「已经在库里」。
- `GET /collections/{cid}/documents/{doc_id}/chunks`、`PUT`（带 `revision`，冲突 409）、`DELETE`、`POST …/reparse` 全部有本地实现。
- 重新切分基于已存原文重跑 `chunking`，片段改错可以一键还原。

### 3.3 前端（`web/index.html`）

- 加资料卡片下方是虚线拖拽区 `.lib-drop`：拖进来、点一下或键盘 Enter/空格都行；
  副标写清支持格式、20MB、一次最多 3 个、图片不支持 OCR。
- 上传任务面板 `.upload-row`：文件名、大小、进度条、状态徽标（排队中 / 上传中 / 解析中 / 已索引 / 失败）、
  失败原因与「重试」。最多同时传 3 个，解析状态每 2 秒看一次、最多 10 次，不做无限轮询。
- 完成后任务行写「已索引 · N 段可引用」，toast 同步改成已索引，不留「正在解析」的误导。
- 文档列表显示 `N 字 · M 段 · 大小`；片段弹层按 `#1 #2 …` 编号，改完当下生效。
- 视觉全部沿用 `design.md` 既有色板与间距（v2.17），没有引入新颜色或新的字号阶梯。

## 4. 兼容与回退

- `WEKNORA_BASE_URL` 仍保留：配了就切回 WeKnora 后端，不配走本地进料。默认是本地。
- 本地进料不依赖任何新服务，只要 `pip install pypdf beautifulsoup4 chardet`
  （三者原本就随项目装好）。
- 没库时资料仍落 `data/work/kb/library.json`，状态机与片段管理照常工作。

## 5. 验证

- 后端：`python -m unittest discover -s tests -t .` → 406 个用例通过（13 跳过），
  其中 `tests/test_parsers.py` 覆盖各格式与拒绝路径，`tests/test_library_files.py` 覆盖状态流转与片段改删。
- 前端：Playwright + 系统 Edge，1440×900 与 390×844 各跑一遍
  「上传 docx → 已索引 → 打开片段 → 改一段并保存」，截图存 `data/work/kb-upload-*.png`、
  `kb-upload-done-*.png`、`kb-chunks-local-*.png`、`kb-chunks-local-saved-*.png`。
- 实测：docx 4KB → 950 字 / 2 段；片段修改返回「已保存，检索立即使用新内容」。
