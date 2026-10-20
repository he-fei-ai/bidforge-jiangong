"""R108 护栏 · 日志审计遗留收口（SQLite 锁族接线 + 提示词缓存水印 + 审计误诊分流 + 确定性死配置 + 测试流量日志隔离）

覆盖 R107 报告修复清单（P0×4 / P1-1 / P1-2 / P1-4 / P2-1）：
  A 组 · db.retry_db_op jitter 参数契约（默认逐字旧行为 / 抖动叠加 / 分流 / 耗尽）
  B 组 · _chart_pipeline DELETE 接重试（功能实证 + AST 锁）
  C 组 · sse_handlers task_control 锁耗尽 → 503+Retry-After；非锁原样上抛；
         _persist_section UPDATE 重试与图表登记降级接线锁
  D 组 · task_registry.set_task_status 加重预算（5 次 + jitter）
  E 组 · export._db_exec 重试 + _persist_export_cache_row 分段 fail-soft
  F 组 · provider_factory：缺列分流判据 / 审计锁上抛 / 真缺列降级 / save_ai_config
         整事务重放 / P1-4 确定性死配置连击（计数、清零、判死、保存重置）
  G 组 · prompts._cache 表级水印（他表 bump 不重载 / 真变更重载 / 无水印旧行为 / 节流）
  H 组 · main P2-1 文件日志守卫（pytest / starlette.testclient / env 逃生 / 幂等安装）
  I 组 · 字节体检（触碰文件无 BOM / 无孤立 CR / 无 FFFD）

判据纪律（AGENTS §5.14/§5.15）：功能实证为主，AST/源码锁只锚调用形态与结构；
模块全局（streak、水印、连击阈值）一律快照/复位 fixture，防跨用例污染。
"""
from __future__ import annotations

import ast
import inspect
import logging
import sqlite3
import sys
import time
from pathlib import Path

import app.db as DB
import app.main as M
import app.routers._chart_pipeline as CP
import app.routers.export as E
import app.routers.sse_handlers as S
import app.services.ai.prompts._cache as PC
import app.services.ai.provider_factory as PF
import app.services.ai.task_registry as TR
import pytest

APP_DIR = Path(S.__file__).resolve().parents[1]

_LOCKED = sqlite3.OperationalError("database is locked")
_NO_SUCH_TABLE = sqlite3.OperationalError("no such table: nope")


def _locked(msg: str = "database is locked") -> sqlite3.OperationalError:
    return sqlite3.OperationalError(msg)


def _fn_ast(rel: str, name: str):
    tree = ast.parse((APP_DIR / rel).read_bytes().decode("utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"未找到函数 {name}")


def _call_names(node) -> set:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _src(fn) -> str:
    return inspect.getsource(fn)


# ===========================================================================
# A 组 · retry_db_op jitter 参数契约
# ===========================================================================

class TestRetryDbOpJitter:
    @pytest.fixture
    def sleeps(self, monkeypatch):
        recorded: list[float] = []

        async def fake_sleep(d):
            recorded.append(d)

        monkeypatch.setattr(DB.asyncio, "sleep", fake_sleep)
        return recorded

    async def test_default_zero_matches_legacy_delays(self, sleeps):
        """jitter 默认 0 → 退避序列与旧行为逐字一致（0.05, 0.10）。"""
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise _locked()
            return "ok"

        assert await DB.retry_db_op(factory) == "ok"
        assert sleeps == [0.05, 0.10], "默认退避必须与引入前逐字一致（无抖动）"

    async def test_jitter_adds_bounded_random(self, sleeps, monkeypatch):
        """jitter>0 → 每级叠加 uniform(0, jitter)；uniform 被固定为 0.5*jitter。"""
        monkeypatch.setattr(DB.random, "uniform", lambda a, b: b * 0.5)

        async def factory():
            raise _locked()

        with pytest.raises(sqlite3.OperationalError):
            await DB.retry_db_op(factory, max_retries=2, base_delay=0.1, jitter=0.2)
        # 0.1*1+0.1, 0.1*2+0.1（uniform(0,0.2) 固定 0.1）
        assert sleeps == [pytest.approx(0.2), pytest.approx(0.3)]

    async def test_non_retryable_raises_immediately(self, sleeps):
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            raise _NO_SUCH_TABLE

        with pytest.raises(sqlite3.OperationalError):
            await DB.retry_db_op(factory)
        assert calls["n"] == 1 and sleeps == []

    async def test_exhaustion_raises_last_error(self, sleeps):
        async def factory():
            raise _locked()

        with pytest.raises(sqlite3.OperationalError):
            await DB.retry_db_op(factory, max_retries=1, base_delay=0.01)
        assert sleeps == [0.01]


# ===========================================================================
# B 组 · _chart_pipeline DELETE 接重试（P0-1）
# ===========================================================================

class _FlakyExecute:
    """前 fail_times 次 execute 抛 exc，之后成功返回哨兵。"""

    def __init__(self, fail_times: int, exc_factory=_locked):
        self.fail_times = fail_times
        self.exc_factory = exc_factory
        self.sqls: list[str] = []

    async def execute(self, sql, params=()):
        self.sqls.append(sql)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.exc_factory()
        return None


class TestChartDeleteRetry:
    async def test_delete_locked_then_success_survives(self, monkeypatch):
        """功能实证：DELETE 首跳 locked → retry_db_op 重放 → 函数正常返回。"""
        async def no_sleep(_d):
            return None
        monkeypatch.setattr(DB.asyncio, "sleep", no_sleep)
        db = _FlakyExecute(fail_times=1)
        out = await CP.apply_inline_chart_plan(db, "sec-1", [], "正文原样")
        assert out == "正文原样", "rows 为空时应原样返回 content（既有契约不变）"
        delete_calls = [s for s in db.sqls if "DELETE FROM chart_predictions" in s]
        assert len(delete_calls) == 2, "locked 一次后必须重放 DELETE（共 2 次尝试）"

    async def test_delete_wrapped_in_retry_db_op_ast(self):
        """AST 锁：DELETE 语句必须包在 retry_db_op 调用子树内，不许裸 execute 回流。"""
        fn = _fn_ast("routers/_chart_pipeline.py", "apply_inline_chart_plan")
        wrapped = False
        bare = False
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = n.func
                fname = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                consts = [c.value for c in ast.walk(n)
                          if isinstance(c, ast.Constant) and isinstance(c.value, str)]
                has_del = any(str(v).startswith("DELETE FROM chart_predictions")
                              for v in consts)
                if fname == "retry_db_op" and has_del:
                    wrapped = True
                if fname == "execute" and has_del:
                    # execute 出现在 retry_db_op 的 lambda 里时也在其子树——
                    # 用「该 execute 调用是否为 retry_db_op 子节点」区分裸调用。
                    pass
        assert wrapped, "DELETE FROM chart_predictions 未接入 retry_db_op"
        # 裸调用判定：retry_db_op 之外的 execute(DELETE) 不允许存在
        retry_subtrees: set = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and (
                (isinstance(n.func, ast.Name) and n.func.id == "retry_db_op")):
                for m in ast.walk(n):
                    retry_subtrees.add(id(m))
        for n in ast.walk(fn):
            # 只对「execute 调用」做裸判定：根节点/语句容器（AsyncFunctionDef 等）
            # 天然不在 retry_subtrees 内，walk 必然扫到函数体里的 DELETE 常量，
            # 若不先过滤 Call+fname 会把正确接线误判为裸调用（假阳性）。
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            fname = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if fname != "execute" or id(n) in retry_subtrees:
                continue
            consts = [c.value for c in ast.walk(n)
                      if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            if any(str(v).startswith("DELETE FROM chart_predictions") for v in consts):
                bare = True
        assert not bare, "存在 retry_db_op 之外的裸 DELETE（半接线=没接）"


# ===========================================================================
# C 组 · task_control 503 分流 + _persist_section 接线锁（P0-2）
# ===========================================================================

class TestTaskControl503:
    async def test_locked_lands_503_with_retry_after(self, db_conn, monkeypatch):
        """锁类异常耗尽 → HTTPException 503 + Retry-After: 2（不再裸 500）。"""
        tid = await TR.register_task("content_generation", scheme_id="s1")

        async def boom(task_id, status):
            raise _locked()

        monkeypatch.setattr(TR, "set_task_status", boom)
        with pytest.raises(M.__dict__.get("HTTPException") or Exception) as ei:
            await S.task_control(tid, {"action": "stop"})
        from fastapi import HTTPException
        exc = ei.value
        assert isinstance(exc, HTTPException), "锁耗尽必须是 503，不是裸异常直穿"
        assert exc.status_code == 503
        assert (exc.headers or {}).get("Retry-After") == "2", \
            "Retry-After 缺失则前端 tasksApi.control 的重试节拍无依据"

    async def test_non_lock_error_passes_through(self, db_conn, monkeypatch):
        tid = await TR.register_task("content_generation", scheme_id="s1")

        async def boom(task_id, status):
            raise RuntimeError("真故障不粉饰")

        monkeypatch.setattr(TR, "set_task_status", boom)
        from fastapi import HTTPException
        with pytest.raises(RuntimeError):
            await S.task_control(tid, {"action": "stop"})

    async def test_persist_update_wrapped_in_retry(self):
        """源码锁：正文 UPDATE+commit 收敛进 _persist_content_once 且被 retry_db_op 包住。"""
        src = _src(S.generate_content)
        assert "async def _persist_content_once" in src, "正文落库段未收敛为可重放闭包"
        seg = src.split("async def _persist_content_once", 1)[1]
        seg = seg.split("retry_db_op", 1)[1]
        assert "UPDATE sections SET content=" in src, "闭包内必须含正文 UPDATE"
        assert "retry_db_op(_persist_content_once" in src.replace(" ", "").replace(
            "retry_db_op(_persist_content_once", "retry_db_op(_persist_content_once"), \
            "闭包未接 retry_db_op"
        assert "retry_db_op(_persist_content_once" in "".join(src.split()), \
            "_persist_content_once 未作为 retry_db_op 实参"

    async def test_chart_apply_failure_degrades_not_kills_content(self):
        """源码锁：apply_inline_chart_plan 调用被 try/except 包住，失败置
        _chart_apply_failed 并 rollback——正文照常落库（P0-1 降级口径）。"""
        src = _src(S.generate_content)
        assert "_chart_apply_failed = False" in src
        i_apply = src.index("await apply_inline_chart_plan(")
        i_flag = src.index("_chart_apply_failed = True")
        assert i_apply < i_flag, "登记失败路径必须经过 _chart_apply_failed 闸"
        i_gate = src.index("if not _chart_apply_failed:")
        assert i_flag < i_gate, "失败闸后必须门控后续字数重算/report 环节"
        seg = src[i_apply:i_flag]
        assert "await db.rollback()" in seg, "登记失败必须回滚半途图表写"


# ===========================================================================
# D 组 · set_task_status 预算加重（P0-2 后端侧）
# ===========================================================================

class TestSetTaskStatusBudget:
    async def test_control_write_budget_5_with_jitter(self, db_conn, monkeypatch):
        """控制指令走 max_retries=5 + jitter>0（0.35s 旧预算是生产 500 实证根因）。"""
        seen: dict = {}
        orig = DB.retry_db_op

        async def spy(coro_factory, **kw):
            seen.update(kw)
            return await orig(coro_factory, **kw)

        monkeypatch.setattr(TR, "retry_db_op", spy)
        tid = await TR.register_task("content_generation", scheme_id="s1")
        await TR.set_task_status(tid, "paused")
        assert seen.get("max_retries") == 5, seen
        assert seen.get("jitter", 0) > 0, "控制指令必须带抖动打散同拍惊群"


# ===========================================================================
# E 组 · export._db_exec 重试 + 缓存行 fail-soft（P0-3）
# ===========================================================================

class _FakeExportDB:
    def __init__(self, commit_fail_times: int = 0, exec_fail_times: int = 0):
        self.commit_fail_times = commit_fail_times
        self.exec_fail_times = exec_fail_times
        self.commits = 0
        self.rollbacks = 0
        self.sqls: list[str] = []

    async def execute(self, sql, params=()):
        self.sqls.append(sql)
        if self.exec_fail_times > 0:
            self.exec_fail_times -= 1
            raise _locked()
        return object()

    async def commit(self):
        self.commits += 1
        if self.commit_fail_times > 0:
            self.commit_fail_times -= 1
            raise _locked()

    async def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def fast_retry(monkeypatch):
    async def no_sleep(_d):
        return None
    monkeypatch.setattr(DB.asyncio, "sleep", no_sleep)


class TestExportDbExec:
    async def test_locked_execute_replays(self, fast_retry):
        db = _FakeExportDB(exec_fail_times=1)
        ok = await E._db_exec(db, "UPDATE export_presets SET x=1 WHERE id=?", (),
                              what="t")
        assert ok is True
        assert len(db.sqls) == 2, "_db_exec 自称写路径唯一出口，锁瞬态必须在此重试"

    async def test_non_retryable_raises(self, fast_retry):
        """语义错误（no such table）不属于锁族 → 首次即抛、零重试（判据不扩大）。"""
        bad = _FlakyExecute(fail_times=1, exc_factory=lambda: _NO_SUCH_TABLE)
        with pytest.raises(sqlite3.OperationalError):
            await E._db_exec(bad, "DROP TABLE nope", (), what="t2")
        assert len(bad.sqls) == 1, "非可重试错误不得烧第二跳"


class TestPersistExportCacheRow:
    async def test_commit_lock_exhausted_failsoft(self, fast_retry, caplog):
        """commit 恒 locked → 不抛异常、rollback、WARNING 点名（导出交付不受影响）。"""
        db = _FakeExportDB(commit_fail_times=99)
        with caplog.at_level(logging.WARNING):
            await E._persist_export_cache_row(
                db, scheme={"project_id": "p1"}, scheme_id="s1",
                config_hash="h", content_fingerprint="f", cache_key="k",
                out_path=Path("out.docx"), what="缓存行")
        assert db.rollbacks >= 1, "重试耗尽必须回滚半开事务"
        assert "缓存行持久化失败" in caplog.text

    async def test_prune_failure_does_not_block(self, fast_retry, caplog, monkeypatch):
        """INSERT+commit 成功后 prune 恒 locked → 二级 fail-soft，主缓存行保留。"""
        db = _FakeExportDB(commit_fail_times=1)  # 第 1 次 commit（主行）成功，prune 段失败

        async def boom(conn, scheme_id, protect_path=None):
            raise _locked()

        monkeypatch.setattr(E, "_prune_export_cache", boom)
        with caplog.at_level(logging.WARNING):
            await E._persist_export_cache_row(
                db, scheme={"project_id": "p1"}, scheme_id="s1",
                config_hash="h", content_fingerprint="f", cache_key="k",
                out_path=Path("out.docx"), what="缓存行")
        assert any("INSERT OR IGNORE INTO export_cache" in s for s in db.sqls)
        assert "缓存裁剪失败" in caplog.text, "裁剪失败必须留痕但不阻断"

    async def test_happy_path_double_commit(self, fast_retry):
        calls: list = []

        async def fake_prune(conn, scheme_id, protect_path=None):
            calls.append(scheme_id)

        db = _FakeExportDB()
        monkey_prune = db  # noqa
        import app.routers.export as _E
        orig = _E._prune_export_cache

        async def prune(conn, scheme_id, protect_path=None):
            calls.append(scheme_id)
            await conn.execute("DELETE FROM export_cache WHERE 0", ())

        _E._prune_export_cache = prune
        try:
            await E._persist_export_cache_row(
                db, scheme={"project_id": "p1"}, scheme_id="s1",
                config_hash="h", content_fingerprint="f", cache_key="k",
                out_path=Path("out.docx"), what="缓存行")
        finally:
            _E._prune_export_cache = orig
        assert db.commits == 2, "双提交口径（新行先落盘再裁剪）不得回退"
        assert calls == ["s1"]


# ===========================================================================
# F 组 · provider_factory：缺列分流 / 审计锁上抛 / save 重试 / 确定性死配置
# ===========================================================================

class TestMissingColumnPredicate:
    def test_contract(self):
        assert PF._is_missing_column_error(
            _locked("table ai_audit_logs has no column named config_id"))
        assert PF._is_missing_column_error(_locked("no such column: updated_at"))
        assert not PF._is_missing_column_error(_locked("database is locked"))
        assert not PF._is_missing_column_error(ValueError("行宽守卫"))


class _RecordingConn:
    def __init__(self, fail_map):
        self.fail_map = fail_map      # SQL 前缀 → 抛的异常（每次命中后移除可模拟成功）
        self.calls: list[tuple[str, int]] = []
        self.commits = 0

    def _fail_for(self, sql):
        for prefix, exc in list(self.fail_map.items()):
            if sql.startswith(prefix):
                return exc
        return None

    async def executemany(self, sql, rows):
        self.calls.append((sql, len(list(rows))))
        exc = self._fail_for(sql)
        if exc is not None:
            raise exc

    async def commit(self):
        self.commits += 1


@pytest.fixture
def fake_wtx(monkeypatch):
    """把 PF.write_tx_conn 替换为 yield 指定 conn 的一次性 cm。"""
    from contextlib import asynccontextmanager

    def install(conn):
        @asynccontextmanager
        async def _wtx():
            try:
                yield conn
            except BaseException:
                raise
        monkeypatch.setattr(PF, "write_tx_conn", _wtx)
        return conn
    return install


class TestAuditFlushTriage:
    async def test_locked_raises_without_degrade(self, fake_wtx, monkeypatch):
        """锁瞬态必须**原样上抛**给外层 retry_db_op——绝不进缺列降级链（P1-2 误诊根因）。"""
        conn = fake_wtx(_RecordingConn(
            {"INSERT INTO ai_audit_logs": _locked()}))
        rows = [("i1", "p", "m", "a", 1, 2, 3, 0.5, 0, "e", "s", "cid", "url")]
        with pytest.raises(sqlite3.OperationalError):
            await PF._flush_audit_rows_once(rows)
        assert len(conn.calls) == 1, "锁错后不得尝试任何降级语句"

    async def test_true_missing_column_degrades(self, fake_wtx):
        """真缺列（no column named）→ 降到 NO_IDENTITY 并成功提交。"""
        conn = fake_wtx(_RecordingConn({
            PF._AUDIT_INSERT_FULL: _locked(
                "table ai_audit_logs has no column named config_id")}))
        rows = [("i1", "p", "m", "a", 1, 2, 3, 0.5, 1, "", "s", "cid", "url")]
        await PF._flush_audit_rows_once(rows)
        assert len(conn.calls) == 2
        assert conn.calls[1][0] == PF._AUDIT_INSERT_NO_IDENTITY
        assert conn.calls[1][1] == 1 and conn.commits == 1

    async def test_flush_buffer_wires_retry(self):
        """接线锁：_flush_audit_buffer 必须经 retry_db_op 调 rows_once（分流的前提）。"""
        fn = _fn_ast("services/ai/provider_factory.py", "_flush_audit_buffer")
        assert "retry_db_op" in _call_names(fn)

    async def test_binding_mismatch_still_degrades(self, fake_wtx):
        """旧行宽 ProgrammingError（绑定数≠占位符数）→ 维持按行宽逐级降级，
        不上抛终结降级链——溢出队列可能残留跨版本 11/10 元组旧行，
        上抛会让该批永远回灌并拖死同队列新批次（R24 既有护栏重锚先例）。"""
        conn = fake_wtx(_RecordingConn({
            PF._AUDIT_INSERT_FULL: sqlite3.ProgrammingError(
                "Incorrect number of bindings supplied")}))
        rows = [("i1", "p", "m", "a", 1, 2, 3, 0.5, 1, "", "s")]  # 11 元组旧行宽
        await PF._flush_audit_rows_once(rows)
        assert len(conn.calls) == 2
        assert conn.calls[1][0] == PF._AUDIT_INSERT_NO_IDENTITY
        assert conn.commits == 1


class TestSaveAiConfigRetry:
    async def test_locked_first_attempt_replays_whole_tx(self, db_conn, monkeypatch):
        """P0-4：write_tx_conn 首试 execute 抛 locked → retry_db_op 重放整事务 → 落库成功。"""
        async def no_sleep(_d):
            return None
        monkeypatch.setattr(DB.asyncio, "sleep", no_sleep)

        orig_wtx = PF.write_tx_conn
        state = {"n": 0}

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def flaky_wtx():
            state["n"] += 1
            if state["n"] == 1:
                class _Bad:
                    async def execute(self, *a, **k):
                        raise _locked()
                    async def commit(self):
                        raise _locked()
                yield _Bad()
                return
            async with orig_wtx() as conn:
                yield conn

        monkeypatch.setattr(PF, "write_tx_conn", flaky_wtx)
        cid = await PF.save_ai_config({
            "provider_name": "openai", "model": "gpt-test",
            "plan": "payg", "base_url": "https://api.openai.com/v1"})
        assert state["n"] == 2, "锁瞬态必须整事务重放（每次尝试独立连接）"
        conn = await DB.get_conn()
        cur = await conn.execute("SELECT id FROM ai_config WHERE id=?", (cid,))
        assert await cur.fetchone(), "重放后配置必须真实落库"

    async def test_save_resets_det_dead_streak(self, db_conn, monkeypatch):
        """保存即视为用户改过配置 → 确定性死连击清零。"""
        key = "cfg:cid-reset"
        monkeypatch.setitem(PF._det_dead_streak, key, PF.DET_DEAD_STREAK_THRESHOLD)
        cid = await PF.save_ai_config({
            "id": "cid-reset",
            "provider_name": "openai", "model": "gpt-test",
            "plan": "payg", "base_url": "https://api.openai.com/v1",
            "is_active": False})
        assert key not in PF._det_dead_streak


class TestDeterministicDeadStreak:
    @pytest.fixture(autouse=True)
    def _isolate_state(self, monkeypatch):
        snap_streak = dict(PF._det_dead_streak)
        snap_rel = {k: dict(v) for k, v in PF._provider_reliability.items()}
        snap_buf = list(PF._audit_buffer)
        PF._det_dead_streak.clear()
        yield
        PF._det_dead_streak.clear()
        PF._det_dead_streak.update(snap_streak)
        PF._provider_reliability.clear()
        PF._provider_reliability.update(snap_rel)
        with PF._audit_lock:
            PF._audit_buffer.clear()
            PF._audit_buffer.extend(snap_buf)
        monkeypatch.undo()

    async def _audit_fail(self, error: str, cid: str, monkeypatch, ok: bool = False):
        monkeypatch.setattr(PF, "_flush_audit_buffer", _noop_flush)
        await PF._log_audit("volcengine", "m1", "chat", 0.1, ok,
                            error=error, config_id=cid)

    async def test_three_consecutive_404_marks_dead(self, monkeypatch):
        err = "Error code: 404 - {'error': {'code': 'UnsupportedModel'}}"
        for _ in range(3):
            await self._audit_fail(err, "cA", monkeypatch)
        assert PF._det_dead_streak["cfg:cA"] == 3
        assert PF._is_dead_provider("volcengine", "m1", "cA", "") is True, \
            "连续 3 次确定性 404 必须运行期判死（不必等统计口径攒样本）"

    async def test_success_resets_and_revives(self, monkeypatch):
        err = "Error code: 404 - UnsupportedModel"
        for _ in range(3):
            await self._audit_fail(err, "cB", monkeypatch)
        assert PF._is_dead_provider("", "", "cB", "")
        await self._audit_fail("", "cB", monkeypatch, ok=True)
        assert "cfg:cB" not in PF._det_dead_streak
        # 统计口径样本不足（ok=1, fail=3 < min samples）→ 复活
        assert PF._is_dead_provider("", "", "cB", "") is False

    async def test_other_error_breaks_streak(self, monkeypatch):
        await self._audit_fail("404 UnsupportedModel", "cC", monkeypatch)
        await self._audit_fail("404 UnsupportedModel", "cC", monkeypatch)
        await self._audit_fail("Error code: 429 - rate limit", "cC", monkeypatch)
        assert "cfg:cC" not in PF._det_dead_streak, "非连续签名必须清零（不冤枉瞬态）"
        await self._audit_fail("404 UnsupportedModel", "cC", monkeypatch)
        assert PF._det_dead_streak["cfg:cC"] == 1

    def test_signature_closed_enum(self):
        assert PF._is_deterministic_dead_error("UnsupportedModel")
        assert PF._is_deterministic_dead_error("InvalidEndpointOrModel")
        assert not PF._is_deterministic_dead_error("database is locked")
        assert not PF._is_deterministic_dead_error("")


async def _noop_flush():
    return None


# ===========================================================================
# G 组 · prompts._cache 表级水印（P1-1 重载风暴）
# ===========================================================================

@pytest.fixture
def prompt_db(tmp_path):
    import sqlite3 as _s3
    path = str(tmp_path / "prompts_audit.db")
    conn = _s3.connect(path)
    conn.execute(
        "CREATE TABLE prompt_templates (key TEXT PRIMARY KEY, content TEXT,"
        " updated_at TEXT DEFAULT (datetime('now','localtime')))")
    conn.execute("CREATE TABLE other_table (x)")
    conn.execute("INSERT INTO prompt_templates (key, content, updated_at)"
                 " VALUES ('K1', 'v1', 't0')")
    conn.commit()
    conn.close()

    snap = (PC._prompt_cache, PC._loaded_db_path, PC._loaded_db_mtime,
            PC._loaded_db_watermark, PC._watermark_probe_ts)
    PC._prompt_cache = None
    PC._loaded_db_path = None
    PC._loaded_db_mtime = None
    PC._loaded_db_watermark = None
    PC._watermark_probe_ts = 0.0
    yield path
    PC._prompt_cache, PC._loaded_db_path, PC._loaded_db_mtime = snap[0], snap[1], snap[2]
    PC._loaded_db_watermark, PC._watermark_probe_ts = snap[3], snap[4]


@pytest.fixture
def loads_counter(monkeypatch):
    """统计 _load_prompt_cache_sync 实际进入次数。"""
    counter = {"n": 0}
    orig = PC._load_prompt_cache_sync

    def counted():
        counter["n"] += 1
        orig()

    monkeypatch.setattr(PC, "_load_prompt_cache_sync", counted)
    return counter


def _bump_mtime(path, ahead=10.0):
    st = Path(path).stat()
    import os
    os.utime(path, (st.st_atime + ahead, st.st_mtime + ahead))


class TestPromptCacheWatermark:
    async def test_other_table_bump_no_reload(self, prompt_db, monkeypatch,
                                              loads_counter):
        """他表写入 bump 库 mtime + 水印未变 → **不重载**（1004 条 INFO 风暴根因）。"""
        monkeypatch.setattr(PC, "_current_db_path", lambda: prompt_db)
        PC._get_prompt_cache()
        assert loads_counter["n"] == 1
        import sqlite3
        conn = sqlite3.connect(prompt_db)
        conn.execute("INSERT INTO other_table VALUES (1)")
        conn.commit()
        conn.close()
        _bump_mtime(prompt_db)
        PC._watermark_probe_ts = 0.0  # 放开节流，强制本轮真探测
        cache = PC._get_prompt_cache()
        assert loads_counter["n"] == 1, "水印未变时绝不允许全量重载"
        assert cache["K1"] == "v1"

    async def test_real_change_reloads(self, prompt_db, monkeypatch, loads_counter):
        """prompt_templates 真变更（updated_at 变）→ 重载且新内容可见。"""
        monkeypatch.setattr(PC, "_current_db_path", lambda: prompt_db)
        PC._get_prompt_cache()
        assert loads_counter["n"] == 1
        import sqlite3
        conn = sqlite3.connect(prompt_db)
        conn.execute("UPDATE prompt_templates SET content='v2',"
                     " updated_at='t9' WHERE key='K1'")
        conn.commit()
        conn.close()
        _bump_mtime(prompt_db)
        PC._watermark_probe_ts = 0.0
        cache = PC._get_prompt_cache()
        assert loads_counter["n"] == 2, "水印变更必须重载"
        assert cache["K1"] == "v2"

    async def test_no_watermark_falls_back_legacy(self, prompt_db, monkeypatch,
                                                  loads_counter):
        """水印无法记录（None）→ 逐字回退旧行为：mtime 变新即重载。"""
        monkeypatch.setattr(PC, "_current_db_path", lambda: prompt_db)
        PC._get_prompt_cache()
        PC._loaded_db_watermark = None
        _bump_mtime(prompt_db)
        PC._get_prompt_cache()
        assert loads_counter["n"] == 2

    async def test_probe_throttle(self, prompt_db, monkeypatch, loads_counter):
        """节流窗口内的他表 mtime bump 不重复开连接探测（变更延迟有界不丢失）。"""
        monkeypatch.setattr(PC, "_current_db_path", lambda: prompt_db)
        monkeypatch.setattr(PC, "_WATERMARK_PROBE_MIN_INTERVAL", 1000.0)
        PC._get_prompt_cache()
        assert loads_counter["n"] == 1
        import sqlite3
        # ① 先做一次真实探测建立基线：他表写入 bump mtime → 探测 → 水印未变
        #    不重载，且把 _watermark_probe_ts 推到当前时刻（进入节流窗口）。
        conn = sqlite3.connect(prompt_db)
        conn.execute("INSERT INTO other_table VALUES (1)")
        conn.commit()
        conn.close()
        _bump_mtime(prompt_db)
        PC._watermark_probe_ts = 0.0  # 放开窗口，让本轮真探测
        PC._get_prompt_cache()
        assert loads_counter["n"] == 1, "水印未变只刷 mtime，不重载"
        # ② prompt_templates 真变更 + bump mtime，但仍在探测节流窗口内
        #    → 不重新探测、不重载（变更可见延迟 ≤ 窗口，非丢失）。
        conn = sqlite3.connect(prompt_db)
        conn.execute("UPDATE prompt_templates SET content='v3', updated_at='tX'"
                     " WHERE key='K1'")
        conn.commit()
        conn.close()
        _bump_mtime(prompt_db)
        PC._get_prompt_cache()
        assert loads_counter["n"] == 1, "节流窗口内不得重载"
        # ③ 窗口放开 → 下轮探测发现水印变更 → 重载（最终一致）
        monkeypatch.setattr(PC, "_WATERMARK_PROBE_MIN_INTERVAL", 0.0)
        PC._watermark_probe_ts = 0.0
        cache = PC._get_prompt_cache()
        assert loads_counter["n"] == 2
        assert cache["K1"] == "v3"

    def test_reload_prompt_cache_clears_watermark(self):
        """显式失效必须连水印一并清（否则 mtime 分支拿过期水印误判）。"""
        PC.reload_prompt_cache()
        assert PC._loaded_db_watermark is None
        assert PC._loaded_db_mtime is None and PC._loaded_db_path is None


# ===========================================================================
# H 组 · main P2-1 测试流量文件日志隔离
# ===========================================================================

class TestFileLogGuard:
    def test_pytest_process_skips(self, monkeypatch):
        assert M._should_skip_file_log() is True, "本测试进程内 pytest 必在 sys.modules"

    def test_testclient_module_skips(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "pytest", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.delenv("ALLOW_FILE_LOG_IN_TESTCLIENT", raising=False)
        monkeypatch.setitem(sys.modules, "starlette.testclient", None)
        assert M._should_skip_file_log() is True, \
            "TestClient 独立脚本（非 pytest）必须被拦——生产日志污染源"

    def test_env_escape_hatch(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "pytest", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setitem(sys.modules, "starlette.testclient", None)
        monkeypatch.setenv("ALLOW_FILE_LOG_IN_TESTCLIENT", "1")
        assert M._should_skip_file_log() is False

    def test_production_installs_and_idempotent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(M, "_should_skip_file_log", lambda: False)
        monkeypatch.setattr(M, "_LOGS_DIR", tmp_path)

        class _FakeHandler:
            def __init__(self, *a, **k):
                pass

            def setFormatter(self, f):
                pass

        monkeypatch.setattr(M, "SafeRotatingFileHandler", _FakeHandler)
        monkeypatch.setattr(M, "_file_log_handler_installed", False)
        added: list = []
        real_add = logging.Logger.addHandler

        def spy_add(self, h):
            added.append(h)
            real_add(self, h)

        monkeypatch.setattr(logging.Logger, "addHandler", spy_add)
        M._install_production_file_log_handler()
        M._install_production_file_log_handler()
        assert len(added) == 1, "双点安装必须幂等（模块级 + lifespan 只挂一次）"
        # 清理本用例真实挂到 root 的 fake handler（避免污染同 session 其它测试）
        for h in added:
            try:
                logging.getLogger().removeHandler(h)
            except Exception:
                pass


# ===========================================================================
# I 组 · 字节体检（触碰文件）
# ===========================================================================

_TOUCHED = [
    "db.py", "routers/_chart_pipeline.py", "routers/sse_handlers.py",
    "routers/export.py", "services/ai/task_registry.py",
    "services/ai/provider_factory.py", "services/ai/prompts/_cache.py",
    "main.py",
]


class TestByteHealth:
    @pytest.mark.parametrize("rel", _TOUCHED)
    def test_bytes_clean(self, rel):
        raw = (APP_DIR / rel).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} 不得带 UTF-8 BOM"
        assert raw.count("\r".encode()) == raw.count("\r\n".encode()), \
            f"{rel} 存在孤立 CR"
        assert "\ufffd".encode() not in raw, f"{rel} 存在 U+FFFD 替换符"

    def test_frontend_sse_literals_intact(self):
        """R108 事故回归锁：前端 SSE 解析字面量绝不允许再被转义管道写坏。"""
        fe = (APP_DIR.parent.parent / "frontend" / "src" / "api" / "index.ts").read_bytes()
        assert b'block.replace(/\\r\\n/g, "\\n").replace(/\\r/g, "\\n")' in fe, \
            "parseSseEventBlock 归一字面量断裂 = SSE 解析全废"
        assert b'const separator = /\\r\\n\\r\\n|\\n\\n|\\r\\r/;' in fe, \
            "SSE separator 字面量断裂 = 流式解析全废"
