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

# 修复后正文字数相对原文的允许变化区间（最小必要修改）
MIN_LEN_RATIO = 0.85
MAX_LEN_RATIO = 1.15


def _strip_code_fences(text: str) -> str:
    return re.sub(r"```[\s\S]*?```", "", text or "")


def structure_signature(text: str) -> dict:
    """提取结构指标：标题数、表格行数、列表项数、代码围栏数。"""
    body = text or ""
    return {
        "headings": len(re.findall(r"^#{1,6}\s", body, flags=re.M)),
        "table_rows": len(re.findall(r"^\s*\|.*\|\s*$", body, flags=re.M)),
        "list_items": len(re.findall(r"^\s*(?:[-*+]|\d+[.)])\s+", body, flags=re.M)),
        "fences": body.count("```"),
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
    # 代码围栏数必须为偶数且不减少（保留图表/代码块）
    if sa["fences"] % 2 != 0:
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
