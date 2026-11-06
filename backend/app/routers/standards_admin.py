"""条款级规范库路由（R179 Phase E，2026-11-06）。

开关 ``settings.clause_library_enabled`` 默认关闭：关闭时所有入口首行 404，
对既有正文生成 / 导出 / 预检链路零影响。当前入口：

* ``POST /api/v1/clause-library/import``：校验后 strict 整批导入；
* ``GET  /api/v1/clause-library/search``：精确条文号优先，未命中走 LIKE 降级；
* ``GET  /api/v1/clause-library/template``：下载 CSV / JSON 导入模板；
* ``POST /api/v1/clause-library/batch-import``：逐行校验 + 导入报告（dry_run 默认 true）。
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.config import settings
from app.db import get_db
from app.services import clause_import, clause_store

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


class BatchImportIn(BaseModel):
    confirm_license: bool = False
    dry_run: bool = True
    batch_id: str = ""
    csv_text: str = ""
    clauses: list[dict] = Field(default_factory=list)


@router.get("/clause-library/template")
async def clause_template(file_format: str = Query("csv", alias="format")):
    """下载导入模板：CSV 为「表头 + 一行字段说明」，JSON 为空 clauses + 列定义。"""
    _require_enabled()
    fmt = str(file_format or "").strip().lower()
    if fmt == "csv":
        content = "\ufeff" + clause_import.build_template_csv()
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="clause_import_template.csv"'},
        )
    if fmt == "json":
        return JSONResponse(content=clause_import.build_template_json())
    raise HTTPException(400, "format 只支持 csv / json")


@router.post("/clause-library/batch-import")
async def batch_import_clauses(body: BatchImportIn, db=Depends(get_db)):
    """批量导入：默认 dry_run=true；任一行失败整批不提交。"""
    _require_enabled()
    if body.csv_text and body.clauses:
        raise HTTPException(400, "csv_text 与 clauses 只能提供一项")
    if not body.csv_text and not body.clauses:
        raise HTTPException(400, "缺少导入内容：csv_text 或 clauses 必填")
    try:
        entries = (
            clause_import.parse_csv_text(body.csv_text)
            if body.csv_text
            else clause_import.parse_json_payload({"clauses": body.clauses})
        )
        report = await clause_import.batch_import(
            db,
            entries,
            confirm_license=body.confirm_license,
            dry_run=body.dry_run,
            batch_id=body.batch_id,
        )
    except clause_store.ClauseValidationError as exc:
        raise HTTPException(400, {"message": str(exc), "errors": exc.errors}) from exc
    return report.as_dict()


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
