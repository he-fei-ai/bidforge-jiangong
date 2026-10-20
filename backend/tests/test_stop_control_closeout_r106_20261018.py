"""停止按钮全链路收口护栏（R106 · 2026-10-18）

本轮从「各模块停止按钮实际是否有效」的端到端排查中定位并修复的缺陷，
全部在本文件留下**可执行断言**，并逐条做过 A/B 反向验证（把接线改回旧形态
→ 本文件定向失败），变异脚本见 `artifacts/r106/ab_r106.py`。

分组：
  A 组 · FIX-1 · 导出取消登记语义（每调用独立 Event / 幂等注销 / in_progress）
  B 组 · FIX-1 · /export/cancel 端点契约（幂等、置位、无在飞仍 ok）
  C 组 · FIX-1 · 成本边界检查点（_build_docx_sync 入口 + roots 段边界、渲染段四处）
  D 组 · FIX-1 · 路由接线（登记在锁前、断连置位、ExportCancelled → 499、不写缓存）
  E 组 · FIX-4 · 控制契约 task_control（stop → request_control + 落 stopped；
         orphaned 如实回执；ok=false 有 message）
  F 组 · FIX-4 · register_task 替换旧任务时同步 stop 旧内存任务（跨类型状态混乱防线）
  G 组 · 停止语义回归锁 · request_control("stop") 必须同时解暂停 + 级联 cancel 子任务
  H 组 · FIX-6 · SSE 泵结束的僵尸自愈（mark_stream_ended 宽限回收 / 正常收尾优先 /
         with_heartbeat on_end 同步钩子三条路径）
  I 组 · FIX-6 · reap_orphan_tasks 的内存豁免收紧（在跑仍豁免，已放弃必须回收）
  J 组 · FIX-7 · 目录链 task_id 提前下发接线锁（connecting 先于装配 + on_end 已接）

判据纪律（AGENTS §5.14 / §5.15）：
  · 静态锁一律锚**调用形态**或 AST 节点，不锚「源码里有没有这个字符串」；
  · 计数型断言必须配「非空转自证」，防判据静默失灵；
  · 取消登记簿是模块级全局，本文件自带快照/复位 fixture，避免跨用例污染。
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import threading
import time
from pathlib import Path

import app.routers.export as E
import app.routers.sse_handlers as S
import app.services.ai.sse_utils as SU
import app.services.ai.task_registry as tr
import pytest

APP_DIR = Path(E.__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _src(fn) -> str:
    return inspect.getsource(fn)


def _mod_ast(rel: str):
    return ast.parse((APP_DIR / rel).read_bytes().decode("utf-8"))


def _fn_ast(rel: str, name: str):
    tree = _mod_ast(rel)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError("未找到函数 " + name)


def _call_names(node) -> set:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _count_calls(node, name: str) -> int:
    return sum(1 for n in ast.walk(node)
               if isinstance(n, ast.Call)
               and ((isinstance(n.func, ast.Name) and n.func.id == name)
                    or (isinstance(n.func, ast.Attribute) and n.func.attr == name)))


def _has_kwarg(node, kw: str) -> bool:
    return any(k.arg == kw for n in ast.walk(node)
               if isinstance(n, ast.Call) for k in n.keywords)


@pytest.fixture
def cancel_registry():
    """快照/复位导出取消登记簿（模块级全局，跨用例必须隔离）。"""
    snapshot = {k: list(v) for k, v in E._EXPORT_CANCEL_EVENTS.items()}
    yield E._EXPORT_CANCEL_EVENTS
    E._EXPORT_CANCEL_EVENTS.clear()
    E._EXPORT_CANCEL_EVENTS.update(snapshot)


def _docx_args(tmp_path, cancel_event=None) -> dict:
    """_build_docx_sync 的最小可调用参数集（空 roots ⇒ 不渲染任何章节）。"""
    return dict(
        out_path=str(Path(tmp_path) / "out.docx"),
        scheme={"name": "护栏方案", "id": "scheme-x"},
        roots=[],
        children_map={},
        chart_lookup={},
        rendered_bytes={},
        blocks_cache={},
        font_name="宋体",
        font_size=12,
        page_header="",
        page_footer="",
        show_page_number=False,
        show_title_page=False,
        show_toc=False,
        bidder_name="",
        heading_styles={},
        cancel_event=cancel_event,
    )


# ===========================================================================
# A 组 · FIX-1 · 导出取消登记语义
# ===========================================================================

class TestExportCancelRegistrySemantics:
    """取消登记必须「每次导出独立、注销只摘本次、置位覆盖全部在飞」。

    并发同 scheme 导出若共用一个 Event，旧导出被取消后其孤儿线程会**误读**
    后续新导出登记的事件（新导出被莫名中止）—— 这是本组锁存在的原因。
    """

    def test_each_registration_gets_its_own_event(self, cancel_registry):
        ev1 = E._export_cancel_register("s-a")
        ev2 = E._export_cancel_register("s-a")
        assert ev1 is not ev2, "同 scheme 并发导出必须持独立事件（否则互相误读）"
        assert len(cancel_registry["s-a"]) == 2

        ev1.set()
        assert not ev2.is_set(), "取消旧导出不得波及仍在飞的新导出"

    def test_request_sets_all_inflight_and_reports_progress(self, cancel_registry):
        ev1 = E._export_cancel_register("s-b")
        ev2 = E._export_cancel_register("s-b")
        assert E._export_cancel_request("s-b") is True
        assert ev1.is_set() and ev2.is_set()

    def test_request_without_inflight_is_idempotent_false(self, cancel_registry):
        assert E._export_cancel_request("s-none") is False

    def test_deregister_only_removes_this_event(self, cancel_registry):
        ev1 = E._export_cancel_register("s-c")
        ev2 = E._export_cancel_register("s-c")
        E._export_cancel_deregister("s-c", ev1)
        assert cancel_registry["s-c"] == [ev2], "注销摘除本次即可，不得连带摘除并发对等"
        E._export_cancel_deregister("s-c", ev2)
        assert "s-c" not in cancel_registry, "列表清空后必须 pop 键（防登记簿无界增长）"

    def test_deregister_unknown_event_is_safe(self, cancel_registry):
        ev = E._export_cancel_register("s-d")
        E._export_cancel_deregister("s-d", threading.Event())  # 不在册：静默
        E._export_cancel_deregister("s-absent", ev)            # scheme 不在册：静默
        assert cancel_registry["s-d"] == [ev]
        E._export_cancel_deregister("s-d", ev)

    def test_checkpoint_helper(self):
        E._check_export_cancelled(None)          # 未启用取消 ⇒ 短路
        ev = threading.Event()
        E._check_export_cancelled(ev)            # 未置位 ⇒ 放行
        ev.set()
        with pytest.raises(E.ExportCancelled):
            E._check_export_cancelled(ev)


# ===========================================================================
# B 组 · FIX-1 · /export/cancel 端点契约
# ===========================================================================

class TestExportCancelEndpoint:
    """契约：幂等 + 始终 ok + in_progress 如实反映是否有在飞导出。

    前端 handleCancelExport 依赖「先 cancel 再 abort」的安全顺序，
    若无在飞导出就返回非 ok，前端就得猜后端状态（旧实现只能盲 abort）。
    """

    async def test_endpoint_sets_registered_event(self, cancel_registry):
        ev = E._export_cancel_register("s-e")
        res = await E.export_cancel("s-e")
        assert res == {"ok": True, "in_progress": True}
        assert ev.is_set()

    async def test_endpoint_idempotent_without_inflight(self, cancel_registry):
        first = await E.export_cancel("s-f")
        second = await E.export_cancel("s-f")
        assert first["ok"] is True and first["in_progress"] is False
        assert second == {"ok": True, "in_progress": False}

    def test_route_path_matches_frontend_contract(self):
        """前端 exportApi.cancel 打的是 POST /schemes/{id}/export/cancel。"""
        paths = [(sorted(r.methods), r.path) for r in E.router.routes
                 if r.path.endswith("/cancel")]
        assert (["POST"], "/api/v1/schemes/{scheme_id}/export/cancel") in paths, (
            "取消端点路径/方法变更必须与 frontend/src/api/index.ts exportApi.cancel 同步"
            "（不同步 = 停止按钮重新变成假停止）")


# ===========================================================================
# C 组 · FIX-1 · 成本边界检查点
# ===========================================================================

class TestExportCancelCheckpoints:
    """取消只能在「成本边界」协作式生效：图表逐段之间 / AI 配图逐占位 /
    位图逐 URL / 章节逐章之间。检查点放错位置（如放进 fail-soft 的
    _write_section_safe）会被吞掉，取消即失效。"""

    async def test_build_thread_entry_checkpoint_raises(self, tmp_path):
        ev = threading.Event()
        ev.set()
        with pytest.raises(E.ExportCancelled):
            E._build_docx_sync(**_docx_args(tmp_path, ev))

    async def test_render_entry_checkpoint_raises(self):
        """渲染段顶部检查点必须真读 plan 里的取消事件 —— 若把下传摘成
        `_cancel_ev = None`，AST 计数依然达标，只有行为断言能抓到（A/B M6）。"""
        ev = threading.Event()
        ev.set()
        with pytest.raises(E.ExportCancelled):
            await E._prepare_export_render({"_cancel_event": ev})

    async def test_build_thread_without_cancel_keeps_old_behaviour(self, tmp_path):
        """零影响面：未置位（含不传事件）时不得抛 ExportCancelled。"""
        try:
            E._build_docx_sync(**_docx_args(tmp_path))
            E._build_docx_sync(**_docx_args(tmp_path, threading.Event()))
        except E.ExportCancelled:  # pragma: no cover - 回归即失败
            pytest.fail("未请求取消时构建线程不得抛出 ExportCancelled")
        except Exception:
            # 空文档构建的其它既有行为不在本护栏范围（doc.save 依赖真实产物）
            pass

    def test_build_thread_signature_has_trailing_cancel_event(self):
        """追加参数必须落在签名**末尾**：历史调用方多为位置参数，插中间会错位。"""
        params = list(inspect.signature(E._build_docx_sync).parameters)
        assert params[-1] == "cancel_event"
        assert inspect.signature(E._build_docx_sync).parameters["cancel_event"].default is None

    def test_build_thread_has_entry_and_loop_boundary_checkpoints(self):
        node = _fn_ast("routers/export.py", "_build_docx_sync")
        assert _count_calls(node, "_check_export_cancelled") >= 2, "入口 + roots 段边界检查点缺失"
        # 段边界检查点必须在章节写入 for 循环**体内**，且不得落进 _write_section_safe
        loops = [n for n in ast.walk(node) if isinstance(n, ast.For)]
        in_loop = [
            lp for lp in loops
            if any(isinstance(c, ast.Call)
                   and isinstance(c.func, ast.Name)
                   and c.func.id == "_check_export_cancelled"
                   for st in lp.body for c in ast.walk(st))
        ]
        assert in_loop, "取消检查点未放在章节写入循环段边界（在飞章节无法提前退出）"
        safe = [n for n in ast.walk(node)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "_write_section_safe"]
        for fn in safe:
            assert _count_calls(fn, "_check_export_cancelled") == 0, (
                "_write_section_safe 吞 Exception（fail-soft），检查点放它内部会被静默失效")

    def test_ai_image_blocks_skip_instead_of_raise(self):
        """AI 配图逐块循环：任务内 skip-return（不在 gather 里 raise，
        避免兄弟任务未取回异常噪声），段后由调用方统一判定。"""
        fn = _fn_ast("routers/export.py", "_auto_generate_ai_image_blocks")
        assert "cancel_event" in {a.arg for a in fn.args.args + fn.args.kwonlyargs}
        inner = [n for n in ast.walk(fn)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_gen"]
        assert inner, "_auto_generate_ai_image_blocks 内部生成函数结构变更"
        ifs = [n for n in ast.walk(inner[0]) if isinstance(n, ast.If)]
        guarded_return = [
            i for i in ifs
            if "cancel_event" in ast.dump(i.test)
            and any(isinstance(r, ast.Return) for st in i.body for r in ast.walk(st))
        ]
        assert guarded_return, "AI 配图逐块循环缺少「已取消即跳过」的守卫分支"
        assert _count_calls(inner[0], "_check_export_cancelled") == 0, (
            "逐块任务内不得抛（gather 兄弟未取回异常噪声）")

    def test_render_track_has_four_checkpoints(self):
        """渲染段：顶部 + 图表段后 + AI 配图段后 + 位图下载段后。"""
        fn = _fn_ast("routers/export.py", "_prepare_export_render")
        assert _count_calls(fn, "_check_export_cancelled") >= 4
        assert _count_calls(fn, "_auto_generate_ai_image_blocks") == 1
        assert _has_kwarg(fn, "cancel_event"), "AI 配图段必须将取消事件下传"

    def test_prepare_export_threads_event_into_plan_and_task(self):
        pe = _fn_ast("routers/export.py", "_prepare_export")
        assert "cancel_event" in {a.arg for a in pe.args.args + pe.args.kwonlyargs}
        assigns = [n for n in ast.walk(pe) if isinstance(n, ast.Assign)]
        keyed = [a for a in assigns
                 if isinstance(a.targets[0], ast.Subscript)
                 and isinstance(a.targets[0].slice, ast.Constant)
                 and a.targets[0].slice.value == "_cancel_event"]
        assert keyed, "_prepare_export 未把取消事件放进 plan（渲染段拿不到）"
        # 写入值必须真读形参：A/B B-M12 证明「键存在」断言抓不到
        # `plan["_cancel_event"] = None`（键在、值被摘 → 渲染段永远拿不到事件）
        assert any(isinstance(a.value, ast.Name) and a.value.id == "cancel_event"
                   for a in keyed), "plan 的取消事件键未写入形参 cancel_event（下传被摘除）"
        task = _fn_ast("routers/export.py", "_build_docx_task")
        rets = [n for n in ast.walk(task) if isinstance(n, ast.Return)]
        assert rets, "_build_docx_task 无 return（结构变更需同步本护栏）"
        assert "_cancel_event" in ast.dump(rets[0]), "构建任务元组末尾未透传取消事件"


# ===========================================================================
# D 组 · FIX-1 · 路由接线（docx / pdf 同构）
# ===========================================================================

class TestExportRouteWiring:
    """/cancel 只有在路由把「登记 → 置位 → 清理 → 注销」全接上才真的有效。"""

    def test_both_export_routes_wired(self):
        for name in ("export_docx", "export_pdf"):
            fn = _fn_ast("routers/export.py", name)
            calls = _call_names(fn)
            assert "_export_cancel_register" in calls, f"{name} 未登记取消事件"
            assert "_export_cancel_deregister" in calls, f"{name} 未注销（登记簿泄漏）"
            assert "_check_export_cancelled" in calls or name == "export_docx", (
                f"{name} 缺少段边界检查点")
            handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
            kinds = [ast.dump(h.type) if h.type else "" for h in handlers]
            assert any("CancelledError" in k for k in kinds), (
                f"{name} 缺 except asyncio.CancelledError：客户端断连（关页/abort）"
                "无法置位取消事件，孤儿线程会跑完整个构建")
            # 断连处理块必须真的「置位」——A/B B-M14 证明只锁 handler 存在抓不到
            # 块体里的 cancel_ev.set() 被摘（那时 /cancel 与断连都不再通知线程）
            _canc = [h for h in handlers if h.type is not None
                     and "CancelledError" in ast.dump(h.type)]
            assert any("attr='set'" in ast.dump(h) for h in _canc), (
                f"{name} 的 except CancelledError 未置位取消事件（断连 = 假停止）")
            assert any("ExportCancelled" in k for k in kinds), (
                f"{name} 缺 except ExportCancelled：取消将冒泡成 500 而非 499")
            # finally 必须成对释放去重锁与取消登记（锁先行、登记簿随后）
            finals = [st for n in ast.walk(fn) if isinstance(n, ast.Try)
                      for st in (n.finalbody or [])]
            assert any("_export_cancel_deregister" in ast.dump(f) for f in finals), (
                f"{name} 的 deregister 不在 finally：异常路径泄漏登记")

    def test_cancel_path_cleans_tmp_and_skips_cache(self):
        """取消分支：清理临时产物 + 注销在飞保护 + 上抛，且**不写缓存行**。

        脏指纹缓存会让「取消后下次导出」命中一份不存在的产物（缓存行指向
        已被 unlink 的路径）。
        """
        fn = _fn_ast("routers/export.py", "export_docx")
        handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers
                    if h.type is not None and "ExportCancelled" in ast.dump(h.type)]
        assert handlers, "export_docx 的 ExportCancelled 处理块消失"
        # 取 to_thread 内层那个（含 unlink 的），而非路由外层（转 499 的）
        inner = [h for h in handlers if "_export_inflight_unregister" in ast.dump(h)]
        assert inner, "取消分支未注销在飞产物保护（孤儿回收会误删/集合膨胀）"
        for h in inner:
            assert "_persist_export_cache_row" not in ast.dump(h), (
                "取消分支不得写缓存行（脏指纹）")
            assert "unlink" in ast.dump(h), "取消分支必须清理本次临时产物"

    def test_http_499_on_cancelled(self):
        fn = _fn_ast("routers/export.py", "export_docx")
        handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers
                    if h.type is not None and "ExportCancelled" in ast.dump(h.type)]
        outer = [h for h in handlers if "HTTPException" in ast.dump(h)]
        assert outer, "取消未转 HTTPException（前端拿不到结构化失败）"
        codes = [c for h in outer for st in h.body for c in ast.walk(st)
                 if isinstance(c, ast.Call) and c.args
                 and isinstance(c.args[0], ast.Constant) and c.args[0].value == 499]
        assert codes, "取消应回 499（client closed request 语义），非 500/400"


# ===========================================================================
# E 组 · FIX-4 · 控制契约 task_control
# ===========================================================================

class TestTaskControlContract:
    """前端「停止中」置灰依赖后端回执三态：accepted / orphaned / rejected。

    旧前端只看 `data.ok` 且对 ok=false 静默 —— 后端契约本身是有 message 的，
    本组把契约钉住，前端才有「如实提示」的可能。
    """

    async def test_stop_applies_control_and_lands_stopped(self, db_conn):
        tid = await tr.register_task("outline_generation", scheme_id="s1")
        res = await S.task_control(tid, {"action": "stop"})
        assert res == {"ok": True, "action": "stop"}
        assert tr.is_stopped(tid) is True, "stop 未落到任务 stop_event"
        conn = db_conn
        cur = await conn.execute("SELECT status FROM task_registry WHERE id=?", (tid,))
        row = await cur.fetchone()
        assert row["status"] == "stopped", "内存停了但 DB 未落终态 → 任务栏永远显示运行中"

    async def test_unknown_action_rejected(self, db_conn):
        tid = await tr.register_task("outline_generation", scheme_id="s1")
        res = await S.task_control(tid, {"action": "explode"})
        assert res["ok"] is False and res.get("message")

    async def test_missing_task_rejected_with_message(self, db_conn):
        res = await S.task_control("no-such-task", {"action": "stop"})
        assert res["ok"] is False, "不存在的任务必须如实回执（前端据此解除置灰并提示）"
        assert res.get("message")
        assert "orphaned" not in res

    async def test_orphan_row_returns_ok_with_orphaned_flag(self, db_conn):
        """内存无实例但 DB 仍 running（多 worker / 进程重启残留）：
        必须 ok=true + orphaned=true，前端据此**立即**解除「停止中」。"""
        tid = await tr.register_task("content_generation", scheme_id="s1")
        tr._tasks.pop(tid, None)  # 模拟「DB 在跑、内存已无」
        res = await S.task_control(tid, {"action": "stop"})
        assert res["ok"] is True and res.get("orphaned") is True
        assert res.get("message")
        cur = await db_conn.execute("SELECT status FROM task_registry WHERE id=?", (tid,))
        assert (await cur.fetchone())["status"] == "stopped"


# ===========================================================================
# F 组 · register_task 替换旧任务时同步 stop（跨类型状态混乱防线）
# ===========================================================================

class TestRegisterTaskReplacesOldStopsIt:
    """重复点「生成」时新任务替换旧 DB 行，旧 event_stream 若不同步 stop，
    会与新任务竞态覆盖写库；而用户看到的状态是「已停止」却仍在烧 AI。"""

    async def test_replacing_old_running_task_triggers_stop(self, db_conn):
        old_id = await tr.register_task("outline_generation", scheme_id="s2")
        new_id = await tr.register_task("outline_generation", scheme_id="s2")
        assert new_id != old_id
        assert tr.is_stopped(old_id) is True, (
            "旧任务只改了 DB 状态、内存控制事件未触发 —— 旧流会继续生成（缺陷本体）")
        assert tr.is_stopped(new_id) is False, "新任务不得被自己的登记误停"
        cur = await db_conn.execute("SELECT status FROM task_registry WHERE id=?", (old_id,))
        assert (await cur.fetchone())["status"] == "failed"


# ===========================================================================
# G 组 · 停止语义回归锁（解暂停死锁 + 级联 cancel）
# ===========================================================================

class TestStopSemanticsRegression:
    """协作式停止的两个既有前提（本轮 FIX-2 让目录/事实两 Tab 也能用到它们）：
    ① stop 必须同时 set pause_event（否则暂停中的任务永久挂起，stopped 事件发不出）；
    ② stop 必须级联 cancel 在飞子任务（含 AI 调用）。"""

    async def test_stop_releases_pause_wait_and_cancels_children(self, db_conn):
        tid = await tr.register_task("facts_generation", scheme_id="s3")
        tr.request_control(tid, "pause")
        assert tr._tasks[tid]["pause_event"].is_set() is False, "pause 应清 pause_event"

        async def _child():
            import asyncio
            await asyncio.sleep(30)

        import asyncio
        child = asyncio.create_task(_child())
        assert tr.register_child_task(tid, child) is True
        await asyncio.sleep(0)  # 让子任务真正进入 await

        tr.request_control(tid, "stop")
        assert tr.is_stopped(tid) is True
        # ① pause_event 已解 → wait_resume 立即返回（不永久挂起）
        await asyncio.wait_for(tr.wait_resume(tid), timeout=1)
        # ② 子任务被级联 cancel
        with pytest.raises(asyncio.CancelledError):
            await child

    def test_four_registered_types_all_have_stop_checkpoints(self):
        """四注册任务类型的生成循环必须仍有 wait_resume + is_stopped 检查点
        （加法式地板，防「停止按钮点了没人听」复发）。"""
        tree = _mod_ast("routers/sse_handlers.py")
        fns = [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        # 非空转自证：判据本身要能命中（模块里确有这两类调用）
        total_wait = sum(_count_calls(f, "wait_resume") for f in fns)
        total_stop = sum(_count_calls(f, "is_stopped") for f in fns)
        assert total_wait > 0 and total_stop > 0, "检查点判据静默失灵（AST 遍历未命中任何调用）"
        assert total_stop >= 8, (
            f"is_stopped 检查点只剩 {total_stop} 处（地板 8）：某条生成链的停止检查点被摘除")
        # 四类型各自的生成函数里都要同时有 wait_resume 与 is_stopped
        chained = [f for f in fns
                   if _count_calls(f, "wait_resume") > 0 and _count_calls(f, "is_stopped") > 0]
        assert len(chained) >= 4, (
            f"同时具备 wait_resume + is_stopped 的生成函数只剩 {len(chained)} 个（地板 4）："
            "目录/正文/事实/解析四条链的停止检查点被摘除")


# ===========================================================================
# H 组 · FIX-6 · SSE 泵结束的僵尸自愈
# ===========================================================================

class TestStreamEndReaping:
    """活体浏览器 E2E 实测（logs/backend.log，task=b3dc5ae0…）：目录生成的检查点
    已在 23:36:14 落库、前端此后每 3s 轮询 `GET /sse/task/{id}` 一直拿到
    `status=running`，而 `finish_task` 从未进入执行（全程无「任务终止」INFO）。
    客户端断开时的收尾是一串 await（checkpoint → finish_task），在 anyio 的取消
    风暴里会被**再次取消**；`reap_orphan_tasks` 又明文豁免"本进程内存态存在的任务"，
    于是形成「内存 running → DB 永不回收 → 停止按钮再也点不动」的死局，只能重启。
    FIX-6 补上唯一可靠的"流已结束"信号：with_heartbeat 的 finally 里的**同步**回调。"""

    async def test_abandoned_task_reaped_after_grace(self, db_conn):
        tid = await tr.register_task("outline_generation", scheme_id="z1")
        assert tr.mark_stream_ended(tid, grace_seconds=0.05) is True
        # 让出事件循环：detached 回收协程若"无宽限期"就会在此刻抢跑
        await asyncio.sleep(0.01)
        # 宽限期内**不得**立即 pop：生成器自己的 finally 还要靠 has_active_task
        # 判断"是否需要保存部分成果"，抢跑会抹掉 2026-09-16 的「断线成果可恢复」修复
        assert tid in tr._tasks, "宽限期内立即清理 = 抢跑，断线成果保存通道被摘除"
        await asyncio.sleep(0.4)
        assert tid not in tr._tasks, "SSE 已结束而内存仍 running = 僵尸本体（缺陷未修）"
        cur = await db_conn.execute(
            "SELECT status, message FROM task_registry WHERE id=?", (tid,))
        row = await cur.fetchone()
        assert row["status"] == "stopped", (
            "内存回收了但 DB 仍 running → 任务栏/刷新接管继续显示运行中")
        assert tr._STREAM_ABANDON_MESSAGE in (row["message"] or "")

    async def test_normal_finish_wins_over_grace(self, db_conn):
        tid = await tr.register_task("content_generation", scheme_id="z2")
        assert tr.mark_stream_ended(tid, grace_seconds=0.3) is True
        await tr.finish_task(tid, "completed", "正文生成完成")
        await asyncio.sleep(0.6)
        cur = await db_conn.execute("SELECT status FROM task_registry WHERE id=?", (tid,))
        assert (await cur.fetchone())["status"] == "completed", (
            "正常收尾必须优先：宽限回收不得把 completed 改写成 stopped")

    async def test_noop_for_unknown_and_terminal_task(self, db_conn):
        assert tr.mark_stream_ended("no-such-task") is False, "不存在的任务必须零动作"
        tid = await tr.register_task("facts_generation", scheme_id="z3")
        await tr.finish_task(tid, "completed", "事实提取完成")
        assert tr.mark_stream_ended(tid) is False, (
            "已终态（正常路径）返回 False —— on_end 每次都触发，幂等是零侵入的前提")

    async def test_control_contract_unchanged_after_stream_end(self, db_conn):
        """B-M17 契约：request_control 的返回值语义不因僵尸改变（只加 WARNING）。"""
        tid = await tr.register_task("outline_generation", scheme_id="z4")
        assert tr.mark_stream_ended(tid, grace_seconds=0.2) is True
        assert tr.request_control(tid, "stop") is True
        assert tr.is_stopped(tid) is True
        tr._tasks.pop(tid, None)   # 让宽限回收任务早退，不留悬挂
        await asyncio.sleep(0.3)


class TestWithHeartbeatOnEnd:
    """on_end 必须是**同步**回调，且三条退出路径都命中：正常耗尽 / 消费者提前
    aclose（= 客户端断开）/ 内层生成器抛异常。放在 finally 最前面——后面的
    `await gather(gen_task, hb_task)` 在取消风暴里会立刻再抛，挂在那之后的通知
    等于永远收不到。"""

    async def test_called_on_normal_exhaustion(self):
        calls: list[str] = []

        async def _g():
            yield "a"
            yield "b"

        out = [x async for x in SU.with_heartbeat(_g(), interval=1,
                                                  on_end=lambda: calls.append("end"))]
        assert out == ["a", "b"]
        assert calls == ["end"], "正常结束也必须通知（僵尸不挑退出方式）"

    async def test_called_when_consumer_closes_early(self):
        calls: list[str] = []

        async def _g():
            yield "a"
            await asyncio.sleep(30)   # 模拟在飞的长 AI 调用
            yield "b"

        agen = SU.with_heartbeat(_g(), interval=1, on_end=lambda: calls.append("end"))
        assert await agen.__anext__() == "a"
        await agen.aclose()            # 客户端断开
        assert calls == ["end"], (
            "消费者提前关闭正是产生僵尸的那条路径，on_end 缺席 = FIX-6 落空")

    async def test_callback_exception_does_not_break_stream(self):
        def _boom():
            raise RuntimeError("回调炸了")

        async def _g():
            yield "a"

        out = [x async for x in SU.with_heartbeat(_g(), interval=1, on_end=_boom)]
        assert out == ["a"], "on_end 抛异常不得吞掉业务流（收尾通知是加法式旁路）"

    def test_on_end_is_invoked_before_await_in_finally(self):
        """顺序锁（AST）：finally 里对 on_end 的引用必须早于该 finally 内的第一个
        await。摘到 `await gather(...)` 之后本用例即红 —— 那正是"通知永不到达"
        的形态（取消风暴打断的是 await，不是同步语句）。"""
        fn = _fn_ast("services/ai/sse_utils.py", "with_heartbeat")
        finals = [t.finalbody for t in ast.walk(fn)
                  if isinstance(t, ast.Try) and t.finalbody]
        assert finals, "判据静默失灵（with_heartbeat 内未找到 finally）"

        def _refs(body):
            # on_end 是**形参**，引用形态是 ast.Name（不是 Attribute）
            mod = ast.Module(body=list(body), type_ignores=[])
            return [n.lineno for n in ast.walk(mod)
                    if isinstance(n, ast.Name) and n.id == "on_end"]

        # 内层 _gen_to_queue 也有 finally，必须挑"引用了 on_end 的那个"
        with_cb = [(f, _refs(f)) for f in finals if _refs(f)]
        assert with_cb, "finally 里根本没有 on_end 引用（钩子被摘）"
        fin, cb_lines = with_cb[0]
        mod = ast.Module(body=list(fin), type_ignores=[])
        aw_lines = [n.lineno for n in ast.walk(mod) if isinstance(n, ast.Await)]
        assert aw_lines, "判据静默失灵（该 finally 内没有 await，顺序无从谈起）"
        assert min(cb_lines) < min(aw_lines), (
            "on_end 被挪到 await 之后 → 断连场景下通知可能永不到达（僵尸回归）")


# ===========================================================================
# I 组 · FIX-6 · reap_orphan_tasks 的内存豁免收紧
# ===========================================================================

async def _age_row(db_conn, tid: str):
    await db_conn.execute(
        "UPDATE task_registry SET updated_at=? WHERE id=?",
        ("2000-01-01T00:00:00", tid))
    await db_conn.commit()


class TestReaperMemoryExemption:
    """旧实现第二层防误伤是「本进程内存态 `_tasks` 中存在的任务一律不动」——
    在跑的任务当然不能动，但内存僵尸（stream 已结束、收尾协程被取消）也住在
    `_tasks` 里，于是它既进不了 DB 回收、也没有内存回收通道。现豁免只看
    "是否仍是活跃实例"。"""

    async def test_in_flight_memory_task_still_exempt(self, db_conn):
        tid = await tr.register_task("content_generation", scheme_id="r1")
        await _age_row(db_conn, tid)   # 长时间无进度的在跑任务
        assert await tr.reap_orphan_tasks(max_stale_seconds=0) == 0, (
            "内存有活跃实例就回收 = 误伤正在生成的任务（原三层防误伤的初衷）")
        assert tid in tr._tasks
        cur = await db_conn.execute("SELECT status FROM task_registry WHERE id=?", (tid,))
        assert (await cur.fetchone())["status"] == "running"
        tr._tasks.pop(tid, None)

    async def test_abandoned_memory_task_is_reaped(self, db_conn):
        tid = await tr.register_task("content_generation", scheme_id="r2")
        await _age_row(db_conn, tid)
        # 已被 on_end 标记「流已结束」且超过 30s 仍未落终态 → 不再是活跃实例
        tr._tasks[tid]["stream_ended_at"] = time.monotonic() - 60
        assert await tr.reap_orphan_tasks(max_stale_seconds=0) == 1, (
            "被标记放弃的内存条目仍被豁免 = 僵尸无自愈通道（缺陷本体）")
        cur = await db_conn.execute("SELECT status FROM task_registry WHERE id=?", (tid,))
        assert (await cur.fetchone())["status"] == "stopped"

    async def test_freshly_marked_task_keeps_grace_window(self, db_conn):
        tid = await tr.register_task("content_generation", scheme_id="r3")
        await _age_row(db_conn, tid)
        tr._tasks[tid]["stream_ended_at"] = time.monotonic()   # 刚标记，还在宽限内
        assert await tr.reap_orphan_tasks(max_stale_seconds=0) == 0, (
            "刚标记就回收会抢跑正常收尾（终态消息被写成自动回收）")
        tr._tasks.pop(tid, None)


# ===========================================================================
# J 组 · FIX-7 · 目录链 task_id 提前下发接线锁
# ===========================================================================

class TestOutlineEarlyTaskIdDispatch:
    """RC-1：旧实现 `event_stream` 先 `await _assemble_context()`（读文档/摘要/规范、
    可能发 AI 调用）再 yield connecting。实测 trace=03563da7f91a 该窗口 30s，期间
    前端 `activeTaskIdRef` 仍是上一代**已终结**的 id → 用户点停止打到死 id，
    新任务从未收到指令，表现为「停止按钮完全无效」。"""

    def _outline_node(self):
        return _fn_ast("routers/sse_handlers.py", "generate_outline")

    def _lineno(self, node, pred):
        return [n.lineno for n in ast.walk(node)
                if pred(n) and getattr(n, "lineno", 0)]

    def test_connecting_is_sent_before_assembly(self):
        node = self._outline_node()
        holders = self._lineno(
            node, lambda n: isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Subscript) and getattr(t.value, "id", None)
                == "_stream_holder" for t in n.targets))
        connect = self._lineno(
            node, lambda n: isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "update_progress" and any(
                k.arg == "event" and isinstance(k.value, ast.Constant)
                and k.value.value == "connecting" for k in n.keywords))
        yields = self._lineno(node, lambda n: isinstance(n, ast.Yield))
        assemble = self._lineno(
            node, lambda n: isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "_assemble_context")
        # 非空转自证：三段判据本身都要命中
        assert holders and connect and assemble and yields, (
            "判据静默失灵（未命中 holder 赋值 / connecting / yield / 装配调用）")
        hc, mc, ac = min(holders), min(connect), min(assemble)
        # ① holder 必须早于 connecting —— 否则流恰在 connecting 之前结束时
        #    on_end 拿到空 holder，FIX-6 对本链落空
        assert hc < mc, "task_id 未先写进 _stream_holder（on_end 会拿到空 holder）"
        # ② connecting 必须早于装配 —— 装配阶段可能耗时数十秒（RC-1 本体）
        assert mc < ac, (
            "connecting 的进度写入晚于装配 → task_id 下发窗口重新张开，"
            "用户在这段窗口点的停止会打到上一代已终结的 id")
        # ③ 光写 DB 不算，必须真的 yield 给前端（紧跟在 connecting 之后 3 行内）
        assert any(mc <= y <= mc + 3 for y in yields), (
            "connecting 只写了进度库、没有下发 SSE 事件 → 前端 ref 仍拿不到 task_id")

    def test_outline_stream_wires_on_end(self):
        node = self._outline_node()
        pumps = [n for n in ast.walk(node)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "with_heartbeat"]
        assert pumps, "判据静默失灵（目录链未命中 with_heartbeat 调用）"
        assert any(any(k.arg == "on_end" for k in p.keywords) for p in pumps), (
            "目录链的 with_heartbeat 没接 on_end → FIX-6 对本链零效果")

    def test_guard_calls_mark_stream_ended(self):
        guard = _fn_ast("routers/sse_handlers.py", "_stream_end_guard")
        inner = [n for n in ast.walk(guard)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == "_on_end"]
        assert inner, "判据静默失灵（_stream_end_guard 内未找到 _on_end）"
        assert _count_calls(inner[0], "mark_stream_ended") == 1, (
            "on_end 没真正通知 task_registry（只留了个空壳回调）")
