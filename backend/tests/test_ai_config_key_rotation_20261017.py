"""主密钥轮转 + 存量密文迁移端点护栏（✅ 2026-10-17 新增）。

覆盖 ``POST /api/v1/ai/key-rotation`` 及其背后的 ``crypto`` 能力接线：
  1. inspect 只读体检：健康度三态、broken 明细、**绝不回传任何密钥材料**；
  2. reencrypt 恢复：坏密文（当前密钥解不开）能被「当前密钥 / legacy_key」重加密
     回来，健康密文原样保留，**绝不把可用密文覆盖成空串/坏值**；
  3. rotate 轮转：备份 + 换密钥 + 同一窗口迁移（对 crypto 真实写盘用 monkeypatch
     隔离），轮转失败 = 零副作用（不写密文、不写审计）；
  4. 静态锁：路由已登记、审计动作已登记、re-export 在位、dry_run 预演与真执行
     判据同源（plan 命中数 == 实际写库数）。
"""

import importlib

import app.routers.ai_config as ai_router
import pytest
from app.models import KeyRotationIn
from app.services.audit_service import CONFIG_ACTIONS
from app.services.crypto import encrypt_api_key

# ⚠️ 必须用 importlib 取「子模块」本体：包 `__init__` 已 `from .key_rotation import key_rotation`
#    （把函数 re-export 到包命名空间），`import app.routers.ai_config.key_rotation as kr`
#    实际绑到的是**同名函数**而非模块（Python `import a.b as c` = `c = getattr(a, 'b')`），
#    导致 monkeypatch 无法触到模块全局。importlib.import_module 才拿到真正的模块对象。
kr = importlib.import_module("app.routers.ai_config.key_rotation")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _insert_cfg(db, cid, key="", provider="openai", model="gpt-4o", raw_cipher=None):
    """插入一条 ai_config 行。``raw_cipher`` 非空时**原样**写该密文（用于造
    「坏密文」—— 非 Fernet 无法解开的形态），否则写 ``encrypt_api_key(key)``。"""
    cipher = raw_cipher if raw_cipher is not None else encrypt_api_key(key)
    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url,"
        " model, is_active, priority, timeout, concurrency)"
        " VALUES (?,?,?,?,?, ?,0,0,60,4)",
        (cid, provider, "pay_as_you_go", cipher, "https://api.test.com/v1", model),
    )
    await db.commit()


def _cipher(db_row_factory):
    """读某配置的 api_key_encrypted（供断言密文是否被改写）。"""

    async def _get(cid):
        cur = await db_row_factory.execute("SELECT api_key_encrypted FROM ai_config WHERE id=?", (cid,))
        return (await cur.fetchone())[0]

    return _get


# ===========================================================================
# 1. inspect（只读体检）
# ===========================================================================
class TestKeyRotationInspect:
    async def test_healthy_row_reports_decryptable(self, db_conn):
        await _insert_cfg(db_conn, "c1", key="sk-healthy")
        res = await ai_router.key_rotation(KeyRotationIn(mode="inspect"), db=db_conn)
        assert res["ok"] is True
        assert res["health"]["decryptable"] == 1
        assert res["health"]["broken_count"] == 0
        assert res["broken_configs"] == []

    async def test_broken_row_is_detected(self, db_conn):
        await _insert_cfg(db_conn, "c1", key="sk-healthy")
        await _insert_cfg(db_conn, "c2", raw_cipher="garbage-not-a-token")
        res = await ai_router.key_rotation(KeyRotationIn(mode="inspect"), db=db_conn)
        assert res["health"]["with_ciphertext"] == 2
        assert res["health"]["decryptable"] == 1
        assert res["health"]["broken_count"] == 1
        assert [b["id"] for b in res["broken_configs"]] == ["c2"]
        assert res["advice"]  # broken 非空必须给出恢复指引

    async def test_response_never_leaks_key_material(self, db_conn):
        await _insert_cfg(db_conn, "c1", key="sk-secret-value")
        res = await ai_router.key_rotation(KeyRotationIn(mode="inspect"), db=db_conn)
        # 响应体任何层级都不得出现密钥明文 / 主密钥 / 密文原文
        flat = str(res)
        assert "sk-secret-value" not in flat
        assert "effective_key" not in res
        assert "api_key" not in res

    async def test_unknown_mode_rejected(self, db_conn):
        """非法 mode 必须 400（不静默执行）。"""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await ai_router.key_rotation(KeyRotationIn(mode="bogus"), db=db_conn)
        assert exc.value.status_code == 400


# ===========================================================================
# 2. reencrypt（坏密文恢复，绝不覆盖可用密文）
# ===========================================================================
class TestKeyRotationReencrypt:
    async def test_unrecoverable_broken_row_is_not_clobbered(self, db_conn):
        """legacy_key 给不出来时，坏密文原样保留（绝不写成空串/坏值）。"""
        await _insert_cfg(db_conn, "c-bad", raw_cipher="garbage")
        get = _cipher(db_conn)
        assert (await get("c-bad")) == "garbage"
        res = await ai_router.key_rotation(KeyRotationIn(mode="reencrypt"), db=db_conn)
        assert res["failed"] == 1 and res["migrated"] == 0
        # 关键不变式：仍无法解开的密文**未被**清空/改写
        assert (await get("c-bad")) == "garbage"

    async def test_healthy_row_untouched(self, db_conn):
        await _insert_cfg(db_conn, "c-ok", key="sk-abc")
        res = await ai_router.key_rotation(KeyRotationIn(mode="reencrypt"), db=db_conn)
        assert res["unchanged"] == 1 and res["failed"] == 0 and res["migrated"] == 0
        get = _cipher(db_conn)
        # 健康密文未被改写（仍由当前密钥可解）
        from app.services.crypto import decrypt_api_key

        assert decrypt_api_key(await get("c-ok"))

    async def test_recovery_via_legacy_writes_back(self, db_conn, monkeypatch):
        """坏密文能被（模拟的）重加密结果替换回可解密形态。"""
        await _insert_cfg(db_conn, "c-bad", raw_cipher="garbage")
        monkeypatch.setattr(kr, "reencrypt_token", lambda enc, legacy="": encrypt_api_key("sk-recovered"))
        res = await ai_router.key_rotation(KeyRotationIn(mode="reencrypt"), db=db_conn)
        assert res["migrated"] == 1 and res["failed"] == 0
        from app.services.crypto import decrypt_api_key

        assert decrypt_api_key(await _cipher(db_conn)("c-bad")) == "sk-recovered"

    async def test_dry_run_does_not_write(self, db_conn, monkeypatch):
        """dry_run 只预演：能迁移的计数命中，但库里密文原样不动。"""
        await _insert_cfg(db_conn, "c-bad", raw_cipher="garbage")
        monkeypatch.setattr(kr, "reencrypt_token", lambda enc, legacy="": encrypt_api_key("sk-recovered"))
        res = await ai_router.key_rotation(KeyRotationIn(mode="reencrypt", dry_run=True), db=db_conn)
        assert res["dry_run"] is True
        assert res["plan"]["migrated"] == 1  # 预演命中
        # 预演不落库：密文仍是原坏值
        assert await _cipher(db_conn)("c-bad") == "garbage"

    async def test_reencrypt_writes_key_rotation_audit(self, db_conn, monkeypatch):
        await _insert_cfg(db_conn, "c-bad", raw_cipher="garbage")
        monkeypatch.setattr(kr, "reencrypt_token", lambda enc, legacy="": encrypt_api_key("sk-recovered"))
        await ai_router.key_rotation(KeyRotationIn(mode="reencrypt"), db=db_conn)
        cur = await db_conn.execute("SELECT action FROM ai_config_audit_logs WHERE action='key_rotation'")
        assert await cur.fetchall()


# ===========================================================================
# 3. rotate（备份 + 换密钥 + 同窗口迁移；对 crypto 写盘用 monkeypatch 隔离）
# ===========================================================================
class TestKeyRotationRotate:
    async def test_rotate_migrates_and_backs_up(self, db_conn, monkeypatch):
        await _insert_cfg(db_conn, "c-bad", raw_cipher="garbage")
        monkeypatch.setattr(
            kr,
            "rotate_master_key",
            lambda new_key="": {
                "ok": True,
                "new_key_from": "generated",
                "backup_path": "secret_key.key.bak-20261017",
                "error": "",
            },
        )
        monkeypatch.setattr(kr, "reencrypt_token", lambda enc, legacy="": encrypt_api_key("sk-migrated"))
        res = await ai_router.key_rotation(KeyRotationIn(mode="rotate"), db=db_conn)
        assert res["ok"] is True and res["new_key_from"] == "generated"
        assert res["backup_path"] == "secret_key.key.bak-20261017"
        assert res["migrated"] == 1
        from app.services.crypto import decrypt_api_key

        assert decrypt_api_key(await _cipher(db_conn)("c-bad")) == "sk-migrated"

    async def test_rotate_failure_has_zero_side_effects(self, db_conn, monkeypatch):
        """轮转失败（新密钥非法 / 写文件失败）：旧密钥文件不动、不迁移、不写审计。"""
        await _insert_cfg(db_conn, "c-bad", raw_cipher="garbage")
        monkeypatch.setattr(
            kr,
            "rotate_master_key",
            lambda new_key="": {
                "ok": False,
                "new_key_from": "",
                "backup_path": "",
                "error": "new_key 不是合法的 Fernet 密钥",
            },
        )
        res = await ai_router.key_rotation(KeyRotationIn(mode="rotate"), db=db_conn)
        assert res["ok"] is False and "error" in res
        # 零副作用：密文原样、没有 key_rotation 审计
        assert await _cipher(db_conn)("c-bad") == "garbage"
        cur = await db_conn.execute("SELECT COUNT(*) FROM ai_config_audit_logs WHERE action='key_rotation'")
        assert int((await cur.fetchone())[0]) == 0

    async def test_env_source_warns_to_update_envvar(self, db_conn, monkeypatch):
        """当前主密钥来自 FERNET_KEY 环境变量时，轮转文件不影响生效密钥 → 必须提示。"""
        await _insert_cfg(db_conn, "c-bad", raw_cipher="garbage")
        monkeypatch.setattr(
            kr, "master_key_source", lambda: {"source": "env", "key_file_exists": True, "effective_key": "envkey"}
        )
        monkeypatch.setattr(
            kr,
            "rotate_master_key",
            lambda new_key="": {"ok": True, "new_key_from": "generated", "backup_path": "", "error": ""},
        )
        monkeypatch.setattr(kr, "reencrypt_token", lambda enc, legacy="": encrypt_api_key("sk-x"))
        res = await ai_router.key_rotation(KeyRotationIn(mode="rotate"), db=db_conn)
        assert res["ok"] is True
        assert "FERNET_KEY" in res["warning"]  # 提示「环境变量仍优先，请同步更新」


# ===========================================================================
# 4. 静态锁（路由 / 审计动作 / 结构）
# ===========================================================================
class TestKeyRotationStatic:
    def test_route_is_registered_on_ai_router(self):
        # 子路由挂载 + 包级前缀 = 最终 /api/v1/ai/key-rotation。
        # （不用 `ai_router.router.routes` 展开：新版 FastAPI 的 include_router
        #   以 _IncludedRouter 承载，顶层 .routes 拿不到扁平路径。）
        sub_paths = [getattr(r, "path", "") for r in kr.router.routes]
        assert "/key-rotation" in sub_paths
        assert ai_router.router.prefix == "/api/v1/ai"
        # 且父路由确实接入了本子路由（否则端点不会真正可访问）。
        # 新版 FastAPI 的 include_router 以 _IncludedRouter 承载，其子路由在
        # ``original_router`` 属性里（兼容旧版直接暴露 .router 的形态）。
        included = [getattr(r, "original_router", None) or getattr(r, "router", None) for r in ai_router.router.routes]
        assert kr.router in included

    def test_audit_action_registered(self):
        assert "key_rotation" in CONFIG_ACTIONS

    def test_function_reexported_from_package(self):
        assert callable(ai_router.key_rotation)
        assert ai_router.key_rotation is kr.key_rotation
