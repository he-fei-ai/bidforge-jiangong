# ✅ R179 M1（2026-11-05）：评分项 → 目录映射校验路由。
#
# 三条接口全部为**加法式新增**，不改动既有接口契约：
#   POST /api/v1/projects/{project_id}/scoring-items/extract
#   GET  /api/v1/projects/{project_id}/scoring-items
#   GET  /api/v1/schemes/{scheme_id}/scoring-coverage
#
# 错误沿用仓内约定：HTTPException → {"detail": "..."}。
# 覆盖率报告**不缓存**（纯函数计算）；抽取结果落库即快照。
# v1 语义：doc_ids 仅做类型校验，不改变扫描范围（全自动扫候选页块）。
from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException

from app.config import settings
from app.db import get_db
from app.services import scoring_items as svc

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scoring"])

# 同项目抽取互斥（单进程内），避免并发重复烧 AI；进程重启后自然释放
_ACTIVE_EXTRACT: set[str] = set()


def _whitelist() -> list[str]:
    raw = getattr(settings, "scoring_extra_whitelist", "") or ""
    return [w.strip() for w in raw.split(",") if w.strip()]


async def _get_project(db, project_id: str):
    cur = await db.execute("SELECT id, name FROM projects WHERE id=?", (project_id,))
    return await cur.fetchone()


async def _get_scheme(db, scheme_id: str):
    cur = await db.execute("SELECT id, project_id, name FROM schemes WHERE id=?", (scheme_id,))
    return await cur.fetchone()


async def _load_outline_rows(db, scheme_id: str) -> list[dict]:
    cur = await db.execute(
        "SELECT id, parent_id, title, sort_order, content FROM sections WHERE scheme_id=?"
        " ORDER BY sort_order, rowid",
        (scheme_id,),
    )
    rows = await cur.fetchall()
    return [
        {
            "id": svc._row(r, "id", 0),
            "parent_id": svc._row(r, "parent_id", 1) or "",
            "title": svc._row(r, "title", 2) or "",
            "sort_order": svc._row(r, "sort_order", 3) or 0,
            "content": svc._row(r, "content", 4) or "",
        }
        for r in rows
    ]


@router.post("/api/v1/projects/{project_id}/scoring-items/extract")
async def extract_scoring_items(project_id: str, body: dict | None = Body(None), db=Depends(get_db)):
    """抽取评分项快照；重复调用 = 按 id UPSERT（success run 会把未覆盖旧行标 stale）。"""
    project = await _get_project(db, project_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    body = body or {}
    doc_ids = body.get("doc_ids") if isinstance(body, dict) else None
    if doc_ids is not None and (not isinstance(doc_ids, list) or any(not isinstance(d, str) for d in doc_ids)):
        raise HTTPException(400, "doc_ids 必须是字符串数组")
    # v1：doc_ids 传了也不改变扫描范围（仅为 v2 保留字段，见方案 §1.3）

    if project_id in _ACTIVE_EXTRACT:
        raise HTTPException(409, "评分项解析正在进行，请稍后重试")
    _ACTIVE_EXTRACT.add(project_id)
    try:
        return await _do_extract(db, project_id)
    finally:
        _ACTIVE_EXTRACT.discard(project_id)


async def _do_extract(db, project_id: str) -> dict:
    max_docs = int(getattr(settings, "scoring_extract_max_docs", 4))
    max_chars = int(getattr(settings, "scoring_extract_max_chars", 60000))
    timeout = int(getattr(settings, "scoring_extract_timeout", 120))

    sources = await svc.select_scoring_sources(db, project_id)
    if not sources:
        run_id = uuid.uuid4().hex
        run = {
            "id": run_id,
            "project_id": project_id,
            "status": "failed",
            "docs_total": 0,
            "docs_ok": 0,
            "failed_sources": [],
            "items_count": 0,
            "error": "未在投标文件中定位到「评标办法/评分标准」候选页块",
        }
        await svc.insert_run(db, run)
        await db.commit()
        logger.info("AI_WARMUP_NA scoring extract: project=%s status=failed reason=no_source_block", project_id)
        return {
            "project_id": project_id,
            "run_id": run_id,
            "status": "failed",
            "items": [],
            "items_count": 0,
            "docs_total": 0,
            "docs_ok": 0,
            "failed_sources": [],
            "error": run["error"],
        }

    batches, overflow = svc.plan_batches(sources, max_chars=max_chars, max_docs=max_docs)
    page_index = {s["doc_id"]: s.get("pages") or [] for s in sources}
    raw_items: list[dict] = []
    failed_sources: list[dict] = list(overflow)
    errors: list[str] = []
    ok_batches = 0
    for batch in batches:
        try:
            raw_items.extend(await svc.extract_batch(batch, timeout=timeout))
            ok_batches += 1
        except Exception as e:  # 单批失败不整体失败：其余批次结果照常保留
            failed_sources.extend(
                {
                    "doc_id": s.get("doc_id") or "",
                    "file_name": s.get("name") or "",
                    "reason": f"{type(e).__name__}: {e}"[:200],
                }
                for s in batch
            )
            errors.append(f"{type(e).__name__}: {e}"[:400])
            logger.warning("评分项抽取批次失败（%d docs）：%s", len(batch), e)

    items = svc.normalize_and_validate(project_id, raw_items, page_index)

    # 「本次返回 0 条」≠「真的没有评分项」，必须可区分：
    #   此前已有活跃快照 → empty（保留旧快照，不静默清空）；
    #   此前没有任何快照 → success（确无评分项，覆盖率侧判 empty_items）。
    prior_active = 0
    if ok_batches > 0 and not items:
        cur = await db.execute(
            "SELECT COUNT(*) FROM scheme_scoring_items WHERE project_id=? AND status='active'",
            (project_id,),
        )
        prior_active = int((await cur.fetchone())[0] or 0)

    if items and ok_batches > 0 and not failed_sources:
        run_status = "success"
    elif items and ok_batches > 0:
        run_status = "partial"
    elif ok_batches > 0 and prior_active > 0:
        run_status = "empty"
    elif ok_batches > 0:
        run_status = "success"
    else:
        run_status = "failed"

    if run_status == "empty":
        errors.append(f"本次解析返回 0 条评分项，已保留上一次快照（{prior_active} 条），请复核评标办法是否变更")

    run_id = uuid.uuid4().hex
    run = {
        "id": run_id,
        "project_id": project_id,
        "status": run_status,
        "docs_total": len(sources),
        "docs_ok": (len(sources) - len(failed_sources)) if failed_sources else len(sources),
        "failed_sources": failed_sources,
        "items_count": len(items),
        "error": "；".join(errors),
    }
    try:
        await svc.persist_items(db, project_id, items, run_status=run_status)
        await svc.insert_run(db, run)
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(500, f"评分项保存失败：{e}")

    logger.info(
        "M1 scoring extract: project=%s status=%s items=%d batches=%d failed_sources=%d",
        project_id,
        run_status,
        len(items),
        len(batches),
        len(failed_sources),
    )
    return {
        "project_id": project_id,
        "run_id": run_id,
        "status": run_status,
        "items": items,
        "items_count": len(items),
        "docs_total": run["docs_total"],
        "docs_ok": run["docs_ok"],
        "failed_sources": failed_sources,
        "error": run["error"],
    }


@router.get("/api/v1/projects/{project_id}/scoring-items")
async def list_scoring_items(project_id: str, db=Depends(get_db)):
    project = await _get_project(db, project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    items = await svc.load_active_items(db, project_id)
    run = await svc.load_latest_run(db, project_id)
    return {
        "project_id": project_id,
        "items": items,
        "run": run,
        "undetermined": (not items) or (not run) or run.get("status") in ("failed", "running"),
    }


def _parse_thresholds(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        raise HTTPException(400, "thresholds 参数必须是 JSON 对象")
    if not isinstance(obj, dict):
        raise HTTPException(400, "thresholds 参数必须是 JSON 对象")
    return obj


async def scoring_coverage_state(db, scheme_id: str) -> dict:
    """评分项覆盖**轻量状态**（✅ R198 · R197 遗留②：就绪度第七源复用核心）。

    与 ``/scoring-coverage`` 同源（同一 ``scoring_items.build_coverage_report``
    判据，不复制第二份口径），但只取聚合态、不构建逐项 UI 报告：

    - ``applicable=False``：评分项体系**从未启用**（该项目没有提取运行记录）
      —— 这不是「该查未查」，调用方（总检）不得据此计分（向后兼容：既有
      未用评分项功能的方案，总检分数与放行判定逐字不变）；
    - ``applicable=True, status="undetermined"``：体系已启用但本次不可判定
      （提取 failed/running、或 0 条评分项）—— 调用方应计入 partial_sources；
    - 其余：``status=ok|partial`` + ``coverage_rate`` + missing 清单（前若干项
      名称供总检 finding 文案）。

    读库异常**不吞** —— 由调用方按 failed_sources 处置（与总检其它来源
    同一容错口径）。纯读函数，无写副作用。
    """
    scheme = await _get_scheme(db, scheme_id)
    if not scheme:
        return {
            "applicable": False,
            "status": "not_applicable",
            "undetermined_reason": "no_scheme",
            "coverage_rate": None,
            "total_items": 0,
            "missing_count": 0,
            "missing_names": [],
        }
    project_id = svc._row(scheme, "project_id", 1) or ""
    items = await svc.load_active_items(db, project_id) if project_id else []
    run = await svc.load_latest_run(db, project_id) if project_id else None
    if run is None:
        return {
            "applicable": False,
            "status": "not_applicable",
            "undetermined_reason": "no_extraction",
            "coverage_rate": None,
            "total_items": len(items),
            "missing_count": 0,
            "missing_names": [],
        }
    if not items:
        # 有运行记录但 0 条激活评分项：体系「启用过」，本次覆盖无从谈起
        reason = "extraction_failed" if run.get("status") in ("failed", "running") else "empty_items"
        return {
            "applicable": True,
            "status": "undetermined",
            "undetermined_reason": reason,
            "coverage_rate": None,
            "total_items": 0,
            "missing_count": 0,
            "missing_names": [],
            "run_status": run.get("status"),
        }
    extraction_status = "success"
    if run.get("status") in ("partial", "empty"):
        extraction_status = "partial"
    outline_rows = await _load_outline_rows(db, scheme_id)
    tree = svc.build_outline_tree(outline_rows)
    sections = svc.flatten_outline(tree)
    section_texts = {str(r.get("id") or ""): str(r.get("content") or "") for r in outline_rows}
    report = svc.build_coverage_report(
        items=items,
        sections=sections,
        section_texts=section_texts,
        thresholds=svc.threshold_config_from_settings(),
        extra_whitelist=_whitelist(),
        extraction_status=extraction_status,
        failed_sources=(run or {}).get("failed_sources") or [],
    )
    return {
        "applicable": True,
        "status": report["status"],
        "undetermined_reason": report.get("undetermined_reason"),
        "coverage_rate": report["summary"]["coverage_rate"],
        "total_items": report["summary"]["total_items"],
        "missing_count": report["summary"]["missing"],
        "missing_names": [
            str(i.get("item_name") or "") for i in report["items"] if i.get("status") == "missing"
        ][:10],
        "run_status": run.get("status"),
    }


@router.get("/api/v1/schemes/{scheme_id}/scoring-coverage")
async def scoring_coverage(scheme_id: str, thresholds: str | None = None, db=Depends(get_db)):
    scheme = await _get_scheme(db, scheme_id)
    if not scheme:
        raise HTTPException(404, "方案不存在")
    project_id = svc._row(scheme, "project_id", 1) or ""

    items = await svc.load_active_items(db, project_id) if project_id else []
    run = await svc.load_latest_run(db, project_id) if project_id else None
    outline_rows = await _load_outline_rows(db, scheme_id)

    tree = svc.build_outline_tree(outline_rows)
    sections = svc.flatten_outline(tree)
    section_texts = {str(r.get("id") or ""): str(r.get("content") or "") for r in outline_rows}

    diag_thresholds = svc.threshold_config_from_settings()
    user_thresholds = _parse_thresholds(thresholds) or {}
    cfg = {**diag_thresholds, **{k: v for k, v in user_thresholds.items() if k in diag_thresholds}}

    extraction = None
    if run:
        extraction = {
            "run_id": run.get("run_id"),
            "status": run.get("status"),
            "docs_total": run.get("docs_total") or 0,
            "docs_ok": run.get("docs_ok") or 0,
            "failed_sources": run.get("failed_sources") or [],
            "updated_at": run.get("updated_at") or "",
        }

    base = {
        "scheme_id": scheme_id,
        "project_id": project_id,
        "extraction": extraction,
        "diagnostics": {
            "run_status": (run or {}).get("status") or "none",
            "thresholds": {
                "exact": cfg["exact"],
                "contains": cfg["contains"],
                "keyword_high": cfg["keyword_high"],
                "keyword_mid": cfg["keyword_mid"],
                "keyword_low": cfg["keyword_low"],
                "criteria_max": cfg["criteria_max"],
            },
        },
    }

    if not items:
        reason = None
        if run is None:
            reason = "no_extraction"
        elif run.get("status") in ("failed", "running"):
            reason = "extraction_failed"
        else:
            reason = "empty_items"
        return {
            **base,
            "status": "undetermined",
            "undetermined_reason": reason,
            "coverage_rate": None,
            "summary": {
                "total_items": 0,
                "mapped": 0,
                "missing": 0,
                "extra": 0,
                "unmatched": len((extraction or {}).get("failed_sources") or []),
                "coverage_rate": None,
                "undetermined_items": 0,
            },
            "items": [],
            "missing": [],
            "extra": [],
            "unmatched": [{"kind": "source", **s} for s in ((extraction or {}).get("failed_sources") or [])],
            "overloaded_sections": [],
        }

    extraction_status = "success"
    if run and run.get("status") == "partial":
        extraction_status = "partial"
    elif run and run.get("status") == "empty":
        # 本次抽取 0 条、沿用旧快照：结论可用但不是本次最新结果，不得标注为 ok
        extraction_status = "partial"

    report = svc.build_coverage_report(
        items=items,
        sections=sections,
        section_texts=section_texts,
        thresholds=cfg,
        extra_whitelist=_whitelist(),
        extraction_status=extraction_status,
        failed_sources=(run or {}).get("failed_sources") or [],
    )
    return {
        **base,
        **report,
        "coverage_rate": report["summary"]["coverage_rate"],
    }
