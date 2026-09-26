"""正文生成标准（精准内容 / 模糊内容）— 文案、生效解析与确定性后处理校验。

F-CONTENT-STANDARD（2026-09-26），对齐《正文生成模块 —「生成标准」功能设计说明》v1.0，
并按本仓工程实际做以下收敛（详见 .trae/specs/content-generation-standard/spec.md）：

1. 生效值三级回落：任务级（override_section_standard=true 时强制）→
   章节级（sections.generation_standard，''=未覆盖）→ 方案级（schemes.generation_standard）
   → 默认 precise。
2. 校验口径 = 本章**实际注入的相关事实行**（调用方须先用 _filter_facts_rows 逐章精选），
   不要求一章引用全方案事实。
3. 校验结果为**咨询性报告**：只检测、只上报，绝不阻断生成、绝不静默改正文数值
   （矛盾修复走既有全文一致性 Agent，含快照可回滚）。

本模块全部为纯函数（无 DB / 无 AI / 无 IO），便于单测与生成后实时复算。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 常量与基础解析
# ---------------------------------------------------------------------------

#: 合法值域
PRECISE = "precise"
FUZZY = "fuzzy"
STANDARDS: tuple[str, str] = (PRECISE, FUZZY)
#: 默认标准：与既有「数据真实性红线」强约束口径等价（旧行为不变）
DEFAULT_STANDARD = PRECISE

#: 面向 UI / 报告的中文名
STANDARD_LABELS: dict[str, str] = {
    PRECISE: "精准内容",
    FUZZY: "模糊内容",
}


def normalize_standard(v) -> str | None:
    """把任意入参归一为合法标准值；非法（None / 空串 / 未知值）返回 None。"""
    if isinstance(v, str) and v in STANDARDS:
        return v
    return None


def resolve_effective_standard(
    *,
    task_standard=None,
    override: bool = False,
    section_standard: str = "",
    scheme_standard: str = "",
) -> str:
    """解析单章生效标准（后端唯一口径，见 spec FR-3）。

    - override=True 且任务值合法：任务值强制生效；
    - 否则章节级非空覆盖；
    - 否则方案级合法值；
    - 最终回落 precise。
    注意：override=False 时**忽略**任务值（即便传了合法值）——「按章节设置」语义。
    """
    if override:
        task = normalize_standard(task_standard)
        if task:
            return task
    sec = normalize_standard(section_standard)
    if sec:
        return sec
    sch = normalize_standard(scheme_standard)
    if sch:
        return sch
    return DEFAULT_STANDARD


# ---------------------------------------------------------------------------
# 提示词文案（system 段落 / user 块 / 续写提醒 —— 单点维护，禁止散落）
# ---------------------------------------------------------------------------

_SYSTEM_BLOCKS: dict[str, str] = {
    PRECISE: """## 生成标准：精准内容（本次生成强制执行）
1. 【全局事实变量】中已设定的数值、型号、人员姓名、工期、承诺口径，必须逐项**原样引用**：数值连同单位写出（如“基坑深度 12.5m”），型号写全（如“塔吊 QTZ80”），人员写姓名与岗位（如“项目经理张伟”），工期写具体天数（如“总工期 450 日历天”）。
2. 严禁模糊表述：“约/大约/左右/大概”等概数；“满足要求的/符合规范的/按设计确定/按合同要求/按业主要求/按相关规定”等空泛指代；“不少于/不超过/不小于/不大于”等限值（**事实本身即为限值时除外**，此时须照抄事实原话）。
3. 事实中未设定的参数，必须输出占位标识【待补充：参数名】并注明以设计/勘察/计算书为准，严禁编造数值、型号、人员姓名与文件编号。
4. 引用规范写全《名称》（编号）；条款号无把握时只写到规范层级，严禁杜撰条文号。""",
    FUZZY: """## 生成标准：模糊内容（本次生成按此执行）
1. 【全局事实变量】作为编写**方向参考**，不强制逐项引用：数值可使用范围值或限定性表述（如“约 12m”“不小于 12m”），型号可用限定性表述（如“满足要求的塔吊”），人员可只写岗位称谓，工期可写“按合同工期”。
2. 底线是**不得与全局事实矛盾**：不得出现与事实不同的具体数值、不同型号或相反承诺；事实给出具体值时，模糊表述只能围绕该值展开（数值偏差不得超过 ±10%）。
3. 事实中未设定的参数可用“按设计确定”“按规范要求”等表述，但不得编造具体数值、型号、人员姓名与文件编号。
4. 引用规范可只写《名称》，不强制条款号，但不得引用已废止版本、不得杜撰标准编号。""",
}

_USER_BLOCKS: dict[str, str] = {
    PRECISE: (
        "【本次生成标准：精准内容】\n"
        "- 上方【全局事实变量】中与本章相关的事实必须逐项原样引用（数值+单位、型号、姓名、工期、承诺口径），不得遗漏、不得改写数值。\n"
        "- 禁止“约/大约/左右/满足要求的/按设计确定/按合同要求”等模糊表述；事实本身即为限值（如“不低于 C30”）时照抄事实原话。\n"
        "- 事实未提供而正文必需的参数，写【待补充：参数名】，严禁编造。"
    ),
    FUZZY: (
        "【本次生成标准：模糊内容】\n"
        "- 上方【全局事实变量】仅作方向参考，不强制逐项引用；可使用范围值、限定性表述、岗位称谓与条件性承诺。\n"
        "- 底线：不得与事实中的具体数值（偏差不超过 ±10%）、型号、承诺口径相矛盾；事实未提供的数据不得编造具体值。"
    ),
}

_CONTINUE_HINTS: dict[str, str] = {
    PRECISE: "（续写仍执行【精准内容】标准：原样引用事实数值与单位，禁用“约/左右”等模糊表述；缺失参数用【待补充：参数名】）",
    FUZZY: "（续写仍执行【模糊内容】标准：可使用范围与限定表述，但不得与全局事实的数值、型号相矛盾）",
}


def build_system_block(standard: str) -> str:
    """system prompt 中插入的生成标准段落。"""
    return _SYSTEM_BLOCKS.get(normalize_standard(standard) or DEFAULT_STANDARD, "")


def build_user_block(standard: str) -> str:
    """user 消息追加的生成标准块（DB 定制 system 模板缺占位符时的兜底通道）。"""
    return _USER_BLOCKS.get(normalize_standard(standard) or DEFAULT_STANDARD, "")


#: 事实块引导语（**引用强度随模式变化**）
#:
#: ✅ 缺口修复（2026-09-26 · B1）：此前 sse_handlers 对两种模式**硬编码同一句**
#:    「与各变量相关的数据必须直接引用」—— 模糊模式下该措辞与 user 块的
#:    「方向参考」自相矛盾，模式差异在最关键的事实注入点上被抹平
#:    （选项看似生效、实际提示词只差一句 user 块）。
#:    PRECISE 的文案与历史硬编码版本**逐字一致**（默认精准 → 旧行为不变）。
_FACTS_HEADERS: dict[str, str] = {
    PRECISE: (
        "全局事实变量（唯一可信数据源：与各变量相关的数据必须直接引用，"
        "不得改写、推算或另取数值；未提供的数据严禁编造，"
        "按提示词“数据真实性红线”使用占位符或条件式表述）：\n"
    ),
    FUZZY: (
        "全局事实变量（唯一可信数据源：与本章节相关的数据以其为**编写方向参考**，"
        "允许合理概括、归纳与范围表述，但不得改写事实口径、"
        "不得出现与事实冲突的数值与型号；未提供的数据严禁编造，"
        "按提示词“数据真实性红线”使用占位符或条件式表述）：\n"
    ),
}


def build_facts_header(standard: str) -> str:
    """user 消息中事实块的引导语（末尾已含换行，直接拼接事实正文）。"""
    return _FACTS_HEADERS.get(normalize_standard(standard) or DEFAULT_STANDARD,
                               _FACTS_HEADERS[DEFAULT_STANDARD])


def build_continue_hint(standard: str) -> str:
    """续写后续轮 user 消息尾部的一行模式提醒。"""
    return _CONTINUE_HINTS.get(normalize_standard(standard) or DEFAULT_STANDARD, "")


# ---------------------------------------------------------------------------
# 后处理校验（确定性纯函数）
# ---------------------------------------------------------------------------

#: 围栏代码块（mermaid / chart-json / ai_image / 普通代码）整体剔除
_FENCE_RE = re.compile(r"```[\s\S]*?```")
#: 行内代码剔除
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

#: 【待补充：参数名】占位符（提示词要求的合规产物，只计数不算问题）
_PLACEHOLDER_RE = re.compile(r"【待补充[:：][^】]*】")

#: 数值（支持千分位与小数）
_NUMBER_RE = re.compile(r"(\d[\d,]{0,12}(?:\.\d+)?)")
#: 紧随数值的单位（长串在前，避免 m 抢先匹配 mm/m³）
_UNIT_RE = re.compile(
    r"\s{0,2}(日历天|工作日|立方米|平方米|m³|m²|m3|m2|MPa|kPa|kN·m|kN/m|kN"
    r"|毫米|厘米|千米|公分|mm|cm|km|kg|MPa"
    r"|台|套|层|人|天|次|根|块|个|米|吨|周|年|桩|孔|段|h|d|m|%|‰)")

#: 单位归一（同义单位合并；只合并确定等价者，工作日≠日历天不合并）
_UNIT_ALIASES: dict[str, str] = {
    "m": "m", "米": "m", "毫米": "mm", "厘米": "cm", "公分": "cm", "千米": "km",
    "mm": "mm", "cm": "cm", "km": "km",
    "m³": "m3", "m3": "m3", "立方米": "m3",
    "m²": "m2", "m2": "m2", "平方米": "m2",
    "天": "day", "日历天": "day", "d": "day",
    "工作日": "workday",
    "h": "hour", "年": "year", "周": "week",
    "吨": "t", "t": "t", "kg": "kg",
}

#: 型号 / 强度等级：C30（混凝土强度）或 2 个以上大写字母 + 2~4 位数字（QTZ80）
#: 规范编号（JGJ 120 / GB 50007 等）字母与数字间通常有空格/连字符，且前缀在黑名单内。
_MODEL_RE = re.compile(r"(?<![A-Za-z0-9])(C\d{2}|[A-Z]{2,6}\d{2,4}[A-Z0-9-]*)")
_NORM_PREFIXES = {"GB", "JGJ", "JG", "DB", "DBJ", "CJJ", "CECS", "TB", "TJ",
                  "GBJ", "JTJ", "SL", "DL", "QB", "HG", "SH", "SY"}

#: 模糊表述：前缀型（约/大约/大概 + 数字）。
#: 负向后顾屏蔽「合约12」这类「约」非模糊语素（约定/约束后跟数字时中间有别的字，本就不匹配）。
_HEDGE_PREFIX_RE = re.compile(r"(?<![合结])(约|大约|大概)\s*(\d[\d.]{0,12})")
#: 模糊表述：后缀型（数字 + 可选单位 + 左右/上下/前后，如“12.5 米左右”）
_HEDGE_SUFFIX_RE = re.compile(r"(\d[\d.]{0,12})[^0-9]{0,3}(左右|上下|前后)")
#: 模糊表述：空泛指代（无事实豁免例外）
_BARE_PHRASES = (
    "满足要求的", "符合规范的", "符合要求的", "按设计确定", "按设计要求确定",
    "按设计要求", "按合同要求", "按业主要求", "按相关规定", "按有关规定", "按规范要求",
)
#: 模糊表述：限值措辞（仅当事实本身使用同一措辞时豁免）
_LIMIT_PHRASES = (
    "不少于", "不超过", "不小于", "不大于", "不高于", "不低于",
    "不短于", "不长于", "不厚于", "不多于",
)

#: 事实标题关键词同现窗口（字符）
_COOCCUR_WINDOW = 30
#: 模糊模式数值容差
FUZZY_VALUE_TOLERANCE = 0.10
#: 关键词黑名单（过泛的词不参与同现判定，避免误关联）
_KEYWORD_STOP = {
    "工程", "施工", "方案", "项目", "要求", "技术", "章节", "内容", "情况",
    "系统", "措施", "管理", "标准", "规定", "一般", "相关", "主要", "其他",
    "参数", "数据", "信息", "说明", "备注", "名称", "类别", "分类",
}


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------

def strip_code_blocks(text: str) -> str:
    """剔除围栏代码块与行内代码（mermaid/chart-json/ai_image 内的数字不参与事实校验）。"""
    if not text:
        return ""
    text = _FENCE_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    return text


def _to_float(num: str) -> float | None:
    try:
        return float(num.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _norm_unit(u: str) -> str:
    return _UNIT_ALIASES.get((u or "").strip(), (u or "").strip())


def extract_number_tokens(text: str) -> list[dict]:
    """提取文本中的「数值 + 单位」token：[{value, raw, unit, span:(start,end)}]。

    无单位的纯数字不提取（正文中编号/日期/序号等噪声过多；事实矛盾检测要求同单位同现）。
    """
    tokens: list[dict] = []
    for m in _NUMBER_RE.finditer(text or ""):
        tail = _UNIT_RE.match(text, m.end())
        if not tail:
            continue
        value = _to_float(m.group(1))
        if value is None:
            continue
        unit_raw = tail.group(1)
        tokens.append({
            "value": value,
            "raw": m.group(1),
            "unit": _norm_unit(unit_raw),
            "span": (m.start(), tail.end()),
        })
    return tokens


def extract_model_tokens(text: str) -> list[dict]:
    """提取型号 / 强度等级 token（剔除规范编号）：[{token, family, span}]。"""
    out: list[dict] = []
    for m in _MODEL_RE.finditer(text or ""):
        token = m.group(1)
        family = re.match(r"[A-Z]{1,6}", token)
        fam = family.group(0) if family else ""
        if fam in _NORM_PREFIXES:
            continue
        out.append({"token": token, "family": fam, "span": m.span()})
    return out


def _title_keywords(title: str) -> list[str]:
    """从事实标题提取同现关键词。

    中文连续段除整体保留外，再切相邻二元组（如“塔吊型号”→塔吊/型号，
    “基坑深度”→基坑/坑深/深度），去停用词；英文段整体保留。
    """
    kws: list[str] = []
    for seg in re.findall(r"[一-鿿]{2,}|[A-Za-z][A-Za-z0-9]{1,}", title or ""):
        if seg not in _KEYWORD_STOP and re.search(r"[一-鿿]", seg):
            kws.append(seg)
            for i in range(len(seg) - 1):
                bi = seg[i:i + 2]
                if bi not in _KEYWORD_STOP:
                    kws.append(bi)
        elif seg not in _KEYWORD_STOP:
            kws.append(seg)
    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for kw in kws:
        if kw not in seen:
            seen.add(kw)
            uniq.append(kw)
    return uniq


def _co_located(content: str, pos: int, keywords: list[str]) -> bool:
    """content 中 pos 附近 ±窗口内是否出现任一事实标题关键词。"""
    if not keywords:
        return False
    lo, hi = max(0, pos - _COOCCUR_WINDOW), min(len(content), pos + _COOCCUR_WINDOW)
    window = content[lo:hi]
    return any(kw in window for kw in keywords)


def _excerpt(content: str, pos: int, radius: int = 14) -> str:
    """取问题位置附近的简短原文片段（供前端 Popover 展示）。"""
    s = max(0, pos - radius)
    e = min(len(content), pos + radius)
    frag = content[s:e].replace("\n", " ").strip()
    return f"…{frag}…" if frag else ""


# ---------------------------------------------------------------------------
# 校验主入口
# ---------------------------------------------------------------------------

def _fact_text(row) -> tuple[str, str]:
    """从事实行取 (title, content)。行兼容 3/4/5 元组；dict 行也容忍。"""
    if isinstance(row, dict):
        return str(row.get("title") or ""), str(row.get("content") or "")
    try:
        return str(row[1] or ""), str(row[2] or "")
    except (IndexError, TypeError):
        return "", ""


def standard_report(content: str, standard: str, fact_rows: list) -> dict:
    """对一章已生成正文执行生成标准校验，返回结构化报告（纯函数、不抛业务异常）。

    参数：
        content: 最终落库正文（Markdown）
        standard: precise / fuzzy（非法值按 precise 处理）
        fact_rows: 本章**实际注入**的相关事实行（5 元组 gt,title,content,conf,chapter）

    返回：
        {standard, passed, error_count, warning_count,
         issues:[{type,severity,message,excerpt}], stats:{...}}
    """
    std = normalize_standard(standard) or DEFAULT_STANDARD
    issues: list[dict] = []

    try:
        scanned = strip_code_blocks(content or "")

        # ---- 事实侧索引（数值/型号/标题关键词 + 豁免集合） ----
        fact_numbers: list[dict] = []    # {value,unit,title,kw}
        fact_models: list[dict] = []     # {token,family,title,kw}
        exempt_limits: set[str] = set()  # 事实本身使用的限值措辞
        exempt_hedges: set[tuple] = set()  # 事实本身使用的 (模糊词, 数值)
        exempt_bare: set[str] = set()   # 事实文本中出现的空泛指代（极端保守豁免）
        for row in fact_rows or []:
            title, ftext = _fact_text(row)
            joined = f"{title} {ftext}"
            kw = _title_keywords(title)
            for nt in extract_number_tokens(joined):
                fact_numbers.append({**nt, "title": title, "kw": kw})
            for mt in extract_model_tokens(joined):
                fact_models.append({**mt, "title": title, "kw": kw})
            for ph in _LIMIT_PHRASES:
                if ph in joined:
                    exempt_limits.add(ph)
            for hm in _HEDGE_PREFIX_RE.finditer(joined):
                val = _to_float(hm.group(2))
                if val is not None:
                    exempt_hedges.add((hm.group(1), val))
            for bp in _BARE_PHRASES:
                if bp in joined:
                    exempt_bare.add(bp)

        placeholder_count = len(_PLACEHOLDER_RE.findall(scanned))

        # ---- 正文侧 token ----
        body_numbers = extract_number_tokens(scanned)
        body_models = extract_model_tokens(scanned)

        # ============ 精准模式：模糊表述扫描 ============
        fuzzy_hits = 0
        if std == PRECISE:
            seen_spans: set[tuple[int, int]] = set()
            for hm in _HEDGE_PREFIX_RE.finditer(scanned):
                val = _to_float(hm.group(2))
                if hm.group(1) in ("约", "大约", "大概") and val is not None \
                        and (hm.group(1), val) in exempt_hedges:
                    continue  # 事实原话即“约 X”
                key = hm.span()
                if key in seen_spans:
                    continue
                seen_spans.add(key)
                fuzzy_hits += 1
                issues.append({
                    "type": "fuzzy_expression", "severity": "warning",
                    "message": f"精准模式禁用概数表述“{hm.group(1)}{hm.group(2)}”",
                    "excerpt": _excerpt(scanned, hm.start()),
                })
            for hm in _HEDGE_SUFFIX_RE.finditer(scanned):
                key = hm.span()
                if key in seen_spans:
                    continue
                seen_spans.add(key)
                fuzzy_hits += 1
                issues.append({
                    "type": "fuzzy_expression", "severity": "warning",
                    "message": f"精准模式禁用概数表述“{hm.group(1)}{hm.group(2)}”",
                    "excerpt": _excerpt(scanned, hm.start()),
                })
            for bp in _BARE_PHRASES:
                if bp in exempt_bare:
                    continue
                start = scanned.find(bp)
                if start >= 0:
                    fuzzy_hits += 1
                    issues.append({
                        "type": "fuzzy_expression", "severity": "warning",
                        "message": f"精准模式禁用空泛指代“{bp}”",
                        "excerpt": _excerpt(scanned, start),
                    })
            for lp in _LIMIT_PHRASES:
                if lp in exempt_limits:
                    continue  # 事实本身即限值（如“不低于 C30”），照抄合法
                start = scanned.find(lp)
                if start >= 0:
                    fuzzy_hits += 1
                    issues.append({
                        "type": "fuzzy_expression", "severity": "warning",
                        "message": f"精准模式禁用限值表述“{lp}”（事实未如此设定时）",
                        "excerpt": _excerpt(scanned, start),
                    })

        # ============ 数值：覆盖率（仅精准）/ 矛盾（两模式） ============
        # 按事实聚合：同一事实（title）的全部数值/型号 token
        fact_groups: dict[str, dict] = {}
        for fn in fact_numbers:
            g = fact_groups.setdefault(fn["title"], {"numbers": [], "models": [], "kw": fn["kw"]})
            g["numbers"].append(fn)
        for fm in fact_models:
            g = fact_groups.setdefault(fm["title"], {"numbers": [], "models": [], "kw": fm["kw"]})
            g["models"].append(fm)

        # 正文「数值+单位」与「型号」查找索引（原始值集合 + 同系列型号）
        body_value_units = {(bn["value"], bn["unit"]) for bn in body_numbers}
        body_model_by_family: dict[str, set[str]] = {}
        for bm in body_models:
            body_model_by_family.setdefault(bm["family"], set()).add(bm["token"])

        def _number_present(value: float, unit: str) -> bool:
            return (value, unit) in body_value_units

        for title, g in fact_groups.items():
            numbers = g["numbers"]
            models = g["models"]
            kw = g["kw"]

            # ---- 覆盖率（精准，warning）：事实的数值/型号在正文一个都找不到 ----
            if std == PRECISE and (numbers or models):
                num_hit = any(_number_present(n["value"], n["unit"]) for n in numbers)
                model_hit = any(m["token"] in body_model_by_family.get(m["family"], set())
                                for m in models)
                if not num_hit and not model_hit:
                    expected = "、".join(
                        [f"{n['value']:g}{n['unit']}" for n in numbers]
                        + [m["token"] for m in models])
                    issues.append({
                        "type": "fact_value_missing", "severity": "warning",
                        "message": f"本章相关事实「{title}」（{expected}）未在正文中引用",
                        "excerpt": "",
                    })

            # ---- 数值矛盾：正文中与事实同单位、且在事实标题语境附近的不同数值 ----
            for fn in numbers:
                for bn in body_numbers:
                    if bn["unit"] != fn["unit"]:
                        continue
                    if abs(bn["value"] - fn["value"]) < 1e-9:
                        continue
                    if not _co_located(scanned, bn["span"][0], kw):
                        continue
                    rel = abs(bn["value"] - fn["value"]) / fn["value"] if fn["value"] else 1.0
                    if std == FUZZY and rel <= FUZZY_VALUE_TOLERANCE:
                        continue  # 模糊模式 ±10% 内视为合理范围表述
                    issues.append({
                        "type": "value_conflict" if std == FUZZY else "value_mismatch",
                        "severity": "error",
                        "message": (
                            f"「{title}」数值与全局事实矛盾：事实 {fn['value']:g}{fn['unit']}，"
                            f"正文 {bn['value']:g}{bn['unit']}"
                            + (f"（偏差 {rel * 100:.0f}%，超出模糊模式 ±10% 容差）"
                               if std == FUZZY else "（精准模式须与事实完全一致）")),
                        "excerpt": _excerpt(scanned, bn["span"][0]),
                    })

            # ---- 型号冲突：同系列不同型号，且与事实标题语境同现（两模式均查） ----
            for fm in models:
                siblings = body_model_by_family.get(fm["family"], set())
                for token in siblings:
                    if token == fm["token"]:
                        continue
                    # 定位该冲突型号在正文中的位置做同现判定
                    pos = scanned.find(token)
                    if pos < 0 or not _co_located(scanned, pos, kw):
                        continue
                    issues.append({
                        "type": "model_conflict", "severity": "error",
                        "message": f"「{title}」型号与全局事实矛盾：事实 {fm['token']}，正文出现 {token}",
                        "excerpt": _excerpt(scanned, pos),
                    })

        # ---- 汇总（同类型同消息去重，保留首条带 excerpt 的命中） ----
        deduped: list[dict] = []
        seen_msgs: set[tuple] = set()
        for it in issues:
            key = (it["type"], it["message"])
            if key in seen_msgs:
                continue
            seen_msgs.add(key)
            deduped.append(it)
        issues = deduped
        errors = [i for i in issues if i["severity"] == "error"]
        warnings = [i for i in issues if i["severity"] == "warning"]
        return {
            "standard": std,
            "passed": len(issues) == 0,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "issues": issues,
            "stats": {
                "placeholders": placeholder_count,
                "fuzzy_hits": fuzzy_hits,
                "checked_facts": len(fact_groups),
                "fact_numbers": len(fact_numbers),
                "fact_models": len(fact_models),
            },
        }
    except Exception:
        # 兜底：校验器自身任何意外都不得影响生成结果（调用方另包一层 try/except）
        return _empty_report(std)


def _empty_report(standard: str) -> dict:
    """降级形态：校验不可用时的空报告（available 语义由调用方决定）。"""
    return {
        "standard": normalize_standard(standard) or DEFAULT_STANDARD,
        "passed": True,
        "error_count": 0,
        "warning_count": 0,
        "issues": [],
        "stats": {"placeholders": 0, "fuzzy_hits": 0, "checked_facts": 0,
                  "fact_numbers": 0, "fact_models": 0, "degraded": True},
    }
