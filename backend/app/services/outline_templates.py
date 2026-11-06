"""建筑工程行业标准目录模板库

依据（2026-09 重构，对齐现行建筑工程行业标准与法定要求）：

- 《危险性较大的分部分项工程安全管理规定》（住建部令第 37 号）
- 建办质〔2018〕31 号《关于实施〈危险性较大的分部分项工程安全管理规定〉有关问题的通知》
- GB/T 50502-2009《建筑施工组织设计规范》
- GB 50300-2013《建筑工程施工质量验收统一标准》
- GB/T 29639-2020《生产经营单位生产安全事故应急预案编制导则》
- 各专业现行国家 / 行业标准（详见 TEMPLATE_META）

设计约定：
- 目录树硬限三级（一级=章、二级=节、三级=条），与 services.outline_utils.MAX_OUTLINE_DEPTH 对齐；
- 章节标题不带编号，编号由 renumber_outline 统一重排（避免双重编号）；
- 危大工程类方案章结构对齐住建部令第 37 号要求的九章法定内容；
  管理类方案按专业惯例调整，但统一保留「概况—依据—计划—工艺—安全—人员—验收—应急」主干。
"""

# -------------------------------------------------------------------------
# ✅ 2026-11-04 拆分（纯搬运，零行为变更）
# 本模块保留：模板注册表（BUILDERS / RULES / TEMPLATE_META / _TEMPLATE_NAMES）
#   与公共 API（match_template / build_outline / get_meta / list_templates）。
# 其余 builder 与装配助手按主题拆到同级 _outline_tpl_* 模块，并在此**原样转发**：
#   `from app.services.outline_templates import <任意原符号>` 仍可用；
#   `monkeypatch.setattr(outline_templates, "<名字>", ...)` 对 build_outline
#   的 `_t_general` 兜底、match_template 的 RULES 等运行期查找语义不变。
# -------------------------------------------------------------------------
from __future__ import annotations

from typing import Callable

from app.services._outline_tpl_base import (
    C,
    S,
    _acceptance_chapter,
    _basis_chapter,
    _calc_chapter,
    _chapter,
    _emergency_chapter,
    _n,
    _nine_chapter,
    _personnel_chapter,
    _plan_chapter,
)
from app.services._outline_tpl_generic_a import (
    _t_confined_space,
    _t_decoration,
    _t_fire_water,
    _t_formwork,
    _t_foundation_pit,
    _t_lifting,
    _t_mep,
    _t_prefabricated,
    _t_scaffold,
    _t_temp_electricity,
    _t_waterproof,
    _t_work_at_height,
)
from app.services._outline_tpl_generic_b import (
    _t_demolition,
    _t_earthwork,
    _t_emergency,
    _t_general,
    _t_green,
    _t_masonry,
    _t_monitoring,
    _t_quality,
    _t_safety_management,
    _t_seasonal,
)
from app.services._outline_tpl_lift_demolition import (
    _t_construction_hoist,
    _t_demolition_blast,
    _t_demolition_machine,
    _t_demolition_manual,
    _t_tower_crane,
)
from app.services._outline_tpl_other_hazard import (
    _t_curtain_wall,
    _t_grid_structure,
    _t_manual_dig_pile,
    _t_new_tech,
    _t_pipe_jacking,
    _t_prestress,
    _t_slope,
    _t_steel_structure,
    _t_underground_excavation,
    _t_underwater,
)
from app.services._outline_tpl_scaffold import (
    _t_formwork_disc,
    _t_formwork_tall,
    _t_scaffold_attached,
    _t_scaffold_cantilever,
    _t_scaffold_cuplock,
    _t_scaffold_disc,
    _t_scaffold_gate,
    _t_scaffold_gondola,
    _t_scaffold_ground,
    _t_scaffold_unload_platform,
    _t_scaffold_work_platform,
)

__all__ = [
    "BUILDERS",
    "C",
    "CONTENT_TYPES",
    "FACT_DEP_ALIASES",
    "RULES",
    "S",
    "TEMPLATE_META",
    "_TEMPLATE_NAMES",
    "_acceptance_chapter",
    "_basis_chapter",
    "_calc_chapter",
    "_chapter",
    "_emergency_chapter",
    "_n",
    "_nine_chapter",
    "_personnel_chapter",
    "_plan_chapter",
    "_t_confined_space",
    "_t_construction_hoist",
    "_t_curtain_wall",
    "_t_decoration",
    "_t_demolition",
    "_t_demolition_blast",
    "_t_demolition_machine",
    "_t_demolition_manual",
    "_t_earthwork",
    "_t_emergency",
    "_t_fire_water",
    "_t_formwork",
    "_t_formwork_disc",
    "_t_formwork_tall",
    "_t_foundation_pit",
    "_t_general",
    "_t_green",
    "_t_grid_structure",
    "_t_lifting",
    "_t_manual_dig_pile",
    "_t_masonry",
    "_t_mep",
    "_t_monitoring",
    "_t_new_tech",
    "_t_pipe_jacking",
    "_t_prefabricated",
    "_t_prestress",
    "_t_quality",
    "_t_safety_management",
    "_t_scaffold",
    "_t_scaffold_attached",
    "_t_scaffold_cantilever",
    "_t_scaffold_cuplock",
    "_t_scaffold_disc",
    "_t_scaffold_gate",
    "_t_scaffold_gondola",
    "_t_scaffold_ground",
    "_t_scaffold_unload_platform",
    "_t_scaffold_work_platform",
    "_t_seasonal",
    "_t_slope",
    "_t_steel_structure",
    "_t_temp_electricity",
    "_t_tower_crane",
    "_t_underground_excavation",
    "_t_underwater",
    "_t_waterproof",
    "_t_work_at_height",
    "annotate_outline_meta",
    "build_outline",
    "content_type_of",
    "fact_deps_of",
    "get_meta",
    "list_templates",
    "match_template",
]

# ==================================================================== 模板注册表

BUILDERS: dict[str, Callable[[], list[dict]]] = {
    "foundation_pit": _t_foundation_pit,
    "formwork": _t_formwork,
    "scaffold": _t_scaffold,
    "lifting": _t_lifting,
    "temp_electricity": _t_temp_electricity,
    "work_at_height": _t_work_at_height,
    "confined_space": _t_confined_space,
    "fire_water": _t_fire_water,
    "prefabricated": _t_prefabricated,
    "waterproof": _t_waterproof,
    "decoration": _t_decoration,
    "mep": _t_mep,
    "quality": _t_quality,
    "safety_management": _t_safety_management,
    "emergency": _t_emergency,
    "seasonal": _t_seasonal,
    "green": _t_green,
    "earthwork": _t_earthwork,
    "masonry": _t_masonry,
    "demolition": _t_demolition,
    "monitoring": _t_monitoring,
    "general": _t_general,
    # 以下为 2026-10-01 目录库全面更新新增的六大类危大逐型式独立条目
    # —— 脚手架族
    "scaffold_ground": _t_scaffold_ground,
    "scaffold_attached": _t_scaffold_attached,
    "scaffold_cantilever": _t_scaffold_cantilever,
    "scaffold_gate": _t_scaffold_gate,
    "scaffold_cuplock": _t_scaffold_cuplock,
    "scaffold_disc": _t_scaffold_disc,
    "scaffold_gondola": _t_scaffold_gondola,
    "scaffold_unload_platform": _t_scaffold_unload_platform,
    "scaffold_work_platform": _t_scaffold_work_platform,
    # —— 模板支撑族
    "formwork_tall": _t_formwork_tall,
    "formwork_disc": _t_formwork_disc,
    # —— 起重族
    "tower_crane": _t_tower_crane,
    "construction_hoist": _t_construction_hoist,
    # —— 拆除族
    "demolition_manual": _t_demolition_manual,
    "demolition_machine": _t_demolition_machine,
    "demolition_blast": _t_demolition_blast,
    # —— 其他危大族
    "curtain_wall": _t_curtain_wall,
    "steel_structure": _t_steel_structure,
    "grid_structure": _t_grid_structure,
    "prestress": _t_prestress,
    "manual_dig_pile": _t_manual_dig_pile,
    "slope": _t_slope,
    "underground_excavation": _t_underground_excavation,
    "pipe_jacking": _t_pipe_jacking,
    "underwater": _t_underwater,
    "new_tech": _t_new_tech,
}

#: 方案名关键字 → 模板（按特异性从高到低匹配，命中即返回）
RULES: list[tuple[tuple[str, ...], str]] = [
    # ==================================================================
    # 2026-10-01 目录库全面更新：六大类危大逐型式细粒度路由。
    #   置于所有通用规则之前——match_template 命中即返回，故具体型式名
    #   （如「落地式钢管脚手架」「塔吊安装拆卸」「爆破拆除」「预应力张拉」）
    #   优先归入对应独立条目；未指明型式的通用名（如「脚手架工程专项方案」）
    #   仍落到下方 scaffold/demolition/lifting/formwork 兜底（向后兼容）。
    #   关键词均带型式后缀，避免跨类误伤（如「基坑边坡」靠下方 foundation）。
    # ==================================================================
    (("人工挖孔桩", "挖孔桩", "人工挖孔"), "manual_dig_pile"),
    (("边坡支护", "边坡开挖", "高边坡", "边坡工程", "滑坡治理", "锚索边坡", "锚杆边坡"), "slope"),
    (("地下暗挖", "暗挖", "盾构", "矿山法", "隧道", "浅埋暗挖", "顶进法"), "underground_excavation"),
    (("顶管", "管道顶进", "非开挖顶管", "泥水平衡", "土压平衡"), "pipe_jacking"),
    (("水下作业", "水下混凝土", "水下结构", "水下安装", "潜水", "封底混凝土", "水下封底"), "underwater"),
    (("爆破拆除", "控制爆破", "爆破", "预裂爆破", "光面爆破", "拆除爆破"), "demolition_blast"),
    (("机械拆除", "机械破碎", "炮机拆除", "液压拆除", "破碎机拆除"), "demolition_machine"),
    (("人工拆除", "人工破碎"), "demolition_manual"),
    (("网架", "索膜", "空间结构", "网架安装", "管桁架", "网架结构"), "grid_structure"),
    (("钢结构安装", "钢结构吊装", "钢结构", "钢构件", "型钢柱", "钢管柱", "箱型柱"), "steel_structure"),
    (("幕墙", "玻璃幕墙", "石材幕墙", "铝板幕墙", "金属幕墙", "全玻幕墙", "点支式幕墙"), "curtain_wall"),
    (("预应力", "张拉", "后张法", "先张法", "钢绞线", "无粘结"), "prestress"),
    (("承插型盘扣式模板", "盘扣式模板支撑", "盘扣支撑架", "盘扣式支撑", "盘扣支架"), "formwork_disc"),
    (("高大模板", "高支模", "高大支模", "超限梁", "大跨度模板", "承重支撑体系", "超限模板"), "formwork_tall"),
    (("塔吊", "塔式起重机", "塔机", "群塔", "群吊群塔", "顶升加节", "塔吊基础", "塔吊安装", "塔吊拆卸"), "tower_crane"),
    (
        ("施工升降机", "人货电梯", "人货梯", "施工电梯", "升降机安装", "升降机拆卸", "物料提升机", "施工电梯基础"),
        "construction_hoist",
    ),
    (("卸料平台", "悬挑卸料平台", "落地式卸料平台", "操作式卸料平台"), "scaffold_unload_platform"),
    (("操作平台", "移动式操作平台", "移动平台", "操作架"), "scaffold_work_platform"),
    (("附着式升降", "爬架", "附着升降", "全附着", "半附着", "附着式"), "scaffold_attached"),
    (
        (
            "落地式钢管脚手架",
            "落地式脚手架",
            "落地脚手架",
            "落地架",
            "落地式",
            "扣件式钢管脚手架",
            "落地钢管脚手架",
            "扣件式脚手架",
        ),
        "scaffold_ground",
    ),
    (
        ("悬挑脚手架", "悬挑式脚手架", "型钢悬挑脚手架", "悬挑架", "悬挑式钢管", "悬挑外架", "悬挑式"),
        "scaffold_cantilever",
    ),
    (("门式脚手架", "门型脚手架", "门式钢管", "门架"), "scaffold_gate"),
    (("碗扣式脚手架", "碗扣脚手架", "碗扣式钢管", "承插型碗扣", "碗扣式"), "scaffold_cuplock"),
    (("盘扣式脚手架", "承插型盘扣式钢管脚手架", "盘扣脚手架", "盘扣式钢管", "盘扣式", "快拆架"), "scaffold_disc"),
    (("吊篮", "高处作业吊篮", "电动吊篮"), "scaffold_gondola"),
    (("新技术", "新工艺", "新材料", "新设备", "四新", "无技术标准", "新型模板"), "new_tech"),
    (("人工挖孔桩", "地下连续墙"), "foundation_pit"),
    (("降水", "排水", "截水"), "foundation_pit"),
    (("基坑监测", "沉降观测", "变形监测", "基坑监测"), "monitoring"),
    # ✅ 2026-10-01 匹配质量收口：「超前支撑注浆钢管专项施工方案」此前
    #    不含任何既有关键词（「钢支撑」≠「支撑注浆钢管」），落入通用骨架 ——
    #    超前支撑（注浆钢管）是基坑支护超前加固语义，应归基坑模板。
    (
        ("基坑", "围护", "支护", "土方", "开挖", "回填", "钢支撑", "型钢拔除", "SMW", "锚索", "超前支撑", "注浆钢管"),
        "foundation_pit",
    ),
    # ✅ 2026-09-26 死模板修复：`earthwork`（场地平整/边坡修整/土方回填与压实）
    #    此前在 BUILDERS / TEMPLATE_META / list_templates 中齐备，却**没有任何
    #    RULES 规则指向它** → match_template 永远不可能返回该 key，模板能力
    #    完全悬空（list_templates 仍会把它列进前端「套用标准模板」下拉，
    #    用户选中后 AI 参考里却始终是别的类别）。
    #    排位放在 foundation_pit **之后**：其关键词刻意只用「无基坑语义」的
    #    纯土方词，使「基坑开挖 / 边坡支护」类复合名仍归 foundation_pit，
    #    仅「场地平整」「土方回填与压实」这类独立土方方案落到 earthwork。
    (
        ("场地平整", "边坡修整", "土方回填与压实", "土方压实", "土方平衡", "土方外运", "场内土方", "土方调配"),
        "earthwork",
    ),
    (("高大支模", "高支模", "超限梁", "模板支撑", "排架", "模板工程", "支模"), "formwork"),
    # ✅ 2026-10-01 匹配质量收口：「电梯井道防护专项施工方案」此前落入
    #    通用骨架（「井道脚手架」≠「井道防护」），电梯井道防护属脚手架与防护语义。
    (("吊篮", "附着式升降", "悬挑脚手架", "脚手架", "操作平台", "井道脚手架", "井道防护"), "scaffold"),
    (
        (
            "塔吊",
            "塔式起重机",
            "施工电梯",
            "人货梯",
            "人货电梯",
            "升降机",
            "群塔",
            "吊装",
            "安拆",
            "安装与拆卸",
            "起重",
            "汽车吊",
            "人防门",
        ),
        "lifting",
    ),
    (("临时用电", "临电", "施工用电"), "temp_electricity"),
    # ✅ 2026-10-01 匹配质量收口：「预防高处坠落事故专项施工方案」此前
    #    不含「高处作业 / 防高坠 / 防坠落」任一关键词（是「高处坠落」），
    #    落入通用骨架，应归高处作业模板。
    (("高处作业", "高空作业", "防高坠", "防坠落", "高处坠落", "临边", "洞口", "安全带"), "work_at_height"),
    (("有限空间", "密闭空间", "受限空间"), "confined_space"),
    # ✅ 2026-10-01 匹配质量收口：裸「消防」子串过宽 ——「消防工程专项施工方案」
    #    「消防改造更新专项方案」（机电与智能化分类）指**正式消防系统**（喷淋、报警），
    #    此前被本规则（临时设施与现场消防）截胡，拿到的编制依据/适用条件是临建消防口径。
    #    收窄为「现场消防 / 临时消防」，把「消防工程 / 消防改造」留给 mep 模板。
    (("现场消防", "临时消防", "临时用水", "大临", "围墙", "围挡", "安全通道", "临时照明", "临建"), "fire_water"),
    (("拆除", "爆破"), "demolition"),
    # ✅ 2026-10-01 匹配质量收口：waterproof 提到 prefabricated 之前 ——
    #    「装配式混凝土建筑防水」（防水与渗漏分类）此前先命中 prefabricated
    #    （构件吊装/安装），而其核心语义是防水专项，应是防水模板。
    #    提前只影响「同时含防水词与装配式词」的方案名（清单内仅此一条），
    #    「装配式结构 / 钢结构吊装」类方案名不受影响。
    # ✅ 同轮补「套筒连接」：「套筒连接灌浆专项施工方案」此前不含
    #    「套筒灌浆 / 灌浆套筒」任一关键词，落入通用骨架。
    (("防水", "防渗漏", "渗漏", "淋水"), "waterproof"),
    (("装配式", "预制构件", "套筒灌浆", "灌浆套筒", "PC构件", "钢结构", "预制结构", "套筒连接"), "prefabricated"),
    (
        (
            "装饰",
            "装修",
            "保温",
            "门窗",
            "幕墙",
            "涂料",
            "涂饰",
            "屋面",
            "外立面",
            "烟道",
            "室外总体",
            "粗装饰",
            "精装修",
        ),
        "decoration",
    ),
    (("机电", "智能化", "通风空调", "建筑电气", "给排水", "消防工程", "消防改造", "节能", "防雷接地", "弱电"), "mep"),
    (("质量计划", "质量", "创优", "检验批", "样板", "通病", "修补", "见证取样", "影像留存"), "quality"),
    # ✅ 2026-10-01 匹配质量收口：补「排查整治」——「重大事故隐患专项排查整治
    #    行动工作方案」此前不含「隐患排查」（是「隐患专项排查」），落入通用骨架。
    (
        (
            "危险源",
            "危险源辨识",
            "文明工地",
            "扬尘",
            "大气污染",
            "安保计划",
            "安全措施",
            "安全施工",
            "安全管控",
            "十条指令",
            "隐患排查",
            "排查整治",
            "一带一帽",
            "动火作业",
            "安全组织",
        ),
        "safety_management",
    ),
    (("应急预案", "应急处置", "防台防汛", "防汛", "防台", "突发"), "emergency"),
    (("冬雨季", "雨季", "冬季施工", "防暑降温", "高温"), "seasonal"),
    # ✅ 2026-10-01 匹配质量收口：补「环境因素」——「环境因素调查、评价及控制
    #    措施清单」「重要环境因素汇总表」此前落入通用骨架，应归绿色施工（环境管理）。
    (("绿色施工", "环境保护", "环保", "环境因素", "节能施工"), "green"),
    (("砌体", "二次结构", "填充墙", "加气块"), "masonry"),
    (("监测", "观测"), "monitoring"),
    (("疫情", "治本攻坚"), "emergency"),
]

#: 模板元信息：编制依据摘要 / 适用条件 / 危大分级
TEMPLATE_META: dict[str, dict[str, str]] = {
    "foundation_pit": {
        "basis": "GB 51004-2015《建筑地基基础工程施工规范》、GB 50202-2018《建筑地基基础工程施工质量验收标准》、"
        "JGJ 120-2012《建筑基坑支护技术规程》、JGJ 311-2013《建筑深基坑工程施工安全技术规范》、"
        "GB 50497-2019《建筑基坑工程监测技术标准》、住建部令第 37 号、建办质〔2018〕31 号",
        "applicable": "适用于开挖深度≥3m（含 3m）或虽未超过 3m 但地质条件、周边环境复杂的基坑（槽）开挖、"
        "支护、降水工程；开挖深度≥5m 属超过一定规模危大工程，须组织专家论证",
        "risk": "危大工程",
    },
    "formwork": {
        "basis": "JGJ 162-2008《建筑施工模板安全技术规范》、JGJ 130-2011、JGJ 166-2016、"
        "JGJ/T 231-2021、GB 50204-2015《混凝土结构工程施工质量验收规范》、"
        "GB 50666-2011、住建部令第 37 号、建办质〔2018〕31 号",
        "applicable": "适用于搭设高度≥5m、搭设跨度≥10m、施工总荷载≥10kN/m²、集中线荷载≥15kN/m 的"
        "混凝土模板支撑工程；高度≥8m、跨度≥18m 或荷载更大的属超过一定规模危大工程",
        "risk": "危大工程",
    },
    "scaffold": {
        "basis": "JGJ 130-2011《建筑施工扣件式钢管脚手架安全技术规范》、JGJ 166-2016、"
        "JGJ/T 231-2021、JGJ 202-2010《建筑施工工具式脚手架安全技术规范》、"
        "GB 19155-2017《高处作业吊篮》、JGJ 80-2016",
        "applicable": "适用于搭设高度≥24m 的落地式钢管脚手架、附着式升降脚手架、悬挑式脚手架、"
        "高处作业吊篮、异型脚手架等",
        "risk": "危大工程",
    },
    "lifting": {
        "basis": "JGJ 276-2012《建筑施工起重吊装工程安全技术规范》、JGJ 33-2012、"
        "JGJ 196-2010、JGJ 215-2010、GB 5144-2006、GB/T 5972-2016、住建部令第 37 号",
        "applicable": "适用于单件起吊重量≥100kN（10t）的起重吊装工程，以及塔式起重机、施工升降机等"
        "起重机械的安装与拆卸工程；起重量≥300kN 或搭设总高度≥200m 属超规模",
        "risk": "危大工程",
    },
    "temp_electricity": {
        "basis": "JGJ/T 46-2024《建筑与市政工程施工现场临时用电安全技术标准》、GB 50194-2014《建设工程施工现场供用电安全规范》、"
        "GB 13955-2017、GB/T 13869-2017",
        "applicable": "适用于施工现场临时用电工程的设计、安装、使用、维护与拆除（含 5 台及以上设备或"
        "总容量 50kW 及以上者须编制用电组织设计）",
        "risk": "一般危大",
    },
    "work_at_height": {
        "basis": "JGJ 80-2016《建筑施工高处作业安全技术规范》、GB 6095-2021、GB 5725-2009、GB 2811-2019、"
        "JGJ 59-2011",
        "applicable": "适用于坠落高度基准面 2m 及以上有可能坠落的临边、洞口、攀登、悬空、操作平台及交叉作业",
        "risk": "一般危大",
    },
    "confined_space": {
        "basis": "GB 30871-2022、GBZ/T 205-2007《密闭空间作业职业危害防护规范》、GB 8958-2006、GBZ 2.1-2019",
        "applicable": "适用于封闭或部分封闭、进出口受限、通风不良的有限（受限）空间作业，"
        "如集水井、化粪池、管沟、水池、地下室、设备内部等",
        "risk": "重大风险",
    },
    "fire_water": {
        "basis": "GB 50720-2011《建设工程施工现场消防安全技术规范》、JGJ 146-2013、GB 50242-2002、"
        "GB 50140-2005、JGJ/T 46-2024",
        "applicable": "适用于施工现场临时办公与生活用房、临时给水排水、临时消防与临时照明等临时设施的"
        "规划、搭设、使用与拆除",
        "risk": "一般危大",
    },
    "prefabricated": {
        "basis": "GB/T 51231-2016《装配式混凝土建筑技术标准》、JGJ 1-2014、JGJ 355-2015、"
        "GB 50204-2015、GB 50205-2020、JGJ 276-2012",
        "applicable": "适用于装配式混凝土结构、钢结构的构件进场、堆放、吊装、临时固定、节点连接"
        "（套筒灌浆 / 螺栓 / 焊接）与接缝防水等施工",
        "risk": "危大工程",
    },
    "waterproof": {
        "basis": "GB 50108-2008《地下工程防水技术规范》、GB 50208-2011、GB 50345-2012、GB 50207-2012、" "JGJ 298-2013",
        "applicable": "适用于地下室、屋面、卫生间、外墙及水池等部位的防水（防渗）工程施工与渗漏治理",
        "risk": "一般危大",
    },
    "decoration": {
        "basis": "GB 50210-2018《建筑装饰装修工程质量验收标准》、GB 50327-2001、JGJ 144-2019、"
        "GB 50411-2019、JGJ 102-2003、GB 50345-2012",
        "applicable": "适用于建筑内外装饰装修、外墙外保温、门窗与幕墙、涂饰、吊顶、楼地面及屋面工程施工",
        "risk": "一般危大",
    },
    "mep": {
        "basis": "GB 50242-2002、GB 50243-2016、GB 50303-2015、GB 50261-2017、GB 50606-2010、GB 50411-2019",
        "applicable": "适用于建筑给水排水、暖通空调、建筑电气、智能建筑、消防及节能等机电安装工程的"
        "预留预埋、安装、调试与验收",
        "risk": "一般危大",
    },
    "quality": {
        "basis": "GB 50300-2013《建筑工程施工质量验收统一标准》、GB 50204-2015、GB/T 50375-2016、"
        "GB 50319-2013、GB/T 19001-2016",
        "applicable": "适用于施工质量策划、过程控制、检验批与分部分项验收、质量通病防治、"
        "创优（优质结构 / 精品工程）及质量影像留存管理",
        "risk": "管理类",
    },
    "safety_management": {
        "basis": "JGJ 59-2011《建筑施工安全检查标准》、GB 50870-2013、GB/T 50640-2010、JGJ 146-2013、"
        "GB 50720-2011、住建部令第 37 号",
        "applicable": "适用于施工现场安全生产与文明施工管理、危险源辨识与风险分级管控、"
        "隐患排查治理、扬尘治理与文明工地创建",
        "risk": "管理类",
    },
    "emergency": {
        "basis": "GB/T 29639-2020《生产经营单位生产安全事故应急预案编制导则》、AQ/T 9007-2019、"
        "GB 50870-2013、JGJ 59-2011",
        "applicable": "适用于生产安全事故综合（专项）应急预案与现场处置方案的编制、评审、演练与修订，"
        "含防汛防台、防暑降温、疫情防控等专项",
        "risk": "管理类",
    },
    "seasonal": {
        "basis": "JGJ/T 104-2011《建筑工程冬期施工规程》、GB 50666-2011、GB 50496-2018、JGJ 59-2011、" "JGJ/T 46-2024",
        "applicable": "适用于冬期（连续 5 天平均气温低于 5℃）、雨期及高温季节的施工技术与管理措施编制",
        "risk": "季节性",
    },
    "green": {
        "basis": "GB/T 50905-2014《建筑工程绿色施工规范》、GB/T 50640-2010《建筑工程绿色施工评价标准》、"
        "JGJ 146-2013、GB 12523-2011",
        "applicable": "适用于建筑工程绿色施工（四节一环保）策划、实施、监测与评价",
        "risk": "管理类",
    },
    "earthwork": {
        "basis": "GB 51004-2015、GB 50201-2012《土方与爆破工程施工及验收规范》、GB 50202-2018、" "JGJ 180-2009",
        "applicable": "适用于场地平整、基坑（槽）土方开挖、边坡修整、土方回填与压实等土方工程施工",
        "risk": "危大工程",
    },
    "masonry": {
        "basis": "GB 50924-2014《砌体结构工程施工规范》、GB 50203-2011、GB 50003-2011、GB 50011-2010、" "JGJ 145-2013",
        "applicable": "适用于砌体（填充墙、二次结构）工程的植筋、砌筑、构造柱（圈梁、过梁）施工与验收",
        "risk": "一般危大",
    },
    "demolition": {
        "basis": "JGJ 147-2016《建筑拆除工程安全技术规范》、GB 6722-2014《爆破安全规程》、"
        "GB 50870-2013、GB 12523-2011",
        "applicable": "适用于建筑物（构筑物）的人工、机械、爆破及静力破碎拆除工程，" "含拆除前管线切断与周边防护",
        "risk": "危大工程",
    },
    "monitoring": {
        "basis": "GB 50497-2019《建筑基坑工程监测技术标准》、JGJ 8-2016《建筑变形测量规范》、"
        "GB 50026-2020、GB 50911-2013（涉轨项目）",
        "applicable": "适用于基坑（边坡）、建（构）筑物、管线的变形与受力监测方案编制，"
        "含测点布置、监测频次、报警值与成果反馈",
        "risk": "专项监测",
    },
    "general": {
        "basis": "GB 50300-2013《建筑工程施工质量验收统一标准》、GB/T 50502-2009《建筑施工组织设计规范》、"
        "JGJ 59-2011、GB 50870-2013",
        "applicable": "通用专项施工方案目录骨架，适用于未匹配到专业模板的管理性与综合性方案",
        "risk": "一般",
    },
    # ==================================================================
    # 2026-10-01 目录库全面更新：新增六大类危大逐型式条目的适用条件与分级编制依据。
    #   basis 按《专项方案分类体系》分层：法律法规 / 部门规章 / 强制性规范 / 专项技术规范；
    #   applicable 明确危大范围 / 超一定规模范围 / 适用工程类型与部位 / 需专家论证与监测验收。
    # ==================================================================
    "scaffold_ground": {
        "basis": "法律法规：《建设工程安全生产管理条例》（国务院令 393 号）、住建部令第 37 号；"
        "部门规章：建办质〔2018〕31 号；强制性规范：GB 55023-2022、GB 51210-2016；"
        "专项技术规范：JGJ 130-2011《建筑施工扣件式钢管脚手架安全技术规范》、JGJ 80-2016",
        "applicable": "适用于搭设高度 ≥24m 的落地式钢管脚手架工程（危大）；搭设高度 ≥50m 属超过一定规模危大工程，"
        "须专家论证；需分段验收挂牌与变形监测。不适用：构配件未经复试、地基未处理达标、连墙件不能可靠设置。",
        "risk": "危大工程",
    },
    "scaffold_attached": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 55023-2022、GB 51210-2016；专项技术规范：JGJ 202-2010《建筑施工工具式脚手架安全技术规范》",
        "applicable": "适用于全（半）附着式升降脚手架工程，本身即属危大工程；提升高度 ≥150m 或架体高度 >4 倍标准层属超规模，需专家论证；"
        "每次升降前需防坠防倾装置专项验收与同步监控。不适用：附墙支承结构混凝土强度未达标、无障碍未清理。",
        "risk": "危大工程",
    },
    "scaffold_cantilever": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 51210-2016；专项技术规范：JGJ 130-2011、JGJ 80-2016、GB 50017-2017",
        "applicable": "适用于分型悬挑脚手架工程，本身即属危大工程；架体高度 >20m（悬挑分段）需专家论证；悬挑钢梁锚固与斜拉卸荷属隐蔽验收。"
        "不适用：锚固楼板厚度/强度不足、预埋拉环未经验算。",
        "risk": "危大工程",
    },
    "scaffold_gate": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 51210-2016《建筑施工脚手架安全技术统一标准》；专项技术规范：JGJ 128-2019《建筑施工门式钢管脚手架安全技术标准》",
        "applicable": "适用于门型（门式）钢管脚手架，搭设高度 ≥24m 属危大；交叉支撑与锁臂不全时不得使用；需分段验收与变形监测。",
        "risk": "危大工程",
    },
    "scaffold_cuplock": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 51210-2016；专项技术规范：JGJ 166-2016《建筑施工碗扣式钢管脚手架安全技术规范》",
        "applicable": "适用于承插型碗扣式钢管脚手架/支撑架，搭设（支撑）高度 ≥24m 属危大；自由端与剪刀撑不满足限值时不得验收使用。",
        "risk": "危大工程",
    },
    "scaffold_disc": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 51210-2016；专项技术规范：JGJ/T 231-2021《建筑施工承插型盘扣式钢管脚手架安全技术标准》",
        "applicable": "适用于承插型盘扣式钢管脚手架，搭设高度 ≥24m 属危大；可调托座伸出 ≤300mm、自由端 ≤650mm；需节点抗剪与垂直度验收。",
        "risk": "危大工程",
    },
    "scaffold_gondola": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 51210-2016；专项技术规范：GB/T 19155-2017《高处作业吊篮》、JGJ 202-2010",
        "applicable": "适用于高处作业吊篮（安装高度 ≥50m 或非常规悬挑机构属超规模，需专家论证）；本身即属危大工程；安全锁、抗倾覆与安全绳专人监护。",
        "risk": "危大工程",
    },
    "scaffold_unload_platform": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 51210-2016；专项技术规范：JGJ 80-2016、JGJ 202-2010、GB 50017-2017",
        "applicable": "适用于悬挑（工具式）卸料平台，本身即属危大；限载与斜拉钢丝绳、预埋锚环属隐蔽验收。不适用：与脚手架连接受力、超高超高使用。",
        "risk": "危大工程",
    },
    "scaffold_work_platform": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 51210-2016；专项技术规范：JGJ 80-2016",
        "applicable": "适用于移动式/悬挑式操作平台，属危大；台面面积、高度、高宽比 ≤3 与荷载限值需验收。不适用：移动时载人、超高作业。",
        "risk": "危大工程",
    },
    "formwork_tall": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 55023-2022；专项技术规范：JGJ 162-2008《建筑施工模板安全技术规范》、GB 50666-2011",
        "applicable": "适用于各类工具式模板工程（大体积/异形/曲面）、混凝土模板支撑工程：搭设高度 ≥8m、跨度 ≥18m、"
        "施工总荷载 ≥15kN/m²、集中线荷载 ≥20kN/m 属超过一定规模危大，需专家论证；需浇筑令与支架监测。",
        "risk": "超过一定规模危大工程",
    },
    "formwork_disc": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 55023-2022、GB 51210-2016；专项技术规范：JGJ/T 231-2021、JGJ 162-2008",
        "applicable": "适用于承插型盘扣式模板支撑架；搭设高度 ≥5m 等任一超限属危大，≥8m 或总荷载 ≥15kN/m² 属超规模需论证；斜杆与自由端为验收重点。",
        "risk": "危大工程",
    },
    "tower_crane": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号、《特种设备安全法》；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 5144-2006《塔式起重机安全规程》；专项技术规范：JGJ 196-2010、GB/T 5031",
        "applicable": "适用于塔式起重机安装/顶升加节/拆卸工程，本身即属危大；起重量 ≥300kN 或搭设总高度 ≥200m 属超规模，需论证与第三方检验；四级风及以上停止顶升。",
        "risk": "危大工程",
    },
    "construction_hoist": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号、《特种设备安全法》；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 10055-2007《施工升降机安全规程》；专项技术规范：JGJ 215-2010",
        "applicable": "适用于施工升降机（人货电梯）安装/顶升/拆卸，本身即属危大；额定起重量 ≥300kN 或高度 ≥200m 属超规模；防坠安全器标定期与限位为验收重点。",
        "risk": "危大工程",
    },
    "demolition_manual": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：JGJ 147-2016《建筑拆除工程安全技术规范》（含强制性条文）；专项技术规范：GB 50870-2013",
        "applicable": "适用于采用人工方式拆除建筑物/构筑物；拆除工程属危大；影响大、相邻建（构）筑物与管线复杂。不适用：立体交叉拆除、拆除承重构件未支护。",
        "risk": "危大工程",
    },
    "demolition_machine": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：JGJ 147-2016（含强制性条文）；专项技术规范：JGJ 33-2012",
        "applicable": "适用于机械拆除（液压破碎/切割/击拆）；拆除工程属危大；需人机隔离、警戒作业半径与防尘。不适用：机械未验收、地基承载力不足。",
        "risk": "危大工程",
    },
    "demolition_blast": {
        "basis": "法律法规：《民用爆炸物品安全管理条例》（国务院令 466 号）、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 6722-2014《爆破安全规程》；专项技术规范：JGJ 147-2016",
        "applicable": "适用于爆破（控制爆破/预裂/松动）拆除；爆破拆除本身即属超过一定规模危大工程，需专家论证、公安爆破许可与安全评估；需震速与飞石监测。",
        "risk": "超过一定规模危大工程",
    },
    "curtain_wall": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 50210-2018《建筑装饰装修工程质量验收标准》（含强制性条文）；专项技术规范：JGJ 102-2003、JGJ 133-2001",
        "applicable": "适用于玻璃/金属/石材幕墙安装；本身即属危大；施工高度 ≥50m 属超过一定规模危大，需专家论证；四性试验与拉拔为验收重点。",
        "risk": "危大工程",
    },
    "steel_structure": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 50205-2020《钢结构工程施工质量验收标准》（含强制性条文）；专项技术规范：GB 50755-2012、JGJ 276-2012",
        "applicable": "适用于钢结构（构件）安装与吊装；本身即属危大；跨度 ≥36m 属超过一定规模危大，需专家论证；高强螺栓终拧与焊缝探伤为验收重点。",
        "risk": "危大工程",
    },
    "grid_structure": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 50205-2020（含强制性条文）；专项技术规范：JGJ 7-2010、JGJ 69-2016、GB 50755-2012",
        "applicable": "适用于网架和索膜结构安装（整体提升/分条吊装/滑移）；本身即属危大；跨度 ≥36m 属超过一定规模危大，需专家论证；同步高差与卸载为验收重点。",
        "risk": "危大工程",
    },
    "prestress": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 50204-2015《混凝土结构工程施工质量验收规范》（含强制性条文）；专项技术规范：GB 50666-2011、JGJ 85-2010",
        "applicable": "适用于预应力结构张拉（先张/后张、有/无粘结）；属其他类危大；张拉端挡护与防断筋飞出；应力与伸长双控、锚具静载为验收重点。",
        "risk": "危大工程",
    },
    "manual_dig_pile": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 50202-2018《建筑地基基础工程施工质量验收标准》（含强制性条文）；专项技术规范：JGJ 94-2008、GB 51004-2015",
        "applicable": "适用于人工挖孔桩工程；属其他类危大，部分地区一律需专家论证；防塌孔与防窒息中毒为核心；每日气体检测与井上监护。",
        "risk": "危大工程",
    },
    "slope": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 50330-2013《建筑边坡工程技术规范》（含强制性条文）；专项技术规范：GB 51004-2015",
        "applicable": "适用于岩质/土质边坡开挖与支护；边坡高度 ≥6m 属危大，≥ 相应规模属超一定规模危大，需论证与监测；分段开挖、及时支护、信息化施工。",
        "risk": "危大工程",
    },
    "underground_excavation": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 50497-2019（监控量测相关强制性条文）；专项技术规范：GB 50299-1999（参照）",
        "applicable": "适用于地下暗挖（矿山法/盾构）工程；暗挖本身即属超过一定规模危大工程，需专家论证；预加固、短开挖、强支护、勤量测与监控量测。",
        "risk": "超过一定规模危大工程",
    },
    "pipe_jacking": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 50268-2008《给水排水管道工程施工及验收规范》（含强制性条文）；专项技术规范：GB 50108-2008",
        "applicable": "适用于顶管（管道顶进）工程；属其他类危大，需专家论证；工作井与后背稳定、有限空间下井作业、同步注浆与地面沉降控制。",
        "risk": "危大工程",
    },
    "underwater": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：GB 20827（承压特种设备相关，按适用项确认）；专项技术规范：JTS 205-1-2008（参照）",
        "applicable": "适用于水下作业（潜水/水下混凝土灌注/封堵）；属其他类危大，需专家论证；持证潜水、封航警戒、备用气源与应急上浮。",
        "risk": "危大工程",
    },
    "new_tech": {
        "basis": "法律法规：国务院令 393 号、住建部令第 37 号；部门规章：建办质〔2018〕31 号；"
        "强制性规范：按所应用分项对应的全文强制工程建设规范（GB 55XXX 系列）为准；专项技术规范：GB/T 50502-2009（总体依据）",
        "applicable": "适用于采用新技术、新工艺、新材料且无国家技术标准的情况；属其他类危大工程，需专家论证与样板（试验）验证。",
        "risk": "危大工程",
    },
}


# ---------------------------------------------------------------------
# 节点元数据（R179 Phase B）：内容类型 + 事实依赖
# ---------------------------------------------------------------------
# 设计约定：
# - 两个标签都是**加法字段**：仅 ``build_outline(..., with_meta=True)`` 才写入，
#   默认 False 时输出与拆分前逐字一致（零行为变更）。
# - ``content_type`` 由标题确定性派生，必落在五类枚举内 —— 这是路由到
#   「经验驱动 / 规范驱动」生成策略的依据，取值不允许自由扩展。
# - ``fact_deps`` 只声明「需要哪些事实」，不携带事实值；事实命中与未命中
#   由 ``services.outline_instance`` 判定（本模块不查库）。

CONTENT_TYPES: tuple[str, ...] = ("experience", "standard", "calculation", "schedule", "verification")

# 顺序即优先级（先命中先定档）：
#   计算书 → 验收/检测 → 计划/资源 → 规范/安全类 → 其余按经验驱动
_CONTENT_TYPE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("计算书", "验算", "计算", "荷载取值"), "calculation"),
    (("验收", "检验", "检测", "试验", "实测"), "verification"),
    (("施工计划", "进度", "工期", "资源", "劳动力", "材料", "设备", "机具", "准备", "部署"), "schedule"),
    (
        ("依据", "规范", "标准", "法规", "安全", "应急", "保证措施", "防护", "监测", "文明", "环保", "消防", "质量"),
        "standard",
    ),
)

# 标题关键词 → 事实依赖键（dep key 的语义与别名见 FACT_DEP_ALIASES）。
_TITLE_FACT_DEPS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("工程概况", "概况"), ("project_name", "participants")),
    (("设计文件", "设计"), ("design_params", "ground_conditions")),
    (("地质", "岩土", "土层"), ("ground_conditions",)),
    (("周边", "环境"), ("surroundings",)),
    (("开挖", "土方", "基坑"), ("excavation_depth",)),
    (("模板", "支撑", "支架"), ("support_height", "span", "total_load", "line_load")),
    (("脚手架", "搭设"), ("scaffold_height",)),
    (("吊装", "起重", "塔机", "升降机", "吊"), ("single_weight", "crane_capacity")),
    (("人员", "资质", "特种作业"), ("personnel",)),
    (("计划", "进度", "工期", "资源"), ("schedule",)),
    (("验收", "检测"), ("acceptance",)),
    (("应急", "风险", "危险源"), ("risk_sources",)),
    (("监测",), ("monitoring_items",)),
)

#: 事实依赖键 → 匹配 global_facts 的别名（标题/内容/fact_key 任一命中即可）。
#: 只做语义别名，不改变任何既有事实口径。
FACT_DEP_ALIASES: dict[str, tuple[str, ...]] = {
    "project_name": ("工程名称", "项目名称", "工程名"),
    "participants": ("建设单位", "施工单位", "监理单位", "设计单位"),
    "design_params": ("设计", "结构", "层高", "建筑面积", "参数"),
    "ground_conditions": ("地质", "土层", "岩土", "地基承载力", "勘察", "地下水位"),
    "surroundings": ("周边", "管线", "相邻", "环境", "道路"),
    "excavation_depth": ("开挖深度", "基坑深度", "depth"),
    "support_height": ("搭设高度", "支撑高度", "支模高度", "height"),
    "span": ("跨度", "span"),
    "total_load": ("总荷载", "total_load"),
    "line_load": ("线荷载", "line_load"),
    "scaffold_height": ("脚手架高度", "搭设高度", "scaffold"),
    "single_weight": ("单件重量", "单件", "吊重", "起重量", "weight"),
    "crane_capacity": ("起重能力", "额定起重量", "起重机械", "capacity"),
    "personnel": ("人员", "资质", "证书", "特种作业"),
    "schedule": ("工期", "进度", "节点", "日历天"),
    "acceptance": ("验收", "检测", "试验"),
    "risk_sources": ("风险", "危险源", "隐患"),
    "monitoring_items": ("监测项目", "监测频率", "预警值"),
}


def content_type_of(title: str) -> str:
    """按标题确定性给出内容类型（五类枚举，默认 experience）。"""
    text = title or ""
    for keywords, ctype in _CONTENT_TYPE_RULES:
        if any(kw in text for kw in keywords):
            return ctype
    return "experience"


def fact_deps_of(title: str) -> list[str]:
    """按标题给出事实依赖键（去重保序；无依赖返回空列表）。"""
    text = title or ""
    deps: list[str] = []
    for keywords, keys in _TITLE_FACT_DEPS:
        if any(kw in text for kw in keywords):
            for k in keys:
                if k not in deps:
                    deps.append(k)
    return deps


def annotate_outline_meta(nodes: list, template_key: str = "") -> list:
    """就地给目录节点补 ``content_type`` / ``fact_deps`` / ``placeholder``。

    仅由 ``build_outline(..., with_meta=True)`` 与模板实例化路径调用；
    未调用时节点保持 ``{title, description, children}`` 原形。
    """
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        title = str(node.get("title") or "")
        node["content_type"] = content_type_of(title)
        deps = fact_deps_of(title)
        node["fact_deps"] = deps
        if deps:
            node["placeholder"] = "；".join("{" + d + "}" for d in deps)
        children = node.get("children")
        if isinstance(children, list) and children:
            annotate_outline_meta(children, template_key)
    return nodes


def match_template(scheme_name: str) -> str:
    """按方案名称匹配最合适的模板 key（未命中返回 general）"""
    name = scheme_name or ""
    for keywords, key in RULES:
        for kw in keywords:
            if kw in name:
                return key
    return "general"


def build_outline(scheme_name: str, with_meta: bool = False) -> list[dict]:
    """生成方案的标准化三级目录树（未编号，由 renumber_outline 统一重排）。

    ``with_meta=True`` 时额外给每个节点补内容类型/事实依赖标签（Phase B）；
    默认 False 时输出与拆分前逐字一致，既有调用点零感知。
    """
    key = match_template(scheme_name)
    builder = BUILDERS.get(key) or _t_general
    outline = builder()
    if with_meta:
        annotate_outline_meta(outline, key)
    return outline


def get_meta(scheme_name: str) -> dict:
    """返回方案对应的编制依据摘要 / 适用条件 / 危大分级"""
    key = match_template(scheme_name)
    meta = TEMPLATE_META.get(key, TEMPLATE_META["general"])
    return {"template": key, **meta}


def list_templates() -> list[dict]:
    """列出全部模板（供前端『套用标准模板』下拉使用）"""
    return [
        {
            "key": k,
            "name": _TEMPLATE_NAMES.get(k, k),
            "applicable": TEMPLATE_META.get(k, {}).get("applicable", ""),
            "risk": TEMPLATE_META.get(k, {}).get("risk", ""),
        }
        for k in BUILDERS
    ]


_TEMPLATE_NAMES: dict[str, str] = {
    "foundation_pit": "基坑与土方工程",
    "formwork": "模板与支撑工程",
    "scaffold": "脚手架工程",
    "lifting": "起重吊装与机械安拆",
    "temp_electricity": "施工现场临时用电",
    "work_at_height": "高处作业与临边防护",
    "confined_space": "有限空间作业",
    "fire_water": "临时设施与现场消防",
    "prefabricated": "装配式与钢结构",
    "waterproof": "防水与渗漏治理",
    "decoration": "装饰装修与幕墙",
    "mep": "机电安装与智能化",
    "quality": "质量计划与创优",
    "safety_management": "安全文明与风险管理",
    "emergency": "应急预案与处置",
    "seasonal": "冬雨季与高温季节施工",
    "green": "绿色施工与环境保护",
    "earthwork": "土方开挖与回填",
    "masonry": "砌体与二次结构",
    "demolition": "拆除工程",
    "monitoring": "监测与变形观测",
    "general": "通用方案骨架",
    # —— 脚手架族（逐型式）
    "scaffold_ground": "落地式钢管脚手架",
    "scaffold_attached": "附着式升降脚手架",
    "scaffold_cantilever": "悬挑式脚手架",
    "scaffold_gate": "门式脚手架",
    "scaffold_cuplock": "碗扣式钢管脚手架",
    "scaffold_disc": "承插型盘扣式脚手架",
    "scaffold_gondola": "高处作业吊篮",
    "scaffold_unload_platform": "悬挑卸料平台",
    "scaffold_work_platform": "移动式操作平台",
    # —— 模板支撑族
    "formwork_tall": "高大模板支撑体系",
    "formwork_disc": "承插型盘扣式模板支撑架",
    # —— 起重族
    "tower_crane": "塔式起重机安装拆卸",
    "construction_hoist": "施工升降机安装拆卸",
    # —— 拆除族
    "demolition_manual": "人工拆除工程",
    "demolition_machine": "机械拆除工程",
    "demolition_blast": "爆破拆除工程",
    # —— 其他危大族
    "curtain_wall": "建筑幕墙安装",
    "steel_structure": "钢结构安装",
    "grid_structure": "网架和索膜结构安装",
    "prestress": "预应力结构张拉",
    "manual_dig_pile": "人工挖孔桩工程",
    "slope": "边坡工程",
    "underground_excavation": "地下暗挖工程",
    "pipe_jacking": "顶管工程",
    "underwater": "水下作业工程",
    "new_tech": "新技术新工艺新材料新设备",
}
