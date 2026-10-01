"""目录生成质量校验 —— 围绕方案名称的**全面性** + 目录**连续性**（2026-09-27）

为什么需要本模块（探查结论，均有代码依据）
------------------------------------------
1. ``services.scheme_basis`` 已把方案名称确定性解析为「主要施工内容 / 工序 / 工艺 /
   对象 / 危大分类 / 标准章节模板 key」，但目录生成**从未用它做过覆盖校验**：
   名称里的工序/工艺/对象是否在目录里有落点，全靠提示词 0.1 条款自觉，
   生成完即无人复核 → 要求四「全面性校验」在代码里完全缺实现。
2. 目录连续性只有「生成时裁剪到三级 + renumber 重排」这一层，生成**之后**没有
   任何自动校验（层级跳级 / 编号错位 / 同名章节）→ 要求三「生成后自动校验」
   缺实现。
3. 本模块**纯函数、零 IO、零 AI**，可在生成前/生成后/单测任意调用；校验只产出
   报告，由调用方决定"外科补齐"还是"仅告警"。

设计红线（对齐 AGENTS.md §3.1 与需求「不得编造」）
------------------------------------------------
- 校验**只读**：不修改、不删除任何目录节点；
- 「冗余章节」只**报告**不删除（删章节是不可逆的用户数据变更）；
- 命中判定一律**偏宽**：宁可少报缺失，也不误触发一次外科补齐 AI 调用。
"""
from __future__ import annotations

import logging
import re

from app.services.duplicate_detection import find_similar_titles

logger = logging.getLogger("outline_quality")

#: 与方案名称无关但属"通用必要章节"的标题特征（命中即视为围绕名称合理存在）。
#: 与 ``sse_handlers._DANGEROUS_REQUIRED_KEYWORDS``（危大 10 章）互补而不重复：
#: 本表只用于"冗余候选"报告，不参与缺失判定。
GENERIC_CHAPTER_HINTS: tuple[str, ...] = (
    "工程概况", "工程概述", "编制依据", "施工计划", "施工部署", "施工总平面",
    "施工工艺", "工艺技术", "安全保证", "安全保障", "人员分工", "组织机构",
    "验收", "应急", "计算书", "图纸", "监测", "质量保证", "质量管理",
    "环保", "文明施工", "成本", "进度计划", "风险管理", "新技术", "BIM",
)

#: 名称维度 → 中文标签（报告与提示词共用，避免两处各写一份）
DIMENSION_LABELS: dict[str, str] = {
    "scope_items": "主要施工内容",
    "process_steps": "施工工序",
    "techniques": "施工工艺",
    "objects": "施工对象",
}

_DOT_ID_RE = re.compile(r"\d+(?:\.\d+)*")
_STRIP_RE = re.compile(r"[\s、，,。；;：:（）()【】\[\]]+")


def _norm(text: str) -> str:
    return _STRIP_RE.sub("", str(text or ""))


def collect_titles(outline: list, *, max_level: int | None = None) -> list[dict]:
    """展平目录为 ``[{level, path, title, text}]``（text = 标题 + 描述）。

    ``path`` 为点分**位置**路径（与 outline_json.id 同构）；AI 生成结果的 id 会被
    renumber 覆写，用位置路径做报告定位更稳定。
    """
    out: list[dict] = []

    def _walk(nodes: list, level: int, parent_path: str) -> None:
        if not isinstance(nodes, list) or level > 12:
            return
        for i, n in enumerate(nodes):
            if not isinstance(n, dict):
                continue
            path = f"{parent_path}.{i + 1}" if parent_path else str(i + 1)
            title = str(n.get("title") or "").strip()
            if (max_level is None or level <= max_level) and title:
                out.append({"level": level, "path": path, "title": title,
                            "text": f"{title} {str(n.get('description') or '')}"})
            _walk(n.get("children") or [], level + 1, path)

    _walk(outline or [], 1, "")
    return out


def _common_run(a: str, b: str) -> int:
    """最长公共子串长度（标题/关键词都很短，DP 足够，无性能顾虑）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _keyword_hits(kw: str, entries: list[dict]) -> list[str]:
    """关键词在目录中的落点（返回命中的 path）。判定刻意偏宽：
    - 子串包含（"土方开挖" ⊂ "土方开挖及支护"）；
    - 或与标题/描述有足够长的公共子串（"开挖" 也能命中"基坑土方开挖"）。
    """
    k = _norm(kw)
    if len(k) < 2:
        return []
    hits: list[str] = []
    for e in entries:
        t = _norm(e.get("title") or "")
        d = _norm(e.get("text") or "")
        if (k and (k in t or (d and k in d))) or _common_run(k, t) >= 2 \
                or (d and _common_run(k, d) >= 3):
            hits.append(e["path"])
    return hits


# ---------------------------------------------------------------------------
# 要求四：围绕方案名称的全面性
# ---------------------------------------------------------------------------
def analyze_name_coverage(basis, outline: list) -> dict:
    """方案名称关键词 → 目录章节 的覆盖分析（**只读**）。

    Args:
        basis: ``scheme_basis.SchemeBasis``（None / 无任何维度 → 返回"未评估"）。
        outline: 目录树（AI 生成 / 目录库 / 上传识别 / 手动编辑 皆可）。

    Returns::

        {evaluated, dimensions, matrix:[{dim,item,hits,covered}],
         missing:[str], covered:bool, total_items:int}

    ``missing`` 是「方案名称里有、目录里无任何落点」的关键词（中文化标签前缀），
    供调用方喂给外科式补齐（``_try_outline_patch``）——这是要求四的**唯一出口**。
    """
    if basis is None:
        return {"evaluated": False, "dimensions": dict(DIMENSION_LABELS),
                "matrix": [], "missing": [], "covered": True, "total_items": 0}
    dims = {k: list(getattr(basis, k, None) or []) for k in DIMENSION_LABELS}
    total = sum(len(v) for v in dims.values())
    if total == 0:
        # 名称里没有可解析维度 → 无法评估（**不得**据此判目录不合规）
        return {"evaluated": False, "dimensions": dict(DIMENSION_LABELS),
                "matrix": [], "missing": [], "covered": True, "total_items": 0}

    entries = collect_titles(outline)
    matrix: list[dict] = []
    missing: list[str] = []
    for dim, items in dims.items():
        for it in items:
            hits = _keyword_hits(it, entries) if entries else []
            matrix.append({"dim": dim, "item": it, "hits": hits,
                           "covered": bool(hits)})
            if not hits:
                missing.append(f"{DIMENSION_LABELS[dim]}·{it}")
    return {"evaluated": True, "dimensions": dict(DIMENSION_LABELS), "matrix": matrix,
            "missing": missing, "covered": not missing, "total_items": total}


def find_redundant_titles(basis, outline: list) -> list[dict]:
    """列出"与方案名称零关联且非通用必要章节"的标题候选（**只报告不删除**）。"""
    if basis is None:
        return []
    try:
        kws = {k for k in basis.keywords() if len(k) >= 2}
    except Exception:  # pragma: no cover - 防御
        kws = set()
    if not kws:
        return []
    out: list[dict] = []
    for e in collect_titles(outline, max_level=2):
        t = str(e.get("title") or "")
        if not t or any(h in t for h in GENERIC_CHAPTER_HINTS):
            continue
        tn = _norm(t)
        if not any(kw in tn for kw in kws):
            out.append({"path": e["path"], "title": t, "level": e["level"]})
    return out


# ---------------------------------------------------------------------------
# 要求三：目录连续性
# ---------------------------------------------------------------------------
def check_outline_continuity(outline: list) -> dict:
    """目录连续性校验（层级 / 编号 / 重复章节 / 空壳），返回结构化报告。

    检查项：
    - ``level_gaps``：子节点声明的 ``level`` ≠ 父层级 + 1（跳级）；
    - ``empty_parents``：children 非空但其下无任何有效标题（有父无子壳）；
    - ``duplicate_titles``：同名标题（跨层也记，附首次出现路径便于定位）；
    - ``numbering_mismatch``：节点 id 为点分路径却与位置编号不一致（编号错位）；
    - ``max_level`` / ``nodes``：规模指标。

    附加项（不进 ``issues``、不影响 ``ok``）：
    - ``similar_titles``：**近似**雷同标题对（2-gram Dice ≥ 0.70 且不完全同名，
      如「基坑降水与支护施工」vs「基坑降水及支护施工」）。这是**目录组织建议**
      而非结构缺陷 —— 真正的同名章节由 ``duplicate_titles`` 判为缺陷，
      此处若也计入 ``issues`` 会让 ``ok`` 误变 False、进而误判目录不合格。
    """
    issues: dict[str, list] = {"level_gaps": [], "empty_parents": [],
                               "duplicate_titles": [], "numbering_mismatch": []}
    seen: dict[str, str] = {}
    counter = {"nodes": 0, "max_level": 0}
    # ✅ 新增（2026-10-01）：收集 (path, title) 供标题近似雷同检测使用
    titles: list[dict] = []

    def _visit(node: dict, level: int, path: str) -> None:
        title = str(node.get("title") or "").strip()
        if title:
            counter["nodes"] += 1
            counter["max_level"] = max(counter["max_level"], level)
            titles.append({"path": path, "title": title})
            key = _norm(title)
            if key and key not in seen:
                seen[key] = path
            elif key:
                issues["duplicate_titles"].append(
                    {"path": path, "title": title, "first": seen[key]})
            nid = str(node.get("id") or "").strip()
            if nid and _DOT_ID_RE.fullmatch(nid) and nid != path:
                issues["numbering_mismatch"].append(
                    {"path": path, "id": nid, "expected": path, "title": title})
        children = node.get("children")
        if isinstance(children, list) and children:
            _visit_children(children, level + 1, path, title)

    def _visit_children(children: list, level: int, parent_path: str,
                        parent_title: str) -> None:
        valid = 0
        for j, c in enumerate(children):
            if not isinstance(c, dict):
                continue
            cpath = f"{parent_path}.{j + 1}" if parent_path else str(j + 1)
            ctitle = str(c.get("title") or "").strip()
            if ctitle:
                valid += 1
            declared = c.get("level")
            if isinstance(declared, int) and declared and declared != level:
                issues["level_gaps"].append(
                    {"path": cpath, "title": ctitle, "parent": parent_title,
                     "declared": declared, "expected": level})
            _visit(c, level, cpath)
        if valid == 0 and children:
            issues["empty_parents"].append(
                {"path": parent_path, "count": len(children)})

    for i, n in enumerate(outline or []):
        if isinstance(n, dict):
            _visit(n, 1, str(i + 1))

    # ✅ 新增（2026-10-01）：标题近似雷同（2-gram Dice）。
    # 加法式 —— 只作为顶层附加键返回，**不进入 issues**：近似标题属于
    # 「建议合并或明确区分」的组织建议，不是结构缺陷，因此不得改变
    # ``ok`` / ``issue_counts`` 的既有契约（调用方按 ok 决定是否放行）。
    # 完全同名的对由 ``duplicate_titles`` 负责，find_similar_titles 内部已跳过。
    try:
        similar_titles = find_similar_titles(titles)
    except Exception as exc:  # 防御：标题检测异常不得影响连续性结论
        logger.warning("目录标题近似雷同检测异常: %s", exc, exc_info=True)
        similar_titles = []

    return {
        "ok": not any(issues.values()),
        "nodes": counter["nodes"],
        "max_level": counter["max_level"],
        "issue_counts": {k: len(v) for k, v in issues.items()},
        "similar_titles": similar_titles,
        **issues,
    }


def render_coverage_notice(missing: list[str]) -> str:
    """把缺失的名称关键词渲染成一句话（写进审核建议 / 日志，用户可直接行动）。"""
    if not missing:
        return ""
    head = "、".join(missing[:6])
    more = f"等 {len(missing)} 项" if len(missing) > 6 else ""
    return f"方案名称涉及但目录未覆盖：{head}{more}"


def render_continuity_notice(report: dict) -> str:
    """连续性问题的一句话摘要（无问题返回空串）。"""
    if not report or report.get("ok"):
        return ""
    labels = {"level_gaps": "层级跳级", "empty_parents": "有父无子",
              "duplicate_titles": "同名章节", "numbering_mismatch": "编号错位"}
    parts = [f"{label} {len(report.get(key) or [])} 处"
             for key, label in labels.items() if report.get(key)]
    return "目录连续性问题：" + "、".join(parts) if parts else ""
