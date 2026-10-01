"""审核规则注册表（审核与预检模块的**唯一事实源**）

背景
----
此前审核规则分散在三处各自维护，存在事实分叉风险：

1. 前端 ``DEFAULT_COMPLIANCE_CHECKLIST``（10 条自由文本，用户可改，无 ID）；
2. 后端 ``routers/compliance.py::EXPERT_CHECK_ITEMS``（10 项论证必要项）；
3. 提示词 ``prompts/analysis.py::expert_review_system`` 内联的同一份清单文案。

三处文案一旦漂移，前端展示的"已通过"与 AI 实际判定的"已通过"就不是同一件事。
本模块把规则收敛为**带稳定 ID 的结构化条目**，前端 / 后端 / 提示词三方共用同一份
``rule_id``，使历史检查结果可跨版本比对（趋势分析的前提）。

行业依据
--------
规则条目并非凭空设定，逐条对应现行有效法规与标准：

- 《危险性较大的分部分项工程安全管理规定》（住房和城乡建设部令第37号，2019年修正）
  第十七条：专项施工方案应当包括 ①工程概况 ②编制依据 ③施工计划 ④施工工艺技术
  ⑤安全保证措施 ⑥施工管理及作业人员配备和分工 ⑦验收要求 ⑧应急处置措施
  ⑨计算书及相关图纸。
- 《关于实施〈危险性较大的分部分项工程安全管理规定〉有关问题的通知》
  （建办质〔2018〕31号）附件二：专家论证主要内容 ——
  ①方案内容是否完整、可行；②方案计算书和验算依据是否符合有关标准规范；
  ③安全施工的基本条件是否满足现场实际情况。
- 《建筑施工组织设计规范》GB/T 50502-2009（专项方案章节编排与内容深度）。
- 《生产经营单位生产安全事故应急预案编制导则》GB/T 29639-2020
  （应急组织机构及职责 / 应急物资装备保障 / 应急预案演练 —— 应急处置措施三要素）。
- 《建筑与市政施工现场安全卫生与职业健康通用规范》GB 55034-2022（全文强制）。
- 《建筑与市政工程施工质量控制通用规范》GB 55032-2022（全文强制，验收要求）。
- 《建设工程项目管理规范》GB/T 50903-2013（人员配备与岗位职责）。

维护约定
--------
1. 新增 / 修改规则必须同步 ``RULE_VERSION``，否则历史结果会与新规则混在一起比对。
2. ``rule_id`` 一经发布不得复用（废弃请设 ``deprecated=True`` 并保留条目），
   否则历史记录里的同 ID 会指向完全不同的语义。
3. ``basis`` 必须写明具体条文出处，不得写"根据现行规范"这类无法核实的描述。
4. **（2026-09-27 新增）** 本模块的"唯一事实源"地位必须**可验证**，否则承诺形同虚设：
   - 导出预检在 ``routers/export.py::_EXPORT_ISSUE_RULE_MAP`` 引用本表规则，
     ``preflight_engine`` 产出规则 —— 两侧任一漏登都会让 finding 落到兜底分支
     （title 退化为 ID 本身、basis / suggestion 全空、dimension 塌陷为空串，
     进而使 ``audit_scoring`` 把扣分记到错误维度，总分系统性偏移）。
   - 护栏：``validate_rule_registry(mapping, emitted_rule_ids)`` 做双向校验，
     回归用例见 ``tests/test_audit_fixes_20260927.py::TestRuleRegistryConsistency``。
     新增规则时须同步登记 ``_PROGRAM_EMITTED_RULE_IDS``（引擎产出的基编号全集）。
   - ⚠️ 本模块属 services 层，**禁止 import routers**（分层约束），故校验所需的
     映射表由调用方传入。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 规则集版本：规则增删改后 +1（前端据此提示"规则已更新，建议重新预检"）
# ---------------------------------------------------------------------------
RULE_VERSION = "1.7.0"  # 2026-10-01：新增 CON-06 跨章节段落搬运检测（骨架归一 + 双阈值 Dice）；
                        #             并修正 CON-05 的行业依据引用（§3.12.4 → §3.12.3）
                       #             导致 high 级问题无标题/无依据/无修复建议）
# 2026-09-27：1.6.0 —— 补登记 DLV-13/DLV-14（此前由 export.py 产出但注册表缺失，
                        #             导致 high 级问题无标题/无依据/无修复建议）
# 2026-09-25：1.5.0 —— 新增 DLV-15 全局事实门控（未确认/模拟/冲突/来源过期）
# 2026-09-23：1.4.0 —— TRC-01 改为只报"个别章节缺计算过程"，与 CMP-09 聚合口径互斥（跨维度双扣修复）
# 2026-09-23：1.3.0 —— CON-02/CON-03 改判 ai 通道（程序化引擎无实现，死规则修复）
# 2026-09-21：1.2.0 —— 新增 DLV-09~DLV-12（导出预检→就绪度总检共用规则词表，G1）
# 2026-09-21：1.1.0 —— CON-05 输出改为 CON-05-N 编号，修复 merge_findings 塌缩

# 严重度：block > high > medium > low
# block 为**交付阻断项**——命中即不允许标记"可交付"（如引用已废止标准、缺计算书）
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "block": 3}

SEVERITY_LABEL = {"low": "提示", "medium": "一般", "high": "严重", "block": "阻断"}

# 检查方式：program=纯程序化判定（离线、秒级）；ai=交由 AI 语义判定
CHECK_MODE_PROGRAM = "program"
CHECK_MODE_AI = "ai"


@dataclass(frozen=True)
class AuditRule:
    """一条审核规则。

    Attributes:
        rule_id: 稳定规则编号（如 ``CMP-01``），跨版本不得复用。
        dimension: 所属评分维度（见 ``DIMENSIONS``）。
        title: 规则标题（面向用户的中文短句）。
        detail: 判定口径说明（什么情况下算命中）。
        severity: 命中时的严重度（low / medium / high / block）。
        mode: 检查方式（program / ai）。
        basis: 行业依据（法规条款或标准编号，须可核实）。
        keywords: 程序化判定用的关键词（章节标题命中即视为"已覆盖"）。
        deprecated: 已废弃（保留用于解析历史记录，不再参与检查）。
    """

    rule_id: str
    dimension: str
    title: str
    detail: str = ""
    severity: str = "medium"
    mode: str = CHECK_MODE_AI
    basis: str = ""
    keywords: tuple[str, ...] = ()
    deprecated: bool = False

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "dimension": self.dimension,
            "title": self.title,
            "detail": self.detail,
            "severity": self.severity,
            "mode": self.mode,
            "basis": self.basis,
            "keywords": list(self.keywords),
            "deprecated": self.deprecated,
        }


# ---------------------------------------------------------------------------
# 评分维度（六维，权重合计 100）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    weight: int
    desc: str


DIMENSIONS: tuple[Dimension, ...] = (
    Dimension("completeness", "内容完整性", 25,
              "住建部令第37号第十七条规定的专项方案九项内容是否齐备"),
    Dimension("compliance", "规范符合性", 20,
              "编制依据是否引用现行有效标准，是否误引已废止版本"),
    Dimension("safety", "安全措施有效性", 20,
              "风险源识别、安全技术措施、应急预案与监测方案是否有针对性"),
    Dimension("consistency", "全文一致性", 15,
              "数值 / 单位 / 术语 / 时间口径在全文中是否前后一致"),
    Dimension("traceability", "可追溯性", 10,
              "关键工序是否有设计计算书与验算依据（建办质31号论证要点②）"),
    Dimension("deliverability", "可交付性", 10,
              "空章节、字数达标度、图表完成率、乱码与控制字符等交付形态问题"),
)

DIMENSION_MAP: dict[str, Dimension] = {d.key: d for d in DIMENSIONS}

# 维度扣分权重：命中严重度 → 该维度扣分（维度满分 100，扣至 0 为止）
SEVERITY_PENALTY = {"block": 40, "high": 20, "medium": 8, "low": 2}

# 等级阈值（总分 0-100）
GRADE_BANDS = (
    (90, "A", "可直接交付"),
    (75, "B", "可提交论证，建议优化"),
    (60, "C", "需整改后重新预检"),
    (0, "D", "不具备交付条件"),
)


def grade_of(score: float) -> tuple[str, str]:
    """按总分返回 (等级, 结论文案)。"""
    for floor, letter, desc in GRADE_BANDS:
        if score >= floor:
            return letter, desc
    return "D", GRADE_BANDS[-1][2]


_BASIS_37 = "《危险性较大的分部分项工程安全管理规定》（住建部令第37号）第十七条"
_BASIS_31 = "《关于实施〈危险性较大的分部分项工程安全管理规定〉有关问题的通知》（建办质〔2018〕31号）附件二"
_BASIS_GB55034 = "GB 55034-2022《建筑与市政施工现场安全卫生与职业健康通用规范》"
_BASIS_GB55032 = "GB 55032-2022《建筑与市政工程施工质量控制通用规范》"
_BASIS_GB_T_29639 = "GB/T 29639-2020《生产经营单位生产安全事故应急预案编制导则》"
_BASIS_GB_T_50903 = "GB/T 50903-2013《建设工程项目管理规范》"
_BASIS_GB_T_50502 = "GB/T 50502-2009《建筑施工组织设计规范》"


# ---------------------------------------------------------------------------
# 一、内容完整性（completeness）—— 专项方案九项法定内容
# ---------------------------------------------------------------------------
_COMPLETENESS_RULES: tuple[AuditRule, ...] = (
    AuditRule("CMP-01", "completeness", "工程概况完整",
              "应说明工程基本情况、周边环境（地质、水文、邻近建（构）筑物、管线）与危大工程特点",
              "high", CHECK_MODE_PROGRAM,
              _BASIS_37, ("工程概况", "工程基本", "周边环境")),
    AuditRule("CMP-02", "completeness", "编制依据现行有效",
              "应列明法律、法规、规范性文件、标准、规范及施工图设计文件、施工组织设计等",
              "block", CHECK_MODE_PROGRAM,
              _BASIS_37, ("编制依据", "编制说明", "依据", "法律法规")),
    AuditRule("CMP-03", "completeness", "施工计划明确",
              "应包含施工进度计划、材料与设备计划（规格、数量、进场时间）",
              "high", CHECK_MODE_PROGRAM,
              _BASIS_37, ("施工计划", "进度计划", "施工进度", "材料计划", "设备计划")),
    AuditRule("CMP-04", "completeness", "施工工艺技术完整",
              "应包含技术参数、工艺流程、施工方法、操作要求、检查要求等，工序可落地执行",
              "high", CHECK_MODE_PROGRAM,
              _BASIS_37, ("施工工艺", "工艺技术", "工艺流程", "施工方法", "操作要求")),
    AuditRule("CMP-05", "completeness", "安全保证措施有针对性",
              "应包含组织保障措施、技术措施、监测监控措施，且针对本工程主要风险源",
              "block", CHECK_MODE_PROGRAM,
              _BASIS_37, ("安全保证", "安全措施", "安全保障", "安全保证措施")),
    AuditRule("CMP-06", "completeness", "人员配备与分工明确",
              "应明确施工管理人员、专职安全生产管理人员、特种作业人员及其他作业人员的配备与分工",
              "high", CHECK_MODE_PROGRAM,
              _BASIS_37, ("人员分工", "人员配备", "作业人员", "岗位职责", "管理人员")),
    AuditRule("CMP-07", "completeness", "验收要求明确",
              "应明确验收标准、验收程序、验收内容、验收人员，检验批划分符合规范",
              "high", CHECK_MODE_PROGRAM,
              _BASIS_37 + "；" + _BASIS_GB55032,
              ("验收要求", "验收标准", "验收程序", "检验批")),
    AuditRule("CMP-08", "completeness", "应急处置措施完备",
              "应含应急组织机构及职责、应急物资装备保障、应急预案演练要求",
              "block", CHECK_MODE_PROGRAM,
              _BASIS_37 + "；" + _BASIS_GB_T_29639,
              ("应急处置", "应急预案", "应急措施", "应急组织")),
    AuditRule("CMP-09", "completeness", "计算书及相关图纸齐全",
              "应附具安全验算结果（计算书）与相关图纸；无计算过程视为缺失",
              "block", CHECK_MODE_PROGRAM,
              _BASIS_37 + "；" + _BASIS_31 + "论证要点②",
              ("计算书", "验算", "受力计算", "附图", "相关图纸")),
)

# ---------------------------------------------------------------------------
# 二、规范符合性（compliance）
# ---------------------------------------------------------------------------
_COMPLIANCE_RULES: tuple[AuditRule, ...] = (
    AuditRule("STD-01", "compliance", "引用的标准编号均为现行有效版本",
              "正文不得出现已废止 / 已被替代的标准编号",
              "block", CHECK_MODE_PROGRAM,
              "现行标准库 ABOLISHED_STANDARDS（住房城乡建设部公告 / 全国标准信息公共服务平台）"),
    AuditRule("STD-02", "compliance", "已引用全文强制性工程建设规范",
              "涉及施工安全与质量控制的章节应引用 GB 55xxx 系列全文强制规范",
              "high", CHECK_MODE_PROGRAM,
              _BASIS_GB55034 + "；" + _BASIS_GB55032),
    AuditRule("STD-03", "compliance", "引用的标准编号在现行标准库内可查",
              "避免编造标准编号；清单外引用须给出编号与现行年号",
              "medium", CHECK_MODE_PROGRAM,
              "现行标准库 BASE_STANDARDS / CATEGORY_STANDARDS"),
    AuditRule("STD-04", "compliance", "引用了法规与规范性文件",
              "危大工程方案应引用住建部令第37号、建办质〔2018〕31号等法定依据",
              "medium", CHECK_MODE_PROGRAM,
              _BASIS_37 + "；" + _BASIS_31),
    AuditRule("STD-05", "compliance", "编制依据覆盖本工程专业类别",
              "基坑 / 模板 / 脚手架 / 起重机械等应按专业引用对应的现行专业技术标准",
              "high", CHECK_MODE_AI,
              "现行标准库 CATEGORY_STANDARDS（按方案类型命中）"),
)

# ---------------------------------------------------------------------------
# 三、安全措施有效性（safety）
# ---------------------------------------------------------------------------
_SAFETY_RULES: tuple[AuditRule, ...] = (
    AuditRule("SAF-01", "safety", "已识别主要风险源",
              "应有危险源辨识 / 风险源清单，且覆盖本工程主要风险",
              "high", CHECK_MODE_AI, _BASIS_GB55034),
    AuditRule("SAF-02", "safety", "安全技术措施有针对性",
              "措施应针对识别出的风险源逐项对应，而非通用模板套话",
              "high", CHECK_MODE_AI, _BASIS_37 + "第十七条第（五）项"),
    AuditRule("SAF-03", "safety", "应急预案含组织机构与职责",
              "应明确应急组织机构组成与各岗位职责",
              "high", CHECK_MODE_PROGRAM,
              _BASIS_GB_T_29639, ("组织机构", "应急组织", "指挥", "职责")),
    AuditRule("SAF-04", "safety", "应急预案含物资装备保障",
              "应列出应急物资与装备清单（种类、数量、存放位置）",
              "high", CHECK_MODE_PROGRAM,
              _BASIS_GB_T_29639, ("物资", "装备", "应急物资", "救援器材")),
    AuditRule("SAF-05", "safety", "应急预案含演练要求",
              "应明确演练频次、组织方式与记录要求",
              "medium", CHECK_MODE_PROGRAM,
              _BASIS_GB_T_29639, ("演练", "应急演练", "演习")),
    AuditRule("SAF-06", "safety", "危大工程含监测监控方案",
              "需监测的危大工程应明确监测项目、点位、频次与预警值",
              "high", CHECK_MODE_PROGRAM,
              _BASIS_37 + "第十七条第（五）项监测监控措施",
              ("监测", "监控", "变形监测", "预警值")),
    AuditRule("SAF-07", "safety", "特种作业人员持证要求明确",
              "涉及特种作业的应明确持证上岗要求",
              "medium", CHECK_MODE_AI,
              "《建筑施工特种作业人员管理规定》（建质〔2008〕75号）"),
)

# ---------------------------------------------------------------------------
# 四、全文一致性（consistency）
# ---------------------------------------------------------------------------
_CONSISTENCY_RULES: tuple[AuditRule, ...] = (
    AuditRule("CON-01", "consistency", "关键数值前后一致",
              "工期、深度、高度、混凝土强度等级等数值在全文口径一致",
              "high", CHECK_MODE_PROGRAM, _BASIS_GB_T_50502),
    # ✅ 死规则修复（2026-09-23）：CON-02/CON-03 此前注册为 program 但
    #    preflight_engine 无任何实现，永远不产出结论（既不报命中也不报未命中）。
    #    术语统一与时间口径逻辑本质是语义判断，改判 ai 通道并入 /check 清单。
    AuditRule("CON-02", "consistency", "术语与工程名称前后一致",
              "同一实体（如设备型号、人名、部位名称）在全文中称谓统一",
              "medium", CHECK_MODE_AI, _BASIS_GB_T_50502),
    AuditRule("CON-03", "consistency", "时间口径一致",
              "进度计划、里程碑与设备进退场时间不存在逻辑冲突",
              "high", CHECK_MODE_AI, _BASIS_GB_T_50502),
    AuditRule("CON-04", "consistency", "与项目关键事实一致",
              "正文表述应与「全局事实」中已确认的事实值一致",
              "high", CHECK_MODE_AI, "全局事实为唯一可信数据源"),
    AuditRule("CON-05", "consistency", "无章节内容重复",
              "不同章节之间不应存在大段雷同（整章相似度 > 80%）",
              "medium", CHECK_MODE_PROGRAM,
              "《产品需求文档》§3.12.3 程序化预检（含 Jaccard 相似度查重）"),
    # ✅ 新增（2026-10-01）：CON-05 只做**整章级** 4-gram Jaccard，
    #    两章整体不同时漏掉「成段照抄」（AI 生成最常见的雷同形态：
    #    同一段「安全保证措施」被复制到多个章节）。CON-06 用**骨架归一**
    #    （数字/日期/标准号 → 占位符）+ 双阈值 Dice 在**段落级**补上，
    #    抓「换了数字的照抄」这类整章比对必然漏报的场景。
    AuditRule("CON-06", "consistency", "无跨章节段落搬运",
              "不同章节之间不应存在成段照抄（骨架归一后相似度达阈值，"
              "含仅改数字 / 日期的变形）",
              "medium", CHECK_MODE_PROGRAM,
              "《产品需求文档》§3.12.3 程序化预检（含相似度查重）"),
)

# ---------------------------------------------------------------------------
# 五、可追溯性（traceability）
# ---------------------------------------------------------------------------
_TRACEABILITY_RULES: tuple[AuditRule, ...] = (
    AuditRule("TRC-01", "traceability", "关键工序有计算书或验算",
              "涉及受力 / 稳定性的工序应有可追溯的计算过程（公式、参数取值、结论）",
              "block", CHECK_MODE_PROGRAM,
              _BASIS_31 + "论证要点②：方案计算书和验算依据是否符合有关标准规范"),
    AuditRule("TRC-02", "traceability", "计算参数取值有依据",
              "计算书应注明参数取值来源（勘察报告 / 设计文件 / 规范取值）",
              "high", CHECK_MODE_AI, _BASIS_31 + "论证要点②"),
    AuditRule("TRC-03", "traceability", "附图纸或附表齐全",
              "引用的附图（平面布置、节点详图、监测点布置等）应齐全并在正文中被引出",
              "medium", CHECK_MODE_PROGRAM,
              _BASIS_37 + "第十七条第（九）项计算书及相关图纸",
              ("附图", "详图", "平面布置", "节点图")),
    AuditRule("TRC-04", "traceability", "材料与构配件规格可追溯",
              "材料规格、强度等级、进场验收要求应明确，可对应到检验与复试要求",
              "medium", CHECK_MODE_AI, _BASIS_GB55032),
)

# ---------------------------------------------------------------------------
# 六、可交付性（deliverability）
# ---------------------------------------------------------------------------
_DELIVERABILITY_RULES: tuple[AuditRule, ...] = (
    AuditRule("DLV-01", "deliverability", "无空章节",
              "叶子章节不得为空（无正文且无子章节）",
              "high", CHECK_MODE_PROGRAM, "《产品需求文档》§3.10.3 导出预检"),
    AuditRule("DLV-02", "deliverability", "章节字数达标",
              "叶子章节字数显著低于目标（< 100 字）视为生成不完整",
              "medium", CHECK_MODE_PROGRAM, "《产品需求文档》§3.10.3 导出预检"),
    AuditRule("DLV-03", "deliverability", "无孤立节点",
              "章节父节点必须存在，否则目录结构与导出编号会错乱",
              "high", CHECK_MODE_PROGRAM, "《产品需求文档》§3.10.3 导出预检"),
    AuditRule("DLV-04", "deliverability", "图表均已生成",
              "登记在册的图表应全部完成渲染，未生成会导致交付文档缺图",
              "medium", CHECK_MODE_PROGRAM, "《产品需求文档》§3.10.3 导出预检"),
    AuditRule("DLV-05", "deliverability", "无控制字符与乱码",
              "正文不得含 C0/C1 控制字符、U+FFFD 替换字符等（导出后表现为乱码或方框）",
              "block", CHECK_MODE_PROGRAM, "《产品需求文档》§3.10.3 控制字符检查"),
    AuditRule("DLV-06", "deliverability", "无口语化与 AI 腔残留",
              "交付文本应为工程书面语，不得残留口语化表达与 AI 客套话",
              "medium", CHECK_MODE_PROGRAM, "《产品需求文档》§3.12 质量管理"),
    AuditRule("DLV-07", "deliverability", "无未闭合的 Markdown 代码围栏",
              "未闭合围栏会吞噬后续正文，导出后大段内容丢失",
              "high", CHECK_MODE_PROGRAM, "《产品需求文档》§3.10.3 导出预检"),
    AuditRule("DLV-08", "deliverability", "整体篇幅达标",
              "方案总字数应达到设定预算的 80% 以上，避免内容单薄",
              "low", CHECK_MODE_PROGRAM, "《产品需求文档》§3.10 字数管理"),
    # ✅ G1（2026-09-21）：以下四条是「导出预检 → 就绪度总检」共用同一份数据所必需
    # 的规则条目。导出预检（routers/export.py::export_check）此前是一套**独立体系**：
    # 算出的问题只在自己的弹窗里显示，既不进 preflight_runs 也不进 readiness_overview
    # 的 findings/sources。用户在总检页看到 B 级，去导出页却被一堆问题拦住，
    # 两份报告口径对不上。现把导出预检的 issue 类型映射到下面四个规则，
    # 与程序化预检共用同一套 rule_id 词表与维度权重，两处结论天然可比较。
    # 由 routers/export.py::export_issues_to_findings 产出（prelight_engine 不重复实现，
    # 避免同一问题两套判定逻辑分叉）。
    AuditRule("DLV-09", "deliverability", "正文均已通过审核",
              "已生成正文的章节应完成审核（通过）后才交付；待审核 / 已驳回 / 无审核记录的"
              "章节不得混进交付文档",
              "high", CHECK_MODE_PROGRAM,
              "《产品需求文档》§3.12.5 审核工作流 + §3.10.3 导出预检"),
    AuditRule("DLV-10", "deliverability", "章节状态与正文一致",
              "章节 status 不得与实际正文相矛盾（如 status=empty 却已有正文），"
              "矛盾状态会让生成进度统计与导出预检同时失真",
              "medium", CHECK_MODE_PROGRAM, "《产品需求文档》§3.10.3 导出预检"),
    AuditRule("DLV-11", "deliverability", "提取项目必选项已完整",
              "项目资料结构化提取的必选项若大量失败，下游目录与正文的关键参数"
              "（基坑深度、荷载等）会缺失或被编造",
              "medium", CHECK_MODE_PROGRAM, "《产品需求文档》§3.10 项目资料与结构化提取"),
    AuditRule("DLV-12", "deliverability", "同级无重复编号",
              "目录同级节点不应出现重复编号 / 重复标题（长方案分步生成易重复挂接子节点），"
              "重复会让导出文档的目录与标题编号错乱",
              "medium", CHECK_MODE_PROGRAM, "《建筑施工组织设计规范》GB/T 50502-2009"),
    AuditRule("DLV-13", "deliverability", "正文子标题未与子章节编号撞号",
              "正文内的 X.X 点分子标题与目录中真实存在的子章节编号占用同一命名空间，"
              "导出后会与自动编号的子章节标题重号、目录层级错乱",
              "high", CHECK_MODE_PROGRAM,
              "《建筑施工组织设计规范》GB/T 50502-2009 章节编号唯一性"),
    AuditRule("DLV-14", "deliverability", "正文交叉引用未失效",
              "正文引用的图号 / 表号 / 节号在当前目录与图表清单中已不存在"
              "（章节被删除或重排、图表未生成），交付后引用悬空",
              "medium", CHECK_MODE_PROGRAM,
              "《建筑施工组织设计规范》GB/T 50502-2009 图表编号与引用一致性"),
    AuditRule("DLV-15", "deliverability", "全局事实已核对且来源有效",
              "存在未确认、模拟值、多来源冲突或来源资料已变化的全局事实时，"
              "这些事实不会进入正文/导出，继续交付会造成关键参数缺失或使用过期值",
              "high", CHECK_MODE_PROGRAM, "建办质〔2021〕48号 + 平台全局事实安全门控"),
)


# ---------------------------------------------------------------------------
# 全量规则表
# ---------------------------------------------------------------------------
ALL_RULES: tuple[AuditRule, ...] = (
    _COMPLETENESS_RULES + _COMPLIANCE_RULES + _SAFETY_RULES
    + _CONSISTENCY_RULES + _TRACEABILITY_RULES + _DELIVERABILITY_RULES
)

RULE_MAP: dict[str, AuditRule] = {r.rule_id: r for r in ALL_RULES}


def get_rule(rule_id: str) -> AuditRule | None:
    return RULE_MAP.get(rule_id)


def active_rules() -> list[AuditRule]:
    """参与本轮检查的规则（排除已废弃条目）。"""
    return [r for r in ALL_RULES if not r.deprecated]


def rules_by_dimension(dimension: str) -> list[AuditRule]:
    return [r for r in active_rules() if r.dimension == dimension]


def program_rules() -> list[AuditRule]:
    """可离线程序化判定的规则（无需 AI）。"""
    return [r for r in active_rules() if r.mode == CHECK_MODE_PROGRAM]


def ai_rules() -> list[AuditRule]:
    """需交由 AI 语义判定的规则。"""
    return [r for r in active_rules() if r.mode == CHECK_MODE_AI]


def ai_checklist() -> list[dict]:
    """供 AI 判定的规则清单（注入提示词 / 前端展示）。

    只送 AI 规则（程序化规则本地已判定，再让 AI 判一遍既浪费 token 又可能给出
    与之矛盾的结论）。
    """
    return [r.as_dict() for r in ai_rules()]


def rule_catalog() -> list[dict]:
    """全量规则目录（前端「规则说明」抽屉用）。"""
    return [r.as_dict() for r in active_rules()]


def dimension_catalog() -> list[dict]:
    return [
        {
            "key": d.key,
            "label": d.label,
            "weight": d.weight,
            "desc": d.desc,
            "rules": [r.rule_id for r in rules_by_dimension(d.key)],
        }
        for d in DIMENSIONS
    ]


# ---------------------------------------------------------------------------
# 专家论证必要项：与 CMP-* 规则绑定（消除原 EXPERT_CHECK_ITEMS 与提示词的分叉）
# ---------------------------------------------------------------------------
#: 论证必要项 → 对应规则 ID（前端 Tag 与后端评分共用同一份映射）
EXPERT_ITEM_RULES: dict[str, str] = {
    "工程概况": "CMP-01",
    "编制依据": "CMP-02",
    "施工计划": "CMP-03",
    "施工工艺技术": "CMP-04",
    "安全保证措施": "CMP-05",
    "人员分工": "CMP-06",
    "验收要求": "CMP-07",
    "应急处置措施": "CMP-08",
    "计算书及相关图纸": "CMP-09",
    "监测方案": "SAF-06",
}

EXPERT_CHECK_ITEMS: list[str] = list(EXPERT_ITEM_RULES.keys())


def expert_items() -> list[dict]:
    """论证必要项清单（含规则依据），供前端展示与提示词渲染。"""
    out: list[dict] = []
    for item, rid in EXPERT_ITEM_RULES.items():
        rule = get_rule(rid)
        out.append({
            "item": item,
            "rule_id": rid,
            "basis": rule.basis if rule else "",
            "severity": rule.severity if rule else "medium",
        })
    return out


# ---------------------------------------------------------------------------
# 注册表自检（导入期契约，双向校验）
# ---------------------------------------------------------------------------
#: 程序化引擎（preflight_engine）实际会产出的 rule_id 基编号全集。
#: 维护约定：引擎内新增 ``_finding("XXX-NN")`` 时必须同步登记到本集合，
#: 否则自检无法覆盖该规则。派生编号（``CON-05-1``）以基编号登记即可（可回退解析）。
_PROGRAM_EMITTED_RULE_IDS: frozenset[str] = frozenset({
    "CMP-01", "CMP-02", "CMP-03", "CMP-04", "CMP-05", "CMP-06", "CMP-07",
    "CMP-08", "CMP-09",
    "STD-01", "STD-02", "STD-03", "STD-04", "STD-05",
    "SAF-01", "SAF-02", "SAF-03", "SAF-04", "SAF-05", "SAF-06", "SAF-07",
    "CON-01", "CON-05", "CON-06",
    "TRC-01", "TRC-03",
    "DLV-01", "DLV-02", "DLV-03", "DLV-04", "DLV-05", "DLV-06", "DLV-07",
    "DLV-08",
})


def _resolve_base_rule(rule_id: str):
    """按 rule_id 定位规则，容忍派生编号（``CON-05-1`` → 基规则 ``CON-05``）。

    ✅ BUG 修复（2026-09-27）：查重规则按出现顺序派生编号（``CON-05-1``、``CON-05-2``…）
    以避免 ``merge_findings`` 按 rule_id 去重时把多对重复塌缩成一条。但派生 ID 在注册表里
    并不存在，``get_rule`` 返回 None → **dimension 退化为空串**，后果有三：
      1. ``audit_scoring.score_findings`` 把空维度计入 ``unknown_dimension_count``，
         且 consistency 维度的扣分被记到 deliverability（权重 15 → 10），总分系统性偏移；
      2. 前端「规则说明」抽屉查不到该 ID，用户只看到裸串 ``CON-05-1``；
      3. high 级阻断项在报告里没有行业依据（basis 为空）。

    修复策略：**rule_id 保持派生值不变**（去重语义依赖它），只回退取基规则的
    维度 / 标题 / 依据。逐级向上剥离末尾的 ``-<数字>`` 后缀直到命中注册表。

    放在本模块（唯一事实源）而非引擎里，使注册表自检无需反向 import 引擎。
    """
    rid = (rule_id or "").strip()
    seen: set[str] = set()
    while rid and rid not in seen:
        seen.add(rid)
        rule = get_rule(rid)
        if rule is not None:
            return rule
        # 仅剥离「-<数字>」形态的序号后缀，避免误伤 CON-05 这类基编号本身
        head, sep, tail = rid.rpartition("-")
        if not sep or not tail.isdigit() or not head:
            break
        rid = head
    return None


def validate_rule_registry(*, mapping: dict | None = None,
                           emitted_rule_ids: "Iterable[str] | None" = None) -> list[str]:
    """校验规则注册表与各产出方的一致性，返回问题描述列表（空 = 健康）。

    ✅ 缺陷类别根治（2026-09-27）：本模块被声明为「审核规则唯一事实源」，但此前
    没有任何机制保证它真的覆盖了所有产出方，实际已发生两处分叉：

    1. ``routers/export.py::_EXPORT_ISSUE_RULE_MAP`` 产出 ``DLV-13`` / ``DLV-14``，
       注册表里**根本没有**这两条 → high 级问题落到兜底分支，title 退化为
       「导出预检问题」、basis / suggestion 全空。
    2. ``preflight_engine`` 产出派生编号 ``CON-05-1``（为避免 merge 去重塌缩），
       注册表只登记基编号 ``CON-05`` → dimension 退化为空串，consistency 维度的
       扣分被记到 deliverability，总分系统性偏移。

    本函数把这些漂移变成**可自动检测的契约**：
    - ``rule_id`` 唯一、格式合法、维度/严重度/方式合法、标题与依据非空；
    - 所有外部映射表引用的 rule_id 均已注册（覆盖 export 的 issue 映射）；
    - 程序化引擎产出的 rule_id 均可解析到基规则（容忍派生编号）。

    纯函数、无副作用，可安全用于测试断言与启动期告警。
    """
    import re as _re

    problems: list[str] = []
    valid_dims = {d.key for d in DIMENSIONS}
    valid_sevs = set(SEVERITY_ORDER)
    valid_modes = {CHECK_MODE_PROGRAM, CHECK_MODE_AI}

    seen: set[str] = set()
    for r in ALL_RULES:
        if r.rule_id in seen:
            problems.append(f"rule_id 重复注册：{r.rule_id}")
        seen.add(r.rule_id)
        if not _re.match(r"^[A-Z]{3}-\d{2}$", r.rule_id):
            problems.append(f"rule_id 格式非法（应为 XXX-NN）：{r.rule_id}")
        if r.dimension not in valid_dims:
            problems.append(f"{r.rule_id} 维度非法：{r.dimension}")
        if r.severity not in valid_sevs:
            problems.append(f"{r.rule_id} 严重度非法：{r.severity}")
        if r.mode not in valid_modes:
            problems.append(f"{r.rule_id} 检查方式非法：{r.mode}")
        if not r.title:
            problems.append(f"{r.rule_id} 缺少面向用户的标题")
        if not r.basis:
            problems.append(f"{r.rule_id} 缺少行业依据（维护约定要求可核实）")

    # 外部映射表引用的 rule_id 必须已注册（唯一事实源的承诺必须可验证）。
    # ⚠️ 架构约束：services 层**禁止 import routers**（见
    #    tests/test_outline_name_line_20260927.py::test_services_never_import_routers），
    #    故此处只接收调用方（路由层 / 测试）传入的映射表，不自行导入。
    for issue_type, (rid, _sev) in sorted((mapping or {}).items()):
        if rid not in RULE_MAP:
            problems.append(
                f"导出预检 issue 类型 {issue_type!r} 映射到未注册规则 {rid}，"
                f"请在 _DELIVERABILITY_RULES 中补登记")

    # 程序化引擎产出的 rule_id 必须能解析（容忍 CON-05-1 这类派生编号）。
    # 同样只接收传入的产出集合，不反向 import（引擎 import 注册表才是正确方向）。
    for rid in sorted(emitted_rule_ids or ()):
        if _resolve_base_rule(rid) is None:
            problems.append(
                f"程序化引擎产出 {rid}，但注册表中查不到（含基编号回退）")

    return problems
