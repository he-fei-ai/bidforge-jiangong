# -*- coding: utf-8 -*-
"""R177（2026-11-05）后台日志审计收口护栏。

本文件锁定的 4 项修复，全部来自 ``logs/backend.log``（2026-10-28 ~ 2026-11-05，
39,276 行 / 4.8 MB）的结构化审计与源码取证：

A 组 · ``task_registry.finish_task`` 根修（G12-5 不变量 + 终态落库 shield）
    生产实证：11 次「断线兜底保存正文成果被取消」之后**严格 3 秒**出现
    「mark_stream_ended: 任务 X 在 SSE 结束后 3s 内未落终态」（配对率 11/11）。
    根因：① 终态落库被取消 → DB 里任务永远停在 running；② 内存态清理的 finally
    只挂在「广播」那层 try 上 → 首个 await 抛 CancelledError 时整段清理被跳过。

B 组 · ``sse_handlers`` 三处断线兜底 ``finish_task`` 必须 shield
    与 ``bid_analysis.py:1348``（2026-09-23 已落地）同一范式。

C 组 · ``provider_factory._gate_candidates`` 本地配置问题不得计入全局熔断闸门
    生产实证：连续 2 次「候选 storm 已保存的密钥无法解密」即激活 60s 全局快速
    失败 → 全站所有模块（正文生成 / bid_analysis / content_plan / 事实提取）中止。

D 组 · ``upload_outline.save_as_outline`` 必须有 ``isinstance(outline, list)`` 闸门
    非数组但 truthy 的载荷（``{"a": 1}`` / ``"abc"``）此前会绕过空值判据 →
    ``normalize_outline`` 原样返回 → 收集到 0 个节点 → ``to_delete`` = 旧章节全集
    → **整表 DELETE 掉该方案所有章节（含已生成正文）并返回 200**。

手法：行为实证（内存库 / monkeypatch）+ AST 源码锁（防后人误删）。
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import time
import uuid

import pytest
from fastapi import HTTPException

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"
SH_PATH = APP_DIR / "routers" / "sse_handlers.py"
PF_PATH = APP_DIR / "services" / "ai" / "provider_factory.py"
TR_PATH = APP_DIR / "services" / "ai" / "task_registry.py"
UO_PATH = APP_DIR / "routers" / "upload_outline.py"


# =====================================================================
# A 组 · finish_task 根修（行为实证）
# =====================================================================
class TestFinishTaskTerminalGuarantee:
    """终态必须落库 + 内存态必须清理（任何退出路径，含取消）。"""

    @pytest.mark.asyncio
    async def test_normal_path_unchanged(self, db_conn):
        """正常路径：DB 落终态 + 内存态清理 + message 原样。"""
        from app.services.ai import task_registry as tr

        tid = await tr.register_task("content_generation", "p1", "s1")
        assert tid in tr._tasks
        await tr.finish_task(tid, "completed", "全部完成")
        assert tid not in tr._tasks, "finish_task 必须清理内存态（G12-5 不变量）"
        cur = await db_conn.execute("SELECT status, message FROM task_registry WHERE id=?", (tid,))
        row = await cur.fetchone()
        assert row["status"] == "completed"
        assert row["message"] == "全部完成"

    @pytest.mark.asyncio
    async def test_db_write_failure_still_clears_memory(self, db_conn, monkeypatch):
        """落库抛异常（database is locked 等）→ 内存态仍必须清理。"""
        from app.services.ai import task_registry as tr

        tid = await tr.register_task("content_generation", "p2", "s2")

        async def _boom(task_id, status, message):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(tr, "_write_task_terminal_db", _boom)
        await tr.finish_task(tid, "stopped", "客户端断开，任务已终止")
        assert tid not in tr._tasks, "落库失败不得阻断内存态清理"

    @pytest.mark.asyncio
    async def test_cancelled_still_clears_memory_state(self, db_conn, monkeypatch):
        """R177 核心：落库处被取消 → 内存态仍必须被清理（否则任务永久卡 running）。"""
        from app.services.ai import task_registry as tr

        tid = await tr.register_task("content_generation", "p3", "s3")
        assert tid in tr._tasks

        started = asyncio.Event()
        release = asyncio.Event()
        orig = tr._write_task_terminal_db

        async def _slow_write(task_id, status, message):
            started.set()
            await release.wait()
            await orig(task_id, status, message)

        monkeypatch.setattr(tr, "_write_task_terminal_db", _slow_write)

        task = asyncio.create_task(tr.finish_task(tid, "stopped", "客户端断开，任务已终止"))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert tid not in tr._tasks, "取消后内存态残留 → 任务永久停在 running（sections 409 守卫失效、只能重启进程）"

    @pytest.mark.asyncio
    async def test_cancelled_still_writes_terminal_to_db(self, db_conn, monkeypatch):
        """R177 核心：取消后终态**仍要落库**（shield 的意义）。"""
        from app.services.ai import task_registry as tr

        tid = await tr.register_task("content_generation", "p4", "s4")

        started = asyncio.Event()
        release = asyncio.Event()
        orig = tr._write_task_terminal_db

        async def _slow_write(task_id, status, message):
            started.set()
            await release.wait()
            await orig(task_id, status, message)

        monkeypatch.setattr(tr, "_write_task_terminal_db", _slow_write)

        task = asyncio.create_task(tr.finish_task(tid, "stopped", "客户端断开，任务已终止"))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        release.set()
        deadline = time.monotonic() + 5.0
        row = None
        while time.monotonic() < deadline:
            cur = await db_conn.execute("SELECT status, message FROM task_registry WHERE id=?", (tid,))
            row = await cur.fetchone()
            if row and row["status"] == "stopped":
                break
            await asyncio.sleep(0.02)
        assert (
            row is not None and row["status"] == "stopped"
        ), "取消后终态未落库 → 前端 pollTaskUntilTerminal 永远等不到终态（任务栏永远转圈）"
        assert row["message"] == "客户端断开，任务已终止"

    @pytest.mark.asyncio
    async def test_stopped_cancels_child_tasks(self, db_conn):
        """stopped 语义：后台子任务必须被取消（与既有口径一致，不得回归）。"""
        from app.services.ai import task_registry as tr

        tid = await tr.register_task("content_generation", "p5", "s5")

        async def _forever():
            await asyncio.sleep(3600)

        child = asyncio.create_task(_forever())
        tr._tasks[tid]["child_tasks"].add(child)
        await tr.finish_task(tid, "stopped", "客户端断开，任务已终止")
        await asyncio.sleep(0.05)
        assert child.cancelled() or child.done(), "stopped 终态必须取消后台子任务"


# =====================================================================
# B 组 · SSE 三处断线兜底 finish_task 必须 shield（AST 源码锁）
# =====================================================================
def _sse_finish_task_calls():
    """返回 (未 shield 的断线兜底调用, 已 shield 的断线兜底调用) 两个源码片段列表。"""
    src = SH_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)

    shielded_ids: set[int] = set()
    protected: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "shield"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncio"
        ):
            continue
        for arg in node.args:
            if not (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "finish_task"):
                continue
            shielded_ids.add(id(arg))
            seg = ast.get_source_segment(src, arg) or ""
            if "客户端断开" in seg:
                protected.append(seg)

    unshielded: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "finish_task"
        ):
            continue
        if id(node.value) in shielded_ids:
            continue
        seg = ast.get_source_segment(src, node.value) or ""
        if "客户端断开" in seg:
            unshielded.append(seg)
    return unshielded, protected


class TestSseDisconnectFallbackShielded:
    def test_all_disconnect_fallbacks_are_shielded(self):
        """断线兜底的 finish_task 必须被 asyncio.shield 包住（否则终态落不下来）。"""
        unshielded, _ = _sse_finish_task_calls()
        assert not unshielded, (
            "以下断线兜底 finish_task 未被 asyncio.shield 保护，取消风暴下终态落不了库：\n  " + "\n  ".join(unshielded)
        )

    def test_three_disconnect_fallbacks_present(self):
        """目录 / 正文 / 全局事实 三处断线兜底必须都在位（防后人删掉某一条链）。"""
        _, protected = _sse_finish_task_calls()
        assert len(protected) == 3, f"断线兜底 finish_task 应为 3 处，实际 {len(protected)} 处：{protected}"

    def test_finish_task_memory_cleanup_is_unconditional(self):
        """R177 根修：内存态清理必须挂在**外层** finally（覆盖 CancelledError 路径）。"""
        src = TR_PATH.read_text(encoding="utf-8")
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "finish_task":
                target = node
                break
        assert target is not None, "finish_task 函数不存在"

        # 找出所有 `_tasks.pop(...)` 调用及其所在 try 的 finally 归属
        pops = [
            n
            for n in ast.walk(target)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "pop"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "_tasks"
        ]
        assert len(pops) == 1, f"_tasks.pop 应恰好 1 处，实际 {len(pops)} 处"

        # pop 必须位于某个 Try 的 finalbody 中，且该 Try 的 body 覆盖了整段函数体
        # （判据：pop 所在 Try 的 body 里含终态落库调用 retry_db_op）
        owner = None
        for node in ast.walk(target):
            if not isinstance(node, ast.Try):
                continue
            final_ids = {id(n) for stmt in node.finalbody for n in ast.walk(stmt)}
            if any(id(p) in final_ids for p in pops):
                owner = node
                break
        assert owner is not None, "_tasks.pop 不在 finally 中 → 取消时清理被跳过"
        body_calls = [
            n
            for stmt in owner.body
            for n in ast.walk(stmt)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "retry_db_op"
        ]
        assert body_calls, "_tasks.pop 所在的 try 未覆盖终态落库（retry_db_op）→ 落库被取消时整段清理被跳过"

    def test_terminal_db_write_is_shielded(self):
        """终态落库必须被 shield 保护（取消只中断等待，内层写仍在后台完成）。"""
        src = TR_PATH.read_text(encoding="utf-8")
        assert "asyncio.shield(retry_db_op(" in src, "终态落库未被 asyncio.shield 保护"


# =====================================================================
# C 组 · 本地配置问题不得激活全熔断快速失败（provider_factory）
# =====================================================================
class TestGateCandidatesLocalConfigNotCounted:
    @pytest.fixture(autouse=True)
    def _gate_deps(self, monkeypatch):
        from app.services.ai import provider_factory as pf

        async def _no_disabled():
            return set()

        async def _no_audit(*a, **k):
            pass

        monkeypatch.setattr(pf, "resolve_disabled_providers", _no_disabled)
        monkeypatch.setattr(pf, "_log_audit", _no_audit)
        pf.reset_all_circuit_fast_fail()
        yield
        pf.reset_all_circuit_fast_fail()

    @staticmethod
    def _cand(pname="storm", **over):
        c = {
            "provider_name": pname,
            "api_key": "sk-test",
            "base_url": "https://example.com",
            "model": "m",
            "config_id": "c1",
        }
        c.update(over)
        return c

    @pytest.mark.asyncio
    async def test_key_broken_does_not_trip_global_fast_fail(self):
        """密钥解不开 = 本地配置问题，不得激活全站 60s 快速失败。"""
        from app.services.ai import provider_factory as pf

        cand = self._cand(api_key="", key_broken=True)
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await pf._gate_candidates([dict(cand)], "test")
        assert not pf._is_all_circuit_fast_fail(), "本地密钥问题被计入全局熔断闸门 → 60s 内全站所有模块停摆"

    @pytest.mark.asyncio
    async def test_no_key_does_not_trip_global_fast_fail(self):
        """没填 Key = 本地配置问题，不得激活全站 60s 快速失败。"""
        from app.services.ai import provider_factory as pf

        cand = self._cand(api_key="")
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await pf._gate_candidates([dict(cand)], "test")
        assert not pf._is_all_circuit_fast_fail()

    @pytest.mark.asyncio
    async def test_provider_side_thinking_dead_still_trips_fast_fail(self):
        """厂商侧不可用（思考吞噬冷却）**仍要**计入闸门（R110 语义不得回归）。"""
        from app.services.ai import provider_factory as pf

        monkey = pf._is_thinking_dead
        pf._is_thinking_dead = lambda pname: True
        try:
            cand = self._cand(pname="sensetime")
            for _ in range(3):
                with pytest.raises(RuntimeError):
                    await pf._gate_candidates([dict(cand)], "test")
        finally:
            pf._is_thinking_dead = monkey
        assert (
            pf._is_all_circuit_fast_fail()
        ), "厂商侧不可用未计入全熔断闸门 → R110 的跨章空转防护失效（每章各自 probe 150-300s）"


# =====================================================================
# D 组 · upload_outline.save_as_outline 类型闸门（防整表清空）
# =====================================================================
@pytest.fixture
async def scheme_ctx(tmp_path):
    import app.db as _appdb
    from app.db import close_db, get_conn, init_db

    _appdb.DB_PATH = tmp_path / "r177.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, sid, pid
    await close_db()


async def _seed_upload(db, upload_id: str):
    await db.execute("INSERT INTO uploaded_outlines (id, status) VALUES (?, 'parsed')", (upload_id,))


async def _seed_sections(db, sid: str, pid: str, items: list) -> None:
    for so, (title, content) in enumerate(items):
        await db.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
            " level, sort_order, status, content, word_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, sid, pid, "", title, 1, so, "done", content, len(content)),
        )
    await db.commit()


class TestSaveAsOutlineTypeGuard:
    @pytest.mark.parametrize(
        "payload",
        [{"a": 1}, "abc", 123, {"title": "x"}],
        ids=["dict", "str", "int", "dict-with-title"],
    )
    async def test_non_list_payload_rejected_and_sections_preserved(self, scheme_ctx, payload):
        """非数组载荷必须 400，且**绝不能**清空该方案已有章节与正文。"""
        from app.routers.upload_outline import save_as_outline

        db, sid, pid = scheme_ctx
        await _seed_sections(db, sid, pid, [("工程概况", "正文内容A"), ("施工部署", "正文内容B")])
        up = uuid.uuid4().hex
        await _seed_upload(db, up)

        with pytest.raises(HTTPException) as e:
            await save_as_outline(up, {"scheme_id": sid, "outline": payload}, db=db)
        assert e.value.status_code == 400, f"非数组载荷必须 400，实际 {e.value.status_code}"
        assert "必须是数组" in str(e.value.detail)

        cur = await db.execute(
            "SELECT COUNT(*) c, SUM(CASE WHEN COALESCE(content,'')!='' THEN 1 ELSE 0 END) w"
            " FROM sections WHERE scheme_id=?",
            (sid,),
        )
        row = await cur.fetchone()
        assert row["c"] == 2, f"章节被清空（剩 {row['c']} 条）→ 破坏性副作用发生在成功路径"
        assert row["w"] == 2, "已生成正文被清空"

    async def test_list_payload_still_works(self, scheme_ctx):
        """正常数组载荷不得回归（闸门只拦类型错误，不拦合法请求）。"""
        from app.routers.upload_outline import save_as_outline

        db, sid, pid = scheme_ctx
        up = uuid.uuid4().hex
        await _seed_upload(db, up)
        tree = [{"id": uuid.uuid4().hex, "title": "工程概况", "children": []}]
        res = await save_as_outline(up, {"scheme_id": sid, "outline": tree}, db=db)
        assert res["ok"] is True
        cur = await db.execute("SELECT COUNT(*) c FROM sections WHERE scheme_id=?", (sid,))
        assert (await cur.fetchone())["c"] == 1

    def test_source_has_type_guard_before_write(self):
        """源码锁：类型闸门必须出现在任何 DELETE/INSERT 之前。"""
        src = UO_PATH.read_text(encoding="utf-8")
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "save_as_outline":
                target = node
                break
        assert target is not None, "save_as_outline 函数不存在"
        seg = ast.get_source_segment(src, target) or ""

        guard_pos = seg.find("isinstance(outline, list)")
        assert guard_pos >= 0, "save_as_outline 缺 isinstance(outline, list) 类型闸门"

        first_delete = seg.find("DELETE FROM sections")
        first_insert = seg.find("INSERT INTO sections")
        write_pos = min(p for p in (first_delete, first_insert) if p >= 0)
        assert guard_pos < write_pos, "类型闸门必须早于任何写操作"
