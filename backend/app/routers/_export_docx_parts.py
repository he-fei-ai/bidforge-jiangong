"""导出渲染·DOCX 部件群（行内格式/表格/封面页/表单页/图表插图插入等部件构建 helper，自 export.py 按导出阶段拆出，facade 延迟绑定，纯搬移零行为变更）"""

import app.routers.export as _sh  # noqa: F401 — facade 命名空间延迟绑定（monkeypatch 兼容）

_INLINE_RE = _sh.re.compile(
    r"\*\*\*(?P<bi>.+?)\*\*\*"
    r"|\*\*(?P<b>.+?)\*\*"
    r"|\*(?P<i>.+?)\*"
    r"|~~(?P<s>.+?)~~"
    r"|`(?P<c>[^`]+)`"
    r"|!?\[(?P<l>[^\]\n]+)\]\((?P<lu>[^)\s]*)\)"
)


#: XML 1.0 不允许的字符：C0 控制符（保留 \t \n \r）、U+FFFE/U+FFFF、孤立代理项。
#: ✅ BUG 修复（2026-11-03 · R175 · BUG-2）：python-docx 的 ``add_run(text)`` 把文本
#: 直接写进 ``w:t``，lxml 遇到非法字符会抛 ``ValueError: All strings must be
#: XML compatible``。封面标题 / 投标人 / 页眉页脚 / 封面信息表 / 前置表单等
#: 路径此前**直写用户输入**，表单里混入一个 ``\x00`` 即让整单导出 500
#: （连同已生成内容全部作废）。现统一经 ``_xml_safe_text`` 剥除非法字符
#: （合法空白 \t/\n/\r 原样保留），并由 ``_safe_add_run`` 作为唯一出口。
_XML_ILLEGAL_RE = _sh.re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff\ud800-\udfff]")


def _xml_safe_text(text) -> str:
    """剥除 XML 非法字符；``None`` → 空串，非字符串按 ``str`` 转换。"""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    if not s:
        return s
    return _XML_ILLEGAL_RE.sub("", s)


def _safe_add_run(paragraph, text):
    """``paragraph.add_run`` 的安全出口：先剥 XML 非法控制字符再添加 run。

    与 ``add_run`` 返回同一 run 对象，调用方可继续 ``_set_run_font``。
    """
    return paragraph.add_run(_xml_safe_text(text))


def _set_run_font(run, name: str, size=None, *, bold=None, italic=None,
                  strike=None, color=None):
    """统一设置 run 字体：同时写入 w:ascii / w:hAnsi 与 w:eastAsia。

    ✅ BUG 修复：旧实现只写 `run.font.name`（= w:ascii + w:hAnsi），中文实际
    由主题的 eastAsia 字体接管，在"宋体正文 + 黑体标题"等混排场景下中文会
    悄悄回退成默认字体，与配置不符。
    """
    from docx.oxml.ns import qn
    run.font.name = name
    rfonts = run._element.get_or_add_rPr().get_or_add_rFonts()
    rfonts.set(qn("w:eastAsia"), name)
    if size is not None:
        from docx.shared import Pt
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    if strike is not None:
        run.font.strike = strike
    if color is not None:
        from docx.shared import RGBColor
        run.font.color.rgb = RGBColor(*color)
    return run


def _add_markdown_runs(paragraph, text: str):
    """解析 Markdown 行内标记并添加为带格式的 run（公式已剥离的纯文本段）。

    支持 ``**粗体**`` / ``*斜体*`` / ``***粗斜体***`` / ``~~删除线~~`` /
    ``` `行内代码` ``` / ``[显示文本](链接)`` 与 ``![图片说明](url)``。

    ✅ 增强：链接/图片只保留显示文本 —— 旧实现把 `[附件1](http://…)` 这类
    Markdown 原文整段写进成稿，交付文档里出现裸语法。
    """
    pos = 0
    for m in _sh._INLINE_RE.finditer(text):
        if m.start() > pos:
            paragraph.add_run(text[pos:m.start()])
        if m.group("bi"):
            run = paragraph.add_run(m.group("bi"))
            run.bold = True
            run.italic = True
        elif m.group("b"):
            run = paragraph.add_run(m.group("b"))
            run.bold = True
        elif m.group("i"):
            run = paragraph.add_run(m.group("i"))
            run.italic = True
        elif m.group("s"):
            run = paragraph.add_run(m.group("s"))
            run.font.strike = True
        elif m.group("c") is not None:
            _sh._set_run_font(paragraph.add_run(m.group("c")), "Consolas")
        elif m.group("l") is not None:
            paragraph.add_run(m.group("l"))
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:])


def _add_omml_formula(paragraph, latex: str, display: bool, whole_para: bool):
    """将 LaTeX 公式转为 Word 原生公式（OMML）节点插入段落。

    - ``display`` 且 ``whole_para``：插入块级公式 ``<m:oMathPara>``（居中独占段）；
    - 其余：插入内联公式 ``<m:oMath>``。

    转换失败时回退为原文文本（``$...$``），保证不丢内容。
    """
    from docx.oxml import parse_xml

    try:
        if display and whole_para:
            xml = _sh.docx_math.latex_to_omml_display(latex)
        else:
            xml = _sh.docx_math.latex_to_omml(latex)
        node = parse_xml(xml)
        paragraph._p.append(node)
    except Exception:  # pragma: no cover - 转换失败兜底
        _sh.logger.warning("LaTeX 公式转换失败，保留原文: %r", latex[:60])
        paragraph.add_run(("$$" if display else "$") + latex + ("$$" if display else "$"))


def _collapse_redundant_spaces(text: str) -> str:
    """保守压缩纯文本片段中的冗余空白。

    ✅ F2 修复：dangling 残式（如 ``$N = $``）右值位置为空，公式节点前后
    残留的空格会在成稿里形成连续双空格。这里仅把 **2 个及以上连续的
    空格/制表符** 归一为单个空格，不触碰单个空格、换行与中文，避免误伤
    正常排版与中英文混排间距。
    """
    if not text:
        return text
    return _sh.re.sub(r"[ \t]{2,}", " ", text)


# ---------------------------------------------------------------------------
# ✅ R179（2026-11-05）：「第1轮格式问题分析报告」根因收口 —— 交付文本规范化
# ---------------------------------------------------------------------------
# 报告把 16 类痕迹归因为「PDF/Word/HTML/Markdown 多次转换」，但那只是表象。
# 逐段追溯后的真实入口有四类：
#   ① 上传解析把 PDF 分页标记 / 页脚当正文写进内容模型；
#   ② 提示词生成直接输出 LaTeX / HTML 实体 / Markdown 管道符；
#   ③ 旧版转换器把 HTML 再转 Markdown 时产生双重实体（``&amp;gt;``）；
#   ④ 模型混入英文误植词、破折号与标准号格式不统一。
# 本函数是渲染层统一出口：``_add_runs_with_inline_format`` 覆盖正文 / 标题 /
# 表格单元格；题注路径（表题 / 图题 / 占位行）单独调用。**只做确定性替换**：
# 「一」形破折号无法与「统一」等正常词区分，明确不自动替换（列入人工校对待办）。
_FORMULA_SPAN_RE = _sh.re.compile(
    r"(\$\$.*?\$\$|\$[^$\n]*?\$|\\\(.+?\\\)|\\\[.+?\\\])", _sh.re.S)

#: 「1.5% 0」→「1.5‰」（千分号被 PDF/OCR 拆成「% 0」的高置信形态）。
_RE_PERMILLE = _sh.re.compile(r"(?<=\d)[ \t]*%[ \t]+0(?![0-9.])")

#: 「0.5MQ」→「0.5MΩ」（兆欧符号 O 被识别成 Q 的高置信形态）。
_RE_MQ = _sh.re.compile(r"(?<=[0-9])[ \t]*MQ")

#: 中英混排误植词（仅替换**紧邻中文**的独立英文词，避免误伤 URL / 代码 / 品牌）。
_RE_EN_TERMS = _sh.re.compile(
    r"(?P<cn>[\u4e00-\u9fff])[ \t]?"
    r"(?P<w>devices|fabrication|ventilation|spots|spot|materials|equipment)"
    r"(?![A-Za-z])",
    _sh.re.I,
)
_EN_TERMS_MAP = {
    "devices": "设备",
    "fabrication": "制作",
    "ventilation": "通风",
    "spots": "斑点",
    "spot": "斑点",
    "materials": "材料",
    "equipment": "设备",
}

#: 标准编号统一为「前缀 编号-年份」口径（如 `GB50303- 2015` → `GB 50303-2015`）。
_RE_STD_NO = _sh.re.compile(
    r"(?P<p>GBJ|GB/T|GB|JGJ/T|JGJ|CJJ/T|CJJ|DG/TJ|DGJ|JG/T|SH/T|HG/T)"
    r"[ \t]*(?P<n>\d{2,5}(?:\.\d+)?)[ \t]*[—–\-－][ \t]*(?P<y>[12]\d{3})",
    _sh.re.I,
)

#: 中文语境下的半角括号统一为全角；`f(x)` 这类纯 ASCII 公式形态保持不动。
_RE_PAREN_CJK_CONTENT = _sh.re.compile(r"\(([^()\n]{0,50}[\u4e00-\u9fff][^()\n]{0,50})\)")
_RE_PAREN_AFTER_CJK = _sh.re.compile(r"(?<=[\u4e00-\u9fff])[ \t]?\(([^()\n]{1,50})\)")
_RE_PAREN_STD = _sh.re.compile(
    r"\(((?:GBJ|GB/T|GB|JGJ/T|JGJ|CJJ/T|CJJ|DG/TJ|DGJ|JG/T|SH/T|HG/T)"
    r"[ \t]*\d{2,5}(?:\.\d+)?[ \t]*[—–\-－][ \t]*[12]\d{3})\)", _sh.re.I)

#: 仅归一**两个中文之间**的连字符为破折号；`GB 50303-2015` / `1-2` 不动。
_RE_CJK_DASH = _sh.re.compile(r"(?<=[\u4e00-\u9fff])[ \t]*[-–][ \t]*(?=[\u4e00-\u9fff])")

#: 数值比较符两侧去空格（`30 ≤ 40` → `30≤40`）。
_RE_CMP_GAP = _sh.re.compile(r"(?<=\d)[ \t]*(≤|≥|≠|<|>)[ \t]*(?=\d)")

#: 数字与单位间空格统一（`30 mA` → `30mA`）。
#: ⚠️ 只收**多字符或无歧义**单位：单字母 `A/W/V/N/L/m/g/t/h/s` 会把
#: `表 1-1 A、B`（表题列名）误改成 `1-1A、B`，故一律不纳入。
_RE_UNIT_GAP = _sh.re.compile(
    r"(?<=\d)[ \t\xa0\u3000]+"
    r"(?P<u>mm|cm|km|MPa|kPa|kW|kN|mA|mL|MΩ|min|kg|Pa|℃|%|°)"
    r"(?![A-Za-z0-9])"
)

#: LaTeX 命令 → 普通符号。只映射**语义唯一**的常见命令，未知命令原样保留
#: （由落盘前 lint 统计为残留并触发可见降级，绝不静默放过）。
_LATEX_CMD_MAP = {
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "times": "×", "cdot": "·", "pm": "±", "sim": "～",
    "Omega": "Ω", "Phi": "Φ", "varphi": "φ", "phi": "φ", "mu": "μ",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "theta": "θ",
    "lambda": "λ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ",
    "degree": "°", "circ": "°", "quad": " ", "qquad": " ",
}
_RE_LATEX_CMD = _sh.re.compile(r"\\(?P<c>[A-Za-z]+)")
_RE_LATEX_ESC = _sh.re.compile(r"\\(?P<c>[%&_#{}$])")
_RE_LATEX_MATHRM = _sh.re.compile(r"\\(?:mathrm|text|mathbf|mathit)\{([^{}]*)\}")
_RE_LATEX_FRAC = _sh.re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}")
_RE_LATEX_SQRT = _sh.re.compile(r"\\sqrt\{([^{}]*)\}")
_RE_LATEX_DEG_C = _sh.re.compile(
    r"(?:\^\s*)?\{?\s*\\circ\s*\}?\s*(?:\\mathrm\{C\}|\{C\}|C)?")
_RE_SUPERSCRIPT = _sh.re.compile(r"(?<=[A-Za-z0-9)])[ \t]*\^[ \t]*\{?([23])\}?")
#: 数值区间里的波浪号两侧去空格（`0.6～ 0.8` → `0.6～0.8`）。
_RE_TILDE_GAP = _sh.re.compile(r"(?<=\d)[ \t]*～[ \t]*(?=\d)")


def _latex_to_plain(text: str) -> str:
    """把**残留 LaTeX** 转成可读普通符号（已成功转 OMML 的公式不会走到这里）。"""
    if not text:
        return text
    def _deg(m):
        # `^{\circ}C` 是「摄氏度」不是「度」—— 有 C 才用 ℃，否则用 °
        return "℃" if m.group(0).rstrip().endswith("C") else "°"

    s = _RE_LATEX_DEG_C.sub(_deg, text)
    s = _RE_LATEX_MATHRM.sub(r"\1", s)
    s = _RE_LATEX_FRAC.sub(r"\1/\2", s)
    s = _RE_LATEX_SQRT.sub(r"√\1", s)
    s = _RE_SUPERSCRIPT.sub(lambda m: {"2": "²", "3": "³"}[m.group(1)], s)
    s = _RE_LATEX_CMD.sub(
        lambda m: _LATEX_CMD_MAP.get(m.group("c"), m.group(0)), s)
    s = _RE_LATEX_ESC.sub(lambda m: m.group("c"), s)
    s = s.replace("°C", "℃").replace("° C", "℃")
    return s


def _normalize_plain_segment(text: str) -> str:
    """规范化一个**公式跨度之外**的文本片段（顺序固定，保证幂等）。"""
    if not text or not isinstance(text, str):
        return text
    s = text.replace("\xa0", " ")
    if "&" in s:
        # 逐层解码 HTML 实体（`&amp;gt;` → `&gt;` → `>`），最多多解一层覆盖
        # 「PDF→HTML→Markdown→docx」链路造成的 `&amp;amp;gt;` 三重形态。
        for _ in range(3):
            try:
                decoded = _sh.docx_math.decode_html_entities(s)
            except Exception:  # pragma: no cover - 解码失败保持原文
                break
            if decoded == s:
                break
            s = decoded
    if "\\" in s or "^" in s:
        s = _latex_to_plain(s)
    s = _RE_PERMILLE.sub("‰", s)
    s = _RE_MQ.sub("MΩ", s)
    s = s.replace("力矩矩", "力矩").replace("峻工", "竣工")
    s = _RE_EN_TERMS.sub(
        lambda m: m.group("cn") + _EN_TERMS_MAP[m.group("w").lower()], s)

    def _std(m):
        p = _sh.re.sub(r"[ \t]*/[ \t]*", "/", m.group("p")).upper()
        return f"{p} {m.group('n')}-{m.group('y')}"

    s = _RE_STD_NO.sub(_std, s)
    s = _RE_TILDE_GAP.sub("～", s)
    s = _RE_CMP_GAP.sub(r"\1", s)
    s = _RE_PAREN_CJK_CONTENT.sub(r"（\1）", s)
    s = _RE_PAREN_AFTER_CJK.sub(r"（\1）", s)
    # 纯 ASCII 括号里若是标准编号（`(GB 50303-2015)`）也统一为全角；
    # `f(x)` / `(a+b)` 这类公式形态不动（正反例见回归测试）。
    s = _RE_PAREN_STD.sub(r"（\1）", s)
    s = _RE_CJK_DASH.sub("—", s)
    s = _RE_UNIT_GAP.sub(lambda m: m.group("u"), s)
    return s


def _normalize_delivery_text(text: str, *, residual: bool = False) -> str:
    """交付文本规范化的唯一入口。

    Args:
        text: 待规范化的文本。
        residual: ``False``（渲染期）→ 公式跨度保持原样，交给 OMML 转换器；
            ``True``（落盘前 lint）→ 任何仍存在的公式跨度都意味着转换失败，
            剥掉分隔符并按普通符号还原，避免 ``$...$`` / ``\\(...\\)`` 印进成稿。
    """
    if not text or not isinstance(text, str):
        return text
    parts = _FORMULA_SPAN_RE.split(text)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            out.append(_normalize_plain_segment(part))
        elif residual:
            inner = part
            if part.startswith("$$") and part.endswith("$$"):
                inner = part[2:-2]
            elif part.startswith(r"\(") and part.endswith(r"\)"):
                inner = part[2:-2]
            elif part.startswith(r"\[") and part.endswith(r"\]"):
                inner = part[2:-2]
            elif part.startswith("$") and part.endswith("$"):
                inner = part[1:-1]
            out.append(_normalize_plain_segment(inner))
        else:
            out.append(part)
    return "".join(out)


def _add_runs_with_inline_format(paragraph, text: str):
    """按公式 + 行内标记混合流写入段落。

    ✅ 乱码修复（软件内建方案）：
    1. ``clean_text`` 先清理控制字符 / U+FFFD / GBK 乱码 / 编码 mojibake /
       中文文档中西里尔误植；
    2. ``$...$`` / ``$$...$$`` / ``\\(...\\)`` / ``\\[...\\]`` 公式转为
       Word 原生公式（OMML），不再以 LaTeX 源码形式残留在导出文档中；
    3. ✅ R179：``_normalize_delivery_text`` 收口残留 LaTeX 符号 / 双重实体 /
       千分号 / 兆欧符号 / 英文误植词 / 标准号 / 单位空格 / 中文破折号。
    """
    _sh._accumulate_fix_stats(text)
    text = _sh.docx_math.clean_text(text)
    text = _normalize_delivery_text(text)
    pos = 0
    for disp, latex, m in _sh.docx_math.iter_formulas(text):
        if m.start() > pos:
            _sh._add_markdown_runs(paragraph, _sh._collapse_redundant_spaces(text[pos:m.start()]))
        whole = text.strip() == m.group(0).strip()
        _sh._add_omml_formula(paragraph, latex, disp, whole)
        pos = m.end()
    if pos < len(text):
        _sh._add_markdown_runs(paragraph, _sh._collapse_redundant_spaces(text[pos:]))


# 本次导出会话的修复统计（导出完成时输出日志并清零）。
# ✅ BUG 修复（并发数据竞争）：DOCX 构建经 `asyncio.to_thread` 在**多个工作线程**中
#    并发执行，而旧实现用模块级全局 dict 累加统计，两个导出同时进行时会出现：
#      · 后完成的导出把先完成者累加的计数一并读出并清零（统计张冠李戴）；
#      · `dict[k] += n` 本身非原子，并发自增还会静默丢计数。
#    DOCX 构建全程都在**单一线程内**完成，改用线程局部存储（threading.local）
#    即可天然隔离各次导出，无需加锁。
_FIX_STATS_KEYS = ("formulas", "block_formulas", "replacement_chars",
                   "control_chars", "gbk_mojibake", "latin1_mojibake",
                   "cyrillic",
                   # ✅ 新增（2026-10-06 · P0 逐块 fail-soft 可观测性）：
                   #   write_section 内单个内容块渲染抛异常时降级为纯文本/跳过，
                   #   绝不让一份文档因一个坏块整体 500。降级次数必须可观测
                   #   （日志 + X-Fix-Stats 响应头），否则「成稿少了一块」变成
                   #   与「静默丢数据」同级的不可见缺陷。
                   #   加法式扩展：既有 7 个键的语义与取值一字未动。
                   "block_render_failed",
                    # ✅ 新增（2026-10-06 · P1 表格尺寸上限）：
                    #   GFM 表格行列/单元格数超限时截断的次数（正常表格恒为 0）。
                    "table_truncated",
                    # ✅ R99 新增（2026-10-17 · 围栏自动修复可观测性）：
                    #   导出前置「自动补齐未闭合围栏」修好的围栏数。开启该开关后
                    #   DLV-07 的 error 级提示会被补齐动作抹掉，而围栏之后的正文
                    #   可能被并入图表源码（实测开启后丢得比关闭时更多），用户若
                    #   无任何提示就等于「静默丢正文」。计数随 X-Fix-Stats 下发，
                    #   与 audit_content 的 fence_auto_fixed 提示互为佐证。
                    #   加法式扩展：既有 9 个键的语义与取值一字未动（未开启该
                    #   开关时恒为 0）。
                    "fences_fixed",
                    # ✅ 2026-10-17（文档质量报告 符号/文字/公式问题）：
                    #   HTML 实体残留（`&gt;`/`&lt;`）/ 中英混排误植词 / 带边界空白
                    #   LaTeX 残留的自动修复计数。加法式扩展：既有键的语义与取值
                    #   一字未动，新键仅在源文含对应残留时非零。
                    "html_entities", "en_residue", "latex_residue",
                    # ✅ 2026-10-17（导出文档发现的问题）：表格「两块侧叠」错位行
                    #   被拆分还原的次数（普通表恒为 0）。加法式扩展：既有键语义
                    #   与取值一字未动。
                    "table_unstacked")
_FIX_STATS_LOCAL = _sh.threading.local()


def _fix_stats() -> dict:
    """返回当前线程的修复统计累加器（线程内单例）。"""
    d = getattr(_sh._FIX_STATS_LOCAL, "stats", None)
    if d is None:
        d = {k: 0 for k in _sh._FIX_STATS_KEYS}
        _sh._FIX_STATS_LOCAL.stats = d
    return d


def _accumulate_fix_stats(text: str):
    """累加本次导出的公式/乱码修复统计（供导出完成日志）。"""
    try:
        s = _sh.docx_math.scan_issues(text)
        st = _sh._fix_stats()
        for k in st:
            st[k] += s.get(k, 0)
    except Exception:  # pragma: no cover
        _sh.logger.debug("导出修复统计累加失败（跳过该段扫描）", exc_info=True)


def _log_fix_stats() -> dict:
    """输出导出修复统计日志并清零，返回统计 dict（供响应头带给前端）。"""
    st = _sh._fix_stats()
    if not any(st.values()):
        return {}
    parts = []
    if st["formulas"]:
        parts.append(f"公式转数学排版 {st['formulas']} 处"
                     f"（块级 {st['block_formulas']}）")
    if st["replacement_chars"]:
        parts.append(f"替换字符 {st['replacement_chars']} 处")
    if st["control_chars"]:
        parts.append(f"控制字符 {st['control_chars']} 处")
    if st["gbk_mojibake"]:
        parts.append(f"GBK 乱码 {st['gbk_mojibake']} 处")
    if st["latin1_mojibake"]:
        parts.append(f"编码乱码 {st['latin1_mojibake']} 处")
    if st["cyrillic"]:
        parts.append(f"西里尔误植 {st['cyrillic']} 处")
    if st["block_render_failed"]:
        parts.append(f"内容块降级为纯文本 {st['block_render_failed']} 处"
                     "（成稿已保留文字，版式降级）")
    if st["table_truncated"]:
        parts.append(f"超宽/超长表格截断 {st['table_truncated']} 处")
    if st["fences_fixed"]:
        parts.append(f"未闭合围栏自动补齐 {st['fences_fixed']} 处"
                     "（围栏之后的正文可能被并入图表源码，请核对成稿）")
    if st["html_entities"]:
        parts.append(f"HTML 实体还原 {st['html_entities']} 处（&gt;/&lt; → >/<）")
    if st["en_residue"]:
        parts.append(f"中英混排误植还原 {st['en_residue']} 处（英文词 → 中文）")
    if st["latex_residue"]:
        parts.append(f"公式边界空白归一 {st['latex_residue']} 处（$ x $ → $x$）")
    stats = dict(st)
    if st["block_render_failed"] or st["table_truncated"]:
        _sh.logger.warning(
            "DOCX 导出存在降级：%d 处内容块渲染失败（已降级为纯文本）、"
            "%d 处表格超尺寸（已截断）—— 成稿可交付但版式不完整，"
            "请回查源文对应章节", st["block_render_failed"], st["table_truncated"])
    if st["fences_fixed"]:
        _sh.logger.warning(
            "DOCX 导出前置自动补齐了 %d 处未闭合围栏：围栏之后的正文会被并入"
            "图表源码并整体跳过（CommonMark 口径），成稿请人工核对缺失段落",
            st["fences_fixed"])
    _sh.logger.info("DOCX 导出自动修复: %s", "、".join(parts))
    st.update({k: 0 for k in st})
    return stats


#: 附录事实单元格行首的 Markdown 列表符号（``- `` / ``* `` / ``+ ``）。
_APPENDIX_LIST_LEAD_RE = _sh.re.compile(r"^\s*[-*+]\s+")


def _strip_leading_fact_name(text: str, name: str) -> str:
    """去掉「**名称**: 取值」里与前列表头重复的名称前缀，只留取值。

    ✅ R179（2026-11-05）：附录事实表第一列已是事实名，第二列旧实现只剥了
    ``- `` 列表符，仍显示「名称: 取值」→ 每行名称重复一次（成稿观感问题，
    与 R175 BUG-09 只修 Markdown 残留属同一处未收口）。仅在行首**精确**
    匹配本行名称时剥离，取值本身含冒号/名称不匹配时一律原样保留。
    """
    nm = (name or "").strip()
    body = (text or "").strip()
    if not nm or not body:
        return text
    for pat in (rf"^\*\*\s*{_sh.re.escape(nm)}\s*\*\*\s*[:：]\s*", rf"^{_sh.re.escape(nm)}\s*[:：]\s*"):
        m = _sh.re.match(pat, body)
        if m:
            return body[m.end():].strip()
    return text


def _appendix_cell_paragraph(cell, text: str, font_name: str,
                             font_size: float = 10.5, *, first: bool = False,
                             strip_name: str = ""):
    """向附录事实表单元格写入一行内容：剥离开头列表符号并解析行内 Markdown。

    旧实现直接 ``add_run`` 写入全局事实原文，``- **名称**: 内容`` 等列表 +
    加粗语法会以源码形式残留在成稿单元格内（实测附录 14 张表全部中招）。
    本函数：① 剥离行首 ``- ``/``* `` 列表符号；② 复用
    ``_add_runs_with_inline_format`` 解析 ``**加粗**`` / 公式 / 链接等行内标记；
    ③ 统一中文字体。仅改变附录事实单元格的渲染，正文与其他表格不受影响。
    ④ ✅ R179：``strip_name`` 非空时剥离与第一列重复的名称前缀（只留取值）。
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    lines = [ln for ln in (text or "").split("\n")]
    p = cell.paragraphs[0] if first else cell.add_paragraph()
    started = False
    for raw in lines:
        cleaned = _sh._APPENDIX_LIST_LEAD_RE.sub("", raw.strip())
        if strip_name:
            cleaned = _strip_leading_fact_name(cleaned, strip_name)
        target = p if not started else cell.add_paragraph()
        target.alignment = WD_ALIGN_PARAGRAPH.LEFT
        target.paragraph_format.first_line_indent = Pt(0)
        _sh._add_runs_with_inline_format(target, cleaned)
        for run in target.runs:
            _sh._set_run_font(run, font_name, font_size)
        p = target
        started = True
    return p


def _add_table_caption(doc, text: str, font_name: str = "宋体", font_size: float = 10.5):
    """插入表题「表 {章号}-{序号} 表名」（GB/T：表题位于表格**上方**，居中加粗）。

    ✅ 新增：与图题（位于图下方）配套，使表格也有规范编号，满足交付评审要求。
    ``keep_with_next`` 保证表题不与其表格被分页拆散。
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    pf = p.paragraph_format
    pf.first_line_indent = Cm(0)
    pf.space_before = Pt(6)
    pf.space_after = Pt(2)
    pf.keep_with_next = True
    # ✅ R179：题注与正文走同一规范化口径（表名可能来自 AI 首行，含 LaTeX/单位残留）
    _sh._set_run_font(_safe_add_run(p, _normalize_delivery_text(text)), font_name, font_size, bold=True)
    return p


#: 表题开头「表 X-Y」/「表：」等编号前缀（剥离后再由导出统一编号，避免叠写）。
_LEADING_TABLE_NUM_RE = _sh.re.compile(
    r"^表\s*(?:[:：]\s*|[0-9一二三四五六七八九十][0-9.\-–—]*[\s、.：:]+)")


def _strip_leading_table_number(text: str) -> str:
    """剥离表题中 AI 自带的「表 X-Y」编号前缀。

    ✅ 2026-10-17（文档质量报告 表号问题根因 B）：实测交付文档出现
    「表 6-3 表 6-1 木工班组劳动力配置计划」——AI 把表题连同编号写进表格
    **首行首格**（首格 = 「表 6-1 木工班组劳动力配置计划」），导出的自动表题
    取首行前 3 列拼接后再前置统一编号，于是「表 6-3」与「表 6-1」叠写。
    表题编号由导出**唯一**生成，源文自带的编号一律剥离，杜绝双表号。
    """
    t = str(text or "").strip()
    if not t.startswith("表"):
        return t
    stripped = _sh._LEADING_TABLE_NUM_RE.sub("", t).strip()
    return stripped or t


#: GFM 表格尺寸上限（2026-10-06 · P1 性能/稳定性）。
#: 实测无上限时单表耗时随行列乘积急剧上升：60 列×20 行 = 2.4s、
#: 200 列×40 行 = **39.5s**、400 列×60 行 > 110s 未完成（建单元格 XML 是
#: O(rows×cols) 次 OXML 节点构造）。而 `_build_docx_sync` 全程在
#: `asyncio.to_thread` 的**单个工作线程**里跑、无内部超时 → 一张畸形宽表
#: 就能把导出线程长期占死，用户侧表现为"导出卡住不动"。
#: 正常工程表格（列 ≲ 20、行 ≲ 100）远低于下列阈值，产物逐字节不变。
_TABLE_MAX_COLS = 63     # Word 表格硬上限即 63 列
_TABLE_MAX_ROWS = 400
_TABLE_MAX_CELLS = 12000


def _unstack_or_trim_table_rows(rows: list[list[str]]) -> list[list[str]]:
    """把「两块侧叠」的错位表格拆回规范行，并去掉尾部整列全空的多余列。

    ✅ BUG 修复（2026-10-17 · 导出文档发现的问题）：AI 偶发把**两条记录塞进一行**
    的「两块并列」形态（实测第 6 章「劳动力配置计划」表：表头只有 5 列有名称、
    其后 6 列全空，而首条数据行在空白分隔列之后又填了第二条记录）。旧实现
    ``ncols = max(len(r))`` → 整表被撑成 **11 列**、两条记录挤在同一行，成稿既宽
    又错位、无法阅读。

    判据（保守，只在形态高度可信时才改写；普通表逐字不动）：
      1. 先去掉**尾部整列全空**的多余列（所有行该列都为空 ⇒ 必为冗余）；
      2. 再找**整列全空**的「分隔列」，把表切成 ≥2 个列块；
      3. 仅当表头**只填充第一个列块**（其后各列块表头全空）时，判为侧叠；
    命中则每个数据行按「非空列块」拆成多行（列块内取值左对齐），让侧叠的
    第二条记录回到独立行；不命中一律原样返回（零误伤）。

    设计取舍：只**重排**不**丢弃**（AGENTS.md §3.1.6「宁可不改，不丢内容」）——
    侧叠的第二条记录被提升为独立行而非截掉。
    """
    if not rows:
        return rows
    ncols = max(len(r) for r in rows)
    if ncols < 2:
        return rows
    grid = [list(r) + [""] * (ncols - len(r)) for r in rows]
    empty_col = [all(not g[c].strip() for g in grid) for c in range(ncols)]

    # 1) 尾部整列全空 → 去掉（安全：所有行该列都为空）
    while ncols > 1 and empty_col[ncols - 1]:
        ncols -= 1
    grid = [g[:ncols] for g in grid]
    empty_col = empty_col[:ncols]

    # 2) 列块 = 被「整列全空」分隔的连续非空列区间
    blocks: list[tuple[int, int]] = []
    start = None
    for c in range(ncols):
        if empty_col[c]:
            if start is not None:
                blocks.append((start, c))
                start = None
        elif start is None:
            start = c
    if start is not None:
        blocks.append((start, ncols))
    if len(blocks) < 2:
        return grid
    # 3) 表头只填第一块（其后各块表头全空）才认定侧叠
    header = grid[0]
    if any(any(header[a:b]) for a, b in blocks[1:]):
        return grid

    out: list[list[str]] = [header[blocks[0][0]:blocks[0][1]]]
    width = len(out[0])
    for g in grid[1:]:
        for a, b in blocks:
            seg = g[a:b]
            if any(x.strip() for x in seg):
                out.append(seg)
                width = max(width, len(seg))
    _sh.logger.warning(
        "导出：表格侧叠行已拆分还原（原 %d 行 × %d 列 → %d 行 × %d 列）"
        "——源文存在「两条记录挤在同一行」的错位，成稿已按记录分行。",
        len(rows), ncols, len(out), width)
    _sh._fix_stats()["table_unstacked"] = _sh._fix_stats().get("table_unstacked", 0) + 1
    return [r + [""] * (width - len(r)) for r in out]


def _add_table_from_markup(doc, tbl_lines: list[str], font_name: str = "宋体"):
    """将 GFM 表格行转为 Word 表格。

    表头：`font_name` 五号加粗、居中、暗板岩蓝浅色 60% 底纹（B6B1D1）；
    表体：`font_name` 五号居中；整表居中、单元格垂直居中、表头跨页重复。

    ✅ 2026-10-06（P1）：行列/单元格数设上限并**留痕**（`table_truncated`
    计入 X-Fix-Stats），超限时按「行 → 列」顺序截断而非让导出线程被占死。
    """
    from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    def _split_row(raw: str) -> list[str]:
        # 先保护转义竖线 \| ，再按竖线切分，最后还原为普通 |
        safe = raw.strip().replace("\\|", "\x00").strip("|")
        return [c.replace("\x00", "|").strip() for c in safe.split("|")]

    # 跳过第 2 行（|:---| 对齐分隔行，由调用方保证存在）
    rows = [_split_row(r) for j, r in enumerate(tbl_lines) if j != 1]
    if not rows:
        return
    # ✅ 2026-10-17（导出文档发现的问题）：先还原「两块侧叠」的错位行 + 去掉尾部
    #    整列全空的多余列，再据此确定列数（否则 max(len) 会把整表撑成 11 列）。
    rows = _sh._unstack_or_trim_table_rows(rows)
    ncols = max(len(r) for r in rows)
    # ---- 尺寸上限（先列后行，保证表头行永远保留）----
    raw_cols, raw_rows = ncols, len(rows)
    truncated = False
    if ncols > _sh._TABLE_MAX_COLS:
        ncols = _sh._TABLE_MAX_COLS
        truncated = True
    if len(rows) > _sh._TABLE_MAX_ROWS:
        rows = rows[:_sh._TABLE_MAX_ROWS]
        truncated = True
    if len(rows) * ncols > _sh._TABLE_MAX_CELLS:
        keep = max(1, _sh._TABLE_MAX_CELLS // max(1, ncols))
        if keep < len(rows):
            rows = rows[:keep]
            truncated = True
    if truncated:
        _sh._fix_stats()["table_truncated"] = _sh._fix_stats().get("table_truncated", 0) + 1
        _sh.logger.warning(
            "导出：表格超尺寸已截断（原始 %d 列 × %d 行 → %d 列 × %d 行，"
            "上限 %d 列 / %d 行 / %d 单元格）—— 成稿可交付但该表内容不完整，"
            "请回查源文", raw_cols, raw_rows, ncols, len(rows),
            _sh._TABLE_MAX_COLS, _sh._TABLE_MAX_ROWS, _sh._TABLE_MAX_CELLS)
    table = doc.add_table(rows=len(rows), cols=ncols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    # ✅ BUG 修复：旧实现 `table.allow_autofit = True` —— python-docx 的 Table
    #    根本没有该属性（已核实），赋值只是往对象上挂了个无人读取的属性（死代码），
    #    容易被误读为"已开启自适应布局"。

    for ri, row_data in enumerate(rows):
        for ci in range(ncols):
            cell_text = row_data[ci] if ci < len(row_data) else ""
            cell = table.rows[ri].cells[ci]
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.first_line_indent = Pt(0)
            if cell_text:
                # 单元格内也解析 **加粗** 等行内标记（旧实现整格按纯文本输出）
                _sh._add_runs_with_inline_format(p, cell_text)
            for run in p.runs:
                _sh._set_run_font(run, font_name, 10.5,
                              bold=True if ri == 0 else run.bold)
            if ri == 0:
                # ✅ R109：按 CT_TcPr 序列插入 w:shd（旧实现 append 到末尾 →
                #    与已写入的 w:vAlign 顺序颠倒，WPS 拒绝打开）
                _sh._set_cell_shading(cell, "B6B1D1")

    # ✅ 增强：表头行跨页重复（长表格翻页后仍能看到列名）
    if len(rows) > 1:
        tr_pr = table.rows[0]._tr.get_or_add_trPr()
        if tr_pr.find(qn("w:tblHeader")) is None:
            tbl_header = OxmlElement("w:tblHeader")
            tbl_header.set(qn("w:val"), "true")
            tr_pr.append(tbl_header)
    # ✅ 增强：列宽按各列最长内容自适应分配（总宽约 16cm），避免长文本被挤成多行
    _sh._assign_table_column_widths(table, rows, ncols)


def _assign_table_column_widths(table, rows: list[list[str]], ncols: int) -> None:
    """按各列最长内容（中文字符权重1、半角0.5）比例分配列宽，总宽约 16cm。

    同时声明固定布局（tblLayout=fixed）+ 表格总宽（tblW），否则 Word 会按内容
    重新折列宽，自适应列宽不生效。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm

    def _w(text: str) -> float:
        s = (text or "").replace("\n", " ")
        return sum(1.0 if ord(ch) > 0x2E80 else 0.5 for ch in s)

    weights = [0.0] * ncols
    for r in rows:
        for ci in range(ncols):
            weights[ci] = max(weights[ci], _w(r[ci] if ci < len(r) else ""))
    total = sum(weights) or 1.0
    total_cm = 16.0
    widths_cm = [max(1.2, total_cm * w / total) for w in weights]
    scale = total_cm / (sum(widths_cm) or 1.0)  # 归一化，避免浮点误差溢出页边距
    widths_cm = [w * scale for w in widths_cm]
    twips = [int(round(c * 567)) for c in widths_cm]

    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(sum(twips)))
    tbl_w.set(qn("w:type"), "dxa")
    layout = tbl_pr.find(qn("w:tblLayout"))
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        tbl_pr.append(layout)
    layout.set(qn("w:type"), "fixed")
    for row in table.rows:
        for ci, cell in enumerate(row.cells):
            cell.width = Cm(widths_cm[ci])


def _add_cover_info_table(doc, info: dict, font_name: str) -> None:
    """在封面插入项目信息表（两列：标签 / 值，整体居中）。

    仅当 info 含非空值时渲染；空值条目自动跳过，避免封面出现空白行。
    标签列右对齐加粗、值列左对齐，整体 14cm 宽、浅灰底纹。
    """
    from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm

    if not isinstance(info, dict):
        return
    pairs = [(str(k), str(v).strip()) for k, v in info.items() if str(v).strip()]
    if not pairs:
        return
    for _ in range(1):
        doc.add_paragraph()
    table = doc.add_table(rows=len(pairs), cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    table.autofit = False
    tbl_pr = table._tbl.tblPr
    layout = tbl_pr.find(qn("w:tblLayout"))
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        tbl_pr.append(layout)
    layout.set(qn("w:type"), "fixed")
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(int(14 * 567)))
    tbl_w.set(qn("w:type"), "dxa")
    for i, (k, v) in enumerate(pairs):
        c0, c1 = table.rows[i].cells
        c0.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        c1.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        c0.width = Cm(4)
        c1.width = Cm(10)
        p0 = c0.paragraphs[0]
        p0.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        _sh._set_run_font(_safe_add_run(p0, k), font_name, 12, bold=True)
        p1 = c1.paragraphs[0]
        p1.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _sh._set_run_font(_safe_add_run(p1, v), font_name, 12)
        # ✅ R109：同上，改走按序列插入的单一实现（并防重复 w:shd）
        _sh._set_cell_shading(c0, "F2F2F2")
        _sh._set_cell_shading(c1, "F2F2F2")




# ---------------------------------------------------------------------------
# ✅ 2026-10-11（引入《文档生成规则分析与软件标准建立》第四部分 4.2 导出标准）：
#    可选**版式预设**。既有默认（2.5cm 四边 / 行距 1.15）是历轮锁定的字节契约，
#    一字不动；预设仅在 config.layout_preset 显式点名时整体覆盖行距与四边距。
#    "engineering" 取通用 Word「工程公文」档：上下 2.54cm、左右 3.18cm、1.5 倍行距。
# ---------------------------------------------------------------------------
LAYOUT_PRESETS: dict[str, dict] = {
    "engineering": {
        "line_spacing": 1.5,
        "margins": {"top": 2.54, "bottom": 2.54, "left": 3.18, "right": 3.18},
    },
}


def _resolve_layout_preset(config: dict, base_opts: dict) -> dict:
    """按 config.layout_preset 覆盖版式字段（纯函数，未知/缺省键原样返回）。

    返回**新 dict**（base_opts 不被就地修改）；预设命中时只覆盖
    line_spacing 与 margins 两项 —— 字体/字号/标题样式等由用户显式配置继续生效。
    """
    name = str((config or {}).get("layout_preset", "") or "").strip().lower()
    preset = _sh.LAYOUT_PRESETS.get(name)
    if not preset:
        return dict(base_opts)
    out = dict(base_opts)
    out["line_spacing"] = max(1.0, min(float(preset["line_spacing"]), 3.0))
    _mg = dict(preset["margins"])
    # 用户在 config.margins 里显式给过的键保持原值（预设补缺，不强改细调）
    _user_margins = (config or {}).get("margins")
    if isinstance(_user_margins, dict):
        for _k, _v in _user_margins.items():
            try:
                if _v is not None and float(_v) > 0:
                    _mg[_k] = float(_v)
            except (TypeError, ValueError):
                continue
    out["margins"] = _mg
    return out


# ---------------------------------------------------------------------------# ---------------------------------------------------------------------------
# 专项施工方案法定前置表单（2026-10-02 · 第二十三轮）
# ---------------------------------------------------------------------------
# 依据（均为部文原文，不凭记忆编写）：
# · 住建部令第37号 第十一条：专项施工方案应当由施工单位技术负责人审核签字、
#   加盖单位公章，并由总监理工程师审查签字、加盖执业印章后方可实施；
#   危大工程实行分包并由分包单位编制的，应当由总承包单位技术负责人及分包
#   单位技术负责人**共同**审核签字并加盖单位公章。
# · 住建部令第37号 第十二条：专家应当从专家库中选取，符合专业要求且
#   **人数不得少于 5 名**；与本工程有利利害关系的人员不得以专家身份参加。
# · 住建部令第37号 第十三条：专家论证会后应当形成**论证报告**，对专项
#   施工方案提出**通过、修改后通过或者不通过**的一致意见，专家签字确认。
# · 建办质〔2018〕31号 三、参会人员五类；四、专家论证主要内容三项。
# · 建办质〔2018〕31号 二、第(九)项：计算书**及相关施工图纸** → 图纸附件清单。
#
# 红线：与正文同一口径 —— **不编造**人名、证书编号、单位名称与日期，
# 签字栏一律留空由各方手签（表单本就是待签状态，留空才是正确产物）。
# 空值行不渲染，避免交付文档出现「XXX：」这类空栏。


def _add_form_table(doc, rows: list[tuple[str, str]], font_name: str = "宋体",
                    col1_cm: float = 4.0, col2_cm: float = 10.0,
                    keep_empty: bool = False) -> None:
    """插入「标签 / 值」两列表单（审批表、论证报告表共用）。

    与 :func:`_add_cover_info_table` 同款固定布局（14cm 宽、灰底纹），
    区别是不强制换页 —— 供正文前的法定表单复用。

    ``keep_empty`` 区分两类行（⚠️ 缺了它会静默吞掉整张签字表）：
    · False（默认）：值列为空的行**跳过** —— 用于「工程名称 / 方案编号」
      这类抬头行，没填就不该在交付文档里留「工程名称：」的空栏；
    · True：值列为空也**保留** —— 用于签字栏。签字栏的「空」不是缺失，
      恰恰是正确产物（表单交付态即「待签」，由各方线下手签）；
      把它当空值过滤掉，等于把整张审批表 / 论证报告表的签字栏抹平，
      文档看起来"干净"却彻底失去法律效力。
    """
    from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm

    pairs = [(str(k), str(v or "").strip()) for k, v in rows
             if keep_empty or str(v or "").strip()]
    if not pairs:
        return
    table = doc.add_table(rows=len(pairs), cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    table.autofit = False
    tbl_pr = table._tbl.tblPr
    layout = tbl_pr.find(qn("w:tblLayout"))
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        tbl_pr.append(layout)
    layout.set(qn("w:type"), "fixed")
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(int(14 * 567)))
    tbl_w.set(qn("w:type"), "dxa")
    for i, (k, v) in enumerate(pairs):
        c0, c1 = table.rows[i].cells
        for cell in (c0, c1):
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            cell.width = Cm(col1_cm if cell is c0 else col2_cm)
            # ✅ R109：同上，改走按序列插入的单一实现（并防重复 w:shd）
            _sh._set_cell_shading(cell, "F2F2F2")
        p0 = c0.paragraphs[0]
        p0.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        _sh._set_run_font(_safe_add_run(p0, k), font_name, 10.5, bold=True)
        p1 = c1.paragraphs[0]
        p1.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _sh._add_runs_with_inline_format(p1, v)


def _add_prelim_page_title(doc, title: str, font_name: str,
                           basis: str = "") -> None:
    """前置页页题（居中加粗 + 编制依据注），并为其后留白。"""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(12)
    _sh._set_run_font(_safe_add_run(p, title), font_name, 16, bold=True)
    if basis:
        b = doc.add_paragraph()
        b.alignment = WD_ALIGN_PARAGRAPH.CENTER
        b.paragraph_format.space_after = Pt(10)
        _sh._set_run_font(_safe_add_run(b, basis), font_name, 9)


def _scheme_form_header(scheme: dict, cover_info: dict | None) -> list[tuple[str, str]]:
    """四张前置表共用的「工程 / 方案」抬头行（空值自动过滤）。"""
    info = cover_info if isinstance(cover_info, dict) else {}
    # ✅ 2026-10-11（文档质量报告 P0-B）：抬头行同样过模板残留清洗。
    #    残留串被置空后由 `_add_form_table` 的「空值行跳过」自动隐藏该行，
    #    表单不会因此变形；此处不做项目名兜底（表头应来自封面信息，不猜）。
    return [
        ("工程名称", _sh._sanitize_template_residue_title(info.get("工程名称", ""))),
        ("方案名称", _sh._sanitize_template_residue_title((scheme or {}).get("name", ""))),
        ("方案编号", info.get("方案编号", "")),
        ("编制单位", info.get("编制单位", "")),
    ]


def _add_compilation_note_page(doc, scheme: dict, cover_info: dict | None,
                               font_name: str) -> None:
    """编制说明（专项施工方案标准组成，说明编制背景与执行要求）。

    仅陈述可从方案自身与封面信息确定的内容；不从事实库取未经确认的数值，
    不出现「待补充」类占位（与正文「数据真实性红线」同款要求）。
    """
    info = cover_info if isinstance(cover_info, dict) else {}
    project_name = _sh._sanitize_template_residue_title(info.get("工程名称", "")).strip()
    scheme_name = _sh._sanitize_template_residue_title((scheme or {}).get("name", "")).strip()
    parts = [
        f"本方案为{project_name}的{scheme_name}，"
        f"由施工单位技术负责人组织工程技术人员编制，"
        f"经施工单位技术负责人审核签字并加盖单位公章、"
        f"总监理工程师审查签字后方可实施。"
        if project_name and scheme_name else
        "本方案由施工单位组织工程技术人员编制，"
        "经施工单位技术负责人审核签字并加盖单位公章、"
        "总监理工程师审查签字后方可实施。",
        "本方案依据国家、行业及地方现行法律法规、部门规章、"
        "强制性工程建设规范和本专业技术标准，并结合本工程设计文件、"
        "施工组织设计与现场实际条件编制，编制依据详见「编制依据」章节。",
        "本方案是指导本专项工程施工的作业文件，"
        "施工现场应严格执行；因设计变更、规划调整等原因确需调整的，"
        "修改后的专项施工方案应当重新履行审核（及论证）程序。",
        "本方案的技术参数与施工做法已结合现场条件核定，"
        "现场实际条件与本方案不一致时，应及时反馈编制单位核实，"
        "不得擅自修改后直接实施。",
    ]
    for text in parts:
        p = doc.add_paragraph()
        p.paragraph_format.first_line_indent = None
        _sh._add_runs_with_inline_format(p, text)


def _add_scheme_approval_table(doc, scheme: dict, cover_info: dict | None,
                               font_name: str) -> None:
    """专项施工方案审批表（住建部令第37号 第十一条）。

    签字栏留空 —— 表单交付态即「待签」，由各方线下手签，
    严禁预填人名 / 证书编号（AGENTS.md 数据真实性红线）。
    """
    from docx.shared import Pt

    _sh._add_prelim_page_title(doc, "专项施工方案审批表", font_name,
                           "依据《危险性较大的分部分项工程安全管理规定》"
                           "（住建部令第37号）第十一条")
    _sh._add_form_table(doc, _sh._scheme_form_header(scheme, cover_info), font_name)
    doc.add_paragraph()
    _sh._add_form_table(doc, [
        ("编制人（签字）", ""),
        ("编制日期", ""),
        ("施工单位技术负责人（审核签字）", ""),
        ("审核日期", ""),
        ("总承包单位技术负责人（分包工程共同审核）", ""),
        ("专业分包单位技术负责人（分包工程共同审核）", ""),
        ("总监理工程师（审查签字）", ""),
        ("审查日期", ""),
    ], font_name, col1_cm=6.0, col2_cm=8.0, keep_empty=True)
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    _sh._add_runs_with_inline_format(
        p, "注：本表签字并加盖单位公章（总监理工程师加盖执业印章）齐全后方可实施；"
           "实行分包的专项施工方案，由总承包单位与分包单位技术负责人共同审核签字。")


def _add_expert_review_form(doc, scheme: dict, cover_info: dict | None,
                            font_name: str) -> None:
    """专家论证会论证报告（住建部令第37号 第十二/十三条 + 建办质〔2018〕31号 三/四）。

    论证结论固定三选一（通过 / 修改后通过 / 不通过），专家栏按
    「人数不得少于 5 名」给出 5 行空栏；论证内容三项照 31 号文原文逐字列出。
    """
    from docx.shared import Pt

    _sh._add_prelim_page_title(doc, "专项施工方案专家论证报告", font_name,
                           "依据《危险性较大的分部分项工程安全管理规定》"
                           "（住建部令第37号）第十二条、第十三条，"
                           "建办质〔2018〕31号第三条、第四条")
    _sh._add_form_table(doc, _sh._scheme_form_header(scheme, cover_info), font_name)
    doc.add_paragraph()

    p = doc.add_paragraph()
    _sh._set_run_font(p.add_run("一、专家论证主要内容（建办质〔2018〕31号 第四条）"),
                  font_name, 12, bold=True)
    for item in (
        "（一）专项施工方案内容是否完整、可行；",
        "（二）专项施工方案计算书和验算依据、施工图是否符合有关标准规范；",
        "（三）专项施工方案是否满足现场实际情况，并能够确保施工安全。",
    ):
        q = doc.add_paragraph()
        _sh._add_runs_with_inline_format(q, item)

    doc.add_paragraph()
    p = doc.add_paragraph()
    _sh._set_run_font(p.add_run("二、论证结论（住建部令第37号 第十三条，三选一）"),
                  font_name, 12, bold=True)
    _sh._add_form_table(doc, [("论证结论", "□ 通过    □ 修改后通过    □ 不通过"),
                          ("论证意见（修改后通过时须写明具体修改内容）", "")],
                    font_name, col1_cm=6.0, col2_cm=8.0)

    doc.add_paragraph()
    p = doc.add_paragraph()
    _sh._set_run_font(p.add_run("三、参会人员（建办质〔2018〕31号 第三条）"),
                  font_name, 12, bold=True)
    _sh._add_form_table(doc, [
        ("专家（不得少于 5 名）", ""),
        ("建设单位项目负责人", ""),
        ("勘察、设计单位项目技术负责人及相关人员", ""),
        ("施工总承包 / 分包单位项目负责人、项目技术负责人、"
         "专项施工方案编制人员、项目专职安全生产管理人员", ""),
        ("监理单位项目总监理工程师及专业监理工程师", ""),
    ], font_name, col1_cm=6.0, col2_cm=8.0, keep_empty=True)

    doc.add_paragraph()
    p = doc.add_paragraph()
    _sh._set_run_font(p.add_run("四、专家签字确认（专家对论证报告负责并签字确认）"),
                  font_name, 12, bold=True)
    # 住建部令第37号 第十二条：专家人数不得少于 5 名 —— 固定 5 行空栏
    _sh._add_form_table(doc, [(f"专家 {i}（签字）", "") for i in range(1, 6)],
                    font_name, col1_cm=6.0, col2_cm=8.0, keep_empty=True)
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    _sh._add_runs_with_inline_format(
        p, "注：专家应当从地方人民政府住房城乡建设主管部门建立的专家库中选取，"
           "符合专业要求且人数不得少于 5 名；与本工程有利害关系的人员"
           "不得以专家身份参加专家论证会。结论为「修改后通过」的，"
           "应当按论证意见修改完善后重新履行审核程序后方可实施。")


def _add_drawing_appendix_page(doc, scheme: dict, font_name: str) -> None:
    """施工图纸附件清单（建办质〔2018〕31号 第(九)项「计算书及相关施工图纸」）。

    随文附图逐项登记；无图纸时给出编号栏 + 名称栏空表由现场填写，
    不预填图号（图号由导出器统一编排，正文不得自写 —— 与正文图号纪律一致）。
    """
    from docx.shared import Pt

    _sh._add_prelim_page_title(doc, "施工图纸附件清单", font_name,
                           "依据建办质〔2018〕31号 专项施工方案内容 第（九）项")
    _sh._add_form_table(doc, _sh._scheme_form_header(scheme, None), font_name)
    doc.add_paragraph()
    p = doc.add_paragraph()
    _sh._set_run_font(p.add_run("随本方案附以下施工图纸，图纸应与正文「计算书及相关施工图纸」"
                            "章节相互对应、编号连续："), font_name, 10.5)
    doc.add_paragraph()
    rows = [("图号", "图纸名称", "比例 / 说明")]
    rows += [(str(i), "", "") for i in range(1, 9)]
    _sh._add_table_from_markup(doc, [
        "| " + " | ".join(rows[0]) + " |",
        "| " + " | ".join(["---"] * 3) + " |",
        *["| " + " | ".join(r) + " |" for r in rows[1:]],
    ], font_name)
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    _sh._add_runs_with_inline_format(
        p, "注：图号由导出器统一编排并与正文图题连续；本清单仅登记图名与内容，"
           "不预填图号。危大工程涉及第三方监测的，监测点布置图一并列入本清单。")


def _set_margins(section, margins: dict) -> None:
    """设置页面边距（单位 cm，缺省 2.5）。"""
    from docx.shared import Cm

    if not isinstance(margins, dict):
        margins = {}

    def _cm(key, default):
        try:
            return Cm(float(margins.get(key, default)))
        except (TypeError, ValueError):
            return Cm(default)

    section.left_margin = _cm("left", 2.5)
    section.right_margin = _cm("right", 2.5)
    section.top_margin = _cm("top", 2.5)
    section.bottom_margin = _cm("bottom", 2.5)


# ---------------------------------------------------------------------------
# ✅ 2026-09-25 修复（P0 · 图表尺寸溢出页面 / 窄图被拉伸）
#    旧实现只按「PNG 像素 × 2.54 / 96dpi」估算物理宽度、且只用 16cm 宽度封顶，
#    **完全无视图片高度**。而本项目渲染器产出的是 2.5x 超采样栅格图
#    （后端 PIL 2.5x + 300 DPI 元数据 / 前端 canvas 2.5x），纵向流程图
#    （`flowchart TD` 十几个节点，专项方案里极常见）实测 700×4070 px：
#      · 旧算法 → 宽度封顶 16cm，高度 = 16 × 4070/700 = **93.03cm**
#        （python-docx 默认页面为 Letter，正文可用高度仅 22.94cm；A4 为 24.7cm）
#        → 单张图比页面高 3.8 倍，Word 里必然溢出/被裁切，成稿不可用；
#      · 新算法：优先用 PNG 自带 DPI 元数据（本项目渲染器统一写 300 DPI）换算
#        真实物理尺寸，再同时受「正文栏宽 16cm」与「单图最大高度 22cm」约束，
#        等比缩放 → 同一张图实测 3.78×22.00cm，完整落在页面内，图内文字仍保持
#        300 DPI 有效分辨率。
#    向后兼容：宽图（占实际图表 99%）两种算法的结果都被 16cm 宽度上限夹住，
#    产出尺寸逐字节一致；只有"旧算法会溢出页面"的极端窄高图行为发生变化。
_CHART_MAX_WIDTH_CM = 16.0
_CHART_MAX_HEIGHT_CM = 22.0
_CHART_DPI_FALLBACK = 96.0


def _read_image_dpi(info_dpi) -> float:
    """从 PIL 的 ``img.info["dpi"]`` 取值（容错：元组/标量/字符串/缺失均可用）。

    返回 0.0 表示"无有效 DPI"（由 `_fit_image_cm` 回退到 96dpi 口径）。
    """
    if not info_dpi:
        return 0.0
    if isinstance(info_dpi, (tuple, list)):
        if not info_dpi:
            return 0.0
        info_dpi = info_dpi[0]
    try:
        dpi = float(info_dpi)
    except (TypeError, ValueError):
        return 0.0
    # 1 或更小的"DPI"是脏数据（0/1 会让物理尺寸爆表），按缺失处理
    return dpi if dpi > 1.0 else 0.0


def _fit_image_cm(w_px, h_px, dpi: float | None = None,
                  max_w: float = _CHART_MAX_WIDTH_CM,
                  max_h: float = _CHART_MAX_HEIGHT_CM) -> tuple[float, float]:
    """按像素尺寸 + DPI 计算图片插入尺寸（cm），**同时**受宽/高上限约束。

    纯函数（不依赖 docx / PIL 打开文件），便于单测直接断言尺寸口径。

    Args:
        w_px / h_px: 图片像素尺寸；非法值 → 回退 ``(max_w, 0.0)``（按栏宽插入）。
        dpi: 图片 DPI 元数据；``None``/``<=1`` → 回退 `_CHART_DPI_FALLBACK`(96)。
        max_w / max_h: 正文栏宽、单图最大高度上限（cm）。

    Returns:
        ``(宽 cm, 高 cm)``，等比缩放后均已封顶，保留两位小数。
        ``h_cm`` 为 0.0 时表示"尺寸不可知"，调用方只传宽度即可（由 Word 按
        图片自身比例缩放）。
    """
    try:
        w_px = float(w_px)
        h_px = float(h_px)
    except (TypeError, ValueError):
        return max_w, 0.0
    if w_px <= 0 or h_px <= 0:
        return max_w, 0.0
    _dpi = _sh._read_image_dpi(dpi) or _sh._CHART_DPI_FALLBACK

    w_cm = w_px * 2.54 / _dpi
    h_cm = h_px * 2.54 / _dpi
    scale = 1.0
    if w_cm > max_w:
        scale = min(scale, max_w / w_cm)
    if h_cm > max_h:
        scale = min(scale, max_h / h_cm)
    return round(w_cm * scale, 2), round(h_cm * scale, 2)


def _figure_chapter_num(heading_gen) -> int:
    """图号 / 表号中的"章序号"（>0 恒成立）。

    ✅ P0 修复（2026-09-27 · 图号命名空间塌缩 / 图号虚跳）：
      旧实现三处（表格题注 / 图表 / AI 配图）都写死
      ``heading_gen.counters[0] or 1`` —— ``counters[0]`` 是 **L1 专属**计数器。
      当目录树里没有 L1（用户把一级目录删成只剩 L2 子树、或导入的方案本身就
      以 L2 为根）时它恒为 0，于是**全文所有章节的图表都被塞进 ch1 命名空间**，
      产出"图 1-1、图 1-2 …"跨章节连续编号 —— 图号等于失去了章节归属，
      正文里"图 1-3"可能被误读成第一章的第三张图（与图号虚跳同源）。

      修复：上溯到**当前实际存在的最高层级祖先**计数器。正常含 L1 的目录树
      行为与旧实现逐字一致（counters[0] 非零即返回它），仅在缺 L1 时才回退到
      L2 / L3 … 的计数器 —— 此时每个平级章节各自占据独立命名空间，符合
      "图号归属章节"的语义。
    """
    counters = getattr(heading_gen, "counters", None) or []
    for value in counters:
        try:
            n = int(value or 0)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return 1


#: python-docx（底层 OOXML ``a:blip``）真正支持的图片格式。
#: WEBP / AVIF / HEIC 等现代格式 PIL 能解码，但 docx 写入时会抛
#: ``UnrecognizedImageError`` —— 即"图号已占用但成稿里没有图"。
_DOCX_SAFE_IMAGE_FORMATS = frozenset({"PNG", "JPEG", "GIF", "BMP", "TIFF"})


def _image_format_supported(img_bytes) -> bool:
    """图片字节流是否为 docx 可安全插入的格式。

    ✅ BUG 修复（2026-09-27 · P1）：``_chart_ok`` 原先只验"是不是图片 + 长度 > 100"，
    不验"docx 能不能插入"。实测 WEBP 通过校验后，图号已被自增占用，
    ``doc.add_picture`` 却抛 ``UnrecognizedImageError`` 被 except 吞掉 ——
    成稿只剩一个孤立的「图 1-1 测试配图」图题，且因 ``ai_image_pending=0`` /
    ``render_stats["failed"]=0`` 而**被写入 export_cache**，服务恢复后永久命中坏产物。

    与 ``_chart_ok`` 同口径：判定必须发生在**占用图号之前**，否则又是虚跳编号。
    """
    try:
        from PIL import Image as PILImage
        with PILImage.open(_sh.BytesIO(img_bytes.getvalue())) as im:
            return (im.format or "").upper() in _sh._DOCX_SAFE_IMAGE_FORMATS
    except Exception:
        _sh.logger.debug("图片格式探测失败（按不支持处理，对应图表/配图将跳过）",
                     exc_info=True)
        return False


def _chart_ok(img_bytes) -> bool:
    """图表渲染结果是否可用（与 ``_add_inline_chart_from_bytes`` 判定口径完全一致）。

    单独抽出是因为"跳过 vs 占位"的决策必须发生在**占用图号之前**，
    否则会出现"图 1-2 却没有 1-1"的虚跳编号。
    """
    try:
        if not (img_bytes and len(img_bytes.getvalue()) > 100):
            return False
    except Exception:  # pragma: no cover - BytesIO 异常即视为渲染失败
        return False
    # 格式白名单：PIL 能解码 ≠ docx 能插入（见 _image_format_supported）
    return _sh._image_format_supported(img_bytes)


# ---------------------------------------------------------------------------
# ✅ BUG 修复（2026-10-06 · P0 逐块 fail-soft）：单个内容块渲染抛异常 → 整份导出 500
# ---------------------------------------------------------------------------
# 现象：write_section 的块分发里，**只有图表 / 配图两条分支**做了 fail-soft
#    （渲染失败即跳过并回退图号）。table / code / heading / list_item / quote /
#    普通段落这 6 类分支**完全没有保护**：任何一个块渲染抛异常都会冒到
#    `_build_docx_sync` 之外 → export_docx / export_pdf 返回 500，
#    用户**一分钱都拿不到**（连"降级产物"都没有）。
#    实测（monkeypatch `_add_table_from_markup` 抛异常）：PROPAGATED RuntimeError。
#    对交付类文档而言，"少一个表格的版式"远好于"整份方案导不出来"。
# 修法：把**整个块分发**包成一次事务 ——
#    ① 记录 body 元素快照 + 图号/表号/去重集合快照；
#    ② 异常时回滚文档元素与全部计数器（不留半截表格、不留已占图号）；
#    ③ 降级为**纯文本段落**保留内容（文字绝不丢），并累加
#       `block_render_failed` 供日志 + X-Fix-Stats 响应头观测。


def _block_plain_text(block: dict) -> str:
    """把内容块降级为可读的纯文本（表格/代码块保留原始行）。"""
    bt = block.get("type")
    if bt == "table":
        return "\n".join(str(x) for x in (block.get("lines") or []))
    if bt == "code":
        return "\n".join(str(x) for x in (block.get("lines") or []))
    for key in ("_fixed_text", "text"):
        v = block.get(key)
        if v:
            return str(v)
    return ""


def _degrade_block_to_text(doc, block: dict, font_name: str, font_size: float) -> bool:
    """块渲染失败后的降级出口：纯文本段落兜底 + 统计累加。返回是否成功降级。"""
    from docx.shared import Cm, Pt

    st = _sh._fix_stats()
    st["block_render_failed"] = st.get("block_render_failed", 0) + 1
    _sh.logger.warning("导出：内容块（type=%s）渲染失败，已降级为纯文本保留：%s",
                   block.get("type"), str(block.get("text") or "")[:40])
    txt = _sh._block_plain_text(block)
    if not txt.strip():
        return False
    try:
        p = doc.add_paragraph()
        _sh._add_runs_with_inline_format(p, txt)
        p.paragraph_format.first_line_indent = Cm(0)
        p.paragraph_format.space_after = Pt(3)
        return True
    except Exception as e:  # pragma: no cover - 连纯文本都失败则放弃该块
        _sh.logger.warning("导出：内容块降级为纯文本仍失败（该块内容缺失）: %s", e)
        return False


def _pop_orphan_lead_in(doc) -> bool:
    """删除文末"孤儿引导语"段落（图表被跳过时留下的"如下图所示："）。

    ✅ 2026-09-25 修复（图文逻辑连贯 · 容错降级的一致性）：
    图表在导出时被跳过（渲染失败 / 同代码去重 / 无可用代码 / AI 配图未生成）时，
    只 `continue` 会把它上方那句**专为引出该图而写**的引导语（"施工工艺流程
    如下图所示："）留在正文里 —— 成稿出现"见下图"却无图的悬空引用，
    评审一眼可见（与"渲染失败红字"同样属于交付硬伤）。
    现在跳过图表块时顺带回收该引导语：**仅当**文末最后一个 body 元素就是
    紧邻的段落、文本 ≤60 字、且以"如下图/见下图/如图所示"类引导语收尾时
    才删除，绝不触碰标题、列表、表格与正文中间的长句。

    Returns:
        是否执行了删除。
    """
    try:
        from docx.oxml.ns import qn
        paras = doc.paragraphs
        if not paras:
            return False
        last = paras[-1]
        body = doc.element.body
        # 文末唯一合法尾随元素是节属性 sectPr；倒数第一个"内容元素"必须就是
        # 这个段落，否则（如刚写入表格）不能删 —— 否则会误删表格前的段落。
        content_els = [el for el in body if el.tag != qn("w:sectPr")]
        if not content_els or content_els[-1] is not last._element:
            return False
        try:
            if last.style is not None and (last.style.name or "").startswith("Heading"):
                return False
        except Exception:
            _sh.logger.debug("孤儿引导语清理：段落样式读取失败（按非标题段落处理）",
                         exc_info=True)
        text = (last.text or "").strip()
        if not _sh._is_orphan_lead_in_text(text):
            return False
        last._element.getparent().remove(last._element)
        _sh.logger.info("图表被跳过：已同步移除其孤儿引导语「%s」", text[:30])
        return True
    except Exception as e:  # pragma: no cover - 版式清理失败不应阻断导出
        _sh.logger.debug("孤儿引导语清理失败（忽略）: %s", e)
        return False


class _DocxBodyRollback:
    """记录 ``doc.element.body`` 的子元素数，用于**失败时回滚本次新增元素**。

    ✅ BUG 修复（2026-10-06 · P0 图号重号，确定性复现）：
      ``_add_inline_chart_from_bytes`` / ``_add_illustration_from_bytes`` 的
      ``doc.add_picture()`` 与图题段落是**两次独立写入**。若图片已写入成功、
      随后图题步骤抛异常（字体名非法 / runs 被外部改写 / docx 内部异常），
      函数 ``return False`` 而**图片仍留在文档里**；调用方按契约回退图号
      （``figure_counters -= 1``）→ 下一张图重新拿到**同一个图号**，
      成稿出现**两张图都标注「图 1-1」**的重号。
      实测（``_exp_probe3``）：两张不同流程图，第一张图题阶段抛错 →
      ``pics=2 captions=['图 1-1 施工流程图', '图 1-1 施工流程图']``。
      修复：把「插入」变成**原子操作** —— 失败时删除本次新增的 body 子元素，
      使「返回 False」严格等价于「本次未向文档写入任何元素」，
      调用方的图号回退才是安全的。

    ⚠️ 实现要点（踩过一次坑）：**不能**用「记录 ``len(body)`` 再删尾部切片」
    的方式。``python-docx`` 的 ``add_picture`` / ``add_paragraph`` 把新元素
    插入在**节属性 ``w:sectPr`` 之前**，于是新元素的下标 < 快照下标，
    尾部切片只切得到 sectPr（还要跳过）→ 一个元素都删不掉，实测
    ``ins2(fail)=False`` 但 ``pics=1``、body 仍是 ``['p','sectPr']``。
    正确做法是**快照已有元素的对象身份**（lxml 在持有强引用期间对同一元素
    恒返回同一代理对象，故 ``is`` 比较可靠）。
    """

    __slots__ = ("_body", "_seen")

    def __init__(self, doc):
        self._body = doc.element.body
        # 持有强引用：lxml 只在无引用时重建代理对象，持有后 ``is`` 比较才稳定
        self._seen = list(self._body)

    def rollback(self) -> int:
        """删除本次调用新增的 body 子元素（跳过节属性 sectPr），返回删除数。"""
        from docx.oxml.ns import qn

        removed = 0
        try:
            for el in list(self._body):
                if any(el is s for s in self._seen):
                    continue  # 调用前就存在的元素，绝不动
                if el.tag == qn("w:sectPr"):
                    continue  # 节属性是文档骨架，绝不可删
                self._body.remove(el)
                removed += 1
        except Exception as e:  # pragma: no cover - 回滚失败不应掩盖原异常
            _sh.logger.warning("导出：图片插入失败后回滚文档元素失败（可能有残留元素）: %s", e)
        return removed


def _write_chart_placeholder_line(doc, caption_text: str, reason: str) -> None:
    """写红字占位行「[图 X-Y 图名 — 渲染失败/插入失败]」（仅占位模式）。"""
    from docx.shared import Cm, RGBColor

    p = doc.add_paragraph()
    p.paragraph_format.first_line_indent = Cm(0)
    run = _safe_add_run(p, _normalize_delivery_text(f"[{caption_text} — {reason}]"))
    run.font.color.rgb = RGBColor(0xFF, 0x00, 0x00)


def _insert_image_with_caption(doc, img_bytes, caption_text: str, *,
                               font_name: str = "宋体", font_size: float = 10.5,
                               log_label: str = "图片",
                               log_height_scale: bool = False) -> bool:
    """DOCX 位图插入的**唯一实现**：尺寸换算 → 居中插图 → 居中图题。

    ✅ 重构（2026-10-06 · D3）：`_add_inline_chart_from_bytes` 与
    `_add_illustration_from_bytes` 此前是两份**逐行平行**的实现（差异只有图题
    来源、可用性判据、占位文案三处），任何版式修复都必须改两遍 —— 本仓反复踩的
    「同一业务判据在 2~3 处各自实现」同构陷阱（v20 的 DPI 修复就改过两次）。
    现抽为单一实现，两个入口只保留**各自的差异**（可用性判据 + 占位形态），
    使「返回 False ⇒ 未写入任何元素」这一图号回退契约也只有一份。

    失败时用 ``_DocxBodyRollback`` 删除本次写入的元素，保证调用方可以安全回退图号。
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm

    _rb = _sh._DocxBodyRollback(doc)
    try:
        from PIL import Image as PILImage
        stream = _sh.BytesIO(img_bytes.getvalue())
        try:
            _im = PILImage.open(stream)
            _iw_px, _ih_px = _im.size
            _dpi = _sh._read_image_dpi(_im.info.get("dpi"))
        except Exception:
            _sh.logger.debug("%s 尺寸/DPI 读取失败（按默认 DPI 兜底）", log_label,
                         exc_info=True)
            _iw_px = _ih_px = 0
            _dpi = 0.0
        # ✅ 2026-09-25：按「栏宽 16cm × 高度 22cm」双上限等比缩放。
        #    旧实现只按 96dpi 估宽 + 仅宽度封顶，纵向长图（700×4070px）
        #    会被撑到 16×93cm，远超页面高度（Letter 22.94cm / A4 24.7cm）。
        _w_cm, _h_cm = _sh._fit_image_cm(_iw_px, _ih_px, _dpi)
        if log_height_scale and _h_cm >= _sh._CHART_MAX_HEIGHT_CM:
            _sh.logger.info(
                "%s 按高度上限缩放至 %.2f×%.2fcm（页面可用高度约 %.1fcm，"
                "原图 %dx%dpx dpi=%s）",
                log_label, _w_cm, _h_cm, _sh._CHART_MAX_HEIGHT_CM,
                _iw_px, _ih_px, _dpi or _sh._CHART_DPI_FALLBACK)
        stream.seek(0)
        doc.add_picture(stream, width=Cm(_w_cm))
        doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
        caption = doc.add_paragraph()
        caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
        caption.paragraph_format.first_line_indent = Cm(0)
        _sh._set_run_font(
            _safe_add_run(caption, _normalize_delivery_text(caption_text)),
            font_name, font_size, bold=True)
        return True
    except Exception as e:
        _sh.logger.warning("%s 插入失败: %s", log_label, e)
        _rb.rollback()
        return False


def _add_inline_chart_from_bytes(doc, chart_type: str, img_bytes, figure_num: str,
                                 default_title: str = "",
                                 font_name: str = "宋体", font_size: float = 10.5,
                                 placeholder: bool = False):
    """在 DOCX 中插入已渲染的图表图片（预渲染字节流）+ 规范图题。

    图题遵循规范「图 {章号}-{序号} {图名}」，居中加粗，字号取正文字号。
    ✅ BUG 修复：旧实现拼成「{图名} {章号}-{序号}」（如"施工进度计划 1-1"），
    图名与图号顺序颠倒，不符合图题惯例。
    ✅ 增强：图题字体/字号跟随导出配置（旧实现硬编码"宋体 / 10.5pt"，
    当用户把正文设为微软雅黑或四号时，图题会与正文明显不一致）。

    ✅ 图号虚跳收口（2026-10-03）：插入成功返回 ``True``、失败返回 ``False``，
    **不直接占用图号**（图号由调用方在成功后才递增）。失败处理：
    · ``placeholder=False``（默认）：**先回滚本次写入的文档元素**再返回
      ``False``，调用方回退图号 + 回收孤儿引导语，避免「占号却无图」的错号 /
      虚跳，**以及「图已插入却回退图号」导致的重号**（见 _DocxBodyRollback）；
    · ``placeholder=True``：仍写红字「图 X-Y — 渲染失败 / 插入失败」
      （图号已被调用方占用，属可见错误，用于排查哪张图没出来）；同样先回滚，
      保证「红字占位」与「真实图片」不同时出现在同一图号上。

    ✅ D3 重构（2026-10-06）：版式实现下沉到 ``_insert_image_with_caption``
    （与 AI 配图共用），本函数只保留图表侧**特有**的差异：字节量判据、
    占位红字形态、以及按图表类型的缩放日志。
    """
    caption_text = f"图 {figure_num} {default_title}".strip()
    # 可用性判据：字节缺失/过小。此处**不**重复校验格式白名单 —— 调用方
    # （write_section）已用 _chart_ok（= 长度 + 格式）先行判定，避免重复 IO；
    # 若调用方绕过 _chart_ok，格式问题会在 add_picture 抛错并被回滚。
    if not (img_bytes and len(img_bytes.getvalue()) > 100):
        if placeholder:
            _sh._write_chart_placeholder_line(doc, caption_text, "渲染失败")
        return False
    if _sh._insert_image_with_caption(
            doc, img_bytes, caption_text, font_name=font_name,
            font_size=font_size, log_label=f"图表({chart_type})",
            log_height_scale=True):
        return True
    if placeholder:
        _sh._write_chart_placeholder_line(doc, caption_text, "插入失败")
    return False


def _add_illustration_from_bytes(doc, img_bytes, figure_num: str, alt: str = "",
                                 font_name: str = "宋体", font_size: float = 10.5,
                                 placeholder: bool = False):
    """在 DOCX 中插入 AI 配图（文生图，已下载的位图字节流）+ 规范图题。

    与图表版的区别（**仅此两点**，版式实现共用 `_insert_image_with_caption`）：
    - 可用性判据多一道格式白名单（``_image_format_supported``）—— 配图来自
      远端 URL，格式不受控（WEBP/AVIF 会被 PIL 解码但 docx 拒收）；
    - 失败时**不写红字报错**（占位模式下只留图题）—— 交付文档里出现报错文本
      比少一张图更糟。

    ✅ 图号虚跳收口（2026-10-03）：插入成功返回 ``True``、失败返回 ``False``，
    不直接占用图号。失败（含格式不安全 / 文件损坏 / 图题阶段异常）时**先回滚
    本次写入的文档元素**再返回 ``False``，由调用方回退图号 + 回收孤儿引导语，
    与 chart 分支同口径；否则会出现「图片已在文档里、图号却回退」的重号。
    ``placeholder`` 形参保留仅为签名一致（AI 配图分支当前无占位模式）。
    """
    from docx.shared import Cm

    caption_text = f"图 {figure_num} {alt or '配图'}".strip()
    if not (img_bytes and len(img_bytes.getvalue()) > 100
            and _sh._image_format_supported(img_bytes)):
        return False
    if _sh._insert_image_with_caption(
            doc, img_bytes, caption_text, font_name=font_name,
            font_size=font_size, log_label=f"AI 配图({caption_text})"):
        return True
    if placeholder:
        p = doc.add_paragraph()
        p.paragraph_format.first_line_indent = Cm(0)
        _sh._set_run_font(_safe_add_run(p, caption_text), font_name, font_size, bold=True)
    return False
