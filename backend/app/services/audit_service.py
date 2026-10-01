"""统一审计服务：配置变更审计 + 提示词变更审计。

本模块是所有审计写入的唯一实现入口：

* 配置审计（``ai_config_audit_logs``）：
  - 记录配置新增/修改/删除/切换/降级链/导入/清 Key/场景路由/环境/运行时开关/回滚；
  - 快照只允许非敏感字段，密钥仅以 ``has_key`` 布尔体现；
  - 写入是「尽力而为」，旧库缺列时降级写入，不阻断业务主流程。
* 提示词审计（``prompt_audit_logs``）：
  - 只记录内容 SHA-256 与变量集合，不复制完整提示词正文；
  - 与提示词内容更新处于同一事务，由路由层负责 commit。

路由层与业务层统一从本模块导入，避免审计逻辑散落在多个路由文件。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from fastapi import Request

from app.config import settings
from app.services.crypto import decrypt_api_key

logger = logging.getLogger("audit_service")

#: 配置变更动作 → 中文标签（前端展示用；新增动作时同步登记）。
CONFIG_ACTIONS: dict[str, str] = {
    "create": "新增配置",
    "update": "修改配置",
    "delete": "删除配置",
    "toggle": "切换当前使用",
    "fallback_chain": "调整降级链顺序",
    "import": "导入配置",
    "clear_key": "清除 API Key",
    "scene_route": "调整场景模型路由",
    "env": "切换生效环境",
    "rollback": "回滚配置版本",
    "runtime_switch": "调整运行时厂商开关",
}

#: detail 字段最大长度（防超长内容把审计表撑大）。
_DETAIL_MAX = 500

#: 允许进快照的字段白名单 —— **不含任何密钥相关列**。
SNAPSHOT_FIELDS: tuple[str, ...] = (
    "provider_name", "plan", "base_url", "model", "max_tokens", "temperature",
    "timeout", "concurrency", "request_mode", "env", "remark", "is_active",
)

#: 快照字段 → 中文名（生成结构化 diff 用）。
FIELD_LABELS: dict[str, str] = {
    "provider_name": "供应商",
    "plan": "计费方式",
    "base_url": "Base URL",
    "model": "模型",
    "max_tokens": "Max Tokens",
    "temperature": "Temperature",
    "timeout": "超时（秒）",
    "concurrency": "并发数",
    "request_mode": "请求方式",
    "env": "环境",
    "remark": "备注",
    "is_active": "当前使用",
}

#: 字符串字段限长：避免单条超长备注/地址把审计快照撑大。数值字段不转字符串。
_SNAPSHOT_STRING_LIMITS: dict[str, int] = {
    "provider_name": 128,
    "plan": 32,
    "base_url": 2048,
    "model": 512,
    "request_mode": 32,
    "env": 64,
    "remark": 2000,
}


def sanitize_config_snapshot(cfg: dict | None) -> dict | None:
    """把一条 ai_config 行转成**可安全落库**的快照（脱敏）。"""
    if not cfg:
        return None
    out: dict = {}
    for f in SNAPSHOT_FIELDS:
        if f not in cfg:
            continue
        value = cfg.get(f)
        if f in _SNAPSHOT_STRING_LIMITS and isinstance(value, str):
            value = value[:_SNAPSHOT_STRING_LIMITS[f]]
        out[f] = value
    enc = cfg.get("api_key_encrypted") or ""
    # 与 GET /ai/config 的 has_key 口径一致：「有密文且能解密」才算已设置
    out["has_key"] = int(bool(enc) and bool(decrypt_api_key(enc)))
    return out


def _json_snapshot(snapshot: dict | None) -> str:
    """序列化快照（禁止字符级截断，否则会生成非法 JSON）。"""
    if not snapshot:
        return ""
    try:
        return json.dumps(snapshot, ensure_ascii=False)
    except Exception:
        return ""


def diff_snapshots(snapshot: dict | None) -> list[dict]:
    """把 ``{"before": {...}, "after": {...}}`` 变成结构化变更列表。"""
    if not isinstance(snapshot, dict):
        return []
    before = snapshot.get("before") or {}
    after = snapshot.get("after") or {}
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    changes: list[dict] = []
    for f in SNAPSHOT_FIELDS:
        if f not in before and f not in after:
            continue
        b, a = before.get(f), after.get(f)
        if b == a:
            continue
        changes.append({
            "field": f,
            "label": FIELD_LABELS.get(f, f),
            "before": "" if b is None else b,
            "after": "" if a is None else a,
        })
    if "has_key" in before or "has_key" in after:
        if before.get("has_key") != after.get("has_key"):
            changes.append({
                "field": "has_key", "label": "API Key",
                "before": "已设置" if before.get("has_key") else "未设置",
                "after": "已设置" if after.get("has_key") else "未设置",
            })
    return changes


def client_ip_of(request: Request | None) -> str:
    """取调用方 IP；仅当直连对端是显式可信代理时才信任 X-Forwarded-For。"""
    if request is None:
        return ""
    try:
        peer = (request.client.host if request.client else "") or ""
        trusted = {
            ip.strip() for ip in (settings.trusted_proxy_ips or "").split(",")
            if ip.strip()
        }
        if peer in trusted:
            fwd = request.headers.get("x-forwarded-for", "") or ""
            if fwd:
                return fwd.split(",")[0].strip()[:64]
        return peer[:64]
    except Exception:
        return ""


_AUDIT_INSERT_FULL = (
    "INSERT INTO ai_config_audit_logs"
    " (id, action, config_id, provider_name, model, detail, client_ip, snapshot_json, created_at)"
    " VALUES (?,?,?,?,?,?,?,?,?)")
# 旧库尚未 _migrate 出 snapshot_json 列时的降级写入（审计行不丢，仅无 diff/回滚信息）
_AUDIT_INSERT_LEGACY = (
    "INSERT INTO ai_config_audit_logs"
    " (id, action, config_id, provider_name, model, detail, client_ip, created_at)"
    " VALUES (?,?,?,?,?,?,?,?)")


async def record_config_audit(db, action: str, *, config_id: str = "",
                              provider_name: str = "", model: str = "",
                              detail: str = "", request: Request | None = None,
                              snapshot: dict | None = None,
                              commit: bool = True) -> None:
    """写入一条配置变更审计（尽力而为，失败不影响主流程）。"""
    base = (str(uuid.uuid4()), action, config_id or "", provider_name or "",
            model or "", (detail or "")[:_DETAIL_MAX], client_ip_of(request),
            datetime.now().isoformat())
    snap_json = _json_snapshot(snapshot)
    try:
        if snap_json:
            await db.execute(_AUDIT_INSERT_FULL, base[:7] + (snap_json, base[7]))
        else:
            await db.execute(_AUDIT_INSERT_LEGACY, base)
        if commit:
            await db.commit()
    except Exception as e:
        # 列缺失（旧库）时降级重试一次：宁可丢掉 diff 信息，也不要丢审计行
        if snap_json:
            try:
                await db.execute(_AUDIT_INSERT_LEGACY, base)
                if commit:
                    await db.commit()
                logger.debug("ai_config_audit_logs 缺 snapshot_json 列，已降级写入")
                return
            except Exception as e2:
                logger.debug("配置审计降级写入失败（忽略）: %s", e2)
                return
        logger.debug("配置审计写入失败（忽略，不影响主流程）: %s", e)


def prompt_content_hash(content: str) -> str:
    """提示词内容 SHA-256（用于审计比对，不保存正文）。"""
    import hashlib
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


#: 提示词正文长度上限（20 万字符）。
#: ✅ 2026-09-27（BUG-P1-F · 同值双份字面量）：此前该值在
#:   ``routers/prompts.py::PROMPT_MAX_CHARS`` 与
#:   ``config.prompt_audit_snapshot_max_chars`` 各写一份 200000，
#:   而「回滚时长度校验」与「快照截断阈值」必须同口径 —— 两者一旦调成
#:   不同值就会出现「快照按 A 截断、按 B 放行」的窗口（截断标记没打，
#:   但长度已超写入上限，写回去即损坏模板）。现统一到本常量。
PROMPT_MAX_CHARS = 200000


#: 提示词变更动作 → 中文标签（前端展示用）。
PROMPT_AUDIT_ACTIONS: dict[str, str] = {
    "update": "修改提示词",
    "reset": "恢复出厂默认",
    "rollback": "回滚版本",
}


def prompt_snapshot_json(before: str, after: str) -> str:
    """构造提示词变更快照 ``{"before": ..., "after": ...}``（G2 版本回滚数据基础）。

    旧实现只存 SHA-256 哈希，哈希无法还原正文 → 审计只能"看到变了"，却无法把
    改坏的提示词恢复回去（提示词是核心资产，改坏会直接拉低全线生成质量）。
    本函数存完整正文，但做两件事保证健壮性：

    1. **长度保护**：单侧超过 ``settings.prompt_audit_snapshot_max_chars`` 时
       截断并标记 ``truncated=True``（不截断会让一条超大内容把审计表撑爆）；
    2. **恒为合法 JSON**：与 ai_config 快照一致口径 —— 只截断字符串值，
       绝不做字符级截断 JSON 本身。

    由 ``settings.prompt_audit_snapshot_enabled`` 控制是否调用（默认开）。
    """
    if not before and not after:
        return ""
    try:
        limit = int(getattr(settings, "prompt_audit_snapshot_max_chars", 200000) or 0)
    except Exception:
        limit = 200000
    snap: dict = {}
    for role, txt in (("before", before or ""), ("after", after or "")):
        txt = txt or ""
        if limit > 0 and len(txt) > limit:
            snap[role] = txt[:limit]
            snap[f"{role}_truncated"] = True
            snap[f"{role}_chars"] = len(txt)
        else:
            snap[role] = txt
    return json.dumps(snap, ensure_ascii=False)


#: 无法回滚时的统一原因文案（前端 tooltip / HTTP 400 detail 共用同一份）。
ROLLBACK_BLOCKED_REASONS = {
    "no_snapshot": "该变更记录没有可用的变更前正文（旧记录只存哈希，无法回滚）",
    "truncated": ("该变更记录的变更前正文已被截断，不是完整提示词，"
                  "为避免写入损坏模板已拒绝回滚。"
                  "请改用「恢复出厂默认」或手动重新编辑。"),
    "too_long": "快照正文超长，已拒绝回滚",
    "malformed": "该变更记录的快照格式异常，无法回滚",
}


def prompt_snapshot_is_rollbackable(snapshot, max_chars: int = 200000
                                    ) -> tuple[bool, str]:
    """快照是否可回滚 → ``(是否可回滚, 不可回滚时的原因)``。

    ✅ 2026-09-27（BUG-P1-E · 「能不能回滚」判据曾有两份实现）：
    本模块的 :func:`list_prompt_audit_logs` 算 ``rollbackable`` 时只判
    「``before`` 非空」；而 ``routers/prompts.py::rollback_prompt`` 实际有
    **三条**前置校验（非空 / 未截断 / 未超长）。两侧分叉的直接后果：
    被截断的审计行 ``rollbackable=True`` → 前端渲染出可点的「回滚」按钮
    （``PromptEditorPage.tsx`` 用 ``log.rollbackable`` 决定按钮是否 disabled）
    → 用户一点必然 400，且提示语与按钮 tooltip 不一致。
    这正是 AGENTS.md §4.3 点名的「同一判据在两处各自实现」模式。

    现把判据收敛到本函数：列表接口用它算 ``rollbackable``，
    回滚端点用它决定是否放行 —— **按钮可点 ⟺ 回滚必成功**（不变量）。

    :param snapshot: 已解析的 ``snapshot_json``（dict；非 dict 一律不可回滚）。
    :param max_chars: 正文长度上限，与写入侧 ``PROMPT_MAX_CHARS`` 同口径。
    :return: ``(True, "")`` 或 ``(False, 原因文案)``。
    """
    if not isinstance(snapshot, dict):
        return False, ROLLBACK_BLOCKED_REASONS["malformed"]
    before = snapshot.get("before")
    if not before:
        return False, ROLLBACK_BLOCKED_REASONS["no_snapshot"]
    if snapshot.get("before_truncated"):
        return False, ROLLBACK_BLOCKED_REASONS["truncated"]
    if max_chars and len(before) > max_chars:
        return False, f"{ROLLBACK_BLOCKED_REASONS['too_long']}（最多 {max_chars} 字符）"
    return True, ""


def diff_prompt_snapshot(snapshot: dict | None) -> list[dict]:
    """把提示词快照变成结构化变更列表（供前端展示「改了什么」）。

    只报告**有差异**的维度：字数变化、变量增删。正文全文不回传前端
    （前端编辑器已有内容，审计页只需要 diff 摘要 + 是否可回滚）。
    """
    if not isinstance(snapshot, dict):
        return []
    changes: list[dict] = []
    b, a = snapshot.get("before") or "", snapshot.get("after") or ""
    if len(b) != len(a):
        changes.append({"field": "length", "label": "字数",
                        "before": len(b), "after": len(a)})
    try:
        # ✅ 2026-09-25（BUG-E · 变量口径不统一）：diff 与路由 / 编辑器一律用
        #   extract_user_variables（排除 {SHARED_*} 运行时解析的共享片段引用）。
        #   旧实现用 extract_variables，会把 SHARED_* 误报成「新增/删除变量」，
        #   与 audit_service.record_prompt_audit 里 before_vars/after_vars
        #   的口径（extract_user_variables）分叉。
        from app.services.ai.prompts._registry import extract_user_variables
        b_vars, a_vars = set(extract_user_variables(b)), set(extract_user_variables(a))
    except Exception:
        b_vars, a_vars = set(), set()
    added, removed = sorted(a_vars - b_vars), sorted(b_vars - a_vars)
    if added:
        changes.append({"field": "added_variables", "label": "新增变量",
                        "before": "", "after": ", ".join(added)})
    if removed:
        changes.append({"field": "removed_variables", "label": "删除变量",
                        "before": ", ".join(removed), "after": ""})
    return changes


async def record_prompt_audit(db, key: str, action: str, before: str, after: str,
                              before_vars: list[str], after_vars: list[str],
                              request: Request | None = None) -> None:
    """写入一条提示词变更审计（指纹 + 变量集合 + 正文快照）。

    由调用方事务提交；本函数不 commit，保证「内容更新 + 审计」原子性。
    快照写入失败时**降级写入无快照行**（宁可丢回滚能力，也不丢审计行）。
    """
    vals = (uuid.uuid4().hex, key, action,
            prompt_content_hash(before), prompt_content_hash(after),
            json.dumps(sorted(set(before_vars)), ensure_ascii=False),
            json.dumps(sorted(set(after_vars)), ensure_ascii=False),
            client_ip_of(request))
    snap = ""
    if getattr(settings, "prompt_audit_snapshot_enabled", True):
        snap = prompt_snapshot_json(before, after)
    if snap:
        try:
            await db.execute(
                "INSERT INTO prompt_audit_logs"
                " (id,prompt_key,action,before_hash,after_hash,"
                "variables_before,variables_after,client_ip,snapshot_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                vals + (snap,))
            return
        except Exception:
            # 旧库尚未 _migrate 出 snapshot_json 列 → 降级写入
            logger.debug("prompt_audit_logs 缺 snapshot_json 列，已降级写入（无回滚能力）")
    await db.execute(
        "INSERT INTO prompt_audit_logs"
        " (id,prompt_key,action,before_hash,after_hash,"
        "variables_before,variables_after,client_ip)"
        " VALUES (?,?,?,?,?,?,?,?)",
        vals)


async def list_prompt_audit_logs(db, key: str, limit: int = 50,
                                 offset: int = 0) -> dict:
    """查询提示词审计记录（指纹、变量集合、结构化 diff、是否可回滚）。"""
    key = (key or "").strip()
    if not key:
        raise ValueError("提示词 key 不能为空")
    limit = max(1, min(200, int(limit or 50)))
    offset = max(0, int(offset or 0))
    cur = await db.execute(
        "SELECT COUNT(*) FROM prompt_audit_logs WHERE prompt_key=?", (key,))
    total = int((await cur.fetchone())[0] or 0)
    cur = await db.execute(
        "SELECT id,prompt_key,action,before_hash,after_hash,variables_before,variables_after,"
        "client_ip,created_at,snapshot_json FROM prompt_audit_logs WHERE prompt_key=?"
        " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?", (key, limit, offset))
    items = []
    for row in await cur.fetchall():
        item = dict(row)
        for field in ("variables_before", "variables_after"):
            try:
                item[field] = json.loads(item.get(field) or "[]")
            except Exception:
                item[field] = []
        item["action_label"] = PROMPT_AUDIT_ACTIONS.get(
            item.get("action", ""), item.get("action", ""))
        # 解析快照生成结构化 diff（历史行无快照 → 空列表，前端按摘要展示）
        snap = {}
        raw = item.get("snapshot_json") or ""
        if raw:
            try:
                snap = json.loads(raw)
            except Exception:
                snap = {}
        item["changes"] = diff_prompt_snapshot(snap)
        # ✅ 2026-09-27（BUG-P1-E）：判据收敛到 prompt_snapshot_is_rollbackable，
        #   与回滚端点共用 —— 保证「按钮可点 ⟺ 回滚必成功」。
        #   附带回传不可回滚原因，前端 tooltip 直接用，无需自己猜文案。
        _ok, _reason = prompt_snapshot_is_rollbackable(snap, PROMPT_MAX_CHARS)
        item["rollbackable"] = _ok
        item["rollback_blocked_reason"] = _reason
        # 快照仅用于服务端生成 diff / 回滚，原始 JSON 不必回传前端（减小载荷）
        item.pop("snapshot_json", None)
        items.append(item)
    return {
        "items": items, "total": total, "limit": limit, "offset": offset,
        "actions": [{"value": k, "label": v} for k, v in PROMPT_AUDIT_ACTIONS.items()],
    }

