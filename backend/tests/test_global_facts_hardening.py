"""全局事实模块增强回归测试：作用域、文件边界与重提取冲突。"""
import uuid

import app.db as _appdb
import pytest
from app.db import get_conn, init_db
from app.routers.global_facts import (
    _managed_upload_path,
    _persist_parse_result,
    _resolve_project_id,
    _safe_filename,
    _signature_valid,
    batch_resolve,
)
from app.routers.projects import delete_project
from app.routers.sse_handlers import _load_facts_rows
from app.services.facts_extractor import (
    ExtractionResult,
    FactGroup,
    FactItem,
    persist_extraction,
)


@pytest.fixture
async def db_ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "facts-hardening.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    sid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid


def test_upload_signature_rejects_extension_spoofing():
    assert _signature_valid("pdf", b"not-a-pdf") is False
    assert _signature_valid("pdf", b"%PDF-1.7") is True
    assert _signature_valid("docx", b"plain text") is False
    assert _signature_valid("txt", b"plain text") is True


def test_safe_filename_basic_sanitization():
    # 剥离客户端路径（含 Windows 反斜杠），防目录穿越
    assert _safe_filename(r"C:\a\b\方案.docx") == "方案.docx"
    assert _safe_filename("/tmp/a/方案.docx") == "方案.docx"
    # 非法字符与控制字符替换为下划线
    out = _safe_filename('a:*?"<>|.txt')
    assert "/" not in out and "\\" not in out
    assert out.endswith(".txt")
    # 空/空白/仅点号 → 兜底名
    assert _safe_filename("") == "uploaded_file"
    assert _safe_filename("   ") == "uploaded_file"
    assert _safe_filename("...") == "uploaded_file"
    # None 入参不抛异常
    assert _safe_filename(None) == "uploaded_file"


def test_safe_filename_windows_reserved_names():
    # 保留设备名（含小写/带扩展名）一律加下划线前缀，避免在 Windows 命中设备
    for raw in ["CON", "nul", "NUL.txt", "aux.docx", "Prn.pdf",
                "com1", "COM9.xlsx", "lpt4"]:
        result = _safe_filename(raw)
        assert result.startswith("_"), raw
        assert _safe_filename(raw).upper().split(".", 1)[0].lstrip("_") in {
            "CON", "NUL", "AUX", "PRN", "COM1", "COM9", "LPT4"}
    # 仅主名完全相等才算保留名；正常文件名不受影响
    assert _safe_filename("CONTRACT.docx") == "CONTRACT.docx"
    assert _safe_filename("配置文件.txt") == "配置文件.txt"
    assert _safe_filename("xCON.doc") == "xCON.doc"


def test_managed_upload_path_rejects_outside_path(tmp_path):
    from app.config import FACT_UPLOADS_DIR

    assert _managed_upload_path(str(tmp_path / "outside.txt")) is None
    inside = FACT_UPLOADS_DIR / "project" / "doc.txt"
    assert _managed_upload_path(str(inside)) == inside.resolve()


async def test_resolve_project_id_rejects_mismatched_scope(db_ctx):
    db, pid, sid = db_ctx
    with pytest.raises(Exception) as exc:
        await _resolve_project_id(db, sid, uuid.uuid4().hex)
    assert "不匹配" in str(exc.value)
    assert await _resolve_project_id(db, sid, pid) == pid


async def test_persist_parse_result_writes_all_fields(db_ctx):
    db, _pid, _sid = db_ctx
    doc_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO project_documents(id,project_id,file_name,file_type,parse_status) "
        "VALUES(?,?,?,?,?)",
        (doc_id, _pid, "改名无后缀", "", "pending"),
    )
    await db.commit()

    await _persist_parse_result(
        db,
        doc_id,
        "第一章" * 4,
        1.23,
        ["OCR 兜底", "原文 100000 字，已截断"],
        {"file_type": "pdf"},
        True,
    )
    cur = await db.execute(
        "SELECT parsed_markdown,parse_time,parse_warnings,file_type,parse_status,"
        "parse_truncated FROM project_documents WHERE id=?",
        (doc_id,),
    )
    row = dict(await cur.fetchone())
    assert row["parsed_markdown"] == "第一章" * 4
    assert row["parse_time"] == 1.23
    assert "OCR 兜底" in row["parse_warnings"]
    assert row["file_type"] == "pdf"
    assert row["parse_status"] == "success"
    assert row["parse_truncated"] == 1

    # file_type 空值不得覆盖既有类型：先把类型改成 docx，再以空 diag 重写
    await db.execute(
        "UPDATE project_documents SET file_type='docx' WHERE id=?", (doc_id,))
    await db.commit()
    await _persist_parse_result(db, doc_id, "x", 0.5, [], {"file_type": ""}, False)
    cur = await db.execute(
        "SELECT file_type,parse_truncated FROM project_documents WHERE id=?", (doc_id,))
    row = dict(await cur.fetchone())
    assert row["file_type"] == "docx"
    assert row["parse_truncated"] == 0


async def test_reextract_conflict_not_silently_dropped(db_ctx):
    db, pid, sid = db_ctx
    await persist_extraction(
        ExtractionResult(groups=[FactGroup(
            title="人员角色", category="personnel",
            items=[FactItem(name="项目经理", value="张伟",
                            key="project_manager", category="personnel")],
        )], total_items=1), db, pid, sid)
    await db.execute(
        "UPDATE global_facts SET is_resolved=1 WHERE scheme_id=?", (sid,))
    await db.commit()

    await persist_extraction(
        ExtractionResult(groups=[FactGroup(
            title="人员角色", category="personnel",
            items=[FactItem(name="项目经理", value="李强",
                            key="project_manager", category="personnel", source="new.docx")],
        )], total_items=1), db, pid, sid)
    cur = await db.execute(
        "SELECT content, has_conflict, is_resolved, conflict_keys "
        "FROM global_facts WHERE scheme_id=? AND fact_key=?",
        (sid, "project_manager"),
    )
    row = await cur.fetchone()
    # ✅ 冲突证据不得静默丢弃：新值进入候选并把事实退出注入链路，
    #    保留用户已确认值供人工裁决。
    assert row["is_resolved"] == 1
    assert row["has_conflict"] == 1
    assert "李强" in row["conflict_keys"]
    assert "张伟" in row["content"]


async def test_batch_resolve_is_scoped_to_scheme(db_ctx):
    db, pid, sid = db_ctx
    other_sid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
        (other_sid, pid, "other"),
    )
    await db.execute(
        "INSERT INTO global_facts "
        "(id,project_id,scheme_id,title,content,is_resolved) VALUES (?,?,?,?,?,?)",
        ("fact-a", pid, sid, "a", "a", 0),
    )
    await db.execute(
        "INSERT INTO global_facts "
        "(id,project_id,scheme_id,title,content,is_resolved) VALUES (?,?,?,?,?,?)",
        ("fact-b", pid, other_sid, "b", "b", 0),
    )
    await db.commit()
    await batch_resolve({"scheme_id": sid, "fact_ids": ["fact-a", "fact-b"]}, db)
    cur = await db.execute(
        "SELECT id,is_resolved FROM global_facts WHERE id IN ('fact-a','fact-b') ORDER BY id"
    )
    rows = [dict(r) for r in await cur.fetchall()]
    assert rows == [
        {"id": "fact-a", "is_resolved": 1},
        {"id": "fact-b", "is_resolved": 0},
    ]


async def test_unresolved_non_simulated_fact_not_injected(db_ctx):
    db, _pid, sid = db_ctx
    await db.execute(
        "INSERT INTO global_facts "
        "(id,scheme_id,group_title,title,content,is_simulated,is_resolved,has_conflict) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, "人员角色", "项目经理", "- **项目经理**: 张伟", 0, 0, 0),
    )
    await db.commit()
    assert await _load_facts_rows(db, sid) == []
