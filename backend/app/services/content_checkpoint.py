"""审核与预检检查点 → 正文生成约束（检查点前置的唯一事实源，2026-10-02）。

定位
----
审核与预检（``audit_rules`` + ``preflight_engine``）是**事后**防线；本模块把
其中**可在生成侧预防**的检查点前置为正文约束，形成
「检查点 → 生成约束 → 生成后自检 → 审核兜底」闭环，让正文生成时就按审核标准写。

设计红线（本仓反复踩过的坑，逐条规避）
--------------------------------------
1. **判据同源**：检查点条目一律**从 audit_rules 注册表派生**（rule_id / title /
   keywords 只作指针与主题词），本模块不复制审核规则的措辞与阈值 ——
   「同一判据三份措辞」是 AGENTS.md 记录的分叉病根。
2. **services 分层**：本模块禁止 import routers；被 prompts/content.py 与
   routers/sse_handlers.py 共同消费。
3. **消费口径**：注入提示词的主题词来自生产库预检实证的高频缺陷
   （SAF-02 缺高处作业/临电/防火/机械防护、SAF-07 缺特种作业持证、
   STD-04 编制依据未引 37 号令/31 号文、CON-04 正文残留【待补充】）。
4. **fail-soft**：自检函数对任何脏输入返回空结论，绝不抛业务异常
   （自检是"体检"，不是"闸门"）。

三类产出
--------
- ``build_content_system_checkpoint_block(is_hazardous)``：全方案级硬约束
  （注入 ``content_generation_system`` / ``content_continue_system`` 的
  ``{content_checkpoint_block}`` 占位符，注册时或渲染时替换）。
- ``build_chapter_checkpoint_block(title)``：按九大章节反查本章必含主题
  （未命中章节返回空串 = 不注入，零干扰）。
- ``checkpoint_selfcheck(...)``：生成后程序化自检（纯函数），产出与预检
  rule_id 同词表的 findings；是否阻断/修复由调用方按配置决定（默认不修）。
"""
from __future__ import annotations

import logging
import re

from app.services.audit_rules import get_rule
from app.services.content_fuzzy import (
    PLACEHOLDER_MARK_PATTERNS,
    placeholder_nonrewritable_patterns,
    placeholder_rewritable_patterns,
)
from app.services.scheme_classification import (
    is_hazardous_by_keywords, match_category_keywords, required_fields_for_chapter,
)

logger = logging.getLogger("content_checkpoint")

# ---------------------------------------------------------------------------
# 一、九大章节必含要素（按 chapter_key 反查）
# ---------------------------------------------------------------------------
#: chapter_key（scheme_classification.NINE_CHAPTERS 的 key，同一词表）
#: → 本章必含主题。
#:
#: topics 的每一项都**锚定一条审核规则**（rules 字段），主题词本身取自
#: 该规则在生产库中的命中证据（preflight_runs.findings，2026-10-01/02 实测）；
#: 不是凭空发挥。keywords 为空表示"主题词即判定词"（自检按子串匹配）。
CHAPTER_CHECKPOINT_REQUIREMENTS: dict[str, dict] = {
    "overview": {
        "title": "工程概况",
        "requirements": [
            {"rule_ids": ("CMP-01",),
             "label": "周边环境与危大特点",
             "must_include": ("地质", "水文", "周边环境"),
             "note": "工程概况须写明地质条件、水文（地下水位）、邻近建（构）筑物"
                     "与管线等周边环境，以及本危大工程的特点与风险辨识要点"},
        ],
    },
    "basis": {
        "title": "编制依据",
        "requirements": [
            {"rule_ids": ("STD-04",),
             "label": "危大工程法定依据",
             "must_include": ("37号",),
             "alt_include": ("建办质〔2018〕31号", "31号文"),
             "hazard_only": True,
             "note": "危大工程方案的编制依据必须列出《危险性较大的分部分项工程"
                     "安全管理规定》（住建部令第37号）与建办质〔2018〕31号文，"
                     "不得遗漏"},
            {"rule_ids": ("STD-02", "STD-05"),
             "label": "四层依据齐备",
             "must_include": ("法律法规", "标准", "规范"),
             "note": "编制依据按四层列齐：法律法规 → 部门规章 / 规范性文件 → "
                     "全文强制性工程建设规范（GB 55xxx）→ 本专业技术标准；"
                     "有地方标准 / 项目文件（施工图、施工组织设计、合同）一并列入"},
        ],
    },
    "plan": {
        "title": "施工计划",
        "requirements": [
            {"rule_ids": ("CMP-03",),
             "label": "进度与资源计划",
             "must_include": ("工期",),
             "note": "施工计划须含进度安排（总工期与关键节点，口径与全局事实一致）、"
                     "材料与设备计划（规格、数量、进场时间）与劳动力配置"},
        ],
    },
    "technique": {
        "title": "施工工艺技术",
        "requirements": [
            {"rule_ids": ("CMP-04",),
             "label": "参数·工艺·方法·检查要求",
             "must_include": ("工艺",),
             "note": "施工工艺技术须含：技术参数、工艺流程、施工方法、"
                     "操作要求与检查要求，工序可落地执行"},
        ],
    },
    "safety": {
        "title": "施工安全保证措施",
        "requirements": [
            {"rule_ids": ("CMP-05", "SAF-02"),
             "label": "专项安全技术措施",
             "must_include": ("高处作业", "临时用电", "消防", "机械"),
             "note": "安全保证措施除组织保障与监测监控外，必须逐项给出针对本方案"
                     "的专项安全技术措施：高处作业防护、施工临时用电、消防防火、"
                     "机械设备与吊装防护；不得只写通用套话"},
            {"rule_ids": ("SAF-06",),
             "label": "监测监控与预警值",
             "must_include": ("监测",),
             "alt_include": ("监控",),
             "note": "需监测的危大工程应明确监测项目、点位布置、监测频次与预警值"},
        ],
    },
    "personnel": {
        "title": "施工管理及作业人员配备和分工",
        "requirements": [
            {"rule_ids": ("CMP-06", "SAF-07"),
             "label": "人员配备与持证上岗",
             "must_include": ("特种作业", "持证"),
             "note": "必须写明管理人员与作业人员配备及岗位职责，并明确电工、焊工、"
                     "架子工、起重司索信号工等特种作业人员持证上岗要求"
                     "（只写岗位称谓，不得编造姓名与证书编号）"},
        ],
    },
    "acceptance": {
        "title": "验收要求",
        "requirements": [
            {"rule_ids": ("CMP-07",),
             "label": "验收标准·程序·内容·人员",
             "must_include": ("验收",),
             "note": "验收要求须明确验收标准（引用现行规范）、验收程序、验收内容"
                     "与验收人员，检验批划分符合规范"},
        ],
    },
    "emergency": {
        "title": "应急处置措施",
        "requirements": [
            {"rule_ids": ("SAF-03",),
             "label": "应急组织机构及职责",
             "must_include": ("应急组织", "职责"),
             "alt_include": ("组织机构",),
             "note": "按 GB/T 29639-2020 写明应急组织机构组成与各岗位职责"},
            {"rule_ids": ("SAF-04",),
             "label": "应急物资装备保障",
             "must_include": ("应急物资",),
             "alt_include": ("物资装备", "救援器材"),
             "note": "列出应急物资与装备清单（种类、数量、存放位置）"},
            {"rule_ids": ("SAF-05",),
             "label": "应急预案演练",
             "must_include": ("演练",),
             "alt_include": ("演习",),
             "note": "明确演练频次、组织方式与记录要求"},
        ],
    },
    "calc_drawings": {
        "title": "计算书及相关施工图纸",
        "requirements": [
            {"rule_ids": ("CMP-09", "TRC-01"),
             "label": "计算过程完整链条",
             "must_include": ("计算",),
             "note": "计算书必须给出完整链条：计算依据 → 参数取值及其来源 → "
                     "计算公式与过程 → 结论与安全系数；只有结论没有过程视为缺失"},
            {"rule_ids": ("TRC-03",),
             "label": "附图与节点详图",
             "must_include": ("图",),
             "note": "应附相关图纸（平面布置、节点详图、监测点布置等）并在正文中引出"},
        ],
    },
}

# ---------------------------------------------------------------------------
# 二、全方案级检查点硬约束（system 注入）
# ---------------------------------------------------------------------------
#: 与预检同词表的通用硬约束（措辞面向生成侧；判据指向 rule_id，不复制阈值）。
_GENERIC_CHECKPOINT_LINES: tuple[str, ...] = (
    "1. **法定内容齐全**（CMP-01~09）：本章属于九大法定章节之一时，必须覆盖该章"
    "审核要求的必备要素（见下方【本章审核检查点要求】），不得整章只写概述性套话。",
    "2. **编制依据合规**（STD-01~05）：只引用【现行有效标准参考清单】内的标准并写全"
    "编号与年号（禁止裸编号如「GB 50210」不带年号引用）；涉及危大工程的安全法规"
    "（住建部令第37号、建办质〔2018〕31号）在编制依据相关章节必须列明；"
    "引用清单外标准必须给出准确编号与现行年号，不得编造。",
    "3. **数值与事实一致**（CON-01~04）：工期、深度、高度、强度等级、人员数量、"
    "设备型号、质保期等关键数值全文只允许一个口径，一律以【全局事实变量】为准；"
    "注入的资料或事实文本中若出现「【待补充】」等占位写法，严禁照抄进正文，"
    "必须按「模糊生成规则」改写为完整表述。",
    "4. **安全与应急要素**（SAF-02~07）：安全保证类章节必须逐项给出针对性安全技术"
    "措施（高处作业、临时用电、消防防火、机械设备与吊装防护、监测监控与预警值），"
    "涉及特种作业必须写明持证上岗要求；应急类章节必须含应急组织及职责、应急物资"
    "装备、演练要求三要素。",
    "5. **禁止成段雷同**（CON-05/06）：不得把同一段表述复制进多个章节；即使主题"
    "相近，也必须结合本章的工艺、部位与工程条件改写为有针对性内容。",
    "6. **图表同步与可追溯**（DLV-04/TRC-01~03）：图号由导出器统一编号，正文不得"
    "自写图号；计算与验算内容必须给出公式、参数来源与结论的完整链条。",
    "7. **交付形态底线**（DLV-05~07）：不输出控制字符与乱码符号；Markdown 围栏"
    "（``` / ~~~）必须成对闭合；不使用口语化与 AI 客套表述。",
)

#: 危大工程方案追加约束（37号令 / 31号文要求，仅 is_hazardous 时注入）
_HAZARD_CHECKPOINT_LINES: tuple[str, ...] = (
    "8. **危大工程要求**（STD-04/SAF-06）：本方案属危险性较大的分部分项工程，"
    "正文必须按建办质〔2018〕31号明确：危大工程范围与判定依据（本方案的规模参数）、"
    "监测监控方案与预警值、安全防护与验收要求；涉及超过一定规模的危大工程时，"
    "应写明需组织专家论证及相关验算依据。",
)

_CHECKPOINT_HEADER = (
    "\n\n## 审核检查点前置要求（硬性，与预检/审核同一规则词表）\n"
    "正文生成后即进入审核与预检（规则 CMP/STD/SAF/CON/TRC/DLV）；"
    "下列检查点在**写作时**就必须满足，不要指望事后修复：")


def _category_display_names(scheme_name: str, scheme_type: str = "") -> list[str]:
    """方案命中的危大子类中文名（用于「按识别出的风险类型给措施」的措辞）。"""
    names: list[str] = []
    try:
        for hit in match_category_keywords(f"{scheme_name} {scheme_type}"):
            nm = (hit.get("sub_name") or hit.get("category_name") or "").strip()
            if nm and nm not in names:
                names.append(nm)
    except Exception:  # 纯派生信息，异常时降级为不点名
        logger.debug("危大子类反查失败（降级为通用措辞）", exc_info=True)
    return names[:6]


def is_hazardous_scheme(scheme_name: str = "", scheme_type: str = "") -> bool:
    """方案是否命中危大工程（确定性关键词反查，零 AI）。

    薄委托 :func:`scheme_classification.is_hazardous_by_keywords`（单一事实源）——
    与审核预检侧 STD-04 危大门控共用同一判据，避免「生成按危大约束写、预检按
    非危大判」的分叉。保留本函数名供生成链路调用点零改动。
    """
    return is_hazardous_by_keywords(f"{scheme_name} {scheme_type}")


def build_content_system_checkpoint_block(
        scheme_name: str = "", scheme_type: str = "",
        is_hazardous: bool | None = None) -> str:
    """全方案级「检查点 → 生成约束」system 段落。

    Args:
        scheme_name / scheme_type: 用于反查危大子类点名（可为空）。
        is_hazardous: None = 按方案名自动判定；True/False = 调用方显式指定。

    Returns:
        以换行开头的段落文本（永不为空——通用约束恒注入）。
    """
    lines = list(_GENERIC_CHECKPOINT_LINES)
    if is_hazardous is None:
        is_hazardous = is_hazardous_scheme(scheme_name, scheme_type)
    if is_hazardous:
        hazard_lines = list(_HAZARD_CHECKPOINT_LINES)
        cats = _category_display_names(scheme_name, scheme_type)
        if cats:
            hazard_lines[0] += f"（本工程识别为：{'、'.join(cats)}）"
        lines.extend(hazard_lines)
    return _CHECKPOINT_HEADER + "\n".join(lines)


def chapter_required_elements(chapter_key: str, scheme_name: str = "",
                              scheme_type: str = "") -> list[str]:
    """返回某章节的**完整**必含要素清单（通用要素 + 命中的危大类别追加要素）。

    单一事实源为 :func:`scheme_classification.required_fields_for_chapter`
    （``NINE_CHAPTERS.base_fields`` + ``category_fields``），本函数只做
    「多危大类别合并去重」的编排，**不复制任何字段清单**——本仓反复踩过的
    「同一判据两处实现」分叉病根，在此从结构上排除。

    Args:
        chapter_key: 九大章节 key；空串或未知 key 返回空表。
        scheme_name / scheme_type: 用于反查命中的危大类别，取到的类别按
            ``category_fields`` 追加该章专属要素（如基坑类追加「基坑深度」）。

    Returns:
        要素名列表，顺序稳定（通用要素在前，追加要素按类别命中顺序在后）。
    """
    if not chapter_key:
        return []
    try:
        base = required_fields_for_chapter(chapter_key)
    except Exception:
        return []
    if not base:
        return []
    out: list[str] = list(base)
    seen: set = set(out)
    try:
        hits = match_category_keywords(f"{scheme_name} {scheme_type}")
    except Exception:
        hits = []
    for hit in hits:
        cid = str(hit.get("category_id") or "")
        for field in required_fields_for_chapter(chapter_key, cid):
            if field not in seen:
                seen.add(field)
                out.append(field)
    return out


def build_chapter_element_block(chapter_key: str, scheme_name: str = "",
                                scheme_type: str = "",
                                include_elements: bool = True) -> str:
    """渲染【本章必含要素清单】提示段（未命中章节 / 开关关闭返回空串 = 不注入）。

    ⚠️ 措辞只给**写作要求**（逐项落实 / 缺一项即不完整），不承诺「已包含」——
    模型确实写不出时由生成后自检与预检捕获，而不是在此处造假。

    ⚠️ 明确「缺数据时怎么写字」的口径：要素涉及的事实数据未给出时，按
    「模糊生成规则」用条件式或范围式表述把话说完，**不得留占位标记、
    不得只写要素名称不写内容**（点名「要素名称」正是历史上 AI 最容易
    退化的形态：写出一行「一、工程名称」却无任何实质内容）。
    """
    if not include_elements:
        return ""
    elements = chapter_required_elements(chapter_key, scheme_name, scheme_type)
    if not elements:
        return ""
    req = CHAPTER_CHECKPOINT_REQUIREMENTS.get(chapter_key or "")
    title = str((req or {}).get("title") or chapter_key or "").strip() or "本章"
    lines = [
        f"\n【{title} · 本章必含要素清单】（逐项落实，缺一项即视为章节不完整）",
        f"本章必须逐项落实下列 {len(elements)} 项要素："
        f"{'；'.join(elements)}。",
        "落实要求：每一项都要有实质内容（做法、参数、组织或判定结论），"
        "不得只写要素名称不写内容；要素涉及的事实数据以【全局事实变量】为准，"
        "事实未给出具体数值的按「模糊生成规则」用条件式或范围式表述写完整"
        "（如「按设计文件及现场实际确定」），**严禁留占位标记**。",
    ]
    return "\n".join(lines)


def build_chapter_checkpoint_block(chapter_key: str,
                                   is_hazardous: bool = False,
                                   include_elements: bool = True,
                                   scheme_name: str = "",
                                   scheme_type: str = "") -> str:
    """按九大章节 key 反查「本章必含要素」提示块（未命中返回空串 = 不注入）。

    ``is_hazardous=False`` 时 hazard_only 要求（如 STD-04 危大法规）整条跳过，
    非危大方案不被要求引用 37 号令。

    措辞为写作要求（必须/不得），不承诺「已包含」——若模型确实写不出，
    生成后自检会捕获并上报，而不是在此处造假。

    Args:
        chapter_key: 九大章节 key。
        is_hazardous: 是否危大工程方案（决定 hazard_only 要求是否参与）。
        include_elements: 是否在块尾追加**完整**必含要素清单
            （:func:`build_chapter_element_block`）；由
            ``settings.content_chapter_elements_inject`` 控制（默认 True）。
        scheme_name / scheme_type: 用于反查危大类别以追加该章专属要素。
            均为可选、默认空串 → 仅通用要素，既有调用点行为不变。
    """
    req = CHAPTER_CHECKPOINT_REQUIREMENTS.get(chapter_key or "")
    if not req:
        return ""
    items = [r for r in req["requirements"] if _req_applicable(r, is_hazardous)]
    lines: list[str] = []
    if items:
        lines.append("\n【本章审核检查点要求】（预检与审核将按下列规则检查本章，写作时必须满足）")
        for r in items:
            rid = "、".join(r["rule_ids"])
            topic = "、".join(r["must_include"])
            if r.get("alt_include"):
                topic += "（或同义表述：" + "、".join(r["alt_include"]) + "）"
            lines.append(f"- 〔{rid}｜{r['label']}〕{r['note']}；涉及关键词：{topic}")
    element_block = build_chapter_element_block(
        chapter_key, scheme_name=scheme_name, scheme_type=scheme_type,
        include_elements=include_elements)
    if element_block:
        lines.append(element_block)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 三、生成后程序化自检（纯函数， findings 与预检同词表）
# ---------------------------------------------------------------------------
#: 占位标记残留（CON-04 生成侧形态）—— **判据唯一事实源在 content_fuzzy**。
#:
#: ⚠️ 2026-10-03 收敛（生产缺陷驱动）：本处原先自带一份**更窄**的正则副本，
#: 只收 ``【待(补充|定|确认)`` + ``[待补充]`` + ``××`` 三类。而检测侧
#: ``content_fuzzy.scan_placeholder_marks`` 收五类（另有【待完善】【待补录】
#: 【略】【】[TBD] 裸 TBD/N/A 等）。后果是「检出却修不掉」：
#: ``standard_report`` 报 placeholder、CON-04 自检漏报、
#: :func:`rewrite_placeholder_marks` 也改不动 —— 三处口径不一致。
#: 现检测与改写都从 ``content_fuzzy.PLACEHOLDER_MARK_PATTERNS`` 取用，
#: 分叉在结构上不可能发生。
#:
#: ✅ 2026-10-02（B-2 过度匹配修复）的结论仍然成立且已由上游承接：
#: 占位标记的内部字符类排除 ``【``，否则「深度【待定】m 宽度【待补充：宽度】mm」
#: 会被当成**一个**占位标记（匹配到整段，把「m 宽度」这段正常正文吞掉）——
#: 自检报告把 2 个占位符报成 1 个，改写更会把正常正文一起删掉。
_PLACEHOLDER_RES: tuple[re.Pattern, ...] = tuple(
    rx for rx, _kind, _rw in PLACEHOLDER_MARK_PATTERNS
)
#: 危大法规引用（STD-04）：任一命中即视为已引
_HAZARD_REG_RE = re.compile(
    r"37\s*号|住建部令第\s*37|住房和城乡建设部令第37号|危险性较大的分部分项工程"
    r"|建办质〔?2018〕?31号|建办质\[?2018\]?31号")
#: 裸标准编号（无年号）——STD-03 的生成侧形态（正文要求写全年号）
_BARE_CODE_RE = re.compile(
    r"(?:GB/T|GB|JGJ/T|JGJ|JTG/T|JTG|DBJ|CECS|CJJ/T|CJJ)\s*[0-9]{2,5}"
    r"(?:\.[0-9]{1,3})?(?![0-9])(?!\s*[—\-－]\s*[0-9]{4})(?![0-9])")
#: 年份形态排除（如「JGJ 59-2011」已带年号；正则已用负向断言，此处双保险不必再查）


def _topic_hit(text: str, terms: tuple[str, ...],
               alts: tuple[str, ...] = ()) -> bool:
    """主题命中：``must_include`` **全部**出现才算满足，或命中任一
    ``alt_include`` 同义表述即视为等价满足。

    ⚠️ 不是「任一 must_include」：SAF-02 要求高处/临电/防火/机械**逐项**给出，
    任一即过会让只写其中一项的正文蒙混过关（护栏测试锁定）。
    """
    if alts and any(t and t in text for t in alts):
        return True
    terms = [t for t in terms if t]
    return bool(terms) and all(t in text for t in terms)


def _req_applicable(req: dict, is_hazardous: bool) -> bool:
    """hazard_only 要求只对危大方案注入/自检（非危大方案不提 37 号令）。"""
    return not req.get("hazard_only") or bool(is_hazardous)


def _req_in_scope(title: str, req: dict) -> bool:
    """判断**小节**是否承担某一条章节要求（作用域过滤）。

    为什么需要：审核侧（``preflight.check_safety``）是把标题含「应急/救援/预案」
    的**全部章节正文聚合**后再判定 SAF-03/04/05 —— 它是**章级聚合**判据。
    而生成侧自检是**逐叶子章节**跑的。若不区分，「应急组织机构及职责」这个小节
    会被要求同时具备应急物资与演练（它本来就不该写那两件事），于是产出
    2 条**假缺项**。

    判据：小节标题须与该要求的主题词（``must_include`` / ``alt_include``）或
    要求 ``label`` 相关（子串包含或互含）才算承担该要求。全部不相关时该小节
    不承担任何要求 —— 与主判据「未命中不猜」的既有契约一致。

    例：标题「应急组织机构及职责」→ 只承担 SAF-03；不承担 SAF-04 / SAF-05。
    """
    t = (title or "").strip()
    if not t:
        return False
    for kw in tuple(req.get("must_include") or ()) + tuple(req.get("alt_include") or ()):
        if kw and kw in t:
            return True
    label = (req.get("label") or "").strip()
    if label and (label in t or t in label):
        return True
    return False



def _basis_standard_findings(text: str, *, scheme_name: str = "",
                             scheme_type: str = "") -> list[dict]:
    """编制依据章的标准**编号**级自检（STD-02 / STD-05）。

    判据与 ``preflight_engine.check_standards`` **同源**：编号提取直接复用
    引擎的 ``_STANDARD_CODE_RE``，归一化 / 类别匹配复用 ``standards_registry``。
    因此「本章自检判缺」蕴含「预检会报」—— 不会自造假缺项。

    - ``STD-02``：本章须出现至少一个 **GB 55xxx** 全文强制性工程建设规范编号；
    - ``STD-05``：本章须出现至少一个**本方案专业类别**的现行标准编号。

    两项都是「**至少一个**」而非「全部」：预检就是这么判的（任一命中即通过），
    判严了会产生假缺项。fail-soft：任何异常返回空列表。

    ⚠️ **不可复用本模块的 ``_BARE_CODE_RE``**：它是「裸编号（无年号）」检测器，
    带 ``(?![-\\s]*\\d{4})`` 负向断言 → **写全年号的编号一个都提取不到**
    （「GB 55034-2022」不匹配）。用它判「有没有引用」会把合规正文判成缺失。

    Args:
        scheme_name / scheme_type: 供 STD-05 反查专业类别；两者皆空时
            **跳过 STD-05**（无法判定类别就不猜），STD-02 仍照常判定。
    """
    out: list[dict] = []
    try:
        from app.services import preflight_engine as _pf
        from app.services.standards_registry import (
            CATEGORY_STANDARDS, match_categories, normalize_standard_code,
        )
        norm = normalize_standard_code(text)
        # ✅ 单一事实源：与 check_standards 完全相同的编号提取正则
        codes = {m.group(0).strip() for m in _pf._STANDARD_CODE_RE.finditer(text)}
        if not any(_pf._MANDATORY_CODE_RE.search(c) for c in codes):
            out.append({
                "rule_id": "STD-02",
                "checkpoint": "未引用全文强制性工程建设规范编号",
                "severity": "medium",
                "source": "selfcheck",
                "message": "编制依据章节未检出 GB 55xxx 系列全文强制规范编号"
                           "（如 GB 55034-2022 / GB 55032-2022）",
                "suggestion": "在编制依据中列出本工程适用的全文强制性工程建设规范"
                              "（GB 55xxx 系列），并写全编号与年号",
            })
        cats = match_categories(scheme_name, scheme_type)[:2] if (
            scheme_name or scheme_type) else []
        cat_codes = [s.code for cat in cats
                     for s in CATEGORY_STANDARDS.get(cat, [])]
        if cat_codes and not any(
                normalize_standard_code(c) in norm for c in cat_codes):
            out.append({
                "rule_id": "STD-05",
                "checkpoint": "未引用本专业类别现行技术标准",
                "severity": "medium",
                "source": "selfcheck",
                "message": f"编制依据章节未检出「{'/'.join(cats)}」类的现行"
                           f"专业技术标准编号",
                "suggestion": "补充本方案专业类别对应的现行技术标准编号"
                              "（见【现行有效标准参考清单】中该类条目）",
            })
    except Exception as exc:  # fail-soft：自检是体检，不是闸门
        logger.warning("编制依据标准编号自检异常（忽略）: %s", exc, exc_info=True)
    return out


def checkpoint_selfcheck(
        content: str, *, chapter_key: str = "",
        is_hazardous_basis: bool = False,
        section_title: str = "", subsection_scope: bool = False,
        scheme_name: str = "", scheme_type: str = "") -> list[dict]:
    """对一章生成结果做检查点自检，返回 findings 列表（rule_id 与预检同词表）。

    Args:
        content: 本章正文（Markdown）。
        chapter_key: 九大章节 key；空串 = 无章节归属，只跑通用检查。
        is_hazardous_basis: 是否危大工程方案（决定 hazard_only 要求是否参与）。
        section_title: 本章标题。**仅当 ``subsection_scope=True`` 时参与判定**。
        subsection_scope: True = 本章是**小节**（chapter_key 由补充推断得出，
            主判据未命中），只检查该小节承担的条要求，避免把章级聚合要求
            拆到每个小节上造成假缺项（见 :func:`_req_in_scope`）。
            默认 False = 保持既有行为不变（章级标题 / 未分类章节照旧全查）。
        scheme_name / scheme_type: 方案名与类型，**仅**供编制依据章的
            STD-05 专业类别判定使用（:func:`_basis_standard_findings`）。
            均为可选、默认空串 → 既有调用点行为逐字不变；为空时 STD-05
            无法判定类别，**直接跳过**（不猜、不假报）。

    只做**确定性、低误报**的程序化判定（语义级判定仍归 AI 审核）：
    - 本章必含主题缺失 → 对应 CMP/SAF/STD 规则，severity=medium
      （单章缺要素属「整改」级，整案缺失才由预检升级为 block/high）；
      hazard_only 要求（STD-04）仅 is_hazardous_basis=True 时参与判定，
      并用 _HAZARD_REG_RE 宽口径豁免（全令号/简称/方括号变体均视为已引）；
    - 占位标记残留 → CON-04，severity=error（交付硬伤，可自动修复）；
    - 裸标准编号（无年号）→ STD-03，severity=warning（提示补年号，不拦截）。

    任何脏输入 / 内部异常一律返回已收集结论，绝不抛出。
    """
    findings: list[dict] = []
    text = content or ""
    if not text.strip():
        return findings
    try:
        # ---- 章节必含主题 ----
        req = CHAPTER_CHECKPOINT_REQUIREMENTS.get(chapter_key or "")
        if req:
            for r in req["requirements"]:
                if not _req_applicable(r, is_hazardous_basis):
                    continue
                # 小节只查自己承担的要求（章级聚合要求不拆到小节）
                if subsection_scope and not _req_in_scope(section_title, r):
                    continue
                if _topic_hit(text, r["must_include"],
                              tuple(r.get("alt_include") or ())):
                    continue
                if r.get("hazard_only") and _HAZARD_REG_RE.search(text):
                    continue
                findings.append({
                    "rule_id": r["rule_ids"][0],
                    "checkpoint": r["label"],
                    "severity": "medium",
                    "source": "selfcheck",
                    "message": f"〔{req['title']}〕缺少「{r['label']}」相关内容"
                               f"（关键词：{'、'.join(r['must_include'])}）",
                    "suggestion": r["note"],
                })

        # ---- 编制依据章：标准**编号**级校验（STD-02 / STD-05） ----
        # ⚠️ 上面的章节必含要素只判「法律法规/标准/规范」等**词**，
        # 而预检 ``check_standards`` 判的是**编号**：
        #   - STD-02：全篇至少一个 GB 55xxx 全文强制规范编号；
        #   - STD-05：本方案类别（CATEGORY_STANDARDS）至少一个现行标准编号。
        # 词过了而编号没有 → 目录侧/正文侧全绿、预检照样报 high（生产库 STD-05
        # high 实证）。此处用**与预检同源**的判据补齐，零 AI 成本。
        if chapter_key == "basis":
            findings.extend(_basis_standard_findings(
                text, scheme_name=scheme_name, scheme_type=scheme_type))

        # ---- 计算书章：计算过程完整性（TRC-01，与预检同源判据） ----
        # ⚠️ 上面的章节必含要素只判「计算」这个**词**，而预检
        # ``check_traceability`` 的 TRC-01 判的是**公式与参数代入过程**
        # （``_FORMULA_RES``），且是 **block** 级。词过了而过程没有 →
        # 生成侧自检全绿、预检照报 block。故此处直接复用**预检的同一
        # 判据函数**（``preflight_engine.has_calc_process``），不在本模块
        # 重抄公式正则 —— 本仓反复出现的「同一判据多处实现」分叉病根。
        # 「本节是不是计算书章节」同样必须同源：预检用
        # ``CALC_TITLE_KEYWORDS``（计算书/验算/受力计算/承载力计算/稳定性验算/
        # 安全系数）判标题，而章节归类补表只到「计算书/验算」—— 只按
        # chapter_key 判会让「承载力计算」这类章节**漏检**，与预检分叉。
        # 两个维度都取预检同一出口，分叉在结构上不可能发生。
        if _is_calc_section(section_title, chapter_key):
            findings.extend(_calc_process_findings(text))

        # ---- 占位标记残留（可自动修复） ----
        marks: list[str] = []
        for rx in _PLACEHOLDER_RES:
            for m in rx.finditer(text):
                frag = m.group(0).strip()
                # ⚠️ 2026-10-03：判据收敛到 content_fuzzy 后**不再跳过**
                # 「纯 ×× 短形态」。旧守卫 `len(frag) <= 3` 是配合旧正则
                # `[×xX]{2,}\s*(?:单位)?`（会连带吞掉后面的单位、且无词边界）
                # 写的，效果是「裸 ×× 不报、××kPa 才报」—— 与
                # standard_report / 《待补充清单》 全绿/全红的口径相反，
                # 属「检测侧报了、自检侧漏报」的分叉。
                # 新判据 RE_FUZZY 自带边界守卫（`××+` / 前后非字母数字的
                # `xx`），单个乘号「2×3」本就不匹配，无需再用长度守卫兜底。
                if frag and frag not in marks:
                    marks.append(frag)
        if marks:
            findings.append({
                "rule_id": "CON-04",
                "checkpoint": "占位标记残留",
                "severity": "error",
                "source": "selfcheck",
                "fixable": True,
                "message": f"正文残留 {len(marks)} 处占位标记："
                           f"{'、'.join(marks[:5])}",
                "suggestion": "按「模糊生成规则」把占位处改写为完整表述"
                              "（范围值 / 技术原则 / 条件式），不得编造具体数值",
                "evidence": marks[:20],
            })

        # ---- 裸标准编号（无年号） ----
        bare = sorted({m.group(0).strip() for m in _BARE_CODE_RE.finditer(text)})
        # 排除同时以带年号形态出现的编号（正文写全时不报）
        norm = re.sub(r"\s+", "", text)
        bare = [b for b in bare
                if re.sub(r"\s+", "", b) + "-" not in norm
                and not re.search(re.sub(r"\s+", "", b) + r"[—－–-]\s*\d{4}", text)]
        if bare:
            findings.append({
                "rule_id": "STD-03",
                "checkpoint": "标准编号未写年号",
                "severity": "warning",
                "source": "selfcheck",
                "message": f"{len(bare)} 处标准编号未带年号：{'、'.join(bare[:5])}",
                "suggestion": "首次引用写全「《名称》（编号-年号）」",
                "evidence": bare[:20],
            })
    except Exception as exc:  # fail-soft：自检异常绝不污染生成链路
        logger.warning("检查点自检异常（忽略）: %s", exc, exc_info=True)
    return findings


# ---------------------------------------------------------------------------
# 三之三、计算书计算过程完整性（TRC-01，与预检同源判据）
# ---------------------------------------------------------------------------
def _is_calc_section(title: str = "", chapter_key: str = "") -> bool:
    """判断一节是否为「计算书 / 验算」章节 —— 与预检 **同一标题判据**。

    预检 ``check_traceability`` 用 ``CALC_TITLE_KEYWORDS`` 判「这节算不算
    计算书章节」，本函数必须用同一常量，否则会出现「生成侧认为不是计算书
    → 不做 TRC-01 自检 → 预检认为它是计算书 → 照报 block」的分叉。

    两个来源（章节归类 + 标题关键词）**任一命中**即判计算书章节：

    - ``chapter_key == "calc_drawings"``：章节归类已确认（能覆盖
      「平面布置图」这类关键词表里没有的标题）；
    - 标题命中 ``CALC_TITLE_KEYWORDS``：与预检逐字同口径（能覆盖
      「承载力计算」「安全系数分析」这类章节归类未覆盖的标题）。

    取并集而非只用其一：两者互为补充，不互相覆盖、也绝不放宽判据
    （「目录侧判据必须等于或略宽于审核侧」的方向约束不变）。
    常量读取失败时 fail-soft 回落到章节归类，绝不抛异常。
    """
    if chapter_key == "calc_drawings":
        return True
    try:
        from app.services.preflight_engine import CALC_TITLE_KEYWORDS
    except Exception:
        return False
    t = str(title or "")
    if not t:
        return False
    return any(k in t for k in CALC_TITLE_KEYWORDS)


def _calc_process_findings(text: str) -> list[dict]:
    """计算书章节的计算过程完整性判定 —— **与预检 TRC-01 同一判据函数**。

    为什么必须同源：章节必含要素只判「计算」这个**词**（见
    ``CHAPTER_CHECKPOINT_REQUIREMENTS["calc_drawings"]`` 的 ``must_include``），
    而预检 ``check_traceability`` 的 TRC-01 用 ``_FORMULA_RES`` 判**公式与
    参数代入过程**，且是 **block** 级。词过了而过程没有 → 生成侧自检全绿、
    预检照报 block。

    ⚠️ 直接 import 预检的公开出口 ``has_calc_process``，**不在本模块重抄公式
    正则**。这是本仓反复出现的「同一判据多处实现」分叉病根（与 STD-05 补
    编号级判据、目录侧必备章节按审核侧同谓词 是同一类修复）。

    零 IO、零 AI、fail-soft：任何异常返回空结论。
    """
    try:
        if not text or not text.strip():
            return []
        from app.services.preflight_engine import has_calc_process
        if has_calc_process(text):
            return []
    except Exception as exc:  # fail-soft：自检异常绝不污染生成链路
        logger.warning("计算过程判定异常（忽略）: %s", exc, exc_info=True)
        return []
    return [{
        "rule_id": "TRC-01",
        "checkpoint": "计算过程完整性",
        "severity": "medium",
        "source": "selfcheck",
        "message": "〔计算书及相关施工图纸〕未见计算公式或参数代入过程",
        "suggestion": "计算书须给出完整链条：计算依据 → 参数取值及其来源 → "
                      "计算公式与过程 → 结论与安全系数；只有结论没有过程"
                      "视为缺失（该检查点为预检 block 级）",
    }]


# ---------------------------------------------------------------------------
# 三之四、跨章节段落搬运（CON-06，与预检同源判据）
# ---------------------------------------------------------------------------
def cross_section_copy_findings(sections: list[dict], *,
                                new_section_id: str = "",
                                limit: int = 3) -> list[dict]:
    """跨章节段落搬运检测 —— 生成后自检用，**与预检 CON-06 同源判据**。

    Args:
        sections: ``[{id, title, content}, ...]``，须包含刚生成完的章节
            （与 ``PreflightContext.sections`` 同形；缺键一律容忍）。
        new_section_id: 刚生成完的章节 id；只报**涉及该章节**的搬运组，
            否则会把历史搬运反复报出来。空串 = 报全部组（离线诊断语义）。
        limit: 最多返回的 findings 条数（避免一章抄多处时刷屏）。

    Returns:
        findings 列表，``rule_id`` 恒为 ``CON-06``（基规则；预检侧用派生
        编号 ``CON-06-N`` 避免评分去重时多组塌缩成一条）。

    ⚠️ 为什么放在**生成后**而非提示词：CON-06 是**跨章节**判据，生成单章时
    模型看不到其他章节的正文，system 级「禁止成段雷同」的约束**结构上无法
    预防**它。生产库实证 3 条 CON-06 全部是「骨架归一后相似度 100%」
    （整段照抄，连数字都没换）——该判据只能在「本章已生成、其余章节已在库」
    时判定，故挂生成后自检。

    判据直接复用 ``duplicate_detection.find_cross_section_copies``（预检
    CON-06 的同一实现），**不在本模块重抄相似度阈值与骨架归一规则**。
    零 IO、零 AI、fail-soft。
    """
    findings: list[dict] = []
    if not sections:
        return findings
    try:
        from app.services.duplicate_detection import find_cross_section_copies
        result = find_cross_section_copies(sections)
        groups = result.get("groups") or []
    except Exception as exc:  # 防御性兜底：查重异常不应拖垮生成链路
        logger.warning("跨章节段落搬运检测异常（忽略）: %s", exc,
                       exc_info=True)
        return findings

    emitted = 0
    for g in groups:
        pairs = g.get("section_pairs") or []
        if not pairs:
            continue
        # 只报涉及本章的组（新章节是搬运的**接收方**或**来源方**都算）
        if new_section_id and not any(
                (p[0] == new_section_id or p[1] == new_section_id)
                for p in pairs):
            continue
        others: list[str] = []
        for a_id, b_id, a_title, b_title in pairs:
            # 取「不是本章」的那一侧标题作为搬运目标名
            tgt_id, tgt_title = ((a_id, b_title) if b_id == new_section_id
                                 else (b_id, a_title))
            nm = str(tgt_title or "").strip()
            if tgt_id != new_section_id and nm and nm not in others:
                others.append(nm)
        if not others:
            continue
        dice = float(g.get("dice") or 0.0)
        if g.get("reason") == "exact":
            sim_txt = "骨架归一后完全一致"
        else:
            sim_txt = f"骨架归一后相似度 {dice:.0%}"
        f = {
            "rule_id": "CON-06",
            "checkpoint": "跨章节段落搬运",
            "severity": "high" if g.get("large") else "medium",
            "source": "selfcheck",
            "message": "本章与「%s」存在成段照抄（%s）" % (
                "、".join(others[:3]), sim_txt),
            "suggestion": "请改写为针对本章工艺、部位与工程条件的表述"
                          "（换章节侧重、工艺细节与工程条件），不要复用"
                          "其他章节的成段文字",
            "evidence": [str(s) for s in (g.get("sentences") or [])][:3],
        }
        if g.get("large"):
            f["message"] += "（大段照抄，疑似直接复制）"
        findings.append(f)
        emitted += 1
        if emitted >= limit:
            break
    return findings


def validate_rule_anchoring() -> list[str]:
    """自检：本章必含要求引用的每个 rule_id 必须存在于 audit_rules 注册表。

    「检查点 → 生成约束」的全部合法性建立在**锚点真实存在**上：审核规则
    一旦重编号/废弃，本模块必须同步修正而不是静默悬空。护栏测试消费本函数。
    """
    problems: list[str] = []
    for key, req in CHAPTER_CHECKPOINT_REQUIREMENTS.items():
        for r in req.get("requirements", []):
            for rid in r.get("rule_ids", ()):
                if get_rule(rid) is None:
                    problems.append(f"{key}/{r.get('label', '')}: 规则 {rid} 不在审核注册表")
    return problems


# ---------------------------------------------------------------------------
# 三之一、章节 key 补充推断（小节标题覆盖，2026-10-02）
# ---------------------------------------------------------------------------
#: 九大章节标准名与既有别名表都命不中的**小节标题** → chapter_key。
#:
#: 生产实证（scheme=d3c1a897…，36 个叶子章节标题实测）：正文只落叶子，而
#: 「应急组织机构及职责 / 应急物资装备保障 / 应急演练」三节是应急处置章的叶子，
#: 标题**既不等于**也不**互含**九大章节标准名「应急处置措施」，主判据
#: （``sse_handlers.chapter_key_of_title``）一律返回空串 —— 于是 SAF-03/04/05
#: （应急三要素）在生成侧**从未被要求、也从未被自检过**。审核侧按「标题含
#: 应急」能聚合到这些章节，生成侧却因章节归类失败而完全漏管。
#:
#: 同类漏映射还有「照明及手持电动工具管理」（临时用电）、「装修动火作业审批」
#: （消防）—— 恰是 SAF-02 在生产 findings 里点名的缺失项（高处作业 / 临时用电 /
#: 消防防火 / 机械设备）。本表按此补齐。
#:
#: **只补不覆盖**：主判据命中时绝不参与；本表未命中仍返回空串（宁可漏注入
#: 也不猜，与 ``chapter_key_of_title`` 的既有契约一致）。
CHAPTER_TITLE_SUPPLEMENT: dict[str, tuple[str, ...]] = {
    "overview": ("工程基本情况", "周边环境", "工程环境", "施工条件", "工程条件"),
    "basis": ("编制依据", "法律法规", "标准清单", "规范清单"),
    "plan": ("进场计划", "材料计划", "设备计划", "材料准备", "设备准备",
             "劳动力计划", "进度安排", "工期安排"),
    "technique": ("施工做法", "施工方法", "施工工序", "工艺要求", "细部做法"),
    "safety": ("临时用电", "手持电动工具", "临电", "动火", "消防", "防火",
               "高处作业", "机械防护", "机械设备管理", "起重吊装安全",
               "垃圾清运", "扬尘", "噪声", "防护棚"),
    "personnel": ("持证上岗", "特种作业", "岗位职责", "安全管理机构",
                  "安全生产管理"),
    "acceptance": ("观感质量", "检验批", "竣工资料", "环保检测", "实体检测"),
    "emergency": ("应急组织", "组织机构及职责", "应急物资", "应急演练",
                  "救援", "急救", "疏散", "预警响应", "响应程序", "善后"),
    "calc_drawings": ("施工图纸", "节点详图", "平面布置图", "计算书", "验算"),
}

#: 补充关键词按长度降序展开（长词优先，避免"垃圾"抢先于"垃圾清运"这类前缀误配）
_SUPPLEMENT_ORDERED: tuple[tuple[str, str], ...] = tuple(
    sorted(((kw, key) for key, kws in CHAPTER_TITLE_SUPPLEMENT.items()
            for kw in kws),
           key=lambda kv: (-len(kv[0]), kv[1])))

# ---------------------------------------------------------------------------
# 三之二、STD-03 裸标准编号补年号（确定性自动修复，2026-10-02）
# ---------------------------------------------------------------------------
def _build_base_code_index() -> tuple:
    """返回 ``(可用基号→完整编号, 歧义基号→另一编号)``。

    **唯一来源** = ``standards_registry`` 的现行标准库
    （BASE_STANDARDS + CATEGORY_STANDARDS）—— 本模块不另立词表、不写死年号。
    同一基号对应**多个不同**完整编号时视为歧义并从可用映射剔除：
    年号无法唯一确定时不补（补了就是编造年号）。
    同一完整编号在多类别重复登记（实测 ``GB 50204-2015`` 出现 4 次）不算歧义。
    """
    from app.services import standards_registry as _sr
    base2code: dict = {}
    ambiguous: dict = {}
    pool = list(_sr.BASE_STANDARDS)
    for items in _sr.CATEGORY_STANDARDS.values():
        pool.extend(items)
    for s in pool:
        base = _sr.strip_standard_year(s.code)
        if not base:
            continue
        cur = base2code.get(base)
        if cur is None:
            base2code[base] = s.code
        elif cur != s.code:
            ambiguous[base] = s.code
    usable = {b: c for b, c in base2code.items() if b not in ambiguous}
    return usable, ambiguous


BASE_CODE_INDEX, AMBIGUOUS_BASES = _build_base_code_index()

#: 围栏行（``` / ~~~，允许 ≤3 前导空格，与 CommonMark 口径一致）
_FENCE_LINE_RE = re.compile(r"^[ \t]{0,3}(```+|~~~+)", re.MULTILINE)


def _fence_ranges(text: str) -> list:
    """返回正文中所有围栏代码块的字符区间 ``[(start, end), ...]``。

    状态机配对：同类围栏标记（``` 与 ~~~ 不混）且长度不小于开围栏时才闭合。
    未闭合时区间断到文末（与 ``auto_fix_unclosed_fences`` 的既有兜底兼容）。
    区间外的才是可修改正文 —— 避免改写 Mermaid / chart-json 载荷。
    """
    hits = list(_FENCE_LINE_RE.finditer(text))
    ranges: list = []
    i, n = 0, len(hits)
    while i < n:
        opener = hits[i]
        mark, olen = opener.group(1)[0], len(opener.group(1))
        close_idx = None
        for j in range(i + 1, n):
            if hits[j].group(1)[0] == mark and len(hits[j].group(1)) >= olen:
                close_idx = j
                break
        if close_idx is None:
            ranges.append((opener.start(), len(text)))
            break
        ranges.append((opener.start(), hits[close_idx].end()))
        i = close_idx + 1
    return ranges


def fix_bare_standard_codes(text: str) -> tuple:
    """把裸标准编号补成带年号形态，返回 ``(新正文, 修复清单)``。

    对应审核规则 ``STD-03``（生产 findings 实测：检出 6 个编号缺年号）。
    这是「检查点 → 生成后自检 → **确定性自动修复**」闭环中**唯一可零风险
    自动修复**的高频缺陷，逐条红线：

    - 年号**只取自现行标准库**（``BASE_CODE_INDEX``），库外编号一律不补；
    - 已带年号的不重复补（``_BARE_CODE_RE`` 负向断言已排除）；
    - 歧义基号不补，只在清单里报出待人工核实（``fixable=False``）；
    - 围栏代码块内的文本不动；
    - **幂等**：对已修复文本再跑一次返回零修复；
    - fail-soft：任何异常返回原文与空清单，绝不污染生成链路。

    Returns:
        ``(text, fixes)``。``fixes`` 每项含
        ``rule_id / checkpoint / from / to / position / severity /
        fixable / fixed / source``，可直接并入报告。
    """
    if not text:
        return (text or ""), []
    try:
        masked = _fence_ranges(text)

        def _in_mask(pos: int, end: int) -> bool:
            return any(a <= pos and end <= b for a, b in masked)

        applied: list = []
        for m in _BARE_CODE_RE.finditer(text):
            s, e = m.span()
            if _in_mask(s, e):
                continue
            base = _normalize_code_for_index(m.group(0))
            full = BASE_CODE_INDEX.get(base)
            if not full:
                continue
            applied.append((s, e, full, base))

        for s, e, full, _base in sorted(applied, key=lambda x: -x[0]):
            text = text[:s] + full + text[e:]

        fixes = [{
            "rule_id": "STD-03",
            "checkpoint": "标准编号未写年号",
            "from": _base,
            "to": _full,
            "position": _s,
            "severity": "warning",
            "fixable": True,
            "fixed": True,
            "source": "selfcheck_autofix",
        } for _s, _e, _full, _base in sorted(applied, key=lambda x: x[0])]

        # 歧义基号：报出但不改（年号无法唯一确定）
        seen: set = set()
        for m in _BARE_CODE_RE.finditer(text):
            if _in_mask(m.start(), m.end()):
                continue
            base = _normalize_code_for_index(m.group(0))
            if base in AMBIGUOUS_BASES and base not in seen:
                seen.add(base)
                fixes.append({
                    "rule_id": "STD-03",
                    "checkpoint": "标准编号存在多个年号版本",
                    "from": base,
                    "to": "",
                    "position": -1,
                    "severity": "warning",
                    "fixable": False,
                    "fixed": False,
                    "source": "selfcheck_autofix",
                    "suggestion": "同一基号在现行标准库中对应多个编号，"
                                  "年号无法唯一确定，请人工核实现行版本",
                })
        return text, fixes
    except Exception as exc:  # fail-soft：修复失败不影响正文落库
        logger.warning("裸标准编号自动修复异常（返回原文）: %s", exc,
                       exc_info=True)
        return text, []



def _normalize_code_for_index(code: str) -> str:
    """基号归一化：与 ``standards_registry.strip_standard_year`` 同口径。"""
    from app.services.standards_registry import strip_standard_year
    return strip_standard_year(code)


# ---------------------------------------------------------------------------
# 四、「检查点 → 生成约束 → 实现方式」映射表（唯一事实源，2026-10-02）
# ---------------------------------------------------------------------------
#: 12 类检查点分组（顺序即需求清单顺序）：``(分组 key, 中文说明)``
CHECKPOINT_GROUPS: tuple[tuple[str, str], ...] = (
    ("completeness", "内容完整性"),
    ("consistency", "事实一致性"),
    ("numbering", "章节编号"),
    ("charts", "图表要求"),
    ("language", "语言质量"),
    ("format", "格式规范"),
    ("logic", "逻辑连贯"),
    ("truthfulness", "数据真实性"),
    ("depth", "专业深度"),
    ("compliance", "合规性"),
    ("hazard", "危大工程"),
    ("other", "其他"),
)

#: 覆盖通道（封闭枚举）：检查点落在生成链路的哪一环
CHECKPOINT_CHANNELS: tuple[str, ...] = (
    "system_preprompt",     # 写入 system 硬约束（build_content_system_checkpoint_block）
    "chapter_preprompt",    # 逐章注入本章必含要素（build_chapter_checkpoint_block）
    "selfcheck",            # 生成后程序化自检（checkpoint_selfcheck）
    "autofix",              # 确定性自动修复 / 改写（零 AI 成本）
    "export_pipeline",      # 导出期兜底（编号统一、图号、公式、封面）
    "audit_fallback",       # 只能事后审核（AI 语义 / 章级聚合），生成侧刻意不前置
)

#: 跨章节（非「本章必含要素」）的检查点映射。
#: ``rule_ids`` 只作**指针**指向 audit_rules 注册表 —— 措辞与阈值一律不在
#: 这里复制（复制就会分叉）；锚点合法性由 :func:`validate_constraint_map` 锁定。
CROSS_CHAPTER_CONSTRAINTS: tuple[dict, ...] = (
    {"group": "completeness", "checkpoint": "九大法定章节齐备",
     "rule_ids": ("CMP-01", "CMP-02", "CMP-03", "CMP-04", "CMP-05", "CMP-06",
                  "CMP-07", "CMP-08", "CMP-09"),
     "constraint": "目录必须含九大法定章节骨架；本章属九章之一时不得整章只写概述性套话",
     "implementation": "目录生成保证九章骨架 + 通用约束第 1 条",
     "channel": "chapter_preprompt"},
    {"group": "consistency", "checkpoint": "数值口径唯一",
     "rule_ids": ("CON-01",),
     "constraint": "工期、深度、高度、强度等级、人员数量、设备型号、质保期等关键数值"
                   "全篇只允许一个口径，一律取自【全局事实变量】",
     "implementation": "全局事实逐章精选注入 + 通用约束第 3 条",
     "channel": "system_preprompt"},
    {"group": "consistency", "checkpoint": "术语统一",
     "rule_ids": ("CON-02",),
     "constraint": "同一事物全篇使用同一称谓（如「项目经理」不与「项目负责人」混用）",
     "implementation": "通用约束第 3 条", "channel": "system_preprompt"},
    {"group": "consistency", "checkpoint": "无占位标记与自相矛盾",
     "rule_ids": ("CON-03", "CON-04"),
     "constraint": "禁止输出【待补充】【待定】等占位写法；注入材料含占位符时按模糊"
                   "生成规则改写为完整表述",
     "implementation": "通用约束第 3 条 + 自检 CON-04 + rewrite_placeholder_marks 确定性改写",
     "channel": "selfcheck"},
    {"group": "consistency", "checkpoint": "禁止跨章重复段落",
     "rule_ids": ("CON-05",),
     "constraint": "不得把同一段表述复制进多个章节；主题相近也必须结合本章工艺与工程条件改写",
     "implementation": "通用约束第 5 条（事前约束）；跨章相似度比对仍归审核",
     "channel": "audit_fallback"},
    {"group": "numbering", "checkpoint": "三级编号层级与跳级",
     "rule_ids": ("DLV-12",),
     "constraint": "一级一/二/三、二级 1.1、三级 1.1.1，不得跳级跳号；编号由导出器统一生成",
     "implementation": "numbering.normalize_section_content_subheadings + 导出器编号统一",
     "channel": "export_pipeline"},
    {"group": "numbering", "checkpoint": "标题层级",
     "rule_ids": ("DLV-13",),
     "constraint": "章节标题层级与目录层级严格对应，不跳级",
     "implementation": "导出期标题层级映射", "channel": "export_pipeline"},
    {"group": "numbering", "checkpoint": "正文小节序号",
     "rule_ids": ("DLV-14", "CON-05"),
     "constraint": "小节序号用 (1)(2)(3) 或 ①②③，不与章节编号冲突、不重复",
     "implementation": "build_subheading_rule（{subheading_rule} 占位符注入）",
     "channel": "system_preprompt"},
    {"group": "charts", "checkpoint": "图表类型选择",
     "rule_ids": ("DLV-04",),
     "constraint": "工艺流程 / 施工平面 / 组织机构 / 进度用 Mermaid；参数对比用 chart-json",
     "implementation": "通用约束第 6 条 + chart_validators 类型校验",
     "channel": "system_preprompt"},
    {"group": "charts", "checkpoint": "图号统一",
     "rule_ids": ("DLV-04",),
     "constraint": "正文不得写死图号，统一写「图 1/图 2」递增，实际图号由导出器渲染",
     "implementation": "通用约束第 6 条 + 导出器图号重排",
     "channel": "export_pipeline"},
    {"group": "charts", "checkpoint": "图表节点数",
     "rule_ids": ("DLV-04",),
     "constraint": "单图节点数控制在 4~16，超过则拆多张图",
     "implementation": "通用约束第 6 条 + chart_validators 节点数校验",
     "channel": "system_preprompt"},
    {"group": "language", "checkpoint": "口语化与 AI 腔",
     "rule_ids": ("DLV-06",),
     "constraint": "不用「首先/其次/最后」堆砌、不用 emoji、不用「总而言之」等客套",
     "implementation": "通用约束第 7 条 + content_polish 清理",
     "channel": "system_preprompt"},
    {"group": "language", "checkpoint": "空话套话",
     "rule_ids": ("DLV-06",),
     "constraint": "禁止「确保质量」「高度重视」「加强管理」等无信息量表述",
     "implementation": "通用约束第 7 条；语义级判定仍归 AI 审核",
     "channel": "audit_fallback"},
    {"group": "format", "checkpoint": "Markdown 围栏",
     "rule_ids": ("DLV-05",),
     "constraint": "``` / ~~~ 必须成对闭合，代码块与正文不混排",
     "implementation": "通用约束第 7 条 + auto_fix_unclosed_fences 兜底",
     "channel": "autofix"},
    {"group": "format", "checkpoint": "表格规范",
     "rule_ids": ("DLV-05",),
     "constraint": "表格必须有表头与分隔行，列对齐，不出现空表格",
     "implementation": "通用约束第 7 条", "channel": "system_preprompt"},
    {"group": "logic", "checkpoint": "章节衔接",
     "rule_ids": ("DLV-06",),
     "constraint": "章节开头交代与前章的关系，避免突兀起笔；不重复前章已述内容",
     "implementation": "上级章节要点 + 同级章节提示 + 前序同级结尾注入 user 上下文",
     "channel": "system_preprompt"},
    {"group": "logic", "checkpoint": "章节正文非空",
     "rule_ids": ("CMP-01", "CMP-09"),
     "constraint": "每个叶子章节必须生成实质正文，不得只有标题",
     "implementation": "字数预算（word_budget）+ 续写轮补齐",
     "channel": "system_preprompt"},
    {"group": "truthfulness", "checkpoint": "数据可追溯",
     "rule_ids": ("CON-01", "CON-03"),
     "constraint": "所有数值、材料、设备、人员信息必须与【全局事实变量】一致且可回溯",
     "implementation": "逐章相关事实精选注入（_filter_facts_rows + _render_facts_text）",
     "channel": "system_preprompt"},
    {"group": "truthfulness", "checkpoint": "严禁编造",
     "rule_ids": ("CON-03", "TRC-02"),
     "constraint": "事实未提供的参数按模糊生成规则写条件式表述（技术原则 / 按设计文件 / "
                   "范围值），不得编造具体数值",
     "implementation": "内容标准系统提示 + 模糊生成规则注入 + 自检不产出占位符",
     "channel": "system_preprompt"},
    {"group": "depth", "checkpoint": "计算链条完整",
     "rule_ids": ("TRC-01", "TRC-02", "CMP-09"),
     "constraint": "计算书必须给出「计算依据 → 参数取值及来源 → 公式与过程 → 结论与"
                   "安全系数」完整链条，只有结论没有过程视为缺失",
     "implementation": "build_chapter_checkpoint_block（calc_drawings 章）",
     "channel": "chapter_preprompt"},
    {"group": "depth", "checkpoint": "措施针对性",
     "rule_ids": ("SAF-02", "CMP-05"),
     "constraint": "安全技术措施须结合本工程条件逐项给出，不得只写通用套话",
     "implementation": "build_chapter_checkpoint_block（safety 章）；针对性语义判定归审核",
     "channel": "audit_fallback"},
    {"group": "compliance", "checkpoint": "标准引用现行有效",
     "rule_ids": ("STD-01",),
     "constraint": "只引用现行有效标准，禁止引用已废止版本",
     "implementation": "通用约束第 2 条 + standards_registry 现行标准清单注入",
     "channel": "system_preprompt"},
    {"group": "compliance", "checkpoint": "库外标准准确",
     "rule_ids": ("STD-02",),
     "constraint": "引用标准清单外的标准必须给出准确编号与现行年号，不得编造编号",
     "implementation": "通用约束第 2 条", "channel": "system_preprompt"},
    {"group": "compliance", "checkpoint": "标准编号含年号",
     "rule_ids": ("STD-03",),
     "constraint": "首次引用标准写全「《名称》（编号-年号）」，禁止裸编号"
                   "（如「GB 50210」不带年号）",
     "implementation": "通用约束第 2 条 + 自检 STD-03 + fix_bare_standard_codes 确定性补年号",
     "channel": "autofix"},
    {"group": "compliance", "checkpoint": "危大法规引用",
     "rule_ids": ("STD-04",),
     "constraint": "危大工程方案的编制依据必须列出住建部令第37号与建办质〔2018〕31号",
     "implementation": "build_chapter_checkpoint_block（basis 章，hazard_only）",
     "channel": "chapter_preprompt"},
    {"group": "compliance", "checkpoint": "专业类别依据",
     "rule_ids": ("STD-05",),
     "constraint": "编制依据须覆盖本方案专业类别对应标准（按四层列齐）",
     "implementation": "build_chapter_checkpoint_block（basis 章）+ 标准清单按方案类型注入",
     "channel": "audit_fallback"},
    {"group": "hazard", "checkpoint": "危大判定与规模参数",
     "rule_ids": ("STD-04",),
     "constraint": "明确危大工程范围与判定依据，写明本方案的规模参数",
     "implementation": "危大追加约束（is_hazardous 时注入）",
     "channel": "system_preprompt"},
    {"group": "hazard", "checkpoint": "监测监控与预警值",
     "rule_ids": ("SAF-06",),
     "constraint": "需监测的危大工程须明确监测项目、点位布置、监测频次与预警值",
     "implementation": "build_chapter_checkpoint_block（safety 章）",
     "channel": "chapter_preprompt"},
    {"group": "other", "checkpoint": "生成与审核同词表",
     "rule_ids": ("DLV-08",),
     "constraint": "正文生成即按 CMP/STD/SAF/CON/TRC/DLV 同一规则词表约束与自检",
     "implementation": "检查点前置（本模块唯一事实源）", "channel": "selfcheck"},
    {"group": "other", "checkpoint": "专家论证提示",
     "rule_ids": ("DLV-09",),
     "constraint": "超过一定规模的危大工程须写明需组织专家论证及相关验算依据",
     "implementation": "危大追加约束", "channel": "system_preprompt"},
)


# ---------------------------------------------------------------------------
# 三之三、CON-04 占位标记改写（确定性自动改写，2026-10-02）
# ---------------------------------------------------------------------------
#: 占位标记改写的替代短语 —— **条件式表述**，不引入任何具体数值。
#: 与「模糊生成规则」的既有口径一致（技术原则 / 条件式 / 按设计文件确定
#: 属允许表达，编造数值与留占位符才是不允许）。
_PLACEHOLDER_CONDITIONAL = "按设计文件及现场实际确定"

#: 占位标记紧随其后的单位 —— 改写时保留为括号注记，避免遗留孤立单位
#
# ✅ 2026-10-02（生产库 CON-04 证据驱动）：此前**只收拉丁计量单位**，中文
#    单位全部漏收，改写后遗留孤立单位、句子读不通（生产 findings 实证）：
#      「堆放区面积【待补充：堆放区面积】平方米」→「…确定平方米」
#      「每周清运不少于【待补充：清运频次】次」  →「…确定次」
#    现补齐中文计量单位。⚠️ 补的是**无歧义**的：
#      - `平方米`/`立方米`/`公斤`/`小时`/`米`/`遍`：不存在以它们起首的常用词，
#        紧随占位标记时几乎必然是单位；
#      - `次` 加了 `(?!日)` 守卫：`次日`（next day）是常用词，不加守卫会把
#        「【待定】次日恢复施工」改写成「…（次）日恢复施工」。
#    刻意**不补** `周`/`月`：`周边`/`月末` 等词会以它们起首，误收风险高于收益。
_PLACEHOLDER_TRAILING_UNIT_RE = re.compile(
    r"^\s*(m²|㎡|m³|mm|cm|km|MPa|kPa|kN·m|kN|kg|t|%|元|万元|天|日历天|人|台|套|个|件"
    r"|平方米|立方米|公斤|小时|米|遍"
    r"|次(?!日)"
    r"|m(?![A-Za-z0-9]))")


def rewrite_placeholder_marks(text: str) -> tuple:
    """把占位标记（CON-04）改写为条件式完整表述，返回 ``(新正文, 清单)``。

    对应审核规则 ``CON-04``（生产 findings 实测：**88 处占位标记 / 64 个字段**，
    是全部审核问题中体量最大的一项）。这是闭环中除 STD-03 外**唯一可零风险
    自动处置**的高频缺陷，逐条红线：

    - 检测**复用** :data:`PLACEHOLDER_MARK_PATTERNS`（与自检、与
      ``standard_report`` **同一**判据，不另立词表）；
    - 只改写 ``rewritable=True`` 的 ``【…】`` / ``[待补充…]`` 形态 → 条件式表述
      （统一走 :func:`placeholder_rewritable_patterns` 出口，不手抄正则）；
    - ``××`` / 裸 ``TBD`` 形态**不自动改写**（上下文不明，改错比不改更糟），
      只报出待人工复核（``fixable=False``）；
    - 改写时保留紧随其后的单位（「深度【待定】m」→「深度按设计文件及现场
      实际确定（m）」），避免遗留孤立单位；
    - 围栏代码块内不动；**幂等**；fail-soft。
    """
    if not text:
        return (text or ""), []
    try:
        masked = _fence_ranges(text)

        def _in_mask(start: int, end: int) -> bool:
            return any(a <= start and end <= b for a, b in masked)

        pending: list = []
        # ① 只报不改的形态（×× / 裸 TBD / N/A）：上下文不明，自动改写会误伤
        #    正常正文，故不碰正文，但仍须报出待人工复核（fixable=False）。
        for rx in placeholder_nonrewritable_patterns():
            for m in rx.finditer(text):
                if _in_mask(m.start(), m.end()):
                    continue
                frag = m.group(0)
                if frag not in pending:
                    pending.append(frag)
        # ② 可确定性改写的形态（【…】/ [待补充…]）
        hits = []
        for rx in placeholder_rewritable_patterns():
            for m in rx.finditer(text):
                if not _in_mask(m.start(), m.end()):
                    hits.append(m)
        # 按起点排序；同起点保留最长匹配，并跳过与上一条重叠的命中。
        # ⚠️ 收敛判据后多条正则有包含关系（如【待补充：宽度】与更宽形态），
        # 不去重会按短匹配先替换、再让长匹配的定位错位，把正常正文一起改坏。
        hits.sort(key=lambda m: (m.start(), -m.end()))
        _kept: list = []
        for m in hits:
            if _kept and m.start() < _kept[-1].end():
                continue
            _kept.append(m)
        hits = _kept

        out: list = []
        pos = 0
        fixes: list = []
        for m in hits:
            frag = m.group(0)
            head = frag.lstrip()[:1]
            if head not in ("【", "["):
                # 防御兜底：改写子集本应全是括号形态；若上游新增形态时未同步
                # rewritable 标记，此处宁可只报不改，也不能盲改正文。
                if frag not in pending:
                    pending.append(frag)
                continue
            tail = text[m.end():]
            um = _PLACEHOLDER_TRAILING_UNIT_RE.match(tail)
            unit, end = "", m.end()
            if um:
                unit = um.group(1).strip()
                end = m.end() + um.end()  # um 基于 tail，须回加 m.end() 才是绝对偏移
            repl = _PLACEHOLDER_CONDITIONAL + (f"（{unit}）" if unit else "")
            out.append(text[pos:m.start()])
            out.append(repl)
            pos = end
            fixes.append({
                "rule_id": "CON-04",
                "checkpoint": "占位标记残留",
                "from": frag.strip(),
                "to": repl,
                "position": m.start(),
                "severity": "error",
                "fixable": True,
                "fixed": True,
                "source": "selfcheck_autofix",
            })
        out.append(text[pos:])
        return "".join(out), fixes + [{
            "rule_id": "CON-04",
            "checkpoint": "占位标记残留（上下文不明形态，需人工改写）",
            "from": p,
            "to": "",
            "position": -1,
            "severity": "error",
            "fixable": False,
            "fixed": False,
            "source": "selfcheck_autofix",
            "suggestion": "×× / XX / TBD / N/A 等上下文不明形态无法确定性改写；"
                          "请人工补全或按模糊生成规则改写为条件式表述",
        } for p in pending]
    except Exception as exc:  # fail-soft：改写失败不影响正文落库
        logger.warning("占位标记自动改写异常（返回原文）: %s", exc, exc_info=True)
        return text, []



def infer_chapter_key(title: str, primary_key: str = "") -> str:
    """章节标题 → 九大章节 key（**主判据优先**，本模块补充表兜底）。

    Args:
        title: 章节标题（可带目录编号前缀）。
        primary_key: 调用方已按主判据（``chapter_key_of_title``）解析出的 key；
            非空时**直接返回、不经过本表**（只补不覆盖，杜绝覆盖既有归类）。

    Returns:
        九大章节 key；均未命中返回空串（调用方按「无章节归属」处理）。

    为什么单独存在而不是改 ``chapter_key_of_title``：后者是**结构化提取按章
    注入**与**全局事实按章前置**共用的分类器（§4.11 / §4.15 的既有契约），
    改它的返回会连带改变提取注入与事实注入的行为面。本模块只服务于
    「审核检查点」，需要一个更宽的标题覆盖面（应急 / 临电 / 消防等小节），
    故在此独立补表，判据与主判据解耦、互不影响。
    """
    key = (primary_key or "").strip()
    if key:
        return key
    t = (str(title).strip() if title else "")
    if not t:
        return ""
    for kw, ckey in _SUPPLEMENT_ORDERED:
        if kw in t:
            return ckey
    return ""


# ---------------------------------------------------------------------------
# 五、映射表访问与校验
# ---------------------------------------------------------------------------
def checkpoint_constraint_map() -> list[dict]:
    """返回「检查点 → 生成约束 → 实现方式」映射表（跨章条目 + 章节派生条目）。

    跨章条目来自 :data:`CROSS_CHAPTER_CONSTRAINTS`；本章必含要素条目
    **派生自** :data:`CHAPTER_CHECKPOINT_REQUIREMENTS`（``constraint`` 直接取
    该要求的 ``note``）—— **不另写一份措辞**，这是本表不产生判据分叉的关键。

    每条含 ``group / checkpoint / rule_ids / constraint / implementation /
    channel / chapter_key``（跨章条目的 ``chapter_key`` 为空串）。
    """
    rows: list[dict] = []
    for c in CROSS_CHAPTER_CONSTRAINTS:
        rows.append({
            "group": c["group"],
            "checkpoint": c["checkpoint"],
            "rule_ids": list(c["rule_ids"]),
            "constraint": c["constraint"],
            "implementation": c["implementation"],
            "channel": c["channel"],
            "chapter_key": "",
        })
    for ckey, spec in CHAPTER_CHECKPOINT_REQUIREMENTS.items():
        for r in spec["requirements"]:
            rows.append({
                "group": "completeness",
                "checkpoint": f"{spec['title']}·{r['label']}",
                "rule_ids": list(r["rule_ids"]),
                "constraint": r["note"],
                "implementation": ("build_chapter_checkpoint_block 逐章注入"
                                   "（hazard_only，仅危大方案）"
                                   if r.get("hazard_only")
                                   else "build_chapter_checkpoint_block 逐章注入"),
                "channel": "chapter_preprompt",
                "chapter_key": ckey,
            })
    return rows


def validate_constraint_map() -> list[str]:
    """校验映射表完整性，返回问题清单（空 = 全部合法）。

    护栏：
    - 每条 ``rule_id`` 必须真实存在于 audit_rules 注册表（规则废弃即失败）；
    - ``group`` 必须在 12 类清单内，且 12 类**全部**至少有一条映射；
    - ``channel`` 必须在 :data:`CHECKPOINT_CHANNELS` 内。

    与 :func:`validate_rule_anchoring` 同源合并 —— 两处校验合起来锁定
    「检查点 ↔ 生成约束 ↔ 审核规则」三者的锚点关系。
    """
    problems: list[str] = list(validate_rule_anchoring())
    group_keys = {k for k, _ in CHECKPOINT_GROUPS}
    channels = set(CHECKPOINT_CHANNELS)
    for c in CROSS_CHAPTER_CONSTRAINTS:
        if c["group"] not in group_keys:
            problems.append(f"跨章条目 {c['checkpoint']!r}: 分组 {c['group']!r} 不在 12 类清单")
        if c["channel"] not in channels:
            problems.append(f"跨章条目 {c['checkpoint']!r}: 通道 {c['channel']!r} 非法")
        if not c.get("rule_ids"):
            problems.append(f"跨章条目 {c['checkpoint']!r}: 缺少 rule_ids 锚点")
        for rid in c["rule_ids"]:
            if get_rule(rid) is None:
                problems.append(f"跨章条目 {c['checkpoint']!r}: 规则 {rid} 不在审核注册表")
    covered = {c["group"] for c in CROSS_CHAPTER_CONSTRAINTS}
    for gk, gname in CHECKPOINT_GROUPS:
        if gk not in covered:
            problems.append(f"分组 {gname}({gk}) 无任何检查点映射")
    return problems


__all__ = [
    "CHAPTER_CHECKPOINT_REQUIREMENTS",
    "CHAPTER_TITLE_SUPPLEMENT",
    "CHECKPOINT_CHANNELS",
    "CHECKPOINT_GROUPS",
    "CROSS_CHAPTER_CONSTRAINTS",
    "AMBIGUOUS_BASES",
    "BASE_CODE_INDEX",
    "build_chapter_checkpoint_block",
    "build_chapter_element_block",
    "build_content_system_checkpoint_block",
    "chapter_required_elements",
    "checkpoint_constraint_map",
    "checkpoint_selfcheck",
    "cross_section_copy_findings",
    "fix_bare_standard_codes",
    "infer_chapter_key",
    "is_hazardous_scheme",
    "rewrite_placeholder_marks",
    "validate_constraint_map",
    "validate_rule_anchoring",
]


