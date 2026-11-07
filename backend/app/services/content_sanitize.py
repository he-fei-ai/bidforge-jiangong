"""正文内容归一化（2026-11-05 · 生成侧防回归第一道闸）

背景
----
《机电安装施工组织设计_20261104_第1轮》格式问题分析报告暴露的一批成稿缺陷
（字面 ``\\n`` 粘行、PDF 分页标记残留、表题与 GFM 表头粘连成一行、错别字与
单位符号错误）均属于**源文进入块解析器之前**就该消除的杂质 —— 块解析器
``content_blocks._parse_content_blocks`` 按「真实换行」切行，任何在上游被
转义/污染的正文都会以「一整行」的形态绕过所有结构识别，最终原样印进交付
DOCX。

本模块是**解析唯一入口的前置防线**：``_parse_content_blocks`` 的第一步先调
:func:`normalize_raw_content`，导出 / 预览 / 编号规范化三条链路同时受益。

修复项（与报告编号对应）：
  1. 字面 ``\\n``（反斜杠+n 两个字符）→ 真实换行（修复 40 行内容粘连一段）；
  2. ``===== Page N =====`` 分页标记与「第X页共Y页」页脚残留整行删除；
  3. 「表X-Y 表名|列名|列名|…」表题与表头粘连行 → 拆为表题行 + 表格行
     （让既有 GFM 表格分支接管渲染）；
  4. 确定性错别字 / 单位符号映射（峻工→竣工、力矩矩→力矩、0.5MQ→0.5MΩ、
     ``1.5% 0``→1.5‰、``40 °C``→40℃）。

设计约束：
  - 只做**确定性、可逐条解释**的替换；不做模糊的英文残留翻译（无法保证
    不误伤术语，报告项 devices/fabrication 需人工/AI 校对环节处理）；
  - 幂等：归一化结果再次归一化零改动；
  - 统计随返回值上抛，调用方记日志（不静默）。
"""

from __future__ import annotations

import logging
import re

from app.services.fence_common import iter_all_blocks

logger = logging.getLogger("content_sanitize")

# ---------------------------------------------------------------------------
# 1) 字面 \n → 真实换行（AI 输出被 JSON 序列化或前端粘贴时常见）
#    仅匹配「反斜杠 + n」两个字符；不触碰 ``\\n``（转义反斜杠后跟 n，
#    属于正则/LaTeX 内容语义）。为保守起见，只在「反斜杠+n 两侧无其他
#    反斜杠」时替换，避免破坏 ``\\n`` 这类双反斜杠序列。
# ---------------------------------------------------------------------------
_RE_LITERAL_NEWLINE = re.compile(r"(?<!\\)\\n(?!\\)")
_RE_CR = re.compile(r"\r\n?")

# ---------------------------------------------------------------------------
# 2) PDF / OCR 转换残留：分页标记与页脚页码（仅整行匹配时删除）
# ---------------------------------------------------------------------------
_RE_PAGE_MARK_LINE = re.compile(r"^\s*=+\s*Page\s*\d+\s*=+\s*$", re.I)
_RE_FOOTER_PAGE_LINE = re.compile(r"^\s*第\s*\d+\s*页\s*共\s*\d+\s*页\s*$")

# ---------------------------------------------------------------------------
# 3) 表题与 GFM 表头粘连：「表4-1 智能化系统联调|测试系统|测试项目|…」
#    拆分条件（保守，防止误伤含竖线的普通正文）：
#      · 行首是「表X-Y/表一」类表题前缀；
#      · 行内竖线 ≥ 2 个（单独一个竖线更像行文符号）。
# ---------------------------------------------------------------------------
_RE_CAPTION_WITH_PIPES = re.compile(r"^(\s*表\s*[0-9一二三四五六七八九十][0-9.\-–—]*\s*[^|]*?)\s*\|")

# ---------------------------------------------------------------------------
# 4) 确定性错别字 / 单位符号映射（来源：第1轮格式问题分析报告 §11）
#    每条均为「坏写法 → 唯一正确写法」，替换前先整词/上下文锚定。
# ---------------------------------------------------------------------------
_TYPO_RULES: tuple[tuple[re.Pattern, str], ...] = (
    # 峻工 → 竣工（报告 Page89/98）
    (re.compile(r"峻工"), "竣工"),
    # 力矩矩 / 扭矩矩 → 力矩 / 扭矩（报告 Page145「力矩矩」重复）
    (re.compile(r"([扭力])矩矩"), r"\1矩"),
    # 0.5MQ → 0.5MΩ（数字后缀 MQ 是 MΩ 的 OCR 误写；要求 MQ 前是数字，
    # 避免误伤以 MQ 结尾的编号/型号）
    (re.compile(r"(?<=[0-9])\s*MQ(?![0-9A-Za-z])"), "MΩ"),
    # 1.5% 0 → 1.5‰（‰ 的 OCR 拆字；仅当 % 后紧跟孤立的 0/０ 且其后不是
    # 数字或字母时才替换，正常百分数如 "50%"、"0.85" 不受影响）
    (re.compile(r"(\d(?:\.\d+)?)\s*%\s*[0０](?![0-9A-Za-z.%])"), r"\1‰"),
    # 40 °C / 40°C → 40℃（数字与 ℃ 之间不留空格的统一写法）
    (re.compile(r"(?<=[0-9])\s*°\s*C\b"), "℃"),
)

#: normalize_raw_content 返回的统计键（防回归测试锁定）。
SANITIZE_STATS_KEYS = (
    "literal_newline",
    "page_marks",
    "caption_pipe_split",
    "typo_fixed",
)


def _fence_line_mask(text: str) -> list[bool]:
    """按行标记「该行处于代码围栏内」（含开/闭围栏行、未闭合块之后的全部行）。

    为什么归一化必须认得围栏：围栏内是**代码/图表语法**，不是散文。
      · chart-json 的字符串值里合法存在 ``\\n`` 转义（JSON 规范），若被当成
        「字面换行」拆开，JSON 立即出现非法控制字符 → 解析器整块跳过 →
        成稿静默缺图（且日志只有一行 INFO）；
      · mermaid / 代码块里的 ``===== Page N =====``、``峻工``、``0.5MQ``
        是示例文本或标识，按散文规则删除/改写等于破坏图表数据。
    未闭合围栏按「其后全部行都在块内」处理 —— 与解析器 / 导出侧同口径。
    """
    lines = text.split("\n")
    mask = [False] * len(lines)
    for b in iter_all_blocks(text):
        end = b["close_line"] if b["close_line"] >= 0 else len(lines) - 1
        for i in range(b["open_line"], min(end, len(lines) - 1) + 1):
            mask[i] = True
    return mask


def _closed_fence_char_ranges(text: str) -> list[tuple[int, int]]:
    """**闭合**围栏的字符区间（``[start, end)``，行首到闭合行末）。

    只保护闭合围栏，是「整份正文被 JSON 转义」场景的必要条件：那种正文里
    所有换行都是字面 ``\\n``，围栏标记本身也粘成一行，扫描器只能判为
    「未闭合」。若把未闭合区间也纳入保护，字面换行永远无法还原，整篇正文
    会退化成一个代码块。闭合围栏说明换行结构本身是真的，块内转义才有
    「属于语法而非排版」的语义。
    """
    lines = text.split("\n")
    offsets: list[int] = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    ranges: list[tuple[int, int]] = []
    for b in iter_all_blocks(text):
        if b["close_line"] < 0:
            continue
        end_line = b["close_line"]
        ranges.append((offsets[b["open_line"]], offsets[end_line] + len(lines[end_line])))
    return ranges


def _looks_like_pathish(text: str, backslash_pos: int, floor: int = 0) -> bool:
    """判断 ``backslash_pos`` 处的 ``\\n`` 是否位于路径/转义片段中。

    背景：字面 ``\\n`` 既可能是「被 JSON 转义的真换行」，也可能是 Windows
    路径（``D:\\资料\\newfile.doc`` / ``\\\\server\\newshare``）的一部分。
    后者拆开会把路径劈成两段（实测 ``C:\\new`` → ``C:`` + 换行 + ``ew``）。
    判据只看 ``\\n`` 所在 token（向前到空白/全角标点为止）：
      · 含盘符 ``[A-Za-z]:`` → 路径；
      · 已含反斜杠 → 路径/转义片段。
    两个判据都不命中时按真换行还原（中文正文的转义换行至少一侧是中文，
    不会被误判成路径）。
    """
    start = backslash_pos
    j = start - 1
    # floor = 上一个字面 \n 的结束位置：token 不得跨越已识别的转义换行，
    # 否则其反斜杠会被误当作路径分隔符（``A-->B\n``` `` 的 token 会变成
    # ``\nA-->B`` → 误判为路径，最后一段换行永远无法还原）。
    limit = max(floor, start - 60)
    while j >= limit:
        ch = text[j]
        if ch.isspace() or ch in "，。；：、（）()\"'“”":
            break
        j -= 1
    token = text[j + 1 : start]
    if not token:
        return False
    if "\\" in token:
        return True
    return bool(re.search(r"[A-Za-z]:", token))


def _unroll_literal_newlines(segment: str, *, ascii_guard: bool = True) -> tuple[str, int]:
    """把片段里的字面 ``\\n`` 还原为真实换行，返回 ``(新片段, 替换数)``。

    两道**防误伤**：
      · ``\\n`` 所在 token 像 Windows 路径（盘符 / 已有反斜杠）→ 永不还原；
      · ``\\n`` 两侧都是 ASCII 字符（``Hello\\nWorld``、``s/\\n//``）→ 不还原
        （``ascii_guard=False`` 的整篇转义模式除外：那时全文都是字面换行，
        代码/英文段落也必须还原，否则粘成一行）。
    中文正文里被 JSON 转义的真换行至少有一侧是中文/全角字符，仍会正常还原。
    """
    if "\\n" not in segment:
        return segment, 0
    out: list[str] = []
    cursor = 0
    count = 0
    floor = 0
    for m in _RE_LITERAL_NEWLINE.finditer(segment):
        prev = segment[m.start() - 1] if m.start() else ""
        nxt = segment[m.end() : m.end() + 1]
        both_ascii = bool(prev and prev.isascii() and nxt and nxt.isascii())
        pathish = _looks_like_pathish(segment, m.start(), floor)
        floor = m.end()
        if pathish or (ascii_guard and both_ascii):
            continue
        out.append(segment[cursor : m.start()])
        out.append("\n")
        cursor = m.end()
        count += 1
    out.append(segment[cursor:])
    return "".join(out), count


def _transform_outside_ranges(
    text: str,
    ranges: list[tuple[int, int]],
    fn,
) -> tuple[str, int]:
    """仅对围栏区间之外的片段执行 ``fn``，区间内逐字保留。"""
    if not ranges:
        return fn(text)
    out: list[str] = []
    cursor = 0
    total = 0
    for start, end in ranges:
        if start > cursor:
            seg, n = fn(text[cursor:start])
            total += n
            out.append(seg)
        out.append(text[start:end])
        cursor = end
    if cursor < len(text):
        seg, n = fn(text[cursor:])
        total += n
        out.append(seg)
    return "".join(out), total


def _apply_typo_rules(line: str, stats: dict[str, int]) -> str:
    """对单行应用确定性错别字 / 单位符号规则（就地累计统计）。"""
    for rx, repl in _TYPO_RULES:
        line, n_sub = rx.subn(repl, line)
        if n_sub:
            stats["typo_fixed"] += n_sub
    return line


def normalize_raw_content(content: str) -> tuple[str, dict[str, int]]:
    """解析前置归一化：返回 (归一化后的正文, 修复统计)。

    处理顺序不可颠倒：先还原字面 ``\\n`` 得到真实行结构，才能按行删除
    分页标记 / 拆分表题粘连行。

    幂等：对已归一化的正文再次调用，统计全 0、内容逐字不变。
    """
    stats = {k: 0 for k in SANITIZE_STATS_KEYS}
    if not content:
        return (content or ""), stats

    text = str(content)

    # 1) 字面 \n → 真实换行；\r\n / \r → \n。
    #    闭合围栏内不还原（chart-json 的 \n 是 JSON 转义，拆开即语法损坏）。
    #    但整篇被 JSON 转义时（几乎全是字面 \n，真实换行 ≤ 1），围栏标记也
    #    粘成一行、扫描器无法识别围栏 —— 此时必须全文还原，否则整篇正文
    #    会退化成一个代码块。该场景下图表 JSON 自身的 \n 已被双重转义为
    #    ``\\n``，本正则（前后不允许再出现反斜杠）不会误伤。
    _real_nl = text.count("\n")
    _literal_nl = len(_RE_LITERAL_NEWLINE.findall(text))
    if _real_nl <= 1 and _literal_nl >= 2:
        text, n_sub = _unroll_literal_newlines(text, ascii_guard=False)
    else:
        text, n_sub = _transform_outside_ranges(
            text, _closed_fence_char_ranges(text), _unroll_literal_newlines
        )
    stats["literal_newline"] += n_sub
    text = _RE_CR.sub("\n", text)

    # 2)/3) 逐行处理：删分页标记 / 页脚残留行；拆表题粘连行。
    #    围栏内的行原样保留（代码/图表内容不是排版杂质）。
    line_mask = _fence_line_mask(text)
    out_lines: list[tuple[str, bool]] = []
    for line, protected in zip(text.split("\n"), line_mask):
        if protected:
            out_lines.append((line, True))
            continue
        if _RE_PAGE_MARK_LINE.match(line):
            stats["page_marks"] += 1
            continue
        if _RE_FOOTER_PAGE_LINE.match(line):
            stats["page_marks"] += 1
            continue
        m = _RE_CAPTION_WITH_PIPES.match(line)
        if m and line.count("|") >= 2:
            caption = m.group(1).strip()
            rest = line[line.index("|") :].strip()
            if caption and rest:
                out_lines.append((caption, False))
                out_lines.append((rest, False))
                stats["caption_pipe_split"] += 1
                continue
        out_lines.append((line, False))

    # 4) 确定性错别字 / 单位符号（仅围栏外；逐行等价于全文替换，
    #    规则均不含跨行断言）
    text = "\n".join(
        line if protected else _apply_typo_rules(line, stats) for line, protected in out_lines
    )

    if any(stats.values()):
        logger.warning(
            "内容归一化：字面\\n=%d · 分页/页脚残留=%d · 表题粘连拆分=%d · 错别字/单位=%d",
            stats["literal_newline"],
            stats["page_marks"],
            stats["caption_pipe_split"],
            stats["typo_fixed"],
        )
    return text, stats
