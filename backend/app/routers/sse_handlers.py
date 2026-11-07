"""SSE 事件处理器（核心：目录生成、正文生成、全局事实提取）

所有长任务通过 SSE 推送进度，支持暂停/恢复/停止。

控制机制：
- pause_event（默认 set=运行中）：循环顶部 await wait_resume() 挂起
- stop_event（默认未 set）：每轮检查 is_stopped() 并触发 asyncio.CancelledError
- 停止时通过 child_tasks 集合统一 cancel 所有并发子任务
"""

import asyncio
import copy  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
import json
import logging
import re  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.config import settings
from app.db import (
    get_conn,
    get_db,
    get_read_conn,
    is_retryable_db_error,
    release_read_conn,
    retry_db_op,
    safe_rowcount,
    settle_global_conn,
)
from app.routers._chart_pipeline import (
    _load_scheme_type_counts,
    apply_inline_chart_plan,
    build_chart_quota_hint,
    build_inline_chart_plan,
    chart_drop_label,
    count_inline_charts,
    resync_inline_chart_rows,
)
from app.services import activity_broadcaster as _ab
from app.services import facts_extractor as _fe
from app.services import l3_standard
from app.services.ai import task_registry as _tr

# ✅ R179 U5：分阶段并发读数（outline/content 阶段；开关关=逐字现状）
from app.services.ai import warmup as ai_warmup

# ✅ 正文子标题编号规则生成方（{subheading_rule} 占位符的唯一实现）；
#    命名空间与导出 _compute_subheading 同口径，避免提示词与成稿不一致。
from app.services.ai.heading_templates import build_subheading_rule
from app.services.ai.json_response import (
    OUTLINE_REPAIR_KEY,
    collect_json_response,
    renumber_outline,  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
)
from app.services.ai.prompts._registry import render
from app.services.ai.provider_factory import chat_with_fallback

# ---------- SSE 心跳机制 ----------
# ✅ 单一权威实现（2026-09-15）：心跳包装器统一由 app.services.ai.sse_utils 提供。
#    旧实现本文件内另有一份「无界队列」副本，与 sse_utils 版本（maxsize=100 背压）
#    长期分叉；现统一 import，并新增 stats_provider —— 长 AI 调用期间由心跳通道
#    周期性推送运行统计（已耗时/进行中章节/累计字数/ETA），解决进度条静止问题。
from app.services.ai.sse_utils import with_heartbeat  # noqa: E402
from app.services.ai.task_registry import (
    TaskTypeConflict,  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
    finish_task,
    has_active_task,
    is_stopped,
    register_child_task,
    register_task,  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
    request_control,
    update_progress,
    update_task_stats,
    wait_resume,
)
from app.services.ai.workflows_base import concurrency_controller

# ✅ 2026-10-02（检查点前置）：审核与预检检查点 → 正文生成约束（唯一事实源）。
#    system 级硬约束整案一份（渲染时注入 {content_checkpoint_block}）；
#    章节级必含要素逐章注入（{chapter_checkpoint_block}）；
#    生成后自检由 content_selfcheck / content_selfcheck_autofix 控制（默认开）。
from app.services.content_checkpoint import (
    build_chapter_checkpoint_block,
    build_content_system_checkpoint_block,
    checkpoint_selfcheck,
    cross_section_copy_findings,
    fix_bare_standard_codes,
    infer_chapter_key,
    is_hazardous_scheme,
    rewrite_placeholder_marks,
)
from app.services.content_data_contract import cross_section_value_findings

# ✅ R77（2026-10-14 · 引入易标「生成后事实冲突审计 + 定点替换」）：
#    standard_report 检出的 error 级事实冲突经本模块接入自动修复闭环。
from app.services.content_fact_autofix import (
    accumulate_fact_autofix,
    autofix_section_fact_conflicts,
    empty_fact_autofix_summary,
    select_fact_conflicts,
)
from app.services.content_plan import (
    plan_section_content,
    render_content_plan_guidance,
    resolve_selected_fact_rows,
)
from app.services.content_polish import quality_issues, sanitize_ai_content

# ✅ 2026-09-28（T-1 收口）：章节运行时纯函数（user 上下文 / 续写 messages /
#    轮次判定 / token 折算），原 generate_content 内联实现逐字搬迁至此。
from app.services.content_runtime import (
    build_chapter_extras_block,
    build_chapter_layered_messages,
    build_chapter_user_content,
    build_continuation_messages,
    continue_max_tokens,
    resolve_system_variables,
    should_continue_round,
)
from app.services.content_shrink import (
    SHRINK_MAX_ROUNDS,
    shrink_content_rounds,
)

# F-CONTENT-STANDARD(2026-09-26): generation standard (precise/fuzzy)
from app.services.content_standard import (
    PRECISE,
    attach_fence_swallowed_issue,
    build_system_block,
    normalize_standard,
    paragraph_structure_report,
    resolve_effective_standard,
    standard_report,
)
from app.services.content_utils import (
    DEFAULT_WORD_BUDGET,  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
    LEAF_MIN_WORD_BUDGET,
    WORD_OVER_RATIO,  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
    WORD_UNDER_RATIO,  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
    auto_fix_unclosed_fences,
    build_sibling_context,
    leaf_word_budget,
    max_tokens_for_budget,
    normalize_word_budget_override,
    order_sections_dfs,
    prev_leaf_tail_summary,
    resolve_concurrency,
    resolve_word_budget_tier,
    select_target_leaves,
    summarize_fact_mapping,
    text_word_count,
    verify_generation_order,  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
    word_audit_summary,
    word_closeout_notice,
    word_deviation_of,
    word_status_for,
)

# ✅ R47 债-5（2026-10-06）：项目关键事实构建整组下沉到 services/facts_builder。
#    消除 compliance.py / consistency_scanner.py 对本路由器私有符号的反向 import；
#    本模块实际使用下方多个符号；LOW_CONFIDENCE_THRESHOLD 是被 tests 经
#    sse_handlers re-export 取用的对外别名（故显式 noqa，不是漏改）。
#    R57 清理：彼时 4 个别名（_chapter_inject_enabled / _FACTS_GENERIC_GROUP_HINTS /
#    _row_chapter / _rank_facts_by_basis）零消费者被删；✅ R77（2026-10-14）
#    重新引入 _rank_facts_by_basis + render_outline_chapter_facts：目录子目录
#    按章相关性注入事实（outline_sublevel_facts_relevant）重新消费二者。
from app.services.facts_builder import (  # noqa: E402
    LOW_CONFIDENCE_THRESHOLD,  # noqa: F401 - 对外 re-export，tests 经此名取阈值
    _facts_keywords,  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
    _filter_facts_rows,
    _load_facts_rows,
    _rank_facts_by_basis,
    _render_facts_text,  # noqa: F401 - 对外 re-export，tests 经此名取渲染纯函数；
    # 正文生成调用点自 R105 起改走 fact_injection_audit 单一出口
    # （内部复用同一 _render_facts_text，rendered_text 逐字节一致 + 审计视图）
    build_facts_text,
    chapter_fact_mapping,
    core_facts_rows,
    exclude_rows,
    fact_injection_audit,
    render_outline_chapter_facts,
)

# ✅ 编号统一（2026-09-25）：收敛到 services/numbering —— 目录编号的唯一事实源。
# 导入三件套：
#   stored_outline_id     存储态编号读取（UUID 永不泄漏进提示词）
#   stored_id_to_display  存储态 → 展示态（第X章 / N / N.M），上级链与导出一致
#   strip_outline_numbering 标题内嵌编号剥离（与 json_response 同源再导出）
from app.services.numbering import (
    load_scheme_section_index,
    normalize_section_content_subheadings,
    stored_id_to_display,  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
    stored_outline_id,  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
    strip_outline_numbering,  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
    validate_section_content_numbering,
)

# ✅ R179 Phase F（2026-11-06）：数值锁（机械校验，纯函数）。
from app.services.numeric_lock import enforce_text as enforce_numeric_lock

# ✅ 2026-10-17（分层债收口）：占位标题外科式重写整组下沉 services/；
#    本模块同名 import（调用点零改动），路由器（sections.py）改从 services 取，
#    消除「路由器反向 import sse_handlers 私有符号」的分层耦合。
from app.services.outline_title_fix import (  # noqa: F401
    OUTLINE_FIX_MAX_TOKENS,
    OUTLINE_FIX_TIMEOUT,
    _apply_title_updates,
    _build_existing_titles,
    _build_title_fix_items,
    _outline_path_index,
    _title_fix_validate_fn,
    _try_outline_title_fix,
)
from app.services.outline_utils import (
    normalize_outline,
)
from app.services.placeholder_sections import (
    PLACEHOLDER_CONTENT,
    persist_placeholder_sections,
    split_placeholder_leaves,
)
from app.services.section_router import (
    ClauseSearchBudget,
    allowed_numbers_for,
    blacklist_from_settings,
    build_hybrid_block,
    route_node,
    search_for_section,
    search_report_fields,
    search_skip_note,
    verify_experience_text,
)
from app.services.standards_registry import get_standards_text

# 公开入口改名后保留旧私有名别名，防止 sse_handlers 内部漏改调用点。
_build_facts_text = build_facts_text  # noqa: E402

# ✅ B1 拆分（2026-11-03）：正文生成三个终态载荷拼装点下沉到 _sse_content_payloads，
#    本模块只保留同名薄包装（调用点、源码锚点与 monkeypatch 语义不变）。
from app.routers._sse_content_payloads import (
    build_content_checkpoint,
    build_error_payload,
    build_stopped_payload,
)
from app.routers.sse_checkpoint import (  # noqa: F401 — 测试直接从 sse_handlers monkeypatch/re-export
    _FACTS_CHECKPOINT_FIELDS,
    _checkpoint_partial_outline,
    _facts_checkpoint_payload,
    _load_outline_checkpoint,
    _load_task_checkpoint,
    _retry_db_locked,
    _save_content_checkpoint,
    _save_facts_checkpoint,
    _save_outline_checkpoint,
    _save_task_checkpoint,
    _write_task_checkpoint_db,
)
from app.routers.sse_utils import (
    _DEDUP_TAIL_WINDOW,
    _cn_number,  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
    _coerce_bool,
    _coerce_suggestions,  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
    _dedup_continuation,
    _safe_tail,
)

logger = logging.getLogger("sse")
router = APIRouter(prefix="/api/v1/sse", tags=["sse"])

DANGEROUS_TYPES = {"深基坑", "高支模", "脚手架", "塔吊", "施工电梯", "临时用电", "有限空间", "爆破", "暗挖"}


def _degrade_section_done_payload(payload: dict, max_chars: int) -> dict:
    """section_done 事件体积闸门：超限时**显式**截断正文并附降级字段。

    ✅ R181（2026-11-06）：此前事件体积没有任何上限，超大章节会把整章正文
    原样塞进 SSE；一旦客户端消费端有内存/日志截断，表现为「正文突然少了但
    没有任何提示」。现改为：
      - max_chars <= 0   → 完全不干预，返回原对象（与引入前逐字节一致）；
      - 未超限           → 返回原对象（不新增任何键）；
      - 超限             → 只截 content，附 truncated / truncated_chars /
                           kept_chars / content_ref / truncation_note。
    正文已在本事件之前落库，因此截断不影响断线重挂与检查点续跑（二者都读库，
    不依赖本次事件载荷）；前端可据 truncated=true 提示并重新拉取章节全文。
    """
    if max_chars <= 0:
        return payload
    try:
        raw = json.dumps(payload, ensure_ascii=False)
    except Exception:
        return payload
    total = len(raw)
    if total <= max_chars:
        return payload
    content = str(payload.get("content") or "")
    # 预留新增降级字段与外壳的字节开销，保证截断后整体仍不超过上限
    overhead = max(0, total - len(content)) + 240
    keep = max(0, max_chars - overhead)
    kept = content[:keep]
    out = dict(payload)
    out["content"] = kept
    out["truncated"] = True
    out["truncated_chars"] = len(content) - len(kept)
    out["kept_chars"] = len(kept)
    out["content_ref"] = {"section_id": str(payload.get("section_id") or ""), "source": "db"}
    out["truncation_note"] = "事件体积超出上限，正文已截断；完整正文已落库，刷新章节即可获取"
    return out


# ⚠️ 遗留问题 3 修复（R101）：此处**不再**定义 CONTENT_REQUEST_TIMEOUT /
#    CONTENT_TOTAL_TIMEOUT 字面量。二者此前在本模块被定义两次（本处 300/660
#    过期字面量 + 下方「配置配了不生效」块的 settings 绑定版），后者按执行顺序
#    静默覆盖前者 ⇒ 同名遮蔽（R65 遮蔽锁此前把本文件排除在外）。现收敛为
#    下方唯一一处定义：设置 CONTENT_REQUEST_TIMEOUT=120 等环境变量即生效。
#    下方「正文生成参数」注释仍说明**为何**放宽超时（默认值与旧字面量逐字相同）。
# ---------- 正文生成参数（稳定性 / 体验） ----------
# ✅ 单次章节生成的 provider 超时：AI 配置里常见 timeout=60s，对 1500+ 字的章节明显偏短，
#    线上表现为大量章节 ~62s 超时失败。这里统一放宽并允许降级链逐个尝试。
# ✅ 目录生成超时：一级/短方案目录输出体量比单章正文大，沿用配置里的 60s
#    容易在弱模型/慢链路下超时失败；这里统一放宽（降级链仍会逐个尝试）。
# ✅ 2026-09-26 配置化（与下方 CONTENT_* 同一口径）：原为硬编码字面量，
#    运维想调只能改源码。现绑定 Settings，默认值与原字面量逐字相同
#    （180/60/120/50000/0.8/45.0）→ 不设环境变量时行为与旧版完全一致。
OUTLINE_REQUEST_TIMEOUT = max(10, int(settings.outline_request_timeout))
# ✅ 增强审核/修复超时常量化：旧实现内联 30s/45s，修复轮输入=完整目录+建议、
#    输出=完整目录（体量与生成相当），45s 在弱模型下频繁超时 →"自动修复超时，
#    保留原目录"，审核-修复循环形同虚设。放宽至与生成链路同量级。
OUTLINE_REVIEW_TIMEOUT = max(10, int(settings.outline_review_timeout))
# 目录审核/修复显式输出上限（2026-10-04）：合并修复 fast-path 需回吐完整
# fixed_outline，长方案下 provider 默认 8192 会截断，导致 repair 轮再烧
# 120s 才回退。0 = 沿用 provider 默认（向后兼容、用户面板可调）。
OUTLINE_REVIEW_MAX_TOKENS = int(settings.outline_review_max_tokens) or None
# ✅ 分步生成阈值（原为内联魔数 50000）：字数预算超过此值的方案改走
#    「一级 → 逐章二三级 → 审核」分步链路。一次性直出 5 万字以上方案的
#    完整三级目录，输出体量过大、弱模型下极易被截断或超时。
OUTLINE_STEPWISE_MIN_WORDS = max(1, int(settings.outline_stepwise_min_words))

# ✅ R58（2026-10-10）：短方案一次性直出失败时是否切换分步链路（见上方配置项）。
#    与 OUTLINE_STEPWISE_MIN_WORDS 一样在 import 期固化 —— 测试请 monkeypatch
#    本常量（改 settings 实例对已导入的模块无效）。
OUTLINE_ONESHOT_FALLBACK = bool(getattr(settings, "outline_oneshot_fallback_stepwise", True))

# ✅ R175 BUG-1（2026-11-03）：目录生成模式（auto/oneshot/stepwise）。
#    与其它 outline_* 常量一样在 import 期固化 —— 测试请 monkeypatch 本常量
#    或直接给 `_use_stepwise_for_budget` 传 scheme 形参。
OUTLINE_SCHEME = str(getattr(settings, "outline_scheme", "auto") or "auto").strip().lower()
_VALID_OUTLINE_SCHEMES = ("auto", "oneshot", "stepwise")


def _use_stepwise_for_budget(word_budget, scheme: str | None = None) -> bool:
    """分步生成判定的**唯一事实源**（禁止任何调用点再裸比较字数阈值）。

    - auto（默认）：逐字保持引入前语义 —— ``word_budget > OUTLINE_STEPWISE_MIN_WORDS``
      走分步（边界取等 = 一次性直出，与旧实现 `>` 完全一致）；
    - oneshot：强制一次性直出。与 ``OUTLINE_ONESHOT_FALLBACK`` 降级兜底不冲突：
      兜底只在一次性直出**失败之后**生效，本函数只回答「首轮走哪条链路」；
    - stepwise：强制分步（跳过一次性直出）。
    非法/空值回退 auto（向后兼容：老 .env 不写 outline_scheme = 行为不变）。
    脏 word_budget 与调用方同口径回退 30000，绝不抛异常中断生成。
    """
    mode = OUTLINE_SCHEME if scheme is None else scheme
    mode = str(mode or "auto").strip().lower()
    if mode not in _VALID_OUTLINE_SCHEMES:
        mode = "auto"
    if mode == "oneshot":
        return False
    if mode == "stepwise":
        return True
    try:
        budget = int(word_budget)
    except (TypeError, ValueError):
        budget = 30000
    return budget > OUTLINE_STEPWISE_MIN_WORDS


# ✅ 审核提示词的节点预算：超出部分不送入审核（避免 token 爆炸）。
#    按前序优先保留，一级/二级轮廓始终完整。
OUTLINE_REVIEW_MAX_NODES = 150
# ✅ 审核-修复链路的目录节点上限：比生成链路（500）宽松。
#    长方案完整目录（30 章 × 20+ 节点极易 > 500）若沿用 500，
#    修复结果会被 _validate_outline 判为非法并整轮丢弃，
#    「按审核建议修复」形同虚设。
OUTLINE_FIX_MAX_NODES = 1200
# ✅ P2-2（2026-09-27）：生成链路的节点上限改为可配置（此前是
#    ``_validate_outline`` 的默认形参 500，写死在 services 层无法调整）。
#    默认 500 与修复前**逐字相同**；与修复轮 1200 的口径差异保持可见、可调。
OUTLINE_GENERATE_MAX_NODES = max(10, int(settings.outline_generate_max_nodes))
# ✅ 修复结果的最小覆盖率（相对原目录节点数）：
#    低于该比例视为「模型截断/敷衍」，保留原目录 —— 宁可未修复，不可丢目录。
#    见 _outline_fix_looks_degraded。
OUTLINE_FIX_MIN_COVERAGE = min(1.0, max(0.0, float(settings.outline_fix_min_coverage)))
# ✅ 单章子目录生成的预期耗时（秒）：用于分步链路「当前章」的进度渐近填充。
OUTLINE_CHAPTER_EXPECT = max(1.0, float(settings.outline_chapter_expect_seconds))
# ---------- 结构化摘要按小节比例截断（2026-09-23） ----------
# ✅ 2026-10-06（R47 债-2）：字符预算分配器下沉到
#    ``app.services.prompt_governance.allocate_char_budgets``。
#    本模块以 ``as _allocate_char_budgets`` 保留旧私有名绑定，下方所有
#    ``_allocate_char_budgets(...)`` 调用一行不改；``MIN_SECTION_CHARS`` /
#    ``MAX_SINGLE_SECTION_RATIO`` 随函数一并下沉（此前仅被本函数消费）。
from app.services.prompt_governance import (  # noqa: E402,F401
    allocate_char_budgets as _allocate_char_budgets,
)

#: 配额/认证类错误（402/403/404/429）是否仍重试。
#: O8（2026-09-21）：默认 **False** —— 这类错误重试几乎必然再失败
#: （余额不足、密钥失效、模型不存在），白白多烧一次配额与一轮超时；
#: 置 True 可恢复旧的「一律重试一次」行为（向后兼容开关）。
AI_RETRY_ON_QUOTA_ERROR = bool(getattr(settings, "ai_retry_on_quota_error", False))

#: 判定为「配额/认证类」的 HTTP 状态码与错误关键词。
#: ⚠️ **429 不在其中**：429 是限流（临时、可退避自愈），必须保持重试 ——
#:    把它与 402/403/404（余额不足 / key 失效 / 模型不存在，重试必败）
#:    混为一谈会让限流场景直接失败，削弱 provider 降级链的抗抖动能力。
_QUOTA_HTTP_CODES = (402, 403, 404)
_QUOTA_ERROR_KEYWORDS = (
    "402",
    "403",
    "404",
    "insufficient",
    "quota exceeded",
    "unauthorized",
    "forbidden",
    "not found",
    "余额",
    "配额",
)


def _is_quota_error(err: BaseException | str) -> bool:
    """判断异常是否属于「余额 / 认证 / 模型不存在」类**确定性**错误。

    这类错误重试不会自愈（余额不足、key 失效、模型 ID 写错），必须直接
    失败并切 provider。**429 限流不在此列** —— 它是临时状态，退避后
    重试通常即成功，必须保持重试。
    """
    text = str(err or "")
    for code in _QUOTA_HTTP_CODES:
        if f"HTTP {code}" in text or f" {code} " in text:
            return True
    low = text.lower()
    return any(k in low for k in _QUOTA_ERROR_KEYWORDS)


# ---------- 上下文降级可见信号（D-39 · R176 · 2026-11-04） ----------
#: `context_degraded.reason` 的稳定枚举（前端按此映射文案，禁止塞自由文本）。
CONTEXT_DEGRADED_BUDGET = "budget_truncated"  # 预算硬切：字段被截短
CONTEXT_DEGRADED_SEGMENT = "segment_failed"  # 装配段失败：整段上下文缺席
CONTEXT_DEGRADED_REASONS = (CONTEXT_DEGRADED_BUDGET, CONTEXT_DEGRADED_SEGMENT)


def _record_context_degraded(
    out: list | None,
    *,
    field: str,
    stage: str,
    original_chars: int,
    kept_chars: int,
    reason: str = CONTEXT_DEGRADED_BUDGET,
    segment_id: str = "",
    extra: dict | None = None,
) -> None:
    """登记一次**真实发生**的上下文降级（只簿记，不发送）。

    发送由 `_context_degraded_events` 在 SSE 流内统一完成。`original_chars <=
    kept_chars` 的预算记录直接丢弃 —— 未截断的正常路径**不得**产生任何记录，
    否则前端会把「一切正常」也渲染成降级提示（多发即噪音）。

    ``extra`` 用于附加**结构化明细**（如知识库逐条裁剪清单），不得覆盖
    field/stage/reason/字符数/segment_id 这些契约字段。
    """
    if out is None:
        return
    try:
        original = int(original_chars or 0)
        kept = int(kept_chars or 0)
    except (TypeError, ValueError):
        return
    if reason == CONTEXT_DEGRADED_BUDGET and original <= kept:
        return
    record = {
        "field": str(field or ""),
        "stage": str(stage or ""),
        "reason": (reason if reason in CONTEXT_DEGRADED_REASONS else CONTEXT_DEGRADED_SEGMENT),
        "original_chars": original,
        "kept_chars": kept,
        "dropped_chars": max(0, original - kept),
    }
    if segment_id:
        record["segment_id"] = str(segment_id)
    for _k, _v in (extra or {}).items():
        if _k not in ("field", "stage", "reason", "original_chars", "kept_chars", "dropped_chars", "segment_id"):
            record[_k] = _v
    out.append(record)


def _record_knowledge_budget(
    out: list | None,
    stats: dict | None,
    *,
    stage: str,
    field: str = "knowledge_text",
) -> None:
    """把 ``build_knowledge_text(stats_out=...)`` 的精确裁剪统计并入降级簿记。

    正常无裁剪（``truncated_items + dropped_items == 0``）时零记录/零事件。
    """
    if not stats:
        return
    affected = int(stats.get("truncated_items") or 0) + int(stats.get("dropped_items") or 0)
    if affected <= 0:
        return
    _record_context_degraded(
        out,
        field=field,
        stage=stage,
        original_chars=int(stats.get("original_chars") or 0),
        kept_chars=int(stats.get("kept_chars") or 0),
        reason=CONTEXT_DEGRADED_BUDGET,
        segment_id="knowledge_text",
        extra={
            "cut_items": affected,
            "truncated_items": int(stats.get("truncated_items") or 0),
            "dropped_items": int(stats.get("dropped_items") or 0),
            "items": list(stats.get("items") or []),
        },
    )


def _context_degraded_events(task_id: str, records: list | None) -> list[str]:
    """把降级簿记转为 SSE 事件串；**空输入 → 空输出**（正常路径零噪音）。

    同一 (field, stage, reason, segment_id) 的记录跨章节/跨环节合并为一条，
    字符数与计数累加、``items`` 明细拼接，避免逐章重复下发把前端提示刷成噪音。
    """
    grouped: dict[tuple, dict] = {}
    for rec in records or []:
        key = (rec.get("field"), rec.get("stage"), rec.get("reason"), rec.get("segment_id"))
        cur = grouped.get(key)
        if cur is None:
            grouped[key] = {**rec, "occurrences": 1}
            continue
        cur["original_chars"] += int(rec.get("original_chars") or 0)
        cur["kept_chars"] += int(rec.get("kept_chars") or 0)
        cur["dropped_chars"] += int(rec.get("dropped_chars") or 0)
        cur["occurrences"] += 1
        for _k, _v in rec.items():
            if _k in ("field", "stage", "reason", "segment_id", "original_chars", "kept_chars", "dropped_chars"):
                continue
            if isinstance(_v, list):
                cur.setdefault(_k, []).extend(_v)
            elif isinstance(_v, bool):
                cur[_k] = _v
            elif isinstance(_v, (int, float)):
                cur[_k] = cur.get(_k, 0) + _v
            else:
                cur.setdefault(_k, _v)
    events = []
    for rec in grouped.values():
        payload = {"event": "context_degraded", "task_id": task_id, **rec}
        events.append("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n")
    return events


# ---------- 目录生成 · 并发 / 批处理 / 审核模式（2026-09-21 ~ 09-23 配置化） ----------
# 长方案逐章子目录的批内并发数。旧实现纯串行（30 章 × ~45s ≈ 22 分钟），
# 是长方案"看起来卡死"的最大来源；批间仍串行以保留跨章去重上下文。
# <=0 一律回退 1（串行），保证配置脏值不炸编排层。
OUTLINE_CHAPTER_CONCURRENCY = max(1, int(settings.outline_chapter_concurrency))
# 单次调用合并生成的章数。默认 1 = 逐章调用（与旧行为逐字一致），
# 调大可把调用次数降为 ⌈N/k⌉，代价是批内跨章去重上下文变弱。
OUTLINE_CHAPTER_BATCH_SIZE = max(1, int(settings.outline_chapter_batch_size))
# 审核模式：auto = 先跑程序化覆盖预检（全过则跳过 AI 审核 / 有缺失则外科补齐），
# always = 总是走完整 AI 审核（旧行为）。
OUTLINE_REVIEW_MODE = str(settings.outline_review_mode or "auto")
# ============================================================================
# ✅ 修复（2026-09-26 · 配置「配了不生效」）：正文链路常量全部绑定 Settings
# ---------------------------------------------------------------------------
# 背景：config.py 早已提供 content_request_timeout / content_total_timeout /
# content_section_retries / content_retry_backoff / content_rate_limit_backoff /
# content_continue_max_rounds / content_temperature 共 7 项（都有环境变量覆盖
# 与单测默认值断言），但本模块此前把它们**硬编码成字面量**（CONTENT_SECTION_RETRIES
# = 1 / CONTENT_RETRY_BACKOFF = 6.0 / …），于是运维改 CONTENT_REQUEST_TIMEOUT=120
# 等环境变量**完全无效** —— 属 AGENTS.md §4.5 明令禁止的「配置散落 / 静默失效」，
# test_content_pipeline_settings.py::test_sse_handlers_constants_wired_from_settings
# 正是为此设的护栏（一直红着）。
#
# 现统一从 settings 取值，与 OUTLINE_* 系列（上方 163/166 行）口径一致。
# 默认值与原硬编码**逐字相同**（300/660/1/6.0/20.0/2）→ 旧行为不变；
# 显式设置环境变量即可生效，向后兼容。
# ============================================================================
CONTENT_REQUEST_TIMEOUT = int(settings.content_request_timeout)
CONTENT_TOTAL_TIMEOUT = int(settings.content_total_timeout)
CONTENT_SECTION_RETRIES = max(0, int(settings.content_section_retries))
CONTENT_RETRY_BACKOFF = float(settings.content_retry_backoff)
CONTENT_RATE_LIMIT_BACKOFF = float(settings.content_rate_limit_backoff)
# ✅ P1-1（2026-09-17）：自动续写轮数上限。
#    旧实现最多 4 轮、且每轮内还带 CONTENT_SECTION_RETRIES 次重试 → 单章
#    最坏 1（首稿）+ 4×2（续写）= 9 次会话级 AI 调用；实测均值 12.5 次/章
#    （2080 次调用 / 166 章），是墙钟的线性乘数。
#    首稿已按目标字数折算 max_tokens（见 max_tokens_for_budget）后，
#    返工需求大幅下降，故默认收敛为 2 轮、且续写失败**不重试**（见下）。
CONTENT_CONTINUE_MAX_ROUNDS = max(0, int(settings.content_continue_max_rounds))
# ✅ 2026-09-22 引入：正文采样温度。默认 None = 不传 temperature，沿用 provider
#    默认（保持旧行为）；设 CONTENT_TEMPERATURE=0.3 才覆盖。
CONTENT_TEMPERATURE = settings.content_temperature

# ---------- 正文生成进度模型（2026-09-15 增强） ----------
# ✅ 背景：原实现的进度只有「已完成章数 / 总章数」一个口径，且只在整章落库后
#    才推一次事件。单章 AI 调用（首轮 + 最多 4 轮续写）可持续 1~5 分钟，期间
#    零业务事件 —— 长方案下进度条数分钟纹丝不动，用户误判「卡死」。
# ✅ 现改为三段式：
#    1) 章节内阶段事件 section_stage（context → draft → continue → persist）；
#    2) 按阶段权重折算的**平滑总进度**（进行中章节也贡献部分进度）；
#    3) 心跳通道周期性推送 stats（已耗时 / 进行中章节 / 累计字数 / ETA）。
# 章节生成阶段的进度上限：其余区间留给全文一致性扫描与定向修复。
# ✅ 收敛（2026-10-12 · 技术债清单 P0-1 · 进度模型常量单一事实源）：
#    进度模型 2026-09-21 已从本文件抽到 ``services/content_progress.py``，但
#    ``_STAGE_MODEL`` / ``_STAGE_FILL_MAX`` / ``_STAGE_LABELS`` /
#    ``_SECTION_PHASE_MAX`` / ``_STATS_PUSH_INTERVAL`` 五个常量在**抽离后又各
#    自重新定义了一份**（数值此刻仍逐字相同，属「已漂移但尚未分叉」的中间态）。
#    风险正是本仓反复记录的：调 progress 的人改 content_progress、调 SSE 的人
#    改本文件，两边都「看起来生效」，实际只有一半链路生效。
#    现改为 import 别名 —— 保留 ``_`` 前缀名（既有调用点与单测
#    ``tests/test_content_progress.py`` / ``test_content_deep_audit_20260923.py``
#    均按此名导入，故兼容不变），值由 content_progress 单一持有。
#    ⚠️ 注意：monkeypatch 本模块的 ``_STAGE_MODEL`` 只会影响本模块引用；
#    要改全局应 patch ``app.services.content_progress.STAGE_MODEL``。
from app.services.content_progress import (  # noqa: E402
    SECTION_PHASE_MAX as _SECTION_PHASE_MAX,
)
from app.services.content_progress import (
    STAGE_FILL_MAX as _STAGE_FILL_MAX,  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
)
from app.services.content_progress import (
    STAGE_LABELS as _STAGE_LABELS,
)
from app.services.content_progress import (
    STAGE_MODEL as _STAGE_MODEL,  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
)
from app.services.content_progress import (
    STATS_PUSH_INTERVAL as _STATS_PUSH_INTERVAL,  # noqa: F401 — 拆分后保留绑定（测试/兼容引用）
)

# ✅ 阶段耗时在线校准（2026-09-15 增强 · 承接上一轮报告遗留建议 #1+#3）：
#    _STAGE_MODEL.expect 是出厂经验值（draft 90s / continue 60s），与具体模型、
#    章节目标字数差异很大：
#      · expect 偏小 → 阶段内还没干完就填满 85%，之后进度条又长时间「静止」；
#      · expect 偏大 → 进度条几乎不动，同样退回「静止观感」。
#    这里用**本次任务实测的阶段耗时**做 EMA 校准（只在阶段切换点更新，见
#    _calibrate_stage_expect 的单调性说明），使填充速率贴合真实模型速度。
_STAGE_EXPECT_MIN = 5.0  # 校准下限（秒）：防止个别极快样本把预期压到近 0
_STAGE_EXPECT_MAX = 600.0  # 校准上限（秒）
_STAGE_EXPECT_ALPHA = 0.3  # EMA 系数：新样本权重（越小越稳，越大越跟手）


# R47 债-1（拆分保留）：常量值单一事实源在 _limits，本模块仍以同名可读（护栏锁定）。
from app.routers._sse_outline_build import (  # noqa: F401,E402 — 拆分兼容转发（原名原对象，monkeypatch 语义不变）
    _OUTLINE_CALIBRATE_MIN_SAMPLE,
    _OUTLINE_EXPECT_ALPHA,
    _OUTLINE_EXPECT_MAX,
    _OUTLINE_EXPECT_MIN,
    _OUTLINE_PHASE_MODEL,
    _TRUNC_LABELS,
    CHAPTER_TITLE_ALIASES,
    SECTION_NUMBER_MISSING,
    _advance_outline_phase,
    _apply_word_budget_allocations,
    _await_with_stats,
    _budgeted_truncate_sections,
    _build_chapter_extraction_map,
    _build_knowledge_text,
    _build_parent_chain,
    _build_partial_preview,
    _calibrate_outline_chapter_expect,
    _calibrate_outline_expect,
    _calibrate_stage_expect,
    _continue_failed_flag,
    _count_nodes,
    _filter_knowledge_rows,
    _fmt_duration,
    _join_tail_budget,
    _level1_validate_fn,
    _load_knowledge_rows,
    _locked_progress_updater,
    _monotonic_progress,
    _outline_chapter_expect,
    _outline_eta_ms,
    _outline_expect,
    _outline_fix_validate_fn,
    _outline_patch_validate_fn,
    _outline_phase_label,
    _outline_progress,
    _outline_push_value,
    _outline_skeleton,
    _outline_validate_fn,
    _rank_sections_by_basis,
    _section_number_for_prompt,
    _section_outline_number,
    _section_partial,
    _should_auto_shrink,
    _site_trunc_extra,
    _snapshot_outline_stats,
    _snapshot_stats,
    _split_md_sections,
    _sublevel_batch_validate_fn,
    _sublevel_validate_fn,
    _validate_outline,
    _weighted_progress,
    _word_budget_hint,
    chapter_key_of_title,
)
from app.services.ai.prompts._limits import (  # noqa: E402,F401
    CONTENT_FACTS_TOTAL_BUDGET,
    PROJECT_FACTS_FULL_CAP,
    PROJECT_FACTS_LIMIT_OUTLINE,
    PROJECT_FACTS_LIMIT_SUBLEVEL,
)


async def _run_input_coverage_audit(
    db,
    project_id: str,
    scheme_id: str,
    scope_items: list,
    requirements_text: str,
    sample_provider,
    *,
    scene: str = "outline",
) -> dict:
    """四项依据输入的差集审计（只读；失败降级为 WARNING，绝不阻断生成）。

    sample_provider：可调用对象（返回本次**真正注入**的提示词文本）或字符串。
    m1（2026-09-23）：支持 callable —— 审计要检查的是实际发出去的那份文本，
    而非重新拼一份（两者可能因截断 / 模式不同而不同）。
    m2（2026-09-23）：样本求值异常必须**抬升为 WARNING**（此前只落 DEBUG，
    生产环境根本看不到），日志含「差集审计失败」以便检索。
    """
    try:
        from app.services import input_coverage as ic

        sample = sample_provider() if callable(sample_provider) else sample_provider
        inv = await ic.build_inventory(
            db,
            project_id,
            scheme_id,
            scope_items=list(scope_items or []),
            requirements_text=str(requirements_text or ""),
        )
        report = ic.audit_prompt_coverage(inv, str(sample or ""))
        ic.log_coverage_audit(scene, scheme_id, report)
        return report
    except Exception:
        logger.warning("输入差集审计失败（不阻断生成，scene=%s）", scene, exc_info=True)
        return {}


async def input_coverage_snapshot(scheme_id: str, db=Depends(get_db)) -> dict:
    """四项依据输入台账快照（前端「输入覆盖」面板的数据源）。

    方案不存在时 404 —— 快照无归属对象，返回空台账只会让前端显示
    "全绿"，掩盖"方案 id 传错"这类调用方 bug。
    """
    from app.services import input_coverage as ic
    from app.services.scheme_scope import extract_construction_scope

    cur = await db.execute("SELECT project_id, name, config_json FROM schemes WHERE id=?", (scheme_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "方案不存在")
    project_id = row["project_id"]
    try:
        config = json.loads(row["config_json"] or "{}")
    except (TypeError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    scope_items = list(extract_construction_scope(str(row["name"] or "")))
    inv = await ic.build_inventory(
        db, project_id, scheme_id, scope_items=scope_items, requirements_text=str(config.get("requirements") or "")
    )
    by_source: dict = {}
    for src in (ic.SRC_PARSE, ic.SRC_FACTS, ic.SRC_SCOPE, ic.SRC_REQ, ic.SRC_BASIS, ic.SRC_STANDARDS):
        es = inv.by_source(src)
        by_source[src] = {"total": len(es), "available": sum(1 for e in es if e.status == "available")}
    return {
        "scheme_id": scheme_id,
        "construction_scope": {"items": scope_items},
        "entries": [e.as_dict() for e in inv.entries],
        "degraded": list(inv.degraded),
        "summary": {
            "total": len(inv.entries),
            "available": sum(1 for e in inv.entries if e.status == "available"),
            "by_source": by_source,
        },
    }


@router.get("/input-coverage/{scheme_id}")
async def input_coverage(scheme_id: str, db=Depends(get_db)) -> dict:
    """四项依据输入台账（前端「输入覆盖」面板）。"""
    return await input_coverage_snapshot(scheme_id, db=db)


from app.routers._sse_outline_review import (  # noqa: F401,E402 — 拆分兼容转发（原名原对象，monkeypatch 语义不变）
    _DANGEROUS_REQUIRED_KEYWORDS,
    _OUTLINE_REVIEW_AUDIT_HINT,
    _SENTENCE_END_CHARS,
    _TITLE_FIXED_MODE_SUFFIX,
    _apply_prompt_context_budget,
    _as_children,
    _attach_quality_report,
    _build_structured_brief,
    _check_requirements_coverage,
    _common_run,
    _compact_outline_json,
    _coverage_recheck,
    _dangerous_required_keywords,
    _detect_duplicate_titles,
    _detect_generic_titles,
    _fetch_chapter_children,
    _fetch_unit_children,
    _finish_stopped_with_partial,
    _guard_external_material,
    _merge_patch_chapters,
    _merge_unit_results,
    _ocp,
    _outline_construction_scope,
    _outline_fix_looks_degraded,
    _outline_input_audit,
    _outline_review_checkpoint_kwargs,
    _outline_scheme_basis,
    _outline_standards_text,
    _register_task_exclusive,
    _residual_generic_notice,
    _restore_descriptions,
    _retry_empty_chapters,
    _review_and_fix_outline,
    _scheme_basis_obj,
    _scheme_is_dangerous,
    _split_requirement_items,
    _stream_end_guard,
    _truncate_context,
    _try_outline_patch,
)


def _outline_checkpoint_kwargs(scheme_name: str = "", scheme_type: str = "", is_hazardous: bool = False) -> dict:
    """目录提示词的「审核检查点前置要求」变量（``{outline_checkpoint_block}``）。

    受 ``settings.outline_checkpoint_check`` 控制（默认 True）。关闭或构建失败时
    返回**空字典**——调用方据此**不传该变量**，模板独占行整行丢弃 → 提示词与
    本功能引入前**逐字一致**（向后兼容红线，勿改成传空串）。
    （拆分说明：本函数的裸 ``render(**_outline_checkpoint_kwargs(...))`` 调用点
    留在 facade，护栏扫描器要求 helper 定义与调用点同文件可静态解析。）

    Args:
        scheme_name / scheme_type / is_hazardous: 危大判定的三个输入。
    """
    if not getattr(settings, "outline_checkpoint_check", True):
        return {}
    try:
        block = _ocp.build_outline_checkpoint_block(
            scheme_name=scheme_name, scheme_type=scheme_type, is_hazardous=bool(is_hazardous)
        )
        # 2026-10-03（P0-2 · 章节要素体系接入目录生成）：追加「九大章节必含
        # 要素」，让目录二三级小节逐项有落点。正文生成只按目录给的标题写
        # —— 目录里没有该要素的落点，正文阶段再要求也落不了地。
        # 受 content_chapter_elements_inject 控制（默认 True；False = 与
        # 本项引入前逐字一致）。挂在同一变量上追加，不改任何提示词模板
        # 与变量契约（复用既有 {outline_checkpoint_block} 独占行占位符）。
        if bool(getattr(settings, "content_chapter_elements_inject", True)):
            element_block = _ocp.build_outline_element_block(
                scheme_name=scheme_name, scheme_type=scheme_type, include_elements=True
            )
            if element_block:
                block = (block + element_block) if block else element_block
    except Exception:  # fail-soft：提示词增强失败不得阻断目录生成
        logger.warning("目录检查点提示块构建失败（降级为不注入）", exc_info=True)
        return {}
    return {"outline_checkpoint_block": block} if block else {}


@router.post("/generate-outline/{scheme_id}")
async def generate_outline(scheme_id: str, request: Request, db=Depends(get_db)):
    # ⚠️ 404 校验必须留在路由体（HTTP 状态语义，前端与既有测试都依赖它）。
    #    但**装配类 await 一律不得出现在这里**（见下方 _assemble_context）。
    cur = await db.execute("SELECT * FROM schemes WHERE id=?", (scheme_id,))
    scheme = await cur.fetchone()
    if not scheme:
        from fastapi import HTTPException

        raise HTTPException(404, "方案不存在")
    # ✅ 并发竞态守卫（目录生成·自我防护，2026-10-03）：本端点此前只被 save-outline /
    #    reorder / delete / update 等"写 sections 表"的端点反向守卫（它们检查
    #    outline_generation_in_progress 以避免整表重建被覆盖），自身却从不检查
    #    "是否已有同型目录生成任务在跑"。后果：
    #    ① 同一方案被连点两次"生成目录"会并发起两路 SSE —— 两路各自写
    #       task_registry.checkpoint_json，断线兜底 / 较慢那路收尾时可能用空或旧
    #       checkpoint 覆盖先完成那路的成果；
    #    ② 与 generate_content 同理（见其守卫），两路并发无互斥。
    #    现与下游写库端点同口径：同型任务在跑（running/paused）即 409 拒绝重入。
    # ✅ P0 修复（2026-10-04 · 跨类型守卫缺口）：目录生成入口除同型互斥外，
    #    也必须检查"正文生成是否在跑"——目录生成结束会整表重建 sections，
    #    若此时正文正在写 content/word_count/图表，重建会把正文 wipe 掉，
    #    或反过来正文落库时章节 id 已被替换，图表登记挂到旧 id。
    from app.routers.sections import (
        content_generation_in_progress,
        facts_generation_in_progress,
        outline_generation_in_progress,
    )

    _og = outline_generation_in_progress(scheme_id)
    if _og:
        from fastapi import HTTPException as _HE

        logger.warning("generate_outline 409 冲突：scheme_id=%s 已有目录生成任务在跑", scheme_id)
        raise _HE(409, "本方案目录正在后台生成中，请勿重复触发（请等待当前生成完成，或在任务面板停止后再试）")
    _cg2 = content_generation_in_progress(scheme_id)
    if _cg2:
        from fastapi import HTTPException as _HE

        logger.warning("generate_outline 409 冲突：scheme_id=%s 已有正文生成任务在跑（跨类型互斥）", scheme_id)
        raise _HE(409, "本方案正文正在后台生成中，需等正文生成完成后再触发目录生成（否则重建目录会覆盖已写正文）")
    # ✅ R166（2026-11-03 · 三链互斥对称化）：事实链注册时就声明与目录/正文互斥，
    #    但目录/正文此前不反向避让事实提取 —— persist_extraction 是「DELETE 未
    #    保护行 + INSERT 新行」的作用域重写，目录/正文在重写窗内读 global_facts
    #    会拿到新旧混杂的快照，且 facts_updated_at 推进会让刚生成的成果立刻被
    #    标「事实已过期」。事实链入口注释自称「与目录/正文对称」，此处补齐对称面。
    _fg2 = facts_generation_in_progress(scheme_id)
    if _fg2:
        from fastapi import HTTPException as _HE

        logger.warning("generate_outline 409 冲突：scheme_id=%s 已有事实提取任务在跑（跨类型互斥）", scheme_id)
        raise _HE(
            409,
            "本方案事实提取正在后台进行中，需等提取完成后再生成目录"
            "（否则目录将使用新旧混杂的事实快照；可在任务面板停止提取后再试）",
        )
    scheme = dict(scheme)
    project_id = scheme["project_id"]
    project: dict = {}

    # ✅ 响应头先行（2026-09-23 · 30s 建连超时根因修复）：
    #    旧实现在返回 StreamingResponse 之前串行 await 约 8 次装配 DB 查询/写
    #    （结构化摘要、目录库参考 + ref_count 写库、事实/知识库构建）。DB 锁
    #    竞争下（busy_timeout=15s × 最多 4 次重试）最坏远超前端 sseFetch 的
    #    30s 建连超时 → 前端报「后端连接超时」，而任务注册/AI 调用尚未开始
    #    （症状：左侧显示后台未运行、无 AI 调用、日志无痕）。
    #    现把全部装配移入 _assemble_context，只在 event_stream 内部调用。
    _prog: dict = {
        "started_at": time.monotonic(),
        "phase": "prepare",
        "phase_started_at": time.monotonic(),
        "chapter_started_at": time.monotonic(),
        "sub_total": 0,  # 分步链路：一级章节总数
        "sub_done": 0,  # 分步链路：已完成子目录的章节数
        "sub_started_at": 0.0,  # 分步链路：子目录阶段起点（ETA 分母，不含准备/一级目录）
        "stage_expect": {},  # ✅ 阶段耗时 EMA 校准值（本任务内，见 _calibrate_outline_expect）
        "chapter_expect": 0.0,  # ✅ 单章子目录耗时 EMA 校准值（0 = 用出厂值）
        "failed_chapters": [],  # 子目录生成失败的章节标题
        "stepwise": False,
        "nodes": 0,  # 已生成目录节点数（增量累计，避免每章 O(n) 全量统计）
        "last_p": 0.0,  # 已推送的最大进度（保证单调不倒退）
    }
    # ✅ 断线兜底成果容器：客户端断开时生成器被 aclose，GeneratorExit 直接落在
    #    yield 处（不经过任何 except），只有 finally 能收尾。把「已生成的目录树 /
    #    失败章节」提升到闭包外的可变容器，finally 才能把它们写进 checkpoint，
    #    否则跑了数分钟的成果确定性丢失（前端只能提示"后台任务已停止"）。
    _partial_holder: dict = {"outline": [], "failed_chapters": []}
    # ✅ R106 FIX-6：SSE 泵结束时的收尾句柄。event_stream 注册成功后把 task_id
    #    写进来，with_heartbeat 的 finally 以**同步回调**读它并通知 task_registry
    #    「流已结束」——生成器自己的 finish_task 依赖一串 await，在客户端断开的
    #    取消风暴里会被再次取消（活体 E2E 实测 task=b3dc5ae0… 检查点已落库、
    #    finish_task 从未进入 → 内存态永久 running、目录 Tab 再也点不动停止）。
    _stream_holder: dict = {}
    # 装配产物：由 _assemble_context 在流内填充，event_stream 解包还原
    # （同名局部变量，下游 50+ 处引用零改动）
    _assembled: dict = {}

    def _stats_provider() -> dict:
        """心跳通道回调：返回目录生成运行统计快照（同步、绝不抛异常）。"""
        return _snapshot_outline_stats(_prog)

    def _outline_phase_cb(phase: str):
        """审核/修复子阶段回调（同步）：只更新内存状态。

        此刻调用方正 await 在审核协程上、无法 yield 事件，进度改由心跳通道
        的 stats_provider 以 ping 事件带出（每 10s 一次）。
        """
        _advance_outline_phase(_prog, phase)

    async def _assemble_context() -> None:
        """装配目录生成所需的全部上下文（**只在 SSE 流内调用**）。

        产物写入外层 _assembled 字典，由 event_stream 解包为同名局部变量。
        单个装配环节失败只降级不阻断（与旧实现的 try/except 口径一致）：
        任一环节拿不到数据时目录仍可生成，只是提示词里少一块上下文。
        """
        nonlocal project, _assembled
        # ✅ D-39（R176）：上下文降级的唯一簿记点 —— 截断/段失败都记进这里，
        #    由 event_stream 解包后以 context_degraded 事件显式下发。
        _ctx_degraded: list[dict] = []
        # ✅ 2026-09-27（要求一）：方案名称解析对象**只构建一次**，向下游三处复用
        #    （解析提取相关性前置 / 全局事实相关性前置 / 生成后全面性校验），
        #    保证「围绕方案名称」这条主线在整条链路上口径一致。
        basis = _scheme_basis_obj(scheme)
        # ✅ BUG 修复（文件解析 → 目录生成的数据传递）：旧实现在**过滤空值之前**
        #    就 LIMIT 5，且没有 ORDER BY。只要待解析/解析失败的文档恰好排在前 5 条
        #    （新上传尚未解析的文件就是这种形态），已解析资料一条都取不到 ——
        #    目录生成静默退化成"只有工程类型 + 项目名称"，用户上传的资料完全不可见。
        #    现统一走 global_facts.load_parsed_texts（非空条件下推 SQL + 固定排序）。
        try:
            cur = await db.execute("SELECT * FROM projects WHERE id=?", (project_id,))
            project_row = await cur.fetchone()
            # ✅ 修复：project 记录缺失时旧实现 dict(None) 直接 TypeError → 500
            project = dict(project_row) if project_row else {}
        except Exception:
            logger.warning("项目 %s 读取失败（目录生成降级继续）", project_id, exc_info=True)
        from app.routers.global_facts import load_parsed_texts

        try:
            # ✅ 2026-09-30 第十四轮：把「用了被截断的文档」这件事回传出来，
            #    让目录生成在 SSE 上显式告知用户（依据不完整 → 生成的目录可能
            #    漏掉后段章节）。load_parsed_texts 内部已恒记 WARNING，这里再把
            #    清单带到事件流，避免用户必须去翻日志才知道。
            _truncated_docs: list[str] = []
            docs = await load_parsed_texts(db, project_id, limit=5, truncated_out=_truncated_docs)
        except Exception:
            logger.warning("已解析文档读取失败（目录生成降级继续）", exc_info=True)
            docs = []
            _truncated_docs = []
            _record_context_degraded(
                _ctx_degraded,
                field="parsed_texts",
                stage="outline.assemble.parsed_texts",
                original_chars=0,
                kept_chars=0,
                reason=CONTEXT_DEGRADED_SEGMENT,
                segment_id="parsed_texts",
            )
        _brief_source = (
            "\n".join(docs)
            if docs
            else f"工程类型：{project.get('engineering_type','')}，" f"项目名称：{project.get('name','')}"
        )
        raw_brief = _brief_source[:4000]
        _record_context_degraded(
            _ctx_degraded,
            field="raw_brief",
            stage="outline.assemble.raw_brief",
            original_chars=len(_brief_source),
            kept_chars=len(raw_brief),
            segment_id="raw_brief",
        )
        # ✅ 跨模块数据传递接线（文件解析 → 结构化提取 → 目录生成）：
        #    旧实现只把「原始文档前 4000 字」当项目摘要 —— 原文截断点之后的
        #    关键参数（基坑深度、支护形式、工期）对目录生成不可见；且「结构化
        #    提取」（bid-analysis 20 项）的成果从未被任何下游消费。现优先消费
        #    结构化提取的 success 结果作为项目摘要，原始摘录降级为附录；
        #    未跑结构化提取时行为与旧版完全一致（回退 raw_brief）。
        try:
            project_brief = await _build_structured_brief(
                db,
                project_id,
                raw_brief,
                basis=basis,
                degraded_out=_ctx_degraded,
                degraded_stage="outline.assemble.structured_brief",
            )
        except Exception:
            logger.warning("结构化摘要构建失败（目录生成降级继续）", exc_info=True)
            project_brief = raw_brief
            _record_context_degraded(
                _ctx_degraded,
                field="structured_brief",
                stage="outline.assemble.structured_brief",
                original_chars=0,
                kept_chars=0,
                reason=CONTEXT_DEGRADED_SEGMENT,
                segment_id="structured_brief",
            )

        # ✅ 防御：config_json 被其它路径写坏时降级为空配置，避免 SSE 请求以
        #    非流式 500 失败（前端此时收不到 error 事件，只能看到通用
        #    「SSE 请求失败」）。
        try:
            config = json.loads(scheme.get("config_json") or "{}")
        except (TypeError, ValueError):
            logger.warning("方案 %s 的 config_json 非法，已降级为空配置", scheme_id)
            config = {}
        if not isinstance(config, dict):
            config = {}
        # ✅ 编制要求/评审要点（对齐 OpenBidKit score-planning 的评分大项来源）：
        #    注入目录生成（一级必须逐条覆盖）与审核（缺失判不通过并自动修复补齐）。
        requirements_text = str(config.get("requirements") or "").strip()
        library_ids = config.get("library_ids", [])
        # ✅ G1/G2 修复：使用 outline_reference 服务构建「人/模型可读」的紧凑参考
        #    文本，而非把裸 JSON 直接塞进 prompt（同等信息量下 token 浪费近 3 倍）；
        #    默认 only_approved=True 只取「已通过」目录库，避免待审核/已停用库污染
        #    AI 生成（与 apply-and-save 的强约束口径一致）；命中的库累加 ref_count，
        #    修复「仅 apply-and-save 才 +1、AI 参考不计数」的统计失真。
        reference_outline = ""
        _bump_ids: list[str] = []
        try:
            from app.services.outline_reference import (
                build_category_reference_outline,
                build_reference_outline,
                bump_ref_count,
            )

            reference_outline, _hit_ids = await build_reference_outline(db, library_ids, only_approved=True)
            _bump_ids.extend(_hit_ids or [])
            # ✅ 按类别自动匹配目录库（scheme_auto_match_outline，默认关闭）：
            #    方案自身被分类为某类危大工程后，把该类别的**标准目录模板**
            #    追加到参考里。必须**增量追加**（不允许覆盖用户选库结果），
            #    且仅在命中非空文本时才计数 —— 否则 ref_count 虚增而参考
            #    从未进入提示词（2026-09-25 修复的正是这个静默失效）。
            if getattr(settings, "scheme_auto_match_outline", False):
                # ✅ 2026-10-01 断链修复：bid_analysis 写入的 hazard_category 是
                #    **逗号连接的多类别码**（",".join(category_ids)，如
                #    "foundation_pit,scaffold"），旧代码整串当单个 type 传给
                #    IN 查询 → 多类别方案永远匹配不到任何目录库。现按逗号拆分。
                _cat_ids = [c.strip() for c in str(scheme.get("hazard_category") or "").split(",")]
                _cat_ids = [c for c in _cat_ids if c]
                if _cat_ids:
                    _cat_text, _cat_hits = await build_category_reference_outline(db, _cat_ids)
                    if _cat_text and _cat_text.strip():
                        reference_outline = (
                            (reference_outline + "\n\n" + _cat_text).strip() if reference_outline else _cat_text
                        )
                        _bump_ids.extend(_cat_hits or [])
            if _bump_ids:
                await bump_ref_count(db, _bump_ids)
                # ✅ R79 C1：本函数体内零 db.commit，而 db 是池连接、
                #    请求结束由 get_db 统一 rollback —— 不在此 commit，
                #    ref_count 递增**确定性丢失**（目录库「AI 参考也计数」
                #    的承诺对生成链从未生效）。commit 失败被外层 except
                #    吞为「目录库参考构建失败」降级继续，不阻断生成。
                await db.commit()
        except Exception:
            logger.warning("目录库参考构建失败（目录生成降级继续）", exc_info=True)
            _record_context_degraded(
                _ctx_degraded,
                field="reference_outline",
                stage="outline.assemble.reference_outline",
                original_chars=0,
                kept_chars=0,
                reason=CONTEXT_DEGRADED_SEGMENT,
                segment_id="reference_outline",
            )
        # ✅ 增强：注入结构化「项目关键事实」（与正文生成共用同一构建函数），
        #    使目录紧扣本项目的真实设计参数（开挖深度、搭设高度、地质条件、
        #    周边环境等），而非仅凭原始文档全文泛泛生成、脱离项目实际。
        # ✅ D-39（R176）：builder 的预算分配会静默截短事实正文 —— 先取一次
        #    原始可注入事实的正文总字符数（仅用于判定「是否真的切了」），
        #    正常未超预算路径不产生事件。
        _facts_char_rows: list | None = None

        async def _facts_source_chars() -> int:
            nonlocal _facts_char_rows
            if _facts_char_rows is None:
                try:
                    _facts_char_rows = await _load_facts_rows(db, scheme_id)
                except Exception:
                    _facts_char_rows = []
            return sum(len(str(_r[2] or "")) for _r in _facts_char_rows if len(_r) > 2)

        try:
            project_facts = await _build_facts_text(db, scheme_id, max_total=3000, basis=basis)
        except Exception:
            logger.warning("项目关键事实构建失败（目录生成降级继续）", exc_info=True)
            project_facts = ""
            _record_context_degraded(
                _ctx_degraded,
                field="project_facts",
                stage="outline.assemble.project_facts",
                original_chars=0,
                kept_chars=0,
                reason=CONTEXT_DEGRADED_SEGMENT,
                segment_id="project_facts",
            )
        else:
            if project_facts:
                _facts_src_chars = await _facts_source_chars()
                _record_context_degraded(
                    _ctx_degraded,
                    field="project_facts",
                    stage="outline.assemble.project_facts",
                    original_chars=_facts_src_chars,
                    kept_chars=len(project_facts),
                    segment_id="project_facts",
                )
        # ✅ L1/BUG-03（R177 · 2026-11-04）：一级目录单独按 1500 总预算构建，
        #    由 builder 统一分配「事实保底 + 字典剩余额度」，不再对 3000 文本切片。
        try:
            project_facts_outline = await _build_facts_text(
                db, scheme_id, max_total=PROJECT_FACTS_LIMIT_OUTLINE, basis=basis
            )
        except Exception:
            logger.warning("一级目录事实构建失败（降级为整案事实）", exc_info=True)
            project_facts_outline = ""
            _record_context_degraded(
                _ctx_degraded,
                field="project_facts_outline",
                stage="outline.assemble.project_facts_outline",
                original_chars=0,
                kept_chars=0,
                reason=CONTEXT_DEGRADED_SEGMENT,
                segment_id="project_facts_outline",
            )
        else:
            if project_facts_outline:
                _facts_src_chars = await _facts_source_chars()
                _record_context_degraded(
                    _ctx_degraded,
                    field="project_facts_outline",
                    stage="outline.assemble.project_facts_outline",
                    original_chars=_facts_src_chars,
                    kept_chars=len(project_facts_outline),
                    segment_id="project_facts_outline",
                )
        # ✅ 知识库注入：企业规范/工艺素材随事实一并注入目录生成
        try:
            _knowledge_text = await _build_knowledge_text(
                db, scheme_id, degraded_out=_ctx_degraded, degraded_stage="outline.assemble.knowledge_text"
            )
        except Exception:
            logger.warning("知识库素材构建失败（目录生成降级继续）", exc_info=True)
            _knowledge_text = ""
            _record_context_degraded(
                _ctx_degraded,
                field="knowledge_text",
                stage="outline.assemble.knowledge_text",
                original_chars=0,
                kept_chars=0,
                reason=CONTEXT_DEGRADED_SEGMENT,
                segment_id="knowledge_text",
            )
        if _knowledge_text:
            _kn_suffix = "\n\n【项目知识库素材】\n" + _knowledge_text
            _facts_before_kn = len(project_facts) + len(_kn_suffix)
            project_facts = (project_facts + _kn_suffix)[:PROJECT_FACTS_FULL_CAP]
            _record_context_degraded(
                _ctx_degraded,
                field="project_facts_with_knowledge",
                stage="outline.assemble.knowledge_suffix",
                original_chars=_facts_before_kn,
                kept_chars=len(project_facts),
                segment_id="knowledge_suffix",
            )
            _kn_header_len = len("\n\n【项目知识库素材】\n")
            _outline_room = max(0, PROJECT_FACTS_LIMIT_OUTLINE - len(project_facts_outline))
            _kn_outline_kept = max(0, _outline_room - _kn_header_len) if _outline_room > _kn_header_len else 0
            if _kn_outline_kept > 0:
                project_facts_outline += _kn_suffix[:_kn_outline_kept]
            if _kn_outline_kept < len(_kn_suffix):
                _record_context_degraded(
                    _ctx_degraded,
                    field="project_facts_outline_knowledge",
                    stage="outline.assemble.knowledge_suffix",
                    original_chars=len(_kn_suffix),
                    kept_chars=_kn_outline_kept,
                    segment_id="knowledge_suffix_outline",
                )
        _assembled = {
            "project_brief": project_brief,
            "reference_outline": reference_outline,
            "requirements_text": requirements_text,
            "project_facts": project_facts,
            "project_facts_outline": project_facts_outline,
            # ✅ 2026-09-27（要求一/四 · 方案名称主线强制化）：
            #    旧实现只看 schemes.type 下拉值 —— 名称写「深基坑支护」而 type 选
            #    「其它」时，危大必备 10 章约束（_DANGEROUS_REQUIRED_KEYWORDS）既不
            #    进提示词也不触发程序化覆盖预检。config.outline_name_basis 的注释
            #    （"type 命中 DANGEROUS_TYPES **或** 名称字面命中六大类危大"）承诺的
            #    正是下面这个口径，此处补齐实现（type 命中仍然生效，行为只增不减）。
            "is_dangerous": _scheme_is_dangerous(scheme),
            "construction_scope": _outline_construction_scope(scheme),
            # ✅ 2026-09-26：补齐 {scheme_basis} 的生产方（模板自 09-23 起声明却
            #    无人赋值 → 生成提示词里【方案名称解析】整段丢失，0.1 条款永不生效）
            "scheme_basis": _outline_scheme_basis(scheme),
            # ✅ 2026-09-27：方案名称解析对象单点传递（供相关性前置 + 全面性校验 + 冗余报告）
            "basis_obj": _scheme_basis_obj(scheme),
            "standards_text": _outline_standards_text(scheme),
            "_audit_outline_inputs": await _outline_input_audit(db, project_id),
            # ✅ 断链修复（R72 · 2026-10-12）：上方 L3426 已把「用了被截断的文档」
            #    采集进 _truncated_docs，注释承诺「把清单带到事件流」，但装配重构
            #    （移入 _assemble_context）时把 yield 点丢了 —— 清单只落日志不进
            #    SSE。现补进装配产物，由 event_stream 解包后以 warning 事件显式告知。
            "truncated_docs": list(_truncated_docs),
            # ✅ D-39（R176）：装配期上下文降级簿记，由 event_stream 转成
            #    context_degraded 事件；空列表在正常路径下不发任何事件。
            "context_degraded": list(_ctx_degraded),
        }

    async def event_stream():
        # ✅ 跨类型互斥（F5 · TOCTOU 收口，2026-10-05）：路由体的 pre-guard 判定与
        #    注册之间隔着「路由返回 + 响应头下发 + 生成器首次 next()」，中间必然
        #    await —— 另一路正文生成可能在窗口内通过自己的 pre-guard 并先注册，
        #    两路同时 running 会让「目录整表重建」与「正文逐章 UPDATE」并发。
        #    判定已下沉到 register_task 的 _register_lock 内，这里只消费结果。
        task_id, _conflict = await _register_task_exclusive(
            "outline_generation", project_id, scheme_id, ("content_generation", "facts_generation")
        )
        if _conflict is not None:
            _msg = "本方案正文正在后台生成中，需等正文生成完成后再触发目录生成（否则目录重建会覆盖正文）"
            logger.warning("generate_outline 跨类型互斥（注册期二次判定）：scheme_id=%s", scheme_id)
            yield f"data: {json.dumps({'event': 'error', 'task_id': _conflict.task_id, 'message': _msg}, ensure_ascii=False)}\n\n"
            return
        _stream_holder["task_id"] = task_id
        # ✅ R106 FIX-7（活体浏览器 E2E 实测「点了停止没反应」的直接成因）：
        #    task_id 必须**第一时间**下发。旧实现把 connecting 放在
        #    `_assemble_context()` 之后，而装配阶段要读文档/摘要/规范并可能发起 AI
        #    调用（实测 trace=03563da7f91a：401 → 20s 对冲，整整 30s 才走到第一个
        #    yield）。这 30s 里前端 `activeTaskIdRef` 仍停在**上一代已终结的任务 id**，
        #    用户点停止就打到 e8c726ce（后端回「任务不存在或已结束」），新任务
        #    b3dc5ae0 从未收到停止指令 —— 表现为「停止按钮完全无效」。
        #    现提前到注册之后、装配之前；事件顺序与进度口径不变
        #    （connecting 0.05 → 截断 warning → draft 0.1，进度取 max 单调不倒退）。
        #    ⚠️ 该 yield 位于主 try **之外**：客户端恰在此刻断开时不会走到主 try 的
        #    finally 兜底，由 FIX-6 的 with_heartbeat on_end 宽限回收接管。
        await update_progress(task_id, 0.05, "正在连接 AI...", event="connecting")
        yield f"data: {json.dumps({'event':'connecting','task_id':task_id,'progress':0.05,'message':'正在连接 AI...'}, ensure_ascii=False)}\n\n"
        _prog["last_p"] = 0.05
        # ✅ 响应头先行：在流内做装配（见路由体注释）。失败只降级 ——
        #    _assemble_context 内部各环节自带 try/except，这里再兜一层，
        #    保证「装配异常」永远不会让整个 SSE 以非流式 500 失败。
        try:
            await _assemble_context()
        except Exception:
            # 只读降级：_assembled 保持空字典即可（各产物 .get 默认空串），
            # 不得在此赋值 —— 那会把 _assembled 变成 event_stream 的局部名，
            # 后续读取直接抛 UnboundLocalError（正是本次要修的那类缺陷）。
            logger.warning(
                "目录生成上下文装配失败（降级为空上下文继续 · scheme_id=%s · task=%s）",
                scheme_id,
                task_id,
                exc_info=True,
            )
        # 解包还原同名局部变量（下游 50+ 处引用零改动）
        project_brief = _assembled.get("project_brief", "")
        reference_outline = _assembled.get("reference_outline", "")
        requirements_text = _assembled.get("requirements_text", "")
        project_facts = _assembled.get("project_facts", "")
        project_facts_outline = _assembled.get("project_facts_outline", "")
        construction_scope = _assembled.get("construction_scope", "")
        scheme_basis = _assembled.get("scheme_basis", "")
        basis = _assembled.get("basis_obj")
        standards_text = _assembled.get("standards_text", "")
        _audit_outline_inputs = _assembled.get("_audit_outline_inputs", "")
        is_dangerous = bool(_assembled.get("is_dangerous"))
        # ✅ 断链修复（R72）：装配阶段采集的被截断文档清单，解包后由下方
        #    warning 事件显式告知用户（依据不完整 → 目录可能漏后段章节）。
        _trunc_docs = _assembled.get("truncated_docs", [])
        # ✅ D-39（R176）：装配期上下文降级清单 —— 有内容才发事件。
        _ctx_degraded_records = _assembled.get("context_degraded") or []
        # ✅ 修复内存泄漏：事件直接 yield，不再 subscribe 未消费的队列
        try:

            async def _push_stats():
                """推送一次运行统计（不写 DB；失败静默，不影响生成）。"""
                try:
                    await update_task_stats(task_id, **_snapshot_outline_stats(_prog))
                except Exception:
                    logger.debug("目录生成运行统计推送失败（不影响生成 · task=%s）", task_id, exc_info=True)

            async def _set_phase(phase: str, msg: str, *, force: float | None = None, extra: dict | None = None) -> str:
                """切换目录生成阶段：更新状态 + 推送 progress 事件，返回待 yield 的 SSE 串。

                进度取「阶段模型折算值」与「已推送最大值」的较大者，保证单调不倒退。
                force 用于对齐既有的可见刻度（如 0.05 连接、0.55 目录已生成）；
                extra 用于随事件附带业务载荷（如长方案的目录树预览 outline）。
                ✅ 阶段切换统一走 _advance_outline_phase（顺带完成上一阶段的耗时校准 +
                计时基准重置），避免与审核子阶段回调两处各写一遍。
                """
                _advance_outline_phase(_prog, phase)
                p = _outline_push_value(_prog) if force is None else float(force)
                p = max(p, _prog.get("last_p", 0.0))
                _prog["last_p"] = p
                await update_progress(task_id, p, msg)
                payload = {
                    "event": "progress",
                    "task_id": task_id,
                    "progress": p,
                    "message": msg,
                    **_snapshot_outline_stats(_prog),
                }
                if extra:
                    payload.update(extra)
                return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

            # ✅ 断链修复（R72 · 2026-10-12）：依据不完整必须显式告知（与
            #    generate_facts 截断 warning 同一口径）。旧实现在装配阶段采集了
            #    被截断文档清单，却从未进事件流，用户看不到「目录可能漏后段章节」。
            if _trunc_docs:
                _td_msg = (
                    f"⚠️ {len(_trunc_docs)} 份文档解析不完整"
                    f"（内容被截断）：{'、'.join(_trunc_docs[:5])}"
                    + ("等" if len(_trunc_docs) > 5 else "")
                    + "；本次目录生成仅基于已解析到的部分，"
                    "可能漏掉后段章节，建议拆分文件后重新上传并解析"
                )
                yield f"data: {json.dumps({'event':'warning','task_id':task_id,'message':_td_msg}, ensure_ascii=False)}\n\n"

            # ✅ D-39（R176）：上下文被截断/装配段失败必须可见。空清单时
            #    `_context_degraded_events` 返回空列表，正常路径一条也不多发。
            for _cd_event in _context_degraded_events(task_id, _ctx_degraded_records):
                yield _cd_event

            # ✅ 防御（2026-09-19 · 目录生成深度审查）：schemes.word_budget 虽有
            #    DEFAULT 30000，但历史行 / 直接改库 / 副本导入等场景下仍可能为
            #    NULL 或字符串。旧实现 `scheme.get("word_budget", 30000)` 在键存在
            #    但值为 None 时返回 None，随后 `None > OUTLINE_STEPWISE_MIN_WORDS`
            #    抛 TypeError → 被外层 except 吞成「目录生成失败」，用户白跑。
            #    统一稳健取整：脏值一律回退默认预算，绝不让分档判断崩主流程。
            try:
                word_budget = int(scheme.get("word_budget") or 30000)
            except (TypeError, ValueError):
                word_budget = 30000
            # ✅ R175 BUG-1（2026-11-03）：分档判定收敛到 `_use_stepwise_for_budget`
            #    唯一判定点（auto/oneshot/stepwise 三档；auto 与旧裸比较逐字等价）。
            use_stepwise = _use_stepwise_for_budget(word_budget)
            _prog["stepwise"] = use_stepwise

            # ✅ R58（2026-10-10）：一次性直出的**结果标志**。
            #    True = 拿到可用目录，走原收尾；False = 失败且不值得硬失败，
            #    落到下方分步块（该块变量在入口就地初始化，天然可重入）。
            _one_shot_ok = True
            if not use_stepwise:
                await wait_resume(task_id)
                if is_stopped(task_id):
                    yield await _finish_stopped_with_partial(task_id, _partial_holder)
                    return

                yield await _set_phase("draft", "正在生成目录...", force=0.1)

                sys_prompt = render(
                    "outline_short_system",
                    scheme_name=scheme.get("name", ""),
                    scheme_type=scheme.get("type", ""),
                    construction_scope=construction_scope,
                    # ✅ 补齐：模板第 14 行的 {scheme_basis} 此前无人赋值 → 整行被
                    #    当可选区块丢弃，0.1 条款（工序/工艺/对象逐项落实）永不生效
                    scheme_basis=scheme_basis,
                    standards_text=standards_text,
                    # ✅ R38 D4：短方案主目录**有意不截断**事实（目录规模小，
                    #    全量事实收益大于成本；与长方案 level1 的 OUTLINE 档不同）
                    project_facts=project_facts or "",
                    # ✅ 2026-10-02（目录侧检查点前置）：必备法定章节 + 标题关键词
                    # （** 须置于所有关键字实参之后）
                    **_outline_checkpoint_kwargs(scheme.get("name", ""), scheme.get("type", ""), is_dangerous),
                )
                user_prompt = f"【方案名称】：{scheme.get('name','')}\n【方案类型】：{scheme.get('type','')}\n【项目资料摘要】：{project_brief[:3000]}\n"
                if construction_scope:
                    user_prompt += f"【方案名称主要施工内容】：{construction_scope}\n"
                if reference_outline:
                    user_prompt += f"【目录库参考】：{reference_outline[:2000]}\n"
                if is_dangerous:
                    user_prompt += "【注意：本方案为危大工程，目录必须包含全部必要章节】\n"
                if requirements_text:
                    user_prompt += (
                        "【编制要求（目录必须逐条覆盖，每一条都要有对应章节，缺失即为不合格）】：\n"
                        f"{requirements_text}\n"
                    )
                messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}]

                # ✅ 进度增强：目录 AI 调用最长 180s，期间用 _await_with_stats
                #    每 3s 推送一次 stats（已耗时 / ETA / 当前阶段），进度条不再静止。
                try:
                    obj, raw = await _await_with_stats(
                        collect_json_response(
                            messages,
                            _outline_validate_fn,
                            timeout=OUTLINE_REQUEST_TIMEOUT,
                            json_mode=True,
                            temperature=0.2,
                            scene="outline_draft",
                            repair_key=OUTLINE_REPAIR_KEY,
                        ),
                        _push_stats,
                        task_id=task_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    # ✅ R58：一次性直出失败（ValueError / 超时 / 连接错误等）
                    #    → 不硬失败，转分步链路。
                    #    ⚠️ 配额/限流/认证/模型不存在类错误**不切**分步 ——
                    #    分步会按「一级 + 逐章」发起十几次调用，在注定失败的错误
                    #    上全白烧；与 _fetch_chapter_children 的既有判据同口径。
                    if OUTLINE_ONESHOT_FALLBACK and not _is_quota_error(e):
                        logger.warning("目录一次性生成失败（task=%s），切换分步链路：%s", task_id, e)
                        await update_progress(task_id, _prog["last_p"], "一次性生成未完成，正在切换分步生成")
                        _one_shot_ok = False
                    else:
                        raise
                # ✅ R58（2026-10-10）：while + break 结构 —— 任一失败点
                #    直接跳出，落到下方「分步」块，而不是 finish_task failed。
                #    成功路径末尾 return，与原来 if/else 互斥语义一致。
                while _one_shot_ok:
                    outline = obj.get("outline", [])
                    if not outline:
                        logger.warning("目录一次性直出返回空目录（task=%s），切换分步链路", task_id)
                        await update_progress(task_id, _prog["last_p"], "一次性生成未完成，正在切换分步生成")
                        _one_shot_ok = False
                        break
                    # ✅ 目录限三级：硬性裁剪 + 统一重排编号/补全 children（提示词约束外的兜底保障）
                    # ✅ 清理：此处原有函数内的 `from app.services.outline_utils import
                    #    normalize_outline`（与模块顶部导入重复，徒增阅读干扰）
                    outline = normalize_outline(outline)
                    # ✅ 防御：模型返回的节点全非法（字符串/数字等）时裁剪后为空树，
                    #    旧实现会把空目录带进审核（"目录为空，无法审核"）并 completed，
                    #    前端只能显示"AI 返回空目录"。这里显式 failed，错误归因更准确。
                    if not outline:
                        logger.warning("目录一次性直出节点全非法（task=%s），切换分步链路", task_id)
                        await update_progress(task_id, _prog["last_p"], "一次性生成未完成，正在切换分步生成")
                        _one_shot_ok = False
                        break

                    _prog["nodes"] = _count_nodes(outline)
                    _partial_holder["outline"] = outline  # 断线兜底：短方案成果同样可恢复
                    # ✅ 短方案一次性直出也实时推送 outline，保证「目录树」在生成阶段即同步更新
                    #    （长方案分步路径已在每章 progress 事件中携带 outline，此处补齐短方案的一致性）
                    yield await _set_phase(
                        "draft", "目录已生成，正在构建目录树...", force=0.55, extra={"outline": outline}
                    )

                    await wait_resume(task_id)
                    if is_stopped(task_id):
                        yield await _finish_stopped_with_partial(task_id, _partial_holder)
                        return

                    yield await _set_phase("review", "目录生成完成，正在审核...")

                    await wait_resume(task_id)
                    if is_stopped(task_id):
                        yield await _finish_stopped_with_partial(task_id, _partial_holder)
                        return

                    outline, review_obj = await _review_and_fix_outline(
                        outline,
                        scheme.get("type", ""),
                        is_dangerous,
                        project_brief,
                        scheme_name=scheme.get("name", ""),
                        project_facts=project_facts,
                        requirements=requirements_text,
                        construction_scope=construction_scope,
                        scheme_basis=scheme_basis,
                        standards_text=standards_text,
                        basis=basis,
                        phase_cb=_outline_phase_cb,
                    )
                    # ✅ 2026-11-05（《L3以下“少目录、多正文”编制标准》）：
                    #    AI 生成（含审核补齐）后做确定性结构归一：L4+ 折叠为父级
                    #    L3 的「（1）（2）」段首引导（不丢内容）；L3 数量默认**不合并**
                    #    （标准对 L3 无硬性要求，超量只由 DLV-17 告警），仅当显式打开
                    #    outline_l3_converge_enforced 才合并。折叠/合并后统一重排编号。
                    _l3_res = l3_standard.converge_tree(outline)
                    if _l3_res.get("folded"):
                        logger.info(
                            "目录深层节点折叠：%d 个 L4+ 条目并入父级三级正文引导（最大层级 %d）",
                            _l3_res["folded"],
                            _l3_res["after"]["max_depth"],
                        )
                    if _l3_res.get("merged"):
                        logger.info(
                            "目录 L3 标准收敛：合并 %d 个三级条目（%d → %d，最大层级 %d）",
                            _l3_res["merged"],
                            _l3_res["before"]["l3_total"],
                            _l3_res["after"]["l3_total"],
                            _l3_res["after"]["max_depth"],
                        )
                    # ✅ 生成后自动校验（要求三/四）：连续性 + 全面性 + 冗余，
                    #    结果进日志与 review 载荷，前端"审核结论"可见。
                    _attach_quality_report(outline, basis, review_obj)
                    # ✅ 断线兜底用「修复后」的成果（审核补齐的章节不应在断线恢复时丢掉）
                    _partial_holder["outline"] = outline

                    await update_progress(task_id, 1.0, "目录生成完成", event="completed")
                    _short_payload = {
                        "event": "completed",
                        "task_id": task_id,
                        "outline": outline,
                        "review": review_obj,
                    }
                    # ✅ 收尾顺序统一（2026-09-16）：先落 checkpoint → 再置终态 → 最后
                    #    yield 事件。旧实现是 yield 完才 finish_task —— 前端收到
                    #    completed 后立即 break/断开连接时，生成器被 aclose，
                    #    finally 兜底会把**已成功完成**的任务标记成 "stopped"
                    #   （活动中心/任务列表显示"客户端断开，任务已终止"），与真实结果矛盾。
                    try:
                        await _save_outline_checkpoint(task_id, _short_payload)
                    except Exception:
                        logger.warning("检查点写入失败（task=%s · 目录生成 · 终态成果）", task_id, exc_info=True)
                    await finish_task(task_id, "completed", "目录生成完成")
                    # ✅ BUG 修复（2026-09-16 · 依据运行库最近一次任务记录）：
                    #    此处曾误写旧变量名 `_partial`（本轮改名 _partial_holder 时漏改），
                    #    短方案链路生成成功后在收尾处抛
                    #      NameError: name '_partial' is not defined
                    #    → 被外层 except 捕获并把任务置为 failed，前端收到 error 事件而
                    #    **拿不到一级目录确认闸门**（目录其实已生成、checkpoint 也已落库）。
                    #    实测：task_registry 最新一条 outline_generation 即为此故障。
                    _partial_holder["outline"] = []
                    yield f"data: {json.dumps(_short_payload, ensure_ascii=False)}\n\n"
                    return
            if use_stepwise or not _one_shot_ok:
                if not _one_shot_ok:
                    # ✅ 切档准备（仅一次性失败时才需要）：
                    #    ① 进度不倒退 —— 短路径已把 last_p 推到 0.55，而分步的
                    #       level1 是 force=0.1，会被 max(p, last_p) 顶住，
                    #       进度条从此静止。这里把 last_p 退回起点。
                    #    ② 清空短路径可能写入的脏成果，避免 finally 落库。
                    #    ③ stepwise 标记置真：前端 stats 与断线恢复都读它。
                    _prog["stepwise"] = True
                    _prog["last_p"] = 0.05
                    _prog["nodes"] = 0
                    _prog["sub_done"] = 0
                    _prog["sub_total"] = 0
                    _partial_holder["outline"] = []
                # 长方案分步生成（clamp_outline_depth / normalize_outline /
                # MAX_OUTLINE_DEPTH 已在模块顶部导入）
                await wait_resume(task_id)
                if is_stopped(task_id):
                    yield await _finish_stopped_with_partial(task_id, _partial_holder)
                    return

                yield await _set_phase("level1", "正在生成一级目录...", force=0.1)
                sys_prompt = render(
                    "outline_level1_system",
                    scheme_name=scheme.get("name", ""),
                    scheme_type=scheme.get("type", ""),
                    construction_scope=construction_scope,
                    # ✅ 同短方案链路：补齐 {scheme_basis}
                    scheme_basis=scheme_basis,
                    standards_text=standards_text,
                    project_brief=project_brief[:2000],
                    reference_outline=reference_outline[:1000] or "无",
                    project_facts=project_facts_outline,
                    # ✅ 2026-10-02（目录侧检查点前置）
                    **_outline_checkpoint_kwargs(scheme.get("name", ""), scheme.get("type", ""), is_dangerous),
                )
                messages = [{"role": "system", "content": sys_prompt}]
                if construction_scope:
                    messages.append({"role": "user", "content": f"【方案名称主要施工内容】：{construction_scope}"})
                if requirements_text:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "【编制要求（一级目录必须逐条覆盖，每一条都要有对应一级章节，缺失即为不合格）】：\n"
                                + requirements_text
                            ),
                        }
                    )
                # ✅ 进度增强：一级目录 AI 调用同样用 _await_with_stats 保活进度
                obj, _ = await _await_with_stats(
                    collect_json_response(
                        messages,
                        _level1_validate_fn,
                        timeout=OUTLINE_REQUEST_TIMEOUT,
                        json_mode=True,
                        temperature=0.2,
                        scene="outline_level1",
                        repair_key=OUTLINE_REPAIR_KEY,
                    ),
                    _push_stats,
                    task_id=task_id,
                )
                level1 = obj.get("outline", [])
                if not level1:
                    await finish_task(task_id, "failed", "AI 返回空一级目录")
                    yield f"data: {json.dumps({'event':'error','task_id':task_id,'message':'AI 返回空一级目录，请重试'}, ensure_ascii=False)}\n\n"
                    return
                _prog["sub_total"] = len(level1)
                _prog["sub_done"] = 0
                _prog["nodes"] = _count_nodes(level1)
                # ✅ R77（2026-10-14 · 目录子目录按章相关性注入事实）：
                #    旧实现对所有章节下发同一份整案事实头部（PROJECT_FACTS_LIMIT_
                #    SUBLEVEL 预算内的 project_facts 截断）—— 长方案尾部事实
                #    （监测/验收/应急…）对任何子目录都不可见。现循环外把事实行
                #    加载一次 + 按方案名称相关性排序 + 数据字典块算一次；
                #    每章渲染时按本章标题/描述把相关事实**重排**到预算头部
                #    （relevant_first_rows，只重排不删除；无关键词章节与旧行为
                #    等价）。开关 outline_sublevel_facts_relevant 设 False → 取消
                #    按章重排（整案事实仍按 1000 总预算渲染）；加载失败 → 回退
                #    已按 1500 构建好的整案事实（不再切片）。
                _outline_facts_rows: list = []
                _outline_dd_block = ""
                _sublevel_facts_relevant = bool(getattr(settings, "outline_sublevel_facts_relevant", True))
                # ✅ L1/BUG-03（R177 · 2026-11-04）：事实行不再因开关关闭而不加载 ——
                #    关闭只影响「按章重排」，整案事实仍由 builder 按 1000 总预算
                #    统一分配，避免对 1500 文本二次切片。
                try:
                    _outline_facts_rows = await _load_facts_rows(db, scheme_id)
                    _outline_facts_rows, _ = _rank_facts_by_basis(_outline_facts_rows, basis)
                    if bool(getattr(settings, "content_data_dictionary", True)):
                        from app.services.content_data_contract import (
                            build_global_data_dictionary,
                            render_data_dictionary_block,
                        )

                        _dd = build_global_data_dictionary(_outline_facts_rows)
                        _outline_dd_block = render_data_dictionary_block(_dd) or ""
                except Exception:
                    logger.warning("子目录事实行加载失败（回退整案事实）", exc_info=True)
                    _outline_facts_rows = []
                    _outline_dd_block = ""

                def _sublevel_facts_text(unit_idx: list) -> str:
                    """子目录提示词的【项目关键事实】段（R77 按章相关性重排）。

                    单元内多章（批处理）时按各章标题/描述的**并集**相关性
                    重排，保证批内每一章的相关事实都有机会进入预算头部；
                    任何失败都回退已构建好的整案事实，绝不因此阻断目录生成。
                    """
                    _fallback = project_facts_outline or project_facts
                    if not _outline_facts_rows:
                        return _fallback
                    try:
                        _leaf = {}
                        if _sublevel_facts_relevant:
                            _leaf = {
                                "title": " ".join(str(level1[i].get("title") or "") for i in unit_idx),
                                "description": " ".join(str(level1[i].get("description") or "") for i in unit_idx),
                            }
                        return render_outline_chapter_facts(
                            _outline_facts_rows,
                            _leaf,
                            max_total=PROJECT_FACTS_LIMIT_SUBLEVEL,
                            dd_block=_outline_dd_block,
                        )
                    except Exception:
                        logger.warning("子目录按章事实渲染失败（回退整案事实）", exc_info=True)
                        return _fallback

                yield await _set_phase(
                    "sublevels", f"一级目录生成完成（{len(level1)}章），正在生成二三级...", force=0.3
                )
                # ✅ ETA 口径：记录「子目录阶段」起点 —— 此后剩余时间按本阶段的
                #    实测速率外推，不再把准备阶段/一级目录 AI 的耗时摊到每章上。
                _prog["sub_started_at"] = time.monotonic()

                full_outline = []
                failed_chapters: list[str] = []
                stopped = False
                # ✅ 防御：审核环节在「中途停止」时会被跳过，此时 review_obj 未绑定。
                #    旧实现的 payload 分支恰好依赖「跳过审核 ⇒ stopped=True」的隐含
                #    不变量，任何后续改动都可能把它变成 NameError（整次生成失败）。
                review_obj: dict | None = None
                # ✅ 级间上下文增量维护（旧实现每章都遍历 full_outline 重建一次，O(n²)）：
                #    每章完成后把该章的二级标题追加进来即可。
                prior_l2: list[str] = []
                # ✅ 提示词里「其他章节标题」不随章变化，循环外算一次（旧实现每章重算）
                level1_titles = [str(c.get("title") or f"第{j + 1}章") for j, c in enumerate(level1)]

                # ---------- 批内并发编排（OUTLINE_CHAPTER_CONCURRENCY） ----------
                # 旧实现逐章**纯串行**：30 章 x ~45s ~ 22 分钟，是长方案
                # 「看起来卡死」的最大来源。现改为「批内并发 + 批间串行」：
                # 批内并发吃满许可，批间仍按章序推进以保留跨章去重上下文
                # （prior_chapters 依赖前序章的二级标题）。
                # 编排不变式：
                #   1) 批间先过暂停闸门 + 停止检查，再发起并发 gather；
                #   2) 用 asyncio.gather（保序）而非「完成序」迭代 —— 结果必须
                #      按章序号归位，否则 full_outline 章序会乱、编号全错；
                #   3) 单元内多章时按 OUTLINE_CHAPTER_BATCH_SIZE 合并为一次
                #      调用（默认 1 = 逐章，与旧行为逐字一致）。
                # ✅ R179 U5：分阶段并发读数接线（outline 阶段）。开关关 →
                #    stage_concurrency 返回 0，走 or 分支 = 逐字现状
                #    （OUTLINE_CHAPTER_CONCURRENCY）；开关开 → 阶段上限
                #    （默认 2，已夹取 max_concurrency）。
                _u5_outline_conc = ai_warmup.stage_concurrency("outline") or OUTLINE_CHAPTER_CONCURRENCY
                _sem = asyncio.Semaphore(_u5_outline_conc) if _u5_outline_conc > 1 else None
                _merge_k = max(1, OUTLINE_CHAPTER_BATCH_SIZE)
                _total = len(level1)
                # 并发窗口宽度 = 本轮并行发起的单元调用数。
                # ✅ BUG 修复（2026-10-04）：旧实现只靠 asyncio.Semaphore 表达并发，
                # 但信号量在 _fetch_chapter_children 内部才被消费，而每轮 gather
                # 恒只含**一个** _fetch_unit_children —— 信号量从未被跨章竞争，
                # OUTLINE_CHAPTER_CONCURRENCY=2 实际并发度永远为 1（配置形同虚设）。
                # 现让本常量直接决定「本轮同时发起多少个单元的调用」：
                #   _window = 1 → 每轮 1 个单元串行（与旧实现逐字等价）
                #   _window > 1 → 每轮 _window 个单元同时排队
                # 配置键沿用 outline_chapter_concurrency（默认 2 不变），仅修正语义。
                # ✅ R179 U5：并发窗口宽度同样走阶段读数（与 _sem 同源同值）。
                _window = max(1, _u5_outline_conc)

                def _build_unit_prompts(unit_idx: list, prior_ctx: list) -> tuple:
                    """为一个「执行单元」构建 (single_prompts, batch_prompt)。

                    抽成闭包是并发窗口编排的需要：窗口内 _window 个单元需要
                    各自一套提示词，若逐字复制 50 行构建逻辑，两处口径极易
                    漂移。统一从同一函数产出，保证窗口内各章的提示词口径
                    （other_outline / prior_chapters / 各类预算截断）完全一致。

                    prior_ctx 显式入参：并发窗口内的各单元共享同一份 prior_l2
                    快照（窗口起点取定），避免 await 期间被其他窗口改写导致
                    同批各章的「已生成小节」上下文不一致。
                    """
                    single_prompts: list = []
                    for i in unit_idx:
                        ch = level1[i]
                        if not ch.get("title"):
                            ch["title"] = f"第{i + 1}章"
                        other_titles = [t for j, t in enumerate(level1_titles) if j != i]
                        single_prompts.append(
                            render(
                                "outline_sublevel_system",
                                chapter_id=str(i + 1),
                                chapter_title=ch.get("title", ""),
                                chapter_desc=ch.get("description", ""),
                                scheme_name=scheme.get("name", ""),
                                scheme_type=scheme.get("type", ""),
                                construction_scope=construction_scope,
                                standards_text=standards_text,
                                project_brief=project_brief[:1500],
                                other_outline=_join_tail_budget(other_titles, 1200),
                                prior_chapters=_join_tail_budget(prior_ctx, 1500) or "无",
                                requirements=requirements_text[:1500] or "无",
                                # ✅ R77：按章相关性重排的事实（无关键词/关闭时
                                #    与旧行为逐字等价：整案事实截断）。
                                project_facts=_sublevel_facts_text([i]),
                            )
                        )
                    batch_prompt = None
                    if len(unit_idx) > 1:
                        chapters_text = "\n".join(
                            f"- chapter_id={i + 1}｜标题：{level1[i].get('title','')}"
                            f"｜说明：{level1[i].get('description','')}"
                            for i in unit_idx
                        )
                        batch_prompt = [
                            {
                                "role": "system",
                                "content": render(
                                    "outline_sublevel_batch_system",
                                    chapter_count=len(unit_idx),
                                    scheme_name=scheme.get("name", ""),
                                    scheme_type=scheme.get("type", ""),
                                    construction_scope=construction_scope,
                                    standards_text=standards_text,
                                    project_brief=project_brief[:1500],
                                    other_outline=_join_tail_budget(
                                        [t for j, t in enumerate(level1_titles) if j not in unit_idx], 1200
                                    ),
                                    prior_chapters=_join_tail_budget(prior_ctx, 1500) or "无",
                                    requirements=requirements_text[:1500] or "无",
                                    # ✅ R77：批内多章按并集相关性重排（见
                                    #    _sublevel_facts_text docstring）。
                                    project_facts=_sublevel_facts_text(unit_idx),
                                    chapters_text=chapters_text,
                                ),
                            }
                        ]
                    return single_prompts, batch_prompt

                # ✅ BUG 修复（2026-10-06 · 窗口 × 单元合并重叠）：每轮实际覆盖的
                #    章数 = _window × _merge_k（_window 个单元、每单元 _merge_k 章），
                #    而本循环步进旧实现只写 _window —— 当 _merge_k == 1（出厂默认）
                #    两者恰好相等，问题完全不可见，只有把 outline_chapter_batch_size
                #    调大（配置注释明确鼓励用户「升批」）才暴露：相邻两轮窗口重叠
                #    _merge_k - 1 章，同一章被子目录 AI 重复调用（成本翻倍），并被
                #    _merge_unit_results 二次 append 到 full_outline → 目录出现同名
                #    重复章节、编号 1..N+K 而非 1..N；若两次调用返回的 children
                #    不同，第一次的成果还会被第二次静默覆盖。步进取整轮覆盖量。
                _round_step = max(1, _window) * _merge_k
                for batch_start in range(0, _total, _round_step):
                    # 1) 批间：暂停闸门 -> 停止检查 -> 才发起并发
                    await wait_resume(task_id)
                    if is_stopped(task_id):
                        stopped = True
                        break
                    unit_idx = list(range(batch_start, min(batch_start + _merge_k, _total)))
                    _prior_ctx = list(prior_l2)
                    single_prompts, batch_prompt = _build_unit_prompts(unit_idx, _prior_ctx)
                    _window_units = [(unit_idx, single_prompts, batch_prompt)]
                    # 窗口内其余单元：沿用同一份 prior_l2 快照构建提示词
                    # （快照在窗口起点取定，避免 await 期间被其他窗口改写）
                    _u = batch_start + _merge_k
                    while _u < _total and len(_window_units) < _window:
                        _w_idx = list(range(_u, min(_u + _merge_k, _total)))
                        _w_prompts, _w_batch = _build_unit_prompts(_w_idx, _prior_ctx)
                        _window_units.append((_w_idx, _w_prompts, _w_batch))
                        _u += _merge_k
                    # 2) 并发发起（gather 保序）
                    _prog["sub_done"] = batch_start
                    _prog["chapter_started_at"] = time.monotonic()
                    _cur_chapter_at = time.monotonic()
                    _prev_done = len(full_outline)
                    results = await asyncio.gather(
                        *[
                            _fetch_unit_children(
                                [(i, level1[i]) for i in _w_idx],
                                batch_prompt=_w_batch,
                                single_prompts=_w_prompts,
                                task_id=task_id,
                                sem=_sem,
                                timeout=OUTLINE_REQUEST_TIMEOUT,
                                push_stats=_push_stats,
                            )
                            for _w_idx, _w_prompts, _w_batch in _window_units
                        ]
                    )
                    # 3) 按章序号归位（results[j] 对应 unit_idx[j]）
                    #    ✅ 2026-09-26：归位逻辑收口到 _merge_unit_results（纯函数、
                    #    可单测），并修掉「单元内遇 stopped 丢弃整批已完成章节」与
                    #    「结果缺失被静默当成成功」两处缺陷（详见该函数说明）。
                    _added_nodes = 0
                    _stopped_in_unit = False
                    for j, (_w_idx, _w_prompts, _w_batch) in enumerate(_window_units):
                        _unit_status, per = results[j]
                        # ✅ R181（2026-11-06）：空章/失败章的**单元内定向重试**。
                        #    旧实现只能靠外层整轮重跑，一个空章会让用户重烧全部章节
                        #    的配额；现只补该章（最多 1 次），结果原位替换，并经
                        #    _prog["chapter_retry"] 随 progress 快照对外可追踪。
                        per = await _retry_empty_chapters(
                            [(i, level1[i]) for i in _w_idx],
                            per,
                            _w_prompts,
                            task_id,
                            _sem,
                            OUTLINE_REQUEST_TIMEOUT,
                            _push_stats,
                            _prog.setdefault("chapter_retry", {}),
                        )
                        _st, _added = _merge_unit_results(
                            level1, _w_idx, per, full_outline, prior_l2, failed_chapters, unit_status=_unit_status
                        )
                        _added_nodes += _added
                        if _st:
                            _stopped_in_unit = True
                            break
                    _prog["nodes"] = _prog.get("nodes", 0) + _added_nodes
                    _prog["failed_chapters"] = list(failed_chapters)
                    # 断线/停止兜底：逐单元归位后立即刷新「部分成果」快照（含本窗
                    # 各单元已完成的章节），否则停止/断线时最近一批成果确定性丢失
                    _partial_holder["outline"] = full_outline
                    _partial_holder["failed_chapters"] = list(failed_chapters)
                    if _stopped_in_unit:
                        stopped = True
                        break
                    done = len(full_outline)
                    _prog["sub_done"] = done
                    # 单章耗时 EMA 校准：出厂值 45s 在网络波动/弱模型下会让
                    # 「当前章填充」迅速顶格后长时间静止，用本任务实测均速替换。
                    # 并发窗口内的章是同时执行的，样本取「窗口跨度 / 本窗新增章数」，
                    # 否则多章并发时窗口跨度会被当成单章耗时，EMA 被系统性放大。
                    _window_chapters = max(len(full_outline) - _prev_done, 0)
                    _window_elapsed = max(0.0, _cur_chapter_at - _prog.get("chapter_started_at", 0.0))
                    _calibrate_outline_chapter_expect(
                        _prog, (_window_elapsed / _window_chapters if _window_chapters else _window_elapsed)
                    )
                    _prog["chapter_started_at"] = time.monotonic()
                    # 节点数与「部分成果」快照已在归位时更新（_merge_unit_results），
                    # 此处不再重复累计 —— 旧实现在此处再 sum 一遍，与归位处的累计叠加
                    # 会让节点数翻倍（2026-09-26 抽取归位函数时一并收口）。
                    # 不可回退护栏：进度取「阶段折算值」与「已推送最大值」较大者
                    p = _outline_push_value(_prog)
                    msg = f"已完成 {done}/{_total} 章子目录"
                    await update_progress(task_id, p, msg)
                    # 预览载荷由 _build_partial_preview 深拷贝 + 全树规范化构造，
                    # 保证与收尾 normalize_outline 的结构逐节点一致
                    _preview = _build_partial_preview(full_outline)
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "event": "progress",
                                "task_id": task_id,
                                "progress": p,
                                "message": msg,
                                "done": done,
                                "total": _total,
                                "outline": _preview,
                                **_snapshot_outline_stats(_prog),
                            },
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )

                # ✅ 目录限三级：硬性裁剪 + 统一重排编号/补全 children（提示词约束外的兜底保障）
                full_outline = normalize_outline(full_outline)
                _prog["nodes"] = _count_nodes(full_outline)
                _partial_holder["outline"] = full_outline

                # ✅ 全章失败语义：AI 连续故障时每章都是"标题+空 children"，normalize 后
                # 仍非空，旧逻辑报 completed 引导用户锁定一份只有章标题的废目录。
                all_failed = not stopped and bool(level1) and len(failed_chapters) >= len(level1)
                if all_failed:
                    # ✅ 遗留修复（2026-09-16）：全部章节失败时旧实现只发一个 error
                    #    事件、不写 checkpoint —— 长方案跑完数分钟，唯一成果（一级章
                    #    标题）确定性丢失。现把 full_outline（仅一级章标题）随 checkpoint
                    #    落库，SSE 断线/刷新后 GET /sse/task/{id} 仍可取回部分成果。
                    _fail_payload = {
                        "event": "error",
                        "task_id": task_id,
                        "outline": full_outline,
                        "failed_chapters": failed_chapters,
                        "partial": True,
                    }
                    try:
                        await _save_outline_checkpoint(task_id, _fail_payload)
                    except Exception:
                        logger.warning(
                            "检查点写入失败（task=%s · 目录生成 · 全章失败时的部分成果）", task_id, exc_info=True
                        )
                    await finish_task(
                        task_id, "failed", f"全部 {len(level1)} 章的子目录生成均失败（AI 服务异常），请重试"
                    )
                    _partial_holder["outline"] = []
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "event": "error",
                                "task_id": task_id,
                                "message": "全部章节子目录生成失败，请检查 AI 服务后重试",
                                "outline": full_outline,
                                "failed_chapters": failed_chapters,
                                "partial": True,
                            },
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
                    return

                # 审核环节（与短方案一致）；审核+修复最长约 3 分钟，进入前补停止检查点
                if not stopped and full_outline and not is_stopped(task_id):
                    yield await _set_phase("review", "目录生成完成，正在审核...")
                    _pre_review_nodes = _count_nodes(full_outline)
                    full_outline, review_obj = await _review_and_fix_outline(
                        full_outline,
                        scheme.get("type", ""),
                        is_dangerous,
                        project_brief,
                        scheme_name=scheme.get("name", ""),
                        project_facts=project_facts,
                        requirements=requirements_text,
                        construction_scope=construction_scope,
                        scheme_basis=scheme_basis,
                        standards_text=standards_text,
                        basis=basis,
                        phase_cb=_outline_phase_cb,
                    )
                    # ✅ 审核/修复可能增删节点（补齐缺失章节），统计里的节点数需同步，
                    #    否则进度卡一直显示修复前的旧值。
                    _prog["nodes"] = _count_nodes(full_outline)
                    if _prog["nodes"] != _pre_review_nodes:
                        logger.info("目录审核修复后节点数变化：%d → %d", _pre_review_nodes, _prog["nodes"])
                    # ✅ 兜底：修复轮若返回空目录（模型输出异常且未触及覆盖率护栏，
                    #    例如原目录本身为空）绝不把空成果当"完成"发出去
                    if not full_outline:
                        full_outline = _partial_holder.get("outline") or []
                        _prog["nodes"] = _count_nodes(full_outline)

                    # ✅ 缺口修复（2026-10-07 · 长方案质量自检缺失）：短方案路径在
                    #    _review_and_fix_outline 后一直会调 _attach_quality_report
                    #    （连续性 / 名称覆盖 / 冗余 → review.quality），唯独长方案分步
                    #    路径漏挂 —— 而 word_budget ≥ OUTLINE_STEPWISE_MIN_WORDS 的
                    #    长方案恰恰是章节最多、最易缺章/跨章重复的场景，前端
                    #    summarizeOutlineQuality(evt.review) 对长方案永远拿不到报告，
                    #    生成日志里也没有全面性/连续性提示。现与短方案 L3987 同口径
                    #    补齐；函数内部 try/except 全兜底（fail-soft），校验异常绝不
                    #    影响生成结果，且只在真正跑过审核（非停止）时挂。
                    # ✅ 2026-11-05（L3 标准）：分步（长方案）路径同样在审核/修复后
                    #    做确定性结构归一（L4+ 折叠；L3 默认不合并），与短方案同源。
                    if full_outline:
                        _l3_res = l3_standard.converge_tree(full_outline)
                        if _l3_res.get("folded"):
                            logger.info(
                                "目录深层节点折叠（分步）：%d 个 L4+ 条目并入父级三级正文引导" "（最大层级 %d）",
                                _l3_res["folded"],
                                _l3_res["after"]["max_depth"],
                            )
                        if _l3_res.get("merged"):
                            logger.info(
                                "目录 L3 标准收敛（分步）：合并 %d 个三级条目（%d → %d，最大层级 %d）",
                                _l3_res["merged"],
                                _l3_res["before"]["l3_total"],
                                _l3_res["after"]["l3_total"],
                                _l3_res["after"]["max_depth"],
                            )
                    if full_outline and isinstance(review_obj, dict):
                        _attach_quality_report(full_outline, basis, review_obj)

                # 审核/修复期间用户点了停止：收尾为 stopped
                if not stopped and is_stopped(task_id):
                    stopped = True
                final_status = "stopped" if stopped else "completed"
                finish_msg = (
                    "用户已停止"
                    if stopped
                    else (
                        f"目录生成完成（{len(failed_chapters)}章子目录生成失败）" if failed_chapters else "目录生成完成"
                    )
                )
                # ✅ 终态进度收口：长方案最后一条 progress 停在审核/修复区间（≈0.9），
                #    而 task_registry.progress 会被「断线后重新挂接」的前端读到并展示。
                #    这里显式推到 1.0（与短方案路径的 completed 写法对齐）。
                if not stopped:
                    _advance_outline_phase(_prog, "fix")
                    # ✅ BUG 修复（2026-10-05 · F1）：短路径（:4353）一直带
                    #    event="completed"，长方案路径漏了 —— update_progress 仅当
                    #    event ∈ {completed,stopped,failed,error} 才 force=True 写库，
                    #    缺参时进度被 _PROGRESS_DB_MIN_INTERVAL(0.5s) 节流吞掉，
                    #    DB 里 status=completed 而 progress 停在最后一次节流值
                    #    （实测 0.97），前端重连后读到「已完成但没到 100%」。
                    await update_progress(task_id, 1.0, "目录生成完成", event="completed")
                event_name = "stopped" if stopped else "completed"
                payload = {"event": event_name, "task_id": task_id, "outline": full_outline}
                if not stopped and full_outline:
                    payload["review"] = review_obj if isinstance(review_obj, dict) else {}
                # 增强：长方案分步生成时，在 completed 事件中明确携带失败章节列表，
                # 前端可据此高亮显示失败章节，提示用户手动补充或重新生成。
                if failed_chapters:
                    payload["failed_chapters"] = failed_chapters
                    payload["failed_count"] = len(failed_chapters)
                # ✅ R179 N1（2026-11-05）：自动确认硬前置判定 —— 纯函数，只回传
                #    `auto_confirm{ready, blockers}`（加法式键，旧客户端忽略）。
                #    是否真正启动 8 秒倒计时由前端治理开关决定；M1 undetermined 不阻塞。
                try:
                    from app.services.outline_auto_confirm import evaluate as _evaluate_auto
                    from app.services.outline_auto_confirm import scheme_has_content as _has_content

                    payload["auto_confirm"] = _evaluate_auto(
                        full_outline,
                        review_obj if isinstance(review_obj, dict) else {},
                        failed_chapters,
                        has_content=await _has_content(db, scheme_id),
                    )
                except Exception as _e:
                    logger.warning("N1 自动确认判定失败（忽略，不阻塞 completed 事件）: %s", _e)

                # ✅ 成果可恢复 + 收尾顺序（2026-09-16 修复）：
                #    先落 checkpoint → 再置终态 → 最后 yield 事件。
                #    旧顺序（finish_task → checkpoint → yield）存在两个窗口：
                #      ① finish_task 已把任务 pop 出内存、checkpoint 尚未写入时，
                #         断线重挂接的 GET /sse/task/{id} 读到 status=completed 却
                #         没有 outline_result → 前端判定"无成果"并 load()，成果丢失；
                #      ② 反向的窗口会让已成功的任务被 finally 兜底标成 stopped。
                #
                # ✅ R89 S3：分步中段被停止时，checkpoint 事件与
                # `_finish_stopped_with_partial`（显式停止路径）**同口径带 partial**
                # —— 否则前端 `buildPartialOutlineHint` 收到 partial=undefined，
                # 把「用户主动停止、已有半份成果」当成「完整完成」展示，行为分叉。
                if stopped and full_outline:
                    payload["partial"] = True
                #
                # ✅ R89 S2：checkpoint 写失败**不得静默降级为 completed**。
                # 旧实现 except 后只打 warning、接着照常 finish_task(completed)：
                # 于是「任务状态=completed」而 checkpoint_json 为空 —— 用户刷新页面
                # 或 SSE 断线重挂时读不到 outline_result，**成果确定性丢失**，
                # 但界面告诉他「后台目录生成完成」。现在降为加法式终态 `degraded`
                # （前后端均已收口：TERMINAL_TASK_STATUSES / TaskStatusBar /
                # pollTaskUntilTerminal 均认该态），并加法式在 payload 里带上
                # `checkpoint_failed` 供前端告警；旧客户端不认识该键时行为不变。
                _ckpt_ok = True
                try:
                    await _save_outline_checkpoint(task_id, payload)
                except Exception:
                    _ckpt_ok = False
                    logger.warning(
                        "检查点写入失败（task=%s · 目录生成 · 终态成果）→ 终态降级为 degraded", task_id, exc_info=True
                    )
                if not _ckpt_ok:
                    final_status = "degraded"
                    payload["checkpoint_failed"] = True
                    finish_msg = "目录已生成，但成果未能写入检查点：刷新或断线后可能无法恢复，建议立即手动保存"
                await finish_task(task_id, final_status, finish_msg)
                _partial_holder["outline"] = []
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            yield await _finish_stopped_with_partial(task_id, _partial_holder)
        except Exception as e:
            logger.exception("目录生成失败（scheme_id=%s · task=%s）", scheme_id, task_id)
            # ✅ 增强（2026-09-16 · 依据运行库「目录已生成却以 error 收场、成果无出口」的
            #    真实实例）：终态失败时把**已生成的部分成果**随 error 事件下发 ——
            #    前端 `doGenerateOutline` 早已实现「目录生成失败，但有部分成果 →
            #    是否保存」分支（由 `evt.outline` 触发），但 error 事件此前不带
            #    outline，用户只能看到一句报错、已生成的一级/多级目录白丢。
            _err_payload = {"event": "error", "task_id": task_id, "message": str(e)}
            _err_outline = _partial_holder.get("outline") or []
            if _err_outline:
                _err_payload["outline"] = _err_outline
                _err_payload["partial"] = True
                _err_ch = _partial_holder.get("failed_chapters") or []
                if _err_ch:
                    _err_payload["failed_chapters"] = _err_ch
                    _err_payload["failed_count"] = len(_err_ch)
                try:
                    await _save_outline_checkpoint(
                        task_id,
                        {"event": "error", "partial": True, "outline": _err_outline, "failed_chapters": _err_ch},
                    )
                except Exception:
                    logger.warning("检查点写入失败（task=%s · 目录生成 · 异常终止的部分成果）", task_id, exc_info=True)
            await finish_task(task_id, "failed", str(e))
            yield f"data: {json.dumps(_err_payload, ensure_ascii=False)}\n\n"
        finally:
            # ✅ 断开兜底：客户端断开时生成器被 aclose，GeneratorExit 直接落在
            # yield 处（不经过任何 except），任务会永远停在 running、_tasks 泄漏。
            # 此处幂等清理：仅当任务尚未进入终态（finish_task 未执行过）时兜底。
            # 注意：GeneratorExit 路径下 finally 中不允许 yield。
            if has_active_task(task_id):
                # ✅ 成果可恢复（2026-09-16 修复）：断线兜底收尾前，把已生成的部分
                #    成果（长方案：已完成章的目录树；短方案：完整目录）写进 checkpoint。
                #    旧实现只标 stopped 不落库 → 前端 pollTaskUntilTerminal 拿到
                #    stopped 却取不到 outline_result，跑了数分钟的成果确定性丢失。
                try:
                    await _checkpoint_partial_outline(
                        task_id,
                        _partial_holder.get("outline") or [],
                        _partial_holder.get("failed_chapters") or [],
                    )
                except asyncio.CancelledError:
                    # 任务被取消时该 await 也可能被取消 —— 必须让位给下面的
                    # finish_task，否则任务会永远停在 running（_tasks 泄漏）
                    logger.warning("断线兜底保存目录成果被取消（task=%s）", task_id)
                except Exception:
                    logger.warning("断线兜底保存目录成果失败（task=%s）", task_id, exc_info=True)
                # ✅ R177（2026-11-05 · 断线收尾 P1）：终态落库必须 shield ——
                #    取消风暴下 finish_task 的「等待」会被打断，导致 DB 里任务永远
                #    停在 running（前端 pollTaskUntilTerminal 永远等不到终态），
                #    内存态也残留（与 bid_analysis.py 断线兜底同一范式）。
                try:
                    await asyncio.shield(finish_task(task_id, "stopped", "客户端断开，任务已终止"))
                except asyncio.CancelledError:
                    logger.info("目录生成断线兜底 finish_task 已 shield（task=%s）", task_id)
                except Exception:
                    logger.warning("目录生成断线兜底 finish_task 失败（task=%s）", task_id, exc_info=True)

    # ✅ 进度增强：审核/修复期间 event_stream 整体挂起（最长 180s），
    #    由心跳通道以 stats_provider 周期性带出阶段与进度（ping 事件）。
    return StreamingResponse(
        with_heartbeat(event_stream(), stats_provider=_stats_provider, on_end=_stream_end_guard(_stream_holder)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- 正文生成 ----------
@router.post("/generate-content/{scheme_id}")
async def generate_content(scheme_id: str, request: Request, db=Depends(get_db)):
    # 解析请求体中的可选参数
    try:
        body = await request.json()
    except Exception:
        body = {}
    # ✅ 脏 JSON 防御：请求体可能是数组/字符串/null（脚本或代理误发），
    #    旧实现直接 body.get() → AttributeError → SSE 以非流式 500 失败，
    #    前端收不到 error 事件，只看到通用"SSE 请求失败"。统一降级为空选项。
    if not isinstance(body, dict):
        body = {}
    section_id = body.get("section_id")  # 指定章节，只生成该节点及子树
    mode = body.get("mode", "all")  # all / missing / section / continue
    # ✅ 归一化（2026-10-01 第七模块专项 · C-1）：旧实现直接把原值参与
    #    `> 0` 比较与 int 运算（下方 :4556 及后续 :4587/:4643），前端或脚本若传
    #    字符串 "2000" 会抛 TypeError，被外层兜底吞成「整批正文生成失败」
    #    —— 病因与 AI 无关，排障方向被直接带偏。
    #    content_utils.normalize_word_budget_override 本就是为此类脏值而设
    #    （2026-09-23 B4），此前在本文件 import 了却**从未使用**，现补上接线。
    #    归一规则保持向后兼容：None/缺省/非法 → None（沿用各章自身预算）。
    word_budget_override = normalize_word_budget_override(
        body.get("word_budget_override")
    )  # 覆盖字数预算（正整数或 None）
    # ✅ R66（2026-10-11）四档字数控制：word_budget_tier ∈ {brief, standard,
    #    detailed, ultra}（或档位数值 800/1200/2000/2500）。命中档位时**每个叶子
    #    章节**的目标字数 = max(档位值, LEAF_MIN_WORD_BUDGET=300)，跳过旧的
    #    「按二级章节总量 AI 分配」链路；未传/非法 → None，行为与旧版逐字一致。
    word_budget_tier = resolve_word_budget_tier(body.get("word_budget_tier"))
    concurrency = body.get("concurrency")  # 并发档位：slow/balanced/fast 或整数
    # ✅ P0 修复（2026-09-19）：本次任务的有效并发 = 用户档位（归一为 1~5 整数）；
    #    未传档位时回退 AI 配置的目标档位（controller.target），而不是被自适应
    #    漂移污染的 controller.current。后续以**每任务独立信号量**严格执行。
    try:
        _conc_default = int(getattr(concurrency_controller, "target", 3) or 3)
    except (TypeError, ValueError):
        _conc_default = 3
    effective_concurrency = resolve_concurrency(concurrency, default=max(1, min(5, _conc_default)))
    # ✅ 布尔选项一律走 _coerce_bool（字符串/数字安全）：body.get(x, False)
    #    直接返回原值，前端若传 "false"（字符串）会被 Python 真值判为 True ——
    #    用户明明关了"超字数自动压缩"，却被白跑一轮 AI 压缩调用。
    force_rewrite = _coerce_bool(body.get("force_rewrite"), default=False)
    # ✅ 超字数自动压缩（可选）：生成后对「超过目标 130%（over）」的
    #    章节自动跑「AI 局部 replace/delete 压缩」，与手动「压缩本章」同一套实现
    #    （services/content_shrink.shrink_content_rounds）。
    #    未显式传值时的默认（✅ R66 2026-10-11 调整）：
    #      · 传了 word_budget_tier 或 word_budget_override → **默认开**——
    #        用户明确设定了全局字数标准，超写章节却从不纠偏正是本轮要修的缺陷；
    #      · 两者都没传 → 维持旧默认**关**（每章额外压缩调用有费用，
    #        逐章自定义预算的既有场景行为不变）。显式 true/false 永远优先。
    #    ⚠️ R70（2026-10-12）体检订正依据：**默认值本轮保持不动**，但 R66 当年
    #    写在这里的「实测均值 136.9% 系统性超写」是**含标点字符口径**
    #    （text_word_count 把中文标点、| - # * >、URL、空白换行全计入），不等于
    #    「用户多读了三成文字」。同批 33 章语料按可读口径重算：mean 136.9% →
    #    106.5%，触发 >1.3× 的章节 19/33 → 4/33（对照读数已接进 preflight
    #    DLV-16 明细，函数见 services/content_utils.readable_word_count）。
    #    ⇒ 默认开仍保留（4/33 是真实超写，且翻默认会重排每章 AI 调用成本），
    #    但不再以「系统性超写」为论据；若要切口径，800/1200/2000/2500 这几档
    #    预算必须同轮下调（÷1.285），否则真实目标被隐形放大近三成。
    _shrink_default = word_budget_tier is not None or word_budget_override is not None
    auto_shrink_over = _coerce_bool(body.get("auto_shrink_over"), default=_shrink_default)

    # ✅ 自动流程：正文生成后自动执行全文一致性 Agent 修复
    #    （扫描 → 仲裁 → 定向修复 → 待用户在质量管理界面确认/回滚），默认开启。
    auto_consistency_repair = _coerce_bool(body.get("auto_consistency_repair"), default=True)
    # ✅ 一致性修复的「全文重写」强度开关（默认关）：关 = 只做定向修复，
    #    开 = 允许对冲突章节做整章重写（更彻底但 AI 调用与字数成本显著更高）。
    force_full_repair = _coerce_bool(body.get("force_full_repair"), default=False)
    consistency_severity = str(body.get("consistency_severity") or "high")
    if consistency_severity not in ("high", "medium", "low"):
        consistency_severity = "high"

    # F-CONTENT-STANDARD(2026-09-26): generation standard request params
    task_standard = normalize_standard(body.get("task_standard"))
    override_section_standard = _coerce_bool(body.get("override_section_standard"), default=False)

    cur = await db.execute("SELECT * FROM schemes WHERE id=?", (scheme_id,))
    row = await cur.fetchone()
    if not row:
        from fastapi import HTTPException

        raise HTTPException(404, "方案不存在")
    # ✅ 并发竞态守卫（正文生成·自我防护，2026-10-03）：本端点此前只被 save-outline /
    #    reorder / delete / update / reset-content 等"写 sections 表"的端点反向守卫
    #    （它们检查 content_generation_in_progress），自身从不检查"是否已有同型正文
    #    生成任务在跑"。而 generate_content 落库走 _persist_section 的 _db_write_lock，
    #    该锁是**每任务实例独立**的 —— 两路并发正文生成各自持独立锁、彼此无互斥，
    #    章节正文（content / word_count / 图表登记）互相覆盖与丢失更新。
    #    现与下游写库端点同口径：同型任务在跑（running/paused）即 409 拒绝重入。
    # ✅ P0 修复（2026-10-04 · 跨类型守卫缺口）：除同型任务外，正文生成也必须
    #    检查"目录生成是否在跑"——目录生成结束会整表重建 sections，此时正文
    #    生成正在写 content/word_count/图表登记，两者并发会出现：
    #    ① 目录生成重建 sections 时把已写正文 wipe 掉（正文丢失）；
    #    ② 正文落库时目录已被重建，章节 id 对不上，图表登记挂到已删除的旧 id。
    #    与目录生成入口的对称守卫（见下方 generate_outline 段）同口径：
    #    任一类型的任务在跑（running/paused）即 409 拒绝重入。
    from app.routers.sections import (
        content_generation_in_progress,
        facts_generation_in_progress,
        outline_generation_in_progress,
    )

    _cg = content_generation_in_progress(scheme_id)
    if _cg:
        from fastapi import HTTPException as _HE

        logger.warning("generate_content 409 冲突：scheme_id=%s 已有正文生成任务在跑", scheme_id)
        raise _HE(409, "本方案正文正在后台生成中，请勿重复触发（请等待当前生成完成，或在任务面板停止后再试）")
    _og2 = outline_generation_in_progress(scheme_id)
    if _og2:
        from fastapi import HTTPException as _HE

        logger.warning("generate_content 409 冲突：scheme_id=%s 已有目录生成任务在跑（跨类型互斥）", scheme_id)
        raise _HE(409, "本方案目录正在后台生成中，需等目录生成完成后再触发正文生成（否则正文会被目录重建覆盖）")
    # ✅ R166（2026-11-03 · 三链互斥对称化）：与 generate_outline 同口径 ——
    #    正文逐章注入的事实来自 _load_facts_rows 快照，事实提取中途重写
    #    global_facts 会让后生成的章节用到与先生成章节不同的事实集，
    #    全文口径自相矛盾；且生成后事实修复链（content_fact_autofix）按
    #    终态事实回灌，与注入期快照错位。事实提取在跑即 409。
    _fg3 = facts_generation_in_progress(scheme_id)
    if _fg3:
        from fastapi import HTTPException as _HE

        logger.warning("generate_content 409 冲突：scheme_id=%s 已有事实提取任务在跑（跨类型互斥）", scheme_id)
        raise _HE(
            409,
            "本方案事实提取正在后台进行中，需等提取完成后再生成正文"
            "（否则正文将使用新旧混杂的事实快照；可在任务面板停止提取后再试）",
        )
    scheme = dict(row)
    project_id = scheme["project_id"]

    # ✅ 进度增强（2026-09-15）：正文生成的实时进度状态。
    #    定义在 event_stream 之外，使心跳通道的 stats_provider 也能读到 ——
    #    单章 AI 调用期间 event_stream 整体挂起、无法 yield，只有心跳通道仍在
    #    运行，这是消除「进度条长时间静止」的关键。
    _prog: dict = {
        "started_at": time.monotonic(),
        "total": 0,
        "done": 0,  # 已处理章节数（含成功与失败）
        "failed": 0,
        "words": 0,  # 已落库正文累计字数
        "running": {},  # section_id -> {title, index, stage, stage_label, started_at, stage_started_at}
        "section_ms": [],  # 每章实测耗时（秒），用于「平均单章耗时」统计
        "concurrency": 0,  # 并发档位（严格 = 用户选择，见 effective_concurrency）
        "phase": "prepare",
        "phase_label": "准备中",
        # ✅ 章节阶段起点：ETA 只按「章节生成阶段」的耗时外推，
        #    不含准备阶段（字数分配 AI 最长 180s）与全文一致性阶段。
        "sections_started_at": None,
        # ✅ 阶段耗时在线校准值（stage -> expect 秒），见 _calibrate_stage_expect
        "stage_expect": {},
        # ✅ 对外进度历史最大值（_monotonic_progress 护栏）
        "progress_max": 0.0,
    }
    # ✅ 并发档位严格 = 用户选择（每任务独立信号量，见 guarded_gen 的 _run_semaphore），
    #    不再读取全局自适应控制器的漂移值作展示 —— 展示值与实际闸门值是同一个数。
    _prog["concurrency"] = effective_concurrency

    def _stats_provider() -> dict:
        """心跳通道回调：返回运行统计快照（同步、绝不抛异常）。"""
        return _snapshot_stats(_prog)

    # ✅ R109（P0）：正文链接 on_end 宽限回收 —— R106 FIX-6 只给目录链接了
    #    on_end，正文链/事实链断连后 mark_stream_ended 永不触发、内存态永远 running、
    #    reap_orphan_tasks 的 _abandoned() 因 stream_ended_at 缺失而豁免 —— 只能重启进程。
    #    holder 在 event_stream 内拿到 task_id 后写入，on_end 同步回调读取。
    _content_holder: dict = {}

    async def event_stream():
        # ✅ 跨类型互斥（F5 · TOCTOU 收口，2026-10-05）：与 generate_outline 对称 ——
        #    pre-guard（上方 outline_generation_in_progress）与本注册之间存在
        #    await 窗口，判定下沉到 register_task 的 _register_lock 内。
        task_id, _conflict = await _register_task_exclusive(
            "content_generation", project_id, scheme_id, ("outline_generation", "facts_generation")
        )
        _content_holder["task_id"] = task_id
        if _conflict is not None:
            _msg = "本方案目录正在后台生成中，需等目录生成完成后再触发正文生成（否则正文会被目录重建覆盖）"
            logger.warning("generate_content 跨类型互斥（注册期二次判定）：scheme_id=%s", scheme_id)
            yield f"data: {json.dumps({'event': 'error', 'task_id': _conflict.task_id, 'message': _msg}, ensure_ascii=False)}\n\n"
            return
        # ✅ 断线兜底成果清单：客户端断开时 GeneratorExit 直接落在 yield 处（不经过
        #    任何 except），只有 finally 能收尾；而 finally 看不到 try 内部定义的
        #    done_ids / _failed_reasons。把清单提升到闭包外层容器，逐章维护，
        #    finally 才能把「本次生成了几章、哪几章失败」写进 checkpoint。
        # ✅ 本次运行的质量计数（超字数章节数）：依据运行库日志，超字数是最高频的
        #    质量缺陷（96 章中 72% 判 over、均值 1.69 倍）——完成消息里必须让用户
        #    看到"本次有多少章超字数"，否则无从得知可以压缩。
        _stat: dict = {"over": 0, "audit_rows": []}
        # F-CONTENT-STANDARD(2026-09-26 · B2): 生成标准校验汇总累加器。
        # 与 _stat 同级提升到闭包外层容器（早期失败路径也必须可读，不能 NameError），
        # 逐章在落库后累加，completed/stopped 终态一次性下发。
        _std_sum: dict = {"precise": 0, "fuzzy": 0, "issue_sections": 0, "total_issues": 0, "errors": 0, "warnings": 0}
        # ✅ R77b（2026-10-15）：事实冲突定点修复终态汇总。与 _std_sum 同处
        #    初始化（闭包外层，早期失败路径可读）；无冲突章节不进汇总，
        #    载荷与功能引入前同构（空汇总全 0）。累加经纯函数
        #    accumulate_fact_autofix（单一计数口径，勿内联重复实现）。
        _fa_sum: dict = empty_fact_autofix_summary()
        # ✅ 修复内存泄漏：事件直接 yield，不再 subscribe 未消费的队列
        # ✅ E1 修复（2026-09-25 · 顺序护栏）：进度更新器必须在 try 之前建立。
        #    `if not leaves:`（无待生成章节）是 try 内的早退分支，它要调用
        #    _update_progress_safe；而该名字在闭包内被赋值 ⇒ Python 编译期即
        #    判定为**局部变量**，若定义写在 try 内靠后位置（章节树加载之后），
        #    早退分支取到的不是外层值而是直接 UnboundLocalError →
        #    被外层 except 吞成「正文生成失败」，而真实原因只是「本来就没有
        #    可生成的章节」。写锁同理必须与其成对提前建立。
        # ✅ B1（2026-09-23 · 早期失败二次崩溃）：收尾状态同样必须在 try
        #    **之前**就绪。旧实现把 total / done_ids / _failed_* / gen_runner
        #    定义在 try 内靠后位置，而 except / finally 收尾要读它们 ——
        #    早期异常（如章节树查询失败）时这些名字尚未绑定，收尾本身再抛
        #    NameError，任务永久卡在 running（前端显示"后台未运行"、无日志）。
        total = 0
        done_ids: set = set()
        _failed_ids: set = set()
        _failed_reasons: dict = {}
        gen_runner = None
        _ckpt: dict = {"done": 0, "total": 0, "failed_sections": [], "words": 0}
        # ✅ R105（2026-10-18 · G1/G3 留痕容器）：正文生成的**实际派发序**
        #    （_dispatched_order，进入并发闸门的时刻逐位追加——信号量对等待者
        #    FIFO 放行 ⇒ 放行序即启动序）与**期望序**（_expected_order，本次
        #    目标叶子的目录 DFS 序）。终态由唯一拼装点逐位比对并落
        #    checkpoint.order_check，失守即 WARNING 告警（_order_warned 只报一次，
        #    多条收尾路径共用同一闸）。_fact_audit_by_leaf 记逐章「注入了哪些
        #    事实 / 因预算丢了哪些事实」（G3 留痕，随章节报告落库 + 终态汇总）。
        #    与 total/done_ids 同一纪律：必须在 try **之前**初始化，早期异常
        #    收尾路径引用它们不得 NameError（B1 教训）。
        _dispatched_order: list = []
        _expected_order: list = []
        _fact_audit_by_leaf: dict = {}
        # ✅ R179 Phase F（2026-11-06）：开关打开时逐章记录混合生成状态
        #    （路由 / 允许数值集合 / 检索结果），供生成后置校验与终态报告使用。
        _hybrid_by_leaf: dict = {}
        # ✅ 2026-11-07 检索重做：文种上下文只算一次，逐节复用；
        #    检索预算按方案维度记账（超限进报告，不静默丢弃）。
        _hybrid_category_ids: tuple[str, ...] = ()
        _hybrid_template_key: str = ""
        _hybrid_search_budget: ClauseSearchBudget | None = None
        if bool(getattr(settings, "hybrid_generation_enabled", False)):
            try:
                _raw_cls = scheme.get("scheme_classification_json")
                _parsed_cls = json.loads(_raw_cls) if isinstance(_raw_cls, str) and _raw_cls.strip() else _raw_cls
                if not isinstance(_parsed_cls, dict):
                    _parsed_cls = {}
                _hybrid_category_ids = tuple(
                    str(cid) for cid in (_parsed_cls.get("category_ids") or ()) if str(cid or "").strip()
                )
                _hybrid_template_key = str(_parsed_cls.get("template_key") or "")
                if not _hybrid_category_ids and not _hybrid_template_key:
                    from app.services.scheme_classification import build_genre_tag

                    _genre = build_genre_tag(str(scheme.get("name") or ""))
                    _hybrid_category_ids = tuple(_genre.category_ids)
                    _hybrid_template_key = str(_genre.template_key or "")
                _hybrid_search_budget = ClauseSearchBudget(
                    limit=int(getattr(settings, "hybrid_clause_search_budget", 500) or 500)
                )
            except Exception:  # noqa: BLE001 - 文种解析失败不得阻断生成，后续逐节落 no_standard_code
                logger.warning("Phase F 文种上下文解析失败（后续章节按 no_standard_code 记录）", exc_info=True)
                _hybrid_category_ids = ()
                _hybrid_template_key = ""
                _hybrid_search_budget = ClauseSearchBudget(
                    limit=int(getattr(settings, "hybrid_clause_search_budget", 500) or 500)
                )
        _order_warned: dict = {"flag": False}

        # ✅ B1 拆分（2026-11-03）：三个终态拼装点已下沉到
        #    app/routers/_sse_content_payloads.py（显式收参、不读闭包）。
        #    此处保留同名薄包装 —— 调用点、源码锚点与 monkeypatch 语义不变。
        #    _word_tol 提前置 None：该名字原先在 try 内绑定，薄包装收参时不得
        #    NameError；None 表示「容差未建立」，拼装点按拆分前降级口径记空汇总。
        _word_tol: float | None = None

        def _content_ckpt_payload(event: str, message: str = "") -> dict:
            """正文生成终态 checkpoint 的唯一拼装点（实现见 _sse_content_payloads）。"""
            return build_content_checkpoint(
                event,
                message,
                ckpt=_ckpt,
                task_id=task_id,
                total=total,
                done_ids=done_ids,
                failed_ids=_failed_ids,
                failed_reasons=_failed_reasons,
                prog=_prog,
                run_stats=_stat,
                word_tol=_word_tol,
                fact_autofix_summary=_fa_sum,
                dispatched_order=_dispatched_order,
                expected_order=_expected_order,
                fact_audit_by_leaf=_fact_audit_by_leaf,
                order_warned=_order_warned,
            )

        def _stopped_payload(message: str, progress: float | None = None) -> dict:
            """`stopped` 终态事件的唯一拼装点（实现见 _sse_content_payloads）。

            progress 省略时按当前加权进度取值（_progress_now 过不可回退护栏）。
            """
            _p = _progress_now() if progress is None else float(progress)
            return build_stopped_payload(
                message,
                progress=_p,
                task_id=task_id,
                total=total,
                done_ids=done_ids,
                failed_reasons=_failed_reasons,
                ckpt=_ckpt,
                standard_summary=_std_sum,
                fact_autofix_summary=_fa_sum,
                fact_audit_by_leaf=_fact_audit_by_leaf,
            )

        def _error_payload(message: str, *, include_sections: bool = True, progress: float | None = None) -> dict:
            """`error` 终态事件的唯一拼装点（实现见 _sse_content_payloads）。"""
            _p = _progress_now() if progress is None else float(progress)
            return build_error_payload(
                message,
                include_sections=include_sections,
                progress=_p,
                task_id=task_id,
                total=total,
                done_ids=done_ids,
                failed_reasons=_failed_reasons,
                ckpt=_ckpt,
                standard_summary=_std_sum,
                fact_autofix_summary=_fa_sum,
                fact_audit_by_leaf=_fact_audit_by_leaf,
            )

        _db_write_lock = asyncio.Lock()
        _update_progress_safe = _locked_progress_updater(_db_write_lock, task_id)
        try:
            # ✅ R66（2026-10-11）字数可追溯：audit_rows 逐章累积
            #    {section_id,title,level,word_budget,word_count}，终态由
            #    word_audit_summary 汇总进 checkpoint / completed 载荷（审核预检可读）；
            #    _word_tol 为容差带半宽（默认 0.1 = 提示词承诺的 0.9~1.1 倍）。
            try:
                _word_tol = abs(float(getattr(settings, "word_budget_tolerance", 0.1)))
            except (TypeError, ValueError):
                _word_tol = 0.1
            _prog["phase"], _prog["phase_label"] = "prepare", "准备中"
            _prog["progress_max"] = _monotonic_progress(_prog["progress_max"], 0.02)
            # ✅ 写锁口径统一：建立写锁之后的所有进度写入一律走
            #    _update_progress_safe（持锁 + 失败静默），与章节落库事务互斥。
            #    直调 update_progress 会与并发章节写事务争 SQLite 写锁。
            await _update_progress_safe(_prog["progress_max"], "正在加载章节树...", event="connecting")
            yield f"data: {json.dumps({'event':'connecting','task_id':task_id,'progress':_prog['progress_max'],'message':'正在加载章节树...'}, ensure_ascii=False)}\n\n"

            # 1) 先加载本方案所有章节（完整列表，用于构建上下文）
            # ✅ R13 判空（2026-10-05 · 正文生成 R-4）：本仓库 db.execute() 在全局
            #    单连接 + aiosqlite 下**可能返回 None**（连接/事务异常，见 AGENTS.md
            #    §5.5 R13 事故）。此处是正文生成的**第一个数据读取点**，旧实现直接
            #    `await cur.fetchall()` → AttributeError → 被外层 except Exception
            #    整段吞掉，终态报「正文生成失败：'NoneType' object has no
            #    attribute 'fetchall'」—— 病因文案把本地存储层问题说成业务失败，
            #    排障方向被带偏（与 2026-09-29「38/38 章全失败」事故同族）。
            #    降级语义：读到 0 章 → 走既有的「没有待生成的章节」早退分支，
            #    仍是**正常终态**（checkpoint + completed），不产生孤儿任务。
            cur = await db.execute("SELECT * FROM sections WHERE scheme_id=? ORDER BY sort_order", (scheme_id,))
            all_sections = [dict(r) for r in await cur.fetchall()] if cur is not None else []
            if cur is None:
                logger.warning("正文生成：章节树查询返回 None（R13），按「无可生成章节」降级；scheme=%s", scheme_id[:8])

            # ✅ P0 修复（2026-09-19 · 目录顺序）：sort_order 是「同级内序号」而非全局
            #    序号，ORDER BY sort_order 会把不同层级的同序号节点排在一起（按"列"
            #    展开）。重排为目录树前序 DFS：生成任务排队、进度序号、前序同级参考
            #    均严格按目录层级递进，保证全文结构一致性。
            all_sections = order_sections_dfs(all_sections)
            # ✅ BUG 修复（2026-09-26）：{subheading_rule} 需要「本章是否有 DB
            #    子章节」才能选命名空间。all_sections 是完整列表，直接取所有
            #    parent_id 即可得子章节 id 集合（零额外 DB 往返）。
            _parent_ids = {str(s.get("parent_id") or "") for s in all_sections if str(s.get("parent_id") or "")}

            # 2)+3) 目标叶子章节筛选（纯函数，规则与测试见 services/content_utils.py）
            leaves = select_target_leaves(all_sections, section_id=section_id, mode=mode, force_rewrite=force_rewrite)

            # 2.5) ✅ 2026-11-05 目录占位章节（计算书/附图/附件/布置图/详图/系统图 等）：
            #    判定唯一事实源 = services/placeholder_sections（标题归一化后子串命中）。
            #    这些章节**不进入生成任务**：不调用 AI（含字数分配调用）、不计入
            #    进度分母，直接登记固定文本「另行补充内容」并保留标题/层级/编号。
            #    存量方案不迁移，再次生成/续写命中时在此覆盖旧正文（含人工编辑）。
            leaves, _placeholder_leaves = split_placeholder_leaves(leaves)
            if _placeholder_leaves:

                async def _persist_placeholders_once():
                    await persist_placeholder_sections(db, _placeholder_leaves)

                async with _db_write_lock:
                    await retry_db_op(_persist_placeholders_once, max_retries=3, base_delay=0.1)
                logger.info(
                    "目录占位章节：%d 章按规则登记「%s」，跳过 AI 生成；scheme=%s",
                    len(_placeholder_leaves),
                    PLACEHOLDER_CONTENT,
                    scheme_id[:8],
                )

            # 4) ✅ 字数设置（2026-09 新语义）：override = 二级章节目标字数
            #    - 二级章节（level<=2）为预算单元，其总字数严格受控
            #    - 单元内各叶子（三级等）由 AI 智能分配；AI 失败降级为均分
            #    - 不属于任何二级单元的叶子（自身即单元）直接使用 override
            # ✅ R66（2026-10-11）四档字数控制（word_budget_tier）：与上面的
            #    「按二级单元总量分配」语义**互斥**——档位是「每个叶子（三级目录）
            #    各自的目标字数」，直接绑定叶子、不做单元分配，且不低于
            #    LEAF_MIN_WORD_BUDGET=300（三级目录 300 字左右正文要求）。
            #    未传档位时本分支整体跳过，override/默认路径行为逐字不变。
            if word_budget_tier is not None:
                _tier_budget = max(int(word_budget_tier), LEAF_MIN_WORD_BUDGET)
                for lf in leaves:
                    lf["word_budget"] = _tier_budget
                try:
                    await db.executemany(
                        "UPDATE sections SET word_budget=? WHERE id=?", [(_tier_budget, lf["id"]) for lf in leaves]
                    )
                    await db.commit()
                except Exception as e:
                    logger.warning("档位字数持久化失败（不影响生成）: %s", e)
                logger.info(
                    "字数预算：档位=%s（每叶子 %d 字，下限 %d 字），%d 个叶子",
                    body.get("word_budget_tier"),
                    _tier_budget,
                    LEAF_MIN_WORD_BUDGET,
                    len(leaves),
                )
            elif word_budget_override and word_budget_override > 0:
                by_id = {s["id"]: s for s in all_sections}

                def _find_unit(sec: dict):
                    """确定叶子的预算单元：叶子自身为二级时单元=自身；
                    否则向上找最近的 level<=2 祖先"""
                    if int(sec.get("level") or 1) <= 2:
                        return sec
                    pid = sec.get("parent_id", "")
                    while pid:
                        p = by_id.get(pid)
                        if not p:
                            return None
                        if int(p.get("level") or 1) <= 2:
                            return p
                        pid = p.get("parent_id", "")
                    return None

                units: dict[str, dict] = {}
                for leaf in leaves:
                    unit = _find_unit(leaf)
                    if unit is None:
                        unit = leaf  # 叶子自身就是二级/一级（无子树）→ 直接使用 override
                    u = units.setdefault(unit["id"], {"unit": unit, "leaves": []})
                    u["leaves"].append(leaf)

                # 构造 AI 分配输入
                units_payload = []
                for u in units.values():
                    unit, unit_leaves = u["unit"], u["leaves"]
                    if len(unit_leaves) == 1:
                        u["alloc"] = {unit_leaves[0]["id"]: word_budget_override}
                        continue
                    units_payload.append(
                        {
                            "unit_id": unit["id"],
                            "title": unit.get("title", ""),
                            "description": unit.get("description", ""),
                            "total_budget": word_budget_override,
                            "children": [
                                {"id": lf["id"], "title": lf.get("title", ""), "description": lf.get("description", "")}
                                for lf in unit_leaves
                            ],
                        }
                    )

                # AI 智能分配（一次调用处理全部单元）；失败降级均分
                allocations: dict[str, int] = {}
                if units_payload:
                    try:
                        # ✅ 停止/暂停检查点：分配调用可能耗时数分钟且位于所有章生成之前
                        await wait_resume(task_id)
                        if is_stopped(task_id):
                            raise asyncio.CancelledError()
                        alloc_prompt = render(
                            "word_budget_allocate_system", units_json=json.dumps(units_payload, ensure_ascii=False)
                        )
                        alloc_obj, _ = await collect_json_response(
                            [
                                {"role": "system", "content": alloc_prompt},
                                {"role": "user", "content": "请输出字数分配结果。"},
                            ],
                            lambda o: [] if isinstance(o, dict) and "allocations" in o else ["缺少 allocations 字段"],
                            timeout=OUTLINE_REQUEST_TIMEOUT,
                            # ✅ 场景归因：不打 scene 时 /ai/stats 的 by_scene
                            #    看不到本次字数分配调用量（该调用最长 180s，
                            #    是正文生成首段的隐藏耗时来源）。
                            json_mode=True,
                            temperature=0.2,
                            scene="word_budget_alloc",
                        )
                        raw_alloc = alloc_obj.get("allocations", {})
                        if isinstance(raw_alloc, dict):
                            allocations = {
                                str(k): int(v) for k, v in raw_alloc.items() if isinstance(v, (int, float)) and v > 0
                            }
                    except Exception as e:
                        logger.warning("字数 AI 分配失败，降级均分: %s", e)

                    # ✅ 修复：遍历 units（含 leaves）写入 u["alloc"]。
                    #    原代码误遍历 units_payload（仅含 children、无 leaves）导致
                    #    KeyError；且分配结果写回的是临时 units_payload 而非 units，
                    #    后续 apply 阶段读取 u["alloc"] 再次 KeyError，使整个生成任务崩溃。
                    _apply_word_budget_allocations(units, word_budget_override, allocations)

                # ✅ 修复：应用分配 + 持久化移到 if units_payload 外部。
                # 旧实现仅在存在多叶子单元时才持久化，全单叶子单元（units_payload为空）时
                # word_budget 完全不写入 DB，导致 UI 展示与续写判断使用旧值/默认值。
                # 现无论 AI 分配成功还是降级均分，也无论是否有单叶子单元，都统一持久化。
                for u in units.values():
                    for lf in u["leaves"]:
                        # ✅ R66（2026-10-11）：叶子预算下限 300 字（三级目录
                        #    正文要求）。刻意放在 sse 应用循环而非
                        #    _apply_word_budget_allocations 内部——后者被性能用例
                        #    以 total_budget=100 直接调用并断言守恒，floor 入内会
                        #    打挂既有契约。override 极小且叶子极多的旧场景下，
                        #    单元总量从此可能超过 override（300 字要求优先）。
                        lf["word_budget"] = max(u["alloc"].get(lf["id"], word_budget_override), LEAF_MIN_WORD_BUDGET)
                try:
                    # ✅ P1-1 性能优化：逐条 UPDATE → executemany 单次提交
                    await db.executemany(
                        "UPDATE sections SET word_budget=? WHERE id=?",
                        [(lf["word_budget"], lf["id"]) for u in units.values() for lf in u["leaves"]],
                    )
                    await db.commit()
                except Exception as e:
                    logger.warning("字数分配持久化失败（不影响生成）: %s", e)

                total_units = len(units)
                logger.info(
                    "字数预算：override=%d，%d 个二级预算单元，%d 个叶子",
                    word_budget_override,
                    total_units,
                    len(leaves),
                )

            # ✅ 2026-11-05（《L3以下“少目录、多正文”编制标准》）：
            #    L3 正文标准下限（普通 1500 字 / 关键 2500 字）。只在**默认路径**
            #    （未选字数档位、未传 override）生效——用户显式选择的档位/总量
            #    优先，避免标准悄悄覆盖用户意图。占位章节已在上方从 leaves 剔除，
            #    不会把「另行补充内容」的章节预算抬高；预算一旦抬高会同时写回
            #    leaf dict，保证提示词目标字数、word_status 与预检 DLV-16 同口径。
            if (
                l3_standard.standard_enabled()
                and l3_standard.word_floor_enabled()
                and word_budget_tier is None
                and not (word_budget_override and word_budget_override > 0)
            ):
                _floored: list[dict] = []
                for lf in leaves:
                    if int(lf.get("level") or 0) < 3:
                        continue
                    try:
                        _old_wb = int(lf.get("word_budget") or 0)
                    except (TypeError, ValueError):
                        _old_wb = 0
                    _new_wb = l3_standard.apply_word_floor(_old_wb, str(lf.get("title") or ""))
                    if _new_wb > _old_wb:
                        lf["word_budget"] = _new_wb
                        _floored.append(lf)
                if _floored:
                    try:
                        await db.executemany(
                            "UPDATE sections SET word_budget=? WHERE id=?",
                            [(lf["word_budget"], lf["id"]) for lf in _floored],
                        )
                        await db.commit()
                    except Exception as e:
                        logger.warning("L3 标准字数下限持久化失败（不影响生成）: %s", e)
                    logger.info(
                        "字数预算：按 L3 标准下限抬升 %d 个叶子（普通 %d 字 / 关键 %d 字）",
                        len(_floored),
                        l3_standard.word_floor(""),
                        l3_standard.word_floor("加固"),
                    )

            if not leaves:
                # ✅ E1：走 _update_progress_safe（与全文其余进度推送同口径：
                #    持写锁、失败静默、不阻断），且该更新器已在 try 之前建立
                #    —— 直接调 update_progress 会绕过写锁，与并发章节写事务
                #    争 SQLite 写锁（busy_timeout 内重试，拖慢收尾）。
                # ✅ 成果可恢复：空章节也是一次**正常的终态**，与 completed
                #    路径同口径先落 checkpoint 再置终态 —— 用户若在
                #    「没有待生成的章节」提示出现的瞬间刷新页面，重挂接
                #    GET /sse/task/{id} 才能读到 event=completed 而非"任务不存在"。
                try:
                    await _save_content_checkpoint(task_id, _content_ckpt_payload("completed", "没有待生成的章节"))
                except Exception:
                    logger.warning("检查点写入失败（task=%s · 正文生成 · 空章节标记）", task_id, exc_info=True)
                await _update_progress_safe(1.0, "没有待生成的章节", event="completed")
                # ✅ BUG 修复（2026-10-17 · 正文/审核联合审计）：收尾必须
                #    **先 finish_task 再 yield**，与其余六条终态路径同口径。
                #    旧实现把 `finish_task(task_id, "completed")` 写在 yield
                #    **之后**，而前端收到 completed 立即 `break` + 关闭响应体
                #    → 生成器在 yield 处收到 GeneratorExit → 该行**永不执行**
                #    → `finally` 的 `has_active_task(task_id)` 仍为真
                #    → 兜底分支把一次**已正常完成**的任务改写成
                #    「stopped · 客户端断开」（用户点生成、方案本就无待生成章节
                #    的常见场景，却看到「已停止」，误以为生成失败要重试）。
                #    与本文件 :7776 / :7794 两处注释记载的同一条不变量一致。
                await finish_task(task_id, "completed")
                yield f"data: {json.dumps({'event':'completed','task_id':task_id,'message':'没有待生成的章节'}, ensure_ascii=False)}\n\n"
                return

            # ✅ 模拟值闸门 + 按分组标题聚合：
            #   1. 未确认的模拟值（is_simulated=1 且未人工审核）与存在矛盾的事实
            #      禁止注入正文，避免把待裁决/编造值写成"确定性事实"；
            #   2. 按 group_title 聚合成小节，修复此前用单条事实名当小节标题
            #      导致同一分组事实各自成节、语义割裂的问题。
            # ✅ token 优化：结构化「项目关键事实」由共享函数统一构建（目录生成复用同一逻辑），
            #    每条事实截断 300 字、总量截断 6000 字（_build_facts_text 默认值）。
            #
            # ✅ BUG 修复（致命）：旧实现此处把事实文本按"当前叶子"构建（relevant_to=叶子），
            #    而该叶子变量只在上面 `word_budget_override` 分支的 `for leaf in leaves` 里绑定。
            #    用户使用默认字数（不传 word_budget_override，前端默认档）时该 for 不执行，
            #    `leaf` 未定义 → 此处抛 NameError → 被外层兜底 except 捕获，
            #    表现为「正文生成失败：name 'leaf' is not defined」，整批章节一章都生成不了。
            #    同时旧实现只在循环外算一次 facts_text，逐章精选实际只按最后一个叶子过滤，
            #    所有章节共享同一份事实（与「逐章精选」设计相悖）。
            #    现改为：事实行只加载一次，逐章在 _build_generation_context 内按 leaf 精选。
            facts_rows = await _load_facts_rows(db, scheme_id)
            # ✅ R105（G1/G4 · 顺序期望序与「章节 → 所需事实」映射表接线）：
            #    期望序 = 本次目标叶子的目录 DFS 序（leaves 在上方已经过
            #    order_sections_dfs + select_target_leaves，天然严格目录序）；
            #    映射表与逐章注入**共用同一判据**（chapter_fact_mapping 内部
            #    即 _filter_facts_rows 预筛），两侧天然 parity，不存在第二套
            #    归类规则。两者只读计算、fail-soft，绝不阻断生成。
            _expected_order[:] = [str(lf.get("id") or "") for lf in leaves]
            try:
                _ckpt["fact_mapping"] = chapter_fact_mapping(leaves, facts_rows)
            except Exception:
                logger.warning("章节事实映射表计算失败（终态不带 fact_mapping，不影响生成）", exc_info=True)
                _ckpt["fact_mapping"] = []
            # ✅ 2026-10-02（检查点前置）：审核检查点 → 生成约束的 system 级段落，
            #    整案一份（危大判定按方案名/类型确定性反查，零 AI 零成本）。
            #    受 content_checkpoint_prepend 控制（默认 True）；False 时两段均
            #    返回空串 → 独占行占位符整行丢弃，提示词与该功能引入前逐字一致。
            _ck_prepend = bool(getattr(settings, "content_checkpoint_prepend", True))
            # 生成后程序化自检开关（默认开，2026-10-02 第二十六轮：需求目标一
            # 「生成即完整、不残留占位标记」；设 False 可逐字回退到观察期行为）。
            _selfcheck_on = bool(getattr(settings, "content_selfcheck", True))
            _selfcheck_autofix = bool(getattr(settings, "content_selfcheck_autofix", True))
            # ✅ R29（2026-10-02 · 检查点反哺）：CON-06 跨章节段落搬运的生成后自检。
            # 仅当 _selfcheck_on 时生效；关闭完整回退到引入前行为。
            _crosscheck_dup_on = bool(getattr(settings, "content_crosscheck_duplicate", True))
            _crosscheck_values_on = bool(
                getattr(settings, "content_crosscheck_values", True)
            )  # ✅ R52：CON-01 跨章节数值一致性自检开关 —— 与 content_crosscheck_duplicate **互相独立**（关掉搬运检测不得连带关掉数值一致性自检），默认 True。
            # ✅ R77（2026-10-14 · 引入易标「生成后事实冲突审计 + 定点替换」）：
            #    standard_report 检出 error 级事实冲突后是否走「AI 定点编辑 +
            #    唯一命中才替换」自动修复。默认开（仅在检出冲突的章节触发 AI
            #    调用，无冲突章节零成本）；设 False 逐字回退引入前行为
            #    （冲突只进 last_generation_report、不进修复链路）。
            _fact_autofix_on = bool(getattr(settings, "content_fact_autofix_enabled", True))
            # ✅ R82（2026-10-15 · 引入易标 CG:1686「审计用全量事实」）：生成后一致性
            #    审计（standard_report 冲突检出 + fact_autofix 修复参考面）是否从
            #    「本章精选事实」扩到「全量已确认事实」（facts_rows，已过 fail-closed
            #    门控）。捕获「正文与未注入本章的事实冲突」这一引入前漏检形态。
            #    默认开；设 False 逐字回退「仅本章精选事实」口径。
            _audit_full_facts_on = bool(getattr(settings, "content_audit_full_facts", True))
            # ✅ P2 修复（2026-10-04 · O(N²) 性能）：跨章搬运检测的内存快照。
            #    正文生成过程中，库内 sections 表只会「增量增加已成功落库的章节」
            #    （章节 id 稳定，正文只在首次落库后不再变）。首次进入 _persist_section
            #    时把整表快照一次性加载到 _crossdup_snapshot，之后每章只在内存里
            #    追加/覆盖自己那条，避免 N 次全表扫描。语义与旧实现等价
            #    （cross_section_copy_findings 只看「本章 vs 库内既有」）。
            #    用 dict 而非 list：section_id 为唯一键，追加天然幂等，且
            #    与 `_crossdup_snapshot[section_id] = {...}` 的直接赋值对齐。
            _crossdup_snapshot: dict | None = None
            # ✅ P2 修复（2026-10-05 · 正文生成 R-5 · 编号元数据 N+1）：
            #   落库前的编号规范化与落库后的编号复核都支持传入预取索引
            #   （numbering.load_scheme_section_index，2 条查询压掉逐章 4 次），
            #   但本链路此前**从不传** —— 96 章方案 = 384 次额外 db.execute，
            #   每次都要走 aiosqlite 线程往返，在 5 章并发档下明显拖慢落库。
            #   索引只依赖 outline_json / level / title / parent_id，正文生成
            #   **不写这四类字段**，且结构变更入口（create/update/delete/reorder/
            #   save-outline/reset-content）在生成期间一律 409 拒绝 → 生成前
            #   预取一次即可，全程有效。
            #   load_scheme_section_index 自身 fail-soft（失败返回空索引），
            #   两个消费方在索引缺失时都会回退逐章查询，行为与旧版完全一致。
            _numbering_index: dict = await load_scheme_section_index(db, scheme_id)
            # 危大判定：确定性关键词反查，**整案一次**，逐章注入与自检共用
            # 同一结果（避免「提示词按危大要求写、自检按非危大判」的分叉）。
            _ck_hazardous = is_hazardous_scheme(scheme.get("name", ""), scheme.get("type", ""))
            _checkpoint_block = (
                build_content_system_checkpoint_block(
                    scheme.get("name", ""), scheme.get("type", ""), is_hazardous=_ck_hazardous
                )
                if _ck_prepend
                else ""
            )
            # ✅ 知识库注入（§3.9）：项目级知识条目作为生成素材（非唯一数据源）。
            #    只加载一次，逐章在 _build_generation_context 内按 leaf 精选
            #    （旧实现全量注入每一章，长方案下整本制度库无差别重复发送）。
            # ✅ D-39（R176）：正文链上下文降级簿记（装配期截断/读失败可见）。
            _ctx_degraded: list[dict] = []

            def _flush_context_degraded() -> list[str]:
                """把当前簿记转成事件并清空（正常空清单 → 空列表）。"""
                nonlocal _ctx_degraded
                if not _ctx_degraded:
                    return []
                events = _context_degraded_events(task_id, _ctx_degraded)
                _ctx_degraded = []
                return events

            knowledge_rows = await _load_knowledge_rows(
                db, scheme_id, degraded_out=_ctx_degraded, degraded_stage="content.assemble.knowledge_rows"
            )

            def _knowledge_text_for(leaf: dict) -> str:
                if not knowledge_rows:
                    return ""
                from app.routers.knowledge import build_knowledge_text

                _kn_stats: dict = {}
                text = build_knowledge_text(_filter_knowledge_rows(knowledge_rows, leaf), stats_out=_kn_stats)
                # ✅ D-39（R176）：逐章知识库硬切并入统一降级簿记。
                _record_knowledge_budget(
                    _ctx_degraded, _kn_stats, stage="content.generate.knowledge_text", field="knowledge_text"
                )
                return text

            # ✅ BUG 修复（与目录生成同一处缺陷）：LIMIT 必须先过滤空值，
            #    否则未解析文档占满名额 → 已解析资料对正文生成不可见。
            from app.routers.global_facts import load_parsed_texts

            # ✅ R72（2026-10-12）：与目录生成链对齐采集被截断文档清单。
            #    旧实现不传 truncated_out（目录链传了），正文生成对「依据
            #    不完整」不告知，三条消费链（目录/正文/事实提取）口径不一致。
            _content_trunc_docs: list[str] = []
            docs = await load_parsed_texts(db, project_id, limit=3, truncated_out=_content_trunc_docs)
            # ✅ 跨模块接线（2026-09-17 / 2026-09-23）：与目录生成对齐，优先消费
            #    bid_analysis 结构化提取成果（format_downstream_context 此前是
            #    死代码），提取失败/无成果时自动回退原文档摘录。
            # ✅ 预算 2000 → **4000**（2026-09-23 · 四项依据改造）：旧预算在
            #    18 项提取成果全量下发时**常态化截断**，与「完整调用、不丢失不
            #    截断」的约束相悖（后半段提取项整段消失，参数不可见）。
            _brief_source = "\n".join(docs) if docs else ""
            raw_brief = _brief_source[:4000]
            _record_context_degraded(
                _ctx_degraded,
                field="raw_brief",
                stage="content.assemble.raw_brief",
                original_chars=len(_brief_source),
                kept_chars=len(raw_brief),
                segment_id="raw_brief",
            )
            project_brief = await _build_structured_brief(
                db,
                project_id,
                raw_brief,
                max_chars=4000,
                degraded_out=_ctx_degraded,
                degraded_stage="content.assemble.structured_brief",
            )

            # ✅ R72（2026-10-12）：依据不完整必须显式告知（与目录生成链
            #    同一口径）。被截断文档参与正文生成时，后半段依据不可见，
            #    用户需在事件流看到提示，而非只能翻日志。
            if _content_trunc_docs:
                _ctd_msg = (
                    f"⚠️ {len(_content_trunc_docs)} 份文档解析不完整"
                    f"（内容被截断）：{'、'.join(_content_trunc_docs[:5])}"
                    + ("等" if len(_content_trunc_docs) > 5 else "")
                    + "；本次正文生成仅基于已解析到的部分，"
                    "后段参数可能不可见，建议拆分文件后重新上传并解析"
                )
                yield f"data: {json.dumps({'event':'warning','task_id':task_id,'message':_ctd_msg}, ensure_ascii=False)}\n\n"

            # ✅ D-39（R176）：正文链装配期的上下文降级可见信号；空清单
            #    （正常路径）不发任何事件，避免前端提示噪音。
            for _cd_event in _flush_context_degraded():
                yield _cd_event

            # ✅ 图表与正文一体生成：不再注入"预编排配图规划"。
            #    旧实现预查 chart_predictions(needed=1) 注入 {chart_plan}，是已删除的
            #    "正文生成前预编排"（Phase 2）的残留 —— 该表现在**只由正文同步登记**
            #    （register_inline_charts）写入，重新生成时会读到上一次的陈旧类型，
            #    既与"由 AI 自主判定是否配图、配哪种图"的设计相悖，也会把旧结论
            #    当成新规划误导模型。配图判定与插入位置现完全由正文 AI 自主完成
            #    （见 content_generation_system「图表同步生成规范」）。

            total = len(leaves)
            # ✅ 进度增强：登记总章节数与阶段（进入章节生成阶段）
            _prog["total"] = total
            _prog["phase"], _prog["phase_label"] = "sections", "生成正文"
            # ✅ ETA 口径修复：章节阶段起点独立记录 —— ETA 只按本阶段耗时外推，
            #    不含准备阶段（章节树加载 + 字数分配 AI 调用，最长 180s）与
            #    全文一致性阶段，否则开头几章的 ETA 会被系统性放大。
            _prog["sections_started_at"] = time.monotonic()
            _ckpt["total"] = total

            # 并发闸门（P0 修复 2026-09-19）：**每任务独立信号量**，容量严格 =
            #    用户前端档位（effective_concurrency，请求入口已归一）。
            #    不再复用全局 concurrency_controller.semaphore —— 旧实现三个缺陷：
            #      1) 正文/目录/多方案任务共用一把信号量，后启动任务的档位会
            #         挤掉先启动任务的用户设置（跨任务互染）；
            #      2) provider 层 adjust_concurrency 自适应降档使**实际并发与用户
            #         选择脱节**（需求要求严格遵循用户档位）；
            #      3) AI 配置保存/启动应用的 set_concurrency 会中途改变运行中任务闸门。
            #    全局控制器仍供 provider 层记录统计/熔断与事实提取等路径使用，
            #    只是不再充当正文生成的闸门。
            # ✅ R179 U5：分阶段并发读数接线（content 阶段）。开关关 →
            #    stage_concurrency 返回 0，走 or 分支 = 逐字现状（用户档位
            #    effective_concurrency）；开关开 → 阶段上限（默认 2，已夹取
            #    max_concurrency）。启动顺序仍 = 目录序（信号量 FIFO 不变）。
            _u5_content_conc = ai_warmup.stage_concurrency("content")
            _run_semaphore = asyncio.Semaphore(_u5_content_conc or effective_concurrency)
            # ⚠️ 实现口径（2026-09-26 澄清）：本任务用的是
            #    **「每任务独立信号量 + FIFO 放行」**，并非显式 `_WINDOW` 窗口调度器。
            #    · 协程按 leaves 目录序创建（DFS 序），
            #    · asyncio.Semaphore 对等待者 FIFO 放行 → 启动顺序 = 创建顺序 = 目录序，
            #    · 容量严格 = 用户档位 effective_concurrency（不随全局自适应漂移）。
            #    该方案曾被改成"显式 pending 队列 + _WINDOW 窗口调度"，后因
            #    as_completed 的完成顺序不影响启动顺序而回退为信号量实现；
            #    tests/test_content_order_concurrency.py 的
            #    test_per_task_semaphore_strict_user_level 锁定的是**本口径**，
            #    其早期断言里的 `_WINDOW = int(effective_concurrency)` 已随回退失效。

            # ✅ 追踪：已完成章节 ID 集合（done_ids 已在 try 之前预初始化，
            #    此处不得重复绑定 —— 重复绑定会让 finally 读到 try 内的新集合，
            #    早期 except 已登记的失败记录被丢弃）
            # ✅ 增强：本次生成过程中已完成章节的最新正文（落库清洗后的最终内容）。
            #    供后生成的同级章节做"前序风格参考"——旧实现只读启动时的 DB 快照，
            #    同批新生成时快照为空，摘要恒为空、功能失效；这里改为实时维护。
            generated_contents: dict[str, str] = {}

            # 预构建 parent_id → children 映射（避免 O(N²) 查找）
            by_parent: dict[str, list[dict]] = {}
            for s in all_sections:
                by_parent.setdefault(s.get("parent_id", ""), []).append(s)
            for k in by_parent:
                by_parent[k].sort(key=lambda x: x.get("sort_order", 0))
            nodes_map: dict[str, dict] = {s["id"]: s for s in all_sections}

            # ✅ R105（G5 · 前驱叶子承接摘要接线）：all_sections 已 DFS 重排，
            #    按 _parent_ids（有子节点的 id 集合）筛出文档序叶子序列。
            #    前序同级的姊妹口径是 build_sibling_context；父下第一个叶子
            #    同级摘要恒为空，文档意义上的「直接前驱叶子」（可能是上一章
            #    最后一个子节）此前零承接。服务层 prev_leaf_tail_summary 与
            #    content_runtime 双装配函数的 prev_leaf_summary 形参早已备好，
            #    本处只接线调用点。严格只取直接前驱：前驱无正文（未生成/空章）
            #    → 回落空串，不回溯更早章节、不 Fabrication。
            _dfs_leaf_ids = [
                str(s.get("id") or "")
                for s in all_sections
                if str(s.get("id") or "") and str(s.get("id") or "") not in _parent_ids
            ]
            _leaf_pos: dict[str, int] = {sid: i for i, sid in enumerate(_dfs_leaf_ids)}

            def _prev_leaf_tail_for(leaf: dict) -> str:
                """取 leaf 在目录树文档序上的直接前驱叶子结尾片段（fail-soft）。"""
                if not bool(getattr(settings, "content_prev_leaf_context", True)):
                    return ""
                try:
                    pos = _leaf_pos.get(str(leaf.get("id") or ""))
                    if pos is None or pos <= 0:
                        return ""
                    prev = nodes_map.get(_dfs_leaf_ids[pos - 1]) or {}
                    # 本次新生成内容优先（实时），回落启动时 DB 快照正文
                    text = generated_contents.get(str(prev.get("id") or "")) or str(prev.get("content") or "")
                    if not text.strip():
                        return ""
                    return prev_leaf_tail_summary(text)
                except Exception:  # noqa: BLE001 - 承接摘要绝不阻断正文生成
                    logger.warning("前驱叶子承接摘要获取失败（回落为空，不影响生成）", exc_info=True)
                    return ""

            # ✅ E1：_db_write_lock / _update_progress_safe 已提升到 try 之前
            #    建立（见上方注释），此处不得重复定义 —— 迟到的赋值会让
            #    `if not leaves:` 早退分支抛 UnboundLocalError。

            # ---------- 进度推送辅助（2026-09-15 增强） ----------

            async def _push_stats():
                """推送一次运行统计（不写 DB；失败静默，不影响生成）。"""
                try:
                    await update_task_stats(task_id, **_snapshot_stats(_prog))
                except Exception:
                    logger.debug("正文生成运行统计推送失败（不影响生成 · task=%s）", task_id, exc_info=True)

            def _progress_now() -> float:
                """折算当前加权进度并过「不可回退」护栏（返回并记录历史最大值）。"""
                p = _monotonic_progress(_prog.get("progress_max", 0.0), _weighted_progress(_prog))
                _prog["progress_max"] = p
                return p

            async def _on_stage(leaf_id: str, stage: str, detail: str = ""):
                """章节阶段切换：更新进度状态 + 推送 section_stage 事件。

                同时刷新加权总进度，使进度条在单章生成期间也持续前进，
                而不是等整章落库才跳一格。

                ✅ 阶段耗时在线校准：离开上一阶段时，用该阶段的实测耗时校准
                `stage_expect`（EMA），使后续阶段的填充速率贴合真实模型速度
                （报告遗留建议 #1）。只在切换点更新 ⇒ 阶段内曲线连续、
                进度不回退（_calibrate_stage_expect 内有完整说明）。
                """
                rec = _prog["running"].get(leaf_id)
                now = time.monotonic()
                if rec is not None:
                    prev_stage = rec.get("stage")
                    prev_started = rec.get("stage_started_at")
                    if prev_stage and prev_stage != stage and prev_started:
                        _calibrate_stage_expect(_prog, prev_stage, now - prev_started)
                    rec["stage"] = stage
                    rec["stage_label"] = _STAGE_LABELS.get(stage, stage)
                    rec["stage_started_at"] = now
                p = _progress_now()
                label = _STAGE_LABELS.get(stage, stage)
                title = (rec or {}).get("title", "")
                await _update_progress_safe(p, f"「{title}」{label}{detail}")
                await event_queue.put(
                    "data: "
                    + json.dumps(
                        {
                            "event": "section_stage",
                            "task_id": task_id,
                            "section_id": leaf_id,
                            "title": title,
                            "index": (rec or {}).get("index", 0),
                            "total": _prog["total"],
                            "stage": stage,
                            "stage_label": label,
                            "detail": detail,
                            "progress": p,
                            "elapsed_ms": int((now - rec["started_at"]) * 1000) if rec else 0,
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )

            async def _persist_section(
                section_id: str,
                content: str,
                word_budget: int,
                eff_standard: str = "",
                fact_rows_for_section=None,
                section_title: str = "",
                plan_json: str = "",
            ) -> tuple[str, int, str, dict]:
                """章节完成后立即持久化到数据库（不复用 buffer，避免崩溃全丢）。

                ✅ BUG 修复：内联图表登记会改写正文（修复成功替换代码 / 修复失败删块），
                word_count 与 word_status 必须基于【最终落库正文】计算；
                旧实现用清洗前的长度，导致库里字数、SSE 事件字数与实际正文偏大。

                Returns:
                    (最终正文, word_count, word_status, 生成标准校验报告)

                ✅ 事务守卫（BUG 修复 · 2026-09-16）：整个写块（清洗 → 图表全量重登记
                → 正文 UPDATE）必须原子。旧实现只对 `register_inline_charts` 的异常
                做 rollback；若**正文 UPDATE / commit 自身失败**（连接被中断、
                磁盘满、并发写冲突等），register 内已执行的
                `DELETE FROM chart_predictions WHERE section_id=?` 会被下一次 commit
                （例如 _mark_section_failed）顺带提交 —— 形成「正文里还有图表块、
                图表清单却没有登记」的不一致，导出预检与图表清单从此对不上。
                现在图表登记**之外**的任一步失败仍整体回滚并原样抛出，交由调用方按"生成失败"处理。
                ⚠️ R108（2026-10-18 · 日志审计 P0-1）口径更新：图表登记（apply）
                自身失败不再冒泡——DELETE 已接 retry_db_op，重试预算耗尽后回滚
                半途图表写、记 WARNING、**正文照常优先落库**（生产实证旧口径下
                锁冲突会连带丢失已生成正文）；「登记与正文不一致」由下一轮持久化
                的全量重登记自动收敛。正文 UPDATE+commit 亦接 retry_db_op。

                ✅ P1-3（2026-09-17 · 写锁临界区收缩）：正文清洗、标准审计、内联图表
                「计数 + 扫描 + 校验/修复 + 限额裁剪」全部是 CPU/读操作，已**整体前移到
                写锁之外**；锁内只保留最小事务（DELETE + INSERT 图表登记 + 正文 UPDATE
                + commit）。旧实现持锁做完整套计算，高并发档（5 章并行）时所有章节都在
                落库点排队等同一把全局写锁，实际并发退化为串行。
                事务原子性不变：图表登记与正文 UPDATE 仍在同一事务内提交。
                """
                # ✅ P0 修复（2026-10-05 · 正文生成 R-6 · CON-06 跨章搬运检测**整链失效**）：
                #   本函数内对 `_crossdup_snapshot` 既有读取（`if _crossdup_snapshot is None`）
                #   又有**普通名字赋值**（`_crossdup_snapshot = {...}`）。Python 在编译期
                #   就把同名名字判定为**本函数的局部变量**，于是那条先执行的读取每次都抛
                #   `UnboundLocalError: cannot access local variable '_crossdup_snapshot'`。
                #   而本块被外层 `except Exception` fail-soft 包着（只留一条 WARNING
                #   + exc_info）→ **`cross_section_copy_findings` 从未真正执行过一次**。
                #   即 `content_crosscheck_duplicate`（默认 True）与 R29 引入 CON-06
                #   的全部意图自 2026-10-04 那次 P2 性能优化起就是死代码：
                #   生产库实证的 3 条「骨架归一后相似度 100%」跨章搬运，
                #   生成侧自检一条都没报出来。
                #   修法：显式 `nonlocal`，让赋值落到 event_stream 层的共享快照，
                #   与该变量上方的设计注释（「首次调用从 DB 加载、后续复用内存快照」）
                #   完全一致 —— 原实现从未满足过自己声明的语义。
                nonlocal _crossdup_snapshot
                # ---------- 锁外：CPU 密集（清洗 / 审计 / 图表扫描校验修复） ----------
                # ✅ P0 入口断言（2026-09-29 · 「内联图表登记失败: tuple has no split」在线事故）：
                #    之前 _persist_section 接收的 content 被下游函数当作 str 处理，
                #    若上游返回了 tuple 则在 build_inline_chart_plan 内部 .split("\n")
                #    才爆炸，丢失根因。这里第一时间校验类型，若异常则打 WARNING + exc_info，
                #    便于回溯调用链。
                if not isinstance(content, str):
                    logger.warning(
                        "章节 %s _persist_section 入口 content 非 str（type=%s），" "尝试降级；scheme=%s",
                        section_id[:8],
                        type(content).__name__,
                        scheme_id[:8],
                        exc_info=True,
                    )
                    if isinstance(content, (tuple, list)) and content:
                        content = content[0] if isinstance(content[0], str) else str(content[0])
                    elif isinstance(content, bytes):
                        content = content.decode("utf-8", errors="replace")
                    else:
                        content = str(content) if content else ""
                # ✅ 交付前清洗：消除 AI 生成正文中的口语化、宣传腔与 AI 表述，
                #    跳过 ``` 围栏内的图表代码块，不改变技术含义与数据。
                try:
                    _before = len(content)
                    content = sanitize_ai_content(content)
                    if len(content) != _before:
                        logger.debug("章节 %s 正文清洗：%d → %d 字", section_id[:8], _before, len(content))
                except Exception as e:
                    logger.warning("章节 %s 正文清洗失败（使用原文）: %s", section_id[:8], e, exc_info=True)
                # ✅ 未闭合围栏补齐（2026-09-23）：AI 达到 max_tokens 上限时
                #    会把 ```mermaid 的收尾围栏截断，落库后**该行之后的所有正文
                #    都会被 Markdown 当成代码块内容吞掉**（导出 DOCX 时整章只剩
                #    一段代码）。必须在落库前补齐；修复本身失败只降级不阻断。
                # ✅ P0 契约修复（2026-09-29 · 「38/38 章全失败」在线事故）：
                #   auto_fix_unclosed_fences 的返回契约是 **(fixed_content,
                #   fixes_log)**（导出侧 export.py 按 `new, log = ...` 解包），
                #   而此处沿用了「返回字符串」的旧假设，直接
                #   `content = auto_fix_unclosed_fences(content)` ——
                #   content 因此变成 **元组**，紧接着的
                #   `text_word_count(content)` → `strip_fenced_code_blocks`
                #   → `content.split("\n")` 抛
                #   `AttributeError: 'tuple' object has no attribute 'split'`。
                #   该异常发生在**每一章**的落库路径上（且不论正文是否含未闭合
                #   围栏，函数恒返回元组）→ 全方案每一章 100% 失败，终态报
                #   「AI 服务异常」，而真实原因与 AI 毫无关系（误导排障方向）。
                #   根因与 AGENTS.md 记录的「同一契约在多处各自实现、改一处漏一处」
                #   同构：护栏见 tests/test_content_fence_contract_20260929.py
                #   （AST 断言 + 全仓调用点扫描 + 端到端落库用例）。
                try:
                    _swallowed = 0
                    content, _fence_fixes = auto_fix_unclosed_fences(content)
                    if _fence_fixes:
                        # ✅ R76（F4 可观测性）：「补齐 N 处」不能一概而论 —— 两步动作
                        # 性质不同：normalize_close_length 只把闭合行标记补齐到开围栏
                        # 长度（不增删行、正文一字不动）；append_close 是在文末追加闭合，
                        # **救不回已被吞进块里的正文**。旧日志把两者都写成「补齐 N 处」，
                        # 运维据此以为问题已解决。分动作计数，并在疑似吞正文时升级为
                        # WARNING 提示人工核对。
                        _n_norm = sum(1 for f in _fence_fixes if f.get("action") == "normalize_close_length")
                        _n_append = sum(1 for f in _fence_fixes if f.get("action") == "append_close")
                        _swallowed = sum(int(f.get("swallowed_prose") or 0) for f in _fence_fixes)
                        logger.info(
                            "章节 %s 围栏缺陷自动归一：闭合标记长度补齐 %d 处、文末追加闭合 %d 处",
                            section_id[:8],
                            _n_norm,
                            _n_append,
                        )
                        if _swallowed:
                            logger.warning(
                                "章节 %s 追加闭合前有 %d 行疑似正文困在围栏块内（补齐只能让块"
                                "合法结束，救不回内容），请人工核对该章导出成稿",
                                section_id[:8],
                                _swallowed,
                            )
                except Exception as e:
                    logger.warning("章节 %s 未闭合围栏修复失败（使用原文）: %s", section_id[:8], e, exc_info=True)
                # ✅ 生成后审计：命中已废止标准编号时告警，供人工复核
                try:
                    _issues = quality_issues(content)
                    if _issues["abolished_standards"]:
                        logger.warning("章节 %s 疑似引用已废止标准：%s", section_id[:8], _issues["abolished_standards"])
                except Exception:
                    logger.debug("章节 %s 生成后质量审计失败（跳过，不影响落库）", section_id[:8], exc_info=True)
                # ✅ 内联图表（锁外计算）：修复成功用修复后代码写正文，修复失败删除坏块，
                #    保证导出质量；enforce_limits=True = 程序级配图上限（每个 section_id
                #    （叶子小节）≤1、同类型全方案限额，对齐 OpenBidKit 编排 Agent 的"程序拍板"）。
                _chart_ok = True
                _chart_rows: list[tuple] = []
                _chart_dropped: list = []
                try:
                    _type_counts = await _load_scheme_type_counts(db, scheme_id, section_id)
                    content, _chart_rows = build_inline_chart_plan(
                        scheme_id,
                        section_id,
                        content,
                        enforce_limits=True,
                        scheme_type_counts=_type_counts,
                        dropped_out=_chart_dropped,
                    )
                except Exception as e:
                    # 图表登记失败：不写图表登记、继续落库正文（宁可少登记，不可坏正文）
                    # ✅ 恢复 exc_info（2026-09-29 · 「内联图表登记失败」在线事故）：
                    #    之前只打消息丢失 traceback，无法回溯 content tuple 的根因。
                    logger.warning(
                        "章节 %s 内联图表登记失败（本次跳过图表登记，不影响正文）: %s " "[content_type=%s]",
                        section_id[:8],
                        e,
                        type(content).__name__,
                        exc_info=True,
                    )
                    _chart_ok = False
                    _chart_rows = []

                # ✅ 正文字数剔除图表代码块：图表现与正文一体生成，若把内嵌的
                #    mermaid/chart-json 代码计入字数，会虚高字数并掩盖"正文偏短"。
                wc = text_word_count(content)
                ws = word_status_for(wc, word_budget)

                # ✅ 统一编号命名空间（2026-09-26 · 补齐死代码 + 时序修复）：
                #    规范化必须**在 standard_report 之前**、**在锁外**执行。
                #    ① 时序：旧实现把规范化放在锁内、standard_report 之后 ——
                #       报告按「AI 原始编号正文」计算，落库正文却是「规范化后」，
                #       两者不是同一份文本（正文里的编号 token 会进入
                #       extract_number_tokens 的数值抽取，影响交叉引用类问题判定），
                #       报告与落库正文对不上号：用户按报告看到的证据在正文里找不到。
                #    ② 锁：规范化要读 outline_json 与子章节数（两次 DB 往返），
                #       放在全局写锁内会让并发档（5 章并行）排队等同一把锁，
                #       与本函数 P1-3「锁内只保留最小事务」的设计结论冲突。
                #    实现收口到 numbering.normalize_section_content_subheadings
                #    （含 content_subheading_renumber 开关 + 降级兜底，唯一实现）。
                content, _renorm_changed = await normalize_section_content_subheadings(
                    db,
                    scheme_id,
                    section_id,
                    content,
                    # ✅ R5：传预取索引消除逐章 2 次查库（缺失时自动回退逐章路径）
                    index=_numbering_index,
                )
                if _renorm_changed:
                    wc = text_word_count(content)
                    ws = word_status_for(wc, word_budget)

                # F-CONTENT-STANDARD: 生成标准校验（**锁外**执行）
                # ✅ 缺口修复（2026-09-26 · B4）：此前 `standard_report()` 被直接写在
                #    `async with _db_write_lock` 内的 UPDATE 参数里 —— 全章正文 ×
                #    相关事实行的正则扫描是纯 CPU 密集操作，落在全局写锁里会与本函数
                #    上方 P1-3 的设计结论（「锁内只保留最小事务」）自相矛盾：
                #    并发档（5 章并行）时每章都要排队等同一把锁，实际并发退化为串行。
                #    口径等价性：锁内 `apply_inline_chart_plan` 只会**删除超限图表代码块**，
                #    而校验器第一步即整体剔除 ``` 围栏代码块，故「裁剪前/裁剪后」求值
                #    完全一致，移出锁不改变任何报告结果。
                std_for_report = eff_standard or PRECISE
                report: dict = {}
                report_json = ""

                # ✅ 2026-10-02（检查点前置 · 确定性自动修复，**锁外**）：
                #    生成侧可零风险自动处置的两类高频缺陷（STD-03 裸标准编号缺年号、
                #    CON-04 占位标记残留）在**任何报告计算之前**就地修复，使
                #    standard_report / 检查点自检 / 字数 / SSE 载荷 / 落库正文
                #    五处口径一致。若在报告之后再修，报告与落库正文就对不上号 ——
                #    与上方「编号规范化必须在 standard_report 之前」是同一结论。
                #    受 content_selfcheck_autofix 控制（默认 True，2026-10-02 第二十六轮）：
                #    设 False 时完全不执行，正文与该开关引入前逐字一致。两处修复均为
                #    纯函数确定性变换、零 AI 成本、幂等（详见 content_checkpoint 两个函数的红线）。
                _ck_fix_actions: list = []
                if _selfcheck_autofix:
                    try:
                        content, _fix_std = fix_bare_standard_codes(content)
                        content, _fix_ph = rewrite_placeholder_marks(content)
                        _ck_fix_actions = [a for a in (_fix_std + _fix_ph) if a.get("fixed")]
                        if _ck_fix_actions:
                            logger.info(
                                "章节 %s 生成后确定性自动修复 %d 处" "（STD-03 补年号 / CON-04 占位改写）",
                                section_id[:8],
                                len(_ck_fix_actions),
                            )
                            wc = text_word_count(content)
                            ws = word_status_for(wc, word_budget)
                    except Exception:
                        # 两个函数自身已 fail-soft（返回原文），此处兜底不阻断落库
                        logger.warning("章节 %s 确定性自动修复异常（忽略，不影响落库）", section_id[:8], exc_info=True)
                        _ck_fix_actions = []

                if _ck_fix_actions:
                    # 修复动作留痕进报告（键名与 checkpoint_findings 同族，
                    # 便于前端 / 审核按 checkpoint 词表统一消费）。
                    report = dict(report or {})
                    report["checkpoint_fixes"] = _ck_fix_actions
                    try:
                        report_json = json.dumps(report, ensure_ascii=False)
                    except Exception:
                        report_json = ""
                # ✅ R82：冲突检出/修复参考面 = 全量已确认事实（含未注入本章的事实，
                #    对齐易标 CG:1686）；覆盖率/豁免/trace 仍按本章精选事实。
                #    开关 content_audit_full_facts=False → _conflict_rows=None →
                #    standard_report 输出与引入前逐字节一致。
                _conflict_rows = list(facts_rows) if _audit_full_facts_on else None
                _autofix_rows = list(facts_rows) if _audit_full_facts_on else (fact_rows_for_section or [])
                if eff_standard or fact_rows_for_section or _conflict_rows:
                    try:
                        report = standard_report(
                            content,
                            std_for_report,
                            fact_rows_for_section or [],
                            section_id=section_id,
                            section_title=section_title,
                            conflict_fact_rows=_conflict_rows,
                        )
                        # ✅ 2026-10-02（第 27 轮收口）：standard_report 整体**重建**
                        #    report，上面已写入的 checkpoint_fixes 留痕会被无条件覆盖
                        #    丢失（生产实证：autofix 改了正文、库里报告却无修复记录，
                        #    可追溯性断裂）。修复本身已在重建前作用于 content，报告
                        #    各口径仍基于修复后正文计算；这里只把留痕带进新报告。
                        if _ck_fix_actions:
                            report["checkpoint_fixes"] = _ck_fix_actions
                        report_json = json.dumps(report, ensure_ascii=False)
                    except Exception:
                        # 校验器异常绝不影响正文落库（降级为空报告形态 + WARNING）
                        logger.warning(
                            "章节 %s 生成标准校验失败（已降级，不影响正文落库）: %s", section_id[:8], exc_info=True
                        )
                        report = {}
                        report_json = ""

                # ✅ R77（2026-10-14 · 引入易标「生成后事实冲突审计 + 定点替换」）：
                #    standard_report 检出的 error 级事实冲突（正文数值/型号与本章
                #    事实矛盾）引入前只写报告、不进修复链路 —— 用户要到审核/预检或
                #    全文一致性 Agent（需先扫描+仲裁+人工确认）才能处理。现接入
                #    「AI 定点编辑 + 唯一命中才替换 + 失败回灌重试（上限 2 次）」：
                #    · 只在**检出冲突**时触发 AI 调用（无冲突章节零成本）；
                #    · old_text 不唯一/找不到一律拒绝，绝不猜；
                #    · AI 可返回空 edits（判定并非真实冲突时宁可不改）；
                #    · 修复后**重算** standard_report（纯函数、零 AI），使
                #      error_count / issues 口径与落库正文一致，checkpoint_fixes
                #      留痕按同族做法带进新报告；结果写报告新键 fact_autofix；
                #    · 任何异常 fail-soft，绝不阻断正文落库。
                #    开关 content_fact_autofix_enabled 设 False 逐字回退引入前。
                if _fact_autofix_on and _autofix_rows:
                    try:
                        _fa_conflicts = select_fact_conflicts(report)
                        if _fa_conflicts:
                            content, _fa_res = await autofix_section_fact_conflicts(
                                content=content,
                                standard=std_for_report,
                                fact_rows=_autofix_rows,
                                conflicts=_fa_conflicts,
                                section_id=section_id,
                                section_title=section_title,
                            )
                            if _fa_res.get("attempted"):
                                report = dict(report or {})
                                report["fact_autofix"] = _fa_res
                            if _fa_res.get("applied"):
                                # 修复生效：重算标准校验报告 + 字数口径，
                                # 保证报告 / SSE 载荷 / 落库正文三处一致。
                                report = standard_report(
                                    content,
                                    std_for_report,
                                    fact_rows_for_section or [],
                                    section_id=section_id,
                                    section_title=section_title,
                                    conflict_fact_rows=_conflict_rows,
                                )
                                if _ck_fix_actions:
                                    report["checkpoint_fixes"] = _ck_fix_actions
                                report["fact_autofix"] = _fa_res
                                report_json = json.dumps(report, ensure_ascii=False)
                                wc = text_word_count(content)
                                ws = word_status_for(wc, word_budget)
                                # ✅ R92（2026-10-17 · 根治项1）：事实修复改写了正文
                                #    （可能动到 ```mermaid 围栏内文字，如把「提前2天」
                                #    对齐全局事实改成「提前14天」），而上方锁外构建的
                                #    _chart_rows 仍是**修复前正文**算出来的配对。若就此
                                #    落库，chart_predictions 登记码 ≠ 正文内联码：前端轨
                                #    按旧登记码渲染 PNG、后端轨按新内联码取键，_norm_code
                                #    匹配必然 miss → 回落后端轨 → 命中本机零可用渲染后端
                                #    → 导出成「[图 X-Y … — 渲染失败]」红字（本轮报障根因 B）。
                                #    修复生效即按**修复后正文**用同一纯计算原语重建配对
                                #    （同 enforce_limits / 限额计数入参），使锁内 apply 写入
                                #    的 rows 与最终正文一致。异常 fail-soft：保留原 rows
                                #    （退回引入前行为），绝不阻断正文落库。
                                if _chart_ok:
                                    try:
                                        content, _chart_rows = resync_inline_chart_rows(
                                            scheme_id,
                                            section_id,
                                            content,
                                            enforce_limits=True,
                                            scheme_type_counts=_type_counts,
                                            dropped_out=_chart_dropped,
                                        )
                                        wc = text_word_count(content)
                                        ws = word_status_for(wc, word_budget)
                                    except Exception:
                                        logger.warning(
                                            "章节 %s 事实修复后图表配对重建失败" "（保留原登记，不影响正文落库）",
                                            section_id[:8],
                                            exc_info=True,
                                        )
                                logger.info(
                                    "章节 %s 事实冲突定点修复 %d 处" "（冲突 %d 条，尝试 %d 次）",
                                    section_id[:8],
                                    _fa_res["applied"],
                                    _fa_res.get("conflicts") or 0,
                                    _fa_res.get("attempts") or 0,
                                )
                            elif _fa_res.get("attempted"):
                                # 尝试过但未能修复（AI 判定无冲突 / 定位失败）：
                                # 透明留痕，转人工核对（对齐 R71「失败不静默」）。
                                report_json = json.dumps(report, ensure_ascii=False)
                                logger.info(
                                    "章节 %s 事实冲突未能自动修复（冲突 %d 条，" "拒绝 %d 条），留待人工核对",
                                    section_id[:8],
                                    _fa_res.get("conflicts") or 0,
                                    len(_fa_res.get("rejected") or []),
                                )
                    except Exception:
                        logger.warning("章节 %s 事实冲突定点修复失败（不影响正文落库）", section_id[:8], exc_info=True)

                # ✅ 2026-10-11（引入《文档生成规则分析与软件标准建立》正文生成标准 2.1）：
                #    段落长度 / 日期写法 / 工期单位口径的**咨询性**校验。加法式落地：
                #    只写报告新键 structure，不改 error/warning 计数、不进修复链路、
                #    不动正文；standard_report 未触发（无事实/未设标准）时也会补一份，
                #    并负责重建 report_json（否则本块成果无法序列化落库）。
                try:
                    _struct_rep = paragraph_structure_report(content)
                    report = dict(report or {})
                    report["structure"] = _struct_rep
                    report_json = json.dumps(report, ensure_ascii=False)
                    if _struct_rep.get("warnings"):
                        logger.info(
                            "章节 %s 段落结构建议 %d 条（仅咨询，不阻断）: %s",
                            section_id[:8],
                            len(_struct_rep["warnings"]),
                            "；".join(str(w.get("message", "")) for w in _struct_rep["warnings"][:3]),
                        )
                except Exception:
                    logger.warning("章节 %s 段落结构校验失败（忽略，不影响落库）", section_id[:8], exc_info=True)

                # ✅ R71（2026-10-12，引入易标 R12「失败不硬裁 → 人工核对透明警告」）：
                #    正文收尾时，若最终字数仍落在 word_status 正常带之外（under/over），
                #    说明续写/压缩没能把本章拉回预算，而系统按红线「宁可超字数不可损坏
                #    正文」刻意不强行裁剪 —— 把这一事实**透明告知**用户，而非静默放行。
                #    加法式落地：只写报告新键 word（advisory），不改 error/warning 计数、
                #    不进修复链路、不动正文与 word_count/word_status 落库；判定口径仍
                #    唯一走 text_word_count，护栏 test_word_caliber_transparency_r71 锁定。
                #    wc / word_budget 复用上方落库同口径读数（structure 块不改正文）。
                try:
                    _word_rep = word_closeout_notice(wc, word_budget)
                    report = dict(report or {})
                    report["word"] = _word_rep
                    report_json = json.dumps(report, ensure_ascii=False)
                    if _word_rep.get("advisory"):
                        logger.info(
                            "章节 %s 字数收尾提示（仅建议，不阻断）：%s", section_id[:8], _word_rep.get("message", "")
                        )
                except Exception:
                    logger.warning("章节 %s 字数收尾提示失败（忽略，不影响落库）", section_id[:8], exc_info=True)

                # ✅ R179（2026-11-05 · 未闭合围栏透明化 / 日志可见性）：
                #    `auto_fix_unclosed_fences` 的 append_close 只能在文末补闭合，
                #    已困在围栏块内的正文救不回来（本块上方仅写 backend.log，
                #    用户在前端日志区与生成报告里完全看不到）。现把该事实写进
                #    标准报告 issues（warning 级）—— 复用既有 Popover 展示链路，
                #    用户可即时看到「本章有 N 行正文可能被围栏吞掉，请人工核对」。
                #    只增 warning 计数、不改 passed（passed 仍由 error 决定）、
                #    不进修复链路、不动正文；无吞正文时零改动（向后兼容）。
                try:
                    if _swallowed:
                        # ✅ R181：载荷由 content_standard 唯一构造，单章保存路径共用同口径。
                        report = attach_fence_swallowed_issue(report, _swallowed)
                        report_json = json.dumps(report, ensure_ascii=False)
                except Exception:
                    logger.warning("章节 %s 围栏吞正文留痕失败（忽略，不影响落库）: %s", section_id[:8], exc_info=True)

                # ✅ R105（G3 · 逐章注入留痕落库）：把闭包容器里本章的注入审计
                #    合并进报告新键 fact_audit（加法式：不改 error/warning 计数、
                #    不进修复链路、不动正文）。后续块均走 `dict(report)` 合并式
                #    重建，本键随之保留。fail-soft：合并/序列化异常只 WARNING，
                #    落库继续（留痕丢失不得拖垮正文）。
                try:
                    _faudit = _fact_audit_by_leaf.get(str(section_id or ""))
                    if _faudit:
                        report = dict(report or {})
                        report["fact_audit"] = _faudit
                        report_json = json.dumps(report, ensure_ascii=False)
                except Exception:
                    logger.warning("章节 %s 事实注入留痕合并失败（忽略，不影响落库）", section_id[:8], exc_info=True)

                # ✅ 2026-10-02（检查点前置 · 生成后自检，**锁外**）：按检查点对
                #    本章正文做程序化体检（纯函数零 AI，fail-soft）。
                #    - 默认开（content_selfcheck=True，2026-10-02 第二十六轮），设 False
                #      完整回退到观察期行为（不产生任何新行为）；
                #    - findings 写入报告新键 checkpoint_findings（与预检 rule_id
                #      同词表），**不改**既有 error/warning 计数口径；
                #    - content_selfcheck_autofix（默认 True）另并入 issues 视图并计入
                #      汇总（供前端与审核消费；确定性自动修复已在报告前落地，见上）。
                # ✅ 2026-10-02（章节 key 补充推断）：主判据 chapter_key_of_title
                #    对「应急组织机构及职责 / 应急物资装备保障 / 应急演练」这类小节
                #    标题返回空串（既不等于也不互含九大章节标准名），导致 SAF-03/04/05
                #    在生成侧从未被要求过。现补 infer_chapter_key 兜底，并在「主判据
                #    未命中」时置 subsection_scope —— 小节只查自己承担的要求，避免把
                #    章级聚合要求拆到每个小节上产生假缺项（见 _req_in_scope）。
                _ck_title = str(section_title or "")
                _ck_primary = chapter_key_of_title(_ck_title)
                _ck_chapter_key = infer_chapter_key(_ck_title, _ck_primary)
                _ck_is_subsection = _ck_primary == ""
                if _selfcheck_on:
                    try:
                        _ck_findings = checkpoint_selfcheck(
                            content,
                            chapter_key=_ck_chapter_key,
                            is_hazardous_basis=_ck_hazardous,
                            section_title=_ck_title,
                            subsection_scope=_ck_is_subsection,
                            # ✅ 2026-10-02（第二十五轮）：STD-05 需按方案类别
                            #    反查现行技术标准编号，必须带方案上下文。
                            scheme_name=str(scheme.get("name") or ""),
                            scheme_type=str(scheme.get("type") or ""),
                        )
                    except Exception:
                        logger.warning("章节 %s 检查点自检失败（忽略，不影响落库）", section_id[:8], exc_info=True)
                        _ck_findings = []
                    # ✅ R29（2026-10-02 · 检查点反哺）：CON-06 跨章节段落搬运。
                    #    这是唯一一个**结构上无法在提示词预防**的检查点 ——
                    #    生成单章时模型看不到其他章节的正文，system 级「禁止
                    #    成段雷同」只能提高概率、无法保证。生产库实证 3 条
                    #    CON-06 全部是「骨架归一后相似度 100%」（整段照抄，
                    #    连数字都没换），属评审硬伤。该判据只能在「本章已生成、
                    #    其余章节已在库」时判定，故挂**生成后自检**而非提示词。
                    #    - 判据直接复用 duplicate_detection 的同一实现，
                    #      不在生成侧重抄相似度阈值与骨架归一规则（判据分叉病根）；
                    #    - 锁外只读、fail-soft：查询异常只打 WARNING，
                    #      绝不影响正文落库（正文已在锁内事务里保证原子）；
                    #    - 只报涉及本章的搬运组，避免把历史搬运重复报出。
                    if _crosscheck_dup_on or _crosscheck_values_on:
                        _dup_secs: list = []
                        try:
                            if _crossdup_snapshot is None:
                                _dup_cur = await db.execute(
                                    "SELECT id, title, content FROM sections"
                                    " WHERE scheme_id=? AND content IS NOT NULL"
                                    " AND content != ''",
                                    (scheme_id,),
                                )
                                _dup_rows = await _dup_cur.fetchall() if _dup_cur is not None else []
                                _crossdup_snapshot = {
                                    _r["id"]: {
                                        "id": _r["id"],
                                        "title": _r["title"],
                                        "content": _r["content"],
                                    }
                                    for _r in _dup_rows
                                }
                            # ✅ BUG 修复（R58）：快照更新与搬运检测必须在**每章**执行，
                            #    不得只在「首次加载」分支内。旧结构把 _crossdup_snapshot[section_id]
                            #    赋值与 cross_section_copy_findings 调用都嵌在
                            #    `if _crossdup_snapshot is None:` 内 → 第二章起快照已非 None，
                            #    整块被跳过 → CON-06 只在第一章运行（无对 比 章 可 比 较 → 恒无 finding），
                            #    生产中跨章搬运从第二章起 100% 漏检。
                            _crossdup_snapshot[section_id] = {
                                "id": section_id,
                                "title": _ck_title,
                                "content": content,
                            }
                            _dup_secs = list(_crossdup_snapshot.values())
                            # ⚠️ 20 字门槛只用于跳过「搬运」判定（性能护栏），数值一致性自检不受它约束 ——
                            # 短正文同样可能写出与其它章节矛盾的工期/人数取值。
                            if _crosscheck_dup_on and len(content or "") >= 20:
                                # ✅ R178（B-1 · 2026-11-05）：跨章搬运判定是**纯 CPU**
                                #    同步计算，复杂度 O(N²)（实测 N=10/20/40 章 →
                                #    37/83/195 ms，隐含指数 2.36，见
                                #    artifacts/r178/bench_outline_rebuild.py）。
                                #    此前在**事件循环内直跑** ⇒ 每章生成后把整个
                                #    事件循环卡住 195 ms（N=40 的方案全书约 7.8 s），
                                #    SSE 心跳与其它请求全部被拖慢。
                                #    现与 B-4（预检移入工作线程）同构：整段挪进
                                #    ``asyncio.to_thread`` —— **判据逐字不变**（同一
                                #    函数、同一参数），只改执行位置。
                                #    安全性：① 本段在 ``_db_write_lock``（:3534）
                                #    之前、**不在任何锁内**；② ``_dup_secs`` 已切片，
                                #    快照后续只增不改 ⇒ 让出控制权不影响判据输入。
                                _ck_findings.extend(
                                    await asyncio.to_thread(
                                        cross_section_copy_findings, _dup_secs, new_section_id=section_id
                                    )
                                )
                        except Exception:
                            logger.warning(
                                "章节 %s 跨章搬运检测失败（忽略，不影响落库）", section_id[:8], exc_info=True
                            )
                    # ✅ R52（2026-10-07）：CON-01 跨章节数值一致性自检。
                    # 复用 _crossdup_snapshot（与 CON-06 同形），调用 content_data_contract.
                    # cross_section_value_findings（与预检 numeric_consistency_findings 同源
                    # 判据实现），只报涉及本章的冲突。与搬运开关互相独立；_dup_secs 预初始化，
                    # 快照构建失败时退化为空列表（自检空转，绝不阻断落库）。
                    if _crosscheck_values_on:
                        try:
                            # ✅ R178（B-1）：同上，数值一致性自检同样是纯 CPU 的
                            #    全量重算（内部调 numeric_consistency_findings(全量)），
                            #    同样移入工作线程，判据逐字不变。
                            _ck_findings.extend(
                                await asyncio.to_thread(
                                    cross_section_value_findings, _dup_secs, new_section_id=section_id
                                )
                            )
                        except Exception:
                            logger.warning(
                                "章节 %s 数值一致性自检失败（忽略，不影响落库）", section_id[:8], exc_info=True
                            )
                    if _ck_findings:
                        report = dict(report or {})
                        report["checkpoint_findings"] = _ck_findings
                        if _selfcheck_autofix:
                            _ck_err = 0
                            _ck_warn = 0
                            for _f in _ck_findings:
                                _sev = "error" if _f.get("severity") == "error" else "warning"
                                report.setdefault("issues", []).append(
                                    {
                                        "type": f"checkpoint_{_f.get('rule_id', '')}",
                                        "severity": _sev,
                                        "message": _f.get("message", ""),
                                        "excerpt": "",
                                    }
                                )
                                if _sev == "error":
                                    _ck_err += 1
                                else:
                                    _ck_warn += 1
                            report["error_count"] = int(report.get("error_count") or 0) + _ck_err
                            report["warning_count"] = int(report.get("warning_count") or 0) + _ck_warn
                            report["passed"] = bool(report.get("passed", True)) and _ck_err == 0
                        try:
                            report_json = json.dumps(report, ensure_ascii=False)
                        except Exception:
                            logger.debug(
                                "章节 %s 报告 JSON 序列化失败（report_json 维持原值，" "不阻断落库）",
                                section_id[:8],
                                exc_info=True,
                            )

                # ---------- 锁内：最小事务（图表重登记 + 正文 UPDATE） ----------
                async with _db_write_lock:
                    try:
                        if _chart_ok:
                            # ✅ B2 配套：锁内复核超限时同步裁剪正文（幽灵图防护）
                            # ✅ R108（2026-10-18 · 日志审计 P0-1）：图表登记整体
                            #    失败（含 DELETE 重试预算耗尽）不再拖垮正文落库。
                            #    生产实证：批量生成 + 审计 flush 并发下
                            #    `DELETE FROM chart_predictions` 抛 locked 直通冒泡
                            #    → 已生成正文随异常丢失（tracebacks block#8）。
                            #    与下方 _chart_ok=False 清理分支同口径降级：
                            #    回滚半途图表写 → WARNING → 正文照常落库，本章
                            #    登记回落上一轮行 / 部分孤儿行，下次持久化全量重登记
                            #    自动修复（「正文是主产物，登记残留可后清」）。
                            _chart_apply_failed = False
                            try:
                                content = await apply_inline_chart_plan(
                                    db, section_id, _chart_rows, content, dropped_out=_chart_dropped
                                )
                            except Exception as _apply_e:
                                try:
                                    await db.rollback()
                                except Exception:
                                    logger.debug(
                                        "章节 %s 图表登记失败后的回滚也失败" "（放弃清理，不影响正文落库）",
                                        section_id[:8],
                                        exc_info=True,
                                    )
                                _chart_apply_failed = True
                                logger.warning(
                                    "章节 %s 内联图表登记失败（重试预算耗尽），"
                                    "正文仍优先落库；本章图表登记保留上一轮行"
                                    "（可能残留/缺失），下一轮持久化自动重登记: %s",
                                    section_id[:8],
                                    _apply_e,
                                )
                            if not _chart_apply_failed:
                                # ✅ 字数必须按【裁剪后】正文重算：apply_inline_chart_plan
                                #    会删掉超出配图上限的图表块，块内文字随之消失。
                                #    旧实现沿用裁剪前的 wc/ws → 库里字数、SSE 载荷、
                                #    超字数统计三处口径漂移（front-end 显示的字数与
                                #    实际正文对不上）。
                                wc = text_word_count(content)
                                ws = word_status_for(wc, word_budget)
                                # ✅ R48：删图可观测（P1 · 图表模块深度审查）
                                #    build_inline_chart_plan / apply_inline_chart_plan 两条
                                #    路径的删块理由（每章超限 / 全方案同类型超限 / 校验修复
                                #    失败 / ai_image 缺 prompt / 事务内复核跳过）此前只写
                                #    backend.log —— 用户看到「正文里图没了、图表清单没有
                                #    这张图」却完全无从解释，也不知道是限流还是校验失败。
                                #    现汇总进 report（加法式键 charts_dropped/chart_count），
                                #    并写一条带 scheme/task 标识的 INFO。
                                #    ⚠️ 必须在此处**重算 report_json**：上面的 json.dumps
                                #    发生在锁之前，若不重算，charts_dropped 永远不会出现在
                                #    落库的 report_json 里（护栏 test_charts_dropped_persists
                                #    锁定该顺序）。
                                # ✅ R49（图表产出闭环）：chart_count = 最终正文里真实存活的图表块数。
                                #    与 charts_dropped_count 配对即得完整口径「提名 ≈ 保留 + 删除」，
                                #    用户可区分「本应配图但 AI 零产出」（提示词/模型问题）与
                                #    「产出了但被删」（限流/校验问题）—— 此前 report 只有
                                #    "被删的"没有"留下的"，两种截然不同的情况无法分辨。
                                #    计数走 count_inline_charts（与 has_inline_charts 同一事实来源，
                                #    仅统计闭合且可判定的图表围栏，未闭合残片不计）。
                                try:
                                    _chart_count = count_inline_charts(content)
                                except Exception:
                                    _chart_count = 0
                                if _chart_dropped:
                                    report["charts_dropped"] = _chart_dropped
                                    report["charts_dropped_count"] = len(_chart_dropped)
                                report["chart_count"] = _chart_count
                                # ⚠️ 重算时机的语义变化：R48 只在「有删图」时重算，R49 起**总是**重算
                                # —— chart_count 恒有值，若沿用条件重算它同样永远不会落库。
                                try:
                                    report_json = json.dumps(report, ensure_ascii=False)
                                except Exception:
                                    logger.debug(
                                        "章节 %s 报告 JSON 重算序列化失败" "（report_json 维持原值，不阻断落库）",
                                        section_id[:8],
                                        exc_info=True,
                                    )
                                if _chart_dropped:
                                    logger.info(
                                        "正文生成删除内联图表 %d 个（scheme=%s · task=%s · "
                                        "%s），理由：§5.9「删图必须用户可解释」",
                                        len(_chart_dropped),
                                        scheme_id[:8],
                                        task_id[:8] if task_id else "-",
                                        "、".join(f"{chart_drop_label(d)}" for d in _chart_dropped),
                                    )
                        else:
                            # ✅ R48（P1 · 僵尸图 / 登记与正文双向不一致）：
                            #    图表登记计算（build_inline_chart_plan）异常时也必须清理本章旧登记。
                            #    旧实现只在 _chart_ok=True 时调 apply_inline_chart_plan，而该函数
                            #    **第一步**就是 `DELETE FROM chart_predictions WHERE section_id=?`
                            #    —— 计算失败时这条 DELETE 从不执行，但下方的正文 UPDATE 仍**无条件**
                            #    提交，于是出现「正文是这一轮的新值、图表登记是上一轮的旧值」：
                            #      · 导出把已不在正文里的旧图追加到章末（chart_predictions 是导出主数据源）；
                            #      · 图表清单显示正文里根本不存在的图（幽灵图，用户无法定位）；
                            #      · 导出预检 / 一致性扫描的图表统计虚高。
                            #    三者都静默发生，只有 `章节 xxx 内联图表登记失败` 一条日志，
                            #    且该日志文案是「本次跳过图表登记，不影响正文」——**没提旧登记残留**，
                            #    排障者会误以为库是干净的。修法：异常分支也做「仅清理」调用
                            #    （rows=[] + content=None → 只执行 DELETE、不动正文），
                            #    使「正文落库」与「本章登记」在每个出口都成对一致。
                            try:
                                await apply_inline_chart_plan(db, section_id, [], None)
                                logger.info(
                                    "章节 %s 图表登记计算失败，已同步清空本章旧登记"
                                    "（避免正文与 chart_predictions 不一致的僵尸图）",
                                    section_id[:8],
                                )
                            except Exception as _clean_e:
                                # 清理失败不阻断正文落库：正文是主产物，且此处处于写锁临界区，
                                # 二次异常会让整章正文都丢（比旧登记残留严重得多）。
                                logger.warning(
                                    "章节 %s 图表登记清理失败（正文仍按原计划落库，"
                                    "本章可能残留上一轮的旧图表登记）: %s",
                                    section_id[:8],
                                    _clean_e,
                                )

                        # ✅ BUG 修复（2026-10-11 · 跨模块数据链 R70 · plan_json 静默抹除）：
                        #    D1 离散编排默认关闭（content_plan_stage_enabled=False），
                        #    _plan_json 恒为 ""；而续写分支（mode=continue）更是显式以
                        #    _plan_enabled=False 构建上下文（:6129），同样拿到 ""。
                        #    旧实现无条件 `SET plan_json=?` ⇒ **任何一轮生成/续写都会把
                        #    上一轮真实落库的编排决策抹成空串**，D1 的编排成果无法跨轮存活
                        #    （开关打开→跑一轮→关掉或续写一章即全丢），且无任何日志。
                        #    修法：空串时保留原列（CASE WHEN），非空才覆盖。
                        #    向后兼容：plan_json 非空时 SQL 语义与旧实现逐字一致。
                        # ✅ R108（P0-1）：正文 UPDATE + commit 作为一对幂等写接
                        #    retry_db_op（稍大预算：正文是本链路的最终主产物）。
                        #    commit 撞瞬态锁时事务仍未提交，重放 UPDATE 为幂等覆盖，
                        #    安全；确定性错误原样抛出，走下方整体回滚。
                        async def _persist_content_once():
                            await db.execute(
                                "UPDATE sections SET content=?, word_count=?, word_status=?, "
                                "status='generated', updated_at=?, "
                                "last_generation_standard=?, last_generation_report=?, "
                                "plan_json = CASE WHEN ? <> '' THEN ? ELSE plan_json END "
                                "WHERE id=?",
                                (
                                    content,
                                    wc,
                                    ws,
                                    datetime.now().isoformat(),
                                    # F-CONTENT-STANDARD: persist generation standard + report
                                    eff_standard or "",
                                    report_json,
                                    plan_json or "",
                                    plan_json or "",
                                    section_id,
                                ),
                            )
                            await db.commit()

                        await retry_db_op(_persist_content_once, max_retries=4, base_delay=0.1)
                    except Exception:
                        # ✅ 整体回滚：绝不让半途事务（含已删图表登记）被后续 commit 提交
                        try:
                            await db.rollback()
                        except Exception:
                            logger.debug(
                                "章节 %s 落库失败后的回滚也失败（原异常继续上抛）", section_id[:8], exc_info=True
                            )
                        raise
                done_ids.add(section_id)
                generated_contents[section_id] = content
                # ✅ 编号统一（2026-09-26 · 显式跨校验器）：生成后校验目录与正文
                #    编号一致（原始需求 五.6 前半）。落库前已按同一算法规范化
                #    （normalize_section_content_subheadings），此处为显式复核：
                #    仅在出现异常残留（理论上不应发生）时 WARNING 留痕，
                #    不阻断本次生成、不影响返回内容；修复入口
                #    /sections/numbering-consistency/repair。
                try:
                    _vrep = await validate_section_content_numbering(
                        db,
                        scheme_id,
                        section_id,
                        content,
                        # ✅ R5：与规范化共用同一份预取索引（省掉逐章 2 次查库）
                        index=_numbering_index,
                    )
                    if not _vrep.get("consistent") and not _vrep.get("skipped"):
                        logger.warning(
                            "章节 %s 生成后编号一致性复核未通过（落库正文与目录编号"
                            "不一致，%d 处差异），可用 /numbering-consistency/repair 修复。",
                            section_id[:8],
                            len(_vrep.get("diffs") or []),
                        )
                except Exception as _ve:  # noqa: BLE001 — 复核失败不影响生成结果
                    logger.debug("章节 %s 生成后编号复核异常（忽略）: %s", section_id[:8], _ve)
                # ✅ 进度增强：累计已落库正文字数（心跳统计展示用）
                _prog["words"] = _prog.get("words", 0) + (wc or 0)
                # ✅ 断线兜底清单：成功落库的章节数 / 已生成字数（finally 可读）
                _ckpt["done"] = _ckpt.get("done", 0) + 1
                _ckpt["words"] = _prog["words"]
                # ✅ 本次运行超字数章节计数（完成消息与 SSE 载荷使用）
                if ws == "over":
                    _stat["over"] = _stat.get("over", 0) + 1
                return content, wc, ws, report

            # ✅ B1：_failed_ids / _failed_reasons 已在 try 之前预初始化
            #    （早期 except/finally 收尾要读它们），此处不得重复绑定。

            def _count_failed(section_id: str, title: str = "", reason: str = ""):
                """登记一次章节失败（**只计数，不改库状态**）。

                ✅ BUG 修复：旧实现把"计数"和"改库状态"绑在一起（只在
                `_mark_section_failed` 内累加），导致两类不一致：
                  1) 强制重写时章节本有旧正文，失败后**不得**把状态打成 failed
                     （否则 missing 模式要求 content 为空才重选、普通模式又跳过非空
                     章节 → 该章从此无法被任何模式选中，只能手改）；既然不改库，
                     旧实现也就漏计了失败数 → 运行中 stats.failed 与终态
                     failed_count（done_ids 口径）自相矛盾；
                  2) 同一章可能被多条路径标记，需去重（用 _failed_ids 保证只计一次）。
                现拆开：本函数只管计数与明细，状态标记由 _mark_section_failed 决定。
                """
                if section_id in _failed_ids:
                    return
                _failed_ids.add(section_id)
                _failed_reasons[section_id] = {"section_id": section_id, "title": title or "", "reason": reason or ""}
                _prog["failed"] = _prog.get("failed", 0) + 1
                # ✅ 断线兜底清单：失败明细（finally 可读，上限与 completed 载荷一致）
                _ckpt["failed_sections"] = list(_failed_reasons.values())[:50]

            async def _mark_section_failed(section_id: str, title: str = "", reason: str = "", *, mark_db: bool = True):
                """登记章节失败：计数总在跑，DB 状态标记按 mark_db 决定。

                mark_db=False 用于「章节本有旧正文」的失败（强制重写 / 续写场景）：
                保留其原有状态（generated/reviewed…），只计入失败统计。
                """
                _count_failed(section_id, title, reason)
                if not mark_db:
                    return
                async with _db_write_lock:
                    # ✅ R169：与正文主落库（_persist_content_once）同族接
                    #    retry_db_op——WAL 锁竞争窗口下本章失败标记裸写曾直接抛
                    #    locked 逃出 gen_one（其 try 只兜 CancelledError），造成
                    #    同章双份 section_error 与 stats/库状态漂移。单语句 UPDATE
                    #    幂等可重放；确定性错误原样上抛走既有兜底。
                    async def _mark_failed_once():
                        await db.execute(
                            "UPDATE sections SET status='failed', updated_at=? WHERE id=?",
                            (datetime.now().isoformat(), section_id),
                        )
                        await db.commit()

                    await retry_db_op(_mark_failed_once, max_retries=4, base_delay=0.1)

            async def _build_generation_context(
                leaf: dict, eff_standard: str = "", _plan_enabled: bool = False
            ) -> tuple[str, list, list, str, str]:
                """子函数1：构建章节生成的 AI 上下文（system prompt + user content）。
                返回 (生效标准, 本章相关事实行, messages, user_content)，
                user_content 供续写阶段复用。

                ✅ 2026-09-26（B2）：生效标准改由调用方（章节循环）预先解析并经
                `eff_standard` 传入，保证 section_start / 提示词 / 校验报告
                三处同源，避免事件字段与实际提示词口径漂移。

                Args:
                    leaf: 叶子章节 dict。
                    eff_standard: 调用方已解析好的生效标准；留空时内部兜底解析
                        （保持旧调用点可用，但会导致事件字段缺失 → 应显式传入）。
                """
                if not eff_standard:
                    eff_standard = resolve_effective_standard(
                        task_standard=task_standard,
                        override=override_section_standard,
                        section_standard=leaf.get("generation_standard", "") or "",
                        scheme_standard=scheme.get("generation_standard", "") or "",
                    )
                # ✅ 清理（2026-09-16 · ruff F841）：此处的 `leaf_id = leaf["id"]`
                #    是死赋值（下方一律用 leaf["id"]），删除以免与新代码混淆。
                parent_chain = _build_parent_chain(all_sections, leaf["id"], nodes_map, by_parent)
                # ✅ 增强：上级章节的标题 + 描述（旧实现只给标题链，AI 难以判断父级职责边界）
                parent_points: list[str] = []
                _pid = leaf.get("parent_id", "")
                while _pid and _pid in nodes_map:
                    _p = nodes_map[_pid]
                    _pd = (_p.get("description") or "").strip()
                    if _pd:
                        parent_points.append(f"- {_p.get('title', '')}：{_pd}")
                    _pid = _p.get("parent_id", "")
                parent_points.reverse()
                # ✅ 增强：同级章节标题 + 描述，以及前序同级正文摘要
                #    （摘要优先取本次生成过程中已完成的实时正文，使"风格延续"真正生效）
                sibling_lines, prev_sibling_summary = build_sibling_context(
                    all_sections, leaf, generated_contents=generated_contents, children_by_parent=by_parent
                )

                word_budget = leaf_word_budget(leaf)
                # ✅ R58（2026-10-10）：三个逐章量**前置**到事实注入之前 ——
                #    事实分段注入需要读 _stable_prefix（决定走稳定段还是单条 user）。
                _stable_prefix = bool(getattr(settings, "content_stable_prefix_enabled", True))
                _sec_no = _section_outline_number(leaf)
                _sub_rule = build_subheading_rule(
                    has_db_children=str(leaf.get("id") or "") in _parent_ids,
                    # ✅ R181（2026-11-06）：示例按本节实际编号生成，不再写死 1.1
                    section_prefix=_sec_no,
                )
                # ✅ 逐章精选：按本章标题/描述从已加载事实行中预筛相关事实
                #    （旧实现只在生成循环外算一次，导致所有章节共享同一份事实）
                # ✅ P1 修复（2026-09-27 · 九大章节分类对正文零影响）：
                #   chapter 维度的「章节内事实前置」是九大章节分类接进正文提示词的
                #   **唯一通道**，此前调用点既没传 chapter、开关 facts_chapter_inject
                #   又默认 False —— 于是 global_facts.chapter 打了标却完全不参与
                #   正文生成（需求「九大章节分类」的核心价值未兑现）。
                #   现补传 chapter（命中不到返回空串 → 行为与旧实现完全一致，
                #   零回归风险），并按章事实优先消费预算。
                # ✅ R58（2026-10-10）：事实**分段注入**。
                #    旧实现把「按章预筛后的事实」整块放进 R57 的「整案稳定上下文」
                #    段 —— 可预筛结果本来逐章不同，于是所谓稳定段每章都在变，
                #    Prompt Cache 前缀失效（F1 的收益被这一处抵消）。
                #    现拆为：通用组（整案恒定 → 稳定段）+ 其余（按章命中 → 任务段）。
                #    开关关闭时逐字保持旧行为。
                # ✅ R105（G3 · 注入审计单一出口）：改走 fact_injection_audit——
                #    内部复用同一次 _render_facts_text（per_fact=300；core 侧
                #    leaf=None/chapter="" 即 relevant_to=None/chapter=""），
                #    只多带回「预算丢弃了哪些事实」的审计视图。
                _core_audit = _chapter_audit = _plain_audit = None
                _core_facts_rows: list = []
                _chapter_budget = CONTENT_FACTS_TOTAL_BUDGET
                if _stable_prefix and bool(getattr(settings, "content_facts_split_inject", True)):
                    _core_facts_rows = core_facts_rows(facts_rows)
                    _rest_facts_rows = exclude_rows(facts_rows, _core_facts_rows)
                    # ✅ L7/BUG-12（R177 · 2026-11-04）：core 与 chapter 共享
                    #    CONTENT_FACTS_TOTAL_BUDGET，core 先算，chapter 只用剩余额度；
                    #    旧实现两侧各 6000，总注入量可达 12000。
                    _core_audit = fact_injection_audit(_core_facts_rows, max_total=CONTENT_FACTS_TOTAL_BUDGET)
                    facts_text = _core_audit["rendered_text"]
                    _chapter_budget = max(0, CONTENT_FACTS_TOTAL_BUDGET - len(facts_text))
                    _chapter_audit = fact_injection_audit(
                        _rest_facts_rows,
                        leaf=leaf,
                        chapter=chapter_key_of_title(str(leaf.get("title") or "")),
                        max_total=_chapter_budget,
                    )
                    _chapter_facts_text = _chapter_audit["rendered_text"]
                else:
                    _plain_audit = fact_injection_audit(
                        facts_rows, leaf=leaf, chapter=chapter_key_of_title(str(leaf.get("title") or ""))
                    )
                    facts_text = _plain_audit["rendered_text"]
                    _chapter_facts_text = ""
                # ✅ 编制依据注入：按方案类型 + 本章标题命中现行有效标准清单，
                #    禁止 AI 引用已废止版本或杜撰标准编号（见 standards_registry）
                standards_text = get_standards_text(
                    scheme.get("name", ""), scheme.get("type", ""), section_title=leaf.get("title", "")
                )
                # ✅ 2026-10-02（检查点前置）：本章属九大法定章节时注入
                #    「本章审核检查点要求」（必含要素 + 锚定的预检 rule_id）。
                #    变量**只在非空时传入**：未传时独占行占位符整行丢弃，
                #    未命中章节 / 开关关闭 → 下发模板与本功能引入前逐字一致。
                # ✅ 2026-10-02（章节 key 补充推断）：主判据 chapter_key_of_title
                #    对「应急组织机构及职责 / 应急物资装备保障 / 应急演练」这类小节
                #    标题返回空串 → SAF-03/04/05 三要素在生成侧从未被要求过。
                #    现经 infer_chapter_key 补充兜底（主判据命中时不覆盖，杜绝
                #    改变 facts 注入与提取分类的既有归类结果）。
                _ck_title = str(leaf.get("title") or "")
                # 2026-10-03 增强：块尾追加该章**完整**必含要素清单（P0-1）。
                # 此前生成侧只看到每条要求的一两个主题词，NINE_CHAPTERS 的
                # 完整清单（通用要素 + 危大类别追加要素）从未进入提示词。
                # 受 content_chapter_elements_inject 控制（默认 True，
                # False = 提示词与引入前逐字一致）。
                _chapter_ck = (
                    build_chapter_checkpoint_block(
                        infer_chapter_key(_ck_title, chapter_key_of_title(_ck_title)),
                        _ck_hazardous,
                        include_elements=bool(getattr(settings, "content_chapter_elements_inject", True)),
                        scheme_name=str(scheme.get("name") or ""),
                        scheme_type=str(scheme.get("type") or ""),
                    )
                    if _ck_prepend
                    else ""
                )
                _ck_kwargs: dict = {}
                if _checkpoint_block:
                    _ck_kwargs["content_checkpoint_block"] = _checkpoint_block
                if _chapter_ck:
                    _ck_kwargs["chapter_checkpoint_block"] = _chapter_ck
                # ✅ R57（2026-10-09 · 提示词前缀稳定化，开关 content_stable_prefix_enabled）：
                #    依据《标书智能体（四）——提示词顺序优化，让缓存命中，输入成本直降10倍》
                #    §五：各厂商 Prompt Cache 命中的是**请求前缀**，而不是「某段内容看起来
                #    一样」。旧实现 system 自首字符起即逐章变化 —— section_number /
                #    standards_text / subheading_rule / 本章检查点块全是逐章量，于是同一
                #    方案每换一章，前缀整体失效：项目概述、施工内容、全局事实、知识库这些
                #    真正的大块内容被反复全价计费。
                #    开启时逐章变量注入**空串**（变量照传，模板占位符既不残留也不被整行
                #    丢弃），其真实值原样落到最后一条 user 消息的【本章编写定位】段，
                #    信息零丢失；整案常量的检查点块（外层只算一次）仍留在 system。
                # ✅ 决策下沉（services/content_runtime.resolve_system_variables）：
                #    纯函数、可单测、可被 A/B 反向验证覆盖。
                _sys_vars = resolve_system_variables(
                    stable=_stable_prefix,
                    section_number=_sec_no,
                    standards_text=standards_text,
                    subheading_rule=_sub_rule,
                )
                # ✅ 保持**静态可解析**形态（护栏 TestRenderDynamicBlindspots）：
                #    用字典推导生成 render 的 kwargs 会把该调用点降级为
                #    「动态盲区」，此后漏传模板变量再也测不出来；故逐条显式赋值，
                #    与原 _ck_kwargs 同句型。
                _ck_sys_kwargs: dict = {}
                if _checkpoint_block:
                    _ck_sys_kwargs["content_checkpoint_block"] = _checkpoint_block
                if _chapter_ck and not _stable_prefix:
                    # 本章检查点块同样是逐章量 → 稳定化模式下移出 system，
                    # 其真实值由 build_chapter_extras_block 补给模型。
                    _ck_sys_kwargs["chapter_checkpoint_block"] = _chapter_ck
                sys_prompt = render(
                    "content_generation_system",
                    scheme_name=scheme.get("name", ""),
                    scheme_type=scheme.get("type", ""),
                    # ✅ R57：逐章变量虽已由 resolve_system_variables 决策，
                    #    仍必须**逐个显式**书写 —— 若写成 **_sys_vars 展开，
                    #    该调用点会被 RenderDynamicBlindspots 判为动态盲区，
                    #    从此漏传模板变量也测不出来（静态护栏退化）。
                    section_number=_sys_vars["section_number"],
                    standards_text=_sys_vars["standards_text"],
                    # ✅ BUG 修复（2026-09-26）：补齐 {subheading_rule} 的生成方。
                    #    该占位符自登记进 PROMPT_VARIABLE_CONTRACTS 起就**无人注入**，
                    #    render 时被丢弃 → 发给模型的「章节内部小标题编号规范」整段为空，
                    #    AI 只能自由发挥子标题编号（跳号 / 混用 / 与子章节撞号）。
                    #    命名空间判定与正文落库规范化
                    #    （numbering.normalize_section_content_subheadings）、
                    #    导出渲染（export._compute_subheading）三处同口径。
                    subheading_rule=_sys_vars["subheading_rule"],
                    **_ck_sys_kwargs,
                )

                # F-CONTENT-STANDARD: 生成标准段落已由调用方解析（eff_standard），
                # 此处只负责渲染（DB 定制模板无占位符时也能生效）。
                sys_prompt += "\n\n" + build_system_block(eff_standard)

                # ✅ 2026-09-28（T-1 收口）：user 上下文装配下沉到服务层纯函数
                #    services/content_runtime.build_chapter_user_content（逐字搬迁，
                #    无逻辑改动）。本处只计算闭包依赖的入参：
                #    - content_scope  ← _outline_construction_scope（scheme_scope 唯一口径）
                #    - knowledge_text ← _knowledge_text_for（知识库素材，闭包内 DB 读取）
                #    - word_budget_hint ← _word_budget_hint（空串回报 "N字"，同旧实现）
                knowledge_text = _knowledge_text_for(leaf)
                # ✅ D-39（R176）：逐章知识库硬切的降级事件由调用方（gen_one，
                #    异步生成器）在拿到返回后立即 flush 下发 —— 本函数是普通
                #    async 函数且需返回值，不能在此 yield（否则变成异步生成器，
                #    `return <value>` 直接 SyntaxError）。
                # ✅ R58（2026-10-10）：图表配额感知 —— 让 AI 在**配额内**提名，
                #    避免「画了又被程序按上限删掉」。纯读一次全方案已用计数
                #    （已排除本章），失败不阻断生成（软提示）。
                _chart_hint = ""
                if _stable_prefix and bool(getattr(settings, "content_chart_quota_hint_enabled", True)):
                    try:
                        _chart_hint = build_chart_quota_hint(
                            await _load_scheme_type_counts(db, scheme_id, str(leaf.get("id") or "")),
                            word_budget=word_budget,
                        )
                    except Exception:  # noqa: BLE001 - 软提示，绝不阻断生成
                        logger.warning("图表配额统计失败（按无提示继续）", exc_info=True)
                # system 中被清空的逐章变量，在这里**原样**补给模型（保证零信息丢失）：
                # 两个标量 + 子标题规范 + 本章检查点块，作为「任务差异」的最后一段。
                _chapter_extras = ""
                if _stable_prefix:
                    _chapter_extras = build_chapter_extras_block(
                        section_number=_sec_no,
                        standards_text=standards_text,
                        subheading_rule=_sub_rule,
                        chapter_checkpoint_block=str(_ck_kwargs.get("chapter_checkpoint_block") or ""),
                    )

                # ✅ D1（2026-10-11 · 离散正文编排阶段，默认关闭）：在组装生成消息前，
                #    先由独立 LLM 步为当前章产出结构化编排决策（写作聚焦 / 建议素材 /
                #    建议事实 / 是否表格），作为**软建议**注入「本章任务差异段」，并随
                #    正文落库。开关关闭或编排失败时 _plan_json 恒为 "" 且不触发任何额外
                #    LLM 调用 —— 缺省路径与本项引入前逐字一致。本步刻意只给建议、不硬
                #    限制模型可用素材范围，回避 R57 论证的「预编排=陈旧结论陷阱」风险。
                _plan_json = ""
                # ⚠️ R74（G3）：引导语变量必须在 try 之外初始化 —— 旧实现只在
                #    try 内赋值，编排中途抛异常时下方 else 分支引用会 NameError。
                _plan_guidance = ""
                if _plan_enabled and bool(getattr(settings, "content_plan_stage_enabled", False)):
                    try:
                        _k_pool = [str(r.get("name") or "") for r in knowledge_rows]
                        # ⚠️ facts_rows 的行契约是 6 元组 (gt, title, content,
                        #    confidence, chapter, value_kind)（facts_builder.
                        #    _load_facts_rows 自 R80 起投影 6 元组，不得直接外泄），
                        #    不是 dict —— 用
                        #    .get() 会 AttributeError，被下方 fail-soft 吞掉后 D1 编排
                        #    阶段在每一章都静默空转（功能全灭、日志只有一条 WARNING）。
                        #    按 _row_chapter / _render_facts_text 同口径按下标取 title。
                        _f_pool = [str(r[1] or "") for r in facts_rows if isinstance(r, (tuple, list)) and len(r) > 1]
                        _plan = await plan_section_content(
                            chat_with_fallback,
                            leaf=leaf,
                            word_budget=word_budget,
                            parent_chain=parent_chain,
                            sibling_lines=sibling_lines,
                            project_brief=project_brief,
                            knowledge_names=_k_pool,
                            fact_titles=_f_pool,
                            scheme_name=str(scheme.get("name") or ""),
                            scheme_type=str(scheme.get("type") or scheme.get("engineering_type") or ""),
                        )
                        _plan_json = _plan.to_json()
                        _plan_guidance = render_content_plan_guidance(_plan)
                        if _plan_guidance and _chapter_extras:
                            _chapter_extras = _chapter_extras + "\n" + _plan_guidance
                        elif _plan_guidance:
                            _chapter_extras = _plan_guidance
                        # ✅ R122-G1（引入易标「编排选事实→生成注事实」闭环，
                        #    参考 contentGenerationTask.cjs:134/171 appendSelectedFactsMessage）：
                        #    编排阶段 AI 只看标题清单挑事实，生成阶段由**程序**按标题
                        #    取回完整事实行、只注入当前章节。开关
                        #    `content_plan_selected_facts_inject` 默认关闭 ⇒ 整段不执行，
                        #    `_chapter_audit`/`_chapter_facts_text` 逐字节保持上方 D1 前算好的值。
                        #    仅分段注入模式（_chapter_audit 非 None）才重算——单条 user
                        #    模式事实已全量进 facts_text，无处可加选中强注入。
                        if (
                            bool(getattr(settings, "content_plan_selected_facts_inject", False))
                            and _chapter_audit is not None
                            and _plan.fact_titles
                        ):
                            _sel_rows = resolve_selected_fact_rows(_plan.fact_titles, facts_rows)
                            # ✅ L7/BUG-12（R177 · 2026-11-04）：选中事实先剔除
                            #    core 已注入行 —— 同一事实一次请求只出现一次；
                            #    再并回本章预筛行（exclude_rows 身份+相等双判）。
                            _sel_rows = exclude_rows(_sel_rows, _core_facts_rows)
                            if _sel_rows:
                                _union_rows = _rest_facts_rows + exclude_rows(_sel_rows, _rest_facts_rows)
                                _chapter_audit = fact_injection_audit(
                                    _union_rows,
                                    leaf=leaf,
                                    chapter=chapter_key_of_title(str(leaf.get("title") or "")),
                                    full_titles=_plan.fact_titles,
                                    max_total=_chapter_budget,
                                )
                                _chapter_facts_text = _chapter_audit["rendered_text"]
                    except Exception:  # noqa: BLE001 - 编排失败绝不阻断正文生成
                        logger.warning(
                            "章节 %s 离散编排阶段失败（回退为无编排）", str(leaf.get("title") or ""), exc_info=True
                        )
                        _plan_json = ""

                # ✅ R179 Phase F（2026-11-06 · 分区混合生成，默认关闭）：
                #    按节点 content_type 决定注入策略 —— 规范驱动节点检索条款并注入
                #    「条款上下文 + 数值锁清单」，经验驱动节点注入事实引用与空话黑名单。
                #    开关关闭时 _hybrid_user_block 恒为 ""、_hybrid_by_leaf 无记录，
                #    两条装配路径与本项引入前逐字一致。注入失败不阻断生成，但必须
                #    留下「无法判定」记录 —— 不许把「没检查」显示成「没问题」。
                _hybrid_user_block = ""
                if bool(getattr(settings, "hybrid_generation_enabled", False)):
                    _hroute = None
                    try:
                        _hroute = route_node(leaf)
                        # ✅ 2026-11-07：只对规范驱动节点检索（经验驱动节点不查库，省预算）。
                        #    检索键是「文种收敛出的候选标准号」，不是章节标题。
                        _hresult = None
                        if _hroute.strategy == "rag":
                            _hresult = await search_for_section(
                                db,
                                _hroute.title,
                                content_type=_hroute.content_type,
                                category_ids=_hybrid_category_ids,
                                template_key=_hybrid_template_key,
                                budget=_hybrid_search_budget,
                            )
                        _hallowed = allowed_numbers_for(_hroute, _hresult.hits if _hresult else [])
                        _hblock = build_hybrid_block(_hroute, _hresult, _hallowed, blacklist=blacklist_from_settings())
                        _hybrid_by_leaf[str(leaf.get("id") or "")] = {
                            "route": _hroute,
                            "allowed": _hallowed,
                            "result": _hresult,
                            "hits": len(_hresult.hits) if _hresult else 0,
                            "search_status": _hresult.status if _hresult else "ok",
                            "search_message": _hresult.message if _hresult else "",
                        }
                        if _stable_prefix:
                            _chapter_extras = (_chapter_extras + "\n" + _hblock).strip("\n")
                        else:
                            _hybrid_user_block = _hblock
                    except Exception as exc:  # noqa: BLE001 - 注入失败仍必须留下「无法判定」记录
                        logger.warning(
                            "章节 %s 混合生成注入失败（记为无法判定）", str(leaf.get("title") or ""), exc_info=True
                        )
                        if _hroute is not None:
                            _hybrid_by_leaf[str(leaf.get("id") or "")] = {
                                "route": _hroute,
                                "allowed": None,
                                "hits": 0,
                                "search_status": "error",
                                "search_message": f"混合生成注入失败：{exc}",
                            }

                if _stable_prefix:
                    messages, user_content = build_chapter_layered_messages(
                        sys_prompt=sys_prompt,
                        scheme=scheme,
                        project_brief=project_brief,
                        content_scope=_outline_construction_scope(scheme),
                        parent_chain=parent_chain,
                        parent_points=parent_points,
                        sibling_lines=sibling_lines,
                        section_number=_sec_no,
                        leaf=leaf,
                        word_budget_hint=_word_budget_hint(word_budget),
                        word_budget=word_budget,
                        prev_sibling_summary=prev_sibling_summary,
                        facts_text=facts_text,
                        eff_standard=eff_standard,
                        knowledge_text=knowledge_text,
                        chapter_extras=_chapter_extras,
                        chart_quota_hint=_chart_hint,
                        chapter_facts_text=_chapter_facts_text,
                        # ✅ R105（G5）：直接前驱叶子结尾片段（开关关闭/无前驱
                        #    → 空串，content_runtime 缺省形态输出逐字一致）
                        prev_leaf_summary=_prev_leaf_tail_for(leaf),
                    )
                else:
                    user_content = build_chapter_user_content(
                        scheme=scheme,
                        project_brief=project_brief,
                        content_scope=_outline_construction_scope(scheme),
                        parent_chain=parent_chain,
                        parent_points=parent_points,
                        sibling_lines=sibling_lines,
                        section_number=_sec_no,
                        leaf=leaf,
                        word_budget_hint=_word_budget_hint(word_budget),
                        word_budget=word_budget,
                        prev_sibling_summary=prev_sibling_summary,
                        facts_text=facts_text,
                        eff_standard=eff_standard,
                        knowledge_text=knowledge_text,
                        # ✅ R105（G5）：与分层分支同口径（两装配函数同形参）。
                        prev_leaf_summary=_prev_leaf_tail_for(leaf),
                    )
                    # ✅ R74（G3 修复）：stable-prefix 关闭时 _chapter_extras 不会
                    #    被任何分支消费，编排引导语此前在这条链路**静默丢弃**
                    #    （D1 开启但分层前缀关闭的组合下，正文模型永远看不到建议）。
                    #    现直接拼到 user_content 末尾，两种配置下引导语都必达。
                    if _plan_guidance:
                        user_content = user_content + "\n\n" + _plan_guidance
                    # ✅ R179 Phase F：分层前缀关闭时混合生成块同样直接拼到末尾，
                    #    保证两条装配路径都能看到条款上下文 / 经验段约束。
                    if _hybrid_user_block:
                        user_content = user_content + "\n\n" + _hybrid_user_block
                    messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_content}]
                _sec_facts = _filter_facts_rows(facts_rows, leaf)
                # ✅ R105（G3 · 逐章注入留痕）：按叶子记录「本章注入了哪些事实 /
                #    渲染多少字符 / 多少条因预算被截弃」。三段审计（core/chapter/
                #    plain）按分支至多命中一组，丢弃标题合并去重。只进闭包容器：
                #    终态汇总进 checkpoint 载荷，_persist_section 合并进报告
                #    fact_audit 键。留痕失败绝不阻断生成（fail-soft）。
                # ✅ R166（FIX-10 · 审计精确化）：injected_facts 改取三段审计的
                #    injected_titles 并集（渲染器逐条留痕的**实际下发集合**）——
                #    旧口径取 _filter_facts_rows 预筛结果，与「core 通用组 + rest
                #    逐章精选」的分段下发不同源（把未下发的行也记成已注入）。
                #    _sec_facts 保留原判据，供续写/扩写轮 fact_rows_for_section 使用。
                try:
                    _audit_group = tuple(_au for _au in (_core_audit, _chapter_audit, _plain_audit) if _au)
                    _drop_titles: list[str] = []
                    _injected_facts: list[str] = []
                    # ✅ 账实相符（2026-11-04）：单条 per_fact 硬切曾被视为
                    #    「完整注入」，现按渲染器逐行留痕的 truncated_facts
                    #    汇总，与 budget_dropped 分开记（截断 ≠ 丢弃）。
                    _truncated_facts: list[dict] = []
                    for _au in _audit_group:
                        for _t in _au.get("budget_dropped") or []:
                            if _t not in _drop_titles:
                                _drop_titles.append(_t)
                        for _t in _au.get("injected_titles") or []:
                            _t = str(_t or "")
                            if _t and _t not in _injected_facts:
                                _injected_facts.append(_t)
                        for _item in _au.get("truncated_facts") or []:
                            if not isinstance(_item, dict):
                                continue
                            _t = str(_item.get("title") or "")
                            if _t and any(str(_x.get("title") or "") == _t for _x in _truncated_facts):
                                continue
                            _truncated_facts.append(dict(_item))
                    if not _audit_group:
                        # 审计视图缺位（理论不可达：三段审计必有一段）→ 保留
                        # 旧预筛口径兜底，绝不因留痕改造丢掉 injected_facts 键。
                        _injected_facts = [str(r[1] or "") for r in (_sec_facts or []) if str(r[1] or "")]
                    _fact_audit_by_leaf[str(leaf.get("id") or "")] = {
                        "injected_facts": _injected_facts,
                        "rendered_chars": sum(int(_au.get("rendered_chars") or 0) for _au in _audit_group),
                        "budget_dropped": _drop_titles,
                        "budget_dropped_count": len(_drop_titles),
                        # 三态账本：完整注入 | 截断注入 | 丢弃
                        "truncated_facts": _truncated_facts,
                        "truncated_count": len(_truncated_facts),
                    }
                except Exception:  # noqa: BLE001 - 留痕绝不阻断正文生成
                    logger.warning("事实注入审计留痕失败（不影响生成）", exc_info=True)
                # ✅ 提示词治理（G5 预算 / G6 注入防护，默认关闭）：
                #    两个封装在配置未开启时**逐字返回原文**，故此处接入
                #    不改变任何既有行为；开启后才削减超长上下文 / 加资料围栏。
                #    必须在 messages 组装**之后**做 —— 治理对象是最终 user 文本。
                # ✅ R57：分层后改为对**每条** user 消息逐一治理 —— G5 本就按
                #    「【标签】：」独立切片预算，分段裁剪与整串裁剪同判据；
                #    开关关闭时只有一条 user，与旧实现逐字节一致。
                _user_idx = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "user"]
                for _mi in _user_idx:
                    _msg = messages[_mi]
                    _txt = _guard_external_material(_msg.get("content") or "")
                    _txt = _apply_prompt_context_budget(_txt)
                    messages[_mi] = {**_msg, "content": _txt}
                if _user_idx:
                    # user_content 供续写轮复用，必须与治理后的 messages 同步
                    user_content = "".join(str(messages[i].get("content") or "") for i in _user_idx)
                return eff_standard, _sec_facts, messages, user_content, _plan_json

            async def _generate_first_draft(
                messages: list[dict],
                title: str,
                stage_cb=None,
                max_tokens: int | None = None,
            ) -> tuple[str, Exception | None]:
                """子函数2：调用 AI 生成首轮正文（含失败重试+指数退避）。
                返回 (result, last_err)，last_err 为 None 表示成功。

                ✅ 进度增强：每次尝试前回调 stage_cb 上报「AI 生成中」阶段，
                AI 调用期间由 _await_with_stats 周期性推送运行统计。
                ✅ P1-2（2026-09-17）：max_tokens 按目标字数折算，给"跑飞"设物理上限。
                """
                result = ""
                last_err: Exception | None = None
                for attempt in range(CONTENT_SECTION_RETRIES + 1):
                    try:
                        if stage_cb is not None:
                            await stage_cb(
                                "draft",
                                f"（第 {attempt + 1}/{CONTENT_SECTION_RETRIES + 1} 次）"
                                if CONTENT_SECTION_RETRIES
                                else "",
                            )
                        result = await _await_with_stats(
                            asyncio.wait_for(
                                chat_with_fallback(
                                    messages,
                                    timeout=CONTENT_REQUEST_TIMEOUT,
                                    max_tokens=max_tokens,
                                    temperature=CONTENT_TEMPERATURE,
                                    scene="content_draft",
                                ),
                                timeout=CONTENT_TOTAL_TIMEOUT,
                            ),
                            _push_stats,
                        )
                        # ✅ 空结果按失败处理（兜底）：正常情况下空返回已在
                        #    chat_with_fallback/provider 层转为异常并降级重试，
                        #    这里防止未来新路径漏网 —— 空串一旦当成功 break，
                        #    章节会直接标失败且不享受重试。
                        if not (result or "").strip():
                            raise RuntimeError("所有候选 AI 提供商均返回空正文")
                        last_err = None
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        last_err = e
                        if attempt < CONTENT_SECTION_RETRIES:
                            err_s = str(e).lower()
                            base = (
                                CONTENT_RATE_LIMIT_BACKOFF
                                if ("429" in err_s or "rate" in err_s or "limit" in err_s)
                                else CONTENT_RETRY_BACKOFF
                            )
                            backoff = base * (2**attempt)
                            logger.warning(
                                "章节 %s 生成失败（第 %d/%d 次），%.0fs 后重试: %s",
                                title,
                                attempt + 1,
                                CONTENT_SECTION_RETRIES + 1,
                                backoff,
                                e,
                            )
                            await asyncio.sleep(backoff)
                return result, last_err

            async def _continue_if_needed(
                result: str,
                leaf: dict,
                user_content: str,
                title: str,
                min_passes: int = 0,
                stage_cb=None,
                eff_standard: str = "",
            ) -> tuple[str, bool]:
                """子函数3：字数不足时自动续写（含重试）。

                返回 (补充后的完整正文, cont_failed)：
                cont_failed = 续写过程中发生过**最终失败**（全部轮次都没产出可用内容）。
                调用方据此在 section_done 事件里带出 continue_failed，前端才能
                区分「本章字数达标」与「本章欠字数且补写失败」—— 两者 word_status
                都是 under，光看状态用户无从判断该不该手工补。

                min_passes：最少续写轮数（0=默认按字数门槛；continue 模式传 1，
                即使已达字数目标也强制补写一轮 —— 用户点「续写本章」的语义就是
                在现有正文基础上继续扩充）。

                eff_standard：生效生成标准。续写是**独立的 AI 调用**（system 换成
                `content_continue_system`），首轮 user 里的标准块在后续轮不再重发
                （token 优化，见 `_ctx_for_continue`），若不在此处补一行模式提醒，
                精准模式在续写轮就会退化成"凭记忆"、出现与全局事实不一致的数值
                —— 即「切换标准后策略未更新」的隐性缺口。
                """
                word_budget = leaf_word_budget(leaf)
                # ✅ 口径与落库一致：只数正文文字，不含内嵌图表代码块 ——
                #    否则一张几百字符的 mermaid 图会提前满足字数门槛而不续写。
                wc = text_word_count(result)
                # ✅ 修复：续写消息中的 user 消息不得为空串 —— 部分 provider
                #    （Anthropic 等）显式拒绝空 content，直接报 400 导致续写全失败。
                #    调用方（continue 模式）现已传入完整上下文，这里再兜一层最小上下文。
                _user_ctx = user_content.strip() or (
                    f"【方案名称】：{scheme.get('name','')}\n【方案类型】：{scheme.get('type','')}\n"
                    f"【当前章节】：{leaf.get('title','')} — {leaf.get('description','')}\n"
                )
                continue_count = 0
                cont_failed = False
                # ✅ R66（2026-10-11）：续写触发线取 max(0.8×预算, 300 字叶子下限)；
                #    下限被钉在**不超过该章预算本身**（min(300, budget)）：用户显式
                #    设了 <300 的小预算时，若下限仍取 300 会与续写总字数上限
                #    （1.1×预算）互相矛盾 —— 触发线永远够不着，白烧 max_rounds 轮。
                #    续写后总字数上限系数 = 1 + 容差（默认 0.1 → 1.1，与旧硬编码逐字一致）。
                try:
                    _cont_tol = abs(float(getattr(settings, "word_budget_tolerance", 0.1)))
                except (TypeError, ValueError):
                    _cont_tol = 0.1
                while should_continue_round(
                    wc=wc,
                    word_budget=word_budget,
                    continue_count=continue_count,
                    min_passes=min_passes,
                    max_rounds=CONTENT_CONTINUE_MAX_ROUNDS,
                    min_floor_chars=min(LEAF_MIN_WORD_BUDGET, int(word_budget or 0)),
                ):
                    await wait_resume(task_id)
                    if is_stopped(task_id):
                        break
                    # ✅ 进度增强：上报「续写扩充中」阶段（含轮次与当前字数），
                    #    让用户看到字数在增长，而不是只看到进度条不动。
                    if stage_cb is not None:
                        await stage_cb("continue", f"（第 {continue_count + 1} 轮，当前 {wc}/{word_budget} 字）")
                    cont_prompt = render(
                        "content_continue_system",
                        scheme_name=scheme.get("name", ""),
                        scheme_type=scheme.get("type", ""),
                        # ✅ 2026-10-02（检查点前置）：续写轮携带与首轮同源的
                        #    检查点硬约束（同一实现，避免「首轮有、续写无」分叉）；
                        #    只在非空时传入，关闭/异常时与旧模板逐字一致。
                        **({"content_checkpoint_block": _checkpoint_block} if _checkpoint_block else {}),
                        standards_text=get_standards_text(
                            scheme.get("name", ""), scheme.get("type", ""), section_title=leaf.get("title", "")
                        ),
                    )
                    cont_tail = _safe_tail(result, 2000)
                    # ✅ token 优化：首轮之后不再重发完整 _user_ctx（全局事实/知识库
                    # 每轮重复计费且稀释指令），只发章节定位 + 尾部前文。
                    _ctx_for_continue = (
                        _user_ctx
                        if continue_count == 0
                        else (
                            f"【方案名称】：{scheme.get('name','')}\n【方案类型】：{scheme.get('type','')}\n"
                            f"【当前章节】：{leaf.get('title','')} — {leaf.get('description','')}\n"
                        )
                    )
                    # ✅ 2026-09-28（T-1 收口）：续写轮 messages 构造下沉到
                    #    services/content_runtime.build_continuation_messages
                    #    （含模式提醒 + 续写指令，逐字搬迁）。
                    cont_messages = build_continuation_messages(
                        user_ctx=_ctx_for_continue,
                        cont_tail=cont_tail,
                        wc=wc,
                        word_budget=word_budget,
                        cont_prompt=cont_prompt,
                        eff_standard=eff_standard,
                        total_cap_factor=1.0 + _cont_tol,
                    )
                    cont = None
                    cont_last_err: Exception | None = None
                    # ✅ P1-2：续写输出上限按「剩余待补字数」折算（不再用配置的 32768）。
                    # ✅ 2026-09-28：上限折算下沉到 content_runtime.continue_max_tokens。
                    _cont_max_tokens = continue_max_tokens(word_budget=word_budget, wc=wc)
                    try:
                        # ✅ 口径统一（2026-09-16）：续写与首稿使用同一总超时。
                        #    旧实现此处写死 360s（对照首稿的 CONTENT_TOTAL_TIMEOUT
                        #    =660s）：降级链在续写路径被提前砍断 —— 同一模型链路下
                        #    "首稿能成、续写必超时"，用户看到"续写最终失败"且字数不达标。
                        cont = await _await_with_stats(
                            asyncio.wait_for(
                                chat_with_fallback(
                                    cont_messages,
                                    timeout=CONTENT_REQUEST_TIMEOUT,
                                    max_tokens=_cont_max_tokens,
                                    temperature=CONTENT_TEMPERATURE,
                                    scene="content_continue",
                                ),
                                timeout=CONTENT_TOTAL_TIMEOUT,
                            ),
                            _push_stats,
                        )
                        cont_last_err = None
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        cont_last_err = e
                    if cont is not None and cont.strip():
                        # ✅ 段落级去重：弱模型常把最后一段原样重写，直接拼接会产生重复段落
                        cleaned = _dedup_continuation(result, cont)
                        # R175 BUG-1：二次守卫与去重函数共用同一窗口常量，
                        # 避免「函数剥了 8000、守卫只认 3000」再次漏放重复。
                        if cleaned and cleaned not in result[-_DEDUP_TAIL_WINDOW:]:
                            result += "\n" + cleaned
                        else:
                            logger.info("章节 %s 续写内容与前文重复，已丢弃", title)
                        wc = text_word_count(result)
                        # ✅ 单章硬上限：续写最多把正文扩到目标的 2 倍，防止无限扩写
                        if wc >= word_budget * 2:
                            logger.info("章节 %s 达到单章字数硬上限（%d 字），停止续写", title, wc)
                            break
                    elif cont_last_err is not None:
                        # ✅ 记录续写最终失败（某一轮彻底失败）：与"续写成功但字数
                        #    仍不达标"区分开 —— 后者交给 word_status=under 表达。
                        cont_failed = True
                        logger.warning("章节 %s 续写最终失败: %s", title, cont_last_err)
                    continue_count += 1
                return result, cont_failed

            async def _persist_and_notify(
                leaf_id: str,
                result: str,
                leaf: dict,
                eff_standard: str = "",
                fact_rows_for_section=None,
                plan_json: str = "",
            ) -> tuple[str, int, int, str, dict]:
                """子函数4：持久化到 DB（含内联图表清洗），
                返回 (最终正文, word_count, word_budget, word_status, 生成标准校验报告)。"""
                wb = leaf_word_budget(leaf)
                content, wc, ws, report = await _persist_section(
                    leaf_id,
                    result,
                    wb,
                    eff_standard=eff_standard,
                    fact_rows_for_section=fact_rows_for_section,
                    section_title=str(leaf.get("title") or ""),
                    plan_json=plan_json,
                )
                # ✅ R66（2026-10-11）字数校验可追溯：每章落库后记一条结构化
                #    「目标|实际|偏差|偏差率|判定」日志，并累积进 _stat["audit_rows"]
                #    （终态 checkpoint / completed 事件的 word_audit 键即由它汇总，
                #    审核预检模块可读取做字数审计）。纯观测，不影响任何返回值；
                #    统计异常绝不允许打断生成主流程。
                try:
                    _dev = word_deviation_of(wc, wb, tolerance=_word_tol)
                    _stat.setdefault("audit_rows", []).append(
                        {
                            "section_id": leaf_id,
                            "title": str(leaf.get("title") or ""),
                            "level": int(leaf.get("level") or 0),
                            "word_budget": wb,
                            "word_count": wc,
                        }
                    )
                    logger.info(
                        "字数校验|节=%s|目标=%d字|实际=%d字|偏差=%+d字|偏差率=%.1f%%|" "判定=%s（%s容差带）",
                        str(leaf.get("title") or leaf_id)[:8],
                        wb,
                        wc,
                        _dev["deviation"],
                        _dev["ratio"] * 100,
                        ws,
                        "落在" if _dev["within_band"] else "超出",
                    )
                except Exception:
                    logger.debug("字数审计记录失败（忽略）: %s", leaf_id, exc_info=True)
                return content, wc, wb, ws, report

            async def _auto_shrink_if_over(result: str, leaf: dict, stage_cb=None) -> str:
                """超字数自动压缩（可选）：仅当章节判定为 over 时才跑 AI 压缩。

                与手动端点共用 `shrink_content_rounds`（同一提示词条目、同一保护
                区间校验、同一轮次/收敛参数），避免"自动压缩"与"手动压缩"两套行为。
                任何失败都保留原正文（宁可超字数，不可损坏正文），只记日志。
                """
                wb = leaf_word_budget(leaf)
                # ✅ 门槛判定与 word_status='over' 同口径（module 级纯函数，可单测）
                if not _should_auto_shrink(result, wb):
                    return result

                # 进度上报沿用 persist 阶段的标签（不新增阶段：_STAGE_MODEL 的
                # base/span 是成型进度模型，加阶段需重新配平并影响既有刻度）
                async def _on_round(round_no: int, cur_wc: int):
                    if stage_cb is not None:
                        await stage_cb("persist", f"（超字数压缩第 {round_no} 轮，当前 {cur_wc}/{wb} 字）")

                def _prompt_factory(round_no: int, cur_wc: int) -> str:
                    return render(
                        "content_shrink_system",
                        scheme_name=scheme.get("name", ""),
                        scheme_type=scheme.get("type", ""),
                        section_title=leaf.get("title", ""),
                        current_words=cur_wc,
                        target_words=wb,
                        round_no=round_no,
                        max_rounds=SHRINK_MAX_ROUNDS,
                    )

                async def _call_ai(sys_prompt: str, user_payload: str) -> str:
                    # ✅ 修复（2026-09-17）：压缩调用必须显式传 max_tokens，否则
                    #    _attempt_candidate 会回退到配置值（可能 32768），与首稿/续写
                    #    已按 max_tokens_for_budget 折算的口径不一致，也违反「max_tokens
                    #    不要留空」的设计约束。压缩产出是短 JSON，按目标字数折算即可。
                    _shrink_max_tokens = max_tokens_for_budget(wb)
                    return await _await_with_stats(
                        asyncio.wait_for(
                            chat_with_fallback(
                                [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_payload}],
                                timeout=CONTENT_REQUEST_TIMEOUT,
                                max_tokens=_shrink_max_tokens,
                                temperature=CONTENT_TEMPERATURE,
                                scene="content_shrink",
                            ),
                            timeout=CONTENT_TOTAL_TIMEOUT,
                        ),
                        _push_stats,
                    )

                try:
                    shrink = await shrink_content_rounds(
                        result, word_budget=wb, prompt_factory=_prompt_factory, call_ai=_call_ai, on_round=_on_round
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("章节 %s 自动压缩失败（保留原正文）: %s", leaf.get("title", ""), e)
                    return result
                if shrink["applied"]:
                    logger.info(
                        "章节 %s 自动压缩：%d → %d 字（%d 轮，%s）",
                        leaf.get("title", ""),
                        shrink["before"],
                        shrink["word_count"],
                        shrink["rounds_used"],
                        shrink["stop_reason"],
                    )
                    return shrink["content"]
                logger.info("章节 %s 自动压缩未生效（%s），保留原正文", leaf.get("title", ""), shrink["stop_reason"])
                return result

            async def gen_one(idx: int, leaf: dict):
                """生成单个章节：开始 → 构建上下文 → AI生成 → 续写 → 持久化 → 通知
                （编排器，具体逻辑拆分为 _build_generation_context / _generate_first_draft /
                  _continue_if_needed / _persist_and_notify 四个子函数）

                ✅ 进度增强：章节开始即登记为「进行中」，并在各阶段（构建上下文 /
                AI 生成 / 续写 / 落库）推送 section_stage 事件与加权进度。"""
                # ✅ R77b：_fa_sum 在闭包外层（generate_content）初始化，而本函数
                #    的累加是**名字重绑定**（_fa_sum = accumulate(...)，返回新 dict）
                #    —— 与 _std_sum 的「下标赋值变异」不同，Python 编译期会把重绑定
                #    名判为 gen_one 局部变量，不声明 nonlocal 即 UnboundLocalError
                #    （R77b 全量 e2e 实测：section_done 事件下发失败、章节落库成功
                #    但前端收不到完成事件）。_std_sum/_stat/_prog 均为变异型访问
                #    （下标/方法），不受此规则影响，勿照抄加 nonlocal。
                nonlocal _fa_sum
                leaf_id = leaf["id"]
                title = leaf["title"]
                db_written = False

                # F-CONTENT-STANDARD(2026-09-26 · B2): 本章生效标准**在事件之前**解析一次
                # （章节级 → 方案级 → 任务级强覆盖 → 默认 precise），
                # 使 section_start 就能携带该字段；后续上下文构建/提示词注入/
                # 校验报告全部复用这一个值，杜绝「事件与实际生效值不一致」。
                _eff_std = resolve_effective_standard(
                    task_standard=task_standard,
                    override=override_section_standard,
                    section_standard=leaf.get("generation_standard", "") or "",
                    scheme_standard=scheme.get("generation_standard", "") or "",
                )

                # ✅ 登记为进行中章节（心跳统计与阶段事件的数据源）
                _prog["running"][leaf_id] = {
                    "title": title,
                    "index": idx + 1,
                    "stage": "context",
                    "stage_label": _STAGE_LABELS["context"],
                    "started_at": time.monotonic(),
                    "stage_started_at": time.monotonic(),
                }

                async def _stage_cb(stage: str, detail: str = ""):
                    await _on_stage(leaf_id, stage, detail)

                yield (
                    json.dumps(
                        {
                            "event": "section_start",
                            "section_id": leaf_id,
                            "title": title,
                            "index": idx + 1,
                            "total": total,
                            "stage": "context",
                            "stage_label": _STAGE_LABELS["context"],
                            "progress": _progress_now(),
                            # F-CONTENT-STANDARD(2026-09-26 · B2): 本章实际生效的生成标准
                            "generation_standard": _eff_std,
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                await _on_stage(leaf_id, "context")

                try:
                    await wait_resume(task_id)
                    if is_stopped(task_id):
                        # ✅ 停止不是失败（2026-09-16）：必须抛 CancelledError 交由
                        #    上层停止分支收尾，**不得** yield section_error。
                        #    旧实现 yield 后 return，前端把被停止打断的章节记成
                        #    「失败」日志，与 stopped 终态自相矛盾（用户明明点了
                        #    停止，日志区却一片红）。
                        raise asyncio.CancelledError()

                    if mode == "continue":
                        # ✅ 续写模式（对齐 OpenBidKit 任意点续写）：以已有正文为底稿，
                        #    跳过首轮生成，直接进入续写（至少补写 1 轮，即使已达字数目标）。
                        #    select_target_leaves 的 continue 分支已保证仅有正文章节入选。
                        result = (leaf.get("content") or "").strip()
                        if not result:
                            # ✅ B2（2026-09-23）：continue 模式下空正文**必须登记
                            #    失败**。旧实现只发 section_error 就 return，失败
                            #    章不进 stats.failed / 终态 failed_count ——
                            #    前端 stats 显示"0 失败"、终态汇总也对不上，
                            #    用户看不出这一章其实没写成。
                            _reason = "该章节暂无正文，无法续写（请先生成）"
                            yield (
                                json.dumps(
                                    {
                                        "event": "section_error",
                                        "section_id": leaf_id,
                                        "title": title,
                                        "reason": _reason,
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n\n"
                            )
                            await _mark_section_failed(leaf_id, title, _reason)
                            return
                        # ✅ 修复：续写同样需要完整生成上下文（方案信息/上级要点/同级边界/
                        #    逐章精选事实/标准清单/目标字数）。旧实现传空 user_content，
                        #    续写消息里出现空 user 消息（部分 provider 直接 400 报错），
                        #    且续写缺少事实与同级信息，易与全文口径不一致。
                        # ✅ R167（BUG 修复）：续写分支同样绑定 _plan_json ——
                        #    落库点 plan_json=_plan_json 为两种模式共用，该名字在
                        #    gen_one 内有赋值即为局部变量，continue 路径不绑定就在
                        #    落库点抛 UnboundLocalError（自 R70 透传引入起续写本章
                        #    100% 失败）。_plan_enabled=False ⇒ 恒为空串，
                        #    CASE WHEN 保留原列语义不变。
                        _eff_std_c, _sec_facts, _, user_content, _plan_json = await _build_generation_context(
                            leaf, _eff_std, _plan_enabled=False
                        )
                        # ✅ D-39（R176）：逐章知识库硬切事件随上下文构建立即下发
                        #    （空清单零事件；只在本章真裁剪时出现）。
                        for _cd_event in _flush_context_degraded():
                            yield _cd_event
                        result, cont_failed = await _continue_if_needed(
                            result, leaf, user_content, title, min_passes=1, stage_cb=_stage_cb, eff_standard=_eff_std_c
                        )
                    else:
                        # 子函数1：构建上下文
                        _eff_std2, _sec_facts, messages, user_content, _plan_json = await _build_generation_context(
                            leaf, _eff_std, _plan_enabled=True
                        )
                        # ✅ D-39（R176）：逐章知识库硬切事件随上下文构建立即下发
                        #    （空清单零事件；只在本章真裁剪时出现）。
                        for _cd_event in _flush_context_degraded():
                            yield _cd_event

                        # ✅ P1-2（2026-09-17）：按本章目标字数折算输出上限，
                        #    避免落到配置值（实测 32768）导致模型无上限扩写。
                        _draft_max_tokens = max_tokens_for_budget(leaf_word_budget(leaf))
                        # 子函数2：AI 首轮生成（含重试）
                        result, last_err = await _generate_first_draft(
                            messages, title, stage_cb=_stage_cb, max_tokens=_draft_max_tokens
                        )

                        if last_err is not None:
                            logger.error(
                                "章节生成最终失败（section_id=%s · title=%s · scheme_id=%s · task=%s）: %s",
                                leaf_id,
                                title,
                                scheme_id,
                                task_id,
                                last_err,
                            )
                            _reason = (
                                "章节生成超时，请重试"
                                if isinstance(last_err, asyncio.TimeoutError)
                                else str(last_err)[:200]
                            )
                            yield (
                                json.dumps(
                                    {
                                        "event": "section_error",
                                        "section_id": leaf_id,
                                        "title": title,
                                        "reason": _reason,
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n\n"
                            )
                            # ✅ BUG 修复（force_rewrite 永久卡死）：旧实现无条件标 failed。
                            # 强制重写时该章节本有旧正文，失败后状态被打成 failed，
                            # 且 missing 模式要求 content 为空才重选（旧文还在），
                            # 普通模式又跳过非空章节 —— 该章从此无法被任何生成模式选中，
                            # 只能手改。旧文仍在时保持其原状态（generated/reviewed…）。
                            # ✅ 二次修复（2026-09-16）：失败**必须计入统计**（mark_db=False
                            # 只跳过状态标记），否则运行中 stats.failed 与终态 failed_count
                            # 不一致 —— 前端进度卡显示 0 失败、完成提示却说"N 章失败"。
                            # ✅ P1 修复（2026-10-04 · DB 写失败静默 · 语义澄清）：
                            #    此处 db_written 恒为 False（持久化尚未发生），
                            #    与旧实现的 `not leaf.get("content")` 语义等价
                            #    （因为 leaf["content"] 在整个 gen_one 内**从不被就地更新**，
                            #    全仓 grep 确认无 `leaf["content"] = ...` 赋值）。
                            #    此处显式用 db_written 表意"落库是否已成功"——将来
                            #    若 gen_one 内部有中途写库路径，无需再改本处。
                            await _mark_section_failed(leaf_id, title, _reason, mark_db=not db_written)
                            db_written = True
                            return

                        if not (result or "").strip():
                            logger.warning("章节 %s 返回空内容，标记为失败", title)
                            _reason = "AI 返回空内容，请重试"
                            yield (
                                json.dumps(
                                    {
                                        "event": "section_error",
                                        "section_id": leaf_id,
                                        "title": title,
                                        "reason": _reason,
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n\n"
                            )
                            # 同上（db_written 恒为 False，语义 = 落库未发生）
                            await _mark_section_failed(leaf_id, title, _reason, mark_db=not db_written)
                            db_written = True
                            return

                        # 子函数3：字数不足续写
                        result, cont_failed = await _continue_if_needed(
                            result, leaf, user_content, title, stage_cb=_stage_cb, eff_standard=_eff_std2
                        )

                    # 子函数4：持久化
                    await _stage_cb("persist")
                    # ✅ 超字数自动压缩（可选，默认关）：在**落库前**压缩 ——
                    #    正文只写一次（清洗/图表登记/字数统计一次成型），避免
                    #    "先落库再改写" 产生两倍写库与中间态。
                    if auto_shrink_over:
                        result = await _auto_shrink_if_over(result, leaf, _stage_cb)

                    # ✅ R179 Phase F（2026-11-06 · 生成后置校验）：
                    #    规范驱动 → 数值锁机械比对（集合外数值按句拦截，拒绝写出）；
                    #    经验驱动 → 空话黑名单命中即记缺陷。校验只读、不改路由与落库
                    #    契约；失败 fail-soft 保留原文。报告仅随终态载荷回传（开关关闭
                    #    时完全不产生 hybrid_report，载荷与引入前逐字一致）。
                    _hybrid_report = None
                    if bool(getattr(settings, "hybrid_generation_enabled", False)):
                        _hstate = _hybrid_by_leaf.get(str(leaf_id or ""))
                        if _hstate:
                            _hroute = _hstate["route"]
                            try:
                                if _hroute.strategy == "rag":
                                    _hlock = None
                                    _hallowed = _hstate["allowed"]
                                    if _hstate["search_status"] != "ok" or _hallowed is None or not _hallowed.tokens:
                                        # 条款库不可用 / 无可比对数值 → 无法判定（不得冒充通过）
                                        _hstatus = "undetermined"
                                    else:
                                        result, _hlock = enforce_numeric_lock(result, _hallowed)
                                        _hstatus = _hlock.status
                                    _hybrid_report = {
                                        "strategy": "rag",
                                        "content_type": _hroute.content_type,
                                        "search_message": _hstate["search_message"],
                                        "hits": _hstate["hits"],
                                        "numeric_lock_status": _hstatus,
                                        "numeric_lock": _hlock.as_dict() if _hlock else None,
                                    }
                                    # 三态原因码 + 「本节未经规范校验」由同一处生成，
                                    # 避免报告与提示词措辞分叉。
                                    _search_result = _hstate.get("result")
                                    if _search_result is not None:
                                        _hybrid_report.update(
                                            search_report_fields(_search_result, budget=_hybrid_search_budget)
                                        )
                                    else:
                                        _hskip = search_skip_note(_hstate["search_status"])
                                        _hybrid_report.update(
                                            {
                                                "search_status": _hstate["search_status"],
                                                "search_reason_code": _hstate["search_status"],
                                                "standard_candidates": [],
                                                "query_terms": [],
                                                "skipped_checks": _hskip,
                                                "section_unverified": True,
                                                "section_unverified_note": "本节未经规范校验：" + _hskip,
                                                "budget_exceeded": False,
                                                "budget": (
                                                    _hybrid_search_budget.as_dict() if _hybrid_search_budget else None
                                                ),
                                            }
                                        )
                                else:
                                    _hdefects = verify_experience_text(result, _hroute, blacklist_from_settings())
                                    _hybrid_report = {
                                        "strategy": "llm",
                                        "content_type": _hroute.content_type,
                                        "fact_status": _hroute.fact_status,
                                        "missing_fact_deps": list(_hroute.missing_deps),
                                        "defect_count": len(_hdefects),
                                        "defects": _hdefects,
                                    }
                                    if _hdefects:
                                        logger.warning("章节 %s 经验段命中空话/缺事实引用 %d 处", title, len(_hdefects))
                            except Exception as exc:  # noqa: BLE001 - 校验失败保留原文，但仍须留下可见记录
                                logger.warning(
                                    "章节 %s 混合生成后置校验失败（保留原文并记为无法判定）", title, exc_info=True
                                )
                                _hybrid_report = {
                                    "strategy": _hroute.strategy,
                                    "content_type": _hroute.content_type,
                                    "search_status": "error",
                                    "search_reason_code": "error",
                                    "search_message": f"混合生成后置校验失败：{exc}",
                                    "hits": _hstate["hits"],
                                    "standard_candidates": [],
                                    "query_terms": [],
                                    "skipped_checks": search_skip_note("error"),
                                    "section_unverified": True,
                                    "section_unverified_note": "本节未经规范校验：" + search_skip_note("error"),
                                    "budget_exceeded": False,
                                    "budget": _hybrid_search_budget.as_dict() if _hybrid_search_budget else None,
                                    "numeric_lock_status": "undetermined" if _hroute.strategy == "rag" else None,
                                    "numeric_lock": None,
                                }
                    content, wc, wb, ws, _std_report = await _persist_and_notify(
                        leaf_id,
                        result,
                        leaf,
                        eff_standard=_eff_std,
                        fact_rows_for_section=_sec_facts,
                        plan_json=_plan_json,
                    )
                    db_written = True

                    # ✅ P1 修复（2026-10-04 · DB 写失败静默）：落库已提交成功后，
                    #    本节后续步骤（章节质量扫描、终态汇总累加、section_done 组事件
                    #    与 yield）**任何异常都不允许再冒到 guarded_gen**——否则上层会
                    #    把该章节**再标一次 status='failed'**，把已经成功的库内正文
                    #    语义上"抹掉"（正文仍在，但 status/计数与终态汇总全部对不上）。
                    #    旧实现里 json.dumps / yield 一旦抛异常（客户端提前断开时
                    #    event_queue.put 可能 CancelledError，或 content 含非 JSON 字符
                    #    时 json 抛错），异常会直接穿过 gen_one 落到 guarded_gen，
                    #    那里把该章节按 "leaf 原状态" 记为失败，与库内真相冲突。
                    #    这里对非取消类异常只记日志、不再外抛；CancelledError 保持
                    #    向上冒泡交由外层 except CancelledError 分支按停止语义处理。
                    #    前端本次会话看不到该章节的 section_done 事件，但**下次打开**
                    #    方案详情即可看到该章节正文——即"库内真相优先"。
                    try:
                        # ✅ 修复（2026-09-17）：自动生成章节的质量审计结果（口语化/废止标准）
                        #    此前只在 _persist_section 里记一条日志，不随 section_done 下发，
                        #    前端质量面板对自动生成章节看不到任何告警。现随事件携带，与手工保存
                        #    （sections.py）口径一致，生成过程中质量面板即可实时更新。
                        try:
                            _section_issues = quality_issues(content)
                        except Exception:
                            _section_issues = {"colloquial_hits": [], "abolished_standards": []}

                        # F-CONTENT-STANDARD(2026-09-26 · B2): 复用落库时**同一份**校验报告，
                        # 不在本处重复扫描 —— 旧实现对同一章正文跑了两遍正则（一次落库、
                        # 一次组事件），高并发档白烧 CPU。落库阶段已做 try/except 降级，
                        # 校验器异常绝不阻断生成，事件侧只需兜一个空形态。
                        _std_rep = _std_report or {
                            "standard": _eff_std or PRECISE,
                            "passed": True,
                            "error_count": 0,
                            "warning_count": 0,
                            "issues": [],
                        }

                        # F-CONTENT-STANDARD(2026-09-26 · B2): 逐章累加终态汇总。
                        # 只统计"本次落库成功"的章节（失败章无正文可校验，不计入）。
                        _std_sum[_eff_std] = _std_sum.get(_eff_std, 0) + 1
                        _se = int(_std_rep.get("error_count") or 0)
                        _sw = int(_std_rep.get("warning_count") or 0)
                        if _se or _sw:
                            _std_sum["issue_sections"] += 1
                        _std_sum["errors"] += _se
                        _std_sum["warnings"] += _sw
                        _std_sum["total_issues"] += _se + _sw
                        # ✅ R77b：事实冲突定点修复累加（纯函数单一计数口径；
                        #    _std_rep 是落库后同一份报告，fact_autofix 键随附）。
                        _fa_sum = accumulate_fact_autofix(_fa_sum, _std_rep)

                        # ✅ 续写失败信号收口：判定统一走纯函数
                        #    _continue_failed_flag（与 WORD_UNDER_RATIO 同口径）——
                        #    「续写失败但字数已达标」不报，「未达标且补写失败」才报。
                        word_budget = wb
                        cont_failed = _continue_failed_flag(cont_failed, wc, word_budget)
                        # ✅ R66（2026-10-11）：偏差随事件下发（可追溯 + 前端实时展示）
                        _wdev_done = word_deviation_of(wc, wb, tolerance=_word_tol)
                        _done_payload = {
                            "event": "section_done",
                            "section_id": leaf_id,
                            "title": title,
                            "word_count": wc,
                            "word_budget": wb,
                            "word_status": ws,
                            # ✅ BUG-4：续写失败信号随事件下发，前端日志区据此标注
                            #    "补写失败"，用户不必自己比对字数去猜。
                            "continue_failed": cont_failed,
                            # ✅ R66：目标/实际偏差、偏差率、是否落在容差带
                            "word_deviation": _wdev_done["deviation"],
                            "word_ratio": _wdev_done["ratio"],
                            "word_within_band": _wdev_done["within_band"],
                            # ✅ 增强：携带最终正文，前端可即时刷新"当前选中章节"预览，
                            #    无需等整批生成结束后的全量 load()（旧实现预览长时间停留在旧内容）
                            "content": content,
                            "quality_issues": _section_issues,
                            # ✅ R49（图表产出闭环）：配图产出/删除明细随事件**实时**下发。
                            #    此前只有「生成结束翻报告」一条路，用户在日志区只能看到
                            #    「少了几张图却毫无理由」。三键与 last_generation_report
                            #    同名同值（单一来源 _std_rep），故断线重挂后与库内真相一致：
                            #    chart_count = 最终正文里存活的图表块数；
                            #    charts_dropped_count / charts_dropped = 被删的块及其理由。
                            "chart_count": int(_std_rep.get("chart_count") or 0),
                            "charts_dropped_count": int(_std_rep.get("charts_dropped_count") or 0),
                            "charts_dropped": _std_rep.get("charts_dropped") or [],
                            # F-CONTENT-STANDARD(2026-09-26 · B2): 本章生效标准 + 完整校验报告
                            # （含 issues 明细供前端 Popover 展示；扁平计数保留便于告警 Tag）
                            "generation_standard": _eff_std or "",
                            "standard_report": _std_rep,
                            "standard_passed": _std_rep.get("passed", True),
                            "standard_error_count": _std_rep.get("error_count", 0),
                            "standard_warning_count": _std_rep.get("warning_count", 0),
                        }
                        # ✅ R179 Phase F：混合生成报告仅在开关打开且本章有记录时下发
                        #    （关闭时不新增任何键，终态载荷与引入前逐字一致）。
                        if _hybrid_report is not None:
                            _done_payload["hybrid_report"] = _hybrid_report
                        # ✅ R181：体积闸门（默认关闭=0，与引入前一致；配置正数后
                        #    超出时显式降级，绝不静默截断）
                        _done_payload = _degrade_section_done_payload(
                            _done_payload,
                            int(getattr(settings, "section_done_payload_max_chars", 0) or 0),
                        )
                        yield (
                            json.dumps(
                                _done_payload,
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                    except asyncio.CancelledError:
                        # 用户停止 / 客户端断开 / 生成器 aclose —— 交由下方外层
                        # except CancelledError 按"停止语义"处理（db_written=True
                        # 时不会重标 failed，保持库内真相）。
                        raise
                    except Exception as _post_e:
                        # 落库已提交，section_done 事件推送失败（客户端提前断开 /
                        # JSON 序列化失败 / event_queue 满等）—— **只记日志不重抛**，
                        # 避免上层把该章节误标为 failed。
                        logger.warning(
                            "章节 %s 落库已成功但事件下发失败（忽略，库内状态保持）: %s", title, _post_e, exc_info=True
                        )

                except asyncio.CancelledError:
                    # ✅ 语义修正：用户主动停止≠章节生成失败。停止时未完成章节保持
                    # pending（不标 failed、不发 section_error），否则质量面板把用户
                    # 停止计入失败、missing 模式还会把用户故意跳过的章重新选中。
                    if not db_written and not is_stopped(task_id):
                        yield (
                            json.dumps(
                                {
                                    "event": "section_error",
                                    "section_id": leaf_id,
                                    "title": title,
                                    "reason": "任务已取消",
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                        try:
                            await _mark_section_failed(
                                leaf_id,
                                title,
                                "任务已取消",
                                # leaf["content"] 在 gen_one 内从不被就地更新，
                                # 因此这里读到的值等价于"进入 gen_one 之前的
                                # 库内正文"：原章有正文 → mark_db=False（保留
                                # 旧正文与旧状态），空章 → mark_db=True（打成 failed）。
                                mark_db=not (leaf.get("content") or "").strip(),
                            )
                        except Exception:
                            logger.debug("章节 %s 取消后失败标记写入失败（不影响取消流程）", leaf_id, exc_info=True)
                    raise

            # 统一事件队列：所有事件（章节级别 + 总进度 + 终态）都 push 到这里
            # ✅ BUG-3（2026-09-19 · 有界队列 + 背压）：event_queue 必须**有界**。
            #    旧实现用无界队列：客户端断开后生成器被 cancel，消费者消失，
            #    生产者（章节任务）仍无界往里塞 → 内存单调增长直到进程被 OOM kill
            #    （长方案 + 弱客户端场景必现）。maxsize=1000 + 全链路阻塞式
            #    `await put` 形成背压：队列满时生产者自然等，被 cancel 时等待被打断。
            event_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
            DONE_SENTINEL = object()

            async def guarded_gen(idx: int, leaf: dict):
                """包装 gen_one，把 SSE 事件逐行塞入 queue"""
                # ✅ 暂停闸门前置到信号量**之前**（2026-09-17）：
                #    旧实现的顺序是「先抢 semaphore → gen_one 内才 await wait_resume」，
                #    暂停时已在信号量内的章节会**一直占住全局并发许可**（实测暂停期间
                #    semaphore 剩余许可恒为 0）——同一进程内其它方案的生成任务被饿死，
                #    且用户点暂停后这批许可永不释放。闸门前置后暂停不占用任何并发额度。
                await wait_resume(task_id)
                if is_stopped(task_id):
                    # 保持与本文件其它检查点一致的协作式停止语义（不记为章节失败）
                    raise asyncio.CancelledError()
                # ✅ 每任务独立信号量：容量严格 = 用户档位（不随全局自适应漂移）；
                #    协程按 leaves 目录序创建，asyncio.Semaphore 对等待者 FIFO 放行
                #    → 启动顺序即目录树层级顺序，递进式生成
                async with _run_semaphore:
                    # ✅ R105（G1 · 顺序留痕）：过闸即记实际派发序。
                    #    口径 = 启动序（Semaphore FIFO 放行序 = 协程创建序 = 目录 DFS 序），
                    #    与 as_completed 的**完成**序无关；终态拼装点
                    #    _content_ckpt_payload 拿它与 _expected_order 逐位比对。
                    _dispatched_order.append(str(leaf.get("id") or ""))
                    try:
                        async for sse_line in gen_one(idx, leaf):
                            if not sse_line.startswith("data: "):
                                sse_line = f"data: {sse_line}"
                            await event_queue.put(sse_line)
                    except asyncio.CancelledError:
                        # gen_one 被取消 → 已经发过 section_error，这里直接传播
                        raise
                    except Exception as e:
                        # 其他未预期异常 → 发 section_error
                        leaf_id = leaf["id"]
                        title = leaf["title"]
                        # ✅ 可观测性修复（2026-09-29 · 「38/38 章全失败」零堆栈）：
                        #   本分支原先只发 SSE 事件、**不打任何日志**，而 gen_one 只
                        #   捕获 CancelledError —— 于是所有「非预期异常」全部静默
                        #   变成一条 section_error 事件。线上表现为终态 failed、
                        #   失败明细只有一行 `生成异常: <msg>`，日志里**完全没有
                        #   堆栈**（本次真实故障：'tuple' object has no attribute
                        #   'split'，38 章同因，却无从定位到行）。
                        #   修复：按 ERROR 级别 + exc_info 落盘（业务阻断级故障），
                        #   并带上章节标题与 id，便于按 trace/章节检索。
                        #   ⚠️ 注释块须整段位于 `_reason` **之前**：护栏
                        #   test_content_fence_contract 的 800 字符窗口以
                        #   `_reason = f"生成异常:` 为锚，若注释夹在锚点与
                        #   logger.error 之间会把 `exc_info=True` 挤出窗口。
                        _reason = f"生成异常: {str(e)[:150]}"
                        logger.error(
                            "章节生成未预期异常（section_id=%s · title=%s · scheme_id=%s · task=%s）: %s",
                            leaf_id,
                            title,
                            scheme_id,
                            task_id,
                            e,
                            exc_info=True,
                        )
                        try:
                            await event_queue.put(
                                json.dumps(
                                    {
                                        "event": "section_error",
                                        "section_id": leaf_id,
                                        "title": title,
                                        "reason": _reason,
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n\n"
                            )
                            # ✅ BUG 修复（与 gen_one 的失败分支口径统一）：
                            #    章节本有旧正文时**不得**把状态打成 failed（否则该章
                            #    从此无法被任何生成模式选中），但必须计入失败统计，
                            #    否则 stats.failed 与终态 failed_count 不一致。
                            # ✅ P1 修复（2026-10-04 · DB 写失败静默 · 语义澄清）：
                            #    这里读 leaf["content"] 是「章节进入 gen_one 之前
                            #    的库内状态」——leaf["content"] 在 gen_one 内**从不**
                            #    被就地更新（全仓 grep 确认无 `leaf["content"] = ...`
                            #    赋值），所以本处读取的值与"进入 gen_one 时"完全一致。
                            #    mark_db=False 仅当"章节原本已有正文"时启用：
                            #      · 强制重写 / 续写场景：失败后保留旧正文与旧状态
                            #        （否则该章从此无法被任何生成模式选中，见下方注释）；
                            #      · 首次生成 / 空章：mark_db=True → 状态打成 failed。
                            #    gen_one 内部的"落库成功后任何异常"已用 try/except
                            #    就地吞掉（只记日志），保证 DB 已提交的成功不会被
                            #    本层重标为 failed；因此能落到本层的异常**必然**
                            #    是"持久化未成功"路径。
                            await _mark_section_failed(
                                leaf_id, title, _reason, mark_db=not (leaf.get("content") or "").strip()
                            )
                        except Exception:
                            logger.warning(
                                "章节 %s 失败标记/错误事件写入失败" "（失败计数与库内状态可能不一致）",
                                leaf_id,
                                exc_info=True,
                            )
                    finally:
                        # ✅ 进度增强：章节结束（成功 / 失败 / 取消）后移出「进行中」
                        #    集合，否则被取消或异常终止的章节会永久留在统计里。
                        _rec = _prog["running"].pop(leaf["id"], None)
                        if _rec:
                            # ✅ 记录实测单章耗时：用于「平均单章耗时」展示，
                            #    也是阶段耗时校准的旁证（ETA 与"还要多久"判断依据）。
                            _started = _rec.get("started_at")
                            if _started:
                                _prog.setdefault("section_ms", []).append(max(0.0, time.monotonic() - _started))

            tasks = [asyncio.create_task(guarded_gen(i, leaf)) for i, leaf in enumerate(leaves)]
            for t in tasks:
                register_child_task(task_id, t)

            completed_count = 0

            async def run_all_generations():
                """等待所有章节生成完成，期间不断推送总进度"""
                nonlocal completed_count
                try:
                    for coro in asyncio.as_completed(tasks):
                        try:
                            await coro
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.error(
                                "章节生成子任务异常（scheme_id=%s · task=%s）: %s", scheme_id, task_id, e, exc_info=True
                            )
                        completed_count += 1
                        # ✅ 进度增强：更新已处理数 → 折算加权进度，并附 ETA /
                        #    累计字数 / 失败数等统计，前端无需再自行推算。
                        # 内部口径：已处理（含失败），供进度折算与 ETA 使用
                        _prog["done"] = completed_count
                        p = _progress_now()
                        snap = _snapshot_stats(_prog)
                        # R175 BUG-4：对外 done 恒为成功数，processed 表达已处理总数；
                        # 与终态 checkpoint / 重挂摘要保持同一口径。
                        done_n = snap.get("done", completed_count)
                        processed_n = snap.get("processed", completed_count)
                        failed_n = snap.get("failed", 0)
                        msg = f"已完成 {done_n}/{total}"
                        if snap.get("eta_ms"):
                            msg += f"，预计剩余 {_fmt_duration(snap['eta_ms'])}"
                        # ✅ 失败章节显式提示：失败数与 ETA 同级重要（用户据此决定
                        #    是否等结束再重试），不再只在终态汇总里出现。
                        if failed_n:
                            msg += f"（{failed_n} 章失败）"
                        await _update_progress_safe(p, msg)
                        await event_queue.put(f"data: {json.dumps({
                            'event': 'progress', 'task_id': task_id,
                            'progress': p, 'message': msg,
                            'done': done_n, 'processed': processed_n, 'total': total,
                            'failed': failed_n,
                            'words': _prog.get('words', 0),
                            'elapsed_ms': snap.get('elapsed_ms'),
                            'eta_ms': snap.get('eta_ms'),
                            'stats': snap,
                        }, ensure_ascii=False)}\n\n")
                except asyncio.CancelledError:
                    logger.info("正文生成被取消")
                finally:
                    # ✅ 哨兵同样阻塞投递：队列满时 await 形成背压，
                    #    消费侧 break 后不再 get，靠取消传播解开等待。
                    await event_queue.put(DONE_SENTINEL)

            gen_runner = asyncio.create_task(run_all_generations())

            # ------ event_stream 主循环：统一从 queue 读并 yield ------
            # ✅ 修复竞态：只靠 DONE_SENTINEL break，去掉 queue.empty() + gen_runner.done() 提前退出
            while True:
                line = await event_queue.get()
                if line is DONE_SENTINEL:
                    break
                yield line

            # 等待生成任务完全收尾
            await gen_runner

            # ✅ 用户主动停止：未完成章节保持 pending（不批量标 failed）。
            # 停止不是失败，标 failed 会污染质量统计并使 missing 模式误选这些章。
            stopped = is_stopped(task_id)
            if stopped:
                # ✅ BUG 修复（2026-09-16）：停止时把进度硬推到 1.0 会让人误以为
                #    "已完成"——前端进度条跳到 100%、断线重连后任务列表/活动中心
                #    读到的也是 100%，与实际"只生成了一半"完全相反。这里只上报
                #    **真实进度**（过不可回退护栏），与目录生成的停止路径口径一致。
                stop_progress = _progress_now()
                # ✅ 成果清单落库（断线/刷新后可恢复"生成了哪几章"）
                #    统计量（words/run_words/over_count）由 _content_ckpt_payload
                #    内部统一回填（见该函数 docstring 的 R-1），此处不再手写。
                try:
                    await _save_content_checkpoint(task_id, _content_ckpt_payload("stopped", "用户已停止"))
                except Exception:
                    logger.warning("检查点写入失败（task=%s · 正文生成 · 阶段成果）", task_id, exc_info=True)
                await _update_progress_safe(stop_progress, "用户已停止", event="stopped")
                await finish_task(task_id, "stopped", "用户已停止")
                # ✅ 2026-09-24：stopped 与 completed 同口径携带
                #    failed_sections —— 此前只有 completed 下发，停止后前端
                #    日志区整片空白，用户看不到停止前已有哪几章失败。
                # ✅ 补 failed_count（2026-10-01 第七模块专项 · C-3）：
                #    前端 useContentGeneration.ts:165 与 SchemeWorkbenchPage.tsx:5299
                #    均按 `typeof evt.failed_count === 'number'` 读取该字段，
                #    而后端此前两处 stopped 载荷都不含它 → 恒为 0，停止提示
                #    永远显示「0 章失败」，与 checkpoint 重挂接路径（带该字段）不一致。
                # ✅ P1 修复（2026-10-04 · failed_count 口径统一）：
                #    旧实现 stopped 分支用 `len(_failed_reasons)` 作 failed_count，
                #    而 completed 分支用 `max(total - len(done_ids), 0)`——两条终态
                #    路径口径不一致，前端在用户停止与正常完成两种场景下读到
                #    「失败章数」会互相打架（同样是 20 章失败，一条报 20、另一条
                #    报别的值）。统一用 done_ids 口径：
                #      · done_ids 只记录**成功落库**的章节（_persist_section 成功
                #        commit 后 add）；
                #      · 未被计入 done_ids 的章节 = 失败 / 未处理 / 用户跳过；
                #      · 与 completed / all-failed 三条终态路径完全同口径。
                #    _failed_reasons 仍保留作为**明细**下发，让前端能展示"哪几章
                #    为什么失败"，但**计数**必须与 completed 一致，否则终态横幅
                #    与统计卡对不上。
                # ✅ 载荷改由 _stopped_payload 单一拼装点产出（R-2），
                #    与 CancelledError 路径字段完全一致。
                yield f"data: {json.dumps(_stopped_payload('用户已停止', stop_progress), ensure_ascii=False)}\n\n"
                return

            # ✅ 自动流程：全文一致性 Agent 修复阶段
            #    正文全部落库后执行「扫描 → 仲裁 → 定向修复」：
            #    修复前自动快照、修复结果先写正文但状态为 pending_confirm，
            #    用户可在「审核与预检 → 全文一致性」界面逐条确认或一键回滚。
            #    该阶段任何失败都不影响已生成正文。
            consistency_summary = None
            if auto_consistency_repair and not stopped and mode not in ("section", "continue"):
                cons_queue: asyncio.Queue = asyncio.Queue()
                CONS_DONE = object()

                async def _run_consistency_pipeline():
                    try:
                        from app.services import conflict_arbiter as _arbiter
                        from app.services import consistency_scanner as _scanner
                        from app.services import repair_agent as _repair_agent

                        def _put(event: str, **kw):
                            cons_queue.put_nowait(
                                json.dumps({"event": event, "task_id": task_id, **kw}, ensure_ascii=False) + "\n\n"
                            )

                        async def _scan_cb(done: int, total: int, message: str):
                            # ✅ 进度增强：扫描阶段映射到 [_SECTION_PHASE_MAX, 0.98]
                            _p = _SECTION_PHASE_MAX + (0.98 - _SECTION_PHASE_MAX) * (done / total if total else 0.0)
                            _p = round(min(_p, 0.98), 4)
                            _put("consistency_scan_progress", done=done, total=total, message=message, progress=_p)
                            try:
                                await _update_progress_safe(_p, message)
                            except Exception:
                                logger.debug("一致性扫描进度更新失败（不影响扫描 · task=%s）", task_id, exc_info=True)

                        async def _repair_cb(done: int, total: int, message: str):
                            # ✅ 进度增强：定向修复阶段映射到 [0.98, 1.0]
                            _p = 0.98 + (1.0 - 0.98) * (done / total if total else 0.0)
                            _p = round(min(_p, 1.0), 4)
                            _put("consistency_repair_progress", done=done, total=total, message=message, progress=_p)
                            try:
                                await _update_progress_safe(_p, message)
                            except Exception:
                                logger.debug("一致性修复进度更新失败（不影响修复 · task=%s）", task_id, exc_info=True)

                        scan_result = await _scanner.run_scan(
                            db,
                            scheme_id=scheme_id,
                            project_id=project_id,
                            scheme_name=scheme.get("name", ""),
                            scheme_type=scheme.get("type", ""),
                            progress_cb=_scan_cb,
                        )
                        if not scan_result.get("sections"):
                            _put("consistency_skipped", reason="方案尚无正文，跳过一致性检查")
                            return

                        conflicts = await _arbiter.arbitrate_conflicts(
                            db,
                            scan_result["conflicts"],
                            facts=scan_result["contexts"].get("global_facts", ""),
                            design_docs=scan_result["contexts"].get("design_docs", ""),
                            standards=scan_result["contexts"].get("standards", ""),
                            project_requirements=scan_result["contexts"].get("project_docs", ""),
                        )

                        def _sev(c):
                            return c.get("severity", "medium")

                        summary = {
                            "scan_id": scan_result["scan_id"],
                            "total": len(conflicts),
                            "high": sum(1 for c in conflicts if _sev(c) == "high"),
                            "medium": sum(1 for c in conflicts if _sev(c) == "medium"),
                            "low": sum(1 for c in conflicts if _sev(c) == "low"),
                            "pending": sum(1 for c in conflicts if c.get("status") == "pending"),
                            "skipped": sum(1 for c in conflicts if c.get("status") == "skipped"),
                        }
                        _put("consistency_scan_done", scanned_sections=scan_result["sections"], summary=summary)

                        # ✅ R122-G2：开关 `consistency_fullbook_repair_enabled` 默认关闭
                        #    ⇒ else 分支逐字节保持既有逐章 run_repair 链路；开启时收尾
                        #    自动修复改走整卷合并修复（少轮整卷往返 + 小节闸门）。
                        if bool(getattr(settings, "consistency_fullbook_repair_enabled", False)):
                            from app.services.consistency_fullbook_repair import (
                                run_fullbook_repair as _run_fullbook,
                            )

                            repair_result = await _run_fullbook(
                                db,
                                scheme_id=scheme_id,
                                scan_id=scan_result["scan_id"],
                                conflicts=conflicts,
                                facts=scan_result.get("contexts", {}).get("global_facts", ""),
                                severity_threshold=consistency_severity,
                                progress_cb=_repair_cb,
                            )
                        else:
                            repair_result = await _repair_agent.run_repair(
                                db,
                                scheme_id=scheme_id,
                                scan_id=scan_result["scan_id"],
                                conflicts=conflicts,
                                mode="auto",
                                severity_threshold=consistency_severity,
                                contexts=scan_result.get("contexts"),
                                # ✅ BUG 修复（2026-10-03 · 跨模块传递丢失）：入口早已
                                #    解析 force_full_repair（:4583），但从未传进本调用 ——
                                #    ruff F841 坐实其为死变量。后果：前端「强制全量重修」
                                #    复选框（SchemeWorkbenchPage :5087 / useContentGeneration
                                #    :127 都随 generate-content 请求体下发，且前端注释明言
                                #    「同时作用于正文生成收尾的自动一致性修复」）在主生成
                                #    链路**静默失效**：勾选后收尾修复仍跳过「已修复且成果
                                #    仍在」的冲突，用户白花一遍生成却没拿到预期强度重修。
                                #    独立修复端点 consistency_repair.py:144 消费正常，
                                #    两条路径口径在此分叉。现补齐透传。
                                force_full_repair=force_full_repair,
                                progress_cb=_repair_cb,
                            )
                        _put(
                            "consistency_repair_done",
                            scan_id=scan_result["scan_id"],
                            repair_id=repair_result.get("repair_id"),
                            snapshot_id=repair_result.get("snapshot_id"),
                            repaired=repair_result.get("repaired", 0),
                            failed=repair_result.get("failed", 0),
                            skipped=repair_result.get("skipped", 0),
                            summary=summary,
                        )
                    except asyncio.CancelledError:
                        # ✅ BUG-1（2026-09-19 · 停止不是失败）：取消必须**re-raise**。
                        #    旧实现只记日志就吞掉 —— 用户点停止（或「防僵尸替换」触发
                        #    request_control("stop")）后，cons_runner 正常返回，
                        #    一致性阶段被当作"完成"，主流程继续发 completed 事件并把
                        #    schemes.status 改成"已生成"—— 用户明明点了停止，
                        #    方案却显示生成完毕。
                        logger.info("全文一致性修复阶段被取消（向上传播）")
                        raise
                    except Exception as e:
                        logger.warning("全文一致性 Agent 修复失败（不影响正文）: %s", e)
                        try:
                            cons_queue.put_nowait(
                                json.dumps(
                                    {"event": "consistency_failed", "task_id": task_id, "reason": str(e)[:150]},
                                    ensure_ascii=False,
                                )
                                + "\n\n"
                            )
                        except Exception:
                            logger.debug("一致性失败事件推送失败（不影响收尾 · task=%s）", task_id, exc_info=True)
                    finally:
                        await cons_queue.put(CONS_DONE)

                cons_runner = asyncio.create_task(_run_consistency_pipeline())
                register_child_task(task_id, cons_runner)
                # ✅ 进度增强：进入全文一致性阶段（进度从 _SECTION_PHASE_MAX 推进到 1.0）。
                #    旧实现此处写死 1.0，导致最后数分钟的扫描 + 定向修复期间进度条
                #    停在 100% 不动，用户误判卡死。
                _prog["phase"], _prog["phase_label"] = "consistency", "全文一致性检查"
                await _update_progress_safe(_SECTION_PHASE_MAX, "正文完成，正在执行全文一致性扫描...")
                yield f"data: {json.dumps({'event':'consistency_scan_start','task_id':task_id,'progress':_SECTION_PHASE_MAX}, ensure_ascii=False)}\n\n"
                while True:
                    line = await cons_queue.get()
                    if line is CONS_DONE:
                        break
                    # 捕获扫描/修复结果，随 completed 事件一并带给前端
                    try:
                        _obj = json.loads(line.strip())
                        if _obj.get("event") == "consistency_scan_done":
                            consistency_summary = _obj.get("summary")
                            _m = (
                                f"全文一致性扫描完成：发现 {consistency_summary.get('total', 0)} 处"
                                f"（高 {consistency_summary.get('high', 0)}），正在定向修复..."
                            )
                            await _update_progress_safe(0.98, _m)
                        elif _obj.get("event") == "consistency_repair_done":
                            consistency_summary = _obj.get("summary") or consistency_summary
                            if isinstance(consistency_summary, dict):
                                consistency_summary = {
                                    **consistency_summary,
                                    "repair_id": _obj.get("repair_id"),
                                    "snapshot_id": _obj.get("snapshot_id"),
                                    "repaired": _obj.get("repaired", 0),
                                    "failed": _obj.get("failed", 0),
                                    "skipped": _obj.get("skipped", 0),
                                }
                            _m = (
                                f"全文一致性修复完成：已修复 {_obj.get('repaired', 0)} 处，"
                                f"失败 {_obj.get('failed', 0)} 处，"
                                f"待人工确认 {_obj.get('skipped', 0)} 处"
                            )
                            await _update_progress_safe(1.0, _m)
                    except Exception:
                        logger.debug(
                            "一致性进度事件解析/进度更新失败（事件照常下发 · task=%s）", task_id, exc_info=True
                        )
                    yield f"data: {line}"
                await cons_runner

            # ✅ R122-G3：成稿后跨章节字数批量调度。开关
            #    `word_adjust_batch_enabled` **默认关闭 ⇒ 整段不执行，收尾链
            #    与引入前逐字节一致**。思路引自易标（contentGenerationTask
            #    .cjs:28-45 TOTAL_WORD_ADJUSTMENT_BATCH_SIZE=10）：收集成稿
            #    under/over 章，按目录序每 10 章一批、批间串行批内并发；
            #    执行不新造 AI 交互形态 —— under 走既有续写链
            #    （_continue_if_needed，R66 触发线与轮次封顶不变）、over 走
            #    既有压缩链（_auto_shrink_if_over，失败保留原正文），只补
            #    「检出→分批调度」这最后一公里（DLV-02/08 原为转人工）。
            if bool(getattr(settings, "word_adjust_batch_enabled", False)) and done_ids:
                from app.services.word_adjust_batch import (
                    WORD_ADJUST_CONCURRENCY,
                    build_adjust_plan,
                )

                _adj_leaf_by_id = {str(_l.get("id") or ""): _l for _l in (leaves or [])}
                # 1) 清单以**库内最新落库态**为真值 —— 一致性修复可能已改写
                #    正文，内存 generated_contents / audit_rows 都可能过时。
                _adj_entries: list[dict] = []
                try:
                    _adj_ph = ",".join("?" * len(done_ids))
                    _adj_cur = await db.execute(
                        f"SELECT id, title, content FROM sections " f"WHERE id IN ({_adj_ph})", tuple(done_ids)
                    )
                    for _r in await _adj_cur.fetchall() if _adj_cur is not None else []:
                        _leaf = _adj_leaf_by_id.get(str(_r["id"] or ""))
                        if _leaf is None:
                            continue
                        _adj_entries.append(
                            {
                                "section_id": str(_r["id"]),
                                "title": str(_r["title"] or ""),
                                "word_count": text_word_count(_r["content"] or ""),
                                "word_budget": leaf_word_budget(_leaf),
                            }
                        )
                except Exception:
                    logger.warning("字数批调度清单读取失败（跳过调度）", exc_info=True)
                    _adj_entries = []
                # SELECT IN 不保序 —— 按 leaves 的目录 DFS 序还原（落库/展示/
                # 导出顺序与目录一致是 R105 既有纪律，调度不改任何序）。
                _adj_order = {str(_l.get("id") or ""): _i for _i, _l in enumerate(leaves or [])}
                _adj_entries.sort(key=lambda _e: _adj_order.get(_e["section_id"], 1 << 30))
                _adj_plan = build_adjust_plan(_adj_entries)

                if _adj_plan["items"]:
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "event": "word_adjust_start",
                                "task_id": task_id,
                                "under": _adj_plan["under_count"],
                                "over": _adj_plan["over_count"],
                                "batch_count": len(_adj_plan["batches"]),
                            },
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
                    _adj_sem = asyncio.Semaphore(WORD_ADJUST_CONCURRENCY)
                    _adjusted: dict = {}

                    async def _adjust_one(item: dict) -> None:
                        """单章扩/缩 + 回写（fail-soft：任何异常只留日志，
                        绝不打断收尾 —— 与一致性管道同一容错纪律）。"""
                        _sid = item["section_id"]
                        _leaf = _adj_leaf_by_id.get(_sid)
                        if _leaf is None or is_stopped(task_id):
                            return
                        try:
                            await wait_resume(task_id)
                            if is_stopped(task_id):
                                return
                            _cur0 = await db.execute("SELECT content FROM sections WHERE id=?", (_sid,))
                            _row0 = await _cur0.fetchone() if _cur0 is not None else None
                            _content = str((_row0[0] if _row0 else "") or "")
                            if not _content.strip():
                                return
                            _wb = leaf_word_budget(_leaf)
                            if item["direction"] == "under":
                                # 重建 user_content 供续写轮复用（默认
                                # _plan_enabled=False ⇒ 不再触发编排 AI 调用）
                                try:
                                    (
                                        _g_eff,
                                        _g_facts,
                                        _g_msgs,
                                        _g_user,
                                        _g_plan_json,
                                    ) = await _build_generation_context(_leaf)
                                except Exception:
                                    logger.warning("字数批调度：%s 上下文重建失败（跳过）", _sid, exc_info=True)
                                    return
                                _new, _cont_failed = await _continue_if_needed(
                                    _content, _leaf, _g_user, str(_leaf.get("title") or ""), eff_standard=_g_eff
                                )
                                if _cont_failed or _new == _content:
                                    return
                                _c2, _wc2, _ws2, _ = await _persist_section(
                                    _sid,
                                    _new,
                                    _wb,
                                    eff_standard=_g_eff,
                                    fact_rows_for_section=_g_facts,
                                    section_title=str(_leaf.get("title") or ""),
                                    plan_json=_g_plan_json,
                                )
                            else:
                                _new = await _auto_shrink_if_over(_content, _leaf)
                                if _new == _content:
                                    return
                                _c2, _wc2, _ws2, _ = await _persist_section(
                                    _sid, _new, _wb, section_title=str(_leaf.get("title") or "")
                                )
                            # 2) 同步内存缓存与终态审计口径：word_audit 保持
                            #    「每章一行、取最终落库态」（原地替换而非追加，
                            #    防 completed 事件 total 被调度章双计）；
                            #    _stat["over"] 按新旧判定差额修正（完成消息一致）。
                            generated_contents[_sid] = _c2
                            _old_row = None
                            for _ar in reversed(_stat.get("audit_rows") or []):
                                if _ar.get("section_id") == _sid:
                                    _old_row = _ar
                                    break
                            _old_ws = word_status_for(int((_old_row or {}).get("word_count") or 0), _wb)
                            if _old_ws == "over" and _ws2 != "over":
                                _stat["over"] = max(0, _stat.get("over", 0) - 1)
                            elif _old_ws != "over" and _ws2 == "over":
                                _stat["over"] = _stat.get("over", 0) + 1
                            if _old_row is not None:
                                _old_row["word_count"] = _wc2
                                _old_row["word_status"] = _ws2
                            else:
                                _stat.setdefault("audit_rows", []).append(
                                    {
                                        "section_id": _sid,
                                        "title": str(_leaf.get("title") or ""),
                                        "level": int(_leaf.get("level") or 0),
                                        "word_budget": _wb,
                                        "word_count": _wc2,
                                    }
                                )
                            _adjusted[_sid] = {
                                "title": str(_leaf.get("title") or ""),
                                "direction": item["direction"],
                                "before": item["word_count"],
                                "after": _wc2,
                                "word_status": _ws2,
                            }
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.warning("字数批调度：章节 %s 调整失败" "（保留原正文）", _sid, exc_info=True)

                    _adj_done = 0
                    for _bi, _batch in enumerate(_adj_plan["batches"]):
                        if is_stopped(task_id):
                            break
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "event": "word_adjust_batch_start",
                                    "task_id": task_id,
                                    "batch": _bi + 1,
                                    "batch_total": len(_adj_plan["batches"]),
                                    "sections": [_it["section_id"] for _it in _batch],
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                        # 批内并发、批间串行（本 for 不并发 ⇒ 批序即目录序）
                        await asyncio.gather(*(_adjust_one(_it) for _it in _batch))
                        _adj_done += len(_batch)
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "event": "word_adjust_batch_done",
                                    "task_id": task_id,
                                    "batch": _bi + 1,
                                    "done": _adj_done,
                                    "total": len(_adj_plan["items"]),
                                    "adjusted": _adjusted,
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "event": "word_adjust_done",
                                "task_id": task_id,
                                "planned": len(_adj_plan["items"]),
                                "adjusted": len(_adjusted),
                            },
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )

            # 更新方案总字数
            # ✅ P0 修复（2026-09-27 · R13 漏改点）：db.execute() 在全局单连接 + aiosqlite
            #    下**可能返回 None**（连接/事务异常），直接 .fetchone() → AttributeError。
            #    且该代码位于**正文全部落库之后**：命中则整个 SSE 收尾报 500，
            #    schemes.word_count 未更新、review_status 未置 pending、任务终态未写 ——
            #    「成果在库、状态全空」，用户看到的是一个无法自洽的任务。
            #    参照 _chart_pipeline.py 的同类守卫：降级为 0 而不中断生成。
            total_wc = 0
            try:
                cur = await db.execute(
                    "SELECT COALESCE(SUM(word_count),0) FROM sections WHERE scheme_id=?", (scheme_id,)
                )
                if cur is None:
                    logger.warning("正文收尾统计失败（db.execute 返回 None），word_count 降级为 0")
                else:
                    _wc_row = await cur.fetchone()
                    total_wc = int((_wc_row[0] if _wc_row else 0) or 0)
            except Exception:
                logger.warning("正文收尾总字数统计异常，word_count 降级为 0", exc_info=True)
                total_wc = 0
            await db.execute(
                "UPDATE schemes SET word_count=?, status='审核中', updated_at=? WHERE id=?",
                (total_wc, datetime.now().isoformat(), scheme_id),
            )
            await db.commit()

            # ✅ P0-1：正文生成后自动把成功章节标记为待审核（打通 review 数据链）
            #    旧实现 review_status 始终为空字符串，审核 Tab 看不到"待审核"章节；
            #    导出前预检也无从判断"有多少正文尚未过审就准备交付"。
            if done_ids:
                placeholders = ",".join("?" * len(done_ids))
                # ✅ P1 修复（2026-09-27 · 审核留痕断层 + from_status 真实性）：
                #    旧实现只 UPDATE 了 sections.review_status，没写 review_records。
                #    review.py:134 的注释明写「全库只有本模块写 sections.review_status」，
                #    实际已被本处打破 —— 正文生成后审核 Tab 看到 N 章 pending，但
                #    /review/summary 的「最近评审记录」SELECT 的是 review_records，
                #    一条都查不到：状态机变了却无人能追溯。
                #
                #    ⚠️ **顺序是本修复的核心，不是风格问题**：必须**先 SELECT 旧状态
                #    再 UPDATE**。同一 `db` 连接上，SELECT 会读到本事务自己刚写入的
                #    未提交值 —— 若把 SELECT 放在 UPDATE 之后，`_old` 恒为 "pending"，
                #    幂等判断会把每一章都跳过，留痕 100% 空转（本轮首版即踩此坑）。
                #    故此处先取旧值快照，UPDATE 之后再据快照落留痕。
                _prior: dict = {}
                _titles: dict = {}
                try:
                    _prior_cur = await db.execute(
                        f"SELECT id, title, COALESCE(review_status,'') AS rs "
                        f"FROM sections WHERE id IN ({placeholders})",
                        tuple(done_ids),
                    )
                    if _prior_cur is not None:
                        for _r in await _prior_cur.fetchall():
                            _prior[_r["id"]] = _r["rs"] or ""
                            _titles[_r["id"]] = _r["title"] or ""
                except Exception:
                    # 读旧状态失败不阻断置位（置位本身是 P0-1 的既有行为）
                    logger.warning("读取章节审核旧状态失败（仍会置为待审核）", exc_info=True)
                await db.execute(
                    f"UPDATE sections SET review_status='pending', updated_at=? WHERE id IN ({placeholders})",
                    (datetime.now().isoformat(), *done_ids),
                )
                # 只对**真的发生状态变化**的章节落留痕（对齐
                # review.reset_review_on_content_change 的幂等约定
                # REVIEW_STATUS_NEED_RESET）："" / pending 本身就是「未过审」语义，
                # 重复置位没有信息量，否则重生成同一章每次都插一条、记录被刷屏。
                try:
                    from app.routers.review import _write_record as _write_review_record

                    # 预查一次 project_id 传入（review.py:99 明确要求批量调用方
                    # 这么做），避免 N 章 = N 次额外 SELECT
                    _pid = ""
                    try:
                        _pc = await db.execute(_fe.SCHEME_PROJECT_ID_SQL, (scheme_id,))
                        _pr = await _pc.fetchone() if _pc is not None else None
                        if _pr:
                            _pid = str(_pr[0] or "")
                    except Exception:
                        _pid = ""
                    for _sid in done_ids:
                        _old = _prior.get(_sid, "")
                        if _old in ("", "pending"):
                            continue
                        await _write_review_record(
                            db,
                            scheme_id,
                            _sid,
                            _titles.get(_sid, ""),
                            _old,
                            "pending",
                            "系统",
                            "正文已重新生成，原审核结论失效，自动退回待审核（请重新送审）",
                            project_id=_pid or None,
                        )
                except Exception:
                    # 留痕失败不得让整次收尾失败（正文已落库）
                    logger.warning("正文收尾写审核留痕失败（不影响生成结果）", exc_info=True)
                await db.commit()

            # ✅ 失败章节统计：done_ids 只记录"成功落库"的章节，其余即为失败
            failed_count = max(total - len(done_ids), 0)

            # ✅ 全章失败语义（2026-09-16 · 依据运行库真实日志）：
            #    历史记录里出现过「正文0字完成（55/55 章失败）」被标成 **completed**
            #    的任务 —— 一章都没写成，任务列表/活动中心却显示"完成"。这里与目录
            #    生成的 all_failed 口径对齐：全章失败时终态判 failed 并发 error 事件
            #    （携带失败明细，前端可直接逐章重试）。
            if total > 0 and not done_ids:
                _reason_txt = "；".join(
                    dict.fromkeys((v.get("reason") or "")[:60] for v in list(_failed_reasons.values())[:3])
                )
                # ✅ 病因如实播报（2026-09-29 · 「38/38 章全失败」事故复盘）：
                #   旧文案无条件写死「（AI 服务异常），请检查模型/AI 配置后重试」，
                #   而本次 38 章全失败的**真实病因是本地代码缺陷**（未闭合围栏修复
                #   函数返回契约漏改 → content 变元组 → AttributeError），
                #   与 AI 毫无关系。该话术把排障方向直接误导到「换模型/改 AI 配置」，
                #   用户照做后问题原样复现，而日志里又没有任何堆栈（guarded_gen 吞异常）。
                #   现按失败原因判定病因：全部为未预期异常（reason 以「生成异常:」开头）
                #   → 报「内部处理异常」并指引看后端日志；否则沿用原 AI 口径。
                #   仅改括注与指引语，消息结构与长度上限（200 字）保持不变。
                _all_local = bool(_failed_reasons) and all(
                    (v.get("reason") or "").startswith("生成异常:") for v in _failed_reasons.values()
                )
                _all_msg = (
                    f"全部 {total} 章生成失败（内部处理异常），" "请查看后端日志 logs/backend.log 定位后重试"
                    if _all_local
                    else f"全部 {total} 章生成失败（AI 服务异常），" "请检查模型/AI 配置后重试"
                ) + (f"：{_reason_txt}" if _reason_txt else "")
                try:
                    await _save_content_checkpoint(task_id, _content_ckpt_payload("error", _all_msg[:200]))
                except Exception:
                    logger.warning("检查点写入失败（task=%s · 正文生成 · 全章失败明细）", task_id, exc_info=True)
                await finish_task(task_id, "failed", _all_msg[:200])
                # ✅ 载荷改由 _error_payload 单一拼装点产出（R-3），与整批异常
                #    路径字段完全一致（含 progress / standard_summary）。
                yield f"data: {json.dumps(_error_payload(_all_msg), ensure_ascii=False)}\n\n"
                return

            # ✅ 图表与正文同步生成：图表编排已在正文生成时由文本 AI 一体完成
            #    （配图判定 → Mermaid/JSON 生成 → 插入位置由正文 AI 判定，
            #     格式与正文相同；每章持久化时由 register_inline_charts
            #     提取、校验/修复并登记），不再有正文后置的独立图表管线。
            # ✅ 字数口径修复（依据运行库真实日志）：`total_wc` 是**方案合计**字数，
            #    旧实现无论本次生成多少章都报"正文 N 字完成"，用户重生成单章时会
            #    看到整个方案的字数，误以为"一章写了 7 万字"。现区分本次与合计。
            _run_words = _prog.get("words", 0)
            _partial_run = mode in ("section", "continue") or len(done_ids) < total
            if _partial_run:
                _done_msg = f"本次生成 {_run_words} 字（方案合计 {total_wc} 字）"
            else:
                _done_msg = f"正文{total_wc}字完成"
            if failed_count:
                _done_msg += f"（{failed_count}/{total} 章失败）"
            # ✅ 超字数提示（依据运行库日志：96 章中 72% 超字数、均值 1.69 倍）：
            #    把本次超字数的章节数写进完成消息，用户才知道可以点「压缩本章」
            #    或开启「超字数自动压缩」。
            _over_n = _stat.get("over", 0)
            if _over_n:
                _done_msg += f"（{_over_n} 章超字数，可压缩或开启自动压缩）"

            # ✅ P0-3 修复（2026-10-07 · 一致性修复静默失败）：
            #    第4轮生产方案 4 章 repair_agent 全失败（全 Provider 熔断/配额冷却），
            #    但任务仍标记"任务完成"——成稿含未修复已知缺陷，用户无从得知。
            #    现：一致性修复有 failed > 0 时，任务终态标记为"degraded"（降级完成），
            #    完成消息追加失败数量与提示，前端可在状态栏/消息区展示降级标记。
            #    degraded 是加法式终态（与 completed 同语义：正文已落库、可导出），
            #    但额外携带一致性修复失败信号，用户据此决定是否重跑一致性修复。
            _cons_failed = 0
            if consistency_summary and isinstance(consistency_summary, dict):
                _cons_failed = int(consistency_summary.get("failed") or 0)
            _is_degraded = _cons_failed > 0
            if _is_degraded:
                _done_msg += f"（一致性修复 {_cons_failed} 处失败，" "成稿含未修复缺陷，建议重跑一致性修复）"
                logger.warning(
                    "正文生成降级完成：一致性修复 %d 处失败（scheme=%s · task=%s）", _cons_failed, scheme_id, task_id
                )
            _terminal_status = "degraded" if _is_degraded else "completed"
            # ✅ 「本次生成字数 / 超字数章数」同步进 _ckpt 由
            #    _content_ckpt_payload 统一负责（R-1），此处不再手写 ——
            #    stopped / 断线兜底 / 服务端取消 等**非 completed** 收尾路径
            #    同样会自动带上（此前只有 completed 记得写，断线重挂后前端统计全为 0）。
            completed_payload = {
                "event": _terminal_status,
                "task_id": task_id,
                # ✅ 终态进度与计数必须随 completed 一起下发：前端断线重挂
                #    读到的最后一个 completed 事件若缺 progress/done/total，
                #    进度条会停在最后一章的中间值、章节计数也对不上总数。
                # ✅ BUG 修复（2026-10-11 · 跨模块数据链 R70 · 终态 done 口径分裂）：
                #    旧实现硬写 `done=total`，而有失败章节时 checkpoint 侧
                #    `_ckpt["done"]` 只在**成功落库**时自增（:5423），是真实成功数。
                #    于是同一份「已完成 N/M」在在线终态读到 total、断线重挂读到
                #    成功数 —— 刷新页面后数字突然变小，与失败章节数也对不上。
                #    统一为成功数（与 _content_ckpt_payload 同口径）。
                "progress": 1.0,
                "done": len(done_ids),
                "total": total,
                "word_count": total_wc,
                "failed_count": failed_count,
                "run_words": _run_words,
                "over_count": _over_n,
                "message": _done_msg,
                # ✅ P0-3：一致性修复失败标记（degraded 时携带）
                **({"consistency_failed": _cons_failed} if _is_degraded else {}),
            }
            # ✅ 失败明细（2026-09-16 增强）：只给"失败 N 章"用户无法定位是哪几章、
            #    为什么失败（章节可能压根没进前端日志，例如异常路径）。
            #    随 completed 一并下发 section_id/标题/原因，前端可直接在日志区
            #    展示并支持逐章重试。上限 50 条，避免超大方案把载荷撑爆。
            if _failed_reasons:
                completed_payload["failed_sections"] = list(_failed_reasons.values())[:50]
            # ✅ R179 U5（§2.5 加法式诊断字段）：本次正文运行的 warmup 编排摘要。
            #    仅做并发读数接线（无代表先行编排）→ applied=False 恒成立；
            #    enabled=True 表示阶段并发上限已生效（equivalent=changed），
            #    enabled=False 表示逐字现状。旧消费方忽略未知字段。
            completed_payload["warmup"] = {
                "stage": "content",
                "group": "",
                "enabled": bool(_u5_content_conc),
                "concurrency": _u5_content_conc or effective_concurrency,
                "applied": False,
                "equivalent": "changed" if _u5_content_conc else "verbatim",
            }
            if consistency_summary is not None:
                completed_payload["consistency_summary"] = consistency_summary
            # F-CONTENT-STANDARD(2026-09-26 · B2): 生成标准校验汇总下发
            completed_payload["standard_summary"] = _std_sum
            # ✅ R77b（2026-10-15）：事实冲突定点修复终态汇总。与
            #    standard_summary 同处拼装；无冲突时全 0（前端据此不显示，
            #    不产生"一切正常"式噪音）。stopped / error 两条终态路径
            #    由各自拼装点同键下发，三处字段完全一致。
            completed_payload["fact_autofix_summary"] = _fa_sum
            # ✅ R66（2026-10-11）字数可追溯：逐章目标/实际/偏差明细 + 达标统计。
            #    与 _content_ckpt_payload 里的同名键同构（审核预检两处都可读）。
            try:
                completed_payload["word_audit"] = word_audit_summary(_stat.get("audit_rows") or [], tolerance=_word_tol)
            except Exception:
                completed_payload["word_audit"] = {}
            # ✅ 收尾顺序（2026-09-16 修复）：先落 checkpoint → 再置终态 → 最后 yield。
            #    旧实现 `yield completed` 之后才 finish_task：前端收到 completed 立即
            #    break（SSE 生成器被 aclose），`finally` 兜底就把**已成功完成**的任务
            #    写成 stopped（"客户端断开，任务已终止"）—— 任务列表/活动中心与真实
            #    结果相反（与目录生成同一类缺陷，本轮统一口径）。
            try:
                await _save_content_checkpoint(task_id, _content_ckpt_payload(_terminal_status, _done_msg))
            except Exception:
                logger.warning("检查点写入失败（task=%s · 正文生成 · 终态成果）", task_id, exc_info=True)
            # ✅ R105（G1/G4）：顺序校验 + 事实映射汇总与 stopped / error 两处
            #    拼装点同键同口径（三处字段一致纪律）。上方 _content_ckpt_payload
            #    已回填 _ckpt["order_check"] / _ckpt["fact_mapping"]，此处取同源值；
            #    全量映射只进 checkpoint（DB 面），SSE 终态仅带 summarize 汇总。
            completed_payload["order_check"] = _ckpt.get("order_check", {})
            completed_payload["fact_mapping_stats"] = summarize_fact_mapping(
                _ckpt.get("fact_mapping") or [], _fact_audit_by_leaf
            )
            await _update_progress_safe(1.0, _done_msg, event=_terminal_status)
            _prog["progress_max"] = 1.0
            await finish_task(task_id, _terminal_status, _done_msg)
            yield f"data: {json.dumps(completed_payload, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            # ✅ 成果清单落库（2026-09-21 修复）：本路径**必须**写 checkpoint。
            #    旧实现直接 finish_task + yield —— 用户在「全文一致性」阶段
            #    （常耗时数分钟）关闭页面时，已生成几十章的成果清单永久丢失，
            #    重挂接只看到一句"任务已取消"。与停止/断线路径同口径收口。
            #    收尾顺序仍为：checkpoint → finish_task → yield。
            try:
                await _save_content_checkpoint(task_id, _content_ckpt_payload("stopped", "任务已取消"))
            except Exception:
                logger.warning("检查点写入失败（task=%s · 正文生成 · 取消路径成果）", task_id, exc_info=True)
            await finish_task(task_id, "stopped", "任务已取消")
            # ✅ 同上（C-3）：取消/断连路径的 stopped 同样补 failed_count。
            # ✅ P1 修复（2026-10-04 · failed_count 口径统一）：
            #    与 completed / stopped（用户停止）保持一致，用
            #    `max(total - len(done_ids), 0)` 而非 `len(_failed_reasons)`——
            #    后者只包含"本次进入失败分支并写入 dict 的章"，前者覆盖
            #    "所有未成功完成"的章节（含被取消但未进入失败分支的章节），
            #    与前端"失败 N 章 / 共 M 章"的直观语义对齐。
            # ✅ R2 修复（2026-10-05）：本路径此前**缺 progress 与
            #    standard_summary**（注释却写"已补齐"）。前端 stopped 分支读
            #    `evt.progress` 定位进度条 —— 服务端 SSE 总超时中断（连接仍在）
            #    时进度条会冻结在最后一次心跳值。现由 _stopped_payload 单一
            #    拼装点产出，两条 stopped 路径字段**完全一致**。
            yield f"data: {json.dumps(_stopped_payload('任务已取消'), ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.exception("正文生成失败（scheme_id=%s · task=%s）", scheme_id, task_id)
            # ✅ 成果清单落库：整批失败时已落库的章节仍需可追溯（哪几章成功/失败）
            try:
                await _save_content_checkpoint(task_id, _content_ckpt_payload("error", str(e)[:200]))
            except Exception:
                logger.warning("检查点写入失败（task=%s · 正文生成 · 异常终止明细）", task_id, exc_info=True)
            await finish_task(task_id, "failed", str(e))
            # ✅ R3 修复（2026-10-05）：本路径此前只带 message，把
            #    total / failed_count / failed_sections / progress /
            #    standard_summary 全丢了。整批异常恰恰最需要明细 —— 异常可能
            #    发生在任何章节进入 gen_one 之前（例如章节树查询失败），一条
            #    section_error 都没下发过，_failed_reasons 里的明细被随载荷丢弃，
            #    用户只看到一句病因文案、逐章失败原因彻底丢失。
            yield f"data: {json.dumps(_error_payload(str(e)), ensure_ascii=False)}\n\n"
        finally:
            # ✅ 断开兜底：客户端断开时 GeneratorExit 直接落在 yield 处（不经过
            # except），gen_runner 无人 await、任务永远停在 running。幂等清理：
            # 仅当任务尚未进入终态时兜底 finish_task（其内部会对非 completed
            # 状态统一 cancel child_tasks，停止语义单点收口）。
            if has_active_task(task_id):
                # ✅ B1：早期异常时协程尚未创建，gen_runner 为 None ——
                #    无守卫直接 .cancel() 会抛 AttributeError，让 finally 自身
                #    失败 → finish_task 走不到 → 任务永久卡在 running。
                if gen_runner is not None:
                    gen_runner.cancel()
                # ✅ 成果清单落库（2026-09-16 增强）：把「已生成 N/M 章、哪几章失败」
                #    写进 checkpoint —— 用户刷新页面后前端重挂接 GET /sse/task/{id}
                #    即可拿到失败明细，而不是只有一句"后台任务已停止"。
                try:
                    await _save_content_checkpoint(task_id, _content_ckpt_payload("stopped", "客户端断开，任务已终止"))
                except asyncio.CancelledError:
                    logger.warning("断线兜底保存正文成果被取消（task=%s）", task_id)
                except Exception:
                    logger.warning("断线兜底保存正文成果失败（task=%s）", task_id, exc_info=True)
                # ✅ R177（2026-11-05 · 断线收尾 P1）：终态落库必须 shield ——
                #    取消风暴下 finish_task 的「等待」会被打断，导致 DB 里任务永远
                #    停在 running（前端 pollTaskUntilTerminal 永远等不到终态），
                #    内存态也残留。生产实证：11 次「断线兜底保存正文成果被取消」
                #    之后**严格 3 秒**出现「mark_stream_ended: 未落终态」。
                #    与 bid_analysis.py 断线兜底同一范式。
                try:
                    await asyncio.shield(finish_task(task_id, "stopped", "客户端断开，任务已终止"))
                except asyncio.CancelledError:
                    logger.info("正文生成断线兜底 finish_task 已 shield（task=%s）", task_id)
                except Exception:
                    logger.warning("正文生成断线兜底 finish_task 失败（task=%s）", task_id, exc_info=True)
            # ✅ P0 悬挂写事务兜底（2026-09-25 · 对齐 bid_analysis 的 shield 模式）：
            #    SSE 流被取消时，task_registry / checkpoint 的写可能停在
            #    「execute() 已 BEGIN、commit() 未发生」的状态。全局共享连接
            #    一旦挂着未提交写事务，**后续所有写都要等它提交/回滚** →
            #    全站 500 "database is locked"（db.settle_global_conn 的注释
            #    记载的正是这一事故）。本模块此前只 import 未调用。
            #    用 asyncio.shield：外层协程被取消时，内层回滚仍在后台跑完，
            #    否则「为解冻而去 await」本身会被取消 → 事务照旧悬挂。
            try:
                await asyncio.shield(settle_global_conn("content_finally"))
            except asyncio.CancelledError:
                logger.warning("正文生成收尾悬挂事务收敛被取消（task=%s）", task_id)
            except Exception:
                logger.warning("正文生成收尾悬挂事务收敛失败（task=%s，不阻断）", task_id, exc_info=True)

    # ✅ 进度增强：心跳通道携带运行统计（stats_provider）—— 单章 AI 调用期间
    #    event_stream 整体挂起、无法 yield，只有心跳 Task 仍在运行，由它周期性
    #    推送「已耗时 / 进行中章节 / 累计字数 / ETA」，消除进度条长时间静止。
    return StreamingResponse(
        with_heartbeat(event_stream(), stats_provider=_stats_provider, on_end=_stream_end_guard(_content_holder)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- 全局事实提取（增强版：分段 + 合并去重 + 矛盾检测） ----------
async def _project_doc_diag_cols(db) -> set:
    """探测 project_documents 的诊断列集合（parse_truncated / parse_warnings）。

    两列均由 ``db._migrate`` 幂等补列，但测试库 / 外部进程直改的库可能尚未补齐，
    故按实际列集合动态拼装 SQL —— **缺列只降级该列，绝不让读路径整体失败**
    （与 ``bid_analysis._list_parsed_documents`` 同口径）。
    """
    try:
        cur = await db.execute("PRAGMA table_info(project_documents)")
        rows = await cur.fetchall()
        return {str(r[1]) for r in rows} if rows else set()
    except Exception:  # pragma: no cover - PRAGMA 不可用时按「无诊断列」处理
        return set()


async def _load_facts_source_docs(db, project_id: str):
    """读事实提取的源文档，返回 ``(file_names, bodies, truncated_file_names)``。

    ✅ 修复（2026-09-30 第十四轮 · P0）：此前 ``generate_facts`` 的 SQL 只取
    ``(file_name, parsed_markdown)``，**从不读** ``parse_truncated`` ——
    而解析阶段已把「PDF 截页 / 表格截行 / 落库字数超限」写进该列。于是
    一份只被解析了前 N 页的招标文件会被当成**完整依据**参与事实提取，
    用户看到「提取完成」却无从得知依据是残缺的（``bid_analysis`` 同类读路径
    早已消费该列，本链路是漏改点）。现把截断文档清单一并回传，由 SSE 以
    ``warning`` 事件显式告知用户。

    三层降级（任一失败都不阻断提取）：
    ① 有 ``parse_truncated`` 列 → 直接读；
    ② 仅有 ``parse_warnings`` 列（旧库）→ 按告警文本含「截断」兜底判定；
    ③ 两列都没有 / 读失败 → 只取基础两列，``truncated`` 为空（即旧行为）。
    """
    base = (
        "SELECT file_name, parsed_markdown FROM project_documents "
        "WHERE project_id=? AND parsed_markdown IS NOT NULL "
        "AND parsed_markdown != '' ORDER BY created_at, id"
    )
    names: list[str] = []
    bodies: list[str] = []
    truncated: list[str] = []
    try:
        cols = await _project_doc_diag_cols(db)
        has_trunc = "parse_truncated" in cols
        has_warn = "parse_warnings" in cols
        if not (has_trunc or has_warn):
            cur = await db.execute(base, (project_id,))
            for r in await cur.fetchall():
                if r[1]:
                    names.append(r[0] or "")
                    bodies.append(r[1])
            return names, bodies, truncated

        extra = []
        if has_trunc:
            extra.append("parse_truncated")
        if has_warn:
            extra.append("parse_warnings")
        sql = (
            "SELECT file_name, parsed_markdown, " + ", ".join(extra) + " FROM project_documents "
            "WHERE project_id=? AND parsed_markdown IS NOT NULL "
            "AND parsed_markdown != '' ORDER BY created_at, id"
        )
        cur = await db.execute(sql, (project_id,))
        for r in await cur.fetchall():
            d = dict(r)
            if not d.get("parsed_markdown"):
                continue
            names.append(d.get("file_name") or "")
            bodies.append(d["parsed_markdown"])
            if has_trunc and d.get("parse_truncated"):
                truncated.append(d.get("file_name") or "")
            elif not has_trunc and "截断" in str(d.get("parse_warnings") or ""):
                truncated.append(d.get("file_name") or "")
    except Exception as e:  # noqa: BLE001 - 诊断列读取失败不应阻断提取
        logger.warning("读取文档截断诊断失败（按未截断处理）: %s", e)
        names, bodies = [], []
        try:
            cur = await db.execute(base, (project_id,))
            for r in await cur.fetchall():
                if r[1]:
                    names.append(r[0] or "")
                    bodies.append(r[1])
        except Exception as e2:  # noqa: BLE001
            logger.warning("读取源文档失败: %s", e2)
    return names, bodies, truncated


async def _mark_docs_extract_status(db, project_id: str, status: str = "success") -> int:
    """事实提取成功后回写 ``project_documents.extract_status / extract_time``。

    ✅ 2026-11-05（第二轮日志驱动分析 P1）：生产库中 ``doc_chunks=266``、
    ``global_facts=190``（17 条清单项），但 ``project_documents.extract_status``
    长期停在 ``pending``。根因是唯一会写这两列的是 ``doc_pipeline`` 的
    ``sync_extract_layer``（只在「提取层同步」显式调用时执行），而事实 SSE
    完成分支（``generate_facts``）落库成功后就再没人回写 —— 状态列与
    事实表长期自相矛盾。

    口径：只更新**确有已解析正文**的文档（``parsed_markdown`` 非空），即真正
    参与本次提取的来源；字节与名称匹配旧链路，不触碰未解析 / 解析失败的文档。
    缺列（旧库）时降级为 0 行更新，不报错；调用方按 fail-soft 处理。
    """
    try:
        cols = await _project_doc_diag_cols(db)
    except Exception as e:  # noqa: BLE001 — 诊断列读取失败不影响提取
        logger.warning("回写文档提取状态：读取列失败，跳过（%s）", e)
        return 0
    if "extract_status" not in cols:
        return 0
    sets = ["extract_status=?"]
    params: list = [status]
    if "extract_time" in cols:
        sets.append("extract_time=datetime('now','localtime')")
    try:
        cur = await db.execute(
            "UPDATE project_documents SET " + ", ".join(sets) + " "
            "WHERE project_id=? AND parsed_markdown IS NOT NULL AND parsed_markdown != ''",
            (*params, project_id),
        )
        n = safe_rowcount(cur)
        await db.commit()
        if n:
            logger.info("已回写 %d 份文档提取状态=%s（project=%s）", n, status, project_id[:8])
        return n
    except Exception as e:  # noqa: BLE001
        logger.warning("回写文档提取状态失败（不影响事实提取）: %s", e)
        return 0


async def _count_docs_pending_parse(db, project_id: str):
    """返回 ``(项目下是否有任何文档, 待解析文档名列表)``。

    「待解析」= 已上传但正文为空的文档。旧实现复用「只查已解析文档」的
    结果集算 ``pending``，该集合里每行的正文都非空 —— 于是
    ``pending_docs`` **恒为空**，「有 N 份文档尚未解析」的告警从未触发过。
    """
    try:
        cur = await db.execute(
            "SELECT file_name, parsed_markdown FROM project_documents " "WHERE project_id=? ORDER BY created_at, id",
            (project_id,),
        )
        rows = await cur.fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning("统计待解析文档失败（降级为 0）: %s", e)
        return False, []
    pending = [r[0] or "" for r in rows if not (r[1] or "").strip()]
    return bool(rows), pending


def _facts_all_skipped_payload(extraction_result) -> tuple:
    """事实提取「全部段被跳过」时的状态与文案判定（R175 续 · 2026-11-04）。

    同样走 ``all_skipped`` 早退，实际有两种来源，旧实现统一回「均已提取过」：
      · **增量跳过**：历史确有成功提取的成果 → ``completed``，文案点明
        「本次无新增内容」；
      · **失败退避跳过**：全部段都在 ``facts_chunk_failures`` 的退避窗口内，
        此前从未成功提取 → 必须按 ``failed`` 上报，不能计入成功数，文案说明
        「暂无已提取成果」并给出立即重试入口。

    返回 ``(task_status, sse_event, message, extra_fields)``；extra_fields 为
    加法式回传字段（skipped / deferred / skipped_source），旧字段一个不删。
    """
    skipped_n = int(getattr(extraction_result, "skipped_chunks", 0) or 0)
    real_ok_n = len(getattr(extraction_result, "chunk_hashes_ok", None) or set())
    deferred_n = max(0, skipped_n - real_ok_n)
    if skipped_n > 0 and real_ok_n == 0:
        msg = (
            f"全部 {skipped_n} 段均处于失败重试退避窗口，本次未重新提取"
            "（此前提取未成功，暂无已提取成果）；如需立即重试请使用「全部重新提取」"
        )
        return (
            "failed",
            "error",
            msg,
            {
                "skipped": skipped_n,
                "deferred": deferred_n,
                "skipped_source": "deferred_backoff",
            },
        )
    if deferred_n:
        msg = (
            f"全部 {skipped_n} 段均已提取过，本次无新增内容"
            f"（其中 {deferred_n} 段处于失败重试退避窗口，暂未重试）；"
            "如需强制刷新请使用「全部重新提取」"
        )
    else:
        msg = f"全部 {skipped_n} 段均已提取过，本次无新增内容；" "如需强制刷新请使用「全部重新提取」"
    return (
        "completed",
        "completed",
        msg,
        {
            "skipped": skipped_n,
            "deferred": deferred_n,
            "skipped_source": "incremental",
        },
    )


def _facts_partial_terminal_payload(extraction_result) -> tuple[str, str, str]:
    """部分段未成功时的终态判定（R180 · 2026-11-05）。

    背景（运行库实证）：一次全局事实提取 23 段中 16 段成功、7 段模型声明
    「未提取到事实」，任务仍以 ``completed`` + 「全局事实提取完成」落库，
    用户无法区分「全部成功」与「部分降级」。

    判定口径（只针对**已经产出事实**的收尾路径；零事实在前置分支已判 failed）：
      · ``failed=0`` 且 ``declared_failed=0`` → ``completed``（全部段成功）；
      · 任一未成功段（真实失败或模型声明未提取）→ ``degraded``。
        ``degraded`` 与 ``completed`` 同语义（事实已落库、可继续生成正文），
        只是额外标注「结果不完整」，前端按终态处理并显示降级原因。

    ``skipped``（增量跳过已提取段）是正常的增量行为，**不**触发 degraded。
    返回 ``(task_status, sse_event, message)``。
    """
    seg = getattr(extraction_result, "segment_stats", None) or {}
    failed_n = int(seg.get("failed") or 0)
    declared_n = int(seg.get("declared_failed") or 0)
    total_n = int(seg.get("total") or 0)
    skipped_n = int(seg.get("skipped") or 0)
    bad_n = failed_n + declared_n
    _skip_part = f"，跳过 {skipped_n} 段已提取" if skipped_n else ""
    if bad_n <= 0:
        return "completed", "completed", f"全局事实提取完成{_skip_part}"

    # 可读原因：分类计数 + 最多 3 个涉及章节（failed_details / declared_failed_details 均带 heading）
    reasons: list[str] = []
    if failed_n:
        reasons.append(f"{failed_n} 段提取失败")
    if declared_n:
        reasons.append(f"{declared_n} 段未提取到事实")
    heads: list[str] = []
    for d in list(seg.get("failed_details") or []) + list(seg.get("declared_failed_details") or []):
        h = (d or {}).get("heading") if isinstance(d, dict) else None
        if h and h not in heads:
            heads.append(h)
    _items_part = f"；涉及：{'、'.join(heads[:3])}" if heads else ""
    msg = (
        f"全局事实提取降级完成（{'，'.join(reasons)}，共 {bad_n}/{total_n} 段未成功"
        f"{_skip_part}；结果可能不完整{_items_part}）"
    )
    return "degraded", "degraded", msg


@router.post("/generate-facts/{scheme_id}")
async def generate_facts(scheme_id: str, request: Request, db=Depends(get_db)):
    cur = await db.execute("SELECT * FROM schemes WHERE id=?", (scheme_id,))
    row = await cur.fetchone()
    if not row:
        from fastapi import HTTPException

        raise HTTPException(404, "方案不存在")
    # ✅ 并发竞态守卫（事实提取·自我防护，2026-10-04）：与 generate-outline /
    #    generate-content 同口径 —— 本端点此前只校验「方案是否存在」，不校验
    #    「是否已有同型事实提取任务在跑」。连点两次「提取事实」/ 双标签页并发会
    #    起两路 SSE，各自跑完整条提取管线，最终由 persist_extraction 的
    #    「DELETE+INSERT」后提交者覆盖前者（详见 sections.py 的
    #    facts_generation_in_progress docstring）。现同型任务在跑即 409 拒绝重入。
    from app.routers.sections import facts_generation_in_progress

    _fg = facts_generation_in_progress(scheme_id)
    if _fg:
        from fastapi import HTTPException as _HE

        logger.warning("generate_facts 409 冲突：scheme_id=%s 已有事实提取任务在跑", scheme_id)
        raise _HE(409, "本方案事实正在后台提取中，请勿重复触发（请等待当前提取完成，或在任务面板停止后再试）")
    scheme = dict(row)
    project_id = scheme["project_id"]

    # ✅ 缺值模式（对齐 OpenBidKit 全局事实三模式）：
    # fabricate（默认，合理补全并标记模拟值）/ omit（不杜撰，剔除模拟值）/
    # placeholder（资料未给出的值置为【待填写】）
    # ✅ 单一出口（2026-09-30 第十三轮）：此前本文件、`facts_extractor.run_extraction_pipeline`
    #    与 `facts_patches.MISSING_VALUE_MODES` 各写一份合法值域字面量。缺值模式
    #    是「用户可选的三种语义」，新增第四种时漏改一处 → 入口已放行而下游按
    #    fabricate 处理（用户选了新模式却看到「合理补全」的结果，且无任何报错）。
    #    现统一走 facts_patches.normalize_missing_value_mode（fail-closed 到 fabricate）。
    try:
        _facts_body = await request.json()
    except Exception:
        _facts_body = {}
    from app.services.facts_patches import normalize_missing_value_mode as _norm_facts_mode

    missing_value_mode = _norm_facts_mode((_facts_body or {}).get("missing_value_mode"))
    # ✅ 增量提取（2026-09-17）：默认跳过已完成段（上次提取成功的分段不再重复
    #    调用 AI）；force=true 时全量重提取（「全部重新提取」入口使用）。
    force_full = bool((_facts_body or {}).get("force"))

    # ✅ P1（2026-09-27）：心跳统计通道所需的闭包状态。定义在 event_stream
    #    **之外**，因为 `with_heartbeat(..., stats_provider=...)` 由本层调用，
    #    拿不到 event_stream 内部的局部变量。用单元素 list 承载 task_id 是
    #    为了让心跳回调能读到 register_task 的返回值（它在 event_stream 内）。
    #    心跳未触发时 task_id 为空串，ping 事件对前端是无害增量。
    _facts_task_id: list[str] = [""]
    _facts_started_at = time.time()
    # ✅ R109（P0）：事实链 on_end 宽限回收 holder（与正文链同口径）。
    _facts_holder: dict = {}

    def _facts_stats_provider() -> dict:
        """心跳统计载荷（与目录/正文链路同口径的 ping 事件）。

        纯读闭包状态、无副作用、永不抛异常 —— with_heartbeat 内部也会兜底，
        这里再兜一层是为了不让「统计异常」影响心跳与业务流。
        """
        try:
            return {
                "progress": round(_facts_progress[0], 4),
                "elapsed_ms": int((time.time() - _facts_started_at) * 1000),
                "task_id": _facts_task_id[0],
            }
        except Exception:  # noqa: BLE001 - 统计不得影响心跳
            return {}

    _facts_progress: list[float] = [0.0]

    async def event_stream():
        from app.services.facts_extractor import (
            format_for_frontend,
            invalidate_export_cache,
            load_completed_chunks,
            persist_extraction,
            run_extraction_pipeline,
            save_extracted_chunks,
        )

        # ✅ T-04（2026-11-06）：后台提取落库与手工写入共用项目级写锁，
        #    避免「用户在界面确认/新增事实」与「提取结果落库」交叉覆盖。
        from app.services.facts_write_guard import (
            acquire_project_facts_write,
            release_project_facts_write,
        )

        # ✅ 跨类型互斥（F5 · 2026-10-05 收口第三条链路，2026-10-06 补齐）：
        #    与 generate_outline / generate_content 对称 —— 路由体的
        #    facts_generation_in_progress pre-guard 只拦**同类型**重入，
        #    与本注册之间还隔着 await 窗口，且**完全不拦异类型**。
        #    事实提取与另外两条链路都写/读同一批 global_facts 行：
        #      · vs 正文生成：正文启动时快照 facts，提取中途
        #        persist_extraction 会 DELETE+INSERT 整批 → 正文注入到
        #        已被删掉的行（读到空）且章节结论与事实库永久不一致；
        #      · 提取收尾 invalidate_export_cache(facts_touched=True)
        #        推进 schemes.facts_updated_at → 正文正在写的章被标「事实已过期」；
        #      · vs 目录生成：目录模板消费 project_facts，同理读到撕裂快照。
        #    判定下沉到 register_task 的 _register_lock 内（TOCTOU 收口）。
        task_id, _conflict = await _register_task_exclusive(
            "facts_generation", project_id, scheme_id, ("outline_generation", "content_generation")
        )
        if _conflict is not None:
            _msg = (
                "本方案正在后台生成目录/正文，需等其完成后再提取事实" "（否则事实落库会与生成过程读到的事实快照不一致）"
            )
            logger.warning(
                "generate_facts 跨类型互斥（注册期二次判定）：scheme_id=%s " "冲突任务类型=%s",
                scheme_id,
                getattr(_conflict, "kind", "?"),
            )
            yield f"data: {json.dumps({'event': 'error', 'task_id': _conflict.task_id, 'message': _msg}, ensure_ascii=False)}\n\n"
            return
        # 供心跳通道回传（_facts_stats_provider 定义在本层之外）
        _facts_task_id[0] = task_id
        _facts_holder["task_id"] = task_id
        pipe_task: asyncio.Task | None = None
        # ✅ BUG 修复（进度回退）：事实提取链路的进度来源有多处——SSE 层
        #    （0.05 加载资料 / 0.08 分段就绪 / 0.09 增量提示）与管线内部
        #    （0.03 正在智能分段 / 0.06 截断告警），两侧口径不一致会发出
        #    比上一条更小的进度（实测 0.08 → 0.03），前端事实提取路径无
        #    单调护栏、直接 setProgress → 进度条肉眼可见「倒退」，观感等同
        #    卡死或任务被重置。目录/正文链路已有 _monotonic_progress 护栏，
        #    此处共用同一口径：所有进度事件统一抬到历史最大值，不可回退。
        _max_pushed = 0.0

        def _sse(obj: dict) -> str:
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

        async def _stage(p: float, m: str) -> str:
            """更新任务进度并返回一条 progress SSE 事件（不可回退）"""
            nonlocal _max_pushed
            _max_pushed = p = _monotonic_progress(_max_pushed, p)
            # 同步给心跳通道：ping 事件据此回传进度（见 _facts_stats_provider）
            _facts_progress[0] = _max_pushed
            await update_progress(task_id, p, m)
            return _sse({"event": "progress", "task_id": task_id, "progress": p, "message": m})

        # ✅ P1 修复（2026-09-27 · 心跳携带运行统计）：全局事实提取单段 AI
        #    调用可达数十秒，期间 event_stream 整体挂起、无法 yield progress，
        #    前端进度条**完全静止**（观感等同卡死）。目录（:4303）与正文
        #    （:6138）链路早已接入 stats_provider，只有 facts 链路是裸调用。
        #    `_facts_stats_provider` 定义在本层之外（见上文），这里只负责
        #    把单调进度同步给它。仅新增 `ping` 事件，不改动任何既有
        #    progress 事件的字段与顺序（前端未监听 ping 时为纯静默增量）。
        #    （注释保留于此以便与 _max_pushed 的单调护栏读在一起。）

        try:
            await wait_resume(task_id)
            if is_stopped(task_id):
                await finish_task(task_id, "stopped", "用户已停止")
                yield _sse({"event": "stopped", "task_id": task_id})
                return

            # 1. 加载项目资料
            yield await _stage(0.05, "正在加载项目资料...")

            # ✅ 修复（2026-09-30 第十四轮 · P0 静默截断）：
            #    本查询此前只取 (file_name, parsed_markdown)，**不读**
            #    parse_truncated / parse_warnings —— 而解析阶段已把「落库字数超限」
            #    与「解析器级截断（PDF 截页 / 表格截行）」写进这两列。于是一份只
            #    被解析了前 N 页的招标文件会被**当成完整依据**参与事实提取，
            #    用户看到「提取完成」却无从得知依据是残缺的（18 项提取链
            #    bid_analysis._list_parsed_documents 早已读这两列，本链路是漏改点）。
            #    现按实际列集合动态拼装（缺列只降级该列，不让读路径失效）。
            parsed_file_names, parsed_bodies, truncated_docs = await _load_facts_source_docs(db, project_id)
            source_files = list(parsed_file_names)
            all_text = "\n\n".join(f"=== {fn} ===\n{bd}" for fn, bd in zip(parsed_file_names, parsed_bodies))
            parsed_count = len(parsed_file_names)

            # ✅ 分步工作流：提取前置是"已解析文档"。
            # 无任何已解析文本时不再静默降级为方案基本信息（会产出全模拟值），
            # 而是明确提示用户先完成 上传保存 → 解析 两步。
            has_any_doc, pending_docs = await _count_docs_pending_parse(db, project_id)
            if not all_text.strip():
                hint = (
                    "项目下没有可提取的资料文本。请先在「全局事实」页上传文件并点击「解析文档」。"
                    if has_any_doc
                    else "项目下没有已上传的资料文档。请先在「全局事实」页上传文件并解析。"
                )
                if pending_docs:
                    hint += f"（当前有 {len(pending_docs)} 份文档待解析）"
                await finish_task(task_id, "failed", hint)
                yield _sse({"event": "error", "task_id": task_id, "message": hint})
                return

            if pending_docs:
                yield _sse(
                    {
                        "event": "warning",
                        "task_id": task_id,
                        "message": (
                            f"有 {len(pending_docs)} 份文档尚未解析，" f"本次提取仅使用已解析的 {parsed_count} 份"
                        ),
                    }
                )

            # ✅ 截断必须显式告知（否则用户把残缺依据当成完整依据）
            if truncated_docs:
                yield _sse(
                    {
                        "event": "warning",
                        "task_id": task_id,
                        "message": (
                            f"⚠️ {len(truncated_docs)} 份文档解析不完整"
                            f"（内容被截断）：{'、'.join(truncated_docs[:5])}"
                            + ("等" if len(truncated_docs) > 5 else "")
                            + "；本次提取仅基于已解析到的部分，"
                            "建议拆分文件后重新上传并重新解析"
                        ),
                    }
                )

            yield await _stage(0.08, f"已加载 {len(source_files)} 份资料（{len(all_text)} 字），准备分段...")

            # 2. 分段提取：pipeline 的分段级进度回调经队列桥接为实时 SSE，
            #    同时支持提取过程中的暂停/停止（旧实现只能等整段跑完才看得到结果）。
            q: asyncio.Queue = asyncio.Queue()

            async def _progress_cb(p: float, m: str):
                await q.put((p, m))

            async def _wait_resume_cb():
                await wait_resume(task_id)

            # ✅ 增量提取：加载已完成段指纹（force 时跳过读取 → 全量重提取）
            # ✅ BUG-1 修复：进度作用域含 scheme_id，避免多方案共享项目时
            #    非首方案命中其它方案的完成段指纹而 all_skipped 早退。
            completed_chunks = set() if force_full else await load_completed_chunks(db, project_id, scheme_id)
            if completed_chunks:
                yield await _stage(
                    0.09, f"增量提取：已记录 {len(completed_chunks)} 段历史成果，" "重复段落将自动跳过..."
                )

            # 知识库补充所需的文本块（对齐易标 runKnowledgeGlobalFactPatches）。
            # 默认开关关闭 → 不查询、不传空串，事实链路行为与引入前一致。
            _facts_knowledge_text = ""
            if settings.facts_knowledge_patch_enabled:
                try:
                    _facts_knowledge_text = await _build_knowledge_text(db, scheme_id)
                except Exception as e:  # noqa: BLE001 - 知识库不可用不应阻断提取
                    logger.warning("加载知识库文本失败（降级为无）: %s", e)
                    _facts_knowledge_text = ""

            pipe_task = asyncio.create_task(
                run_extraction_pipeline(
                    all_text,
                    progress_cb=_progress_cb,
                    should_stop=lambda: is_stopped(task_id),
                    wait_resume_cb=_wait_resume_cb,
                    missing_value_mode=missing_value_mode,
                    completed_chunks=completed_chunks,
                    knowledge_text=_facts_knowledge_text,
                )
            )

            while True:
                if pipe_task.done() and q.empty():
                    break
                if is_stopped(task_id):
                    pipe_task.cancel()
                    try:
                        await pipe_task
                    except BaseException:
                        logger.debug("停止时等待事实提取管线任务退出失败（已忽略 · task=%s）", task_id, exc_info=True)
                    pipe_task = None
                    await finish_task(task_id, "stopped", "用户已停止")
                    yield _sse({"event": "stopped", "task_id": task_id})
                    return
                try:
                    p, m = await asyncio.wait_for(q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                yield await _stage(p, m)

            extraction_result = await pipe_task
            pipe_task = None

            # 3. 完整性保护：零事实时绝不覆盖既有事实
            #    （旧实现会先删库再插入 0 条 → 一次失败提取清空全部历史事实）
            # ✅ 增量提取例外（2026-09-17）：全部段都已提取过（无新增内容）时
            #    属正常成功——既有事实保持上次落库结果，进度照常记录。
            if extraction_result.all_skipped:
                # ✅ R175 续（2026-11-04）：区分「增量跳过（确有历史成果）」与
                #    「全部段处于失败退避窗口（此前从未提取成功）」。后者不得按
                #    completed 计入成功，否则用户既分不清来源，也会把失败当成功。
                _task_status, _event, done_msg, _extra = _facts_all_skipped_payload(extraction_result)
                if _task_status == "completed":
                    await update_progress(task_id, 1.0, done_msg, event="completed")
                await save_extracted_chunks(
                    db, project_id, scheme_id, extraction_result.chunk_hashes_ok, extraction_result.chunk_hashes_all
                )
                await finish_task(task_id, _task_status, done_msg)
                yield _sse({"event": _event, "task_id": task_id, "message": done_msg, **_extra})
                return
            if not extraction_result.groups or extraction_result.total_items == 0:
                warn = "；".join(extraction_result.warnings) or "未提取到任何事实，已保留原有事实"
                await finish_task(task_id, "failed", warn)
                yield _sse({"event": "error", "task_id": task_id, "message": warn})
                return

            # 4. 保存并输出
            yield await _stage(0.96, "正在保存事实...")
            # ✅ T-04：先拿项目级写锁再落库（手工写入走同一把锁）；冲突时抛
            #    FactsWriteConflict，由外层 SSE 异常处理给出任务失败终态，
            #    不再「后写覆盖」用户刚确认/新增的事实。
            _facts_write_lease = await acquire_project_facts_write(db, project_id, holder="ai_extraction")
            try:
                await persist_extraction(extraction_result, db, project_id, scheme_id)
            finally:
                if _facts_write_lease:
                    await release_project_facts_write(db, project_id, _facts_write_lease)
            # ✅ 2026-11-05：事实已落库 → 同步回写来源文档的提取状态，
            #    否则 extract_status 会一直停在 pending（事实表却已有数据）。
            await _mark_docs_extract_status(db, project_id, "success")
            await invalidate_export_cache(db, scheme_id, facts_touched=True)
            # ✅ 增量提取：记录本次完成段指纹并清理失效残留（删除/重解析的文档）
            await save_extracted_chunks(
                db, project_id, scheme_id, extraction_result.chunk_hashes_ok, extraction_result.chunk_hashes_all
            )

            frontend_data = format_for_frontend(extraction_result)

            # 如果有告警，额外推送 warning 事件
            if extraction_result.warnings:
                yield _sse({"event": "warning", "task_id": task_id, "warnings": extraction_result.warnings})

            # ✅ 部分失败时在任务消息里显式标注，页面刷新/断线重挂接后仍可见。
            #    R180（2026-11-05）：部分段未成功时终态降级为 degraded（此前一律
            #    completed，用户无法区分「全部成功」与「部分降级」）；判定与文案
            #    统一由 `_facts_partial_terminal_payload` 产出，_seed 与 SSE 事件同源。
            _terminal_status, _terminal_event, done_msg = _facts_partial_terminal_payload(extraction_result)
            await update_progress(task_id, 1.0, done_msg, event=_terminal_event)
            # ✅ P0 修复（2026-09-27 · 断线重挂成果通道）：写 facts_result checkpoint，
            #    使 `GET /sse/task/{id}` 能回传 segment_stats / cross_conflicts /
            #    warnings —— 与在线 completed 事件同源同字段（前端 applyFactsCompletedEvent
            #    消费的是同一组键，无需为重挂接写第二套解析）。
            #    必须在 finish_task **之前**写：finish_task 会 pop 内存态，
            #    此刻若客户端已断开，后续语句不会执行。
            # ✅ 异常兜底（2026-09-27）：checkpoint 是**锦上添花**的断线重挂通道，
            #    不是业务成果本体（事实已由 persist_extraction 落库）。若写盘失败
            #    抛出，会落进外层 `except Exception` → finish_task(failed) +
            #    yield error，前端 completed 分支不执行、loadFacts() 不刷新 ——
            #    「事实全部提取成功」被展示成「提取失败」，比不写 checkpoint 更糟。
            #    对齐正文链路 :6062-6066 的同款兜底：记 WARNING 后继续。
            try:
                await _save_facts_checkpoint(
                    task_id,
                    {
                        "event": _terminal_event,
                        "message": done_msg,
                        **_facts_checkpoint_payload(frontend_data),
                    },
                )
            except Exception:
                logger.warning(
                    "检查点写入失败（task=%s · 事实提取 · " "成果已落库，不影响本次提取结论）", task_id, exc_info=True
                )
            # ✅ P0 修复（2026-09-27 · 收尾顺序）：旧实现在此先 `yield completed`
            #    再 `finish_task(completed)`。前端收到 completed 立即 break 并关闭
            #    响应体 → 生成器在 yield 处收到 GeneratorExit，**跳过**下一行的
            #    finish_task，直落 finally 的 `has_active_task` 兜底分支，
            #    把一个**已成功**的任务改写成 `stopped`（"客户端断开，任务已终止"）。
            #    后果：事实已全部落库，任务栏却显示「已停止」，断线重挂接拿到
            #    status=stopped 走不到 completed 分支（前端 SchemeWorkbenchPage.tsx:5435），
            #    用户看到"后台任务已停止"而事实其实提取成功。
            #    正确顺序与同文件目录生成（:4201）、正文生成（:5985）、
            #    all_skipped 分支（:6224）保持一致：先 finish_task 落终态，再 yield。
            await finish_task(task_id, _terminal_status, done_msg)
            yield _sse({"event": _terminal_event, "task_id": task_id, "message": done_msg, **frontend_data})
        except asyncio.CancelledError:
            await finish_task(task_id, "stopped", "用户已停止")
            yield _sse({"event": "stopped", "task_id": task_id})
        except Exception as e:
            logger.exception("全局事实提取失败（scheme_id=%s · task=%s）", scheme_id, task_id)
            await finish_task(task_id, "failed", str(e))
            yield _sse({"event": "error", "task_id": task_id, "message": str(e)})
        finally:
            # 客户端断开/生成器关闭时，避免后台提取任务泄漏
            if pipe_task is not None and not pipe_task.done():
                pipe_task.cancel()
            # ✅ 断开兜底：GeneratorExit 落在 yield 处不经过 except，
            # 幂等清理未终态的任务（finish_task 未执行过才会命中）。
            if has_active_task(task_id):
                # ✅ R177（2026-11-05 · 断线收尾 P1）：shield 保证终态落库不被取消打断
                #    （与 bid_analysis.py 断线兜底同一范式）。
                try:
                    await asyncio.shield(finish_task(task_id, "stopped", "客户端断开，任务已终止"))
                except asyncio.CancelledError:
                    logger.info("全局事实断线兜底 finish_task 已 shield（task=%s）", task_id)
                except Exception:
                    logger.warning("全局事实断线兜底 finish_task 失败（task=%s）", task_id, exc_info=True)

    return StreamingResponse(
        with_heartbeat(event_stream(), stats_provider=_facts_stats_provider, on_end=_stream_end_guard(_facts_holder)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- 任务控制 ----------
@router.post("/task/{task_id}/control")
async def task_control(task_id: str, body: dict):
    action = body.get("action", "")
    if action in ("pause", "resume", "stop"):
        ok = request_control(task_id, action)
        if not ok:
            # ✅ 僵尸任务可停（2026-09-17）：request_control 只认**本进程内存态**，
            #    但任务栏数据源（task_registry 表）只要 DB 还是 running/paused 就一直
            #    显示为运行中。多 worker 部署 / 进程异常重启 / 生成流异常退出都可能留下
            #    「DB 仍在跑、内存已无此任务」的行 —— 旧实现此时直接返回"任务不存在"，
            #    用户点停止永远无效，任务栏留下清不掉的僵尸。
            #    这里退化为「DB 态直接落终态」，保证停止按钮对所有可见任务都有效。
            cur = await _stopped_orphan_rows([task_id])
            if cur:
                return {
                    "ok": True,
                    "action": action,
                    "orphaned": True,
                    "message": "任务已无运行实例（可能由外部进程残留），已标记为已停止",
                }
            return {"ok": False, "message": "任务不存在或已结束"}
        _ctl_status = {"pause": "paused", "resume": "running", "stop": "stopped"}[action]
        # ✅ R108（2026-10-18 · 日志审计 P0-2）：控制指令落库重试预算耗尽后
        #    异常此前直穿中间件变成裸 500——前端只弹「操作失败」，用户不知道
        #    指令其实没写进库（内存 Event 已置位，DB 仍 running，轮询风暴下
        #    表现为「点了停止停不掉」）。现按异常实型分流：瞬态锁类 → 503 +
        #    Retry-After（可行动语义：稍后必成，前端 tasksApi.control 自动
        #    重试一次）；其余异常原样上抛（真故障不粉饰）。
        try:
            from app.services.ai.task_registry import set_task_status

            await set_task_status(task_id, _ctl_status)
        except Exception as e:
            if is_retryable_db_error(e):
                logger.warning(
                    "任务 %s 控制指令（%s）落库重试耗尽（DB 写锁竞争），" "返回 503 引导客户端重试: %s",
                    task_id[:8],
                    action,
                    str(e)[:200],
                )
                raise HTTPException(
                    503, "数据库写入繁忙，控制指令暂未持久化，请稍后重试", headers={"Retry-After": "2"}
                ) from e
            raise
        return {"ok": True, "action": action}
    return {"ok": False, "message": "未知操作"}


async def _stopped_orphan_rows(task_ids: list[str]) -> int:
    """把「DB 仍 running/paused 但本进程无内存态」的任务批量落终态 stopped。

    返回实际迁移的行数。仅改 DB（内存态本就不存在），并触发一次活动广播，
    使侧边栏任务窗口立刻反映结果。
    """
    if not task_ids:
        return 0
    conn = await get_conn()
    try:
        placeholders = ",".join("?" * len(task_ids))
        cur = await conn.execute(
            f"UPDATE task_registry SET status='stopped', message=?, updated_at=? "
            f"WHERE id IN ({placeholders}) AND status IN ('running','paused')",
            ["无运行实例，已终止", datetime.now().isoformat(), *task_ids],
        )
        await conn.commit()
        changed = safe_rowcount(cur, what="孤儿后台任务置 stopped")
    except Exception as e:
        logger.warning("_stopped_orphan_rows 失败（已忽略）: %s", e)
        return 0
    if changed:
        _ab.notify()
    return changed


# ---------- 任务状态查询（SSE 断线/页面刷新后重新挂接） ----------

_TERMINAL_STATUSES = {"completed", "degraded", "failed", "stopped"}

#: task_type → checkpoint kind → 允许回传给前端的字段白名单
#  （白名单而非整体透传：checkpoint 里可能含内部字段，且避免未来新增字段泄漏）
_CHECKPOINT_KINDS = {
    "outline_generation": (
        "outline_result",
        # ✅ event / partial 必须回传（2026-09-25 补齐）：
        #    断线重挂接时前端要靠 event 区分「用户主动停止
        #    （stopped，可继续补生成）」与「异常中断
        #    （error，需整轮重来）」，靠 partial 区分
        #    「完整目录」与「只生成了一部分的目录」。缺这两个
        #    字段时，前端只能笼统提示"任务已结束"，用户不知道
        #    该不该保存已生成的那部分 —— 而部分成果是有效的。
        # ✅ R179 N1（2026-11-05）：`auto_confirm{ready,blockers}` 入白名单，
        #    使断线/刷新重挂后的两条恢复路径与在线 completed 同构（加法式）。
        ("event", "partial", "outline", "review", "failed_chapters", "failed_count", "auto_confirm"),
    ),
    "content_generation": (
        "content_result",
        # ✅ run_words / over_count 必须入白名单（2026-09-23）：
        #    断线重挂后前端要显示「本次生成 X 字 / 超字数 N 章」，
        #    缺这两项时重挂页面上的统计全为 0，与在线看到的不一致。
        (
            "event",
            "message",
            "done",
            "total",
            "failed_count",
            "failed_sections",
            "words",
            "run_words",
            "over_count",
            "word_count",
            # ✅ R66（2026-10-11）：字数审计汇总入白名单，
            #    断线重挂/审核预检才能读到逐章偏差明细。
            "word_audit",
            # ✅ R77b（2026-10-15）：事实冲突定点修复汇总入
            #    白名单（断线重挂与在线终态载荷同构）。
            "fact_autofix_summary",
        ),
    ),
    # ✅ P0 修复（2026-09-27）：facts_generation 此前缺席，导致全局事实提取
    #    断线/刷新后成果全丢（前端重挂接拿不到 segment_stats / cross_conflicts）。
    #    字段集与写入侧的 `_FACTS_CHECKPOINT_FIELDS` 同源，两侧不会分叉。
    "facts_generation": ("facts_result", ("event", "message", *_FACTS_CHECKPOINT_FIELDS)),
}


def _attach_checkpoint_result(row, result: dict) -> None:
    """把 checkpoint_json 里的任务成果附到 task_status 响应（按 task_type 白名单）。

    ✅ 目录成果恢复：断线/刷新后 completed 的目录仅存在于 SSE 事件中，
    通过 checkpoint 回传 outline 载荷，前端可走与在线一致的保存确认流程。
    ✅ 正文成果恢复（2026-09-16 增强）：正文已逐章落库，但「哪些章失败、为什么」
    只在 SSE 里出现过；断线重挂接时由 content_result 回传失败明细与累计字数。
    """
    spec = _CHECKPOINT_KINDS.get(row["task_type"])
    if not spec or not row["checkpoint_json"]:
        return
    kind, fields = spec
    try:
        ckpt = json.loads(row["checkpoint_json"])
    except (TypeError, ValueError):
        return
    if not isinstance(ckpt, dict) or ckpt.get("kind") != kind:
        return
    result[kind] = {k: v for k, v in ckpt.items() if k in fields}


@router.get("/task/{task_id}")
async def task_status(task_id: str):
    """查询单个任务状态。

    内存优先（进行中任务含实时进度），回退 DB task_registry 表
    （历史任务 / 进程重启后的遗留任务）。
    """
    state = _tr._tasks.get(task_id)
    if state:
        # ✅ 修复：内存分支补齐 message/updated_at —— 旧实现缺这两项，而前端
        #    pollTaskUntilTerminal 读 data.message 展示终态原因；任务刚进内存终态、
        #    finish_task 尚未 pop 的窗口内，断线重挂会拿不到 message（与 DB 分支口径不一）。
        res = {
            "task_id": task_id,
            "task_type": state.get("type", ""),
            "status": state.get("status", "running"),
            "progress": state.get("progress", 0.0),
            "message": state.get("message", ""),
            "scheme_id": state.get("scheme_id", ""),
            "live": True,
        }
        # ✅ 成果补挂（2026-09-20 · 断线重挂空窗修复）：finish_task 已把状态
        #    置为终态、但尚未 pop 内存条目时，重挂接走的是**内存分支** —— 旧实现
        #    直接 return，读不到 DB checkpoint，于是前端拿到 status=stopped
        #    却没有 outline_result，误判"无成果"并整轮重来（跑了几分钟白跑）。
        #    仅终态才补挂：进行中任务读 DB 属无谓开销（且此时本就无成果）。
        if res["status"] in _TERMINAL_STATUSES:
            try:
                conn = await get_read_conn()
            except Exception:
                conn = None
            if conn is not None:
                try:
                    cur = await conn.execute(
                        "SELECT task_type, checkpoint_json FROM task_registry " "WHERE id=?", (task_id,)
                    )
                    row = await cur.fetchone()
                    if row:
                        _attach_checkpoint_result(row, res)
                except Exception:
                    logger.debug("内存终态补挂 checkpoint 失败（忽略）", exc_info=True)
                finally:
                    try:
                        await release_read_conn(conn)
                    except Exception:
                        logger.debug("终态补挂后读取连接归还真失败（task=%s）", task_id, exc_info=True)
        return res
    conn = await get_read_conn()
    try:
        cur = await conn.execute(
            "SELECT id, task_type, status, progress, message, scheme_id, updated_at, checkpoint_json"
            " FROM task_registry WHERE id=?",
            (task_id,),
        )
        row = await cur.fetchone()
    finally:
        await release_read_conn(conn)
    if not row:
        raise HTTPException(404, "任务不存在")
    result = {
        "task_id": row["id"],
        "task_type": row["task_type"],
        "status": row["status"],
        "progress": row["progress"] or 0.0,
        "message": row["message"] or "",
        "scheme_id": row["scheme_id"] or "",
        "updated_at": row["updated_at"],
        "live": False,
    }
    # ✅ 成果恢复：断线/刷新后终态任务的成果（目录树 / 正文失败明细）仅在 SSE 事件中，
    #    通过 checkpoint 回传，前端可走与在线一致的处理流程。
    _attach_checkpoint_result(row, result)
    return result


@router.get("/tasks")
async def list_tasks(scheme_id: str = "", limit: int = 20):
    """列出最近任务（可按方案过滤），前端刷新后据此重新挂接进行中任务。"""
    limit = max(1, min(limit, 100))
    conn = await get_read_conn()
    try:
        if scheme_id:
            cur = await conn.execute(
                "SELECT id, task_type, status, progress, message, updated_at"
                " FROM task_registry WHERE scheme_id=? ORDER BY created_at DESC LIMIT ?",
                (scheme_id, limit),
            )
        else:
            cur = await conn.execute(
                "SELECT id, task_type, status, progress, message, scheme_id, updated_at"
                " FROM task_registry ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await release_read_conn(conn)
    # 合并内存实时状态（DB 的 status/progress 落后于内存）
    for t in rows:
        st = _tr._tasks.get(t["id"])
        if st:
            t["status"] = st.get("status", t["status"])
            t["progress"] = st.get("progress", t["progress"])
            t["live"] = True
        else:
            t["live"] = False
    return {"tasks": rows}
