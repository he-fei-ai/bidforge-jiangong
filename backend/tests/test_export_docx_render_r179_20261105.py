"""R179（2026-11-05）导出 DOCX 成稿格式缺陷回归测试。

覆盖《机电安装施工组织设计20261104第1轮.docx》格式问题分析报告中的 16 类缺陷，
逐条锁定「入口/终稿清洗 + 渲染层结构 + 出口残留闸门」三层收口行为：

* 内容清洗：PDF 分页标记/页脚、双重 HTML 实体、LaTeX 残留、千分号、兆欧符号、
  中英误植词、标准编号、括号/破折号/单位空格；
* 渲染结构：粘连标题拆分、管道符段落转原生表格、表头行跨页重复、表题在表上；
* 出口闸门：表格单元格 / 页眉页脚纳入扫描；无法消除的残留计入
  ``lint_residual_total`` 并触发 ``X-Export-Degraded: content-residue``。

每条清洗规则都配正反例；闸门配「关闭清理必须变红」的变异验证。
"""

import re

import app.routers.export as E
import pytest
from docx import Document
from docx.oxml.ns import qn


# ---------------------------------------------------------------------------
# 一、交付文本规范化（正例 + 反例 + 幂等）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "src,expected",
    [
        # LaTeX → 普通符号（渲染期，公式跨度之外）
        (r"\mathrm{m}^2", "m²"),
        (r"^{\circ}C", "℃"),
        (r"30\leq 40", "30≤40"),
        (r"\Omega", "Ω"),
        (r"\Phi", "Φ"),
        (r"80\%", "80%"),
        (r"0.6\sim 0.8", "0.6～0.8"),
        # 双重 HTML 实体
        ("&amp;gt; 0.8", "> 0.8"),
        ("&amp;amp;lt; 0.5", "< 0.5"),
        # 千分号 / 兆欧符号
        ("1.5% 0", "1.5‰"),
        ("0.5MQ", "0.5MΩ"),
        # 中英混排误植词
        ("末端devices", "末端设备"),
        ("风管fabrication", "风管制作"),
        ("焊接ventilation", "焊接通风"),
        ("白银 spots", "白银斑点"),
        # 标准编号口径
        ("GB50303- 2015", "GB 50303-2015"),
        ("gb/t50303 - 2015", "GB/T 50303-2015"),
        # 括号 / 破折号 / 单位空格
        ("(GB 50303-2015)", "（GB 50303-2015）"),
        ("施工工艺-给水", "施工工艺—给水"),
        ("30 mA", "30mA"),
        # 已知错词
        ("力矩矩", "力矩"),
        ("峻工", "竣工"),
    ],
)
def test_normalize_delivery_text(src, expected):
    assert E._normalize_delivery_text(src) == expected


@pytest.mark.parametrize(
    "src",
    [
        "统一",  # 「一」形破折号无法与正常词区分 → 不得替换
        "GB 50303-2015",  # 已规范的标准号 → 保持
        "f(x)",  # 纯 ASCII 公式形态 → 括号不得全角化
        "f(x+1)",
        "1-2",  # 数字区间 → 不得变破折号
        "A-B",
    ],
)
def test_normalize_delivery_text_negative(src):
    assert E._normalize_delivery_text(src) == src


def test_normalize_delivery_text_idempotent():
    src = "1.5% 0 / 0.5MQ / &amp;gt; / 30 mA / GB50303- 2015"
    once = E._normalize_delivery_text(src)
    assert E._normalize_delivery_text(once) == once


def test_normalize_delivery_text_keeps_formula_for_omml():
    """渲染期公式跨度必须原样保留，交给 OMML 转换器（不得提前拆散）。"""
    src = r"面积 \(3447.42 \mathrm{m}^2\)"
    assert E._normalize_delivery_text(src) == src


# ---------------------------------------------------------------------------
# 二、落盘前 lint 闸门：覆盖正文 / 表格单元格 / 页眉页脚
# ---------------------------------------------------------------------------
def test_final_lint_covers_table_cells_and_footer():
    d = Document()
    d.add_paragraph("===== Page 1 =====")
    d.add_paragraph("## 1 工程概况")
    d.add_paragraph("&amp;gt; 0.8")
    d.add_paragraph(r"\(1.5\% 0\)")
    table = d.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "<td>规格</td>"
    d.sections[0].footer.paragraphs[0].text = "第 3 页 共 10 页"

    stats = E._docx_final_lint(d)

    assert stats["lint_page_mark"] >= 2  # 正文 + 页脚
    assert stats["lint_html_tag"] >= 1  # 表格单元格
    assert stats["lint_md_residue"] == 1
    assert stats["lint_entity_residue"] >= 1
    assert stats["lint_latex_residue"] >= 1
    # 自动还原后不应再残留
    assert stats["lint_residual_page"] == 0
    assert stats["lint_residual_html"] == 0
    assert stats["lint_residual_entity"] == 0
    assert stats["lint_residual_latex"] == 0
    assert "Page" not in d.paragraphs[0].text
    assert d.paragraphs[1].text == "1 工程概况"
    assert d.paragraphs[2].text.startswith(">")
    assert d.paragraphs[3].text == "1.5‰"
    assert table.cell(0, 0).text == "规格"
    assert "页" not in d.sections[0].footer.paragraphs[0].text


def test_final_lint_residual_counts_unfixable_pipe_and_duplicate_table_no():
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    d = Document()
    d.add_paragraph("| a | b |")
    for _ in range(2):
        p = d.add_paragraph("表4-1 主要设备表")
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    stats = E._docx_final_lint(d)

    assert stats["lint_residual_pipe"] >= 1
    assert stats["lint_duplicate_table_no"] == 1
    assert stats["lint_residual_total"] >= 2


def test_final_lint_ignores_body_table_references():
    """正文里的「见表 4-1」重复引用不是表号重复缺陷。"""
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    d = Document()
    d.add_paragraph("详见表4-1。")
    d.add_paragraph("见表4-1。")
    p = d.add_paragraph("表4-1 主要设备表")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    stats = E._docx_final_lint(d)
    assert stats["lint_duplicate_table_no"] == 0
    assert stats["lint_residual_total"] == 0


def test_final_lint_skips_code_style():
    from docx.enum.style import WD_STYLE_TYPE

    d = Document()
    code_style = d.styles.add_style("Code Block", WD_STYLE_TYPE.PARAGRAPH)
    p = d.add_paragraph("## title | a | b |")
    p.style = code_style

    stats = E._docx_final_lint(d)
    # 代码段不参与 Markdown / 管道符清洗
    assert stats["lint_md_residue"] == 0
    assert stats["lint_residual_pipe"] == 0


def test_final_lint_mutation_without_normalizer(monkeypatch):
    """变异验证：关闭规范化后，双重实体与 LaTeX 必须变成可见残留。"""
    d = Document()
    d.add_paragraph("&amp;gt; 0.8")
    d.add_paragraph(r"\(1.5\% 0\)")
    monkeypatch.setattr(E, "_normalize_delivery_text", lambda text, **kw: text)

    stats = E._docx_final_lint(d)

    assert stats["lint_residual_entity"] >= 1
    assert stats["lint_residual_latex"] >= 1
    assert stats["lint_residual_total"] >= 2
    assert E._export_content_residue(stats) >= 2


def test_export_content_residue_zero_for_legacy_stats():
    assert E._export_content_residue(None) == 0
    assert E._export_content_residue({}) == 0
    assert E._export_content_residue({"lint_page_mark": 3}) == 0


# ---------------------------------------------------------------------------
# 三、渲染前结构收口（不改变正常块）
# ---------------------------------------------------------------------------
def test_render_blocks_split_adhered_list_item():
    blocks = [{"type": "list_item", "num": 1, "text": "国家法律国家法律是工程建设的根本遵循"}]
    out = E._normalize_render_blocks(blocks)
    assert out[0] == {"type": "list_item", "num": 1, "text": "国家法律"}
    assert out[1] == {"type": "paragraph", "text": "是工程建设的根本遵循"}


def test_render_blocks_strip_duplicated_letter_ordinal():
    blocks = [{"type": "list_item", "text": "a、a、吊顶施工准备"}]
    out = E._normalize_render_blocks(blocks)
    assert out == [{"type": "list_item", "text": "吊顶施工准备"}]


def test_render_blocks_keep_numeric_content():
    # 数字开头的正文不得被当成序号剥离（防「1.5 倍」误伤）
    blocks = [{"type": "list_item", "text": "1.5 倍安全系数复核"}]
    out = E._normalize_render_blocks(blocks)
    assert out[0]["text"] == "1.5 倍安全系数复核"


def test_render_blocks_merge_caption_pipe_with_following_table():
    blocks = [
        {"type": "paragraph", "text": "表4-1 主要施工机械设备表 | 序号 | 设备名称 | 数量 |"},
        {
            "type": "table",
            "lines": [
                "| 1 | 交流弧焊机 | 2台 |",
                "| --- | --- | --- |",
                "| 2 | 砂轮切割机 | 1台 |",
            ],
        },
    ]
    out = E._normalize_render_blocks(blocks)
    assert len(out) == 1
    assert out[0]["type"] == "table"
    assert out[0]["caption"] == "表4-1 主要施工机械设备表"
    assert out[0]["lines"][0] == "| 序号 | 设备名称 | 数量 |"
    assert out[0]["lines"][2] == "| 1 | 交流弧焊机 | 2台 |"


def test_render_blocks_consecutive_pipe_rows_to_native_table():
    blocks = [
        {"type": "paragraph", "text": "表4-2 劳动力计划表"},
        {"type": "paragraph", "text": "| 工种 | 人数 |"},
        {"type": "paragraph", "text": "| 电工 | 8 |"},
    ]
    out = E._normalize_render_blocks(blocks)
    assert len(out) == 1
    assert out[0]["type"] == "table"
    assert out[0]["caption"] == "表4-2 劳动力计划表"
    assert out[0]["lines"][0] == "| 工种 | 人数 |"
    assert out[0]["lines"][2] == "| 电工 | 8 |"


def test_render_blocks_noop_for_clean_content():
    blocks = [
        {"type": "heading", "text": "1.1 工程概况"},
        {"type": "paragraph", "text": "本工程为机电安装工程。"},
        {"type": "table", "lines": ["| a | b |", "| --- | --- |", "| 1 | 2 |"]},
    ]
    out = E._normalize_render_blocks(blocks)
    assert out == blocks
    # 不得原地改写调用方数据
    assert blocks[0]["text"] == "1.1 工程概况"


# ---------------------------------------------------------------------------
# 四、端到端：缺陷内容 → 成稿无残留 / 表格结构正确
# ---------------------------------------------------------------------------
_DEFECT_CONTENT = """## 1 工程概况
===== Page 2 =====
1）国家法律国家法律是工程建设的根本遵循。
1.5% 0 与 0.5MQ，面积 \\(3447.42 \\mathrm{m}^2\\)。
&amp;gt; 0.8 的阈值。
表4-1 主要施工机械设备表 | 序号 | 设备名称 | 数量 |
| 1 | 交流弧焊机 | 2台 |
| 2 | 砂轮切割机 | 1台 |
"""


def _build(tmp_path, content=_DEFECT_CONTENT):
    blocks = E._parse_content_blocks(content)
    roots = [
        {
            "id": "s1",
            "parent_id": "",
            "level": "1",
            "sort_order": 1,
            "title": "章",
            "content": content,
        }
    ]
    prep = {
        "scheme": {"name": "S", "project_id": "p"},
        "roots": roots,
        "children_map": {},
        "chart_lookup": {},
        "rendered_bytes": {},
        "blocks_cache": {"s1": blocks},
        "heading_styles": E._load_heading_styles({}),
        "image_bytes": {},
        "global_facts": [],
        "docx_options": {
            "font_name": "宋体",
            "font_size": 12.0,
            "page_header": "",
            "page_footer": "",
            "show_page_number": True,
            "show_title_page": False,
            "show_toc": False,
            "bidder_name": "",
            "page_break_before_chapter": True,
            "line_spacing": 1.15,
            "page_number_style": "simple",
            "toc_depth": 3,
        },
    }
    out = tmp_path / "r179.docx"
    stats = E._build_docx_sync(*E._build_docx_task(str(out), prep))
    return out, stats


def test_build_docx_clears_all_residue(tmp_path):
    out, stats = _build(tmp_path)
    assert stats["lint_residual_total"] == 0
    d = Document(str(out))

    texts = [p.text for p in d.paragraphs]
    joined = "\n".join(texts)
    assert "===== Page" not in joined
    assert "&amp;" not in joined
    assert "\\mathrm" not in joined
    assert "\\leq" not in joined
    assert not any(t.startswith("##") for t in texts)
    assert "1.5‰" in joined and "0.5MΩ" in joined
    assert "> 0.8" in joined


def test_build_docx_heading_list_and_table_structure(tmp_path):
    out, _ = _build(tmp_path)
    d = Document(str(out))

    assert any(p.style.name == "Heading 1" for p in d.paragraphs)
    # 粘连列表项被拆成「短标题 + 正文」
    texts = [p.text for p in d.paragraphs]
    assert "1）国家法律" in texts
    assert "是工程建设的根本遵循。" in texts

    assert len(d.tables) == 1
    tbl = d.tables[0]
    tr_pr = tbl.rows[0]._tr.trPr
    assert tr_pr is not None and tr_pr.find(qn("w:tblHeader")) is not None
    assert [c.text for c in tbl.rows[0].cells] == ["序号", "设备名称", "数量"]
    # 表题在表上（紧邻表格的前一个 body 元素是表题段落）
    body = list(d.element.body)
    idx = body.index(tbl._tbl)
    assert "主要施工机械设备表" in "".join(body[idx - 1].itertext())

    # LaTeX 已转 Word 原生公式（OMML），不是 `$...$` 文本
    xml = d.element.xml
    assert "m:oMath" in xml
    assert not re.search(r"\\mathrm|\\leq|\\\(|\\\)", xml)


def test_build_docx_pipe_table_is_native(tmp_path):
    content = "表4-2 劳动力计划表\n| 工种 | 人数 |\n| 电工 | 8 |\n"
    out, stats = _build(tmp_path, content)
    assert stats["lint_residual_total"] == 0
    d = Document(str(out))
    assert d.tables
    assert [c.text for c in d.tables[0].rows[0].cells] == ["工种", "人数"]
