# -*- coding: utf-8 -*-
"""检测规则库：五类"应配图但缺图"信号的识别。

不用单一关键词匹配——每条规则都结合：
- 上下文窗口内是否已有图片/图注（BlockKind.IMAGE/CAPTION）；
- 标题层级（同章节内查找）；
- 表格数值密度（是否有可视化价值）；
- 图号引用与图注编号的对应关系。
"""

from __future__ import annotations

import re

from ..models import TODO_RE

# --- 引用模式 -------------------------------------------------------------

# 带编号引用："如图 4-2 所示"/"见图7"/"参见图 3-1"
REF_NUMBERED_RE = re.compile(
    r"(?:如|见|参见|详见)?\s*图\s*(\d+(?:[-—.]\d+)*)\s*[所示]{0,2}"
)
# 无编号引用："如下图所示"/"见下图"/"示意如下"
REF_UNNUMBERED_RE = re.compile(
    r"(?:如下|下图|附图)[所示意的]{0,3}(?:图|所示|示意)|见下图|如图下方|流程图如下|示意图如下"
)

# --- 语义信号：关键词 -> 建议图表类型 --------------------------------------

SEMANTIC_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # (正则, 建议类型, 所需数据说明)
    (re.compile(r"趋势|变化曲线|增减|逐月|随.{0,6}(?:上升|下降|变化)"), "line",
     "两个及以上时间/顺序点的量化数值"),
    (re.compile(r"占比|构成|比例|分布情况|百分比"), "pie",
     "各分项名称及其百分比或频次"),
    (re.compile(r"对比|对照|较.{0,4}(?:高|低|多|少)|优于|低于"), "bar",
     "同类多项的可比数值（≥2 组）"),
    (re.compile(r"流程|工序|顺序为|依次(?:进行|施工|拆除)?|先.{1,24}后.{1,24}"), "flow",
     "有序步骤序列（可由文字抽取）"),
    (re.compile(r"组织架构|层级|体系结构|网络架构|架构"), "relation",
     "节点及上下级/连接关系"),
    (re.compile(r"里程碑|工期节点|时间轴|进度计划"), "timeline",
     "事件名称 + 日期区间"),
    (re.compile(r"相关性|散点|两变量"), "scatter",
     "成对的二维观测值"),
]

# 流程链文本："A→B→C" / "A->B->C"
FLOW_CHAIN_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9（）()]{2,12}(?:\s*(?:→|->|＞|>)\s*[\u4e00-\u9fffA-Za-z0-9（）()]{2,12}){1,10})")
# "拆除顺序为：安全网、挡脚板、脚手板…" 类枚举
SEQ_ENUM_RE = re.compile(r"(?:顺序|步骤|流程)(?:为|如下)?[:：]?\s*((?:[\u4e00-\u9fffA-Za-z0-9]{2,12}[、，,]){2,}[\u4e00-\u9fffA-Za-z0-9]{2,12})")


def extract_flow_steps(text: str) -> list[str]:
    """从正文中抽取流程步骤（箭头链或顿号枚举）。"""
    m = FLOW_CHAIN_RE.search(text)
    if m:
        parts = re.split(r"\s*(?:→|->|＞|>)\s*", m.group(1))
        return [p.strip() for p in parts if p.strip()]
    m = SEQ_ENUM_RE.search(text)
    if m:
        parts = re.split(r"[、，,]", m.group(1))
        return [p.strip() for p in parts if p.strip()]
    return []


def numeric_cell(value: str) -> float | None:
    """从单元格文本解析出一个数值（支持百分号、千分位、单位后缀）。"""
    if not value or TODO_RE.search(value):
        return None
    s = value.replace(",", "").replace("，", "")
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return None
    v = float(m.group(0))
    if "%" in s:
        return v  # 百分数值原样保留
    return v


def table_numeric_density(rows: list[list[str]]) -> tuple[float, list[int]]:
    """返回 (整体数值密度, 数值列下标列表)。首列为标签列不参与统计。"""
    if len(rows) < 2:
        return 0.0, []
    n_cols = max(len(r) for r in rows)
    numeric_cols: list[int] = []
    data_rows = rows[1:]
    for c in range(1, n_cols):
        vals = [r[c] for r in data_rows if c < len(r) and r[c].strip()]
        if not vals:
            continue
        ok = sum(1 for v in vals if numeric_cell(v) is not None)
        if ok / len(vals) >= 0.6:
            numeric_cols.append(c)
    total_cells = sum(len([x for x in r if x.strip()]) for r in data_rows)
    num_cells = sum(
        1 for r in data_rows for c, x in enumerate(r)
        if c >= 1 and x.strip() and numeric_cell(x) is not None
    )
    density = num_cells / total_cells if total_cells else 0.0
    return density, numeric_cols


def table_todo_ratio(rows: list[list[str]]) -> float:
    """除首列外，数据单元格中占位符（待补充）的比例。"""
    cells = [c for r in rows[1:] for i, c in enumerate(r)
             if c.strip() and i >= 1]
    if not cells:
        return 0.0
    return sum(1 for c in cells if TODO_RE.search(c)) / len(cells)


def suggest_type_from_text(text: str) -> tuple[str, str] | None:
    """按语义关键词给出 (图表类型, 数据需求)。"""
    for pat, ctype, need in SEMANTIC_PATTERNS:
        if pat.search(text):
            return ctype, need
    return None
