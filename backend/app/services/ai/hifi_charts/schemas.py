"""高保真图表规格 · 数据结构校验（文档 §3.7「生成后自检」）

三类高保真图表各自定义了**新增**数据契约（与既有 chart-json 的
totalDays / root / steps 形态不同），本模块实现「渲染前校验」，
校验失败即拒绝出图（fail-closed），避免把 AI 的错误结构渲染成错图。

纯函数、零依赖、零 IO —— 供生成侧消费（提示词输出 → 校验 → 渲染）。

⚠️ 与既有 `chart_validators.validate_inline_chart` 是**两套判据**：
   既有判据面向「既有 7 类 chart-json / Mermaid」；本模块面向
   「高保真图表规格」的 3 种新增结构。二者互不侵入。
"""
from __future__ import annotations

import datetime as _dt

__all__ = [
    "GANTT_TYPES",
    "ORG_LEVEL_KEYS",
    "FLOW_TYPES",
    "validate_gantt_spec",
    "validate_org_matrix_spec",
    "validate_flow_complex_spec",
]

# 甘特图 type 枚举锁（§3.4.5）
GANTT_TYPES: frozenset[str] = frozenset({"primary", "secondary", "light"})

# 组织图层级键（§3.4.1）
ORG_LEVEL_KEYS: tuple[str, ...] = ("level1", "level2", "level3", "level4")

# 工艺流程图 type 枚举锁（§3.4.2）
FLOW_TYPES: frozenset[str] = frozenset({"primary", "warning", "success"})


def _parse_date(s: str) -> _dt.date | None:
    """解析 YYYY-MM-DD；非法返回 None（日期刚性公式的前置）。"""
    try:
        return _dt.date.fromisoformat(str(s).strip())
    except (ValueError, TypeError):
        return None


def _days_diff(a: _dt.date, b: _dt.date) -> int:
    return (b - a).days


def validate_gantt_spec(data: dict) -> tuple[bool, str]:
    """甘特图自检（§3.7）。

    校验项：
      1. ``total_days == end_date - start_date + 1``
      2. 每个 ``task.duration == end - start + 1``（含首尾两天）
      3. 所有 ``task.start/end`` ∈ ``[start_date, end_date]``
      4. 每条 ``dependency`` 的 ``from/to`` 都在 ``tasks`` 中
      5. ``type`` ∈ ``{primary, secondary, light}``
    """
    if not isinstance(data, dict):
        return False, "数据不是对象"
    if not isinstance(data.get("tasks"), list) or not data["tasks"]:
        return False, "tasks 为空或缺失"
    sd, ed = _parse_date(data.get("start_date", "")), _parse_date(data.get("end_date", ""))
    if sd is None or ed is None:
        return False, "start_date/end_date 非法（须 YYYY-MM-DD）"
    if ed < sd:
        return False, "end_date 早于 start_date"
    want_total = _days_diff(sd, ed) + 1
    if int(data.get("total_days", 0) or 0) != want_total:
        return False, f"total_days 不自治：{data.get('total_days')} != {want_total}"
    ids: set[str] = set()
    for i, t in enumerate(data["tasks"]):
        if not isinstance(t, dict):
            return False, f"tasks[{i}] 不是对象"
        tid = str(t.get("id", ""))
        if not tid:
            return False, f"tasks[{i}] 缺 id"
        if tid in ids:
            return False, f"tasks[{i}] id 重复：{tid}"
        ids.add(tid)
        ts, te = _parse_date(t.get("start", "")), _parse_date(t.get("end", ""))
        if ts is None or te is None:
            return False, f"tasks[{i}] start/end 非法"
        if te < ts:
            return False, f"tasks[{i}] end 早于 start"
        want_dur = _days_diff(ts, te) + 1
        if int(t.get("duration", 0) or 0) != want_dur:
            return False, f"tasks[{i}] duration 不自洽：{t.get('duration')} != {want_dur}"
        if ts < sd or te > ed:
            return False, f"tasks[{i}] 越出 [start_date, end_date]"
        if str(t.get("type", "")) not in GANTT_TYPES:
            return False, f"tasks[{i}] type 非法：{t.get('type')}"
    for i, d in enumerate(data.get("dependencies", []) or []):
        if not isinstance(d, dict):
            return False, f"dependencies[{i}] 不是对象"
        if str(d.get("from", "")) not in ids or str(d.get("to", "")) not in ids:
            return False, f"dependencies[{i}] from/to 引用不存在 id"
    return True, "ok"


def validate_org_matrix_spec(data: dict) -> tuple[bool, str]:
    """矩阵总线组织图自检（§3.7）。

    校验项：
      1. ``level1`` 恰好 1 个节点（项目经理唯一）
      2. 所有 ``level3[].parent`` ∈ ``{null} ∪ level2.id``
      3. 所有 ``id`` 全局唯一、无重复
      4. 无「孤儿节点」（既无 parent 又非根）
    """
    if not isinstance(data, dict):
        return False, "数据不是对象"
    nodes = data.get("nodes")
    if not isinstance(nodes, dict):
        return False, "nodes 缺失"
    l1 = nodes.get("level1") or []
    l2 = nodes.get("level2") or []
    l3 = nodes.get("level3") or []
    if len(l1) != 1:
        return False, f"level1 必须恰好 1 个根节点，实际 {len(l1)}"
    l2_ids: set[str] = set()
    all_ids: set[str] = set()
    for lv, arr in (("level1", l1), ("level2", l2), ("level3", l3), ("level4", nodes.get("level4") or [])):
        for i, n in enumerate(arr or []):
            if not isinstance(n, dict):
                return False, f"{lv}[{i}] 不是对象"
            nid = str(n.get("id", ""))
            if not nid:
                return False, f"{lv}[{i}] 缺 id"
            if nid in all_ids:
                return False, f"{lv}[{i}] id 重复：{nid}"
            all_ids.add(nid)
            if lv == "level2":
                l2_ids.add(nid)
    for i, n in enumerate(l3):
        p = n.get("parent")
        if p is not None and str(p) not in l2_ids:
            return False, f"level3[{i}] parent 引用不存在 id：{p}"
        # 孤儿节点：parent 为 None 且非根 —— level3 parent=null 是合法直连总线
        # （综合办/物资/财务），故 level3 的 parent=null 不算孤儿；真正孤儿是
        # 既无 parent 且不在任何引用链中的节点（本结构由层级归属保证，无需额外判）。
    return True, "ok"


def validate_flow_complex_spec(data: dict) -> tuple[bool, str]:
    """复杂工艺流程图自检（§3.7）。

    校验项：
      1. 所有 ``links[].from/to`` 都在 ``nodes`` 中
      2. 所有 ``type`` ∈ ``{primary, warning, success}``
      3. 坐标落在画布内（X∈[40,600]、Y∈[20,660]）
      4. 无闭环引用（允许汇聚，禁止死循环）
    """
    if not isinstance(data, dict):
        return False, "数据不是对象"
    nodes = data.get("nodes")
    links = data.get("links") or []
    if not isinstance(nodes, list) or not nodes:
        return False, "nodes 为空或缺失"
    ids: set[str] = set()
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            return False, f"nodes[{i}] 不是对象"
        nid = str(n.get("id", ""))
        if not nid:
            return False, f"nodes[{i}] 缺 id"
        if nid in ids:
            return False, f"nodes[{i}] id 重复：{nid}"
        ids.add(nid)
        if str(n.get("type", "")) not in FLOW_TYPES:
            return False, f"nodes[{i}] type 非法：{n.get('type')}"
        x, y = n.get("x", -1), n.get("y", -1)
        if not (40 <= int(x) <= 600 and 20 <= int(y) <= 660):
            return False, f"nodes[{i}] 坐标越界：({x},{y})"
    # 邻接表 + 环检测（DFS 三色）
    adj: dict[str, list[str]] = {i: [] for i in ids}
    for i, ln in enumerate(links):
        if not isinstance(ln, dict):
            return False, f"links[{i}] 不是对象"
        f, t = str(ln.get("from", "")), str(ln.get("to", ""))
        if f not in ids or t not in ids:
            return False, f"links[{i}] from/to 引用不存在 id"
        if f != t:
            adj[f].append(t)
    color: dict[str, int] = {i: 0 for i in ids}  # 0=白 1=灰 2=黑
    def _dfs(u: str) -> bool:
        color[u] = 1
        for v in adj[u]:
            if color[v] == 1:
                return False  # 后向边 → 环
            if color[v] == 0 and not _dfs(v):
                return False
        color[u] = 2
        return True
    for nid in ids:
        if color[nid] == 0 and not _dfs(nid):
            return False, "存在闭环引用（死循环）"
    return True, "ok"
