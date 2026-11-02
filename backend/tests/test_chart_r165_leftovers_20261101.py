# -*- coding: utf-8 -*-
"""R165 · 图表模块遗留问题修复护栏（2026-11-01）。

上一轮报告列出的 5 项遗留问题，全部按「先复现、再修复、后锁定」处理
（复现探针为一次性脚本，不随仓交付）：

1. **4 空格缩进图表围栏不被识别**（``services/content_blocks.py``）
   ``fence_common.FENCE_LINE_RE`` 只认 ≤3 前导空白（CommonMark 口径），
   AI 按列表续行写成 4 空格时整块退化为普通段落 ⇒ **裸源码印进交付 DOCX**。
   修复：读取前调用 ``normalize_chart_fence_indent`` 去缩进（只对图表家族语言），
   **判据本身不放宽**（否则会误判 CommonMark 里合法的 4 空格代码块）。
   ⚠️ 登记侧（``_chart_pipeline.build_inline_chart_plan``）与导出侧
   （``_parse_content_blocks``）必须调用**同一份**实现 —— 否则「成稿有图 /
   清单查无此图 / 限额管不到」的幽灵图复活（R76 同族）。

2. **`` ```markdown `` 外层包裹内嵌 mermaid 被整块丢弃**（同上）
   通用扫描器按 CommonMark 判闭合，内层 `` ```mermaid `` 的裸闭合行被当成外层
   闭合 ⇒ 图表丢失、外层真闭合行变成「新开围栏」把其后正文吞进代码块。
   修复：markdown 家族改用 ``_read_markdown_fenced_block``（内层先配平）；
   读到文末仍未配平则**回退**通用扫描器，保持畸形输入的历史行为。

3. **非 ``-`` 分隔形态的图号不被剥离**（``services/content_format_contract.py``
   + ``routers/export.py::_strip_leading_figure_number``）
   ``图 3.1`` / ``图 A-1`` / ``图 3-1-2`` / ``图 3`` 此前不匹配 ``FIG_TITLE_PREFIX_RE``
   ⇒ 成稿图题重复编号（「图 2-1 图 3.1 施工工艺流程」）。
   修复：新增**派生式**宽口径 ``FIG_TITLE_ANY_PREFIX_RE``（契约模块唯一事实源），
   导出侧两判据「或」后再剥前导标点。

4. **``图 3-1-2 x`` → ``2 x`` 残留**：多段编号必须**整段**剥全（含在 #3）。

5. **劳动力图最小字档略低于 2.4mm**（``services/ai/mermaid_labor.py``）
   主文字达标但 Y 轴刻度（15px）仍 2.29mm；现统一抬到 16px ⇒ 全部字档 ≥ 2.4mm。
   本文件把 R164 的「仅主文字达标」升级为「**最小字档**达标」。

本文件全部走**运行时真实解析/渲染**（patch ``paper_floor_note`` 拿真实画布与
字号），不抄常量表做断言 —— 实现日后调几何，断言仍按同一公式重新判定。
"""
from __future__ import annotations

import json
import logging

import pytest
from app.routers._chart_pipeline import build_inline_chart_plan
from app.services import content_blocks as cb
from app.services.ai import mermaid_common as ma
from app.services.ai import mermaid_labor as labor
from PIL import Image

MERMAID = "```mermaid\nflowchart TD\n  A[开始]-->B[施工]\n  B-->C[验收]\n```"
LEAD_IN = "施工工艺流程如下图所示：\n\n"


def _chart_blocks(content: str) -> list[dict]:
    return [b for b in cb._parse_content_blocks(content) if b.get("type") == "chart"]


# ===========================================================================
# 1 · 4+ 空格缩进图表围栏：归一为可解析形态
# ===========================================================================

class TestIndentedChartFence:

    @pytest.mark.parametrize("indent", [4, 6, 8])
    def test_indented_fence_parsed_as_chart(self, indent):
        """4/6/8 空格缩进的图表围栏必须被解析成 chart（修复前退化为段落）。"""
        pad = " " * indent
        text = LEAD_IN + pad + MERMAID.replace("\n", "\n" + pad) + "\n"
        blocks = _chart_blocks(text)
        assert len(blocks) == 1, blocks
        assert blocks[0].get("title") == "施工工艺流程"

    @pytest.mark.parametrize("indent", [4, 6])
    def test_indented_fence_registered_by_pipeline(self, indent):
        """登记侧必须同样识别（同口径），否则图表清单/限额查不到这张图。"""
        pad = " " * indent
        text = LEAD_IN + pad + MERMAID.replace("\n", "\n" + pad) + "\n"
        normalized, rows = build_inline_chart_plan("sid", "secid", text)
        assert len(rows) == 1, rows
        # 归一后的围栏行必须是 ≤3 前导空白（否则导出侧仍解析不到）
        fence_line = next(ln for ln in normalized.splitlines()
                          if ln.lstrip(" \t").startswith("```mermaid"))
        assert fence_line == "```mermaid", repr(fence_line)

    def test_normalization_keeps_line_count(self):
        """行数必须不变 —— ``src_line`` 与围栏 ordinal 契约依赖它。"""
        pad = " " * 4
        text = LEAD_IN + pad + MERMAID.replace("\n", "\n" + pad) + "\n"
        normalized, log = cb.normalize_chart_fence_indent(text)
        assert len(normalized.split("\n")) == len(text.split("\n"))
        assert log and log[0]["indent"] == 4

    def test_normalization_idempotent(self):
        """二次归一必须无操作（幂等）—— 导出与登记两侧都会调它。"""
        pad = " " * 4
        text = LEAD_IN + pad + MERMAID.replace("\n", "\n" + pad) + "\n"
        once, _ = cb.normalize_chart_fence_indent(text)
        twice, log2 = cb.normalize_chart_fence_indent(once)
        assert twice == once and log2 == []

    def test_non_chart_fence_untouched(self):
        """非图表家族的 4 空格缩进块（合法 CommonMark 代码块）一律不动。"""
        text = "示例代码：\n\n    ```python\n    print(1)\n    ```\n"
        normalized, log = cb.normalize_chart_fence_indent(text)
        assert normalized == text and log == []

    def test_unclosed_indented_fence_untouched(self):
        """找不到闭合行时原样保留 —— 绝不制造新的未闭合围栏。"""
        text = LEAD_IN + "    ```mermaid\n    flowchart TD\n"
        normalized, log = cb.normalize_chart_fence_indent(text)
        assert normalized == text and log == []

    def test_chart_fence_inside_other_block_untouched(self):
        """被其它围栏块包住的「示例文本」不得被去缩进（保护区规则）。"""
        text = (
            "```markdown\n"
            "示例（本块内不解析）：\n"
            "    ```mermaid\n"
            "    flowchart TD\n"
            "    ```\n"
            "```\n"
        )
        normalized, log = cb.normalize_chart_fence_indent(text)
        assert normalized == text and log == []


# ===========================================================================
# 2 · ```markdown 外层包裹：内层先配平，外层闭合不吞正文
# ===========================================================================

class TestMarkdownWrapper:

    @pytest.mark.parametrize("indent", [0, 4])
    def test_inner_mermaid_kept(self, indent):
        """`` ```markdown `` 包裹内嵌 mermaid：图表必须保留（修复前整块丢弃）。"""
        pad = " " * indent
        wrapper = (
            "```markdown\n"
            f"{pad}{LEAD_IN}"
            f"{pad}{MERMAID.replace(chr(10), chr(10) + pad)}\n"
            "```\n"
        )
        blocks = _chart_blocks(wrapper)
        assert len(blocks) == 1, blocks

    def test_text_after_outer_close_not_swallowed(self):
        """外层真正闭合行之后的正文不得被吞进代码块。"""
        wrapper = (
            "```markdown\n"
            f"{LEAD_IN}"
            f"{MERMAID}\n"
            "```\n\n"
            "其后正文段落不得被吞。\n"
        )
        blocks = cb._parse_content_blocks(wrapper)
        paras = [b.get("text") for b in blocks if b.get("type") == "paragraph"]
        assert "其后正文段落不得被吞。" in paras, paras
        assert len([b for b in blocks if b.get("type") == "chart"]) == 1

    def test_unbalanced_inner_falls_back(self):
        """内层未配平（孤立裸围栏）→ 回退通用扫描器，保持历史行为。"""
        text = "```markdown\n示例内容\n```\n\n其后正文。\n"
        blocks = cb._parse_content_blocks(text)
        kinds = [b.get("type") for b in blocks]
        # 历史行为：外层被正常闭合，其后正文是独立段落（不被吞）
        assert "paragraph" in kinds
        tail = [b.get("text") for b in blocks if b.get("type") == "paragraph"][-1]
        assert "其后正文" in (tail or ""), blocks


# ===========================================================================
# 3 · 图号剥离：宽口径（多段 / 点号 / 字母章号 / 裸序号）
# ===========================================================================

class TestStripLeadingFigureNumberWide:

    @staticmethod
    def _strip(text: str) -> str:
        from app.routers.export import _strip_leading_figure_number
        return _strip_leading_figure_number(text)

    @pytest.mark.parametrize("text,expected", [
        ("图 3.1 施工工艺流程", "施工工艺流程"),
        ("图3.1 施工工艺流程", "施工工艺流程"),
        ("图 A-1 平面布置", "平面布置"),
        ("图 B.2 节点构造", "节点构造"),
        ("图 3-1-2 节点详图", "节点详图"),
        ("图 3 施工准备", "施工准备"),
        ("图3-1：施工工艺流程", "施工工艺流程"),
        ("图 3-1 施工工艺流程", "施工工艺流程"),
        ("图 3—1 施工工艺流程", "施工工艺流程"),
    ])
    def test_wide_prefix_stripped(self, text, expected):
        assert self._strip(text) == expected

    def test_non_figure_text_not_stripped(self):
        """「图纸会审流程」等普通词组不得被误剥成「纸会审流程」。"""
        for text in ["图纸会审流程", "图像识别方案", "", "施工工艺流程"]:
            assert self._strip(text) == text

    def test_dirties_idempotent(self):
        """幂等：导出链路在 alt / title / 源文图题三处都会调。"""
        for text in ["图 3.1 施工工艺流程", "图 A-1 平面布置", "图 3 施工准备"]:
            once = self._strip(text)
            assert self._strip(once) == once

    def test_contract_source_of_truth(self):
        """宽口径判据只允许住在契约模块，导出侧只 import 不复制。"""
        from app.routers import export as export_mod
        from app.services.content_format_contract import FIG_TITLE_ANY_PREFIX_RE
        assert export_mod.FIG_TITLE_ANY_PREFIX_RE is FIG_TITLE_ANY_PREFIX_RE

    def test_contract_derives_from_narrow(self):
        """宽口径必须是**派生式**：窄口径能剥的，宽口径必须也能剥。"""
        from app.services.content_format_contract import (
            FIG_TITLE_ANY_PREFIX_RE,
            FIG_TITLE_PREFIX_RE,
        )
        for text in ["图 3-1 施工工艺流程", "图3-1 施工工艺流程"]:
            m_narrow = FIG_TITLE_PREFIX_RE.match(text)
            m_wide = FIG_TITLE_ANY_PREFIX_RE.match(text)
            assert m_narrow and m_wide, text
            assert m_wide.end() >= m_narrow.end(), text


# ===========================================================================
# 4 · 劳动力图：**全部字档**（含 Y 轴刻度）≥ 2.4mm
# ===========================================================================

_LABOR_DATA = {
    "type": "labor",
    "title": "劳动力投入及工种分布图",
    "phases": ["准备期", "基础期", "主体期", "装饰期", "收尾期"],
    "categories": ["钢筋工", "木工", "混凝土工", "电工", "焊工"],
    "data": [
        [10, 30, 40, 60, 90],
        [5, 20, 35, 50, 70],
        [5, 25, 40, 55, 60],
        [2, 10, 20, 30, 40],
        [0, 5, 10, 12, 8],
    ],
}


def _capture_paper(monkeypatch, module, render) -> dict:
    """渲染一次并捕获 ``paper_floor_note`` 的**真实**画布/字号入参。"""
    captured: dict = {}

    def fake_note(label, fonts, canvas_w, canvas_h=0, *, log=None,
                  suggest="", primary=None):
        captured.update(label=label, fonts=list(fonts), w=canvas_w, h=canvas_h,
                        primary=list(primary if primary is not None else fonts))
        return 0.0

    monkeypatch.setattr(module, "paper_floor_note", fake_note)
    stream = render()
    assert stream is not None
    captured["img_w"] = Image.open(stream).size[0]
    assert captured["w"] == captured["img_w"], "纸面播报必须用真实画布宽"
    return captured


def _paper_mm(captured: dict, font) -> float:
    return ma.paper_font_mm(ma.font_px(font), captured["w"], captured["h"])


class TestLaborAllFontFloor:

    def test_all_fonts_meet_floor(self, monkeypatch):
        """最小字档（Y 轴刻度）也必须 ≥ 2.4mm —— 修复前实测 2.29mm。"""
        cap = _capture_paper(
            monkeypatch, labor,
            lambda: labor._render_labor_image_v2(json.loads(json.dumps(_LABOR_DATA))),
        )
        worst = min(_paper_mm(cap, f) for f in cap["fonts"])
        assert worst >= ma.PAPER_FONT_MIN_MM - 1e-6, (
            f"劳动力图最小字档纸面 {worst:.2f}mm（画布 {cap['w']}px），"
            f"低于下限 {ma.PAPER_FONT_MIN_MM}mm"
        )

    def test_render_emits_no_floor_warning(self, monkeypatch, caplog):
        """渲染后不得再有「低于下限」告警（用户可见红字的来源）。"""
        monkeypatch.delenv("CHART_PAPER_MIN_MM", raising=False)
        monkeypatch.delenv("ARCH_LEAF_PAPER_MIN_MM", raising=False)
        caplog.set_level(logging.WARNING)
        labor._render_labor_image_v2(json.loads(json.dumps(_LABOR_DATA)))
        bad = [r.getMessage() for r in caplog.records if "低于下限" in r.getMessage()]
        assert bad == [], bad


# ===========================================================================
# 5 · 端到端 DOCX：缩进块 + markdown 包裹块都出图、图号不虚跳、源码不泄漏
# ===========================================================================

def _png_bytes(color=(200, 210, 230)) -> bytes:
    from io import BytesIO
    buf = BytesIO()
    Image.new("RGB", (60, 40), color).save(buf, format="PNG")
    return buf.getvalue()


#: 缩进 4 空格的图表块（首行缩进 = AI 列表续行的真实形态）
_CHAPTER_CONTENT = (
    "施工工艺流程如下图所示：\n"
    "\n"
    "    ```mermaid\n"
    "    graph TD\n"
    "     A-->B\n"
    "    ```\n"
    "\n"
    "```markdown\n"
    "图 3.1 验收流程\n"
    "\n"
    "```mermaid\n"
    "graph TD\n"
    " X-->Y\n"
    "```\n"
    "```\n"
    "\n"
    "其后正文段落不得被吞。\n"
)


def _build_r165_docx(tmp_path):
    from io import BytesIO

    from app.routers.export import (
        _build_docx_sync,
        _load_heading_styles,
        _parse_content_blocks,
    )
    from docx import Document

    sections = [{
        "id": "c1", "parent_id": "", "sort_order": 0, "level": 1,
        "title": "第一章 施工组织", "content": _CHAPTER_CONTENT,
    }]
    # ⚠️ 键必须是**原始代码**（export.py `rendered_bytes.get((chart_type, mermaid_code))`），
    #    不是 `_norm_code` 折叠空白后的形态。
    rendered = {
        ("flowchart", "graph TD\n A-->B"): BytesIO(_png_bytes()),
        # 第二张用不同底色：python-docx 会按内容哈希合并**完全相同**的图，
        # 同图无法证明「两张图各自嵌入」。
        ("flowchart", "graph TD\n X-->Y"): BytesIO(_png_bytes((230, 200, 200))),
    }
    out = str(tmp_path / "r165.docx")
    _build_docx_sync(
        out, {"id": "s1", "project_id": "p1", "name": "R165 验证方案"},
        sections, {}, {}, rendered,
        {"c1": _parse_content_blocks(_CHAPTER_CONTENT)},
        "宋体", 12, "", "", False, False, False,
        bidder_name="",
        heading_styles=_load_heading_styles({}),
        page_break_before_chapter=True,
        line_spacing=1.5,
        page_number_style="simple",
        toc_depth=3,
        margins=None, cover_info=None,
    )
    return out, Document(out)


class TestEndToEndDocx:

    def test_indented_and_wrapped_charts_both_rendered(self, tmp_path):
        """缩进块与 markdown 包裹块都必须出图（修复前：前者源码泄漏、后者整块丢弃）。"""
        import zipfile

        out, doc = _build_r165_docx(tmp_path)
        with zipfile.ZipFile(out) as z:
            media = [n for n in z.namelist() if n.startswith("word/media/")]
        assert len(media) >= 2, f"两张图应各自嵌入，实得 {media}"

    def test_figure_numbers_do_not_skip(self, tmp_path):
        """图号按出现顺序连续：图 1-1 / 图 1-2（任一图丢失都会造成虚跳）。"""
        _out, doc = _build_r165_docx(tmp_path)
        texts = [p.text for p in doc.paragraphs]
        assert "图 1-1 施工工艺流程" in texts, texts
        assert "图 1-2 验收流程" in texts, texts
        # 源文自写的旧号「图 3.1」已被吸收并重编，不得残留在成稿里
        assert not any("图 3.1" in t for t in texts), texts

    def test_no_fence_source_leaked(self, tmp_path):
        """围栏源码不得印进交付文档（`` ``` `` / mermaid 语句都不允许出现）。"""
        _out, doc = _build_r165_docx(tmp_path)
        joined = "\n".join(p.text for p in doc.paragraphs)
        assert "```" not in joined, joined
        assert "graph TD" not in joined, joined
        assert "mermaid" not in joined, joined

    def test_trailing_paragraph_not_swallowed(self, tmp_path):
        """markdown 外层闭合行之后的正文必须保留（修复前被吞进代码块）。"""
        _out, doc = _build_r165_docx(tmp_path)
        joined = "\n".join(p.text for p in doc.paragraphs)
        assert "其后正文段落不得被吞。" in joined, joined
        assert "施工工艺流程如下图所示：" in joined, joined

