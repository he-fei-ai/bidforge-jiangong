"""目录生成模块增强 · 五项修复的回归护栏（2026-10-05）

本轮深探目录生成链路后落地的 5 项修复，全部默认向后兼容、零新增配置项：

F1 长方案终态进度缺终态 event（**一致性硬化，非当前可复现缺陷**）
    sse_handlers.generate_outline 长方案路径调
    ``update_progress(task_id, 1.0, "目录生成完成")`` 漏 ``event="completed"``，
    短方案路径一直带着。实测（probe，见下）：在当前常量
    ``MIN_INTERVAL=0.5s / MIN_DELTA=0.005`` 下，0.97→1.0 的增量 0.03 ≥ MIN_DELTA
    **照样落库 1.0**，所以「停在 0.97」在当前常量下**不可复现** —— 该调用点的
    正确性一直隐式依赖 MIN_DELTA 的数值恰好放行（把它调到 0.05 即复现停在 0.999）。
    修复 = 两条路径统一显式带 event → force=True，把终值正确性从常量巧合改成结构保证。
    下方两条用例锁的正是这层机制（节流本身 + force 语义），而非「有没有加注释」。

F2 create_section 缺导出缓存失效
    update / delete / reorder / save-outline 四条结构变更路径都有
    ``invalidate_export_cache``，唯独 create 漏接 —— 新增章节使编号顺移、
    章节树变化，旧 export_cache 行与磁盘产物成为孤儿。

F3 _save_outline_to_db 空目录分支缺导出缓存失效
    非空分支（:1197 一带）已有，空分支只清了一致性扫描缓存 ——
    清空目录删掉全部章节与图表登记，导出缓存同样作废。

F4 reset_content 缺目录生成 409 守卫
    docstring 与 test_outline_guard_endpoints 的说明都宣称「生成任务运行中
    409」，实现里却只有 content_generation_in_progress，目录生成的 409
    一条都没有 —— 与 AI 确认闸门的整表重建构成同表写写竞态。

F5 目录 / 正文跨类型互斥的 TOCTOU
    两处 pre-guard 都在路由体里，而 register_task 要等路由返回 + 响应头
    下发 + 生成器首次 next() 才执行，中间必然 await，另一路请求可能在窗口
    内通过自己的 pre-guard 并先注册 → 两路同时 running。
    互斥判定下沉到 register_task 的 _register_lock 内（同型防僵尸同一把锁），
    且**放在本任务 INSERT 之后** —— 锁把两路注册串行化，先注册者放行、
    后注册者抛 TaskTypeConflict，结构上不可能「双双 abort」。
"""
import asyncio
import json

import pytest
from app.models import SectionCreate
from app.routers.sections import _save_outline_to_db, create_section, reset_content
from app.services.ai.task_registry import (
    TaskTypeConflict,
    finish_task,
    register_task,
    update_progress,
)
from fastapi import HTTPException

_SECTIONS_SRC = "app/routers/sections.py"
_SSE_SRC = "app/routers/sse_handlers.py"


def _read(path: str) -> str:
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, path), encoding="utf-8") as f:
        return f.read()


async def _base(db, sid="s1", pid="p1"):
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)", (pid, "项目"))
    await db.execute("INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES (?,?,?)",
                     (sid, pid, "方案"))
    await db.commit()


async def _seed_section(db, sid, pid, title="工程概况") -> str:
    nid = f"sec-{title}"
    await db.execute(
        "INSERT OR IGNORE INTO sections (id, scheme_id, project_id, title, level,"
        " sort_order, status, outline_json, word_budget)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (nid, sid, pid, title, 1, 0, "empty",
         '{"id": "1", "confidence": 0.9}', 1500))
    await db.commit()
    return nid


async def _seed_export_cache(db, sid, n=1) -> None:
    for i in range(n):
        await db.execute(
            "INSERT INTO export_cache (id, scheme_id, result_path) VALUES (?,?,?)",
            (f"ec-{sid}-{i}", sid, f"/nonexistent/{sid}-{i}.docx"))
    await db.commit()


async def _export_cache_count(db, sid) -> int:
    cur = await db.execute(
        "SELECT COUNT(*) FROM export_cache WHERE scheme_id=?", (sid,))
    row = await cur.fetchone()
    return (row[0] if row else 0)


# ============================================================
# F1 · 长方案终态进度必须带 event="completed"
# ============================================================
class TestTerminalProgressCarriesCompletedEvent:
    def test_both_terminal_progress_calls_pass_event(self):
        """长/短两条终态 update_progress 调用都必须显式带 event="completed"。"""
        src = _read(_SSE_SRC)
        hits = [ln for ln in src.splitlines()
                if 'update_progress(task_id, 1.0, "目录生成完成"' in ln]
        assert len(hits) == 2, f"预期长/短两条终态进度调用，实测 {len(hits)} 条: {hits}"
        for ln in hits:
            assert 'event="completed"' in ln, f"终态进度缺 event=completed: {ln.strip()}"

    async def test_throttled_progress_is_not_written_without_event(self, db_conn):
        """反例锚定：同一 0.5s 窗口内、增量 < _PROGRESS_DB_MIN_DELTA 的调用被节流吞掉。

        实测常量：MIN_INTERVAL=0.5s / MIN_DELTA=0.005。0.9 → 0.902（Δ0.002）
        两条都命中「该节流」的条件 → DB 停在 0.9、内存态实时到 0.902。
        若节流本身被误改成「每次都写」，本例会失败，说明 F1 依赖的是真实机制。
        """
        tid = await register_task("outline_generation", "", "s1")
        try:
            await update_progress(tid, 0.9, "阶段一")
            await update_progress(tid, 0.902, "阶段二")   # Δ=0.002 < 0.005 → 应被节流
            cur = await db_conn.execute(
                "SELECT progress FROM task_registry WHERE id=?", (tid,))
            got = (await cur.fetchone())[0]
            assert got == 0.9, f"节流失效，实测 progress={got}"
            from app.services.ai.task_registry import _tasks
            assert abs(_tasks[tid].get("progress", 0) - 0.902) < 1e-9, (
                "内存态应始终实时（节流只作用于 DB）")
        finally:
            await finish_task(tid, "completed")

    async def test_terminal_event_bypasses_throttle_and_writes_1_0(self, db_conn):
        """F1 的机制测试：终态 event → force=True，绕过节流强制写 1.0。

        对照组（同窗口、同增量）：0.999 之后不带 event 调 1.0 → Δ=0.001 < 0.005
        且距上次落库 <0.5s → **DB 停在 0.999**；带 event 则必须写到 1.0。
        这正是两条路径都必须显式带 event 的原因 —— 终值正确性不得依赖
        MIN_DELTA 的数值恰好放行（把 MIN_DELTA 调到 0.05 即可复现停在 0.999）。
        """
        # 对照：无 event → 被节流，停在 0.999
        t_no = await register_task("outline_generation", "", "s1")
        # 修复：带 event → force 落库 1.0
        t_ev = await register_task("outline_generation", "", "s2")
        try:
            await update_progress(t_no, 0.999, "接近完成")
            await update_progress(t_ev, 0.999, "接近完成")
            await update_progress(t_no, 1.0, "目录生成完成")
            await update_progress(t_ev, 1.0, "目录生成完成", event="completed")
            cur = await db_conn.execute(
                "SELECT id, progress FROM task_registry WHERE id IN (?,?)",
                (t_no, t_ev))
            rows = {r[0]: r[1] for r in await cur.fetchall()}
            assert rows[t_no] == 0.999, (
                f"对照组应被节流停在 0.999，实测 {rows[t_no]}")
            assert rows[t_ev] == 1.0, (
                f"带 event 的终态必须落库 1.0，实测 {rows[t_ev]}")
        finally:
            await finish_task(t_no, "completed")
            await finish_task(t_ev, "completed")


# ============================================================
# F2 / F3 · 结构变更必须失效导出缓存
# ============================================================
class TestExportCacheInvalidationOnStructChange:
    async def test_create_section_clears_export_cache(self, db_conn):
        await _base(db_conn)
        await _seed_export_cache(db_conn, "s1")
        assert await _export_cache_count(db_conn, "s1") == 1
        await create_section("s1", SectionCreate(title="新增章节"), db=db_conn)
        assert await _export_cache_count(db_conn, "s1") == 0, (
            "create_section 未失效导出缓存：新增章节使编号顺移，旧产物成孤儿")

    async def test_save_outline_empty_clears_export_cache(self, db_conn):
        await _base(db_conn)
        await _seed_export_cache(db_conn, "s1")
        assert await _export_cache_count(db_conn, "s1") == 1
        r = await _save_outline_to_db(db_conn, "s1", [], source="测试")
        assert r.get("count") == 0
        assert await _export_cache_count(db_conn, "s1") == 0, (
            "清空目录分支未失效导出缓存")

    async def test_save_outline_nonempty_still_clears_export_cache(self, db_conn):
        """正向对照：非空分支的既有失效不得被本轮改动破坏。"""
        await _base(db_conn)
        await _seed_export_cache(db_conn, "s1")
        await _save_outline_to_db(db_conn, "s1", [{"title": "A"}, {"title": "B"}],
                                  source="测试")
        assert await _export_cache_count(db_conn, "s1") == 0

    async def test_create_keeps_consistency_cache_invalidation(self, db_conn):
        """回归对照：create 的一致性扫描缓存失效不得因本轮改动被顶掉。"""
        from app.routers.sections import invalidate_consistency_scan_cache  # noqa: F401
        await _base(db_conn)
        await db_conn.execute(
            "INSERT INTO consistency_scan_cache"
            " (section_id, scheme_id, content_hash, context_hash, rows_json)"
            " VALUES ('a','s1','h1','ctx1','[]')")
        await db_conn.commit()
        await create_section("s1", SectionCreate(title="新增章节"), db=db_conn)
        cur = await db_conn.execute(
            "SELECT COUNT(*) FROM consistency_scan_cache WHERE scheme_id=?", ("s1",))
        assert (await cur.fetchone())[0] == 0

    def test_both_missing_sites_are_now_wired(self):
        """静态双锁：两个修复点在源码里都必须真的出现 invalidate_export_cache。"""
        src = _read(_SECTIONS_SRC)
        i_create = src.find("async def create_section")
        assert i_create > 0
        i_update = src.find("async def update_section")
        create_body = src[i_create:i_update]
        assert "invalidate_export_cache" in create_body, "create_section 仍缺导出缓存失效"

        i_save = src.find("async def _save_outline_to_db")
        assert i_save > 0
        i_next = src.find("\nasync def ", i_save + 10)
        save_body = src[i_save:i_next if i_next > 0 else len(src)]
        assert "invalidate_export_cache" in save_body, "_save_outline_to_db 仍缺导出缓存失效"


# ============================================================
# F4 · reset_content 的目录生成 409 守卫
# ============================================================
class TestResetContentOutlineGuard:
    async def test_reset_blocked_while_outline_running(self, db_conn):
        await _base(db_conn)
        await _seed_section(db_conn, "s1", "p1")
        tid = await register_task("outline_generation", "", "s1")
        try:
            with pytest.raises(HTTPException) as ei:
                await reset_content("s1", db=db_conn)
            assert ei.value.status_code == 409
            assert "目录" in str(ei.value.detail)
        finally:
            await finish_task(tid, "completed")

    async def test_reset_blocked_while_outline_paused(self, db_conn):
        """守卫口径与其它端点一致：running/paused 都拦。"""
        await _base(db_conn)
        tid = await register_task("outline_generation", "", "s1")
        try:
            from app.services.ai.task_registry import request_control
            request_control(tid, "pause")   # 同步函数，返回 bool
            with pytest.raises(HTTPException) as ei:
                await reset_content("s1", db=db_conn)
            assert ei.value.status_code == 409
        finally:
            await finish_task(tid, "completed")

    async def test_reset_allowed_after_terminal_and_content_path_kept(self, db_conn):
        """终态不误拦；原有的正文生成守卫不得被删除。"""
        await _base(db_conn)
        await _seed_section(db_conn, "s1", "p1")
        tid = await register_task("outline_generation", "", "s1")
        await finish_task(tid, "completed")
        r = await reset_content("s1", db=db_conn)
        assert r.get("ok") is True

        tid2 = await register_task("content_generation", "", "s1")
        try:
            with pytest.raises(HTTPException) as ei:
                await reset_content("s1", db=db_conn)
            assert ei.value.status_code == 409
            assert "正文" in str(ei.value.detail)
        finally:
            await finish_task(tid2, "completed")

    def test_source_wires_outline_guard(self):
        src = _read(_SECTIONS_SRC)
        i = src.find("async def reset_content")
        assert i > 0
        j = src.find("\nasync def ", i + 10)
        body = src[i:j if j > 0 else len(src)]
        assert "outline_generation_in_progress(scheme_id)" in body, (
            "reset_content 缺目录生成 409 守卫")
        assert "content_generation_in_progress(scheme_id)" in body, (
            "reset_content 的正文生成 409 守卫被误删")


# ============================================================
# F5 · 跨类型互斥（register_task 锁内二次判定）
# ============================================================
class TestCrossTypeMutexAtRegister:
    async def test_legacy_signature_unchanged(self, db_conn):
        """conflict_types 缺省 → 行为与历史逐字一致（正文在跑也允许注册目录任务）。"""
        await register_task("content_generation", "", "s1")
        tid = await register_task("outline_generation", "", "s1")
        assert tid, "缺省签名不得改变既有行为"

    async def test_no_conflict_registers_running(self, db_conn):
        tid = await register_task("outline_generation", "", "s1",
                                  conflict_types=("content_generation",))
        cur = await db_conn.execute(
            "SELECT status FROM task_registry WHERE id=?", (tid,))
        assert (await cur.fetchone())[0] == "running"

    async def test_conflict_raises_with_task_id_and_marks_self_failed(self, db_conn):
        winner = await register_task("content_generation", "", "s1")
        with pytest.raises(TaskTypeConflict) as ei:
            await register_task("outline_generation", "", "s1",
                                conflict_types=("content_generation",))
        # 冲突方必须拿到可下发的 task_id（前端轮询它会读到 failed + message）
        loser = ei.value.task_id
        assert loser and isinstance(loser, str)
        assert ei.value.conflicts == ["content_generation"]
        cur = await db_conn.execute(
            "SELECT status, message FROM task_registry WHERE id=?", (loser,))
        row = await cur.fetchone()
        assert row[0] == "failed", "冲突任务必须自标 failed，不得留在 running 泄漏"
        assert "互斥" in (row[1] or "")
        # 先注册者不受影响
        cur = await db_conn.execute(
            "SELECT status FROM task_registry WHERE id=?", (winner,))
        assert (await cur.fetchone())[0] == "running"
        # 冲突任务不得进内存控制表（否则 has_active_task 永远 True → 泄漏）
        from app.services.ai.task_registry import has_active_task
        assert has_active_task(loser) is False
        await finish_task(winner, "completed")

    async def test_other_scheme_not_blocked(self, db_conn):
        await register_task("content_generation", "", "other")
        tid = await register_task("outline_generation", "", "s1",
                                  conflict_types=("content_generation",))
        assert tid

    async def test_concurrent_opposite_types_exactly_one_wins(self, db_conn):
        """锁串行化 ⇒ 恰好一路成功、一路抛冲突（结构上不可能「双双 abort」）。"""
        results = await asyncio.gather(
            register_task("outline_generation", "", "s1",
                          conflict_types=("content_generation",)),
            register_task("content_generation", "", "s1",
                          conflict_types=("outline_generation",)),
            return_exceptions=True)
        oks = [r for r in results if isinstance(r, str)]
        conflicts = [r for r in results if isinstance(r, TaskTypeConflict)]
        assert len(oks) == 1 and len(conflicts) == 1, (
            f"必须恰好一路成功一路冲突，实测 oks={oks} conflicts={conflicts}")
        # 输家 row 已 failed，赢家仍 running
        cur = await db_conn.execute(
            "SELECT status FROM task_registry WHERE id=?", (oks[0],))
        assert (await cur.fetchone())[0] == "running"
        cur = await db_conn.execute(
            "SELECT status FROM task_registry WHERE id=?", (conflicts[0].task_id,))
        assert (await cur.fetchone())[0] == "failed"

    async def test_conflict_check_is_inside_register_lock(self, db_conn):
        """A/B 保护：判定必须与注册同锁 —— 若有人把它挪回路由体，本例会失败。

        读源码断言：conflict 分支出现在 ``async with _register_lock`` 块内部。
        """
        import os
        import app.services.ai.task_registry as tr
        path = tr.__file__
        with open(path, encoding="utf-8") as f:
            src = f.read()
        i_lock = src.find("async with _register_lock:")
        assert i_lock > 0
        # 锁块的结束 = 下一个同缩进的顶层语句（此处为 4 空格的 pause_event）
        i_end = src.find("\npause_event = asyncio.Event()", i_lock)
        lock_body = src[i_lock:i_end if i_end > 0 else len(src)]
        assert "if conflict_types:" in lock_body, (
            "跨类型互斥判定被移出了 _register_lock —— TOCTOU 回归")
        assert lock_body.index("INSERT INTO task_registry") < lock_body.index("if conflict_types:"), (
            "判定必须在本任务 INSERT 之后（否则会出现两路互相 abort）")

    def test_both_sse_entry_points_pass_conflict_types(self):
        """接线静态锁：**三条** SSE 生成链路都必须接跨类型互斥。

        ✅ 2026-10-06 扩容：原断言硬编码 ``== 2``（目录 + 正文），本轮把
        ``generate_facts`` 也接入 ``_register_task_exclusive`` 后变成 3。
        魔数在「新增第四条链路」时会再次失效 —— 故本例改为**从路由装饰器
        推导**应接互斥的链路集合：凡 ``kind`` 落在
        ``{outline, content, facts}_generation`` 的 event_stream 都必须
        （a）调 _register_task_exclusive 且带非空 conflict_types、
        （b）消费 ``_conflict`` 并 yield error 事件。
        这样日后新增/移除链路无需改护栏，也不会漏检。
        """
        import ast
        import re

        src = _read(_SSE_SRC)
        tree = ast.parse(src)

        # 1) 找出所有「注册了 generation 类任务」的端点函数
        gen_fns = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body_src = ast.get_source_segment(src, node) or ""
            m = re.search(r'_register_task_exclusive\(\s*"(\w+)"', body_src)
            if m:
                gen_fns[node.name] = (m.group(1), body_src)

        expected_kinds = {"outline_generation", "content_generation",
                          "facts_generation"}
        got_kinds = {k for k, _ in gen_fns.values()}
        assert expected_kinds <= got_kinds, (
            f"以下生成链路未接跨类型互斥：{sorted(expected_kinds - got_kinds)}")

        # 2) 每条链路都必须消费冲突结果
        for fn_name, (kind, body_src) in gen_fns.items():
            if kind not in expected_kinds:
                continue
            assert "_conflict is not None" in body_src, (
                f"{fn_name}（{kind}）未消费跨类型互斥冲突")
            assert "'event': 'error'" in body_src, (
                f"{fn_name}（{kind}）未把冲突转成 error 事件")

        # 3) 事实链路必须同时与目录、正文互斥（它读写同一批 global_facts 行）
        assert '("outline_generation", "content_generation")' in src, (
            "generate_facts 未同时声明与目录/正文互斥")

        # 判定只在 helper 里 catch 一次 —— event_stream 内不得新增 try 块
        # （既有护栏要求进度/计数器预初始化在外层 try 之前，见
        #  test_content_deep_audit_20260923.TestEarlyFailureGuard）
        assert src.count("except TaskTypeConflict") == 1, (
            "TaskTypeConflict 必须只在 _register_task_exclusive 里 catch")
        i = src.find("async def _register_task_exclusive")
        assert i > 0, "缺少 _register_task_exclusive helper"

    async def test_helper_maps_conflict_to_return_value(self, db_conn):
        from app.routers.sse_handlers import _register_task_exclusive
        tid, tc = await _register_task_exclusive("content_generation", "", "s1")
        assert tc is None and tid
        tid2, tc2 = await _register_task_exclusive(
            "outline_generation", "", "s1", ("content_generation",))
        assert tid2 is None and isinstance(tc2, TaskTypeConflict)
        assert tc2.task_id and tc2.conflicts == ["content_generation"]
        await finish_task(tid, "completed")


# ============================================================
# F5 · 端到端：真实路由函数 + 真实 event_stream 复现 TOCTOU 窗口
# ============================================================
class TestCrossTypeMutexE2E:
    """不走 ASGI 传输，直接驱动路由函数与其返回的流式生成器 —— 这样窗口两侧
    的顺序可以**确定性**编排（ASGITransport 是否缓冲整包会导致时机不可控）：

    路由体 pre-guard 执行 → 路由返回 StreamingResponse → **此刻才**注册正文任务
    （= 复现 TOCTOU 窗口）→ 消费首帧 → 断言拿到 error 事件而非第 2 路生成。
    """

    async def test_route_pre_guard_blocks_outline_when_content_running(self, db_conn):
        from app.routers.sse_handlers import generate_outline
        await _base(db_conn, "s-e2e1", "p-e2e1")
        tid = await register_task("content_generation", "p-e2e1", "s-e2e1")
        try:
            with pytest.raises(HTTPException) as ei:
                await generate_outline("s-e2e1", None, db_conn)
            assert ei.value.status_code == 409
            assert "正文" in str(ei.value.detail), "跨类型 pre-guard 文案必须点名正文任务"
        finally:
            await finish_task(tid, "stopped", "测试清理")

    async def test_window_race_is_caught_at_registration(self, db_conn, monkeypatch):
        """pre-guard 全过、正文任务在窗口内才注册 → 首帧必须是 error 事件。"""
        from app.routers import sse_handlers as sh
        await _base(db_conn, "s-e2e2", "p-e2e2")

        # 安全网：互斥若真的失效（回归），生成链不得把测试拖进真实 AI 调用
        async def _boom(*_a, **_k):
            raise RuntimeError("E2E 安全网：互斥失效，目录生成不应被真正触发")
        monkeypatch.setattr(sh, "chat_with_fallback", _boom, raising=False)
        monkeypatch.setattr(sh, "collect_json_response", _boom, raising=False)

        # ① 路由体 pre-guard 通过（此刻无任何任务）→ 返回流式响应
        resp = await sh.generate_outline("s-e2e2", None, db_conn)

        # ② 窗口内：正文任务此时才注册（正是 pre-guard 与注册之间的 await 间隙）
        ctid = await register_task("content_generation", "p-e2e2", "s-e2e2")

        # ③ 消费生成器首帧后立即收束，避免进入真实生成
        frames: list[str] = []
        agen = resp.body_iterator
        try:
            async for chunk in agen:
                frames.append(chunk if isinstance(chunk, str)
                              else chunk.decode("utf-8", "replace"))
                if len(frames) >= 3:
                    break
        finally:
            close = getattr(agen, "aclose", None)
            if close is not None:
                await close()
        assert frames, "生成器未产出任何帧"
        # 首个 data 帧必须是跨类型冲突的 error，而不是 progress / ping
        first_data = next((f for f in frames if f.startswith("data:")), "")
        payload = json.loads(first_data[len("data:"):].strip())
        assert payload.get("event") == "error", f"首帧不是 error: {payload}"
        assert "正文" in payload.get("message", ""), "错误文案必须点名正文生成在跑"
        assert not any('"event": "progress"' in f or '"event":"progress"' in f
                       for f in frames), "冲突后不得继续下发生成进度"

        # ④ 落库终态：赢家 running、输家 failed、未泄漏第 2 路 running 目录任务
        cur = await db_conn.execute(
            "SELECT status FROM task_registry WHERE id=?", (ctid,))
        assert (await cur.fetchone())[0] == "running", "正文任务不得被目录冲突波及"
        cur = await db_conn.execute(
            "SELECT status, message FROM task_registry WHERE task_type=? AND scheme_id=?",
            ("outline_generation", "s-e2e2"))
        rows = await cur.fetchall()
        assert rows, "冲突方必须落库一条目录任务记录（供前端轮询读到失败原因）"
        assert all(r[0] == "failed" for r in rows), f"目录任务残留 running: {rows}"
        assert all("互斥" in (r[1] or "") for r in rows)
        await finish_task(ctid, "stopped", "测试清理")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
