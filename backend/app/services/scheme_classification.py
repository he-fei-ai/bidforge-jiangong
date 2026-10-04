"""危大工程专项方案分类体系（提取项目模块 · 自动识别 / 规范匹配 / 九大章节字段映射）

法规依据
--------
- 住房城乡建设部令第37号《危险性较大的分部分项工程安全管理规定》
- 建办质〔2018〕31号《关于实施〈危险性较大的分部分项工程安全管理规定〉有关问题的通知》
- 建办质〔2021〕48号《危险性较大的分部分项工程专项施工方案编制指南》
- 各省市危大工程安全管理实施细则（阈值以部文为基准，地方从严时以地方为准）

本模块是「提取项目模块」的确定性核心（纯函数、零 AI、零 DB 依赖），负责：
1. 六大类危大工程 + 子类 分类体系（含识别特征关键词）；
2. 危大工程 / 超过一定规模危大工程 的阈值判定（确定性，依据部文附件）；
3. 方案名称关键词解析 → 自动判定所属大类与子类；
4. 九大章节（建办质〔2018〕31号）与现有 18 提取项的字段映射；
5. 字段完整性校验（差集分析，定位缺失章节/字段）。

接入层（routers/bid_analysis.py、services/bid_analysis_service.py）按需调用，
不改变既有 18 项提取的任何字段与默认值（向后兼容）。阈值取用本文件常量，
后续若需按省市细则差异化，可经 config 注入覆盖（当前默认部文口径）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# =========================================================================
# 一、六大类危大工程 + 子类 分类体系
# =========================================================================
# 每个子类含：
#   id          唯一标识（stable，供前端/DB 落库）
#   name        子类名称
#   keywords    识别特征关键词（方案名称 / 资料命中即归类的确定性依据）
#   standards   关联 standards_registry.CATEGORY_KEYWORDS 的类别键（用于匹配编制依据）
#   threshold   阈值判定键（见 HAZARD_THRESHOLDS；None 表示非参数型，按是否出现即危大）
# 大类含：
#   id/name/subs 以及 standards（该大类在 standards_registry 的类别键集合）

HAZARD_CATEGORIES: list[dict] = [
    {
        "id": "foundation_pit", "name": "基坑工程",
        "standards": ["基坑"],
        "subs": [
            {
                "id": "fp_support_drain", "name": "基坑支护与降水工程",
                "keywords": ("基坑支护", "支护", "降水", "地下连续墙", "SMW", "排桩",
                             "地下水位", "坑底以上", "止水帷幕", "内支撑", "围护"),
                "standards": ["基坑"],
                "threshold": "fp_support_drain",
            },
            {
                "id": "fp_earthwork", "name": "土方开挖工程",
                "keywords": ("土方开挖", "开挖", "挖土", "基坑开挖", "石方开挖", "管沟开挖"),
                "standards": ["基坑"],
                "threshold": "fp_earthwork",
            },
            # ✅ 2026-10-01 覆盖度补齐（对照用户清单「基坑工程：基坑支护与降水、
            #    土方开挖、**基坑监测**」）：基坑监测是危大基坑的强制性配套措施
            #    （建办质〔2018〕31号 附件一 基坑工程明确要求设置监测），此前
            #    本大类只有 2 个子类，方案名写「基坑监测专项方案」时 is_dangerous
            #    判为 False，九大必要章节约束与监测章节模板不触发。
            #    ⚠️ threshold 设为 None：监测本身无独立阈值参数，按「是否出现即
            #    危大」处理；不要为本子类新建数值阈值（无部文依据，凭记忆编造
            #    阈值是本项目明确禁止的做法）。
            {
                "id": "fp_monitoring", "name": "基坑监测工程",
                "keywords": ("基坑监测", "变形监测", "自动化监测", "监测方案",
                            "位移监测", "沉降监测", "测斜", "监测预警"),
                "standards": ["基坑"],
                "threshold": None,
            },
        ],
    },
    {
        "id": "formwork", "name": "模板工程及支撑体系",
        "standards": ["模板"],
        "subs": [
            {
                "id": "fw_support", "name": "模板支撑体系工程",
                "keywords": ("模板支撑", "支撑体系", "支模", "模板支架", "满堂支撑",
                             "承重支撑", "钢管支撑"),
                "standards": ["模板"],
                "threshold": "fw_support",
            },
            {
                "id": "fw_tall", "name": "高大模板工程",
                "keywords": ("高大模板", "高支模", "超限梁", "大截面", "超限模板",
                             "高支撑架", "大跨度模板"),
                "standards": ["模板"],
                "threshold": "fw_tall",
            },
            # ✅ 2026-10-01 覆盖度补齐（对照用户清单「模板工程及支撑体系：模板
            #    支撑体系、高大模板、**盘扣式模板支撑**」）：盘扣式支撑与扣件式
            #    的施工参数、验收要点不同（盘扣节点间距、可调托撑伸出长度等），
            #    需独立识别以便匹配 outline_templates 的盘扣式模板支撑目录。
            #    ⚠️ threshold 复用 "fw_support"：盘扣式属模板支撑体系的一种型式，
            #    危大/超规模的判定阈值与部文附件一/二口径一致，不新增阈值记录。
            {
                "id": "fw_disc", "name": "盘扣式模板支撑体系工程",
                "keywords": ("盘扣式模板支撑", "盘扣式支撑", "承插型盘扣式",
                            "盘扣支架", "圆盘式模板支撑"),
                "standards": ["模板"],
                "threshold": "fw_support",
            },
        ],
    },
    {
        "id": "hoisting", "name": "起重吊装及起重机械安装拆卸工程",
        "standards": ["起重机械"],
        "subs": [
            {
                "id": "ho_lift", "name": "起重吊装工程",
                "keywords": ("起重吊装", "吊装", "构件吊装", "大型设备吊装", "钢结构吊装",
                             "网架吊装", "非常规起重"),
                "standards": ["起重机械"],
                "threshold": "ho_lift",
            },
            {
                "id": "ho_crane", "name": "起重机械安装拆卸工程",
                "keywords": ("塔吊安装", "起重机械安装", "拆卸", "塔式起重机", "施工升降机安装",
                             "塔机拆除", "群塔", "起重机械基础"),
                "standards": ["起重机械"],
                "threshold": "ho_crane",
            },
            # ✅ 2026-10-01 覆盖度补齐（对照用户清单「起重吊装及起重机械安装拆卸
            #    工程：起重吊装、**塔机安装拆卸**、**施工升降机安装拆卸**」）：
            #    上一个是部文口径的合并子类（"起重机械安装拆卸工程"），下面两个
            #    是**设备级**细分。塔机与施工升降机的安装基础、顶升加节、附着
            #    与验收要点差异很大，需独立识别以匹配 outline_templates 的
            #    _t_tower_crane / _t_construction_hoist 目录模板。
            #    ⚠️ threshold 复用 "ho_crane"：二者均属起重机械安装工程，危大
            #    判定与超规模阈值（300kN / 200m）口径一致，不新增阈值记录。
            #    与 ho_crane 并存而非替换：部文原文用合并表述，细分子类是产品
            #    视角的识别精度增强，多命中不影响判定结果（match_category_keywords
            #    返回命中列表，evaluate_hazard_level 取最严口径）。
            {
                "id": "ho_tower_crane", "name": "塔式起重机（塔机）安装拆卸工程",
                "keywords": ("塔机", "塔吊", "塔式起重机", "QT", "QTZ", "平衡臂",
                            "起重臂", "顶升加节", "附墙加节"),
                "standards": ["起重机械"],
                "threshold": "ho_crane",
            },
            {
                "id": "ho_construction_hoist", "name": "施工升降机安装拆卸工程",
                "keywords": ("施工升降机", "施工电梯", "齿轮齿条", "SC系列", "SC200",
                            "标准节", "附墙支架"),
                "standards": ["起重机械"],
                "threshold": "ho_crane",
            },
        ],
    },
    {
        "id": "scaffold", "name": "脚手架工程",
        "standards": ["脚手架"],
        "subs": [
            {
                "id": "sc_ground", "name": "落地式钢管脚手架（高度>24m）",
                "keywords": ("落地式脚手架", "扣件式钢管脚手架", "落地式", "钢管脚手架"),
                "standards": ["脚手架"],
                "threshold": "sc_ground",
            },
            {
                "id": "sc_attached", "name": "附着式升降脚手架",
                "keywords": ("附着式", "爬架", "升降脚手架", "附着升降"),
                "standards": ["脚手架"],
                "threshold": "sc_attached",
            },
            {
                "id": "sc_cantilever", "name": "悬挑式脚手架",
                "keywords": ("悬挑脚手架", "悬挑式", "型钢悬挑"),
                "standards": ["脚手架"],
                "threshold": "sc_cantilever",
            },
            {
                "id": "sc_other", "name": "门型脚手架、挂脚手架、吊篮脚手架、卸料平台",
                "keywords": ("门型脚手架", "门式脚手架", "挂脚手架", "吊篮", "吊篮脚手架",
                             "卸料平台", "操作平台", "移动脚手架"),
                "standards": ["脚手架"],
                "threshold": "sc_other",
            },
            # ✅ 2026-10-01 覆盖度补齐（对照用户清单「脚手架工程：落地式、附着式
            #    升降、悬挑式、门式、**碗扣式**、**盘扣式**、吊篮、卸料平台、
            #    操作平台」）：本大类此前 4 个子类里**没有碗扣式与盘扣式** ——
            #    这两类在国内房建项目用量最大，方案名写「碗扣式/盘扣式脚手架」
            #    时 is_dangerous 判为 False，九大必要章节约束与脚手架目录模板
            #    （outline_templates._t_scaffold_cuplock / _t_scaffold_disc）不触发。
            #    ⚠️ threshold 复用 "sc_ground"：碗扣式/盘扣式均按落地式钢管
            #    脚手架管理，危大阈值（24m 及以上）与超规模阈值（50m 及以上）
            #    口径一致，不新增阈值记录。
            {
                "id": "sc_cuplock", "name": "碗扣式钢管脚手架",
                "keywords": ("碗扣式", "碗扣脚手架", "碗扣式钢管", "BW"),
                "standards": ["脚手架"],
                "threshold": "sc_ground",
            },
            {
                "id": "sc_disc", "name": "盘扣式钢管脚手架",
                "keywords": ("盘扣式脚手架", "盘扣式钢管脚手架", "承插型盘扣式钢管",
                            "盘扣脚手架", "圆盘式脚手架"),
                "standards": ["脚手架"],
                "threshold": "sc_ground",
            },
        ],
    },
    {
        "id": "demolition", "name": "拆除、爆破工程",
        "standards": [],
        "subs": [
            {
                "id": "dm_manual", "name": "人工拆除工程",
                "keywords": ("人工拆除", "人工爆破", "手动拆除"),
                "standards": [],
                "threshold": None,
            },
            {
                "id": "dm_machine", "name": "机械拆除工程",
                "keywords": ("机械拆除", "机械破碎", "炮机拆除", "液压拆除"),
                "standards": [],
                "threshold": None,
            },
            {
                "id": "dm_blast", "name": "爆破拆除工程",
                "keywords": ("爆破", "爆破拆除", "控制爆破", "炸药"),
                "standards": [],
                "threshold": None,
            },
        ],
    },
    {
        "id": "other", "name": "其他危大工程",
        "standards": [],
        "subs": [
            {
                "id": "ot_curtain", "name": "建筑幕墙安装",
                "keywords": ("幕墙", "玻璃幕墙", "石材幕墙", "铝板幕墙"),
                "standards": ["装饰保温"],
                "threshold": "ot_curtain",
            },
            {
                "id": "ot_steel", "name": "钢结构（网架、索膜结构）安装",
                "keywords": ("钢结构", "网架", "索膜", "管桁架", "钢构安装"),
                "standards": [],
                "threshold": "ot_steel",
            },
            {
                "id": "ot_prestress", "name": "预应力结构张拉",
                "keywords": ("预应力", "张拉", "预应力张拉", "先张法", "后张法"),
                "standards": ["混凝土"],
                "threshold": "ot_prestress",
            },
            {
                "id": "ot_underground", "name": "地下暗挖、顶管、水下作业",
                "keywords": ("暗挖", "顶管", "水下作业", "盾构", "矿山法", "浅埋暗挖"),
                "standards": [],
                "threshold": None,
            },
            {
                "id": "ot_slope", "name": "6m以上边坡施工",
                "keywords": ("边坡", "高边坡", "边坡支护", "边坡开挖"),
                "standards": ["基坑"],
                "threshold": "ot_slope",
            },
            {
                "id": "ot_confined", "name": "有限空间作业",
                # ✅ 2026-09-26 缺口修复（六大类危大工程及子类覆盖度审计）：
                #    建办质〔2018〕31号 附件一「七、其他」明确含**有限空间作业**，
                #    但本表 `other` 六个子类此前只覆盖幕墙/钢结构/预应力/暗挖/
                #    边坡/四新，**缺失有限空间** → 方案名写「有限空间作业专项方案」
                #    时 is_dangerous 判为 False，九大必要章节约束不触发。
                #    （outline_templates 早已有 confined_space 模板，属能力悬空。）
                #    注：临时用电**不**在 31号文危大范围内，故不补入本表。
                "keywords": ("有限空间", "密闭空间", "受限空间", "有限空间作业"),
                "standards": [],
                "threshold": None,
            },
            {
                "id": "ot_newtech", "name": "采用新技术、新工艺、新材料且无技术标准的工程",
                "keywords": ("新技术", "新工艺", "新材料", "无技术标准", "四新"),
                "standards": [],
                "threshold": None,
            },
            # ✅ 2026-10-01 覆盖度补齐（对照用户清单「其他危大工程：幕墙安装、
            #    钢结构安装、网架索膜安装、预应力张拉、地下暗挖、顶管、水下作业、
            #    **人工挖孔桩**、边坡工程、新技术新工艺」）：本大类此前 7 个子类
            #    里缺「人工挖孔桩」—— 它是桩基工程中危险性最高的一种（孔内缺氧、
            #    有害气体、塌孔、涌水涌砂），住建部文件将其明确列为危大工程。
            #    缺失后果：方案名写「人工挖孔桩专项方案」时 is_dangerous 判为
            #    False，九大必要章节约束与应急章节不触发 —— 这正是本项目最不应
            #    出现的安全判定漏洞（人工挖孔桩致死事故率远高于普通桩基）。
            #    ⚠️ threshold 由 None 改为 "ot_bored_pile"（2026-10-02 第二十三轮
            #    · L-2 收口）：此前注释写「无部文原文可核时不编造阈值」。现已核对
            #    建办质〔2018〕31号**原文**（住建部官网 2018-05-17 印发件）：
            #      · 附件一 七(三)：人工挖孔桩工程 → 危大工程，**无深度门槛**
            #        （故 hazard_always=True，与原「出现即危大」方向一致）；
            #      · 附件二 七(三)：**开挖深度 16m 及以上**的人工挖孔桩工程
            #        → 超过一定规模的危大工程（闭区间口径）。
            #    旧行为（threshold=None）把人工挖孔桩**一律**判为超规模，
            #    10m 挖孔桩也被要求专家论证 —— 与部文不符（属过判）。
            {
                "id": "ot_bored_pile", "name": "人工挖孔桩工程",
                "keywords": ("人工挖孔桩", "人工挖孔", "挖孔桩", "干作业挖孔",
                            "混凝土护壁"),
                "standards": ["桩基"],
                "threshold": "ot_bored_pile",
            },
        ],
    },
]


# =========================================================================
# 二、危大工程 / 超过一定规模 阈值判定（部文附件口径）
# =========================================================================
# 判定参数单位约定：depth/height/span → 米(m)；total_load → kN/m²；line_load → kN/m。
# 每个子类一条阈值记录：
#   hazard_when  满足即「危大工程」的条件（list of (param, op, value)）
#   oversize_when 满足即「超过一定规模危大工程」的条件
#   any_match    True 表示 hazard_when 中任一满足即成立；False 表示需全部满足
HAZARD_THRESHOLDS: dict[str, dict] = {
    # 基坑：建办质〔2018〕31号 附件一 —— 开挖深度超过3m（含3m）→ 危大工程；
    # 开挖深度超过5m（含5m）→ 超过一定规模的危大工程。
    # ✅ BUG 修复（2026-09-24）：旧实现两条阈值都写成 depth>=5，把「3~5m 的基坑」
    #    误判为非危大工程（旧测试 test_scheme_classification.py::test_threshold_shallow_pit
    #    的注释里也承认了这一偏差）。专项方案会因此漏做专家论证预检、漏配监测章节，
    #    属 P0 安全判定缺陷，现按部文附件一改为 >=3 / >=5。
    "fp_support_drain": {
        "params": ("depth",),
        "hazard_when": [("depth", ">=", 3)],
        "oversize_when": [("depth", ">=", 5)],
        "any_match": True,
    },
    "fp_earthwork": {
        "params": ("depth",),
        "hazard_when": [("depth", ">=", 3)],
        "oversize_when": [("depth", ">=", 5)],
        "any_match": True,
    },
    # 模板支撑体系：搭设高度≥5m 或 跨度≥10m 或 总荷载≥10 或 线荷载≥15 → 危大
    "fw_support": {
        "params": ("height", "span", "total_load", "line_load"),
        "hazard_when": [("height", ">=", 5), ("span", ">=", 10),
                        ("total_load", ">=", 10), ("line_load", ">=", 15)],
        "oversize_when": [("height", ">=", 8), ("span", ">=", 18),
                          ("total_load", ">=", 15), ("line_load", ">=", 20)],
        "any_match": True,
    },
    # 高大模板：建办质〔2018〕31号 附件二 —— 搭设高度8m及以上、跨度18m及以上、
    # 施工总荷载15kN/m²及以上、集中线荷载20kN/m及以上 → 超过一定规模的危大工程。
    # ✅ BUG 修复（2026-09-24）：旧实现把比较符写成严格 `>` 且总荷载/线荷载阈值
    #    沿用模板支撑体系的 10/15 —— 部文「及以上」为闭区间，8m/18m 与
    #    15kN/m²/20kN/m 这几个临界值会被漏判（临界值正是专家论证的分水岭）。
    #    现统一改为 >=，并按附件二口径修正 total_load/line_load 阈值。
    "fw_tall": {
        "params": ("height", "span", "total_load", "line_load"),
        "hazard_when": [("height", ">=", 8), ("span", ">=", 18),
                        ("total_load", ">=", 15), ("line_load", ">=", 20)],
        "oversize_when": [("height", ">=", 8), ("span", ">=", 18),
                          ("total_load", ">=", 15), ("line_load", ">=", 20)],
        "any_match": True,
    },
    # 起重吊装：非常规起重设备/方法且单件≥10kN → 危大；采用起重机械安装 → 危大
    "ho_lift": {
        "params": ("single_weight",),
        "hazard_when": [("single_weight", ">=", 10)],
        "oversize_when": [("single_weight", ">=", 100)],
        "any_match": True,
    },
    # 起重机械安装拆卸：建办质〔2018〕31号 附件一 —— 采用起重机械安装工程的
    # 安装/拆卸即属危大工程；起重量达到300kN及以上或高度≥200m → 超过一定规模。
    # ✅ BUG 修复（2026-09-24）：旧实现用 hazard_when=[("crane_capacity",">=",0)]
    #    表达「凡涉及即危大」——该写法依赖「缺参时保守判危大」的副作用，一旦
    #    crane_capacity 被显式填 0（资料未给）或误填负值，判定语义就不成立；
    #    且它让 missing_params 恒非空、上游误以为「参数齐全」。
    #    现改为显式 hazard_always=True（规则自描述），缺参不再被伪装成判定依据。
    "ho_crane": {
        "params": ("crane_capacity", "crane_height"),
        "hazard_always": True,
        "hazard_when": [],
        "oversize_when": [("crane_capacity", ">=", 300), ("crane_height", ">=", 200)],
        "any_match": True,
    },
    # 落地式脚手架：建办质〔2018〕31号 附件一 —— 搭设高度 24m 及以上 → 危大；
    # 超过一定规模（附件二）：50m 及以上。
    # ✅ BUG 修复（2026-09-30）：旧实现 hazard_when 写成严格 `> 24`，而部文原文
    #    是「搭设高度 24m 及以上的落地式钢管脚手架工程」——闭区间口径。
    #    后果：恰好 24m 的脚手架被漏判为「非危大」，前端不红标、导出预检不拦，
    #    而 24m 正是部文划定的临界值（专家论证与否的分水岭）。与本文件
    #    2026-09-24 对基坑（3m/5m）、高大模板（8m/18m/15/20）做过的同类
    #    「> → >=」修正属同一根因，此处补齐以消除最后一处严格大于号。
    "sc_ground": {
        "params": ("height",),
        "hazard_when": [("height", ">=", 24)],
        "oversize_when": [("height", ">=", 50)],
        "any_match": True,
    },
    "sc_attached": {
        "params": (),
        "hazard_when": [],  # 附着式升降脚手架本身即危大（部文附件一）
        "oversize_when": [],
        "any_match": True,
    },
    "sc_cantilever": {
        "params": (),
        "hazard_when": [],  # 悬挑式脚手架本身即危大
        "oversize_when": [],
        "any_match": True,
    },
    "sc_other": {
        "params": (),
        "hazard_when": [],  # 门型/挂/吊篮/卸料平台本身即危大
        "oversize_when": [],
        "any_match": True,
    },
    # 其他类：参数型子类按阈值；非参数型（暗挖/四新等）依出现即危大
    "ot_curtain": {
        "params": ("install_height",),
        "hazard_when": [("install_height", ">=", 50)],
        "oversize_when": [("install_height", ">=", 50)],
        "any_match": True,
    },
    "ot_steel": {
        "params": ("span",),
        "hazard_when": [("span", ">=", 36)],
        "oversize_when": [("span", ">=", 36)],
        "any_match": True,
    },
    "ot_prestress": {
        "params": (),
        "hazard_when": [],
        "oversize_when": [],
        "any_match": True,
    },
    "ot_slope": {
        "params": ("slope_height",),
        "hazard_when": [("slope_height", ">=", 6)],
        "oversize_when": [("slope_height", ">=", 6)],
        "any_match": True,
    },
    # 人工挖孔桩：建办质〔2018〕31号 附件一 七(三) —— 人工挖孔桩工程本身即危大
    # （无深度门槛），故 hazard_always；附件二 七(三) —— 开挖深度 **16m 及以上**
    # 才属「超过一定规模的危大工程」（部文「及以上」为闭区间，与本文件此前对
    # 基坑 3m/5m、脚手架 24m/50m 的闭区间修正同源）。
    # ✅ 2026-10-02（第二十三轮 · L-2 收口）：此前 threshold=None →
    #    evaluate_hazard_level 的「非参数型」分支把人工挖孔桩**一律**判为超规模，
    #    10m 挖孔桩也被要求组织专家论证，与部文附件二口径不符（属过判）。
    "ot_bored_pile": {
        "params": ("pile_depth",),
        "hazard_always": True,
        "hazard_when": [],
        "oversize_when": [("pile_depth", ">=", 16)],
        # ⚠️ 缺参时**不得**判非超规模。hazard_always 分支默认「缺参即不超规模」，
        #    对本类会漏判一个真正 16m+ 的深孔（不漏判才是安全红线），
        #    故显式要求缺参时按超规模保守判定并记入 missing_params 供上游补全。
        "oversize_conservative_missing": True,
        "any_match": True,
    },
}


# =========================================================================
# 三、九大章节（建办质〔2018〕31号）与 18 提取项 字段映射
# =========================================================================
# 每个章节含：
#   chapter   章节序号（1-9）
#   key       稳定键
#   title     章节名
#   source_items  主要来源提取项（ANALYSIS_ITEMS 的 item_id）
#   base_fields  通用必填字段（所有方案类型都需覆盖）
#   category_fields  按危大类别追加的必填字段（由 required_fields_for_chapter 合并）
NINE_CHAPTERS: list[dict] = [
    {
        "chapter": 1, "key": "overview", "title": "工程概况",
        "source_items": ["projectBasicInfo", "overviewParams"],
        "base_fields": ["工程名称", "工程地点", "建设规模", "结构形式",
                        "参建各方责任主体单位", "风险辨识与分级", "气候特征"],
        "category_fields": {
            "foundation_pit": ["基坑周长/面积/深度", "支护形式", "降水方式", "监测要求"],
            "formwork": ["模板类型", "支撑高度", "跨度", "荷载", "混凝土等级"],
            "hoisting": ["吊装设备型号", "起重量", "起升高度", "被吊物参数"],
            "scaffold": ["脚手架类型", "搭设高度", "立杆间距", "连墙件"],
            "demolition": ["拆除方式", "拆除对象", "爆破等级"],
            "other": ["安装高度/跨度", "特殊工艺参数"],
        },
    },
    {
        "chapter": 2, "key": "basis", "title": "编制依据",
        "source_items": ["compilationBasis"],
        "base_fields": ["适用法规清单", "适用标准清单", "施工图编号",
                        "施工组织设计编号", "施工合同编号"],
        "category_fields": {
            "foundation_pit": ["JGJ120", "GB50497", "GB50202"],
            "formwork": ["JGJ162", "GB51210", "JGJ300"],
            "hoisting": ["JGJ276", "JGJ196", "JGJ215"],
            "scaffold": ["JGJ130", "JGJ202", "GB55023"],
            "demolition": ["GB6722（爆破）"],
            "other": ["专项标准编号"],
        },
    },
    {
        "chapter": 3, "key": "plan", "title": "施工计划",
        "source_items": ["deploymentSchedule", "resourceAllocation"],
        "base_fields": ["计划开工日期", "计划竣工日期", "分项进度节点",
                        "材料需求清单", "设备配置清单", "劳动力配置表"],
        "category_fields": {},
    },
    {
        "chapter": 4, "key": "technique", "title": "施工工艺技术",
        "source_items": ["constructionTechnique"],
        "base_fields": ["材料选型", "规格", "技术参数", "工艺流程步骤",
                        "施工方法描述", "操作要求", "质量检查标准"],
        "category_fields": {},
    },
    {
        "chapter": 5, "key": "safety", "title": "施工安全保证措施",
        "source_items": ["safetyMeasures"],
        "base_fields": ["安全组织机构", "安全职责分工", "技术措施清单",
                        "监测方案参数", "预警值"],
        "category_fields": {},
    },
    {
        "chapter": 6, "key": "personnel", "title": "施工管理及作业人员配备和分工",
        "source_items": ["resourceAllocation", "projectBasicInfo"],
        "base_fields": ["管理人员名单及岗位", "安全员名单",
                        "特种作业人员及证书编号", "作业人员配置"],
        "category_fields": {},
    },
    {
        "chapter": 7, "key": "acceptance", "title": "验收要求",
        "source_items": ["qualityAcceptance"],
        "base_fields": ["验收标准编号", "验收程序步骤", "验收内容清单",
                        "验收人员组成"],
        "category_fields": {},
    },
    {
        "chapter": 8, "key": "emergency", "title": "应急处置措施",
        "source_items": ["emergencyResponse"],
        "base_fields": ["应急组织架构", "应急联系人及电话", "应急物资清单",
                        "救援线路", "附近医院信息"],
        "category_fields": {},
    },
    {
        "chapter": 9, "key": "calc_drawings", "title": "计算书及相关施工图纸",
        "source_items": ["calcAndDrawings"],
        "base_fields": ["计算书类型", "计算参数", "图纸清单", "图纸编号"],
        "category_fields": {},
    },
]

# 类别 → standards_registry.CATEGORY_KEYWORDS 类别键（供 get_standards_text 注入编制依据）
CATEGORY_STANDARDS_KEYS: dict[str, list[str]] = {
    cat["id"]: list(cat.get("standards") or []) for cat in HAZARD_CATEGORIES
}


# =========================================================================
# 四、方案名称关键词解析与自动分类（纯函数）
# =========================================================================
# 通用方案名称后缀，从尾部剥离（与 scheme_scope 口径一致，避免后缀噪声干扰命中）
_SUFFIX_RE = re.compile(r"(?:安全)?(?:专项)?(?:施工)?(?:组织)?(?:方案|设计|预案|措施)$")


def _strip_suffix(name: str) -> str:
    out = (name or "").strip()
    while True:
        stripped = _SUFFIX_RE.sub("", out)
        if stripped == out:
            return out
        out = stripped


def match_category_keywords(text: str) -> list[dict]:
    """按关键词命中危大工程大类与子类（确定性，零 AI）。

    Args:
        text: 待匹配文本（方案名称 / 资料片段）。

    Returns:
        命中列表，元素为 {"category_id","category_name","sub_id","sub_name"}，
        按 HAZARD_CATEGORIES 定义顺序排列（同一子类只出现一次）。
    """
    if not text:
        return []
    hay = (text or "").lower()
    hits: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for cat in HAZARD_CATEGORIES:
        for sub in cat.get("subs", []):
            for kw in sub.get("keywords", ()):
                if kw.lower() in hay:
                    key = (cat["id"], sub["id"])
                    if key not in seen:
                        seen.add(key)
                        hits.append({
                            "category_id": cat["id"],
                            "category_name": cat["name"],
                            "sub_id": sub["id"],
                            "sub_name": sub["name"],
                        })
                    break
    return hits


def is_hazardous_by_keywords(text: str = "") -> bool:
    """方案文本（名称/类型拼接）是否命中危大工程（确定性关键词反查，零 AI）。

    ✅ 新增（2026-10-02）：危大判定的**单一事实源**出口 —— 正文生成侧
    （content_checkpoint / 自检）与审核预检侧（STD-04 危大法规门控）共用，
    避免「生成按危大约束写、预检按非危大判」的判据分叉。
    异常降级为非危大（与历史行为一致，绝不让判定异常打断主流程）。
    """
    try:
        return bool(match_category_keywords(text))
    except Exception:
        return False


def classify_scheme_name(scheme_name: str) -> list[dict]:
    """从专项方案名称解析施工内容并自动归类（别名，语义更明确）。

    仅依赖名称字面关键词，绝不推断名称之外的施工内容（与 scheme_scope 一致）。
    """
    core = _strip_suffix(scheme_name or "")
    return match_category_keywords(core or scheme_name or "")


# =========================================================================
# 五、危大工程 / 超过一定规模 阈值判定（纯函数）
# =========================================================================
_OPS = {
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: a == b,
}


def _check_oversize(rule: dict, params: dict) -> list[str]:
    """判定「超过一定规模」条件是否命中，返回命中的可读原因列表（纯判定，无副作用）。"""
    reasons: list[str] = []
    for pname, op, val in rule.get("oversize_when") or []:
        if pname not in params:
            continue
        try:
            actual = float(params[pname])
        except (TypeError, ValueError):
            continue
        if _OPS[op](actual, float(val)):
            reasons.append(f"{pname}={actual} 满足 {op}{val}")
    return reasons


def resolve_threshold_key(sub_id: Optional[str]) -> Optional[str]:
    """子类 id → HAZARD_THRESHOLDS 键（阈值复用的唯一解析出口）。

    子类表里 ``"threshold"`` 允许指向**另一个**子类的阈值键 —— 这是有意复用：
    盘扣 / 碗扣脚手架与落地式同为 24m/50m（部文口径相同），塔机 / 施工升降机
    与起重机械安装拆卸同为 300kN/200m，盘扣式模板支撑与模板支撑体系同为
    8m/18m/15/20。复用的是**部文口径**，不是实现细节。

    ⚠️ 本函数是**唯一**出口：任何要按子类做阈值判定的地方都必须先过它。
    直接把 sub_id 当阈值键用是本仓踩过的 P0 坑 —— ``HAZARD_THRESHOLDS``
    里没有复用子类的键，``evaluate_hazard_level`` 会落进「非参数型」分支，
    把"没有阈值记录"误读成"本身即危大即超规模"，于是 30m 碗扣脚手架
    （部文口径 50m 才超规模）被要求组织专家论证。

    Args:
        sub_id: 子类 id。

    Returns:
        可用于 ``HAZARD_THRESHOLDS`` 查表的键；子类未声明阈值时返回
        ``sub_id`` 本身（交由 evaluate_hazard_level 走「非参数型」兜底，
        与旧行为一致，不改变无阈值子类的语义）。
    """
    if not sub_id:
        return None
    for cat in HAZARD_CATEGORIES:
        for s in cat.get("subs", ()):
            if s.get("id") == sub_id:
                return s.get("threshold") or sub_id
    return sub_id


def evaluate_hazard_level(threshold_key: Optional[str],
                          params: Optional[dict] = None) -> dict:
    """依据阈值记录判定危大 / 超规模。

    Args:
        threshold_key: HAZARD_THRESHOLDS 的键（对应子类 id）；None 表示非参数型
            （出现即危大，超规模同危大）。
        params: 判定参数字典（如 {"depth": 6.2, "height": 9}），单位见 HAZARD_THRESHOLDS。

    Returns:
        {"is_hazardous": bool, "is_oversize": bool,
         "hazard_reasons": [str], "oversize_reasons": [str],
         "missing_params": [str]}
        无阈值记录或参数缺失 → is_hazardous 按「非参数型即危大」处理（保守判定，
        确保不漏判危大工程；缺参项列入 missing_params 供上游补全）。
    """
    params = params or {}
    rule = HAZARD_THRESHOLDS.get(threshold_key) if threshold_key else None

    # ✅ 显式「本身即危大」标记（hazard_always）：规则不依赖任何判定参数，
    #    凡涉及该子类即危大（起重机械安装拆卸等）。缺参不再计入 missing_params，
    #    避免上游误以为「参数齐全」。
    if rule is not None and rule.get("hazard_always"):
        oversize_reasons = _check_oversize(rule, params)
        missing_oversize = [
            p for p in rule.get("params", ())
            if p not in params
        ]
        # ✅ 2026-10-02：超规模判定的**缺参方向必须显式声明**。
        #    本分支默认「缺参 → 不判超规模」（参数型危大如基坑 3m 未给深度时
        #    不该断言超规模）。但人工挖孔桩不同：它**本身即危大**、超规模与否
        #    只差一个深度门槛，缺参时若按默认判「非超规模」，一个真正的 16m+
        #    深孔会被漏掉专家论证 —— 漏判是安全红线，过判只是多花成本。
        #    故由 oversize_conservative_missing 显式开启「缺参按超规模」。
        conservative = bool(rule.get("oversize_conservative_missing"))
        if conservative and missing_oversize:
            oversize_reasons = oversize_reasons + [
                f"缺少 {'、'.join(missing_oversize)}，按保守口径暂判为超过一定规模"
                "（部文附件二阈值待补全后复核）"
            ]
        return {
            "is_hazardous": True,
            "is_oversize": bool(oversize_reasons),
            "hazard_reasons": ["该子类工程本身即属危大（部文附件一）"],
            "oversize_reasons": oversize_reasons,
            "missing_params": missing_oversize if conservative else [],
        }

    # 非参数型子类（拆除/暗挖/四新/附着式/悬挑/门型等）：出现即危大
    if rule is None or not rule.get("hazard_when"):
        if threshold_key is None:
            return {"is_hazardous": True, "is_oversize": True,
                    "hazard_reasons": ["非参数型危大工程（出现即危大）"],
                    "oversize_reasons": ["非参数型危大工程（同属超规模）"],
                    "missing_params": []}
        # 有阈值键但 hazard_when 为空（附着式等本身即危大）
        return {"is_hazardous": True, "is_oversize": True,
                "hazard_reasons": ["该类危大工程本身即属危大（部文附件一）"],
                "oversize_reasons": ["该类危大工程本身即属危大"],
                "missing_params": []}

    missing: list[str] = [p for p in rule["params"] if p not in params]
    hazard_reasons: list[str] = []
    oversize_reasons: list[str] = []

    def _check(conds: list[tuple]) -> list[str]:
        reasons: list[str] = []
        for pname, op, val in conds:
            if pname not in params:
                continue
            try:
                actual = float(params[pname])
            except (TypeError, ValueError):
                continue
            if _OPS[op](actual, float(val)):
                reasons.append(f"{pname}={actual} 满足 {op}{val}")
        return reasons

    hazard_reasons = _check(rule["hazard_when"])
    oversize_reasons = _check(rule["oversize_when"])

    # 无参数可判定 → 保守视为危大（避免漏判），但 missing 提示需补全
    is_hazardous = bool(hazard_reasons) or bool(missing)
    is_oversize = bool(oversize_reasons)
    # 若 hazard_when 完全无命中且参数齐全，则非危大
    if not hazard_reasons and not missing:
        is_hazardous = False

    return {
        "is_hazardous": is_hazardous,
        "is_oversize": is_oversize,
        "hazard_reasons": hazard_reasons,
        "oversize_reasons": oversize_reasons,
        "missing_params": missing,
    }


# =========================================================================
# 六、分类结果聚合
# =========================================================================
@dataclass
class SchemeClassification:
    """方案分类结果（可序列化为 JSON 落库 schemes.scheme_classification_json）。"""
    scheme_name: str = ""
    category_ids: list[str] = field(default_factory=list)
    category_names: list[str] = field(default_factory=list)
    sub_ids: list[str] = field(default_factory=list)
    sub_names: list[str] = field(default_factory=list)
    is_hazardous: bool = False
    is_oversize: bool = False
    hazards: list[dict] = field(default_factory=list)
    standards_keys: list[str] = field(default_factory=list)
    source_text: str = ""

    def to_dict(self) -> dict:
        return {
            "scheme_name": self.scheme_name,
            "category_ids": self.category_ids,
            "category_names": self.category_names,
            "sub_ids": self.sub_ids,
            "sub_names": self.sub_names,
            "is_hazardous": self.is_hazardous,
            "is_oversize": self.is_oversize,
            "hazards": self.hazards,
            "standards_keys": self.standards_keys,
            "source_text": self.source_text,
        }


def classify_scheme(scheme_name: str, params: Optional[dict] = None,
                    extra_text: str = "") -> SchemeClassification:
    """综合「名称关键词 + 阈值参数」给出方案分类结果。

    Args:
        scheme_name: 方案名称（必填）。
        params: 阈值判定参数（开挖深度/支撑高度/起重量等），可选。
        extra_text: 补充匹配文本（如资料片段），提升子类命中率。

    Returns:
        SchemeClassification 聚合结果。
    """
    params = params or {}
    hits = classify_scheme_name(scheme_name)
    if extra_text:
        hits.extend(match_category_keywords(extra_text))
    # 去重（按 sub_id）
    seen_sub: set[str] = set()
    uniq: list[dict] = []
    for h in hits:
        if h["sub_id"] not in seen_sub:
            seen_sub.add(h["sub_id"])
            uniq.append(h)

    cat_ids: list[str] = []
    cat_names: list[str] = []
    sub_ids: list[str] = []
    sub_names: list[str] = []
    standards_keys: set[str] = set()
    hazards: list[dict] = []
    any_hazard = False
    any_oversize = False

    for h in uniq:
        if h["category_id"] not in cat_ids:
            cat_ids.append(h["category_id"])
            cat_names.append(h["category_name"])
        sub_ids.append(h["sub_id"])
        sub_names.append(h["sub_name"])
        for k in CATEGORY_STANDARDS_KEYS.get(h["category_id"], []):
            standards_keys.add(k)
        # ✅ P0 修复（2026-10-02 · 第二十三轮）：阈值键必须**解析**后再判定。
        #    子类表声明了阈值复用（sc_cuplock/sc_disc → sc_ground、
        #    ho_tower_crane/ho_construction_hoist → ho_crane、fw_disc → fw_support），
        #    但此处一直直接传 sub_id：HAZARD_THRESHOLDS 里没有这些复用子类的键，
        #    evaluate_hazard_level 便落进「非参数型」分支 → **无条件判超规模**。
        #    后果（实测）：30m 碗扣式/盘扣式脚手架（<50m 不需专家论证）被误判
        #    超规模；50kN 塔机（<300kN）被误判超规模。
        #    数据声明了复用却没人在判定侧读它 —— 典型「判据分叉」。
        lvl = evaluate_hazard_level(resolve_threshold_key(h["sub_id"]), params)
        any_hazard = any_hazard or lvl["is_hazardous"]
        any_oversize = any_oversize or lvl["is_oversize"]
        hazards.append({
            "category_id": h["category_id"],
            "category_name": h["category_name"],
            "sub_id": h["sub_id"],
            "sub_name": h["sub_name"],
            **lvl,
        })

    return SchemeClassification(
        scheme_name=scheme_name,
        category_ids=cat_ids,
        category_names=cat_names,
        sub_ids=sub_ids,
        sub_names=sub_names,
        is_hazardous=any_hazard,
        is_oversize=any_oversize,
        hazards=hazards,
        standards_keys=sorted(standards_keys),
        source_text=(scheme_name or "") + (" " + extra_text if extra_text else ""),
    )


# =========================================================================
# 六-A、分类结论 → 提取提示词（供提取循环注入，提升针对性）
# =========================================================================
def format_classification_hint(cls: "SchemeClassification") -> str:
    """把分类结果格式化为一段给提取 AI 的「重点参考」提示。

    仅作引导性约束（告诉模型本方案属于哪类危大工程、重点抽哪些章节字段），
    不改变既有 18 项提取字段定义；cls 为空/无类别时返回空串（调用方据此不注入）。
    """
    if not cls or not cls.category_ids:
        return ""
    lines = ["【本方案危大工程分类结论（提取重点参考）】"]
    lines.append("识别大类：" + "、".join(cls.category_names))
    if cls.sub_names:
        lines.append("识别子类：" + "、".join(cls.sub_names))
    level: list[str] = []
    if cls.is_hazardous:
        level.append("危大工程")
    if cls.is_oversize:
        level.append("超过一定规模的危大工程")
    lines.append("危大级别：" + ("、".join(level) if level
                                  else "按名称特征未识别为危大工程（仍请完整抽取）"))
    if cls.standards_keys:
        lines.append("建议重点匹配编制规范类别：" + "、".join(cls.standards_keys))
    lines.append(
        "提取要求：请按上述危大工程类别，重点、完整地抽取九大章节对应字段——"
        "工程概况、编制依据、施工计划、施工工艺技术、施工安全保证措施、"
        "施工管理及作业人员配备和分工、验收要求、应急处置措施、计算书及相关施工图纸；"
        "不漏项、不错位、不臆造。")
    return "\n".join(lines)


def build_classification_hint(scheme_name: str,
                             params: Optional[dict] = None,
                             extra_text: str = "") -> str:
    """便捷封装：方案名称(+可选参数/文本) → 分类提示串。"""
    if not (scheme_name or extra_text):
        return ""
    return format_classification_hint(
        classify_scheme(scheme_name, params or {}, extra_text))


# =========================================================================
# 六-B、章节标题 → 九大章节 key 匹配（供正文生成按章精准注入提取成果）
# =========================================================================
# 章节标题含下列关键词之一即判定归属该九大章节（key 与 NINE_CHAPTERS[].key 严格一致）。
CHAPTER_KEYWORDS: dict[str, list[str]] = {
    "overview": ["工程概况"],
    "basis": ["编制依据"],
    "plan": ["施工计划"],
    "technique": ["施工工艺", "工艺技术"],
    "safety": ["安全保证", "安全保障", "施工安全", "安全措施"],
    "personnel": ["施工管理", "人员配备", "作业人员", "管理人员"],
    "acceptance": ["验收"],
    "emergency": ["应急", "处置措施"],
    "calc_drawings": ["计算书", "施工图纸", "图纸"],
}


def match_chapter_by_title(title: str) -> Optional[str]:
    """从章节标题反查九大章节 key（overview/compilationBasis/...）。无命中返回 None。

    采用「关键词包含」而非精确相等，兼容「1 工程概况及施工条件」这类带编号/后缀的标题。
    """
    t = (title or "").strip()
    if not t:
        return None
    for key, kws in CHAPTER_KEYWORDS.items():
        if any(kw in t for kw in kws):
            return key
    return None


def top_ancestor(section_id: str,
                 nodes_map: dict,
                 by_parent: Optional[dict] = None) -> Optional[dict]:
    """沿 parent_id 向上走到一级章节（顶层祖先），返回其节点 dict；传入非法 id 返回 None。

    nodes_map: section_id → section 节点（建议预构建，避免逐章重建）。
    """
    node = nodes_map.get(section_id)
    if not node:
        return None
    seen: set[str] = set()
    cur = node
    while cur.get("parent_id") and cur["parent_id"] in nodes_map and cur["id"] not in seen:
        seen.add(cur["id"])
        cur = nodes_map[cur["parent_id"]]
    return cur


def build_chapter_extraction_text(extraction_items: list[dict],
                                  chapter_key: str,
                                  max_chars: int = 2000) -> str:
    """把某九大章节对应的提取项拼成「本章专项提取成果」文本块（纯函数，不查库）。

    extraction_items: 形如 [{"label":..., "content":...}, ...]（来自 bid_analysis_items）。
    返回前按 max_chars 截断（0 表示不限）。
    """
    parts: list[str] = []
    for e in extraction_items or []:
        label = e.get("label") or e.get("item_id") or ""
        content = e.get("content") or ""
        if content:
            parts.append(f"### {label}\n{content}")
    text = "\n\n".join(parts)
    return text[:max_chars] if max_chars else text


# =========================================================================
# 七、九大章节字段映射与完整性校验
# =========================================================================
def required_fields_for_chapter(chapter_key: str,
                                category_id: Optional[str] = None) -> list[str]:
    """返回某章节在给定危大类别下的必填字段（通用 + 类别追加）。"""
    for ch in NINE_CHAPTERS:
        if ch["key"] == chapter_key:
            fields = list(ch["base_fields"])
            if category_id and category_id in ch.get("category_fields", {}):
                fields = fields + list(ch["category_fields"][category_id])
            return fields
    return []


def _norm_text(s: str) -> str:
    """完整性判定的轻量归一（去空白与常见标点，小写）。"""
    return re.sub(r"[\s，,。；;、:：()（）【】\[\]]+", "", (s or "").lower())


def map_extraction_to_chapters(extraction: dict) -> dict:
    """把提取结果（item_id → 内容文本）映射到九大章节。

    Args:
        extraction: {item_id: content_str}（content 已是落库文本/JSON 字符串）。

    Returns:
        {chapter_key: {"chapter","title","source_items","filled":bool,
                       "missing_source_items":[...]}}
        仅判断来源提取项是否存在且非空（具体字段级校验见 validate_chapter_fields）。
    """
    out: dict = {}
    for ch in NINE_CHAPTERS:
        srcs = ch["source_items"]
        missing = [s for s in srcs if not (extraction.get(s) or "").strip()]
        out[ch["key"]] = {
            "chapter": ch["chapter"],
            "title": ch["title"],
            "source_items": srcs,
            "filled": not missing,
            "missing_source_items": missing,
        }
    return out


def validate_chapter_fields(extraction: dict,
                           category_id: Optional[str] = None) -> dict:
    """九大章节字段完整性校验（差集分析）。

    判定策略（可解释、可测试）：
      - 对每个章节，合并其来源提取项内容，按「必填字段名是否出现在内容中」判定覆盖；
      - 未覆盖的字段列入 missing_fields，并标出该章节 completeness（0~1）；
      - 章节级 filled 以「来源提取项非空」为准，字段级以关键词命中为准，两者互补。

    Args:
        extraction: {item_id: content_str}
        category_id: 危大类别（用于追加类别专属必填字段）

    Returns:
        {"chapters": {key: {...}}, "missing_chapters": [...],
         "total_required": int, "covered": int, "completeness": float}
    """
    result: dict = {"chapters": {}, "missing_chapters": []}
    total_required = 0
    covered = 0

    for ch in NINE_CHAPTERS:
        fields = required_fields_for_chapter(ch["key"], category_id)
        total_required += len(fields)
        # 合并来源内容
        blob = _norm_text(" ".join(
            (extraction.get(s) or "") for s in ch["source_items"]))
        missing_fields: list[str] = []
        for f in fields:
            # 字段名含 "/"（如「基坑周长/面积/深度」）表示「或」关系：
            # 任一片段在内容中出现即视为该字段已覆盖（保留更细的章节覆盖率信号）。
            # 片段与正文均走同一归一（小写 + 去标点/空白），避免标准编号大小写
            # （JGJ120 vs jgj120）导致的误判（2026-09-24 修复）。
            segments = [_norm_text(s) for s in re.split(r"[/、]", f) if s]
            if any(seg and seg in blob for seg in segments):
                covered += 1
            else:
                missing_fields.append(f)
        chapter_filled = not any(
            not (extraction.get(s) or "").strip() for s in ch["source_items"])
        if not chapter_filled:
            result["missing_chapters"].append(ch["key"])
        result["chapters"][ch["key"]] = {
            "chapter": ch["chapter"],
            "title": ch["title"],
            "required_fields": fields,
            "missing_fields": missing_fields,
            "chapter_filled": chapter_filled,
            "field_coverage": round(1 - len(missing_fields) / len(fields), 3)
            if fields else 1.0,
        }

    result["total_required"] = total_required
    result["covered"] = covered
    result["completeness"] = round(covered / total_required, 3) if total_required else 1.0
    return result
