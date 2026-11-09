# ✅ R179 N1（2026-11-05）：Agent 化目录生成 —— 自动确认（硬前置判定）。
#
# 契约（方案 §3，冻结）：
#   - 纯函数：不读配置、不写库、不调 AI —— 同一输入必得同一输出，可单测。
#   - 硬前置**只有四类**：占位标题、编号连续性、空章、失败章节。
#   - M1 的 `undetermined`（评分项解析不出）**不阻塞**自动确认（缺证据 ≠ 目录不合格）。
#   - **已存在正文的项目永不自动确认**（单独前置，防止覆盖人工成果）。
#   - 默认关闭由前端/治理开关控制；本模块只回答「ready/blockers」。
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _count(review: dict[str, Any], key: str) -> int:
    """读取缺陷计数：顶层列表 / 顶层 issue_counts / quality.continuity.issue_counts。

    ✅ R187（2026-11-08 · 跨模块字段层级错位）：``_attach_quality_report`` 把
    ``check_outline_continuity`` 的 ``issue_counts`` 放在
    ``review["quality"]["continuity"]["issue_counts"]``（见
    ``_sse_outline_review.py`` 的 report 构造），而旧实现只读顶层 —— 于是
    ``numbering_mismatch`` / ``level_gaps`` 恒为 0，「编号/层级不连续」硬前置
    形同虚设（generic_titles 有 detect 兜底、empty_parents 有 outline 兜底，
    唯独编号没有）。顶层优先保持向后兼容，嵌套仅作回退。
    """
    value = review.get(key)
    if isinstance(value, list):
        return len(value)
    counts = review.get("issue_counts") or {}
    if not counts:
        quality = review.get("quality")
        continuity = quality.get("continuity") if isinstance(quality, dict) else None
        counts = continuity.get("issue_counts") if isinstance(continuity, dict) else None
    if not isinstance(counts, dict):
        return 0
    try:
        return int(counts.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _empty_parent_count(outline: list[Any]) -> int:
    """空章判据（与方案 §3.3 一致）。

    - **一级节点无任何子节点** → 空章（顶层就是全部目录时也按空章处理）；
    - 有 children 但子项标题全空 → 空壳父章；
    - 更深层「有父无子」由 ``review.empty_parents`` 计入。
    """
    count = 0
    for node in outline or []:
        if not isinstance(node, dict):
            continue
        children = node.get("children")
        if not isinstance(children, list) or not children:
            count += 1
            continue
        if all(not str(c.get("title") or "").strip() if isinstance(c, dict) else True for c in children):
            count += 1
    return count


def evaluate(
    outline: list[Any] | None,
    review: dict[str, Any] | None = None,
    failed_chapters: list[Any] | None = None,
    has_content: bool = False,
) -> dict[str, Any]:
    """自动确认判定：返回 ``{ready, blockers, counts}``（纯函数）。

    - ``outline``：目录树（``[{title, level, children}]``）。
    - ``review``：``outline_quality.check_outline_continuity`` 的报告（可为空）。
    - ``failed_chapters``：目录生成失败章节（长度即失败数）。
    - ``has_content``：项目是否**已经存在正文**（True 时永不自动确认）。
    """
    review = review if isinstance(review, dict) else {}
    outline = outline if isinstance(outline, list) else []
    blockers: list[dict[str, Any]] = []

    def block(code: str, message: str, count: int = 1) -> None:
        blockers.append({"code": code, "message": message, "count": int(count)})

    generic = _count(review, "generic_titles")
    if outline and not generic:
        try:
            from app.services.outline_quality import detect_generic_titles

            generic = len(detect_generic_titles(outline))
        except Exception:
            generic = 0

    if not outline:
        block("no_outline", "目录为空，无任何章节")
    else:
        if generic > 0:
            block("placeholder_titles", f"存在 {generic} 个占位/泛化标题", generic)

        numbering = _count(review, "numbering_mismatch") + _count(review, "level_gaps")
        if numbering > 0:
            block("numbering_invalid", f"编号/层级不连续 {numbering} 处", numbering)

        # ✅ R192（FIX-R5 · 重复标题硬前置）：重复标题此前只有程序化预检早退闸门
        #    （_sse_outline_review 预检出口）一处消费——AI 审核跑完后只重检泛化标题、
        #    从不重检重复标题；AI 审核漏报或修复轮引入新同名章节时，弱审核 passed=true
        #    即可让同名章节落库并被 8 秒倒计时自动确认。现复用同一 ``_count`` 判据
        #    （顶层 issue_counts / quality.continuity.issue_counts，R187 FIX-4 口径）
        #    读取计数进 blockers——计数由审核出口重检（FIX-R5 另半）与质量报告供给，
        #    本函数不新增第二套检出实现（单一实现纪律）。
        duplicate = _count(review, "duplicate_titles")
        if duplicate > 0:
            block("duplicate_titles", f"存在 {duplicate} 个重复标题（同名章节）", duplicate)

        empty = _count(review, "empty_parents") or _empty_parent_count(outline)
        if empty > 0:
            block("empty_chapters", f"存在 {empty} 个空壳父章节", empty)

    failed = len(failed_chapters or [])
    if failed > 0:
        block("failed_sections", f"{failed} 个章节生成失败", failed)

    # ✅ R187（2026-11-08）：审核超时/失败/结构异常时 ``_sse_outline_review``
    #    加法式标记 review_skipped，意味着目录**未经 AI 审核**（``passed=True``
    #    只是前端兼容值）。不得让 8 秒倒计时把未审目录自动落库。
    #    旧事件无该键时 ready 判定逐字不变。
    if review.get("review_skipped"):
        block("review_skipped", "目录未经 AI 审核（审核超时/失败），禁止自动确认", 1)

    # ⚠️ 硬红线：项目已有正文 → 永不自动确认（避免自动确认/回退覆盖人工成果）
    if has_content:
        block("existing_content", "项目已存在正文，禁止自动确认", 1)

    return {
        "ready": not blockers,
        "blockers": blockers,
        "counts": {
            "generic_titles": generic,
            "numbering": _count(review, "numbering_mismatch") + _count(review, "level_gaps"),
            "duplicate_titles": _count(review, "duplicate_titles"),
            "empty_parents": _count(review, "empty_parents"),
            "failed_chapters": failed,
            "has_content": 1 if has_content else 0,
        },
    }


async def scheme_has_content(db, scheme_id: str) -> bool:
    """方案下是否已有任何已生成正文（content 非空或字数 > 0）。"""
    if not scheme_id:
        return False
    try:
        cur = await db.execute(
            "SELECT COUNT(*) FROM sections WHERE scheme_id=? "
            "AND (TRIM(COALESCE(content,'')) <> '' OR COALESCE(word_count,0) > 0)",
            (scheme_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return bool(row and int(row[0] or 0) > 0)
    except Exception as e:  # 查询失败按「有正文」保守处理，宁可不自动确认
        logger.warning("N1 自动确认：查询既有正文失败，按保守口径禁止自动确认: %s", e)
        return True
