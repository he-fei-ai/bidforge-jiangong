# -*- coding: utf-8 -*-
"""BUG #2 护栏（2026-10-03）：strip_outline_numbering 数量型标题不被误剥首字。

背景（数据丢失型缺陷）：numbering.py::_STRIP_NUMBER_RE 的点分编号分支原用
`[0-9]+(?:\\.[0-9]+)*`（星号），使「单段数字直接接中文（无分隔符）」也被当编号
剥离，把数量型标题的数字剥掉：
    "2层作业平台" → "层作业平台"、"2台塔吊" → "台塔吊"、"10个人" → "个人"
与该函数 docstring 承诺的「单段数字编号必须后跟分隔符才剥离」相矛盾。
修复：捕获限定为至少一个点段 `(?:\\.[0-9]+)+`，无分隔符的 CJK 紧邻剥离只作用于
多段点分路径（"2.4.1钢筋工程"）。前端 LEADING_NUMBER_RE 必须逐字符同步（parity 锁）。
"""
from app.services.numbering import (
    _STRIP_NUMBER_RE,
    strip_outline_numbering,
)


# 数量型标题：单段数字直接接中文（无量词分隔符），必须原样保留
QUANTITY_TITLES = [
    "2层作业平台",
    "2台塔吊",
    "4处预留洞口",
    "10个人",
    "1级配电",
    "2名安全员",
    "3层脚手架",
    "5部施工电梯",
]


def test_quantity_titles_preserved_not_stripped():
    """数量型标题首字（数字）不得被剥离 —— 数据丢失型 BUG 的直接锚定。"""
    for t in QUANTITY_TITLES:
        assert strip_outline_numbering(t) == t, f"数量型标题被误剥: {t!r} → {strip_outline_numbering(t)!r}"


def test_quantity_digit_survives_raw_strip_regex():
    """原始 _STRIP_NUMBER_RE 对数量型标题亦不匹配（无分隔符 + 无点段 → 剥不动）。"""
    for t in QUANTITY_TITLES:
        assert _STRIP_NUMBER_RE.sub("", t, count=1) == t, f"原始正则误剥数量标题: {t!r}"


def test_multisegment_dot_path_before_cjk_still_stripped():
    """多段点分路径紧邻 CJK（无空格）仍必须整条剥离 —— 修复不得回退此行为。"""
    cases = {
        "2.4.1钢筋工程": "钢筋工程",
        "3.2.4.5钢筋": "钢筋",
        "1.2.3（1）细部构造": "（1）细部构造",
        "5.1.1.1 深基坑": "深基坑",
    }
    for src, exp in cases.items():
        assert strip_outline_numbering(src) == exp, f"{src!r} 期望剥为 {exp!r}, 实得 {strip_outline_numbering(src)!r}"


def test_single_segment_with_separator_still_stripped():
    """带分隔符的单段编号剥离行为不变（由 [0-9]+分隔符+(?![0-9]) 分支负责）。"""
    assert strip_outline_numbering("2、施工安排") == "施工安排"
    assert strip_outline_numbering("2 施工准备") == "施工准备"


def test_year_and_word_number_still_guarded():
    """回归锁：年份 / 中文数字标题 / 纯数字编号契约不受本次修复影响。"""
    assert strip_outline_numbering("2023年规范") == "2023年规范"
    assert strip_outline_numbering("3D打印施工方案") == "3D打印施工方案"
    assert strip_outline_numbering("十二层平面布置") == "十二层平面布置"
    assert strip_outline_numbering("第一章 工程概况") == "工程概况"
    assert strip_outline_numbering("1.1") == "1.1"  # 纯数字编号原样返回


def test_fix_locks_at_least_one_dot_segment():
    """结构锁：点分编号分支的捕获量词必须是 `+`（至少一个点段），不得回退成 `*`。

    回退成 `*` 即复活「单段数字紧邻 CJK 被误剥」的数量型标题缺陷。
    """
    pat = _STRIP_NUMBER_RE.pattern
    assert r"([0-9]+(?:\.[0-9]+)+)" in pat, "点分编号捕获缺少『至少一个点段』限定"
    assert r"([0-9]+(?:\.[0-9]+)*)" not in pat, "点分编号捕获回退成星号将误剥数量型标题"
