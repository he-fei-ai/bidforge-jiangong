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

# ruff: noqa: I001 —— R13 契约：review_db 必须单独一行，护栏锁（R78 F-3 起本文件纳入收口范围）
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from app.db import get_db
from app.routers._chart_pipeline import resync_inline_charts
from app.routers.review import reset_review_on_content_change  # ✅ G9：修复改写正文→退回待审核
from app.services import conflict_arbiter as arbiter
from app.services import consistency_scanner as scanner
from app.services import repair_agent, repair_record

# ✅ R13 判空单一出口（2026-10-14 · R78 F-3）：本模块与 compliance/review/
#    review_autofix 同属「审核与预检」的两条修复闭环，此前是 R13 收口的漏网之鱼
#    （护栏 TARGET_FILES 只登记了三个 router）。全局单写连接 + aiosqlite 下
#    db.execute 可能返回 None：读路径直接 AttributeError 500；写路径（confirm 的
#    正文恢复 UPDATE / 冲突状态回写）不抛异常照常返回 confirmed —— 用户拒绝冲突
#    但正文没恢复的**假成功**。现全部改走 review_db 出口，静态护栏
#    tests/test_review_preflight_closeout_r77_20261014.py 禁止退回裸调用。
# noqa: I001 —— R13 契约：review_db 必须单独一行
from app.services import review_db  # noqa: I001
from app.services.content_utils import (
    DEFAULT_WORD_BUDGET,
    text_word_count,
    word_status_for,
)

logger = logging.getLogger("consistency_repair")

router = APIRouter(prefix="/api/v1/schemes/{scheme_id}/consistency", tags=["consistency_repair"])


async def _load_scheme(db, scheme_id: str) -> dict:
    row = await review_db.fetch_one(
        db, "SELECT id, project_id, name, type FROM schemes WHERE id=?", (scheme_id,), what="一致性修复：读取方案"
    )
    if not row:
        raise HTTPException(404, "方案不存在")
    return row


def _make_chart_resync(scheme_id: str):
    """✅ R92：给 services 层（repair_agent / repair_record）注入的图表重登记回调。

    返回 ``async (db, section_id, content) -> 校正后正文``：按传入正文全量重登记本章
    内联图表（先清后登、不 commit，由调用方同事务提交），并回传登记管线校验/修复后的
    正文（供调用方以校正后正文落库，使两侧逐字一致）。enforce_limits=False：修复/回滚
    是对既有正文的改写，不因程序级配图上限反向删图（与手动保存 B31 同口径）。
    """

    async def _resync(_db, _sid, _content):
        _n, _fixed = await resync_inline_charts(_db, scheme_id, _sid, _content, enforce_limits=False)
        return _fixed

    return _resync


async def _load_conflicts(db, scheme_id: str, scan_id: str) -> list[dict]:
    out = []
    for item in await review_db.fetch_all(
        db,
        "SELECT id, conflict_type, severity, topic, occurrences, authoritative_value,"
        " authoritative_source, repair_instruction, reason, status "
        "FROM consistency_conflicts WHERE scheme_id=? AND scan_id=? ORDER BY id",
        (scheme_id, scan_id),
        what="一致性修复：读取冲突清单",
    ):
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
        db,
        result["conflicts"],
        facts=result["contexts"].get("global_facts", ""),
        design_docs=result["contexts"].get("design_docs", ""),
        standards=result["contexts"].get("standards", ""),
        project_requirements=result["contexts"].get("project_docs", ""),
    )

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
        row = await review_db.fetch_one(
            db,
            "SELECT scan_id FROM consistency_conflicts WHERE scheme_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (scheme_id,),
            what="一致性冲突：定位最近一次扫描",
        )
        if not row:
            return {"exists": False, "conflicts": [], "summary": _summary([])}
        scan_id = row["scan_id"]
    conflicts = await _load_conflicts(db, scheme_id, scan_id)
    return {"exists": True, "scan_id": scan_id, "summary": _summary(conflicts), "conflicts": conflicts}


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
    # ✅ R98（R90 遗留⑦收口）：一致性修复整章改写 content——正文/目录生成在跑时
    #    与 _persist_section 无条件 UPDATE / 目录闸门整表重建互相覆盖。守卫接在
    #    任何 AI 调用之前（判据单一事实源在 routers.sections）。
    from app.routers.sections import ensure_no_generation_write

    ensure_no_generation_write(scheme_id, "全文一致性修复")
    if not scan_id:
        row = await review_db.fetch_one(
            db,
            "SELECT scan_id FROM consistency_conflicts WHERE scheme_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (scheme_id,),
            what="一致性修复：定位最近一次扫描",
        )
        if not row:
            raise HTTPException(422, "尚未扫描，请先执行全文一致性扫描")
        scan_id = row["scan_id"]

    conflicts = await _load_conflicts(db, scheme_id, scan_id)
    if not conflicts:
        raise HTTPException(422, "该扫描没有冲突记录，请重新扫描")

    # 重新构建上下文（事实/规范），供修复提示词使用
    facts = await scanner.build_global_facts_text(db, scheme_id)
    standards = scanner.build_standards_text(scheme["name"], scheme["type"])

    async def _progress(done: int, total: int, message: str):
        logger.info("[一致性修复] %s %d/%d", scheme_id, done, total)

    # ✅ R92（2026-10-17 · 正文写点同步补链）：一致性修复改写了 sections.content，
    #    须在同事务内把本章内联图表全量重登记（消除登记/正文两侧漂移）。回调在
    #    routers 层实现（services 不得 import routers），交给 repair_agent 在其写锁内
    #    调用；返回管线校正后的正文供落库。
    _resync_charts = _make_chart_resync(scheme_id)

    # ✅ R122-G2（引入易标「持久 Agent 整卷修复」的程序级等价物）：
    #    body.mode=="fullbook" 走整卷合并修复（少轮整卷往返 + 四道小节闸门）。
    #    开关 `consistency_fullbook_repair_enabled` 默认关闭 ⇒ 显式请求 fullbook 时
    #    返回 422（不默默降级）；默认 mode!="fullbook" 逐字节保持下方既有逐章链路。
    if mode == "fullbook":
        from app.config import settings as _settings

        if not bool(getattr(_settings, "consistency_fullbook_repair_enabled", False)):
            raise HTTPException(422, "整卷合并修复未启用（consistency_fullbook_repair_enabled=False）")
        try:
            from app.services import consistency_fullbook_repair as _fullbook

            result = await _fullbook.run_fullbook_repair(
                db,
                scheme_id=scheme_id,
                scan_id=scan_id,
                conflicts=conflicts,
                facts=facts,
                severity_threshold=severity_threshold,
                conflict_ids=conflict_ids,
                resync_fn=_resync_charts,
                progress_cb=_progress,
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("整卷合并修复失败")
            raise HTTPException(500, f"整卷合并修复失败：{e}")
        result["status"] = "completed"
        for _item in result.get("items") or []:
            if _item.get("status") == "repaired" and _item.get("section_id"):
                await reset_review_on_content_change(
                    db,
                    scheme_id,
                    _item["section_id"],
                    actor="整卷一致性修复",
                    comment="整卷一致性修复已改写正文，原审核结论失效，请重新送审",
                )
        await db.commit()
        return result

    try:
        result = await repair_agent.run_repair(
            db,
            scheme_id=scheme_id,
            scan_id=scan_id,
            conflicts=conflicts,
            mode=mode,
            severity_threshold=severity_threshold,
            conflict_ids=conflict_ids,
            force_full_repair=force_full_repair,
            contexts={"global_facts": facts, "standards": standards},
            progress_cb=_progress,
            resync_fn=_resync_charts,
        )
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
                db,
                scheme_id,
                _item["section_id"],
                actor="一致性修复",
                comment="一致性修复已改写正文，原审核结论失效，请重新送审",
            )
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
    # ✅ R179（E4 残项）：恢复结果三态记账（恢复成功 / 快照缺失跳过 / 未涉及）。
    restored_sections: set[str] = set()
    restore_skipped_sections: set[str] = set()
    if rejected:
        # ✅ R98（R90 遗留⑦收口）：拒绝会把整章正文恢复为修复前快照 —— 与
        #    生成中正文互相覆盖，仅在真有拒绝项（写正文）时拦；纯 accepted
        #    只写冲突状态、不触碰 content，不拦。
        from app.routers.sections import ensure_no_generation_write

        ensure_no_generation_write(scheme_id, "一致性修复拒绝恢复")
        snap = await repair_record.get_snapshot(db, rep.get("snapshot_id") or "")
        if snap:
            snap_map = {s.get("section_id"): s.get("content_before", "") for s in snap.get("sections", [])}
        # ✅ 修复（2026-09-17）：rejected_sections 收集与恢复循环移到 if snap 之外，
        #    避免快照缺失（snapshot_id 为空/已被删）时 snap_map 未定义触发 NameError；
        #    此时 snap_map 为空，恢复分支自然跳过，仅回写冲突状态（降级为不恢复正文）。
        for item in rep.get("items", []):
            if item.get("conflict_id") in rejected and item.get("section_id"):
                rejected_sections.add(item["section_id"])
        # ✅ R179（E4 残项）：区分「真的恢复了」与「因快照缺失只回写了冲突状态」。
        #    旧实现把 rejected_sections 全量当 restored_sections 返回，快照丢失时
        #    前端会提示“已恢复原文”而正文其实没动，用户据此误判可放心导出。
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
                brow = await review_db.fetch_one(
                    db,
                    "SELECT word_budget FROM sections WHERE id=?",
                    (sid,),
                    what=f"一致性修复确认：读取章节字数预算（section={sid[:8]}）",
                )
                wb = (brow["word_budget"] if brow and brow["word_budget"] else None) or DEFAULT_WORD_BUDGET
                _wc = text_word_count(restored_content)
                # ✅ R13（R78 F-3）：正文恢复写经 exec_write（严格模式）——
                #    rejected_sections 均来自本批次 items 且章节刚被修复写过，
                #    行必然存在，0 行只可能是「本次写没生效」，绝不汇报假成功。
                await review_db.exec_write(
                    db,
                    "UPDATE sections SET content=?, word_count=?, " "word_status=?, updated_at=? WHERE id=?",
                    (restored_content, _wc, word_status_for(_wc, wb), datetime.now().isoformat(), sid),
                    what=f"一致性修复确认：恢复章节快照原文（section={sid[:8]}）",
                )
                # ✅ R92（2026-10-17 · 正文写点同步补链）：拒绝 = 该章整体回退到
                #    修复前快照原文，正文里的内联图表随之复原；须在**同一事务**内
                #    （commit 在函数末尾）按恢复后正文全量重登记，否则 chart_predictions
                #    仍是修复时那一份（run_repair 已重登记过）→ 与恢复后正文再度漂移。
                try:
                    await resync_inline_charts(db, scheme_id, sid, restored_content, enforce_limits=False)
                except Exception as _re:  # noqa: BLE001 - 登记失败不阻断恢复
                    logger.warning("一致性修复确认：章节 %s 图表重登记失败（保留恢复正文）: %s", sid[:8], _re)
                restored_sections.add(sid)
            else:
                # 快照缺失：只回写了冲突状态，正文未变 —— 单独记账，不再冒充已恢复。
                restore_skipped_sections.add(sid)

    # 接受/拒绝的冲突项状态回写
    # ✅ R13（R78 F-3）：cid 来自请求体，未预先校验行存在（历史批次/外部调用
    #    可能指向已不存在的冲突）→「0 行」是合法结果，require_rows=False，
    #    与 _persist_fixed 记账回填同口径；rowcount 由日志侧可查。
    for cid in accepted:
        await review_db.exec_write(
            db,
            "UPDATE consistency_conflicts SET status='accepted' WHERE id=? AND scheme_id=?",
            (cid, scheme_id),
            what="一致性修复确认：回写 accepted 状态",
            require_rows=False,
        )
    for cid in rejected:
        await review_db.exec_write(
            db,
            "UPDATE consistency_conflicts SET status='pending' WHERE id=? AND scheme_id=?",
            (cid, scheme_id),
            what="一致性修复确认：回写 rejected→pending 状态",
            require_rows=False,
        )

    all_decided = accepted | rejected

    # ✅ R179（E4）：只对**确实回滚了正文**的章节重置审核结论；快照缺失的章节
    #    正文未变，重置审核会凭空制造「已重新送审」的假状态。
    for sid in restored_sections:
        await reset_review_on_content_change(
            db,
            scheme_id,
            sid,
            actor="一致性修复·拒绝回滚",
            comment="已拒绝该章节的一致性修复并恢复原文，原审核结论失效，请重新送审",
        )
    repaired_ids = {i.get("conflict_id") for i in rep.get("items", []) if i.get("status") == "repaired"}
    new_status = "confirmed"
    if repaired_ids and not repaired_ids.issubset(all_decided):
        new_status = "partially_confirmed"
    if rejected and not accepted:
        new_status = "rejected"
    await repair_record.mark_repair_status(db, repair_id, new_status)
    await db.commit()

    # ✅ E1（R175，闭环·低风险版）：拒绝会把整章正文恢复为快照原文 —— 正文已变，
    #    旧的扫描缓存必须失效，否则下一轮扫描可能拿回基于修复后正文的旧结论。
    #    这里只做缓存失效 + 明确告知「需重扫」，不在确认请求内同步跑 AI 扫描 /
    #    仲裁（那会把确认接口拖到分钟级，并产生用户未预期的额外调用成本）。
    rescan_required = bool(restored_sections)
    if rescan_required:
        try:
            from app.routers.sections import invalidate_consistency_scan_cache

            await invalidate_consistency_scan_cache(db, scheme_id)
        except Exception as e:  # noqa: BLE001 - 缓存失效失败不阻断确认
            logger.warning("一致性修复确认：扫描缓存失效失败（不影响确认）: %s", e)

    return {
        "repair_id": repair_id,
        "status": new_status,
        "accepted": sorted(accepted),
        "rejected": sorted(rejected),
        "restored_sections": sorted(restored_sections),
        # ✅ R179（加法式）：快照缺失导致未真正回滚的章节，明确告知调用方，
        #    避免前端把「只改了冲突状态」误报成「正文已恢复」。
        "restore_skipped_sections": sorted(restore_skipped_sections),
        # ✅ E1（加法式）：true 表示正文已回滚 → 请重新扫描再判定结论；
        #    旧消费方忽略该键即可，既有字段语义不变。
        "rescan_required": rescan_required,
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
    # ✅ R98（R90 遗留⑦收口）：回滚整章恢复修复前正文，生成在跑时互相覆盖。
    from app.routers.sections import ensure_no_generation_write

    ensure_no_generation_write(scheme_id, "一致性修复回滚")
    try:
        result = await repair_record.rollback_snapshot(db, snapshot_id, resync_fn=_make_chart_resync(scheme_id))
    except KeyError:
        raise HTTPException(404, "快照不存在")

    # 回滚后把相关冲突退回 pending、修复批次标记 rolled_back
    for r in await review_db.fetch_all(
        db,
        "SELECT id FROM consistency_repairs WHERE snapshot_id=?",
        (snapshot_id,),
        what="一致性修复回滚：定位修复批次",
    ):
        await repair_record.mark_repair_status(db, r["id"], "rolled_back")

    # ✅ 修复（2026-09-17）：回滚只应影响「本快照对应的修复批次所触及的冲突」，
    #    旧实现 UPDATE 全方案所有 repaired/accepted 冲突，会把**其它批次**已确认/已
    #    修复的修复结果一并回退成 pending，造成跨批次污染（误撤回别人已接受的内容）。
    #    现从该快照的修复批次里收集 conflict_id，仅回退这些冲突。
    rows = await review_db.fetch_all(
        db,
        "SELECT items FROM consistency_repairs WHERE snapshot_id=?",
        (snapshot_id,),
        what="一致性修复回滚：读取批次冲突清单",
    )
    conflict_ids: list[str] = []
    for r in rows:
        try:
            items = json.loads(r["items"] or "[]")
        except Exception:
            items = []
        for it in items or []:
            cid = it.get("conflict_id") if isinstance(it, dict) else None
            if cid:
                conflict_ids.append(cid)
    conflict_ids = list(dict.fromkeys(conflict_ids))  # 去重保序
    if conflict_ids:
        placeholders = ",".join("?" * len(conflict_ids))
        # ✅ R13 + R78 F-3 加固：补 `AND scheme_id=?` —— P0（2026-10-04）后
        #    冲突 ID 自带方案作用域前缀、结构上不互撞，但**历史无前缀行**
        #    （C001…形态）仍可能与其他方案同名；回滚是本模块唯一一条
        #    不带 scheme 约束的状态写，加上即杜绝跨方案误伤，本方案行零影响。
        await review_db.exec_write(
            db,
            f"UPDATE consistency_conflicts SET status='pending' " f"WHERE id IN ({placeholders}) AND scheme_id=?",
            conflict_ids + [scheme_id],
            what="一致性修复回滚：冲突退回 pending",
            require_rows=False,
        )
    await db.commit()
    return {"status": "rolled_back", **result}


@router.get("/repairs")
async def list_repairs(scheme_id: str, limit: int = 20, db=Depends(get_db)):
    await _load_scheme(db, scheme_id)
    return {"items": await repair_record.list_repairs(db, scheme_id, limit)}


@router.get("/snapshots")
async def list_snapshots(scheme_id: str, limit: int = 20, db=Depends(get_db)):
    limit = max(1, min(limit, 50))
    return {
        "items": await review_db.fetch_all(
            db,
            "SELECT id, type, created_at FROM scheme_snapshots WHERE scheme_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (scheme_id, limit),
            what="一致性修复：读取快照列表",
        )
    }
