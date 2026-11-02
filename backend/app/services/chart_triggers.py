"""图表触发词库与章节配图判定（程序侧唯一事实来源）。

来源
----
《报告_图表生成机制深度探索与全套技术方案》中「招投标方案平台」的触发机制
（词库 + 句式模式 + 行业术语 + 置信度元数据）。本仓图表**与正文一体生成**
（见 ``prompts/content.py``「图表同步生成规范」），故此处按本仓架构重构为
**纯函数、无 AI、无 DB** 的判定层，不引入独立编排器：

1. 词库是提示词「触发关键词映射表」的**唯一事实来源** ——
   ``prompts/content.py`` 的表格由 :func:`build_trigger_table_markdown` 生成，
   避免「提示词一份词表 / 程序一份词表」的反复漂移。
2. 判定结果只用于三类**软用途**：
   - 本章配图参考提示（注入正文生成的任务差异段，仅供参考）；
   - 本地模板兜底的触发判定（``chart_local_templates``，默认关闭）；
   - 观测日志。
3. **刻意不做**（沿用 R58 / R160 的裁定）：
   - 不读 ``chart_predictions`` 的陈旧结论做预编排；
   - 不硬阻断任何章节配图（最终仍由正文 AI 自主判定）；
   - 不落库、不决定图号、不改变配额。

命名说明：R160 已删除旧「预编排判型死代码簇」，其符号名被护栏测试禁止残留
（见 ``tests/test_r160_chart_doc_closeout_20261101.py``），故本模块使用
``CHART_TRIGGER_LEXICON`` / ``CHART_SENTENCE_SIGNALS`` / ``SCHEME_DOMAIN_TERMS``
等新名字。
"""
from __future__ import annotations

import re

__all__ = [
    "MIN_SECTION_WORDS",
    "CHART_TRIGGER_PRIORITY",
    "CHART_TRIGGER_LEXICON",
    "CHART_STRONG_SIGNALS",
    "CHART_SENTENCE_SIGNALS",
    "SCHEME_DOMAIN_TERMS",
    "CHART_TYPE_INFO",
    "build_trigger_table_markdown",
    "analyze_section_chart_need",
    "build_section_chart_hint",
]

#: 提示词「章节字数 ≤ 200 字不配图」的程序侧同口径阈值。
#: ``routers/_chart_pipeline.py`` 的 ``_CHART_MIN_WORDS`` 转发本常量（单一口径）。
MIN_SECTION_WORDS = 200

#: 判型优先级（命中多类时取信息密度最高的一种，与提示词优先级一致）。
CHART_TRIGGER_PRIORITY: tuple[str, ...] = (
    "gantt", "architecture", "flowchart", "labor", "layout", "comparison", "timeline",
)

CHART_TYPE_LABELS: dict[str, str] = {
    "flowchart": "流程图", "gantt": "甘特图", "architecture": "组织架构图",
    "labor": "劳动力配置图", "comparison": "对比图", "layout": "总平面布置图",
    "timeline": "时间轴",
}

#: 映射表第三列「图表承载的信息」（提示词表格用）。
CHART_TYPE_INFO: dict[str, str] = {
    "gantt": "各任务起止天数、持续时长与先后衔接",
    "flowchart": "工序先后、并行穿插、检验判定分支",
    "architecture": "层级结构、部门岗位设置、体系构成",
    "labor": "各施工阶段 × 各工种的人数矩阵（四合一图）",
    "comparison": "柱状对比 / 占比饼图 / 对比表格",
    "layout": "施工场地空间分区布局",
    "timeline": "按期罗列的关键事件",
}

#: 各类型触发词库（**提示词表格的唯一来源**）。
#: 顺序即表格展示顺序；同一类型内按语义重要性排列。
#: ⚠️ 语义隔离纪律（R160 护栏锁定）：「应急响应」只入 flowchart、
#:    「应急组织」只入 architecture；机械/材料资源词只入 comparison，
#:    不得进 labor（labor 是工种人数矩阵）。
CHART_TRIGGER_LEXICON: dict[str, tuple[str, ...]] = {
    "gantt": (
        "进度计划", "工期安排", "里程碑", "时间节点", "总体施工部署", "阶段划分",
        "工期控制", "流水施工", "工序穿插", "关键线路", "横道图", "施工进度",
        "总工期", "进度控制", "进度保障", "工期节点", "节点计划", "施工部署",
        "穿插施工", "工期计划",
    ),
    "flowchart": (
        "施工工序", "工艺流程", "部署顺序", "操作步骤", "工艺衔接", "施工流向",
        "检验流程", "验收程序", "加工制作", "审批流程", "应急响应流程", "拆除顺序",
        "材料进场检验", "工序流程", "施工流程", "作业流程", "处置流程",
        "施工顺序", "质量控制流程", "报验流程", "安全技术交底流程",
    ),
    "architecture": (
        "组织机构", "项目组织架构", "管理体系", "岗位配置", "责任划分", "任务分解",
        "WBS", "系统架构", "智慧工地", "质量保证体系", "安全保证体系",
        "应急组织机构", "应急救援体系", "岗位职责", "项目班子", "组织架构",
        "项目组织机构", "管理架构", "职责分工", "组织体系",
    ),
    "labor": (
        "劳动力配置", "人员配置", "人力计划", "工种分配", "劳动力投入", "用工需求",
        "施工人员动态管理", "劳动力动态曲线", "劳动力计划", "用工计划", "人员投入",
        "工种配置", "劳务配置", "劳动力曲线", "人员动态", "班组配置",
    ),
    "comparison": (
        "方案比选", "多方案对比", "风险分布", "成本占比", "资源配置比例", "投入对比",
        "机械设备配置", "材料需求", "物资需求", "比选", "对比分析", "成本构成",
        "费用构成", "占比分析", "方案对比", "资源配置", "投入比例", "指标对比",
        "优劣对比", "设备配置", "材料用量", "资源投入", "造价对比",
    ),
    "layout": (
        "总平面布置", "功能分区", "区域划分", "临建设施布置", "施工平面布置图",
        "施工分区", "平面布置", "施工总平面", "临建布置", "场地布置",
        "施工场地布置", "临时设施布置", "道路布置", "堆场布置", "加工区布置",
        "平面规划", "场地规划", "功能布置",
    ),
    "timeline": (
        "关键节点", "里程碑", "实施步骤节点", "时间轴", "时间节点", "节点控制",
        "验收节点", "关键里程碑", "时间线", "节点计划", "实施节点", "里程碑节点",
        "关键时间点", "阶段节点",
    ),
}

#: 强判据子集：命中即为高置信候选（标题命中一次约等于 5 分）。
CHART_STRONG_SIGNALS: dict[str, tuple[str, ...]] = {
    "gantt": ("进度计划", "工期安排", "横道图", "施工进度", "进度控制", "关键线路", "总工期"),
    "flowchart": ("施工工序", "工艺流程", "施工流程", "检验流程", "验收程序", "审批流程", "施工顺序"),
    "architecture": ("组织机构", "组织架构", "管理体系", "岗位配置", "项目班子", "WBS", "职责分工"),
    "labor": ("劳动力配置", "人员配置", "人力计划", "劳动力计划", "用工计划", "劳动力投入"),
    "comparison": ("方案比选", "方案对比", "成本构成", "占比分析", "对比分析", "造价对比"),
    "layout": ("总平面布置", "平面布置", "施工总平面", "场地布置", "施工分区", "功能分区"),
    "timeline": ("里程碑", "时间轴", "关键节点", "时间线"),
}

#: 句式信号：正文/描述出现特定句式时，说明该类型有数据支撑的可能。
#: 元素为 ``(正则, 图表类型)``；仅对标题/描述/正文前三段匹配（避免长文误报）。
CHART_SENTENCE_SIGNALS: tuple[tuple[str, str], ...] = (
    (r"横道图|网络计划|关键线路|总工期", "gantt"),
    (r"(?:工序|流程)(?:为|如下|包括|依次|按)", "flowchart"),
    (r"(?:部门|岗位)(?:设置|职责|分工|配置)", "architecture"),
    (r"各工种|工种(?:人数|配置|投入)|(?:人数|投入)(?:达|至|约)", "labor"),
    (r"(?:比选|对比|优于|劣于|得分|占比)", "comparison"),
    (r"总平面|临建|塔吊|堆场|作业区|加工区", "layout"),
    (r"里程碑|节点目标|时间轴|时间线", "timeline"),
)

#: 行业/方案类型术语：命中后给对应类型小幅加权（体现行业语料倾向）。
#: ⚠️ 命名须避开 R160 护栏的词根（该测试按**子串**扫描全 ``app/`` 树）。
SCHEME_DOMAIN_TERMS: dict[str, dict[str, tuple[str, ...]]] = {
    "装饰装修": {"labor": ("油漆工", "瓦工", "木工", "安装工", "装饰装修")},
    "市政工程": {"layout": ("围挡", "交通疏解", "管线", "作业面"), "timeline": ("节点工期", "通车节点")},
    "机电安装": {"labor": ("电工", "管道工", "焊工", "调试"), "flowchart": ("调试流程", "试运行")},
    "钢结构": {"flowchart": ("吊装", "焊接", "拼装"), "labor": ("焊工", "起重工")},
    "安全专项": {"architecture": ("应急组织机构", "领导小组"), "flowchart": ("应急响应", "处置流程")},
    "水利工程": {"layout": ("围堰", "导流", "施工区"), "gantt": ("汛期", "节点工期")},
}

#: 标题/描述命中即判定「不配图」的管理类章节（与提示词排除场景同口径）。
_EXCLUDED_SECTION_WORDS: tuple[str, ...] = (
    "项目概况", "工程概况", "编制说明", "编制依据", "管理目标",
    "质量方针", "安全方针", "安全教育培训", "文明施工管理", "工程简介", "方案概述",
)

#: 类型默认全方案上限（与 ``_chart_pipeline`` 同口径：3；ai_image 不在本判定内）。
_DEFAULT_TYPE_LIMIT = 3

#: 规范化图题建议（章节标题过短/不含类型词时使用）。
_TITLE_HINTS: dict[str, str] = {
    "gantt": "施工总进度计划",
    "flowchart": "施工工艺流程",
    "architecture": "项目管理组织机构图",
    "labor": "劳动力配置计划",
    "comparison": "方案比选对比",
    "layout": "施工总平面布置图",
    "timeline": "关键里程碑时间轴",
}

_SENTENCE_RE = tuple((re.compile(p), ct) for p, ct in CHART_SENTENCE_SIGNALS)
_CJK_SPLIT_RE = re.compile(r"[，。；、：！？\n\r]")
_SHORT_HEAD_RE = 3  # 只取正文前三段做句式匹配


def build_trigger_table_markdown() -> str:
    """生成提示词「触发关键词映射表」（**提示词表格唯一来源**）。

    Returns:
        三列 Markdown 表格文本（不含前后空行）；表格每行以 ``| {type} |`` 开头，
        供提示词与测试按类型定位。
    """
    lines = [
        "| 图表类型 | 触发关键词（标题/描述命中即候选） | 图表承载的信息 |",
        "|---|---|---|",
    ]
    for ct in CHART_TRIGGER_PRIORITY:
        kws = "、".join(CHART_TRIGGER_LEXICON[ct])
        lines.append(f"| {ct} | {kws} | {CHART_TYPE_INFO[ct]} |")
    return "\n".join(lines)


def _matched(text: str, ct: str) -> list[str]:
    s = text or ""
    return [kw for kw in CHART_TRIGGER_LEXICON[ct] if kw in s]


def _score_type(*, ct: str, title: str, description: str, content: str,
                scheme_type: str) -> tuple[float, list[str]]:
    """计算单一类型的加权得分与命中词（得分越高越可能该类型）。"""
    strong = CHART_STRONG_SIGNALS.get(ct, ())
    t_hits = _matched(title, ct)
    d_hits = _matched(description, ct)
    # 正文只取前 3 段（长文关键词堆砌会稀释判型）
    head = "".join(_CJK_SPLIT_RE.split(content or "")[:_SHORT_HEAD_RE])
    c_hits = _matched(head, ct)

    score = len(t_hits) * 3 + len(d_hits) * 2 + len(c_hits) * 0.5
    score += sum(2 for kw in t_hits if kw in strong)
    score += sum(1 for kw in d_hits if kw in strong)

    # 句式信号：标题/描述中各计一次
    for pattern, pct in _SENTENCE_RE:
        if pct != ct:
            continue
        if pattern.search(title or "") or pattern.search(description or ""):
            score += 1.5

    # 行业术语加权（最多 +2，避免行业词喧宾夺主）
    industry = SCHEME_DOMAIN_TERMS.get((scheme_type or "").strip(), {})
    terms = industry.get(ct, ())
    score += min(2.0, sum(1.0 for kw in terms if kw in title or kw in description))

    detail = ""
    if t_hits:
        detail = "标题命中「" + "、".join(t_hits[:3]) + "」"
    elif d_hits:
        detail = "描述命中「" + "、".join(d_hits[:3]) + "」"
    elif c_hits:
        detail = "正文出现「" + "、".join(c_hits[:3]) + "」"
    return score, ([detail] if detail else [])


def _title_hint(ct: str, section_title: str) -> str:
    t = (section_title or "").strip()
    if t and len(t) <= 24 and any(k in t for k in CHART_STRONG_SIGNALS.get(ct, ())):
        return t
    return _TITLE_HINTS.get(ct, CHART_TYPE_LABELS.get(ct, ct))


def analyze_section_chart_need(*, title: str = "", description: str = "",
                               content: str = "", scheme_type: str = "",
                               word_count: int | None = None,
                               used_counts: dict | None = None,
                               type_limits: dict | None = None,
                               min_words: int = MIN_SECTION_WORDS,
                               min_confidence: float = 0.70) -> dict:
    """判定一个章节是否需要配图、最可能配哪种（**纯函数**）。

    Args:
        title: 章节标题。
        description: 章节描述（目录里的任务说明，可为空）。
        content: 已生成的正文（可为空 —— 生成前预判时只给标题/描述）。
        scheme_type: 方案类型（用于行业术语加权）。
        word_count: 正文字数；``None`` 表示未知（不做字数排除）。
        used_counts: ``{chart_type: 已用数量}``，用于跳过已超上限的类型。
        type_limits: ``{chart_type: 上限}``；缺省 3。
        min_words: 「字数不足不配图」阈值，默认 :data:`MIN_SECTION_WORDS`。
        min_confidence: 判定 needed 的置信度阈值，默认 0.70（对齐外部平台）。

    Returns:
        ``{needed, chart_type, confidence, reason, title_hint, insert_after,
        source, candidates, excluded, excluded_by}``。
        ``source`` 恒为 ``"local"``（规则推断，区别于 AI 产出）。
    """
    title = (title or "").strip()
    description = (description or "").strip()
    content = content or ""
    limits = dict(type_limits or {})
    used = dict(used_counts or {})

    base = {
        "needed": False, "chart_type": "", "confidence": 0.0, "reason": "",
        "title_hint": "", "insert_after": "首段概述之后", "source": "local",
        "candidates": [], "excluded": False, "excluded_by": "",
    }

    excluded_by = next((w for w in _EXCLUDED_SECTION_WORDS
                        if w in title or w in description), "")
    if excluded_by:
        base.update(excluded=True, excluded_by=excluded_by,
                    reason=f"管理/背景类章节（命中「{excluded_by}」），不配图")
        return base

    if word_count is not None and int(word_count) < int(min_words):
        base.update(reason=f"章节字数 {int(word_count)} < {int(min_words)}，不配图")
        return base

    scored: list[dict] = []
    for ct in CHART_TRIGGER_PRIORITY:
        score, notes = _score_type(
            ct=ct, title=title, description=description,
            content=content, scheme_type=scheme_type)
        if score <= 0:
            continue
        limit = int(limits.get(ct, _DEFAULT_TYPE_LIMIT))
        if limit > 0 and int(used.get(ct, 0)) >= limit:
            notes.append(f"全方案已用满 {limit} 张，跳过")
            continue
        conf = round(min(0.95, 0.45 + 0.10 * score), 2)
        scored.append({
            "chart_type": ct, "score": round(score, 2), "confidence": conf,
            "reason": "；".join(notes),
        })

    if not scored:
        base["reason"] = "标题/描述未命中任何图表触发词"
        return base

    # 先按置信度，再按类型优先级（同分时 gantt > … > timeline）
    scored.sort(key=lambda c: (-c["confidence"],
                               CHART_TRIGGER_PRIORITY.index(c["chart_type"])))
    best = scored[0]
    base.update(
        needed=bool(best["confidence"] >= float(min_confidence)),
        chart_type=best["chart_type"],
        confidence=best["confidence"],
        reason=best["reason"] or "命中触发词",
        title_hint=_title_hint(best["chart_type"], title),
        candidates=scored[:3],
    )
    return base


def build_section_chart_hint(*, title: str, description: str = "",
                             scheme_type: str = "", used_counts: dict | None = None,
                             min_confidence: float = 0.70) -> str:
    """生成「本章配图参考」软提示片段（无命中时返回空串）。

    ⚠️ 措辞必须保留「仅供参考、最终由你判定」——R58 裁定否定了程序侧预编排，
    本提示只把词库命中事实告诉模型，不构成指令，也不改变配额与判型规则。
    """
    res = analyze_section_chart_need(
        title=title, description=description, scheme_type=scheme_type,
        used_counts=used_counts, min_confidence=min_confidence)
    if not res["needed"]:
        return ""
    label = CHART_TYPE_LABELS.get(res["chart_type"], res["chart_type"])
    return (
        "\n【本章配图参考（程序词库检测，仅供参考；是否配图、配哪种仍由你自主判定）】：\n"
        f"{res['reason']}，若有可量化数据支撑可考虑配一张「{label}」"
        f"（图题建议：{res['title_hint']}）；无数据依据则不配图。\n"
    )
