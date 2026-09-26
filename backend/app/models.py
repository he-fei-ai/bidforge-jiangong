"""Pydantic 数据模型"""
from typing import Optional, List, Union
from pydantic import BaseModel, Field, field_validator

#: ✅ 2026-09-26（F-CONTENT-STANDARD）：正文生成标准合法值域
#: precise=精准内容（强制逐项引用全局事实、禁模糊表述）；fuzzy=模糊内容（方向参考、允许范围表述）。
GENERATION_STANDARDS = ("precise", "fuzzy")


# ---------- 项目 ----------
class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    engineering_type: str = ""
    location: str = ""
    client_name: str = ""
    contractor_name: str = ""
    project_period: str = ""


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    engineering_type: Optional[str] = None
    location: Optional[str] = None
    client_name: Optional[str] = None
    contractor_name: Optional[str] = None
    project_period: Optional[str] = None
    status: Optional[str] = None


# ---------- 方案 ----------
class SchemeCreate(BaseModel):
    name: str
    type: str = ""
    profession: str = ""
    word_budget: int = 30000
    outline_source: str = "ai"  # ai/library/upload/mixed
    library_ids: List[str] = Field(default_factory=list)
    knowledge_scope: str = "project"
    config_json: str = "{}"


class SchemeUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    profession: Optional[str] = None
    status: Optional[str] = None
    word_budget: Optional[int] = None
    outline_source: Optional[str] = None
    config_json: Optional[str] = None
    #: ✅ 断链修复：旧模型无此字段，创建后无法更换/补选方案关联的目录库
    library_ids: Optional[List[str]] = None
    #: ✅ 2026-09-26（F-CONTENT-STANDARD）：方案级正文生成标准
    generation_standard: Optional[str] = None

    @field_validator("generation_standard")
    @classmethod
    def _check_generation_standard(cls, v):
        # None = 未传（PATCH 忽略该字段）；非空值必须在合法值域内（FastAPI 自动映射为 422）。
        if v is not None and v not in GENERATION_STANDARDS:
            raise ValueError("generation_standard 仅支持 precise（精准内容）/ fuzzy（模糊内容）")
        return v


# ---------- 章节 ----------
class SectionCreate(BaseModel):
    title: str
    description: str = ""
    parent_id: str = ""
    level: int = 1
    sort_order: int = 0
    word_budget: int = 1500


class SectionUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[str] = None
    level: Optional[int] = None
    sort_order: Optional[int] = None
    word_budget: Optional[int] = None
    content: Optional[str] = None
    status: Optional[str] = None
    locked: Optional[int] = None
    review_status: Optional[str] = None
    #: ✅ 2026-09-26（F-CONTENT-STANDARD）：章节级生成标准覆盖。
    #: '' = 清除覆盖（沿用方案级，落库为 ''）；precise / fuzzy = 章节覆盖。
    generation_standard: Optional[str] = None

    @field_validator("generation_standard")
    @classmethod
    def _check_generation_standard(cls, v):
        # 与 SchemeUpdate 的区别：章节级额外允许 ''（显式清除覆盖、回落方案级）。
        if v is not None and v != "" and v not in GENERATION_STANDARDS:
            raise ValueError("generation_standard 仅支持 ''（沿用方案级）/ precise / fuzzy")
        return v


# ---------- 目录库 ----------
class OutlineLibraryCreate(BaseModel):
    name: str
    type: str = ""
    engineering_type: str = ""
    profession: str = ""
    applicable_conditions: str = ""
    basis: str = ""
    outline_json: str = "[]"
    tags: str = ""
    source: str = "手动创建"


class OutlineLibraryUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    engineering_type: Optional[str] = None
    profession: Optional[str] = None
    applicable_conditions: Optional[str] = None
    basis: Optional[str] = None
    outline_json: Optional[str] = None
    tags: Optional[str] = None
    review_status: Optional[str] = None


# ---------- 全局事实 ----------
class FactItem(BaseModel):
    """单条事实条目（增强版，支持溯源/模拟/置信度）"""
    name: str                           # 事实名称/键（如 "项目经理", "基坑深度"）
    value: str                          # 事实值（如 "张伟", "12.5m"）
    category: str = "other"             # 类别: personnel/schedule/equipment/tech_param/scale/other
    source: str = ""                    # 来源文件/文档名
    source_ref: str = ""                # 详细溯源引用（原文摘录）
    is_simulated: bool = False          # 是否AI模拟值
    confidence: float = 1.0             # 置信度 0-1
    is_resolved: bool = True            # 是否已人工确认
    key: str = ""                       # 归一化键（用于去重/矛盾检测）


class FactGroupIn(BaseModel):
    title: str
    content: str
    category: Optional[str] = None
    items: Optional[list[FactItem]] = None  # 结构化事实条目


class FactGroupUpdate(BaseModel):
    id: str
    title: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None
    # 单条事实级别更新
    item_updates: Optional[list[dict]] = None


# ---------- AI 配置 ----------
class AIConfigIn(BaseModel):
    id: str = ""
    provider_name: str
    plan: str = "pay_as_you_go"  # 计费方式：pay_as_you_go（按量计费）/ coding_plan（包月套餐）
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    max_tokens: int = 8192
    temperature: float = 0.7
    timeout: int = 900
    concurrency: int = 4
    # 请求方式：normal（普通请求）/ stream（流式请求）。
    # ✅ 注意：流式只改变**后端与厂商之间**的调用方式（后端边收边拼），
    #    应用侧（章节生成、事实提取等）仍等待完整结果后继续流程，对外行为不变。
    request_mode: str = "normal"
    is_active: bool = True
    # None = 本次保存不改动降级顺序（降级链由 /fallback-chain 独立维护）；
    # 若给成默认 0，每次「保存配置」都会把用户拖好的 priority 顺序清零。
    priority: Optional[int] = None
    remark: str = ""
    # ✅ 2026-09-23（多环境）：环境标签（dev / test / prod …），空串 = 通用。
    # 默认空串 → 未启用多环境时行为与引入前完全一致。
    env: str = ""


class AIConfigTest(BaseModel):
    provider_name: str = ""
    plan: str = "pay_as_you_go"   # 计费方式（仅用于前端回显/后续扩展，测试连接不依赖）
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    config_id: str = ""   # 可选：已有配置的 id，用来从 DB 读 key
    # ✅ 探测请求使用的 max_tokens（默认与 Provider 一致的 8192）。
    #    原实现恒定用 8192，而部分平台单次上限更低（如 4096），
    #    用户填对了 Key/地址/模型却因探测参数越界拿到 400。现随表单下发。
    max_tokens: int = 8192
    # 期望的请求方式（normal / stream；空 = 未指定，沿用配置值或默认策略）。
    # 探测时优先用该方式，失败后用另一种方式兜底再探一次，并把实际方式回传。
    request_mode: str = ""


class ProviderModelsIn(BaseModel):
    """拉取模型列表入参（原为裸 dict，无任何校验）。"""
    base_url: str = ""
    api_key: str = ""
    config_id: str = ""


class FallbackChainUpdate(BaseModel):
    """降级链顺序更新入参。

    ✅ 修复（本轮审查）：该模型此前**已定义但从未被使用**，
      `PUT /ai/fallback-chain` 直接用裸 `dict` 接收 ——
      请求体结构写错（如 `{"items": [...]}`）时不会报错，只会「链为空」400，
      甚至把非法元素静默忽略。现改为强类型接收。

    chain 支持两种元素形态：
      - 字符串 id：`["id1", "id2"]`
      - 对象：`[{"id": "id1"}, ...]`（前端现有格式）
    """
    chain: List[Union[str, dict]] = Field(default_factory=list)


class AuditLogCleanup(BaseModel):
    """审计日志清理入参。"""
    keep_days: int = 30      # 保留最近 N 天
    only_failed: bool = False  # 仅清理失败记录


class ConfigImportIn(BaseModel):
    """配置导入（备份迁移）：items 为导出的配置文件数组。"""
    items: List[dict] = Field(default_factory=list)
    overwrite: bool = False   # 同名（同 provider + model + base_url）是否覆盖
    set_first_active: bool = False  # 是否把第一条设为当前使用配置


class SceneRouteUpdate(BaseModel):
    """场景模型路由更新入参（✅ 2026-09-23 新增）。

    ``config_id`` 为空串表示**清除**该场景的路由（恢复共用「当前使用」配置）。
    """
    scene: str = ""
    config_id: str = ""


class ActiveEnvIn(BaseModel):
    """切换「当前生效环境」入参（✅ 2026-09-23 新增 · 多环境）。

    空串是合法取值，语义为「通用环境」= 不做任何环境过滤（旧行为）。
    """
    env: str = ""


class ConfigRollbackIn(BaseModel):
    """配置版本回滚入参（✅ 2026-09-23 新增 · G6）。

    ``audit_id`` = 要回滚到「该变更记录之前」的状态。
    ``include_active`` = 是否连「当前使用」标记一起还原（默认 False，见 rollback 端点说明）。
    """
    audit_id: str = ""
    include_active: bool = False


class DisabledProvidersIn(BaseModel):
    """运行时「厂商开关」入参（✅ 2026-09-23 新增）。

    整体覆盖语义：传空数组 = 恢复全部厂商。
    """
    providers: List[str] = Field(default_factory=list)


# ---------- 目录节点（树） ----------
class OutlineNode(BaseModel):
    id: str
    title: str
    description: str = ""
    level: int = 1
    confidence: Optional[float] = None
    children: List["OutlineNode"] = Field(default_factory=list)


OutlineNode.model_rebuild()


# ---------- 审核与预检（商业级增强） ----------
#: 评审状态机（PRD §3.12.5）
REVIEW_STATUSES = ("pending", "reviewing", "approved", "rejected")


class ComplianceCheckIn(BaseModel):
    """规范符合性检查入参。

    此前该端点直接收 ``body: dict``，靠 AI 返回后再断言字段，
    缺字段/类型错只能等到运行期才炸。此处补上契约校验。
    """

    scheme_id: str
    checklist: List[str] = Field(default_factory=list)
    #: 仅检查指定规则（留空表示全部 AI 规则）
    rule_ids: List[str] = Field(default_factory=list)


class ExpertReviewIn(BaseModel):
    scheme_id: str
    attachments: List[str] = Field(default_factory=list)


class SectionReviewIn(BaseModel):
    """章节审核（状态流转 + 评审意见）。"""

    to_status: str                      # pending / reviewing / approved / rejected
    reviewer: str = ""
    comment: str = ""


class SchemeReviewIn(BaseModel):
    """方案级评审提交（把方案整体推进到指定状态）。"""

    to_status: str
    reviewer: str = ""
    comment: str = ""
    #: ✅ BUG 修复（2026-09-21）：方案级 /submit 此前不校验章节审核完整性。
    #: 现引入可选开关：True 时若仍有章节未纳入审核，返回 422；默认 False
    #: 保持向后兼容（既有调用方不会被硬拦截），仅在返回体里给出未审明细。
    require_all_sections_reviewed: bool = False


# ---------- 通用 ----------
class TaskControl(BaseModel):
    action: str  # pause / resume / stop