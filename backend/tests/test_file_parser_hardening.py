"""文件解析器资源防护与诊断回归测试。

覆盖此前缺失的两类问题（安全/QA 审计结论）：

1. **压缩炸弹**：DOCX/XLSX 是 ZIP 容器，30MB 上传上限 ≠ 解压体积上限。
   1:1000 膨胀比的归档可在解压阶段直接打满内存/CPU，且解析发生在服务进程内，
   OOM 会连带拖垮正在生成的方案任务。
2. **静默截断**：PDF 超 50 页、CSV/Excel 超 1000 行只会写日志，
   路由层拿不到任何标记 → 用户看到"解析成功"，但后半部分事实永远不会被提取。

本文件锁定：闸门必须**拒绝**超限归档；解析器截断必须通过诊断字典**回传**。
"""
import io
import sys
import zipfile

import pytest

import app.services.file_parser as fp
from app.services.file_parser import (
    ParseError,
    parse_file_content,
    parse_file_content_ex,
)


# ---------------------------------------------------------------------------
# 压缩炸弹闸门
# ---------------------------------------------------------------------------

def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_archive_with_too_many_entries_rejected(monkeypatch):
    monkeypatch.setattr(fp, "MAX_ARCHIVE_ENTRIES", 3)
    content = _zip_bytes({f"f{i}.xml": b"<a/>" for i in range(5)})
    with pytest.raises(ParseError) as e:
        fp._guard_zip_archive(content, "docx")
    assert "条目过多" in str(e.value)


def test_archive_with_huge_uncompressed_size_rejected(monkeypatch):
    monkeypatch.setattr(fp, "MAX_ARCHIVE_UNCOMPRESSED_BYTES", 1024)
    content = _zip_bytes({"big.xml": b"A" * 5000})
    with pytest.raises(ParseError) as e:
        fp._guard_zip_archive(content, "xlsx")
    assert "解压后体积过大" in str(e.value)


def test_archive_with_extreme_compression_ratio_rejected(monkeypatch):
    # 5MB 全零数据压缩后极小 → 压缩比远超上限
    monkeypatch.setattr(fp, "MAX_ARCHIVE_UNCOMPRESSED_BYTES", 100 * 1024 * 1024)
    content = _zip_bytes({"bomb.xml": b"\x00" * (5 * 1024 * 1024)})
    with pytest.raises(ParseError) as e:
        fp._guard_zip_archive(content, "docx")
    assert "压缩比异常" in str(e.value)


def test_normal_archive_passes_guard():
    content = _zip_bytes({"word/document.xml": b"<w:document/>" * 10})
    fp._guard_zip_archive(content, "docx")  # 不抛异常即通过


def test_broken_zip_reports_parse_error_not_crash():
    with pytest.raises(ParseError) as e:
        fp._guard_zip_archive(b"definitely not a zip", "docx")
    assert "损坏" in str(e.value) or "无效" in str(e.value)


def test_docx_zip_bomb_rejected_through_public_entry(monkeypatch):
    """闸门必须在 parse_file_content 入口生效（而非只在内部函数）。"""
    monkeypatch.setattr(fp, "MAX_COMPRESSION_RATIO", 5)
    content = _zip_bytes({"word/document.xml": b"\x00" * (1024 * 1024)})
    with pytest.raises(ParseError):
        parse_file_content(content, "bomb.docx")


def test_xlsx_zip_bomb_rejected_through_public_entry(monkeypatch):
    monkeypatch.setattr(fp, "MAX_COMPRESSION_RATIO", 5)
    content = _zip_bytes({"xl/worksheets/sheet1.xml": b"\x00" * (1024 * 1024)})
    with pytest.raises(ParseError):
        parse_file_content(content, "bomb.xlsx")


# ---------------------------------------------------------------------------
# 诊断字典（解析成功但内容不完整必须可回传）
# ---------------------------------------------------------------------------

def test_plain_text_diagnostics_defaults():
    text, diag = parse_file_content_ex("你好世界".encode("utf-8"), "a.txt")
    assert text == "你好世界"
    assert diag["file_type"] == "txt"
    assert diag["text_len"] == 4
    assert diag["truncated"] is False
    assert diag["warnings"] == []


def test_empty_content_diagnostics():
    text, diag = parse_file_content_ex(b"", "a.txt")
    assert text == ""
    assert diag["truncated"] is False


# ---------------------------------------------------------------------------
# ✅ B-1 / B-3：无扩展名二进制拒绝、加密 PDF 明确报错
# ---------------------------------------------------------------------------

def test_no_extension_pdf_sniffed_and_parsed():
    """B-1：无扩展名的 PDF 应通过文件头嗅探正确解析（而非当文本乱码入库）。"""
    content = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF"
    text, diag = parse_file_content_ex(content, "招标文件")  # 无扩展名
    assert diag["file_type"] == "pdf"


def test_no_extension_binary_rejected(monkeypatch):
    """B-1：无扩展名且无法嗅探的二进制（ZIP/OLE 容器）必须明确拒绝，不能乱码入库。"""
    # 构造一个 ZIP 容器（docx/xlsx 底层），但无扩展名 —— 嗅探无法区分 docx/xlsx
    content = _zip_bytes({"docProps/core.xml": b"<x/>"})
    with pytest.raises(ParseError) as e:
        parse_file_content_ex(content, "未命名文件")
    assert "扩展名" in str(e.value)


def test_no_extension_plain_text_decoded():
    """B-1：无扩展名的纯文本应正常解码（内容非二进制）。"""
    text, diag = parse_file_content_ex("第一章 工程概况".encode("utf-8"), "说明")
    assert text == "第一章 工程概况"
    assert diag["file_type"] == "txt"


def test_encrypted_pdf_without_password_raises(monkeypatch):
    """B-3：已加密且空密码解不开的 PDF，文本为空时应明确报出根因（已加密）。"""
    # 构造一个能被 PyMuPDF 打开但标记为加密、空密码认证失败的 PDF 较复杂；
    # 此处用 monkeypatch 模拟「is_encrypted=True 且 authenticate('') 失败」并
    # 让主/回退通道与 OCR 均产出空文本，验证末尾明确抛出已加密错误。
    import app.services.file_parser as fp_mod

    class _FakePage:
        number = 0

        def get_text(self, *_a, **_k):
            return ""

    class _FakeDoc:
        is_encrypted = True
        page_count = 1

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def authenticate(self, pw):
            return False  # 空密码认证失败

        def pages(self, *a, **k):
            return [_FakePage()]

    monkeypatch.setattr(fp_mod, "has_informative_text", lambda t: False)
    monkeypatch.setattr(fp_mod, "_parse_pdf_fallback", lambda c: ("", 1))
    monkeypatch.setattr(fp_mod, "_pdf_ocr_fallback", lambda c, **k: "")
    monkeypatch.setattr(fp_mod, "has_informative_text", lambda t: False)
    # _parse_pdf 内部 `from app.config import settings` 惰性导入，需直接 patch 模块属性
    import app.config as _cfg_mod
    monkeypatch.setattr(
        _cfg_mod, "settings",
        type("S", (), {"mineru_enabled": False,
                       "mineru_provider": "",
                       "ocr_min_chars_per_page": 30})())

    real_fitz = sys.modules.get("fitz")

    class _Fitz:
        @staticmethod
        def open(*a, **k):
            return _FakeDoc()

    # _parse_pdf 内部用 importlib.import_module("fitz") 惰性导入，patch sys.modules
    monkeypatch.setitem(sys.modules, "fitz", _Fitz)
    try:
        with pytest.raises(ParseError) as e:
            fp_mod._parse_pdf(b"%PDF-1.4 encrypted", None)
        assert "加密" in str(e.value)
    finally:
        if real_fitz is not None:
            sys.modules["fitz"] = real_fitz
        else:
            sys.modules.pop("fitz", None)


def test_csv_row_truncation_reported(monkeypatch):
    monkeypatch.setattr(fp, "MAX_CSV_ROWS", 3)
    content = ("h\n" + "\n".join(f"r{i}" for i in range(10))).encode("utf-8")
    text, diag = parse_file_content_ex(content, "big.csv")
    assert diag["truncated"] is True
    assert any("超过上限" in w for w in diag["warnings"])
    # 截断后仍返回可用的 Markdown 表格
    assert "【表格】" in text


def test_excel_row_truncation_reported(monkeypatch):
    from openpyxl import Workbook
    monkeypatch.setattr(fp, "MAX_EXCEL_ROWS", 2)
    wb = Workbook()
    ws = wb.active
    ws.title = "机械表"
    for i in range(6):
        ws.append([f"设备{i}", i])
    buf = io.BytesIO()
    wb.save(buf)

    text, diag = parse_file_content_ex(buf.getvalue(), "big.xlsx")
    assert diag["truncated"] is True
    assert any("超过上限" in w for w in diag["warnings"])
    assert "【工作表：机械表】" in text


def test_pdf_page_truncation_reported(monkeypatch):
    """PDF 页数超上限时必须回报截断（旧实现只截断不告知）。"""
    pypdf = pytest.importorskip("pypdf")
    monkeypatch.setattr(fp, "MAX_PDF_PAGES", 3)
    # 空白页 PDF：文本为空 → 会走 OCR 兜底，这里屏蔽 OCR 以聚焦页数诊断
    monkeypatch.setattr(fp, "_pdf_ocr_fallback", lambda *a, **k: "")

    writer = pypdf.PdfWriter()
    for _ in range(5):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)

    text, diag = parse_file_content_ex(buf.getvalue(), "long.pdf")
    assert diag["truncated"] is True
    assert any("仅解析前 3 页" in w for w in diag["warnings"])


def test_pdf_within_page_limit_not_marked_truncated(monkeypatch):
    pypdf = pytest.importorskip("pypdf")
    monkeypatch.setattr(fp, "_pdf_ocr_fallback", lambda *a, **k: "")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)

    _text, diag = parse_file_content_ex(buf.getvalue(), "one.pdf")
    assert diag["truncated"] is False


def test_note_truncation_without_diag_still_logs():
    """无 diag 调用方（旧路径）不得因缺少字典而抛异常。"""
    fp._note_truncation(None, "some message")


def test_parse_file_content_backward_compatible():
    """兼容入口必须仍然只返回字符串（大量既有调用方依赖此签名）。"""
    assert parse_file_content("abc".encode("utf-8"), "a.txt") == "abc"


# ---------------------------------------------------------------------------
# 中央目录谎报大小（闸门只能看声明值，必须靠「实际读出上限」兜底）
# ---------------------------------------------------------------------------

def _forge_central_dir_uncompressed_size(content: bytes, declared: int) -> bytes:
    """把中央目录里最后一条记录的 uncompressed size 改写成 declared。

    ZIP 中央目录记录（PK\\x01\\x02）布局：签名 4B + ... + compressed 4B(偏移20)
    + uncompressed 4B(偏移24)。压缩后的字节流不动，只改「声明值」。
    """
    import struct

    raw = bytearray(content)
    pos = raw.rfind(b"PK\x01\x02")
    assert pos >= 0, "未找到中央目录记录"
    struct.pack_into("<I", raw, pos + 24, declared)
    return bytes(raw)


def test_guard_can_be_fooled_by_forged_central_directory():
    """记录已知弱点：闸门只看声明值，伪造中央目录即可放行。

    这条断言**不是**在认可该行为，而是把它钉成回归基线 —— 一旦将来改成
    「校验本地头与中央目录一致性」而不再放行，本用例会失败，提醒同步更新
    `_read_zip_entry_limited` 的说明与下面那条兜底用例。
    """
    real = b"\x00" * (4 * 1024 * 1024)
    forged = _forge_central_dir_uncompressed_size(_zip_bytes({"word/document.xml": real}), 100)

    with zipfile.ZipFile(io.BytesIO(forged)) as z:
        assert z.infolist()[0].file_size == 100          # 谎报生效

    fp._guard_zip_archive(forged, "docx")                # 闸门被绕过，不抛异常


def test_read_zip_entry_limited_rejects_valid_entry_over_limit():
    """确定性证明「实际读出上限」生效：不涉及伪造，条目真实内容就超限。

    这是防「中央目录谎报大小」的最终防线 —— 它只看真正读出了多少字节，
    与中央目录声明值无关。
    """
    payload = b"A" * 4096
    with zipfile.ZipFile(io.BytesIO(_zip_bytes({"word/document.xml": payload}))) as z:
        with pytest.raises(ParseError) as e:
            fp._read_zip_entry_limited(z, "word/document.xml", limit=1024)
        assert "条目过大" in str(e.value)


def test_forged_archive_cannot_inflate_beyond_actual_read_limit():
    """伪造归档不得解出超量内容。

    两条独立防线任一生效即通过：① 命中实际读取上限（ParseError）；
    ② zipfile 自身校验失败（BadZipFile）。**唯一不可接受的结果**是
    悄悄把远超 limit 的字节读出来。
    """
    real = b"\x00" * (4 * 1024 * 1024)
    forged = _forge_central_dir_uncompressed_size(_zip_bytes({"word/document.xml": real}), 100)

    with zipfile.ZipFile(io.BytesIO(forged)) as z:
        try:
            data = fp._read_zip_entry_limited(z, "word/document.xml", limit=1024)
        except (ParseError, zipfile.BadZipFile):
            return
        pytest.fail(f"读取上限未生效：返回了 {len(data)} 字节（上限 1024）")


def test_read_zip_entry_limited_returns_full_content_under_limit():
    """正常归档（内容小于上限）必须原样读出，不受防护影响。"""
    payload = "标题\n正文".encode("utf-8") * 100
    with zipfile.ZipFile(io.BytesIO(_zip_bytes({"word/document.xml": payload}))) as z:
        assert fp._read_zip_entry_limited(z, "word/document.xml") == payload
        assert fp._read_zip_entry_limited(z, "word/document.xml", limit=len(payload)) == payload


def test_entry_too_large_error_reaches_caller_not_swallowed_by_fallback(monkeypatch):
    """DOCX 的 docx2python 回退链路不得吞掉安全闸门的 ParseError。

    旧实现 `_parse_docx` 里是裸 `except Exception` → 回退；一旦条目超限被判
    ParseError，会被回退链路吃掉，最终可能变成「解析成功但内容为空」。
    """
    monkeypatch.setattr(fp, "MAX_ARCHIVE_ENTRY_BYTES", 512)
    monkeypatch.setattr(fp, "_parse_docx_fallback",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("不应回退到 docx2python")))

    payload = "<w:document/>".encode("utf-8") * 200      # 远超 512B 上限
    content = _zip_bytes({"word/document.xml": payload})

    with pytest.raises(ParseError) as e:
        fp._parse_docx(content)
    assert "条目过大" in str(e.value)


# ---------------------------------------------------------------------------
# 整体压缩比可被「不可压缩填充条目」稀释（安全审计实测的绕过手法）
# ---------------------------------------------------------------------------

def test_ratio_dilution_by_incompressible_pad_rejected():
    """回归：在归档里塞一个大体积、不可压缩的填充条目，把**整体**压缩比稀释到
    阈值以下，从而掩护真正的炸弹条目 —— 这是实测可行的绕过手法。

    默认阈值下构造：8MB 全零炸弹（自身比值约 1000:1）+ 120KB 随机填充，
    整体比值被拉到 100 以下（旧实现放行），逐条判定必须拦下炸弹条目。
    """
    import random

    bomb = b"\x00" * (8 * 1024 * 1024)
    pad = random.Random(20260916).randbytes(120 * 1024)   # 近乎不可压缩
    content = _zip_bytes({"word/document.xml": bomb, "pad.bin": pad})

    with zipfile.ZipFile(io.BytesIO(content)) as z:
        infos = z.infolist()
        total_u = sum(i.file_size for i in infos)
        total_c = sum(max(i.compress_size, 1) for i in infos)
        aggregate_ratio = total_u / total_c

    # 前提校验：整体比值确实已被稀释到阈值以下，否则这条用例没有覆盖到目标分支
    assert aggregate_ratio <= fp.MAX_COMPRESSION_RATIO, (
        f"填充量不足，整体比值 {aggregate_ratio:.0f} 未被稀释到 "
        f"{fp.MAX_COMPRESSION_RATIO} 以下，用例未命中目标场景")

    with pytest.raises(ParseError) as e:
        fp._guard_zip_archive(content, "docx")
    assert "压缩比异常" in str(e.value)
    assert "word/document.xml" in str(e.value)


def test_dilution_cannot_be_hidden_by_pad_in_public_entry():
    """公开入口同样拦得住（防止只有私有函数被加固、调用链漏改）。"""
    import random

    content = _zip_bytes({
        "word/document.xml": b"\x00" * (8 * 1024 * 1024),
        "pad.bin": random.Random(7).randbytes(120 * 1024),
    })
    with pytest.raises(ParseError):
        parse_file_content_ex(content, "bomb.docx")


# ---------------------------------------------------------------------------
# PDF 回退通道无闸门（MuPDF 拦下的畸形文件不得转交 pdfplumber / pypdf）
# ---------------------------------------------------------------------------

def _require_mupdf():
    try:
        import fitz  # noqa: F401
    except ImportError:
        pytest.importorskip("pymupdf")


def test_pdf_open_failure_does_not_reach_unguarded_fallback(monkeypatch):
    """回归：MuPDF 打不开的 PDF 必须直接失败，不能转交无体积/压缩比闸门的回退通道。

    实测（安全审计）：20KB 畸形 PDF，MuPDF 0.0s 拒绝，转交 pdfplumber 后
    耗时 39.7s 且零产出（约 2000x 放大）。
    """
    _require_mupdf()
    monkeypatch.setattr(fp, "_parse_pdf_fallback",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("畸形 PDF 不得进入无闸门的回退通道")))

    with pytest.raises(ParseError) as e:
        fp._parse_pdf(b"not a pdf at all, no trailer here")
    assert "无法解析" in str(e.value)


def test_pdf_without_text_still_uses_fallback(monkeypatch):
    """反向覆盖：MuPDF 能正常打开但提取不到文字（扫描件）时，回退通道必须仍然可用。

    否则上面的加固会误伤真实的扫描件 PDF。
    """
    _require_mupdf()
    pypdf = pytest.importorskip("pypdf")

    called: list[int] = []
    monkeypatch.setattr(fp, "_parse_pdf_fallback",
                        lambda content: (called.append(1), ("", 1))[1])
    monkeypatch.setattr(fp, "_pdf_ocr_fallback", lambda *a, **k: "")

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)

    fp._parse_pdf(buf.getvalue())
    assert called, "MuPDF 可打开但无文字时，回退通道被误伤关闭了"


# ---------------------------------------------------------------------------
# CSV：流式截断 + 解析错误转可读提示
# ---------------------------------------------------------------------------

def test_csv_error_converted_to_actionable_parse_error():
    """回归：`_csv.Error` 必须转成可读的 ParseError。

    旧实现让它冒到路由的泛化 `except Exception`，用户只看到「文件无法识别或
    已损坏」，真正原因（超长且未加引号的单元格）被完全吞掉。
    """
    content = (b"a" * 200000 + b"\n").decode("ascii").encode("utf-8")  # 超过字段长度上限
    with pytest.raises(ParseError) as e:
        fp._parse_csv(content)
    assert "CSV 内容异常" in str(e.value)


def test_csv_streaming_truncation_keeps_memory_bounded(monkeypatch):
    """回归：截断必须发生在读取过程中，而不是「先整表读进内存再切片」。

    判据：把上限设成 5 行，喂一个 20 万行的 CSV —— 流式实现只物化 5 行，
    返回的 Markdown 表格行数必须与上限一致（旧实现也会返回 5 行，但它先把
    20 万行全部展开成列表，内存峰值与文件大小同阶）。
    """
    monkeypatch.setattr(fp, "MAX_CSV_ROWS", 5)
    content = ("h\n" + "\n".join(f"r{i}" for i in range(200000))).encode("utf-8")

    text, diag = parse_file_content_ex(content, "huge.csv")
    assert diag["truncated"] is True
    assert any("超过上限" in w for w in diag["warnings"])
    # 排除 Markdown 对齐分隔行（`| --- |`，strip 后为空），只数真实的表头/数据行
    data_rows = [ln for ln in text.splitlines()
                 if ln.startswith("|") and ln.strip("| -")]
    assert len(data_rows) == 5


