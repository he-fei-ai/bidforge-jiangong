"""《L3 以下"少目录、多正文"编制标准》的程序判据（单一事实源）。

适用文档：用户 2026-11-05 上传的《L3以下"少目录、多正文"编制标准》。
本模块只承载**可量化、可确定性判定**的那部分条款，供三处共用：

1. 目录生成侧（``routers/sse_handlers.py``）：对 AI 产出的目录树做
   「L4+ 无损折叠 + 编号重排」，保证落库目录不超过三级、编号连续；
2. 正文生成侧（``services/content_checkpoint.py`` + ``routers/sse_handlers.py``）：
   把 L3 正文下限写进提示词，并在默认预算路径上把叶子预算抬到标准下限；
3. 校验侧（``services/preflight_engine.py``）：DLV-17（目录过深/节点过多，
   仅提示不阻断）、DLV-18（三级正文单薄）。

设计约束（对齐 AGENTS 4.3「同一判据单一事实源」）：
- 阈值与开关全部来自 ``app.config.settings``，默认值即标准原文数值；
- 本模块不 import 任何提示词 / AI 模块，避免 ``prompts.__init__`` 循环导入；
- 结构归一是**确定性**的：同样的树 + 同样的配置必得同样的结果，不调用 AI；
- 折叠/合并都不直接丢弃内容：L4+ 标题改写为父级 L3 的「（1）（2）」段首引导；
  被合并的三级标题以「（含：A；B）」写回保留节点的 description。

口径（2026-11-05 二次修正）：标准中的「每节 2-5、单章 ≤5、全文 ≤40」均为
**建议性**，L3 数量不做硬性要求。``converge_tree`` 默认只折叠 L4+、不合并
任何 L3；规模类判据只由 DLV-17 告警。只有显式打开
``settings.outline_l3_converge_enforced`` 才恢复自动合并 L3 的能力
（此时单章有效上限 = max(每章上限, 每二级下限 × 该章二级目录数)）。
"""

from __future__ import annotations

import logging
import re

#: 标准原文的关键 L3 主题（标题命中即按 2500 字下限要求）
#: 2026-11-05 口径补充：加入「吊装/脚手架/深基坑」，覆盖「起重吊装」
#: 「落地脚手架」「深基坑支护」等变体（按子串命中）。
DEFAULT_KEY_TITLE_KEYWORDS: tuple[str, ...] = (
    "加固",
    "拆除",
    "危大",
    "危险性较大",
    "消防",
    "调试",
    "质量",
    "安全",
    "监测",
    "监控",
    "吊装",
    "脚手架",
    "深基坑",
)

#: 标准原文数值（配置缺省时回落到这里，避免配置项缺失导致判定静默）
DEFAULT_MAX_L3_PER_SECTION = 5
DEFAULT_MAX_L3_PER_CHAPTER = 5
DEFAULT_MIN_L3_PER_SECTION = 2
DEFAULT_MAX_L3_TOTAL = 40
DEFAULT_L3_WORD_FLOOR = 1500
DEFAULT_L3_KEY_WORD_FLOOR = 2500
DEFAULT_L3_MIN_PARAGRAPHS = 3
DEFAULT_L3_MAX_PARAGRAPHS = 5

_FENCE_LINE_RE = re.compile(r"^\s*(```|~~~)")
_TABLE_ROW_RE = re.compile(r"^\s*\|")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
_BOLD_ONLY_RE = re.compile(r"^\s*(\*\*|__).+(\*\*|__)\s*$")
_LIST_HEAD_RE = re.compile(
    r"^\s*(?:[-*+]\s|\d+(?:\.\d+)*[.、）)]\s*[^\s]|" r"[（(]\s*\d{1,2}\s*[)）]\s|^[一二三四五六七八九十]+、)"
)

logger = logging.getLogger("l3_standard")


def _settings():
    """延迟导入 settings：本模块可能在提示词包初始化期被导入。"""
    from app.config import settings

    return settings


def _cfg_int(name: str, default: int) -> int:
    try:
        value = int(getattr(_settings(), name, default) or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def standard_enabled() -> bool:
    """标准总开关（默认启用）。"""
    return bool(getattr(_settings(), "l3_standard_enabled", True))


def converge_enabled() -> bool:
    """目录自动收敛开关（默认启用；仅影响 AI 生成后的后处理）。"""
    return bool(getattr(_settings(), "outline_l3_converge_enabled", True))


def word_floor_enabled() -> bool:
    """正文预算下限开关（默认启用；用户显式档位/override 优先）。"""
    return bool(getattr(_settings(), "content_l3_word_floor_enabled", True))


def max_l3_per_section() -> int:
    return _cfg_int("outline_l3_max_per_section", DEFAULT_MAX_L3_PER_SECTION)


def max_l3_per_chapter() -> int:
    return _cfg_int("outline_l3_max_per_chapter", DEFAULT_MAX_L3_PER_CHAPTER)


def min_l3_per_section() -> int:
    return _cfg_int("outline_l3_min_per_section", DEFAULT_MIN_L3_PER_SECTION)


def max_l3_total() -> int:
    return _cfg_int("outline_l3_max_total", DEFAULT_MAX_L3_TOTAL)


def converge_enforced() -> bool:
    """L3 规模判据是否为**强制合并**（默认 False）。

    2026-11-05 二次口径：L3 数量不做硬性要求，「每节 2-5 / 单章≤5 / 全文≤40」
    仅为建议值，默认全部交给 DLV-17 告警。只有显式打开
    ``outline_l3_converge_enforced`` 才恢复自动合并 L3 的能力。
    """
    if not converge_enabled():
        return False
    return bool(getattr(_settings(), "outline_l3_converge_enforced", False))


def key_title_keywords() -> tuple[str, ...]:
    raw = str(getattr(_settings(), "content_l3_key_title_keywords", "") or "")
    items = tuple(k.strip() for k in re.split(r"[,，、;；\s]+", raw) if k.strip())
    return items or DEFAULT_KEY_TITLE_KEYWORDS


def is_key_l3_title(title: str | None) -> bool:
    """三级标题是否属于标准点名的「关键 L3」主题。"""
    text = str(title or "")
    return any(k in text for k in key_title_keywords())


def word_floor(title: str | None = "") -> int:
    """某三级标题对应的正文标准下限（普通 1500 / 关键 2500）。"""
    if is_key_l3_title(title):
        return _cfg_int("content_l3_key_word_floor", DEFAULT_L3_KEY_WORD_FLOOR)
    return _cfg_int("content_l3_word_floor", DEFAULT_L3_WORD_FLOOR)


def apply_word_floor(word_budget: int, title: str | None = "") -> int:
    """把叶子预算抬到标准下限（只抬不降，开关关闭时原样返回）。"""
    try:
        budget = int(word_budget or 0)
    except (TypeError, ValueError):
        budget = 0
    if not standard_enabled() or not word_floor_enabled():
        return budget
    return max(budget, word_floor(title))


def min_paragraphs() -> int:
    return _cfg_int("content_l3_min_paragraphs", DEFAULT_L3_MIN_PARAGRAPHS)


def max_paragraphs() -> int:
    return _cfg_int("content_l3_max_paragraphs", DEFAULT_L3_MAX_PARAGRAPHS)


def count_body_paragraphs(content: str | None) -> int:
    """统计正文自然段数（不含标题、列表项、表格行、围栏代码块）。

    只用于「正文过于单薄」的咨询性判定：口径从简、可复现，不追求与
    渲染器逐像素一致；宁可少报也不把结构化内容算成自然段。
    """
    text = str(content or "")
    if not text:
        return 0

    in_fence = False
    blocks: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if _FENCE_LINE_RE.match(line):
            in_fence = not in_fence
            # 围栏行本身既是分隔，也中断当前段落
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        if in_fence:
            continue
        stripped = line.strip()
        if not stripped:
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        if _TABLE_ROW_RE.match(line) or _HEADING_RE.match(line) or _BOLD_ONLY_RE.match(stripped) or _LIST_HEAD_RE.match(line):
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return len(blocks)


# ---------------------------------------------------------------------------
# 目录树统计与收敛
# ---------------------------------------------------------------------------
def _children(node: dict) -> list:
    children = node.get("children")
    return children if isinstance(children, list) else []


def _node_title(node: dict | None) -> str:
    return str((node or {}).get("title") or "")


def _is_placeholder_title(title: str) -> bool:
    try:
        from app.services.placeholder_sections import is_placeholder_title

        return is_placeholder_title(title)
    except Exception:  # 占位模块异常时不影响收敛主流程
        return False


def _node_depth(node: dict) -> int:
    try:
        return int(node.get("level") or 0)
    except (TypeError, ValueError):
        return 0


def _walk(nodes: list, depth: int = 1) -> list[tuple[dict, int]]:
    """返回 ``(node, depth)`` 平铺列表（深度按树位置，不信任节点自带 level）。"""
    out: list[tuple[dict, int]] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        out.append((node, depth))
        out.extend(_walk(_children(node), depth + 1))
    return out


def tree_stats(outline: list | None) -> dict:
    """统计目录树规模：层级、三级条目总数、每章/每节三级条目数。

    返回：:

        {
          "chapters": [{"title": ..., "sections": n, "l3": n, "effective_limit": n}],
          "sections": [{"chapter": ..., "title": ..., "l3": n}],
          "l3_total": int,
          "max_depth": int,
          "over_depth": int,
        }
    """
    chapters: list[dict] = []
    sections: list[dict] = []
    l3_total = 0
    max_depth = 0
    over_depth = 0

    for chapter in outline or []:
        if not isinstance(chapter, dict):
            continue
        sec_items = []
        chapter_l3 = 0
        for sec in _children(chapter):
            if not isinstance(sec, dict):
                continue
            leaves = [c for c in _children(sec) if isinstance(c, dict)]
            sec_items.append({"chapter": _node_title(chapter), "title": _node_title(sec), "l3": len(leaves)})
            chapter_l3 += len(leaves)
        sections.extend(sec_items)
        chapters.append(
            {
                "title": _node_title(chapter),
                "sections": len(sec_items),
                "l3": chapter_l3,
                "effective_limit": chapter_l3_limit(len(sec_items)),
            }
        )
        l3_total += chapter_l3

    for node, depth in _walk(outline or []):
        level = max(depth, _node_depth(node))
        max_depth = max(max_depth, level)
        if level > 3:
            over_depth += 1

    return {
        "chapters": chapters,
        "sections": sections,
        "l3_total": l3_total,
        "max_depth": max_depth,
        "over_depth": over_depth,
    }


def chapter_l3_limit(section_count: int) -> int:
    """单章有效三级条目上限（调和「每章≤5」与「每二级≥2」）。"""
    return max(max_l3_per_chapter(), min_l3_per_section() * max(int(section_count or 0), 0))


_HINT_RE = re.compile(r"（含：([^）]*)）")


def _merged_titles(node: dict) -> list[str]:
    """返回节点自身标题 + 其 description 里已累积的「（含：…）」标题。"""
    titles = [_node_title(node)]
    desc = str(node.get("description") or "")
    match = _HINT_RE.search(desc)
    if match:
        titles.extend(t.strip() for t in match.group(1).split("；") if t.strip())
    titles.extend(_node_title(c) for c in _children(node) if isinstance(c, dict))
    return [t for t in titles if t]


def _merge_hint(target: dict, titles: list[str]) -> None:
    cleaned = [t for t in titles if t]
    if not cleaned:
        return
    desc = str(target.get("description") or "").strip()
    addition = "（含：" + "；".join(cleaned) + "）"
    if "含：" in desc and all(t in desc for t in cleaned):
        return
    target["description"] = (desc + addition).strip()


def _merge_one(children: list) -> bool:
    """把最后一个「可合并」的三级节点并入其前一个同级节点。

    占位章节（计算书/附图等）永不合并、永不删除；若可合并节点不足 2 个
    （剩余都是占位或只剩 1 个），返回 False 由调用方停止收敛。
    """
    candidates = [
        i
        for i, child in enumerate(children)
        if isinstance(child, dict) and not _is_placeholder_title(_node_title(child))
    ]
    if len(candidates) < 2:
        return False
    removed_i = candidates[-1]
    target_i = candidates[-2]
    removed = children[removed_i]
    _merge_hint(children[target_i], _merged_titles(removed))
    children.pop(removed_i)
    return True


def _renumber(outline: list) -> None:
    try:
        from app.services.numbering import renumber_outline_nodes

        renumber_outline_nodes(outline, strip_titles=False)
    except Exception as e:  # 编号失败不影响已收敛的树结构，但必须留痕
        logger.warning("目录 L3 收敛后编号重排失败（树结构已保留）: %s", e)


def _count_descendants(nodes: list) -> int:
    total = 0
    for child in nodes:
        if isinstance(child, dict):
            total += 1 + _count_descendants(_children(child))
    return total


def fold_deep_nodes(outline: list | None, max_depth: int = 3) -> dict:
    """把 L4 及更深节点确定性折叠进父级 L3 正文引导（就地修改）。

    依据 2026-11-05 口径：落库目录树不得包含 L4+ 节点。若 AI 仍输出 L4/L5，
    其标题与说明**不丢失**，而是由 ``outline_utils.clamp_outline_depth``
    统一改写为父级 L3 description 中的「（1）标题：说明；（2）…」段首引导，
    随后由正文生成按该引导展开段落。折叠不删除任何文字、不改变父级编号；
    折叠后由 :func:`converge_tree` 统一重排编号。

    复用 ``outline_utils.clamp_outline_depth`` 作为唯一裁剪实现（含深层标题
    全收集、重复裁剪不叠加等既有护栏）。

    返回 ``{"folded": 被折叠的深层节点数}``。
    """
    if not isinstance(outline, list):
        return {"folded": 0}

    def _count(deeper: list, level: int) -> int:
        total = 0
        for node in deeper:
            if not isinstance(node, dict):
                continue
            children = _children(node)
            if level >= max_depth and children:
                total += _count_descendants(children)
            else:
                total += _count(children, level + 1)
        return total

    folded = _count(outline, 1)
    if folded:
        from app.services.outline_utils import clamp_outline_depth

        clamp_outline_depth(outline, max_depth)
    return {"folded": folded}


def converge_tree(outline: list | None) -> dict:
    """目录结构归一：**无损折叠 L4+**（默认），可选合并超量 L3。

    默认行为（``outline_l3_converge_enforced=False``）：只把 L4 及更深节点折叠
    成父级 L3 的段首引导，L3 数量一个都不合并（标准对 L3 无硬性要求，
    规模判据只由 DLV-17 告警）。
    显式打开 ``outline_l3_converge_enforced`` 后，才依次执行
    「单节超限 → 单章超限 → 全文超限」的确定性合并。

    两种路径都会在结构变化后重排编号，保证不跳号。
    返回 ``{"before","after","merged","folded","skipped"}``。
    """
    stats_before = tree_stats(outline)
    if not isinstance(outline, list) or not standard_enabled() or not converge_enabled():
        return {
            "before": stats_before,
            "after": stats_before,
            "merged": 0,
            "folded": 0,
            "skipped": True,
        }

    folded = fold_deep_nodes(outline)["folded"]
    merged = 0
    enforced = converge_enforced()

    if enforced:
        # 占位章节（计算书/附图/节点详图等，登记「另行补充内容」）整棵子树不合并：
        # 这些条目由另一条规则线按「不生成正文、登记另行补充」处理，目录规模不应
        # 反过来把它们合并掉。
        def _protected(node: dict) -> bool:
            return _is_placeholder_title(_node_title(node))

        # 1) 单节超限
        per_section = max_l3_per_section()
        for chapter in outline:
            if not isinstance(chapter, dict) or _protected(chapter):
                continue
            for sec in _children(chapter):
                if not isinstance(sec, dict) or _protected(sec):
                    continue
                children = _children(sec)
                while len(children) > per_section and _merge_one(children):
                    merged += 1

        # 2) 单章超限（有效上限 = max(每章上限, 每二级下限 × 二级数)）
        for chapter in outline:
            if not isinstance(chapter, dict) or _protected(chapter):
                continue
            sections = [s for s in _children(chapter) if isinstance(s, dict) and not _protected(s)]
            limit = chapter_l3_limit(len(sections))
            while sum(len(_children(s)) for s in sections) > limit:
                target = max(sections, key=lambda s: len(_children(s)), default=None)
                if target is None or not _merge_one(_children(target)):
                    break
                merged += 1

        # 3) 全文超限：从三级条目最多的章开始合并，每个二级目录至少保留 1 个
        #    三级（标准 §3.3 允许「内容较少时不设 L3」，但保底 1 个避免二级
        #    变空壳；§3.5 的「不足 1000 字应合并相邻 L3」优先于建议值）。
        total_limit = max_l3_total()
        while tree_stats(outline)["l3_total"] > total_limit:
            chapter_stats = [
                (ch, tree_stats([ch])["l3_total"])
                for ch in outline
                if isinstance(ch, dict) and not _protected(ch)
            ]
            if not chapter_stats:
                break
            chapter = max(chapter_stats, key=lambda pair: pair[1])[0]
            sections = [s for s in _children(chapter) if isinstance(s, dict) and not _protected(s)]
            trimmable = [s for s in sections if len(_children(s)) > 1]
            if not trimmable:
                break
            target = max(trimmable, key=lambda s: len(_children(s)))
            if not _merge_one(_children(target)):
                break
            merged += 1

    stats_after = tree_stats(outline)
    if folded or merged:
        _renumber(outline)
        stats_after = tree_stats(outline)
    return {
        "before": stats_before,
        "after": stats_after,
        "merged": merged,
        "folded": folded,
        "skipped": not enforced,
    }
