"""全局事实 · 补丁（patch）机制 + 分批合并 + 上下文预算（引入自参考软件易标）

引入背景
--------
参考软件 ``OpenBidKit_Yibiao`` 的全局事实模块（``client/electron/services/
globalFactsTask.cjs``）用**「先建组、再补充、最后统一整理」**三段式组织事实：

1. **建组**：招标文件按上下文预算分段 → 每段只输出「候选大项」``{groups:[]}``
   → 合并（``mergeGroupResultsInBatches``，分批 + 多轮收敛）。
2. **补充**：知识库条目 / 已有原方案按段只输出**补丁** ``{patches:[]}``，
   补丁带 ``mode=append|prepend|replace`` 与 ``target_group_id``，
   由 ``mergeGlobalFactPatches`` 合并回既有大项（**不重新生成全部大项**）。
3. **整理**：``finalizeGlobalFacts`` 对全部大项做去重、把「要求句」改写为
   「本方案统一采用的事实」、并强制保留工期类变量。

本仓（专项方案工具箱）的既有事实模型是**扁平 ``FactItem`` 行**（name/value/key/
category/chapter/…），没有 groups/patches 这一层；参考软件的「补充」与「整理」
两段能力因此完全没有对应实现。本模块把上述**纯逻辑**（归一化 / 校验 / 合并 /
分批 / 等待）原样引入，并补一层面向 ``FactItem`` 的适配器，使参考软件的能力
可以在不改动既有落库不变式（溯源 / 矛盾 / 模拟值闸门）的前提下使用。

与参考软件的一致性（逐条对应，便于回归）
----------------------------------------
===========================  ==============================================
参考软件（.cjs）            本模块
===========================  ==============================================
``normalizeFactId``         :func:`normalize_fact_id`
``ensureUniqueId``          :func:`ensure_unique_id`
``valueToMarkdown``         :func:`value_to_markdown`
``buildMissingFactRule``    :func:`build_missing_value_rule`
``buildGlobalFactsCompletenessRules`` :func:`build_completeness_rules`
``normalizeGlobalFactsPatchResponse`` :func:`normalize_patches_response`
``validateGlobalFactsPatchResponse``  :func:`validate_patches_response`
``mergeGlobalFactPatches``  :func:`merge_fact_patches`
``batchRenderedItems``      :func:`batch_rendered_items`
``waitAllOrThrow``          :func:`wait_all_or_throw`
===========================  ==============================================

设计要点
--------
1. **纯函数、零 DB、零 AI**：本模块只做数据形状与合并语义，可被任意写路径/
   读路径安全调用，也便于单测穷举。
2. **fail-closed**：校验器宁可整体拒绝，也不让残缺结构进入下游（对齐参考
   ``validateGlobalFactsResponse``「缺 groups 直接抛」的语义）。
3. **适配器保不变式**：:func:`apply_patches_to_fact_items` 只改 ``value``，
   绝不重建 ``FactItem``——因此 ``source`` / ``confidence`` / ``is_simulated`` /
   ``chapter`` / ``is_shared`` 等标注在补丁后全部保留。
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Sequence

logger = logging.getLogger("facts_patches")

# =========================================================================
# 一、缺值模式（对齐参考软件 globalFactsMode）
# =========================================================================
#: 参考软件 globalFactsTask.cjs:8 normalizeGlobalFactsMode 的合法值域
MISSING_VALUE_MODES: tuple[str, ...] = ("fabricate", "omit", "placeholder")

#: 本仓既有事实链路的缺值模式（sse_handlers.generate_facts 同口径）
DEFAULT_MISSING_VALUE_MODE = "fabricate"


def normalize_missing_value_mode(value: Any) -> str:
    """归一化缺值模式（对齐参考 ``normalizeGlobalFactsMode``）。"""
    text = str(value or "").strip().lower()
    return text if text in MISSING_VALUE_MODES else DEFAULT_MISSING_VALUE_MODE


def _commitment_wording() -> str:
    """omit 模式的「笼统承诺」措辞约束（参考软件三处复用同一句，单点出口）。"""
    return ("不涉及具体时间、地点、人员、业绩、证书、规格型号、工艺步骤、数量指标的"
            "正确笼统承诺，表明本方案按招标要求执行该项，但不展开具体做法")


def build_missing_value_rule(mode: str) -> str:
    """按缺值模式给出「资料未给出具体值时怎么办」的规则（参考 :12-20）。

    ⚠️ 三种模式的措辞**逐字不同**是契约的一部分：``【待填写】``（placeholder）
    与「笼统承诺」（omit）不可互换使用——混用会让下游无法区分「用户明确留白」
    与「资料确实没有」，正文侧标注也会跟着失真。
    """
    mode = normalize_missing_value_mode(mode)
    if mode == "omit":
        return (f"4. 用户资料没有给出具体值，但该信息对全文一致性重要时，该项仍须保留，"
                f"写成{_commitment_wording()}；严禁省略该项或杜撰具体值。")
    if mode == "placeholder":
        return ("4. 用户资料没有给出具体值，但该信息会影响后续正文一致写法时，必须保留该项"
                "并把事实值逐字写成【待填写】，不要改写成“待定”“TBD”或其他说法，"
                "也不要编造具体值。严禁省略该项。")
    return ("4. 用户资料没有给出具体值，但该信息对全文一致性重要，且当前任务允许补足时，"
            "可以根据项目语境模拟生成合理、稳定、不冲突的事实值。")


def build_completeness_rules(mode: str) -> str:
    """按缺值模式给出「事实补全规则」全文（参考 :22-48）。

    参考软件把这套规则同时注入**分段提取 / 合并 / 补丁 / 最终整理**四处，
    目的是解决弱模型「因为本段没提到就整项省略」的塌缩。本仓沿用同一约束，
    但由本函数单点产出，避免四处各写一份再分叉。
    """
    mode = normalize_missing_value_mode(mode)
    if mode == "fabricate":
        # 参考软件 fabricate 模式不注入完整性规则（保持默认行为逐字一致）
        return ""
    label = "别招欠模式" if mode == "omit" else "放着我来模式"
    missing_note = (
        f"写成{_commitment_wording()}。" if mode == "omit"
        else "值必须逐字写成【待填写】；【待填写】不是空泛内容，合并与最终整理时不得因不够具体而删除。"
    )
    return f"""事实补全规则（{label}）：
1. 先按统一选题标准确定要输出哪些事实项，再按本模式填写值。选题与是否缺少具体值无关。
2. 凡招标要求、评分口径、项目概述、目录或参考材料表明后续技术方案正文需要统一口径的事项，都必须建项并写出内容；不得因材料缺少具体实施方案、人名、日期、指标、型号等而省略该项。
3. 参考材料已经给出明确事实值时，照录材料中的事实值。
4. 严禁虚拟、杜撰、补造任何未在参考材料中出现的具体事实。
5. 材料只有要求、约束或评价口径、没有具体值时，该项仍须保留，{missing_note}
6. 当前分段或当前材料只给出要求、没有具体实施方案时，仍须输出该事实项，不得因此返回空结果或跳过该项。
7. 工期、运维期或交货时间等事项若正文需要统一口径，必须保留为事实项；材料没有具体值时不要编造日期或周期，也不要因此省略该项。"""


# =========================================================================
# 二、标识符与内容归一化（对齐参考 normalizeFactId / ensureUniqueId /
#    valueToMarkdown）
# =========================================================================
_FACT_ID_CLEAN_RE = re.compile(r"[^a-z0-9_\-]+")
_FACT_ID_EDGE_RE = re.compile(r"^_+|_+$")


def normalize_fact_id(value: Any, index: int = 0) -> str:
    """归一化事实/分组标识（对齐参考 ``normalizeFactId``）。

    非 ASCII（中文）标识会被清洗为空 → 落到 ``fact_001`` 形式的序号兜底，
    保证 id **恒非空**（参考 ``validateGlobalFactsResponse`` 要求 id 必填，
    空 id 会让整轮结果被拒）。
    """
    normalized = _FACT_ID_EDGE_RE.sub("", _FACT_ID_CLEAN_RE.sub(
        "_", str(value or "").strip().lower()))
    return normalized or f"fact_{int(index) + 1:03d}"


def ensure_unique_id(base_id: str, used: set) -> str:
    """保证 id 在集合内唯一（对齐参考 ``ensureUniqueId``：冲突时追加 ``_2``）。"""
    next_id = base_id
    suffix = 2
    while next_id in used:
        next_id = f"{base_id}_{suffix}"
        suffix += 1
    used.add(next_id)
    return next_id


def _single_line(value: Any) -> str:
    """压成单行（对齐参考 ``singleLine``：折叠所有空白后 trim）。"""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def value_to_markdown(value: Any) -> str:
    """把任意形状的事实值转成 Markdown 文本（对齐参考 ``valueToMarkdown``）。

    参考软件的弱模型有时会把大项内容返回成 ``["- a", "- b"]`` 或
    ``{"项目经理": "张伟"}``；直接 ``String(obj)`` 会得到 ``[object Object]``
    并静默污染正文。本函数做形状归一，**永不抛异常**。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return _single_line(value)
    if isinstance(value, (list, tuple, set)):
        lines: list[str] = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, str):
                text = item.strip()
                if text:
                    lines.append(f"- {text}")
                continue
            if isinstance(item, dict):
                name = _single_line(
                    item.get("name") or item.get("title") or item.get("fact")
                    or item.get("key") or "事实项")
                detail = _single_line(
                    item.get("value") or item.get("content") or item.get("detail")
                    or item.get("description") or item.get("requirement") or "")
                lines.append(f"- **{name}**" + (f"：{detail}" if detail else ""))
                continue
            text = _single_line(item)
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines)
    if isinstance(value, dict):
        return "\n".join(
            f"- **{_single_line(k)}**：{_single_line(v)}"
            for k, v in value.items() if _single_line(v)
        )
    return _single_line(value)


# =========================================================================
# 三、补丁（patch）数据结构与归一化 / 校验
# =========================================================================
#: 参考软件 globalFactsTask.cjs:230 的 mode 白名单（append / prepend / replace）
PATCH_MODES: tuple[str, ...] = ("append", "prepend", "replace")
DEFAULT_PATCH_MODE = "append"

#: 归一化时可接受的顶层键（参考 :214-224 逐级回退，本仓保持同样宽容度）
_PATCH_LIST_KEYS: tuple[str, ...] = (
    "patches", "supplements", "additions", "items", "facts", "groups",
)
_PATCH_CONTENT_KEYS: tuple[str, ...] = (
    "content", "markdown", "facts", "items", "details", "description", "value",
)
_PATCH_TITLE_KEYS: tuple[str, ...] = ("title", "group_title", "target_group_title", "name")
_PATCH_TARGET_KEYS: tuple[str, ...] = (
    "target_fact_id", "target_group_id", "targetGroupId", "group_id", "target_id", "id",
)
_PATCH_NEW_ID_KEYS: tuple[str, ...] = (
    "new_fact_id", "new_group_id", "newGroupId", "id", "key",
)


def _first_str(obj: dict, keys: Sequence[str]) -> str:
    """按候选键顺序取第一个非空字符串（宽容解析的唯一出口）。"""
    for k in keys:
        val = obj.get(k)
        if val is not None and str(val).strip():
            return _single_line(val)
    return ""


def _first_content(obj: dict) -> str:
    """按候选键顺序取第一个可转 Markdown 的内容字段。"""
    for k in _PATCH_CONTENT_KEYS:
        if k in obj and obj.get(k) is not None:
            text = value_to_markdown(obj.get(k))
            if text.strip():
                return text
    return ""


@dataclass
class FactPatch:
    """一条事实补丁（对齐参考软件的 patch 对象）。

    字段语义与参考一致，只是把 ``target_group_id`` 更名为
    ``target_fact_id``（本仓落库的是扁平事实行，没有「组」实体），
    并保留 :attr:`target_group_id` 作为兼容别名。
    """

    content: str
    target_fact_id: str = ""
    new_fact_id: str = ""
    title: str = ""
    mode: str = DEFAULT_PATCH_MODE
    #: True=新建大项；False=补充既有大项
    create: bool = True

    @property
    def target_group_id(self) -> str:
        """兼容别名（对齐参考软件字段名，便于跨语言比对）。"""
        return self.target_fact_id

    def to_dict(self) -> dict:
        return {
            "target_fact_id": self.target_fact_id,
            "new_fact_id": self.new_fact_id,
            "title": self.title,
            "content": self.content,
            "mode": self.mode,
        }


def normalize_patches_response(value: Any) -> dict:
    """归一化补丁响应（对齐参考 ``normalizeGlobalFactsPatchResponse``）。

    与参考一致地**丢弃**没有内容的补丁（``if (!content) return null``）：
    空补丁对合并无贡献，保留它只会在校验阶段报错并让整轮结果被拒。
    返回 ``{"patches": [FactPatch, ...]}``。
    """
    source: Any = value
    if isinstance(value, dict) and isinstance(value.get("result"), (dict, list)):
        source = value["result"]
    if not isinstance(source, (dict, list)):
        source = {}

    raw_patches: list = []
    if isinstance(source, list):
        raw_patches = source
    else:
        for k in _PATCH_LIST_KEYS:
            cand = source.get(k)
            if isinstance(cand, list):
                raw_patches = cand
                break

    patches: list[FactPatch] = []
    for idx, raw in enumerate(raw_patches):
        if not isinstance(raw, dict):
            continue
        content = _first_content(raw)
        if not content.strip():
            continue
        raw_mode = _single_line(raw.get("mode") or raw.get("operation")
                               or DEFAULT_PATCH_MODE).lower()
        mode = raw_mode if raw_mode in PATCH_MODES else DEFAULT_PATCH_MODE
        target = _first_str(raw, _PATCH_TARGET_KEYS)
        new_id = _first_str(raw, _PATCH_NEW_ID_KEYS) or f"patch_{idx + 1}"
        patches.append(FactPatch(
            content=content,
            target_fact_id=target,
            new_fact_id=normalize_fact_id(new_id, idx),
            title=_first_str(raw, _PATCH_TITLE_KEYS),
            mode=mode,
            # 有明确 target（id 或标题）时视为「补充既有项」，否则「新建」
            create=not bool(target),
        ))
    return {"patches": patches}


def validate_patches_response(value: Any) -> None:
    """校验补丁响应，残缺即抛（对齐参考 ``validateGlobalFactsPatchResponse``）。

    fail-closed：``patches`` 必须存在且每条 ``content`` 非空。
    """
    if not isinstance(value, dict) or not isinstance(value.get("patches"), list):
        raise ValueError("全局事实补充结果缺少 patches")
    for idx, patch in enumerate(value["patches"]):
        if not isinstance(patch, FactPatch):
            raise ValueError(f"全局事实补充第 {idx + 1} 项不是合法补丁对象")
        if not str(getattr(patch, "content", "") or "").strip():
            raise ValueError(f"全局事实补充第 {idx + 1} 项缺少 content")


# =========================================================================
# 四、补丁合并（对齐参考 mergeGlobalFactPatches）
# =========================================================================
def apply_patch_mode(current: str, patch: str, mode: str) -> str:
    """按 mode 合并内容（对齐参考 :267-274）。"""
    patch = str(patch or "").strip()
    current = str(current or "").strip()
    if mode == "replace":
        return patch
    if mode == "prepend":
        return f"{patch}\n\n{current}".strip() if current else patch
    return f"{current}\n\n{patch}".strip() if current else patch


def merge_fact_patches(groups: list, patches: Iterable[FactPatch]) -> list:
    """把补丁合并进大项列表（对齐参考 ``mergeGlobalFactPatches``）。

    定位规则与参考一致：**先按 id，再按 title**；都命中不了就新建大项。
    参考软件的实现是「就地替换数组元素」，本实现保持纯函数语义（返回新
    列表、不改动入参），因为本仓的 ``FactItem`` 适配器需要在同一批对象上
    保留溯源等元数据。
    """
    result = [dict(g) if isinstance(g, dict) else g for g in (groups or [])]
    for patch in patches or []:
        if not isinstance(patch, FactPatch):
            continue
        content = str(patch.content or "").strip()
        if not content:
            continue
        target_idx = -1
        if patch.target_fact_id:
            for i, g in enumerate(result):
                if isinstance(g, dict) and str(g.get("id") or "") == patch.target_fact_id:
                    target_idx = i
                    break
        if target_idx < 0 and patch.title:
            for i, g in enumerate(result):
                if isinstance(g, dict) and patch.title and str(
                        g.get("title") or "") == patch.title:
                    target_idx = i
                    break

        if target_idx >= 0:
            current = result[target_idx]
            current["content"] = apply_patch_mode(
                str(current.get("content") or ""), content, patch.mode)
        else:
            result.append({
                "id": patch.new_fact_id or normalize_fact_id(patch.title, len(result)),
                "title": patch.title or "补充事实项",
                "content": content,
            })
    return result


# =========================================================================
# 五、分批渲染与并发等待（对齐参考 batchRenderedItems / waitAllOrThrow）
# =========================================================================
def batch_rendered_items(items: Sequence, render_item: Callable[[Any], str],
                         limit: int) -> list[list]:
    """按渲染后长度把条目分批（对齐参考 ``batchRenderedItems``）。

    参考软件用它把「分段候选」按上下文预算分批合并，避免单次合并请求超长。
    本实现保持同样的贪心装箱语义（当前批非空且加入后超限 → 先 flush）。
    """
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0:
        return [list(items)] if items else []

    batches: list[list] = []
    current: list = []
    current_length = 0
    for item in items or []:
        try:
            length = len(render_item(item))
        except Exception:  # noqa: BLE001 - 渲染失败不应中断分批
            length = 0
        extra = length + (2 if current else 0)
        if current and current_length + extra > cap:
            batches.append(current)
            current, current_length = [], 0
        current.append(item)
        current_length += extra
    if current:
        batches.append(current)
    return batches


async def wait_all_or_throw(tasks: Sequence[Awaitable]) -> list:
    """并发等待全部协程，**任一失败即抛出首个异常**（对齐参考 ``waitAllOrThrow``）。

    与本仓 ``asyncio.gather(..., return_exceptions=True)`` 的差异是有意的：
    分段提取里 gather 的用途是「单段失败不拖垮整轮」（失败段记入
    ``failed_details``）；而**合并 / 整理**阶段失败就没有降级语义——合并失败
    意味着结果集本身不可信，静默继续会把未经合并的重复大项写进事实库。
    """
    if not tasks:
        return []
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for res in results:
        if isinstance(res, BaseException):
            raise res
    return list(results)


# =========================================================================
# 六、上下文预算分段（对齐参考 getGlobalFactsSegmentLimit）
# =========================================================================
#: 参考软件 globalFactsTask.cjs:4-6 的三个常量
DEFAULT_CONTEXT_LENGTH_LIMIT = 400_000
GLOBAL_FACTS_CONTEXT_LIMIT_RATIO = 0.8
MIN_GLOBAL_FACTS_SEGMENT_CHARS = 1_000


def normalize_positive_int(value: Any, fallback: int) -> int:
    """把配置值收敛为正整数（非法/非正 → fallback，对齐参考 :122-125）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return fallback
    if num != num or num in (float("inf"), float("-inf")) or num <= 0:
        return fallback
    return int(num)


def measure_messages_length(messages: Sequence[dict]) -> int:
    """估算固定消息占用的上下文长度（对齐参考 ``getMessagesContentLength``）。

    每条消息额外计 64 字符的 role / 分隔开销——与参考同口径。
    """
    total = 0
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        total += len(str(msg.get("role") or "user")) + len(str(msg.get("content") or "")) + 64
    return total


def get_segment_limit(context_length_limit: Any = None,
                      fixed_messages: Sequence[dict] = ()) -> int:
    """按上下文预算算出单段可用的正文长度（对齐参考 :363-368）。

    ``floor(limit × 0.8) - 固定消息长度``，下限 ``MIN_GLOBAL_FACTS_SEGMENT_CHARS``。
    """
    limit = normalize_positive_int(context_length_limit, DEFAULT_CONTEXT_LENGTH_LIMIT)
    request_budget = int(limit * GLOBAL_FACTS_CONTEXT_LIMIT_RATIO)
    return max(MIN_GLOBAL_FACTS_SEGMENT_CHARS,
               request_budget - measure_messages_length(fixed_messages))


# =========================================================================
# 七、FactItem 适配器（把补丁落到本仓的扁平事实行上）
# =========================================================================
def fact_item_id(item) -> str:
    """取事实行的稳定标识（归一化键优先，退回事实名）。"""
    return str(getattr(item, "key", "") or getattr(item, "fact_key", "")
               or getattr(item, "name", "") or "")


def apply_patches_to_fact_items(items: Sequence, patches: Iterable[FactPatch]) -> list:
    """把补丁就地合并到 ``FactItem`` 列表（保全部元数据不变式）。

    与 :func:`merge_fact_patches` 的差别只在两点，均为本仓适配所必需：

    1. **就地改写**：只替换命中的 ``item.value``，**不重建对象**——否则
       ``source`` / ``source_ref`` / ``confidence`` / ``is_simulated`` /
       ``chapter`` / ``fact_attr`` / ``source_kind`` / ``is_shared`` 等溯源与
       闸门标注会全部丢失（对齐本仓 ``/global-facts/adjust`` 的设计取舍：
       让 AI 只产出最小操作、值仍走既有写路径）。
    2. **新建项补齐闸门**：补丁新建的事实默认 ``is_simulated=False``
       （补丁内容由知识库 / 原方案提供，属已给定的具体值），并调用
       ``apply_fact_dimensions`` 补齐四维标注，避免新行在章节视图里「消失」。

    Args:
        items: ``FactItem`` 序列（就地修改其 value）。
        patches: 已归一化的 :class:`FactPatch` 序列。

    Returns:
        合并后的 ``FactItem`` 列表。
    """
    result = list(items or [])
    if not result:
        return result

    for patch in patches or []:
        if not isinstance(patch, FactPatch) or not str(patch.content or "").strip():
            continue
        target = None
        if patch.target_fact_id:
            for it in result:
                if fact_item_id(it) == patch.target_fact_id:
                    target = it
                    break
        if target is None and patch.title:
            for it in result:
                if str(getattr(it, "name", "") or "") == patch.title:
                    target = it
                    break

        if target is not None:
            target.value = apply_patch_mode(
                str(getattr(target, "value", "") or ""), patch.content, patch.mode)
            # 补丁 replace 后原有 conflict_values 可能已包含被替换掉的取值，
            # 留着会让前端把已不存在的值渲染成「备选值」。
            if getattr(target, "has_conflict", False) and patch.mode == "replace":
                target.has_conflict = False
                target.conflict_values = []
            continue

        new_item = make_fact_item_from_patch(patch, len(result))
        if new_item is not None:
            result.append(new_item)
    return result


def make_fact_item_from_patch(patch: FactPatch, index: int = 0):
    """由补丁构造新的 ``FactItem``（延迟 import，避免模块级循环依赖）。"""
    content = str(patch.content or "").strip()
    if not content:
        return None
    try:
        from app.services.facts_extractor import FactItem
    except Exception:  # pragma: no cover - 仅在极端导入顺序下发生
        logger.warning("构造补丁事实项失败：facts_extractor 不可导入", exc_info=True)
        return None

    name = (patch.title or "").strip() or derive_title_from_content(content, index)
    try:
        item = FactItem(
            name=name,
            value=content,
            key=patch.new_fact_id or normalize_fact_id(name, index),
            category="other",
            source="patch",
            source_ref="补丁补充",
            is_simulated=False,
            confidence=0.7,
        )
    except Exception:  # noqa: BLE001 - 构造失败不应中断整轮补丁
        logger.warning("构造补丁事实项失败（已跳过该条补丁）", exc_info=True)
        return None

    try:
        from app.services.facts_classification import apply_fact_dimensions
        apply_fact_dimensions([item])
    except Exception:  # noqa: BLE001 - 维度标注失败可由读路径惰性派生兜底
        logger.debug("补丁事实项四维标注失败（读路径将惰性派生兜底）", exc_info=True)
    return item


def derive_title_from_content(content: str, index: int = 0) -> str:
    """补丁未给标题时，从内容首行反推一个稳定标题。"""
    first = next((ln.strip().lstrip("-*").strip()
                  for ln in str(content or "").splitlines() if ln.strip()), "")
    if not first:
        return f"补充事实项{index + 1}"
    for sep in ("：", ":"):
        if sep in first:
            head = first.split(sep, 1)[0].strip()
            if head:
                return head[:20]
    return first[:20] or f"补充事实项{index + 1}"
