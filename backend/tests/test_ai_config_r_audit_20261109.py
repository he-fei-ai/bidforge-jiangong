"""文本模型配置模块 · 本轮深度审计修复项回归（2026-11-09）。

覆盖：
1. save_config 写审计后必须提交路由连接 —— 修复前该审计在生产环境被 get_db
   的 finally rollback 静默回滚丢失（既有单测靠「同一连接读未提交事务」假绿）。
2. save_config 的 duplicate_hint 必须按 env 去重 —— 不同环境同三元组不应误报重复。
3. import_config 的覆盖/去重必须按 env 隔离 —— 多环境迁移时不应把 prod 导入
   覆盖到 dev 配置（反之亦然）。
"""

import pytest
from app.models import AIConfigIn, ConfigImportIn
from app.routers import ai_config as ai_router
from app.services.crypto import encrypt_api_key


def _base_payload(**over):
    data = {
        "id": "",
        "provider_name": "deepseek",
        "plan": "pay_as_you_go",
        "api_key": "sk-abc",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "max_tokens": 8192,
        "temperature": 0.7,
        "timeout": 900,
        "concurrency": 4,
        "is_active": True,
        "priority": None,
        "remark": "",
    }
    data.update(over)
    return data


async def test_save_config_audit_persists_after_route_rollback(db_conn):
    """save_config 写审计后必须提交；否则生产环境 get_db 的 finally rollback 会
    把审计行静默回滚丢失。本测试在 save_config 返回后显式 rollback 模拟该路径。"""
    res = await ai_router.save_config(AIConfigIn(**_base_payload(id="", env="prod")), db=db_conn)
    assert res["ok"] is True
    # 模拟 get_db 依赖在请求结束时对路由连接做的 rollback（生产真实行为）
    await db_conn.rollback()
    cur = await db_conn.execute(
        "SELECT COUNT(*) FROM ai_config_audit_logs WHERE action='create' AND config_id=?",
        (res["id"],),
    )
    assert (await cur.fetchone())[0] >= 1, "配置变更审计未提交，生产环境会被回滚丢失"


async def test_save_config_audit_visible_via_listing(db_conn):
    """审计落库后应能被 config_audit_logs 读到（跨查询可见）。"""
    res = await ai_router.save_config(AIConfigIn(**_base_payload(id="", env="prod")), db=db_conn)
    listing = await ai_router.config_audit_logs(db=db_conn)
    assert any(i["config_id"] == res["id"] and i["action"] == "create" for i in listing["items"])


async def test_save_duplicate_hint_ignores_other_env(db_conn):
    """dev 环境已有同三元组配置时，在 prod 环境保存同三元组不应提示重复。"""
    await db_conn.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url, model,"
        " is_active, enabled, env) VALUES (?,?,?,?,?,?,0,1,'dev')",
        ("dev1", "deepseek", "pay_as_you_go", encrypt_api_key("sk-dev"),
         "https://api.deepseek.com/v1", "deepseek-chat"),
    )
    await db_conn.commit()

    res_prod = await ai_router.save_config(
        AIConfigIn(**_base_payload(id="", env="prod")), db=db_conn
    )
    assert res_prod["duplicate_hint"] == "", res_prod

    res_dev = await ai_router.save_config(
        AIConfigIn(**_base_payload(id="", env="dev")), db=db_conn
    )
    assert res_dev["duplicate_hint"], res_dev


async def test_import_overwrite_respects_env(db_conn):
    """导入 prod 环境同三元组（overwrite=True）应新建，不覆盖 dev 配置。"""
    await db_conn.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url, model,"
        " remark, is_active, enabled, env) VALUES (?,?,?,?,?,?,'原dev',0,1,'dev')",
        ("dev1", "deepseek", "pay_as_you_go", encrypt_api_key("sk-dev"),
         "https://api.deepseek.com/v1", "deepseek-chat"),
    )
    await db_conn.commit()

    body = ConfigImportIn(
        items=[{
            "provider_name": "deepseek",
            "plan": "pay_as_you_go",
            "api_key": "",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "remark": "导入prod",
            "env": "prod",
        }],
        overwrite=True,
    )
    res = await ai_router.import_config(body, db=db_conn)
    assert res["imported"] == 1

    cur = await db_conn.execute(
        "SELECT COUNT(*) FROM ai_config WHERE env='dev' AND remark='原dev'"
    )
    assert (await cur.fetchone())[0] == 1, "dev 配置不应被 prod 导入覆盖"
    cur = await db_conn.execute("SELECT COUNT(*) FROM ai_config WHERE env='prod'")
    assert (await cur.fetchone())[0] == 1

    # 控制：导入 dev 环境同三元组（overwrite=True）应覆盖 dev 配置
    body2 = ConfigImportIn(
        items=[{
            "provider_name": "deepseek",
            "plan": "pay_as_you_go",
            "api_key": "",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "remark": "覆盖dev",
            "env": "dev",
        }],
        overwrite=True,
    )
    res2 = await ai_router.import_config(body2, db=db_conn)
    assert res2["imported"] == 1
    cur = await db_conn.execute(
        "SELECT COUNT(*) FROM ai_config WHERE env='dev' AND remark='覆盖dev'"
    )
    assert (await cur.fetchone())[0] == 1
