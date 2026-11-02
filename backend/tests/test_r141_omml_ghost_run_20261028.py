# -*- coding: utf-8 -*-
r"""R141 · LaTeX→OMML 幽灵空 run 清理 —— `\text{}`/`\mathrm{}` 等空内容不得产出空 `<m:t></m:t>`。

背景（R128 留痕 · 建议后续「幽灵 token 清理」）：`\text{}` / `\mathrm{}` /
`\operatorname{}` / `\mbox{}` 内容为空时，旧实现把 `txt=""` 组装成
`("op", "")` 节点 → `_node_to_omml` 的 `_r("")` 产出 `<m:r><m:t></m:t></m:r>`
空 run（探针实拍）。空 run 虽不违反 OOXML schema（m:t 可为空），但：

  ① 语义空洞 —— 一段「看不见」的数学内容；
  ② 会把 `\frac{a}{\text{}}` 的空 m:den 伪装成「有内容」，从而绕过 R122
     空参防护网（后者只查 `<m:num></m:num>` 这类**元素级**空，而
     `<m:den><m:r><m:t></m:t></m:r></m:den>` 元素级非空）。

R141 修复：`\text{}` 等空内容不再 append `("op", "")`（_parse_seq 跳过 /
_parse_atom 返回空 seq），空参形态变回**真正为空** → R122 防护网接管
（回退源码文本），与 `\frac{a}{}` 同一一致性口径。非空 `\text{…}` 逐字不变。
"""
import io
import re
import sys
import zipfile
from pathlib import Path

import pytest
from docx import Document
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers.export import _add_runs_with_inline_format
from app.services import docx_math

M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"

#: 幽灵空 run：`<m:t></m:t>` 零内容（一个空格都不是）。
_GHOST_MT_RE = re.compile(r"<m:t[^>]*></m:t>")

#: R122 空 OMML 必需参数元素（m:f 的 num/den、m:rad 与 m:nary 的 e、m:rad 的 deg）。
_EMPTY_ARG_RE = re.compile(
    r"<m:(num|den|e|deg)(?:\s[^>]*)?>\s*</m:\1>|<m:(num|den|e|deg)(?:\s[^>]*)?/>")


# ---------------------------------------------------------------------------
# A 组 · 幽灵空 run 不再产出
# ---------------------------------------------------------------------------

class TestGhostEmptyRunRemoved:
    """空 `\\text{}` 族不再产出 `<m:t></m:t>` 空 run。"""

    @pytest.mark.parametrize("latex", [
        r"\text{}",
        r"\mathrm{}",
        r"\operatorname{}",
        r"\mbox{}",
        r"\text{ }",
        r"a\text{}b",
        r"\alpha\text{}",
        r"\text{} + \text{}",
    ])
    def test_no_ghost_empty_run(self, latex):
        xml = docx_math.latex_to_omml(latex)
        assert not _GHOST_MT_RE.search(xml), (
            f"{latex!r} 仍产出幽灵空 run: {xml}")

    def test_empty_text_not_inside_required_arg_still_valid(self):
        """独立 `\\text{}` 整式不触发防护（仅空 run 消除，公式仍合法）。"""
        xml = docx_math.latex_to_omml(r"a\text{}b")
        # 合法 OMML 且不含任何空参元素
        etree.fromstring(xml)
        assert not _EMPTY_ARG_RE.search(xml), xml


# ---------------------------------------------------------------------------
# B 组 · 空 \text{} 位于必需参数内 → 回退源码（与 \frac{a}{} 同口径）
# ---------------------------------------------------------------------------

class TestEmptyTextInRequiredArgFallsBack:
    """`\\frac{a}{\\text{}}` 的空 den 不再伪装成有内容，R122 防护网接管。"""

    def test_empty_text_den_raises_guard(self):
        with pytest.raises(ValueError, match="空参数元素"):
            docx_math.latex_to_omml(r"\frac{a}{\text{}}")

    def test_empty_mathrm_den_raises_guard(self):
        with pytest.raises(ValueError, match="空参数元素"):
            docx_math.latex_to_omml(r"\frac{a}{\mathrm{}}")

    def test_export_chain_no_empty_den(self):
        """真实导出链：`\\frac{a}{\\text{}}` 落 docx 后**无空 m:den**、无幽灵空 run。

        降级路径（matplotlib 图片 / 源码文本）由环境决定，本护栏只锁不变量：
        空 den 与幽灵空 run 绝不出现在产物里（R122/R141 的共同目标）。
        """
        doc = Document()
        para = doc.add_paragraph()
        _add_runs_with_inline_format(para, r"残式 $\frac{a}{\text{}}$ 保留内容。")
        buf = io.BytesIO()
        doc.save(buf)
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
            root = etree.fromstring(zf.read("word/document.xml"))
        body = root.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}body")
        assert not list(root.iter(f"{{{M_NS}}}den")), (
            "导出 docx 仍含空 m:den（\text{} 伪装有内容绕过 R122）")
        assert not _GHOST_MT_RE.search(etree.tostring(root).decode()), (
            "导出 docx 仍含幽灵空 run")
        # 内容确实进了 docx（图片或源码文本至少一种）
        assert "残式" in "".join(body.itertext()) or root.findall(
            f".//{{{M_NS}}}oMath")


# ---------------------------------------------------------------------------
# C 组 · 非空 \text{…} 逐字不变（回归护栏）
# ---------------------------------------------------------------------------

class TestNonEmptyTextPreserved:
    """非空 `\\text{…}` / `\\mathrm{…}` 内容保留，零行为变更。"""

    def test_text_content_preserved(self):
        xml = docx_math.latex_to_omml(r"\text{式中：} N")
        assert "式中" in xml and "N" in xml
        assert not _GHOST_MT_RE.search(xml)

    def test_mathrm_content_preserved(self):
        xml = docx_math.latex_to_omml(r"\mathrm{MPa}")
        assert "MPa" in xml
        assert not _GHOST_MT_RE.search(xml)

    def test_realistic_battery_no_ghost(self):
        for f in (r"\sigma = \frac{N}{\phi A} \le f",
                  r"\sqrt[3]{27} = 3",
                  r"x_{i}^{2}", r"a_{b_c}",
                  r"\int_{0}^{t} v \, dt"):
            xml = docx_math.latex_to_omml(f)
            assert not _GHOST_MT_RE.search(xml), f"{f!r}: {xml}"
            assert not _EMPTY_ARG_RE.search(xml), f"{f!r}: {xml}"


# ---------------------------------------------------------------------------
# D 组 · `\sum_{}` 空被和式保留（R126 FIX-C2 刻意形态，非回归）
# ---------------------------------------------------------------------------

class TestEmptyNaryBodyKept:
    """`\\sum_{}` 的空 m:e 空参 run 是 R126 FIX-C2 的刻意形态（schema 非空
    CT_OMathArg 必需），本修复**不得**将其一并删除。"""

    def test_sum_empty_body_keeps_empty_run(self):
        xml = docx_math.latex_to_omml(r"\sum_{}")
        root = etree.fromstring(xml)
        nary = root.find(f".//{{{M_NS}}}nary")
        assert nary is not None, xml
        body = nary.find(f"{{{M_NS}}}e")
        assert body is not None and len(body) >= 1, xml
        # 刻意空参 run（Word 同款），非空参元素
        assert not _EMPTY_ARG_RE.search(xml), xml
