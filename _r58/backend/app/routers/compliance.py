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
import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response

from app.db import get_db
from app.models import ComplianceCheckIn, ExpertReviewIn
from app.services import review_autofix
# ✅ R13 判空单一出口（2026-10-06）：本模块所有 db.execute 走 review_db，
#    读失败 503 / 写没生效 503。静态护栏
#    tests/test_review_r13_closeout_20261006.py 禁止退回裸调用。
from app.services import review_db
from app.services.ai.json_response import collect_json_response
from app.services.ai.prompts._registry import render
from app.services.audit_rules import (
    EXPERT_CHECK_ITEMS,
    RULE_VERSION,
    ai_rules,
    dimension_catalog,
    expert_items,
    get_rule,
    rule_catalog,
)
from app.services.audit_scoring import (
    ai_results_to_findings,
    expert_result_to_findings,
    merge_findings,
    score_findings,
)
from app.services.content_utils import order_sections_dfs

# ✅ 2026-10-03（全局事实桥接）：预检消费「已确认全局事实」（SAF-08 事实反哺）。
#    门控/作用域口径一律取 facts_extractor 唯一出口，不在本文件重写 SQL。
from app.services.facts_extractor import (
    get_facts_inject_where,
    load_resolved_facts_for_scope,
    resolve_scheme_project_id,
)
from app.services.preflight_engine import (
    PreflightContext,
    preflight_stats,
    run_preflight,
)
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
#: 单章节送 AI 的最大字符数（超过则截断，并在正文末尾显式标记；避免长章节
#: 后半段永远不被 AI 检查，同时避免整份方案 60k+ 字符打爆上下文窗口）
SECTION_CONTENT_CAP = 4000
#: /check 拼装送 AI 的整份内容字符上限（保留旧值 6000 以兼容既有前端提示）
AI_CONTENT_HARD_CAP = 6000
#: /expert-review 允许的 outline 节点上限（超过视为数据异常，明确 400 而非静默截断）
EXPERT_OUTLINE_MAX_NODES = 300
#: /expert-review 允许的附件数量与单条长度上限（防 token 爆炸）
EXPERT_ATTACH_MAX_COUNT = 8
EXPERT_ATTACH_MAX_LEN = 500
#: /results 默认 & 最大返回条数（此前无 LIMIT，历史累积后一次返回可能数万行）
RESULTS_DEFAULT_LIMIT = 50
RESULTS_MAX_LIMIT = 500
#: 一致性审计送 AI 的单章节 / 整份内容字符上限（对齐 /check 的口径）
CONSISTENCY_SECTION_CAP = 3000
CONSISTENCY_TOTAL_CAP = 50000


# ---------------------------------------------------------------------------
# 规则目录（前端「规则说明」与 AI 提示词共用同一份）
# ---------------------------------------------------------------------------
@router.get("/rules")
async def list_rules():
    """全量审核规则目录（含行业依据），供前端展示与用户自查。"""
    return {
        "rule_version": RULE_VERSION,
        "items": rule_catalog(),
        "dimensions": dimension_catalog(),
        "standard_db_version": STANDARD_DB_VERSION,
        "standard_db_checked_at": STANDARD_DB_CHECKED_AT,
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
from app.services.ai.prompts._limits import FACTS_PROMPT_CAP  # noqa: E402
#: 单条事实值的截断长度（value 兜底取整段 content 时防长文本撑爆）
from app.services.ai.prompts._limits import (  # noqa: E402
    FACT_PROMPT_VALUE_CAP as _FACT_PROMPT_VALUE_CAP,
)
#: 安全关键标记：预算截断下也必须**完整**保留（见 _join_with_budget）
SAFETY_MARK = "（安全关键）"


def _join_with_budget(lines: list[str], budget: int,
                       suffixes: list[str] | None = None) -> str:
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
        return _keep_suffixes("\n".join(lines)[:budget],
                                suffixes)
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
            return text[:len(text) - n]
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
    """把桥接返回的事实清单格式化为提示词文本（空 → 「（无）」）。"""
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


@router.post("/check")
async def compliance_check(body: ComplianceCheckIn, db=Depends(get_db)):
    scheme_id = body.scheme_id
    # ✅ BUG 修复（2026-09-21）：旧实现无方案存在性校验，不存在的 scheme_id 会
    #    走完整次 AI 调用（空内容 + 空方案名），既浪费额度又返回看似正常的响应，
    #    前端无从判断是「方案不存在」还是「正文恰好都没命中」。现补 404。
    sc_row = await review_db.fetch_one(
        db, "SELECT name, type, project_id FROM schemes WHERE id=?", (scheme_id,),
        what="规范符合性检查：读取方案")
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
            logger.warning("compliance_check: rule_ids %r 均不在规则注册表，"
                           "已回退为默认清单（scheme=%s）", body.rule_ids, scheme_id)
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
    sections = [(r["title"], r["content"]) for r in order_sections_dfs(
        await review_db.fetch_all(
            db,
            "SELECT id, parent_id, title, content, sort_order FROM sections"
            " WHERE scheme_id=? AND content!='' ORDER BY sort_order", (scheme_id,),
            what="规范符合性检查：读取章节正文"))]
    # ✅ BUG 修复（2026-09-21）：旧实现 c[:2000] 单章节截断，长章节后半段
    #    永远不被 AI 看到；同时总长 [:6000] 也是硬截断，超长方案只能送前几章。
    #    现提升单章节上限至 SECTION_CONTENT_CAP（覆盖绝大多数危大工程章节），
    #    并在超长章节末尾显式标记截断事实，让 AI 至少知道「这段有更多内容」，
    #    避免无中生有。总长上限保持 6000 以避免上下文窗口爆炸。
    _trunc_count = 0
    _parts: list = []
    for t, c in sections:
        if len(c) > SECTION_CONTENT_CAP:
            _trunc_count += 1
            _c = c[:SECTION_CONTENT_CAP] + f"\n\n[……本章正文已截断，原文 {len(c)} 字]"
        else:
            _c = c
        _parts.append(f"### {t}\n{_c}")
    content = "\n\n".join(_parts)
    if _trunc_count:
        logger.warning("compliance_check: %d 个章节正文超过 %d 字上限被截断（scheme=%s）",
                       _trunc_count, SECTION_CONTENT_CAP, scheme_id)

    sys_prompt = render("compliance_check_system",
                        scheme_name=scheme_name,
                        scheme_type=scheme_type,
                        global_facts=await _load_facts_prompt_text(db, scheme_id),
                        checklist=json.dumps(checklist, ensure_ascii=False),
                        content=content[:AI_CONTENT_HARD_CAP])
    # ✅ 修复（2026-09-24）：旧实现未传 scene —— 该调用在 ai_audit_logs 里
    #    scene 恒为空串，/ai/stats 场景聚看不到「符合性检查」的消耗，且无法
    #    为其单独配置模型（场景路由按 scene 精确匹配）。
    obj, _ = await collect_json_response(
        [{"role": "system", "content": sys_prompt}],
        _validate_check_results,
        # ✅ R38：补 json_mode —— 本模块此前三个 JSON 端点是全链路唯一未开
        #    结构化输出模式的（同族 facts/consistency/global_facts 均已开），
        #    与 json_response 模块「弱模型稳定 JSON 工作流」的前提相悖。
        json_mode=True, temperature=0.2,
        scene="compliance_check")

    results = _normalize_ai_results(obj.get("results", []), checklist, body.rule_ids)
    # ✅ 根治批次截头（2026-09-23）：本次调用的全部行共享一个 batch_id，
    #    总检聚合按 batch_id 取「最近一批」，不再依赖 rowid 连续段推断
    #    （两次 /check 跨秒交错时 rowid 段会错切，首批被截头/混批）。
    batch_id = uuid.uuid4().hex
    # ✅ 2026-10-07：本批结论的正文指纹（整批共用，供总检判定「该批是否已过期」）
    _batch_fp = await _content_fingerprint(db, scheme_id)
    for r in results:
        cid = str(uuid.uuid4())
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
            (cid, scheme_id, project_id, "compliance", r.get("rule_id", ""),
             r.get("item", ""), r.get("severity", ""),
             json.dumps(r, ensure_ascii=False), r.get("suggestion", ""), batch_id,
             _batch_fp),
            what="规范符合性检查：写入结果行")
    await db.commit()
    return {"results": results, "batch_id": batch_id}


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
    out: list = []
    for idx, r in enumerate(results or []):
        item = dict(r)
        rid = str(item.get("rule_id") or "").strip()
        if not get_rule(rid):
            if idx < len(known):
                rid = known[idx]
            else:
                rid = _match_rule_by_title(item.get("item") or "", checklist)
        item["rule_id"] = rid
        out.append(item)
    return out


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
        db, "SELECT name, type, project_id FROM schemes WHERE id=?", (scheme_id,),
        what="专家论证预检：读取方案")
    if not sc_row:
        raise HTTPException(404, "方案不存在")
    scheme_name = sc_row["name"]
    scheme_type = sc_row["type"]
    project_id = sc_row["project_id"] or ""

    # ✅ 遗留修复（2026-09-22）：目录树按前序 DFS 送 AI，避免扁平序层级错乱
    outline_rows = order_sections_dfs(await review_db.fetch_all(
        db,
        "SELECT id, parent_id, title, level, sort_order FROM sections"
        " WHERE scheme_id=? ORDER BY sort_order", (scheme_id,),
        what="专家论证预检：读取章节目录"))
    # ✅ BUG 修复（2026-09-21）：旧实现 outline_tree 无节点上限，方案章节多时
    #    JSON 序列化后的提示词可轻易突破模型上下文；现引入 EXPERT_OUTLINE_MAX_NODES
    #    显式 400，避免静默打爆 token。附件同理：既限数量也限单条长度。
    if len(outline_rows) > EXPERT_OUTLINE_MAX_NODES:
        raise HTTPException(
            400, f"章节数 {len(outline_rows)} 超过上限 {EXPERT_OUTLINE_MAX_NODES}，"
                 "请精简目录后再执行专家论证预检")
    outline_tree = [{"title": r["title"], "level": r["level"]} for r in outline_rows]

    attachments = [str(a) for a in (body.attachments or [])]
    if len(attachments) > EXPERT_ATTACH_MAX_COUNT:
        raise HTTPException(
            400, f"附件数量 {len(attachments)} 超过上限 {EXPERT_ATTACH_MAX_COUNT}，"
                 "请筛选最关键的材料再试")
    for _i, a in enumerate(attachments):
        if len(a) > EXPERT_ATTACH_MAX_LEN:
            raise HTTPException(
                400, f"第 {_i + 1} 条附件长度 {len(a)} 超过上限 {EXPERT_ATTACH_MAX_LEN}，"
                     "请精简后重试")

    # ✅ 论证必要项由规则注册表在运行时注入（消除提示词内联清单与 EXPERT_CHECK_ITEMS 的分叉）
    check_items = "\n".join(
        f"- {it['item']}（规则 {it['rule_id']}｜{it['basis']}）"
        for it in expert_items())
    sys_prompt = render("expert_review_system",
                        scheme_name=scheme_name,
                        scheme_type=scheme_type,
                        global_facts=await _load_facts_prompt_text(db, scheme_id),
                        outline_tree=json.dumps(outline_tree, ensure_ascii=False),
                        attachments=json.dumps(attachments, ensure_ascii=False),
                        check_items=check_items)
    # ✅ 修复（2026-09-24）：补 scene（原为空 → 统计归空场景、场景路由配不上）
    obj, _ = await collect_json_response(
        [{"role": "system", "content": sys_prompt}],
        lambda o: [] if "score" in o else ["缺少 score"],
        json_mode=True, temperature=0.2,
        scene="expert_review")

    result = _normalize_expert_review_result(obj)
    cid = str(uuid.uuid4())
    # ✅ 单行也是一批：补 batch_id（与 /check 同口径，历史行空串不影响读取回退）。
    #    ✅ R13（2026-10-06）：写路径经 exec_write —— 旧实现 INSERT 未生效时
    #    commit 照常成功、接口返回评分，总检的「专家论证预检」来源永远取不到本次。
    await review_db.exec_write(
        db,
        "INSERT INTO compliance_check (id, scheme_id, project_id, check_type,"
        " result, batch_id, content_fingerprint) VALUES (?,?,?,?,?,?,?)",
        (cid, scheme_id, project_id, "expert_review",
         json.dumps(result, ensure_ascii=False), uuid.uuid4().hex,
         await _content_fingerprint(db, scheme_id)),
        what="专家论证预检：写入结果行")
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
            logger.warning("expert_review 输出缺少 %s 字段，按空列表补齐（score=%r）",
                           f, result.get("score"))
            result[f] = []
        elif not isinstance(result[f], list):
            # 模型可能返回单字符串（如 missing="工程概况"），直接丢弃会丢失信息
            # 转为单元素数组保留内容，并记 WARNING 供运维审计
            raw = result[f]
            if isinstance(raw, str) and raw.strip():
                logger.warning("expert_review 的 %s 是字符串（%r），转为单元素数组保留",
                               f, raw[:120])
                result[f] = [raw.strip()]
            else:
                logger.warning("expert_review 的 %s 不是数组（%r），按空列表补齐",
                               f, type(raw).__name__)
                result[f] = []
    return result


@router.get("/results/{scheme_id}")
async def get_results(scheme_id: str, check_type: str = "", limit: int = RESULTS_DEFAULT_LIMIT,
                      offset: int = 0, db=Depends(get_db)):
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
    total = int(await review_db.fetch_scalar(
        db, f"SELECT COUNT(*) AS n FROM compliance_check WHERE {where}", params, 0,
        what="合规历史：统计总数") or 0)
    items = await review_db.fetch_all(
        db,
        f"SELECT * FROM compliance_check WHERE {where}"
        " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
        params + [limit, offset], what="合规历史：读取结果行")
    return {"items": items, "total": total,
            "limit": limit, "offset": offset}


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
        db, "SELECT name, type, project_id FROM schemes WHERE id=?", (scheme_id,),
        what="一致性审计：读取方案")
    if not sc_row:
        raise HTTPException(404, "方案不存在")

    # ✅ 遗留修复（2026-09-22）：审计上下文同样按目录树前序 DFS 送 AI，
    #    保证截断与审阅顺序与文档实序一致
    sections = [(r["title"], r["content"]) for r in order_sections_dfs(
        await review_db.fetch_all(
            db,
            "SELECT id, parent_id, title, content, sort_order FROM sections"
            " WHERE scheme_id=? AND content!='' ORDER BY sort_order",
            (scheme_id,), what="一致性审计：读取章节正文"))]
    if not sections:
        raise HTTPException(422, "方案尚无正文内容，无法审计")
    # ✅ BUG 修复（2026-09-21）：旧实现 c[:1500] 单章节 + 总长 [:30000] 双重硬
    #    截断，长章节后半段永远不被审计；且截断无标记，AI 无从知晓。
    #    现提升到 3000/50000，并在超长章节末尾显式标注。
    _parts: list = []
    _trunc_count = 0
    for t, c in sections:
        if len(c) > CONSISTENCY_SECTION_CAP:
            _trunc_count += 1
            _c = c[:CONSISTENCY_SECTION_CAP] + f"\n\n[……本章正文已截断，原文 {len(c)} 字]"
        else:
            _c = c
        _parts.append(f"### {t}\n{_c}")
    content = "\n\n".join(_parts)[:CONSISTENCY_TOTAL_CAP]
    if _trunc_count:
        logger.warning("consistency_audit: %d 个章节正文超过 %d 字上限被截断（scheme=%s）",
                       _trunc_count, CONSISTENCY_SECTION_CAP, scheme_id)

    facts = await _build_facts_text(db, scheme_id, max_total=6000)
    if not facts.strip():
        raise HTTPException(422, "尚未提取项目关键事实，请先执行「全局事实提取」再审计一致性")

    sys_prompt = render("consistency_audit_system",
                        scheme_name=sc_row["name"],
                        scheme_type=sc_row["type"],
                        facts=facts,
                        content=content)
    # ✅ 修复（2026-09-24）：补 scene（原为空 → 统计归空场景、场景路由配不上）
    obj, _ = await collect_json_response(
        [{"role": "system", "content": sys_prompt}],
        _validate_consistency_audit,
        json_mode=True, temperature=0.2,
        scene="consistency_audit")

    issues = obj["issues"]
    audit_id = str(uuid.uuid4())
    # 评分容错：AI 可能返回字符串（如"良好"）或缺失，强制转为 float 失败则记 0
    score_raw = obj.get("score", 0)
    try:
        score_float = float(score_raw)
    except (TypeError, ValueError):
        logger.warning("一致性审计评分非数值，按 0 处理: %r", score_raw)
        score_float = 0.0
    # ✅ R13（2026-10-06）：写路径经 exec_write。旧实现 INSERT 未生效时
    #    commit 照常成功、接口返回评分与 issues —— 而 /latest 与总检的
    #    「一致性审计」来源永远取不到本次，用户以为已审计。
    await review_db.exec_write(
        db,
        "INSERT INTO consistency_audit (id, project_id, scheme_id, score,"
        " content_fingerprint, issues) VALUES (?,?,?,?,?,?)",
        (audit_id, sc_row["project_id"], scheme_id,
         await _content_fingerprint(db, scheme_id),
         score_float, json.dumps(issues, ensure_ascii=False)),
        what="一致性审计：写入审计结果")
    await db.commit()
    return {"id": audit_id, "score": score_float, "issues": issues}


@router.get("/consistency-audit/{scheme_id}/latest")
async def latest_consistency_audit(scheme_id: str, db=Depends(get_db)):
    item = await review_db.fetch_one(
        db,
        "SELECT id, score, issues, created_at FROM consistency_audit "
        "WHERE scheme_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (scheme_id,), what="一致性审计：读取最近一次结果")
    if not item:
        return {"exists": False}
    try:
        item["issues"] = json.loads(item.get("issues") or "[]")
    except json.JSONDecodeError:
        item["issues"] = []
    item["exists"] = True
    return item


# @deprecated 孤儿 API：本前端无调用方；按向后兼容原则保留，大版本评估清理。
@router.get("/consistency-audit/{scheme_id}/history", deprecated=True)
async def consistency_audit_history(
        scheme_id: str, db=Depends(get_db), limit: int = 10,
        response: Response = None):
    _apply_deprecation_headers(
        response,
        replacement=f"/api/v1/compliance/runs/{scheme_id}",
    )
    limit = max(1, min(limit, 50))
    items = []
    for r in await review_db.fetch_all(
            db,
            "SELECT id, score, issues, created_at FROM consistency_audit "
            "WHERE scheme_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (scheme_id, limit), what="一致性审计历史：读取记录"):
        item = r
        try:
            item["issues"] = json.loads(item.get("issues") or "[]")
        except json.JSONDecodeError:
            item["issues"] = []
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
            scope += " OR (project_id=? AND (scheme_id='' OR scheme_id IS NULL))"
            params.append(pid)
        row = await review_db.fetch_one(
            db,
            f"SELECT COUNT(*) AS n, COALESCE(MAX(updated_at),'') AS latest FROM global_facts "
            f"WHERE ({scope}) AND {get_facts_inject_where()}", params,
            what="预检缓存键：统计可注入事实")
        # ⚠️ 必须按列名取值：review_db.fetch_one 返回 dict（sqlite3.Row 迭代出的是
        #    值、dict 迭代出的是键）。旧实现写 `row[0] / row[1]`，换成 dict 后
        #    恒 KeyError → 被本函数的 except 吞掉恒返回 ""，等于**事实签名永久失效**：
        #    事实确认/新增后 TTL 内仍命中旧缓存，SAF-08 永远不出现（2026-10-03 修复复发）。
        return f"{row['n'] or 0}:{row['latest'] or ''}" if row else "0:"
    except Exception:  # 签名失败仅视为「无事实」，不阻断预检
        return ""


async def _build_preflight_context(scheme_id: str, db) -> PreflightContext:
    """从 DB 装配预检上下文（章节 + 图表 + 方案元信息 + 已确认全局事实）。

    ✅ R13（2026-10-06）：三处读全部走 ``review_db``。本函数是 /preflight、
    /overview 与自动修复三端重算的**共同入口**，且不在任何 try 内 ——
    旧实现命中 R13 即 ``AttributeError`` → 500，用户既拿不到预检结论
    也看不到原因。
    """
    sc_row = await review_db.fetch_one(
        db, "SELECT name, type, word_budget FROM schemes WHERE id=?", (scheme_id,),
        what="预检上下文：读取方案")
    if not sc_row:
        raise HTTPException(404, "方案不存在")

    # ✅ 遗留修复（2026-09-22）：预检上下文按目录树前序 DFS 装配，
    #    使重复内容检测（两两配对）之外的序敏感检查与导出链路同口径。
    sections = order_sections_dfs(await review_db.fetch_all(
        db,
        "SELECT id, parent_id, title, content, word_count, level, status, sort_order FROM sections"
        " WHERE scheme_id=? ORDER BY sort_order", (scheme_id,),
        what="预检上下文：读取章节"))

    charts = await review_db.fetch_all(
        db, "SELECT chart_type, status FROM chart_predictions WHERE scheme_id=?", (scheme_id,),
        what="预检上下文：读取图表登记")

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
    n = (1 if _OVERVIEW_RECENT.pop(scheme_id, None) is not None else 0)
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


async def _run_is_stale(db, scheme_id: str, row,
                        current_fingerprint: str | None = None) -> bool:
    """判定一条预检运行记录是否已过期（G3）。

    历史行没有指纹（本功能上线前的数据）→ 视为**不过期**：无法判定就不打扰用户，
    避免把库里所有旧记录全标成「已过期」的红条。

    ✅ BUG 修复（2026-09-23）：列表场景下旧签名每行重算一次内容指纹
       （N 行 = N 次全表扫描）。现允许调用方预先算好后传入。
       ⚠️ 同时发现调用方（/runs、/report、导出页摘要）的 SELECT 均未取
       content_fingerprint 列 → stored 永远取空 → stale 恒为 False，G3 时效
       判定在三处全部失效。已随本次修复在各调用方补齐取列。
    """
    try:
        keys = set(row.keys()) if hasattr(row, "keys") else set(row)
        stored = row["content_fingerprint"] if "content_fingerprint" in keys else ""
    except (IndexError, KeyError, TypeError):
        stored = ""
    if not stored:
        return False
    if current_fingerprint is None:
        current_fingerprint = await _content_fingerprint(db, scheme_id)
    return bool(current_fingerprint) and stored != current_fingerprint


# @deprecated 孤儿 API：本前端走 /overview（内部已含同一预检引擎且带落库+缓存）；
# 端点保留供脚本/手工诊断（已补幂等锁+缓存），大版本评估清理。
@router.post("/preflight/{scheme_id}", deprecated=True)
async def run_preflight_check(
        scheme_id: str, db=Depends(get_db), force: bool = False,
        response: Response = None):
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
        # ✅ 2026-10-03：进程内缓存键并入「已确认事实签名」，事实确认后
        #    TTL 内不再返回旧结论（SAF-08 能及时出现）；落库指纹语义不变。
        cache_key = (fingerprint, await _facts_signature(db, scheme_id))
        hit = _PREFLIGHT_RECENT.get(scheme_id)
        if (not force and hit and hit[0] == cache_key
                and time.monotonic() - hit[1] < OVERVIEW_CACHE_TTL):
            cached = dict(hit[2])
            cached["cached"] = True
            cached["stale"] = False
            return cached
        ctx = await _build_preflight_context(scheme_id, db)
        findings = run_preflight(ctx)
        stats = preflight_stats(ctx)
        stats["standard_db_version"] = STANDARD_DB_VERSION
        stats["standard_db_checked_at"] = STANDARD_DB_CHECKED_AT
        result = score_findings(findings)
        payload = result.as_dict()
        payload.update({
            "scheme_id": scheme_id,
            "scheme_name": ctx.scheme_name,
            "stats": stats,
            "sources": ["program"],
            "created_at": datetime.now().isoformat(timespec="seconds"),
            # ✅ G3：本次结论对应的内容指纹（落库后供 /runs /report 判定是否过期）
            "content_fingerprint": fingerprint,
            "stale": False,
            "cached": False,
        })
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
        # ✅ 2026-10-03：进程内缓存键并入「已确认事实签名」（同 /preflight）；
        #    落库 content_fingerprint（G3 时效语义）保持不变。
        cache_key = (fingerprint, await _facts_signature(db, scheme_id))
        hit = _OVERVIEW_RECENT.get(scheme_id)
        if not force and hit and hit[0] == cache_key:
            if time.monotonic() - hit[1] < OVERVIEW_CACHE_TTL:
                cached = dict(hit[2])
                cached["cached"] = True
                cached["stale"] = False
                return cached
        payload = await _readiness_overview_compute(db, scheme_id)
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


async def _readiness_overview_compute(db, scheme_id: str, *, persist: bool = True) -> dict:
    """总检的实际计算（由 readiness_overview 的并发锁与幂等缓存包着）。

    ✅ 2026-10-03（数据链收口）：新增 ``persist`` 形参。**默认 True，既有端点
    调用逐字不变**（向后兼容）。自动修复链路（routers/review_autofix.py 的
    plan/apply/collect/stage）此前直接调本函数重算 findings，而本函数末尾
    无条件 ``_persist_run`` 落 preflight_runs —— 用户每点一次「定位/修复/
    收集/暂存」就往分数趋势里灌一条总检历史（污染 G2 要保护的趋势线），
    且不持 ``_overview_lock`` 存在并发写竞态。内部重算只需读 findings，
    传 ``persist=False`` 跳过落库，两个问题同时消除且不改任何判定口径。
    """
    ctx = await _build_preflight_context(scheme_id, db)
    program_findings = run_preflight(ctx)
    sources = ["program"]
    # ✅ 修复（2026-10-07 · 陈旧 AI 结论计入评分，生产库实证）：
    #    生产方案 10-01 的 AI 合规/一致性结论，在正文已多次修改的 10-07
    #    仍被计入总检评分（CON-04 / SAF-* 指向已不存在的章节），用户据此
    #    「整改」实际不存在的缺陷。AI 各源写入时已记正文指纹，此处比对后
    #    跳过过期行，并把跳过的来源名放进 stale_ai_sources 供前端提示重跑。
    fingerprint = await _content_fingerprint(db, scheme_id)
    stale_ai_sources: list[str] = []

    # --- ✅ G1：导出预检问题并入评分（与程序化预检共用同一套规则词表）---
    # 此前导出预检是独立体系，其问题既不进 preflight_runs 也不进本聚合，
    # 于是"总检说能交付、导出却被拦住"。现两处共用 collect_export_issues 的
    # 同一份判定；导出页也回 preflight_summary，反向打通。
    export_findings: list = []
    try:
        from app.routers.export import collect_export_issues, export_issues_to_findings
        _exp = await collect_export_issues(scheme_id, db)
        export_findings = export_issues_to_findings(_exp.get("issues") or [])
        if export_findings:
            sources.append("export_check")
    except Exception as e:
        logger.warning("overview: 导出预检问题聚合失败（跳过，不阻断总检）: %s", e)

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
            " ORDER BY created_at DESC, rowid DESC LIMIT 1", (scheme_id,),
            what="总检聚合：定位最近一批合规结果")
        _batch = (_brow["batch_id"] if _brow else "") or ""
        if _batch:
            _rows_raw = await review_db.fetch_all(
                db,
                "SELECT result, content_fingerprint FROM compliance_check WHERE scheme_id=?"
                " AND check_type='compliance' AND batch_id=? ORDER BY rowid",
                (scheme_id, _batch), what="总检聚合：读取该批合规结果")
        else:
            last = await review_db.fetch_one(
                db,
                "SELECT MIN(rowid) AS min_rowid, created_at"
                " FROM compliance_check WHERE scheme_id=? AND check_type='compliance' "
                "GROUP BY created_at ORDER BY created_at DESC LIMIT 1",
                (scheme_id,), what="总检聚合：定位历史行连续段锚点")
            _rows_raw = await review_db.fetch_all(
                db,
                "SELECT result, content_fingerprint FROM compliance_check WHERE scheme_id=? AND check_type='compliance'"
                " AND rowid >= ? ORDER BY rowid",
                (scheme_id, last["min_rowid"]),
                what="总检聚合：读取历史行连续段") if last else []
        # ✅ R13（2026-10-06）：旧实现把游标本身当判据（`if cur is not None`），
        #    而 `db.execute` 返回 None 时恰好命中「跳过」分支 —— 表面无害，
        #    但同段的 `cur.fetchone()` 会先抛 AttributeError，整段降级；
        #    现统一走 review_db，读失败由本段 except 降级为「跳过该来源」。
        if _rows_raw:
            rows = []
            _stale_n = 0
            for r in _rows_raw:
                if _ai_row_is_stale(r, fingerprint):
                    _stale_n += 1   # 正文已变：该结论不再计入评分
                    continue
                try:
                    rows.append(json.loads(r["result"] or "{}"))
                except json.JSONDecodeError:
                    continue
            if _stale_n:
                stale_ai_sources.append("compliance")
                logger.warning(
                    "overview: %d 条 AI 合规结论正文指纹不一致（已跳过不计分）scheme=%s",
                    _stale_n, scheme_id)
            ai_findings = ai_results_to_findings(rows)
            if rows:
                sources.append("compliance")
    except Exception as e:
        logger.warning("overview: 读取 AI 规范符合性结果失败（跳过）: %s", e)

    # --- 最近一次一致性审计（作为一致性维度的补充证据）---
    try:
        row = await review_db.fetch_one(
            db, "SELECT score, issues, content_fingerprint FROM consistency_audit "
            "WHERE scheme_id=?" " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (scheme_id,), what="总检聚合：读取最近一次一致性审计")
        _audit_stale = bool(row) and _ai_row_is_stale(row, fingerprint)
        if _audit_stale:
            stale_ai_sources.append("consistency")
            logger.warning("overview: 一致性审计结论正文指纹不一致（已跳过）scheme=%s",
                           scheme_id)
        if row and not _audit_stale:
            try:
                issues = json.loads(row["issues"] or "[]")
            except json.JSONDecodeError:
                issues = []
            for idx, it in enumerate(issues):
                sev = str(it.get("severity") or "medium").lower()
                if sev not in ("high", "medium", "low"):
                    sev = "medium"
                # ✅ 修复（2026-09-17）：每条一致性审计 issue 使用唯一 rule_id，
                #    旧实现全部并入 CON-04 被 merge_findings 按 rule_id 去重塌缩为
                #    1 条，导致一份方案有多处不一致时一致性维度只扣一次分、总分虚高。
                #    现逐条计入一致性维度扣分，与「看了报告仍不知道能不能交付」的
                #    设计目标一致（每条不一致都是独立的交付风险）。
                ai_findings.append({
                    "rule_id": f"CON-04-{idx + 1}", "dimension": "consistency",
                    "severity": sev,
                    "title": f"一致性审计：{it.get('dimension') or '不一致项'}",
                    "detail": it.get("content_quote") or it.get("fact") or "",
                    "evidence": [it.get("fact") or "", it.get("content_quote") or ""],
                    "section_id": "", "section_title": it.get("section_title") or "",
                    "suggestion": it.get("suggestion") or "",
                    "basis": "全文一致性审计（AI）", "mode": "ai",
                })
            if issues or row["score"] is not None:
                sources.append("consistency")
    except Exception as e:
        logger.warning("overview: 读取一致性审计结果失败（跳过）: %s", e)

    # --- ✅ 新增（2026-09-17）：最近一次全文一致性扫描（规则 + 仲裁）未解决冲突 ---
    # 此前「一致性扫描 → 修复工作台」产出的冲突清单（consistency_conflicts）完全不参与
    # 就绪度评分 —— 扫描发现高危数值冲突，总览分数却纹丝不动，两个体系断链。
    # 只统计最近一批扫描中 status IN ('pending','failed') 的未解决项；
    # repaired/accepted（已解决）与 skipped（用户明确不处理）不计分。
    try:
        last_scan = await review_db.fetch_one(
            db, "SELECT scan_id, content_fingerprint FROM consistency_conflicts "
            "WHERE scheme_id=?" " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (scheme_id,), what="总检聚合：定位最近一批一致性扫描")
        _scan_stale = bool(last_scan) and _ai_row_is_stale(last_scan, fingerprint)
        if _scan_stale:
            stale_ai_sources.append("consistency_scan")
            logger.warning(
                "overview: 一致性扫描结论正文指纹不一致（已跳过）scheme=%s",
                scheme_id)
        if last_scan and not _scan_stale:
            scan_conflicts = await review_db.fetch_all(
                db,
                "SELECT conflict_type, severity, topic, occurrences,"
                " authoritative_value, repair_instruction, status"
                " FROM consistency_conflicts WHERE scheme_id=? AND scan_id=?"
                " ORDER BY created_at, rowid",
                (scheme_id, last_scan["scan_id"]),
                what="总检聚合：读取一致性扫描冲突")
            unresolved = [c for c in scan_conflicts
                          if c.get("status") in ("pending", "failed")]
            for idx, c in enumerate(unresolved):
                sev = str(c.get("severity") or "medium").lower()
                if sev not in ("high", "medium", "low"):
                    sev = "medium"
                try:
                    occ = json.loads(c.get("occurrences") or "[]")
                except json.JSONDecodeError:
                    occ = []
                if not isinstance(occ, list):
                    occ = []
                ai_findings.append({
                    "rule_id": f"CON-SCAN-{idx + 1}", "dimension": "consistency",
                    "severity": sev,
                    "title": f"一致性扫描：{c.get('conflict_type') or '数值冲突'}",
                    "detail": c.get("reason") or c.get("topic") or "",
                    "evidence": [str(o) for o in occ],
                    "section_id": "", "section_title": c.get("topic") or "",
                    "suggestion": c.get("repair_instruction") or "",
                    "basis": "全文一致性扫描（规则+仲裁）", "mode": "program",
                })
            sources.append("consistency_scan")
    except Exception as e:
        logger.warning("overview: 读取一致性扫描冲突失败（跳过）: %s", e)

    # --- 最近一次专家论证预检 ---
    try:
        row = await review_db.fetch_one(
            db, "SELECT result, content_fingerprint FROM compliance_check "
            "WHERE scheme_id=? AND check_type='expert_review'"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1", (scheme_id,),
            what="总检聚合：读取最近一次专家论证预检")
        _expert_stale = bool(row) and _ai_row_is_stale(row, fingerprint)
        if _expert_stale:
            stale_ai_sources.append("expert_review")
            logger.warning("overview: 专家论证预检结论正文指纹不一致（已跳过）scheme=%s",
                           scheme_id)
        if row and not _expert_stale:
            try:
                expert = json.loads(row["result"] or "{}")
            except json.JSONDecodeError:
                expert = {}
            if expert:
                ai_findings.extend(expert_result_to_findings(expert))
                sources.append("expert_review")
    except Exception as e:
        logger.warning("overview: 读取专家论证预检结果失败（跳过）: %s", e)

    findings = merge_findings(program_findings, ai_findings, export_findings)
    # ✅ 自动修复能力标注（services/review_autofix.py）：为每条 finding 补
    #    ``autofix`` 字段（mode / fixable / reason），前端据此渲染「一键修复」
    #    按钮。只新增字段，既有字段与评分口径完全不变（向后兼容）。
    review_autofix.capability_summary(findings)
    result = score_findings(findings)
    stats = preflight_stats(ctx)
    stats["standard_db_version"] = STANDARD_DB_VERSION
    stats["standard_db_checked_at"] = STANDARD_DB_CHECKED_AT
    # ✅ G1：单列导出预检命中数，前端可说明"这分里有几项来自导出预检"
    stats["export_issue_count"] = sum(int(f.get("count") or 0) for f in export_findings)
    payload = result.as_dict()
    payload.update({
        "scheme_id": scheme_id,
        "scheme_name": ctx.scheme_name,
        "stats": stats,
        "sources": sources,
        # ✅ 2026-10-07：因正文已变而跳过的 AI 来源（提示用户重跑对应 AI 检查）
        "stale_ai_sources": stale_ai_sources,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        # ✅ G3：落库后供 /runs /report / 导出页判定「结论是否已过期」
        "content_fingerprint": await _content_fingerprint(db, scheme_id),
    })
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
            " rule_version, total, grade, verdict, released, blocked,"
            " counts, dimensions, findings, stats)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), scheme_id, await _scheme_project_id(db, scheme_id),
             payload.get("content_fingerprint") or "",
             RULE_VERSION, payload.get("total", 0),
             payload.get("grade", ""), payload.get("verdict", ""),
             1 if payload.get("released") else 0, 1 if payload.get("blocked") else 0,
             json.dumps(payload.get("counts") or {}, ensure_ascii=False),
             json.dumps(payload.get("dimensions") or [], ensure_ascii=False),
             json.dumps(payload.get("findings") or [], ensure_ascii=False),
             json.dumps(stats or {}, ensure_ascii=False)),
            what=f"写入预检运行记录（scheme={scheme_id[:8]}）")
        await db.commit()
    except Exception as e:
        # ✅ BUG 修复（2026-09-21）：旧实现 `except Exception: pass` 静默吞错，
        #    磁盘满 / 连接 poisoned / JSON 序列化失败全无声息，运维无从排查；
        #    且当 INSERT 抛异常但连接事务状态未回滚时，后续请求会踩到
        #    "transaction already in progress"。现至少记录日志并尝试 rollback。
        logger.warning("preflight_runs 写入失败（不影响评分返回）: scheme=%s err=%s",
                       scheme_id, e)
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
        " content_fingerprint, created_at FROM preflight_runs WHERE scheme_id=?"
        " ORDER BY created_at DESC, rowid DESC LIMIT ?", (scheme_id, limit),
        what="预检历史：读取运行记录")
    current_fp: str | None = None
    items = []
    for r in rows:
        item = r
        try:
            item["counts"] = json.loads(item.get("counts") or "{}")
        except json.JSONDecodeError:
            item["counts"] = {}
        if current_fp is None and (item.get("content_fingerprint") or ""):
            current_fp = await _content_fingerprint(db, scheme_id)
        item["stale"] = await _run_is_stale(db, scheme_id, item,
                                            current_fingerprint=current_fp)
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
        " stats, content_fingerprint, created_at FROM preflight_runs WHERE scheme_id=?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1", (scheme_id,),
        what="整改清单报告：读取最近一次预检记录")
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
        db, "SELECT name, type FROM schemes WHERE id=?", (scheme_id,),
        what="整改清单报告：读取方案名")
    name = sc_row["name"] if sc_row else ""

    # ✅ G3（2026-09-21）：报告可能基于旧正文 —— 整改清单被抄进评审意见后，
    # 若正文已改过，按旧结论整改就是白干。报告头部显式标注时效状态。
    stale = await _run_is_stale(db, scheme_id, row)
    stale_line = ("- ⚠️ 时效状态：**已过期**（本次预检之后正文 / 图表已发生变更，"
                  "结论可能失效，请先重新执行「一键总检」）"
                  if stale else
                  "- 时效状态：有效（正文 / 图表与本次预检一致）")

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
        lines.append(f"| {d.get('label')} | {d.get('weight')} | {d.get('score')} "
                     f"| {d.get('issue_count')} |")

    blockers = [f for f in findings if f.get("severity") == "block"]
    if blockers:
        lines += ["", "## 二、交付阻断项（须整改）", ""]
        for i, f in enumerate(blockers, 1):
            lines.append(f"{i}. **{f.get('title')}**（{f.get('rule_id')}）"
                         f"：{f.get('detail')}")
            if f.get("suggestion"):
                lines.append(f"   - 整改建议：{f.get('suggestion')}")

    others = [f for f in findings if f.get("severity") != "block"]
    if others:
        lines += ["", "## 三、其他问题清单", ""]
        for i, f in enumerate(others, 1):
            sev = {"high": "严重", "medium": "一般", "low": "提示"}.get(
                f.get("severity"), f.get("severity"))
            lines.append(f"{i}. [{sev}] **{f.get('title')}**：{f.get('detail')}")
            if f.get("suggestion"):
                lines.append(f"   - 建议：{f.get('suggestion')}")

    lines += ["", "## 四、客观统计", ""]
    lines.append(f"- 章节数：{stats.get('section_count', 0)}"
                 f"（已生成 {stats.get('generated_count', 0)}）")
    lines.append(f"- 总字数：{stats.get('total_words', 0)}")
    lines.append(f"- 图表完成：{stats.get('chart_done', 0)}/{stats.get('chart_total', 0)}")

    md = "\n".join(lines)
    if fmt == "json":
        return {"markdown": md, "total": total_value, "grade": row["grade"] or "—",
                "findings": findings, "dimensions": dims, "stats": stats,
                "stale": stale, "created_at": row["created_at"]}
    return {"format": "markdown", "filename": f"{name}_审核预检报告.md", "content": md,
            "stale": stale, "created_at": row["created_at"]}
