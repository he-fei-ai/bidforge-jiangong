"""系统活动聚合端点：供前端「后台任务运行状态栏」轮询 + SSE 实时推送。

把三类分散的运行态一次取齐，避免状态栏并发打多个接口：
1. 任务运行态：task_registry 表 + task_registry 内存实时态（进度/耗时/运行统计）；
2. AI 调用态：provider_factory 内存实时计数（进行中/最近一次）+ 今日审计汇总；
3. 服务态：版本 / 进程运行时长。

只读接口，异常时返回空态而不是 500（状态栏是常驻 UI，不应反复报错弹窗）。
SSE 流在任务/AI 状态变化时由 activity_broadcaster 触发刷新，并叠加 10s 心跳保活。
"""

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.config import APP_VERSION, settings
from app.db import get_read_conn, release_read_conn
from app.routers import global_facts as _gf  # 上传配额运行时权威（R73 续作 · G5 收敛）
from app.services import activity_broadcaster as _ab
from app.services.ai import task_registry as _tr
from app.services.ai.provider_factory import get_ai_live_stats

logger = logging.getLogger("system")

router = APIRouter(prefix="/api/v1/system", tags=["system"])

# 进程启动时间（uptime 用；模块加载即应用导入期，误差可忽略）
_STARTED_AT = time.time()

# 「今日 00:00:00 本地时间」前缀缓存：审计查询的 SARGable 时间下界。
# SQLite 的 created_at 由 datetime('now','localtime') 写入，格式恒为
# 'YYYY-MM-DD HH:MM:SS'，字符串比较与时间比较结果一致；因此用
# created_at >= 本前缀 即可精确等价于 date(created_at) = 今天。
# 用 time.monotonic() 而非 wall clock 判断过期（避免系统时间被改导致不刷新）。
_today_prefix_cache: tuple[float, str] | None = None

# 「运行中任务」查询的独立上限（2026-09-30 新增）。
# 与 limit（历史任务条数）**刻意解耦**：不变量「只要存在运行中任务，
# running 必非空」不应受展示条数影响。实测并发任务上限远小于此值，
# 故这是防御性上限而非真实约束（AI 并发硬上限为 5，见 AGENTS.md §4.1）。
_RUNNING_MAX = 50


def _today_local_prefix() -> str:
    """返回今日本地时间零点前缀（1 天 TTL 缓存）。"""
    global _today_prefix_cache
    if _today_prefix_cache is not None:
        mono, val = _today_prefix_cache
        if time.monotonic() - mono < 86400:
            return val
    val = time.strftime("%Y-%m-%d") + " 00:00:00"
    _today_prefix_cache = (time.monotonic(), val)
    return val


def _today_prefix_cache_reset() -> None:
    """测试用：清空今日前缀缓存（供用例验证缓存与失效行为）。"""
    global _today_prefix_cache
    _today_prefix_cache = None


def reset_today_prefix_cache() -> None:
    """对外暴露的缓存重置入口（测试用）。"""
    _today_prefix_cache_reset()


async def _build_activity_snapshot(limit: int = 8) -> dict:
    """构建系统活动聚合快照。被轮询端点与 SSE 端点共享，保证两份数据一致。"""
    limit = max(1, min(int(limit or 8), 30))

    # ---- 1. 任务列表（DB 历史 + 内存实时态合并）----
    # ✅ R176 B2-4（2026-11-04）：部分来源读取失败时，快照仍是 200 合法空态，
    #    但必须**显式登记**哪些来源降级了（否则前端只能看到全 0，误判为"系统空闲"）。
    #    键始终存在；全部成功时为空列表。
    partial_sources: list[str] = []
    tasks: list[dict] = []
    rows: list[dict] = []
    running_rows: list[dict] = []
    conn = None
    try:
        conn = await get_read_conn()
        cur = await conn.execute(
            "SELECT t.id, t.task_type, t.status, t.progress, t.message,"
            " t.scheme_id, t.created_at, t.updated_at, s.name AS scheme_name"
            " FROM task_registry t LEFT JOIN schemes s ON s.id = t.scheme_id"
            " ORDER BY t.created_at DESC LIMIT ?",
            (limit,),
        )
        rows = [dict(r) for r in await cur.fetchall()]

        # ✅ P0 BUG 修复（2026-09-30 · 「先截断后过滤」→ AI 在跑却显示"后台空闲"）：
        #   旧实现把 ORDER BY created_at DESC LIMIT 与"挑出 running/paused"写成
        #   两个独立步骤（:76 截断 → :109 过滤）。`created_at DESC` 把**最新创建**
        #   的任务排在最前，而**正在运行的长任务恰恰是最早创建的那批**（正文生成
        #   十几分钟）。用户在此期间每完成一章 / 切一次 Tab / 重跑一次其它任务，
        #   都会插入更"新"的行 → 长任务被挤出 limit → running 变成空列表。
        #   后果是**自相矛盾**的用户可见故障：同一份快照里 ai.in_flight 来自
        #   get_ai_live_stats()（全局内存态，不受 LIMIT 影响）> 0，前端却按
        #   running.length === 0 渲染「**后台空闲**」。
        #   正确不变量：**「只要存在运行中任务，running 必非空」——与 limit 无关**。
        #   limit 只应约束"历史终态任务"展示多少条。
        #   修法：running 独立成一条**不受 limit 约束**的查询（上限 _RUNNING_MAX），
        #   与 recent 按 id 去重合并。对既有场景逐字节一致（running 数本就 < limit），
        #   仅在"长任务被挤出"时修正。与 recent 共用同一连接，避免重复借还。
        cur = await conn.execute(
            "SELECT t.id, t.task_type, t.status, t.progress, t.message,"
            " t.scheme_id, t.created_at, t.updated_at, s.name AS scheme_name"
            " FROM task_registry t LEFT JOIN schemes s ON s.id = t.scheme_id"
            " WHERE t.status IN ('running','paused')"
            " ORDER BY t.created_at DESC LIMIT ?",
            (_RUNNING_MAX,),
        )
        running_rows = [dict(r) for r in await cur.fetchall()]
    except Exception as e:
        # fail-soft：任一查询失败都不阻断状态栏（只读常驻 UI，不应弹错）
        logger.warning("activity: 读取任务列表失败: %s", e)
        partial_sources.append("tasks")
        # 退回旧行为：从已取到的 recent 里挑运行中任务
        if not running_rows:
            running_rows = [r for r in rows if r.get("status") in ("running", "paused")]
    finally:
        if conn is not None:
            await release_read_conn(conn)

    # 合并：running 在前（与 recent 按 id 去重），再补 recent 其余条目
    # ⚠️ 合并**只在这一处**做。修复前下方还有一个 `for t in rows: ... tasks.append(t)`
    # 循环，若两处并存会让 recent 里的任务重复出现（前端列表出现重复行）。
    seen: set[str] = set()
    for r in running_rows + rows:
        rid = r.get("id")
        if rid and rid in seen:
            continue
        if rid:
            seen.add(rid)
        tasks.append(r)

    for t in tasks:
        st = _tr._tasks.get(t["id"])
        if st:
            # 内存态永远比 DB 新（进度落库有节流）
            t["status"] = st.get("status", t["status"])
            t["progress"] = st.get("progress", t["progress"])
            t["message"] = st.get("message") or t["message"] or ""
            t["live"] = True
            started = st.get("started_at")
            if started:
                # ✅ 扣除暂停时长（2026-09-17）：协作式暂停不打断已在飞的 AI 调用，
                #    「已耗时冻结」是用户判断暂停是否生效的唯一证据。旧实现直接
                #    now - started，暂停期间耗时继续增长，看起来像"暂停没生效"。
                elapsed_ms = _tr.get_task_elapsed_ms(t["id"])
                t["elapsed"] = int(max(0.0, (elapsed_ms or 0.0)) / 1000)
            # 运行统计（已耗时/ETA/字数等，由 update_task_stats 广播的内存态）
            t["stats"] = dict(st.get("stats") or {})
        else:
            t["live"] = False
            t.setdefault("message", "")
            t["stats"] = {}

    running = [t for t in tasks if t.get("status") in ("running", "paused")]
    # recent 只回最近 limit 条（running 可能额外多出若干条，这是修复的预期差异：
    # 之前它们是被静默丢弃的，现在必须出现，否则界面会说"后台空闲"）
    recent = tasks[:limit]

    # ---- 2. 今日 AI 调用汇总（审计日志攒批落库最长延迟 ~10s）----
    ai_today: dict = {}
    ai_error_classes: dict = {}
    conn = None
    try:
        conn = await get_read_conn()
        # ✅ 性能优化（2026-09-24 · 基线优化 P0-1）：原写法
        #   `WHERE date(created_at) = date('now','localtime')`
        # 对索引列施加函数 → `idx_audit_logs_created` **无法使用**，每次都必须
        # SCAN 全表（基线实测 13334 行 / 2.885ms，p95 3.2ms）。本端点由前端
        # 状态栏每 3s 轮询一次（TaskStatusBar POLL_MS），加上 SSE 流每次
        # 活动变更都会重建快照，是全站最高频的热点路径。
        # 改写为 `created_at >= <今日 00:00:00 local>`：
        #   1) 语义等价（created_at 由 `datetime('now','localtime')` 写入，
        #      字符串 ISO 排序与本地时间比较结果一致）；
        #   2) 可走 idx_audit_logs_created 索引，只扫「今天」的行；
        #   3) 参数化避免每次拼接 SQL 文本。
        # 基线实测：2.885ms → 0.114ms（↓96%），p95 3.203ms → 0.131ms。
        cur = await conn.execute(
            # ✅ 2026-11-05（统计口径修复）：calls_today 旧口径是 COUNT(*)，把
            #    circuit_skipped / provider_disabled 等「未真实发请求」的行也算进
            #    「今日调用」；与 summary 的 action='chat' 口径不一致，面板与
            #    ai_audit_logs 对不上。现统一为 action='chat' 的**逻辑调用**行
            #    （方案 B 合并后，每次业务调用恰好一行；尝试次数见 attempts_today）
            "SELECT COALESCE(SUM(CASE WHEN action='chat' THEN 1 ELSE 0 END), 0)"
            " AS calls_today,"
            " COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens_today,"
            # ✅ 2026-11-05（口径修复）：平均耗时只取**成功真实调用**，
            #    与 AIConfigPage 统计口径一致；失败尝试（大量 0.18s 快速失败）
            #    不再拉低均值。全量均值另给 avg_duration_all 便于排查。
            " COALESCE(AVG(CASE WHEN action='chat' AND success=1 THEN duration END), 0)"
            " AS avg_duration,"
            " COALESCE(AVG(CASE WHEN action='chat' THEN duration END), 0)"
            " AS avg_duration_all,"
            " COALESCE(SUM(CASE WHEN action='chat' AND success=1 THEN 1 ELSE 0 END), 0)"
            " AS ok_calls,"
            # ✅ 2026-11-04（口径修复）：成功率分母只算真实调用，跳过行单列。
            " COALESCE(SUM(CASE WHEN action='chat' THEN 1 ELSE 0 END), 0) AS real_calls_today,"
            " COALESCE(SUM(CASE WHEN action='chat' AND success=0 THEN 1 ELSE 0 END), 0)"
            " AS failed_today,"
            " COALESCE(SUM(CASE WHEN action='circuit_skipped' THEN 1 ELSE 0 END), 0)"
            " AS skipped_today,"
            # ✅ 2026-11-05（方案 B · 审计合并）：一次逻辑调用只落一条主记录，
            #    attempt_count 记录其真实尝试次数（含重试/降级/对冲）。
            #    旧行无该列时由迁移补 1，历史口径不变。
            " COALESCE(SUM(CASE WHEN action='chat' THEN COALESCE(attempt_count, 1) ELSE 0 END), 0)"
            " AS attempts_today,"
            " MAX(created_at) AS last_call_at"
            " FROM ai_audit_logs WHERE created_at >= ?",
            (_today_local_prefix(),),
        )
        row = await cur.fetchone()
        if row:
            ai_today = dict(row)
        # 今日失败按 error_class 聚合（历史 NULL/'' 归入 unknown，不报错）
        cur2 = await conn.execute(
            "SELECT COALESCE(NULLIF(error_class, ''), 'unknown') AS cls,"
            " COUNT(*) AS n FROM ai_audit_logs"
            " WHERE created_at >= ? AND action='chat' AND success=0"
            " GROUP BY cls ORDER BY n DESC",
            (_today_local_prefix(),),
        )
        ai_error_classes = {str(r["cls"]): int(r["n"] or 0) for r in await cur2.fetchall()}
    except Exception as e:
        logger.warning("activity: 读取 AI 审计汇总失败: %s", e)
        partial_sources.append("ai_today")
    finally:
        if conn is not None:
            await release_read_conn(conn)

    # 实时态（进行中调用数 / 会话累计成败 / 最近一次调用）优先级更高
    ai = {
        # ✅ 2026-11-05（方案 B）：calls_today / real_calls_today 是**逻辑调用**
        #    行数（一次业务调用含重试/降级/对冲只落一行）；attempts_today 是
        #    其真实尝试次数之和（SUM(attempt_count)），二者与 ai_audit_logs
        #    完全可对账。
        "calls_today": int(ai_today.get("calls_today") or 0),
        "real_calls_today": int(ai_today.get("real_calls_today") or 0),
        "attempts_today": int(ai_today.get("attempts_today") or 0),
        "failed_today": int(ai_today.get("failed_today") or 0),
        # 尝试级失败 = 尝试数 - 成功次数（每次成功逻辑调用恰好 1 次成功尝试；
        # 旧行 attempt_count=1 时退化为原「失败尝试数」口径）。
        "attempt_failed_today": max(0, int(ai_today.get("attempts_today") or 0) - int(ai_today.get("ok_calls") or 0)),
        "skipped_today": int(ai_today.get("skipped_today") or 0),
        "tokens_today": int(ai_today.get("tokens_today") or 0),
        "avg_duration": round(float(ai_today.get("avg_duration") or 0), 2),
        "avg_duration_all": round(float(ai_today.get("avg_duration_all") or 0), 2),
        "ok_calls": int(ai_today.get("ok_calls") or 0),
        "error_classes_today": ai_error_classes,
        "last_call_at": ai_today.get("last_call_at") or "",
        # 实时态里的 total/failed 是**尝试**口径，calls/call_failed 是**逻辑调用**口径
        **get_ai_live_stats(),
    }
    ok = ai.get("ok_calls") or 0
    # ✅ 2026-11-04（口径修复）：分母改用 action='chat' 行数，跳过行不进分母。
    # ✅ 2026-11-05（方案 B）：这些行已合并为逻辑调用 → 现在是**逻辑成功率**。
    real_calls = ai.get("real_calls_today") or 0
    ai["success_rate"] = round(ok / real_calls * 100, 1) if real_calls else None
    attempts = ai.get("attempts_today") or 0
    ai["attempt_success_rate"] = round(ok / attempts * 100, 1) if attempts else None
    logical_calls = int(ai.get("calls") or 0)
    logical_failed = int(ai.get("call_failed") or 0)
    ai["logical_success_rate"] = (
        round((logical_calls - logical_failed) / logical_calls * 100, 1) if logical_calls else None
    )

    # ---- 3. 文档解析统计（监控缺口补齐 2026-10-06）：此前解析失败率/积压量
    #        在活动快照里完全不可见，用户批量上传后只能逐行翻文档列表。
    #        fail-soft：查询失败不阻断常驻状态栏，缺省给零值 ----
    docs_stats: dict = {}
    conn = None
    try:
        conn = await get_read_conn()
        cur = await conn.execute(
            "SELECT COUNT(*) AS total,"
            " COALESCE(SUM(CASE WHEN parse_status='success' THEN 1 ELSE 0 END), 0) AS parsed,"
            " COALESCE(SUM(CASE WHEN parse_status='failed' THEN 1 ELSE 0 END), 0) AS failed,"
            " COALESCE(SUM(CASE WHEN parse_status='pending' THEN 1 ELSE 0 END), 0) AS pending,"
            " MAX(created_at) AS last_upload_at"
            " FROM project_documents"
        )
        row = await cur.fetchone()
        if row:
            docs_stats = dict(row)
    except Exception as e:
        logger.warning("activity: 读取文档解析统计失败: %s", e)
        partial_sources.append("documents")
    finally:
        if conn is not None:
            await release_read_conn(conn)

    total_docs = int(docs_stats.get("total") or 0)
    failed_docs = int(docs_stats.get("failed") or 0)
    documents = {
        "total": total_docs,
        "parsed": int(docs_stats.get("parsed") or 0),
        "failed": failed_docs,
        "pending": int(docs_stats.get("pending") or 0),
        "last_upload_at": docs_stats.get("last_upload_at") or "",
        # 与 ai.success_rate 同口径的服务端派生指标；无文档时为 None（不误报 0）
        "failure_rate": round(failed_docs / total_docs * 100, 1) if total_docs else None,
    }

    return {
        "server": {
            "version": APP_VERSION,
            "uptime": int(time.time() - _STARTED_AT),
        },
        "tasks": {"running": running, "recent": recent},
        "ai": ai,
        "documents": documents,
        "partial_sources": partial_sources,
    }


@router.get("/activity")
async def activity(limit: int = 8):
    """后台活动聚合：任务（运行中 + 最近） / AI 调用实时态与今日汇总 / 服务信息。

    前端任务状态栏默认每 3s 轮询本端点；SSE 连接建立后主要依赖 /activity/stream
    推送，轮询降级为断线补偿。limit 控制最近任务条数（1~30）。
    """
    return await _build_activity_snapshot(limit)


@router.get("/activity/stream")
async def activity_stream(request: Request, limit: int = 8):
    """系统活动 SSE 实时流：任务 / AI 状态变化时立即推送快照，10s 无事件则推心跳。

    与 /activity 共享 _build_activity_snapshot，保证数据一致。连接建立时先推
    当前完整快照，随后由 activity_broadcaster 信号触发刷新并合并 10s 心跳。
    """
    limit = max(1, min(int(limit or 8), 30))

    async def _event_stream():
        queue = await _ab.subscribe()
        try:
            # 连接建立立即推一次完整快照
            snapshot = await _build_activity_snapshot(limit)
            yield f"data: {json.dumps({'event': 'snapshot', 'data': snapshot}, ensure_ascii=False)}\n\n"

            while not await request.is_disconnected():
                # 等待变更信号或 10s 心跳超时
                try:
                    await asyncio.wait_for(queue.get(), timeout=10.0)
                except asyncio.TimeoutError:
                    pass

                # 合并极短窗口内可能到达的多个信号，避免连续 DB 查询
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 0.3
                while loop.time() < deadline:
                    try:
                        await asyncio.wait_for(queue.get(), timeout=deadline - loop.time())
                    except asyncio.TimeoutError:
                        break
                    except asyncio.QueueEmpty:
                        break

                if await request.is_disconnected():
                    break

                snapshot = await _build_activity_snapshot(limit)
                yield f"data: {json.dumps({'event': 'snapshot', 'data': snapshot}, ensure_ascii=False)}\n\n"
        finally:
            await _ab.unsubscribe(queue)

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ✅ R73 续作 · G5（2026-10-12）：上传配额「第三份字面量副本」已收敛。
#    此前本模块自带 ``_UPLOAD_LIMIT_DEFAULTS`` + ``_positive_setting`` 读取器，
#    与 ``global_facts.MAX_UPLOAD_*``（上传路径真正消费的运行时权威）各自读
#    settings —— 一旦 monkeypatch ``setattr(gf, "MAX_UPLOAD_BYTES", N)``，上传
#    路径按 N 执行而本端点仍下发旧值，**前端拿到的上限与后端实际执行的上限
#    不一致**（本仓 R73/G2 同族「同一判据两份取值」的第三处残留）。
#    现直接读 ``_gf.MAX_UPLOAD_*``：单一事实源，monkeypatch 对两侧同时生效；
#    非正数配置的回落由 ``global_facts._positive_int_setting`` 统一兜底。


@router.get("/upload-limits")
async def upload_limits():
    """上传配额动态下发：前端启动时读取一次，作为文件大小/数量校验的唯一口径。

    返回：
      - max_upload_bytes：单文件大小上限（字节）
      - max_files_per_request：单次请求文件数上限
      - max_total_bytes：单次请求累计体积上限（字节）

    值取自 ``global_facts.MAX_UPLOAD_*``（运行时权威，由 ``settings.upload_*``
    派生、可被 monkeypatch 覆盖）—— 与上传路径 ``upload_documents`` 消费的是
    **同一对象**，前端拿到的上限与后端实际执行的上限恒一致。
    """
    return {
        "max_upload_bytes": _gf.MAX_UPLOAD_BYTES,
        "max_files_per_request": _gf.MAX_UPLOAD_FILES_PER_REQUEST,
        "max_total_bytes": _gf.MAX_UPLOAD_TOTAL_BYTES,
    }


# --------------------------------------------------------------------------
# 提示词治理运行时开关（R47 债-3 · 2026-10-06）
#
# 能力在库但此前只能通过环境变量开启：
#   - ``prompt_context_budget``（``sse_handlers._apply_prompt_context_budget``，
#     ≤0 关闭；>0 时按预算削减外部资料上下文长度）；
#   - ``prompt_injection_defense``（``sse_handlers._apply_prompt_injection_defense``，
#     False 关闭；True 时对外部资料加「只读数据」围栏并脱敏疑似凭据）。
#
# 本端点只做进程内存态读写（重启即回 .env 默认），不写 DB——这是运行时
# 旋钮，不是持久化配置。默认值逐字沿用 settings 现值（0 / False），不开启
# 时行为与现状完全一致。
# --------------------------------------------------------------------------
from pydantic import BaseModel  # noqa: E402


class GovernanceSettings(BaseModel):
    """``PUT /system/governance`` 请求体（PATCH 语义）。

    ✅ 2026-11-05：字段默认值改为 ``None`` = 「不修改该项」—— 旧实现
    全字段默认值（0 / False）导致「只提交部分字段时把其他开关静默重置」
    （test_governance_partial_put_keeps_other_flags 报障）。
    """

    prompt_context_budget: int | None = None
    prompt_injection_defense: bool | None = None


class RedactionSettings(BaseModel):
    """``PUT /system/governance/redaction`` 请求体：敏感信息脱敏开关。"""

    enabled: bool = False


class OutlineAutoConfirmSettings(BaseModel):
    """``PUT /system/outline-auto-confirm`` 请求体：目录生成自动确认开关。"""

    enabled: bool | None = None


# ✅ R179 N1（2026-11-05）：目录生成自动确认开关（默认关，存 governance_settings kv，
#    零迁移）。**不复用 /governance** —— 该接口的响应键集被既有用例逐字锁定
#    （test_governance_get_returns_two_expected_fields），新增键会破坏既有契约。
_OUTLINE_AUTO_CONFIRM_KEY = "outline_auto_confirm"


@router.get("/outline-auto-confirm")
async def outline_auto_confirm_get():
    """读取自动确认开关（持久化优先；无记录 = 关闭）。

    ``delay_ms`` 为后端有效倒计时（运维经 ``OUTLINE_AUTO_CONFIRM_DELAY_MS`` 配置），
    前端只读展示、不提供修改入口（加法式键，旧前端忽略）。
    """
    from app.config import settings

    kv = await _governance_kv_load()
    return {
        "enabled": kv.get(_OUTLINE_AUTO_CONFIRM_KEY, "false") == "true",
        "delay_ms": int(getattr(settings, "outline_auto_confirm_delay_ms", 8000) or 8000),
    }


@router.put("/outline-auto-confirm")
async def outline_auto_confirm_put(body: OutlineAutoConfirmSettings):
    """写回自动确认开关（PATCH 语义：``None`` = 不改动）。"""
    kv = await _governance_kv_load()
    current = kv.get(_OUTLINE_AUTO_CONFIRM_KEY, "false") == "true"
    enabled = current if body.enabled is None else bool(body.enabled)
    kv[_OUTLINE_AUTO_CONFIRM_KEY] = "true" if enabled else "false"
    await _governance_kv_save(kv)
    logger.info("目录自动确认开关已更新并持久化: outline_auto_confirm=%s", enabled)
    return {"enabled": enabled}


# ✅ 2026-11-05（R175 收口）：治理开关持久化 —— 旧实现只写进程内存态
# （重启即回 .env 默认），用户在界面开的防护开关一重启就静默关闭，
# 属于「以为已防护」的假安全感。现写入 SQLite kv 表，重启后读回；
# 表在首次读写时幂等创建（不依赖 init_db 顺序）。
_GOV_KV_TABLE_SQL = "CREATE TABLE IF NOT EXISTS governance_settings (" "key TEXT PRIMARY KEY, value TEXT NOT NULL)"


async def _governance_kv_load() -> dict[str, str]:
    """读取治理开关 kv（表不存在/库未初始化时返回空 dict，不抛错）。"""
    import app.db as _appdb

    conn = await _appdb.get_conn()
    await conn.execute(_GOV_KV_TABLE_SQL)
    await conn.commit()
    # 注意：本项目对 aiosqlite 打过补丁（_patch_audiosqlite），conn.execute
    # 返回 coroutine（await 后直接得到 cursor），不能用作 async context manager。
    cur = await conn.execute("SELECT key, value FROM governance_settings")
    rows = await cur.fetchall()
    await cur.close()
    return {str(k): str(v) for k, v in rows}


async def _governance_kv_save(kv: dict[str, str]) -> None:
    """写回治理开关 kv（upsert）。"""
    import app.db as _appdb

    conn = await _appdb.get_conn()
    await conn.execute(_GOV_KV_TABLE_SQL)
    await conn.executemany(
        "INSERT INTO governance_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [(k, v) for k, v in kv.items()],
    )
    await conn.commit()


async def governance_get():
    """读取提示词治理开关当前生效值（持久化优先，重启不丢）。

    返回字段：
      - prompt_context_budget: int（≤0 = 关闭上下文预算削减；>0 = 最大字节数）
      - prompt_injection_defense: bool（False = 不对外部资料加围栏/脱敏）
    """
    kv = await _governance_kv_load()
    if "prompt_context_budget" in kv:
        try:
            budget = int(kv["prompt_context_budget"] or 0)
        except ValueError:
            budget = 0
    else:
        budget = int(getattr(settings, "prompt_context_budget", 0) or 0)
    if "prompt_injection_defense" in kv:
        defense = kv["prompt_injection_defense"] == "true"
    else:
        defense = bool(getattr(settings, "prompt_injection_defense", False))
    return {
        "prompt_context_budget": budget,
        "prompt_injection_defense": defense,
    }


@router.put("/governance")
async def governance_put(body: GovernanceSettings):
    """写回提示词治理开关（PATCH 语义 + SQLite 持久化）。

    ``None`` 字段保留现值不重置；返回写回后的现值（与 GET 同构）。
    非法值由 Pydantic 校验拒绝（422）。
    """
    kv = await _governance_kv_load()
    # 现值取序：持久化记录 > 进程内 settings
    if "prompt_context_budget" in kv:
        try:
            cur_budget = int(kv["prompt_context_budget"] or 0)
        except ValueError:
            cur_budget = 0
    else:
        cur_budget = int(getattr(settings, "prompt_context_budget", 0) or 0)
    cur_defense = (
        (kv["prompt_injection_defense"] == "true")
        if "prompt_injection_defense" in kv
        else bool(getattr(settings, "prompt_injection_defense", False))
    )

    new_budget = cur_budget if body.prompt_context_budget is None else int(body.prompt_context_budget)
    new_defense = cur_defense if body.prompt_injection_defense is None else bool(body.prompt_injection_defense)

    settings.prompt_context_budget = new_budget
    settings.prompt_injection_defense = new_defense
    kv["prompt_context_budget"] = str(int(new_budget))
    kv["prompt_injection_defense"] = "true" if new_defense else "false"
    await _governance_kv_save(kv)
    logger.info(
        "提示词治理开关已更新并持久化: context_budget=%d, injection_defense=%s",
        new_budget,
        new_defense,
    )
    return {
        "prompt_context_budget": int(new_budget or 0),
        "prompt_injection_defense": bool(new_defense),
    }


@router.put("/governance/redaction")
async def governance_redaction_put(body: RedactionSettings):
    """写回敏感信息脱敏开关（持久化，重启不丢）。"""
    enabled = bool(body.enabled)
    settings.prompt_redact_sensitive = enabled
    kv = await _governance_kv_load()
    kv["redaction_enabled"] = "true" if enabled else "false"
    await _governance_kv_save(kv)
    logger.info("敏感信息脱敏开关已更新并持久化: redaction_enabled=%s", enabled)
    return {"redaction_enabled": enabled}


@router.get("/governance/posture")
async def governance_posture_get():
    """治理姿态总览：脱敏开关生效值 + 是否已有持久化记录 + 风险提示。"""
    from app.services import prompt_material_guard as _pmg

    kv = await _governance_kv_load()
    persisted = bool(kv)
    if "redaction_enabled" in kv:
        redaction_enabled = kv["redaction_enabled"] == "true"
    else:
        redaction_enabled = _pmg.redaction_enabled()
    return {
        "redaction_enabled": redaction_enabled,
        "persisted": persisted,
        "redaction_notice": ("" if redaction_enabled else _pmg.governance_posture()["redaction_notice"]),
    }
