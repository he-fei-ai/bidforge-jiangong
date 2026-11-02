"""可观测性日志查询路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.db import get_db
from app.services import observability as obs_svc

router = APIRouter(prefix="/api/v1/logs", tags=["logs"])


@router.get("")
async def list_logs(db=Depends(get_db),
    trace_id: str = Query(""), task_id: str = Query(""), project_id: str = Query(""),
    scheme_id: str = Query(""), module: str = Query(""), level: str = Query(""),
    error_code: str = Query(""), keyword: str = Query(""), since: str = Query(""),
    until: str = Query(""), limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
):
    return await obs_svc.query_logs(db, trace_id=trace_id, task_id=task_id, project_id=project_id,
                                    scheme_id=scheme_id, module=module, level=level,
                                    error_code=error_code, keyword=keyword, since=since,
                                    until=until, limit=limit, offset=offset)


@router.get("/trace/{trace_id}")
async def trace_tree(trace_id: str, db=Depends(get_db)):
    return await obs_svc.get_trace_tree(db, trace_id)


@router.get("/task/{task_id}")
async def task_timeline(task_id: str, db=Depends(get_db)):
    return await obs_svc.get_task_timeline(db, task_id)


@router.get("/export")
async def export_logs(db=Depends(get_db), fmt: str = Query("json"), trace_id: str = Query(""), task_id: str = Query(""),
                      project_id: str = Query(""), level: str = Query(""), keyword: str = Query(""),
                      since: str = Query(""), until: str = Query("")):
    from fastapi.responses import PlainTextResponse
    content = await obs_svc.export_logs(db, fmt=fmt, trace_id=trace_id, task_id=task_id,
                                       project_id=project_id, level=level, keyword=keyword,
                                       since=since, until=until)
    media = "text/csv" if fmt == "csv" else "application/json"
    return PlainTextResponse(content, media_type=media)


@router.post("/cleanup")
async def cleanup(db=Depends(get_db), retention_days: int = Query(30, ge=1)):
    n = await obs_svc.cleanup_old_logs(db, retention_days)
    # ✅ R142：结构化 JSON Lines 日志文件的过期清理（此前只清 system_logs 表，
    #    磁盘上 structured/*.jsonl 永不清除 ⇒ 文件无限增长）。按各保留档位的
    #    最小天数清理（ERROR 90 / INFO 30 / DEBUG 7 ⇒ 以 7 天为保守界）。
    from pathlib import Path

    from app.config import LOGS_DIR, settings
    from app.utils.observability import cleanup_expired_logs
    try:
        _base = getattr(settings, "structured_log_dir", "") \
            or str(Path(LOGS_DIR) / "structured")
        removed_files = cleanup_expired_logs(
            Path(_base), retention_days={"ERROR": 90, "FATAL": 90, "WARN": 30,
                                         "WARNING": 30, "INFO": 30, "DEBUG": 7})
    except Exception:  # noqa: BLE001 - 文件清理失败不影响 DB 清理结果
        removed_files = 0
    return {"removed": n, "removed_files": removed_files}

@router.get("/metrics")
async def metrics(db=Depends(get_db), days: int = Query(7, ge=1, le=90)):
    return await obs_svc.get_metrics(db, days=days)

@router.get("/alerts")
async def alerts(db=Depends(get_db), days: int = Query(1, ge=1, le=30)):
    return await obs_svc.evaluate_alerts(db, days=days)