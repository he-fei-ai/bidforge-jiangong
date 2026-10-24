# -*- coding: utf-8 -*-
"""R122 · WPS「文字程序意外中断」崩溃收口 —— LaTeX->OMML 空参数元素回归测试。

背景（2026-10-21 22:03 用户取证）：WPS 打开平台导出的 DOCX 报
「文字程序意外中断，当前文档编辑已被意外打断，可尝试重新载入以恢复工作状态」。
诊断（artifacts 扫描全部 37 个导出件）：`43b4612e` 导出件 132 个 oMath 中
**10/10 个 m:f 的 m:num/m:den 双空**、`d5104b41` 有空 m:e、`s5` 有空 sub/sup。

根因（docx_math 三处叠加，缺一不可）：
  ① `_parse_seq` 返回 `(节点列表, 新索引)` 二元组，而 `_parse_required_group` /
     `_parse_script_arg` / `_parse_optional_bracket` 把整个元组再包一层返回，
     调用方按 `(x, i)` 解包 → num/den/base/sub/sup/deg 变成 `(list, int)` 元组；
  ② `_node_to_omml` 只接受单节点元组，对元组型 `node[0]`（是 list 不是 str）
     无分支命中 → 返回 "" → 空 `<m:num></m:num><m:den></m:den>`；
  ③ 空 CT_OMathArg 违反 OOXML schema —— Word 静默修复，WPS 直接崩溃。

修复：
  ① 三个 helper 取 `_parse_seq(...)[0]` 解包（根因）；
  ② `_node_to_omml` 顶部增加 list 分支按 seq 语义拼接（同根因）；
  ③ `latex_to_omml` 增加空参数元素防护网：命中则 ValueError，
     由 `export._add_omml_formula` 既有 except 回退为 LaTeX 原文文本
     （宁可显示源码，不写崩溃 OMML）。

历史盲区（本文件 D 组留痕）：`test_docx_math.py::test_fraction_structure`
旧断言 `"<m:num>" in xml` 对空标签 `<m:num></m:num>` **vacuous 通过** ——
只查标签存在、不查内容，是 15 条单测全绿却漏掉此缺陷的原因。
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
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

#: 空 OMML 必需参数元素（num/den 属 m:f；e 属 m:rad/m:nary；deg 属 m:rad）
_EMPTY_ARG_RE = re.compile(
    r"<m:(num|den|e|deg)(?:\s[^>]*)?>\s*</m:\1>|<m:(num|den|e|deg)(?:\s[^>]*)?/>")


def _omml_of(latex: str) -> str:
    return docx_math.latex_to_omml(latex)


def _text_of(el) -> str:
    return "".join(el.itertext())


# ---------------------------------------------------------------------------
# A 组 · 根因修复：frac / rad / nary 内容必须保留（不再静默清空）
# ---------------------------------------------------------------------------

class TestRootCauseContentPreserved:
    """公式内容不再被静默丢弃（修复前所有分式/根号/求和上下限全空）。"""

    def test_fraction_num_den_keep_content(self):
        xml = _omml_of(r"\frac{N}{A}")
        m = re.search(r"<m:num>(.*?)</m:num>", xml, re.S)
        d = re.search(r"<m:den>(.*?)</m:den>", xml, re.S)
        assert m and d, xml
        assert "N" in _text_of(etree.fromstring(
            f"<x xmlns:m='{M_NS}'>{m.group(1)}</x>"))
        assert "A" in _text_of(etree.fromstring(
            f"<x xmlns:m='{M_NS}'>{d.group(1)}</x>"))

    def test_fraction_with_sub_script(self):
        xml = _omml_of(r"\lambda = \frac{l_0}{i}")
        num = re.search(r"<m:num>(.*?)</m:num>", xml, re.S).group(1)
        den = re.search(r"<m:den>(.*?)</m:den>", xml, re.S).group(1)
        num_txt = _text_of(etree.fromstring(f"<x xmlns:m='{M_NS}'>{num}</x>"))
        den_txt = _text_of(etree.fromstring(f"<x xmlns:m='{M_NS}'>{den}</x>"))
        assert "l" in num_txt and "0" in num_txt
        assert "i" in den_txt

    def test_radical_keeps_radicand(self):
        # ✅ R126（2026-10-21）：组内脚本附着后，被开方数内部的 x^2/y^2 成为
        #   嵌套 m:sSup/m:e，旧「首个 <m:e>(.*?)</m:e> 切片」会截到不平衡片段
        #   （lxml 标签失配报错）。断言语义不变（被开方数字符不丢、在 m:rad 内），
        #   改为整树解析后定位 m:rad 取 itertext。
        xml = _omml_of(r"\sqrt{x^2 + y^2}")
        root = etree.fromstring(xml)
        rad = root.find(f".//{{{M_NS}}}rad")
        assert rad is not None, xml
        txt = _text_of(rad)
        assert "x" in txt and "y" in txt

    def test_nary_limits_and_body_kept(self):
        """nary 上下限与body内容保留。

        历史局限（R122 留痕时）：nary body 内的 `x_i` 上下标会被拍平为 "xi"
        （`_attach_scripts` 只在顶层 parse 执行，nary 体内不附着）。
        ✅ R126 FIX-C 已修复：body 产物统一过 _attach_scripts，`x_i` 现为真
        m:sSub；旧「首个 <m:e>(.*?)</m:e> 切片」对嵌套结构会截到不平衡片段，
        改整树解析。断言只紧「字符不丢」的崩溃收口初衷，结构正确性由
        test_r126_docx_math_20261021.py 内容级护栏接管。
        """
        xml = _omml_of(r"\sum_{i=1}^{n} x_i")
        root = etree.fromstring(xml)
        nary = root.find(f".//{{{M_NS}}}nary")
        assert nary is not None, xml
        sub_el = nary.find(f"{{{M_NS}}}sub")
        sup_el = nary.find(f"{{{M_NS}}}sup")
        body_el = nary.find(f"{{{M_NS}}}e")
        assert sub_el is not None and sup_el is not None and body_el is not None, xml
        assert "i=1" in _text_of(sub_el)
        assert "n" in _text_of(sup_el)
        body_txt = _text_of(body_el)
        assert "x" in body_txt and "i" in body_txt

    @pytest.mark.parametrize("latex", [
        r"\frac{N}{\phi A} + \frac{M_w}{W} \le f",
        r"M_w = \frac{1.4 W_k l_a h^2}{10}",
        r"\sigma = \frac{18.5 \times 10^3}{0.0225} = 822.2 kPa",
        r"\left( \frac{a}{b} \right)^2",
        r"\sqrt{x^2 + y^2}",
        r"\sum_{i=1}^{n} x_i",
        r"N = 1.2 N_{Gk} + 1.4 \sum N_{Qk}",
        r"\lambda = \frac{l_0}{i}",
    ])
    def test_no_empty_arg_elements(self, latex):
        """决定性护栏：产物不得含任何空参数元素（崩溃根因 ③）。"""
        xml = _omml_of(latex)
        assert not _EMPTY_ARG_RE.search(xml), (
            f"{latex!r} 仍产出空 OMML 参数元素: {xml}")

    def test_block_formula_display_also_clean(self):
        xml = docx_math.latex_to_omml_display(
            r"$$\sigma' = \frac{18.5}{0.9} = 20.56 kPa$$")
        assert "<m:oMathPara" in xml and "center" in xml
        assert not _EMPTY_ARG_RE.search(xml)


# ---------------------------------------------------------------------------
# B 组 · 防护网：残式公式拒绝产出 schema 非法 OMML（回退原文文本）
# ---------------------------------------------------------------------------

class TestGuardRejectsEmptyArgs:
    """空花括号/裸命令等残式 → ValueError → 调用方回退 LaTeX 原文。

    ✅ R126 FIX-C2（2026-10-21）：裸 `\\sum` 从拒绝清单移出 —— 无界 nary 改为
    Word 原生形态（naryPr subHide/supHide + 自闭合 `<m:sub/>`），schema 合法、
    无需再整条回退；见下方 test_bare_nary_renders_as_unbounded_operator。
    """

    @pytest.mark.parametrize("latex", [
        r"\frac{}{}",
        r"\frac{a}{}",
        r"\frac{f_{ak}}{}",
        r"\sqrt{}",
    ])
    def test_guard_raises(self, latex):
        with pytest.raises(ValueError, match="空参数元素"):
            _omml_of(latex)

    def test_bare_nary_renders_as_unbounded_operator(self):
        """裸 `\\sum`（无界无体）→ Word 原生无界 nary，不再回退源码文本。"""
        xml = _omml_of(r"\sum")
        root = etree.fromstring(xml)
        nary = root.find(f".//{{{M_NS}}}nary")
        assert nary is not None, xml
        pr = nary.find(f"{{{M_NS}}}naryPr")
        assert pr is not None
        assert pr.find(f"{{{M_NS}}}subHide") is not None
        assert pr.find(f"{{{M_NS}}}supHide") is not None
        # 自闭合空界（Word 同款）+ 被和式含空参 run（schema 合法非空 CT_OMathArg）
        sub_el = nary.find(f"{{{M_NS}}}sub")
        sup_el = nary.find(f"{{{M_NS}}}sup")
        body_el = nary.find(f"{{{M_NS}}}e")
        assert sub_el is not None and len(sub_el) == 0
        assert sup_el is not None and len(sup_el) == 0
        assert body_el is not None and len(body_el) >= 1
        # 分式/根号等真违规仍不得借道漏出
        assert not _EMPTY_ARG_RE.search(xml), xml

    def test_guard_covers_display(self):
        with pytest.raises(ValueError, match="空参数元素"):
            docx_math.latex_to_omml_display(r"\frac{}{}")

    def test_guard_does_not_touch_valid_formulas(self):
        """完整公式不触发防护（加法式、零行为变更）。"""
        for latex in (r"\frac{N}{A}", r"\sqrt{2}", r"\sum_{i=1}^{n} x"):
            assert _omml_of(latex)  # 不抛错即通过


# ---------------------------------------------------------------------------
# C 组 · 导出链端到端：走 export._add_runs_with_inline_format 落 docx
# ---------------------------------------------------------------------------

def _docx_document_xml(paragraph_text: str) -> etree._Element:
    doc = Document()
    para = doc.add_paragraph()
    _add_runs_with_inline_format(para, paragraph_text)
    buf = io.BytesIO()
    doc.save(buf)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        return etree.fromstring(zf.read("word/document.xml"))


class TestExportChainEndToEnd:
    """经真实导出链产出的 docx 不含空 OMML 参数元素（WPS 不再崩溃）。"""

    def test_exported_docx_has_no_empty_omml_args(self):
        root = _docx_document_xml(
            r"稳定性验算：$\sigma = \frac{N}{\phi A} \le f$，"
            r"其中 $N = 1.2 N_{Gk} + 1.4 \sum N_{Qk}$，"
            r"根号项 $\sqrt{x^2 + y^2}$ 成立。")
        for tag in ("num", "den", "e", "deg"):
            for el in root.iter(f"{{{M_NS}}}{tag}"):
                assert len(el) > 0, f"导出 docx 仍含空 m:{tag}"
        # 内容确实进了 docx
        txt = "".join(root.itertext())
        assert "N" in txt and "φ" in txt

    def test_exported_docx_fraction_content_present(self):
        root = _docx_document_xml(r"计算 $\frac{18.5}{0.0225} = 822.2 kPa$")
        nums = list(root.iter(f"{{{M_NS}}}num"))
        assert len(nums) == 1
        assert "18.5" in _text_of(nums[0])
        assert "0.0225" in _text_of(list(root.iter(f"{{{M_NS}}}den"))[0])

    def test_broken_formula_falls_back_to_source_text(self):
        """残式公式回退为 LaTeX 原文（不丢内容、不崩 WPS）。"""
        root = _docx_document_xml(r"残式 $\frac{}{}$ 与 $\frac{a}{}$ 保留原文。")
        body = root.find(f"{{{W_NS}}}body")
        txt = "".join(body.itertext())
        assert r"\frac{}{}" in txt and r"\frac{a}{}" in txt
        for tag in ("num", "den"):
            assert not list(root.iter(f"{{{M_NS}}}{tag}"))


# ---------------------------------------------------------------------------
# D 组 · 历史盲区留痕：旧断言的 vacuous 形态必须被新护栏替换
# ---------------------------------------------------------------------------

class TestLegacyAssertionBlindSpot:
    """旧 `assert "<m:num>" in xml` 对空标签也通过 —— 本组钉死内容级断言。"""

    def test_vacuous_tag_presence_is_not_enough(self):
        """证明：只查标签存在的断言抓不到本缺陷（教学性留痕）。"""
        empty_xml = "<m:num></m:num>"
        assert "<m:num>" in empty_xml  # 旧断言形态：vacuous 通过
        assert _EMPTY_ARG_RE.search(empty_xml)  # 新护栏：抓到

    def test_content_level_assertion_on_fraction(self):
        xml = _omml_of(r"\frac{a}{b}")
        assert re.search(r"<m:num><m:r><m:t[^>]*>a</m:t>", xml), xml
        assert re.search(r"<m:den><m:r><m:t[^>]*>b</m:t>", xml), xml
