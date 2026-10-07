"""编号剥离分叉收口 + T-2 下沉死副本清理的护栏（2026-10-05 · D4）。

背景
----
「标题内嵌编号剥离」在本仓曾出现**多份各自演化的副本**（编号剥离是双重编号的
根因高发区）。2026-10-05 本轮把最后一处活动分叉与全部死副本收口：

1. `services/content_blocks.py::_strip_title_number` 旧实现自带
   `_TITLE_NUM_STRIP_RES`（5 条正则），实测比规范实现**更差**：
       "1.2.3钢筋工程" → "3钢筋工程"（点分未吃满整条路径）
       "1.1"           → "1"（纯编号标题被误剥半截）
   现改为委托 `numbering.strip_outline_numbering`（唯一实现）+ 有界循环。

2. `routers/export.py` 在 T-2 下沉后残留 7 个**无任何引用**的本地副本常量
   （`_ORDERED_MARKER_RES` / `_TABLE_CAPTION_RE` / `_IMAGE_LINE_RE` /
   `_MERMAID_TITLE_RES` / `_LEAD_IN_TAIL_RE` / `_HEADING_NUM_PREFIX_RES` /
   `_TITLE_NUM_STRIP_RES`），以及一份与 content_blocks 完全相同的
   `_LEAD_IN_HINT_RE`（活动副本）。本轮全部删除，后者改为从 content_blocks 导入。

本文件锁定两件事：行为正确 + 不再分叉。
复跑：``python -m pytest tests/test_numbering_fork_cleanup_20261005.py -q``
"""
from app.routers import export as export_mod
from app.services import content_blocks as cb
from app.services.numbering import strip_outline_numbering

# T-2 下沉后不得在 export.py 中再次出现的死副本名
_REMOVED_FORK_NAMES = [
    "_ORDERED_MARKER_RES",
    "_TABLE_CAPTION_RE",
    "_IMAGE_LINE_RE",
    "_MERMAID_TITLE_RES",
    "_LEAD_IN_TAIL_RE",
    "_HEADING_NUM_PREFIX_RES",
    "_TITLE_NUM_STRIP_RES",
]


def test_strip_title_number_handles_dotted_path_fully():
    """"1.2.3钢筋工程" 必须一次吃满整条编号路径（旧实现回退成 "3钢筋工程"）。"""
    assert cb._strip_title_number("1.2.3钢筋工程") == "钢筋工程"
    assert cb._strip_title_number("1.1 项目基本信息") == "项目基本信息"
    assert cb._strip_title_number("第一章 工程概况") == "工程概况"


def test_strip_title_number_preserves_pure_number_and_year():
    """纯编号标题与年份前缀不得被误剥（旧实现把 "1.1" 剥成 "1"）。"""
    assert cb._strip_title_number("1.1") == "1.1"
    assert cb._strip_title_number("1.2.3") == "1.2.3"
    assert cb._strip_title_number("2023 年度安全生产计划") == "2023 年度安全生产计划"
    assert cb._strip_title_number("十二层平面布置") == "十二层平面布置"


def test_strip_title_number_collapses_multi_prefix():
    """多重前缀（"第1章 1.1 工程概况"）仍需循环收敛到裸标题。"""
    assert cb._strip_title_number("第1章 1.1 工程概况") == "工程概况"
    assert cb._strip_title_number("一、 1）编制依据") == "编制依据"


def test_strip_title_number_matches_canonical_fixed_point():
    """content_blocks 的剥离 = numbering 唯一实现的**有界不动点**（防再次分叉）。"""
    cases = ["第五章 施工计划", "（一）编制说明", "1.1 项目概况",
             "a. 附录说明", "钢筋工程", "", "第1章 1.1 工程概况"]
    for c in cases:
        s = c
        for _ in range(8):
            n = strip_outline_numbering(s).strip()
            if n == s:
                break
            s = n
        assert cb._strip_title_number(c) == s, f"分叉: {c!r}"


def test_export_no_dead_fork_constants():
    """export.py 不得再残留 T-2 下沉后的死副本常量。"""
    for name in _REMOVED_FORK_NAMES:
        assert not hasattr(export_mod, name), f"export.py 死副本未清理: {name}"


def test_export_lead_in_hint_is_single_source():
    """活动副本 `_LEAD_IN_HINT_RE` 必须与 content_blocks 同源（同一对象）。"""
    assert export_mod._LEAD_IN_HINT_RE is cb._LEAD_IN_HINT_RE