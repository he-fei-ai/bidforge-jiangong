"""修复记录与版本快照（F-AGENT-CONSISTENCY-REPAIR §5.5）

职责：
- 修复前为涉及章节保存版本快照（scheme_snapshots）
- 保存修复批次记录（consistency_repairs）
- 支持一键回滚：只影响快照涉及的章节
- 查询历史修复记录

所有写操作幂等且只影响方案正文，不触碰其他数据。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from app.db import get_conn  # noqa: F401  — 兼容旧调用（历史模块级引用）
from app.services.content_utils import (
    text_word_count, word_status_for, DEFAULT_WORD_BUDGET,
)

logger = logging.getLogger("repair_record")


def _now() -> str:
    return datetime.now().isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def create_snapshot(
    db, scheme_id: str, sections: list[dict], snapshot_type: str = "consistency_repair"
) -> str:
    """为指定章节（dict 需含 id/content）保存修复前快照，返回 snapshot_id。

    sections: [{"section_id": ..., "content_before": ...}, ...] 或含 id/content 的字典。
    """
    payload = []
    for s in sections:
        sid = s.get("section_id") or s.get("id")
        payload.append({
            "section_id": sid,
            "content_before": s.get("content_before", s.get("content", "")),
        })
    snapshot_id = new_id("ver")
    await db.execute(
        "INSERT INTO scheme_snapshots (id, scheme_id, type, sections, created_at)"
        " VALUES (?,?,?,?,?)",
        (snapshot_id, scheme_id, snapshot_type,
         json.dumps(payload, ensure_ascii=False), _now()))
    await db.commit()
    return snapshot_id


async def get_snapshot(db, snapshot_id: str) -> dict | None:
    cur = await db.execute(
        "SELECT id, scheme_id, type, sections, created_at FROM scheme_snapshots WHERE id=?",
        (snapshot_id,))
    row = await cur.fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["sections"] = json.loads(item.get("sections") or "[]")
    except json.JSONDecodeError:
        item["sections"] = []
    return item


async def rollback_snapshot(db, snapshot_id: str) -> dict:
    """一键回滚：把快照涉及章节的正文恢复为 content_before。

    回滚前再存一个"回滚前"快照，保证回滚本身也可撤销；只更新快照内章节。
    """
    snap = await get_snapshot(db, snapshot_id)
    if not snap:
        raise KeyError("快照不存在")
    sections = snap.get("sections") or []
    if not sections:
        return {"snapshot_id": snapshot_id, "restored": 0}

    # 回滚前保存当前正文，便于撤销回滚
    current = []
    for sec in sections:
        sid = sec.get("section_id")
        if not sid:
            continue
        cur = await db.execute("SELECT content FROM sections WHERE id=?", (sid,))
        row = await cur.fetchone()
        current.append({"section_id": sid,
                        "content_before": row[0] if row else ""})
    undo_id = await create_snapshot(db, snap["scheme_id"], current,
                                    snapshot_type="consistency_rollback")

    restored = 0
    for sec in sections:
        sid = sec.get("section_id")
        if not sid:
            continue
        cur = await db.execute(
            "SELECT id, word_budget FROM sections WHERE id=?", (sid,))
        srow = await cur.fetchone()
        if not srow:
            continue
        # ✅ BUG 修复（2026-09-16 · 与 repair_agent 同一字数口径问题）：
        #    旧实现回滚只写 content，word_count / word_status 停留在**修复后**的值 ——
        #    回滚后章节字数与状态与实际正文脱节（例如修复把 1200 字改成 1500 字后
        #    回滚，库里仍显示 1500 字/over）。这里按全项目唯一口径重算。
        restored_content = sec.get("content_before", "")
        wc = text_word_count(restored_content)
        await db.execute(
            "UPDATE sections SET content=?, word_count=?, word_status=?, updated_at=? "
            "WHERE id=?",
            (restored_content, wc,
             word_status_for(wc, srow["word_budget"] or DEFAULT_WORD_BUDGET),
             _now(), sid))
        restored += 1
    await db.commit()
    logger.info("快照 %s 已回滚：恢复 %d 章（撤销快照 %s）",
                snapshot_id, restored, undo_id)
    return {"snapshot_id": snapshot_id, "restored": restored, "undo_snapshot_id": undo_id}


async def save_repair(db, *, scheme_id: str, scan_id: str, mode: str,
                      items: list[dict], snapshot_id: str,
                      total_conflicts: int,
                      stats: dict | None = None) -> dict:
    """落库一次修复批次，返回统计与 repair_id（状态 pending_confirm，等待用户确认）。

    stats：可选，显式指定 repaired/failed/skipped（**按冲突 id 计数**）。
    不传时按 items 的条目状态计数（历史口径：一章多冲突会重复计数）——
    `run_repair` 现统一传入按冲突计的统计，保证 repaired+failed+skipped == total_conflicts。
    """
    if stats:
        repaired = int(stats.get("repaired") or 0)
        skipped = int(stats.get("skipped") or 0)
        failed = int(stats.get("failed") or 0)
    else:
        repaired = sum(1 for i in items if i.get("status") == "repaired")
        skipped = sum(1 for i in items if i.get("status") == "skipped")
        failed = sum(1 for i in items if i.get("status") == "failed")
    repair_id = new_id("R")
    await db.execute(
        "INSERT INTO consistency_repairs "
        "(id, scheme_id, scan_id, mode, total_conflicts, repaired, skipped, failed,"
        " items, snapshot_id, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (repair_id, scheme_id, scan_id, mode, total_conflicts,
         repaired, skipped, failed,
         json.dumps(items, ensure_ascii=False), snapshot_id,
         "pending_confirm", _now()))
    await db.commit()
    return {
        "repair_id": repair_id, "scan_id": scan_id,
        "total_conflicts": total_conflicts,
        "repaired": repaired, "skipped": skipped, "failed": failed,
        "snapshot_id": snapshot_id, "status": "pending_confirm",
        "items": items,
    }


async def get_repair(db, repair_id: str) -> dict | None:
    cur = await db.execute(
        "SELECT * FROM consistency_repairs WHERE id=?", (repair_id,))
    row = await cur.fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["items"] = json.loads(item.get("items") or "[]")
    except json.JSONDecodeError:
        item["items"] = []
    return item


async def mark_repair_status(db, repair_id: str, status: str):
    await db.execute(
        "UPDATE consistency_repairs SET status=? WHERE id=?", (status, repair_id))
    await db.commit()


async def list_repairs(db, scheme_id: str, limit: int = 20) -> list[dict]:
    limit = max(1, min(limit, 50))
    cur = await db.execute(
        "SELECT id, scan_id, mode, total_conflicts, repaired, skipped, failed,"
        " snapshot_id, status, created_at FROM consistency_repairs "
        "WHERE scheme_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (scheme_id, limit))
    return [dict(r) for r in await cur.fetchall()]
