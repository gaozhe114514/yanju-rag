# -*- coding: utf-8 -*-
"""单一问答链路：先查资料，查不到依据再给标明出处的通用回答。

v0.54 起前端不再让用户选「资料 / 日常」。每个问题都先走检索与拒答闸门：
- 闸门通过：照旧生成带逐字引用的答案；
- 闸门不通过（没命中 / 分数太低 / 模型判断资料不足）：不把用户卡在拒答上，
  改用日常链路给通用回答，但正文第一句必须写明「资料里没有直接依据」，
  且不带任何引用——既不打乱引用纪律，也不假装查过资料。

硬规则不变：生成失败和引用校验失败仍直接拒答，绝不拿通用回答顶替。
"""
from answer.pipeline import AnswerResult

# 这些拒答原因属于「系统 / 生成出了故障」或「引用纪律」，不能降级成通用回答。
# stat_failed（数字和表格逐行数出的结果对不上）也是纪律问题：改给不带引用的
# 通用回答只会把错数字说得更像真的。
_STRICT_PREFIXES = ("citation_failed", "stat_failed", "provider_error",
                    "server_not_ready", "empty_collection")


def should_fallback(res: AnswerResult) -> bool:
    """这条拒答能不能改走通用回答。"""
    if not res.refused:
        return False
    reason = res.reason or ""
    return not reason.startswith(_STRICT_PREFIXES)


def fallback_detail(reason: str) -> str:
    """写进执行过程的一句话，说明为什么没走引用。"""
    reason = reason or "no_hits"
    if reason.startswith("low_score"):
        return "召回分数低于阈值，资料里没有可直接引用的依据"
    if reason.startswith("ambiguous") or reason.startswith("low_margin"):
        return "候选资料区分度不足，资料里没有可直接引用的依据"
    if reason.startswith("no_hits"):
        return "没有召回到资料，改按通用知识回答"
    return "资料里没有可直接引用的依据，改按通用知识回答"
