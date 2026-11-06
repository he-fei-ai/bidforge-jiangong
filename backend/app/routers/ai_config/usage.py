"""AI 配置路由 · 用量统计 / 审计日志（stats / audit-logs）。"""

from fastapi import APIRouter, Depends

from app.db import get_db, read_db, safe_rowcount
from app.models import AuditLogCleanup

router = APIRouter(tags=["ai_config"])


@router.get("/stats")
async def ai_stats(days: int = 30, db=Depends(read_db)):
    """AI 用量统计。

    ✅ 修复/增强：
      - Token 三列（prompt/completion/cached）此前从未被写入，现已在
        provider_factory 的审计链路补齐，这里同时补上缓存命中率与按操作维度统计；
      - 无数据时 success_rate 不再显示为 0（改由前端按 total 判断），
        且避免 `AVG` 返回 None 导致前端 `.toFixed()` 抛错。

    ✅ 2026-09-17 增强（AI 模型指标优化）：
      - cache_hit_rate 口径修正为 cached/prompt（原为 cached/总token——
        输出 token 天然无缓存，分母掺入 completion 会把命中率稀释约 20%）；
      - avg_duration 改为仅统计成功调用（原把 duration=0 的熔断跳过/
        秒失败的记录一起平均，实测把整体均值拉低约一半）；
      - by_provider 增加 fail_count / cache_hit_rate / avg_duration_ok；
      - 新增 by_error：失败原因 TOP 分布（配合审计 error 列，
        近 7 天 35% 失败记录此前零信息，无法区分 429/超时/认证失败）。

    ✅ 2026-09-17 第二轮：统计口径修正 —— by_provider / summary 只统计真实调用
      （action='chat'），「熔断器跳过」（circuit_skipped）单列 skipped_count。
      旧口径把 3180 条「压根没发出去的请求」算作失败，成功率被系统性低估
      （agnes-3.0-flash 显示 13.9% vs 真实 37.6%）。
    """
    days = max(1, min(int(days or 30), 365))
    since = f"-{days} days"

    # ✅ 修复（本轮审查）：原查询不限 action，把「熔断器 OPEN 跳过」的记录
    #    （action='circuit_skipped'，success=0，duration=0）当成一次真实调用计入 ——
    #    运行库实测 3180 条跳过 vs 6108 条真实调用，跳过占比 34%，
    #    界面上 agnes-3.0-flash 显示 13.9% 成功率、sensetime/deepseek-v4-flash 3.7%，
    #    而真实成功率分别是 37.6% / 26%：失败数被凭空放大，用户据此误判模型不可用。
    #    现 by_provider 只统计真实调用（action='chat'），熔断跳过单列 skipped_count。
    cur = await db.execute(
        "SELECT provider_name, model, COALESCE(config_id,'') as config_id,"
        " COALESCE(base_url,'') as base_url, COUNT(*) as calls, "
        "SUM(prompt_tokens) as prompt_tokens, SUM(completion_tokens) as completion_tokens, "
        "SUM(cached_tokens) as cached_tokens, "
        "SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) as success_count, "
        "SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) as fail_count, "
        # ✅ 2026-11-05（方案 B）：calls 是逻辑调用行数；attempts 是底层尝试数
        #    （含重试/降级/对冲）。旧行 attempt_count 由迁移补默认 1，口径不变。
        "COALESCE(SUM(COALESCE(attempt_count, 1)), 0) as attempts, "
        "AVG(CASE WHEN success=1 THEN duration END) as avg_duration_ok "
        "FROM ai_audit_logs WHERE action='chat' "
        "AND created_at >= datetime('now','localtime',?) "
        "GROUP BY provider_name, model, config_id, base_url ORDER BY calls DESC",
        (since,),
    )
    by_provider = [dict(r) for r in await cur.fetchall()]
    cur = await db.execute(
        "SELECT provider_name, model, COALESCE(config_id,'') as config_id,"
        " COALESCE(base_url,'') as base_url, COUNT(*) as n FROM ai_audit_logs "
        "WHERE action='circuit_skipped' AND created_at >= datetime('now','localtime',?) "
        "GROUP BY provider_name, model, config_id, base_url",
        (since,),
    )
    _skipped_map = {
        (r["provider_name"], r["model"], r["config_id"], r["base_url"]): int(r["n"] or 0)
        for r in (dict(x) for x in await cur.fetchall())
    }
    for p in by_provider:
        p["skipped_count"] = _skipped_map.pop((p["provider_name"], p["model"], p["config_id"], p["base_url"]), 0)
        p["success_rate"] = round((p["success_count"] or 0) * 100.0 / p["calls"], 1) if p["calls"] else None
        # 尝试级失败 = 尝试数 - 成功次数（每次成功逻辑调用恰好 1 次成功尝试）
        p["attempt_failed"] = max(0, int(p.get("attempts") or 0) - int(p.get("success_count") or 0))
        # 缓存命中率：cached / prompt（输出 token 无缓存语义，不进分母）
        p["cache_hit_rate"] = (
            round((p["cached_tokens"] or 0) * 100.0 / p["prompt_tokens"], 1) if p["prompt_tokens"] else None
        )
        p["avg_duration_ok"] = round(p["avg_duration_ok"] or 0, 1)
    # 只被熔断跳过、从未真实调用过的配置也要可见（否则「为什么它一次都没成功」无迹可寻）
    for (_pn, _mn, _cid, _url), _n in _skipped_map.items():
        by_provider.append(
            {
                "provider_name": _pn,
                "model": _mn,
                "config_id": _cid,
                "base_url": _url,
                "calls": 0,
                "attempts": 0,
                "attempt_failed": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cached_tokens": 0,
                "success_count": 0,
                "fail_count": 0,
                "avg_duration_ok": 0,
                "success_rate": None,
                "cache_hit_rate": None,
                "skipped_count": _n,
            }
        )

    cur = await db.execute(
        "SELECT COUNT(*) as total, "
        "COALESCE(SUM(prompt_tokens + completion_tokens), 0) as total_tokens, "
        "COALESCE(SUM(cached_tokens), 0) as cached_tokens, "
        "COALESCE(SUM(prompt_tokens), 0) as prompt_tokens, "
        "COALESCE(SUM(CASE WHEN success=1 THEN 1 ELSE 0 END), 0) as success_count, "
        "COALESCE(SUM(COALESCE(attempt_count, 1)), 0) as attempts, "
        "COALESCE(AVG(CASE WHEN success=1 THEN duration END), 0) as avg_duration "
        "FROM ai_audit_logs WHERE action='chat' "
        "AND created_at >= datetime('now','localtime',?)",
        (since,),
    )
    summary = dict(await cur.fetchone())
    total = summary.get("total") or 0
    summary["total_tokens"] = summary.get("total_tokens") or 0
    summary["success_count"] = summary.get("success_count") or 0
    summary["avg_duration"] = summary.get("avg_duration") or 0.0
    summary["success_rate"] = round(summary["success_count"] * 100.0 / total, 1) if total else None
    # 缓存命中率：cached / prompt（✅ 口径修正，见上）
    summary["cache_hit_rate"] = (
        round(summary["cached_tokens"] * 100.0 / summary["prompt_tokens"], 1) if summary["prompt_tokens"] else None
    )
    summary["days"] = days
    # ✅ 2026-11-05（方案 B）：failed_count = 逻辑调用失败数；attempt_failed = 尝试失败数
    summary["failed_count"] = total - summary["success_count"]
    summary["attempts"] = summary.get("attempts") or 0
    summary["attempt_failed"] = max(0, int(summary["attempts"]) - summary["success_count"])
    summary["attempt_success_rate"] = (
        round(summary["success_count"] * 100.0 / summary["attempts"], 1) if summary["attempts"] else None
    )
    # ✅ 熔断跳过单独成列：它是「没发出去的请求」，不是「发出去但失败了」
    cur = await db.execute(
        "SELECT COUNT(*) FROM ai_audit_logs WHERE action='circuit_skipped' "
        "AND created_at >= datetime('now','localtime',?)",
        (since,),
    )
    summary["skipped_count"] = (await cur.fetchone())[0] or 0

    cur = await db.execute(
        # ✅ 2026-11-05（统计口径修复）：旧查询未过滤 action，circuit_skipped /
        #    provider_disabled 等「未真实发请求」的行被算进每日 calls 与成功率分母
        #    （生产事故窗 264 次真实调用里混入大量跳过行 → 面板"今日失败 128 次 /
        #    成功率 86.7%"，而 ai_audit_logs 真实失败仅 26 次）。统一为 action='chat'。
        "SELECT DATE(created_at) as date, COUNT(*) as calls, "
        "COALESCE(SUM(prompt_tokens + completion_tokens), 0) as tokens, "
        "COALESCE(SUM(CASE WHEN success=1 THEN 1 ELSE 0 END), 0) as success_count, "
        "COALESCE(SUM(CASE WHEN success=0 THEN 1 ELSE 0 END), 0) as failed_count, "
        "COALESCE(SUM(COALESCE(attempt_count, 1)), 0) as attempts "
        "FROM ai_audit_logs WHERE action='chat' "
        "AND created_at >= datetime('now','localtime',?) "
        "GROUP BY DATE(created_at) ORDER BY date DESC LIMIT ?",
        # ✅ 修复：原为 min(days, 60) —— 选「最近 90/365 天」时趋势数据被静默截成 60 天，
        #    统计口径与调用方请求不符。days 本身已被钳制在 365 内，这里不再额外缩水。
        (since, min(days, 366)),
    )
    daily = [dict(r) for r in await cur.fetchall()]

    cur = await db.execute(
        "SELECT action, COUNT(*) as calls, "
        "COALESCE(SUM(CASE WHEN success=1 THEN 1 ELSE 0 END), 0) as success_count "
        "FROM ai_audit_logs WHERE created_at >= datetime('now','localtime',?) "
        "GROUP BY action ORDER BY calls DESC LIMIT 20",
        (since,),
    )
    by_action = [dict(r) for r in await cur.fetchall()]

    # ✅ 2026-09-17 新增：失败原因 TOP 分布（错误摘要前 80 字符 + provider 聚合）
    # ✅ 修复（本轮审查）：
    #   1) 原查询未排除 action='circuit_skipped' —— 「熔断器 OPEN，跳过本次调用」
    #      被当成真实失败原因排在 TOP 前列，把真正的 429/402/超时全挤到看不见的地方；
    #   2) 原查询用 ``error!=''`` 过滤，而 httpx 超时类异常 str() 为空 ——
    #      实测 1141 条失败（占真实失败 52%）在失败原因里彻底消失。
    #      现改为不丢弃它们，统一显示为「（无错误信息·疑似超时）」（空值归一类）。
    cur = await db.execute(
        "SELECT provider_name, model, "
        "SUBSTR(COALESCE(NULLIF(error, ''), '(无错误信息·疑似超时)'), 1, 80) as err, "
        "COUNT(*) as calls "
        "FROM ai_audit_logs "
        "WHERE action='chat' AND success=0 "
        "AND created_at >= datetime('now','localtime',?) "
        "GROUP BY provider_name, model, err ORDER BY calls DESC LIMIT 8",
        (since,),
    )
    by_error = [dict(r) for r in await cur.fetchall()]

    # ✅ 2026-11-05（方案 B）：失败按 error_class 聚合（机器可读类别 + 占比）。
    #    历史 NULL/'' 行归入 unknown，前端无数据时显示 0 不报错。
    cur = await db.execute(
        "SELECT COALESCE(NULLIF(error_class, ''), 'unknown') as error_class, "
        "COUNT(*) as calls FROM ai_audit_logs "
        "WHERE action='chat' AND success=0 "
        "AND created_at >= datetime('now','localtime',?) "
        # GROUP BY 1（按 SELECT 表达式）而非列名：error_class 与真实列同名，
        # SQLite 会按原始列分组，把 '' 与 NULL 拆成两组再都映射成 unknown。
        "GROUP BY 1 ORDER BY calls DESC",
        (since,),
    )
    _err_rows = [dict(r) for r in await cur.fetchall()]
    _err_total = sum(int(r.get("calls") or 0) for r in _err_rows)
    by_error_class = [
        {
            "error_class": r["error_class"],
            "calls": int(r["calls"] or 0),
            "share": round(int(r["calls"] or 0) * 100.0 / _err_total, 1) if _err_total else 0.0,
        }
        for r in _err_rows
    ]

    # ✅ 2026-09-21：按业务场景聚合（outline_draft/outline_review/...）。
    #    「一次目录生成到底烧了多少次调用/token」从时间窗估算变为精确数字，
    #    是调用次数优化（A~D）的度量基准。仅统计打了 scene 标记的真实调用。
    cur = await db.execute(
        "SELECT scene, COUNT(*) as calls, "
        "COALESCE(SUM(CASE WHEN success=1 THEN 1 ELSE 0 END), 0) as success_count, "
        "COALESCE(SUM(prompt_tokens + completion_tokens), 0) as tokens, "
        "COALESCE(SUM(COALESCE(attempt_count, 1)), 0) as attempts "
        "FROM ai_audit_logs WHERE action='chat' AND scene!='' "
        "AND created_at >= datetime('now','localtime',?) "
        "GROUP BY scene ORDER BY calls DESC LIMIT 20",
        (since,),
    )
    by_scene = [dict(r) for r in await cur.fetchall()]

    return {
        "by_provider": by_provider,
        "by_action": by_action,
        "by_error": by_error,
        "by_error_class": by_error_class,
        "by_scene": by_scene,
        "summary": summary,
        "daily": daily,
    }


@router.get("/audit-logs")
async def audit_logs(
    limit: int = 50,
    offset: int = 0,
    provider_name: str = "",
    action: str = "",
    success: str = "",
    days: int = 0,
    scene: str = "",
    error_class: str = "",
    db=Depends(read_db),
):
    """调用审计明细（✅ 增强：筛选 + 分页 + 总数）。

    原实现只支持 limit，前端无法按供应商/结果过滤，日志上千条时基本看不过来。

    ✅ 2026-09-25（数据链闭环）：新增 ``scene`` 筛选 + 返回 ``scenes`` 可选值 ——
    ``/ai/stats`` 的 by_scene 聚合此前已按场景出数，但明细无法按场景下钻
    （聚合→明细的数据链在最后一环断开）。前端据此渲染场景筛选下拉。
    """
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))

    where, params = [], []
    if provider_name.strip():
        where.append("provider_name=?")
        params.append(provider_name.strip())
    if action.strip():
        where.append("action=?")
        params.append(action.strip())
    if success in ("0", "1"):
        where.append("success=?")
        params.append(int(success))
    if scene.strip():
        where.append("scene=?")
        params.append(scene.strip())
    # ✅ 2026-11-05（方案 B）：失败类别筛选（历史 NULL/'' 行归 unknown 时也能命中）
    if error_class.strip():
        cls = error_class.strip()
        if cls == "unknown":
            where.append("(COALESCE(NULLIF(error_class, ''), 'unknown')=?)")
        else:
            where.append("error_class=?")
        params.append(cls)
    if days and int(days) > 0:
        where.append("created_at >= datetime('now','localtime',?)")
        params.append(f"-{min(int(days), 365)} days")
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    cur = await db.execute(f"SELECT COUNT(*) FROM ai_audit_logs {clause}", tuple(params))
    total = (await cur.fetchone())[0]

    cur = await db.execute(
        "SELECT id, provider_name, model, action, prompt_tokens, completion_tokens, "
        "cached_tokens, duration, success, error, scene, "
        "COALESCE(error_class,'') as error_class, COALESCE(attempt_count,1) as attempt_count, "
        "COALESCE(attempts_json,'') as attempts_json, created_at "
        f"FROM ai_audit_logs {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    items = [dict(r) for r in await cur.fetchall()]

    # 供前端筛选下拉使用（无数据时也返回，避免下拉为空）
    cur = await db.execute(
        "SELECT DISTINCT provider_name FROM ai_audit_logs WHERE provider_name!='' ORDER BY provider_name"
    )
    providers = [r[0] for r in await cur.fetchall()]
    cur = await db.execute("SELECT DISTINCT action FROM ai_audit_logs WHERE action!='' ORDER BY action")
    actions = [r[0] for r in await cur.fetchall()]
    # ✅ 2026-09-25：场景筛选可选值（与 stats.by_scene 同源，闭环聚合→明细下钻）
    cur = await db.execute("SELECT DISTINCT scene FROM ai_audit_logs WHERE scene!='' ORDER BY scene LIMIT 200")
    scenes = [r[0] for r in await cur.fetchall()]
    # ✅ 2026-11-05：失败类别筛选可选值（'' 归 unknown，保证下拉可选中历史行）
    cur = await db.execute(
        "SELECT COALESCE(NULLIF(error_class, ''), 'unknown') FROM ai_audit_logs "
        "WHERE action='chat' AND success=0 GROUP BY 1 ORDER BY 1"
    )
    error_classes = [r[0] for r in await cur.fetchall()]

    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "providers": providers,
        "actions": actions,
        "scenes": scenes,
        "error_classes": error_classes,
    }


@router.delete("/audit-logs")
async def cleanup_audit_logs(body: AuditLogCleanup, db=Depends(get_db)):
    """清理历史审计日志（✅ 新增：原实现只增不删，日志无限增长）。

    默认保留最近 30 天；only_failed=true 时只清失败记录（保留成功样本用于统计）。
    """
    keep_days = max(1, min(int(body.keep_days or 30), 3650))
    params: list = [f"-{keep_days} days"]
    sql = "DELETE FROM ai_audit_logs WHERE created_at < datetime('now','localtime',?)"
    if body.only_failed:
        sql += " AND success=0"
    cur = await db.execute(sql, tuple(params))
    # R13：旧实现 cur.rowcount 无守卫，execute() 返回 None 时该端点直接 500
    deleted = safe_rowcount(cur, what="AI 审计日志清理")
    await db.commit()
    return {"ok": True, "deleted": deleted, "keep_days": keep_days, "only_failed": body.only_failed}
