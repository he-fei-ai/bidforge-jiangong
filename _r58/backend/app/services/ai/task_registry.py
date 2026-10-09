"""Task Registry：任务持久化、断点恢复、SSE 订阅、暂停/停止控制

内存控制结构：
- pause_event: 默认 set（运行中），clear = 暂停，set = 恢复
- stop_event: 默认未 set，set = 停止
- cancel_tasks: set() 存储并发子任务的 asyncio.Task 引用，stop 时统一取消
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta

from app.db import get_conn, retry_db_op, safe_rowcount
from app.services import activity_broadcaster as _ab

# ✅ Fix C 接入：pause 时同步调用 reject_waiters 打断全局 provider 层排队协程
from app.services.ai.workflows_base import concurrency_controller as _cc

logger = logging.getLogger("task_registry")

# ✅ 进度落库节流参数（2026-09-15）：内存态与 SSE 广播始终实时，
#    仅对 DB 落库做节流以降低写放大（长方案 60 章会产生上百次进度写盘）。
_PROGRESS_DB_MIN_INTERVAL = 0.5   # 秒：距上次进度落库的最小间隔
_PROGRESS_DB_MIN_DELTA = 0.005    # 进度最小增量（0.5%）

# task_id -> 运行时控制状态
_tasks: dict[str, dict] = {}
# ✅ 并发注册互斥锁：防止两个并发请求同时通过防僵尸检查后各自 INSERT running 任务
_register_lock = asyncio.Lock()


class TaskTypeConflict(RuntimeError):
    """跨类型任务互斥冲突（目录生成 ⇄ 正文生成）。

    ✅ BUG 修复（2026-10-05 · F5 · 跨类型 TOCTOU）：目录/正文各自的入口函数在
    **路由体**里做跨类型 409 判定（generate_outline :3982 / generate_content :4862），
    而 `register_task` 要等路由返回、ASGI 送出响应头、生成器首次被 next() 才执行
    （:4183 / :4902）—— 中间必然 `await`，另一路请求完全可能在窗口内通过自己的
    pre-guard 并先注册，于是两路同时 running：目录整表重建 sections 与正文逐章
    UPDATE 并发 → 正文被 wipe / 图表登记挂到已删除章节 id。

    修法：互斥判定放进 `_register_lock`（与既有同型防僵尸判定同一把锁），
    且**放在本任务 INSERT 之后** —— 锁把两路注册串行化，先注册者判定无冲突放行、
    后注册者判定命中先注册者而抛本异常，**结构上不可能"双双 abort"**。
    """

    def __init__(self, task_id: str, message: str, conflicts: list[str]):
        self.task_id = task_id
        self.conflicts = list(conflicts)
        super().__init__(message)


async def register_task(task_type: str, project_id: str = "", scheme_id: str = "",
                        checkpoint: dict | None = None,
                        conflict_types=()) -> str:
    """注册任务。`conflict_types` 为空时行为与历史逐字一致（零影响面）。

    传入 `conflict_types` 时：本任务 INSERT 成功后，若同 scheme 下存在这些类型的
    running/paused 任务，则把**本任务**置 failed 并抛 :class:`TaskTypeConflict`
    （返回的 `task_id` 仍在，调用方可以直接把它带进 error 事件下发给前端）。
    """
    # ✅ 防僵尸：同一 scheme + 同一类型，不允许并存多个 running 任务
    # （用户重复点"生成"时，旧 SSE 连接可能还没收到 stop 事件，新任务就已经 register 了）
    # ✅ 并发修复：防僵尸 SELECT + INSERT 必须原子完成。原实现两个并发协程都在
    # 对方 INSERT 前执行了 stale SELECT（都查不到对方），随后各自 INSERT 一条 running
    # 并各自启动 event_stream，两路 AI 并发生成同一方案章节、竞态覆盖写库。
    async with _register_lock:
        conn = await get_conn()
        cur = await conn.execute(
            "SELECT id FROM task_registry "
            "WHERE scheme_id=? AND task_type=? AND status IN ('running','paused')",
            (scheme_id, task_type))
        stale = [r[0] for r in await cur.fetchall()]
        if stale:
            placeholders = ",".join("?" * len(stale))
            await conn.execute(
                f"UPDATE task_registry SET status='failed', message='已被新任务替换（{len(stale)} 条旧 running）', "
                f"updated_at=? WHERE id IN ({placeholders})",
                [datetime.now().isoformat(), *stale])
            await conn.commit()
            logger.warning("register_task: 清理了 %d 条同 scheme=%s type=%s 的旧 running 任务 %s",
                           len(stale), scheme_id, task_type, stale)
            # ✅ BUG 修复：旧实现只改 DB 状态，内存 _tasks 里的旧任务控制事件未触发——
            # 旧 event_stream 仍在跑（AI 调用照常、sections 照常写库），与新任务竞态覆盖。
            # 现对仍在内存的旧任务同步触发 stop（set stop_event + cancel 子任务），
            # 旧流会在下一个 wait_resume/is_stopped 检查点尽快退出。
            for old_id in stale:
                if old_id in _tasks:
                    request_control(old_id, "stop")

        tid = str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO task_registry (id, task_type, project_id, scheme_id, status, progress, checkpoint_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (tid, task_type, project_id, scheme_id, "running", 0.0,
             json.dumps(checkpoint or {}, ensure_ascii=False)))
        await conn.commit()
        # ✅ 跨类型互斥（F5）：判定必须在**本任务 INSERT 之后**、仍在锁内（见类 docstring）。
        if conflict_types:
            _ctypes = tuple(conflict_types)
            marks = ",".join("?" * len(_ctypes))
            _ccur = await conn.execute(
                "SELECT DISTINCT task_type FROM task_registry WHERE scheme_id=?"
                f" AND status IN ('running','paused') AND task_type IN ({marks})",
                [scheme_id, *_ctypes])
            # R13：execute() 可能返回 None（此处刚成功 INSERT，实为不可达，
            # 仍守卫以免 AttributeError 把一次合法注册变成 500）。
            _crows = (await _ccur.fetchall()) if _ccur is not None else []
            rows = [r[0] for r in _crows]
            if rows:
                _msg = f"与正在运行的 {'、'.join(rows)} 任务互斥，本任务已放弃"
                await conn.execute(
                    "UPDATE task_registry SET status='failed', message=?, updated_at=? WHERE id=?",
                    (_msg, datetime.now().isoformat(), tid))
                await conn.commit()
                logger.warning(
                    "register_task: 跨类型互斥拒绝 %s（scheme=%s，冲突类型=%s，task=%s）",
                    task_type, scheme_id, rows, tid)
                raise TaskTypeConflict(tid, _msg, rows)
    pause_event = asyncio.Event()
    pause_event.set()  # 默认为运行中（未暂停）
    stop_event = asyncio.Event()
    _tasks[tid] = {
        "type": task_type,
        "status": "running",
        "progress": 0.0,
        "message": "",
        "scheme_id": scheme_id,
        "pause_event": pause_event,
        "stop_event": stop_event,
        "child_tasks": set(),
        "last_control": None,
        # ✅ 进度增强（2026-09-15）：启动时间 + 运行统计 + 落库节流水位
        "started_at": time.monotonic(),
        # ✅ 暂停时长账本（2026-09-17）：「已耗时 / ETA」必须扣除暂停时段，
        #    否则用户点暂停后看到耗时仍一路增长，无法判断暂停是否真的生效。
        "paused_at": None,        # 当前暂停段的 monotonic 起点（未暂停时 None）
        "paused_total": 0.0,      # 已完成暂停段的累计秒数
        "stats": {},
        "_db_progress_at": 0.0,
        "_db_progress_val": -1.0,
    }
    _ab.notify()
    return tid


async def _update_task_progress_db(task_id: str, progress: float, message: str):
    """任务进度落库（幂等单语句，供 retry_db_op 安全重放）。"""
    conn = await get_conn()
    await conn.execute(
        "UPDATE task_registry SET progress=?, message=?, updated_at=? WHERE id=?",
        (progress, message, datetime.now().isoformat(), task_id))
    await conn.commit()


async def update_progress(task_id: str, progress: float, message: str = "",
                          event: str = "progress", force: bool = False):
    """更新任务进度：内存态/SSE 广播实时，DB 落库节流。

    ✅ 增强（2026-09-15）：原实现每次调用都 UPDATE+commit 一次。正文生成
    链路里每章至少 1 次（进度）+ 1 次（终态），长方案（60 章）会产生
    上百次写盘，且这些 commit 与章节落库事务在同一连接上交错。现改为：
      - 内存态与 SSE 广播**始终实时**（用户可见进度不受任何影响）；
      - DB 落库按「最小间隔 + 最小增量」节流，降低写放大；
      - 终态（force=True，如 completed/stopped/failed）强制落库，保证进程重启后
        task_status 回退查询拿到的是最终值。
    """
    # ✅ 终态事件必须强制落库：进度节流会让 DB 落后于内存态，而
    #    completed/stopped/failed/error 是轮询回退（task_status）读取的最终值。
    if event in ("completed", "degraded", "stopped", "failed", "error"):
        force = True
    state = _tasks.get(task_id)
    now = time.monotonic()
    need_write = force or state is None
    if state is not None and not force:
        last_at = state.get("_db_progress_at", 0.0)
        last_val = state.get("_db_progress_val", -1.0)
        # 间隔够久 或 增量够大 就落库
        if ((now - last_at) >= _PROGRESS_DB_MIN_INTERVAL
                or abs(progress - last_val) >= _PROGRESS_DB_MIN_DELTA):
            need_write = True
    if need_write:
        try:
            # ✅ 2026-09-23（正文生成深度审计 · P0）：进度落库此前零重试，
            #    运行库实测 20:44~20:48 连续 4 次 "database is locked" 失败
            #    （logs/backend.log，trace=9fe4498339da）。进度本身不影响展示
            #    （内存态 + SSE 广播始终实时），但**终态事件是 force=True**，
            #    写失败会让 DB 里的 progress 停在中间值，task_status 轮询回退
            #    查询拿到的就是脏值。现接入共享重试（仅重试瞬态锁错误）。
            await retry_db_op(lambda: _update_task_progress_db(
                task_id, progress, message))
        except Exception as e:
            # 进度落库失败不应中断生成主流程（内存态与广播仍继续）
            logger.warning("update_progress 落库失败（已忽略）: %s", e)
        if state is not None:
            state["_db_progress_at"] = now
            state["_db_progress_val"] = progress
    if state is not None:
        state["progress"] = progress
        state["message"] = message
    await broadcast(task_id, {"event": event, "task_id": task_id,
                              "progress": progress, "message": message})
    _ab.notify()


async def update_task_stats(task_id: str, **stats):
    """广播轻量运行统计（不写 DB）。

    ✅ 新增（2026-09-15）：正文生成中单章 AI 调用可能持续数分钟，期间没有
    任何业务事件；原实现只有 10s 一次的 SSE 心跳 comment（前端不解析），
    用户看到进度条长时间静止。本函数用于在长调用期间持续推送
    「已耗时 / 进行中章节 / 累计字数 / ETA」等运行态，让前端保持实时感知。
    统计只存在于内存，不落库（无持久化价值，且高频写盘得不偿失）。
    """
    state = _tasks.get(task_id)
    if state is not None:
        state.setdefault("stats", {}).update(stats)
    await broadcast(task_id, {"event": "stats", "task_id": task_id, **stats})
    _ab.notify()


def get_task_stats(task_id: str) -> dict:
    """读取任务的最新运行统计（供心跳等场景读取，无则返回空字典）。"""
    state = _tasks.get(task_id)
    if not state:
        return {}
    return dict(state.get("stats") or {})


def get_task_started_at(task_id: str) -> float | None:
    """任务启动的 monotonic 时间戳（供耗时/ETA 计算）。"""
    state = _tasks.get(task_id)
    if not state:
        return None
    return state.get("started_at")


def get_task_paused_ms(task_id: str) -> float:
    """任务累计暂停毫秒数（含当前正在进行的暂停段）。

    ✅ 2026-09-17 新增：协作式暂停不打断已在飞的 AI 调用，用户侧唯一的
    「暂停已生效」证据就是「已耗时停止增长」。因此耗时展示必须扣除暂停时段。
    """
    state = _tasks.get(task_id)
    if not state:
        return 0.0
    total = float(state.get("paused_total") or 0.0)
    at = state.get("paused_at")
    if at is not None:
        total += time.monotonic() - at
    return max(0.0, total * 1000.0)


def get_task_elapsed_ms(task_id: str) -> float | None:
    """任务的**有效运行**毫秒数（总耗时扣除暂停时长）；任务不存在返回 None。

    ✅ 2026-09-17 新增：活动快照的 `elapsed` 由本函数产出，保证暂停期间
    任务栏「已耗时」冻结，UI 语义与 `paused` 状态一致。
    """
    started = get_task_started_at(task_id)
    if started is None:
        return None
    return max(0.0, (time.monotonic() - started) * 1000.0 - get_task_paused_ms(task_id))


async def _write_task_terminal_db(task_id: str, status: str, message: str):
    """任务终态落库（幂等单语句，供 retry_db_op 安全重放）。

    ✅ 竞态守卫（2026-09-17 P0 修复）：只从 running/paused 写入终态。
       防止 register_task 的"替换旧任务"逻辑先写 status='failed'，
       然后被晚到的正常 finish_task 无条件 UPDATE 覆盖回 'completed'
       （反之亦然——正常完成后又被替换覆盖回 failed）。
       重试重放时 rowcount 可能为 0（第二次进入已是终态），这是安全的：
       UPDATE 幂等，且 WHERE 条件本身阻止终态互相覆盖。
    """
    conn = await get_conn()
    await conn.execute(
        "UPDATE task_registry SET status=?, message=?, updated_at=? "
        "WHERE id=? AND status IN ('running','paused')",
        (status, message, datetime.now().isoformat(), task_id))
    await conn.commit()


async def finish_task(task_id: str, status: str = "completed", message: str = ""):
    """把任务置终态并清理内存态（**异常安全**）。

    ✅ BUG 修复（G12-5 · 2026-09-20）：旧实现把「写 DB」与「pop 内存态」写成
    顺序语句 —— `get_conn()` / UPDATE / commit / broadcast 任一步抛异常
    （连接池耗尽、磁盘满、并发写冲突、订阅者队列异常）就直接跳出函数，
    `_tasks[task_id]` 永远残留。而 sections 路由的 409 竞态守卫遍历 _tasks
    判断「正文是否在跑」，于是本方案的手工保存 / 重置正文从此**永久** 409
    （用户表现为「生成早就停了，却再也存不了任何一章」），只能重启后端。
    现把 DB 写库与广播各自 try/except，内存清理放进 finally 无条件执行，
    保证内存态绝不泄漏。
    """
    state = _tasks.get(task_id)
    _terminal_db_ok = False
    try:
        # ✅ 2026-09-23（正文生成深度审计 · P0 最高优先级）：终态落库此前零重试。
        #    运行库实测 2026-09-23 20:48:27（logs/backend.log，trace=9fe4498339da）：
        #    finish_task 终态落库失败: database is locked → task_registry 表里该任务
        #    永远停留在 running。而「终态」是前端 pollTaskUntilTerminal 唯一等待的
        #    信号，也是 sections 路由 409 竞态守卫（content_generation_in_progress）
        #    之外第二条判断依据；结果表现为「生成早就停了，却再也存不了任何一章、
        #    任务栏永远转圈」，只能重启后端（当天 20:51:33 启动清理日志
        #    「已清理 1 个中断遗留任务（completed=0, failed=1）」即为此任务的归宿）。
        #    内存态清理仍在下方 finally 无条件执行（G12-5 不变量），
        #    重试只补「DB 可见性」这一环，语义与并发安全性不变。
        await retry_db_op(lambda: _write_task_terminal_db(
            task_id, status, message))
        _terminal_db_ok = True
    except Exception as e:
        # 终态写库失败必须显式记录（否则用户看到「任务还在跑」却查不到原因），
        # 但绝不能阻断下面的内存清理 —— 那是防泄漏的关键一步。
        logger.error("finish_task 终态落库失败（task=%s status=%s）: %s",
                     task_id, status, e, exc_info=True)

    # ✅ F6（2026-10-05 · 日志准确性）：任务生命周期的**成功路径此前零日志** ——
    #    finish_task 是所有任务类型（目录/正文/事实/一致性/…）唯一的终态收口点，
    #    正常完成后 logs/backend.log 里查不到任何一行，排障时无法区分
    #    「任务没跑」与「跑完了但没记录」。此处补状态迁移记录（INFO = 正常追踪，
    #    见 AGENTS.md §3.1.6）；业务级失败/异常仍由各自调用点按 ERROR/WARNING
    #    落盘，不在此重复报错。仅在终态**成功落库**时记录 —— 落库失败时上一行
    #    已按 ERROR 落盘，避免同一事件出现「一条失败 + 一条完成」的矛盾日志。
    if _terminal_db_ok:
        _st = state or {}
        _type = _st.get("type", "?")
        _scheme = _st.get("scheme_id", "?") or "?"
        if status == "completed":
            logger.info("任务完成（type=%s · scheme=%s · task=%s）",
                        _type, _scheme, task_id)
        elif status == "degraded":
            logger.info("任务降级完成（type=%s · scheme=%s · task=%s）: %s",
                        _type, _scheme, task_id, (message or "")[:120])
        else:
            logger.info("任务终止 %s（type=%s · scheme=%s · task=%s）: %s",
                        status, _type, _scheme, task_id,
                        (message or "")[:120])

    if state:
        state["status"] = status
        # ✅ 收口修复：非正常完成的任务（stopped/failed/中断清理）必须同步取消所有
        # 后台子任务。原实现只有 request_control("stop") 才 cancel，而 event_stream
        # 的 CancelledError 分支直接 finish_task 后 child_tasks 仍在跑——DB 已是
        # 终态、_tasks 已被 pop，后台生成彻底失去停止通道（继续写库/写进度）。
        if status != "completed":
            for t in list(state["child_tasks"]):
                if not t.done():
                    t.cancel()
    try:
        # completed/stopped/failed 事件都广播
        broadcast_event = "completed" if status == "completed" else status
        await broadcast(task_id, {"event": broadcast_event,
                                  "task_id": task_id, "message": message})
    except Exception as e:
        logger.warning("finish_task 广播失败（已忽略，不影响终态）: %s", e)
    finally:
        # ✅ G12-5：内存态清理必须**无条件**执行 —— 即使上面的写库 / 广播
        # 抛了异常，也不能让终态任务残留在 _tasks 里（见函数说明）。
        _tasks.pop(task_id, None)
        _ab.notify()



async def _set_task_status_db(task_id: str, status: str) -> int:
    """控制指令落库（幂等单语句，供 retry_db_op 安全重放）。

    返回受影响行数：0 表示任务已处终态或不存在（竞态守卫生效，非错误）。
    """
    conn = await get_conn()
    cur = await conn.execute(
        "UPDATE task_registry SET status=?, updated_at=? "
        "WHERE id=? AND status IN ('running','paused')",
        (status, datetime.now().isoformat(), task_id))
    await conn.commit()
    # ✅ 2026-09-30 收敛到 safe_rowcount 单一出口（R13）：旧写法
    #   int(getattr(cur, "rowcount", 0) or 0) 不抛 AttributeError，但
    #   execute() 返回 None 时**静默返回 0、零日志** —— 调用方会误判为
    #   「任务已是终态」（正常的竞态守卫结果），从而把一次 R13 写失败
    #   伪装成正常语义。safe_rowcount 打 WARNING 并把负 rowcount 归一为 0。
    return safe_rowcount(cur, what=f"任务控制指令落库 status={status}")


async def set_task_status(task_id: str, status: str) -> bool:
    # ✅ 竞态守卫：任务可能在控制指令到达瞬间自然完成（finish_task 已写终态并 pop 内存）。
    # 无条件 UPDATE 会把 completed/failed/stopped 覆盖回 paused/running/stopped，产生永久
    # 僵尸（前端 pollTaskUntilTerminal 永远等不到终态，空转 10~20 分钟）。
    # ✅ 返回值（2026-09-17）：True=确实发生状态迁移，False=已是终态/任务不存在。
    #    控制路由据此区分「真正停掉了」与「本来就结束了」，避免误报成功。
    # ✅ 2026-09-23（正文生成深度审计 · P0）：暂停/停止控制指令的落库此前零重试。
    #    用户点「停止」恰逢 DB 锁竞争时，指令静默丢失（异常冒泡到路由变成 500），
    #    而生成任务仍在后台跑。与 finish_task 同一根因，接入共享重试。
    rc = await retry_db_op(lambda: _set_task_status_db(task_id, status))
    if rc == 0:
        logger.info("set_task_status: 任务 %s 已处终态或不存在，忽略 %s 覆盖", task_id, status)
        return False
    if task_id in _tasks:
        _tasks[task_id]["status"] = status
        _ab.notify()
    return True


# ---------- 真正的控制 API ----------

def request_control(task_id: str, action: str):
    """从后端路由调用，将暂停/恢复/停止指令作用到 Event 上。"""
    if task_id not in _tasks:
        logger.warning("request_control: 任务 %s 不存在（可能已结束）", task_id)
        return False
    state = _tasks[task_id]
    state["last_control"] = action
    logger.info("control=%s applied to task %s", action, task_id)
    now = time.monotonic()
    if action == "pause":
        # ✅ 暂停账本：重复 pause 不得重置已累计时长（否则可无限刷新清零）
        if state.get("paused_at") is None:
            state["paused_at"] = now
        state["pause_event"].clear()
        # ✅ 2026-09-23（Fix C 接入）：把全局 provider 层正在排队等待
        #    concurrency_controller.semaphore 许可的协程全部拒绝。
        #    否则暂停后「下一批章节」会持续占满许可排队，直到已有在飞请求
        #    释放许可才被放行，然后才在章节生成循环顶部的 wait_resume 挂起——
        #    表现为暂停响应慢至秒级。这里同步调用，被拒绝的协程在
        #    provider_factory 会收到 CancelledError（走 except CancelledError 分支
        #    正确记账 in_flight 后向上抛），最终任务在下一个 wait_resume 检查点
        #    挂起。已在运行的协程不受影响，会在本轮自然结束后进入 wait_resume。
        try:
            n_rejected = _cc.semaphore.reject_waiters(owner_id=task_id)
            if n_rejected > 0:
                logger.info(
                    "pause task=%s：拒绝并发排队中的 %d 个 AI 请求协程（加速暂停响应）",
                    task_id, n_rejected)
        except Exception as e:
            logger.warning("pause task=%s：reject_waiters 调用失败（已忽略）: %s",
                           task_id, e)
    elif action == "resume":
        # ✅ 结算暂停段；重复 resume 无副作用（paused_at 为 None 时跳过）
        if state.get("paused_at") is not None:
            state["paused_total"] = float(state.get("paused_total") or 0.0) + (
                now - state["paused_at"])
            state["paused_at"] = None
        state["pause_event"].set()
    elif action == "stop":
        state["stop_event"].set()
        # ✅ 死锁修复：若任务正处于 paused（pause_event 被 clear），
        # wait_resume 会永久挂起导致 stopped 事件永远发不出去。stop 必须同时
        # set pause_event，让挂起的循环立即通过、随后被 is_stopped 检查点退出。
        state["pause_event"].set()
        # 同时取消所有正在运行的子任务
        for t in list(state["child_tasks"]):
            if not t.done():
                t.cancel()
    return True


async def wait_resume(task_id: str):
    """正文/目录生成循环顶部调用：暂停时挂起，恢复时返回。

    ✅ 修复：改为 async def。原实现任务不存在时返回 None，
    调用方 `await wait_resume(...)` 会触发 TypeError (await None)。
    """
    state = _tasks.get(task_id)
    if not state:
        return
    await state["pause_event"].wait()


def is_stopped(task_id: str) -> bool:
    state = _tasks.get(task_id)
    if not state:
        return True  # 任务不存在视为已结束
    return state["stop_event"].is_set()


def has_active_task(task_id: str) -> bool:
    """任务是否仍处于活动状态（未进入终态、尚未被 finish_task 清理）。

    ✅ 供 event_stream 的 finally 兜底使用：客户端断开时生成器被 aclose，
    GeneratorExit 直接落在 yield 处，except 分支不会执行——只有此函数
    能区分"已正常收尾"与"需要兜底 finish_task"。"""
    return task_id in _tasks


def register_child_task(task_id: str, child: asyncio.Task) -> bool:
    """把子任务登记到父任务的 child_tasks 集合。

    ✅ P2 修复（2026-10-04 · 子任务泄漏）：旧实现父任务不存在时**静默跳过**，
    返回隐式 None，调用方（例如 _ai_call_with_stop_awareness）无法区分
    "登记成功" 与 "父任务已被 finish_task 清出 _tasks、child_tasks 集合随之消失"，
    结果：父任务已终态，子任务（如一次跑 30~180s 的 AI 调用）继续跑到自然结束
    甚至被 finish_task 之后才完成的回调 `state["child_tasks"].discard(child)`
    访问已被清理的 state 引用——极端情况下子任务孤儿化，用户点停止后依然
    占用连接池/AI quota 直到超时预算耗尽。
    现改为显式返回 bool：False 表示父任务已不存在（或登记失败），
    调用方应立即 `child.cancel()`，避免挂住。
    """
    state = _tasks.get(task_id)
    if not state:
        return False
    state["child_tasks"].add(child)
    child.add_done_callback(lambda t: state["child_tasks"].discard(t))
    return True


# ---------- SSE 订阅 ----------

# ✅ 2026-10-04 死链路清理：
#   `_subscribers: dict[task_id -> list[Queue]]` 及其配套的
#   subscribe/unsubscribe 曾在旧实现里承担「task_registry 主动 push 事件」
#   的语义，但生产代码从未 add 过任何 Queue —— SSE 走的是 StreamingResponse
#   生成器 yield 直出，activity_broadcaster.notify() 只做「让 SSE 端
#   heartbeat 通道刷新状态」的间接通知。保留 `_subscribers` 让
#   broadcast() 永远在 for-loop 空列表上打转，属于**纯死代码**。
#   现将 `broadcast()` 降级为 no-op（**保留函数名与签名**，供
#   monkeypatch 测试沿用旧接口；生产代码调用它即等于静默空转），
#   并同步删除 `_subscribers` 字典与 `finish_task` 中的 pop 语句。
#   ⚠️ 依赖 `_subscribers` 语义的旧测试已改为直接验证内存态 / DB 状态。
async def broadcast(task_id: str, payload: dict):
    return



async def get_interrupted_tasks() -> list[dict]:
    """进程重启后恢复：找出 running/paused 状态的旧任务"""
    conn = await get_conn()
    cur = await conn.execute(
        "SELECT * FROM task_registry WHERE status IN ('running','paused') ORDER BY created_at DESC")
    return [dict(r) for r in await cur.fetchall()]



async def reap_orphan_tasks(max_stale_seconds: float = 900.0) -> int:
    """运行期僵尸任务回收：DB 仍 running/paused 但本进程无内存态且已陈旧 → 落终态。

    ✅ P0（2026-09-23 断连僵尸任务）：既有防线只有两道 ——
    ① main.lifespan 启动清理（get_interrupted_tasks，仅进程重启时执行一次）；
    ② task_control 对单个任务的 _stopped_orphan_rows（需用户手点停止）。
    SSE 断连/流异常若把任务留在「DB running、内存无态」（finish_task 未跑到），
    任务栏会**永远转圈**，直到重启进程或逐个手点停止。本函数由 lifespan
    周期调用（每 5 分钟），补上「无人干预也能自愈」的第三道防线。

    安全性（三层防误伤）：
    - 只扫 `updated_at < now - max_stale_seconds`：刚注册/刚有进度的任务不命中；
    - 本进程内存态 `_tasks` 中存在的任务一律不动（在跑、或只是长时间无进度）；
    - 单语句幂等 UPDATE，只迁移 running/paused。
    DB 异常只记 WARNING 不抛出 —— 下个周期自然重试，绝不影响主流程。
    """
    try:
        conn = await get_conn()
        cutoff = (datetime.now() - timedelta(seconds=max_stale_seconds)).isoformat()
        cur = await conn.execute(
            "SELECT id FROM task_registry "
            "WHERE status IN ('running','paused') AND updated_at < ?",
            (cutoff,))
        rows = [r[0] for r in await cur.fetchall()]
        orphan_ids = [i for i in rows if i not in _tasks]
        if not orphan_ids:
            return 0
        ph = ",".join("?" * len(orphan_ids))
        cur2 = await conn.execute(
            "UPDATE task_registry SET status='stopped', "
            "message='后台周期回收：无运行实例的僵尸任务', updated_at=? "
            f"WHERE id IN ({ph}) AND status IN ('running','paused')",
            [datetime.now().isoformat(), *orphan_ids])
        await conn.commit()
        changed = safe_rowcount(cur2, what="僵尸后台任务批量置 stopped")
        if changed:
            _ab.notify()
            logger.warning(
                "reap_orphan_tasks: 已回收 %d 个僵尸任务（DB running 但无运行实例）: %s",
                changed, orphan_ids)
        return changed
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("reap_orphan_tasks 失败（已忽略，下周期重试）: %s", e)
        return 0
