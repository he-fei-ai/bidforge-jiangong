"""文本模型配置模块 · 复制配置端点回归（2026-11-09）。

覆盖 duplicate_config：
- 基本复制（参数/密钥密文/不抢占激活/priority 置后/审计落库且跨查询可见）
- 源不存在 → 404
- env 覆盖（多环境友好，源环境不变）
- 不抢占「当前使用」
- 非法 env → 400
"""

import pytest
from app.models import DuplicateConfigIn
from app.routers import ai_config as ai_router
from app.services.crypto import encrypt_api_key


async def _insert_source(db_conn, **over):
    pid = over.pop("id", "src1")
    vals = {
        "id": pid,
        "provider_name": "deepseek",
        "plan": "pay_as_you_go",
        "api_key_encrypted": encrypt_api_key("sk-source"),
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "max_tokens": 8192,
        "temperature": 0.7,
        "timeout": 900,
        "concurrency": 4,
        "request_mode": "normal",
        "env": "",
        "is_active": 1,
        "enabled": 1,
        "priority": 0,
        "remark": "源备注",
    }
    vals.update(over)
    cols = ", ".join(vals.keys())
    placeholders = ", ".join("?" for _ in vals)
    await db_conn.execute(
        f"INSERT INTO ai_config ({cols}) VALUES ({placeholders})",
        list(vals.values()),
    )
    await db_conn.commit()
    return pid


async def test_duplicate_config_basic(db_conn):
    src = await _insert_source(db_conn)
    res = await ai_router.duplicate_config(
        DuplicateConfigIn(source_id=src), db=db_conn
    )
    assert res["ok"] is True
    assert res["id"] != src

    cur = await db_conn.execute("SELECT * FROM ai_config WHERE id=?", (res["id"],))
    new = dict(await cur.fetchone())
    # 参数原样复制
    assert new["provider_name"] == "deepseek"
    assert new["model"] == "deepseek-chat"
    assert new["max_tokens"] == 8192
    assert new["request_mode"] == "normal"
    # 密钥以密文直接复制（不解密、不暴露明文）
    cur_src = await db_conn.execute(
        "SELECT api_key_encrypted FROM ai_config WHERE id=?", (src,)
    )
    assert new["api_key_encrypted"] == (await cur_src.fetchone())[0]
    assert new["api_key_encrypted"]  # 非空的密文
    # 复制体不抢占「当前使用」
    assert new["is_active"] == 0
    # priority 置于现有最大值之后
    assert new["priority"] > 0
    # 审计落库且跨查询可见
    listing = await ai_router.config_audit_logs(db=db_conn)
    assert any(i["config_id"] == res["id"] and i["action"] == "duplicate" for i in listing["items"])


async def test_duplicate_config_source_not_found(db_conn):
    with pytest.raises(Exception) as exc:
        await ai_router.duplicate_config(
            DuplicateConfigIn(source_id="nope"), db=db_conn
        )
    assert exc.value.status_code == 404


async def test_duplicate_config_env_override(db_conn):
    src = await _insert_source(db_conn, env="dev")
    res = await ai_router.duplicate_config(
        DuplicateConfigIn(source_id=src, env="prod"), db=db_conn
    )
    cur = await db_conn.execute("SELECT env FROM ai_config WHERE id=?", (res["id"],))
    assert (await cur.fetchone())[0] == "prod"
    # 源环境不变
    cur_src = await db_conn.execute("SELECT env FROM ai_config WHERE id=?", (src,))
    assert (await cur_src.fetchone())[0] == "dev"


async def test_duplicate_config_remark_override(db_conn):
    src = await _insert_source(db_conn, remark="源备注")
    res = await ai_router.duplicate_config(
        DuplicateConfigIn(source_id=src, remark="新备注"), db=db_conn
    )
    cur = await db_conn.execute("SELECT remark FROM ai_config WHERE id=?", (res["id"],))
    assert (await cur.fetchone())[0] == "新备注"


async def test_duplicate_config_does_not_displace_active(db_conn):
    src = await _insert_source(db_conn, is_active=1)
    await ai_router.duplicate_config(DuplicateConfigIn(source_id=src), db=db_conn)
    # 源仍是当前使用配置
    cur = await db_conn.execute("SELECT COUNT(*) FROM ai_config WHERE is_active=1 AND id=?", (src,))
    assert (await cur.fetchone())[0] == 1


async def test_duplicate_config_invalid_env(db_conn):
    src = await _insert_source(db_conn)
    with pytest.raises(Exception) as exc:
        await ai_router.duplicate_config(
            DuplicateConfigIn(source_id=src, env="bad!!env"), db=db_conn
        )
    assert exc.value.status_code == 400
