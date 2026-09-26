# -*- coding: utf-8 -*-
"""内容自动改写服务单测（v15，config.auto_rewrite_content 开关背后）。

锁定三条不变量：
1. HTML/剪贴板表格 → 合法 GFM 表格（畸形无 <tr> 结构原样保留）；
2. 英文串写只在孤立小写词处替换，绝不误伤标准编号（Q235B/C20）；
3. 所有改写只在代码围栏外进行，chart-json/mermaid 载荷不被破坏。
"""
import pytest


# ---------------------------------------------------------------------------
# ① HTML 表格 → GFM
# ---------------------------------------------------------------------------

def test_html_table_converts_to_gfm():
    from app.services.content_rewrite import html_table_to_gfm
    html_src = ("<table><tr><th>参数</th><th>值</th></tr>"
                "<tr><td>立杆纵距</td><td>1.5m</td></tr></table>")
    out, n = html_table_to_gfm(html_src)
    assert n == 1
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines[0] == "| 参数 | 值 |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| 立杆纵距 | 1.5m |"


def test_html_table_cell_noise_and_pipe_cleaned():
    from app.services.content_rewrite import html_table_to_gfm
    # 单元格含 <br>、剪贴板标记与竖线 → 清洗且不破坏列结构
    html_src = "<table><tr><td>a<br>b</td><td>x|y</td></tr></table>"
    out, n = html_table_to_gfm(html_src)
    assert n == 1
    row = [ln for ln in out.splitlines() if ln.startswith("| a")][0]
    assert "<br>" not in row and "／" in row  # 竖线转全角斜杠


def test_html_table_without_rows_is_left_untouched():
    from app.services.content_rewrite import html_table_to_gfm
    src = "<table>没有 tr 的畸形内容</table>"
    out, n = html_table_to_gfm(src)
    assert n == 0 and out == src  # 交回 v14 扁平化兜底


def test_html_table_pads_ragged_rows():
    from app.services.content_rewrite import html_table_to_gfm
    src = ("<table><tr><td>a</td><td>b</td><td>c</td></tr>"
           "<tr><td>x</td></tr></table>")
    out, n = html_table_to_gfm(src)
    body = [ln for ln in out.splitlines() if ln.startswith("| x")]
    assert body[0] == "| x |  |  |"  # 补齐到 3 列


# ---------------------------------------------------------------------------
# ② 英文串写替换
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("src,expect", [
    ("严禁使用 brick 等易碎材料", "严禁使用 砖料 等易碎材料"),
    ("棚顶 material 应采用双层脚手板", "棚顶 材料 应采用双层脚手板"),
])
def test_fix_english_leak_replaces_artifacts(src, expect):
    from app.services.content_rewrite import fix_english_leak
    out, n = fix_english_leak(src)
    assert n == 1 and out == expect


def test_fix_english_leak_does_not_touch_codes():
    """标准/材质编号里的字母不得被替换（Q235B、C20、HPB300）。"""
    from app.services.content_rewrite import fix_english_leak
    src = "钢管 Q235B，混凝土 C20，锚筋 HPB300，材质 material 合格"
    out, n = fix_english_leak(src)
    assert "Q235B" in out and "C20" in out and "HPB300" in out
    assert n == 1  # 只替换孤立小写 material


# ---------------------------------------------------------------------------
# ③ 相邻重复块合并
# ---------------------------------------------------------------------------

def test_collapse_duplicate_adjacent_lines():
    from app.services.content_rewrite import collapse_duplicate_blocks
    src = "## 3.6 技术保证条件\n## 3.6 技术保证条件\n正文不同行\n另一行"
    out, removed = collapse_duplicate_blocks(src)
    assert removed == 1
    assert out.count("## 3.6 技术保证条件") == 1


def test_collapse_keeps_distinct_lines():
    from app.services.content_rewrite import collapse_duplicate_blocks
    src = "检查架体变形\n检查连墙件\n检查脚手板"
    out, removed = collapse_duplicate_blocks(src)
    assert removed == 0 and out == src


# ---------------------------------------------------------------------------
# ④ 编排 + 代码围栏保护（最关键的安全不变量）
# ---------------------------------------------------------------------------

def test_normalize_protects_chart_json_fence():
    """chart-json 载荷里的键名/英文绝不能被改写，否则渲染失败。"""
    from app.services.content_rewrite import normalize_section_content
    payload = '{"type":"labor","materials":[{"name":"普工"}],"title":"材料计划"}'
    content = f"劳动力配置如下：\n\n```chart-json\n{payload}\n```\n"
    out, stats = normalize_section_content(content)
    assert payload in out, "围栏内 JSON 必须逐字保留"
    assert stats["english"] == 0, "围栏内的 materials 不应被替换"


def test_normalize_protects_html_table_inside_fence():
    from app.services.content_rewrite import normalize_section_content
    fenced = "<table><tr><td>a</td></tr></table>"
    content = f"代码示例：\n\n```html\n{fenced}\n```\n"
    out, stats = normalize_section_content(content)
    assert fenced in out and stats["tables"] == 0


def test_normalize_end_to_end_outside_fences():
    from app.services.content_rewrite import normalize_section_content
    content = ("搭设参数如下：\n\n"
               "<table><tr><td>项目</td><td>值</td></tr>"
               "<tr><td>严禁使用 brick</td><td>1.5</td></tr></table>\n\n"
               "## 3.6 技术保证条件\n## 3.6 技术保证条件\n")
    out, stats = normalize_section_content(content)
    assert stats["tables"] == 1 and stats["english"] == 1 and stats["dupes"] == 1
    assert "| 严禁使用 砖料 | 1.5 |" in out
    assert out.count("## 3.6 技术保证条件") == 1
