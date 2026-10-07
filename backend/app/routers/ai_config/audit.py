"""AI 配置路由 · 配置变更审计（✅ 2026-09-23 新增）。

背景（补齐「缺少安全与审计」缺口）：
  ``ai_audit_logs`` 只记录 **AI 调用**，而配置本身的新增 / 修改 / 删除 /
  切换「当前使用」/ 调整降级链 / 导入 / 清除密钥**完全没有留痕** ——
  出问题时无法回答「谁在什么时候把主配置换成了哪条」「Key 什么时候被清的」。

硬性约定：
  1. ``detail`` 只允许写**非敏感摘要**（如 ``key_hint`` 后 4 位），
     **严禁写入明文 API Key 或密文**（脱敏要求）；
  2. 审计写入是**尽力而为**：旧库尚未触发 ``_migrate`` 建表时，表不存在，
     此时只记 debug 日志，绝不让配置保存/切换等主流程失败。
"""
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import settings  # noqa: F401  模块公共面：测试经 audit.settings monkeypatch trusted_proxy_ips
from app.db import get_db, read_db
from app.models import ConfigRollbackIn
from app.services.ai.provider_factory import (
    apply_config_concurrency,
    clamp_config_numbers,
    invalidate_config_cache,
    normalize_env,
    normalize_plan,
    normalize_request_mode,
)

logger = logging.getLogger("ai_config")

router = APIRouter(tags=["ai_config"])


# Audit implementation moved to app.services.audit_service.
# Keep re-exports for backward compatibility.
from app.services.audit_service import (
    CONFIG_ACTIONS,
    client_ip_of,  # noqa: F401  兼容再导出：测试与路由经 audit.client_ip_of 调用
    diff_snapshots,
    record_config_audit,
    sanitize_config_snapshot,
)


@router.get("/config/audit-logs")
async def config_audit_logs(limit: int = 50, offset: int = 0, days: int = 30,
                            action: str = "", config_id: str = "",
                            db=Depends(read_db)):
    """配置变更审计列表（最新在前）。

    参数与 ``/ai/audit-logs`` 保持同口径：limit 钳到 1..200、days 钳到 1..3650。

    ✅ 2026-10-06（G2）：新增可选 ``config_id`` 筛选，并回传 ``config_ids``
      （该时间窗内出现过的配置清单）。此前只有 ``action`` 一个维度，
      「这条配置一共被改过几次 / 最近一次是谁改的」只能靠翻页肉眼翻，
      且无法回答「主配置最近一次变更是哪个动作」。
      不传 ``config_id`` 时行为与引入前逐字一致（加法式变更）。
    """
    limit = max(1, min(200, int(limit or 50)))
    offset = max(0, int(offset or 0))
    days = max(1, min(3650, int(days or 30)))

    where = ["created_at >= datetime('now','localtime', ?)"]
    params: list = [f"-{days} days"]
    if action:
        where.append("action = ?")
        params.append(action)
    if config_id:
        where.append("config_id = ?")
        params.append(config_id)
    clause = " AND ".join(where)

    cur = await db.execute(
        f"SELECT COUNT(*) FROM ai_config_audit_logs WHERE {clause}", tuple(params))
    total = int((await cur.fetchone())[0])

    cur = await db.execute(
        f"SELECT * FROM ai_config_audit_logs WHERE {clause}"
        " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset))
    items = [dict(r) for r in await cur.fetchall()]
    for it in items:
        it["action_label"] = CONFIG_ACTIONS.get(it.get("action", ""), it.get("action", ""))
        # ✅ 2026-09-23：结构化变更 diff（历史行无快照 → 空列表，前端按摘要展示）
        snap = {}
        raw = it.get("snapshot_json") or ""
        if raw:
            try:
                snap = json.loads(raw)
            except Exception:
                snap = {}
        it["changes"] = diff_snapshots(snap)
        # 是否有「变更前」快照 → 决定能否回滚（前端据此决定是否显示回滚按钮，
        # 避免点了必然 400；历史行无快照时为 False）
        it["rollbackable"] = bool(isinstance(snap, dict) and snap.get("before"))
        # 快照仅用于服务端生成 diff / 回滚，原始 JSON 不必回传前端（减小载荷）
        it.pop("snapshot_json", None)
    # ✅ 2026-10-06（G2）：该时间窗内出现过的配置清单（供前端下拉筛选 + 变更次数）。
    #    标签取「供应商 / 模型」，两者都缺失（老行 / 导入汇总行）时回落到 id 前 8 位。
    #    ⚠️ 配置可能已被删除，故不从 ai_config 联表取名，只用审计行里留存的字段。
    config_ids: list[dict] = []
    try:
        cur = await db.execute(
            f"SELECT config_id, COUNT(*) AS n, "
            f"GROUP_CONCAT(DISTINCT provider_name || '/' || model) AS labels "
            f"FROM ai_config_audit_logs WHERE {clause} GROUP BY config_id "
            "ORDER BY MAX(created_at) DESC", tuple(params))
        for r in await cur.fetchall():
            cid = str(r["config_id"] or "")
            label = str(r["labels"] or "").split(",")[0].strip()
            if not label or label == "/":
                label = cid[:8] if cid else "（汇总记录）"
            config_ids.append({"value": cid, "label": label,
                               "count": int(r["n"]) or 0})
    except Exception:
        # 审计查询的附加能力失败不应影响主列表（与表缺失时只记 debug 的约定一致）
        config_ids = []

    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "actions": [{"value": k, "label": v} for k, v in CONFIG_ACTIONS.items()],
        "config_ids": config_ids,
        "config_id": config_id,
    }


# ---------------------------------------------------------------------------
# ✅ 2026-09-23 新增：配置版本回滚（G6）
# ---------------------------------------------------------------------------
#: 可回滚的字段 = 快照白名单去掉只读派生项
#:
#: ⚠️ ``is_active`` 与 ``priority`` 刻意**不在**本表（两者都在 SNAPSHOT_FIELDS 里）：
#:   1. ``is_active`` 由下方 ``include_active`` 分支单独处理，带主配置唯一性守卫；
#:   2. ``priority`` 是**表级排序属性**而非单条配置的独立取值 —— 降级链顺序由
#:      ``PUT /fallback-chain`` 整体维护，``_resequence_priority`` 在增删配置后会把
#:      全表重排为 0..n-1。回滚时把某条配置的 priority 写回一个**过期序号**，会在
#:      降级链里留下空洞（0,2,3,5）或重复（两条同为 2），候选链顺序即失真，而用户
#:      无从察觉（``/fallback-chain`` 只按相对顺序取前 N 条）。宁可快照只记录、
#:      回滚不触碰 —— 与 models.ConfigSave.priority 的「None = 本次不改动降级顺序」
#:      同口径。
ROLLBACK_FIELDS: tuple[str, ...] = (
    "provider_name", "plan", "base_url", "model", "max_tokens", "temperature",
    "timeout", "concurrency", "request_mode", "env", "remark",
)


@router.post("/config/{config_id}/rollback")
async def rollback_config(config_id: str, body: ConfigRollbackIn | None = None,
                          request: Request = None, db=Depends(get_db)):
    """把配置回滚到某条审计记录**变更前**的状态（✅ G6）。

    安全与边界约定：
      1. **密钥不参与回滚** —— 快照里根本没有密钥字段，回滚不会让旧密文复活，
         也不会误删当前已填的 Key（避免「回滚把 Key 清了导致全线失败」）；
      2. 需要目标审计行带 ``before`` 快照（即一次 update/toggle/clear_key 之前的状态）；
         `create` 之前不存在配置 → 400 明确提示改用「删除配置」；
      3. 回滚本身也写一条 ``action="rollback"`` 审计（带前后快照，可再次回滚回来）；
      4. 配置当前不存在（已删除）→ 404（回滚不等于「复现已删除的配置」，防止误操作）；
      5. **「当前使用」标记默认不还原**（``include_active=False``）：主配置唯一性有系统级
         守卫（不允许零主配置），自动改动它是危险操作。显式传 ``include_active=true``
         才会一并还原，且仍带守卫 —— 还原为「关闭」但库里没有其它主配置时保留现状并回 ``warning``。
    """
    audit_id = str(getattr(body, "audit_id", "") or "").strip()
    if not audit_id:
        raise HTTPException(400, "缺少 audit_id（要回滚到哪条变更记录之前）")

    cur = await db.execute(
        "SELECT * FROM ai_config_audit_logs WHERE id = ?", (audit_id,))
    log = await cur.fetchone()
    if not log:
        raise HTTPException(404, "变更记录不存在")
    log = dict(log)
    if (log.get("config_id") or "") != config_id:
        raise HTTPException(400, "该变更记录不属于此配置")

    raw = log.get("snapshot_json") or ""
    snap = {}
    if raw:
        try:
            snap = json.loads(raw)
        except Exception:
            snap = {}
    before = snap.get("before") if isinstance(snap, dict) else None
    if not isinstance(before, dict) or not before:
        raise HTTPException(
            400, "该变更记录没有『变更前』快照，无法回滚"
                 "（新增配置的记录请改用删除；历史记录可能早于快照功能上线）")

    cur = await db.execute("SELECT * FROM ai_config WHERE id = ?", (config_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "配置不存在（已删除的配置无法回滚，请重新新增）")
    current = dict(row)

    # 只回滚快照中**确实存在**的字段；数值与请求方式仍走既有白名单归一，
    # 防止历史快照里的越界值/脏值被原样写回。
    # ✅ 2026-09-23：``include_active=True`` 时连「当前使用」标记一起还原（默认不还原，
    #    因为主配置唯一性有系统级守卫，见下方 want_active 分支）。
    include_active = bool(getattr(body, "include_active", False))
    nums = clamp_config_numbers({**current, **{k: v for k, v in before.items()
                                               if k in ROLLBACK_FIELDS}})
    assign: dict = {}
    for f in ROLLBACK_FIELDS:
        if f not in before:
            continue
        val = before.get(f)
        if f in ("max_tokens", "temperature", "timeout", "concurrency"):
            val = nums[f]
        elif f == "request_mode":
            val = normalize_request_mode(val)
        elif f == "env":
            try:
                val = normalize_env(val)
            except ValueError:
                raise HTTPException(400, f"快照中的环境名非法：{val!r}")
        elif f == "plan":
            val = normalize_plan(val)
        assign[f] = val

    warning = ""
    active_restored: bool | None = None
    if include_active and "is_active" in before:
        want = 1 if before.get("is_active") else 0
        if want == 1 and not current.get("is_active"):
            # 还原为「当前使用」：先把其他配置的主标记清掉，保证全局唯一
            await db.execute("UPDATE ai_config SET is_active=0")
            assign["is_active"] = 1
            active_restored = True
        elif want == 0 and current.get("is_active"):
            cur2 = await db.execute(
                "SELECT COUNT(1) FROM ai_config WHERE is_active=1 AND id != ?",
                (config_id,))
            others = int((await cur2.fetchone())[0])
            if others > 0:
                assign["is_active"] = 0
                active_restored = False
            else:
                warning = ("快照中该配置当时不是「当前使用」，但回滚它会变成零个主配置，"
                           "已保留其「当前使用」状态。请先启用其它配置后再回滚。")

    if not assign:
        raise HTTPException(400, "该变更记录不含可回滚字段")
    sets = [f"{k}=?" for k in assign] + ["updated_at=?"]
    params = list(assign.values()) + [datetime.now().isoformat(), config_id]
    await db.execute(
        f"UPDATE ai_config SET {', '.join(sets)} WHERE id=?", tuple(params))

    cur = await db.execute("SELECT * FROM ai_config WHERE id = ?", (config_id,))
    after_row = await cur.fetchone()
    after = dict(after_row) if after_row else {}

    await record_config_audit(
        db, "rollback", config_id=config_id,
        provider_name=str(after.get("provider_name") or ""),
        model=str(after.get("model") or ""),
        detail=(f"回滚到变更记录 {audit_id[:8]} 之前的状态"
                + ("（含「当前使用」标记）" if active_restored is not None else "")),
        request=request,
        snapshot={"before": sanitize_config_snapshot(current),
                  "after": sanitize_config_snapshot(after)},
        commit=False)
    await db.commit()
    invalidate_config_cache()
    if after.get("is_active"):
        await apply_config_concurrency()
    restored = sanitize_config_snapshot(after) or {}
    fields = list(ROLLBACK_FIELDS)
    if include_active:
        fields.append("is_active")
    return {
        "ok": True,
        "config_id": config_id,
        "restored": {k: restored.get(k) for k in fields if k in before},
        "active_restored": active_restored,
        "warning": warning,
        "changes": diff_snapshots(
            {"before": sanitize_config_snapshot(current), "after": restored}),
    }
