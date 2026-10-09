"""全局事实 · 知识库补充 + 最终整理（对齐参考软件易标 globalFactsTask.cjs）

引入背景
--------
上一轮（第十二轮）已把易标的**纯逻辑**（补丁 / 分批 / 上下文预算）引入
``facts_patches``，但两个 AI 阶段未接线。本模块补上接线，且**默认关闭**
（``settings.facts_knowledge_patch_enabled`` / ``settings.facts_finalize_enabled``）：

1. **知识库补充**（易标 :876-891 ``runKnowledgeGlobalFactPatches``）
   参考软件把「项目知识库条目」当作**第二事实来源**：AI 只读知识库并产出
   **补丁**，补丁按 ``append/prepend/replace`` 合并回既有事实，**不重新生成
   全部事实**。
   ⚠️ 更正上一轮的一处**事实性错误记录**：上一轮 AGENTS.md §4.16.5 写「本仓
   无对应数据源」——这是错的。本仓 ``knowledge_base`` 表早已存在（产品需求
   §3.9），``sse_handlers._load_knowledge_rows`` / ``_build_knowledge_text``
   也早已被**目录生成**与**正文生成**消费；只是事实链路从不读它。现已接线。

2. **最终整理**（易标 :909-920 ``finalizeGlobalFacts``）
   一次 AI 调用做「同义项合并 + 要求句改写为事实句 + 强制保留工期类变量」。
   本仓既有 ``merge_and_deduplicate`` 是**纯程序**归一化（按归一化 key 聚类），
   缺这一步语义改写。

设计要点
--------
- **默认关闭**：两个开关默认 ``False``，关闭时 ``run_extraction_pipeline``
  的行为与引入前逐字节一致（零新增 AI 调用）。
- **失败降级而非中断**：参考软件这两个阶段失败会让整轮失败；本仓按
  ``fail-soft`` 处理——记 ``warnings`` 后用未加工的结果继续，绝不因补充
  阶段失败而让已经成功提取的事实整批丢失（与「分段提取失败不拖垮整轮」
  的既有策略一致）。
- **保不变式**：所有回写都走 ``facts_patches.apply_patches_to_fact_items``
  （只改 ``value``，不重建 ``FactItem``），溯源/置信度/模拟值闸门全部保留。
- **不被 AI 放大规模**：补充阶段只产补丁；整理阶段对条数做上限保护
  （``_MAX_FINALIZE_FACT``），AI 若误当成「重新提取」返回大量新事实，
  超限部分**丢弃**而非全量采纳。
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any, Sequence

from app.services.ai.json_response import collect_json_response
from app.services.ai.prompts._registry import render
from app.services.facts_patches import (
    FactPatch,
    apply_patches_to_fact_items,
    normalize_patches_response,
    value_to_markdown,
)

logger = logging.getLogger("facts_enrich")

#: 整理阶段允许的最大事实条数。输入事实本身远小于此；AI 若误当成「重新提取」
#: 返回成百上千条时，超限部分直接丢弃（避免事实库被一次性污染）。
_MAX_FINALIZE_FACT = 400

#: 送给 AI 的事实条数上限（超长库只送前 N 条，防止 prompt 超长）。
_MAX_PROMPT_FACTS = 300

#: 单条事实送给 AI 的 value 截断长度。
_VALUE_CLIP = 300


def _clip(value: Any, limit: int = _VALUE_CLIP) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def items_to_prompt_json(items: Sequence) -> list[dict]:
    """把 ``FactItem`` 序列转成送 AI 的精简 JSON 列表。

    只带 ``fact_key`` / ``name`` / ``value`` 三项：``fact_key`` 是 AI 回传
    ``target_fact_id`` 的稳定锚点；带上溯源/置信度/模拟值等运营字段只会诱导
    模型去改写它们（与本仓 ``/global-facts/adjust`` 的同一取舍）。
    """
    out: list[dict] = []
    for it in (items or [])[:_MAX_PROMPT_FACTS]:
        out.append({
            "fact_key": str(getattr(it, "key", "") or getattr(it, "fact_key", "") or ""),
            "name": _clip(getattr(it, "name", ""), 40),
            "value": _clip(getattr(it, "value", "")),
        })
    return out


def _timeout() -> int:
    """新增 AI 调用的超时（秒），读配置失败时回落到 240。"""
    try:
        from app.config import settings
        return int(getattr(settings, "facts_enrich_timeout", 240) or 240)
    except Exception:  # pragma: no cover
        return 240


# =========================================================================
# 一、知识库补充（对齐易标 :876-891）
# =========================================================================
def _validate_patches(obj: Any) -> list[str]:
    """校验知识库补充结果；空 patches 视为合法（无内容可补是正常结果）。"""
    if not isinstance(obj, dict):
        return ["顶层必须是 JSON 对象"]
    if obj.get("patches") is None:
        return ["缺少 patches 字段"]
    if not isinstance(obj.get("patches"), list):
        return ["patches 必须是数组"]
    return []


async def apply_knowledge_patches(items: list, knowledge_text: str) -> tuple[list, int]:
    """用知识库条目产出的补丁补充事实（对齐易标 ``runKnowledgeGlobalFactPatches``）。

    Args:
        items: 已合并去重的 ``FactItem`` 序列（可能被追加新项）。
        knowledge_text: ``build_knowledge_text`` 产出的知识库文本块；为空则跳过。

    Returns:
        ``(补充后的 items, 实际应用的补丁条数)``；失败时返回 ``(原 items, 0)``。
    """
    text = str(knowledge_text or "").strip()
    if not text or not items:
        return items, 0

    try:
        system = render("facts_knowledge_patch_system",
                        current_facts=json.dumps(
                            items_to_prompt_json(items), ensure_ascii=False),
                        knowledge_text=text)
        obj, _ = await collect_json_response(
            [{"role": "system", "content": system}],
            _validate_patches, max_retries=1, temperature=0.1,
            json_mode=True, timeout=_timeout(), scene="facts_knowledge_patch")
    except Exception as e:  # noqa: BLE001 - fail-soft：补充失败不拖垮提取
        logger.warning("知识库事实补充阶段失败（已跳过，不影响本次提取结果）: %s",
                       e, exc_info=True)
        return items, 0

    patches = _build_patches(obj, items)
    if not patches:
        logger.info("知识库未返回需要补充的事实")
        return items, 0

    try:
        merged = apply_patches_to_fact_items(items, patches)
    except Exception as e:  # noqa: BLE001 - 合并失败不得静默写入半成品
        logger.warning("知识库补丁合并失败（已跳过）: %s", e, exc_info=True)
        return items, 0

    logger.info("知识库事实补充已应用 %d 条补丁，事实数 %d → %d",
                len(patches), len(items), len(merged))
    return merged, len(patches)


def _build_patches(obj: Any, items: Sequence) -> list[FactPatch]:
    """把 AI 返回的 patches 归一化为 :class:`FactPatch`。

    ✅ 2026-10-06 收敛为**单一形状出口**：直接复用
    ``facts_patches.normalize_patches_response``（它持有 content 别名表、
    mode 白名单 PATCH_MODES、new_fact_id 归一 ``normalize_fact_id`` 与
    ``patch_{n}`` 前缀约定）。本函数此前**自带一份形状实现**：
    content 只认 3 个别名、mode 内联 ``("append","prepend","replace")``
    字面量、新事实 id 用第三套前缀 ``kb_{n}`` —— 正是本模块 docstring
    「避免第二套实现」明令禁止的形态（新增第 4 种 mode / 别名时
    知识库补充阶段会静默跟不上）。

    本函数只保留 normalize_patches_response **不做**的那一步：
    **幻觉锚点降级**（target 既不在 fact_key 也不在事实名中 → 视为新增）。
    挂到不存在的锚点上会让补丁被静默丢弃、用户却以为已补充。
    """
    patches = list(normalize_patches_response(obj).get("patches") or [])
    if not patches:
        return []
    valid_keys = {str(getattr(it, "key", "") or getattr(it, "fact_key", "") or "")
                  for it in items}
    valid_names = {str(getattr(it, "name", "") or "") for it in items}
    out: list[FactPatch] = []
    for p in patches:
        is_new = not (p.target_fact_id in valid_keys
                      or (p.title and p.title in valid_names))
        if is_new and (p.target_fact_id or p.title):
            # 幻觉锚点降级：清空 target 并标记 create。
            # ⚠️ 必须**清空** target_fact_id（而不只是置 create=True）——
            #    下游 apply_patches_to_fact_items 正是按 target 定位待补事实，
            #    保留幽灵锚点会让补丁挂到「恰好同名的另一条」上，
            #    或在无匹配时静默丢弃（用户以为已补充）。
            p = replace(p, target_fact_id="", create=True)
        elif is_new:
            p = replace(p, create=True)
        out.append(p)
    return out



# =========================================================================
# 二、最终整理（对齐易标 :652-675 / :909-920）
# =========================================================================
def _validate_finalize(obj: Any) -> list[str]:
    if not isinstance(obj, dict):
        return ["顶层必须是 JSON 对象"]
    if not isinstance(obj.get("facts"), list):
        return ["缺少 facts 字段"]
    return []


def apply_finalize_result(items: list, obj: Any) -> tuple[list, int]:
    """把 AI 整理结果回写到 ``FactItem`` 列表（纯函数，便于单测穷举）。

    只改 ``value``，**不重建对象**、不新增/删除条目——保住 ``source`` /
    ``confidence`` / ``is_simulated`` / ``chapter`` / ``is_shared`` 与溯源。
    条数上限保护见 ``_MAX_FINALIZE_FACT``。

    Returns:
        ``(items, 实际改写条数)``。
    """
    if not isinstance(obj, dict) or not isinstance(obj.get("facts"), list):
        return items, 0
    by_key: dict[str, Any] = {}
    by_name: dict[str, Any] = {}
    for it in items or []:
        key = str(getattr(it, "key", "") or getattr(it, "fact_key", "") or "")
        if key:
            by_key[key] = it
        by_name[str(getattr(it, "name", "") or "")] = it

    changed = 0
    for raw in obj["facts"][:_MAX_FINALIZE_FACT]:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("fact_key") or "").strip()
        name = str(raw.get("name") or "").strip()
        value = value_to_markdown(raw.get("value"))
        if not value.strip():
            continue
        target = by_key.get(key) or by_name.get(name)
        if target is None:
            # 整理阶段**不新增事实**：模型臆造的事实一律丢弃（防事实库被撑大）
            continue
        new_value = value.strip()
        if str(getattr(target, "value", "") or "").strip() != new_value:
            target.value = new_value
            changed += 1
    return items, changed


async def finalize_facts(items: list) -> tuple[list, int]:
    """最终整理：去重 + 要求句改写 + 强制保留工期（对齐易标 ``finalizeGlobalFacts``）。

    Args:
        items: 待整理的 ``FactItem`` 序列。

    Returns:
        ``(整理后的 items, 改写条数)``；失败时返回 ``(原 items, 0)``。
    """
    if not items:
        return items, 0
    try:
        system = render("facts_finalize_system",
                        current_facts=json.dumps(
                            items_to_prompt_json(items), ensure_ascii=False))
        obj, _ = await collect_json_response(
            [{"role": "system", "content": system}],
            _validate_finalize, max_retries=1, temperature=0.1,
            json_mode=True, timeout=_timeout(), scene="facts_finalize")
    except Exception as e:  # noqa: BLE001 - fail-soft
        logger.warning("全局事实最终整理阶段失败（保留整理前结果）: %s",
                       e, exc_info=True)
        return items, 0

    out, changed = apply_finalize_result(items, obj)
    logger.info("全局事实最终整理改写 %d 条（事实总数 %d）", changed, len(out))
    return out, changed

