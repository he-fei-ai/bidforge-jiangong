"""项目管理路由"""
import uuid
import logging
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


@router.delete("/{project_id}")
async def delete_project(project_id: str, db=Depends(get_db)):
    cur = await db.execute("SELECT id FROM projects WHERE id=?", (project_id,))
    if not await cur.fetchone():
        raise HTTPException(404, "项目不存在")
    # ✅ BUG 修复：补齐 chart_predictions / task_registry / prompt_templates /
    #    ai_audit_logs 的清理；旧实现漏删，残留孤儿记录会：
    #      - 让"图表清单"接口在项目已删后仍返回残留图；
    #      - 让任务控制面板看到不存在的方案/项目任务。
    # 顺序：先删所有以 scheme_id 关联的表（逐方案循环），再删项目级表，最后删项目本身。
    cur = await db.execute("SELECT id FROM schemes WHERE project_id=?", (project_id,))
    scheme_ids = [r[0] for r in await cur.fetchall()]
    # ✅ 文件泄漏修复：先收集磁盘文件路径（删行之前），提交后统一清理——
    # 旧实现删 project_documents 行但不删上传的原始文件，文件永久残留。
    doc_files: list[str] = []
    cur = await db.execute(
        "SELECT file_path FROM project_documents WHERE project_id=? AND file_path!=''",
        (project_id,))
    doc_files = [r[0] for r in await cur.fetchall()]
    if scheme_ids:
        placeholders = ",".join("?" * len(scheme_ids))
        # ✅ 级联补全：consistency_conflicts / consistency_repairs / scheme_snapshots
        for table in ("sections", "global_facts", "chart_predictions",
                      "compliance_check", "consistency_audit",
                      "consistency_conflicts", "consistency_repairs",
                      "scheme_snapshots", "export_cache", "task_registry",
                      "preflight_runs", "review_records"):
            await db.execute(f"DELETE FROM {table} WHERE scheme_id IN ({placeholders})",
                             scheme_ids)
    await db.execute("DELETE FROM schemes WHERE project_id=?", (project_id,))
    await db.execute("DELETE FROM project_documents WHERE project_id=?", (project_id,))
    await db.execute("DELETE FROM global_facts WHERE project_id=?", (project_id,))
    await db.execute(
        "DELETE FROM facts_extracted_chunks WHERE project_id=?", (project_id,))
    await db.execute("DELETE FROM knowledge_base WHERE project_id=?", (project_id,))
    # ✅ 级联补全：项目级导出预设
    await db.execute("DELETE FROM export_presets WHERE project_id=?", (project_id,))
    await db.execute("DELETE FROM projects WHERE id=?", (project_id,))
    await db.commit()

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
            for f in EXPORTS_DIR.glob(f"{sid}_*.docx"):
                f.unlink(missing_ok=True)
                removed_files += 1
    except OSError:
        pass
    return {"ok": True, "files_removed": removed_files}