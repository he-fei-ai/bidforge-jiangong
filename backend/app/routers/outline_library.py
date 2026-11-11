"""专项方案目录库路由"""

import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db import get_db, get_read_conn, release_read_conn, safe_rowcount
from app.models import OutlineLibraryCreate, OutlineLibraryUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/outline-library", tags=["outline_library"])

#: 列表查询字段（剔除 outline_json 大字段：单条可达数十 KB）
_LIST_COLS = (
    "id, name, type, engineering_type, profession, applicable_conditions, basis,"
    " tags, version, source, ref_count, review_status, created_at, updated_at"
)

#: 允许排序的列（白名单，避免 SQL 注入）
_SORTABLE = {
    "ref_count": "ref_count",
    "updated_at": "updated_at",
    "created_at": "created_at",
    "name": "name",
    "review_status": "review_status",
}

REVIEW_STATUSES = ("待审核", "已通过", "已停用")


def _normalize_for_write(raw, *, context: str, library_id: str = "") -> str:
    """目录库**写路径统一出口**（R214 · 承 R213 L-2）。

    与 ``create_library`` / ``update_library`` 的 400 硬闸门不同，版本推进 /
    回滚 / 方案沉淀 / 复制都属于「恢复或派生」动作：历史脏数据不应让整条链
    失败 ⇒ ``problems`` 只记 WARNING（规范化结果已消除脏编号/不可见字符），
    但**空目录必须显式拒绝**（由调用方按各自语义 400），且异常分支由
    ``normalize_outline_for_library`` 保证 fail-closed（返回 ``"[]"``）。

    统一前：``new_version`` / ``restore_version`` / ``create_from_scheme`` 走
    ``normalize_outline_json``（只裁剪重排，不清洗标题、不校验编号/长度），
    ``restore_version`` 甚至连空目录校验都没有 ⇒ 脏历史 JSON 会静默写回主表。
    """
    from app.services.outline_utils import normalize_outline_for_library

    normalized, problems = normalize_outline_for_library(raw)
    if problems:
        logger.warning(
            "目录库写路径 %s（%s）检出 %d 项编号/标题问题（已规范化，不阻断）: %s",
            context,
            library_id,
            len(problems),
            "；".join(problems[:3]),
        )
    return normalized


def _count_nodes(nodes, _depth: int = 0) -> int:
    """统计目录树节点总数（含全部层级）

    ✅ 健壮性：加深度上限。入库路径都已 normalize 到三级，但导出/详情等只读路径
    面对历史脏数据（或畸形 JSON）时不应因递归深度触发 RecursionError 让接口 500。
    """
    if not isinstance(nodes, list) or _depth > 12:
        return 0
    total = 0
    for n in nodes:
        if not isinstance(n, dict):
            continue
        total += 1 + _count_nodes(n.get("children") or [], _depth + 1)
    return total


# ============================================================== 只读聚合端点
# ⚠️ 必须定义在 /{library_id} 之前，否则会被路径参数路由吞掉


@router.get("/stats")
async def library_stats():
    """目录库概览：总数 / 各审核状态 / 引用总数 / 分类数 / 预置数"""
    conn = await get_read_conn()
    try:
        cur = await conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN review_status='已通过' THEN 1 ELSE 0 END) AS approved,"
            " SUM(CASE WHEN review_status='待审核' THEN 1 ELSE 0 END) AS pending,"
            " SUM(CASE WHEN review_status='已停用' THEN 1 ELSE 0 END) AS disabled,"
            " COALESCE(SUM(ref_count),0) AS ref_total FROM outline_library"
        )
        row = dict(await cur.fetchone())
        cur = await conn.execute("SELECT COUNT(DISTINCT type) AS c FROM outline_library WHERE type<>''")
        row["categories"] = (await cur.fetchone())["c"]
        cur = await conn.execute("SELECT COUNT(*) AS c FROM outline_library WHERE source='预置清单'")
        row["preset"] = (await cur.fetchone())["c"]
        return row
    finally:
        await release_read_conn(conn)


@router.get("/filters")
async def library_filters():
    """各筛选维度的可选项及计数（分类 / 工程类型 / 专业 / 审核状态 / 来源）"""
    conn = await get_read_conn()
    try:

        async def facet(col: str):
            cur = await conn.execute(
                f"SELECT {col} AS v, COUNT(*) AS c FROM outline_library"
                f" WHERE {col}<>'' GROUP BY {col} ORDER BY c DESC"
            )
            return [{"value": r["v"], "count": r["c"]} for r in await cur.fetchall()]

        return {
            "type": await facet("type"),
            "engineering_type": await facet("engineering_type"),
            "profession": await facet("profession"),
            "review_status": await facet("review_status"),
            "source": await facet("source"),
        }
    finally:
        await release_read_conn(conn)


@router.get("/templates")
async def list_standard_templates():
    """列出可用的行业标准目录模板（供『套用标准模板』下拉使用）"""
    from app.services.outline_templates import list_templates

    return {"items": list_templates()}


@router.get("/templates/{key}")
async def get_standard_template(key: str):
    """取某个行业标准模板的完整目录树 + 编制依据 / 适用条件"""
    from app.services.ai.json_response import renumber_outline
    from app.services.outline_templates import BUILDERS, TEMPLATE_META

    if key not in BUILDERS:
        raise HTTPException(404, "模板不存在")
    meta = TEMPLATE_META.get(key, {})
    # ✅ 修复（2026-09-17）：标准模板 builder 产出的是无 id 节点，而前端 antd Tree
    #    以 id 作 React key。此前未补编号，前端预览「套用标准模板」直接渲染时所有
    #    节点 id=undefined，触发 React key 冲突告警。现与所有其它目录来源口径一致，
    #    返回前统一 renumber_outline，保证 id 全局唯一。
    return {
        "key": key,
        "outline": renumber_outline(BUILDERS[key]()),
        "basis": meta.get("basis", ""),
        "applicable": meta.get("applicable", ""),
        "risk": meta.get("risk", ""),
    }


# ============================================================== 列表


@router.get("")
async def list_library(
    type: str = "",
    engineering_type: str = "",
    profession: str = "",
    review_status: str = "",
    source: str = "",
    keyword: str = "",
    sort_by: str = "ref_count",
    order: str = "desc",
    page: int = 1,
    page_size: int = 0,
    db=Depends(get_db),
):
    """目录列表：多维筛选 + 关键词 + 排序 + 分页。

    page_size=0（默认）表示不分页，一次性返回全部（兼容旧调用方）。
    """
    where = " WHERE 1=1"
    params: list = []
    for col, val in [
        ("type", type),
        ("engineering_type", engineering_type),
        ("profession", profession),
        ("review_status", review_status),
        ("source", source),
    ]:
        if val:
            where += f" AND {col}=?"
            params.append(val)
    if keyword:
        where += " AND (name LIKE ? OR tags LIKE ? OR type LIKE ?)"
        params += [f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"]

    cur = await db.execute(f"SELECT COUNT(*) FROM outline_library{where}", params)
    total = (await cur.fetchone())[0]

    order_sql = "DESC" if str(order).lower() != "asc" else "ASC"
    sort_col = _SORTABLE.get(sort_by, "ref_count")
    sql = f"SELECT {_LIST_COLS} FROM outline_library{where}" f" ORDER BY {sort_col} {order_sql}, updated_at DESC"
    if page_size and page_size > 0:
        page = max(1, page)
        sql += " LIMIT ? OFFSET ?"
        params += [page_size, (page - 1) * page_size]

    cur = await db.execute(sql, params)
    return {"items": [dict(r) for r in await cur.fetchall()], "total": total}


@router.post("")
async def create_library(data: OutlineLibraryCreate, db=Depends(get_db)):
    lid = str(uuid.uuid4())
    # ✅ BUG 修复：入库前统一规范化（三级裁剪 + 编号重排），
    #    保证新建目录库与上传识别/套用路径产出一致的目录结构。
    # ✅ R209（2026-11-10 · 目录库入库闸门）：改用 `normalize_outline_for_library`
    #    单一入口 —— 在「裁剪 + 重排」之外补**标题不可见字符清洗**与
    #    **落库闸门校验**（标题非空 / ≤200 字 / 编号合法）。
    #    旧实现「库里能存、套用时被拒」：超长标题入库成功，套用时
    #    `_save_outline_to_db` 的落库闸门 400 且文案说的是「编号校验未通过」
    #    （归因误导）；零宽字符标题原样入库并随目录库导出进 .md/.txt。
    from app.services.outline_utils import normalize_outline_for_library

    outline_json, _lib_problems = normalize_outline_for_library(data.outline_json)
    # ✅ 与上传识别入库（save_as_library）口径一致：拒绝空目录库。
    #    旧实现在 outline_json 非法（如 {"foo": 1} / "[]" / 节点全是字符串）时
    #    静默写入空库 —— 目录库列表出现"看似正常、点进去空白"的脏数据，
    #    套用后方案目录被清空却没有任何提示。
    try:
        _nodes = json.loads(outline_json)
    except (json.JSONDecodeError, TypeError):
        _nodes = []
    if not isinstance(_nodes, list) or not _nodes:
        raise HTTPException(400, "目录内容为空或格式非法（应为非空节点数组），无法创建目录库")
    if _lib_problems:
        raise HTTPException(
            400,
            "目录库内容校验未通过（标题需非空且不超过 200 字，章节编号须合法）："
            + "；".join(_lib_problems[:3]),
        )
    await db.execute(
        "INSERT INTO outline_library (id, name, type, engineering_type, profession, applicable_conditions, basis, outline_json, tags, source, review_status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            lid,
            data.name,
            data.type,
            data.engineering_type,
            data.profession,
            data.applicable_conditions,
            data.basis,
            outline_json,
            data.tags,
            data.source,
            "待审核",
        ),
    )
    await db.commit()
    cur = await db.execute("SELECT * FROM outline_library WHERE id=?", (lid,))
    return dict(await cur.fetchone())


@router.get("/{library_id}")
async def get_library(library_id: str, db=Depends(get_db)):
    cur = await db.execute("SELECT * FROM outline_library WHERE id=?", (library_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "目录库不存在")
    item = dict(row)
    # 版本列表（附带节点数，便于 UI 直接对比各版本规模）
    # ✅ R216（2026-11-11 · P3）：加 LIMIT 50 —— 每次建版本都归档一条（含整份
    #    outline_json），长生命周期库可积累数百条，历史抽屉此前全量渲染。
    #    UI 展示「最近版本」语义不变，超大历史不再随库无限膨胀。
    cur = await db.execute(
        "SELECT * FROM outline_library_versions WHERE library_id=? ORDER BY created_at DESC LIMIT 50",
        (library_id,),
    )
    versions = [dict(r) for r in await cur.fetchall()]
    for v in versions:
        try:
            v["node_count"] = _count_nodes(json.loads(v.get("outline_json") or "[]"))
        except Exception:
            v["node_count"] = 0
    item["versions"] = versions
    try:
        item["node_count"] = _count_nodes(json.loads(item.get("outline_json") or "[]"))
    except Exception:
        item["node_count"] = 0
    return item


@router.patch("/{library_id}")
async def update_library(library_id: str, data: OutlineLibraryUpdate, db=Depends(get_db)):
    # ✅ BUG 修复：旧实现不校验存在性，id 不存在时也返回 ok=True，
    #    前端误以为保存成功，刷新后修改"消失"，且无法定位问题。
    cur = await db.execute("SELECT id FROM outline_library WHERE id=?", (library_id,))
    if not await cur.fetchone():
        raise HTTPException(404, "目录库不存在")
    fields = {k: v for k, v in data.model_dump(exclude_none=True).items()}
    # ✅ R216（2026-11-11 · P3）：PATCH 此前把 review_status 当普通字段直写主表，
    #    绕过专用审核端点（/review、/batch-review）的 REVIEW_STATUSES 白名单 ——
    #    直连 API 传任意字符串即落库：条目从「已通过」筛选中消失（无法套用）、
    #    /stats 三态计数全部漏计。这是目录库写路径中唯一未过审核状态闸门的
    #    入口（outline_json 本身已被 R209 闸门覆盖，状态字段漏了）。
    if "review_status" in fields and fields["review_status"] not in REVIEW_STATUSES:
        raise HTTPException(
            400,
            f"review_status 非法（允许值：{'、'.join(REVIEW_STATUSES)}），审核状态请使用 /review 或 /batch-review 端点",
        )
    # ✅ BUG 修复：编辑保存的目录同样走规范化，避免绕过三裁剪/重编号写入脏结构。
    if "outline_json" in fields:
        # ✅ R209：同 create_library —— 统一走 `normalize_outline_for_library`
        #    （含标题清洗 + 落库闸门校验），杜绝「编辑能存、套用被拒」。
        from app.services.outline_utils import normalize_outline_for_library

        fields["outline_json"], _upd_problems = normalize_outline_for_library(fields["outline_json"])
        # ✅ 修复（2026-09-17）：编辑保存同样校验非空，与 create_library 口径一致，
        #    避免把目录库静默清空成脏数据（点进去空白、套用后方案目录被清空无提示）。
        try:
            _upd_nodes = json.loads(fields["outline_json"])
        except (json.JSONDecodeError, TypeError):
            _upd_nodes = []
        if not isinstance(_upd_nodes, list) or not _upd_nodes:
            raise HTTPException(400, "目录内容为空或格式非法（应为非空节点数组），无法保存")
        if _upd_problems:
            raise HTTPException(
                400,
                "目录库内容校验未通过（标题需非空且不超过 200 字，章节编号须合法）："
                + "；".join(_upd_problems[:3]),
            )
    if fields:
        fields["updated_at"] = datetime.now().isoformat()
        sets = ", ".join(f"{k}=?" for k in fields)
        await db.execute(f"UPDATE outline_library SET {sets} WHERE id=?", (*fields.values(), library_id))
        await db.commit()
    return {"ok": True}


@router.delete("/{library_id}")
async def delete_library(library_id: str, db=Depends(get_db)):
    await db.execute("DELETE FROM outline_library_versions WHERE library_id=?", (library_id,))
    await db.execute("DELETE FROM outline_library WHERE id=?", (library_id,))
    await db.commit()
    return {"ok": True}


@router.post("/batch-review")
async def batch_review(body: dict, db=Depends(get_db)):
    """批量审核：一次性把多条目录设为同一状态。

    请求体：{"ids": ["id1", "id2"], "status": "已通过"}
    """
    ids = body.get("ids") or []
    status = body.get("status", "已通过")
    if status not in REVIEW_STATUSES:
        raise HTTPException(400, "无效审核状态")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "缺少 ids")
    ids = [str(i) for i in ids if i]
    if len(ids) > 500:
        raise HTTPException(400, "单次批量操作上限 500 条")
    now = datetime.now().isoformat()
    affected = 0
    for lid in ids:
        cur = await db.execute(
            "UPDATE outline_library SET review_status=?, updated_at=? WHERE id=?", (status, now, lid)
        )
        # R13：循环内逐条 UPDATE，任一条 execute() 返回 None 都会让整个批量端点 500
        affected += safe_rowcount(cur, what=f"目录库审核状态更新 id={lid}")
    await db.commit()
    return {"ok": True, "affected": affected, "review_status": status}


@router.post("/{library_id}/review")
async def review_library(library_id: str, body: dict, db=Depends(get_db)):
    status = body.get("status", "已通过")
    if status not in ("已通过", "已停用", "待审核"):
        raise HTTPException(400, "无效审核状态")
    # ✅ BUG 修复：旧实现不校验存在性，对不存在的 id 也返回 ok=True，
    #    前端会误以为审核已生效（与 update_library 的存在性校验口径不一致）。
    cur = await db.execute("SELECT id FROM outline_library WHERE id=?", (library_id,))
    if not await cur.fetchone():
        raise HTTPException(404, "目录库不存在")
    await db.execute(
        "UPDATE outline_library SET review_status=?, updated_at=? WHERE id=?",
        (status, datetime.now().isoformat(), library_id),
    )
    await db.commit()
    return {"ok": True, "review_status": status}


@router.post("/{library_id}/new-version")
async def new_version(library_id: str, body: dict, db=Depends(get_db)):
    # ✅ R190（FIX-2）：归档旧版本 → INSERT → UPDATE 主表是读改写复合写，
    #    与 restore-version 同族。旧实现无写锁：两请求并发 new-version 时
    #    各自基于自己读到的旧状态归档/覆盖，最后提交者胜。
    _owns_lock = not getattr(db, "in_transaction", False)
    if _owns_lock:
        await db.execute("BEGIN IMMEDIATE")
    try:
        return await _new_version_locked(library_id, body, db)
    except Exception:
        if _owns_lock:
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001 — 回滚失败不遮蔽原异常
                pass
        raise


async def _new_version_locked(library_id: str, body: dict, db) -> dict:
    cur = await db.execute("SELECT outline_json, version FROM outline_library WHERE id=?", (library_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "目录库不存在")
    old_json, old_ver = row
    # 保存旧版本（version 为空时回退占位，避免版本列表出现 None）
    vid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO outline_library_versions (id, library_id, version, outline_json) VALUES (?,?,?,?)",
        (vid, library_id, old_ver or "v1.0", old_json),
    )
    # 更新为新版本
    new_ver = body.get("version") or "v2.0"
    # ✅ R214（FIX-2）：改走目录库写路径统一出口（清洗 + 裁剪 + 重排 + 校验）。
    #    历史脏版本只 WARNING 不 400（版本推进不该被旧数据卡死），空目录仍 400。
    new_json = _normalize_for_write(
        body.get("outline_json", old_json), context="new_version", library_id=library_id
    )
    # ✅ 修复（2026-09-17）：新版本同样校验非空，避免版本推进把目录库静默清空。
    try:
        _new_nodes = json.loads(new_json)
    except (json.JSONDecodeError, TypeError):
        _new_nodes = []
    if not isinstance(_new_nodes, list) or not _new_nodes:
        raise HTTPException(400, "新版本目录内容为空或格式非法（应为非空节点数组），无法保存")
    await db.execute(
        "UPDATE outline_library SET version=?, outline_json=?, review_status='待审核', updated_at=? WHERE id=?",
        (new_ver, new_json, datetime.now().isoformat(), library_id),
    )
    await db.commit()
    return {"ok": True, "version": new_ver}


@router.post("/{library_id}/restore-version")
async def restore_version(library_id: str, body: dict, db=Depends(get_db)):
    """回滚到某个历史版本（回滚前把当前版本再次归档，保证任何一步可逆）。

    请求体：{"version_id": "xxx"} 或 {"version": "v1.0"}

    ✅ R190（FIX-2）：与 new-version 同族读改写。旧实现「归档去重查询 →
    INSERT → UPDATE 主表」无写锁：两请求同时回滚时去重查询都查不到对方
    未提交的归档行 → 同一 version 重复归档，且最终主表版本取决于提交顺序。
    现于首个决策读之前 BEGIN IMMEDIATE（R184 同口径）。
    """
    _owns_lock = not getattr(db, "in_transaction", False)
    if _owns_lock:
        await db.execute("BEGIN IMMEDIATE")
    try:
        return await _restore_version_locked(library_id, body, db)
    except Exception:
        if _owns_lock:
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001 — 回滚失败不遮蔽原异常
                pass
        raise


async def _restore_version_locked(library_id: str, body: dict, db) -> dict:
    cur = await db.execute("SELECT * FROM outline_library WHERE id=?", (library_id,))
    lib = await cur.fetchone()
    if not lib:
        raise HTTPException(404, "目录库不存在")
    lib = dict(lib)

    version_id = (body or {}).get("version_id", "")
    version = (body or {}).get("version", "")
    if version_id:
        cur = await db.execute(
            "SELECT * FROM outline_library_versions WHERE id=? AND library_id=?", (version_id, library_id)
        )
    elif version:
        cur = await db.execute(
            "SELECT * FROM outline_library_versions WHERE version=? AND library_id=?" " ORDER BY created_at DESC",
            (version, library_id),
        )
    else:
        raise HTTPException(400, "缺少 version_id 或 version")
    target = await cur.fetchone()
    if not target:
        raise HTTPException(404, "指定的历史版本不存在")
    target = dict(target)

    # 归档当前版本（同版本号幂等去重，避免重复回滚产生大量冗余归档）
    cur = await db.execute(
        "SELECT id FROM outline_library_versions WHERE library_id=? AND version=?",
        (library_id, lib.get("version") or "v1.0"),
    )
    if not await cur.fetchone():
        await db.execute(
            "INSERT INTO outline_library_versions (id, library_id, version, outline_json)" " VALUES (?,?,?,?)",
            (str(uuid.uuid4()), library_id, lib.get("version") or "v1.0", lib.get("outline_json") or "[]"),
        )

    # ✅ R214（FIX-2）：与 new_version 同口径（R188 L2「写回前规范化」意图保留、
    #    强度加强）：统一出口额外做标题清洗与编号校验，并补上此前缺失的
    #    **空目录拒绝** —— 旧实现把脏历史 JSON 规范化成 "[]" 后直接写回主表，
    #    目录库被静默清空且零提示。规范数据仍是逐字节恒等变换。
    _restored_json = _normalize_for_write(
        target.get("outline_json"), context="restore_version", library_id=library_id
    )
    try:
        _restored_nodes = json.loads(_restored_json)
    except (json.JSONDecodeError, TypeError):
        _restored_nodes = []
    if not isinstance(_restored_nodes, list) or not _restored_nodes:
        raise HTTPException(400, "历史版本目录内容为空或格式非法，无法回滚")
    await db.execute(
        "UPDATE outline_library SET outline_json=?, version=?, review_status='待审核', updated_at=?" " WHERE id=?",
        (
            _restored_json,
            target.get("version") or lib.get("version"),
            datetime.now().isoformat(),
            library_id,
        ),
    )
    await db.commit()
    return {"ok": True, "version": target.get("version"), "note": "已回滚至该版本，回滚前的当前版本已归档"}


@router.post("/from-scheme")
async def create_from_scheme(body: dict, db=Depends(get_db)):
    """把某个方案当前已生成的目录树**反向**存为目录库。

    ✅ 闭环补齐：旧实现只能把「上传识别结果」存为目录库（依赖 uploaded_outlines），
    用户在工作台精心调好的方案目录却无法沉淀复用，只能去目录库页手工重建一遍。

    请求体：{"scheme_id": "xxx", "name": "目录名称（可选）", "type": "", ...}
    """
    scheme_id = str((body or {}).get("scheme_id") or "").strip()
    if not scheme_id:
        raise HTTPException(400, "缺少 scheme_id")

    cur = await db.execute("SELECT id, name, type, profession FROM schemes WHERE id=?", (scheme_id,))
    scheme = await cur.fetchone()
    if not scheme:
        raise HTTPException(404, "方案不存在")
    scheme = dict(scheme)

    cur = await db.execute(
        "SELECT id, parent_id, title, description, level, sort_order, word_budget"
        " FROM sections WHERE scheme_id=? ORDER BY sort_order, level",
        (scheme_id,),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        raise HTTPException(400, "该方案暂无目录，无法存为目录库")

    # 扁平行 → 三级树（与 sections 表一致的父子结构还原）
    by_parent: dict[str, list[dict]] = {}
    for r in rows:
        by_parent.setdefault(r.get("parent_id") or "", []).append(r)

    def build(parent_id: str, level: int) -> list[dict]:
        out = []
        for r in by_parent.get(parent_id, []):
            node = {
                "title": r.get("title") or "",
                "description": r.get("description") or "",
                "children": build(r["id"], level + 1) if level < 3 else [],
            }
            # ✅ 增强（2026-09-18）：一并沉淀章节字数预算。旧实现只带
            #    title/description/children → 该目录库再被套用到新方案时各章
            #    预算全部回落默认 1500（用户按章节重要性调好的配比丢失）。
            #    _save_outline_to_db 会读 node["word_budget"]，normalize_outline /
            #    renumber_outline 均为就地修改、保留扩展字段。
            _wb = r.get("word_budget")
            if isinstance(_wb, int) and _wb > 0:
                node["word_budget"] = _wb
            if not node["title"]:
                # ✅ R190（FIX-15）：空标题节点不再连坐丢弃整棵子树 —— 旧实现
                #    直接不 append，该节点 build 好的 children 全部静默消失，
                #    目录库与源方案不一致且无任何告警（历史脏数据防御）。
                #    占位重命名保留节点与子树，加法式回传计数。
                node["title"] = "未命名章节"
                nonlocal _empty_titles_filled
                _empty_titles_filled += 1
            out.append(node)
        return out

    _empty_titles_filled = 0
    outline = build("", 1)
    if not outline:
        raise HTTPException(400, "目录解析结果为空")

    name = str((body or {}).get("name") or "").strip() or f"{scheme['name']} - 目录"
    new_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO outline_library (id, name, type, engineering_type, profession,"
        " applicable_conditions, basis, outline_json, tags, version, source,"
        " review_status, ref_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            new_id,
            name,
            (body or {}).get("type") or scheme.get("type") or "",
            (body or {}).get("engineering_type") or "",
            (body or {}).get("profession") or scheme.get("profession") or "",
            (body or {}).get("applicable_conditions") or "",
            (body or {}).get("basis") or "",
            _normalize_for_write(outline, context="from_scheme", library_id=new_id),
            (body or {}).get("tags") or f"{scheme.get('type') or ''},方案沉淀".strip(","),
            "v1.0",
            "方案沉淀",
            "待审核",
            0,
        ),
    )
    await db.commit()
    cur = await db.execute(f"SELECT {_LIST_COLS} FROM outline_library WHERE id=?", (new_id,))
    _lib_row = dict(await cur.fetchone())
    if _empty_titles_filled:
        _lib_row["placeholder_titles_filled"] = _empty_titles_filled
        logger.warning("方案 %s 沉淀目录库：%d 个空标题章节已占位为「未命名章节」（子树保留）", scheme_id[:8], _empty_titles_filled)
    return _lib_row


@router.post("/{library_id}/duplicate")
async def duplicate_library(library_id: str, body: dict | None = None, db=Depends(get_db)):
    """复制（克隆）目录：生成一条新的「待审核」副本，便于在标准目录基础上修改编制。

    请求体（可选）：{"name": "新名称"}，缺省为「原名 - 副本」
    """
    cur = await db.execute("SELECT * FROM outline_library WHERE id=?", (library_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "目录库不存在")
    src = dict(row)
    name = ((body or {}).get("name") or f"{src['name']} - 副本").strip()
    new_id = str(uuid.uuid4())
    # ✅ R213（FIX-2）：复制此前把 src["outline_json"] **原样** INSERT —— 这是目录库
    #    唯一一条完全绕过编号重排/三级裁剪的写入口（create/update/new_version/
    #    restore_version/save_scheme_as_library 都已规范化）。历史脏库（4 级节点、
    #    重复 id、标题内嵌编号）复制后原样放大，导出 .json 回吐脏编号。现与
    #    create_library 同口径；problems 只记 WARNING 不 400 —— 复制旧库是恢复
    #    动作，不该因历史脏标题整条失败（规范化结果已消除脏编号）。
    _copy_json = _normalize_for_write(
        src.get("outline_json") or "[]", context="duplicate", library_id=library_id
    )
    await db.execute(
        "INSERT INTO outline_library (id, name, type, engineering_type, profession,"
        " applicable_conditions, basis, outline_json, tags, version, source,"
        " review_status, ref_count, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            new_id,
            name,
            src.get("type", ""),
            src.get("engineering_type", ""),
            src.get("profession", ""),
            src.get("applicable_conditions", ""),
            src.get("basis", ""),
            _copy_json,
            src.get("tags", ""),
            "v1.0",
            "复制",
            "待审核",
            0,
            src.get("created_by", ""),
        ),
    )
    await db.commit()
    cur = await db.execute(f"SELECT {_LIST_COLS} FROM outline_library WHERE id=?", (new_id,))
    return dict(await cur.fetchone())


def _render_outline_text(nodes, prefix: str = "") -> list[str]:
    """把目录树渲染为多级编号纯文本（1 / 1.1 / 1.1.1）

    ✅ 复用 services.outline_reference 的统一实现（AI 生成参考、导出共用同一渲染口径，
    避免两处各写一遍导致"导出的文本"与"喂给模型的文本"不一致）。
    """
    from app.services.outline_reference import render_outline_text

    return render_outline_text(nodes, prefix).splitlines()


def _render_outline_md(nodes, level: int = 1) -> list[str]:
    """把目录树渲染为 Markdown（标题层级 + 描述引用）"""
    from app.services.outline_reference import render_outline_md

    return render_outline_md(nodes, level).splitlines()


@router.get("/{library_id}/export")
async def export_library(
    library_id: str,
    format: str = Query("text", pattern="^(text|markdown|json)$"),
    db=Depends(get_db),
):
    """导出目录：text（多级编号纯文本）/ markdown / json。

    返回 {file_name, content}，由前端落盘下载。
    """
    cur = await db.execute("SELECT * FROM outline_library WHERE id=?", (library_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "目录库不存在")
    item = dict(row)

    try:
        outline = json.loads(item.get("outline_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(500, "目录库 outline_json 解析失败")
    if isinstance(outline, dict):
        outline = outline.get("outline", [])

    ext = {"text": "txt", "markdown": "md", "json": "json"}[format]
    if format == "json":
        content = json.dumps(
            {
                "name": item["name"],
                "type": item.get("type", ""),
                "version": item.get("version", ""),
                "basis": item.get("basis", ""),
                "applicable_conditions": item.get("applicable_conditions", ""),
                "outline": outline,
            },
            ensure_ascii=False,
            indent=2,
        )
    elif format == "markdown":
        head = [
            f"# {item['name']}",
            "",
            f"- 分类：{item.get('type', '') or '-'}",
            f"- 版本：{item.get('version', '')}",
            f"- 工程类型：{item.get('engineering_type', '') or '-'}",
            f"- 专业：{item.get('profession', '') or '-'}",
        ]
        if item.get("applicable_conditions"):
            head.append(f"- 适用条件：{item['applicable_conditions']}")
        if item.get("basis"):
            head.append(f"- 编制依据：{item['basis']}")
        head += ["", "---", ""]
        content = "\n".join(head + _render_outline_md(outline))
    else:
        head = [item["name"], f"分类：{item.get('type', '') or '-'}    版本：{item.get('version', '')}"]
        if item.get("applicable_conditions"):
            head.append(f"适用条件：{item['applicable_conditions']}")
        if item.get("basis"):
            head.append(f"编制依据：{item['basis']}")
        head += ["", "=" * 60, ""]
        content = "\n".join(head + _render_outline_text(outline))

    return {
        "file_name": f"{item['name']}.{ext}",
        "format": format,
        "content": content,
        "node_count": _count_nodes(outline),
    }


@router.post("/{library_id}/apply")
async def apply_to_scheme(library_id: str, body: dict, db=Depends(get_db)):
    """【已弃用，仅保留只读预览】返回目录库 outline_json，不产生任何副作用。

    ⚠️ 旧实现接收 scheme_id 却从不使用、也从不写库，却在"什么都没应用"的情况下
    把 ref_count+1，导致目录库引用次数虚高（从统计数据看不出真实使用情况）。
    前端已统一改用 /apply-and-save 一步套用；本端点保留仅为兼容旧调用方，
    不再累加引用计数。
    """
    cur = await db.execute(
        "SELECT outline_json FROM outline_library WHERE id=? AND review_status='已通过'", (library_id,)
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(400, "目录库不存在或未审核通过")
    return {
        "outline_json": row[0],
        "deprecated": True,
        "note": "该接口已弃用（不再累加引用计数），请改用 /apply-and-save",
    }


@router.post("/{library_id}/apply-and-save")
async def apply_library_and_save(library_id: str, body: dict, db=Depends(get_db)):
    """将已审核的目录库直接应用并写入方案的 sections 表（一步完成，减少前端往返）。

    与 /apply 的区别：/apply 只返回 outline_json，需前端再调 save-outline；
    /apply-and-save 直接调用 sections._save_outline_to_db 写入数据库，返回保存后的 tree。

    请求体：{"scheme_id": "xxx"}
    """
    scheme_id = body.get("scheme_id", "")
    if not scheme_id:
        raise HTTPException(400, "缺少 scheme_id")

    cur = await db.execute(
        "SELECT id, name, outline_json, source FROM outline_library WHERE id=? AND review_status='已通过'",
        (library_id,),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(400, "目录库不存在或未审核通过")

    outline_data = row["outline_json"]
    if isinstance(outline_data, str):
        try:
            outline_data = json.loads(outline_data)
        except (json.JSONDecodeError, TypeError):
            raise HTTPException(500, "目录库 outline_json 解析失败")
    # ✅ 兼容：部分历史目录库存的是 {"outline": [...]} 整体对象而非数组，
    #    旧实现直接 500"格式错误"，导致这些库无法套用。这里自动解包。
    if isinstance(outline_data, dict) and isinstance(outline_data.get("outline"), list):
        outline_data = outline_data["outline"]
    if not isinstance(outline_data, list):
        raise HTTPException(500, "目录库 outline_json 格式错误（应为数组）")

    # ✅ 竞态守卫（2026-09-21）：套用目录库是「整表重建 sections」的第二个入口，
    # 与 /save-outline 同样会级联 DELETE 正在被正文生成写的章节。前端虽已用
    # disabled={generating} 挡主路径，但接口层必须兜底（直连调用 / 多标签页）。
    from app.routers.sections import content_generation_in_progress, outline_generation_in_progress

    if content_generation_in_progress(scheme_id):
        raise HTTPException(
            409,
            "本方案正文正在后台生成中，请等待生成完成（或先停止任务）后再套用目录库——"
            "套用会整表重建章节，与生成结果互相覆盖",
        )
    # ✅ BUG 修复（2026-09-22）：旧实现只拦正文生成、不拦目录生成 ——
    #    与 /save-outline 的双守卫口径不一致（注释也声明应共用口径）。
    #    目录生成 SSE 完成后的「一级目录确认闸门」会把刚套用的目录整表覆盖。
    if outline_generation_in_progress(scheme_id):
        raise HTTPException(409, "本方案目录正在后台生成中，请等待生成完成（或先停止任务）后再套用目录库")

    # 复用 sections 模块的核心保存逻辑（智能保留已有正文）
    # ✅ R203（套用目录库保留正文）：目录库节点 id 是编号（1/1.1）而非 sections
    #    主键，_save_outline_to_db 按 __original_id 匹配时全部 is_new → 整表重建
    #    级联删除已生成正文。现复用 upload_outline 的「归一化标题 + 结构位置」
    #    匹配思路，为目录库节点注入 __original_id = 已有 section 的 DB 主键，
    #    使 _save_outline_to_db 能匹配旧章节、保留 content/word_count/status。
    #    匹配不到的节点不注入（仍为 is_new → 正常 INSERT）。
    from app.routers.sections import _save_outline_to_db, passthrough_additive

    # ✅ R209（2026-11-10 · 孪生实现收口）：本路径旧用「归一化标题 → 首个 DB 主键」
    #    的一维字典做匹配（无消费、无结构维度），与 upload_outline.save_as_outline
    #    的「三层索引 + consumed 集合」强度相差一个量级，实测两类真实故障：
    #      ① **同名节点撞主键**：目录库树里两个同名节点（标准骨架里「施工准备」
    #         同时出现在多章下是常态）被注入**同一个** __original_id →
    #         _save_outline_to_db 的重复主键闸门 400 → **整个套用失败**；
    #      ② **跨父级张冠李戴**：同名但不同父级的章节被互换，正文挂到
    #         结构不对应的章节上（upload 路径早已修复，此处漏改）。
    #    现统一复用 services.outline_matcher（结构感知 + 消费式，单一事实源）。
    from app.services.outline_matcher import build_outline_matcher

    _cur_existing = await db.execute(
        "SELECT id, parent_id, title, level FROM sections WHERE scheme_id=?", (scheme_id,)
    )
    _matcher = build_outline_matcher([dict(_r) for _r in await _cur_existing.fetchall()])

    _matched = 0

    def _inject_original_ids(nodes: list, path_titles: tuple = (), depth: int = 0) -> None:
        """按结构（标题路径 → 同层 → 同名）认领旧章节，注入 __original_id。

        消费式认领：每个旧章节至多被一个节点认领 ⇒ 同名节点不会撞成同一主键。
        层级按**树深度**计算（不信任节点自带的 level —— 目录库存的是编号而非
        结构，节点可能缺 level 或带旧层级，落库前还会被 normalize 重排）。
        """
        nonlocal _matched
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if not node.get("__original_id"):
                _old = _matcher.take(
                    title=node.get("title", ""),
                    level=depth + 1,
                    parent_id="",  # 目录库树尚未落库，无 DB 父主键 ⇒ 走路径/层级桶
                    path_titles=path_titles,
                )
                if _old:
                    node["__original_id"] = str(_old.get("id") or "")
                    _matched += 1
            _kids = node.get("children")
            if isinstance(_kids, list) and _kids:
                _nt = str(node.get("title") or "").strip()
                _inject_original_ids(_kids, path_titles + ((_nt,) if _nt else ()), depth + 1)

    _inject_original_ids(outline_data)
    if _matched:
        logger.info(
            "方案 %s 套用目录库 %s：%d 个节点按标题匹配已有章节（保留正文）",
            scheme_id[:8], library_id[:8], _matched,
        )

    logger.info(
        "方案 %s 套用目录库 %s：开始整表重建（%d 顶层节点，source=目录库）",
        scheme_id[:8],
        library_id[:8],
        len(outline_data),
    )
    # ✅ R216 收口：本路径已用 build_outline_matcher 做**消费式**认领，未认领
    #    节点即「确认不存在对应旧章节」；必须让 sections 侧跳过兜底匹配，
    #    否则同名节点会二次匹配回同一旧主键 → 重复主键 400（套用目录库整链失败）。
    result = await _save_outline_to_db(
        db, scheme_id, outline_data, source="目录库", skip_fallback_match=True
    )
    logger.info("方案 %s 套用目录库 %s：整表重建完成", scheme_id[:8], library_id[:8])

    # 更新引用计数
    await db.execute("UPDATE outline_library SET ref_count=ref_count+1 WHERE id=?", (library_id,))
    await db.commit()

    return {
        "ok": True,
        "library_id": library_id,
        "scheme_id": scheme_id,
        "count": result["count"],
        "tree": result["tree"],
        "name": row["name"],
        "source": row["source"],
        # ✅ 2026-09-30（P1-7）：透传 `_save_outline_to_db` 的**加法式**回传字段。
        #    套用目录库是「is_new 全 True」的整表重建 —— 目录库节点的 id 是编号
        #    （1/1.1）而非 sections 主键，必然无法匹配旧章节，因此**已生成的正文会
        #    被级联删除**。旧实现只挑了 count / tree，把 `cleared_content_sections`
        #    丢弃，前端无从提示「本次套用清除了 N 章正文」→ 用户静默丢正文。
        # ✅ R88 FIX-8：改为经 **唯一透传口径** `sections.passthrough_additive` ——
        #    此前这里是**手写白名单**，导致 R79 新增的 `generic_residual`（占位标题
        #    明细）又一次被静默丢弃（套用目录库命中占位标题 ⇒ 前端零感知）。
        #    纪律：新增加法式键只改 `sections.SAVE_OUTLINE_ADDITIVE_KEYS`，
        #    消费方一律不得再手写挑选。
        **passthrough_additive(result),
        # ✅ 措辞修正：目录库节点的 id 是"编号"（1/1.1/1.1.1）而非 sections 主键，
        #    无法与已有章节做 id 匹配，因此套用目录库会按新结构重建章节
        #    （旧文案误称"智能保留已有正文"，与 /apply-and-save 的实际行为不符）。
        "note": "目录库已直接写入方案 sections 表（按目录库结构重建章节）",
    }
