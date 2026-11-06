"""标段检测（多标段投标识别）。

纯规则探测器：仅用于快速判断招标文件是否疑似多标段，不生成最终标段列表。
覆盖：

- 显式声明总数（"本项目划分为 3 个标段 / 包 / 分包 / 标包 / 子项目"）
- 16 种「标段 / 标包 / 分包 / 包」定义模式（中文数字 + 阿拉伯数字 + 第X / X后 变体）
- 文号之外的 【N】 / 【N-M】 括号编号章节

中文数字归一化支持 1–99。

判定口径：只做"疑似判断"，不做最终标段切分——真正的标段拆分由用户（导入
目录后手动分章）或后续流程决定，避免误切正文结构。三条证据（显式总数声明 /
定义式 / 括号编号）独立采集，按优先级合并。
"""

from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# 数字字符与取值
# ---------------------------------------------------------------------------

# 单字小写数字
_SINGLE_DIGITS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

# 大写数字（组合位需要完整的 壹–玖；金额式写法在正式招标文件中常见）
_UPPER_DIGITS = {
    "壹": 1,
    "贰": 2,
    "叁": 3,
    "肆": 4,
    "伍": 5,
    "陆": 6,
    "柒": 7,
    "捌": 8,
    "玖": 9,
}

# 对外保留的单字映射（小写 1–9 + 大写 壹–伍，不含组合位）
CHINESE_SMALL_MAP = {
    **_SINGLE_DIGITS,
    "十": 10,
    **{key: _UPPER_DIGITS[key] for key in ("壹", "贰", "叁", "肆", "伍")},
}

# 组合解析使用的 1–9 取值表（不含"十/拾"，十位标记单独判断）
_DIGIT_VALUES = {**_SINGLE_DIGITS, **_UPPER_DIGITS}
_TEN_MARKS = frozenset(("十", "拾"))

# 定义式允许出现的中文数字字符（与原规则保持同一字符集）
_CN_RUN = "一二三四五六七八九十壹贰叁肆伍"

# 标段单位：短单位"包"放最后，避免抢先命中"标包/分包"
_UNITS = ("标段", "标包", "分包", "包")

# 每种单位的四种定义式：中文数字 / 阿拉伯数字 / 第N / N后置
_DEFINITION_FORMS = (
    ("([{nums_cn}]+){unit}[：:；;]"),
    ("(\\d+){unit}[：:；;]"),
    ("第([{nums_cn}\\d]+){unit}[：:；;]"),
    ("{unit}([{nums_cn}\\d]+)[：:；;]"),
)

# 16 种定义模式（按单位 × 形态生成，单位顺序决定匹配优先级）
_SECTION_DEF_PATTERNS = [
    (re.compile(template.format(nums_cn=_CN_RUN, unit=unit)), unit) for unit in _UNITS for template in _DEFINITION_FORMS
]

# ---------------------------------------------------------------------------
# 显式总数声明
# ---------------------------------------------------------------------------
# 两分支口径：
#   ① 必须含声明动词（划分为/共/为 等）后接 N(个)?单位；
#   ② 或 N+个+单位（即便无动词）。
# 裸「N单位：」定义式不命中，杜绝把「三标段：」误判成"声明总数=3"。

_DECLARATION_VERB = "(?:本?项目)?(?:共|总计|共计|合计|划分|分|设|拆|分拆|为)"
_SECTION_UNIT_ALT = "(?:标段|包|分包|标包|标的|子项目)"
_COUNT_ALT = "\\d+|[" + _CN_RUN + "]+"

_TOTAL_SECTION_PATTERN = re.compile(
    "(?:"
    + _DECLARATION_VERB
    + "\\s*(?P<declared_verb>"
    + _COUNT_ALT
    + ")\\s*(?:个)?\\s*"
    + _SECTION_UNIT_ALT
    + "|"
    + "\\s*(?P<declared_ge>"
    + _COUNT_ALT
    + ")\\s*个\\s*"
    + _SECTION_UNIT_ALT
    + ")"
)

# 【N】/【N-M】括号编号；MULTILINE 让 ^ 匹配行首
_BRACKET_PATTERN = re.compile("^【(\\d+)(?:-(\\d+))?】", re.MULTILINE)

# 文号行：以"号/文"结尾，或行首形如 【2024】15 号
_DOC_NUMBER_RE = re.compile("[号文]$|^\\s*【\\d+】\\d+\\s*号", re.MULTILINE)

# 合并提及："一、二、三标段" 这类列举不应被当作独立标段定义
_COMBINED_MENTION_RE = re.compile(
    "[一二三四五六七八九十\\d]+[、,]\\s*[一二三四五六七八九十\\d]+\\s*(?:标段|标包|分包|包)"
)


# ---------------------------------------------------------------------------
# 数字归一
# ---------------------------------------------------------------------------


def _digit_value(ch: str) -> Optional[int]:
    """取 1–9 的数字位；"十/拾"、非法字符一律 None。"""
    return _DIGIT_VALUES.get(ch)


def _ten_with_ones(ch: str) -> Optional[int]:
    """十X / 拾X → 10 + X；尾字符必须是 1–9。"""
    value = _digit_value(ch)
    return 10 + value if value is not None else None


def _tens_only(ch: str) -> Optional[int]:
    """X十 / X拾 → X × 10；首位必须是 1–9。"""
    value = _digit_value(ch)
    return value * 10 if value is not None else None


def _tens_and_ones(tens_ch: str, ones_ch: str) -> Optional[int]:
    """X十Y / X拾Y → X × 10 + Y；任一位非法则 None。"""
    tens = _digit_value(tens_ch)
    ones = _digit_value(ones_ch)
    if tens is None or ones is None:
        return None
    return tens * 10 + ones


def _parse_composite(token: str) -> Optional[int]:
    """解析「十X」「X十」「X十Y」等组合位；形态不合法一律 None。"""
    if len(token) == 2:
        if token[0] in _TEN_MARKS:
            return _ten_with_ones(token[1])
        if token[1] in _TEN_MARKS:
            return _tens_only(token[0])
        return None
    if len(token) == 3 and token[1] in _TEN_MARKS:
        return _tens_and_ones(token[0], token[2])
    return None


def normalize_chinese_number(value: str) -> Optional[int]:
    """把字符串数字归一化为 1–99 的整数（阿拉伯数字 / 中文小写 / 大写 / 十X / X十X）。"""
    token = (value or "").strip()
    if not token:
        return None

    try:
        arabic = int(token)
    except (ValueError, TypeError):
        arabic = None
    if arabic is not None and 1 <= arabic <= 99:
        return arabic

    if token in CHINESE_SMALL_MAP:
        return CHINESE_SMALL_MAP[token]

    return _parse_composite(token)


# ---------------------------------------------------------------------------
# 行级排除
# ---------------------------------------------------------------------------


def _get_line_at(text: str, index: int) -> str:
    """取 index 所在整行（去空白），供行级排除规则使用。"""
    line_start = text.rfind("\n", 0, index) + 1
    line_end = text.find("\n", index)
    if line_end < 0:
        line_end = len(text)
    return text[line_start:line_end].strip()


def _is_combined_section_mention(line: str) -> bool:
    return bool(_COMBINED_MENTION_RE.search(line))


def _is_document_number_line(line: str) -> bool:
    return bool(_DOC_NUMBER_RE.search(line)) or "号文" in line


# ---------------------------------------------------------------------------
# 三条证据采集
# ---------------------------------------------------------------------------


def detect_total_section_count(text: str) -> Optional[int]:
    """识别显式声明的总标段数（优先命中「标段」字样）。

    口径：只采纳 ≥2 的声明（单标段不构成多标段依据），
    detect_bid_sections 对 "本项目划分为 1 个标段" 同样不回填 total_declared。
    """
    section_count = None
    any_count = None
    for match in _TOTAL_SECTION_PATTERN.finditer(text):
        raw = match.group("declared_verb") or match.group("declared_ge")
        count = normalize_chinese_number(raw)
        if count is None or count < 2:
            continue
        if any_count is None or count > any_count:
            any_count = count
        if "标段" in match.group(0):
            if section_count is None or count > section_count:
                section_count = count
    return section_count if section_count is not None else any_count


def _iter_definition_tags(text: str):
    """逐个产出定义式命中的标段标识（含合并列举排除与去重前的原始序列）。"""
    for pattern, unit in _SECTION_DEF_PATTERNS:
        for match in pattern.finditer(text):
            number = normalize_chinese_number(match.group(1))
            if number is None or number < 1:
                continue
            if _is_combined_section_mention(_get_line_at(text, match.start())):
                continue
            yield f"{unit}:{number}"


def count_definition_sections(text: str):
    """从 16 种定义模式中提取标段编号，返回 (去重计数, 标段标识列表)。"""
    detected = list(dict.fromkeys(_iter_definition_tags(text)))
    return len(detected), detected


def _scan_bracket_ids(text: str):
    """采集括号编号的父级与子级标识集合（排除文号行）。"""
    groups = set()
    children = set()
    for match in _BRACKET_PATTERN.finditer(text):
        parent = int(match.group(1))
        child = int(match.group(2)) if match.group(2) else None
        if parent < 1 or _is_document_number_line(_get_line_at(text, match.start())):
            continue
        if child:
            children.add(f"【{parent}-{child}】")
        else:
            groups.add(f"【{parent}】")
    return groups, children


def count_bracket_sections(text: str):
    """从 【N】 / 【N-M】 括号编号中提取（排除文号行）。返回 (去重计数, 标段标识列表)。"""
    groups, children = _scan_bracket_ids(text)
    # 存在多级编号（【2-1】）时以子项为准，避免父子重复计数
    primary = children if len(children) >= 2 else groups
    return len(primary), sorted(primary)


# ---------------------------------------------------------------------------
# 汇总判定
# ---------------------------------------------------------------------------


def _summary(
    has_multiple: bool,
    total_declared: Optional[int],
    detected_count: int,
    sections: list,
    by_total: bool,
) -> dict:
    return {
        "has_multiple": has_multiple,
        "total_declared": total_declared,
        "detected_count": detected_count,
        "sections": sections,
        "by_total": by_total,
    }


def detect_bid_sections(text: str) -> dict:
    """判断文本是否疑似多标段，返回结构化结果。

    返回字段：
      has_multiple   是否疑似多标段
      total_declared 显式声明的总标段数（无则为 None）
      detected_count 通过模式/括号识别出的标段数
      sections       识别到的标段标识（如 "标段:3"、"【2-1】"）
      by_total       判定为"多标段"是否来自显式总数声明
    """
    raw = text or ""
    if not raw.strip():
        return _summary(False, None, 0, [], False)

    declared = detect_total_section_count(raw)
    if declared is not None:
        sections = ["标段:1"] if declared == 1 else [f"标段:{i}" for i in range(1, declared + 1)]
        return _summary(declared >= 2, declared, declared, sections, True)

    def_count, def_sections = count_definition_sections(raw)
    br_count, br_sections = count_bracket_sections(raw)
    use_brackets = br_count > def_count
    detected = br_count if use_brackets else def_count
    sections = br_sections if use_brackets else def_sections
    return _summary(detected >= 2, None, detected, sections, False)
