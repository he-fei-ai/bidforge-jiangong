"""正文生成模块 · 深度审查回归测试（2026-09-16 第二轮）

本轮修复项与对应测试：

| 编号 | 修复 | 测试类 |
|---|---|---|
| E-1 | 正文终态顺序颠倒：`yield completed` 后才 `finish_task` ⇒ 已成功任务被记为 stopped | TestTerminalOrdering |
| E-2 | 正文生成成果清单未落 checkpoint ⇒ 断线/刷新后无法定位失败章节 | TestContentCheckpoint |
| E-3 | 进度落库与章节写事务共用连接且未互斥 ⇒ 旁路 `_persist_section` 事务守卫 | TestProgressWriteLock |
| E-4 | 续写总超时 360s ≠ 首稿 660s ⇒ 降级链在续写路径被截断 | TestTimeoutConsistency |
| E-5 | 超字数章节「只扩不缩」：新增生成后自动压缩（可选、默认关） | TestAutoShrink |
| E-6 | `_render_facts_text` 组标题不过预算 ⇒ 末尾切片把组标题截成半截 | TestFactsBudget |
"""
import asyncio
import inspect
import json
import re
import time
from pathlib import Path

import pytest

from app.routers import sse_handlers as sh
from app.services import content_shrink as cs



# ============================================================
# E-1：正文终态收尾顺序（checkpoint → finish_task → yield）
# ============================================================
class TestTerminalOrdering:
    """源码级不变量（与 tests/test_content_facts.py 同一风格）。

    为什么用源码断言：`event_stream` 需要 register_task/DB/AI 才能跑起来，
    而这条不变量恰恰是"顺序"问题 —— 顺序错了只会在**客户端提前断开**这一
    时序下暴露（前端收到 completed 立即 break → 生成器 aclose → finally 兜底
    把已成功任务写成 stopped）。顺序是结构性约束，用源码位置断言最可靠。
    """

    def _src(self) -> str:
        return inspect.getsource(sh.generate_content)

    def test_completed_writes_checkpoint_before_finish_and_yield(self):
        src = self._src()
        # ✅ G12-3（2026-09-20）：checkpoint 载荷已收口到 _content_ckpt_payload 单一
        # 拼装点（旧锚点是 `_save_content_checkpoint(task_id, {` 的手拼形态），
        # 顺序不变量保持：checkpoint → finish_task → yield。
        i_ckpt = src.index('_content_ckpt_payload("completed"')
        i_finish = src.index('await finish_task(task_id, "completed", _done_msg)')
        i_yield = src.index("yield f\"data: {json.dumps(completed_payload",
                            src.index("completed_payload = {"))
        assert i_ckpt < i_finish < i_yield, "顺序必须为 checkpoint → finish_task → yield"

    def test_stopped_writes_checkpoint_before_finish_and_yield(self):
        src = self._src()
        # ✅ G12-3：同上，锚点改为 _content_ckpt_payload("stopped", "用户已停止")
        i_stop = src.index('_content_ckpt_payload("stopped", "用户已停止")')
        i_finish = src.index('await finish_task(task_id, "stopped", "用户已停止")')
        # ✅ 锚点改空白容错（2026-09-24）：sse_handlers 重构时 dict 键值加了空格
        # （'progress': stop_progress），硬编码无空格串会误报；顺序语义不变。
        m_progress = re.search(r"'progress':\s*stop_progress", src)
        assert m_progress is not None, "stopped 事件载荷必须携带 stop_progress"
        i_yield = m_progress.start()
        assert i_stop < i_finish < i_yield


    def test_cancelled_writes_checkpoint_before_finish_and_yield(self):
        """✅ BUG 修复（2026-09-21）：except CancelledError 路径也必须保存 checkpoint。

        旧实现直接 finish_task + yield，不写 checkpoint —— 服务端取消时已生成
        章节的成果清单永久丢失。现与停止/断线路径同口径。
        """
        src = self._src()
        i_ckpt = src.index('_content_ckpt_payload("stopped", "任务已取消")')
        i_finish = src.index('await finish_task(task_id, "stopped", "任务已取消")')
        i_yield = src.index("'event':'stopped','task_id':task_id", i_ckpt)
        assert i_ckpt < i_finish < i_yield, "取消路径顺序必须为 checkpoint → finish_task → yield"

    def test_cancelled_checkpoint_uses_stopped_event(self):
        """取消路径的 checkpoint 事件类型必须是 'stopped'（而非 'failed'）。"""
        src = self._src()
        i = src.index('_content_ckpt_payload("stopped", "任务已取消")')
        block = src[i:i + 200]
        assert '"failed"' not in block

    def test_checkpoint_schema_matches_generated_ts(self):
        """✅ 代码生成契约：后端白名单与前端 checkpointSchema.ts 必须一致。

        防止手工漂移（G13-5 / G12-6 同根因）：白名单变更但未重新生成 TS 文件。
        """
        from app.services.checkpoint_schema import (
            CHECKPOINT_KINDS, generate_typescript_schema,
        )
        ts = generate_typescript_schema()
        ts_path = (Path(__file__).resolve().parents[2] / "frontend"
                   / "src" / "types" / "checkpointSchema.ts")
        if ts_path.exists():
            generated = ts_path.read_text(encoding="utf-8")
            # 跳过文件头注释行，比较实际 interface 内容
            body = "\n".join(ts.split("\n", 3)[3:])
            gen_body = "\n".join(generated.split("\n", 3)[3:])
            assert gen_body.strip() == body.strip(), (
                "checkpointSchema.ts 已过期，请重新运行 "
                "python tools/generate_checkpoint_schema.py")
        # 白名单结构完整性
        for task_type, (kind, fields) in CHECKPOINT_KINDS.items():
            assert kind in ts, f"{kind} interface 缺失"
            for f in fields:
                assert f"{f}?" in ts, f"{kind}.{f} 字段缺失"

    def test_no_finish_task_after_completed_yield(self):
        """completed 事件之后不得再出现 finish_task（收尾必须在 yield 之前完成）。"""
        src = self._src()
        tail = src[src.index("yield f\"data: {json.dumps(completed_payload"):]
        assert "finish_task" not in tail.split("except asyncio.CancelledError")[0]


# ============================================================
# E-2：正文成果 checkpoint
# ============================================================
class TestContentCheckpoint:
    @pytest.fixture
    async def ctx(self, tmp_path):
        """临时 DB 上下文（针对真实 get_conn 路径的 checkpoint 往返测试）。

        ✅ G12-7（2026-09-20）：必须**恢复** DB_PATH 并清空模块级连接 —— 旧实现
        只改不还，测试结束后 `app.db.DB_PATH` 永久指向 pytest 的临时目录，
        后续任何走真实 `get_conn` 的测试都会连到「已废弃的临时库」，
        造成顺序依赖的间歇性失败（单文件跑过、全量跑挂）。
        """
        import app.db as _appdb
        _prev_path = _appdb.DB_PATH
        _appdb.DB_PATH = tmp_path / "content.sqlite"
        await _appdb.init_db()
        try:
            yield _appdb
        finally:
            await _appdb.close_db()
            _appdb.DB_PATH = _prev_path

    async def test_roundtrip_and_kind_isolation(self, ctx):
        from app.services.ai.task_registry import register_task, finish_task
        tid = await register_task("content_generation", "p", "s")
        payload = {
            "event": "stopped", "message": "用户已停止",
            "done": 3, "total": 10, "failed_sections": [
                {"section_id": "sec-1", "title": "施工准备", "reason": "AI 超时"}],
            "words": 4200,
        }
        await sh._save_content_checkpoint(tid, payload)
        loaded = await sh._load_task_checkpoint(tid, "content_result")
        assert loaded["kind"] == "content_result"
        assert loaded["done"] == 3 and loaded["total"] == 10
        assert loaded["failed_sections"][0]["reason"] == "AI 超时"
        # kind 不匹配时不得误读（目录成果 vs 正文成果互不串台）
        assert await sh._load_task_checkpoint(tid, "outline_result") is None
        assert await sh._load_outline_checkpoint(tid) is None
        await finish_task(tid, "stopped")

    async def test_outline_loader_still_works(self, ctx):
        """回归：泛化 checkpoint 读取后，目录成果读取语义不变。"""
        from app.services.ai.task_registry import register_task, finish_task
        tid = await register_task("outline_generation", "p", "s")
        await sh._save_outline_checkpoint(tid, {"event": "completed",
                                                "outline": [{"title": "工程概况"}]})
        ckpt = await sh._load_outline_checkpoint(tid)
        assert ckpt["kind"] == "outline_result"
        assert ckpt["outline"][0]["title"] == "工程概况"
        assert await sh._load_task_checkpoint(tid, "content_result") is None
        await finish_task(tid, "completed")

    def test_task_status_whitelist_per_task_type(self):
        """task_status 只回传白名单字段（不泄漏 checkpoint 内部字段）。"""
        row = {"task_type": "content_generation",
               "checkpoint_json": json.dumps({
                   "kind": "content_result", "event": "completed",
                   "failed_sections": [{"section_id": "x"}], "internal_secret": 1,
                   "words": 10, "total": 3, "done": 3, "failed_count": 1})}
        result: dict = {}
        sh._attach_checkpoint_result(row, result)
        assert set(result["content_result"]) <= {
            "event", "message", "done", "total", "failed_count",
            "failed_sections", "words", "word_count"}
        assert "internal_secret" not in result["content_result"]

    def test_task_status_ignores_kind_mismatch(self):
        row = {"task_type": "content_generation",
               "checkpoint_json": json.dumps({"kind": "outline_result",
                                              "outline": [{"title": "x"}]})}
        result: dict = {}
        sh._attach_checkpoint_result(row, result)
        assert result == {}

    def test_task_status_no_checkpoint_and_bad_json_are_safe(self):
        for raw in (None, "", "not-json", "[]"):
            result: dict = {}
            sh._attach_checkpoint_result(
                {"task_type": "content_generation", "checkpoint_json": raw}, result)
            assert result == {}


# ============================================================
# E-3：进度落库与章节写事务互斥
# ============================================================
class TestProgressWriteLock:
    async def test_update_waits_while_write_lock_held(self, monkeypatch):
        """✅ E-3：写锁被占用（章节事务进行中）时，进度落库必须等待。

        否则进度 commit 会把另一个章节的半途事务（已 DELETE/INSERT 图表登记、
        尚未 UPDATE 正文）顺带提交 —— rollback 失效，正文与图表清单不一致。
        """
        calls: list[float] = []

        async def fake_update(task_id, progress, message="", event="progress", **kw):
            calls.append(progress)

        monkeypatch.setattr(sh, "update_progress", fake_update)
        lock = asyncio.Lock()
        update = sh._locked_progress_updater(lock, "t1")

        gate = asyncio.Event()

        async def holder():
            async with lock:
                await gate.wait()

        h = asyncio.create_task(holder())
        await asyncio.sleep(0.01)          # 让 holder 先拿到锁
        pending = asyncio.create_task(update(0.5, "msg"))
        await asyncio.sleep(0.05)
        assert calls == [], "写锁被占用时不得落库进度"
        gate.set()
        await pending
        await h
        assert calls == [0.5]

    async def test_passes_progress_message_and_event(self, monkeypatch):
        seen = {}

        async def fake_update(task_id, progress, message="", event="progress", **kw):
            seen.update(task_id=task_id, progress=progress, message=message, event=event)

        monkeypatch.setattr(sh, "update_progress", fake_update)
        update = sh._locked_progress_updater(asyncio.Lock(), "task-x")
        await update(0.75, "第 2/5 章完成", event="progress")
        assert seen == {"task_id": "task-x", "progress": 0.75,
                        "message": "第 2/5 章完成", "event": "progress"}

    def test_content_path_uses_locked_updater(self):
        """源码级护栏：正文生成链路的进度写入不得绕过写锁。"""
        src = inspect.getsource(sh.generate_content)
        assert "_update_progress_safe = _locked_progress_updater(_db_write_lock, task_id)" in src
        # 章节阶段（_db_write_lock 之后）不得再直接调用 update_progress
        phase_src = src[src.index("_db_write_lock = asyncio.Lock()"):]
        assert "await update_progress(task_id" not in phase_src


# ============================================================
# E-4：超时口径一致
# ============================================================
class TestTimeoutConsistency:
    def test_total_timeout_covers_single_request(self):
        assert sh.CONTENT_TOTAL_TIMEOUT > sh.CONTENT_REQUEST_TIMEOUT

    def test_continuation_uses_same_total_timeout(self):
        """✅ E-4：续写与首稿共用 CONTENT_TOTAL_TIMEOUT（旧实现续写只有 +60s）。"""
        src = inspect.getsource(sh.generate_content)
        assert "CONTENT_REQUEST_TIMEOUT + 60" not in src
        assert src.count("timeout=CONTENT_TOTAL_TIMEOUT") >= 2


# ============================================================
# E-5：超字数自动压缩（门槛 + 轮次服务）
# ============================================================
class TestAutoShrinkGate:
    def test_only_over_threshold_triggers(self):
        """门槛与 word_status='over'（>130%）同口径。"""
        assert sh._should_auto_shrink("字" * 1301, 1000) is True
        assert sh._should_auto_shrink("字" * 1300, 1000) is False
        assert sh._should_auto_shrink("字" * 100, 1000) is False

    def test_chart_code_blocks_are_not_counted(self):
        """✅ 图表代码块不计入字数（否则一张图就能把阈值顶过去、触发无谓调用）。"""
        content = ("正文" * 20) + "\n```mermaid\ngraph TD;\n" + ("A" * 5000) + "\n```\n"
        assert sh._should_auto_shrink(content, 100) is False

    def test_dirty_input_is_safe(self):
        assert sh._should_auto_shrink(None, 1000) is False
        assert sh._should_auto_shrink("x", 0) is False
        assert sh._should_auto_shrink("x", "bad") is False


def _long_content() -> str:
    return ("甲" * 400) + "\n" + ("乙" * 400)


def _replace_ops(target: str, repl: str) -> str:
    return json.dumps({"operations": [
        {"operation": "replace", "target_text": target, "content": repl}]},
        ensure_ascii=False)


class TestShrinkRoundsService:
    """`content_shrink.shrink_content_rounds` —— 自动/手动压缩共用的轮次主循环。"""

    async def test_applies_valid_operations(self):
        content = _long_content()

        async def call_ai(sys_prompt, user_payload):
            # 按当前正文分轮给出不同目标，模拟真实的"逐轮压缩"
            if "乙" * 400 in user_payload:
                return _replace_ops("乙" * 400, "乙" * 10)
            return _replace_ops("甲" * 400, "甲" * 100)

        out = await cs.shrink_content_rounds(
            content, word_budget=200,
            prompt_factory=lambda r, wc: "P", call_ai=call_ai)
        assert out["applied"] is True
        assert out["before"] == 801
        assert out["word_count"] == 111          # 100 甲 + 换行 + 10 乙
        assert out["rounds_used"] == 2
        assert out["stop_reason"] == "已收敛至目标区间"
        assert "甲" * 400 not in out["content"]

    async def test_no_progress_stops_early(self):
        async def call_ai(sys_prompt, user_payload):
            return _replace_ops("甲" * 400, "丙" * 400)   # 等长替换 → 无进展

        out = await cs.shrink_content_rounds(
            _long_content(), word_budget=200,
            prompt_factory=lambda r, wc: "P", call_ai=call_ai)
        assert out["applied"] is False
        assert out["stop_reason"] == "无进展（压缩未减字）"
        assert out["rounds_used"] == 1

    async def test_ai_failure_keeps_original(self):
        async def call_ai(sys_prompt, user_payload):
            raise RuntimeError("模型超时")

        out = await cs.shrink_content_rounds(
            _long_content(), word_budget=200,
            prompt_factory=lambda r, wc: "P", call_ai=call_ai)
        assert out["applied"] is False
        assert out["content"] == _long_content()
        assert "AI 调用失败" in out["stop_reason"]
        assert out["rounds_used"] == 0


    async def test_prompt_factory_failure_stops_without_ai_call(self):
        called = []

        async def call_ai(sys_prompt, user_payload):
            called.append(1)
            return "{}"

        def boom(r, wc):
            raise RuntimeError("占位符缺失")

        out = await cs.shrink_content_rounds(
            _long_content(), word_budget=200,
            prompt_factory=boom, call_ai=call_ai)
        assert called == []                       # 提示词都渲染不出来，不得消耗 AI 调用
        assert "提示词渲染失败" in out["stop_reason"]

    async def test_rounds_capped_and_callback_invoked(self):
        seen = []

        async def call_ai(sys_prompt, user_payload):
            # 每轮只减 10 字（长效目标），验证轮次上限与逐轮回调
            m = re.search(r"甲{300,}", user_payload)
            target = m.group(0)
            return _replace_ops(target, target[:-10])

        async def on_round(round_no, cur_wc):
            seen.append((round_no, cur_wc))

        out = await cs.shrink_content_rounds(
            _long_content(), word_budget=100, max_rounds=2,
            prompt_factory=lambda r, wc: "P", call_ai=call_ai, on_round=on_round)
        assert out["rounds_used"] == 2
        assert out["stop_reason"] == "已达 2 轮上限"
        assert [r for r, _ in seen] == [1, 2]
        assert seen[0][1] == 801 and seen[1][1] == 791

    async def test_callback_exception_is_ignored(self):
        async def call_ai(sys_prompt, user_payload):
            return _replace_ops("甲" * 400, "甲" * 10)

        async def bad_cb(round_no, cur_wc):
            raise RuntimeError("上报炸了")

        out = await cs.shrink_content_rounds(
            _long_content(), word_budget=200, max_rounds=1,
            prompt_factory=lambda r, wc: "P", call_ai=call_ai, on_round=bad_cb)
        assert out["applied"] is True          # 上报异常不影响压缩

    async def test_empty_content_is_noop(self):
        async def call_ai(sys_prompt, user_payload):
            raise AssertionError("空正文不应调用 AI")

        for blank in ("", "   ", "\n\n"):
            out = await cs.shrink_content_rounds(
                blank, word_budget=200,
                prompt_factory=lambda r, wc: "P", call_ai=call_ai)
            assert out["applied"] is False
            assert out["stop_reason"] == "章节正文为空"

    def test_sections_endpoint_reuses_the_service(self):
        """压缩参数与主循环单一来源：路由不得再抄一份轮次逻辑。"""
        from app.routers import sections
        src = inspect.getsource(sections.shrink_section)
        assert "shrink_content_rounds(" in src
        assert "for round_no in range" not in src
        assert sections.SHRINK_MAX_ROUNDS == cs.SHRINK_MAX_ROUNDS
        assert sections.SHRINK_SETTLE_RATIO == cs.SHRINK_SETTLE_RATIO


# ============================================================
# E-6：事实文本预算
# ============================================================
class TestFactsBudget:
    def test_group_header_never_cut_in_half(self):
        rows = [("甲组", "t", "乙" * 100), ("乙组", "t", "丙" * 100)]
        text = sh._render_facts_text(rows, max_total=120, per_fact=100)
        assert len(text) <= 120
        for gt in ("甲组", "乙组"):
            assert f"### {gt}\n" in text          # 组标题要么完整、要么不出现

    def test_length_never_exceeds_budget(self):
        rows = [(f"组{i}", "t", "字" * 50) for i in range(50)]
        text = sh._render_facts_text(rows, max_total=200, per_fact=50)
        assert len(text) <= 200

    def test_grouping_and_order_preserved(self):
        rows = [("概况", "t", "A" * 10), ("基坑", "t", "B" * 10)]
        text = sh._render_facts_text(rows, max_total=1000)
        assert text.index("### 概况") < text.index("### 基坑")

    async def test_invalid_ops_are_rejected_as_a_whole(self):
        async def call_ai(sys_prompt, user_payload):
            return _replace_ops("不存在的片段", "x")

        out = await cs.shrink_content_rounds(
            _long_content(), word_budget=200,
            prompt_factory=lambda r, wc: "P", call_ai=call_ai)
        assert out["applied"] is False
        assert "操作校验未通过" in out["stop_reason"]

    async def test_protected_region_is_never_touched(self):
        """表格/图表/图片/代码块为压缩禁区：命中即整轮拒绝（不部分应用）。"""
        content = "开头段落" * 10 + "\n\n| 参数 | 值 |\n| --- | --- |\n| 深度 | 8m |\n"

        async def call_ai(sys_prompt, user_payload):
            return _replace_ops("| 深度 | 8m |", "| 深度 | — |")

        out = await cs.shrink_content_rounds(
            content, word_budget=10,
            prompt_factory=lambda r, wc: "P", call_ai=call_ai)
        assert out["applied"] is False
        assert out["content"] == content
        assert "图片、Mermaid、代码块或表格" in out["stop_reason"]


