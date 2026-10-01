"""全局事实变量提取服务（增强版）

功能：
1. 分段提取 - 超长资料自动切分，分段并发调用 LLM
2. 合并去重 - 归一化 key 聚类，按来源优先级选取
3. 矛盾检测 - 同 key 多值标记冲突，支持人工裁决
4. 模拟值处理 - 强制标记 + 安全关键项禁止模拟
5. 缓存失效 - 事实变更时自动清空 export_cache

与现有 global_facts.py 路由配合使用。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from functools import lru_cache
from typing import Any

from app.services.ai.json_response import collect_json_response
from app.services.ai.prompts._registry import render
from app.services.ai.prompts.analysis import SAFETY_CRITICAL_FACT_KEYS
from app.services.ai.prompts._norm_dicts import (
    normalize_machinery_name, normalize_material_spec, normalize_unit,
)
from app.services.facts_cross_validators import run_cross_validations
# ✅ 2026-09-24：九大章节分类体系（纯函数、零 AI、零 DB 依赖，无循环引用）
from app.services.facts_classification import apply_fact_dimensions
# ✅ 2026-09-30 第十三轮：缺值模式值域的单一出口（对齐参考软件 globalFactsMode）
from app.services.facts_patches import normalize_missing_value_mode
from app.config import settings

logger = logging.getLogger("facts_extractor")

# ---------------------------------------------------------------------------
# 配置参数
# ---------------------------------------------------------------------------

CHUNK_SIZE = 8000              # 每段最大字符数
CHUNK_OVERLAP = 500            # 段间重叠字符数
MAX_SOURCE_EXCERPT = 120       # 溯源摘录最大字符（旧值 30 会把手册/条文引句截成残句且无任何提示，
                               #   导致溯源与人工复核价值归零；放宽到 120 并在截断时补省略号）
SIMULATED_RATIO_WARN = 0.30    # 模拟值占比告警阈值
SAFETY_CRITICAL_MAX_SIMULATED = 0  # 安全关键项允许的模拟值数量上限
# ✅ 完整性：单次提取允许处理的最大段数。
#    旧值 20 → 8000 字/段，即超过 16 万字的长招标文件会被静默截断、后续章节不提取。
MAX_CHUNKS = 60
# ✅ 速度/稳定性：分段提取并发上限（实际并发 = min(段数, 全局自适应并发, 上限)）。
#    注意：不再强制提高并发下限——线上日志显示 sensetime 在并发 4 时已触发
#    429（"inference exceeds tpm/rpm limit"），强行提高只会放大限流、
#    拖慢整体并造成分段失败。本地排队上限与全局硬上限统一为 5，避免一次资料
#    制造 12 个等待协程；实际网络并发仍由 provider_factory 全局信号量最终钳制。
FACTS_MAX_CONCURRENCY = 5
# ✅ 容错：单段提取失败后的重试次数与指数退避基数（应对 429 限流/瞬时网络错误）
FACTS_CHUNK_RETRIES = 2
FACTS_RETRY_BACKOFF = 5.0
# 触发 429 限流时使用更长的退避基数（等对方限流窗口恢复）
FACTS_RETRY_BACKOFF_RATE_LIMIT = 20.0
# ✅ 结构化提取请求超时（秒）：事实提取提示词长、输出结构化，lite 类模型常需 60s+。
#    线上日志显示配置 timeout=60 时大量分段以 ~62s 超时失败 → 这里显式放宽。
FACTS_REQUEST_TIMEOUT = 240

#: 固定提示词骨架的**估算**长度（字符）：system 提示词里与资料正文**无关**的部分
#: = 核心纪律 + 类型规则块 P7~P20 + JSON Schema 说明 + 归一化字典块。
#: 用于「上下文预算分段」时扣除固定开销（对齐易标 getMessagesContentLength 口径）。
#: ⚠️ 这是**估算常量**而非实时测量：system 提示词随 zone_type 变化，若改为实时
#:   render 会引入 lru_cache 依赖与额外的 IO。估算偏大是安全方向（宁可少切一段，
#:   也不可让请求超窗口）。仅在 ``settings.facts_context_budget_split=True`` 时生效。
_FIXED_PROMPT_SKELETON = "x" * 12000


def _is_structural_error(exc: Exception) -> bool:
    """判断异常是否为"结构性输出错误"（重试无收益）。

    collect_json_response 在"生成→修复×N"全部未通过 schema 校验后抛
    ``ValueError("JSON 生成/修复失败：...")``——内层已用同一份输入做过
    定向修复，外层整段重试只是重复相同请求（默认每段 1 次生成+2 次修复，
    再放大 3 倍 = 9 次 AI 调用），结构性错误不会因重试自愈。
    瞬态错误（429 限流 / 超时 / 网络 / 5xx）才值得退避重试。
    """
    if isinstance(exc, ValueError) and "JSON 生成/修复失败" in str(exc):
        return True
    msg = str(exc)
    return "未找到 JSON 结构" in msg or "输出顶层结构非预期" in msg


def _clip_excerpt(text: str, limit: int) -> str:
    """溯源引句截断：超长时截到 limit 字并补省略号，使截断对用户可见。

    ✅ 数据流审计 2026-09-23：旧实现直接 `text[:30]` 静默截断，用户无法区分
    “原文就这么短”与“被截断了”，现以尾部省略号明示截断。
    """
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class FactItem:
    """单条结构化事实

    ✅ 扩展字段（来自《全局事实变量提取功能可行性研究报告》v2.0）：
    - value_unit: 计量单位（"台"/"kW"/"天"），序列类/null 保留
    - fact_type: 细粒度类型枚举（construction_practice/deployment/process_flow/material/machinery/...）
    - evidence_kind: 证据来源（table/text/figure_ocr/header_footer）
    - page_ref: 页码（PDF/OCR 场景必填）
    - zone_type: 语义区域类型（machinery_stat/construction_practice_zone/...）
    - norm_group: 归一化字典命中组（material/machinery/process/schedule/None）
    """
    name: str                    # 事实名称（如 "项目总工期"）
    value: str | list            # 事实值（序列类为有序数组；单值为 str）
    key: str = ""                # 归一化键（如 total_duration）
    category: str = "other"      # 类别（personnel/schedule/machinery/...）
    source: str = ""             # 来源描述（文件名/章节）
    source_ref: str = ""         # 原文完整引用（source_text，用于溯源比对）
    is_simulated: bool = False   # 是否模拟生成
    confidence: float = 1.0      # 置信度 0-1
    has_conflict: bool = False   # 是否存在矛盾
    conflict_values: list = field(default_factory=list)
    # --- ✅ 以下为研究报告 v2.0 扩展字段 ---
    value_unit: str | None = None       # 计量单位（"台"/"kW"/"天"/"页"）
    fact_type: str = ""                 # 细粒度类型枚举（construction_practice/deployment/process_flow/material/machinery/basic/personnel/schedule/design_param/safety/...）
    evidence_kind: str = ""             # 证据来源（table/text/figure_ocr/header_footer）
    page_ref: int | None = None         # 页码
    zone_type: str = ""                 # 语义区域类型（分段时 zone_type 标记）
    is_safety_critical: bool = False    # 是否属于 17 类白名单（禁止模拟）
    norm_group: str | None = None        # 归一化字典命中组名（"material"/"machinery"/"process"/"schedule"/None）
    # ✅ 增量提取（2026-09-17）：该事实产自分段的 sha1 指纹。跳过已完成段时，
    #    persist_extraction 靠它识别「上次段的事实」并保留，不误删。
    chunk_hash: str = ""
    # --- ✅ 2026-09-24：九大章节分类体系四维标注（正交于既有 22 类 category）---
    # 依据建办质〔2018〕31号，专项方案分九大章节；既有 category 是「事实类型」
    # 视角，与九大章节并非一对一。以下四维由 facts_classification 确定性派生
    # （无 AI、无 DB 往返），支撑：按章节精选事实注入正文、章节视图统计、
    # 字段完整性校验、跨章节共性事实识别。
    chapter: str = ""          # 九大章节归属（overview/basis/plan/technique/safety/
                              #   personnel/acceptance/emergency/calc_drawings，空串=未分类）
    fact_attr: str = ""        # 事实属性（quantitative 定量/qualitative 定性/
                              #   relation 关系/norm 规范）
    source_kind: str = ""      # 数据来源（bid_doc/drawing/survey/overall_plan/manual）
    is_shared: bool = False    # 跨章节共性事实（多章节复用，避免重复提取）

    def to_db_row(self, group_id: str, pid: str, sid: str,
                  group_title: str = "") -> tuple:
        """转为数据库插入行

        ✅ 新增 group_title：分组标题（如"人员角色"）此前未落库，导致
        重新查询后只能拿单条事实名（如"项目经理"）当分组标题展示。
        """
        source_json = json.dumps(
            [{"file": self.source,
              # ✅ 修复（数据流审计 2026-09-23）：旧实现静默截到 30 字、无截断标记，
              #    用户无从得知引句不完整。现放宽到 MAX_SOURCE_EXCERPT 并截断时补省略号。
              "quote": _clip_excerpt(self.source_ref, MAX_SOURCE_EXCERPT)}],
            ensure_ascii=False
        ) if self.source or self.source_ref else ""
        # value 序列化：数组 → 逗号+顿号连接（如 "垫层浇筑、防水施工、钢筋绑扎"）
        if isinstance(self.value, list):
            display_val = "、".join(str(v) for v in self.value if v is not None)
        else:
            display_val = str(self.value)
        if self.value_unit and self.value_unit != "null":
            display_val = f"{display_val} {self.value_unit}"
        # ✅ BUG 修复（2026-09-21）：模拟值标记改用统一口径 append_simulated_marker。
        # 旧写法 "  *(⚠ 模拟值)*" 是唯一的异型标记（其它写点都是 " ⚠️*(模拟值)*"），
        # 而 list_facts 的旧剥离口径只能从 `*` 起匹配 → 统一写法剥离后残留悬空 ⚠️，
        # 两种写法的解析结果不一致（详见 SIMULATED_MARKER_RE 说明）。
        content_line = f"- **{self.name}**: {append_simulated_marker(display_val, self.is_simulated)}"
        # ✅ 序列类候选值序列化为字符串再落库，避免前端把数组当 React 子节点渲染
        conflicts = [
            {"value": _as_text(c.get("value")), "source": c.get("source", ""),
             # ✅ 修复：候选值置信度也可能来自跨条目回写（非 FactItem），
             #    直接用裸值落库，查询端 float() 遇脏值会抛 500。
             "confidence": _safe_confidence(c.get("confidence"), 1.0),
             # ✅ 候选值自带的模拟值语义随值落库：resolve_conflict 裁决换值时
             #    据此重算该行的 is_simulated（否则编造值被裁决为文档实值后，
             #    行上仍残留模拟值标记，stats.simulated 永远不归零）。
             "is_simulated": bool(c.get("is_simulated"))}
            for c in (self.conflict_values or [])
        ]
        return (
            str(uuid.uuid4()), pid, sid, group_id, group_title,
            self.name, content_line,
            self.category, source_json,
            1 if self.is_simulated else 0,
            self.confidence,
            0,  # is_resolved=0 表示待审核
            1 if self.has_conflict else 0,
            json.dumps(conflicts, ensure_ascii=False) if conflicts else "",
            self.key,  # fact_key：归一化键持久化，支撑增量去重与审计追溯
            self.chunk_hash,  # chunk_hash：来源分段指纹（增量提取保留判定）
            # ✅ 数据流审计 2026-09-23：提取期已算出的溯源/语义扩展字段一并落库，
            #    不再因“只存内存”而在重新拉取时丢失（顺序须与 persist INSERT 列一致）。
            self.value_unit or "",
            self.fact_type or "",
            self.evidence_kind or "",
            self.page_ref,
            self.zone_type or "",
            1 if self.is_safety_critical else 0,
            self.norm_group or "",
            # ✅ 2026-09-24：九大章节四维标注落库（顺序须与 persist INSERT 列一致）
            self.chapter or "",
            self.fact_attr or "",
            self.source_kind or "",
            1 if self.is_shared else 0,
        )


@dataclass
class FactGroup:
    """事实分组

    ✅ persisted_group_id：persist_extraction 持久化时分配的最终 group_id
    （复用既有分组/新建）。format_for_frontend 必须优先用它——
    ✅ BUG 修复：旧实现 SSE completed 事件里的分组 id 是独立生成的
    uuid，与 DB 落库的 group_id 不一致；前端拿事件数据直接编辑分组
    （PATCH 按 group_id 匹配）会 404「事实不存在」。
    """
    title: str
    category: str = "other"
    items: list[FactItem] = field(default_factory=list)
    persisted_group_id: str = ""


@dataclass
class ExtractionResult:
    """提取结果汇总"""
    groups: list[FactGroup] = field(default_factory=list)
    total_items: int = 0
    simulated_count: int = 0
    conflict_count: int = 0
    warnings: list[str] = field(default_factory=list)
    cross_conflicts: list[dict] = field(default_factory=list)  # 交叉校验冲突记录
    # ✅ 分段级诊断：{total, ok, failed, failed_details:[{index, heading, error}]}
    #    「部分未完成」必须可定位（哪几段失败、失败原因），否则无法排查修复
    segment_stats: dict = field(default_factory=dict)
    # ✅ 增量提取（2026-09-17）：
    #    chunk_hashes_ok —— 本次成功提取（含跳过）的分段 sha1 指纹，提取成功后
    #    由调用方写入 facts_extracted_chunks，作为下次「跳过已完成段」的依据；
    #    chunk_hashes_all —— 文档切分出的全部段指纹（含因优先级截断而本轮未
    #    参与的段），用于清理「文档已删除/重解析后不再存在」的失效进度行；
    #    chunk_hashes_run —— 本次**实际重跑**（真正调用 AI）的段指纹。
    #    ✅ BUG 修复（2026-09-18）：persist_extraction 的删除范围必须用 run 而不是
    #    all —— all 含被增量跳过的段，会让「跳过段的未确认事实」被删且永不重建
    #    （该段指纹仍在进度表里，下次仍跳过）→ 不可逆数据丢失。
    #    skipped_chunks / all_skipped —— 本次跳过的段数与「全部跳过」标记。
    chunk_hashes_ok: set = field(default_factory=set)
    chunk_hashes_all: set = field(default_factory=set)
    chunk_hashes_run: set = field(default_factory=set)
    skipped_chunks: int = 0
    all_skipped: bool = False


# ---------------------------------------------------------------------------
# 归一化工具
# ---------------------------------------------------------------------------

# 事实名称 → 归一化 key 的映射表（人工维护，持续扩展）
_NAME_TO_KEY: dict[str, str] = {
    # 工期
    "项目总工期": "total_duration", "总工期": "total_duration",
    "工期": "total_duration", "施工工期": "total_duration",
    "运维期": "maintenance_period", "保质期": "warranty_period",
    "交货期": "delivery_period",
    # 人员
    "项目经理": "project_manager", "项目负责人": "project_manager",
    "技术负责人": "tech_leader", "总工": "chief_engineer",
    "安全员": "safety_officer", "质量员": "quality_officer",
    "施工队长": "foreman",
    # 设备
    "塔吊数量": "tower_crane_count", "塔吊型号": "tower_crane_model",
    "塔吊": "tower_crane_model",
    "施工电梯型号": "elevator_model", "施工电梯": "construction_elevator",
    "挖掘机型号": "excavator_model",
    "起重机型号": "crane_model",
    # 机械统计（新增分组）
    "挖掘机数量": "excavator_count", "汽车吊数量": "truck_crane_count",
    "自卸车数量": "dump_truck_count", "推土机数量": "bulldozer_count",
    "压路机数量": "roller_count", "装载机数量": "loader_count",
    "发电机数量": "generator_count", "汽车泵数量": "concrete_pump_count",
    # 材料管理（新增分组）
    "水泥强度等级": "cement_grade", "钢筋用量": "rebar_usage",
    "混凝土用量": "concrete_usage", "砂浆强度等级": "mortar_grade",
    "防水材料": "waterproof_material", "保温材料": "insulation_material",
    "材料堆场": "material_yard", "材料进场验收": "material_acceptance",
    # 施工部署 / 施工流程（新增分组）
    "施工段划分": "work_segmentation", "流水段划分": "flow_segmentation",
    "施工顺序": "construction_sequence", "施工总体部署": "overall_deployment",
    "施工目标": "construction_objectives", "施工准备": "construction_preparation",
    # 地质/工程
    "基坑深度": "foundation_depth", "开挖深度": "foundation_depth",
    "支护形式": "support_type", "支护类型": "support_type",
    "地基承载力": "bearing_capacity",
    "地下水位": "water_table", "地下水位标高": "water_table",
    "边坡坡度": "slope_ratio",
    # 材料
    "混凝土强度等级": "concrete_grade", "混凝土标号": "concrete_grade",
    "钢筋等级": "rebar_grade",
    "钢材型号": "steel_model", "型钢型号": "steel_model",
    # 安全关键
    "脚手架类型": "scaffold_type",
    "模板类型": "formwork_type",
    "最大吊装重量": "max_lift_weight",
    # 规模
    "建筑面积": "building_area",
    "占地面积": "site_area",
    "层数": "floor_count",
    "结构形式": "structure_type",
    "建筑高度": "building_height",
    # 其他
    "建设单位": "owner", "业主": "owner",
    "设计单位": "designer",
    "监理单位": "supervisor",
    "施工单位": "contractor",
    "项目地点": "project_location",
    "项目名称": "project_name",
    # 编制依据（新增分组）
    "编制依据": "compilation_basis", "验收规范": "acceptance_code",
    "执行标准": "execution_standard", "技术标准": "tech_standard",
    # 劳动力配置（新增分组）
    "劳动力计划": "labor_plan", "劳动力配置": "labor_plan", "劳动力": "labor_plan",
    "工种": "labor_trade", "高峰人数": "labor_peak", "管理人员配置": "staffing",
    # 监测方案（新增分组）
    "监测项目": "monitoring_item", "监测频率": "monitoring_frequency",
    "监测点": "monitoring_point", "报警值": "alarm_value",
    "控制值": "control_value", "监测单位": "monitoring_unit",
    # 应急处置（新增分组）
    "应急组织": "emergency_org", "应急物资": "emergency_supplies",
    "应急响应": "emergency_response", "应急电话": "emergency_phone",
    "应急演练": "emergency_drill",
    # 验收要求（新增分组）
    "验收标准": "acceptance_standard", "验收程序": "acceptance_process",
    "隐蔽验收": "concealed_acceptance", "检验批": "inspection_lot",
    # 临时工程（新增分组）
    "临时用电": "temporary_power", "临时用水": "temporary_water",
    "临时道路": "temporary_road", "临时设施": "temporary_facility",
    # 环保与文明施工（新增分组）
    "扬尘控制": "dust_control", "噪声控制": "noise_control",
    "污水处理": "sewage_treatment", "文明施工": "civilized_construction",
    # 风险与危大工程（新增分组）
    "危大工程": "hazardous_project", "重大危险源": "major_hazard_source",
    "风险等级": "risk_level", "风险辨识": "risk_identification",
}


def _build_name_key_index() -> list[tuple[str, str]]:
    """按名称长度降序排列，供 normalize_key 做「最长匹配」。

    ✅ BUG 修复：旧实现按字典插入顺序取【首个包含匹配】，短词会抢先吞掉
    长词（如新增 "监测点数量" 后，若 "数量" 类短词排在前面就会被误并），
    使不同语义的事实落到同一 key，在合并去重阶段被误判为「矛盾」或互相覆盖。
    最长匹配可保证语义最具体的映射优先命中，后续新增词条无需关心插入顺序。
    """
    return sorted(_NAME_TO_KEY.items(), key=lambda kv: -len(kv[0]))


_NAME_KEY_INDEX: list[tuple[str, str]] = _build_name_key_index()


@lru_cache(maxsize=8192)
def normalize_key(name: str) -> str:
    """将事实名称归一化为稳定的英文 key（最长匹配 + 结果缓存）"""
    if not name:
        return ""
    # 直接查映射表（已按名称长度降序 → 最长匹配优先）
    for cn, en in _NAME_KEY_INDEX:
        if cn in name:
            return en
    # 英文直接用
    if re.match(r'^[a-z_]+$', name):
        return name
    # ✅ 修复：兜底改为名称哈希（旧实现 fact_{len(name)} 会让所有同长度
    # 不同名称的事实共用一个 key，跨段合并时被误判为矛盾/互相覆盖）
    cleaned = re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]+', '_', name.strip()).strip('_')
    return f"fact_{hashlib.md5(cleaned.encode('utf-8')).hexdigest()[:8]}"


# ============================================================
# 模拟值标记（唯一写法 + 唯一剥离口径，2026-09-21 收口）
# ============================================================
# ⚠️ Emoji 变体选择符 U+FE0F：用 chr() 拼出，避免在正则里写 \ufe0f 触发
#    raw-string 转义歧义（raw 串中 \u 不被解释，会被 re 当成非法转义）。
_VS16 = chr(0xFE0F)

#: 落库 / 下发统一使用的模拟值标记（单一事实源）
SIMULATED_MARKER = " ⚠" + _VS16 + "*(模拟值)*"

#: ⚠️ 可能出现在 *( 之前、( 之后、或完全没有——历史三种写法通吃
_WARN_OPT = r"(?:⚠" + _VS16 + r"?)?"

#: 剥离正则。历史上散落三种互不相认的写法：
#:   " ⚠️*(模拟值)*"  —— _build_fact_content / create_fact / format_for_frontend
#:   "  *(⚠ 模拟值)*" —— FactItem.to_db_row（AI 提取落库路径，即 DB 内绝大多数数据）
#:   以及 list_facts 里的旧剥离口径 `\s*\*\(?\s*(?:⚠️?\s*)?模拟值\s*\)?\*\s*$`，
#:     它只能从 `*` 起匹配，而 `⚠️` 前缀恰在 `*` 之前 → 剥离后残留半个标记
#:     （"15.0m ⚠️*(模拟值)*" → "15.0m ⚠️"），另一种写法反而干净。
#: 后果：① 走过 PATCH / 手工新增的模拟值事实，value 带着悬空 ⚠️ 在界面展示；
#:       ② 该脏值被 _apply_item_updates / persist_extraction 当作「真实取值」
#:          参与「值是否变化」比较，凡曾落过统一写法的模拟值事实恒被判为
#:          「已改值」——2026-09-20 修的「只改分类/改名静默清矛盾」漏洞
#:          实际并未堵住（脏值让比较永远为 True）。
SIMULATED_MARKER_RE = re.compile(
    r"\s*" + _WARN_OPT + r"\s*\*\s*\(\s*" + _WARN_OPT + r"\s*模拟值\s*\)\s*\*\s*$"
)


def strip_simulated_marker(text: str) -> str:
    """去掉事实值末尾的模拟值标记（历史三种写法通吃），返回纯值。"""
    return SIMULATED_MARKER_RE.sub("", (text or "").strip())


def is_simulated_marked(text: str) -> bool:
    """文本中是否带模拟值标记（用于从 content 回解 is_simulated 口径）。"""
    return "模拟值" in (text or "") and bool(SIMULATED_MARKER_RE.search(text or ""))


def append_simulated_marker(value: str, is_simulated: bool) -> str:
    """按统一口径给事实值追加/不追加模拟值标记（先剥离旧标记，避免叠加）。"""
    clean = strip_simulated_marker(str(value or ""))
    return f"{clean}{SIMULATED_MARKER}" if is_simulated else clean


def extract_value_from_markdown_line(line: str) -> tuple[str, str]:

    """从旧版 Markdown 格式中提取 name 和 value
    旧格式：- **名称**: 值  或  - 名称：值
    """
    line = line.strip().lstrip("-").strip()
    # ✅ BUG 修复（2026-09-21）：模拟值标记必须先于 `**粗体**` / `*(注释)*`
    #    被整段移除。旧顺序只删掉 `*(模拟值)*` 这一半，⚠️ 前缀被留在 value 里
    #    （"- **基坑深度**: 12.5m ⚠️*(模拟值)*" → value="12.5m ⚠️"）。
    #    该脏值随后被 _apply_item_updates / persist_extraction 当作「真实取值」
    #    参与「值是否变化」比较，导致模拟值事实恒被判为已改值。
    line = strip_simulated_marker(line)
    # 去掉 **
    line = re.sub(r'\*\*(.+?)\*\*', r'\1', line)
    # 去掉 *(...)* 括号注释
    line = re.sub(r'\s*\*[^*]*\*\s*', '', line).strip()
    # 按冒号分割
    for sep in ["：", ":"]:
        if sep in line:
            name, _, value = line.partition(sep)
            return name.strip(), value.strip()
    return line, ""



# ---------------------------------------------------------------------------
# 分段工具 + 语义区域识别
# ---------------------------------------------------------------------------

# zone_type 识别权重表（命中关键词越多越确定）
_ZONE_KEYWORDS: dict[str, list[str]] = {
    "machinery_stat": [     # P11 机械统计表（最高价值结构化区）
        "主要施工机械设备", "机械设备配置", "施工机械", "机械配置表",
        "设备名称", "机械名称", "规格型号", "进退场时间",
        "塔式起重机", "挖掘机", "液压挖掘机", "履带吊", "汽车吊",
    ],
    "construction_practice_zone": [  # P7 工程做法区
        "工程做法", "施工工艺", "防水做法", "保温做法", "做法表",
        "构造层次", "分层做法", "屋面做法", "外墙做法", "地下室防水",
        "自上而下", "自下而上", "由内至外",
    ],
    "deployment_zone": [    # P8 施工部署区
        "施工部署", "施工组织", "施工段划分", "流水段", "施工区",
        "总体部署", "施工准备", "施工目标", "平面布置",
    ],
    "process_zone": [       # P9 施工流程区
        "施工流程", "施工顺序", "工艺流程", "工序衔接", "施工步骤",
        "测量放线", "土方开挖", "垫层浇筑", "防水层施工", "钢筋绑扎",
        "→", "施工方法与措施", "施工工艺流程",
    ],
    "material_mgmt_zone": [ # P10 材料管理区
        "材料管理", "材料规格", "材料进场", "材料计划", "检验复试",
        "混凝土强度", "钢筋级别", "砂浆等级", "模板配置", "周转材料",
        "C30", "C35", "C40", "HRB400", "HRB335", "M10",
    ],
    "schedule_zone": [
        "工期安排", "进度计划", "施工进度", "总工期", "施工计划",
        "开工日期", "竣工日期", "进度表", "横道图",
    ],
    "personnel_zone": [
        "项目组织", "管理人员", "人员配置", "项目经理", "技术负责人",
        "安全管理机构", "质量保证体系",
    ],
    "tech_param_zone": [
        "工程概况", "地质条件", "基坑设计", "支护结构", "地下水",
        "土层参数", "承载力", "边坡",
    ],
    # --- 新增语义区（对应新增 fact_type 类别）---
    "basis_zone": [
        "编制依据", "依据规范", "执行标准", "标准规范", "设计文件",
        "法律法规", "技术规程", "验收规范", "图纸会审",
    ],
    "monitoring_zone": [
        "监测方案", "监测项目", "监测频率", "监测点", "报警值", "控制值",
        "沉降观测", "变形观测", "第三方监测", "监测预警",
    ],
    "emergency_zone": [
        "应急预案", "应急处置", "应急救援", "应急物资", "应急组织",
        "应急演练", "抢险", "突发事件", "应急响应",
    ],
    "acceptance_zone": [
        "验收要求", "验收标准", "验收程序", "隐蔽验收", "检验批",
        "分项验收", "质量验收", "验收依据",
    ],
    "labor_zone": [
        "劳动力", "劳动力计划", "劳动力配置", "工种", "投入人员",
        "用工计划", "高峰人数", "管理人员配置",
    ],
    "environment_zone": [
        "环境保护", "文明施工", "扬尘", "噪声", "污水", "固废",
        "降尘", "垃圾清运", "绿色施工",
    ],
    "temporary_zone": [
        "临时用电", "临时用水", "临时道路", "临时设施", "临建",
        "施工用电", "配电箱", "临时消防",
    ],
    "risk_zone": [
        "危险源", "重大危险源", "危大工程", "风险等级", "风险辨识",
        "风险管控", "危险因素", "安全隐患",
    ],
}

_priority_weights: dict[str, float] = {
    "machinery_stat": 1.5,          # 结构化表格，价值最高
    "construction_practice_zone": 1.3,
    "deployment_zone": 1.2,
    "process_zone": 1.2,
    "material_mgmt_zone": 1.2,
    "monitoring_zone": 1.2,          # 监测参数直接关系安全，高价值
    "risk_zone": 1.2,
    "emergency_zone": 1.1,
    "acceptance_zone": 1.1,
    "labor_zone": 1.1,
    "temporary_zone": 1.1,
    "schedule_zone": 1.1,
    "environment_zone": 1.0,
    "personnel_zone": 1.0,
    "tech_param_zone": 1.0,
    "basis_zone": 0.9,               # 规范引用类，结构化价值中等
    "general": 0.8,                  # 低权重区（标题/目录/规范引用）
}


@dataclass
class Chunk:
    """带语义元信息的分段产物

    ✅ 报告步骤⑤：标题边界 > 表格完整 > token 预算；高权重区切分更细。
    zone_type 决定步骤⑥按段注入哪套 P7-P11 类型规则块。
    """
    text: str                    # 段落原文
    zone_type: str = "general"   # 语义区域类型（machinery_stat / construction_practice_zone / ... / general）
    priority_weight: float = 0.8 # 优先权重（高权重区切分更细、提取时注入更多上下文）
    heading: str = ""            # 该段归属的最近标题（用于溯源）


def _classify_zone_type(text: str) -> str:
    """根据段落标题和关键词识别 zone_type"""
    lower = text.lower()
    scores: list[tuple[str, int]] = []
    for zone, kws in _ZONE_KEYWORDS.items():
        hit = sum(1 for kw in kws if kw.lower() in lower or kw in text)
        if hit:
            scores.append((zone, hit))
    if not scores:
        return "general"
    scores.sort(key=lambda x: -x[1])
    return scores[0][0]


def _split_table_rows(sec: str, chunk_size: int, chunks: list, heading: str,
                      zone_type: str = "general",
                      priority_weight: float = 0.8) -> None:
    """把超长表格（如百行机械统计表）按行切分为多个 Chunk，每段复用表头行。

    ✅ 增强（报告步骤⑤表格完整性升级）：旧实现对超长表格直接整段塞入
    （最多放宽到 1.5x chunk_size），一旦表格超过该上限，单段输入体量爆炸、
    极易触发 LLM 超时/截断，该段提取整体失败。现改为逐行切分，每段
    保留表头（列名），既保护表格语义、又让每段体量可控。

    ✅ BUG 修复（zone_type 丢失）：旧实现切分子段的 zone_type 强制为
    "general"/0.8 —— 机械统计表本是最高权重区（machinery_stat/1.5），
    按行切分后每段却以"通用资料区"提示提取，P11 机械统计规则块不再激活，
    恰好削弱了最需要表格保护的对象。现由调用方传入对原段分类的结果。

    ✅ BUG 修复（表头判定）：旧实现 `if header_idx` 在表格位于段首
    （header_idx=0，最常见的形态：表头即首行）时判定为假 → header=[],
    body=全部行 → 每个切分子段【都不再复用表头】，AI 看到的是无列名的
    裸数据行，机械统计表逐行提取质量大降。现按"首个表格行是否真实存在"
    判定，无条件复用表头。
    """
    lines = sec.split("\n")

    # 表头：第一个表格行（以 | 开头）。
    # ✅ token 优化：旧实现把表头前的全部说明文字并入 header，每个切分子段
    # 重复注入可能上千字的说明。说明行只在首个子段保留一次。
    header_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip().startswith("|")), -1)
    if header_idx >= 0:
        header = [lines[header_idx]]
        # 若下一行为 Markdown 对齐分隔行（|---|---|），一并保留
        if (header_idx + 1 < len(lines)
                and set(lines[header_idx + 1].strip().replace("|", "").replace(":", "").replace("-", "").strip()) == set()):
            header.append(lines[header_idx + 1])
            body = lines[header_idx + 2:]
        else:
            body = lines[header_idx + 1:]
        # 说明行仅随第一个子段注入
        preamble = lines[:header_idx]
    else:
        # 防御：调用方保证存在表格行；万一无表格行则整段按普通文本切
        header = []
        body = lines
        preamble = []

    cur: list[str] = list(preamble) + list(header)

    def _flush() -> None:
        if cur:
            chunks.append(Chunk(
                text="\n".join(cur).strip(),
                zone_type=zone_type,
                priority_weight=priority_weight,
                heading=heading,
            ))

    for ln in body:
        # ✅ BUG 修复（丢行）：旧实现对超限行先 _flush() 缓冲、再以
        #    `cur = header + [ln]` 重新开段，正常长度的行不会丢；但当
        #    【单行本身超长】（如合并单元格、长文本单元格）时，
        #    `len(cur)+len(ln)+1 > chunk_size` 恒成立 → 每次循环都
        #    _flush + 重建 cur，该行永远无法入段，循环结束后也无兑底
        #    → 整行内容静默丢失，机械统计表关键设备行可能就此消失。
        #    现：超长单行直接强制独立成段（与正文区“超长 part 强制按字切”
        #    同口径，至少不丢内容）。
        if len(ln) + 1 > chunk_size:
            _flush()
            cur = list(header) if header else []
            chunks.append(Chunk(
                text="\n".join([ln] if not header else header + [ln]).strip(),
                zone_type=zone_type,
                priority_weight=priority_weight,
                heading=heading,
            ))
            continue
        if cur and len("\n".join(cur)) + len(ln) + 1 > chunk_size:
            _flush()
            cur = list(header) + [ln]
        else:
            cur.append(ln)
    _flush()


def resolve_chunk_size(chunk_size: int = CHUNK_SIZE) -> int:
    """按「模型上下文窗口 × 0.8 − 固定消息」动态决定分段上限（对齐易标 :363-377）。

    默认关闭（``settings.facts_context_budget_split=False``）→ 原样返回调用方传入的
    ``chunk_size``（本仓历史基线 8000），切分行为与引入前逐字节一致。

    开启后的意义：本仓 8000 字是**固定值**，对上下文窗口 128k 的模型过于保守
    （资料被切成大量小段 → AI 调用次数与 429 限流风险成倍上升）；而对
    上下文只有 32k 的模型，8000 字 + 归一化字典 + 规则块又可能撑爆。
    按易标口径动态计算可同时解决两端。

    Args:
        chunk_size: 调用方显式指定的上限（默认 ``CHUNK_SIZE``）。

    Returns:
        生效的分段上限；动态计算失败时回落到 ``chunk_size``（fail-soft）。
    """
    try:
        if not settings.facts_context_budget_split:
            return chunk_size
    except Exception:  # pragma: no cover - 配置读取异常时保持旧行为
        return chunk_size
    try:
        from app.services.facts_patches import get_segment_limit
        # 固定消息 = system 提示词骨架 + 归一化字典块（两者都与资料长度无关），
        # 与易标 getMessagesContentLength 的口径一致（每条额外计 64 字符开销）。
        fixed = [{"role": "system", "content": _FIXED_PROMPT_SKELETON}]
        limit = get_segment_limit(None, fixed)
        # 动态值不得小于历史基线的下限保护：过小的窗口配上过小的段会让长资料
        # 段数爆炸（调用次数/限流风险反而上升），故取 max(limit, CHUNK_SIZE)
        # 在「窗口够大时放宽、窗口很小时不更激进」之间取得平衡。
        return max(limit, CHUNK_SIZE)
    except Exception:  # noqa: BLE001 - 任何异常都退回旧行为
        logger.warning("上下文预算分段计算失败，回落到 CHUNK_SIZE=%d", chunk_size,
                       exc_info=True)
        return chunk_size


def split_into_chunks(text: str, chunk_size: int = CHUNK_SIZE,
                       overlap: int = CHUNK_OVERLAP) -> list[Chunk]:
    """智能切分资料为多个重叠段落（报告步骤⑤语义分段升级）

    ✅ 升级点：
    1. 返回 Chunk 对象（非纯 str），携带 zone_type / priority_weight / heading 元信息
    2. 按章节标题边界优先切分
    3. 检测并保护表格完整性（表格段不强制切断，整表归入同一段）
    4. 高权重区（machinery_stat / construction_practice_zone 等）切分更细，
       低权重区（目录/规范引用）切分更粗
    5. ``settings.facts_context_budget_split=True`` 时分段上限改由模型上下文
       窗口动态决定（默认关闭 → 与引入前逐字节一致）
    """
    if not text:
        return []

    chunk_size = resolve_chunk_size(chunk_size)

    # 先按章节标题边界切分（markdown # / Word 四级编号）
    heading_patterns = [
        r'\n(?=#{1,6}\s)',           # markdown 标题
        r'\n(?=(?:第[一二三四五六七八九十\d]+[章节篇部]))',  # 中文章节编号
        r'\n(?=(\d+\.)+\d+\s)',      # 阿拉伯数字多级编号 1. 1.1 1.1.1
    ]
    raw_sections = [text]
    for pat in heading_patterns:
        merged = []
        for sec in raw_sections:
            merged.extend([s for s in re.split(pat, sec) if s])
        raw_sections = merged

    chunks: list[Chunk] = []
    current_text = ""
    current_heading = ""

    # ✅ 优化：单次分段调用内对语义区识别做记忆化。长文档（特别是上百行的
    #    机械统计表）会产生大量内容相同的子段（如复用表头行），重复全量扫描
    #    关键词代价高；按文本缓存后相同段只算一次。
    _zone_cache: dict[str, str] = {}

    def _classify(text: str) -> str:
        cached = _zone_cache.get(text)
        if cached is None:
            cached = _classify_zone_type(text)
            _zone_cache[text] = cached
        return cached

    def _finalize():
        nonlocal current_text
        if not current_text.strip():
            return
        zone = _classify(current_text)
        chunks.append(Chunk(
            text=current_text.strip(),
            zone_type=zone,
            priority_weight=_priority_weights.get(zone, 0.8),
            heading=current_heading,
        ))
        current_text = ""

    for sec in raw_sections:
        sec = sec.strip()
        if not sec:
            continue
        # 提取标题行作为 heading
        lines = sec.split("\n")
        first_line = lines[0].strip() if lines else ""
        if (first_line.startswith("#") or
                re.match(r'^(第[一二三四五六七八九十\d]+[章节篇部]|[一二三四五六七八九十]+[、．.]|\d+[\.、]\d*)', first_line)):
            current_heading = first_line.lstrip("#").strip()[:60]

        # 检测是否为表格主导段（保护表格不切断）
        # ✅ 修复（2026-09-17）：旧实现只扫描段首 200 字，前导说明/公式较长的段，
        #    表格落在 200 字之后会被漏判为普通段、从表格中间切断行 → 该段提取质量
        #    下降、事实丢失。现对整段扫描表格行数。
        is_table_heavy = sec.count("\n|") >= 3

        # 动态 chunk_size：高权重区切分更细
        estimated_zone = _classify(sec)
        effective_size = chunk_size
        if _priority_weights.get(estimated_zone, 0.8) >= 1.2:
            effective_size = int(chunk_size * 0.7)  # 高权重区切分更细
        elif _priority_weights.get(estimated_zone, 0.8) <= 0.9:
            effective_size = chunk_size  # 低权重区保持

        if is_table_heavy:
            if len(sec) <= chunk_size:
                # 普通表格：整表归入 current，不强制切断（上限 1.5x）
                if len(current_text) + len(sec) <= chunk_size * 1.5:
                    current_text += ("\n\n" if current_text else "") + sec
                else:
                    _finalize()
                    current_text = sec  # 表格段直接起新段，不管它多长
            else:
                # ✅ 超大表格（如上百行机械统计表）：按行切分，每段复用表头行，
                # 避免单段塞入超长表格导致 LLM 输入超限 / 单次调用失败、
                # 进而该段提取整体失败（表现为「部分段落无事实」）。
                _finalize()
                _split_table_rows(sec, effective_size, chunks, current_heading,
                                  zone_type=estimated_zone,
                                  priority_weight=_priority_weights.get(
                                      estimated_zone, 0.8))
                current_text = ""
        elif len(sec) > effective_size:
            sub_parts = re.split(r'\n\n', sec)
            for part in sub_parts:
                if len(current_text) + len(part) <= effective_size:
                    current_text += ("\n\n" if current_text else "") + part
                else:
                    _finalize()
                    if len(part) > effective_size:
                        # 超长 part 强制按字数切（相同子段文本走 _classify 缓存，仅首次扫描）
                        for i in range(0, len(part), effective_size - overlap):
                            _seg_text = part[i:i + effective_size]
                            _seg_zone = _classify(_seg_text)
                            chunks.append(Chunk(
                                text=_seg_text,
                                zone_type=_seg_zone,
                                priority_weight=_priority_weights.get(_seg_zone, 0.8),
                                heading=current_heading,
                            ))
                        current_text = ""
                    else:
                        current_text = part
        else:
            if len(current_text) + len(sec) <= effective_size:
                current_text += ("\n\n" if current_text else "") + sec
            else:
                _finalize()
                current_text = sec
    _finalize()

    logger.info("分段：%d 段 (总字符 %d, zone_types: %s)",
                len(chunks), len(text),
                {z: sum(1 for c in chunks if c.zone_type == z)
                 for z in set(c.zone_type for c in chunks)})
    return chunks


# ---------------------------------------------------------------------------
# 提取管线
# ---------------------------------------------------------------------------

async def extract_from_single_chunk(text: str, context_summary: str = "",
                                     chunk_index: int = 0,
                                     zone_type: str = "general",
                                     priority_weight: float = 0.8,
                                     heading: str = "",
                                     error_out: dict | None = None,
                                     missing_value_mode: str = "fabricate") -> list[FactItem]:
    """从单个文本段中提取事实

    ✅ 升级（报告 v2.0 + 步骤⑥ zone_type 加权注入）：
    - 同时支持新 JSON Schema `{"facts":[...]}` 和旧格式 `{"groups":[...]}`
    - 注入 NORM_DICT_BLOCK / summary / chunk_index
    - 注入 zone_type / priority_weight / heading 动态变量
      （按 zone_type 激活对应类型规则块提示，低权重区用通用规则，省 token）
    - 解析 fact_type / evidence_kind / value_unit / page_ref / is_safety_critical 扩展字段
    """
    # 组装归一化字典文本块（供 LLM 参考标准名/规格写法）
    norm_dict_block = _build_norm_dict_block()

    # ✅ 报告步骤⑥：按 zone_type 动态激活对应类型规则块提示
    zone_hint = {
        "machinery_stat": "本段检测到【机械统计表】区（高权重），请严格启用 P11 机械统计规则块",
        "construction_practice_zone": "本段检测到【工程做法】区（高权重），请严格启用 P7 工程做法规则块",
        "deployment_zone": "本段检测到【施工部署】区（中权重），请启用 P8 施工部署规则块",
        "process_zone": "本段检测到【施工流程】区（中权重），请启用 P9 施工流程规则块；流程用有序数组输出",
        "material_mgmt_zone": "本段检测到【材料管理】区（中权重），请启用 P10 材料管理规则块",
        "schedule_zone": "本段检测到【工期安排】区（P13），请启用工期规则块",
        "personnel_zone": "本段检测到【人员配置】区（P12），请启用人员/劳动力规则块",
        "tech_param_zone": "本段检测到【技术参数/工程概况】区",
        "basis_zone": "本段检测到【编制依据】区（P14），请启用编制依据规则块；规范编号与名称原样保留",
        "monitoring_zone": "本段检测到【监测方案】区（P15），请启用监测规则块；测点/频率/报警值必须成组提取",
        "emergency_zone": "本段检测到【应急处置】区（P16），请启用应急规则块",
        "acceptance_zone": "本段检测到【验收要求】区（P17），请启用验收规则块",
        "labor_zone": "本段检测到【劳动力配置】区（P12），请启用劳动力规则块",
        "environment_zone": "本段检测到【环保与文明施工】区（P18），请启用环保规则块",
        "temporary_zone": "本段检测到【临时工程】区（P19），请启用临时工程规则块",
        "risk_zone": "本段检测到【风险与危大工程】区（P20），请启用风险规则块",
        "general": "本段为通用资料区，综合运用所有规则块",
    }.get(zone_type, "本段为通用资料区")

    priority_hint = (
        f"（优先权重 {priority_weight:.1f}，属于{'高' if priority_weight >= 1.3 else '中' if priority_weight >= 1.0 else '低'}价值区）"
    )

    def _validate(o) -> list[str]:
        # ✅ BUG 修复：旧校验对 `{}` / 缺字段返回 []（视为通过），
        #    既不触发定向修复也不重试，表现为"模型偶发输出空对象 →
        #    该段静默零事实"。现要求必须显式给出 facts（可为空数组）；
        #    确无事实时须按 Schema 置 segment_failed=true。
        if not isinstance(o, dict):
            return ["顶层必须是 JSON 对象"]
        if o.get("segment_failed") is True:
            return []
        if o.get("facts") is None and o.get("groups") is None:
            return ["缺少 facts 字段（无事实时应输出 facts: [] 并置 segment_failed=true）"]
        return []

    try:
        prompt_material = text
        if settings.prompt_injection_defense:
            from app.services.prompt_governance import guard_material
            prompt_material = guard_material("全局事实提取资料", text)
        sys_prompt = render("facts_extract_system",
                           material=prompt_material,
                           summary=context_summary[:500] if context_summary else "（无全文摘要）",
                           chunk_index=chunk_index + 1,
                           NORM_DICT_BLOCK=norm_dict_block,
                           zone_type=zone_type,
                           priority_weight=f"{priority_weight:.1f}",
                           heading=heading or "（无章节标题）",
                           zone_hint=zone_hint,
                           priority_hint=priority_hint)
    except Exception as e:
        logger.warning("第 %d 段提示词渲染失败: %s", chunk_index + 1, e)
        if error_out is not None:
            error_out["error"] = f"提示词渲染失败: {e}"
        return []

    # ✅ 缺值模式（对齐 OpenBidKit 全局事实三模式 fabricate/omit/placeholder）：
    #  - fabricate（默认）：资料未给出的值允许 AI 合理补全（is_simulated 标记，前端可见）
    #  - omit：严禁编造，无法给出就跳过该条
    #  - placeholder：资料未给出的值逐字写「【待填写】」
    _mode = normalize_missing_value_mode(missing_value_mode)
    if _mode == "omit":
        sys_prompt += (
            "\n\n【缺值模式：不杜撰（omit）】资料中未明确给出的确定性数据"
            "（人数、工期、设备型号、强度等级、人员姓名等），严禁编造具体值："
            "可改写成笼统但正确的表述（如“按设计要求配置”“满足规范要求”）；"
            "无法笼统表述的条目直接跳过不要输出，所有输出条目 is_simulated 一律为 false。")
    elif _mode == "placeholder":
        sys_prompt += (
            "\n\n【缺值模式：留待填写（placeholder）】资料中未明确给出的确定性数据，"
            "value 逐字写为“【待填写】”，不要编造任何具体值，is_simulated=true。")

    # ✅ 容错重试：429 限流 / 瞬时网络错误常是阶段性的，直接放弃该段会造成
    #    "功能部分未完成"（部分段落无事实）。这里做指数退避重试。
    obj = None
    last_err: Exception | None = None
    for attempt in range(FACTS_CHUNK_RETRIES + 1):
        try:
            obj, _ = await collect_json_response(
                [{"role": "system", "content": sys_prompt}], _validate,
                # 低温 + JSON 模式 + 放宽超时：显著降低非法 JSON 与超时失败率
                temperature=0.0,
                json_mode=True,
                timeout=FACTS_REQUEST_TIMEOUT,
                repair_key="facts_json_fix_system",
                scene="facts_extract",
            )
            last_err = None
            break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_err = e
            # ✅ 结构性输出错误（schema 校验失败/非法 JSON）内层已做过定向
            #    修复轮，整段重试不会改变结果，只会成倍放大 AI 调用与耗时
            #    （最多 3×3=9 次/段）。立即终止该段重试。
            if _is_structural_error(e):
                logger.warning(
                    "第 %d 段提取返回结构性错误（内层修复未通过，不再整段重试）: %s",
                    chunk_index + 1, e)
                break
            if attempt < FACTS_CHUNK_RETRIES:
                err_s = str(e).lower()
                # 429 限流需要更长的冷却，等待限流窗口恢复
                base = (FACTS_RETRY_BACKOFF_RATE_LIMIT
                        if ("429" in err_s or "rate" in err_s or "limit" in err_s)
                        else FACTS_RETRY_BACKOFF)
                backoff = base * (2 ** attempt)
                logger.warning(
                    "第 %d 段提取失败（第 %d/%d 次），%.0fs 后重试: %s",
                    chunk_index + 1, attempt + 1, FACTS_CHUNK_RETRIES + 1,
                    backoff, e)
                await asyncio.sleep(backoff)

    if obj is None:
        logger.warning("第 %d 段提取最终失败（已重试 %d 次）: %s",
                       chunk_index + 1, FACTS_CHUNK_RETRIES, last_err)
        if error_out is not None:
            error_out["error"] = str(last_err)[:200] if last_err else "未知错误"
        return []

    items: list[FactItem] = []
    # ✅ 新格式：{"facts": [...]}
    for f in obj.get("facts", []) or []:
        parsed = _parse_fact_dict(f, default_source=f"第{chunk_index + 1}段:{heading[:30]}")
        if parsed:
            items.append(parsed)
    # ✅ 旧格式兼容：{"groups": [{"items":[...]}]}（渐进迁移期间保留）
    if not items:
        for g in obj.get("groups", []) or []:
            for it in g.get("items", []) or []:
                parsed = _parse_legacy_fact_dict(it, chunk_index=chunk_index)
                if parsed:
                    items.append(parsed)

    # 安全关键事实过滤：禁止保留模拟值（is_simulated 恒 false 但仍保留此防线）
    items = _filter_safety_sensitive(items)
    return items


@lru_cache(maxsize=1)
def _build_norm_dict_block() -> str:
    """从 _norm_dicts.py 生成 Prompt 可读的归一化字典文本块。

    ✅ 速度：字典块是静态内容，旧实现每段都重新拼一遍（几十段 = 几十次重复
    字符串构建）；现进程内缓存一次，直接复用。
    """
    from app.services.ai.prompts._norm_dicts import (
        NORM_DICT_MATERIAL, NORM_DICT_MACHINERY, NORM_DICT_PROCESS,
        NORM_DICT_SCHEDULE, MACH_HEADER_MAP, UNIT_ALIASES,
    )

    def _fmt_dict(name: str, d: dict) -> str:
        lines = [f"### {name}"]
        for k, variants in d.items():
            if isinstance(variants, list):
                lines.append(f"  {k}: [{', '.join(repr(v) for v in variants)}]")
            else:
                lines.append(f"  {k}: {repr(variants)}")
        return "\n".join(lines)

    parts = [
        _fmt_dict("NORM_DICT_MATERIAL（材料规格等级）", NORM_DICT_MATERIAL),
        _fmt_dict("NORM_DICT_MACHINERY（机械名称，仅名称归一；型号代码原样保留）", NORM_DICT_MACHINERY),
        _fmt_dict("NORM_DICT_PROCESS（工序名，用于流程序列）", NORM_DICT_PROCESS),
        _fmt_dict("NORM_DICT_SCHEDULE（工期关键词）", NORM_DICT_SCHEDULE),
        _fmt_dict("MACH_HEADER_MAP（机械统计表表头→字段映射）", MACH_HEADER_MAP),
        _fmt_dict("UNIT_ALIASES（计量单位标准写法：value_unit 一律用右侧标准值）", UNIT_ALIASES),
        "（注：型号代码如 QTZ80/PC220/XCMG-ZL50 不归一，原样保留）",
    ]
    return "\n\n".join(parts)


def _safe_confidence(raw, default: float = 0.8) -> float:
    """置信度安全转换：模型可能输出 "high"/null/越界值 → 防御并 clamp 到 [0,1]。

    ✅ BUG 修复：旧实现 `float(f.get("confidence", 0.8))` 直接转换——
    模型输出脏值（如字符串 "high"）会抛 ValueError，异常沿
    _parse_fact_dict → extract_from_single_chunk 传播，
    该段已成功解析的其余事实全部丢弃（表现为"某段永远失败"）。
    """
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(0.0, min(1.0, v))


def _parse_fact_dict(f: dict, default_source: str = "") -> FactItem | None:
    """研究报告 v2.0 新格式解析：{"facts": [...]} 单条"""
    name = (f.get("name") or "").strip()
    if not name:
        return None

    value_raw = f.get("value")
    if value_raw is None or value_raw == "" or value_raw == []:
        return None
    # value 可能是 string、int、float、list
    if isinstance(value_raw, (int, float)):
        value = str(value_raw)
    elif isinstance(value_raw, list):
        value = value_raw  # 保留数组
    else:
        value = str(value_raw).strip()

    key = f.get("key") or normalize_key(name)
    # fact_type → category 映射
    fact_type = f.get("fact_type", "")
    category = _FACT_TYPE_TO_CATEGORY.get(fact_type, "other")
    source = f.get("source") or default_source
    source_ref = f.get("source_text") or f.get("source_ref") or ""
    is_sim = bool(f.get("is_simulated", False))
    confidence = _safe_confidence(f.get("confidence"), 0.8)
    value_unit = f.get("value_unit")
    evidence_kind = f.get("evidence_kind", "")
    # page_ref 防御：非整数（"第3页"/null/float）一律安全转 int | None
    page_ref_raw = f.get("page_ref")
    try:
        page_ref = int(page_ref_raw) if page_ref_raw is not None else None
    except (TypeError, ValueError):
        page_ref = None
    zone_type = f.get("zone_type", "")
    is_safety = bool(f.get("is_safety_critical", False))

    # is_safety_critical 白名单兜底：fact_type 未标但 name/key 命中时补标
    if not is_safety and _is_safety_critical(FactItem(name=name, value=str(value), key=key)):
        is_safety = True

    return FactItem(
        name=name, value=value, key=key, category=category,
        source=source, source_ref=source_ref,
        is_simulated=is_sim, confidence=confidence,
        value_unit=value_unit, fact_type=fact_type,
        evidence_kind=evidence_kind, page_ref=page_ref,
        zone_type=zone_type, is_safety_critical=is_safety,
    )


def _parse_legacy_fact_dict(it: dict, chunk_index: int = 0) -> FactItem | None:
    """旧格式 {"groups":[...]} 单条 —— 渐进迁移期兼容"""
    name = (it.get("name") or "").strip()
    value = (it.get("value") or "").strip()
    if not name or not value:
        return None
    key = it.get("key") or normalize_key(name)
    cat = it.get("category") or "other"
    return FactItem(
        name=name, value=value, key=key, category=cat,
        source=it.get("source", f"第{chunk_index + 1}段"),
        source_ref=it.get("source_ref", ""),
        is_simulated=bool(it.get("is_simulated", False)),
        confidence=_safe_confidence(it.get("confidence"), 0.8),
    )


# fact_type → category 完整映射（可行性研究报告 v2.0 + 规则扩充）
_FACT_TYPE_TO_CATEGORY: dict[str, str] = {
    "basic": "basic",
    "basis": "basis",                    # 新增：编制依据（规范/标准/设计文件）
    "personnel": "personnel",
    "labor": "labor",                    # 新增：劳动力配置（工种/人数）
    "schedule": "schedule",
    "machinery": "machinery",
    "material": "material_mgmt",
    "construction_practice": "execution",
    "deployment": "deployment",
    "process_flow": "process",
    # ✅ 分类修复：tech_param / design_param / equipment 事实类型在提示词中均
    #    定义为「技术参数 / 地质参数 / 基坑深度 / 地下水位…」（见 analysis.py 的
    #    fact_type 枚举说明），但旧实现把它们统统映射到 equipment 类别
    #    （标题"主要设备配置"），导致基坑深度、承载力、土层参数等地质/技术参数
    #    事实被错误展示在「设备配置」分组下，与"机械统计"混淆。
    #    现统一归入 tech_param 类别（标题"技术参数"），与语义一致；
    #    equipment 类别保留给「实际机械设备」（fact_type=machinery 单独处理），
    #    并仍可作为手工录入选项使用。
    "tech_param": "tech_param",
    "design_param": "tech_param",
    "equipment": "tech_param",
    "safety": "safety_critical",
    "risk": "risk",                      # 新增：风险与危大工程
    "monitoring": "monitoring",          # 新增：监测方案
    "emergency": "emergency",            # 新增：应急处置
    "acceptance": "acceptance",          # 新增：验收要求
    "temporary": "temporary",            # 新增：临时工程
    "environment": "environment",        # 新增：环保与文明施工
    "quality": "quality",
    "other": "other",
}

# category → fact_type 反查表（供查询接口回推细粒度类型，避免扩展字段落库后丢失）
CATEGORY_TO_FACT_TYPE: dict[str, str] = {}
for _ft, _cat in _FACT_TYPE_TO_CATEGORY.items():
    CATEGORY_TO_FACT_TYPE.setdefault(_cat, _ft)

# category → 中文分组标题（单一事实源，供提取分组与查询聚合共用）
CATEGORY_TITLES: dict[str, str] = {
    "basic": "基本信息",
    "basis": "编制依据",
    "personnel": "人员角色",
    "labor": "劳动力配置",
    "schedule": "工期安排",
    "equipment": "主要设备配置",
    "machinery": "机械统计",
    "material_mgmt": "材料管理",
    "tech_param": "技术参数",
    "scale": "工程规模与地质参数",
    "risk": "风险与危大工程",
    "safety_critical": "安全关键参数",
    "deployment": "施工部署",
    "temporary": "临时工程",
    "process": "施工流程",
    "execution": "工程做法",
    "monitoring": "监测方案",
    "quality": "质量标准",
    "acceptance": "验收要求",
    "commitment": "服务承诺",
    "environment": "环保与文明施工",
    "emergency": "应急处置",
    "other": "其他事实",
}

# category 展示顺序（提取结果分组的稳定排序）
_CATEGORY_ORDER: list[str] = [
    "basic", "basis", "personnel", "labor", "schedule", "equipment",
    "machinery", "material_mgmt", "tech_param", "scale", "risk",
    "safety_critical", "deployment", "temporary", "process", "execution",
    "monitoring", "quality", "acceptance", "commitment", "environment",
    "emergency", "other",
]


def _filter_safety_sensitive(items: list[FactItem]) -> list[FactItem]:
    """安全关键项禁止保留模拟值"""
    result = []
    for it in items:
        if it.is_simulated and _is_safety_critical(it):
            logger.info("移除安全关键模拟值: %s", it.name)
            continue
        result.append(it)
    return result


def _is_safety_critical(item: FactItem) -> bool:
    """判断是否为安全关键事实（仅按事实名称 / 归一化键判定）。

    ✅ BUG 修复：旧实现额外用 value 做子串匹配，而白名单含"工期""荷载""塔吊"
    等短词 —— 普通事实（如 name="质量目标", value="满足工期要求"）仅因值里
    出现"工期"就被判为安全关键，第 7 步归一化时 category 被强制改写为
    safety_critical，跨分类漂移。白名单语义本就是【事实名称】白名单
    （基坑深度、支护形式…），值命中不构成依据，故移除 value 匹配。
    """
    name = item.name or ""
    for kw in SAFETY_CRITICAL_FACT_KEYS:
        if kw in name:
            return True
    key_to_safety = {
        "foundation_depth", "support_type", "tower_crane_model",
        "concrete_grade", "bearing_capacity", "water_table",
        "rebar_grade", "steel_model", "scaffold_type",
        "formwork_type", "max_lift_weight", "slope_ratio",
    }
    return item.key in key_to_safety


def is_safety_critical_name(name: str, value: str = "", key: str = "") -> bool:
    """无 FactItem 实例时的安全关键判定（供查询接口读取 DB 行后复算）。

    ✅ 增强：`is_safety_critical` 是纯程序规则，落库时并未单独建列保存。
    旧实现只在提取阶段计算，重新查询后该标记丢失 → 前端"安全关键"提示
    刷新即消失。现由查询接口按同一规则复算，保证展示与提取一致。
    """
    return _is_safety_critical(
        FactItem(name=name or "", value=value or "",
                 key=key or normalize_key(name or "")))


# ---------------------------------------------------------------------------
# 合并去重与矛盾检测
# ---------------------------------------------------------------------------

def _as_text(v) -> str:
    """把 value（str / list / 数字 / None）安全转为比较用字符串。

    ✅ BUG 修复：序列类事实（fact_type=process_flow 等）的 value 是有序数组，
    旧实现直接调用 str.replace / re.sub / unicodedata.normalize，遇 list 会抛
    AttributeError/TypeError，导致整条提取管线或交叉校验中断。
    """
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "、".join(str(x) for x in v if x is not None)
    return str(v)


def _norm_value(v) -> str:
    """值归一化：去除全部空白 + 统一全角冒号，用于去重/矛盾比较。

    ✅ BUG 修复：兼容序列类 list 值（先安全转字符串再归一）。
    """
    return re.sub(r'\s+', '', _as_text(v).replace('：', ':'))


def apply_norm_dicts(all_items: list[FactItem]) -> None:
    """名称/规格归一化（报告补充①：与 Prompt 共用单一事实源）。

    机械名称归一使"塔式起重机型号"与"塔吊型号"聚到同一 key；
    材料规格归一使"C 30/Ｃ30"与"C30"同值合并；
    名称变化时同步重派生 key（仅当旧 key 确实派生自旧名称）。

    ✅ BUG 修复：value 可能是序列（list）。旧实现对 list 调用
    normalize_material_spec → unicodedata.normalize 抛 TypeError，
    使整条管线在"归一化"步骤直接失败、零事实落库。
    现按元素逐项归一，序列顺序保持不变。
    """
    for it in all_items:
        new_name = normalize_machinery_name(it.name)
        if new_name != it.name and (
                not it.key or it.key == normalize_key(it.name)):
            it.key = normalize_key(new_name)
        it.name = new_name
        if isinstance(it.value, list):
            it.value = [normalize_material_spec(_as_text(v)) if isinstance(v, str) else v
                        for v in it.value]
        else:
            it.value = normalize_material_spec(_as_text(it.value))
        # 单位规范化：台套→台、米→m、日历日→日历天等（统一全文口径）
        if it.value_unit:
            it.value_unit = normalize_unit(it.value_unit)


def merge_and_deduplicate(all_items: list[FactItem]) -> list[FactItem]:
    """合并去重：同 key 聚类，选最优值；同 key 多值标记矛盾"""
    by_key: dict[str, list[FactItem]] = {}
    for it in all_items:
        by_key.setdefault(it.key, []).append(it)

    resolved: list[FactItem] = []
    for key, group in by_key.items():
        # ✅【增强·数据完整性】合并去重时优先保留真实提取值：
        #   先按 is_simulated 升序（非模拟在前），再按 confidence 降序。
        #   旧实现仅按 confidence 排序，若 AI 给某「模拟值」标了较高置信度，
        #   它会成为对外展示的 best 事实、真实提取值反被埋进 conflict_values，
        #   违背「模拟值闸门」设计（真实事实应始终优先于模拟值）。
        group.sort(key=lambda x: (x.is_simulated, -x.confidence))

        # ✅ 增强：按归一化 value 去重（空白/全角差异不算矛盾）
        seen_values = set()
        unique = []
        for it in group:
            nv = _norm_value(it.value)
            if nv and nv not in seen_values:
                seen_values.add(nv)
                unique.append(it)

        if len(unique) == 1:
            # 无矛盾，直接取最优
            best = unique[0]
            best.has_conflict = False
            resolved.append(best)
        else:
            # 有矛盾：取归一化后最优值为事实值。
            # ✅ BUG 修复：候选值仅保留「其它取值」（排除事实自身），
            #    旧实现把事实自身也写进 conflict_values，导致前端把本值当作
            #    一条「备选值」渲染（自己与自己并列），既冗余又易误导裁决。
            best = unique[0]
            conflict_vals = [
                {"value": u.value, "source": u.source, "confidence": u.confidence,
                 # ✅ 候选值的模拟值语义一并带上（落库 / 前端回传 / 裁决重算共用）
                 "is_simulated": bool(u.is_simulated)}
                for u in unique[1:]
            ]
            best.has_conflict = True
            best.conflict_values = conflict_vals
            resolved.append(best)
    return resolved


def group_facts(items: list[FactItem]) -> list[FactGroup]:
    """按 category → title 双层分组（标题取自 CATEGORY_TITLES 单一事实源）"""
    groups_map: dict[str, FactGroup] = {}
    for it in items:
        cat = it.category or "other"
        if cat not in groups_map:
            groups_map[cat] = FactGroup(
                title=CATEGORY_TITLES.get(cat, cat),
                category=cat,
                items=[],
            )
        groups_map[cat].items.append(it)

    # 按预设顺序排列
    ordered_groups = []
    for cat in _CATEGORY_ORDER:
        if cat in groups_map:
            ordered_groups.append(groups_map[cat])
    # 补充未在预设中的 category
    for cat, grp in groups_map.items():
        if cat not in _CATEGORY_ORDER:
            ordered_groups.append(grp)

    return ordered_groups


# ---------------------------------------------------------------------------
# 主入口：完整提取管线
# ---------------------------------------------------------------------------


def _build_local_summary(chunks: list, max_chars: int = 800) -> str:
    """本地规则摘要：从前 N 段中抽取关键工程参数关键词的纯程序拼接。

    设计目标：给每段提取提供全局语义锚点（项目名称、地点、工期、地质、规模等），
    不追求自然语言流畅，只求关键参数覆盖。替代 AI 摘要调用，省 5-15s + token 成本。
    """
    keywords = [
        "项目名称", "工程名称", "建设地点", "项目地点",
        "总工期", "建设工期", "计划工期", "运维期", "保修期",
        "基坑深度", "开挖深度", "支护", "地下水位", "地质",
        "建筑面积", "占地面积", "层数", "结构形式",
        "混凝土", "钢筋", "塔吊", "挖掘机",
        "质量标准", "安全等级",
    ]
    hits: list[str] = []
    for chunk in chunks[:3]:
        chunk_text = chunk.text if hasattr(chunk, "text") else str(chunk)
        for line in chunk_text.split("\n"):
            line = line.strip()
            if 6 <= len(line) <= 120 and any(kw in line for kw in keywords):
                hits.append(line)
    seen = set()
    dedup = []
    for h in hits:
        key = re.sub(r'\s+', '', h)[:40]
        if key not in seen:
            seen.add(key)
            dedup.append(h)
    summary = "\n".join(dedup[:12])
    if len(summary) > max_chars:
        summary = summary[:max_chars]
    return summary


def _chunk_hash(text: str) -> str:
    """分段内容指纹（sha1 前 16 位）：增量提取「跳过已完成段」的判定键。

    指纹基于分段正文本身——文档重解析导致内容变化时指纹自然失配，
    对应段会被重新提取；文档删除后残留指纹由进度表清理逻辑移除。
    """
    import hashlib
    return hashlib.sha1((text or "").encode("utf-8", errors="ignore")).hexdigest()[:16]


async def run_extraction_pipeline(
    full_text: str,

    max_chunks: int = MAX_CHUNKS,
    progress_cb=None,
    should_stop=None,
    wait_resume_cb=None,
    missing_value_mode: str = "fabricate",
    completed_chunks: set | None = None,
    knowledge_text: str = "",
) -> ExtractionResult:
    """完整的事实提取管线（分段 → 合并 → 去重 → 分组）

    ✅ 新增进度/控制回调（均为可选，纯函数管线不依赖 FastAPI/SSE）：
    - progress_cb(p, message)：阶段与分段级进度回调（async），用于实时进度条
    - should_stop()：返回 True 时抛出 CancelledError，中止并保留已提取结果
    - wait_resume_cb()：暂停点，await 该回调实现分段间暂停
    - missing_value_mode：缺值模式 fabricate（默认，合理补全）/ omit（不杜撰）/
      placeholder（留【待填写】），对齐 OpenBidKit 全局事实三模式
    - completed_chunks：已完成段的指纹集合（增量提取）。二次提取时指纹命中的
      段直接跳过、不再发起 AI 调用（其事实已在上次提取落库），只提取
      新增/变更/上次失败的段，显著降低重复调用与限流失败
    - knowledge_text：项目知识库文本块（``build_knowledge_text`` 产物）。
      **仅当** ``settings.facts_knowledge_patch_enabled=True`` 时才消费
      （默认关闭 → 零新增 AI 调用）；开启后走易标 ``runKnowledgeGlobalFactPatches``
      的「只产补丁」补充阶段。
    """
    import time
    _t0 = time.perf_counter()
    result = ExtractionResult()


    # 分段区间占进度 0.10 → 0.72；后续程序阶段占 0.72 → 0.94
    _P_START, _P_END = 0.10, 0.72

    async def _emit(p: float, m: str) -> None:
        if progress_cb is None:
            return
        try:
            await progress_cb(p, m)
        except Exception as e:  # 进度回调异常绝不影响主流程
            logger.debug("progress_cb 异常（忽略）: %s", e)

    if not full_text or len(full_text.strip()) < 100:
        result.warnings.append("资料内容过短，无法提取有效事实")
        return result

    # 1. 分段
    all_items: list[FactItem] = []
    await _emit(0.03, "正在智能分段...")
    chunks = split_into_chunks(full_text)
    # ✅ BUG 修复（截断段指纹被误清理）：先固化【截断前】的文档完整指纹集，
    #    供 result.chunk_hashes_all 使用（save_extracted_chunks 的残留清理作用域）。
    #    旧实现把 chunk_hashes_all 设为【截断后保留段】，导致「因文档增长被优先级
    #    挤出窗口」的历史段：其事实仍在 global_facts 保留（persist 删除范围不含它），
    #    指纹却被当作残留删除 → 指纹与事实背离，下次该段重回窗口又要全量重抽
    #    （增量跳过对其失效）。真正的"残留"只应是文档删除/重解析后【不再存在】的段。
    doc_chunk_hashes = {_chunk_hash(c.text) for c in chunks}
    if len(chunks) > max_chunks:
        total_before = len(chunks)
        # ✅ BUG 修复：旧实现直接取【前 N 段】。招标文件的高价值结构化区
        #    （机械统计表 / 工程做法表 / 施工部署）通常位于中后部，按文档顺序
        #    截断会把它们整段丢弃，而前部往往是封面、目录、编制依据等低价值区
        #    （priority_weight 仅 0.9）→ 提取结果"有量无质"。
        #    现按语义区优先权重保留高价值段，再按原文档顺序还原，
        #    保证超限时长资料仍能覆盖最关键的结构化信息。
        ranked = sorted(range(len(chunks)),
                        key=lambda i: (-chunks[i].priority_weight, i))
        keep_idx = sorted(ranked[:max_chunks])
        dropped = total_before - len(keep_idx)
        chunks = [chunks[i] for i in keep_idx]
        logger.warning("分段数 %d 超过上限 %d，按语义区优先级保留 %d 段（丢弃 %d 段低价值区）",
                       total_before, max_chunks, len(chunks), dropped)
        # ✅ 增强：截断对用户可见，避免"后面资料没提取到"无从得知
        result.warnings.append(
            f"资料过长（共 {total_before} 段），本次按优先级保留 {max_chunks} 段"
            f"（已跳过 {dropped} 段低价值区，如封面/目录/规范引用），"
            f"建议拆分文件分批上传以获得完整覆盖")
        # ✅ 实时提示：不要等提取结束才让用户知道后半段没被处理
        await _emit(0.06,
                    f"⚠️ 资料过长（共 {total_before} 段），本次按优先级提取 {max_chunks} 段，"
                    f"建议拆分文件分批上传以完整覆盖")

    # 2. 生成全文摘要（用于增强每段提取的全局语义）
    # ✅ BUG-3 修复：本地规则摘要替代 AI 调用（省 5-15s + token 成本）。
    context_summary = ""
    if len(chunks) > 1:
        context_summary = _build_local_summary(chunks)
        if context_summary:
            logger.info("分段提取使用本地规则摘要（%d 字），跳过 AI 调用", len(context_summary))

    # 3. 分段并发提取
    # ✅ 增量提取（2026-09-17）：先算分段指纹，跳过已完成段——
    #    上次提取成功落库的段不再重复调用 AI（线上审计日志显示平均单次调用
    #    22.9s、失败率约 36%，主因 429 限流；跳过已完成段可直接省掉这部分）。
    completed = completed_chunks or set()
    chunk_hashes = [_chunk_hash(c.text) for c in chunks]
    # ✅ 残留清理作用域 = 文档完整段（含被优先级截断丢弃的段），见上方 doc_chunk_hashes 注释。
    #    本轮实际处理仍以下方 chunk_hashes（截断后保留段）为准，二者职责不同。
    result.chunk_hashes_all = doc_chunk_hashes
    skipped_idx = {i for i, h in enumerate(chunk_hashes) if h in completed}
    # ✅ BUG 修复：仅「实际重跑段」参与事实刷新删除范围（见 chunk_hashes_run 注释）
    result.chunk_hashes_run = {chunk_hashes[i]
                               for i in range(len(chunk_hashes))
                               if i not in skipped_idx}
    result.skipped_chunks = len(skipped_idx)
    if skipped_idx:
        logger.info("增量提取：跳过 %d/%d 段（上次已成功提取）",
                    len(skipped_idx), len(chunks))
    if chunks and len(skipped_idx) == len(chunks):
        # 全部段都已完成：无新增内容，直接成功返回（事实保持上次落库结果）
        result.chunk_hashes_ok = set(chunk_hashes)
        result.all_skipped = True
        result.segment_stats = {
            "total": len(chunks), "ok": 0, "skipped": len(skipped_idx),
            "failed": 0, "failed_details": [],
        }
        await _emit(
            0.9,
            f"全部 {len(chunks)} 段均已提取过，无新增内容（已跳过）；"
            "如需强制重新提取请使用「全部重新提取」")
        return result

    from app.services.ai.workflows_base import concurrency_controller
    _run_total = len(chunks) - len(skipped_idx)
    effective_concurrency = max(1, min(
        _run_total,
        concurrency_controller.current,
        FACTS_MAX_CONCURRENCY,
    ))
    logger.info("分段提取并发: %d (chunks=%d, skipped=%d, global=%d)",
                effective_concurrency, len(chunks), len(skipped_idx),
                concurrency_controller.current)
    await _emit(_P_START,
                f"已切分 {len(chunks)} 段（跳过 {len(skipped_idx)} 段已提取），"
                f"开始并发提取（并发 {effective_concurrency}）...")
    sem = asyncio.Semaphore(effective_concurrency)

    total_chunks = _run_total
    done_count = 0
    fail_count = 0
    acc_count = 0
    failed_details: list[dict] = []
    ok_idx: set[int] = set()
    _lock = asyncio.Lock()

    async def _chunk_worker(idx: int, chunk: Chunk):
        nonlocal done_count, fail_count, acc_count
        # ✅ 增量提取：已完成段直接跳过（其事实已在上次提取落库）
        if idx in skipped_idx:
            return []
        # 暂停点（分段间）+ 停止检查
        if wait_resume_cb is not None:
            await wait_resume_cb()
        if should_stop is not None and should_stop():
            raise asyncio.CancelledError()
        async with sem:
            if should_stop is not None and should_stop():
                raise asyncio.CancelledError()
            err: dict = {}
            try:
                items = await extract_from_single_chunk(
                    text=chunk.text,
                    context_summary=context_summary,
                    chunk_index=idx,
                    zone_type=chunk.zone_type,
                    priority_weight=chunk.priority_weight,
                    heading=chunk.heading,
                    error_out=err,
                    missing_value_mode=missing_value_mode,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("第 %d 段提取异常: %s", idx + 1, e)
                err["error"] = str(e)[:200]
                items = []
            # ✅ 增量提取：为每条事实盖来源分段指纹（merge 合并时保留代表项指纹）
            for _it in items:
                _it.chunk_hash = chunk_hashes[idx]
            async with _lock:
                done_count += 1
                # ✅ 语义修正：只有真正报错（err['error'] 存在）才算失败；
                # 封面/目录/规范引用等"合法无事实段"计为空段，不应虚增 failed
                # 导致前端误报"N 段失败，结果可能不完整"。
                if not items and err.get("error"):
                    fail_count += 1
                    failed_details.append({
                        "index": idx + 1,
                        "heading": (chunk.heading or "")[:60],
                        "zone_type": chunk.zone_type,
                        "reason": err["error"],
                        "preview": (chunk.text or "")[:60].replace("\n", " "),
                    })
                else:
                    # ✅ 增量提取：成功段（含合法空段）记为已完成，下次跳过
                    ok_idx.add(idx)
                acc_count += len(items)
                p = _P_START + (_P_END - _P_START) * done_count / max(total_chunks, 1)
                await _emit(
                    p,
                    f"已完成 {done_count}/{total_chunks} 段"
                    f"（本段 +{len(items)} 条，累计 {acc_count} 条，失败 {fail_count} 段）")
            return items

    tasks = [_chunk_worker(i, c) for i, c in enumerate(chunks)]
    chunk_results = await asyncio.gather(*tasks, return_exceptions=True)

    for res in chunk_results:
        # ✅ 注意：CancelledError 继承自 BaseException（非 Exception），
        #    gather(return_exceptions=True) 会把它当作"结果"返回，
        #    只判 isinstance(res, Exception) 会让它落到 all_items.extend() 崩溃。
        if isinstance(res, BaseException):
            if isinstance(res, asyncio.CancelledError):
                raise res  # 用户停止 → 交由上层按"已停止"处理
            logger.warning("分段提取异常: %s", res)
            continue
        all_items.extend(res)

    # ✅ 分段级诊断（无论成功/部分/全部失败都记录，供接口与前端展示）
    result.chunk_hashes_ok = {chunk_hashes[i] for i in ok_idx} | set(
        chunk_hashes[i] for i in skipped_idx)
    result.segment_stats = {
        "total": len(chunks),
        "ok": max(_run_total - fail_count, 0),
        "skipped": len(skipped_idx),
        "failed": fail_count,
        "failed_details": failed_details[:10],
    }
    _fail_reasons = "；".join(
        dict.fromkeys(d["reason"] for d in failed_details[:3]))  # 去重保序

    logger.info(
        "分段提取完成：共 %d 条原始事实（失败 %d/%d 段，跳过 %d 段）%s",
        len(all_items), fail_count, _run_total, len(skipped_idx),
        f" 原因: {_fail_reasons}" if _fail_reasons else "")

    if not all_items:
        if _run_total and fail_count >= _run_total:
            # ✅ 全部段提取失败：给出实际原因（超时/限流/密钥/网络），
            #    而不是笼统的"AI 未提取到任何事实"。
            result.warnings.append(
                f"全部 {total_chunks} 段提取失败："
                f"{_fail_reasons or '请检查模型配置（限流/密钥/网络）后重试'}")
        else:
            result.warnings.append("AI 未提取到任何事实，请检查资料内容或模型配置")
        return result

    if fail_count:
        if fail_count == total_chunks:
            result.warnings.append(
                f"全部 {total_chunks} 段提取失败："
                f"{_fail_reasons or '请检查模型配置（限流/密钥/网络）后重试'}")
        else:
            # ✅ 部分失败显式告警（带实际原因）：避免用户以为"提取完成"却漏了段落
            result.warnings.append(
                f"{fail_count}/{total_chunks} 段提取失败，结果可能不完整："
                f"{_fail_reasons or '多为限流或超时'}"
                f"（建议稍后重新提取）")

    # 4. 归一化（报告补充①：与 Prompt 共用 _norm_dicts 单一事实源）
    await _emit(_P_END + 0.02, "正在归一化名称与规格...")
    apply_norm_dicts(all_items)

    # 5. 合并去重与矛盾检测
    await _emit(_P_END + 0.06, f"正在合并去重与矛盾检测（原始 {len(all_items)} 条）...")
    merged = merge_and_deduplicate(all_items)

    # ✅ 移植增强：后处理（category 自动分类 → 启发式兜底 → 必须有工期规则）
    merged = run_post_extract_normalize(merged, fallback_text=full_text[:3000])

    # ✅ 缺值模式后处理（对齐 OpenBidKit 全局事实三模式，程序级保障 ——
    #    Prompt 约束可能被弱模型忽略，这里按模式做确定性兜底）：
    #    值域走 facts_patches 单一出口（2026-09-30 第十三轮），与 SSE 入口同源。
    _mode = normalize_missing_value_mode(missing_value_mode)
    if _mode == "omit":
        _before = len(merged)
        merged = [it for it in merged if not it.is_simulated]
        _dropped = _before - len(merged)
        if _dropped:
            result.warnings.append(
                f"缺值模式「不杜撰」：已剔除 {_dropped} 条资料未给出的模拟值"
                f"（正文涉及时请改写为笼统承诺）")
    elif _mode == "placeholder":
        _ph = 0
        for it in merged:
            if it.is_simulated and "待填写" not in _as_text(it.value):
                it.value = "【待填写】"
                _ph += 1
        if _ph:
            result.warnings.append(
                f"缺值模式「留待填写」：{_ph} 条资料未给出的值已置为【待填写】"
                f"（请人工补齐后再生成正文）")

    # 5.5 知识库补充（对齐易标 runKnowledgeGlobalFactPatches，:876-891）
    #    位置在缺值模式后处理**之后**：知识库给的是已确定的具体值，不该被
    #    omit/placeholder 模式当成「模拟值」剔除或改写成【待填写】。
    #    默认关闭（settings.facts_knowledge_patch_enabled=False）→ 零新增 AI 调用。
    if settings.facts_knowledge_patch_enabled and knowledge_text.strip() and merged:
        await _emit(_P_END + 0.10, "正在用项目知识库补充事实...")
        try:
            from app.services.facts_enrich import apply_knowledge_patches
            merged, kb_n = await apply_knowledge_patches(merged, knowledge_text)
            if kb_n:
                result.warnings.append(f"知识库补充了 {kb_n} 条事实（已合并）")
        except Exception as e:  # noqa: BLE001 - fail-soft：绝不中断已成功的提取
            logger.warning("知识库补充阶段异常（已跳过）: %s", e, exc_info=True)
            result.warnings.append("知识库补充阶段异常，本次未应用知识库补丁")

    # 5.6 最终整理（对齐易标 finalizeGlobalFacts，:909-920）
    #    必须在缺值模式与知识库补充**之后** —— 整理要改写「要求句 → 事实句」，
    #    若先整理再走缺值模式，整理产出的新表述会被误判为「资料未给出」。
    #    默认关闭（settings.facts_finalize_enabled=False）→ 零新增 AI 调用。
    if settings.facts_finalize_enabled and merged:
        await _emit(_P_END + 0.11, "正在最终整理事实（去重与口径统一）...")
        try:
            from app.services.facts_enrich import finalize_facts
            merged, fin_n = await finalize_facts(merged)
            if fin_n:
                result.warnings.append(f"最终整理改写了 {fin_n} 条事实表述")
        except Exception as e:  # noqa: BLE001 - fail-soft
            logger.warning("最终整理阶段异常（已跳过）: %s", e, exc_info=True)
            result.warnings.append("最终整理阶段异常，本次保留整理前结果")

    # 6. 交叉校验（报告补充②：三组纯程序规则，零 LLM 成本先行判定）
    await _emit(_P_END + 0.12, f"正在交叉校验（去重后 {len(merged)} 条）...")
    try:
        cross_conflicts = run_cross_validations(merged)
        if cross_conflicts:
            result.cross_conflicts = cross_conflicts
            high_n = sum(1 for c in cross_conflicts if c["severity"] == "high")
            result.warnings.append(
                f"交叉校验发现 {len(cross_conflicts)} 处跨维度矛盾"
                f"（高危 {high_n} 处），请人工裁决")
    except Exception as e:
        logger.warning("交叉校验异常（不影响主流程）: %s", e)

    # 7. 归一化 category（基于安全关键和常规分类）
    # ✅ BUG 修复：安全关键项禁止模拟。旧实现对 `_is_safety_critical` 命中项
    #    无条件提升为 safety_critical，会把"必须有工期"规则注入的模拟兜底
    #    工期也标成安全关键，与"17 类禁止模拟"规则自相矛盾。
    #    现：模拟项强制撤销安全关键标记，只有真实提取值才提升。
    await _emit(_P_END + 0.16, "正在归类安全关键参数...")
    for it in merged:
        if it.is_simulated:
            it.is_safety_critical = False
            continue
        if _is_safety_critical(it):
            it.is_safety_critical = True
            if it.category != "safety_critical":
                it.category = "safety_critical"

    # 8. 分组
    await _emit(_P_END + 0.20, "正在按类别分组...")
    result.groups = group_facts(merged)
    result.total_items = len(merged)
    result.simulated_count = sum(1 for it in merged if it.is_simulated)
    result.conflict_count = sum(1 for it in merged if it.has_conflict)
    await _emit(
        _P_END + 0.22,
        f"提取完成：{len(result.groups)} 组 / {result.total_items} 条事实"
        f"（模拟 {result.simulated_count} 条，矛盾 {result.conflict_count} 条）")

    # 7. 模拟值占比告警
    if result.total_items > 0:
        ratio = result.simulated_count / result.total_items
        if ratio > SIMULATED_RATIO_WARN:
            result.warnings.append(
                f"模拟值占比 {ratio:.0%} 超过 {SIMULATED_RATIO_WARN:.0%} 阈值，"
                f"建议补充更多项目资料")
        if result.conflict_count > 0:
            result.warnings.append(
                f"检测到 {result.conflict_count} 条事实存在矛盾，"
                f"请在审核界面人工裁决")

    logger.info("事实提取管线完成：%.1fs，%d 段 → %d 条事实",
                time.perf_counter() - _t0, len(chunks), result.total_items)
    return result


# =========================================================================
# ✅ 从招投标方案平台移植：值规范化后处理 + 启发式兜底 + category 分类器
# =========================================================================

# 时间类事实的 category 集合（用于"必须有工期"规则校验）
_SCHEDULE_CATEGORIES = {"schedule", "duration"}
_SCHEDULE_KEY_TAGS = (
    "duration", "schedule", "工期", "运维", "保修", "质保",
    "交货", "delivery", "warranty", "service_period",
)


def apply_category_auto_classify(items: list[FactItem]) -> None:
    """tag→category 自动分类器（AI 未明确归类时的确定性兜底）。

    当 AI 返回的 category 缺失或为 other 时，根据 key/name 的关键词自动纠正。
    关键词按"专指 → 泛指"排列，专指类别先命中，避免被泛化词误吞。
    """
    # (类别, 关键词元组)，顺序即优先级（专指 → 泛指）
    # ✅ BUG 修复：
    #   1. machinery 旧规则含泛化词「数量」→ "监测点数量"/"材料数量"/"检验批数量"
    #      等被全部误归为机械统计（只要排在其后的规则没先命中）。现删除泛化词，
    #      改由专指机械名（塔吊/挖掘机/…）命中。
    #   2. equipment 旧规则含 "tech"/"材料"/"工艺" → "tech" 会命中任何英文 key 中
    #      含 "tech" 的片段，"材料" 会吞掉材料管理类事实。现拆出独立的
    #      material_mgmt 规则并置于 equipment 之前，equipment 只保留设备/参数/规格。
    rules: list[tuple[str, tuple[str, ...]]] = [
        ("basis", ("编制依据", "依据规范", "执行标准", "标准规范", "法律法规",
                   "技术规程", "design_code", "standard")),
        ("monitoring", ("监测", "报警值", "控制值", "测点", "观测", "monitoring")),
        ("emergency", ("应急", "预案", "抢险", "emergency")),
        ("acceptance", ("验收", "检验批", "acceptance")),
        ("labor", ("劳动力", "工种", "用工", "高峰人数", "labor")),
        ("temporary", ("临时用电", "临时用水", "临时道路", "临时设施", "临建",
                       "temporary")),
        ("environment", ("环保", "扬尘", "噪声", "污水", "文明施工", "绿色施工",
                         "environment")),
        ("risk", ("危险源", "危大", "风险等级", "风险辨识", "风险管控", "risk")),
        ("schedule", _SCHEDULE_KEY_TAGS),
        ("commitment", ("commit", "承诺", "service", "响应", "售后")),
        ("quality", ("quality", "质量")),
        ("personnel", ("person", "manager", "负责人", "项目经理", "安全员",
                       "质量员", "人员配置", "组织机构")),
        ("material_mgmt", ("材料", "混凝土", "钢筋", "砂浆", "水泥", "防水材料",
                           "保温材料", "周转材料", "material")),
        ("machinery", ("机械", "挖掘机", "起重机", "塔吊", "装载机", "推土机",
                       "压路机", "发电机", "泵车", "自卸车", "施工电梯",
                       "machinery")),
        ("equipment", ("equipment", "设备", "技术参数", "规格型号", "参数", "规格")),
        ("execution", ("implement", "施工", "方法", "组织", "实施")),
        ("safety_critical", ("safety", "安全")),
    ]
    for it in items:
        if it.category and it.category not in ("other", ""):
            continue  # AI 已明确归类时不覆盖
        k = (it.key or "").lower() + " " + (it.name or "")
        for cat, keywords in rules:
            if any(t in k for t in keywords):
                it.category = cat
                break


def _extract_schedule_value(text: str) -> str:
    """从文本中启发式提取工期/运维期/交货时间值（来自招投标平台 facts.py L287-L299）"""
    if not text:
        return ""
    m = re.search(r"(?:工期|建设工期|总工期|计划工期)[^，。；\n]{0,10}?([\d.]+)\s*(日历)?天", text)
    if m:
        return f"{m.group(1)} 日历天"
    m = re.search(r"(?:运维期|保修期|质保期|服务期)[^，。；\n]{0,8}?([\d.]+)\s*年", text)
    if m:
        return f"{m.group(1)} 年"
    return ""


def ensure_schedule_fact(items: list[FactItem], fallback_text: str = "") -> None:
    """✅ 移植增强：必须有工期规则。

    招投标平台 facts.py L258-L282 的核心规则：全局事实中必须存在至少一个
    时间类变量（工期/运维期/交货时间），否则从文本启发式提取或降级默认值。
    这保证了正文生成中始终有工期可用，避免 AI 随机编造。
    """
    has_schedule = any(
        (it.category in _SCHEDULE_CATEGORIES)
        or any(tag in (it.key or "").lower() + (it.name or "")
               for tag in _SCHEDULE_KEY_TAGS)
        for it in items
    )
    if has_schedule:
        return

    # 先从 fallback 文本启发式提取
    val = _extract_schedule_value(fallback_text)
    if val:
        items.append(FactItem(
            name="建设工期", value=val,
            key="total_duration", category="schedule",
            source="heuristic", source_ref="启发式规则提取",
            is_simulated=False, confidence=0.85,
        ))
    else:
        # 降级：给一个安全默认值，标记为模拟
        items.append(FactItem(
            name="建设工期",
            value="按合同约定工期执行",
            key="total_duration", category="schedule",
            source="default", source_ref="无工期信息，默认值",
            is_simulated=True, confidence=0.3,
        ))
        logger.info("未提取到工期，注入默认值保证完整性")


def apply_heuristic_fallback(
    all_items: list[FactItem],
    fallback_text: str = "",
) -> list[FactItem]:
    """✅ 移植增强：启发式规则兜底（AI 完全空时）。

    当 AI 分段提取全部返回空（模型故障/资料过短）时，用正则规则从文本
    中提取最关键的基本事实（项目名称、地点、工期、质量要求）。

    ✅ 修复（2026-09-24，治 A 类供给噪音）：正则也一无所获时**不再伪造
    「项目名称=待补充」**——空事实库是合法状态（上游管线已有显式告警，
    正文侧按三级策略自行标注【待补充：字段名】），伪造的模拟值只会随
    事实文本流回提示词、诱导 AI 原样照抄「待补充」。
    """
    if all_items:
        return all_items  # AI 有结果时不兜底

    logger.warning("AI 未提取到任何事实，启用启发式规则兜底")
    fallback_text = fallback_text or ""
    default_items: list[FactItem] = []

    # 项目名称
    m = re.search(r"(?:工程名称|项目名称)\s*[:：]\s*([^\n*]{2,30})", fallback_text)
    if not m:
        m = re.search(r"项目名称.*?[:：]\s*(.+)", fallback_text)
    if m:
        default_items.append(FactItem(
            name="项目名称", value=m.group(1).strip(),
            key="project_name", category="basic",
            source="heuristic", confidence=0.85,
        ))

    # 项目地点
    locs = re.findall(r"(?:建设地点|项目地点|位于)[:：]\s*([^\n*]{2,30})", fallback_text)
    if locs:
        default_items.append(FactItem(
            name="建设地点", value=locs[0].strip(),
            key="project_location", category="basic",
            source="heuristic", confidence=0.85,
        ))

    # 工期
    duration = _extract_schedule_value(fallback_text)
    if duration:
        default_items.append(FactItem(
            name="建设工期", value=duration,
            key="total_duration", category="schedule",
            source="heuristic", confidence=0.85,
        ))

    if not default_items:
        # ✅ 修复（2026-09-24）：极端兜底从「伪造 项目名称=待补充」改为如实
        #    返回空清单（omit 语义）。旧实现注入的模拟值（source="default"、
        #    value="待补充"）会进入 _render_facts_text 的事实文本，成为正文
        #    【待补充】噪音的源头之一；且与缺值模式 omit/placeholder 的
        #    程序级保障（1618-1638 行）口径不一致。空清单上游已告警兜底，
        #    此处静默返回即可，绝不编造。
        logger.warning(
            "启发式规则未从资料文本中提取到任何基本事实，返回空清单"
            "（不再注入模拟占位值，正文侧将按三级策略标注【待补充】）")

    return default_items


def run_post_extract_normalize(
    merged_items: list[FactItem],
    fallback_text: str = "",
) -> list[FactItem]:
    """✅ 移植增强：统一后处理入口（值规范化→工期强制→category 自动分类→启发式兜底）。

    对应招投标平台 _normalize_facts() 的完整能力。
    """
    # 1. category 自动分类
    apply_category_auto_classify(merged_items)

    # 2. 启发式兜底（AI 完全空时）
    merged_items = apply_heuristic_fallback(merged_items, fallback_text)

    # 3. 必须有工期
    ensure_schedule_fact(merged_items, fallback_text)

    # 4. ✅ 2026-09-24：九大章节四维标注（chapter/fact_attr/source_kind/is_shared）。
    #    依据建办质〔2018〕31号，专项方案分九大章节；既有 22 类 category 是
    #    「事实类型」视角，与九大章节并非一对一（如 monitoring/risk/environment
    #    均属第五章，calc_drawings 在既有 category 中根本没有落点）。
    #    标注必须在 category 自动分类与启发式兜底**之后**执行——两者是章节
    #    推导的输入，且兜底可能新增事实（须一并标注）。
    #    确定性规则派生，无 AI、无 DB 往返；已标注的值不覆盖（保留人工归类）。
    if settings.facts_chapter_classification:
        apply_fact_dimensions(merged_items)

    return merged_items


# ---------------------------------------------------------------------------
# SSE 事件推送辅助
# ---------------------------------------------------------------------------

def _display_value(it: FactItem) -> str:
    """事实值的展示文本：序列 → 顿号连接，追加单位（供 Markdown 与前端共用）"""
    val = _as_text(it.value)
    if it.value_unit and it.value_unit != "null":
        val = f"{val} {it.value_unit}"
    return val


def format_for_frontend(result: ExtractionResult) -> dict[str, Any]:
    """将提取结果转为前端友好的 JSON 结构"""
    flat_groups = []
    for grp in result.groups:
        # ✅ BUG 修复：优先使用 persist_extraction 分配的最终 group_id，
        # 保证 completed 事件数据与 DB 分组一致（前端编辑不再 404）。
        # 未持久化的场景（纯预览）才回退到临时 uuid。
        gid = grp.persisted_group_id or str(uuid.uuid4())
        # 生成 Markdown content（保留模拟值标记）
        lines = []
        for it in grp.items:
            lines.append(f"- **{it.name}**: {append_simulated_marker(_display_value(it), it.is_simulated)}")
        content_md = "\n".join(lines)

        flat_groups.append({
            "id": gid,
            "title": grp.title,
            "category": grp.category,
            "content": content_md,
            "items": [
                {
                    "name": it.name,
                    # ✅ 序列类值转字符串下发（前端直接渲染，避免 React 渲染数组报错）
                    "value": _display_value(it),
                    "value_unit": it.value_unit,
                    "key": it.key,
                    "fact_key": it.key,
                    "fact_type": it.fact_type,
                    "category": it.category,
                    "source": it.source,
                    "source_ref": it.source_ref,
                    "evidence_kind": it.evidence_kind,
                    "page_ref": it.page_ref,
                    # ✅ 信息调用完整性（2026-09-23）：补齐 SSE 下发路径，与 list_facts
                    #    刷新路径保持一致，避免「流式有、刷新无」的字段漂移。
                    "zone_type": it.zone_type,
                    "norm_group": it.norm_group,
                    "chunk_hash": it.chunk_hash,
                    # ✅ 2026-09-24：九大章节四维标注随 SSE 下发（与 list_facts
                    #    刷新路径保持一致，避免「流式有、刷新无」的字段漂移）。
                    "chapter": it.chapter or "",
                    "fact_attr": it.fact_attr or "",
                    "source_kind": it.source_kind or "",
                    "is_shared": bool(it.is_shared),
                    "is_simulated": it.is_simulated,
                    "is_safety_critical": it.is_safety_critical,
                    "confidence": it.confidence,
                    "has_conflict": it.has_conflict,
                    # ✅ 2026-09-26（数据一致性）：补齐 scope / is_stale，与
                    #    list_facts 刷新路径保持一致。SSE 提取事实恒为方案级且
                    #    刚产出非过期，故 scope="scheme"、is_stale=False；避免
                    #    任何直接消费 SSE 数据的客户端出现「项目共享」/「过期」
                    #    徽标丢失（前端收口后虽会 loadFacts 刷新，但保持两路径
                    #    字段同构可防止未来消费方遗漏）。
                    "scope": "scheme",
                    "is_stale": False,
                    "conflict_values": [
                        {"value": _as_text(c.get("value")),
                         "source": c.get("source", ""),
                         # ✅ BUG 修复（一致性）：DB 落库路径（to_db_row）已用
                         #    _safe_confidence 收敛脏值，此处 SSE 回传路径却直传，
                         #    跨条目回写的候选值置信度若为 "high"/越界值，前端进度条
                         #    会渲染异常。两路径统一口径。
                         "confidence": _safe_confidence(c.get("confidence"), 1.0),
                         # ✅ 候选值模拟值语义随 SSE 下发（与 to_db_row 落库口径一致）
                         "is_simulated": bool(c.get("is_simulated"))}
                        for c in (it.conflict_values or [])
                    ],
                }
                for it in grp.items
            ],
        })

    return {
        "ok": True,
        "groups": flat_groups,
        "total_items": result.total_items,
        "simulated_count": result.simulated_count,
        "conflict_count": result.conflict_count,
        "cross_conflicts": result.cross_conflicts,
        "segment_stats": result.segment_stats,
        "warnings": result.warnings,
    }


# ---------------------------------------------------------------------------
# 工具函数：将提取结果持久化到数据库
# ---------------------------------------------------------------------------

def resolve_fact_is_resolved(item: "FactItem", auto_resolve: bool) -> int:
    """计算某条提取事实落库时的 is_resolved 值（信息调用完整性门控）。

    - 默认闸门：返回 0（待审核）。提取结果需经人工确认后，门控
      ``has_conflict=0 AND is_resolved=1`` 才会把它注入 目录/正文/导出。
    - 开启 ``auto_resolve``（配置 ``AUTO_RESOLVE_EXTRACTED_FACTS``）后，对
      **非模拟值、无矛盾** 的事实自动置 1，使其无需人工确认即进入生成链路；
      模拟值（is_simulated）与矛盾值（has_conflict）仍保持 0，安全闸门对
      高风险项依然生效。
    纯函数，便于单测。
    """
    if auto_resolve and not item.is_simulated and not item.has_conflict:
        return 1
    return 0


async def persist_extraction(
    result: ExtractionResult,
    db,
    project_id: str,
    scheme_id: str,
) -> None:
    """将提取结果写入 global_facts 表（原子事务：DELETE+INSERT 同事务）

    ✅ 加固：显式事务包裹 DELETE+INSERT，INSERT 失败时 rollback 保留旧数据。
    此前依赖调用方的零事实守卫，但 DB 层约束冲突/连接中断等异常仍可能
    在 DELETE 已执行、INSERT 未完成时发生，导致全部事实丢失。
    """
    # ✅【增强·信息保存】增量合并持久化：
    #   旧实现每次提取都是 DELETE 全量 + INSERT，会【清空用户已确认(已审核)
    #   的事实与手动新增的事实】——用户辛苦审核/手填的数据在「重新提取」后全部丢失。
    #   现改为：仅删除「未确认(is_resolved=0)且非手动来源」的旧事实，手动新增与
    #   已确认事实原样保留，新 AI 事实仅追加；若新事实的 fact_key 命中已确认事实，
    #   则跳过（以用户确认为准，不重复）。
    #   仍保持 DELETE+INSERT 在同一事务，INSERT 失败 rollback 保留旧数据。

    # 1) 加载同作用域现有事实
    if scheme_id:
        scope_sql = "SELECT * FROM global_facts WHERE scheme_id=?"
        scope_params = (scheme_id,)
    else:
        scope_sql = ("SELECT * FROM global_facts WHERE project_id=? "
                     "AND (scheme_id='' OR scheme_id IS NULL)")
        scope_params = (project_id,)
    cur = await db.execute(scope_sql, scope_params)
    existing = [dict(r) for r in await cur.fetchall()]

    def _is_protected(row: dict) -> bool:
        # 已人工确认(已审核) → 保留
        if row.get("is_resolved"):
            return True
        # ✅ BUG 修复：global_facts 表并无 source 列（来源存于 source_ref 的
        #    JSON [{"file":..,"quote":..}]），旧实现读 row["source"] 恒为空，
        #    手动录入但未确认(is_resolved=0)的事实会被「重新提取」删除。
        #    现解析 source_ref 判定，手动来源一律保护。
        raw = row.get("source_ref") or ""
        if raw:
            try:
                refs = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                refs = [{"file": raw}]
            if isinstance(refs, dict):
                refs = [refs]
            for ref in refs if isinstance(refs, list) else []:
                fname = str((ref or {}).get("file") or "").strip()
                # ✅ 精确白名单：旧实现的 `"手动" in fname` 子串判定会把 AI 溯源
                # 标题含"手动"的事实（如"手动操作步骤""手动计算"）误判为手动来源，
                # 导致其被永久保护、重新提取时同 key 重复堆积。
                if fname in ("手动录入", "manual", "手动", "Manual", "MANUAL"):
                    return True
        return False

    protected = [r for r in existing if _is_protected(r)]
    # 只把已确认事实用于“用户确认为准”的去重；手动但未确认的事实仍需保留，
    # 但不能阻止新资料进入冲突候选，否则人工录入后重新提取会静默吞掉新证据。
    protected_by_key = {
        r.get("fact_key"): r for r in protected
        if r.get("fact_key") and r.get("is_resolved")
    }

    # ✅ BUG 修复：同一分类只应有一个分组卡片。旧实现对每个新 AI 分组都生成
    #    全新的 group_id，而已确认/手动事实保留了上一次提取的 group_id——
    #     「重新提取」后，同一分类会同时存在「旧 group_id（已确认子集）」与
    #    「新 group_id（新 AI 子集）」两条记录，list_facts 按 group_id 聚合会
    #    渲染成两个同名分组卡片（如两个"人员角色"）。
    #    现复用既有 group_id：优先复用已保护分组的 group_id（与用户已确认事实
    #    同组），否则复用该分类任意既有 group_id，皆无则新建。这样无论提取
    #    多少次，每个分类始终只有一个分组，且编辑/删除分组能覆盖全部分类事实。
    protected_group_by_cat: dict[str, str] = {}
    for r in protected:
        cat = (r.get("category") or "other") or "other"
        if cat not in protected_group_by_cat:
            protected_group_by_cat[cat] = r.get("group_id") or r.get("id")
    existing_group_by_cat: dict[str, str] = {}
    for r in existing:
        cat = (r.get("category") or "other") or "other"
        if cat not in existing_group_by_cat:
            existing_group_by_cat[cat] = r.get("group_id") or r.get("id")

    # 2) 新 AI 事实（跳过与已确认事实同 key 的，以用户确认为准）
    insert_buf = []
    skipped_dup = 0
    cat_group_id_cache: dict[str, str] = {}
    for grp in result.groups:
        cat = (grp.category or "other") or "other"
        group_id = (
            cat_group_id_cache.get(cat)
            or protected_group_by_cat.get(cat)
            or existing_group_by_cat.get(cat)
            or str(uuid.uuid4())
        )
        cat_group_id_cache[cat] = group_id
        # ✅ 回写最终 group_id：供 format_for_frontend 输出与 DB 一致的分组 id
        # （前端拿 completed 事件数据编辑分组时按此 id 匹配，不再 404）
        grp.persisted_group_id = group_id
        for it in grp.items:
            if it.key and it.key in protected_by_key:
                prior = protected_by_key[it.key]
                # 同一键但值不同：不要静默丢弃新证据，回写到已确认行的冲突候选。
                # 当前值仍以用户已确认值为准，待用户在界面人工裁决。
                prior_content = str(prior.get("content") or "")
                prior_value = extract_value_from_markdown_line(prior_content)[1]
                new_value = _as_text(it.value)
                if prior_value.strip() and new_value.strip() and \
                        normalize_key(prior_value) != normalize_key(new_value) and \
                        prior_value.strip() != new_value.strip():
                    try:
                        candidates = json.loads(prior.get("conflict_keys") or "[]")
                    except (json.JSONDecodeError, TypeError):
                        candidates = []
                    if not isinstance(candidates, list):
                        candidates = []
                    if not any(_as_text(c.get("value")) == new_value
                               for c in candidates if isinstance(c, dict)):
                        candidates.append({
                            "value": it.value,
                            "source": it.source,
                            "confidence": _safe_confidence(it.confidence, 1.0),
                            # ✅ 候选值自带模拟值语义随证据落库：resolve_conflict
                            #    按值裁决时据此重算行的 is_simulated。
                            "is_simulated": bool(it.is_simulated),
                        })
                    # ✅ BUG-3 修复：用户已确认的事实在「重新提取」时不得被静默打回
                    #    「待审核」（否则会脱离注入/导出门控 is_resolved=1，等于
                    #    「重新提取丢了已审事实」）。
                    #    现保留用户的确认状态：is_resolved 不变，仅把新证据追加进
                    #    conflict_keys 供用户在界面人工裁决。
                    #    注：同时置 is_stale=1 与 has_conflict=1 —— 门控
                    #    （has_conflict=0 AND is_resolved=1 AND is_simulated=0
                    #    AND is_stale=0）本就把「未裁决的矛盾值」排除在注入之外，
                    #    故此处 stale 是**冗余但正确**的加固：新证据出现后该值在人工
                    #    裁决前一律不进正文/导出（回归护栏见
                    #    tests/test_facts_incremental.py 与
                    #    tests/test_facts_inject_gate_failclosed_20260927.py）。
                    await db.execute(
                        "UPDATE global_facts SET conflict_keys=?, has_conflict=1, "
                        "is_stale=1, updated_at=datetime('now','localtime') WHERE id=?",
                        (json.dumps(candidates, ensure_ascii=False), prior["id"]))
                else:
                    # 同 key 同值：本次资料已再次证明该已确认事实，可解除来源过期标记。
                    await db.execute(
                        "UPDATE global_facts SET is_stale=0, "
                        "updated_at=datetime('now','localtime') WHERE id=?",
                        (prior["id"],))
                skipped_dup += 1
                continue
            row = list(it.to_db_row(group_id, project_id, scheme_id, grp.title))
            # ✅ 2026-09-23（信息调用完整性，配置 AUTO_RESOLVE_EXTRACTED_FACTS）：
            #    提取事实默认 is_resolved=0（待审核），门控 `has_conflict=0 AND
            #    is_resolved=1` 会将其排除在 目录/正文/导出 注入之外，导致「提取完成」
            #    后若不手动逐条确认，结构化事实对下游生成完全不可见（有效信息未被
            #    完整调用）。开启开关后，对「非模拟值、无矛盾」的事实在落库时自动置
            #    is_resolved=1，使其无需人工确认即可进入生成链路；模拟值与矛盾值仍
            #    保持待审核，安全闸门对高风险项依然生效。默认 False = 向后兼容。
            #    row[11] 即 is_resolved 列（见 FactItem.to_db_row）。
            row[11] = resolve_fact_is_resolved(it, settings.auto_resolve_extracted_facts)
            insert_buf.append(tuple(row))

    # 3) 仅删除「未保护(未确认/旧 AI) 且 所属分类在本次提取结果中」的行，
    #    保留已确认与手动事实，并保留「本次结果未覆盖分类」下的未确认事实。
    #    ✅ 数据丢失防护（修复）：旧实现无条件删除全部未确认/旧 AI 事实，
    #    一旦本次 AI 提取结果漏掉某个分类（段落失败 / 模型方差 / 分批提取 /
    #    仅针对单分类增量提取），该分类下所有未确认事实会被静默清空且不再补回，
    #    造成不可逆数据丢失。现把删除范围收窄到「本次结果覆盖到的分类」：
    #    未被本次结果覆盖的分类其事实一律保留，等该分类被再次提取时再刷新。
    #    已确认 / 手动事实始终保留。
    new_categories = {(grp.category or "other") or "other"
                      for grp in result.groups}
    # ✅ 跨分类漂移：自动分类器在两次提取间可能把同一 fact_key 归到不同分类
    # （如 schedule→basic）。仅按 category 收窄删除范围会留下旧分类下的未确认
    # 旧行，新行照插 → 列表出现重复事实。同 key 未保护行一并纳入刷新范围。
    new_keys = {it.key for grp in result.groups for it in grp.items if it.key}
    # ✅ 增量提取（2026-09-17）：跳过段的事实行（chunk_hash 命中「已完成指纹」
    #    且不在本次重提取集合中）不在删除范围内 —— 上次段未重跑，其未确认事实
    #    必须原样保留；chunk_hash 为空（手动/旧数据）或命中本次分段集合的行
    #    仍按原口径刷新。
    #    ✅ BUG 修复（2026-09-18）：这里必须用 `chunk_hashes_run`（本次**实际重跑**
    #    的段），而不是 `chunk_hashes_all`（含被增量跳过的段）。用 all 时，只要跳过
    #    段的分类与本次重跑结果的分类相同，跳过段的未确认事实就会被 DELETE，而该段
    #    本轮没有重跑、不会重新产出 → 事实永久丢失（进度表仍记其已完成，下次继续跳过）。
    #    未提供 run 的调用方（历史/测试直接构造）回退为 all，保持原语义。
    _run = result.chunk_hashes_run or result.chunk_hashes_all
    run_hashes = {str(h) for h in (_run or set()) if h}
    # ✅ 2026-09-28（重复/过期事实清理）：
    #   `all_hashes` = 当前文档的**全量**分段指纹（含被优先级截断的低价值段，
    #   见管线 doc_chunk_hashes 注释：截断前的完整段集）。旧实现只删除
    #   「空指纹 / 本次重跑段指纹」的非保护行 —— 当文档内容变化使分段指纹
    #   漂移时（重新解析 / 资料更新），旧指纹既不在 run_hashes 也不在
    #   all_hashes，该行既不被清除、也不被刷新，与本次新插入的事实**同 key
    #   重复堆积**（界面出现多条同名未确认事实，一次提取比一次多）。
    #   现追加「指纹已不在当前文档全量分段集合 → 源头已消失 → 立即清除」；
    #   仍在 all_hashes 但被增量跳过的段（skipped）**保持保留**（与
    #   test_persist_keeps_skipped_chunk_rows 的口径一致）。all_hashes 为空时
    #   （历史/测试调用方未提供）跳过该分支，行为与旧版完全一致。
    all_hashes = {str(h) for h in (result.chunk_hashes_all or set()) if h}
    if new_categories:
        non_protected_ids = [
            r["id"] for r in existing
            if (not _is_protected(r))
            and (((r.get("category") or "other") or "other") in new_categories
                 or (r.get("fact_key") and r.get("fact_key") in new_keys))
            and ((not (r.get("chunk_hash") or ""))
                 or (r.get("chunk_hash") in run_hashes)
                 or (all_hashes and (r.get("chunk_hash") not in all_hashes)))
        ]
    else:
        # 本次结果为空（理论上 SSE 层已拦截，这里兜底）：不删除任何既有事实
        non_protected_ids = []
    try:
        if non_protected_ids:
            ph = ",".join("?" * len(non_protected_ids))
            await db.execute(
                f"DELETE FROM global_facts WHERE id IN ({ph})",
                non_protected_ids)
        if insert_buf:
            await db.executemany(
                "INSERT INTO global_facts "
                "(id, project_id, scheme_id, group_id, group_title, title, content, "
                "category, source_ref, is_simulated, confidence, "
                "is_resolved, has_conflict, conflict_keys, fact_key, chunk_hash, "
                "value_unit, fact_type, evidence_kind, page_ref, zone_type, "
                "is_safety_critical, norm_group, "
                # ✅ 2026-09-24：九大章节四维标注（正交于 category）
                "chapter, fact_attr, source_kind, is_shared) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                insert_buf,
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    logger.info(
        "持久化完成：新增 %d 条 AI 事实，保留 %d 条已确认/手动事实%s"
        "（作用域方案=%s）",
        len(insert_buf), len(protected),
        f"，跳过 {skipped_dup} 条与已确认事实重复" if skipped_dup else "",
        scheme_id or f"项目={project_id}",
    )


# ---------------------------------------------------------------------------
# 工具函数：增量提取进度（facts_extracted_chunks）
# ---------------------------------------------------------------------------

async def load_completed_chunks(db, project_id: str, scheme_id: str = "") -> set:
    """读取方案已成功提取的分段指纹集合（增量提取「跳过已完成段」的依据）。

    ✅ BUG-1 修复：进度作用域从 project 细化到 (project, scheme)。旧实现仅按
    project_id 记录，导致多方案共享同一项目时，非首方案命中首方案的完成段指纹、
    触发 all_skipped 早退而写入 0 条事实。现按 scheme_id 隔离，各方案独立统计。

    表不存在 / 查询异常时返回空集（退化为全量提取，不影响可用性）。
    """
    if not project_id:
        return set()
    try:
        cur = await db.execute(
            "SELECT chunk_hash FROM facts_extracted_chunks "
            "WHERE project_id=? AND scheme_id=?",
            (project_id, scheme_id or ""))
        return {r[0] for r in await cur.fetchall() if r[0]}
    except Exception as e:
        logger.warning("读取提取进度失败（按全量提取处理）: %s", e)
        return set()


async def save_extracted_chunks(db, project_id: str, scheme_id: str,
                                ok_hashes, all_hashes) -> None:
    """提取成功后更新进度表（2026-09-17 增量提取）：

    - 写入本次成功段指纹（含跳过段，它们本就已完成），按 (project, scheme) 作用域；
    - ✅ 残留清理：删除「不在本次分段集合中」的旧指纹 —— 文档被删除或
      重解析后内容变化，其旧段指纹永远不会再命中，留在表里只会膨胀。
    提取中途失败 / 用户停止时调用方不会调用本函数（进度不前移）。
    """
    if not project_id:
        return
    ok = {str(h) for h in (ok_hashes or set()) if h}
    all_h = {str(h) for h in (all_hashes or set()) if h}
    try:
        cur = await db.execute(
            "SELECT chunk_hash FROM facts_extracted_chunks "
            "WHERE project_id=? AND scheme_id=?",
            (project_id, scheme_id or ""))
        existing = {r[0] for r in await cur.fetchall()}
        stale = existing - all_h
        if stale:
            ph = ",".join("?" * len(stale))
            await db.execute(
                f"DELETE FROM facts_extracted_chunks "
                f"WHERE project_id=? AND scheme_id=? AND chunk_hash IN ({ph})",
                (project_id, scheme_id or "", *stale))
        missing = ok - existing
        if missing:
            await db.executemany(
                "INSERT OR IGNORE INTO facts_extracted_chunks "
                "(project_id, scheme_id, chunk_hash) VALUES (?,?,?)",
                [(project_id, scheme_id or "", h) for h in missing])
        await db.commit()
        logger.info(
            "提取进度已更新：项目=%s 方案=%s 完成 %d 段（新增 %d，清理残留 %d）",
            project_id, scheme_id, len(ok), len(missing), len(stale))
    except Exception as e:
        logger.warning("更新提取进度失败（不影响提取结果）: %s", e)
        try:
            await db.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 工具函数：清空缓存（facts 变更后联动）
# ---------------------------------------------------------------------------

async def invalidate_export_cache(db, scheme_id: str, facts_touched: bool = False) -> int:
    """事实变更后清空 export_cache，强制下次导出重新生成。

    ✅ BUG 修复（磁盘文件泄漏）：旧实现只删 DB 记录、不删磁盘产物 ——
    而 export.py 的保留策略是"按 export_cache 行数最多保留 5 份 / 方案"，
    记录一旦被这里清空，那些 .docx 文件就再没有任何引用方，永远不会被
    保留策略回收，`data/_exports` 随事实迭代无限膨胀。现改为随记录一并删除。

    ⚠️ 失效范围边界（数据流审计 2026-09-23 文档化 + 2026-09-29 收口）：
    本函数负责「导出产物」层（export_cache 行 + 磁盘 .docx）的失效。
    已生成的正文 sections.content 仍**不回滚、不自动重生** —— 正文是生成当时
    对事实的快照，避免后台静默重写用户已编辑的章节。
    但「用户无从知道正文可能已过时」这个盲区已补齐：`facts_touched=True` 时
    同时推进方案级 `schemes.facts_updated_at` 时间戳，章节树读路径据此**读侧
    派生** `facts_stale` 标记（章节正文写在事实变更之前 → 界面提示
    「事实已变更，建议重新生成」），由用户显式决定要不要重生。
    选「方案级单点时间戳」而非「给 sections 加布尔列 + 13 处正文写路径各清一次」，
    因为后者正是本仓反复踩的「同一判据散落多处、漏改一处」陷阱。

    Args:
        scheme_id: 方案 ID；
        facts_touched: 本次调用是否伴随**全局事实的写操作**（True = 推进
            ``schemes.facts_updated_at``）。默认 False 保持既有调用方（如
            ``bid_analysis`` 的提取项失效）行为完全不变 —— 那些路径没有改动
            事实表，推进时间戳会让已生成正文被**误标**为过时。
    """
    from pathlib import Path

    cur = await db.execute(
        "SELECT id, result_path FROM export_cache WHERE scheme_id=?", (scheme_id,))
    rows = [dict(r) for r in await cur.fetchall()]
    cache_count = len(rows)
    if cache_count > 0:
        await db.execute("DELETE FROM export_cache WHERE scheme_id=?", (scheme_id,))
        await db.commit()
        for r in rows:
            p = r.get("result_path")
            if not p:
                continue
            try:
                Path(p).unlink(missing_ok=True)
            except OSError as e:  # 文件被占用/权限不足不影响主流程
                logger.warning("清理导出产物失败（可忽略）: %s (%s)", p, e)
        logger.info("已清空 %d 条 export_cache（scheme=%s）", cache_count, scheme_id)

    if facts_touched:
        # 章节失效标记的数据源。写失败只告警不阻断：正文与导出缓存已正确处理，
        # 少一个「建议重生」提示不应让事实写操作整体失败。
        try:
            await db.execute(
                "UPDATE schemes SET facts_updated_at=? WHERE id=?",
                (datetime.now().isoformat(), scheme_id))
            await db.commit()
        except Exception as e:  # pragma: no cover - 仅告警，不改变主流程结论
            logger.warning("推进方案事实变更时间戳失败（章节失效标记不可用）: %s", e)
    return cache_count


# ---------------------------------------------------------------------------
# 工具函数：可注入下游的全局事实查询（目录/正文/导出三处共用）
# ---------------------------------------------------------------------------

# 可注入下游（目录生成/正文生成/导出附录）的全局事实过滤条件：
# 剔除未裁决的矛盾值（has_conflict）与未经人工确认的模拟值（is_resolved=0），
# 避免把“待裁决/编造值”当确定性事实注入模型与交付文档。
# 注入条件是最后一道数据安全闸门：即使历史脏数据误写 is_resolved=1，
# 模拟值也不得进入目录/正文/导出。冲突与未确认值沿用原门控。
_FACTS_INJECT_WHERE = "has_conflict=0 AND is_resolved=1 AND is_simulated=0 AND is_stale=0"

# ✅ BUG 修复（2026-09-27 · 降级兜底放宽门控 → 数据真实性红线）：
#   调用方（sse_handlers._load_facts_rows、global_facts._load_fact_rows）此前
#   在 import 失败时各自回落到「has_conflict=0 AND is_resolved=1」这条**旧口径**，
#   丢掉了 is_simulated=0（AI 编造值）与 is_stale=0（已被重新提取取代的过期值）。
#   后果：一旦 facts_extractor 导入失败，正文/目录注入与 danger-check 危大判定
#   就会把编造值、过期值当确定事实放行 —— 恰好在「最不该出错」的降级路径上
#   把门控放宽，方向完全反了（降级必须 fail-closed，不能 fail-open）。
#   修法：兜底常量与主口径**逐字相同**并集中在此导出，调用方不再各自硬编码，
#   从结构上消除「主口径改了、兜底忘了改」的分叉。
#: 降级兜底门控（与 _FACTS_INJECT_WHERE 逐字一致，故意不写成更宽松的旧口径）
FACTS_INJECT_WHERE_FALLBACK = (
    "has_conflict=0 AND is_resolved=1 AND is_simulated=0 AND is_stale=0"
)


def get_facts_inject_where() -> str:
    """返回「可注入下游」的全局事实门控条件（唯一出口，fail-closed）。

    调用方一律用本函数取门控，不要再各自 ``from ... import _FACTS_INJECT_WHERE``：
    下划线前缀的内部常量跨模块 import 属于技术债，且一旦 import 失败就会静默
    回落到各自硬编码的旧口径（见 FACTS_INJECT_WHERE_FALLBACK 注释）。
    """
    return FACTS_INJECT_WHERE_FALLBACK


# gt 列表达式：空分组标题降级为「其他事实」，且供 ORDER BY gt 引用别名。
FACTS_GT_COLUMN = "COALESCE(NULLIF(group_title,''), '其他事实') AS gt"


async def resolve_scheme_project_id(db, scheme_id: str) -> str:
    """由 scheme_id 反查 project_id；失败返回空串（安全回退为仅方案级查询）。"""
    try:
        pcur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
        prow = await pcur.fetchone()
        return str((prow[0] if prow else "") or "")
    except Exception:
        return ""


def build_injectable_facts_query(scheme_id: str, project_id: str,
                                 columns: str) -> tuple[str, tuple]:
    """构造「可注入下游」全局事实查询（返回 sql + params）。

    ✅ 收敛去重（数据流审计 2026-09-23）：目录生成/正文生成（sse_handlers
    `_load_facts_rows`）与导出（export.py `_query_global_facts`）曾各自维护同一段
    WHERE/ORDER BY，改一处漏一处极易漂移。统一到此，列由调用方按需传入（保持
    各自行映射不变），仅共享过滤与排序口径。

    project_id 非空时，连同同项目的项目级事实（scheme_id 为空）一起读取；
    为空时安全回退为仅方案级查询。columns 需含 `FACTS_GT_COLUMN`（带 AS gt）以保证 ORDER BY gt 成立。
    """
    if project_id:
        sql = (
            f"SELECT {columns} FROM global_facts "
            "WHERE (scheme_id=? OR (project_id=? AND (scheme_id='' OR scheme_id IS NULL))) "
            f"AND {_FACTS_INJECT_WHERE} ORDER BY gt, title"
        )
        return sql, (scheme_id, project_id)
    sql = (
        f"SELECT {columns} FROM global_facts "
        f"WHERE scheme_id=? AND {_FACTS_INJECT_WHERE} ORDER BY gt, title"
    )
    return sql, (scheme_id,)
