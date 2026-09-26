"""提示词管理路由（从 ``main.py`` 拆出）。

端点：
  * ``GET    /api/v1/prompts``                 列表（DB 生效内容 + 审计元数据）
  * ``GET    /api/v1/prompts/audit-logs``      变更审计（指纹 + 变量集合 + diff + 是否可回滚）
  * ``PATCH  /api/v1/prompts/{key}``           保存内容（空内容 = 恢复默认）
  * ``POST   /api/v1/prompts/{key}/reset``     恢复出厂默认
  * ``POST   /api/v1/prompts/{key}/rollback``  回滚到某条审计记录「变更前」的版本

设计要点：
  - 内容更新与审计写入同一事务，提交成功后才更新注册表内存并失效运行时缓存；
  - 审计保存 SHA-256 与变量集合，并按配置保存变更前后正文快照（版本回滚基础）；
  - 列表直接查 DB，重启后不会因内存注册表回退而显示成“未修改”。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request

from app.db import get_db, read_db
from app.services.audit_service import (
    list_prompt_audit_logs, prompt_content_hash, record_prompt_audit,
)

router = APIRouter(prefix="/api/v1/prompts", tags=["prompts"])

#: 提示词内容上限（防止单条超大内容进入数据库与审计链路）。
PROMPT_MAX_CHARS = 200000


@router.get("")
async def list_prompts(category: str = "", db=Depends(read_db)):
    """列出全部提示词，并合并 DB 生效内容、修改时间与审计计数。"""
    from app.services.ai.prompts._registry import (
        list_prompts as _lp, extract_user_variables,
    )
    items = _lp(category) if category else _lp()
    rows = await (await db.execute(
        "SELECT key, content, updated_at FROM prompt_templates")).fetchall()
    meta = {r["key"]: dict(r) for r in rows}
    counts = {
        r["prompt_key"]: int(r["n"] or 0)
        for r in await (await db.execute(
            "SELECT prompt_key, COUNT(*) AS n FROM prompt_audit_logs GROUP BY prompt_key"
        )).fetchall()
    }
    for it in items:
        row = meta.get(it["key"])
        if row and (row.get("content") or "").strip():
            it["content"] = row["content"]
            it["variables"] = extract_user_variables(row["content"])
        it["modified"] = it.get("content", "") != it.get("default_content", "")
        it["content_hash"] = prompt_content_hash(str(it.get("content") or ""))
        it["updated_at"] = (row or {}).get("updated_at") or ""
        it["audit_count"] = counts.get(it["key"], 0)
    return {"items": items}


@router.get("/audit-logs")
async def prompt_audit_logs(key: str, limit: int = 50, offset: int = 0,
                            db=Depends(read_db)):
    """提示词变更审计（只含指纹与变量集合，不复制完整正文）。"""
    try:
        return await list_prompt_audit_logs(db, key, limit, offset)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.patch("/{key}")
async def update_prompt(key: str, body: dict, db=Depends(get_db),
                        request: Request = None):
    """保存提示词编辑；空内容按“恢复默认”处理。"""
    from app.services.ai.prompts._registry import (
        update_prompt as _up, _ALL_PROMPTS, reset_prompt as _reset,
        get_default_prompt, extract_user_variables,
    )
    from app.services.ai.prompts._cache import reload_prompt_cache

    if key not in _ALL_PROMPTS:
        raise HTTPException(404, "提示词不存在")
    content = body.get("content", "")
    if not isinstance(content, str):
        raise HTTPException(400, "提示词内容必须是字符串")
    if len(content) > PROMPT_MAX_CHARS:
        raise HTTPException(400, f"提示词内容过长（最多 {PROMPT_MAX_CHARS} 字符）")

    cur = await db.execute("SELECT content FROM prompt_templates WHERE key=?", (key,))
    row = await cur.fetchone()
    before = (row["content"] if row else "") or get_default_prompt(key)
    before_vars = extract_user_variables(before)

    if not content.strip():
        default = get_default_prompt(key)
        after_vars = extract_user_variables(default)
        try:
            await db.execute("DELETE FROM prompt_templates WHERE key=?", (key,))
            await record_prompt_audit(
                db, key, "reset", before, default, before_vars, after_vars, request)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        _reset(key)
        reload_prompt_cache()
        return {"ok": True, "content": default, "reset": True,
                "content_hash": prompt_content_hash(default),
                # ✅ 2026-09-25（BUG-D · 响应契约不对称）：空内容（=恢复默认）
                #   分支必须与非空分支一样回传 variables —— 前端虽然只用
                #   extractPromptVariables 自行重算，但两端契约不对称会让
                #   后续依赖响应字段的调用方（脚本 / 其它页面）取到 undefined。
                "variables": after_vars,
                "added_variables": sorted(set(after_vars) - set(before_vars)),
                "removed_variables": sorted(set(before_vars) - set(after_vars))}

    after_vars = extract_user_variables(content)
    try:
        await db.execute(
            "INSERT INTO prompt_templates (key, content) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET content=excluded.content,"
            " updated_at=datetime('now','localtime')", (key, content))
        await record_prompt_audit(
            db, key, "update", before, content, before_vars, after_vars, request)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    _up(key, content)
    reload_prompt_cache()
    return {"ok": True, "content": content,
            "content_hash": prompt_content_hash(content),
            "variables": after_vars,
            "added_variables": sorted(set(after_vars) - set(before_vars)),
            "removed_variables": sorted(set(before_vars) - set(after_vars))}


@router.post("/{key}/reset")
async def reset_prompt(key: str, db=Depends(get_db), request: Request = None):
    """恢复出厂默认提示词。"""
    from app.services.ai.prompts._registry import (
        _ALL_PROMPTS, reset_prompt as _reset, get_default_prompt, extract_user_variables,
    )
    from app.services.ai.prompts._cache import reload_prompt_cache
    default = get_default_prompt(key) if key in _ALL_PROMPTS else None
    if default is None:
        raise HTTPException(404, "提示词不存在")
    cur = await db.execute("SELECT content FROM prompt_templates WHERE key=?", (key,))
    row = await cur.fetchone()
    before = (row["content"] if row else "") or default
    before_vars = extract_user_variables(before)
    after_vars = extract_user_variables(default)
    try:
        await db.execute("DELETE FROM prompt_templates WHERE key=?", (key,))
        await record_prompt_audit(
            db, key, "reset", before, default, before_vars, after_vars, request)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    _reset(key)
    reload_prompt_cache()
    return {"ok": True, "content": default,
            "content_hash": prompt_content_hash(default),
            # ✅ 2026-09-25（BUG-D · 响应契约对称）：三个变更端点（PATCH 非空 /
            #   PATCH 空内容 / POST reset）一律回传 variables，供调用方直接
            #   判断「当前生效模板的变量集合」而不必自行重算。
            "variables": after_vars,
            "added_variables": sorted(set(after_vars) - set(before_vars)),
            "removed_variables": sorted(set(before_vars) - set(after_vars))}


@router.post("/{key}/rollback")
async def rollback_prompt(key: str, body: dict, db=Depends(get_db),
                          request: Request = None):
    """把提示词回滚到某条审计记录**变更前**的版本（✅ G2 版本回滚）。

    语义与 ``POST /api/v1/ai/config/{id}/rollback`` 对齐：
      1. 需要目标审计行的快照里带 ``before`` 正文 —— 历史行（引入快照前写入）
         只有哈希、没有正文 → 400 明确提示，不让用户点了必然失败；
      2. 快照正文若被截断（``before_truncated=True``）→ 400 拒绝：
         截断的正文不是完整提示词，写回去等于制造一份损坏的模板；
      3. 回滚本身也写一条 ``action="rollback"`` 审计（同样带前后快照），
         因此「回滚」也可以被再次回滚，形成可撤销的链；
      4. 提示词 key 未注册 → 404；审计行不存在或属于别的 key → 404；
      5. 回滚到「出厂默认」之外仍写 ``prompt_templates``（保持与编辑同口径），
         空内容不写（与「保存空内容 = 恢复默认」一致）。

    请求体：``{"audit_id": "<审计行 id>"}``
    """
    from app.services.ai.prompts._registry import (
        _ALL_PROMPTS, update_prompt as _up,
        get_default_prompt, extract_user_variables,
    )
    from app.services.ai.prompts._cache import reload_prompt_cache

    if key not in _ALL_PROMPTS:
        raise HTTPException(404, "提示词不存在")
    audit_id = (body or {}).get("audit_id") or ""
    if not isinstance(audit_id, str) or not audit_id.strip():
        raise HTTPException(400, "缺少 audit_id")
    audit_id = audit_id.strip()

    cur = await db.execute(
        "SELECT id, prompt_key, snapshot_json FROM prompt_audit_logs WHERE id=?",
        (audit_id,))
    row = await cur.fetchone()
    if not row or row["prompt_key"] != key:
        raise HTTPException(404, "审计记录不存在或不属于该提示词")

    raw = row["snapshot_json"] or ""
    try:
        snap = json.loads(raw) if raw else {}
    except Exception:
        snap = {}
    before = snap.get("before") if isinstance(snap, dict) else None
    if not before:
        raise HTTPException(
            400,
            "该变更记录没有可用的变更前正文（旧记录只存哈希，无法回滚）")
    if snap.get("before_truncated"):
        raise HTTPException(
            400,
            "该变更记录的变更前正文已被截断，不是完整提示词，为避免写入损坏模板"
            "已拒绝回滚。请改用「恢复出厂默认」或手动重新编辑。")
    if len(before) > PROMPT_MAX_CHARS:
        raise HTTPException(400, f"快照正文超长（最多 {PROMPT_MAX_CHARS} 字符）")

    default = get_default_prompt(key)
    cur2 = await db.execute("SELECT content FROM prompt_templates WHERE key=?", (key,))
    r2 = await cur2.fetchone()
    current = (r2["content"] if r2 else "") or default
    current_vars = extract_user_variables(current)
    restored_vars = extract_user_variables(before)

    try:
        if not before.strip():
            await db.execute("DELETE FROM prompt_templates WHERE key=?", (key,))
        else:
            await db.execute(
                "INSERT INTO prompt_templates (key, content) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET content=excluded.content,"
                " updated_at=datetime('now','localtime')", (key, before))
        await record_prompt_audit(
            db, key, "rollback", current, before, current_vars, restored_vars, request)
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # 与「保存/重置」同口径：事务提交成功后才更新内存注册表并失效运行时缓存，
    # 避免出现「已落库但运行时仍用旧缓存」的不一致窗口。
    _up(key, before)
    reload_prompt_cache()
    return {
        "ok": True,
        "content": before,
        "content_hash": prompt_content_hash(before),
        "variables": restored_vars,
        "added_variables": sorted(set(restored_vars) - set(current_vars)),
        "removed_variables": sorted(set(current_vars) - set(restored_vars)),
        "rollback_from_audit": audit_id,
    }

