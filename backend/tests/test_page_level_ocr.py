"""R124（2026-10-21）分块级 OCR 补漏 专项回归测试。

覆盖：
- 页标记反解（_parse_page_markers_to_map 纯函数）。
- 逐页 OCR 补漏编排（_patch_image_pages_with_ocr）：中间扫描页命中、OCR 不可用告警、
  无 image 页时跳过、回填正确。
- _pdf_ocr_fallback 的 pages 参数：仅渲指定页（不误 OCR 其它页）。
- _parse_pdf 端到端：混合文档（中间页空白）走逐页 OCR 补漏；全图文档由逐页 OCR 覆盖。
"""
import app.services.file_parser as fp
import app.services.ocr as ocr_mod
import pytest
from app.services.file_parser import (
    ParseError,
    _parse_page_markers_to_map,
    _parse_pdf,
    _patch_image_pages_with_ocr,
    _pdf_ocr_fallback,
)


def _build_pdf(page_texts):
    """用 PyMuPDF 生成内存 PDF（每页可选注入文字，空串=空白页）。"""
    fitz_mod = pytest.importorskip("fitz")
    doc = fitz_mod.open()
    for t in page_texts:
        page = doc.new_page()
        if t:
            page.insert_text((72, 72), t)
    data = doc.tobytes()
    doc.close()
    return data


def _new_diag():
    return {"file_type": "pdf", "text_len": 0, "truncated": False, "warnings": []}


def test_parse_page_markers_to_map():
    """页标记文本反解为 {页号: 文本} 映射。"""
    s = "<!-- page:1 -->\n甲内容\n<!-- page:2 -->\n乙内容"
    assert _parse_page_markers_to_map(s) == {1: "甲内容", 2: "乙内容"}


def test_patch_image_pages_triggers_per_page_ocr(monkeypatch):
    """中间空页（第 2 页）应触发仅对该页的 OCR 拼接，且原文本页不动。"""
    # 第 1/3 页用超过阈值(30)的正文，避免被误判为图片页
    long1 = "第一章 工程概况 这是一段足够长的正文用于超过三十字符阈值避免被误判为图片页的测试内容"
    long3 = "第三章 同样需要足够长的正文内容超过三十字符以便正确识别为正常文本页而非图片页"
    page_items = [(1, long1), (2, ""), (3, long3)]
    calls: dict = {}

    def fake_ocr(content, max_pages=None, diag=None, pages=None):
        calls["pages"] = pages
        return "<!-- page:2 -->\n扫描页OCR内容"

    monkeypatch.setattr(fp, "_pdf_ocr_fallback", fake_ocr)
    out = _patch_image_pages_with_ocr(b"PDF", page_items, 3, _new_diag(), 30)
    assert calls.get("pages") == [2], "应仅对第 2 页（image-only）做 OCR"
    texts = {num: t for num, t in out}
    assert texts[2] == "扫描页OCR内容", "空页应被 OCR 文本回填"
    assert texts[1] == long1, "有文字页不应被改动"


def test_patch_image_pages_ocr_unavailable_warns(monkeypatch):
    """OCR 引擎不可用时逐页告警，保留已提取文字（不阻断解析）。"""
    long1 = "第一章 工程概况 这是一段足够长的正文内容用于超过三十字符阈值避免被误判为图片页"
    long3 = "第三章 同样需要足够长的正文内容超过三十字符以便正确识别为正常文本页而非图片页"
    page_items = [(1, long1), (2, ""), (3, long3)]

    def fake_ocr(content, max_pages=None, diag=None, pages=None):
        raise ParseError("OCR 引擎不可用")

    monkeypatch.setattr(fp, "_pdf_ocr_fallback", fake_ocr)
    diag = _new_diag()
    out = _patch_image_pages_with_ocr(b"PDF", page_items, 3, diag, 30)
    assert any("第 2 页为纯图片且 OCR 不可用" in w for w in diag["warnings"])
    texts = {num: t for num, t in out}
    assert texts[2] == "", "OCR 不可用时保留原空页（不阻断）"


def test_patch_image_pages_no_image_skips_ocr(monkeypatch):
    """无 image 页（均超阈值）时不应调用 OCR，page_items 原样返回。"""
    long1 = "第一章 工程概况 这是一段足够长的正文内容用于超过三十字符阈值避免被误判为图片页"
    long2 = "第二章 同样需要足够长的正文内容超过三十字符以便正确识别为正常文本页而非图片页"
    long3 = "第三章 同样需要足够长的正文内容超过三十字符以便正确识别为正常文本页而非图片页"
    page_items = [(1, long1), (2, long2), (3, long3)]
    called = {"n": 0}

    def fake_ocr(content, max_pages=None, diag=None, pages=None):
        called["n"] += 1
        return ""

    monkeypatch.setattr(fp, "_pdf_ocr_fallback", fake_ocr)
    out = _patch_image_pages_with_ocr(b"PDF", page_items, 3, _new_diag(), 30)
    assert called["n"] == 0
    assert out == page_items


def test_pdf_ocr_fallback_pages_param(monkeypatch):
    """_pdf_ocr_fallback(pages=[2]) 仅渲染第 2 页，不误 OCR 第 1/3 页。"""
    pdf = _build_pdf(["第一章 工程概况 足够长的正文内容超过三十字符阈值", "", "第三章 工程概况 足够长的正文内容超过三十字符阈值"])

    class _FakeRes:
        def __init__(self, t):
            self.text = t

    def fake_ocr(png):
        return _FakeRes("OCR文字")

    monkeypatch.setattr(ocr_mod, "ocr_bytes_sync", fake_ocr)
    out = _pdf_ocr_fallback(pdf, pages=[2])
    assert "<!-- page:2 -->" in out and "OCR文字" in out
    assert "<!-- page:1 -->" not in out
    assert "<!-- page:3 -->" not in out


def test_parse_pdf_mixed_doc_per_page_ocr(monkeypatch):
    """混合文档（中间页空白）：逐页 OCR 补漏第 2 页，文档级不再重复触发。"""
    long1 = "第一章 工程概况 这是一段足够长的正文内容用于超过三十字符阈值避免被误判为图片页"
    long3 = "第三章 同样需要足够长的正文内容超过三十字符以便正确识别为正常文本页而非图片页"
    pdf = _build_pdf([long1, "", long3])
    calls: list = []

    def fake_ocr(content, max_pages=None, diag=None, pages=None):
        calls.append(pages)
        if pages == [2]:
            return "<!-- page:2 -->\n扫描页OCR内容"
        return ""

    monkeypatch.setattr(fp, "_pdf_ocr_fallback", fake_ocr)
    text = _parse_pdf(pdf, diag=_new_diag())
    assert "<!-- page:2 -->" in text and "扫描页OCR内容" in text
    assert [2] in calls, "仅第 2 页应被按页 OCR 补漏"


def test_parse_pdf_all_image_doc_per_page_ocr(monkeypatch):
    """全图文档（三页空白）由逐页 OCR 全覆盖，不漏页。"""
    pdf = _build_pdf(["", "", ""])

    def fake_fallback(content):
        # 模拟回退通道返回 3 个空白页（环境无关，保证确定性）
        return "", 3, [(1, ""), (2, ""), (3, "")]

    monkeypatch.setattr(fp, "_parse_pdf_fallback", fake_fallback)
    calls: list = []

    def fake_ocr(content, max_pages=None, diag=None, pages=None):
        calls.append(pages)
        if pages:
            return "\n".join(
                f"<!-- page:{p} -->\nOCR页{p}" for p in pages)
        return ""

    monkeypatch.setattr(fp, "_pdf_ocr_fallback", fake_ocr)
    text = _parse_pdf(pdf, diag=_new_diag())
    assert "OCR页1" in text and "OCR页2" in text and "OCR页3" in text
    assert calls and set(calls[0]) == {1, 2, 3}, "三页均应被逐页 OCR 补漏"
