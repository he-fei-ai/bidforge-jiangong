"""项目管理路由"""
import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.db import get_db
from app.models import ProjectCreate, ProjectUpdate

logger = logging.getLogger("projects")
router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


@router.get("")
async def list_projects(keyword: str = "", engineering_type: str = "", db=Depends(get_db)):
    sql = ("SELECT p.*, (SELECT COUNT(*) FROM schemes s WHERE s.project_id=p.id) as scheme_count "
           "FROM projects p WHERE 1=1")
    params: list = []
    if keyword:
        sql += " AND (p.name LIKE ? OR p.description LIKE ?)"
        params += [f"%{keyword}%", f"%{keyword}%"]
    if engineering_type:
        sql += " AND p.engineering_type = ?"
        params.append(engineering_type)
    sql += " ORDER BY p.updated_at DESC"
    cur = await db.execute(sql, params)
    rows = await cur.fetchall()
    result = [dict(r) for r in rows]
    return {"items": result}


@router.post("")
async def create_project(data: ProjectCreate, db=Depends(get_db)):
    pid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO projects (id, name, description, engineering_type, location, client_name, contractor_name, project_period)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (pid, data.name, data.description, data.engineering_type, data.location,
         data.client_name, data.contractor_name, data.project_period))
    await db.commit()
    cur = await db.execute("SELECT * FROM projects WHERE id=?", (pid,))
    return dict(await cur.fetchone())


@router.get("/{project_id}")
async def get_project(project_id: str, db=Depends(get_db)):
    cur = await db.execute("SELECT * FROM projects WHERE id=?", (project_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "项目不存在")
    return dict(row)


@router.patch("/{project_id}")
async def update_project(project_id: str, data: ProjectUpdate, db=Depends(get_db)):
    fields = {k: v for k, v in data.model_dump(exclude_none=True).items()}
    if not fields:
        return {"ok": True}
    fields["updated_at"] = datetime.now().isoformat()
    sets = ", ".join(f"{k}=?" for k in fields)
    await db.execute(f"UPDATE projects SET {sets} WHERE id=?", (*fields.values(), project_id))
    await db.commit()
    return {"ok": True}


#: 项目级表（按 project_id 清理）→ 解析提取域与四层存储派生表。
#: 这些表都没有指向 projects 的外键（schema_sql 里是裸 TEXT 列），
#: 必须显式删除，否则删项目后永久残留孤儿行。
_PROJECT_SCOPED_TABLES = (
    "schemes",                 # 方案主表（自带 ON DELETE CASCADE，显式删更稳妥）
    "bid_analysis_items",      # 解析提取主表（18 项结构化提取结果）
    "bid_sections",            # 多标段检测结果
    "uploaded_outlines",       # 上传识别的目录（raw_text / parsed_json 整份留库）
    "doc_extractions",         # 提取层（AI 提取结果按类）
    "doc_validation_reports",  # 解析层质量 / 交叉校验报告
    "global_facts",
    "facts_extracted_chunks",
    "knowledge_base",
    "export_presets",
)

#: 方案级表（按 scheme_id 清理）。⚠️ consistency_scan_cache 曾长期漏删 ——
#: 它是「章节正文 hash → 扫描结果」的缓存，章节删了缓存仍在，
#: 下次对同名 scheme_id 的复用会让预检命中早已不存在的章节。
#: ⚠️ placeholder_baselines 自带 REFERENCES schemes ON DELETE CASCADE，
#: 但测试连接与部分历史库未开 PRAGMA foreign_keys=ON，级联不生效 →
#: 这里显式删除，不依赖级联。
_SCHEME_SCOPED_TABLES = (
    "sections", "global_facts", "chart_predictions",
    "compliance_check", "consistency_audit",
    "consistency_conflicts", "consistency_scan_cache",
    "consistency_repairs", "scheme_snapshots",
    "export_cache", "task_registry", "preflight_runs", "review_records",
    "placeholder_baselines",
)


async def _delete_rows_by_project(db, project_id: str, removed_counts: dict) -> None:
    """按 project_id 清理项目级表（逐表 fail-soft）。

    每张表各自 try/except：项目删除**绝不能**因为某张派生表缺失或列漂移而整体失败
    （那是用户再也删不掉项目的死锁），漏清理的表只记 WARNING 供后续排障。
    表名来自上方常量（非外部输入），拼接安全。
    """
    for table in _PROJECT_SCOPED_TABLES:
        try:
            await db.execute(f"DELETE FROM {table} WHERE project_id=?",
                             (project_id,))
        except Exception as e:
            logger.warning("删除项目 %s 时清理 %s 失败（项目仍会删除）: %s",
                           project_id[:8], table, e)
            removed_counts["cleanup_errors"].append(table)


@router.delete("/{project_id}")
async def delete_project(project_id: str, db=Depends(get_db)):
    cur = await db.execute("SELECT id FROM projects WHERE id=?", (project_id,))
    if not await cur.fetchone():
        raise HTTPException(404, "项目不存在")
    # ✅ BUG 修复：补齐派生表清理。旧实现漏删，残留孤儿记录会：
    #      - 让"图表清单"接口在项目已删后仍返回残留图；
    #      - 让任务控制面板看到不存在的方案/项目任务；
    #      - 让解析提取域（bid_analysis_items / bid_sections）的提取结果永久残留，
    #        /bid-analysis/results 仍返回已删项目的数据（下游会据此"复活"旧项目）。
    # 顺序：先删所有以 scheme_id 关联的表（逐方案循环），再删项目级表，最后删项目本身。
    cur = await db.execute("SELECT id FROM schemes WHERE project_id=?", (project_id,))
    scheme_ids = [r[0] for r in await cur.fetchall()]
    # ✅ 文件泄漏修复：先收集磁盘文件路径与文档 id（删行之前），提交后统一清理——
    # 旧实现删 project_documents 行但不删上传的原始文件，文件永久残留。
    doc_files: list[str] = []
    doc_ids: list[str] = []
    cur = await db.execute(
        "SELECT id, file_path FROM project_documents WHERE project_id=?",
        (project_id,))
    for r in await cur.fetchall():
        doc_ids.append(str(r["id"]))
        fp = r["file_path"] if "file_path" in r.keys() else ""
        if fp:
            doc_files.append(fp)

    removed_counts: dict = {"cleanup_errors": []}
    if scheme_ids:
        placeholders = ",".join("?" * len(scheme_ids))
        # 逐表 fail-soft（理由同 _delete_rows_by_project）
        for table in _SCHEME_SCOPED_TABLES:
            try:
                await db.execute(
                    f"DELETE FROM {table} WHERE scheme_id IN ({placeholders})",
                    scheme_ids)
            except Exception as e:
                logger.warning("删除项目 %s 时清理 %s 失败（项目仍会删除）: %s",
                               project_id[:8], table, e)
                removed_counts["cleanup_errors"].append(table)

    # ✅ 四层存储派生表：doc_chunks 只有 doc_id 没有 project_id，
    #    必须先用上面收集的 doc_ids 定向删除，不能靠 project_id 过滤。
    if doc_ids:
        placeholders = ",".join("?" * len(doc_ids))
        for table in ("doc_chunks", "doc_extractions", "doc_validation_reports"):
            try:
                await db.execute(
                    f"DELETE FROM {table} WHERE doc_id IN ({placeholders})", doc_ids)
            except Exception as e:
                logger.warning("删除项目 %s 时清理 %s 失败（项目仍会删除）: %s",
                               project_id[:8], table, e)
                removed_counts["cleanup_errors"].append(table)

    await _delete_rows_by_project(db, project_id, removed_counts)
    await db.execute("DELETE FROM project_documents WHERE project_id=?", (project_id,))
    await db.execute("DELETE FROM projects WHERE id=?", (project_id,))
    await db.commit()

    # ✅ 2026-10-06（缓存有界化）：项目连带删除了 scheme_ids，回收这些方案在
    #    进程内总检/预检幂等缓存中的死键（理由同 schemes.delete_scheme）。
    if scheme_ids:
        try:
            from app.routers.compliance import invalidate_overview_cache
            for sid in scheme_ids:
                invalidate_overview_cache(sid)
        except Exception as e:  # fail-soft：缓存清理失败不影响删除结果
            logger.warning("删除项目 %s 时清理总检缓存失败（不影响删除结果）: %s",
                           project_id[:8], e)

    # ✅ BUG 修复（四层存储磁盘泄漏）：doc_storage 的四层目录树
    # （data/projects/{pid}/documents/ + documents_index.json）此前无人清理 ——
    # purge_document 只负责单文档删除，项目级入口从未调用它，
    # 删除项目后整棵四层目录树永久残留（与「删除文档即 purge_document」口径不一致）。
    try:
        from app.services.doc_pipeline import doc_storage as _store
        if await asyncio.to_thread(_store.delete_project_docs_root, project_id):
            removed_counts["doc_trees_removed"] = True
    except Exception as e:
        logger.warning("删除项目 %s 时清理四层存储目录失败（不影响删除结果）: %s",
                       project_id[:8], e)

    # 磁盘清理：项目上传的原始文档 + 各方案的导出产物
    removed_files = 0
    try:
        from app.config import FACT_UPLOADS_DIR
        for fp in doc_files:
            # 路径必须经 resolve 后位于 FACT_UPLOADS_DIR/<project_id>/ 下，
            # 防止历史脏数据中的 ../ 或符号链接绕过词法 parents 检查而误删外部文件。
            root = FACT_UPLOADS_DIR.resolve()
            project_root = (root / project_id).resolve()
            p = Path(fp)
            try:
                resolved = p.resolve()
                resolved.relative_to(project_root)
            except (OSError, ValueError, RuntimeError):
                logger.warning("跳过上传目录外的项目文件清理: %s", fp)
                continue
            if resolved.is_file() and not p.is_symlink():
                resolved.unlink(missing_ok=True)
                removed_files += 1
    except OSError:
        pass
    try:
        from app.config import EXPORTS_DIR
        for sid in scheme_ids:
            # ✅ 修复（2026-10-03 · 导出缓存磁盘泄漏）：旧实现只清 `.docx`，
            #    漏清 `.pdf` 导出缓存文件。PDF 产物同样落在 EXPORTS_DIR，
            #    命名 `{scheme_id}_*.pdf`（见 export.py 4809 的 out_path），
            #    删除方案后这些 PDF 缓存文件永久孤儿、目录无限增长。
            #    现 docx / pdf 一并清理；`{sid}_*` 前缀确保不误删其他方案的文件。
            for ext in ("docx", "pdf"):
                for f in EXPORTS_DIR.glob(f"{sid}_*.{ext}"):
                    f.unlink(missing_ok=True)
                    removed_files += 1
    except OSError:
        pass
    # 加法式返回：ok / files_removed 保持旧契约不变，新增字段供前端提示
    # 「清理有失败项」与「四层存储已回收」，不破坏既有消费方。
    result: dict = {"ok": True, "files_removed": removed_files}
    if removed_counts.get("cleanup_errors"):
        result["cleanup_errors"] = removed_counts["cleanup_errors"]
    if removed_counts.get("doc_trees_removed"):
        result["doc_trees_removed"] = True
    return result