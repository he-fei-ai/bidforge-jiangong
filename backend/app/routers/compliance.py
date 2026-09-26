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

from fastapi import APIRouter, Depends, HTTPException

from app.db import get_db
from app.models import ComplianceCheckIn, ExpertReviewIn
from app.services.ai.json_response import collect_json_response
from app.services.ai.prompts._registry import render
from app.services.audit_rules import (
    EXPERT_CHECK_ITEMS, RULE_VERSION, ai_rules, dimension_catalog,
    expert_items, get_rule, rule_catalog,
)
from app.services.audit_scoring import (
    ai_results_to_findings, expert_result_to_findings, merge_findings,
    score_findings,
)
from app.services.preflight_engine import (
    PreflightContext, preflight_stats, run_preflight,
)
from app.services.content_utils import order_sections_dfs
from app.services.standards_registry import (
    STANDARD_DB_CHECKED_AT, STANDARD_DB_VERSION,
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


# @deprecated 孤儿 API：本前端无调用方（UI 已改用 /overview 的 dimensions）；
# 按向后兼容原则保留，大版本评估清理。
@router.get("/dimensions", deprecated=True)
async def list_dimensions():
    return {"items": dimension_catalog(), "rule_version": RULE_VERSION}


@router.get("/expert-review/items")
async def get_expert_items():
    """危大工程专家论证必要项（每项绑定规则 ID 与行业依据）。

    ``items`` 保持原有的字符串数组形态，兼容既有前端调用。
    """
    return {"items": EXPERT_CHECK_ITEMS, "detail": expert_items()}


@router.post("/check")
async def compliance_check(body: ComplianceCheckIn, db=Depends(get_db)):
    scheme_id = body.scheme_id
    # ✅ BUG 修复（2026-09-21）：旧实现无方案存在性校验，不存在的 scheme_id 会
    #    走完整次 AI 调用（空内容 + 空方案名），既浪费额度又返回看似正常的响应，
    #    前端无从判断是「方案不存在」还是「正文恰好都没命中」。现补 404。
    sc_cur = await db.execute("SELECT name, type, project_id FROM schemes WHERE id=?",
                              (scheme_id,))
    sc_row = await sc_cur.fetchone()
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
        checklist = body.checklist
    cur = await db.execute(
        # ✅ 遗留修复（2026-09-22）：sort_order 是「同级内序号」而非全局文档序，
        #    ORDER BY sort_order 会把不同层级的同序号节点排在一起（按"列"展开），
        #    超长方案被 AI_CONTENT_HARD_CAP 截断时，送审的可能不是前几章而是
        #    错乱的碎片。现取 parent_id/sort_order 重排为目录树前序 DFS。
        "SELECT id, parent_id, title, content, sort_order FROM sections"
        " WHERE scheme_id=? AND content!='' ORDER BY sort_order", (scheme_id,))
    sections = [(r["title"], r["content"])
                for r in order_sections_dfs([dict(r) for r in await cur.fetchall()])]
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
                        checklist=json.dumps(checklist, ensure_ascii=False),
                        content=content[:AI_CONTENT_HARD_CAP])
    # ✅ 修复（2026-09-24）：旧实现未传 scene —— 该调用在 ai_audit_logs 里
    #    scene 恒为空串，/ai/stats 场景聚看不到「符合性检查」的消耗，且无法
    #    为其单独配置模型（场景路由按 scene 精确匹配）。
    obj, _ = await collect_json_response(
        [{"role": "system", "content": sys_prompt}],
        lambda o: [] if o.get("results") else ["缺少 results"],
        scene="compliance_check")

    results = _normalize_ai_results(obj.get("results", []), checklist, body.rule_ids)
    # ✅ 根治批次截头（2026-09-23）：本次调用的全部行共享一个 batch_id，
    #    总检聚合按 batch_id 取「最近一批」，不再依赖 rowid 连续段推断
    #    （两次 /check 跨秒交错时 rowid 段会错切，首批被截头/混批）。
    batch_id = uuid.uuid4().hex
    for r in results:
        cid = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO compliance_check (id, scheme_id, project_id, check_type,"
            " rule_id, item, severity, result, suggestion, batch_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, scheme_id, project_id, "compliance", r.get("rule_id", ""),
             r.get("item", ""), r.get("severity", ""),
             json.dumps(r, ensure_ascii=False), r.get("suggestion", ""), batch_id))
    await db.commit()
    return {"results": results, "batch_id": batch_id}


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
    sc_cur = await db.execute("SELECT name, type, project_id FROM schemes WHERE id=?",
                              (scheme_id,))
    sc_row = await sc_cur.fetchone()
    if not sc_row:
        raise HTTPException(404, "方案不存在")
    scheme_name = sc_row["name"]
    scheme_type = sc_row["type"]
    project_id = sc_row["project_id"] or ""

    cur = await db.execute(
        # ✅ 遗留修复（2026-09-22）：目录树按前序 DFS 送 AI，避免扁平序层级错乱
        "SELECT id, parent_id, title, level, sort_order FROM sections"
        " WHERE scheme_id=? ORDER BY sort_order", (scheme_id,))
    outline_rows = order_sections_dfs([
        dict(r) for r in await cur.fetchall()])
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
                        outline_tree=json.dumps(outline_tree, ensure_ascii=False),
                        attachments=json.dumps(attachments, ensure_ascii=False),
                        check_items=check_items)
    # ✅ 修复（2026-09-24）：补 scene（原为空 → 统计归空场景、场景路由配不上）
    obj, _ = await collect_json_response(
        [{"role": "system", "content": sys_prompt}],
        lambda o: [] if "score" in o else ["缺少 score"],
        scene="expert_review")

    result = obj
    cid = str(uuid.uuid4())
    # ✅ 单行也是一批：补 batch_id（与 /check 同口径，历史行空串不影响读取回退）。
    await db.execute(
        "INSERT INTO compliance_check (id, scheme_id, project_id, check_type,"
        " result, batch_id) VALUES (?,?,?,?,?,?)",
        (cid, scheme_id, project_id, "expert_review",
         json.dumps(result, ensure_ascii=False), uuid.uuid4().hex))
    await db.commit()
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
    cur = await db.execute(
        f"SELECT COUNT(*) AS n FROM compliance_check WHERE {where}", params)
    total = (await cur.fetchone())["n"]
    cur = await db.execute(
        f"SELECT * FROM compliance_check WHERE {where}"
        " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
        params + [limit, offset])
    items = [dict(r) for r in await cur.fetchall()]
    return {"items": items, "total": total,
            "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# ✅ 全文一致性审计（产品需求文档 §3.12.1）
#    此前 consistency_audit 表只有删除级联引用、业务读写未实现（死表）。
#    现补齐实现：AI 将「项目关键事实」（global_facts 唯一可信数据源）与正文
#    逐项比对 → 0-100 评分 + 不一致项清单，持久化到 consistency_audit 表。
# ---------------------------------------------------------------------------
@router.post("/consistency-audit/{scheme_id}")
async def run_consistency_audit(scheme_id: str, db=Depends(get_db)):
    from app.routers.sse_handlers import _build_facts_text

    sc_cur = await db.execute("SELECT name, type, project_id FROM schemes WHERE id=?", (scheme_id,))
    sc_row = await sc_cur.fetchone()
    if not sc_row:
        raise HTTPException(404, "方案不存在")

    cur = await db.execute(
        # ✅ 遗留修复（2026-09-22）：审计上下文同样按目录树前序 DFS 送 AI，
        #    保证截断与审阅顺序与文档实序一致
        "SELECT id, parent_id, title, content, sort_order FROM sections"
        " WHERE scheme_id=? AND content!='' ORDER BY sort_order",
        (scheme_id,))
    sections = [(r["title"], r["content"])
                for r in order_sections_dfs([dict(r) for r in await cur.fetchall()])]
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
        lambda o: [] if "score" in o else ["缺少 score"],
        scene="consistency_audit")

    issues = obj.get("issues", []) or []
    audit_id = str(uuid.uuid4())
    # 评分容错：AI 可能返回字符串（如"良好"）或缺失，强制转为 float 失败则记 0
    score_raw = obj.get("score", 0)
    try:
        score_float = float(score_raw)
    except (TypeError, ValueError):
        logger.warning("一致性审计评分非数值，按 0 处理: %r", score_raw)
        score_float = 0.0
    await db.execute(
        "INSERT INTO consistency_audit (id, project_id, scheme_id, score, issues)"
        " VALUES (?,?,?,?,?)",
        (audit_id, sc_row["project_id"], scheme_id,
         score_float, json.dumps(issues, ensure_ascii=False)))
    await db.commit()
    return {"id": audit_id, "score": score_float, "issues": issues}


@router.get("/consistency-audit/{scheme_id}/latest")
async def latest_consistency_audit(scheme_id: str, db=Depends(get_db)):
    cur = await db.execute(
        "SELECT id, score, issues, created_at FROM consistency_audit "
        "WHERE scheme_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (scheme_id,))
    row = await cur.fetchone()
    if not row:
        return {"exists": False}
    item = dict(row)
    try:
        item["issues"] = json.loads(item.get("issues") or "[]")
    except json.JSONDecodeError:
        item["issues"] = []
    item["exists"] = True
    return item


# @deprecated 孤儿 API：本前端无调用方；按向后兼容原则保留，大版本评估清理。
@router.get("/consistency-audit/{scheme_id}/history", deprecated=True)
async def consistency_audit_history(scheme_id: str, limit: int = 10, db=Depends(get_db)):
    limit = max(1, min(limit, 50))
    cur = await db.execute(
        "SELECT id, score, issues, created_at FROM consistency_audit "
        "WHERE scheme_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (scheme_id, limit))
    items = []
    for r in await cur.fetchall():
        item = dict(r)
        try:
            item["issues"] = json.loads(item.get("issues") or "[]")
        except json.JSONDecodeError:
            item["issues"] = []
        items.append(item)
    return {"items": items}


# ---------------------------------------------------------------------------
# ✅ 程序化预检（离线、秒级）
# ---------------------------------------------------------------------------
async def _build_preflight_context(scheme_id: str, db) -> PreflightContext:
    """从 DB 装配预检上下文（章节 + 图表 + 方案元信息）。"""
    sc_cur = await db.execute(
        "SELECT name, type, word_budget FROM schemes WHERE id=?", (scheme_id,))
    sc_row = await sc_cur.fetchone()
    if not sc_row:
        raise HTTPException(404, "方案不存在")

    cur = await db.execute(
        "SELECT id, parent_id, title, content, word_count, level, status, sort_order FROM sections"
        " WHERE scheme_id=? ORDER BY sort_order", (scheme_id,))
    # ✅ 遗留修复（2026-09-22）：预检上下文按目录树前序 DFS 装配，
    #    使重复内容检测（两两配对）之外的序敏感检查与导出链路同口径。
    sections = order_sections_dfs([dict(r) for r in await cur.fetchall()])

    cur = await db.execute(
        "SELECT chart_type, status FROM chart_predictions WHERE scheme_id=?", (scheme_id,))
    charts = [dict(r) for r in await cur.fetchall()]

    return PreflightContext(
        scheme_id=scheme_id,
        scheme_name=sc_row["name"] or "",
        scheme_type=sc_row["type"] or "",
        word_budget=int(sc_row["word_budget"] or 0),
        sections=sections,
        charts=charts,
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
    parts: list[str] = []
    try:
        cur = await db.execute(
            "SELECT sort_order, level, title, word_count, content FROM sections"
            " WHERE scheme_id=? ORDER BY sort_order, level, id", (scheme_id,))
        parts.extend("|".join(str(v) for v in row) for row in await cur.fetchall())
        cur = await db.execute(
            "SELECT chart_type, status FROM chart_predictions WHERE scheme_id=?"
            " ORDER BY rowid", (scheme_id,))
        parts.extend("|".join(str(v) for v in row) for row in await cur.fetchall())
        # 全局事实状态进入总检缓存指纹：事实确认/裁决/重解析后旧总检必须过期。
        cur = await db.execute(
            "SELECT is_simulated,is_resolved,has_conflict,is_stale,updated_at "
            "FROM global_facts WHERE scheme_id=? OR (project_id="
            "(SELECT project_id FROM schemes WHERE id=?) AND "
            "(scheme_id='' OR scheme_id IS NULL)) ORDER BY rowid",
            (scheme_id, scheme_id))
        parts.extend("|".join(str(v) for v in row) for row in await cur.fetchall())
        sc = await db.execute("SELECT word_budget FROM schemes WHERE id=?", (scheme_id,))
        row = await sc.fetchone()
        budget = int(row["word_budget"]) if row and row["word_budget"] else 0
        parts.append(f"budget={budget}")
    except Exception as e:
        logger.warning("内容指纹计算失败（按空指纹处理）: scheme=%s err=%s", scheme_id, e)
        return ""
    return hashlib.md5("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()


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
async def run_preflight_check(scheme_id: str, db=Depends(get_db),
                              force: bool = False):
    """程序化预检：确定性规则，无需 AI，秒级返回。

    覆盖空章节 / 孤立节点 / 字数 / 图表完成率 / 控制字符 / 废止标准 /
    口语化残留 / 未闭合围栏 / 计算书缺失 / 章节查重 / 应急预案要素等硬伤。

    ✅ BUG 修复（2026-09-23）：与 /overview 同构的 G2 并发锁 + 幂等缓存。
       此前本端点绕过了锁与缓存，连点会往 preflight_runs 灌入重复历史。
       向后兼容：``force`` 默认 False（新参数，不传的老调用方首次仍全量计算，
       仅命中缓存时行为不同，且返回体新增 ``cached`` 字段供调用方感知）。
    """
    async with _overview_lock(scheme_id):
        fingerprint = await _content_fingerprint(db, scheme_id)
        hit = _PREFLIGHT_RECENT.get(scheme_id)
        if (not force and hit and hit[0] == fingerprint
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
        _PREFLIGHT_RECENT[scheme_id] = (fingerprint, time.monotonic(), payload)
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
        hit = _OVERVIEW_RECENT.get(scheme_id)
        if not force and hit and hit[0] == fingerprint:
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
        _OVERVIEW_RECENT[scheme_id] = (payload["content_fingerprint"],
                                       time.monotonic(), payload)
        return payload


async def _readiness_overview_compute(db, scheme_id: str) -> dict:
    """总检的实际计算（由 readiness_overview 的并发锁与幂等缓存包着）。"""
    ctx = await _build_preflight_context(scheme_id, db)
    program_findings = run_preflight(ctx)
    sources = ["program"]

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
        cur = await db.execute(
            "SELECT batch_id FROM compliance_check WHERE scheme_id=?"
            " AND check_type='compliance' AND batch_id != ''"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1", (scheme_id,))
        _brow = await cur.fetchone()
        _batch = (_brow["batch_id"] if _brow else "") or ""
        if _batch:
            cur = await db.execute(
                "SELECT result FROM compliance_check WHERE scheme_id=?"
                " AND check_type='compliance' AND batch_id=? ORDER BY rowid",
                (scheme_id, _batch))
        else:
            cur = await db.execute(
                "SELECT MIN(rowid) AS min_rowid, created_at"
                " FROM compliance_check WHERE scheme_id=? AND check_type='compliance' "
                "GROUP BY created_at ORDER BY created_at DESC LIMIT 1",
                (scheme_id,))
            last = await cur.fetchone()
            if last:
                cur = await db.execute(
                    "SELECT result FROM compliance_check WHERE scheme_id=? AND check_type='compliance'"
                    " AND rowid >= ? ORDER BY rowid",
                    (scheme_id, last["min_rowid"]))
            else:
                cur = None
        if cur is not None:
            rows = []
            for r in await cur.fetchall():
                try:
                    rows.append(json.loads(r["result"] or "{}"))
                except json.JSONDecodeError:
                    continue
            ai_findings = ai_results_to_findings(rows)
            if rows:
                sources.append("compliance")
    except Exception as e:
        logger.warning("overview: 读取 AI 规范符合性结果失败（跳过）: %s", e)

    # --- 最近一次一致性审计（作为一致性维度的补充证据）---
    try:
        cur = await db.execute(
            "SELECT score, issues FROM consistency_audit WHERE scheme_id=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1", (scheme_id,))
        row = await cur.fetchone()
        if row:
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
        cur = await db.execute(
            "SELECT scan_id FROM consistency_conflicts WHERE scheme_id=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1", (scheme_id,))
        last_scan = await cur.fetchone()
        if last_scan:
            cur = await db.execute(
                "SELECT conflict_type, severity, topic, occurrences,"
                " authoritative_value, repair_instruction, status"
                " FROM consistency_conflicts WHERE scheme_id=? AND scan_id=?"
                " ORDER BY created_at, rowid",
                (scheme_id, last_scan["scan_id"]))
            scan_conflicts = [dict(r) for r in await cur.fetchall()]
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
        cur = await db.execute(
            "SELECT result FROM compliance_check WHERE scheme_id=? AND check_type='expert_review'"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1", (scheme_id,))
        row = await cur.fetchone()
        if row:
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
        "created_at": datetime.now().isoformat(timespec="seconds"),
        # ✅ G3：落库后供 /runs /report / 导出页判定「结论是否已过期」
        "content_fingerprint": await _content_fingerprint(db, scheme_id),
    })
    await _persist_run(db, scheme_id, payload, stats)
    return payload


async def _persist_run(db, scheme_id: str, payload: dict, stats: dict):
    """落库一次预检运行（用于分数趋势与审计留痕）。

    落库失败不影响返回结果 —— 评分结论是用户此刻要的东西，
    不能因为历史表写入异常就让用户拿不到结论。
    """
    try:
        # ✅ G5（2026-09-21）：preflight_runs 补齐 project_id（此前无此列），
        # 与 compliance_check / consistency_audit 的项目维度口径对齐；
        # ✅ G3：内容指纹随运行落库，展示层据此判定结论是否已过期。
        from app.routers.review import _scheme_project_id
        await db.execute(
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
             json.dumps(stats or {}, ensure_ascii=False)))
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
    cur = await db.execute(
        "SELECT id, total, grade, verdict, released, blocked, counts, rule_version,"
        " content_fingerprint, created_at FROM preflight_runs WHERE scheme_id=?"
        " ORDER BY created_at DESC, rowid DESC LIMIT ?", (scheme_id, limit))
    rows = [dict(r) for r in await cur.fetchall()]
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
    cur = await db.execute(
        "SELECT total, grade, verdict, released, blocked, dimensions, findings,"
        " stats, content_fingerprint, created_at FROM preflight_runs WHERE scheme_id=?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1", (scheme_id,))
    row = await cur.fetchone()
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

    sc = await db.execute("SELECT name, type FROM schemes WHERE id=?", (scheme_id,))
    sc_row = await sc.fetchone()
    name = sc_row["name"] if sc_row else ""

    # ✅ G3（2026-09-21）：报告可能基于旧正文 —— 整改清单被抄进评审意见后，
    # 若正文已改过，按旧结论整改就是白干。报告头部显式标注时效状态。
    stale = await _run_is_stale(db, scheme_id, row)
    stale_line = ("- ⚠️ 时效状态：**已过期**（本次预检之后正文 / 图表已发生变更，"
                  "结论可能失效，请先重新执行「一键总检」）"
                  if stale else
                  "- 时效状态：有效（正文 / 图表与本次预检一致）")

    lines = [
        f"# 专项方案审核预检报告",
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