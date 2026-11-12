"""提示词管理路由（从 ``main.py`` 拆出）。

端点：
  * ``GET    /api/v1/prompts``                 列表（DB 生效内容 + 审计元数据）
  * ``GET    /api/v1/prompts/audit-logs``      变更审计（指纹 + 变量集合 + diff + 是否可回滚）
  * ``PATCH  /api/v1/prompts/{key}``           保存内容（空内容 = 恢复默认）
  * ``POST   /api/v1/prompts/{key}/reset``     恢复出厂默认
  * ``POST   /api/v1/prompts/{key}/rollback``  回滚到某条审计记录「变更前」的版本

设计要点：
  - 内容更新与审计写入**同一事务**（``BEGIN IMMEDIATE`` + 事务内重读 before
    + ``updated_at`` CAS），提交成功后才更新注册表内存并失效运行时缓存；
    并发写不丢版本，冲突时返回 409 而非静默覆盖（BUG-P1-B）。
  - 保存前做**静态体检**（``validate_prompt_content``）：error 级
    （不存在的 ``{SHARED_*}``、共享片段自引用）直接 400；warning 级
    （契约变量增删）照常保存但随响应 ``warnings`` 回传 —— 保存成功 ≠ 一定正确。
  - 入库、列表展示、运行时缓存加载三处统一经 ``clean_prompt_text``，
    使「展示 = 渲染 = hash」三者恒等（BUG-P1-D）。
  - 审计保存 SHA-256 与变量集合，并按配置保存变更前后正文快照（版本回滚基础）；
    ``rollbackable`` 与回滚端点的前置校验**共用同一函数**
    （``audit_service.prompt_snapshot_is_rollbackable``），保证「按钮可点 ⟺ 回滚必成功」。
  - 列表直接查 DB，重启后不会因内存注册表回退而显示成“未修改”。
  - ``db.execute()`` 返回 ``None`` 时按连接异常处理（AGENTS.md §5.5 R13）。
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from app.db import get_db, read_db
from app.services.admin_auth import require_admin_access
from app.services.ai.prompts._metrics import (
    get_snapshot as _get_metrics_snapshot,
)
from app.services.ai.prompts._metrics import (
    reset as _reset_metrics_counters,
)
from app.services.ai.prompts._registry import (
    clean_prompt_text,
    extract_user_variables,
    validate_prompt_content,
)
from app.services.ai.usage_logging import get_usage_snapshot as _get_usage_snapshot
from app.services.audit_service import (
    PROMPT_MAX_CHARS,
    list_prompt_audit_logs,
    prompt_content_hash,
    record_prompt_audit,
)

# ✅ 2026-11-07（错误回显脱敏）：异常原文一律经同一收敛函数再回显/落日志，
#    防止上游错误体中的凭据（Authorization: Bearer sk-…）与内部路径外泄。
from app.services.prompt_governance import sanitize_error_text as _safe_error

router = APIRouter(prefix="/api/v1/prompts", tags=["prompts"])

#: 提示词内容上限（防止单条超大内容进入数据库与审计链路）。
#: ✅ 2026-09-27（BUG-P1-F）：直接 re-export `audit_service.PROMPT_MAX_CHARS`
#:   （同一常量），消除「写入上限」与「回滚/快照长度校验」两份字面量漂移。
__all__ = ["router", "PROMPT_MAX_CHARS"]

logger = logging.getLogger(__name__)


def _now_ts() -> str:
    """当前本地时间戳（**毫秒**精度，格式 ``YYYY-MM-DD HH:MM:SS.mmm``）。

    ✅ R204（2026-11-09）：``prompt_templates.updated_at`` 是提示词 CAS
    （乐观并发控制）的判据 —— 秒级精度下「同一秒内的两次并发写」无法区分，
    后写会**静默覆盖**前写（模块自述「冲突返回 409」永不触发）。故写侧统一
    用毫秒精度。

    ⚠️ 为什么不用 SQL 侧 ``strftime('%Y-%m-%d %H:%M:%f','localtime')``：
    SQLite **不支持** ``%f``（实测 SQLite 3.50.4 返回 NULL → updated_at 被写成
    NULL，列表页「更新时间」变空）。故由应用层生成，格式与既有秒级
    ``datetime('now','localtime')`` 兼容（旧行照常可比）。
    """
    now = time.time()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + f".{int((now % 1) * 1000):03d}"

#: ✅ R48（2026-10-06 · prompts 运行时指标 + 硬编码→DB 一键同步）：
#:   两个**独立前缀**的新路由，刻意不挂在 ``/api/v1/prompts`` 下 ——
#:   指标与 admin 同步是运维/管理面操作，路径按任务要求落在 ``/system`` 与
#:   ``/admin/prompts``。在 ``main.py`` 的 include 循环之后单独注册。
system_metrics_router = APIRouter(prefix="/system", tags=["prompts-metrics"])
# ✅ R179（2026-11-05 · 管理面鉴权）：``/admin/prompts/*`` 在本地默认配置下
#    （ADMIN_API_TOKEN 为空）只允许回环来源，非回环 403；配置 token 后需携带
#    X-Admin-Token / Authorization: Bearer。依赖挂在 router 级，一次覆盖该组
#    全部端点（新增端点自动继承，无需逐个添加）。
admin_prompts_router = APIRouter(
    prefix="/admin/prompts",
    tags=["prompts-admin"],
    dependencies=[Depends(require_admin_access)],
)


async def _fetch_content_row(db, key: str):
    """读一行 prompt_templates；``db.execute`` 返回 None 时按连接异常处理。

    ✅ 2026-09-27（BUG-P1-A · R13 事故漏改点）：AGENTS.md §5.5 记载
    「全局单连接上 execute() 可能返回 None」是 2026-09-22 的真实事故，
    ``routers/_chart_pipeline.py`` 已按规范加守卫，而本文件 4 处全部裸解引用。
    命中即 AttributeError → 500，且 PATCH 路径下内存注册表不更新、
    运行时缓存不失效（内容存了却不生效）。
    """
    cur = await db.execute("SELECT content, updated_at FROM prompt_templates WHERE key=?", (key,))
    if cur is None:
        raise HTTPException(503, "数据库连接异常，请重试")
    return await cur.fetchone()


async def _write_with_before(
    db,
    key: str,
    before: str,
    after: str,
    action: str,
    request,
    *,
    expected_updated_at,
    effective_after: str | None = None,
):
    """在**单个写事务**内做「CAS 改内容 + 写审计」，消除 lost update。

    ✅ 2026-09-27（BUG-P1-B · 并发丢版本 / 审计链断裂）：
    旧实现「先 SELECT 读 before，再 UPSERT 写」—— pysqlite 默认
    ``isolation_level=""`` 只在 DML 前隐式 BEGIN，**SELECT 走 autocommit**，
    两次读之间存在 TOCTOU 窗口。两个并发 PATCH 同 key 时：
    两者都读到 before=X，各自写入 A / B，审计留下 (X→A) 与 (X→B)；
    而回滚语义是恢复 before，于是**两条都只能回到 X，版本 A 永久丢失**，
    审计序列与实际生效历史不符 —— 违背本模块自己声明的快照设计。

    修法：用 ``BEGIN IMMEDIATE`` 抢占写锁后**在事务内重读** before，
    并按 ``updated_at`` 做 CAS 比对；若期间被别人改过则回滚并返回 409，
    让用户看到「已被他人修改，请刷新」，而不是静默覆盖别人的版本。

    :param after: 落库正文；**空串 = 删除该行**（= 恢复出厂默认）。
    :param effective_after: 变更后**实际生效**的正文（删除行时为出厂默认），
        用于计算 ``after_vars``；``None`` 表示与 ``after`` 相同。
    """
    try:
        await db.execute("BEGIN IMMEDIATE")
        # 事务内重读：拿到的是「真正被覆盖的那一版」
        cur = await db.execute("SELECT content, updated_at FROM prompt_templates WHERE key=?", (key,))
        row = await cur.fetchone() if cur is not None else None
        real_before = (row["content"] if row else "") or before
        real_updated_at = row["updated_at"] if row else None
        if expected_updated_at is not None and real_updated_at != expected_updated_at:
            await db.rollback()
            raise HTTPException(409, "提示词已被他人修改（版本冲突），请刷新后重试")
        before_vars = extract_user_variables(real_before)
        eff = effective_after if effective_after is not None else after
        after_vars = extract_user_variables(eff)
        if not after.strip():
            await db.execute("DELETE FROM prompt_templates WHERE key=?", (key,))
        else:
            # ✅ R204（2026-11-09 · CAS 精度）：旧写法 ``datetime('now','localtime')``
            #   只到**秒** —— 同一秒内的两次并发写，第二次读到的 ``updated_at``
            #   与客户端回传的 ``expected_updated_at`` **完全相同** → CAS 判据
            #   失效 → 后写**静默覆盖**前写（模块自述「冲突时返回 409」永不触发，
            #   回滚路径同理可覆盖掉别人刚保存的版本）。
            #   现改为**毫秒**精度：时间戳由**应用层**生成（``_now_ts()``）——
            #   SQLite 的 ``strftime('%f')`` 并不被支持（实测 SQLite 3.50.4
            #   返回 NULL，会把 updated_at 写成 NULL），故不能走 SQL 侧格式。
            #   兼容：旧库里的秒级字符串照常可比（只是精度低，仍能防跨秒冲突）。
            await db.execute(
                "INSERT INTO prompt_templates (key, content, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET content=excluded.content,"
                " updated_at=excluded.updated_at",
                (key, after, _now_ts()),
            )
        await record_prompt_audit(db, key, action, real_before, eff, before_vars, after_vars, request)
        await db.commit()
        return real_before, before_vars, after_vars
    except HTTPException:
        raise
    except Exception:
        await db.rollback()
        raise


@router.get("")
async def list_prompts(category: str = "", db=Depends(read_db)):
    """列出全部提示词，并合并 DB 生效内容、修改时间与审计计数。"""
    from app.services.ai.prompts._registry import (
        clean_prompt_text,
        extract_user_variables,
    )
    from app.services.ai.prompts._registry import (
        list_prompts as _lp,
    )

    items = _lp(category) if category else _lp()
    cur = await db.execute("SELECT key, content, updated_at FROM prompt_templates")
    rows = await cur.fetchall() if cur is not None else []
    meta = {r["key"]: dict(r) for r in rows}
    cur2 = await db.execute("SELECT prompt_key, COUNT(*) AS n FROM prompt_audit_logs GROUP BY prompt_key")
    counts = {r["prompt_key"]: int(r["n"] or 0) for r in (await cur2.fetchall() if cur2 is not None else [])}
    for it in items:
        row = meta.get(it["key"])
        if row and (row.get("content") or "").strip():
            # ✅ 2026-09-27（BUG-P1-D · 展示 ≠ 渲染 ≠ hash 三方分叉）：
            #   运行时缓存加载时会 clean_prompt_text()（去 BOM/零宽字符/统一换行），
            #   而这里直接回传 DB 原文。后果：编辑器里看到的正文与真正下发给
            #   模型的**不是同一份**，且 content_hash 算的是原文哈希 ——
            #   与审计里的 after_hash（同一函数）看似一致，实际「改一个字就
            #   显示已修改」的同时「运行时可能仍是旧内容」。此处按同一函数清洗，
            #   使「展示 = 渲染 = hash」三者恒等。
            it["content"] = clean_prompt_text(row["content"])
            it["variables"] = extract_user_variables(it["content"])
        it["modified"] = it.get("content", "") != it.get("default_content", "")
        it["content_hash"] = prompt_content_hash(str(it.get("content") or ""))
        it["updated_at"] = (row or {}).get("updated_at") or ""
        it["audit_count"] = counts.get(it["key"], 0)
    return {"items": items}


@router.get("/audit-logs")
async def prompt_audit_logs_legacy_denied():
    """审计查询已迁至管理面（保留本路由仅用于给出明确指引）。

    ✅ R204（2026-11-09 · 越权面收口）：提示词**审计**含
    ``client_ip`` 与内容指纹，属治理面敏感数据；此前它与写端点一起挂在
    无门禁的 ``/api/v1/prompts`` 上，任何可达客户端都能读全平台提示词的
    变更历史。现随写端点一并迁至 ``/admin/prompts/*``（fail-closed 闸门）。
    """
    raise HTTPException(
        403,
        "提示词审计接口已迁移至管理面：GET /admin/prompts/audit-logs"
        "（本机回环可直接访问，远程需携带 X-Admin-Token）",
    )


@router.patch("/{key}")
async def update_prompt_legacy_denied(key: str):
    """写接口已迁至管理面（保留本路由仅用于给出明确指引）。"""
    raise HTTPException(
        403,
        f"提示词写接口已迁移至管理面：PATCH /admin/prompts/{key}"
        "（本机回环可直接访问，远程需携带 X-Admin-Token）",
    )


@router.post("/{key}/reset")
async def reset_prompt_legacy_denied(key: str):
    """写接口已迁至管理面（保留本路由仅用于给出明确指引）。"""
    raise HTTPException(
        403,
        f"提示词写接口已迁移至管理面：POST /admin/prompts/{key}/reset"
        "（本机回环可直接访问，远程需携带 X-Admin-Token）",
    )


@router.post("/{key}/rollback")
async def rollback_prompt_legacy_denied(key: str):
    """写接口已迁至管理面（保留本路由仅用于给出明确指引）。"""
    raise HTTPException(
        403,
        f"提示词写接口已迁移至管理面：POST /admin/prompts/{key}/rollback"
        "（本机回环可直接访问，远程需携带 X-Admin-Token）",
    )


@admin_prompts_router.get("/audit-logs")
async def prompt_audit_logs(key: str, limit: int = 50, offset: int = 0, db=Depends(read_db)):
    """提示词变更审计（只含指纹与变量集合，不复制完整正文）。

    ✅ R204：随写端点迁至管理面（``require_admin_access``，fail-closed）。
    """
    try:
        return await list_prompt_audit_logs(db, key, limit, offset)
    except ValueError as e:
        # ✅ 2026-11-07：不再回显异常原文（仅保留脱敏后的短说明）。
        raise HTTPException(400, _safe_error(e, max_chars=120))


@admin_prompts_router.patch("/{key}")
async def update_prompt(key: str, body: dict, db=Depends(get_db), request: Request = None):
    """保存提示词编辑；空内容按“恢复默认”处理。

    ✅ R204（2026-11-09 · 管理面收口）：本端点自 ``/api/v1/prompts/{key}``
    迁至 ``/admin/prompts/{key}``（``require_admin_access`` 闸门）——
    提示词是**全局共享资产**，无门禁的写接口等价于「任意客户端可改写全平台
    提示词」（此前 ``/admin/prompts/sync-from-code`` 有闸门、而功能等价的
    保存/重置/回滚没有，属口径倒挂）。
    """
    from app.services.ai.prompts._cache import reload_prompt_cache
    from app.services.ai.prompts._registry import (
        _ALL_PROMPTS,
        get_default_prompt,
    )
    from app.services.ai.prompts._registry import (
        reset_prompt as _reset,
    )
    from app.services.ai.prompts._registry import (
        update_prompt as _up,
    )

    if key not in _ALL_PROMPTS:
        raise HTTPException(404, "提示词不存在")
    # ✅ R204（数据丢失防护）：旧实现 ``body.get("content", "")`` 把「**字段缺失**」
    #   与「显式空串」压成同一语义 —— 请求体写错字段名（``{"text": ...}``）或
    #   网关吞字段时，空串会走 reset 分支 **DELETE 掉用户定制**（并记一条
    #   action="reset" 审计，看起来像"保存成功"）。现在区分二者：
    #   缺字段 → 400；显式空串 → 才走「恢复默认」（另有独立端点 POST /reset）。
    if not isinstance(body, dict) or "content" not in body:
        raise HTTPException(400, "缺少 content 字段（恢复出厂默认请用 POST /admin/prompts/{key}/reset）")
    content = body.get("content", "")
    if not isinstance(content, str):
        raise HTTPException(400, "提示词内容必须是字符串")
    if len(content) > PROMPT_MAX_CHARS:
        raise HTTPException(400, f"提示词内容过长（最多 {PROMPT_MAX_CHARS} 字符）")

    # ✅ BUG-P1-C 保存期体检：error 级问题（不存在的 {SHARED_*} / 共享片段自引用）
    #    直接 400 拒绝；warning 级（契约变量增删）照常保存但随响应回传，
    #    让用户看到「保存成功 ≠ 一定正确」。
    issues = validate_prompt_content(key, content)
    errors = [i for i in issues if i["level"] == "error"]
    warnings_ = [i for i in issues if i["level"] == "warning"]
    if errors:
        raise HTTPException(400, errors[0]["message"])

    # ✅ 2026-09-27（BUG-P1-D）：入库前统一清洗，使「入库 = 展示 = 渲染 = hash」
    #    恒等（旧实现只有运行时缓存清洗，编辑器展示的却是未清洗原文）。
    content = clean_prompt_text(content)

    row = await _fetch_content_row(db, key)
    before = (row["content"] if row else "") or get_default_prompt(key)
    expected_updated_at = row["updated_at"] if row else None

    is_reset = not content.strip()
    default = get_default_prompt(key)
    # 恢复默认 = **删除该行**（落库空串），与 POST /reset 同口径 ——
    # 保留行会把「出厂默认」复制一份进库，徒增表体积且让「是否被改过」
    # 只能靠内容比对来推断。变更后实际生效的是出厂默认，故传 effective_after。
    before, before_vars, after_vars = await _write_with_before(
        db,
        key,
        before,
        "" if is_reset else content,
        "reset" if is_reset else "update",
        request,
        expected_updated_at=expected_updated_at,
        effective_after=default if is_reset else None,
    )
    if is_reset:
        _reset(key)
    else:
        _up(key, content)
    reload_prompt_cache()
    return {
        "ok": True,
        "content": default if is_reset else content,
        # ✅ 2026-09-25（BUG-D · 响应契约对称）：空内容（=恢复默认）分支也
        #   必须回传 variables，前端与脚本才能统一取「当前生效变量集合」。
        "reset": True if is_reset else None,
        "content_hash": prompt_content_hash(default if is_reset else content),
        "variables": after_vars,
        "added_variables": sorted(set(after_vars) - set(before_vars)),
        "removed_variables": sorted(set(before_vars) - set(after_vars)),
        "warnings": [i["message"] for i in warnings_],
    }


@admin_prompts_router.post("/{key}/reset")
async def reset_prompt(key: str, db=Depends(get_db), request: Request = None):
    """恢复出厂默认提示词（管理面，fail-closed 闸门）。"""
    from app.services.ai.prompts._cache import reload_prompt_cache
    from app.services.ai.prompts._registry import (
        _ALL_PROMPTS,
        get_default_prompt,
    )
    from app.services.ai.prompts._registry import (
        reset_prompt as _reset,
    )

    default = get_default_prompt(key) if key in _ALL_PROMPTS else None
    if default is None:
        raise HTTPException(404, "提示词不存在")
    row = await _fetch_content_row(db, key)
    before = (row["content"] if row else "") or default
    # 恢复出厂默认 = 删除该行（落库空串），变更后实际生效的是出厂默认
    before, before_vars, after_vars = await _write_with_before(
        db,
        key,
        before,
        "",
        "reset",
        request,
        expected_updated_at=(row["updated_at"] if row else None),
        effective_after=default,
    )
    _reset(key)
    reload_prompt_cache()
    return {
        "ok": True,
        "content": default,
        "content_hash": prompt_content_hash(default),
        # ✅ 2026-09-25（BUG-D · 响应契约对称）：三个变更端点（PATCH 非空 /
        #   PATCH 空内容 / POST reset）一律回传 variables，供调用方直接
        #   判断「当前生效模板的变量集合」而不必自行重算。
        "variables": after_vars,
        "added_variables": sorted(set(after_vars) - set(before_vars)),
        "removed_variables": sorted(set(before_vars) - set(after_vars)),
        "warnings": [],
    }


@admin_prompts_router.post("/{key}/rollback")
async def rollback_prompt(key: str, body: dict, db=Depends(get_db), request: Request = None):
    """把提示词回滚到某条审计记录**变更前**的版本（✅ G2 版本回滚 · 管理面）。

    语义与 ``POST /api/v1/ai/config/{id}/rollback`` 对齐：
      1. 需要目标审计行的快照里带 ``before`` 正文 —— 历史行（引入快照前写入）
         只有哈希、没有正文 → 400 明确提示，不让用户点了必然失败；
      2. 快照正文若被截断（``before_truncated=True``）→ 400 拒绝：
         截断的正文不是完整提示词，写回去等于制造一份损坏的模板；
      3. 回滚本身也写一条 ``action="rollback"`` 审计（同样带前后快照），
         因此「回滚」也可以被再次回滚，形成可撤销的链；
      4. 提示词 key 未注册 → 404；审计行不存在或属于别的 key → 404；
      5. 回滚到「出厂默认」之外仍写 ``prompt_templates``（保持与编辑同口径），
         空内容不写（与「保存空内容 = 恢复默认」一致）。

    请求体：``{"audit_id": "<审计行 id>"}``
    """
    from app.services.ai.prompts._cache import reload_prompt_cache
    from app.services.ai.prompts._registry import (
        _ALL_PROMPTS,
        get_default_prompt,
    )
    from app.services.ai.prompts._registry import (
        update_prompt as _up,
    )
    from app.services.audit_service import prompt_snapshot_is_rollbackable

    if key not in _ALL_PROMPTS:
        raise HTTPException(404, "提示词不存在")
    audit_id = (body or {}).get("audit_id") or ""
    if not isinstance(audit_id, str) or not audit_id.strip():
        raise HTTPException(400, "缺少 audit_id")
    audit_id = audit_id.strip()

    cur = await db.execute("SELECT id, prompt_key, snapshot_json FROM prompt_audit_logs WHERE id=?", (audit_id,))
    if cur is None:
        raise HTTPException(503, "数据库连接异常，请重试")
    row = await cur.fetchone()
    if not row or row["prompt_key"] != key:
        raise HTTPException(404, "审计记录不存在或不属于该提示词")

    raw = row["snapshot_json"] or ""
    try:
        snap = json.loads(raw) if raw else {}
    except Exception:
        snap = {}
    # ✅ 2026-09-27（BUG-P1-E · rollbackable 与前置校验口径分叉）：
    #   本段三条校验与 audit_service.list_prompt_audit_logs 里算
    #   ``rollbackable`` 的判据曾是**两份独立实现** —— 后者只判「before 非空」，
    #   于是被截断的审计行会向前端回 rollbackable=True，前端据此渲染可点的
    #   「回滚」按钮（PromptEditorPage.tsx:427），用户一点必 400。
    #   现统一到单一函数 prompt_snapshot_is_rollbackable()，两侧共用。
    ok, reason = prompt_snapshot_is_rollbackable(snap, PROMPT_MAX_CHARS)
    if not ok:
        raise HTTPException(400, reason)
    before = snap["before"]

    default = get_default_prompt(key)
    row2 = await _fetch_content_row(db, key)
    expected_updated_at = row2["updated_at"] if row2 else None
    # 与「保存/重置」同口径：单事务 + CAS，事务提交成功后才更新内存注册表
    # 并失效运行时缓存，避免出现「已落库但运行时仍用旧缓存」的不一致窗口。
    current, current_vars, restored_vars = await _write_with_before(
        db,
        key,
        (row2["content"] if row2 else "") or default,
        before,
        "rollback",
        request,
        expected_updated_at=expected_updated_at,
    )
    _up(key, before)
    reload_prompt_cache()
    return {
        "ok": True,
        "content": before,
        "content_hash": prompt_content_hash(before),
        "variables": restored_vars,
        "added_variables": sorted(set(restored_vars) - set(current_vars)),
        "removed_variables": sorted(set(current_vars) - set(restored_vars)),
        "rollback_from_audit": audit_id,
        "warnings": [i["message"] for i in validate_prompt_content(key, before) if i["level"] == "warning"],
    }


# ---------------------------------------------------------------------------
# ✅ R48（2026-10-06）：prompts 运行时指标（进程内内存计数器，重启清零）
# ---------------------------------------------------------------------------
@system_metrics_router.get("/prompt-metrics")
async def get_prompt_metrics(db=Depends(read_db)):
    """提示词运行时指标快照（进程内内存计数器，不持久化、重启清零）。

    顶层为「若干计数器维度 dict + ``registered_keys``（注册表全部 key，供运维
    对照「哪些模板从无人渲染过」）」。计数器随功能演进**加法式增加**，此处
    **不再硬编码键数**（旧 docstring 写「固定 7 个键 … 六个维度」，与实现不符）。

    ✅ R180（2026-11-05 · 可观测性）：新增 ``today`` 子对象（加法式，不改原有键）：
      · ``version_changes_today``：今日提示词版本变更（覆盖/回滚/恢复默认）次数，
        来源 ``prompt_audit_logs``（每次变更都写审计，见 audit_service）；
      · ``render_calls_today``：今日渲染次数，口径为 ``ai_audit_logs`` 调用数
        （本仓每次 AI 调用都渲染一次提示词；进程内 ``render_total`` 重启清零，
        无法回答「今日」，故给出 DB 侧近似口径，并显式标注 ``render_basis``）。
    指标查询失败不得影响运维页：任何异常都降级为 0 并在 ``today.error`` 说明。
    """
    _snap = _get_metrics_snapshot()
    _today = time.strftime("%Y-%m-%d")
    _today_stats = {"version_changes_today": 0, "render_calls_today": 0}
    if hasattr(db, "execute"):  # 直调（非 FastAPI 注入）时 db 为 Depends 占位，跳过统计
        try:
            _cur = await db.execute("SELECT COUNT(*) FROM prompt_audit_logs WHERE created_at >= ?", (_today,))
            _row = await _cur.fetchone() if _cur is not None else None
            if _row is not None:
                _today_stats["version_changes_today"] = int(_row[0] or 0)
            _cur = await db.execute("SELECT COUNT(*) FROM ai_audit_logs WHERE created_at >= ?", (_today,))
            _row = await _cur.fetchone() if _cur is not None else None
            if _row is not None:
                _today_stats["render_calls_today"] = int(_row[0] or 0)
        except Exception as e:  # 指标失败绝不阻断页面
            # ✅ 2026-11-07：日志与响应都只保留脱敏后的短文本（不落凭据/内部路径明文）。
            _err = _safe_error(e, max_chars=120)
            logger.warning("prompt-metrics 今日计数查询失败（已降级为 0）: %s", _err)
            _today_stats["error"] = _err
    _today_stats["render_basis"] = "ai_audit_logs 调用数（每次 AI 调用渲染一次提示词，近似口径）"
    _snap["today"] = _today_stats
    # ✅ R220（加法式）：AI Token 用量进程内快照。既有键逐字不变；
    #    读取失败由 get_usage_snapshot 内部降级为空结构，绝不拖垮运维页。
    _snap["ai_usage"] = _get_usage_snapshot()
    return _snap


@system_metrics_router.post("/prompt-metrics/reset")
async def reset_prompt_metrics(request: Request = None, _admin: None = Depends(require_admin_access)):
    """清零提示词运行时内存计数器（运维调试 / 测试隔离用）。

    ✅ R204（2026-11-09 · 越权面收口）：清零会**销毁运维证据**（谁在读不存在的
    模板、哪些提示词被截断），此前挂在无门禁的 ``/system`` 前缀下，任意客户端
    均可调用。现与管理面同口径（``require_admin_access``，fail-closed：
    本机回环免 token，远程需 ``X-Admin-Token``）。
    """
    _reset_metrics_counters()
    return {"ok": True, "message": "prompt metrics counters reset"}


# ---------------------------------------------------------------------------
# ✅ R48（2026-10-06）：硬编码 → DB 一键同步（seed 新模板 / 报告漂移）
# ---------------------------------------------------------------------------
@admin_prompts_router.post("/sync-from-code")
async def sync_prompts_from_code(force: bool = False, db=Depends(get_db)):
    """遍历注册表全部模板 key，对比硬编码出厂值 vs DB 现有行并同步。

    逐 key 语义：
      * DB 无行 → 用硬编码出厂值**插入**，``status="inserted"``；
      * DB 行与硬编码逐字一致 → 不动，``status="in_sync"``；
      * DB 行与硬编码不一致（用户在后台改过）→ **默认不覆盖**，
        ``status="drift"`` 并回传 ``code_hash``/``db_hash``/``db_modified_at``；
        仅当 query ``force=true`` 时才用硬编码覆盖（会丢失用户后台编辑成果，
        故默认 false），``status="overwritten"``。

    幂等：第二次调用 inserted=0、in_sync=绝大多数、drift 反映真实漂移。
    fail-soft：单个 key 出错记 error 并跳过，不阻断其他 key。
    """
    from app.services.ai.prompts._cache import reload_prompt_cache
    from app.services.ai.prompts._registry import (
        _ALL_PROMPTS,
        get_default_prompt,
        register_lazy_prompts,
    )

    register_lazy_prompts()
    summary = {"inserted": 0, "in_sync": 0, "drift": 0, "overwritten": 0, "errors": 0}
    details: list[dict] = []
    # ✅ R204（2026-11-09 · 原子性）：force 覆盖时**内存注册表**的改写必须
    #   发生在 commit **之后**。旧实现先 `_up()`（内存立即变）再 commit，
    #   commit 失败 → DB 回滚、内存已改 → 「列表显示新内容、运行期用旧内容」，
    #   且 reload_prompt_cache() 也不会执行（缓存仍是旧值）→ 三方不一致。
    _pending_memory_updates: list[tuple[str, str]] = []
    for key in sorted(_ALL_PROMPTS.keys()):
        # ✅ R204：每个 key 用**独立保存点**包裹「模板行 + 审计行」——
        #   旧实现两者在同一个 try 里，审计写失败时只 errors+=1 并 continue，
        #   模板行**仍留在待提交事务里**最终被 commit → 「内容已改但无审计留痕」
        #   （违背模块自述「内容变更必留痕」），且事后无法回滚。
        _sp = f"r203_sync_{key}"
        try:
            await db.execute(f"SAVEPOINT {_sp}")
        except Exception as e:  # noqa: BLE001 - 保存点不可用时退回旧行为（不阻断）
            logger.warning("sync-from-code 建立保存点失败（继续，原子性降级）: %s", _safe_error(e))
            _sp = ""
        try:
            # 对比基准 = 硬编码出厂值（统一清洗，与 DB 落库口径一致）
            code_content = clean_prompt_text(get_default_prompt(key))
            cur = await db.execute("SELECT content, updated_at FROM prompt_templates WHERE key=?", (key,))
            row = await cur.fetchone() if cur is not None else None
            if row is None:
                # DB 无行 → 插入硬编码出厂值
                await db.execute(
                    "INSERT INTO prompt_templates (key, content, updated_at) VALUES (?,?,"
                    "strftime('%Y-%m-%d %H:%M:%f','localtime'))",
                    (key, code_content),
                )
                # ✅ R169：同步写入也要落审计（与 PATCH/reset/rollback 同为「内容
                #    变更必留痕」口径）。旧实现直写 DB 不写 prompt_audit_logs，
                #    force 之外的首插行不在任何审计快照里；本函数与最终 commit
                #    同事务提交（record_prompt_audit 自身不 commit）。
                await record_prompt_audit(
                    db, key, "sync_insert", "", code_content, [], extract_user_variables(code_content), None
                )
                summary["inserted"] += 1
                details.append(
                    {
                        "key": key,
                        "status": "inserted",
                        "code_hash": prompt_content_hash(code_content),
                    }
                )
                if _sp:
                    await db.execute(f"RELEASE {_sp}")
                continue
            db_content = clean_prompt_text(row["content"] or "")
            if db_content == code_content:
                summary["in_sync"] += 1
                details.append(
                    {
                        "key": key,
                        "status": "in_sync",
                        "code_hash": prompt_content_hash(code_content),
                        "db_hash": prompt_content_hash(db_content),
                        "db_modified_at": row["updated_at"] or "",
                    }
                )
                if _sp:
                    await db.execute(f"RELEASE {_sp}")
                continue
            # 不一致：默认 drift（不覆盖用户编辑）；force=true 才覆盖
            if force:
                await db.execute(
                    "INSERT INTO prompt_templates (key, content, updated_at) VALUES (?,?,?)"
                    " ON CONFLICT(key) DO UPDATE SET content=excluded.content,"
                    " updated_at=excluded.updated_at",
                    (key, code_content, _now_ts()),
                )
                # ✅ R204：内存改写延后到 commit 成功之后（见 _pending_memory_updates）。
                _pending_memory_updates.append((key, code_content))
                # ✅ R169：force 覆盖用户编辑是本端点唯一可能**丢失内容**的路径，
                #    被覆盖的 DB 原文必须进审计快照，否则回滚 UI 永远无法恢复。
                _db_before = row["content"] or ""
                await record_prompt_audit(
                    db,
                    key,
                    "sync_overwrite",
                    _db_before,
                    code_content,
                    extract_user_variables(_db_before),
                    extract_user_variables(code_content),
                    None,
                )
                summary["overwritten"] += 1
                details.append(
                    {
                        "key": key,
                        "status": "overwritten",
                        "code_hash": prompt_content_hash(code_content),
                        "db_hash": prompt_content_hash(db_content),
                        "db_modified_at": row["updated_at"] or "",
                    }
                )
            else:
                summary["drift"] += 1
                details.append(
                    {
                        "key": key,
                        "status": "drift",
                        "code_hash": prompt_content_hash(code_content),
                        "db_hash": prompt_content_hash(db_content),
                        "db_modified_at": row["updated_at"] or "",
                    }
                )
            if _sp:
                await db.execute(f"RELEASE {_sp}")
        except Exception as e:  # noqa: BLE001 - 单 key 失败不阻断其他 key
            summary["errors"] += 1
            if _sp:
                # 回滚该 key 的**全部**改动（模板行 + 审计行）→ 真正 all-or-nothing
                try:
                    await db.execute(f"ROLLBACK TO {_sp}")
                    await db.execute(f"RELEASE {_sp}")
                except Exception as e2:  # noqa: BLE001 - 回滚失败只记日志
                    logger.warning("sync-from-code 回滚保存点失败 key=%s: %s", key, _safe_error(e2))
                # 该 key 的内存改写尚未发生（延后到 commit 后），此处无需回退
            # ✅ 2026-11-07：批量明细与日志同样走脱敏收敛，避免错误体带出凭据。
            _err = _safe_error(e, max_chars=160)
            logger.warning("sync-from-code 处理 key=%s 失败（跳过，不阻断）: %s", key, _err)
            details.append({"key": key, "status": "error", "error": _err})

    try:
        await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("sync-from-code commit 失败: %s", _safe_error(e))
        await db.rollback()
        raise HTTPException(500, "硬编码→DB 同步提交失败")
    # ✅ R204：commit 成功后才改内存注册表（此前 force 分支在 commit 前改，
    #   commit 失败会造成「内存已改 / DB 已回滚 / 缓存未失效」三方不一致）。
    if _pending_memory_updates:
        from app.services.ai.prompts._registry import (
            update_prompt as _up,
        )

        for _k, _c in _pending_memory_updates:
            _up(_k, _c)
    reload_prompt_cache()
    return {"summary": summary, "details": details, "force": force}
