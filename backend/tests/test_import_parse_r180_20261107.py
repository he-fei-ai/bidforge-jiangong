# -*- coding: utf-8 -*-
"""解析提取模块 · R180 回归护栏（2026-11-07）

覆盖本轮证伪并修复的四类「扩展名 / 文件头 / 编码」错配：

1. **UTF-16 文本被二进制防护误杀**（P0）
   - 现象：Windows 记事本「Unicode」保存的 .txt/.md/.csv 在公开入口
     `parse_file_content_ex` 里直接抛 `ParseError`（"疑似二进制"）。
   - 根因：`_reject_binary_text_ext` 先于 `_decode_text_ex` 执行，UTF-16
     的 NUL 字节命中 `_looks_binary`，而 utf-8-sig/gbk 二次确认必然失败。
   - 后果：R175 已实现的 `_detect_utf16` 能力在生产链路上不可达。

2. **文本扩展名下的强二进制头未被识别**（P1）
   - 现象：PDF 改名 .txt 后整篇 `%PDF-1.3 ... obj` 原文入库。

3. **OOXML（ZIP）容器的扩展名纠错缺失**（P1）
   - 现象：真实 .xlsx 改名 .xls → xlrd 报「XLS 已损坏」；
     真实 .xlsx 改名 .doc → Office 转换后输出 ZIP 原始字节乱码；
     真实 .docx 改名 .wps → 白跑一次 Office 转换。

4. **OLE 容器改名 .xlsx 未纠错**（P1）
   - 现象：ZIP 闸门直接报「XLSX 文件已损坏」，真实 .xls 无法解析。

5. **UTF-16 放行判据收窄**（R180-补）
   - 旧判据「可严格解码且无 NUL」判的是**解码后的文本**；但 UTF-16
     严格解码对「BOM + 随机合法码元」同样成功，5/5 构造样本曾被放行。
   - 现判据追加码点质量闸门（Cc/Cs/Co/Cn 占比 ≤ 2%，采样前 4096 字符），
     合法中英文/表格文本实测为 0。

6. **Excel 合并单元格 / 无缓存公式**（P2）
   - read_only 模式无 merged_cells：A1:B1 合并表头退化为「项目 | 空」，
     表头识别降级为「列1/列2」；现从 sheet XML 的 `<mergeCells>` 流式
     读取并展开左上角值，仍保持 `read_only=True`。
   - `data_only=True` 对无缓存公式返回 None（静默丢数）；现回填
     `[公式无缓存值] =A1+B1`，不允许输出为空。
"""

from __future__ import annotations

import io

import app.routers.upload_outline as uo
import pytest
from app.services import file_parser as fp
from starlette.datastructures import UploadFile


def _xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "清单"
    ws.append(["项目", "金额"])
    ws.append(["钢筋", 100])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _docx_bytes() -> bytes:
    from docx import Document

    d = Document()
    d.add_heading("工程概况", level=1)
    d.add_paragraph("测试正文")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _pdf_bytes() -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 700, "PDF content")
    c.save()
    return buf.getvalue()


_OLE_HEADER = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _ole_with_stream(name: str) -> bytes:
    return _OLE_HEADER + b"\x00" * 64 + name.encode("utf-16-le") + b"\x00" * 16


def _xlsx_with(setup) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "清单"
    setup(ws)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1 · UTF-16 文本 / CSV 必须可解析（不是「疑似二进制」）
# ---------------------------------------------------------------------------
class TestUtf16ThroughPublicEntry:
    @pytest.mark.parametrize("fname", ["a.txt", "a.md", "a.csv"])
    def test_utf16_bom_text_is_parsed(self, fname):
        payload = "工程概况\n建筑面积 1000 平方米\n".encode("utf-16")  # BOM + LE
        text, diag = fp.parse_file_content_ex(payload, fname)
        assert "工程概况" in text, "UTF-16 文本被二进制防护误杀"
        assert any("UTF-16" in w for w in diag["warnings"])

    def test_utf16le_csv_without_bom_is_parsed(self):
        # 无 BOM 探测只认 ASCII 为主的 UTF-16（纯 CJK 无 BOM 与 GBK 无法可靠
        # 区分，`_detect_utf16` 刻意不猜测），故此处用 ASCII 为主的 CSV。
        payload = "name,qty\nbolt,5\n".encode("utf-16-le")
        text, diag = fp.parse_file_content_ex(payload, "x.csv")
        assert "name" in text and "bolt" in text
        assert any("UTF-16" in w for w in diag["warnings"])

    @pytest.mark.parametrize("with_bom", [True, False])
    def test_utf16_mixed_cjk_ascii_roundtrip(self, with_bom):
        """用户指定样本：带 BOM / 不带 BOM 都必须成功且解码结果等于原文。

        判据说明：`_reject_binary_text_ext` 判的是 `content.decode(codec)`
        之后的**文本**（不是原始字节），因此 UTF-16LE 的 NUL 字节不会造成
        误杀；`_looks_binary` 只在 UTF-16 质量闸门不通过时才继续执行。
        """
        src = "工程概况 abc"
        payload = src.encode("utf-16-le")
        if with_bom:
            payload = b"\xff\xfe" + payload
        text, diag = fp.parse_file_content_ex(payload, "a.txt")
        assert text == src, f"UTF-16 解码结果与原文不一致: {text!r}"
        assert any("UTF-16" in w for w in diag["warnings"])


# ---------------------------------------------------------------------------
# 1b · 反例：整文件能被 UTF-16 严格解码的「二进制」不得放行
# ---------------------------------------------------------------------------
class TestUtf16QualityGateCounterExample:
    def test_strictly_decodable_binary_is_rejected(self):
        """构造「BOM + 私用区码元」样本：严格解码成功，但码点质量不达标。

        旧判据（仅要求「可严格解码且解码文本无 NUL」）会放行 —— 这正是
        R180-补 收窄前实测 5/5 被接受的那类样本。现必须抛 ParseError。
        """
        # U+E000..U+E027 为私用区（category=Co），UTF-16 编码不含 0x0000，
        # 严格解码必然成功；但与正常文本的码点分布差异极大。
        body = "".join(chr(0xE000 + i) for i in range(40))
        payload = b"\xff\xfe" + body.encode("utf-16-le")
        decoded = payload.decode("utf-16-le")  # 证明「可严格解码」
        assert "\x00" not in decoded
        assert fp._utf16_text_is_plausible(decoded) is False
        with pytest.raises(fp.ParseError):
            fp.parse_file_content_ex(payload, "a.txt")

    def test_control_char_binary_is_rejected(self):
        """BOM + 控制字符（Cc）：同样严格可解码，但必须拒绝。"""
        body = "".join(chr(0x01 + i % 0x1F) for i in range(60))
        payload = b"\xff\xfe" + body.encode("utf-16-le")
        with pytest.raises(fp.ParseError):
            fp.parse_file_content_ex(payload, "a.csv")

    def test_legit_utf16_text_still_passes_gate(self):
        """合法中英文 / 表格文本不得被质量闸门误杀（防止收窄过头）。"""
        for src in ("工程概况 abc", "a,b\n1,2\n", "# 标题\n- 条款 1\n"):
            assert fp._utf16_text_is_plausible(src) is True


# ---------------------------------------------------------------------------
# 2 · 文本扩展名下的真实二进制必须被拒绝（不得乱码入库）
# ---------------------------------------------------------------------------
class TestBinaryDisguisedAsText:
    def test_pdf_renamed_txt_is_rejected(self):
        with pytest.raises(fp.ParseError) as ei:
            fp.parse_file_content_ex(_pdf_bytes(), "a.txt")
        assert "PDF" in str(ei.value)

    def test_exe_renamed_txt_still_rejected(self):
        with pytest.raises(fp.ParseError):
            _ = fp.parse_file_content_ex(b"MZ" + b"\x00" * 200, "a.txt")


# ---------------------------------------------------------------------------
# 3 · OOXML（ZIP）容器扩展名纠错
# ---------------------------------------------------------------------------
class TestOoxmlExtensionCorrection:
    def test_xlsx_renamed_xls_is_parsed(self):
        text, diag = fp.parse_file_content_ex(_xlsx_bytes(), "a.xls")
        assert "钢筋" in text and "【表格】" in text
        assert diag["file_type"] == "xlsx"

    def test_xlsx_renamed_doc_is_parsed(self):
        text, diag = fp.parse_file_content_ex(_xlsx_bytes(), "a.doc")
        assert "钢筋" in text, "真实 xlsx 改名 .doc 后输出了乱码 / 空内容"
        assert diag["file_type"] == "xlsx"

    def test_docx_renamed_wps_bypasses_office_conversion(self, monkeypatch):
        def _boom(*_a, **_kw):
            raise AssertionError("真实 docx 不应再走 Office 转换")

        monkeypatch.setattr(fp, "_parse_legacy_word", _boom)
        text, diag = fp.parse_file_content_ex(_docx_bytes(), "a.wps")
        assert "工程概况" in text
        assert diag["file_type"] == "docx"


# ---------------------------------------------------------------------------
# 4 · OLE 容器改名 .xlsx 必须纠错到 xls
# ---------------------------------------------------------------------------
class TestOleRenamedXlsx:
    def test_ole_workbook_renamed_xlsx_routes_to_xls(self, monkeypatch):
        captured: dict = {}

        def _fake_excel(content, ftype, diag=None):  # noqa: ANN001
            captured["ftype"] = ftype
            return "SHEET"

        monkeypatch.setattr(fp, "_parse_excel", _fake_excel)
        text, diag = fp.parse_file_content_ex(_ole_with_stream("Workbook"), "a.xlsx")
        assert captured.get("ftype") == "xls"
        assert diag["file_type"] == "xls"


# ---------------------------------------------------------------------------
# 5 · 接口级贯通：UTF-16 文本经 /upload-outline/parse 落库并回传正文
# ---------------------------------------------------------------------------
class TestUploadOutlineInterface:
    async def test_utf16_txt_through_parse_route(self, db_conn):
        body = (
            "第一章 工程概况\r\n"
            "1.1 项目简介\r\n工程位于示例市。\r\n"
            "第二章 施工方案\r\n"
            "2.1 施工工艺\r\n采用示例工艺。\r\n"
        ).encode("utf-16")
        up = UploadFile(file=io.BytesIO(body), filename="utf16.txt")
        res = await uo.parse_outline(file=up, db=db_conn, scheme_name="", reorganize=False)
        titles = [n.get("title") for n in (res.get("outline") or [])]
        assert "工程概况" in titles and "施工方案" in titles
        assert any("UTF-16" in w for w in (res.get("parse_warnings") or []))


# ---------------------------------------------------------------------------
# 6 · Excel 合并单元格：左上角值必须展开（表头不得退化）
# ---------------------------------------------------------------------------
class TestExcelMergedCells:
    def test_single_row_merge_expands_header_value(self):
        """A1:B1 合并：A1 的值必须展开到 B1（旧输出 `| 项目 |  |`）。"""

        def setup(ws):
            ws["A1"] = "项目"
            ws.merge_cells("A1:B1")
            ws["A2"] = 1
            ws["B2"] = 2

        text, _ = fp.parse_file_content_ex(_xlsx_with(setup), "a.xlsx")
        assert "| 项目 | 项目 |" in text, f"合并表头未展开:\n{text}"
        assert "| 项目 |  |" not in text

    def test_cross_row_merge_expands_anchor_downwards(self):
        """A1:A3 纵向合并：A1 的值必须展开到 A2/A3。"""

        def setup(ws):
            ws["A1"] = "项目"
            ws.merge_cells("A1:A3")
            ws["B1"] = "金额"
            ws["B2"] = 1
            ws["B3"] = 2

        text, _ = fp.parse_file_content_ex(_xlsx_with(setup), "a.xlsx")
        assert "| 项目 | 1 |" in text and "| 项目 | 2 |" in text, f"纵向合并未展开:\n{text}"

    def test_merged_title_plus_real_header(self):
        """合并大标题 + 真正的表头行：表头识别必须落在第二行。"""

        def setup(ws):
            ws["A1"] = "项目"
            ws.merge_cells("A1:B1")
            ws["A2"] = "名称"
            ws["B2"] = "金额"
            ws["A3"] = "甲"
            ws["B3"] = 1

        text, _ = fp.parse_file_content_ex(_xlsx_with(setup), "a.xlsx")
        assert "| 名称 | 金额 |" in text, f"真实表头未被识别:\n{text}"
        assert "项目" in text, "合并区的标题值被静默丢弃"

    def test_no_merge_output_unchanged(self):
        """无合并区：输出不得被新逻辑改写（零回归锁）。"""

        def setup(ws):
            ws["A1"] = "项目"
            ws["B1"] = "金额"
            ws["A2"] = 1
            ws["B2"] = 2

        text, _ = fp.parse_file_content_ex(_xlsx_with(setup), "a.xlsx")
        assert "| 项目 | 金额 |" in text
        assert "列1" not in text

    def test_read_only_workbook_kept(self, monkeypatch):
        """大文件保护：必须保持 read_only=True（不得整表非只读驻留内存）。"""
        import openpyxl

        captured: list[dict] = []
        real = openpyxl.load_workbook

        def _spy(*args, **kwargs):
            captured.append(dict(kwargs))
            return real(*args, **kwargs)

        monkeypatch.setattr(openpyxl, "load_workbook", _spy)
        fp.parse_file_content_ex(_xlsx_with(lambda ws: ws.append(["项目", "金额"])), "a.xlsx")
        assert captured, "未调用 load_workbook"
        assert all(c.get("read_only") is True for c in captured), captured


# ---------------------------------------------------------------------------
# 7 · Excel 无缓存公式：不得静默输出空单元格
# ---------------------------------------------------------------------------
class TestExcelFormulaWithoutCachedValue:
    def test_formula_cell_is_not_empty(self):
        """程序生成 / 未重算的文件：`data_only=True` 返回 None，必须回填占位。"""

        def setup(ws):
            ws["A1"] = 1
            ws["B1"] = 2
            ws["C1"] = "=A1+B1"

        text, _ = fp.parse_file_content_ex(_xlsx_with(setup), "a.xlsx")
        assert "[公式无缓存值] =A1+B1" in text, f"公式单元格被静默留空:\n{text}"

    def test_formula_row_has_no_empty_cell(self):
        """锁住「公式单元格不得输出为空」：该行第三列必须有可见内容。"""

        def setup(ws):
            ws["A1"] = 1
            ws["B1"] = 2
            ws["C1"] = "=A1+B1"

        text, _ = fp.parse_file_content_ex(_xlsx_with(setup), "a.xlsx")
        row = next(line for line in text.splitlines() if line.startswith("| 1 | 2 |"))
        assert row == "| 1 | 2 | [公式无缓存值] =A1+B1 |", row
