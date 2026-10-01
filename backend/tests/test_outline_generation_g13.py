"""目录生成模块第十三轮审查回归（2026-09-21）

本轮针对「目录生成链路 + 跨模块协作」的缺陷做行为与接线锁定：

- G13-1  未落库临时节点判定口径漂移（前端，根因）：目录树里存在两类尚未写入
         sections 表的临时 key —— local_（手工新增）与 upload_（「导入目录」识别
         结果替换本地树）。旧实现四处调用点只认 local_，导入后的整棵树被误判为
         「已落库章节」：deleteNode 误调 DELETE 404（本地节点删不掉）、
         renameNode 误调 PATCH 404 被 .catch(()=>{}) 吞掉、moveNode 误走「已同步
         到服务端」假成功分支（executemany 对不存在 id 静默 0 行）。
         前端修复：收口到 isUnsavedLocalKey（UNSAVED_KEY_PREFIXES）。
- G13-2  /save-outline 缺竞态守卫：该端点整表重建 sections（INSERT + UPDATE +
         级联 DELETE），与正文生成 _persist_section 的无条件 UPDATE 直接冲突；
         旧实现零守卫，正文生成运行中点「保存目录」会把正在写的章节删掉，
         之后 UPDATE 命中 0 行，整章正文静默丢失。
         修复：content_generation_in_progress + 新增 outline_generation_in_progress。
- G13-3  /outline-library/{id}/apply-and-save 同样整表重建：旧实现同样零守卫。
         修复：接入 content_generation_in_progress → 409。
- G13-4  清空目录后状态口径错误：_save_outline_to_db 空数组分支把 schemes.status
         写成「目录已确认」—— 该值是编译状态语义（草稿 / 目录已确认 / 已完成），
         前端头部 Tag 与 review 端点 scheme_status 都按此展示；目录已清空还标
         「已确认」会让用户与审核端同时误判目录已就绪。
         修复：回退 OUTLINE_EMPTY_STATUS（目录待生成）并清空 outline_source。
- G13-5  checkpoint 白名单丢字段：_save_outline_checkpoint /
         _finish_stopped_with_partial 写入的 checkpoint_json 始终带 event / partial，
         但 _CHECKPOINT_KINDS.outline_generation 白名单不含这两项，一过滤就丢 ——
         前端断线重挂只能靠 task_registry.status 反推，无法区分「用户主动停止
         保留的部分成果」与「AI 异常后保留的部分成果」。
         修复：白名单补齐 event / partial（与 content_generation 的 event 对齐）。
"""
import asyncio
import inspect
import json

import pytest
from fastapi import HTTPException

import app.routers.sections as sec
import app.routers.sse_handlers as sh
import app.services.ai.task_registry as tr


# ---------------------------------------------------------------------------
# 测试基建：注入任务条目 + 播种方案数据
# ---------------------------------------------------------------------------
class _TaskSeeder:
    @staticmethod
    def add(scheme_id: str = "sc1", *, task_type: str = "outline_generation",
            status: str = "running") -> str:
        """往 task_registry 内存态塞一个任务条目（与 G12 测试同一形状）。"""
        tid = f"t-{task_type}-{scheme_id}-{status}"
        tr._tasks[tid] = {
            "type": task_type, "status": status, "scheme_id": scheme_id,
            "progress": 0.5, "message": "",
            "pause_event": asyncio.Event(), "stop_event": asyncio.Event(),
            "child_tasks": set(),
        }
        return tid


async def _seed_scheme(db, scheme_id: str = "sc1") -> None:
    """插入最小 projects / schemes / sections 行（保存目录的前置数据）。

    方案预置 outline_source='ai' / status='已完成'：用于验证清空目录后
    两者都会被回退（旧实现只写 status，且写成错误的「目录已确认」）。
    """
    await db.execute("INSERT INTO projects (id, name) VALUES (?, ?)", ("p1", "项目A"))
    await db.execute(
        "INSERT INTO schemes (id, project_id, name, type) VALUES (?, ?, ?, ?)",
        (scheme_id, "p1", "深基坑专项方案", "深基坑"))
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level,"
        " status, content, word_count, word_budget, sort_order) VALUES"
        " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("s1", scheme_id, "p1", "", "第一章 工程概况", 1, "generated",
         "旧正文内容", 100, 1500, 0))
    await db.execute(
        "UPDATE schemes SET outline_source=?, status=? WHERE id=?",
        ("ai", "已完成", scheme_id))
    await db.commit()


# ---------------------------------------------------------------------------
# G13-2：outline_generation_in_progress 守卫本体
# ---------------------------------------------------------------------------
class TestOutlineGenerationGuard:
    def test_no_task(self):
        tr._tasks.clear()
        assert sec.outline_generation_in_progress("sc1") is None

    def test_running_blocks(self):
        _TaskSeeder.add(status="running")
        assert sec.outline_generation_in_progress("sc1") == "running"

    def test_paused_blocks(self):
        """paused 也是「在跑」：AI 调用仍在飞，成果仍会弹闸门覆盖手工目录。"""
        _TaskSeeder.add(status="paused")
        assert sec.outline_generation_in_progress("sc1") == "paused"

    @pytest.mark.parametrize("status", ["stopped", "failed", "completed"])
    def test_terminal_entries_do_not_block(self, status):
        """终态残留条目不得阻塞保存目录（与 G12-4 同口径）。

        task_control 的 stop 走 set_task_status（只改状态、不 pop 内存态），
        finish_task 也可能在写库阶段抛异常走不到 _tasks.pop —— 终态条目必须放行。
        """
        tr._tasks.clear()
        _TaskSeeder.add(status=status)
        assert sec.outline_generation_in_progress("sc1") is None

    def test_missing_status_ignored(self):
        """脏数据（无 status 字段）按终态处理：宁可放行，不可误拦。"""
        tr._tasks.clear()
        tr._tasks["t-nostatus"] = {"type": "outline_generation", "scheme_id": "sc1"}
        assert sec.outline_generation_in_progress("sc1") is None

    def test_other_scheme_ignored(self):
        tr._tasks.clear()
        _TaskSeeder.add(scheme_id="other-scheme", status="running")
        assert sec.outline_generation_in_progress("sc1") is None

    def test_content_task_ignored(self):
        """正文生成任务不由本守卫拦截（走 content_generation_in_progress）。"""
        tr._tasks.clear()
        _TaskSeeder.add(task_type="content_generation", status="running")
        assert sec.outline_generation_in_progress("sc1") is None

    def test_delegates_to_single_entry(self):
        """两类守卫必须共用 _scheme_task_status_in_progress，禁止各写一份遍历。"""
        assert "outline_generation" in inspect.getsource(
            sec.outline_generation_in_progress)
        assert "_scheme_task_status_in_progress" in inspect.getsource(
            sec.outline_generation_in_progress)
        assert "_scheme_task_status_in_progress" in inspect.getsource(
            sec.content_generation_in_progress)


# ---------------------------------------------------------------------------
# G13-2：save_outline 端到端 409
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_save_outline_blocked_by_content_generation(db_conn):
    """正文生成运行中保存目录 → 409（否则会删掉正在被 _persist_section 写的章节）。"""
    await _seed_scheme(db_conn)
    _TaskSeeder.add(task_type="content_generation", status="running")
    with pytest.raises(HTTPException) as ei:
        await sec.save_outline("sc1", {"outline": [{"title": "新目录"}]}, db_conn)
    assert ei.value.status_code == 409
    assert "正文正在后台生成中" in ei.value.detail


@pytest.mark.asyncio
async def test_save_outline_blocked_by_outline_generation(db_conn):
    """目录生成运行中保存目录 → 409（AI 完成后的确认闸门会整表覆盖手工目录）。"""
    await _seed_scheme(db_conn)
    _TaskSeeder.add(task_type="outline_generation", status="paused")
    with pytest.raises(HTTPException) as ei:
        await sec.save_outline("sc1", {"outline": [{"title": "新目录"}]}, db_conn)
    assert ei.value.status_code == 409
    assert "目录正在后台生成中" in ei.value.detail


@pytest.mark.asyncio
async def test_save_outline_allowed_when_tasks_are_terminal(db_conn):
    """终态残留（正文 stopped + 目录 completed）不得阻塞 —— G12-4 同口径回归。"""
    await _seed_scheme(db_conn)
    _TaskSeeder.add(task_type="content_generation", status="stopped")
    _TaskSeeder.add(task_type="outline_generation", status="completed")
    res = await sec.save_outline(
        "sc1", {"outline": [{"title": "第一章 工程概况", "children": []}]}, db_conn)
    assert res["ok"] is True


@pytest.mark.asyncio
async def test_save_outline_guard_runs_before_parsing(db_conn):
    """守卫必须先于 outline 解析/落库执行（否则脏输入先报错，掩盖真实冲突）。"""
    await _seed_scheme(db_conn)
    _TaskSeeder.add(task_type="content_generation", status="running")
    with pytest.raises(HTTPException) as ei:
        await sec.save_outline("sc1", {"outline": "not-a-list"}, db_conn)
    assert ei.value.status_code == 409


# ---------------------------------------------------------------------------
# G13-3：apply-and-save 守卫接线
# ---------------------------------------------------------------------------
def test_apply_and_save_imports_and_checks_guard():
    """apply-and-save 是「整表重建 sections」的第二个入口，必须接入同一守卫。"""
    from app.routers.outline_library import apply_library_and_save
    src = inspect.getsource(apply_library_and_save)
    assert "content_generation_in_progress" in src, (
        "apply-and-save 必须接入 content_generation_in_progress —— "
        "它同样会级联 DELETE 正在被正文生成写的章节")
    assert "409" in src
# ---------------------------------------------------------------------------
# G13-4：清空目录后的状态口径
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_clear_outline_resets_status_and_source(db_conn):
    """清空目录后 status 必须回退「目录待生成」，且 outline_source 一并清空。

    旧实现写成「目录已确认」—— 编译状态语义与「方案里一个章节都没有」直接冲突，
    前端头部 Tag 与 review 端点 scheme_status 会同时误判「目录已就绪」。
    """
    await _seed_scheme(db_conn)   # 预置 outline_source='ai' / status='已完成'
    res = await sec.save_outline("sc1", {"outline": []}, db_conn)
    assert res["ok"] is True and res["count"] == 0 and res["tree"] == []
    # ✅ 2026-09-26 新增字段：清空目录同样是「正文批量丢失」，必须量化告知
    #    （seed 内有 1 个带正文的章节）。旧实现只回 {ok,count,tree}，用户在
    #    前端看不到任何提示，正文静默消失且不可恢复。
    assert res.get("cleared_content_sections") == 1, (
        "清空目录必须回传被清除正文的章节数，供前端明示用户")

    cur = await db_conn.execute(
        "SELECT status, outline_source FROM schemes WHERE id=?", ("sc1",))
    row = dict(await cur.fetchone())
    assert row["status"] == sec.OUTLINE_EMPTY_STATUS
    assert row["outline_source"] == "", "来源已随目录一起删除，保留旧来源会溯源失真"


@pytest.mark.asyncio
async def test_clear_outline_also_deletes_sections_and_charts(db_conn):
    """清空目录必须同时清掉章节与图表预测（旧行为保留，防回归）。"""
    await _seed_scheme(db_conn)
    await sec.save_outline("sc1", {"outline": []}, db_conn)
    cur = await db_conn.execute("SELECT COUNT(*) FROM sections WHERE scheme_id=?", ("sc1",))
    assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_save_nonempty_outline_keeps_confirmed_status(db_conn):
    """非空目录落库后仍统一置「目录已确认」（语义正确，保持既有行为）。"""
    await _seed_scheme(db_conn)
    await sec.save_outline(
        "sc1", {"outline": [{"title": "第一章 工程概况", "children": []}]}, db_conn)
    cur = await db_conn.execute("SELECT status FROM schemes WHERE id=?", ("sc1",))
    row = dict(await cur.fetchone())
    assert row["status"] == sec.OUTLINE_SAVED_STATUS


def test_empty_status_constants_are_distinct():
    """两个状态常量必须不同值 —— 否则「清空」与「已保存」无法区分。"""
    assert sec.OUTLINE_EMPTY_STATUS != sec.OUTLINE_SAVED_STATUS
    assert sec.OUTLINE_EMPTY_STATUS == "目录待生成"
# ---------------------------------------------------------------------------
# G13-5：outline_result checkpoint 白名单
# ---------------------------------------------------------------------------
class TestOutlineCheckpointContract:
    def test_whitelist_includes_event_and_partial(self):
        """event / partial 必须回传：否则前端无法区分停止 vs 异常的中断原因。"""
        _, fields = sh._CHECKPOINT_KINDS["outline_generation"]
        for f in ("event", "partial", "outline", "review"):
            assert f in fields, f"白名单缺 {f}"

    def test_attach_passes_event_and_partial_through(self):
        """_attach_checkpoint_result 必须透传 event / partial（白名单补齐生效）。"""
        ckpt = {
            "kind": "outline_result",
            "event": "stopped",
            "partial": True,
            "outline": [{"title": "第一章", "children": []}],
            "failed_chapters": ["第二章"],
            "failed_count": 1,
            "_internal": "不得泄漏",
        }
        row = {"task_type": "outline_generation", "checkpoint_json": json.dumps(ckpt)}
        result: dict = {}
        sh._attach_checkpoint_result(row, result)
        got = result["outline_result"]
        assert got["event"] == "stopped"
        assert got["partial"] is True
        assert got["outline"] == ckpt["outline"]
        assert got["failed_chapters"] == ["第二章"]
        assert got["failed_count"] == 1
        assert "_internal" not in got, "白名单失效：内部字段泄漏给前端"

    def test_attach_still_filters_unknown_fields(self):
        """白名单语义不变：未登记的字段仍然不泄漏。"""
        ckpt = {"kind": "outline_result", "event": "completed",
                "prompt_debug": "system prompt 明文", "outline": []}
        row = {"task_type": "outline_generation", "checkpoint_json": json.dumps(ckpt)}
        result: dict = {}
        sh._attach_checkpoint_result(row, result)
        assert "prompt_debug" not in result["outline_result"]
        assert result["outline_result"]["event"] == "completed"

    def test_attach_skips_mismatched_kind(self):
        """kind 不匹配的 checkpoint 不附加（防跨任务类型串扰）。"""
        row = {"task_type": "outline_generation",
               "checkpoint_json": json.dumps({"kind": "content_result", "outline": []})}
        result: dict = {}
        sh._attach_checkpoint_result(row, result)
        assert result == {}

    def test_attach_skips_empty_checkpoint(self):
        """checkpoint_json 为空 / None 时静默跳过，不抛异常。"""
        result: dict = {}
        sh._attach_checkpoint_result(
            {"task_type": "outline_generation", "checkpoint_json": None}, result)
        assert result == {}
    assert sec.OUTLINE_SAVED_STATUS == "目录已确认"