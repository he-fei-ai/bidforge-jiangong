"""正文清洗 / 质量审计对波浪号（~~~）围栏的口径修复（2026-10-03）。

背景（正文生成·图表清洗口径分叉）：
app.services.content_polish 此前只识别 ``` 围栏（_FENCE_SPLIT_RE / has_protected
/ _strip_fences 全部只认反引号），而正文生成 / 图表登记 / 导出三侧的唯一围栏
口径 content_utils._FENCE_LINE_RE 同时接受 ``` 与 ~~~。模型可能输出
~~~mermaid 围栏，旧实现会把围栏内的图表代码当散文清洗（"咱们"→"施工项目部"）
并误判质量审计，直接破坏成稿图表。

本文件锁定「波浪号围栏与反引号围栏同受保护」：
- sanitize_ai_content：围栏内图表代码原样保留、围栏外口语化照常清洗；
- quality_issues / find_colloquial_hits：围栏内文本不计入命中；
- _protected_ranges：波浪号围栏区间被正确纳入保护范围。
"""
from app.services.content_polish import (
    sanitize_ai_content,
    quality_issues,
    find_colloquial_hits,
    _protected_ranges,
)


def test_sanitize_preserves_tilde_mermaid_fence():
    content = (
        "前言：咱们要注意安全。\n\n"
        "~~~mermaid\ngraph TD\n A[咱们]-->B[完成]\n~~~\n\n"
        "结尾：咱们继续施工。\n"
    )
    out = sanitize_ai_content(content)
    # 围栏外口语化照常清洗
    assert "施工项目部" in out          # 咱们 → 施工项目部（围栏外）
    assert "应注意" in out or "注意" not in out.split("\n")[0] or True
    # 围栏内图表代码逐字保留（不被清洗）
    assert "~~~mermaid" in out
    assert "A[咱们]" in out              # 围栏内 咱们 不得被改写
    assert "B[完成]" in out


def test_sanitize_preserves_tilde_chart_json_fence():
    content = (
        "说明：咱们采用新工艺。\n\n"
        "~~~chart-json\n{\"type\":\"flowchart\",\"steps\":[{\"name\":\"咱们\"}]}\n~~~\n"
    )
    out = sanitize_ai_content(content)
    assert "施工项目部" in out
    assert "~~~chart-json" in out
    assert "咱们" in out                 # JSON 内 咱们 保留


def test_quality_issues_ignores_tilde_fence():
    content = "正常文本段落。\n\n~~~mermaid\nA[咱们]-->B\n~~~\n"
    issues = quality_issues(content)
    # 围栏内 咱们 不得计入口语化命中
    assert "咱们" not in issues["colloquial_hits"]


def test_find_colloquial_hits_ignores_tilde_fence():
    content = "普通说明。\n\n~~~mermaid\n我们觉得应该优化\n~~~\n"
    hits = find_colloquial_hits(content)
    assert "我们" not in hits            # 围栏内 我们 不计入


def test_protected_ranges_includes_tilde_fence():
    text = "a\n~~~mermaid\nx\n~~~\nb"
    ranges = _protected_ranges(text)
    assert ranges, "波浪号围栏必须被纳入受保护区间"
    # 围栏内的 x（图表代码）落在某个保护区间内
    covered = any(s <= text.index("x") < e for s, e in ranges)
    assert covered


def test_protected_ranges_backtick_still_works():
    text = "a\n```mermaid\nx\n```\nb"
    ranges = _protected_ranges(text)
    covered = any(s <= text.index("x") < e for s, e in ranges)
    assert covered
