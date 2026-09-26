"""文件导入/解析模块增强与修复的回归测试。

覆盖本轮修复的问题（每条用例对应一处真实缺陷，防止回退）：

1. **表格语法不完整**：CSV / Excel / DOCX 输出的 Markdown 表格缺少对齐分隔行，
   前端 marked(gfm) 渲染解析预览时不会识别为表格；DOCX 表格单元格内的竖线
   未转义，会撕裂列结构（机械统计表丢列）。
2. **目录节点 id 冲突**：`simple_parse_outline` 直接用点分编号当节点 id，
   同一编号重复出现时产生重复 id（前端 Tree React key 冲突、落库编号串号）。
3. **多页 TIFF 只识别首帧**：第 2 页起内容静默丢失。
4. **文件头校验口径不一致**：目录识别上传链路此前完全没有扩展名伪造校验。
5. **解析诊断丢失**：`/upload-outline/parse` 用无诊断入口，PDF 截页 / 表格截行 /
   OCR 兜底等告警全部丢弃。
6. **跨模块数据传递缺陷**：目录/正文生成取已解析文档时"先 LIMIT 再过滤空值"，
   未解析文档占满名额 → 已解析资料对生成完全不可见。
7. **批量解析不写 parse_time**：与单文档解析口径不一致，前端"解析用时"恒为空。
"""
import io
import uuid
import zipfile

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

import app.db as _appdb
import app.routers.global_facts as gf
import app.routers.upload_outline as uo
import app.services.file_parser as fp
from app.db import get_conn, init_db


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


# ---------------------------------------------------------------------------
# 1. Markdown 表格：对齐分隔行 + 单元格竖线转义
# ---------------------------------------------------------------------------

def _table_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("|")]


def test_csv_table_has_alignment_separator():
    out = fp.parse_file_content(
        "设备名称,规格型号,数量\n塔吊,QTZ80,2\n".encode("utf-8"), "equip.csv")
    lines = _table_lines(out)
    assert lines[0] == "| 设备名称 | 规格型号 | 数量 |"
    # ✅ 对齐分隔行必须存在，否则 GFM 不渲染为表格
    assert lines[1] == "| --- | --- | --- |"
    assert lines[2] == "| 塔吊 | QTZ80 | 2 |"


def test_excel_table_has_alignment_separator():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "机械表"
    ws.append(["设备名称", "数量"])
    ws.append(["塔吊", 2])
    buf = io.BytesIO()
    wb.save(buf)

    out = fp.parse_file_content(buf.getvalue(), "equip.xlsx")
    lines = _table_lines(out)
    assert lines[0] == "| 设备名称 | 数量 |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| 塔吊 | 2 |"


def _minimal_docx_with_table() -> bytes:
    """构造只含一个段落 + 一张两行两列表格的最小 OOXML。"""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w='
        '"http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        "<w:p><w:r><w:t>设备清单</w:t></w:r></w:p>"
        "<w:tbl>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>设备名称</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>规格|备注</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>塔吊</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>QTZ80</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "</w:tbl>"
        "</w:body></w:document>"
    ).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_docx_table_escapes_pipe_and_has_separator():
    out = fp.parse_file_content(_minimal_docx_with_table(), "t.docx")
    lines = _table_lines(out)

    assert lines[0] == "| 设备名称 | 规格／备注 |", "单元格内竖线必须转义"
    assert lines[1] == "| --- | --- |", "缺失对齐分隔行 → 前端不渲染为表格"
    assert lines[2] == "| 塔吊 | QTZ80 |"
    # 原始竖线不得残留在单元格内（否则被下游当作列分隔符）
    assert "规格|备注" not in out


def test_separator_column_count_matches_header():
    """分隔行列数必须与表头一致，否则 GFM 表格列错位。"""
    out = fp.parse_file_content("a,b,c,d\n1,2,3,4\n".encode("utf-8"), "x.csv")
    header = _table_lines(out)[0]
    sep = _table_lines(out)[1]
    assert header.count("|") == sep.count("|")


# ---------------------------------------------------------------------------
# 2. 目录节点 id 唯一性
# ---------------------------------------------------------------------------

def _collect_ids(nodes: list, out: list) -> None:
    for n in nodes:
        out.append(n["id"])
        _collect_ids(n.get("children") or [], out)


def test_duplicate_decimal_numbering_ids_are_unique():
    """同一编号重复出现（OCR 误识别/跨章重复编号）时 id 不得重复。"""
    text = "1.1 甲\n1.1 乙\n1.1.1 丙\n1.1 丁\n"
    outline = fp.simple_parse_outline(text)
    ids: list[str] = []
    _collect_ids(outline, ids)
    assert len(ids) == 4
    assert len(ids) == len(set(ids)), f"目录节点 id 重复: {ids}"


def test_unique_numbering_ids_are_preserved():
    """未冲突的编号仍沿用编号本身作 id（不改变既有语义）。"""
    outline = fp.simple_parse_outline("1.1 甲\n1.2 乙\n")
    ids: list[str] = []
    _collect_ids(outline, ids)
    assert ids == ["1.1", "1.2"]


# ---------------------------------------------------------------------------
# 3. 多页 TIFF 逐帧识别
# ---------------------------------------------------------------------------

def test_multi_frame_tiff_ocrs_every_frame(monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image

    import app.services.ocr as ocr_mod

    frames = [Image.new("RGB", (12, 12), c) for c in ("white", "black", "gray")]
    buf = io.BytesIO()
    frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])
    data = buf.getvalue()

    calls: list[bytes] = []

    class _Res:
        def __init__(self, text: str):
            self.text = text

    def fake_ocr(payload, **_kw):
        calls.append(payload)
        return _Res(f"第{len(calls)}帧内容")

    monkeypatch.setattr(ocr_mod, "ocr_bytes_sync", fake_ocr)
    out = fp._parse_image_ocr(data, "tiff")

    assert len(calls) == 3, "多页 TIFF 必须逐帧识别（旧实现只取首帧）"
    assert "第1帧内容" in out and "第3帧内容" in out


def test_single_page_image_uses_single_call(monkeypatch):
    """单帧图片不得被拆帧逻辑影响（仍只调用一次 OCR）。"""
    pytest.importorskip("PIL")
    from PIL import Image

    import app.services.ocr as ocr_mod

    buf = io.BytesIO()
    Image.new("RGB", (12, 12), "white").save(buf, format="PNG")

    calls: list[bytes] = []

    class _Res:
        text = "单帧"

    monkeypatch.setattr(ocr_mod, "ocr_bytes_sync", lambda payload, **k: (calls.append(payload), _Res())[1])
    assert fp._parse_image_ocr(buf.getvalue(), "png") == "单帧"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 4. 公共文件头校验
# ---------------------------------------------------------------------------

def test_signature_valid_public_helper():
    assert fp.signature_valid("pdf", b"%PDF-1.7") is True
    assert fp.signature_valid("pdf", b"not-a-pdf") is False
    assert fp.signature_valid("docx", b"PK\x03\x04rest") is True
    assert fp.signature_valid("docx", b"\xd0\xcf\x11\xe0rest") is True
    assert fp.signature_valid("xlsx", b"plain text") is False
    assert fp.signature_valid("png", b"\x89PNG\r\n\x1a\n") is True
    assert fp.signature_valid("tiff", b"MM\x00*") is True
    # 文本类未登记 → 一律放行（保持兼容）
    assert fp.signature_valid("txt", b"anything") is True
    assert fp.signature_valid("", b"anything") is True


def test_global_facts_signature_helper_delegates():
    """global_facts 保留原函数名，但实现已统一到 file_parser。"""
    assert gf._signature_valid("pdf", b"%PDF-1.7") is True
    assert gf._signature_valid("pdf", b"nope") is False


# ---------------------------------------------------------------------------
# 5. 目录识别：文件头校验 + 解析诊断回传
# ---------------------------------------------------------------------------

async def test_parse_outline_rejects_spoofed_binary(db_conn):
    with pytest.raises(HTTPException) as e:
        await uo.parse_outline(
            file=_upload("fake.png", b"this is definitely not a png"),
            scheme_name="", reorganize=False, db=db_conn)
    assert e.value.status_code == 400
    assert "不符" in e.value.detail


async def test_parse_outline_reports_parser_warnings(db_conn, monkeypatch):
    """CSV 行数截断必须随识别响应回传（旧实现用无诊断入口，告警全丢）。"""
    monkeypatch.setattr(fp, "MAX_CSV_ROWS", 2)

    async def _no_ai(*_a, **_k):
        raise RuntimeError("测试环境不调用 AI")

    monkeypatch.setattr(uo, "collect_json_response", _no_ai)

    csv = ("表头1,表头2\n" + "\n".join(f"r{i},v{i}" for i in range(20))).encode("utf-8")
    res = await uo.parse_outline(
        file=_upload("outline.csv", csv),
        scheme_name="", reorganize=False, db=db_conn)

    assert res.get("parse_truncated") is True
    assert any("超过上限" in w for w in res.get("parse_warnings", [])), res


async def test_parse_outline_text_file_has_no_warnings(db_conn, monkeypatch):
    """正常文本文件不得凭空产生解析告警（避免误报）。"""
    async def _no_ai(*_a, **_k):
        raise RuntimeError("测试环境不调用 AI")

    monkeypatch.setattr(uo, "collect_json_response", _no_ai)

    text = "\n".join(f"第{i}章 章节{i}" for i in range(1, 7))
    res = await uo.parse_outline(
        file=_upload("outline.txt", text.encode("utf-8")),
        scheme_name="", reorganize=False, db=db_conn)

    assert "parse_warnings" not in res
    assert "parse_truncated" not in res
    assert "empty_text" not in res


async def test_parse_outline_reports_empty_text(db_conn, monkeypatch):
    """空白文件必须明确回传原因（避免用户只看到"共 0 个章节"而反复重试）。"""
    async def _no_ai(*_a, **_k):
        raise RuntimeError("测试环境不调用 AI")

    monkeypatch.setattr(uo, "collect_json_response", _no_ai)

    res = await uo.parse_outline(
        file=_upload("blank.txt", b"   \n\n  "),
        scheme_name="", reorganize=False, db=db_conn)

    assert res["empty_text"] is True
    assert "未从文件中解析到有效文本" in res["warning"]
    assert res["outline"] == []


# ---------------------------------------------------------------------------
# 6. 跨模块取已解析文档：非空条件下推 SQL
# ---------------------------------------------------------------------------

async def test_load_parsed_texts_skips_unparsed_documents(db_conn):
    from app.routers.global_facts import load_parsed_texts

    pid = "p-parse"
    for i, md in enumerate(["", "已解析A", "", "已解析B"]):
        await db_conn.execute(
            "INSERT INTO project_documents (id, project_id, file_name, parsed_markdown)"
            " VALUES (?,?,?,?)", (f"d{i}", pid, f"f{i}.txt", md))
    await db_conn.commit()

    assert await load_parsed_texts(db_conn, pid, limit=5) == ["已解析A", "已解析B"]
    # ✅ 关键回归：旧实现"先 LIMIT 2 再过滤"会得到 []（未解析文档占满名额）
    assert await load_parsed_texts(db_conn, pid, limit=2) == ["已解析A", "已解析B"]


async def test_load_parsed_texts_empty_project_id(db_conn):
    from app.routers.global_facts import load_parsed_texts
    assert await load_parsed_texts(db_conn, "", limit=5) == []


async def test_load_parsed_docs_returns_name_text_pairs(db_conn):
    """✅ 新增唯一入口：带文件名的规范读取（事实提取拼「=== 文件名 ===」用）。

    同一份「已解析文档」SQL 现收敛到 load_parsed_docs，load_parsed_texts 委托它，
    事实提取也复用 —— 本用例钉住"跳过未解析 + 带文件名 + 固定顺序 + limit 作用于
    非空之后"这四条不变量。
    """
    from app.routers.global_facts import load_parsed_docs

    pid = "p-docs"
    for i, md in enumerate(["", "甲", "", "乙"]):
        await db_conn.execute(
            "INSERT INTO project_documents (id, project_id, file_name, parsed_markdown)"
            " VALUES (?,?,?,?)", (f"d{i}", pid, f"f{i}.txt", md))
    await db_conn.commit()

    # 全量（limit=None，事实提取用）：只返回非空文档，带文件名、固定 (created_at,id) 顺序
    assert await load_parsed_docs(db_conn, pid) == [("f1.txt", "甲"), ("f3.txt", "乙")]
    # limit 仍作用在「过滤非空之后」（不是"先 LIMIT 再过滤"）
    assert await load_parsed_docs(db_conn, pid, limit=1) == [("f1.txt", "甲")]


async def test_load_parsed_docs_empty_project_id(db_conn):
    from app.routers.global_facts import load_parsed_docs
    assert await load_parsed_docs(db_conn, "") == []
    assert await load_parsed_docs(db_conn, "", limit=None) == []


# ---------------------------------------------------------------------------
# 7. 批量解析写入 parse_time
# ---------------------------------------------------------------------------

@pytest.fixture
async def gf_ctx(tmp_path, monkeypatch):
    """真实磁盘库 + 隔离上传目录（批量解析需要文件真实落盘）。"""
    _appdb.DB_PATH = tmp_path / "file-import.sqlite"
    await init_db()
    db = await get_conn()
    uploads = tmp_path / "uploads" / "facts"
    uploads.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)

    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    fpath = uploads / pid / "d1_a.txt"
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_bytes("工程名称：某某深基坑工程，开挖深度 8.5 米".encode("utf-8"))
    await db.execute(
        "INSERT INTO project_documents (id, project_id, file_name, file_type,"
        " parsed_markdown, file_path) VALUES (?,?,?,?,?,?)",
        ("d1", pid, "a.txt", "txt", "", str(fpath)))
    await db.commit()
    yield db, pid


async def test_parse_all_documents_records_parse_time(gf_ctx):
    db, pid = gf_ctx
    res = await gf.parse_all_documents(
        scheme_id="", project_id=pid, force=False, db=db)

    assert res["parsed"] == 1
    cur = await db.execute(
        "SELECT parsed_markdown, parse_time FROM project_documents WHERE id='d1'")
    row = await cur.fetchone()
    assert row["parsed_markdown"], "解析结果必须落库"
    assert row["parse_time"] is not None and row["parse_time"] >= 0, \
        "批量解析必须记录 parse_time（与单文档解析口径一致）"


async def test_parse_all_documents_reports_failure_without_losing_others(gf_ctx):
    """单个文档解析失败不得影响其它文档，且失败原因必须回传。"""
    db, pid = gf_ctx
    missing = gf.FACT_UPLOADS_DIR / pid / "d2_missing.txt"
    await db.execute(
        "INSERT INTO project_documents (id, project_id, file_name, file_type,"
        " parsed_markdown, file_path) VALUES (?,?,?,?,?,?)",
        ("d2", pid, "missing.txt", "txt", "", str(missing)))
    await db.commit()

    res = await gf.parse_all_documents(
        scheme_id="", project_id=pid, force=False, db=db)
    assert res["parsed"] == 1
    assert res["failed_count"] == 1
    assert res["failed"][0]["file_name"] == "missing.txt"
