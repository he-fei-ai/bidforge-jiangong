"""审核与预检 · 问题定向自动修复路由
端点（前缀 ``/api/v1/schemes/{scheme_id}/review/autofix``）：
- ``GET  /capabilities``  全部规则的修复能力表（前端按钮的唯一判据）
- ``POST /plan``          给定 ``rule_id`` → **定位矛盾位置**（章节 + 行号 + 原文），不调 AI
- ``POST /apply``         执行修复：定位 → 改写 → 校验 → 落库（可回滚）
- ``POST /rollback``      按快照一键回滚
为什么必须由服务端重新派生 finding
----------------------------------
前端只传 ``rule_id``（+ 可选 ``section_id``），**不接受**前端回传的
``detail`` / ``evidence`` / ``suggestion``：这些字段会原样进入 AI 提示词，
若由客户端提供，等于开了一条「任意文本 → AI 提示词」的注入通道。
服务端用 ``compliance._readiness_overview_compute`` 的同一套口径重新算出
findings 再取那一条，保证「用户点的那条」与「系统判的那条」逐字一致。
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response

from app.db import get_db
from app.routers.review import reset_review_on_content_change
from app.services import review_autofix
from app.services.audit_rules import active_rules
# ✅ R13 判空单一出口（2026-10-06）：静态护栏 tests/test_review_r13_closeout_20261006.py
#    禁止本文件重新出现裸 db.execute。最关键的一处是 _persist_fixed 的正文 UPDATE ——
#    旧实现丢弃返回值，写没生效时仍返回快照 id，界面报「已修复」而正文未变。
from app.services import review_db

logger = logging.getLogger("review_autofix")
router = APIRouter(
    prefix="/api/v1/schemes/{scheme_id}/review/autofix",
    tags=["review"])

async def _load_scheme(db, scheme_id: str) -> dict:
    row = await review_db.fetch_one(
        db, "SELECT id, project_id, name, type FROM schemes WHERE id=?", (scheme_id,),
        what="自动修复：读取方案")
    if not row:
        raise HTTPException(404, "方案不存在")
    return row

async def _load_sections(db, scheme_id: str) -> list[dict]:
    """载入章节（id/title/content），按目录树前序 DFS 排序（与预检同口径）。"""
    from app.services.content_utils import order_sections_dfs
    rows = await review_db.fetch_all(
        db,
        "SELECT id, parent_id, title, content, word_count, level, status, sort_order"
        " FROM sections WHERE scheme_id=? ORDER BY sort_order", (scheme_id,),
        what="自动修复：读取章节")
    return order_sections_dfs(rows)

async def _resolve_finding(db, scheme_id: str, rule_id: str,
                           section_id: str = "") -> dict:
    """按 rule_id 从**当前总检口径**重新派生那一条 finding。
    复用 ``compliance._readiness_overview_compute``（与总检页完全同一套聚合
    逻辑），不另写一份解析 —— 否则两处分叉就会出现「界面显示可修、后端说
    找不到」。``section_id`` 命中不到时**不**静默放宽：同规则可能涉及多章，
    悄悄改到别的章比直接报错危险得多。
    """
    from app.routers.compliance import _readiness_overview_compute
    # persist=False（2026-10-03 数据链收口）：修复链路重算只取 findings，
    # 不得落 preflight_runs —— 否则每点一次定位/修复都污染分数趋势。
    payload = await _readiness_overview_compute(db, scheme_id, persist=False)
    hits = [f for f in (payload.get("findings") or [])
            if (f.get("rule_id") or "") == (rule_id or "")]
    if section_id:
        hits = [f for f in hits if (f.get("section_id") or "") == section_id]
    if not hits:
        raise HTTPException(
            404, f"当前检查结果中不存在问题 {rule_id}"
                 + (f"（章节 {section_id}）" if section_id else "")
                 + "，请先重新执行「一键总检」")
    return hits[0]

# @deprecated 孤儿 API（2026-10-06 核实）：前端**零消费** —— 可修复性是随
# /overview 的 findings[].autofix 逐条下发的（同一 capability_of 单一来源，
# 不存在与本表分叉的可能），AutoFixModal 读的是 finding.autofix 而非本端点。
# 端点保留供脚本/CI 直接读全量能力表（48 条规则 × mode/reason），
# 数据被 /overview 覆盖，故按仓库惯例挂 deprecated（同 compliance /preflight）。
@router.get("/capabilities", deprecated=True)
async def capabilities(scheme_id: str = "", db=Depends(get_db),
                       response: Response = None):
    """全部规则的自动修复能力表（脚本/诊断用；UI 判据走 findings[].autofix）。"""
    from app.routers.compliance import _apply_deprecation_headers
    _apply_deprecation_headers(
        response,
        replacement="/api/v1/compliance/overview/{scheme_id}（findings[].autofix）",
    )
    items = []
    for rule in active_rules():
        cap = review_autofix.capability_of(rule.rule_id)
        items.append({
            "rule_id": rule.rule_id, "title": rule.title,
            "dimension": rule.dimension, "severity": rule.severity,
            "mode": cap.mode,
            "fixable": cap.mode in (review_autofix.FIX_MODE_AUTO,
                                    review_autofix.FIX_MODE_AI),
            "reason": cap.reason,
        })
    return {"items": items, "total": len(items)}

@router.post("/plan")
async def plan(scheme_id: str, body: dict | None = None, db=Depends(get_db)):
    """定位矛盾位置（**不调 AI、不落库**，纯只读预览）。
    前端在真正修复前先展示「问题出在哪一章第几行、原文是什么」，
    让用户确认后再决定是否花一次 AI 调用。
    """
    body = body or {}
    rule_id = str(body.get("rule_id") or "").strip()
    if not rule_id:
        raise HTTPException(422, "缺少 rule_id")
    await _load_scheme(db, scheme_id)
    finding = await _resolve_finding(db, scheme_id, rule_id,
                                     str(body.get("section_id") or ""))
    cap = review_autofix.capability_of(rule_id)
    if cap.mode == review_autofix.FIX_MODE_MANUAL:
        return {"ok": False, "fixable": False, "mode": cap.mode,
                "reason": cap.reason, "finding": finding, "targets": []}
    sections = await _load_sections(db, scheme_id)
    targets = review_autofix.locate_targets(finding, sections)
    return {
        "ok": bool(targets), "fixable": bool(targets), "mode": cap.mode,
        "reason": "" if targets else
                  "未能在正文中定位到该问题的具体位置，为避免无依据改写正文，"
                  "请按整改建议人工处理。",
        "finding": finding, "targets": targets,
        "max_sections": review_autofix.AUTOFIX_MAX_SECTIONS,
    }

async def _persist_fixed(db, *, scheme_id: str, rule_id: str,
                        pending: list, repair_id: str = "") -> str:
    """把校验通过的修复结果落库，返回快照 id（回滚凭据）。
    口径与人工编辑 / 一致性修复完全一致（复用同一批唯一实现）：
    ``word_count`` / ``word_status`` 用 ``content_utils`` 唯一口径重算；
    审核状态经 ``reset_review_on_content_change`` 退回待审核
    （改写正文后原审核结论必然失效）；修复前存快照。
    ⚠️ 本函数位于 **routers 层**（``services`` 不得 import routers，
    见 ``test_outline_name_line_20260927::test_services_never_import_routers``）。
    """
    from app.routers.sections import invalidate_consistency_scan_cache
    from app.services import repair_record
    from app.services.content_utils import text_word_count, word_status_for
    # 修复前快照：回滚的唯一凭据（回滚本身也会再存一份撤销快照）
    snapshot_id = await repair_record.create_snapshot(
        db, scheme_id,
        [{"section_id": sid, "content_before": before} for sid, before, _ in pending],
        snapshot_type="review_autofix")
    for sid, _before, after in pending:
        row = await review_db.fetch_one(
            db, "SELECT word_budget FROM sections WHERE id=?", (sid,),
            what=f"自动修复落库：读取章节字数预算（section={sid[:8]}）")
        budget = (row["word_budget"] if row else None) or 1500
        wc = text_word_count(after)
        # ✅ R13（2026-10-06）：旧实现丢弃返回值 —— 正文 UPDATE 返回 None 时
        #    循环照常走完、commit 成功、函数返回快照 id，端点据此返回 ok=True。
        #    用户看到「已修复」+「章节审核结论已自动退回待审核」，而**正文一字未改**，
        #    且审核状态被真的退回了（半生效比不生效更糟：结论与正文彻底脱节）。
        await review_db.exec_write(
            db,
            "UPDATE sections SET content=?, word_count=?, word_status=?,"
            " updated_at=? WHERE id=?",
            (after, wc, word_status_for(wc, budget),
             datetime.now().isoformat(), sid),
            what=f"自动修复落库：写入正文（section={sid[:8]}）")
        await reset_review_on_content_change(
            db, scheme_id, sid, actor="审核预检自动修复",
            comment=f"按问题 {rule_id} 自动修复正文，原审核结论失效，请重新送审")
    # 正文已变 → 一致性扫描缓存的 content_hash 自动失效；标题亦可能随补充
    # 内容变化，按结构变更口径再清一次更保险（按 scheme 隔离，不误清其它方案）
    await invalidate_consistency_scan_cache(db, scheme_id)
    if repair_id:
        # 回填快照 id 到留痕行（回滚端点据此定位批次）。
        # ⚠️ 刻意用 require_rows=False（与同函数正文 UPDATE 的严格模式不同）：
        #    这一笔是**记账回填**，不是修复本体。若留痕行恰好不存在，此时抛 503
        #    会把已写入的正文一并回滚（get_db 的 finally 会 rollback）—— 用一个
        #    记账问题否掉一次真实修复，比留一行待补的快照更糟。此处只记 WARNING，
        #    运维可据此发现「修复成功但无法回滚」的批次。
        await review_db.exec_write(
            db,
            "UPDATE consistency_repairs SET snapshot_id=? WHERE id=?",
            (snapshot_id, repair_id), what="自动修复：回填快照 id 到留痕行",
            require_rows=False)
    await db.commit()
    logger.info("审核预检自动修复 rule=%s：已落库 %d 章（快照 %s）",
                rule_id, len(pending), snapshot_id)
    return snapshot_id

@router.post("/apply")
async def apply(scheme_id: str, body: dict | None = None, db=Depends(get_db)):
    """执行修复：定位 → 改写 → 校验 → 落库（失败保留原文，可回滚）。"""
    body = body or {}
    rule_id = str(body.get("rule_id") or "").strip()
    if not rule_id:
        raise HTTPException(422, "缺少 rule_id")
    scheme = await _load_scheme(db, scheme_id)
    finding = await _resolve_finding(db, scheme_id, rule_id,
                                     str(body.get("section_id") or ""))
    sections = await _load_sections(db, scheme_id)
    # AI 改写的依据：全局事实（数值权威值）+ 现行标准清单（编号权威值）
    facts = ""
    standards_text = ""
    try:
        from app.services.consistency_scanner import build_global_facts_text, build_standards_text
        facts = await build_global_facts_text(db, scheme_id, limit=3000)
        standards_text = build_standards_text(scheme.get("name") or "",
                                              scheme.get("type") or "")
    except Exception as e:  # 依据缺失只降级，不阻断修复
        logger.warning("自动修复：构建事实 / 标准依据失败（降级为空）: %s", e)
    res = await review_autofix.apply_fix(
        db, scheme_id=scheme_id, finding=finding, sections=sections,
        scheme=scheme, facts=facts, standards_text=standards_text)
    pending = res.pop("pending", []) or []
    if pending:
        res["snapshot_id"] = await _persist_fixed(
            db, scheme_id=scheme_id, rule_id=rule_id, pending=pending,
            repair_id=res.get("repair_id", ""))
    return res

@router.post("/rollback")
async def rollback(scheme_id: str, body: dict | None = None, db=Depends(get_db)):
    """按快照一键回滚自动修复（只影响该快照涉及的章节）。"""
    from app.services import repair_record
    body = body or {}
    snapshot_id = str(body.get("snapshot_id") or "")
    if not snapshot_id:
        raise HTTPException(422, "缺少 snapshot_id")
    snap = await repair_record.get_snapshot(db, snapshot_id)
    if not snap or snap.get("scheme_id") != scheme_id:
        raise HTTPException(404, "快照不存在")
    try:
        result = await repair_record.rollback_snapshot(
            db, snapshot_id, undo_type="review_autofix_rollback")
    except KeyError:
        raise HTTPException(404, "快照不存在")
    # 回滚同样改写正文 → 审核结论再次失效，退回待审核并留痕
    for sec in snap.get("sections") or []:
        await reset_review_on_content_change(
            db, scheme_id, sec.get("section_id") or "",
            actor="审核预检自动修复·回滚",
            comment="已回滚自动修复内容，恢复修复前正文，请重新送审")
    await db.commit()
    # 修复批次状态同步（与一致性修复的 rollback 同口径）
    rows = await review_db.fetch_all(
        db, "SELECT id FROM consistency_repairs WHERE snapshot_id=? AND scheme_id=?",
        (snapshot_id, scheme_id), what="自动修复回滚：定位待标记的修复批次")
    for r in rows:
        await repair_record.mark_repair_status(db, r["id"], "rolled_back")
    return {"status": "rolled_back", **result}

@router.get("/repairs")
async def list_repairs(scheme_id: str, limit: int = 20, db=Depends(get_db)):
    """自动修复批次历史（复用一致性修复的记录表，按 mode 过滤）。

    ⚠️ 前端**零消费**（2026-10-06 核实）：BatchFixModal 展示的是 confirm
    响应里的逐条 status，不回读历史。本端点是修复历史的**唯一**数据出口
    （/overview 不含批次列表），故**不挂 deprecated**，保留给脚本 / 诊断 /
    未来的「修复历史」面板直接使用。
    """
    from app.services import repair_record
    await _load_scheme(db, scheme_id)
    items = await repair_record.list_repairs(db, scheme_id, limit)
    return {"items": [i for i in items if i.get("mode") in ("review_autofix", "review_autofix_batch")]}
# ---------------------------------------------------------------------------
# 批量：发现收集（只读）→ 暂存（定位/改写/校验，不落库）→ 确认（落库/回滚）
# ---------------------------------------------------------------------------
def _severity_rank(sev: str) -> int:
    return {"block": 0, "high": 1, "medium": 2, "low": 3}.get(sev or "", 9)

def _merged_after_if_prefix(items: list[dict], accepted_ids: set) -> str | None:
    """同章多条问题时，若接受集合恰为链式前缀，取**最后一条被接受项**的 after。
    ⚠️ 修复（2026-10-01）：旧实现只要「接受集合是前缀」就返回
    ``ordered[-1].after``，即**最后一条（无论是否被接受）**的 after。
    后果：接受子集 ``{A}`` 时返回了 ``B`` 改写后的正文 —— **用户只接受了 A，
    却静默写入了 B 的修改**。而 B 未被接受恰恰意味着 B 的内容仍待确认（可能是
    风险较高、需要人工判断的修复）。这是「确认」环节最不该出现的越权写入。
    现在语义为：按 ``chain_index`` 排序后，接受集合必须是前缀（否则返回 None
    触发重算），且只取**前缀末尾那条**的 after。
    """
    ordered = sorted(items, key=lambda x: x.get("chain_index", 0))
    if not ordered:
        return None
    # 接受集合必须是前缀：一旦出现「未接受项之后还有被接受项」即非前缀
    seen_rejected = False
    last_accepted_after = None
    for it in ordered:
        if it.get("rule_id") in accepted_ids:
            if seen_rejected:
                return None      # 非前缀 → 交由调用方重新链式改写
            last_accepted_after = it.get("after")
        else:
            seen_rejected = True
    return last_accepted_after

async def _build_facts(db, scheme: dict) -> tuple[str, str]:
    """构建自动修复的 AI 依据：全局事实（数值权威值）+ 现行标准清单。"""
    facts = ""
    standards_text = ""
    try:
        from app.services.consistency_scanner import build_global_facts_text, build_standards_text
        facts = await build_global_facts_text(db, scheme.get("id") or "", limit=3000)
        standards_text = build_standards_text(
            scheme.get("name") or "", scheme.get("type") or "")
    except Exception as e:  # 依据缺失只降级，不阻断修复
        logger.warning("自动修复：构建事实 / 标准依据失败（降级为空）: %s", e)
    return facts, standards_text

@router.post("/collect")
async def collect(scheme_id: str, body: dict | None = None, db=Depends(get_db)):
    """只读收集当前总检的全部问题，标注自动修复能力 + 定位预览。
    不调 AI、不写总检历史（重算 persist=False，2026-10-03 收口：旧实现虽
    声明「不落库」实则每次重算都往 preflight_runs 灌一条分数趋势）。
    供前端「一键修复全部阻断项」前的预览与勾选。
    ``scope``：``all_blocking``（默认，仅阻断项且可自动修复）/
    ``auto_fixable``（所有可自动修复）/ ``all``（含人工项，供展示）。
    """
    from app.routers.compliance import _readiness_overview_compute
    from app.services import review_autofix
    body = body or {}
    scope = str(body.get("scope") or "all_blocking").strip()
    await _load_scheme(db, scheme_id)
    # persist=False（2026-10-03 数据链收口）：修复链路重算只取 findings，
    # 不得落 preflight_runs —— 否则每点一次定位/修复都污染分数趋势。
    payload = await _readiness_overview_compute(db, scheme_id, persist=False)
    findings = payload.get("findings") or []
    review_autofix.capability_summary(findings)
    if scope == "all_blocking":
        sel = [f for f in findings
               if (f.get("severity") == "block")
               and (f.get("autofix") or {}).get("fixable")]
    elif scope == "auto_fixable":
        sel = [f for f in findings if (f.get("autofix") or {}).get("fixable")]
    else:
        sel = findings
    sections = await _load_sections(db, scheme_id)
    for f in sel:
        if (f.get("autofix") or {}).get("fixable"):
            f["targets"] = review_autofix.locate_targets(f, sections)
        else:
            f["targets"] = []
    return {
        "scheme_id": scheme_id, "scope": scope, "total": len(sel),
        "items": sel,
        "content_fingerprint": payload.get("content_fingerprint"),
        "stale": payload.get("stale", False),
    }

@router.post("/stage")
async def stage(scheme_id: str, body: dict | None = None, db=Depends(get_db)):
    """批量定位 + 改写 + 校验，暂存为一条 review_autofix_batch 记录（**不落库**）。
    入参 ``rule_ids``（显式选定）或 ``scope``（默认 all_blocking）。仅处理
    auto/ai 模式（manual 项提示原因）。落库由 ``/confirm`` 完成，支持逐条/批量
    接受（落库 + 联动）或拒绝（丢弃 / 回滚）。
    """
    from app.routers.compliance import _readiness_overview_compute
    from app.services import review_autofix
    body = body or {}
    rule_ids = body.get("rule_ids") or []
    scope = str(body.get("scope") or "all_blocking").strip()
    scheme = await _load_scheme(db, scheme_id)
    # persist=False（2026-10-03 数据链收口）：修复链路重算只取 findings，
    # 不得落 preflight_runs —— 否则每点一次定位/修复都污染分数趋势。
    payload = await _readiness_overview_compute(db, scheme_id, persist=False)
    findings = payload.get("findings") or []
    if rule_ids:
        want = set(rule_ids)
        findings = [f for f in findings if (f.get("rule_id") or "") in want]
    elif scope == "all_blocking":
        findings = [f for f in findings if f.get("severity") == "block"]
    elif scope == "auto_fixable":
        findings = [f for f in findings
                    if review_autofix.capability_of(f.get("rule_id") or "").mode
                    in (review_autofix.FIX_MODE_AUTO, review_autofix.FIX_MODE_AI)]
    else:
        findings = list(findings)
    # 仅 auto/ai 可暂存
    findings = [f for f in findings
                if review_autofix.capability_of(f.get("rule_id") or "").mode
                in (review_autofix.FIX_MODE_AUTO, review_autofix.FIX_MODE_AI)]
    if not findings:
        return {"batch_id": "", "items": [],
                "stats": {"repaired": 0, "failed": 0, "skipped": 0},
                "status": "empty", "reason": "当前范围内没有可自动修复的问题"}
    sections = await _load_sections(db, scheme_id)
    facts, standards_text = await _build_facts(db, scheme)
    res = await review_autofix.stage_fixes(
        db, scheme_id=scheme_id, findings=findings, sections=sections,
        scheme=scheme, facts=facts, standards_text=standards_text)
    return res

@router.post("/confirm")
async def confirm(scheme_id: str, body: dict | None = None, db=Depends(get_db)):
    """逐条/批量 接受（落库 + 审核退回 + 缓存失效）或 拒绝（丢弃/回滚）。
    入参：``batch_id``（/stage 返回）+ 下列之一：
    - ``accept_all=true``：接受全部已修复项（默认，未给 accept/reject 时同此）；
    - ``accept=[rule_id...]``：仅接受指定项；
    - ``reject=[rule_id...]``：拒绝指定项（其余已修复项接受）。
    同章多条问题时，非前缀式「拒绝中间某条」会重新链式改写以保证合并正确。
    """
    from app.services import repair_record, review_autofix
    body = body or {}
    batch_id = str(body.get("batch_id") or "").strip()
    if not batch_id:
        raise HTTPException(422, "缺少 batch_id")
    batch = await repair_record.get_repair(db, batch_id)
    if not batch or batch.get("scheme_id") != scheme_id \
            or batch.get("mode") != "review_autofix_batch":
        raise HTTPException(404, "暂存批次不存在")
    items = batch.get("items") or []
    repaired_ids = {it.get("rule_id") for it in items if it.get("status") == "repaired"}
    accept = body.get("accept")
    reject = body.get("reject")
    accept_all = bool(body.get("accept_all"))
    if accept_all or (not accept and not reject):
        accepted_ids = set(repaired_ids)
    elif accept is not None:
        accepted_ids = set(accept) & repaired_ids
    else:  # 仅给了 reject
        accepted_ids = set(repaired_ids) - set(reject or [])
    if not accepted_ids:
        await repair_record.mark_repair_status(db, batch_id, "rejected")
        return {"status": "rejected", "accepted": 0, "repaired_sections": 0,
                "snapshot_id": "", "batch_id": batch_id}
    by_section: dict[str, list[dict]] = {}
    for it in items:
        if it.get("rule_id") in accepted_ids and it.get("status") == "repaired":
            by_section.setdefault(it.get("section_id"), []).append(it)
    #: 失效 finding 的跳过明细（供运维审计与前端提示）。
    #:
    #: ✅ BUG 修复（2026-10-04 · R39 顺带收口）：旧实现在**循环体内**才
    #:    ``skipped = []`` 初始化，而函数末尾无条件读它 —— 当
    #:    ``by_section`` 为空、或每个 section 都走 ``merged is not None`` /
    #:    ``if not resolved: continue`` 这些**不经过初始化点**的分支时，
    #:    ``UnboundLocalError: cannot access local variable 'skipped'`` →
    #:    批量确认接口直接 500。
    #:   实测（``test_review_autofix_batch_20261001.py`` 两条用例）：
    #:   「全部接受」与「部分接受」两条**主干**流程都必崩。
    #:   提到循环外初始化，与 ``resolved`` 的作用域对齐；默认值保持空列表，
    #:   **返回体结构逐字不变**（旧行为是抛异常，不存在需要兼容的旧取值）。
    skipped: list[dict] = []
    sections = await _load_sections(db, scheme_id)
    sec_by_id = {s.get("id"): s for s in sections}
    pending: list[tuple[str, str, str]] = []
    # 罕见路径：非前缀拒绝 → 重新链式改写（需再调 AI）
    for sid, its in by_section.items():
        section = sec_by_id.get(sid)
        if not section:
            continue
        merged = _merged_after_if_prefix(its, accepted_ids)
        if merged is not None:
            pending.append((sid, section.get("content") or "", merged))
            continue
        rule_ids = [it.get("rule_id") for it in its
                    if it.get("rule_id") in accepted_ids]
        resolved = []
        for rid in rule_ids:
            try:
                resolved.append(await _resolve_finding(db, scheme_id, rid))
            except HTTPException as e:
                # 失效 finding 静默跳过会丢失可观测性：用户以为修了、实际没修
                # 记 WARNING 含 rule_id 与错误码，供运维审计与前端提示
                logger.warning(
                    "confirm 链式重算跳过失效 finding（rule_id=%s, status=%s, detail=%s）",
                    rid, e.status_code, str(e.detail)[:200])
                skipped.append({"rule_id": rid, "status": e.status_code, "detail": str(e.detail)[:200]})
        if not resolved:
            continue
        resolved = sorted(resolved, key=lambda x: _severity_rank(x.get("severity")))
        scheme = await _load_scheme(db, scheme_id)
        facts, standards_text = await _build_facts(db, scheme)
        _content, _items = await review_autofix._chain_section_fixes(
            section, resolved, scheme, facts, standards_text)
        pending.append((sid, section.get("content") or "", _content))
    snapshot_id = ""
    if pending:
        snapshot_id = await _persist_fixed(
            db, scheme_id=scheme_id, rule_id="batch", pending=pending,
            repair_id=batch_id)
    await repair_record.mark_repair_status(db, batch_id, "confirmed")
    return {
        "status": "confirmed", "accepted": len(accepted_ids),
        "repaired_sections": len(pending), "snapshot_id": snapshot_id,
        "batch_id": batch_id,
        "skipped": skipped,
    }
