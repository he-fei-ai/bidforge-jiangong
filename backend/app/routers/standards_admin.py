"""条款级规范库路由（R179 Phase E，2026-11-06）。

开关 ``settings.clause_library_enabled`` 默认关闭：关闭时所有入口首行 404，
对既有正文生成 / 导出 / 预检链路零影响。本阶段只暴露两个入口：

* ``POST /api/v1/clause-library/import``：校验后 strict 整批导入；
* ``GET  /api/v1/clause-library/search``：精确条文号优先，未命中走 LIKE 降级。
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.db import get_db
from app.services import clause_store

router = APIRouter(prefix="/api/v1", tags=["standards"])


def _require_enabled() -> None:
    if not settings.clause_library_enabled:
        raise HTTPException(404, "Not Found")


class ClauseIn(BaseModel):
    clause_uid: str = ""
    standard_code: str = Field(min_length=1)
    standard_name: str = ""
    version: str = ""
    clause_no: str = ""
    chapter: str = ""
    clause_title: str = ""
    excerpt: str = ""
    excerpt_type: str = "summary"
    mandatory: int | str | bool = 0
    applicability: str = ""
    license_note: str = ""
    source_url: str = ""
    page_ref: str = ""
    source_kind: str = "public"
    verification_status: str = "unverified"
    status: str = "active"


class ImportIn(BaseModel):
    confirm_license: bool = False
    batch_id: str = ""
    clauses: list[ClauseIn]


@router.post("/clause-library/import")
async def import_clause_library(body: ImportIn, db=Depends(get_db)):
    _require_enabled()
    try:
        result = await clause_store.import_clauses(
            db,
            [c.model_dump() for c in body.clauses],
            confirm_license=body.confirm_license,
            batch_id=body.batch_id,
        )
    except clause_store.ClauseValidationError as exc:
        raise HTTPException(400, {"message": str(exc), "errors": exc.errors}) from exc
    return result


@router.get("/clause-library/search")
async def search_clause_library(
    standard_code: str = "",
    version: str = "",
    clause_no: str = "",
    q: str = "",
    limit: int = 10,
    db=Depends(get_db),
):
    _require_enabled()
    result = await clause_store.search_clauses(
        db,
        standard_code=standard_code,
        version=version,
        clause_no=clause_no,
        query=q,
        limit=limit,
    )
    return asdict(result)
