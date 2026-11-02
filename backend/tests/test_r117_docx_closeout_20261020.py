"""R117 · 正文导出成稿问题收口（2026-10-20）。

【取证来源】`室外汽车吊吊装施工专项方案_20261020_第1轮.docx`（1023 段 / 32 表 /
2 内联图 / 81 个 oMath）。逐段核对后定位到三个**导出渲染层**缺陷：

1. **markdown 围栏被当成代码块**：3 个章节的正文被模型包进 ````` ```markdown `````，
   其中含 GFM 表格、``$W_{max}$`` 公式、``##### 3.1.3 …`` 子标题。旧实现落到通用
   code 分支 ⇒ 成稿出现**一个 Normal 段落**（53 runs / 33 个 ``w:br``）：
   表格不渲染、表号与后续真实表格重号（两个「表 1-1」）、LaTeX 原文泄漏、
   4 个子标题既无 Heading 样式也不进目录、源文 ``1.2.2`` 与父章节 ``2.2`` 编号冲突。
2. **加粗中文枚举伪标题**：``**1）、工程基本信息**`` 这类整行加粗 + ``N）、`` 的
   伪标题不被识别（既有判据要求「编号 + 空格」）⇒ Normal 段落，且保留源文
   ``1）、`` 编号与 ``_compute_subheading`` 的 L6 编号撞号（同节出现多个 ``1）、``）。
3. **有序列表遇非列表块一律重置**：``1、A / 正文 / 表 / 1、B`` 全部渲染成 ``1、``，
   6 个章节的顶格列举无一例外。

另有第 4 项（自动表题把 LaTeX 与「序号/备注」拼进题注）同属渲染层，一并收口。

本文件按「解析层 → 渲染层 → 成稿字节」三层锁定，并保留 ```text 围栏的既有契约。
"""
import os
import tempfile

import pytest
from app.routers.export import (
    _build_docx_sync,
    _load_heading_styles,
    _sanitize_auto_caption,
)
from app.services.content_blocks import _parse_content_blocks
from docx import Document

from docx_display import display_texts


def _build(content: str, *, chapter: str = "第一章 测试章节") -> Document:
    """用导出单章节入口生成 docx 并返回 Document。"""
    sections = [{
        "id": "c1", "parent_id": "", "sort_order": 0, "level": 1,
        "title": chapter, "content": content,
    }]
    blocks = {"c1": _parse_content_blocks(content)}
    out = os.path.join(tempfile.gettempdir(), "r117_closeout.docx")
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


def _texts(doc: Document, *, skip_first: str = "第一章 测试章节") -> list[str]:
    # R127 起列表前缀由 numbering.xml 渲染，用 display_texts 还原可见文本
    return [t for t in display_texts(doc)
            if t.strip() and t.strip() != skip_first]


def _types(blocks: list[dict]) -> list[str]:
    return [b["type"] for b in blocks]


# ============================================================
# A 组：`` ```markdown `` 围栏按 Markdown 解析（核心）
# ============================================================
class TestMarkdownFenceUnwrapped:
    FENCED = (
        "```markdown\n"
        "1.2.2 汽车吊选型与性能校核 — 起重臂长\n"
        "\n"
        "依据《建筑施工起重吊装工程安全技术规范》（JGJ 276-2012）。\n"
        "\n"
        "| 构件 | 重量 |\n"
        "| --- | --- |\n"
        "| 预制板 | 3.2t |\n"
        "\n"
        "$$H_{max} = L_{jib} \\sin\\alpha$$\n"
        "```\n"
    )

    def test_no_code_block_emitted(self):
        blocks = _parse_content_blocks(self.FENCED)
        assert "code" not in _types(blocks), blocks

    def test_inner_table_is_real_table(self):
        blocks = _parse_content_blocks(self.FENCED)
        tables = [b for b in blocks if b["type"] == "table"]
        assert len(tables) == 1, blocks
        assert "预制板" in "\n".join(tables[0]["lines"])

    def test_inner_heading_becomes_heading(self):
        blocks = _parse_content_blocks(self.FENCED)
        heads = [b for b in blocks if b["type"] == "heading"]
        assert heads and heads[0]["text"].startswith("汽车吊选型与性能校核"), blocks
        # 源文 ``1.2.2`` 的编号被剥除（交由 _compute_subheading 重排）
        assert blocks[0]["text"].startswith("汽车吊选型与性能校核")

    def test_inner_formula_survives_as_paragraph(self):
        blocks = _parse_content_blocks(self.FENCED)
        assert any("H_{max}" in (b.get("text") or "") for b in blocks), blocks

    def test_text_fence_still_code(self):
        """回归锚：`` ```text `` 是**纯文本代码块**，其中的 ### 不得被当成标题。"""
        blocks = _parse_content_blocks("```text\n### 围栏内不是标题\n```\n")
        assert _types(blocks) == ["code"], blocks

    def test_unclosed_markdown_fence_not_dropped(self):
        """未闭合的 markdown 围栏：内容按 Markdown 解析，绝不整块丢弃。"""
        blocks = _parse_content_blocks("```markdown\n## 标题\n正文段落。\n")
        kinds = _types(blocks)
        assert "heading" in kinds, blocks
        assert "paragraph" in kinds, blocks

    def test_nested_depth_capped(self):
        """畸形嵌套（markdown 套 markdown 套 markdown）不得无限递归。"""
        content = "```markdown\n```markdown\n```markdown\n## 深\n```\n```\n```\n"
        blocks = _parse_content_blocks(content)
        assert blocks  # 有产出即可（不抛 RecursionError）

    def test_docx_has_no_fence_leak_and_no_giant_paragraph(self):
        doc = _build(self.FENCED)
        texts = _texts(doc)
        assert not any("```" in t for t in texts), texts
        assert not any(len(t) > 2000 for t in texts), [len(t) for t in texts]
        assert len(doc.tables) == 1, len(doc.tables)

    def test_docx_formula_rendered_as_omml(self):
        """``$$…$$`` 必须转成 OMML（``m:oMath``），不能以 ``$`` 原文出现在正文。"""
        doc = _build("正文段落。\n\n$$H_{max} = L_{jib} \\sin\\alpha$$\n")
        xml = doc.element.xml
        assert "oMath" in xml, xml[:400]
        assert not any("$" in t for t in _texts(doc)), _texts(doc)


# ============================================================
# B 组：整行加粗的中文枚举伪标题
# ============================================================
class TestBoldEnumHeading:
    @pytest.mark.parametrize("line,expect", [
        ("**1）、工程基本信息**", "工程基本信息"),
        ("**2）气候条件**", "气候条件"),
        ("**3、风险辨识**", "风险辨识"),
        ("**（4）施工准备**", "施工准备"),
    ])
    def test_recognized_as_heading(self, line, expect):
        blocks = _parse_content_blocks(line + "\n正文。\n")
        assert blocks[0]["type"] == "heading", blocks
        assert blocks[0]["level"] == 2
        assert blocks[0]["text"] == expect

    def test_bold_list_item_with_colon_not_heading(self):
        """``**1. 吊装构件设计图**：明确…`` 是「加粗列表项 + 冒号接正文」，不是标题。"""
        blocks = _parse_content_blocks("**1. 吊装构件设计图**：明确构件外形尺寸。\n")
        assert blocks[0]["type"] != "heading", blocks

    def test_plain_enum_not_promoted(self):
        """不加粗的 ``1）、xxx`` 仍按列表项处理（不抢标题语义）。"""
        blocks = _parse_content_blocks("1）、工程基本信息\n")
        assert blocks[0]["type"] == "list_item", blocks

    def test_same_number_repeated_is_renumbered_in_docx(self):
        """实测症状：同节多个 ``1）、``。成稿必须是连续编号。"""
        content = (
            "**1）、工程基本信息**\n正文甲。\n\n"
            "**1）、气候条件**\n正文乙。\n\n"
            "**1）、风险辨识**\n正文丙。\n"
        )
        texts = [t for t in _texts(_build(content)) if t.startswith(("1.1", "1.2", "1.3"))]
        assert len(texts) == 3, _texts(_build(content))
        assert len(set(texts)) == 3


# ============================================================
# C 组：有序列表跨「正文 / 表格」续号
# ============================================================
class TestOrderedAcrossBlocks:
    def test_paragraph_between_items(self):
        texts = _texts(_build(
            "1、被吊构件参数梳理\n正文说明甲。\n\n1、汽车吊选型依据\n正文说明乙。\n"))
        assert "1、被吊构件参数梳理" in texts
        assert "2、汽车吊选型依据" in texts, texts
        assert not any(t.startswith("1、汽车吊选型依据") for t in texts), texts

    def test_table_between_items(self):
        texts = _texts(_build(
            "1、现场地基承载力分析\n正文甲。\n\n"
            "表 1-1 地基参数表\n| 项目 | 参数 |\n| --- | --- |\n| fak | 80kPa |\n\n"
            "1、作业条件综合评估\n正文乙。\n"))
        assert "2、作业条件综合评估" in texts, texts

    def test_ascii_dot_items_continue(self):
        texts = _texts(_build(
            "1. 起重高度校核：满足要求。\n说明甲。\n\n1. 起重半径校核：满足要求。\n"))
        assert "1. 起重高度校核：满足要求。" in texts
        assert "2. 起重半径校核：满足要求。" in texts, texts

    def test_colon_lead_in_resets(self):
        """冒号引导句（"具体如下："）之后的列举是**新一组**，必须从 1 起算。"""
        texts = _texts(_build(
            "1、第一步\n正文甲。\n\n具体如下：\n\n1、新组第一步\n正文乙。\n"))
        assert "1、第一步" in texts, texts
        assert "1、新组第一步" in texts, texts
        assert not any(t.startswith("2、新组第一步") for t in texts), texts

    def test_heading_resets(self):
        texts = _texts(_build(
            "1、第一章内第一条\n正文甲。\n\n## 新小节\n\n1、新小节第一条\n"))
        assert "1、第一章内第一条" in texts, texts
        assert "1、新小节第一条" in texts, texts
        assert not any(t.startswith("2、新小节第一条") for t in texts), texts

    def test_marker_change_still_resets(self):
        """既有契约：同层标记样式变化仍从 1 重计。"""
        texts = _texts(_build("1. 甲\n正文。\n\n（一）乙\n"))
        assert "（一）乙" in texts, texts


# ============================================================
# D 组：自动表题清洗
# ============================================================
class TestAutoCaptionSanitized:
    def test_strip_math_and_junk_cells(self):
        assert _sanitize_auto_caption(
            ["序号", "项目", "参数指标", "备注"]) == "项目、参数指标"
        assert _sanitize_auto_caption(
            ["构件类型", "高度 $H$ (m)", "垂直度允许偏差 $[\\Delta]$ (mm)"]
        ) == "构件类型、高度 (m)、垂直度允许偏差 (mm)"

    def test_dedupe_and_trim(self):
        assert _sanitize_auto_caption(["项目", "项目", " 参数 "]) == "项目、参数"

    def test_all_junk_returns_empty(self):
        assert _sanitize_auto_caption(["序号", "备注"]) == ""

    def test_docx_auto_caption_has_no_dollar(self):
        doc = _build(
            "正文。\n\n| 构件类型 | 高度 $H$ (m) | 外观检查 |\n"
            "| --- | --- | --- |\n| 柱 | 12 | 合格 |\n")
        caps = [p.text for p in doc.paragraphs if p.text.strip().startswith("表 ")]
        assert caps, [p.text for p in doc.paragraphs][:10]
        assert "$" not in caps[0], caps
        assert "序号" not in caps[0], caps


# ============================================================
# E 组：真实形态端到端（围栏 + 表格 + 枚举标题 + 打断的列表）
# ============================================================
class TestRealShapeEndToEnd:
    CONTENT = (
        "```markdown\n"
        "2.2.1 设计文件\n"
        "依据 JGJ 276-2012 执行。\n"
        "\n"
        "| 控制项目 | 允许偏差 |\n"
        "| --- | --- |\n"
        "| 垂直度 | 1/1000 |\n"
        "```\n"
        "\n"
        "**1）、工程基本信息**\n"
        "本工程位于上海市。\n"
        "\n"
        "1、被吊构件参数梳理\n"
        "最重构件 3.2t。\n"
        "\n"
        "表 2-2 主要参数表\n"
        "| 项目 | 参数 |\n"
        "| --- | --- |\n"
        "| 最大重量 | 3.2t |\n"
        "\n"
        "1、汽车吊选型依据\n"
        "选用 25t 汽车吊。\n"
    )

    def test_structure(self):
        blocks = _parse_content_blocks(self.CONTENT)
        kinds = _types(blocks)
        assert "code" not in kinds, blocks
        assert kinds.count("table") == 2, kinds
        assert "heading" in kinds

    def test_docx_numbers_unique(self):
        doc = _build(self.CONTENT)
        texts = _texts(doc)
        assert not any("```" in t for t in texts), texts
        assert "2、汽车吊选型依据" in texts, texts
        # 表号唯一
        caps = [t for t in texts if t.startswith("表 ")]
        assert len(caps) == len(set(caps)) == 2, caps

    def test_heading_style_present(self):
        doc = _build(self.CONTENT)
        styles = [p.style.name for p in doc.paragraphs if p.text.strip()]
        assert any(s.startswith("Heading") for s in styles), styles
