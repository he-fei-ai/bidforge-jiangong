"""AI 配置路由 · 场景模型路由（✅ 2026-09-23 新增；2026-09-25 增强）。

补齐「多模型 / 模型路由」功能缺口：
  引入前所有业务场景（目录、正文、事实提取、一致性扫描…）共用**同一条**
  「当前使用」配置，``scene`` 参数只写审计、不参与选模型 ——
  无法做到「正文用长文模型、事实提取用快模型」。

语义与兼容性：
  - 表 ``ai_scene_routes`` 为空（默认）时，``resolve_scene_config()`` 恒返回
    None，运行时回落主配置 —— **默认行为与引入该功能前完全一致**；
  - ``scene`` 只允许已知场景白名单（``KNOWN_SCENES``），避免拼错后静默失效；
  - ``config_id`` 传空串 = 清除该场景的路由（恢复共用主配置）。

✅ 2026-09-25 增强（「配了不生效」静默失效修复）：
  1. ``GET /scene-routes`` 返回 ``active_env``，每项新增 ``config_env`` /
     ``env_mismatch`` —— 路由指向**其它环境**的配置时，运行时
     ``resolve_scene_config`` 会静默跳过（只写后端日志），界面此前完全
     看不出来；现如实标注，与「配置已删除（missing）」同等可见。
  2. ``PUT /scene-routes`` 返回 ``warning``：跨环境路由 / 目标配置无可用
     Key 时明确告知「暂不生效，运行时自动回落」，不再只回 ok:true。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from app.db import get_db, read_db
from app.models import SceneRouteUpdate
from app.services.ai.provider_factory import (
    KNOWN_SCENES,
    invalidate_config_cache,
    resolve_active_env,
)
from app.services.crypto import decrypt_api_key

from .audit import record_config_audit

router = APIRouter(tags=["ai_config"])


async def _safe_active_env() -> str:
    """展示用途的当前环境读取：损坏时返回空串而不抛错。

    ``resolve_active_env`` 对损坏值 fail-closed 抛错是**运行时选模**的硬约束
    （绝不把损坏值当通用环境去用密钥/地址）；但列表/设置类展示端点若跟着
    500，用户连「把环境重置为通用」的入口都看不到（错误配置不可恢复）。
    ``env_error`` 由 ``GET /ai/config``、``GET /ai/env``、``GET /ai/health``
    负责如实暴露，这里只保证展示端点自身可用。
    """
    try:
        return await resolve_active_env()
    except ValueError:
        return ""


def _env_mismatch(active_env: str, config_env: str) -> bool:
    """路由目标配置在当前环境下是否会被运行时跳过。

    与 ``resolve_scene_config`` 的判定严格同口径：
    ``active_env 非空 且 config_env 非空 且两者不同`` → 跳过（回落主配置）。
    """
    return bool(active_env and config_env and config_env != active_env)


def _scene_items(rows: list[dict], known: dict[str, str],
                 active_env: str = "") -> list[dict]:
    """把 DB 行与场景白名单合并成前端可用列表（含未配置的场景）。"""
    by_scene = {r.get("scene"): r for r in rows}
    items: list[dict] = []

    def _one(scene: str, label: str, r: dict) -> dict:
        config_env = str(r.get("config_env") or "")
        missing = bool(r.get("config_id")) and not r.get("exists_ok")
        return {
            "scene": scene,
            "label": label,
            "config_id": r.get("config_id", "") or "",
            "provider_name": r.get("provider_name", "") or "",
            "model": r.get("model", "") or "",
            # 路由目标不生效的两种形态：配置已删除 → missing（原有）；
            # 跨环境 → env_mismatch（✅ 2026-09-25 新增，运行时同样回落主配置）
            "missing": missing,
            "config_env": config_env if r.get("config_id") else "",
            "env_mismatch": (not missing) and _env_mismatch(active_env, config_env),
        }

    for scene, label in known.items():
        items.append(_one(scene, label, by_scene.get(scene, {})))
    # 兜底：DB 里存在但不在白名单的场景（历史/手工写入）也要展示，否则用户看不到它
    for scene, r in by_scene.items():
        if scene and scene not in known:
            items.append(_one(scene, scene, r))
    return items


@router.get("/scene-routes")
async def list_scene_routes(db=Depends(read_db)):
    """场景模型路由列表（含未配置的已知场景）。"""
    cur = await db.execute(
        "SELECT r.scene AS scene, r.config_id AS config_id,"
        " c.provider_name AS provider_name, c.model AS model,"
        " c.env AS config_env,"
        " CASE WHEN c.id IS NULL THEN 0 ELSE 1 END AS exists_ok"
        " FROM ai_scene_routes r LEFT JOIN ai_config c ON c.id = r.config_id")
    rows = [dict(r) for r in await cur.fetchall()]
    active_env = await _safe_active_env()
    items = _scene_items(rows, KNOWN_SCENES, active_env)
    return {
        "items": items,
        "known_scenes": [{"value": k, "label": v} for k, v in KNOWN_SCENES.items()],
        "configured_count": sum(1 for i in items if i["config_id"]),
        # 当前生效环境（空串 = 通用）：前端据此解释 env_mismatch 的成因
        "active_env": active_env,
    }


@router.put("/scene-routes")
async def update_scene_route(body: SceneRouteUpdate, request: Request = None,
                             db=Depends(get_db)):
    """设置 / 清除某个场景的专属配置。

    - ``scene`` 必须在 ``KNOWN_SCENES`` 白名单内（否则 400，避免拼错静默失效）；
    - ``config_id`` 为空串 → 清除该场景路由（恢复共用主配置）；
    - ``config_id`` 非空 → 必须是已存在的配置 id（否则 400）；
    - ✅ 2026-09-25：返回 ``warning`` —— 跨环境 / 目标配置无可用 Key 时明确
      告知「该路由暂不生效，运行时自动回落」。路由**仍会保存**（用户可能
      先配好路由、稍后再切环境），与 ``resolve_scene_config`` 运行时语义一致。
    """
    scene = (body.scene or "").strip()
    if not scene:
        raise HTTPException(400, "场景不能为空")
    if scene not in KNOWN_SCENES:
        raise HTTPException(
            400, f"未知场景：{scene}（可选：{', '.join(KNOWN_SCENES.keys())}）")

    config_id = (body.config_id or "").strip()
    provider_name = model = ""
    warnings: list[str] = []
    if config_id:
        cur = await db.execute(
            "SELECT provider_name, model, env, api_key_encrypted"
            " FROM ai_config WHERE id=?", (config_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(400, "指定的配置不存在（可能已被删除），请刷新后重试")
        provider_name = row["provider_name"] or ""
        model = row["model"] or ""
        # ✅ 2026-09-25：设置时即告知「配了也不生效」的两种情形，不再静默
        cfg_env = str(row["env"] or "").strip()
        active_env = await _safe_active_env()
        if _env_mismatch(active_env, cfg_env):
            warnings.append(
                f"该配置属于环境「{cfg_env}」，当前生效环境为「{active_env}」——"
                "该路由暂不生效（运行时自动回落主配置），切换环境后即可生效")
        if not decrypt_api_key(row["api_key_encrypted"] or ""):
            warnings.append(
                "该配置没有可用的 API Key（未填写或无法解密）——"
                "该场景运行时会跳过它并回落主配置/降级链")
        await db.execute(
            "INSERT INTO ai_scene_routes (scene, config_id, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(scene) DO UPDATE SET config_id=excluded.config_id,"
            " updated_at=excluded.updated_at",
            (scene, config_id, datetime.now().isoformat()))
        detail = f"场景 {scene} → {provider_name}/{model}"
    else:
        await db.execute("DELETE FROM ai_scene_routes WHERE scene=?", (scene,))
        detail = f"场景 {scene} → 恢复共用主配置"
    if warnings:
        detail += f"；⚠ {'；'.join(warnings)}"

    await record_config_audit(
        db, "scene_route", config_id=config_id, provider_name=provider_name,
        model=model, detail=detail, request=request, commit=False)
    await db.commit()
    invalidate_config_cache()
    return {"ok": True, "scene": scene, "config_id": config_id,
            "warning": "；".join(warnings)}
