"""解析提取（import）模块 —— 对抗边界 + 跨链路契约测试（2026-10-08）

本文件补齐既有 import 套件未直接钉住的边界与契约，全部结论基于
`C:\\gitee_work`（与工作区 `专项方案工具箱` 同源的镜像）的实际运行结果：

1. 解析器边界：告警序列化上限、不可见字符归一、编码回退链、CSV BOM/引号/空行、
   ZIP 压缩炸弹闸门边界（2000/2001 条目、单条目膨胀比）、OLE 类型细分。
2. 上传路由（upload_outline.parse_outline）防御：超限 413、扩展名白名单 400、
   魔数不一致 400、未知 project_id 404、空文件 400、解析告警落库。
3. 分类契约：auto_classify_document / category_options / extract_priority
   与前端下拉选项的一致性（单一事实源）。

与既有用例的关系：不重复 test_file_parser / test_upload_format_contract /
test_file_import_pipeline 已覆盖的签名字段，只补其未覆盖的**数值边界**与
**告警/编码/分类**契约。
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest
from app.routers import upload_outline as uo
from app.services import doc_categories as dc
from app.services import file_parser as fp
from fastapi import HTTPException
from starlette.datastructures import UploadFile

# ---------------------------------------------------------------------------
# 1. 解析告警序列化（dump_parse_warnings）数值边界
# ---------------------------------------------------------------------------

def test_dump_parse_warnings_caps_total_count():
    """告警条数上限：25 条只保留前 20 条，且序列化结果必须仍是合法 JSON。"""
    src = [f"w{i}" for i in range(25)]
    dumped = fp.dump_parse_warnings(src)
    parsed = json.loads(dumped)          # 非法 JSON 会在这里抛异常
    assert len(parsed) == fp.MAX_PARSE_WARNINGS == 20
    assert parsed[0] == "w0" and parsed[-1] == "w19"


def test_dump_parse_warnings_limits_single_length():
    """单条告警长度上限：300 字保留，301 字截断并带省略号，仍是合法 JSON。"""
    dumped_ok = fp.dump_parse_warnings(["x" * fp.MAX_PARSE_WARNING_CHARS])
    parsed_ok = json.loads(dumped_ok)
    assert len(parsed_ok[0]) == 300

    dumped_cut = fp.dump_parse_warnings(["y" * (fp.MAX_PARSE_WARNING_CHARS + 1)])
    parsed_cut = json.loads(dumped_cut)
    assert len(parsed_cut[0]) == fp.MAX_PARSE_WARNING_CHARS  # 300 字符（含省略号）
    assert parsed_cut[0].endswith("…")


def test_dump_parse_warnings_empty_and_none():
    """空列表 / None / 混入空格与 None 元素都必须产出合法 JSON 数组。"""
    assert json.loads(fp.dump_parse_warnings([])) == []
    assert json.loads(fp.dump_parse_warnings(None)) == []
    mixed = json.loads(fp.dump_parse_warnings(["中文", "  ", None]))
    # str() 归一使 None → "None"（保持可读），空串/空格被过滤
    assert mixed == ["中文", "None"]


# ---------------------------------------------------------------------------
# 2. 不可见字符归一（_normalize_invisible）
# ---------------------------------------------------------------------------

def test_normalize_invisible_removes_cr_nul_zwj_nbsp():
    """CR / NUL / 零宽字符删除，NBSP 归一为空格；\n 与 \t 保留。"""
    from app.services.file_parser import _normalize_invisible
    text = "a\r\nb\x00c\u200bd\u200ce\u200df\u00a0g\th"
    out, changed = _normalize_invisible(text)
    assert changed is True
    assert out == "a\nbcdef g\th"


# ---------------------------------------------------------------------------
# 3. 编码回退链（_decode_text_ex）
# ---------------------------------------------------------------------------

def test_decode_utf8_bom_no_warning():
    """UTF-8 BOM 剥离，且不应产生任何下降级告警。"""
    text, warnings = fp._decode_text_ex("\ufeff名称\n".encode("utf-8"))
    assert text == "名称\n" and not text.startswith("\ufeff")
    assert warnings == []


def test_decode_gbk_adds_warning_with_correct_text():
    """GBK 文本正确解码，同时必须带「已按 gbk 解码」告警。"""
    text, warnings = fp._decode_text_ex("中文测试\n".encode("gbk"))
    assert "中文测试\n" in text
    assert any("gbk" in w for w in warnings)


def test_decode_unrecognized_encoding_lossy_with_warning():
    """Big5 这类无法识别的编码：不得崩溃，必须产生乱码告警。"""
    text, warnings = fp._decode_text_ex("中文測試\n".encode("big5"))
    assert isinstance(text, str)
    assert any("无法识别" in w or "Big5" in w or "乱码" in w for w in warnings)


def test_decode_utf16_text_outputs_warning_and_no_crash():
    """UTF-16 编码文本：不得崩溃且必须给出编码告警（避免静默乱码入库）。"""
    text, warnings = fp._decode_text_ex("name,age\n".encode("utf-16"))
    assert isinstance(text, str)
    assert len(warnings) >= 1


# ---------------------------------------------------------------------------
# 4. CSV 解析边界
# ---------------------------------------------------------------------------

def test_csv_bom_utf8_and_crlf_and_quoted_comma():
    """BOM、CRLF、引号内逗号三种边界共处一份 CSV 必须完整解析。"""
    payload = "\ufeff设备,数量\n塔吊,\"3,5 台\"\n起重机,2\r\n".encode("utf-8")
    out = fp.parse_file_content(payload, "equip.csv")
    assert "设备" in out and "塔吊" in out
    assert "| --- |" in out            # 对齐分隔行
    assert "3,5 台" in out


def test_csv_gbk_encoding_parses():
    """GBK 编码 CSV 走完整回退链解析，中文内容保留。"""
    payload = "名称,数量\n测试,5\n".encode("gbk")
    out, _diag = fp.parse_file_content_ex(payload, "x.csv")
    assert "测试" in out and "数量" in out


def test_csv_only_header_and_empty_rows_no_crash():
    """只有表头 / 全是空行的 CSV 不得崩溃，仍产出结构化表格。"""
    out1, d1 = fp.parse_file_content_ex(b"header1,header2\n", "x.csv")
    assert "【表格】" in out1 and "header1" in out1
    out2, d2 = fp.parse_file_content_ex(b"a,b\n\n\n", "x.csv")
    assert "【表格】" in out2


def test_csv_utf16le_rejected_as_binary():
    """UTF-16LE 的 CSV 被二进制防护拒绝（不是静默乱码入库）。"""
    with pytest.raises(fp.ParseError):
        fp.parse_file_content_ex("a,b\n1,2\n".encode("utf-16-le"), "x.csv")
# ---------------------------------------------------------------------------
# 5. ZIP 归档安全闸门（_guard_zip_archive）边界
# ---------------------------------------------------------------------------

def _mkzip(n: int, size: int = 10, compress: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w",
                         zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED) as z:
        for i in range(n):
            z.writestr(f"f{i}.txt", ("x" * size).encode())
    return buf.getvalue()


def test_zip_guard_boundary_2000_ok_2001_blocked():
    """条目数恰在上限 2000 必须放行，2001 必须拒绝。"""
    fp._guard_zip_archive(_mkzip(fp.MAX_ARCHIVE_ENTRIES), "docx")   # 不抛
    with pytest.raises(fp.ParseError):
        fp._guard_zip_archive(_mkzip(fp.MAX_ARCHIVE_ENTRIES + 1), "docx")


def test_zip_guard_empty_zip_passes():
    """空 ZIP（0 条目）不得被误拒（后续解析自行处理缺 document.xml）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w"):
        pass
    fp._guard_zip_archive(buf.getvalue(), "docx")


def test_zip_guard_single_entry_ratio_bomb():
    """单条目超高压缩比（合成炸弹）必须拒绝 —— 汇总比值可被稀释，逐条判定必须生效。"""
    bomb = _mkzip(1, size=fp.MAX_COMPRESSION_RATIO * 64 + 64, compress=True)
    with pytest.raises(fp.ParseError):
        fp._guard_zip_archive(bomb, "docx")


def test_parse_empty_docx_zip_returns_empty_text():
    """合法 ZIP 但缺 word/document.xml：防护通过、解析返回空文本（不崩溃）。
    上层路线（global_facts.parse_document）随后按「未解析到有效文本」判失败。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w"):
        pass
    text, diag = fp.parse_file_content_ex(buf.getvalue(), "x.docx")
    assert text == "" and diag["file_type"] == "docx"


# ---------------------------------------------------------------------------
# 6. OLE 复合文档类型细分
# ---------------------------------------------------------------------------

def test_ole_content_kind_xls_vs_doc_markers():
    """OLE UTF-16 流名细分：Workbook → xls；WordDocument → doc；双标记 → 空。"""
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    assert fp._ole_content_kind(ole + "Workbook".encode("utf-16-le")) == "xls"
    assert fp._ole_content_kind(ole + "WordDocument".encode("utf-16-le")) == "doc"
    both = ole + "Workbook".encode("utf-16-le") + "WordDocument".encode("utf-16-le")
    assert fp._ole_content_kind(both) == ""


# ---------------------------------------------------------------------------
# 7. 上传路由防御（upload_outline.parse_outline）
# ---------------------------------------------------------------------------

def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


async def _call_parse(db_conn, name, data, **kw):
    return await uo.parse_outline(file=_upload(name, data), db=db_conn, **kw)


async def test_parse_outline_oversize_rejected_413(db_conn, monkeypatch):
    """超过单文件上限（monkeypatch 调小）必须 413，不得整读进内存。"""
    monkeypatch.setattr(uo, "MAX_UPLOAD_BYTES", 4)
    with pytest.raises(HTTPException) as ei:
        await _call_parse(db_conn, "big.txt", b"x" * 32)
    assert ei.value.status_code == 413


async def test_parse_outline_unsupported_extension_400(db_conn):
    """白名单外扩展名必须在解析前被 400 拒绝。"""
    with pytest.raises(HTTPException) as ei:
        await _call_parse(db_conn, "evil.exe", b"MZ\x90\x00" + b"\x00" * 100)
    assert ei.value.status_code == 400


async def test_parse_outline_magic_mismatch_400(db_conn):
    """扩展名与真实内容不符（.docx 实为 PE）必须 400；文本类由二进防护兜底。"""
    with pytest.raises(HTTPException) as ei:
        await _call_parse(db_conn, "fake.docx", b"MZ\x90\x00" + b"\x00" * 64)
    assert ei.value.status_code == 400


async def test_parse_outline_empty_file_400(db_conn):
    """0 字节文件必须 400「空文件」。"""
    with pytest.raises(HTTPException) as ei:
        await _call_parse(db_conn, "empty.txt", b"")
    assert ei.value.status_code == 400


async def test_parse_outline_unknown_project_id_404(db_conn):
    """不存在的 project_id 不得静默落库（归属链必须存在）。"""
    with pytest.raises(HTTPException) as ei:
        await _call_parse(db_conn, "a.txt",
                          "第一章 概述\n第二章 部署\n".encode("utf-8"),
                          project_id="no-such-project")
    assert ei.value.status_code == 404


async def test_parse_outline_persists_warning_and_record(db_conn, monkeypatch):
    """GBK 文本上传：解析记录落库且 parse_warnings 含编码降级告警。"""
    monkeypatch.setattr(uo, "MAX_UPLOAD_BYTES", 5 * 1024 * 1024)
    res = await _call_parse(db_conn, "gbk.txt",
                            "一、工程概况\n二、施工部署\n三、资源配置\n".encode("gbk"))
    assert res["id"]
    cur = await db_conn.execute(
        "SELECT parse_warnings FROM uploaded_outlines WHERE id=?",
        (res["id"],))
    row = await cur.fetchone()
    assert row is not None
    warnings = json.loads(row["parse_warnings"])
    assert any("gbk" in w for w in warnings)


async def test_parse_outline_success_persists_record(db_conn, monkeypatch):
    """正常文本：识别记录落库、confidence 落在合法区间、outline 有结构。"""
    monkeypatch.setattr(uo, "MAX_UPLOAD_BYTES", 5 * 1024 * 1024)
    res = await _call_parse(
        db_conn, "outline.txt",
        "一、工程概况\n施工范围说明\n二、施工部署\n总体流程\n三、资源配置\n人员机械\n".encode("utf-8"))
    assert res["id"] and res["outline"]
    assert 0.4 <= res["confidence"] <= 0.95
    cur = await db_conn.execute(
        "SELECT status, file_name, confidence FROM uploaded_outlines WHERE id=?",
        (res["id"],))
    row = await cur.fetchone()
    assert row["status"] == "parsed"
    assert row["file_name"] == "outline.txt"


# ---------------------------------------------------------------------------
# 8. 分类契约（单一事实源）
# ---------------------------------------------------------------------------

def test_category_options_is_single_source():
    """分类选项与自动分类兜底必须同源：任何文本最终落回选项集合内。"""
    options = set(dc.category_options())
    assert options == set(dc.ALL_DOC_CATEGORIES)
    assert dc.OTHER_CATEGORY in options
    for name in ("招标文件.docx", "施工组织设计.pdf", "奇怪名字.xlsx",
                 "无扩展名", "b7F3ca.txt"):
        cat = dc.auto_classify_document(name)
        assert cat in options, f"分类结果 {cat} 不在选项集合内"


def test_extract_priority_unknown_is_last():
    """未知分类的提取优先级必须最大（最后），空值等价「其他」。"""
    unknown = dc.extract_priority("不存在分类")
    assert unknown >= max(dc.extract_priority(c) for c in dc.ALL_DOC_CATEGORIES)
    assert dc.extract_priority(None) == dc.extract_priority(dc.OTHER_CATEGORY)