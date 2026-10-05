"""解析提取模块 R13 写/读路径收口 + 上传配额点名（R41 · 2026-10-05）。

本轮覆盖五类此前没有护栏的缺陷（全部属 AGENTS §5.5「R13：execute() 可能返回
None」与「返回值被丢弃导致假成功」两类）：

  A 写路径假成功   `_persist_parse_result` 返回值被两条调用链丢弃
                   → 「解析成功 N 字 / 成功 N 份」而库里正文是空的
  B 四层分块写路径 `ingest_parse_result` 的 DELETE / INSERT / UPDATE 裸取返回值
  C 读路径 500 面  `list_documents` / `parse_document` / `parse_all_documents`
                   的游标未判空 → AttributeError → 500（前端只看到裸错误）
  D 上传写路径     INSERT / 同名替换 DELETE 未生效仍报 `saved_count`
  E 配额静默丢名   `break` 之后的剩余文件在响应里不出现于任何字段
  F 视觉 OCR 缓存  `vision_available` 把探测异常写成 False 并缓存 300s
                   → 一次抖动就让扫描件连续 5 分钟静默跳过 OCR

方法论：R13 分支无法在真实连接上自然触发（aiosqlite 的 None 是偶发态），故统一
用「代理连接」按 SQL 片段把 `db.execute` 重定向到 None —— 与本仓既有护栏
`test_cross_module_chain_20261004.py::_NoneOnceDb` 同款手法。
"""
import ast
import io
import uuid
from pathlib import Path

import app.db as _appdb
import app.routers.global_facts as gf
import app.services.doc_pipeline.pipeline as pipe
import pytest
from app.db import get_conn, init_db
from fastapi import HTTPException, UploadFile


# ---------------------------------------------------------------------------
# 代理连接：按谓词把匹配的 execute / executemany 重定向到 None
# ---------------------------------------------------------------------------

class _NullDb:
    def __init__(self, conn, predicate):
        self._c = conn
        self._p = predicate
        self.hits = 0

    async def execute(self, sql, params=()):
        if self._p(sql):
            self.hits += 1
            return None
        return await self._c.execute(sql, params)

    async def executemany(self, sql, seq):
        if self._p(sql):
            self.hits += 1
            return None
        return await self._c.executemany(sql, seq)

    async def commit(self):
        await self._c.commit()

    async def rollback(self):
        await self._c.rollback()


def _upload(name: str, data: bytes, size: int | None = None) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name, size=size)


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "import-r13.sqlite"
    await init_db()
    db = await get_conn()
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
        "SELECT id, file_name, file_path, parsed_markdown, parse_status "
        "FROM project_documents WHERE project_id=?", (pid,))
    return [dict(r) for r in await cur.fetchall()]


async def _chunks(db, doc_id):
    cur = await db.execute(
        "SELECT chunk_id FROM doc_chunks WHERE doc_id=?", (doc_id,))
    return [r["chunk_id"] for r in await cur.fetchall()]


# ===========================================================================
# A · 写路径假成功：_persist_parse_result 返回值不得被丢弃
# ===========================================================================
class TestPersistParseResultContract:
    async def test_returns_false_when_execute_returns_none(self, ctx, monkeypatch):
        db, pid, _sid, _up = ctx
        doc_id = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO project_documents(id,project_id,file_name,file_type,"
            "doc_type,parsed_markdown) VALUES (?,?,?,?,?,?)",
            (doc_id, pid, "a.txt", "txt", "全局事实上传", ""))
        await db.commit()

        proxy = _NullDb(db, lambda s: "UPDATE project_documents" in s)
        ok = await gf._persist_parse_result(
            proxy, doc_id, "正文内容", 0.1, [], {}, False)
        assert ok is False, "R13 时必须显式返回 False，否则调用方会当成成功"

        rows = await _docs(db, pid)
        assert rows[0]["parsed_markdown"] == "", "未写入的正文不得被记为已写"
        assert proxy.hits >= 1, "变异守卫：本用例必须真的命中 None 分支"

    async def test_returns_true_on_success(self, ctx):
        db, pid, _sid, _up = ctx
        doc_id = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO project_documents(id,project_id,file_name,file_type,"
            "doc_type,parsed_markdown) VALUES (?,?,?,?,?,?)",
            (doc_id, pid, "a.txt", "txt", "全局事实上传", ""))
        await db.commit()

        ok = await gf._persist_parse_result(
            db, doc_id, "工程名称：某某项目", 0.2, ["x"], {"file_type": "txt"}, False)
        assert ok is True
        rows = await _docs(db, pid)
        assert rows[0]["parsed_markdown"] == "工程名称：某某项目"

    async def test_parse_document_raises_503_when_persist_fails(
            self, ctx, monkeypatch):
        """落库未生效时**不得**回 ok=True（旧实现在此处给出假成功）。"""
        db, pid, sid, _up = ctx
        await gf.upload_documents(
            scheme_id=sid, project_id="",
            files=[_upload("doc.txt", "工程名称：某某产业园".encode("utf-8"))], db=db)
        doc_id = (await _docs(db, pid))[0]["id"]

        async def _fail(*a, **k):
            return False
        monkeypatch.setattr(gf, "_persist_parse_result", _fail)

        with pytest.raises(HTTPException) as e:
            await gf.parse_document(doc_id, db=db)
        assert e.value.status_code == 503

        rows = await _docs(db, pid)
        assert rows[0]["parsed_markdown"] == "", "假成功会让用户以为解析已保存"

    async def test_parse_all_counts_persist_failure_as_failed(self, ctx, monkeypatch):
        db, pid, sid, _up = ctx
        await gf.upload_documents(
            scheme_id=sid, project_id="",
            files=[_upload("doc.txt", "工程名称：某某产业园".encode("utf-8"))], db=db)

        async def _fail(*a, **k):
            return False
        monkeypatch.setattr(gf, "_persist_parse_result", _fail)

        res = await gf.parse_all_documents(scheme_id=sid, project_id="", db=db)
        assert res["parsed"] == 0, "落库失败的文档不得计入 parsed（假成功）"
        assert res["failed_count"] == 1
        assert "保存失败" in res["failed"][0]["reason"]

        rows = await _docs(db, pid)
        assert rows[0]["parse_status"] == "failed"
        assert rows[0]["parsed_markdown"] == ""


# ===========================================================================
# B · 四层分块写路径
# ===========================================================================
class TestIngestParseResultWriteGuards:
    @pytest.fixture
    async def ing_ctx(self, ctx, monkeypatch):
        db, pid, sid, up = ctx
        # 落盘层与本断言无关，且避免污染仓库 data/projects
        async def _noop(fn, *a, **k):
            return None
        monkeypatch.setattr(pipe, "_safe_io", _noop)
        doc_id = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO project_documents(id,project_id,file_name,file_type,"
            "doc_type,parsed_markdown) VALUES (?,?,?,?,?,?)",
            (doc_id, pid, "a.txt", "txt", "全局事实上传", "# 标题\n正文内容"))
        await db.commit()
        # 预置一个旧分块：DELETE 未生效时它必须原样保留（证明 DELETE 没执行）
        await db.execute(
            "INSERT INTO doc_chunks(chunk_id, doc_id, chunk_type, text, hash)"
            " VALUES (?,?,?,?,?)",
            (f"{doc_id}:1", doc_id, "section", "旧块", "h-old"))
        await db.commit()
        yield db, pid, doc_id

    async def test_normal_path_is_not_degraded(self, ing_ctx):
        db, pid, doc_id = ing_ctx
        res = await pipe.ingest_parse_result(
            db=db, doc_id=doc_id, project_id=pid, file_name="a.txt",
            markdown="# 标题\n正文内容", page_count=1, parse_duration_s=0.1,
            parse_engine="test")
        assert res["db_write_degraded"] is False
        assert await _chunks(db, doc_id), "正常路径必须写入分块"

    async def test_delete_none_skips_rebuild_without_integrity_error(self, ing_ctx):
        """DELETE 返回 None 时旧块未删，直接 INSERT 必撞确定性 chunk_id 主键。

        修复前：IntegrityError 被 `_ingest_parsed_doc` 的 except 整段吞掉 →
        doc_chunks 全丢、响应 layers 消失、日志只有「解析成功」。
        """
        db, pid, doc_id = ing_ctx
        proxy = _NullDb(db, lambda s: s.lstrip().upper().startswith(
            "DELETE FROM DOC_CHUNKS"))
        res = await pipe.ingest_parse_result(
            db=proxy, doc_id=doc_id, project_id=pid, file_name="a.txt",
            markdown="# 标题\n正文内容", page_count=1, parse_duration_s=0.1,
            parse_engine="test")
        assert proxy.hits >= 1, "变异守卫：必须真的命中 None 分支"
        assert res["db_write_degraded"] is True
        # 旧块保留（没有冲突崩掉、也没有被清空）
        assert f"{doc_id}:1" in await _chunks(db, doc_id)

    async def test_meta_update_none_still_commits_chunks(self, ing_ctx):
        """元数据 UPDATE 未生效时**不得**照搬 freshness 的跳过 commit ——
        上方的分块写入还在本事务里排队，跳过会把它们一并悬空。
        """
        db, pid, doc_id = ing_ctx
        proxy = _NullDb(db, lambda s: "UPDATE project_documents SET "
                        "parse_status='success'" in s)
        res = await pipe.ingest_parse_result(
            db=proxy, doc_id=doc_id, project_id=pid, file_name="a.txt",
            markdown="# 标题\n正文内容", page_count=1, parse_duration_s=0.1,
            parse_engine="test")
        assert proxy.hits >= 1, "变异守卫：必须真的命中 None 分支"
        assert res["db_write_degraded"] is True
        # 关键差异：分块仍已提交（不是整体悬空）
        chunks = await _chunks(db, doc_id)
        assert chunks, "分块必须随本次 commit 落库"

    async def test_chunk_insert_none_is_reported(self, ing_ctx):
        db, pid, doc_id = ing_ctx
        proxy = _NullDb(db, lambda s: s.lstrip().upper().startswith(
            "INSERT INTO DOC_CHUNKS"))
        res = await pipe.ingest_parse_result(
            db=proxy, doc_id=doc_id, project_id=pid, file_name="a.txt",
            markdown="# 标题\n正文内容", page_count=1, parse_duration_s=0.1,
            parse_engine="test")
        assert proxy.hits >= 1, "变异守卫：必须真的命中 None 分支"
        assert res["db_write_degraded"] is True, "静默丢分块必须被标记"
        assert await _chunks(db, doc_id) == [], "INSERT 未生效即无新块"


# ===========================================================================
# C · 读路径不得 500（cursor 为 None 时 503 = 可重试）
# ===========================================================================
class TestReadCursorGuards:
    async def test_list_documents_none_cursor_returns_503(self, ctx):
        db, pid, _sid, _up = ctx
        proxy = _NullDb(db, lambda s: "ORDER BY created_at DESC" in s)
        with pytest.raises(HTTPException) as e:
            await gf.list_documents(project_id=pid, scheme_id="", db=proxy)
        assert e.value.status_code == 503
        assert proxy.hits >= 1, "变异守卫：必须真的命中 None 分支"

    async def test_parse_document_none_cursor_returns_503(self, ctx):
        db, pid, sid, _up = ctx
        await gf.upload_documents(
            scheme_id=sid, project_id="",
            files=[_upload("doc.txt", "工程名称：某某产业园".encode("utf-8"))], db=db)
        doc_id = (await _docs(db, pid))[0]["id"]
        proxy = _NullDb(db, lambda s: s.startswith(
            "SELECT parsed_markdown FROM project_documents"))
        with pytest.raises(HTTPException) as e:
            await gf.parse_document(doc_id, db=proxy)
        assert e.value.status_code == 503
        assert proxy.hits >= 1, "变异守卫：必须真的命中 None 分支"

    async def test_parse_all_none_cursor_returns_503(self, ctx):
        db, _pid, sid, _up = ctx
        proxy = _NullDb(db, lambda s: s.startswith(
            "SELECT id, project_id, file_name, file_path FROM project_documents"))
        with pytest.raises(HTTPException) as e:
            await gf.parse_all_documents(
                scheme_id=sid, project_id="", db=proxy)
        assert e.value.status_code == 503, (
            "降级成空 pending 列表会回「所有文档均已解析」，是比 500 更糟的假结论")
        assert proxy.hits >= 1, "变异守卫：必须真的命中 None 分支"


# ===========================================================================
# D · 上传写路径：不得假成功
# ===========================================================================
class TestUploadWriteGuards:
    async def test_insert_none_returns_503_and_writes_no_row(self, ctx):
        db, pid, sid, uploads = ctx
        proxy = _NullDb(db, lambda s: s.lstrip().upper().startswith(
            "INSERT INTO PROJECT_DOCUMENTS"))
        with pytest.raises(HTTPException) as e:
            await gf.upload_documents(
                scheme_id=sid, project_id="",
                files=[_upload("a.txt",
                               "工程名称：某某项目".encode("utf-8"))], db=proxy)
        assert e.value.status_code == 503
        assert proxy.hits >= 1, "变异守卫：必须真的命中 None 分支"
        # 不得回 saved_count>0，也不得留孤儿文件
        assert await _docs(db, pid) == []
        leftovers = [p for p in uploads.rglob("*") if p.is_file()]
        assert leftovers == [], f"回滚后不得残留磁盘文件: {leftovers}"

    async def test_same_name_delete_none_returns_503_and_rolls_back(self, ctx):
        db, pid, sid, _uploads = ctx
        await gf.upload_documents(
            scheme_id=sid, project_id="",
            files=[_upload("same.txt", b"first version")], db=db)
        before = (await _docs(db, pid))[0]
        old_path = gf._managed_upload_path(before["file_path"])
        assert old_path.exists()

        proxy = _NullDb(db, lambda s: s.lstrip().upper().startswith(
            "DELETE FROM PROJECT_DOCUMENTS"))
        with pytest.raises(HTTPException) as e:
            await gf.upload_documents(
                scheme_id=sid, project_id="",
                files=[_upload("same.txt", b"second version")], db=proxy)
        assert e.value.status_code == 503
        assert proxy.hits >= 1, "变异守卫：必须真的命中 None 分支"

        rows = await _docs(db, pid)
        assert len(rows) == 1, "DELETE 未生效时不得产生同名重复行"
        assert rows[0]["id"] == before["id"], "原档案行必须因回滚而保留"
        assert old_path.exists(), "未提交的替换不得删除旧文件"


# ===========================================================================
# E · 上传配额：break 之后的剩余文件必须被点名
# ===========================================================================
class TestQuotaNamesRemainingFiles:
    async def test_files_after_trigger_are_named(self, ctx, monkeypatch):
        db, _pid, sid, _up = ctx
        monkeypatch.setattr(gf, "MAX_UPLOAD_TOTAL_BYTES", 10)
        res = await gf.upload_documents(
            scheme_id=sid, project_id="",
            files=[_upload("a.txt", b"12345678"),
                   _upload("b.txt", b"12345678"),
                   _upload("c.txt", b"12345678"),
                   _upload("d.txt", b"12345678")],
            db=db)

        assert res["saved_count"] == 1
        assert res["quota_exceeded"] is True
        # b 触顶被拒；c、d 同样未保存，必须一并点名（否则在响应里凭空消失）
        assert res["quota_files"] == ["b.txt", "c.txt", "d.txt"], (
            "break 跳过的剩余文件不得静默丢名")
        # 既有的「超配额不进 oversize」口径不变
        assert "oversize" not in res
        assert await _docs(db, _pid) and len(await _docs(db, _pid)) == 1

    async def test_single_trigger_file_still_only_names_itself(self, ctx, monkeypatch):
        """锁住既有 2 文件口径：触顶文件之后没有剩余文件时不改变 quota_files。"""
        db, _pid, sid, _up = ctx
        monkeypatch.setattr(gf, "MAX_UPLOAD_TOTAL_BYTES", 10)
        res = await gf.upload_documents(
            scheme_id=sid, project_id="",
            files=[_upload("a.txt", b"12345678"), _upload("b.txt", b"12345678")],
            db=db)
        assert res["quota_files"] == ["b.txt"]


# ===========================================================================
# F · 视觉 OCR 可用性：瞬时探测失败不得污染 TTL 缓存
# ===========================================================================
class TestVisionAvailabilityCache:
    """B4：`vision_available` 把「探测异常」写成 False 并缓存 300s。

    后果：一次 DB/配置读抖动 → 扫描件/图片型资料连续 5 分钟静默跳过 OCR，
    解析结果缺页且零告警（用户只会看到「解析成功」但没有那几页正文）。
    """

    @pytest.fixture
    def vcache(self, monkeypatch):
        from app.services import ocr
        monkeypatch.setattr(ocr, "_VISION_CACHE", {"value": None, "ts": 0.0})
        return ocr

    @staticmethod
    def _stub(monkeypatch, fn):
        import app.services.ai.provider_factory as pf
        monkeypatch.setattr(pf, "get_vision_providers", fn)

    async def test_probe_failure_is_not_cached(self, vcache, monkeypatch):
        async def _boom():
            raise RuntimeError("配置读抖动")
        self._stub(monkeypatch, _boom)

        assert await vcache.vision_available() is False, "本次应降级"
        assert vcache._VISION_CACHE["value"] is None, (
            "瞬时失败不得写进 TTL 缓存，否则 300s 内 OCR 恒被静默跳过")

        async def _ok():
            return [object()]
        self._stub(monkeypatch, _ok)
        assert await vcache.vision_available() is True, "故障恢复后必须立刻自愈"

    async def test_failure_reuses_last_known_value(self, vcache, monkeypatch):
        import time as _t
        async def _ok():
            return [object()]
        self._stub(monkeypatch, _ok)
        assert await vcache.vision_available() is True
        # 人为过期，迫使下一次真的去探测
        vcache._VISION_CACHE["ts"] = _t.time() - vcache._VISION_TTL - 1
        probe_ts = vcache._VISION_CACHE["ts"]

        async def _boom():
            raise RuntimeError("瞬时故障")
        self._stub(monkeypatch, _boom)

        assert await vcache.vision_available() is True, (
            "有旧的可信结论时沿用它，而不是翻成 False")
        assert vcache._VISION_CACHE["ts"] == probe_ts, (
            "失败不得推进时间戳（否则等于把故障期续进缓存）")

    async def test_success_still_writes_cache(self, vcache, monkeypatch):
        async def _ok():
            return ["p"]
        self._stub(monkeypatch, _ok)
        assert await vcache.vision_available() is True
        assert vcache._VISION_CACHE["value"] is True
        assert vcache._VISION_CACHE["ts"] > 0
        # 缓存命中时不再调用探测函数
        async def _boom():
            raise AssertionError("缓存命中时不应再探测")
        self._stub(monkeypatch, _boom)
        assert await vcache.vision_available() is True


# ===========================================================================
# G · 静态护栏：返回值不得再被裸丢弃
# ===========================================================================
def _bare_execute_calls(func_node) -> list[str]:
    """收集函数体内「语句级裸 await db.execute/executemany(...)」的调用名。"""
    out: list[str] = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Expr):
            continue
        value = node.value
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
            if value.func.attr in {"execute", "executemany", "executescript"}:
                out.append(value.func.attr)
    return out


def _find_func(module_src: str, name: str) -> ast.AST:
    tree = ast.parse(module_src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"未找到函数 {name}")


def test_persist_parse_result_has_no_bare_execute():
    src = Path(gf.__file__).read_text(encoding="utf-8")
    node = _find_func(src, "_persist_parse_result")
    assert _bare_execute_calls(node) == [], (
        "_persist_parse_result 内出现裸 await db.execute —— 返回值又会被丢弃")


def test_ingest_parse_result_has_no_bare_execute():
    src = Path(pipe.__file__).read_text(encoding="utf-8")
    node = _find_func(src, "ingest_parse_result")
    assert _bare_execute_calls(node) == [], (
        "ingest_parse_result 内出现裸 await db.execute/executemany —— R13 漏改")


def test_upload_write_statements_are_guarded():
    """上传链路的 INSERT / 同名替换 DELETE 必须接住返回值并判空。"""
    src = Path(gf.__file__).read_text(encoding="utf-8")
    node = _find_func(src, "upload_documents")
    body = ast.get_source_segment(src, node) or ""
    assert "ins_cur = await db.execute" in body, "上传 INSERT 未接返回值"
    assert "del_cur = await db.execute" in body, "同名替换 DELETE 未接返回值"
    assert "if ins_cur is None" in body and "if del_cur is None" in body
    assert _bare_execute_calls(node) == []
