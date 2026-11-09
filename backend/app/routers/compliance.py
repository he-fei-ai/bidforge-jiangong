"""规范符合性检查 + 专家论证预检 + 程序化预检 + 就绪度评分路由

演进说明
--------
本模块原只有「AI 语义检查」一条链路，五个检查项各自出结论、互不关联，
用户无法回答"这份方案现在能不能交付"。现补齐为三层：

1. **程序化预检**（``/preflight``）—— 确定性规则，离线秒级，覆盖空章节、
   废止标准、控制字符、查重、计算书缺失等硬伤（PRD 遗漏项在此补齐）；
2. **AI 语义检查**（``/check`` ``/expert-review`` ``/consistency-audit``）——
   覆盖需要语义理解的部分；
3. **就绪度聚合**（``/overview``）—— 把 1、2 的结果合并去重后，按六个维度
   加权评分，给出 A/B/C/D 等级与是否放行的明确结论。

规则定义统一取自 ``services/audit_rules.py``（唯一事实源），
不再在本文件内联清单（原 ``EXPERT_CHECK_ITEMS`` 与提示词文案重复维护，存在分叉）。
"""

# ruff: noqa: I001 —— R13 契约：review_db 必须单独一行，护栏 test_review_db_is_imported 锁
import asyncio
import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response

from app.config import settings
from app.db import get_db
from app.models import ComplianceCheckIn, ExpertReviewIn

# ✅ R13 判空单一出口（2026-10-06）：本模块所有 db.execute 走 review_db，
#    读失败 503 / 写没生效 503。静态护栏
#    tests/test_review_r13_closeout_20261006.py 禁止退回裸调用。
# noqa: I001 —— R13 契约：review_db 必须单独一行，护栏 test_review_db_is_imported 锁
from app.services import review_autofix
from app.services import review_db  # noqa: I001
from app.services.ai.json_response import collect_json_response
from app.services.ai.prompts._registry import render

# ✅ D-41（R176）：批次审计行复用 provider_factory 的三级降级 INSERT
#    （旧库缺 scene / config_id / base_url 列时逐级退让，不新增表/列）
from app.services.ai.provider_factory import (
    _AUDIT_INSERT_FULL,
    _AUDIT_INSERT_LEGACY,
    _AUDIT_INSERT_NO_IDENTITY,
    _AUDIT_INSERT_RICH,
    _error_class,
)
from app.services.audit_rules import (
    EXPERT_CHECK_ITEMS,
    RULE_VERSION,
    ai_rules,
    dimension_catalog,
    expert_items,
    get_rule,
    rule_catalog,
    validate_rule_registry,
)
from app.services.audit_scoring import (
    ai_results_to_findings,
    expert_result_to_findings,
    merge_findings,
    normalize_severity_text,
    score_findings,
)
from app.services.content_utils import order_sections_dfs

# ✅ B-19（R176）：长文本阈值唯一事实源（/check、/consistency-audit 与扫描共用）
from app.services.numeric_topics import (
    LONG_TEXT_CALL_CAP,
    LONG_TEXT_SECTION_CAP,
)

# ✅ R81（2026-10-15）：requirement 口径前缀的**单一事实源**在 facts_builder
#    （正文/目录注入渲染同款）。本文件只 import 不自建副本——「同一判据多处
#    实现」是本仓最高频反模式（AGENTS.md §4.3/§4.7）。
#    与正文注入的差异仅在前缀拼入位置（那里拼在 per_fact 截断后的内容尾部，
#    这里拼在 unit 之后、安全关键标记之前），语义逐字相同。
from app.services.facts_builder import _REQUIREMENT_KIND_PREFIX

# ✅ 2026-10-03（全局事实桥接）：预检消费「已确认全局事实」（SAF-08 事实反哺）。
#    门控/作用域口径一律取 facts_extractor 唯一出口，不在本文件重写 SQL。
from app.services.facts_extractor import (
    FACT_SCOPE_SHARED_FRAGMENT,
    get_facts_inject_where,
    load_resolved_facts_for_scope,
    resolve_scheme_project_id,
)
from app.services.preflight_engine import (
    PreflightContext,
    preflight_stats,
    run_preflight,
)


# ✅ R178（B-4 · 2026-11-05）：预检是**纯 CPU 的同步计算**（8 个 checker +
#    CON-01 的 11 条数值正则 + 整章查重），此前在**事件循环内直跑**。
#    大方案（数百章）时会把 SSE 心跳与所有并发请求一起卡住 —— 而预检端点
#    自称「确定性规则，秒级返回」，实测并不秒级（同步阻塞整个事件循环）。
#    现整体挪到工作线程执行，**判定口径逐字不变**（同一 ctx、同一函数）。
def _preflight_findings_blocking(ctx: PreflightContext) -> list:
    """跑完全部程序化规则（阻塞调用，须在 ``asyncio.to_thread`` 内执行）。"""
    return run_preflight(ctx)


from app.services.standards_registry import (
    STANDARD_DB_CHECKED_AT,
    STANDARD_DB_VERSION,
)

router = APIRouter(prefix="/api/v1/compliance", tags=["compliance"])

# ✅ BUG 修复（2026-09-21）：本模块有 4 处 `except` 分支调用
#    `logger.warning(...)`（把静默 `pass` 改为可观测日志的那次修复只写了调用、
#    没有定义 logger）→ 一旦这些容错分支被触发（评分非数值 / 读取 AI 结果失败），
#    会抛 `NameError: name 'logger' is not defined`，把一次**可容忍的降级**
#    升级成接口 500。此处补齐模块 logger。
logger = logging.getLogger("compliance")

# ---------------------------------------------------------------------------
# 请求 / 输出上限（token 保护 + 响应可预测）
# ---------------------------------------------------------------------------
#: 单章节送 AI 的最大字符数（超长章节切片并显式标注续段，禁止静默丢弃）
#: ✅ B-19（R176）：改为引用 numeric_topics 的统一阈值（旧值 4000 只覆盖 99.85%
#:    存量章节；统一为 6000 后存量 100% 章节单次送审，不再产生续片）。
SECTION_CONTENT_CAP = LONG_TEXT_SECTION_CAP
#: /check 单批内容字符上限（一批 = 一次 AI 调用）
AI_CONTENT_HARD_CAP = LONG_TEXT_CALL_CAP
#: ✅ D-41（R176）预算闸门：单次审核最多执行的批次数与字符总量。
#: ✅ 2026-11-04 配置化：数值改由 ``config.py`` 的 ``compliance_max_batches`` /
#:    ``compliance_max_chars`` 提供（默认 40 / 240000，与 R176 逐字相同）。
#:    读取统一走 :func:`_batch_budget`，不在模块常量上再抄一份。
#: 超出时审到上限并返回 partial=true + 未审章节清单（已审部分不白跑）。
_BUDGET_WARNED = False
#: ai_audit_logs 的 action 值：新增 action 必须同步
#: ``tests/test_audit_budget_config_and_action_enum_20261104.py`` 的白名单，否则测试红。
COMPLIANCE_BATCH_ACTION = "compliance_batch"
#: /expert-review 允许的 outline 节点上限（超过视为数据异常，明确 400 而非静默截断）
EXPERT_OUTLINE_MAX_NODES = 300
#: /expert-review 允许的附件数量与单条长度上限（防 token 爆炸）
EXPERT_ATTACH_MAX_COUNT = 8
EXPERT_ATTACH_MAX_LEN = 500
#: /results 默认 & 最大返回条数（此前无 LIMIT，历史累积后一次返回可能数万行）
RESULTS_DEFAULT_LIMIT = 50
RESULTS_MAX_LIMIT = 500


# ---------------------------------------------------------------------------
# 规则目录（前端「规则说明」与 AI 提示词共用同一份）
# ---------------------------------------------------------------------------
@router.get("/rules")
async def list_rules():
    """全量审核规则目录（含行业依据），供前端展示与用户自查。

    ✅ R95（2026-10-17，R90 遗留①）：注册表一致性自检接入生产读路径。
    此前 ``validate_rule_registry`` 只在 CI 测试时触发 —— 规则表与
    导出映射 / 引擎产出集一旦在生产分叉（改规则不发版测试），用户拿到的
    是静默塌陷的 title/basis/dimension。本端点每次被调用即做双向校验：
    健康时 ``registry_problems`` 为空列表（前端可忽略，响应体加法式）；
    漂移时记 ERROR 日志并把问题原文透出，「规则说明」抽屉自此成为
    分叉可观测点。懒导入 export 避免 router 间循环依赖。
    """
    from app.routers.export import _EXPORT_ISSUE_RULE_MAP
    from app.services.audit_rules import _PROGRAM_EMITTED_RULE_IDS

    problems = validate_rule_registry(
        mapping=_EXPORT_ISSUE_RULE_MAP,
        emitted_rule_ids=_PROGRAM_EMITTED_RULE_IDS,
    )
    if problems:
        logger.error("审核规则注册表自检发现 %d 项漂移: %s", len(problems), "; ".join(problems[:10]))
    return {
        "rule_version": RULE_VERSION,
        "items": rule_catalog(),
        "dimensions": dimension_catalog(),
        "standard_db_version": STANDARD_DB_VERSION,
        "standard_db_checked_at": STANDARD_DB_CHECKED_AT,
        "registry_problems": problems,
    }


@router.get("/checkpoints")
async def list_checkpoint_constraints():
    """「检查点 → 生成约束 → 实现方式」映射表（唯一事实源）。

    ✅ 2026-10-02（检查点前置）：审核与预检检查点不再只是**事后**判据 ——
    本表把它们逐条映射为正文生成侧的约束与实现方式，供前端展示
    「这条审核要求是怎么在生成时被满足的」，让质量门槛可见、可追溯。

    返回体：
    - ``items``：映射条目（group/checkpoint/rule_ids/constraint/implementation/
      channel/chapter_key）；
    - ``groups`` / ``channels``：分组与通道的封闭枚举（前端下拉用）；
    - ``by_group`` / ``by_channel``：各分组 / 通道的条目数；
    - ``anchor_problems``：**非空即说明映射表与审核规则注册表已分叉**
      （审核规则废弃或编号漂移），前端应据此告警而非静默展示。
    """
    from app.services.content_checkpoint import (
        CHECKPOINT_CHANNELS,
        CHECKPOINT_GROUPS,
        checkpoint_constraint_map,
        validate_constraint_map,
    )

    items = checkpoint_constraint_map()
    by_group: dict = {}
    by_channel: dict = {}
    for it in items:
        by_group[it["group"]] = by_group.get(it["group"], 0) + 1
        by_channel[it["channel"]] = by_channel.get(it["channel"], 0) + 1
    return {
        "rule_version": RULE_VERSION,
        "groups": [{"key": k, "label": v} for k, v in CHECKPOINT_GROUPS],
        "channels": list(CHECKPOINT_CHANNELS),
        "items": items,
        "by_group": by_group,
        "by_channel": by_channel,
        "anchor_problems": validate_constraint_map(),
    }


def _apply_deprecation_headers(
    response: Response | None,
    *,
    replacement: str,
    sunset: str = "2026-12-31T00:00:00Z",
    deprecation: str = "true",
) -> None:
    """给 deprecated 端点统一注入标准弃用信号（向后兼容：不改变响应体）。

    - ``Deprecation``：``true`` 或 RFC 8594 风格的时间戳，客户端可判断弃用起始。
    - ``Sunset``：预计下线日期（RFC 8594），便于客户端排期。
    - ``Link: <…>; rel="deprecation"``：指向替代端点，前端/脚本可据此迁移。

    默认行为向后兼容：老客户端不识别这些头仍然能拿到原响应体；新客户端
    （SDK / CI / 日志采集器）可据此告警。

    容错：既有单测常把 router 函数**当作普通 async 函数**直接调用（无
    FastAPI 依赖注入），此时可能不传 Response 或误传其他对象（如 sqlite
    Connection）。此处以 ``hasattr(..., "headers")`` 判定，只写入真正
    可用的响应对象，其他情况静默返回，不改变业务返回体。
    """
    if response is None or not hasattr(response, "headers"):
        return
    response.headers["Deprecation"] = deprecation
    response.headers["Sunset"] = sunset
    response.headers["Link"] = f'<{replacement}>; rel="deprecation"'


# @deprecated 孤儿 API：本前端无调用方（UI 已改用 /overview 的 dimensions）；
# 按向后兼容原则保留，大版本评估清理。
@router.get("/dimensions", deprecated=True)
async def list_dimensions(response: Response):
    _apply_deprecation_headers(
        response,
        replacement="/api/v1/compliance/overview/{scheme_id}",
    )
    return {"items": dimension_catalog(), "rule_version": RULE_VERSION}


@router.get("/expert-review/items")
async def get_expert_items():
    """危大工程专家论证必要项（每项绑定规则 ID 与行业依据）。

    ``items`` 保持原有的字符串数组形态，兼容既有前端调用。
    """
    return {"items": EXPERT_CHECK_ITEMS, "detail": expert_items()}


# ---------------------------------------------------------------------------
# ✅ 2026-10-03（AI 语义检查侧消费事实）：/check 与 /expert-review 两条 AI 链路
#    此前完全不读 global_facts（预检/一致性审计已有事实通道，唯此二条断链）。
#    现统一经跨模块只读桥接 load_resolved_facts_for_scope 装配（与预检/正文/
#    导出同一 fail-closed 门控，不在本文件重写 SQL），格式化为提示词文本注入
#    {global_facts}；无事实/桥接失败一律降级为「（无）」，不阻断 AI 检查。
# ---------------------------------------------------------------------------
#: 事实文本总量上限（token 保护，与 SECTION_CONTENT_CAP 同思路）
#: ✅ 2026-10-06（R47 债-1）：数值搬入 ``services/ai/prompts/_limits.py`` 单一事实源，
#:    本模块仍以同名可读，下游消费代码零改动。
#: 单条事实值的截断长度（value 兜底取整段 content 时防长文本撑爆）
from app.services.ai.prompts._limits import (  # noqa: E402
    FACT_PROMPT_VALUE_CAP as _FACT_PROMPT_VALUE_CAP,
)
from app.services.ai.prompts._limits import FACTS_PROMPT_CAP  # noqa: E402

#: 安全关键标记：预算截断下也必须**完整**保留（见 _join_with_budget）
SAFETY_MARK = "（安全关键）"


def _join_with_budget(lines: list[str], budget: int, suffixes: list[str] | None = None) -> str:
    """按**长度比例**分配预算后逐条截断（**不丢条**），而非头部优先切片。

    ✅ R38（2026-10-03 · P1-e）：本模块原实现是 ``"\\n".join(lines)[:budget]``
    —— 头部优先硬切片。事实条数一多，第 N 条之后**整条消失**，模型看到的是
    「只有关键前半段事实」的错觉，进而在正文里补出并不存在的【待补充】。
    仓库自己在 ``sse_handlers._allocate_char_budgets`` 的注释里记录过同一个
    教训（「20 个提取项里第 6 项之后整段消失 → 后面的项目参数对目录生成完全
    不可见」），且 ``_render_facts_text`` 早已改用按比例分配。本函数是
    2026-10-03 新加的，复制了**已被否决**的旧写法 —— 于是同名变量
    ``{global_facts}`` 在一致性链走按比例分配、在 ``/check`` 与
    ``/expert-review`` 走头部切片，方向相反。现统一到同一实现。

    ``suffixes``：每条**必须完整保留**的尾部标记（如「（安全关键）」）。
    按比例分配会把这截后缀切碎（实测切成 ``（安``）—— 模型既读不到"安全关键"三个字、
    还会读到半个括号。故截断后若后缀未完整保留则整段补回，允许极小的总量超支。
    传 ``None`` 时行为与引入该参数前**逐字一致**（既有调用方零影响）。

    降级口径：``_allocate_char_budgets`` 导入失败时退回旧的头部切片（与修复前
    逐字一致），保证本模块在任何加载顺序下都不会因此抛异常。
    """
    total = sum(len(x) for x in lines) + max(0, len(lines) - 1)  # 含换行
    if total <= budget:
        return "\n".join(lines)
    try:
        # ✅ 2026-10-06（R47 债-2）：分配器下沉到 ``prompt_governance``，
        #    本路由器不再反向 import ``sse_handlers`` 的私有函数。
        from app.services.prompt_governance import allocate_char_budgets
    except Exception:  # noqa: BLE001 - 加载顺序异常不得阻断 AI 检查
        logger.warning("事实预算分配器不可用，退回头部截断（可能丢失尾部事实）")
        return _keep_suffixes("\n".join(lines)[:budget], suffixes)
    quotas = allocate_char_budgets([len(x) for x in lines], budget)
    # ✅ 遗留收口（2026-10-03 · R38）：消费侧超支纠偏。分配器的「每项保底
    #    min_chars」面向少量小节（n≈20）设计，保底量是等比份额的 2 倍 ——
    #    事实条数一大（n×保底 > budget）quotas 总和可达 2×budget，旧实现
    #    逐条按 quota 截完后总长 6199 > CAP 3000，「总量封顶」承诺被破坏。
    #    本函数的语义是「不丢条 且 总量 ≤ budget」：仅当按 quota 截完后
    #    仍超支才介入（均匀硬上限，扣除换行后按条数等分），正常少量条目
    #    场景保留分配器的不等比份额不受影响；quota=0 也至少留 1 字代表内容。
    kept_lens = [min(int(q or 0), len(x)) for q, x in zip(quotas, lines)]
    if sum(kept_lens) + len(lines) - 1 > budget:
        cap = max(1, (budget - (len(lines) - 1)) // len(lines))
        kept_lens = [max(1, min(k, cap)) for k in kept_lens]
    out: list[str] = []
    for i, (line, q) in enumerate(zip(lines, kept_lens)):
        kept = line[:q] if 0 < q < len(line) else line
        mark = suffixes[i] if suffixes and i < len(suffixes) else ""
        if mark and mark not in kept:
            kept = _drop_partial_suffix(kept, mark) + mark
        out.append(kept)
    return "\n".join(out)


def _drop_partial_suffix(text: str, mark: str) -> str:
    """去掉末尾那段「被切碎的标记前缀」。

    按比例分配把 ``（安全关键）`` 切成 ``（安`` 时，只 ``rstrip("（(")``
    不够 —— 括号后面还跟着「安」。这里按「末尾与 mark 前缀重合的最长长度」
    精确裁掉残段，避免拼出 ``（安（安全关键）`` 这种双截断。
    """
    if not mark:
        return text
    for n in range(min(len(text), len(mark) - 1), 0, -1):
        if text.endswith(mark[:n]):
            return text[: len(text) - n]
    return text


def _keep_suffixes(text: str, suffixes: "list[str] | None") -> str:
    """头部切片降级路径的后缀抢救（供 :func:`_join_with_budget` 的 except 分支用）。

    切片可能把 ``（安全关键）`` 切成 ``（安``；这里把残缺后缀整体补回，
    并清掉切片残留的半截，避免模型读到半个词。
    """
    if not suffixes:
        return text
    out = text
    for mark in {s for s in suffixes if s}:
        if mark not in out:
            out = _drop_partial_suffix(out, mark) + mark
    return out


def _facts_prompt_text(facts: list[dict] | None) -> str:
    """把桥接返回的事实清单格式化为提示词文本（空 → 「（无）」）。

    ✅ R81（2026-10-15）：``value_kind='requirement'``（招标/规范要求原文
    口径）的行附加口径前缀（与正文注入渲染同一常量）。缺该字段的历史
    行/fact 行/'' 行渲染逐字不变——行为不变性锚由护栏锁定。
    """
    if not facts:
        return "（无）"
    lines: list[str] = []
    suffixes: list[str] = []
    for f in facts:
        name = str((f or {}).get("name") or "").strip()
        if not name:
            continue
        val = str((f or {}).get("value") or "").strip()
        if len(val) > _FACT_PROMPT_VALUE_CAP:
            val = val[:_FACT_PROMPT_VALUE_CAP] + "…"
        unit = str((f or {}).get("value_unit") or "").strip()
        line = f"- {name}：{val or '（值缺失）'}"
        if unit and unit not in val:
            line += f" {unit}"
        # ✅ R81：要求口径行显式标注「不得当作本方案既定值」——否则
        #    /check 第 6 条「以事实为唯一权威值」会把招标要求数字当成
        #    方案采用值，正文写本方案控制值反被判矛盾（误报 + 错误建议）。
        if str((f or {}).get("value_kind") or "") == "requirement":
            line += _REQUIREMENT_KIND_PREFIX
        mark = ""
        if f.get("is_safety_critical"):
            line += SAFETY_MARK
            mark = SAFETY_MARK
        lines.append(line)
        suffixes.append(mark)
    if not lines:
        return "（无）"
    return _join_with_budget(lines, FACTS_PROMPT_CAP, suffixes)


async def _load_facts_prompt_text(db, scheme_id: str) -> str:
    """装配已确认事实文本（fail-soft：任何异常降级为「（无）」）。"""
    try:
        facts = await load_resolved_facts_for_scope(db, scheme_id=scheme_id)
        return _facts_prompt_text(facts)
    except Exception as e:  # 事实缺失只降级，不阻断 AI 检查
        logger.warning("装配 AI 检查事实文本失败（降级为无）: %s", e)
        return "（无）"


# ---------------------------------------------------------------------------
# ✅ D-41 / B-19（R176）：章节分片 + 装箱分批（/check 与 /consistency-audit 共用）
# ---------------------------------------------------------------------------
def _build_content_batches(sections: list) -> tuple[list[list[dict]], str]:
    """把章节切「片」再按单批上限装箱，返回 ``(批次列表, 全量正文)``。

    - 单章 > :data:`SECTION_CONTENT_CAP`：切片，续段显式标注（不丢字，AI 可感知）；
    - 每批 = 一次 AI 调用，块长累计 ≤ :data:`AI_CONTENT_HARD_CAP`
      （首批允许单块超限，否则单个超长章会被无限细分）。
    """
    _chunks: list[dict] = []
    for _sec in sections:
        _t, _c = _sec["title"], _sec["content"]
        _subs = [_c[i : i + SECTION_CONTENT_CAP] for i in range(0, len(_c), SECTION_CONTENT_CAP)] or [""]
        for _k, _sub in enumerate(_subs):
            _head = f"### {_t}"
            if len(_subs) > 1:
                _head += f"（续 {_k + 1}/{len(_subs)}）"
            _chunks.append({"section_id": _sec["id"], "block": f"{_head}\n{_sub}"})

    _batches: list[list[dict]] = []
    _cur: list[dict] = []
    _cur_len = 0
    for _ch in _chunks:
        _blen = len(_ch["block"]) + (2 if _cur else 0)  # 与 "\n\n".join 分隔符一致
        if _cur and _cur_len + _blen > AI_CONTENT_HARD_CAP:
            _batches.append(_cur)
            _cur, _cur_len, _blen = [], 0, len(_ch["block"])
        _cur.append(_ch)
        _cur_len += _blen
    _batches.append(_cur)  # 空目录也保留一次调用（与原实现一致）
    return _batches, "\n\n".join(_ch["block"] for _ch in _chunks)


def _batch_text(batch: list) -> str:
    """单批正文（与装箱时的分隔符口径一致）。"""
    return "\n\n".join(_ch["block"] for _ch in batch)


def _batch_budget() -> tuple[int, int]:
    """读取分批送审预算 ``(最大批次数, 最大字符数)``（配置项单一事实源）。

    ✅ 默认值在 ``config.py``（40 / 240000），未设置时与 R176 行为逐字相同。
    ``<=0`` 视为该维度无上限（仓库「0 = 不限」惯例），并**只记一次** WARNING：
    选「无上限 + 告警」而非拒绝启动 —— 预算是成本约束，不是正确性依赖，
    调参失误不应把整个服务拖死（与 fail-soft 原则相悖）。
    """
    global _BUDGET_WARNED
    _mb = int(getattr(settings, "compliance_max_batches", 0) or 0)
    _mc = int(getattr(settings, "compliance_max_chars", 0) or 0)
    if (_mb <= 0 or _mc <= 0) and not _BUDGET_WARNED:
        _BUDGET_WARNED = True
        logger.warning(
            "compliance: 分批送审预算被设为无上限（compliance_max_batches=%d,"
            " compliance_max_chars=%d；<=0 表示不限），单次审核成本可能随目录"
            "长度线性增长，请确认这是预期配置",
            _mb,
            _mc,
        )
    return _mb, _mc


def _apply_batch_budget(batches: list, limits: tuple[int, int] | None = None) -> tuple[list, list]:
    """✅ D-41 预算闸门：按「最大批次数 / 最大字符数」截取可执行批次。

    至少保留 1 批（单个批次本身超预算时也执行，否则长方案会得到空结果）。
    返回 ``(可执行批次, 被跳过的批次)``；被跳过批次用于生成未审章节清单，
    **不允许静默丢弃**。``<=0`` 的维度不设上限。
    """
    _max_batches, _max_chars = limits if limits is not None else _batch_budget()
    _kept: list = []
    _chars = 0
    for _b in batches:
        _blen = len(_batch_text(_b))
        if _max_batches > 0 and len(_kept) >= _max_batches:
            break
        if _max_chars > 0 and _kept and _chars + _blen > _max_chars:
            break
        _kept.append(_b)
        _chars += _blen
    return _kept, batches[len(_kept) :]


def _unchecked_section_list(kept: list, skipped: list) -> list:
    """把预算闸门跳过的批次转成「未审 / 部分审」章节清单（可见性契约）。"""
    _checked_ids = {_ch["section_id"] for _b in kept for _ch in _b}
    out: dict = {}
    for _b in skipped:
        for _ch in _b:
            _sid = _ch["section_id"]
            _entry = out.get(_sid)
            if _entry is None:
                _head = _ch["block"].split("\n", 1)[0]
                _title = _head[4:] if _head.startswith("### ") else _head
                _entry = {
                    "section_id": _sid,
                    "section_title": _title.rsplit("（续", 1)[0],
                    "status": ("partially_checked" if _sid in _checked_ids else "not_checked"),
                    "unchecked_chars": 0,
                }
                out[_sid] = _entry
            _entry["unchecked_chars"] += len(_ch["block"])
    return list(out.values())


async def _record_batch_audit(
    db,
    *,
    index: int,
    total: int,
    chars: int,
    duration: float,
    success: bool = True,
    note: str = "",
    error_class: str = "",
) -> None:
    """✅ D-41 可观测：把「第几批 / 共几批 / 本批字符数」写入 ai_audit_logs。

    不新增表、不新增列 —— 复用已有 ``action`` / ``error`` / ``scene`` 字段：
    ``action='compliance_batch'``、``error='batch=1/23 chars=5980 [note]'``。
    ``/ai/stats`` 的 by_provider / by_scene / by_error / summary 均过滤
    ``action='chat'``，故不会污染这些统计；仅 by_action 会多出该 action。

    ✅ R180 收尾（2026-11-05）：``ai_audit_logs`` 已扩到 16 列（+error_class /
    attempt_count / attempts_json），本函数旧实现只按 13 列写 → 失败行的
    ``error_class`` 恒为空，统计归 ``unknown``。现优先走 RICH 16 列：
    失败行按 ``error`` 原文分类（与 provider_factory 同一分类函数），
    成功行留空；旧库缺列时仍按 16→13→11→9 逐级退让，兼容性不变。
    """
    _err = f"batch={index}/{total} chars={chars}"
    if note:
        _err += f" {note}"
    # 失败行分类：显式传入优先，否则按错误原文归类（成功行留空）
    _cls = error_class or ("" if success else _error_class(_err))
    _row = (
        str(uuid.uuid4()),
        COMPLIANCE_BATCH_ACTION,
        "",
        COMPLIANCE_BATCH_ACTION,
        0,
        0,
        0,
        round(float(duration), 3),
        1 if success else 0,
        _err[:300],
        "compliance_check",
        "",
        "",
        _cls,
        1,
        "[]",
    )
    for _sql, _params in (
        (_AUDIT_INSERT_RICH, _row),
        (_AUDIT_INSERT_FULL, _row[:13]),
        (_AUDIT_INSERT_NO_IDENTITY, _row[:11]),
        (_AUDIT_INSERT_LEGACY, _row[:9]),
    ):
        try:
            await review_db.exec_write(db, _sql, _params, what="规范符合性检查：写入批次审计")
            return
        except Exception as exc:
            # ✅ R198：降级判据窄化 —— 旧判据 ``str(exc)`` 含 "column" 会把错误
            #    信息里碰巧带该词的**无关失败**（磁盘 I/O、库锁定等）误判为
            #    「旧库缺列」并**静默**降级写下一级；且降级本身零日志，审计行
            #    缺 error_class 的根因无从排查。现只认 sqlite 缺列的两种规范
            #    形态（INSERT 场景 "has no column named" / 其它 "no such column"）。
            _msg = str(exc).lower()
            if (
                isinstance(exc, sqlite3.OperationalError)
                and ("no such column" in _msg or "has no column named" in _msg)
            ):
                logger.debug("批次审计降级写低级 INSERT（旧库缺列）: %s", exc)
                continue
            logger.warning("批次审计写入失败（忽略）: %s", exc)
            return
    logger.warning("批次审计写入失败：ai_audit_logs 列均不兼容")


@router.post("/check")
async def compliance_check(body: ComplianceCheckIn, db=Depends(get_db)):
    scheme_id = body.scheme_id
    # ✅ BUG 修复（2026-09-21）：旧实现无方案存在性校验，不存在的 scheme_id 会
    #    走完整次 AI 调用（空内容 + 空方案名），既浪费额度又返回看似正常的响应，
    #    前端无从判断是「方案不存在」还是「正文恰好都没命中」。现补 404。
    sc_row = await review_db.fetch_one(
        db, "SELECT name, type, project_id FROM schemes WHERE id=?", (scheme_id,), what="规范符合性检查：读取方案"
    )
    if not sc_row:
        raise HTTPException(404, "方案不存在")
    scheme_name = sc_row["name"]
    scheme_type = sc_row["type"]
    # ✅ G4（2026-09-21）：compliance_check 表早已有 project_id 列，但两处 INSERT
    # 都不写它 → 项目维度的合规统计必须 JOIN schemes，且与 consistency_audit
    # （写了该列）口径不一致。现补齐。
    project_id = sc_row["project_id"] or ""

    # 未显式指定规则时沿用调用方传入的自由清单；指定时以规则注册表为准，
    # 保证 rule_id 语义稳定（历史结果可跨版本比对）
    # ✅ BUG 修复（2026-09-23）：rule_ids 全部无效时旧实现得到空清单，
    #    把一个空 checklist 送进提示词（AI 收到「逐项检查：[]」会自行编造检查项，
    #    结论与规则注册表完全脱钩）。现回退到调用方自由清单，再回退到规则库
    #    AI 规则全集，并记 warning 让运维可感知。
    if body.rule_ids:
        checklist = [get_rule(r).title for r in body.rule_ids if get_rule(r)]
        if not checklist:
            checklist = body.checklist or [r.title for r in ai_rules()]
            logger.warning(
                "compliance_check: rule_ids %r 均不在规则注册表，" "已回退为默认清单（scheme=%s）",
                body.rule_ids,
                scheme_id,
            )
    else:
        # ✅ BUG 修复（P1 · 2026-10-04）：`ComplianceCheckIn.checklist` 的默认工厂
        #    是 `list`（models.py:302），因此**前端不传清单、也不传 rule_ids** 时
        #    checklist 为 `[]` → 提示词里是「逐项检查：[]」，AI 只能自行编造检查项，
        #    结论与规则注册表完全脱钩（正是上面 310-313 注释要根治的现象，但旧实现
        #    只在 `rule_ids` 非空分支做了兜底，`else` 分支漏了）。
        #    真实触发面：前端 `useRules` 判据为 `checklist.length === aiRules.length`
        #    （SchemeWorkbenchPage.tsx:7041），而 `DEFAULT_COMPLIANCE_CHECKLIST`
        #    是 10 条自由文本、`ai_rules()` 现为 8 条 → 判据恒 false → 永远走
        #    「不传 rule_ids」这条路径。现补同一兜底：自由清单为空时回退 AI 规则全集。
        checklist = body.checklist or [r.title for r in ai_rules()]
    # ✅ 遗留修复（2026-09-22）：sort_order 是「同级内序号」而非全局文档序，
    #    ORDER BY sort_order 会把不同层级的同序号节点排在一起（按"列"展开），
    #    超长方案被 AI_CONTENT_HARD_CAP 截断时，送审的可能不是前几章而是
    #    错乱的碎片。现取 parent_id/sort_order 重排为目录树前序 DFS。
    sections = list(
        order_sections_dfs(
            await review_db.fetch_all(
                db,
                "SELECT id, parent_id, title, content, sort_order FROM sections"
                " WHERE scheme_id=? AND content!='' ORDER BY sort_order",
                (scheme_id,),
                what="规范符合性检查：读取章节正文",
            )
        )
    )
    # ✅ D-41（R176 · 2026-11-04）：旧实现「单章 >4000 硬截断 + 总长 >6000 丢弃
    #    尾部章节」—— 长方案后几章**从未进入模型**，用户却看到「已检查」。
    #    现改为「章节分片 + 分批送审」：
    #      1) 单章按 SECTION_CONTENT_CAP 切片，续段显式标注（不丢字）；
    #      2) 切片按 AI_CONTENT_HARD_CAP 装箱成批，每批独立调用同一提示词；
    #      3) 批次结果按 rule_id 合并（建议去重并集、严重度取高）。
    #    正常路径（含存量最大方案 23 批）完整覆盖：partial=False、
    #    truncated_sections=[]，覆盖度由 batch_count 披露。
    # ✅ D-41 成本闸门（R176）：分批使调用次数随目录长度线性增长，故设
    #    compliance_max_batches / compliance_max_chars（config.py，默认 40 批 /
    #    240000 字）；超限时审到上限即停，partial=true + truncated_sections
    #    （not_checked / partially_checked），已审部分照常返回，不白跑。
    _batches, content = _build_content_batches(sections)
    if len(_batches) > 1:
        logger.warning(
            "compliance_check: 内容 %d 字超过单批上限 %d，已拆为 %d 批送审（scheme=%s）",
            len(content),
            AI_CONTENT_HARD_CAP,
            len(_batches),
            scheme_id,
        )

    _budget = _batch_budget()
    _run_batches, _skipped_batches = _apply_batch_budget(_batches, _budget)
    _truncated_sections = _unchecked_section_list(_run_batches, _skipped_batches)
    partial = bool(_skipped_batches)
    if partial:
        logger.warning(
            "compliance_check: 触发预算闸门（上限 %d 批 / %d 字），" "已审 %d/%d 批，未审 %d 章（scheme=%s）",
            _budget[0],
            _budget[1],
            len(_run_batches),
            len(_batches),
            len(_truncated_sections),
            scheme_id,
        )

    # ✅ D-41：事实文本只装配一次（并发批次共享，避免 N 次重复 DB 读）
    _facts_text = await _load_facts_prompt_text(db, scheme_id)

    async def _run_one_batch(_bidx: int, _batch: list):
        """执行单批：返回 (本批正文, 模型对象, 耗时秒)。"""
        _text = _batch_text(_batch)
        _sys = render(
            "compliance_check_system",
            scheme_name=scheme_name,
            scheme_type=scheme_type,
            global_facts=_facts_text,
            checklist=json.dumps(checklist, ensure_ascii=False),
            content=_text,
        )
        # ✅ 修复（2026-09-24）：旧实现未传 scene —— 该调用在 ai_audit_logs 里
        #    scene 恒为空串，/ai/stats 场景聚看不到「符合性检查」的消耗，且无法
        #    为其单独配置模型（场景路由按 scene 精确匹配）。
        _t0 = time.perf_counter()
        _obj, _ = await collect_json_response(
            [{"role": "system", "content": _sys}],
            _validate_check_results,
            # ✅ R38：补 json_mode —— 本模块此前三个 JSON 端点是全链路唯一未开
            #    结构化输出模式的（同族 facts/consistency/global_facts 均已开），
            #    与 json_response 模块「弱模型稳定 JSON 工作流」的前提相悖。
            json_mode=True,
            temperature=0.2,
            scene="compliance_check",
        )
        return _text, _obj, time.perf_counter() - _t0

    # ✅ D-41：并发跑批 + 按批序串行合并（结果确定性不依赖完成顺序）。
    #    并发安全边界**不自造**：collect_json_response 内部固定走
    #    provider_factory.chat_with_fallback，后者用 concurrency_controller.semaphore
    #    兜底全局在飞 ≤ settings.max_concurrency（默认 5）—— 仓库 8 并发单日
    #    769 次 429 的历史教训即由此闸门收敛。return_exceptions=True 保证任何
    #    一批失败时其余任务被回收，不留孤儿协程，随后统一抛出（与串行语义一致）。
    _outcomes = await asyncio.gather(
        *[_run_one_batch(_i, _b) for _i, _b in enumerate(_run_batches)], return_exceptions=True
    )
    _merged: dict = {}
    _sent_chars = 0
    for _i, _out in enumerate(_outcomes):
        if isinstance(_out, BaseException):
            raise _out
        _text, _obj, _dur = _out
        _sent_chars += len(_text)
        _merge_check_results(_merged, _normalize_ai_results(_obj.get("results", []), checklist, body.rule_ids))
        # 单批（绝大多数场景）不写额外审计行，避免污染 ai_audit_logs
        if len(_batches) > 1:
            await _record_batch_audit(db, index=_i + 1, total=len(_batches), chars=len(_text), duration=_dur)
    if partial:
        await _record_batch_audit(
            db,
            index=len(_run_batches),
            total=len(_batches),
            chars=_sent_chars,
            duration=0.0,
            note=(
                f"budget_hit=1 skipped_batches={len(_skipped_batches)}"
                f" unchecked_sections={len(_truncated_sections)}"
            ),
        )
    results = list(_merged.values())
    # ✅ 根治批次截头（2026-09-23）：本次调用的全部行共享一个 batch_id，
    #    总检聚合按 batch_id 取「最近一批」，不再依赖 rowid 连续段推断
    #    （两次 /check 跨秒交错时 rowid 段会错切，首批被截头/混批）。
    batch_id = uuid.uuid4().hex
    # ✅ 2026-10-07：本批结论的正文指纹（整批共用，供总检判定「该批是否已过期」）
    _batch_fp = await _content_fingerprint(db, scheme_id)
    for r in results:
        cid = str(uuid.uuid4())
        # ✅ B-01：把「部分检查」事实随结果行落库。总检聚合读 result JSON 时
        #    可据此把该批标记为 partial，而不是当成完整检查通过。
        _row = dict(r)
        _row["partial"] = partial
        _row["truncated_section_count"] = len(_truncated_sections)
        # ✅ R13（2026-10-06）：写路径经 exec_write。旧实现丢弃返回值 ——
        #    INSERT 返回 None 时 commit 照常成功、接口返回 batch_id，前端
        #    「清除本次结果」后端查无此批，「最近一批合规结果」永远取不到，
        #    而用户以为已经跑过了（静默丢数据，AGENTS.md 零容忍项）。
        await review_db.exec_write(
            db,
            "INSERT INTO compliance_check (id, scheme_id, project_id, check_type,"
            " rule_id, item, severity, result, suggestion, batch_id,"
            " content_fingerprint)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                cid,
                scheme_id,
                project_id,
                "compliance",
                r.get("rule_id", ""),
                r.get("item", ""),
                r.get("severity", ""),
                json.dumps(_row, ensure_ascii=False),
                r.get("suggestion", ""),
                batch_id,
                _batch_fp,
            ),
            what="规范符合性检查：写入结果行",
        )
    await db.commit()
    return {
        "results": results,
        "batch_id": batch_id,
        # ✅ B-01（加法式，向后兼容）：partial=true 表示本次 AI 检查只覆盖了
        #    前 AI_CONTENT_HARD_CAP 个字符，truncated_sections 逐章标注未完整
        #    参与检查的章节。旧消费方忽略这些字段即可，语义不变。
        # ✅ D-41（R176）：分批送审后正常路径不再丢章节（partial=false、
        #    truncated_sections=[]）；仅预算闸门触顶时 partial=true 并逐章
        #    标注 not_checked / partially_checked（加法式，旧消费方忽略即可）。
        "partial": partial,
        "truncated_sections": _truncated_sections,
        "checked_chars": _sent_chars,
        "total_chars": len(content),
        "batch_count": len(_batches),
        "executed_batch_count": len(_run_batches),
        # 闸门值随响应下发，便于前端在 partial 时解释「为什么没审完」
        # （<=0 表示该维度无上限，与配置语义一致）
        "budget": {"max_batches": _budget[0], "max_chars": _budget[1]},
    }


def _validate_check_results(o) -> list[str]:
    """/check 的元素级校验：``results`` 每一项都必须有可展示的 ``item`` 与 ``hit``。

    ✅ R38（2026-10-03 · P1-b）：旧实现只有 ``lambda o: [] if o.get("results")``
    —— 列表 truthy 即通过。随后 ``_normalize_ai_results`` 逐项 ``dict(r)``、
    改写 ``rule_id`` 后**无条件 append**（不做任何过滤），于是模型返回
    ``{"results": [{}]}`` 时：``rule_id`` 被回退成规则表第 0 项（语义错配），
    ``item``/``severity`` 以空串落库，UI 上多出一条空行 —— 用户看到的是
    「有一条规则命中」但没有任何内容。提示词 analysis.py:377 明确声明了
    6 个字段，校验器一个都不查。
    """
    rows = o.get("results")
    if not rows:
        return ["缺少 results"]
    if not isinstance(rows, list):
        return ["results 不是数组"]
    issues: list[str] = []
    for idx, r in enumerate(rows):
        if not isinstance(r, dict):
            issues.append("results[%d] 不是对象" % idx)
            continue
        if not str(r.get("item") or "").strip():
            issues.append("results[%d] 缺少 item（清单项名称）" % idx)
        hit = r.get("hit")
        if not isinstance(hit, bool) and str(hit).strip().lower() not in ("true", "false"):
            issues.append("results[%d] 的 hit 不是布尔值" % idx)
    return issues


def _normalize_ai_results(results: list, checklist: list, rule_ids: list) -> list:
    """把 AI 返回的 rule_id 归一到规则注册表编号。

    提示词里 rule_id 只是示例（``R1``），模型常常自造编号或直接照抄。
    若不归一，历史记录里的 rule_id 语义不稳定 —— 既无法跨版本比对，
    也无法把 AI 结论归并进维度评分。此处按「顺序 → 显式规则号 → 标题匹配」
    三级回退确定真实规则编号。
    """
    known = [r for r in rule_ids if get_rule(r)]
    rows = list(results or [])
    out: list = []
    for idx, r in enumerate(rows):
        item = dict(r) if isinstance(r, dict) else {"item": str(r or "")}
        # ✅ R197（B1）：``hit`` 字符串布尔在此归一为**真布尔**。/check 的元素级
        #    校验器（_validate_check_results）放行 "true"/"false" 字符串，但下游
        #    三条口径互斥且全都不一致：评分侧 ``ai_results_to_findings`` 只认
        #    ``hit is True``（"true" 会被当「未命中」误报并扣分）、
        #    ``_merge_check_results`` 的 ``bool("false")`` 恒为 True、校验器又
        #    接受字符串 —— 唯一收口点就是落库前的归一。
        raw_hit = item.get("hit")
        if isinstance(raw_hit, str) and raw_hit.strip().lower() in ("true", "false"):
            item["hit"] = raw_hit.strip().lower() == "true"
        rid = str(item.get("rule_id") or "").strip()
        if not get_rule(rid):
            # ✅ R197（B2）：顺序回退仅在「返回条数 == 清单条数」时启用 ——
            #    模型漏判某项或乱序返回时，按列表下标强配会把 A 规则的结论
            #    安到 B 规则头上（severity 锚定、维度扣分全部错位，且比缺项
            #    更难察觉）。条数不等时只按标题匹配，匹配不到宁可留空。
            if idx < len(known) and len(rows) == len(known):
                rid = known[idx]
            else:
                rid = _match_rule_by_title(item.get("item") or "", checklist)
        item["rule_id"] = rid
        out.append(item)
    return out


#: 严重度排序（用于分批结果合并时「取更高一档」）
#: ✅ R197（B3）：补 block/阻断 档（AI 返回 block 时旧词表判 0，合并恒取旧档）；
#:    档位词先经 normalize_severity_text 归一，此处保留中英文双写仅作冗余防御。
_SEVERITY_RANK = {
    "block": 4,
    "阻断": 4,
    "高": 3,
    "中": 2,
    "低": 1,
    "high": 3,
    "medium": 2,
    "low": 1,
}


def _suggestion_list(row: dict) -> list[str]:
    """把一条结果的建议归一为**去重后的条目列表**（兼容加法式 ``suggestions``）。"""
    _raw = row.get("suggestions")
    _items = list(_raw) if isinstance(_raw, list) else []
    if not _items and _raw:
        _items = [str(_raw)]
    _items.append(row.get("suggestion") or "")
    out: list[str] = []
    for _s in _items:
        _s = str(_s or "").strip()
        if _s and _s not in out:
            out.append(_s)
    return out


def _merge_check_results(merged: dict, rows: list) -> None:
    """✅ D-41（R176）：把单批的检查结果并入跨批结果集（按规则号合并）。

    同一规则可能在多批中都被命中（不同章节的同类问题）。合并口径：
    - ``hit`` 取并集（任一批命中即为命中）；
    - ``severity`` 取更高一档（高 > 中 > 低）；
    - ✅ D-41 修复（R176）：``suggestions`` 取**去重并集**（不同批次的补充建议
      都保留为独立条目，不拼接成一段、也不只留首条 —— 旧实现会丢后续批次
      的建议），``suggestion`` 兼容旧消费方取并集首条。
    规则号缺失时按 item 文案归并；两者都缺失时保留为独立行（不误合并）。
    """
    for _i, _r in enumerate(rows or []):
        rid = str(_r.get("rule_id") or "").strip()
        item = str(_r.get("item") or "").strip()
        key = rid or (f"item:{item}" if item else f"__row__{len(merged)}:{_i}")
        prev = merged.get(key)
        if prev is None:
            _first = dict(_r)
            _sugs = _suggestion_list(_r)
            if _sugs:
                _first["suggestions"] = _sugs
                _first["suggestion"] = _sugs[0]
            merged[key] = _first
            continue
        prev["hit"] = bool(prev.get("hit")) or bool(_r.get("hit"))
        if _SEVERITY_RANK.get(
            normalize_severity_text(_r.get("severity")), 0
        ) > _SEVERITY_RANK.get(normalize_severity_text(prev.get("severity")), 0):
            prev["severity"] = _r.get("severity")
        _sugs = _suggestion_list(prev)
        for _s in _suggestion_list(_r):
            if _s not in _sugs:
                _sugs.append(_s)
        if _sugs:
            prev["suggestions"] = _sugs
            prev["suggestion"] = _sugs[0]


def _match_rule_by_title(item_text: str, checklist: list) -> str:
    """按检查项文案在清单中的位置反查规则号（清单来自规则注册表时有效）。"""
    from app.services.audit_rules import ai_rules

    if item_text:
        for rule in ai_rules():
            if rule.title == item_text or rule.title in item_text:
                return rule.rule_id
    try:
        pos = checklist.index(item_text)
    except ValueError:
        return ""
    rules = ai_rules()
    return rules[pos].rule_id if pos < len(rules) else ""


@router.post("/expert-review")
async def expert_review(body: ExpertReviewIn, db=Depends(get_db)):
    scheme_id = body.scheme_id
    # ✅ BUG 修复（2026-09-21）：旧实现无方案存在性校验；不存在的 scheme_id 会
    #    走一次 AI 调用（空 outline + 空方案名），返回看似合理的评分，前端无从
    #    区分"方案不存在"与"章节全部通过论证"。现补 404。
    sc_row = await review_db.fetch_one(
        db, "SELECT name, type, project_id FROM schemes WHERE id=?", (scheme_id,), what="专家论证预检：读取方案"
    )
    if not sc_row:
        raise HTTPException(404, "方案不存在")
    scheme_name = sc_row["name"]
    scheme_type = sc_row["type"]
    project_id = sc_row["project_id"] or ""

    # ✅ 遗留修复（2026-09-22）：目录树按前序 DFS 送 AI，避免扁平序层级错乱
    outline_rows = order_sections_dfs(
        await review_db.fetch_all(
            db,
            "SELECT id, parent_id, title, level, sort_order FROM sections" " WHERE scheme_id=? ORDER BY sort_order",
            (scheme_id,),
            what="专家论证预检：读取章节目录",
        )
    )
    # ✅ BUG 修复（2026-09-21）：旧实现 outline_tree 无节点上限，方案章节多时
    #    JSON 序列化后的提示词可轻易突破模型上下文；现引入 EXPERT_OUTLINE_MAX_NODES
    #    显式 400，避免静默打爆 token。附件同理：既限数量也限单条长度。
    if len(outline_rows) > EXPERT_OUTLINE_MAX_NODES:
        raise HTTPException(
            400, f"章节数 {len(outline_rows)} 超过上限 {EXPERT_OUTLINE_MAX_NODES}，" "请精简目录后再执行专家论证预检"
        )
    outline_tree = [{"title": r["title"], "level": r["level"]} for r in outline_rows]

    attachments = [str(a) for a in (body.attachments or [])]
    if len(attachments) > EXPERT_ATTACH_MAX_COUNT:
        raise HTTPException(
            400, f"附件数量 {len(attachments)} 超过上限 {EXPERT_ATTACH_MAX_COUNT}，" "请筛选最关键的材料再试"
        )
    for _i, a in enumerate(attachments):
        if len(a) > EXPERT_ATTACH_MAX_LEN:
            raise HTTPException(
                400, f"第 {_i + 1} 条附件长度 {len(a)} 超过上限 {EXPERT_ATTACH_MAX_LEN}，" "请精简后重试"
            )

    # ✅ 论证必要项由规则注册表在运行时注入（消除提示词内联清单与 EXPERT_CHECK_ITEMS 的分叉）
    check_items = "\n".join(f"- {it['item']}（规则 {it['rule_id']}｜{it['basis']}）" for it in expert_items())
    sys_prompt = render(
        "expert_review_system",
        scheme_name=scheme_name,
        scheme_type=scheme_type,
        global_facts=await _load_facts_prompt_text(db, scheme_id),
        outline_tree=json.dumps(outline_tree, ensure_ascii=False),
        attachments=json.dumps(attachments, ensure_ascii=False),
        check_items=check_items,
    )
    # ✅ 修复（2026-09-24）：补 scene（原为空 → 统计归空场景、场景路由配不上）
    obj, _ = await collect_json_response(
        [{"role": "system", "content": sys_prompt}],
        lambda o: [] if "score" in o else ["缺少 score"],
        json_mode=True,
        temperature=0.2,
        scene="expert_review",
    )

    result = _normalize_expert_review_result(obj)
    cid = str(uuid.uuid4())
    # ✅ 单行也是一批：补 batch_id（与 /check 同口径，历史行空串不影响读取回退）。
    #    ✅ R13（2026-10-06）：写路径经 exec_write —— 旧实现 INSERT 未生效时
    #    commit 照常成功、接口返回评分，总检的「专家论证预检」来源永远取不到本次。
    await review_db.exec_write(
        db,
        "INSERT INTO compliance_check (id, scheme_id, project_id, check_type,"
        " result, batch_id, content_fingerprint) VALUES (?,?,?,?,?,?,?)",
        (
            cid,
            scheme_id,
            project_id,
            "expert_review",
            json.dumps(result, ensure_ascii=False),
            uuid.uuid4().hex,
            await _content_fingerprint(db, scheme_id),
        ),
        what="专家论证预检：写入结果行",
    )
    await db.commit()
    return result


def _normalize_expert_review_result(obj: dict) -> dict:
    """显式补齐 ``expert_review_system``（analysis.py:399）声明的 4 个字段。

    ✅ R38（2026-10-03 · P1-b）：旧实现 ``result = obj`` 直接把模型返回体透给
    前端 —— 模型漏掉 ready/missing/suggestions 时，前端拿到的是 **undefined**
    （不是空数组），渲染分支各自兜底，行为不确定且日志无痕。这里把缺失字段
    归一成空数组、非数组字段降级为空数组，并在两者发生时各记一条 WARNING，
    使「模型漏字段」从静默变成可观测事件。

    刻意**不**加硬校验：expert_review 的评分语义容错（字符串评分按 0 处理）
    是既有行为，改成硬失败会把「降级返回」变成 500，属越界的行为变更。
    """
    result = dict(obj or {})
    for f in ("ready", "missing", "suggestions"):
        if f not in result:
            logger.warning("expert_review 输出缺少 %s 字段，按空列表补齐（score=%r）", f, result.get("score"))
            result[f] = []
        elif not isinstance(result[f], list):
            # 模型可能返回单字符串（如 missing="工程概况"），直接丢弃会丢失信息
            # 转为单元素数组保留内容，并记 WARNING 供运维审计
            raw = result[f]
            if isinstance(raw, str) and raw.strip():
                logger.warning("expert_review 的 %s 是字符串（%r），转为单元素数组保留", f, raw[:120])
                result[f] = [raw.strip()]
            else:
                logger.warning("expert_review 的 %s 不是数组（%r），按空列表补齐", f, type(raw).__name__)
                result[f] = []
    return result


@router.get("/results/{scheme_id}")
async def get_results(
    scheme_id: str, check_type: str = "", limit: int = RESULTS_DEFAULT_LIMIT, offset: int = 0, db=Depends(get_db)
):
    # ✅ BUG 修复（2026-09-21）：旧实现无 LIMIT 无分页，历史累积后一次返回可能
    #    数万行 JSON 撑爆前端与网络。现引入 limit/offset（默认 50、上限 500），
    #    保持向后兼容：老调用方不传参仍拿到最近 50 条；前端要全量可显式传
    #    limit=RESULTS_MAX_LIMIT。返回体同时给 total，便于前端做分页控件。
    # ✅ BUG 修复（2026-09-23）：上一版注释声称分页，实现却是「全量拉取 →
    #    Python 切片」，历史行多时每次请求仍把全部行（含 result JSON 大字段）
    #    读进内存，分页只省了带宽没省 IO。现改为 SQL 级 COUNT + LIMIT/OFFSET。
    limit = max(1, min(int(limit) if limit else RESULTS_DEFAULT_LIMIT, RESULTS_MAX_LIMIT))
    offset = max(0, int(offset) if offset else 0)
    where = "scheme_id=?"
    params: list = [scheme_id]
    if check_type:
        where += " AND check_type=?"
        params.append(check_type)
    total = int(
        await review_db.fetch_scalar(
            db, f"SELECT COUNT(*) AS n FROM compliance_check WHERE {where}", params, 0, what="合规历史：统计总数"
        )
        or 0
    )
    items = await review_db.fetch_all(
        db,
        f"SELECT * FROM compliance_check WHERE {where}" " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
        what="合规历史：读取结果行",
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# ✅ 全文一致性审计（产品需求文档 §3.12.1）
#    此前 consistency_audit 表只有删除级联引用、业务读写未实现（死表）。
#    现补齐实现：AI 将「项目关键事实」（global_facts 唯一可信数据源）与正文
#    逐项比对 → 0-100 评分 + 不一致项清单，持久化到 consistency_audit 表。
# ---------------------------------------------------------------------------
def _validate_consistency_audit(o) -> list[str]:
    """一致性审计的字段校验：**必须同时**有 ``score`` 与 ``issues`` 数组。

    ✅ R38（2026-10-03 · P1-b）：旧校验器只有 ``"score" in o``，而调用方
    消费的是 ``obj.get("issues", []) or []``、落库的也是 ``issues``。
    校验字段与消费字段**完全不重叠** → 模型返回 ``{"score": 100}``（漏 issues）
    时校验通过、接口 200、``issues="[]"`` 落库，用户看到「一致性 0 处问题、
    100 分」—— 用**缺字段**换来了**看起来最好的结论**。
    提示词 analysis.py:419 明确声明了 ``{"score", "issues"}`` 两个字段。

    注意 ``issues`` 允许是**空数组**（真的没有不一致），因此不能用 truthy 判断；
    判据是「键存在且是 list」。``score`` 同理允许 0，但必须是可转数值的。
    """
    issues: list[str] = []
    if not isinstance(o.get("issues"), list):
        issues.append("缺少 issues 数组（不一致项清单；没有不一致时也要给 []）")
    try:
        float(o.get("score"))
    except (TypeError, ValueError):
        issues.append("score 不是数值")
    return issues


@router.post("/consistency-audit/{scheme_id}")
async def run_consistency_audit(scheme_id: str, db=Depends(get_db)):
    from app.services.facts_builder import build_facts_text as _build_facts_text

    sc_row = await review_db.fetch_one(
        db, "SELECT name, type, project_id FROM schemes WHERE id=?", (scheme_id,), what="一致性审计：读取方案"
    )
    if not sc_row:
        raise HTTPException(404, "方案不存在")

    # ✅ 遗留修复（2026-09-22）：审计上下文同样按目录树前序 DFS 送 AI，
    #    保证截断与审阅顺序与文档实序一致
    sections = [
        {"id": r["id"], "title": r["title"], "content": r["content"]}
        for r in order_sections_dfs(
            await review_db.fetch_all(
                db,
                "SELECT id, parent_id, title, content, sort_order FROM sections"
                " WHERE scheme_id=? AND content!='' ORDER BY sort_order",
                (scheme_id,),
                what="一致性审计：读取章节正文",
            )
        )
    ]
    if not sections:
        raise HTTPException(422, "方案尚无正文内容，无法审计")
    # ✅ B-19（R176）：与 /check 完全同一套长文本策略 —— 单章 6000 切片（续段
    #    显式标注）、按 6000 装箱成批、共用同一预算闸门。旧实现「整份 50000
    #    硬截断且无总长标记」会静默丢弃长方案尾部章节（存量 6 个方案中 4 个中招）。
    _batches, content = _build_content_batches(sections)
    _budget = _batch_budget()
    _run_batches, _skipped_batches = _apply_batch_budget(_batches, _budget)
    if _skipped_batches:
        # 一致性审计的结论会落库供总检消费，而表结构无处存放「部分检查」标记；
        # 与其把不完整结论当成完整结论落库，不如明确拒绝（不让总检误判已放行）。
        raise HTTPException(
            422,
            f"目录过长：本次需审计 {len(_batches)} 批，超过单次预算上限 "
            f"{_budget[0]} 批（{_budget[1]} 字），请分批审核",
        )

    facts = await _build_facts_text(db, scheme_id, max_total=6000)
    if not facts.strip():
        raise HTTPException(422, "尚未提取项目关键事实，请先执行「全局事实提取」再审计一致性")

    async def _run_one_batch(_batch: list):
        _sys = render(
            "consistency_audit_system",
            scheme_name=sc_row["name"],
            scheme_type=sc_row["type"],
            facts=facts,
            content=_batch_text(_batch),
        )
        # ✅ 修复（2026-09-24）：补 scene（原为空 → 统计归空场景、场景路由配不上）
        _obj, _ = await collect_json_response(
            [{"role": "system", "content": _sys}],
            _validate_consistency_audit,
            json_mode=True,
            temperature=0.2,
            scene="consistency_audit",
        )
        return _obj

    # ✅ D-41（R176）：与 /check 同一并发模型 —— gather + return_exceptions，
    #    全局在飞上限由 provider_factory 的 concurrency_controller 统一兜底。
    _outcomes = await asyncio.gather(*[_run_one_batch(_b) for _b in _run_batches], return_exceptions=True)
    for _out in _outcomes:
        if isinstance(_out, BaseException):
            raise _out

    issues: list = []
    _seen: set = set()
    _scores: list[float] = []
    _sent_chars = 0
    for _batch, _obj in zip(_run_batches, _outcomes):
        _sent_chars += len(_batch_text(_batch))
        for _it in _obj.get("issues", []) or []:
            _sig = json.dumps(_it, ensure_ascii=False, sort_keys=True)
            if _sig not in _seen:
                _seen.add(_sig)
                issues.append(_it)
        # 评分容错：AI 可能返回字符串（如"良好"）或缺失，强制转为 float 失败则记 0
        try:
            _scores.append(float(_obj.get("score", 0)))
        except (TypeError, ValueError):
            logger.warning("一致性审计评分非数值，按 0 处理: %r", _obj.get("score"))
            _scores.append(0.0)
    # 多批取最小分：整体一致性不高于最差批次（保守口径，避免分批后分数虚高）
    # ✅ R181（遗留问题 6）：确认这是**有意**口径而非副作用 —— 分批是内部实现
    #    细节（受单次 token 预算约束），若取平均或最大分，用户会看到「同一份
    #    正文，只是批次变了，分数就升高」的假改善。故固定取最小，并在响应与
    #    前端文案中显式披露（score_rule / score_note，加法式）。
    score_float = min(_scores) if _scores else 0.0
    audit_id = str(uuid.uuid4())
    # ✅ R180（2026-11-05 · 可观测性）：把本次审计的覆盖范围一并落库，
    #    使「今天做了几次校验、每次覆盖多少节/字、发现多少问题」可直接查库
    #    （此前只有 score + issues，无法回答覆盖度）。
    _counts = {
        "sections": len(sections),
        "batches": len(_batches),
        "checked_chars": _sent_chars,
        "total_chars": len(content),
        "issues": len(issues),
    }
    # ✅ R13（2026-10-06）：写路径经 exec_write。旧实现 INSERT 未生效时
    #    commit 照常成功、接口返回评分与 issues —— 而 /latest 与总检的
    #    「一致性审计」来源永远取不到本次，用户以为已审计。
    # ✅ BUG 修复（2026-10-14 · R78 F-1）：列序与参数序必须逐位对应 ——
    #    旧实现在 2026-10-07（05e1380）给 INSERT 加 content_fingerprint 列时
    #    把指纹参数插到了 score_float **之前**，导致 score 列落指纹字符串、
    #    content_fingerprint 列落分值。连带三处后果（详见护栏
    #    tests/test_review_preflight_closeout_r78_20261014.py）：
    #      ① _ai_row_is_stale 读到 content_fingerprint="90.0" 与当前 hex 指纹
    #         恒不等 → 一致性审计来源在总检中**永远判 stale 被跳过**；
    #      ② /latest 把指纹串当 score 返回，前端刷新后分数面板坏；
    #      ③ /history 同污染。
    await review_db.exec_write(
        db,
        "INSERT INTO consistency_audit (id, project_id, scheme_id, score,"
        " content_fingerprint, issues, counts_json) VALUES (?,?,?,?,?,?,?)",
        (
            audit_id,
            sc_row["project_id"],
            scheme_id,
            score_float,
            await _content_fingerprint(db, scheme_id),
            json.dumps(issues, ensure_ascii=False),
            json.dumps(_counts, ensure_ascii=False),
        ),
        what="一致性审计：写入审计结果",
    )
    await db.commit()
    return {
        "id": audit_id,
        "score": score_float,
        "issues": issues,
        # ✅ B-19（R176，加法式）：分批后披露批次数与覆盖字符数；
        #    本端点不做截断（超预算直接 422），故 partial 恒为 False。
        "batch_count": len(_batches),
        "partial": False,
        "checked_chars": _sent_chars,
        "total_chars": len(content),
        "counts": _counts,
        # ✅ R181（遗留问题 6，加法式）：显式披露评分口径，避免用户以为
        #    「分数比单批时低」是正文变差了。
        "score_rule": "min_across_batches",
        "score_note": _consistency_score_note(len(_batches)),
    }


@router.get("/consistency-audit/{scheme_id}/latest")
async def latest_consistency_audit(scheme_id: str, db=Depends(get_db)):
    item = await review_db.fetch_one(
        db,
        "SELECT id, score, issues, counts_json, created_at FROM consistency_audit "
        "WHERE scheme_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (scheme_id,),
        what="一致性审计：读取最近一次结果",
    )
    if not item:
        return {"exists": False}
    try:
        item["issues"] = json.loads(item.get("issues") or "[]")
    except json.JSONDecodeError:
        item["issues"] = []
    # ✅ R180：历史行 counts_json 为 '{}'（或旧库空串）→ 统一解析为空对象，
    #    读取端永不因新增列报错。
    try:
        item["counts"] = json.loads(item.get("counts_json") or "{}")
    except json.JSONDecodeError:
        item["counts"] = {}
    item.pop("counts_json", None)
    item["exists"] = True
    # ✅ R181（遗留问题 6，加法式）：读取端同样披露评分口径（口径恒定，
    #    多批与否只影响文案里的批次说明）。
    item["score_rule"] = "min_across_batches"
    try:
        _batches_n = int(item["counts"].get("batches") or 0)
    except (TypeError, ValueError, AttributeError):
        _batches_n = 0
    item["score_note"] = _consistency_score_note(_batches_n)
    return item


# @deprecated 孤儿 API：本前端无调用方；按向后兼容原则保留，大版本评估清理。
@router.get("/consistency-audit/{scheme_id}/history", deprecated=True)
async def consistency_audit_history(scheme_id: str, db=Depends(get_db), limit: int = 10, response: Response = None):
    _apply_deprecation_headers(
        response,
        replacement=f"/api/v1/compliance/runs/{scheme_id}",
    )
    limit = max(1, min(limit, 50))
    items = []
    for r in await review_db.fetch_all(
        db,
        "SELECT id, score, issues, counts_json, created_at FROM consistency_audit "
        "WHERE scheme_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (scheme_id, limit),
        what="一致性审计历史：读取记录",
    ):
        item = r
        try:
            item["issues"] = json.loads(item.get("issues") or "[]")
        except json.JSONDecodeError:
            item["issues"] = []
        # ✅ R180：历史行无 counts_json 时解析为空对象（零迁移兼容）
        try:
            item["counts"] = json.loads(item.get("counts_json") or "{}")
        except json.JSONDecodeError:
            item["counts"] = {}
        item.pop("counts_json", None)
        items.append(item)
    return {"items": items}


# ---------------------------------------------------------------------------
# ✅ 程序化预检（离线、秒级）
# ---------------------------------------------------------------------------
async def _facts_signature(db, scheme_id: str) -> str:
    """已确认全局事实的轻量签名（条数 + 最新更新时间），进预检进程内缓存键。

    ✅ 2026-10-03（全局事实桥接）：预检缓存键此前只含正文内容指纹，
    事实确认/新增后 TTL 内仍返回旧结论（SAF-08 永远不出现）。现把
    「可注入事实」的条数+最新 updated_at 并入**进程内**缓存键；
    ⚠️ 落库的 content_fingerprint（G3 时效语义）保持不变 —— 若把事实并进
    指纹，历史 preflight_runs 会因旧签名缺事实项被全量判 stale，
    正是 2026-09-23 明确避免过的「旧记录全标红条」问题。
    """
    try:
        pid = await resolve_scheme_project_id(db, scheme_id)
        scope = "scheme_id=?"
        params: list = [scheme_id]
        if pid:
            scope += " OR " + FACT_SCOPE_SHARED_FRAGMENT
            params.append(pid)
        row = await review_db.fetch_one(
            db,
            f"SELECT COUNT(*) AS n, COALESCE(MAX(updated_at),'') AS latest FROM global_facts "
            f"WHERE ({scope}) AND {get_facts_inject_where()}",
            params,
            what="预检缓存键：统计可注入事实",
        )
        # ⚠️ 必须按列名取值：review_db.fetch_one 返回 dict（sqlite3.Row 迭代出的是
        #    值、dict 迭代出的是键）。旧实现写 `row[0] / row[1]`，换成 dict 后
        #    恒 KeyError → 被本函数的 except 吞掉恒返回 ""，等于**事实签名永久失效**：
        #    事实确认/新增后 TTL 内仍命中旧缓存，SAF-08 永远不出现（2026-10-03 修复复发）。
        return f"{row['n'] or 0}:{row['latest'] or ''}" if row else "0:"
    except Exception as exc:
        # ✅ R198：签名失败仅视为「无事实」，不阻断预检 —— 但必须留痕：
        #    2026-10-03 的修复复发（恒 KeyError 被 except 吞掉、签名永久失效、
        #    SAF-08 永不出现）正是「静默降级」形态；无日志则复发无从定位。
        logger.warning(
            "预检缓存键：事实签名计算失败（按无事实降级） scheme=%s: %s", scheme_id, exc, exc_info=True
        )
        return ""


#: ✅ R207（日志驱动）：就绪度总检聚合时实际会读、但**不改变正文**的来源表。
#: /check、/expert-review、/consistency-audit、/consistency/scan|repair|confirm、
#: 评分项提取、全局事实确认等写入都不会改变 content_fingerprint / review_state_hash，
#: 也不一定改变「可注入事实」签名（_facts_signature 只统计可注入事实）。
#: 结果是 TTL(120s) 内 /overview 会返回**旧放行结论**（生产日志实证：跑完合规
#: 检查后总检仍显示上一次的 released=True）。签名只需感知「写入水位」，不做判据复制。
_SOURCE_SIGNATURE_QUERIES: tuple[tuple[str, str, tuple], ...] = (
    (
        "compliance_check",
        "SELECT COUNT(*) AS n, COALESCE(MAX(rowid),0) AS mx FROM compliance_check WHERE scheme_id=?",
        ("scheme",),
    ),
    (
        "consistency_audit",
        "SELECT COUNT(*) AS n, COALESCE(MAX(rowid),0) AS mx FROM consistency_audit WHERE scheme_id=?",
        ("scheme",),
    ),
    (
        "consistency_conflicts",
        "SELECT COUNT(*) AS n, COALESCE(MAX(rowid),0) AS mx,"
        " COALESCE(SUM(CASE WHEN status IN ('pending','failed') THEN 1 ELSE 0 END),0) AS unresolved"
        " FROM consistency_conflicts WHERE scheme_id=?",
        ("scheme",),
    ),
    (
        "consistency_repairs",
        "SELECT COUNT(*) AS n, COALESCE(MAX(rowid),0) AS mx FROM consistency_repairs WHERE scheme_id=?",
        ("scheme",),
    ),
    # 下面三张表的写入键是 project_id 或全库共享；按全表取水位属**保守失效**
    # （别的项目写入也会让本方案缓存重算一次），代价仅是一次 COUNT+MAX(rowid)。
    ("scheme_scoring_items", "SELECT COUNT(*) AS n, COALESCE(MAX(rowid),0) AS mx FROM scheme_scoring_items", ()),
    ("bid_analysis_items", "SELECT COUNT(*) AS n, COALESCE(MAX(rowid),0) AS mx FROM bid_analysis_items", ()),
    ("global_facts", "SELECT COUNT(*) AS n, COALESCE(MAX(rowid),0) AS mx FROM global_facts", ()),
)


async def _sources_signature(db, scheme_id: str) -> str:
    """六源写入水位签名（并入总检缓存键，防止「刚跑完检查却拿到旧结论」）。

    每一路独立 try：旧库缺表 / 测试内存库无该表 / 瞬时读失败时该路返回空串
    （fail-soft，不改变其它路的判定，绝不阻断总检）。刻意**不把
    ``preflight_runs`` 计入**：总检自身会写该表，否则缓存将永不命中。
    """
    parts: list[str] = []
    for name, sql, arg_kinds in _SOURCE_SIGNATURE_QUERIES:
        try:
            params = (scheme_id,) if arg_kinds else ()
            row = await review_db.fetch_one(db, sql, params, what=f"总检缓存键：{name} 写入水位")
            parts.append(f"{name}={row['n'] if row else 0}:{row['mx'] if row else 0}")
            if "unresolved" in sql and row:
                parts.append(f"{name}.unresolved={row['unresolved'] or 0}")
        except Exception as exc:  # noqa: BLE001 - 单一来源不可判定不得阻断总检
            logger.debug("总检缓存键：%s 写入水位不可用（跳过该路）: %s", name, exc)
            parts.append(f"{name}=?")
    return "|".join(parts)


async def _build_preflight_context(scheme_id: str, db) -> PreflightContext:
    """从 DB 装配预检上下文（章节 + 图表 + 方案元信息 + 已确认全局事实）。

    ✅ R13（2026-10-06）：三处读全部走 ``review_db``。本函数是 /preflight、
    /overview 与自动修复三端重算的**共同入口**，且不在任何 try 内 ——
    旧实现命中 R13 即 ``AttributeError`` → 500，用户既拿不到预检结论
    也看不到原因。
    """
    sc_row = await review_db.fetch_one(
        db, "SELECT name, type, word_budget FROM schemes WHERE id=?", (scheme_id,), what="预检上下文：读取方案"
    )
    if not sc_row:
        raise HTTPException(404, "方案不存在")

    # ✅ 遗留修复（2026-09-22）：预检上下文按目录树前序 DFS 装配，
    #    使重复内容检测（两两配对）之外的序敏感检查与导出链路同口径。
    sections = order_sections_dfs(
        await review_db.fetch_all(
            db,
            "SELECT id, parent_id, title, content, word_count, word_budget, level, status, sort_order FROM sections"
            " WHERE scheme_id=? ORDER BY sort_order",
            (scheme_id,),
            what="预检上下文：读取章节",
        )
    )

    charts = await review_db.fetch_all(
        db,
        "SELECT chart_type, status FROM chart_predictions WHERE scheme_id=?",
        (scheme_id,),
        what="预检上下文：读取图表登记",
    )

    # ✅ 2026-10-03（全局事实桥接）：装配「已确认可注入」事实（桥接自身
    #    fail-soft，异常返回空 → check_hazard_params 跳过，预检可用性不受影响）。
    facts = await load_resolved_facts_for_scope(db, scheme_id=scheme_id)

    return PreflightContext(
        scheme_id=scheme_id,
        scheme_name=sc_row["name"] or "",
        scheme_type=sc_row["type"] or "",
        word_budget=int(sc_row["word_budget"] or 0),
        sections=sections,
        charts=charts,
        facts=facts,
    )


# ---------------------------------------------------------------------------
# ✅ G2/G3（2026-09-21）：就绪度总检的并发锁、幂等缓存与结论时效判定
# ---------------------------------------------------------------------------
#: 就绪度总检的进程内锁与幂等缓存。
#: ✅ G2：此前既无锁也无幂等 —— 连点「一键总检」会往历史趋势里灌入多条几乎
#: 相同的记录（污染分数趋势），并发两次还会读到不同批的结果。
_OVERVIEW_LOCKS: dict[str, asyncio.Lock] = {}
#: scheme_id → (content_fingerprint, 落缓存的 monotonic 时间, payload)
_OVERVIEW_RECENT: dict[str, tuple[str, float, dict]] = {}
#: 同内容指纹下，多久内的重复请求直接返回上次的结论（秒）
OVERVIEW_CACHE_TTL = 120.0
#: ✅ BUG 修复（2026-09-23）：独立预检 /preflight 与 /overview 同构的幂等缓存
#: （scheme_id → (content_fingerprint, monotonic 时间, payload)）。此前 /preflight
#: 既无锁也无缓存，连点会往 preflight_runs 灌入多条几乎相同的记录，污染分数趋势。
_PREFLIGHT_RECENT: dict[str, tuple[str, float, dict]] = {}
#: ✅ 2026-10-06（缓存有界化）：上面两份幂等缓存的条目上限。
#: TTL 只保证「过期后不再命中」，不回收内存 —— 每条缓存存的是**整份 payload**
#: （findings + dimensions + stats 全量），且删除方案时无人清理（条目按 scheme_id
#: 索引、永不过期的残条会一直挂着）。长跑进程里按「用过的方案数」无界增长。
#: 取 128：单机方案数在几十~几百量级，TTL 120s 内活跃方案远小于此值，
#: 上限只是兜底防泄漏，正常负载下**永不触碰**（不改变任何命中行为）。
RECENT_CACHE_MAX_ENTRIES = 128


def _evict_recent_cache(store: dict) -> None:
    """给幂等缓存做内存回收：先清过期项，再把仍在的项压到上限内。

    语义保持不变：过期项本就不可能命中（``_PREFLIGHT_RECENT`` / ``_OVERVIEW_RECENT``
    的读侧都判 TTL），提前删除与留着等过期对调用方**完全等价**；未到上限时零淘汰。
    超上限时按落缓存时间淘汰最旧 —— 保留最新一次真实计算的结论。
    """
    if not store:
        return
    now = time.monotonic()
    for key in [k for k, v in store.items() if now - v[1] >= OVERVIEW_CACHE_TTL]:
        store.pop(key, None)
    while len(store) > RECENT_CACHE_MAX_ENTRIES:
        oldest = min(store, key=lambda k: store[k][1])
        store.pop(oldest, None)


def _cache_put(store: dict, scheme_id: str, value: tuple) -> None:
    """写入幂等缓存并立即回收（唯一写入口，保证上限恒成立）。"""
    store[scheme_id] = value
    _evict_recent_cache(store)


def invalidate_overview_cache(scheme_id: str | None = None) -> int:
    """方案（或全部）删除后清理进程内总检/预检幂等缓存，返回清理条数。

    调用方：``routers/schemes.py::delete_scheme`` 与 ``routers/projects.py::delete_project``。
    正文/目录/事实变更**不需要**这里插手 —— 缓存键含内容指纹与事实签名，
    变更后自然不命中；只有「条目指向已不存在的方案」这种死键需要显式回收。

    ⚠️ 刻意**不碰** ``_OVERVIEW_LOCKS``：锁若在协程持有时被删，另一个协程会
    另取一把新锁，两把锁串行化失效（见 ``_overview_lock`` docstring）。
    锁对象只有几十字节且与方案数同阶，不是泄漏面。
    """
    if scheme_id is None:
        n = len(_OVERVIEW_RECENT) + len(_PREFLIGHT_RECENT)
        _OVERVIEW_RECENT.clear()
        _PREFLIGHT_RECENT.clear()
        return n
    n = 1 if _OVERVIEW_RECENT.pop(scheme_id, None) is not None else 0
    n += 1 if _PREFLIGHT_RECENT.pop(scheme_id, None) is not None else 0
    return n


def _overview_lock(scheme_id: str) -> asyncio.Lock:
    """取该方案的总检锁（每个方案一把，串行化同方案的并发总检）。

    字典**不做清理**：一个方案一把锁，占用与方案数同阶（单进程几十 KB），
    远小于「清理时恰好有协程持有锁」导致两个协程各持一把不同锁而永久等待的风险。
    """
    lock = _OVERVIEW_LOCKS.get(scheme_id)
    if lock is None:
        lock = asyncio.Lock()
        _OVERVIEW_LOCKS[scheme_id] = lock
    return lock


async def _content_fingerprint(db, scheme_id: str) -> str:
    """方案正文 / 图表 / 字数预算的内容指纹（G3：判定结论是否过期）。

    口径：章节 (sort_order, level, title, word_count, content) +
    图表 (chart_type, status) + 方案字数预算。任一变化都会改变指纹。
    只用于「结论是否过期」的二元判定，不参与导出缓存命中
    （export.py 有自己那份更细的 _content_fingerprint）。
    计算失败返回空串（空指纹 = 无法判定 = 视为不过期，不误导用户）。
    """
    # ✅ 2026-10-07（单一事实源收口）：实现下沉到 services/scheme_fingerprint。
    #    一致性扫描侧（consistency_scanner）按同一算法落冲突指纹，聚合两侧
    #    才能可靠比较（同一判据两处实现必然再次分叉）。
    from app.services.scheme_fingerprint import content_fingerprint as _fp

    return await _fp(db, scheme_id)


def _consistency_score_note(batch_count: int) -> str:
    """一致性审计评分口径的用户可读说明（R181 · 遗留问题 6，加法式文案）。"""
    try:
        n = int(batch_count or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 1:
        return f"评分口径：本次分 {n} 批审计，取各批最低分（保守口径，整体一致性不高于最差批次）"
    return "评分口径：本次 1 批完成（多批时取各批最低分，保守口径）"


async def _review_state_hash(db, scheme_id: str) -> str:
    """章节审核状态指纹（R181 · B-14）。

    与 :func:`_content_fingerprint` 分开（见 services/scheme_fingerprint.py
    ``review_state_hash`` docstring）：审核通过/驳回不改正文，却会让总检缓存
    与「结论是否仍对应当前审核进度」失真。计算失败返回空串（fail-open）。
    """
    from app.services.scheme_fingerprint import review_state_hash as _rs

    return await _rs(db, scheme_id)


async def _run_is_stale(
    db,
    scheme_id: str,
    row,
    current_fingerprint: str | None = None,
    current_review_hash: str | None = None,
) -> bool:
    """判定一条预检运行记录是否已过期（G3 正文 + R181/B-14 审核状态）。

    历史行没有指纹（本功能上线前的数据）→ 视为**不过期**：无法判定就不打扰用户，
    避免把库里所有旧记录全标成「已过期」的红条。

    ✅ BUG 修复（2026-09-23）：列表场景下旧签名每行重算一次内容指纹
       （N 行 = N 次全表扫描）。现允许调用方预先算好后传入。
       ⚠️ 同时发现调用方（/runs、/report、导出页摘要）的 SELECT 均未取
       content_fingerprint 列 → stored 永远取空 → stale 恒为 False，G3 时效
       判定在三处全部失效。已随本次修复在各调用方补齐取列。

    ✅ R181（B-14）：审核状态单独比较。历史行 ``review_state_hash`` 为空
       （或当前值算不出来）→ 不做审核状态失效判定（fail-open），与正文指纹
       的历史行约定一致。**注意**：/submit 的放行门控不调用本函数的新审核
       维分支（通过动作本身就会改审核状态，纳入会让门控永远过期）。
    """
    try:
        keys = set(row.keys()) if hasattr(row, "keys") else set(row)
        stored = row["content_fingerprint"] if "content_fingerprint" in keys else ""
        stored_review = row["review_state_hash"] if "review_state_hash" in keys else ""
    except (IndexError, KeyError, TypeError):
        stored = ""
        stored_review = ""
    if not stored:
        return False
    if current_fingerprint is None:
        current_fingerprint = await _content_fingerprint(db, scheme_id)
    if bool(current_fingerprint) and stored != current_fingerprint:
        return True
    if not stored_review:
        return False
    if current_review_hash is None:
        current_review_hash = await _review_state_hash(db, scheme_id)
    return bool(current_review_hash) and stored_review != current_review_hash


# @deprecated 孤儿 API：本前端走 /overview（内部已含同一预检引擎且带落库+缓存）；
# 端点保留供脚本/手工诊断（已补幂等锁+缓存），大版本评估清理。
@router.post("/preflight/{scheme_id}", deprecated=True)
async def run_preflight_check(scheme_id: str, db=Depends(get_db), force: bool = False, response: Response = None):
    """程序化预检：确定性规则，无需 AI，秒级返回。

    覆盖空章节 / 孤立节点 / 字数 / 图表完成率 / 控制字符 / 废止标准 /
    口语化残留 / 未闭合围栏 / 计算书缺失 / 章节查重 / 应急预案要素等硬伤。

    ✅ BUG 修复（2026-09-23）：与 /overview 同构的 G2 并发锁 + 幂等缓存。
       此前本端点绕过了锁与缓存，连点会往 preflight_runs 灌入重复历史。
       向后兼容：``force`` 默认 False（新参数，不传的老调用方首次仍全量计算，
       仅命中缓存时行为不同，且返回体新增 ``cached`` 字段供调用方感知）。
    """
    _apply_deprecation_headers(
        response,
        replacement=f"/api/v1/compliance/overview/{scheme_id}",
    )
    async with _overview_lock(scheme_id):
        fingerprint = await _content_fingerprint(db, scheme_id)
        # ✅ R181（B-14）：缓存键并入审核状态指纹 —— 只改 review_status 也必须失效。
        review_hash = await _review_state_hash(db, scheme_id)
        # ✅ 2026-10-03：进程内缓存键并入「已确认事实签名」，事实确认后
        #    TTL 内不再返回旧结论（SAF-08 能及时出现）；落库指纹语义不变。
        # ✅ R207：把「六源写入水位」并入原有事实签名槽位 —— cache_key 仍是
        #    3 元组（既有护栏/消费方形态不变）。AI 检查 / 一致性审计 / 冲突状态 /
        #    评分项 / 全局事实任一写入后，TTL 内也必须重算而非返回旧放行结论。
        _facts_sig = (await _facts_signature(db, scheme_id)) + "|" + (await _sources_signature(db, scheme_id))
        cache_key = (fingerprint, _facts_sig, review_hash)
        hit = _PREFLIGHT_RECENT.get(scheme_id)
        if not force and hit and hit[0] == cache_key and time.monotonic() - hit[1] < OVERVIEW_CACHE_TTL:
            cached = dict(hit[2])
            cached["cached"] = True
            cached["stale"] = False
            return cached
        ctx = await _build_preflight_context(scheme_id, db)
        # ✅ R178（B-4）：预检计算移入工作线程（纯 CPU，不阻塞事件循环）。
        findings = await asyncio.to_thread(_preflight_findings_blocking, ctx)
        stats = await asyncio.to_thread(preflight_stats, ctx)
        stats["standard_db_version"] = STANDARD_DB_VERSION
        stats["standard_db_checked_at"] = STANDARD_DB_CHECKED_AT
        # ✅ R207：与 /overview 同口径 —— 单个 checker 抛错代表「该维度没检查」，
        #    不得折叠成通过（released 必须为 False）。/overview 已在
        #    _readiness_overview_compute 计入 partial_sources，此前本端点漏接。
        _preflight_partial = ["program"] if any(bool(f.get("engine_error")) for f in findings) else []
        result = score_findings(findings, partial_sources=_preflight_partial)
        payload = result.as_dict()
        payload.update(
            {
                "scheme_id": scheme_id,
                "scheme_name": ctx.scheme_name,
                "stats": stats,
                "sources": ["program"],
                "created_at": datetime.now().isoformat(timespec="seconds"),
                # ✅ G3：本次结论对应的内容指纹（落库后供 /runs /report 判定是否过期）
                "content_fingerprint": fingerprint,
                # ✅ R181（B-14，加法式）：本次结论对应的审核状态指纹
                "review_state_hash": review_hash,
                "stale": False,
                "cached": False,
            }
        )
        await _persist_run(db, scheme_id, payload, stats)
        _cache_put(_PREFLIGHT_RECENT, scheme_id, (cache_key, time.monotonic(), payload))
        return payload


@router.post("/overview/{scheme_id}")
async def readiness_overview(scheme_id: str, db=Depends(get_db), force: bool = False):
    """就绪度总览：聚合「程序化预检 + 导出预检 + 最近一次 AI 检查
    + 最近一次一致性审计 + 最近一次专家论证预检」，去重后按六维加权评分，给出放行结论。

    这是用户进入「审核与预检」页后真正需要的那一个数字与那一个结论，
    而不是五份互不相干的报告。

    ✅ G1（2026-09-21）：并入导出预检的问题（export_check 的 issue 经
    ``export_issues_to_findings`` 映射为 DLV-* finding）。导出页与总检页从此
    共用同一份数据与同一套规则词表 —— 此前两套体系互不共享，用户在总检页看到
    B 级，去导出页却被一堆问题拦住，两份报告对不上，用户不知道该信哪个。

    ✅ G2：进程内并发锁 + 幂等缓存。此前连点「一键总检」会往历史趋势里灌入
    多条几乎相同的记录（污染趋势），并发两次还会读到不同批的结果。同内容指纹
    下 TTL 内重复请求直接返回上次结论（``cached=True``）；传 ``force=true``
    强制重算（正文 / 目录变更后前端会自动带上）。

    ✅ G3：结论附内容指纹，展示层（/runs、/report、导出页）据此判定
    「这份结论是否已过期」，而不是把旧结论当成"现在能不能交付"。
    """
    async with _overview_lock(scheme_id):
        fingerprint = await _content_fingerprint(db, scheme_id)
        # ✅ R181（B-14）：审核状态指纹并入缓存键 —— 只改 review_status 也必须失效。
        review_hash = await _review_state_hash(db, scheme_id)
        # ✅ 2026-10-03：进程内缓存键并入「已确认事实签名」（同 /preflight）；
        #    落库 content_fingerprint（G3 时效语义）保持不变。
        # ✅ R207：把「六源写入水位」并入原有事实签名槽位 —— cache_key 仍是
        #    3 元组（既有护栏/消费方形态不变）。AI 检查 / 一致性审计 / 冲突状态 /
        #    评分项 / 全局事实任一写入后，TTL 内也必须重算而非返回旧放行结论。
        _facts_sig = (await _facts_signature(db, scheme_id)) + "|" + (await _sources_signature(db, scheme_id))
        cache_key = (fingerprint, _facts_sig, review_hash)
        hit = _OVERVIEW_RECENT.get(scheme_id)
        if not force and hit and hit[0] == cache_key:
            if time.monotonic() - hit[1] < OVERVIEW_CACHE_TTL:
                cached = dict(hit[2])
                cached["cached"] = True
                cached["stale"] = False
                return cached
        # ✅ P1-4：复用本函数已算好的指纹，避免同一次请求第二次全表扫描
        payload = await _readiness_overview_compute(db, scheme_id, fingerprint=fingerprint, review_hash=review_hash)
        payload["cached"] = False
        payload["stale"] = False
        if not payload.get("content_fingerprint"):
            payload["content_fingerprint"] = fingerprint
        _cache_put(_OVERVIEW_RECENT, scheme_id, (cache_key, time.monotonic(), payload))
        return payload


def _ai_row_is_stale(row: dict, current_fingerprint: str) -> bool:
    """AI 结论行是否已过期（其正文指纹与当前正文不一致）。

    历史行无指纹（本功能上线前写入）→ 视为**不过期**（fail-open）：无法判定
    就不打扰用户，与 :func:`_run_is_stale` 对 preflight_runs 历史行的约定一致。
    当前指纹算不出来（空串）同样 fail-open，避免把整库旧行全判成过期。
    """
    fp = str(row.get("content_fingerprint") or "")
    if not fp or not current_fingerprint:
        return False
    return fp != current_fingerprint


async def _numbering_consistency_findings(db, scheme_id: str) -> tuple[list, bool]:
    """DLV-13：编号一致性（正文↔目录 + 目录结构↔真实树）。

    ✅ R183（2026-11-07 · 结构漂移盲区修复）：旧实现只调
    :func:`validate_scheme_content_numbering`（R185 更名，旧名
    ``validate_scheme_numbering_consistency``）—— 它只解析落库正文，不看
    ``outline_json.id`` 是否等于该章节在真实树（parent_id + sort_order）里的位置。
    于是「第 2 章存储编号被改成 1」这类结构漂移：正文与陈旧编号仍自洽 → 校验器
    报绿 → DLV-13 报绿，而 ``/sections/numbering-consistency``（已并入结构自检）
    与导出侧却能看到问题 —— 同一方案两个相反结论。

    现**只读复用** ``routers/sections.py::_outline_structure_report``（目录模块
    已提交 ``aac9955``），与路由层同一份实现、同一口径，不在本文件复制第二份。

    Returns:
        (findings, checked_any)
        · findings：0 或 1 条 DLV-13（正文漂移与结构漂移合并为同一条，避免
          同 rule_id 在 merge_findings 里被去重丢掉其中一半证据）；
        · checked_any：是否确有章节参与校验（为 False 时不得把 numbering 计为
          「已检查来源」，避免「没检查」被显示成「通过」）。
    """
    from app.routers.sections import _outline_structure_report
    from app.services.numbering import validate_scheme_content_numbering

    num = await validate_scheme_content_numbering(db, scheme_id)
    if num.get("error"):
        raise RuntimeError(str(num["error"]))
    num_bad = [s for s in (num.get("sections") or []) if not s.get("consistent")]
    outline = await _outline_structure_report(db, scheme_id)
    outline_bad = int(outline.get("mismatched") or 0)
    checked_any = bool(num.get("checked")) or bool(outline.get("checked"))
    if not num_bad and not outline_bad:
        return [], checked_any

    parts: list[str] = []
    evidence: list[str] = []
    section_ids: list[str] = []
    if num_bad:
        parts.append(f"{len(num_bad)} 个章节的正文子标题编号与当前目录编号不一致（D4 类漂移）")
        evidence.extend(
            f"{s.get('section_id')}: {len(s.get('diffs') or [])} 处差异" for s in num_bad[:10]
        )
        section_ids.extend(s.get("section_id") for s in num_bad if s.get("section_id"))
    if outline_bad:
        parts.append(
            f"{outline_bad} 个章节的目录结构编号（outline_json.id）与真实树位置不一致（结构漂移）"
        )
        for s in (outline.get("samples") or [])[:10]:
            evidence.append(
                f"{s.get('title')}: 存储 {s.get('stored')}（level {s.get('stored_level')}）"
                f" → 期望 {s.get('expected')}（level {s.get('expected_level')}）"
            )
            if s.get("section_id"):
                section_ids.append(str(s.get("section_id")))

    return (
        [
            {
                "rule_id": "DLV-13",
                "dimension": "deliverability",
                "severity": "high",
                "title": "正文子标题编号与目录/导出编号不一致",
                "detail": "；".join(parts) + "，导出后目录与标题会错位",
                "evidence": evidence[:10],
                "section_id": "",
                "section_ids": section_ids,
                "section_title": "",
                "suggestion": (
                    "调用 POST /api/v1/schemes/{id}/sections/"
                    "numbering-consistency/repair 按当前目录重新编号（含结构编号回写）"
                ),
                "basis": "目录 / 正文编号一致性校验",
                "mode": "program",
            }
        ],
        checked_any,
    )


async def _readiness_overview_compute(
    db,
    scheme_id: str,
    *,
    persist: bool = True,
    fingerprint: str | None = None,
    review_hash: str | None = None,
) -> dict:
    """总检的实际计算（由 readiness_overview 的并发锁与幂等缓存包着）。

    ✅ 2026-10-03（数据链收口）：新增 ``persist`` 形参。**默认 True，既有端点
    调用逐字不变**（向后兼容）。自动修复链路（routers/review_autofix.py 的
    ✅ 2026-10-12（技术债 P1-4）：新增 ``fingerprint`` 形参。默认 ``None`` →
    内部自行计算，既有调用方（review_autofix 三处）行为**逐字不变**。
    ``readiness_overview`` 为判定缓存命中已经算过一次指纹，现把它传进来复用，
    避免同一次请求对 ``sections`` / ``chart_predictions`` / ``global_facts``
    / ``schemes`` 做**两轮全表扫描**；更重要的是杜绝「缓存键用指纹 A、
    落库用指纹 B」的口径分裂（并发下两次扫描之间内容可能已变）。
    plan/apply/collect/stage）此前直接调本函数重算 findings，而本函数末尾
    无条件 ``_persist_run`` 落 preflight_runs —— 用户每点一次「定位/修复/
    收集/暂存」就往分数趋势里灌一条总检历史（污染 G2 要保护的趋势线），
    且不持 ``_overview_lock`` 存在并发写竞态。内部重算只需读 findings，
    传 ``persist=False`` 跳过落库，两个问题同时消除且不改任何判定口径。
    """
    ctx = await _build_preflight_context(scheme_id, db)
    # ✅ R178（B-4）：预检计算移入工作线程（纯 CPU，不阻塞事件循环）。
    program_findings = await asyncio.to_thread(_preflight_findings_blocking, ctx)
    sources = ["program"]
    # ✅ 修复（2026-10-07 · 陈旧 AI 结论计入评分，生产库实证）：
    #    生产方案 10-01 的 AI 合规/一致性结论，在正文已多次修改的 10-07
    #    仍被计入总检评分（CON-04 / SAF-* 指向已不存在的章节），用户据此
    #    「整改」实际不存在的缺陷。AI 各源写入时已记正文指纹，此处比对后
    #    跳过过期行，并把跳过的来源名放进 stale_ai_sources 供前端提示重跑。
    # ✅ BUG 修复（2026-10-14 · R78 F-2）：P1-4（2026-10-12）新增的
    #    ``fingerprint`` 形参此前被下面这行**无条件重算覆盖**——docstring
    #    承诺「复用调用方指纹、避免两轮全表扫描与缓存键/落库指纹口径分裂」，
    #    实际从未兑现（readiness_overview L1104 传入即被丢弃，末尾
    #    ``if fingerprint is not None`` 回退分支因此永真）。现按契约改为
    #    仅缺省时计算；不传参的既有调用方（review_autofix 三处）逐字不变。
    if fingerprint is None:
        fingerprint = await _content_fingerprint(db, scheme_id)
    stale_ai_sources: list[str] = []
    # ✅ 2026-10-12（技术债 P1-6）：新增「**读取失败**」来源清单。
    #    与 ``stale_ai_sources``（正文已变 → 结论过期，用户重跑即可）刻意分开：
    #    前者是**系统故障**，重跑也不一定好，两类处置方式不同，混在一个键里
    #    前端只能给一句模棱两可的提示。旧实现这类失败只有一行 warning，
    #    前端看到的是「分数正常、来源齐全」，实际某一路根本没参与评分 ——
    #    分数静默偏高且无任何信号。现加法式上报（不改动既有键的语义与取值）。
    failed_sources: list[str] = []
    # ✅ B-01（R175）：**部分检查**的来源清单。与 failed_sources（完全没跑成）
    #    和 stale_ai_sources（正文已变、结论过期）都不同：这里来源跑成了，但
    #    只覆盖了正文的一部分（如 /check 6000 字硬上限外的章节根本没进模型）。
    #    前端/消费方据此显示「部分检查」，避免把 partial 当完整通过。
    partial_sources: list[str] = []
    # ✅ R183（2026-11-07 · 三态口径修复）：程序化检查项异常 = 该维度**没检查**。
    #    旧实现把 check_* 抛错降级为一条 low 级 engine_error finding（-3 分），
    #    总分仍可能 ≥75、grade 仍为 A/B，released 仍为 True —— 用户看到
    #    「建议放行」，实际有体检项根本没跑（「没检查」被折叠成「通过」）。
    #    现把异常计入 partial_sources，交给 score_findings 统一置 incomplete，
    #    released 必为 False、verdict 追加「检查不完整，不得据此放行」。
    #    注：单条 low finding 仍保留（用户可见具体是哪个 checker 崩了）。
    if any(bool(f.get("engine_error")) for f in program_findings):
        partial_sources.append("program")

    # --- ✅ G1：导出预检问题并入评分（与程序化预检共用同一套规则词表）---
    # 此前导出预检是独立体系，其问题既不进 preflight_runs 也不进本聚合，
    # 于是"总检说能交付、导出却被拦住"。现两处共用 collect_export_issues 的
    # 同一份判定；导出页也回 preflight_summary，反向打通。
    export_findings: list = []
    try:
        from app.routers.export import collect_export_issues, export_issues_to_findings

        # ✅ BUG 修复（2026-10-17）：必须传 strict_read=True，否则下面这个
        #    except 对「DB 读失败」这一形态**结构性不可达** —— collect_export_issues
        #    内部四路查询原本一律 fail-soft 降级为空结果集，DB 瞬时故障时
        #    export_check 这一路 findings 整体消失、异常也不抛 → 本来源既不进
        #    sources 也不进 failed_sources → 用户看到「六路正常」而分数虚高。
        _exp = await collect_export_issues(scheme_id, db, strict_read=True)
        export_findings = export_issues_to_findings(_exp.get("issues") or [])
        if export_findings:
            sources.append("export_check")
        # ✅ R185（2026-11-07 · 三态口径）：导出预检里的「无法判定」子项
        #    同样不得折叠成通过。检测器崩溃时 issues 里没有该类问题（零发现），
        #    若只看 findings 就是「没冲突」；此处按执行态计入 partial_sources，
        #    使 incomplete=True、released=False。
        # ✅ R207：R183/R185 的三态此前只覆盖 subheading_check；导出预检同时
        #    会产出 crossref_check.undetermined（方案无图表但正文引用「图 9-9」等
        #    → 无法判定），/export/check 已显式提示「不代表通过」，总检却把它
        #    折叠成「无问题」甚至 released=True。两路同口径计入 partial_sources。
        _exp_undetermined = bool((_exp.get("subheading_check") or {}).get("undetermined")) or bool(
            (_exp.get("crossref_check") or {}).get("undetermined")
        )
        if _exp_undetermined and "export_check" not in partial_sources:
            partial_sources.append("export_check")
    except Exception as e:
        # ✅ P1-6：读取失败必须让用户可见（否则该来源静默不参与评分、分数虚高）
        failed_sources.append("export_check")
        logger.warning("overview: 导出预检问题聚合失败（跳过，不阻断总检）: %s", e, exc_info=True)

    # --- AI 规范符合性：取最近一批（同一批 = 同一次 /check 调用写入的行）---
    ai_findings: list = []
    try:
        # ✅ 根治批次截头（2026-09-23）：优先按 batch_id 取最新一批（写入端已保证
        #    一次 /check 一个批号，跨秒/交错都不受影响）。历史行无批号时回退旧口径：
        #    「同一 created_at 视为一批」+ rowid 连续段锚定（created_at 仅秒级精度，
        #    同秒两批会误聚合；rowid 严格递增只能保证单次调用内连续）。
        _brow = await review_db.fetch_one(
            db,
            "SELECT batch_id FROM compliance_check WHERE scheme_id=?"
            " AND check_type='compliance' AND batch_id != ''"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (scheme_id,),
            what="总检聚合：定位最近一批合规结果",
        )
        _batch = (_brow["batch_id"] if _brow else "") or ""
        if _batch:
            _rows_raw = await review_db.fetch_all(
                db,
                "SELECT result, content_fingerprint FROM compliance_check WHERE scheme_id=?"
                " AND check_type='compliance' AND batch_id=? ORDER BY rowid",
                (scheme_id, _batch),
                what="总检聚合：读取该批合规结果",
            )
        else:
            last = await review_db.fetch_one(
                db,
                "SELECT MIN(rowid) AS min_rowid, created_at"
                " FROM compliance_check WHERE scheme_id=? AND check_type='compliance' "
                "GROUP BY created_at ORDER BY created_at DESC LIMIT 1",
                (scheme_id,),
                what="总检聚合：定位历史行连续段锚点",
            )
            _rows_raw = (
                await review_db.fetch_all(
                    db,
                    "SELECT result, content_fingerprint FROM compliance_check WHERE scheme_id=? AND check_type='compliance'"
                    " AND rowid >= ? ORDER BY rowid",
                    (scheme_id, last["min_rowid"]),
                    what="总检聚合：读取历史行连续段",
                )
                if last
                else []
            )
        # ✅ R13（2026-10-06）：旧实现把游标本身当判据（`if cur is not None`），
        #    而 `db.execute` 返回 None 时恰好命中「跳过」分支 —— 表面无害，
        #    但同段的 `cur.fetchone()` 会先抛 AttributeError，整段降级；
        #    现统一走 review_db，读失败由本段 except 降级为「跳过该来源」。
        if _rows_raw:
            rows = []
            _stale_n = 0
            for r in _rows_raw:
                if _ai_row_is_stale(r, fingerprint):
                    _stale_n += 1  # 正文已变：该结论不再计入评分
                    continue
                try:
                    rows.append(json.loads(r["result"] or "{}"))
                except json.JSONDecodeError as _exc:
                    # 仅跳过该行、不降级整段（行为不变）；但损坏必须可观测，
                    # 否则该结论静默消失、总检分数无声偏高
                    logger.warning(
                        "overview: AI 合规结论 JSON 损坏（已跳过该行，不计分） scheme=%s err=%s",
                        scheme_id,
                        _exc,
                    )
                    continue
            if _stale_n:
                stale_ai_sources.append("compliance")
                logger.warning(
                    "overview: %d 条 AI 合规结论正文指纹不一致（已跳过不计分）scheme=%s", _stale_n, scheme_id
                )
            ai_findings = ai_results_to_findings(rows)
            # ✅ B-01：该批结果行带 partial=true → 本次 AI 检查只覆盖部分正文。
            if any(isinstance(x, dict) and x.get("partial") for x in rows):
                partial_sources.append("compliance")
            if rows:
                sources.append("compliance")
    except Exception as e:
        failed_sources.append("compliance")
        logger.warning("overview: 读取 AI 规范符合性结果失败（跳过）: %s", e, exc_info=True)

    # --- 最近一次一致性审计（作为一致性维度的补充证据）---
    try:
        row = await review_db.fetch_one(
            db,
            "SELECT score, issues, content_fingerprint FROM consistency_audit "
            "WHERE scheme_id=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (scheme_id,),
            what="总检聚合：读取最近一次一致性审计",
        )
        _audit_stale = bool(row) and _ai_row_is_stale(row, fingerprint)
        if _audit_stale:
            stale_ai_sources.append("consistency")
            logger.warning("overview: 一致性审计结论正文指纹不一致（已跳过）scheme=%s", scheme_id)
        if row and not _audit_stale:
            _audit_corrupt = False
            try:
                issues = json.loads(row["issues"] or "[]")
            except json.JSONDecodeError as _exc:
                # ✅ R207：结论 JSON 损坏 = 该来源没读到，旧实现静默当「零发现」
                #    （不进 sources / failed_sources，分数无声偏高）；与同文件
                #    AI 合规段的损坏留痕口径对齐，并显式计入 failed_sources。
                issues = []
                _audit_corrupt = True
                logger.warning(
                    "overview: 一致性审计结论 JSON 损坏（按读取失败处理）scheme=%s err=%s", scheme_id, _exc
                )
                if "consistency" not in failed_sources:
                    failed_sources.append("consistency")
            for idx, it in enumerate(issues):
                # ✅ R207：与 /check 合并段、audit_scoring 共用同一归一出口
                #    （中文「高/中/低/阻断/严重」+ block），旧实现把中文档位
                #    一律降为 medium、把 block(40 分) 降为 medium(8 分)。
                sev = normalize_severity_text(it.get("severity") or "medium")
                # ✅ 既有契约（test_readiness_overview_six_sources）：consistency
                #    源不得产出 block 级 finding（AI/扫描不得自行判交付阻断），
                #    故归一后的 block 仍按历史口径降为 medium；本轮修复的是
                #    中文「高/中/低」此前被 .lower() 一律误降为 medium。
                if sev not in ("high", "medium", "low"):
                    sev = "medium"
                # ✅ 修复（2026-09-17）：每条一致性审计 issue 使用唯一 rule_id，
                #    旧实现全部并入 CON-04 被 merge_findings 按 rule_id 去重塌缩为
                #    1 条，导致一份方案有多处不一致时一致性维度只扣一次分、总分虚高。
                #    现逐条计入一致性维度扣分，与「看了报告仍不知道能不能交付」的
                #    设计目标一致（每条不一致都是独立的交付风险）。
                ai_findings.append(
                    {
                        "rule_id": f"CON-04-{idx + 1}",
                        "dimension": "consistency",
                        "severity": sev,
                        "title": f"一致性审计：{it.get('dimension') or '不一致项'}",
                        "detail": it.get("content_quote") or it.get("fact") or "",
                        "evidence": [it.get("fact") or "", it.get("content_quote") or ""],
                        "section_id": "",
                        "section_title": it.get("section_title") or "",
                        "suggestion": it.get("suggestion") or "",
                        "basis": "全文一致性审计（AI）",
                        "mode": "ai",
                    }
                )
            if not _audit_corrupt and (issues or row["score"] is not None):
                sources.append("consistency")
    except Exception as e:
        failed_sources.append("consistency")
        logger.warning("overview: 读取一致性审计结果失败（跳过）: %s", e, exc_info=True)

    # --- ✅ B-17（R175）：编号一致性进入总检 ---
    # 此前 /numbering-consistency 是独立体系：它报「正文子标题编号与目录不一致」，
    # 总检却完全不知情，用户在两处看到互相矛盾的结论（一处说漂移、一处说可交付）。
    # 现把同一校验器的结果以 DLV-13（交付物编号一致性）并入评分，口径单一。
    # ✅ R183（2026-11-07 · 结构漂移盲区）：正文校验器只解析正文、不读提纲结构，
    #    而 /sections/numbering-consistency 已把「目录结构编号 vs 真实树位置」并入
    #    结论。总检若只调正文校验器，结构漂移时 DLV-13 报绿、路由层报不通过 ——
    #    同一方案两个相反结论。现**只读复用** sections.py::_outline_structure_report
    #    （目录模块已提交 aac9955），不复制第二份实现。
    try:
        _num_findings, _num_checked = await _numbering_consistency_findings(db, scheme_id)
        ai_findings.extend(_num_findings)
        if _num_checked:
            sources.append("numbering")
    except Exception as e:
        if "numbering" not in failed_sources:
            failed_sources.append("numbering")
        logger.warning("overview: 编号一致性校验异常（跳过）: %s", e, exc_info=True)

    # --- ✅ 新增（2026-09-17）：最近一次全文一致性扫描（规则 + 仲裁）未解决冲突 ---
    # 此前「一致性扫描 → 修复工作台」产出的冲突清单（consistency_conflicts）完全不参与
    # 就绪度评分 —— 扫描发现高危数值冲突，总览分数却纹丝不动，两个体系断链。
    # 只统计最近一批扫描中 status IN ('pending','failed') 的未解决项；
    # repaired/accepted（已解决）与 skipped（用户明确不处理）不计分。
    try:
        last_scan = await review_db.fetch_one(
            db,
            "SELECT scan_id, content_fingerprint FROM consistency_conflicts "
            "WHERE scheme_id=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (scheme_id,),
            what="总检聚合：定位最近一批一致性扫描",
        )
        _scan_stale = bool(last_scan) and _ai_row_is_stale(last_scan, fingerprint)
        if _scan_stale:
            stale_ai_sources.append("consistency_scan")
            logger.warning("overview: 一致性扫描结论正文指纹不一致（已跳过）scheme=%s", scheme_id)
        if last_scan and not _scan_stale:
            scan_conflicts = await review_db.fetch_all(
                db,
                "SELECT conflict_type, severity, topic, occurrences,"
                " authoritative_value, authoritative_source, repair_instruction,"
                " reason, status"
                " FROM consistency_conflicts WHERE scheme_id=? AND scan_id=?"
                " ORDER BY created_at, rowid",
                (scheme_id, last_scan["scan_id"]),
                what="总检聚合：读取一致性扫描冲突",
            )
            unresolved = [c for c in scan_conflicts if c.get("status") in ("pending", "failed")]
            for idx, c in enumerate(unresolved):
                # ✅ R207：同上，统一走 audit_scoring.normalize_severity_text。
                sev = normalize_severity_text(c.get("severity") or "medium")
                # ✅ 既有契约（test_readiness_overview_six_sources）：consistency
                #    源不得产出 block 级 finding（AI/扫描不得自行判交付阻断），
                #    故归一后的 block 仍按历史口径降为 medium；本轮修复的是
                #    中文「高/中/低」此前被 .lower() 一律误降为 medium。
                if sev not in ("high", "medium", "low"):
                    sev = "medium"
                try:
                    occ = json.loads(c.get("occurrences") or "[]")
                except json.JSONDecodeError:
                    occ = []
                if not isinstance(occ, list):
                    occ = []
                topic = c.get("topic") or ""
                reason = str(c.get("reason") or "").strip()
                auth = str(c.get("authoritative_value") or "").strip()
                auth_src = str(c.get("authoritative_source") or "").strip()
                # ✅ 修复（2026-10-13 · R76）：detail 权威值与仲裁理由都要露出来。
                #   旧实现 ``detail = c.get("reason") or c.get("topic")`` —— 但 SQL
                #   的 SELECT **漏了 reason 列**，``c.get("reason")`` 恒为 None，detail
                #   永远退化为 topic，与 section_title 逐字重复（用户在总检页和整改
                #   报告里看到同一句话两遍）；authoritative_value 明明被 SELECT 出来
                #   却从未用于 finding —— 而 CON-07 的规则描述正是「以权威值为准修复」，
                #   用户拿到结论却看不到「该改成什么」，整改建议不可执行。
                detail_parts: list = []
                if reason and reason != topic:
                    detail_parts.append(reason)
                if auth:
                    detail_parts.append(f"权威值：{auth}（{auth_src}）" if auth_src else f"权威值：{auth}")
                detail = "；".join(detail_parts) or topic
                ai_findings.append(
                    {
                        "rule_id": f"CON-SCAN-{idx + 1}",
                        "dimension": "consistency",
                        "severity": sev,
                        "title": f"一致性扫描：{c.get('conflict_type') or '数值冲突'}",
                        "detail": detail,
                        "evidence": [str(o) for o in occ],
                        "section_id": "",
                        "section_title": topic,
                        # 权威值单独成字段（/report 与前端可各自消费）。刻意不塞进
                        # suggestion —— suggestion 是「怎么改」的指令（repair_instruction），
                        # 权威值是「改成什么」的目标，混在一起会让 repair_instruction 缺失时
                        # 用户看到一句孤立的值当指令。
                        "authoritative_value": auth,
                        "authoritative_source": auth_src,
                        "suggestion": c.get("repair_instruction") or "",
                        "basis": "全文一致性扫描（规则+仲裁）",
                        "mode": "program",
                    }
                )
            sources.append("consistency_scan")
    except Exception as e:
        failed_sources.append("consistency_scan")
        logger.warning("overview: 读取一致性扫描冲突失败（跳过）: %s", e, exc_info=True)

    # --- 最近一次专家论证预检 ---
    try:
        row = await review_db.fetch_one(
            db,
            "SELECT result, content_fingerprint FROM compliance_check "
            "WHERE scheme_id=? AND check_type='expert_review'"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (scheme_id,),
            what="总检聚合：读取最近一次专家论证预检",
        )
        _expert_stale = bool(row) and _ai_row_is_stale(row, fingerprint)
        if _expert_stale:
            stale_ai_sources.append("expert_review")
            logger.warning("overview: 专家论证预检结论正文指纹不一致（已跳过）scheme=%s", scheme_id)
        if row and not _expert_stale:
            try:
                expert = json.loads(row["result"] or "{}")
            except json.JSONDecodeError as _exc:
                # ✅ R207：专家论证结论损坏时旧实现静默当「没有这一路来源」
                #    （既不在 sources 也不在 failed_sources，总检照样放行）。
                expert = {}
                logger.warning(
                    "overview: 专家论证预检结论 JSON 损坏（按读取失败处理）scheme=%s err=%s", scheme_id, _exc
                )
                if "expert_review" not in failed_sources:
                    failed_sources.append("expert_review")
            if expert:
                ai_findings.extend(expert_result_to_findings(expert))
                sources.append("expert_review")
    except Exception as e:
        failed_sources.append("expert_review")
        logger.warning("overview: 读取专家论证预检结果失败（跳过）: %s", e, exc_info=True)

    # --- ✅ R198（R197 遗留②）：评分项覆盖第七源（「该查未查」的该查侧）---
    #     评分项（招标文件评分标准）覆盖此前只在 /scoring-coverage 独立成页，
    #     总检完全不知情：方案整体漏掉多个评分项，总检分数照常可放行 ——
    #     用户看了总检放心交付、评标时才发现缺项。现把同一判定以 CMP-SC-1
    #     （完整性维度）并入评分；口径单一（复用 scoring.py::scoring_coverage_state
    #     → scoring_items.build_coverage_report，不复制第二份判据）。
    #     向后兼容：项目从未启用评分项体系（无提取运行记录）→ 不进 sources、
    #     不计分、不产 finding，既有方案的总检结果逐字不变。
    try:
        from app.routers.scoring import scoring_coverage_state

        sc_state = await scoring_coverage_state(db, scheme_id)
        if sc_state["applicable"]:
            if sc_state["status"] == "undetermined":
                # ✅ B-01 同口径：体系已启用但本次不可判定 → partial_sources
                #    （不产具体 finding，但 released 必须为 False）
                partial_sources.append("scoring_coverage")
            else:
                sources.append("scoring_coverage")
                _miss = int(sc_state.get("missing_count") or 0)
                if _miss > 0:
                    _rate = sc_state.get("coverage_rate")
                    _sev = "high" if (_rate is not None and float(_rate) < 0.6) or _miss >= 5 else "medium"
                    _names = "、".join(filter(None, sc_state.get("missing_names") or []))
                    ai_findings.append(
                        {
                            "rule_id": "CMP-SC-1",
                            "dimension": "completeness",
                            "severity": _sev,
                            "title": f"评分项覆盖缺失：{_miss}/{sc_state.get('total_items', 0)} 项未在方案中体现",
                            "detail": (_names or "评分项未匹配到任何章节") + (
                                f"（覆盖率 {round(float(_rate) * 100)}%）" if _rate is not None else ""
                            ),
                            "evidence": list(sc_state.get("missing_names") or []),
                            "section_id": "",
                            "section_title": "",
                            "suggestion": "到「评分项」页核对缺失项；为缺失评分项补充对应章节或内容",
                            "basis": "评分项覆盖校验（与 /scoring-coverage 同一判据）",
                            "mode": "program",
                        }
                    )
    except Exception as e:
        if "scoring_coverage" not in failed_sources:
            failed_sources.append("scoring_coverage")
        logger.warning("overview: 评分项覆盖状态读取失败（跳过，不阻断总检）: %s", e, exc_info=True)

    findings = merge_findings(program_findings, ai_findings, export_findings)
    # ✅ 自动修复能力标注（services/review_autofix.py）：为每条 finding 补
    #    ``autofix`` 字段（mode / fixable / reason），前端据此渲染「一键修复」
    #    按钮。只新增字段，既有字段与评分口径完全不变（向后兼容）。
    review_autofix.capability_summary(findings)
    # ✅ B-04（R175）：失败/部分来源必须参与放行判定 —— 此前只回传展示字段，
    #    ``released`` 仍可为 True（导出预检崩溃时 DLV-09~15 静默缺席却仍能放行）。
    result = score_findings(findings, failed_sources=failed_sources, partial_sources=partial_sources)
    # ✅ R178（B-4）：统计聚合同样移入工作线程（口径不变）。
    stats = await asyncio.to_thread(preflight_stats, ctx)
    stats["standard_db_version"] = STANDARD_DB_VERSION
    stats["standard_db_checked_at"] = STANDARD_DB_CHECKED_AT
    # ✅ G1：单列导出预检命中数，前端可说明"这分里有几项来自导出预检"
    # ✅ R207：按 issue 自带 count 求和（缺失回退聚合条数），20 章待审核的
    #    聚合 issue 不再显示成「1 项」；hit_count 由 export_issues_to_findings 产出。
    stats["export_issue_count"] = sum(int(f.get("hit_count") or f.get("count") or 0) for f in export_findings)
    payload = result.as_dict()
    payload.update(
        {
            "scheme_id": scheme_id,
            "scheme_name": ctx.scheme_name,
            "stats": stats,
            "sources": sources,
            # ✅ 2026-10-07：因正文已变而跳过的 AI 来源（提示用户重跑对应 AI 检查）
            "stale_ai_sources": stale_ai_sources,
            # ✅ 2026-10-12（P1-6）：**读取失败**而未参与评分的来源（系统故障，
            #    与上面「结论过期」是两类问题）。为空即表示六路来源全部正常参与。
            "failed_sources": failed_sources,
            # ✅ B-01（加法式）：跑成但未覆盖全文的来源。为空即表示所有已参与
            #    评分的来源都是完整检查。
            "partial_sources": partial_sources,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            # ✅ G3：落库后供 /runs /report / 导出页判定「结论是否已过期」
            # ✅ P1-4：调用方已传入指纹时直接复用（同一请求只扫一次库）；
            #    未传（review_autofix 等既有调用方）时仍自行计算，行为不变。
            "content_fingerprint": (
                fingerprint if fingerprint is not None else await _content_fingerprint(db, scheme_id)
            ),
            # ✅ R181（B-14，加法式）：审核状态指纹。调用方已算好则复用（同一次
            #    请求不重复扫库）；既有调用方（review_autofix）不传 → 自行计算。
            "review_state_hash": (review_hash if review_hash is not None else await _review_state_hash(db, scheme_id)),
        }
    )
    if persist:
        await _persist_run(db, scheme_id, payload, stats)
    return payload


async def _persist_run(db, scheme_id: str, payload: dict, stats: dict):
    """落库一次预检运行（用于分数趋势与审计留痕）。

    落库失败不影响返回结果 —— 评分结论是用户此刻要的东西，
    不能因为历史表写入异常就让用户拿不到结论。

    ✅ R13（2026-10-06）：INSERT 走 ``review_db.exec_write``。旧实现丢弃
    返回值，``db.execute`` 返回 None **不是异常** → commit 照常成功、
    无任何日志 → 分数趋势**静默丢一条记录**。这正是本函数 2026-09-21 建立
    日志时想根治的那类问题，但只覆盖了「抛异常」分支，R13 这条静默路径
    依然无观测。exec_write 会打 WARNING，且 rowcount==0 时抛 503 被本函数的
    ``except`` 降级为「记录一条 warning + rollback」—— 结论照常返回。
    """
    try:
        # ✅ G5（2026-09-21）：preflight_runs 补齐 project_id（此前无此列），
        # 与 compliance_check / consistency_audit 的项目维度口径对齐；
        # ✅ G3：内容指纹随运行落库，展示层据此判定结论是否已过期。
        from app.routers.review import _scheme_project_id

        await review_db.exec_write(
            db,
            "INSERT INTO preflight_runs (id, scheme_id, project_id, content_fingerprint,"
            " review_state_hash, rule_version, total, grade, verdict, released, blocked,"
            " counts, dimensions, findings, stats, incomplete, incomplete_sources)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                scheme_id,
                await _scheme_project_id(db, scheme_id),
                payload.get("content_fingerprint") or "",
                # ✅ R181（B-14）：审核状态指纹随运行落库
                payload.get("review_state_hash") or "",
                RULE_VERSION,
                payload.get("total", 0),
                payload.get("grade", ""),
                payload.get("verdict", ""),
                1 if payload.get("released") else 0,
                1 if payload.get("blocked") else 0,
                json.dumps(payload.get("counts") or {}, ensure_ascii=False),
                json.dumps(payload.get("dimensions") or [], ensure_ascii=False),
                json.dumps(payload.get("findings") or [], ensure_ascii=False),
                json.dumps(stats or {}, ensure_ascii=False),
                # ✅ R185：落库的必须是**执行时**那一个值（payload 即 score_findings
                #    的产物），不得在读取端按来源列表重算 —— 否则历史回看会与
                #    当时看到的结论分叉。
                1 if payload.get("incomplete") else 0,
                json.dumps(
                    {
                        "failed_sources": payload.get("failed_sources") or [],
                        "partial_sources": payload.get("partial_sources") or [],
                    },
                    ensure_ascii=False,
                ),
            ),
            what=f"写入预检运行记录（scheme={scheme_id[:8]}）",
        )
        await db.commit()
    except Exception as e:
        # ✅ BUG 修复（2026-09-21）：旧实现 `except Exception: pass` 静默吞错，
        #    磁盘满 / 连接 poisoned / JSON 序列化失败全无声息，运维无从排查；
        #    且当 INSERT 抛异常但连接事务状态未回滚时，后续请求会踩到
        #    "transaction already in progress"。现至少记录日志并尝试 rollback。
        logger.warning("preflight_runs 写入失败（不影响评分返回）: scheme=%s err=%s", scheme_id, e)
        try:
            await db.rollback()
        except Exception as _re:
            logger.warning("preflight_runs rollback 也失败: %s", _re)


@router.get("/runs/{scheme_id}")
async def list_preflight_runs(scheme_id: str, limit: int = 10, db=Depends(get_db)):
    """预检历史（分数趋势）。

    只看一次分数没有意义 —— 用户真正关心的是"我改完之后分数涨了没"。

    ✅ G3（2026-09-21）：每条记录附 ``stale``。此前趋势图会把基于旧正文的
    高分当成"整改后分数更高了"，而实际正文早已改过、结论已失效。
    """
    limit = max(1, min(limit, 50))
    # ✅ BUG 修复（2026-09-23）：① SELECT 补上 content_fingerprint 列
    #    （旧实现未取该列 → _run_is_stale 永远判不出过期，stale 恒为 False）；
    #    ② 当前指纹一次算好传给逐行判定，消除 N+1 全表扫描。
    rows = await review_db.fetch_all(
        db,
        "SELECT id, total, grade, verdict, released, blocked, counts, rule_version,"
        " content_fingerprint, review_state_hash, incomplete, incomplete_sources,"
        " created_at FROM preflight_runs WHERE scheme_id=?"
        " ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (scheme_id, limit),
        what="预检历史：读取运行记录",
    )
    current_fp: str | None = None
    current_review: str | None = None
    items = []
    for r in rows:
        item = r
        try:
            item["counts"] = json.loads(item.get("counts") or "{}")
        except json.JSONDecodeError:
            item["counts"] = {}
        # ✅ R185：原因原样回显（执行时写入的 JSON），不在读取端按来源重算。
        #    历史行（本列上线前）该列为 NULL/'' → 两个清单均为空、incomplete 为 None。
        try:
            _src = json.loads(item.get("incomplete_sources") or "{}")
        except (json.JSONDecodeError, TypeError):
            _src = {}
        if not isinstance(_src, dict):
            _src = {}
        item["failed_sources"] = [str(x) for x in (_src.get("failed_sources") or [])]
        item["partial_sources"] = [str(x) for x in (_src.get("partial_sources") or [])]
        if current_fp is None and (item.get("content_fingerprint") or ""):
            current_fp = await _content_fingerprint(db, scheme_id)
        # ✅ R181（B-14）：当前审核状态指纹一次算好，逐行复用
        if current_review is None and (item.get("review_state_hash") or ""):
            current_review = await _review_state_hash(db, scheme_id)
        item["stale"] = await _run_is_stale(
            db,
            scheme_id,
            item,
            current_fingerprint=current_fp,
            current_review_hash=current_review,
        )
        items.append(item)
    return {"items": items}


@router.get("/report/{scheme_id}")
async def readiness_report(scheme_id: str, fmt: str = "markdown", db=Depends(get_db)):
    """生成整改清单报告（Markdown），供复制进评审意见 / 整改通知单。

    商业级审查工具的标配：结论不能只留在软件里，必须能带走。
    """
    row = await review_db.fetch_one(
        db,
        "SELECT total, grade, verdict, released, blocked, dimensions, findings,"
        " stats, content_fingerprint, review_state_hash, created_at FROM preflight_runs WHERE scheme_id=?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (scheme_id,),
        what="整改清单报告：读取最近一次预检记录",
    )
    if not row:
        raise HTTPException(404, "尚无预检记录，请先执行「一键总检」")
    # ✅ BUG 修复（NameError → 500）：旧实现只写了 isinstance 守卫，却**从未从 row
    #    给 dims / findings / stats 赋值**（且同一段守卫生成了两份完全重复的代码），
    #    该端点一执行就抛 `NameError: name 'dims' is not defined`。
    #    现补上取值，并兼容字段为 NULL 的历史记录。
    # ✅ 兼容旧格式：若字段本身已是 dict/list（如迁移过程中写入），json.loads 会抛
    #    异常；此处用 isinstance 守卫避免无意义 500。
    dims = row["dimensions"] if row["dimensions"] is not None else "[]"
    findings = row["findings"] if row["findings"] is not None else "[]"
    stats = row["stats"] if row["stats"] is not None else "{}"
    if isinstance(dims, str):
        try:
            dims = json.loads(dims)
        except json.JSONDecodeError:
            dims = []
    if isinstance(findings, str):
        try:
            findings = json.loads(findings)
        except json.JSONDecodeError:
            findings = []
    if isinstance(stats, str):
        try:
            stats = json.loads(stats)
        except json.JSONDecodeError:
            stats = {}
    # 历史迁移/外部写库可能留下 JSON 标量或错误类型；报告链路只接受约定容器。
    if not isinstance(dims, list):
        dims = []
    if not isinstance(findings, list):
        findings = []
    findings = [f for f in findings if isinstance(f, dict)]
    dims = [d for d in dims if isinstance(d, dict)]
    if not isinstance(stats, dict):
        stats = {}
    try:
        total_value = float(row["total"])
        if total_value != total_value or total_value in (float("inf"), float("-inf")):
            total_value = 0.0
    except (TypeError, ValueError):
        total_value = 0.0

    sc_row = await review_db.fetch_one(
        db, "SELECT name, type FROM schemes WHERE id=?", (scheme_id,), what="整改清单报告：读取方案名"
    )
    name = sc_row["name"] if sc_row else ""

    # ✅ G3（2026-09-21）：报告可能基于旧正文 —— 整改清单被抄进评审意见后，
    # 若正文已改过，按旧结论整改就是白干。报告头部显式标注时效状态。
    stale = await _run_is_stale(db, scheme_id, row)
    stale_line = (
        "- ⚠️ 时效状态：**已过期**（本次预检之后正文 / 图表 / 审核状态已发生变更，"
        "结论可能失效，请先重新执行「一键总检」）"
        if stale
        else "- 时效状态：有效（正文 / 图表 / 审核状态与本次预检一致）"
    )

    lines = [
        "# 专项方案审核预检报告",
        "",
        f"- 方案名称：{name}",
        f"- 方案类型：{sc_row['type'] if sc_row else ''}",
        f"- 预检时间：{row['created_at']}",
        stale_line,
        f"- 综合评分：**{total_value:.1f} / 100**（等级 {row['grade'] or '—'}）",
        f"- 结论：{row['verdict']}",
        f"- 是否建议放行：{'是' if row['released'] else '否'}",
        f"- 标准库版本：{STANDARD_DB_VERSION}（核对于 {STANDARD_DB_CHECKED_AT}）",
        "",
        "## 一、维度得分",
        "",
        "| 维度 | 权重 | 得分 | 问题数 |",
        "| --- | --- | --- | --- |",
    ]
    for d in dims:
        lines.append(f"| {d.get('label')} | {d.get('weight')} | {d.get('score')} " f"| {d.get('issue_count')} |")

    blockers = [f for f in findings if f.get("severity") == "block"]
    if blockers:
        lines += ["", "## 二、交付阻断项（须整改）", ""]
        for i, f in enumerate(blockers, 1):
            lines.append(f"{i}. **{f.get('title')}**（{f.get('rule_id')}）" f"：{f.get('detail')}")
            if f.get("suggestion"):
                lines.append(f"   - 整改建议：{f.get('suggestion')}")

    others = [f for f in findings if f.get("severity") != "block"]
    if others:
        lines += ["", "## 三、其他问题清单", ""]
        for i, f in enumerate(others, 1):
            sev = {"high": "严重", "medium": "一般", "low": "提示"}.get(f.get("severity"), f.get("severity"))
            lines.append(f"{i}. [{sev}] **{f.get('title')}**：{f.get('detail')}")
            if f.get("suggestion"):
                lines.append(f"   - 建议：{f.get('suggestion')}")

    lines += ["", "## 四、客观统计", ""]
    lines.append(f"- 章节数：{stats.get('section_count', 0)}" f"（已生成 {stats.get('generated_count', 0)}）")
    lines.append(f"- 总字数：{stats.get('total_words', 0)}")
    lines.append(f"- 图表完成：{stats.get('chart_done', 0)}/{stats.get('chart_total', 0)}")

    md = "\n".join(lines)
    if fmt == "json":
        return {
            "markdown": md,
            "total": total_value,
            "grade": row["grade"] or "—",
            "findings": findings,
            "dimensions": dims,
            "stats": stats,
            "stale": stale,
            "created_at": row["created_at"],
        }
    return {
        "format": "markdown",
        "filename": f"{name}_审核预检报告.md",
        "content": md,
        "stale": stale,
        "created_at": row["created_at"],
    }
