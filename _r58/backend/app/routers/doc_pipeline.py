"""项目资料解析四层存储 API（规范 §6 API 设计）

端点一览：
  GET  /api/v1/documents/{doc_id}/status          解析状态查询（四层概览）
  GET  /api/v1/documents/{doc_id}/extractions     提取结果查询（按类别）
  GET  /api/v1/documents/{doc_id}/chunks          分块查询（含 source_ref 溯源）
  POST /api/v1/documents/{doc_id}/reparse         增量更新（指纹比对，force 强制）
  GET  /api/v1/documents/{doc_id}/completeness    完整性校验报告
  GET  /api/v1/documents/{doc_id}/freshness       时效性检查（指纹/过期）
  POST /api/v1/documents/{doc_id}/sync-extractions 提取层物化（阶段7 入库索引）
  POST /api/v1/documents/{doc_id}/cross-check     交叉校验（阶段5）
  GET  /api/v1/projects/{project_id}/documents/index 文档索引（项目全景）
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db import get_db
from app.services.doc_pipeline import doc_storage as store
from app.services.doc_pipeline import pipeline

logger = logging.getLogger("doc_pipeline_api")
router = APIRouter(prefix="/api/v1", tags=["doc_pipeline"])


async def _load_doc(db, doc_id: str) -> dict:
    cur = await db.execute(
        "SELECT id, project_id, file_name, file_type, doc_type, doc_category,"
        " file_path, file_size, parsed_markdown, parse_warnings,"
        " file_hash_md5, file_hash_sha256, page_count, parse_status,"
        " parse_version, parsed_at, parse_duration_ms, parse_engine,"
        " extract_status, extract_time, quality_score, completeness_json,"
        " expires_at, status, created_at "
        "FROM project_documents WHERE id=?", (doc_id,))
    # ✅ P1 修复（2026-09-27 · R13 漏改点）：db.execute() 在全局单连接 + aiosqlite 下
    #    可能返回 None（连接/事务异常），直接 .fetchone() → AttributeError → 500。
    #    语义选择：返回 **503** 而非 404 —— 前端会据 404 把仍在库里的文档
    #    判为「已丢失」并从列表中移除，而实际只是连接瞬时故障（R13 的本仓约定是
    #    「log + 降级」，见 _chart_pipeline.py 同类守卫）。503 触发客户端重试，
    #    不会造成数据误判。
    if cur is None:
        logger.warning("读取文档失败（db.execute 返回 None），doc=%s", doc_id)
        raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    return dict(row)

def _doc_project_root(doc: dict):
    from app.routers.global_facts import _managed_upload_path
    return _managed_upload_path(doc.get("file_path") or "")


# ---------------------------------------------------------------------------
# 状态查询（四层概览）
# ---------------------------------------------------------------------------

@router.get("/documents/{doc_id}/status")
async def document_status(doc_id: str, db=Depends(get_db)):
    """解析状态查询：DB 时效列 + 磁盘 meta + 各层产物落盘情况。"""
    doc = await _load_doc(db, doc_id)
    meta = await asyncio.to_thread(store.read_meta, doc["project_id"], doc_id) or {}
    layers = {}
    for layer in store.ALL_LAYERS:
        d = store.layer_dir(doc["project_id"], doc_id, layer)
        layers[layer] = await asyncio.to_thread(
            lambda p=d: sorted(f.name for f in p.iterdir()) if p.exists() else [])
    completeness = {}
    try:
        completeness = json.loads(doc.get("completeness_json") or "{}")
    except ValueError:
        pass
    warnings = []
    try:
        warnings = json.loads(doc.get("parse_warnings") or "[]")
        if not isinstance(warnings, list):
            warnings = []
    except ValueError:
        pass
    # ✅ BUG 修复（质量分 0% 被吞）：旧写法 `float(doc.get("quality_score") or -1)`
    #    用 `or` 兜底 null，但 0.0 是假值会被替换成 -1 → 判定 <0 → 返回 None。
    #    后果：质量分恰好为 0（解析极差、覆盖率全 0）的文档在前端显示为「未评估」，
    #    用户误以为「还没跑校验」而实际是「跑了、结果很差」。现区分 null 与 0.0，
    #    仅 None / 负数哨兵 / 非数字才归一为 None。
    qs_raw = doc.get("quality_score")
    try:
        quality_score = None if qs_raw is None else float(qs_raw)
    except (TypeError, ValueError):
        quality_score = None
    if quality_score is not None and quality_score < 0:
        quality_score = None
    return {
        "doc_id": doc_id,
        "project_id": doc["project_id"],
        "file_name": doc["file_name"],
        "parse_status": doc.get("parse_status") or "pending",
        "parse_version": doc.get("parse_version") or "v1",
        "parse_time": doc.get("parsed_at") or "",
        "parse_duration_ms": doc.get("parse_duration_ms") or 0,
        "page_count": doc.get("page_count") or 0,
        "extract_status": doc.get("extract_status") or "pending",
        "extract_time": doc.get("extract_time") or "",
        "quality_score": quality_score,
        "file_hash_md5": doc.get("file_hash_md5") or meta.get("file_hash_md5", ""),
        "expires_at": doc.get("expires_at") or meta.get("expires_at", ""),
        "completeness": completeness,
        "parse_warnings": [str(w) for w in warnings],
        "layers": layers,
        "meta_on_disk": bool(meta),
    }


# ---------------------------------------------------------------------------
# 提取结果查询
# ---------------------------------------------------------------------------

@router.get("/documents/{doc_id}/extractions")
async def get_extractions(
    doc_id: str,
    type: str = Query("", alias="type"),
    db=Depends(get_db),
):
    """提取层查询：按类别返回 AI 提取结果（含 confidence 与 source_refs 溯源）。"""
    doc = await _load_doc(db, doc_id)
    sql = ("SELECT extraction_id, doc_id, extract_type, extract_data,"
           " confidence, source_refs, extract_time, extract_engine, status"
           " FROM doc_extractions WHERE doc_id=?")
    params: list = [doc_id]
    if type:
        if type not in store.EXTRACT_TYPES:
            raise HTTPException(400,
                               f"未知提取类别：{type}（可选 {', '.join(store.EXTRACT_TYPES)}）")
        sql += " AND extract_type=?"
        params.append(type)
    sql += " ORDER BY extract_type"
    cur = await db.execute(sql, params)
    items = []
    # ✅ P1：同 R13 守卫（查询异常时返回空列表而非 500）
    if cur is None:
        logger.warning("查询提取结果失败（db.execute 返回 None），返回空列表")
        return {"doc_id": doc_id, "project_id": doc["project_id"],
                "items": [], "types": list(store.EXTRACT_TYPES)}
    for r in await cur.fetchall():
        d = dict(r)
        try:
            d["extract_data"] = json.loads(d.get("extract_data") or "{}")
        except ValueError:
            pass
        try:
            d["source_refs"] = json.loads(d.get("source_refs") or "[]")
        except ValueError:
            d["source_refs"] = []
        items.append(d)
    return {"doc_id": doc_id, "project_id": doc["project_id"],
            "items": items, "types": list(store.EXTRACT_TYPES)}


@router.get("/documents/{doc_id}/chunks")
async def get_chunks(
    doc_id: str,
    chunk_type: str = Query(""),
    page_num: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
):
    """分块查询（可追溯性核验入口）：每块带 source_ref，可回溯原文页码/段落。"""
    await _load_doc(db, doc_id)
    # ✅ BUG 修复（分页总数失真）：旧实现 COUNT 只按 doc_id 过滤，而 items 查询额外
    #    带 chunk_type / page_num 条件 —— 一旦按类型或页码筛选，返回的 total 仍是
    #    全文档块总数（虚高），前端据此算出的页数偏大、翻页出现空页。现让 COUNT 与
    #    items 复用同一套 WHERE 过滤条件，total 才是「符合筛选的真实块数」。
    where = "WHERE doc_id=?"
    filter_params: list = [doc_id]
    if chunk_type:
        where += " AND chunk_type=?"
        filter_params.append(chunk_type)
    if page_num:
        where += " AND page_num=?"
        filter_params.append(page_num)
    cur = await db.execute(
        f"SELECT COUNT(*) AS n FROM doc_chunks {where}", filter_params)
    # ✅ P1：COUNT 返回 None 时降级为 0（不抛异常），避免 total 为 None
    #    让前端分页计算负数或报错。
    total = int(((await cur.fetchone()) or ["0"])[0] or 0) if cur is not None else 0
    sql = ("SELECT chunk_id, chunk_type, title, level, page_num, text,"
           " source_ref, tables_json, images_json, hash, meta_json"
           f" FROM doc_chunks {where}"
           " ORDER BY chunk_id LIMIT ? OFFSET ?")
    params: list = filter_params + [limit, offset]
    cur = await db.execute(sql, params)
    items = []
    # ✅ P1：同上，列表查询失败时返回空列表而非 500
    if cur is None:
        logger.warning("查询文档分块失败（db.execute 返回 None）")
        return {"doc_id": doc_id, "total": 0, "items": []}
    for r in await cur.fetchall():
        d = dict(r)
        # 列表默认截断正文（预览用），完整内容按 chunk_id 单查即可
        text = d.get("text") or ""
        d["text_preview"] = text[:300]
        d["text_length"] = len(text)
        d.pop("text", None)
        items.append(d)
    return {"doc_id": doc_id, "total": total, "items": items}


# ---------------------------------------------------------------------------
# 增量更新（指纹比对 → 未变更跳过）
# ---------------------------------------------------------------------------

@router.post("/documents/{doc_id}/reparse")
async def reparse_document(doc_id: str, body: dict | None = None,
                           db=Depends(get_db)):
    """增量重解析：mode=incremental（默认）指纹未变直接跳过；force=true 强制。

    复用全局事实模块的解析主链路（parse_document），在其之上刷新四层产物。
    """
    from app.routers.global_facts import parse_document as _parse_document

    body = body or {}
    mode = str(body.get("mode") or "incremental")
    force = bool(body.get("force")) or mode == "force"
    doc = await _load_doc(db, doc_id)

    # 指纹比对（时效性 + 增量）：原始文件未变且已解析成功 → 跳过
    path = _doc_project_root(doc)
    current_md5 = ""
    if path and await asyncio.to_thread(path.exists):
        content = await asyncio.to_thread(path.read_bytes)
        current_md5 = store.file_fingerprints(content)["md5"]
    stored_md5 = doc.get("file_hash_md5") or ""
    unchanged = bool(current_md5) and stored_md5 == current_md5
    if not force and unchanged and (doc.get("parsed_markdown") or "") \
            and doc.get("parse_status") == "success":
        return {"ok": True, "skipped": True, "reason": "文件指纹未变更，跳过重新解析",
                "parse_version": doc.get("parse_version") or "v1",
                "file_hash_md5": stored_md5}

    # 走既有解析链路（内部完成 parse + 四层落盘 + 版本递增）
    result = await _parse_document(doc_id, force=True, db=db)
    result.setdefault("skipped", False)
    result["reparse_mode"] = "force" if force else "incremental"
    result["file_changed_detected"] = bool(current_md5) and not unchanged
    return result


# ---------------------------------------------------------------------------
# 完整性 / 时效性
# ---------------------------------------------------------------------------

@router.get("/documents/{doc_id}/completeness")
async def document_completeness(doc_id: str, refresh: bool = False,
                                db=Depends(get_db)):
    """完整性校验：解析/表格/图片/字段覆盖率 + 质量评分（可重复执行）。"""
    doc = await _load_doc(db, doc_id)
    if not refresh:
        cur = await db.execute(
            "SELECT report_json, created_at FROM doc_validation_reports"
            " WHERE doc_id=? AND kind='completeness'"
            " ORDER BY created_at DESC LIMIT 1", (doc_id,))
        # ✅ P1（R13 漏改点 · 2026-09-29）：同文件 _load_doc / get_extractions /
        #    get_chunks 四处均已对 db.execute() 返回 None 加守卫，唯独此处漏改。
        #    命中即 AttributeError → 500，而本端点正是「解析质量体检」入口，
        #    短暂连接故障会让用户无法看到覆盖率与缺失字段。
        #    语义选择：按「无可用缓存」处理（继续实时计算）而非报错，
        #    与 get_extractions 的 fail-soft 降级一致（缓存是加速而非唯一来源）。
        if cur is None:
            logger.warning("查询完整性缓存失败（db.execute 返回 None），改为实时计算")
        row = await cur.fetchone() if cur is not None else None
        if row:
            try:
                report = json.loads(row["report_json"])
            except ValueError:
                report = None
            if report:
                report["cached_at"] = row["created_at"]
                return report
    report = await pipeline.build_completeness_report(
        db, doc_id=doc_id, project_id=doc["project_id"],
        total_pages_hint=int(doc.get("page_count") or 0))
    # ✅ 响应结构一致（2026-09-25）：缓存命中分支会注入 cached_at，实时计算
    #    分支此前没有该字段 —— 前端/调用方无法用统一形状判读「是否走缓存」。
    #    现两分支均携带：cached_at=None 表示实时计算（generated_at 为计算时刻）。
    report.setdefault("cached_at", None)
    return report


@router.get("/documents/{doc_id}/freshness")
async def document_freshness(doc_id: str, db=Depends(get_db)):
    """时效性检查：文件指纹一致性 + 解析结果有效期 + 版本信息。"""
    doc = await _load_doc(db, doc_id)
    meta = await asyncio.to_thread(store.read_meta, doc["project_id"], doc_id)
    meta = meta or {}
    meta.setdefault("parse_status", doc.get("parse_status") or "pending")
    meta.setdefault("parse_version", doc.get("parse_version") or "v1")
    meta.setdefault("parse_time", doc.get("parsed_at") or "")
    meta.setdefault("extract_time", doc.get("extract_time") or "")
    meta.setdefault("expires_at", doc.get("expires_at") or "")
    meta.setdefault("file_hash_md5", doc.get("file_hash_md5") or "")
    meta.setdefault("doc_id", doc_id)
    current_md5 = ""
    path = _doc_project_root(doc)
    if path and await asyncio.to_thread(path.exists):
        content = await asyncio.to_thread(path.read_bytes)
        current_md5 = store.file_fingerprints(content)["md5"]
    fresh = pipeline.compute_freshness(meta, current_md5=current_md5)
    # 状态漂移回写（file_changed / expired 提示下游任务需重跑）
    if fresh["status"] not in ("valid",) and (doc.get("status") or "valid") != fresh["status"]:
        # ✅ P1（R13 漏改点 · 2026-10-04）：本文件 6 处 `db.execute` 中唯一一处
        #    **写路径**未判空 —— 命中时 `await db.execute(...)` 返回 None，随后
        #    的 `db.commit()` 提交的是「上一次成功语句」的残留事务上下文，
        #    而状态漂移回写**静默丢失**（无异常、无日志），表现为
        #    「/freshness 说过期，project_documents.status 却一直是 valid」。
        #    写路径不能像读路径那样 fail-soft 继续，只能明确告警后跳过 commit。
        _fcur = await db.execute("UPDATE project_documents SET status=? WHERE id=?",
                                 (fresh["status"], doc_id))
        if _fcur is None:
            logger.warning("文档状态漂移回写失败（db.execute 返回 None），跳过 commit：%s",
                           doc_id)
            return fresh
        await db.commit()
    return fresh


# ---------------------------------------------------------------------------
# 提取层物化 / 交叉校验
# ---------------------------------------------------------------------------

@router.post("/documents/{doc_id}/sync-extractions")
async def sync_extractions(doc_id: str, db=Depends(get_db)):
    """阶段7 入库与索引：把 AI 提取结果（解析项 + 全局事实）按标准格式
    物化到提取层（doc_extractions 表 + extracted/*.json），人工校正后重调用即可刷新。"""
    doc = await _load_doc(db, doc_id)
    return await pipeline.sync_extract_layer(
        db, doc_id=doc_id, project_id=doc["project_id"])


@router.post("/documents/{doc_id}/cross-check")
async def cross_check(doc_id: str, db=Depends(get_db)):
    """阶段5 交叉校验：多文档事实冲突 + 一致性规则跑批。"""
    doc = await _load_doc(db, doc_id)
    return await pipeline.run_cross_check(db, project_id=doc["project_id"],
                                          doc_id=doc_id)


# ---------------------------------------------------------------------------
# 项目文档索引（全景视图）
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/documents/index")
async def project_documents_index(project_id: str, db=Depends(get_db)):
    """项目文档索引：磁盘 documents_index.json 与 DB 档案合并视图。"""
    cur = await db.execute(
        "SELECT id, file_name, file_type, doc_category, file_size,"
        " parse_status, parse_version, extract_status, page_count,"
        " quality_score, file_hash_md5, expires_at, status, created_at"
        " FROM project_documents WHERE project_id=? ORDER BY created_at, id",
        (project_id,))
    # ✅ P1（R13 漏改点 · 2026-09-29）：与同文件 _load_doc / get_extractions /
    #    get_chunks / document_completeness 同口径补判空。本端点是前端「资料列表」
    #    的数据源，命中 None → AttributeError → 500，用户看不到项目资料全景。
    #    语义选择：空索引（fail-soft）—— 磁盘索引仍照常合并返回，
    #    调用方据此可区分「数据库暂不可用」与「项目确无资料」。
    rows: list = []
    if cur is not None:
        rows = [dict(r) for r in await cur.fetchall()]
    else:
        logger.warning("查询项目文档索引失败（db.execute 返回 None），返回空索引")
    disk = await asyncio.to_thread(store.read_json, store.index_path(project_id))
    disk_map = {}
    if isinstance(disk, list):
        disk_map = {it.get("doc_id"): it for it in disk if isinstance(it, dict)}
    for r in rows:
        d = disk_map.pop(r["id"], None) or {}
        r["layers_on_disk"] = bool(d)
        r["disk_updated_at"] = d.get("updated_at", "")
    return {"project_id": project_id, "documents": rows,
            "orphan_disk_entries": list(disk_map.values()),
            "count": len(rows)}
