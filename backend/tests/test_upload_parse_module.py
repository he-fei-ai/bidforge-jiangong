"""上传解析模块增强回归测试（2026-09-23）。

聚焦此前无覆盖或行为已变更的路径：
  - 解析成功即写 parse_status='success'（消除四层入库失败导致状态陈旧）；
  - 逐文档解析并发锁（同文档并发 parse / parse-all 不再重复解析或后写覆盖）；
  - 手动分类保存 PATCH /documents/{id}/category（合法/空/未知/404）；
  - 分类选项接口 GET /documents/category-options。

基础的上传/解析/删除/预览路径已由 test_global_facts_routes.py 覆盖，此处不再重复。
"""
import asyncio
import io
import uuid

import pytest
from app.db import get_conn, init_db
from app.routers import global_facts as gf
from starlette.datastructures import UploadFile


def _upload(name: str, data: bytes, size: int | None = None) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name, size=size)


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    _appdb = __import__("app.db", fromlist=["DB_PATH"])
    _appdb.DB_PATH = tmp_path / "upload-parse.sqlite"
    await init_db()
    uploads = tmp_path / "uploads"
    uploads.mkdir(exist_ok=True)
    monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)

    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid, uploads
    await _appdb.close_db()


async def _docs(db, pid):
    cur = await db.execute(
        "SELECT id, file_name, file_path, parsed_markdown, parse_status "
        "FROM project_documents WHERE project_id=?", (pid,))
    return [dict(r) for r in await cur.fetchall()]


async def _row(db, doc_id):
    cur = await db.execute(
        "SELECT parsed_markdown, parse_status FROM project_documents WHERE id=?",
        (doc_id,))
    return dict(await cur.fetchone())


# ---------------------------------------------------------------------------
# parse_status 写入
# ---------------------------------------------------------------------------

async def test_parse_document_writes_parse_status_success(ctx):
    """解析成功必须把 parse_status 置为 success（此前依赖四层入库、失败则陈旧）。"""
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("doc.txt",
                       "工程名称：某某产业园\n建设工期：120 日历天".encode("utf-8"))],
        db=db)
    doc_id = (await _docs(db, _pid))[0]["id"]
    res = await gf.parse_document(doc_id, db=db)
    assert res["ok"] and res["text_len"] > 0
    row = await _row(db, doc_id)
    assert row["parse_status"] == "success"
    assert row["parsed_markdown"]


async def test_parse_all_writes_parse_status_for_all(ctx):
    """批量解析所有待解析文档，每份都应落 parse_status='success'。"""
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("a.txt", "工程概况：某大桥加固工程。".encode("utf-8")),
               _upload("b.txt", "编制依据：国家现行规范。".encode("utf-8"))],
        db=db)
    res = await gf.parse_all_documents(scheme_id=sid, project_id="", db=db)
    assert res["parsed"] == 2
    assert res["failed_count"] == 0
    for d in await _docs(db, _pid):
        assert d["parse_status"] == "success"


# ---------------------------------------------------------------------------
# 并发守卫
# ---------------------------------------------------------------------------

async def test_concurrent_parse_same_doc_serialized(ctx):
    """同文档并发解析不应抛异常，且最终解析结果一致（锁串行化、不重复写坏）。"""
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("same.txt",
                       "工程名称：某某工程\n施工工期：365 天".encode("utf-8"))],
        db=db)
    doc_id = (await _docs(db, _pid))[0]["id"]

    results = await asyncio.gather(
        gf.parse_document(doc_id, db=db),
        gf.parse_document(doc_id, db=db),
        return_exceptions=True)
    # 两次都成功返回（已解析的非强制调用返回 already_parsed，也算 ok）
    for r in results:
        assert not isinstance(r, Exception), r
    row = await _row(db, doc_id)
    assert row["parse_status"] == "success"
    assert len(row["parsed_markdown"]) > 0


async def test_concurrent_parse_all_and_single_same_doc(ctx):
    """单份解析与批量解析并发同一文档：锁内复核保证只解析一次。"""
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("overlap.txt",
                       "第一章 工程概况\n第二章 施工部署".encode("utf-8"))],
        db=db)
    doc_id = (await _docs(db, _pid))[0]["id"]
    results = await asyncio.gather(
        gf.parse_document(doc_id, db=db),
        gf.parse_all_documents(scheme_id=sid, project_id="", db=db),
        return_exceptions=True)
    for r in results:
        assert not isinstance(r, Exception), r
    row = await _row(db, doc_id)
    assert row["parse_status"] == "success"


# ---------------------------------------------------------------------------
# 分类保存 / 选项
# ---------------------------------------------------------------------------

async def test_update_document_category(ctx):
    """合法分类写入；空值/未知分类 400；不存在文档 404。"""
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("c.txt", "招标公告：公开招标".encode("utf-8"))], db=db)
    doc_id = (await _docs(db, _pid))[0]["id"]

    res = await gf.update_document_category(doc_id, {"doc_category": "招标文件"}, db=db)
    assert res["ok"] and res["doc_category"] == "招标文件"
    cur = await db.execute(
        "SELECT doc_category FROM project_documents WHERE id=?", (doc_id,))
    assert (await cur.fetchone())["doc_category"] == "招标文件"

    with pytest.raises(Exception) as e_empty:
        await gf.update_document_category(doc_id, {"doc_category": "  "}, db=db)
    assert _status(e_empty.value) == 400

    with pytest.raises(Exception) as e_unknown:
        await gf.update_document_category(doc_id, {"doc_category": "未知分类"}, db=db)
    assert _status(e_unknown.value) == 400

    with pytest.raises(Exception) as e_404:
        await gf.update_document_category(uuid.uuid4().hex, {"doc_category": "其他"}, db=db)
    assert _status(e_404.value) == 404


async def test_category_options_endpoint(ctx):
    """分类选项接口返回合法分类 + 兜底「其他」。"""
    db, _pid, sid, _uploads = ctx
    res = await gf.list_category_options()
    assert "其他" in res["options"]
    assert "招标文件" in res["options"]
    # 自动分类关键词表应随接口返回，供前端展示
    assert any(c["category"] == "招标文件" for c in res["auto_keywords"])


def _status(exc) -> int:
    """从 HTTPException / 含 status_code 的异常取状态码。"""
    return getattr(exc, "status_code", getattr(exc, "status", 500))


# ---------------------------------------------------------------------------
# 性能稳定性：解析并发锁泄漏回收
# ---------------------------------------------------------------------------

async def test_parse_lock_released_after_single_parse(ctx):
    """✅ 性能稳定性：解析完成后解析锁字典不应残留该 doc_id（防内存泄漏）。"""
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("lock.txt", "工程概况：某市政道路工程。".encode("utf-8"))],
        db=db)
    doc_id = (await _docs(db, _pid))[0]["id"]
    assert doc_id not in gf._doc_parse_locks
    await gf.parse_document(doc_id, db=db)
    # 解析结束（含 finally 回收）后，锁字典不再持有该条目
    assert doc_id not in gf._doc_parse_locks


async def test_parse_lock_released_after_concurrent_parse(ctx):
    """✅ 并发解析同一文档后，锁字典仍应回收（不残留）。"""
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("clock.txt", "第一章 总则\n第二章 术语".encode("utf-8"))],
        db=db)
    doc_id = (await _docs(db, _pid))[0]["id"]
    await asyncio.gather(
        gf.parse_document(doc_id, db=db),
        gf.parse_document(doc_id, db=db),
        return_exceptions=True)
    assert doc_id not in gf._doc_parse_locks


# ---------------------------------------------------------------------------
# 跨模块可观测性：解析失败状态透传（parse_status='failed' + 原因持久化）
# ---------------------------------------------------------------------------

async def test_parse_document_failure_sets_failed_status(ctx, monkeypatch):
    """✅ 解析抛 ParseError 时，parse_status 应为 'failed' 且原因写入 parse_warnings。

    旧实现失败时状态永远停在 'pending'，前端恒显"待解析"，用户无法得知已失败。
    """
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("bad.txt", b"x")], db=db)
    doc_id = (await _docs(db, _pid))[0]["id"]

    def _boom(content, fname):
        raise gf.ParseError("测试用解析失败：缺少 OCR 依赖")

    monkeypatch.setattr(gf, "parse_file_content_ex", _boom)
    with pytest.raises(Exception):
        await gf.parse_document(doc_id, db=db)
    row = await _row(db, doc_id)
    assert row["parse_status"] == "failed"
    assert row["parsed_markdown"] == ""  # 失败未落库正文
    cur = await db.execute(
        "SELECT parse_warnings FROM project_documents WHERE id=?", (doc_id,))
    raw = (await cur.fetchone())["parse_warnings"] or ""
    assert "解析失败" in raw


async def test_parse_document_empty_text_sets_failed_status(ctx, monkeypatch):
    """✅ 解析出空白文本（<10 字）时同样标记 failed，而非停在 pending。"""
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("blank.txt", b"x")], db=db)
    doc_id = (await _docs(db, _pid))[0]["id"]

    def _empty(content, fname):
        return ("   ", {"warnings": [], "file_type": "txt"})

    monkeypatch.setattr(gf, "parse_file_content_ex", _empty)
    with pytest.raises(Exception):
        await gf.parse_document(doc_id, db=db)
    row = await _row(db, doc_id)
    assert row["parse_status"] == "failed"


async def test_parse_all_failure_sets_failed_status(ctx, monkeypatch):
    """✅ 批量解析中某文档失败时，该文档单独标记 failed（其余仍成功）。"""
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("ok.txt", "建设规模：10 万平方米。".encode("utf-8")),
               _upload("bad2.txt", b"y")],
        db=db)
    doc_ids = [d["id"] for d in await _docs(db, _pid)]

    def _maybe_boom(content, fname):
        if "bad2" in fname:
            raise gf.ParseError("测试用批量解析失败")
        return ("建设规模：本项目总建筑面积约 10 万平方米，地上 20 层。",
                {"warnings": [], "file_type": "txt"})

    monkeypatch.setattr(gf, "parse_file_content_ex", _maybe_boom)
    res = await gf.parse_all_documents(scheme_id=sid, project_id="", db=db)
    assert res["parsed"] == 1
    assert res["failed_count"] == 1
    for d in await _docs(db, _pid):
        if d["file_name"].startswith("bad2"):
            assert d["parse_status"] == "failed"
        else:
            assert d["parse_status"] == "success"


async def test_parse_all_blank_text_marks_failed_status(ctx, monkeypatch):
    """✅ 回归（2026-09-24 B1）：批量解析中「有效文本 <10」分支（空白/扫描件/损坏）
    必须把该文档落 parse_status='failed' 并持久化失败原因，不能停留 pending。

    旧实现此分支只 append failed 后 continue，文档恒显「待解析」，
    「解析全部待解析」每次都重扫永远不会成功的文件。
    """
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("ok.txt", "建设规模：10 万平方米。".encode("utf-8")),
               _upload("blank.txt", b"y")],
        db=db)

    def _maybe_blank(content, fname):
        if "blank" in fname:
            # 模拟扫描件无 OCR / 纯空白文档：解析器返回空白文本
            return ("   \n  ", {"warnings": [], "file_type": "txt"})
        return ("建设规模：本项目总建筑面积约 10 万平方米，地上 20 层。",
                {"warnings": [], "file_type": "txt"})

    monkeypatch.setattr(gf, "parse_file_content_ex", _maybe_blank)
    res = await gf.parse_all_documents(scheme_id=sid, project_id="", db=db)
    assert res["parsed"] == 1
    assert res["failed_count"] == 1
    assert "未解析到有效文本" in res["failed"][0]["reason"]

    for d in await _docs(db, _pid):
        if d["file_name"].startswith("blank"):
            assert d["parse_status"] == "failed"
            assert d["parsed_markdown"] in ("", None)
            cur = await db.execute(
                "SELECT parse_warnings FROM project_documents WHERE id=?", (d["id"],))
            warnings = (await cur.fetchone())["parse_warnings"]
            assert "解析失败" in warnings and "未解析到有效文本" in warnings
        else:
            assert d["parse_status"] == "success"


async def test_parse_all_missing_original_file_marks_failed(ctx):
    """✅ 回归（2026-09-24 B1）：原始文件在磁盘缺失时，批量解析不抛异常、不阻断
    其余文档，且缺失文档落 parse_status='failed'（旧实现只计入 failed 列表）。"""
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("ok.txt", "建设规模：10 万平方米。".encode("utf-8")),
               _upload("gone.txt", "这是一份即将被删除原文件的文档内容。".encode("utf-8"))],
        db=db)
    # 删除 gone.txt 对应的磁盘原文件，模拟上传目录被外部清理
    for d in await _docs(db, _pid):
        if d["file_name"].startswith("gone"):
            gf._managed_upload_path(d["file_path"]).unlink(missing_ok=True)

    res = await gf.parse_all_documents(scheme_id=sid, project_id="", db=db)
    assert res["parsed"] == 1
    assert res["failed_count"] == 1
    assert res["failed"][0]["reason"] == "原始文件缺失"
    for d in await _docs(db, _pid):
        if d["file_name"].startswith("gone"):
            assert d["parse_status"] == "failed"
        else:
            assert d["parse_status"] == "success"


# ---------------------------------------------------------------------------
# 跨模块链路收敛：删除 / 强制重解析 → 导出缓存失效
# ---------------------------------------------------------------------------

async def test_invalidate_project_export_caches_clears_rows(ctx):
    """✅ 按项目失效导出缓存：该项目下所有方案的 export_cache 行被清空。"""
    db, _pid, sid, _uploads = ctx
    scheme_id = f"scheme_for_{_pid}"
    await db.execute(
        "INSERT INTO schemes(id, project_id, name, type) VALUES(?,?,?,?)",
        (scheme_id, _pid, "测试方案", "technical"))
    await db.execute(
        "INSERT INTO export_cache(id, project_id, scheme_id, result_path) "
        "VALUES(?,?,?,?)",
        ("ec1", _pid, scheme_id, "data/_exports/zzz.docx"))
    await db.commit()
    await gf._invalidate_project_export_caches(db, _pid)
    cur = await db.execute(
        "SELECT id FROM export_cache WHERE scheme_id=?", (scheme_id,))
    assert len(await cur.fetchall()) == 0


async def test_delete_document_invalidates_export_cache(ctx):
    """✅ 删除文档后，所属项目的导出缓存被失效（跨模块链路收敛）。"""
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("del.txt",
                       "这是一份用于验证删除后导出缓存失效的待删除文档内容。".encode("utf-8"))],
        db=db)
    doc_id = (await _docs(db, _pid))[0]["id"]
    scheme_id = f"scheme_for_{_pid}"
    await db.execute(
        "INSERT INTO schemes(id, project_id, name, type) VALUES(?,?,?,?)",
        (scheme_id, _pid, "测试方案", "technical"))
    await db.execute(
        "INSERT INTO export_cache(id, project_id, scheme_id, result_path) "
        "VALUES(?,?,?,?)",
        ("ec2", _pid, scheme_id, "data/_exports/yyy.docx"))
    await db.commit()
    # 先解析（走正常路径）→ 再删除，验证删除触发失效
    await gf.parse_document(doc_id, db=db)
    await gf.delete_document(doc_id, db=db)
    cur = await db.execute(
        "SELECT id FROM export_cache WHERE scheme_id=?", (scheme_id,))
    assert len(await cur.fetchall()) == 0
    # 文档记录本身已删除
    assert len(await _docs(db, _pid)) == 0


async def test_force_reparse_invalidates_export_cache(ctx):
    """✅ 强制重解析（force）后，所属项目的导出缓存被失效（跨模块链路收敛）。"""
    db, _pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("re.txt",
                       "这是一份用于验证强制重解析后导出缓存失效的文档内容。".encode("utf-8"))],
        db=db)
    doc_id = (await _docs(db, _pid))[0]["id"]
    scheme_id = f"scheme_for_{_pid}"
    await db.execute(
        "INSERT INTO schemes(id, project_id, name, type) VALUES(?,?,?,?)",
        (scheme_id, _pid, "测试方案", "technical"))
    await db.execute(
        "INSERT INTO export_cache(id, project_id, scheme_id, result_path) "
        "VALUES(?,?,?,?)",
        ("ec3", _pid, scheme_id, "data/_exports/zzz.docx"))
    await db.commit()
    # 首次解析（非 force）不应失效缓存
    await gf.parse_document(doc_id, db=db)
    cur = await db.execute(
        "SELECT id FROM export_cache WHERE scheme_id=?", (scheme_id,))
    assert len(await cur.fetchall()) == 1
    # 强制重解析 → 失效缓存
    await gf.parse_document(doc_id, force=True, db=db)
    cur = await db.execute(
        "SELECT id FROM export_cache WHERE scheme_id=?", (scheme_id,))
    assert len(await cur.fetchall()) == 0



async def test_force_reparse_marks_old_facts_stale(ctx):
    """强制重解析后旧事实立即退出注入链路，响应回传 stale 数量。"""
    db, pid, sid, _uploads = ctx
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("stale.txt", "这是一份用于验证旧事实失效的资料内容。".encode("utf-8"))],
        db=db)
    doc_id = (await _docs(db, pid))[0]["id"]
    await db.execute(
        "INSERT INTO global_facts(id,project_id,scheme_id,group_id,title,content,category,is_resolved) "
        "VALUES('old-fact',?,?,'g-old','旧工期','- **旧工期**: 365天','schedule',1)",
        (pid, sid))
    await db.commit()
    await gf.parse_document(doc_id, db=db)
    res = await gf.parse_document(doc_id, force=True, db=db)
    assert res["stale_facts"] == 1
    row = await (await db.execute("SELECT is_stale FROM global_facts WHERE id='old-fact'")).fetchone()
    assert row["is_stale"] == 1
