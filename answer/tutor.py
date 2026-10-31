# -*- coding: utf-8 -*-
"""精通之路的导师会话：第一阶段「把大纲定下来」。

它和问答链路是两件事：问答必须逐字引用、资料没覆盖就拒答；大纲是设计，
不产生引用，所以这里不做引用校验。但有一条同样硬的要求——材料里没有的
东西不能写成大纲里的知识点，所以模型只拿得到我们给的资料摘要，返回后
还要按结构再清洗一遍（模块数、知识点数、类型、重复项）。

协议是一轮一段 JSON：
  {"say": "...", "ask": {...}|null, "profile": {...}|null,
   "outline": {...}|null, "stage": "intake|draft|approved"}
前端据此决定显示导师的话、一张结构化问答卡，还是把大纲写进路线。
"""
import hashlib
import json
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .providers import ProviderError, parse_payload


class GuardError(ProviderError):
    """模型答应了出题却没出：话以冒号结尾、把选项写成散文、或声称题卡已给。"""


MAX_MODULES = 10
MAX_POINTS = 48
MAX_POINTS_PER_MODULE = 8
MAX_QUESTIONS = 4
MAX_OPTIONS = 6

KP_TYPES = ("memory", "procedure", "concept", "design")
TYPE_LABELS = {
    "memory": "记忆",
    "procedure": "步骤",
    "concept": "概念",
    "design": "判断",
}
PROFILE_KEYS = ("prior", "target", "time", "style")
PROFILE_LABELS = {
    "prior": "现在已经会什么",
    "target": "学完算什么样",
    "time": "能投入多少时间",
    "style": "希望怎么讲",
}
STAGES = ("intake", "draft", "approved")

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)

# 回合后守卫：出题即结束这一轮，说了要出题就必须真的把题放进来
_GUARD_END_COLON = re.compile(r"[:：]\s*$")
_GUARD_PROMISE = re.compile(
    r"(题目如下|下面这道题|接下来这道|来一道[^。！？\n]{0,12}题|考考你|"
    r"看看你(?:知不知道|会不会|能不能|掌握没)|请选择|选一个|做一道题)")
_GUARD_PROSE_OPTION = re.compile(r"(?:^|\n)\s*([A-DＡ-Ｄ])[\s.、:：)）]\s*\S")
_GUARD_FAKE_CARD = re.compile(
    r"(?:题目|题卡|题目卡|卡片)[^。！？\n]{0,6}(?:已|已经)(?:给|放|挂|出现|生成)")
CONTROL_KINDS = ("skip", "repair", "defer", "resume")


TUTOR_SYSTEM = """你是「言据 · 精通之路」里的一对一导师。这一轮你在和一个学习者一起定他的精通大纲，不是上课，也不出题。

你要输出的每一句话都会原样显示给学习者。不要复述工具结果，不要写内部推理，不要自言自语「让我先」「等等」。说不清楚的宁可少说。

规则：
1. 先看「学习档案」。四件事——现在已经会什么、学完算什么样、能投入多少时间、希望怎么讲——知道两项以上就可以起草大纲；一项都不知道时，用 ask 问，问法要针对这份资料和这个目标，不要每次背同样四条。一次问 1 到 4 个，选项要有区分度，允许自由作答。学习者可以只答一部分，也可以跳过；答多少就按多少设计，不要为了凑齐而追问。
2. 已经有「当前大纲」时不要整张换掉。学习者说改哪里就只改那一块，其余模块原样保留（连同 done 状态）。他要求改标题、顺序、增删模块都可以，但不要顺手重写没提到的模块。
3. 大纲围绕学习者写的目标组织，不是照搬资料目录。不要出现「本书第 X 章」「材料一」「本章」这类按资料分模块的写法；多份资料要按主题重新组织到一起；每一份已选资料都要在某个模块里有交代，不能因为看起来次要就丢掉。
4. 每个模块给一句 objective：学完这个模块，学习者能做什么。写成能力，不是话题（「能独立用高斯消元解线性方程组并判断解的结构」，不是「线性方程组」）。
5. 模块 4 到 8 个，每个模块 2 到 6 个知识点，总数不超过 40。知识点要具体到能练的粒度，并且符合这门科目的考试大纲；拿不准的考点不要编。
6. 每个知识点标 type：memory（要记住的事实）/ procedure（步骤技能）/ concept（要理解的概念）/ design（开放判断）。
7. 不要编出处、页码、题号，也不要说「资料第几页」。材料里没有的内容不要假装它有；资料确实不够时，在 say 里直说缺什么。
8. say 是给学习者看的话：这次做了什么、为什么这样排、请他改哪里。定稿后明说「大纲已定稿」，并直接问一句「现在就按第一个模块开始学？」——学习者回「开始学习」时，系统会在同一轮切到学习模式让他开讲，所以不要只说「去找按钮」，也可以接着说改哪里。他认可后 stage 用 approved。第一次起草出来时 stage 用 draft；还在问问题时 stage 用 intake。
9. 如果学习者这一轮只是在问概念、闲聊或补充信息，没有要求改动，就把 outline 置为 null，正常回答，不要每轮都重写大纲。
10. 中文，简洁，可以用短列表；语气像真人导师，不谄媚。

只输出一个 JSON 对象，不要代码围栏，不要前后多余文字。结构：
{
  "say": "给学习者看的话",
  "ask": null 或 {"intro": "一句引导", "questions": [
    {"id": "prior", "header": "基础", "prompt": "问题原文",
     "multi_select": false, "allow_free_text": true, "placeholder": "也可以自己写",
     "options": [{"label": "短选项", "description": "选它意味着什么"}]}]},
  "profile": null 或 {"prior": "...", "target": "...", "time": "...", "style": "..."},
  "outline": null 或 {"name": "路线名（不超过 30 字）", "modules": [
    {"name": "模块名", "objective": "学完能做什么",
     "knowledge_points": [{"name": "知识点", "type": "concept"}]}]},
  "stage": "intake" 或 "draft" 或 "approved"
}
profile 只写学习者这一轮明确说过的内容，没提到就省略或置 null，不要替他编。"""


def _clip(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _clean_say(raw, limit: int = 4000) -> str:
    text = _THINK_RE.sub("", str(raw or "")).strip()
    return text[:limit]


def _clean_options(raw) -> List[Dict]:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:MAX_OPTIONS]:
        if isinstance(item, str):
            label, desc = item.strip()[:60], ""
        elif isinstance(item, dict):
            label = _clip(item.get("label"), 60)
            desc = _clip(item.get("description"), 160)
        else:
            continue
        if not label:
            continue
        if label.lower() in ("other", "其他", "其它"):
            continue  # 卡片自己会给「其他」入口
        out.append({"label": label, "description": desc})
    return out


def clean_ask(raw) -> Optional[Dict]:
    """把模型给的问答卡清洗成前端能画的结构；不合法就当作没问。"""
    if not isinstance(raw, dict):
        return None
    questions = raw.get("questions")
    if not isinstance(questions, list):
        return None
    out = []
    seen = set()
    for i, q in enumerate(questions[:MAX_QUESTIONS]):
        if not isinstance(q, dict):
            continue
        prompt = _clip(q.get("prompt") or q.get("question"), 400)
        if not prompt or prompt in seen:
            continue
        seen.add(prompt)
        options = _clean_options(q.get("options"))
        if len(options) < 2 and not q.get("allow_free_text", True):
            continue
        out.append({
            "id": _clip(q.get("id"), 20) or ("q%d" % (i + 1)),
            "header": _clip(q.get("header"), 12),
            "prompt": prompt,
            "multi_select": bool(q.get("multi_select")),
            "allow_free_text": bool(q.get("allow_free_text", True)),
            "placeholder": _clip(q.get("placeholder"), 80),
            "options": options,
        })
    if not out:
        return None
    return {"intro": _clip(raw.get("intro"), 300), "questions": out}


def clean_profile(raw) -> Dict[str, str]:
    """只收四个已知字段，各截 200 字；没说的留空，不替学习者编。"""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in PROFILE_KEYS:
        value = _clip(raw.get(key), 200)
        if value:
            out[key] = value
    return out


def _previous_points(previous) -> Dict:
    """按「模块名 → 知识点名 → 旧节点」建索引，改大纲时保住进度。"""
    index = {}
    for module in (previous or {}).get("modules", []) or []:
        mname = _clip(module.get("name"), 60)
        points = {}
        for point in module.get("points", []) or []:
            pname = _clip(point.get("name"), 80)
            if pname:
                points[pname] = point
        if mname:
            index[mname] = {"id": _clip(module.get("id"), 40), "points": points}
    return index


def clean_outline(raw, previous=None) -> Optional[Dict]:
    """清洗大纲；模块或知识点太少说明模型没设计出来，当作失败。"""
    if not isinstance(raw, dict):
        return None
    modules_raw = raw.get("modules")
    if not isinstance(modules_raw, list):
        return None
    prev = _previous_points(previous)
    modules = []
    seen_modules = set()
    total = 0

    for mi, module in enumerate(modules_raw):
        if not isinstance(module, dict):
            continue
        name = _clip(module.get("name"), 60)
        if not name or name in seen_modules:
            continue
        seen_modules.add(name)
        old = prev.get(name) or {}
        module_id = old.get("id") or ("m%d" % (mi + 1))
        points_raw = module.get("knowledge_points")
        if not isinstance(points_raw, list):
            points_raw = module.get("points") or []
        points = []
        seen_points = set()
        for pi, point in enumerate(points_raw[:MAX_POINTS_PER_MODULE]):
            if not isinstance(point, dict):
                continue
            pname = _clip(point.get("name") or point.get("title"), 80)
            if not pname or pname in seen_points:
                continue
            seen_points.add(pname)
            if total >= MAX_POINTS:
                break
            old_point = (old.get("points") or {}).get(pname) or {}
            ptype = point.get("type")
            if ptype not in KP_TYPES:
                ptype = "concept"
            points.append({
                "id": _clip(old_point.get("id"), 60) or ("%s-p%d" % (module_id, pi + 1)),
                "name": pname,
                "type": ptype,
                "done": bool(old_point.get("done")),
                "attempts": list(old_point.get("attempts") or []),
                "masteredAt": int(old_point.get("masteredAt") or 0),
            })
            total += 1
        if not points:
            continue
        modules.append({
            "id": module_id,
            "name": name,
            "objective": _clip(module.get("objective"), 200),
            "points": points,
        })
        if len(modules) >= MAX_MODULES:
            break

    if len(modules) < 2 or total < 4:
        return None
    return {"name": _clip(raw.get("name"), 30), "modules": modules}


def _learner_lines(learner) -> str:
    lines = []
    for key in PROFILE_KEYS:
        value = _clip((learner or {}).get(key), 200)
        if value:
            lines.append("- %s：%s" % (PROFILE_LABELS[key], value))
    return "\n".join(lines) if lines else "（还没有回答过，先问再设计）"


def _outline_block(outline) -> str:
    if not outline or not (outline.get("modules") or []):
        return ""
    slim = {
        "name": outline.get("name") or "",
        "modules": [
            {
                "name": m.get("name"),
                "objective": m.get("objective") or "",
                "points": [
                    {"name": p.get("name"), "type": p.get("type"),
                     "done": bool(p.get("done"))}
                    for p in (m.get("points") or [])
                ],
            }
            for m in outline.get("modules") or []
        ],
    }
    return json.dumps(slim, ensure_ascii=False)


def _history_block(messages, limit: int = 8, each: int = 900) -> str:
    rows = []
    for turn in (messages or [])[-limit:]:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = _clip(turn.get("content"), each)
        if role not in ("user", "assistant") or not content:
            continue
        rows.append(("学习者" if role == "user" else "导师") + "：" + content)
    return "\n\n".join(rows)


def build_turn_messages(payload: dict, digest: Sequence[dict]) -> List[dict]:
    """拼这一轮发给模型的消息：系统提示 + 一段结构化上下文。"""
    parts = []
    parts.append("【精通目标】\n" + (_clip(payload.get("goal"), 2000) or "（还没写）"))
    subject = _clip(payload.get("subject"), 20)
    if subject:
        parts.append("【科目】" + subject)

    lines = []
    for item in digest or []:
        label = _clip(item.get("label"), 100)
        text = _clip(item.get("text"), 1200)
        if label and text:
            lines.append("● %s\n%s" % (label, text))
        elif label:
            lines.append("● %s（没有可读内容）" % label)
    parts.append("【已选资料】\n" + ("\n\n".join(lines) if lines
                                 else "（学习者没有勾选资料，按目标和科目考试大纲设计）"))

    parts.append("【学习档案】\n" + _learner_lines(payload.get("learner")))

    outline = _outline_block(payload.get("outline"))
    if outline:
        parts.append("【当前大纲】\n" + outline)

    history = _history_block(payload.get("messages"))
    if history:
        parts.append("【最近对话】\n" + history)

    parts.append("【学习者这一轮说】\n" + (_clip(payload.get("message"), 2000) or "（刚建好这个目标，还没说话）"))
    return [
        {"role": "system", "content": TUTOR_SYSTEM},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def _parse_result(text: str, previous) -> dict:
    obj = parse_payload(text)
    say = _clean_say(obj.get("say"))
    ask = clean_ask(obj.get("ask"))
    profile = clean_profile(obj.get("profile"))
    outline = clean_outline(obj.get("outline"), previous)
    if not say and not ask and not outline:
        raise ProviderError("模型既没有给话，也没有给问题或大纲")
    stage = obj.get("stage") if obj.get("stage") in STAGES else ""
    if outline:
        stage = "approved" if stage == "approved" else "draft"
    elif ask:
        stage = "intake"
    elif not stage:
        stage = "draft"
    return {"reply": say, "ask": ask, "profile": profile,
            "outline": outline, "stage": stage, "offline": False}


def _run_outline_turn(provider, payload: dict, digest: Sequence[dict]) -> dict:
    """定大纲模式下的一轮（v0.34 的行为原样保留）。"""
    if provider is None or not hasattr(provider, "complete"):
        return offline_turn(payload, digest)

    messages = build_turn_messages(payload, digest)
    last_error = None
    last_text = ""
    for attempt in range(2):
        try:
            last_text = provider.complete(messages, temperature=0.3,
                                          max_tokens=2600, json_mode=True)
            return _parse_result(last_text, payload.get("outline"))
        except ProviderError as exc:
            last_error = exc
            if attempt == 0:
                # 第二次把要求再说一遍，模型多半能自己纠正
                messages = messages + [
                    {"role": "assistant", "content": _clip(last_text, 400)},
                    {"role": "user", "content": "上一次的输出不能直接使用（%s）。请只输出一个 JSON 对象，"
                                                "包含 say / ask / profile / outline / stage 五个键，"
                                                "不要代码围栏和多余文字。" % exc},
                ]
    raise ProviderError(str(last_error) if last_error else "导师没有给出可用结果")


def offline_turn(payload: dict, digest: Sequence[dict]) -> dict:
    """没有生成模型时：先把四件事问完，再按勾的资料拼一份大纲，并说明是拼的。"""
    learner = payload.get("learner") or {}
    known = [k for k in PROFILE_KEYS if _clip(learner.get(k), 200)]
    offline_note = ("现在是离线演示模式，没有接生成模型，所以这份大纲是按你勾的资料按章节拼出来的，"
                    "不是导师设计的。接上模型后同一段对话会让导师重新排一版。")

    if len(known) < 2:
        return {
            "reply": offline_note + " 先问四件事，答完我再拼一版：",
            "ask": {
                "intro": "这些答案会用来决定大纲的起点、篇幅和讲法。",
                "questions": [
                    {"id": "prior", "header": "基础", "prompt": "这些内容你现在会到什么程度？",
                     "multi_select": False, "allow_free_text": True,
                     "placeholder": "也可以自己写，例如：学过一遍，图论最弱",
                     "options": [
                         {"label": "零基础", "description": "概念都没系统接触过"},
                         {"label": "学过一遍", "description": "上课听过，做题不稳"},
                         {"label": "只差个别章节", "description": "大部分会，个别点补一下"},
                     ]},
                    {"id": "target", "header": "目标", "prompt": "学完什么程度算过关？",
                     "multi_select": False, "allow_free_text": True,
                     "placeholder": "例如：真题里这科的题能稳拿分",
                     "options": [
                         {"label": "会做基础题", "description": "覆盖常考选择填空"},
                         {"label": "能独立做综合题", "description": "大题能自己推"},
                         {"label": "熟练掌握", "description": "能给别人讲清楚"},
                     ]},
                    {"id": "time", "header": "时间", "prompt": "能投入多少时间？",
                     "multi_select": False, "allow_free_text": True,
                     "placeholder": "例如：每天 2 小时，四周",
                     "options": [
                         {"label": "两周以内", "description": "时间紧，只挑重点"},
                         {"label": "一个月左右", "description": "常规节奏"},
                         {"label": "时间充裕", "description": "能铺开细讲"},
                     ]},
                    {"id": "style", "header": "讲法", "prompt": "希望我怎么讲？",
                     "multi_select": False, "allow_free_text": True,
                     "placeholder": "例如：先讲例子再讲定义",
                     "options": [
                         {"label": "先讲概念再练", "description": "先听懂，再做题"},
                         {"label": "直接做题带讲", "description": "边做边补概念"},
                     ]},
                ],
            },
            "profile": {}, "outline": None, "stage": "intake", "offline": True,
        }

    outline = offline_outline(payload, digest)
    if outline is None:
        return {
            "reply": offline_note + " 但资料里没有能拼成知识点的内容，先勾几张考点卡片或补一段资料。",
            "ask": None, "profile": {}, "outline": None, "stage": "intake",
            "offline": True,
        }
    return {
        "reply": offline_note + " 下面是按资料拼的草稿，你可以在这里继续提要求。",
        "ask": None, "profile": {}, "outline": outline, "stage": "draft",
        "offline": True,
    }


def offline_outline(payload: dict, digest: Sequence[dict]) -> Optional[Dict]:
    """按资料里的条目拼大纲：每份资料一个模块，条目就是知识点。"""
    modules = []
    for item in digest or []:
        titles = [str(t).strip() for t in (item.get("titles") or []) if str(t).strip()]
        if not titles:
            continue
        label = _clip(item.get("label"), 60) or "资料"
        points = []
        for i, title in enumerate(titles[:MAX_POINTS_PER_MODULE]):
            points.append({"name": title[:80], "type": "concept"})
        if len(points) < 2:
            continue
        modules.append({
            "name": label,
            "objective": "能把这些内容讲清楚，并在题目里用得上。",
            "knowledge_points": points,
        })
        if len(modules) >= MAX_MODULES:
            break
    if len(modules) < 2:
        return None
    return clean_outline({"name": _clip(payload.get("goal"), 30), "modules": modules})


# ---------------------------------------------------------------------------
# v0.35：三条模式（outline / study / review）
#
# 照 DeepTutor 的分工：模型负责讲什么、出什么题；「有没有掌握」由这里的
# 纯函数按作答证据算，模型说了不算。一轮最多挂一道题，出题就结束这一轮，
# 学习者作答是下一条消息——所以 /mastery/turn 收到的 answer 是上一轮那道题
# 的答案，判分在这里做完，再把既成事实交给模型解释。
# ---------------------------------------------------------------------------

MODES = ("outline", "study", "review")
QUAL_TYPES = ("concept", "design")
GATE_WINDOW = 4
GATE_SCORE = 0.75
GATE_DIFF = 2


def normalize_mode(raw, outline=None) -> str:
    """没传模式时：有大纲就是学习，没大纲就是定大纲（和 DeepTutor 一致）。"""
    mode = str(raw or "").strip().lower()
    if mode in MODES:
        return mode
    return "study" if _all_points(outline) else "outline"


# 学习者在定大纲时说「开始学习」= 想上课了。照 DeepTutor 的做法，这句话
# 本身就切换模式，并且在同一轮把课开起来，而不是让他再去点按钮。
_START_PAT = re.compile(
    r"^(好[的]?[，,]?\s*)?(我们|那就|现在)?\s*(开始|进入)(学习|上课|学)?"
    r"(吧|啊|呀|了)?[。！!～~]*$")
_CONTINUE_PAT = re.compile(
    r"^(好[的]?[，,]?\s*)?(继续|接着)(学习|学)?(吧|啊|呀)?[。！!～~]*$")


def wants_study(raw) -> bool:
    """保守判断：只有很短的一句「开始学吧」才算，别把正经讨论当指令。"""
    text = str(raw or "").strip()
    if not text or len(text) > 20:
        return False
    return bool(_START_PAT.match(text) or _CONTINUE_PAT.match(text))


def _last_user_text(messages) -> str:
    for turn in reversed(list(messages or [])):
        if isinstance(turn, dict) and turn.get("role") == "user":
            return str(turn.get("content") or "")
    return ""


def _all_points(outline) -> List[Dict]:
    rows = []
    for module in (outline or {}).get("modules") or []:
        for point in module.get("points") or []:
            rows.append({"module": module, "point": point})
    return rows


def _find_point(outline, kp_id) -> Optional[Dict]:
    target = str(kp_id or "")
    for row in _all_points(outline):
        if str(row["point"].get("id") or "") == target:
            return row["point"]
    return None


def _quiz_attempts(point) -> List[Dict]:
    return [a for a in (point.get("attempts") or [])
            if isinstance(a, dict) and a.get("kind") != "assess"]


def engine_mastered(point) -> bool:
    """掌握只由证据算：概念/判断要有一次通过的自述，其余看最近几次作答。"""
    attempts = [a for a in (point.get("attempts") or []) if isinstance(a, dict)]
    if point.get("type") in QUAL_TYPES:
        return any(a.get("kind") == "assess" and a.get("ok") for a in attempts)
    quiz = _quiz_attempts(point)[-GATE_WINDOW:]
    if len(quiz) < 2 or not quiz[-1].get("ok"):
        return False
    score = sum(1 for a in quiz if a.get("ok")) / float(len(quiz))
    # 门槛要见过一道有难度的题：只对回忆题不算掌握
    diff = max([int(a.get("diff") or 1) for a in quiz if a.get("ok")] or [0])
    return score >= GATE_SCORE and diff >= GATE_DIFF


def _point_done(point) -> bool:
    return bool(point.get("done")) or engine_mastered(point)


def gate_status(point) -> Dict:
    """把「为什么还没掌握」讲清楚：证据量、最近一次、正确率、难度。

    界面直接显示这几句，避免「我全答对了怎么还没过」的误会。
    """
    mastered = _point_done(point)
    if point.get("type") in QUAL_TYPES:
        return {"mastered": mastered, "kind": "assess",
                "need": [] if mastered else ["用自己的话讲清一次；选择题打不开这类门槛"]}
    quiz = _quiz_attempts(point)[-GATE_WINDOW:]
    need = []
    if not mastered:
        if len(quiz) < 2:
            need.append("至少要有 2 次选择题作答（现在 %d 次）" % len(quiz))
        if quiz and not quiz[-1].get("ok"):
            need.append("最近一次答错，下一次答对再看")
        if quiz:
            score = sum(1 for a in quiz if a.get("ok")) / float(len(quiz))
            if score < GATE_SCORE:
                need.append("最近 %d 次正确率 %d%%（要 ≥75%%）" % (
                    len(quiz), int(round(score * 100))))
            diff = max([int(a.get("diff") or 1) for a in quiz if a.get("ok")] or [0])
            if diff < GATE_DIFF:
                need.append("还没答对过难度 ≥2 的题")
        if not need:
            need.append("证据还不够，再答一道看看")
    return {"mastered": mastered, "kind": "quiz", "need": need,
            "window": len(quiz),
            "correct": sum(1 for a in quiz if a.get("ok")),
            "difficulty": max([int(a.get("diff") or 1) for a in quiz if a.get("ok")] or [0])}


def current_point(outline) -> Optional[Dict]:
    """下一步该学的知识点：按模块顺序，第一个没掌握、也没被暂缓的。"""
    for row in _all_points(outline):
        point = row["point"]
        if _point_done(point) or int(point.get("deferredAt") or 0):
            continue
        return row
    return None


def _norm_label(raw) -> str:
    text = str(raw or "").strip().upper()
    match = re.match(r"^([A-Z0-9])", text)
    return match.group(1) if match else text


def grade_choice(pending, answer) -> bool:
    key = _norm_label((pending or {}).get("key"))
    choice = _norm_label((answer or {}).get("choice"))
    return bool(key) and key == choice


def _status_lines(outline) -> str:
    lines = []
    for module in (outline or {}).get("modules") or []:
        rows = []
        for point in module.get("points") or []:
            quiz = _quiz_attempts(point)
            right = sum(1 for a in quiz if a.get("ok"))
            state = "已掌握" if _point_done(point) else (
                "已暂缓" if int(point.get("deferredAt") or 0) else
                "作答 %d 次，对 %d 次" % (len(quiz), right) if quiz else "未开始")
            rows.append("%s（%s，%s）" % (point.get("name"), point.get("type") or "concept", state))
        lines.append("- %s：%s" % (module.get("name"), "；".join(rows)))
    return "\n".join(lines)


def _guard_say(say: str, quiz, assess) -> str:
    """这一轮没有题时，检查话里是不是答应了出题。返回问题描述，没问题返回空串。"""
    if quiz or assess or not say:
        return ""
    text = say.strip()
    problems = []
    if _GUARD_END_COLON.search(text):
        problems.append("话以冒号结尾，却没有出题")
    elif _GUARD_PROMISE.search(text):
        problems.append("话里说要出题，却把题空着")
    labels = {m.group(1) for m in _GUARD_PROSE_OPTION.finditer(text)}
    if len(labels) >= 2:
        problems.append("把选项写进了正文，没有放进 quiz")
    if _GUARD_FAKE_CARD.search(text):
        problems.append("声称题目已经给到，实际没有题")
    return "；".join(problems)


def _strip_dangling(say: str) -> str:
    return re.sub(r"[\s:：]+$", "", (say or "").strip())


def _apply_control(payload: dict, outline: Dict, pending: Optional[Dict]) -> Optional[Dict]:
    """退路不是「给掌握」：跳过和修题不记作答，暂缓只把知识点挪开。

    返回这一轮的控制事实（写进提示词）；没有控制或控制无效时返回 None。
    """
    control = payload.get("control")
    if not isinstance(control, dict):
        return None
    kind = str(control.get("kind") or "").strip().lower()
    if kind not in CONTROL_KINDS:
        return None
    if kind in ("skip", "repair") and not pending:
        return None
    now = int(payload.get("now") or 0)
    note = _clip(control.get("note"), 400).strip()
    kp_id = str((pending or {}).get("kpId") or control.get("kpId") or "")
    point = _find_point(outline, kp_id)
    if point is None and kind == "defer":
        row = current_point(outline)
        point = (row or {}).get("point")
    if point is None:
        return None
    if kind == "skip":
        point["skips"] = int(point.get("skips") or 0) + 1
        point["lastSkippedAt"] = now
    elif kind == "repair":
        point["repairs"] = int(point.get("repairs") or 0) + 1
        point["lastRepairedAt"] = now
    elif kind == "defer":
        point["deferredAt"] = now or 1
        point["deferredReason"] = note[:200]
    else:
        point["deferredAt"] = 0
        point["deferredReason"] = ""
    return {"kind": kind, "kpId": point.get("id") or "",
            "name": point.get("name") or "", "note": note,
            "deferredAt": int(point.get("deferredAt") or 0)}


def _control_block(control: Optional[Dict]) -> str:
    if not control:
        return ""
    kind = control.get("kind")
    name = control.get("name") or "这个知识点"
    if kind == "skip":
        return ("【学习者跳过了这道题】\n没有判分，也没有给掌握分，门槛原地不动。"
                "不要重出同一道题，不要公布答案键；换一道更小的题确认，"
                "或先换个角度把这一点讲清楚再问。")
    if kind == "repair":
        note = control.get("note") or "题干或选项有问题"
        return ("【学习者说这道题有问题】\n他说：%s\n不要重出原题，也不要怪学习者；"
                "先用一句话说明这道题想考什么，再出一道修正后的题。" % note)
    if kind == "defer":
        return ("【学习者要求先放一放】\n「%s」这一轮先不学，也不要给它判分或宣布掌握。"
                "改讲下一个还没掌握、也没暂缓的知识点；如果都不剩了，就如实说。"
                % name)
    return ("【学习者要把「%s」放回来】\n这一轮从它开始讲，不要跳过去；"
            "它还没有掌握，正常讲、正常出题。" % name)


STUDY_SYSTEM = """你是「言据 · 精通之路」里的一对一导师。现在在「学习」模式：陪着学习者按一份已经排好的大纲走，一次讲一个知识点，讲完出题检查。

你输出的 say 会原样显示给学习者。不要复述内部状态，不要写内部推理，不要自言自语。

这一轮只做一件事：
- 学习者刚答完上一道题（下面有「引擎判分」）→ 先讲这次作答说明了什么，补差距，再出一道新题；引擎已经判为掌握时，讲一句这次为什么过关，然后切到下一个知识点。
- 没有判分 → 从「这一轮该讲」那个知识点开始讲一小段，然后出题。**学习者说「开始学习」时也一样：直接讲第一小段，不要只回一句「好的，我们开始」就停下**；这一轮要让他真的看到课已经开始。
- 出题（quiz 或 assess 不是 null）就代表这一轮结束：say 只写引出这道题的话，题本身只放在 quiz / assess 里，不要再用散文写一遍。
- 学习者只是在问问题或闲聊 → 正常回答，quiz 和 assess 都置 null。

出题规则（硬性）：
1. 不要用散文写选择题。题干只放情境和问法，选项只能出现在 options 里，题干里不许复述选项内容。
2. 每个选项都要有完整正文（body），不能只给「A」「对」这种空壳。
3. 三个或四个选项；干扰项必须对应学习者真实可能犯的误解，长度和风格与正确项相当；乱序后学习者只看得到正文，所以不要提「上面/下面」「选项字母」。
4. 每题都要 explanation：为什么对，干扰项各错在哪。
5. difficulty 用 1–5：1 是回忆事实，3 是要理解或推一步，5 是综合应用。
6. memory / procedure 类知识点用 quiz；concept / design 类知识点用 assess——让学习者用自己的话说清楚，quiz 打不开这两类的门槛。
7. 资料里没有的内容不要编题；确实缺资料就直说，把 quiz 置 null。
8. 「掌握」由引擎判定，不要说「你已经掌握了」除非下面的引擎结果说 mastered 为真。

如果这一轮的话要以冒号结尾，这一轮就必须出一题。

只输出一个 JSON 对象，不要代码围栏：
{
  "say": "给学习者看的话",
  "quiz": null 或 {"kpId": "知识点 id", "prompt": "题干", "options": [
      {"label": "A", "body": "选项正文"}, {"label": "B", "body": "选项正文"},
      {"label": "C", "body": "选项正文"}], "key": "B",
    "explanation": "为什么 B 对、A/C 各错在哪", "difficulty": 2},
  "assess": null 或 {"kpId": "知识点 id", "prompt": "请用自己的话解释……",
    "key_points": ["必须讲到的第一点", "必须讲到的第二点"]}
}
同一轮最多出 quiz 或 assess 其中一个，另一个必须是 null。"""

REVIEW_SYSTEM = """你是「言据 · 精通之路」里的一对一导师。现在在「复习」模式：只在到期要回忆的知识点上出题，不重新讲整章。

规则和「学习」模式一样，另外：
1. 只从【到期要复习】里挑一个知识点出题，不要跑题。
2. 答对就一句话确认，再出下一个到期点的题；答错就讲清楚错在哪，再用一道更小的题确认。
3. 「掌握」由引擎判定；复习答错不会直接取消掌握，但下一次复习会变早。
4. 没有到期项时，正常回答学习者的问题，quiz / assess 置 null。

输出格式与「学习」模式相同：{"say": "...", "quiz": {...}|null, "assess": {...}|null}。"""

ASSESS_JUDGE_SYSTEM = """你在判定学习者对某个知识点的自述是否讲清楚了。

标准：学习者用自己的话讲到 key_points 里的要点，没有明显概念错误，就判通过。只是复述题目、空泛表态、或漏掉一半要点，判不通过。

只输出一个 JSON 对象：{"pass": true 或 false, "why": "一句话说明理由"}。"""


def clean_quiz(raw, outline) -> Optional[Dict]:
    """把模型给的题清洗成能画的卡；不合法就不出这道题。"""
    if not isinstance(raw, dict):
        return None
    prompt = _clip(raw.get("prompt") or raw.get("question"), 1200).strip()
    if not prompt:
        return None
    options = []
    for i, item in enumerate(raw.get("options") or []):
        if isinstance(item, str):
            body = item.strip()[:400]
            label = chr(65 + i)
        elif isinstance(item, dict):
            body = _clip(item.get("body") or item.get("description") or item.get("text"), 400).strip()
            label = _normalize_option_label(item.get("label"), i)
        else:
            continue
        if not body:
            continue
        options.append({"label": label, "body": body})
    if len(options) < 2:
        return None
    labels = [o["label"] for o in options]
    key = _norm_label(raw.get("key") or raw.get("answer"))
    if key not in labels:
        # 模型偶尔把正确项的正文当答案给，按正文找回字母
        body_key = str(raw.get("key") or raw.get("answer") or "").strip()
        for option in options:
            if body_key and body_key in option["body"]:
                key = option["label"]
                break
    if key not in labels:
        return None
    rows = _all_points(outline)
    kp_id = str(raw.get("kpId") or "").strip()
    if not _find_point(outline, kp_id):
        target = current_point(outline)
        kp_id = str((target or {}).get("point", {}).get("id") or "")
    if not kp_id:
        return None
    difficulty = raw.get("difficulty")
    try:
        difficulty = int(difficulty)
    except (TypeError, ValueError):
        difficulty = 2
    difficulty = max(1, min(5, difficulty))
    question_id = "q-" + hashlib.sha1(
        (kp_id + "\n" + prompt).encode("utf-8")).hexdigest()[:10]
    return {
        "questionId": question_id,
        "kpId": kp_id,
        "prompt": _strip_option_echo(prompt, options),
        "options": options,
        "key": key,
        "explanation": _clip(raw.get("explanation") or raw.get("rationale"), 1200),
        "difficulty": difficulty,
        "kind": "quiz",
    }


def _normalize_option_label(raw, index: int) -> str:
    text = str(raw or "").strip().upper()
    match = re.match(r"^([A-Z0-9])", text)
    return match.group(1) if match else chr(65 + index)


def _strip_option_echo(prompt: str, options: List[Dict]) -> str:
    """题干里如果也把选项抄了一遍，从最早出现的那一项开始截断。"""
    starts = []
    for option in options:
        body = str(option.get("body") or "").strip()
        if len(body) < 2:
            continue
        pattern = r"(?:^|[\n\s])%s(?:\s*[.、):：）]\s*|\s+)" % re.escape(
            str(option.get("label") or "")) + re.escape(body)
        match = re.search(pattern, prompt, re.I)
        if match:
            starts.append(match.start())
    if len(starts) < 2:
        return prompt
    head = prompt[:min(starts)].rstrip(" -—*·")
    return head or prompt


def clean_assess(raw, outline) -> Optional[Dict]:
    if not isinstance(raw, dict):
        return None
    prompt = _clip(raw.get("prompt") or raw.get("question"), 600).strip()
    if not prompt:
        return None
    kp_id = str(raw.get("kpId") or "").strip()
    if not _find_point(outline, kp_id):
        target = current_point(outline)
        kp_id = str((target or {}).get("point", {}).get("id") or "")
    if not kp_id:
        return None
    points = [_clip(p, 200) for p in (raw.get("key_points") or []) if _clip(p, 200)]
    return {"kpId": kp_id, "prompt": prompt, "key_points": points[:6],
            "kind": "assess", "options": [], "key": "",
            "questionId": "a-" + hashlib.sha1(
                (kp_id + "\n" + prompt).encode("utf-8")).hexdigest()[:10],
            "difficulty": 3}


def _session_context(payload: dict, digest: Sequence[dict], mode: str,
                     grade: Optional[Dict], control: Optional[Dict] = None) -> str:
    parts = []
    parts.append("【精通目标】\n" + (_clip(payload.get("goal"), 2000) or "（还没写）"))
    subject = _clip(payload.get("subject"), 20)
    if subject:
        parts.append("【科目】" + subject)

    lines = []
    for item in digest or []:
        label = _clip(item.get("label"), 100)
        text = _clip(item.get("text"), 700)
        if label and text:
            lines.append("● %s\n%s" % (label, text))
    parts.append("【已选资料】\n" + ("\n\n".join(lines) if lines
                                 else "（学习者没有勾选资料）"))
    profile = _learner_lines(payload.get("learner"))
    if profile != "（还没有回答过，先问再设计）":
        parts.append("【学习档案】\n" + profile)

    outline = payload.get("outline") or {}
    parts.append("【当前模式】" + ("学习" if mode == "study" else "复习"))
    parts.append("【大纲与进度】\n" + (_status_lines(outline) or "（还没有大纲）"))

    if mode == "review":
        due = [str(x) for x in (payload.get("review") or []) if str(x or "").strip()]
        parts.append("【到期要复习】\n" + ("、".join(due) if due else "（没有到期项）"))
    else:
        target = current_point(outline)
        if control and control.get("kind") == "resume":
            target = next((row for row in _all_points(outline)
                           if str(row["point"].get("id") or "") == control.get("kpId")), target)
        if target:
            point = target["point"]
            module = target["module"]
            parts.append("【这一轮该讲】\n%s（%s）\n模块目标：%s" % (
                point.get("name"), point.get("type") or "concept",
                module.get("objective") or "（没写）"))
        else:
            parts.append("【这一轮该讲】\n（没有可讲的知识点了：剩下的都已掌握或已暂缓）")

    pending = payload.get("pending")
    if isinstance(pending, dict) and pending.get("prompt"):
        parts.append("【挂着的题】\n%s\n选项：%s" % (
            _clip(pending.get("prompt"), 600),
            "／".join("%s. %s" % (o.get("label"), o.get("body"))
                     for o in pending.get("options") or [])))

    block = _control_block(control)
    if block:
        parts.append(block)

    if grade:
        rows = ["学习者作答：%s" % (grade.get("answerText") or grade.get("choice") or "（空）"),
                "引擎判：%s" % ("正确" if grade.get("ok") else "错误"),
                "正确答案：%s" % grade.get("correctLabel", ""),
                "引擎判定这个知识点：%s" % ("已掌握" if grade.get("mastered") else "还没掌握")]
        if grade.get("explanation"):
            rows.append("这一轮约定好的解释：%s" % grade.get("explanation"))
        parts.append("【引擎判分（已定事实）】\n" + "\n".join(rows)
                     + "\n不要重新判分，不要重复公布答案键；先讲这次作答说明了什么。")

    history = _history_block(payload.get("messages"), limit=8, each=700)
    if history:
        parts.append("【最近对话】\n" + history)
    parts.append("【学习者这一轮说】\n" + (_clip(payload.get("message"), 2000) or "（继续）"))
    return "\n\n".join(parts)


def _parse_session(raw: str, outline, mode: str) -> Dict:
    obj = parse_payload(raw)
    say = _clean_say(obj.get("say"))
    quiz = clean_quiz(obj.get("quiz"), outline)
    assess = clean_assess(obj.get("assess"), outline)
    if quiz and assess:
        assess = None
    if not say and not quiz and not assess:
        raise ProviderError("模型既没有说话也没有出题")
    problem = _guard_say(say, quiz, assess)
    if problem:
        raise GuardError(problem)
    stage = "review" if mode == "review" else "study"
    return {"reply": say, "quiz": quiz, "assess": assess, "stage": stage,
            "ask": None, "profile": {}, "outline": None, "offline": False}


def _judge_assess(provider, pending: Dict, answer_text: str) -> Optional[bool]:
    if provider is None or not hasattr(provider, "complete"):
        return None
    payload = json.dumps({
        "question": pending.get("prompt"),
        "key_points": pending.get("key_points") or [],
        "learner_answer": answer_text,
    }, ensure_ascii=False)
    try:
        raw = provider.complete(
            [{"role": "system", "content": ASSESS_JUDGE_SYSTEM},
             {"role": "user", "content": payload}],
            temperature=0.1, max_tokens=400, json_mode=True)
        obj = parse_payload(raw)
    except ProviderError:
        return None
    return bool(obj.get("pass"))


def _apply_grade(payload: dict, outline: Dict, pending: Dict, answer: Dict,
                 provider) -> Tuple[Dict, Dict, Optional[Dict]]:
    """判分 + 记一次作答 + 重算掌握，返回 (outline, grade, 已掌握的 point)。"""
    kp_id = str(pending.get("kpId") or "")
    point = _find_point(outline, kp_id)
    if point is None:
        return outline, {}, None
    text = _clip(answer.get("text"), 2000).strip()
    choice = str(answer.get("choice") or "").strip()
    kind = str(pending.get("kind") or "").strip()
    if kind not in ("quiz", "assess"):
        kind = "assess" if pending.get("key") in (None, "") else "quiz"
    if kind == "quiz":
        ok = grade_choice(pending, answer)
        judged = None
    else:
        judged = _judge_assess(provider, pending, text)
        ok = bool(judged)
    attempts = list(point.get("attempts") or [])
    attempts.append({
        "ok": ok, "diff": int(pending.get("difficulty") or 1),
        "at": int(payload.get("now") or 0), "kind": kind,
    })
    point["attempts"] = attempts[-40:]
    mastered = engine_mastered(point)
    if mastered:
        point["done"] = True
        if not point.get("masteredAt"):
            point["masteredAt"] = int(payload.get("now") or 0)
    elif not point.get("manual"):
        # 人工标过的保持标着；引擎给的不再成立就撤掉
        point["done"] = False
    correct = ""
    if pending.get("key"):
        for option in pending.get("options") or []:
            if option.get("label") == pending.get("key"):
                correct = "%s. %s" % (option.get("label"), option.get("body"))
    grade = {
        "questionId": pending.get("questionId") or "",
        "kpId": kp_id, "ok": ok, "kind": kind,
        "choice": choice, "answerText": text,
        "correctLabel": correct, "explanation": pending.get("explanation") or "",
        "mastered": bool(mastered), "judged": judged,
        "gate": gate_status(point),
    }
    if kind == "assess" and judged is None:
        grade["explanation"] = "这次没能判定（模型没接上），先当作没通过。"
    return outline, grade, point if mastered else None


def run_session_turn(provider, payload: dict, digest: Sequence[dict], mode: str) -> Dict:
    """学习 / 复习模式的一轮：先判分（如果有作答），再让模型讲和出下一题。"""
    outline = payload.get("outline") or {}
    pending = payload.get("pending") if isinstance(payload.get("pending"), dict) else None
    answer = payload.get("answer") if isinstance(payload.get("answer"), dict) else None

    control = _apply_control(payload, outline, pending)
    if control:
        # 跳过 / 修题 / 暂缓都不判分；这一轮不把旧题再摆给模型看
        pending = None
        answer = None

    grade = None
    if pending and answer and str(answer.get("questionId") or "") == str(pending.get("questionId") or ""):
        outline, grade, mastered_point = _apply_grade(payload, outline, pending, answer, provider)
    else:
        mastered_point = None

    if provider is None or not hasattr(provider, "complete"):
        return _offline_session(payload, outline, grade, mastered_point, mode, control)

    ctx_payload = dict(payload)
    if control:
        ctx_payload["pending"] = None
    messages = [
        {"role": "system", "content": STUDY_SYSTEM if mode == "study" else REVIEW_SYSTEM},
        {"role": "user", "content": _session_context(ctx_payload, digest, mode, grade, control)},
    ]
    last_error = None
    raw = ""
    for attempt in range(2):
        try:
            raw = provider.complete(messages, temperature=0.4, max_tokens=2200,
                                    json_mode=True)
            result = _parse_session(raw, outline, mode)
            result["grade"] = grade
            result["outline"] = outline
            result["mode"] = mode
            if control:
                result["control"] = control
            if mastered_point is not None:
                result["mastered"] = {"id": mastered_point.get("id"),
                                      "name": mastered_point.get("name")}
            return result
        except ProviderError as exc:
            last_error = exc
            if attempt == 0:
                if isinstance(exc, GuardError):
                    hint = ("上一次的输出没通过出题规则（%s）。如果话里答应了出题，就必须在 "
                            "quiz 或 assess 里给出完整题目，选项只放在 options 里；"
                            "如果这一轮不出题，就不要承诺出题，也不要用冒号收尾。" % exc)
                else:
                    hint = ("上一次的输出不能直接用（%s）。只输出一个 JSON 对象，"
                            "键是 say / quiz / assess。" % exc)
                messages = messages + [
                    {"role": "assistant", "content": _clip(raw, 300)},
                    {"role": "user", "content": hint},
                ]
    if isinstance(last_error, GuardError):
        # 两次都没把题补完整：不硬凑题，也不把半句话当消息发出去
        try:
            obj = parse_payload(raw)
        except ProviderError:
            obj = {}
        say = _strip_dangling(_clean_say(obj.get("say")))
        note = "（这一轮没能把题目补完整，先不硬凑；你说「再来一道」，我就接着出。）"
        return {"reply": (say + "\n\n" + note) if say else note,
                "quiz": None, "assess": None, "grade": grade, "outline": outline,
                "mode": mode, "stage": mode, "ask": None, "profile": {},
                "offline": False, "error": False, "guarded": True,
                **({"control": control} if control else {})}
    # 模型没给出可用结果时，判分仍然有效，只是这一轮不再出题
    return {"reply": "（导师这次没能接着说下去：%s）" % last_error,
            "quiz": None, "assess": None, "grade": grade, "outline": outline,
            "mode": mode, "stage": mode, "ask": None, "profile": {},
            "offline": False, "error": True}


def _offline_session(payload, outline, grade, mastered_point, mode,
                     control=None) -> Dict:
    """离线演示：能判分，但不出新题——出题必须有模型。"""
    if control:
        done = {"skip": "这道题先跳过，没判分也没给掌握分。",
                "repair": "这道题标成有问题，原题不再追问。",
                "defer": "「%s」先暂缓，大纲里的进度不动。" % (control.get("name") or "这个知识点"),
                }[control.get("kind")]
        say = done + "\n\n（离线演示：没有生成模型，暂时不能接着出下一道题。）"
    elif grade:
        verdict = "答对了。" if grade.get("ok") else "这道答错了。"
        extra = grade.get("correctLabel") and ("正确答案：" + grade["correctLabel"] + "。") or ""
        note = "（离线演示：判分是本地算的，但没有模型就不能接着出下一道题。）"
        say = verdict + extra + (grade.get("explanation") or "") + "\n\n" + note
    else:
        say = ("现在是离线演示模式，没有接生成模型。大纲和判分都能用，"
               "但讲课和出题需要模型——接上模型后这里会接着讲。")
    result = {"reply": say, "quiz": None, "assess": None, "grade": grade,
              "outline": outline, "mode": mode, "stage": mode, "ask": None,
              "profile": {}, "offline": True}
    if control:
        result["control"] = control
    return result


def run_turn(provider, payload: dict, digest: Sequence[dict]) -> dict:
    """按模式分发：没大纲先定大纲，有大纲就跟着大纲学或复习。"""
    mode = normalize_mode(payload.get("mode"), payload.get("outline"))
    switched = ""
    # 已定稿的大纲 + 一句「开始学吧」：当作模式请求处理，在这一轮直接开讲。
    hear = payload.get("message") or _last_user_text(payload.get("messages"))
    if mode == "outline" and _all_points(payload.get("outline")) and wants_study(hear):
        mode = "study"
        switched = "study"
    if mode == "outline":
        result = _run_outline_turn(provider, payload, digest)
    else:
        result = run_session_turn(provider, payload, digest, mode)
    result["mode"] = mode
    if switched:
        result["switched"] = switched
    return result
