"""正文质量审计的两条判据必须共享同一套「围栏保护」口径（2026-10-06）。

背景（正文生成·质量审计口径分叉）：
``content_polish.quality_issues`` 返回两路告警 —— 口语化/AI 腔命中与
已废止标准编号命中。口语化判据 ``find_colloquial_hits`` 走 ``_strip_fences``
（围栏代码块与表格内的文本不计入），而废止标准判据却直接扫**原文**：

* AI 生成的 ```` ```chart-json ```` 对比图最常见的形态是「规范版本对比」，
  ``data`` 数组里必然出现被替代的旧编号（``JGJ 46-2005``、``GB 50202-2002`` …）；
* ``~~~mermaid`` 图里也可能出现编号（如节点标签「按 GB 50202-2002 验收」）。

于是章节被误报「引用了已废止标准」，而该编号在正文里根本不存在。用户按告警
逐段排查却找不到，质量面板失去可信度。该告警随 ``section_done`` 事件与
手工保存（sections.update_section）两条路径下发到前端日志区，是用户可见的假告警。

本文件锁定：
- 代码块（``` 与 ~~~）内的编号不告警；
- 表格单元格内的编号**仍告警**（编制依据表引用废止编号是交付件缺陷，
  不能被豁免 —— 这是本次修复刻意保留的判据，防止一刀切放宽）；
- 正文散文中的编号告警不变（不回归）；
- 两条判据对同一份正文的保护范围一致（parity）。
"""
from app.services.content_polish import (
    _protected_ranges,
    _strip_fences,
    find_abolished_codes,
    find_colloquial_hits,
    quality_issues,
    sanitize_ai_content,
)


def _chart_json_with_abolished():
    """图表数据里出现被替代的旧编号（正文并未引用）。"""
    return (
        "本项工程执行现行规范组织施工。\n\n"
        "```chart-json\n"
        '{"type":"comparison","title":"规范版本对比","data":'
        '[{"name":"JGJ 46-2005","value":"旧版"},'
        '{"name":"JGJ/T 46-2024","value":"新版"}]}\n'
        "```\n\n"
        "以上图表仅供版本对照，正文不引用旧版编号。"
    )


def _tilde_fence_with_abolished():
    """波浪号围栏（mermaid）内出现被替代的旧编号。"""
    return (
        "本项工程执行现行规范。\n\n"
        "~~~mermaid\n"
        "flowchart TD\n"
        '  A["GB 50202-2002 验收"] --> B["GB 50202-2018 验收"]\n'
        "~~~\n\n"
        "尾段散文。\n"
    )


def test_quality_issues_ignores_code_in_backtick_fence():
    issues = quality_issues(_chart_json_with_abolished())
    assert issues["abolished_standards"] == []


def test_quality_issues_ignores_code_in_tilde_fence():
    issues = quality_issues(_tilde_fence_with_abolished())
    assert issues["abolished_standards"] == []


def test_quality_issues_still_flags_code_in_prose():
    """正文散文中的废止编号必须照旧告警（不回归）。"""
    assert quality_issues("临时用电执行 JGJ 46-2005 的规定。")["abolished_standards"] \
        == ["JGJ 46-2005"]
    # 无空格 / 全角破折号等变体同样命中（口径归一化不变）
    assert quality_issues("依据 GB50202-2002 施工")["abolished_standards"] \
        == ["GB 50202-2002"]
    assert quality_issues("依据 GB 50202—2002 施工")["abolished_standards"] \
        == ["GB 50202-2002"]


def test_quality_issues_still_flags_code_in_table():
    """表格单元格里的编号是交付件本身的内容，必须仍告警。

    编制依据表引用废止编号是真实缺陷；若随代码块一起豁免，
    就等于一刀切放宽判据 —— 本用例锁死该取舍。
    """
    text = (
        "本项工程执行下列标准：\n\n"
        "| 序号 | 标准编号 | 状态 |\n"
        "| --- | --- | --- |\n"
        "| 1 | GB 50202-2002 | 现行 |\n"
        "\n正文段落。\n"
    )
    assert quality_issues(text)["abolished_standards"] == ["GB 50202-2002"]


def test_both_judges_share_the_same_fence_scope():
    """parity：两条判据对同一份正文的保护范围必须一致。

    把口语化词与废止编号同时只放进代码块 —— 两条判据都必须零命中。
    这是风险同构的判据：任一判据退回「扫原文」，本用例立即失败。
    """
    text = (
        "本项工程临时用电执行 JGJ/T 46-2024，施工安全按规范落实。\n\n"
        "```chart-json\n"
        '{"type":"comparison","data":[{"name":"JGJ 46-2005","value":1},'
        '{"name":"咱们","value":2}]}\n'
        "```\n\n"
        "尾段：以上图表仅供版本对照。\n"
    )
    issues = quality_issues(text)
    # 围栏内 咱们 / JGJ 46-2005 均不得计入（两条判据同口径）
    assert issues["abolished_standards"] == []
    assert issues["colloquial_hits"] == []


def test_both_judges_agree_when_fence_and_prose_coexist():
    """正文里有口语化词、代码块里有废止编号：只剩口语化告警。"""
    text = (
        "咱们要注意临时用电安全。\n\n"
        "~~~chart-json\n"
        '{"type":"comparison","data":[{"name":"GB 50202-2002","value":1}]}\n'
        "~~~\n\n"
        "尾段散文。\n"
    )
    issues = quality_issues(text)
    assert issues["abolished_standards"] == []
    assert "咱们" in issues["colloquial_hits"]


def test_strip_fences_default_still_removes_tables():
    """默认参数行为不变：口语化判据仍剔除表格（历史契约）。"""
    text = "咱们施工。\n| 估算 | 500万 |\n| --- | --- |\n| 值 | 咱们 |\n尾段。\n"
    stripped = _strip_fences(text)
    # 表格行被整段剔除，表格内的「咱们」不进入审计文本
    assert stripped.count("咱们") == 1
    assert "| 估算 | 500万 |" not in stripped
    assert "| --- | --- |" not in stripped
    # 表格外散文逐字保留
    assert stripped.startswith("咱们施工。")
    assert stripped.endswith("尾段。\n")


def test_strip_fences_code_only_keeps_tables():
    text = "前言。\n```mermaid\nA-->B\n```\n| 编号 | 值 |\n| --- | --- |\n| x | y |\n"
    stripped = _strip_fences(text, include_tables=False)
    assert "```mermaid" not in stripped        # 代码块仍被剔除
    assert "A-->B" not in stripped
    assert "| 编号 | 值 |" in stripped          # 表格行保留
    assert stripped.startswith("前言。\n")


def test_strip_fences_no_fence_no_table_returns_unchanged():
    text = "纯散文段落，没有任何围栏也没有表格。"
    assert _strip_fences(text) is text
    assert _strip_fences(text, include_tables=False) is text


def test_strip_fences_fence_only_still_strips_fences():
    """无表格、只有围栏：include_tables=False 仍须剔除围栏（不可因早退而漏）。"""
    text = "前言。\n```mermaid\nA-->B\n```\n"
    stripped = _strip_fences(text, include_tables=False)
    assert stripped == "前言。\n\n"
    assert "```mermaid" not in stripped


def test_protected_ranges_code_only_omits_tables():
    text = "a\n```mermaid\nx\n```\n| h1 | h2 |\n| --- | --- |\n| v1 | v2 |\n"
    assert _protected_ranges(text), "围栏必须仍受保护"
    ranges_no_tbl = _protected_ranges(text, include_tables=False)
    ranges_full = _protected_ranges(text)
    tbl_pos = text.index("| v1 | v2 |")
    assert not any(s <= tbl_pos < e for s, e in ranges_no_tbl)
    assert any(s <= tbl_pos < e for s, e in ranges_full)
    # 围栏区间两种形态下都在
    code_pos = text.index("x")
    assert any(s <= code_pos < e for s, e in ranges_no_tbl)
    assert any(s <= code_pos < e for s, e in ranges_full)


def test_quality_issues_empty_input():
    assert quality_issues("") == {"colloquial_hits": [], "abolished_standards": []}
    assert quality_issues(None) == {"colloquial_hits": [], "abolished_standards": []}


def test_colloquial_audit_path_unchanged_by_fix():
    """口语化判据的行为必须与修复前完全一致（默认 include_tables=True）。"""
    text = "咱们搞定它。\n```mermaid\nA[咱们]-->B\n```\n"
    hits = find_colloquial_hits(text)
    assert set(hits) == {"咱们", "搞定"}   # 仅正文命中，围栏内不计
    assert sanitize_ai_content(text).count("施工项目部") == 1


def test_find_abolished_codes_still_scans_raw_text():
    """底层判据的契约不变：仍是「扫传入文本」，围栏保护由调用方负责。"""
    assert find_abolished_codes("本工程临时用电按 JGJ 46-2005 执行") == ["JGJ 46-2005"]
    assert find_abolished_codes("按 JGJ/T 46-2024 执行") == []
