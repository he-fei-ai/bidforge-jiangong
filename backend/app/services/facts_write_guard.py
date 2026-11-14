"""项目级事实写保护（T-04 · 2026-11-06）。

背景：全局事实有两条写入来源——
1) 用户界面手工新增 / 编辑 / 确认 / 裁决 / 清空（REST → routers/global_facts.py）；
2) 后台 AI 提取结果落库（SSE → facts_extractor.persist_extraction）。

两条链路都遵循「读取现有事实 → 计算保留/删除/插入 → 提交」，升级前没有
任何互斥：手工保存与提取落库并发时，后提交者会覆盖先提交者的结果（典型
症状是用户刚确认的事实被重新提取打回、或手工新增的行被 DELETE 清掉）。

本模块提供**项目级**写保护（同一项目下不同方案也互斥，因为项目共享层
``scheme_id=''`` 是双方都会写的作用域）：

- 进程内：按「事件循环 + 项目」隔离的 ``asyncio.Lock``，避免同进程协程重入；
  （按 loop 隔离是为了让每个事件循环（含测试）拥有独立锁，不触发
  "Lock bound to a different event loop"）。
- 数据库层：``facts_write_leases`` 租约表（``INSERT ... ON CONFLICT`` 原子抢占，
  租约超时后可被后来者抢占），覆盖多 worker 场景；进程崩溃不会永久锁死。

冲突时抛 ``FactsWriteConflict``，由调用方转成可见的 409，而不是静默后写覆盖。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import AsyncIterator

logger = logging.getLogger(__name__)

#: 进程内等待上限（秒）。超时即判定冲突，避免请求无限挂起。
LOCK_WAIT_SECONDS = 20.0
#: 数据库租约有效期（秒）。进程崩溃/连接中断后，过期租约可被抢占。
LEASE_TTL_SECONDS = 300
#: ``_LOCKS`` 规模上限。超限即触发一次「未持有且无等待者」条目的清理，
#: 避免长期运行进程里 per-(loop, project) 键无界增长（R247 · A6）。
_LOCKS_MAX = 512

_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
#: 每个 key 上「正要 / 正在 acquire」的协程数。只有计数归零且锁未持有时才允许
#: 驱逐 —— 否则会把别的协程刚拿到手的 lock 对象从映射里摘掉，破坏互斥。
_LOCKS_WAITERS: dict[tuple[int, str], int] = {}
_LOCKS_GUARD = threading.Lock()


class FactsWriteConflict(RuntimeError):
    """事实写入冲突：同一项目已有其它写入方持有锁。"""

    def __init__(self, project_id: str, holder: str, reason: str = "locked"):
        self.project_id = str(project_id or "")
        self.holder = str(holder or "")
        self.reason = reason
        super().__init__(
            f"项目事实正在被「{self.holder or '其它写入'}」写入，请稍后重试" f"（project_id={self.project_id or '-'}）"
        )


def _lock_for(project_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = (id(loop), str(project_id or ""))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS[key] = lock
        _LOCKS_WAITERS[key] = _LOCKS_WAITERS.get(key, 0) + 1
        return lock


def _release_lock_ref(project_id: str) -> None:
    """配对 :func:`_lock_for` 的取锁引用计数（异常路径也必须调用）。

    ✅ R247（A6 · 锁映射无界增长收口）：``_LOCKS`` 按 ``(id(loop), project_id)``
    累积，旧实现永不清理 ⇒ 长期运行进程访问过多少个项目就沉淀多少个
    ``asyncio.Lock`` 条目（弱内存增长，本项目常驻后端进程）。现取锁时计数 +1、
    用完（含超时 / 异常 / 提前 return）计数 -1；归零且锁未被持有即从映射驱逐。
    计数保证「别的协程已从映射拿到 lock 对象、正要 acquire」时绝不驱逐。
    """
    loop = asyncio.get_running_loop()
    key = (id(loop), str(project_id or ""))
    with _LOCKS_GUARD:
        remaining = _LOCKS_WAITERS.get(key, 0) - 1
        if remaining > 0:
            _LOCKS_WAITERS[key] = remaining
            return
        _LOCKS_WAITERS.pop(key, None)
        lock = _LOCKS.get(key)
        if lock is not None and not lock.locked():
            _LOCKS.pop(key, None)


def _evict_idle_locks_locked() -> int:
    """清理「未被持有且无等待者」的锁条目，返回驱逐数。

    调用方必须已持有 :data:`_LOCKS_GUARD`。规模超 :data:`_LOCKS_MAX` 时触发一次，
    纯防御：正常路径下 ``_release_lock_ref`` 已逐条驱逐，这里只兜住「取了锁却
    从未走到 release」（如进程内 guard 等待超时的降级分支遗留）的沉淀。
    """
    if len(_LOCKS) <= _LOCKS_MAX:
        return 0
    evicted = 0
    for key in [k for k, lk in _LOCKS.items() if not lk.locked() and _LOCKS_WAITERS.get(k, 0) <= 0]:
        _LOCKS.pop(key, None)
        _LOCKS_WAITERS.pop(key, None)
        evicted += 1
    return evicted


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def _acquire_lease(db, project_id: str, token: str, holder: str) -> str:
    """原子抢占数据库租约；成功返回当前持有者描述，失败抛 FactsWriteConflict。"""
    cutoff = (datetime.now() - timedelta(seconds=LEASE_TTL_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
    now = _now()
    # 一条语句完成「空闲→插入 / 过期→抢占 / 未过期→不动」，避免 TOCTOU。
    await db.execute(
        "INSERT INTO facts_write_leases (project_id, token, holder, acquired_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(project_id) DO UPDATE SET "
        "token=excluded.token, holder=excluded.holder, acquired_at=excluded.acquired_at "
        "WHERE facts_write_leases.acquired_at < ?",
        (project_id, token, holder, now, cutoff),
    )
    # 租约必须是**已提交**状态，其它连接才看得见（这是 DB 层互斥生效的前提）。
    await db.commit()
    cur = await db.execute("SELECT token, holder FROM facts_write_leases WHERE project_id=?", (project_id,))
    row = await cur.fetchone()
    if not row or str(row["token"] if isinstance(row, dict) else row[0]) != token:
        current_holder = ""
        if row:
            current_holder = str(row["holder"] if isinstance(row, dict) else row[1])
        raise FactsWriteConflict(project_id, current_holder, "lease_held")
    return holder


async def _release_lease(db, project_id: str, token: str) -> None:
    try:
        await db.execute("DELETE FROM facts_write_leases WHERE project_id=? AND token=?", (project_id, token))
        await db.commit()
    except Exception as e:  # noqa: BLE001 - 释放失败不能掩盖业务结果，租约到点自动失效
        logger.warning("释放事实写租约失败（将依赖 TTL 自动失效）: %s", e)


async def acquire_project_facts_write(db, project_id: str, *, holder: str = "") -> str:
    """获取项目级事实写入权，返回租约 token（配合 ``release_project_facts_write``）。

    Args:
        db: 当前请求/提取链路使用的数据库连接。
        project_id: 项目作用域；空值表示未绑定项目（此时跳过保护，保持旧行为）。
        holder: 便于排查的写入方描述（如 ``POST /global-facts`` / ``ai_extraction``）。

    Raises:
        FactsWriteConflict: 进程内等待超时，或数据库租约已被其它进程持有。
    """
    pid = str(project_id or "")
    if not pid:
        return ""
    with _LOCKS_GUARD:
        _evict_idle_locks_locked()
    lock = _lock_for(pid)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=LOCK_WAIT_SECONDS)
    except asyncio.TimeoutError as e:
        _release_lock_ref(pid)
        raise FactsWriteConflict(pid, holder, "lock_wait_timeout") from e
    token = uuid.uuid4().hex
    try:
        await _acquire_lease(db, pid, token, holder)
    except Exception:
        lock.release()
        _release_lock_ref(pid)
        raise
    return token


async def release_project_facts_write(db, project_id: str, token: str) -> None:
    """释放项目级事实写入权（幂等；token 为空时只释放进程内锁）。"""
    pid = str(project_id or "")
    if not pid:
        return
    if token:
        await _release_lease(db, pid, token)
    lock = _lock_for(pid)
    try:
        if lock.locked():
            try:
                lock.release()
            except RuntimeError:
                # 锁不属于当前任务或已释放 —— 不影响业务结果
                pass
    finally:
        _release_lock_ref(pid)
        with _LOCKS_GUARD:
            _evict_idle_locks_locked()


@asynccontextmanager
async def project_facts_write_guard(db, project_id: str, *, holder: str = "") -> AsyncIterator[str]:
    """``async with`` 形式：进入即抢锁，退出必释放（含异常路径）。"""
    token = await acquire_project_facts_write(db, project_id, holder=holder)
    try:
        yield token
    finally:
        await release_project_facts_write(db, project_id, token)


@asynccontextmanager
async def project_facts_inproc_guard(project_id: str, *, holder: str = "") -> AsyncIterator[bool]:
    """仅抢**进程内**同一把项目锁（不碰 DB 租约、不 commit）。

    ✅ R189（2026-11-08 · T-04 第三条写入源补面）：``global_facts_docs.py`` 的
    ``_mark_project_facts_stale`` 在删除/重解析文档时直接 ``UPDATE global_facts``，
    是事实表的第三条写来源，此前既没走 REST 的 ``_facts_write_guard_dependency``
    也没走 SSE 的 ``acquire_project_facts_write`` —— 与 AI 提取落库并发时构成
    典型 lost update（新落库事实未标 stale，正文继续用过时数据）。

    这里刻意**只复用 ``_lock_for`` 而不申请 DB 租约**：``_acquire_lease`` 内部会
    ``db.commit()``，而 docs 的调用点带着未提交事务进来（R166 注释已明示该
    前提），提前提交会破坏调用方的事务边界。共享同一把进程内锁即可与
    ``acquire_project_facts_write`` 互斥（后者也是先取这把锁）。

    等待超时不阻断业务：yield ``False`` 并留 ERROR 日志，由调用方决定是否降级。
    跨 worker 场景仍依赖 DB 租约（见报告遗留项），当前部署为单 worker。
    """
    pid = str(project_id or "")
    if not pid:
        yield True
        return
    with _LOCKS_GUARD:
        _evict_idle_locks_locked()
    lock = _lock_for(pid)
    acquired = False
    try:
        await asyncio.wait_for(lock.acquire(), timeout=LOCK_WAIT_SECONDS)
        acquired = True
    except asyncio.TimeoutError:
        logger.error(
            "项目事实进程内锁等待超时（holder=%s · project=%s），本次写入可能与 AI 提取并发",
            holder,
            pid,
        )
    try:
        yield acquired
    finally:
        try:
            if acquired and lock.locked():
                lock.release()
        finally:
            _release_lock_ref(pid)
            with _LOCKS_GUARD:
                _evict_idle_locks_locked()
