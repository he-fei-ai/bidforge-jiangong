"""导出侧围栏检测「三侧口径统一」回归（2026-09-24）。

背景
----
export._parse_content_blocks 与 _count_unclosed_chart_fences 此前用
``startswith("```") + line.strip()[3:]`` 提取语言标签，与登记/改写侧
``_chart_pipeline.parse_fence_line`` 的修复口径分叉：

  · 4 反引号围栏 ````mermaid```` → lang 变成 "`` `mermaid`` `" → 不被识别为 mermaid
    → 落到通用 code 分支 → **Mermaid 源码原样印进交付 DOCX**（既不渲染成图、
    也不进图表清单/预检），正是 _chart_pipeline 注释反复强调要杜绝的
    「幽灵图 / 三侧口径分叉」；
  · 波浪号围栏 ~~~mermaid 完全检测不到（startswith("```") 为 False）→
    围栏内容被当普通段落解析、源码泄漏进正文。

本次改用 parse_fence_line 后登记/改写/导出三侧同口径。本文件锁定该修复不回归。
"""
from __future__ import annotations

from app.routers.export import _count_unclosed_chart_fences, _parse_content_blocks


def _blocks_of(content: str, btype: str):
    return [b for b in _parse_content_blocks(content) if b.get("type") == btype]


def test_four_backtick_mermaid_parsed_as_chart():
    """4 反引号围栏 ````mermaid 必须识别为图表块（而非通用 code 块泄漏源码）。"""
    content = "````mermaid\ngraph TD\n A-->B\n````"
    charts = _blocks_of(content, "chart")
    assert len(charts) == 1, f"应识别 1 个图表块，实际 {charts}"
    assert charts[0]["chart_type"] == "flowchart"
    assert "A-->B" in charts[0]["code"]
    # 旧 bug：lang="`mermaid" → 落通用 code 分支 → 源码印进 DOCX，必须不再发生
    assert not _blocks_of(content, "code"), "4 反引号 mermaid 围栏不应落到通用 code 分支"


def test_tilde_mermaid_fence_parsed_as_chart():
    """波浪号围栏 ~~~mermaid 必须被检测到并识别为图表块。"""
    content = "~~~mermaid\ngraph TD\n A-->B\n~~~"
    charts = _blocks_of(content, "chart")
    assert len(charts) == 1, f"波浪号围栏应识别为图表块，实际 {charts}"
    assert charts[0]["chart_type"] == "flowchart"
    assert "A-->B" in charts[0]["code"]


def test_four_backtick_python_still_code_block():
    """4 反引号普通代码围栏（非图表）仍按 code 块输出，lang 不带多余反引号。"""
    content = "````python\nprint(1)\n````"
    codes = _blocks_of(content, "code")
    assert len(codes) == 1
    assert codes[0]["lang"] == "python", f"lang 应为 python，实际 {codes[0]['lang']!r}"


def test_tilde_chart_json_parsed_as_chart():
    """波浪号 chart-json 围栏同样必须被识别（数据型图表不应因围栏字符而丢失）。"""
    content = '~~~chart-json\n{"type":"timeline","title":"里程碑","milestones":[]}\n~~~'
    charts = _blocks_of(content, "chart")
    # timeline 为可渲染类型；即便载荷为空，至少应被解析为 chart 块而非段落泄漏
    assert len(charts) == 1, f"波浪号 chart-json 应识别为图表块，实际 {charts}"


def test_three_backtick_behavior_unchanged():
    """3 反引号围栏行为不变（回归保护，确保修复不影响主路径）。"""
    content = "```mermaid\ngraph TD\n A-->B\n```"
    charts = _blocks_of(content, "chart")
    assert len(charts) == 1
    assert charts[0]["chart_type"] == "flowchart"


def test_count_unclosed_tilde_fence():
    """未闭合的波浪号围栏应被计数（旧实现漏检 ~~~）。"""
    assert _count_unclosed_chart_fences("前言\n~~~mermaid\ngraph TD") == 1


def test_count_unclosed_four_backtick():
    """未闭合的 4 反引号围栏应被计数。"""
    assert _count_unclosed_chart_fences("````mermaid\ngraph TD") == 1


def test_count_closed_four_backtick_mixed_close_not_unclosed():
    """4 反引号开 + 3 反引号闭的错配不应被判为未闭合（同字符即闭合，与
    read_fenced_block 的宽松闭合判据一致，避免合法图表被判未闭合而丢弃）。"""
    assert _count_unclosed_chart_fences("````mermaid\ngraph TD\n A-->B\n```") == 0


def test_count_closed_tilde_fence():
    """闭合的波浪号围栏计数为 0。"""
    assert _count_unclosed_chart_fences("~~~mermaid\ngraph TD\n A-->B\n~~~") == 0
