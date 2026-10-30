# 精通之路 · DeepTutor 流程对照

这份文档是把 `E:\DeepTutor` 里精通之路（Mastery Path）的每一步读完之后写下的
依据，用来对齐我们自己的实现。左边是 DeepTutor 的做法（带文件位置），右边是我们
当前的做法和差距。**改代码前先查这里**，不要凭印象重做。

读过的源码：

| 层 | 文件 |
|---|---|
| 后端能力 | `deeptutor/capabilities/mastery/{capability,loop,pipeline,mode,tools,binding,choices,history,memory}.py` |
| 提示词 | `deeptutor/capabilities/mastery/prompts/{zh,en}/mastery_loop.yaml` |
| 前端页面 | `web/app/(workspace)/learning/mastery/page.tsx`、`.../[pathId]/page.tsx` |
| 前端组件 | `web/components/space/learning/MasteryStudy.tsx` 等 23 个（见 §7） |
| 路由与文案 | `web/lib/mastery-mode.ts`、`web/lib/learning-routes.ts` |

---

## 1. 一句话结构

精通之路 = **三条模式的同一条会话**（定大纲 / 学 / 复习）。

- 模式是会话状态，不是页面。`mastery_session_mode` 存在会话偏好里，下一轮接着用
  （`binding.py` `remember_mode_on_session`）。
- 一轮只解决一件事；**出题就结束这一轮**，学习者作答是**下一条消息**
  （`loop.py` `final_text_override` 的注释把这件事讲得很清楚：以前用
  `pause_for_user` 把回合挂起等回答，会话被一直占着；现在出题即收尾）。
- 谁决定“掌握”：**引擎**，不是模型。模型只能出题、讲解、给评价；
  `mastered` 由确定性代码按证据算（`capability.py` 的设计公理）。
- 一条路同一时刻只允许一个回合在跑（按路的租约，不按会话）。

## 2. 三条模式与工具闸门

`mode.py`：

```
MODES = ("outline", "study", "review")
TOOL_MODES = {
  "mastery_build": {"outline"},
  "mastery_revise": {"outline"},
  "mastery_quiz": {"study", "review"},
  "mastery_grade": {"study", "review"},
  "mastery_assess": {"study"},
  ...
}
```

- `normalize_mode(None) -> "study"`（默认学习），但 `enforced_mode` 保留
  “这轮根本没记模式”的事实，否则老的 CLI/网页会话会被当成学习会话而被收走工具。
- **study 需要已有大纲**；没有大纲时直接进 study 会被拒绝，提示先定大纲。
- **review 永远不以“有到期项”为门槛**：没到期也可以开一轮复习，导师自己挑。
- 越权调用返回 `wrong_mode_message`，说清楚“现在是什么模式、这个工具属于哪个模式”。

**改我们自己的时候：模式必须显式可见、可切换，不能藏在按钮背后。**

## 3. 新建一条路

1. `mastery/page.tsx`：右上「New topic」→ 弹层。
2. `CreateTopicWizard.tsx` 两步：**目标 → 资料**。资料里额外塞一条
   `{kind:"goal", label, excerpt: goal}`，让导师知道目标本身就是一份材料。
3. 提交 `{goal, emoji, sources, modules: []}` —— **大纲是空的**，不在弹层里排。
4. 建完 `setPendingPrompt(masteryOpeningMessage("outline"), MASTERY_OPENING_SCOPE)` 再
   `router.push(masterySessionRoute(path_id, "outline"))`。也就是说：
   **那个「开始定大纲」按钮本身就是这一轮的请求**，不是进去再自动补发一句话。
   - `masteryOpeningMessage("outline")` = “Use the materials I chose and design the
     outline for this goal with me.”
   - 进会话后，`MasteryStudy` 读到 pending prompt，一次性交给 composer；
     `MASTERY_OPENING_SCOPE` 保证只消费一次。

我们的差距：我们是在进入详情页后 `setTimeout(120ms)` 自动 `masterySend`，且做了
“同一条路只发一次”。效果接近，但**按钮不是请求，刷新就不发**，且和“用户自己点进来”
分不开。改成按钮即请求更干净。

## 4. 大纲模式（mode = outline）

### 4.1 开场先读状态，不调用模型

`loop.py` `pre_loop`：每轮开始先由运行时调一次 `mastery_status`（无 LLM），把
当前状态作为 `[Current-turn mastery_status snapshot]` 块塞进提示词；模型第一轮
就该直接用这份快照，不许为了“开始”再调一次。

### 4.2 档案（intake）

`mastery_loop.yaml`：导师先问清 4 件事：

1. 现在已经会什么
2. “学完了”是什么样
3. 能投入多少时间
4. 希望怎么讲

问法要**针对这份资料和目标**，不是背四条模板；用 `ask_user` 一次问 1–4 个，
选项必须彼此有区分度、允许自由作答。`mastery_profile` **只合并学习者真的说过的
字段**，没说的保持原值，不替他编。写档案要有存储回执；没有回执就不能声称“已记住”。

### 4.3 建大纲

- 空大纲 → `mastery_build`；已有大纲 → `mastery_revise`（**最小改动**：
  只改被提到的模块，其余原样保留，连同掌握证据）。
- 改大纲两步：`action=preview` 返回 `preview_id`，`action=commit` 才落库。
  排序用 `module_order`。**id 复用**，所以已掌握的题目不会因为改名而丢。
- 模块带 `objective`：写“学完能做什么”（能力），不是话题名词。
- 知识点四类：`memory`（要记住的事实）/ `procedure`（步骤技能）/
  `concept`（要理解的概念）/ `design`（开放判断）。
- 大纲围绕**学习者的目标**组织，不照搬资料目录；**每一份已选资料都要有交代**；
  读资料用 `read_source`。
- 资料没覆盖的地方必须直说（`CoverageNotice` 会在界面上列出没进大纲的文件）。

### 4.4 讲完话就出题（这条最容易漏）

`mastery_loop.yaml` 与 `loop.py` 的守卫：

- 回复如果以冒号结尾，这一轮**必须**调 `mastery_quiz`。
- 不许“预告一个问题”而不出题。`loop.py` 里专门检测：悬空冒号、
  “让我们看看你知不知道…”这类承诺句、“A. B. C. D.” 纯文本选项、
  “请选择”开场白、以及“卡片已给到”的假声明，都会被打回重做。
- 已经有一道题挂着时，不许再用散文复述它；要重问用 `pending_action='resume'`。
- 一次只设计一道题；已经出过的题不重复用。

## 5. 学习模式（mode = study）

### 5.1 一轮的形状

```
读状态（status 快照）
  → 决定当前目标（status.next）
  → 讲一小段（用资料，不用编）
  → 出题（mastery_quiz）：这一轮到此结束
== 学习者作答是下一条消息 ==
  → 引擎先判分（grade 在回合开始前就做完了）
  → 回合带着 [Mastery] 判分事实开始
  → 导师讲这次作答说明什么、补差距
  → 出下一道题，或宣布越过门槛并推进到下一个目标
```

关键：**判分发生在回合开始前**（`pre_loop_seed` / `_grade_seed`）。模型拿到的是
既成事实：“学习者选了 C，引擎判 incorrect，反馈是…；不要重复判分，不要重述答案键”。

### 5.2 两类门槛

| 类型 | 门槛 | 工具 |
|---|---|---|
| memory / procedure | 定量：正确率 + 证据量 + 难度 + 最近一致性；门槛默认 80% | `mastery_quiz` + `mastery_grade` |
| concept / design | 定性：学习者用自己的话讲清楚，导师记录 | `mastery_assess` |

**练习题打不开概念 / 判断类型的门槛**（`ObjectiveDetail.tsx` 的 GateBar 会明说）。
定量门槛即使当前全对也可能还没到（证据量、难度、最近一致性都算），界面要解释
“为什么 100% 还没掌握”，否则学习者会觉得系统坏了。

### 5.3 退路都不是“给掌握”

- `mastery_skip_question`：跳过当前题。不判分、不给掌握分，目标门槛原地不动。
- `mastery_defer_objective`：先把这个目标放一放。
- `mastery_repair_question`：题面/答案键有毛病时修题。
- `mastery_assess`：只有这一条能开定性门槛。

### 5.4 跳过与判分后的种子

- 学习者跳过后，运行时已经把它撤掉了。提示词会明说：“不要再调 skip，不要重出这道题，
  没判分也没给分”。
- 判分后提示词会带上：结果、学习者原话、反馈、是否 final、是否 mastered。
  非最终判分（需要澄清）不计罚，保留原题，等学习者补充后再判。

### 5.5 掌握瞬间

`MasteryStudy` 用 `mastery_status` 的前后 diff 发现“某个知识点刚刚变成 mastered”，
播放 `LevelUpCelebration`（三台彩纸炮，canvas，`prefers-reduced-motion` 直接跳过），
同时大纲树那一行有一次高亮（`mastery-just-mastered-*`）。

我们不做彩纸（设计规范里“不撒花”写在 v1.8），改成：行右侧出现一次「掌握」徽标 +
进度环走一格 + toast。**动效可以不同，状态变化必须有。**

## 6. 复习模式（mode = review）

- 入口：详情页的 `ReviewTrail`（记忆曲线排的复习计划）和列表页顶部的
  “有 N 条到期”横幅。
- 打开复习会话时，开场消息直接把到期知识点名列出来：
  `masteryOpeningMessage("review", {dueTitles})`。
- 复习不是重新学：直接在到期点上出题，答对就顺延下次复习，答错降回学习。
- 回访目标（`desired_retention`，0.7–0.99）影响复习间隔，界面可调。

我们的现状：v0.35 补了复习模式与「到期」（1/3/7/21 天，答错回学习）；
v0.40 把列表页横幅补上。仍没有 `desired_retention` 可调，到期后也没有单独的
`ReviewTrail` 计划视图。

## 7. 前端屏幕与状态（交互层）

### 7.1 列表（`mastery/page.tsx` + `TopicAtlas`）

- 抬头 + 右侧「New topic」；有到期时顶部一条横幅：
  “N 条复习到期，分布在 M 条路”，主按钮直接进**第一条到期路的复习会话**。
  （v0.40 已做：`.mp-due`，按到期数排序取第一条，并自动发出「开始复习：…」。）
- 卡片 `TopicMapCard`：题名、进度环 `ProgressRing`（mastered/total，
  完成才用主色，过程中用 foreground 墨色，避免和“可点”混淆）。

### 7.2 详情（`[pathId]/page.tsx`）

- 顶部 `PathTitle`（就地改名，空名回落）。下面是「Next up」吸顶条：
  下一步是什么（probe/teach/practice/assess/review/answer_pending/complete → 文案见
  `next-step-copy.ts`），右侧按钮直接进会话。
- 没有大纲（`counts.total === 0`）时：`needsRouteRepair`，显示虚线卡
  「还没有大纲」+ 主按钮「和导师定大纲」+ 次按钮「自己写模块」。
- 工具栏可在「大纲 / 看板」间切换；看板是只读投影（模块成列、知识点成卡），
  点卡片开同一个证据抽屉、进同一个会话。
  （v0.41 已做证据弹层 `.mp-ev`：点大纲里已掌握的行打开；只有作答记录、
  复习计划与退路记录，没有独立 `ObjectiveDetail` 路由。）
- `EditTopicRouteDialog`：手改模块「地图工坊」。**改名保留证据；删知识点连它的
  掌握、复习计划、人工标记一起删**——保存前必须警告。
- 右栏：`LearnerProfileCard`（“关于你”：已经会什么 / 想达到什么 / 时间 / 讲法；
  想改就在会话里说，不在这里编辑——否则会有第二个写入口）。
  （v0.40 放在详情页头目标下面一行 `.mp-profile`，只渲染真说过的字段，没有编辑入口。）
- `SessionCamp`：这条路的所有会话，可续可新开（“换一个角度重来”）。

### 7.3 会话（`MasteryStudy.tsx`）

- 头部：徽标 + 题名 + `ModeSwitch`（三个模式都显示，当前项高亮；回合进行中禁用）
  + `ProgressRing`。
- 模式下的说明条（`MODE_PRESENTATION`）：每个模式有自己的 note 和空态文案。
- 空态：每个模式 3 张起步卡（如“先看看我会多少”“从第一个知识点开始”）。
- 左栏大纲 `StudyOutline`（252px，可折叠，折叠状态存 `dt.mastery.outline`）：
  模块序号 + 名称，知识点头用 6px 圆点表达三态——实心 = 已掌握，描边环 = 当前，
  空心 = 之后。**填充 → 环 → 空心，不靠颜色单独表达。**
- 题目卡：选项带正文；作答后显示对/错、正确答案、解释；不显示选项字母之外的元信息
  （乱序后不提字母）。
- composer：占位符会用本轮结束后拉到的「建议问一句」（`fetchMasteryAskHint`），
  Tab 补全。回合结束时 composer 重新可输入。
- 自动滚动只做单向钉住：用户往上翻就不打断。

### 7.4 组件清单

`MasteryStudy`（会话）、`ModeSwitch`、`StudyOutline`（会话左栏）、`ModuleOutline`
（详情页大纲）、`ObjectiveDetail`（证据抽屉：门槛条、复习计划、作答历史、错误诊断）、
`LearningBoard`、`TopicAtlas`、`TopicMapCard`、`ProgressRing`、`SessionCamp`、
`ReviewTrail`、`LearnerProfileCard`、`PathTitle`、`CoverageNotice`、
`RouteDraftEditor`、`EditTopicRouteDialog`、`LevelUpCelebration`、`CreateTopicWizard`、
`TopicWizardSteps`、`ConfirmDialog`、`next-step-copy`、`format`、`route-draft`。

## 8. 不变量（抄也要抄全）

1. 一道题只有一份答案键；判分只认引擎。
2. 出题即结束回合；作答永远是下一条消息。
3. 同一时刻只有一道题挂着；重问用 resume，不重出。
4. 选择题：每个选项都要有**正文**；题干里不许复述选项；乱序后不许提字母。
5. 每题必须有解释与难度；干扰项要对应真实的误解，长度和风格与被选答案一致。
6. 概念 / 判断类型只由 `assess`（学习者自己的话）开门槛。
7. 资料没覆盖就直说，不编考点；大纲要给每份资料一个位置。
8. 档案只写学习者说过的；没有存储回执不许声称记住。
9. 模式不匹配的工具调用要被拒绝，并解释“为什么现在不能用”。
10. 点名要求：不要用“让我们看看…：”这类预告结束一轮。

## 9. 我们现在差在哪（2026-10-30）

v0.34 时我们只有“定大纲”半条链路。v0.35 已按 §10 的映射把「学」补上：

| 能力 | DeepTutor | 我们（v0.35） |
|---|---|---|
| 模式 | outline / study / review，可切换、持久化 | ✅ `path.mode` + `.ms-modes` 显式开关；无大纲不能进学习，复习不要求有到期项 |
| 学 | 会话里讲 + 出题 + 判分 + 推进 | ✅ 同一会话：讲一段 → 出题（这一轮结束）→ 作答是下一条消息 |
| 判分 | 引擎，回合开始前完成 | ✅ 后端在调模型之前用纯函数判完，结果作为事实写进提示词 |
| 掌握 | 引擎按证据算，两类门槛；GateBar 明说差什么 | ✅ 选择题看最近窗口；概念 / 判断只认 `assess`；v0.39 起 `grade.gate` 逐条说明离门槛还差什么 |
| 证据 | `ObjectiveDetail` 抽屉：门槛条、复习计划、作答历史、错误诊断 | ✅ v0.41 弹层 `.mp-ev`：作答历史、掌握与下次复习、退路记录；没有门槛条与错误诊断 |
| 复习 | 记忆曲线到期 + 复习会话 + 列表页到期横幅 | ✅ 1 / 3 / 7 / 21 天；v0.40 列表顶部「N 个知识点该复习了」直接进到期最多的那条路（简版，无 retention 可调） |
| 出题约束 | 提示词 + 守卫双重约束 | ✅ v0.38：提示词硬规则 + `clean_quiz` 清洗 + 回合后守卫（悬空冒号 / 承诺出题 / 散文选项 / 假卡片声明）；违规重试一次，仍不过就如实收尾，不硬凑题 |
| 档案 | 只并真说过的，带回执 | ✅ v0.40：后端 `clean_profile` 只收真说过的四项；详情页头显示「关于你」，档案变化 toast 回执「档案记下 N 项」；没有独立存储版本 |
| 大纲修订 | preview → commit，最小改动 | ⚠️ 每轮整份覆盖，靠名字保 id；没有 preview |
| 会话 | 多会话、可续 | ⚠️ 单会话 |
| 失败与退路 | defer / skip / repair 三个工具 | ✅ v0.38：题卡上三个按钮（跳过 / 修题 / 暂缓），大纲里点已暂缓的行恢复；都不记作答、不给掌握分 |

**结论（v0.35 已执行）：把“学”这条主链路补上（讲 → 出题 → 判分 → 门槛 → 推进），
再把模式做成显式开关。** 剩下的差距集中在“对话框上的守卫”和“退路工具”，
它们需要模型侧工具调用，不是纯前端能补的；下一步优先做冒号规则和跳过/修题的等价路径。

**v0.38 已把这两项补上**：守卫放在 `_parse_session`（`answer/tutor.py` 的 `_guard_say`），
退路做成请求里的 `control`（skip / repair / defer / resume），前端是题卡上的三个按钮。
我们没有工具调用，所以“意图”由按钮明确给出，后端把它翻译成提示词里的既成事实。

**v0.39 补上 GateBar 的说明面**：`gate_status(point)` 与判分结果一起返回
`grade.gate.need`，前端在判定条下面画一行「离掌握还差：…」。定量门槛会分别指出
作答次数不够、最近一次答错、最近窗口正确率不足、还没答对难度 ≥2 的题；
概念 / 判断类则写「用自己的话讲清一次；选择题打不开这类门槛」。
这样“全对但还没掌握”不再像系统故障——缺哪条证据，界面就写哪条。

## 10. 落到我们自己的实现（无状态约束下的映射）

我们没有数据库、没有租约、没有多会话存储：`path` 全在 localStorage，
`POST /mastery/turn` 每轮带整份快照。所以：

- **模式**：`path.mode`（outline / study / review），切换时写回并作为下一轮
  `mode` 字段。学习模式要求已有大纲；复习模式不要求有到期项。
- **挂着的题**：`path.pending = {questionId, kpId, kind, prompt, options, key,
  explanation, difficulty, at}`，随下一轮请求整份带回（无状态，服务端不存题）；
  前端只在作答后显示正确项，作答前不提前泄露 `key`。
- **作答**：前端把 `answer: {questionId, choice|text}` 随下一轮请求发给后端；
  后端先判分、算出 `mastered`（引擎规则在 `answer/tutor.py` 里，纯函数），
  再拼提示词让模型解释。**模型不参与“对不对”的判断。**
- **引擎规则**（`answer/tutor.py`，纯函数）：
  - 定量门槛（memory / procedure）：取最近 4 次选择题记录，正确率 ≥75%、
    最近一次答对、且答对的题里至少一道难度 ≥2（只对回忆题不算掌握）→ mastered。
  - 定性门槛（concept / design）：只看 `assess`，判定通过一次才算；选择题打不开。
  - 答错会撤销引擎给的 `done`，但人工标过的不撤；作答记录最多留 40 条。
- **复习**：掌握时间 → 间隔 1 / 3 / 7 / 21 天；到期项进复习模式，答错回到学习。
- **多会话**：暂不做成多个独立会话，先保证一条路一条会话、切模式不断线。

---

## 附：DeepTutor 关键文件位置速查

- 模式与工具闸门：`deeptutor/capabilities/mastery/mode.py`
- 回合钩子、守卫、出题结束回合：`deeptutor/capabilities/mastery/loop.py`
  （`pre_loop` 657、`pre_loop_seed` 696、`finish_instruction` 489、
  `final_text_override` 591、`settlement_recovery` 450）
- 提示词编队顺序：`deeptutor/capabilities/mastery/pipeline.py`
  （mastery_tutor → session_mode → runtime_context → runtime_policy → loop → playbook）
- 工具契约：`deeptutor/capabilities/mastery/tools.py`（3816 行）
- 选择题数据契约：`deeptutor/capabilities/mastery/choices.py`
- 历史投影（省 token）：`history.py`；辅导记忆：`memory.py`
- 提示词：`prompts/zh/mastery_loop.yaml`
- 会话界面：`web/components/space/learning/MasteryStudy.tsx`
- 模式与开场：`web/lib/mastery-mode.ts`
