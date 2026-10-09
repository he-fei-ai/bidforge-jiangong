"""AI 配置路由 · 配置增删改 / 激活 / 导入导出 / 降级链。

路由聚合见 ``ai_config/__init__.py``；本模块只负责与「单条配置 CRUD」相关的端点。
"""
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from app.db import get_db, read_db
from app.models import (
    ActiveEnvIn,
    AIConfigIn,
    ConfigImportIn,
    FallbackChainUpdate,
)
from app.services.ai.provider_factory import (
    KNOWN_SCENES,
    PROVIDER_PRESETS,
    RUNTIME_ACTIVE_ENV_KEY,
    RUNTIME_DISABLED_PROVIDERS_KEY,
    apply_config_concurrency,
    clamp_config_numbers,
    clamp_warnings,
    invalidate_config_cache,
    normalize_env,
    normalize_plan,
    normalize_provider_name,
    normalize_request_mode,
    resolve_active_env,
    resolve_config_base_url,
    save_ai_config,
    upsert_runtime_setting,
)
from app.services.crypto import decrypt_api_key

from .audit import record_config_audit, sanitize_config_snapshot

router = APIRouter(tags=["ai_config"])

# 说明：以下端点的 `request: Request = None` 中，默认值 None 仅为兼容
# 单元测试直接调用路由函数（`await save_config(data, db=conn)`）的写法；
# FastAPI 按**注解**识别 Request 并注入真实对象（见
# fastapi/dependencies/utils.py::add_non_field_param_to_dependency），
# 默认值不参与运行时注入。注意不可写成 `Request | None`，
# 联合类型会让 FastAPI 识别不出 Request，被误当作请求体参数。


@router.get("/config")
async def get_config(db=Depends(read_db)):
    """配置列表。

    ✅ 修复（本轮审查）：
      - 原实现把 api_key 明文前 8 位回传前端（`decrypt(...)[:8] + "***"`）。
        加密存储的意义是「密钥不落前端」，前 8 位足以被用于撞库/指纹比对，
        且前端只用它判断「有没有配」。现改为仅回传 key_hint（后 4 位），
        并统一走只读连接池（query_only）。
    """
    cur = await db.execute(
        "SELECT * FROM ai_config ORDER BY is_active DESC, priority ASC, updated_at DESC")
    rows = [dict(r) for r in await cur.fetchall()]
    active_id = ""
    for r in rows:
        enc = r.get("api_key_encrypted", "")
        key = decrypt_api_key(enc) if enc else ""
        if key:
            r["api_key"] = ("*" * 4) + key[-4:]
            r["key_hint"] = key[-4:]
        else:
            r["api_key"] = ""
            r["key_hint"] = ""
        r.pop("api_key_encrypted", None)
        r["has_key"] = bool(enc and key)
        # 密文存在但解不开（换过 FERNET_KEY / 密钥文件被删）——静默以「未配置」呈现，
        # 用户只会看到「明明填了 Key 却调不通」。显式标记，前端给出告警。
        r["key_broken"] = bool(enc and not key)
        r["env"] = str(r.get("env") or "")
        if r.get("is_active"):
            active_id = r["id"]
    # ✅ 2026-09-23（多环境）：回传「当前生效环境」与全部已用环境标签供切换器使用。
    #    active_env 为空串 = 通用（不做任何环境过滤，与引入该功能前行为一致）。
    # ✅ 2026-09-25（缺口修复）：运行时环境值损坏时 resolve_active_env 会
    #    fail-closed 抛错（运行时选模路径保持该语义不变），但配置**列表页**
    #    跟着 500 会让用户连「把环境重置为通用」的入口都看不到（错误配置
    #    不可恢复）。展示端点改为捕获并回传 env_error，前端给出重置入口。
    env_error = ""
    try:
        active_env = await resolve_active_env()
    except ValueError as e:
        active_env, env_error = "", str(e)
    envs = sorted({r["env"] for r in rows if r["env"]})
    return {
        "items": rows,
        "presets": PROVIDER_PRESETS,
        "active_id": active_id,
        "count": len(rows),
        "active_env": active_env,
        "env_error": env_error,
        "envs": envs,
    }


@router.post("/config")
async def save_config(data: AIConfigIn, request: Request = None, db=Depends(get_db)):
    """保存（新增/更新）一条 AI 配置。

    ✅ 修复（本轮审查）：原实现把「是否当前使用」完全交给弹窗里的开关，
      而 `save_ai_config` 在 `is_active` 为真时会先 `UPDATE ai_config SET is_active=0` 再写回 ——
      编辑「当前使用」的那条配置、顺手把开关关掉，库里就再无 `is_active=1`：
      `/ai/health` 立刻变 not_configured、所有 AI 生成能力全线失败，
      而接口仍返回 `{"ok": true}`，用户完全不知道刚把系统关掉了。
      （`toggle` / `delete` 早已修好同一类问题，`save` 此前漏掉了。）
      现与它们对齐：**不允许把唯一的主配置取消激活** —— 本次保存照常写入其他字段，
      但保留 `is_active=1`，并通过 `warning` 明确告知。

    ✅ 2026-09-23 增强：
      1. 数值字段被 `clamp_config_numbers` 静默收敛时，通过 `warnings` 列表
         如实回传（旧版本前端 / 手工构造的请求提交 `concurrency=8` 时，
         之前界面显示 8、实际生效 5，属静默失效）；
      2. 写入一条配置变更审计（**不含明文密钥**）。
    """
    warning = ""
    if not data.is_active:
        cur = await db.execute("SELECT id FROM ai_config WHERE is_active=1")
        actives = [r[0] for r in await cur.fetchall()]
        # 本次保存后仍会处于「当前使用」的配置（更新自己的那条会被关掉，需排除）
        remaining = [a for a in actives if a != (data.id or "")]
        if not remaining:
            data = data.model_copy(update={"is_active": True})
            warning = (
                "已自动保留该配置为「当前使用」：系统不允许存在零个当前使用配置，"
                "否则所有 AI 生成能力会立即不可用。如需停用，请先启用其他配置再关闭本条。"
            )
    warnings = clamp_warnings(data.model_dump())
    # 变更前快照：区分「新增 / 修改」（审计口径），让 detail 只写脱敏摘要，
    # 并留存**结构化前后快照**（不含密钥）供 diff 与回滚使用（✅ G6）。
    prev: dict | None = None
    if data.id:
        cur0 = await db.execute("SELECT * FROM ai_config WHERE id=?", (data.id,))
        r0 = await cur0.fetchone()
        prev = dict(r0) if r0 else None
    try:
        cid = await save_ai_config(data.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    after_row = None
    cur1 = await db.execute("SELECT * FROM ai_config WHERE id=?", (cid,))
    r1 = await cur1.fetchone()
    if r1:
        after_row = dict(r1)
    await record_config_audit(
        db, "update" if prev else "create", config_id=cid,
        provider_name=data.provider_name, model=data.model,
        detail=(f"{'更新' if prev else '新增'}：plan={normalize_plan(data.plan)} "
                f"环境={normalize_env(data.env) or '通用'} "
                f"位置={'当前使用' if data.is_active else '降级候选'} "
                f"密钥={'已填写' if (data.api_key or '').strip() else '未变更'} "
                f"并发={data.concurrency} 温度={data.temperature}"),
        request=request,
        snapshot={"before": sanitize_config_snapshot(prev),
                  "after": sanitize_config_snapshot(after_row)})
    return {"id": cid, "ok": True, "warning": warning, "warnings": warnings}


@router.delete("/config/{config_id}")
async def delete_config(config_id: str, request: Request = None, db=Depends(get_db)):
    cur = await db.execute("SELECT * FROM ai_config WHERE id=?", (config_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "配置不存在")
    if row["is_active"]:
        raise HTTPException(400, "不能删除当前使用中的配置，请先切换到其他配置")
    await db.execute("DELETE FROM ai_config WHERE id=?", (config_id,))
    # ✅ 2026-09-25（缺口修复）：删除配置时同步清理指向它的场景路由。
    #    否则 ai_scene_routes 留下僵尸行：界面永远显示「配置已删除」红标，
    #    用户还得逐场景手动清除；运行时虽会回落，但数据链已断（DB 与展示
    #    永远对不上）。删除路由行后场景自动恢复「跟随当前使用配置」。
    cur_routes = await db.execute(
        "SELECT COUNT(*) FROM ai_scene_routes WHERE config_id=?", (config_id,))
    cleared_routes = int((await cur_routes.fetchone())[0] or 0)
    if cleared_routes:
        await db.execute(
            "DELETE FROM ai_scene_routes WHERE config_id=?", (config_id,))
    # ✅ 修复：删除后 priority 留下空洞（0,2,3…），降级链顺序仍旧可用但序号不连续。
    #    统一重排为 0..n-1，保证与前端展示的 1..n 一致。
    await _resequence_priority(db)
    await record_config_audit(
        db, "delete", config_id=config_id,
        provider_name=row["provider_name"], model=row["model"],
        detail=f"删除配置：{row['provider_name']}/{row['model']}"
               f"（密钥{'已设置' if row['api_key_encrypted'] else '未设置'}，已随配置一并删除）"
               + (f"；已同步解除 {cleared_routes} 条场景路由" if cleared_routes else ""),
        request=request,
        # 快照留存删除前状态（供「这条配置当时是什么样」追溯；配置已删故不可回滚）
        snapshot={"before": sanitize_config_snapshot(dict(row)), "after": None},
        commit=False)
    await db.commit()
    invalidate_config_cache()
    return {"ok": True, "cleared_scene_routes": cleared_routes}


@router.delete("/config/{config_id}/key")
async def clear_config_key(config_id: str, request: Request = None, db=Depends(get_db)):
    """清除某条配置已保存的 API Key（✅ 2026-09-23 新增）。

    补齐「密钥管理」缺口：此前界面只能整体删除配置才能移除 Key，
    而「当前使用」的配置**不允许删除** —— 用户实际上无法收回已保存的密钥。

    安全约定：只清空密文列，明文 Key 从不回传；清除后若该配置正被使用，
    返回 `warning` 明确告知「AI 生成能力将不可用」，不再静默失效。
    """
    cur = await db.execute(
        "SELECT is_active, provider_name, model, api_key_encrypted FROM ai_config WHERE id=?",
        (config_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "配置不存在")

    warning = ""
    if not row["api_key_encrypted"]:
        # 幂等：本来就没有 Key，不写审计噪音
        return {"ok": True, "unchanged": True, "warning": ""}
    if row["is_active"]:
        warning = ("已清除该配置的 API Key，但它是当前的「当前使用」配置 —— "
                   "在所有 AI 调用成功前，请尽快补填新 Key 或切换到其他配置。")
    # ✅ 2026-09-25（缺口修复）：被场景路由引用的配置清 Key 后，相关场景会在
    #    运行时被静默跳过并回落主配置 —— 此前完全无提示（静默失效）。
    cur_routes = await db.execute(
        "SELECT COUNT(*) FROM ai_scene_routes WHERE config_id=?", (config_id,))
    n_routes = int((await cur_routes.fetchone())[0] or 0)
    if n_routes:
        route_warn = (f"该配置被 {n_routes} 条场景路由引用，清除 Key 后这些场景"
                      "将自动回落「当前使用」配置/降级链，直到重新填写 Key。")
        warning = f"{warning}{route_warn}" if warning else route_warn

    await db.execute(
        "UPDATE ai_config SET api_key_encrypted='', updated_at=? WHERE id=?",
        (datetime.now().isoformat(), config_id))
    await record_config_audit(
        db, "clear_key", config_id=config_id,
        provider_name=row["provider_name"], model=row["model"],
        detail=f"清除已保存的 API Key：{row['provider_name']}/{row['model']}"
               + ("（该配置为当前使用）" if row["is_active"] else "")
               + (f"；解除 {n_routes} 条场景路由引用" if n_routes else ""),
        request=request,
        snapshot={"before": sanitize_config_snapshot(dict(row)),
                  "after": sanitize_config_snapshot(
                      {**dict(row), "api_key_encrypted": ""})},
        commit=False)
    await db.commit()
    invalidate_config_cache()
    return {"ok": True, "warning": warning}


async def _resequence_priority(db) -> None:
    """把 ai_config.priority 重排为连续的 0..n-1（按现有顺序）。"""
    cur = await db.execute(
        "SELECT id FROM ai_config ORDER BY priority ASC, updated_at DESC")
    for i, r in enumerate(await cur.fetchall()):
        await db.execute("UPDATE ai_config SET priority=? WHERE id=?", (i, r[0]))


@router.patch("/config/{config_id}/toggle")
async def toggle_config(config_id: str, request: Request = None, db=Depends(get_db)):
    """设为「当前使用」（唯一主配置）。

    ✅ 修复：原实现允许把唯一的主配置取消激活，之后 ai_config 里再无 is_active=1，
      `/ai/health` 立即变成 not_configured、AI 生成全线失败，而接口只返回 ok:true，
      用户完全不知道自己刚把系统关掉了。现明确拒绝并给出操作指引。

    ✅ 增强（本轮审查）：激活一条「没填 Key / 密文解不开」的配置时，
      原实现只回 ok:true，用户以为切好了 —— 实际下一次调用必然失败。
      现额外返回 `warning`（不再静默），前端据此提示。
    """
    cur = await db.execute("SELECT * FROM ai_config WHERE id=?", (config_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "配置不存在")
    if row["is_active"]:
        # 已经处于「当前使用」，重复点击按幂等处理（不再把系统置为无主配置）
        return {"ok": True, "is_active": True, "unchanged": True}
    _before_snap = sanitize_config_snapshot(dict(row))

    warning = ""
    enc = row["api_key_encrypted"] or ""
    key = decrypt_api_key(enc) if enc else ""
    if not key:
        warning = ("该配置已设为当前使用，但它的 API Key 不可用：" + (
            "已保存的密文无法解密（常见于更换过加密密钥 FERNET_KEY 或删除过 "
            "data/secret_key.key），请重新填写并保存 API Key。"
            if enc else "尚未填写 API Key，请补填后再使用。"))

    await db.execute("UPDATE ai_config SET is_active=0")
    await db.execute("UPDATE ai_config SET is_active=1, updated_at=? WHERE id=?",
                     (datetime.now().isoformat(), config_id))
    cur = await db.execute("SELECT * FROM ai_config WHERE id=?", (config_id,))
    _r = await cur.fetchone()
    await record_config_audit(
        db, "toggle", config_id=config_id,
        provider_name=str(row["provider_name"] or ""), model=str(row["model"] or ""),
        detail=f"切换「当前使用」配置为：{row['provider_name']}/{row['model']}",
        request=request,
        snapshot={"before": _before_snap,
                  "after": sanitize_config_snapshot(dict(_r) if _r else None)},
        commit=False)
    await db.commit()
    invalidate_config_cache()
    # ✅ 统一并发体系：切换活跃配置后自动应用该配置的并发数
    from app.services.ai.provider_factory import apply_config_concurrency
    await apply_config_concurrency()
    return {"ok": True, "is_active": True, "warning": warning}


@router.put("/fallback-chain")
async def update_fallback_chain(body: FallbackChainUpdate, request: Request = None,
                                db=Depends(get_db)):
    """更新降级链优先级。

    ✅ 增强：原实现对未知 id 静默忽略（前端拖错/配置已删时无任何反馈），
      且不保证序号连续。现校验 id 合法性并统一重排为 0..n-1。

    ✅ 修复（本轮审查）：
      1. 入参由裸 `dict` 改为强类型 `FallbackChainUpdate`
         —— `{"items": [...]}` 这类结构写错此前只会得到「降级链不能为空」400，
         看不出是字段名写错了；
      2. 重复 id 去重 —— 同一条配置出现两次时，后写的一次会覆盖前一次的
         priority，「保存成功」但顺序与界面不一致（静默错序）。
    """
    ids: list[str] = []
    for item in body.chain:
        if isinstance(item, str):
            cid = item.strip()
        elif isinstance(item, dict):
            cid = str(item.get("id", "") or "").strip()
        else:
            cid = ""
        if cid and cid not in ids:
            ids.append(cid)
    if not ids:
        raise HTTPException(400, "降级链不能为空（请提交 [{\"id\": \"...\"}, ...]）")

    cur = await db.execute(
        f"SELECT id FROM ai_config WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))
    known = {r[0] for r in await cur.fetchall()}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise HTTPException(400, f"以下配置不存在（可能已被删除）：{', '.join(unknown[:3])}")

    for i, cid in enumerate(ids):
        await db.execute("UPDATE ai_config SET priority=? WHERE id=?", (i, cid))
    await record_config_audit(
        db, "fallback_chain", detail=f"降级链顺序调整为 {len(ids)} 个候选", request=request,
        commit=False)
    await db.commit()
    # ✅ 修复（F-2 复发）：降级链有 5 分钟缓存（P0-4 性能优化引入），
    #    改完 priority 不失效缓存的话，`_fallback_chain()` 会继续返回旧顺序，
    #    用户「调完顺序、界面显示已保存、运行时最长 5 分钟无变化」——
    #    与当年「配置写进库 ≠ 生效」同一类静默失效，必须在此显式失效。
    invalidate_config_cache()
    return {"ok": True, "count": len(ids)}


def _classify_import_item(raw, existing: set, overwrite: bool) -> dict:
    """校验并归一化一条导入条目（纯函数，不落库）。

    「导入校验」的**单一出口**：``import_config``（真导入）与
    ``import_config_dry_run``（导入预演）共用同一判据 —— 否则会出现
    「预演说会通过、真导入却跳过」的静默错配。

    返回::

        {"action": "skip" | "new" | "overwrite",  该行将如何处理
         "reason": str,                           action=="skip" 时的原因（非空）
         "invalid_plan": bool,                    因「计费方式 ↔ Base URL」联动被拒
         "clamped": bool,                         数值字段被静默收敛
         "provider_name" / "model" / "plan" / "base_url" / "nums" /
         "request_mode" / "env" / "remark"}
    """
    out: dict = {
        "action": "new", "reason": "", "invalid_plan": False, "clamped": False,
        "provider_name": "", "model": "", "plan": "pay_as_you_go", "base_url": "",
        "nums": {}, "request_mode": "normal", "env": "", "remark": "",
    }
    if not isinstance(raw, dict):
        out.update({"action": "skip", "reason": "条目格式非法（不是对象）"})
        return out
    provider_name = (raw.get("provider_name") or "").strip()
    model = (raw.get("model") or "").strip()
    out["provider_name"], out["model"] = provider_name, model
    if not provider_name or not model:
        out.update({"action": "skip",
                    "reason": f"{provider_name or '(空)'}：供应商或模型名称为空"})
        return out
    plan = normalize_plan(raw.get("plan"))
    out["plan"] = plan
    # 「计费方式 ↔ Base URL」联动校验与保存路径共用 resolve_config_base_url：
    # 包月套餐 / 自定义供应商必须手填地址，违规条目按既有语义跳过并如实回传原因
    # （不静默写脏数据 —— 否则「包月配置实际按量调用」）。
    try:
        base_url = resolve_config_base_url(provider_name, plan, raw.get("base_url") or "")
    except ValueError as e:
        out.update({"action": "skip", "invalid_plan": True,
                    "reason": f"{provider_name}/{model}：{e}"})
        return out
    out["base_url"] = base_url
    nums = clamp_config_numbers(raw)
    out["nums"] = nums
    out["clamped"] = bool(clamp_warnings(raw))
    out["request_mode"] = normalize_request_mode(raw.get("request_mode"))
    try:
        out["env"] = normalize_env(raw.get("env"))
    except ValueError:
        out.update({"action": "skip",
                    "reason": f"{provider_name}/{model}：环境标签非法"})
        return out
    out["remark"] = str(raw.get("remark") or "").strip()[:2000]
    if (provider_name, model, base_url) in existing:
        if overwrite:
            out["action"] = "overwrite"
        else:
            out.update({"action": "skip",
                        "reason": f"{provider_name}/{model}：已存在同名配置（未开启覆盖）"})
    return out


async def _export_scene_routes(db) -> list:
    """导出场景路由（scene → config_id；不含任何敏感字段）。"""
    try:
        cur = await db.execute(
            "SELECT scene, config_id, updated_at FROM ai_scene_routes"
            " WHERE config_id != '' ORDER BY scene")
        return [dict(r) for r in await cur.fetchall()]
    except Exception:
        # 表未创建等异常按「无路由」处理，不影响配置主流程导出。
        return []


async def _export_runtime(db) -> dict:
    """导出运行时设置（当前生效环境 / 运行时禁用厂商清单）。

    只导出**表中确实存在**的键，不导出「从未设置过」的键 —— 保持
    「显式设置为空」与「从未设置」两种语义可区分（与 load_runtime_setting
    返回 None 的约定同口径）。
    """
    out: dict = {}
    try:
        for key in (RUNTIME_ACTIVE_ENV_KEY, RUNTIME_DISABLED_PROVIDERS_KEY):
            cur = await db.execute(
                "SELECT value FROM ai_runtime_settings WHERE key=?", (key,))
            row = await cur.fetchone()
            if row is not None:
                out[key] = str(row["value"] or "")
    except Exception:
        pass
    return out


@router.get("/config/export")
async def export_config(db=Depends(read_db)):
    """导出全部配置（✅ 新增：备份/迁移）。

    安全约定：**绝不导出 API Key**（含密文），导入后需重新填写 Key，
    避免密钥随配置文件在邮件/聊天工具中流转。

    ✅ 2026-10-06（G1 · 迁移完整性）：此前只导出 ``ai_config`` 单表，跨机器
      迁移后 ``ai_scene_routes``（24 个场景的模型路由）与
      ``ai_runtime_settings``（当前生效环境 / 运行时厂商开关）全部丢失 ——
      用户须在界面逐场景重配，且运行时开关丢失后无法追溯「当时禁用了哪些厂商」。
      现加法式导出这两个顶层可选键（不含任何密钥）；``version`` 保持 1
      （新键为可选，旧版导入逻辑忽略未知键即兼容）。
    """
    cur = await db.execute("SELECT * FROM ai_config ORDER BY priority ASC, updated_at DESC")
    items = []
    for r in await cur.fetchall():
        d = dict(r)
        d.pop("api_key_encrypted", None)
        items.append(d)
    scene_routes = await _export_scene_routes(db)
    runtime = await _export_runtime(db)
    return {
        "version": 1,
        "exported_at": datetime.now().isoformat(),
        "count": len(items),
        "api_key_included": False,
        "items": items,
        # ✅ 场景路由与运行时设置（可选键；旧调用方忽略即可）
        "scene_routes": scene_routes,
        "runtime": runtime,
        "scene_route_count": len(scene_routes),
        "runtime_keys": sorted(runtime),
    }


@router.post("/config/import")
async def import_config(body: ConfigImportIn, request: Request = None, db=Depends(get_db)):
    """导入配置（✅ 新增：备份/迁移；API Key 需重新填写）。

    ✅ 修复（本轮审查）：
      1. 数值字段（max_tokens / temperature / timeout / concurrency）此前原样落库，
         导入文件可写入 `temperature=99` 这类越界值并一路带到厂商请求 ——
         现在统一走 `clamp_config_numbers`（与表单保存同一口径）；
      2. `plan` 走白名单归一，避免脏值让前端计费方式列显示乱码；
      3. 导入完成后如果库里没有任何 `is_active=1` 的配置（例如原本就没启用过、
         或用户刻意没勾「设为当前使用」），返回 `warning` 明确告知 ——
         否则用户看到「导入成功」却依然什么都生成不了。
    """
    if not body.items:
        raise HTTPException(400, "导入内容为空")

    cur = await db.execute(
        "SELECT provider_name, model, base_url FROM ai_config")
    existing = {(r[0], r[1], r[2]) for r in await cur.fetchall()}

    imported, skipped = 0, 0
    clamped_items = 0   # 数值被收敛的条目数（导入文件可能来自旧版本/被手工改过）
    invalid_plan_items = 0  # 因「计费方式 ↔ Base URL」联动校验不合规而跳过的条目数
    import_skip_reasons: list[str] = []   # 跳过原因（最多回传前若干条，避免载荷膨胀）
    first_new_id = ""
    import_changes: list[tuple[str, str, str, str, dict | None, dict | None]] = []
    # ✅ 2026-10-06（G1 · 迁移完整性）：导出源机器上的配置 id → 本次导入落库的 id。
    #    导出文件的 items 携带原始 id，用它把场景路由的 config_id 映射到新 id，
    #    否则迁移后场景路由指向不存在的配置（与 delete_config 清理僵尸路由是
    #    同一类数据链断裂问题）。
    key_to_new_id: dict[str, str] = {}
    for raw in body.items:
        # 真导入与「导入预演」共用 _classify_import_item（导入校验的单一出口）：
        # 两条路径若各写一份判据，会出现「预演说会通过、真导入却跳过」的静默错配。
        c = _classify_import_item(raw, existing, body.overwrite)
        if c["action"] == "skip":
            skipped += 1
            if c["invalid_plan"]:
                invalid_plan_items += 1
            if c["reason"]:
                import_skip_reasons.append(c["reason"])
            continue
        if c["clamped"]:
            clamped_items += 1
        provider_name, model = c["provider_name"], c["model"]
        plan, base_url, nums = c["plan"], c["base_url"], c["nums"]
        request_mode, env, remark = c["request_mode"], c["env"], c["remark"]
        old_id = str((raw or {}).get("id") or "")
        if c["action"] == "overwrite":
            # 历史库可能存在同一三元组的重复行；逐行留快照，避免 UPDATE 多行却只审计一行。
            before_rows = [
                dict(r) for r in await (await db.execute(
                    "SELECT * FROM ai_config WHERE provider_name=? AND model=? AND base_url=?",
                    (provider_name, model, base_url))).fetchall()
            ]
            await db.execute(
                "UPDATE ai_config SET plan=?, max_tokens=?, temperature=?, timeout=?, concurrency=?,"
                " request_mode=?, env=?, remark=?, updated_at=?"
                " WHERE provider_name=? AND model=? AND base_url=?",
                (plan, nums["max_tokens"], nums["temperature"], nums["timeout"],
                 nums["concurrency"], request_mode, env, remark,
                 datetime.now().isoformat(), provider_name, model, base_url))
            for before_row in before_rows:
                key_to_new_id[str(before_row.get("id") or "")] = str(before_row["id"])
                after_cur = await db.execute(
                    "SELECT * FROM ai_config WHERE id=?", (before_row["id"],))
                after_row = await after_cur.fetchone()
                import_changes.append((
                    before_row["id"], provider_name, model, "覆盖", before_row,
                    dict(after_row) if after_row else None,
                ))
            imported += 1
            continue

        new_id = str(uuid.uuid4())
        cur = await db.execute("SELECT COALESCE(MAX(priority), -1) + 1 FROM ai_config")
        pr = (await cur.fetchone())[0]
        await db.execute(
            "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url, model,"
            " max_tokens, temperature, timeout, concurrency, request_mode, env,"
            " is_active, priority, remark)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
            (new_id, provider_name, plan, "",
             base_url, model, nums["max_tokens"], nums["temperature"],
             nums["timeout"], nums["concurrency"], request_mode, env,
             int(pr) if pr is not None else 0, remark))
        existing.add((provider_name, model, base_url))
        if old_id:
            key_to_new_id[old_id] = new_id
        if not first_new_id:
            first_new_id = new_id
        new_cur = await db.execute("SELECT * FROM ai_config WHERE id=?", (new_id,))
        new_row = await new_cur.fetchone()
        import_changes.append((
            new_id, provider_name, model, "新增", None,
            dict(new_row) if new_row else None,
        ))
        imported += 1

    if body.set_first_active and first_new_id:
        await db.execute("UPDATE ai_config SET is_active=0")
        await db.execute("UPDATE ai_config SET is_active=1 WHERE id=?", (first_new_id,))

    for cid, provider_name, model, change_kind, before_row, _after_row in import_changes:
        # set_first_active 可能在本轮末尾改变 is_active；审计必须在全部导入写操作
        # 完成后重读最终行，避免快照与真实落库状态不一致。
        final_cur = await db.execute("SELECT * FROM ai_config WHERE id=?", (cid,))
        final_row = await final_cur.fetchone()
        await record_config_audit(
            db, "import", config_id=cid, provider_name=provider_name, model=model,
            detail=f"导入{change_kind}配置：{provider_name}/{model}",
            request=request, commit=False,
            snapshot={"before": sanitize_config_snapshot(before_row),
                      "after": sanitize_config_snapshot(
                          dict(final_row) if final_row else None)})
    # ✅ 2026-10-06（G1 · 迁移完整性）：迁移场景路由与运行时设置。
    #    两者均为可选（默认 None = 不迁移），旧版导出文件不含这些键时行为不变。
    migrated_scene_routes = 0
    migrated_runtime_keys: list[str] = []
    if body.scene_routes is not None:
        migrated_scene_routes = await _import_scene_routes(
            db, body.scene_routes, key_to_new_id, import_skip_reasons)
    if body.runtime is not None:
        migrated_runtime_keys = await _import_runtime(db, body.runtime)

    route_note = ""
    if body.scene_routes is not None or body.runtime is not None:
        route_note = (f"；场景路由迁移 {migrated_scene_routes} 条"
                      f"，运行时设置迁移 {len(migrated_runtime_keys)} 项")
    await record_config_audit(
        db, "import", detail=f"导入配置汇总：新增/覆盖 {imported} 条、跳过 {skipped} 条"
                             f"（overwrite={bool(body.overwrite)}，"
                             f"set_first_active={bool(body.set_first_active)}）{route_note}",
        request=request, commit=False)
    await db.commit()
    invalidate_config_cache()
    # ✅ 2026-09-25（缺口修复）：set_first_active 激活了新主配置后必须重放并发 ——
    #    save_ai_config / toggle_config / set_active_env 均调用了
    #    apply_config_concurrency，唯独导入路径遗漏 → 导入激活的配置并发数
    #    不生效（要等下次保存/切换才生效），属「配置写进库 ≠ 生效」的静默失效。
    await apply_config_concurrency()

    # ✅ 增强：导入后若系统仍无「当前使用」配置，明确告知（否则用户以为导入即可用）
    cur = await db.execute("SELECT COUNT(*) FROM ai_config WHERE is_active=1")
    has_active = bool((await cur.fetchone())[0])
    warning = "" if has_active else (
        "导入完成，但当前没有任何配置处于「当前使用」状态，AI 生成能力仍不可用："
        "请在列表中点击目标配置的「设为当前使用」。"
    )
    return {
        "ok": True, "imported": imported, "skipped": skipped,
        "activated_id": first_new_id if body.set_first_active else "",
        "warning": warning,
        # ✅ 2026-09-23：导入文件里的越界数值会被静默收敛，如实回传条目数
        "clamped_items": clamped_items,
        # ✅ 2026-10-06（B-1）：因联动校验被跳过的条目数与原因（前 5 条）。
        #    不回传的话用户只会看到「导入 N 条、跳过 M 条」，不知道 M 条为何被跳。
        "invalid_plan_items": invalid_plan_items,
        "skip_reasons": import_skip_reasons[:5],
        # ✅ 2026-10-06（G1 · 迁移完整性）：随配置一并迁移的派生数据数量。
        #    迁移不完整的场景路由（原配置未包含在本次导入中）同样计入 skip_reasons。
        "migrated_scene_routes": migrated_scene_routes,
        "migrated_runtime_keys": migrated_runtime_keys,
        "hint": "导入的配置不含 API Key，请在列表中逐条补齐 Key 后再启用",
    }


@router.post("/config/import/dry-run")
async def import_config_dry_run(body: ConfigImportIn, db=Depends(read_db)):
    """导入预演（✅ 2026-10-06 G1）：只校验不落库。

    导入是**整批**操作，用户看不到「这批文件里哪些会新增、哪些会覆盖、哪些会被
    跳过、跳过原因是什么」就只能盲点导入。本端点复用 ``_classify_import_item``
    （与真导入**同一判据**）返回逐条处置计划与汇总，**不产生任何写入、不写审计**。

    ⚠️ 判据单一出口：本端点绝不允许另写一份校验逻辑 —— 否则会出现
    「预演说会通过、真导入却跳过」的静默错配（本仓已多次踩同类陷阱）。
    """
    cur = await db.execute(
        "SELECT provider_name, model, base_url FROM ai_config")
    # 批内去重与真导入同口径：前一条新增的三元组会挡住后一条同名单元
    existing = {(r[0], r[1], r[2]) for r in await cur.fetchall()}

    planned: list[dict] = []
    skipped = clamped_items = invalid_plan_items = 0
    skip_reasons: list[str] = []
    key_to_new_id: dict[str, str] = {}
    for raw in body.items:
        c = _classify_import_item(raw, existing, body.overwrite)
        if c["action"] == "skip":
            skipped += 1
            if c["invalid_plan"]:
                invalid_plan_items += 1
            if c["reason"]:
                skip_reasons.append(c["reason"])
            planned.append({"action": "skip", "reason": c["reason"],
                            "provider_name": c["provider_name"], "model": c["model"]})
            continue
        if c["clamped"]:
            clamped_items += 1
        old_id = str((raw or {}).get("id") or "")
        if c["action"] == "overwrite":
            key_to_new_id[old_id] = old_id
        else:
            existing.add((c["provider_name"], c["model"], c["base_url"]))
            key_to_new_id[old_id] = "__new__"
        planned.append({
            "action": c["action"], "provider_name": c["provider_name"],
            "model": c["model"], "plan": c["plan"], "env": c["env"],
            "request_mode": c["request_mode"], "clamped": c["clamped"],
        })

    # 场景路由预演：能否映射到本次导入会落库的配置 id（与真导入同一批跳过原因）
    route_planned = route_skipped = 0
    route_reasons: list[str] = []
    if body.scene_routes is not None:
        for raw in (body.scene_routes if isinstance(body.scene_routes, list) else []):
            if not isinstance(raw, dict):
                continue
            scene = str(raw.get("scene") or "").strip()
            old_cfg_id = str(raw.get("config_id") or "").strip()
            if not scene or not old_cfg_id:
                continue
            if scene not in KNOWN_SCENES:
                route_skipped += 1
                route_reasons.append(f"场景路由「{scene}」：未在场景白名单登记，已跳过")
                continue
            if old_cfg_id not in key_to_new_id:
                route_skipped += 1
                route_reasons.append(
                    f"场景路由「{scene}」：原配置未包含在本次导入中，已跳过")
                continue
            route_planned += 1

    runtime_planned: list[str] = []
    runtime_skipped: list[str] = []
    if body.runtime is not None and isinstance(body.runtime, dict):
        for key, value in body.runtime.items():
            if key == RUNTIME_ACTIVE_ENV_KEY:
                try:
                    normalize_env(value)
                    runtime_planned.append(key)
                except ValueError:
                    runtime_skipped.append(key)
            elif key == RUNTIME_DISABLED_PROVIDERS_KEY:
                try:
                    names = json.loads(value) if isinstance(value, str) and value else []
                except Exception:
                    names = None
                if isinstance(names, list):
                    runtime_planned.append(key)
                else:
                    runtime_skipped.append(key)
            else:
                runtime_skipped.append(key)

    return {
        "ok": True,
        "planned": planned,
        "total": len(body.items),
        "would_import": len(planned) - skipped,
        "skipped": skipped,
        "clamped_items": clamped_items,
        "invalid_plan_items": invalid_plan_items,
        "skip_reasons": skip_reasons[:5],
        "scene_routes": {"planned": route_planned, "skipped": route_skipped,
                         "skip_reasons": route_reasons[:5]},
        "runtime": {"planned": runtime_planned, "skipped": runtime_skipped},
        "hint": "预演不产生任何写入；确认无误后再点「导入」",
    }


# ---------------------------------------------------------------------------
# ✅ 2026-10-06 新增：配置迁移完整性 —— 场景路由 / 运行时设置随配置一并迁移
# ---------------------------------------------------------------------------
async def _import_scene_routes(db, raw_routes, key_to_new_id, reasons) -> int:
    """迁移场景路由（scene → 本次导入落库的新 config_id）。

    导出源机器上的 config_id 在新机器上不存在，必须按「导出文件 items 携带的
    原始 id → 本次导入生成的新 id」映射；映射不到（原配置被跳过或未包含在本次
    导入中）的场景按跳过处理并如实回传原因 —— 绝不写成指向不存在配置的僵尸行
    （与 delete_config 清理僵尸路由是同一类数据链断裂问题：界面会永久显示
    「配置已删除」而运行时只能回落，数据与展示永远对不上）。

    非法值一律跳过而非抛出：导入文件来自外部（邮件/聊天/手工改过），单个场景
    写错不该让整份迁移失败；scene 白名单判据与 PUT /ai/scene-routes 同源。
    """
    migrated = 0
    if not isinstance(raw_routes, list):
        return migrated
    for raw in raw_routes:
        if not isinstance(raw, dict):
            continue
        scene = str(raw.get("scene") or "").strip()
        old_cfg_id = str(raw.get("config_id") or "").strip()
        if not scene or not old_cfg_id:
            continue
        if scene not in KNOWN_SCENES:
            reasons.append(f"场景路由「{scene}」：未在场景白名单登记，已跳过")
            continue
        new_id = key_to_new_id.get(old_cfg_id, "")
        if not new_id:
            reasons.append(f"场景路由「{scene}」：原配置未包含在本次导入中，已跳过")
            continue
        await db.execute(
            "INSERT INTO ai_scene_routes (scene, config_id, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(scene) DO UPDATE SET config_id=excluded.config_id,"
            " updated_at=excluded.updated_at",
            (scene, new_id, datetime.now().isoformat()))
        migrated += 1
    return migrated


async def _import_runtime(db, raw_runtime) -> list[str]:
    """迁移运行时设置（当前生效环境 / 运行时禁用厂商清单）。

    两个键都走与运行时端点**同一套**归一化 —— 导入文件来自任何来源，脏值
    不能绕过 PUT /env 与 PUT /runtime/disabled-providers 的校验：
      - 环境名走 normalize_env（非法抛 ValueError → 跳过该键）；
      - 厂商名走 normalize_provider_name（非法/空值逐条剔除）。

    只写入白名单内的两个键，未知键忽略（前向兼容未来新增的运行时设置）。
    返回实际写入的键名列表。
    """
    migrated: list[str] = []
    if not isinstance(raw_runtime, dict):
        return migrated
    for key, value in raw_runtime.items():
        try:
            if key == RUNTIME_ACTIVE_ENV_KEY:
                await upsert_runtime_setting(db, key, normalize_env(value))
            elif key == RUNTIME_DISABLED_PROVIDERS_KEY:
                names = json.loads(value) if isinstance(value, str) and value else []
                if not isinstance(names, list):
                    continue
                clean = []
                for n in names:
                    if not isinstance(n, str) or not n.strip():
                        continue
                    try:
                        nm = normalize_provider_name(n)
                    except ValueError:
                        continue
                    if nm:
                        clean.append(nm)
                await upsert_runtime_setting(
                    db, key, json.dumps(sorted(set(clean)), ensure_ascii=False))
            else:
                continue
            migrated.append(key)
        except ValueError:
            # 非法环境名等：跳过该键，不影响其它键与配置主体的导入。
            continue
    return migrated


# ---------------------------------------------------------------------------
# ✅ 2026-09-23 新增：多环境 —— 当前生效环境的读取与切换（G5）
# ---------------------------------------------------------------------------
@router.get("/env")
async def get_active_env(db=Depends(read_db)):
    """当前生效环境 + 全部已用环境标签（供前端环境切换器）。

    ``active_env`` 为空串表示**通用环境**：不做任何环境过滤，
    主配置/降级链的选取口径与引入多环境前完全一致。
    """
    # ✅ 2026-09-25：同 ``get_config`` —— 环境值损坏时仍要能读到环境切换器，
    #    用户才能把环境重置为通用（PUT /env 写入合法值即可恢复）。
    env, env_error = "", ""
    try:
        env = await resolve_active_env()
    except ValueError as e:
        env, env_error = "", str(e)
    envs: list[str] = []
    try:
        cur = await db.execute("SELECT DISTINCT env FROM ai_config")
        envs = sorted({str(dict(r).get("env") or "").strip()
                       for r in await cur.fetchall()} - {""})
    except Exception:
        # 旧库缺 env 列（未重启触发迁移）时不影响主流程
        envs = []
    return {"active_env": env, "envs": envs, "routed": bool(env),
            "env_error": env_error}


@router.put("/env")
async def set_active_env(body: ActiveEnvIn, request: Request = None, db=Depends(get_db)):
    """切换「当前生效环境」（空串 = 通用）。

    ✅ 无需重启即时生效（写 ai_runtime_settings + 失效配置缓存）。
    切换后：主配置优先取该环境专用配置（取不到回落通用），降级链只纳入
    「通用 + 该环境」的候选；场景路由指向其它环境的配置会被自动跳过。
    """
    try:
        env = normalize_env(body.env)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        await upsert_runtime_setting(db, RUNTIME_ACTIVE_ENV_KEY, env)
        await record_config_audit(
            db, "env", detail=f"切换当前生效环境为：{env or '通用（不过滤）'}",
            request=request, commit=False)
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(500, f"环境设置写入失败：{e}")
    # 提交成功后再失效缓存并重放环境感知并发，避免出现「已落库但仍用旧缓存」。
    invalidate_config_cache()
    await apply_config_concurrency()
    return {"ok": True, "active_env": env,
            "hint": ("已切回通用环境：不做环境过滤，行为与未启用多环境时一致"
                     if not env else
                     f"已切换到环境「{env}」：主配置优先取该环境专用配置，"
                     "降级链只使用「通用 + 该环境」的候选")}
