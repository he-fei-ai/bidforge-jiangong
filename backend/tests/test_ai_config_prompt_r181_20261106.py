"""R181（2026-11-06）AI 配置 / 提示词 / 结构自检 遗留项回归。

覆盖本轮 9 项中可在后端闭环的部分：
  * `_load_active_config` 过滤「is_active=1 且 enabled=0」脏行；
  * 矛盾配置归一端点（可重入 / 审计 / 失败回滚）；
  * 启动期 schema 自检（缺表、版本过高）与 `/health` 诊断出口；
  * 未注册提示词 key 计入 render_errors 且仍返回空串；
  * `__VAR__` 独占行参与可选区块删除；
  * 非字符串变量（数字 / 布尔 / None / 列表）统一渲染形态。

变异判别：下列断言在「移除过滤 / 移除计数 / 退回 str() / 只认 {var}」等
旧实现下必然变红。
"""

from __future__ import annotations

import aiosqlite
import app.services.ai.provider_factory as pf
import pytest
from app.db import check_schema_integrity, schema_status
from app.routers.ai_config import config as ai_config_router
from app.services.ai.prompts import _metrics, get_prompt, render_prompt
from app.services.ai.prompts._registry import _format_render_value
from fastapi import HTTPException


async def _insert_config(
    db,
    cid: str,
    *,
    is_active: int,
    enabled: int,
    updated_at: str,
    provider: str = "deepseek",
    model: str = "deepseek-chat",
    base_url: str = "https://api.example.com/v1",
) -> None:
    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url,"
        " model, env, is_active, enabled, priority, concurrency, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, provider, "pay_as_you_go", "enc", base_url, model, "", is_active, enabled, 10, 4, updated_at),
    )
    await db.commit()


# ===========================================================================
# 1. 读时过滤脏行
# ===========================================================================
class TestActiveConfigDirtyRowFilter:
    async def test_contradictory_active_row_is_ignored(self, db_conn):
        """只有 is_active=1 & enabled=0 的行时，必须视为「无可用主配置」。

        旧实现（WHERE is_active=1）会返回该行 → 已停用配置继续被真实调用。
        """
        pf.invalidate_config_cache()
        await _insert_config(db_conn, "dirty", is_active=1, enabled=0, updated_at="2026-11-06T01:00:00")
        assert await pf._load_active_config() is None

    async def test_clean_active_row_still_selected(self, db_conn):
        """正例：干净主配置照常可用（过滤不能误伤 enabled=1）。"""
        pf.invalidate_config_cache()
        await _insert_config(db_conn, "clean", is_active=1, enabled=1, updated_at="2026-11-06T01:00:00")
        cfg = await pf._load_active_config()
        assert cfg is not None
        assert cfg["id"] == "clean"


# ===========================================================================
# 2. 归一迁移端点：可重入 / 审计 / 失败回滚
# ===========================================================================
class TestRepairConsistencyEndpoint:
    async def test_normalizes_and_is_idempotent(self, db_conn):
        await _insert_config(db_conn, "dirty", is_active=1, enabled=0, updated_at="2026-11-06T01:00:00")
        await _insert_config(db_conn, "off", is_active=0, enabled=1, updated_at="2026-11-06T00:30:00")

        first = await ai_config_router.repair_config_consistency(request=None, db=db_conn)
        assert first["updated_rows"] == 1
        assert first["unchanged"] is False
        assert first["rows"] == [
            {
                "id": "dirty",
                "provider_name": "deepseek",
                "model": "deepseek-chat",
                "env": "",
                "before": {"is_active": 1, "enabled": 0},
                "after": {"is_active": 1, "enabled": 1},
            }
        ]

        row = await (await db_conn.execute("SELECT enabled FROM ai_config WHERE id='dirty'")).fetchone()
        assert int(row[0]) == 1
        # 非目标行（is_active=0）绝不被改动
        off = await (await db_conn.execute("SELECT enabled, is_active FROM ai_config WHERE id='off'")).fetchone()
        assert (int(off[0]), int(off[1])) == (1, 0)

        audit = await (
            await db_conn.execute(
                "SELECT action, config_id, detail FROM ai_config_audit_logs WHERE action='consistency_repair'"
            )
        ).fetchall()
        assert len(audit) == 1
        assert audit[0]["config_id"] == "dirty"
        assert "enabled=0 → enabled=1" in audit[0]["detail"]
        assert "enc" not in audit[0]["detail"]  # 不含密钥材料

        second = await ai_config_router.repair_config_consistency(request=None, db=db_conn)
        assert second["updated_rows"] == 0
        assert second["unchanged"] is True
        assert second["rows"] == []
        audit2 = await (
            await db_conn.execute("SELECT COUNT(*) FROM ai_config_audit_logs WHERE action='consistency_repair'")
        ).fetchone()
        assert int(audit2[0]) == 1  # 幂等重跑不产生重复审计

    async def test_no_dirty_rows_is_noop(self, db_conn):
        result = await ai_config_router.repair_config_consistency(request=None, db=db_conn)
        assert result == {
            "ok": True,
            "updated_rows": 0,
            "unchanged": True,
            "rows": [],
            "message": "未发现「is_active=1 且 enabled=0」的矛盾配置（幂等重跑）",
        }

    async def test_commit_failure_rolls_back_and_raises(self, db_conn, monkeypatch):
        await _insert_config(db_conn, "dirty", is_active=1, enabled=0, updated_at="2026-11-06T01:00:00")

        async def _boom():
            raise RuntimeError("commit boom")

        monkeypatch.setattr(db_conn, "commit", _boom)
        with pytest.raises(HTTPException) as ei:
            await ai_config_router.repair_config_consistency(request=None, db=db_conn)
        assert ei.value.status_code == 500
        assert "已回滚" in str(ei.value.detail)

        row = await (await db_conn.execute("SELECT enabled FROM ai_config WHERE id='dirty'")).fetchone()
        assert int(row[0]) == 0  # 回滚生效，用户数据未被半途修改


# ===========================================================================
# 3. 启动期 schema 自检（N-10）
# ===========================================================================
class TestSchemaIntegrity:
    async def test_full_schema_is_ok(self, db_conn):
        status = await check_schema_integrity()
        assert status["ok"] is True
        assert status["count"] == 0
        assert schema_status()["ok"] is True

    async def test_missing_critical_table_is_reported(self, db_conn):
        await db_conn.execute("DROP TABLE ai_scene_routes")
        await db_conn.commit()
        status = await check_schema_integrity()
        assert status["ok"] is False
        assert any(i["scope"] == "table:ai_scene_routes" for i in status["issues"])
        # fail-soft：函数本身不抛异常，诊断出口可读
        assert schema_status()["count"] >= 1

    async def test_missing_required_column_is_reported(self, db_conn):
        # 等价于「外部改库」删掉了关键列：重建一张缺列的同名表
        await db_conn.execute("ALTER TABLE ai_runtime_settings RENAME TO ai_runtime_settings_bak")
        await db_conn.execute("CREATE TABLE ai_runtime_settings (key TEXT PRIMARY KEY)")
        await db_conn.commit()
        status = await check_schema_integrity()
        assert status["ok"] is False
        assert any("缺少必需列" in i["detail"] for i in status["issues"])

    async def test_newer_schema_version_is_reported(self, db_conn):
        await db_conn.execute("PRAGMA user_version = 99")
        status = await check_schema_integrity()
        assert status["ok"] is False
        assert any(i["scope"] == "schema_version" for i in status["issues"])


# ===========================================================================
# 4. 提示词模块遗留项
# ===========================================================================
class TestPromptLeftovers:
    @pytest.fixture(autouse=True)
    def _isolate_metrics(self):
        _metrics.reset()
        yield
        _metrics.reset()

    async def test_unknown_key_returns_empty_but_counts_error(self):
        key = "__R181_UNKNOWN_PROMPT_KEY__"
        assert get_prompt(key) == ""
        assert _metrics.render_errors[key] == 1
        assert _metrics.render_total.get(key, 0) == 0

    def test_underscore_optional_block_deleted_when_absent(self):
        tpl = "前置说明\n__OPTIONAL_BLOCK__\n后续说明"
        out = render_prompt(tpl)
        assert out == "前置说明\n后续说明"
        assert "__OPTIONAL_BLOCK__" not in out

    def test_underscore_optional_block_kept_when_present(self):
        tpl = "前置说明\n__OPTIONAL_BLOCK__\n后续说明"
        out = render_prompt(tpl, OPTIONAL_BLOCK="补丁内容")
        assert out == "前置说明\n补丁内容\n后续说明"

    def test_brace_optional_block_semantics_unchanged(self):
        tpl = "前置说明\n{OPTIONAL}\n后续说明"
        assert render_prompt(tpl) == "前置说明\n后续说明"
        assert render_prompt(tpl, OPTIONAL="补丁内容") == "前置说明\n补丁内容\n后续说明"

    def test_non_string_values_render_uniformly(self):
        assert render_prompt("{v}", v=3) == "3"
        assert render_prompt("{v}", v=True) == "true"
        assert render_prompt("{v}", v=False) == "false"
        assert render_prompt("{v}", v=["甲", "乙"]) == "甲、乙"
        assert render_prompt("{v}", v=("甲", "乙")) == "甲、乙"
        assert render_prompt("{v}", v={"k": "v"}) == '{"k": "v"}'
        # None 语义 =「未提供」：独占整行时整行丢弃（保留既有可选区块契约）
        assert render_prompt("{v}", v=None) == ""
        # 行内占位符仍原样保留，供上层变量合同校验发现缺失
        assert render_prompt("前缀{v}后缀", v=None) == "前缀{v}后缀"
        assert _format_render_value(None) == ""
