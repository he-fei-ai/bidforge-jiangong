#!/usr/bin/env python3
import asyncio


"""图表数据校验模块 - 遵循 v3.1 规范

实现各类图表的结构验证规则，确保生成内容符合工程标准。
"""

import json
import logging
import re
from typing import Any


logger = logging.getLogger(__name__)


def _flatten_with_level(node: dict, level: int = 0) -> list[dict]:
    """递归展平树结构，返回带层级信息的节点列表"""
    result = []
    label = node.get("label") or node.get("name", "")
    if label:
        result.append({"label": str(label), "level": level})
    children = node.get("children", []) or []
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                result.extend(_flatten_with_level(child, level + 1))
    return result


def calculate_tree_depth(node: dict, level: int = 0) -> int:
    """计算树的最大深度"""
    if not isinstance(node, dict):
        return level
    children = node.get("children", []) or []
    if not children:
        return level
    max_d = level
    for child in children:
        if isinstance(child, dict):
            max_d = max(max_d, calculate_tree_depth(child, level + 1))
    return max_d


def count_all_nodes(node: dict) -> int:
    """统计节点总数（含根节点）"""
    if not isinstance(node, dict):
        return 0
    count = 1  # 当前节点
    children = node.get("children", []) or []
    for child in children:
        if isinstance(child, dict):
            count += count_all_nodes(child)
    return count


def _coerce_arch_children(node: dict, _seen: set | None = None,
                          _depth: int = 0) -> dict:
    """把架构树节点的子节点列表规范为统一的 ``children`` 列表（元素均为 dict）。

    兼容三类弱模型/历史产出（此前渲染器与校验器口径不一致，导致静默丢节点）：
      · ``children`` 元素是字符串/数字（``{"children": ["技术部","安全部"]}``）——
        旧实现的展平/计数/布局循环都 ``continue`` 跳过非 dict，架构图最终只剩根节点，
        且校验器把节点数当 1 仍判合法（"能过校验、却画不出东西"）；
      · 子节点列表使用别名键 ``nodes`` / ``sub_units`` / ``branches``
        （渲染器认识、校验器不认识，出现"能画但不能计数/校验"）；
      · ``None`` 占位元素。

    返回**新** dict（不原地修改调用方数据），保证"校验"与"渲染"看到同一棵树。

    ✅ 环保护：以对象身份（``id``）记录**祖先链**，子节点引用了祖先时直接剪除
    （与渲染器 `_layout_architecture_tree` 的判重口径一致）。归一后的树必然无环，
    因此下游 `count_all_nodes` / `calculate_tree_depth` / 布局递归都不会爆栈。
    """
    if _seen is None:
        _seen = set()
    if id(node) in _seen or _depth > 30:
        return node
    _seen.add(id(node))
    children = None
    for key in ("children", "nodes", "sub_units", "branches"):
        v = node.get(key)
        if isinstance(v, list) and v:
            children = v
            break
    if children is None:
        return node
    new_children: list[dict] = []
    for child in children:
        if isinstance(child, dict):
            if id(child) in _seen:
                continue  # 环：子节点引用了祖先，剪除
            new_children.append(_coerce_arch_children(child, _seen, _depth + 1))
        elif child is None:
            continue
        else:
            s = str(child).strip()
            if s:
                new_children.append({"label": s})
    return {**node, "children": new_children}


def normalize_architecture_tree(data: Any) -> dict | None:
    """把各种 architecture 载荷形态归一为「带标签的根节点树」。

    支持的历史形态：
      · ``{"label": "项目部", "children": [...]}``                —— 规范树
      · ``{"root": {...}}`` / ``{"tree": {...}}`` / ``{"data": {...}}`` —— 嵌套包装
      · ``{"type":"architecture","root":"项目部","nodes":[...]}`` —— 字符串根 + 节点列表

    ✅ 为何必须共享：渲染器（mermaid_renderer → mermaid_architecture）与校验器
    （validate_architecture_tree）此前**各写一套容错规则**：渲染侧认识"字符串根 +
    nodes 列表"，校验侧却只认顶层 label/name。结果是"渲染器明明能画，校验器判为
    非法" → _chart_pipeline 的"校验失败 → 删块"策略把正文里的架构图数据整块删掉。
    归一规则收敛到本函数后，两侧容错度必然一致。

    ✅ 同时收敛子节点形态（``_coerce_arch_children``）：字符串子节点、别名子节点键
    统一为 dict 形式的 ``children``，避免"校验通过但渲染只剩根节点"。

    Returns:
        归一后的根节点 dict；无法识别时返回 None。
    """
    if not isinstance(data, dict):
        return None
    root = data
    if not (data.get("label") or data.get("name") or data.get("children")):
        for key in ("root", "tree", "data", "node"):
            nested = data.get(key)
            if isinstance(nested, dict) and (
                nested.get("label") or nested.get("name") or nested.get("children")
            ):
                root = nested
                break
        if not (root.get("label") or root.get("name") or root.get("children")):
            nodes_list = data.get("nodes")
            if isinstance(nodes_list, list) and nodes_list:
                root = {
                    "label": str(data.get("root") or "组织架构图"),
                    "children": nodes_list,
                }
    if not (root.get("label") or root.get("name") or root.get("children")):
        return None
    return _coerce_arch_children(root)


# ============================================================================
# 流程图（施工流程图 / 工艺流程图，横向 chart-json 载荷）
# 2026-09-18 横向流程图样张改版：与 architecture 同思路——校验侧与渲染侧共用
# 同一套归一规则，避免"渲染器能画、校验器判非法 → 删块"。
# ============================================================================

_FLOW_VARIANTS = {"start", "end", "process", "decision", "highlight"}


def normalize_flowchart_data(data: Any) -> dict | None:
    """把 flowchart 载荷归一为 ``{"nodes", "edges", "direction", "title"}``。

    兼容形态：
      · ``{"type":"flowchart","steps":[{"id","label","variant"}],"edges":[{"from","to","label"}]}``
        —— 施工/工艺横向流程图规范载荷（direction 缺省 LR）
      · ``{"nodes":[...], "edges":[...]}`` —— 历史 JSON 形态（direction 缺省 TD）
      · nodes 元素可为字符串 / 数字 / 对象；edges 元素可为 dict / [from,to] / "A --> B"
      · 容器别名：steps≡nodes、links≡edges、variant≡type、name≡label

    Returns:
        归一后的 dict；无有效节点时返回 None。
    """
    if not isinstance(data, dict):
        return None
    nodes_raw = data.get("nodes")
    if not (isinstance(nodes_raw, list) and nodes_raw):
        nodes_raw = data.get("steps")
    if not isinstance(nodes_raw, list) or not nodes_raw:
        return None

    def _norm_variant(v: Any) -> str:
        s = str(v or "").strip().lower()
        return s if s in _FLOW_VARIANTS else "process"

    nodes: list[dict] = []
    seen_ids: set[str] = set()
    idx_to_id: dict[str, str] = {}
    for i, node in enumerate(nodes_raw):
        if isinstance(node, dict):
            nid = str(node.get("id") or f"N{i + 1}")
            suffix = 1
            while nid in seen_ids:  # id 撞车：与架构图 _unique_id 同口径
                suffix += 1
                nid = f"{nid}#{suffix}"
            seen_ids.add(nid)
            idx_to_id[str(i)] = nid
            nodes.append({
                "id": nid,
                "label": str(node.get("label") or node.get("name") or nid),
                "type": _norm_variant(node.get("variant") or node.get("type")),
            })
        else:
            nid = f"N{i + 1}"
            seen_ids.add(nid)
            idx_to_id[str(i)] = nid
            nodes.append({"id": nid, "label": str(node).strip(), "type": "process"})
    if len(nodes) < 2:
        return None

    edges_raw = data.get("edges")
    if not (isinstance(edges_raw, list) and edges_raw):
        edges_raw = data.get("links")
    edges: list[dict] = []
    for edge in (edges_raw or []):
        if isinstance(edge, dict):
            f = edge.get("from", edge.get("from_"))
            t = edge.get("to")
            label = str(edge.get("label") or "").strip()
        elif isinstance(edge, (list, tuple)) and len(edge) >= 2:
            f, t, label = edge[0], edge[1], ""
        elif isinstance(edge, str) and "-->" in edge:
            parts = edge.split("-->", 1)
            f = parts[0].strip().rstrip(";").strip()
            t_rest = parts[1].strip()
            label = ""
            if t_rest.startswith("|") and "|" in t_rest:
                label = t_rest[1:t_rest.index("|")]
                t_rest = t_rest[t_rest.index("|") + 1:]
            f, t = f, t_rest.rstrip(";").strip()
        else:
            continue
        if f in (None, "") or t in (None, ""):
            continue
        # 端点解析：精确 id → 数字下标 → 丢弃（渲染器同样会跳过未知端点）
        def _resolve(v: Any) -> str | None:
            s = str(v)
            if s in seen_ids:
                return s
            return idx_to_id.get(s)

        f_id, t_id = _resolve(f), _resolve(t)
        if not f_id or not t_id:
            continue
        edges.append({"from": f_id, "to": t_id, "label": label})

    direction = str(data.get("direction") or "").upper()
    if direction == "TB":
        direction = "TD"
    if direction not in ("TD", "LR", "RL", "BT"):
        # steps 形态（施工/工艺横向流程图）缺省横向；历史 nodes 形态保持纵向
        direction = "LR" if ("steps" in data and "direction" not in data) else "TD"

    return {
        "nodes": nodes,
        "edges": edges,
        "direction": direction,
        "title": str(data.get("title") or ""),
    }


def validate_flowchart_data(data: Any, subtype: str = None) -> tuple[bool, list[str]]:
    """验证流程图数据结构（宽进口径：宁可放行给渲染器，也不误删正文图表）。

    规则：
    - steps/nodes 至少 2 个节点、≥1 条有效边
    - 边端点必须能解析到已注册节点（数字下标视为合法引用）
    """
    errors: list[str] = []
    normalized = normalize_flowchart_data(data)
    if normalized is None:
        errors.append("缺少有效的 steps/nodes 节点列表（至少 2 个节点）")
        return False, errors
    if not normalized["edges"]:
        errors.append("缺少有效的 edges 连线（至少 1 条，端点须引用已有节点 id 或下标）")
        return False, errors
    return True, errors


def validate_architecture_tree(data: Any, subtype: str = None) -> tuple[bool, list[str]]:
    """
    验证架构图数据结构完整性（v3.1 规范）。

    验证规则：
    - 根节点有且仅有 1 个
    - 同级节点标签不得完全重复
    - 深度不得超过 5 层
    - 节点数量在 subtype 规定的区间内

    Args:
        data: 架构树数据（dict 或 JSON 字符串）
        subtype: 子类型 organization/wbs/system/infrastructure（可选）

    Returns:
        (is_valid, error_messages) 元组
    """
    errors = []

    # 解析数据
    try:
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            errors.append("架构图数据必须是字典格式")
            return False, errors
    except (json.JSONDecodeError, TypeError) as e:
        errors.append(f"数据解析失败: {e!s}")
        return False, errors

    # 检查根节点存在且有标签
    # ✅ 修复：先按渲染器同款规则归一（兼容 {root:{...}} / {root:"名称", nodes:[...]}），
    #    避免"渲染器能画、校验器判非法"导致正文图表被删块。
    data = normalize_architecture_tree(data)
    if data is None:
        errors.append("缺少根节点标签（label 或 name 字段）")
        return False, errors

    # 深度检查（≤6层）
    # ✅ 修复（BUG-4）：渲染器（mermaid_architecture.py）对深度不设硬上限、照画不误，
    #    旧校验 limit≤5 会拒绝 6 层合法树；且提示词（charts.py）仅要求"≥4 节点"，
    #    旧区间下限 6 会拒绝 AI 产出的 4~5 节点小树 —— 二者矛盾导致能画的图被删块。
    #    放宽到 6 层，与渲染能力对齐（过深树在排版上本身已不可读，6 层足够覆盖实际场景）。
    depth = calculate_tree_depth(data)
    if depth > 6:
        errors.append(f"架构深度超过6层限制: {depth}层")
        return False, errors

    # 同级节点标签唯一性检查（按层级分组）
    # BUG-FIX-31 修复：原 labels_seen 是全局集合不区分层级，
    # 不同层级相同标签（如各分部同岗位"项目经理"）被误报为重复。
    #
    # ✅ 口径对齐（2026-09-17）：同级重复标签由**致命错误降级为告警**。
    #    渲染器（mermaid_architecture._render_architecture_image_v2）对同级同名
    #    节点照画不误，而校验器判非法时 `_chart_pipeline` 会把**整张架构图**从
    #    正文删除（宁缺勿滥策略）—— 用一整张组织体系图去换一处命名重复，代价
    #    明显不对等。证据：`_diagnostics/_chart_shape_audit.py` 的「同级重名」
    #    形态在修复前为 ❗删块型错配（校验=拒绝 / 渲染=出图）。
    #    重复命名仍由 `chart_json_fix` 提示词要求 AI 修正，只是不再删块。
    labels_by_level: dict[int, set[str]] = {}
    for node_info in _flatten_with_level(data):
        level = node_info["level"]
        label = node_info["label"]
        if level not in labels_by_level:
            labels_by_level[level] = set()
        if label in labels_by_level[level]:
            logger.warning(
                "架构图同级重复标签: '%s'（层级 %s）—— 保留出图，建议后续修正",
                label, level)
        labels_by_level[level].add(label)

    # 节点数量区间检查（根据subtype）
    total_nodes = count_all_nodes(data)
    expected_ranges = {
        "organization": (4, 24),
        "wbs": (4, 24),
        "system": (4, 24),
        "infrastructure": (4, 24),
    }
    if subtype and subtype in expected_ranges:
        min_n, max_n = expected_ranges[subtype]
        if total_nodes < min_n:
            errors.append(f"{subtype}类型最少需{min_n}个节点，实际{total_nodes}")
        elif total_nodes > max_n:
            errors.append(f"{subtype}类型最多{max_n}个节点，实际{total_nodes}")

    return len(errors) == 0, errors


def detect_architecture_subtype(data: dict, section_title: str = None) -> str | None:
    """
    根据架构图数据内容和章节标题，检测子类型（v3.1规范）。

    子类型分类：
    - "organization": 组织机构类（含"组织"、"部门"、"岗位"等）
    - "wbs": 工作分解结构（含"WBS"、"工作包"、"工作分解"等）
    - "system": 系统架构（含"系统"、"模块"、"软件"、"平台"等）
    - "infrastructure": 基础设施/网络拓扑（含"网络"、"服务器"、"设备"等）

    Returns:
        子类型字符串或 None（无法确定）
    """
    # 收集关键词（从数据和标题中提取）
    keywords = []

    # 从树数据中提取标签关键词
    def extract_labels(node: dict):
        if not isinstance(node, dict):
            return
        label = node.get("label") or node.get("name", "")
        if label:
            keywords.append(str(label).lower())
        children = node.get("children", []) or []
        for child in children:
            extract_labels(child)

    try:
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, dict):
            extract_labels(data)
    except Exception as _e:
        pass

    # 添加章节标题关键词
    if section_title:
        keywords.extend([s.strip() for s in re.split(r"[，。、！？\s]+", section_title) if s])

    keywords_lower = [k.lower().strip() for k in keywords if k.strip()]

    # 定义各子类型的关键词模式
    patterns = {
        "organization": [
            r"组织(构)?",
            r"部门",
            r"岗(位)?",
            r"机构",
            r"管理处",
            r"项目部",
            r"指挥部",
            r"负责人",
            r"总工",
            r"经理",
            r"主任",
            r"主管",
            r"管理层",
            r"五方责任",
            r"职责",
            r"职能",
            r"汇报",
            r"矩阵管理",
            r"项目制",
            r"职能制",
            r"岗位说明书",
            r"权责矩阵",
        ],
        "wbs": [
            r"wbs",
            r"工作分解",
            r"工作包",
            r"工作包.",
            r"任务分解",
            r"分解结构",
            r"任务清单",
            r"工序",
            r"作业",
            r"单项工程",
            r"分部工程",
            r"分项工程",
        ],
        "system": [
            r"系统(架构)?",
            r"模块构成",
            r"模块",
            r"软件(架构)?",
            r"平台(架构)?",
            r"子系统",
            r"系统拓扑",
            r"网络拓扑",
            r"接口",
            r"数据库",
            r"服务",
            r"体系",
            r"框架",
            r"架构",
            r"组件",
            r"中间件",
            r"应用层",
            r"数据层",
        ],
        "infrastructure": [
            r"网络拓?扑",
            r"设备构成",
            r"设备",
            r"服务器",
            r"主机",
            r"交换机",
            r"路由器",
            r"存储",
            r"布线",
            r"机房",
            r"物理架构",
            r"硬件架构",
            r"基础设施",
            r"部署架构",
            r"站点",
            r"节点",
            r"终端",
            r"控制器",
        ],
    }

    # 计算匹配分数
    scores = {"organization": 0, "wbs": 0, "system": 0, "infrastructure": 0}
    for sub_type, pattern_list in patterns.items():
        for pattern in pattern_list:
            try:
                if any(re.search(pattern, kw) for kw in keywords_lower):
                    scores[sub_type] += 1
            except re.error:
                pass

    # 获取最高分的子类型
    best_subtype = max(scores, key=scores.get)
    if scores[best_subtype] > 0:
        return best_subtype

    return None


# ============================================================
# 载荷归一器（校验侧与渲染侧共用同一套容错规则）
# ============================================================
# ✅ 为什么必须共用同一套规则：
#    校验器与渲染器对同一载荷的容错度一旦不一致，就会出现两种静默故障——
#      · 渲染器能画、校验器判非法 → 正文里的图表**被整块删除**；
#      · 校验通过、渲染器返回 None → 导出只剩红字占位。
#    因此约定：validate_* 与 _render_* 都先过同一个 normalize_*；
#    归一返回 None 即双方一致判「不可用」，交给删块/占位链路处理。
#
# ✅ 归一结果的形态约定：**只含该图表渲染所必需的核心字段**。
#    调用方一律用 `{**原载荷, **归一结果}` 合并，title / totalDays / legend
#    等展示字段仍从原载荷保留（见 mermaid_gantt/labor/layout 的调用点）。

_LABOR_AXIS_KEYS = ("phases", "stages", "time", "categories", "types")

# 阶段对象里「不是工种计数」的键：平铺形态下必须跳过，
# 否则 {"name":"基础","workers":[...]} 会把 name 当成一个工种。
_LABOR_PHASE_META_KEYS = frozenset({
    "name", "label", "title", "workers", "trades", "items",
    "desc", "description", "remark", "note", "phase", "stage", "id",
})

# 时间轴 status 别名 → 规范值（渲染器与校验器只认规范值）
_TIMELINE_STATUS_ALIASES = {
    "completed": "completed", "complete": "completed", "done": "completed",
    "finished": "completed", "已完成": "completed", "完成": "completed",
    "in_progress": "in_progress", "inprogress": "in_progress", "doing": "in_progress",
    "ongoing": "in_progress", "进行中": "in_progress", "实施中": "in_progress",
    "pending": "pending", "todo": "pending", "not_started": "pending",
    "未开始": "pending", "待开始": "pending", "计划中": "pending",
}


def _to_float(value: Any, default: float | None = None) -> float | None:
    """宽松数值转换：非法 / NaN / Inf 一律返回 default，绝不抛异常。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def _as_str_list(value: Any) -> list[str]:
    """把「分隔字符串 / 标量序列 / 对象列表」统一为 str 列表。

    AI 产出的 phases/categories 有大量别名键，且常把数组写成
    `"基础,主体"` / `"木工、钢筋工"` 这种分隔字符串（中英文标点混用）。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[,，、;；|\n]+", value) if p.strip()]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for v in value:
            if isinstance(v, dict):
                t = (v.get("name") or v.get("label") or v.get("title")
                     or v.get("trade") or v.get("type"))
                if t is not None and str(t).strip():
                    out.append(str(t).strip())
            elif v is not None and str(v).strip():
                out.append(str(v).strip())
        return out
    return []


def _align_labor_axes(phases: list, categories: list,
                      matrix: list) -> dict | None:
    """对齐 labor 三件套的轴长度：**一律以数据为准**，绝不丢数据行/列。

    AI 常写出「阶段名数量 ≠ 数据行数」「工种数 ≠ 矩阵列数」。旧实现直接按
    `phases[i]` 取名字（越界 IndexError），或让渲染器截断多余行（数据静默丢失）。
    """
    rows = [[_to_float(v, 0.0) or 0.0 for v in r]
            for r in matrix if isinstance(r, (list, tuple)) and r]
    if not rows:
        return None

    # 列宽以最宽行为准，缺列补 0
    width = max(len(r) for r in rows)
    for r in rows:
        if len(r) < width:
            r.extend([0.0] * (width - len(r)))

    # 工种名：不足补造、多余裁剪（以矩阵列数为准）
    cats = list(categories)
    for i in range(len(cats), width):
        cats.append(f"工种{i + 1}")
    cats = cats[:width]

    # 阶段名：不足补造、多余裁剪（以数据行数为准）
    names = list(phases)
    for i in range(len(names), len(rows)):
        names.append(f"阶段{i + 1}")
    names = names[:len(rows)]

    return {"phases": names, "categories": cats, "data": rows}


def normalize_labor_data(data: Any) -> dict | None:
    """把 AI 产出的各种「自然形态」劳动力载荷归一为 phases/categories/data 三件套。

    支持的形态（全部来自线上真实产出）：
      1. 三件套矩阵：`{"phases":[...],"categories":[...],"data":[[...]]}`
      2. 行内阶段名：`{"categories":["木工"],"data":[["基础",10]]}`
      3. 工种→序列映射：`{"data":{"木工":[10,20]}}`（转置：行=阶段、列=工种）
      4. 阶段→工种→值：`{"data":{"基础":{"木工":10}}}`
      5. 阶段×工种计数：`{"data":[{"phase":"基础","木工":10}]}`
      6. 工种清单（单阶段）：`{"trades":[{"name":"木工","peak":30}]}`
      7. 阶段对象内嵌工种：`{"phases":[{"name":"基础","workers":[{"trade":"木工","count":10}]}]}`
      8. 阶段对象平铺计数：`{"phases":[{"name":"基础","木工":10}]}`
      9. ECharts series / Chart.js labels+datasets
     10. labels+values 一维序列（单工种 × 各阶段）
     11. 别名键 time/types/stages + 分隔字符串
     12. 嵌套包装：`{"type":"labor","data":{...}}`

    返回 `{"phases": [...], "categories": [...], "data": [[float]]}`；
    无法识别时返回 None。
    """
    if not isinstance(data, dict) or not data:
        return None

    # ① 嵌套包装解包（递归一层，防自引用）
    inner = data.get("data")
    if isinstance(inner, dict) and any(k in inner for k in _LABOR_AXIS_KEYS):
        return normalize_labor_data(inner)

    phases = _as_str_list(data.get("phases") or data.get("stages") or data.get("time"))
    categories = _as_str_list(data.get("categories") or data.get("types"))
    rows = data.get("data")
    matrix: list = []

    # ② 阶段对象列表（内嵌 workers / 平铺工种计数）
    raw_phases = data.get("phases") or data.get("stages")
    if isinstance(raw_phases, list) and raw_phases and isinstance(raw_phases[0], dict):
        names: list = []
        for i, p in enumerate(raw_phases):
            if not isinstance(p, dict):
                continue
            row: dict = {}
            workers = p.get("workers") or p.get("trades") or p.get("items")
            if isinstance(workers, list) and workers:
                for w in workers:
                    if isinstance(w, dict):
                        wn = (w.get("trade") or w.get("name") or w.get("label")
                              or w.get("type"))
                        wv = w.get("count")
                        if wv is None:
                            wv = w.get("value")
                        if wv is None:
                            wv = w.get("peak")
                        fv = _to_float(wv, 0.0)
                    elif isinstance(w, (list, tuple)) and len(w) >= 2:
                        wn, fv = w[0], _to_float(w[1], 0.0)
                    else:
                        continue
                    if wn is None or not str(wn).strip():
                        continue
                    row[str(wn).strip()] = fv if fv is not None else 0.0
            else:
                for k, v in p.items():
                    if k in _LABOR_PHASE_META_KEYS:
                        continue
                    fv = _to_float(v)
                    if fv is not None:
                        row[str(k).strip()] = fv
            if not row:
                continue
            nm = p.get("name") or p.get("label") or p.get("title")
            names.append(str(nm).strip() if nm is not None and str(nm).strip()
                         else f"阶段{i + 1}")
            for k in row:
                if k not in categories:
                    categories.append(k)
            matrix.append([row.get(c, 0.0) for c in categories])
        if names and matrix and categories:
            return _align_labor_axes(names, categories, matrix)

    # ③ data 为 list：阶段×工种计数 / 矩阵（可带行内阶段名）
    if isinstance(rows, list) and rows:
        if all(isinstance(r, dict) for r in rows):
            names = []
            for i, r in enumerate(rows):
                row = {}
                for k, v in r.items():
                    if k in _LABOR_PHASE_META_KEYS:
                        continue
                    fv = _to_float(v)
                    if fv is not None:
                        row[str(k).strip()] = fv
                if not row:
                    continue
                nm = r.get("phase") or r.get("stage") or r.get("name") or r.get("label")
                if nm is None and i < len(phases):
                    nm = phases[i]
                names.append(str(nm).strip() if nm is not None and str(nm).strip()
                             else f"阶段{i + 1}")
                for k in row:
                    if k not in categories:
                        categories.append(k)
                matrix.append([row.get(c, 0.0) for c in categories])
            if names and categories:
                return _align_labor_axes(names, categories, matrix)
        elif all(isinstance(r, (list, tuple)) for r in rows):
            names = list(phases)
            for i, r in enumerate(rows):
                vals = list(r)
                # 行内阶段名：首元素不是数值 → 当作阶段名
                if vals and isinstance(vals[0], str) and _to_float(vals[0]) is None:
                    if i >= len(names):
                        names.append(vals[0].strip())
                    vals = vals[1:]
                nums = [_to_float(v, 0.0) or 0.0 for v in vals]
                if nums:
                    matrix.append(nums)
            if matrix:
                return _align_labor_axes(names, categories, matrix)

    # ④ data 为 dict：阶段→工种→值 / 工种→序列（转置）
    if isinstance(rows, dict) and rows:
        values = list(rows.values())
        if all(isinstance(v, dict) for v in values):
            names = []
            for pname, r in rows.items():
                row = {}
                for k, v in r.items():
                    fv = _to_float(v)
                    if fv is not None:
                        row[str(k).strip()] = fv
                if not row:
                    continue
                names.append(str(pname).strip() or f"阶段{len(names) + 1}")
                for k in row:
                    if k not in categories:
                        categories.append(k)
                matrix.append([row.get(c, 0.0) for c in categories])
            if names and categories:
                return _align_labor_axes(names, categories, matrix)
        elif all(isinstance(v, (list, tuple)) for v in values):
            cats = list(categories)
            cols: list = []
            for trade, series in rows.items():
                nm = str(trade).strip()
                if not nm:
                    continue
                if nm not in cats:
                    cats.append(nm)
                cols.append([_to_float(v, 0.0) or 0.0 for v in series])
            if cats and cols:
                n = max(len(c) for c in cols)
                matrix = [[cols[j][i] if i < len(cols[j]) else 0.0
                           for j in range(len(cols))] for i in range(n)]
                return _align_labor_axes(list(phases), cats, matrix)

    # ⑤ ECharts series / Chart.js datasets（labels 为阶段轴）
    series = data.get("series") or data.get("datasets")
    if isinstance(series, list) and series:
        cats = list(categories)
        cols = []
        for s in series:
            if not isinstance(s, dict):
                continue
            nm = s.get("name") or s.get("label")
            if nm is None or not str(nm).strip():
                continue
            if str(nm).strip() not in cats:
                cats.append(str(nm).strip())
            cols.append([_to_float(v, 0.0) or 0.0 for v in (s.get("data") or [])])
        if cats and cols:
            n = max(len(c) for c in cols)
            names = list(phases) or _as_str_list(data.get("labels"))
            matrix = [[cols[j][i] if i < len(cols[j]) else 0.0
                       for j in range(len(cols))] for i in range(n)]
            return _align_labor_axes(names, cats, matrix)

    # ⑥ labels + values 一维序列（单工种 × 各阶段）
    labels = _as_str_list(data.get("labels"))
    values = data.get("values")
    if labels and isinstance(values, (list, tuple)) and values:
        nums = [_to_float(v) for v in values]
        if any(n is not None for n in nums):
            matrix = [[n if n is not None else 0.0] for n in nums]
            return _align_labor_axes(labels, ["劳动力"], matrix)

    # ⑦ 工种清单（单阶段）：trades + peak/count/value
    trades = data.get("trades")
    if isinstance(trades, list) and trades:
        cats, vals = [], []
        for t in trades:
            if isinstance(t, dict):
                nm = t.get("name") or t.get("trade") or t.get("label") or t.get("type")
                if nm is None or not str(nm).strip():
                    continue
                v = t.get("peak")
                if v is None:
                    v = t.get("count")
                if v is None:
                    v = t.get("value")
                cats.append(str(nm).strip())
                vals.append(_to_float(v, 0.0) or 0.0)
            elif isinstance(t, (list, tuple)) and len(t) >= 2:
                if not str(t[0]).strip():
                    continue
                cats.append(str(t[0]).strip())
                vals.append(_to_float(t[1], 0.0) or 0.0)
        if cats:
            return _align_labor_axes(["阶段1"], cats, [vals])

    return None


def normalize_layout_data(data: Any) -> dict | None:
    """把平面布置图载荷归一为 `{"zones": [...]}`。

    - 容器别名 zones / areas / regions；
    - 区域缺 name 时回落 label / title → 「区域N」
      （渲染器本来就这么回落，校验器此前却判非法 → 整块被删块）；
    - 原始字段（id / description / category / position / size / color）全部保留。
    """
    if not isinstance(data, dict):
        return None
    zones = data.get("zones")
    if not isinstance(zones, list) or not zones:
        zones = data.get("areas")
    if not isinstance(zones, list) or not zones:
        zones = data.get("regions")
    if not isinstance(zones, list) or not zones:
        return None

    out: list = []
    for i, z in enumerate(zones):
        if not isinstance(z, dict):
            continue
        name = z.get("name") or z.get("label") or z.get("title")
        name = str(name).strip() if name is not None else ""
        out.append({**z, "name": name or f"区域{i + 1}"})
    if not out:
        return None
    return {"zones": out}


def normalize_timeline_data(data: Any) -> dict | None:
    """把时间轴载荷归一为 `{"milestones": [...]}`。

    - 容器别名 milestones / events / points / items；
    - name 缺失 → label/title → 「里程碑N」；
    - date 缺失 → 空串（渲染器按数组顺序绘制，date 仅作文本展示，不参与日期计算）；
    - status 别名（done/进行中/未开始…）→ completed/in_progress/pending；
      无法识别时**丢弃该键**，而不是让整块被判非法、正文图表被删。
    """
    if not isinstance(data, dict):
        return None
    raw = None
    for key in ("milestones", "events", "points", "items"):
        v = data.get(key)
        if isinstance(v, list) and v:
            raw = v
            break
    if not raw:
        return None

    out: list = []
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            continue
        item = {**m}
        name = m.get("name") or m.get("title") or m.get("label")
        name = str(name).strip() if name is not None else ""
        item["name"] = name or f"里程碑{i + 1}"
        date = m.get("date")
        if date is None:
            date = m.get("time")
        if date is None:
            date = m.get("day")
        item["date"] = str(date).strip() if date is not None else ""
        status = m.get("status")
        mapped = (_TIMELINE_STATUS_ALIASES.get(str(status).strip().lower())
                  if status else None)
        if mapped:
            item["status"] = mapped
        else:
            item.pop("status", None)
        out.append(item)
    if not out:
        return None
    return {"milestones": out}


def normalize_gantt_plan(plan: Any) -> dict | None:
    """把甘特图计划归一为 `{"tasks": [...]}`。

    - 容器别名 tasks / items / rows；
    - 任务 id 缺失时依次回落 name / task / title → 1-based 序号；
    - `dependencies` 支持用**任务名**引用（AI 常这么写）→ 归一为 id 的字符串形式
      （渲染器按 `str(task["id"])` 连线，name 形式的引用此前连不出箭头）；
      引用不到的任务直接丢弃，而不是让整块被判非法；
    - 任务上的 start / end / section / critical / remark 等字段全部保留。

    无法识别时返回 None（与渲染侧一致，交给占位/删除链路处理）。
    """
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(plan, dict):
        return None

    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        tasks = plan.get("items")
    if not isinstance(tasks, list) or not tasks:
        tasks = plan.get("rows")
    if not isinstance(tasks, list) or not tasks:
        return None

    out: list = []
    for idx, t in enumerate(tasks):
        if not isinstance(t, dict):
            continue
        tid = t.get("id")
        if tid is None or (isinstance(tid, str) and not tid.strip()):
            tid = t.get("name") or t.get("task") or t.get("title")
        if tid is None or (isinstance(tid, str) and not tid.strip()):
            tid = idx + 1
        out.append({**t, "id": tid, "dependencies": list(t.get("dependencies") or [])})
    if not out:
        return None

    # dependencies 归一：任务名 / 数字 / 字符串统一解析为「已存在任务 id 的字符串」
    # ✅ 键既含 id 也含 name/task/title —— AI 常写 `dependencies:["施工准备"]`
    #    用**任务名**引用（id 却是 1），只认 id 会让箭头全部连不出来。
    known: dict = {}
    for t in out:
        sid = str(t["id"])
        known[sid] = sid
        for alias_key in ("name", "task", "title"):
            alias = t.get(alias_key)
            if alias is not None and str(alias).strip():
                known.setdefault(str(alias).strip(), sid)
    for t in out:
        resolved: list = []
        for dep in t["dependencies"]:
            key = str(dep)
            if key in known:
                resolved.append(known[key])
        t["dependencies"] = resolved
    return {"tasks": out}


def validate_gantt_plan(plan: Any) -> tuple[bool, list[str]]:
    """
    验证甘特图计划数据（v3.1 规范）。

    验证规则：
    - 先过 normalize_gantt_plan（容器别名 / id 回落 / 依赖按任务名归一），
      与渲染器共用同一套容错规则；
    - 归一失败（无可用任务）即判非法；
    - 归一后任务 ID 不得重复。
    """
    errors = []

    try:
        if isinstance(plan, str):
            plan = json.loads(plan)
        if not isinstance(plan, dict):
            errors.append("甘特图计划必须是字典格式")
            return False, errors

        # ✅ 先归一：容器别名（tasks/items/rows）、id 回落 name/task/title/序号、
        #    dependencies 按**任务名**引用归一为 id。
        #    归一后所有 dependencies 均已解析为「已存在任务的 id」，因此下面的
        #    存在性检查恒成立 —— 这是刻意的：容错交给归一器（与渲染器同一套），
        #    校验器只负责归一器无法表达的结构性问题（id 重复）。
        normalized = normalize_gantt_plan(plan)
        if normalized is None:
            errors.append("必须包含至少一个任务")
            return False, errors
        tasks = normalized["tasks"]

        # 检查任务ID重复
        seen_ids = set()
        for task in tasks:
            task_id = task.get("id")
            if task_id in seen_ids:
                errors.append(f"检测到重复的任务ID: {task_id}")
            seen_ids.add(task_id)

    except (json.JSONDecodeError, TypeError) as e:
        errors.append(f"数据解析失败: {e!s}")

    return len(errors) == 0, errors


def validate_flowchart_structure(data: Any) -> tuple[bool, list[str]]:
    """
    验证流程图结构（v3.1 规范）。

    验证规则：
    - 节点ID仅含ASCII字符
    - 无孤立节点（所有节点均存在至少一条入边或出边，首尾节点除外）
    """
    errors = []

    try:
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            errors.append("流程图数据必须是字典格式")
            return False, errors

        nodes = data.get("nodes", []) or []
        edges = data.get("edges", []) or []

        if len(nodes) < 2:
            errors.append("流程图必须至少包含2个节点")
            return False, errors

        # 检查节点ID仅含ASCII
        ascii_allowed = re.compile(r"^[\w\s]+$")
        node_ids = set()
        for node in nodes:
            if not isinstance(node, dict):
                continue
            nid = node.get("id", str(node))
            if not ascii_allowed.match(str(nid)):
                errors.append(f"节点ID '{nid}' 包含非ASCII字符")
            node_ids.add(str(nid))

        # 检查入边/出边覆盖（简化：只要出现在边上就不算孤立）
        connected_ids = set()
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            from_id = str(edge.get("from", ""))
            to_id = str(edge.get("to", ""))
            if from_id:
                connected_ids.add(from_id)
            if to_id:
                connected_ids.add(to_id)

        # 孤立节点检测（排除可能在边列表中但未明确作为from/to的节点）
        isolated = node_ids - connected_ids
        if isolated:
            errors.append(f"检测到孤立节点（未在任何连线上出现）: {', '.join(sorted(isolated))}")

    except (json.JSONDecodeError, TypeError) as e:
        errors.append(f"数据解析失败: {e!s}")

    return len(errors) == 0, errors


_TIMELINE_DATE_PATTERNS = (
    re.compile(r"^\d{4}[-/.]\d{1,2}([-/.]\d{1,2})?$"),
    re.compile(r"^\d{4}年\d{1,2}月?(\d{1,2}日?)?$"),
    re.compile(r"^\d{1,2}[-/]\d{1,2}$"),
    re.compile(r"^第\s*\d+\s*(天|日|周|个?月|季度|年)$"),
    re.compile(r"^T\s*[+＋-]\s*\d+$", re.IGNORECASE),
)


def _is_valid_timeline_date_label(label: str) -> bool:
    """日期标签宽松校验：绝对日期或工程常用相对日期均可，长度 ≤ 40。"""
    label = label.strip()
    if not label or len(label) > 40:
        return False
    return any(p.match(label) for p in _TIMELINE_DATE_PATTERNS)


def validate_timeline_data(data: Any) -> tuple[bool, list[str]]:
    """
    验证时间线数据（timeline）。

    验证规则：
    - 先过 normalize_timeline_data（容器别名 / name 回落 / status 别名映射），
      与渲染器共用同一套容错规则；
    - 归一后 milestones 必须是 ≥2 个事件；
    - date 若非空必须是合法日期标签（YYYY-MM-DD、第N天、T+N 等）；
      空 date 合法 —— 渲染器按数组顺序绘制，date 仅作文本展示，不参与日期计算；
    - status 已由归一器映射为 completed/in_progress/pending 或删除。
    """
    errors = []

    try:
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            errors.append("时间线数据必须是字典格式")
            return False, errors

        # ✅ 先归一：容器别名（milestones/events/points/items）、name 回落
        #    label/title → 「里程碑N」、date 回落空串、status 别名
        #    （done/进行中/未开始…）映射为规范值、无法识别的 status 丢弃。
        #    校验器只看归一结果 —— 与渲染器同一套口径，避免
        #    "渲染器能画、校验器判非法 → 正文图表被删块"。
        normalized = normalize_timeline_data(data)
        if normalized is None:
            errors.append("timeline.milestones 必须是 ≥2 个事件的列表")
            return False, errors
        milestones = normalized["milestones"]
        if len(milestones) < 2:
            errors.append("timeline.milestones 必须是 ≥2 个事件的列表")
            return False, errors

        valid_statuses = {"completed", "in_progress", "pending"}
        for i, m in enumerate(milestones):
            date_str = str(m.get("date", "")).strip()
            # date 仅作展示标签：接受 YYYY-MM-DD、YYYY/MM/DD、YYYY年M月D日、
            # 第N天/周/月、T+N 等任意相对日期写法（渲染器按数组顺序绘制）。
            # 空 date 合法（归一器允许、渲染器画空日期）。
            if date_str and not _is_valid_timeline_date_label(date_str):
                errors.append(
                    f"timeline.milestones[{i}] date 应为日期标签（YYYY-MM-DD、YYYY年M月D日、第N天 等），实际: {date_str}"
                )
            status = m.get("status")
            if status and status not in valid_statuses:
                errors.append(
                    f"timeline.milestones[{i}] status 应为 completed/in_progress/pending，实际: {status}"
                )

    except (json.JSONDecodeError, TypeError) as e:
        errors.append(f"数据解析失败: {e!s}")

    return len(errors) == 0, errors


def validate_labor_data(data: Any) -> tuple[bool, list[str]]:
    """
    验证劳动力图数据（v3.1 规范）。

    验证规则：
    - 先过 normalize_labor_data（自然形态 → phases/categories/data 三件套），
      与渲染器共用同一套容错规则；
    - 归一后每个阶段至少有一个工种的数值 > 0；
    - 各工种人数之和不为负数。
    """
    errors = []

    try:
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            errors.append("劳动力数据必须是字典格式")
            return False, errors

        # ✅ 先归一：labor 曾是唯一「校验侧 + 渲染侧都零容错」的 JSON 图表类型 ——
        #    只认顶层 phases/categories/data 三件套，AI 产出的
        #    trades / series / datasets / phases[].workers 等自然形态一律判非法
        #    → 正文图表被整块删除（而渲染器其实画得出来）。
        #    归一器已把轴长度对齐（**一律以数据为准**，不丢行/列），
        #    这里只判语义有效性。
        normalized = normalize_labor_data(data)
        if normalized is None:
            errors.append("必须包含阶段、类别和劳动率数据")
            return False, errors

        phases = normalized["phases"]
        matrix = [list(row) for row in normalized["data"]]
        if not matrix:
            errors.append("无效的数据矩阵")
            return False, errors

        # ✅ 口径对齐（2026-09-17）：**单个阶段**全 0 由致命错误降级为告警。
        #    渲染器能画（该阶段画一根 0 高柱），而校验器判非法时
        #    `_chart_pipeline` 会把整张劳动力图从正文删除 —— 用整图换一行空柱，
        #    代价不对等。证据：`_diagnostics/_chart_shape_audit.py` 的
        #    「某阶段全 0」形态在修复前为 ❗删块型错配（校验=拒绝 / 渲染=出图）。
        #    仅当**所有阶段全 0**（整图无任何数值信息）时才判非法。
        _zero_phases = [
            (phases[i] if i < len(phases) else f"第{i + 1}个阶段")
            for i, row in enumerate(matrix)
            if not any(v > 0 for v in row)
        ]
        if _zero_phases and len(_zero_phases) == len(matrix):
            errors.append("所有阶段的工种人数均为0，图表无数值信息")
        elif _zero_phases:
            logger.warning(
                "劳动力图含全 0 阶段（%s）—— 保留出图，建议后续修正",
                "、".join(str(p) for p in _zero_phases[:3]))

        # 检查总人数非负（隐含，因为都是非负值，但显式检查更清晰）
        total = sum(sum(row) for row in matrix)
        if total < 0:
            errors.append(f"总劳动力人数不能为负数: {total}")

    except (json.JSONDecodeError, TypeError) as e:
        errors.append(f"数据解析失败: {e!s}")

    return len(errors) == 0, errors


def normalize_comparison_data(data: Any) -> dict | None:
    """把 comparison（比选 / 占比）载荷归一为 ``{"type","title","items"}``。

    ✅ **唯一归一入口**：校验侧（`validate_comparison_data`）与渲染侧
    （`mermaid_comparison._render_comparison_image_v2`）必须过同一个归一器，否则
    会出现「校验放行 → 渲染返回 None → 导出只剩红字占位」的占位型错配：
    comparison 此前**没有校验器**（`_JSON_VALIDATORS` 无该键，一律放行），而渲染器
    对「无 rows/items/data」「数值全为 0」的载荷返回 None —— 实测 3 例
    （空对象仅 type / 只有 title + note / items 数值全 0）会在交付文档里变成
    ``[图 X-Y … — 渲染失败]``（见 `_diagnostics/_chart_shape_audit.py`）。

    支持的形态（与原渲染器解析分支一一对应，行为保持等价）：
      1. 极简 / 饼图：``{"items":[{"label":"A","value":30}]}``
      2. 数据别名：``{"data":[{"label"|"name":"A","value"|"数值"|"占比":30}]}``
      3. 多指标行：``{"rows":[{"label":"A","values":[80,50]}]}``（detail 取 headers[1:]）
      4. 同义键：``{"rows":[{"item":"工期(天)","values":[80,50]}]}``
      5. 标准表格：``{"headers":["方案","费用"],"rows":[{"方案":"A","费用":80}]}``
      6. 数值对：``{"rows":[{"方案A":80}]}``

    规则：数值必须为**正数**（≤0 的项被丢弃，与原渲染器一致）；无有效项时返回 None。

    Returns:
        ``{"type": "comparison", "title": str, "items": [{"label","value","detail"?}]}``
        或 ``None``（无法识别 / 无数值信息）。
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    if not isinstance(data, dict):
        return None

    rows_data = data.get("rows", []) or []
    headers = data.get("headers", ["对比项", "数值"])
    if not isinstance(headers, list):
        headers = ["对比项", "数值"]

    if not rows_data and "items" in data:
        # Mermaid pie / 极简对比：{"items": [{"label": "A", "value": 30}]}
        items = data.get("items", []) or []
        if isinstance(items, list) and items:
            rows_data = items
            headers = ["对比项", "数值"]

    # ✅ 兼容 {type:comparison, data:[{label,value}]} 饼图 JSON 形态
    #（前端 jsonToMermaid 与导出裸 JSON 路径都会把该形态交给对比图渲染器）
    if not rows_data and "data" in data:
        items = data.get("data", []) or []
        if isinstance(items, list) and items:
            rows_data = [
                {
                    "label": str(it.get("label") or it.get("name") or "").strip(),
                    "value": it.get("value") or it.get("数值") or it.get("占比") or 0,
                }
                for it in items
                if isinstance(it, dict)
            ]
            headers = ["对比项", "数值"]

    if not isinstance(rows_data, list) or not rows_data:
        return None

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    parsed: list[dict] = []
    for row in rows_data:
        if not isinstance(row, dict):
            continue
        # 1) 极简：{"label","value"}（value / 数值 / 占比 三选一）
        if "label" in row and any(k in row for k in ("value", "数值", "占比")):
            value = _num(row.get("value") or row.get("数值") or row.get("占比") or 0)
            label = str(row.get("label", "")).strip()
            if label and value is not None and value > 0:
                parsed.append({"label": label, "value": value})
            continue
        # 2/3) 多指标行：{"label"|"item","values":[...]} → 取各项之和
        if ("label" in row or "item" in row) and isinstance(row.get("values"), list):
            label = str(row.get("label") or row.get("item") or "").strip()
            nums = [_num(v) for v in row["values"]]
            if label and nums and all(n is not None for n in nums):
                value = float(sum(nums))
                if value > 0:
                    detail = [
                        {"name": str(headers[idx + 1]), "value": n}
                        for idx, n in enumerate(nums) if idx + 1 < len(headers)
                    ]
                    parsed.append({"label": label, "value": value, "detail": detail})
            continue
        # 4) 标准表格：{"方案":"A","费用":80}（第一列作名称、第二列作数值）
        if len(headers) >= 2:
            label = str(row.get(headers[0], "")).strip()
            value = _num(row.get(headers[1], 0))
            if label and value is not None and value > 0:
                parsed.append({"label": label, "value": value})
                continue
        # 5) 数值对：{"方案A": 80}
        for k, v in list(row.items())[:1]:
            value = _num(v)
            if value is not None and value > 0:
                parsed.append({"label": str(k), "value": value})
                break

    if not parsed:
        return None
    return {
        "type": "comparison",
        "title": str(data.get("title") or "").strip(),
        "items": parsed,
    }
def validate_comparison_data(data: Any) -> tuple[bool, list[str]]:
    """验证 comparison（比选 / 占比）载荷（v3.2 规范）。

    ✅ 与渲染器**同口径**：本校验器放行 ⇔ 渲染器必然出图。
       实现方式是「先归一后校验」（与 labor / layout / timeline / gantt 一致）：
       归一无结果 ⇔ 渲染器同样返回 None，两端不可能分叉。
       校验器自身异常时按**通过**处理（宁可保留正文，也不误删图表）。
    """
    try:
        if normalize_comparison_data(data) is None:
            return False, ["对比图缺少可绘制的数据项（需 rows / items / data，且数值为正数）"]
    except Exception as e:  # noqa: BLE001
        logger.warning("comparison 校验器异常，按通过处理: %s", e)
        return True, []
    return True, []


# ⚠️ 兼容别名：旧名 `validate_comparison_pie` 保留但已指向新实现。
#    原实现**全仓无调用点**且头条规则在数学上恒真（sum((v/total)*100) ≡ 100，
#    永远落在 [99,101] 内），对渲染器支持的多指标行 {"label","values":[...]}
#    还会 float(list) 抛 TypeError —— 属于"检查器本身也是 bug"，故重写并接入管线。
validate_comparison_pie = validate_comparison_data


def validate_layout_data(data: Any) -> tuple[bool, list[str]]:
    """
    验证布局图数据（v3.1 规范）。

    验证规则：
    - 先过 normalize_layout_data（容器别名 / 区域 name 回落），
      与渲染器共用同一套容错规则；
    - 归一后必须至少有一个区域；
    - connections 的 from/to 必须是整数或字符串索引。
    """
    errors = []

    try:
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            errors.append("布局图数据必须是字典格式")
            return False, errors

        # ✅ 先归一：容器别名（zones/areas/regions）、区域缺 name 时回落
        #    label/title → 「区域N」。
        #    渲染器本来就这么回落，校验器此前却判"缺少有效名称" → 整块被删。
        normalized = normalize_layout_data(data)
        if normalized is None:
            errors.append("必须包含至少一个区域（zones/areas/regions）")
            return False, errors

        connections = data.get("connections") or []
        if isinstance(connections, list):
            for conn in connections:
                if not isinstance(conn, dict):
                    continue
                from_idx = conn.get("from")
                to_idx = conn.get("to")
                if from_idx is not None and to_idx is not None:
                    # 简单的索引有效性检查
                    if isinstance(from_idx, int) or isinstance(from_idx, str):
                        pass  # 合法，稍后检查是否在范围内
                    else:
                        errors.append("连接项 from/to 必须是整数或字符串索引")

    except (json.JSONDecodeError, TypeError) as e:
        errors.append(f"数据解析失败: {e!s}")

    return len(errors) == 0, errors


# ============================================================
# 触发条件关键词库（v3.1 规范，120+ 触发词）
# ============================================================
TRIGGER_KEYWORDS = {
    "gantt": [
        "进度计划",
        "施工进度",
        "工期安排",
        "里程碑",
        "时间节点",
        "施工部署",
        "阶段划分",
        "总工期",
        "开竣工日期",
        "关键线路",
        "月度计划",
        "周计划",
        "赶工措施",
        "进度保证",
        "工期目标",
        "工期倒排",
        "节点目标",
        "进度款支付节点",
        "分段验收",
        "先...再...最后...",
        "分...个阶段",
        "从...到...需要...",
        "计划...开始，...结束",
        "工期60天",
        "第30天",
    ],
    "flowchart": [
        "施工工序",
        "工艺流程",
        "部署顺序",
        "操作步骤",
        "工艺衔接",
        "施工流向",
        "作业流程",
        "施工程序",
        "逻辑关系",
        "先...后...",
        "施工流程",
        "验收流程",
        "报验流程",
        "审批流程",
        "移交流程",
        "交接检",
        "隐蔽验收流程",
        "变更流程",
        "索赔流程",
        "招标流程",
        "采购流程",
        "验收步骤",
        "检查步骤",
        "安装步骤",
        "调试步骤",
        "施工准备→...→交付",
        "如果...则...，反之...",
        "是否合格",
        "满足要求",
        "条件判断",
    ],
    "architecture": [
        "组织机构",
        "项目管理架构",
        "组织架构",
        "管理体系",
        "岗位配置",
        "岗位设置",
        "职责分工",
        "责任划分",
        "职能分配",
        "WBS",
        "工作分解",
        "工作包",
        "系统架构",
        "网络拓扑",
        "系统拓扑",
        "子系统",
        "模块构成",
        "设备构成",
        "系统构成",
        "平台架构",
        "软件架构",
        "管理层级",
        "汇报关系",
        "上下级关系",
        "矩阵管理",
        "项目制",
        "职能制",
        "指挥部",
        "项目部",
        "五方责任主体",
        "部门设置",
        "总包/分包/业主/监理关系",
        "管理跨度",
        "指挥体系",
        "决策层→管理层→执行层",
        "岗位职责",
        "岗位说明书",
        "权责矩阵",
        "设...层",
        "下设...个部门",
        "由...负责...，...向...汇报",
        "包含...层",
    ],
    "labor": [
        "劳动力配置",
        "人员配置",
        "人力计划",
        "工种分配",
        "劳动力投入",
        "用工需求",
        "资源配置",
        "用工峰值",
        "劳动力曲线",
        "劳动力动态",
        "各阶段人数",
        "技术工种",
        "普工配置",
        "劳动力缺口",
        "人员调配",
        "劳动力平衡",
        "配置...名...工，...名...工",
        "高峰期80人",
        "钢筋工30人",
        "木工25人",
    ],
    "layout": [
        "总平面布置",
        "施工分区",
        "功能分区",
        "功能布局",
        "区域划分",
        "临建布置",
        "交通组织",
        "场地布置",
        "堆场布置",
        "加工区布置",
        "办公区布置",
        "生活区布置",
        "临时道路",
        "围挡布置",
        "出入口设置",
        "材料堆放区",
        "周转场地",
        "施工便道",
        "现场排水",
        "泥浆池布置",
        "塔吊覆盖范围",
        "施工电梯定位",
        "钢筋加工棚",
        "木工加工棚",
        "砂浆搅拌站",
        "预制构件堆场",
        "进场道路",
        "消防通道",
        "垂直运输布置",
        "进入...区域",
        "...区布置",
        "...侧为...",
    ],
    "comparison": [
        "得分权重",
        "成本占比",
        "资源配置比例",
        "风险分布",
        "投资构成",
        "费用占比",
        "人员比例",
        "工期占比",
        "材料占比",
        "合格率对比",
        "方案对比",
        "指标对比",
        "A/B/C方案比选",
        "预算占比",
        "实际vs计划",
        "目标vs完成",
        "节约率",
        "损耗率",
        "利用率",
        "投诉分布",
        "质量问题分布",
        "工期延误原因分析",
        "各项占比",
        "各项权重",
        "百分比",
        "一方面...另一方面...",
        "与...相比，...高出...",
    ],
}

# 句式触发模式（正则表达式）
SENTENCE_PATTERNS = {
    "gantt": [
        r"先(.+)再(.+)最后(.+)",
        r"分(\d+)个阶段",
        r"从(.+)到(.+)需要(.+)天",
        r"计划(.+)开始，(.+)结束",
    ],
    "architecture": [
        r"由(.+)负责(.+), (.+)向(.+)汇报",
        r"设(.+)层",
        r"下设(.+)个部门",
        r"包含(.+)层",
    ],
    "comparison": [r"与(.+)相比(.+)高(.+)", r"与(.+)相比(.+)低(.+)", r"一方面(.+)另一方面(.+)"],
    "layout": [r"进入(.+)区域", r"(.+)区布置", r"东侧为(.+)|西侧为(.+)|北侧为(.+)"],
    "labor": [r"配置(.+)名(.+)工，(.+)名(.+)工"],
    "flowchart": [r"如果(.+)则(.+)，反之(.+)"],
}

# 行业标准化术语库（按分类）
INDUSTRY_TERMS = {
    # 建筑工程施工术语
    "construction": {
        "地基基础": ["桩基", "基坑支护", "土方开挖", "回填", "垫层"],
        "主体结构": ["砌体", "混凝土", "钢筋模板", "钢结构", "幕墙"],
        "建筑装饰": ["吊顶", "墙面装饰", "地面铺装", "门窗安装"],
        "建筑机电": ["给排水", "电气布线", "通风空调", "智能建筑"],
    },
    # 市政工程施工术语
    "municipal": {
        "路基路面": ["路基处理", "水稳层", "沥青面层", "水泥路面"],
        "桥梁隧道": ["墩柱", "梁板", "隧道开挖", "衬砌", "涵洞"],
        "管道工程": ["给排水管道", "污水管网", "雨水管", "顶管", "箱涵"],
        "附属设施": ["交通设施", "照明工程", "绿化工程", "围堰"],
    },
    # 机电安装术语
    "mech_elect": {
        "设备安装": ["压缩机安装", "泵组安装", "变压器安装", "盘柜安装"],
        "管道工程": ["管道预制", "焊接工艺", "法兰连接", "阀门安装", "试压"],
        "电气工程": ["电缆桥架", "母线槽", "防雷接地", "DCS系统", "PLC系统"],
        "调试试车": ["单机试车", "联动试车", "吹扫清洗", "防腐绝热"],
    },
    # 水利水电术语
    "water_cons": {
        "土石方": ["土石方开挖", "土石方填筑", "地基处理"],
        "水工建筑": ["混凝土坝", "水闸", "泵站", "隧洞", "渡槽"],
        "机电设备": ["启闭机", "闸门机组", "水力发电设备", "水文监测"],
        "河道治理": ["护岸工程", "防汛工程", "灌溉渠系", "河道疏浚"],
    },
    # 公路交通术语
    "highway": {
        "路基路面": ["路基", "桩基", "墩柱", "梁板", "伸缩缝"],
        "桥隧工程": ["桥梁", "隧道", "沥青路面", "水稳层"],
        "交通安全": ["交通标志", "标线", "护栏", "照明"],
        "机电工程": ["监控系统", "收费系统", "通信系统", "绿化环保"],
    },
    # 通用管理类
    "general_project": [
        "项目经理部",
        "五方责任主体",
        "施工组织设计",
        "专项施工方案",
        "技术交底",
        "图纸会审",
        "设计变更",
        "工程签证",
        "隐蔽验收",
        "检验批",
        "分项工程",
        "分部工程",
        "单位工程",
        "竣工验收",
        "备案",
        "质量管理体系",
        "三检制",
        "实测实量",
        "通病防治",
        "安全技术交底",
        "危险源辨识",
        "应急预案",
        "文明施工",
        "扬尘治理",
        "噪声控制",
        "绿色施工",
        "碳减排",
        "BIM模型",
    ],
}


def get_industry_terms_by_category(category: str) -> list[str]:
    """获取指定行业的标准化术语列表"""
    terms = []
    if category in INDUSTRY_TERMS:
        val = INDUSTRY_TERMS[category]
        if isinstance(val, dict):
            for term_list in val.values():
                terms.extend(term_list)
        elif isinstance(val, list):
            terms.extend(val)
    # 也返回通用术语
    terms.extend(INDUSTRY_TERMS.get("general_project", []))
    return terms


def trigger_match(keyword: str, text: str) -> bool:
    """检查关键词是否与文本匹配（子串匹配，忽略大小写和空格）"""
    if not keyword or not text:
        return False
    kw = keyword.replace(" ", "").replace("·", "").lower()
    txt = text.replace(" ", "").replace("·", "").lower()
    return kw in txt or txt in kw


def detect_chart_type_from_text(text: str) -> list[tuple[str, float]]:
    """
    根据文本内容检测可能的图表类型及匹配分数（v3.1规范）。

    返回按分数降序排列的 (chart_type, score) 列表。
    """
    results = []
    text_lower = text.lower()

    # 关键词匹配计数
    for chart_type, keywords in TRIGGER_KEYWORDS.items():
        match_count = sum(1 for kw in keywords if trigger_match(kw, text_lower))
        results.append((chart_type, match_count))

    # 句式模式匹配（额外加分）
    for chart_type, patterns in SENTENCE_PATTERNS.items():
        for pattern in patterns:
            try:
                import re

                if re.search(pattern, text):
                    # 句式匹配额外加 1 分
                    idx = next((i for i, (ct, _) in enumerate(results) if ct == chart_type), -1)
                    if idx >= 0:
                        results[idx] = (chart_type, results[idx][1] + 1)
            except re.error:
                pass

    # 按匹配数排序
    results.sort(key=lambda x: (-x[1], x[0]))
    return results


def extract_matched_keywords(text: str, chart_type: str) -> list[str]:
    """提取用于 matchedKeywords 字段的匹配关键词"""
    matched = []
    keywords = TRIGGER_KEYWORDS.get(chart_type, [])
    for kw in keywords:
        if trigger_match(kw, text):
            matched.append(kw)
    return matched


def detect_chart_with_metadata(text: str) -> dict:
    """
    根据文本内容检测图表类型及匹配信息（v3.1规范增强版）。

    Returns:
        {
            "chart_types": [(chart_type, score), ...],  # 按分数降序排列
            "matched_keywords": {chart_type: [keywords], ...},  # 每个类型的匹配关键词
            "top_chart": chart_type or None,  # 最高分的图表类型
            "top_score": int,  # 最高分
            "patterns_matched": List[str]  # 匹配的句式模式
        }
    """
    results = []
    matched_keywords = {}
    patterns_matched = []
    text_lower = text.lower()

    # 关键词匹配计数
    for chart_type, keywords in TRIGGER_KEYWORDS.items():
        match_count = sum(1 for kw in keywords if trigger_match(kw, text_lower))
        if match_count > 0:
            matched_keywords[chart_type] = [kw for kw in keywords if trigger_match(kw, text_lower)]
        results.append((chart_type, match_count))

    # 句式模式匹配（额外加分）
    for chart_type, patterns in SENTENCE_PATTERNS.items():
        for pattern in patterns:
            try:
                if re.search(pattern, text):
                    patterns_matched.append(f"{chart_type}:{pattern[:50]}...")
                    # 额外加分
                    for i, (ct, _) in enumerate(results):
                        if ct == chart_type:
                            results[i] = (chart_type, results[i][1] + 1)
                            break
            except re.error:
                pass

    # 按匹配数排序
    results.sort(key=lambda x: (-x[1], x[0]))

    # 确保所有类型都有 matched_keywords 字段（空列表）
    all_types = set(TRIGGER_KEYWORDS.keys())
    for ct in all_types:
        if ct not in matched_keywords:
            matched_keywords[ct] = []

    top_chart = results[0][0] if results else None
    top_score = results[0][1] if results else 0

    return {
        "chart_types": results,
        "matched_keywords": matched_keywords,
        "top_chart": top_chart,
        "top_score": top_score,
        "patterns_matched": patterns_matched,
    }


# AI调用超时装饰器
async def with_timeout(coro, timeout: int = 30, error_msg: str = "AI调用超时"):
    """带超时的AI调用装饰器"""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        raise TimeoutError(f"{error_msg}（{timeout}秒）")


# ============================================================
# Mermaid 语法归一（校验侧与渲染侧**共用同一实现**）
# ============================================================
# 为什么必须共享：`;` 与换行在 Mermaid 语义上完全等价，但**校验侧
# validate_mermaid 与全部 PIL 解析器都是按行解析**。两侧各写一份归一化，任何一侧
# 漏掉就是「正文里的合法图表被静默删除」：渲染侧早已在
# mermaid_renderer.render_mermaid_to_bytes 内联做 `;` → 换行，校验侧却只删"行末"
# 分号 → `flowchart TD; A-->B; B-->C`（单行分号写法）被整行跳过、节点数算成 0，
# 判为「节点数不足」→ 修复失败 → 整块从正文删除（实测 2 例，见
# `_diagnostics/_chart_shape_audit.py` 的 flowchart 形态）。
def strip_leading_mermaid_comments(code: str) -> str:
    """去掉 Mermaid 代码开头的空行与 ``%%`` 注释行（含 ``%%{init: ...}%%``）。

    AI 常在首行输出 ``%% 流程图`` 或 ``%%{init: ...}%%``，而下游解析与图表类型判定
    普遍使用 ``code.startswith("graph"/"gantt"/...)``，一旦以注释开头则所有分支落空
    （实测 gantt 会因此多画一行垃圾任务）。
    """
    if not code:
        return ""
    lines = str(code).split("\n")
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or s.startswith("%%"):
            i += 1
            continue
        break
    return "\n".join(lines[i:]).strip()


def normalize_mermaid_statements(code: str) -> str:
    """把 Mermaid 语句分隔符 ``;`` 归一为换行（与 Mermaid 语义等价）。

    ✅ 只替换**引号外、括号外**的分号：节点标签里的分号（``A["准备;验收"]``）必须
    原样保留，一刀切替换会破坏标签内容（旧渲染侧实现即为此类朴素替换）。
    """
    if not code or ";" not in code:
        return code
    out: list[str] = []
    in_quote = False
    depth = 0
    for ch in str(code):
        if ch == '"':
            in_quote = not in_quote
            out.append(ch)
            continue
        if not in_quote:
            if ch in "[{(":
                depth += 1
            elif ch in "]})":
                depth = max(0, depth - 1)
            elif ch == ";" and depth == 0:
                out.append("\n")
                continue
        out.append(ch)
    return "".join(out)


# ============================================================
# 图表类型标签与快速推断（charts.py 与 _chart_pipeline.py 共享，
# 消除两份独立词表漂移；顺序敏感：先命中先返回，勿随意重排）
# ============================================================
CHART_TYPE_LABELS = {
    "flowchart": "流程图", "gantt": "甘特图", "architecture": "组织架构图",
    "labor": "劳动力配置图", "comparison": "对比图", "layout": "总平面布置图",
    "timeline": "时间轴",
}


# ============================================================
# Mermaid 首关键字 → 内部图表类型（**唯一事实来源**）
# ============================================================
# 历史问题：本映射曾在 3 处各维护一份且互相不一致 ——
#   · routers/_chart_pipeline.py（8 条）：mindmap / journey / classDiagram /
#     erDiagram / quadrantChart / gitGraph / sankey / block / kanban 的内联图表
#     **不会被登记**进 chart_predictions → 图表清单与导出预检里看不到它，
#     而预览（前端 mermaid.js）与导出又能正常出图（"幽灵图表"）；
#   · routers/export.py（20 条）：同一段正文在导出侧会被识别为图表块；
#   · 已删除的显式生成模块（6 条）：mindmap / journey / sequenceDiagram
#     被判定为"非法 Mermaid 代码"而**直接丢弃**（该模块已被移除）。
# 现统一到本表：登记侧 / 导出侧行为完全一致。
MERMAID_KEYWORD_TO_CHART_TYPE: dict[str, str] = {
    "graph": "flowchart",
    "flowchart": "flowchart",
    "gantt": "gantt",
    "pie": "comparison",
    "xychart": "comparison",
    "xychart-beta": "comparison",
    "timeline": "timeline",
    "architecture": "architecture",
    "architecture-beta": "architecture",
    "sequencediagram": "sequence",
    "classdiagram": "class",
    "statediagram": "state",
    "statediagram-v2": "state",
    "erdiagram": "er",
    "journey": "journey",
    "mindmap": "mindmap",
    "quadrantchart": "quadrant",
    "gitgraph": "git",
    "sankey": "sankey",
    "sankey-beta": "sankey",
    "block": "block",
    "block-beta": "block",
    "kanban": "kanban",
}

# 具备原生 PIL 渲染器的 7 类；其余类型只有 mermaid 原生引擎（前端 mermaid.js /
# HTTP Service）能够渲染，PIL 兜底时必然产出占位提示。
PIL_RENDERABLE_CHART_TYPES: frozenset[str] = frozenset({
    "flowchart", "gantt", "architecture", "labor",
    "comparison", "layout", "timeline",
})


def first_mermaid_keyword(code: str) -> str:
    """取 Mermaid 代码的首个有效关键字（小写，跳过空行与 ``%%`` 注释行）。"""
    if not code:
        return ""
    for raw in str(code).split("\n"):
        toks = raw.strip().split()
        if not toks or toks[0].startswith("%%"):
            continue
        # `gitGraph:` / `stateDiagram-v2 ` 等写法带尾部标点
        return toks[0].lower().rstrip(":;,{")
    return ""


def detect_mermaid_chart_type(code: str, default: str = "flowchart") -> str:
    """从 Mermaid 代码推断内部图表类型；未识别的关键字返回 ``default``。

    传 ``default=""`` 可用来判断"这是否是一段我们认识的可渲染图表代码"。
    """
    return MERMAID_KEYWORD_TO_CHART_TYPE.get(first_mermaid_keyword(code), default)


def infer_chart_type_from_payload(payload) -> str:
    """从 chart-json **载荷结构**推断图表类型（无 ``type`` 键 / 类型非法时的兜底）。

    ✅ 新增（2026-09-19）：导出侧原先把"合法 JSON 对象但缺 ``type`` 键"静默兜底为
    ``labor``（`obj.get("type") or "labor"`），而 labor 渲染器对非 labor 结构必然
    返回 None → 交付文档出现红色「[图 X-Y 劳动力配置计划 — 渲染失败]」占位，
    且图题与实际内容完全不符（实测脚手架方案第 4 章 3 处）。
    这与登记侧口径（``_scan_inline_charts`` 要求 type 在图表白名单内，否则不登记）
    分叉。此处按载荷**结构特征**做确定性推断，两侧共用同一张判据表；
    结构无法判定时返回空串，由调用方按"未知类型不渲染"处理。

    判据顺序敏感（先命中先返回）：结构键比数值形状更可靠，故先看键名。
    """
    if not isinstance(payload, dict):
        return ""

    def _has(key: str) -> bool:
        return isinstance(payload.get(key), (list, tuple, dict)) and bool(payload.get(key))

    # 显式声明的合法类型优先（大小写/空白容错），避免被结构判据覆盖
    declared = str(payload.get("type", "") or "").strip().lower()
    if declared in PIL_RENDERABLE_CHART_TYPES:
        return declared

    if _has("root") or _has("children"):
        return "architecture"
    if _has("zones"):
        return "layout"
    if _has("steps") or _has("nodes") or _has("edges"):
        return "flowchart"
    # 劳动力四合一图：phases + categories + data 三键齐备（items/labels 为别名形态）
    if (_has("phases") or _has("categories")) and (_has("data") or _has("rows")):
        return "labor"
    if _has("tasks") or _has("milestones") or _has("events"):
        # milestones/events 更接近时间轴；tasks 是甘特
        return "timeline" if not _has("tasks") else "gantt"
    if _has("items") or _has("headers"):
        items = payload.get("items")
        if isinstance(items, (list, tuple)) and items and isinstance(items[0], dict):
            first = items[0]
            if "start" in first or "end" in first or "duration" in first:
                return "gantt"
            if "value" in first or "ratio" in first or "percent" in first:
                return "comparison"
        if _has("headers"):
            return "comparison"
    if _has("data") or _has("series"):
        return "comparison"
    return ""


def infer_chart_type(text: str) -> str:
    """从章节标题/描述/编排理由中快速推断图表类型（兜底 flowchart）"""
    t = text or ""
    if any(k in t for k in ("进度", "工期", "横道", "甘特", "施工计划")):
        return "gantt"
    if any(k in t for k in ("架构", "组织", "机构", "体系", "职责分工", "组成结构")):
        return "architecture"
    if any(k in t for k in ("劳动力", "人员配置", "用工")):
        return "labor"
    if any(k in t for k in ("对比", "比较", "选型", "方案比选")):
        return "comparison"
    if any(k in t for k in ("总平面", "布置", "平面", "场地规划", "临建", "功能分区", "施工区划", "交通组织")):
        return "layout"
    if any(k in t for k in ("里程碑", "时间轴", "时间线", "关键节点", "时间节点", "工期节点", "节点控制", "验收节点")):
        return "timeline"
    return "flowchart"
