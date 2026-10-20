# -*- coding: utf-8 -*-
"""DOCX 导出前的公式与乱码修复工具。

解决 AI 生成内容导出 DOCX 时的三类问题：
1. LaTeX 公式残留（``$...$`` / ``$$...$$`` / ``\\(...\\)`` / ``\\[...\\]``）
   —— 转换为 Word 原生公式（OMML），不再以反斜杠源码形式出现在文档中；
2. 常见乱码符号（U+FFFD、控制字符、GBK mojibake "锟斤拷"、Latin-1 mojibake、西里尔误植）
   —— 高置信模式修复；
3. 常见科学/数学公式（分式、上下标、根号、求和/连乘/积分、希腊字母、比较与运算符号）
   —— 由纯 Python LaTeX 解析器转为 OMML，零外部依赖（不依赖 pandoc）。

OMML 节点结构与 pandoc 输出一致，Word / WPS 均可原生渲染。
"""

from __future__ import annotations

import html
import re
from typing import List, Tuple

# ---------------------------------------------------------------------------
# OMML 命名空间
# ---------------------------------------------------------------------------
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# ---------------------------------------------------------------------------
# 乱码清理（高置信模式，避免误伤正常文本）
# ---------------------------------------------------------------------------

# 控制字符：除 \t \n \r 外的 C0 控制符
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# U+FFFD 替换字符（UTF-8 解码失败的产物）
_REPLACEMENT_RE = re.compile(r"\ufffd+")
# GBK 乱码循环 "锟斤拷"（0xEF 0xBF 0xBD 被按 GBK 读取的经典结果）
_MOJIBUKE_GBK_RE = re.compile(r"锟斤拷+|锟斤拷(?:锟斤拷)*")
# Latin-1 / Windows-1252 mojibake（UTF-8 字节被按 Latin-1 读取）
_LATIN1_MOJIBUKE = [
    ("Ã©", "é"), ("Ã¨", "è"), ("Ãª", "ê"), ("Ã«", "ë"), ("Ã¢", "â"),
    ("Ã¤", "ä"), ("Ã¯", "ï"), ("Ã®", "î"), ("Ã´", "ô"), ("Ã¶", "ö"),
    ("Ã¹", "ù"), ("Ã¼", "ü"), ("Ã»", "û"), ("Ã§", "ç"), ("Ã³", "ó"),
    ("Ã¡", "á"), ("Ã\xa0", "à"), ("Ã´", "ô"),
    ("â€™", "’"), ("â€œ", "“"), ("â€\x9d", "”"), ("â€“", "–"),
    ("â€”", "—"), ("â€¦", "…"), ("Â\xa0", " "), ("Â°", "°"),
    # 注：移除裸 ("Â", "") —— 合法文本中的 Â（法语词/专名）会被静默删字，
    # 违背"宁可不改，不丢内容"原则。常见 mojibake Â°（度数符号）保留修复。
]
_LATIN1_RE = re.compile("|".join(re.escape(a) for a, _ in _LATIN1_MOJIBUKE))

# 西里尔误植：AI 生成中文时混入的俄语高频词（带中文修复建议）。
# 命中替换；未命中的西里尔片段保留原样（宁可不改，不丢内容）。
# 注：移除 "поверх" 映射 —— 正文中合法的俄语单词（引用俄文标准/文献）会被
# 静默改写为中文，属内容篡改。
_CYRILLIC_WORD = {
    "а": "а",  # 单字母不做替换（见 _CYRILLIC_SEG_RE 的 min_len 控制）
}
# 连续西里尔字母片段（>=2 个字符），最多 20 字符，避免吞掉长俄语句子
_CYRILLIC_SEG_RE = re.compile(r"[А-Яа-яЁё]{2,20}")
# ✅ R104（2026-10-18 · 导出乱码收口）：**单字母**西里尔→希腊形近字误植。
# 生产取证（scheme_assistant.db sections.content 全量扫描）：西里尔字符仅
# ``ф``(U+0444)×6，全部是「单字母 + 数字」形态（``ф48.3mm×3.6mm`` / ``ф14``
# 对拉螺栓）—— AI 把 GB 工程文档里的直径符号 **φ（希腊 Phi, U+03C6）** 误植为
# 字形几乎一致的西里尔 ф。交付文档里它与 φ 形近但不同码：按 GB 口径是错字，
# 且 preflight「钢管壁厚」判据（只认 ``[φΦ]``）与事实抽取都会对不上号。
# 既有 ``_CYRILLIC_SEG_RE`` 只覆盖 **≥2 连续字母的词段**，单字母符号误植
# 结构性漏修（词表 ``_CYRILLIC_WORD`` 对此亦无映射）。
# 判据（高置信，封闭映射，宁可不改）：
#   · 仅映射封闭字母 ``ф/Ф → φ/Φ``（生产实证的唯一形态；不扩大到 а/в/с 等
#     高频俄语字母——它们在俄语文本里合法，单字母+数字的工程符号上下文才可疑）；
#   · **后随 ASCII 数字**（工程符号典型上下文；俄语单词/文本里的单字母不会
#     后随裸数字，如引文「ГОСТ 5」是数字在前）；
#   · **前不能是西里尔字母**（排除俄语词内字母，如 ``версия5`` 的 в 前有 с）；
#   · 未命中形态一律保留原样。
_CYRILLIC_GLYPH_MAP = {"ф": "φ", "Ф": "Φ"}
_CYRILLIC_GLYPH_NUM_RE = re.compile(r"(?<![А-Яа-яЁё])([фФ])(?=\s*\d)")

# ---------------------------------------------------------------------------
# 表格残留清理：Excel/Word 剪贴板表格标记 + 粘贴带出的 HTML 表格标签
# ---------------------------------------------------------------------------
# 从 Excel/Word 复制表格粘贴进正文（上传解析或 AI 转写）时，会带入一批
# 剪贴板占位标记：<fcel> 填充单元格 / <lcel> 列表单元格 / <ucel> 汇总单元格 /
# <xc> 交叉单元格 / <nl> 换行。它们**不携带任何正文信息**，旧实现原样印进
# 交付文档即成「<br><br><table><fcel><fcel><nl></table>」式乱码（实测第 88 页）。
# 处理原则：占位标记直接丢弃、<nl>/<br> 还原为换行、单元格/行结束符还原为
# 分隔，其余表格标签丢弃 —— 让「标签汤」降级为可读纯文本，而非乱码。
# 注：仅处理审计侧已判定为非法的标签集合（table/thead/tbody/tr/td/th/div/
#     span/p/br 等），不触碰正文中合法的 < 、>（如「a < b」），零误伤。
_CLIPBOARD_CELL_RE = re.compile(r"</?(?:fcel|lcel|ucel|xc)\s*/?>", re.I)
_CLIPBOARD_NL_RE = re.compile(r"<nl\s*/?>", re.I)
_HTML_BR_RE = re.compile(r"<br\s*/?>", re.I)
_HTML_CELL_END_RE = re.compile(r"</t[dh]\s*>", re.I)
_HTML_ROW_END_RE = re.compile(r"</tr\s*>", re.I)
# ✅ BUG 修复（2026-09-27）：属性段原为 ``[^>]*``，允许「标签名」与「>」之间夹任意文本，
#    于是正文里合法的比较式被当成标签整段删除（实测「当 T<P 且 Q>R 时」→「当 TR 时」，
#    静默丢正文，违反 AGENTS.md §3.1.6 数据真实性红线）。现按真实 HTML 属性语法收紧：
#    属性名必须是 ASCII 标识符、属性值必须是引号串或无空白串，中文/空格内容不再匹配；
#    同时保留「仅处理真实标签」的语义（``<p class="x">`` 仍被清理）。
_HTML_ATTR = (
    r"(?:\s+[a-zA-Z_:][-a-zA-Z0-9_:.]*"
    r"(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'>]+))?)*\s*"
)
_HTML_TABLE_TAG_RE = re.compile(
    r"</?(?:table|thead|tbody|tfoot|tr|td|th|col|colgroup|caption|div|span|p|font)"
    r"\b" + _HTML_ATTR + r">",
    re.I,
)


def strip_table_markup(text: str) -> str:
    """把剪贴板表格标记 / 残留 HTML 表格标签降级为可读纯文本。

    - ``<nl>`` / ``<br>`` → 换行；``</td>`` / ``</th>`` → 空格（单元格分隔）；
      ``</tr>`` → 换行；``<fcel>`` 等占位标记与其余表格标签 → 丢弃。
    - 不删除单元格内正文，只去除包裹它的标记，故「宁可不改，不丢内容」。

    ✅ BUG 修复（2026-09-27）：属性段由 ``[^>]*`` 收紧为真实 HTML 属性语法后，
    正文里的合法比较式不再被当成标签整段删除（实测「当 T<P 且 Q>R 时设计」曾被删成
    「当 TR 时设计」，**静默丢正文**且用户与导出日志均无提示，违反 AGENTS.md §3.1.6）。
    误伤面已收敛为「只有形态确为标签的尖括号才会被删」：``<p class="x">``、``</span>``
    照常清理，而 ``T<P``、``a<b 且 c>d``、``f(x) < 3`` 逐字节保留。
    """
    if not text or ("<" not in text):
        return text
    out = _CLIPBOARD_NL_RE.sub("\n", text)
    out = _HTML_BR_RE.sub("\n", out)
    out = _HTML_ROW_END_RE.sub("\n", out)
    out = _HTML_CELL_END_RE.sub(" ", out)
    out = _CLIPBOARD_CELL_RE.sub("", out)
    out = _HTML_TABLE_TAG_RE.sub("", out)
    # 多个连续换行（标记堆叠产生）压成单个，避免整段空行
    out = re.sub(r"\n{2,}", "\n", out)
    return out


def clean_text(text: str) -> str:
    """清理文本中的乱码字符（高置信修复）。

    规则（保守，避免误伤正常内容）：
    - 删除控制字符；
    - U+FFFD 替换字符 -> 空格（避免粘连）；
    - GBK 乱码循环 "锟斤拷" -> 删除；
    - Latin-1 mojibake 高置信模式 -> 还原；
    - 中文/普通文档中的西里尔误植词 -> 映射替换（命中才替换）。
    """
    if not text:
        return text
    out = _CTRL_RE.sub("", text)
    out = _REPLACEMENT_RE.sub(" ", out)
    out = _MOJIBUKE_GBK_RE.sub("", out)
    out = _LATIN1_RE.sub(lambda m: dict(_LATIN1_MOJIBUKE)[m.group(0)], out)

    def _fix_cyrillic(m: re.Match) -> str:
        seg = m.group(0)
        return _CYRILLIC_WORD.get(seg.lower(), seg)

    out = _CYRILLIC_SEG_RE.sub(_fix_cyrillic, out)
    # ✅ R104（2026-10-18 · 导出乱码收口）：单字母西里尔形近字（ф→φ 等，
    #    仅「后随数字」的高置信工程符号形态，见 _CYRILLIC_GLYPH_NUM_RE 注释）。
    #    既有词段修复覆盖不到单字母符号误植，生产实测 6 处全部漏修进交付文档。
    out = _CYRILLIC_GLYPH_NUM_RE.sub(
        lambda m: _CYRILLIC_GLYPH_MAP[m.group(1)], out)
    # ✅ 新增：剪贴板表格标记 / 残留 HTML 表格标签降级为可读纯文本
    #    （修复交付文档出现「<br><br><table><fcel><nl></table>」式乱码）
    out = strip_table_markup(out)
    # ✅ 2026-10-17（文档质量报告 符号问题）：HTML 实体还原（`&gt;`→`>` / `&lt;`→`<`）
    out = decode_html_entities(out)
    # ✅ 2026-10-17（文档质量报告 格式问题5）：Markdown 转义残留还原
    #    （字面 ``\n`` 黏行 / ``\*`` ``\_`` ``\~`` 裸反斜杠，高置信才动）
    out = unescape_markdown_literals(out)
    # ✅ 2026-10-17（文档质量报告 英文残留）：中英混排误植词还原（公式跨度外）
    out = fix_english_residue(out)
    # ✅ 2026-10-17（文档质量报告 公式乱码）：带边界空白的 `$ x $` 归一为 `$x$`，
    #    使其落回严格形态被 OMML 转换器消费（否则 LaTeX 源码原样印进成稿）
    out = normalize_spaced_inline_math(out)
    return out


# ---------------------------------------------------------------------------
# Markdown 转义残留还原（2026-10-10 · 文档质量报告 格式问题5）
# ---------------------------------------------------------------------------
# AI 输出 / 上传解析中，Markdown 源码的转义序列会以字面形态漏进成稿：
#   · ``\n`` / ``\t`` —— 源码换行/制表符未还原，正文印出字面反斜杠并黏行；
#   · ``\*`` ``\_`` ``\~`` ``\` `` —— Markdown 转义的星号/下划线/波浪线/反引号，
#     成稿印出裸反斜杠。
# 处理原则（**高置信才动**，宁可不改，不丢内容 · AGENTS.md §3.1.6）：
#   1. ``\n``/``\t`` 前面不得是字母/数字/反斜杠、后面不得是字母 ——
#      避免误伤 LaTeX 命令（``\nabla`` / ``\top``）、Windows 路径（``C:\new``）
#      与已转义反斜杠（``\\n``）；
#   2. ``\*`` 等标点转义只剥反斜杠保留原字符（Markdown 转义语义）；
#   3. ``$...$`` / ``$$...$$`` / ``\(...\)`` / ``\[...\]`` 公式跨度**整段跳过**
#      —— 内部反斜杠交给公式转换器处理，这里绝不触碰。
_MD_UNESCAPE_NL_TAB_RE = re.compile(
    r"(?<![A-Za-z0-9\\])\\(?P<ch>[nt])(?![A-Za-z])")
_MD_UNESCAPE_PUNCT_RE = re.compile(r"(?<![A-Za-z0-9\\])\\([*_~`])")
# 公式跨度（外层捕获组供 split 使用：偶数段=普通文本，奇数段=公式原文）
_FORMULA_SPAN_RE = re.compile(
    r"(\$\$.*?\$\$|\$[^$\n]*?\$|\\\(.+?\\\)|\\\[.+?\\\])", re.S)


def unescape_markdown_literals(text: str) -> str:
    """还原 Markdown 源码转义残留（高置信修复，公式跨度内逐字不动）。

    ✅ 2026-10-10（文档质量报告 格式问题5）：AI 生成的正文里常见字面
    ``\\n``（黏行）/ ``\\*`` ``\\_``（裸反斜杠）残留 —— 它们是 Markdown 源码
    的转义序列，导出成稿时原样印出即成乱码。仅当形态高度可信（前后无字母
    数字、不在公式跨度内）才还原；正常文本中的反斜杠（LaTeX 源码、Windows
    路径、已转义反斜杠）一律保留。
    """
    if not text or "\\" not in text:
        return text

    def _fix_seg(seg: str) -> str:
        seg = _MD_UNESCAPE_NL_TAB_RE.sub(
            lambda m: "\n" if m.group("ch") == "n" else "\t", seg)
        seg = _MD_UNESCAPE_PUNCT_RE.sub(r"\1", seg)
        return seg

    parts = _FORMULA_SPAN_RE.split(text)
    if len(parts) == 1:
        return _fix_seg(text)
    # 偶数索引是公式跨度外的普通文本（可修复），奇数索引是公式原文（保留）
    return "".join(p if i % 2 else _fix_seg(p) for i, p in enumerate(parts))


# ---------------------------------------------------------------------------
# HTML 实体还原（2026-10-17 · 「层高垂直度（&gt;5m）」类实体残留）
# ---------------------------------------------------------------------------
# 实测第 2 轮交付文档：正文与表格单元格共 45 处 `<w:t>` 文本内出现**字面** `&gt;`
# / `&lt;`（如「当跨度&gt;2m 时」「层高垂直度（&gt;5m）」「&gt;2，≤8」），应为
# `>` / `<`。根因：AI 生成 / 上传解析把 HTML 实体原样写进正文，而 `clean_text`
# 此前**不解码实体** —— 实体只在 HTML 表格单元格（content_blocks._html_cell_text）
# 侧被还原，GFM 表格与正文段落的实体一路漏进成稿。
# 现按**白名单**收口：仅还原常见且无歧义的实体 + 数字引用（`&#NN;` / `&#xNN;`），
# 最多迭代 2 轮以兼容 `&amp;gt;` 双重转义。白名单外的 `&xxx;` 一律保留
# （宁可不改，不丢内容）。
_HTML_ENTITY_MAP = {
    "&gt;": ">", "&lt;": "<", "&amp;": "&",
    "&quot;": '"', "&apos;": "'", "&nbsp;": " ",
}
_HTML_ENTITY_RE = re.compile(
    r"&(?:gt|lt|amp|quot|apos|nbsp|#\d{1,6}|#[xX][0-9a-fA-F]{1,6});", re.I)


def decode_html_entities(text: str) -> str:
    """还原白名单内的 HTML 实体（最多 2 轮，兼容 `&amp;gt;` 双重转义）。"""
    if not text or "&" not in text:
        return text
    for _ in range(2):
        def _rep(m: "re.Match") -> str:
            ent = m.group(0)
            low = ent.lower()
            if low in _HTML_ENTITY_MAP:
                return _HTML_ENTITY_MAP[low]
            try:
                if low.startswith("&#x"):
                    return chr(int(ent[3:-1], 16))
                if low.startswith("&#"):
                    return chr(int(ent[2:-1]))
            except (ValueError, OverflowError):
                return ent
            return ent

        new = _HTML_ENTITY_RE.sub(_rep, text)
        if new == text:
            break
        text = new
    return text


# ---------------------------------------------------------------------------
# 行内公式「边界空白」归一（2026-10-17 · `$ \gamma_c = 25 $` 类 LaTeX 残留）
# ---------------------------------------------------------------------------
# 实测第 2 轮交付文档：`$ \gamma_c = 25 $`、`$ t_0 $`、`$ k $`、`$ \mu $`、
# `$ \varphi $` 等**带边界空格**的 `$ ... $` 跨度未被转成 OMML，LaTeX 源码
# 原样印进成稿。根因：`_INLINE_DOLLAR` 要求开/闭 `$` **紧贴非空白**
# （`(?!\s)` / `(?<!\s)`），而 AI 常写成 `$ x $`（两端各留空格）→ 不匹配 → 公式
# 不转换。现做「边界空白归一」：仅当跨度内容**形似数学式**（含反斜杠命令 / 上下标
# / 运算符，或为短变量名）才把两端空白剥掉，使其落回严格 `$x$` 形态被转换器消费；
# 纯数字（`$ 100 $` 之类货币/数值）与普通文本一律不动。**只剥边界空白，不改内文**。
_MATH_WS_DOLLAR_RE = re.compile(r"(?<!\$)\$[ \t]+([^$\n]*?\S)[ \t]+\$(?!\$)")
_MATH_HINT_RE = re.compile(r"\\[A-Za-z]+|[_^=<>+\-*/×÷±≤≥∑∫√∅∈∀∃]")
_MATH_VAR_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,7}")
_PURE_NUMBER_RE = re.compile(r"[\d.,]+")


def _spaced_inline_math_inners(text: str) -> list[str]:
    """返回文本中「形似数学式」的带边界空白 `$ ... $` 跨度内文（供统计）。"""
    if not text or "$" not in text:
        return []
    out: list[str] = []
    for m in _MATH_WS_DOLLAR_RE.finditer(text):
        inner = m.group(1).strip()
        if inner and not _PURE_NUMBER_RE.fullmatch(inner) and (
                _MATH_HINT_RE.search(inner) or _MATH_VAR_RE.fullmatch(inner)):
            out.append(inner)
    return out


def normalize_spaced_inline_math(text: str) -> str:
    """把形似数学式的 `$ x $` 归一为 `$x$`（仅剥两端空白，不动内文）。"""
    if not text or "$" not in text:
        return text

    def _rep(m: "re.Match") -> str:
        inner = m.group(1).strip()
        if inner and not _PURE_NUMBER_RE.fullmatch(inner) and (
                _MATH_HINT_RE.search(inner) or _MATH_VAR_RE.fullmatch(inner)):
            return "$" + inner + "$"
        return m.group(0)

    return _MATH_WS_DOLLAR_RE.sub(_rep, text)


# ---------------------------------------------------------------------------
# 中英混排残留词语还原（2026-10-17 · required/assemble/steel tube 类英文误植）
# ---------------------------------------------------------------------------
# 实测第 2 轮交付文档（21 处）：AI 生成时把英文单词/短语原样留在中文句中
# （「高处作业required搭设」「先 assemble 四片模板」「加steel tube 龙骨体系」…）。
# 语义明确的词按**词边界整体**替换为中文；可能是专名/乱码（如 trofface）
# 的**不收录**，避免乱猜改写（宁可不改，不丢内容）。
# 仅作用于**公式跨度之外**的普通文本（避免误伤 `$N_{slab}$` 之类公式内变量）。
_EN_RESIDUE_MAP = {
    "required": "需",
    "assemble": "拼装",
    "steel tube": "钢管",
    "renovate": "改造",
    "nearest": "最近的",
    "filler": "填充料",
    "cutting": "切割",
    "footing": "底座",
    "verticality": "垂直度",
    "plywood": "胶合板",
}
_EN_RESIDUE_RE = re.compile(
    r"(?<![A-Za-z])("
    + "|".join(re.escape(k) for k in sorted(_EN_RESIDUE_MAP, key=len, reverse=True))
    + r")(?![A-Za-z])", re.I)


def fix_english_residue(text: str) -> str:
    """把公式跨度外的英文误植词还原为中文（词边界整体替换）。"""
    if not text or not _EN_RESIDUE_RE.search(text):
        return text
    parts = _FORMULA_SPAN_RE.split(text)
    return "".join(
        p if i % 2 else _EN_RESIDUE_RE.sub(
            lambda m: _EN_RESIDUE_MAP[m.group(1).lower()], p)
        for i, p in enumerate(parts))


# ---------------------------------------------------------------------------
# LaTeX -> OMML 转换
# ---------------------------------------------------------------------------

_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ϵ", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ",
    "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "ϕ",
    "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ",
    "Omega": "Ω",
}

# 命令 -> Unicode 运算符（作为普通样式 run，与 pandoc 一致）
_OPS = {
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠",
    "times": "×", "div": "÷", "cdot": "⋅", "pm": "±", "mp": "∓",
    "infty": "∞", "approx": "≈", "equiv": "≡", "propto": "∝",
    "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "Leftarrow": "⇐",
    "to": "→", "gets": "←", "leftrightarrow": "↔",
    "partial": "∂", "nabla": "∇", "forall": "∀", "exists": "∃",
    "in": "∈", "notin": "∉", "subset": "⊂", "supset": "⊃",
    "subseteq": "⊆", "supseteq": "⊇", "cup": "∪", "cap": "∩",
    "emptyset": "∅", "angle": "∠", "perp": "⊥", "parallel": "∥",
    "prime": "′", "ldots": "…", "cdots": "⋯", "dots": "…",
    "degree": "°", "circ": "°",
}

# 大运算符（n-ary）
_NARY = {"sum": "∑", "prod": "∏", "int": "∫", "oint": "∮", "bigcup": "⋃", "bigcap": "⋂"}

# 需要成对处理的定界符（命令 -> Unicode）
_LEFT_DELIM = {
    "(": "(", "[": "[", "{": "{", "|": "|", "langle": "⟨",
    "lfloor": "⌊", "lceil": "⌈",
}
_RIGHT_DELIM = {
    ")": ")", "]": "]", "}": "}", "|": "|", "rangle": "⟩",
    "rfloor": "⌋", "rceil": "⌉",
}

_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<cmd>\\[a-zA-Z]+)"
    r"|(?P<brace>\{|\})"
    r"|(?P<script>[_^])"
    r"|(?P<space>\\[ ,;!])"
    r"|(?P<char>[^\\{}_^\s]+)"
    r"|(?P<ws>\s+)"
    r")"
)


def _tokenize(latex: str) -> List[Tuple[str, str]]:
    tokens: List[Tuple[str, str]] = []
    pos = 0
    while pos < len(latex):
        m = _TOKEN_RE.match(latex, pos)
        if not m:
            # 无法匹配的单个字符（如孤立反斜杠）按文本处理
            tokens.append(("char", latex[pos]))
            pos += 1
            continue
        pos = m.end()
        if m.group("cmd"):
            tokens.append(("cmd", m.group("cmd")[1:]))
        elif m.group("brace"):
            tokens.append(("brace", m.group("brace")))
        elif m.group("script"):
            tokens.append(("script", m.group("script")))
        elif m.group("space"):
            tokens.append(("space", " "))
        elif m.group("char"):
            tokens.append(("char", m.group("char")))
        elif m.group("ws"):
            tokens.append(("ws", " "))
    return tokens


def _is_group_consume(tokens: List[Tuple[str, str]], i: int) -> Tuple[List[Tuple[str, str]], int]:
    """若 tokens[i] 是 '{'，返回（内部 token 列表, 新索引）；否则返回 None。"""
    if i < len(tokens) and tokens[i] == ("brace", "{"):
        depth = 1
        j = i + 1
        inner = []
        while j < len(tokens) and depth > 0:
            t = tokens[j]
            if t == ("brace", "{"):
                depth += 1
            elif t == ("brace", "}"):
                depth -= 1
                if depth == 0:
                    break
            inner.append(t)
            j += 1
        return inner, j + 1
    return None, i


class _LatexParser:
    """递归下降解析 LaTeX 数学表达式为 AST 节点列表。"""

    def parse(self, latex: str) -> list:
        tokens = _tokenize(latex)
        nodes, _ = self._parse_seq(tokens, 0, stop_brace=False)
        return self._attach_scripts(nodes)

    # --- 节点类型 ---
    # ("text", str)         普通文本（斜体）
    # ("op", str)           运算符（普通样式，非斜体）
    # ("frac", num, den)
    # ("sSub", base, sub)
    # ("sSup", base, sup)
    # ("sSubSup", base, sub, sup)
    # ("rad", deg, base)    deg 可为 None
    # ("nary", char, sub, sup, body)
    # ("seq", [nodes])

    def _parse_seq(self, tokens, i, stop_brace=True):
        nodes: list = []
        while i < len(tokens):
            t = tokens[i]
            if t[0] == "brace":
                if t[1] == "}" and stop_brace:
                    break
                if t[1] == "{":
                    inner, i = _is_group_consume(tokens, i)
                    sub_nodes, _ = self._parse_seq(inner, 0, stop_brace=True)
                    nodes.append(("seq", sub_nodes))
                    continue
            elif t[0] == "script":
                # 脚本延迟处理（_attach_scripts），这里先记录占位
                nodes.append(("script_marker", t[1]))
                i += 1
                continue
            elif t[0] == "ws":
                i += 1
                continue
            elif t[0] == "space":
                nodes.append(("op", " "))
                i += 1
                continue
            elif t[0] == "char":
                nodes.append(("text", t[1]))
                i += 1
                continue
            elif t[0] == "cmd":
                name = t[1]
                if name == "frac":
                    num, i = self._parse_required_group(tokens, i + 1)
                    den, i = self._parse_required_group(tokens, i)
                    nodes.append(("frac", num, den))
                    continue
                if name == "sqrt":
                    # \sqrt[n]{x}
                    if i + 1 < len(tokens) and tokens[i + 1] == ("brace", "["):
                        deg, i = self._parse_optional_bracket(tokens, i + 1)
                        base, i = self._parse_required_group(tokens, i)
                        nodes.append(("rad", deg, base))
                    else:
                        base, i = self._parse_required_group(tokens, i + 1)
                        nodes.append(("rad", None, base))
                    continue
                if name in _NARY:
                    # \sum_{i=1}^{n} expr
                    sub = sup = None
                    j = i + 1
                    if j < len(tokens) and tokens[j][0] == "script" and tokens[j][1] == "_":
                        sub, j = self._parse_script_arg(tokens, j + 1)
                    if j < len(tokens) and tokens[j][0] == "script" and tokens[j][1] == "^":
                        sup, j = self._parse_script_arg(tokens, j + 1)
                    body, i = self._parse_seq(tokens, j, stop_brace=True)
                    nodes.append(("nary", _NARY[name], sub, sup, body))
                    continue
                if name in ("text", "mathrm", "operatorname", "mbox"):
                    inner, i = _is_group_consume(tokens, i + 1)
                    if inner is not None:
                        txt = "".join(tk[1] for tk in inner if tk[0] in ("char", "cmd"))
                        nodes.append(("op", txt))
                        continue
                    nodes.append(("op", name))
                    i += 1
                    continue
                if name in ("left", "right"):
                    # \left( ... \right) —— 定界符作为普通运算符输出
                    if i + 1 < len(tokens):
                        d = tokens[i + 1][1]
                        ch = _LEFT_DELIM.get(d, d)
                        nodes.append(("op", ch))
                        i += 2
                    else:
                        i += 1
                    continue
                if name in _GREEK:
                    nodes.append(("text", _GREEK[name]))
                    i += 1
                    continue
                if name in _OPS:
                    nodes.append(("op", _OPS[name]))
                    i += 1
                    continue
                # 未知命令：尝试还原为符号名（如 \phi48 -> φ48 由后续 char 补全）
                if name == "phi" or name == "varphi":
                    nodes.append(("text", _GREEK[name]))
                    i += 1
                    continue
                # 其它未知命令：丢弃命令本身（避免反斜杠源码残留）
                i += 1
                continue
            i += 1
        return nodes, i

    def _parse_required_group(self, tokens, i):
        inner, ni = _is_group_consume(tokens, i)
        if inner is not None:
            return self._parse_seq(inner, 0, stop_brace=True), ni
        # 无花括号：单个原子
        if i < len(tokens):
            node, ni = self._parse_atom(tokens, i)
            return [node], ni
        return [("text", "")], i

    def _parse_optional_bracket(self, tokens, i):
        """tokens[i] == ('brace', '[') 时解析 [..] 返回（节点列表, 新索引）。"""
        if i < len(tokens) and tokens[i] == ("brace", "["):
            j = i + 1
            inner = []
            while j < len(tokens) and tokens[j] != ("brace", "]"):
                inner.append(tokens[j])
                j += 1
            return self._parse_seq(inner, 0, stop_brace=True), j + 1
        return None, i

    def _parse_script_arg(self, tokens, i):
        """解析脚本参数（单个原子或 {组}）。"""
        inner, ni = _is_group_consume(tokens, i)
        if inner is not None:
            return self._parse_seq(inner, 0, stop_brace=True), ni
        if i < len(tokens):
            node, ni = self._parse_atom(tokens, i)
            return [node], ni
        return [("text", "")], i

    def _parse_atom(self, tokens, i):
        t = tokens[i]
        if t[0] == "brace" and t[1] == "{":
            inner, ni = _is_group_consume(tokens, i)
            sub_nodes, _ = self._parse_seq(inner, 0, stop_brace=True)
            return ("seq", sub_nodes), ni
        if t[0] == "char":
            return ("text", t[1]), i + 1
        if t[0] == "cmd":
            name = t[1]
            if name == "frac":
                num, ni = self._parse_required_group(tokens, i + 1)
                den, ni = self._parse_required_group(tokens, ni)
                return ("frac", num, den), ni
            if name in _GREEK:
                return ("text", _GREEK[name]), i + 1
            if name in _OPS:
                return ("op", _OPS[name]), i + 1
            if name in _NARY:
                return ("nary", _NARY[name], None, None, []), i + 1
            if name in ("text", "mathrm", "operatorname", "mbox"):
                inner, ni = _is_group_consume(tokens, i + 1)
                if inner is not None:
                    return ("op", "".join(tk[1] for tk in inner if tk[0] in ("char", "cmd"))), ni
                return ("op", name), i + 1
            return ("text", ""), i + 1
        if t[0] == "script":
            return ("script_marker", t[1]), i + 1
        if t[0] == "space":
            return ("op", " "), i + 1
        return ("text", ""), i + 1

    def _attach_scripts(self, nodes: list) -> list:
        """将 script_marker 附着到前面的节点（支持 _ ^ 组合为 sSub/sSup/sSubSup）。"""
        out: list = []
        for node in nodes:
            if node[0] == "script_marker":
                if not out:
                    # 脚本前无基节点（罕见）：丢弃
                    continue
                marker = node[1]
                # 取出脚本参数（下一个节点即为脚本内容，已被解析为普通节点）
                # 这里通过后续处理：marker 后面应紧跟一个节点
                continue
            out.append(node)

        # 第二遍：真正附着。重新扫描，遇到 script 标记时合并。
        result: list = []
        # 简化实现：先按"标记+内容"成对合并为 attach 节点
        merged: list = []
        j = 0
        while j < len(nodes):
            node = nodes[j]
            if node[0] == "script_marker":
                marker = node[1]
                # 内容节点
                if j + 1 < len(nodes) and nodes[j + 1][0] != "script_marker":
                    content = nodes[j + 1]
                    merged.append(("_attach", marker, content))
                    j += 2
                else:
                    merged.append(("_attach", marker, ("text", "")))
                    j += 1
            else:
                merged.append(node)
                j += 1

        for node in merged:
            if node[0] == "_attach":
                marker, content = node[1], node[2]
                if not result:
                    result.append(("sSub" if marker == "_" else "sSup", ("text", ""), content))
                    continue
                base = result[-1]
                if base[0] == "sSub" and marker == "^":
                    result[-1] = ("sSubSup", base[1], base[2], content)
                elif base[0] == "sSup" and marker == "_":
                    result[-1] = ("sSubSup", base[1], content, base[2])
                elif base[0] == "sSubSup":
                    # 再叠加脚本：包一层
                    result[-1] = ("sSub" if marker == "_" else "sSup", base, content)
                elif base[0] == "_attach":
                    continue
                else:
                    if marker == "_":
                        result[-1] = ("sSub", base, content)
                    else:
                        result[-1] = ("sSup", base, content)
            else:
                result.append(node)
        return result


# ---------------------------------------------------------------------------
# AST -> OMML XML
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def _r(txt: str, sty_p: bool = False) -> str:
    if sty_p:
        return f'<m:r><m:rPr><m:sty m:val="p"/></m:rPr><m:t xml:space="preserve">{_esc(txt)}</m:t></m:r>'
    return f"<m:r><m:t xml:space='preserve'>{_esc(txt)}</m:t></m:r>"


def _node_to_omml(node) -> str:
    kind = node[0]
    if kind == "text":
        return _r(node[1])
    if kind == "op":
        return _r(node[1], sty_p=True)
    if kind == "seq":
        return "".join(_node_to_omml(n) for n in node[1])
    if kind == "frac":
        return (f"<m:f><m:fPr><m:type m:val='bar'/></m:fPr>"
                f"<m:num>{_node_to_omml(node[1])}</m:num>"
                f"<m:den>{_node_to_omml(node[2])}</m:den></m:f>")
    if kind == "sSub":
        return (f"<m:sSub><m:e>{_node_to_omml(node[1])}</m:e>"
                f"<m:sub>{_node_to_omml(node[2])}</m:sub></m:sSub>")
    if kind == "sSup":
        return (f"<m:sSup><m:e>{_node_to_omml(node[1])}</m:e>"
                f"<m:sup>{_node_to_omml(node[2])}</m:sup></m:sSup>")
    if kind == "sSubSup":
        return (f"<m:sSubSup><m:e>{_node_to_omml(node[1])}</m:e>"
                f"<m:sub>{_node_to_omml(node[2])}</m:sub>"
                f"<m:sup>{_node_to_omml(node[3])}</m:sup></m:sSubSup>")
    if kind == "rad":
        deg = node[1]
        base = _node_to_omml(node[2])
        if deg:
            return (f"<m:rad><m:radPr><m:degHide m:val='0'/></m:radPr>"
                    f"<m:deg>{_node_to_omml(deg)}</m:deg><m:e>{base}</m:e></m:rad>")
        return f"<m:rad><m:e>{base}</m:e></m:rad>"
    if kind == "nary":
        char, sub, sup, body = node[1], node[2], node[3], node[4]
        body_xml = "".join(_node_to_omml(n) for n in body)
        sub_xml = _node_to_omml(sub) if sub else ""
        sup_xml = _node_to_omml(sup) if sup else ""
        return (f"<m:nary><m:naryPr><m:chr m:val='{_esc(char)}'/></m:naryPr>"
                f"<m:sub>{sub_xml}</m:sub><m:sup>{sup_xml}</m:sup>"
                f"<m:e>{body_xml}</m:e></m:nary>")
    return ""


#: 悬空运算符结尾判据：``=`` / ``≤`` / ``≥`` / ``<`` / ``>`` 等（允许尾随空格）。
#: 命中表示「等式右端取值待定」的残式，旧实现只渲染运算符、右值位置空无一物，
#: 成稿在该处出现语义空洞（配合外层残留空格表现为双空格）。
_DANGLING_OP_RE = re.compile(r"(?:=|≤|≥|<|>|≈|le|geq?|leq?|ne)\s*$")

#: 悬空右值的可见占位文本（虚线方框由 OMML 边框呈现）。
_DANGLING_PLACEHOLDER = "待填"


def _dangling_placeholder_omml() -> str:
    """构造悬空右值占位框 OMML：带虚线边框的「待填」文字（直立样式）。"""
    return (
        "<m:box><m:boxPr><m:opEmu m:val=\"0\"/>"
        "<m:noBreak m:val=\"1\"/>"
        "<m:diff m:val=\"0\"/><m:brk m:val=\"0\"/>"
        "<m:alignment m:val=\"center\"/>"
        "<m:border><m:borderHide m:val=\"0\"/>"
        "<m:borderTop m:val=\"dashed\" smtHide=\"0\"/>"
        "<m:borderBot m:val=\"dashed\" smtHide=\"0\"/>"
        "<m:borderLeft m:val=\"dashed\" smtHide=\"0\"/>"
        "<m:borderRight m:val=\"dashed\" smtHide=\"0\"/>"
        "</m:border></m:boxPr>"
        f"<m:e>{_r(_DANGLING_PLACEHOLDER, sty_p=True)}</m:e></m:box>"
    )


def latex_to_omml(latex: str) -> str:
    """将 LaTeX 数学表达式转为 OMML 内联公式 XML（``<m:oMath>...</m:oMath>``）。

    若表达式以悬空关系运算符结尾（右值待定，如 ``N =``），自动在运算符后补一个
    虚线「待填」占位框：既明确提示此处需补数值，又避免成稿在该处出现语义空洞。
    仅作用于本就不完整的残式，完整公式产物逐字节不变。
    """
    nodes = _LatexParser().parse(latex)
    inner = "".join(_node_to_omml(n) for n in nodes)
    if _DANGLING_OP_RE.search(latex.strip()):
        inner += _dangling_placeholder_omml()
    return f"<m:oMath xmlns:m='{M_NS}'>{inner}</m:oMath>"


def latex_to_omml_display(latex: str) -> str:
    """将 LaTeX 数学表达式转为 OMML 块级公式 XML（``<m:oMathPara>...</m:oMathPara>``，居中）。"""
    inner = latex_to_omml(latex)
    # 提取 oMath 内容（去掉外层包裹，避免双重 <m:oMath>）
    m = re.search(r"<m:oMath[^>]*>(.*)</m:oMath>", inner, re.S)
    body = m.group(1) if m else inner
    return (f"<m:oMathPara xmlns:m='{M_NS}'><m:oMathParaPr><m:jc m:val='center'/></m:oMathParaPr>"
            f"<m:oMath>{body}</m:oMath></m:oMathPara>")


# ---------------------------------------------------------------------------
# 段落文本中的公式扫描
# ---------------------------------------------------------------------------

# ✅ 悬空运算尾：`= ` / `× ` 等运算符 + 一个空白结尾。
#    用于识别「等式右端取值待定」的行内公式写法（见下方 _INLINE_DOLLAR 说明）。
_DANGLING_OP_TAIL = r"[=+\-×÷≤≥<>≈]\s"

# 匹配 $$...$$ / \[...\]（块级）与行内 $...$ / \(...\)
# ✅ 误判修复：行内 $...$ 原为无约束的 \$.+?\$，正文中任意两个 $（货币、金额、
#    单位）之间的整段文本会被送进 LaTeX 解析器，未知命令被丢弃、_/^ 被拆上下标、
#    数学斜体改写，交付文档静默损坏。现收紧约束：
#    - 开 $ 后首字符非空白；
#    - 内容不含换行（防跨行误配对）；
#    - 内容不含中文标点（两个 $ 之间的普通中文文本几乎必含）；
#    - 闭 $ 前一字符非空白（LaTeX 行内公式惯例）。
# ✅ 补充修复（2026-09-19，实测交付文档取证）：上述第 4 条把
#    `$p_{max} = $××kPa`、`$f_a = $××kPa`、`$w_0 = $××kN/m²`、`$f_k = $××kN/m²`
#    这类「等式右端缺值」写法整段判为**非公式**（闭 $ 前是空格）→ 公式退化成
#    字面文本 `$p_{max} = $` 印进交付文档（脚手架方案计算书章节实测 4 处）。
#    现放宽：**仅当**公式内容以悬空运算符结尾（`= + - × ÷ ≤ ≥ < > ≈`，其后一个空白）
#    时才允许闭 $ 前为空白。普通中文文本跨 $ 误配对仍被字符类与"无空格"规则挡住。
_INLINE_DOLLAR = (
    r"\$(?!\s)"
    r"(?:\\.|[^$\\\n，。；：！？、（）《》“”‘’])+?"
    r"(?<!\s)\$"
)
# 悬空形态单独成式（与上式二选一），放在同组内由 _FORMULA_RE 的 inl 分支消费
_INLINE_DOLLAR_DANGLING = (
    r"\$(?!\s)"
    r"(?:\\.|[^$\\\n，。；：！？、（）《》“”‘’])+?"
    r"(?<=" + _DANGLING_OP_TAIL + r")\$"
)
_FORMULA_RE = re.compile(
    r"(?P<disp>\$\$.+?\$\$|\\\[.+?\\\])"
    r"|(?P<inl>(?:" + _INLINE_DOLLAR + r")|(?:" + _INLINE_DOLLAR_DANGLING
    + r")|\\\(.+?\\\))",
    re.S,
)


def iter_formulas(text: str):
    """迭代文本中的公式，产出（是否块级, latex内容, 匹配）三元组。"""
    for m in _FORMULA_RE.finditer(text):
        if m.group("disp"):
            raw = m.group("disp")
            latex = raw[2:-2] if raw.startswith("$$") else raw[2:-2]
            yield True, latex.strip(), m
        else:
            raw = m.group("inl")
            if raw.startswith(r"\("):
                latex = raw[2:-2]
            else:
                latex = raw[1:-1]
            yield False, latex.strip(), m


# ---------------------------------------------------------------------------
# 导出文档级扫描报告（供日志 / 前端提示）
# ---------------------------------------------------------------------------

def scan_issues(text: str) -> dict:
    """扫描一段文本中的可修复问题，返回统计（供报告展示）。"""
    stats = {"formulas": 0, "block_formulas": 0, "replacement_chars": 0,
             "control_chars": 0, "gbk_mojibake": 0, "latin1_mojibake": 0,
             "cyrillic": 0,
             # ✅ 2026-10-10（文档质量报告 格式问题5）：字面 ``\n`` / ``\*``
             #    转义残留数（排除公式跨度后计数，与 unescape 修复口径一致）
             "escape_residue": 0,
             # ✅ 2026-10-17（文档质量报告 符号/文字问题）：HTML 实体残留数 /
             #    中英混排误植词数 / 带边界空白 LaTeX 残留数（供 X-Fix-Stats 观测）
             "html_entities": 0, "en_residue": 0, "latex_residue": 0}
    for disp, _, _ in iter_formulas(text):
        stats["formulas"] += 1
        if disp:
            stats["block_formulas"] += 1
    stats["replacement_chars"] = len(re.findall(r"\ufffd", text))
    stats["control_chars"] = len(_CTRL_RE.findall(text))
    stats["gbk_mojibake"] = len(_MOJIBUKE_GBK_RE.findall(text))
    stats["latin1_mojibake"] = len(_LATIN1_RE.findall(text))
    # ✅ R104（2026-10-18 · 导出乱码收口）：西里尔统计覆盖两种形态——
    #    ≥2 字母词段误植 + 单字母形近字（ф→φ，后随数字）。
    #    词段正则不匹配单字母，旧口径对后者恒 0（修复发生了却无观测）。
    stats["cyrillic"] = (len(_CYRILLIC_SEG_RE.findall(text))
                         + len(_CYRILLIC_GLYPH_NUM_RE.findall(text)))
    # 转义残留只在公式跨度外的普通文本里统计（跨度内不修也不计）
    outside = "".join(p for i, p in enumerate(_FORMULA_SPAN_RE.split(text))
                      if i % 2 == 0)
    stats["escape_residue"] = (len(_MD_UNESCAPE_NL_TAB_RE.findall(outside))
                               + len(_MD_UNESCAPE_PUNCT_RE.findall(outside)))
    stats["html_entities"] = len(_HTML_ENTITY_RE.findall(text))
    stats["en_residue"] = len(_EN_RESIDUE_RE.findall(text))
    stats["latex_residue"] = len(_spaced_inline_math_inners(text))
    return stats


def summarize(text: str) -> str:
    """返回一行人类可读的修复摘要。"""
    s = scan_issues(text)
    parts = []
    if s["formulas"]:
        parts.append(f"公式 {s['formulas']} 处"
                     f"（块级 {s['block_formulas']}）")
    if s["replacement_chars"]:
        parts.append(f"替换字符 {s['replacement_chars']} 处")
    if s["control_chars"]:
        parts.append(f"控制字符 {s['control_chars']} 处")
    if s["gbk_mojibake"]:
        parts.append(f"GBK 乱码 {s['gbk_mojibake']} 处")
    if s["latin1_mojibake"]:
        parts.append(f"编码乱码 {s['latin1_mojibake']} 处")
    if s["cyrillic"]:
        parts.append(f"西里尔误植 {s['cyrillic']} 处")
    if s["escape_residue"]:
        parts.append(f"转义残留 {s['escape_residue']} 处")
    if s["html_entities"]:
        parts.append(f"HTML 实体残留 {s['html_entities']} 处")
    if s["en_residue"]:
        parts.append(f"英文误植 {s['en_residue']} 处")
    if s["latex_residue"]:
        parts.append(f"公式边界空白 {s['latex_residue']} 处")
    return "、".join(parts) if parts else "未发现可修复问题"
