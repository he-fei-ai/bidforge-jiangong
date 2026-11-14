"""图像模型配置路由（GET/PUT/test）。

契约：
- GET  /api/v1/image-config → {config, presets, decision}
- PUT  /api/v1/image-config → {ok, warnings?}
- POST /api/v1/image-config/test → {ok, message}
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.db import get_db, read_db
from app.services.ai.image_engine import resolve_image_config
from app.services.ai.image_providers.registry import ProviderRegistry
from app.services.crypto import encrypt_api_key

logger = logging.getLogger("image_config")
router = APIRouter(prefix="/api/v1/image-config", tags=["image_config"])

VALID_PROVIDERS = {"custom", "dashscope", "volcengine", "hunyuan", "zhipu", "stepfun", "baidu", "agnes"}
VALID_SIZES = {"1K", "2K", "3K", "4K"}
# 5 家展示厂商预设（DALL-E-3 → custom + OpenAI 端点；其余映射适配器默认）
PROVIDER_PRESETS = {
    "dalle3": {"provider": "custom", "base_url": "https://api.openai.com/v1", "model": "dall-e-3"},
    "tongyi_wanxiang": {"provider": "dashscope", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-image-3.0-pro"},
    "doubao_seedream": {"provider": "volcengine", "base_url": "", "model": ""},
    "tencent_hunyuan": {"provider": "hunyuan", "base_url": "", "model": ""},
    "custom": {"provider": "custom", "base_url": "", "model": ""},
}
# 展示厂商键 → provider_id（前端下拉值）
DISPLAY_TO_PROVIDER = {"dalle3": "custom", "tongyi_wanxiang": "dashscope",
                       "doubao_seedream": "volcengine", "tencent_hunyuan": "hunyuan", "custom": "custom"}


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
        (body.provider, body.model, body.base_url, enc, 1 if body.enabled else 0,
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
