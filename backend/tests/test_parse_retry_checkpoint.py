"""R123（2026-10-21）解析失败重试 / 断点续跑 专项回归测试。

覆盖：
- 落库瞬时失败指数退避重试（耗尽前成功 → ok，persist 调用次数 = 1+重试）。
- 结构化失败（ParseError 400）不重试（persist 根本不会被调用）。
- 断点续跑：persist 阶段失败后，检查点缓存已提取文本，下次解析跳过提取直接续跑。
- 检查点记录失败阶段：persist 持续失败 → 检查点行 stage=persist/status=failed，并抛 503。
- force 重解析清除旧失败检查点、走完整提取（不续跑旧失败态）。
"""
import asyncio
import io
import uuid

import app.routers.global_facts as gf
import app.routers.global_facts_docs as gf_docs
import pytest
import pytest_asyncio
from app.config import settings
from starlette.datastructures import UploadFile


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


@pytest_asyncio.fixture
async def ctx(db_conn, tmp_path, monkeypatch):
    """重定向四层存储根目录与上传目录到 tmp，建一个项目。"""
    from app.services.doc_pipeline import doc_storage as store
    uploads = tmp_path / "uploads"
    docs_root = tmp_path / "projects"
    monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)
    monkeypatch.setattr(store, "DOCS_ROOT", docs_root)
    pid = uuid.uuid4().hex
    await db_conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db_conn.commit()
    return db_conn, pid, docs_root, uploads


async def _upload_doc(ctx, content: bytes) -> str:
    db, pid, _docs_root, _uploads = ctx
    f = _upload("doc.txt", content)
    res = await gf.upload_documents(scheme_id="", project_id=pid, files=[f], db=db)
    return res["saved"][0]["id"]


async def test_persist_retry_then_success(ctx, monkeypatch):
    """落库瞬时失败指数退避重试：第 3 次（1 初 + 2 重试）成功 → ok，检查点清除。"""
    db, pid, _dr, _up = ctx
    # 加速退避，避免测试等待
    monkeypatch.setattr(settings, "parse_retry_base_ms", 1)
    monkeypatch.setattr(settings, "parse_retry_cap_ms", 2)
    monkeypatch.setattr(settings, "parse_retry_max", 2)

    doc_id = await _upload_doc(ctx, b"# Chapter One Engineering Overview\n\nFoundation depth 12.5m.\n")

    calls = {"n": 0}

    async def fake_persist(*a, **k):
        calls["n"] += 1
        return calls["n"] >= 3  # 前两次失败，第三次成功

    monkeypatch.setattr(gf_docs, "_persist_parse_result", fake_persist)

    res = await gf.parse_document(doc_id, force=False, db=db)
    assert res["ok"] is True
    assert calls["n"] == 3, "应重试至第 3 次成功（1 初 + 2 重试）"
    cur = await db.execute(
        "SELECT COUNT(*) n FROM doc_parse_checkpoints WHERE doc_id=?", (doc_id,))
    assert (await cur.fetchone())["n"] == 0, "成功路径须清除检查点"


async def test_no_retry_on_structural_parse_error(ctx, monkeypatch):
    """结构化失败（ParseError→400）不重试：persist 根本不会被调用。"""
    db, pid, _dr, _up = ctx
    from app.services.file_parser import ParseError

    doc_id = await _upload_doc(ctx, b"# Chapter One\n")

    def fake_extract(content, fname):
        raise ParseError("测试结构化失败（无 OCR 引擎）")

    monkeypatch.setattr(gf, "parse_file_content_ex", fake_extract)

    persist_calls = {"n": 0}

    async def fake_persist(*a, **k):
        persist_calls["n"] += 1
        return True

    monkeypatch.setattr(gf_docs, "_persist_parse_result", fake_persist)

    with pytest.raises(Exception) as ei:
        await gf.parse_document(doc_id, force=False, db=db)
    assert ei.value.status_code == 400
    assert persist_calls["n"] == 0, "结构化失败不应进入落库重试"


async def test_checkpoint_resume_skips_extract(ctx, monkeypatch):
    """persist 失败后检查点缓存文本；下次解析从 persist 阶段续跑，跳过提取。"""
    db, pid, _dr, _up = ctx
    doc_id = await _upload_doc(ctx, b"# Chapter One\n\nFoundation depth 12.5m.\n")

    cached = "cached parsed text foundation depth 12.5m resume must skip extract."
    await db.execute(
        "INSERT INTO doc_parse_checkpoints (doc_id, stage, status, error, text_cache, updated_at) "
        "VALUES (?,?,?,?,?,datetime('now','localtime'))",
        (doc_id, "persist", "failed", "落库瞬时失败", cached))
    await db.commit()

    extract_calls = {"n": 0}

    async def fake_extract(content, fname):
        extract_calls["n"] += 1
        raise AssertionError("续跑不应重新提取")

    monkeypatch.setattr(gf, "parse_file_content_ex", fake_extract)

    async def fake_persist(*a, **k):
        return True

    monkeypatch.setattr(gf_docs, "_persist_parse_result", fake_persist)

    res = await gf.parse_document(doc_id, force=False, db=db)
    assert res["ok"] is True
    assert extract_calls["n"] == 0, "断点续跑须跳过提取"
    assert res["text_len"] == len(cached), "须使用缓存文本续跑"
    cur = await db.execute(
        "SELECT COUNT(*) n FROM doc_parse_checkpoints WHERE doc_id=?", (doc_id,))
    assert (await cur.fetchone())["n"] == 0, "续跑成功须清除检查点"


async def test_checkpoint_records_failed_stage(ctx, monkeypatch):
    """persist 持续失败 → 检查点记录 stage=persist/status=failed，并抛 503。"""
    db, pid, _dr, _up = ctx
    monkeypatch.setattr(settings, "parse_retry_base_ms", 1)
    monkeypatch.setattr(settings, "parse_retry_cap_ms", 2)
    monkeypatch.setattr(settings, "parse_retry_max", 2)

    doc_id = await _upload_doc(ctx, b"# Chapter One\n\nFoundation depth 12.5m.\n")

    async def fake_persist(*a, **k):
        return False

    monkeypatch.setattr(gf_docs, "_persist_parse_result", fake_persist)

    with pytest.raises(Exception) as ei:
        await gf.parse_document(doc_id, force=False, db=db)
    assert ei.value.status_code == 503

    cur = await db.execute(
        "SELECT stage, status, error FROM doc_parse_checkpoints WHERE doc_id=?",
        (doc_id,))
    row = dict(await cur.fetchone())
    assert row["stage"] == "persist"
    assert row["status"] == "failed"
    assert "落库" in row["error"]


async def test_force_parse_clears_stale_checkpoint(ctx, monkeypatch):
    """force 重解析须清除旧失败检查点，走完整提取流程（不续跑旧失败态）。"""
    db, pid, _dr, _up = ctx
    doc_id = await _upload_doc(ctx, b"# Chapter One\n\nFoundation depth 12.5m.\n")

    await db.execute(
        "INSERT INTO doc_parse_checkpoints (doc_id, stage, status, error, text_cache, updated_at) "
        "VALUES (?,?,?,?,?,datetime('now','localtime'))",
        (doc_id, "persist", "failed", "落库瞬时失败", "stale cached text"))
    await db.commit()

    extract_calls = {"n": 0}
    real_extract = gf.parse_file_content_ex

    def counting_extract(content, fname):
        extract_calls["n"] += 1
        return real_extract(content, fname)

    monkeypatch.setattr(gf, "parse_file_content_ex", counting_extract)

    res = await gf.parse_document(doc_id, force=True, db=db)
    assert res["ok"] is True
    assert extract_calls["n"] == 1, "force 须走完整提取，不续跑旧失败态"
    cur = await db.execute(
        "SELECT COUNT(*) n FROM doc_parse_checkpoints WHERE doc_id=?", (doc_id,))
    assert (await cur.fetchone())["n"] == 0, "force 须清除旧检查点"
