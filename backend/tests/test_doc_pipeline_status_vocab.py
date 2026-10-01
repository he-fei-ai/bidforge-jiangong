"""解析状态取值契约回归测试（锁定 schema 注释漂移）。

问题背景（2026-09-26 审计）：
- `schema_sql.py` 的 `parse_status` 列注释曾写 `pending | success | error`，
  但 `routers/global_facts._mark_parse_failed` 实际写入的是 `'failed'`，
  前端 `utils/workflowDerived.isDocFailed` 也以 `'failed'` 为判定值 ——
  注释与运行时/前端三方口径长期不一致，曾有审计据此误判"失败文档漏判"。
- `status` 列注释写 `valid | file_changed | expired`，但
  `services/doc_pipeline.pipeline.compute_freshness` 在未解析时回写 `'not_parsed'`，
  该取值未被注释覆盖。

本文件把这两个**实际生效的取值**钉死，任何把 `failed` 误改回 `error`、
或把 `not_parsed` 取值挪作他用的改动都会在此失败，防止注释漂移再演变为真实 BUG。
"""
import uuid

import pytest_asyncio

import app.routers.global_facts as gf
import app.services.doc_pipeline.pipeline as pipeline


@pytest_asyncio.fixture
async def ctx(db_conn, tmp_path, monkeypatch):
    """复用 test_doc_pipeline 的 ctx 语义：重定向存储根 + 建项目行。"""
    uploads = tmp_path / "uploads"
    docs_root = tmp_path / "projects"
    monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)
    monkeypatch.setattr(
        __import__(
            "app.services.doc_pipeline.doc_storage", fromlist=["DOCS_ROOT"]
        ),
        "DOCS_ROOT",
        docs_root,
    )
    pid = uuid.uuid4().hex
    await db_conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db_conn.commit()
    return db_conn, pid, docs_root, uploads


async def test_mark_parse_failed_writes_failed_vocabulary(ctx):
    """解析失败必须把 parse_status 置为 'failed'（前端 isDocFailed 的唯一判定值）。"""
    db, pid, _docs_root, _uploads = ctx
    doc_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO project_documents(id, project_id, file_name, parse_status)"
        " VALUES(?,?,?, 'pending')",
        (doc_id, pid, "a.docx"),
    )
    await db.commit()

    await gf._mark_parse_failed(db, doc_id, "文件无法识别或已损坏")
    await db.commit()

    cur = await db.execute(
        "SELECT parse_status, parse_warnings FROM project_documents WHERE id=?",
        (doc_id,),
    )
    row = await cur.fetchone()
    assert row["parse_status"] == "failed", "失败状态必须是 'failed'，不能是注释里的 'error'"
    # 失败原因随列表回传，前端据此展示"⚠ 解析失败"标签
    assert "文件无法识别或已损坏" in (row["parse_warnings"] or "")


async def test_compute_freshness_not_parsed_vocabulary():
    """未成功解析时 freshness.status 必须是 'not_parsed'（status 列真实取值之一）。"""
    # 任意未成功解析的 meta（pending / failed / 空）
    for parse_status in ("pending", "failed", ""):
        fresh = pipeline.compute_freshness({"parse_status": parse_status})
        assert fresh["status"] == "not_parsed"
        assert "文档尚未成功解析" in fresh["reasons"]

    # 已成功解析且指纹一致 → 回到 valid（验证 not_parsed 不是无条件返回）
    fresh_ok = pipeline.compute_freshness(
        {"parse_status": "success", "file_hash_md5": "abc"}
    )
    assert fresh_ok["status"] == "valid"
