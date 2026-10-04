"""正文生成深度审计（2026-09-23）回归测试

覆盖本轮依据 logs/backend.log 定位并修复的 7 项缺陷（每项均附真实日志证据）：

F1. app/db.py::retry_db_op —— 共享 DB 写重试助手（此前仅 sse_handlers 内部有）
F2. task_registry 三处裸奔 DB 写接入重试
    证据：20:48:27 finish_task 终态落库失败: database is locked（trace=9fe4498339da）
    后果：任务在 DB 里永远 running，前端轮询空转 10~20 分钟
F3. _save_task_checkpoint 接入重试
    证据：20:48:27「保存目录生成 checkpoint 失败」与终态失败同一事件
F4. 审计批量落库接入重试
    证据：20:47:20「AI 审计日志批量落库失败（丢失 1 条）: database is locked」
F5. 思考吞噬严格「翻倍一次」
    证据：20:54~20:55 单正文任务 6 条「翻倍至 3072 重试一次」全部失败
F6. render_prompt 未解析占位符检测对齐 _is_false_positive（AGENTS.md §5.3）
    证据：20:54~20:55 单任务 6 条 "unresolved placeholders: ['max']"
F7. 提取成果超预算按 `## ` 小节配额截断（不再头部优先整段丢弃）
    证据：20:54:08 差集审计「提取成果 110662 字超预算 4000 字，已截断」（3.6% 下发率）

所有反例均为「旧行为可复现的错误路径」，断言修复后不再出现。
"""
import logging
import sqlite3

import pytest


class _CapLog:
    """捕获指定 logger 消息的上下文管理器（轻量，避免引入 pytest 插件）。"""

    def __init__(self, logger_name: str):
        self.name = logger_name
        self.records: list[str] = []

    def __enter__(self):
        lg = logging.getLogger(self.name)
        self._lg, self._level = lg, lg.level
        recs = self.records

        def _emit(rec):
            recs.append(rec.getMessage())

        self._h = logging.Handler()
        self._h.emit = _emit
        lg.addHandler(self._h)
        lg.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *a):
        self._lg.removeHandler(self._h)
        self._lg.setLevel(self._level)
        return False


# ===========================================================================
# F1. retry_db_op
# ===========================================================================
class TestRetryDbOp:
    async def test_retries_on_database_locked_then_succeeds(self):
        """database is locked 瞬时错误应被重试，最终成功不抛异常。"""
        import app.db as dbmod
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        assert await dbmod.retry_db_op(flaky, max_retries=3, base_delay=0) == "ok"
        assert calls["n"] == 3, "应在重试后才成功"

    async def test_retries_on_disk_io_error(self):
        """disk I/O error（USB 外置盘抖动）同属可重试瞬态错误。"""
        import app.db as dbmod
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("disk I/O error")
            return 42

        assert await dbmod.retry_db_op(flaky, max_retries=2, base_delay=0) == 42
        assert calls["n"] == 2

    async def test_non_retryable_error_raises_immediately(self):
        """业务错误（如缺列）不得重试，原样抛出。"""
        import app.db as dbmod
        calls = {"n": 0}

        async def always_fail():
            calls["n"] += 1
            raise sqlite3.OperationalError("table ai_audit_logs has no column scene")

        with pytest.raises(sqlite3.OperationalError, match="no column scene"):
            await dbmod.retry_db_op(always_fail, max_retries=3, base_delay=0)
        assert calls["n"] == 1, "非重试错误不得重复调用"

    async def test_retry_exhausted_reraises_last_error(self):
        """重试耗尽后必须把最后的异常抛给调用方（不得静默吞掉）。"""
        import app.db as dbmod
        calls = {"n": 0}

        async def always_fail():
            calls["n"] += 1
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            await dbmod.retry_db_op(always_fail, max_retries=2, base_delay=0)
        assert calls["n"] == 3, "1 次正常 + 2 次重试"

    async def test_zero_retries_is_single_attempt(self):
        """max_retries=0 即单次尝试（供不需要重试的调用方显式关闭）。"""
        import app.db as dbmod
        calls = {"n": 0}

        async def always_fail():
            calls["n"] += 1
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError):
            await dbmod.retry_db_op(always_fail, max_retries=0, base_delay=0)
        assert calls["n"] == 1

    def test_is_retryable_db_error_classification(self):
        """分类函数：只认两种瞬态 SQLite 锁/IO 错误。"""
        from app.db import is_retryable_db_error
        assert is_retryable_db_error(sqlite3.OperationalError("database is locked"))
        assert is_retryable_db_error(sqlite3.OperationalError("disk I/O error"))
        assert not is_retryable_db_error(
            sqlite3.OperationalError("no such table: x"))


# ===========================================================================
# F2. task_registry 终态 / 进度 / 控制指令落库重试
# ===========================================================================
class TestTaskRegistryDbRetry:
    async def test_finish_task_retries_terminal_write(self, db_conn, monkeypatch):
        """终态写库遇 database is locked 必须重试——旧实现零重试，
        任务在 DB 里永远停留在 running（前端轮询空转 10~20 分钟）。"""
        import app.services.ai.task_registry as tr

        calls = {"n": 0}
        orig = tr._write_task_terminal_db

        async def flaky(task_id, status, message):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return await orig(task_id, status, message)

        monkeypatch.setattr(tr, "_write_task_terminal_db", flaky)
        await tr.register_task("content_generation", scheme_id="sc1")
        tid = list(tr._tasks)[0]
        await tr.finish_task(tid, "completed", "done")

        assert calls["n"] >= 2, "终态写库必须重试"
        cur = await db_conn.execute(
            "SELECT status FROM task_registry WHERE id=?", (tid,))
        assert (await cur.fetchone())["status"] == "completed"

    async def test_finish_task_non_retryable_still_cleans_memory(
            self, db_conn, monkeypatch):
        """业务错误不得重试，但内存态清理（G12-5 不变量）必须执行。"""
        import app.services.ai.task_registry as tr

        async def boom(task_id, status, message):
            raise sqlite3.OperationalError("no such column: status")

        async def _noop():
            return None

        monkeypatch.setattr(tr, "_write_task_terminal_db", boom)
        monkeypatch.setattr(tr, "broadcast", _noop)
        await tr.register_task("content_generation", scheme_id="sc2")
        tid = list(tr._tasks)[0]
        await tr.finish_task(tid, "failed", "x")
        assert tid not in tr._tasks, "内存态必须清理，否则 409 竞态守卫永久生效"

    async def test_update_progress_retries_and_terminal_persists(
            self, db_conn, monkeypatch):
        """进度落库（含终态 force 路径）遇锁必须重试并真正写入。"""
        import app.services.ai.task_registry as tr

        calls = {"n": 0}
        orig = tr._update_task_progress_db

        async def flaky(task_id, progress, message):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return await orig(task_id, progress, message)

        monkeypatch.setattr(tr, "_update_task_progress_db", flaky)
        await tr.register_task("content_generation", scheme_id="sc3")
        tid = list(tr._tasks)[0]
        await tr.update_progress(tid, 1.0, "完成", event="completed")

        assert calls["n"] >= 2
        cur = await db_conn.execute(
            "SELECT progress FROM task_registry WHERE id=?", (tid,))
        assert (await cur.fetchone())["progress"] == 1.0

    async def test_set_task_status_rowcount_semantics(self, db_conn):
        """控制指令落库返回受影响行数：命中 1，已是终态 0（竞态守卫生效）。"""
        import app.services.ai.task_registry as tr
        await tr.register_task("content_generation", scheme_id="sc4")
        tid = list(tr._tasks)[0]
        assert await tr._set_task_status_db(tid, "paused") == 1, "running→paused 应迁移"
        # 已是终态时 WHERE status IN ('running','paused') 不命中 → 0
        await db_conn.execute("UPDATE task_registry SET status='completed' WHERE id=?",
                              (tid,))
        assert await tr._set_task_status_db(tid, "paused") == 0, \
            "已是终态不得被覆盖（竞态守卫）"


# ===========================================================================
# F3. checkpoint 落库重试
# ===========================================================================
class TestCheckpointDbRetry:
    async def test_save_task_checkpoint_retries(self, db_conn, monkeypatch):
        """checkpoint 是断线重挂的唯一数据源，写失败必须重试。"""
        import app.routers.sse_handlers as sh

        calls = {"n": 0}
        orig = sh._write_task_checkpoint_db

        async def flaky(task_id, kind, payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return await orig(task_id, kind, payload)

        monkeypatch.setattr(sh, "_write_task_checkpoint_db", flaky)
        await self._ensure_task(db_conn, "t-1")
        await sh._save_task_checkpoint("t-1", "content_result",
                                       {"event": "stopped"})

        assert calls["n"] >= 2
        cur = await db_conn.execute(
            "SELECT checkpoint_json FROM task_registry WHERE id='t-1'")
        row = await cur.fetchone()
        assert row and row["checkpoint_json"], "checkpoint 必须真正落库"

    async def test_content_checkpoint_roundtrip(self, db_conn):
        """正文成果清单写入后可被读回（断线重挂链路），kind 不匹配返回 None。"""
        import app.routers.sse_handlers as sh
        await self._ensure_task(db_conn, "t-2")
        payload = {"event": "stopped", "sections_total": 3, "sections_ok": 2}
        await sh._save_content_checkpoint("t-2", payload)
        got = await sh._load_task_checkpoint("t-2", "content_result")
        assert got is not None and got["sections_ok"] == 2
        assert await sh._load_task_checkpoint("t-2", "outline_result") is None, \
            "kind 不匹配必须返回 None（防串台）"

    async def _ensure_task(self, conn, task_id: str) -> None:
        """checkpoint 走 UPDATE，任务行必须已存在（正常链路由 register_task 建）。"""
        await conn.execute(
            "INSERT OR IGNORE INTO task_registry (id, task_type, status) "
            "VALUES (?, 'content_generation', 'running')", (task_id,))
        await conn.commit()


# ===========================================================================
# F4. 审计批量落库重试
# ===========================================================================
class TestAuditFlushDbRetry:
    async def test_flush_retries_on_lock(self, db_conn, monkeypatch):
        """审计批量落库遇 database is locked 必须重试——旧实现整批永久丢失。"""
        import app.services.ai.provider_factory as pf

        pf._audit_buffer.append(
            ("a1", "deepseek", "m", "chat", 10, 20, 0, 1.0, 1, "", "content_draft"))
        calls = {"n": 0}
        orig = pf._flush_audit_rows_once

        async def flaky(rows):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return await orig(rows)

        monkeypatch.setattr(pf, "_flush_audit_rows_once", flaky)
        await pf._flush_audit_buffer()

        assert calls["n"] >= 2, "审计落库必须重试"
        cur = await db_conn.execute("SELECT COUNT(*) AS c FROM ai_audit_logs")
        assert (await cur.fetchone())["c"] == 1, "审计记录不得因重试丢失"

    async def test_flush_legacy_fallback_not_retried_as_transient(self, db_conn,
                                                                 monkeypatch):
        """业务错误（缺 scene 列）走 legacy 降级写入，不被误当瞬态锁错误重试整批。"""
        import app.services.ai.provider_factory as pf

        pf._audit_buffer.append(
            ("a2", "deepseek", "m", "chat", 10, 20, 0, 1.0, 1, "", "outline_draft"))
        calls = {"n": 0}

        async def legacy_only(rows):
            calls["n"] += 1
            async with pf.write_tx_conn() as conn:
                await conn.executemany(pf._AUDIT_INSERT_LEGACY,
                                        [r[:10] for r in rows])
                await conn.commit()

        monkeypatch.setattr(pf, "_flush_audit_rows_once", legacy_only)
        pf._audit_legacy_warned = False
        await pf._flush_audit_buffer()

        assert calls["n"] == 1
        cur = await db_conn.execute("SELECT COUNT(*) AS c FROM ai_audit_logs")


# ===========================================================================
# F5. 思考吞噬严格翻倍一次
# ===========================================================================
class _FakeProvider:
    name = "fake"

    def __init__(self, err):
        self.err = err

    async def chat(self, *a, **k):
        raise self.err


def _thinking_err(mt: int = 1536) -> RuntimeError:
    return RuntimeError(
        f"推理模型把 max_tokens({mt}) 全部消耗在思考过程"
        "（finish_reason=length），正文为空：请调大 max_tokens")


class TestThinkingExhaustedOnce:
    async def test_no_double_doubling_below_half_cap(self, monkeypatch):
        """反例：base_mt=1536、cap=4096 时旧实现会连续翻倍两次
        （1536→3072→4096），每候选多烧一次完整网络往返。修复后严格一次。"""
        import app.services.ai.provider_factory as pf
        from app.config import settings

        monkeypatch.setattr(settings, "ai_retry_on_thinking_exhausted", True)
        monkeypatch.setattr(settings, "ai_reasoning_max_tokens", 4096)

        built: list[int] = []

        def _build(*a, **k):
            built.append(k.get("max_tokens"))
            return _FakeProvider(_thinking_err(1536))

        monkeypatch.setattr(pf, "_build_provider", _build)
        monkeypatch.setattr(pf, "extract_usage", lambda *a, **k: {})

        await pf._attempt_candidate(
            {"provider_name": "fake", "api_key": "k", "base_url": "", "model": "m",
             "max_tokens": 1536, "temperature": 0.7},
            [], temperature=0.7, json_mode=False, max_tokens=1536,
            req_timeout=10, scene="content_draft")

        assert built == [1536, 3072], f"应严格翻倍一次，实际调用链={built}"

    async def test_retry_disabled_is_legacy(self, monkeypatch):
        """ai_retry_on_thinking_exhausted=False → 不重试（旧行为）。"""
        import app.services.ai.provider_factory as pf
        from app.config import settings

        monkeypatch.setattr(settings, "ai_retry_on_thinking_exhausted", False)
        built: list[int] = []

        def _build(*a, **k):
            built.append(k.get("max_tokens"))
            return _FakeProvider(_thinking_err(1536))

        monkeypatch.setattr(pf, "_build_provider", _build)

        await pf._attempt_candidate(
            {"provider_name": "fake", "api_key": "k", "base_url": "", "model": "m",
             "max_tokens": 1536, "temperature": 0.7},
            [], temperature=0.7, json_mode=False, max_tokens=1536,
            req_timeout=10, scene="content_draft")

        assert built == [1536], "关闭重试时只应调用一次"

    async def test_recovers_after_single_doubling(self, monkeypatch):
        """翻倍一次后成功 → 正常返回正文（原 O3 收益不丢）。"""
        import app.services.ai.provider_factory as pf
        from app.config import settings

        monkeypatch.setattr(settings, "ai_retry_on_thinking_exhausted", True)
        monkeypatch.setattr(settings, "ai_reasoning_max_tokens", 4096)

        calls = {"n": 0}

        class _Recover:
            name = "fake"

            async def chat(self, *a, **k):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise _thinking_err(1536)
                return "正常正文"

        built: list[int] = []

        def _build(*a, **k):
            built.append(k.get("max_tokens"))
            return _Recover()

        monkeypatch.setattr(pf, "_build_provider", _build)
        monkeypatch.setattr(pf, "extract_usage",
                            lambda *a, **k: {"prompt_tokens": 1,
                                            "completion_tokens": 2,
                                            "cached_tokens": 0})

        result, err = await pf._attempt_candidate(
            {"provider_name": "fake", "api_key": "k", "base_url": "", "model": "m",
             "max_tokens": 1536, "temperature": 0.7},
            [], temperature=0.7, json_mode=False, max_tokens=1536,
            req_timeout=10, scene="content_draft")

        assert err is None and result == "正常正文"
        assert built == [1536, 3072], "应翻倍一次后成功"


# ===========================================================================
# F8. 提示词缺失变量校验的"空 kwargs 误报"（_cache 层）
# ===========================================================================
#
# 根因：_cache.get_prompt() 把缺失变量校验写在 `if kwargs:` 之外，导致任何
# "取原始模板"的合法调用（无 kwargs）都会把**全部**变量当成缺失上报。
# 真实日志里因此每次正文生成都刷出：
#   2026-09-23 20:33:57,539 _cache: Prompt 'content_generation_system' has
#       missing variables: ['scheme_name', 'scheme_type',
#                           'section_number', 'standards_text']
# 而 scheme_name / scheme_type 实际是传了的。"没传参数"根本无从判断"缺了什么"。
# 同类：JSON 示例 {"max": 60000} 在 render_prompt 里已按 R6 过滤，
# 但 _cache 的告警路径漏过滤，仍会漏进 missing 列表。

import pytest


class _WarnCapture(logging.Handler):
    """收集 _cache 模块的告警，供断言"未误报"。"""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record):  # noqa: D102
        self.lines.append(record.getMessage())


class TestCacheMissingVariablesNoFalsePositive:
    """get_prompt / get_prompt_with_validation 的缺失变量告警口径。"""

    def test_get_prompt_without_kwargs_logs_nothing(self):
        """无 kwargs 取原始模板是合法用法，不得报"缺变量"。"""
        import app.services.ai.prompts._cache as c
        cap = _WarnCapture()
        c.logger.addHandler(cap)
        try:
            r = c.get_prompt("content_generation_system")
            assert r, "无 kwargs 应返回非空原始模板"
        finally:
            c.logger.removeHandler(cap)
        bogus = [l for l in cap.lines if "missing variables" in l]
        assert bogus == [], f"空 kwargs 不应报缺失变量，实际: {bogus}"

    def test_get_prompt_with_all_kwargs_logs_nothing(self):
        """传全变量时不得误报；且 JSON 示例 {"max": ...} 不计入 missing。"""
        import app.services.ai.prompts._cache as c
        from app.services.ai.prompts._registry import (
            PROMPT_VARIABLE_CONTRACTS,
            extract_user_variables,
        )
        cap = _WarnCapture()
        c.logger.addHandler(cap)
        kwargs = {
            name: f"测试-{name}"
            for name in PROMPT_VARIABLE_CONTRACTS["content_generation_system"]
        }
        # 防漂移：契约变量必须覆盖模板真实业务变量；JSON 示例误报变量不在契约内。
        assert set(kwargs).issubset(set(extract_user_variables(
            c.get_prompt("content_generation_system")
        )))
        try:
            r = c.get_prompt("content_generation_system", **kwargs)
            assert r
        finally:
            c.logger.removeHandler(cap)
        bogus = [l for l in cap.lines if "missing variables" in l]
        assert bogus == [], f"传全变量不应报缺失，实际: {bogus}"

    def test_get_prompt_empty_kwargs_suppresses_warning_only(self):
        """校验结果仍如实返回缺失（契约不变），只是不再刷日志告警。"""
        import app.services.ai.prompts._cache as c
        cap = _WarnCapture()
        c.logger.addHandler(cap)
        try:
            _, missing = c.get_prompt_with_validation("outline_review_system")
        finally:
            c.logger.removeHandler(cap)
        assert missing, "校验 API 空 kwargs 仍应如实报出全部缺失变量"
        assert not [l for l in cap.lines if "missing variables" in l], \
            f"但不得刷日志告警（噪音修复点），实际: {cap.lines}"

    def test_get_prompt_with_validation_still_reports_real_gap(self):
        """只要有传参，真实缺口仍要报出来（修 bug 不得吞掉真信号）。

        实现口径：kwargs 为空时"缺失"无从谈起（取原始模板是合法调用，
        空 → [] 已由上一个用例覆盖）；这里传一个真实存在的变量触发校验，
        其余未传的模板变量必须全部报缺。
        """
        import app.services.ai.prompts._cache as c
        _, missing = c.get_prompt_with_validation(
            "outline_review_system", scheme_name="深基坑支护")
        assert "scheme_name" not in missing, \
            f"已传入的变量不应报缺，实际: {missing}"
        assert "outline_json" in missing, \
            f"传了部分参数时剩余真实缺口仍应报缺失，实际: {missing}"

    def test_get_prompt_with_validation_partial_passes_partial_missing(self):
        """传了一个不相关变量时，真实缺口仍要报出来。"""
        import app.services.ai.prompts._cache as c
        _, missing = c.get_prompt_with_validation(
            "outline_review_system", unrelated="无关参数")
        assert "outline_json" in missing, \
            f"传了无关参数但没传 outline_json 时应报缺失，实际: {missing}"

    def test_residual_placeholder_warning_still_fires(self):
        """残留占位符告警路径不受本次改动影响。"""
        import app.services.ai.prompts._cache as c
        cap = _WarnCapture()
        c.logger.addHandler(cap)
        try:
            # 传全变量后不应有任何缺失 / 残留
            prompt, missing = c.get_prompt_with_validation(
                "outline_review_system",
                construction_scope="基坑 10m",
                is_dangerous="是",
                outline_json="[]",
                project_facts="深 10m",
                scheme_name="深基坑支护",
                scheme_type="基坑支护",
            )
            assert missing == []
            assert "{outline_json}" not in prompt
        finally:
            c.logger.removeHandler(cap)
        residual = [l for l in cap.lines if "residual placeholders" in l]
        assert residual == [], f"传全变量后不应有残留占位符: {residual}"


# ===========================================================================
# F6. render_prompt 误报过滤对齐（AGENTS.md §5.3 遗留项）
# ===========================================================================
class TestRenderPromptFalsePositive:
    def test_json_example_max_not_reported(self):
        """`{max}` 来自 JSON 示例，不应再触发「未解析占位符」告警。"""
        import app.services.ai.prompts._registry as reg

        tmpl = '输出示例：{"max": 200} 任务清单。' + "正文内容" * 60
        with _CapLog("app.services.ai.prompts._registry") as cm:
            out = reg.render_prompt(tmpl)
        assert not any("placeholders" in r and "max" in r for r in cm.records), \
            f"JSON 示例误报未过滤：{cm.records}"
        assert out == tmpl, "无变量可替换时模板应原样返回"

    def test_real_missing_variable_still_reported(self):
        """真缺业务变量仍必须告警——过滤不能掩盖真实缺陷。"""
        import app.services.ai.prompts._registry as reg

        tmpl = "方案名称：{scheme_name}。" + "正文内容" * 60
        with _CapLog("app.services.ai.prompts._registry") as cm:
            reg.render_prompt(tmpl)
        assert any("scheme_name" in r for r in cm.records), \
            f"真实缺失变量被误报过滤吞掉：{cm.records}"

    def test_shared_prefix_exempt_unchanged(self):
        """SHARED_ 前缀变量的既有豁免行为不受本次改动影响。"""
        import app.services.ai.prompts._registry as reg

        tmpl = "共享上下文：{SHARED_CTX}。" + "正文内容" * 60
        with _CapLog("app.services.ai.prompts._registry") as cm:
            reg.render_prompt(tmpl)
        assert not any("SHARED_CTX" in r for r in cm.records), \
            f"SHARED_ 前缀变量应被豁免：{cm.records}"


# ===========================================================================
# F7. 提取成果按 `## ` 小节配额截断
# ===========================================================================
class TestBudgetedTruncation:
    def _mk_sections(self, n=18, size=6000):
        """构造与 format_downstream_context 同构的多小节文本。"""
        parts = ["# 提取项目结果（自动提取，供后续步骤参考）", ""]
        for i in range(1, n + 1):
            parts.append(f"## 第{i}项：项目信息与参数")
            parts.append((f"第{i}项内容细节。" * (size // 12))[:size])
            parts.append("")
        return "\n".join(parts)

    def test_split_md_sections(self):
        import app.routers.sse_handlers as sh
        secs = sh._split_md_sections("# 大标题\n\n## A\n正文A\n\n## B\n正文B")
        assert len(secs) == 3
        assert secs[0][0] == "" and secs[0][1].strip() == "# 大标题"
        assert secs[1][0] == "## A" and "正文A" in secs[1][1]
        assert secs[2][0] == "## B" and "正文B" in secs[2][1]

    def test_split_ignores_deeper_headings(self):
        """`### ` 及以下层级不得被误切为独立小节。"""
        import app.routers.sse_handlers as sh
        assert len(sh._split_md_sections("## 一级\n### 二级\n正文")) == 2

    def test_no_section_is_single_block(self):
        """无 `## ` 节时返回单块（调用方按整体处理）。"""
        import app.routers.sse_handlers as sh
        assert sh._split_md_sections("只有前言") == [("", "只有前言")]

    def test_allocates_proportional_with_floor_and_cap(self):
        import app.routers.sse_handlers as sh
        allocs = sh._allocate_char_budgets([10000, 1000, 1000, 1000, 1000], 4000)
        assert len(allocs) == 5
        assert sum(allocs) <= 4000, f"总预算不得超出：{sum(allocs)}"
        assert min(allocs) >= 150, f"每项至少有保底配额：{allocs}"
        assert allocs[0] <= int(4000 * 0.35) + 1, "巨节不得吃掉全部预算"

    def test_fits_budget_is_exact(self):
        """未超预算时逐项返回原长度（等比即恒等）。"""
        import app.routers.sse_handlers as sh
        assert sh._allocate_char_budgets([1000, 1000, 1000], 3000) == \
            [1000, 1000, 1000]

    def test_empty_and_zero_budget(self):
        import app.routers.sse_handlers as sh
        assert sh._allocate_char_budgets([], 4000) == []
        assert sh._allocate_char_budgets([100], 0) == [0]

    def test_all_sections_survive_budget_cut(self):
        """核心反例：旧实现头部优先切片会让尾部 15 项整段消失。"""
        import app.routers.sse_handlers as sh
        out, rep = sh._budgeted_truncate_sections(self._mk_sections(18, 6000), 4000)
        for i in range(1, 19):
            assert f"## 第{i}项" in out, f"第{i}项标题丢失"
        assert rep["sections"] == 19  # 前言 + 18 小节
        assert rep["truncated_sections"] > 0
        assert len(out) <= 4000 + 18 * 60, f"输出明显超预算：{len(out)}"

    def test_under_budget_untouched(self):
        """未超预算时不改动内容（向后兼容）。"""
        import app.routers.sse_handlers as sh
        out, rep = sh._budgeted_truncate_sections("## A\n短内容", 1000)
        assert rep["truncated_sections"] == 0
        assert "短内容" in out