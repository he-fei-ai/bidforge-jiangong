# -*- coding: utf-8 -*-
"""docx_math 模块单测（pytest 风格）：LaTeX->OMML 转换与乱码清理。"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import docx_math


# ---------------------------------------------------------------------------
# LaTeX -> OMML
# ---------------------------------------------------------------------------

FORMULA_CASES = [
    # (display, latex, 说明)
    (False, "l_a", "纯下标"),
    (False, "G_{k1}", "下标组"),
    (False, "N_{Qk}", "下标组2"),
    (True, r"N = 1.2 N_{Gk} + 1.4 \sum N_{Qk}", "块级求和"),
    (True, r"\lambda = \frac{l_0}{i}", "块级分式"),
    (True, r"\frac{N}{\phi A} + \frac{M_w}{W} \le f", "稳定性公式"),
    (False, r"M_w = \frac{1.4 W_k l_a h^2}{10}", "分式+上下标"),
    (True, r"\sigma = \frac{18.5 \times 10^3}{0.0225} = 822.2 kPa", "数值分式"),
    (False, r"f = K_c \times f_{ak} = 1.0 \times 120 = 120kPa", "乘号"),
    (False, r"f_t = 1.43 N/mm^2", "单位上标"),
    (False, r"\phi48.3 \times 3.6", "希腊+数值"),
    (False, r"A' = 0.6m \times 1.5m = 0.9m^2", "平方"),
    (False, r"\sqrt{x^2 + y^2}", "根号"),
    (False, r"\sum_{i=1}^{n} x_i", "求和上下限"),
    (False, r"\left( \frac{a}{b} \right)^2", "定界符+分式"),
    (True, r"$$\sigma' = \frac{18.5}{0.9} = 20.56 kPa$$", "块级带撇号"),
]


def test_latex_to_omml_all_cases():
    for disp, latex, _ in FORMULA_CASES:
        xml = docx_math.latex_to_omml_display(latex) if disp else docx_math.latex_to_omml(latex)
        assert "<m:oMath" in xml, f"{latex}: 缺少 oMath"
        assert "\\frac" not in xml and "\\sum" not in xml, f"{latex}: 残留 LaTeX 命令"
        expected_tag = "oMathPara" if disp else "oMath"
        assert f"<m:{expected_tag}" in xml, f"{latex}: 缺少 {expected_tag}"


def test_block_formula_centered():
    xml = docx_math.latex_to_omml_display(r"\lambda = \frac{l_0}{i}")
    assert "<m:oMathParaPr>" in xml and "center" in xml


def test_fraction_structure():
    xml = docx_math.latex_to_omml(r"\frac{a}{b}")
    assert "<m:f><m:fPr><m:type m:val='bar'/></m:fPr>" in xml
    assert "<m:num>" in xml and "<m:den>" in xml


def test_subscript_structure():
    xml = docx_math.latex_to_omml("N_{Qk}")
    assert "<m:sSub><m:e>" in xml and "<m:sub>" in xml


def test_nary_structure():
    xml = docx_math.latex_to_omml(r"\sum_{i=1}^{n} x_i")
    assert "<m:nary>" in xml and "∑" in xml
    assert "<m:sub>" in xml and "<m:sup>" in xml


def test_greek_and_ops_unicode():
    xml = docx_math.latex_to_omml(r"\phi \le f")
    assert "φ" in xml and "≤" in xml


def test_unknown_command_dropped():
    """未知命令丢弃命令本身，不留反斜杠残留。"""
    xml = docx_math.latex_to_omml(r"\unknowncmd x")
    assert "\\" not in xml


# ---------------------------------------------------------------------------
# 乱码清理
# ---------------------------------------------------------------------------

CLEAN_CASES = [
    # ✅ 行为变更：поверх 是合法俄语单词（引用俄文标准时不得改写），保留原样
    ("应 поверх 添加更多敷料并继续加压。", "应 поверх 添加更多敷料并继续加压。"),
    ("abc\x00\x01def", "abcdef"),
    ("遇到\ufffd替换字符", "遇到 替换字符"),
    ("乱码锟斤拷锟斤拷结束", "乱码结束"),
    ("donÃ©e", "donée"),
    # ✅ 行为变更：裸 Â（合法字符）不再无条件删除；常见 mojibake Â° 仍修复
    ("温度 25Â°C", "温度 25°C"),  # mojibake Â° -> °
    ("法语词 déjà vu 与 Â 保留", "法语词 déjà vu 与 Â 保留"),
    ("正常中文≤≥±×÷²³㎡Φφμ°′″N·m45°~60°", "正常中文≤≥±×÷²³㎡Φφμ°′″N·m45°~60°"),
]


def test_clean_text_cases():
    for src, expect in CLEAN_CASES:
        assert docx_math.clean_text(src) == expect, f"{src!r} -> {docx_math.clean_text(src)!r} != {expect!r}"


def test_clean_text_normal_unchanged():
    normal = "脚手架专项施工方案：立杆纵距 1.5m，步距 1.8m，地基承载力≥80kPa。"
    assert docx_math.clean_text(normal) == normal


def test_clean_text_no_crash_on_empty():
    assert docx_math.clean_text("") == ""
    assert docx_math.clean_text(None if False else "x") == "x"


# ---------------------------------------------------------------------------
# 扫描与摘要
# ---------------------------------------------------------------------------

def test_iter_formulas_mixed():
    text = r"满足 $\frac{N}{\phi A}\le f$，且 $$\sigma = \frac{N}{A}$$ 成立"
    found = list(docx_math.iter_formulas(text))
    assert len(found) == 2
    assert found[0][0] is False and "frac" in found[0][1]
    assert found[1][0] is True and "sigma" in found[1][1]


def test_scan_summarize():
    text = (r"$\frac{a}{b}$ 应 поверх 添加，遇到" "\ufffd" "和锟斤拷")
    s = docx_math.scan_issues(text)
    assert s["formulas"] == 1
    assert s["cyrillic"] >= 1
    assert s["replacement_chars"] >= 1
    assert s["gbk_mojibake"] >= 1
    summary = docx_math.summarize(text)
    assert "公式" in summary


# ---------------------------------------------------------------------------
# 行内公式「悬空运算符」形态（2026-09-19 实测交付文档取证修复）
# ---------------------------------------------------------------------------
# 背景：AI 在计算书章节写成 `$p_{max} = $××kPa`（等式右端取值另附在公式外），
# 闭合 $ 前是空格。旧约束 (?<!\s)\$ 把整段判为非公式 → 交付文档里出现字面
# `$p_{max} = $`（LaTeX 源码残留）。现仅在"内容以悬空运算符 + 空白结尾"时放宽。
_DANGLING_CASES = [
    (r"经计算，立杆基础底面最大压力$p_{max} = $××kPa。", "p_{max} ="),
    (r"地基承载力设计值$f_a = $××kPa。", "f_a ="),
    (r"计算得$f_k = $××kN/m²。", "f_k ="),
    (r"基本风压取值为$w_0 = $××kN/m²。", "w_0 ="),
]


def test_inline_dollar_dangling_operator_recognized():
    for text, expect in _DANGLING_CASES:
        found = list(docx_math.iter_formulas(text))
        assert len(found) == 1, f"{text!r} 未识别为公式"
        disp, latex, m = found[0]
        assert disp is False and latex == expect, f"{text!r} -> {latex!r} != {expect!r}"
        # 转 OMML 后不得残留 LaTeX 源码
        xml = docx_math.latex_to_omml(latex)
        assert "<m:oMath" in xml and "$" not in xml


def test_inline_dollar_currency_still_not_formula():
    """货币/金额等普通文本仍不得被误判为公式（放宽不得回退旧误判）。"""
    assert list(docx_math.iter_formulas("单价 $100 与 $200 之间")) == []
    assert list(docx_math.iter_formulas("费用约 $5000 元")) == []


def test_inline_dollar_normal_still_recognized():
    """正常行内公式（闭 $ 前非空白）行为不变。"""
    found = list(docx_math.iter_formulas(r"公式 $l_{0} = \mu h$ 成立"))
    assert len(found) == 1 and found[0][1] == r"l_{0} = \mu h"


# ---------------------------------------------------------------------------
# 脚本直跑入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    failed = 0
    for fn in sorted(n for n in globals() if n.startswith("test_")):
        try:
            globals()[fn]()
            print(f"[OK] {fn}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {fn}: {e}")
            traceback.print_exc()
    print("全部通过" if failed == 0 else f"{failed} 项失败")
    sys.exit(1 if failed else 0)
