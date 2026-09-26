# -*- coding: utf-8 -*-
"""统一文档数据模型：解析器、检测器、生成器、导出器之间以此交换数据。

设计要点：
- Document.blocks 按文档顺序排列，block.id 即顺序索引，是插入锚点定位的基础；
- 每个 Block 携带 src（行号/段落号）与 native（原文档对象引用），
  以便导出时"回插"而不破坏原结构；
- ChartGap 描述一处"应配图但缺图"，ChartData/ChartSpec 描述补图所需数据与配置。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 文档块
# ---------------------------------------------------------------------------

class BlockKind(str, Enum):
    HEADING = "heading"      # 标题（携带 level）
    PARAGRAPH = "paragraph"  # 正文段落
    TABLE = "table"          # 表格
    IMAGE = "image"          # 图片/图形
    CAPTION = "caption"      # 图注（如"图 4-1 xxx"）
    LIST = "list"            # 列表项
    CODE = "code"            # 代码块/引用块


@dataclass
class Block:
    """文档中的一个结构块。"""

    id: int                            # 文档顺序索引（全局唯一起点）
    kind: BlockKind
    text: str = ""                     # 纯文本内容（表格为首行摘要或说明文字）
    level: int = 0                     # 标题层级（h1=1），非标题为 0
    rows: list[list[str]] = field(default_factory=list)  # 表格数据（含表头行）
    section: str = ""                  # 所属章节面包屑，如 "第四章 > 2 架体搭设"
    src: dict = field(default_factory=dict)   # 源位置：{line_start,line_end} 或 {para_idx}
    native: Any = None                 # 原文档对象引用（docx 段落等），供回插
    image_ref: str = ""                # 图片块的路径/引用




# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------

@dataclass
class Document:
    source_path: str
    fmt: str                           # "markdown" | "html" | "docx"
    blocks: list[Block] = field(default_factory=list)
    raw: str = ""                      # 原始文本（markdown/html 导出回插需要）
    native: Any = None                 # 原生文档对象（docx 的 Document 等）

    def headings(self) -> list[Block]:
        return [b for b in self.blocks if b.kind == BlockKind.HEADING]

    def nearest_heading(self, block_id: int) -> str:
        """向上找到最近的标题，用于报告定位。"""
        for b in reversed(self.blocks[: block_id + 1]):
            if b.kind == BlockKind.HEADING:
                return b.text
        return "(文档开头)"


# ---------------------------------------------------------------------------
# 图表数据与规格
# ---------------------------------------------------------------------------

@dataclass
class Series:
    name: str
    values: list[float] = field(default_factory=list)


@dataclass
class ChartData:
    """从表格/正文中抽取出的结构化数据。"""

    title: str = ""
    categories: list[str] = field(default_factory=list)      # X 轴/扇区标签
    series: list[Series] = field(default_factory=list)        # 数值系列
    x_label: str = ""
    y_label: str = ""
    source_block_id: int = -1                                 # 数据来源块
    complete: bool = True                                     # 数据是否完整（无占位符）

    def numeric_series(self) -> list[Series]:
        return [s for s in self.series if any(v is not None for v in s.values)]


@dataclass
class ChartSpec:
    """渲染一份图表所需完整配置。data 与 steps 二选一。"""

    chart_type: str                    # bar/line/pie/scatter/flow/relation/timeline
    title: str = ""
    data: Optional[ChartData] = None
    steps: list[str] = field(default_factory=list)   # 流程图步骤（数据不足时的示意路径）
    note: str = ""                     # 图下方补充说明（如"示意流程，待项目实测数据"）


# ---------------------------------------------------------------------------
# 缺失检测结果
# ---------------------------------------------------------------------------

class GapType(str, Enum):
    DANGLING_REF = "dangling_reference"        # 引用了图但图不存在
    CAPTION_NO_IMAGE = "caption_without_image" # 图注无图
    EMPTY_PLACEHOLDER = "empty_placeholder"    # 空图表占位符
    TABLE_NO_CHART = "table_without_chart"     # 有表无图
    SEMANTIC_HINT = "semantic_visualization"   # 语义上适合可视化
    BROKEN_NUMBERING = "broken_figure_number"  # 图号引用断裂


@dataclass
class ChartGap:
    anchor_block_id: int               # 插入锚点块
    gap_type: GapType
    reason: str                        # 缺失原因（人类可读）
    confidence: float                  # 置信度 0~1
    suggested_type: str                # 建议图表类型
    caption: str                       # 建议图注
    needed_data: str = ""              # 所需数据说明
    data: Optional[ChartData] = None   # 已抽取到的数据（可能为示意流程）
    steps: list[str] = field(default_factory=list)  # 流程/结构类示意步骤
    needs_data: bool = False           # 数据不足，仅能出示意图或需人工补充
    insert_before: bool = False        # True=图插在锚点块之前（如图注无图），否则之后
    skip_reason: str = ""              # 无法生成时的原因与补数建议
    status: str = "pending"            # pending/filled/skipped(附原因)
    output_image: str = ""             # 生成后的图片路径
    section: str = ""                  # 所属章节，便于报告阅读
    final_type: str = ""               # 推荐器最终确定的类型

    def to_dict(self) -> dict:
        return {
            "anchor_block_id": self.anchor_block_id,
            "gap_type": self.gap_type.value,
            "reason": self.reason,
            "confidence": round(self.confidence, 3),
            "suggested_type": self.suggested_type,
            "caption": self.caption,
            "needed_data": self.needed_data,
            "needs_data": self.needs_data,
            "has_data": self.data is not None or bool(self.steps),
            "insert_before": self.insert_before,
            "status": self.status,
            "skip_reason": self.skip_reason,
            "final_type": self.final_type,
            "output_image": self.output_image,
            "section": self.section,
        }


# ---------------------------------------------------------------------------
# 通用文本工具：图号、占位符识别
# ---------------------------------------------------------------------------

# "图 4-1 xxx"/"图4-1"等图注编号；第二捕获组记录编号与标题间的分隔符，
# 用于区分真正的图注与"图4所示的…"这类以图号开头的引用句式
FIGURE_NO_RE = re.compile(
    r"^\s*图\s*([\d]+(?:[-—.][\d]+)*)((?:\s*[：:.、]\s*|\s+)?)\s*(.*)$")
# 单段编号（如"图4"）后若直接接这些词，属于引用句式而非图注
_REF_TITLE_LEAD = ("所", "示", "如", "见", "的")
# 表号（与图注同一句式规则）
TABLE_NO_RE = re.compile(
    r"^\s*表\s*([\d]+(?:[-—.][\d]+)*)((?:\s*[：:.、]\s*|\s+)?)\s*(.*)$")
# 空占位符：【插入图表】【待补充：图表】(chart placeholder) 等
PLACEHOLDER_RE = re.compile(
    r"[【\[(（]\s*(?:此处)?(?:插入|补充|待补充|待插入)\s*(?:图表?|图|示意|曲线|流程图?|架构图?)\s*[】\])）]"
)
# 数据待补充占位（用于判断表格数据完整性）
TODO_RE = re.compile(r"【待补充[^】]*】|_{3,}|TBD|待定", re.IGNORECASE)


def _split_caption(m: Optional[re.Match]) -> Optional[tuple[str, str]]:
    """对图/表号正则的匹配结果做句式校验，返回 (编号, 标题) 或 None。

    排除两类误判：
    - "图4所示的架体…"：编号后无分隔符且标题以引用词开头；
    - "图4层结构…"：单段编号与正文直连（无分隔符）视为引用句式。
    多段编号（如 4-1）允许直连标题，如"图4-1荷载统计"。
    """
    if not m:
        return None
    num, sep, rest = m.group(1), m.group(2), (m.group(3) or "").strip()
    if not sep:
        if rest.startswith(_REF_TITLE_LEAD):
            return None
        if not any(ch in num for ch in "-—."):
            return None
    return num.rstrip(".。"), rest


def figure_caption_of(text: str) -> Optional[tuple[str, str]]:
    """若文本是图注，返回 (图号, 标题)，否则 None。"""
    return _split_caption(FIGURE_NO_RE.match(text or ""))


def table_caption_of(text: str) -> Optional[tuple[str, str]]:
    """若文本是表注（"表 x-y 标题"），返回 (表号, 标题)，否则 None。"""
    return _split_caption(TABLE_NO_RE.match(text or ""))
