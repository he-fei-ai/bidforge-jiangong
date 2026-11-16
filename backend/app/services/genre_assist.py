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

from app.services.prompt_material_guard import clip_text_notice

logger = logging.getLogger("genre_assist")

# ✅ 提示词注册表收口（2026-11-16）：文种识别 system 此前是模块级字面量 `_SYSTEM`
#    直接拼进 messages，绕过统一注册表（不能后台编辑 / 不能 DB 定制 / 不进审计与指标）。
#    现按 R39 · T1 同构模式注册（出厂默认 = `_SYSTEM`，逐字节不变），消费点改
#    「DB 优先 → 出厂字面量回退」。`_reg` 定义先于本模块依赖完成加载，顶层 import 无环
#    （本模块顶层仅依赖 prompt_material_guard，不反向 import prompts 包）。
from app.services.ai.prompts._registry import _reg

_SYSTEM = (
    "你是建筑工程专项施工方案的文种识别助手。"
    "只根据给定方案名称与补充文本，判断它最可能属于哪一类危大工程。"
    "严禁判断危大等级或超规模等级（该结论只能由数值阈值规则给出）。"
    '只输出 JSON：{"category_ids": [...], "sub_ids": [...], "reason": "..."}，'
    "无法判断时三个字段都为空。"
)


#: 文种识别 system 是否已完成注册（幂等锁）。
_GENRE_PROMPT_REGISTERED = False


def _reg_genre_prompt() -> None:
    """把文种识别 system 登记进统一注册表（出厂默认 = `_SYSTEM`）。"""
    global _GENRE_PROMPT_REGISTERED
    if _GENRE_PROMPT_REGISTERED:
        return
    # 无 ``{var}`` 调用方变量，声明空白名单让契约校验监督「编辑误引入占位符」。
    _reg("genre_assist_system", "analysis", "文种识别 · LLM 辅助 system", _SYSTEM, requires=[])
    _GENRE_PROMPT_REGISTERED = True


_reg_genre_prompt()


def _system_prompt() -> str:
    """文种识别 system（DB 定制优先 → 出厂字面量回退，任何异常 fail-soft）。

    ``_cache.get_prompt`` 走函数体内惰性 import：本模块可能被惰性注册钩子
    半初始化时反向 import，此时局部 import 可避免循环导入。
    """
    try:
        from app.services.ai.prompts._cache import get_prompt

        text = get_prompt("genre_assist_system")
    except Exception:  # noqa: BLE001 - 辅助路径 fail-soft，回退出厂默认
        return _SYSTEM
    return text if isinstance(text, str) and text.strip() else _SYSTEM


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

    # ✅ R181（2026-11-07）：进模型提示词的补充文本超长时留痕（模型可见 + 日志）。
    user = f"方案名称：{scheme_name}\n补充文本：{clip_text_notice(extra_text or '', 800, '文种识别补充文本')}"
    messages = [
        {"role": "system", "content": _system_prompt()},
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
