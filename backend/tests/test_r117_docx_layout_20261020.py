"""R117 · 成稿版式收口 · 第二批（2026-10-20）。

【取证来源】`室外汽车吊吊装施工专项方案_20261020_第1轮.docx`（1023 段 / 32 表 /
195 表格行 / 2 内联图 / 81 个 oMath / 目录域 1 个）。

第一批（``test_r117_docx_closeout_20261020.py``）收口**解析层**：```markdown 围栏、
加粗枚举伪标题、有序列表跨块续号、自动表题清洗。本文件收口**版式层**，逐项对应
导出稿里可直接量到的字节事实：

1. 页面 `sectPr` = **21.59×27.94cm（US Letter）** —— python-docx 默认模板，
   中国工程报审件应为 A4 21×29.7cm；
2. 标题样式：H1~H4 全 **14pt**、H2/H3/H4 同一主题蓝 `#4F81BD`，且带
   `<w14:textFill>`（Word 2010+ 优先于 `w:color`）⇒ 四级标题在纸面上无法分辨，
   用户也没有颜色开关；
3. 图题 `keep_with_next=None`（表题侧却是 True）⇒ 分页可把图与图题拆到两页；
4. 32 张表 / 195 行**无一行** `w:cantSplit` ⇒ 长表翻页把同一行劈成两半。

⚠️ 刻意**不**在本轮改动的两项（报告里给出方案，见
``docs/正文导出成稿问题报告_R117_20261020.md``）：
- 章节编号 L2 每章重排（``heading_v2._format_number`` 的 L1 第一章 / L2 "1" /
  L3 "1.1"）—— 改成「章号.节号」会改变全文目录与交叉引用，且与
  ``_detect_duplicate_sections`` 的「章作用域判重」语义绑定；
- TOC 域深度维持用户配置 —— 导出稿 H5/H6 共 76 段实际是「节内 body 命名空间」
  伪标题（``1）、`` / ``X.Y.Z.W、``），自动抬深度会把它们全部拉进目录。
"""
import os
import tempfile

import pytest
from app.routers.export import (
    _DEFAULT_HEADING_STYLES,
    _EXPORTER_VERSION,
    _FIX_STATS_KEYS,
    _NEAR_DUP_PREFIX_CHARS,
    _W14_NS,
    _WS_ONLY_RE,
    _build_docx_sync,
    _collapse_repeated_blocks,
    _fix_stats,
    _insert_image_with_caption,
    _load_heading_styles,
    _report_near_duplicate_blocks,
    _set_row_cant_split,
)
from app.services.content_blocks import _parse_content_blocks
from docx import Document
from docx.shared import Cm, RGBColor

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

TABLE_MD = (
    "正文段落。\n\n"
    "| 序号 | 控制项目 | 允许偏差 |\n"
    "| --- | --- | --- |\n"
    "| 1 | 垂直度 | 1/1000 |\n"
    "| 2 | 标高 | ±5mm |\n"
    "| 3 | 平面位置 | ±10mm |\n"
)


def _build(content: str, *, chapter: str = "第一章 测试章节") -> Document:
    """用导出单章节入口生成 docx 并返回 Document（与第一批同口径）。"""
    sections = [{
        "id": "c1", "parent_id": "", "sort_order": 0, "level": 1,
        "title": chapter, "content": content,
    }]
    blocks = {"c1": _parse_content_blocks(content)}
    out = os.path.join(tempfile.gettempdir(), "r117_layout.docx")
    _build_docx_sync(
        out, {"id": "s1", "project_id": "p1", "name": "测试方案"},
        sections, {}, {}, {}, blocks,
        "宋体", 12, "", "", False, False, False,
        bidder_name="",
        heading_styles=_load_heading_styles({}),
        page_break_before_chapter=True,
        line_spacing=1.5,
        page_number_style="simple",
        toc_depth=3,
        margins=None, cover_info=None,
    )
    return Document(out)


def _png_bytes():
    """生成最小合法 PNG（40×30），供 ``_insert_image_with_caption`` 使用。"""
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (40, 30), (200, 200, 200)).save(buf, format="PNG")
    buf.seek(0)
    return buf


# ============================================================
# A 组：页面与标题版式（导出稿可直接量到的 5 类字节事实）
# ============================================================
class TestPageAndHeadingLayout:
    def test_page_is_a4(self):
        """旧实现沿用 python-docx 默认模板 US Letter（21.59×27.94cm）。"""
        doc = _build("正文段落。\n")
        sec = doc.sections[0]
        assert abs(sec.page_width - Cm(21.0)) < Cm(0.02), sec.page_width
        assert abs(sec.page_height - Cm(29.7)) < Cm(0.02), sec.page_height
        # 反向锚：绝不能还是 Letter（21.59 / 27.94）
        assert abs(sec.page_width - Cm(21.59)) > Cm(0.1)

    def test_heading_size_ladder(self):
        """1~4 级旧默认同为 14pt ⇒ 成稿里四级标题同大。新阶梯 16/14/14/12。"""
        doc = _build("正文段落。\n")
        sizes = [doc.styles[f"Heading {lvl}"].font.size.pt for lvl in range(1, 8)]
        assert sizes == [16.0, 14.0, 14.0, 12.0, 12.0, 12.0, 12.0], sizes
        # 反向锚：H1 必须大于 H2（旧实现 14 == 14）
        assert sizes[0] > sizes[1]
        # 反向锚：H4 必须小于 H1（旧实现 14 == 14）
        assert sizes[3] < sizes[0]

    def test_heading_color_is_black(self):
        """旧实现从不写 w:color，成稿继承模板主题蓝 376092 / 4F81BD。"""
        doc = _build("正文段落。\n")
        for lvl in range(1, 5):
            color = doc.styles[f"Heading {lvl}"].font.color.rgb
            assert color == RGBColor(0, 0, 0), (lvl, color)

    def test_theme_text_fill_removed(self):
        """``<w14:textFill>`` 在 Word 2010+ 优先于 ``w:color`` —— 只设 rgb 不够。"""
        doc = _build("正文段落。\n")
        for lvl in range(1, 8):
            el = doc.styles[f"Heading {lvl}"].element
            # textFill 挂在 w:rPr 下，必须用 .// 深查（直接 find 只找直接子元素）
            assert el.find(f".//{{{_W14_NS}}}textFill") is None, lvl

    def test_heading_space_before_ladder(self):
        """旧实现只有 12/8 两档；新阶梯 18/14/12/10/8。"""
        doc = _build("正文段落。\n")
        got = [doc.styles[f"Heading {lvl}"].paragraph_format.space_before.pt
               for lvl in range(1, 8)]
        assert got == [18.0, 14.0, 12.0, 10.0, 8.0, 8.0, 8.0], got

    def test_defaults_match_module_constant(self):
        """``_load_heading_styles({})`` 必须与模块默认值逐级一致（无配置时）。"""
        assert _DEFAULT_HEADING_STYLES[1]["font_size"] == 16
        assert _load_heading_styles({})[4]["font_size"] == 12

    def test_frontend_heading_defaults_aligned(self):
        """前端 initialValues 必须与后端默认阶梯一致，否则"用户没改过"也是旧值。"""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            "frontend", "src", "pages", "SchemeWorkbenchPage.tsx")
        if not os.path.exists(path):
            pytest.skip("前端源码不在当前检出中")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        assert '"1": { font_name: "宋体", font_size: 16, bold: true }' in src
        assert '"4": { font_name: "宋体", font_size: 12, bold: true }' in src
        # 反向锚：旧的 4 级 14pt 写法不得残留
        assert '"4": { font_name: "宋体", font_size: 14, bold: true }' not in src


# ============================================================
# B 组：图与图题同页（导出稿 2 张图的图题 keep_with_next=None）
# ============================================================
class TestFigureCaptionKeptTogether:
    def test_picture_keeps_with_next(self):
        doc = Document()
        assert _insert_image_with_caption(doc, _png_bytes(), "图 1-1 测试图") is True
        pic_p, cap_p = doc.paragraphs[-2], doc.paragraphs[-1]
        assert pic_p.paragraph_format.keep_with_next is True
        assert cap_p.text == "图 1-1 测试图"

    def test_picture_and_caption_keep_together(self):
        doc = Document()
        _insert_image_with_caption(doc, _png_bytes(), "图 1-2 测试图")
        pic_p, cap_p = doc.paragraphs[-2], doc.paragraphs[-1]
        assert pic_p.paragraph_format.keep_together is True
        assert cap_p.paragraph_format.keep_together is True
        # 图题仍居中、首行缩进为 0（既有版式契约不得被本次改动破坏）
        assert cap_p.paragraph_format.first_line_indent == Cm(0)


# ============================================================
# C 组：表格行禁止跨页断开（导出稿 195 行无一带 cantSplit）
# ============================================================
class TestTableRowCantSplit:
    def _tr_prs(self, doc):
        return [row._tr.find(_W + "trPr") for t in doc.tables for row in t.rows]

    def test_every_row_has_cant_split(self):
        doc = _build(TABLE_MD)
        assert doc.tables, "测试内容必须产出表格"
        tr_prs = self._tr_prs(doc)
        assert len(tr_prs) == 4, len(tr_prs)  # 表头 + 3 数据行
        for tr_pr in tr_prs:
            assert tr_pr is not None
            assert tr_pr.find(_W + "cantSplit") is not None

    def test_cant_split_precedes_tbl_header(self):
        """CT_TrPr 严格序列：cantSplit 必须在 tblHeader 之前（WPS 严格校验）。"""
        doc = _build(TABLE_MD)
        tr_pr = doc.tables[0].rows[0]._tr.find(_W + "trPr")
        tags = [child.tag for child in tr_pr]
        assert _W + "tblHeader" in tags, tags
        assert tags.index(_W + "cantSplit") < tags.index(_W + "tblHeader"), tags

    def test_idempotent(self):
        """重复调用不得堆出多个 w:cantSplit（CT_TrPr 只允许 0~1 个）。"""
        doc = _build(TABLE_MD)
        row = doc.tables[0].rows[1]
        _set_row_cant_split(row)
        _set_row_cant_split(row)
        tr_pr = row._tr.find(_W + "trPr")
        assert len(tr_pr.findall(_W + "cantSplit")) == 1

    def test_no_cant_split_leak_into_other_parts(self):
        """反向锚：普通段落不得被写入 cantSplit（只看表格行）。"""
        doc = _build(TABLE_MD)
        assert "cantSplit" not in doc.element.xml.split("<w:tbl>")[0]


# ============================================================
# C2 组：首行「通栏标题行」横向合并（导出稿 表 2-4 中招）
# ============================================================
class TestTableTitleBand:
    BAND_MD = (
        "正文段落。\n\n"
        "| 吊装作业关键文件核查清单 |  |  |\n"
        "| --- | --- | --- |\n"
        "| 文件类别 | 核查重点内容 | 核查责任人 |\n"
        "| 设计文件 | 施工图审查合格书 | 技术负责人 |\n"
        "| 地质报告 | 承载力特征值 | 安全员 |\n"
    )

    def _grid(self, doc):
        t = doc.tables[0]
        return [[c.text.strip() for c in r.cells] for r in t.rows]

    def test_first_row_merged_into_one_cell(self):
        doc = _build(self.BAND_MD)
        t = doc.tables[0]
        # 横向合并后首行 tc 数 < 列数（gridSpan）
        assert len(t.rows[0]._tr.tc_lst) == 1, len(t.rows[0]._tr.tc_lst)
        assert len(t.columns) == 3
        # 合并后同一个 tc 被 cells 三次返回 —— 文本自然也三处一致
        assert self._grid(doc)[0] == ["吊装作业关键文件核查清单"] * 3

    def test_band_has_no_extra_empty_paragraphs(self):
        """python-docx 的 merge 会把每格空段落串进来 —— 必须清掉，否则色带撑高。"""
        doc = _build(self.BAND_MD)
        band = doc.tables[0].rows[0].cells[0]
        assert len(band.paragraphs) == 1, [p.text for p in band.paragraphs]

    def test_header_repeat_moves_to_column_row(self):
        """重复行必须是**列名行**（第 2 行），不是标题带。"""
        doc = _build(self.BAND_MD)
        t = doc.tables[0]
        first = t.rows[0]._tr.find(_W + "trPr")
        second = t.rows[1]._tr.find(_W + "trPr")
        assert first is None or first.find(_W + "tblHeader") is None
        assert second is not None and second.find(_W + "tblHeader") is not None

    def test_normal_header_table_untouched(self):
        """反向锚：正常表头（多格有字）不得被合并。"""
        doc = _build(TABLE_MD)
        t = doc.tables[0]
        assert len(t.rows[0]._tr.tc_lst) == 3, len(t.rows[0]._tr.tc_lst)
        assert t.rows[0]._tr.find(_W + "trPr").find(_W + "tblHeader") is not None

    def test_single_data_column_table_not_merged(self):
        """第 2 行不足 2 格有字（如「最大吊重 | 」）⇒ 不是标题带，不合并。"""
        doc = _build(
            "正文。\n\n| 指标 | 说明 |\n| --- | --- |\n| 最大吊重 |  |\n")
        t = doc.tables[0]
        assert len(t.rows[0]._tr.tc_lst) == 2, len(t.rows[0]._tr.tc_lst)

    def test_band_width_spans_all_columns(self):
        """固定布局下合并单元格必须拿到全表宽，否则被压成首列宽。

        ✅ R127（2026-10-22）：不再断言「≥15cm」的魔数 —— 表格列宽已改为按
        当前节版心自适应（A4 + python-docx 默认 3.17cm 边距 ⇒ 版心 14.65cm），
        改为断言合并格宽 == 表宽（真正的「跨满全部列」语义）。
        """
        doc = _build(self.BAND_MD)
        t = doc.tables[0]
        band = t.rows[0].cells[0]
        tbl_w = t._tbl.tblPr.find(_W + "tblW")
        assert tbl_w is not None
        table_cm = int(tbl_w.get(_W + "w")) / 567
        band_cm = band.width / 360000
        assert band.width is not None and abs(band_cm - table_cm) < 0.1, (
            band_cm, table_cm)


# ============================================================
# D 组：版本号与「刻意不落地」的决策留痕
# ============================================================
class TestVersionAndDecision:
    def test_exporter_version_bumped(self):
        """版式/去重变更必须抬版本号，否则旧缓存让修复永不可见（v23 的教训）。

        ✅ R126（2026-10-21）：v26（公式 OMML B/C 修复 + PDF 下线）→ v27
        （FIX-C2/LIM：空脚本参数丢弃 / 无界 nary hide 形态 / 算子词表 / 空 nary 体
        补空参 run —— 均改变含公式章产物字节，旧缓存内含 schema 违规 OMML）。
        ✅ R127（2026-10-22）：v27 → v28（DOCX 格式标准收口：原生列表编号 /
        附录表头重复与版心列宽 / 域拆分 run / 正文样式 / 图片版心封顶 ——
        旧缓存件列表被拍平、图表溢出，修复必须整体可见）。
        ✅ R128（2026-10-22）：v28 → v29（交叉引用 REF 域：题注书签 + 正文
        回填 5-run REF 域 —— 改变 XML 字节，未登记/被回滚编号保持纯文本）。
        ✅ R129（2026-10-22）：v29 → v30（Word 2010+ 专有命名空间清除 +
        L1~L9 标题层级 + 公式图片降级 —— 均改变产物字节，旧缓存必须整体失效）。
        ✅ R131-A（2026-10-23）：v30 → v31（图题 / AI 配图 alt 剥离生成期残留
        「图 X-Y」前缀 —— 可见题注文本由「图 2-1 图 4-1 xxx」变「图 2-1 xxx」，
        旧缓存内含双图号题注必须整体失效）。
        ✅ R151（2026-10-31）：v32（并行会话：正文子标题深度规则 + 字母前缀剥离）
        → v33（仅识别 L1~L4 级标题：DB 章节 L5+ 与正文子标题超 4 级降级加粗正文、
        TOC 域深度封顶 4 —— 标题样式/层级变化，旧缓存必须整体失效）。
        """
        assert _EXPORTER_VERSION == "33"

    def test_decision_documented_in_code(self):
        """编号/TOC 深度属跨模块契约 —— 决策必须留在代码里，不能只在报告里。"""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "app", "routers", "export.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        assert "章号.节号" in src
        assert "TOC 域深度维持用户配置" in src
        assert "章作用域判重" in src


# ============================================================
# E 组：真实形态端到端（标题 + 表格 + 图片同在一份成稿）
# ============================================================
class TestRealShapeEndToEnd:
    def test_layout_all_at_once(self):
        content = "## 1.1 吊装准备\n\n" + TABLE_MD + "\n1、第一条\n正文甲。\n"
        doc = _build(content)
        sec = doc.sections[0]
        assert abs(sec.page_width - Cm(21.0)) < Cm(0.02)
        assert doc.tables, "表格必须被渲染"
        for row in doc.tables[0].rows:
            tr_pr = row._tr.find(_W + "trPr")
            assert tr_pr is not None and tr_pr.find(_W + "cantSplit") is not None
        # 标题阶梯在真实成稿里同样生效
        assert doc.styles["Heading 1"].font.size.pt == 16
        assert doc.styles["Heading 2"].font.size.pt == 14
        # 正文段落没有被误加 cantSplit
        assert "cantSplit" not in doc.element.xml.split("<w:tbl>")[0]


# ============================================================
# F 组：R117 FIX-10 · 同章「逐字重复」正文块折叠
# ============================================================
# 取证形态复刻（artifacts/r117/_probe13.txt + 全字符级复核 _probe16.txt）：AI 把
# 同一节内容写进 ```markdown 围栏，围栏闭合后**又在围栏外重述一遍**，其中
# 逐字相同的部分（F 组）折叠、只共享前缀的部分（G 组）不得动。
_LONG = ("《建筑施工安全检查标准》（JGJ 59-2011）是本项目开展施工现场安全文明"
         "施工管理、组织自查与接受监理及建设单位核查的重要依据，重点参照其中"
         "起重机械与施工用电检查评分表的相关内容执行。")
_BOLD_SUB = "**JGJ 59-2011 现场检查标准**"
_SHORT = "吊装作业前必须完成支腿垫设验收。"  # 归一 16 字 < 40 ⇒ 不该被折叠

DUP_MD = (
    "```markdown\n"
    "本节说明编制依据与检查标准。\n\n"
    f"{_BOLD_SUB}\n\n"
    f"{_LONG}\n"
    "```\n"
    f"作业期间严禁无关人员进入警戒范围。\n\n"
    f"{_BOLD_SUB}\n\n"
    f"{_LONG}\n"
)


def _texts(doc):
    return [p.text for p in doc.paragraphs]


class TestRepeatedBlockCollapse:
    def test_duplicate_outside_fence_collapsed(self):
        """端到端：围栏外那份逐字重复的正文不得再进成稿。"""
        n = _texts(_build(DUP_MD)).count(_LONG)
        assert n == 1, f"整段重复未被折叠（成稿出现 {n} 次）"

    def test_reverse_anchor_identity_patch_keeps_two(self, monkeypatch):
        """检测力：把折叠函数换成恒等 ⇒ 同一断言必须变红（出现 2 次）。

        没有这条，「永远只输出 1 份」的变异（例如误写成整体丢弃）无法被抓到。
        """
        import app.routers.export as exp
        monkeypatch.setattr(exp, "_collapse_repeated_blocks",
                            lambda blocks, *, where="": list(blocks))
        assert _texts(_build(DUP_MD)).count(_LONG) == 2

    def test_bold_only_subtitle_also_collapsed(self):
        """`**小标题**` 形降档生效 —— 否则正文折叠后小标题仍出现两次。"""
        n = _texts(_build(DUP_MD)).count(_BOLD_SUB.replace("**", ""))
        assert n == 1, f"加粗小标题重复未被折叠（出现 {n} 次）"

    def test_short_sentence_below_threshold_kept(self):
        """阈值保护：短段重复在正常行文里常见，一律不折叠（零误伤）。"""
        blocks = [{"type": "paragraph", "text": _SHORT},
                  {"type": "paragraph", "text": _SHORT}]
        assert len(_collapse_repeated_blocks(blocks)) == 2

    def test_inline_bold_item_not_mistaken_for_subtitle(self):
        """`**名称**：说明…` 是行内加粗，不得按「小标题形」降档。"""
        short = {"type": "list_item", "text": "**名称**：说明较短"}  # 归一 11 字
        # 若被误判成 bold 形（floor=8），这条短项会被当成重复而**误删**
        assert len(_collapse_repeated_blocks([short, dict(short)])) == 2
        a = {"type": "list_item",
             "text": "**吊装构件设计图**：明确待吊装的预制混凝土构件、钢结构加固"
                     "节点、外墙装饰板材及屋面防水层的规格、外形尺寸与单件最大重量。"}
        assert len(_collapse_repeated_blocks([a, dict(a), dict(a)])) == 1

    def test_structural_blocks_never_collapsed(self):
        """表格/图表/代码是结构块 —— 重复即刻意安排，一律不动。"""
        blocks = [{"type": "table", "rows": [["a"]]}, {"type": "table", "rows": [["a"]]},
                  {"type": "chart", "text": "x"}, {"type": "chart", "text": "x"},
                  {"type": "code", "text": "y"}, {"type": "code", "text": "y"}]
        assert len(_collapse_repeated_blocks(blocks)) == 6

    def test_scope_is_per_chapter_not_global(self):
        """跨章复用同一条标准条文是正常写法 ⇒ 每次调用各自计数。"""
        b = {"type": "paragraph", "text": _LONG}
        assert len(_collapse_repeated_blocks([b])) == 1
        assert len(_collapse_repeated_blocks([b])) == 1  # 下一章同样保留

    def test_input_not_mutated_and_idempotent(self):
        blocks = [{"type": "paragraph", "text": _LONG},
                  {"type": "paragraph", "text": _LONG}]
        snapshot = [dict(x) for x in blocks]
        once = _collapse_repeated_blocks(blocks)
        assert blocks == snapshot, "入参被改写"
        assert once == _collapse_repeated_blocks(once), "二次调用不稳定"

    def test_counter_and_warning_recorded(self, caplog):
        """「确认已落地」必须落在可观测行为上：计数 + WARNING 点名章节。"""
        st = _fix_stats()
        st["repeated_blocks"] = 0
        with caplog.at_level("WARNING"):
            _collapse_repeated_blocks(
                [{"type": "paragraph", "text": _LONG},
                 {"type": "paragraph", "text": _LONG}], where="通用工程标准")
        assert st["repeated_blocks"] == 1, st["repeated_blocks"]
        st["repeated_blocks"] = 0
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "正文重复" in joined and "通用工程标准" in joined

    def test_fail_soft_on_bad_input(self):
        """畸形 blocks 不得抛异常（fail-soft 原样返回）。"""
        weird = [None, "字符串", {"type": "paragraph"}, {},
                 {"type": "paragraph", "text": _LONG}]
        assert _collapse_repeated_blocks(weird) == weird

    def test_decision_documented_in_collapse(self):
        """决策留痕：为什么折叠、为什么只在单章作用域 —— 必须在代码里。"""
        import inspect

        import app.routers.export as exp
        doc = inspect.getdoc(exp._collapse_repeated_blocks) or ""
        assert "围栏" in doc and "单章" in doc
        # 取证诚实性：两类重复（逐字相同 / 仅前缀相同）必须分开写清，否则后来者
        # 会以为「整段都重复」而把近重复也一起折叠掉。
        assert "逐字相同" in doc and "仅前缀相同" in doc


# ============================================================
# G 组：R117 FIX-11 · 同章「近似重复」只报警、不改写
# ============================================================
# 取证（artifacts/r117/_probe16.txt）：生产库「通用工程标准」章里那五段正文
# **不是**逐字重复 —— 前 66~185 字相同、之后各写各的（137~301 字）。删任一侧
# 都是实质性丢正文 ⇒ 导出器唯一有资格做的动作是**让它可见**，不是替用户取舍。
_COMMON = ("《建筑施工安全检查标准》（JGJ 59-2011）是本项目开展施工现场安全"
           "文明施工管理、组织自查与接受监理及建设单位核查的重要依据")  # 归一 64 字
_NEAR_A = _COMMON + "，重点参照起重机械检查评分表与施工用电检查评分表逐项打分。"
_NEAR_B = _COMMON + "，每月由项目负责人组织一次综合大检查并留存整改闭环记录。"


def _near_blocks():
    return [{"type": "paragraph", "text": _NEAR_A},
            {"type": "paragraph", "text": _NEAR_B}]


class TestNearDuplicateReportOnly:
    def test_prefix_shared_paragraphs_detected(self):
        """前 60 字相同、后文不同 ⇒ 检出 1 处近重复。"""
        assert _NEAR_DUP_PREFIX_CHARS <= len(_WS_ONLY_RE.sub("", _COMMON))
        assert _report_near_duplicate_blocks(_near_blocks(), where="测试章") == 1

    def test_near_duplicate_is_never_collapsed(self):
        """**边界锁定**：折叠函数必须原样保留两侧（各有独有尾句，删一侧即丢正文）。

        这条护栏的存在意义：若日后有人把「近似重复」也顺手折叠，成稿会**静默
        少一段独有内容**，而这类改动在肉眼复核里极难发现。
        """
        before = _near_blocks()
        after = _collapse_repeated_blocks(before, where="测试章")
        assert [b["text"] for b in after] == [b["text"] for b in before]

    def test_end_to_end_both_versions_survive_in_docx(self):
        """成稿侧行为取证：两段都在，且各自尾部文字一字未少。"""
        texts = _texts(_build(_NEAR_A + "\n\n" + _NEAR_B))
        assert _NEAR_A in texts and _NEAR_B in texts
        assert any("整改闭环记录" in t for t in texts), "近重复的第二段被误删"

    def test_report_does_not_touch_blocks(self):
        """纯可观测：检测函数只返回计数，入参一个块都没少。"""
        blocks = _near_blocks()
        snapshot = [dict(x) for x in blocks]
        assert isinstance(_report_near_duplicate_blocks(blocks, where="测试章"), int)
        assert blocks == snapshot and len(blocks) == 2

    def test_below_prefix_threshold_not_reported(self):
        """阈值保护：只共享 30 字的两句不同论述不得报警（防告警失真）。"""
        head = "汽车吊吊装作业必须执行"  # 归一 11 字
        blocks = [{"type": "paragraph", "text": head + "《建筑机械使用安全技术规程》"
                                                    "JGJ 33-2012 的规定，作业前完成安全交底。"},
                  {"type": "paragraph", "text": head + "总承包单位的统一管理要求，"
                                                      "分包作业不得独立指挥。"}]
        assert _report_near_duplicate_blocks(blocks, where="测试章") == 0

    def test_exact_duplicate_not_double_counted(self):
        """逐字相同归 F 组（折叠），不得在近重复里再计一次。"""
        blocks = [{"type": "paragraph", "text": _LONG},
                  {"type": "paragraph", "text": _LONG}]
        assert _report_near_duplicate_blocks(blocks, where="测试章") == 0

    def test_bold_only_headings_excluded(self):
        """`**整串加粗小标题**` 形不参与近重复 —— 短标题天然互相撞前缀。"""
        blocks = [{"type": "paragraph", "text": f"**{_COMMON}现场检查标准**"},
                  {"type": "paragraph", "text": f"**{_COMMON}临时用电标准**"}]
        assert _report_near_duplicate_blocks(blocks, where="测试章") == 0

    def test_scope_is_per_chapter_not_global(self):
        """与折叠同域：近重复判定同样只在单章作用域内做，跨章引用不得报警。"""
        assert _report_near_duplicate_blocks(
            [{"type": "paragraph", "text": _NEAR_A}], where="第二章") == 0
        assert _report_near_duplicate_blocks(
            [{"type": "paragraph", "text": _NEAR_B}], where="第五章") == 0

    def test_counter_and_warning_recorded(self, caplog):
        """报警必须可取证：计数进 X-Fix-Stats 键 + WARNING 点名章节与阈值。"""
        st = _fix_stats()
        st["near_duplicate_blocks"] = 0
        with caplog.at_level("WARNING"):
            assert _report_near_duplicate_blocks(_near_blocks(), where="通用工程标准") == 1
        assert st["near_duplicate_blocks"] == 1, st["near_duplicate_blocks"]
        st["near_duplicate_blocks"] = 0
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "正文近似重复" in joined and "通用工程标准" in joined
        assert f"前 {_NEAR_DUP_PREFIX_CHARS} 字相同" in joined
        assert "未作改动" in joined, "WARNING 必须说清成稿没改，否则用户以为已修"

    def test_stats_key_added_without_touching_legacy(self):
        """加法式扩展：新键存在且默认 0，既有键一个没少。"""
        assert "near_duplicate_blocks" in _FIX_STATS_KEYS
        st = _fix_stats()
        for legacy in ("formulas", "repeated_blocks", "table_unstacked",
                       "block_render_failed", "fences_fixed"):
            assert legacy in _FIX_STATS_KEYS, legacy
            assert st[legacy] == 0

    def test_fail_soft_on_bad_input(self):
        """畸形输入不得抛异常，也不得影响导出（返回 0）。"""
        assert _report_near_duplicate_blocks(
            [None, "字符串", {}, {"type": "paragraph"}], where="测试章") == 0

    def test_decision_documented_in_report_fn(self):
        """决策留痕：为什么「只报警不改写」必须写在代码里，不能只在报告里。"""
        import inspect

        import app.routers.export as exp
        doc = inspect.getdoc(exp._report_near_duplicate_blocks) or ""
        for kw in ("刻意不折叠", "丢正文", "重新生成"):
            assert kw in doc, kw

