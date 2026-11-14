"""统一缺陷账本（R179 Phase C，2026-11-06）。

设计边界
--------
1. **只读引用既有事实源**：``preflight_runs`` / ``consistency_audit`` 仍是各自
   结论的唯一写方；本模块通过 ``source_run_id`` 指向对方主键，从不回写源表。
2. **幂等**：以 ``(scheme_id, fingerprint)`` 唯一索引收敛；同一缺陷反复同步只更新
   来源指针与描述，不产生重复行，也不覆盖已给出的建议/裁决。
3. **建议权与执行权分离**：``propose_fix`` 只写 ``after_text``；``apply_fix`` 对
   ``fix_policy=protected`` 直接抛错（路由映射 409，且在任何写库之前拦截）；
   受保护缺陷只能走 ``decide_fix``（人的裁决权）。
4. **全文**：``before_text`` / ``after_text`` 均为 TEXT 全文，不做截断；
   ``build_diff`` 输出逐行并排结果，供前端直接渲染。
"""

from __future__ import annotations

import difflib
import hashlib
import inspect
import json
import logging
import uuid
from datetime import datetime

from app.services.content_utils import text_word_count, word_status_for
from app.services.facts_extractor import resolve_scheme_project_id
from app.services.fix_policy import classify_fix_policy

logger = logging.getLogger(__name__)

# 三态结论（缺参/检查器失败一律 undetermined，绝不折算为“通过”）
VERDICT_YES = "yes"
VERDICT_NO = "no"
VERDICT_UNDETERMINED = "undetermined"
VERDICT_PENDING = "pending"

STATUS_OPEN = "open"
STATUS_SUGGESTED = "suggested"
STATUS_APPLIED = "applied"
STATUS_DISMISSED = "dismissed"

DECISIONS = ("accept", "reject", "defer", "manual")

#: 缺陷检查器注册表（rule_id → checker）。``POST /defects/{id}/verify`` 使用；
#: 未注册的 rule_id 一律落 undetermined（不冒充通过）。测试可注入失败检查器。
CHECKERS: dict[str, object] = {}


class ProtectedDefectError(RuntimeError):
    """受保护缺陷禁止走自动执行路径（路由映射 409）。"""

    def __init__(self, categories: list[str], reason: str = ""):
        self.categories = categories
        super().__init__(reason or "命中禁改白名单，禁止自动执行")


class DefectStateError(RuntimeError):
    """状态/参数不满足执行条件（路由映射 400）。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fingerprint(*parts: str) -> str:
    raw = "\x1f".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _json_list(raw: str) -> list:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


async def _row_dict(cur) -> dict | None:
    row = await cur.fetchone()
    return dict(row) if row else None


async def _section_content(db, section_id: str) -> str:
    if not section_id:
        return ""
    cur = await db.execute("SELECT content FROM sections WHERE id=?", (section_id,))
    row = await cur.fetchone()
    return (dict(row).get("content") or "") if row else ""


async def get_defect(db, defect_id: str) -> dict | None:
    cur = await db.execute("SELECT * FROM scheme_defects WHERE id=?", (defect_id,))
    return await _row_dict(cur)


async def list_defects(db, scheme_id: str, *, status: str = "", verdict: str = "") -> list[dict]:
    sql = "SELECT * FROM scheme_defects WHERE scheme_id=?"
    params: list = [scheme_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    if verdict:
        sql += " AND verdict=?"
        params.append(verdict)
    sql += " ORDER BY created_at, rowid"
    cur = await db.execute(sql, params)
    return [dict(r) for r in await cur.fetchall()]


async def upsert_defect(
    db,
    *,
    scheme_id: str,
    project_id: str,
    title: str,
    detail: str = "",
    source_kind: str = "manual",
    source_run_id: str = "",
    source_table: str = "",
    rule_id: str = "",
    dimension: str = "",
    severity: str = "",
    section_id: str = "",
    section_title: str = "",
    before_text: str = "",
    after_text: str = "",
    suggestion: str = "",
    verdict: str = VERDICT_PENDING,
    verdict_reason: str = "",
) -> tuple[dict, bool]:
    """幂等写入一条缺陷，返回 ``(行, 是否新建)``。

    冲突键＝``(scheme_id, fingerprint)``。fingerprint 刻意**不含 source_run_id**：
    同一条问题跨多轮预检仍应收敛为一行，只把来源指针更新到最新一次运行。
    已存在的 ``after_text`` / ``status`` / ``verdict`` 不被同步覆盖。
    """
    policy = classify_fix_policy(
        rule_id=rule_id, dimension=dimension, title=title, detail=detail, suggestion=suggestion
    )
    fp = _fingerprint(scheme_id, rule_id, section_id, title, detail)
    exists = await get_defect_by_fingerprint(db, scheme_id, fp)
    did = exists["id"] if exists else str(uuid.uuid4())
    now = _now()
    await db.execute(
        "INSERT INTO scheme_defects ("
        " id, project_id, scheme_id, source_kind, source_run_id, source_table,"
        " rule_id, dimension, severity, title, detail, section_id, section_title,"
        " before_text, after_text, suggestion, fix_policy, policy_categories,"
        " status, verdict, verdict_reason, fingerprint, created_at, updated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(scheme_id, fingerprint) DO UPDATE SET"
        "  source_kind=excluded.source_kind, source_run_id=excluded.source_run_id,"
        "  source_table=excluded.source_table, severity=excluded.severity,"
        "  title=excluded.title, detail=excluded.detail, suggestion=excluded.suggestion,"
        "  updated_at=excluded.updated_at",
        (
            did,
            project_id,
            scheme_id,
            source_kind,
            source_run_id,
            source_table,
            rule_id,
            dimension,
            severity,
            title,
            detail,
            section_id,
            section_title,
            before_text,
            after_text,
            suggestion,
            policy["policy"],
            json.dumps(policy["categories"], ensure_ascii=False),
            STATUS_SUGGESTED if after_text else STATUS_OPEN,
            verdict,
            verdict_reason,
            fp,
            now,
            now,
        ),
    )
    await db.commit()
    row = await get_defect(db, did)
    return (row or {}), exists is None


async def get_defect_by_fingerprint(db, scheme_id: str, fp: str) -> dict | None:
    cur = await db.execute("SELECT * FROM scheme_defects WHERE scheme_id=? AND fingerprint=?", (scheme_id, fp))
    return await _row_dict(cur)


async def sync_from_preflight(db, scheme_id: str) -> dict:
    """只读拉取最近一次预检结果，落账本。

    ``engine_error`` 条目（检查器自身异常）必须落 ``verdict=undetermined``，
    不得被任何「通过」统计计入。
    """
    project_id = await resolve_scheme_project_id(db, scheme_id)
    if not project_id:
        return {"source": "preflight", "scanned": 0, "created": 0, "updated": 0, "skipped": True}
    cur = await db.execute(
        "SELECT id, findings FROM preflight_runs" " WHERE scheme_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (scheme_id,),
    )
    run = await _row_dict(cur)
    if not run:
        return {"source": "preflight", "scanned": 0, "created": 0, "updated": 0, "skipped": True}
    findings = _json_list(run.get("findings") or "[]")
    created = updated = 0
    for f in findings:
        if not isinstance(f, dict):
            continue
        is_engine_error = bool(f.get("engine_error"))
        section_id = str(f.get("section_id") or "")
        before = await _section_content(db, section_id)
        _, is_new = await upsert_defect(
            db,
            scheme_id=scheme_id,
            project_id=project_id,
            title=str(f.get("title") or f.get("rule_id") or "未命名缺陷"),
            detail=str(f.get("detail") or ""),
            source_kind="preflight",
            source_run_id=str(run.get("id") or ""),
            source_table="preflight_runs",
            rule_id=str(f.get("rule_id") or ""),
            dimension=str(f.get("dimension") or ""),
            severity=str(f.get("severity") or ""),
            section_id=section_id,
            section_title=str(f.get("section_title") or ""),
            before_text=before,
            suggestion=str(f.get("suggestion") or ""),
            verdict=VERDICT_UNDETERMINED if is_engine_error else VERDICT_PENDING,
            verdict_reason=str(f.get("detail") or "") if is_engine_error else "",
        )
        created += int(is_new)
        updated += int(not is_new)
    return {
        "source": "preflight",
        "source_run_id": str(run.get("id") or ""),
        "scanned": len(findings),
        "created": created,
        "updated": updated,
        "skipped": False,
    }


async def sync_from_consistency(db, scheme_id: str) -> dict:
    """只读拉取最近一次一致性审计结果，落账本（rule_id=CON-07）。"""
    project_id = await resolve_scheme_project_id(db, scheme_id)
    if not project_id:
        return {"source": "consistency", "scanned": 0, "created": 0, "updated": 0, "skipped": True}
    cur = await db.execute(
        "SELECT id, issues FROM consistency_audit" " WHERE scheme_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (scheme_id,),
    )
    audit = await _row_dict(cur)
    if not audit:
        return {"source": "consistency", "scanned": 0, "created": 0, "updated": 0, "skipped": True}
    issues = _json_list(audit.get("issues") or "[]")
    created = updated = 0
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        occ = (issue.get("section_occurrences") or [{}])[0] if issue.get("section_occurrences") else {}
        section_id = str(occ.get("section_id") or "")
        before = await _section_content(db, section_id)
        topic = str(issue.get("topic") or "跨章节冲突")
        _, is_new = await upsert_defect(
            db,
            scheme_id=scheme_id,
            project_id=project_id,
            title=f"一致性冲突：{topic}",
            detail=str(issue.get("text") or issue.get("value") or ""),
            source_kind="consistency",
            source_run_id=str(audit.get("id") or ""),
            source_table="consistency_audit",
            rule_id="CON-07",
            dimension="consistency",
            severity="medium",
            section_id=section_id,
            section_title=str(occ.get("section_title") or ""),
            before_text=before,
            suggestion="统一各章节数值/术语口径，消除跨章冲突",
        )
        created += int(is_new)
        updated += int(not is_new)
    return {
        "source": "consistency",
        "source_run_id": str(audit.get("id") or ""),
        "scanned": len(issues),
        "created": created,
        "updated": updated,
        "skipped": False,
    }


async def sync_scheme(db, scheme_id: str, *, include_preflight=True, include_consistency=True) -> dict:
    result = {"scheme_id": scheme_id, "sources": []}
    if include_preflight:
        result["sources"].append(await sync_from_preflight(db, scheme_id))
    if include_consistency:
        result["sources"].append(await sync_from_consistency(db, scheme_id))
    result["total"] = len(await list_defects(db, scheme_id))
    return result


async def propose_fix(db, defect_id: str, *, after_text: str, suggestion: str = "") -> dict:
    """建议权：只登记全文建议，不写正文、不改缺陷状态以外的任何事实源。"""
    if not after_text or not after_text.strip():
        raise DefectStateError("after_text 不能为空：建议必须是完整替换文本")
    d = await get_defect(db, defect_id)
    if not d:
        raise LookupError("缺陷不存在")
    await db.execute(
        "UPDATE scheme_defects SET after_text=?, suggestion=?, status=?, updated_at=? WHERE id=?",
        (
            after_text,
            suggestion or d.get("suggestion") or "",
            STATUS_SUGGESTED,
            _now(),
            defect_id,
        ),
    )
    await db.commit()
    return (await get_defect(db, defect_id)) or {}


async def _write_section(db, section_id: str, after_text: str) -> bool:
    cur = await db.execute("SELECT word_budget, scheme_id FROM sections WHERE id=?", (section_id,))
    row = await cur.fetchone()
    if not row:
        raise DefectStateError("缺陷指向的章节不存在，无法执行")
    _row = dict(row)
    budget = int(_row.get("word_budget") or 0)
    # ✅ R203（2026-11-09 · 编号归一化补链）：缺陷修复整章改写正文，UPDATE 前须补
    #    normalize_section_content_subheadings 归一化子标题编号。fail-soft 不阻断。
    _scheme_id = _row.get("scheme_id") or ""
    if _scheme_id:
        try:
            from app.services.numbering import normalize_section_content_subheadings

            after_text, _ = await normalize_section_content_subheadings(db, _scheme_id, section_id, after_text)
        except Exception as _ne:  # noqa: BLE001 - 编号规范化失败不得阻断缺陷修复落库
            logger.warning("缺陷修复落库：章节 %s 编号规范化失败（保留修复正文）: %s", section_id[:8], _ne)
    count = text_word_count(after_text)
    await db.execute(
        "UPDATE sections SET content=?, word_count=?, word_status=?, updated_at=? WHERE id=?",
        (after_text, count, word_status_for(count, budget), _now(), section_id),
    )
    return True


async def _execute_defect(db, d: dict, *, decision: str, decided_by: str, reason: str) -> dict:
    after_text = d.get("after_text") or ""
    if not after_text.strip():
        raise DefectStateError("没有可执行的完整建议文本（after_text 为空）")
    try:
        section_updated = False
        if d.get("section_id"):
            section_updated = await _write_section(db, d["section_id"], after_text)
        await db.execute(
            "UPDATE scheme_defects SET status=?, updated_at=? WHERE id=?",
            (STATUS_APPLIED if decision == "accept" else STATUS_OPEN, _now(), d["id"]),
        )
        await db.execute(
            "INSERT INTO fix_decisions ("
            " id, defect_id, project_id, scheme_id, decision, decided_by, reason,"
            " applied, before_text, after_text, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                d["id"],
                d["project_id"],
                d["scheme_id"],
                decision,
                decided_by,
                reason,
                1 if section_updated else 0,
                d.get("before_text") or "",
                after_text,
                _now(),
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return {"defect": await get_defect(db, d["id"]), "section_updated": section_updated}


async def apply_fix(db, defect_id: str, *, applied_by: str = "") -> dict:
    """执行权（自动路径）：受保护缺陷在**任何写库之前**即拒绝。"""
    d = await get_defect(db, defect_id)
    if not d:
        raise LookupError("缺陷不存在")
    if d.get("fix_policy") == "protected":
        raise ProtectedDefectError(json.loads(d.get("policy_categories") or "[]"), "命中禁改白名单，apply 被拒绝")
    return await _execute_defect(db, d, decision="accept", decided_by=applied_by, reason="")


async def decide_fix(db, defect_id: str, *, decision: str, decided_by: str = "", reason: str = "") -> dict:
    """人的裁决权：受保护缺陷也只能走这里；reject/defer/manual 不写正文。"""
    if decision not in DECISIONS:
        raise DefectStateError(f"decision 必须是 {DECISIONS} 之一")
    d = await get_defect(db, defect_id)
    if not d:
        raise LookupError("缺陷不存在")
    if decision == "accept":
        result = await _execute_defect(db, d, decision="accept", decided_by=decided_by, reason=reason)
        return result
    status = STATUS_DISMISSED if decision == "reject" else STATUS_OPEN
    try:
        await db.execute(
            "UPDATE scheme_defects SET status=?, updated_at=? WHERE id=?",
            (status, _now(), defect_id),
        )
        await db.execute(
            "INSERT INTO fix_decisions ("
            " id, defect_id, project_id, scheme_id, decision, decided_by, reason,"
            " applied, before_text, after_text, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                d["id"],
                d["project_id"],
                d["scheme_id"],
                decision,
                decided_by,
                reason,
                0,
                d.get("before_text") or "",
                d.get("after_text") or "",
                _now(),
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return {"defect": await get_defect(db, defect_id), "section_updated": False}


def _normalize_verdict(result) -> str:
    if isinstance(result, dict):
        result = result.get("verdict")
    if isinstance(result, bool):
        return VERDICT_YES if result else VERDICT_NO
    if isinstance(result, str) and result in (VERDICT_YES, VERDICT_NO, VERDICT_UNDETERMINED):
        return result
    return VERDICT_UNDETERMINED


async def verify_defect(db, defect_id: str, *, checker=None, checker_name: str = "") -> dict:
    """运行检查器并落 ``verdict``。

    检查器**异常**或**未注册**一律 ``undetermined`` —— 这是本模块的硬约束：
    检查没跑完 ≠ 通过。``ledger_summary`` 只把 ``verdict=yes`` 计入 passed。
    """
    d = await get_defect(db, defect_id)
    if not d:
        raise LookupError("缺陷不存在")
    fn = checker if checker is not None else CHECKERS.get(d.get("rule_id") or "")
    name = checker_name or getattr(fn, "__name__", "") or "未注册检查器"
    if fn is None:
        verdict, reason = VERDICT_UNDETERMINED, f"未注册检查器（{d.get('rule_id') or '无规则编号'}），不做通过判定"
    else:
        try:
            result = fn(d)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - 检查器异常必须落 undetermined
            verdict = VERDICT_UNDETERMINED
            reason = f"检查器异常（{name}）：{type(exc).__name__}: {exc}"
        else:
            verdict = _normalize_verdict(result)
            reason = "" if verdict != VERDICT_UNDETERMINED else "检查器返回无法识别的结论"
    await db.execute(
        "UPDATE scheme_defects SET verdict=?, verdict_reason=?, updated_at=? WHERE id=?",
        (verdict, reason, _now(), defect_id),
    )
    await db.commit()
    return await get_defect(db, defect_id) or {}


async def ledger_summary(db, scheme_id: str) -> dict:
    """统计口径：``passed`` 只计 ``verdict=yes``；undetermined 单列，绝不并入通过。

    ✅ R227（D2 · 2026-11-13 · 产品裁决「标记 stale 保留账本留痕」）：加法式新增
    ``stale`` 键（指向已删章节的失效账本行数）。**不改**既有 total/passed/failed/
    undetermined/pending 计数语义——失效账本仍是账本行（留痕），由显示层单独标注
    「章节已删除」，不参与通过/失败重判。旧库无 stale 列（默认 0）时 stale=0，
    返回体与升级前逐字一致（零迁移、零语义变化）。
    """
    cur = await db.execute(
        "SELECT verdict, COUNT(*) AS c FROM scheme_defects WHERE scheme_id=? GROUP BY verdict",
        (scheme_id,),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    counts = {str(r["verdict"] or VERDICT_PENDING): int(r["c"] or 0) for r in rows}
    total = sum(counts.values())
    cur2 = await db.execute(
        "SELECT COUNT(*) AS c FROM scheme_defects WHERE scheme_id=? AND stale=1", (scheme_id,)
    )
    _stale_row = await cur2.fetchone()
    stale = int(_stale_row["c"] or 0) if _stale_row else 0
    return {
        "total": total,
        "passed": counts.get(VERDICT_YES, 0),
        "failed": counts.get(VERDICT_NO, 0),
        "undetermined": counts.get(VERDICT_UNDETERMINED, 0),
        "pending": counts.get(VERDICT_PENDING, 0) + counts.get("", 0),
        "stale": stale,
        "counts": counts,
        "pass_rule": "passed 仅统计 verdict=yes；undetermined 永不计入通过",
    }


def build_diff(before_text: str, after_text: str) -> dict:
    """并排比对（全文、逐行，不做截断）。"""
    before = before_text or ""
    after = after_text or ""
    left = before.splitlines()
    right = after.splitlines()
    rows: list[dict] = []
    matcher = difflib.SequenceMatcher(None, left, right)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append({"type": "equal", "before": left[i1 + k], "after": right[j1 + k]})
        elif tag == "delete":
            for k in range(i1, i2):
                rows.append({"type": "delete", "before": left[k], "after": ""})
        elif tag == "insert":
            for k in range(j1, j2):
                rows.append({"type": "insert", "before": "", "after": right[k]})
        else:  # replace：逐行配对，长度不等的剩余部分单独成行
            span = max(i2 - i1, j2 - j1)
            for k in range(span):
                rows.append(
                    {
                        "type": "replace",
                        "before": left[i1 + k] if i1 + k < i2 else "",
                        "after": right[j1 + k] if j1 + k < j2 else "",
                    }
                )
    unified = "\n".join(difflib.unified_diff(left, right, fromfile="before", tofile="after", lineterm=""))
    return {
        "before_text": before,
        "after_text": after,
        "before_chars": len(before),
        "after_chars": len(after),
        "truncated": False,
        "rows": rows,
        "unified": unified,
    }
