# -*- coding: utf-8 -*-
r"""R126（2026-10-21）· 「导出 DOCX 打不开」修复 护栏测试。

背景（用户取证 2026-10-21 22:03 WPS「文字程序意外中断」截图 + _r122_docx_crash_scan.py
对 data/_exports 全量扫描）：打不开的三类诱因与对应修复——

  D  · 缓存命中判据只查 size>0，写盘中断的**半截文件**（ZIP 中央目录缺失）
       仍被直接交付 ⇒ FIX-D：新增 `_artifact_intact`（DOCX ZIP 双签名：头
       `PK\x03\x04` + 尾部 64KB 内 EOCD `PK\x05\x06`）接入 export_docx 命中点，
       不通过即丢弃缓存走全量重建；**刻意不改** `_cache_row_viable`
       （prune 保留判据语义逐字不变，防存量清理行为漂移）。

  C2 · docx_math 三处 schema 违规/渲染缺陷（探针 artifacts/r126/probe_empty_args.py）：
       ① `x_{}` / `x^{}` 空脚本参数 ⇒ 空 `<m:sub></m:sub>`（CT_OMathArg 空、
          逃过 R122 校验正则）⇒ 丢弃空脚本，仅渲染基座；
       ② 无界 nary（`\sum x`）⇒ 空 `<m:sub></m:sub><m:sup></m:sup>`
          ⇒ Word 原生 subHide/supHide + `<m:sub/>` 自闭合形态；
       ③ `\lim` 落「未知命令丢弃」分支整词消失（sSub 基座空 run）
          ⇒ _OPS 映射为正体文本 run。
       另：空被和式（`\sum_{i=1}^{n}` 无后继）补 Word 同款空参 run，
       不再触发 R122 拒绝而回退 LaTeX 源码文本。

测试策略：内容级 lxml 结构断言（对照 R122 判据①「只查标签存在是 vacuous 护栏」）
+ `_artifact_intact` 单元判据 + 命中点/保留点接线源码锚。零 AI、零 DB。
"""
import io
import os
import re
import sys
import zipfile

import pytest
from lxml import etree

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.routers import export as E  # noqa: E402
from app.services import docx_math  # noqa: E402

M = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def _omml_tree(omml: str):
    return etree.fromstring(f'<m:oMath xmlns:m="{M}">{omml}</m:oMath>')


# ===========================================================================
# A. FIX-C2 ①：空脚本参数丢弃（x_{}/x^{} 不得产出空 m:sub/m:sup）
# ===========================================================================
class TestEmptyScriptArgDropped:
    @pytest.mark.parametrize("latex", ["x_{}", "x^{}", r"G_{}", r"N^{}"])
    def test_no_empty_script_elements(self, latex):
        """空脚本参数不得产出任何 sSub/sSup 或空 sub/sup 元素。"""
        xml = docx_math.latex_to_omml(latex)
        root = _omml_tree(xml)
        assert not root.findall(f".//{{{M}}}sSub"), f"{latex}: 空 sSub 残留"
        assert not root.findall(f".//{{{M}}}sSup"), f"{latex}: 空 sSup 残留"
        assert not root.findall(f".//{{{M}}}sub"), f"{latex}: 空 sub 残留"
        assert not root.findall(f".//{{{M}}}sup"), f"{latex}: 空 sup 残留"
        # 基座内容必须保留（x/G/N 可见字符不丢）
        text = "".join(root.itertext())
        assert latex[0] in text, f"{latex}: 基座字符丢失"

    def test_trailing_lone_script_dropped(self):
        """尾随孤立脚本（`x_` 无参）同样丢弃，产出纯文本 run。"""
        xml = docx_math.latex_to_omml("x_")
        root = _omml_tree(xml)
        assert not root.findall(f".//{{{M}}}sSub")
        assert "".join(root.itertext()) == "x"

    def test_real_script_still_attached(self):
        """非空脚本（x_1）必须保持 sSub 附着——护栏防「一刀切全丢」回归。"""
        xml = docx_math.latex_to_omml("x_1")
        root = _omml_tree(xml)
        ssub = root.findall(f".//{{{M}}}sSub")
        assert len(ssub) == 1, "非空下标被误删（过度修复）"
        assert "1" in "".join(ssub[0].find(f"{{{M}}}sub").itertext())


# ===========================================================================
# B. FIX-C2 ②：无界 nary 的 Word 原生 hide 形态
# ===========================================================================
class TestNaryHiddenBounds:
    def test_bare_sum_uses_word_native_hide_shape(self):
        r"""\sum 无界：naryPr 必须带 subHide/supHide，sub/sup 为自闭合空元素。"""
        root = _omml_tree(docx_math.latex_to_omml(r"\sum x"))
        nary = root.find(f".//{{{M}}}nary")
        assert nary is not None
        npr = nary.find(f"{{{M}}}naryPr")
        assert npr.find(f"{{{M}}}subHide") is not None, "缺 subHide（回退空 sub 形态）"
        assert npr.find(f"{{{M}}}supHide") is not None, "缺 supHide"
        sub = nary.find(f"{{{M}}}sub")
        sup = nary.find(f"{{{M}}}sup")
        assert sub is not None and len(sub) == 0, "sub 应为自闭合空元素"
        assert sup is not None and len(sup) == 0, "sup 应为自闭合空元素"
        # 被和式内容保留
        assert "x" in "".join(nary.find(f"{{{M}}}e").itertext())

    def test_bounded_sum_keeps_wrapped_bounds(self):
        r"""\sum_{i=1}^{n} x_i：上下限必须包在 m:sub/m:sup 内（裸 run 拍平即红）。"""
        root = _omml_tree(docx_math.latex_to_omml(r"\sum_{i=1}^{n} x_i"))
        nary = root.find(f".//{{{M}}}nary")
        npr = nary.find(f"{{{M}}}naryPr")
        assert npr.find(f"{{{M}}}subHide") is None, "有下限却带 subHide"
        assert npr.find(f"{{{M}}}supHide") is None, "有上限却带 supHide"
        sub = nary.find(f"{{{M}}}sub")
        sup = nary.find(f"{{{M}}}sup")
        assert sub is not None and len(sub) > 0, "m:sub 缺失或空（裸 run 拍平）"
        assert "i=1" in "".join(sub.itertext()), "下限内容丢失"
        assert sup is not None and "n" in "".join(sup.itertext()), "上限内容丢失"


# ===========================================================================
# C. FIX-C2 ③：空被和式补 Word 同款空参 run（不再回退源码文本）
# ===========================================================================
class TestNaryEmptyBody:
    def test_sum_with_bounds_but_no_body_still_renders_omml(self):
        r"""\sum_{i=1}^{n}（无被和式）：必须产出合法 OMML 而非抛错回退源码。"""
        xml = docx_math.latex_to_omml(r"\sum_{i=1}^{n}")
        root = _omml_tree(xml)
        nary = root.find(f".//{{{M}}}nary")
        assert nary is not None, "空体 nary 整条丢失（回退源码文本）"
        body = nary.find(f"{{{M}}}e")
        assert body is not None and len(body) > 0, (
            "m:e 无子元素——CT_OMathArg 空参（WPS 崩溃形态）")

    def test_lim_base_not_lost(self):
        r"""\lim_{x \to 0}：基座必须含「lim」文本（整词消失即红）。"""
        root = _omml_tree(docx_math.latex_to_omml(r"\lim_{x \to 0}"))
        ssub = root.find(f".//{{{M}}}sSub")
        assert ssub is not None, "lim 下标结构丢失"
        base = ssub.find(f"{{{M}}}e")
        assert "lim" in "".join(base.itertext()), (
            "lim 基座为空——命令被「未知命令丢弃」分支吞掉")

    def test_common_operator_names_rendered(self):
        r"""max/min/log/sin 等常用算子名不再走「未知命令丢弃」。"""
        for name in ("max", "min", "log", "sin", "exp"):
            xml = docx_math.latex_to_omml(rf"\{name}_{{i}} v")
            assert name in xml, rf"\{name} 整词丢失"


# ===========================================================================
# D. R122 防护网语义保持：真残式仍回退（不因本轮放宽而放行空参）
# ===========================================================================
class TestR122GuardUnchanged:
    @pytest.mark.parametrize("latex", [r"\frac{}{}", r"\frac{1}{}", r"\sqrt{}", r"\sqrt[3]{}"])
    def test_empty_frac_rad_still_rejected(self, latex):
        with pytest.raises(ValueError, match="空参数元素"):
            docx_math.latex_to_omml(latex)


# ===========================================================================
# E. FIX-D：_artifact_intact 产物完整性判据
# ===========================================================================
def _make_minimal_docx(path):
    """构造带完整 EOCD 的最小合法 zip（DOCX 头签名形态）。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document/>")


class TestArtifactIntact:
    def test_valid_docx_passes(self, tmp_path):
        p = tmp_path / "ok.docx"
        _make_minimal_docx(p)
        assert E._artifact_intact(str(p)) is True

    def test_truncated_no_eocd_rejected(self, tmp_path):
        """半截文件：头签名在、EOCD 缺（写盘中断的典型形态）必须拒绝。"""
        p = tmp_path / "trunc.docx"
        _make_minimal_docx(p)
        data = p.read_bytes()
        p.write_bytes(data[: len(data) // 2])  # 截掉尾部（含 EOCD）
        assert E._artifact_intact(str(p)) is False

    def test_zero_byte_rejected(self, tmp_path):
        p = tmp_path / "empty.docx"
        p.write_bytes(b"")
        assert E._artifact_intact(str(p)) is False

    def test_tiny_below_signature_floor_rejected(self, tmp_path):
        p = tmp_path / "tiny.docx"
        p.write_bytes(b"PK\x03\x04")  # 仅头签名，不足最低下界
        assert E._artifact_intact(str(p)) is False

    def test_non_zip_content_rejected(self, tmp_path):
        p = tmp_path / "fake.docx"
        p.write_bytes(b"not a zip file at all" * 10)
        assert E._artifact_intact(str(p)) is False

    def test_missing_file_rejected(self, tmp_path):
        assert E._artifact_intact(str(tmp_path / "nope.docx")) is False

    def test_empty_path_rejected(self):
        assert E._artifact_intact("") is False

    def test_header_only_zip_rejected(self, tmp_path):
        """头签名正确但中央目录被覆写损坏（EOCD 缺）——FAT 表损坏形态。"""
        p = tmp_path / "corrupt.docx"
        _make_minimal_docx(p)
        data = bytearray(p.read_bytes())
        eocd = data.rfind(b"PK\x05\x06")
        data[eocd:eocd + 4] = b"XXXX"  # 覆写 EOCD 签名
        p.write_bytes(bytes(data))
        assert E._artifact_intact(str(p)) is False


# ===========================================================================
# F. 接线锚：命中点叠加完整性校验；prune 保留判据逐字不变
# ===========================================================================
def _code_only(src: str) -> str:
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in src.splitlines())


class TestWiringAnchors:
    def test_export_docx_hit_point_requires_artifact_intact(self):
        """export_docx 缓存命中分支必须叠加 _artifact_intact 校验。"""
        import inspect
        body = _code_only(inspect.getsource(E.export_docx))
        i_hit = body.index('X-Cache-Status": "hit"')
        hit_block = body[:i_hit]
        assert "_artifact_intact(" in hit_block, (
            "DOCX 命中点未接 _artifact_intact——半截缓存文件将直接交付")

    def test_prune_still_uses_row_viable_only(self):
        """prune 保留判据逐字不变：只认 _cache_row_viable（FIX-D 不动保留语义）。"""
        import inspect
        body = _code_only(inspect.getsource(E._prune_export_cache))
        assert "_cache_row_viable" in body, "prune 保留判据漂移"
        assert "_artifact_intact" not in body, (
            "FIX-D 泄漏进 prune——截断缓存行会被批量清理，违反「只收紧命中判据」约定")

    def test_intact_reject_does_not_return_hit(self):
        """命中分支：intact 不通过时不得 return FileResponse（必须落到重建链）。"""
        import inspect
        body = _code_only(inspect.getsource(E.export_docx))
        i_call = body.index("_artifact_intact(")
        i_hit_ret = body.index('X-Cache-Status": "hit"')
        between = body[i_call:i_hit_ret]
        # 调用点到命中 return 之间必须是 else 分支结构（拒绝路径无交付）
        assert "else:" in between, "intact 拒绝路径结构漂移"
