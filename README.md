# 引证 · 可引用知识库（内置 408）

以 408（计算机学科专业基础综合）真题为内置语料的知识库问答系统：**每个结论都能被引用回原文，答不出来就明说，不硬编。** 不考 408 也能用——把讲义、笔记、手册贴进自建知识库，同一套检索、引用校验与拒答直接生效。

> 状态：v0.47。**缺工具就去设置里开，不用再答「查不了」**：照 DeepTutor 的工具 / MCP 设置思路，设置页「服务」组多了一个「工具」，日常模式可打开「天气查询」（Open-Meteo，无需密钥）。开着时日常提问会把工具名单随请求发出，服务端按白名单执行、问法不沾边（例如「帮我排个计划」）就不调用，回答元信息写 `工具 · 天气 · Open-Meteo`，调用阶段提示「正在查实时数据…」，工具查不到就写 `工具失败 · 天气` 并给出原因，不编天气。资料模式永远不调用工具，仍只按原文回答；MCP / CLI / 子代理还没接入，设置里如实写「未接入」，不摆假开关。规范见 [design.md](design.md) v2.15 §6.16。
>
> v0.46：**拒答不再是一条死路，学习空间也不再空转**：资料模式下问日常问题（「今天郑州天气怎么样」）以前只能看到拒答，现在拒答下面多一条「切到日常再问 →」，点一下把同一句话切到日常模式重发，不用自己找模式胶囊；系统判定码拒答（召回分数不足 / 区分度不足）没有具体缺口，也照样给「换个问法」和这条日常退路，生成服务异常时不给误导性的建议。顺手修掉两个基础问题：学习空间每次渲染都强制重拉 `/collections` 再渲染自己，形成死循环，页面一直在转圈状刷新；没有口令时这个 401 也会被反复重试几百次并叠出多个设置框，现在失败一次就记住，验证口令成功后自动重试，知识库页也不再显示「-1 个自建知识库」。规范见 [design.md](design.md) v2.14 §6.10。
>
> v0.45：**拒答的收尾更实在**：拒答时召回清单默认摊开，标题写「召回 N 条 · 都没能作为依据」，不用再点开才知道检索到了什么，展开后占满整行；自建库的缺口入口直接开到出问题的那个库（`#/collection/<id>`），不再落到笼统的知识库列表；拒答不出「存到笔记本」（没有正文可存，免得存出空笔记），「记入日志」改写具体缺口。规范见 [design.md](design.md) v2.13 §6.10。
>
> v0.44：**拒答说清缺什么，引用悬停先看原话**：以前拒答只有一句「资料里没有足够依据」和系统判定码，模型自己写下的缺口（例如「资料里只讲了 B 树的定义，没有删除步骤」）被埋掉了。现在问答响应多一个 `gap_detail`（系统判定码与空话不出），拒答时正文下面接一条「缺口」说明，行尾按来源给「换个问法 →」（内置语料，聚焦输入框）或「去知识库补一段 →」（自建库）；复制也会带上这句。桌面端把引用胶囊悬停 120ms 就能先看被引用的原话，点开仍是全文高亮；触屏没有悬停，行为不变。规范见 [design.md](design.md) v2.12 §6.10。
>
> v0.43：**改大纲先看一眼再应用**：定大纲的会话里，导师改出来的新大纲不再直接生效，先出一张预览卡，写清会新增几个、删掉几个、应用后一共几个；如果会丢掉已掌握知识点的记录，卡片用红字点名。点「应用这版」才落库，「先不改」保持原样。第一次建大纲不拦，比对没有变化也不拦。规范见 [design.md](design.md) v2.11 §6.11。
>
> v0.42：**一条路可以有多段对话**：详情页「对话记录」里能新开一段、切回旧的一段或删掉；换对话只换聊天记录和模式，大纲、掌握证据、复习计划全部共用，所以「换一个角度重来」不会清零进度。没有内容的空对话不占位置，回合进行中不允许切换。规范见 [design.md](design.md) v2.10 §6.11。
>
> v0.41：**已掌握的知识点能点开看证据**：在大纲里点一个已掌握的知识点，弹层里能看到它的最近作答（对错、题型、难度、日期）、掌握日期和下次复习时间、以及跳过 / 修题 / 暂缓的记录。掌握一直是系统按证据算的，这条弹层让它算过什么变得可查；弹层只读，改掌握的唯一办法仍然是作答。规范见 [design.md](design.md) v2.9 §6.11。
>
> v0.40：**到期复习有入口，学习档案看得见**：路线列表顶部多一条复习横幅「N 个知识点该复习了」，点了直接进到期最多那条路的复习会话，不用一条条点开找；详情页标题下面一行「关于你」，显示你已经会什么、想达到什么、能投入多少时间、希望怎么讲——只显示你真说过的，想改就在对话里说。规范见 [design.md](design.md) v2.8 §6.11。
>
> v0.39：**精通之路说清「离掌握还差什么」**：门槛由引擎按证据算，所以全对也未必立刻算掌握——以前界面只显示「还没过」，学习者会以为系统坏了。现在判分结果带回 `grade.gate`，判定条下面多一行「离掌握还差：…」，逐条说明缺的是作答次数、最近一次对错、正确率，还是还没答对难度 ≥2 的题；概念 / 判断类则写「用自己的话讲清一次」。规范见 [design.md](design.md) v2.7 §6.11。
>
> v0.38：**精通之路补上回合后守卫与退路**：模型说「下面这道题：」却没把题给出来、把选项写成散文、或声称题卡已给——这些都会在解析时被拦下，重试一次；仍不过就如实收尾，不硬凑一道题。题卡上多了三个退路按钮：**跳过这题 / 这题有问题 / 暂缓这个知识点**，跳过与修题都不记作答、不给掌握分，暂缓的知识点在左侧大纲里变成虚线点，点一下放回来。后端 `/mastery/turn` 新增 `control` 字段（skip / repair / defer / resume），前端仍然是那一条无状态会话。规范见 [design.md](design.md) v2.6 §6.11。
>
> v0.35：**精通之路补上「学」的主链路**：会话改成三种显式模式——定大纲 / 学习 / 复习，模式是会话状态（`path.mode`），切模式不断线。学习模式里导师讲一小段就出题，**出题即结束这一轮**，学习者作答是下一条消息；后端在调用模型之前用纯函数判分（最近几次作答 + 难度 + 概念类只认自述），把结果当作既成事实交给导师解释，**掌握由引擎判定，不由模型宣布**。复习按 1 / 3 / 7 / 21 天排出到期项，答错回学习。界面上新增模式开关、起步卡、题目卡（选项带正文、难度五点、自述框）、判定条和大纲树进度条；没有生成模型时仍能判分，只是不出新题。旧路线（没有模块数据）继续走原来的步骤清单。规范见 [design.md](design.md) v2.5 §6.11；DeepTutor 逐行对照见 [docs/精通之路-DeepTutor对照.md](docs/精通之路-DeepTutor对照.md)。
>
> v0.34：**精通之路的大纲改成和导师一起定**：新建只走两步（目标 → 资料），选完直接进「导师工作台」——左边是大纲树（模块 + 知识点 + 进度），右边是会话。导师先读资料，再从学习档案里缺的那几项里挑 1–4 个问题问（结构化提问卡：多题切换、选项带说明、可自由作答、可跳过），答完起草 4–8 个模块、每模块 2–6 个知识点，每个模块写一句「学完能做什么」。之后说「把排序拆细」这类话，只动提到的那一块，已掌握的知识点原样保留（服务端按名字沿用旧 id 与 `done`）。资料没覆盖的地方导师直说，不编页码题号。后端新增无状态 `POST /mastery/turn`（`answer/tutor.py`），没有生成模型时退到离线演示并在会话里标明。
>
> v0.33：问答分**两种模式**：输入条最左边一颗胶囊在「资料」与「日常」之间切（存 `408-chat-mode-v1`）。资料模式（默认）仍只按资料原文回答、逐字校验引用；日常模式不查资料、也不带引用，回答的元信息写「日常对话 · 未查资料」，适合排计划、改文字、问点常识。问 408 的题请留在资料模式，从「今日」提问会自动切回来。后端 `POST /chat` 与 `/chat/stream` 新增 `mode` 字段，日常分支不检索、不写缓存、不记缺口日志，两条链路的硬规则互不混用。顺手把「伙伴」下线后留下的死代码（JS 段、`.partner-*` 样式、路由特判与事件绑定）删干净了。规范见 [design.md](design.md) v2.2 §6.10。
>
> v0.32：设置页照 DeepTutor 重做：左边 248px 分组导航（个人：通用 / 外观 / 学习 / 本机数据；服务：连接）+ 搜索，右边分区标题 + 一行一项；偏好改动先留在草稿里，点底部的「应用更改」才写进 `408-prefs-v1`。**配色换成 DeepTutor 原样的四套**——默认（雪，纯白 + 蓝）/ 奶油（暖纸 + 陶土）/ 深色（近黑暖调）/ 琉璃（深紫渐变 + 毛玻璃面板），设置「外观」里用四张 5:3 预览卡选，`<head>` 内联脚本首帧挂 `data-theme`，没选过就跟系统的深色偏好。**精通之路的新建也照 DeepTutor 走完整个流程**：先写目标并选一枚徽标（≤2000 字），再勾素材（知识库 / 考点 / 题库 / 对话），然后和老师排大纲——老师先问「已会什么 / 学完什么算数 / 多少时间 / 怎么讲」，再按勾选的考点起草；建完没排大纲的路线，详情页有「还没有大纲」的落点。「伙伴」入口、列表与对话页下线（还没决定要不要做），旧地址一律回问答。规范见 [design.md](design.md) v2.1 §2.6 / §6.11 / §6.16。
>
> v0.29 补上了**设置页**：照 DeepTutor 的设置布局简化成左边四项分区（通用 / 学习 / 连接 / 本机数据）、右边一行一项——通用里改考试日期、启动页（今日 / 问答 / 精通之路）和「减少动效」；学习里设精通之路默认考点数；连接里录入访问口令、看服务端状态（存储 / 检索 / 模型 / 语料 / 考点 / 自建库）并跳质量看板；本机数据里看各类内容的条数，按组清除对话、学习进度、笔记与自定内容，或一次清空（保留访问口令）。侧栏底部的状态钮现在进设置页，学习空间的「个性化」组也有一张入口卡。
>
> v0.28 照 DeepTutor 重做了**精通之路**：路线列表从卡片墙改成单行清单，详情页改成「进度条 + 下一步卡 + 按章节分组的步骤清单 + 手风琴展开自测」，答对关联真题那一步才变绿；同时从 DeepTutor 的另外两套主题借来配色——学习区用 `snow` 的蓝 `#2563eb`，伙伴区用 `glass` 的紫 `#7c3aed`，问答区仍是陶土主色，三个区各一个强调色。v0.27 的问答界面重做不变：助手消息不再带头像、徽标和大段引用块，引用收成一行小胶囊，点开在弹层里看逐字原话；核对结果与召回折进一行灰色元信息，复制 / 存到笔记本 / 记入日志改为鼠标悬停才出现；输入条改成「问题在上、库与伙伴在下」的一张 26px 竖排卡，欢迎态去掉眉标与 2×2 卡片，改成四行纯文字推荐。v0.26 的通用品牌不变：把站点从「408 考研专用」改成**通用资料问答**：站点名改为「引证」，副标「资料为据 · 句句可查」，标志由 408 换成单字「引」；问答、首页、伙伴与今日页的话术从「真题或考点」改成「资料」，凡是 408 的地方都标「内置」；首页推荐卡下面加了「想用自己的资料？把讲义贴进知识库 →」直达入口。v0.25 的知识库能力不变：把「408 语料」抽象成**知识库**（照 DeepTutor 的三层：知识库 → 文档 → 片段）：内置「408 考研」只读库不可删，用户可自建知识库、贴资料（20–50000 字，切 512 字 / 50 重叠、保留原文偏移）、在输入条左侧切库提问，引用弹窗能高亮回原文原句。用户资料落在 `collections` / `documents` / `doc_chunks` 三张新表，与 408 的 `chunks` 物理隔离；每个库用独立 searcher（关掉题号解析）、独立缓存命名空间，拒答日志带 collection id。v0.24 的多题库、CSV / Markdown 导入导出与「用资料出题」保持不变。v0.24 之前的能力见下文各节：数据管线、检索、拒答、真实生成、引用校验、HTTP 服务与单机 Docker 部署已跑通，并在腾讯云轻量（2核4G）实机验证。408 真题是不可删除的**预置题库**（235 题，仍走服务端语料），用户自建题库、给题打**标签**、每道题都带**出处**字段；自建题库、导入题与生成题只存在本机 localStorage，换设备不同步。**回答为流式输出**（`POST /chat/stream`，阶段提示 + 逐字追加 + 停止键；引用未逐字校验通过前不展示，校验失败会清空重试一次，仍失败整条拒答），**导航为四项**（问答 / 学习 / 学习空间 / 任务清单）。质量看板在设置页「连接」分区的「运行质量」。人格、技能与知识库只影响「用哪些资料」和「怎么说」，引用必须逐字命中、资料没覆盖就拒答这两条硬规则不变（日常模式见顶部 v0.33）。视觉沿用 DeepTutor Cream 主题：奶油画布、陶土主色、衬线页标题、16px 卡片，规范见 [design.md](design.md)。408 题库现有 2019、2020、2021、2023、2024 五卷共 235 题，解析分别来自 AI 生成和公开文字版整理，均标为 `draft`、待人工校对；2022、2025 的解析源是扫描件或 OCR 噪声过大，暂未入库；答案正确率评测尚未接入。存储层为 **PostgreSQL 16 + pgvector**：JSONL 是构建产物和语料种子，启动时按指纹同步进库，向量召回走数据库；未配置 `DATABASE_URL` 时退回内存索引（知识库退到 `data/work/kb/library.json`）。另有**应答缓存**（`answer_cache`，默认 24h，拒答不缓存）、**缺口统计** `/stats/gaps` 与**反馈接口**（`answer_feedback`、`POST /feedback`、`GET /stats/feedback`）。伙伴、笔记本、题库、日志、精通之路与任务清单数据只存在浏览器 localStorage，不登录、不跨设备同步。

![系统架构](docs/fig2_architecture.png)

## 文档

- [设计规范](design.md) — 配色、字体、间距、组件与布局的唯一来源，新页面先读
- [架构设计](docs/架构设计.md) — 在线链路、分层架构、选型理由、成本与落地顺序
- [数据管线](docs/数据管线.md) — 真题 → 结构化题库的五步流程与验收门槛
- [部署](docs/部署.md) — 云服务器 ECS 上的 Docker + Nginx + 口令 + 限流
- [全链路流程图](docs/流程图.html) — 离线建库 → 在线问答（拒答 + 引用逐字校验）→ 精通之路 → 页面；8 条泳道、19 个节点带源码出处，单文件 HTML，浏览器直接打开
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

JSONL 是构建产物，也是数据库的语料种子；线上由 Postgres 承载题目、考点和向量。没有数据库时（单测、离线开发）自动退回内存索引，两条路径共用同一套检索代码。需要 `numpy / torch / transformers`：

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

# 2020 / 2023 卷：答案与解析已经在答案文本里，同样离线生成
py -m pipeline.build --paper data/raw/2020_paper.txt \
                     --answer data/raw/2020_answer.txt \
                     --year 2020 --provider mock
py -m pipeline.build --paper data/raw/2023_paper.txt \
                     --answer data/raw/2023_answer.txt \
                     --year 2023 --provider mock

# 2021 / 2024 卷：答案文本只有字母，解析已由 pipeline.analysis 落盘
py -m pipeline.build --paper data/raw/2021_paper.txt \
                     --answer data/raw/2021_answer.txt \
                     --year 2021 --provider mock
py -m pipeline.analysis --draft data/work/2021_draft.jsonl \
                        --out data/curated/2021_analysis.jsonl --merge-only \
                        --apply-to data/work/2021_draft.jsonl
py -m pipeline.build --paper data/raw/2024_paper.txt \
                     --answer data/raw/2024_answer.txt \
                     --year 2024 --provider mock
py -m pipeline.analysis --draft data/work/2024_draft.jsonl \
                        --out data/curated/2024_analysis.jsonl --merge-only \
                        --apply-to data/work/2024_draft.jsonl

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

## 存储层（PostgreSQL + pgvector）

线上只有一个数据库服务，不做过度拆分：

| 表 | 内容 |
|---|---|
| `chunks` | 235 道题 + 42 张考点卡；题干/选项/解析、原始 JSONL、`vector(512)` 向量 |
| `meta` | 语料指纹、embedding 模型名与维度 |
| `gap_logs` | 拒答日志（query / 原因 / top1 命中 / 分数），用来反哺资料补充 |
| `answer_cache` | 应答缓存（缓存键 / 原问题 / 完整响应 JSON / 写入时间）；只存作答成功的回答 |
| `answer_feedback` | 回答反馈（原问题 / 回答 / 是否有帮助 / 差评原因 / 备注 / 引用 id / 是否命中缓存） |

启动时 `api.app` 读 JSONL → 计算「语料 + 模型」指纹 → 和 `meta` 里的比对，只有变了才重新 embedding 并 upsert；没变直接复用库里的向量。浏览接口的筛选分页、科目统计走 SQL，向量召回是 `ORDER BY embedding <=> query LIMIT k`。

**为什么 BM25 还留在应用进程**：Postgres 的中文全文检索离不开 zhparser 一类扩展，换来的只是「少一个内存索引」，却多一个编译安装的扩展和一个要调试的分词器。所以关键词召回继续用项目里的 bigram + BM25 Okapi，向量召回交给 pgvector——数据库承担的是存储、过滤和向量检索，不是硬塞所有检索逻辑。

手工重建或核对数据：

```bash
DATABASE_URL=postgresql://rag:***@127.0.0.1:5432/rag py -m db.load
docker compose exec db psql -U rag -d rag -c "SELECT kind, count(*) FROM chunks GROUP BY kind"
```

### 题目与解析的来历

五卷都只用于个人学习交流，原始文件头注明了出处；**没有一份被标成人工审过的定稿**。

- **2019 卷**：试卷和答案来自 GitHub `xiaolei565/aimto408`（文字抽取后人工修复了第 31–34 题
  双栏串行）。1–40 原卷只转写了答案字母，网上解析要么是扫描 PDF、要么不符合引用校验，
  因此用 DeepSeek 基于「题干 + 选项 + 正确答案」生成，落在 `data/curated/2019_analysis.jsonl`：
  36 条 `origin: ai`，4 条 `origin: manual`（Q05/Q10/Q27/Q37 模型算错，人工改写）；
  生成时自检字数、含糊表述和未映射字符，返工用 `--ids ... --replace` 只重跑指定题。
- **2020 卷**：试卷来自 GitHub `neville-studio/408-exam-paper` 的社区重制文字版（pypdf 抽取，
  人工修复了第 1、13、19、27、31、41、43 题的转录问题）；答案和解析来自
  `datastructure.ouxinyu.cn/PastPapers/Answer2020.pdf`，选择题答案与答案表逐题核对过。
  第 10 题原解析为空，由项目补写并按 4 阶 B 树插入过程验算。
- **2023 卷**：试卷和解析同样来自 `neville-studio/408-exam-paper`（`papers-rebuild/2023.pdf`
  与 `answers/2023-answer.pdf`，均有文字层）；选择题答案取自解析里逐题给出的结论，并与
  答案速查表交叉核对过；上标被 PDF 拆成独立行的地方按题修正。
- **2021 / 2024 卷**：试卷来自 `neville-studio/408-exam-paper` 的 `papers-rebuild`（有文字层，
  人工修正了上标拆行、2021 第 43 题分值等转录问题）；这两年的答案 PDF 是扫描件，没有文字层，
  因此 1–40 的答案字母取自 `csgraduates.com` 的公开解析页，只取字母、没有复制它的解析正文。
  1–40 的解析由 DeepSeek 基于「题干 + 选项 + 正确答案」生成，落在
  `data/curated/2021_analysis.jsonl` / `2024_analysis.jsonl`，每题都标 `origin: ai`。
  41–47 的综合题**只有题干、没有解析**，问「怎么做」时只能引用题干，不能给出完整解答。
- 五卷的 `status` 都是 `draft`；题干引用「下图」的题，图片尚未入库，解析只给通用解法，
  具体数值需对照原卷。图片抽取和展示是下一步的数据工作。

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
set KB_CACHE_TTL=86400                # 应答缓存秒数，0 = 关闭
py -m uvicorn api.app:app --host 0.0.0.0 --port 8000
```

应答缓存只在配置了 `DATABASE_URL` 时生效，键由「检索模式 + provider + 精排候选数 + 语料指纹 + 归一化问题」组成；`/chat` 用改写后的问题查缓存。响应里的 `cached` / `cache_age_s` 标明是否命中及回答年龄，命中时 `trace` 只保留「应答缓存」一步。**拒答和寒暄/澄清不缓存**，避免把一次失败固化 24 小时。伙伴 / 人格 / 技能传入 `style` 时，缓存键会额外拼上 style 的 SHA-1 前缀：同一个问题换个伙伴会重新生成，不会串用别人的语气。

`style` 是可选字段（≤400 字），服务端会压成单行再注入 system prompt 的【讲解方式】段。该段明确写出「只影响措辞、语气和组织顺序，不得改变事实与结论，不得放宽引用要求，也不得引入资料以外的知识」；生成结果仍要过同一套逐字引用校验，校验失败照样重试一次、仍失败则拒答。

### 产品框架（v0.25，对话优先 + 流式问答 + 多题库 + 自建知识库 + 学习闭环）

入口仍是「问答」：打开就是对话，欢迎态的问候、倒计时、连续天数、近 7 天问答量与推荐问题不变；侧栏「最近对话」按今天 / 昨天 / 最近 7 天 / 更早分组。主导航为 **问答 / 学习 / 学习空间 / 任务清单** 四项，手机底部同名 tab 栏；「今日」暂时退出导航，页面与 `#/today` 路由保留。所有个人数据（伙伴、笔记本、日志、精通之路、任务清单、人格、技能、记忆）只存在浏览器 localStorage，不登录、不设置、换设备不同步。界面按 [design.md](design.md) v1.6 的奶油 · 陶土规范实现：对话与伙伴对话 820px，题库 / 考点 / 知识库 / 学习空间总览 / 任务清单 1180px，今日 / 精通之路 / 练习 / 学习空间子页 900px；页头 27px 衬线标题，卡片 16px 圆角与 1px 暖边框。

- **问答**：默认首页，流式输出。多轮对话；用户消息为浅奶油气泡，助手回答不套卡片、直接排版，边生成边显示（阶段提示 + 闪烁光标），引用于逐字校验通过后才出现；校验失败会清空未通过的文字并重试一次，仍失败则整条拒答。生成中发送键原位变停止键，点停止丢弃未校验内容并恢复输入。每条回答展示引用、召回资料、改写后的问题和执行过程，真题和考点都能被引用。输入条左侧的人格胶囊可以切换预设，把「讲解方式」随请求发给服务端；引用校验与拒答规则不变。助手回答下方可「复制」「存到笔记本」「记入日志」。底部是 24px 胶囊输入条 + 圆形发送键，桌面自动聚焦、手机不自动弹键盘。
- **今日**：辅助工作台（不在主导航，保留 `#/today` 入口）。① 一句话开场，按时间、连续天数、上次学习间隔生成；② 初试倒计时（默认按「12 月倒数第二个周六」估算，可改成本机日期）与「连续 N 天」「近 7 天：问 X · 练 Y」；③ 常驻提问框；④ 今天这一题：优先从错题本、其次从上次提问引用过的真题里挑一道**单选题**，当天固定，答完记「会了/还不太会」；⑤ 接着上次：上次对话 + 上次练习，一键继续。断签不惩罚。
- **学习 · 精通之路**：写目标、勾资料，然后和导师在会话里定大纲。导师先读资料并问 1–4 个问题（基础 / 薄弱点 / 时间 / 讲法），再排成 4–8 个模块、每模块 2–6 个知识点；之后可以继续让它加、删、拆、并，没提到的模块和已掌握进度都保留。详情页左边大纲树、右边会话，切到「学习」按模块逐个走：能配上关联真题的，选完才核对答案，答对才算完成；配不上的可以「读完了，记住这一步」。走完会写一条学习日志。路线和会话存在本机，刷新后保留。
- **学习 · 练习**：三个标签页。① 随机练习：随机抽题，先作答再展开答案，用「会了/还不会」做本地统计；也可从考点卡片进入「按考点练习」。② 错题本：按科目统计掌握度，支持单题重练和全部重练。③ 10 题小测：按科目抽 10 道单选题组卷，提交即自动判分，错题直接进错题本；完成后自动写学习日志。
- **学习空间 · 知识库**：自己贴讲义、笔记、手册，切成可引用片段后直接提问，答案按资料原文逐字引用。内置「408 考研」只读知识库不可删；自建库带名称、说明，可加资料、看原文、整库删除。输入条左侧的知识库胶囊切换提问范围（不选就是内置 408 语料）。详见下节。
- **学习空间 · 题库**：题库改成卡片墙，408 真题是不可删除的**预置题库**（按科目 / 年份筛选、分页、题目详情列出关联考点，仍走服务端语料），下面是用户自己的题库。自建题库带名称、说明与标签；题目按标签筛选，搜索同时命中题干、选项、解析、标签与出处；每道题都记 `source`（标题 + 原文摘句 + 偏移）。自建题库支持删除（连题一起删，二次确认）。
- **题库 · 导入 / 导出**：CSV 表头 `stem,option_a,option_b,option_c,option_d,answer,explanation,tags,source`，四个选项全空即填空题；Markdown 用 `## 题干` + `A.` 选项 + `答案：` / `解析：` / `标签：` / `出处：`。可点选文件或直接粘贴，导入前把行级错误一次列清（行号 + 原因），有错不许导入；题干重复的自动跳过。导出为带 BOM 的 CSV，Excel 直接打开不乱码。
- **题库 · 用资料出题**：粘贴讲义、笔记或手册（80–20000 字），选 3 / 5 / 8 / 10 道，`POST /bank/generate` 调模型出单选题。每道题必须附一段**逐字来自资料**的 `source_quote`（去空白后比对，至少 8 字），对不上的题直接丢弃并说明原因，生成的题数可能少于请求数；题目只落在本机。离线演示模式（未配 `DEEPSEEK_API_KEY`）会如实提示不能出题，不返回假题。
- **题库 · 练习**：自建题库可「开始练习」，随机顺序逐题作答，单选与填空都支持（填空答案可用 `|` 分隔多个可接受写法）；提交后给出对错、解析与出处，并把对错记进题库的答题统计。408 预置题库仍走原来的练习 / 10 题小测。
- **学习空间 · 考点**：四科 → 章节 → 考点卡片，支持全文搜索和章节筛选。卡片是项目自写的考点摘要，标了 `source: self` / `status: draft`，不冒充教材原文；详情里可跳关联真题。章节导航带「考点数 · 涉及真题数」，「考频榜」按关联真题数排出考点热度。
- **学习空间 · 笔记本**：可建多个笔记本，从问答回答一键保存（连同引用原文），也可手写条目；条目只在本机，删除不可恢复。
- **学习空间 · 学习日志**：记录当前目标、上次交接（自动汇总最近对话 / 练习 / 精通之路）与确认过的学习记录；小测完成、精通之路完成、任务清单任务完成会自动写入，也可手记。
- **学习空间 · 对话**：按更新时间列出全部对话，支持搜索标题与消息内容、重命名、删除、继续。
- **学习空间 · 个性化**：人格预设（一句话风格，可选用到问答）、技能（内置分步推导 / 例题先行 / 结论先行 / 易错点提醒 / 讲完自测，可自建）、学习记忆（目标、目标分数、薄弱科目、学习偏好）。这些只会拼进生成阶段的「讲解方式」，不得改变事实、引用与拒答；没有接入 MCP / CLI，也没有本地工具沙箱。
- **伙伴（已下线，v0.30）**：入口、列表与对话页已从界面移除，用户还没决定要不要做；旧地址 `#/partners`、`#/partner` 一律回问答。人格预设与技能仍在，问答时只把「讲解方式」拼进生成阶段，引用、原题入口与拒答规则不变。
- **任务清单**：三列待办 / 进行中 / 已完成；支持新建、编辑标题与备注、拖动或箭头移动、归档、查看归档并恢复、指定科目、关联一段对话（卡片上可直接跳回）。清单底部显示各列计数，所有数据只在本机。
- **设置 · 运行状态**：质量看板从侧栏设置进入。顶部四张 KPI（窗口内拒答数、缓存命中率、本进程问答量、语料规模）；中部拒答原因条形与每日拒答柱图；底部最近拒答。数据源是 `/stats/overview`。

### 自建知识库（v0.25）

把「408 语料」抽象成「知识库」，同一套检索 / 拒答 / 引用校验直接复用到用户自己的资料上。结构照搬 DeepTutor 的三层：**知识库（collection）→ 文档（document）→ 片段（doc chunk）**。

- **物理隔离**：内置 408 语料仍在 `chunks` 表（只读）；用户资料落在 `collections` / `documents` / `doc_chunks` 三张新表，`doc_chunks` 带 `collection_id`、`start_off` / `end_off`。删库靠外键级联，不影响预置语料。
- **切分**：`kb/chunking.py` 是 DeepTutor `chunk_size=512 / chunk_overlap=50` 的纯标准库实现——先按句末标点攒到目标长度，叠加 50 字重叠；没有标点的长串按 400 字硬切。每段都记原文起止偏移，前端据此把引用原句高亮出来（`GET /collections/{cid}/documents/{doc_id}`，弹窗里滚动到 `<mark>`）。
- **检索**：每个知识库各建一个 `HybridSearcher`（BM25 + pgvector → RRF → Cross-Encoder），`exact_refs=False` 关掉题号解析——用户资料里没有「2019 年第 10 题」，不能因为正则不命中就拒答。精排模型按名字缓存，多个库共享一份权重。
- **prompt**：doc 模式换用 `SYSTEM_PROMPT_DOC`（「用户资料问答助手」），只允许引用给到的资料片段，仍然逐字校验、仍会拒答。
- **不串库**：应答缓存键额外拼上 collection id（内置库沿用原命名空间），断拒答日志带 `|collection:<cid>`，甲的答案不会喂给乙的资料。
- **边界**：单篇 20–50000 字、同库最多 200 篇、同名文档按 sha256 去重；空知识库提问直接回「这个知识库里还没有资料」，不硬编答案。

问答检索同时覆盖真题和考点两个库：BM25 + 向量召回 → RRF → Cross-Encoder 精排，引用校验对两类 chunk 用同一套逐字规则。考点只作为概念解释的补充，涉及具体题目答案时以真题解析为准。

后端在原有单轮链路前加了一层轻量 Agent：

1. 纯寒暄/“你能做什么”不检索，直接回固定说明；
2. 检测到「它 / 这题 / 那为什么 / 还有呢」等指代时，用 DeepSeek 结合最近 4 条消息改写成独立问题；
3. 改写失败或没有 key 时退回规则拼接，不影响答题；
4. 指代实在补不出来时返回“需要补充”，而不是猜。

| 接口 | 说明 |
|---|---|
| `POST /ask` | 单轮问答：`{"query": "...", "style": "可选，≤400 字", "collection": "可选，自建知识库 id"}`，返回 answer / citations / refused / reason / hits / latency_ms / cached / cache_age_s；带 `collection` 且库不存在回 404 |
| `POST /chat` | 多轮问答：`{"query": "...", "history": [{"role": "user", "content": "..."}], "style": "可选，≤400 字", "collection": "可选"}`，额外返回 `intent`、`rewritten_query`、`trace.steps`；拒答时 `gap_detail` 是模型自己说出的缺口（系统判定码与空话为空串） |
| `POST /chat/stream` | 同参数的流式问答，返回 `text/event-stream`：`status`（plan / cache / retrieve / gate / generate / verify）、`delta`（增量文字，已从 JSON 里剥出 answer 字段）、`reset`（校验失败，清空已显示文字）、`error`、`done`（最终 payload，字段同 `/chat`）。校验未过时 `reset` → 重试一次 → 仍不过则 `done` 里是拒答 |
| `POST /bank/generate` | 用资料出题：`{"text": "≥80 字的资料", "count": 5, "title": "资料标题"}`，返回 `{"questions": [...], "requested", "skipped", "notes"}`。每道题带 `options / answer / explanation / tags / source{title, quote, offset}`；`source.quote` 去空白后必须出现在 `text` 里，否则该题被丢弃并记进 `notes`。资料不落盘；离线演示模式返回 503 |
| `POST /mastery/turn` | 精通之路的导师会话，一轮一个请求（无状态）：`{"goal", "emoji", "subject", "materials": [{"key", "label", "kind", "detail"}], "learner": {prior/target/time/style}, "outline": {"name", "modules": [{id, name, objective, points: [{id, name, type, done}]}]}, "messages": [...], "message"}`，返回 `reply / ask / profile / outline / stage / offline / latency_ms`。`ask` 是 1–4 道带选项的提问卡（可为空），`outline` 只在模型给了合规大纲时出现（≥2 模块、≥4 知识点，最多 10 模块 / 48 知识点）。知识库与考点资料由服务端自己读，题库与对话由前端带上摘要。没有生成模型时走离线演示脚本，`offline: true` |
| `GET /collections` | 知识库列表：内置 408（`builtin: true`）+ 自建库，各带 `documents` / `chunks` 计数 |
| `POST /collections` | 新建知识库：`{"name": "≤40 字", "description": "≤200 字"}`，重名回 409 |
| `GET /collections/{cid}` | 知识库详情（含文档列表） |
| `DELETE /collections/{cid}` | 删除知识库：连文档和片段一起删，内置库拒绝（400） |
| `POST /collections/{cid}/documents` | 加资料：`{"text": "20–50000 字", "title": "可选，≤80 字"}`，返回切出的片段数；同一份资料重复添加回 409 |
| `GET /collections/{cid}/documents/{doc_id}` | 资料原文（前端用来高亮引用原句） |
| `DELETE /collections/{cid}/documents/{doc_id}` | 删除一篇资料，删完该库的检索索引随之失效重建 |
| `GET /chunks` | 题库列表：`q` / `subject` / `year` / `limit` / `offset`，只返回题干和选项 |
| `GET /chunks/{id}` | 题目详情：题干、选项、答案、解析、`source_ref`、`related_knowledge` |
| `GET /knowledge` | 考点列表：`q` / `subject` / `chapter` / `limit` / `offset`，附带科目计数和章节计数 |
| `GET /knowledge/tree` | 考点树 + 考频榜：科目 → 章节（考点数 / 涉及真题数），`heat` 按关联真题数排序，`limit` 控制榜单条数 |
| `GET /knowledge/{id}` | 考点详情：正文、公式、易错点、来源行号、关联真题 |
| `POST /feedback` | 回答反馈：`{"query": "...", "helpful": true|false, "reason": "...", "note": "...", "answer": "...", "citations": ["2019-408-01"], "cached": false}`；不占问答额度。前端评分入口已撤掉，接口保留给后续分析 |
| `GET /stats/gaps` | 缺口统计：`days` / `limit`，返回总数、按原因、按天、最近拒答 |
| `GET /stats/feedback` | 反馈统计：`days` / `limit`，返回总数、有帮助率、按差评原因、最近差评 |
| `GET /stats/overview` | 质量看板数据：进程内问答计数、缓存命中率、语料规模，外加同窗口的缺口与反馈统计 |
| `GET /meta` | 题目数、科目分布、年份、考点数、检索模式 |
| `GET /health` | 题目数、考点数、检索模式、provider |
| `GET /` | 对话优先的前端：默认问答，主导航问答 / 学习 / 学习空间 / 任务清单，质量看板在设置内，可直接在手机上打开 |

设置 `KB_ACCESS_TOKEN` 后，内容接口（`/ask`、`/chat`、`/chat/stream`、`/bank/generate`、`/mastery/turn`、`/feedback`、`/chunks`、`/knowledge`、`/collections` 及其子路径、`/meta`、`/stats/gaps`、`/stats/feedback`、`/stats/overview`）都必须带
`X-Access-Token`；`/health` 和静态页保持公开。`KB_RATE_MAX` / `KB_RATE_WINDOW` 控制问答接口的每 IP
限额（刷题库/知识库不占额度），`KB_DAILY_MAX` 是全局每日上限（0 为不限）。公网部署见 [docs/部署.md](docs/部署.md)。

考点文件默认读 `data/curated/knowledge.jsonl`，可用 `KB_KNOWLEDGE` 指向别的 JSONL。启动时会校验
`id / subject / chapter / title / body` 必填字段，并检查考点 id 不能和题目 id 冲突；同一 id 后出现的覆盖先出现的。

引用会带 `source_ref`（原文件 + 行号），拒答会写入 `data/work/gap_log.jsonl`（`KB_GAP_LOG` 可改），记录 query、拒答原因和最近命中题，作为补资料的输入。反馈有数据库时写 `answer_feedback` 表，没数据库或写库失败时退到 `data/work/feedback.jsonl`（`KB_FEEDBACK_LOG` 可改），不丢用户的操作。

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

实测混合语料上 `min_score=0.1, min_margin=0` 时负样本拒答 93.8%、正样本误拒 9.4%，同拒答率下比 `0.05/0.05` 少 3 条误拒，所以 `hybrid-ce` 默认用这组值；加入 2020、2023 卷后（141 题 + 42 考点）在同一组 32 正/16 负评测集上复测，负样本拒答率仍为 93.8%，正样本误拒从 9.4% 降到 6.2%。扩到五卷（235 题 + 42 考点）后复核：0.1/0 时闸门负样本拒答 87.5%、正样本误拒 6.2%；把 `2024 年第 1 题` 换成仍未入库的 `2022 年第 1 题` 后，端到端（检索→闸门→生成→引用校验）16 条负样本 **16/16 全部拒答**——BGP、Linux CFS 两条虽然过了分数闸门，模型仍明确说明资料里没有对应内容并拒答。0.3/0 可把闸门拒答提到 93.8%，但正样本误拒也升到 12.5%，因此默认仍为 0.1/0。`KB_MIN_SCORE` / `KB_MIN_MARGIN` 可覆盖。分差阈值（`min_margin`）会误伤「多个考点都相关」的问题，混合语料下默认关闭。

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
cp .env.example .env          # 填 DEEPSEEK_API_KEY / KB_ACCESS_TOKEN / POSTGRES_PASSWORD
mkdir -p logs && sudo chown -R 1000:1000 logs
docker compose up -d --build  # 首次 10~30 分钟，含 2.4GB 模型
curl -s http://127.0.0.1:8000/health
# {"status":"ok",...,"storage":"postgres",...}
```

`db` 用 `pgvector/pgvector:pg16` 镜像，数据落在 `pgdata` 卷；API 等 `pg_isready` 通过后才启动。首次启动会 embedding 277 个 chunk 写库（CPU 约十几秒），之后语料没变就跳过。

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
  cache.py     应答缓存键、TTL 与响应包装（纯函数）
  gate.py      拒答闸门（分数 + 分差阈值）
  prompt.py    强制 JSON 的 system/user prompt
  providers.py DeepSeek / 离线抽取占位实现
  verify.py    引用逐字校验
  pipeline.py  拒答 → 生成 → 校验 → 重试，并记录分阶段耗时
  stream.py    流式增量解析 answer 字段 + SSE 编码
  qgen.py      用资料出题：严格 JSON prompt + 逐题原文校验（对不上就丢）
  eval_gate.py 拒答阈值标定
  eval_answer.py 真实生成评测（一次引用通过率 / 拒答分布 / 延迟）
  cli.py       命令行问答
api/app.py     FastAPI 服务（/ask、/chat、/chat/stream、/bank/generate、/collections、/feedback、/chunks、/knowledge、/stats、/meta、/health、/）
api/catalog.py 题库筛选、列表/详情序列化（纯函数）
api/metrics.py 进程内问答计数（质量看板用，重启归零）
api/knowledge.py 考点卡片加载、校验、筛选、题目双向关联、章节考频树
api/guard.py   访问口令 + 按 IP 限流 + 每日额度
api/gap.py     拒答缺口日志（JSONL，仅内存模式）
api/feedback.py 反馈标准化、JSONL 落盘与统计（纯函数 + 文件 IO）
kb/
  chunking.py  用户资料切分（512 字目标 / 50 字重叠，保留原文起止偏移）
  library.py   知识库管理：建库、加资料（切分 + 向量）、删文档、按库惰性建 searcher
db/
  schema.sql   chunks / collections / documents / doc_chunks / meta / gap_logs / answer_cache / answer_feedback 表与索引
  store.py     语料同步、向量召回、SQL 筛选、拒答与反馈日志、统计聚合
  load.py      手工重建语料（python -m db.load）
  wait.py      容器启动前等 Postgres 就绪
web/index.html 对话优先的单页前端（默认问答；导航问答 / 学习 / 学习空间 / 任务清单），流式对话，多题库与导入导出，自建知识库与原文高亮，质量在设置内，无构建步骤
deploy/nginx.conf  反向代理 + 第二层限流（问答与 /bank/generate 限流）
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
