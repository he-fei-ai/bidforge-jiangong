"""目录结构硬性标准（R162 · 2026-11-01）——层级/数量/编号的**单一事实源**。

用户指令（2026-11-01）：目录树管理到 L3，「三级下 4~6 个四级」的数量控制
不再约束目录树，转为 L3 叶子节**正文生成**结构要求（sse_handlers 为 L3 叶
注入【结构要求】块；成稿中「N）」要点以 Heading 4 呈现，编号命名空间与
DB 子章节 X.X 隔离）。硬性约束为——
  1. 层级深度：目录树最多 L4 级，L3 为最小叶子（正文写作单位）；L4 仅作
     兼容层（旧数据/手工新增/上传解析可存在，不作数量校验）；严禁 L5。
  2. 数量控制：L1 下包含 3~5 个 L2；L2 下包含 3~5 个 L3。L3 下是否细分
     L4 不作数量校验。
  3. 编号规则：严格遵循 第一章 -> 1 -> 1.1 -> 1.1.1 的子承父系规则
     （显示编号由 heading_v2 按树位置推导；落库权威为 sections.outline_json.id
     点分编号，子编号必须以父编号为前缀）。

⚠️ 本模块取代 R151（2026-10-31）的「四级叶子」标准（L4 为最小叶子、
L3 下 4~6 个 L4）——该标准与 one-shot 生成路径的模型自然产出持续冲突
（R162 取证：无骨架兜底 + 弱模型必产三级树 → 合规修复轮白烧后 failed），
按用户指令翻转为「三级叶子」标准。翻转方向为**放宽**：旧合规四级树在
新判据下仍合规，零数据迁移。

消费方（全链同源，改动判据只改这里）：
  - 目录生成链路（sse_handlers.generate_outline / outline agent）：生成后校验，
    不合规触发 AI 补齐/精简（outline_compliance.fix_outline_compliance），
    仍不合规 failed；
  - 正文生成前置闸门（sse_handlers.generate_content）：树不合规直接 400 拒绝，
    文案「目录结构不合规，请先修复目录」；
  - 目录标准化修复端点（sections.standardize-outline）：L1/L2 数量违规 AI 补齐；
  - 清洗脚本（backend/_r151_clean_outline.py）：防御性 L5+ 合并至父级；
  - 前端红点警告（frontend/src/utils/outlineRules.ts）：数值与本模块 parity，
    由 backend/tests/test_r162_outline_leaf3_standard_20261101.py 双向锚定。

纯函数模块：不 import 任何 AI / DB 依赖（仅 stdlib + numbering 的点分编号判读）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 硬性标准常量（单一事实源）
# ---------------------------------------------------------------------------

#: 目录允许的最大层级（L4 仅作兼容层；出现 L5 即 DEPTH_EXCEEDED）
OUTLINE_MAX_LEVEL = 4

#: 最小叶子层级（正文生成以该级为单位；R162 起 L3 即可作叶子）
LEAF_LEVEL = 3

#: 各父级层级的子节点数量区间（闭区间）：L1 下 3~5 个 L2、L2 下 3~5 个 L3。
#: L3 为最小叶子，其下是否细分 L4 **不作数量校验**（R162 用户指令：
#: 数量控制转入 L3 叶子节的正文生成提示词）。
CHILDREN_MIN: dict[int, int] = {1: 3, 2: 3}
CHILDREN_MAX: dict[int, int] = {1: 5, 2: 5}

# 违规代码（前端/提示词/日志共用的稳定字面量）
V_DEPTH_EXCEEDED = "depth_exceeded"          # 出现 L5 及更深
V_TOO_FEW_CHILDREN = "too_few_children"      # 子节点数低于下限（含 0=叶子层级不足）
V_TOO_MANY_CHILDREN = "too_many_children"    # 子节点数高于上限
V_NUMBERING_MISMATCH = "numbering_mismatch"  # 子编号未承父编号（仅 DB 行校验）

_LEVEL_NAMES = {1: "一级", 2: "二级", 3: "三级", 4: "四级"}


@dataclass
class OutlineViolation:
    """一条目录结构违规。path 是从根到父节点的标题链（定位用）。"""

    code: str
    level: int
    title: str
    detail: str
    path: list[str] = field(default_factory=list)
    expected: str = ""
    actual: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "level": self.level,
            "title": self.title,
            "detail": self.detail,
            "path": list(self.path),
            "expected": self.expected,
            "actual": self.actual,
        }


def violations_to_payload(violations: list[OutlineViolation]) -> list[dict]:
    """违规清单 → 可 JSON 序列化的载荷（API / SSE / 前端共用）。"""
    return [v.to_dict() for v in violations]


def format_violations_message(violations: list[OutlineViolation], *, max_items: int = 5) -> str:
    """把违规清单压缩成一段人类可读中文（闸门 400 文案 / 日志共用）。

    首句固定为用户规定的标准文案，随后逐条点名（最多 max_items 条）。
    """
    head = "目录结构不合规，请先修复目录"
    if not violations:
        return head
    parts = []
    for v in violations[:max_items]:
        loc = " > ".join(p for p in (*v.path[-2:], v.title) if p)
        parts.append(f"{loc or v.title}：{v.detail}")
    more = f"（等共 {len(violations)} 处）" if len(violations) > max_items else ""
    return f"{head}（{'；'.join(parts)}{more}）"


# ---------------------------------------------------------------------------
# 树形 outline JSON 校验（normalize_outline 之后的节点形态：title/children）
# ---------------------------------------------------------------------------

def validate_outline_nodes(nodes, *, max_violations: int = 200) -> list[OutlineViolation]:
    """校验目录树 JSON（dict 节点 + children）是否符合层级硬性标准。

    层级按**树深度**推导（与 numbering.renumber_outline_nodes 的 level=id 段数
    同源），不信任节点自带 level 字段。非字典节点跳过（结构合法性由
    sse_handlers._validate_outline 负责，本模块只管硬性标准）。

    ⚠️ 索引口径：递归进入某一层时，``len(该层节点)`` 是**上一层父节点**的
    子节点数 —— 所以数量区间查 ``CHILDREN_MIN[level-1]``（父级层级），
    违规归属到父节点（path 末位）。顶层 L1 组的数量用户标准未约束，不判。

    违规判定：
      - L1/L2 父节点的子节点数不在 CHILDREN_MIN/MAX 区间（含 0 个子节点
        = TOO_FEW，叶子层级必须达到 L3；L3 作叶子合法）；
      - L3 下是否细分 L4 不作数量校验（R162）；
      - L4 节点不得再有 children（有即出现 L5 = DEPTH_EXCEEDED，
        其深层后代不再逐个上报，由该条统一承载）。
    """
    violations: list[OutlineViolation] = []

    def _walk(ns, level: int, path: list[str]) -> None:
        if len(violations) >= max_violations:
            return
        kids = [n for n in ns if isinstance(n, dict)]
        # 数量校验：本层节点个数 = 上一层父节点的子节点数（顶层 L1 组不判数量；
        # 仅约束 L1/L2 作父 —— L3 起为最小叶子，L4 兼容层不作数量校验）
        if 2 <= level <= LEAF_LEVEL:
            parent_title = path[-1] if path else ""
            n = len(kids)
            lo, hi = CHILDREN_MIN[level - 1], CHILDREN_MAX[level - 1]
            if n < lo:
                hint = "（该节点为叶子，但叶子层级必须达到三级）" if n == 0 else ""
                violations.append(OutlineViolation(
                    code=V_TOO_FEW_CHILDREN, level=level - 1,
                    title=parent_title,
                    detail=f"「{parent_title}」下仅 {n} 个{_LEVEL_NAMES[level]}节点，要求 {lo}~{hi} 个{hint}",
                    path=list(path[:-1]), expected=f"{lo}~{hi}", actual=str(n)))
            elif n > hi:
                violations.append(OutlineViolation(
                    code=V_TOO_MANY_CHILDREN, level=level - 1,
                    title=parent_title,
                    detail=f"「{parent_title}」下多达 {n} 个{_LEVEL_NAMES[level]}节点，要求 {lo}~{hi} 个",
                    path=list(path[:-1]), expected=f"{lo}~{hi}", actual=str(n)))
        for node in kids:
            title = str(node.get("title") or "").strip()
            children = node.get("children")
            child_list = children if isinstance(children, list) else []
            if level >= OUTLINE_MAX_LEVEL:
                if child_list:
                    violations.append(OutlineViolation(
                        code=V_DEPTH_EXCEEDED, level=level,
                        title=title,
                        detail=f"「{title}」是四级节点，不得再挂 {len(child_list)} 个子节点（出现第五级）",
                        path=list(path), expected="0", actual=str(len(child_list))))
                continue  # 深层后代不再逐个上报
            _walk(child_list, level + 1, [*path, title] if title else path)

    if isinstance(nodes, list):
        _walk(nodes, 1, [])
    return violations


def _first_title(ns) -> str:
    """取节点数组第一个合法节点的标题（作为违规定位的归属标题）。"""
    for n in ns:
        if isinstance(n, dict):
            return str(n.get("title") or "").strip()
    return ""


def is_compliant_outline(nodes) -> tuple[bool, list[OutlineViolation]]:
    """目录树 JSON 是否符合层级硬性标准。"""
    v = validate_outline_nodes(nodes)
    return (not v, v)


# ---------------------------------------------------------------------------
# DB 行校验（sections 表扁平行：id/parent_id/level/title + outline_json.id）
# ---------------------------------------------------------------------------

def validate_section_rows(rows) -> list[OutlineViolation]:
    """校验 sections 表行集合是否符合层级硬性标准。

    rows：每行至少含 id / parent_id / level / title（可含 outline_json，
    形如 {"id": "1.2.3", ...} 的 JSON 文本，用于子承父系编号校验）。

    fail-soft 约定（与导出侧装配同口径）：
      - parent_id 指向不存在的行（孤儿）→ 按根节点处理，不另报违规；
      - level 缺失/非法 → 按 1 处理；
      - outline_json 不可解析或父链任一侧缺编号 → 跳过编号校验（不做宽松误报）。

    额外校验（树形 JSON 没有的面）：落库点分编号的子承父系——
    子节点 outline_json.id 必须等于「父节点 id + "." + 同级序号」。
    数量校验仅约束 L1/L2 作父（R162：L3 为最小叶子，L4 兼容层不判数量）；
    编号子承父系校验保持对 L1~L3 全部父节点生效（含 L3 下的 L4 兼容行）。
    """
    violations: list[OutlineViolation] = []
    if not isinstance(rows, list):
        return violations
    items = [r for r in rows if isinstance(r, dict) and r.get("id")]
    by_id = {str(r["id"]): r for r in items}
    children_map: dict[str, list[dict]] = {}
    roots: list[dict] = []
    for r in items:
        pid = str(r.get("parent_id") or "")
        if pid and pid in by_id:
            children_map.setdefault(pid, []).append(r)
        else:
            roots.append(r)

    def _level_of(r: dict) -> int:
        try:
            lv = int(r.get("level") or 1)
        except (TypeError, ValueError):
            lv = 1
        return max(1, lv)

    def _stored_id(r: dict) -> str:
        raw = r.get("outline_json")
        if not raw:
            return ""
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return ""
        if not isinstance(data, dict):
            return ""
        sid = str(data.get("id") or "")
        return sid if re.fullmatch(r"[0-9]+(\.[0-9]+)*", sid) else ""

    # 数量/深度校验（按树结构走一遍；上限防病态树拖爆调用方）
    max_violations = 200

    def _walk(row: dict | None, rows_at: list[dict], path: list[str]) -> None:
        if len(violations) >= max_violations:
            return
        # rows_at 是「同一父下的兄弟行集合」；row 为 None 时表示根层
        if row is not None:
            lv = _level_of(row)
            if lv > OUTLINE_MAX_LEVEL:
                violations.append(OutlineViolation(
                    code=V_DEPTH_EXCEEDED, level=lv,
                    title=str(row.get("title") or "").strip(),
                    detail=f"存在 L{lv} 节点「{row.get('title')}」，超出四级上限",
                    path=list(path), expected=f"L1~L{OUTLINE_MAX_LEVEL}", actual=f"L{lv}"))
                return  # 其后代并入该条，不重复上报
            if lv < OUTLINE_MAX_LEVEL:
                kids = children_map.get(str(row["id"]), [])
                n = len(kids)
                if lv < LEAF_LEVEL:
                    # 数量校验仅约束 L1/L2 作父（L3 为最小叶子，L4 兼容层不判数量）
                    lo, hi = CHILDREN_MIN.get(lv, 0), CHILDREN_MAX.get(lv, 0)
                    if n < lo:
                        hint = "（叶子层级必须达到三级）" if n == 0 else ""
                        violations.append(OutlineViolation(
                            code=V_TOO_FEW_CHILDREN, level=lv,
                            title=str(row.get("title") or "").strip(),
                            detail=f"{_LEVEL_NAMES.get(lv, lv)}节点「{row.get('title')}」下仅 {n} 个子节点，要求 {lo}~{hi} 个{hint}",
                            path=list(path), expected=f"{lo}~{hi}", actual=str(n)))
                    elif n > hi:
                        violations.append(OutlineViolation(
                            code=V_TOO_MANY_CHILDREN, level=lv,
                            title=str(row.get("title") or "").strip(),
                            detail=f"{_LEVEL_NAMES.get(lv, lv)}节点「{row.get('title')}」下多达 {n} 个子节点，要求 {lo}~{hi} 个",
                            path=list(path), expected=f"{lo}~{hi}", actual=str(n)))
                # 子承父系编号校验：每个孩子的点分编号 = 父编号.序号
                # （对 L1~L3 全部父节点生效，含 L3 下的 L4 兼容行）
                parent_sid = _stored_id(row)
                if parent_sid:
                    for idx, kid in enumerate(kids, start=1):
                        kid_sid = _stored_id(kid)
                        if kid_sid and kid_sid != f"{parent_sid}.{idx}":
                            violations.append(OutlineViolation(
                                code=V_NUMBERING_MISMATCH, level=lv + 1,
                                title=str(kid.get("title") or "").strip(),
                                detail=f"子节点编号 {kid_sid} 未承父编号 {parent_sid}（应为 {parent_sid}.{idx}）",
                                path=[*path, str(row.get("title") or "")],
                                expected=f"{parent_sid}.{idx}", actual=kid_sid))
            new_path = [*path, str(row.get("title") or "")]
        else:
            new_path = list(path)

        if row is None:
            for r in rows_at:
                _walk(r, [r], new_path)
            return
        kids = children_map.get(str(row["id"]), [])
        lv = _level_of(row)
        if lv <= OUTLINE_MAX_LEVEL:
            for kid in kids:
                _walk(kid, kids, new_path)

    _walk(None, roots, [])
    return violations


def is_compliant_sections(rows) -> tuple[bool, list[OutlineViolation]]:
    """sections 表行集合是否符合层级硬性标准（正文生成前置闸门用）。"""
    v = validate_section_rows(rows)
    return (not v, v)


# ---------------------------------------------------------------------------
# 防御性清洗：L5+ 行合并至父 L4（用户指令「将现有 L5 合并至 L4」）
# ---------------------------------------------------------------------------

def merge_section_rows_deeper_than_leaf(rows) -> tuple[list[dict], dict]:
    """把 level ≥ 5 的章节行合并进其父级（逐层向上，最终并入最近的 L4 祖先）。

    合并规则（保数据不丢）：
      - 被合并行的标题以「**标题**」加粗行、正文以原文追加到父行 content 尾部
        （父 content 与追加块之间空一行；被合并行无正文时只追加标题行）；
      - 多层深（L6 挂在 L5 下）先自底向上逐层合并，最终全部落到 L4；
      - 被合并行从返回行集中删除；父行 level/parent_id 不变。

    返回 (新行列表, stats)；stats = {"merged": 删除行数, "touched": 被改写的父行 id 集}。
    纯函数：不修改传入对象（内部深拷贝）。
    """
    import copy as _copy

    stats = {"merged": 0, "touched": []}
    if not isinstance(rows, list):
        return rows, stats
    items = [_copy.deepcopy(r) for r in rows if isinstance(r, dict) and r.get("id")]
    by_id = {str(r["id"]): r for r in items}

    def _level_of(r: dict) -> int:
        try:
            return max(1, int(r.get("level") or 1))
        except (TypeError, ValueError):
            return 1

    # 自底向上：按层级从深到浅处理，确保 L6 先并入 L5、L5 再并入 L4
    max_lv = max((_level_of(r) for r in items), default=1)
    for lv in range(max_lv, OUTLINE_MAX_LEVEL, -1):
        layer = [r for r in items if _level_of(r) == lv]
        for row in layer:
            pid = str(row.get("parent_id") or "")
            parent = by_id.get(pid)
            if parent is None or _level_of(parent) != lv - 1:
                # 父缺失/层级断裂的孤儿深行：无处可并，直接删除（与「严禁 L5」一致）
                items.remove(row)
                by_id.pop(str(row["id"]), None)
                stats["merged"] += 1
                continue
            block = str(row.get("title") or "").strip()
            content = str(row.get("content") or "").strip()
            appended = f"**{block}**" if block else ""
            if content:
                appended = f"{appended}\n{content}" if appended else content
            if appended:
                base = str(parent.get("content") or "").rstrip()
                parent["content"] = f"{base}\n\n{appended}" if base else appended
                if str(parent["id"]) not in stats["touched"]:
                    stats["touched"].append(str(parent["id"]))
            items.remove(row)
            by_id.pop(str(row["id"]), None)
            stats["merged"] += 1
    return items, stats
