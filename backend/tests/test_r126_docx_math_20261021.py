# -*- coding: utf-8 -*-
"""R126 护栏：docx_math 公式解析两缺陷（B: \\sqrt[n] 根号结构 / C: 组内脚本丢失）。

取证（artifacts/r126/probe_math_converters.py，2026-10-21 实测）：
  B) `\\sqrt[3]{x}` 旧产出 `<m:rad><m:e>[3]</m:e></m:rad>` + 游离 `x` run——
     被开方数在根号外、m:e 内容是度数文本，**结构错误**（比 R122 留痕的
     「度数未进 m:deg」更重）。根因：_TOKEN_RE 的 brace 类只含 {}，`[` 落
     char 分支，_parse_optional_bracket 的判据永不成立（死代码）。
  C) `\\sum_{i=1}^{n} x_i` 的 nary body、`\\frac{x_i}{y_j}` 的参数组、
     `a_{b_c}` 的脚本组、`{x_i}` 的 brace 组内，`_parse_seq` 只产
     script_marker 占位而这些路径**不经** _attach_scripts ⇒
     _node_to_omml 对 script_marker 无分支 → return "" ⇒ 上下标静默丢失。

本文件全部为**内容级**断言（兑现 R122 判据①「只查标签存在的断言是 vacuous 护栏」）：
不只断 m:deg/m:sSub 标签存在，还断言其**内部装的是正确内容**。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import docx_math  # noqa: E402


def _omml(latex: str) -> str:
    return docx_math.latex_to_omml(latex)


def _plain(xml: str) -> str:
    """剥掉全部标签后的可见字符序列（断言「内容没丢/没错位」）。"""
    return re.sub(r"<[^>]+>", "", xml)


# ---------------------------------------------------------------------------
# B) \\sqrt[n]{x}：度数必须进 m:deg，被开方数必须进 m:e
# ---------------------------------------------------------------------------

def test_sqrt_index_degree_inside_mdeg():
    xml = _omml(r"\sqrt[3]{x}")
    m_deg = re.search(r"<m:deg>(.*?)</m:deg>", xml, re.S)
    assert m_deg is not None, f"未产出 m:deg（度数丢失/根号结构错误）: {xml}"
    deg = _plain(m_deg.group(1))
    assert "3" in deg, f"m:deg 内容不含度数 3: {deg!r}"
    # 关键反证：旧实现把 "[3]" 当被开方数塞进 m:e，m:deg 根本不存在；
    # 修复后 m:deg 内也不得残留方括号字符。
    assert "[" not in deg and "]" not in deg, f"m:deg 残留方括号: {deg!r}"


def test_sqrt_index_radical_contains_radicand():
    xml = _omml(r"\sqrt[3]{x}")
    # 带度数分支的 rad 结构为 <m:rad><m:radPr>…<m:deg>…<m:e>radicand</m:e>
    m_e = re.search(r"<m:deg>.*?</m:deg>\s*<m:e>(.*?)</m:e>", xml, re.S)
    assert m_e is not None, f"m:e 未紧随 m:deg（rad 结构错位）: {xml}"
    e = _plain(m_e.group(1))
    assert "x" in e, f"被开方数 x 不在 m:e 内（游离在根号外）: {e!r}"
    assert "[" not in e, f"m:e 混入度数方括号: {e!r}"


def test_sqrt_index_multi_char_radicand():
    xml = _omml(r"\sqrt[3]{x+y}")
    m_e = re.search(r"<m:deg>.*?</m:deg>\s*<m:e>(.*?)</m:e>", xml, re.S)
    assert m_e is not None, xml
    e = _plain(m_e.group(1))
    for ch in "xy+":
        assert ch in e, f"被开方数字符 {ch!r} 丢失: {e!r}"


def test_plain_sqrt_structure_unchanged():
    """无度数的 \\sqrt{x} 逐字保持旧结构（无 radPr/degHide 分支）。"""
    xml = _omml(r"\sqrt{x^2 + y^2}")
    assert "<m:rad><m:e>" in xml, xml
    assert "degHide" not in xml, "无度数根号被误入度数分支"
    # 既有能力回归：组内脚本（x^2）在 R122 修复后本就正常，不得因本轮改动漂移
    assert "<m:sSup>" in xml


# ---------------------------------------------------------------------------
# C) 组内脚本附着：nary body / frac 参数组 / 脚本组 / brace 组
# ---------------------------------------------------------------------------

def test_nary_body_scripts_kept():
    """\\sum_{i=1}^{n} x_i —— body 里的 x_i 必须成 m:sSub，而非拍平丢内容。"""
    xml = _omml(r"\sum_{i=1}^{n} x_i")
    assert "<m:nary>" in xml
    m_body = re.search(r"<m:nary>.*?<m:e>(.*)</m:e></m:nary>", xml, re.S)
    assert m_body is not None, xml
    body = m_body.group(1)
    assert "<m:sSub>" in body, f"nary body 内 x_i 未附着为 m:sSub: {body}"
    m_sub = re.search(r"<m:sSub><m:e>(.*?)</m:e><m:sub>(.*?)</m:sub></m:sSub>", body, re.S)
    assert m_sub is not None, f"body sSub 结构不完整: {body}"
    assert "x" in _plain(m_sub.group(1)), f"base 丢 x: {m_sub.group(1)}"
    assert "i" in _plain(m_sub.group(2)), f"下标丢 i（旧实现 script_marker 无分支→''）: {m_sub.group(2)}"


def test_frac_arg_scripts_kept():
    """\\frac{x_i}{y_j} —— num/den 内的上下标不得丢失。"""
    xml = _omml(r"\frac{x_i}{y_j}")
    m_num = re.search(r"<m:num>(.*?)</m:num>", xml, re.S)
    m_den = re.search(r"<m:den>(.*?)</m:den>", xml, re.S)
    assert m_num is not None and m_den is not None, xml
    assert "<m:sSub>" in m_num.group(1), f"m:num 内 x_i 未附着: {m_num.group(1)}"
    assert "<m:sSub>" in m_den.group(1), f"m:den 内 y_j 未附着: {m_den.group(1)}"
    assert "i" in _plain(m_num.group(1)) and "j" in _plain(m_den.group(1))


def test_script_arg_nested_group_scripts_kept():
    """a_{b_c} —— 脚本参数组内的再层脚本必须附着（旧实现拍平成 'bc' 语义错乱）。"""
    xml = _omml(r"a_{b_c}")
    assert "<m:sSub>" in xml
    # 外层 sSub 的 m:sub 里应还有内层 sSub
    m_outer = re.search(r"<m:sSub><m:e>(.*?)</m:e><m:sub>(.*?)</m:sub></m:sSub>", xml, re.S)
    assert m_outer is not None, xml
    assert "<m:sSub>" in m_outer.group(2), f"脚本组内层 b_c 未附着: {m_outer.group(2)}"


def test_brace_group_scripts_kept():
    """{x_i} 独立 brace 组：seq 节点内脚本必须附着。"""
    xml = _omml(r"E = {x_i} + {y_j}")
    assert xml.count("<m:sSub>") >= 2, f"brace 组内脚本附着不足两处: {xml}"


def test_dangling_bracket_text_survives():
    """未配对的 [ ] 作普通文本处理：x \\in [0,1] 的方括号字符不得丢失。"""
    xml = _omml(r"x \in [0,1]")
    plain = _plain(xml)
    for ch in "x[0,1]":
        assert ch in plain, f"可见字符 {ch!r} 丢失: {plain!r}"


# ---------------------------------------------------------------------------
# 反向锚：修复不得虚过——空断言防呆（若 _attach_scripts 被摘掉，上面全红）
# ---------------------------------------------------------------------------

def test_probe_parity_content_nonempty():
    """守卫本文件断言链本身有效：全部用例的 OMML 可见内容必须非空。"""
    for latex in (r"\sqrt[3]{x}", r"\sum_{i=1}^{n} x_i", r"\frac{x_i}{y_j}", r"a_{b_c}"):
        assert _plain(_omml(latex)).strip(), f"{latex}: OMML 可见内容为空（vacuous）"


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
