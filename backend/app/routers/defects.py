"""统一缺陷账本路由（R179 Phase C，2026-11-06）。

开关 ``settings.defect_ledger_enabled`` 默认关闭：关闭时所有入口首行 404，
对既有预检 / 一致性扫描 / 评分链路零影响（本模块只读它们的结果表）。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.db import get_db
from app.services import defect_ledger as ledger
from app.services.facts_extractor import resolve_scheme_project_id

router = APIRouter(prefix="/api/v1", tags=["defects"])


def _require_enabled() -> None:
    if not settings.defect_ledger_enabled:
        raise HTTPException(404, "Not Found")


def _serialize(row: dict | None) -> dict | None:
    if not row:
        return row
    out = dict(row)
    try:
        out["policy_categories"] = json.loads(out.get("policy_categories") or "[]")
    except (TypeError, ValueError):
        out["policy_categories"] = []
    return out


class DefectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    detail: str = ""
    rule_id: str = ""
    dimension: str = ""
    severity: str = "medium"
    section_id: str = ""
    section_title: str = ""
    before_text: str = ""
    after_text: str = ""
    suggestion: str = ""


class ProposalIn(BaseModel):
    after_text: str
    suggestion: str = ""


class ApplyIn(BaseModel):
    applied_by: str = ""


class DecisionIn(BaseModel):
    decision: str
    decided_by: str = ""
    reason: str = ""


class SyncIn(BaseModel):
    include_preflight: bool = True
    include_consistency: bool = True


@router.get("/schemes/{scheme_id}/defects")
async def list_scheme_defects(scheme_id: str, status: str = "", verdict: str = "", db=Depends(get_db)):
    _require_enabled()
    rows = await ledger.list_defects(db, scheme_id, status=status, verdict=verdict)
    return {"items": [_serialize(r) for r in rows], "total": len(rows)}


@router.get("/schemes/{scheme_id}/defects/summary")
async def scheme_defect_summary(scheme_id: str, db=Depends(get_db)):
    _require_enabled()
    return await ledger.ledger_summary(db, scheme_id)


@router.post("/schemes/{scheme_id}/defects/sync")
async def sync_scheme_defects(scheme_id: str, body: SyncIn | None = None, db=Depends(get_db)):
    """只读拉取既有预检/一致性结果；绝不回写源表。"""
    _require_enabled()
    body = body or SyncIn()
    return await ledger.sync_scheme(
        db,
        scheme_id,
        include_preflight=body.include_preflight,
        include_consistency=body.include_consistency,
    )


@router.post("/schemes/{scheme_id}/defects")
async def create_scheme_defect(scheme_id: str, body: DefectCreate, db=Depends(get_db)):
    _require_enabled()
    project_id = await resolve_scheme_project_id(db, scheme_id)
    if not project_id:
        raise HTTPException(404, "方案不存在")
    row, created = await ledger.upsert_defect(
        db,
        scheme_id=scheme_id,
        project_id=project_id,
        title=body.title,
        detail=body.detail,
        section_id=body.section_id,
        section_title=body.section_title,
        before_text=body.before_text,
        after_text=body.after_text,
        suggestion=body.suggestion,
        rule_id=body.rule_id,
        dimension=body.dimension,
        severity=body.severity,
    )
    return {"created": created, "defect": _serialize(row)}


@router.get("/defects/{defect_id}")
async def get_defect_detail(defect_id: str, db=Depends(get_db)):
    _require_enabled()
    row = await ledger.get_defect(db, defect_id)
    if not row:
        raise HTTPException(404, "缺陷不存在")
    return _serialize(row)


@router.get("/defects/{defect_id}/diff")
async def get_defect_diff(defect_id: str, db=Depends(get_db)):
    _require_enabled()
    row = await ledger.get_defect(db, defect_id)
    if not row:
        raise HTTPException(404, "缺陷不存在")
    return ledger.build_diff(row.get("before_text") or "", row.get("after_text") or "")


@router.post("/defects/{defect_id}/proposal")
async def propose_defect_fix(defect_id: str, body: ProposalIn, db=Depends(get_db)):
    """建议权：登记全文建议，不写正文。"""
    _require_enabled()
    try:
        row = await ledger.propose_fix(db, defect_id, after_text=body.after_text, suggestion=body.suggestion)
    except LookupError as exc:
        raise HTTPException(404, "缺陷不存在") from exc
    except ledger.DefectStateError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _serialize(row)


@router.post("/defects/{defect_id}/apply")
async def apply_defect_fix(defect_id: str, body: ApplyIn | None = None, db=Depends(get_db)):
    """执行权（自动路径）：受保护缺陷 409，且任何写库之前拦截。"""
    _require_enabled()
    body = body or ApplyIn()
    try:
        result = await ledger.apply_fix(db, defect_id, applied_by=body.applied_by)
    except ledger.ProtectedDefectError as exc:
        raise HTTPException(
            409,
            {
                "message": "命中禁改白名单，禁止自动执行；请走 decision",
                "categories": exc.categories,
            },
        ) from exc
    except LookupError as exc:
        raise HTTPException(404, "缺陷不存在") from exc
    except ledger.DefectStateError as exc:
        raise HTTPException(400, str(exc)) from exc
    result["defect"] = _serialize(result.get("defect"))
    return result


@router.post("/defects/{defect_id}/decision")
async def decide_defect_fix(defect_id: str, body: DecisionIn, db=Depends(get_db)):
    """人的裁决权：受保护缺陷的唯一执行通道。"""
    _require_enabled()
    try:
        result = await ledger.decide_fix(
            db,
            defect_id,
            decision=body.decision,
            decided_by=body.decided_by,
            reason=body.reason,
        )
    except LookupError as exc:
        raise HTTPException(404, "缺陷不存在") from exc
    except ledger.DefectStateError as exc:
        raise HTTPException(400, str(exc)) from exc
    result["defect"] = _serialize(result.get("defect"))
    return result


@router.post("/defects/{defect_id}/verify")
async def verify_defect(defect_id: str, db=Depends(get_db)):
    """运行检查器；异常/未注册一律 undetermined，不计入通过。"""
    _require_enabled()
    try:
        row = await ledger.verify_defect(db, defect_id)
    except LookupError as exc:
        raise HTTPException(404, "缺陷不存在") from exc
    return _serialize(row)
