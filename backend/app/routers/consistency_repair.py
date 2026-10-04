"""全文一致性 Agent 修复路由（F-AGENT-CONSISTENCY-REPAIR v1.0）

端点（前缀 /api/v1/schemes/{scheme_id}/consistency）：
- POST /scan      扫描 + 仲裁，生成冲突清单
- POST /repair    按仲裁结果定向修复（修复前自动快照，写正文待确认）
- POST /confirm   逐条接受 / 拒绝（拒绝 = 该章恢复快照原文）
- POST /rollback  按快照一键回滚
- GET  /repairs   修复批次历史
- GET  /conflicts 最新/指定扫描的冲突清单
- GET  /repair/{repair_id} 修复批次详情（含前后对比）
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from app.db import get_db
from app.routers.review import reset_review_on_content_change  # ✅ G9：修复改写正文→退回待审核
from app.services import conflict_arbiter as arbiter
from app.services import consistency_scanner as scanner
from app.services import repair_agent, repair_record
from app.services.content_utils import (
    DEFAULT_WORD_BUDGET,
    text_word_count,
    word_status_for,
)

logger = logging.getLogger("consistency_repair")

router = APIRouter(
    prefix="/api/v1/schemes/{scheme_id}/consistency",
    tags=["consistency_repair"])


async def _load_scheme(db, scheme_id: str) -> dict:
    cur = await db.execute(
        "SELECT id, project_id, name, type FROM schemes WHERE id=?", (scheme_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "方案不存在")
    return dict(row)


async def _load_conflicts(db, scheme_id: str, scan_id: str) -> list[dict]:
    cur = await db.execute(
        "SELECT id, conflict_type, severity, topic, occurrences, authoritative_value,"
        " authoritative_source, repair_instruction, reason, status "
        "FROM consistency_conflicts WHERE scheme_id=? AND scan_id=? ORDER BY id",
        (scheme_id, scan_id))
    out = []
    for r in await cur.fetchall():
        item = dict(r)
        try:
            item["occurrences"] = json.loads(item.get("occurrences") or "[]")
        except json.JSONDecodeError:
            item["occurrences"] = []
        out.append(item)
    return out


def _summary(conflicts: list[dict]) -> dict:
    return {
        "total": len(conflicts),
        "high": sum(1 for c in conflicts if c.get("severity") == "high"),
        "medium": sum(1 for c in conflicts if c.get("severity") == "medium"),
        "low": sum(1 for c in conflicts if c.get("severity") == "low"),
        "pending": sum(1 for c in conflicts if c.get("status") == "pending"),
        "skipped": sum(1 for c in conflicts if c.get("status") == "skipped"),
    }


@router.post("/scan")
async def scan(scheme_id: str, body: dict | None = None, db=Depends(get_db)):
    body = body or {}
    scheme = await _load_scheme(db, scheme_id)

    async def _progress(done: int, total: int, message: str):
        logger.info("[一致性扫描] %s %d/%d", scheme_id, done, total)

    try:
        result = await scanner.run_scan(
            db,
            scheme_id=scheme_id,
            project_id=scheme["project_id"],
            scheme_name=scheme["name"],
            scheme_type=scheme["type"],
            include_global_facts=bool(body.get("include_global_facts", True)),
            include_project_docs=bool(body.get("include_project_docs", True)),
            include_design_docs=bool(body.get("include_design_docs", True)),
            progress_cb=_progress,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("一致性扫描失败")
        raise HTTPException(500, f"一致性扫描失败：{e}")

    if not result.get("sections"):
        raise HTTPException(422, "方案尚无正文内容，请先生成正文")

    # 扫描完成立即仲裁（定级 + 权威值 + 修复指令）
    conflicts = await arbiter.arbitrate_conflicts(
        db, result["conflicts"],
        facts=result["contexts"].get("global_facts", ""),
        design_docs=result["contexts"].get("design_docs", ""),
        standards=result["contexts"].get("standards", ""),
        project_requirements=result["contexts"].get("project_docs", ""))

    return {
        "scan_id": result["scan_id"],
        "status": "completed",
        "scanned_sections": result["sections"],
        "summary": _summary(conflicts),
        "conflicts": conflicts,
    }


@router.get("/conflicts")
async def get_conflicts(scheme_id: str, scan_id: str = "", db=Depends(get_db)):
    if not scan_id:
        cur = await db.execute(
            "SELECT scan_id FROM consistency_conflicts WHERE scheme_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (scheme_id,))
        row = await cur.fetchone()
        if not row:
            return {"exists": False, "conflicts": [], "summary": _summary([])}
        scan_id = row[0]
    conflicts = await _load_conflicts(db, scheme_id, scan_id)
    return {"exists": True, "scan_id": scan_id,
            "summary": _summary(conflicts), "conflicts": conflicts}


@router.post("/repair")
async def repair(scheme_id: str, body: dict | None = None, db=Depends(get_db)):
    body = body or {}
    scan_id = body.get("scan_id", "")
    mode = body.get("mode", "auto")
    severity_threshold = body.get("severity_threshold", "high")
    conflict_ids = body.get("conflict_ids") or None
    # ✅ 2026-09-22：默认跳过「已修复且修复成果仍在正文里」的冲突（省 AI 调用）；
    #    传 force_full_repair=true 可强制本批全部重修一遍。
    force_full_repair = bool(body.get("force_full_repair", False))

    scheme = await _load_scheme(db, scheme_id)
    if not scan_id:
        cur = await db.execute(
            "SELECT scan_id FROM consistency_conflicts WHERE scheme_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (scheme_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(422, "尚未扫描，请先执行全文一致性扫描")
        scan_id = row[0]

    conflicts = await _load_conflicts(db, scheme_id, scan_id)
    if not conflicts:
        raise HTTPException(422, "该扫描没有冲突记录，请重新扫描")

    # 重新构建上下文（事实/规范），供修复提示词使用
    facts = await scanner.build_global_facts_text(db, scheme_id)
    standards = scanner.build_standards_text(scheme["name"], scheme["type"])

    async def _progress(done: int, total: int, message: str):
        logger.info("[一致性修复] %s %d/%d", scheme_id, done, total)

    try:
        result = await repair_agent.run_repair(
            db, scheme_id=scheme_id, scan_id=scan_id, conflicts=conflicts,
            mode=mode, severity_threshold=severity_threshold,
            conflict_ids=conflict_ids,
            force_full_repair=force_full_repair,
            contexts={"global_facts": facts, "standards": standards},
            progress_cb=_progress)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("一致性修复失败")
        raise HTTPException(500, f"一致性修复失败：{e}")

    result["status"] = "completed"

    # ✅ G9（2026-09-21）：一致性修复改写了正文 → 原审核结论已失效。
    # 全库只有 review 路由写 sections.review_status，修复链路此前不重置，
    # 于是已修复的章节仍挂着 approved/reviewing，导出预检的 review 类问题也漏报。
    # 动作落在路由层（而非 repair_agent service 层），避免 service → router 的层级倒置。
    for _item in result.get("items") or []:
        if _item.get("status") == "repaired" and _item.get("section_id"):
            await reset_review_on_content_change(
                db, scheme_id, _item["section_id"],
                actor="一致性修复",
                comment="一致性修复已改写正文，原审核结论失效，请重新送审")
    await db.commit()
    return result


@router.get("/repair/{repair_id}")
async def get_repair_detail(scheme_id: str, repair_id: str, db=Depends(get_db)):
    rep = await repair_record.get_repair(db, repair_id)
    if not rep or rep.get("scheme_id") != scheme_id:
        raise HTTPException(404, "修复记录不存在")
    return rep


@router.post("/confirm")
async def confirm(scheme_id: str, body: dict | None = None, db=Depends(get_db)):
    """逐条确认修复结果。

    accepted[]：保留修复（冲突状态 accepted）。
    rejected[]：拒绝该冲突 —— 其所在章节整体恢复修复前快照原文，冲突回退 pending。
    未在两个列表中的冲突维持原状。
    """
    body = body or {}
    repair_id = body.get("repair_id", "")
    accepted = set(body.get("accepted") or [])
    rejected = set(body.get("rejected") or [])
    if not repair_id:
        raise HTTPException(422, "缺少 repair_id")

    rep = await repair_record.get_repair(db, repair_id)
    if not rep or rep.get("scheme_id") != scheme_id:
        raise HTTPException(404, "修复记录不存在")

    snap = None
    snap_map: dict = {}
    rejected_sections: set[str] = set()
    if rejected:
        snap = await repair_record.get_snapshot(db, rep.get("snapshot_id") or "")
        if snap:
            snap_map = {s.get("section_id"): s.get("content_before", "")
                        for s in snap.get("sections", [])}
        # ✅ 修复（2026-09-17）：rejected_sections 收集与恢复循环移到 if snap 之外，
        #    避免快照缺失（snapshot_id 为空/已被删）时 snap_map 未定义触发 NameError；
        #    此时 snap_map 为空，恢复分支自然跳过，仅回写冲突状态（降级为不恢复正文）。
        for item in rep.get("items", []):
            if item.get("conflict_id") in rejected and item.get("section_id"):
                rejected_sections.add(item["section_id"])
        for sid in rejected_sections:
            if sid in snap_map:
                    # ✅ BUG 修复（2026-09-17 · 与 repair_agent / rollback_snapshot
                    #    同类字数口径缺陷）：拒绝某冲突 → 该章整体恢复快照原文
                    #    时，旧实现仅写 content，word_count / word_status 停留在
                    #    **修复后** 的值 —— 例如修复把 1200 字改为 1500 字（记
                    #    over），用户拒绝后正文回到 1200 字但库里仍显示 1500 字
                    #    判 over，触发误导性压缩建议。这里按全项目唯一口径
                    #    （content_utils.text_word_count，剔除 ``` 图表代码块）
                    #    重算，并同步 word_status。
                    restored_content = snap_map[sid]
                    cur = await db.execute(
                        "SELECT word_budget FROM sections WHERE id=?", (sid,))
                    brow = await cur.fetchone()
                    wb = (brow[0] if brow and brow[0] else None) or DEFAULT_WORD_BUDGET
                    _wc = text_word_count(restored_content)
                    await db.execute(
                        "UPDATE sections SET content=?, word_count=?, "
                        "word_status=?, updated_at=? WHERE id=?",
                        (restored_content, _wc, word_status_for(_wc, wb),
                         datetime.now().isoformat(), sid))

    # 接受/拒绝的冲突项状态回写
    for cid in accepted:
        await db.execute(
            "UPDATE consistency_conflicts SET status='accepted' WHERE id=? AND scheme_id=?",
            (cid, scheme_id))
    for cid in rejected:
        await db.execute(
            "UPDATE consistency_conflicts SET status='pending' WHERE id=? AND scheme_id=?",
            (cid, scheme_id))

    all_decided = accepted | rejected

    # ✅ G9（2026-09-21）：拒绝某冲突会把该章正文整体恢复为修复前快照 ——
    # 正文已变，原审核结论同样失效，退回「待审核」并留痕。
    for sid in rejected_sections:
        await reset_review_on_content_change(
            db, scheme_id, sid, actor="一致性修复·拒绝回滚",
            comment="已拒绝该章节的一致性修复并恢复原文，原审核结论失效，请重新送审")
    repaired_ids = {i.get("conflict_id") for i in rep.get("items", [])
                    if i.get("status") == "repaired"}
    new_status = "confirmed"
    if repaired_ids and not repaired_ids.issubset(all_decided):
        new_status = "partially_confirmed"
    if rejected and not accepted:
        new_status = "rejected"
    await repair_record.mark_repair_status(db, repair_id, new_status)
    await db.commit()

    return {
        "repair_id": repair_id, "status": new_status,
        "accepted": sorted(accepted), "rejected": sorted(rejected),
        "restored_sections": sorted(rejected_sections),
    }


@router.post("/rollback")
async def rollback(scheme_id: str, body: dict | None = None, db=Depends(get_db)):
    body = body or {}
    snapshot_id = body.get("snapshot_id", "")
    repair_id = body.get("repair_id", "")
    if not snapshot_id and repair_id:
        rep = await repair_record.get_repair(db, repair_id)
        if rep and rep.get("scheme_id") == scheme_id:
            snapshot_id = rep.get("snapshot_id") or ""
    if not snapshot_id:
        raise HTTPException(422, "缺少 snapshot_id")

    # 校验快照归属
    snap = await repair_record.get_snapshot(db, snapshot_id)
    if not snap or snap.get("scheme_id") != scheme_id:
        raise HTTPException(404, "快照不存在")
    try:
        result = await repair_record.rollback_snapshot(db, snapshot_id)
    except KeyError:
        raise HTTPException(404, "快照不存在")

    # 回滚后把相关冲突退回 pending、修复批次标记 rolled_back
    cur = await db.execute(
        "SELECT id FROM consistency_repairs WHERE snapshot_id=?", (snapshot_id,))
    for r in await cur.fetchall():
        await repair_record.mark_repair_status(db, r[0], "rolled_back")

    # ✅ 修复（2026-09-17）：回滚只应影响「本快照对应的修复批次所触及的冲突」，
    #    旧实现 UPDATE 全方案所有 repaired/accepted 冲突，会把**其它批次**已确认/已
    #    修复的修复结果一并回退成 pending，造成跨批次污染（误撤回别人已接受的内容）。
    #    现从该快照的修复批次里收集 conflict_id，仅回退这些冲突。
    cur = await db.execute(
        "SELECT items FROM consistency_repairs WHERE snapshot_id=?", (snapshot_id,))
    conflict_ids: list[str] = []
    for r in await cur.fetchall():
        try:
            items = json.loads(r["items"] or "[]") if isinstance(r, dict) else json.loads(r[0] or "[]")
        except Exception:
            items = []
        for it in (items or []):
            cid = it.get("conflict_id") if isinstance(it, dict) else None
            if cid:
                conflict_ids.append(cid)
    conflict_ids = list(dict.fromkeys(conflict_ids))  # 去重保序
    if conflict_ids:
        placeholders = ",".join("?" * len(conflict_ids))
        await db.execute(
            f"UPDATE consistency_conflicts SET status='pending' "
            f"WHERE id IN ({placeholders})",
            conflict_ids)
    await db.commit()
    return {"status": "rolled_back", **result}


@router.get("/repairs")
async def list_repairs(scheme_id: str, limit: int = 20, db=Depends(get_db)):
    await _load_scheme(db, scheme_id)
    return {"items": await repair_record.list_repairs(db, scheme_id, limit)}


@router.get("/snapshots")
async def list_snapshots(scheme_id: str, limit: int = 20, db=Depends(get_db)):
    limit = max(1, min(limit, 50))
    cur = await db.execute(
        "SELECT id, type, created_at FROM scheme_snapshots WHERE scheme_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?", (scheme_id, limit))
    return {"items": [dict(r) for r in await cur.fetchall()]}
