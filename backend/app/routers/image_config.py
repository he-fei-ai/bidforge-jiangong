"""图像模型配置路由（GET/PUT/test + 多模型路由族）。

既有契约（零破坏）：
- GET  /api/v1/image-config → {config, presets, decision}
- PUT  /api/v1/image-config → {ok, warnings?}
- POST /api/v1/image-config/test → {ok, message}

新增契约（R252 · /api/v1/image-models）：
- GET    /image-models             → {items, presets, active_id, count, generation}
- POST   /image-models             → 创建（SSRF 校验 + 密钥加密 + 审计）
- PUT    /image-models/{id}        → 更新（热更新：缓存代际号 +1）
- DELETE /image-models/{id}        → 删除
- POST   /image-models/{id}/set-default → 设为默认
- POST   /image-models/test        → 连通性测试（SSRF 前置校验）
- GET/PUT /image-models/scene-routes   → 场景模型路由
- GET    /image-models/audit-logs  → 配置变更审计
"""
import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.db import get_db, read_db
from app.services.ai.image_engine import (
    IMAGE_SCENES,
    resolve_image_config,
    resolve_scene_image_config,  # noqa: F401  # re-export：场景路由消费方由此可见
)
from app.services.ai.image_providers.registry import ProviderRegistry
from app.services.ai.provider_factory import normalize_base_url
from app.services.audit_service import client_ip_of
from app.services.crypto import decrypt_api_key, encrypt_api_key

logger = logging.getLogger("image_config")
router = APIRouter(prefix="/api/v1/image-config", tags=["image_config"])
models_router = APIRouter(prefix="/api/v1/image-models", tags=["image_models"])

VALID_PROVIDERS = {"custom", "dashscope", "volcengine", "hunyuan", "zhipu", "stepfun", "baidu", "agnes"}
VALID_SIZES = {"1K", "2K", "3K", "4K"}
# 5 家展示厂商预设（DALL-E-3 → custom + OpenAI 端点；其余映射适配器默认）
PROVIDER_PRESETS = {
    "dalle3": {"provider": "custom", "base_url": "https://api.openai.com/v1", "model": "dall-e-3"},
    "tongyi_wanxiang": {"provider": "dashscope", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-image-3.0-pro"},
    "doubao_seedream": {"provider": "volcengine", "base_url": "", "model": ""},
    "tencent_hunyuan": {"provider": "hunyuan", "base_url": "", "model": ""},
    # ✅ TB-3（2026-11-16 · 展示面补齐）：4 家已注册适配器此前仅后端可用、
    #    前端下拉未展示（「服务层就绪 ≠ 功能可达」同族）。加法式补展示，
    #    默认端点/模型取自各适配器 default_model / base_url，零破坏。
    "zhipu": {"provider": "zhipu", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "cogview-4"},
    "stepfun": {"provider": "stepfun", "base_url": "https://api.stepfun.com/v1", "model": "step-2x-image"},
    "baidu": {"provider": "baidu", "base_url": "https://aip.baidubce.com/rest/2.0/image-gen/v1", "model": "ernie-vilg"},
    "agnes": {"provider": "agnes", "base_url": "https://api.agnes-ai.cn/v1", "model": "agnes-image-2.1-flash"},
    "custom": {"provider": "custom", "base_url": "", "model": ""},
}
# 展示厂商键 → provider_id（前端下拉值）
DISPLAY_TO_PROVIDER = {"dalle3": "custom", "tongyi_wanxiang": "dashscope",
                       "doubao_seedream": "volcengine", "tencent_hunyuan": "hunyuan",
                       "zhipu": "zhipu", "stepfun": "stepfun", "baidu": "baidu",
                       "agnes": "agnes", "custom": "custom"}


class ImageConfigIn(BaseModel):
    provider: str = Field("custom", max_length=32)
    model: str = Field("", max_length=200)
    base_url: str = Field("", max_length=500)
    api_key: str = Field("", max_length=2000)   # 明文或 ENC:（编辑不重填时传 "" 保持原样）
    enabled: bool = True
    default_size: str = Field("2K", max_length=8)
    concurrency: int = Field(2, ge=1, le=8)
    price_per_image: float = Field(0.0, ge=0.0, le=1000.0)
    max_ai_images: int = Field(0, ge=0, le=1000)


class ImageTestIn(BaseModel):
    provider: str = Field("custom", max_length=32)
    base_url: str = Field("", max_length=500)
    api_key: str = Field("", max_length=2000)


@router.get("")
async def get_config(db=Depends(read_db)):
    cfg = await resolve_image_config(db, settings)
    has_key = bool(cfg.get("image_api_key_enc"))
    cfg.pop("image_api_key_enc", None)  # 用户裁决：剥离敏感字段
    # decision：协同决策流 + Prompt 模板说明（只读展示数据，不改判定逻辑）
    return {
        "config": {**cfg, "has_key": has_key, "api_key": ""},
        "presets": PROVIDER_PRESETS,
        "decision": {
            "routes": [
                {"route": "structured", "desc": "命中图表生成关键词（gantt/architecture/flowchart）→ 结构化图表管线"},
                {"route": "illustration", "desc": "需氛围/示意配图（效果图/示意图/三维/实景/渲染/氛围/鸟瞰/透视/封面）→ AI 文生图"},
                {"route": "none", "desc": "既无图表关键词、也无配图需求 → 纯文字"},
            ],
            "decision_fn": "route_visual_strategy",
            "prompt_template": "[IMG_GEN_PROMPT: 主题=…；风格=…；构图=…；比例=…；负面词=…]",
            "prompt_fields": ["主题", "风格", "构图", "比例", "负面词"],
        },
    }


@router.put("")
async def save_config(body: ImageConfigIn, db=Depends(get_db)):
    if body.provider not in VALID_PROVIDERS:
        raise HTTPException(400, f"provider 不合法：{body.provider}")
    if body.default_size not in VALID_SIZES:
        raise HTTPException(400, f"default_size 不合法：{body.default_size}")
    # ✅ R252（IM-1 · SSRF）：保存路径复用 normalize_base_url（协议/域名/内网拦截）。
    #    空 base_url（预设厂商默认端点）放行；非法/内网地址 → 400。
    try:
        base_url = normalize_base_url(body.base_url)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # 加密（仅当传入明文新 key；为空保持原样）
    enc = ""
    if body.api_key:
        enc = encrypt_api_key(body.api_key) if not body.api_key.startswith("ENC:") else body.api_key
    else:
        cur = await db.execute("SELECT api_key_encrypted FROM image_model_config WHERE id='primary'")
        row = await cur.fetchone()
        if row:
            enc = row[0] if not hasattr(row, "keys") else row["api_key_encrypted"]
    await db.execute(
        "INSERT INTO image_model_config (id, provider, model, base_url, api_key_encrypted, enabled, "
        "default_size, concurrency, price_per_image, max_ai_images, updated_at) "
        "VALUES ('primary', ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime')) "
        "ON CONFLICT(id) DO UPDATE SET provider=excluded.provider, model=excluded.model, "
        "base_url=excluded.base_url, api_key_encrypted=excluded.api_key_encrypted, "
        "enabled=excluded.enabled, default_size=excluded.default_size, concurrency=excluded.concurrency, "
        "price_per_image=excluded.price_per_image, max_ai_images=excluded.max_ai_images, "
        "updated_at=datetime('now','localtime')",
        (body.provider, body.model, base_url, enc, 1 if body.enabled else 0,
         body.default_size, body.concurrency, body.price_per_image, body.max_ai_images),
    )
    await db.commit()
    return {"ok": True, "warnings": []}


@router.post("/test")
async def test_connection(body: ImageTestIn):
    if body.provider not in VALID_PROVIDERS:
        raise HTTPException(400, f"provider 不合法：{body.provider}")
    try:
        adapter = ProviderRegistry.get_adapter(body.provider, {"api_key": body.api_key, "base_url": body.base_url})
        ok = await adapter.test_connection(body.api_key, body.base_url)
    except Exception as e:
        return {"ok": False, "message": f"适配器异常：{e!s}"}
    return {"ok": ok, "message": "连接成功" if ok else "连接失败：请核对 Base URL / API Key / 模型"}


# ============================================================================
# R252 · 多模型多配置 / 场景路由 / 参数模板 / 审计（新增，全部加法式）
# ============================================================================


#: 热更新缓存代际号：每次配置写路径 +1，读侧据此丢弃过期行（TOCTOU 防护，
#: 与 provider_factory.invalidate_config_cache 同族语义）。
_image_generation = 0


def bump_image_generation() -> int:
    """配置变更后递增代际号（热更新：无需重启后端，下次读库即用新配置）。"""
    global _image_generation
    _image_generation += 1
    return _image_generation


#: default_params 白名单键（仅这些可经 **kwargs 注入适配器；其余忽略防签名冲突）。
_DEFAULT_PARAM_KEYS = {
    "resolution", "steps", "cfg", "negative_prompt", "style",
    "quality", "seed", "aspect_ratio", "sampler",
}


def parse_default_params(raw) -> dict:
    """解析参数模板（JSON 或 dict），仅保留白名单键；非法输入回退空模板。"""
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if k in _DEFAULT_PARAM_KEYS}
    if isinstance(raw, str):
        try:
            data = json.loads(raw) if raw.strip() else {}
        except Exception:
            return {}
        return parse_default_params(data) if isinstance(data, dict) else {}
    return {}


def _mask_key(enc: str) -> dict:
    """密钥三态脱敏：key_hint（后4位）/ has_key / key_broken（密文损坏）。"""
    enc = enc or ""
    if not enc:
        return {"key_hint": "", "has_key": False, "key_broken": False}
    key = decrypt_api_key(enc)
    if key:
        return {"key_hint": key[-4:], "has_key": True, "key_broken": False}
    # 密文存在但解不开（换过 FERNET_KEY / 密钥文件被删）→ 显式标记
    return {"key_hint": "", "has_key": False, "key_broken": True}


def _image_config_item(row) -> dict:
    """image_config 行 → 前端列表项（密钥脱敏 + 参数模板解析）。"""
    item = dict(row) if hasattr(row, "keys") else dict(zip(
        ("id", "provider", "model", "base_url", "api_key_encrypted", "enabled",
         "default_size", "concurrency", "price_per_image", "max_ai_images",
         "is_default", "status", "env", "priority", "remark", "default_params",
         "config_version", "created_at", "updated_at"),
        row,
    ))
    enc = item.pop("api_key_encrypted", "") or ""
    item.update(_mask_key(enc))
    item["default_params"] = parse_default_params(item.get("default_params", {}))
    item["enabled"] = bool(item.get("enabled"))
    item["is_default"] = bool(item.get("is_default"))
    return item


class ImageModelIn(BaseModel):
    provider: str = Field("custom", max_length=32)
    model: str = Field("", max_length=200)
    base_url: str = Field("", max_length=500)
    api_key: str = Field("", max_length=2000)   # 明文或 ENC:；空 = 保持原样（更新时）
    enabled: bool = True
    default_size: str = Field("2K", max_length=8)
    concurrency: int = Field(2, ge=1, le=8)
    price_per_image: float = Field(0.0, ge=0.0, le=1000.0)
    max_ai_images: int = Field(0, ge=0, le=1000)
    is_default: bool = False
    status: str = Field("active", max_length=16)
    env: str = Field("", max_length=16)
    priority: int = Field(0, ge=0, le=1000)
    remark: str = Field("", max_length=500)
    default_params: dict = Field(default_factory=dict)


class ImageSceneRouteIn(BaseModel):
    scene: str = Field("", max_length=64)
    config_id: str = Field("", max_length=64)


class ImageSceneRouteBatchIn(BaseModel):
    routes: list[ImageSceneRouteIn] = Field(default_factory=list)


async def _write_image_audit(db, action: str, config_id: str, provider_name: str,
                             model: str, detail: str, request: Request | None = None) -> None:
    """写配置变更审计（尽力而为，失败不影响主流程）。"""
    try:
        await db.execute(
            "INSERT INTO image_config_audit_logs"
            " (id, action, config_id, provider_name, model, detail, client_ip, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), action, config_id, provider_name, model, detail,
             client_ip_of(request), datetime.now().isoformat()),
        )
    except Exception as e:
        logger.debug("图像配置审计写入失败（忽略，不影响主流程）: %s", e)


@models_router.get("")
async def list_image_models(db=Depends(read_db)):
    """列出全部图像模型配置（密钥脱敏：key_hint / key_broken，明文恒不回传）。"""
    cur = await db.execute(
        "SELECT * FROM image_config ORDER BY is_default DESC, priority ASC, updated_at DESC"
    )
    rows = await cur.fetchall()
    items = [_image_config_item(r) for r in rows]
    active_id = next((i["id"] for i in items if i["is_default"]), "")
    return {
        "items": items,
        "presets": PROVIDER_PRESETS,
        "active_id": active_id,
        "count": len(items),
        "generation": _image_generation,
        "scenes": IMAGE_SCENES,
    }


@models_router.post("")
async def create_image_model(body: ImageModelIn, request: Request = None, db=Depends(get_db)):
    """创建图像模型配置（SSRF 校验 + 密钥加密 + 审计 + 热更新）。"""
    if body.provider not in VALID_PROVIDERS:
        raise HTTPException(400, f"provider 不合法：{body.provider}")
    if body.default_size not in VALID_SIZES:
        raise HTTPException(400, f"default_size 不合法：{body.default_size}")
    try:
        base_url = normalize_base_url(body.base_url)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if body.status not in ("active", "disabled"):
        raise HTTPException(400, "status 仅允许 active / disabled")
    config_id = uuid.uuid4().hex[:12]
    enc = encrypt_api_key(body.api_key) if body.api_key else ""
    params_json = json.dumps(parse_default_params(body.default_params), ensure_ascii=False)
    await db.execute(
        "INSERT INTO image_config (id, provider, model, base_url, api_key_encrypted, enabled,"
        " default_size, concurrency, price_per_image, max_ai_images, is_default, status, env,"
        " priority, remark, default_params, config_version, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1, datetime('now','localtime'), datetime('now','localtime'))",
        (config_id, body.provider, body.model, base_url, enc, 1 if body.enabled else 0,
         body.default_size, body.concurrency, body.price_per_image, body.max_ai_images,
         1 if body.is_default else 0, body.status, body.env, body.priority,
         body.remark, params_json),
    )
    if body.is_default:
        await db.execute("UPDATE image_config SET is_default = 0 WHERE is_default = 1 AND id <> ?", (config_id,))
    await _write_image_audit(db, "create", config_id, body.provider, body.model,
                             f"新增配置（env={body.env or '通用'}）", request)
    await db.commit()
    return {"ok": True, "id": config_id, "generation": bump_image_generation()}


@models_router.put("/{config_id}")
async def update_image_model(config_id: str, body: ImageModelIn, request: Request = None, db=Depends(get_db)):
    """更新图像模型配置（热更新：代际号 +1，无需重启后端）。"""
    if body.provider not in VALID_PROVIDERS:
        raise HTTPException(400, f"provider 不合法：{body.provider}")
    if body.default_size not in VALID_SIZES:
        raise HTTPException(400, f"default_size 不合法：{body.default_size}")
    try:
        base_url = normalize_base_url(body.base_url)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if body.status not in ("active", "disabled"):
        raise HTTPException(400, "status 仅允许 active / disabled")
    # 密钥：传明文 → 加密；传空 → 保持原样（查旧密文）；传 ENC: → 原样保留
    enc = ""
    if body.api_key:
        enc = encrypt_api_key(body.api_key) if not body.api_key.startswith("ENC:") else body.api_key
    else:
        cur = await db.execute("SELECT api_key_encrypted FROM image_config WHERE id = ?", (config_id,))
        row = await cur.fetchone()
        if row is not None:
            enc = row["api_key_encrypted"] if hasattr(row, "keys") else row[0]
    params_json = json.dumps(parse_default_params(body.default_params), ensure_ascii=False)
    await db.execute(
        "UPDATE image_config SET provider=?, model=?, base_url=?, api_key_encrypted=?, enabled=?,"
        " default_size=?, concurrency=?, price_per_image=?, max_ai_images=?, is_default=?, status=?,"
        " env=?, priority=?, remark=?, default_params=?, config_version=config_version+1,"
        " updated_at=datetime('now','localtime') WHERE id=?",
        (body.provider, body.model, base_url, enc, 1 if body.enabled else 0,
         body.default_size, body.concurrency, body.price_per_image, body.max_ai_images,
         1 if body.is_default else 0, body.status, body.env, body.priority,
         body.remark, params_json, config_id),
    )
    if body.is_default:
        await db.execute("UPDATE image_config SET is_default = 0 WHERE is_default = 1 AND id <> ?", (config_id,))
    await _write_image_audit(db, "update", config_id, body.provider, body.model,
                             f"更新配置（env={body.env or '通用'}，版本 +1）", request)
    await db.commit()
    return {"ok": True, "id": config_id, "generation": bump_image_generation()}


@models_router.delete("/{config_id}")
async def delete_image_model(config_id: str, request: Request = None, db=Depends(get_db)):
    """删除图像模型配置（同时清理其场景路由引用）。"""
    await db.execute("DELETE FROM image_scene_routes WHERE config_id = ?", (config_id,))
    await db.execute("DELETE FROM image_config WHERE id = ?", (config_id,))
    await _write_image_audit(db, "delete", config_id, "", "", "删除配置", request)
    await db.commit()
    return {"ok": True, "id": config_id, "generation": bump_image_generation()}


@models_router.post("/{config_id}/set-default")
async def set_default_image_model(config_id: str, request: Request = None, db=Depends(get_db)):
    """设为默认主配置（其余 is_default 清零，原子性由单连接保证）。"""
    await db.execute("UPDATE image_config SET is_default = 0 WHERE is_default = 1 AND id <> ?", (config_id,))
    await db.execute(
        "UPDATE image_config SET is_default = 1, updated_at=datetime('now','localtime') WHERE id = ?",
        (config_id,),
    )
    await _write_image_audit(db, "set_default", config_id, "", "", "设为默认主配置", request)
    await db.commit()
    return {"ok": True, "default_id": config_id, "generation": bump_image_generation()}


@models_router.post("/test")
async def test_image_model_connection(body: ImageTestIn):
    """连通性测试（SSRF 前置校验：内网 Base URL 直接拒绝，不发请求）。"""
    if body.provider not in VALID_PROVIDERS:
        raise HTTPException(400, f"provider 不合法：{body.provider}")
    try:
        normalize_base_url(body.base_url)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    try:
        adapter = ProviderRegistry.get_adapter(body.provider, {"api_key": body.api_key, "base_url": body.base_url})
        ok = await adapter.test_connection(body.api_key, body.base_url)
    except Exception as e:
        return {"ok": False, "message": f"适配器异常：{e!s}"}
    return {"ok": ok, "message": "连接成功" if ok else "连接失败：请核对 Base URL / API Key / 模型"}


@models_router.get("/scene-routes")
async def list_scene_routes(db=Depends(read_db)):
    """场景模型路由列表（含未配置的已知场景）。"""
    cur = await db.execute("SELECT scene, config_id FROM image_scene_routes")
    rows = await cur.fetchall()
    by_scene = {r["scene"] if hasattr(r, "keys") else r[0]: (r["config_id"] if hasattr(r, "keys") else r[1])
                for r in rows}
    items = [
        {"scene": scene, "label": label, "config_id": by_scene.get(scene, ""),
         "missing": False}
        for scene, label in IMAGE_SCENES.items()
    ]
    for scene, cid in by_scene.items():
        if scene not in IMAGE_SCENES:
            items.append({"scene": scene, "label": scene, "config_id": cid, "missing": False})
    return {
        "items": items,
        "known_scenes": [{"value": k, "label": v} for k, v in IMAGE_SCENES.items()],
        "configured_count": sum(1 for i in items if i["config_id"]),
    }


@models_router.put("/scene-routes")
async def save_scene_routes(body: ImageSceneRouteBatchIn, request: Request = None, db=Depends(get_db)):
    """批量保存场景路由（config_id 空串 = 清除该场景路由）。"""
    for r in body.routes:
        if r.scene not in IMAGE_SCENES:
            raise HTTPException(400, f"scene 不合法：{r.scene}")
    for r in body.routes:
        if not r.config_id:
            await db.execute("DELETE FROM image_scene_routes WHERE scene = ?", (r.scene,))
        else:
            await db.execute(
                "INSERT INTO image_scene_routes (scene, config_id, updated_at)"
                " VALUES (?,?,datetime('now','localtime'))"
                " ON CONFLICT(scene) DO UPDATE SET config_id=excluded.config_id,"
                " updated_at=datetime('now','localtime')",
                (r.scene, r.config_id),
            )
    await _write_image_audit(db, "scene_routes", "", "", "", "更新场景模型路由", request)
    await db.commit()
    return {"ok": True, "generation": bump_image_generation()}


@models_router.get("/audit-logs")
async def image_audit_logs(limit: int = 50, offset: int = 0, db=Depends(read_db)):
    """配置变更审计列表（最新在前，含操作人 IP / 时间）。"""
    limit = max(1, min(200, int(limit or 50)))
    offset = max(0, int(offset or 0))
    cur = await db.execute(
        "SELECT * FROM image_config_audit_logs ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = [dict(r) if hasattr(r, "keys") else dict(zip(
        ("id", "action", "config_id", "provider_name", "model", "detail",
         "client_ip", "snapshot_json", "created_at"), r))
        for r in await cur.fetchall()]
    return {"items": rows, "count": len(rows)}
