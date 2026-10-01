"""定向修复 Agent（F-AGENT-CONSISTENCY-REPAIR §5.3）

- 按章节分组冲突，逐章调用 LLM 做最小必要修改
- 修复后经 repair_validator 二次校验，失败重试一次，仍失败标 failed（不写库）
- 修复前统一保存版本快照；修复结果直接写入 sections，等待用户确认/回滚
  （接受 = 保留；拒绝某条 = 该章整体恢复快照原文，因为一章的多处修改是一次成稿）
- 只改正文，不触碰图表代码
"""
from __future__ import annotations

import asyncio
import json
import logging

from datetime import datetime

from app.config import settings
from app.services.ai.provider_factory import chat_with_fallback
from app.services.ai.prompts._registry import render
from app.services.content_utils import (
    text_word_count, word_status_for, DEFAULT_WORD_BUDGET,
)
from app.services.repair_validator import validate_repair
from app.services import repair_record
# ✅ 2026-09-30 第十五轮：定点编辑（old_text/new_text，对齐参考软件 §三.4/§五.1）
from app.services import consistency_edits

logger = logging.getLogger("repair_agent")

REPAIR_TIMEOUT = 120
SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}

# ---------- 修复阶段降本（2026-09-22） ----------
# 实测：扫描优化后，定向修复成为新大头（12 章方案占 42.9%，= 涉及章节数 × 1~2 次）。
#   ① 并发：逐章串行 → Semaphore 并发（只降墙钟，调用数不变）；
#   ② 跳过「已修复且修复成果仍在正文里」的冲突（避免同一处反复花钱修）；
#   ③ 章节数上限：长方案一次只修最严重的 N 章，其余本轮跳过（可下一轮继续）；
#   ④ 重试收敛：仅 AI 调用异常才重试，校验不合格不再盲目重试第二次。
REPAIR_CONCURRENCY = max(
    1, int(getattr(settings, "consistency_repair_concurrency", 3) or 1))
REPAIR_MAX_SECTIONS = max(
    0, int(getattr(settings, "consistency_repair_max_sections", 40) or 0))
REPAIR_RETRY_ON_INVALID = bool(
    getattr(settings, "consistency_repair_retry_on_invalid", False))
# ✅ 2026-09-22（调用次数优化 O8）：按严重度分级的重试。默认关闭重试可省
#    调用，但基线实测省重试会让 **3/12 的高危冲突留在交付文档里**（错误参数/
#    标号直接进文档，质量代价远大于省下的调用）。开启后：组内含 high 级冲突
#    时校验不过仍重试一次；medium/low 维持不重试（旧行为）。
REPAIR_RETRY_ON_INVALID_BY_SEVERITY = bool(
    getattr(settings, "consistency_repair_retry_on_invalid_by_severity", True))
# 「修复成果仍在」的比对片段长度（取修复后正文开头，兼顾性能与误判）
REPAIR_MARK_SNIPPET = 200


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit]


def _clean_repair_output(text: str, section_title: str) -> str:
    """剥离模型可能误加的 Markdown 代码块包裹与重复的章节标题。"""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # 去掉开头重复的 "# 标题"
    if section_title:
        first = text.splitlines()[0] if text else ""
        if first.lstrip("#").strip() == section_title.strip():
            text = text[len(first):].lstrip("\n")
    return text


def filter_conflicts(conflicts: list[dict], *, severity_threshold: str = "high",
                     conflict_ids: list[str] | None = None) -> list[dict]:
    """按等级阈值与冲突 ID 白名单筛选；无权威值（skipped）的不修复。"""
    threshold = SEVERITY_RANK.get(severity_threshold, 3)
    id_set = set(conflict_ids or [])
    out = []
    for c in conflicts:
        if not c.get("authoritative_value"):
            continue
        if id_set and c["id"] not in id_set:
            continue
        if not id_set and SEVERITY_RANK.get(c.get("severity", "medium"), 2) < threshold:
            continue
        if c.get("status") in ("accepted", "repaired"):
            # 已确认接受的不再重复修复；repaired_pending 的可继续
            continue
        out.append(c)
    return out


def group_by_section(conflicts: list[dict]) -> dict[str, dict]:
    """把冲突按出现章节分组，仅纳入取值与权威值不一致的出现点。"""
    groups: dict[str, dict] = {}
    for c in conflicts:
        auth = c.get("authoritative_value", "")
        for occ in c.get("occurrences", []):
            sid = occ.get("section_id")
            value = occ.get("value", "")
            if not sid:
                continue
            if value and auth and value == auth:
                continue  # 该章节本来就是权威值，无需改
            g = groups.setdefault(sid, {
                "section_id": sid,
                "section_title": occ.get("section_title", ""),
                "conflicts": [],
            })
            g["conflicts"].append({
                "conflict_id": c["id"],
                "topic": c.get("topic"),
                "type": c.get("conflict_type"),
                # ✅ 2026-09-22：带上严重度，供「单次修复章节数上限」按严重度排序取舍
                "severity": c.get("severity", "medium"),
                "current_value": value,
                "authoritative_value": auth,
                "authoritative_source": c.get("authoritative_source"),
                "repair_instruction": c.get("repair_instruction"),
                "quote": _clip(occ.get("text", ""), 120),
            })
    return groups


async def repair_section(*, section_id: str, section_title: str, section_content: str,
                         conflicts_in_section: list[dict], facts: str,
                         sources: str) -> str:
    """调用 LLM 修复单章，返回修复后的完整正文（纯文本）。

    ✅ 2026-09-30 第十五轮：改为**定点编辑优先、整章重写兜底**
    （对齐《标书智能体（三）》§三.4 / §五.1）。旧实现无论冲突大小都让模型
    重写整章并整列覆盖 ``sections.content``，一处「工期 120 vs 90」会连带
    改写章内已正确的内容；而拒绝单条冲突时又要把**整章**退回快照，同章其它
    已修好的冲突一起丢。现先要「old_text/new_text」定点编辑，全部唯一命中
    才落库；拿不到可用编辑时**回落到整章重写**（保证不因模型不支持 JSON
    而让修复能力整体失效）。
    """
    # ① 定点编辑优先
    try:
        edit_res = await consistency_edits.collect_repair_edits(
            section_id=section_id, section_title=section_title,
            section_content=section_content,
            conflicts_in_section=conflicts_in_section,
            facts=facts, sources=sources,
            # 注入模块级引用：既有单测 monkeypatch 的是 repair_agent 的
            # chat_with_fallback，不注入就会绕过它们去打真实 provider。
            chat_fn=chat_with_fallback)
        if edit_res.applied > 0 and edit_res.content.strip() != (section_content or "").strip():
            logger.info("[一致性修复] 第 %s 章定点编辑命中 %d 处（不改其余内容）",
                        section_id, edit_res.applied)
            return edit_res.content
        if edit_res.applied > 0:
            # 编辑命中但内容没变 → 模型空转，按未修复处理并回落到重写
            logger.info("[一致性修复] 第 %s 章定点编辑内容无变化，回落整章重写", section_id)
    except Exception as e:  # noqa: BLE001 - 定点编辑任何异常都不得阻断修复
        logger.warning("[一致性修复] 第 %s 章定点编辑异常，回落整章重写: %s",
                       section_id, e)

    # ② 整章重写兜底（既有行为，保持不变）
    user = render(
        "consistency_repair_user",
        global_facts=facts or "（无）",
        authoritative_sources=sources or "（见各冲突项权威值）",
        section_id=section_id,
        section_title=section_title,
        section_content=section_content,
        conflicts_in_section=json.dumps(conflicts_in_section, ensure_ascii=False, indent=2),
    )
    system = render("consistency_repair_system")
    raw = await chat_with_fallback(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        temperature=0.2, timeout=REPAIR_TIMEOUT, scene="consistency_repair")
    return _clean_repair_output(raw, section_title)


# ---------- ② 跳过「已修复且修复成果仍在」的冲突 ----------
async def load_repaired_marks(db, scheme_id: str,
                              limit: int = 20) -> set[tuple[str, str, str]]:
    """历史已修复痕迹集合：{(section_id, topic, 修复后正文片段)}。

    读取失败返回空集合 —— 该能力只是省调用，失败时退化为「不跳过」，绝不阻断。
    """
    try:
        cur = await db.execute(
            "SELECT items FROM consistency_repairs WHERE scheme_id=?"
            " ORDER BY created_at DESC LIMIT ?", (scheme_id, limit))
        rows = await cur.fetchall()
    except Exception as e:
        logger.warning("读取历史修复记录失败（本次不跳过重复修复）: %s", e)
        return set()
    marks: set[tuple[str, str, str]] = set()
    for r in rows:
        try:
            payload = json.loads(dict(r)["items"] or "[]")
        except Exception:
            continue
        for it in payload:
            if not isinstance(it, dict) or it.get("status") != "repaired":
                continue
            sid = str(it.get("section_id") or "")
            topic = str(it.get("topic") or "")
            after = (it.get("after") or "")[:REPAIR_MARK_SNIPPET]
            if sid and topic and after:
                marks.add((sid, topic, after))
    return marks


def filter_already_repaired(section_id: str, conflicts_in_section: list[dict],
                            content: str,
                            marks: set[tuple[str, str, str]],
                            ) -> tuple[list[dict], list[dict]]:
    """拆分 (仍需修复的冲突, 可跳过的冲突)。

    跳过条件：历史上该章节同一主题**已修复成功**，且**修复后的正文片段仍在当前
    正文中** —— 说明上一次修复成果没被后续生成覆盖，不必再花一次 AI 调用重修。
    （纯函数，可单测）
    """
    content = content or ""
    todo: list[dict] = []
    skipped: list[dict] = []
    for c in conflicts_in_section or []:
        topic = str(c.get("topic") or "")
        hit = any(m[0] == str(section_id) and m[1] == topic and m[2] in content
                  for m in marks or ())
        (skipped if hit else todo).append(c)
    return todo, skipped


def _section_priority(group: dict) -> tuple[int, int]:
    """章节取舍优先级：组内最高严重度 → 冲突条数（严重、问题多的先修）。"""
    sev = max(SEVERITY_RANK.get(str(c.get("severity") or "medium"), 2)
              for c in group.get("conflicts", [])) if group.get("conflicts") else 0
    return (sev, len(group.get("conflicts", [])))


def _group_has_high_severity(group: dict) -> bool:
    """章节组内是否包含 high 级冲突（O8 分级重试的判定，纯函数可单测）。"""
    return any(str(c.get("severity") or "").lower() == "high"
               for c in (group.get("conflicts") or []))


def limit_repair_sections(work: list[tuple[str, dict]],
                          max_sections: int) -> tuple[list[tuple[str, dict]],
                                                      list[tuple[str, dict]]]:
    """按「单次修复章节数上限」取舍，返回 (本轮修复, 本轮跳过)。

    max_sections <= 0 表示不限（旧行为）。（纯函数，可单测）
    """
    if max_sections and max_sections > 0 and len(work) > max_sections:
        ranked = sorted(work, key=lambda kv: _section_priority(kv[1]), reverse=True)
        return ranked[:max_sections], ranked[max_sections:]
    return list(work), []


async def run_repair(db, *, scheme_id: str, scan_id: str, conflicts: list[dict],
                     mode: str = "auto", severity_threshold: str = "high",
                     conflict_ids: list[str] | None = None,
                     contexts: dict | None = None,
                     force_full_repair: bool = False,
                     progress_cb=None) -> dict:
    """执行一次修复批次，返回 repair_record.save_repair 的结果。

    ``force_full_repair=True``：关闭「跳过已修复且成果仍在」的优化，本批对全部
    命中冲突重新调用 AI 修复（用户想强制重修一遍时使用，代价是多花调用）。
    默认 False —— 已修复且正文未被覆盖的冲突不再重复花钱修。
    """
    contexts = contexts or {}
    facts = contexts.get("global_facts", "")

    targets = filter_conflicts(
        conflicts, severity_threshold=severity_threshold, conflict_ids=conflict_ids)
    groups = group_by_section(targets)

    # 修复前快照（所有涉及章节的当前正文）
    snapshot_sections: list[dict] = []
    section_contents: dict[str, str] = {}
    section_budgets: dict[str, int] = {}
    for sid, g in groups.items():
        cur = await db.execute(
            "SELECT title, content, word_budget FROM sections WHERE id=?", (sid,))
        row = await cur.fetchone()
        if not row:
            continue
        content = row[1] or ""
        section_contents[sid] = content
        section_budgets[sid] = row[2] or DEFAULT_WORD_BUDGET
        g["section_title"] = row[0] or g["section_title"]
        snapshot_sections.append({"section_id": sid, "content_before": content})

    snapshot_id = await repair_record.create_snapshot(db, scheme_id, snapshot_sections)

    items: list[dict] = []
    repaired_conflicts: set[str] = set()
    failed_conflicts: set[str] = set()

    # ---------- ② 跳过「已修复且修复成果仍在正文中」的冲突 ----------
    marks = (await load_repaired_marks(db, scheme_id)
             if (groups and not force_full_repair) else set())
    work: list[tuple[str, dict]] = []
    for sid, g in groups.items():
        before_now = section_contents.get(sid, "")
        todo, already = filter_already_repaired(
            sid, g["conflicts"], before_now, marks)
        for c in already:
            items.append({
                "conflict_id": c["conflict_id"], "section_id": sid,
                "section_title": g["section_title"], "topic": c["topic"],
                "current_value": c["current_value"],
                "authoritative_value": c["authoritative_value"],
                "before": _clip(before_now, 4000),
                "after": _clip(before_now, 4000),
                "status": "skipped",
                "problems": ["该处已修复且修复成果仍在正文中，无需重复修复"],
            })
        if todo:
            g["conflicts"] = todo
            work.append((sid, g))

    # ---------- ③ 单次修复章节数上限（长方案分批修，避免超长任务） ----------
    work, overflow = limit_repair_sections(work, REPAIR_MAX_SECTIONS)
    for sid, g in overflow:
        for c in g["conflicts"]:
            items.append({
                "conflict_id": c["conflict_id"], "section_id": sid,
                "section_title": g["section_title"], "topic": c["topic"],
                "current_value": c["current_value"],
                "authoritative_value": c["authoritative_value"],
                "before": _clip(section_contents.get(sid, ""), 4000),
                "after": _clip(section_contents.get(sid, ""), 4000),
                "status": "skipped",
                "problems": ["超出单次修复章节数上限（按严重度排序后本轮跳过，"
                             "可在下一轮继续或人工处理）"],
            })

    total_groups = len(work)
    slots: list[list[dict]] = [[] for _ in range(len(work))]
    sem = asyncio.Semaphore(REPAIR_CONCURRENCY)
    done = 0
    _write_lock = asyncio.Lock()

    async def _repair_one(pos: int, sid: str, g: dict) -> None:
        """修复单个章节（并发执行单元），结果写回 slots[pos] 保持顺序稳定。"""
        nonlocal done
        async with sem:
            title = g["section_title"]
            before = section_contents.get(sid, "")
            confs = g["conflicts"]
            wrong_values = sorted({c["current_value"] for c in confs if c["current_value"]})
            auth_values = sorted({c["authoritative_value"] for c in confs
                                  if c["authoritative_value"]})
            sources = "\n".join(
                f"- 「{c['topic']}」权威值：{c['authoritative_value']}"
                f"（来源：{c.get('authoritative_source') or '未注明'}）"
                for c in confs)
            if progress_cb:
                await progress_cb(
                    pos + 1, total_groups,
                    f"正在修复章节 {pos + 1}/{total_groups}：{title}")

            after, ok, problems = "", False, ["未执行"]
            for attempt in (1, 2):
                try:
                    # ✅ BUG 修复（2026-09-16 · 依据运行库 consistency_repairs 真实记录）：
                    #    此处漏传 repair_section 的**必填关键字参数 section_id**，导致
                    #    每次调用都抛
                    #      TypeError: repair_section() missing 1 required keyword-only argument
                    #    修复轮两次尝试全部异常 → 所有冲突判 failed、repaired 恒为 0。
                    #    实测：consistency_repairs 唯一一条记录 repaired=0 / failed=13，
                    #    13 条 problems 全是这条 TypeError —— 即「全文一致性自动修复」
                    #    自该签名变更以来从未真正生效过。
                    after = await repair_section(
                        section_id=sid,
                        section_title=title, section_content=before,
                        conflicts_in_section=[{**c, "section_id": sid} for c in confs],
                        facts=facts, sources=sources)
                    ok, problems = validate_repair(
                        before=before, after=after,
                        wrong_values=wrong_values,
                        authoritative_value=auth_values[0] if len(auth_values) == 1 else "")
                    if ok:
                        break
                    logger.info("章节 %s 第 %d 次修复未通过校验：%s",
                                title, attempt, problems)
                    # ✅ ④ 重试收敛（2026-09-22）：AI 正常返回但校验不合格时，
                    #    再问一次多半仍不合格（实测修复失败多为模型能力/口径问题），
                    #    默认不再花第二次调用（可用 consistency_repair_retry_on_invalid
                    #    打开旧行为）。AI 调用本身异常走 except，仍会重试一次。
                    # ✅ 2026-09-22（调用次数优化 O8）：按严重度分级 —— 组内含
                    #    high 级冲突时校验不过仍重试一次（高危冲突留错值会直接
                    #    进交付文档，质量代价远大于省下的调用）。
                    if not REPAIR_RETRY_ON_INVALID:
                        if not (REPAIR_RETRY_ON_INVALID_BY_SEVERITY
                                and _group_has_high_severity(g)):
                            break
                except Exception as e:
                    logger.warning("章节 %s 第 %d 次修复异常：%s", title, attempt, e)
                    problems = [str(e)[:150]]

            cids = [c["conflict_id"] for c in confs]
            if ok and after:
            # ✅ BUG 修复（2026-09-16 · 依据运行库字数口径普查）：旧实现写
            #    `word_count=len(after)` 且不更新 word_status —— 两个问题：
            #      ① 字数口径与全项目唯一口径（content_utils.text_word_count，
            #         剔除```图表代码块）不一致：带图章节的字数被图表代码抬高；
            #      ② word_status 停留在修复前的值，与修复后的实际字数脱节。
            #    实测（运行库）：3 章 word_count 被污染，其中「进度计划横道图与网络图」
            #    实际 1718 字（normal）却被记为 2608 字判为 over —— 用户会看到
            #    假的"超字数"，进而触发无意义的压缩。
                _wc = text_word_count(after)
                _wb = section_budgets.get(sid) or DEFAULT_WORD_BUDGET
                # ✅ 并发写库串行化：同一连接上的 UPDATE + COMMIT 不与其他章节交错
                async with _write_lock:
                    await db.execute(
                        "UPDATE sections SET content=?, word_count=?, word_status=?, "
                        "updated_at=? WHERE id=?",
                        (after, _wc, word_status_for(_wc, _wb),
                         datetime.now().isoformat(), sid))
                    await db.commit()
                repaired_conflicts.update(cids)
                status = "repaired"
            else:
                failed_conflicts.update(cids)
                status = "failed"

            done += 1
            if progress_cb:
                await progress_cb(done, total_groups,
                                  f"已完成 {done}/{total_groups} 章修复")
            slots[pos] = [{
                "conflict_id": c["conflict_id"],
                "section_id": sid,
                "section_title": title,
                "topic": c["topic"],
                "current_value": c["current_value"],
                "authoritative_value": c["authoritative_value"],
                "before": _clip(before, 4000),
                "after": _clip(after, 4000) if after else before,
                "status": status,
                "problems": [] if ok else problems,
            } for c in confs]

    # ① 并发执行（旧实现为逐章串行）
    if work:
        await asyncio.gather(*[_repair_one(i, sid, g)
                               for i, (sid, g) in enumerate(work)])
        for part in slots:
            items.extend(part or [])

    # skipped（无权威值）+ 阈值/白名单外的冲突计入 total 但不修复
    skipped_ids = {c["id"] for c in conflicts if not c.get("authoritative_value")}
    for cid in skipped_ids:
        if cid not in repaired_conflicts and cid not in failed_conflicts:
            items.append({"conflict_id": cid, "section_id": "", "status": "skipped",
                          "problems": ["无法确定权威值，建议人工确认"]})

    # 回写冲突状态
    for cid in repaired_conflicts:
        await db.execute(
            "UPDATE consistency_conflicts SET status='repaired' WHERE id=?", (cid,))
    for cid in failed_conflicts:
        await db.execute(
            "UPDATE consistency_conflicts SET status='failed' WHERE id=?", (cid,))
    await db.commit()

    # ✅ 统计口径统一（2026-09-16 · 依据运行库记录）：旧实现 total_conflicts 按
    #    **冲突**计数（len(targets)+len(skipped)），而 repaired/failed/skipped 由
    #    items 按 **章节×冲突** 计数 —— 同一批修复出现「发现 9 处，失败 13 处」
    #    这种自相矛盾的展示（实测 consistency_repairs.total_conflicts=9, failed=13）。
    #    现统一按冲突 id 计数，并保证 repaired + failed + skipped == total 恒成立。
    all_ids = {c["id"] for c in conflicts}
    stats = {
        "repaired": len(repaired_conflicts & all_ids),
        "failed": len(failed_conflicts & all_ids),
    }
    stats["skipped"] = max(len(all_ids) - stats["repaired"] - stats["failed"], 0)
    result = await repair_record.save_repair(
        db, scheme_id=scheme_id, scan_id=scan_id, mode=mode,
        items=items, snapshot_id=snapshot_id, total_conflicts=len(all_ids),
        stats=stats)
    logger.info("修复批次 %s 完成：成功 %d 失败 %d 跳过 %d（冲突总数 %d）",
                result["repair_id"], result["repaired"], result["failed"],
                result["skipped"], result["total_conflicts"])
    return result
