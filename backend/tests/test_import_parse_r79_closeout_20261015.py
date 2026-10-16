"""解析提取模块 · R79（2026-10-15）后续需修复项收口护栏。

覆盖本轮 5 项修复：
  A 组 · `bid_analysis.py` R13 收口（37 处 `await db.execute(` 零判空）：
        读路径 None → 503（此前 AttributeError → 500）；写路径 None → 503
        （此前静默不生效）。静态 AST 锁「裸 db.execute 只允许出现在 _execute」。
  B 组 · `format_downstream_context` 遇 JSON **数组**不再抛未捕获 AttributeError
        （人工校正端点契约允许提交数组；异常曾让整份结构化摘要静默退化）。
  C 组 · `sync_extract_layer` 两处 UPDATE 判空（陈旧标记 / extract_status 回写）。
  D 组 · **首次**解析成功也标记事实 stale + 失效导出缓存（单份 / 批量两条路径）。
  E 组 · `_MAX_DOC_CHARS` 的 `or` 模式订正（显式 0 不再被静默改成默认值）。

全部用例均先证伪后写断言：每组都含「修复前必失败」的锚点（A/B 反向验证见
docs/解析提取模块R79收口报告）。
"""
import ast
import importlib
import io
import json
import os
import subprocess
import sys
import textwrap
import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from app.db import get_conn, init_db
from app.routers import bid_analysis as ba

_BA_PATH = os.path.join(os.path.dirname(ba.__file__), "bid_analysis.py")


def _read(path: str) -> str:
    raw = open(path, "rb").read()
    if raw[:3] == b"\xef\xbb\xbf":
        raise AssertionError("BOM 不应出现：%s" % path)
    text = raw.decode("utf-8")
    assert "\ufffd" not in text, "U+FFFD 不应出现：%s" % path
    return text


class _BrokenDb:
    """R13 故障桩：任何 execute 都返回 None（连接/事务瞬时故障形态）。"""

    def __init__(self):
        self.calls = 0

    async def execute(self, *args, **kwargs):
        self.calls += 1
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


class _R13Db:
    """包装真实连接：仅命中关键字的 SQL 返回 None，不影响同请求其它查询。"""

    def __init__(self, real, needles):
        self._real = real
        self._needles = needles
        self.hit = 0

    async def execute(self, sql, *args, **kwargs):
        if any(n in sql for n in self._needles):
            self.hit += 1
            return None
        return await self._real.execute(sql, *args, **kwargs)

    async def commit(self):
        return await self._real.commit()

    async def rollback(self):
        return await self._real.rollback()


# ---------------------------------------------------------------------------
# A 组：bid_analysis R13 收口
# ---------------------------------------------------------------------------
class TestBidAnalysisR13Closeout:
    def _raw_execute_locations(self) -> list[int]:
        """返回裸 `await db.execute(` 的行号（helper 自身那一次除外）。"""
        src = _read(_BA_PATH)
        tree = ast.parse(src)
        helper_nodes: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == "_execute":
                for sub in ast.walk(node):
                    helper_nodes.add(id(sub))
        raw: list[int] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
                f = node.value.func
                if isinstance(f, ast.Attribute) and f.attr == "execute" \
                        and isinstance(f.value, ast.Name) and f.value.id == "db":
                    if id(node) in helper_nodes:
                        continue
                    raw.append(node.lineno)
        return raw

    def test_no_bare_execute_outside_helper(self):
        """全模块裸 db.execute 只允许出现在 _execute 函数体内。

        修复前 37 处裸调用：读路径 AttributeError(500)、写路径静默丢失。
        该断言不依赖具体数字 —— 新增调用点若绕过 helper 立刻失败。
        """
        raw = self._raw_execute_locations()
        assert raw == [], (
            "bid_analysis.py 存在绕过 _execute 的裸 db.execute 调用：%s" % raw)

    def test_helper_sites_present(self):
        """helper 必须真实被消费（防空转：只定义不接线）。"""
        src = _read(_BA_PATH)
        tree = ast.parse(src)
        n = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
                f = node.value.func
                if isinstance(f, ast.Name) and f.id == "_execute":
                    n += 1
        assert n >= 30, "helper 调用点过少（%d），疑似未接线" % n

    def test_helper_raises_503_on_none(self):
        """helper 契约：游标 None → HTTPException(503)，绝不降级为空结果。"""
        async def _run():
            return await ba._execute(_BrokenDb(), "SELECT 1")
        with pytest.raises(HTTPException) as ei:
            asyncio_run(_run())
        assert ei.value.status_code == 503

    @pytest.mark.parametrize("caller,args", [
        ("list_analysis_results", {"scheme_id": "s", "project_id": "p"}),
        ("get_single_result", {"item_id": "projectBasicInfo",
                               "scheme_id": "s", "project_id": "p"}),
    ])
    def test_read_paths_503_on_none_cursor(self, caller, args):
        """读路径 None 游标 → 503（此前 AttributeError → 500）。

        注：两者是路由端点，db 是关键字参数（Depends(get_db)），首参是
        scheme_id/item_id —— 不能按位置传 db（先前写法 TypeError 错位）。
        """
        fn = getattr(ba, caller)
        with pytest.raises(HTTPException) as ei:
            asyncio_run(fn(**args, db=_BrokenDb()))
        assert ei.value.status_code == 503

    def test_scheme_name_fail_soft(self, caplog):
        """_fetch_scheme_name 是展示性兜底：None 游标 → 告警 + 空串（不 503）。

        唯一消费点 /classify 的 scheme_name 兜底，设计明示「取不到名称 →
        空串，不阻断分类」——与端点级「None → 503」是两档语义，不得混同。
        """
        with caplog.at_level("WARNING"):
            out = asyncio_run(ba._fetch_scheme_name(_BrokenDb(), "s"))
        assert out == ""
        assert any("R13" in r.getMessage() for r in caplog.records), \
            "None 游标必须留下可观测告警"

    def test_scheme_name_empty_short_circuit(self):
        """空 scheme_id 直接短路返回空串（不触库）。"""
        db = _BrokenDb()
        out = asyncio_run(ba._fetch_scheme_name(db, ""))
        assert out == ""
        assert db.calls == 0

    def test_resolve_pid_503_on_none_cursor(self):
        """`_resolve_pid` 是模块所有入口的前置 —— 它 500 则全模块 500。"""
        with pytest.raises(HTTPException) as ei:
            asyncio_run(ba._resolve_pid(_BrokenDb(), "sid"))
        assert ei.value.status_code == 503

    def test_write_path_503_on_none_cursor(self):
        """写路径 None 游标 → 503（此前 UPDATE/INSERT 静默不生效）。"""
        with pytest.raises(HTTPException) as ei:
            asyncio_run(ba._update_item_status(
                _BrokenDb(), "pid", "projectBasicInfo", "success", "内容"))
        assert ei.value.status_code == 503

    def test_update_single_result_503_on_none_cursor(self):
        """人工校正端点 None → 503（此前用户提交的校正静默丢失）。"""
        with pytest.raises(HTTPException) as ei:
            asyncio_run(ba.update_single_result(
                "projectBasicInfo", {"content": "某项目"},
                scheme_id="s", project_id="p", db=_BrokenDb()))
        assert ei.value.status_code == 503

    async def test_normal_path_unaffected(self, db_conn):
        """回归：正常连接下读路径行为不变（不因 helper 引入 503）。"""
        db = db_conn
        pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db.execute(
            "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
        await db.execute(
            "INSERT INTO bid_analysis_items(id, project_id, item_id, label,"
            " output_type, required, status, content, sort_order, source)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"{pid}_projectBasicInfo", pid, "projectBasicInfo", "项目基本信息",
             "json", 1, "success", json.dumps({"project_name": "某项目"}), 1, "ai"))
        await db.commit()
        out = await ba.list_analysis_results(scheme_id=sid, project_id="", db=db)
        # summary.total 是 18 项提取目录的固定口径（len(ANALYSIS_ITEMS)），
        # 不随存储行数变化；本用例只存了 1 行 ⇒ 断言 items 与 success。
        assert len(out["items"]) == 1
        assert out["summary"]["success"] == 1


def asyncio_run(coro):
    """同步驱动协程（测试内不引 pytest-asyncio 也能跑同步用例）。

    用 asyncio.run（每次新建事件循环）：3.14 起 get_event_loop 在无运行
    循环时直接 RuntimeError，asyncio.run 在 3.13/3.14 行为一致。
    """
    import asyncio
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# B 组：format_downstream_context 的 JSON 数组
# ---------------------------------------------------------------------------
class TestDownstreamJsonArray:
    def _items(self, content: str):
        return {"projectBasicInfo": {
            "status": "success", "content": content,
            "output_type": "json", "label": "项目基本信息"}}

    def test_array_content_does_not_raise(self):
        """修复前：data.items() 对 list 抛 AttributeError（被上层吞成降级）。"""
        from app.services.bid_analysis_service import format_downstream_context
        content = json.dumps([{"name": "甲段"}, {"name": "乙段"}],
                             ensure_ascii=False)
        out = format_downstream_context(self._items(content))
        assert "项目基本信息" in out
        assert "甲段" in out and "乙段" in out, "数组条目必须展平下发，不得整段丢弃"

    def test_array_of_scalars_and_nested_dicts(self):
        from app.services.bid_analysis_service import format_downstream_context
        content = json.dumps(["清单A", {"k1": "v1", "k2": ""}, "没有提及"],
                             ensure_ascii=False)
        out = format_downstream_context(self._items(content))
        assert "清单A" in out
        assert "k1：v1" in out, "dict 元素须拼为 k：v"
        assert "k2" not in out, "空值字段不得下发"
        assert "没有提及" not in out

    def test_object_form_unchanged(self):
        """回归：对象形态与旧行为逐字一致（仍是 - **k**: v 行）。"""
        from app.services.bid_analysis_service import format_downstream_context
        out = format_downstream_context(
            self._items(json.dumps({"project_name": "某项目"},
                                   ensure_ascii=False)))
        assert "- **project_name**: 某项目" in out

    def test_invalid_json_falls_back_to_raw(self):
        """回归：坏 JSON 仍原样下发（不抛异常、不吞内容）。"""
        from app.services.bid_analysis_service import format_downstream_context
        out = format_downstream_context(self._items("{not json"))
        assert "{not json" in out

    def test_empty_object_still_skipped(self):
        """回归：空对象失败哨兵仍整体跳过（§4.11.2 修复不回退）。"""
        from app.services.bid_analysis_service import format_downstream_context
        out = format_downstream_context(self._items("{}"))
        assert "项目基本信息" not in out


# ---------------------------------------------------------------------------
# C 组：sync_extract_layer R13 判空
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestSyncExtractLayerR13:
    @pytest_asyncio.fixture
    async def ctx(self, db_conn, tmp_path, monkeypatch):
        import app.routers.global_facts as gf
        import app.services.doc_pipeline.doc_storage as ds
        monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", tmp_path / "uploads")
        monkeypatch.setattr(ds, "DOCS_ROOT", tmp_path / "projects")
        db = db_conn
        pid, doc_id = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db.execute(
            "INSERT INTO project_documents(id, project_id, file_name,"
            " parse_status, parsed_markdown) VALUES(?,?,?,?,?)",
            (doc_id, pid, "a.docx", "success", "正文"))
        await db.commit()
        return {"db": db, "project_id": pid, "doc_id": doc_id}

    async def test_stale_update_none_does_not_raise(self, ctx, caplog):
        """修复前：db.execute 返回 None 时陈旧标记静默不生效。"""
        from app.services.doc_pipeline import pipeline
        c = ctx
        await db_execute(c["db"],
                         "INSERT INTO doc_extractions(extraction_id, doc_id,"
                         " project_id, extract_type, extract_data, status)"
                         " VALUES(?,?,?,?,?,?)",
                         (uuid.uuid4().hex, c["doc_id"], c["project_id"],
                          "project_info", "{}", "success"))
        await c["db"].commit()
        fake = _R13Db(c["db"], ["UPDATE doc_extractions SET status='stale'"])
        with caplog.at_level("WARNING"):
            res = await pipeline.sync_extract_layer(
                fake, doc_id=c["doc_id"], project_id=c["project_id"])
        assert fake.hit, "测试必须真实命中被拦截的 UPDATE"
        assert isinstance(res, dict), "判空后必须继续返回，不得抛错"
        assert any("db.execute 返回 None" in r.message or "R13" in r.message
                   for r in caplog.records), "None 游标必须留下可观测告警"

    async def test_extract_status_update_none_does_not_raise(self, ctx, caplog):
        from app.services.doc_pipeline import pipeline
        c = ctx
        fake = _R13Db(c["db"], ["UPDATE project_documents SET extract_status"])
        with caplog.at_level("WARNING"):
            res = await pipeline.sync_extract_layer(
                fake, doc_id=c["doc_id"], project_id=c["project_id"])
        assert fake.hit
        assert res["extract_status"] in ("success", "pending")

    def test_both_updates_are_guarded(self):
        """静态锁：两条 UPDATE 的结果必须被 None 检查（防再漏）。"""
        from app.services.doc_pipeline import pipeline as _pl
        src = textwrap.dedent(inspect_getsource(_pl.sync_extract_layer))
        tree = ast.parse(src)
        guarded = 0
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Await)):
                continue
            call = node.value.value
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                continue
            if call.func.attr != "execute":
                continue
            sql = call.args[0] if call.args else None
            if not (isinstance(sql, ast.JoinedStr) or isinstance(sql, ast.Constant)):
                continue
            if isinstance(sql, ast.Constant):
                # 纯字符串字面量（非 f-string）：SQL 在 .value 里
                text = sql.value if isinstance(sql.value, str) else ""
            else:
                text = "".join(v.value for v in sql.values
                               if isinstance(v, ast.Constant))
            if "UPDATE doc_extractions SET status='stale'" not in text \
                    and "UPDATE project_documents SET extract_status" not in text:
                continue
            target = node.targets[0]
            name = target.id if isinstance(target, ast.Name) else ""
            if not name:
                continue
            if f"if {name} is None:" not in src:
                raise AssertionError("UPDATE 结果 %s 未判空" % name)
            guarded += 1
        assert guarded == 2, "两条 UPDATE 都应判空，实际 %d" % guarded


async def db_execute(db, sql, params=()):
    return await db.execute(sql, params)


def inspect_getsource(fn) -> str:
    import inspect
    return inspect.getsource(fn)


# ---------------------------------------------------------------------------
# D 组：首次解析成功的下游失效
# ---------------------------------------------------------------------------
def _upload(name: str, data: bytes):
    from starlette.datastructures import UploadFile
    return UploadFile(file=io.BytesIO(data), filename=name, size=len(data))


@pytest.mark.asyncio
class TestFirstParseInvalidatesDownstream:
    @pytest_asyncio.fixture
    async def ctx(self, tmp_path, monkeypatch):
        import app.db as appdb
        appdb.DB_PATH = tmp_path / "first-parse.sqlite"
        await init_db()
        monkeypatch.setattr(ba, "_appdb_stub", appdb, raising=False)
        import app.routers.global_facts as gf
        monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", tmp_path / "uploads")
        db = await get_conn()
        pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db.execute(
            "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
        await db.commit()
        yield db, pid, sid
        await appdb.close_db()

    async def _first_doc(self, ctx):
        db, pid, sid = ctx
        import app.routers.global_facts as gf
        await gf.upload_documents(
            scheme_id=sid, project_id="",
            files=[_upload("spec.txt", "工程名称：某产业园\n工期：120 日历天".encode("utf-8"))],
            db=db)
        cur = await db.execute(
            "SELECT id FROM project_documents WHERE project_id=?", (pid,))
        return (await cur.fetchone())[0]

    async def test_first_parse_marks_facts_stale(self, ctx):
        """修复前：非 force 的首次解析成功不标记 stale —— 事实库漏掉整份文档。"""
        db, pid, sid = ctx
        doc_id = await self._first_doc(ctx)
        await db.execute(
            "INSERT INTO global_facts(id,project_id,scheme_id,group_id,title,"
            "content,category,is_resolved) VALUES(?,?,?,'g','工期',"
            "'- **工期**: 120 日历天','schedule',1)", (uuid.uuid4().hex, pid, sid))
        await db.commit()
        import app.routers.global_facts as gf
        res = await gf.parse_document(doc_id, db=db)
        assert res.get("stale_facts") == 1
        assert res.get("note")
        cur = await db.execute("SELECT is_stale FROM global_facts WHERE project_id=?",
                               (pid,))
        assert (await cur.fetchone())["is_stale"] == 1

    async def test_first_parse_all_marks_facts_stale(self, ctx):
        """批量非强制路径同样收口（此前只有 `force and parsed` 才处理）。"""
        db, pid, sid = ctx
        doc_id = await self._first_doc(ctx)
        await db.execute(
            "INSERT INTO global_facts(id,project_id,scheme_id,group_id,title,"
            "content,category,is_resolved) VALUES(?,?,?,'g','工期',"
            "'- **工期**: 120 日历天','schedule',1)", (uuid.uuid4().hex, pid, sid))
        await db.commit()
        import app.routers.global_facts as gf
        res = await gf.parse_all_documents(scheme_id=sid, project_id="", db=db)
        assert res.get("parsed") == 1
        assert res.get("stale_facts") == 1
        assert res.get("note")
        cur = await db.execute("SELECT is_stale FROM global_facts WHERE project_id=?",
                               (pid,))
        assert (await cur.fetchone())["is_stale"] == 1

    async def test_force_reparse_note_unchanged(self, ctx):
        """回归：force 分支文案逐字不变（既有护栏契约）。"""
        db, pid, sid = ctx
        doc_id = await self._first_doc(ctx)
        import app.routers.global_facts as gf
        await gf.parse_document(doc_id, db=db)
        res = await gf.parse_document(doc_id, force=True, db=db)
        assert "已重新解析" in res.get("note", "")

    async def test_already_parsed_short_circuit_unchanged(self, ctx):
        """回归：已解析短路分支不带新键（加法式只加在真正解析成功的路径）。"""
        db, pid, sid = ctx
        doc_id = await self._first_doc(ctx)
        import app.routers.global_facts as gf
        await gf.parse_document(doc_id, db=db)
        res = await gf.parse_document(doc_id, db=db)
        assert res.get("already_parsed") is True
        assert "note" not in res and "stale_facts" not in res


# ---------------------------------------------------------------------------
# E 组：_MAX_DOC_CHARS 的 or 模式
# ---------------------------------------------------------------------------
class TestMaxDocCharsConfig:
    def test_zero_budget_is_honored_not_silently_defaulted(self):
        """修复前：`getattr(...) or 400000` 把显式 0 静默改成 400000。

        用子进程隔离 settings（避免 modle 级常量重算污染其它用例）。
        """
        env = dict(os.environ, BID_ANALYSIS_SEGMENT_BUDGET="0")
        r = subprocess.run(
            [sys.executable, "-c",
             "from app.routers import bid_analysis as ba;"
             "print(ba._MAX_DOC_CHARS)"],
            # _BA_PATH = backend/app/routers/bid_analysis.py
            # → 三层 dirname 才是 backend（先前少取一层落在 backend/app，
            #   子进程 `import app` 报 ModuleNotFoundError）
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(_BA_PATH))),
            env=env, capture_output=True)
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
        assert r.stdout.decode().strip() == "1000", (
            "显式 0 必须被尊重（走到 max(1000, 0)=1000），而不是被 or 改成默认值")

    def test_default_budget_unchanged(self):
        """回归：未配置时仍是配置默认值。"""
        from app.config import settings
        assert ba._MAX_DOC_CHARS == max(
            1000, settings.bid_analysis_segment_budget)

    def test_source_has_no_or_default_pattern(self):
        """静态锁：赋值右值不得再出现 `or <默认>` 形态。"""
        src = _read(_BA_PATH)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "_MAX_DOC_CHARS":
                        seg = ast.get_source_segment(src, node) or ""
                        assert " or " not in seg, (
                            "_MAX_DOC_CHARS 仍用 or 读取配置：%s" % seg)
                        assert "is None" in seg, "必须显式判 None"
