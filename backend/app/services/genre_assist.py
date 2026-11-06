"""文种判断的 LLM 辅助（仅补类别建议，不产出危大结论）。

定位
----
规则引擎（``scheme_classification`` 关键词 + 阈值）是文种判断的主路径。
本模块只在**规则零命中**时，对工程名称不规范 / 材料描述歧义等模糊输入
给出「可能的类别」建议，供人工确认；模型输出：

- 只允许给出 ``category_ids`` / ``sub_ids`` / ``reason``；
- 不得给出危大 / 超规模等级（等级只能由阈值规则引擎产出）；
- 结果以 ``source="llm_suggested"`` 标记，且必须与规则校验记录一起留存。

失败一律 fail-soft 返回 ``None``，由调用方保持 ``undetermined``。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("genre_assist")

_SYSTEM = (
    "你是建筑工程专项施工方案的文种识别助手。"
    "只根据给定方案名称与补充文本，判断它最可能属于哪一类危大工程。"
    "严禁判断危大等级或超规模等级（该结论只能由数值阈值规则给出）。"
    '只输出 JSON：{"category_ids": [...], "sub_ids": [...], "reason": "..."}，'
    "无法判断时三个字段都为空。"
)


def _valid_ids(payload: dict) -> tuple[list[str], list[str]]:
    """校验模型给出的 id 是否真实存在于分类体系（防臆造）。"""
    from app.services.scheme_classification import HAZARD_CATEGORIES

    cat_ids = {c.get("id") for c in HAZARD_CATEGORIES}
    sub_ids = {s.get("id") for c in HAZARD_CATEGORIES for s in c.get("subs", ())}

    cats = [str(x) for x in (payload.get("category_ids") or []) if str(x) in cat_ids]
    subs = [str(x) for x in (payload.get("sub_ids") or []) if str(x) in sub_ids]
    return cats[:3], subs[:5]


async def suggest_category(scheme_name: str, extra_text: str = "", *, chat_fn=None, timeout: int = 60) -> dict | None:
    """规则零命中时请求 LLM 给出类别建议；失败/无有效结果返回 ``None``。

    Args:
        scheme_name: 方案名称。
        extra_text: 补充文本（可为空）。
        chat_fn: 可注入的聊天函数（测试用；缺省走 ``provider_factory``）。
        timeout: 单次调用超时秒数。

    Returns:
        ``{"category_ids": [...], "sub_ids": [...], "reason": str,
           "source": "llm_suggested"}`` 或 ``None``。
    """
    if not (scheme_name or "").strip():
        return None
    if chat_fn is None:
        from app.services.ai.provider_factory import chat_with_fallback as chat_fn

    user = f"方案名称：{scheme_name}\n补充文本：{(extra_text or '')[:800]}"
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]
    try:
        raw = await chat_fn(
            messages,
            temperature=0.1,
            json_mode=True,
            timeout=timeout,
            max_tokens=512,
            scene="genre_tag_assist",
        )
    except Exception as e:  # noqa: BLE001 - 辅助路径 fail-soft，保持 undetermined
        logger.warning("文种标签 LLM 辅助调用失败（保持无法判定）: %s", e)
        return None

    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("文种标签 LLM 辅助返回非 JSON（保持无法判定）: %s", e)
        return None
    if not isinstance(payload, dict):
        return None

    cats, subs = _valid_ids(payload)
    if not cats and not subs:
        return None
    return {
        "category_ids": cats,
        "sub_ids": subs,
        "reason": str(payload.get("reason") or "")[:200],
        "source": "llm_suggested",
    }
