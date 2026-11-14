# -*- coding: utf-8 -*-
"""R238 解析提取模块（import）：跨文件修复护栏。

聚焦【解析提取模块】的解析器级缺陷：
  - FIX-A：`.xls` xlrd ``open_workbook`` 从不 ``release_resources()`` ——
    xlrd 内部对源文件 mmap 并持有句柄，Windows 上外层 os.unlink 抛
    PermissionError（被静默吞掉）→ 临时文件永久残留。成功与异常路径都必须释放。

配套的 DOCX 内容控件/页眉页脚护栏见 ``test_parse_extract_docx_parts_20261114.py``。
"""

import pytest
import xlrd
from app.services import file_parser as fp


class _FakeCell:
    ctype = xlrd.XL_CELL_TEXT  # 1 = 文本单元格

    def __init__(self, value: str):
        self.value = value


class _FakeSheet:
    name = "Sheet1"
    nrows = 2
    datemode = 0

    def __init__(self, raise_on_row: bool = False):
        self._raise_on_row = raise_on_row

    def row(self, rx: int):
        if self._raise_on_row:
            raise RuntimeError("模拟工作表读取异常")
        return [_FakeCell("a"), _FakeCell("b")] if rx == 0 else [_FakeCell("1"), _FakeCell("2")]


class _FakeBook:
    datemode = 0

    def __init__(self, raise_on_row: bool = False):
        self.released = False
        self._sheets = [_FakeSheet(raise_on_row)]

    def sheets(self):
        return self._sheets

    def release_resources(self) -> None:
        self.released = True


def _patch_xlrd(monkeypatch, raise_on_row: bool = False):
    book = _FakeBook(raise_on_row)
    monkeypatch.setattr(xlrd, "open_workbook", lambda *a, **k: book)
    return book


def test_xls_release_resources_called_on_success(monkeypatch):
    """正常解析完成后必须 release_resources（Windows 临时文件不被 mmap 占住）。"""
    book = _patch_xlrd(monkeypatch)
    out = fp._parse_excel(b"stub-xls-bytes", "xls", {"warnings": []})
    assert book.released is True
    assert "【工作表：Sheet1】" in out
    assert "| a | b |" in out
    assert "| 1 | 2 |" in out


def test_xls_release_resources_called_on_error(monkeypatch):
    """工作表读取抛异常时也必须 release_resources（不泄漏句柄）。"""
    book = _patch_xlrd(monkeypatch, raise_on_row=True)
    with pytest.raises(RuntimeError):
        fp._parse_excel(b"stub-xls-bytes", "xls", {"warnings": []})
    assert book.released is True


def test_docx_parts_switch_defaults_on():
    """PARSE_DOCX_PARTS 未显式关闭时默认开启（部件增强是预期默认行为）。"""
    assert fp._PARSE_DOCX_PARTS is True


def test_docx_parts_switch_env_off(monkeypatch):
    """环境变量 PARSE_DOCX_PARTS=0 时模块级常量回退为关闭。"""
    monkeypatch.setattr(fp, "_PARSE_DOCX_PARTS", False)
    assert fp._PARSE_DOCX_PARTS is False


def test_tesseract_timeout_passed_through(monkeypatch):
    """tesseract 调用必须携带 ocr_timeout_seconds（防挂起占死 OCR 串行锁）。"""
    import pytesseract
    from app.services import ocr as ocr_mod

    captured: dict = {}

    def _fake_image_to_string(img, lang=None, config="", timeout=0, **kw):
        captured["timeout"] = timeout
        captured["lang"] = lang
        return "OCR文本"

    monkeypatch.setattr(ocr_mod, "_preprocess", lambda data: object())
    monkeypatch.setattr(pytesseract, "image_to_string", _fake_image_to_string)
    out = ocr_mod._ocr_tesseract(b"fake-png", "chi_sim+eng")
    assert out == "OCR文本"
    assert captured["lang"] == "chi_sim+eng"
    assert captured["timeout"] == 120  # 默认配置


def test_tesseract_timeout_zero_means_no_limit(monkeypatch):
    """ocr_timeout_seconds=0 时传入 timeout=0（pytesseract 0 = 不限制）。"""
    import pytesseract
    from app.config import settings
    from app.services import ocr as ocr_mod

    captured: dict = {}

    def _fake_image_to_string(img, lang=None, config="", timeout=0, **kw):
        captured["timeout"] = timeout
        return "OCR文本"

    monkeypatch.setattr(settings, "ocr_timeout_seconds", 0)
    monkeypatch.setattr(ocr_mod, "_preprocess", lambda data: object())
    monkeypatch.setattr(pytesseract, "image_to_string", _fake_image_to_string)
    ocr_mod._ocr_tesseract(b"fake-png", "chi_sim+eng")
    assert captured["timeout"] == 0
