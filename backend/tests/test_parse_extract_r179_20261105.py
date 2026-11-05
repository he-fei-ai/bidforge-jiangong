"""R179（2026-11-05）解析提取模块深度审计修复的回归测试。

每个用例对应一处真实缺陷，防止回退：

1. **长文档扫描件误判**：`_parse_pdf` 用「文档总页数」做分母计算平均字数，
   而主 / 回退通道都只解析前 `MAX_PDF_PAGES` 页 → 页数越多平均值被稀释得越
   厉害，800 页文档前 500 页明明有文字层也会被判成扫描件，触发整册 OCR。
2. **CSV 分隔符写死逗号**：分号（欧洲区域 / 国产办公软件导出）或制表符分隔
   的 CSV 被整表解析成单列，下游表格识别与机械统计高权重区全部失效。
3. **`.tif` 扩展名缺口**：白名单只登记 `tiff`，扫描仪 / 相机直出的 `.tif`
   被 400 拒绝，而底层 TIFF 多帧 OCR 能力完全可用。
4. **超长文件名落盘失败**：Windows 单段路径有长度上限，超长文件名让
   `dest.open("wb")` 抛 OSError 并被泛化成 500「上传保存失败」。
5. **xls 数值 / 日期失真**：xlrd 的 `row_values` 把日期返回为 Excel 序列号
   浮点、整数返回为 `2.0`。
6. **落盘 I/O 失败无归因**：单个文件写盘失败会中断整批并 500，用户无法得知
   是哪个文件、什么原因。
"""

import io
import sys
import types
import uuid

import app.db as _appdb
import app.routers.global_facts as gf
import app.routers.global_facts_docs as gfd
import app.services.file_parser as fp
import pytest
from app.db import get_conn, init_db
from starlette.datastructures import UploadFile


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


# ---------------------------------------------------------------------------
# 1. PDF 扫描件判定分母
# ---------------------------------------------------------------------------
def test_pdf_long_document_not_misjudged_as_scanned(monkeypatch):
    """800 页、前 500 页每页约 39 字：旧分母 800 → 约 24 字/页（< 30）误判
    扫描件并触发 OCR；修复后分母 500 → 约 39 字/页（≥ 30），不触发。"""

    class _Page:
        def __init__(self, number: int):
            self.number = number

        def get_text(self, _mode: str = ""):
            return "本工程施工组织设计说明文字样例" * 3

    class _Doc:
        page_count = 800
        is_encrypted = False

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def pages(self, start: int, end: int):
            return [_Page(i) for i in range(start, min(end, fp.MAX_PDF_PAGES))]

    monkeypatch.setitem(sys.modules, "fitz", types.SimpleNamespace(open=lambda **_kw: _Doc()))
    ocr_calls = {"n": 0}

    def _ocr(*_a, **_kw):
        ocr_calls["n"] += 1
        return ""

    monkeypatch.setattr(fp, "_pdf_ocr_fallback", _ocr)
    text = fp._parse_pdf(b"%PDF-1.4\n", diag=None)
    assert text.strip(), "正常文字层必须被提取"
    assert ocr_calls["n"] == 0, "长文档有正常文字层时不得被判为扫描件并触发 OCR"


def test_pdf_scanned_document_still_triggers_ocr(monkeypatch):
    """文字层确实缺失（每页空白）时，仍应触发 OCR 兜底 —— 修复不得误伤真扫描件。"""

    class _Page:
        def __init__(self, number: int):
            self.number = number

        def get_text(self, _mode: str = ""):
            return ""

    class _Doc:
        page_count = 800
        is_encrypted = False

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def pages(self, start: int, end: int):
            return [_Page(i) for i in range(start, min(end, fp.MAX_PDF_PAGES))]

    monkeypatch.setitem(sys.modules, "fitz", types.SimpleNamespace(open=lambda **_kw: _Doc()))
    ocr_calls = {"n": 0}

    def _ocr(*_a, **_kw):
        ocr_calls["n"] += 1
        return "OCR 识别文本"

    monkeypatch.setattr(fp, "_pdf_ocr_fallback", _ocr)
    text = fp._parse_pdf(b"%PDF-1.4\n", diag=None)
    assert ocr_calls["n"] == 1
    assert "OCR" in text


# ---------------------------------------------------------------------------
# 2. CSV 分隔符嗅探
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("payload", "expect_cols"),
    [
        ("设备名称;规格型号;数量\n塔吊;QTZ80;2\n", 3),
        ("设备名称\t规格型号\t数量\n塔吊\tQTZ80\t2\n", 3),
        ("设备名称|规格型号|数量\n塔吊|QTZ80|2\n", 3),
        ("设备名称,规格型号,数量\n塔吊,QTZ80,2\n", 3),  # 逗号行为零回归
    ],
)
def test_csv_delimiter_sniffing(payload, expect_cols):
    out = fp._parse_csv(payload.encode("utf-8"), None)
    table_lines = [ln for ln in out.splitlines() if ln.startswith("|")]
    assert table_lines, f"未输出 Markdown 表格：{out!r}"
    header = table_lines[0]
    assert header.count("|") - 1 == expect_cols, f"分隔符嗅探失败：{header!r}"
    assert "塔吊" in out and "QTZ80" in out


def test_csv_single_column_keeps_comma_default():
    """无分隔符的纯文本 CSV 仍按逗号解析（单列），不得因嗅探而产生副作用。"""
    out = fp._parse_csv(b"hello world\nsecond line\n", None)
    assert "hello world" in out


# ---------------------------------------------------------------------------
# 3. .tif 扩展名支持
# ---------------------------------------------------------------------------
def test_tif_extension_supported_and_signed():
    assert "tif" in fp.SUPPORTED_EXTENSIONS
    assert fp.signature_valid("tif", b"II*\x00")
    assert fp.signature_valid("tif", b"MM\x00*")
    assert not fp.signature_valid("tif", b"PK\x03\x04")


def test_tif_routes_to_image_ocr_dispatch(monkeypatch):
    called = {"n": 0}

    def _ocr(content, ftype, diag=None):
        called["n"] += 1
        return "TIFF 文本"

    monkeypatch.setattr(fp, "_parse_image_ocr", _ocr)
    text, diag = fp.parse_file_content_ex(b"II*\x00", "扫描件.tif")
    assert called["n"] == 1
    assert diag["file_type"] == "tif"
    assert text == "TIFF 文本"


# ---------------------------------------------------------------------------
# 4. 超长文件名截断
# ---------------------------------------------------------------------------
def test_safe_filename_truncates_and_keeps_extension():
    raw = "机电安装工程施工组织设计说明书" * 8 + ".docx"
    out = gfd._safe_filename(raw)
    assert len(out) <= gfd._MAX_SAFE_FILENAME_CHARS
    assert out.endswith(".docx")


def test_safe_filename_short_name_unchanged():
    assert gfd._safe_filename("施工方案.docx") == "施工方案.docx"


def test_safe_filename_still_guards_reserved_names():
    assert gfd._safe_filename("CON.docx").startswith("_")


# ---------------------------------------------------------------------------
# 5. xls 数值 / 日期单元格
# ---------------------------------------------------------------------------
def test_xlrd_numeric_cell_drops_trailing_zero():
    xlrd = pytest.importorskip("xlrd")
    cell = types.SimpleNamespace(ctype=xlrd.XL_CELL_NUMBER, value=2.0)
    assert fp._xlrd_cell_text(cell, 0) == "2"


def test_xlrd_date_cell_not_serial_number():
    xlrd = pytest.importorskip("xlrd")
    cell = types.SimpleNamespace(ctype=xlrd.XL_CELL_DATE, value=45292.0)
    out = fp._xlrd_cell_text(cell, 0)
    assert "-" in out and ".0" not in out, f"日期仍输出序列号：{out!r}"


def test_xlrd_text_cell_passthrough():
    xlrd = pytest.importorskip("xlrd")
    cell = types.SimpleNamespace(ctype=xlrd.XL_CELL_TEXT, value="塔吊")
    assert fp._xlrd_cell_text(cell, 0) == "塔吊"


# ---------------------------------------------------------------------------
# 6. 落盘 I/O 失败归入 write_errors
# ---------------------------------------------------------------------------
@pytest.fixture
async def upload_ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(_appdb, "DB_PATH", tmp_path / "r179.sqlite")
    await init_db()
    db = await get_conn()
    uploads = tmp_path / "uploads" / "facts"
    uploads.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.commit()
    yield db, pid
    await db.close()


async def test_upload_disk_error_reported_not_500(upload_ctx, monkeypatch):
    db, pid = upload_ctx

    async def _boom(*_a, **_kw):
        raise OSError(36, "File name too long")

    monkeypatch.setattr(gfd, "_stream_to_disk", _boom)
    result = await gfd.upload_documents(
        scheme_id="",
        project_id=pid,
        files=[_upload("正常文件.docx", b"PK\x03\x04")],
        db=db,
    )
    assert result["write_errors"] == ["正常文件.docx"]
    assert any("保存失败" in w for w in result["warnings"])
    assert result["saved_count"] == 0


async def test_upload_disk_error_does_not_block_other_files(upload_ctx, monkeypatch):
    db, pid = upload_ctx
    calls = {"n": 0}

    async def _sometimes_boom(_f, dest, limit):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"PK\x03\x04")
        return 4

    monkeypatch.setattr(gfd, "_stream_to_disk", _sometimes_boom)
    result = await gfd.upload_documents(
        scheme_id="",
        project_id=pid,
        files=[_upload("第一个.docx", b"PK\x03\x04"), _upload("第二个.docx", b"PK\x03\x04")],
        db=db,
    )
    assert result["write_errors"] == ["第一个.docx"]
    assert result["saved_count"] == 1


# ---------------------------------------------------------------------------
# 7. 端到端贯通：上传 → 解析 → 落库 → 下游目录/正文生成消费
# ---------------------------------------------------------------------------
async def test_end_to_end_semicolon_csv_reaches_downstream(upload_ctx):
    """分号分隔 CSV 走完整链路：上传落盘 → 批量解析写 parsed_markdown →
    下游 load_parsed_docs（目录/正文/事实提取的统一读取入口）能拿到完整表格。

    旧实现在第一步就把三列塌缩成一列，下游读到的是 ``设备名称;规格型号;数量``
    单个单元格，表格识别与机械统计分类全部失效。
    """
    db, pid = upload_ctx
    payload = "设备名称;规格型号;数量\n塔吊;QTZ80;2\n".encode("utf-8")
    await gfd.upload_documents(
        scheme_id="",
        project_id=pid,
        files=[_upload("设备表.csv", payload)],
        db=db,
    )
    res = await gf.parse_all_documents(scheme_id="", project_id=pid, force=False, db=db)
    assert res["parsed"] == 1, res

    docs = await gf.load_parsed_docs(db, pid)
    assert docs and docs[0][0] == "设备表.csv"
    md = docs[0][1]
    assert "| 设备名称 | 规格型号 | 数量 |" in md
    assert "| 塔吊 | QTZ80 | 2 |" in md
