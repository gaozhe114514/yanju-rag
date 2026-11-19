# -*- coding: utf-8 -*-
"""检索单元：资料切段后的最小引用单位。

一个 chunk 既参与召回（``text``），也带着回跳原文所需的位置
（``source_ref``）；引用校验只认这里逐字出现的原文。
"""
from dataclasses import asdict, dataclass, field
from typing import Dict, List


@dataclass
class Chunk:
    id: str
    seq: int
    text: str
    answer: str = ""
    analysis: str = ""
    subject: str = ""
    kp_ids: List[str] = field(default_factory=list)
    source_ref: Dict = field(default_factory=dict)
    # 新增字段一律放在末尾：测试和外部代码用 Chunk(id, seq, text) 位置参数构造
    kind: str = "doc"             # doc
    title: str = ""
    chapter: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)
