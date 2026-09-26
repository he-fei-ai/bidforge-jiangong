"""冲突仲裁器（F-AGENT-CONSISTENCY-REPAIR §5.2）

对扫描出的冲突清单：
1. 按权威值优先级确定权威值与来源
2. 按严重程度定级（高/中/低）
3. 生成逐条修复指令
4. 无法确定权威值的标记为 skipped（不强行修复）

优先使用全局事实做程序化命中（最高优先级、确定性最强），
其余冲突交 AI 一次性批量仲裁，结果回写 consistency_conflicts。
"""
from __future__ import annotations

import json
import logging
import re

from app.services.ai.json_response import collect_json_response
from app.services.ai.prompts._registry import render

logger = logging.getLogger("conflict_arbiter")

SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1}
# 单次 AI 仲裁的冲突条数上限：冲突可达上百条（长方案全文一致性扫描），
# 一次性塞入会导致 prompt 超长、超时与 JSON 截断。分批请求，逐批降级
# （单批失败不影响其他批次走规则仲裁）。
ARBITRATE_BATCH_SIZE = 20


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit]


def match_global_facts(conflict: dict, facts: str) -> dict | None:
    """用全局事实文本为冲突匹配权威值（确定性规则，命中即最高优先级）。

    规则：以冲突主题关键词在事实文本中找行，再在该行中找出现有取值，
    若能唯一定位到一个不同于多数取值的值，则采信。
    返回 {value, source} 或 None。
    """
    if not facts:
        return None
    topic = conflict.get("topic") or ""
    values = [o.get("value", "") for o in conflict.get("occurrences", []) if o.get("value")]
    if not topic or not values:
        return None
    # 取主题的 2-6 字关键词（去通用后缀）
    keyword = re.sub(r"(项目总|本工程|本项目)", "", topic)
    keyword = keyword[:6] or topic[:4]
    for line in facts.splitlines():
        line = line.strip()
        if not line or keyword not in line:
            continue
        # 事实行中若直接出现某个现有值，说明该值有事实背书
        hit = next((v for v in set(values) if v and v in line), None)
        if hit:
            return {"value": hit, "source": f"全局事实变量 · {topic}"}
    return None


def majority_value(conflict: dict) -> tuple[str, int]:
    """多数章节一致值（出现次数最多的取值）。"""
    counter: dict[str, int] = {}
    for o in conflict.get("occurrences", []):
        v = (o.get("value") or "").strip()
        if v:
            counter[v] = counter.get(v, 0) + 1
    if not counter:
        return "", 0
    value, cnt = max(counter.items(), key=lambda kv: kv[1])
    return value, cnt


async def _arbitrate_batch(batch: list[dict], *, facts: str, design_docs: str,
                           standards: str, project_requirements: str) -> dict[str, dict]:
    """单批 AI 仲裁，返回 conflict_id -> 仲裁结果。失败时返回空（调用方降级）。"""
    compact = [{
        "conflict_id": c["id"],
        "type": c.get("conflict_type"),
        "topic": c.get("topic"),
        "occurrences": [
            {"section_id": o.get("section_id"), "section_title": o.get("section_title"),
             "value": o.get("value"), "text": _clip(o.get("text", ""), 80)}
            for o in c.get("occurrences", [])
        ],
    } for c in batch]

    user = render(
        "consistency_arbitrate_user",
        global_facts=facts or "（无）",
        design_docs_summary=design_docs or "（无）",
        standards_summary=standards or "（无）",
        project_requirements=project_requirements or "（无）",
        conflicts=json.dumps(compact, ensure_ascii=False),
    )
    obj, _ = await collect_json_response(
        [{"role": "system", "content": render("consistency_arbitrate_system")},
         {"role": "user", "content": user}],
        lambda o: [] if isinstance(o.get("arbitrations"), list) else ["缺少 arbitrations"],
        max_retries=1, temperature=0.1, json_mode=True, timeout=240,
        scene="consistency_arbitrate")
    rows = obj.get("arbitrations") or []
    return {str(r.get("conflict_id")): r for r in rows if isinstance(r, dict)}


async def ai_arbitrate(conflicts: list[dict], *, facts: str, design_docs: str,
                       standards: str, project_requirements: str) -> dict[str, dict]:
    """AI 分批仲裁，返回 conflict_id -> 仲裁结果。

    按 ARBITRATE_BATCH_SIZE 分批（避免上百条冲突导致 prompt 超长/超时/JSON 截断），
    单批失败只降级该批（该批冲突随后走多数一致/skipped 规则仲裁），不影响其他批次。
    """
    if not conflicts:
        return {}
    results: dict[str, dict] = {}
    for i in range(0, len(conflicts), ARBITRATE_BATCH_SIZE):
        batch = conflicts[i:i + ARBITRATE_BATCH_SIZE]
        try:
            results.update(await _arbitrate_batch(
                batch, facts=facts, design_docs=design_docs,
                standards=standards, project_requirements=project_requirements))
        except Exception as e:
            logger.warning(
                "AI 仲裁第 %d 批（%d 条）失败，该批降级为规则仲裁: %s",
                i // ARBITRATE_BATCH_SIZE + 1, len(batch), e)
    return results


def build_repair_instruction(conflict: dict, auth_value: str) -> str:
    wrong = sorted({o.get("value", "") for o in conflict.get("occurrences", [])
                    if o.get("value") and o.get("value") != auth_value})
    if not wrong:
        return f"将「{conflict.get('topic')}」统一为 {auth_value}"
    if len(wrong) <= 3:
        return f"将 {('/'.join(wrong))} 统一改为 {auth_value}"
    return f"将与权威值不一致的「{conflict.get('topic')}」取值统一改为 {auth_value}"


async def arbitrate_conflicts(db, conflicts: list[dict], *, facts: str,
                              design_docs: str = "", standards: str = "",
                              project_requirements: str = "") -> list[dict]:
    """对整批冲突执行仲裁，回写权威值/修复指令/等级，返回更新后的冲突清单。"""
    ai_results = await ai_arbitrate(
        conflicts, facts=facts, design_docs=design_docs,
        standards=standards, project_requirements=project_requirements)

    out: list[dict] = []
    update_rows: list[tuple] = []
    for c in conflicts:
        cid = c["id"]
        ai = ai_results.get(cid, {})

        # 1) 全局事实程序化命中（最高优先级，优先于 AI）
        fact_hit = match_global_facts(c, facts)

        auth_value = (fact_hit or {}).get("value") or ai.get("authoritative_value") or ""
        auth_source = ((fact_hit or {}).get("source")
                       or ai.get("authoritative_source") or "")
        reason = ai.get("reason", "")
        severity = ai.get("severity") or c.get("severity") or "medium"
        if severity not in SEVERITY_ORDER:
            severity = "medium"
        # 命中全局事实/设计/规范类，等级不低于 high
        if fact_hit or c.get("conflict_type") in ("facts", "design", "standard"):
            severity = "high"

        # 2) 无权威值：退化为多数一致值（仍无法判定则 skipped）
        skipped = False
        if not auth_value:
            major, cnt = majority_value(c)
            n_sec = len({o.get("section_id") for o in c.get("occurrences", [])})
            if major and cnt >= 2 and cnt > n_sec / 2:
                auth_value = major
                auth_source = auth_source or "多数章节一致值"
                reason = reason or "多数章节采用同一取值，按多数一致原则仲裁"
            else:
                skipped = True
                auth_value = ""
                auth_source = ""
                reason = reason or "无法确定权威值，建议人工确认"

        instruction = "" if skipped else (
            ai.get("repair_instruction")
            or build_repair_instruction(c, auth_value))

        status = "skipped" if skipped else "pending"
        c.update({
            "severity": severity,
            "authoritative_value": auth_value,
            "authoritative_source": auth_source,
            "repair_instruction": instruction,
            "reason": reason,
            "status": status,
        })
        out.append(c)
        update_rows.append(
            (severity, auth_value, auth_source, instruction, reason, status, cid))

    # 单事务批量回写（旧实现逐行 execute 后统一 commit，行数多时 round-trip 放大）
    try:
        await db.executemany(
            "UPDATE consistency_conflicts SET severity=?, authoritative_value=?,"
            " authoritative_source=?, repair_instruction=?, reason=?, status=? WHERE id=?",
            update_rows)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return out
