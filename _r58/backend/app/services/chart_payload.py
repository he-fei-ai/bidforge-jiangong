"""图表载荷的**唯一构造器 + 唯一解析器**（单一事实来源）。

为什么需要本模块
----------------
一张图表在库中同时落两处（双写，互为备份）：

  * `chart_predictions.data_json`  —— 方案级图表表（导出主数据源）
  * `sections.<type>_json`         —— 章节级列（导出第二数据源）

「双写」架构的隐含契约是：**两条写入路径必须产出同一形状**。
历史上有三个写入点各自手搓 JSON，形状互不相同：

  | 写入点 | 原形状 |
  |---|---|
  | `_persist_chart()` | `{"mermaid_code": code, "title":…, "reason":…}` |
  | `generate_chart_data()` | 裸结构化 JSON `{"tasks":…}` |
  | `fix_mermaid()` | `{"mermaid_code": fixed}`（无 title/reason）|

读取侧同样分裂过：`export.py` 有一套能归一化三种形态的私有实现，
`charts.py::list_charts` 另有一套只认 `mermaid_code` 的简化版 ——
结果 `/charts/generate-chart-data` 产出的甘特图**生成成功、落库成功、
导出也有，却永远不出现在图表清单里**（界面看不到，无法预览/定位/删除）。
该缺陷长期不可见，因为那个接口原本没有 UI 入口，直到补齐前端能力时才被激活。

收敛方案（V5.3）
----------------
  * **写**：所有写入点一律调用 `build_chart_envelope()`，再无第二处 JSON 拼装。
  * **读**：所有消费端一律调用 `extract_chart_payload()`，再无第二处归一化。
  * 二者严格互逆：`extract_chart_payload(build_chart_envelope(code=x)) == x`。
  * `chart_payload_shape()` 供审计/回归脚本断言「库里不存在第三种形状」。

兼容性
------
`extract_chart_payload()` 仍完整兼容历史遗留形状（裸 Mermaid、裸结构化 JSON、
旧信封），因此**无需数据迁移**：老行照常可读，新行一律为规范信封。
"""
import json

__all__ = [
    "build_chart_envelope",
    "extract_chart_payload",
    "chart_payload_shape",
    "is_canonical_chart_payload",
]

# 信封中表示"Mermaid 代码"的键，按优先级排列
_CODE_KEYS = ("mermaid_code", "mermaid", "code")
# 信封中表示"结构化数据"的键，按优先级排列
_DATA_KEYS = ("data", "payload", "content")
# 原生结构化数据自身的特征键（无信封包装时用于识别）
_STRUCT_HINT_KEYS = (
    "tasks", "zones", "milestones", "children", "phases",
    "items", "nodes", "label", "areas",
)


def build_chart_envelope(code: str = "", data=None, *,
                         title: str = "", reason: str = "") -> str:
    """构造图表持久化信封 —— **写入侧唯一构造器**。

    与 `extract_chart_payload()` 严格互逆。

    Args:
        code: Mermaid 代码（flowchart/architecture 等）；与 `data` 二选一。
        data: 结构化图表数据（gantt/labor/comparison 等 dict）；与 `code` 二选一。
        title: 图表标题（落库供清单/导出展示，可空）。
        reason: 生成理由 / 用户需求原文（可空）。

    Returns:
        规范信封 JSON 字符串；`code` 与 `data` 均空时返回 `""`（调用方据此跳过落库）。

    Examples:
        >>> build_chart_envelope(code="graph TD;A-->B", title="施工流程")
        '{"mermaid_code": "graph TD;A-->B", "title": "施工流程", "reason": ""}'
    """
    code = (code or "").strip() if isinstance(code, str) else ""
    has_data = data is not None and data != "" and data != {} and data != []

    if not code and not has_data:
        return ""

    if code:
        return json.dumps(
            {"mermaid_code": code, "title": title or "", "reason": reason or ""},
            ensure_ascii=False)

    return json.dumps(
        {"mermaid_code": "", "data": data, "title": title or "", "reason": reason or ""},
        ensure_ascii=False)


def extract_chart_payload(raw) -> str:
    """从各种持久化格式中提取可渲染载荷 —— **读取侧唯一解析器**。

    兼容全部历史形态：
      1. 规范信封（`build_chart_envelope` 产出）
      2. 纯 Mermaid 代码字符串
      3. 旧式包装 `{"mermaid_code"|"mermaid"|"code"|"data"|"payload"|"content": ...}`
      4. 裸结构化 JSON 数据（`tasks` / `zones` / `milestones` / `phases` ...）

    Returns:
        可渲染载荷字符串；无法识别时返回 `""`（调用方据此跳过该图）。
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    try:
        obj = json.loads(s)
    except (json.JSONDecodeError, TypeError, ValueError):
        return s  # 纯 Mermaid 代码

    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, list) and obj:
        return s
    if not isinstance(obj, dict):
        return ""

    # 1) 显式代码包装
    for k in _CODE_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # 2) 嵌套数据包装（dict / list / 字符串均接受）
    for k in _DATA_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (dict, list)) and v:
            return json.dumps(v, ensure_ascii=False)

    # 3) 原生结构化数据（含 type 或已知数据键）→ 原样交给渲染器
    if obj.get("type") or any(k in obj for k in _STRUCT_HINT_KEYS):
        return s
    return ""


def chart_payload_shape(raw) -> str:
    """识别载荷形态标签，供审计脚本断言「不存在第三种形状」。

    Returns:
        ``"empty"``            —— 空 / 全空白
        ``"envelope:mermaid"`` —— 信封承载可渲染文本（Mermaid 代码）
        ``"envelope:data"``    —— 信封承载结构化数据（dict / list）
        ``"legacy:raw-mermaid"`` —— 历史遗留：未包装的 Mermaid 代码
        ``"legacy:json-string"`` —— 历史遗留：JSON 字符串套娃
        ``"legacy:bare-json"``   —— 历史遗留：未包装的结构化数据
        ``"unknown"``          —— 无法归类（应视为异常）
    """
    if raw is None:
        return "empty"
    s = str(raw).strip()
    if not s:
        return "empty"

    try:
        obj = json.loads(s)
    except (json.JSONDecodeError, TypeError, ValueError):
        return "legacy:raw-mermaid"

    if isinstance(obj, str):
        return "legacy:json-string"
    if isinstance(obj, list):
        return "legacy:bare-json" if obj else "empty"
    if not isinstance(obj, dict):
        return "unknown"

    # 字符串载荷（无论挂在代码键还是数据键下）都是"可渲染文本"，
    # 与 `extract_chart_payload` 的读取优先级保持一致。
    has_code = any(
        isinstance(obj.get(k), str) and obj[k].strip()
        for k in _CODE_KEYS + _DATA_KEYS)
    has_data = any(
        isinstance(obj.get(k), (dict, list)) and obj[k] for k in _DATA_KEYS)

    # 规范信封：恰好携带一种载荷
    if has_code and not has_data:
        return "envelope:mermaid"
    if has_data and not has_code:
        return "envelope:data"
    if has_code and has_data:
        return "unknown"  # 双载荷信封非法，视为异常

    if obj.get("type") or any(k in obj for k in _STRUCT_HINT_KEYS):
        return "legacy:bare-json"
    if not obj:
        return "empty"
    return "unknown"


def is_canonical_chart_payload(raw) -> bool:
    """是否为规范信封（或空值）。新增写入**必须**满足此条件。"""
    return chart_payload_shape(raw) in ("empty", "envelope:mermaid", "envelope:data")
