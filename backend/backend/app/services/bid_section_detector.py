"""标段检测（多标段投标识别）—— 对齐 OpenBidKit client/electron/utils/bidSectionDetector.cjs

纯规则探测器：仅用于快速判断招标文件是否疑似多标段，不生成最终标段列表。
移植自 OpenBidKit（2026-09-14 版），覆盖：
- 显式声明总数（"本项目划分为 3 个标段 / 包 / 分包 / 标包 / 子项目"）
- 16 种「标段 / 标包 / 分包 / 包」定义模式（中文数字 + 阿拉伯数字 + 第X / X前 变体）
- 文号之外的 【N】 / 【N-M】 括号编号章节
中文数字归一化支持 1–99。

设计取舍（与 OpenBidKit 一致）：只做"疑似判断"，不做最终标段切分——真正的
标段拆分由用户（导入目录后手动分章）或后续流程决定，避免误切正文结构。

⚠️ 还原说明（2026-09-15）：本文件源码曾被误删，仅存 __pycache__ 下的 .pyc。
现依据字节码（常量表 + 反汇编）逐函数重建，并用「编译后逐函数 dis 比对」
验证与原始实现完全等价（见 _diagnostics/_bsd_verify.py）。
"""
from __future__ import annotations

import re
from typing import Optional

# 中文数字 → 阿拉伯数字（小写 1–10 + 大写壹–伍）
CHINESE_SMALL_MAP = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5,
}

# 含"零"占位，使 index() 结果与数值对齐（"一"→1 … "十"→10；"零"→0 视为无效）
_CHINESE_DIGITS = ("零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十")

# ✅ 增强（2026-09-25）：大写数字组合位（贰拾/拾贰 等金额式写法，正式招标文件常见）。
# CHINESE_SMALL_MAP 只有单字壹–伍，组合位需要完整的 壹–玖 取值表。
_CHINESE_UPPER_DIGITS = {
    "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5,
    "陆": 6, "柒": 7, "捌": 8, "玖": 9,
}
# 「十」的小写/大写写法在组合位等价（二十 / 贰拾）
_TEN_CHARS = ("十", "拾")

# 16 种定义模式：中文数字 / 阿拉伯数字 × 「N单位」「第N单位」「单位N」
# 单位顺序为 标段 → 标包 → 分包 → 包（"包"最短，放最后避免抢先命中"标包/分包"）
_SECTION_DEF_PATTERNS = [
    (re.compile("([一二三四五六七八九十壹贰叁肆伍]+)标段[：:；;]"), "标段"),
    (re.compile("(\\d+)标段[：:；;]"), "标段"),
    (re.compile("第([一二三四五六七八九十壹贰叁肆伍\\d]+)标段[：:；;]"), "标段"),
    (re.compile("标段([一二三四五六七八九十壹贰叁肆伍\\d]+)[：:；;]"), "标段"),
    (re.compile("([一二三四五六七八九十壹贰叁肆伍]+)标包[：:；;]"), "标包"),
    (re.compile("(\\d+)标包[：:；;]"), "标包"),
    (re.compile("第([一二三四五六七八九十壹贰叁肆伍\\d]+)标包[：:；;]"), "标包"),
    (re.compile("标包([一二三四五六七八九十壹贰叁肆伍\\d]+)[：:；;]"), "标包"),
    (re.compile("([一二三四五六七八九十壹贰叁肆伍]+)分包[：:；;]"), "分包"),
    (re.compile("(\\d+)分包[：:；;]"), "分包"),
    (re.compile("第([一二三四五六七八九十壹贰叁肆伍\\d]+)分包[：:；;]"), "分包"),
    (re.compile("分包([一二三四五六七八九十壹贰叁肆伍\\d]+)[：:；;]"), "分包"),
    (re.compile("([一二三四五六七八九十壹贰叁肆伍]+)包[：:；;]"), "包"),
    (re.compile("(\\d+)包[：:；;]"), "包"),
    (re.compile("第([一二三四五六七八九十壹贰叁肆伍\\d]+)包[：:；;]"), "包"),
    (re.compile("包([一二三四五六七八九十壹贰叁肆伍\\d]+)[：:；;]"), "包"),
]

# 显式总数声明："本项目划分为 3 个标段" / "共分为五个标包" / "共2个分包"
# ✅ 修复（2026-09-25）：旧正则的所有动词前缀与"个?"均为可选，导致裸写的
#   定义式「三标段：」也会被误判为"显式声明共 3 个标段"（_TOTAL_SECTION_PATTERN
#   对"三标段"的 number=三 + 标段 直接命中，number 落在 [2,99] 即被当作总数）。
#   实际后果：在仅有「一/二/三标段：」定义列表的文档里，最后一条"三标段"会被
#   当成"声明总数=3"，使 detect_bid_sections 误报 by_total=True。现改为两分支：
#   ① 必须含动词（划分为/共/为 等）后接 N(个)?单位；② 或 N+个+单位（即便无动词）。
#   裸「N单位：」定义式不再命中，杜绝误报。
_TOTAL_SECTION_PATTERN = re.compile(
    "(?:"
    # ① 带动词的显式声明：划分为 / 共 / 为 ... N (个)? 单位
    "(?:本?项目)?(?:共|总计|共计|合计|划分|分|设|拆|分拆|为)"
    "\\s*(\\d+|[一二三四五六七八九十]+)\\s*(?:个)?\\s*(?:标段|包|分包|标包|标的|子项目)"
    "|"
    # ② 带"个"的声明（无动词也算）：N个标段
    "\\s*(\\d+|[一二三四五六七八九十]+)\\s*个\\s*(?:标段|包|分包|标包|标的|子项目)"
    ")"
)

# 【N】/【N-M】括号编号；MULTILINE 让 ^ 匹配行首
_BRACKET_PATTERN = re.compile("^【(\\d+)(?:-(\\d+))?】", re.MULTILINE)

# 文号行：以"号/文"结尾，或行首形如 【2024】15 号
_DOC_NUMBER_RE = re.compile("[号文]$|^\\s*【\\d+】\\d+\\s*号", re.MULTILINE)

# 合并提及："一、二、三标段" 这类列举不应被当作独立标段定义
_COMBINED_MENTION_RE = re.compile(
    "[一二三四五六七八九十\\d]+[、,]\\s*[一二三四五六七八九十\\d]+\\s*(?:标段|标包|分包|包)"
)


def _chinese_to_digit(ch: str) -> Optional[int]:
    idx = _CHINESE_DIGITS.index(ch) if ch in _CHINESE_DIGITS else -1
    return idx if idx >= 1 else None


def _ones_value(ch: str) -> Optional[int]:
    """个位/十位数字位：仅接受 一–九 / 壹–玖（1–9）。

    ✅ BUG 修复（2026-09-25）：旧实现十X 分支用 _chinese_to_digit 取个位，
    而它对 "十" 返回 10，且非法尾字符兜底成 10 —— 畸形输入 "十十" 被归一为
    20、"十a" 被归一为 10，非法值必须返回 None。
    """
    idx = _CHINESE_DIGITS.index(ch) if ch in _CHINESE_DIGITS else -1
    if 1 <= idx <= 9:
        return idx
    return _CHINESE_UPPER_DIGITS.get(ch)


def normalize_chinese_number(value: str) -> Optional[int]:
    """把字符串数字归一化为 1–99 的整数（阿拉伯数字 / 中文小写 / 大写 / 十X / X十X）。"""
    trimmed = (value or "").strip()
    if not trimmed:
        return None
    try:
        digit = int(trimmed)
    except (ValueError, TypeError):
        digit = None
    if digit is not None and 1 <= digit <= 99:
        return digit
    if trimmed in CHINESE_SMALL_MAP:
        return CHINESE_SMALL_MAP[trimmed]
    # 十X / 拾X（十一 → 11，拾贰 → 12）：X 必须是 1–9，非法/越界尾字符一律 None
    if len(trimmed) == 2 and trimmed[0] in _TEN_CHARS:
        ones = _ones_value(trimmed[1])
        return 10 + ones if ones is not None else None
    # X十Y / X拾Y（二十三 → 23，贰拾叁 → 23）
    if len(trimmed) == 3 and trimmed[1] in _TEN_CHARS:
        tens = _ones_value(trimmed[0])
        ones = _ones_value(trimmed[2])
        if tens is not None and ones is not None:
            return tens * 10 + ones
        return None
    # X十 / X拾（二十 → 20，贰拾 → 20）
    if len(trimmed) == 2 and trimmed[1] in _TEN_CHARS:
        tens = _ones_value(trimmed[0])
        return tens * 10 if tens is not None else None
    return None


def _get_line_at(text: str, index: int) -> str:
    """取 index 所在整行（去空白），供行级排除规则使用。"""
    line_start = index
    while line_start > 0 and text[line_start - 1] != "\n":
        line_start -= 1
    line_end = index
    while line_end < len(text) and text[line_end] != "\n":
        line_end += 1
    return text[line_start:line_end].strip()


def _is_combined_section_mention(line: str) -> bool:
    return bool(_COMBINED_MENTION_RE.search(line))


def _is_document_number_line(line: str) -> bool:
    return bool(_DOC_NUMBER_RE.search(line)) or "号文" in line


def detect_total_section_count(text: str) -> Optional[int]:
    """识别显式声明的总标段数（优先命中「标段」字样）。

    口径：只采纳 ≥2 的声明（单标段不构成多标段依据），
    detect_bid_sections 对 "本项目划分为 1 个标段" 同样不回填 total_declared。
    """
    section_count = None
    any_count = None
    for m in _TOTAL_SECTION_PATTERN.finditer(text):
        # 两分支分别落在 group(1) / group(2)
        raw = m.group(1) or m.group(2)
        count = normalize_chinese_number(raw)
        if not count or count < 2:
            continue
        any_count = count if any_count is None else max(any_count, count)
        if "标段" in m.group(0):
            section_count = count if section_count is None else max(section_count, count)
    if section_count is not None:
        return section_count
    return any_count


def count_definition_sections(text: str):
    """从 16 种定义模式中提取标段编号，返回 (去重计数, 标段标识列表)。"""
    detected = []
    for pattern, unit in _SECTION_DEF_PATTERNS:
        for m in pattern.finditer(text):
            idx = normalize_chinese_number(m.group(1))
            if not idx:
                continue
            if idx >= 1:
                if _is_combined_section_mention(_get_line_at(text, m.start())):
                    continue
                tag = f"{unit}:{idx}"
                if tag not in detected:
                    detected.append(tag)
    return len(detected), detected


def count_bracket_sections(text: str):
    """从 【N】 / 【N-M】 括号编号中提取（排除文号行）。返回 (去重计数, 标段标识列表)。"""
    groups = set()
    children = set()
    for m in _BRACKET_PATTERN.finditer(text):
        parent = int(m.group(1))
        child = int(m.group(2)) if m.group(2) else None
        if not parent or parent < 1 or _is_document_number_line(
            _get_line_at(text, m.start())
        ):
            continue
        if child:
            children.add(f"【{parent}-{child}】")
        else:
            groups.add(f"【{parent}】")
    # 存在多级编号（【2-1】）时以子项为准，避免父子重复计数
    if len(children) >= 2:
        return len(children), sorted(children)
    return len(groups), sorted(groups)


def detect_bid_sections(text: str) -> dict:
    """判断文本是否疑似多标段。返回结构化结果。

    返回字段：
      has_multiple   是否疑似多标段
      total_declared 显式声明的总标段数（无则为 None）
      detected_count 通过模式/括号识别出的标段数
      sections       识别到的标段标识（如 "标段:3"、"【2-1】"）
      by_total       判定为"多标段"是否来自显式总数声明
    """
    raw = text or ""
    if not raw.strip():
        return {
            "has_multiple": False,
            "total_declared": None,
            "detected_count": 0,
            "sections": [],
            "by_total": False,
        }

    total = detect_total_section_count(raw)
    if total == 1:
        return {
            "has_multiple": False,
            "total_declared": 1,
            "detected_count": 1,
            "sections": ["标段:1"],
            "by_total": True,
        }
    if total and total >= 2:
        return {
            "has_multiple": True,
            "total_declared": total,
            "detected_count": total,
            "sections": [f"标段:{i + 1}" for i in range(total)],
            "by_total": True,
        }

    def_count, def_sections = count_definition_sections(raw)
    br_count, br_sections = count_bracket_sections(raw)
    detected = max(def_count, br_count)
    sections = def_sections if def_count >= br_count else br_sections
    return {
        "has_multiple": detected >= 2,
        "total_declared": None,
        "detected_count": detected,
        "sections": sections,
        "by_total": False,
    }
