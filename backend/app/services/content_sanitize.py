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

    # 1) 字面 \n → 真实换行；\r\n / \r → \n
    text, n_sub = _RE_LITERAL_NEWLINE.subn("\n", text)
    stats["literal_newline"] += n_sub
    text = _RE_CR.sub("\n", text)

    # 2)/3) 逐行处理：删分页标记 / 页脚残留行；拆表题粘连行
    out_lines: list[str] = []
    for line in text.split("\n"):
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
                out_lines.append(caption)
                out_lines.append(rest)
                stats["caption_pipe_split"] += 1
                continue
        out_lines.append(line)
    text = "\n".join(out_lines)

    # 4) 确定性错别字 / 单位符号
    for rx, repl in _TYPO_RULES:
        text, n_sub = rx.subn(repl, text)
        if n_sub:
            stats["typo_fixed"] += n_sub

    if any(stats.values()):
        logger.warning(
            "内容归一化：字面\\n=%d · 分页/页脚残留=%d · 表题粘连拆分=%d · 错别字/单位=%d",
            stats["literal_newline"],
            stats["page_marks"],
            stats["caption_pipe_split"],
            stats["typo_fixed"],
        )
    return text, stats
