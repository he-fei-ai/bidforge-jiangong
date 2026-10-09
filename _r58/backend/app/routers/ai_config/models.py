"""AI 配置路由 · 模型管理（拉取厂商模型列表 / 预设 / 自定义供应商）。"""
from fastapi import APIRouter, Depends

from app.db import read_db
from app.models import ProviderModelsIn
from app.services.ai.provider_factory import PROVIDER_PRESETS, normalize_base_url
from app.services.crypto import decrypt_api_key

from ._common import _classify_error, _dns_precheck_async, _same_base_url

router = APIRouter(tags=["ai_config"])


#: 平台返回模型列表的截断上限（聚合类平台可返回上千条，全量回传会拖垮弹窗下拉）
_MODEL_LIST_MAX = 400


async def _resolve_conn_credentials(db, data: ProviderModelsIn) -> tuple[str, str, str]:
    """解析模型列表所需的 base_url / api_key（config_id 优先，其次表单临时值）。

    返回 ``(base_url, api_key, error)``；``error`` 非空表示凭据无法解析。

    ✅ 2026-09-23 修复：原实现在 ``config_id`` 查不到行时直接返回 ``("", "")``，
       调用方随后只会报「Base URL 为空，请先填写 API 地址」——
       把「配置已被删除」误归因成「你没填地址」，用户按提示补填地址也永远拉不到模型。
       现显式回传错误原因（错误归因原则：不把 A 的失败说成 B 的原因）。
    """
    base_url = (data.base_url or "").strip()
    api_key = (data.api_key or "").strip()
    config_id = (data.config_id or "").strip()
    if config_id:
        cur = await db.execute(
            "SELECT base_url, api_key_encrypted FROM ai_config WHERE id=?", (config_id,))
        row = await cur.fetchone()
        if not row:
            return "", "", "配置不存在（可能已被删除）"
        saved_base_url = (row["base_url"] or "").strip()
        if base_url and not _same_base_url(base_url, saved_base_url) and not api_key:
            return "", "", (
                "Base URL 已变更，请重新输入该地址对应的 API Key；"
                "为防止密钥外泄，系统不会复用原配置中保存的 Key"
            )
        if not base_url:
            base_url = saved_base_url
        if not api_key and row["api_key_encrypted"]:
            api_key = decrypt_api_key(row["api_key_encrypted"]).strip()
    return base_url, api_key, ""


def _ctx_tokens(ctx) -> int:
    """把厂商返回的上下文长度归一化成 token 数（取不到返回 0，仅用于排序）。"""
    try:
        n = int(ctx)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _fmt_context(ctx) -> str:
    """把上下文长度格式化成可读串。

    ✅ 修复（本轮审查）：原实现是 `f"{ctx}K" if ctx > 1000 else ctx`，
      `context_length=128000` 会被显示成 **"128000K"**（比真实值放大 1000 倍）。
      用户正是靠这一列判断「这个模型能装多少内容」，误判会直接影响选型
      （128K 的模型看起来比 10M 的 qwen-long 还大）。现按 token 归一化。
    """
    n = _ctx_tokens(ctx)
    if n <= 0:
        return str(ctx or "")
    if n >= 1000:
        k = round(n / 1000, 1)
        return f"{int(k)}K" if float(k).is_integer() else f"{k}K"
    return str(n)


async def _fetch_models_list(base_url: str, api_key: str, timeout: int = 15) -> dict:
    """统一实现：请求 {base_url}/models 并归一化模型列表。

    ✅ 修复（本轮审查）：原 /config/fetch-models 与 /custom-models 是两份
      几乎相同的实现（排序、字段映射、错误处理各不相同），同一功能两套行为。
      现合并为单一实现，两个端点共用。
    """
    import httpx

    try:
        base_url = normalize_base_url(base_url)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not api_key:
        return {"ok": False, "error": "API Key 不能为空",
                "suggestion": "请先填写 API Key（或保存后由系统从已存配置读取）"}

    # 先做 DNS/TCP 预检，命不中就不浪费一次带 Key 的请求
    pre = await _dns_precheck_async(base_url)
    if not pre["ok"]:
        return {"ok": False, "error": pre["message"], "detail": pre,
                "suggestion": _classify_error(
                    Exception(pre.get("raw") or pre["message"])).get("suggestion", "")}

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url}/models", headers=headers)
    except Exception as e:
        classified = _classify_error(e)
        return {"ok": False, "error": classified["message"],
                "suggestion": classified["suggestion"]}

    if resp.status_code >= 400:
        classified = _classify_error(Exception(f"HTTP {resp.status_code}: {resp.text[:300]}"))
        return {"ok": False, "error": classified["message"],
                "suggestion": classified["suggestion"], "raw": resp.text[:300]}

    try:
        payload = resp.json()
    except Exception:
        return {"ok": False, "error": "厂商返回了非 JSON 响应",
                "suggestion": "该地址可能不是 OpenAI 兼容接口，请检查 Base URL 是否以 /v1 结尾"}

    raw = payload.get("data") or payload.get("models") or []
    if isinstance(raw, dict):
        raw = list(raw.values())
    models: list[dict] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("name") or m.get("model") or ""
        if not mid:
            continue
        owned = m.get("owned_by", "")
        ctx = m.get("context_window") or m.get("context_length") or m.get("max_context") or ""
        models.append({
            "value": mid,
            "label": f"{mid}  ({owned})" if owned else mid,
            "owned_by": owned,
            "context": _fmt_context(ctx),
            "context_tokens": _ctx_tokens(ctx),
        })

    if not models:
        return {"ok": False, "error": "供应商返回了空的模型列表",
                "suggestion": "该平台可能未开放 /models 接口，请手动填写模型名称"}

    # 排序：上下文越大越靠前（能力更强通常更靠前），同尺寸按字母序；
    # 无上下文信息的排在末尾。
    # ✅ 修复（本轮审查）：注释原本就写着这句，代码却只做了 `sort(key=value)`。
    models.sort(key=lambda x: (-(x.get("context_tokens") or 0), x["value"]))
    # ✅ 增强：部分聚合平台（OpenRouter / 魔搭镜像等）会返回上千个模型，
    #    全量回传既拖慢弹窗渲染、也让下拉框不可用。截断到前 N 个并显式告知。
    truncated = len(models) > _MODEL_LIST_MAX
    if truncated:
        models = models[:_MODEL_LIST_MAX]
    return {"ok": True, "count": len(models), "models": models,
            "base_url": base_url, "truncated": truncated}


@router.post("/config/fetch-models")
async def fetch_provider_models(data: ProviderModelsIn, db=Depends(read_db)):
    """
    动态拉取厂商可用模型列表（调用 {base_url}/models）。
    支持两种方式：
      1) 传 config_id — 从数据库读取 base_url + api_key
      2) 直接传 base_url + api_key — 表单里还没保存的临时值
    """
    base_url, api_key, err = await _resolve_conn_credentials(db, data)
    if err:
        return {"ok": False, "error": err, "suggestion": "请刷新页面后重新选择配置"}
    if not base_url:
        return {"ok": False, "error": "Base URL 为空，请先填写 API 地址"}
    return await _fetch_models_list(base_url, api_key)


@router.get("/models")
async def list_models():
    """返回每个供应商的可用模型列表（含计费方式 plans）"""
    result = {}
    for key, preset in PROVIDER_PRESETS.items():
        result[key] = {
            "label": preset.get("label", key),
            "models": preset.get("models", []),
            "plans": preset.get("plans", {}),
            "description": preset.get("description", ""),
            "website": preset.get("website", ""),
            "pricing": preset.get("pricing", ""),
            "default_model": preset.get("model", ""),
            "base_url": preset.get("base_url", ""),
            "supports_vision": bool(preset.get("supports_vision")),
        }
    return {"providers": result}


@router.post("/custom-models")
async def fetch_custom_models(data: ProviderModelsIn, db=Depends(read_db)):
    """自定义供应商：调用 {base_url}/models 获取可用模型列表。

    ✅ 修复：与 /config/fetch-models 合并为同一实现（此前两份逻辑行为不一致：
      这里用 raise_for_status 异常分类，那里用状态码分支；字段映射也不同）。
    """
    base_url, api_key, err = await _resolve_conn_credentials(db, data)
    if err:
        return {"ok": False, "error": err, "suggestion": "请刷新页面后重新选择配置"}
    if not base_url:
        return {"ok": False, "error": "请填写 Base URL"}
    return await _fetch_models_list(base_url, api_key, timeout=30)
