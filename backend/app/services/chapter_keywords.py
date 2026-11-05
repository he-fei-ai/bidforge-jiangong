"""章节标题关键词匹配（**唯一事实源**）。

✅ B-16（R175，判定源统一）：章节标题「是否存在某类内容」的判据此前散落
在 ``preflight_engine``（CMP 完整性判定）与 ``review_autofix``（补充类
finding 的目标章节定位）两处：预检用「连续子串」，定位用 ``AuditRule.keywords``
连续子串。同一个标题在预检被判「缺失」、在修复侧又被判「找不到目标章节」，
两边都不认「安全技术措施」这类法定常见写法。

本模块把「关键词 → 词族 → 语义兜底」三级判定收敛为单一定义，两侧共用。
"""

from __future__ import annotations

import re

#: 标题语义归一：去掉空白 / 标点 / 括号 / 编号前缀后再做词族匹配
_TITLE_NOISE_RE = re.compile(r"[\s\u3000·•・,，、。.:：;；()（）\[\]【】<>《》\-—_/\\|]+")

#: ✅ B-11（R175，误报治理）：法定常见标题与登记 keywords 的「词族」映射。
#:    生产/法规里「安全技术措施」「应急救援预案」是最常见的法定标题写法，
#:    但 CMP-05 的 keywords 只含「安全保证/安全措施/安全保障」、CMP-08 只含
#:    「应急处置/应急预案/应急措施/应急组织」——「安全技术措施」不含连续子串
#:    「安全措施」，「应急救援预案」也不含「应急预案」→ 天然漏配 → block 误报。
TITLE_FAMILY: dict[str, tuple[str, ...]] = {
    "CMP-05": (
        "安全保证",
        "安全保障",
        "安全措施",
        "安全技术措施",
        "安全施工措施",
        "安全防护措施",
        "安全专项措施",
        "安全生产措施",
        "安全管理措施",
        "安全保证措施",
    ),
    "CMP-08": (
        "应急处置",
        "应急预案",
        "应急救援",
        "应急救援预案",
        "应急措施",
        "应急组织",
        "应急管理",
        "应急响应",
        "应急专项方案",
    ),
}

#: 语义兜底：命中「主体词」再叠加限定词即视为该章存在（防词族仍有漏项）。
#: 形如 ``{rule_id: ((主体词...), (限定词...))}``；限定词为空表示只看主体词。
TITLE_SEMANTIC: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "CMP-05": (("安全",), ("措施", "保证", "保障")),
    "CMP-08": (("应急",), ()),
}


def norm_title(title: str) -> str:
    """标题归一：去掉空白 / 标点 / 括号 / 编号分隔符后用于词族比对。"""
    return _TITLE_NOISE_RE.sub("", title or "")


def title_matches(title: str, rule_id: str, keywords=()) -> bool:
    """章节标题是否命中该规则：原关键词 → 词族 → 语义兜底三级判定。

    ✅ B-11（R175）：旧实现只有「``kw in title`` 连续子串」一级，法定常见
    标题（安全技术措施 / 应急救援预案）实际存在却判「缺失章节」并给 block。
    """
    raw = title or ""
    if any(kw in raw for kw in keywords or ()):
        return True
    norm = norm_title(raw)
    fam = TITLE_FAMILY.get(rule_id)
    if fam and any(norm_title(f) in norm for f in fam):
        return True
    sem = TITLE_SEMANTIC.get(rule_id)
    if sem:
        heads, tails = sem
        if any(h in norm for h in heads) and (not tails or any(t in norm for t in tails)):
            return True
    return False
