"""全局事实 · 补丁合并与分批工具。

本模块解决一个具体问题：知识库条目与原方案已经提供了大量事实信息，但**不能
推翻**已经抽取、已经人工确认的事实。因此采用「以补丁描述增量」的方式：

- 每条补丁声明自己的目标（按 id 或标题定位）与合并方式（追加 / 前置 / 替换）；
- 合并只改动被命中的那一条事实内容，其余元数据（溯源、置信度、模拟值标记、
  所属章节）原样保留；
- 补丁定位不到既有项时，才新建一条事实行。

模块只做数据形状与合并语义（纯函数、零 DB、零 AI），因此可以被写路径与读路径
安全共享，也便于穷举单测。

不变式（任何改动都不得破坏）：
1. 校验失败一律整体拒绝，不让残缺结构进入下游；
2. 适配器只改 ``value``，不重建事实对象，避免溯源与闸门标注丢失；
3. 补丁新建的事实默认 ``is_simulated=False``（内容来自既有资料，不是模型编造）。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Sequence

logger = logging.getLogger("facts_patches")

# =========================================================================
# 一、缺值模式
# =========================================================================
#: 缺值模式的合法值域。
#: ``fabricate``：允许按项目语境模拟合理值（本仓默认，保持既有行为）；
#: ``omit``：保留事实项但只写笼统承诺；
#: ``placeholder``：保留事实项并逐字写「【待填写】」。
MISSING_VALUE_MODES: tuple[str, ...] = ("fabricate", "omit", "placeholder")

#: 默认缺值模式。历史方案与既有事实链路的缺省口径都是「允许合理模拟」，
#: 保持默认值即保持升级前后结果一致。
DEFAULT_MISSING_VALUE_MODE = "fabricate"

#: 「笼统承诺」的固定措辞：三处提示词复用同一句，单点出口避免分叉。
_COMMITMENT_WORDING = (
    "不涉及具体时间、地点、人员、业绩、证书、规格型号、工艺步骤、数量指标的"
    "正确笼统承诺，表明本方案按设计与合同要求执行该项，但不展开具体做法"
)


def normalize_missing_value_mode(value: Any) -> str:
    """归一化缺值模式：非法值回落到默认模式，保证下游永远拿到三选一。"""
    text = str(value or "").strip().lower()
    return text if text in MISSING_VALUE_MODES else DEFAULT_MISSING_VALUE_MODE


def _omit_rule() -> str:
    return (
        f"4. 用户资料没有给出具体值，但该信息对全文一致性重要时，该项仍须保留，"
        f"写成{_COMMITMENT_WORDING}；严禁省略该项或杜撰具体值。"
    )


def _placeholder_rule() -> str:
    return (
        "4. 用户资料没有给出具体值，但该信息会影响后续正文一致写法时，必须保留该项"
        "并把事实值逐字写成【待填写】，不要改写成“待定”“TBD”或其他说法，"
        "也不要编造具体值。严禁省略该项。"
    )


def _fabricate_rule() -> str:
    return (
        "4. 用户资料没有给出具体值，但该信息对全文一致性重要，且当前任务允许补足时，"
        "可以根据项目语境模拟生成合理、稳定、不冲突的事实值。"
    )


#: 缺值模式 → 第 4 条规则的策略表（新增模式只需追加一项，规则顺序不会错位）。
_MISSING_VALUE_RULE_BUILDERS: dict[str, Callable[[], str]] = {
    "omit": _omit_rule,
    "placeholder": _placeholder_rule,
    "fabricate": _fabricate_rule,
}


def build_missing_value_rule(mode: str) -> str:
    """按缺值模式给出「资料未给出具体值时怎么办」的规则条目。

    三种模式的措辞不可互换：``【待填写】`` 表示「用户明确留白」，笼统承诺表示
    「资料确实没有」。混用会让正文侧标注与下游判断失真。
    """
    builder = _MISSING_VALUE_RULE_BUILDERS.get(normalize_missing_value_mode(mode), _fabricate_rule)
    return builder()


#: 完整性规则的展示名（只在提示词标题里出现，便于用户识别当前模式）。
_COMPLETENESS_LABELS = {"omit": "别招欠模式", "placeholder": "放着我来模式"}

#: 完整性规则里「缺具体值时怎么写」的补充句（与第 4 条规则同义但用于第 5 条）。
_COMPLETENESS_MISSING_NOTES = {
    "omit": f"写成{_COMMITMENT_WORDING}。",
    "placeholder": "值必须逐字写成【待填写】；【待填写】不是空泛内容，合并与最终整理时不得因不够具体而删除。",
}

#: 完整性规则正文骨架：``{label}`` 是模式名，``{missing_note}`` 是缺值写法。
#: 规则条数固定为 7，目的是覆盖「选题 / 建项 / 照录 / 禁编造 / 缺值 / 空结果 /
#: 工期类保留」七类塌缩场景；缺任一条都会让弱模型在其中一类上整项省略。
_COMPLETENESS_TEMPLATE = """事实补全规则（{label}）：
1. 先按统一选题标准确定要输出哪些事实项，再按本模式填写值。选题与是否缺少具体值无关。
2. 凡设计文件、合同要求、项目概述、目录或参考材料表明后续技术方案正文需要统一口径的事项，都必须建项并写出内容；不得因材料缺少具体实施方案、人名、日期、指标、型号等而省略该项。
3. 参考材料已经给出明确事实值时，照录材料中的事实值。
4. 严禁虚拟、杜撰、补造任何未在参考材料中出现的具体事实。
5. 材料只有要求、约束或评价口径、没有具体值时，该项仍须保留，{missing_note}
6. 当前分段或当前材料只给出要求、没有具体实施方案时，仍须输出该事实项，不得因此返回空结果或跳过该项。
7. 工期、运维期或交货时间等事项若正文需要统一口径，必须保留为事实项；材料没有具体值时不要编造日期或周期，也不要因此省略该项。"""


def build_completeness_rules(mode: str) -> str:
    """按缺值模式产出「事实补全规则」全文（fabricate 模式不注入，返回空串）。

    同一份规则会被注入分段提取、合并、补丁、最终整理四个环节；由本函数单点
    产出，避免四处各写一份而分叉。
    """
    mode = normalize_missing_value_mode(mode)
    label = _COMPLETENESS_LABELS.get(mode)
    if not label:
        return ""
    return _COMPLETENESS_TEMPLATE.format(label=label, missing_note=_COMPLETENESS_MISSING_NOTES[mode])


# =========================================================================
# 二、标识符与内容归一化
# =========================================================================
#: 标识符允许的字符集合：小写字母、数字、下划线、连字符。其余字符（含中文）
#: 一律折叠为下划线后清洗，最终落到 ``fact_001`` 形式的序号兜底。
_FACT_ID_ALLOWED_RE = re.compile(r"[^a-z0-9_\-]+")
_FACT_ID_TRIMMED_EDGE_RE = re.compile(r"^_+|_+$")

#: 序号兜底的补零宽度。
#: 取 3 位可覆盖 999 条事实（单份方案的典型事实量在数十条量级），补零让序号
#: 在字典序下与自然序一致；超出 999 时字段自然变宽，不影响唯一性。
_FACT_ID_DIGITS = 3


def normalize_fact_id(value: Any, index: int = 0) -> str:
    """归一化事实/分组标识，保证结果恒为非空。

    id 为空会让整轮补丁被校验器拒绝，因此无有效字符时按位置生成兜底 id。
    """
    cleaned = _FACT_ID_TRIMMED_EDGE_RE.sub("", _FACT_ID_ALLOWED_RE.sub("_", str(value or "").strip().lower()))
    return cleaned or f"fact_{int(index) + 1:0{_FACT_ID_DIGITS}d}"


def ensure_unique_id(base_id: str, used: set) -> str:
    """在集合内保证 id 唯一：冲突时依次追加 ``_2``、``_3``……。

    从 2 起编号是自然语言的「第几份」习惯，避免出现 ``_1`` 这种与基准 id 易混
    的后缀；同时把最终 id 写回集合，调用方连续取号即可。
    """
    candidate = base_id
    suffix = 2
    while candidate in used:
        candidate = f"{base_id}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _single_line(value: Any) -> str:
    """把任意值压成一行：折叠所有空白（含换行）后去首尾空白。"""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _render_string(value: str) -> str:
    return value.strip()


def _render_scalar(value) -> str:
    return _single_line(value)


def _render_sequence_item(item) -> str:
    """渲染列表中的单个元素（字符串 → 列表项；对象 → 「名称：内容」）。"""
    if item is None:
        return ""
    if isinstance(item, str):
        return f"- {item.strip()}" if item.strip() else ""
    if isinstance(item, dict):
        name = _single_line(item.get("name") or item.get("title") or item.get("fact") or item.get("key") or "事实项")
        detail = _single_line(
            item.get("value")
            or item.get("content")
            or item.get("detail")
            or item.get("description")
            or item.get("requirement")
            or ""
        )
        return f"- **{name}**" + (f"：{detail}" if detail else "")
    text = _single_line(item)
    return f"- {text}" if text else ""


def _render_sequence(value) -> str:
    return "\n".join(line for line in (_render_sequence_item(item) for item in value) if line)


def _render_mapping(value: dict) -> str:
    return "\n".join(f"- **{_single_line(k)}**：{_single_line(v)}" for k, v in value.items() if _single_line(v))


#: 值渲染策略：按类型谓词声明处理方式，替代 isinstance 判断链。
#: 顺序敏感：bool 必须先于 int（bool 是 int 的子类）。
_VALUE_RENDER_STRATEGIES: tuple[tuple[Callable[[Any], bool], Callable[[Any], str]], ...] = (
    (lambda v: v is None, lambda v: ""),
    (lambda v: isinstance(v, str), _render_string),
    (lambda v: isinstance(v, bool), _render_scalar),
    (lambda v: isinstance(v, (int, float)), _render_scalar),
    (lambda v: isinstance(v, (list, tuple, set)), _render_sequence),
    (lambda v: isinstance(v, dict), _render_mapping),
)


def value_to_markdown(value: Any) -> str:
    """把任意形状的事实值转成 Markdown 文本（永不抛异常）。

    弱模型有时把大项内容返回成 ``["- a", "- b"]`` 或 ``{"项目经理": "张伟"}``；
    直接字符串化会得到 ``[object Object]`` 之类的脏值并污染正文，因此必须按
    形状归一。
    """
    for matches, render_fn in _VALUE_RENDER_STRATEGIES:
        if matches(value):
            return render_fn(value)
    return _single_line(value)


# =========================================================================
# 三、补丁对象与归一化 / 校验
# =========================================================================
#: 补丁合并方式白名单。
PATCH_MODES: tuple[str, ...] = ("append", "prepend", "replace")
DEFAULT_PATCH_MODE = "append"


@dataclass(frozen=True)
class _PatchKeyAliases:
    """补丁字段别名表（弱模型/不同调用点返回的键名差异集中在这里）。"""

    list_roots: tuple[str, ...]
    content: tuple[str, ...]
    title: tuple[str, ...]
    target: tuple[str, ...]
    new_id: tuple[str, ...]


_PATCH_KEYS = _PatchKeyAliases(
    # 补丁数组可能挂在不同的顶层键下，按顺序取第一个数组
    list_roots=("patches", "supplements", "additions", "items", "facts", "groups"),
    # 内容字段的别名。``text`` 必须保留：知识库补充阶段的对象习惯用 text 承载正文，
    # 若在此缺失，形状合法但内容被丢弃，最终一条补丁都产不出来。
    content=("content", "markdown", "facts", "items", "details", "description", "value", "text"),
    title=("title", "group_title", "target_group_title", "name"),
    target=("target_fact_id", "target_group_id", "targetGroupId", "group_id", "target_id", "id"),
    new_id=("new_fact_id", "new_group_id", "newGroupId", "id", "key"),
)


def _first_str(obj: dict, keys: Sequence[str]) -> str:
    """按候选键顺序取第一个非空值并压成单行。"""
    for key in keys:
        value = obj.get(key)
        if value is not None and str(value).strip():
            return _single_line(value)
    return ""


def _first_content(obj: dict) -> str:
    """按候选键顺序取第一个可转 Markdown 的内容。"""
    for key in _PATCH_KEYS.content:
        if key in obj and obj.get(key) is not None:
            text = value_to_markdown(obj.get(key))
            if text.strip():
                return text
    return ""


@dataclass
class FactPatch:
    """一条事实补丁。

    ``target_fact_id`` 指向既有的扁平事实行（本仓没有「组」实体）；为兼容
    跨语言命名，同时提供 :attr:`target_group_id` 只读别名。
    """

    content: str
    target_fact_id: str = ""
    new_fact_id: str = ""
    title: str = ""
    mode: str = DEFAULT_PATCH_MODE
    #: True=新建事实项；False=补充既有事实项
    create: bool = True

    @property
    def target_group_id(self) -> str:
        return self.target_fact_id

    def to_dict(self) -> dict:
        return {
            "target_fact_id": self.target_fact_id,
            "new_fact_id": self.new_fact_id,
            "title": self.title,
            "content": self.content,
            "mode": self.mode,
        }


def _unwrap_patch_source(value: Any) -> Any:
    """剥掉 ``{"result": ...}`` 外壳；非 dict/list 一律视为空。"""
    if isinstance(value, dict) and isinstance(value.get("result"), (dict, list)):
        value = value["result"]
    return value if isinstance(value, (dict, list)) else {}


def _iter_raw_patches(source) -> Iterable:
    """从顶层对象中取出补丁数组（顶层本身就是数组时直接使用）。"""
    if isinstance(source, list):
        return source
    for key in _PATCH_KEYS.list_roots:
        candidate = source.get(key)
        if isinstance(candidate, list):
            return candidate
    return []


def normalize_patches_response(value: Any) -> dict:
    """归一化补丁响应；无内容的补丁直接丢弃（空补丁对合并无贡献）。

    返回 ``{"patches": [FactPatch, ...]}``，供 :func:`validate_patches_response`
    与合并函数直接消费。
    """
    raw_patches = _iter_raw_patches(_unwrap_patch_source(value))
    patches: list[FactPatch] = []
    for position, raw in enumerate(raw_patches):
        if not isinstance(raw, dict):
            continue
        content = _first_content(raw)
        if not content.strip():
            continue
        requested_mode = _single_line(raw.get("mode") or raw.get("operation") or DEFAULT_PATCH_MODE).lower()
        mode = requested_mode if requested_mode in PATCH_MODES else DEFAULT_PATCH_MODE
        target = _first_str(raw, _PATCH_KEYS.target)
        new_id = _first_str(raw, _PATCH_KEYS.new_id) or f"patch_{position + 1}"
        patches.append(
            FactPatch(
                content=content,
                target_fact_id=target,
                new_fact_id=normalize_fact_id(new_id, position),
                title=_first_str(raw, _PATCH_KEYS.title),
                mode=mode,
                # 有明确目标（id 或标题）即视为补充既有项，否则新建
                create=not bool(target),
            )
        )
    return {"patches": patches}


def validate_patches_response(value: Any) -> None:
    """校验补丁响应（fail-closed）：``patches`` 必须存在且每条 content 非空。"""
    if not isinstance(value, dict) or not isinstance(value.get("patches"), list):
        raise ValueError("全局事实补充结果缺少 patches")
    for position, patch in enumerate(value["patches"], start=1):
        if not isinstance(patch, FactPatch):
            raise ValueError(f"全局事实补充第 {position} 项不是合法补丁对象")
        if not str(getattr(patch, "content", "") or "").strip():
            raise ValueError(f"全局事实补充第 {position} 项缺少 content")


# =========================================================================
# 四、补丁合并
# =========================================================================
def _apply_replace(current: str, patch: str) -> str:
    return patch


def _apply_prepend(current: str, patch: str) -> str:
    return f"{patch}\n\n{current}".strip() if current else patch


def _apply_append(current: str, patch: str) -> str:
    return f"{current}\n\n{patch}".strip() if current else patch


#: 合并方式 → 拼接函数。段落之间统一用空行分隔，与正文的 Markdown 书写习惯一致。
_MODE_APPLIERS: dict[str, Callable[[str, str], str]] = {
    "replace": _apply_replace,
    "prepend": _apply_prepend,
    "append": _apply_append,
}


def apply_patch_mode(current: str, patch: str, mode: str) -> str:
    """按合并方式拼接内容；未知方式按追加处理（与历史行为一致）。"""
    patch = str(patch or "").strip()
    current = str(current or "").strip()
    return _MODE_APPLIERS.get(mode, _apply_append)(current, patch)


def _locate_target(
    patch: FactPatch,
    id_index: dict[str, int],
    title_index: dict[str, int],
) -> int:
    """定位补丁目标：先按 id，再按标题，都未命中返回 -1。"""
    if patch.target_fact_id:
        position = id_index.get(patch.target_fact_id)
        if position is not None:
            return position
    if patch.title:
        position = title_index.get(patch.title)
        if position is not None:
            return position
    return -1


def _register_dict_lookup(position: int, item: dict, id_index: dict[str, int], title_index: dict[str, int]) -> None:
    """把大项的 id / 标题登记进索引（首次出现优先，setdefault 保持该语义）。"""
    identifier = str(item.get("id") or "")
    if identifier:
        id_index.setdefault(identifier, position)
    title = str(item.get("title") or "")
    if title:
        title_index.setdefault(title, position)


def merge_fact_patches(groups: list, patches: Iterable[FactPatch]) -> list:
    """把补丁合并进大项列表（纯函数：返回新列表、不改动入参）。

    定位规则：先按 id，再按标题；都命中不了就新建大项。索引在合并开始时构建
    一次，新建项同步登记，保证后续补丁能命中新项（与逐条线性扫描等价）。
    """
    result = [dict(group) if isinstance(group, dict) else group for group in (groups or [])]
    id_index: dict[str, int] = {}
    title_index: dict[str, int] = {}
    for position, group in enumerate(result):
        if isinstance(group, dict):
            _register_dict_lookup(position, group, id_index, title_index)

    for patch in patches or []:
        if not isinstance(patch, FactPatch):
            continue
        content = str(patch.content or "").strip()
        if not content:
            continue
        position = _locate_target(patch, id_index, title_index)
        if position >= 0:
            target = result[position]
            target["content"] = apply_patch_mode(str(target.get("content") or ""), content, patch.mode)
            continue
        created = {
            "id": patch.new_fact_id or normalize_fact_id(patch.title, len(result)),
            "title": patch.title or "补充事实项",
            "content": content,
        }
        result.append(created)
        _register_dict_lookup(len(result) - 1, created, id_index, title_index)
    return result


def _locate_fact_item(patch: FactPatch, key_index: dict[str, int], name_index: dict[str, int]) -> int:
    """在 FactItem 列表中定位补丁目标（key/事实键优先，事实名其次）。"""
    if patch.target_fact_id:
        position = key_index.get(patch.target_fact_id)
        if position is not None:
            return position
    if patch.title:
        position = name_index.get(patch.title)
        if position is not None:
            return position
    return -1


def fact_item_id(item) -> str:
    """取事实行的稳定标识（归一化键优先，退回事实名）。"""
    return str(getattr(item, "key", "") or getattr(item, "fact_key", "") or getattr(item, "name", "") or "")


def _register_fact_item(position: int, item, key_index: dict[str, int], name_index: dict[str, int]) -> None:
    identifier = fact_item_id(item)
    if identifier:
        key_index.setdefault(identifier, position)
    name = str(getattr(item, "name", "") or "")
    if name:
        name_index.setdefault(name, position)


def apply_patches_to_fact_items(items: Sequence, patches: Iterable[FactPatch]) -> list:
    """把补丁合并到 ``FactItem`` 列表（就地改写 value，保全元数据）。

    与 :func:`merge_fact_patches` 的差别：
    1. 只替换命中项的 ``value``，不重建对象 —— 否则 ``source`` / ``confidence``
       / ``is_simulated`` / ``chapter`` / ``is_shared`` 等标注会全部丢失；
    2. 新建项补齐闸门标注（``is_simulated=False`` 并调用维度派生），避免新行
       在章节视图里「消失」。
    """
    result = list(items or [])
    if not result:
        return result

    key_index: dict[str, int] = {}
    name_index: dict[str, int] = {}
    for position, item in enumerate(result):
        _register_fact_item(position, item, key_index, name_index)

    for patch in patches or []:
        if not isinstance(patch, FactPatch) or not str(patch.content or "").strip():
            continue
        position = _locate_fact_item(patch, key_index, name_index)
        if position >= 0:
            target = result[position]
            target.value = apply_patch_mode(str(getattr(target, "value", "") or ""), patch.content, patch.mode)
            # replace 之后旧的候选值已不存在，继续保留会让前端把它们当成备选值渲染
            if getattr(target, "has_conflict", False) and patch.mode == "replace":
                target.has_conflict = False
                target.conflict_values = []
            continue

        created = make_fact_item_from_patch(patch, len(result))
        if created is not None:
            result.append(created)
            _register_fact_item(len(result) - 1, created, key_index, name_index)
    return result


def make_fact_item_from_patch(patch: FactPatch, index: int = 0):
    """由补丁构造新的 ``FactItem``（延迟导入，避免模块级循环依赖）。"""
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
        #: 补丁内容来自知识库/原方案（间接佐证），置信度取 0.7：低于直接抽取事实
        #: 的 0.9，高于模拟值的 0.5，处于「可用但需人工复核」区间。
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


#: 标题长度上限。列表行与章节侧的展示宽度约 20 个汉字，超出部分由正文承担；
#: 截断只影响标题，不影响事实内容。
_TITLE_MAX_CHARS = 20

#: 标题分隔符（中文全角优先：先按「：」切分，再退回半角）。
_TITLE_SEPARATORS = ("：", ":")


def derive_title_from_content(content: str, index: int = 0) -> str:
    """补丁未给标题时，从内容首行反推一个稳定标题。"""
    first_line = next(
        (line.strip().lstrip("-*").strip() for line in str(content or "").splitlines() if line.strip()), ""
    )
    if not first_line:
        return f"补充事实项{index + 1}"
    for separator in _TITLE_SEPARATORS:
        if separator in first_line:
            head = first_line.split(separator, 1)[0].strip()
            if head:
                return head[:_TITLE_MAX_CHARS]
    return first_line[:_TITLE_MAX_CHARS] or f"补充事实项{index + 1}"


# =========================================================================
# 五、分批渲染与并发等待
# =========================================================================
#: 批内条目之间预留的分隔开销（拼接时使用空行，共 2 个字符）。
_BATCH_SEPARATOR_COST = 2


def _rendered_length(item, render_item: Callable[[Any], str]) -> int:
    """渲染并取长度；渲染失败按 0 计（分批不应因渲染异常中断）。"""
    try:
        return len(render_item(item))
    except Exception:  # noqa: BLE001
        return 0


def batch_rendered_items(items: Sequence, render_item: Callable[[Any], str], limit: int) -> list[list]:
    """按渲染后长度做贪心装箱：当前批非空且加入后超限时先封批。

    返回批次数量的上限由 ``limit`` 决定；``limit`` 非正数时退化为「全部一批」。
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
        length = _rendered_length(item, render_item)
        extra = length + (_BATCH_SEPARATOR_COST if current else 0)
        if current and current_length + extra > cap:
            batches.append(current)
            current, current_length = [], 0
        current.append(item)
        current_length += extra
    if current:
        batches.append(current)
    return batches


async def wait_all_or_throw(tasks: Sequence[Awaitable]) -> list:
    """并发等待全部协程，任一失败即抛出首个异常。

    与 ``gather(return_exceptions=True)`` 的差别是有意的：提取阶段单段失败可以
    记录后继续（失败段进 ``failed_details``）；而合并/整理阶段失败意味着结果集
    本身不可信，静默继续会把未经合并的重复项写进事实库。
    """
    if not tasks:
        return []
    results = await asyncio.gather(*tasks, return_exceptions=True)
    first_error = next((item for item in results if isinstance(item, BaseException)), None)
    if first_error is not None:
        raise first_error
    return list(results)


# =========================================================================
# 六、上下文预算分段
# =========================================================================
#: 上下文长度的安全上限（字符）。
#: 推导：以本仓既有方案库的招标文件为样本，单份文件大多在 8 万~20 万字符，
#: 40 万覆盖到 P99 以上的长尾；这是一个**安全上限**，不是某个模型窗口的换算
#: 结果（不同 provider 的窗口差异很大，硬编码窗口值反而更脆弱）。
DEFAULT_CONTEXT_LENGTH_LIMIT = 400_000

#: 预算使用比例。
#: 取 0.8 是给「输出 token + 消息骨架 + 模型侧格式开销」预留 20%：事实提取的
#: 输出长度与输入分段长度同量级，留 20% 可避免末尾截断导致 JSON 不完整。
GLOBAL_FACTS_CONTEXT_LIMIT_RATIO = 0.8

#: 单段下限（字符）。
#: 低于 1000 字符的段落所能提供的事实信息不足以抵消一次调用的固定开销
#: （system 提示 + 消息骨架），因此把预算扣减的负面影响兜底到此值。
MIN_GLOBAL_FACTS_SEGMENT_CHARS = 1_000

#: 每条消息的固定开销估算（字符）。
#: 消息由 role 标记、分隔符与协议层包装组成；按常见 tokenizer 的 20~30 token
#: 估算并取偏保守的 64 字符，避免固定提示被低估后挤占正文预算。
_MESSAGE_FIXED_OVERHEAD = 64


def normalize_positive_int(value: Any, fallback: int) -> int:
    """把配置值收敛为正整数；非法、NaN、无穷、非正一律回落到 fallback。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number) or number <= 0:
        return fallback
    return int(number)


def measure_messages_length(messages: Sequence[dict]) -> int:
    """估算固定消息占用的上下文长度（每条消息计入 role/分隔的固定开销）。"""
    total = 0
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        total += (
            len(str(message.get("role") or "user")) + len(str(message.get("content") or "")) + _MESSAGE_FIXED_OVERHEAD
        )
    return total


def get_segment_limit(context_length_limit: Any = None, fixed_messages: Sequence[dict] = ()) -> int:
    """按上下文预算算出单段可用正文长度：``floor(limit × 0.8) - 固定消息长度``，向下兜底。"""
    limit = normalize_positive_int(context_length_limit, DEFAULT_CONTEXT_LENGTH_LIMIT)
    request_budget = int(limit * GLOBAL_FACTS_CONTEXT_LIMIT_RATIO)
    return max(MIN_GLOBAL_FACTS_SEGMENT_CHARS, request_budget - measure_messages_length(fixed_messages))
