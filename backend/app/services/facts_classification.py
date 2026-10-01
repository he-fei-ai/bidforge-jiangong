"""全局事实 · 九大章节分类体系与多维标注（纯函数、零 AI、零 DB 依赖）

法规依据
--------
- 住房城乡建设部令第37号《危险性较大的分部分项工程安全管理规定》
- 建办质〔2018〕31号《关于实施〈危险性较大的分部分项工程安全管理规定〉有关问题的通知》
  （专项施工方案的主要内容 = 九大章节：工程概况 / 编制依据 / 施工计划 / 施工工艺技术 /
   施工安全保证措施 / 施工管理及作业人员配备和分工 / 验收要求 / 应急处置措施 /
   计算书及相关施工图纸）
- 建办质〔2021〕48号《危险性较大的分部分项工程专项施工方案编制指南》
- 各省市危大工程安全管理实施细则（阈值以部文为基准，地方从严时以地方为准）

设计要点
--------
1. **不重复维护**：九大章节定义（NINE_CHAPTERS）、六大类危大工程分类、阈值判定、
   编制规范匹配全部取自 ``scheme_classification``，本模块只做「全局事实侧」的映射
   与派生，两侧口径天然一致。
2. **正交叠加、不改既有 22 类 category**：前端下拉、落库分组排序、
   ``apply_category_auto_classify`` 均依赖既有 22 类，故在其之上叠加 4 个正交维度：

   - ``chapter``     九大章节归属（overview / basis / plan / technique / safety /
                     personnel / acceptance / emergency / calc_drawings，空串=未分类）
   - ``fact_attr``   事实属性（quantitative 定量 / qualitative 定性 /
                     relation 关系 / norm 规范）
   - ``source_kind`` 数据来源（bid_doc 项目文件解析 / drawing 施工图设计文件 /
                     survey 勘察报告 / overall_plan 施工组织设计 / manual 用户补充录入）
   - ``is_shared``   跨章节共性事实（多个章节都需要使用，标记后避免重复提取）

3. **全部派生均为确定性规则**（无 AI、无 DB 调用），任何写路径与读路径都可安全调用；
   历史数据在首次「提取/编辑」时补列，读路径同时做了惰性派生兜底（旧行不丢事实）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

from app.services import scheme_classification as sc

# =========================================================================
# 一、九大章节元数据（唯一口径取自 scheme_classification.NINE_CHAPTERS）
# =========================================================================

#: 九大章节顺序（键列表），供稳定排序与前端渲染使用
CHAPTER_ORDER: list[str] = [ch["key"] for ch in sc.NINE_CHAPTERS]

#: chapter_key → 中文标题
CHAPTER_TITLES: dict[str, str] = {ch["key"]: ch["title"] for ch in sc.NINE_CHAPTERS}

#: chapter_key → 章节序号（1-9）
CHAPTER_NUMBERS: dict[str, int] = {ch["key"]: int(ch["chapter"]) for ch in sc.NINE_CHAPTERS}

#: 章节标题 → chapter_key 反查（正文生成时由章节标题反推章节码）
TITLE_TO_CHAPTER: dict[str, str] = {ch["title"]: ch["key"] for ch in sc.NINE_CHAPTERS}

# =========================================================================
# 二、既有 22 类 category / fact_type → 九大章节 映射
# =========================================================================
# ✅ 单一事实源：前端 CATEGORY_TITLES 的键即 category 值域；
#    本表是「九大章节分类」的权威映射，未列出的键一律回退为空串（未分类）。
#    注：一个 category 只能有一个「主归属」章节；跨章节复用由 SHARED_FACT_RULES 表达。
CATEGORY_TO_CHAPTER: dict[str, str] = {
    # 一、工程概况
    "basic": "overview",
    "tech_param": "overview",   # 基坑深度/支护形式/地下水位/地质参数等专项工程特征
    "scale": "overview",        # 工程规模与地质参数
    # 二、编制依据
    "basis": "basis",
    # 三、施工计划（进度 / 材料计划 / 设备计划 / 劳动力配置）
    "schedule": "plan",
    "labor": "plan",
    "machinery": "plan",
    "equipment": "plan",
    "commitment": "plan",
    # 四、施工工艺技术
    "material_mgmt": "technique",
    "deployment": "technique",
    "process": "technique",
    "execution": "technique",
    "temporary": "technique",
    # 五、施工安全保证措施（技术措施 / 文明施工 / 环境保护 / 监测监控 / 风险分级）
    "safety_critical": "safety",
    "risk": "safety",
    "monitoring": "safety",
    "environment": "safety",
    # 六、施工管理及作业人员配备和分工
    "personnel": "personnel",
    # 七、验收要求
    "acceptance": "acceptance",
    "quality": "acceptance",
    # 八、应急处置措施
    "emergency": "emergency",
    # 九、计算书及相关图纸（无既有 category 归属，仅靠文本规则命中，见 CHAPTER_TEXT_RULES）
    "other": "",
}

#: fact_type（AI 提示词里的细粒度枚举）→ 九大章节。
#: fact_type 比 category 更细，优先级高于 category：category 为安全关键项被强制改写
#: 成 safety_critical 时，原语义（如 tech_param=基坑深度）由 fact_type 保留。
FACT_TYPE_TO_CHAPTER: dict[str, str] = {
    "basic": "overview",
    "basis": "basis",
    "personnel": "personnel",
    "labor": "plan",
    "schedule": "plan",
    "machinery": "plan",
    "material": "technique",
    "construction_practice": "technique",
    "deployment": "technique",
    "process_flow": "technique",
    "tech_param": "overview",
    "design_param": "technique",
    "safety": "safety",
    "risk": "safety",
    "monitoring": "safety",
    "emergency": "emergency",
    "acceptance": "acceptance",
    "temporary": "technique",
    "environment": "safety",
    "quality": "acceptance",
    "other": "",
}


# =========================================================================
# 三、文本规则 → 九大章节（高优先级，用于补全 category 无法表达的事实）
# =========================================================================
# 顺序即优先级：靠前的规则先命中。特别地，「计算书及相关图纸」在既有 22 类
# category 中**没有落点**（这是分类体系的历史缺口），完全依赖本表命中。
#: (关键词元组, chapter_key)
CHAPTER_TEXT_RULES: list[tuple[tuple[str, ...], str]] = [
    # 九、计算书及相关施工图纸（最专指，必须最先判）
    (("计算书", "验算", "内力计算", "稳定性计算", "承载力计算", "受力计算",
      "图纸清单", "图纸目录", "图纸编号", "平面布置图", "剖面图", "大样图",
      "节点图", "施工图编号"), "calc_drawings"),
    # 五、施工安全保证措施（监测监控 / 风险 / 文明施工与环保 / 季节性施工）
    (("监测", "报警值", "控制值", "预警值", "巡视检查", "沉降观测", "变形观测",
      "安全组织机构", "安全措施", "防护", "文明施工", "环境保护", "扬尘", "噪声",
      "绿色施工", "季节性施工", "危险源", "危大", "风险等级", "风险辨识",
      "风险管控"), "safety"),
    # 八、应急处置措施
    (("应急处置", "应急预案", "应急救援", "应急物资", "应急电话", "应急组织",
      "应急演练", "演练", "抢险", "事故类型", "救援", "急救"), "emergency"),
    # 七、验收要求
    (("验收", "检验批", "隐蔽"), "acceptance"),
    # 二、编制依据
    (("编制依据", "依据规范", "执行标准", "标准规范", "法律法规", "设计文件",
      "勘察报告", "合同编号", "施工组织设计编号"), "basis"),
    # 六、施工管理及作业人员配备和分工
    (("名单", "职责", "组织机构", "安全员", "质量员", "质检员", "特种作业",
      "岗位", "管理组织"), "personnel"),
    # 三、施工计划（进度 / 材料计划 / 设备计划 / 劳动力）
    (("劳动力", "工种", "用工计划", "高峰人数", "工期", "进度", "里程碑",
      "节点", "材料计划", "材料需求", "材料清单", "进场时间", "设备计划",
      "设备配置清单", "施工准备"), "plan"),
    # 四、施工工艺技术
    (("施工流程", "工艺流程", "工序", "施工方法", "施工步骤", "工程做法",
      "技术措施", "临时用电", "临时用水", "临时道路", "临时设施", "施工段划分",
      "流水段"), "technique"),
    # 一、工程概况（专项工程特征 / 地质水文 / 周边环境 / 气候 / 参建单位）
    # ✅ 修复（2026-09-30 · 危大判定参数的九大章节归属缺失）：原表只列了
    #    「基坑周长 / 开挖深度 / 搭设高度 / 起升高度 / 起重量」，却漏掉了
    #    ``DANGER_PARAM_RULES``（危大阈值参数的唯一事实源）与
    #    ``NINE_CHAPTERS[0].category_fields``（工程概况必填字段）里
    #    **实际使用的规范名**：基坑深度 / 支撑高度 / 跨度 / 荷载 等 13 个。
    #    后果：这些正是危大判定的核心参数（用户亦点名要求），却全部落到
    #    空串 =「未分类」——在九大章节视图里不显示、不计入
    #    ``chapter_field_completeness`` 的任一章覆盖率，用户看到
    #    「工程概况 0 条事实」却不知数据其实已提取。
    #    本次按 NINE_CHAPTERS[0].category_fields 的口径补齐（基坑/模板支撑/
    #    起重/脚手架/其他五类的专项工程特征均属第一章工程概况）。
    (("地质", "水文", "地层", "地下水", "地形", "周边环境", "周边管线", "气候",
      "支护形式", "支护安全等级", "设计使用年限", "基坑周长", "开挖深度",
      "基坑深度", "坑深", "搭设高度", "架体形式", "支撑高度", "支撑架高度",
      "架体高度", "跨度", "跨距", "施工总荷载", "总荷载", "集中线荷载",
      "线荷载", "起升高度", "起重量", "起吊重量", "单件起吊重量",
      "边坡高度", "边坡开挖高度", "安装高度", "承载力", "立杆间距",
      "立杆步距", "连墙件", "降水方式", "起重机高度", "起重设备高度",
      "结构形式", "建设规模",
      "工程名称", "项目名称", "工程地点", "项目地点", "参建", "建设单位",
      "设计单位", "监理单位", "施工单位", "施工要求"), "overview"),
]


#: 英文归一化键（fact_key）→ 九大章节。
#: ✅ 修复（2026-09-30 第十三轮 · 英文键从未参与章节判定）：
#: ``classify_fact_dimensions`` 早已把 ``fact_key`` 作为入参接收（并用于
#: ``shared_chapters_for``），但 ``classify_chapter_from_text`` 的签名里
#: **没有这个参数** —— 于是 ``foundation_depth`` / ``span`` / ``total_load``
#: 等英文归一化键只对「共性事实」判定生效、对「章节归属」完全无效。
#: 真实后果：模型若把事实名写成英文（或人工录入用英文键），该事实在九大章节
#: 视图里落空串 =「未分类」，不显示、不计入 ``chapter_field_completeness``。
#: 本表从 ``DANGER_PARAM_RULES`` 派生（危大参数一律属第一章「工程概况」的
#: 专项工程特征），与中文规则共用同一结论，不会出现两套口径。
FACT_KEY_TO_CHAPTER: dict[str, str] = {
    "foundation_depth": "overview",
    "excavation_depth": "overview",
    "slope_height": "overview",
    "install_height": "overview",
    "support_type": "overview",
    "height": "overview",
    "span": "overview",
    "total_load": "overview",
    "line_load": "overview",
    "single_weight": "overview",
    "max_lift_weight": "overview",
    "crane_capacity": "overview",
    "crane_height": "overview",
    "tower_crane_model": "overview",
    "excavator_model": "overview",
    "scaffold_type": "overview",
    "formwork_type": "overview",
    "water_table": "overview",
    "bearing_capacity": "overview",
    "slope_ratio": "overview",
    "structure_type": "overview",
    "building_area": "overview",
    "building_height": "overview",
    "site_area": "overview",
}


def classify_chapter_from_text(name: str, value: str = "",
                               category: str = "", fact_type: str = "",
                               fact_key: str = "") -> str:
    """判定单条事实的九大章节归属（确定性规则，返回 chapter_key，空串 = 未分类）。

    判定优先级（专指 → 泛指）：
    1. 文本关键词规则 —— 最专指。建办质〔2018〕31号 把「基坑开挖深度」列在
       第一章工程概况的专项工程特征下，却把「混凝土强度等级」列在第四章工艺
       技术参数下；这类区分只存在于事实文本本身，粗粒度的 fact_type
       （design_param/tech_param）无法表达，故文本规则优先。
    2. fact_key 英文归一化键 —— 事实名写英文时唯一可用的专指信号（§FACT_KEY_TO_CHAPTER）。
    3. fact_type —— AI 已给出的细粒度类型，文本规则未命中时的次优信号。
    4. category —— 兜底映射（category 为安全关键项被强制改写时仍可用）。

    纯函数：输入仅依赖事实自身的 name/value/category/fact_type/fact_key，可在任何
    读路径惰性调用（历史行无 chapter 列值时也不会丢事实）。

    ⚠️ ``fact_key`` 是**新增的可选参数**（默认空串），既有 4 参调用点全部
    逐字保持原行为——中文事实名的判定链完全不受影响。
    """
    text = f"{name or ''} {value or ''}"
    if text.strip():
        for keywords, chapter in CHAPTER_TEXT_RULES:
            for kw in keywords:
                if kw and kw in text:
                    return chapter
    fk = (fact_key or "").strip()
    if fk and fk in FACT_KEY_TO_CHAPTER:
        return FACT_KEY_TO_CHAPTER[fk]
    ft = (fact_type or "").strip()
    if ft in FACT_TYPE_TO_CHAPTER:
        mapped = FACT_TYPE_TO_CHAPTER[ft]
        if mapped:
            return mapped
    cat = (category or "").strip()
    return CATEGORY_TO_CHAPTER.get(cat, "")


# =========================================================================
# 四、事实属性判定（定量 / 定性 / 关系 / 规范）
# =========================================================================
#: 属性枚举 → 中文标题
FACT_ATTR_TITLES: dict[str, str] = {
    "quantitative": "定量事实",
    "qualitative": "定性事实",
    "relation": "关系事实",
    "norm": "规范事实",
}

#: 规范/标准编号形态（GB 50011 / JGJ 120-2012 / GB 55003-2021 等）
#: ✅ 故意宽松匹配 —— 事实值常写作 "GB50011-2010"、"JGJ120" 混排，
#: 过严的正则会漏判，把规范引用错当成定性事实。
_STD_CODE_RE = re.compile(
    r"(?i)(?:GB|JGJ|JG|CJJ|GBJ|CECS|DL|DB|DBJ|HG|SH|SY)[\s./-]*[0-9]{2,5}"
    r"(?:[\s./-]*[12][0-9]{2})?"
)
#: 条文/表格编号形态（第 3.1.2 条 / 表 4.1.2 / 图 2-1 / 3.1.2.1）
#: ✅ BUG 修复：旧实现含 `\d+\.\d+` 分支，会把「4.2」这类**测量值**误判为
#: 条文编号（4.2m 的开挖深度被判成 norm 规范事实，定量信号丢失）。
#: 现要求「第…条」/「表·图·附录 编号」前缀，或**至少 3 段**的点分编号，
#: 从而只认条文编号、不认普通小数。
_STD_CLAUSE_RE = re.compile(
    r"(?:第\s*[1-9]\d?(?:\.\s*[1-9]\d?){1,3}\s*条"
    r"|(?:表|图|附录|条款)\s*[1-9]\d?(?:\.\s*[1-9]\d?){0,3}"
    r"|\d+\.\d+\.\d+(?:\.\d+)*)"
)
#: 数值 + 计量单位（长单位在前，避免 "m" 吞掉 "mm"、"m2" 吞掉 "m3"）
_NUM_UNIT_RE = re.compile(
    r"[0-9]+(?:\.[0-9]+)?\s*"
    r"(?:m2|m3|㎡|m²|m³|mm|cm|kN/m2|kN/m|kN|kVa|kW|kVA|dB|MPa|Pa|℃|°C"
    r"|毫米|厘米|米|m|千牛|吨|t|kg|千克|天|日历天|日|月|年|个|台|套|人|名"
    r"|处|道|遍|层|级|次|小时|h|V|A|Ω)",
    re.IGNORECASE,
)
#: 纯数值（无数值单位时也算定量，如「5」「3.5」「1:50」）
_BARE_NUM_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:[/:][0-9]+)*")
#: 关系类事实关键词（邻近关系 / 隶属关系 / 工序依赖关系）
_RELATION_KEYWORDS = (
    "关系", "邻近", "周边", "相邻", "上下游", "工序", "顺序", "搭接", "依赖",
    "隶属", "相对位置", "距离", "间距", "方位", "位于",
)


def classify_fact_attr(name: str, value: str) -> str:
    """判定事实属性（quantitative 定量 / qualitative 定性 / relation 关系 / norm 规范）。

    判定优先级：
    1. ``norm``        —— 值含标准/规范编号或条文编号（如 GB 55003-2021、第 3.1.2 条）；
    2. ``relation``    —— 名称含关系类关键词（邻近关系 / 工序依赖 / 隶属关系等）；
    3. ``quantitative``—— 值含「数值 + 单位」或纯数值；
    4. ``qualitative`` —— 其余描述性事实（地质描述、环境描述、方法描述）。

    注：关系类关键词优先于定量判定，因为「与基坑距离 5m」这类事实的核心语义
    是关系，数值只是关系的量度。

    ⚠️ **刻意不接收 ``category``**（2026-09-29 清理死参数）：事实属性只由事实
    自身的 name/value 文本决定，与 22 类 ``category`` **正交**。旧签名曾声明
    ``category="" `` 形参但函数体从未读取 —— 那是死参数，极易被误读成
    「fact_attr 依赖分类结果」，从而把分类口径错误地耦合进属性判定（而属性
    判定是纯文本正则，不该被上游的粗分类影响）。护栏：
    ``tests/test_facts_content_fixes_20260930.py`` 用 ``inspect.signature``
    锁死本函数只接受 ``(name, value)``。
    """
    text = f"{name or ''} {value or ''}"
    low = text.lower()
    if _STD_CODE_RE.search(low) or _STD_CLAUSE_RE.search(low):
        return "norm"
    for kw in _RELATION_KEYWORDS:
        if kw in text:
            return "relation"
    if _NUM_UNIT_RE.search(low) or _BARE_NUM_RE.search(low):
        return "quantitative"
    return "qualitative"


# =========================================================================
# 五、数据来源判定（项目文件解析 / 施工图 / 勘察报告 / 施工组织设计 / 用户补充）
# =========================================================================
#: 来源类别 → 中文标题
SOURCE_KIND_TITLES: dict[str, str] = {
    "bid_doc": "项目文件解析提取",
    "drawing": "施工图设计文件提取",
    "survey": "勘察报告提取",
    "overall_plan": "施工组织设计提取",
    "manual": "用户补充录入",
}

#: 来源类别判定规则：(来源文本关键词元组, source_kind)
#: 顺序即优先级（勘察 > 施工图 > 施工组织设计 > 手工 > 项目文件）。
SOURCE_KIND_RULES: list[tuple[tuple[str, ...], str]] = [
    (("勘察", "地勘", "地质报告", "土工试验"), "survey"),
    (("施工图", "图纸", "设计文件", "蓝图"), "drawing"),
    (("施工组织设计", "总体施工组织", "实施性施工组织", "施工组织总设计"), "overall_plan"),
    (("手动", "手工", "录入", "补充", "manual", "hand"), "manual"),
    (("招标", "标书", "投标", "答疑", "补充通知"), "bid_doc"),
]

#: 无来源标记时的默认来源（提取管线的主数据源即项目文件解析）
DEFAULT_SOURCE_KIND = "bid_doc"


def _as_text(v: Any) -> str:
    """把事实值/来源统一转为可匹配文本（序列转顿号连接，dict 走 JSON 摘要）。"""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "、".join(str(x) for x in v)
    return str(v)


def classify_source_kind(source: str = "", source_ref: str = "") -> str:
    """判定事实的数据来源类别（确定性规则，返回 source_kind）。

    接受两种来源形态：
    - ``source``：简单文件名/章节名；
    - ``source_ref``：可能是 JSON（list/dict/str）形态的引用，需先解析再匹配。
    """
    blob = _as_text(source)
    ref = source_ref
    if isinstance(ref, (dict, list, tuple)):
        blob += " " + _as_text(ref)
    elif isinstance(ref, str) and ref:
        blob += " " + ref
        try:
            blob += " " + _as_text(json.loads(ref))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass  # 非 JSON 字符串按原文匹配
    for keywords, kind in SOURCE_KIND_RULES:
        for kw in keywords:
            if kw and kw in blob:
                return kind
    return DEFAULT_SOURCE_KIND


# =========================================================================
# 六、跨章节共性事实（避免重复提取、支持多章节复用）
# =========================================================================
# 建办质〔2018〕31号 的九大章节并非互斥：工程名称、参建单位、工期、材料规格、
# 人员配置、技术参数等事实在多个章节都需要引用。本表记录「哪些事实应标记为
# 共性事实，以及它被哪些章节需要」。
#
# 用途：
# 1. 提取阶段标记 ``is_shared=1``，前端可聚合出「共性事实」视图；
# 2. 正文生成时，共性事实可被多章引用而不必为每章复制一条记录（避免重复提取）；
# 3. 字段完整性校验时，共性事实可计入其全部归属章节的覆盖率。
#: (关键词元组, 需复用的章节元组)
SHARED_FACT_RULES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    # 工程名称与地点：概况 / 依据 / 应急 / 验收 均需要
    (("工程名称", "项目名称", "工程地点", "项目地点"),
     ("overview", "basis", "emergency", "acceptance")),
    # 参建单位信息：概况 / 施工管理 / 验收 / 应急 均需要
    (("参建", "建设单位", "设计单位", "监理单位", "施工单位", "监测单位",
      "建设规模", "结构形式"),
     ("overview", "personnel", "acceptance", "emergency")),
    # 施工工期：施工计划 / 应急 / 季节性施工 均需要
    (("工期", "进度", "里程碑", "节点工期"), ("plan", "emergency", "safety")),
    # 材料规格参数：工艺 / 计算书 / 验收 均需要
    (("混凝土", "钢筋", "钢材", "材料", "规格", "强度等级", "牌号", "型号"),
     ("technique", "calc_drawings", "acceptance")),
    # 人员配置信息：施工管理 / 应急 / 验收 均需要
    (("人员", "组织", "名单", "职责", "安全员", "特种作业", "岗位"),
     ("personnel", "emergency", "acceptance")),
    # 技术参数：工艺 / 计算书 / 监测监控 均需要
    (("深度", "高度", "跨度", "荷载", "参数", "起重量", "起升高度", "承载力",
      "厚度", "间距"), ("technique", "calc_drawings", "safety")),
]


def shared_chapters_for(name: str, value: str = "") -> tuple[str, ...]:
    """返回该事实应被复用的章节元组（按章节序号升序）；空元组表示非共性事实。

    返回值按九大章节序号排序（而非关键词声明顺序），保证前端展示与
    章节视图聚合时的顺序稳定一致。
    """
    text = f"{name or ''} {value or ''}"
    for keywords, chapters in SHARED_FACT_RULES:
        for kw in keywords:
            if kw and kw in text:
                return tuple(sorted(chapters, key=lambda c: _chapter_order(c)))
    return ()


def _chapter_order(key: str) -> int:
    """章节序号（未知章节排在最后）。"""
    try:
        return CHAPTER_ORDER.index(key)
    except ValueError:
        return len(CHAPTER_ORDER)


# =========================================================================
# 七、危大工程阈值判定参数抽取（事实 → HAZARD_THRESHOLDS 参数）
# =========================================================================
# HAZARD_THRESHOLDS 的判定参数键（depth / height / span / total_load / line_load /
# single_weight / crane_capacity / slope_height / install_height）与全局事实的
# 事实名并非一一对应（事实名是中文长句），本表给出「事实名/归一化键 → 参数键」
# 的映射，供 evaluate_hazard_level 使用。
#: (匹配关键词元组, 阈值参数键)
DANGER_PARAM_RULES: list[tuple[tuple[str, ...], str]] = [
    (("开挖深度", "基坑深度", "坑深", "foundation_depth", "excavation_depth"), "depth"),
    (("边坡高度", "边坡开挖高度", "slope_height"), "slope_height"),
    (("支撑高度", "搭设高度", "架体高度", "支撑架高度", "立杆步距"), "height"),
    (("安装高度", "幕墙安装高度", "install_height"), "install_height"),
    (("跨度", "跨距", "span"), "span"),
    (("施工总荷载", "总荷载", "total_load"), "total_load"),
    (("集中线荷载", "线荷载", "line_load"), "line_load"),
    (("单件起吊重量", "最大起吊重量", "起吊重量"), "single_weight"),
    (("起重量", "额定起重量", "起重设备起重量", "crane_capacity"), "crane_capacity"),
    (("起重机高度", "起重设备高度"), "crane_height"),
]

#: 长度单位 → 米 的换算系数（HAZARD_THRESHOLDS 长度类阈值全部以 m 为单位）
_UNIT_TO_METER = {"mm": 0.001, "毫米": 0.001, "cm": 0.01, "厘米": 0.01,
                  "m": 1.0, "米": 1.0}
#: 长度类参数键（其余为荷载/重量类，保持原单位 kN/m²、kN/m）
_LENGTH_PARAMS = {"depth", "height", "span", "slope_height",
                  "install_height", "crane_height"}
_NUM_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z\u4e00-\u9fff/²³]*)")


def _extract_number_with_unit(text: str):
    """从文本中提取第一个「数值 + 单位」；无法提取时返回 None。"""
    if not text:
        return None
    m = _NUM_RE.search(text)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    return num, (m.group(2) or "").strip()


def _to_meter(num: float, unit: str):
    """把长度类单位换算为米；无法识别单位时返回 None（调用方按原值处理）。"""
    low = (unit or "").lower()
    for u, ratio in _UNIT_TO_METER.items():
        if low == u.lower() or low.replace(" ", "") == u:
            return num * ratio
    return None


def extract_danger_params(facts) -> dict:
    """从全局事实中抽取危大工程阈值判定参数。

    Args:
        facts: 事实行序列，元素需含 name / value / value_unit / fact_key 等字段
            （``global_facts`` 表的 dict 行或 FactItem 的 asdict 均可）。

    Returns:
        ``{参数键: 数值}``，如 ``{"depth": 6.5, "span": 18.0}``。
        长度类参数统一换算为米；荷载类保持原单位（kN/m²、kN/m）。
        同一参数出现多个候选时取最大值（保守判定，避免低估危大级别）。
    """
    out = {}
    for f in facts or []:
        if not isinstance(f, dict):
            continue
        name = f"{_as_text(f.get('name'))} {_as_text(f.get('fact_key'))}"
        value = _as_text(f.get("value"))
        unit = _as_text(f.get("value_unit")) or ""
        matched = None
        for keywords, param in DANGER_PARAM_RULES:
            for kw in keywords:
                if kw and kw in name:
                    matched = param
                    break
            if matched:
                break
        if not matched:
            continue
        pair = _extract_number_with_unit(value) or _extract_number_with_unit(
            f"{value} {unit}")
        if not pair:
            continue
        num, got_unit = pair
        if matched in _LENGTH_PARAMS:
            meter = _to_meter(num, got_unit or unit)
            if meter is not None:
                num = meter
        prev = out.get(matched)
        if prev is None or num > prev:
            out[matched] = num
    return out


def danger_check(scheme_name: str, facts=None, extra_text: str = "") -> dict:
    """自动识别专项方案类型 + 危大工程/超过一定规模判定。

    两步判定（均可单独调用，便于测试与前端按需取用）：
    1. 方案名称关键词解析 → 六大类危大工程分类（``classify_scheme``）；
    2. 事实中抽取的定量参数 → 阈值判定（hazard / oversize 及命中原因）。

    Args:
        scheme_name: 专项方案名称（唯一权威判定依据）；
        facts: 全局事实行序列，用于抽取阈值判定参数；
        extra_text: 补充判定文本（如工程概况正文，用于名称信息不足时兜底）。

    Returns:
        ``{"classification": {...}, "threshold_params": {...}}``。
        classification 结构同 ``SchemeClassification.to_dict()``，含
        category_ids / sub_ids / is_hazardous / is_oversize / thresholds 等字段。
    """
    facts_list = [f for f in (facts or []) if isinstance(f, dict)]
    params = extract_danger_params(facts_list)
    cls = sc.classify_scheme(scheme_name, params, extra_text)
    return {"classification": cls.to_dict(), "threshold_params": params}


# =========================================================================
# 八、标注编排（供提取管线 / 读路径统一调用）
# =========================================================================

#: 新增落库列名（顺序即落库顺序，与 FactItem.to_db_row / persist INSERT 一致）
DIMENSION_COLUMNS = ("chapter", "fact_attr", "source_kind", "is_shared")


def _content_value(content) -> str:
    """从落库 content（Markdown 行）中取事实值文本，供维度派生兜底使用。"""
    text = _as_text(content)
    if ":" in text:
        return text.split(":", 1)[1].strip()
    if "：" in text:
        return text.split("：", 1)[1].strip()
    return text


def classify_fact_dimensions(name: str, value: str = "", category: str = "",
                             fact_type: str = "", fact_key: str = "",
                             source: str = "", source_ref: str = "") -> dict:
    """一次性派生全部四个分类维度（确定性、纯函数）。

    Returns:
        ``{"chapter": str, "fact_attr": str, "source_kind": str,
           "is_shared": bool, "shared_chapters": [str, ...]}``

    说明：``is_shared`` 为 True 表示该事实被多个章节复用；``shared_chapters``
    记录具体被哪些章节需要（含主归属章节去重后的完整集合，按章节序号排序）。
    """
    text_value = value if isinstance(value, str) else _as_text(value)
    chapter = classify_chapter_from_text(name, text_value, category, fact_type, fact_key)
    shared = shared_chapters_for(f"{name} {fact_key}", text_value)
    shared_set = set(shared)
    if chapter:
        shared_set.add(chapter)
    return {
        "chapter": chapter,
        "fact_attr": classify_fact_attr(name, text_value),
        "source_kind": classify_source_kind(source, source_ref),
        "is_shared": bool(shared),
        "shared_chapters": tuple(sorted(shared_set, key=_chapter_order)),
    }


def apply_fact_dimensions(items: list) -> list:
    """就地为 FactItem 列表补齐四个分类维度（已有值不覆盖，保证幂等）。

    在提取管线中紧跟 ``apply_category_auto_classify`` 之后调用；对已有值的事实
    （人工编辑 / 增量提取保留的历史行）不覆盖，避免「重新提取」冲掉人工归类。
    """
    for it in items:
        # FactItem 的归一化键字段名是 `key`（落库列才是 fact_key）
        dims = classify_fact_dimensions(
            getattr(it, "name", "") or "",
            _as_text(getattr(it, "value", "")),
            getattr(it, "category", "") or "",
            getattr(it, "fact_type", "") or "",
            getattr(it, "fact_key", "") or getattr(it, "key", "") or "",
            getattr(it, "source", "") or "",
            getattr(it, "source_ref", "") or "",
        )
        if not getattr(it, "chapter", ""):
            it.chapter = dims["chapter"]
        if not getattr(it, "fact_attr", ""):
            it.fact_attr = dims["fact_attr"]
        if not getattr(it, "source_kind", ""):
            it.source_kind = dims["source_kind"]
        if not getattr(it, "is_shared", False):
            it.is_shared = dims["is_shared"]
    return items


def dimensions_for_row(row: dict) -> dict:
    """为一条已落库事实行派生维度（读路径惰性兜底，不写库）。

    历史行无 ``chapter`` 等列值时，调用方可用本函数结果做内存派生，
    避免「历史事实因未标注而被章节过滤丢光」。已落库值优先（尊重人工归类）。
    """
    if not isinstance(row, dict):
        row = {}
    stored_chapter = _as_text(row.get("chapter"))
    dims = classify_fact_dimensions(
        _as_text(row.get("name") or row.get("title")),
        _as_text(row.get("value") or _content_value(row.get("content"))),
        _as_text(row.get("category")),
        _as_text(row.get("fact_type")),
        _as_text(row.get("fact_key")),
        _as_text(row.get("source")),
        _as_text(row.get("source_ref")),
    )
    out = dict(dims)
    out["chapter"] = stored_chapter or dims["chapter"]
    for col in ("fact_attr", "source_kind"):
        if _as_text(row.get(col)):
            out[col] = _as_text(row.get(col))
    out["is_shared"] = bool(row.get("is_shared")) or dims["is_shared"]
    return out


# =========================================================================
# 九、章节聚合与字段完整性校验（差集分析）
# =========================================================================

def chapter_of_row(row: dict) -> str:
    """取单条事实行的章节归属（优先落库列，缺失时惰性派生）。"""
    if not isinstance(row, dict):
        row = {}
    stored = _as_text(row.get("chapter"))
    if stored:
        return stored
    return classify_fact_dimensions(
        _as_text(row.get("name") or row.get("title")),
        _as_text(row.get("value") or _content_value(row.get("content"))),
        _as_text(row.get("category")),
        _as_text(row.get("fact_type")),
        _as_text(row.get("fact_key")),
    )["chapter"]


def chapter_field_completeness(rows: list) -> dict:
    """九大章节字段完整性校验（差集分析，供目录生成前预检使用）。

    对每个章节，统计其「已提取事实数」「应提取字段清单」「已覆盖字段」，
    缺失字段 = 应提取字段 - 已覆盖字段（按字段名在事实名/值中的字面命中判定）。

    Args:
        rows: 事实行序列（dict，含 name/value/category/fact_type 等）。

    Returns:
        ``{"chapters": {chapter_key: {...}}, "missing_chapters": [str],
           "total_fields": int, "covered_fields": int, "coverage": float}``
    """
    by_chapter: dict[str, list] = {k: [] for k in CHAPTER_ORDER}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        ch = chapter_of_row(r)
        if ch in by_chapter:
            by_chapter[ch].append(r)

    chapters: dict[str, dict] = {}
    missing_chapters: list[str] = []
    total_fields = 0
    covered_fields = 0

    for key in CHAPTER_ORDER:
        fields = sc.required_fields_for_chapter(key)
        text = " ".join(
            f"{_as_text(r.get('name'))} "
            f"{_as_text(r.get('value') or _content_value(r.get('content')))}"
            for r in by_chapter[key]
        )
        blob = sc._norm_text(text)
        def _field_present(field: str) -> bool:
            # 与 scheme_classification.validate_chapter_fields 保持同一归一口径：
            # 去空白/标点/大小写，并支持「A/B/C」任一命中，避免 JGJ 120、
            # 英文标点或斜杠字段造成假缺失。
            return any(sc._norm_text(seg) in blob
                       for seg in re.split(r"[/、]", field) if seg.strip())
        present = [f for f in fields if f and _field_present(f)]
        missing = [f for f in fields if not (f and _field_present(f))]
        total_fields += len(fields)
        covered_fields += len(present)
        chapters[key] = {
            "chapter": CHAPTER_NUMBERS[key],
            "title": CHAPTER_TITLES[key],
            "fact_count": len(by_chapter[key]),
            "has_facts": bool(by_chapter[key]),
            "fields": fields,
            "covered_fields": present,
            "missing_fields": missing,
            "field_coverage": round(len(present) / len(fields), 3) if fields else 1.0,
        }
        # 章节为空 或 必填字段有缺失 → 视为「章节不完整」
        if not chapters[key]["has_facts"] or missing:
            missing_chapters.append(key)

    return {
        "chapters": chapters,
        "missing_chapters": missing_chapters,
        "total_fields": total_fields,
        "covered_fields": covered_fields,
        "coverage": round(covered_fields / total_fields, 3) if total_fields else 1.0,
    }


def nine_chapter_summary(rows: list) -> dict:
    """九大章节统计与字段覆盖率（供全局事实页「章节视图」渲染）。

    Args:
        rows: 事实行序列（dict）。

    Returns:
        ``{"chapters": [{order, key, title, count, coverage, missing_fields}],
           "totals": {...}, "uncategorized": int}``
    """
    comps = chapter_field_completeness(rows)
    items: list[dict] = []
    uncategorized = 0
    rows = rows or []
    for r in rows:
        if isinstance(r, dict) and chapter_of_row(r) not in CHAPTER_ORDER:
            uncategorized += 1
    for key in CHAPTER_ORDER:
        comp = comps["chapters"][key]
        items.append({
            "order": comp["chapter"],
            "key": key,
            "title": comp["title"],
            "count": comp["fact_count"],
            "coverage": comp["field_coverage"],
            "total_fields": len(comp["fields"]),
            "covered_fields": len(comp["covered_fields"]),
            "missing_fields": comp["missing_fields"],
        })
    return {
        "chapters": items,
        "totals": {
            "facts": len(rows),
            "uncategorized": uncategorized,
            "total_fields": comps["total_fields"],
            "covered_fields": comps["covered_fields"],
            "coverage": comps["coverage"],
            "missing_chapters": comps["missing_chapters"],
        },
    }


def category_map_payload() -> dict:
    """输出分类映射表（供前端下拉 / 文档化展示，避免前端二次硬编码）。"""
    return {
        "chapters": [
            {"order": CHAPTER_NUMBERS[k], "key": k, "title": CHAPTER_TITLES[k]}
            for k in CHAPTER_ORDER
        ],
        "category_to_chapter": dict(CATEGORY_TO_CHAPTER),
        "fact_attr_titles": dict(FACT_ATTR_TITLES),
        "source_kind_titles": dict(SOURCE_KIND_TITLES),
    }