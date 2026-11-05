# -*- coding: utf-8 -*-
"""R173（2026-11-03）三项修复回归测试。

对应审计报告《后端工作机制与BUG深度审计_R173_20261103.md》：

- BUG-B（P2）：删除方案/项目不取消内存中运行任务 —— 删除行之前必须先对
  scheme_id 匹配的 running/paused 内存任务触发协作式停止（request_control
  "stop" 范式），并有界等待收尾；任务已结束的幂等情形不得误伤其他方案任务。
- BUG-A（P2）：delete_scheme 文件清理不全 —— 只清 ``*.docx``，漏 ``*.pdf``
  （项目级 projects.py 2026-10-03 已修，方案级漏改）；原子替换 tmp 变体
  （``*.tmp.docx`` / ``*.tmp.pdf``）由同一 glob 口径覆盖。
- BUG-C（P3）：续写去重失效 —— 整段重复复活（``return b or cont.strip()``）
  与 >300 字单段重复漏删（探针上限写死 300）必须确定性拦截，且不得误删
  合法短续写。
"""

import asyncio
import uuid

import app.services.ai.task_registry as tr
from app.routers.sse_utils import _dedup_continuation
from app.services.ai.task_registry import (
    cancel_active_tasks_for_schemes,
    cancel_and_wait_tasks_for_schemes,
)


def _make_mem_task(scheme_id: str, status: str = "running") -> str:
    """往内存 `_tasks` 注入一条带真实控制事件的任务态，返回 task_id。"""
    tid = str(uuid.uuid4())
    pause_event = asyncio.Event()
    pause_event.set()  # 与 register_task 一致：默认运行中
    tr._tasks[tid] = {
        "type": "content_generation",
        "status": status,
        "scheme_id": scheme_id,
        "pause_event": pause_event,
        "stop_event": asyncio.Event(),
        "child_tasks": set(),
        "last_control": None,
    }
    return tid


# ===========================================================================
# BUG-B：删除前置取消
# ===========================================================================
class TestBugBCancelBeforeDelete:
    def test_cancel_active_tasks_targets_matching_scheme_only(self):
        """running/paused 的同方案任务被 stop；其他方案/已终态任务不受影响。"""
        t_run = _make_mem_task("s-run")
        t_paused = _make_mem_task("s-run", status="paused")
        t_other = _make_mem_task("s-other")  # 其他方案：不得误伤
        t_done = _make_mem_task("s-run", status="completed")  # 已终态：幂等跳过

        stopped = cancel_active_tasks_for_schemes(["s-run"])

        assert set(stopped) == {t_run, t_paused}
        assert tr._tasks[t_run]["stop_event"].is_set()
        assert tr._tasks[t_paused]["stop_event"].is_set()
        # 暂停挂起的任务必须被唤醒（stop 必须同时 set pause_event，否则 wait_resume 永挂）
        assert tr._tasks[t_paused]["pause_event"].is_set()
        assert not tr._tasks[t_other]["stop_event"].is_set(), "其他方案任务不得被误停"
        assert not tr._tasks[t_done]["stop_event"].is_set(), "已终态任务幂等跳过"
        for t in (t_run, t_paused, t_other, t_done):
            tr._tasks.pop(t, None)

    def test_cancel_idempotent_for_absent_tasks(self):
        """任务不存在（已结束/已清理）时返回空列表，不抛异常。"""
        assert cancel_active_tasks_for_schemes(["no-such-scheme"]) == []
        assert cancel_active_tasks_for_schemes([]) == []
        assert cancel_active_tasks_for_schemes(None) == []

    async def test_delete_scheme_cancels_running_task_before_row_delete(self, db_conn, monkeypatch, tmp_path):
        """端到端：delete_scheme 前置取消内存任务（stop_event 置位 + 响应计数），
        且 task_registry 行仍被删除。"""
        import app.config as config
        import app.routers.schemes as schemes_mod

        await db_conn.execute("INSERT INTO projects(id,name) VALUES('p-r173','proj')")
        await db_conn.execute(
            "INSERT INTO schemes(id,project_id,name,status,word_budget) " "VALUES('s-r173','p-r173','方案','草稿',0)"
        )
        await db_conn.execute(
            "INSERT INTO task_registry(id,task_type,project_id,scheme_id,status) "
            "VALUES('t-r173','content_generation','p-r173','s-r173','running')"
        )
        await db_conn.commit()
        tid = _make_mem_task("s-r173")
        # EXPORTS_DIR 指向临时目录，避免触碰真实导出目录
        monkeypatch.setattr(config, "EXPORTS_DIR", tmp_path)

        resp = await schemes_mod.delete_scheme("p-r173", "s-r173", db=db_conn)

        assert resp["ok"] is True
        assert tr._tasks[tid]["stop_event"].is_set(), "删除方案必须先对内存运行任务触发停止（BUG-B）"
        assert resp.get("tasks_cancelled") == 1, "响应需加法式回传取消数"
        cur = await db_conn.execute("SELECT id FROM task_registry WHERE scheme_id='s-r173'")
        assert await cur.fetchone() is None, "task_registry 行仍须删除"
        tr._tasks.pop(tid, None)

    async def test_delete_project_cancels_all_scheme_tasks(self, db_conn, monkeypatch, tmp_path):
        """项目级：项目内全部方案的运行任务都被取消，项目外任务不误伤。"""
        import app.config as config
        import app.routers.projects as projects_mod

        await db_conn.execute("INSERT INTO projects(id,name) VALUES('p-r173b','proj')")
        await db_conn.execute(
            "INSERT INTO schemes(id,project_id,name,status,word_budget) " "VALUES('s-a','p-r173b','A','草稿',0)"
        )
        await db_conn.execute(
            "INSERT INTO schemes(id,project_id,name,status,word_budget) " "VALUES('s-b','p-r173b','B','草稿',0)"
        )
        await db_conn.execute("INSERT INTO projects(id,name) VALUES('p-keep','keep')")
        await db_conn.execute(
            "INSERT INTO schemes(id,project_id,name,status,word_budget) " "VALUES('s-keep','p-keep','K','草稿',0)"
        )
        await db_conn.commit()
        t_a = _make_mem_task("s-a")
        t_b = _make_mem_task("s-b")
        t_keep = _make_mem_task("s-keep")
        monkeypatch.setattr(config, "EXPORTS_DIR", tmp_path)

        resp = await projects_mod.delete_project("p-r173b", db=db_conn)

        assert resp["ok"] is True
        assert tr._tasks[t_a]["stop_event"].is_set()
        assert tr._tasks[t_b]["stop_event"].is_set()
        assert resp.get("tasks_cancelled") == 2
        assert not tr._tasks[t_keep]["stop_event"].is_set(), "其他项目的运行任务不得被误停"
        cur = await db_conn.execute("SELECT id FROM projects WHERE id='p-r173b'")
        assert await cur.fetchone() is None
        for t in (t_a, t_b, t_keep):
            tr._tasks.pop(t, None)

    async def test_delete_scheme_failsoft_when_cancel_raises(self, db_conn, monkeypatch, tmp_path):
        """取消环节异常（如 import 失败）不得阻断删除本身（fail-soft）。"""
        import builtins

        import app.config as config
        import app.routers.schemes as schemes_mod

        await db_conn.execute("INSERT INTO projects(id,name) VALUES('p-r173c','proj')")
        await db_conn.execute(
            "INSERT INTO schemes(id,project_id,name,status,word_budget) " "VALUES('s-r173c','p-r173c','方案','草稿',0)"
        )
        await db_conn.commit()
        monkeypatch.setattr(config, "EXPORTS_DIR", tmp_path)

        real_import = builtins.__import__

        def _boom(name, *args, **kwargs):
            if name == "app.services.ai.task_registry":
                raise ImportError("模拟导入失败")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _boom)
        resp = await schemes_mod.delete_scheme("p-r173c", "s-r173c", db=db_conn)
        monkeypatch.undo()
        assert resp["ok"] is True, "取消失败不得阻断删除"
        cur = await db_conn.execute("SELECT id FROM schemes WHERE id='s-r173c'")
        assert await cur.fetchone() is None

    async def test_wait_returns_after_tasks_exit_memory(self):
        """有界等待：任务退出内存（finish_task pop）后立即返回，不满等宽限期。"""
        tid = _make_mem_task("s-wait")
        real_request_control = tr.request_control

        def _stop_then_finish(task_id, action):
            ok = real_request_control(task_id, action)
            if ok:
                tr._tasks.pop(task_id, None)  # 模拟生成器检查点退出 → finish_task pop
            return ok

        orig = tr.request_control
        tr.request_control = _stop_then_finish
        try:
            stopped = await cancel_and_wait_tasks_for_schemes(["s-wait"], grace_seconds=5.0)
        finally:
            tr.request_control = orig
        assert stopped == [tid]
        assert tid not in tr._tasks


# ===========================================================================
# BUG-A：delete_scheme 文件清理补齐
# ===========================================================================
class TestBugAExportFileCleanup:
    async def test_delete_scheme_removes_docx_pdf_and_tmp_variants(self, db_conn, monkeypatch, tmp_path):
        """docx / pdf / tmp.docx / tmp.pdf 四类产物全清；他方案前缀文件保留。"""
        import app.config as config
        import app.routers.schemes as schemes_mod

        await db_conn.execute("INSERT INTO projects(id,name) VALUES('p-r173d','proj')")
        await db_conn.execute(
            "INSERT INTO schemes(id,project_id,name,status,word_budget) " "VALUES('s-r173d','p-r173d','方案','草稿',0)"
        )
        await db_conn.commit()
        monkeypatch.setattr(config, "EXPORTS_DIR", tmp_path)

        names = [
            "s-r173d_abcdef12_12345678.docx",  # docx 缓存
            "s-r173d_abcdef12_12345678.pdf",  # pdf 缓存（BUG-A 漏项）
            "s-r173d_abcdef12_12345678.deadbeef.tmp.docx",  # docx 原子替换 tmp
            "s-r173d_abcdef12_12345678.deadbeef.tmp.pdf",  # pdf 原子替换 tmp
        ]
        for n in names:
            (tmp_path / n).write_bytes(b"PK")
        keep = tmp_path / "s-other_abcdef12_12345678.docx"  # 他方案：不得误删
        keep.write_bytes(b"PK")
        keep2 = tmp_path / "unrelated.docx"  # 无 scheme 前缀：保留
        keep2.write_bytes(b"PK")

        resp = await schemes_mod.delete_scheme("p-r173d", "s-r173d", db=db_conn)

        assert resp["ok"] is True
        assert resp["export_files_removed"] == 4, "docx+pdf+两种 tmp 应全清（BUG-A）"
        for n in names:
            assert not (tmp_path / n).exists(), f"{n} 应被删除"
        assert keep.exists() and keep2.exists(), "不得误删其他方案的文件"

    def test_source_covers_pdf_extension(self):
        """静态锚点：方案级清理必须是 docx/pdf 双扩展循环（防回退单 docx）。"""
        import inspect

        import app.routers.schemes as schemes_mod

        src = inspect.getsource(schemes_mod)
        assert 'for ext in ("docx", "pdf"):' in src, "delete_scheme 导出文件清理必须覆盖 docx 与 pdf 双扩展（BUG-A）"


# ===========================================================================
# BUG-C：续写去重确定性修复
# ===========================================================================
class TestBugCDedupContinuation:
    # 与审计报告探针同款形态（前文构造为重复文本块）
    PREV = (
        "本工程深基坑开挖深度为十二米，采用地下连续墙加内支撑支护体系。"
        "基坑周边环境复杂，邻近既有地铁隧道，变形控制要求严格。"
    ) * 40
    PREV_LONG = PREV * 4

    def test_full_duplicate_discarded(self):
        """整段重复（续写=前文尾部 200 字）必须整体丢弃，不得复活。"""
        assert _dedup_continuation(self.PREV, self.PREV[-200:]) == ""

    def test_long_single_paragraph_duplicate_discarded(self):
        """>300 字单段重复（重抄前文末尾 3500 字）必须拦截，不得残留。"""
        assert _dedup_continuation(self.PREV_LONG, self.PREV_LONG[-3500:]) == ""

    def test_seam_copy_plus_new_text_keeps_only_new(self):
        """缝对齐（重抄尾部 + 新内容）只保留新内容。"""
        out = _dedup_continuation(self.PREV, self.PREV[-100:] + "接下来开挖。")
        assert out == "接下来开挖。"

    def test_legit_short_continuation_preserved(self):
        """合法短续写（一句衔接语，<40 字）不得误删。"""
        short = "综上所述，本方案各项措施可行，能够满足施工安全与质量要求。"
        assert _dedup_continuation(self.PREV, short) == short

    def test_legit_long_continuation_preserved(self):
        """≥40 字的全新合法续写不得误删。"""
        legal = "下一步应重点做好降水井的日常巡检与水位观测，" * 3 + "发现异常立即启动应急预案并组织专家论证。"
        assert _dedup_continuation(self.PREV, legal) == legal

    def test_multi_paragraph_duplicate_not_resurrected(self):
        """多段重复全部剥空后返回空串（旧实现 return b or cont.strip() 复活）。"""
        para_a = "第一段内容：基坑降水必须在开挖前十五天完成，确保干作业条件。" * 2
        para_b = "第二段内容：内支撑架设与土方开挖严格按时空顺序交替进行施工。" * 2
        prev = f"开头无关内容。\n{para_a}\n{para_b}\n收尾内容。"
        cont = f"{para_a}\n{para_b}"
        assert _dedup_continuation(prev, cont) == ""

    def test_empty_and_none_prev_robustness(self):
        """prev 为空/None 时不抛异常（既有健壮性契约保持）。"""
        assert _dedup_continuation("", "全新内容" * 10) == "全新内容" * 10
        assert _dedup_continuation(None, "") == ""
