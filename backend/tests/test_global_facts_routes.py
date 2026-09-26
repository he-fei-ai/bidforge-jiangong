"""全局事实「上传 / 解析 / 资料档案」路由级回归测试。

覆盖此前只有纯函数测试、没有路由层测试的环节（QA 审计缺口 #1、#3）：
- 上传：多文件混合结果、扩展名不支持、0 字节、文件头伪造、同名替换、
  单请求文件数与累计体积配额、scheme/project 归属校验
- 解析：首次解析落库、已解析短路、force 重解析、原文件缺失、
  解析失败【不得把错误信息当正文落库】、解析器截断诊断回传
- 资料档案：列表（含 truncated 标记）、删除（越界路径不删盘）

测试直接调用路由函数（与既有测试风格一致），避免 TestClient 跨事件循环
持有 aiosqlite 连接导致的偶发失败。
"""
import io
import uuid

import pytest
from fastapi import HTTPException, UploadFile

import app.db as _appdb
import app.routers.global_facts as gf
from app.db import get_conn, init_db


def _upload(name: str, data: bytes, size: int | None = None) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name, size=size)


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "facts-routes.sqlite"
    await init_db()
    db = await get_conn()
    # 上传目录隔离到临时目录，避免污染仓库 data/uploads/facts
    uploads = tmp_path / "uploads" / "facts"
    uploads.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)

    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid, uploads


async def _docs(db, pid):
    cur = await db.execute(
        "SELECT id, file_name, file_path, parsed_markdown FROM project_documents "
        "WHERE project_id=?", (pid,))
    return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------

async def test_upload_saves_files_and_rows(ctx):
    db, pid, sid, uploads = ctx
    res = await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("a.txt", "工程名称：某某项目".encode("utf-8")),
               _upload("b.csv", "设备,数量\n塔吊,2\n".encode("utf-8"))],
        db=db)

    assert res["saved_count"] == 2
    rows = await _docs(db, pid)
    assert {r["file_name"] for r in rows} == {"a.txt", "b.csv"}
    # 上传阶段不解析：parsed_markdown 必须为空（分步工作流约定）
    assert all(not r["parsed_markdown"] for r in rows)
    # 文件真实落盘且位于上传根目录内
    for r in rows:
        p = gf._managed_upload_path(r["file_path"])
        assert p is not None and p.exists()


async def test_upload_rejects_unsupported_empty_and_spoofed(ctx):
    db, _pid, sid, _uploads = ctx
    res = await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("x.exe", b"MZ"),
               _upload("empty.txt", b""),
               # 扩展名是 .pdf 但内容不是 PDF → 文件头校验拦截
               _upload("fake.pdf", b"this is plain text")],
        db=db)

    assert res["saved_count"] == 0
    assert res["unsupported"] == ["x.exe"]
    assert res["empty"] == ["empty.txt"]
    assert res["signature_invalid"] == ["fake.pdf"]
    assert res["warnings"]


async def test_upload_rejects_oversize_by_declared_size(ctx):
    db, _pid, sid, _uploads = ctx
    res = await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("big.txt", b"x", size=gf.MAX_UPLOAD_BYTES + 1)],
        db=db)
    assert res["saved_count"] == 0
    assert res["oversize"] == ["big.txt"]


async def test_upload_enforces_file_count_quota(ctx, monkeypatch):
    db, _pid, sid, _uploads = ctx
    monkeypatch.setattr(gf, "MAX_UPLOAD_FILES_PER_REQUEST", 2)
    files = [_upload(f"f{i}.txt", b"data") for i in range(4)]
    res = await gf.upload_documents(scheme_id=sid, project_id="", files=files, db=db)

    assert res["saved_count"] == 2
    assert res["too_many"] == ["f2.txt", "f3.txt"]


async def test_upload_enforces_total_bytes_quota(ctx, monkeypatch):
    db, _pid, sid, _uploads = ctx
    monkeypatch.setattr(gf, "MAX_UPLOAD_TOTAL_BYTES", 10)
    res = await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("a.txt", b"12345678"), _upload("b.txt", b"12345678")],
        db=db)

    assert res["saved_count"] == 1
    assert res["quota_exceeded"] is True
    # ✅ 回归（2026-09-21）：累计体积超限被拒的文件必须归入 quota_files，
    #    不得混进 oversize —— 否则前端按 oversize 提示「1 个超过 30MB」，
    #    而该文件实际只有 8 字节，用户会去拆分文件重试却依然被拒（错误归因）。
    assert res["quota_files"] == ["b.txt"]
    assert "oversize" not in res
    # 超配额的文件必须已从磁盘清理，不能留下孤儿文件
    rows = await _docs(db, _pid)
    assert len(rows) == 1


async def test_upload_same_name_replaces_row_and_old_file(ctx):
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("same.txt", b"first version")], db=db)
    first = (await _docs(db, _pid))[0]

    res = await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("same.txt", b"second version")], db=db)

    assert res["replaced"] == 1
    rows = await _docs(db, _pid)
    assert len(rows) == 1, "同名上传应替换而非累积"
    # 旧文件在提交成功后清理，新文件存在
    assert not gf._managed_upload_path(first["file_path"]).exists()
    assert gf._managed_upload_path(rows[0]["file_path"]).exists()


async def test_upload_rejects_scope_mismatch_and_unknown_scheme(ctx):
    db, pid, _sid, _uploads = ctx
    other_pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (other_pid, "o"))
    await db.commit()

    with pytest.raises(HTTPException) as e1:
        await gf.upload_documents(scheme_id=_sid, project_id=other_pid,
                                  files=[_upload("a.txt", b"x")], db=db)
    assert e1.value.status_code == 400

    with pytest.raises(HTTPException) as e2:
        await gf.upload_documents(scheme_id=uuid.uuid4().hex, project_id="",
                                  files=[_upload("a.txt", b"x")], db=db)
    assert e2.value.status_code == 404
    # 不允许跨项目写入
    assert await _docs(db, other_pid) == []


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

async def test_parse_document_stores_text_and_reports_diagnostics(ctx):
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("doc.txt", "工程名称：某某产业园\n建设工期：120 日历天".encode("utf-8"))],
        db=db)
    doc_id = (await _docs(db, _pid))[0]["id"]

    res = await gf.parse_document(doc_id, db=db)
    assert res["ok"] and res["text_len"] > 0
    assert res["truncated"] is False

    rows = await _docs(db, _pid)
    assert "某某产业园" in rows[0]["parsed_markdown"]

    # 已解析 → 短路返回
    again = await gf.parse_document(doc_id, db=db)
    assert again.get("already_parsed") is True

    # force → 真正重解析
    forced = await gf.parse_document(doc_id, force=True, db=db)
    assert forced["ok"] and "note" in forced


async def test_parse_document_missing_file_returns_400(ctx):
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("gone.txt", b"hello world")], db=db)
    row = (await _docs(db, _pid))[0]
    gf._managed_upload_path(row["file_path"]).unlink()

    with pytest.raises(HTTPException) as e:
        await gf.parse_document(row["id"], db=db)
    assert e.value.status_code == 400


async def test_parse_failure_does_not_store_error_message_as_content(ctx):
    """回归：解析失败必须 400，且绝不能把错误字符串当正文落库。

    旧实现把「解析失败：...」当正文返回，上层落库后 AI 会从这段错误信息里
    提取"事实"，污染全局事实库。
    """
    db, _pid, sid, uploads = ctx
    # 手工写入一个"扩展名是 docx、内容不是 zip"的文件（绕过上传期文件头校验，
    # 模拟磁盘上的历史脏文件 / 上传后被外部替换的场景）
    doc_id = uuid.uuid4().hex
    fpath = uploads / "proj" / f"{doc_id[:8]}_broken.docx"
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_bytes(b"this is definitely not a zip archive")
    await db.execute(
        "INSERT INTO project_documents "
        "(id, project_id, file_name, file_type, doc_type, parsed_markdown, file_path) "
        "VALUES (?,?,?,?,?,?,?)",
        (doc_id, _pid, "broken.docx", "docx", "全局事实上传", "", str(fpath)))
    await db.commit()

    with pytest.raises(HTTPException) as e:
        await gf.parse_document(doc_id, db=db)
    assert e.value.status_code == 400

    rows = await _docs(db, _pid)
    assert rows[0]["parsed_markdown"] in ("", None), "错误信息不得落库为正文"


async def test_parse_all_mixed_success_and_failure(ctx):
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("ok.txt",
                       "工程名称：某某产业园建设项目\n建设工期：120 日历天".encode("utf-8")),
               _upload("lost.txt", b"content")],
        db=db)
    rows = await _docs(db, _pid)
    lost = next(r for r in rows if r["file_name"] == "lost.txt")
    gf._managed_upload_path(lost["file_path"]).unlink()

    res = await gf.parse_all_documents(scheme_id=sid, db=db)
    assert res["parsed"] == 1
    assert res["failed_count"] == 1
    assert res["failed"][0]["file_name"] == "lost.txt"


# ---------------------------------------------------------------------------
# 资料档案
# ---------------------------------------------------------------------------

async def test_list_documents_marks_truncated(ctx):
    db, _pid, sid, _uploads = ctx
    doc_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO project_documents "
        "(id, project_id, file_name, file_type, doc_type, parsed_markdown, file_path) "
        "VALUES (?,?,?,?,?,?,?)",
        (doc_id, _pid, "big.txt", "txt", "全局事实上传",
         "字" * gf.MAX_PARSED_CHARS, ""))
    await db.commit()

    res = await gf.list_documents(scheme_id=sid, db=db)
    assert res["documents"][0]["truncated"] is True


async def test_delete_document_refuses_path_outside_upload_root(ctx, tmp_path):
    """删除接口不得成为任意文件删除器（历史脏路径场景）。"""
    db, _pid, sid, _uploads = ctx
    outside = tmp_path / "outside-secret.txt"
    outside.write_bytes(b"keep me")
    doc_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO project_documents "
        "(id, project_id, file_name, file_type, doc_type, parsed_markdown, file_path) "
        "VALUES (?,?,?,?,?,?,?)",
        (doc_id, _pid, "sneaky.txt", "txt", "全局事实上传", "", str(outside)))
    await db.commit()

    res = await gf.delete_document(doc_id, db=db)
    assert res["ok"] is True
    assert outside.exists(), "上传根目录外的文件绝不能被删除"
    assert await _docs(db, _pid) == []


# ---------------------------------------------------------------------------
# 解析内容预览（/documents/{id}/preview）
#   回归场景（2026-09-18，HTTP 冒烟实测）：get_conn 的 row_factory 是
#   sqlite3.Row，没有 .get() 方法 —— 旧实现对 Row 调 row.get(...) 使文档
#   存在时必然 AttributeError → 500；且该路由此前零测试覆盖。
# ---------------------------------------------------------------------------

async def test_preview_document_unparsed_and_parsed(ctx):
    db, _pid, sid, _uploads = ctx
    up = await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("prev.txt",
                       "工程概况：某大桥加固工程，工期 180 天，施工单位为某建司。".encode("utf-8"))],
        db=db)
    doc_id = up["saved"][0]["id"]

    # 未解析：回 is_parsed=False + 可操作提示，不得抛错
    r0 = await gf.preview_document(doc_id, max_chars=100, db=db)
    assert r0["is_parsed"] is False
    assert r0["text_len"] == 0
    assert isinstance(r0["parse_warnings"], list)

    # 解析后：预览窗口、截断标记、告警列表都须随响应回传
    pres = await gf.parse_document(doc_id, db=db)
    assert pres["ok"] is True
    r = await gf.preview_document(doc_id, max_chars=10, db=db)
    assert r["is_parsed"] is True
    assert r["preview_truncated"] is True
    assert len(r["preview"]) == 10
    assert r["text_len"] == pres["text_len"]
    assert isinstance(r["parse_warnings"], list)
    assert r["file_name"] == "prev.txt"


async def test_preview_document_404(ctx):
    db, _pid, _sid, _uploads = ctx
    with pytest.raises(HTTPException) as e:
        await gf.preview_document("nonexistent-doc", max_chars=100, db=db)
    assert e.value.status_code == 404


async def test_preview_response_contract_symmetric(ctx):
    """契约对称回归（2026-09-21）：未解析 / 已解析两条返回路径字段集合必须一致。

    旧实现未解析分支缺 file_type / doc_category / file_size / parse_time /
    created_at / preview_truncated —— 前端用同一 interface 消费预览响应时，
    未解析文档会拿到 undefined 并渲染出空白占位（如「格式：」）。
    """
    db, _pid, sid, _uploads = ctx
    up = await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("sym.txt", "工程名称：某项目；工期 90 天。".encode("utf-8"))],
        db=db)
    doc_id = up["saved"][0]["id"]

    before = await gf.preview_document(doc_id, max_chars=50, db=db)
    assert before["is_parsed"] is False
    await gf.parse_document(doc_id, db=db)
    after = await gf.preview_document(doc_id, max_chars=50, db=db)
    assert after["is_parsed"] is True

    assert set(before.keys()) == set(after.keys())
    # 未解析分支也必须回填列表里已有的元数据，而不是缺失字段
    assert before["file_type"] == "txt"
    assert before["file_size"] > 0
    assert before["preview_truncated"] is False
    assert before["file_name"] == "sym.txt"
