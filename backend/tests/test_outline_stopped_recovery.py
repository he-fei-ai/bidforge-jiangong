"""目录生成「停止/重挂成果恢复」回归测试（2026-09-20 修复批次）

覆盖本轮修复：
- sse_handlers._finish_stopped_with_partial
  → 显式 stopped / CancelledError 收尾时携带已生成目录 + checkpoint 落库
    （旧实现短方案链路发裸 stopped 事件，成果确定性丢失）
- sse_handlers.task_status 内存终态窗口
  → 补挂 outline_result（旧实现该窗口断线重挂拿不到成果，前端误走 load()）
- generate_outline 源码回归锁
  → 全部 stopped 分支必须走 _finish_stopped_with_partial，禁止裸事件回潮
- sections.reorder_sections body 类型防御（非 list → 400，非字符串元素过滤）
- schemes.delete_scheme 断开 uploaded_outlines 悬挂引用（记录不删、引用置空）
"""

import inspect
import json
import uuid

import pytest
from fastapi import HTTPException


# ============================================================
# 夹具辅助
# ============================================================
async def _seed_project_scheme(db, sid="s1", pid="p1"):
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)", (pid, "测试项目"))
    await db.execute("INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES (?,?,?)", (sid, pid, "测试方案"))
    await db.commit()


async def _seed_task(db, task_id="t1", task_type="outline_generation", scheme_id="s1", status="running"):
    await db.execute(
        "INSERT INTO task_registry (id, task_type, project_id, scheme_id,"
        " status, progress, checkpoint_json) VALUES (?,?,?,?,?,?,?)",
        (task_id, task_type, "p1", scheme_id, status, 0.0, "{}"),
    )
    await db.commit()


def _parse_sse(line: str) -> dict:
    assert line.startswith("data: ")
    return json.loads(line[len("data: ") :])


# ============================================================
# 1. _finish_stopped_with_partial：stopped 收尾带成果
# ============================================================
class TestFinishStoppedWithPartial:
    async def test_stopped_carries_outline_and_saves_checkpoint(self, db_conn):
        """目录已生成后用户点停止：成果必须同时出现在事件与 checkpoint 中。"""
        import app.routers.sse_handlers as sh

        await _seed_task(db_conn, "t_stop1")
        outline = [{"title": "第一章 工程概况", "children": []}, {"title": "第二章 施工部署", "children": []}]
        holder = {"outline": outline, "failed_chapters": ["第三章"]}

        line = await sh._finish_stopped_with_partial("t_stop1", holder)
        evt = _parse_sse(line)
        assert evt["event"] == "stopped"
        assert evt["task_id"] == "t_stop1"
        assert evt["outline"] == outline, "stopped 事件必须携带已生成目录（前端已就绪消费）"
        assert evt["failed_chapters"] == ["第三章"]
        assert evt["failed_count"] == 1
        # holder 清空：防 finally 兜底重复落库
        assert holder["outline"] == []

        # checkpoint 落库（kind + 成果字段），供断线重挂 task_status 回传
        cur = await db_conn.execute("SELECT status, checkpoint_json FROM task_registry WHERE id=?", ("t_stop1",))
        row = await cur.fetchone()
        assert row["status"] == "stopped"
        ckpt = json.loads(row["checkpoint_json"])
        assert ckpt["kind"] == "outline_result"
        assert ckpt["outline"] == outline
        assert ckpt["failed_count"] == 1

    async def test_stopped_without_outline_emits_bare_event(self, db_conn):
        """生成尚未产出目录（holder 为空）：不写 checkpoint，任务仍正确置终态。"""
        import app.routers.sse_handlers as sh

        await _seed_task(db_conn, "t_stop2")
        holder = {"outline": [], "failed_chapters": []}

        line = await sh._finish_stopped_with_partial("t_stop2", holder)
        evt = _parse_sse(line)
        assert evt["event"] == "stopped"
        assert "outline" not in evt

        cur = await db_conn.execute("SELECT status, checkpoint_json FROM task_registry WHERE id=?", ("t_stop2",))
        row = await cur.fetchone()
        assert row["status"] == "stopped"
        assert json.loads(row["checkpoint_json"]) == {}, "无成果不得覆盖 checkpoint"

    async def test_checkpoint_written_before_terminal_state(self, db_conn, monkeypatch):
        """收尾顺序契约：checkpoint 落库必须发生在 finish_task 置终态之前
        （杜绝「DB 已终态但成果未写入」的断线重挂空窗）。"""
        import app.routers.sse_handlers as sh

        order = []
        real_save = sh._save_outline_checkpoint

        async def spy_save(task_id, payload, *_sp_args, **_sp_kwargs):
            order.append("checkpoint")
            return await real_save(task_id, payload, *_sp_args, **_sp_kwargs)

        async def fake_finish(task_id, status="completed", message=""):
            order.append("finish")

        monkeypatch.setattr(sh, "_save_outline_checkpoint", spy_save)
        monkeypatch.setattr(sh, "finish_task", fake_finish)
        await _seed_task(db_conn, "t_stop3")
        await sh._finish_stopped_with_partial(
            "t_stop3", {"outline": [{"title": "A", "children": []}], "failed_chapters": []}
        )
        assert order == ["checkpoint", "finish"]


# ============================================================
# 2. generate_outline 源码回归锁：禁止裸 stopped 事件回潮
# ============================================================
class TestGenerateOutlineStoppedBranches:
    def test_all_stopped_branches_carry_partial_results(self):
        """generate_outline 内所有 stopped 出口必须走 _finish_stopped_with_partial。

        旧实现短方案 3 个显式分支 + CancelledError 发裸 stopped 事件
        （不带 outline、不写 checkpoint），目录已生成时用户点停止成果确定性丢失。
        """
        import app.routers.sse_handlers as sh

        src = inspect.getsource(sh.generate_outline)
        # 归一化空格后，裸 stopped SSE 行（'event':'stopped'）不允许再出现在该路由内
        normalized = src.replace("'event': 'stopped'", "'event':'stopped'")
        assert (
            "'event':'stopped'" not in normalized
        ), "generate_outline 内禁止裸 stopped 事件（成果必须经 _finish_stopped_with_partial 下发）"
        assert (
            normalized.count("_finish_stopped_with_partial") >= 5
        ), "短方案 3 分支 + 长方案入口 + CancelledError 共 5 处 stopped 出口"


# ============================================================
# 3. task_status 内存终态窗口补挂 checkpoint（BUG B）
# ============================================================
class TestTaskStatusMemoryTerminalCheckpoint:
    async def test_terminal_memory_state_attaches_outline_result(self, db_conn, monkeypatch):
        """finish_task 已置终态、尚未 pop 的窗口内，重挂的前端必须拿到 outline_result。"""
        import app.routers.sse_handlers as sh
        import app.services.ai.task_registry as tr

        await _seed_task(db_conn, "t_mem")
        ckpt = {
            "kind": "outline_result",
            "event": "stopped",
            "outline": [{"title": "第一章", "children": []}],
            "failed_count": 0,
        }
        await db_conn.execute(
            "UPDATE task_registry SET status='stopped', checkpoint_json=? WHERE id=?",
            (json.dumps(ckpt, ensure_ascii=False), "t_mem"),
        )
        await db_conn.commit()

        monkeypatch.setattr(sh, "get_read_conn", lambda: _conn_ctx(db_conn))
        monkeypatch.setattr(sh, "release_read_conn", _noop_release)
        tr._tasks["t_mem"] = {
            "type": "outline_generation",
            "status": "stopped",
            "progress": 1.0,
            "message": "用户已停止",
            "scheme_id": "s1",
        }
        res = await sh.task_status("t_mem")
        assert res["live"] is True
        assert res["status"] == "stopped"
        assert res["outline_result"]["outline"] == [{"title": "第一章", "children": []}]

    async def test_running_memory_state_does_not_touch_db(self, monkeypatch):
        """进行中任务走纯内存分支：不得额外查库（口径不变、无性能回退）。"""
        import app.routers.sse_handlers as sh
        import app.services.ai.task_registry as tr

        async def boom():
            raise AssertionError("running 状态不应读 DB checkpoint")

        monkeypatch.setattr(sh, "get_read_conn", boom)
        tr._tasks["t_run"] = {
            "type": "outline_generation",
            "status": "running",
            "progress": 0.3,
            "message": "生成中",
            "scheme_id": "s1",
        }
        res = await sh.task_status("t_run")
        assert res["status"] == "running"
        assert "outline_result" not in res


def _conn_ctx(conn):
    """把测试连接包成 awaitable（与 get_read_conn 返回协程的口径一致）。"""

    async def _get():
        return conn

    return _get()


async def _noop_release(conn):
    pass


# ============================================================
# 4. reorder_sections 类型防御
# ============================================================
class TestReorderDefense:
    async def test_non_list_order_rejected(self, db_conn):
        import app.routers.sections as sec

        await _seed_project_scheme(db_conn)
        with pytest.raises(HTTPException) as ei:
            await sec.reorder_sections("s1", {"order": "abc"}, db=db_conn)
        assert ei.value.status_code == 400
        # 字符串 "abc" 曾被 enumerate 成 ['a','b','c'] 逐字符写 sort_order

    async def test_invalid_elements_filtered(self, db_conn):
        """列表内的 None/数字/空串被过滤，合法 id 正常排序且不报错。"""
        import app.routers.sections as sec

        await _seed_project_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, title, parent_id, level, sort_order, outline_json)"
            " VALUES ('sec1','s1','第一章','',1,9,'{\"id\": \"1\"}')"
        )
        await db_conn.commit()
        res = await sec.reorder_sections("s1", {"order": ["sec1", None, 5, ""]}, db=db_conn)
        assert res.get("ok") is not False
        cur = await db_conn.execute("SELECT sort_order FROM sections WHERE id='sec1'")
        assert (await cur.fetchone())["sort_order"] == 0


# ============================================================
# 5. 方案删除断开 uploaded_outlines 悬挂引用
# ============================================================
class TestSchemeDeleteUploadRefs:
    async def test_uploaded_outline_reference_detached(self, db_conn):
        import app.routers.schemes as sch

        await _seed_project_scheme(db_conn)
        rid = str(uuid.uuid4())
        await db_conn.execute(
            "INSERT INTO uploaded_outlines (id, project_id, scheme_id, status)" " VALUES (?, 'p1', 's1', 'saved')",
            (rid,),
        )
        await db_conn.commit()

        await sch.delete_scheme(project_id="p1", scheme_id="s1", db=db_conn)

        cur = await db_conn.execute("SELECT scheme_id, status FROM uploaded_outlines WHERE id=?", (rid,))
        row = await cur.fetchone()
        assert row is not None, "上传记录是项目级素材，不得随方案删除"
        assert row["scheme_id"] == "", "悬挂引用必须断开，否则统计/查询指向已删方案"


# ============================================================
# 6. R226（DAT-6′）：任务已终态后迟到停止，不得覆写 completed 成果
# ============================================================
class TestFinishStoppedLateReentry:
    async def test_late_stop_skips_when_db_terminal(self, db_conn, monkeypatch):
        """R223 遗留 #1（DAT-6′）：任务已 completed（成果已落 checkpoint）后，
        迟到停止/断线兜底不得再写 partial checkpoint 覆盖、不得再调
        finish_task("stopped") 覆写终态。

        旧实现：_write_task_checkpoint_db 是无条件 UPDATE（无 status 守卫），
        partial 成果会覆盖 completed checkpoint —— 用户看到「任务 completed 但
        重挂提示部分成果」；且 finish_task("stopped") 会广播 stopped 事件。
        守卫读 DB 行状态，终态 completed/degraded 时直接返回、零副作用。
        """
        import app.routers.sse_handlers as sh

        completed_ckpt = {
            "kind": "outline_result",
            "event": "completed",
            "outline": [{"title": "第一章 已生成", "children": []}],
            "failed_count": 0,
        }
        await _seed_task(db_conn, "t_dat6_late", status="completed")
        await db_conn.execute(
            "UPDATE task_registry SET checkpoint_json=? WHERE id=?",
            (json.dumps(completed_ckpt, ensure_ascii=False), "t_dat6_late"),
        )
        await db_conn.commit()

        calls = []

        async def spy_save(task_id, payload, *_sp_args, **_sp_kwargs):
            calls.append(("checkpoint", payload))

        async def spy_finish(task_id, status="completed", message=""):
            calls.append(("finish", status))

        monkeypatch.setattr(sh, "_save_outline_checkpoint", spy_save)
        monkeypatch.setattr(sh, "finish_task", spy_finish)

        holder = {"outline": [{"title": "第一章 部分", "children": []}], "failed_chapters": []}
        line = await sh._finish_stopped_with_partial("t_dat6_late", holder)
        evt = _parse_sse(line)
        assert evt["event"] == "stopped"

        # 守卫生效：零副作用（不写 checkpoint、不 finish_task）
        assert calls == [], "任务已终态时迟到停止不得再写 checkpoint / 再调 finish_task"
        # DB 终态与 completed 成果未被覆盖
        cur = await db_conn.execute("SELECT status, checkpoint_json FROM task_registry WHERE id=?", ("t_dat6_late",))
        row = await cur.fetchone()
        assert row["status"] == "completed", "终态不得被覆写成 stopped"
        ckpt = json.loads(row["checkpoint_json"])
        assert ckpt["outline"][0]["title"] == "第一章 已生成", "completed 成果不得被 partial 覆盖"
        assert ckpt["event"] == "completed"

    async def test_live_stop_proceeds_when_running(self, db_conn, monkeypatch):
        """对照：任务仍在 running（未终态）时，正常停止路径照常写 checkpoint +
        finish_task（守卫不得误伤合法停止，也不得引入 DB 读取失败时的行为回退）。"""
        import app.routers.sse_handlers as sh

        await _seed_task(db_conn, "t_dat6_run", status="running")
        calls = []

        async def spy_save(task_id, payload, *_sp_args, **_sp_kwargs):
            calls.append(("checkpoint", payload))

        async def spy_finish(task_id, status="completed", message=""):
            calls.append(("finish", status))

        monkeypatch.setattr(sh, "_save_outline_checkpoint", spy_save)
        monkeypatch.setattr(sh, "finish_task", spy_finish)

        holder = {"outline": [{"title": "第一章", "children": []}], "failed_chapters": []}
        line = await sh._finish_stopped_with_partial("t_dat6_run", holder)
        evt = _parse_sse(line)
        assert evt["event"] == "stopped"
        assert evt["outline"] == [{"title": "第一章", "children": []}]
        # 正常路径：checkpoint + finish 都执行（且顺序正确）
        assert [c[0] for c in calls] == ["checkpoint", "finish"]

    async def test_db_read_failure_fails_open(self, db_conn, monkeypatch):
        """读 DB 行状态失败（连接异常）时保守放行：仍执行正常停止收尾，
        finish_task 自身的 WHERE status IN running/paused 守卫仍能阻止终态覆写。"""
        import app.routers.sse_handlers as sh

        await _seed_task(db_conn, "t_dat6_fail", status="running")
        calls = []

        async def spy_save(task_id, payload, *_sp_args, **_sp_kwargs):
            calls.append(("checkpoint", payload))

        async def spy_finish(task_id, status="completed", message=""):
            calls.append(("finish", status))

        async def boom(task_id):
            raise RuntimeError("连接异常")

        monkeypatch.setattr(sh, "_save_outline_checkpoint", spy_save)
        monkeypatch.setattr(sh, "finish_task", spy_finish)
        monkeypatch.setattr(sh, "_load_task_db_status", boom)

        holder = {"outline": [{"title": "第一章", "children": []}], "failed_chapters": []}
        line = await sh._finish_stopped_with_partial("t_dat6_fail", holder)
        evt = _parse_sse(line)
        assert evt["event"] == "stopped"
        assert [c[0] for c in calls] == ["checkpoint", "finish"], "DB 读失败应 fail-open 走正常收尾"
