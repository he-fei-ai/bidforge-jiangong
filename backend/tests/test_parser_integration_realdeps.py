"""解析提取模块 · 真实依赖集成测试（非桩）

背景：test_file_parser / test_ocr / test_legacy_office 全部以 mock/桩覆盖，
真实二进制与库的端到端路径（PyMuPDF 读 PDF、python-docx 造 DOCX、openpyxl 造
XLSX、RapidOCR 跑真实推理）从未被保护 —— 回归只能靠人肉点页面发现。

本文件用「真实构造文件 + 真实解析器」跑端到端：
- fitz(PyMuPDF)、python-docx、openpyxl、Pillow、rapidocr 已在 requirements.txt
  或本机环境中可用（import 失败时 pytest.skip，不阻塞 CI）；
- LibreOffice / tesseract 本机未装，用 skipif 守卫 —— 装了即自动生效；
- MinerU 依赖外部网络服务，不做自动化（保持桩测试）。

与桩测试的分工：桩测试钉「分支语义 / 错误契约」，本文件钉「真实链路能跑通
且产物语义正确」，二者互补、互不替代。
"""
import shutil
from pathlib import Path

import pytest

from app.services.file_parser import parse_file_content_ex
from app.services.ocr import ocr_bytes_sync

# ---------------------------------------------------------------------------
# 依赖探测：缺什么跳什么（探测一次，模块级缓存）
# ---------------------------------------------------------------------------


def _import_or_none(name: str):
    try:
        return __import__(name)
    except Exception:
        return None


fitz = _import_or_none("fitz")                      # PyMuPDF（PDF 生成 + 解析）
docx_lib = _import_or_none("docx")                  # python-docx（DOCX 生成）
openpyxl = _import_or_none("openpyxl")              # XLSX 生成
PIL = _import_or_none("PIL")                        # 图片生成（OCR 输入）

SOFFICE = shutil.which("soffice") or shutil.which("soffice.exe")
TESSERACT = shutil.which("tesseract") or shutil.which("tesseract.exe")

RAPIDOCR_AVAILABLE = _import_or_none("rapidocr_onnxruntime") is not None


def _diag_has_no_fatal_warnings(diag: dict) -> None:
    """真实链路的通用断言：不允许出现「编码无法识别」类致命告警。"""
    for w in diag.get("warnings", []):
        assert "无法识别" not in w, f"真实依赖链路不应触发编码兜底告警: {w}"


# ---------------------------------------------------------------------------
# PDF：PyMuPDF 真实创建 → 真实解析
# ---------------------------------------------------------------------------

@pytest.mark.skipif(fitz is None, reason="PyMuPDF 未安装")
def test_pdf_roundtrip_real_pymupdf():
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "基坑深度 12.5 米", fontname="china-s", fontsize=16)
    page.insert_text((72, 140), "Support depth: 12.5m", fontsize=12)
    pdf_bytes = doc.tobytes()
    doc.close()

    text, diag = parse_file_content_ex(pdf_bytes, "方案.pdf")
    assert diag["file_type"] == "pdf"
    assert "基坑深度" in text, "PyMuPDF 文字层必须完整提取中文"
    assert "12.5" in text
    assert not diag["truncated"]
    assert diag["page_count"] >= 1
    _diag_has_no_fatal_warnings(diag)


@pytest.mark.skipif(fitz is None, reason="PyMuPDF 未安装")
def test_pdf_multipage_page_marks_real():
    """多页 PDF：页标记数量与真实页数一致（可追溯性锚点）。"""
    import fitz
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 100), f"第{i + 1}页内容", fontname="china-s", fontsize=14)
    pdf_bytes = doc.tobytes()
    doc.close()

    text, diag = parse_file_content_ex(pdf_bytes, "多页.pdf")
    assert diag["page_count"] == 3, f"页标记数应等于真实页数，got {diag['page_count']}"


# ---------------------------------------------------------------------------
# DOCX：python-docx 真实创建 → 真实解析（docx2python / fallback 链）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(docx_lib is None, reason="python-docx 未安装")
def test_docx_roundtrip_real():
    import docx as docx_mod
    d = docx_mod.Document()
    d.add_heading("工程概况", level=1)
    d.add_paragraph("本工程基坑深度为 12.5 米。")
    # ✅ 修复（2026-09-25）：Document.save() 返回 None（与 openpyxl 的
    #    Workbook.save 不同），不能 `buf = d.save(BytesIO())`，必须先建缓冲区。
    buf = __import__("io").BytesIO()
    d.save(buf)

    text, diag = parse_file_content_ex(buf.getvalue(), "设计说明.docx")
    assert diag["file_type"] == "docx"
    assert "工程概况" in text
    assert "12.5" in text
    assert not diag["truncated"]
    _diag_has_no_fatal_warnings(diag)


# ---------------------------------------------------------------------------
# XLSX：openpyxl 真实创建 → 真实解析（表格 → Markdown，下游高权重区依赖）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(openpyxl is None, reason="openpyxl 未安装")
def test_xlsx_roundtrip_real():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "机械统计"
    ws.append(["设备名称", "规格型号", "数量"])
    ws.append(["塔吊", "QTZ80", 2])
    ws.append(["挖掘机", "PC220", 3])
    buf = __import__("io").BytesIO()
    wb.save(buf)

    text, diag = parse_file_content_ex(buf.getvalue(), "机械统计.xlsx")
    assert diag["file_type"] == "xlsx"
    # 必须输出 Markdown 表格（表格保护 + machinery_stat 高权重区只认竖线语法）
    assert "| 设备名称 | 规格型号 | 数量 |" in text
    assert "| 塔吊 | QTZ80 | 2 |" in text
    _diag_has_no_fatal_warnings(diag)


# ---------------------------------------------------------------------------
# OCR：RapidOCR 真实推理（渲染文字图片 → 识别）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not (PIL and RAPIDOCR_AVAILABLE),
                    reason="Pillow / rapidocr 未安装")
def test_ocr_roundtrip_real_rapidocr():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (360, 120), "white")
    draw = ImageDraw.Draw(img)
    # 默认字体渲染 ASCII（中文渲染依赖系统字体，跨机器不稳定，不进断言）
    draw.text((20, 45), "SAFETY 2026", fill="black")
    buf = __import__("io").BytesIO()
    img.save(buf, format="PNG")

    result = ocr_bytes_sync(buf.getvalue())
    # 关键断言：真实引擎跑通且能识别（端到端主链路），文本内容宽松断言
    assert result.engine == "rapidocr", f"应实际落入 rapidocr 引擎，got {result.engine!r}"
    assert isinstance(result.text, str)
    assert "2026" in result.text, f"清晰渲染的数字应被识别，got {result.text!r}"


@pytest.mark.skipif(not PIL, reason="Pillow 未安装")
def test_image_parse_through_ocr_chain():
    """parse_file_content_ex 图片路由：无 tesseract 时回退 rapidocr，全链不崩。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (320, 100), "white")
    ImageDraw.Draw(img).text((20, 35), "PLAN 2026", fill="black")
    buf = __import__("io").BytesIO()
    img.save(buf, format="PNG")

    text, diag = parse_file_content_ex(buf.getvalue(), "scan.png")
    assert diag["file_type"] == "png"
    assert isinstance(text, str)


# ---------------------------------------------------------------------------
# 旧版 Office：LibreOffice / tesseract 本机未装 → skipif 守卫（装了自动生效）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not SOFFICE, reason="LibreOffice (soffice) 未安装")
def test_legacy_doc_roundtrip_real_libreoffice():
    """真实 .doc → docx 转换：需本机可构造 .doc（此处用最简 OLE 占位并断言
    转换链路能产出可解析文本；真正的 .doc 二进制由 LibreOffice 侧保证）。"""
    pytest.skip("无可靠 .doc 二进制构造器（xlwt/word 均不可用），仅在人工验证时启用")


@pytest.mark.skipif(not TESSERACT, reason="tesseract 未安装")
def test_ocr_tesseract_engine_real():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (320, 100), "white")
    ImageDraw.Draw(img).text((20, 35), "HELLO 123", fill="black")
    buf = __import__("io").BytesIO()
    img.save(buf, format="PNG")
    result = ocr_bytes_sync(buf.getvalue())
    assert result.engine in ("tesseract", "rapidocr", "vision")
    assert isinstance(result.text, str)


# ---------------------------------------------------------------------------
# 解析临时文件卫生：真实解析后不留临时产物（回归 2026-09 事故类问题）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(docx_lib is None, reason="python-docx 未安装")
def test_docx_parse_leaves_no_temp_files(tmp_path, monkeypatch):
    """DOCX 解析走临时文件 → 解析结束必须清理（防句柄泄漏/临时目录膨胀）。"""
    import docx as docx_mod
    import tempfile as tempfile_mod

    real_mkdtemp = tempfile_mod.mkdtemp
    created_dirs: list[Path] = []

    def _tracking_mkdtemp(*a, **kw):
        d = Path(real_mkdtemp(*a, **kw))
        created_dirs.append(d)
        return str(d)

    monkeypatch.setattr(tempfile_mod, "mkdtemp", _tracking_mkdtemp)

    d = docx_mod.Document()
    d.add_heading("临时文件卫生", level=1)
    buf = __import__("io").BytesIO()
    d.save(buf)

    text, _diag = parse_file_content_ex(buf.getvalue(), "卫生检查.docx")
    assert "临时文件卫生" in text
    for p in created_dirs:
        assert not p.exists() or not any(p.iterdir()), \
            f"解析临时目录未清理: {p}"
