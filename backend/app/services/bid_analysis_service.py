"""项目资料结构化提取服务（对齐《专项方案生成与编制 — 基本信息需求清单》）。

与本项目现有「文件解析」（file→text）的关系：
  - 文件解析 = 把 DOCX/PDF/图片转为纯文本（file_parser.py，基础层）
  - 结构化提取 = 把纯文本按 18 个固定维度提取为 Markdown/JSON 结构（本文件，业务层）

维度体系（2026-09-17 全面重构）：
  - 12 项方案基本信息：对齐《专项方案生成与编制 — 基本信息需求清单》的十二大类
    （项目级、方案级、工程概况、编制依据、施工条件、施工部署、施工工艺、资源配置、
      安全措施、质量验收、应急处置、计算书图纸）
    ⚠️ 原「全局事实」「输出与导出偏好」两类已下线（改由 global-facts 模块独立承担），
    对应 Prompt 槽位已删除，勿按旧注释恢复。
  - 6 项施工组织设计：原「施工组织设计（新增）」分组整体保留
  → 合计 18 项（17 必选 + 1 可选「资源配置」）

解析项定义**只在此维护一份**（避免前后端双份定义的分叉风险）。
Prompt 文本使用 __PLACEHOLDER__ 占位符避免与 Python .format() 冲突，统一在 render() 时
替换。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("bid_analysis")

# =========================================================================
# 一、18 项解析项定义（唯一权威源）
# =========================================================================
# 1-12：方案基本信息 12 大类（对齐需求清单一~十二章）
# 13-18：施工组织设计（新增 6 项，整体保留）
# output_type: markdown → 存 Markdown 文本；json → 存 JSON 字符串
# required: 1=必选项（需求清单「结论」中必填或强建议的 17 大类），0=可选（1 个）
# group: 13 分组，供前端展示用
ANALYSIS_ITEMS: list[dict] = [
    # --- 方案基本信息 12 大类（需求清单一~十二）---
    {
        "item_id": "projectBasicInfo", "label": "项目级基本信息", "required": 1,
        "output_type": "json", "sort_order": 1, "group": "project_info",
    },
    {
        "item_id": "schemeBasicInfo", "label": "方案级基本信息", "required": 1,
        "output_type": "markdown", "sort_order": 2, "group": "scheme_info",
    },
    {
        "item_id": "overviewParams", "label": "工程概况与设计参数", "required": 1,
        "output_type": "markdown", "sort_order": 3, "group": "overview_params",
    },
    {
        "item_id": "compilationBasis", "label": "编制依据", "required": 1,
        "output_type": "markdown", "sort_order": 4, "group": "basis",
    },
    {
        "item_id": "siteConditions", "label": "施工条件与环境", "required": 1,
        "output_type": "markdown", "sort_order": 5, "group": "condition",
    },
    {
        "item_id": "deploymentSchedule", "label": "施工部署与进度", "required": 1,
        "output_type": "markdown", "sort_order": 6, "group": "deployment",
    },
    {
        "item_id": "constructionTechnique", "label": "施工工艺与技术", "required": 1,
        "output_type": "markdown", "sort_order": 7, "group": "technique",
    },
    {
        "item_id": "resourceAllocation", "label": "资源配置", "required": 0,
        "output_type": "markdown", "sort_order": 8, "group": "resource",
    },
    {
        "item_id": "safetyMeasures", "label": "安全保证措施", "required": 1,
        "output_type": "markdown", "sort_order": 9, "group": "safety",
    },
    {
        "item_id": "qualityAcceptance", "label": "质量管理与验收", "required": 1,
        "output_type": "markdown", "sort_order": 10, "group": "quality",
    },
    {
        "item_id": "emergencyResponse", "label": "应急处置", "required": 1,
        "output_type": "markdown", "sort_order": 11, "group": "emergency",
    },
    {
        "item_id": "calcAndDrawings", "label": "计算书与图纸", "required": 1,
        "output_type": "markdown", "sort_order": 12, "group": "calc_drawing",
    },
    # ================================================================
    # ✅ 施工组织设计相关（新增 6 项，2026-09-17 保留）
    # 这 6 项不是招标要求本身，而是项目资料对施工组织设计应包含的内容维度的
    # 约束。提取的是「须覆盖哪些方面、有哪些硬性要求」。
    # ================================================================
    {
        "item_id": "materialManagement", "label": "材料管理", "required": 1,
        "output_type": "markdown", "sort_order": 13, "group": "construction",
    },
    {
        "item_id": "equipmentManagement", "label": "机械管理", "required": 1,
        "output_type": "markdown", "sort_order": 14, "group": "construction",
    },
    {
        "item_id": "constructionDeployment", "label": "施工部署", "required": 1,
        "output_type": "markdown", "sort_order": 15, "group": "construction",
    },
    {
        "item_id": "constructionProcess", "label": "施工流程", "required": 1,
        "output_type": "markdown", "sort_order": 16, "group": "construction",
    },
    {
        "item_id": "workInterfaceDivision", "label": "工作界面划分", "required": 1,
        "output_type": "markdown", "sort_order": 17, "group": "construction",
    },
    {
        "item_id": "engineeringMethods", "label": "工程做法", "required": 1,
        "output_type": "markdown", "sort_order": 18, "group": "construction",
    },
]

# 解析项 id → 定义 的快速索引
_ITEM_MAP: dict[str, dict] = {it["item_id"]: it for it in ANALYSIS_ITEMS}

# 必选项 id 列表（17 个必选项 + 1 个可选项 = 18 总项）
REQUIRED_ITEM_IDS = [it["item_id"] for it in ANALYSIS_ITEMS if it["required"]]

# 13 个分组定义（供前端展示用，分组名对齐需求清单章节）
GROUPS: list[dict] = [
    {"group": "project_info", "label": "项目级基本信息", "items": [it for it in ANALYSIS_ITEMS if it["group"] == "project_info"]},
    {"group": "scheme_info", "label": "方案级基本信息", "items": [it for it in ANALYSIS_ITEMS if it["group"] == "scheme_info"]},
    {"group": "overview_params", "label": "工程概况与设计参数", "items": [it for it in ANALYSIS_ITEMS if it["group"] == "overview_params"]},
    {"group": "basis", "label": "编制依据", "items": [it for it in ANALYSIS_ITEMS if it["group"] == "basis"]},
    {"group": "condition", "label": "施工条件与环境", "items": [it for it in ANALYSIS_ITEMS if it["group"] == "condition"]},
    {"group": "deployment", "label": "施工部署与进度", "items": [it for it in ANALYSIS_ITEMS if it["group"] == "deployment"]},
    {"group": "technique", "label": "施工工艺与技术", "items": [it for it in ANALYSIS_ITEMS if it["group"] == "technique"]},
    {"group": "resource", "label": "资源配置", "items": [it for it in ANALYSIS_ITEMS if it["group"] == "resource"]},
    {"group": "safety", "label": "安全保证措施", "items": [it for it in ANALYSIS_ITEMS if it["group"] == "safety"]},
    {"group": "quality", "label": "质量管理与验收", "items": [it for it in ANALYSIS_ITEMS if it["group"] == "quality"]},
    {"group": "emergency", "label": "应急处置", "items": [it for it in ANALYSIS_ITEMS if it["group"] == "emergency"]},
    {"group": "calc_drawing", "label": "计算书与图纸", "items": [it for it in ANALYSIS_ITEMS if it["group"] == "calc_drawing"]},
    {"group": "construction", "label": "施工组织设计（新增）", "items": [it for it in ANALYSIS_ITEMS if it["group"] == "construction"]},
]

# Markdown 项无结果时模型应返回此标记
MARKDOWN_MISSING_RESULT = "未提取到"

# =========================================================================
# 二、通用约束 Prompt（system）
# =========================================================================
STABLE_SYSTEM_PROMPT = """你是建筑工程专项方案资料分析专家。严格按照以下规则从给定的项目资料文本（招标文件、设计文件、地勘报告、合同、规范、工程量清单等）中提取结构化信息：

1. 严格基于给定上下文提取，除非任务明确允许，否则不得编造或猜测。
2. 已提取到内容但局部未提及时，明确写「没有提及」。
3. 只输出最终结果，不输出过程、思考、提示语或客套话。
4. 始终使用简体中文。

JSON 格式任务的额外约束：
- 严格按照给定的 JSON 模板输出，只填写 value，不要改动 key 和结构。
- 没有的字段统一填「没有提及」。

Markdown 格式任务的额外约束：
- 使用清晰的小标题和列表组织内容。
- 如果该解析项在项目资料中完全没有提及，只输出「未提取到」四个字（不加任何其他内容）。"""

# =========================================================================
# 三、18 项专属 Prompt（user）
# =========================================================================
# 使用 __CONTEXT__ 占位符注入项目资料文本
_ITEM_PROMPTS: dict[str, str] = {

    # ================================================================
    # 一、项目级基本信息（JSON，37 字段平铺模板）
    # ================================================================
    "projectBasicInfo": """请从以下项目资料中提取【项目级基本信息】，严格按以下 JSON 模板输出（只填 value，不要改 key）：

```json
{
  "project_name": "项目名称",
  "project_number": "项目编号",
  "project_alias": "项目简称",
  "construction_unit": "建设单位",
  "contractor": "施工单位",
  "supervision_unit": "监理单位",
  "design_unit": "设计单位",
  "survey_unit": "勘察单位",
  "project_location": "工程地点",
  "administrative_region": "行政区划",
  "surrounding_roads": "周边道路",
  "engineering_type": "工程类型（房建/市政/公路/铁路/水利/电力/地铁/桥梁/隧道/管廊/机电安装等）",
  "total_building_area": "总建筑面积",
  "land_area": "占地面积",
  "total_cost": "总造价/合同额",
  "contract_duration": "合同工期",
  "planned_duration": "计划总工期（日历天）",
  "start_date": "开工日期",
  "completion_date": "竣工日期",
  "milestones": "里程碑节点",
  "project_status": "项目状态（新建/在建/改扩建/修缮改造）",
  "quality_goal": "质量目标",
  "safety_goal": "安全目标",
  "schedule_goal": "工期目标",
  "civility_goal": "文明施工目标",
  "green_goal": "绿色施工目标",
  "structure_form": "结构形式",
  "floors": "层数",
  "building_height": "建筑高度",
  "foundation_pit_depth": "基坑深度",
  "span": "跨度",
  "seismic_grade": "抗震等级",
  "project_manager": "项目经理",
  "technical_director": "技术负责人",
  "production_manager": "生产经理",
  "safety_director": "安全总监",
  "chief_supervision_engineer": "总监理工程师"
}
```

提取说明：
- 覆盖项目标识、建设主体、项目地点、工程类型、项目规模、项目周期、项目状态、项目目标、项目特征、参建人员共 10 个分项
- 目标类字段（质量/安全/工期/文明施工/绿色施工）填资料中明确的量化指标，没有则填「没有提及」
- 参建人员填姓名+职务，只有职务没有姓名时填职务
- 日期统一为「YYYY-MM-DD」或「YYYY年MM月DD日」，资料只有年份时填年份

项目资料文本：
__CONTEXT__""",

    # ================================================================
    # 二、方案级基本信息
    # ================================================================
    "schemeBasicInfo": """请从以下项目资料中提取【方案级基本信息】，输出为 Markdown。

提取要点（按分项组织）：
- 方案标识：方案名称、方案编号、版本号
- 方案类型：深基坑、高支模、脚手架、塔吊、施工电梯、临时用电、消防、安全文明、绿色施工、质量创优、进度计划、应急预案、施工组织设计、钢结构吊装、降水、土方开挖、模板工程、混凝土工程、有限空间等（资料中明确要求编制的方案类型逐一列出）
- 专业分类：土建、机电、钢结构、安全、绿色施工、质量、测量、试验等
- 适用范围：适用部位、适用阶段、适用工况
- 编制目的：编制原因、解决的主要问题
- 目标字数：方案总目标字数、各章节字数预算（如有）
- 审核要求：项目级审批、公司级审批、专家论证、监理审核（资料中明确的审批/论证要求列出）

项目资料文本：
__CONTEXT__""",

    # ================================================================
    # 三、工程概况与设计参数
    # ================================================================
    "overviewParams": """请从以下项目资料中提取【工程概况与设计参数】，输出为 Markdown。

提取要点（按分项组织，逐一核对）：
- 工程简介：工程性质、建设目的、工程规模、总投资
- 建筑设计：建筑面积、层数、层高、建筑高度、防火等级
- 结构设计：结构形式、基础形式、抗震设防烈度、结构安全等级
- 基坑工程：基坑深度、周长、面积、支护形式、降水方式、监测要求（深基坑类方案必填）
- 模板工程：模板类型、支撑高度、跨度、荷载、混凝土等级（高支模类方案必填）
- 脚手架工程：脚手架类型、搭设高度、立杆间距、步距、连墙件（脚手架类方案必填）
- 起重吊装：设备型号、起重量、幅度、高度、吊装构件重量（吊装类方案必填）
- 临时用电：用电负荷、变压器容量、配电方式、接地形式（临时用电类方案必填）
- 机电安装：系统类型、设备参数、管线材质、安装高度（如有）
- 装饰装修：装饰做法、材料类型、施工工艺（如有）
- 防水工程：防水等级、防水材料、施工工艺（如有）
- 绿色施工：节能、节水、节材、环保指标（如有）

注意：所有数值参数（深度、高度、跨度、荷载、等级）必须与原文一致，保留单位和精度。

项目资料文本：
__CONTEXT__""",

    # ================================================================
    # 四、编制依据
    # ================================================================
    "compilationBasis": """请从以下项目资料中提取【编制依据】，输出为 Markdown。

提取要点（按分项组织）：
- 法律法规：建筑法、安全生产法、建设工程质量管理条例等（列出资料中提到的名称）
- 部门规章：危大工程安全管理规定、专项施工方案编制指南等
- 规范标准：国家标准、行业标准、地方标准、企业标准（完整列出标准编号+名称，如 GB 50204-2015）
- 设计文件：施工图、设计说明、设计变更、图纸会审记录（列出文件名称/编号）
- 地勘报告：地质分层、水位、承载力、不良地质
- 合同文件：施工合同、分包合同、采购合同（列出合同名称/编号）
- 企业制度：企业技术管理制度、安全管理制度、质量管理制度（如有）
- 参考方案：类似工程专项方案、企业历史方案（如有）
- 其他依据：政府批文、专家论证意见、监测数据（如有）

注意：规范/标准编号是后续技术参数与验收标准的依据，必须完整保留编号、年份和全名，不得简写或改写。

项目资料文本：
__CONTEXT__""",

    # ================================================================
    # 五、施工条件与环境
    # ================================================================
    "siteConditions": """请从以下项目资料中提取【施工条件与环境】，输出为 Markdown。

提取要点（按分项组织）：
- 地形地貌：场地标高、地形起伏、周边建筑
- 地质条件：土层分布、承载力、压缩模量、地下水位（地勘报告中的关键参数逐一列出）
- 水文条件：地表水、地下水、洪水位、潮汐（如有）
- 周边环境：周边道路、管线、建筑物、地铁、河道（含保护/监测要求）
- 交通条件：运输道路、限高限宽、出入口（如有）
- 气候条件：气温、降雨、风速、台风、冬雨季（如有）
- 场地条件：场地面积、临时设施、材料堆场、加工区（如有）
- 资源条件：水电供应、通信、劳动力来源、材料供应（如有）
- 环境敏感点：居民区、学校、医院、文物（如有）

注意：地质参数（土层名称、厚度、承载力特征值、地下水位埋深）是基坑、降水、基础方案的计算输入，必须与地勘报告原文一致。

项目资料文本：
__CONTEXT__""",

    # ================================================================
    # 六、施工部署与进度
    # ================================================================
    "deploymentSchedule": """请从以下项目资料中提取【施工部署与进度】，输出为 Markdown。

提取要点（按分项组织）：
- 施工目标：工期目标、质量目标、安全目标、成本目标
- 施工顺序：总体施工顺序、分阶段施工顺序
- 流水段划分：流水段划分原则、流水段数量（如有）
- 施工阶段：准备阶段、实施阶段、验收阶段
- 里程碑节点：关键节点名称、计划日期（逐一列出）
- 总工期：计划总工期（日历天）
- 进度计划：各任务名称、开始天数、结束天数、依赖关系、是否里程碑（资料中有进度表的尽量保留任务清单）
- 施工机械：机械名称、型号、数量、进场时间（如有）
- 劳动力计划：工种、人数、阶段分布（如有）
- 材料计划：材料名称、规格、数量、进场时间（如有）
- 临时设施：办公区、生活区、加工区、堆场、道路（如有）
- 施工平面布置：区域划分、尺寸、位置、流向（如有）

项目资料文本：
__CONTEXT__""",

    # ================================================================
    # 七、施工工艺与技术
    # ================================================================
    "constructionTechnique": """请从以下项目资料中提取【施工工艺与技术】，输出为 Markdown。

提取要点（按分项组织）：
- 工艺流程：施工工艺步骤、工序衔接、关键控制点
- 施工方法：各工序施工方法、操作要点
- 技术参数：材料参数、设备参数、工艺参数
- 质量控制点：关键工序、隐蔽工程、检验批
- 验收标准：验收规范、允许偏差、检验方法（含允许偏差数值表）
- 新技术应用：新技术、新工艺、新材料、新设备（如有）
- 试验检测：试验项目、检测频率、检测方法（如有）
- 测量控制：测量方法、控制点布置、精度要求（如有）
- 监测要求：监测项目、监测频率、报警值（如有）

注意：允许偏差、报警值等量化指标必须保留原文数值；工艺流程按原文顺序整理，便于后续生成流程图。

项目资料文本：
__CONTEXT__""",

    # ================================================================
    # 八、资源配置
    # ================================================================
    "resourceAllocation": """请从以下项目资料中提取【资源配置】，输出为 Markdown。

提取要点（按分项组织）：
- 人员配置：项目经理、技术负责人、施工员、安全员、质检员、特种作业人员（含持证要求）
- 劳动力计划：工种、人数、阶段分布、峰值
- 机械设备：设备名称、型号、数量、功率、进场时间
- 材料计划：材料名称、规格、数量、供应商、进场时间
- 周转材料：模板、脚手架、支撑体系（如有）
- 临时用电：用电负荷、配电箱、电缆、接地（如有）
- 临时用水：用水量、水源、管网、排水（如有）
- 资金计划：资金需求、支付节点（如有）

项目资料文本：
__CONTEXT__""",

    # ================================================================
    # 九、安全保证措施
    # ================================================================
    "safetyMeasures": """请从以下项目资料中提取【安全保证措施】，输出为 Markdown。

提取要点（按分项组织）：
- 安全目标：伤亡事故控制、安全达标、文明施工
- 危险源辨识：危险源清单、风险评价、控制措施
- 重大危险源：重大危险源清单、监控措施（危大工程清单逐一列出）
- 安全技术措施：各工序安全措施、防护设施
- 安全管理制度：安全生产责任制、安全教育、安全交底
- 安全检查：检查频率、检查内容、整改要求（如有）
- 安全监测：监测项目、监测频率、报警值（如有）
- 消防措施：消防设施、动火作业、易燃材料（如有）
- 临边防护：临边、洞口、高处作业防护（如有）
- 用电安全：临时用电安全、接地保护、漏电保护（如有）
- 机械安全：机械操作规程、验收、维护（如有）

注意：危大工程清单（深基坑、高支模、起重吊装安拆等）及其论证要求是专项方案合规性的关键，必须完整提取。

项目资料文本：
__CONTEXT__""",

    # ================================================================
    # 十、质量管理与验收
    # ================================================================
    "qualityAcceptance": """请从以下项目资料中提取【质量管理与验收】，输出为 Markdown。

提取要点（按分项组织）：
- 质量目标：合格率、优良率、创优目标（如鲁班奖、省优等）
- 质量保证体系：组织机构、岗位职责、管理制度
- 质量控制措施：事前、事中、事后控制
- 检验批划分：检验批、分项、分部工程划分（如有）
- 隐蔽工程验收：验收项目、验收程序、记录（如有）
- 质量通病防治：常见质量通病、防治措施（如有）
- 成品保护：保护措施、责任分工（如有）
- 质量记录：质量记录清单、归档要求（如有）
- 竣工验收：验收条件、验收程序、验收标准

项目资料文本：
__CONTEXT__""",

    # ================================================================
    # 十一、应急处置
    # ================================================================
    "emergencyResponse": """请从以下项目资料中提取【应急处置】，输出为 Markdown。

提取要点（按分项组织）：
- 应急组织：应急领导小组、职责分工、联系方式
- 应急物资：应急物资清单、存放地点、数量（如有）
- 应急设备：应急设备清单、存放地点（如有）
- 风险分析：风险类型、发生概率、影响程度
- 应急响应：响应分级、响应程序、处置措施
- 专项应急预案：坍塌、高处坠落、火灾、触电、机械伤害、中毒等（资料中明确要求编制的专项预案逐一列出）
- 应急演练：演练计划、演练记录（如有）
- 事故报告：报告程序、报告内容、时限（如有）
- 事后处理：现场保护、事故调查、整改措施（如有）

项目资料文本：
__CONTEXT__""",

    # ================================================================
    # 十二、计算书与图纸
    # ================================================================
    "calcAndDrawings": """请从以下项目资料中提取【计算书与图纸】要求，输出为 Markdown。

提取要点（按分项组织）：
- 计算书类型：支护计算、模板支架计算、吊装计算、降水计算、用电负荷计算等（危大工程要求的计算书类型逐一列出）
- 计算参数：荷载、材料强度、安全系数、几何参数（资料中给定的计算输入参数）
- 计算软件：计算软件名称、版本、计算模型（如有）
- 计算结果：内力、位移、应力、稳定性（资料中已有的计算结果）
- 结论：是否满足规范要求、建议措施（如有）
- 图纸类型：平面布置图、剖面图、节点图、流程图、监测点布置图（要求提交的图纸清单）
- 图纸比例：比例尺、图幅（如有）
- 图纸说明：图例、标注、说明（如有）

项目资料文本：
__CONTEXT__""",

    # ================================================================
    # 十三、全局事实变量
    # ================================================================

    # ================================================================
    # 十四、输出与导出偏好
    # ================================================================

    # ================================================================
    # ✅ 施工组织设计相关 6 项（保留，2026-09-17）
    # 注意：项目资料中通常以「投标人/施工单位须在施工组织设计中包含 XX 内容」
    # 的形式要求，也可能直接给出技术规格/参数/规范条款。按原文能提取到什么就提取什么。
    # ================================================================
    "materialManagement": """请从以下项目资料中提取【材料管理】相关要求，输出为 Markdown。

提取要点（按项目资料实际提到的内容组织，不强行套模板）：
- 主要材料清单及规格（钢筋/水泥/骨料/外加剂/防水材料/装饰材料等的品牌、型号、规格参数）
- 材料采购与进场计划要求
- 材料检验/试验要求（复试项目、检测标准、见证取样规定）
- 材料存储与保管要求（特殊材料如防腐、保温、电子元器件的仓储条件）
- 甲方供货/指定品牌/指定厂家的材料清单
- 材料代换规定
- 绿色建材/环保材料要求（如有）
- 项目资料原文中明确列出的材料管理相关评分标准（如有）

项目资料文本：
__CONTEXT__""",

    "equipmentManagement": """请从以下项目资料中提取【机械管理】相关要求，输出为 Markdown。

提取要点：
- 主要施工机械/设备清单（塔式起重机、施工电梯、混凝土泵车、挖掘机、压路机、盾构机、焊接设备、检测仪器等）
- 关键设备的型号/规格/数量/性能参数要求
- 设备进场计划要求
- 特种设备检验/备案要求
- 设备操作人员资质要求（起重机械司机、塔吊司机、焊工等持证要求）
- 设备租赁/自有要求
- 项目资料原文中明确列出的机械管理相关评分标准（如有）

项目资料文本：
__CONTEXT__""",

    "constructionDeployment": """请从以下项目资料中提取【施工部署】相关要求，输出为 Markdown。

提取要点：
- 施工组织架构与管理体系（项目经理部设置、各职能部门、岗位职责）
- 施工区域划分与任务分配（分区分段施工、流水段划分）
- 总包/分包/各专业承包商的界面划分（属于 workInterfaceDivision 重点提取内容，这里侧重整体部署）
- 主要里程碑与节点工期要求
- 施工资源总平面布置要求（塔吊覆盖范围、材料堆场、加工场、办公生活区）
- 关键线路/关键工序要求
- 项目资料原文中明确列出的施工部署相关评分标准（如有）

项目资料文本：
__CONTEXT__""",

    "constructionProcess": """请从以下项目资料中提取【施工流程/施工顺序】相关要求，输出为 Markdown。

提取要点：
- 总体施工顺序（先地下后地上、先结构后装饰等原则性要求）
- 分部分项工程的施工流程（如基坑开挖→支护→基础→主体→机电→装饰→屋面的先后逻辑）
- 工序交接/验收程序（上道工序完工→验收→下道工序开工的管控要求）
- 关键工序/特殊工序的专项要求（如大体积混凝土浇筑、预应力张拉、防水施工等）
- 冬雨季施工/夜间施工安排要求
- 项目资料原文中明确列出的施工流程相关评分标准（如有）

项目资料文本：
__CONTEXT__""",

    "workInterfaceDivision": """请从以下项目资料中提取【工作界面划分】相关要求，输出为 Markdown。

提取要点：
- 各专业承包商/分包商之间的工作界面（土建/机电/幕墙/精装修/消防/弱电/园林等各承包商的工程范围边界）
- 甲方发包与乙方承包的界面（甲供材/甲指分包/独立承包与总承包的分工）
- 各工作界面的交接条件与验收标准
- 交叉作业/配合施工的管控要求
- 界面冲突处理机制
- 项目资料原文中明确列出的工作界面相关评分标准（如有）

项目资料文本：
__CONTEXT__""",

    "engineeringMethods": """请从以下项目资料中提取【工程做法/技术要求】相关内容，输出为 Markdown。

提取要点：
- 各分部分项工程的技术要求/施工工艺要求（如土方开挖、基坑支护、基础形式、主体结构、防水、砌体、抹灰、地面、门窗、幕墙、屋面、机电管线安装等）
- 采用的施工规范/标准（国标/行标/地标编号）
- 关键技术参数/强制性条文
- 新工艺/新材料/新技术的要求（如有）
- 允许偏差/验收标准
- 质量通病防治要求（如有）
- 项目资料原文中明确列出的工程做法相关评分标准（如有）

注意：
- 如果项目资料中给出了详细的技术规格书/规范要求，尽量按章节结构保留原文要点
- 如果项目资料只给了规范编号（如「按 GB 50204-2015 执行」），明确列出规范编号即可

项目资料文本：
__CONTEXT__""",

}


def get_item_prompt(item_id: str) -> str | None:
    """获取指定解析项的 Prompt 模板（user 消息内容）。"""
    return _ITEM_PROMPTS.get(item_id)


def get_item_def(item_id: str) -> dict | None:
    """获取指定解析项的定义（从唯一权威源 ANALYSIS_ITEMS 查找）。"""
    return _ITEM_MAP.get(item_id)


def get_all_items() -> list[dict]:
    """返回全部 18 项定义（拷贝，防止外部修改权威源）。"""
    return [dict(it) for it in ANALYSIS_ITEMS]


def get_groups() -> list[dict]:
    """返回 13 分组视图（供前端展示用，与 GROUPS 同源）。"""
    result = []
    for g in GROUPS:
        result.append({
            "group": g["group"],
            "label": g["label"],
            "items": [dict(it) for it in g["items"]],
        })
    return result


def build_item(content: str, item: dict) -> str:
    """组装完整的 user 消息内容：Prompt 模板 + 替换 __CONTEXT__ 占位符。"""
    prompt_template = _ITEM_PROMPTS.get(item["item_id"])
    if not prompt_template:
        # 无专属 Prompt，返回通用 Prompt（极端兜底）
        return f"请从以下项目资料文本中提取「{item['label']}」相关信息。\n\n项目资料文本：\n{content}"
    # 替换占位符（注意使用 replace 而非 .format，避免 Prompt 中的 {name} 被误处理）
    return prompt_template.replace("__CONTEXT__", content)


def build_system_prompt(section_hint: str = "", classification_hint: str = "") -> str:
    """组装完整的 system 消息：通用约束 + 可选标段上下文 + 可选危大工程分类上下文。

    ✅ 2026-09-24 新增 classification_hint（纯增量，默认空）：由 /bid-analysis/classify
    产出的方案危大工程分类结论（大类/子类/危大级别/适用规范），注入后引导 AI 在提取
    时按对应章节字段重点抽取；空值 → 与旧版逐字一致（向后兼容）。
    """
    prompt = STABLE_SYSTEM_PROMPT
    if section_hint:
        prompt += f"\n\n【当前处理标段上下文】{section_hint}"
    if classification_hint:
        prompt += f"\n\n【本方案危大工程分类结论（提取重点参考）】{classification_hint}"
    return prompt


# =========================================================================
# 四、分段策略（从 facts_extractor.py split_into_chunks 复用思路）
# =========================================================================
# 项目资料常超 20 万字符，单次 AI 调用会被截断或超模型上下文上限。
# 按固定上限切分，留出 500 字符 overlap 确保跨段内容不丢失。
DEFAULT_CHUNK_SIZE = 16000  # 每段字符数（约 8000-12000 token）
DEFAULT_CHUNK_OVERLAP = 500

# ---------------------------------------------------------------------------
# 增强分段策略（2026-09-23 吸收 OpenBidKit userTextSplitter.cjs 成熟思路）
#   旧实现只在 ±200 窗口内回退到「最近的换行符」，存在两类质量问题：
#     ① 会把 Markdown 代码块 / 表格从中间切断（本软件解析产物含大量表格与围栏），
#        切断后模型看到残缺片段，提取质量下降；
#     ② 窗口内无换行时只能硬切，遇到「整段无换行的长文」直接从句中截断。
#   借鉴参考实现补两点（复用思路而非照搬代码，保持签名/overlap/终止契约不变）：
#     · 代码围栏保护：断点绝不落在 ``` / ~~~ 围栏内部；硬切点若命中围栏则顺延到围栏结束；
#     · 多级边界优先级：标题/空行 > 换行 > 句末标点 > 分号 > 逗号/冒号，逐级放宽。
# ---------------------------------------------------------------------------

#: 围栏起始行（CommonMark：行首 ≤3 空格 + ≥3 个连续 ` 或 ~）
_FENCE_LINE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")

#: 边界优先级分组（越靠前越优先），在每个分组内挑「离理想断点最近且不在代码块内」者。
#: 断点取匹配结束位置（分隔符之后），使标点/换行归属当前段。
_BOUNDARY_GROUPS = (
    re.compile(r"\r?\n(?=[ \t]{0,3}#{1,6}\s)|\r?\n[ \t]*\r?\n"),  # 标题前换行 / 空行
    re.compile(r"\r?\n"),                                            # 普通换行
    re.compile(r"[。！？!?]"),                                        # 句末标点
    re.compile(r"[；;]"),                                             # 分号
    re.compile(r"[，,、：:]"),                                        # 逗号/顿号/冒号
)

_BACK_WINDOW = 200   # 向前搜索边界的最大回退字符数（与旧实现一致）
_FWD_WINDOW = 100    # 向后搜索边界的最大前伸字符数（与旧实现一致）


def _collect_fence_ranges(text: str) -> list[tuple[int, int]]:
    """收集 Markdown 代码围栏覆盖的字符区间 ``[start, end)``（含围栏行本身）。

    未闭合的围栏延伸到文末。对齐 OpenBidKit
    ``userTextSplitter.collectMarkdownFenceRanges``，并遵循 CommonMark：
    闭合围栏须与开围栏同种字符且长度不小于开围栏长度。
    """
    ranges: list[tuple[int, int]] = []
    opened_at = -1
    opened_marker = ""
    pos = 0
    for line in text.split("\n"):
        m = _FENCE_LINE_RE.match(line)
        if m:
            marker = m.group(1)
            if opened_at < 0:
                opened_at = pos
                opened_marker = marker
            elif (marker[0] == opened_marker[0]
                  and len(marker) >= len(opened_marker)):
                ranges.append((opened_at, pos + len(line)))
                opened_at = -1
                opened_marker = ""
        pos += len(line) + 1  # +1 还原被 split 去掉的换行符
    if opened_at >= 0:
        ranges.append((opened_at, len(text)))
    return ranges


def _inside_fence(idx: int, ranges: list[tuple[int, int]]) -> bool:
    """判断字符位置是否落在某个代码围栏内部（边界处不算）。"""
    for s, e in ranges:
        if s < idx < e:
            return True
    return False


def _fence_end_at(idx: int, ranges: list[tuple[int, int]]) -> Optional[int]:
    """若 idx 落在某围栏内部，返回该围栏结束位置（可安全切断处）；否则 None。"""
    for s, e in ranges:
        if s < idx < e:
            return e
    return None


def _choose_boundary_cut(text: str, start: int, end: int, limit: int,
                         fence_ranges: list[tuple[int, int]]) -> int:
    """在 end 附近的 ±窗口内按边界优先级挑一个「不在代码围栏内」的最佳断点。

    返回断点位置（>start）；窗口内找不到任何可接受边界时返回 -1。
    """
    back = max(start + 1, end - _BACK_WINDOW)
    fwd = min(limit, end + _FWD_WINDOW)
    if fwd <= back:
        return -1
    window = text[back:fwd]
    for pattern in _BOUNDARY_GROUPS:
        best = -1
        best_score = None
        for m in pattern.finditer(window):
            cut = back + m.end()
            if cut <= start or cut >= limit:
                continue
            if _inside_fence(cut, fence_ranges):
                continue
            score = abs(cut - end)
            if best_score is None or score < best_score:
                best_score = score
                best = cut
        if best > 0:
            return best
    return -1


def split_for_analysis(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE,
                        overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    """把超长项目资料按字符切分为多段，保证 AI 调用不超上下文。

    切分策略：优先在自然边界（标题/空行 > 换行 > 句末 > 分号 > 逗号）断开，
    且**绝不切断 Markdown 代码围栏/表格**；窗口内无自然边界时退回硬切。
    保留 chunk_size/overlap 语义与「每轮至少前进 1 字符」的终止保证（向后兼容）。
    """
    text = text or ""
    if len(text) <= chunk_size:
        return [text]
    fence_ranges = _collect_fence_ranges(text)
    segments = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        # 非最后一段：在窗口内挑不切断代码块的自然边界
        if end < n:
            cut = _choose_boundary_cut(text, start, end, n, fence_ranges)
            if cut > 0:
                end = cut
            elif _inside_fence(end, fence_ranges):
                # 窗口内无自然边界且硬切点落在代码块内 → 顺延到围栏结束，绝不切断代码块
                fence_end = _fence_end_at(end, fence_ranges)
                if fence_end:
                    end = min(fence_end, n)
        segments.append(text[start:end].strip())
        if end >= n:
            break
        # ✅ BUG 修复（2026-09-22）：旧实现 `start = max(end - overlap, 0)` 在
        #    chunk_size <= overlap 时会让 start 原地不动（如 chunk_size=40、
        #    overlap=500 → max(-460, 0) = 0）→ **while 死循环**，请求永不返回、
        #    任务永久 running。该组合是函数的合法入参（按文件大小调参 / 单测
        #    都可能命中），故必须保证每轮至少前进 1 个字符。
        start = max(end - overlap, start + 1)
    return segments


# =========================================================================
# 五、下游消费汇总
# =========================================================================
def format_downstream_context(items: dict[str, dict]) -> str:
    """把关键提取项拼成「提取项目结果」Markdown，供全局事实/正文生成下游使用。

    仅取 status='success' 且**有有效内容**的项；判定统一走 is_missing_result
    （markdown 的「未提取到」、json 的全「没有提及」都算无有效内容，不下发，
    否则下游会拿到一个只有标题、没有信息的空小节）。

    ✅ 修复（数据流审计 2026-09-23）：旧实现只遍历硬编码的 7 个 item_id，
    另外 11 项（含 10 个必选项：安全措施计划、质量保证目标、应急管理、资源配置计划、
    拟投入设备、主要材料计划、施工计算书、施工部署、施工流程、工作界面、工程做法）
    的提取结果从未进入目录/正文提示词。现按 ANALYSIS_ITEMS 权威顺序遍历全部传入项，
    做到「提取即消费」；JSON 项平铺为键值、Markdown 项原样保留；
    items 中存在但不在权威清单内的 item_id 兜底追加，防御未来新增/脏数据。
    """
    lines = ["# 提取项目结果（自动提取，供后续步骤参考）\n"]

    def _emit(item_id: str) -> None:
        item = items.get(item_id)
        if not item or item.get("status") != "success":
            return
        content = item.get("content", "")
        output_type = item.get("output_type", "")
        # 「未提取到」/全空 JSON 一律跳过（避免下发空小节）
        if is_missing_result(content, output_type):
            return
        lines.append(f"\n## {item.get('label', item_id)}")
        if output_type == "json":
            # JSON 项平铺为键值列表
            try:
                data = json.loads(content)
                for k, v in data.items():
                    if v and v != "没有提及":
                        lines.append(f"- **{k}**: {v}")
            except (json.JSONDecodeError, TypeError):
                lines.append(content)
        else:
            # Markdown 项原样保留（内含规范表格/清单）
            lines.append(content)

    emitted: set[str] = set()
    # 按 ANALYSIS_ITEMS 权威顺序遍历，保证提示词拼装确定性
    for defn in ANALYSIS_ITEMS:
        iid = defn["item_id"]
        if iid in items:
            _emit(iid)
            emitted.add(iid)
    # 兜底：items 里存在但不在权威清单中的 item_id（保持 dict 原顺序）
    for iid in items:
        if iid not in emitted:
            _emit(iid)
            emitted.add(iid)

    return "\n".join(lines)


# JSON 项中表示「该字段没有提及」的取值（与 STABLE_SYSTEM_PROMPT 的约束一致）
_JSON_EMPTY_VALUES = {"", "没有提及", MARKDOWN_MISSING_RESULT, "n/a", "无", "null", "none"}


def _json_all_empty(content: str) -> bool:
    """JSON 项是否「整体无有效信息」（所有字段都是空 / 「没有提及」）。

    ✅ 背景：`is_missing_result` 旧实现只识别 markdown 的「未提取到」标记，
    JSON 项（projectBasicInfo）即便所有字段都是「没有提及」也被判为「已完成」，
    导致 /results 的 all_required_done 误报 true、Tab 徽标变绿、用户带着
    完全空白的项目级信息进入目录生成。

    容错约定：
      - 模型可能包 ```json 代码块 → 先剥离围栏；
      - 解析失败 / 非对象 / 空对象 → 返回 False（不擅自判为缺失，交由调用方
        按原有逻辑处理，避免把「格式坏但内容存在」误判成「无内容」）。
    """
    raw = (content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(data, dict) or not data:
        return False
    for value in data.values():
        if isinstance(value, (dict, list, tuple, set)):
            if len(value) > 0:
                return False
            continue
        text = "" if value is None else str(value).strip()
        if text and text.lower() not in _JSON_EMPTY_VALUES:
            return False
    return True


def is_missing_result(content: str, output_type: str = "markdown") -> bool:
    """判断解析项是否整体无结果（前端「未提取到」标记 / 必选项缺失判定的唯一口径）。

    - 空 / 全空白 → 缺失；
    - markdown 项内容恰为「未提取到」 → 缺失；
    - json 项所有字段都是「没有提及」 → 缺失。
    """
    if not content or not content.strip():
        return True
    if output_type == "json":
        return _json_all_empty(content)
    if output_type == "markdown" and content.strip() == MARKDOWN_MISSING_RESULT:
        return True
    return False


@dataclass
class AnalysisConfig:
    """结构化解析配置。

    mode 语义：
      - key   ：全部必选项（REQUIRED_ITEM_IDS）
      - full  ：全部 18 项
      - custom：用户勾选项 + **强制补全**必选项（保证一次跑完即得到完整必选集）
      - item  ：单项/局部重跑，**严格按勾选执行、不补全必选项**
    """
    mode: str = "key"  # key | full | custom | item
    selected_item_ids: list[str] = field(default_factory=list)
    force_rerun: bool = False

    def normalize(self) -> "AnalysisConfig":
        """归一化：full 含全部；key 含全部必选项；custom 保留用户选择并补全必选项；
        item 只保留用户勾选（合法的解析项 id）。"""
        if self.mode == "full":
            self.selected_item_ids = [it["item_id"] for it in ANALYSIS_ITEMS]
        elif self.mode == "key":
            self.selected_item_ids = list(REQUIRED_ITEM_IDS)
        elif self.mode == "item":
            # ✅ 单项重跑不补全必选项：其余必选项已在前一轮落库，_get_missing_required
            #    会回退 DB 校验；若强行补全，「重跑 1 项」会变成「重跑 17 项」
            #    （AI 调用 ×17，且前端「单项重新提取」按钮语义被静默改变）。
            self.selected_item_ids = [i for i in self.selected_item_ids
                                      if get_item_def(i)]
        else:  # custom
            # 强制包含必选项
            for rid in REQUIRED_ITEM_IDS:
                if rid not in self.selected_item_ids:
                    self.selected_item_ids.append(rid)
        return self

    def get_task_items(self) -> list[dict]:
        """返回实际要执行的解析项列表（按 sort_order 排序）。"""
        self.normalize()
        selected = set(self.selected_item_ids)
        return [it for it in ANALYSIS_ITEMS if it["item_id"] in selected]


# =========================================================================
# 五、来源位置（evidence）溯源 —— 确定性反查，零 AI 成本
# =========================================================================
# 背景（2026-09-23）：提取结果详情此前只有「更新时间 + 来源(AI/人工)」，用户
# 无法核对某条提取值是摘自哪份文件的哪个位置。AI 逐项重排内容并不返回引用
# 位置，故用确定性匹配补上：把结果文本的候选句反向在原文（_combine_doc_texts
# 拼接后的 Markdown）里查出处，命中即记录 {doc, line, heading, quote}。
#
# 匹配口径（宁缺勿滥，匹配不上就留空，不编造出处）：
#   1) 整句包含：归一化（去 Markdown 符号/空白/标点）后互为子串 —— 覆盖
#      条文、参数表这类逐字抄写的内容（提取结果的大头）；
#   2) 数字锚点：结果句与原文行共享「特征 token」（含字母的规格号如 C30/
#      HRB400，或 ≥2 个数字 token 相同）—— 覆盖被轻度改写但数字忠实的内容。

_DOC_HEADER_RE = re.compile(r"^#\s*文档：(.+?)（分类：.*?）\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# 特征 token：字母+数字规格号（C30/HRB400/GB50300）、数字+单位（8.5m/1200mm）、
# 纯数字（由 _token_set 按长度/小数过滤掉无区分度的个位数）
_TOKEN_RE = re.compile(
    r"[A-Za-z]{1,6}\d+(?:\.\d+)?|\d+(?:\.\d+)?(?:[A-Za-z]{1,6})?")
_NORM_STRIP_RE = re.compile(r"[#>*`|_~\-—\[\]()（）【】:：,，。；;、'\"“”‘’!！?？\s]+")


def _norm_evidence(s: str) -> str:
    """evidence 匹配归一化：去掉 Markdown 符号、空白与常见标点后小写。"""
    return _NORM_STRIP_RE.sub("", s or "").lower()


def _doc_line_index(combined_text: str) -> list[dict]:
    """把拼接原文解析成按文档分组的行索引（带标题路径），带单槽缓存。

    一次全量提取要跑 18 个解析项，每项都要对同一份原文反查；索引构建是
    O(总字数)，缓存后整个运行只建一次（以哈希为键，原文变化自动失效）。
    """
    import hashlib
    key = hashlib.blake2b(combined_text.encode("utf-8"), digest_size=16).hexdigest()
    cache = getattr(_doc_line_index, "_cache", None)
    if cache and cache[0] == key:
        return cache[1]

    docs: list[dict] = []
    current = {"file_name": "项目资料", "headings": [], "lines": []}
    docs.append(current)
    for ln, raw in enumerate(combined_text.splitlines(), start=1):
        m = _DOC_HEADER_RE.match(raw.strip())
        if m:
            # 新的 `# 文档：xxx（分类：yyy）` 头 → 开一份新文档，标题栈重置
            current = {"file_name": m.group(1).strip(), "headings": [], "lines": []}
            docs.append(current)
            continue
        hm = _HEADING_RE.match(raw.strip())
        if hm:
            level = len(hm.group(1))
            title = hm.group(2).strip()
            path = current["headings"][:level - 1] + [title]
            current["headings"] = path
        norm = _norm_evidence(raw)
        if len(norm) >= 6:
            toks = _token_set(raw)
            current["lines"].append({
                "line": ln, "raw": raw.strip(), "norm": norm,
                "heading": " › ".join(current["headings"]),
                # 预拆两类 token：含字母规格号（C30/HRB400）与纯数字，
                # 匹配热路径直接取集合，不再逐行重算
                "atokens": {t for t in toks if any(c.isalpha() for c in t)},
                "ntokens": {t for t in toks if not any(c.isalpha() for c in t)},
            })
    index = [d for d in docs if d["lines"]]
    _doc_line_index._cache = (key, index)
    return index


def _token_set(s: str) -> set[str]:
    """抽取特征 token（小写归一）：字母数字混合规格号 + 有区分度的纯数字。"""
    out: set[str] = set()
    for m in _TOKEN_RE.finditer(_norm_evidence(s)):
        t = m.group(0)
        has_alpha = any(c.isalpha() for c in t)
        digits = re.sub(r"\D", "", t)
        if has_alpha or len(digits) >= 3 or ("." in t and len(digits) >= 2):
            out.add(t)
    return out


def _evidence_entry(doc: dict, line: dict, matched: str,
                    field_path: str = "") -> dict:
    """组装一条证据：文档名 + 行号 + 标题路径 + 原文摘录（截断）。"""
    entry = {
        "doc": doc["file_name"],
        "line": line["line"],
        "heading": line["heading"],
        "quote": (line["raw"] or "")[:120],
        "match": matched[:60],
    }
    if field_path:
        entry["field"] = field_path
    return entry


def _match_candidate(cand_norm: str, cand_tokens: set[str],
                     docs: list[dict]) -> Optional[tuple[dict, dict]]:
    """在一个候选句上找原文出处：优先整句包含，其次数字锚点。返回 (doc, line)。"""
    cand_alpha = {t for t in cand_tokens if any(c.isalpha() for c in t)}
    cand_num = cand_tokens - cand_alpha
    anchor_hit = None
    for doc in docs:
        for line in doc["lines"]:
            ln_norm = line["norm"]
            if cand_norm in ln_norm or ln_norm in cand_norm:
                # 整句命中直接返回（逐字出处最可信）
                return doc, line
            if anchor_hit is None:
                # 锚点命中先记下，继续扫描看是否有整句命中
                if cand_alpha & line["atokens"] \
                        or len(cand_num & line["ntokens"]) >= 2:
                    anchor_hit = (doc, line)
    return anchor_hit


def build_evidence(result_text: str, output_type: str,
                   combined_text: str, max_items: int = 10) -> list[dict]:
    """为一条提取结果反查来源位置（见模块注释的匹配口径）。

    result_text: 落库内容（markdown 文本或 JSON 字符串）；
    combined_text: _combine_doc_texts 的产物（即本次提取的输入原文）；
    返回证据列表（可为空——匹配不上时如实留空，绝不编造出处）。
    """
    result_text = (result_text or "").strip()
    if (not result_text or not combined_text
            or is_missing_result(result_text, output_type)):
        return []
    docs = _doc_line_index(combined_text)
    if not docs:
        return []

    evidences: list[dict] = []
    seen: set[tuple] = set()

    def _push(cand: str, field_path: str = "") -> bool:
        cand_norm = _norm_evidence(cand)
        if len(cand_norm) < 8:
            return False
        hit = _match_candidate(cand_norm, _token_set(cand), docs)
        if not hit:
            return False
        doc, line = hit
        dedupe = (doc["file_name"], line["line"], field_path)
        if dedupe in seen:
            return False
        seen.add(dedupe)
        evidences.append(_evidence_entry(doc, line, cand, field_path))
        return len(evidences) >= max_items

    if output_type == "json":
        try:
            data = json.loads(result_text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return []

        def _walk(node, path: str) -> bool:
            if isinstance(node, dict):
                for k, v in node.items():
                    if _walk(v, f"{path}.{k}" if path else str(k)):
                        return True
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    if _walk(v, f"{path}[{i}]"):
                        return True
            elif isinstance(node, str):
                if node.strip() and node.strip() != "没有提及":
                    return _push(node, path)
            return False

        _walk(data, "")
    else:
        # markdown：逐行→再按句切分，取前若干个候选（成本护栏：结果通常几十行，
        # 上限 80 个候选足够覆盖要点行）
        candidates: list[str] = []
        for raw_line in result_text.splitlines():
            line_txt = re.sub(r"^[\s\-•*\d.、)+]+", "", raw_line).strip()
            if not line_txt or set(line_txt) <= {"#", " ", "-", ":", "：", "|", "*"}:
                continue
            parts = re.split(r"[。；;]", line_txt)
            for p in parts:
                p = p.strip()
                if len(_norm_evidence(p)) >= 8:
                    candidates.append(p)
            if len(candidates) >= 80:
                break
        for cand in candidates[:80]:
            if _push(cand):
                break
    return evidences


def build_evidence_json(result_text: str, output_type: str,
                        combined_text: str) -> str:
    """build_evidence 的落库封装：空结果返回 ''，异常由调用方兜底。"""
    items = build_evidence(result_text, output_type, combined_text)
    if not items:
        return ""
    return json.dumps(items, ensure_ascii=False)
