"""R231 SSE 链迟到代理回收 · 补审护栏（2026-11-13）。

第 4 路只读代理（SSE 分步链）在轮次收口后迟归（429 限额家族，R226 先例），
主会话逐条亲验定罪后落地 6 项修复，本文件为配套护栏：

F1（P1）stopped 终态不再喂 evaluate —— 部分成果显式 ``partial_outline_block``
    （fail-closed），「目录不完整」从不可见 blocker 变为显式判定；配合前端
    两条接管路径的 status 分流（outlineR231.test.ts），stopped checkpoint
    不再可能触发 8 秒倒计时自动保存半份目录。
F2（P2）partial/failed/error 形态 checkpoint 写带终态守卫（skip_terminal
    单语句原子 WHERE）+ completed 落库后 holder 打 ``terminal_ckpt`` 标记 ——
    R226 DAT-6′ 守卫（读 DB 行状态后早退）与 shield 内层落库之间
    「读时 running、写时 completed 已落库」窄窗三层闭合。
疑点2（防御）续跑取数跳过 status=stopped 但 event=completed/degraded 的
    矛盾组合行（R209「completed 不参与续跑」语义按事件补全）。
P3-1 converge（L4+ 折叠 / L3 合并）后 ``_prog["nodes"]`` 重算（两路径）。
P3-2 在途重试前复查配额熔断（两路径，与 R213 入口判据同源）。
P3-3 续跑预填充异常时剥除已挂复用子树（防二次调度产出重复章）。
"""

from __future__ import annotations

import json
import os

from app.services.outline_auto_confirm import partial_outline_block

_ROUTERS = os.path.join("app", "routers")


def _src(rel: str) -> str:
    """读源码并统一行尾（断言不含行尾敏感锚）。"""
    with open(rel, "rb") as f:
        return f.read().decode("utf-8").replace("\r\n", "\n")


# ===================== F1 · stopped 不再自动确认 =====================


def test_f1_partial_block_pure():
    """显式判定纯函数：ready=False + partial_outline blocker（fail-closed）。"""
    res = partial_outline_block()
    assert res["ready"] is False
    assert res["blockers"][0]["code"] == "partial_outline"


def test_f1_stopped_exit_uses_explicit_block():
    """源码棘轮：分步 stopped 出口走 partial_outline_block，evaluate 收进非停止臂。"""
    src = _src(os.path.join(_ROUTERS, "sse_handlers.py"))
    assert "from app.services.outline_auto_confirm import partial_outline_block as _partial_block" in src
    assert "payload[\"auto_confirm\"] = _partial_block()" in src
    # 结构棘轮（归一行尾后精确锚）：if stopped 臂在先、else 臂承载 evaluate。
    # ⚠️ 不用文件序 index 比较——oneshot 短方案出口（:1884 段）在分步出口之前
    #    合法 import evaluate，文件序断言必然自咬。
    assert (
        'if stopped:\n'
        '                    from app.services.outline_auto_confirm import partial_outline_block as _partial_block\n'
        '\n'
        '                    payload["auto_confirm"] = _partial_block()\n'
        '                else:'
    ) in src


# ===================== F2 · checkpoint 写层终态守卫 =====================


async def _seed_task(db, task_id="t1", status="running", checkpoint_json="{}"):
    await db.execute(
        "INSERT INTO task_registry (id, task_type, project_id, scheme_id,"
        " status, progress, checkpoint_json) VALUES (?,?,?,?,?,?,?)",
        (task_id, "outline_generation", "p1", "s1", status, 0.0, checkpoint_json),
    )
    await db.commit()


class TestF2WriteGuard:
    async def test_skip_terminal_blocks_completed_row(self, db_conn):
        """任务行已终态 completed：skip_terminal 写必须放弃（成果保留）。"""
        from app.routers.sse_checkpoint import _write_task_checkpoint_db

        await _seed_task(db_conn, "t_f2a", status="completed", checkpoint_json="{}")
        await _write_task_checkpoint_db(
            "t_f2a", "outline_result", {"event": "stopped", "partial": True}, skip_terminal=True
        )
        cur = await db_conn.execute("SELECT checkpoint_json FROM task_registry WHERE id=?", ("t_f2a",))
        row = await cur.fetchone()
        assert json.loads(row["checkpoint_json"]) == {}, "partial 写不得覆盖 completed 成果"

    async def test_skip_terminal_writes_running_row(self, db_conn):
        """任务行 running（正常停止窗口）：skip_terminal 写照常落库（合法停止不受误伤）。"""
        from app.routers.sse_checkpoint import _write_task_checkpoint_db

        await _seed_task(db_conn, "t_f2b", status="running")
        await _write_task_checkpoint_db(
            "t_f2b", "outline_result", {"event": "stopped", "partial": True}, skip_terminal=True
        )
        cur = await db_conn.execute("SELECT checkpoint_json FROM task_registry WHERE id=?", ("t_f2b",))
        row = await cur.fetchone()
        ckpt = json.loads(row["checkpoint_json"])
        assert ckpt["kind"] == "outline_result" and ckpt["partial"] is True

    async def test_default_write_keeps_unconditional_semantics(self, db_conn):
        """缺省（completed 写自身）：无条件 UPDATE 语义逐字保持（R226 契约不变）。"""
        from app.routers.sse_checkpoint import _write_task_checkpoint_db

        await _seed_task(db_conn, "t_f2c", status="completed", checkpoint_json="{}")
        await _write_task_checkpoint_db("t_f2c", "outline_result", {"event": "completed", "outline": []})
        cur = await db_conn.execute("SELECT checkpoint_json FROM task_registry WHERE id=?", ("t_f2c",))
        row = await cur.fetchone()
        assert json.loads(row["checkpoint_json"])["event"] == "completed"

    async def test_partial_writers_pass_skip_terminal(self, db_conn):
        """partial 写点全部带守卫：兜底/显式停止/error 三个写点源码棘轮。"""
        ckpt_src = _src(os.path.join(_ROUTERS, "sse_checkpoint.py"))
        assert "await _save_outline_checkpoint(task_id, payload, skip_terminal=True)" in ckpt_src
        review_src = _src(os.path.join(_ROUTERS, "_sse_outline_review.py"))
        assert review_src.count("_save_outline_checkpoint(task_id, _ckpt_payload, skip_terminal=True)") == 2
        handlers_src = _src(os.path.join(_ROUTERS, "sse_handlers.py"))
        assert "_save_outline_checkpoint(task_id, _fail_payload, skip_terminal=True)" in handlers_src
        assert "_save_outline_checkpoint(task_id, _err_ckpt, skip_terminal=True)" in handlers_src

    async def test_terminal_flag_anchors(self):
        """holder 级标记：completed 落库成功处打标 ×2 + 显式停止出口检查 + 兜底透传。"""
        handlers_src = _src(os.path.join(_ROUTERS, "sse_handlers.py"))
        assert handlers_src.count('_partial_holder["terminal_ckpt"] = True') == 2
        assert "terminal_ckpt_marked=bool(_partial_holder.get(\"terminal_ckpt\"))" in handlers_src
        review_src = _src(os.path.join(_ROUTERS, "sse_checkpoint.py"))
        assert "if terminal_ckpt_marked:" in review_src
        stopped_src = _src(os.path.join(_ROUTERS, "_sse_outline_review.py"))
        assert 'if holder.get("terminal_ckpt"):' in stopped_src


# ===================== 疑点2 · 续跑取数跳过矛盾组合行 =====================


class TestResumableSkipsCompleteEvent:
    async def test_completed_event_row_skipped(self, db_conn):
        """status=stopped 行 + event=completed checkpoint：续跑取数跳过（全量重算）。"""
        from app.routers.sse_checkpoint import _load_resumable_outline_checkpoint

        await _seed_project(db_conn)
        _ckpt = json.dumps(
            {
                "kind": "outline_result",
                "event": "completed",
                "outline": [{"title": "章1", "children": []}],
            },
            ensure_ascii=False,
        )
        await _seed_task(db_conn, "t_r2a", status="stopped", checkpoint_json=_ckpt)
        res = await _load_resumable_outline_checkpoint("s1")
        assert res is None, "完整成果行（event=completed）不得参与续跑（R209 语义）"

    async def test_stopped_event_row_still_resumable(self, db_conn):
        """正常 stopped 行：续跑取数照常返回（合法续跑不受误伤）。"""
        from app.routers.sse_checkpoint import _load_resumable_outline_checkpoint

        await _seed_project(db_conn)
        _ckpt = json.dumps(
            {
                "kind": "outline_result",
                "event": "stopped",
                "partial": True,
                "outline": [{"title": "章1", "children": []}],
            },
            ensure_ascii=False,
        )
        await _seed_task(db_conn, "t_r2b", status="stopped", checkpoint_json=_ckpt)
        res = await _load_resumable_outline_checkpoint("s1")
        assert res is not None and res["event"] == "stopped"


async def _seed_project(db):
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)", ("p1", "测试项目"))
    await db.execute("INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES (?,?,?)", ("s1", "p1", "测试方案"))
    await db.commit()


# ===================== P3 组 · 源码棘轮 =====================


def test_p31_nodes_recompute_after_converge():
    """converge 只减不增节点：两路径其后各补一次 _prog["nodes"] 重算。"""
    src = _src(os.path.join(_ROUTERS, "sse_handlers.py"))
    assert src.count('if _l3_res.get("folded") or _l3_res.get("merged"):') == 2
    assert src.count('_prog["nodes"] = _count_nodes(outline)') >= 1
    assert src.count('_prog["nodes"] = _count_nodes(full_outline)') >= 2


def test_p32_quota_recheck_before_retry():
    """在途重试前复查熔断：两处新判据在位（与 R213 入口判据同源）。"""
    src = _src(os.path.join(_ROUTERS, "_sse_outline_review.py"))
    assert "重试前命中配额熔断" in src
    assert "尝试前命中配额熔断" in src
    # 既有 3 处入口/终态判据（:433/:540/:676，:1484 为带 task_id 前置的变体不计入）
    # + 本轮 2 处重试前复查 = 5
    assert src.count("if _is_quota_blocked(task_id):") >= 5


def test_p33_prefill_except_strips_reused_children():
    """续跑预填充异常：剥除已挂复用子树（防二次调度重复章）。"""
    src = _src(os.path.join(_ROUTERS, "sse_handlers.py"))
    assert "for _n_prefill in level1:" in src
    assert '_n_prefill["children"] = []' in src


# ===================== 字节体检 =====================


class TestByteHealth:
    """本轮触碰的 SSE 链生产源：行尾不混装 / 无 BOM / 无 U+FFFD。"""

    FILES = [
        os.path.join("app", "routers", "sse_handlers.py"),
        os.path.join("app", "routers", "sse_checkpoint.py"),
        os.path.join("app", "routers", "_sse_outline_review.py"),
        os.path.join("app", "services", "outline_auto_confirm.py"),
    ]

    def test_line_endings_not_mixed(self):
        for rel in self.FILES:
            with open(rel, "rb") as f:
                raw = f.read()
            crlf = raw.count(b"\r\n")
            lf = raw.count(b"\n")
            assert crlf == lf, f"{rel} 行尾混装：CRLF={crlf} 总LF={lf}"

    def test_no_bom_no_fffd(self):
        for rel in self.FILES:
            with open(rel, "rb") as f:
                raw = f.read()
            assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} 带 BOM"
            # ⚠️ 检索针用转义 "\ufffd" 而非字面量——字面量会让本文件自带
            #    U+FFFD（R212 教训③：字节体检必须覆盖新增测试文件，护栏文件
            #    自身先被自家断言击中）。
            assert raw.decode("utf-8", errors="replace").count("\ufffd") == 0, f"{rel} 含 U+FFFD"
