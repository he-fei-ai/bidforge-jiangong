# -*- coding: utf-8 -*-
"""导出前的「可选内容自动改写」（config.auto_rewrite_content=true 时启用）。

与导出器默认的「只体检、不篡改」策略互补：本模块只做**无损 / 低风险**的
格式与词汇修复，**绝不臆造任何数据**——`【待补充】`、空章节、被 max_tokens
截断的半句一律不补，因此对「危大工程」等安全敏感的专项方案不会引入误导性内容。

三类改写（均在正文 Markdown 层完成，之后交导出器正常渲染）：

1. :func:`html_table_to_gfm`
   把从 Excel/Word 粘贴表格带入的 HTML 表格（``<table><tr><td>``）转成
   GFM 表格，交给导出器渲染为**原生 Word 表格**。旧行为（v14）是把这些标签
   扁平化成纯文本，虽消除了乱码但丢失了表格结构。

2. :func:`fix_english_leak`
   修正 AI 生成中文正文里偶发的英文串写（``brick``→砖料、``material``→材料）。
   词表高度收敛，且只在**代码围栏之外**、孤立小写词处替换，避免误伤
   标准编号（Q235B / JGJ 130 / C20）与 chart-json 载荷键名。

3. :func:`collapse_duplicate_blocks`
   合并**相邻且完全相同**的非空行（重复段落、重复子标题）。精确匹配，
   不做任何语义判断（语义级"1.7 与 1.1 近似重复"不在自动处理范围）。

所有改写**默认关闭**：不传 ``auto_rewrite_content`` 时导出行为与既往完全一致。
"""
from __future__ import annotations

import html as _html
import re

# ---------------------------------------------------------------------------
# 代码围栏切分：改写只作用于围栏外的正文，保护 ```mermaid / ```chart-json /
# ```python 等代码块内容不被破坏（否则英文替换会改坏 JSON 键名、导致渲染失败）。
# ---------------------------------------------------------------------------

_FENCE_MARK = "```"


def _split_fences(text: str) -> list[tuple[bool, str]]:
    """把文本切成 (是否在代码围栏内, 片段) 序列，围栏分隔行本身归入围栏侧。"""
    lines = text.split("\n")
    segs: list[tuple[bool, list[str]]] = []
    buf: list[str] = []
    in_fence = False
    for ln in lines:
        if ln.lstrip().startswith(_FENCE_MARK):
            if not in_fence:
                if buf:
                    segs.append((False, buf))
                    buf = []
                in_fence = True
                buf.append(ln)
            else:
                buf.append(ln)
                segs.append((True, buf))
                buf = []
                in_fence = False
            continue
        buf.append(ln)
    if buf:
        segs.append((in_fence, buf))
    return [(is_fence, "\n".join(ls)) for is_fence, ls in segs]


def _apply_outside_fences(text: str, fn) -> tuple[str, int]:
    """对每个「围栏外」片段执行 ``fn(seg) -> (new_seg, count)``，返回 (新文本, 总计数)。"""
    total = 0
    out: list[str] = []
    for is_fence, seg in _split_fences(text):
        if not is_fence:
            seg, n = fn(seg)
            total += n
        out.append(seg)
    return "\n".join(out), total


# ---------------------------------------------------------------------------
# ① HTML / 剪贴板表格 → GFM 表格
# ---------------------------------------------------------------------------

_TABLE_RE = re.compile(r"<table\b[^>]*>(.*?)</table>", re.I | re.S)
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.I | re.S)
# 捕获组 1 = d/h（td 或 th），组 2 = 单元格内容
_CELL_RE = re.compile(r"<t([dh])\b[^>]*>(.*?)</t\1>", re.I | re.S)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# 单元格内需要清洗的换行/剪贴板标记
_CELL_NOISE_RE = re.compile(r"</?(?:fcel|lcel|ucel|xc|nl)\s*/?>|<br\s*/?>", re.I)


def _cell_text(raw: str) -> str:
    """单元格 HTML → GFM 安全纯文本（去标签/标记、反转义实体、压空白、转义竖线）。"""
    t = _CELL_NOISE_RE.sub(" ", raw)
    t = _TAG_STRIP_RE.sub("", t)
    t = _html.unescape(t)
    t = _WS_RE.sub(" ", t).strip()
    return t.replace("|", "／")  # 竖线会破坏 GFM 表格列结构，替换为全角斜杠


def html_table_to_gfm(text: str) -> tuple[str, int]:
    """把正文中的 HTML 表格转成 GFM 表格；无 ``<tr>`` 结构的畸形表原样保留。

    返回 ``(新文本, 转换表格数)``。转换后的表以空行包裹，独占成块，
    确保 :func:`export._parse_content_blocks` 能按表格行识别。
    """
    count = 0

    def _repl(m: re.Match) -> str:
        nonlocal count
        rows = _ROW_RE.findall(m.group(1))
        if not rows:
            return m.group(0)  # 没有行结构，交回上层（v14 扁平化兜底）
        grid: list[list[str]] = []
        for r in rows:
            cells = [_cell_text(c) for (_k, c) in _CELL_RE.findall(r)]
            if any(cells):  # 跳过整行皆空（Excel 粘贴常见尾部空行）
                grid.append(cells)
        if not grid:
            return m.group(0)
        ncol = max(len(row) for row in grid)
        for row in grid:
            while len(row) < ncol:
                row.append("")
        lines = ["| " + " | ".join(grid[0]) + " |",
                 "| " + " | ".join(["---"] * ncol) + " |"]
        lines += ["| " + " | ".join(row) + " |" for row in grid[1:]]
        count += 1
        return "\n\n" + "\n".join(lines) + "\n\n"

    out = _TABLE_RE.sub(_repl, text)
    return out, count


# ---------------------------------------------------------------------------
# ② 英文串写 → 中文（收敛词表，只在孤立小写词处替换）
# ---------------------------------------------------------------------------

_EN_LEAK_MAP = {
    "brick": "砖料",
    "material": "材料",
    "materials": "材料",
}
# 前后不接字母 → 不切断 Q235B / HPB300 等编号；仅小写 → 不碰句首大写英文专名
_EN_LEAK_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(_EN_LEAK_MAP) + r")(?![A-Za-z])")


def fix_english_leak(text: str) -> tuple[str, int]:
    """替换 AI 中文正文里偶发的英文串写词；返回 ``(新文本, 替换次数)``。"""
    cnt = 0

    def _r(m: re.Match) -> str:
        nonlocal cnt
        cnt += 1
        return _EN_LEAK_MAP[m.group(1)]

    return _EN_LEAK_RE.sub(_r, text), cnt


# ---------------------------------------------------------------------------
# ③ 相邻完全重复块合并
# ---------------------------------------------------------------------------

def collapse_duplicate_blocks(text: str) -> tuple[str, int]:
    """删除与前一条非空行**完全相同**的行（重复段落/重复子标题）；返回 (新文本, 删除数)。"""
    lines = text.split("\n")
    out: list[str] = []
    removed = 0
    last = ""
    for ln in lines:
        s = ln.strip()
        if s and s == last:
            removed += 1
            continue
        out.append(ln)
        if s:
            last = s
    return "\n".join(out), removed


# ---------------------------------------------------------------------------
# 编排：对单个章节正文执行三类改写（均在代码围栏外）
# ---------------------------------------------------------------------------

def normalize_section_content(text: str) -> tuple[str, dict]:
    """返回 ``(改写后正文, {"tables":n,"english":n,"dupes":n})``。

    顺序：先表格结构化（HTML→GFM），再词汇修复（英文串写），最后去重。
    表格先转 GFM 可让后续英文替换覆盖到表格单元格文本。
    """
    stats = {"tables": 0, "english": 0, "dupes": 0}
    if not text:
        return text, stats

    def _t(seg: str) -> tuple[str, int]:
        out, n = html_table_to_gfm(seg)
        stats["tables"] += n
        return out, n

    def _e(seg: str) -> tuple[str, int]:
        out, n = fix_english_leak(seg)
        stats["english"] += n
        return out, n

    def _d(seg: str) -> tuple[str, int]:
        out, n = collapse_duplicate_blocks(seg)
        stats["dupes"] += n
        return out, n

    for fn in (_t, _e, _d):
        text, _ = _apply_outside_fences(text, fn)
    return text, stats
