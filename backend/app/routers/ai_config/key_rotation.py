"""AI 配置路由 · 主密钥轮转与存量密文迁移（✅ 2026-10-17 新增）。

补齐「密钥轮转」能力缺口：``crypto`` 里的 ``rotate_master_key`` /
``reencrypt_token`` / ``decrypt_with_legacy_key`` / ``backup_master_key`` 此前在
``app/`` 全仓**零调用点**（纯函数死代码）——合规要求定期轮换 at-rest 主密钥，
而手动换 ``FERNET_KEY`` / 删 ``data/secret_key.key`` 后，库里全部
``ai_config.api_key_encrypted`` 会瞬间变「key_broken（解不开）」，用户此前唯一
的恢复手段是逐条重填 API Key。本端点把「轮转 + 存量密文一次性迁移」收敛成
**单个维护动作**，全程 fail-soft、**绝不删除/清空任何可用密文**。

三个 mode：
  * ``inspect``   只读体检（主密钥来源 + 存量密钥健康度），零写入、零密钥改动；
  * ``reencrypt`` 在「当前密钥（优先）或传入 ``legacy_key``」下重加密存量密文，
                  只修复「解不开」的行，健康行不动（避免无谓 churn）；
  * ``rotate``    备份旧密钥 → 换新密钥 → 同一维护窗口内用「轮转前密钥」迁移全部
                  存量密文（一次动作完成「合规轮换 + 数据不失效」）。

安全不变式（``tests/test_ai_config_key_rotation_*`` 锁定）：
  * 任何一次重加密**只有当新密文可回解**（``decrypt_api_key`` 非空）时才写回，
    绝不把可用密文覆盖成空串/坏值；健康密文一律原样保留；
  * ``rotate`` 写密钥文件失败时旧密钥文件原样保留（``rotate_master_key`` 已保证），
    此时**不做任何**迁移、零副作用；
  * 审计 detail 只写「来源 + 迁移/不变/失败计数」，**绝不含任何密钥材料**；
  * 面向前端的响应体只回传来源标签、健康计数与配置 id/名称，**不回传
    ``effective_key`` / 明文 / 密文**。
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from app.db import get_db
from app.models import KeyRotationIn
from app.services.ai.provider_factory import invalidate_config_cache
from app.services.crypto import (
    decrypt_api_key,
    master_key_source,
    reencrypt_token,
    rotate_master_key,
)

from .audit import record_config_audit

router = APIRouter(tags=["ai_config"])

_VALID_MODES = ("inspect", "reencrypt", "rotate")


async def _row_health(db) -> dict:
    """统计 ``ai_config.api_key_encrypted`` 的三态健康度（只读，不回传密钥材料）。

    返回::

        {"total": int, "with_ciphertext": int, "decryptable": int,
         "broken": [{"id","provider_name","model"}, ...]}

    「broken」= 密文非空但 ``decrypt_api_key`` 解不出（换过 FERNET_KEY / 删过
    secret_key.key）。仅回传配置标识，**不含**密文或任何密钥片段。
    """
    cur = await db.execute(
        "SELECT id, provider_name, model, api_key_encrypted FROM ai_config"
        " ORDER BY priority ASC, updated_at DESC")
    rows = [dict(r) for r in await cur.fetchall()]
    total = len(rows)
    with_ct = 0
    decryptable = 0
    broken: list[dict] = []
    for r in rows:
        enc = (r.get("api_key_encrypted") or "").strip()
        if not enc:
            continue
        with_ct += 1
        if decrypt_api_key(enc):
            decryptable += 1
        else:
            broken.append({"id": r["id"], "provider_name": r["provider_name"],
                           "model": r["model"]})
    return {"total": total, "with_ciphertext": with_ct,
            "decryptable": decryptable, "broken": broken}


def _health_summary(health: dict) -> dict:
    """把 :func:`_row_health` 的结果压成只含计数的可回传摘要（不回传 broken 明细）。"""
    return {
        "total": health["total"],
        "with_ciphertext": health["with_ciphertext"],
        "decryptable": health["decryptable"],
        "broken_count": len(health["broken"]),
    }


async def _reencrypt_scan(db, legacy_key: str) -> dict:
    """**只计算、不写库**：统计在「当前密钥 / legacy_key」下存量密文的重加密结果。

    判据（与 :func:`_reencrypt_apply` 严格同口径，保证「预演 = 真执行」）：
      * 健康密文（``decrypt_api_key`` 非空）→ 计入 ``unchanged``（不动，避免 churn）；
      * 坏密文 → 用 ``reencrypt_token(enc, legacy_key)`` 尝试重加密，能回解且变体
        才计 ``migrated``，否则 ``failed``。
    """
    cur = await db.execute(
        "SELECT id, api_key_encrypted FROM ai_config WHERE api_key_encrypted != ''")
    rows = [dict(r) for r in await cur.fetchall()]
    migrated = unchanged = failed = 0
    for r in rows:
        enc = (r.get("api_key_encrypted") or "").strip()
        if decrypt_api_key(enc):
            unchanged += 1
            continue
        new = reencrypt_token(enc, legacy_key)
        if new and new != enc and decrypt_api_key(new):
            migrated += 1
        else:
            failed += 1
    return {"scanned": len(rows), "migrated": migrated,
            "unchanged": unchanged, "failed": failed}


async def _reencrypt_apply(db, legacy_key: str) -> dict:
    """**真正写库**重加密存量密文（判据与 :func:`_reencrypt_scan` 逐字同源）。

    安全不变式：只写回「可回解」的新密文，且**只动坏密文**（健康行原样保留）；
    单条失败不影响其它条，也不回滚整批（每条独立 UPDATE，最终统一 commit）。
    """
    cur = await db.execute(
        "SELECT id, api_key_encrypted FROM ai_config WHERE api_key_encrypted != ''")
    rows = [dict(r) for r in await cur.fetchall()]
    migrated = unchanged = failed = 0
    for r in rows:
        enc = (r.get("api_key_encrypted") or "").strip()
        if decrypt_api_key(enc):
            unchanged += 1
            continue
        new = reencrypt_token(enc, legacy_key)
        if new and new != enc and decrypt_api_key(new):
            await db.execute(
                "UPDATE ai_config SET api_key_encrypted=? WHERE id=?", (new, r["id"]))
            migrated += 1
        else:
            failed += 1
    return {"scanned": len(rows), "migrated": migrated,
            "unchanged": unchanged, "failed": failed}


@router.post("/key-rotation")
async def key_rotation(body: KeyRotationIn, request: Request = None, db=Depends(get_db)):
    """主密钥轮转 / 存量密文迁移（默认 ``inspect`` 只读，无副作用）。

    语义与 ``dry_run``：
      * ``mode=inspect``：零写入。回传主密钥来源（env/persisted_file/temporary）+
        存量密钥健康度（total/with_ciphertext/decryptable/broken 明细），
        broken 非空时给出一句话恢复指引。
      * ``mode=reencrypt``：把「解不开」的存量密文用当前密钥 / ``legacy_key`` 重加密
        回来；健康密文不动。``dry_run=True`` 只预演不写库。
      * ``mode=rotate``：备份旧密钥 → 换新密钥（``new_key`` 空则自动生成）→ 用
        「轮转前生效密钥」作为 legacy 迁移全部存量密文。``dry_run=True`` 只预演。
        轮转失败（写密钥文件失败 / 新密钥非法）时**零副作用**（旧密钥文件原样保留）。

    响应体**绝不含**主密钥明文、Fernet 密文或 API Key（只含来源标签与计数）。
    """
    mode = (body.mode or "inspect").strip().lower()
    if mode not in _VALID_MODES:
        raise HTTPException(400, f"mode 必须是 {list(_VALID_MODES)}（收到 {mode!r}）")
    legacy_key = (body.legacy_key or "").strip()

    src = master_key_source()
    health = await _row_health(db)

    # ---- 只读体检：零写入、零密钥改动 --------------------------------------
    if mode == "inspect":
        advice = ""
        if health["broken"]:
            advice = (f"{len(health['broken'])} 条已保存密钥无法解密（常见于更换过 "
                      "FERNET_KEY 或 secret_key.key 被删）。可用 mode=reencrypt"
                      "（必要时带 legacy_key）一次性重加密恢复，无需逐条重填 Key。")
        return {
            "ok": True,
            "mode": "inspect",
            "master_key_source": src["source"],
            "key_file_exists": src["key_file_exists"],
            "health": _health_summary(health),
            "broken_configs": health["broken"],
            "advice": advice,
        }

    # ---- reencrypt / rotate（写入型；dry_run 只预演）-----------------------
    if body.dry_run:
        plan = await _reencrypt_scan(db, legacy_key or src["effective_key"])
        return {
            "ok": True,
            "mode": mode,
            "dry_run": True,
            "master_key_source": src["source"],
            "health": _health_summary(health),
            "plan": plan,
            "hint": "dry_run 未写库、未轮转；确认预演结果后去掉 dry_run 再执行。",
        }

    if mode == "reencrypt":
        res = await _reencrypt_apply(db, legacy_key)
        await record_config_audit(
            db, "key_rotation",
            detail=(f"主密钥重加密（来源={src['source']}）：迁移 {res['migrated']} 条、"
                    f"健康不变 {res['unchanged']} 条、失败 {res['failed']} 条"),
            request=request, commit=False)
        await db.commit()
        invalidate_config_cache()
        warning = ""
        if res["failed"]:
            warning = (f"{res['failed']} 条密文无法重加密（当前密钥与 legacy_key 都解不开）。"
                       "这些配置需重新手动填写 API Key。")
        return {"ok": True, "mode": "reencrypt",
                "migrated": res["migrated"], "unchanged": res["unchanged"],
                "failed": res["failed"], "warning": warning}

    # ---- rotate：备份 → 换密钥 → 同窗口迁移 --------------------------------
    legacy_before = src["effective_key"]  # 轮转前生效主密钥（作为迁移 legacy）
    rot = rotate_master_key((body.new_key or "").strip())
    if not rot["ok"]:
        # 轮转失败：密钥文件原样保留、未写任何密文 → 零副作用
        return {"ok": False, "mode": "rotate", "error": rot["error"],
                "backup_path": rot["backup_path"],
                "hint": "主密钥文件未变，未迁移任何密文；请修正后重试。"}
    use_legacy = legacy_key or legacy_before
    res = await _reencrypt_apply(db, use_legacy)
    await record_config_audit(
        db, "key_rotation",
        detail=(f"主密钥轮转（来源={rot['new_key_from']}）：备份={rot['backup_path'] or '无'}、"
                f"迁移 {res['migrated']} 条、不变 {res['unchanged']} 条、失败 {res['failed']} 条"),
        request=request, commit=False)
    await db.commit()
    invalidate_config_cache()
    warning = ""
    if res["failed"]:
        warning = (f"主密钥已轮转，但 {res['failed']} 条密文未能迁移（legacy 密钥不可用）。"
                   "这些配置需重新手动填写 API Key。")
    elif src["source"] == "env":
        warning = ("当前主密钥由 FERNET_KEY 环境变量控制：轮转持久化文件不影响生效密钥"
                   "（环境变量仍优先），请同步在部署层更新 FERNET_KEY 后重启。")
    return {"ok": True, "mode": "rotate",
            "new_key_from": rot["new_key_from"], "backup_path": rot["backup_path"],
            "migrated": res["migrated"], "unchanged": res["unchanged"],
            "failed": res["failed"], "warning": warning}
