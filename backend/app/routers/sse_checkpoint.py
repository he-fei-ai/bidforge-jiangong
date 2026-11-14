"""SSE 任务成果 checkpoint 持久化 + DB 瞬态重试。

从 sse_handlers.py 拆出，负责：
- 把目录/正文/事实生成的终态成果写入 task_registry.checkpoint_json
- 断线重挂接时回读 checkpoint
- 对 SQLite "database is locked" 瞬态错误做指数退避重试
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime

from app.db import get_conn, get_read_conn, release_read_conn, safe_rowcount
from app.services.checkpoint_schema import (
    FACTS_CHECKPOINT_FIELDS as SCHEMA_FACTS_CHECKPOINT_FIELDS,
)

logger = logging.getLogger("sse.checkpoint")

#: checkpoint 写库瞬态失败的重试次数 / 退避基数（秒）
CHECKPOINT_WRITE_RETRIES = 2
CHECKPOINT_WRITE_BASE_DELAY = 0.2

#: facts checkpoint 回传字段白名单（与在线 completed 事件消费的键保持一致）。
#: 刻意**不含** groups/facts 明细：成果本身已落 global_facts 表，前端重挂接后
#: 会走 loadFacts() 重新拉取；把明细塞进 checkpoint 只会让 task_registry 行膨胀。
#:
#: ✅ R89（2026-10-17）：改为**由单一事实源派生**，不再手写第二份清单。
#:   `checkpoint_schema.FACTS_CHECKPOINT_FIELDS` 是完整 kind 契约（含 event /
#:   message —— 那两项由调用方在筛选**之外**补入，故不属于本过滤器的范围）；
#:   本常量 = 该契约去掉 event/message 后的内容部分。这样：
#:     · 新增终态字段只需改 schema 一处，写入侧筛选与前端生成的 TS 类型同步生效；
#:     · `sh._CHECKPOINT_KINDS["facts_generation"][1]` 与
#:       `cs.CHECKPOINT_KINDS["facts_generation"][1]` 恒等（由 sse_handlers 拼装）。
#:   旧实现在两处各写一份且**无任何 parity 断言**，分叉时测试不会红。
_FACTS_CHECKPOINT_FIELDS = tuple(f for f in SCHEMA_FACTS_CHECKPOINT_FIELDS if f not in ("event", "message"))


async def _retry_db_locked(factory, max_retries: int = 3, base_delay: float = 0.2):
    """对「database is locked / disk I/O error」做有限重试，其余异常原样抛出。

    背景（2026-09-23）：SQLite 在并发写 + busy_timeout 耗尽时会抛
    OperationalError("database is locked")。这类错误是**瞬态**的，退避后
    重试通常即成功；而把它与「no such table」这类确定性错误混在一起
    直接抛出，会让并发写场景出现难以复现的随机失败。

    只重试两类**瞬态**错误：
      · "database is locked"
      · "disk I/O error"
    其余 sqlite3.OperationalError（如 no such table）与非 sqlite3 异常
    一律原样抛出 —— 重试它们只是浪费时间并掩盖真实缺陷。

    退避为指数递增（base_delay × 2^尝试次数），总尝试次数 = 1 + max_retries。
    max_retries=0 即不重试。
    """
    attempts = max(0, int(max_retries))
    last_exc: Exception | None = None
    for i in range(attempts + 1):
        try:
            return await factory()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            transient = "database is locked" in msg or "disk i/o error" in msg
            if not transient:
                raise
            last_exc = e
            if i >= attempts:
                break
            await asyncio.sleep(base_delay * (2**i))
    if last_exc is not None:
        raise last_exc
    # ✅ 2026-10-12（技术债 P2-13 · 删除不可达的死代码）：
    #    此处原有一行 ``return await factory()``。它**永远不可达** ——
    #    ``attempts = max(0, int(max_retries))`` ⇒ 循环至少执行 1 次，每次迭代
    #    要么 return（成功）、要么 raise（非瞬态错误）、要么 break（重试耗尽且
    #    last_exc 已置位），退出循环后 last_exc 必不为 None，上面已经 raise。
    #    危害不在运行期而在**可读性**：读到这行的人会以为「重试耗尽后还会再试
    #    一次」，从而把真实总尝试次数误算成 1+max_retries+1，与 docstring 的
    #    「1 + max_retries」相矛盾 —— 属于本仓反复记录的「注释宣称一套、
    #    实现看起来是另一套」。删掉后 docstring 与实际实现一致。
    raise AssertionError(  # pragma: no cover - 结构性不可达，仅防未来改坏循环
        "_retry_db_locked 退出循环时 last_exc 必不为 None"
    )


async def _write_task_checkpoint_db(
    task_id: str, kind: str, payload: dict, *, skip_terminal: bool = False
) -> None:
    """把 checkpoint 真正写入 task_registry.checkpoint_json。

    独立成函数是为了让 `_save_task_checkpoint` 能整体重试（单测可 monkeypatch
    本函数注入"首次 locked、次次成功"来验证重试链路）。

    ✅ R231（2026-11-13 · P2-1）：``skip_terminal=True`` 的写（partial 成果专用）
    带终态守卫——``AND status NOT IN ('completed','degraded')``，rowcount=0
    即放弃。R226 DAT-6′ 守卫（读 DB 行状态后早退）与本次写之间隔着
    ``finish_task`` 的 shield 内层落库（R177）：读时还是 running、写时
    completed 已落库的窄竞态窗，partial UPDATE（无条件 WHERE）会覆盖
    completed 成果。守卫下沉到写库层获得单语句原子性，彻底关窗。
    """
    conn = await get_conn()
    if skip_terminal:
        cur = await conn.execute(
            "UPDATE task_registry SET checkpoint_json=?, updated_at=? "
            "WHERE id=? AND status NOT IN ('completed','degraded')",
            (json.dumps({"kind": kind, **(payload or {})}, ensure_ascii=False), datetime.now().isoformat(), task_id),
        )
        await conn.commit()
        # ✅ R232（2026-11-13）：R13 判据单一出口——裸 ``.rowcount`` 回归归零，
        #    改走 ``safe_rowcount``（cursor 为 None 时兜 0 并告警，语义同向：
        #    None 也进「放弃写」分支，fail-closed 方向不变）。
        if safe_rowcount(cur, what="partial checkpoint 终态守卫写入") == 0:
            logger.info(
                "迟到 partial checkpoint 被放弃（task=%s · kind=%s · 任务已进入终态，保留成果）",
                task_id, kind,
            )
        return
    await conn.execute(
        "UPDATE task_registry SET checkpoint_json=?, updated_at=? WHERE id=?",
        (json.dumps({"kind": kind, **(payload or {})}, ensure_ascii=False), datetime.now().isoformat(), task_id),
    )
    await conn.commit()


async def _save_task_checkpoint(task_id: str, kind: str, payload: dict, *, skip_terminal: bool = False):
    """把任务终态成果写入 task_registry.checkpoint_json（kind 区分任务类型）。

    payload 统一带 `event` 字段（completed/stopped/error），便于前端判断来源。

    ✅ 瞬态写失败自动重试（2026-09-23）：checkpoint 是断线重挂接的**唯一**
    数据源，一次 "database is locked" 就让用户刷新后拿不到任何成果。
    通过 _retry_db_locked 对 locked / disk I/O 退避重试（确定性错误不重试）。

    ✅ 通过 sse_handlers 命名空间调用 _write_task_checkpoint_db（延迟 import 避免
    循环依赖），这样测试 monkeypatch ``sse_handlers._write_task_checkpoint_db``
    时本函数会拿到 mock 版本，保持与拆分前的测试兼容。
    ⚠️ skip_terminal 仅在 True 时透传（False 不带形参）——3 参 mock
    （test_content_deep_audit 的 flaky 注入）保持逐字兼容。
    """
    from app.routers import sse_handlers  # 延迟 import：避免循环依赖

    _extra = {"skip_terminal": True} if skip_terminal else {}
    await _retry_db_locked(
        lambda: sse_handlers._write_task_checkpoint_db(task_id, kind, payload, **_extra),
        max_retries=CHECKPOINT_WRITE_RETRIES,
        base_delay=CHECKPOINT_WRITE_BASE_DELAY,
    )


async def _save_outline_checkpoint(task_id: str, payload: dict, *, skip_terminal: bool = False):
    """把目录生成终态成果（outline/review/failed_chapters）写入 checkpoint_json。"""
    await _save_task_checkpoint(task_id, "outline_result", payload, skip_terminal=skip_terminal)


async def _save_content_checkpoint(task_id: str, payload: dict):
    """把正文生成成果清单（done/failed_sections/word_count）写入 checkpoint_json。

    ✅ 增强（2026-09-16）：正文内容本身已逐章落库（sections.content），
    但「本次任务生成了哪几章、哪几章失败、为什么失败」只在 SSE 事件里出现过。
    """
    await _save_task_checkpoint(task_id, "content_result", payload)


def _facts_checkpoint_payload(frontend_data: dict) -> dict:
    """从 format_for_frontend 载荷中筛出可安全落 checkpoint 的字段。"""
    if not isinstance(frontend_data, dict):
        return {}
    return {k: v for k, v in frontend_data.items() if k in _FACTS_CHECKPOINT_FIELDS}


async def _save_facts_checkpoint(task_id: str, payload: dict):
    """把全局事实提取终态成果写入 checkpoint_json。"""
    await _save_task_checkpoint(task_id, "facts_result", payload)


async def _load_task_checkpoint(task_id: str, kind: str) -> dict | None:
    """读取指定类型的任务成果 checkpoint（不匹配的 kind 返回 None）。"""
    conn = await get_read_conn()
    try:
        cur = await conn.execute("SELECT checkpoint_json FROM task_registry WHERE id=?", (task_id,))
        row = await cur.fetchone()
    finally:
        await release_read_conn(conn)
    if not row or not row[0]:
        return None
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        # ✅ R192（FIX-R11）：损坏静默吞（DB/WAL 损坏或手改库）会让「白跑几分钟后
        #    无成果」不可归因。留 WARNING 点名（载荷前 80 字符），不改既有返回语义。
        logger.warning(
            "任务 %s 的 checkpoint_json 解析失败（kind=%s，长度=%d），按无成果处理: %.80s",
            task_id,
            kind,
            len(row[0]),
            str(row[0]),
        )
        return None
    if isinstance(data, dict) and data.get("kind") == kind:
        return data
    return None


async def _load_task_db_status(task_id: str) -> str | None:
    """读取任务 DB 行的当前 status；无行返回 None。

    ✅ R226（DAT-6′ · R223 遗留 #1）：供「迟到收尾不覆写终态」守卫使用——
    任务已进入 completed/degraded 终态（成果已落 checkpoint）时，迟到停止
    /断线兜底不得再用 partial 覆盖。与 task_status 的 DB 分支（断线重挂）
    同源读取，避免内存态（_tasks）在进程重启/跨进程时失真。
    """
    conn = await get_read_conn()
    try:
        cur = await conn.execute("SELECT status FROM task_registry WHERE id=?", (task_id,))
        row = await cur.fetchone()
    finally:
        await release_read_conn(conn)
    return row[0] if row else None


async def _checkpoint_partial_outline(
    task_id: str,
    outline,
    failed_chapters,
    *,
    event: str = "stopped",
    input_fingerprint: str = "",
    resumed_chapters: int = 0,
    level1_truncated: int = 0,
    level1_total: int = 0,
    terminal_ckpt_marked: bool = False,
) -> bool:
    """落库「部分成果」checkpoint（客户端断开/用户停止时的兜底）。

    返回是否成功落库（失败静默，绝不影响收尾流程）。

    ✅ R210（L1-7）：``input_fingerprint`` = 本次目录生成的**输入指纹**
    （见 ``sse_handlers._outline_input_fingerprint``）—— 断点续跑时据此判断
    「上次成果是否已过时」；指纹不一致 ⇒ 不复用旧章（防陈旧复用）。
    该键**不进前端白名单**（内部判据，不外泄）。
    """
    outline = outline or []
    if not outline and not failed_chapters:
        return False
    # ✅ R231（2026-11-13 · P2-1）：completed checkpoint 已成功落库后（生成器在
    #    holder 上打 terminal_ckpt 标记），迟到断线兜底不得用 partial 覆盖 ——
    #    与下方写层 skip_terminal 守卫（DB 行状态）双层：标记覆盖「finish_task
    #    尚未把终态提交」的窄窗，写层守卫覆盖其余全部窗口。
    if terminal_ckpt_marked:
        logger.info(
            "断线兜底 partial checkpoint 被放弃（task=%s · completed 成果已落库，保留）",
            task_id,
        )
        return False
    payload = {"event": event, "task_id": task_id, "outline": outline}
    if input_fingerprint:
        payload["input_fingerprint"] = str(input_fingerprint)
    # ✅ R213（FIX-5）：断线重挂必须与在线终态同构 —— 复用章数 / 一级章截断
    #    信号此前只进 completed 的 SSE 载荷，checkpoint 通道全丢（用户把被
    #    截断的目录当完整成果保存）。加法式，未发生时不出现该键。
    if resumed_chapters:
        payload["resumed_chapters"] = int(resumed_chapters)
    if level1_truncated:
        payload["level1_truncated"] = int(level1_truncated)
        payload["level1_total"] = int(level1_total)
    # ✅ R197（B10，R89 S3 同口径）：本函数只服务「部分成果」场景（断线兜底 /
    #    用户停止），必须带 ``partial: True`` —— 缺省时前端 buildPartialOutlineHint
    #    收到 partial=undefined，把半份目录当成完整目录（sse_handlers 在线三处
    #    部分成果出口均已带，唯独本兜底路径漏）。白名单 checkpoint_schema 已登记。
    payload["partial"] = True
    if failed_chapters:
        payload["failed_chapters"] = list(failed_chapters)
        payload["failed_count"] = len(failed_chapters)
    try:
        await _save_outline_checkpoint(task_id, payload, skip_terminal=True)
        return True
    except Exception:
        logger.warning("检查点写入失败（task=%s · 目录生成 · 部分成果）", task_id, exc_info=True)
        return False


async def _load_outline_checkpoint(task_id: str) -> dict | None:
    """读取目录生成成果 checkpoint（薄封装，语义与 kind 校验见 _load_task_checkpoint）。"""
    return await _load_task_checkpoint(task_id, "outline_result")


#: 可作为「断点续跑」来源的目录生成任务终态。
#:
#: ✅ R209（2026-11-10 · P1-1）：**只有被中断的任务**才允许被复用 ——
#:   stopped（用户停止 / 客户端断开兜底）、failed（异常终止 / 全章失败）。
#:   completed（正常跑完）**刻意不在此列**：用户显式「重新生成目录」时必须全量重算，
#:   否则第二次点击会静默复用上一次的成果（用户感知 = 「AI 一次没调，目录却没变」）。
_RESUMABLE_TASK_STATUSES = ("stopped", "failed")

#: 单次续跑取数扫描的最大候选任务行数（防「历史任务过多」时的全表扫描）。
_RESUMABLE_SCAN_LIMIT = 10


async def _load_resumable_outline_checkpoint(scheme_id: str, *, exclude_task_id: str = "") -> dict | None:
    """取同方案**最近一次被中断**的目录生成成果（断点续跑取数的唯一入口）。

    ✅ R209（2026-11-10 · P1-1 · 断点续跑从未生效）：
      R203b #21 的续跑逻辑读的是**本次** ``task_id`` 的 checkpoint，而
      ``task_registry.register_task`` 每次请求都生成**新 uuid** ⇒ 上次的成果落在
      **上次的任务行**上，本次读到恒为空 ⇒ ``_resumed_indices`` 恒空 ⇒
      「断线后重新生成复用已有章节」这一验收锚（R203b #21 效果第 6 条：日志出现
      「断点续跑」）在生产中**零命中**，长方案每次重跑都要重烧全部配额。

    本函数按 ``scheme_id`` 找**最近一次被中断**（stopped/failed）的目录生成任务，
    取其 checkpoint 里的部分成果。completed 任务**不参与**（见
    :data:`_RESUMABLE_TASK_STATUSES` 的说明）—— 保持「正常跑完后重新生成 = 全量
    重算」的既有行为不变。

    Args:
        scheme_id: 方案 id。
        exclude_task_id: 需要排除的任务 id（通常是本次任务自身，避免自引用）。

    Returns:
        目录成果 checkpoint 字典（kind=outline_result，且 outline 非空）；
        没有可用来源时返回 ``None``。
    """
    if not scheme_id:
        return None
    conn = await get_read_conn()
    try:
        # ✅ R218（2026-11-11 · P3 · O-2）：SQL 硬编码可复用终态字面量（原 status IN 字面量）
        #    与 `_RESUMABLE_TASK_STATUSES` 同义但**两份来源** —— 若将来终态集合
        #    演进（如新增可复用状态），只改常量则本 SQL 漏接（续跑取数与复用判据
        #    漂移）。现参数化引用常量（单一事实源），状态集合变时自动同步。
        _status_ph = ",".join("?" for _ in _RESUMABLE_TASK_STATUSES)
        cur = await conn.execute(
            "SELECT checkpoint_json FROM task_registry "
            "WHERE scheme_id=? AND task_type='outline_generation' "
            f"AND status IN ({_status_ph}) AND id<>? "
            "ORDER BY updated_at DESC, id DESC LIMIT ?",
            (scheme_id, *_RESUMABLE_TASK_STATUSES, exclude_task_id or "", _RESUMABLE_SCAN_LIMIT),
        )
        rows = await cur.fetchall() if cur is not None else []
    finally:
        await release_read_conn(conn)
    for row in rows or []:
        raw = row[0] if row else None
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            # 同 _load_task_checkpoint：损坏留痕（载荷前 80 字符），按「无成果」跳过。
            logger.warning(
                "任务 checkpoint_json 解析失败（续跑取数跳过 · scheme=%s，长度=%d）: %.80s",
                scheme_id,
                len(str(raw)),
                str(raw),
            )
            continue
        if not isinstance(data, dict) or data.get("kind") != "outline_result":
            continue
        # ✅ R231（2026-11-13 · 疑点2 防御）：status=stopped 行 + event=completed/
        #    degraded 完整成果 = 「停止指令直写 DB 终态后生成器仍以 completed
        #    收尾」的三方矛盾组合（P2-2 窗口产物）。completed/degraded 不参与
        #    续跑是 R209 明文意图（「跑完后重新生成」必须全量重算）——该组合行
        #    按事件语义跳过，防止下次生成零 AI 调用静默复用完整旧目录。
        if data.get("event") in ("completed", "degraded"):
            logger.info(
                "续跑取数跳过完整成果行（task 行 status=stopped 但 checkpoint event=%s，"
                "按 R209 语义全量重算）",
                data.get("event"),
            )
            continue
        outline = data.get("outline")
        if not isinstance(outline, list) or not outline:
            continue
        return data
    return None
