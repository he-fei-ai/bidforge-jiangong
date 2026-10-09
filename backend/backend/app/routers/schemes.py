"""方案管理路由（项目下多方案）"""
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from app.db import get_db, read_db
from app.models import SchemeCreate, SchemeUpdate

router = APIRouter(prefix="/api/v1/projects/{project_id}/schemes", tags=["schemes"])


def require_project_id(project_id: str | None) -> str:
    """校验并归一 ``schemes.project_id`` —— **唯一入口**。

    ⚠️ G3 根因加固（R45 · 2026-10-06）：``schemes.project_id`` 是
    ``NOT NULL`` 但**允许空串**（无 CHECK 约束）。空串会让按项目维度的
    缓存失效退化成「双空作用域」的静默 no-op ——
    ``global_facts._invalidate_fact_scope_cache(db, "", "")`` 直接 return：
    已确认的事实改动**一个缓存都不失效**、``schemes.facts_updated_at``
    不推进，「事实已变更」标记永久停在旧值。

    旧实现里三条写路径各用一套防空手段（404 / 404 / 400），语义分散且
    错误文案对不上（空 project_id 会报「项目不存在」而非「参数为空」）。
    现收敛为单一入口：全部写 ``schemes.project_id`` 的路径都必须过本函数。

    已知写路径（护栏 test_schemes_project_id_guard_20261006.py 扫描锁定）：

    - ``create_scheme``（新建方案）
    - ``duplicate_scheme``（复制方案）
    - ``bid_analysis.correct_item``（人工校正时回填归属项目）
    """
    pid = (project_id or "").strip()
    if not pid:
        raise HTTPException(422, "project_id 不能为空")
    return pid


async def _refresh_word_count(db, scheme_id: str):
    cur = await db.execute("SELECT COALESCE(SUM(word_count),0) FROM sections WHERE scheme_id=?", (scheme_id,))
    total = (await cur.fetchone())[0]
    await db.execute("UPDATE schemes SET word_count=? WHERE id=?", (total, scheme_id))


@router.get("")
async def list_schemes(project_id: str, db=Depends(read_db)):
    cur = await db.execute(
        "SELECT * FROM schemes WHERE project_id=? ORDER BY created_at DESC", (project_id,))
    rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        return {"items": []}
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    cur = await db.execute(
        f"SELECT scheme_id, COUNT(*) as cnt, COALESCE(SUM(word_count),0) as wc "
        f"FROM sections WHERE scheme_id IN ({placeholders}) GROUP BY scheme_id", ids)
    agg = {r[0]: {"section_count": r[1], "word_count": r[2]} for r in await cur.fetchall()}
    for r in rows:
        r.update(agg.get(r["id"], {"section_count": 0, "word_count": 0}))
    return {"items": rows}


@router.post("")
async def create_scheme(project_id: str, data: SchemeCreate, db=Depends(get_db)):
    # ⚠️ G3 根因加固（R45）：空 project_id 不得落库（唯一入口，见函数 docstring）
    project_id = require_project_id(project_id)
    cur = await db.execute("SELECT id FROM projects WHERE id=?", (project_id,))
    if not await cur.fetchone():
        raise HTTPException(404, "项目不存在")
    sid = str(uuid.uuid4())
    config = {"library_ids": data.library_ids, "knowledge_scope": data.knowledge_scope}
    try:
        config.update(json.loads(data.config_json or "{}"))
    except json.JSONDecodeError:
        pass
    await db.execute(
        "INSERT INTO schemes (id, project_id, name, type, profession, word_budget, outline_source, config_json)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (sid, project_id, data.name, data.type, data.profession,
         data.word_budget, data.outline_source, json.dumps(config, ensure_ascii=False)))

    # ✅ 断链修复：用户创建时选了目录库（outline_source='library'），旧实现只把
    #    library_ids 写进 config_json 却从不落 sections —— 方案创建后章节树为空，
    #    用户必须自己进工作台重新找一遍目录再点「套用」，与"我选了这份目录"的心智不符。
    #    这里在创建阶段直接套用首个已通过的目录库，并累加引用次数。
    applied = None
    if data.library_ids and data.outline_source == "library":
        applied = await _apply_library_on_create(db, sid, data.library_ids)

    await db.commit()
    cur = await db.execute("SELECT * FROM schemes WHERE id=?", (sid,))
    row = dict(await cur.fetchone())
    if applied:
        row["applied_library"] = applied
    return row


async def _apply_library_on_create(db, scheme_id: str, library_ids: list[str]) -> dict | None:
    """创建方案时自动套用目录库：写入 sections + 累加 ref_count。

    只取第一个「已通过」的目录库（创建页单选），不覆盖任何已有内容（新方案必然为空）。
    失败不影响方案创建（降级为"创建后手动套用"，与旧行为一致）。
    """
    import logging

    from app.services.outline_reference import parse_outline

    logger = logging.getLogger("schemes")
    for lid in library_ids:
        if not lid:
            continue
        cur = await db.execute(
            "SELECT id, name, version, outline_json FROM outline_library"
            " WHERE id=? AND review_status='已通过'", (lid,))
        row = await cur.fetchone()
        if not row:
            continue
        outline = parse_outline(row["outline_json"])
        if not outline:
            continue
        try:
            from app.routers.sections import _save_outline_to_db
            result = await _save_outline_to_db(db, scheme_id, outline, source="目录库")
        except Exception as e:
            logger.warning("创建方案时套用目录库失败（降级为手动套用）: %s", e)
            return None
        await db.execute(
            "UPDATE outline_library SET ref_count=ref_count+1 WHERE id=?", (lid,))
        await db.execute(
            "UPDATE schemes SET outline_source='library', status='目录已确认' WHERE id=?",
            (scheme_id,))
        return {
            "library_id": row["id"],
            "name": row["name"],
            # ⚠️ sqlite3.Row 未实现 Mapping.get（与 dict(row) 后的行为不同），只能索引取值
            "version": row["version"] or "v1.0",
            "count": result.get("count", 0),
        }
    return None


@router.get("/{scheme_id}")
async def get_scheme(project_id: str, scheme_id: str, db=Depends(read_db)):
    cur = await db.execute("SELECT * FROM schemes WHERE id=? AND project_id=?", (scheme_id, project_id))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "方案不存在")
    return dict(row)


@router.patch("/{scheme_id}")
async def update_scheme(project_id: str, scheme_id: str, data: SchemeUpdate, db=Depends(get_db)):
    fields = {k: v for k, v in data.model_dump(exclude_none=True).items()}
    # ✅ 断链修复：library_ids 不是 schemes 的物理列，而是存在 config_json 里。
    #    旧实现把它当普通列拼进 SET 子句 → 直接报 "no such column: library_ids"。
    #    这里拆出来合并进 config_json，使创建后仍可更换 / 补选关联目录库。
    if "library_ids" in fields:
        ids = fields.pop("library_ids") or []
        cur = await db.execute("SELECT config_json FROM schemes WHERE id=?", (scheme_id,))
        row = await cur.fetchone()
        cfg = {}
        if row and row[0]:
            try:
                cfg = json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                cfg = {}
        cfg["library_ids"] = ids
        fields["config_json"] = json.dumps(cfg, ensure_ascii=False)
    if fields:
        fields["updated_at"] = datetime.now().isoformat()
        sets = ", ".join(f"{k}=?" for k in fields)
        await db.execute(f"UPDATE schemes SET {sets} WHERE id=?", (*fields.values(), scheme_id))
        await db.commit()
    return {"ok": True}


@router.delete("/{scheme_id}")
async def delete_scheme(project_id: str, scheme_id: str, db=Depends(get_db)):
    """删除专项方案及其全部关联数据。

    清理范围：
    - sections（章节正文，显式删除，CASCADE 双保险）
    - global_facts / chart_predictions / compliance_check / consistency_audit
    - export_cache + EXPORTS_DIR 磁盘导出文件
    - task_registry（该方案的任务记录；运行中任务的后续写库会被各处 try/except 安全吞掉）
    - 不删除 project_documents（项目级资料，同项目其他方案共用）
    """
    from app.config import EXPORTS_DIR

    cur = await db.execute(
        "SELECT name FROM schemes WHERE id=? AND project_id=?",
        (scheme_id, project_id))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "方案不存在")

    await db.execute("DELETE FROM sections WHERE scheme_id=?", (scheme_id,))
    # ✅ 级联补全：consistency_conflicts / consistency_repairs / scheme_snapshots
    # 均以 scheme_id 关联且无外键约束，旧清单遗漏会残留孤儿行。
    for table in ("global_facts", "chart_predictions", "compliance_check",
                  "consistency_audit", "consistency_conflicts", "consistency_repairs",
                  "scheme_snapshots", "export_cache", "task_registry",
                  "preflight_runs", "review_records"):
        await db.execute(f"DELETE FROM {table} WHERE scheme_id=?", (scheme_id,))
    await db.execute(
        "DELETE FROM facts_extracted_chunks WHERE project_id=? AND scheme_id=?",
        (project_id, scheme_id))
    # ✅ 级联补全（2026-09-20）：uploaded_outlines 以 scheme_id 关联但无外键 ——
    #    方案删除后「已保存」记录仍指向不存在的方案（孤儿引用）。上传记录本身
    #    是项目级素材（可重复用于另存目录），不随方案删除，只断开悬挂引用。
    await db.execute(
        "UPDATE uploaded_outlines SET scheme_id='' WHERE scheme_id=?", (scheme_id,))
    await db.execute("DELETE FROM schemes WHERE id=?", (scheme_id,))
    await db.commit()

    # ✅ 2026-10-06（缓存有界化）：方案已删，回收进程内总检/预检幂等缓存的死键。
    #    正文/事实变更会因缓存键含内容指纹而自然失效，**只有删除**需要显式清理
    #    （条目指向已不存在的方案，留着只会占用内存且永不命中）。
    try:
        from app.routers.compliance import invalidate_overview_cache
        invalidate_overview_cache(scheme_id)
    except Exception as e:  # fail-soft：缓存清理失败不影响删除结果
        import logging
        logging.getLogger("schemes").warning("清理总检缓存失败: %s", e)

    # 清理磁盘导出文件（命名含 scheme_id 前缀）
    removed_files = 0
    try:
        for f in EXPORTS_DIR.glob(f"{scheme_id}_*.docx"):
            f.unlink(missing_ok=True)
            removed_files += 1
    except OSError as e:
        # 删除失败不阻塞（文件残留无害，下次导出同内容会覆写）
        import logging
        logging.getLogger("schemes").warning("清理导出文件失败: %s", e)

    return {"ok": True, "name": row["name"], "export_files_removed": removed_files}


@router.post("/{scheme_id}/duplicate")
async def duplicate_scheme(project_id: str, scheme_id: str, db=Depends(get_db)):
    # ⚠️ G3 根因加固（R45）：空 project_id 不得落库（唯一入口，见函数 docstring）
    project_id = require_project_id(project_id)
    # ✅ BUG 修复：source 校验时同时限定 project_id，避免跨项目误复制
    cur = await db.execute(
        "SELECT * FROM schemes WHERE id=? AND project_id=?", (scheme_id, project_id))
    src = await cur.fetchone()
    if not src:
        raise HTTPException(404, "方案不存在")
    src = dict(src)
    new_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO schemes (id, project_id, name, type, profession, status, word_budget, outline_source, config_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (new_id, project_id, f"{src['name']}（副本）", src["type"], src["profession"],
         "草稿", src["word_budget"], src["outline_source"], src["config_json"]))

    # ✅ BUG 修复：章节 parent_id 重映射。
    # 旧实现直接复制 s["parent_id"]，副本内子章节的 parent_id 仍指向原方案的父章节 id，
    # 造成 tree 断链（副本子节点在 _build_tree 中被误判为 roots，目录结构错乱）；
    # 同时原方案的章节若被删除，副本 parent_id 变成悬空引用。
    # 现改为两阶段：先分配 new_id，建立 old→new 映射，再批量插入（parent_id 走映射）。
    cur = await db.execute(
        "SELECT id, parent_id, title, description, level, word_budget, "
        "outline_json, sort_order, locked FROM sections "
        "WHERE scheme_id=? ORDER BY sort_order, created_at", (scheme_id,))
    src_sections = [dict(r) for r in await cur.fetchall()]
    id_map: dict[str, str] = {}
    inserts: list[tuple] = []
    for s in src_sections:
        nid = str(uuid.uuid4())
        id_map[s["id"]] = nid
        # parent_id 若映射表里不存在（原方案的父节点在别处/悬空），回退为 ""
        new_parent = id_map.get(s.get("parent_id") or "", "") or ""
        inserts.append((
            nid, new_id, project_id, new_parent,
            s["title"], s.get("description", ""), s.get("level", 1),
            "empty", s.get("word_budget", 1500), s.get("sort_order", 0),
            s.get("outline_json", ""), s.get("locked", 0),
        ))
    if inserts:
        await db.executemany(
            "INSERT INTO sections "
            "(id, scheme_id, project_id, parent_id, title, description, level, "
            "status, word_budget, sort_order, outline_json, locked) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            inserts)

    cur = await db.execute("SELECT * FROM global_facts WHERE scheme_id=?", (scheme_id,))
    fact_group_map: dict[str, str] = {}
    for r in await cur.fetchall():
        s = dict(r)
        old_group = s.get("group_id") or s.get("id")
        new_group = fact_group_map.setdefault(old_group, str(uuid.uuid4()))
        # 复制方案必须重映射 group_id。该列不是全局唯一约束，直接复用原值会导致
        # 删除副本分组时误删原方案同组事实，也会让两个方案的 UI 分组边界混淆。
        # ✅ 修复（2026-09-18）：复制时一并向带上 chunk_hash。旧实现漏该列 → 副本
        #    事实的 chunk_hash 恒为空，而 persist_extraction 把「空 chunk_hash」
        #    视为旧 AI 数据必定纳入删除范围，副本首次增量提取会把这些复制来的
        #    未确认事实当作过时数据刷新删除。
        await db.execute(
            "INSERT INTO global_facts "
            "(id, project_id, scheme_id, group_id, group_title, title, content, "
            "category, source_ref, is_simulated, confidence, is_resolved, "
            "has_conflict, conflict_keys, fact_key, chunk_hash, value_unit, "
            "fact_type, evidence_kind, page_ref, zone_type, is_safety_critical, "
            "norm_group, chapter, fact_attr, source_kind, is_shared, is_stale) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), project_id, new_id, new_group, s.get("group_title", ""),
             s["title"], s["content"], s.get("category", ""), s.get("source_ref", ""),
             s.get("is_simulated", 0), s.get("confidence", 1.0),
             s.get("is_resolved", 1), s.get("has_conflict", 0),
             s.get("conflict_keys", ""), s.get("fact_key", ""),
             s.get("chunk_hash", ""), s.get("value_unit", ""),
             s.get("fact_type", ""), s.get("evidence_kind", ""),
             s.get("page_ref", ""), s.get("zone_type", ""),
             s.get("is_safety_critical", 0), s.get("norm_group", ""),
             s.get("chapter", ""), s.get("fact_attr", ""),
             s.get("source_kind", ""), s.get("is_shared", 0),
             s.get("is_stale", 0)))
    await db.commit()
    return {"id": new_id}


@router.post("/{scheme_id}/archive")
async def archive_scheme(project_id: str, scheme_id: str, db=Depends(get_db)):
    cur = await db.execute("SELECT status FROM schemes WHERE id=?", (scheme_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "方案不存在")
    new_status = "草稿" if row[0] == "已归档" else "已归档"
    await db.execute("UPDATE schemes SET status=?, updated_at=? WHERE id=?",
                     (new_status, datetime.now().isoformat(), scheme_id))
    await db.commit()
    return {"status": new_status}