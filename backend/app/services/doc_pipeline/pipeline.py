"""项目资料解析全流程编排（阶段1上传 → 阶段2解析 → 阶段3分块 → 阶段4提取 →
阶段5交叉校验 → 阶段6人工校正 → 阶段7入库索引）

职责（对应《文件上传解析结果存储格式与项目功能提取工作流程规范》）：

1. **双格式存储 / 四层落盘**：原始文件保真 + 解析层（Markdown/分页/表格/图片）、
   提取层（七类结构化结果）、语义层（chunk 索引）分别落盘；
2. **时效性**：MD5/SHA256 指纹入库，重解析前比对指纹判定是否变更；
   parse_version 版本代次管理；expires_at 过期检测；
3. **可追溯**：分块携带 source_ref 持久化到 doc_chunks；提取层结果
   从 doc_extractions / global_facts 汇总，逐条可回溯页码/段落；
4. **完整性/可校验**：completeness 覆盖率统计 + 质量评分 + 冲突项汇总，
   落库 doc_validation_reports 并随 meta 回写。

所有 DB 写入使用调用方传入的连接（与既有路由的事务/commit 模型一致）；
所有文件 IO 走 asyncio.to_thread，不阻塞事件循环；任何一层落盘失败只降级
告警，绝不影响既有上传/解析主链路。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.doc_pipeline import doc_storage as store
from app.services.doc_pipeline.doc_chunker import chunk_document, chunk_row_of
from app.services.doc_pipeline.md_structured import (
    parse_markdown_structured, wrap_parsed_markdown,
)

logger = logging.getLogger("doc_pipeline")

PARSER_VERSION = "parser-v3"
EXTRACT_SCHEMA_VERSION = "extract-v1"

#: bid_analysis_items（阶段4 AI 提取产物）→ 提取层标准类别 的映射
#: （无对应解析项的类别如 boq，留待后续专项提取接入）
_ITEM_TO_EXTRACT_TYPE: dict[str, tuple[str, ...]] = {
    "projectBasicInfo": ("project_info",),
    "schemeBasicInfo": ("project_info",),
    "overviewParams": ("engineering", "design_params"),
    "siteConditions": ("geology",),
    "compilationBasis": ("standards",),
}

#: project_info 必备字段（完整性校验的 required_fields 口径）
#:
#: ✅ BUG 修复（2026-09-24）：旧口径是 ("project_name", "project_code",
#:    "location", "client", "contractor")，但 18 项解析项 projectBasicInfo 落库
#:    的真实键名见 backend/app/services/bid_analysis_service.py::_ITEM_PROMPTS
#:    —— 只有 project_name / contractor 命中，project_code / location / client
#:    三项在已落库数据里**永远不存在**（真实键为 project_number /
#:    project_location / construction_unit）。后果：completeness.field_coverage
#:    恒 ≤ 0.4，missing_fields 恒报 3 个幻影缺失字段，进而拉低 quality_score
#:    并让「完整性报告」对人工校正完全失去指导意义。
#:
#: 每项写成「规范键 + 历史别名」元组：命中任一即视为已提取，缺失时只报规范键名。
#: 这样既对上真实数据，又兼容历史库里可能存在的别名键（向后兼容）。
_REQUIRED_PROJECT_INFO_FIELDS: tuple[tuple[str, ...], ...] = (
    ("project_name",),
    ("project_number", "project_code"),
    ("construction_unit", "client"),
    ("contractor",),
    ("project_location", "location"),
)


def _field_value(vals: dict, aliases: tuple[str, ...]):
    """按「规范键 + 历史别名」取字段值；值是三要素结构时取 value。"""
    for key in aliases:
        v = vals.get(key)
        if isinstance(v, dict):
            v = v.get("value")
        if v not in (None, "", "null"):
            return v
    return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jdump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


async def _safe_io(fn, *args, **kwargs):
    """文件层写入失败不阻断主链路（DB 状态优先，磁盘产物可经 reparse 重建）。"""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception:
        logger.exception("四层存储写入失败（不影响主流程）: %s",
                         getattr(fn, "__name__", fn))
        return None


# ---------------------------------------------------------------------------
# 行读取工具（aiosqlite.Row 没有 .get()，统一转 dict 再取值）
# ---------------------------------------------------------------------------

def _row_get(row: Any, key: str, default=None):
    try:
        if key in row.keys():
            v = row[key]
            return default if v is None else v
    except Exception:
        pass
    return default


# ---------------------------------------------------------------------------
# 阶段1：文件上传 —— 原文层入库（指纹 + meta + 索引）
# ---------------------------------------------------------------------------

async def ingest_upload(*, doc_id: str, project_id: str, file_name: str,
                        file_type: str, saved_path: Path, size: int,
                        doc_category: str = "",
                        supersedes: str | None = None) -> dict:
    """原文层落盘登记：计算 MD5/SHA256、写 meta、拷贝原件、更新文档索引。

    返回 {"md5","sha256","page_count":0,...}，调用方将指纹写入
    project_documents 对应行（时效性判定的依据）。
    """
    def _sync() -> dict:
        content = saved_path.read_bytes()
        fp = store.file_fingerprints(content)
        meta = store.build_meta(
            doc_id=doc_id, project_id=project_id, file_name=file_name,
            file_type=file_type, file_size=size,
            md5=fp["md5"], sha256=fp["sha256"],
            doc_category=doc_category, supersedes=supersedes)
        # 原件双份保存：原始上传路径（FACT_UPLOADS_DIR，兼容既有）+ 四层 raw/
        raw_dir = store.layer_dir(project_id, doc_id, store.LAYER_RAW)
        raw_dir.mkdir(parents=True, exist_ok=True)
        dest = raw_dir / f"{doc_id}_original{saved_path.suffix}"
        if not dest.exists():
            shutil.copy2(saved_path, dest)
        store.write_meta(project_id, doc_id, meta)
        store.update_index(project_id, _index_entry(meta))
        return meta

    meta = await _safe_io(_sync) or {}
    return meta


def _index_entry(meta: dict) -> dict:
    """documents_index.json 的精简档案行。"""
    return {
        "doc_id": meta.get("doc_id", ""),
        "file_name": meta.get("file_name", ""),
        "file_type": meta.get("file_type", ""),
        "doc_category": meta.get("doc_category", ""),
        "file_hash_md5": meta.get("file_hash_md5", ""),
        "parse_status": meta.get("parse_status", ""),
        "parse_version": meta.get("parse_version", ""),
        "extract_status": meta.get("extract_status", ""),
        "page_count": meta.get("page_count", 0),
        "quality_score": meta.get("quality_score"),
        "expires_at": meta.get("expires_at", ""),
        "updated_at": _now(),
    }


# ---------------------------------------------------------------------------
# 阶段2+3：解析落盘（解析层）+ 分块入库（chunk 层）
# ---------------------------------------------------------------------------

async def ingest_parse_result(db, *, doc_id: str, project_id: str,
                              file_name: str, markdown: str,
                              page_count: int, parse_duration_s: float,
                              parse_engine: str,
                              warnings: list[str] | None = None,
                              prev_version: str = "") -> dict:
    """解析成功后：写解析层四份产物 + front-matter Markdown、分块入 doc_chunks、
    回写 meta 与 project_documents 时效列。

    ✅ 口径修正（2026-09-25）：存储侧为「全删全插」+ 确定性 chunk_id 保证幂等；
    块级指纹 diff 用于两点 —— ① 统计 changed_chunks（增量/审计判断）；
    ② **未变更块继承旧 created_at**（时间线连续，不再每次重解析整体刷新）。
    不做真正的「未变更块跳过重插」：doc_chunks 的 prev/next 链与全局块序在
    内容变更时会整体变化，跳插需同步维护链引用，收益（省几次 INSERT）远低于
    复杂度与出错面，故明确按全量重建实现（此前注释声称保留、实现却全删全插，
    口径不符误导维护者）。

    prev_version 为空 = 首次解析（v1）；非空 = 重解析（代次递增）。
    返回 {"parse_version", "chunk_count", "changed_chunks", ...}。
    """
    parse_version = store.bump_parse_version(prev_version) if prev_version else "v1"
    structured = parse_markdown_structured(markdown, doc_id=doc_id)
    structured["page_count"] = max(structured.get("page_count", 0), page_count)
    chunks = chunk_document(markdown, doc_id=doc_id, structured=structured)

    # ---- DB：块级指纹 diff（规范 §4.3）----
    #    hash diff 的两个用途：① 统计 changed_chunks；② 未变更块继承旧
    #    created_at（时间线连续）。存储侧仍是「全删全插」+ 确定性 chunk_id
    #    幂等；不做真正的跳插（prev/next 链会随内容变更整体重建，见 docstring）。
    cur = await db.execute("SELECT chunk_id, hash, created_at FROM doc_chunks"
                           " WHERE doc_id=?", (doc_id,))
    # ✅ P1（R13 漏改点 · 2026-10-04）：同模块路由层 doc_pipeline.py 的 5 处
    #    （_load_doc / get_extractions / get_chunks / completeness / sync）均已对
    #    `db.execute()` 返回 None 加守卫，唯独本服务层函数漏改 —— 而它正是
    #    「解析 → 分块落库」的唯一入口（global_facts.parse_document →
    #    _ingest_parsed_doc → ingest_parse_result）。命中即 AttributeError，
    #    再被 _ingest_parsed_doc 的 `except Exception: return {}` 整段吞掉：
    #    doc_chunks 全丢、响应里 layers 字段消失，用户与日志都只能看到「解析成功」。
    #    语义选择：按「无历史块」处理（全量重建，old_rows 为空 → 全部计为 changed，
    #    created_at 全部取当前时刻），与「缓存是加速而非唯一来源」的既有降级口径一致。
    if cur is None:
        logger.warning("查询历史分块失败（db.execute 返回 None），按无历史块处理：%s", doc_id)
        old_rows = {}
    else:
        old_rows = {r["chunk_id"]: (r["hash"], r["created_at"] or "")
                    for r in await cur.fetchall()}
    old_hashes = {cid: h for cid, (h, _t) in old_rows.items()}
    new_ids = {c["chunk_id"] for c in chunks}
    changed = [c for c in chunks
               if old_hashes.get(c["chunk_id"]) != c["hash"]]
    # 删除已不存在的块（文件变更后旧块自然失配）
    await db.execute(
        "DELETE FROM doc_chunks WHERE doc_id=?", (doc_id,))
    created = _now()
    if chunks:
        await db.executemany(
            "INSERT INTO doc_chunks (chunk_id, doc_id, chunk_type, title, level,"
            " page_num, text, source_ref, parent_chunk_id, prev_chunk_id,"
            " next_chunk_id, tables_json, images_json, hash, meta_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [_patched_row(c, created, old_rows) for c in chunks])

    # ---- 解析层文件 ----
    pages_payload = {
        "doc_id": doc_id,
        "page_count": structured["page_count"],
        "pages": [{k: p[k] for k in
                   ("page_num", "text", "tables", "images")} |
                  {"bbox": None, "rotation": 0, "header": "", "footer": ""}
                  for p in structured["pages"]],
    }
    tables_payload = {"doc_id": doc_id,
                      "table_count": structured["table_count"],
                      "tables": structured["tables"]}
    images_payload = {"doc_id": doc_id,
                      "image_count": structured["image_count"],
                      "images": structured["images"]}
    md_with_front = wrap_parsed_markdown(doc_id, file_name, markdown,
                                         structured["page_count"])
    await _safe_io(store.write_parsed_layer, project_id, doc_id,
                   markdown=md_with_front, pages=pages_payload,
                   tables=tables_payload, images=images_payload)

    # ---- meta / 索引 / 时效性回写 ----
    meta = await _safe_io(store.read_meta, project_id, doc_id) or {}

    def _finish_sync(meta_obj: dict) -> dict:
        m = dict(meta_obj or {})
        base = store.build_meta(
            doc_id=doc_id, project_id=project_id, file_name=file_name,
            file_type=m.get("file_type", ""), file_size=m.get("file_size", 0),
            md5=m.get("file_hash_md5", ""), sha256=m.get("file_hash_sha256", ""),
            doc_category=m.get("doc_category", ""))
        base.update({k: v for k, v in m.items() if v not in (None, "")})
        base.update({
            "page_count": structured["page_count"],
            "parse_status": "success",
            "parse_version": parse_version,
            "parse_time": _now(),
            "parse_duration_ms": int(parse_duration_s * 1000),
            "parse_engine": f"{parse_engine}+{PARSER_VERSION}",
            "parse_warnings": warnings or [],
        })
        store.write_meta(project_id, doc_id, base)
        store.update_index(project_id, _index_entry(base))
        store.write_semantic_index(project_id, doc_id, chunks=chunks)
        return base

    await _safe_io(_finish_sync, meta)
    # ✅ BUG 修复（指纹蒸发）：旧实现无条件用磁盘 meta 里的哈希回写 DB ——
    #    当 meta 丢失/写入失败（_safe_io 降级返回 None）时，会把上传阶段已
    #    落在 project_documents 的 MD5/SHA256 清空，时效性判定（file_changed /
    #    增量跳过）从此永久失真且无法自愈。现改为「新值非空才覆盖」。
    await db.execute(
        "UPDATE project_documents SET parse_status='success',"
        " parse_duration_ms=?,"
        " parse_version=COALESCE(NULLIF(?, ''), parse_version),"
        " parsed_at=?,"
        " file_hash_md5=COALESCE(NULLIF(?, ''), file_hash_md5),"
        " file_hash_sha256=COALESCE(NULLIF(?, ''), file_hash_sha256),"
        " page_count=?,"
        " parse_engine=?, completeness_json=?, status='valid' "
        "WHERE id=?",
        (int(parse_duration_s * 1000), parse_version, _now(),
         meta.get("file_hash_md5", ""), meta.get("file_hash_sha256", ""),
         structured["page_count"], f"{parse_engine}+{PARSER_VERSION}",
         _jdump(_completeness_snapshot(structured, chunks, None)), doc_id))
    await db.commit()
    return {
        "parse_version": parse_version,
        "chunk_count": len(chunks),
        "changed_chunks": len(changed) if old_hashes else len(chunks),
        "table_count": structured["table_count"],
        "image_count": structured["image_count"],
        "formula_count": structured.get("formula_count", 0),
        "page_count": structured["page_count"],
    }


def _patched_row(c: dict, created: str,
                 old_rows: dict[str, tuple[str, str]] | None = None) -> tuple:
    """chunk dict → doc_chunks 插入行；内容未变更（同 id 同 hash）的块
    继承旧 created_at，保持入库时间线连续（增量审计可区分新旧块）。"""
    if old_rows:
        old_hash, old_created = old_rows.get(c["chunk_id"], ("", ""))
        if old_created and old_hash == c.get("hash", ""):
            created = old_created
    return chunk_row_of(c, created_at=created)


# ---------------------------------------------------------------------------
# 阶段5 辅助：完整性 / 时效性
# ---------------------------------------------------------------------------

def _completeness_snapshot(structured: dict, chunks: list[dict],
                           extractions: dict | None) -> dict:
    """解析层覆盖率快照（提取字段覆盖在 build_completeness_report 里合并）。"""
    return {
        "parsed_pages": structured.get("page_count", 0),
        "total_tables": structured.get("table_count", 0),
        "extracted_tables": structured.get("table_count", 0),
        "total_images": structured.get("image_count", 0),
        "ocr_images": sum(1 for im in structured.get("images", [])
                          if im.get("ocr_text")),
        "total_formulas": structured.get("formula_count", 0),
        "chunk_count": len(chunks),
    }


async def build_completeness_report(db, *, doc_id: str, project_id: str,
                                    total_pages_hint: int = 0) -> dict:
    """完整性校验报告（规范 §4.1）：解析/表格/图片/字段覆盖率 + 质量评分。"""
    cur = await db.execute(
        "SELECT file_name, page_count, parsed_markdown, parse_status,"
        " parse_warnings, completeness_json FROM project_documents WHERE id=?",
        (doc_id,))
    row = await cur.fetchone()
    if not row:
        return {"doc_id": doc_id, "errors": ["文档不存在"]}
    snap = {}
    try:
        snap = json.loads(_row_get(row, "completeness_json") or "{}")
    except ValueError:
        snap = {}
    warnings = []
    try:
        warnings = json.loads(_row_get(row, "parse_warnings") or "[]")
        if not isinstance(warnings, list):
            warnings = []
    except ValueError:
        warnings = []

    cur = await db.execute(
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN source_ref != '' THEN 1 ELSE 0 END) AS traced "
        "FROM doc_chunks WHERE doc_id=?", (doc_id,))
    r = await cur.fetchone()
    chunk_count = int((r["n"] if r else 0) or 0)
    traced_chunks = int((r["traced"] if r else 0) or 0)

    cur = await db.execute(
        # ✅ BUG 修复（2026-09-29 · 陈旧行虚高覆盖率）：sync_extract_layer 会把
        #    「本轮已无源」的提取类别标记为 status='stale'（保留数据以便回溯）。
        #    若此处不过滤，被清空的项目基本信息仍以旧内容计入字段覆盖率，
        #    完整性报告与「提取结果已被清空」的实际状态分叉。
        "SELECT extract_type, extract_data FROM doc_extractions"
        " WHERE doc_id=? AND COALESCE(status,'') != 'stale'",
        (doc_id,))
    ext_rows = {r2["extract_type"]: r2["extract_data"] for r2 in await cur.fetchall()}

    # 字段覆盖率：project_info 必备字段是否有值
    extracted_fields = required_fields = 0
    missing_fields: list[str] = []
    if "project_info" in ext_rows:
        try:
            payload = json.loads(ext_rows["project_info"] or "{}")
            info = payload.get("project_info", payload) if isinstance(payload, dict) else {}
            vals = info.get("project_info") if isinstance(info, dict) else None
            vals = vals if isinstance(vals, dict) else (info if isinstance(info, dict) else {})
            required_fields = len(_REQUIRED_PROJECT_INFO_FIELDS)
            for aliases in _REQUIRED_PROJECT_INFO_FIELDS:
                if _field_value(vals, aliases) is not None:
                    extracted_fields += 1
                else:
                    # 缺失只报规范键名（aliases[0]），历史别名不进缺失清单
                    missing_fields.append(aliases[0])
        except ValueError:
            pass

    total_pages = max(int(_row_get(row, "page_count", 0) or 0), total_pages_hint,
                      int(snap.get("parsed_pages") or 0))
    parsed_pages = int(snap.get("parsed_pages") or 0)
    parse_coverage = min(parsed_pages / total_pages, 1.0) if total_pages else (
        1.0 if parsed_pages else 0.0)
    chunk_traced = (traced_chunks / chunk_count) if chunk_count else 0.0
    field_coverage = (extracted_fields / required_fields) if required_fields else None

    # 冲突项（交叉校验阶段5的产物，global_facts.has_conflict）
    # ⚠️ 作用域说明（2026-09-25 显式化）：冲突登记在 global_facts（项目级变量
    #    表），本身不带 doc 维度，因此该计数是**整个项目**的冲突事实数，而非
    #    本文档独有 —— 单文档视角读该值会高估。字段保留原 key（消费端兼容），
    #    新增 conflict_scope 字段供调用方判读，不再靠约定。
    conflict_count = 0
    try:
        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM global_facts "
            "WHERE project_id=? AND has_conflict=1", (project_id,))
        conflict_count = int(((await cur.fetchone()) or ["0"])[0] or 0)
    except Exception:
        pass

    scores = [parse_coverage, chunk_traced]
    if field_coverage is not None:
        scores.append(field_coverage)
    quality = round(sum(scores) / len(scores), 3) if scores else 0.0
    report = {
        "doc_id": doc_id,
        "file_name": _row_get(row, "file_name", ""),
        "completeness": {
            "total_pages": total_pages,
            "parsed_pages": parsed_pages,
            "parse_coverage": round(parse_coverage, 3),
            "total_tables": int(snap.get("total_tables") or 0),
            "extracted_tables": int(snap.get("extracted_tables") or 0),
            "table_coverage": 1.0 if not snap.get("total_tables") else round(
                int(snap.get("extracted_tables") or 0) / int(snap.get("total_tables") or 1), 3),
            "total_images": int(snap.get("total_images") or 0),
            "ocr_images": int(snap.get("ocr_images") or 0),
            "image_coverage": 1.0 if not snap.get("total_images") else round(
                int(snap.get("ocr_images") or 0) / int(snap.get("total_images") or 1), 3),
            "total_formulas": int(snap.get("total_formulas") or 0),
            "chunk_count": chunk_count,
            "required_fields": required_fields,
            "extracted_fields": extracted_fields,
            "field_coverage": field_coverage,
            "missing_fields": missing_fields,
            "conflict_count": conflict_count,
            "conflict_scope": "project",  # 冲突计数作用域：项目级（global_facts 不带 doc 维度）
        },
        "quality_score": quality,
        "warnings": [str(w) for w in warnings],
        "errors": [] if _row_get(row, "parse_status") == "success"
        else ["文档尚未解析成功"],
        "generated_at": _now(),
    }
    # 落库（doc_validation_reports）+ meta 回写质量分
    await db.execute(
        "INSERT INTO doc_validation_reports (id, doc_id, project_id, kind,"
        " report_json, created_at) VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), doc_id, project_id, "completeness",
         _jdump(report), _now()))
    # ✅ BUG 修复（质量分永远为空的根因）：project_documents.quality_score 列
    #    默认 -1（未评估哨兵），旧实现完整性报告只把质量分写进【磁盘 meta】，
    #    从不回写该 DB 列。而 GET /documents/{id}/status 读的正是 DB 列 —— 于是
    #    即便已执行完整性校验，状态接口的 quality_score 恒为 -1（前端显示「未评估」），
    #    四层质量能力等于白做。现随报告一并回写 DB 列，打通「校验→状态」数据链。
    await db.execute(
        "UPDATE project_documents SET quality_score=? WHERE id=?",
        (quality, doc_id))
    await db.commit()

    # ✅ BUG 修复（事件循环卫生，2026-09-20）：下方 meta 读写此前是【同步】磁盘 IO
    #    直接内联在 async 函数里 —— 违反本模块"所有文件 IO 走 asyncio.to_thread"
    #    的自身约定（doc_pipeline.py 模块头与 _safe_io 均为此设计）。大项目索引
    #    documents_index.json 可达数百 KB，每次 completeness 查询都会阻塞事件循环。
    #    现统一走 _safe_io（to_thread + 失败降级），与 ingest_parse_result 等路径一致。
    def _quality_meta_sync() -> None:
        meta = store.read_meta(project_id, doc_id) or {}
        if not meta:
            return
        meta["quality_score"] = quality
        store.update_index(project_id, _index_entry(meta))
        store.write_meta(project_id, doc_id, meta)

    await _safe_io(_quality_meta_sync)
    return report


def compute_freshness(meta: dict, *, current_md5: str = "") -> dict:
    """时效性判定（规范 §4.2）：valid / expired / file_changed / not_parsed。"""
    status = "valid"
    reasons: list[str] = []
    if (meta.get("parse_status") or "") != "success":
        status = "not_parsed"
        reasons.append("文档尚未成功解析")
    if current_md5 and meta.get("file_hash_md5") and \
            current_md5 != meta["file_hash_md5"]:
        status = "file_changed"
        reasons.append("源文件指纹与解析时不一致，需要重新解析")
    expires = (meta.get("expires_at") or "").strip()
    if status == "valid" and expires:
        try:
            exp_dt = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
            if exp_dt < datetime.now(timezone.utc):
                status = "expired"
                reasons.append(f"解析结果已于 {expires} 过期")
        except ValueError:
            pass
    return {
        "doc_id": meta.get("doc_id", ""),
        "status": status,
        "reasons": reasons,
        "parse_version": meta.get("parse_version", ""),
        "parse_time": meta.get("parse_time", ""),
        "extract_time": meta.get("extract_time", ""),
        "expires_at": expires,
        "file_hash_md5": meta.get("file_hash_md5", ""),
        "checked_at": _now(),
    }


# ---------------------------------------------------------------------------
# 阶段4/7：提取层物化（把 AI 提取结果按标准格式落盘 + 入 doc_extractions）
# ---------------------------------------------------------------------------

async def sync_extract_layer(db, *, doc_id: str, project_id: str) -> dict:
    """把平台既有的 AI 提取产物（bid_analysis_items + global_facts）按规范
    §2.3 的提取层格式物化：写 extracted/*.json + 入 doc_extractions 表 +
    回写 extract 状态/时间。人工校正（阶段6）修改后重新调用即可刷新。

    ⚠️ 下游消费边界（数据流审计 2026-09-23 文档化）：本函数是单向「归档物化」，
    目录/正文生成的实时源仍为 bid_analysis_items 与 global_facts 两张活跃表，不读取
    本层（doc_extractions / extracted/*.json）产物；本层仅供完整性报告、交叉校验与未来回溯。
    """
    # 1) 项目级 AI 解析项 → 按 doc 归档（现阶段项目资料的 AI 结构化提取统一来源）
    cur = await db.execute(
        "SELECT item_id, label, output_type, content, status, updated_at "
        "FROM bid_analysis_items WHERE project_id=? AND status='success'",
        (project_id,))
    items = [dict(r) for r in await cur.fetchall()]
    # ✅ BUG 修复（2026-09-29 · 失败哨兵被物化）：旧实现只按 status='success' 过滤，
    #    而 bid_analysis 在「全部分段无有效结果」时写入 status='success' + content='{}'
    #    的失败哨兵（见 routers/bid_analysis.py 的 _run_single_item / _repair_json）。
    #    哨兵被物化后，完整性报告会把一个空壳 JSON 当成有效提取结果计入字段覆盖率，
    #    与同表其它消费方（/bid-analysis/results、format_downstream_context）经
    #    is_missing_result 判定的「缺失」口径分叉。现复用同一判据过滤（fail-soft：
    #    判定函数不可用时退化为不过滤，保持旧行为）。
    try:
        from app.services.bid_analysis_service import is_missing_result
    except Exception:  # pragma: no cover - 导入失败时退化为不过滤（旧行为）
        is_missing_result = None
    if is_missing_result is not None:
        items = [it for it in items
                 if not is_missing_result(it.get("content") or "",
                                          it.get("output_type") or "markdown")]
    by_type: dict[str, list[dict]] = {}
    for it in items:
        for t in _ITEM_TO_EXTRACT_TYPE.get(it["item_id"], ()):
            by_type.setdefault(t, []).append(it)

    # 2) 全局事实 → global_facts 提取层文件（保留 source_ref 溯源）
    cur = await db.execute(
        "SELECT group_id, group_title, title, content, category, source_ref,"
        " confidence, is_simulated, is_resolved, has_conflict "
        "FROM global_facts WHERE project_id=?", (project_id,))
    fact_rows = [dict(r) for r in await cur.fetchall()]
    facts_payload = {
        "doc_id": doc_id,
        "extract_time": _now(),
        "extract_schema_version": EXTRACT_SCHEMA_VERSION,
        "global_facts": [
            {
                "group_id": f.get("group_id") or f.get("category") or "other",
                "title": f.get("group_title") or f.get("title") or "",
                "content": f.get("content") or "",
                "confidence": float(f.get("confidence") or 0),
                "is_simulated": bool(f.get("is_simulated")),
                "is_resolved": bool(f.get("is_resolved")),
                "has_conflict": bool(f.get("has_conflict")),
                "source": f.get("source_ref") or "",
            } for f in fact_rows
        ],
    }

    written_types: list[str] = []
    now = _now()
    for ext_type in store.EXTRACT_TYPES:
        if ext_type in store.RESERVED_EXTRACT_TYPES:
            # 预留类别（如 boq）：当前无解析项映射，永不物化（显式跳过，
            # 语义与 by_type 为空时相同，但把「预留」契约落到跳过点上）
            continue
        if ext_type == "global_facts":
            payload = facts_payload
            if not fact_rows:
                continue
        else:
            src_items = by_type.get(ext_type)
            if not src_items:
                continue
            payload = {
                "doc_id": doc_id,
                "extract_time": max(i.get("updated_at") or "" for i in src_items),
                "extract_schema_version": EXTRACT_SCHEMA_VERSION,
                ext_type: _merge_item_contents(ext_type, src_items),
                "source_items": [
                    {"item_id": i["item_id"], "label": i["label"],
                     "output_type": i["output_type"]} for i in src_items],
            }
        await _safe_io(store.write_extraction, project_id, doc_id, ext_type, payload)
        # 入 doc_extractions（INSERT OR REPLACE 以 (doc_id, extract_type) 唯一）
        conf = _avg_confidence(payload)
        await db.execute(
            "INSERT INTO doc_extractions (extraction_id, doc_id, project_id,"
            " extract_type, extract_data, confidence, source_refs,"
            " extract_time, extract_engine, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(doc_id, extract_type) DO UPDATE SET"
            " extract_data=excluded.extract_data,"
            " confidence=excluded.confidence,"
            " source_refs=excluded.source_refs,"
            " extract_time=excluded.extract_time,"
            " extract_engine=excluded.extract_engine,"
            " status=excluded.status",
            (str(uuid.uuid4()), doc_id, project_id, ext_type,
             _jdump(payload), conf,
             _jdump(_collect_source_refs(payload)), now,
             "ai-extractor-v1", "success", now))
        written_types.append(ext_type)
    await db.commit()

    # ✅ BUG 修复（2026-09-29 · 陈旧提取层残留）：旧实现只 upsert 本轮有源的类别，
    #    本轮无源的类别既不更新也不清理。用户清空/删除某类解析项（或全局事实被删空）
    #    后再次物化，doc_extractions 里该类别的旧行仍是 status='success' + 旧内容，
    #    GET /documents/{id}/extractions?type=X 返回「成功」的过期结果，
    #    build_completeness_report 的字段覆盖率随之虚高；同时 project_documents
    #    .extract_status 被写成 'pending'（written_types 为空），与表内 success 行
    #    自相矛盾。现对本轮未写入的非预留类别做陈旧化标记：保留 extract_data 便于
    #    回溯（彻底删除仍由 purge_document 负责），但 status 明确不再是 'success'，
    #    各消费方据此可判定「该类别已无有效提取结果」。
    stale_types = [
        t for t in store.EXTRACT_TYPES
        if t not in store.RESERVED_EXTRACT_TYPES and t not in written_types
    ]
    if stale_types:
        ph = ",".join("?" * len(stale_types))
        try:
            await db.execute(
                f"UPDATE doc_extractions SET status='stale', extract_time=? "
                f"WHERE doc_id=? AND extract_type IN ({ph}) "
                f"AND COALESCE(status,'') != 'stale'",
                (now, doc_id, *stale_types))
            await db.commit()
        except Exception as e:  # noqa: BLE001 - 陈旧化失败不应阻断物化主流程
            logger.warning("标记陈旧提取层失败（doc=%s，不影响物化结果）: %s",
                           doc_id, e)

    extract_status = "success" if written_types else "pending"
    # ✅ 清理（2026-09-20）：此处曾有一条 SELECT doc_category 的查询但结果从未
    #    被使用（纯死查询，每次物化白跑一次 DB）。删除。
    # meta 回写（extract 时间/状态 —— 时效性元数据）
    def _meta_finish(meta_obj: dict | None) -> None:
        m = dict(meta_obj or {})
        if not m:
            return
        m.update({"extract_status": extract_status, "extract_time": now,
                  "extracted_types": written_types})
        store.write_meta(project_id, doc_id, m)
        store.update_index(project_id, _index_entry(m))

    meta = await _safe_io(store.read_meta, project_id, doc_id)
    await _safe_io(_meta_finish, meta)
    await db.execute(
        "UPDATE project_documents SET extract_status=?, extract_time=? WHERE id=?",
        (extract_status, now, doc_id))
    await db.commit()
    return {"doc_id": doc_id, "extract_status": extract_status,
            "written_types": written_types,
            "fact_count": len(fact_rows), "item_count": len(items)}


def _merge_item_contents(ext_type: str, src_items: list[dict]) -> dict:
    """合并多个解析项内容为提取层结构（JSON 项直接展开，Markdown 保留文本）。"""
    merged: dict[str, Any] = {}
    for it in src_items:
        content = it.get("content") or ""
        parsed: Any = None
        if it.get("output_type") == "json":
            try:
                parsed = json.loads(content)
            except ValueError:
                parsed = None
        if isinstance(parsed, dict):
            body = parsed.get(ext_type) if isinstance(parsed.get(ext_type), dict) \
                else parsed
            for k, v in body.items():
                if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
                    merged[k].update(v)
                else:
                    merged[k] = v
        else:
            key = f"{it['item_id']}_markdown"
            merged[key] = content
    # project_info 归一化为 {field: {value, confidence, source}} 三要素结构
    # ✅ 修复（2026-09-25）：Markdown 来源字段没有真实置信度，旧实现硬编码 0.9
    #    会伪装成「AI 高确信」；现按未评估哨兵 0.0（与 _avg_confidence /
    #    doc_extractions.confidence DEFAULT 0 约定一致）。
    if ext_type == "project_info":
        for k, v in list(merged.items()):
            if isinstance(v, str):
                merged[k] = {"value": v, "confidence": _UNASSESSED_CONFIDENCE,
                             "source": ""}
    return merged


#: ✅ 修复（2026-09-25）：载荷中无任何置信度字段时的返回值。旧实现兜底 0.9 ——
#: 用「看似合理的高置信度」掩盖了真实置信度缺失，下游读到 0.9 无法区分
#: 「AI 很确信」与「压根没有评估」。现改为 0.0（未评估哨兵，与
#: doc_extractions.confidence 列 DEFAULT 0 的约定一致）；正常载荷的均值
#: 计算不受影响。全库无按 doc_extractions.confidence 阈值分支的消费点
#: （阈值分支只存在于 global_facts 域），改动安全。
_UNASSESSED_CONFIDENCE = 0.0


def _avg_confidence(payload: dict) -> float:
    try:
        body = next((v for k, v in payload.items()
                     if k not in ("doc_id", "extract_time", "extract_schema_version",
                                  "source_items") and isinstance(v, (dict, list))), None)
        if isinstance(body, list):
            confs = [float(i.get("confidence") or 0) for i in body if isinstance(i, dict)]
            return round(sum(confs) / len(confs), 3) if confs else _UNASSESSED_CONFIDENCE
        if isinstance(body, dict):
            confs = [float(v.get("confidence") or 0) for v in body.values()
                     if isinstance(v, dict) and "confidence" in v]
            return round(sum(confs) / len(confs), 3) if confs else _UNASSESSED_CONFIDENCE
    except Exception:
        pass
    return _UNASSESSED_CONFIDENCE


def _collect_source_refs(payload: dict) -> list[str]:
    refs: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("source", "source_ref") and isinstance(v, str) and v:
                    refs.append(v)
                else:
                    _walk(v)
        elif isinstance(node, list):
            for x in node:
                _walk(x)

    _walk(payload)
    return list(dict.fromkeys(refs))


# ---------------------------------------------------------------------------
# 阶段5：交叉校验（多文档冲突 + 数值一致性，复用 facts_cross_validators）
# ---------------------------------------------------------------------------

def _norm_fact_name(name: str) -> str:
    """归一化事实名用于跨源比对（去空白/小写/去常见标点）。"""
    if not name:
        return ""
    s = name.strip().lower()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[，。、：:（）()·\-_/]", "", s)
    return s


async def detect_cross_source_conflicts(
    db, *, project_id: str,
) -> tuple[list[dict], set[str], set[str]]:
    """P2（2026-09-23）：比对「AI 解析项目」(bid_analysis_items) 与「全局事实」
    (global_facts) 两套真值源，找出同名但取值冲突的事实。

    此前两套数据各自落库/触发/面板，互不交叉校验，导致重复/口径不一。
    返回 (冲突列表, 需标记 has_conflict 的 global_facts id 集合,
    取值已与解析项一致的 global_facts id 集合)。第三项用于消解「此前由双源
    冲突置位、解析项现已改一致」的残留标记（fail-closed 口径下不能无证据清零，
    取值一致的同名解析项即消解证据）。
    仅程序判定、零 LLM；裁决权在人工（auto_resolvable=False）。
    """
    cur = await db.execute(
        "SELECT id, label, content FROM bid_analysis_items "
        "WHERE project_id=? AND status='success'", (project_id,))
    bid_rows = [dict(r) for r in await cur.fetchall()]

    cur = await db.execute(
        "SELECT id, title, content FROM global_facts WHERE project_id=?", (project_id,))
    gf_rows = [dict(r) for r in await cur.fetchall()]

    def _value(content: str) -> str:
        c = (content or "")
        return c.split(":", 1)[-1].strip().lstrip("- ").strip() if ":" in c else c.strip()

    # norm_name -> [ (source, raw_name, value, gf_id|None) ]
    by_name: dict[str, list[tuple[str, str, str, str | None]]] = {}
    for r in bid_rows:
        nm = _norm_fact_name(r.get("label") or "")
        if nm:
            by_name.setdefault(nm, []).append(
                ("bid_analysis", r.get("label") or "", _value(r.get("content")), None))
    for r in gf_rows:
        nm = _norm_fact_name(r.get("title") or "")
        if not nm:
            continue
        by_name.setdefault(nm, []).append(
            ("global_facts", r.get("title") or "", _value(r.get("content")), r["id"]))

    conflicts: list[dict] = []
    flagged: set[str] = set()
    resolved: set[str] = set()
    for nm, entries in by_name.items():
        bid_vals = {e[2] for e in entries if e[0] == "bid_analysis"}
        gf_entries = [e for e in entries if e[0] == "global_facts"]
        if not bid_vals or not gf_entries:
            continue
        bid_val = next(iter(bid_vals))
        for ge in gf_entries:
            if any(bv != ge[2] for bv in bid_vals):
                conflicts.append({
                    "rule_id": "XV-SRC-DUP",
                    "severity": "medium",
                    "conflict_type": "cross_source_value_mismatch",
                    "source_a": {"source": "bid_analysis",
                                 "name": ge[1], "value": bid_val},
                    "source_b": {"source": "global_facts",
                                 "name": ge[1], "value": ge[2]},
                    "resolution_hint": "AI 解析项目与全局事实同名取值不一致，需确认权威来源",
                    "auto_resolvable": False,
                })
                flagged.add(ge[3])
            elif all(bv == ge[2] for bv in bid_vals):
                # 全部解析项取值与该事实一致 → 若此前由双源冲突置位，现可消解。
                resolved.add(ge[3])
    return conflicts, flagged, resolved


async def run_cross_check(db, *, project_id: str, doc_id: str = "") -> dict:
    """交叉校验报告：global_facts 冲突项汇总 + 跨类别一致性规则跑批。

    平台既有的事实质感校验规则（facts_cross_validators.run_cross_validations）
    以合并后的事实集为输入；这里把项目全部事实喂进去，冲突明细回写
    doc_validation_reports（kind=cross_check）。

    ✅ P7（2026-09-23）：冲突明细**回写 global_facts.has_conflict**，使事后复核
    与提取期程序交叉校验（facts_cross_validators）落库口径一致——旧实现只写
    doc_validation_reports 不回写，导致 has_conflict 标记不刷新。
    ✅ P2（2026-09-23）：额外比对 bid_analysis_items 与 global_facts 两套真值源，
    同名取值冲突一并写入报告并回写 has_conflict。
    """
    from app.services.facts_cross_validators import run_cross_validations

    cur = await db.execute(
        "SELECT id, group_id, title, content, category, fact_key, source_ref,"
        " confidence, is_simulated, has_conflict, conflict_keys "
        "FROM global_facts WHERE project_id=?", (project_id,))
    rows = [dict(r) for r in await cur.fetchall()]

    class _Item:
        def __init__(self, row: dict):
            self.id = row.get("id")
            self.key = row.get("fact_key") or ""
            self.name = row.get("title") or ""
            self.category = row.get("category") or ""
            c = (row.get("content") or "")
            self.value = c.split(":", 1)[-1].strip().lstrip("- ").strip() if ":" in c else c
            self.source = row.get("source_ref") or ""
            # ✅ 修复（P7，2026-09-23）：旧 _Item 缺 confidence 属性，而
            #    facts_cross_validators._side 会读 item.confidence —— 项目存在材料/设计
            #    参数冲突时 run_cross_check 直接 AttributeError 崩溃，冲突回写永不执行。
            #    脏值兜底为 1.0，与全局事实列表口径一致。
            try:
                self.confidence = float(row.get("confidence") or 1.0)
            except (TypeError, ValueError):
                self.confidence = 1.0
            self.has_conflict = bool(row.get("has_conflict"))
            self.conflict_values = []
            try:
                raw = row.get("conflict_keys")
                if raw:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        self.conflict_values = parsed
            except ValueError:
                pass
            # ✅ 记录进入校验前是否携带结构化冲突候选。run_cross_validations
            #    对「范围值兼容」项会就地清空 conflict_values 并撤销标记，借此
            #    区分「经程序判定消解」与「XV 规则盲区（普通文本值冲突，
            #    非材料/流程/机械时序）」两类候选。
            self.had_candidate = bool(self.conflict_values)

    merged = [_Item(r) for r in rows]
    conflicts = run_cross_validations(merged) if merged else []

    # ✅ 冲突标记以校验器判定后的内存条目状态为唯一事实源，不再按「事实名」
    #    二次映射 —— 旧按名映射依赖 side.name 与 DB title 全文相等，会漏标。

    # ✅ P2：双源交叉校验
    cross_src_conflicts, cross_src_flagged, cross_src_resolved = \
        await detect_cross_source_conflicts(db, project_id=project_id)

    # 计算「全局事实内部同名取值分歧」的名称集合：这些名称即便解析项与其中
    # 一个取值一致，gf 内部仍有真实冲突，双源一致不构成消解（安全侧保守）。
    from app.services.facts_cross_validators import _norm_text_value
    name_vals: dict[str, set] = {}
    for it in merged:
        nm = (it.name or "").strip()
        if nm:
            v = _norm_text_value(it.value)
            if v:
                name_vals.setdefault(nm, set()).add(v)
    gf_disputed_names = {nm for nm, vs in name_vals.items() if len(vs) >= 2}

    def _cross_resolvable(it) -> bool:
        # 双源一致且 gf 内部无分歧，才允许据此消解标记。
        return (it.id in cross_src_resolved
                and (it.name or "").strip() not in gf_disputed_names)

    # 最终保留集合按 fail-closed（安全侧保守）口径逐行判定：
    # ① 有候选且经 XV 判定仍冲突：保留（盲区候选 XV 不处理，标记天然在）；
    # ② 无候选但被 XV-SAME-NAME 新判冲突：保留 —— 旧条件误加 had_candidate
    #    限制，把同名文本值冲突行排除后清0，漏报真实冲突；
    # ③ 无候选的初始标记残留：默认保守保留（test_cross_check_persists），
    #    仅当存在「取值已一致且 gf 无分歧」的解析项时才允许消解；
    # ④ 本轮双源命中：一律保留。
    final_keep = set(cross_src_flagged)
    for it in merged:
        if not it.id:
            continue
        if it.has_conflict and not _cross_resolvable(it):
            final_keep.add(it.id)

    # 应消解集合（reset 恒为最终裁决）：
    # ① 范围值兼容：携带候选进入、判定后标记与候选皆被 XV 清空；
    # ② 双源一致消解：取值与解析项一致且 gf 内部无分歧；
    # ③ 有候选但经 XV 判定无冲突且无候选残留（兼容合并同类）。
    reset_ids: set[str] = set()
    for it in merged:
        if not it.id or it.id in final_keep:
            continue
        if it.had_candidate and not it.has_conflict and not it.conflict_values:
            reset_ids.add(it.id)
        elif _cross_resolvable(it):
            reset_ids.add(it.id)

    # 先置 1 再清 0，reset 恒为最终裁决。
    if final_keep:
        ph = ",".join("?" * len(final_keep))
        await db.execute(
            f"UPDATE global_facts SET has_conflict=1 "
            f"WHERE id IN ({ph})", list(final_keep))

    if reset_ids:
        ph = ",".join("?" * len(reset_ids))
        await db.execute(
            f"UPDATE global_facts SET has_conflict=0 "
            f"WHERE id IN ({ph})", list(reset_ids))


    report = {
        "project_id": project_id,
        "doc_id": doc_id,
        "fact_count": len(rows),
        "stored_conflicts": len(final_keep),
        "cross_conflicts": conflicts,
        "cross_source_conflicts": cross_src_conflicts,
        "flagged_fact_ids": sorted(i for i in final_keep if i),
        "checked_at": _now(),
    }
    await db.execute(
        "INSERT INTO doc_validation_reports (id, doc_id, project_id, kind,"
        " report_json, created_at) VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), doc_id, project_id, "cross_check", _jdump(report),
         _now()))
    await db.commit()
    return report


# ---------------------------------------------------------------------------
# 文档删除：四层目录 + 关联 DB 行清理
# ---------------------------------------------------------------------------

async def purge_document(db, *, doc_id: str, project_id: str) -> None:
    await db.execute("DELETE FROM doc_chunks WHERE doc_id=?", (doc_id,))
    await db.execute("DELETE FROM doc_extractions WHERE doc_id=?", (doc_id,))
    await db.execute("DELETE FROM doc_validation_reports WHERE doc_id=?", (doc_id,))
    await db.commit()
    await _safe_io(store.remove_from_index, project_id, doc_id)
    await _safe_io(store.delete_doc_tree, project_id, doc_id)


# ---------------------------------------------------------------------------
# 存量回填：为没有指纹/版本的旧文档补写 meta（一次性，幂等）
# ---------------------------------------------------------------------------

async def backfill_document(db, *, doc_id: str, project_id: str,
                            file_path: str, file_name: str, file_type: str,
                            doc_category: str = "",
                            parsed_markdown: str = "") -> dict:
    """旧库升级：补指纹 + 解析层产物（若已有 parsed_markdown）。"""
    meta = await _safe_io(store.read_meta, project_id, doc_id)
    if not meta or not meta.get("file_hash_md5"):
        p = Path(file_path)
        if p.exists():
            meta = await ingest_upload(
                doc_id=doc_id, project_id=project_id, file_name=file_name,
                file_type=file_type, saved_path=p, size=p.stat().st_size,
                doc_category=doc_category) or meta or {}
        else:
            return {"ok": False, "reason": "原始文件缺失，无法计算指纹"}
    result: dict = {"ok": True, "doc_id": doc_id}
    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM doc_chunks WHERE doc_id=?", (doc_id,))
    has_chunks = int(((await cur.fetchone()) or ["0"])[0] or 0) > 0
    if parsed_markdown and not has_chunks:
        info = await ingest_parse_result(
            db, doc_id=doc_id, project_id=project_id, file_name=file_name,
            markdown=parsed_markdown, page_count=1, parse_duration_s=0.0,
            parse_engine="backfill")
        result["backfilled"] = info
    return result
