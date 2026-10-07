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
    # ✅ 新增：剪贴板表格标记 / 残留 HTML 表格标签降级为可读纯文本
    #    （修复交付文档出现「<br><br><table><fcel><nl></table>」式乱码）
    out = strip_table_markup(out)
    return out


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
             "cyrillic": 0}
    for disp, _, _ in iter_formulas(text):
        stats["formulas"] += 1
        if disp:
            stats["block_formulas"] += 1
    stats["replacement_chars"] = len(re.findall(r"\ufffd", text))
    stats["control_chars"] = len(_CTRL_RE.findall(text))
    stats["gbk_mojibake"] = len(_MOJIBUKE_GBK_RE.findall(text))
    stats["latin1_mojibake"] = len(_LATIN1_RE.findall(text))
    stats["cyrillic"] = len(_CYRILLIC_SEG_RE.findall(text))
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
    return "、".join(parts) if parts else "未发现可修复问题"
