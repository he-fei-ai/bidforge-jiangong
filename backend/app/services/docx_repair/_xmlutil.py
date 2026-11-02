# -*- coding: utf-8 -*-
"""docx_repair 内部共享：OpenXML 部件级（字符串层）读写工具（R147）。

全部为**无状态纯函数**：输入部件 XML 文本，输出新文本与统计。
插入子元素一律按 ECMA-376 CT_* 严格序列定位（顺序表与
``export.py:_STRICT_CT_SEQUENCE`` 同源；WPS 对序列违例会整份拒开）。

正则口径说明：本模块对 ``w:p`` 的切分用非贪婪 ``<w:p[ >].*?</w:p>``
（OOXML 中 ``w:p`` 不允许嵌套 ``w:p``，表格单元格内的段落是**兄弟**
出现，同样被逐个命中，无需真正的 XML 解析器）。
"""
from __future__ import annotations

import re
from xml.sax.saxutils import unescape

# ECMA-376 CT_PPr / CT_RPr 严格子元素序列（用于有序插入）。
PPR_ORDER: tuple[str, ...] = (
    "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr",
    "widowControl", "numPr", "suppressLineNumbers", "pBdr", "shd", "tabs",
    "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct",
    "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
    "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents",
    "suppressOverlap", "jc", "textDirection", "textAlignment",
    "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr",
    "pPrChange",
)
RPR_ORDER: tuple[str, ...] = (
    "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike",
    "dstrike", "outline", "shadow", "emboss", "imprint", "noProof",
    "snapToGrid", "vanish", "webHidden", "color", "spacing", "w", "kern",
    "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd",
    "fitText", "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout",
    "specVanish", "oMath",
)

#: ``w:pPr`` 开标签 + 内部内容（自闭合形式 ``<w:pPr/>`` 视为空容器）。
_PPR_RE = re.compile(r"<w:pPr>(.*?)</w:pPr>|<w:pPr\s*/>", re.S)
_PARA_RE = re.compile(r"<w:p\s[^>]*>.*?</w:p>|<w:p\s[^>]*/>|<w:p>.*?</w:p>", re.S)
_TEXT_RE = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.S)
_W_PREFIX_RE = re.compile(r"<w:([A-Za-z0-9]+)")


def unescape_xml_text(s: str) -> str:
    """w:t 文本反转义（&amp; &lt; &gt; &quot; &apos;）。"""
    return unescape(s, {"&quot;": '"', "&apos;": "'"})


def iter_paragraphs(xml: str) -> list[tuple[int, int, str]]:
    """返回 document.xml（或其他主部件）内全部段落的 (start, end, p_xml)。"""
    return [(m.start(), m.end(), m.group(0)) for m in _PARA_RE.finditer(xml)]


def para_text(p_xml: str) -> str:
    """段落可见文本（拼接全部 w:t，反转义）。"""
    return unescape_xml_text("".join(_TEXT_RE.findall(p_xml)))


def para_ppr(p_xml: str) -> str | None:
    """段落属性容器原文（含开闭标签）；无则 None。"""
    m = _PPR_RE.search(p_xml)
    return m.group(0) if m else None


def ppr_child(ppr_xml: str, tag: str) -> re.Match | None:
    """在 pPr 内查找子元素 ``<w:{tag} ...>``（含自闭合）。"""
    return re.search(rf"<w:{tag}(?:\s[^>]*)?/>|<w:{tag}(?:\s[^>]*)?>.*?</w:{tag}>",
                     ppr_xml, re.S)


def para_style(p_xml: str) -> str | None:
    """w:pStyle val（如 "Heading1" / "Normal" 缺省 None）。"""
    ppr = para_ppr(p_xml)
    if not ppr:
        return None
    m = ppr_child(ppr, "pStyle")
    if not m:
        return None
    vm = re.search(r'w:val="([^"]*)"', m.group(0))
    return vm.group(1) if vm else None


_HEADING_STYLE_RE = re.compile(r"^Heading([1-9])$")


def para_heading_level(p_xml: str) -> int | None:
    """段落标题级别：pStyle Heading{1-9} 优先，其次 w:outlineLvl（0 起 → 级别 1 起）。"""
    style = para_style(p_xml)
    if style:
        m = _HEADING_STYLE_RE.match(style)
        if m:
            return int(m.group(1))
    ppr = para_ppr(p_xml)
    if ppr:
        m = ppr_child(ppr, "outlineLvl")
        if m:
            vm = re.search(r'w:val="(\d+)"', m.group(0))
            if vm:
                lvl = int(vm.group(1)) + 1
                return min(max(lvl, 1), 9)
    return None


def has_numpr(p_xml: str) -> bool:
    ppr = para_ppr(p_xml)
    return bool(ppr and ppr_child(ppr, "numPr"))


def numpr_numid(p_xml: str) -> str | None:
    ppr = para_ppr(p_xml)
    if not ppr:
        return None
    m = ppr_child(ppr, "numPr")
    if not m:
        return None
    nid = re.search(r'<w:numId\s+w:val="(\d+)"', m.group(0))
    return nid.group(1) if nid else None


def _ordered_insert(container_text: str, child: str, order: tuple[str, ...],
                    container_tag: str) -> str:
    """把 child 插入容器（pPr/rPr）内按 order 序列的正确位置。

    - 容器内已存在同名首元素 → 原样返回（调用方应先判重）；
    - 容器内出现序列表以外的子元素 → 插到第一个「序列中排在 child 之后
      的已知元素」之前；全部未知则插到容器末尾（保守，不动未知元素）。
    """
    # 容器子元素首标签序列（按出现顺序去重；排除容器自身开标签）
    seen: list[str] = []
    for t in _W_PREFIX_RE.findall(container_text):
        if t == container_tag or t in seen:
            continue
        seen.append(t)
    child_tag = re.match(r"<w:([A-Za-z0-9]+)", child).group(1)  # type: ignore[union-attr]
    if child_tag in seen:
        return container_text  # 已存在，幂等
    child_rank = order.index(child_tag) if child_tag in order else len(order)
    for t in seen:
        if t not in order:
            continue
        if order.index(t) > child_rank:
            m = re.search(rf"<w:{t}(?:\s[^>]*)?/?>", container_text)
            if m:
                return container_text[:m.start()] + child + container_text[m.start():]
    # 全部已知元素都排在 child 之前（或无已知元素）→ 插到闭标签之前
    close = f"</w:{container_tag}>"
    idx = container_text.rfind(close)
    return container_text[:idx] + child + container_text[idx:]


def insert_ppr_child(ppr_xml: str, child: str) -> str:
    """向 ``<w:pPr>...</w:pPr>`` 按严格序列插入子元素；自闭合 pPr 先展开。"""
    if ppr_xml.endswith("/>"):
        return ppr_xml[:-2] + ">" + child + "</w:pPr>"
    return _ordered_insert(ppr_xml, child, PPR_ORDER, "pPr")


def insert_rpr_child(rpr_xml: str, child: str) -> str:
    if rpr_xml.endswith("/>"):
        return rpr_xml[:-2] + ">" + child + "</w:rPr>"
    return _ordered_insert(rpr_xml, child, RPR_ORDER, "rPr")


def ensure_ppr(p_xml: str) -> tuple[str, bool]:
    """确保段落有 pPr；返回 (新 p_xml, 是否发生插入)。插在 ``<w:p ...>`` 开标签后。"""
    if para_ppr(p_xml):
        return p_xml, False
    m = re.match(r"(<w:p(?:\s[^>]*)?>)", p_xml)
    if not m:  # 自闭合空段落 <w:p/>
        return p_xml, False
    return p_xml[:m.end()] + "<w:pPr></w:pPr>" + p_xml[m.end():], True
