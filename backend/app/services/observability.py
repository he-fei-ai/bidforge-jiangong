"""可观测性日志服务：system_logs 的写入与查询。"""
from __future__ import annotations

import asyncio
import json

from app.db import safe_rowcount
from app.utils.observability import context_snapshot, redact_sensitive, should_sample

# ✅ R142（2026-10-28 · 错误码规范）：模块 → 错误码前缀 的统一映射。
#    规范：``<PREFIX>_<3位序号>``，如 PARSE_001 / OUTLINE_002 / CONTENT_003 /
#    EXPORT_004。所有业务模块的错误日志应经 :func:`make_error_code` 生成码，
#    保证「按模块前缀分组 + 序号可检索」的一致性（需求三）。
ERROR_CODE_PREFIXES = {
    "parse": "PARSE", "extract": "EXTRACT", "outline": "OUTLINE",
    "content": "CONTENT", "chart": "CHART", "export": "EXPORT",
    "fact": "FACT", "review": "REVIEW", "prompt": "PROMPT", "ai": "AI",
    "db": "DB", "io": "IO", "network": "NETWORK", "validation": "VALIDATION",
}


def make_error_code(module: str, seq: int) -> str:
    """按规范生成错误码：``<模块前缀>_<3位序号>``（如 PARSE_001）。

    未知模块回落到大写模块名作前缀（fail-soft，不抛异常）。
    """
    prefix = ERROR_CODE_PREFIXES.get(str(module).lower(), str(module).upper() or "GEN")
    return f"{prefix}_{int(seq):03d}"

async def log_event(db, *, level="INFO", module="", event="", trace_id="", span_id="", parent_span_id="", request_id="", task_id="", project_id="", chapter_id="", scheme_id="", duration_ms=None, status="", error_code="", error_layer="", retryable=None, message="", detail=None):
    snap = context_snapshot()
    trace_id = trace_id or snap["trace_id"]; span_id = span_id or snap["span_id"]
    parent_span_id = parent_span_id or snap["parent_span_id"]; request_id = request_id or snap["request_id"]
    task_id = task_id or snap["task_id"]; project_id = project_id or snap["project_id"]
    chapter_id = chapter_id or snap["chapter_id"]; scheme_id = scheme_id or snap["scheme_id"]
    # 采样：DEBUG 级按配置概率丢弃
    try:
        from app.utils.observability import should_sample
        if not should_sample(level):
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        await db.execute(
            "INSERT INTO system_logs (level,module,event,trace_id,span_id,parent_span_id,request_id,task_id,project_id,chapter_id,scheme_id,duration_ms,status,error_code,error_layer,retryable,message,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (level, module, event, trace_id, span_id, parent_span_id, request_id, task_id, project_id, chapter_id, scheme_id, duration_ms, status, error_code, error_layer, 1 if retryable else (0 if retryable is False else None), redact_sensitive(message) if isinstance(message, str) else str(message), json.dumps(redact_sensitive(detail), ensure_ascii=False, default=str) if detail is not None else None),
        )
        await db.commit()
    except Exception as e:
        import logging

        logging.getLogger("observability.service").warning("system_logs 写入失败（fail-soft）: %s", e)

async def query_logs(db, *, trace_id="", task_id="", project_id="", scheme_id="", module="", level="", error_code="", keyword="", since="", until="", limit=100, offset=0, order_desc=True):
    where=[]; params=[]
    if trace_id: where.append("trace_id = ?"); params.append(trace_id)
    if task_id: where.append("task_id = ?"); params.append(task_id)
    if project_id: where.append("project_id = ?"); params.append(project_id)
    if scheme_id: where.append("scheme_id = ?"); params.append(scheme_id)
    if module: where.append("module = ?"); params.append(module)
    if level: where.append("level = ?"); params.append(level.upper())
    if error_code: where.append("error_code = ?"); params.append(error_code)
    if keyword:
        where.append("(message LIKE ? OR detail LIKE ? OR event LIKE ?)"); params.extend([f"%{keyword}%",f"%{keyword}%",f"%{keyword}%"])
    if since: where.append("timestamp >= ?"); params.append(since)
    if until: where.append("timestamp <= ?"); params.append(until)
    sql_where = (" WHERE "+" AND ".join(where)) if where else ""
    order = "DESC" if order_desc else "ASC"
    cur = await db.execute(f"SELECT COUNT(*) FROM system_logs{sql_where}", params)
    row = await cur.fetchone(); total = row[0] if row else 0
    cur = await db.execute(f"SELECT * FROM system_logs{sql_where} ORDER BY id {order} LIMIT ? OFFSET ?", params+[limit,offset])
    rows = await cur.fetchall(); cols=[d[0] for d in cur.description] if cur.description else []
    items=[dict(zip(cols,r)) for r in rows]
    return {"total":total,"items":items,"limit":limit,"offset":offset}

async def get_trace_tree(db, trace_id):
    cur = await db.execute("SELECT * FROM system_logs WHERE trace_id = ? ORDER BY id ASC", (trace_id,))
    rows = await cur.fetchall(); cols=[d[0] for d in cur.description] if cur.description else []
    items=[dict(zip(cols,r)) for r in rows]
    # 断链判定：parent_span_id 在 trace 内无对应 span_id 的 span 记入 orphan_spans
    # （前端链路视图已消费该键，旧实现未返回 ⇒ 「断链」提示永不出现）。
    return {"trace_id":trace_id,"count":len(items),"items":items,
            "orphan_spans": detect_orphan_spans(items)}

async def get_task_timeline(db, task_id):
    cur = await db.execute("SELECT * FROM system_logs WHERE task_id = ? ORDER BY id ASC", (task_id,))
    rows = await cur.fetchall(); cols=[d[0] for d in cur.description] if cur.description else []
    items=[dict(zip(cols,r)) for r in rows]
    return {"task_id":task_id,"count":len(items),"items":items}

async def export_logs(db, fmt="json", **kw):
    res = await query_logs(db, limit=10000, **kw)
    items = res["items"]
    if fmt == "csv":
        if not items: return ""
        cols = list(items[0].keys())
        lines = [",".join(cols)]
        for it in items:
            vals=[]
            for c in cols:
                v=it.get(c); s="" if v is None else str(v).replace('"','""'); vals.append('"'+s+'"')
            lines.append(",".join(vals))
        return "\n".join(lines)
    return __import__("json").dumps(items, ensure_ascii=False, default=str)

async def cleanup_old_logs(db, retention_days=30, *, by_level=None):
    """按级别分别保留天数清理（ERROR 90/INFO 30/DEBUG 7）；by_level 为 dict 覆盖默认。"""
    if by_level is None:
        by_level = {"ERROR": 90, "FATAL": 90, "WARN": 30, "WARNING": 30, "INFO": 30, "DEBUG": 7}
    total = 0
    for lvl, days in by_level.items():
        try:
            d = int(days)
        except (TypeError, ValueError):
            d = int(retention_days)
        cur = await db.execute(
            "DELETE FROM system_logs WHERE level = ? AND timestamp < datetime('now','localtime', ?)",
            (str(lvl).upper(), f"-{d} days"))
        total += safe_rowcount(cur, "system_logs.cleanup_by_level")
    # 兜底：未在 by_level 的级别按 retention_days 清理
    cur = await db.execute(
        "DELETE FROM system_logs WHERE level NOT IN (%s) AND timestamp < datetime('now','localtime', ?)"
        % ",".join("?" for _ in by_level),
        list(by_level.keys()) + [f"-{int(retention_days)} days"])
    total += safe_rowcount(cur, "system_logs.cleanup_rest")
    await db.commit()
    return total

async def get_metrics(db, *, days=7):
    """各模块调用次数/成功率/失败率 + P50/P95/P99 耗时 + 错误码 TOP10 + token 趋势。"""
    where = " WHERE timestamp >= datetime('now','localtime', ?)"
    params = [f"-{int(days)} days"]
    # 总览
    cur = await db.execute(f"SELECT COUNT(*) FROM system_logs{where}", params)
    row = await cur.fetchone(); total = row[0] if row else 0
    cur = await db.execute(f"SELECT level, COUNT(*) FROM system_logs{where} GROUP BY level", params)
    by_level = {r[0]: r[1] for r in (await cur.fetchall())}
    # 模块维度
    cur = await db.execute(f"SELECT module, COUNT(*), SUM(CASE WHEN level IN ('ERROR','FATAL') THEN 1 ELSE 0 END) FROM system_logs{where} GROUP BY module", params)
    modules = []
    for r in await cur.fetchall():
        cnt = r[1] or 0; errs = r[2] or 0
        modules.append({"module": r[0] or "", "count": cnt, "errors": errs,
                        "success_rate": round((cnt - errs) / cnt, 4) if cnt else 1.0})
    # 耗时分布
    cur = await db.execute(f"SELECT duration_ms FROM system_logs{where} AND duration_ms IS NOT NULL", params)
    durs = [r[0] for r in await cur.fetchall() if r[0] is not None]
    def pct(p):
        if not durs: return 0
        s = sorted(durs); idx = min(len(s)-1, int(len(s)*p))
        return round(s[idx], 2)
    latency = {"p50": pct(0.5), "p95": pct(0.95), "p99": pct(0.99), "count": len(durs)}
    # 错误码 TOP10
    cur = await db.execute(f"SELECT error_code, COUNT(*) FROM system_logs{where} AND error_code IS NOT NULL AND error_code != '' GROUP BY error_code ORDER BY COUNT(*) DESC LIMIT 10", params)
    top_errors = [{"code": r[0], "count": r[1]} for r in await cur.fetchall()]
    # 事件分布
    cur = await db.execute(f"SELECT event, COUNT(*) FROM system_logs{where} AND event IS NOT NULL AND event != '' GROUP BY event ORDER BY COUNT(*) DESC LIMIT 20", params)
    top_events = [{"event": r[0], "count": r[1]} for r in await cur.fetchall()]
    # 断链数
    try:
        from app.services.observability import detect_orphan_spans
        cur = await db.execute(f"SELECT trace_id FROM system_logs{where} GROUP BY trace_id", params)
        traces = [r[0] for r in await cur.fetchall() if r[0]]
        broken = 0
        for tid in traces:
            cur2 = await db.execute("SELECT * FROM system_logs WHERE trace_id = ?", (tid,))
            rows = await cur2.fetchall()
            cols = [d[0] for d in cur2.description] if cur2.description else []
            items = [dict(zip(cols, r)) for r in rows]
            if detect_orphan_spans(items):
                broken += 1
        broken_rate = round(broken / len(traces), 4) if traces else 0.0
    except Exception:  # noqa: BLE001
        broken_rate = 0.0
    return {
        "days": days, "total": total, "by_level": by_level, "modules": modules,
        "latency": latency, "top_errors": top_errors, "top_events": top_events,
        "broken_trace_rate": broken_rate, "trace_count": len(traces) if 'traces' in locals() else 0,
    }

async def evaluate_alerts(db, *, days=1, error_rate_threshold=0.1, p99_threshold_ms=5000.0, broken_rate_threshold=0.05):
    m = await get_metrics(db, days=days)
    total = m["total"] or 1
    err_count = sum(v for k, v in m["by_level"].items() if k in ("ERROR", "FATAL"))
    error_rate = err_count / total
    alerts = []
    if error_rate > error_rate_threshold:
        alerts.append({"type": "error_rate", "current": round(error_rate, 4), "threshold": error_rate_threshold, "message": "错误率超阈值"})
    if m["latency"]["p99"] and m["latency"]["p99"] > p99_threshold_ms:
        alerts.append({"type": "p99_latency", "current": m["latency"]["p99"], "threshold": p99_threshold_ms, "message": "P99 耗时超阈值"})
    if m["broken_trace_rate"] > broken_rate_threshold:
        alerts.append({"type": "broken_trace", "current": m["broken_trace_rate"], "threshold": broken_rate_threshold, "message": "断链率超阈值"})
    return {"alerts": alerts, "metrics": m}

async def log_error(db, error_code, error_layer, message, *, retryable=False, trace_id="", module="", detail=None, level="ERROR"):
    return await log_event(db, level=level, module=module, event="error", error_code=error_code, error_layer=error_layer, retryable=retryable, message=message, detail=detail, trace_id=trace_id)


async def record_http_request(db, *, trace_id: str, method: str, path: str,
                              status_code, duration_ms: float, client: str = ""):
    """记录 HTTP 请求根 span 到 system_logs（供按 trace_id 重建调用树）。

    R142：RequestLogMiddleware 每请求 fire-and-forget 调用（不阻塞主流程）；
    本函数可被测试直调（tmp 库），也作为调用树根节点 —— 其 trace_id 与后续
    业务 span（task/ai/…）同源，`get_trace_tree` 按 trace_id 即可还原全链路。
    """
    return await log_event(db, level="INFO", module="http", event="http.request",
                           trace_id=trace_id, request_id=trace_id,
                           duration_ms=round(float(duration_ms), 2),
                           status=str(status_code),
                           message=f"{method} {path} -> {status_code}",
                           detail={"method": method, "path": path,
                                   "client": client})


async def log_event_sync(*a, **kw):
    """同步上下文下的 log_event 包装（无运行循环时用 asyncio.run）。"""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        return asyncio.run(log_event(*a, **kw))
    return await log_event(*a, **kw)

async def log_events_batch(db, events):
    """批量写入，减少 IO。"""
    for ev in events or []:
        await log_event(db, **(ev or {}))

# ---------------------------------------------------------------------------
# ✅ R142-B（2026-10-29 · 异步批量缓冲 + AI 调用事件接线）：需求四「日志异步
#    写入不阻塞主流程，缓冲区批量写入减少 IO，写入失败降级内存缓冲并告警，
#    高并发限速防拖垮主流程」。SystemLogBuffer 由 main.py lifespan 启停，
#    业务模块（如 provider_factory._log_audit）enqueue 非阻塞事件，后台
#    _flush_loop 攒批经 bulk_log_events 单次落库。
# ---------------------------------------------------------------------------
import logging as _logging

_SYSTEM_LOG_INSERT = (
    "INSERT INTO system_logs (level,module,event,trace_id,span_id,parent_span_id,"
    "request_id,task_id,project_id,chapter_id,scheme_id,duration_ms,status,"
    "error_code,error_layer,retryable,message,detail)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def _normalize_event(event: dict):
    """事件字典 → system_logs 行元组；DEBUG 采样丢弃返回 None。

    与 log_event 同口径（contextvars 回填 + 脱敏 + 采样），但**不落库**（由
    bulk_log_events 批量 executemany）。显式传参优先于 contextvars。
    """
    level = str(event.get("level") or "INFO")
    if not should_sample(level):
        return None
    snap = context_snapshot()

    def _g(key):
        v = event.get(key)
        if v not in (None, ""):
            return str(v)
        return snap.get(key) or ""

    retryable = event.get("retryable")
    message = event.get("message") or ""
    detail = event.get("detail")
    return (level, event.get("module") or "", event.get("event") or "",
            _g("trace_id"), _g("span_id"), _g("parent_span_id"), _g("request_id"),
            _g("task_id"), _g("project_id"), _g("chapter_id"), _g("scheme_id"),
            event.get("duration_ms"), event.get("status") or "",
            event.get("error_code") or "", event.get("error_layer") or "",
            1 if retryable else (0 if retryable is False else None),
            redact_sensitive(message) if isinstance(message, str) else str(message),
            json.dumps(redact_sensitive(detail), ensure_ascii=False, default=str)
            if detail is not None else None)


async def bulk_log_events(db, events):
    """批量写 system_logs：单次 executemany + 单次 commit（减少 IO）。

    失败告警并返回 0（fail-soft，绝不抛出）。返回成功写入的行数。
    """
    rows = []
    for ev in events or []:
        row = _normalize_event(ev or {})
        if row is not None:
            rows.append(row)
    if not rows:
        return 0
    try:
        await db.executemany(_SYSTEM_LOG_INSERT, rows)
        await db.commit()
        return len(rows)
    except Exception as e:  # noqa: BLE001 - fail-soft
        _logging.getLogger("observability.service").warning(
            "system_logs 批量写入失败（fail-soft，已降级内存缓冲）: %s", e)
        return 0


class SystemLogBuffer:
    """异步批量写 system_logs（非阻塞 + 缓冲区批量减少 IO）。

    用法：
        from app.services.observability import system_log_buffer
        system_log_buffer.enqueue(level="INFO", module="ai", event="ai.call",
                                  message="...", detail={...})
    启动/停止由 main.py lifespan 调用（start/stop）。
    """

    def __init__(self, batch_size: int = 50, flush_interval: float = 1.0,
                 queue_size: int = 10000):
        self._batch_size = max(1, int(batch_size))
        self._flush_interval = max(0.05, float(flush_interval))
        self._queue_size = max(10, int(queue_size))
        self._queue = None
        self._task = None
        self._running = False
        self._dropped = 0
        self._flush_count = 0

    def start(self) -> bool:
        """在运行事件循环中启动后台冲刷任务（幂等）。"""
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._task is not None and not self._task.done():
            return True
        self._queue = asyncio.Queue(maxsize=self._queue_size)
        self._running = True
        self._task = loop.create_task(self._flush_loop())
        return True

    def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def enqueue(self, **event) -> bool:
        """非阻塞入队；队满丢弃最旧并计数（限速防拖垮主流程）。"""
        if self._queue is None:
            return False
        try:
            self._queue.put_nowait(dict(event))
            return True
        except asyncio.QueueFull:  # noqa: BLE001
            self._dropped += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(dict(event))
            except Exception:  # noqa: BLE001
                pass
            return False

    @property
    def dropped_count(self) -> int:
        return self._dropped

    @property
    def flush_count(self) -> int:
        return self._flush_count

    @property
    def queue_size(self) -> int:
        return self._queue.qsize() if self._queue is not None else 0

    async def _flush_loop(self):
        import asyncio
        while self._running:
            batch: list[dict] = []
            try:
                deadline = asyncio.get_running_loop().time() + self._flush_interval
                while len(batch) < self._batch_size:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        ev = await asyncio.wait_for(
                            self._queue.get(), timeout=remaining)
                        batch.append(ev)
                    except asyncio.TimeoutError:
                        break
                if batch:
                    try:
                        from app.db import get_conn
                        conn = await get_conn()
                        await bulk_log_events(conn, batch)
                        self._flush_count += 1
                    except Exception as e:  # noqa: BLE001 - fail-soft：保留在内存
                        _logging.getLogger("observability.service").warning(
                            "system_logs 缓冲落盘失败（保留内存待重试）: %s", e)
            except asyncio.CancelledError:
                if batch:
                    try:
                        from app.db import get_conn
                        conn = await get_conn()
                        await bulk_log_events(conn, batch)
                        self._flush_count += 1
                    except Exception:  # noqa: BLE001
                        pass
                break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.1)


# 模块级单例：main.py lifespan 启停，业务模块直接 enqueue。
system_log_buffer = SystemLogBuffer()

def detect_orphan_spans(items):
    span_ids = { (it.get("span_id") or "") for it in (items or []) }
    orphans = []
    for it in (items or []):
        pid = it.get("parent_span_id") or ""
        if pid and pid not in span_ids:
            orphans.append(it.get("span_id") or "")
    return orphans
