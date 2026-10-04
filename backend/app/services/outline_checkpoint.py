"""审核与预检检查点 → **目录生成**约束（目录侧唯一事实源，2026-10-02）。

定位
----
:mod:`app.services.content_checkpoint` 已把「可在正文侧预防的检查点」前置为正文
约束（system 硬约束 + 逐章必含要素 + 生成后自检 + 确定性自动修复）。但**目录生成
侧此前完全没有对应物**——而预检里有一整类问题**只能在目录阶段预防**：

- ``CMP-01~09``（内容完整性，block/high）：``preflight_engine.check_completeness``
  按 **章节标题关键词** 判定九大法定章节是否存在。标题里没有那些词 → 报
  「未找到与「工程概况」相关的章节」。
- ``CMP-09`` 追加条款 / ``TRC-03``：计算书、附图 / 节点详图相关章节缺失。
- ``SAF-06``：危大工程缺监测监控方案章节。

生产库实证（``preflight_runs``，scheme=d3c1a897…，装饰装修专项施工方案）
------------------------------------------------------------------------
``CMP-01/02/03/04/05/09`` 六条全部命中，其中 3 条 block。根因有二，都不是审核侧
误报，而是**目录侧从未被要求过**：

1. **判据分叉（第三份副本）**：``sse_handlers._DANGEROUS_REQUIRED_KEYWORDS``
   是九大章节关键词的**第三份手抄副本**，与 ``audit_rules`` 注册表的
   ``keywords`` 已实证分叉（目录侧认「工程概述 / 施工部署 / 组织机构 / 图纸」，
   审核侧**不认**）。于是目录写「工程概述」→ 目录侧程序化预检**放行** →
   预检报 ``CMP-01`` high。
2. **门控不对齐**：审核侧 ``check_completeness`` 对九章**无条件**检查；目录侧
   「必须包含九章」只写在提示词的危大分支里，且程序化覆盖预检整段被
   ``if (requirements or basis is not None)`` 门控 —— **非危大专项方案
   （本软件主力场景）目录侧从未被要求过九章**。

本模块的解法
------------
1. **必备章节 spec 全部从 ``audit_rules`` 注册表派生**（``rule.keywords`` 只作
   指针读取，本模块不重抄一份）；额外把旧目录侧关键词**并进来**（只增不减），
   使「目录侧判为覆盖」⊆「审核侧判为覆盖」，**分叉方向单一且只会收敛不会放宽**。
2. 提供 :func:`check_outline_chapters`，其标题命中判据与
   ``preflight_engine.check_completeness`` **逐字同谓词**（``kw in title``），
   且同样对**全层级标题**生效 —— 不是另写一套更宽松的匹配。
3. :func:`build_outline_checkpoint_block` 生成提示词段落，措辞面向生成侧，
   **明确告知「审核按这些关键词匹配标题」**，让模型主动把法定词写进标题。

设计红线（对齐 AGENTS.md 与 content_checkpoint）
------------------------------------------------
- **判据同源**：``rule_id`` / ``keywords`` 一律从 ``audit_rules`` 读，措辞不在
  本模块复制审核阈值；:func:`validate_outline_anchoring` 锁定锚点真实存在。
- **只增不减**：并入旧关键词后，目录侧判据集合 ⊇ 旧集合 → 已覆盖的目录**不会**
  因本模块被判为缺失（无回归），只会把「审核会报、目录侧却放行」的那部分**补上**。
- **不猜**：目录为空时返回「未覆盖」而非静默通过（宁可多提醒）。
- **纯函数、零 IO、零 AI、fail-soft**：异常一律降级为「不阻断 + WARNING」。
- **services 分层**：禁止 import routers；被 ``routers/sse_handlers.py`` 与
  ``services/ai/prompts/outline.py`` 共同消费。
"""
from __future__ import annotations

import logging

from app.services import preflight_engine as _pf
from app.services.audit_rules import get_rule
from app.services.content_checkpoint import (
    chapter_required_elements, is_hazardous_scheme,
)
from app.services.outline_quality import collect_titles
from app.services.scheme_classification import (
    NINE_CHAPTERS, is_hazardous_by_keywords,
)

logger = logging.getLogger("outline_checkpoint")



# ---------------------------------------------------------------------------
# 一、必备法定章节 spec（**全部从 audit_rules 注册表派生**）
# ---------------------------------------------------------------------------

#: 九大法定章节对应的审核规则号（住建部令第37号第十七条）。
#: 只作「规则号 → 章节」的**指针**，判据（keywords）从注册表实时读取。
_NINE_CHAPTER_RULE_IDS: tuple[str, ...] = tuple(f"CMP-{i:02d}" for i in range(1, 10))

#: 章节的**展示名**（写进提示词给模型看的名称）。
#:
#: ⚠️ 用**法定全称的简写**（与 ``sse_handlers._DANGEROUS_REQUIRED_KEYWORDS`` 的
#: 原始标签逐字一致），由 ``tests/test_classification_parity_20261001.py`` 锁定 ——
#: 换成 ``NINE_CHAPTERS`` 的全称会打破该既有契约（「人员分工」并不是「施工管理及
#: 作业人员配备和分工」的子串，不能用子串断言兜底）。
_CHAPTER_DISPLAY_NAMES: dict[str, str] = {
    "CMP-01": "工程概况",
    "CMP-02": "编制依据",
    "CMP-03": "施工计划",
    "CMP-04": "施工工艺技术",
    "CMP-05": "安全保证措施",
    "CMP-06": "人员分工",
    "CMP-07": "验收要求",
    "CMP-08": "应急处置措施",
    "CMP-09": "计算书及相关图纸",
    "SAF-06": "监测方案",
    "TRC-03": "附图及节点详图",
}

#: 旧目录侧手抄关键词（``sse_handlers._DANGEROUS_REQUIRED_KEYWORDS`` 原样迁入）。
#:
#: ⚠️ **这些词一律不参与判定**，仅作「历史分叉」记录供护栏断言。理由（实测）：
#: 目录侧认它们而审核侧不认 → 正是「目录侧放行、预检照报」的根因。若并入判据
#: 只会让分叉**继续存在**（并集方向是错的：并入后目录侧判据 ⊇ 审核侧判据）。
#: 正确方向是让目录侧判据**完全等于**审核侧判据。
#:
#: 实测分叉（``tests/test_outline_checkpoint_20261002.py`` 锁定）：
#: 「工程概述」/「施工部署」/「组织机构」/「图纸」目录侧认、审核侧不认；
#: 「周边环境」/「工程基本」/「材料计划」/「人员配备」/「附图」审核侧认、目录侧不认。
_LEGACY_OUTLINE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "CMP-01": ("工程概况", "工程概述"),
    "CMP-02": ("编制依据",),
    "CMP-03": ("施工计划", "施工部署"),
    "CMP-04": ("施工工艺", "工艺技术"),
    "CMP-05": ("安全保证", "安全保障"),
    "CMP-06": ("人员分工", "组织机构"),
    "CMP-07": ("验收",),
    "CMP-08": ("应急",),
    "CMP-09": ("计算书", "图纸"),
    "SAF-06": ("监测",),
    "TRC-03": ("附图", "详图", "平面布置", "节点图", "图纸"),
}

#: 预检引擎**实际判定谓词**（``preflight_engine`` 模块级常量）。
#:
#: 为什么必须并进来：``check_safety`` / ``check_traceability`` 的 SAF-03~06、
#: TRC-03 判定读的是**引擎自己的常量**，不是 ``audit_rules`` 的 ``keywords``。
#: 只读注册表会漏掉引擎真正认的词（如「领导小组」「器材」「储备」「沉降」）→
#: 目录侧判缺失而预检判通过。故此处以**引擎常量为准**，注册表作为补充。
#:
#: 2026-10-02 已把 ``audit_rules`` 的 keywords 按引擎谓词取并集补齐（只增不减），
#: 两表此后一致；本映射保留是为了**不依赖那次补齐也正确**（纵深防御：
#: 日后有人改动注册表 keywords，目录侧仍与引擎一致）。
#: ⚠️ **只登记「引擎用另一套常量判定」的规则**。
#:
#: ``CMP-01~09`` **不得**登记：``check_completeness`` 读的正是注册表
#: ``rule.keywords``，若再并入 ``CALC_TITLE_KEYWORDS``（含「承载力计算 /
#: 稳定性验算 / 安全系数」等 CMP-09 从不用于标题匹配的词）会让目录侧判据
#: **宽于**审核侧 → 目录侧报「缺计算书」而预检不报（实测 4000 组穷举中
#: 1834 组如此）。这正是本轮要消灭的那类分叉，只是方向相反。
_ENGINE_PREDICATE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "SAF-03": _pf.SAF03_TITLE_KEYWORDS,
    "SAF-04": _pf.SAF04_TITLE_KEYWORDS,
    "SAF-05": _pf.SAF05_TITLE_KEYWORDS,
    "SAF-06": _pf.SAF06_MONITOR_TITLE_KEYWORDS,
    "TRC-03": _pf.TRC03_TITLE_KEYWORDS,
}

#: 危大工程**结构性**必备章节（= 九大法定章节 + 「监测方案」）。
#:
#: 这是 ``sse_handlers._DANGEROUS_REQUIRED_KEYWORDS`` 的**原始契约**（10 章、顺序
#: 固定、标签为法定全称的**简写**），由 ``tests/test_classification_parity_20261001.py``
#: 锁定。本轮**保持它逐字不变**（只把关键词换成审核侧同源词），
#: 新增的九章覆盖检查与它**并存互补**、不替换。
_HAZARD_STRUCTURAL_RULE_IDS: tuple[str, ...] = _NINE_CHAPTER_RULE_IDS + ("SAF-06",)

#: 覆盖检查的**附加**章节（非危大也要求，与预检 ``check_traceability`` 的
#: TRC-03 同门控 —— 该规则无危大门控）。
_EXTRA_REQUIRED_RULE_IDS: tuple[str, ...] = ("TRC-03",)


#: 九大法定章节：**任何**专项方案都必须具备（审核侧 ``check_completeness``
#: 对 CMP-01~09 无条件检查，故生成侧也不设门控 —— 这是本轮修的门控不对齐）。
_ALWAYS_REQUIRED_RULE_IDS: tuple[str, ...] = _NINE_CHAPTER_RULE_IDS

#: 全部必备规则号（九章 + 附加），顺序稳定。
_ALL_REQUIRED_RULE_IDS: tuple[str, ...] = _NINE_CHAPTER_RULE_IDS + _EXTRA_REQUIRED_RULE_IDS

#: 标题关键词并集缓存：``{rule_id: (关键词...)}``。
#: 审核侧关键词在前（旧目录侧关键词在后）—— 提示词展示顺序即匹配优先级。
_KEYWORD_CACHE: dict[str, tuple[str, ...]] = {}


def _keywords_of(rule_id: str) -> tuple[str, ...]:
    """某必备章节的标题关键词 = **审核侧实际谓词**（引擎常量 + 注册表并集）。

    刻意**不并入** ``_LEGACY_OUTLINE_KEYWORDS``：那些词目录侧认、审核侧不认，
    并进来等于把「目录侧放行、预检照报」的分叉固化下来。并集方向必须是
    「目录侧判据 ⊇ 审核侧判据」的反向 —— 即**完全等于或略宽于**审核侧，
    这样「目录侧判覆盖」才蕴含「预检也判覆盖」。

    注册表里的规则若两侧都没有 ``keywords``（纯语义规则）返回空元组 ——
    调用方据此判定「该规则无法用标题关键词预防」，不参与标题覆盖检查。
    """
    cached = _KEYWORD_CACHE.get(rule_id)
    if cached is not None:
        return cached
    rule = get_rule(rule_id)
    sources: list[tuple[str, ...]] = []
    if rule is not None and rule.keywords:
        sources.append(tuple(rule.keywords))
    engine_kw = _ENGINE_PREDICATE_KEYWORDS.get(rule_id)
    if engine_kw:
        sources.append(tuple(engine_kw))
    merged: list[str] = []
    for group in sources:
        for kw in group:
            k = (kw or "").strip()
            if k and k not in merged:
                merged.append(k)
    out = tuple(merged)
    _KEYWORD_CACHE[rule_id] = out
    return out


def needs_monitor_chapter(scheme_name: str = "", scheme_type: str = "") -> bool:
    """本方案是否须有监测监控方案章节（SAF-06）。

    **判据与 ``preflight_engine.check_safety`` 逐字同源**：方案命中的专业类别
    与 ``preflight_engine.MONITOR_CATEGORIES``（基坑 / 模板 / 起重机械 / 脚手架）
    有交集才要求。刻意不做「危大即要求」的粗判 —— 危大六大类里拆除、幕墙、
    预应力等类别预检并不报 SAF-06，目录侧若一并要求就是**假缺项**。
    """
    try:
        from app.services.standards_registry import match_categories
        cats = set(match_categories(scheme_name, scheme_type))
        return bool(cats & _pf.MONITOR_CATEGORIES)
    except Exception:  # pragma: no cover - 纯派生，异常时保守要求
        logger.debug("监测章节门控判定失败（降级为要求）", exc_info=True)
        return True


def _spec_of(rule_id: str) -> dict:
    """单个必备章节的 spec（名称 / 关键词 / 严重度），供两处装配复用。"""
    rule = get_rule(rule_id)
    return {
        "rule_id": rule_id,
        "name": _CHAPTER_DISPLAY_NAMES.get(
            rule_id, rule.title if rule else rule_id),
        "keywords": _keywords_of(rule_id),
        "severity": rule.severity if rule is not None else "medium",
        "hazard_only": rule_id in _HAZARD_STRUCTURAL_RULE_IDS
                      and rule_id not in _ALWAYS_REQUIRED_RULE_IDS,
    }


def required_chapter_specs(
        is_hazardous: bool = False, *,
        need_monitor: bool | None = None) -> list[dict]:
    """必备章节 spec 列表（提示词渲染与程序化自检共用同一份）。

    组成（顺序固定）：
      ``CMP-01~09``（**任何**专项方案都要有 —— 对齐预检的无条件检查）
      → ``SAF-06`` 监测方案（仅危大**且**属需监测类别时插入在第 9 位后）
      → ``TRC-03`` 附图及节点详图（无危大门控，对齐 ``check_traceability``）

    Args:
        is_hazardous: 是否危大工程（SAF-06 的第一层门控）。
        need_monitor: SAF-06 的**类别门控**（需监测的危大类别）。
            None = 按 ``is_hazardous`` 粗判（危大即要求）；
            True/False = 调用方按方案类别显式指定。
            ⚠️ 必须与 ``preflight_engine.check_safety`` 同门控，否则会出现
            「目录侧报缺监测章节、预检却不报」（实测：装饰装修类危大方案
            预检门控为 ``cats & {基坑,模板,起重机械,脚手架}``，不报 SAF-06）。

    Returns:
        每项 ``{rule_id, name, keywords, severity, hazard_only}``；``keywords``
        为空表示该规则无标题关键词（不参与标题覆盖检查）。
    """
    if need_monitor is None:
        need_monitor = bool(is_hazardous)
    specs: list[dict] = [_spec_of(rid)
                         for rid in _ALWAYS_REQUIRED_RULE_IDS
                         + _EXTRA_REQUIRED_RULE_IDS]
    if is_hazardous and need_monitor:
        specs.insert(len(_ALWAYS_REQUIRED_RULE_IDS), _spec_of("SAF-06"))
    return specs


def dangerous_required_keywords() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """危大 10 章结构性必备章节的 ``((标签, 关键词), ...)``（**唯一出口**）。

    供 ``sse_handlers._DANGEROUS_REQUIRED_KEYWORDS`` 派生（该名字是历史兼容
    别名，不再是独立副本）。**标签与顺序与原表逐字一致**：
    九大法定章节（简写）+ 「监测方案」—— 由
    ``tests/test_classification_parity_20261001.py`` 锁定，不得改动。
    """
    return tuple((_CHAPTER_DISPLAY_NAMES[rid], _keywords_of(rid))
                 for rid in _HAZARD_STRUCTURAL_RULE_IDS)


# ---------------------------------------------------------------------------
# 二、程序化自检：目录是否覆盖必备法定章节
# ---------------------------------------------------------------------------

def _matched_titles(titles: list[str], keywords: tuple[str, ...]) -> list[str]:
    """标题命中判定 —— **与 preflight_engine.check_completeness 逐字同谓词**。

    刻意复用 ``kw in title``（子串包含）而非归一化精确匹配：审核侧就是这么判的，
    目录侧若用更严的判据会出现「目录侧判缺失、审核侧判通过」的反向分叉。
    """
    return [t for t in titles if any(kw in t for kw in keywords)]


def check_outline_chapters(outline: list, *, is_hazardous: bool = False,
                          need_monitor: bool | None = None) -> list[dict]:
    """检查目录是否覆盖必备法定章节，返回 findings（rule_id 与预检同词表）。

    与 ``preflight_engine.check_completeness`` 的差异（**刻意**）：

    - 审核侧在「章节存在但正文为空」时也报 —— 那是**正文侧**的判定，目录阶段
      尚无正文，故本函数只判「**章节是否存在**」，不重复报正文为空；
    - 审核侧 CMP-09 追加「有计算书章节但无计算过程」，同样是正文侧判定。

    因此本函数是审核侧 CMP 标题存在性检查的**子集**：用它做生成侧拦截不会
    漏放行审核侧会报的问题，也不会提前误报审核侧不会报的问题。

    零 IO、零 AI、fail-soft：任何异常返回已收集结论。
    """
    findings: list[dict] = []
    try:
        titles = [str(e.get("title") or "").strip()
                  for e in collect_titles(outline or [])
                  if str(e.get("title") or "").strip()]
        for spec in required_chapter_specs(is_hazardous, need_monitor=need_monitor):
            kws = spec["keywords"]
            if not kws:
                # 无标题关键词的规则（如纯语义判定）无法在目录阶段预防 → 跳过，
                # 绝不因为「判不了」就假报缺失。
                continue
            if _matched_titles(titles, kws):
                continue
            findings.append({
                "rule_id": spec["rule_id"],
                "checkpoint": f"目录缺少「{spec['name']}」章节",
                # ✅ 供 outline_coverage_missing / 前端按名消费：不要从 checkpoint
                # 字符串里反解名称（改一次措辞就断链，AGENTS.md §5.14 同类教训）。
                "name": spec["name"],
                "severity": "high" if spec["severity"] in ("block", "high") else "medium",
                "source": "outline_selfcheck",
                "message": f"目录中未找到与「{spec['name']}」相关的章节"
                           f"（审核按标题关键词匹配：{'/'.join(kws[:4])}）",
                "suggestion": f"增设「{spec['name']}」一级章节，标题中包含"
                              f"{' 或 '.join(kws[:3])}等关键词",
            })
    except Exception as exc:  # fail-soft：自检异常不阻断目录生成
        logger.warning("目录检查点自检异常（忽略）: %s", exc, exc_info=True)
    return findings


def outline_coverage_missing(outline: list, *, is_hazardous: bool = False,
                            need_monitor: bool | None = None) -> list[str]:
    """缺失的必备章节名列表（供外科式补齐链路复用，返回去重保序的展示名）。"""
    missing: list[str] = []
    for f in check_outline_chapters(outline, is_hazardous=is_hazardous,
                                    need_monitor=need_monitor):
        name = (f.get("name") or "").strip()
        if name and name not in missing:
            missing.append(name)
    return missing


# ---------------------------------------------------------------------------
# 三、提示词段落（system 注入）
# ---------------------------------------------------------------------------

_OUTLINE_CHECKPOINT_HEADER = (
    "\n\n## 审核检查点前置要求（硬性，与预检/审核同一规则词表）\n"
    "本目录生成后会立即进入「正文生成 → 审核与预检」；下列检查点在**编排目录时**"
    "就必须满足，**优先于上文其它要求中与之冲突的任何表述**，不要指望事后修复：\n"
)


def build_outline_checkpoint_block(
        scheme_name: str = "", scheme_type: str = "",
        is_hazardous: bool | None = None,
        need_monitor: bool | None = None) -> str:
    """目录生成 system 级「检查点 → 生成约束」段落。

    与 :func:`content_checkpoint.build_content_system_checkpoint_block` 的关系：
    本段只讲**目录结构层面**的检查点（章节齐备、标题关键词），措辞层面的通用
    约束仍由正文侧那一段负责 —— 两段不重叠、不互相替代。

    措辞**明确告知「审核按这些关键词匹配标题」**：这是让模型主动把法定词写进
    标题的唯一有效手段（只说「必须有工程概况章」，模型会写成「工程概述」，
    而审核侧不认这个词 —— 生产库实证的 CMP-01 high 正是这么来的）。

    Args:
        scheme_name / scheme_type: 用于反查危大与监测门控（可为空）。
        is_hazardous: None = 按方案名自动判定；True/False = 调用方显式指定。
        need_monitor: None = 按方案类别自动判定（见 :func:`needs_monitor_chapter`）。

    Returns:
        以换行开头的段落文本；必备章节为空时返回空串（不注入，零干扰）。
    """
    if is_hazardous is None:
        is_hazardous = is_hazardous_scheme(scheme_name, scheme_type)
    if need_monitor is None:
        need_monitor = needs_monitor_chapter(scheme_name, scheme_type)
    specs = [s for s in required_chapter_specs(
        is_hazardous, need_monitor=need_monitor) if s["keywords"]]
    if not specs:
        return ""
    lines = [_OUTLINE_CHECKPOINT_HEADER]
    lines.append(
        "1. **必备法定章节齐备**：下列每一项都必须有**独立的一级章节**，"
        "不得合并、不得用二级小节代替（缺一项即被预检判为内容不完整）：")
    for spec in specs:
        kw_txt = " / ".join(spec["keywords"][:4])
        lines.append(f"   - {spec['name']}（{spec['rule_id']}）——标题须含：{kw_txt}")
    lines.append(
        "2. **标题必须带法定关键词**：审核预检按**标题关键词**匹配判定上述章节"
        "是否存在，标题里没有上列关键词就等于没有该章。允许在法定词后补充本工程"
        "特征（如「工程概况与基坑周边环境」），但**不得改写掉法定词本身**"
        "（写「工程概述」不算「工程概况」，会被判缺失）。")
    lines.append(
        "3. **不得出现无关章节**：不得凭空添加与本专项方案无关的章节"
        "（如非本方案对象的装饰 / 机电 / 土建章节），也不得把投标场景内容"
        "（招标文件、评分办法、商务报价）编成章节。")
    if need_monitor:
        lines.append(
            "4. **监测方案附加要求**：本工程属需监测的危大类别，除九大法定章节外还须"
            "有独立的「监测监控方案」章节（明确监测项目、点位布置、监测频次与"
            "预警值），且「计算书及相关图纸」章节须能引出平面布置图 / 节点详图。")
    return "\n".join(lines) + "\n"


def build_outline_element_block(scheme_name: str = "", scheme_type: str = "",
                                include_elements: bool = True) -> str:
    """目录生成侧「九大章节 · 本章必含要素」段落（无命中返回空串 = 不注入）。

    ⚠️ **为什么目录侧也要注入**：正文生成**只按目录给的标题写**。目录里若没有
    「材料计划 / 劳动力配置 / 应急物资清单」这类落点，正文阶段再要求「本章必须
    落实这些要素」也只能靠模型自己临时补标题，覆盖率完全取决于模型自觉 ——
    这正是「必含要素缺失」缺陷的结构性来源。把要素清单前置到目录阶段，是让
    要素真正落进成稿结构上的唯一有效手段。

    要素清单来自 :func:`content_checkpoint.chapter_required_elements`
    （其单一事实源为 ``scheme_classification.NINE_CHAPTERS``），本函数只渲染，
    **不复制任何字段清单**。

    Args:
        scheme_name / scheme_type: 用于反查危大类别以追加该章专属要素。
        include_elements: 调用方总开关（``settings.content_chapter_elements_inject``）。

    Returns:
        以换行开头的段落文本；无可用要素时返回空串（调用方据此不传变量，
        独占行整行丢弃 → 提示词与本功能引入前逐字一致）。
    """
    if not include_elements:
        return ""
    rows: list[tuple[str, list[str]]] = []
    for ch in NINE_CHAPTERS:
        key = str(ch.get("key") or "")
        elements = chapter_required_elements(key, scheme_name, scheme_type)
        if elements:
            rows.append((str(ch.get("title") or key), elements))
    if not rows:
        return ""
    lines = [
        "\n\n## 九大章节必含要素（目录二三级小节须逐项覆盖）",
        "下列每章必须包含的要素已由编制体系确定。设计各章二/三级小节时，"
        "请让下列要素**逐项有落点**（可作为独立小节，也可并入既有小节），"
        "不得整章只留一个笼统标题：",
    ]
    for title, elements in rows:
        lines.append(f"- **{title}**：" + "、".join(elements))
    lines.append(
        "（要素只决定「目录里要有落点」；具体写法与数据由正文生成按"
        "「模糊生成规则」完成，缺失数据用条件式表述写完整，不留占位标记。）")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 四、自检：锚点合法性
# ---------------------------------------------------------------------------

def validate_outline_anchoring() -> list[str]:
    """必备章节 spec 引用的每个 rule_id 必须存在于 audit_rules 注册表。

    「检查点 → 生成约束」的全部合法性建立在**锚点真实存在**上：审核规则一旦
    重编号 / 废弃，本模块必须同步修正而不是静默悬空。护栏测试消费本函数。
    """
    problems: list[str] = []
    for rid in _ALL_REQUIRED_RULE_IDS:
        if get_rule(rid) is None:
            problems.append(f"{rid} 不在审核注册表")
        elif rid not in _CHAPTER_DISPLAY_NAMES:
            problems.append(f"{rid} 缺少展示名")
    for rid in _LEGACY_OUTLINE_KEYWORDS:
        if rid not in _ALL_REQUIRED_RULE_IDS:
            problems.append(f"旧关键词表含未登记的规则 {rid}")
    return problems


def outline_hazard_flag(scheme_name: str = "", scheme_type: str = "") -> bool:
    """薄委托 :func:`scheme_classification.is_hazardous_by_keywords`（单一事实源）。

    与正文侧 :func:`content_checkpoint.is_hazardous_scheme` 同一判据 ——
    「生成按危大约束写、预检按非危大判」的分叉已在 STD-04 收口，目录侧必须
    复用同一出口，否则会重开该分叉。
    """
    return is_hazardous_by_keywords(f"{scheme_name} {scheme_type}")
