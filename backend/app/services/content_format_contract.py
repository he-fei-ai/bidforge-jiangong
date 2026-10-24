# -*- coding: utf-8 -*-
"""三端格式契约与正文格式自检（R130 · 2026-10-22）。

【背景】正文生成、图表生成、DOCX 导出三条链路此前各有一套格式口径：
正文按提示词写 Markdown，图表按 `_chart_pipeline` 的白名单登记，导出按
`export` 的渲染器消费——三者没有一份**共同引用**的契约，于是出现
「正文写了一种、图表按另一种解析、导出按第三种渲染」的漂移（后台日志里
表现为：表格降级纯文本、代码块未闭合被跳过、子标题层级不齐、图表渲染
失败不占号等高频告警）。

【本模块职责（契约的唯一事实源）】
1. **契约常量**：字体、图题格式、Mermaid 图表类型白名单、编号规则、
   图片分辨率下限 —— 提示词、正文自检、导出渲染三方**一律 import 本模块**，
   禁止再各自复制字面量（本仓「同一判据多处副本」头号反模式）。
2. **正文格式自检** :func:`check_format_contract`：正文生成后对落库 Markdown
   跑一次确定性检查（标题 / 列表 / 表格 / 图表 / 公式 / 编码 / 图题 七类），
   供「三层校验」的第一层（正文生成后自检）与第二层（导出前校验）共用
   同一份判据 —— 两端结论天然一致，不存在两套口径。

【设计原则（对齐本仓既有承诺）】
- 纯函数、零 DB、零 AI、零 IO；任何异常都降级为空报告（fail-soft），
  绝不阻断正文落库、绝不改写正文内容；
- 校验**只上报不篡改**：修复一律交给既有链路（`fence_common.normalize_*`
  围栏归一、`content_blocks` 形态重排、`export.clean_text` 乱码清理、
  `numbering` 编号同步），本模块只产出结构化结论；
- severity 三档：`error`（不修必然出错，如未闭合围栏 / 表格语法坏）、
  `warning`（大概率降级，如图表类型不在白名单）、`advisory`（风格建议，
  如手工序号未用标准 Markdown 列表语法）。
"""
from __future__ import annotations

import re

from app.services.content_blocks import INLINE_CHART_FENCE_LANGS as _FENCE_LANGS
from app.services.fence_common import find_unclosed, iter_all_blocks

# ---------------------------------------------------------------------------
# 一、契约常量（三端唯一事实源）
# ---------------------------------------------------------------------------

#: 文字标准 · 中文字体
FONT_BODY_EAST_ASIA = "宋体"        #: 正文中文字体（备选：仿宋，见 FONT_BODY_ALT）
FONT_BODY_ALT_EAST_ASIA = "仿宋"    #: 导出配置可选的正文备选字体
FONT_HEADING_EAST_ASIA = "黑体"     #: 标题中文字体（标题一律黑体、不倾斜）
#: 文字标准 · 西文字体（Times New Roman 统一西文，写入 w:ascii / w:hAnsi）
FONT_LATIN = "Times New Roman"

#: 图表标准 · 图片分辨率下限（dpi），低于此值视为不达商业级交付标准
MIN_IMAGE_DPI = 150

#: 图表标准 · Mermaid 源码围栏允许的 diagram 类型（首行关键字）。
#: 口径 =「渲染器已验证可出图」的类型：流程图 / 层级图 / 职责关系图 /
#: 甘特图 / 时间线；chart-json 载荷（architecture / labor / layout /
#: comparison / flowchart 数据型）不在此列——它们走 JSON 校验器 + PIL v2。
MERMAID_SOURCE_TYPES: tuple[str, ...] = (
    "flowchart", "graph", "sequenceDiagram", "gantt", "timeline",
)

#: 图表标准 · 围栏语言白名单（正文允许内嵌的代码块语言）
#: 与 `content_blocks.INLINE_CHART_FENCE_LANGS` 同源（此处只读引用，不复制元组）。
CHART_FENCE_LANGS: tuple[str, ...] = tuple(_FENCE_LANGS)

#: 图号标准 · 图题 / 表题前缀格式：`图{章号}-{序号} {图名}`、`表{章号}-{序号} {表名}`
FIG_TITLE_RE = re.compile(r"^图\s*\d+\s*[-–—]\s*\d+\s+\S")
TBL_TITLE_RE = re.compile(r"^表\s*\d+\s*[-–—]\s*\d+\s+\S")

#: 图号前缀**剥离**判据（R131-A · 2026-10-23）。
#: 由 :data:`FIG_TITLE_RE` 去掉末尾「图名必须存在」的 `\s+\S` 派生 —— 全仓只有
#: ``FIG_TITLE_RE`` 一份字面量，两式永不漂移（R131-A 护栏锁定该派生关系）。
#: 语义：``FIG_TITLE_RE`` 判定为规范图题的文本（``图 4-1 劳动力配置计划``），
#: 其开头的「图{章}-{序}」一律是生成期残留、必须剥离；派生式比原式更宽
#: （不强制图名后缀），因此连无空格形态 ``图4-1机械设备管理流程`` 一并覆盖 ——
#: 这类形态本就由 ``_check_numbers`` 上报为不规范图题，成稿不得再叠一层导出图号。
FIG_TITLE_PREFIX_RE = re.compile(
    FIG_TITLE_RE.pattern[:FIG_TITLE_RE.pattern.rfind(r"\s+")])

#: 编号标准 · L1 用中文数字（一、二、…），L2 及以下用阿拉伯数字（1.1 / 1.1.1）。
#: 正文里的一级编号形态（供自检识别「L1 写成阿拉伯」的偏差）。
L1_CN_RE = re.compile(r"^[一二三四五六七八九十]+[、．.]")
L1_ARABIC_RE = re.compile(r"^第?\s*\d+\s*[、.．]")

#: 六类标准的分类键（报告 issues 的 category 取值）
CONTRACT_CATEGORIES: tuple[str, ...] = (
    "text", "symbol", "formula", "chart", "table", "number", "numbering",
)

# ---------------------------------------------------------------------------
# 二、判据正则（仅本模块使用；导出侧的残留判据仍在 content_residue_rules）
# ---------------------------------------------------------------------------

#: 标题行：`#` 后必须有空格（`#标题` 是非法 Markdown，渲染器会当普通段落）
_HEADING_BAD_RE = re.compile(r"^#{1,6}(?![#\s])\S")
#: 标题行内出现行内格式标记（标题应走命名样式，不得内联加粗/斜体/代码）
_HEADING_INLINE_RE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`|(?<!\*)\*[^*\n]+\*(?!\*))")

#: 手工列表序号（未走标准 Markdown 列表语法）：
#: 「1、」「1.」「（一）」「(1)」「1)」开头且后面直接跟正文
_MANUAL_LIST_RE = re.compile(
    r"^\s*(?:"
    r"\d{1,3}[、．]\s*\S"                       # 1、正文
    r"|[（(]\s*(?:\d{1,3}|[一二三四五六七八九十]+)\s*[)）]\s*\S"  # （一）正文
    r"|\d{1,3}[）)]\s*\S"                        # 1) 正文
    r")"
)
#: 标准 Markdown 列表语法（命中即不算手工序号）
_MD_LIST_RE = re.compile(r"^\s*(?:[-*+]\s|\d+(?:\.\d+)*[.)]\s)")

#: 表格行与 GFM 分隔行
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(
    r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")

#: 公式定界符（LaTeX 标准输入：行内 $...$，块级 $$...$$）
_DISPLAY_MATH_RE = re.compile(r"\$\$[\s\S]*?\$\$")

#: 编码问题
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_REPLACEMENT_RE = re.compile("\ufffd")
_CYRILLIC_RE = re.compile("[\u0400-\u04FF]")
#: GBK/编码 mojibake 的典型形态（GBK 双字节误按 UTF-8 解码后的固定产物）
_MOJIBAKE_TOKENS: tuple[str, ...] = ("锟斤拷", "ï¿½", "Ã¤", "â€", "銆", "鈥")

#: 每类最多上报条数（报告体积控制，与导出体检口径一致）
_MAX_ISSUES_PER_CATEGORY = 5


# ---------------------------------------------------------------------------
# 三、正文格式自检（第一层 / 第二层共用判据）
# ---------------------------------------------------------------------------

def _issue(category: str, severity: str, message: str, line: int,
           excerpt: str = "") -> dict:
    return {
        "category": category,
        "severity": severity,
        "message": message,
        "line": line,
        "excerpt": (excerpt or "")[:80],
    }


def _iter_prose_lines(content: str):
    """产出 (行号1基, 行文本)，跳过全部围栏块内部行（含未闭合块到文末）。"""
    lines = content.split("\n")
    protected: set[int] = set()
    for blk in iter_all_blocks(content):
        stop = blk["close_line"] if blk["close_line"] >= 0 else len(lines) - 1
        protected.update(range(blk["open_line"], stop + 1))
    for idx, raw in enumerate(lines):
        if idx in protected:
            continue
        yield idx + 1, raw


def _check_headings(content: str, issues: list[dict]) -> None:
    """文字标准：标题语法（# 后空格）与「标题不内联格式」。"""
    seen = 0
    for ln, raw in _iter_prose_lines(content):
        if not raw.lstrip().startswith("#"):
            continue
        stripped = raw.lstrip()
        if _HEADING_BAD_RE.match(stripped):
            if seen < _MAX_ISSUES_PER_CATEGORY:
                issues.append(_issue(
                    "text", "error",
                    f"标题语法非法：`{stripped[:6]}` 后缺少空格，渲染器不会识别为标题",
                    ln, stripped))
                seen += 1
        m = _HEADING_INLINE_RE.search(stripped)
        if m and seen < _MAX_ISSUES_PER_CATEGORY:
            issues.append(_issue(
                "text", "warning",
                f"标题内出现行内格式「{m.group(0)[:16]}」——标题应整段走命名样式，"
                "不得内联加粗/斜体/代码", ln, stripped))
            seen += 1


def _check_lists(content: str, issues: list[dict]) -> None:
    """符号标准：列表必须用标准 Markdown 语法，禁止手工输入序号。"""
    seen = 0
    for ln, raw in _iter_prose_lines(content):
        if _MD_LIST_RE.match(raw) or raw.lstrip().startswith("#"):
            continue
        if _MANUAL_LIST_RE.match(raw) and seen < _MAX_ISSUES_PER_CATEGORY:
            issues.append(_issue(
                "symbol", "advisory",
                "列表未使用标准 Markdown 语法（建议 `-` / `1.`），"
                "手工序号在导出侧需二次还原", ln, raw.strip()))
            seen += 1


def _check_tables(content: str, issues: list[dict]) -> None:
    """表格标准：GFM 表格必须「表头行 + 分隔行」，且列数一致。"""
    lines = content.split("\n")
    protected: set[int] = set()
    for blk in iter_all_blocks(content):
        stop = blk["close_line"] if blk["close_line"] >= 0 else len(lines) - 1
        protected.update(range(blk["open_line"], stop + 1))
    seen = 0
    i = 0
    while i < len(lines):
        if i in protected or not _TABLE_ROW_RE.match(lines[i]):
            i += 1
            continue
        head = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not _TABLE_SEP_RE.match(nxt) or "|" not in nxt:
            if seen < _MAX_ISSUES_PER_CATEGORY:
                issues.append(_issue(
                    "table", "error",
                    "表格缺少 GFM 分隔行（`| --- |`），导出会降级为纯文本",
                    i + 1, head.strip()))
                seen += 1
            i += 1
            continue
        head_cols = [c for c in head.strip().strip("|").split("|")]
        sep_cols = [c for c in nxt.strip().strip("|").split("|")]
        if len(head_cols) != len(sep_cols) and seen < _MAX_ISSUES_PER_CATEGORY:
            issues.append(_issue(
                "table", "error",
                f"表格表头 {len(head_cols)} 列与分隔行 {len(sep_cols)} 列不一致，"
                "列会错位", i + 1, head.strip()))
            seen += 1
        i += 2
        # ✅ R130：消费表格的**数据行**（连续的表格行），否则首行数据会被当成
        #    「下一个新表头」，因下一行无分隔行而误报「缺少 GFM 分隔行」
        #    （冒烟实测：合法三行表被误判 error）。数据行列数与表头不一致
        #    同样是列错位，按表头口径报 error（受每类上限约束）。
        while i < len(lines) and i not in protected and _TABLE_ROW_RE.match(lines[i]):
            row_cols = lines[i].strip().strip("|").split("|")
            if len(row_cols) != len(head_cols) and seen < _MAX_ISSUES_PER_CATEGORY:
                issues.append(_issue(
                    "table", "error",
                    f"表格数据行 {len(row_cols)} 列与表头 {len(head_cols)} 列不一致，"
                    "列会错位", i + 1, lines[i].strip()))
                seen += 1
            i += 1


def _check_charts(content: str, issues: list[dict]) -> None:
    """图表标准：围栏语言白名单 + Mermaid 类型白名单 + 未闭合围栏。"""
    seen = 0
    for blk in find_unclosed(content):
        if seen >= _MAX_ISSUES_PER_CATEGORY:
            break
        issues.append(_issue(
            "chart", "error",
            f"代码块未闭合（lang={blk.get('lang') or '?'}），"
            "疑似生成被截断，导出时整块跳过且不占图号",
            int(blk.get("line", 0)), ""))
        seen += 1

    lines = content.split("\n")
    for blk in iter_all_blocks(content):
        if seen >= _MAX_ISSUES_PER_CATEGORY:
            break
        lang = (blk.get("lang") or "").strip().lower()
        if not lang or lang not in CHART_FENCE_LANGS:
            # 非图表围栏（json / text 等）不报——导出按普通代码块处理
            continue
        body_lines = lines[blk["open_line"] + 1: blk["close_line"]] \
            if blk["close_line"] >= 0 else lines[blk["open_line"] + 1:]
        first = ""
        for cand in body_lines:
            cand = cand.strip()
            if cand and not cand.startswith("%%"):
                first = cand
                break
        if lang == "mermaid":
            head = re.split(r"[\s\[{(:]", first)[0] if first else ""
            if head and head not in MERMAID_SOURCE_TYPES:
                issues.append(_issue(
                    "chart", "warning",
                    f"Mermaid 类型「{head}」不在契约白名单"
                    f"（{', '.join(MERMAID_SOURCE_TYPES)}），"
                    "渲染服务可能拒渲染并跳过该图",
                    blk["open_line"] + 1, first[:60]))
                seen += 1
        elif lang == "chart-json" and first and not first.startswith("{"):
            issues.append(_issue(
                "chart", "error",
                "chart-json 围栏内容不是 JSON 对象，导出时跳过不渲染",
                blk["open_line"] + 1, first[:60]))
            seen += 1


def _check_formulas(content: str, issues: list[dict]) -> None:
    """公式标准：LaTeX 定界符必须成对闭合。"""
    seen = 0
    # 剔除块级公式时**保持换行数不变**，否则其后各行的行号会整体前移（line 失真）
    text_wo_display = _DISPLAY_MATH_RE.sub(
        lambda m: "\n" * m.group(0).count("\n"), content)
    protected: set[int] = set()
    for blk in iter_all_blocks(content):
        stop = blk["close_line"] if blk["close_line"] >= 0 else 10 ** 9
        protected.update(range(blk["open_line"], stop + 1))
    for ln, raw in enumerate(text_wo_display.split("\n"), start=1):
        if (ln - 1) in protected:
            continue
        if seen >= _MAX_ISSUES_PER_CATEGORY:
            break
        # 行内 $ 成对：统计非转义、非 $$ 的 $ 个数（块级跨行公式已由
        # _DISPLAY_MATH_RE 剔除，不在此重复报）
        dollars = len(re.findall(r"(?<!\$)\$(?!\$)", raw.replace("\\$", "")))
        if dollars % 2 == 1:
            issues.append(_issue(
                "formula", "error",
                "行内公式定界符 `$` 未成对，导出会按原文残留或降级为图片",
                ln, raw.strip()))
            seen += 1


def _check_encoding(content: str, issues: list[dict]) -> None:
    """符号标准 · 编码：控制字符 / 替换字符 / 西里尔误植 / GBK 乱码。"""
    seen = 0
    for ln, raw in _iter_prose_lines(content):
        if seen >= _MAX_ISSUES_PER_CATEGORY:
            break
        m = _CONTROL_CHARS_RE.search(raw)
        if m:
            issues.append(_issue(
                "symbol", "error",
                f"正文含控制字符 U+{ord(m.group(0)):04X}（导出后表现为乱码/方框）",
                ln, raw.strip()))
            seen += 1
            continue
        if _REPLACEMENT_RE.search(raw):
            issues.append(_issue(
                "symbol", "error", "正文含 U+FFFD 替换字符（编码损坏）",
                ln, raw.strip()))
            seen += 1
            continue
        m = _CYRILLIC_RE.search(raw)
        if m:
            issues.append(_issue(
                "symbol", "error",
                f"正文含西里尔字母「{m.group(0)}」（中文文档误植）",
                ln, raw.strip()))
            seen += 1
            continue
        for tok in _MOJIBAKE_TOKENS:
            if tok in raw:
                issues.append(_issue(
                    "symbol", "error", f"正文含编码乱码片段「{tok}」",
                    ln, raw.strip()))
                seen += 1
                break


def _check_numbers(content: str, issues: list[dict]) -> None:
    """数字标准 · 图题编号格式：`图{章号}-{序号} {图名}`。"""
    seen = 0
    for ln, raw in _iter_prose_lines(content):
        if seen >= _MAX_ISSUES_PER_CATEGORY:
            break
        s = raw.strip()
        if re.match(r"^图\s*\d+", s) and not FIG_TITLE_RE.match(s):
            issues.append(_issue(
                "numbering", "warning",
                "图题不符合 `图{章号}-{序号} {图名}` 格式，导出时会重编号",
                ln, s))
            seen += 1
        elif re.match(r"^表\s*\d+", s) and not TBL_TITLE_RE.match(s):
            issues.append(_issue(
                "numbering", "warning",
                "表题不符合 `表{章号}-{序号} {表名}` 格式，导出时会重编号",
                ln, s))
            seen += 1


def check_format_contract(content: str, *, section_id: str = "",
                          section_title: str = "") -> dict:
    """对一章正文 Markdown 执行三端格式契约自检（纯函数、fail-soft）。

    返回::

        {"checked": True, "passed": bool, "error_count": int,
         "warning_count": int, "advisory_count": int,
         "issues": [{category, severity, message, line, excerpt}],
         "counts": {category: int},
         "section_id": str, "section_title": str}

    - 只产出结论，不改正文、不抛业务异常（自身异常 → `checked=False` 空报告）；
    - category 取值见 :data:`CONTRACT_CATEGORIES`（表格类问题归 `table`，
      图表围栏类归 `chart`，与各渲染消费方的口径一致）。
    """
    std_empty = {
        "checked": False, "passed": True, "error_count": 0,
        "warning_count": 0, "advisory_count": 0, "issues": [],
        "counts": {}, "section_id": section_id,
        "section_title": section_title,
    }
    if not isinstance(content, str) or not content.strip():
        return std_empty
    try:
        issues: list[dict] = []
        _check_headings(content, issues)
        _check_lists(content, issues)
        _check_tables(content, issues)
        _check_charts(content, issues)
        _check_formulas(content, issues)
        _check_encoding(content, issues)
        _check_numbers(content, issues)

        counts: dict[str, int] = {}
        for it in issues:
            counts[it["category"]] = counts.get(it["category"], 0) + 1
        errors = [i for i in issues if i["severity"] == "error"]
        warns = [i for i in issues if i["severity"] == "warning"]
        advisories = [i for i in issues if i["severity"] == "advisory"]
        return {
            "checked": True,
            "passed": not errors,
            "error_count": len(errors),
            "warning_count": len(warns),
            "advisory_count": len(advisories),
            "issues": issues,
            "counts": counts,
            "section_id": section_id,
            "section_title": section_title,
        }
    except Exception:  # pragma: no cover - 自检器自身故障绝不外溢
        return std_empty


def format_contract_summary(report: dict | None) -> str:
    """把自检报告压成一行日志文案（无问题返回空串）。"""
    if not isinstance(report, dict) or not report.get("checked"):
        return ""
    issues = report.get("issues") or []
    if not issues:
        return ""
    parts = [
        f"{i['severity']}/{i['category']}:{i['message']}"
        for i in issues[:3]
    ]
    more = len(issues) - 3
    if more > 0:
        parts.append(f"…等 {len(issues)} 条")
    return "；".join(parts)


__all__ = [
    "FONT_BODY_EAST_ASIA", "FONT_BODY_ALT_EAST_ASIA",
    "FONT_HEADING_EAST_ASIA", "FONT_LATIN",
    "MIN_IMAGE_DPI", "MERMAID_SOURCE_TYPES", "CHART_FENCE_LANGS",
    "FIG_TITLE_RE", "TBL_TITLE_RE", "FIG_TITLE_PREFIX_RE",
    "L1_CN_RE", "L1_ARABIC_RE",
    "CONTRACT_CATEGORIES",
    "check_format_contract", "format_contract_summary",
]
