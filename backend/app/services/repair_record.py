"""修复记录与版本快照（F-AGENT-CONSISTENCY-REPAIR §5.5）

职责：
- 修复前为涉及章节保存版本快照（scheme_snapshots）
- 保存修复批次记录（consistency_repairs）
- 支持一键回滚：只影响快照涉及的章节
- 查询历史修复记录

所有写操作幂等且只影响方案正文，不触碰其他数据。

## DB 访问单一出口（2026-10-17 收口）

本模块此前有 9 处**裸** ``db.execute``，零游标判空（R13，AGENTS §5.5），
且写路径**汇报假成功**：

  ==========================  =========================================
  站点                        假成功后的用户可见后果
  ==========================  =========================================
  ``mark_repair_status``       ``/consistency/confirm`` 与 ``/rollback``
                               返回 ``confirmed`` / ``rolled_back``，
                               而 ``consistency_repairs.status`` 未变
  ``rollback_snapshot``        ``restored`` 计数照加（``restored += 1``
                               不看写入结果），用户以为已恢复正文
  ``save_repair``              返回一个**从未落库**的 ``repair_id``，
                               后续 confirm/rollback 全部找不到批次
  ``get_snapshot``/``get_repair``  ``AttributeError`` → 500
                               （读路径本应 503「数据服务暂不可用」，
                               空结果会被误读成「快照不存在」）
  ==========================  =========================================

现全部改走 :mod:`app.services.review_db`（审核与预检模块的**唯一** DB 出口，
R78 已把四个 router 收口到同一处；本模块是当时漏网的 services 侧空白点）：

  * 读路径 ``fetch_one`` / ``fetch_all`` / ``fetch_scalar`` —— 游标为
    ``None`` 抛 503，**不是**空结果（否则「快照不存在」会被误当业务答案）；
  * 写路径 ``exec_write(require_rows=True)`` —— 游标为 ``None`` 或
    ``rowcount == 0`` 均抛 503，**绝不汇报假成功**。

⚠️ 语义变化（均为「把原本静默的错误变成显式的 503」，正常路径逐字不变）：
  ① ``get_snapshot`` 查不到行仍返回 ``None``（调用方转 ``KeyError``）；
     DB 不可用时不再谎报「快照不存在」，而是 503。
  ② ``mark_repair_status`` / ``save_repair`` / ``rollback_snapshot`` 的写
     在未生效时不再静默通过；调用点（``routers/consistency_repair.py``
     ``routers/review_autofix.py``）均**未**包裹 ``except Exception``，
     503 会如实冒泡到 HTTP 层。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from app.db import get_conn  # noqa: F401  — 兼容旧调用（历史模块级引用）
from app.services.content_utils import (
    DEFAULT_WORD_BUDGET,
    text_word_count,
    word_status_for,
)
from app.services.review_db import exec_write, fetch_all, fetch_one, fetch_scalar

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
    await exec_write(
        db,
        "INSERT INTO scheme_snapshots (id, scheme_id, type, sections, created_at)"
        " VALUES (?,?,?,?,?)",
        (snapshot_id, scheme_id, snapshot_type,
         json.dumps(payload, ensure_ascii=False), _now()),
        what="repair_record.create_snapshot:INSERT")
    await db.commit()
    return snapshot_id


async def get_snapshot(db, snapshot_id: str) -> dict | None:
    item = await fetch_one(
        db,
        "SELECT id, scheme_id, type, sections, created_at FROM scheme_snapshots WHERE id=?",
        (snapshot_id,), what="repair_record.get_snapshot")
    if not item:
        return None
    try:
        item["sections"] = json.loads(item.get("sections") or "[]")
    except json.JSONDecodeError:
        item["sections"] = []
    return item


async def rollback_snapshot(db, snapshot_id: str, *,
                            undo_type: str = "consistency_rollback") -> dict:
    """一键回滚：把快照涉及章节的正文恢复为 content_before。

    回滚前再存一个"回滚前"快照，保证回滚本身也可撤销；只更新快照内章节。
    undo_type：撤销快照的 type（编号版本管理传 "numbering_rollback" 以隔离场景）。
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
        cur_content = await fetch_scalar(
            db, "SELECT content FROM sections WHERE id=?", (sid,),
            default="", what="repair_record.rollback_snapshot:读当前正文")
        current.append({"section_id": sid, "content_before": cur_content or ""})
    undo_id = await create_snapshot(db, snap["scheme_id"], current,
                                    snapshot_type=undo_type)

    restored = 0
    for sec in sections:
        sid = sec.get("section_id")
        if not sid:
            continue
        srow = await fetch_one(
            db, "SELECT id, word_budget FROM sections WHERE id=?", (sid,),
            what="repair_record.rollback_snapshot:读章节")
        if not srow:
            continue
        # ✅ BUG 修复（2026-09-16 · 与 repair_agent 同一字数口径问题）：
        #    旧实现回滚只写 content，word_count / word_status 停留在**修复后**的值 ——
        #    回滚后章节字数与状态与实际正文脱节（例如修复把 1200 字改成 1500 字后
        #    回滚，库里仍显示 1500 字/over）。这里按全项目唯一口径重算。
        restored_content = sec.get("content_before", "")
        wc = text_word_count(restored_content)
        # ✅ 2026-10-17：改走 exec_write（require_rows=True）——旧实现
        #    `restored += 1` 不看写入结果，DB 写未生效时仍汇报「已恢复 N 章」。
        await exec_write(
            db,
            "UPDATE sections SET content=?, word_count=?, word_status=?, updated_at=? "
            "WHERE id=?",
            (restored_content, wc,
             word_status_for(wc, srow["word_budget"] or DEFAULT_WORD_BUDGET),
             _now(), sid),
            what="repair_record.rollback_snapshot:恢复正文")
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
    await exec_write(
        db,
        "INSERT INTO consistency_repairs "
        "(id, scheme_id, scan_id, mode, total_conflicts, repaired, skipped, failed,"
        " items, snapshot_id, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (repair_id, scheme_id, scan_id, mode, total_conflicts,
         repaired, skipped, failed,
         json.dumps(items, ensure_ascii=False), snapshot_id,
         "pending_confirm", _now()),
        what="repair_record.save_repair:INSERT")
    await db.commit()
    return {
        "repair_id": repair_id, "scan_id": scan_id,
        "total_conflicts": total_conflicts,
        "repaired": repaired, "skipped": skipped, "failed": failed,
        "snapshot_id": snapshot_id, "status": "pending_confirm",
        "items": items,
    }


async def get_repair(db, repair_id: str) -> dict | None:
    item = await fetch_one(
        db, "SELECT * FROM consistency_repairs WHERE id=?", (repair_id,),
        what="repair_record.get_repair")
    if not item:
        return None
    try:
        item["items"] = json.loads(item.get("items") or "[]")
    except json.JSONDecodeError:
        item["items"] = []
    return item


async def mark_repair_status(db, repair_id: str, status: str):
    # ✅ BUG 修复（2026-10-17）：旧实现 `await db.execute(...)` 后**不看结果**
    #    就 commit 并返回 —— R13 下 execute 返回 None 时写根本没生效，调用点
    #    （/consistency/confirm、/consistency/rollback、/autofix/confirm、
    #    /autofix/rollback）照样返回 confirmed / rolled_back，界面报「已回滚」
    #    而 consistency_repairs.status 未变。改走 exec_write（require_rows=True）
    #    → 未生效即抛 503，**绝不汇报假成功**。
    await exec_write(
        db, "UPDATE consistency_repairs SET status=? WHERE id=?",
        (status, repair_id),
        what=f"repair_record.mark_repair_status({status})")
    await db.commit()


async def list_repairs(db, scheme_id: str, limit: int = 20) -> list[dict]:
    limit = max(1, min(limit, 50))
    return await fetch_all(
        db,
        "SELECT id, scan_id, mode, total_conflicts, repaired, skipped, failed,"
        " snapshot_id, status, created_at FROM consistency_repairs "
        "WHERE scheme_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (scheme_id, limit),
        what="repair_record.list_repairs")
