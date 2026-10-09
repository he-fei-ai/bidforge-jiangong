"""旧版 Word（.doc/.wps）解析支持回归测试（2026-09-22）。

引入背景（对齐 OpenBidKit `client/electron/services/doc2markdown/convert.mjs`
的 `withLegacyWordDocxFile` 与 `fileService.resolveFileParser`）：
招投标场景大量招标文件仍是 .doc/.wps，旧实现直接以「不支持的文件格式」400 拒绝。

本文件覆盖：
1. 文件头签名：.doc/.wps 接受 OLE 与 ZIP，拒绝伪装的可执行文件；
2. 无任何转换组件 → 可操作提示（含 LibreOffice 下载地址）；
3. 配置关闭 → 明确报错，不静默走文本解码；
4. 转换成功 → 复用既有 OOXML 解析（标题层级保留）+ 诊断记录转换组件；
5. 多后端依次尝试：前一个失败仍能回退成功后一个；
6. 全部后端失败 → 报错含具体后端名（便于用户排查）；
7. 扩展名为 .docx 但内容为 OLE 复合文档 → 按真实类型改走旧版 Word 通道。
"""
import io
import zipfile

import app.services.legacy_office as legacy_office
import pytest
from app.services.file_parser import (
    ParseError,
    parse_file_content_ex,
    signature_valid,
)

#: OLE 复合文档头（旧版 Word / .xls 的容器魔数）
_OLE_HEADER = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FAKE_OLE_DOC = _OLE_HEADER + b"\x00" * 64


def _build_docx(paragraphs: list[tuple[int, str]]) -> bytes:
    """构造最小可用 DOCX（供转换后端「产出文件」用）。

    paragraphs: [(标题层级, 文本)]，层级 0 表示正文段落。
    """
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body: list[str] = []
    for level, text in paragraphs:
        if level:
            body.append(
                f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>'
                f'<w:r><w:t>{text}</w:t></w:r></w:p>')
        else:
            body.append(f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>')
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           f'<w:document {ns}><w:body>{"".join(body)}</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _install_fake_libreoffice(monkeypatch, docx_bytes: bytes):
    """把 LibreOffice 后端替换为「直接把预置 docx 写到 outdir」的假实现。"""
    calls: list[dict] = []

    def _fake_convert(soffice, src, out_dir, timeout_s):
        calls.append({"soffice": soffice, "src": src, "timeout_s": timeout_s})
        produced = out_dir / "converted.docx"
        produced.write_bytes(docx_bytes)
        return produced

    monkeypatch.setattr(legacy_office, "_run_libreoffice_convert", _fake_convert)
    monkeypatch.setattr(
        legacy_office, "detect_office_backends",
        lambda ext="doc": [{"type": "libreoffice", "label": "LibreOffice",
                            "command": "soffice"}])
    return calls


# ---------------------------------------------------------------------------
# 1. 文件头签名
# ---------------------------------------------------------------------------

def test_signature_accepts_ole_and_zip_for_legacy_word():
    assert signature_valid("doc", _OLE_HEADER) is True
    assert signature_valid("wps", _OLE_HEADER) is True
    # 被改名的 .docx（ZIP）也应放行，真实类型由文件头路由决定
    assert signature_valid("doc", b"PK\x03\x04") is True
    # 伪装成 .doc 的可执行文件必须拒绝
    assert signature_valid("doc", b"MZ\x90\x00") is False
    assert signature_valid("wps", b"\x7fELF") is False


# ---------------------------------------------------------------------------
# 2 / 3. 无组件与关闭开关
# ---------------------------------------------------------------------------

def test_no_backend_gives_actionable_message(monkeypatch):
    monkeypatch.setattr(legacy_office, "detect_office_backends",
                        lambda ext="doc": [])
    monkeypatch.setattr(legacy_office.settings, "legacy_office_enabled", True)
    with pytest.raises(ParseError) as exc:
        parse_file_content_ex(_FAKE_OLE_DOC, "招标文件.doc")
    msg = str(exc.value)
    assert "LibreOffice" in msg
    assert legacy_office.LIBREOFFICE_DOWNLOAD_URL in msg
    # 提示用户「或者手动转 docx」的可操作退路
    assert "docx" in msg


def test_disabled_by_config_raises(monkeypatch):
    monkeypatch.setattr(legacy_office.settings, "legacy_office_enabled", False)
    with pytest.raises(ParseError) as exc:
        parse_file_content_ex(_FAKE_OLE_DOC, "招标文件.doc")
    assert "LEGACY_OFFICE_ENABLED" in str(exc.value)


# ---------------------------------------------------------------------------
# 4. 转换成功后复用 OOXML 解析
# ---------------------------------------------------------------------------

def test_convert_then_parse_keeps_heading_structure(monkeypatch):
    docx_bytes = _build_docx([(1, "工程概况"), (2, "地质条件"), (0, "正文段落")])
    calls = _install_fake_libreoffice(monkeypatch, docx_bytes)
    monkeypatch.setattr(legacy_office.settings, "legacy_office_enabled", True)

    text, diag = parse_file_content_ex(_FAKE_OLE_DOC, "招标文件.doc")

    assert "# 工程概况" in text
    assert "## 地质条件" in text
    assert "正文段落" in text
    # 转换组件写进诊断（会随 parse_warnings 持久化，便于用户核对来源）
    assert any("LibreOffice" in w for w in diag["warnings"])
    assert diag["file_type"] == "doc"
    assert calls and calls[0]["soffice"] == "soffice"


def test_wps_extension_routes_through_legacy_channel(monkeypatch):
    docx_bytes = _build_docx([(1, "投标须知")])
    _install_fake_libreoffice(monkeypatch, docx_bytes)
    text, diag = parse_file_content_ex(_FAKE_OLE_DOC, "招标文件.wps")
    assert "# 投标须知" in text
    assert diag["file_type"] == "wps"


# ---------------------------------------------------------------------------
# 5 / 6. 多后端回退语义
# ---------------------------------------------------------------------------

def test_falls_back_to_next_backend_when_first_fails(monkeypatch):
    docx_bytes = _build_docx([(1, "回退成功")])

    def _failing_lo(soffice, src, out_dir, timeout_s):
        raise ParseError("LibreOffice 未生成 DOCX 文件")

    def _ok_com(powershell, prog_id, src, dst, timeout_s):
        dst.write_bytes(docx_bytes)

    monkeypatch.setattr(legacy_office, "_run_libreoffice_convert", _failing_lo)
    monkeypatch.setattr(legacy_office, "_run_com_convert", _ok_com)
    monkeypatch.setattr(
        legacy_office, "detect_office_backends",
        lambda ext="doc": [
            {"type": "libreoffice", "label": "LibreOffice", "command": "soffice"},
            {"type": "word", "label": "Microsoft Word (Word.Application)",
             "command": "powershell.exe", "prog_id": "Word.Application"},
        ])

    text, diag = parse_file_content_ex(_FAKE_OLE_DOC, "招标文件.doc")
    assert "# 回退成功" in text
    assert any("Microsoft Word" in w for w in diag["warnings"])


def test_all_backends_failed_reports_each_attempt(monkeypatch):
    def _boom(*args, **kwargs):
        raise ParseError("转换引擎异常")

    monkeypatch.setattr(legacy_office, "_run_libreoffice_convert", _boom)
    monkeypatch.setattr(legacy_office, "_run_com_convert", _boom)
    monkeypatch.setattr(
        legacy_office, "detect_office_backends",
        lambda ext="doc": [
            {"type": "libreoffice", "label": "LibreOffice", "command": "soffice"},
            {"type": "word", "label": "Microsoft Word (Word.Application)",
             "command": "powershell.exe", "prog_id": "Word.Application"},
        ])

    with pytest.raises(ParseError) as exc:
        parse_file_content_ex(_FAKE_OLE_DOC, "招标文件.doc")
    msg = str(exc.value)
    assert "LibreOffice" in msg and "Microsoft Word" in msg
    assert "另存为" in msg


# ---------------------------------------------------------------------------
# 7. 扩展名与真实内容不符（.docx + OLE）
# ---------------------------------------------------------------------------

def test_ole_content_with_docx_extension_routed_to_legacy(monkeypatch):
    """旧版 Word「另存为 .docx」失败 / 手工改名 → 不应报「文件已损坏」。"""
    docx_bytes = _build_docx([(1, "真实类型识别")])
    _install_fake_libreoffice(monkeypatch, docx_bytes)

    text, diag = parse_file_content_ex(_FAKE_OLE_DOC, "招标文件.docx")
    assert "# 真实类型识别" in text
    # 真实类型已按文件头纠正，而不是沿用后缀
    assert diag["file_type"] == "doc"
    assert any("OLE" in w for w in diag["warnings"])


def test_convert_legacy_word_rejects_empty_content(monkeypatch):
    monkeypatch.setattr(legacy_office.settings, "legacy_office_enabled", True)
    with pytest.raises(ParseError):
        legacy_office.convert_legacy_word_to_docx(b"", "空.doc")
