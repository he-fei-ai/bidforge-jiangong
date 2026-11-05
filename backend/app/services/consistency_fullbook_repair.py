"""✅ R122-G2（2026-10-21 · 引入易标「持久 Agent 整卷修复」的程序级等价物）

易标（OpenBidKit_Yibiao）用**持久 Agent 会话**在整卷工作区
（`global-facts.md` + `technical-plan.md`）里多轮搜索 / 分段读取 / 修改，最后由
程序校验章节完整性 / ID 标记 / 修改范围、只回写变化小节
（参考 client/electron/services/contentGenerationTask.cjs:5490/5507/5579）。

本软件**刻意不移植持久会话基建**（同 ``routers/sections.py:1665`` 既有裁定：
「参考软件的持久 Agent 会话基建本软件无此设施，照搬成本/风险过高」），改用
**少轮整卷往返 + 程序级小节闸门**实现同等安全语义：

1. 把「全局事实 + 冲突清单 + 全部末级章节」装配成带 ``<!-- §:id -->`` 小节标记的
   整卷工作区文本，交给 AI **一次性**就地修订（不重写结构、只改冲突取值）。
2. 程序按标记把回包切回各章，施加**四道闸门**（任一不过即拒绝）：
   - 闸门 A（ID 标记）：回包出现原卷不存在的 section_id → **整包拒绝**；
   - 闸门 B（章节完整性）：原卷某章在回包缺失 → **整包拒绝**（绝不静默丢章）；
   - 闸门 C（非空改空）：某章改前非空、改后为空 → **该章拒绝回写**；
   - 闸门 D（结构 / 取值）：复用 :func:`repair_validator.validate_repair`
     （标题 / 表格 / 围栏 / 图表块数不减、冲突取值不残留、权威值出现）→ 该章拒绝回写。
3. **只回写通过闸门且确有变化**的小节；修复前统一快照（create_snapshot）、
   落库走 save_repair(mode="fullbook")，与既有逐章修复链路共用 confirm / rollback。

默认关闭（`config.consistency_fullbook_repair_enabled=False`）：关闭时本模块
**从不被调用**，``repair_agent.run_repair`` 逐字节保持既有链路。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from app.services import repair_record
from app.services.ai.prompts._registry import render
from app.services.ai.provider_factory import chat_with_fallback
from app.services.content_utils import (
    DEFAULT_WORD_BUDGET,
    text_word_count,
    word_status_for,
)
from app.services.repair_validator import validate_repair

logger = logging.getLogger("consistency_fullbook_repair")

# 整卷工作区单轮上限（字符）：超限直接放弃整卷模式（交回调用方按逐章修），
# 避免「为省轮次把十万字塞一次」导致模型必然截短 → 闸门全拒 → 白烧大调用。
FULLBOOK_MAX_CHARS = 200000
# 整包被闸门 A/B 拒时的重试上限（对齐易标 maxRetries=2 ⇒ 1 生成 + 2 修复 = 3 次，
# 与 json_response.MAX_REPAIR_RETRIES 同值；这里只做「结构级」重问，不做逐章兜底）。
FULLBOOK_MAX_RETRIES = 2
# 小节标记：独占一行，`<!-- §:{section_id} -->`。用 HTML 注释确保正文渲染不可见。
_MARKER_RE = re.compile(r"^<!--\s*§:([^\s>]+)\s*-->\s*$", flags=re.M)


def _section_marker(sid: str) -> str:
    return f"<!-- §:{sid} -->"


def build_fullbook_workspace(*, facts: str, conflicts_digest: str, sections: list[dict]) -> str:
    """装配整卷工作区文本（纯函数，便于单测）。

    sections 需含 id/title/content。每章以独占一行的 ``<!-- §:id -->`` 标记起始，
    其后紧跟标题行与正文（正文原样、不截断 —— 整卷修复的纪律与逐章重写一致：
    **截断输入即截断输出**，会让闸门误判丢内容）。
    """
    parts: list[str] = [
        "【全局事实变量（本方案统一口径，必须优先使用、不得矛盾）】",
        facts or "（无）",
        "",
        "【已仲裁的跨章节冲突清单（含权威值与修复指令）】",
        conflicts_digest or "（无）",
        "",
        "【待修订的整卷正文（每章以独占一行的 <!-- §:章节ID --> 标记起始）】",
    ]
    for s in sections:
        sid = str(s.get("id") or "")
        parts.append(_section_marker(sid))
        parts.append(f"# {s.get('title') or ''}")
        parts.append(s.get("content") or "")
        parts.append("")
    return "\n".join(parts)


def parse_fullbook_workspace(text: str, known_sids: list[str]) -> tuple[dict[str, str], list[str]]:
    """按小节标记把 AI 回包切回各章正文（纯函数）。

    Returns:
        ``({section_id: content}, [unknown_section_id, ...])``。
        ``content`` 是标记之后、下一标记之前（或文末）的原文，**去掉紧随标记的标题行**
        后再返回（标题行由程序统一维护，AI 不得改动章节标题文本）。
        未知 id（回包出现但原卷没有）单独收集，供闸门 A 判定，不并入结果字典。
    """
    matches = list(_MARKER_RE.finditer(text or ""))
    result: dict[str, str] = {}
    unknown: list[str] = []
    known = set(known_sids)
    for i, m in enumerate(matches):
        sid = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end]
        # 去掉标记后的第一行标题（"# xxx"），保留其下正文
        lines = block.splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines and lines[0].lstrip().startswith("#"):
            lines = lines[1:]
        content = "\n".join(lines).strip("\n")
        if sid not in known:
            unknown.append(sid)
            continue
        # 同一 id 重复出现：以首次为准，后续重复视为结构异常也计入 unknown 触发拒绝
        if sid in result:
            unknown.append(f"{sid}(duplicate)")
            continue
        result[sid] = content
    return result, unknown


def _digest_conflicts(conflicts: list[dict]) -> str:
    """把仲裁后的冲突压成人类 / 模型可读的清单文本（仅取有权威值的可修项）。"""
    lines: list[str] = []
    for c in conflicts:
        auth = (c.get("authoritative_value") or "").strip()
        if not auth:
            continue
        topic = c.get("topic") or ""
        ctype = c.get("conflict_type") or ""
        instr = c.get("repair_instruction") or ""
        occs = c.get("occurrences") or []
        wrong = sorted(
            {
                str(o.get("value") or "").strip()
                for o in occs
                if str(o.get("value") or "").strip() and str(o.get("value") or "").strip() != auth
            }
        )
        lines.append(
            f"- [{c.get('id', '')}] {ctype} 「{topic}」：权威值 = {auth}"
            + (f"；错误取值 = {'、'.join(wrong)}" if wrong else "")
            + (f"；修复指令：{instr}" if instr else "")
        )
    return "\n".join(lines)


def _section_conflict_values(conflicts: list[dict], sid: str) -> tuple[list[str], str]:
    """取某章相关的错误取值集合与（唯一时）权威值，供 validate_repair 逐章闸门。"""
    wrong: list[str] = []
    auths: set[str] = set()
    for c in conflicts:
        auth = (c.get("authoritative_value") or "").strip()
        if not auth:
            continue
        for occ in c.get("occurrences") or []:
            if str(occ.get("section_id") or "") != sid:
                continue
            v = str(occ.get("value") or "").strip()
            if v and v != auth:
                wrong.append(v)
            auths.add(auth)
    auth_single = next(iter(auths)) if len(auths) == 1 else ""
    return sorted(set(wrong)), auth_single


async def run_fullbook_repair(
    db,
    *,
    scheme_id: str,
    scan_id: str,
    conflicts: list[dict],
    facts: str,
    severity_threshold: str = "high",
    conflict_ids: list[str] | None = None,
    resync_fn=None,
    progress_cb=None,
) -> dict:
    """整卷合并修复一次批次。返回 save_repair 结果（含 repair_id / snapshot_id / items）。

    仅修复**有权威值**且落在阈值 / 白名单内的冲突（复用 filter_conflicts 口径）。

    ✅ R168（2026-11-03 · 原子性收口）：回写阶段（章节 UPDATE + 冲突状态回写）改为
    **全程缓冲、不中途 commit**，统一由 :func:`repair_record.save_repair` 内部的那一次
    commit 落库 —— 全有全无。旧实现每章 UPDATE 后立即 commit，中途异常（锁竞争 /
    连接故障）会留下「章节已改、批次无记录、snapshot_id 不为用户所知、无法回滚」的
    半生效态。前置快照（create_snapshot）自带 commit，先于本阶段独立生效，不受影响；
    调用方经 get_db 池化取得连接，进入本函数前无未提交事务，缓冲安全。
    """
    # 延迟 import 避免模块级成环（repair_agent 亦在函数内消费 routers 注入回调）
    from app.services.repair_agent import filter_conflicts

    targets = filter_conflicts(conflicts, severity_threshold=severity_threshold, conflict_ids=conflict_ids)
    if not targets:
        snap_id = await repair_record.create_snapshot(db, scheme_id, [])
        return await repair_record.save_repair(
            db,
            scheme_id=scheme_id,
            scan_id=scan_id,
            mode="fullbook",
            items=[],
            snapshot_id=snap_id,
            total_conflicts=0,
            stats={"repaired": 0, "failed": 0, "skipped": 0},
        )

    # 只取涉及冲突的末级章节作为整卷范围（保持目录 DFS 序）
    from app.services.consistency_scanner import load_leaf_sections

    all_leaves = await load_leaf_sections(db, scheme_id)
    involved: set[str] = set()
    for c in targets:
        for occ in c.get("occurrences") or []:
            sid = str(occ.get("section_id") or "")
            if sid:
                involved.add(sid)
    sections = [s for s in all_leaves if str(s.get("id") or "") in involved]
    section_by_id = {str(s.get("id") or ""): s for s in sections}
    known_sids = list(section_by_id.keys())

    workspace = build_fullbook_workspace(facts=facts, conflicts_digest=_digest_conflicts(targets), sections=sections)
    if len(workspace) > FULLBOOK_MAX_CHARS:
        logger.warning(
            "[整卷修复] 工作区 %d 字超过上限 %d，放弃整卷模式（交回逐章修）", len(workspace), FULLBOOK_MAX_CHARS
        )
        snap_id = await repair_record.create_snapshot(db, scheme_id, [])
        return await repair_record.save_repair(
            db,
            scheme_id=scheme_id,
            scan_id=scan_id,
            mode="fullbook",
            items=[],
            snapshot_id=snap_id,
            total_conflicts=len({c["id"] for c in targets}),
            stats={"repaired": 0, "failed": 0, "skipped": len({c["id"] for c in targets})},
        )

    # 快照（仅涉及章节的当前正文）
    snapshot_sections = [
        {"section_id": sid, "content_before": section_by_id[sid].get("content") or ""} for sid in known_sids
    ]
    snapshot_id = await repair_record.create_snapshot(db, scheme_id, snapshot_sections)

    system = render("consistency_fullbook_system")
    user = render(
        "consistency_fullbook_user",
        section_count=str(len(sections)),
        workspace=workspace,
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    parsed: dict[str, str] = {}
    gate_reject_reason = ""
    for attempt in range(1, FULLBOOK_MAX_RETRIES + 2):  # 1 生成 + 2 重问
        if progress_cb:
            await progress_cb(min(attempt, 1), 1, f"整卷合并修复：第 {attempt} 轮往返")
        try:
            raw = await chat_with_fallback(messages, temperature=0.1, timeout=180, scene="consistency_fullbook")
        except Exception as e:  # noqa: BLE001 - 调用失败重问一次，最终仍失败则整批放弃
            logger.warning("[整卷修复] 第 %d 轮调用异常：%s", attempt, e)
            gate_reject_reason = f"AI 调用异常：{str(e)[:120]}"
            continue
        parsed, unknown = parse_fullbook_workspace(raw, known_sids)
        # 闸门 A：未知 / 重复小节 id → 整包拒绝
        if unknown:
            gate_reject_reason = f"回包含未知或重复小节标记：{unknown[:5]}"
            parsed = {}
            messages = _append_repair_nudge(messages, gate_reject_reason)
            continue
        # 闸门 B：缺章 → 整包拒绝
        missing = [sid for sid in known_sids if sid not in parsed]
        if missing:
            gate_reject_reason = f"回包缺失 {len(missing)} 个小节标记（不得丢章）"
            parsed = {}
            messages = _append_repair_nudge(messages, gate_reject_reason)
            continue
        gate_reject_reason = ""
        break

    items: list[dict] = []
    repaired_conflicts: set[str] = set()
    failed_conflicts: set[str] = set()

    if not parsed:
        # 整包被拒：本批全部涉及冲突计 failed（不写库，原文经快照保留）
        for c in targets:
            failed_conflicts.add(c["id"])
            items.append(
                {
                    "conflict_id": c["id"],
                    "section_id": "",
                    "topic": c.get("topic", ""),
                    "authoritative_value": c.get("authoritative_value", ""),
                    "status": "failed",
                    "problems": [f"整卷回包未通过闸门：{gate_reject_reason or '解析失败'}"],
                }
            )
    else:
        for sid in known_sids:
            orig = section_by_id[sid].get("content") or ""
            new = parsed.get(sid, "")
            confs = [
                c for c in targets if any(str(o.get("section_id") or "") == sid for o in (c.get("occurrences") or []))
            ]
            cids = [c["id"] for c in confs]
            if new.strip() == orig.strip():
                # 未变化：不算修复也不算失败（该章冲突可能本就在别处或无需动）
                for cid in cids:
                    failed_conflicts.add(cid)
                items.extend(
                    {
                        "conflict_id": cid,
                        "section_id": sid,
                        "section_title": section_by_id[sid].get("title", ""),
                        "authoritative_value": next(
                            (c.get("authoritative_value", "") for c in confs if c["id"] == cid), ""
                        ),
                        "before": orig[:4000],
                        "after": orig[:4000],
                        "status": "failed",
                        "problems": ["整卷回包该章无变化，冲突未落改"],
                    }
                    for cid in cids
                )
                continue
            wrong, auth_single = _section_conflict_values(targets, sid)
            ok, problems = validate_repair(before=orig, after=new, wrong_values=wrong, authoritative_value=auth_single)
            if not ok:
                # 闸门 C/D 拒该章：不回写
                for cid in cids:
                    failed_conflicts.add(cid)
                items.extend(
                    {
                        "conflict_id": cid,
                        "section_id": sid,
                        "section_title": section_by_id[sid].get("title", ""),
                        "authoritative_value": next(
                            (c.get("authoritative_value", "") for c in confs if c["id"] == cid), ""
                        ),
                        "before": orig[:4000],
                        "after": new[:4000],
                        "status": "failed",
                        "problems": problems,
                    }
                    for cid in cids
                )
                continue
            # 通过闸门：回写该章
            _wc = text_word_count(new)
            _wb = section_by_id[sid].get("word_budget") or DEFAULT_WORD_BUDGET
            persist_content = new
            if resync_fn is not None:
                try:
                    _resynced = await resync_fn(db, sid, new)
                    if isinstance(_resynced, str) and _resynced:
                        persist_content = _resynced
                        _wc = text_word_count(persist_content)
                except Exception as _re:  # noqa: BLE001 - 登记失败保留原回包正文
                    logger.warning("[整卷修复] 第 %s 章图表重登记失败（保留回包正文）: %s", sid, _re)
            # ✅ R168 原子性：只缓冲不 commit，与冲突状态回写、save_repair 留痕
            #    同事务一次性提交（中途异常 → 调用方 get_db 归还前回滚，零半生效）。
            await db.execute(
                "UPDATE sections SET content=?, word_count=?, word_status=?, " "updated_at=? WHERE id=?",
                (persist_content, _wc, word_status_for(_wc, _wb), datetime.now().isoformat(), sid),
            )
            for cid in cids:
                repaired_conflicts.add(cid)
            items.extend(
                {
                    "conflict_id": cid,
                    "section_id": sid,
                    "section_title": section_by_id[sid].get("title", ""),
                    "authoritative_value": next(
                        (c.get("authoritative_value", "") for c in confs if c["id"] == cid), ""
                    ),
                    "before": orig[:4000],
                    "after": persist_content[:4000],
                    "status": "repaired",
                    "problems": [],
                }
                for cid in cids
            )

    # 冲突状态回写（✅ R168：同上缓冲，save_repair 内部 commit 一并落库）
    for cid in repaired_conflicts:
        await db.execute("UPDATE consistency_conflicts SET status='repaired' WHERE id=?", (cid,))
    for cid in failed_conflicts:
        await db.execute("UPDATE consistency_conflicts SET status='failed' WHERE id=?", (cid,))

    all_ids = {c["id"] for c in conflicts}
    target_ids = {c["id"] for c in targets}
    stats = {
        "repaired": len(repaired_conflicts & all_ids),
        "failed": len(failed_conflicts & all_ids),
    }
    # 阈值 / 白名单外的可修项 + 无权威值项 → 计入 skipped（本轮不处理）
    stats["skipped"] = max(len(all_ids) - stats["repaired"] - stats["failed"], 0)
    result = await repair_record.save_repair(
        db,
        scheme_id=scheme_id,
        scan_id=scan_id,
        mode="fullbook",
        items=items,
        snapshot_id=snapshot_id,
        total_conflicts=len(all_ids),
        stats=stats,
    )
    logger.info(
        "整卷修复批次 %s 完成：成功 %d 失败 %d 跳过 %d（冲突总数 %d，可修 %d）",
        result["repair_id"],
        result["repaired"],
        result["failed"],
        result["skipped"],
        result["total_conflicts"],
        len(target_ids),
    )
    return result


def _append_repair_nudge(messages: list, reason: str) -> list:
    """闸门 A/B 整包拒绝后，追加一轮「严格按标记结构重出」的定向修复提示。"""
    nudge = (
        "上一次返回不符合结构要求，程序已拒绝：" + reason + "。"
        "请严格保持整卷中每一个 <!-- §:章节ID --> 标记独占一行、顺序与数量与原文完全一致，"
        "不得新增、删除、改名或重复任何小节标记；只在标记之下就地修订冲突取值，"
        "其余内容逐字保留。重新输出完整整卷。"
    )
    return [*messages, {"role": "user", "content": nudge}]
