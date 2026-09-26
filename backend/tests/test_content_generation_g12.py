"""正文生成模块第十二轮审查回归（2026-09-20）

本轮针对「正文生成链路 + 跨模块协作」的五个缺陷做行为与接线锁定：

- G12-1  gen_one 的停止检查点 yield section_error（reason='任务已停止'）后 return。
         前端把 section_error 一律落成 status="failed"，于是被停止打断的章节显示
         「失败」，其余未完成章节由 stopped 分支收尾成「已跳过」，最终任务态又是
         stopped —— 三条叙述互相矛盾；且该章从未进 _failed_reasons，终态
         failed_sections 里查不到它。修复：抛 CancelledError，统一走 except 分支
         （is_stopped=True 时不发明细事件、不标 failed、不改库）。
- G12-2  _persist_section 在锁内 apply_inline_chart_plan 裁剪正文**之前**就算好了
         word_count / word_status。fast 档多章并行触发事务内复核超限裁剪时，
         库里字数、SSE 载荷字数、超字数章节计数（_stat["over"]）三处口径不一致。
         修复：裁剪之后按【最终落库正文】重算。
- G12-3  正文终态 checkpoint 五处各自手拼 `{**_ckpt}`，而 _ckpt 只有
         done/total/failed_sections/words —— 白名单里声明的 failed_count 恒为空
        （死字段），run_words / over_count 从未落库。断线或刷新后「这次写了多少
         字、几章超字数、失败几章」信息永久丢失。
         修复：收口到 _content_ckpt_payload 单一拼装点 + 白名单补齐。
- G12-4  sections.update_section / reset_content 的 409 竞态守卫只校验
         type + scheme_id，**不校验状态**。task_control 的 stop 走 set_task_status
         （只改状态、不 pop 内存态），finish_task 又可能在写库阶段抛异常走不到
         _tasks.pop —— 残留一个 stopped 条目就会让本方案所有章节的手工保存与重置
         永久 409，只能重启后端。修复：状态感知守卫（只拦 running/paused）
         + 两处调用点收口到 content_generation_in_progress。
- G12-5  finish_task 把「写 DB」与「pop 内存态」写成顺序语句，任一步抛异常即跳出，
         _tasks[task_id] 永久残留 → 直接触发 G12-4。修复：写库与广播各自
         try/except，内存清理放进 finally 无条件执行。
"""
import asyncio
import inspect
import re

import pytest
from fastapi import HTTPException

import app.routers.sections as sec
import app.routers.sse_handlers as sh
import app.services.ai.task_registry as tr


# ---------------------------------------------------------------------------
# G12-1：用户停止 ≠ 章节失败
# ---------------------------------------------------------------------------
class TestStopIsNotFailure:
    @staticmethod
    def _gen_one_src() -> str:
        src = inspect.getsource(sh.generate_content)
        i = src.index("async def gen_one(")
        j = src.index("async def guarded_gen(", i)
        return src[i:j]

    def test_stopped_checkpoint_raises_cancelled(self):
        """停止检查点必须抛 CancelledError，不得 yield section_error。"""
        src = self._gen_one_src()
        i = src.index("if is_stopped(task_id):")
        assert "raise asyncio.CancelledError()" in src[i:i + 1400], (
            "gen_one 的停止检查点必须抛 CancelledError —— 旧实现 yield "
            "section_error 后 return，前端落成 failed 日志，与 stopped 终态矛盾")

    def test_no_stopped_reason_section_error_in_gen_one(self):
        """gen_one 内不得再出现 reason='任务已停止' 的 section_error 事件。"""
        src = self._gen_one_src()
        assert '"reason": "任务已停止"' not in src, (
            "停止语义修复被回退：该事件会让被停止打断的章节在前端显示「失败」")

    def test_cancelled_branch_gated_by_is_stopped(self):
        """except CancelledError 分支必须以 is_stopped 为闸门（停止时静默退出）。"""
        src = inspect.getsource(sh.generate_content)
        i = src.index("async def gen_one(")
        j = src.index("except asyncio.CancelledError:", i)
        tail = src[j:j + 500]
        assert "if not db_written and not is_stopped(task_id):" in tail, (
            "停止时不得发 section_error、不得标记章节 failed（会污染质量统计，"
            "并使 missing 模式误选这些章节重新生成）")

    def test_guarded_gen_stop_uses_same_semantics(self):
        """guarded_gen 的停止检查点与 gen_one 同语义（抛 CancelledError）。

        ✅ 2026-09-25（陈旧断言修复）：旧实现用固定宽度窗口 `src[i:i + 700]`
        扫描。E3 重构给 guarded_gen 加了两行注释后，`raise asyncio` 落在偏移
        687 处、被 700 字符窗口从中间切断（`CancelledError()` 在窗口外）→
        断言必然失败，与被测语义无关。固定宽度窗口本身即脆弱断言（任何注释/
        docstring 增删都会误报），改为扫描到下一个同级 def/装饰器为止。
        """
        src = inspect.getsource(sh.generate_content)
        i = src.index("async def guarded_gen(")
        rest = src[i + 1:]
        m = re.search(r"\n    (?:async def |def |@)", rest)
        block = rest[:m.start()] if m else rest
        assert "raise asyncio.CancelledError()" in block, (
            "guarded_gen 必须与本文件其它检查点一致的协作式停止语义（不记为章节失败）"
        )


# ---------------------------------------------------------------------------
# G12-2：字数必须基于【最终落库正文】计算
# ---------------------------------------------------------------------------
class TestWordCountAfterClip:
    def test_recompute_after_apply_inline_chart_plan(self):
        """apply_inline_chart_plan 裁剪正文之后必须重算 wc / word_status。"""
        src = inspect.getsource(sh.generate_content)
        i = src.index("async def _persist_section(")
        j = src.index("done_ids.add(section_id)", i)
        block = src[i:j]
        pos = block.index("await apply_inline_chart_plan(")
        tail = block[pos:]
        assert "text_word_count(content)" in tail, (
            "锁内裁剪可能删掉正文片段，字数必须在裁剪之后重算，"
            "否则库里/SSE 载荷/超字数计数三处口径漂移")
        assert "word_status_for(" in tail, (
            "word_status 必须与裁剪后字数同批计算，否则落库状态与字数不匹配")


# ---------------------------------------------------------------------------
# G12-3：正文 checkpoint 载荷契约（单一拼装点 + 白名单补齐）
# ---------------------------------------------------------------------------
class TestContentCheckpointContract:
    def test_whitelist_covers_all_terminal_fields(self):
        kind, fields = sh._CHECKPOINT_KINDS["content_generation"]
        assert kind == "content_result"
        for f in ("failed_count", "failed_sections", "run_words", "over_count",
                  "done", "total", "words", "word_count"):
            assert f in fields, f"白名单缺 {f}：断线重挂无法还原该字段"

    def test_payload_builder_carries_terminal_fields(self):
        src = inspect.getsource(sh.generate_content)
        i = src.index("def _content_ckpt_payload(")
        j = src.index("async def _build_generation_context(", i)
        block = src[i:j]
        for f in ("failed_count", "failed_sections", "run_words", "over_count"):
            assert f in block, f"checkpoint 载荷缺 {f}（旧实现为死字段/未落库）"

    def test_all_checkpoint_sites_use_single_builder(self):
        """七处终态 checkpoint 调用点全部收口，不再各自手拼 {**_ckpt}。"""
        src = inspect.getsource(sh.generate_content)
        # 2026-09-23：新增「空任务（没有待生成章节）」收尾点，同样走唯一拼装点
        assert src.count("_content_ckpt_payload(") == 8, (
            "应为 1 处定义 + 7 处调用（stopped / 全章失败 / completed / "
            "整批失败 / 断线兜底 / 服务端取消 / 空任务）")
        assert "**_ckpt" not in src, "仍存在绕过拼装点的 {**_ckpt} 手拼"

    def test_attach_checkpoint_result_passes_through_new_fields(self):
        """白名单透传行为：run_words / over_count / failed_count 不得被过滤，
        内部字段不得泄漏。"""
        import json as _json
        ckpt = {
            "kind": "content_result", "event": "stopped",
            "message": "用户已停止", "done": 3, "total": 5,
            "failed_count": 2,
            "failed_sections": [{"section_id": "s1", "reason": "超时"}],
            "words": 4200, "run_words": 4200, "over_count": 1,
            "word_count": 9000, "_internal": "secret",
        }
        row = {"task_type": "content_generation",
               "checkpoint_json": _json.dumps(ckpt, ensure_ascii=False)}
        result: dict = {}
        sh._attach_checkpoint_result(row, result)
        cr = result.get("content_result")
        assert cr, "content_result 未透传"
        assert cr["run_words"] == 4200
        assert cr["over_count"] == 1
        assert cr["failed_count"] == 2
        assert cr["failed_sections"][0]["section_id"] == "s1"
        assert "_internal" not in cr, "白名单失效：内部字段泄漏给前端"

    def test_attach_skips_mismatched_kind_or_empty(self):
        row = {"task_type": "outline_generation", "checkpoint_json": None}
        result: dict = {}
        sh._attach_checkpoint_result(row, result)
        assert result == {}
        # kind 不匹配同样跳过
        import json as _json
        row2 = {"task_type": "content_generation",
                "checkpoint_json": _json.dumps({"kind": "outline_result", "outline": []})}
        result2: dict = {}
        sh._attach_checkpoint_result(row2, result2)
        assert result2 == {}

    def test_attach_survives_malformed_json(self):
        row = {"task_type": "content_generation", "checkpoint_json": "{不是JSON"}
        result: dict = {}
        sh._attach_checkpoint_result(row, result)   # 不得抛异常
        assert result == {}


# ---------------------------------------------------------------------------
# G12-4：409 竞态守卫状态感知
# ---------------------------------------------------------------------------
class TestContentGenerationGuard:
    @staticmethod
    def add_task(scheme_id: str = "sc1", *, task_type: str = "content_generation",
                 status: str = "running") -> str:
        """往 task_registry 内存态塞一个任务条目（测试专用）。"""
        tid = f"t-{task_type}-{scheme_id}-{status}"
        tr._tasks[tid] = {
            "type": task_type, "status": status, "scheme_id": scheme_id,
            "progress": 0.5, "message": "",
            "pause_event": asyncio.Event(), "stop_event": asyncio.Event(),
            "child_tasks": set(),
        }
        return tid

    def test_no_task(self):
        tr._tasks.clear()
        assert sec.content_generation_in_progress("sc1") is None

    def test_running_blocks(self):
        self.add_task(status="running")
        assert sec.content_generation_in_progress("sc1") == "running"

    def test_paused_blocks(self):
        """paused 也是「在跑」（AI 调用仍在飞、落库会继续）→ 必须拦截。"""
        self.add_task(status="paused")
        assert sec.content_generation_in_progress("sc1") == "paused"

    @pytest.mark.parametrize("status", ["stopped", "failed", "completed"])
    def test_terminal_entries_do_not_block(self, status):
        """G12-4 回归：终态残留条目不得阻塞手工保存 / 重置正文。

        旧实现只看 type + scheme_id，stopped 残留会让本方案所有章节永久 409。
        """
        tr._tasks.clear()
        self.add_task(status=status)
        assert sec.content_generation_in_progress("sc1") is None, (
            f"status={status} 的任务不应阻塞手工编辑（它是终态残留）")

    def test_missing_status_ignored(self):
        """脏数据（无 status 字段）按终态处理：不阻塞。

        真实链路里 register_task 必定写入 status='running'，只有异常残留的
        条目才会缺字段；缺字段时误拦会让用户永久无法编辑（比多放一次更糟糕），
        故按「非在跑」处理。真在跑的任务由 running/paused 两个值精确命中。
        """
        tr._tasks.clear()
        tr._tasks["t-nostatus"] = {"type": "content_generation", "scheme_id": "sc1"}
        assert sec.content_generation_in_progress("sc1") is None

    def test_other_scheme_ignored(self):
        tr._tasks.clear()
        self.add_task(scheme_id="other-scheme", status="running")
        assert sec.content_generation_in_progress("sc1") is None

    def test_other_task_type_ignored(self):
        tr._tasks.clear()
        self.add_task(task_type="outline_generation", status="running")
        assert sec.content_generation_in_progress("sc1") is None


async def _seed_scheme(db, scheme_id: str = "sc1") -> None:
    """插入最小 projects / schemes / sections 行（手工保存与重置正文的前置数据）。"""
    await db.execute(
        "INSERT INTO projects (id, name) VALUES (?, ?)", ("p1", "项目A"))
    await db.execute(
        "INSERT INTO schemes (id, project_id, name, type) VALUES (?, ?, ?, ?)",
        (scheme_id, "p1", "深基坑专项方案", "深基坑"))
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level,"
        " status, content, word_count, word_budget, sort_order) VALUES"
        " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("s1", scheme_id, "p1", "", "第一章 工程概况", 1, "generated",
         "旧正文内容", 100, 1500, 0))
    await db.commit()


@pytest.mark.asyncio
async def test_update_section_not_blocked_by_zombie_task(db_conn):
    """G12-4 端到端：残留 stopped 任务时手工保存必须成功（旧实现永久 409）。"""
    await _seed_scheme(db_conn)
    TestContentGenerationGuard.add_task(scheme_id="sc1", status="stopped")
    res = await sec.update_section(
        "sc1", "s1", sec.SectionUpdate(content="手工编辑后的正文"), db_conn)
    assert res["ok"] is True
    cur = await db_conn.execute("SELECT content, word_count FROM sections WHERE id='s1'")
    row = await cur.fetchone()
    assert row[0] == "手工编辑后的正文"
    assert row[1] == len("手工编辑后的正文")


@pytest.mark.asyncio
async def test_update_section_still_blocked_by_running_task(db_conn):
    """回归防护：真正在跑（running / paused）时必须继续 409。"""
    await _seed_scheme(db_conn)
    TestContentGenerationGuard.add_task(scheme_id="sc1", status="running")
    with pytest.raises(HTTPException) as ei:
        await sec.update_section(
            "sc1", "s1", sec.SectionUpdate(content="x"), db_conn)
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_reset_content_not_blocked_by_zombie_task(db_conn):
    """G12-4 端到端（重置路径）：终态残留不再永久 409。"""
    await _seed_scheme(db_conn)
    TestContentGenerationGuard.add_task(scheme_id="sc1", status="failed")
    res = await sec.reset_content("sc1", db_conn)
    assert res["ok"] is True
    assert res["cleared"] == 1
    cur = await db_conn.execute(
        "SELECT content, word_count, status, word_status FROM sections WHERE id='s1'")
    row = await cur.fetchone()
    assert row[0] == ""
    assert row[1] == 0
    assert row[2] == "empty"
    assert row[3] == "normal"


@pytest.mark.asyncio
async def test_reset_content_still_blocked_by_running_task(db_conn):
    await _seed_scheme(db_conn)
    TestContentGenerationGuard.add_task(scheme_id="sc1", status="paused")
    with pytest.raises(HTTPException) as ei:
        await sec.reset_content("sc1", db_conn)
    assert ei.value.status_code == 409


# ---------------------------------------------------------------------------
# G12-5：finish_task 异常安全清理（内存态绝不泄漏）
# ---------------------------------------------------------------------------
def _make_running_task(tid: str) -> None:
    tr._tasks[tid] = {
        "type": "content_generation", "status": "running", "scheme_id": "sc1",
        "pause_event": asyncio.Event(), "stop_event": asyncio.Event(),
        "child_tasks": set(),
    }


@pytest.mark.asyncio
async def test_finish_task_cleans_memory_even_when_db_write_fails(
        db_conn, monkeypatch):
    """G12-5：终态写库抛异常时，内存态仍必须清理（否则 G12-4 永久 409）。"""
    tid = "t-db-broken"
    _make_running_task(tid)

    async def _boom():
        raise RuntimeError("database disk image is malformed")

    monkeypatch.setattr(tr, "get_conn", _boom)
    await tr.finish_task(tid, "stopped", "用户已停止")

    assert tid not in tr._tasks, (
        "终态写库失败后 _tasks 仍残留 → 本方案手工保存/重置永久 409（G12-4）")


@pytest.mark.asyncio
async def test_finish_task_cleans_memory_even_when_broadcast_fails(
        db_conn, monkeypatch):
    """广播抛异常同样不得残留内存态。"""
    tid = "t-broadcast-broken"
    _make_running_task(tid)

    async def _boom(task_id, payload):
        raise RuntimeError("订阅者队列异常")

    monkeypatch.setattr(tr, "broadcast", _boom)
    await tr.finish_task(tid, "stopped", "客户端断开")
    assert tid not in tr._tasks


@pytest.mark.asyncio
async def test_finish_task_cancels_children_when_stopped(db_conn):
    """回归防护：非 completed 终态必须取消子任务（原「收口修复」语义不变）。"""
    tid = "t-children"
    child = asyncio.create_task(asyncio.sleep(30))
    _make_running_task(tid)
    tr._tasks[tid]["child_tasks"] = {child}
    await tr.finish_task(tid, "stopped", "用户已停止")
    await asyncio.sleep(0)
    assert child.cancelled() or child.done()
    assert tid not in tr._tasks


@pytest.mark.asyncio
async def test_finish_task_completed_keeps_children(db_conn):
    """completed 不主动 cancel 子任务（原有语义保持）。"""
    tid = "t-children-ok"
    child = asyncio.create_task(asyncio.sleep(30))
    _make_running_task(tid)
    tr._tasks[tid]["child_tasks"] = {child}
    try:
        await tr.finish_task(tid, "completed", "正文生成完成")
        assert not child.cancelled()
    finally:
        child.cancel()


@pytest.mark.asyncio
async def test_finish_task_unknown_task_is_idempotent(db_conn):
    """任务不在内存态（已 pop）时 finish_task 幂等：不报错、不残留。"""
    await tr.finish_task("t-never-existed", "stopped", "兜底")
    assert "t-never-existed" not in tr._tasks
