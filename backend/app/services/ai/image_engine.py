import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

# ✅ 移植适配：源项目为 from .workflows import collect_json_response（provider 直连签名），
# 本项目在 json_response.py 提供了签名兼容的 provider 直连版本

# ✅ R38 D6：随死函数一并移除 `from .prompts import get_prompt`（本模块已无
#    消费点；模板消费链保留在 charts.py / prompts.illustration）

logger = logging.getLogger(__name__)


# 图片生成结果缓存（基于 Prompt+Style+Size+Provider 的 MD5 哈希）
# 使用 OrderedDict 实现 LRU 淘汰，最大 100 条
# ✅ R196（2026-11-09）：value 由裸 URL 改为 (url, monotonic_ts) 二元组 ——
#    多数文生图厂商返回**带时效的签名 URL**，裸 LRU 永不过期：URL 过期后同一
#    prompt 持续命中死链 → 导出下载失败 → 成稿缺图，且进程存活期间反复失败。
#    现在成功缓存条目超 _IMAGE_SUCCESS_TTL 即视为过期（删除后重新生成）。
# key: MD5 hexdigest, value: (图片 URL, 写入时间戳)
_IMAGE_CACHE: OrderedDict[str, tuple[str, float]] = OrderedDict()
_IMAGE_CACHE_MAX_SIZE = 100
_IMAGE_SUCCESS_TTL = 12 * 3600.0
_image_cache_lock = threading.Lock()

# 失败结果缓存（BUG 修复）：旧实现把失败结果以 None 写入 _IMAGE_CACHE，
# 但读取判断是 `if _cached_url is not None`，"键不存在"与"值为 None"无法区分，
# 失败缓存永远无法命中——每张失败配图都会重复走完整 Provider 降级链。
# 现改用独立字典记录失败时间戳，短 TTL（10 分钟）内快速失败，过期后允许重试。
_IMAGE_FAIL_CACHE: dict[str, float] = {}
_IMAGE_FAIL_TTL = 600.0


def _image_cache_set(key: str, value: str | None) -> None:
    """写入图片缓存。

    - value 为 URL：写入 LRU 成功缓存，超出最大条目数时淘汰最旧条目
    - value 为 None（生成失败）：写入短 TTL 失败缓存，避免重复失败尝试，
      同时不会永久记住瞬时故障
    """
    with _image_cache_lock:
        if value is None:
            _IMAGE_FAIL_CACHE[key] = time.monotonic()
            return
        _IMAGE_FAIL_CACHE.pop(key, None)
        if key in _IMAGE_CACHE:
            _IMAGE_CACHE.move_to_end(key)
        _IMAGE_CACHE[key] = (value, time.monotonic())
        while len(_IMAGE_CACHE) > _IMAGE_CACHE_MAX_SIZE:
            _IMAGE_CACHE.popitem(last=False)


# ---------------------------------------------------------------------------
# ✅ 图表提示词库 v3.1 程序端校验清单（确定性解析，零 AI 成本）：
#    流程图：节点 ID 合法性 / 孤立节点 / 最小规模
#    甘特图：任务 ID 重复 / 依赖成环 / 竣工里程碑
# ---------------------------------------------------------------------------
_ARROW_SPLIT_RE = re.compile(r"\s*(?:-->|---|-\.->|==>|--x|--o)\s*")
# 节点 ID 允许以 ASCII 数字开头（Prompt 明确「字母/数字/下划线」；Mermaid 亦支持
# `1[施工准备] --> 2[基础施工]`）。旧正则要求首字母/下划线，导致合法数字 ID 流程图
# 在 `_parse_flow_graph` 中解析出 0 个节点 → 判「节点数不足（0<3）」→ 整块删除。
_ID_TOKEN_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_\-]*)")
_GANTT_META_KEYS = {
    "dateformat",
    "axisformat",
    "title",
    "excludes",
    "includes",
    "todaymarker",
    "autonumber",
    "config",
}
_GANTT_STATUS_TAGS = {"done", "active", "crit"}


def _parse_flow_graph(code: str) -> tuple[set[str], set[str], int, list[str]]:
    """解析 flowchart/graph 代码。

    Returns:
        (defined_ids, linked_ids, edge_count, id_errors)
        - defined_ids: 显式定义的节点 ID（`A1["标签"]` 等定义行）
        - linked_ids:  出现在连线上的节点 ID
        - edge_count:  连线总数
        - id_errors:   节点 ID 非法（含中文/连字符）错误列表
    """
    defined: set[str] = set()
    linked: set[str] = set()
    edge_count = 0
    id_errors: list[str] = []

    def _check_id(tok: str) -> None:
        if "-" in tok:
            id_errors.append(f"节点 ID '{tok}' 含连字符（仅允许字母/数字/下划线）")
        if re.search(r"[\u4e00-\u9fff]", tok):
            id_errors.append(f"节点 ID '{tok}' 含中文")

    for raw in code.split("\n"):
        s = raw.strip()
        if not s or s.startswith(("%%", "//")):
            continue
        if s.lower().startswith(("flowchart", "graph", "subgraph", "end", "direction", "click")):
            continue
        if _ARROW_SPLIT_RE.search(s):
            parts = _ARROW_SPLIT_RE.split(s)
            # 去掉分支条件标签 `|合格|`
            toks = []
            for p in parts:
                p = re.sub(r"\|[^|]*\|", "", p).strip()
                if not p:
                    continue
                m = _ID_TOKEN_RE.match(p)
                if not m:
                    continue
                tok = m.group(1)
                _check_id(tok)
                toks.append(tok)
            linked.update(toks)
            edge_count += max(0, len(toks) - 1)
        else:
            # 无箭头行：节点定义 `A1["标签"]` / `A1(标签)` / `A1{标签}`
            # 与 _ID_TOKEN_RE 同口径：允许数字开头（`1["施工准备"]`）。
            m = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_\-]*)\s*[\[\{\(]", s)
            if m:
                tok = m.group(1)
                _check_id(tok)
                defined.add(tok)
    return defined, linked, edge_count, id_errors


def _parse_gantt_tasks(code: str) -> list[dict]:
    """解析 gantt 任务行 → [{id, deps, milestone, name}]。

    兼容三种任务行：
      施工准备 :a1, 2026-01-01, 15d
      主体施工 :a2, after a1, 60d
      竣工验收 :milestone, a8, after a7, 0d
    """
    tasks: list[dict] = []
    for line in code.split("\n"):
        s = line.strip()
        if not s or s.startswith(("%%", "//")):
            continue
        if ":" not in s:
            continue
        name_part, _, rest = s.partition(":")
        name = name_part.strip()
        if name.lower() in _GANTT_META_KEYS or s.lower().startswith("section"):
            continue
        tid, deps, is_ms = "", [], False
        for seg in (x.strip() for x in rest.split(",")):
            if not seg:
                continue
            low = seg.lower()
            if low.startswith("after "):
                deps.append(seg[6:].strip())
            elif low == "milestone":
                is_ms = True
            elif low in _GANTT_STATUS_TAGS:
                continue
            elif not tid and re.fullmatch(r"[A-Za-z_]\w*", seg):
                tid = seg
        tasks.append({"id": tid, "deps": deps, "milestone": is_ms, "name": name})
    return tasks


def _find_gantt_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """DFS 检测任务依赖环，返回环路径（无环返回 None）。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}

    def _dfs(u: str) -> list[str] | None:
        color[u] = GRAY
        for v in graph.get(u, []):
            if color.get(v, WHITE) == GRAY:
                return [u, v]
            if color.get(v, WHITE) == WHITE:
                cyc = _dfs(v)
                if cyc:
                    return [u] + cyc
        color[u] = BLACK
        return None

    for n in graph:
        if color[n] == WHITE:
            cyc = _dfs(n)
            if cyc:
                return cyc
    return None


def _strip_leading_mermaid_comments(code: str) -> str:
    """去掉 Mermaid 代码开头的空行与 ``%%`` 注释行（含 ``%%{init: ...}%%``）。

    ✅ 2026-09-17 实现已收敛到 ``chart_validators.strip_leading_mermaid_comments``：
    校验侧与渲染侧（mermaid_renderer）共用同一份归一逻辑，避免两侧再次分叉。
    （旧实现的本文件私有版本保留为薄封装，保证既有 import 路径不变。）

    ✅ BUG 修复：AI 常在首行输出 ``%% 流程图`` 或 ``%%{init: ...}%%``，而
    validate_mermaid 全程用 ``code.startswith("graph"/"gantt"/...)`` 判定图表类型，
    一旦以注释开头就**所有分支全部落空** → 误判为"不支持的 Mermaid 类型"。
    该误判在 _chart_pipeline 里会触发"校验失败 → 修复 → 仍失败 → **从正文删除该
    代码块**"的连锁后果，正文里的合法图表被静默删掉。
    """
    from app.services.chart_validators import strip_leading_mermaid_comments

    return strip_leading_mermaid_comments(code)


def validate_mermaid(code: str) -> tuple[bool, str]:
    """基础 Mermaid 语法校验（v3.1 程序端校验清单增强版）"""
    if not code or not code.strip():
        return False, "空内容"

    code = _strip_leading_mermaid_comments(code.strip())
    if not code:
        return False, "空内容"

    # ✅ 语句分隔符归一（与渲染侧**同一实现**，见 chart_validators）。
    #    `;` 在 Mermaid 中与换行完全等价，但本函数与所有 PIL 解析器都按行解析：
    #    旧实现只删"行末"分号，于是
    #    `flowchart TD; A["准备"] --> B["施工"]; B --> C["验收"]` 这种**单行分号**
    #    写法里，`graph/flowchart` 所在行被整体跳过 → 节点数算成 0 →
    #    判「节点数不足（0<3）」→ 正则修复也救不回 → **正文里的合法流程图被整块
    #    删除**（实测 2 例；渲染侧本来就能正常出图 → ❗删块型错配）。
    from app.services.chart_validators import normalize_mermaid_statements

    code = normalize_mermaid_statements(code)

    # Try to detect if it's a flowchart without explicit graph prefix
    # If it contains arrows but no graph/flowchart prefix, treat as simple flowchart
    has_arrow = "-->" in code or "-- >" in code
    has_graph_prefix = code.startswith("graph") or code.startswith("flowchart")

    # ✅ BUG 修复：该启发式（"含 --> 就当无前缀流程图"）必须排除"已带其它图表关键字"
    #    的情形 —— quadrantChart / xychart 的轴定义里同样含 "-->"
    #    （如 `x-axis 低 --> 高`），旧实现会把它当成无前缀流程图去数节点，
    #    得出"节点数不足（1<2）"→ 判为非法 → 正文里的象限图被删块。
    from app.services.chart_validators import (
        MERMAID_KEYWORD_TO_CHART_TYPE,
        first_mermaid_keyword,
    )

    _leading_kw = first_mermaid_keyword(code)
    _has_other_diagram_keyword = not has_graph_prefix and _leading_kw in MERMAID_KEYWORD_TO_CHART_TYPE

    if has_arrow and not has_graph_prefix and not _has_other_diagram_keyword:
        # It looks like a simple flowchart without prefix
        # Count nodes (text segments separated by -->)
        parts = re.split(r"-->\s*", code.strip())
        node_count = len([p for p in parts if p.strip() and not p.strip().startswith(">")])

        if node_count < 2:
            return False, f"节点数不足（{node_count}<2）"

        edge_count = code.count("-->") + code.count("-- >")
        if edge_count < 1:
            return False, f"流程图连线数不足（{edge_count}<1）"

        # Fall through to normal processing with detected prefix
        code = "graph TD\n" + code

    if code.startswith("graph") or code.startswith("flowchart"):
        # Mermaid 合法连线不止 `-->`：虚线 `-.->`、粗线 `==>`、无箭头 `---`、
        # `--x` / `--o` 均有效。旧实现只认 `"-->"` 子串，导致「渲染器能出图、
        # 校验器判缺连接关系」的删块型错配（虚线/粗线流程图被整块删除）。
        # ✅ R196（2026-11-09）：subgraph/end 配对校验 —— _parse_flow_graph 跳过
        #    subgraph/end 行，缺 end 的残缺图此前一路放行（登记 status=generated），
        #    HTTP 渲染轨才失败 → 导出跳过，图表清单与成稿分叉。栈式不必：flowchart
        #    的 subgraph 终止符只有单独 `end` 行（整行 strip 后恰为 end），计数相等
        #    即配对（嵌套 subgraph 也满足）。
        _sub_open = sum(1 for _l in code.split("\n") if _l.strip().lower().startswith("subgraph"))
        _sub_close = sum(1 for _l in code.split("\n") if _l.strip().lower() == "end")
        if _sub_open != _sub_close:
            return False, f"subgraph/end 不配对（subgraph {_sub_open} 个 vs end {_sub_close} 个）"
        if not _ARROW_SPLIT_RE.search(code):
            return False, "流程图缺少连接关系"

        # For simple flowcharts detected without prefix, skip length check as they may be short
        is_simple_flowchart = code.startswith("graph TD\n") or code.startswith("flowchart TD\n")
        if not is_simple_flowchart and len(code) < 20:
            return False, "流程图代码过短"
        if "`" in code:
            return False, "流程图代码包含反引号"
        lines = code.split("\n")
        node_count = 0
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("subgraph") or stripped.startswith("end"):
                continue
            # 用于判断「是否需要补充统计裸标识符节点」：只数方括号/花括号标记
            node_count += stripped.count("[") if "[" in stripped else 0
            node_count += stripped.count("{") if "{" in stripped else 0
        # 修复：补充统计裸标识符节点（A --> B 格式中无方括号的节点）。
        # 原实现仅统计 [ 和 { 标记的节点，导致 AI 生成的裸标识符流程图
        # （如 "graph TD\nA --> B\nB --> C"）被误判为节点数不足。
        if node_count == 0:
            _bare_nodes = set()
            for line in lines:
                s = line.strip()
                if (
                    not s
                    or s.startswith("subgraph")
                    or s.startswith("end")
                    or s.startswith("graph")
                    or s.startswith("flowchart")
                ):
                    continue
                # 只统计含箭头的行中的裸标识符，避免将首行无效类型名误计为节点
                if "-->" not in s and "-- >" not in s:
                    # 无箭头的非空行（非 directive）应视为语法错误
                    if not s.startswith("//") and not s.startswith("%%") and len(s) > 0:
                        return False, f"流程图语法错误：不支持的行 '{s[:40]}'"
                    continue
                for seg in re.split(r"\s*-->\s*", s):
                    ident = seg.strip()
                    if ident and not ident.startswith("//") and len(ident) <= 30:
                        _bare_nodes.add(ident)
            node_count = max(node_count, len(_bare_nodes))
        # ✅ 2026-11-07（R183）：删除「方括号/`-->` 计数」旧预检查。它们只数
        #    `[`/`{` 与 `-->` 字面量：
        #    · `A[开始] --> B --> C --> D`（带标签 + 裸节点混合链）节点数被算成 1；
        #    · 数字 ID 与 `-.->`/`==>`/`---` 连线的合法图同样被判不足。
        #    最小规模/连线/孤立节点由下方 `_parse_flow_graph`（与渲染器同口径）
        #    统一裁决，避免同一事实维护两套判据再次分叉。
        if "--> ... -->" in code:
            return False, "流程图存在未填充的连写箭头占位符"
        # Mermaid 原生支持方括号/花括号中的中文（如 A[开始]），无需双引号
        # 但对于包含特殊字符（逗号、括号等）的中文标签，建议使用双引号。
        # BUG 修复：① 链式箭头 A --> B --> C 是 Mermaid 原生合法语法，
        # 旧正则 -->[^-\n]*--> 会误判为"连写箭头"，触发多余的修复 AI 调用
        # 甚至丢图；仅保留字面占位符检查。② 中文与特殊字符必须是【同一个
        # 标签内】同时出现才需引号，旧实现是两个独立正则 AND，可跨标签
        # 误报；改为单括号内双 lookahead，且限制匹配不越过闭合括号。
        if re.search(
            r"\[(?=[^\]\"\n]*[\u4e00-\u9fff])(?=[^\]\"\n]*[,，、()（）])[^\]\"\n]*\]",
            code,
        ):
            return False, "流程图中文标签含特殊字符但缺少双引号"
        if re.search(
            r"\{(?=[^}\"\n]*[\u4e00-\u9fff])(?=[^}\"\n]*[,，、()（）])[^}\"\n]*\}",
            code,
        ):
            return False, "流程图菱形节点中文标签含特殊字符但缺少双引号"
        # ✅ 已移除"行末分号"判罚：`;` 是 Mermaid **合法**的语句分隔符，且函数开头
        #    的 normalize_mermaid_statements 已把它归一为换行（与渲染侧同口径）。
        #    旧判罚会让单行分号写法的合法流程图被整块删除（实测删块型错配）。

        # ✅ v3.1 程序端校验清单增强：ID 合法性 / 最小规模 / 孤立节点
        _def_ids, _link_ids, _edges, _id_errs = _parse_flow_graph(code)
        if _id_errs:
            return False, _id_errs[0]
        _total_nodes = len(_def_ids | _link_ids)
        if _total_nodes < 3:
            return False, f"流程图节点数不足（{_total_nodes}<3，至少 3 个节点 2 条连线）"
        if _edges < 2:
            return False, f"流程图连线数不足（{_edges}<2）"
        _isolated = _def_ids - _link_ids
        if _isolated:
            return False, "流程图存在孤立节点: " + "、".join(sorted(_isolated)[:5])
        return True, "ok"

    if code.startswith("gantt"):
        lines = code.split("\n")
        has_dateformat = any("dateFormat" in l for l in lines)

        has_task = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("section "):
                pass
            elif ":" in stripped and not stripped.startswith("%") and not stripped.startswith("//"):
                parts = stripped.split(":", 1)
                task_part = parts[1].strip()
                if task_part:
                    has_task = True
        if not has_dateformat:
            return False, "甘特图缺少 dateFormat 声明"
        if not has_task:
            return False, "甘特图缺少任务定义"

        # ✅ v3.1 程序端校验清单增强：任务 ID 唯一 / 依赖不成环 / 竣工里程碑
        _tasks = _parse_gantt_tasks(code)
        _ids = [t["id"] for t in _tasks if t["id"]]
        _dups = sorted({i for i in _ids if _ids.count(i) > 1})
        if _dups:
            return False, f"甘特图任务 ID 重复: {'、'.join(_dups[:3])}"
        _id_set = set(_ids)
        _graph = {t["id"]: [d for d in t["deps"] if d in _id_set] for t in _tasks if t["id"]}
        _cycle = _find_gantt_cycle(_graph)
        if _cycle:
            return False, "甘特图任务依赖成环: " + " -> ".join(_cycle[:6])
        if _ids and not any(t["milestone"] for t in _tasks):
            return False, "甘特图缺少竣工验收里程碑（末行 milestone, 0d）"
        return True, "ok"

    # ✅ R196（2026-11-09）：四类 Mermaid 此前无条件 return True —— 截断/残缺的
    #    图（只剩 header 行）通过登记（status=generated），渲染失败只在导出期暴露
    #    并被跳过 → 图表清单有图、成稿无图，且无法走 fix-mermaid 预修复。现各补
    #    **最低限度结构校验**（合法图必然满足；只拦完全残缺的块，宁缺勿滥）。
    if code.startswith("sequenceDiagram"):
        # 时序图至少 1 条消息行：->>/-->>（实线/虚线箭头）或 -)/--)（开/闭端）
        _msg_lines = sum(
            1 for _l in code.split("\n") if re.search(r"->>|-->>|-\)|--\)", _l)
        )
        if _msg_lines < 1:
            return False, f"时序图缺少消息行（{_msg_lines}<1，疑似截断残块）"
        return True, "ok"

    if code.startswith("classDiagram"):
        # 类图至少 1 个成员/关系行（header 之后的非注释非空行）
        _body = [
            _l.strip()
            for _l in code.split("\n")[1:]
            if _l.strip() and not _l.strip().startswith(("%%", "//"))
        ]
        if len(_body) < 1:
            return False, "类图缺少类声明/成员行（疑似截断残块）"
        return True, "ok"

    if code.startswith("stateDiagram") or code.startswith("stateDiagram-v2"):
        # 状态图至少 1 个转换（-->）或状态声明（state X）
        _ok_states = any(
            ("-->" in _l or _l.strip().startswith("state "))
            for _l in code.split("\n")[1:]
            if _l.strip() and not _l.strip().startswith(("%%", "//", "note "))
        )
        if not _ok_states:
            return False, "状态图缺少状态转换/声明（疑似截断残块）"
        return True, "ok"

    if code.startswith("pie"):
        # ✅ v3.1：pie 至少 2 个数据项
        _data_rows = [
            l.strip()
            for l in code.split("\n")[1:]
            if l.strip() and ":" in l and not l.strip().startswith(("%%", "//", "title"))
        ]
        if len(_data_rows) < 2:
            return False, f"饼图数据项不足（{len(_data_rows)}<2）"
        return True, "ok"

    if code.startswith("erDiagram"):
        # ER 图至少 1 条关系行（形如 CUSTOMER ||--o{ ORDER : places）
        _rel_lines = sum(1 for _l in code.split("\n") if "--" in _l and not _l.strip().startswith(("%%", "//")))
        if _rel_lines < 1:
            return False, f"ER 图缺少实体关系行（{_rel_lines}<1，疑似截断残块）"
        return True, "ok"

    # BUG-FIX-29 修复：原 validate_mermaid 不支持 timeline 类型，
    # timeline 代码会落到兜底分支返回"不支持的 Mermaid 类型"。
    if code.startswith("timeline"):
        lines = code.split("\n")
        has_event = False
        for line in lines[1:]:
            stripped = line.strip()
            if stripped and not stripped.startswith("%") and not stripped.startswith("//"):
                if ":" in stripped or stripped.startswith("section"):
                    has_event = True
                    break
        if not has_event:
            return False, "时间线缺少事件定义"
        return True, "ok"

    # ✅ BUG 修复（重要）：以下合法图表类型此前全部落到兜底分支返回
    #    "不支持的 Mermaid 类型"。而 _chart_pipeline._validate_inline_chart 的策略是
    #    "校验失败 → 正则修复 → 仍失败则**从正文删除该代码块**"。
    #    于是 AI 写在正文里的 mindmap / journey / xychart-beta /
    #    gitGraph / sankey-beta / block-beta / kanban / quadrantChart 等图表
    #    被静默删除 —— 用户既看不到图，也拿不到那段代码。
    #    这里按图表类型补齐识别 + 最低限度结构校验（细节交给渲染引擎判定）。
    _body_lines = [l.strip() for l in code.split("\n")[1:] if l.strip() and not l.strip().startswith(("%%", "//"))]

    if code.startswith(
        (
            "mindmap",
            "journey",
            "gitGraph",
            "quadrantChart",
            "sankey-beta",
            "sankey",
            "block-beta",
            "block",
            "kanban",
            "architecture-beta",
            "architecture",
        )
    ):
        if not _body_lines:
            return False, f"{code.split(chr(10))[0][:20]} 图表缺少内容"
        return True, "ok"

    if code.startswith(("xychart-beta", "xychart")):
        # xychart 至少要有坐标轴或一条数据系列
        if not any(l.strip().startswith(("bar", "line", "x-axis", "y-axis")) for l in _body_lines):
            return False, "xychart 缺少坐标轴/数据系列定义"
        return True, "ok"

    return (
        False,
        f"不支持的 Mermaid 类型: {code.split(chr(10))[0] if chr(10) in code else code[:30]}",
    )


def repair_mermaid(code: str) -> str:
    """修复常见 Mermaid 语法问题（确定性正则修复，零 AI 成本）"""
    if not code:
        return ""

    from app.services.chart_validators import normalize_mermaid_statements

    code = code.strip()

    code = re.sub(r"```mermaid\s*\n?", "", code)
    code = re.sub(r"\n?```", "", code)

    code = re.sub(r"[ \t]+$", "", code, flags=re.MULTILINE)

    # ✅ v3.1 确定性修复 ①：删除样式指令与主题块
    #    （后端 PIL 兜底渲染不解析 classDef/style/%%{init}%%，
    #     会导致预览与导出渲染结果不一致，且部分引擎直接报错）
    code = "\n".join(
        ln for ln in code.split("\n") if not re.match(r"^\s*(classDef\s|class\s+\w|style\s|linkStyle\s|%%\{init)", ln)
    )

    # ✅ ②：去除标签内 HTML 标签（PIL 兜底渲染不支持，按纯文本输出）
    code = re.sub(r"<br\s*/?>", "", code, flags=re.IGNORECASE)
    code = re.sub(r"</?(b|strong|i|em)>", "", code, flags=re.IGNORECASE)

    # ✅ ③：语句分隔符归一（`;` → 换行），与校验侧 / 渲染侧**同一实现**。
    #    旧实现只删"行末"分号：`graph TD; A-->B; B-->C` 这类单行分号代码修复后
    #    仍是单行 → 校验依旧按行解析失败 → 合法流程图被整块删除。
    #    归一为换行后行末分号自然消失，且产物是"按行"的（下游解析器全部按行工作）。
    code = normalize_mermaid_statements(code)

    # ✅ ④：甘特图 status 标签（done/active/crit 渲染器兼容性差）
    code = re.sub(r":\s*(done|active|crit)\s*,", ":", code, flags=re.IGNORECASE)
    code = re.sub(r":\s*(done|active|crit)\s*$", ":", code, flags=re.IGNORECASE | re.MULTILINE)

    # ✅ ⑤：中文标签含特殊字符但缺双引号 → 自动补引号
    #    （与 validate_mermaid 的判定条件保持一致：同标签内 CJK + 特殊字符）
    code = re.sub(
        r"\[(?=[^\]\"\n]*[\u4e00-\u9fff])(?=[^\]\"\n]*[,，、()（）])[^\]\"\n]*\]",
        lambda m: '["' + m.group(0)[1:-1] + '"]',
        code,
    )
    code = re.sub(
        r"\{(?=[^}\"\n]*[\u4e00-\u9fff])(?=[^}\"\n]*[,，、()（）])[^}\"\n]*\}",
        lambda m: '{"' + m.group(0)[1:-1] + '"}',
        code,
    )

    # ✅ ⑥：甘特图补 dateFormat / axisFormat
    if code.startswith("gantt"):
        _lines = code.split("\n")
        if not any("dateFormat" in ln for ln in _lines):
            _ins = 1  # gantt 行之后
            if len(_lines) > 1 and _lines[1].strip().lower().startswith("title"):
                _ins = 2
            _lines[_ins:_ins] = ["    dateFormat YYYY-MM-DD", "    axisFormat %m月%d日"]
            code = "\n".join(_lines)
        elif not any("axisFormat" in ln for ln in _lines):
            _lines = code.split("\n")
            for _i, _ln in enumerate(_lines):
                if "dateFormat" in _ln:
                    _lines.insert(_i + 1, "    axisFormat %m月%d日")
                    break
            code = "\n".join(_lines)

    code = re.sub(r"\n{3,}", "\n\n", code)

    lines = code.split("\n")
    cleaned = []
    for line in lines:
        if not line.strip():
            cleaned.append("")
            continue
        if "graph" in line or "flowchart" in line or "gantt" in line:
            cleaned.append(line.strip())
        else:
            cleaned.append(line.rstrip())
    code = "\n".join(cleaned).strip()

    return code


# ✅ R38 D6 收口（2026-10-03）：删除零调用死函数 generate_illustration_prompt /
#    generate_illustration_arrange（全仓 grep 仅命中 def 本体，从未接入任何链路）
#    及其专属模板 ILLUSTRATION_PLAN_SYSTEM / ILLUSTRATION_ARRANGE_SYSTEM（见
#    prompts/illustration.py 同位注释）。产品配图已全部由图表流水线自动出图
#    （charts.py / export 自动链路，消费 ILLUSTRATION_PROMPT_OPTIMIZE），
#    旧「编排规划」方案已被取代。删除前已核实：前端零硬引用（列表动态背
#    靠 /prompts API），collect_json_response / AIMessage 仍由存留函数消费。


async def generate_illustration_image(
    provider: Any,
    prompt: str,
    style: str = "engineering_diagram",
    section_size: str = "16:9",
    provider_name: str = "volcengine",
    image_config: dict | None = None,
) -> str | None:
    """使用配置的图像模型提供商生成实际图片

    支持 Provider 降级链：主 Provider → qwen_image → dashscope → agnes。
    当主 Provider 调用失败时，自动尝试下一个备选 Provider。
    全部失败则返回 None，不阻塞调用方。

    内置基于 MD5 的图片生成缓存，相同 Prompt 不会重复生成。

    Args:
        provider: 文本 AI 提供商实例（仅用于兜底获取 API 配置）
        prompt: 图片生成提示词
        style: 图像风格（engineering_diagram/realistic_photo/technical_drawing/custom）
        section_size: 尺寸比例（16:9/1:1/9:16/4:3/3:4）
        provider_name: 图像提供商标识（agnes/dashscope/hunyuan 等）
        image_config: 图像模型配置（model_configs 表 image_* 字段），
                      优先于此处的 provider 实例；应包含 image_api_key_enc /
                      image_base_url / image_model / image_default_size 等

    Returns:
        生成的图片 URL，失败返回 None
    """
    from .image_providers.registry import ProviderRegistry

    # ---- 图片生成缓存（基于 MD5，含 provider_name 避免切换 provider 后误命中） ----
    _cache_key = hashlib.md5(f"{prompt}|{style}|{section_size}|{provider_name}".encode()).hexdigest()
    with _image_cache_lock:
        _cached_entry = _IMAGE_CACHE.get(_cache_key)
        if _cached_entry is not None:
            _cached_url, _cached_ts = _cached_entry
            # ✅ R196：签名 URL 时效兜底 —— 超过成功 TTL 的条目视为过期，
            #    删除后按未命中继续走生成链（厂商 URL 过期不再反复命中死链）。
            if time.monotonic() - _cached_ts > _IMAGE_SUCCESS_TTL:
                _IMAGE_CACHE.pop(_cache_key, None)
                logger.info(
                    "generate_illustration_image: 成功缓存已过 TTL（%.0fh），重新生成 (key=%s)",
                    _IMAGE_SUCCESS_TTL / 3600.0,
                    _cache_key[:12],
                )
            else:
                # 命中缓存，将 key 移到末尾（LRU 刷新）
                _IMAGE_CACHE.move_to_end(_cache_key)
                logger.info("generate_illustration_image: 命中缓存 (key=%s)", _cache_key[:12])
                return _cached_url
        # BUG 修复：失败缓存命中检查。旧实现失败结果（None）永远无法命中，
        # 每张失败配图都会重复走完整 Provider 降级链（多次网络调用）。
        # 现在短 TTL（10 分钟）内的失败直接快速返回 None，过期后允许重试。
        _fail_ts = _IMAGE_FAIL_CACHE.get(_cache_key)
        if _fail_ts is not None:
            if time.monotonic() - _fail_ts < _IMAGE_FAIL_TTL:
                logger.info(
                    "generate_illustration_image: 命中失败缓存，跳过重试 (key=%s)",
                    _cache_key[:12],
                )
                return None
            _IMAGE_FAIL_CACHE.pop(_cache_key, None)

    def _resolve_api_key(cfg: dict) -> str:
        """从图像配置中解析 API Key（兼容明文与 ENC: 加密前缀）"""
        raw = (cfg or {}).get("image_api_key_enc") or (cfg or {}).get("api_key") or ""
        if not raw:
            return ""
        try:
            from ..crypto import decrypt_api_key, is_encrypted

            if is_encrypted(raw):
                return decrypt_api_key(raw)
        except Exception as e:
            logger.debug("图像 API Key 解密失败，按原样使用: %s", e)
        return raw

    def _resolve_base_url(cfg: dict) -> str:
        """归一化 base_url，去掉误填的 /images/generations 端点后缀"""
        url = (cfg or {}).get("image_base_url") or (cfg or {}).get("base_url") or ""
        if not url:
            return ""
        return re.sub(r"/(?:dashscope/)?images/generations$", "", url.rstrip("/"))

    # ✅ R196（2026-11-09）：image_enabled=False 是显式的「关闭生图」语义 ——
    #    旧实现只把它当作「不用图像专用配置、改用文本模型配置兜底」的开关，
    #    生图调用照常发出（只是换了 key/base_url），运维关掉后导出仍继续计费生图，
    #    语义反直觉。现显式短路返回 None：调用方（导出/渲染链）对 None 已有
    #    「跳过该图、不占图号」的既有路径，行为即「关闭后不出图、不产生调用」。
    if image_config and not image_config.get("image_enabled", True):
        logger.info(
            "generate_illustration_image: 图像配置 image_enabled=False，跳过生图（不产生模型调用）"
        )
        return None

    # 优先使用图像专用配置
    if image_config and image_config.get("image_enabled", True):
        _fallback_api_key = getattr(provider, "api_key", "") or ""
        _cfg_api_key = _resolve_api_key(image_config) or _fallback_api_key
        _cfg_base_url = _resolve_base_url(image_config) or getattr(provider, "base_url", "") or ""
        _cfg_model = image_config.get("image_model") or getattr(provider, "model", "default-model")
        _cfg_default_size = image_config.get("image_default_size", "2K")
        _cfg_price = image_config.get("image_price_per_image", 0.0)
    else:
        # 没有图像配置时回退到 provider 实例（文本模型配置，仅作兜底）
        _cfg_api_key = getattr(provider, "api_key", "") or ""
        _cfg_base_url = getattr(provider, "base_url", "") or ""
        _cfg_model = getattr(provider, "model", "default-model")
        _cfg_default_size = "2K"
        _cfg_price = 0.0

    # 降级链定义：主 Provider → 备选1 → 备选2
    _FALLBACK_CHAIN = ["qwen_image", "dashscope", "agnes"]

    # ✅ R196（2026-11-09）：用户显式配置的 provider 必须**始终**排在队首 ——
    #    旧实现只在 provider_name 不在降级链内时才前置：配置 provider_name="dashscope"
    #    （或 agnes）+ 自选 image_model 的用户，队列仍是 ["qwen_image", ...]，
    #    而 qwen_image 用 dashscope 适配器 + 硬编码模型 qwen-image-3.0-pro ——
    #    要么静默产出非用户所选模型的图，要么用用户的 key 白烧一次失败调用才轮到
    #    用户配置。现统一：provider_name 非空即队首，链内其余条目作备选（去重）。
    _provider_queue: list[str] = []
    if provider_name:
        _provider_queue.append(provider_name)
    _provider_queue.extend(p for p in _FALLBACK_CHAIN if p != provider_name)

    # 已尝试过的 provider（避免重复）
    _tried_providers: set[str] = set()

    # 映射 size 参数到适配器期望的像素尺寸格式（dashscope 风格：仅 size 字段）
    _size_map = {
        "16:9": "1920x1080",
        "1:1": "1024x1024",
        "9:16": "1024x1792",
        "4:3": "1024x768",
        "3:4": "768x1024",
        "2:3": "768x1024",
        "3:2": "1024x768",
        "21:9": "1920x810",
    }
    _pixel_size = _size_map.get(section_size, "1024x1024")

    # 注入风格约束
    _final_prompt = prompt
    if style != "custom":
        _style_prefix = {
            "engineering_diagram": (
                "画面采用工程项目图示风格，结构清晰、专业克制、适合专项施工方案插图。"
                "避免出现品牌标识、水印、夸张营销元素和无关文字。"
            ),
            "realistic_photo": (
                "画面采用专业实景照片风格，真实、克制、适合专项施工方案插图。"
                "避免出现品牌标识、水印、夸张营销元素和无关文字。"
            ),
            "technical_drawing": (
                "画面采用工程技术图纸风格，线条清晰、标注规范、适合专项施工方案。"
                "避免出现品牌标识、水印、夸张营销元素和无关文字。"
            ),
        }.get(style, "")
        if _style_prefix:
            _final_prompt = f"{_style_prefix}\n{prompt}"

    # 尝试降级链中的每个 Provider
    _last_error: str | None = None
    for _pname in _provider_queue:
        if _pname in _tried_providers:
            continue
        _tried_providers.add(_pname)

        try:
            # --- 处理特殊 provider: qwen_image ---
            # qwen_image 不是注册的 provider ID，它使用 dashscope 适配器 + qwen-image 模型
            _actual_provider = _pname
            _model_override = None
            if _pname == "qwen_image":
                _actual_provider = "dashscope"
                _model_override = "qwen-image-3.0-pro"

            # 从图像配置构建适配器配置（优先图像 key，而非文本模型 key）
            _adapter_config = {
                "api_key": _cfg_api_key,
                "base_url": _cfg_base_url,
                "model": _model_override or _cfg_model,
                "default_size": _cfg_default_size,
                "default_ratio": section_size,
                "timeout": 60,
                "price_per_image": _cfg_price,
            }

            # 获取适配器实例（注意：get_adapter 需要传入 config）
            _adapter = ProviderRegistry.get_adapter(_actual_provider, _adapter_config)

            # ✅ R111（P2-6 · 渠道卫生）：渠道级前置短路——API Key 缺失时直接
            # 跳过该候选，不向渠道发必然 400/401 的请求（生产实证 dashscope
            # 文生图渠道 503/400 噪音：无 key 配置仍逐次白打一轮）。纯短路、
            # 零行为变更：旧实现下这类请求 100% 失败且已 continue，现只是
            # 把失败从「网络往返后」提前到「请求前」，并留 INFO 观测点。
            if not _adapter_config.get("api_key"):
                logger.info("generate_illustration_image: Provider '%s' 无 API Key，跳过（不发请求）", _pname)
                _last_error = f"Provider '{_pname}' 未配置 API Key"
                continue

            # 尺寸语义自适应：
            # - 适配器支持 1K/2K + ratio 风格（agnes 等）→ 传默认尺寸 + 宽高比
            # - 适配器只接受像素尺寸（dashscope 等）→ 传宽x高像素值
            _supported_sizes = getattr(_adapter, "supported_sizes", None)
            if _supported_sizes and _cfg_default_size in _supported_sizes:
                _size_arg, _ratio_arg = _cfg_default_size, section_size
            else:
                _size_arg, _ratio_arg = _pixel_size, None

            # 调用适配器生成图像
            _result = await _adapter.generate(
                prompt=_final_prompt,
                model=_adapter_config.get("model", ""),
                size=_size_arg,
                ratio=_ratio_arg,
            )

            # 提取图片 URL
            _image_url = (
                _result.get("image_url")
                or _result.get("url")
                or (_result.get("data", [{}])[0].get("url") if isinstance(_result.get("data"), list) else None)
            )

            if not _image_url and isinstance(_result, dict):
                for _key in ["url", "image", "imageUrl", "picture"]:
                    if _key in _result:
                        _image_url = _result[_key]
                        break

            if _image_url:
                logger.info(
                    "generate_illustration_image: Provider '%s' 生成成功 (原始: '%s')",
                    _actual_provider,
                    _pname,
                )
                # 写入缓存
                _image_cache_set(_cache_key, _image_url)
                return _image_url

            _last_error = f"Provider '{_pname}' 返回空 URL"
            logger.warning(
                "generate_illustration_image: Provider '%s' 返回空结果，尝试下一个",
                _pname,
            )

        except Exception as e:
            _last_error = f"Provider '{_pname}' 异常: {e}"
            # ✅ R111（P2-6 · 渠道卫生）：区分「确定性配置错误」与「瞬态故障」。
            # 适配器把 HTTP 状态码编码进 error 字符串（"HTTP 5xx: ..."）；
            # 4xx（400 参数/模型不存在 / 401 认证 / 403 无权限 / 404 端点不存在）
            # 属确定性配置错误——同型渠道重放必败，旧实现在日志里与 5xx 瞬态
            # 故障同等 WARNING，用户无从区分「配错了」和「服务抖了」。现对 4xx
            # 点名「确定性渠道配置错误（建议核对 Base URL / 模型名 / Key）」，
            # 降级链行为不变（仍 continue 下一候选）。
            _err_low = str(e).lower()
            _det_cfg = any(tag in _err_low for tag in ("http 400", "http 401", "http 402", "http 403", "http 404"))
            if _det_cfg:
                logger.warning(
                    "generate_illustration_image: Provider '%s' 命中确定性渠道配置"
                    "错误（%s）——同型渠道重放必败，请核对 Base URL / 模型名 / API Key；"
                    "继续尝试下一候选",
                    _pname,
                    str(e)[:160],
                )
            else:
                logger.warning(
                    "generate_illustration_image: Provider '%s' 失败 (%s)，尝试下一个",
                    _pname,
                    e,
                )
            # ✅ R196：旧实现此处还无条件重复打一条同文案 WARNING —— 每个
            #    provider 异常必然双重记录（4xx 三条），日志噪声干扰聚合排查，
            #    已删除（分支内的两条分级 WARNING 是唯一出口）。
            continue

    # 所有 Provider 均失败
    logger.error(
        "generate_illustration_image: 所有 Provider 均失败，prompt='%s...', last_error=%s",
        prompt[:80],
        _last_error,
    )
    # 缓存失败结果（避免重复失败尝试）
    _image_cache_set(_cache_key, None)
    return None


# ---------------------------------------------------------------------------
# ✅ G2（2026-09-30）：AI 配图全局预算 · 分段择优（纯函数、零 AI 成本、可单测）
#
# 上游设计（OpenBidKit 易标《标书智能体（六）》）：AI 可提名很多生图候选，但最终
# 只按 maxAiImages 择优执行；且把候选小节「分段」，在每一段里选优先级最高的，
# 避免前面章节把图片额度全部用完（前文 20 个候选、限 6 张时，前面几章不应独占
# 6 张，后面章节完全无图）。
#
# 本仓每个 ```ai_image``` 占位码 = 一张图。导出自动生图前先按文档位置把候选分段，
# 段内按 priority 降序取 top-k，跨段累计不超过 max_ai_images。
# 确定性实现（同输入同输出），不依赖随机，便于回归测试。
# ---------------------------------------------------------------------------


def apply_image_budget(
    candidates: list[dict],
    max_images: int,
) -> list[dict]:
    """对 AI 配图候选做全局预算分段择优。

    Args:
        candidates: 候选列表，每个元素为 dict，至少含：
            - ``key``    : 唯一标识（占位码 / 章节 id）
            - ``order``  : 大纲顺序（int，越小越靠前）
            - ``priority``: 优先级（int，可选，默认 0，越大越优先）
        max_images: 全局上限；<=0 表示不限制（向后兼容，原样返回）。

    Returns:
        应保留的候选子集（至多 ``max_images`` 个）。列表按「文档位置分段、段内优先
        级降序」选取，保证图片在全文均匀分布而非集中在前段。

    边界：
        - ``max_images<=0`` 或候选为空或候选数<=上限：返回原列表（行为不变）。
        - 返回数量严格 <= ``max_images``。
    """
    if max_images is None or max_images <= 0:
        return list(candidates)
    if not candidates:
        return []
    if len(candidates) <= max_images:
        return list(candidates)

    # 按文档顺序升序；同位置按优先级降序（越重要越靠前被优先选取）
    ordered = sorted(
        candidates,
        key=lambda c: (c.get("order", 0), -c.get("priority", 0)),
    )

    n = len(ordered)
    # 分段：每段约 max_images 个候选，使每个文档区段都能分到额度
    # seg_count = ceil(n / max_images)；seg_size = ceil(n / seg_count)
    seg_count = max(1, (n + max_images - 1) // max_images)
    seg_size = max(1, (n + seg_count - 1) // seg_count)

    per_seg = max(1, max_images // seg_count)
    remainder = max_images - per_seg * seg_count  # 余数顺次补给前若干段

    chosen: list[dict] = []
    for i in range(seg_count):
        start = i * seg_size
        if start >= n:
            break
        end = min(start + seg_size, n)
        seg = ordered[start:end]
        # 段内按优先级降序（整表已大致有序，段内再稳妥排一次）
        seg_sorted = sorted(seg, key=lambda c: -c.get("priority", 0))
        take = per_seg + (1 if i < remainder else 0)
        chosen.extend(seg_sorted[:take])
        if len(chosen) >= max_images:
            break
    return chosen[:max_images]


def select_ai_image_codes(
    groups: "dict[str, list[dict]]",
    order_of: "dict[str, int] | Callable[[str], int]",
    max_ai_images: int,
) -> "set[str]":
    """给定 ai_image 分组（占位码→块）与每码的文档顺序，按全局预算分段择优返回应保留的占位码集合。

    封装 ``apply_image_budget``：把每个占位码构造成候选（key=占位码, order=文档顺序,
    priority=0），预算内返回保留的占位码；``max_ai_images<=0`` 时返回全部占位码
    （向后兼容，等同关闭预算）。

    Args:
        groups: 占位码 -> 该码对应的块列表（与 export._auto_generate_ai_image_blocks 的 groups 同构）
        order_of: 占位码 -> 文档顺序（int）；可为 dict 或可调用对象
        max_ai_images: 全局上限（<=0 关闭）
    """
    if max_ai_images is None or max_ai_images <= 0:
        return set(groups.keys())
    if not groups:
        return set()

    def _ord(code: str) -> int:
        if callable(order_of):
            return order_of(code)
        return order_of.get(code, 0)

    candidates = [{"key": code, "order": _ord(code), "priority": 0} for code in groups]
    kept = apply_image_budget(candidates, max_ai_images)
    return {c["key"] for c in kept}


# ============================================================================
# 高保真图表规格 · 图片生成增强（文档 §8）
# ----------------------------------------------------------------------------
# ① 高保真渲染管线（§8.1）：多倍率 DPI 预设（screen / print / vector）
# ② AI 配图与结构化图表分工协同（§8.2）：正文语义 → 结构化图表 / AI 配图 / 纯文字
# ③ 工程配图 AI 生成 Prompt 模板（§8.3）：[IMG_GEN_PROMPT: ...] 解析
#
# 这些是**生成侧决策纯函数**（零 IO、零副作用），供正文编排 / 导出管线
# 在「该出结构化图表还是文生图配图」处消费；不侵入既有生成链路行为。
# ============================================================================

# 多倍率 DPI（§8.1）：渲染超采样倍率 / 截图 device_scale_factor 预设。
# 现有 PIL v2 商业级渲染器固定 2.5x 超采样（见 mermaid_common._render_hq），
# 本表供高保真图表导出器按用途切换（screen 预览 / print 打印评审 / vector 矢量级）。
IMAGE_SCALE_PRESETS: dict[str, int] = {
    "screen": 2,
    "print": 3,
    "vector": 4,
}


def route_visual_strategy(text: str) -> dict:
    """AI 配图与结构化图表的分工协同决策（§8.2）。

    协同决策流：
        正文段 → 含进度/组织/工艺关键词？
          ├─ 是 → 结构化图表管线（route="structured"）
          └─ 否 → 需要氛围/示意配图？
                   ├─ 是 → AI 配图（route="illustration"）
                   └─ 否 → 纯文字（route="none"）

    Args:
        text: 正文段落 / 章节文本。

    Returns:
        ``{"route": "structured"|"illustration"|"none",
        "chart_type": str|None, "reason": str}`` —— ``chart_type`` 仅
        structured 路由时非 None（gantt/architecture/flowchart）。
    """
    from app.services.chart_validators import detect_chart_type

    t = text or ""
    ctype = detect_chart_type(t)
    if ctype:
        return {
            "route": "structured",
            "chart_type": ctype,
            "reason": "命中图表生成关键词，走结构化图表管线（七段式 Prompt → JSON → 渲染）",
        }
    if any(kw in t for kw in (
        "效果图", "示意图", "三维", "实景", "渲染", "氛围", "鸟瞰", "透视", "封面",
    )):
        return {
            "route": "illustration",
            "chart_type": None,
            "reason": "未命中结构化图表关键词，需氛围/示意配图，走 AI 文生图",
        }
    return {
        "route": "none",
        "chart_type": None,
        "reason": "既无结构化图表关键词、也无配图需求，纯文字",
    }


def parse_img_gen_prompt(text: str) -> dict:
    """解析工程配图 AI 生成 Prompt 模板（§8.3）。

    模板形态：
        [IMG_GEN_PROMPT: 主题=钢结构节点焊接工艺三维示意；风格=写实工程渲染、
        蓝灰工业色调、浅景深；构图=等轴测视角、白底干净背景、无文字水印；
        比例=16:9；负面词=手绘、卡通、模糊、文字]

    Args:
        text: 含 ``[IMG_GEN_PROMPT: ...]`` 模板的文本。

    Returns:
        命中：``{"ok": True, "subject":..., "style":..., "composition":...,
        "ratio":..., "negative":..., "prompt": <已构建的完整文生图提示词>}``；
        未命中：``{"ok": False, "reason": "未命中 [IMG_GEN_PROMPT: ...] 模板标记"}``。
    """
    m = re.search(r"\[\s*IMG_GEN_PROMPT\s*:\s*(.+?)\s*\]", text or "", re.S)
    if not m:
        return {"ok": False, "reason": "未命中 [IMG_GEN_PROMPT: ...] 模板标记"}
    fields: dict[str, str] = {}
    for part in re.split(r"[；;]", m.group(1)):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            fields[k.strip()] = v.strip()
    subject = fields.get("主题", "").strip()
    style = fields.get("风格", "").strip()
    composition = fields.get("构图", "").strip()
    ratio = fields.get("比例", "16:9").strip()
    negative = fields.get("负面词", "").strip()
    seg = []
    if subject:
        seg.append(f"主题：{subject}")
    if style:
        seg.append(f"风格：{style}")
    if composition:
        seg.append(f"构图：{composition}")
    if ratio:
        seg.append(f"画面比例：{ratio}")
    if negative:
        seg.append(f"避免（负面词）：{negative}")
    return {
        "ok": True,
        "subject": subject,
        "style": style,
        "composition": composition,
        "ratio": ratio,
        "negative": negative,
        "prompt": "；".join(seg),
    }


async def resolve_image_config(db, settings) -> dict:
    """读取当前图像模型配置（DB 优先、settings 兜底）。

    Args:
        db: aiosqlite 连接（可为 None，此时直接回落 settings）。
        settings: ``app.config.settings``（env 兜底）。

    Returns:
        dict，键 = image_enabled / image_api_key_enc / image_base_url / image_model /
        image_default_size / image_price_per_image / image_provider /
        image_max_concurrency / max_ai_images。
        无 DB 行时逐字段回落 settings，**默认行为与引入前完全一致**（零变化）。
    """
    def _get(name: str, default=""):
        return getattr(settings, name, default) if settings is not None else default

    row = None
    if db is not None:
        try:
            cur = await db.execute(
                "SELECT provider, model, base_url, api_key_encrypted, enabled, "
                "default_size, concurrency, price_per_image, max_ai_images "
                "FROM image_model_config WHERE id = 'primary'"
            )
            row = await cur.fetchone()
        except Exception as e:
            logger.warning("resolve_image_config: 读 image_model_config 失败，回落 settings: %s", e)
            row = None

    if row is None:
        return {
            "image_enabled": bool(_get("image_enabled", True)),
            "image_api_key_enc": _get("image_api_key", ""),
            "image_base_url": _get("image_base_url", ""),
            "image_model": _get("image_model", ""),
            "image_default_size": _get("image_default_size", "2K"),
            "image_price_per_image": float(_get("image_price_per_image", 0.0) or 0.0),
            "image_provider": _get("image_provider", "agnes"),
            "image_max_concurrency": int(_get("image_max_concurrency", 2) or 2),
            "max_ai_images": int(_get("max_ai_images", 0) or 0),
        }

    # aiosqlite Row 支持 keys()；兼容 tuple 形态用索引
    if hasattr(row, "keys"):
        _k = lambda key: row[key]  # noqa: E731
    else:
        _k = lambda key: row[("provider", "model", "base_url", "api_key_encrypted",
                              "enabled", "default_size", "concurrency",
                              "price_per_image", "max_ai_images").index(key)]  # noqa: E731

    return {
        "image_enabled": bool(_k("enabled")),
        "image_api_key_enc": _k("api_key_encrypted") or "",
        "image_base_url": _k("base_url") or "",
        "image_model": _k("model") or "",
        "image_default_size": _k("default_size") or "2K",
        "image_price_per_image": float(_k("price_per_image") or 0.0),
        "image_provider": _k("provider") or "custom",
        "image_max_concurrency": int(_k("concurrency") or 2),
        "max_ai_images": int(_k("max_ai_images") or 0),
    }
