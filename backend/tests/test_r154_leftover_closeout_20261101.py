# -*- coding: utf-8 -*-
"""R154（2026-11-01）遗留问题最优方案收口护栏（R149 遗留 L-2/L-3/L-4/L-5/L-6）。

五项修复 + 一项真机验收（COM 探针，不入 pytest）：

  FIX-A  终态 checkpoint 永不失效（L-3）：整表重建（_save_outline_to_db，save-outline
         与 apply-and-save 的共同落库出口）成功提交后清理该方案终态任务的
         outline_result checkpoint；运行中任务的 partial 兜底绝不清。
  FIX-B  切档无解释事件（L-2）：一次性→分步三处切档广播 switch_to_stepwise。
  FIX-C  Agent 工作区/manifest 永不清理（L-4）：目录 Agent 薄包装 + 正文 Agent
         薄包装，终态回收 <task_id> 工作区与 <task_id>_baseline。
  FIX-D  修复止损可被 blocking 振荡绕过（L-5）：_SubmissionPolicy 阶段级
         MAX_STAGE_REPAIRS 硬上限（minimum 档豁免）。
  FIX-E  agent_loop 无墙钟超时（L-6）：目录 Agent 传入 total_timeout 预算
         （正文 Agent 早已传 content_agent_repair_timeout=900，本项补齐目录侧）。

锁定不变量：A 终态清理/运行保护/kind 隔离/单一事实源接线；B 计数锚（3 处）+
事件直通；C 真实目录回收/None 跳过/不存在路径不抛；D 振荡截停/minimum 豁免/
既有语义回归；E 定义形态锚。
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import app.routers.sse_checkpoint as sse_checkpoint
import app.routers.sse_handlers as sse_handlers
import app.services.agent.outline_agent_flow as flow
import pytest
from app.routers.sections import SAVE_OUTLINE_ADDITIVE_KEYS
from app.routers.sse_checkpoint import (
    TERMINAL_TASK_STATUSES,
    _clear_outline_checkpoints_for_scheme,
)
from app.services.agent.submission_policy import MAX_STAGE_REPAIRS, create_submission_policy
from app.services.agent.workspace_baseline import cleanup_workspace_and_baseline

# ---------------------------------------------------------------- A 组

async def _mk_task(db, task_id: str, scheme_id: str, status: str,
                   kind: str | None) -> None:
    ckpt = json.dumps({"kind": kind, "event": "completed", "task_id": task_id,
                       "outline": [{"title": "旧树"}]}, ensure_ascii=False) if kind else None
    await db.execute(
        "INSERT INTO task_registry(id, task_type, scheme_id, status, checkpoint_json)"
        " VALUES(?,?,?,?,?)",
        (task_id, "outline_generation", scheme_id, status, ckpt))


class TestCheckpointClearOnSave:
    async def test_a1_terminal_outline_result_cleared(self, db_conn):
        sid = "sc-a1"
        await _mk_task(db_conn, "t-done", sid, "completed", "outline_result")
        n = await _clear_outline_checkpoints_for_scheme(db_conn, sid)
        assert n == 1
        cur = await db_conn.execute(
            "SELECT checkpoint_json FROM task_registry WHERE id='t-done'")
        assert (await cur.fetchone())["checkpoint_json"] is None

    async def test_a2_running_task_protected(self, db_conn):
        """运行中任务的 partial checkpoint 是断线重挂唯一恢复源——绝不清。"""
        sid = "sc-a2"
        await _mk_task(db_conn, "t-run", sid, "running", "outline_result")
        n = await _clear_outline_checkpoints_for_scheme(db_conn, sid)
        assert n == 0
        cur = await db_conn.execute(
            "SELECT checkpoint_json FROM task_registry WHERE id='t-run'")
        assert (await cur.fetchone())["checkpoint_json"] is not None

    async def test_a3_other_kinds_untouched(self, db_conn):
        """content_result / facts_result 与目录成果无关——不动。"""
        sid = "sc-a3"
        await _mk_task(db_conn, "t-c", sid, "completed", "content_result")
        await _mk_task(db_conn, "t-f", sid, "stopped", "facts_result")
        n = await _clear_outline_checkpoints_for_scheme(db_conn, sid)
        assert n == 0
        cur = await db_conn.execute(
            "SELECT COUNT(*) AS n FROM task_registry"
            " WHERE scheme_id=? AND checkpoint_json IS NOT NULL", (sid,))
        assert (await cur.fetchone())["n"] == 2

    async def test_a4_save_outline_clears_and_reports(self, db_conn, monkeypatch):
        """行为级：save-outline 整表重建提交成功 → checkpoint 清空 + 响应键。"""
        from app.routers.sections import _save_outline_to_db

        sid = "sc-a4"
        await db_conn.execute(
            "INSERT INTO projects(id,name) VALUES('p-a4','p')")
        await db_conn.execute(
            "INSERT INTO schemes(id,project_id,name) VALUES(?,?,'s')", (sid, "p-a4"))
        await _mk_task(db_conn, "t-old", sid, "completed", "outline_result")
        await db_conn.commit()
        tree = [{"title": "第一章 新目录", "description": "", "children": []}]
        result = await _save_outline_to_db(db_conn, sid, tree, "R154")
        assert result["ok"] is True
        assert result.get("cleared_checkpoints") == 1
        cur = await db_conn.execute(
            "SELECT checkpoint_json FROM task_registry WHERE id='t-old'")
        assert (await cur.fetchone())["checkpoint_json"] is None

    def test_a5_terminal_wordlist_single_source(self):
        """sse_handlers._TERMINAL_STATUSES 是 sse_checkpoint 常量的别名（单一事实源）。"""
        assert sse_handlers._TERMINAL_STATUSES is TERMINAL_TASK_STATUSES
        assert TERMINAL_TASK_STATUSES == frozenset(
            {"completed", "degraded", "failed", "stopped"})

    def test_a6_additive_key_registered(self):
        assert "cleared_checkpoints" in SAVE_OUTLINE_ADDITIVE_KEYS


# ---------------------------------------------------------------- B 组

class TestSwitchToStepwiseEvent:
    def test_b1_three_switch_sites_anchored(self):
        """计数锚（R151 教训⑬）：一次性→分步切档共 3 处，全部带专用事件。"""
        src = Path(sse_handlers.__file__).read_text(encoding="utf-8")
        assert src.count('event="switch_to_stepwise"') == 3

    async def test_b2_event_reaches_broadcast(self, monkeypatch):
        """update_progress 的 event 形参直通广播载荷（前端可消费）。"""
        from app.services.ai import task_registry as tr

        seen: list[dict] = []

        async def _fake_broadcast(task_id, payload):
            seen.append(payload)

        # ✅ 断言面是广播载荷；DB 落库打桩——否则本用例（无 db_conn 夹具）会
        #    拉起真实 app.db 连接且无人关闭，进程退出被挂住（R138 教训⑦）。
        async def _noop_db(task_id, progress, message):
            return None

        monkeypatch.setattr(tr, "broadcast", _fake_broadcast)
        monkeypatch.setattr(tr, "_update_task_progress_db", _noop_db)
        await tr.update_progress("t-b2", 0.1, "切换分步",
                                 event="switch_to_stepwise")
        assert seen and seen[-1]["event"] == "switch_to_stepwise"
        assert seen[-1]["progress"] == 0.1


# ---------------------------------------------------------------- C 组

class TestWorkspaceCleanup:
    def test_c1_helper_deletes_both_and_tolerates_missing(self, tmp_path):
        ws = tmp_path / "ws1"
        bl = tmp_path / "ws1_baseline"
        (ws / "sub").mkdir(parents=True)
        (ws / "sub" / "outline.json").write_text("{}", encoding="utf-8")
        (bl / "files").mkdir(parents=True)
        (bl / "manifest.json").write_text("{}", encoding="utf-8")
        cleanup_workspace_and_baseline(ws, bl)
        assert not ws.exists() and not bl.exists()
        # 不存在路径 / None：静默跳过，绝不抛
        cleanup_workspace_and_baseline(tmp_path / "ghost", None)
        cleanup_workspace_and_baseline(None, None)

    async def test_c2_outline_flow_wrapper_cleans_up(self, tmp_path, monkeypatch):
        """run_outline_agent 薄包装：impl 抛异常路径也回收（默认工作区 + 基线）。"""
        import app.config as config

        fake_data = tmp_path / "data"
        monkeypatch.setattr(flow, "DATA_DIR", fake_data)
        monkeypatch.setattr(config.settings, "outline_agent_enabled", True,
                            raising=False)

        async def _boom(**kwargs):
            raise RuntimeError("impl 爆炸")

        monkeypatch.setattr(flow, "_run_outline_agent_impl", _boom)
        task_id = "t-c2"
        ws = fake_data / "agent_workspaces" / task_id
        bl = fake_data / "agent_workspaces" / f"{task_id}_baseline"
        ws.mkdir(parents=True)
        bl.mkdir(parents=True)
        (ws / "outline.json").write_text("{}", encoding="utf-8")
        with pytest.raises(RuntimeError):
            await flow.run_outline_agent(
                task_id=task_id, scheme={"name": "s", "type": ""},
                context={}, chat_fn=None)
        assert not ws.exists() and not bl.exists()

    async def test_c3_repair_wrapper_cleans_up(self, tmp_path, monkeypatch):
        """正文 Agent 薄包装：终态回收工作区与基线（同族第二入口）。"""
        import app.config as config
        from app.services.content_agent import repair as repair_mod

        fake_data = tmp_path / "data"
        monkeypatch.setattr(config.settings, "content_agent_repair_enabled", True,
                            raising=False)
        # content_agent.workspace 的 workspace_root_for 读 DATA_DIR（app.config）
        from app.services.content_agent import workspace as caw
        monkeypatch.setattr(caw, "DATA_DIR", fake_data)

        from app.services.content_agent.repair import AgentRepairResult

        async def _fake_impl(db, **kwargs):
            return AgentRepairResult(status="skipped", reason="测试桩")

        monkeypatch.setattr(repair_mod, "_run_agent_section_repair_impl", _fake_impl)
        captured: dict = {}

        real_cleanup = repair_mod.cleanup_workspace_and_baseline

        def _spy(ws, bl):
            captured["ws"] = str(ws)
            captured["bl"] = str(bl)
            return real_cleanup(ws, bl)

        monkeypatch.setattr(repair_mod, "cleanup_workspace_and_baseline", _spy)
        await repair_mod.run_agent_section_repair(
            None, scheme_id="sc-c3", targets=[])
        tid = Path(captured["ws"]).name
        assert tid.startswith("content_repair_")
        assert Path(captured["bl"]).name == f"{tid}_baseline"


# ---------------------------------------------------------------- D 组

def _report(blocking: int, quality: int = 0) -> dict:
    issues = ([{"severity": "blocking", "file": "f", "message": "b"}] * blocking
              + [{"severity": "quality", "file": "f", "message": "q"}] * quality)
    return {"issues": issues}


class TestRepairHardCap:
    def test_d1_oscillation_stopped_at_cap(self):
        """blocking 2→1→2→1… 振荡（improved 持续重置 stalled）：第 MAX+1 次
        repair 决策被截停为 stop，minimum 档永不触发。"""
        pol = create_submission_policy()
        seq = [2, 1, 2, 1, 2, 1, 2, 1, 2, 1, 2, 1]
        decisions = [pol.inspect(_report(b)) for b in seq]
        repairs = [d for d in decisions if d["action"] == "repair"]
        assert len(repairs) == MAX_STAGE_REPAIRS
        assert decisions[MAX_STAGE_REPAIRS]["action"] == "stop"
        assert all(d.get("mode") != "minimum" for d in decisions)

    def test_d2_existing_semantics_unchanged(self):
        pol = create_submission_policy()
        assert pol.inspect(_report(0, 0))["action"] == "accept"
        pol2 = create_submission_policy()
        pol2.inspect(_report(2, 0))
        r2 = pol2.inspect(_report(2, 0))  # stalled=1 → change-strategy
        assert r2["action"] == "repair" and r2["mode"] == "change-strategy"
        r3 = pol2.inspect(_report(2, 0))  # stalled=2 → minimum（设计内最后机会）
        assert r3["action"] == "repair" and r3["mode"] == "minimum"
        r4 = pol2.inspect(_report(1, 0))  # minimumAttempted 后仍有 blocking → stop
        assert r4["action"] == "stop"
        r5 = pol2.inspect(_report(0, 3))  # 清零 → accept
        assert r5["action"] == "accept"

    def test_d3_minimum_exempt_from_cap(self):
        """repair_count 已达上限但 stalled>=2 → minimum 档仍发起（豁免）。"""
        pol = create_submission_policy()
        pol._repair_count = MAX_STAGE_REPAIRS  # 直达上限（模拟长振荡后）
        decision = pol.inspect(_report(2, 0))  # 首轮 stalled=1 → change-strategy
        # 上限已达且非 minimum → stop
        assert decision["action"] == "stop"
        pol2 = create_submission_policy()
        pol2._repair_count = MAX_STAGE_REPAIRS
        pol2.inspect(_report(2, 0))           # stalled=1 → stop（非 minimum）
        pol2._stalled = 2                     # 直置 stalled（构造 minimum 触发态）
        pol2._previous = {"blocking": 2, "remaining": 0}
        d2 = pol2.inspect(_report(2, 0))
        assert d2["action"] == "repair" and d2["mode"] == "minimum"


# ---------------------------------------------------------------- E 组

class TestLoopWallClockBudget:
    def test_e1_constant_positive(self):
        assert flow._LOOP_WALL_CLOCK_BUDGET >= 300.0

    def test_e2_definition_shape_anchor(self):
        """定义处调用形态锚：_prompt_once 的 run_agent_loop 必须传 total_timeout。"""
        src = inspect.getsource(flow._run_outline_agent_impl)
        assert "total_timeout=_LOOP_WALL_CLOCK_BUDGET" in src

    def test_e3_body_timeout_stop_handled(self):
        """STOP_TIMEOUT 与 STOP_TURN_LIMIT 同被上抛（agent_loop 契约：不当成功）。"""
        src = inspect.getsource(flow._run_outline_agent_impl)
        assert "STOP_TURN_LIMIT, STOP_TIMEOUT" in src


# ---------------------------------------------------------------- 直跑入口

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
