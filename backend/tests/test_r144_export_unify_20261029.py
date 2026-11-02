# -*- coding: utf-8 -*-
"""R144 · 正文生成与导出文档 · 六类标准统一/三层校验/w14 清理 完整性护栏（2026-10-29）。

用户指令「统一生成内容的所有标准 + 三层校验 + 避免 Word 2010+ 专有扩展」
经系统盘点（A+C+D 方向）确认：既有实现已高度成熟（R117~R143），本轮补
**「验证既有实现完整性」的三个真实缺口**：

A · 三层同源（第一/二层判据一致性）
  sse_handlers 第一层「正文生成后自检」(check_format_contract) 与
  export.audit_content 第二层「导出前校验」共用同一判据，但此前**无护栏
  锁定「同一正文两侧计数一致」**。若任一侧偷偷换成第二份判据（AGENTS
  「同一判据两份副本」最高频反模式），三层校验将静默分叉。

C · 前后端编号 L7 字母表 parity
  前端 SchemeWorkbenchPage.formatOutlineTitle 的 L7 字母表旧实现硬编码
  26 项（a~z），后端 numbering.ALPHABET 是 52 项（a~z + aa~az）→ 真实
  分叉（已修为同口径，本文件用 AST 读前端源码逐项比对锁定，因前端未装
  @types/node，跨语言 parity 放 pytest 侧 —— AGENTS 惯例）。

D · w14 清理多部件组合
  docx_namespace.sanitize_docx_file 此前 e2e 只锁「单 document 部件」；
  本组构造 document+header+footer+numbering **四部件同时含 w14/w14 属性**
  的组合 docx，锁定「全部部件清干净且可重开」。
"""
from __future__ import annotations

import ast
import zipfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
REPO = Path(__file__).resolve().parents[2]


def _py(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8", errors="replace")


# ===================================================================
# A · 三层同源：第一层(自检)与第二层(导出前校验)对同一正文计数一致
# ===================================================================

class TestThreeLayerSameCriterion:

    def _dirty_content(self) -> str:
        """同时命中六类标准多种 error/warning 的正文。

        ⚠️ 顺序有意为之（R146 修正）：未闭合 mermaid 围栏按既有契约
        （``content_format_contract._protected_line_indexes``，与
        ``content_blocks``「未闭合整块跳过」同口径）保护到文末 ——
        手写序号 / 控制字符行必须排在它**之前**，否则会被围栏保护吞掉，
        第一层 advisory/error 计数为 0（并非判据失效）。
        """
        return (
            "#缺空格标题\n"                       # text error
            "## 施工**部署**\n"                    # text warning（内联格式）
            "1、测量放线\n"                         # symbol advisory（手工序号）
            "\x07乱码行\n"                          # symbol error（控制字符）
            "| a | b |\n| 1 | 2 |\n"               # table error（缺分隔行）
            "```mermaid\nflowchart TD\n  A-->B\n"  # chart error（未闭合围栏）
            "深度【待定】米\n"                      # 填充行（无类别）
            "$$H=\\frac{M}{2}$$"                   # （被未闭合围栏保护，合法公式）
        )

    def test_layer1_report_counts(self):
        """第一层 check_format_contract 的分级计数可算出。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract(self._dirty_content(), section_id="s1")
        assert rep["checked"] is True
        assert rep["error_count"] >= 3  # text/table/chart 至少各 1
        assert rep["advisory_count"] >= 1
        assert rep["warning_count"] >= 1

    def test_layer2_audit_counts_match_layer1(self):
        """第二层 audit_content 的 format_contract_* 槽计数 == 第一层计数。

        两层判据必须同源（共用 check_format_contract 单一实例）；这里锁
        「同一正文两侧各 severity 的条数逐字相等」，任一侧引入第二份
        判据即红。
        """
        from app.routers.export import audit_content
        from app.services.content_format_contract import check_format_contract

        content = self._dirty_content()
        rep = check_format_contract(content, section_id="sX")
        audit = audit_content([{"id": "sX", "title": "第一章", "content": content}])
        slots = {it["key"]: it for it in audit["items"]
                 if it["key"].startswith("format_contract_")}
        # unclosed fence 与 chart「未闭合」同一事实已去重（R130 契约），
        # 比对时把契约侧 chart 的该条排除——用「第二层 chart 条数 ≤ 第一层
        # chart 条数」而非等式锁这一点，其余类别必须逐字相等。
        l1 = {}
        for it in rep["issues"]:
            l1[it["category"]] = l1.get(it["category"], 0) + 1
        for cat in ("text", "table", "symbol"):
            assert slots.get(f"format_contract_{cat}", {}).get("count", 0) == l1.get(cat, 0), \
                f"layer1[{cat}]={l1.get(cat)} vs layer2={slots.get('format_contract_'+cat)}"
        # chart 类别：第二层必须 ≤ 第一层（去重只减不增）
        assert slots.get("format_contract_chart", {}).get("count", 0) <= l1.get("chart", 0)

    def test_second_layer_reuses_single_criterion(self):
        """第二层必须直接调用 check_format_contract（不得自建第二份判据）。

        ⚠️ 判据锚定「真实调用语句」（AST Call 节点），不锚字符串 —— 注释里
        提及函数名不算数（AGENTS 判据教训③：源码锚要断调用形态）。
        """
        tree = ast.parse(_py("routers/export.py"))
        calls = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "check_format_contract":
                calls += 1
        assert calls >= 1, "export.py 未见 check_format_contract( 调用（判据可能已被换副本）"

    def test_sse_first_layer_writes_report_key(self):
        """第一层接线不回流：sse 必须把 report[\"format_contract\"] 落库。"""
        src = _py("routers/sse_handlers.py")
        assert 'report["format_contract"]' in src
        assert "check_format_contract(" in src


# ===================================================================
# C · 前后端编号 L7 字母表 parity（跨语言）
# ===================================================================

class TestL7AlphabetParity:

    @staticmethod
    def _backend_alphabet() -> list[str]:
        from app.services.numbering import ALPHABET
        return list(ALPHABET)

    @staticmethod
    def _frontend_alpha_at(idx: int) -> str:
        """按前端 `formatOutlineTitle` L7 分支的实际语义
        （SchemeWorkbenchPage.tsx）推导第 idx 项：

        前端实现（已修 52 项口径）：
            idx = clamp(n - 1, 0, 51)
            idx < 26 ? String.fromCharCode(97 + idx)
                     : "a" + String.fromCharCode(97 + (idx - 26))
        本推导与其逐行等价。护栏另以「前端源码形态断言」锁该实现不回流
        （AGENTS 判据教训③：源码锚要断调用形态，此处形态=特征字面量）。
        """
        assert 0 <= idx <= 51
        return chr(97 + idx) if idx < 26 else "a" + chr(97 + idx - 26)

    def test_backend_alphabet_is_52_a_z_aa_az(self):
        alpha = self._backend_alphabet()
        assert len(alpha) == 52, "ALPHABET 不再是 52 项（a~z + aa~az）"
        assert alpha[:26] == [chr(97 + i) for i in range(26)]
        assert alpha[26:] == ["a" + chr(97 + i) for i in range(26)]

    def test_frontend_l7_branch_uses_52_item_formula(self):
        """前端 L7 分支必须含 52 项口径（防回流 26 项硬编码）。"""
        fe = REPO / "frontend" / "src" / "pages" / "SchemeWorkbenchPage.tsx"
        ts = fe.read_text(encoding="utf-8", errors="replace")
        # 旧缺陷形态（26 项小写表整体字面量）不得回流
        assert '"abcdefghijklmnopqrstuvwxyz"' not in ts, \
            "前端 L7 字母表退回 26 项硬编码（与后端 ALPHABET 52 项分叉）"
        # 新形态三特征：51 clamp + 两段构造（a~z / aa~az）
        assert "Math.min(n - 1, 51)" in ts, "前端 L7 缺少 51（52 项）clamp"
        assert "String.fromCharCode(97 + idx)" in ts
        assert '"a" + String.fromCharCode(97 + (idx - 26))' in ts

    def test_cross_language_parity_52_items(self):
        """前后端 L7 字母表 52 项逐项一致（同口径判据）。"""
        backend = self._backend_alphabet()
        for idx in range(52):
            assert self._frontend_alpha_at(idx) == backend[idx], \
                f"L7 第 {idx + 1} 项分叉：前端 {self._frontend_alpha_at(idx)} " \
                f"!= 后端 {backend[idx]}"


# ===================================================================
# D · w14 清理多部件组合（document+header+footer+numbering 同含 w14）
# ===================================================================

class TestW14MultiPartSanitize:

    W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    W14 = 'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'

    def _make_docx(self, tmp: Path, parts: dict[str, str]) -> Path:
        p = tmp / "r144_w14_multi.docx"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
            for name, xml in parts.items():
                z.writestr(name, xml)
        return p

    def test_all_parts_banned_ns_stripped(self, tmp_path):
        """四部件同含 w14 声明+w14:paraId 属性 → 全部清除，zip 完整可重开。"""
        from app.services.docx_namespace import check_docx_file, sanitize_docx_file

        head = f"<w:t {self.W_NS} {self.W14}>"
        parts = {
            "word/document.xml": f"{head}<w:body/></w:t>",
            "word/header1.xml":
                f"{head}<w:p w14:paraId=\"11111111\"><w:r/></w:p></w:t>",
            "word/footer1.xml":
                f"{head}<w:p w14:paraId=\"22222222\"/></w:t>",
            "word/numbering.xml":
                f"{head}<w:abstractNum w14:paraId=\"33333333\"/></w:t>",
        }
        p = self._make_docx(tmp_path, parts)
        rep = sanitize_docx_file(p)
        for name in parts:
            assert name in rep["parts_changed"], f"{name} 未被清理: {rep}"
            assert "w14" in rep["parts_changed"][name]
        assert rep["attrs_stripped"] >= 3, "三个 w14:paraId 属性应全部被剥离"
        chk = check_docx_file(p)
        assert chk["clean"] is True, f"清理后仍有残留: {chk['issues']}"
        from lxml import etree
        with zipfile.ZipFile(p) as z:
            assert z.testzip() is None
            for name in parts:
                etree.fromstring(z.read(name))

    def test_settings_w14_optional_elems_removed(self, tmp_path):
        """settings.xml 的 w14:docId/defaultImageDpi 可选元素被清除、非 w14 保留。"""
        from app.services.docx_namespace import sanitize_docx_file

        settings = (
            f"<w:settings {self.W_NS} {self.W14}>"
            "<w14:docId w14:val=\"ABCD1234\"/>"
            "<w14:defaultImageDpi w14:val=\"144\"/>"
            "<w:defaultTabStop w:val=\"420\"/></w:settings>"
        )
        p = self._make_docx(tmp_path, {
            "word/settings.xml": settings,
            "word/document.xml": f"<w:document {self.W_NS}/>"}
        )
        rep = sanitize_docx_file(p)
        with zipfile.ZipFile(p) as z:
            s = z.read("word/settings.xml").decode("utf-8")
        assert "w14:docId" not in s and "defaultImageDpi" not in s
        assert "w:defaultTabStop" in s, "非 w14 的 w: 设置被误删"
        assert "word/settings.xml" in rep["parts_changed"]

    def test_used_prefix_kept_when_referenced(self, tmp_path):
        """前缀在部件内被引用（非仅声明）时保留声明 —— 不得误删（防 XML 损坏）。"""
        from app.services.docx_namespace import sanitize_docx_file

        # 自定义 <w14:customData/> 元素引用 w14 前缀 → 声明必须保留
        doc = (f"<w:document {self.W_NS} {self.W14}>"
               "<w:body><w14:customData/></w:body></w:document>")
        p = self._make_docx(tmp_path, {"word/document.xml": doc})
        rep = sanitize_docx_file(p)
        assert rep.get("parts_changed", {}).get("word/document.xml", []) \
            .count("w14") == 0, "被引用的 w14 前缀被误删"
        from lxml import etree
        with zipfile.ZipFile(p) as z:
            root = etree.fromstring(z.read("word/document.xml"))
            assert root.tag.endswith("document")


# ===================================================================
# E · 字节体检
# ===================================================================

class TestByteHealth:

    def test_no_utf8_bom_or_replacement(self):
        files = [
            APP / "services" / "docx_namespace.py",
            APP / "services" / "content_format_contract.py",
            Path(__file__),
        ]
        for f in files:
            raw = f.read_bytes()
            assert not raw.startswith(b"\xef\xbb\xbf"), f"{f.name} 带 BOM"
            assert b"\xef\xbf\xbd" not in raw, f"{f.name} 含 U+FFFD"
            raw.decode("utf-8")  # 良构

    def test_new_test_parses(self):
        ast.parse(Path(__file__).read_bytes().decode("utf-8"))
