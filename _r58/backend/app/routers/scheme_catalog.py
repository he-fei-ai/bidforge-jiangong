"""专项方案清单路由：按分类返回预置方案清单，支持搜索和获取标准目录"""
from fastapi import APIRouter, Depends, HTTPException

from app.db import get_db

router = APIRouter(prefix="/api/v1/scheme-catalog", tags=["scheme_catalog"])


@router.get("")
async def list_catalog(category: str = "", keyword: str = "", db=Depends(get_db)):
    """返回方案清单，按分类分组"""
    # ✅ 性能优化：清单列表只展示名称/分类/引用数，不再 SELECT outline_json 大字段
    #    （单条可达数十 KB，旧实现取出后仅丢弃，属无谓传输）。
    sql = "SELECT id, name, type, tags, ref_count FROM outline_library WHERE source='预置清单'"
    params: list = []
    if category:
        sql += " AND type=?"
        params.append(category)
    if keyword:
        sql += " AND (name LIKE ? OR tags LIKE ?)"
        params += [f"%{keyword}%", f"%{keyword}%"]
    sql += " ORDER BY type, name"
    cur = await db.execute(sql, params)
    rows = [dict(r) for r in await cur.fetchall()]

    grouped: dict[str, list] = {}
    for r in rows:
        cat = r["type"] or "其他"
        grouped.setdefault(cat, []).append({
            "id": r["id"],
            "name": r["name"],
            "ref_count": r["ref_count"],
        })

    # ✅ 分类顺序直接取自预置清单定义，避免清单调整后此处硬编码列表失同步
    from app.seed_data import SCHEME_CATALOG
    categories = list(SCHEME_CATALOG.keys())
    ordered = {c: grouped.get(c, []) for c in categories}
    for k in grouped:
        if k not in ordered:
            ordered[k] = grouped[k]

    return {"categories": ordered, "total": len(rows)}


@router.get("/categories")
async def list_categories(db=Depends(get_db)):
    """返回所有分类及其方案数量"""
    cur = await db.execute(
        "SELECT type, COUNT(*) as cnt FROM outline_library WHERE source='预置清单' GROUP BY type ORDER BY type"
    )
    rows = [dict(r) for r in await cur.fetchall()]
    return {"items": rows}


@router.get("/{library_id}")
async def get_catalog_item(library_id: str, db=Depends(get_db)):
    """获取某个方案的标准目录 JSON"""
    cur = await db.execute(
        "SELECT id, name, type, outline_json, tags FROM outline_library WHERE id=? AND source='预置清单'",
        (library_id,),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "方案清单项不存在")
    return dict(row)