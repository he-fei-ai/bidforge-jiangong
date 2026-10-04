"""方案 / 章节审核工作流路由（PRD §3.12.5）

背景
----
``sections.review_status`` 字段在库表与 Pydantic 模型里都存在，
但**全库无任何写入方** —— 是一个事实上的死字段，PRD 规划的
``pending → reviewing → approved / rejected`` 状态机从未落地。
结果是：方案写完了，谁审的、审到哪一章、驳回理由是什么，系统里一无所知。

工程软件的可追溯性要求评审过程留痕（谁、何时、什么意见、什么结论），
本模块补齐该能力：

- 章节级审核：逐章标记通过 / 驳回，可附评审意见；
- 方案级提交：整体推进状态（待审核 → 审核中 → 已通过 / 已驳回）；
- 评审记录：每次状态流转落一条 ``review_records``，可回溯完整评审轨迹；
- 与预检联动：存在交付阻断项时提交审核会被拒绝（带阻断项的方案不应进入审核）。
"""
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from app.db import get_db
from app.models import REVIEW_STATUSES, SchemeReviewIn, SectionReviewIn

router = APIRouter(prefix="/api/v1/schemes/{scheme_id}/review", tags=["review"])

logger = logging.getLogger("review")

#: 批量审核单次上限（超过则截断；返回体带截断告警，不再静默）
BATCH_REVIEW_MAX_IDS = 500

#: 章节审核状态中文标签（前端展示用）
REVIEW_STATUS_LABEL = {
    "": "未纳入审核",
    "pending": "待审核",
    "reviewing": "审核中",
    "approved": "已通过",
    "rejected": "已驳回",
}

#: 合法流转（from → 允许 to 集合）
#  ✅ BUG 修复（2026-09-18）：与前端 ReviewWorkflowPanel.NEXT_ACTIONS 对齐。
#     旧实现缺三条**前端确实会发出**的流转，用户点按钮必收 400「不允许的流转」：
#       · "" → rejected        未纳入审核时直接驳回（前端「驳回」按钮，且驳回必填理由）
#       · approved → pending   已通过后「重置为待审核」（前端任一非空状态都渲染「重置」）
#       · rejected → approved  驳回后「整改后通过」（前端唯一出口；方案级 /submit 同理，
#                              旧实现使方案被驳回后**再也无法通过审核**）
_ALLOWED_TRANSITIONS = {
    # ✅ BUG 修复（2026-09-21）：四个非空状态都加入「自流转」（自身）为合法目标。
    #    旧矩阵刻意不含自身，于是 review_section 的「再次通过」会 400，
    #    批量审核里已是目标状态的章节会被 batch_review_sections 静默计入
    #    skipped——用户点完批量确认看到「跳过 2 个」却不知原因。
    #    自流转本身必须幂等（不落 review_records），由各调用方负责。
    "": {"pending", "reviewing", "approved", "rejected"},
    "pending": {"pending", "reviewing", "approved", "rejected"},
    "reviewing": {"reviewing", "approved", "rejected", "pending"},
    "approved": {"approved", "reviewing", "rejected", "pending"},
    "rejected": {"rejected", "reviewing", "pending", "approved"},
}


def _validate_transition(from_status: str, to_status: str):
    if to_status not in REVIEW_STATUSES:
        raise HTTPException(400, f"非法的审核状态：{to_status}")
    # ✅ BUG 修复（2026-09-21）：自流转（from == to）此前被判为「不允许的流转」返回 400。
    #    触发路径：用户点完「通过」后 UI 未刷新，再点一次同一个按钮；或批量选择里
    #    混入已是目标状态的章节。这类请求语义上是「确认当前状态」，应当幂等成功，
    #    而不是让评审人收到一条莫名其妙的 400。现显式放行，由调用方决定是否落痕。
    if from_status == to_status:
        return
    allowed = _ALLOWED_TRANSITIONS.get(from_status or "", set())
    if to_status not in allowed:
        raise HTTPException(
            400,
            f"不允许的流转：{REVIEW_STATUS_LABEL.get(from_status, '未纳入审核')}"
            f" → {REVIEW_STATUS_LABEL.get(to_status, to_status)}")


def _validate_review_payload(reviewer: str, comment: str, to_status: str):
    """审核留痕必须可追溯；驳回还必须给出可执行的原因。"""
    if not str(reviewer or "").strip():
        raise HTTPException(422, "评审人不能为空，审核记录必须可追溯")
    if to_status == "rejected" and not str(comment or "").strip():
        raise HTTPException(422, "驳回时必须填写驳回理由")


async def _write_record(db, scheme_id: str, section_id: str, section_title: str,
                        from_status: str, to_status: str,
                        reviewer: str, comment: str,
                        project_id: str | None = None):
    """落一条评审留痕（不提交事务，由调用方统一 commit）。

    ✅ G4/G5（2026-09-21）：补齐 project_id 列。review_records 此前没有该列，
    项目级审计（"这个项目有几个方案过了审"）只能 JOIN schemes，且与
    compliance_check / consistency_audit / preflight_runs 的口径不一致。
    传 None 时自动反查方案所属项目；批量调用方应预先查一次并显式传入，
    避免 500 条记录各自做一次反查。
    """
    if project_id is None:
        project_id = await _scheme_project_id(db, scheme_id)
    await db.execute(
        "INSERT INTO review_records (id, scheme_id, project_id, section_id, section_title,"
        " from_status, to_status, reviewer, comment) VALUES (?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), scheme_id, project_id or "", section_id, section_title,
         from_status, to_status, reviewer, comment))


async def _scheme_project_id(db, scheme_id: str) -> str:
    """反查方案所属项目（不存在或为空时返回空串，不抛异常）。"""
    try:
        cur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
        row = await cur.fetchone()
        if row and row["project_id"]:
            return str(row["project_id"])
    except Exception as e:
        logger.warning("反查方案项目失败（留痕 project_id 留空）: scheme=%s err=%s",
                       scheme_id, e)
    return ""


# 正文变更后需要退回「待审核」的审核状态集合。
# pending / "" 本身就是"未过审"语义，无需变更（幂等）。
REVIEW_STATUS_NEED_RESET = ("reviewing", "approved", "rejected")


async def reset_review_on_content_change(db, scheme_id: str, section_id: str,
                                         title: str = "", actor: str = "",
                                         comment: str = "") -> bool:
    """正文被改写后，把该章节审核状态退回「待审核」并落一条评审轨迹。

    ✅ G9（2026-09-21）修复：全库只有本模块写 sections.review_status。
    正文生成链路（sse_handlers）在完成后统一置 pending，但**手动编辑**与
    **一致性自动修复**这两条路径此前不重置 —— 已 rewrite 的正文仍挂着
    approved，"这份正文审过了"这句话不再成立，导出预检的 review_pending /
    review_missing 也全部漏报，成稿可能在未被任何审核的前提下交付。

    语义：这是"审核结论因正文变更而失效"的系统动作，评审人为系统（actor），
    from_status = 原状态、to_status = pending，留痕便于审计回溯。

    幂等：pending / "" 状态直接跳过；章节不存在也直接跳过。
    不提交事务：调用方在自身逻辑末尾统一 commit（本函数不做 rollback）。

    Returns:
        是否实际发生了状态变更（用于调用方判断是否需要在响应体里提示用户）。
    """
    if not section_id:
        return False
    cur = await db.execute(
        "SELECT review_status, title FROM sections WHERE id=? AND scheme_id=?",
        (section_id, scheme_id))
    row = await cur.fetchone()
    if not row:
        return False
    current = (row["review_status"] or "").strip()
    if current not in REVIEW_STATUS_NEED_RESET:
        return False
    await db.execute(
        "UPDATE sections SET review_status='pending', updated_at=? WHERE id=?",
        (datetime.now().isoformat(), section_id))
    await _write_record(db, scheme_id, section_id, row["title"] or title,
                        current, "pending", actor or "系统",
                        comment or "正文已变更，原审核结论失效，自动退回待审核（请重新送审）")
    return True


# @deprecated 孤儿 API：本前端无调用方（状态机常量已镜像在 types/audit.ts）；
# 按向后兼容原则保留，大版本评估清理。
@router.get("/statuses", deprecated=True)
async def list_statuses():
    return {"items": [{"key": k, "label": v} for k, v in REVIEW_STATUS_LABEL.items()],
            "allowed": {k: sorted(v) for k, v in _ALLOWED_TRANSITIONS.items()}}


@router.get("/summary")
async def review_summary(scheme_id: str, db=Depends(get_db)):
    """审核进度概览：各状态章节数 + 最近评审记录 + 方案整体状态。"""
    cur = await db.execute("SELECT name, status, review_status FROM schemes WHERE id=?", (scheme_id,))
    scheme = await cur.fetchone()
    if not scheme:
        raise HTTPException(404, "方案不存在")

    cur = await db.execute(
        "SELECT review_status, COUNT(*) c FROM sections WHERE scheme_id=? GROUP BY review_status",
        (scheme_id,))
    counts = {r["review_status"] or "": r["c"] for r in await cur.fetchall()}

    cur = await db.execute(
        "SELECT COUNT(*) c FROM sections WHERE scheme_id=? AND COALESCE(content,'')!=''",
        (scheme_id,))
    generated = (await cur.fetchone())["c"]

    cur = await db.execute(
        "SELECT id, section_id, section_title, from_status, to_status, reviewer,"
        " comment, created_at FROM review_records WHERE scheme_id=?"
        " ORDER BY created_at DESC LIMIT 20", (scheme_id,))
    records = [dict(r) for r in await cur.fetchall()]

    total = sum(counts.values())
    approved = counts.get("approved", 0)
    # ✅ BUG 修复（2026-09-21）：旧实现 progress = approved / total，把「已驳回」
    #    「审核中」与「未纳入审核」都算作未完成，用户无法区分「评审中 40%」
    #    与「评审中且已驳回 2 章」两种截然不同的场景。现补充 reviewed_count /
    #    reviewed_progress（已进入审核流即算完成），保留原 progress 语义
    #    （approved/total，作为「通过率」口径，前端兼容不变）。
    #    reviewed_all 语义收窄为「所有章节都已完成审核（通过或驳回）」，
    #    此前语义是「全部通过」，会掩盖大量已驳回章节。
    reviewed = total - counts.get("", 0)  # 未纳入审核（空串）不计入已审
    reviewed_all = total > 0 and reviewed == total
    return {
        # scheme_status 语义收窄为编译状态（草稿/目录已确认）；审核状态走独立字段
        "scheme_status": scheme["status"] or "",
        # ✅ 2026-09-17：新增审核状态字段（与编译状态解耦后由 /submit 维护）
        "review_status": scheme["review_status"] or "",
        "review_status_label": REVIEW_STATUS_LABEL.get(scheme["review_status"] or "", "未纳入审核"),
        "scheme_name": scheme["name"] or "",
        "counts": counts,
        "labels": REVIEW_STATUS_LABEL,
        "total_sections": total,
        "generated_sections": generated,
        "approved_sections": approved,
        "progress": round(approved / total * 100, 1) if total else 0.0,
        # ✅ 新增字段（保持 progress / reviewed_all 既有字段名不变以兼容前端）
        "reviewed_sections": reviewed,
        "reviewed_progress": round(reviewed / total * 100, 1) if total else 0.0,
        "approved_progress": round(approved / total * 100, 1) if total else 0.0,
        "reviewed_all": reviewed_all,
        "records": records,
    }


@router.get("/checklist")
async def review_checklist(scheme_id: str, db=Depends(get_db)):
    """章节审核清单：逐章列出审核状态与最近意见（审核工作台主表格）。"""
    cur = await db.execute(
        "SELECT id, title, level, word_count, review_status, parent_id FROM sections"
        " WHERE scheme_id=? ORDER BY sort_order", (scheme_id,))
    sections = [dict(r) for r in await cur.fetchall()]
    if not sections:
        return {"items": []}

    cur = await db.execute(
        # ✅ 确定性排序（2026-09-30）：review_records.created_at 精度只到秒（DB 默认
        #    datetime('now','localtime')），批量审核 / 正文重生成重置会在同一秒内写入
        #    多条记录。旧实现只按 created_at DESC —— 并列时 SQLite 返回行序不确定，
        #    "最近一次评审意见"可能显示成同一秒内较早的那条（评审轨迹可追溯性失真）。
        #    补 rowid DESC 兜底（rowid 单调递增 = 写入顺序），保证"最近"恒为最后写入。
        "SELECT section_id, to_status, reviewer, comment, created_at FROM review_records"
        " WHERE scheme_id=? AND section_id!='' ORDER BY created_at DESC, rowid DESC",
        (scheme_id,))
    latest: dict = {}
    for r in await cur.fetchall():
        # 已按时间倒序，首次出现即该章节最近一次评审
        latest.setdefault(r["section_id"], dict(r))

    items = []
    for s in sections:
        rec = latest.get(s["id"], {})
        items.append({
            "id": s["id"],
            "title": s["title"],
            "level": s["level"],
            "parent_id": s.get("parent_id") or "",
            "word_count": s.get("word_count") or 0,
            "review_status": s.get("review_status") or "",
            "review_status_label": REVIEW_STATUS_LABEL.get(s.get("review_status") or ""),
            "last_reviewer": rec.get("reviewer") or "",
            "last_comment": rec.get("comment") or "",
            "last_reviewed_at": rec.get("created_at") or "",
        })
    return {"items": items}


# ⚠️ 路由顺序敏感：/sections/batch 必须声明在 /sections/{section_id} **之前**。
#    FastAPI 按注册顺序匹配，若动态段在前，"batch" 会被当作 section_id 吃掉，
#    批量审核将 404（章节不存在）。
@router.post("/sections/batch")
async def batch_review_sections(scheme_id: str, body: dict, db=Depends(get_db)):
    """批量审核（多选章节统一通过 / 驳回）。

    ``force=True`` 时跳过状态机校验 —— 用于"全部重置为待审核"这类运维动作。
    返回体带 ``changed``（实际变更数）、``skipped``（状态机拦截）、
    ``not_found``（章节不存在或不属于该方案）、``total_ids`` 与 ``truncated``，
    便于前端精确反馈"哪些没动"以及运维识别截断。
    """
    ids = [str(x) for x in (body.get("section_ids") or [])]
    to_status = str(body.get("to_status") or "")
    if to_status not in REVIEW_STATUSES:
        raise HTTPException(400, f"非法的审核状态：{to_status}")
    reviewer = str(body.get("reviewer") or "")
    comment = str(body.get("comment") or "")
    force = bool(body.get("force"))
    if not ids:
        raise HTTPException(400, "未选择章节")

    # ✅ BUG 修复（2026-09-21）：旧实现对不存在的 scheme 静默返回 changed=0，
    #    用户误以为「都成功了」；现补 404，与 /summary / /review_section 一致。
    sc = await db.execute("SELECT id FROM schemes WHERE id=?", (scheme_id,))
    if not await sc.fetchone():
        raise HTTPException(404, "方案不存在")
    _validate_review_payload(reviewer, comment, to_status)
    # ✅ G4/G5：留痕的项目维度一次查好复用（批量可达 500 条，不能每条反查）
    project_id = await _scheme_project_id(db, scheme_id)

    # ✅ BUG 修复（2026-09-21）：旧实现 ids[:500] 静默截断，超出部分无反馈；
    #    现保留 500 上限但把事实显式写入返回体，并记录 warning 日志。
    total_ids = len(ids)
    truncated = total_ids > BATCH_REVIEW_MAX_IDS
    processed_ids = ids[:BATCH_REVIEW_MAX_IDS]
    if truncated:
        logger.warning("batch_review: 收到 %d 个 section_id，截断至 %d（scheme=%s）",
                       total_ids, BATCH_REVIEW_MAX_IDS, scheme_id)

    # ✅ BUG 修复（2026-09-21）：旧实现仅返回 changed 数，前端无法区分
    #    "章节不存在" 与 "被状态机拦截" 两种失败原因；现分别收集并回传。
    changed = 0
    changed_ids: list = []  # 本次实际发生状态变化的章节（供前端精确回显）
    skipped: list = []     # 状态机拦截的章节
    not_found: list = []   # 不存在或不属于该方案的章节
    nochange: list = []    # 已是目标状态（幂等），不落痕
    for sid in processed_ids:
        cur = await db.execute(
            "SELECT id, title, review_status FROM sections WHERE id=? AND scheme_id=?",
            (sid, scheme_id))
        row = await cur.fetchone()
        if not row:
            not_found.append(sid)
            continue
        from_status = row["review_status"] or ""
        # ✅ BUG 修复（2026-09-21）：自流转必须**先于**状态机校验判定。
        #    旧顺序（先查 allowed 再判自流转）会把「approved → approved」这类
        #    幂等请求误判为 skipped——因为 _ALLOWED_TRANSITIONS 的各集合
        #    刻意不含自身，用户批量确认时会被静默跳过且无任何提示。
        if from_status == to_status:
            nochange.append(sid)
            continue
        if not force:
            allowed = _ALLOWED_TRANSITIONS.get(from_status, set())
            if to_status not in allowed:
                skipped.append(sid)
                continue
        await db.execute("UPDATE sections SET review_status=? WHERE id=?", (to_status, sid))
        await _write_record(db, scheme_id, sid, row["title"] or "",
                            from_status, to_status, reviewer, comment, project_id)
        changed += 1
        changed_ids.append(sid)
    await db.commit()
    return {
        "ok": True,
        "changed": changed,
        "changed_ids": changed_ids,
        "skipped": skipped,
        "not_found": not_found,
        "nochange": nochange,
        "total_ids": total_ids,
        "truncated": truncated,
    }


@router.post("/sections/{section_id}")
async def review_section(scheme_id: str, section_id: str, body: SectionReviewIn,
                         db=Depends(get_db)):
    """章节级审核（状态流转 + 评审意见）。"""
    cur = await db.execute(
        "SELECT id, title, review_status FROM sections WHERE id=? AND scheme_id=?",
        (section_id, scheme_id))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "章节不存在")
    if body.to_status not in REVIEW_STATUSES:
        raise HTTPException(400, f"非法的审核状态：{body.to_status}")
    from_status = row["review_status"] or ""
    _validate_transition(from_status, body.to_status)
    _validate_review_payload(body.reviewer, body.comment, body.to_status)

    # ✅ BUG 修复（2026-09-21）：自流转幂等处理。状态没变就不写 review_records，
    #    否则评审轨迹里会堆满「approved → approved」的噪音记录，反而破坏
    #    「谁在何时做了什么结论」的可追溯性（同一次点击重复落痕 = 伪造证据）。
    if from_status == body.to_status:
        return {"ok": True, "from_status": from_status, "to_status": body.to_status,
                "idempotent": True, "changed": False}

    await db.execute("UPDATE sections SET review_status=? WHERE id=?",
                     (body.to_status, section_id))
    await _write_record(db, scheme_id, section_id, row["title"] or "",
                        from_status, body.to_status, body.reviewer, body.comment)
    await db.commit()
    return {"ok": True, "from_status": from_status, "to_status": body.to_status,
            "idempotent": False, "changed": True}


def _parse_dt(ts):
    """把时间戳解析为 datetime，兼容 ISO('T' 分隔) 与 SQLite localtime(空格) 两种格式。"""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


@router.post("/submit")
async def submit_scheme_review(scheme_id: str, body: SchemeReviewIn,
                               db=Depends(get_db)):
    """方案级评审提交。

    ✅ 与预检联动：存在交付阻断项时拒绝提交 —— 带"引用已废止标准""缺计算书"
    这类硬伤的方案进评审只是浪费评审人时间。
    """
    cur = await db.execute("SELECT id, name, status, review_status FROM schemes WHERE id=?",
                           (scheme_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "方案不存在")
    if body.to_status not in REVIEW_STATUSES:
        raise HTTPException(400, f"非法的审核状态：{body.to_status}")
    _validate_review_payload(body.reviewer, body.comment, body.to_status)

    # ✅ BUG 修复（2026-09-21）：章节审核完整性校验提前到预检门控**之前**。
    #    旧实现先查 preflight_runs，导致「一个章节都没审 + 一次总检都没跑」时
    #    用户收到的是「尚未执行审核预检」——可执行性差的提示，掩盖了更根本的
    #    「章节尚未评审」。现顺序为：章节完整性 → 预检门控 → 状态机流转，
    #    用户总是先得到最该处理的那条信息。校验放在任何写库动作之前，
    #    因此拒绝时不需要回滚。
    # 严格开关的语义是「所有章节均 approved」，而不是「状态非空」。
    # reviewing / rejected 虽已进入审核流，但并未通过，不能放行方案级 approved。
    unapproved_ids: list = []
    if body.to_status == "approved":
        cur = await db.execute(
            "SELECT id FROM sections WHERE scheme_id=? AND COALESCE(review_status,'')!='approved'"
            " ORDER BY sort_order", (scheme_id,))
        unapproved_ids = [r["id"] for r in await cur.fetchall()]

    section_review_check = {
        # 保留旧字段名以兼容既有调用方；其值现表示所有未通过章节。
        "unreviewed_count": len(unapproved_ids),
        "unreviewed_ids": unapproved_ids,
        "unapproved_count": len(unapproved_ids),
        "unapproved_ids": unapproved_ids,
    }
    if body.to_status == "approved" and body.require_all_sections_reviewed and unapproved_ids:
        raise HTTPException(
            422,
            f"尚有 {len(unapproved_ids)} 个章节未通过审核（包含未纳入审核、审核中或已驳回），"
            "请先完成章节级审核（或改用 require_all_sections_reviewed=false 强制提交）")

    # 放行门禁（仅在推进到 approved 时校验）
    if body.to_status == "approved":
        # released 与 content_fingerprint 必须同查：只查 blocked 会放过 C/D 级
        # 未放行结论；只查 updated_at 又会漏掉内容变了但时间戳未变的历史脏数据。
        cur = await db.execute(
            "SELECT blocked, released, content_fingerprint, total, created_at FROM preflight_runs"
            " WHERE scheme_id=? ORDER BY created_at DESC LIMIT 1", (scheme_id,))
        pf = await cur.fetchone()
        if pf and pf["blocked"]:
            raise HTTPException(
                422, "最近一次预检存在交付阻断项，请先整改后再提交审核")
        if not pf:
            raise HTTPException(
                422, "尚未执行审核预检，请先完成「一键总检」再提交审核")
        # 指纹是正文 / 图表 / 事实状态的统一版本锚点；历史记录没有指纹时，
        # 继续用章节 updated_at 兜底，保持旧库可用。
        if pf["content_fingerprint"]:
            from app.routers.compliance import _content_fingerprint
            current_fp = await _content_fingerprint(db, scheme_id)
            if current_fp and pf["content_fingerprint"] != current_fp:
                raise HTTPException(
                    422, "正文、图表或事实已变更，最近一次预检结论已过期，"
                         "请先重新运行「一键总检」再提交审核")
        else:
            cur = await db.execute(
                "SELECT MAX(updated_at) AS m FROM sections WHERE scheme_id=?", (scheme_id,))
            _sec_max = (await cur.fetchone())["m"]
            if _sec_max:
                _pf_t = _parse_dt(pf["created_at"])
                _sec_t = _parse_dt(_sec_max)
                if _pf_t and _sec_t and _pf_t < _sec_t:
                    raise HTTPException(
                        422, "正文已变更，最近一次预检结论可能已过期，"
                             "请先重新运行「一键总检」再提交审核")
        if not pf["released"]:
            raise HTTPException(
                422, f"最近一次总检结论为 {pf['total'] or 0} 分，未达到放行条件，"
                     "请整改后重新执行「一键总检」")

    # ✅ 修复（2026-09-17）：审核状态改走独立列 review_status。旧实现直接
    #    UPDATE schemes SET status=?，评审通过后把编译状态（草稿/目录已确认）
    #    覆盖成英文 approved/rejected；反向地，编译态为「草稿」时 from_status 为空、
    #    状态机校验被跳过，可跳级流转。现 status 语义收窄为编译状态，互不干扰。
    from_status = (row["review_status"] or "") if row["review_status"] in REVIEW_STATUSES else ""
    if from_status:
        _validate_transition(from_status, body.to_status)

    await db.execute("UPDATE schemes SET review_status=?, updated_at=? WHERE id=?",
                     (body.to_status, datetime.now().isoformat(), scheme_id))
    await _write_record(db, scheme_id, "", row["name"] or "",
                        from_status, body.to_status, body.reviewer, body.comment,
                        await _scheme_project_id(db, scheme_id))

    # ✅ BUG 修复（2026-09-21）：方案级 /submit 此前不校验章节审核完整性 ——
    #    即使大量章节仍「未纳入审核」，方案也能被推为 approved。现引入
    #    ``body.require_all_sections_reviewed`` 软开关：置 True 且仍有章节未审时
    #    返回 422（见上方「章节审核完整性校验」，写库前即拒绝，无需回滚）；
    #    默认（False）保持向后兼容，仅在返回体里给出未审明细，
    #    让调用方（前端）自行决定是否弹提示。

    await db.commit()
    return {
        "ok": True,
        "from_status": from_status,
        "to_status": body.to_status,
        "section_review_check": section_review_check,
    }


@router.get("/records")
async def review_records(scheme_id: str, section_id: str = "", limit: int = 50,
                         offset: int = 0, db=Depends(get_db)):
    """评审轨迹（可下钻到单章）。

    ✅ G8（2026-09-21）：旧实现只有 limit（上限 200）没有 offset —— 单方案评审
    记录超过 200 条后，早期轨迹**永久不可见**（ORDER BY created_at DESC 永远
    只吐最近的 200 条）。工程可追溯性要求完整历史可查。现补 offset 游标分页，
    返回体带 total / limit / offset，前端做「加载更多」。
    """
    limit = max(1, min(limit, 200))
    try:
        offset = max(0, int(offset) if offset else 0)
    except (TypeError, ValueError):
        offset = 0
    where = "WHERE scheme_id=?"
    params: list = [scheme_id]
    if section_id:
        where += " AND section_id=?"
        params.append(section_id)
    # 总数（同一过滤条件），用于前端判断是否还有下一页
    cur = await db.execute(f"SELECT COUNT(*) AS c FROM review_records {where}", params)
    total = (await cur.fetchone())["c"] or 0
    cur = await db.execute(
        "SELECT id, section_id, section_title, from_status, to_status, reviewer,"
        f" comment, created_at FROM review_records {where}"
        " ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset))
    items = [dict(r) for r in await cur.fetchall()]
    for it in items:
        it["from_label"] = REVIEW_STATUS_LABEL.get(it.get("from_status") or "", "未纳入审核")
        it["to_label"] = REVIEW_STATUS_LABEL.get(it.get("to_status") or "", "")
    return {"items": items, "total": total, "limit": limit, "offset": offset,
            "has_more": total > offset + len(items)}
