# ✅ R179 N1（2026-11-05）：目录生成自动确认 · 结构 + 正文全量快照与回退。
#
# 复用既有 `scheme_snapshots` 表（零迁移），type 取方案 §3.5 的
# `outline_auto_confirm`（自动确认前）/ `outline_rollback_undo`（回退前，保证回退可撤销）。
# 快照 payload 是**整表 sections 行**（含 content）的 JSON + 方案 review_status，
# 与 repair_record 的「仅涉及章节正文」快照不同 —— 自动确认需要能整体回退。
#
# 回退纪律（方案 §3.6）：
#   - 恢复结构 + 正文 + review_status（**本服务只做纯 DB 恢复**）；
#   - 强制失效导出缓存与一致性缓存 + 重挂图表 —— 由路由层
#     `/outline-versions/{id}/restore` 编排执行（services 不得依赖 routers）；
#   - **不自动重跑一致性扫描**（只失效缓存，由用户显式触发）。
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from app.services.outline_quality import detect_empty_top_chapters as _detect_empty_top_chapters

logger = logging.getLogger(__name__)

KEEP_SNAPSHOTS = 5


def _now() -> str:
    return datetime.now().isoformat()


async def _section_columns(db) -> list[str]:
    cur = await db.execute("PRAGMA table_info(sections)")
    rows = await cur.fetchall()
    await cur.close()
    return [str(r[1]) for r in rows]


async def create_outline_snapshot(
    db,
    scheme_id: str,
    *,
    snapshot_type: str = "outline_auto_confirm",
    commit: bool = True,
) -> str:
    """把当前目录结构 + 全部正文存一份快照，返回 snapshot_id。

    ``commit=False`` 时**不提交** —— 供 save-outline 与整表重建共用同一事务。
    """
    cur = await db.execute("SELECT * FROM sections WHERE scheme_id=? ORDER BY sort_order, id", (scheme_id,))
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    cur = await db.execute("SELECT review_status, outline_source FROM schemes WHERE id=?", (scheme_id,))
    scheme_row = await cur.fetchone()
    await cur.close()
    payload = {
        "sections": rows,
        "review_status": str((scheme_row["review_status"] if scheme_row else "") or ""),
        # ✅ R223（2026-11-12 · P3 · DAT-1）：目录来源随快照留存 —— 旧 payload 不含
        #    outline_source，非空回退也不校正 ⇒ 回退后 schemes.outline_source 停留在
        #    「回退动作发生时」的来源而非快照目录的真实来源（如：上传识别 → 模板
        #    实例化 → 回退到实例化前快照，来源仍显示 template_instance，溯源失真）。
        #    restore 侧仅当快照携带该键时还原（旧快照无键 → 维持现状，向后兼容）。
        "outline_source": str((scheme_row["outline_source"] if scheme_row else "") or ""),
    }
    snapshot_id = f"ver_{uuid.uuid4().hex[:12]}"
    await db.execute(
        "INSERT INTO scheme_snapshots (id, scheme_id, type, sections, created_at) VALUES (?,?,?,?,?)",
        (snapshot_id, scheme_id, snapshot_type, json.dumps(payload, ensure_ascii=False, default=str), _now()),
    )
    # ✅ R190（注释诚实化）：保留最近 KEEP_SNAPSHOTS 条 —— 配额按 scheme_id
    #    **全类型共享**（不含 type 维度）。⚠️ 后果：一次自动确认 + 两三次回退
    #    即可把「回退前（可撤销）」快照挤出窗口，撤销链路缩短。这是**当前实现
    #    的真实语义**（旧注释宣称「同类型族」与 SQL 不符，已修正），如需按
    #    type 分配配额属行为变更，须单独评审。
    await db.execute(
        "DELETE FROM scheme_snapshots WHERE scheme_id=? AND id NOT IN ("
        " SELECT id FROM scheme_snapshots WHERE scheme_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?)",
        (scheme_id, scheme_id, KEEP_SNAPSHOTS),
    )
    if commit:
        await db.commit()
    return snapshot_id


async def list_snapshots(db, scheme_id: str, *, limit: int = KEEP_SNAPSHOTS) -> list[dict[str, Any]]:
    """列出该方案最近快照（不返回正文，避免响应过大）。

    每条返回 ``snapshot_id / type / created_at / node_count / has_content``
    （方案 §3.5.2 的版本列表字段；正文只在回退时从 JSON 读取）。
    """
    cur = await db.execute(
        "SELECT id, type, created_at, sections FROM scheme_snapshots "
        "WHERE scheme_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (scheme_id, int(limit)),
    )
    rows = await cur.fetchall()
    await cur.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        node_count = 0
        has_content = False
        try:
            payload = json.loads(r["sections"] or "{}")
            sections = payload.get("sections") if isinstance(payload, dict) else None
            if isinstance(sections, list):
                node_count = len(sections)
                has_content = any(str(s.get("content") or "").strip() for s in sections if isinstance(s, dict))
        except (TypeError, ValueError):
            node_count = 0
        out.append(
            {
                "snapshot_id": r["id"],
                "type": r["type"],
                "created_at": r["created_at"],
                "node_count": node_count,
                "has_content": has_content,
            }
        )
    return out


async def restore_outline_snapshot(db, scheme_id: str, snapshot_id: str) -> dict[str, Any]:
    """回退到指定快照：只做纯 DB 恢复（结构/正文/review_status）。

    返回 ``{ok, restored_sections, empty_top_chapters}``；缓存失效与图表重挂
    由路由层编排（services 不得依赖 routers）。快照不存在/不属于该方案 → ValueError。

    ✅ R190（FIX-2）：首个决策读之前 BEGIN IMMEDIATE 取 RESERVED 写锁
    （与 R184 _save_outline_to_db 同口径）。回退是「读快照 → DELETE 全表 →
    逐行 INSERT」复合写，两路并发回退到不同快照时旧实现各自基于自己读到的
    旧状态组装，最后 commit 者胜，先者的 undo 快照链与最终状态可能对不上。
    """
    _owns_lock = not getattr(db, "in_transaction", False)
    if _owns_lock:
        await db.execute("BEGIN IMMEDIATE")
    try:
        return await _restore_locked(db, scheme_id, snapshot_id)
    except Exception:
        if _owns_lock:
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001 — 回滚失败不再遮蔽原异常
                pass
        raise


async def _restore_locked(db, scheme_id: str, snapshot_id: str) -> dict[str, Any]:
    """restore_outline_snapshot 的加锁主体（事务语义见上层函数注释）。"""
    cur = await db.execute(
        "SELECT scheme_id, sections FROM scheme_snapshots WHERE id=? AND scheme_id=?",
        (snapshot_id, scheme_id),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        raise ValueError("快照不存在或不属于该方案")

    try:
        payload = json.loads(row["sections"] or "{}")
    except (TypeError, ValueError) as e:
        raise ValueError(f"快照内容损坏: {e}") from e
    sections = payload.get("sections") if isinstance(payload, dict) else None
    if not isinstance(sections, list):
        raise ValueError("快照缺少 sections 数据")

    # ✅ R179（2026-11-05）：回退同样是「整表重建写库」，必须先过空壳一级章闸门
    #    （与 save-outline / upload_outline 共用 services 层的唯一实现）。
    #    语义差异：回退是把用户此前确认过的状态还原回来，**只回传清单不阻断**
    #    —— 阻断会让合法快照无法撤销（撤销功能被锁死），与本轮 N1 目标相反。
    _parents = {str(s.get("parent_id") or "") for s in sections if isinstance(s, dict)}
    _tops = [
        {
            "title": str(s.get("title") or ""),
            "children": [1] if str(s.get("id") or "") in _parents else [],
        }
        for s in sections
        if isinstance(s, dict) and str(s.get("id") or "") and not str(s.get("parent_id") or "")
    ]
    empty_top_chapters = _detect_empty_top_chapters(_tops)

    cols = await _section_columns(db)
    # 恢复前再存一份「回退前」快照，保证回退本身可撤销
    await create_outline_snapshot(db, scheme_id, snapshot_type="outline_rollback_undo", commit=False)

    await db.execute("DELETE FROM sections WHERE scheme_id=?", (scheme_id,))
    # ✅ R209（2026-11-10 · 孤儿图表登记）：回退是整表重建 —— 快照里没有的章节
    #    其 chart_predictions 行会变成孤儿（section_id 指向已不存在的行）。
    #    同族三条路径都显式清理（sections.py 删除单章 / save-outline 整表重建 /
    #    upload_outline 整表重建），唯独本路径漏接 ⇒ 图表面板出现幽灵条目、
    #    导出指纹被污染（无谓缓存失效）。
    #    按 scheme_id 整清（与 sections.py 清空目录分支同口径）。
    #    ⚠️ 表缺失（老库 / 精简测试库）时跳过而非报错（与 db._migrate 同纪律）。
    try:
        await db.execute("DELETE FROM chart_predictions WHERE scheme_id=?", (scheme_id,))
    except Exception as e:  # noqa: BLE001 — 表缺失/锁冲突不得阻断回退
        logger.warning("目录快照回退：清理图表登记失败（不影响回退结果）: %s", e)
    restored = 0
    for item in sections:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        item["scheme_id"] = scheme_id
        use_cols = [c for c in cols if c in item]
        values = [item.get(c) for c in use_cols]
        if use_cols:
            await db.execute(
                f"INSERT INTO sections ({', '.join(use_cols)}) VALUES ({', '.join(['?'] * len(use_cols))})",
                values,
            )
            restored += 1

    review_status = str(payload.get("review_status") or "")
    # ✅ R190（FIX-11）：快照恢复是整表重建，schemes.word_count 必须按恢复后的
    #    章节正文重算 —— 旧实现只更新 review_status，回退到「正文为空」快照后
    #    方案列表/头部仍显示回退前的总字数，直到下一次正文写路径才自愈。
    # ✅ R216（2026-11-11 · P2）：快照回退**不回写 schemes.status / outline_source**
    #    的口径分叉 —— 回退到空快照（undo 空快照 / 旧空目录）后 sections 全删，
    #    status 仍停留「目录已确认」；反向「目录待生成」方案回退出非空目录后
    #    停留「目录待生成」。该值被前端头部 Tag 与 review 端点 scheme_status
    #    消费（save-outline 清空分支注释明言此语义，其非空分支置「目录已确认」、
    #    清空分支回退「目录待生成」并清空来源）。现按 restore 结果同步：
    #    0 章 → 「目录待生成」+ 清空来源（与清空分支同不变式；来源所指向的
    #    目录已不存在，保留即溯源失真）；>0 章 → 仅从「目录待生成」升级为
    #    「目录已确认」（CASE 条件更新，不降级任何更高状态）。
    #    ⚠️ services 不得 import routers：字面量与 sections.OUTLINE_EMPTY_STATUS /
    #    OUTLINE_SAVED_STATUS 的一致性由护栏测试锁定（test_outline_r216）。
    if restored == 0:
        await db.execute(
            "UPDATE schemes SET review_status=?,"
            " word_count=(SELECT COALESCE(SUM(word_count),0) FROM sections WHERE scheme_id=?),"
            " status='目录待生成', outline_source='', updated_at=? WHERE id=?",
            (review_status, scheme_id, _now(), scheme_id),
        )
    else:
        # ✅ R223（DAT-1）：非空回退把 outline_source 还原为快照留存值 —— 仅当快照
        #    携带该键（旧快照无键 → 不触碰来源，向后兼容）；空串快照来源也如实
        #    还原（回退到「来源未知」的历史目录时清空本地来源即真值）。
        _snap_source = str(payload.get("outline_source") or "")
        if _snap_source or "outline_source" in payload:
            await db.execute(
                "UPDATE schemes SET review_status=?,"
                " word_count=(SELECT COALESCE(SUM(word_count),0) FROM sections WHERE scheme_id=?),"
                " status=CASE WHEN status='目录待生成' THEN '目录已确认' ELSE status END,"
                " outline_source=?, updated_at=? WHERE id=?",
                (review_status, scheme_id, _snap_source, _now(), scheme_id),
            )
        else:
            await db.execute(
                "UPDATE schemes SET review_status=?,"
                " word_count=(SELECT COALESCE(SUM(word_count),0) FROM sections WHERE scheme_id=?),"
                " status=CASE WHEN status='目录待生成' THEN '目录已确认' ELSE status END,"
                " updated_at=? WHERE id=?",
                (review_status, scheme_id, _now(), scheme_id),
            )
    await db.commit()
    # 缓存失效与图表重挂由路由层统一编排（services 不得依赖 routers）。
    return {
        "ok": True,
        "restored_sections": restored,
        "empty_top_chapters": empty_top_chapters,
    }
