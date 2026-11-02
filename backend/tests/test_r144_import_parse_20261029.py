"""R144（2026-10-29）解析提取模块增量护栏。

承接 R136/R137 已收口项，本轮聚焦**增量缺陷**，全部为「引入前必红、引入后必绿」：

A 组 —— F1 · 同名替换上传后事实/导出缓存未失效（与 delete_document / R79 A-4 同族漏改）
B 组 —— F2 · _persist_parse_result 只判 None 不校验影响行数（并发删除假成功 + 孤儿重建）
C 组 —— F4 · purge_document 漏清 doc_parse_checkpoints（删除/替换遗留孤儿检查点行）
D 组 —— F5 · 同名替换旧档案 DELETE 无 rowcount 守卫（并发同名上传重复建档）
E 组 —— F7 · parse_outline 短而非空文本产出静默空目录（无可操作性告警）
F 组 —— F8 · auto_classify_document docstring 与实现不符（未剥离扩展名）

⚠️ 本文件刻意不新建独立库：复用 conftest `db_conn`（内存库 + SCHEMA_SQL + _migrate）
   或与 test_global_facts_routes 同款 tmp_path 文件库（上传需真实落盘）。
"""
import io
import uuid

import app.db as _appdb
import app.routers.global_facts as gf
import app.routers.global_facts_docs as gf_docs
import app.routers.upload_outline as upload_outline
import pytest
from app.db import get_conn, init_db
from app.services.doc_categories import auto_classify_document
from app.services.doc_pipeline import pipeline as doc_pipeline
from fastapi import HTTPException, UploadFile


def _upload(name: str, data: bytes, size: int | None = None) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name, size=size)


_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    """与 test_global_facts_routes 同款：文件库 + 隔离上传目录 + 项目/方案。"""
    _appdb.DB_PATH = tmp_path / "r144-import-parse.sqlite"
    await init_db()
    db = await get_conn()
    uploads = tmp_path / "uploads" / "r144"
    uploads.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid, uploads
    await _appdb.close_db()


async def _doc_rows(db, pid):
    cur = await db.execute(
        "SELECT id, file_name, file_path, parsed_markdown, parse_status "
        "FROM project_documents WHERE project_id=?", (pid,))
    return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# A 组 · F1：同名替换上传 → 事实 stale + 导出缓存失效
# ---------------------------------------------------------------------------

async def test_replacement_marks_facts_stale_and_invalidates(ctx, monkeypatch):
    """A1（行为级）：同名替换上传后，项目事实必须被标记 stale。

    引入前 upload_documents 的替换路径只 purge 四层产物、从不调用
    _mark_project_facts_stale / _invalidate_project_export_caches ——
    被替换文档内容退出资料全集，但生成链路继续用基于旧内容的 is_stale=0 事实。
    """
    db, pid, sid, uploads = ctx
    # 先上传一份（首次，replaced=0，不应触发 stale）
    res1 = await gf_docs.upload_documents(
        scheme_id=sid, project_id="", files=[_upload("招标文件.pdf", _PDF)], db=db)
    assert res1["saved_count"] == 1 and res1["replaced"] == 0
    assert "stale_facts" not in res1

    # 插入一条项目事实（is_stale=0）
    await db.execute(
        "INSERT INTO global_facts (id, project_id, title, content, is_stale) "
        "VALUES (?,?,?,?,0)", (uuid.uuid4().hex, pid, "t", "c"))
    await db.commit()

    # 同名替换上传
    res2 = await gf_docs.upload_documents(
        scheme_id=sid, project_id="", files=[_upload("招标文件.pdf", _PDF)], db=db)
    assert res2["saved_count"] == 1 and res2["replaced"] == 1
    # 响应须带回 stale_facts 键（与 delete_document / parse_document 同口径）
    assert "stale_facts" in res2
    cur = await db.execute(
        "SELECT is_stale FROM global_facts WHERE project_id=?", (pid,))
    assert dict(await cur.fetchone())["is_stale"] == 1


async def test_replacement_stale_wiring_called(ctx, monkeypatch):
    """A2（接线级）：替换路径必须真实调用 _mark_project_facts_stale 与
    _invalidate_project_export_caches（R137 教训②：不能只断 helper 不断调用点）。"""
    db, pid, sid, uploads = ctx
    calls: list[tuple] = []
    async def _fake_stale(db_, project_id):
        calls.append(("stale", project_id))
        return 3
    async def _fake_invalidate(db_, project_id):
        calls.append(("invalidate", project_id))
    monkeypatch.setattr(gf_docs, "_mark_project_facts_stale", _fake_stale)
    monkeypatch.setattr(gf_docs, "_invalidate_project_export_caches", _fake_invalidate)

    # 首次上传（无替换）→ 不触发
    await gf_docs.upload_documents(
        scheme_id=sid, project_id="", files=[_upload("a.pdf", _PDF)], db=db)
    assert calls == []
    # 同名替换 → 触发 stale + invalidate
    res = await gf_docs.upload_documents(
        scheme_id=sid, project_id="", files=[_upload("a.pdf", _PDF)], db=db)
    assert res["replaced"] == 1
    assert ("stale", pid) in calls and ("invalidate", pid) in calls


# ---------------------------------------------------------------------------
# B 组 · F2：_persist_parse_result 校验影响行数
# ---------------------------------------------------------------------------

async def test_persist_parse_result_zero_rows_returns_false(ctx, monkeypatch):
    """B1（行为级）：文档被并发删除后，落库 UPDATE 命中 0 行必须返回 False。

    引入前只判 `cur is None`（R13），0 行也返回 True → 调用方继续
    _ingest_parsed_doc（读到空正文、重建已删 parsed 层）+ 为已删文档写检查点，
    响应回 ok=True（幽灵成功）。"""
    db, pid, sid, uploads = ctx
    await gf_docs.upload_documents(
        scheme_id=sid, project_id="", files=[_upload("b.pdf", _PDF)], db=db)
    rows = await _doc_rows(db, pid)
    assert len(rows) == 1
    doc_id = rows[0]["id"]

    # 文档存在 → 正常落库返回 True
    ok1 = await gf_docs._persist_parse_result(
        db, doc_id, "正文", 0.1, [], {"file_type": "pdf"}, False)
    assert ok1 is True

    # 模拟并发删除：删掉文档行后，UPDATE 命中 0 行
    # ✅ R145（L4 契约精化）：R144 首版此处断言 return False —— 现已升级为抛
    #    ParseTargetGoneError（区分「已删」与「DB 瞬时」，见 R145 护栏 A 组）。
    await db.execute("DELETE FROM project_documents WHERE id=?", (doc_id,))
    await db.commit()
    with pytest.raises(gf_docs.ParseTargetGoneError):
        await gf_docs._persist_parse_result(
            db, doc_id, "正文2", 0.1, [], {"file_type": "pdf"}, False)


def test_persist_parse_result_has_rowcount_guard():
    """B2（源码锚）：_persist_parse_result 体内必须调用 safe_rowcount（R72 口径）。"""
    src = __import__("inspect").getsource(gf_docs._persist_parse_result)
    assert "safe_rowcount" in src


# ---------------------------------------------------------------------------
# C 组 · F4：purge_document 清理 doc_parse_checkpoints
# ---------------------------------------------------------------------------

async def test_purge_clears_parse_checkpoint(ctx):
    """C1（行为级）：purge_document 必须删除 doc_parse_checkpoints 行。"""
    db, pid, sid, uploads = ctx
    await gf_docs.upload_documents(
        scheme_id=sid, project_id="", files=[_upload("c.pdf", _PDF)], db=db)
    doc_id = (await _doc_rows(db, pid))[0]["id"]
    await db.execute(
        "INSERT INTO doc_parse_checkpoints (doc_id, stage, status) VALUES (?,?,?)",
        (doc_id, "extract", "done"))
    await db.commit()
    await doc_pipeline.purge_document(db, doc_id=doc_id, project_id=pid)
    cur = await db.execute(
        "SELECT 1 FROM doc_parse_checkpoints WHERE doc_id=?", (doc_id,))
    assert await cur.fetchone() is None


def test_purge_delete_sqls_include_checkpoints():
    """C2（源码锚）：purge_document 的 delete_sqls 必须含 doc_parse_checkpoints。"""
    src = __import__("inspect").getsource(doc_pipeline.purge_document)
    assert "DELETE FROM doc_parse_checkpoints WHERE doc_id=?" in src


# ---------------------------------------------------------------------------
# D 组 · F5：同名替换旧档案 DELETE 行数守卫
# ---------------------------------------------------------------------------

async def test_replacement_delete_has_rowcount_guard(ctx):
    """D1（行为级 + 源码锚）：正常同名替换仍成功（行数=1，不误伤）；
    源码须含 safe_rowcount 守卫（R72 口径）。"""
    db, pid, sid, uploads = ctx
    await gf_docs.upload_documents(
        scheme_id=sid, project_id="", files=[_upload("d.pdf", _PDF)], db=db)
    res = await gf_docs.upload_documents(
        scheme_id=sid, project_id="", files=[_upload("d.pdf", _PDF)], db=db)
    assert res["replaced"] == 1
    rows = await _doc_rows(db, pid)
    # 同名替换后必须恰一行（无重复建档）
    assert len(rows) == 1
    # ✅ R144：源码锚必须断**调用形态**（R116 教训③）—— 只断 "safe_rowcount" in src
    #    会被本函数的注释文字满足，摘掉真实调用仍 PASS（A/B M6 即因此首轮 MISSED）。
    #    必须断 del_cur 这个真实调用的形态。
    src = __import__("inspect").getsource(gf_docs.upload_documents)
    assert "safe_rowcount(del_cur," in src
    assert "同名替换删除旧档案" in src


# ---------------------------------------------------------------------------
# E 组 · F7：parse_outline 短文本告警
# ---------------------------------------------------------------------------

async def test_parse_outline_short_text_warns(db_conn):
    """E1（行为级）：短而非空的文本（无编号结构）不得静默返回空目录。"""
    db = db_conn
    res = await upload_outline.parse_outline(
        file=_upload("short.txt", "hello".encode("utf-8")),
        scheme_name="", reorganize=False, project_id="", db=db)
    assert res["outline"] == []
    assert "warning" in res  # 引入前该分支缺失 → 无任何提示
    assert "未识别出章节结构" in res["warning"]
    assert "empty_text" not in res  # 非空文本不是 empty_text


async def test_parse_outline_blank_text_still_empty(db_conn):
    """E2（回归锚）：全空文本仍走 empty_text 通道（既有行为不得被 E1 破坏）。"""
    db = db_conn
    res = await upload_outline.parse_outline(
        file=_upload("blank.txt", b"\n  \n"),
        scheme_name="", reorganize=False, project_id="", db=db)
    assert res.get("empty_text") is True


# ---------------------------------------------------------------------------
# F 组 · F8：分类按文件名（不含扩展名）匹配
# ---------------------------------------------------------------------------

def test_classify_strips_extension():
    """F1（行为级）：扩展名不得参与分类匹配（docstring 契约）。

    引入前对完整文件名匹配 → "foo.预算" 命中的是扩展名里的关键词 → 误判
    报价清单；剥离扩展名后 "foo" 无关键词 → 其他。"""
    assert auto_classify_document("foo.预算") == "其他"
    # 正常文件名（关键词在主干）不受影响
    assert auto_classify_document("招标文件汇总.pdf") == "招标文件"
    assert auto_classify_document("财务资料汇总.xlsx") == "财务资料"


def test_classify_rule_order_still_first_hit():
    """F2（回归锚）：规则顺序取首个命中的既有行为不变。"""
    assert auto_classify_document("施工组织设计") == "招标文件"
    assert auto_classify_document("某某施工合同.pdf") == "合同文件"


# ---------------------------------------------------------------------------
# G 组 · R145-L4：ParseTargetGoneError 与「DB 瞬时」分流
# ---------------------------------------------------------------------------

_TXT = "工程名称：某某产业园"


async def test_parse_document_gone_returns_404_and_clears_checkpoint(ctx, monkeypatch):
    """G1（行为级）：并发删除后 parse_document 必须 404、清检查点、不写 failed 检查点。

    R144 首版此场景回 503（引导用户重试一个已不存在的文档）且为已删文档写一行
    failed 检查点（孤儿）。R145 抛 ParseTargetGoneError 分流。"""
    db, pid, sid, _up = ctx
    await gf_docs.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("g1.txt", _TXT.encode("utf-8"))], db=db)
    doc_id = (await _doc_rows(db, pid))[0]["id"]
    # 模拟 parse 前段已写的 extract 检查点（真实链路 extract→persist 顺序）
    await db.execute(
        "INSERT INTO doc_parse_checkpoints (doc_id, stage, status) VALUES (?,?,?)",
        (doc_id, "extract", "done"))
    await db.commit()

    async def _gone(*a, **k):
        raise gf_docs.ParseTargetGoneError(doc_id)
    monkeypatch.setattr(gf_docs, "_persist_parse_result", _gone)

    with pytest.raises(HTTPException) as e:
        await gf_docs.parse_document(doc_id, db=db)
    assert e.value.status_code == 404, "文档已删必须 404，不得 503 引导重试"
    cur = await db.execute(
        "SELECT 1 FROM doc_parse_checkpoints WHERE doc_id=?", (doc_id,))
    assert await cur.fetchone() is None, "已删文档不得残留检查点（含 extract 段）"


async def test_parse_all_gone_counts_failed_without_status_write(ctx, monkeypatch):
    """G2（行为级）：批量解析遇并发删除 → 计入 failed 并注明原因，批次继续，
    不得为已删文档写 parse_status='failed'（0 行无效写）。"""
    db, pid, sid, _up = ctx
    await gf_docs.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("g2.txt", _TXT.encode("utf-8"))], db=db)
    doc_id = (await _doc_rows(db, pid))[0]["id"]

    async def _gone(*a, **k):
        raise gf_docs.ParseTargetGoneError(doc_id)
    monkeypatch.setattr(gf_docs, "_persist_parse_result", _gone)

    res = await gf_docs.parse_all_documents(scheme_id=sid, project_id="", db=db)
    assert res["parsed"] == 0
    assert res["failed_count"] == 1
    assert "已被删除" in res["failed"][0]["reason"]
    rows = await _doc_rows(db, pid)
    assert rows[0]["parse_status"] != "failed", "已删文档不得写 failed 状态（行已不在）"


async def test_retry_wrapper_does_not_retry_target_gone(ctx, monkeypatch):
    """G3（行为级）：ParseTargetGoneError 是结构性失败，重试包装器不得重试。"""
    db, pid, sid, _up = ctx
    calls: list[int] = []

    async def _gone(*a, **k):
        calls.append(1)
        raise gf_docs.ParseTargetGoneError("doc-x")
    monkeypatch.setattr(gf_docs, "_persist_parse_result", _gone)

    with pytest.raises(gf_docs.ParseTargetGoneError):
        await gf_docs._persist_parse_result_with_retry(
            db, "doc-x", "t", 0.1, [], {}, False)
    assert len(calls) == 1, "文档已删重试无意义（对比：DB 瞬时会退避重试 2 次）"


# ---------------------------------------------------------------------------
# H 组 · R145-L2：_safe_filename 超长截断
# ---------------------------------------------------------------------------

def test_safe_filename_truncates_long_name_keeps_ext():
    """H1（行为级）：>120 字符文件名截断且保留扩展名（旧实现不截断 → 落盘
    OSError → 整批上传 500，用户无从得知原因）。"""
    long_name = "深" * 300 + ".docx"
    out = gf_docs._safe_filename(long_name)
    assert len(out) <= gf_docs._SAFE_FILENAME_MAX
    assert out.endswith(".docx")
    assert out != long_name


def test_safe_filename_normal_names_unchanged():
    """H2（回归锚）：正常文件名逐字节不变（截断只影响极端输入）。"""
    for n in ("招标文件.pdf", "a.txt", "施工组织设计-2026版.docx",
              "数据统计表_final.xlsx", "uploaded_file"):
        assert gf_docs._safe_filename(n) == n


def test_safe_filename_no_ext_and_long_ext():
    """H3（边界）：无扩展名超长名截断到上限；超长扩展名（>16 字符）按原始
    前缀截断（垃圾输入以干净失败收场，不再 OSError 500）。"""
    out1 = gf_docs._safe_filename("长" * 300)
    assert len(out1) == gf_docs._SAFE_FILENAME_MAX
    out2 = gf_docs._safe_filename("a" * 200 + "." + "x" * 20)
    assert len(out2) <= gf_docs._SAFE_FILENAME_MAX
    out3 = gf_docs._safe_filename("")
    assert out3 == "uploaded_file"


# ---------------------------------------------------------------------------
# K 组 · R157：解析链路结构化事件（TD-7 便宜收口）
# ---------------------------------------------------------------------------

async def test_mark_parse_failed_emits_structured_event(ctx):
    """K1（行为级）：_mark_parse_failed 必须向 system_logs 发 parse.failed 事件
    （module='parse'，WARNING，detail 含 doc_id）—— 解析失败率从此可按事件盘点，
    而非只能翻文本日志。"""
    db, pid, sid, _up = ctx
    await gf_docs.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("k1.txt", _TXT.encode("utf-8"))], db=db)
    doc_id = (await _doc_rows(db, pid))[0]["id"]
    await gf_docs._mark_parse_failed(db, doc_id, "K1 测试失败原因")
    cur = await db.execute(
        "SELECT level, module, event, status, message, detail FROM system_logs "
        "WHERE module='parse' AND event='parse.failed' ORDER BY id DESC LIMIT 1")
    row = await cur.fetchone()
    assert row is not None, "parse.failed 事件未落 system_logs"
    r = dict(row)
    assert r["level"] == "WARNING" and r["status"] == "failed"
    assert "K1 测试失败原因" in (r["message"] or "")
    assert doc_id in (r["detail"] or "")


def test_parse_events_wiring_complete():
    """K2（接线锚）：四类事件必须全部接线 —— failed（_mark_parse_failed 咽喉）、
    done/aborted（parse_document）、batch（parse_all）；辅助函数必须用 get_conn()
    独立连接（log_event 内部 commit，传请求级 db 会固化半截事务）。"""
    src = __import__("inspect").getsource(gf_docs)
    assert src.count('"parse.failed"') == 1
    assert '"parse.done"' in src and '"parse.aborted"' in src
    assert '"parse.batch"' in src
    helper_src = __import__("inspect").getsource(gf_docs._log_parse_event)
    assert "get_conn()" in helper_src and 'module="parse"' in helper_src
