"""目录生成模块 R139 深度探索与修复 · 新护栏（2026-10-28）

聚焦目录生成模块，深度探索后发现并修复的真实缺陷，逐项护栏：

- 【BUG-C1 · stage_runner 异步观测回调被静默丢弃（P2）】
  `run_stage` 的 `on_event` / `on_activity` 类型为 ``Callable[[dict], Any]``，
  允许同步或异步可调用；旧实现直接 ``on_event(event)`` 不 await —— 调用方
  传 ``async def`` 时返回的协程**永远不执行**：① RuntimeWarning
  「coroutine was never awaited」；② 观测事件（touchActivity /
  emitMonitorEvent 同族）静默丢失，测试/宿主收不到任何活动回调。
  修法：统一走 ``_maybe_await``（与 run_stage 既有 build_retry_prompt /
  continue_task 同一出口），同步回调逐字不变。
  实测：baseline 全量 outline 簇 `test_技术评分单链_六阶段端到端` 即抛
  RuntimeWarning（coroutine '...emit_event' was never awaited）。

- 【BUG-B · 断线路径 partial 缺键（P1）】
  ``_checkpoint_partial_outline``（客户端断开 GeneratorExit → finally 兜底
  的唯一收尾出口）从不写 ``partial=True``，而显式停止路径
  ``_finish_stopped_with_partial`` 恒写 ``partial=True``。断线重挂接时
  ``_attach_checkpoint_result`` 按白名单回传 partial，缺失时前端
  ``buildPartialOutlineHint`` 拿到 undefined ⇒ 把「用户断开时只生成了一部分」
  当成「完整目录」展示，用户可能把半份目录当完整保存（R89 S3 注释
  「同口径带 partial」的承诺落空）。
  修法：本函数语义即「部分成果」，到达写库时 outline 或 failed_chapters
  必非空，恒置 ``partial=True``（与 _finish_stopped_with_partial 同口径）。
"""
import json

import pytest


async def _seed_task(db, task_id="t1", task_type="outline_generation",
                     scheme_id="s1", status="running"):
    await db.execute(
        "INSERT INTO task_registry (id, task_type, project_id, scheme_id,"
        " status, progress, checkpoint_json) VALUES (?,?,?,?,?,?,?)",
        (task_id, task_type, "p1", scheme_id, status, 0.0, "{}"))
    await db.commit()


# ============================================================
# BUG-B：断线路径 checkpoint 必须带 partial=True
# ============================================================
class TestCheckpointPartialOutline:
    async def test_disconnect_path_writes_partial_flag(self, db_conn):
        """客户端断开兜底 _checkpoint_partial_outline 必须写 partial=True。

        旧实现只写 {event,task_id,outline}（+可选 failed_*），从不写
        partial ⇒ 断线重挂接后前端 buildPartialOutlineHint 拿 undefined，
        把半份目录当「完整完成」展示（R89 S3 同口径承诺落空）。
        """
        from app.routers.sse_checkpoint import _checkpoint_partial_outline
        await _seed_task(db_conn, "t_disc1")
        ok = await _checkpoint_partial_outline(
            "t_disc1", [{"title": "第一章", "children": []}], [])
        assert ok is True
        cur = await db_conn.execute(
            "SELECT checkpoint_json FROM task_registry WHERE id=?", ("t_disc1",))
        row = await cur.fetchone()
        ckpt = json.loads(row["checkpoint_json"])
        assert ckpt["kind"] == "outline_result"
        assert ckpt["event"] == "stopped"
        assert ckpt["partial"] is True, \
            "断线兜底 checkpoint 必须带 partial=True（与 _finish_stopped_with_partial 同口径）"
        assert ckpt["outline"] == [{"title": "第一章", "children": []}]

    async def test_disconnect_path_failed_chapters_only_also_partial(self, db_conn):
        """仅失败章节（无 outline）也属部分成果：恒置 partial=True。"""
        from app.routers.sse_checkpoint import _checkpoint_partial_outline
        await _seed_task(db_conn, "t_disc2")
        ok = await _checkpoint_partial_outline(
            "t_disc2", [], ["第三章"])
        assert ok is True
        cur = await db_conn.execute(
            "SELECT checkpoint_json FROM task_registry WHERE id=?", ("t_disc2",))
        row = await cur.fetchone()
        ckpt = json.loads(row["checkpoint_json"])
        assert ckpt["partial"] is True
        assert ckpt["failed_count"] == 1

    async def test_disconnect_path_no_data_writes_nothing(self, db_conn):
        """无 outline 且无失败章节：不写 checkpoint（不覆盖有效旧成果）。"""
        from app.routers.sse_checkpoint import _checkpoint_partial_outline
        await _seed_task(db_conn, "t_disc3")
        ok = await _checkpoint_partial_outline("t_disc3", [], [])
        assert ok is False
        cur = await db_conn.execute(
            "SELECT checkpoint_json FROM task_registry WHERE id=?", ("t_disc3",))
        row = await cur.fetchone()
        assert json.loads(row["checkpoint_json"]) == {}, "无成果不得写 checkpoint"

    async def test_reattach_roundtrip_keeps_partial_flag(self, db_conn):
        """端到端断线重挂接链路：写入 → 读回 → _attach_checkpoint_result 白名单。

        _checkpoint_partial_outline 写 partial=True；task_status 断线重挂接经
        _attach_checkpoint_result 按 _CHECKPOINT_KINDS 白名单回传，
        必须把 partial 原样带到前端（旧实现断线路径缺键，重挂后丢该信号）。
        """
        from app.routers.sse_checkpoint import _checkpoint_partial_outline
        from app.routers.sse_handlers import _attach_checkpoint_result
        await _seed_task(db_conn, "t_disc4")
        await _checkpoint_partial_outline(
            "t_disc4", [{"title": "第一章", "children": []}], ["第三章"])
        cur = await db_conn.execute(
            "SELECT checkpoint_json FROM task_registry WHERE id=?", ("t_disc4",))
        row = await cur.fetchone()
        # 模拟 task_status 断线重挂接的行（内存终态补挂分支同构）
        fake_row = {"task_type": "outline_generation", "checkpoint_json": row["checkpoint_json"]}
        result: dict = {}
        _attach_checkpoint_result(fake_row, result)
        assert "outline_result" in result, "断线重挂接必须回传 outline_result"
        attached = result["outline_result"]
        assert attached["partial"] is True, \
            "重挂接回传的 outline_result 必须含 partial=True"
        assert attached["event"] == "stopped"
        assert attached["outline"] == [{"title": "第一章", "children": []}]


# ============================================================
# BUG-C1：stage_runner 异步观测回调必须被 await（不得泄漏协程）
# ============================================================
def _blocking(message: str) -> dict:
    return {"severity": "blocking", "file": "outline.json", "message": message}


def _quality(message: str) -> dict:
    return {"severity": "quality", "file": "outline.json", "message": message}


def _make_report(issues: list) -> dict:
    return {"value": {"ok": True}, "issues": issues}


def _make_candidate(content: str = '{"outline": []}') -> dict:
    return {"output_file": "outline.json", "output_content": content}


class TestStageRunnerAsyncCallbacks:
    async def test_async_on_event_and_on_activity_are_awaited(self):
        """async 观测回调必须被 await（否则协程泄漏 + 事件静默丢失）。

        旧实现：emit_event/emit_activity 直接 on_event(event) 不 await，
        传 async def 时返回的协程永远不执行 → events/activities 恒空、
        并抛 RuntimeWarning「coroutine was never awaited」。
        修法：统一走 _maybe_await。
        """
        from app.services.agent.stage_runner import run_stage
        events: list = []
        activities: list = []
        prompt_calls = {"n": 0}
        check_calls = {"n": 0}
        cont_calls = {"n": 0}
        quality_report = _make_report([_quality("列宽不一致")])
        clean_report = _make_report([])
        continuation = [{"stage": "s1", "prompt": "下一阶段提示"}, None]

        async def prompt_once(prompt):
            prompt_calls["n"] += 1
            if prompt_calls["n"] == 1:
                raise RuntimeError("第一次执行失败")
            return {"turns": 1}

        async def check(attempt):
            check_calls["n"] += 1
            report = (quality_report if check_calls["n"] <= 3 else clean_report)
            return _make_candidate(), report, []

        async def read_output():
            return "旧产物内容"

        async def continue_task(candidate, meta):
            cont_calls["n"] += 1
            return continuation[cont_calls["n"] - 1]

        def build_retry(request, meta):
            return "修复提示"

        async def on_event(event):
            events.append(event)

        async def on_activity(activity):
            activities.append(activity)

        result = await run_stage(
            "阶段提示词", prompt_once=prompt_once,
            check_submission=check, build_retry_prompt=build_retry,
            read_output=read_output, continue_task=continue_task,
            on_event=on_event, on_activity=on_activity,
            initial_stage="s0", output_file="outline.json")

        # 旧实现两列表恒空（协程从未执行）；现必须收到完整事件序列。
        assert [e["type"] for e in events] == [
            "retry", "retry", "retry", "task_output", "task_input"], \
            "async on_event 必须被 await，否则观测事件静默丢失"
        assert events[0]["prompt"] == "修复提示"
        assert [a["kind"] for a in activities] == [
            "retry", "retry", "retry", "submission.accepted", "workflow.continue"], \
            "async on_activity 必须被 await，否则活动回调静默丢失"
        assert result.prompt_calls == 5

    async def test_sync_callbacks_unchanged(self):
        """同步回调（生产默认 _emit_event/_emit_activity 形态）逐字不变。"""
        from app.services.agent.stage_runner import run_stage
        events: list = []
        activities: list = []
        quality_report = _make_report([_quality("列宽不一致")])
        clean_report = _make_report([])
        continuation = [{"stage": "s1", "prompt": "下一阶段提示"}, None]
        n = {"p": 0, "c": 0, "cont": 0}

        async def prompt_once(prompt):
            n["p"] += 1
            if n["p"] == 1:
                raise RuntimeError("第一次执行失败")
            return {"turns": 1}

        async def check(attempt):
            n["c"] += 1
            return (_make_candidate(),
                    quality_report if n["c"] <= 3 else clean_report, [])

        async def continue_task(candidate, meta):
            n["cont"] += 1
            return continuation[n["cont"] - 1]

        await run_stage(
            "阶段提示词", prompt_once=prompt_once, check_submission=check,
            build_retry_prompt=lambda request, meta: "修复提示",
            continue_task=continue_task,
            on_event=events.append, on_activity=activities.append,
            initial_stage="s0", output_file="outline.json")
        # 与既有 test_观测回调_收到_retry_与_阶段流转事件 同构 —— 同步回调逐字不变。
        assert [e["type"] for e in events] == [
            "retry", "retry", "retry", "task_output", "task_input"]
        assert [a["kind"] for a in activities] == [
            "retry", "retry", "retry", "submission.accepted", "workflow.continue"]
