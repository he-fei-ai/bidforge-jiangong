"""Flowchart 图表渲染器。

由 scripts/_split_mermaid.py 从原 mermaid_renderer.py 自动拆分而来。
所有公共工具从 .mermaid_common 导入。
"""
from __future__ import annotations

import logging
import re
from io import BytesIO
from typing import Any

from PIL import Image, ImageDraw

from .mermaid_common import (
    _draw_text_center,
    _image_font,
    _image_to_stream,
    paper_floor_note,
    _render_hq,
    _text_height,
    _wrap_text,
)

logger = logging.getLogger(__name__)



# 决策节点关键词（中英双语启发式判定）
_DECISION_KEYWORDS = (
    "?",
    "？",
    "是否",
    "判",
    "审核",
    "审查",
    "审批",
    "是否通过",
    "否",
    "通过",
    "成功",
    "失败",
    "满足",
    "达成",
    "符合",
    "合格",
    "不合格",
    "正确",
    "错误",
    "Check",
    "Pass",
    "Fail",
    "Yes",
    "No",
    "Approve",
    "Verify",
    "Confirm",
)


def _parse_mermaid_flowchart(mermaid_code: str) -> list[str] | None:
    """解析 Mermaid 流程图代码，提取节点标签列表"""
    nodes = []
    lines = mermaid_code.strip().split("\n")
    for line in lines:
        line = line.strip()
        if not line or line.startswith("graph") or line.startswith("flowchart"):
            continue
        node_match = re.match(r"^\w+\s*\[([^\]]*)\]", line)
        if node_match:
            label = node_match.group(1).strip()
            if label:
                nodes.append(label)
            continue
        node_match = re.match(r"^\w+\s*\{([^}]*)\}", line)
        if node_match:
            label = node_match.group(1).strip()
            if label:
                nodes.append(label)
            continue
        node_match = re.match(r"^\w+\s*\(\(([^)]*)\)\)", line)
        if node_match:
            label = node_match.group(1).strip()
            if label:
                nodes.append(label)
            continue
    return nodes if len(nodes) >= 2 else None

def _is_decision_label(label: str) -> bool:
    """根据标签内容启发式判断是否为决策节点（菱形）。"""
    if not label:
        return False
    label = label.strip()
    if "?" in label or "？" in label:
        return True
    for kw in _DECISION_KEYWORDS:
        if kw in label:
            return True
    return False

def _extract_mermaid_node_label_raw(nid: str) -> str | None:
    """从 Mermaid 节点定义中提取可读标签（原始形态，不清洗转义）。

    支持以下语法：
      - A1["测量放线"]      -> "测量放线"
      - A1["水压试验"]      -> "水压试验"
      - A1(测量放线)        -> "测量放线"
      - A1{判定}            -> "判定"
      - A1(("圆形节点"))    -> "圆形节点"
      - A1>"非对称"]        -> "非对称"

    Args:
        nid: Mermaid 节点 ID 字符串（可能含标签）

    Returns:
        提取后的标签文本；若无法提取则返回 None
    """
    if not nid:
        return None
    # ✅ R67：先还原转义序列再提取/剥引号 —— 顺序反了会把 `label\"` 的尾部
    #    引号先剥掉，留下孤悬反斜杠（实测样张 ``CheckBIM -- \"通过\\``）。
    nid = _unescape_label_text(nid)

    # 1) 圆角节点 A1(["label"]) - 商业级优先
    m = re.match(r"^[A-Za-z_]\w*\s*\(\s*\[(.*)\]\s*\)$", nid)
    if m:
        return _strip_quote_pair(m.group(1))

    # 2) 圆形节点 A1(("label"))
    m = re.match(r"^[A-Za-z_]\w*\s*\(\((.*)\)\)$", nid)
    if m:
        return _strip_quote_pair(m.group(1))

    # 3) 矩形节点 A1["label"] / A1[label]
    m = re.match(r"^[A-Za-z_]\w*\s*\[(.*)\]$", nid)
    if m:
        return _strip_quote_pair(m.group(1))

    # 4) 菱形节点 A1{"label"}
    m = re.match(r"^[A-Za-z_]\w*\s*\{(.*)\}$", nid)
    if m:
        return _strip_quote_pair(m.group(1))

    # 5) 圆角矩形 A1("label")
    m = re.match(r"^[A-Za-z_]\w*\s*\((.*)\)$", nid)
    if m:
        return _strip_quote_pair(m.group(1))

    # 6) 非对称 A1>"label"]
    m = re.match(r"^[A-Za-z_]\w*\s*>\s*\"(.*)\"\s*\]$", nid)
    if m:
        return _strip_quote_pair(m.group(1))

    # 7) 并行/平行 A1[/"label"/]
    m = re.match(r"^[A-Za-z_]\w*\s*/\s*\"(.*)\"\s*/\]$", nid)
    if m:
        return _strip_quote_pair(m.group(1))

    return None


def _extract_mermaid_node_label(nid: str) -> str | None:
    """从 Mermaid 节点定义中提取可读标签（含转义残留清洗，R67）。

    所有节点注册路径（独立定义行 / 连线端点）统一经由本函数取标签，
    因此 ``\\"`` 之类的转义序列在这里一次性还原，渲染端不会再把
    反斜杠原样印到图上。
    """
    raw = _extract_mermaid_node_label_raw(nid)
    if raw is None:
        return None
    return _unescape_label_text(raw)


def _register_node_if_match(line: str, nodes_map: dict) -> None:
    """尝试将一行 Mermaid 代码识别为独立节点定义并注册到 nodes_map。

    支持的节点定义语法（Mermaid flowchart 标准）：
      N1["矩形标签"]    N1("圆角标签")    N1{{"六边形"}}
      N1{"菱形标签"}    N1(("圆形"))      N1>"非对称"]
      N1[/"平行左"/]    N1(["子图"])

    Args:
        line: 已 strip 的 Mermaid 行
        nodes_map: 待填充的节点字典（会被原地修改）
    """
    if not line or _find_arrow_token(line) is not None:
        return
    # 快速过滤：不含括号/方括号/大括号的肯定不是节点定义
    if not any(c in line for c in "[{("):
        return

    # 尝试提取标签（如果没有标签，说明不是节点定义行）
    label = _extract_mermaid_node_label(line)
    if not label:
        return

    plain_id = _strip_mermaid_node_brackets(line)
    if not plain_id:
        return
    # ID 必须是合法 Mermaid 节点 ID（字母/下划线开头）
    if not re.match(r"^[A-Za-z_]\w*$", plain_id):
        return

    # 注册：统一以纯 ID（plain_id）作为 nodes_map 的 key，
    # 与连线端点归一化（_norm_endpoint）保持完全一致，
    # 避免「独立定义行（id=纯ID）+ 纯 ID 边（端点带标签形态）」形态下
    # 邻接表对不上导致流程图被拉成超宽细条（BUG-2）。
    nodes_map[plain_id] = {
        "id": plain_id,
        "label": label,
        "type": "decision" if ("{" in line) or _is_decision_label(label) else "process",
        "shape": "diamond" if ("{" in line) else "rect",
    }

def _strip_mermaid_node_brackets(nid: str) -> str:
    """从 Mermaid 节点定义中提取纯 ID 名称（去除方括号/圆括号/大括号包裹的标签部分）。

    示例：
      - A1["测量放线"]   -> A1
      - A1(测量放线)     -> A1
      - A1{"判定"}      -> A1
      - A1(("圆形"))    -> A1
      - A1              -> A1

    Args:
        nid: Mermaid 节点 ID 字符串

    Returns:
        纯 ID 名称（不含标签包裹符号）
    """
    if not nid:
        return nid
    # 匹配 ID 起始：字母/下划线 + 字母数字下划线
    m = re.match(r"^([A-Za-z_]\w*)\s*[\[\(\{]", nid)
    if m:
        return m.group(1)
    # 2026-09-13：兼容行尾分号/逗号（AI 常见输出 A --> B;）
    return re.sub(r"[\s;,]+$", "", nid) or nid

_ESCAPES = (
    ("\\\"", '"'),
    ("\\'", "'"),
    ("\\\\", "\\"),
)


def _unescape_label_text(text: str | None) -> str | None:
    """清洗标签里的转义残留：``\\"`` → ``"``、``\\'`` → ``'``、``\\\\`` → ``\\``。

    AI 偶尔把 JSON 风格的转义序列原样写进 Mermaid 节点标签（实测样张出现
    ``CheckBIM -- \\"通过\\"``），渲染时反斜杠会原样印在图上。此处只做
    机械的转义还原，不做任何语义改写；非字符串原样返回 None。
    """
    if not isinstance(text, str) or not text:
        return text
    for raw, rep in _ESCAPES:
        if raw in text:
            text = text.replace(raw, rep)
    return text


def _strip_quote_pair(s: str) -> str:
    """仅当引号**成对包裹**整段文本时才剥掉（R67）。

    旧实现 ``strip('"').strip("'")`` 会把标签内容里合法的收尾引号一并剥掉，
    留下 ``CheckBIM -- "通过`` 这类引号不闭合的残串。
    """
    s = (s or "").strip()
    if len(s) >= 2:
        for lq, rq in (('"', '"'), ("'", "'"), ("\u201c", "\u201d")):
            if s[0] == lq and s[-1] == rq:
                return s[1:-1].strip()
    return s


def _clean_edge_label(raw: str | None) -> str | None:
    """清洗边标签：还原转义残留 → 成对剥包裹引号 → 去空白；空串返回 None。

    ✅ R67：先还原转义再剥引号（顺序反了 ``\\"通过\\"`` 会留下残引号），
    剥引号复用 `_strip_quote_pair`（只剥成对包裹，内容中合法引号保留）。
    """
    if not raw:
        return None
    s = _unescape_label_text(str(raw)).strip()
    s = _strip_quote_pair(s).strip()
    return s or None


# Mermaid 连线统一解析正则（2026-09-13 增强）：
#   A --> B            标准箭头
#   A -- 文本 --> B    中段文本标签（引号可选，含中英文引号）
#   A -->|文本| B      管道标签
#   A -.-> B / A -. 文本 .-> B   虚线箭头（可带标签）
#   A ==> B / A == 文本 ==> B    粗线箭头（可带标签）
#   A --- B            无箭头连线
_EDGE_LINE_RE = re.compile(
    r"^\s*(?P<from>.+?)\s*"
    r"(?:"
    r"--\s*(?P<l1>\"[^\"]*\"|'[^']*'|\u201c[^\u201d]*\u201d|[^\s\"'\u201c\u201d>=][^\"'\u201c\u201d>=]*?)\s*-->"
    r"|-->"
    r"|-\s*\.\s*(?P<l2>\"[^\"]*\"|'[^']*'|\u201c[^\u201d]*\u201d|[^\s\"'\u201c\u201d>=][^\"'\u201c\u201d>=]*?)\s*\.\s*->"
    r"|-\.->"
    r"|==\s*(?P<l3>\"[^\"]*\"|'[^']*'|\u201c[^\u201d]*\u201d|[^\s\"'\u201c\u201d>=][^\"'\u201c\u201d>=]*?)\s*==>"
    r"|==>"
    r"|---"
    r")\s*(?P<to>.+?)\s*$"
)

# 行内连线记号（用于链式解析的下一段定位）
_ARROW_TOKENS = ("-->", "-.->", "==>", "---")


def _find_arrow_token(line: str) -> int | None:
    """返回行内最先出现的连线记号位置（--> / -.-> / ==> / --- 及带标签变体），无则 None。"""
    if not line:
        return None
    for tok in _ARROW_TOKENS:
        idx = line.find(tok)
        if idx >= 0:
            return idx
    # 带标签的虚线/粗线变体（-. 文本 .-> / == 文本 ==>）没有连续记号，
    # 必须用统一正则识别，否则被当作节点定义行而丢边（2026-09-13）。
    m = _EDGE_LINE_RE.match(line)
    if m:
        arrow_start = m.end("from")
        while arrow_start < len(line) and line[arrow_start] in " \t":
            arrow_start += 1
        return arrow_start
    return None


def _split_mermaid_edge(line: str) -> tuple[str, str | None, str] | None:
    """解析 Mermaid 边定义，支持多种箭头与标签语法。

    Returns:
        (from_id, edge_label or None, to_id)，解析失败返回 None
    """
    line = line.strip()
    if not line:
        return None

    # 1) 统一正则解析（覆盖中段标签、虚线/粗线变体、管道标签）
    m = _EDGE_LINE_RE.match(line)
    if m:
        edge_label = _clean_edge_label(
            m.group("l1") or m.group("l2") or m.group("l3")
        )
        to_raw = m.group("to").strip()
        # 管道标签 -->|文本| B：正则的 to 部分会携带，需再剥离
        pm = re.match(r"^\s*\|([^|]*)\|\s*(.*)$", to_raw)
        if pm:
            if not edge_label:
                edge_label = _clean_edge_label(pm.group(1))
            to_raw = pm.group(2).strip()
        from_id = m.group("from").strip()
        to_id = to_raw
        if not from_id or not to_id:
            return None
        return from_id, edge_label, to_id

    # 2) 兜底：旧式 --> 切分（兼容正则未覆盖的形态）
    if "-->" not in line:
        return None

    arrow_idx = line.find("-->")
    if arrow_idx < 0:
        return None

    left = line[:arrow_idx].strip()
    right = line[arrow_idx + 3 :].strip()

    edge_label: str | None = None

    # 标签在右侧首部:  -->|label| B
    label_m = re.match(r"^\s*\|([^|]*)\|\s*(.*)$", right)
    if label_m:
        edge_label = label_m.group(1).strip() or None
        right = label_m.group(2).strip()
    else:
        # 兼容 A -->|label| 之后直接接节点（label 在右半部分首部）
        label_m = re.match(r"^\s*\|([^|]*)\|\s*$", right)
        if label_m:
            edge_label = label_m.group(1).strip() or None
            right = ""
        # 清理左侧可能存在的连线样式后缀
        left = re.sub(r"\s+---\s*$", "", left)

    # 清理左侧可能存在的 |...| 形式的标签（少数语法）
    label_m_left = re.match(r"^(.*?)\s*\|([^|]*)\|\s*$", left)
    if label_m_left and not edge_label:
        left = label_m_left.group(1).strip()
        edge_label = label_m_left.group(2).strip() or None

    from_id = left
    to_id = right

    if not from_id or not to_id:
        return None

    return from_id, edge_label, to_id

def _parse_flowchart_structure(mermaid_code: str) -> dict:
    """解析 Mermaid 流程图，提取节点、连线、决策分支、并行/汇合结构。

    Returns:
        dict: {
            "direction": "TD" | "LR",
            "nodes": [{"id": str, "label": str, "type": "process"|"decision"|"start"|"end"}],
            "edges": [{"from": str, "to": str, "label": str|None}],
        }
    """
    direction = "TD"
    nodes_map: dict[str, dict] = {}
    edges: list[dict] = []

    for raw in mermaid_code.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("flowchart") or line.startswith("graph"):
            tokens = line.split()
            if len(tokens) >= 2 and tokens[1].upper() in ("TD", "TB", "LR", "RL", "BT"):
                direction = tokens[1].upper()
                if direction == "TB":
                    direction = "TD"
            continue
        # BUG 修复：end 仅在精确匹配（subgraph 结束关键字）时跳过。
        # 旧逻辑 line.startswith("end") 会吞掉独立节点定义行 end(["结束"])，
        # 导致 end 节点标签丢失、渲染时显示英文 "end"。
        if line in ("end", "end;"):
            continue
        if _find_arrow_token(line) is None:
            # 非连线行：跳过纯关键字行（subgraph/title/classDef/class/linkStyle/style）。
            # 注意 title/class/style 等过滤必须带后缀（空格/冒号），
            # 避免误吞同名节点定义行（如 title["标题"]、classNode["分类"]）。
            if (
                line.startswith("subgraph")
                or line.startswith("title ")
                or line.startswith("title:")
                or line.startswith("classDef")
                or line.startswith("class ")
                or line.startswith("linkStyle")
                or line.startswith("style ")
            ):
                continue
            # === 修复：独立节点定义行注册 ===
            # Mermaid 标准格式中节点定义（如 N1["施工准备"]）和连线（N1 --> N2）是分开的。
            # 原先代码只处理含 "-->" 的连线行，导致节点标签从未被注册进 nodes_map，
            # 后续纯 ID 连线无法关联到带标签定义，label 就回退成了纯 ID（N1, N2...）。
            # 现在在跳过 --> 之前先识别独立节点定义行并注册。
            _register_node_if_match(line, nodes_map)
            continue

        # 处理连线（支持带标签 -->|label| 与不带标签 -->；支持节点 ID 中带方括号标签）
        #
        # === BUG-2 修复：端点归一化（纯 ID 与「带标签形态」统一为纯 ID）===
        # 旧逻辑在 _resolve_id 中对「带标签端点」（如 N1["A"]）直接返回带标签完整串，
        # 而独立定义行注册的节点 id 是纯 ID（N1），导致边端点（N1["A"]）与节点 id（N1）
        # 对不上 → 邻接表收不到边 → 所有节点 in_degree=0 → 全部排到层级 0 →
        # 画布被拉成 6550×710 的超宽细条（json_nodes 经 flowchart_json_to_mermaid
        # 生成的「独立定义行 + 纯 ID 边」形态即触发此缺陷）。
        # 新逻辑：无论端点是纯 ID 还是带标签形态都归一化为纯 ID；若端点本身携带标签
        # 且节点尚未注册，则用该标签补全注册（既修复超宽，又不丢失内联节点标签）。
        def _norm_endpoint(nid_raw: str) -> tuple[str, str | None]:
            plain = _strip_mermaid_node_brackets(nid_raw)
            lbl = _extract_mermaid_node_label(nid_raw)
            return plain, lbl

        # ✅ BUG 修复：支持同一行内的链式连线 `A --> B --> C`（AI 很常用的写法）。
        #    旧实现只按**第一个** "-->" 切分：右半段 `B["基础"] --> C["竣工"]` 被整体
        #    当作端点字符串，`_strip_mermaid_node_brackets` 取不到纯 ID 时原样返回，
        #    于是注册出一个名为 `B["基础"] --> C["竣工"]` 的**幽灵节点**，
        #    真正的 B→C 这条边被静默丢弃 —— 流程图画出来是断开的。
        #    这里按箭头逐段剥离，每段产出一条边。
        chain = line
        _guard = 0
        while chain and _find_arrow_token(chain) is not None and _guard < 64:
            _guard += 1
            edge_info = _split_mermaid_edge(chain)
            if not edge_info:
                break
            from_id_raw, edge_label, to_id_raw = edge_info
            if not from_id_raw or not to_id_raw:
                break
            # 若右端仍含连线记号，则本段端点只取记号之前的片段，其余部分作为下一段继续解析
            _next_arrow = _find_arrow_token(to_id_raw)
            if _next_arrow is not None:
                seg_raw = to_id_raw[:_next_arrow].strip()
                chain = seg_raw + to_id_raw[_next_arrow:]
            else:
                seg_raw = to_id_raw
                chain = ""
            if not seg_raw:
                break

            from_plain, from_lbl = _norm_endpoint(from_id_raw)
            to_plain, to_lbl = _norm_endpoint(seg_raw)

            # 端点引用的节点若尚未注册（仅出现在边里），用端点携带的标签（若有）注册，
            # 否则回退为纯 ID 本身作为标签。
            for plain, lbl in ((from_plain, from_lbl), (to_plain, to_lbl)):
                if plain not in nodes_map:
                    label = lbl if lbl else plain
                    is_decision = bool(lbl) and _is_decision_label(lbl)
                    nodes_map[plain] = {
                        "id": plain,
                        "label": label,
                        "type": "decision" if is_decision else "process",
                        "shape": "diamond" if is_decision else "rect",
                    }

            edges.append({"from": from_plain, "to": to_plain, "label": edge_label})

    # ✅ 2026-09-18 横向流程图样张改版：自动推断节点变体（variant）。
    #   Mermaid 载荷禁用 classDef/style（管线会删样式指令），变体信息无法显式携带，
    #   这里按结构+关键词推断，渲染器据此套用样张配色：
    #   · 唯一零入度节点 → start；唯一零出度节点 → end（深藏青）
    #   · 标签含「关键/重点/核心」→ highlight（琥珀高亮，文档 8.1/8.2 提示词约定）
    #   · decision（菱形/判定关键词）已在注册时设置，优先级最高，不被覆盖
    in_deg: dict[str, int] = {nid: 0 for nid in nodes_map}
    out_deg: dict[str, int] = {nid: 0 for nid in nodes_map}
    for e in edges:
        if e["to"] in in_deg:
            in_deg[e["to"]] += 1
        if e["from"] in out_deg:
            out_deg[e["from"]] += 1
    _starts = [nid for nid, d in in_deg.items() if d == 0]
    _ends = [nid for nid, d in out_deg.items() if d == 0]
    if len(_starts) == 1 and nodes_map[_starts[0]]["type"] == "process":
        nodes_map[_starts[0]]["type"] = "start"
    if len(_ends) == 1 and nodes_map[_ends[0]]["type"] == "process":
        nodes_map[_ends[0]]["type"] = "end"
    for _n in nodes_map.values():
        if _n["type"] == "process" and any(
            kw in str(_n.get("label") or "") for kw in ("关键", "重点", "核心")
        ):
            _n["type"] = "highlight"

    return {
        "direction": direction,
        "nodes": list(nodes_map.values()),
        "edges": edges,
    }

LANE_PITCH_DESIGN_PX = 16  # design px：每条边在通道带内的最小占位（连线宽的 8 倍）


def _plan_edge_lanes(
    levels: dict[str, int],
    edges: list[dict],
    *,
    horizontal: bool,
    base_gap: float,
    lane_pitch: int = LANE_PITCH_DESIGN_PX,
    reversed_: bool = False,
) -> tuple[dict[int, int], dict[int, list[int]], dict[int, float], dict[int, int]]:
    """R67 连线通道规划：把每条边分派到「间隙带」并在带内分配通道序号。

    旧实现每条边的水平段都落在 ``(sy+ey)/2`` —— 同一层间间隙内的 N 条边
    在同一 y 上重叠成一条贯穿全图的长轨（分支多时整张图像「公共母线」，
    无法分辨哪条边连接哪两个节点）。现按带分道：

    · 前进边（目标层更深）→ 目标层上方的间隙带（band = 目标层号 - 1）；
    · TD 同层边 → 源层下方的间隙带（band = 源层号，末层即图底预留带）；
      LR 的同层与回退边 → 图底母线带（band = -1）；
    · TD 跨层回退边（目标层更浅）不分带 —— 保持旧直连路由（相邻层回退
      直连本就最短，绕行母线反而会把箭头藏进中间节点后面）；
    · 带内按边数均分通道，间隙净高不足 ``(n+1)*lane_pitch`` 时自动加高，
      由调用方用返回的 ``band_gap`` 参与层坐标累加与画布尺寸计算。

    Returns:
        ``(band_of, band_edges, band_gap, edge_slot)``：
        band_of: 边下标 -> 带号（未分带的边不在其中）；
        band_edges: 带号 -> 边下标列表（已排序）；
        band_gap: 带号 -> 该带需要的间隙（design px，>= base_gap）；
        edge_slot: 边下标 -> 带内序号（0 基）。
    """
    band_of: dict[int, int] = {}
    band_edges: dict[int, list[int]] = {}
    for i, e in enumerate(edges):
        lf = levels.get(e["from"])
        lt = levels.get(e["to"])
        if lf is None or lt is None:
            continue
        if lt > lf:
            b = lt - 1
        elif reversed_:
            b = lf
        elif horizontal:
            b = -1  # LR 同层/回退边：图底母线带
        elif lt == lf:
            b = lf  # TD 同层边：源层下方间隙（末层即图底预留带）
        else:
            continue  # TD 跨层回退边：不分带，保持旧直连路由
        band_of[i] = b
        band_edges.setdefault(b, []).append(i)
    band_gap: dict[int, float] = {}
    edge_slot: dict[int, int] = {}
    for b, idxs in band_edges.items():
        idxs.sort(key=lambda i: (
            levels.get(edges[i]["from"], 0),
            levels.get(edges[i]["to"], 0),
            str(edges[i]["from"]), str(edges[i]["to"]),
        ))
        for k, i in enumerate(idxs):
            edge_slot[i] = k
        band_gap[b] = max(base_gap, (len(idxs) + 1) * lane_pitch)
    return band_of, band_edges, band_gap, edge_slot


def _draw_diamond_label(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    w: float,
    h: float,
    text: str,
    font,
    fill: str,
    scale: float,
    min_font_px: int = 12,
    max_lines: int = 3,
) -> None:
    """在菱形（外接盒 w×h、中心 cx,cy）的内接矩形内自适应绘制标签。

    R67：菱形文字按**内接矩形**（外接盒的一半再扣内边距）排版 —— 菱形内任何
    水平线段都不会超过该宽度，文字因此绝不会越出菱形轮廓。旧实现按整个外接盒
    排版，长标签横向溢出压到相邻节点上（实测样张 ``CheckBIM -- "通过"`` 溢出
    菱形两侧各约 1/3 个字位）。字号自节点字号起逐级缩小直到装得下，
    到最小字号仍装不下时按省略号收口。

    Args:
        draw: PIL ImageDraw
        cx, cy: 菱形中心（device px）
        w, h: 菱形外接盒宽高（device px）
        text: 标签文本
        font: 节点基准字体（取其字号作为自适应起点）
        fill: 文字颜色
        scale: 渲染超采样倍率
        min_font_px: 最小字号（design px）
        max_lines: 最大行数
    """
    inset = int(4 * scale)
    box_w = max(int(w / 2 - inset * 2), int(8 * scale))
    box_h = max(int(h / 2 - inset * 2), int(8 * scale))
    start = int(getattr(font, "size", 0) or int(16 * scale))
    floor = max(int(min_font_px * scale), 8)
    chosen = None
    size = start
    while size >= floor:
        f = _image_font(size, bold=True)
        if f is not None:
            lines = _wrap_text(draw, text, f, box_w, max_lines=max_lines)
            line_h = max(16, _text_height(draw, "国", f) + 4)
            if line_h * len(lines) <= box_h:
                chosen = (f, lines, line_h)
                break
        size -= 2
    if chosen is None:
        f = _image_font(floor, bold=True) or font
        lines = _wrap_text(draw, text, f, box_w, max_lines=max_lines)
        line_h = max(16, _text_height(draw, "国", f) + 4)
        chosen = (f, lines, line_h)
    f, lines, line_h = chosen
    total_h = line_h * len(lines)
    ty = cy - total_h / 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=f)
        text_w = bbox[2] - bbox[0]
        draw.text((cx - text_w / 2, ty), line, fill=fill, font=f)
        ty += line_h


@_render_hq
def _render_flowchart_image_v2(
    mermaid_code: str, variant_overrides: dict[str, str] | None = None
) -> BytesIO | None:
    """商业级流程图渲染器 v2

    特性：
    - 严格区分决策菱形与流程矩形
    - 自动判断 TD / LR 布局（2026-10-11 起统一样张默认纵向 TD，显式 LR/RL/BT 仍生效）
    - 带标签的分支连线（-->|label|）
    - ✅ 2026-10-11 R66 流程图统一样式规范（双模式，与前端 mermaid.js 主题同套配色）：
        全部节点 = 淡紫底 #f0edff + 紫边框 #a89aff + 深灰文字 #333333；
        start/end 加粗边框、highlight 加深边框，仅以边框粗细/深浅区分，不再用色块；
        单链几何按方向取参：纵向（TD/BT）节点 280×80、行距 40，
        横向（LR/RL）节点 200×80、列距 40
    - 自动避让连线，箭头清晰
    - 增强 v3.0: 使用 @_render_hq 实现 2.5x 超采样抗锯齿，300 DPI 输出

    Args:
        mermaid_code: Mermaid 流程图代码
        variant_overrides: 节点 ID → 变体（start/end/process/decision/highlight）。
            chart-json 载荷的显式 variant 经此传入（优先级高于解析端自动推断）。
    """
    if not mermaid_code:
        return None

    parsed = _parse_flowchart_structure(mermaid_code)
    nodes = parsed["nodes"]
    edges = parsed["edges"]
    if len(nodes) < 2 or not edges:
        return None
    if variant_overrides:
        for n in nodes:
            t = variant_overrides.get(n["id"])
            if t in ("start", "end", "process", "decision", "highlight"):
                n["type"] = t
    logger.info(
        "[PIL v2渲染] 流程图渲染启动: 节点数=%d 连线数=%d 方向=%s",
        len(nodes),
        len(edges),
        parsed["direction"],
    )

    direction = parsed["direction"]
    decision_ids = {n["id"] for n in nodes if n["type"] == "decision"}


    # ---- 尺寸参数 ----
    # ✅ R66（2026-10-11 流程图统一样式规范 · 双模式）：单链几何按方向取参 ——
    #    纵向（TD/BT）节点 280×80、行距 40；横向（LR/RL）节点 200×80、列距 40。
    #    副轴间距规范未约束，保留旧值；菱形非单链元素，保留 200×80。
    padding = 40
    node_h = 80
    diamond_w = 200
    diamond_h = 80
    title_h = 60
    caption_h = 32
    if direction in ("LR", "RL"):
        node_w = 200
        row_gap = 32
        col_gap = 40
    else:
        node_w = 280
        row_gap = 40
        col_gap = 60

    # ---- 自动布局（基于 BFS 层级）----
    # 构建邻接表
    adj: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    in_degree: dict[str, int] = {n["id"]: 0 for n in nodes}
    for e in edges:
        if e["from"] in adj and e["to"] in adj:
            adj[e["from"]].append(e["to"])
            in_degree[e["to"]] = in_degree.get(e["to"], 0) + 1

    # BFS 计算层级（处理简单图，支持多个起点；带环路保护）
    levels: dict[str, int] = {}
    queue = [nid for nid, d in in_degree.items() if d == 0]
    if not queue:
        queue = [nodes[0]["id"]]
    for nid in queue:
        levels[nid] = 0
    head = 0
    max_iterations = len(nodes) * len(nodes) + 100  # 环路保护：避免无限循环
    iterations = 0
    while head < len(queue):
        cur = queue[head]
        head += 1
        iterations += 1
        if iterations > max_iterations:
            # 环路保护：剩余节点直接分配层级
            for n in nodes:
                if n["id"] not in levels:
                    levels[n["id"]] = max(levels.values()) + 1 if levels else 0
            break
        for nb in adj.get(cur, []):
            if nb not in levels:
                levels[nb] = levels[cur] + 1
                queue.append(nb)
            # 不再更新已存在节点的层级，避免循环导致无限传播

    # 同层级归组
    if not levels:
        levels = {n["id"]: 0 for n in nodes}
    max_level = max(levels.values()) if levels else 0
    level_groups: dict[int, list[str]] = {lv: [] for lv in range(max_level + 1)}
    for nid, lv in levels.items():
        level_groups[lv].append(nid)
    for lv in level_groups:
        level_groups[lv].sort()

    # ✅ BUG 修复：RL / BT 方向此前被当作 TD 处理 —— 层级仍自上而下（或自左向右）
    #    排布、箭头也一律朝下（朝右），于是 "flowchart BT"（自下而上）画出来是
    #    自上而下、"flowchart RL"（从右向左）画出来是从左到右，与代码声明的方向
    #    完全相反（用户按 BT 排版，导出的图却把"结束"画在最下方）。
    #    这里把四种方向归一为「主轴（水平/垂直）+ 是否反向」两个正交维度。
    is_horizontal = direction in ("LR", "RL")
    is_reversed = direction in ("RL", "BT")

    if is_horizontal:
        # 横向布局：层级 = 列
        max_cols = max_level + 1
        max_rows = max(len(g) for g in level_groups.values())
        col_w = max(node_w, diamond_w) + col_gap
        row_h = max(node_h, diamond_h) + row_gap
    else:
        # 纵向布局：层级 = 行
        max_rows = max_level + 1
        max_cols = max(len(g) for g in level_groups.values())
        col_w = max(node_w, diamond_w) + col_gap
        row_h = max(node_h, diamond_h) + row_gap

    # ---- R67 连线通道规划：同一间隙内多条边各占一条通道，不再共线并轨 ----
    _LANE_PITCH = 16  # design px：每条边在带内的最小占位（连线宽 2 design px 的 8 倍）
    _base_gap = col_gap if is_horizontal else row_gap
    _band_of, _band_edges, _band_gap, _edge_slot = _plan_edge_lanes(
        levels,
        edges,
        horizontal=is_horizontal,
        base_gap=_base_gap,
        lane_pitch=_LANE_PITCH,
        reversed_=is_reversed,
    )

    # 层坐标（level 空间，design px）：第 lv 层起点 = 前层起点 + 层距 + 加宽量。
    # _level_span（col_w/row_h）本身已含基准间距（R66 几何），通道带只叠加
    # 「超出基准的部分」—— 单链（每带 1 条边）加宽量为 0，几何与 R66 逐字一致。
    _level_span = col_w if is_horizontal else row_h
    _band_extra = {
        _b: max(0.0, _g - _base_gap) for _b, _g in _band_gap.items()
    }
    _coord: list[float] = [0.0] * (max_level + 1)
    for _lv in range(1, max_level + 1):
        _coord[_lv] = _coord[_lv - 1] + _level_span + _band_extra.get(_lv - 1, 0.0)
    _content_span = _coord[max_level] + _level_span
    # 末层之后仍有通道带（TD 末层出发的同层/回退边、LR 图底母线）→ 图底预留
    _tail_gap = _band_gap.get(max_level if not is_horizontal else -1, 0.0)

    if is_horizontal:
        total_w = padding * 2 + _content_span
        total_h = title_h + padding * 2 + max_rows * row_h + _tail_gap + caption_h
    else:
        total_w = padding * 2 + max_cols * col_w - col_gap
        total_h = title_h + padding * 2 + _content_span + _tail_gap + caption_h

    # 使用 2.5x 缩放渲染 + @_render_hq 保持原分辨率并添加 300 DPI 元数据
    # 2.5x 渲染 + 不降采样 = 真实超采样抗锯齿，最终 DOCX 插入时 DPI 感知缩放到合适尺寸
    scale = 2.5

    img = Image.new("RGB", (int(total_w * scale), int(total_h * scale)), "white")
    draw = ImageDraw.Draw(img)
    font_title = _image_font(int(20 * scale), bold=True)
    # ✅ R66：节点字号纵向 16px / 横向 15px（统一样式规范 §1.2/§3）
    font_node = _image_font(int((15 if is_horizontal else 16) * scale), bold=True)
    font_edge = _image_font(int(13 * scale))
    font_caption = _image_font(int(13 * scale))

    # ---- 标题 ----
    # 从 Mermaid 中提取 title（如果有）
    title = "流程图"
    for raw in mermaid_code.split("\n"):
        if raw.strip().startswith("title"):
            title = raw.strip()[5:].strip()
            break

    draw.text((int(padding * scale), int(18 * scale)), title, fill="#1E3A5F", font=font_title)

    # ---- 计算节点位置 ----
    def _level_offset(lv: int) -> float:
        """level 空间层起点 → 渲染空间偏移（design px；反向图沿主轴镜像）。"""
        if not is_reversed:
            return _coord[lv]
        return _content_span - _coord[lv] - _level_span

    positions: dict[str, tuple[float, float, float, float]] = {}
    for lv, ids in level_groups.items():
        n_in_level = len(ids)
        # RL / BT 反向时层级序号翻转：layer0（起点）落在最右 / 最下
        # （R67：层起点改由 _coord 累加得出 —— 层间间隙按通道数加高后不再是定值）
        if is_horizontal:
            # BUG 修复：LR（横向）布局中列 x 必须固定按层级推进
            # （padding + lv * col_w），与画布 total_w 的计算方式一致。
            # 旧公式额外叠加 (max_cols-1)*col_w/2 - n_in_level*col_w/2 的
            # "水平居中"项，会让列间距小于 col_w 甚至超出画布（数值验证：
            # 层级规模 [1,2,1] 时相邻列水平重叠 50px）。居中只应作用于 y 轴。
            start_x = (padding + _level_offset(lv)) * scale
            for i, nid in enumerate(ids):
                x = start_x
                y = (
                    title_h * scale
                    + padding * scale
                    + (max_rows - n_in_level) * row_h * scale / 2
                    + i * row_h * scale
                )
                w = diamond_w * scale if nid in decision_ids else node_w * scale
                h = diamond_h * scale if nid in decision_ids else node_h * scale
                if nid in decision_ids:
                    # R67：菱形按自身尺寸在列内居中 —— 视觉中心与同列矩形对齐，
                    # 连线锚点（fx+fw/2）不再偏离列中心（旧实现菱形贴列左缘，
                    # 与上下矩形错开 (node_w-diamond_w)/2*scale）。
                    x += (node_w * scale - w) / 2
                    y += (node_h * scale - h) / 2
                positions[nid] = (x, y, w, h)
        else:
            start_y = (title_h + padding + _level_offset(lv)) * scale
            start_x = padding * scale + (max_cols - n_in_level) * col_w * scale / 2
            for i, nid in enumerate(ids):
                x = start_x + i * col_w * scale
                y = start_y
                w = diamond_w * scale if nid in decision_ids else node_w * scale
                h = diamond_h * scale if nid in decision_ids else node_h * scale
                if nid in decision_ids:
                    x += (node_w * scale - w) / 2
                    y += (node_h * scale - h) / 2
                positions[nid] = (x, y, w, h)

    node_lookup = {n["id"]: n for n in nodes}

    # ---- 绘制连线（先画线，再画节点，确保节点覆盖在连线上方）----
    # ✅ 2026-10-11 统一样张：深灰连线 #333333（与 mermaid default 主题同款），无阴影
    # ✅ R67：同一间隙带内每条边的水平段落在各自的通道上（_lane），
    #    不再全部挤在 (sy+ey)/2 一条线上并轨。
    arrow_color = "#333333"
    edge_label_bg = "#FFFFFF"
    # LR 母线带（band=-1，同层/回退边绕行图底）的基准 y 与带内通道
    _bus_base = title_h + padding + max_rows * row_h - row_gap
    _bus_gap_total = _band_gap.get(-1, 0.0)
    _bus_n = len(_band_edges.get(-1, []))

    def _lane_for(ei: int):
        """返回 (通道坐标 device px, 轴向, 是否母线)。

        反向图（RL/BT）或带外边（TD 跨层回退）返回 None —— 保持旧 mid 路由。
        """
        if is_reversed:
            return None
        band = _band_of.get(ei)
        if band is None:
            return None
        idxs = _band_edges.get(band, [])
        frac = (_edge_slot.get(ei, 0) + 1) / (len(idxs) + 1)
        if band < 0:
            # LR 图底母线：水平通道，y 在内容区之下均分
            return (_bus_base + frac * _bus_gap_total) * scale, "y", True
        extra_b = _band_extra.get(band, 0.0)
        if is_horizontal:
            # LR：竖向通道 x，位于第 band 列节点右缘与 band+1 列节点左缘之间
            inter = (col_w - node_w) + extra_b
            return ((padding + _coord[band] + node_w + frac * inter) * scale,
                    "x", False)
        # TD：横向通道 y，位于第 band 行节点底与下一行节点顶之间的净空内。
        # 同层边（band = 源层号）的通道即其绕行母线 —— 箭头朝上进入目标底边。
        is_bus = levels.get(edges[ei]["from"]) == levels.get(edges[ei]["to"])
        inter = (row_h - node_h) + extra_b
        return ((title_h + padding + _coord[band] + node_h + frac * inter) * scale,
                "y", is_bus)

    for _ei, e in enumerate(edges):
        if e["from"] not in positions or e["to"] not in positions:
            continue
        fx, fy, fw, fh = positions[e["from"]]
        tx, ty, tw, th = positions[e["to"]]
        # ✅ sign = 连线前进方向（+1 沿 +x/+y；-1 沿 -x/-y，即 RL / BT 反向）
        sign = -1 if is_reversed else 1
        # 计算源点和目标点（反向时锚在节点的另一侧）
        if is_horizontal:
            sx = fx if is_reversed else fx + fw
            ex = tx + tw if is_reversed else tx
            sy = fy + fh / 2
            ey = ty + th / 2
        else:
            sx = fx + fw / 2
            ex = tx + tw / 2
            sy = fy if is_reversed else fy + fh
            ey = ty + th if is_reversed else ty

        # 绘制折线（带拐点）
        # ✅ 2026-09-18（横向流程图样张改版）：逆向/回退分支（如 验收 --不通过--> 前序
        #    工序）在 LR 布局下终点左边缘位于起点右边缘左侧，旧实现的中点折线会横穿
        #    中间节点被覆盖，回退边"消失"。改为走下方绕行总线、箭头朝上进入目标底边
        #    （与文档《横向生成与渲染方案》布局算法同款）。
        # ✅ R67：绕行母线按边分道（不再共用一条 bus_y）；TD 的同层/回退边
        #    同样改走源层下方的专用通道、箭头朝上进入目标底边（旧实现把
        #    折线中点落在 (sy+ey)/2，会横穿节点行本身）。
        _lane = _lane_for(_ei)
        # 同层边（通道带标记为母线）与 LR 几何异常回退边走绕行母线；
        # TD 跨层回退边不带通道，保持旧直连路由。
        _to_bus = _lane is not None and _lane[2]
        _backward_h = is_horizontal and (
            _to_bus or ex < sx - int(4 * scale)
        )
        _backward_v = (not is_horizontal) and _to_bus
        if _backward_h:
            _cx_f = fx + fw / 2
            _cx_t = tx + tw / 2
            _bot_f = fy + fh
            _bot_t = ty + th
        # 调整终点（在节点边缘前停住，留箭头空间）
        # BUG 修复（渲染质量）：以下箭头/标签几何量原为「未缩放的屏幕像素常量」
        # （8/2/7/14…），而本渲染器以 2.5x 超采样绘制、线宽却按 int(2*scale) 放大，
        # 于是 2.5x 画布上箭头仅约 14px 而线宽已达 5px —— 箭头几乎与线条重合、看不出
        # 方向；与同文件 _render_flowchart_with_edges（几何全部按 scale 缩放）风格割裂。
        # 统一乘 scale，保证任意缩放比例下箭头比例正确、粗细协调。
        arrow_len = int(14 * scale)   # 箭头长度（沿连线方向）
        arrow_half = int(7 * scale)   # 箭头半宽
        tip_gap = int(2 * scale)      # 箭头尖端回缩，避免盖住节点边框
        stub = int(8 * scale)         # 折线在节点边缘前的停靠距离
        if _backward_h:
            if _lane is not None and _lane[1] == "y":
                bus_y = _lane[0]
            else:
                bus_y = min(
                    max(_bot_f, _bot_t) + int(30 * scale),
                    int(total_h * scale) - int(caption_h * scale) - int(8 * scale),
                )
            path = [
                (_cx_f, _bot_f),
                (_cx_f, bus_y),
                (_cx_t, bus_y),
                (_cx_t, _bot_t + stub),
            ]
        elif _backward_v:
            # TD 同层/回退边：走源层下方专用通道，箭头朝上进入目标底边
            bus_y = _lane[0] if _lane is not None else sy + int(30 * scale)
            _cx_f = sx
            _cx_t = ex
            _bot_t = ty + th
            path = [
                (sx, sy),
                (sx, bus_y),
                (ex, bus_y),
                (ex, _bot_t + stub),
            ]
        elif is_horizontal:
            mid = _lane[0] if _lane is not None else (sx + ex) / 2
            path = [(sx, sy), (mid, sy), (mid, ey), (ex - sign * stub, ey)]
        else:
            mid = _lane[0] if _lane is not None else (sy + ey) / 2
            path = [(sx, sy), (sx, mid), (ex, mid), (ex, ey - sign * stub)]

        draw.line(path, fill=arrow_color, width=int(2 * scale))

        # 绘制箭头（使用实心三角形；反向时三角形翻转到另一侧）
        if _backward_h or _backward_v:
            # 箭头朝上，从下方进入目标节点底边
            draw.polygon(
                [
                    (_cx_t, _bot_t + tip_gap),
                    (_cx_t - arrow_half, _bot_t + tip_gap + arrow_len),
                    (_cx_t + arrow_half, _bot_t + tip_gap + arrow_len),
                ],
                fill=arrow_color,
            )
        elif is_horizontal:
            arrow_tip = (ex - sign * tip_gap, ey)
            arrow_base1 = (ex - sign * arrow_len, ey - arrow_half)
            arrow_base2 = (ex - sign * arrow_len, ey + arrow_half)
            draw.polygon([arrow_tip, arrow_base1, arrow_base2], fill=arrow_color)
        else:
            arrow_tip = (ex, ey - sign * tip_gap)
            arrow_base1 = (ex - arrow_half, ey - sign * arrow_len)
            arrow_base2 = (ex + arrow_half, ey - sign * arrow_len)
            draw.polygon([arrow_tip, arrow_base1, arrow_base2], fill=arrow_color)

        # 绘制边标签
        if e["label"]:
            if _backward_h or _backward_v:
                label_x = (_cx_f + _cx_t) / 2 - int(14 * scale)
                label_y = bus_y - int(20 * scale)
                _along_x = True
            elif is_horizontal:
                label_x = (_lane[0] if _lane is not None else (sx + ex) / 2) \
                    - int(14 * scale)
                label_y = (sy + ey) / 2 - int(16 * scale)
                _along_x = True
            else:
                label_x = (sx + ex) / 2 - int(14 * scale)
                label_y = (_lane[0] if _lane is not None
                           else sy + (ey - sy) / 2) - int(10 * scale)
                _along_x = False
            bbox = draw.textbbox((0, 0), e["label"], font=font_edge)
            tw_l = bbox[2] - bbox[0] + int(8 * scale)
            th_l = bbox[3] - bbox[1] + int(4 * scale)
            _lp = int(2 * scale)

            # BUG 修复（2026-09-13）：回边/长边标签可能落在同列节点框内被覆盖
            #   （如 A5 -- "否" --> A3 的"否"标签藏进上方节点矩形，导出图上不可见）。
            #   沿连线方向在相邻节点的间隙中寻找最近的可见位置，保持标签贴线。
            _lb = [label_x - _lp, label_y - _lp, label_x + tw_l, label_y + th_l]
            _node_boxes = list(positions.values())

            def _overlaps_any(b):
                for _nx, _ny, _nw, _nh in _node_boxes:
                    if not (b[2] <= _nx or b[0] >= _nx + _nw
                            or b[3] <= _ny or b[1] >= _ny + _nh):
                        return True
                return False

            if _overlaps_any(_lb):
                _step = (th_l if not _along_x else tw_l) + int(6 * scale)
                _best = None
                _best_dist = None
                for _d in range(1, 40):
                    for _sign in (1, -1):
                        _ny = label_y + _sign * _d * _step if not _along_x else label_y
                        _nx = label_x + _sign * _d * _step if _along_x else label_x
                        _cand = [_nx - _lp, _ny - _lp, _nx + tw_l, _ny + th_l]
                        if (_cand[1] < 0 or _cand[3] > img.height
                                or _cand[0] < 0 or _cand[2] > img.width):
                            continue
                        if not _overlaps_any(_cand):
                            _dist = abs(_ny - label_y) if not _along_x else abs(_nx - label_x)
                            if _best_dist is None or _dist < _best_dist:
                                _best = (_nx, _ny)
                                _best_dist = _dist
                    if _best:
                        break
                if _best:
                    label_x, label_y = _best
            draw.rectangle(
                [label_x - _lp, label_y - _lp, label_x + tw_l, label_y + th_l],
                fill=edge_label_bg,
                outline="#a89aff",
                width=1,
            )
            draw.text((label_x + _lp, label_y), e["label"], fill="#333333", font=font_edge)

    # ---- 绘制节点（最后画，置于最上层）----
    # ✅ R66（2026-10-11 流程图统一样式规范）：所有节点同色同框（淡紫底 #f0edff +
    #    紫边框 #a89aff + 深灰字 #333333），沿用统一样张「仅以边框粗细/深浅区分」机制；
    #    关键工序 highlight 用深紫加粗边框做语义强调（#a89aff 同系加深）。
    _LAVENDER_FILL = "#f0edff"
    _PURPLE_BORDER = "#a89aff"
    _PURPLE_BORDER_DARK = "#7C63D8"
    _NODE_TEXT = "#333333"
    node_pad = int(4 * scale)
    for nid, (x, y, w, h) in positions.items():
        node = node_lookup[nid]
        label = node["label"]
        if nid in decision_ids:
            # 菱形（决策）：淡紫底 + 紫边框
            # ✅ R67：菱形铺满自身形状盒（不再是 min(w,h)/2 的小方块），
            #    文字按内接矩形自适应，绝不越出菱形轮廓
            cx = x + w / 2
            cy = y + h / 2
            draw.polygon(
                [(cx, y), (x + w, cy), (cx, y + h), (x, cy)],
                fill=_LAVENDER_FILL,
                outline=_PURPLE_BORDER,
                width=max(1, int(1.5 * scale)),
            )
            _draw_diamond_label(
                draw,
                cx,
                cy,
                w,
                h,
                label,
                font_node,
                _NODE_TEXT,
                scale,
            )
        else:
            is_hl = node.get("type", "process") == "highlight"
            draw.rectangle(
                [x, y, x + w, y + h],
                fill=_LAVENDER_FILL,
                outline=_PURPLE_BORDER_DARK if is_hl else _PURPLE_BORDER,
                width=max(1, int(2.5 * scale)) if is_hl else max(1, int(1.5 * scale)),
            )
            _draw_text_center(
                draw,
                (x + node_pad, y + node_pad, x + w - node_pad, y + h - node_pad),
                label,
                font_node,
                _NODE_TEXT,
                max_lines=2,
            )

    # ---- 图例（几何随 scale 缩放，保证与正文节点视觉一致）----
    legend_y = int(total_h * scale) - int(24 * scale)
    legend_x = int(padding * scale)
    _lz = int(14 * scale)
    # 流程节点
    draw.rectangle(
        [legend_x, legend_y - _lz, legend_x + int(24 * scale), legend_y + int(2 * scale)],
        fill=_LAVENDER_FILL,
        outline=_PURPLE_BORDER,
        width=max(1, int(1.5 * scale)),
    )
    draw.text(
        (legend_x + int(30 * scale), legend_y - int(13 * scale)),
        "流程节点",
        fill="#1E3A5F",
        font=font_caption,
    )
    legend_x += int(100 * scale)
    # 决策节点
    cx = legend_x + int(12 * scale)
    cy = legend_y - int(6 * scale)
    _ld = int(10 * scale)
    draw.polygon(
        [(cx, cy - _ld), (cx + _ld, cy), (cx, cy + _ld), (cx - _ld, cy)],
        fill=_LAVENDER_FILL,
        outline=_PURPLE_BORDER,
        width=max(1, int(1.5 * scale)),
    )
    draw.text(
        (legend_x + int(30 * scale), legend_y - int(13 * scale)),
        "决策判定",
        fill="#1E3A5F",
        font=font_caption,
    )
    legend_x += int(100 * scale)
    # 关键工序（highlight，深紫加粗边框区分）
    draw.rectangle(
        [legend_x, legend_y - _lz, legend_x + int(24 * scale), legend_y + int(2 * scale)],
        fill=_LAVENDER_FILL,
        outline=_PURPLE_BORDER_DARK,
        width=max(1, int(2.5 * scale)),
    )
    draw.text(
        (legend_x + int(30 * scale), legend_y - int(13 * scale)),
        "关键工序",
        fill="#1E3A5F",
        font=font_caption,
    )

    paper_floor_note(
        "流程图",
        [font_title, font_node, font_edge, font_caption],
        img.size[0], img.size[1],
        primary=[font_node],
        suggest="（节点标签是主文字；分支多或层级深时图会被等比缩小）",
    )
    result_stream = _image_to_stream(img)
    size_kb = len(result_stream.getvalue()) // 1024
    logger.info(
        "[PIL v2渲染] 流程图渲染完成: 节点=%d 尺寸=%dKB DPI=300",
        len(nodes),
        size_kb,
    )
    return result_stream

def flowchart_json_to_mermaid(data: dict) -> str:
    """将 nodes + edges 结构转换为 Mermaid 流程图代码字符串

    用于在前端 Mermaid.js 预览和 mmdc 渲染中使用。
    支持简化格式：nodes 可以是字符串列表（["步骤1", "步骤2"]），
    此时自动按顺序生成连线；edges 缺失时同样生成顺序连线。

    Args:
        data: {"nodes": [...], "edges": [...], "title": "..."}
              nodes 支持两种格式：
                - 对象列表: [{"id": "A", "label": "开始", "type": "start"}, ...]
                - 字符串列表: ["开始", "处理", "结束"]

    Returns:
        Mermaid 流程图代码字符串
    """
    try:
        # ✅ 2026-09-18：兼容文档数据结构 {"steps":[...], "edges":[...]}（施工/工艺横向
        #    流程图 chart-json 载荷），steps 与 nodes 同义；节点 variant 与 type 同义。
        nodes_raw = data.get("nodes")
        if not nodes_raw and isinstance(data.get("steps"), list):
            nodes_raw = data["steps"]
        nodes_raw = nodes_raw or []
        edges_raw = data.get("edges", [])
        if not nodes_raw:
            return ""

        # === 规范化 nodes：统一为对象列表 ===
        norm_nodes: list[dict] = []
        for i, node in enumerate(nodes_raw):
            if isinstance(node, str):
                # 字符串节点：自动分配 ID
                nid = f"N{i+1}"
                norm_nodes.append({"id": nid, "label": node, "type": "process"})
            elif isinstance(node, dict):
                nid = node.get("id") or f"N{i+1}"
                label = node.get("label") or nid
                ntype = node.get("type") or node.get("variant") or "process"
                norm_nodes.append({"id": nid, "label": label, "type": ntype})
            else:
                nid = f"N{i+1}"
                norm_nodes.append({"id": nid, "label": str(node), "type": "process"})

        if len(norm_nodes) < 2:
            return ""

        # === 规范化 edges ===
        # 构建"数字索引 → 节点 ID"映射，因为 AI 经常输出 edges=[[0,1],[1,2]...] 形式
        # （用节点在 nodes 数组中的索引），需要自动映射为实际节点 ID（N1/N2/...）
        # BUG 修复：仅当值是纯数字且【不是】已有节点 ID 时才做索引映射。
        # AI 也常用数字字符串作节点 ID（如 "1"、"2"），旧逻辑会把真实 ID
        # 误当数组下标改写，产生错误拓扑（自环/错边）。
        node_ids = {n["id"] for n in norm_nodes}
        idx_to_id: dict[str, str] = {
            str(i): n["id"] for i, n in enumerate(norm_nodes)
        }

        def _resolve_endpoint(v: Any) -> str:
            s = str(v)
            if s in node_ids:
                return s  # 精确匹配已有节点 ID，优先于索引映射
            if s in idx_to_id:
                return idx_to_id[s]
            return s

        norm_edges: list[dict] = []
        edges_valid = bool(edges_raw) and isinstance(edges_raw, list)
        if edges_valid:
            for edge in edges_raw:
                if isinstance(edge, dict):
                    f_raw = edge.get("from")
                    t_raw = edge.get("to")
                    elabel = edge.get("label", "")
                    # BUG-3 修复：from/to 可能是 0 基索引整数（0 是合法节点下标），
                    # 旧逻辑 `if f and t:` 会把 from=0 误判为 Falsy 从而静默丢弃首条边，
                    # 导致拓扑断裂（如 {'from':0,'to':1} 被漏掉）。改用显式 None/空串判定。
                    if f_raw not in (None, "") and t_raw not in (None, ""):
                        # 自动把数字索引 from/to 转成节点 ID（优先精确 ID 匹配）
                        f = _resolve_endpoint(f_raw)
                        t = _resolve_endpoint(t_raw)
                        norm_edges.append({"from": f, "to": t, "label": elabel})
                elif isinstance(edge, (list, tuple)) and len(edge) >= 2:
                    f = _resolve_endpoint(edge[0])
                    t = _resolve_endpoint(edge[1])
                    norm_edges.append({"from": f, "to": t, "label": ""})
                elif isinstance(edge, str) and "-->" in edge:
                    # Mermaid 格式边（"A --> B" / "0 --> 1" / "A -->|标签| B"）
                    # ✅ BUG 修复：端点为"节点下标"（如 "0 --> 1"）时旧实现原样写入，
                    #    与自动生成的 N1/N2… 节点 ID 对不上 → 渲染器把 "0"/"1" 当新
                    #    节点注册，产出**幽灵节点 + 断链**（图上多出孤立小框、连线断开）。
                    #    现与 dict/list 边同一口径走 _resolve_endpoint，并清理行尾分号。
                    parts = edge.split("-->", 1)
                    f = _resolve_endpoint(parts[0].strip().rstrip(";").strip())
                    t_raw = parts[1].strip()
                    elabel = ""
                    if t_raw.startswith("|") and "|" in t_raw:
                        elabel = t_raw[1:t_raw.index("|")]
                        t_raw = t_raw[t_raw.index("|") + 1:]
                    t_raw = t_raw.strip().rstrip(";").strip()
                    norm_edges.append(
                        {"from": f, "to": _resolve_endpoint(t_raw), "label": elabel})

        # === 无有效 edges 时自动生成顺序连线 ===
        if not norm_edges:
            for i in range(len(norm_nodes) - 1):
                norm_edges.append({
                    "from": norm_nodes[i]["id"],
                    "to": norm_nodes[i + 1]["id"],
                    "label": ""
                })

        # === 生成 Mermaid 代码 ===
        # BUG-FIX-34 修复：原硬编码 "flowchart TD"，丢弃 data 中的 direction 字段，
        # 导致 AI 输出的 LR 流程图被强制转为 TD。改为读取 direction 并归一化。
        # ✅ 2026-10-11 统一样张：所有流程图默认纵向 TD（自上而下），steps 形态
        #    不再缺省 LR；载荷显式给出的 direction 原样保留。
        direction = str(data.get("direction") or "").upper()
        if direction == "TB":
            direction = "TD"
        if direction not in ("TD", "LR", "RL", "BT"):
            direction = "TD"
        lines = [f"flowchart {direction}"]
        title = data.get("title")
        if title:
            lines.append(f"    title {title}")

        # 节点定义
        for node in norm_nodes:
            nid = node["id"]
            label = node["label"].replace('"', "'")
            ntype = node["type"]
            if ntype in ("start", "end"):
                lines.append(f'    {nid}(["{label}"])')
            elif ntype == "decision":
                lines.append(f'    {nid}{{"{label}"}}')
            else:
                lines.append(f'    {nid}["{label}"]')

        # 边定义
        for edge in norm_edges:
            f = edge["from"]
            t = edge["to"]
            label = edge.get("label", "")
            if label:
                lines.append(f"    {f} -->|{label}| {t}")
            else:
                lines.append(f"    {f} --> {t}")

        logger.info(
            "[flowchart_json_to_mermaid] 转换成功: nodes=%d edges=%d (edges_source=%s)",
            len(norm_nodes), len(norm_edges),
            "auto_generated" if not edges_valid else "user_provided"
        )
        return "\n".join(lines)
    except Exception as e:
        logger.warning("flowchart_json_to_mermaid 转换失败: %s", e)
        return ""

@_render_hq
def _render_flowchart_with_edges(data: dict) -> BytesIO | None:
    """使用 PIL 绘制带箭头和标签的完整流程图（nodes + edges 结构）

    输入：{"nodes": [{"id":"A","label":"施工准备","type":"start"},...],
           "edges": [{"from":"A","to":"B","label":"合格"},...]}
    支持简化格式：nodes 可以是字符串列表，edges 缺失时自动生成顺序连线。

    支持3种节点类型（2026-10-11 R66 统一样式规范：全部淡紫底，靠边框色/粗细区分）:
    - start: 圆角矩形, 深紫边框 (#f0edff / #7C63D8)
    - process: 矩形, 紫边框 (#f0edff / #a89aff)
    - end: 圆角矩形, 深紫边框 (#f0edff / #7C63D8)

    自动布局：按 edges 拓扑排序确定节点位置
    箭头绘制：使用 PIL.ImageDraw 的 line + polygon 绘制方向箭头
    BUG-FIX-33 修复：添加 @_render_hq + 2.5x 超采样，与其他渲染函数保持一致。
    边标签：在箭头中间位置绘制 label 文本
    """
    try:
        nodes_raw = data.get("nodes", [])
        edges_raw = data.get("edges", [])
        if not nodes_raw:
            return None

        # === 规范化 nodes ===
        nodes: list[dict] = []
        for i, node in enumerate(nodes_raw):
            if isinstance(node, str):
                nodes.append({"id": f"N{i+1}", "label": node, "type": "process"})
            elif isinstance(node, dict):
                nid = node.get("id") or f"N{i+1}"
                nodes.append({
                    "id": nid,
                    "label": node.get("label") or nid,
                    "type": node.get("type", "process"),
                })
            else:
                nodes.append({"id": f"N{i+1}", "label": str(node), "type": "process"})

        if len(nodes) < 2:
            return None

        # === 规范化 edges ===
        edges: list[dict] = []
        if isinstance(edges_raw, list) and edges_raw:
            # BUG 修复（与 flowchart_json_to_mermaid 同源）：仅当端点值不是
            # 已有节点 ID 时才做"数字索引 → 节点 ID"映射，避免把数字字符串
            # 节点 ID 误当数组下标改写，产生自环/错边。
            _node_ids = {n["id"] for n in nodes}
            idx_to_id = {str(i): n["id"] for i, n in enumerate(nodes)}

            def _resolve_ep(v) -> str:
                s = str(v)
                if s in _node_ids:
                    return s
                if s in idx_to_id:
                    return idx_to_id[s]
                return s

            for edge in edges_raw:
                if isinstance(edge, dict):
                    f = _resolve_ep(edge.get("from", ""))
                    t = _resolve_ep(edge.get("to", ""))
                    if f and t:
                        edges.append({"from": f, "to": t, "label": edge.get("label", "")})
                elif isinstance(edge, (list, tuple)) and len(edge) >= 2:
                    f = _resolve_ep(edge[0])
                    t = _resolve_ep(edge[1])
                    edges.append({"from": f, "to": t, "label": ""})
                elif isinstance(edge, str) and "-->" in edge:
                    # ✅ 与 flowchart_json_to_mermaid 同口径支持字符串边（含下标端点）
                    parts = edge.split("-->", 1)
                    f = _resolve_ep(parts[0].strip().rstrip(";").strip())
                    t_raw = parts[1].strip()
                    elabel = ""
                    if t_raw.startswith("|") and "|" in t_raw:
                        elabel = t_raw[1:t_raw.index("|")]
                        t_raw = t_raw[t_raw.index("|") + 1:]
                    t_raw = t_raw.strip().rstrip(";").strip()
                    if f and t_raw:
                        edges.append(
                            {"from": f, "to": _resolve_ep(t_raw), "label": elabel})

        # === 无 edges 时自动生成顺序连线 ===
        if not edges:
            for i in range(len(nodes) - 1):
                edges.append({"from": nodes[i]["id"], "to": nodes[i + 1]["id"], "label": ""})

        # ✅ 修复（BUG-6）：支持 direction（LR/TD）。默认 TD（自上而下）；
        #    LR 时把"层级"映射到 x 轴（列）、"层内序号"映射到 y 轴（行），并交换画布宽高。
        #    R66：方向判定上移至尺寸参数之前 —— 单链几何需按方向取参。
        _dir = str(data.get("direction", "TD") or "TD").upper()
        is_lr = _dir in ("LR", "LEFT-RIGHT", "HORIZONTAL", "横向", "左右")

        # 尺寸参数（2.5x 超采样抗锯齿）
        # ✅ R66（2026-10-11 流程图统一样式规范 · 双模式）：纵向（TD/BT）节点
        #    280×80、行距 40；横向（LR/RL）节点 200×80、列距 40（副轴间距保留旧值）。
        scale = 2.5
        padding = int(40 * scale)
        node_h = int(80 * scale)
        title_h = int(60 * scale)
        if is_lr:
            node_w = int(200 * scale)
            col_gap = int(40 * scale)
            row_gap = int(32 * scale)
        else:
            node_w = int(280 * scale)
            col_gap = int(60 * scale)
            row_gap = int(40 * scale)

        # 节点类型配色
        # ✅ 修复（BUG-6）：旧实现只含 start/process/end，AI 标注 type:"decision" 的判定
        #    节点会落到默认 process（画成矩形而非菱形）。现补 decision 菱形样式。
        # ✅ 2026-10-11 R66 统一样式规范：与前端 mermaid.js 主题同套淡紫配色。
        type_styles = {
            "start": {"fill": "#f0edff", "outline": "#7C63D8", "shape": "rounded"},
            "process": {"fill": "#f0edff", "outline": "#a89aff", "shape": "rect"},
            "end": {"fill": "#f0edff", "outline": "#7C63D8", "shape": "rounded"},
            "decision": {"fill": "#f0edff", "outline": "#a89aff", "shape": "diamond"},
        }

        # 构建邻接表 & 入度
        adj: dict[str, list[str]] = {n["id"]: [] for n in nodes}
        in_degree: dict[str, int] = {n["id"]: 0 for n in nodes}
        for e in edges:
            f, t = e.get("from", ""), e.get("to", "")
            if f in adj and t in adj:
                adj[f].append(t)
                in_degree[t] = in_degree.get(t, 0) + 1

        # 拓扑排序（Kahn 算法）确定层级
        levels: dict[str, int] = {}
        queue = [nid for nid, d in in_degree.items() if d == 0]
        if not queue:
            queue = [nodes[0]["id"]]
        for nid in queue:
            levels[nid] = 0
        head = 0
        max_iterations = len(nodes) * len(nodes) + 100
        iterations = 0
        while head < len(queue):
            cur = queue[head]
            head += 1
            iterations += 1
            if iterations > max_iterations:
                for n in nodes:
                    if n["id"] not in levels:
                        levels[n["id"]] = max(levels.values()) + 1 if levels else 0
                break
            for nb in adj.get(cur, []):
                if nb not in levels:
                    levels[nb] = levels[cur] + 1
                    queue.append(nb)

        # 未分配层级的节点
        for n in nodes:
            if n["id"] not in levels:
                levels[n["id"]] = max(levels.values()) + 1 if levels else 0

        if not levels:
            levels = {n["id"]: 0 for n in nodes}

        max_level = max(levels.values()) if levels else 0
        level_groups: dict[int, list[str]] = {lv: [] for lv in range(max_level + 1)}
        for nid, lv in levels.items():
            level_groups[lv].append(nid)
        for lv in level_groups:
            level_groups[lv].sort()

        # ✅ 修复（BUG-6）：支持 direction（LR/TD）。默认 TD（自上而下）。
        #    _dir / is_lr 已在上方「尺寸参数」处计算（R66：几何按方向取参）。
        max_rows = max_level + 1
        max_cols = max(len(g) for g in level_groups.values())
        col_w = node_w + col_gap
        row_h = node_h + row_gap

        # ✅ R67 连线通道规划：与 v2 渲染器同口径 —— 同一间隙内多条边
        #    各占一条通道，间隙高度按边数自动加高，不再共线并轨。
        _base_gap = col_gap if is_lr else row_gap
        _band_of, _band_edges, _band_gap, _edge_slot = _plan_edge_lanes(
            levels,
            edges,
            horizontal=is_lr,
            base_gap=_base_gap,
            # with_edges 全程使用 device px（design×2.5），通道占位同步换算
            lane_pitch=int(LANE_PITCH_DESIGN_PX * scale),
        )
        _level_span = col_w if is_lr else row_h
        _band_extra = {
            _b: max(0.0, _g - _base_gap) for _b, _g in _band_gap.items()
        }
        _coord: list[float] = [0.0] * (max_level + 1)
        for _lv in range(1, max_level + 1):
            _coord[_lv] = _coord[_lv - 1] + _level_span + _band_extra.get(
                _lv - 1, 0.0)
        _content_span = _coord[max_level] + _level_span
        # 末层之后仍有通道带（TD 末层出发的同层/回退边、LR 图底母线）→ 图底预留
        _tail_gap = _band_gap.get(max_level if not is_lr else -1, 0.0)
        # 图底母线带（LR 同层/回退边）基准 y
        _bus_base = title_h + padding + max_cols * row_h - row_gap if is_lr else 0.0

        if is_lr:
            total_w = padding * 2 + _content_span
            total_h = title_h + padding * 2 + max_cols * row_h + _tail_gap
        else:
            total_w = padding * 2 + max_cols * col_w - col_gap
            total_h = title_h + padding * 2 + _content_span + _tail_gap

        img = Image.new("RGB", (int(total_w), int(total_h)), "white")
        draw = ImageDraw.Draw(img)
        font_title = _image_font(int(20 * scale), bold=True)
        # ✅ R66：节点字号纵向 16px / 横向 15px（统一样式规范 §1.2/§3）
        font_node = _image_font(int((15 if is_lr else 16) * scale), bold=True)
        font_edge = _image_font(int(13 * scale))
        font_caption = _image_font(int(13 * scale))

        # 标题
        title = str(data.get("title", "流程图"))
        draw.text((padding, int(18 * scale)), title, fill="#1E3A5F", font=font_title)

        # 计算节点位置
        node_lookup = {n["id"]: n for n in nodes}
        positions: dict[str, tuple[float, float, float, float]] = {}

        for lv, ids in level_groups.items():
            n_in_level = len(ids)
            if is_lr:
                start_x = padding + _coord[lv]
                start_y = title_h + padding + (max_cols - n_in_level) * row_h / 2
                for i, nid in enumerate(ids):
                    x = start_x
                    y = start_y + i * row_h
                    positions[nid] = (x, y, node_w, node_h)
            else:
                start_y = title_h + padding + _coord[lv]
                start_x = padding + (max_cols - n_in_level) * col_w / 2
                for i, nid in enumerate(ids):
                    x = start_x + i * col_w
                    y = start_y
                    positions[nid] = (x, y, node_w, node_h)

        # ---- 先画连线（确保节点覆盖在连线上方）----
        # ✅ 2026-10-11 统一样张：深灰连线，与 v2 渲染器同色
        # ✅ R67：同一间隙带内每条边各占一条通道；同层/回退边走专用母线
        arrow_color = "#333333"
        edge_label_bg = "#FFFFFF"

        def _we_lane(ei: int):
            """该边的 (通道坐标 device px, 轴向, 是否母线)；带外边返回 None。"""
            band = _band_of.get(ei)
            if band is None:
                return None
            idxs = _band_edges.get(band, [])
            frac = (_edge_slot.get(ei, 0) + 1) / (len(idxs) + 1)
            if band < 0:
                return _bus_base + frac * _tail_gap, "y", True
            extra_b = _band_extra.get(band, 0.0)
            if is_lr:
                inter = (col_w - node_w) + extra_b
                return padding + _coord[band] + node_w + frac * inter, "x", False
            is_bus = levels.get(edges[ei]["from"]) == levels.get(edges[ei]["to"])
            inter = (row_h - node_h) + extra_b
            return title_h + padding + _coord[band] + node_h + frac * inter, \
                "y", is_bus

        for _ei, e in enumerate(edges):
            f, t = e.get("from", ""), e.get("to", "")
            if f not in positions or t not in positions:
                continue
            fx, fy, fw, fh = positions[f]
            tx, ty, tw, th = positions[t]
            lane = _we_lane(_ei)

            if is_lr:
                # 左右布局：源点取右侧中点，目标点取左侧中点，折线横向走
                sx = fx + fw
                sy = fy + fh / 2
                ex = tx
                ey = ty + th / 2
                if (lane is not None and lane[2]) or ex < sx - int(4 * scale):
                    # 同层/回退边：走图底专用母线，箭头朝上进入目标底边
                    if lane is not None and lane[2]:
                        bus_y = lane[0]
                    else:
                        bus_y = max(fy + fh, ty + th) + int(30 * scale)
                    cx_f, cx_t = fx + fw / 2, tx + tw / 2
                    bot_t = ty + th
                    path = [(cx_f, fy + fh), (cx_f, bus_y), (cx_t, bus_y),
                            (cx_t, bot_t + int(8 * scale))]
                    draw.line(path, fill=arrow_color, width=int(2 * scale))
                    draw.polygon(
                        [
                            (cx_t, bot_t + int(2 * scale)),
                            (cx_t - int(7 * scale), bot_t + int(2 * scale) + int(14 * scale)),
                            (cx_t + int(7 * scale), bot_t + int(2 * scale) + int(14 * scale)),
                        ],
                        fill=arrow_color,
                    )
                    label_anchor = ("y", bus_y, (cx_f + cx_t) / 2)
                else:
                    mid_x = lane[0] if lane is not None and not lane[2] \
                        else (sx + ex) / 2
                    path = [(sx, sy), (mid_x, sy), (mid_x, ey),
                            (ex - int(8 * scale), ey)]
                    draw.line(path, fill=arrow_color, width=int(2 * scale))
                    # 箭头朝左
                    arrow_tip = (ex - int(2 * scale), ey)
                    arrow_base1 = (ex + int(10 * scale), ey - int(5 * scale))
                    arrow_base2 = (ex + int(10 * scale), ey + int(5 * scale))
                    draw.polygon([arrow_tip, arrow_base1, arrow_base2],
                                 fill=arrow_color)
                    label_anchor = ("x", mid_x, (sy + ey) / 2)
            else:
                # 上下布局：源点取底部中点，目标点取顶部中点，折线纵向走
                sx = fx + fw / 2
                sy = fy + fh
                ex = tx + tw / 2
                ey = ty

                if lane is not None and lane[2]:
                    # 同层边：走源层下方专用母线，箭头朝上进入目标底边
                    bus_y = lane[0]
                    bot_t = ty + th
                    path = [(sx, sy), (sx, bus_y), (ex, bus_y),
                            (ex, bot_t + int(8 * scale))]
                    draw.line(path, fill=arrow_color, width=int(2 * scale))
                    draw.polygon(
                        [
                            (ex, bot_t + int(2 * scale)),
                            (ex - int(7 * scale), bot_t + int(2 * scale) + int(14 * scale)),
                            (ex + int(7 * scale), bot_t + int(2 * scale) + int(14 * scale)),
                        ],
                        fill=arrow_color,
                    )
                    label_anchor = ("y", bus_y, (sx + ex) / 2)
                else:
                    # 折线路径（带拐点）；R67：水平段落在该边自己的通道上
                    mid_y = lane[0] if lane is not None and not lane[2] \
                        else (sy + ey) / 2
                    path = [(sx, sy), (sx, mid_y), (ex, mid_y),
                            (ex, ey - int(8 * scale))]

                    draw.line(path, fill=arrow_color, width=int(2 * scale))

                    # 绘制箭头（使用 polygon 绘制三角箭头）
                    arrow_tip = (ex, ey - int(2 * scale))
                    arrow_base1 = (ex - int(5 * scale), ey - int(12 * scale))
                    arrow_base2 = (ex + int(5 * scale), ey - int(12 * scale))
                    draw.polygon([arrow_tip, arrow_base1, arrow_base2],
                                 fill=arrow_color)
                    label_anchor = ("y", mid_y, (sx + ex) / 2)

            # 在箭头中间位置绘制 label 文本
            label = e.get("label", "")
            if label:
                _axis, _lane_v, _mid_v = label_anchor
                if _axis == "x":
                    label_x = _lane_v - 14
                    label_y = _mid_v - 10
                else:
                    label_x = _mid_v - 14
                    label_y = _lane_v - 20
                bbox = draw.textbbox((0, 0), label, font=font_edge)
                tw_l = bbox[2] - bbox[0] + 8
                th_l = bbox[3] - bbox[1] + 4
                draw.rectangle(
                    [label_x - 2, label_y - 2, label_x + tw_l, label_y + th_l],
                    fill=edge_label_bg,
                    outline="#a89aff",
                    width=1,
                )
                draw.text((label_x + 2, label_y), label, fill="#333333", font=font_edge)

        # ---- 再画节点（置于最上层）----
        for nid, (x, y, w, h) in positions.items():
            node = node_lookup.get(nid, {})
            label = node.get("label", nid)
            ntype = node.get("type", "process")
            style = type_styles.get(ntype, type_styles["process"])

            if style["shape"] == "rounded":
                draw.rounded_rectangle(
                    [x, y, x + w, y + h],
                    radius=int(12 * scale),
                    fill=style["fill"],
                    outline=style["outline"],
                    width=int(2 * scale),
                )
            elif style["shape"] == "diamond":
                # decision 类型：菱形
                cx, cy = x + w / 2, y + h / 2
                draw.polygon(
                    [
                        (cx, y),
                        (x + w, cy),
                        (cx, y + h),
                        (x, cy),
                    ],
                    fill=style["fill"],
                    outline=style["outline"],
                    width=int(2 * scale),
                )
            else:
                draw.rectangle(
                    [x, y, x + w, y + h],
                    fill=style["fill"],
                    outline=style["outline"],
                    width=int(2 * scale),
                )
            if style["shape"] == "diamond":
                # ✅ R67：菱形文字按内接矩形自适应，绝不越出菱形轮廓
                _draw_diamond_label(
                    draw, x + w / 2, y + h / 2, w, h,
                    label, font_node, "#333333", scale,
                )
            else:
                _draw_text_center(
                    draw, (x + 4, y + 4, x + w - 4, y + h - 4),
                    label, font_node, "#333333", max_lines=2
                )

        # ---- 图例 ----
        legend_y = total_h - 24
        legend_x = padding
        legend_items = [
            ("#f0edff", "#7C63D8", "开始"),
            ("#f0edff", "#a89aff", "过程"),
            ("#f0edff", "#a89aff", "判定"),
            ("#f0edff", "#7C63D8", "结束"),
        ]
        for fill, outline, ltext in legend_items:
            draw.rounded_rectangle(
                [
                    legend_x,
                    legend_y - int(14 * scale),
                    legend_x + int(24 * scale),
                    legend_y + int(2 * scale),
                ],
                radius=int(4 * scale),
                fill=fill,
                outline=outline,
                width=int(1 * scale),
            )
            draw.text(
                (legend_x + int(30 * scale), legend_y - int(13 * scale)),
                ltext,
                fill="#1E3A5F",
                font=font_caption,
            )
            legend_x += int(90 * scale)

        paper_floor_note(
            "流程图",
            [font_title, font_node, font_edge, font_caption],
            img.size[0], img.size[1],
            primary=[font_node],
            suggest="（节点标签是主文字；分支多或层级深时图会被等比缩小）",
        )
        return _image_to_stream(img)
    except Exception as e:
        logger.warning("_render_flowchart_with_edges 渲染失败: %s", e)
        return None

