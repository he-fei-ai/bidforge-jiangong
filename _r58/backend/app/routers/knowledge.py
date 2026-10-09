"""知识库 / 素材库路由（产品需求文档 §3.9「知识库与素材库」）

consistency_audit / knowledge_base 两张表此前为"规划中功能"（schema 注释明确
勿当作死表删除）。本次补齐 knowledge_base 的业务实现：

- CRUD：按项目/方案维度管理知识条目（企业管理制度、工艺要点、常用数据等）
- 消费：sse_handlers 的目录/正文生成会把项目级知识条目注入 prompt
  （见 _build_knowledge_text），AI 生成时遵守企业规范与既有素材。
"""
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.db import get_db

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


@router.get("")
async def list_knowledge(project_id: str = "", scheme_id: str = "", db=Depends(get_db)):
    """列出知识条目。project_id / scheme_id 均可选；同时传 scheme_id 时
    返回「该方案的 + 项目共享（scheme_id=''）的」条目，生成链路按此消费。"""
    if scheme_id:
        cur = await db.execute(
            "SELECT * FROM knowledge_base WHERE project_id=? AND (scheme_id=? OR scheme_id='') "
            "ORDER BY created_at DESC",
            (project_id, scheme_id))
    elif project_id:
        cur = await db.execute(
            "SELECT * FROM knowledge_base WHERE project_id=? ORDER BY created_at DESC",
            (project_id,))
    else:
        cur = await db.execute("SELECT * FROM knowledge_base ORDER BY created_at DESC")
    return {"items": [dict(r) for r in await cur.fetchall()]}


@router.post("")
async def create_knowledge(body: dict, db=Depends(get_db)):
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(422, "name 不能为空")
    kid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO knowledge_base (id, project_id, scheme_id, name, usage_hint, content)"
        " VALUES (?,?,?,?,?,?)",
        (kid, str(body.get("project_id", "")), str(body.get("scheme_id", "")),
         name, str(body.get("usage_hint", "")), str(body.get("content", ""))))
    await db.commit()
    cur = await db.execute("SELECT * FROM knowledge_base WHERE id=?", (kid,))
    return dict(await cur.fetchone())


@router.put("/{kid}")
async def update_knowledge(kid: str, body: dict, db=Depends(get_db)):
    cur = await db.execute("SELECT id FROM knowledge_base WHERE id=?", (kid,))
    if not await cur.fetchone():
        raise HTTPException(404, "知识条目不存在")
    fields = {}
    for col in ("name", "usage_hint", "content", "scheme_id"):
        if col in body and body[col] is not None:
            fields[col] = str(body[col])
    if fields.get("name", "") == "" and "name" in fields:
        raise HTTPException(422, "name 不能为空")
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        await db.execute(f"UPDATE knowledge_base SET {sets} WHERE id=?", (*fields.values(), kid))
        await db.commit()
    cur = await db.execute("SELECT * FROM knowledge_base WHERE id=?", (kid,))
    return dict(await cur.fetchone())


@router.delete("/{kid}")
async def delete_knowledge(kid: str, db=Depends(get_db)):
    await db.execute("DELETE FROM knowledge_base WHERE id=?", (kid,))
    await db.commit()
    return {"ok": True}


def build_knowledge_text(rows: list[dict], max_total: int = 3000, per_item: int = 500) -> str:
    """把知识条目格式化为可注入 prompt 的文本块（供生成链路复用与单测）。

    - 每条 = 【名称】（用途提示）内容（截断 per_item 字）
    - 空内容条目跳过；总量截断 max_total 字
    """
    parts: list[str] = []
    total = 0
    for r in rows:
        name = str(r.get("name", "")).strip()
        content = str(r.get("content", "")).strip()
        if not name or not content:
            continue
        hint = str(r.get("usage_hint", "")).strip()
        block = f"【{name}】{('（' + hint + '）') if hint else ''}\n{content[:per_item]}"
        if total + len(block) > max_total:
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)


@router.get("/as-text")
async def knowledge_as_text(project_id: str = "", scheme_id: str = "", db=Depends(get_db)):
    """调试/预览端点：返回生成链路实际注入的知识文本块。"""
    items = (await list_knowledge(project_id=project_id, scheme_id=scheme_id, db=db))["items"]
    text = build_knowledge_text(items)
    # item_count 与实际注入口径对齐：仅统计 name+content 均非空、会被注入的条目
    injected = sum(1 for r in items if str(r.get("name", "")).strip() and str(r.get("content", "")).strip())
    return {"text": text, "item_count": injected}


# json 导入保留：后续持久化结构化知识（schema 扩展）时使用
_ = json
