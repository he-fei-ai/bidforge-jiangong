"""标题编号模板系统与共享常量（从 heading_standard.py 拆分）

包含：
- 共享常量：HEADING_MANAGED_LEVELS / CHINESE_NUMBERS / ALPHABET / HEADING_STYLE_CONFIG 等
- 模板化编号系统：DEFAULT_NUMBERING_TEMPLATES / format_outline_number_by_template / format_heading_by_id
- 标题规范提示词生成：build_heading_spec_prompt
"""
from __future__ import annotations

import re


# ============================================================
# 受控编号层级：L1~L7 全部强制应用标准编号
# ============================================================
HEADING_MANAGED_LEVELS: frozenset[int] = frozenset({1, 2, 3, 4, 5, 6, 7})

# ✅ 编号统一（2026-09-25）：中文数字 / 字母序号表唯一事实源收敛到
#    services/numbering.py，此处保留同名再导出（本模块消费方与既有测试
#    按 heading_templates.CHINESE_NUMBERS / ALPHABET 引用，语义不变）。
from app.services.numbering import ALPHABET, CHINESE_NUMBERS  # noqa: E402, F401

# ============================================================
# 七级标题样式配置（2026-09 规范）
#   L1 第一章  编号+空格+标题
#   L2 1       编号+空格+标题
#   L3 1.1     编号+空格+标题
#   L4 1.1.1   编号+空格+标题
#   L5 1.1.1.1 编号+、+标题
#   L6 1）      编号+、+标题
#   L7 a        编号+、+标题
#   所有标题字体不得倾斜（italic=False）
# ============================================================
HEADING_STYLE_CONFIG: dict[int, dict] = {
    1: {"prefix": "第", "suffix": "章", "number_format": "chinese", "punctuation": " ",
        "font_size": 14, "font_name": "宋体", "bold": True, "italic": False},
    2: {"prefix": "", "suffix": "", "number_format": "digit", "punctuation": " ",
        "font_size": 14, "font_name": "宋体", "bold": True, "italic": False},
    3: {"prefix": "", "suffix": "", "number_format": "digit_dot", "punctuation": " ",
        "font_size": 14, "font_name": "宋体", "bold": True, "italic": False},
    4: {"prefix": "", "suffix": "", "number_format": "digit_dot_dot", "punctuation": " ",
        "font_size": 14, "font_name": "宋体", "bold": True, "italic": False},
    5: {"prefix": "", "suffix": "", "number_format": "digit_dot_dot_dot", "punctuation": "、",
        "font_size": 14, "font_name": "宋体", "bold": True, "italic": False},
    6: {"prefix": "", "suffix": "）", "number_format": "digit_parenthesis", "punctuation": "、",
        "font_size": 12, "font_name": "宋体", "bold": False, "italic": False, "body_style": True},
    7: {"prefix": "", "suffix": "", "number_format": "alpha", "punctuation": "、",
        "font_size": 12, "font_name": "宋体", "bold": False, "italic": False, "body_style": True},
    8: {"prefix": "", "suffix": "", "number_format": "alpha_alpha", "punctuation": "、",
        "font_size": 12, "font_name": "宋体", "bold": False, "italic": False, "body_style": True},
}

# 七级标题正则（用于从文本反向检测层级）
HEADING_REGEX_PATTERNS: list[tuple[str, int, int]] = [
    (r"^第([一二三四五六七八九十百千万零\d]+)[章节][、\s]*(.+)$", 1, 2),
    (r"^[（(]([一二三四五六七八九十百]+)[)）][、\s]*(.+)$", 5, 2),
    (r"^([一二三四五六七八九十百]+)、\s*(.+)$", 5, 2),
    # 十进制四级：1.1.1.1 标题 → 五级（2026-09 规范 L5；分隔符限定 [、\s]+，
    # 若允许点分会被 "1.1.1.1.1 五级" 误判成四级）
    (r"^(\d+(?:\.\d+){3})[、\s]+(.+)$", 5, 2),
    (r"^(\d+\.\d+\.\d+)[、\s]+(.+)$", 4, 2),
    (r"^(\d+\.\d+)[、\s]+(.+)$", 3, 2),
    (r"^(\d+)[、\s]+(.+)$", 2, 2),
    (r"^(\d+)）\s*(.+)$", 6, 2),
    (r"^([a-z]{1,2})[、\s]+(.+)$", 7, 2),
]

# 圈码数字
CIRCLED_NUMBERS: dict[int, str] = {
    1: "①", 2: "②", 3: "③", 4: "④", 5: "⑤", 6: "⑥", 7: "⑦", 8: "⑧",
    9: "⑨", 10: "⑩", 11: "⑪", 12: "⑫", 13: "⑬", 14: "⑭", 15: "⑮", 16: "⑯",
    17: "⑰", 18: "⑱", 19: "⑲", 20: "⑳",
}

# 罗马数字
ROMAN_NUMBERS: list[str] = [
    "", "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
    "xi", "xii", "xiii", "xiv", "xv", "xvi", "xvii", "xviii", "xix", "xx",
]
ROMAN_NUMBERS_UPPER: list[str] = [s.upper() for s in ROMAN_NUMBERS]


# ============================================================
# 默认编号模板配置
# ✅ 2026-09-23 编号命名空间统一：L2~L5 只取 ID 路径的末 N 段
#    （L2={num}、L3={last2}、L4={last3}、L5={last4}），与 heading_v2 计数器口径、
#    前端 outlineTreeLogic slice(-(level-1))、HEADING_STYLE_CONFIG 逐级一致。
#    旧 {full} 含章号全路径（L3 "2.3.1" → "2.3.1"）与展示口径（"3.1"）分叉，
#    导致同一目录在模板预览 / 前端树 / 导出成稿三处编号不一致。
# ============================================================
DEFAULT_NUMBERING_TEMPLATES: dict[int, dict[str, str]] = {
    1: {"numbering_format": "custom", "numbering_template": "第{zh}章"},
    2: {"numbering_format": "custom", "numbering_template": "{num}"},
    3: {"numbering_format": "custom", "numbering_template": "{last2}"},
    4: {"numbering_format": "custom", "numbering_template": "{last3}"},
    5: {"numbering_format": "custom", "numbering_template": "{last4}"},
    6: {"numbering_format": "custom", "numbering_template": "{num}）"},
    7: {"numbering_format": "custom", "numbering_template": "{alpha}"},
}


def outline_number_parts(id_str: str) -> list[int]:
    """将点分 ID 拆分为数字数组"""
    return [int(p) for p in str(id_str or "").split(".") if p.isdigit() and int(p) > 0]


def _replace_last_n(m: re.Match, parts: list[int]) -> str:
    n = int(m.group(1))
    if len(parts) >= n:
        return ".".join(str(p) for p in parts[-n:])
    return ".".join(str(p) for p in parts)


def _replace_tail_n(m: re.Match, parts: list[int]) -> str:
    n = int(m.group(1))
    if len(parts) >= n:
        return ".".join(str(p) for p in parts[n - 1:])
    return str(parts[-1] if parts else 1)


def format_outline_number_by_template(id_str: str, template: str) -> str:
    """基于模板格式化编号（支持 {zh}/{num}/{tail}/{tailN}/{alpha}/{circled}/{roman} 等占位符）"""
    parts = outline_number_parts(id_str)
    if not parts:
        return ""
    last_part = parts[-1]
    full_str = ".".join(str(p) for p in parts)
    cn = CHINESE_NUMBERS[last_part] if 0 < last_part < len(CHINESE_NUMBERS) else str(last_part)
    alpha = ALPHABET[last_part - 1] if 1 <= last_part <= len(ALPHABET) else "a"
    alpha_upper = alpha.upper()
    circled = CIRCLED_NUMBERS.get(last_part, str(last_part))
    roman = ROMAN_NUMBERS[last_part] if 0 < last_part < len(ROMAN_NUMBERS) else str(last_part)
    roman_upper = ROMAN_NUMBERS_UPPER[last_part] if 0 < last_part < len(ROMAN_NUMBERS_UPPER) else str(last_part)
    if len(parts) >= 3:
        tail = ".".join(str(p) for p in parts[2:])
    else:
        tail = str(last_part)
    result = template
    result = re.sub(r"\{tail(\d+)\}", lambda m: _replace_tail_n(m, parts), result)
    result = re.sub(r"\{last(\d+)\}", lambda m: _replace_last_n(m, parts), result)
    result = result.replace("{tail}", tail)
    result = result.replace("{zh}", cn)
    result = result.replace("{num}", str(last_part))
    result = result.replace("{full}", full_str)
    result = result.replace("{circled}", circled)
    result = result.replace("{alpha}", alpha)
    result = result.replace("{ALPHA}", alpha_upper)
    result = result.replace("{roman}", roman)
    result = result.replace("{ROMAN}", roman_upper)
    return result.strip()


def get_heading_template(level: int) -> dict[str, str]:
    return DEFAULT_NUMBERING_TEMPLATES.get(
        level, DEFAULT_NUMBERING_TEMPLATES.get(3, {"numbering_format": "custom", "numbering_template": "{tail}"}))


def format_heading_by_id(id_str: str, level: int, title: str, custom_template: str | None = None) -> str:
    """基于 ID 路径生成完整标题（含编号）"""
    if not title or not title.strip():
        return title or ""
    template = custom_template if custom_template else get_heading_template(level).get("numbering_template", "{tail}")
    number = format_outline_number_by_template(id_str, template)
    if not number:
        return title
    style_config = HEADING_STYLE_CONFIG.get(level, HEADING_STYLE_CONFIG[3])
    punct = style_config.get("punctuation", "、")
    return f"{number}{punct}{title.strip()}"


def should_insert_space_after_number(prefix: str) -> bool:
    return not bool(re.search(r"[、，。；：）)】\]》〉]$", prefix))


def format_outline_title(id_str: str, title: str, level: int, custom_template: str | None = None) -> str:
    return format_heading_by_id(id_str, level, title, custom_template)


def build_heading_spec_prompt() -> str:
    """生成标题编号规范说明（用于注入到 System Prompt）

    2026-09-23 与 HeadingNumberingGeneratorV2 / HEADING_STYLE_CONFIG / 前端树对齐：
    L5 由旧「（X）中文括号」改为十进制 X.X.X.X、；示例编号改为节内相对路径（不含章号）。
    """
    return """【标题编号规范 - 必须严格遵守】

所有章节标题必须按以下格式编号，编号与标题之间用空格分隔，不得自行更改：

=== 强制管理层级（L1~L5）===

一级标题（四号字，宋体，加粗）：第X章 标题
  示例：第一章 编制综合说明

二级标题（四号字，宋体，加粗）：X 标题（阿拉伯数字）
  示例：1 工程概况

三级标题（四号字，宋体，加粗）：X.X 标题
  示例：1.1 地理位置

四级标题（四号字，宋体，加粗）：X.X.X 标题
  示例：1.1.1 立面概况

五级标题（不加粗，字号/字体同正文）：X.X.X.X、标题（阿拉伯数字点分 + 顿号）
  示例：1.1.1.1、设计标准

=== 正文样式层级（L6~L8，不强制编号格式，字体同正文）===

六级标题：不加粗，编号格式如 1）、2）。
七级标题：不加粗，编号格式如 a、b。
八级标题：不加粗，编号格式可自由选择。

重要规则：
1. 所有标题禁止使用倾斜（斜体）
2. L1~L5 必须使用对应的编号格式，不得混用；L1~L4 编号后用空格分隔标题，L5~L7 编号后用顿号（、）分隔
3. L3~L5 编号为节内相对路径、不含章号（如第二章第 3 节下的三级标题为 3.1、四级为 3.1.1、五级为 3.1.1.1）
4. L6~L8 编号格式不限，但必须能从内容上区分层级
5. L1~L4 标题加粗、四号字；L5~L8 不加粗、字号/字体同正文
"""
