"""正文 Agent 分层修复路由（R135）。

端点（前缀 /api/v1/schemes/{scheme_id}/content-agent）：
- POST /repair  对指定小节执行 Agent 修复（隔离工作区 + 全局 FIFO 队列 +
  程序结构校验，只回写变化小节；任何失败保留原正文）；
- GET  /status  全局 Agent 队列快照（运行中 / 待执行 / 统计）。

与 ``/consistency/repair``（第一层：定点编辑 + 整章重写）的关系：
本端点是**独立触发入口**；分层回落（普通修复失败 → Agent 修复）由
``repair_agent.run_repair`` 在 ``content_agent_repair_enabled=True`` 时自动执行。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.db import get_db
from app.services import review_db
from app.services.consistency_scanner import build_global_facts_text

logger = logging.getLogger("content_agent_router")

router = APIRouter(
    prefix="/api/v1/schemes/{scheme_id}/content-agent",
    tags=["content_agent"])


async def _load_targets(db, scheme_id: str, section_ids: list[str]) -> list:
    """按 ID 加载小节当前正文，组装 SectionTarget（不存在的小节跳过并记录）。"""
    from app.services.content_agent.repair import SectionTarget

    targets: list = []
    missing: list = []
    for sid in section_ids:
        row = await review_db.fetch_one(
            db, "SELECT id, title, content, word_budget FROM sections WHERE id=? AND scheme_id=?",
            (sid, scheme_id), what="正文 Agent 修复：读取小节")
        if not row:
            missing.append(sid)
            continue
        targets.append(SectionTarget(
            section_id=sid, title=row.get("title") or "",
            content=row.get("content") or "",
            word_budget=int(row.get("word_budget") or 0)))
    if missing:
        logger.info("正文 Agent 修复：跳过 %d 个不存在的小节 %s",
                    len(missing), missing)
    return targets


@router.post("/repair")
async def repair(scheme_id: str, body: dict | None = None,
                 db=Depends(get_db)):
    """对指定小节执行 Agent 修复（同步等待，经全局 FIFO 队列串行）。"""
    body = body or {}
    section_ids = [str(s) for s in (body.get("section_ids") or []) if str(s)]
    conflicts: dict = body.get("conflicts") or {}
    if not section_ids:
        raise HTTPException(400, "section_ids 不能为空")

    row = await review_db.fetch_one(
        db, "SELECT id FROM schemes WHERE id=?", (scheme_id,),
        what="正文 Agent 修复：读取方案")
    if not row:
        raise HTTPException(404, "方案不存在")

    targets = await _load_targets(db, scheme_id, section_ids)
    if not targets:
        raise HTTPException(404, "指定小节均不存在")

    # 冲突清单按 section_id 分组注入（可选；缺省 = 按全局事实整体核对）。
    for target in targets:
        target.conflicts = list(conflicts.get(target.section_id) or [])

    facts_text = await build_global_facts_text(db, scheme_id)
    # ✅ R143（2026-10-29 · 跨模块深度收口 · P1）：此前本路由传 `resync_fn=None`，
    #    而 `repair_agent` 集成路径（repair.py:313 消费）在正文 UPDATE 同事务内
    #    **全量重登记本章内联图表**（R92 写点同步补链，消除「登记/正文」两侧漂移）。
    #    路由入口漏注入 ⇒ 经 `POST /content-agent/repair` 的 Agent 修复回写正文后
    #    `chart_registry` 与正文图表块可能失同步（旧图号/旧登记残留）。
    #    现按 `consistency_repair._make_chart_resync` 同一口径注入（enforce_limits=False：
    #    对既有正文的改写不因程序级配图上限反向删图，与手动保存 B31 同口径）。
    from app.routers._chart_pipeline import resync_inline_charts
    from app.services.content_agent.repair import run_agent_section_repair

    async def _resync(_db, _sid, _content):
        _n, _fixed = await resync_inline_charts(
            _db, scheme_id, _sid, _content, enforce_limits=False)
        return _fixed

    result = await run_agent_section_repair(
        db, scheme_id=scheme_id, targets=targets, facts_text=facts_text,
        resync_fn=_resync,
        max_turns=body.get("max_turns"),
        total_timeout=body.get("timeout"))
    return {"ok": result.status != "failed", **result.to_dict()}


@router.get("/status")
async def status(scheme_id: str):
    """全局 Agent 队列快照（进程内单例，与方案无关，便于运维观察）。"""
    from app.services.content_agent.queue import global_agent_queue

    stats = global_agent_queue().stats()
    return {
        "running": stats.running,
        "pending": stats.pending,
        "paused": stats.paused,
        "max_pending": stats.max_pending,
        "completed": stats.completed,
        "failed": stats.failed,
    }
