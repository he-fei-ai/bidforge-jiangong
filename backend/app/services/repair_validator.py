"""修复校验器（F-AGENT-CONSISTENCY-REPAIR §5.4）

对修复后的章节做二次校验：
- 冲突消除：待修复的错误取值是否已消失、权威值是否已出现
- 无新冲突（轻量）：修复后正文中不应再出现错误取值
- 结构完整：标题层级、表格、列表、代码围栏数量不显著减少
- 字数合理：修复前后字数变化在阈值内（最小修改原则）
- 全局事实一致：错误取值不得残留

校验失败的修复项标记 failed，调用方不应写库，保证"修复失败不破坏原文"。
"""
from __future__ import annotations

import re

from app.services.fence_common import fence_char_ranges, find_unclosed, iter_all_blocks

# 修复后正文字数相对原文的允许变化区间（最小必要修改）
MIN_LEN_RATIO = 0.85
MAX_LEN_RATIO = 1.15


def _strip_code_fences(text: str) -> str:
    """剥掉全部围栏块（含围栏行本身），返回「可见正文」。

    ✅ R76b · G3（BUG-F9）：旧实现 ``re.sub(r"```[\\s\\S]*?```", "", text)`` 与唯一
    事实源有三处口径分叉，方向都是**让冲突值残留在比对不到的地方**：
      · 非贪婪配对不认「块内带信息串行」—— ```` ```json ```` 会被当成收尾，
        于是真正的闭合行变成下一块的开头，**其后正文整块被误删**；
      · 完全不认 ``~~~`` 围栏（波浪号图里的数字/取值不参与比对）；
      · 未闭合围栏整块不匹配（破损正文反而"全可见"）。
    现由 ``fence_char_ranges`` 供区间、本地只做切片 —— 判据不在本模块复制。
    """
    if not text:
        return ""
    ranges = fence_char_ranges(text)
    if not ranges:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end in ranges:
        if start > cursor:
            parts.append(text[cursor:start])
        cursor = max(cursor, end)
    parts.append(text[cursor:])
    return "".join(parts)


def structure_signature(text: str) -> dict:
    """提取结构指标：标题数、表格行数、列表项数、代码围栏**块数**与未闭合数。

    ✅ R76b · G3（BUG-F8）：``fences`` 此前是 ``body.count("```")``（出现次数），
    调用方用 ``% 2 != 0`` 当「未闭合」判据 —— 这是唯一事实源之外的第四套口径，
    两个方向都错（探针实测）：
      · 块内含 ```` ```json ```` 行的**正常闭合**正文 count=3 → 误判未闭合 →
        一致性修复被判 failed、**拒绝写库**（假阳性，该修的被否）；
      · ``~~~`` 围栏**真的未闭合** count=0 → 判"已闭合" → 破损正文**放行落库**
        （假阴性，该拦的没拦）。
    现改为「块数 + 未闭合数」两个由 ``fence_common`` 供给的确定性指标。
    """
    body = text or ""
    return {
        "headings": len(re.findall(r"^#{1,6}\s", body, flags=re.M)),
        "table_rows": len(re.findall(r"^\s*\|.*\|\s*$", body, flags=re.M)),
        "list_items": len(re.findall(r"^\s*(?:[-*+]|\d+[.)])\s+", body, flags=re.M)),
        "fences": len(iter_all_blocks(body)),
        "unclosed_fences": len(find_unclosed(body)),
    }


def validate_repair(*, before: str, after: str, wrong_values: list[str],
                    authoritative_value: str) -> tuple[bool, list[str]]:
    """校验单次修复，返回 (通过, 问题列表)。"""
    problems: list[str] = []
    before = before or ""
    after = after or ""

    # ✅ 空白归一化：模型常在数字与单位间加/去空格（"90 日历天" vs "90日历天"），
    #    直接子串比对会误判，故统一去空白后再比较。
    norm = lambda s: re.sub(r"\s+", "", s or "")

    if not after.strip():
        return False, ["修复后内容为空"]
    if norm(after) == norm(before):
        # 内容未变：若错误取值仍存在则视为失败
        na = norm(after)
        if any(v and norm(v) in na for v in wrong_values):
            return False, ["内容未变化，冲突取值仍存在"]
        return True, problems  # 本就无需改动

    # 1) 字数合理
    lb, la = len(before), len(after)
    if lb:
        ratio = la / lb
        if ratio < MIN_LEN_RATIO:
            problems.append(f"修复后篇幅显著缩短（{ratio:.0%}），可能丢失内容")
        elif ratio > MAX_LEN_RATIO:
            problems.append(f"修复后篇幅显著增加（{ratio:.0%}），疑似整体重写")

    # 2) 结构完整
    sb, sa = structure_signature(before), structure_signature(after)
    # 代码围栏必须闭合、且块数不得减少（保留图表/代码块）
    # ✅ R76b · G3（BUG-F8）：判据从「``` 出现次数为奇数」改为唯一事实源的未闭合数。
    if sa["unclosed_fences"]:
        problems.append("修复后 Markdown 代码围栏未闭合")
    if sa["headings"] < sb["headings"]:
        problems.append("修复后标题数量减少，结构可能被破坏")
    if sa["table_rows"] < max(0, sb["table_rows"] - 2):
        problems.append("修复后表格行数明显减少，结构可能被破坏")
    if sa["fences"] < sb["fences"]:
        problems.append("修复后代码块/图表数量减少")

    # 3) 冲突消除：错误取值不应残留（空白归一化后比对）
    visible = norm(_strip_code_fences(after))
    auth_norm = norm(authoritative_value)
    for v in wrong_values:
        if v and norm(v) != auth_norm and norm(v) in visible:
            problems.append(f"冲突取值「{v}」仍存在于修复后正文")
    # 权威值应出现（有权威值时；空白归一化后比对）
    if auth_norm and auth_norm not in visible:
        problems.append(f"修复后未出现权威值「{authoritative_value}」")

    # 结构/围栏类问题判失败；纯篇幅问题仅在同时有残留时判失败
    hard_keywords = ("代码围栏", "标题数量", "表格行数", "代码块", "冲突取值", "权威值", "内容为空")
    hard = [p for p in problems if any(k in p for k in hard_keywords)]
    return (not hard), problems
