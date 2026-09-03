"""标题匹配共享工具：全项目唯一的标题归一化/比较实现。

规则（线上验证过，勿随意改动）：
- 标点折叠：全/半角逗号、冒号、句号、引号、括号、空格、连字符全部去掉后再
  小写比较——避免 "Four Hands, Two Sonatas" vs "Four Hands Two Sonatas"
  或 "四手联弹，两首奏鸣曲" vs "四手联弹两首奏鸣曲" 被判成不同。
- 匹配语义：归一化后相等或互相包含（≥4 字符防短词误匹配）

原先仅存在于 app/media/namer.py，提取为共享模块供各匹配场景复用
（namer 多轮匹配 / 未来 normalizer 或 Web 搜索）。
"""

from __future__ import annotations

import re

TITLE_STRIP_RE = re.compile(r"[\s,，:：;；!！.。·\-_''\"()（）\[\]【】<>《》]")


def norm_title(s: str) -> str:
    """标题归一化：剥全部全/半角标点 + 小写。"""
    return TITLE_STRIP_RE.sub("", (s or "").lower())


def title_match(query: str, candidate_titles: list[str]) -> bool:
    """标题匹配：归一化后相等或包含（≥4 字符防短词误匹配）。"""
    q = norm_title(query)
    if not q:
        return False
    for t in candidate_titles:
        c = norm_title(t)
        if not c:
            continue
        if q == c or (len(q) >= 4 and q in c) or (len(c) >= 4 and c in q):
            return True
    return False
