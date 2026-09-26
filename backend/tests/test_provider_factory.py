"""provider_factory.py 单元测试

覆盖 PB-4 性能优化：_load_active_config 5 分钟 TTL 缓存

测试策略：
- 直接调用 _load_active_config，验证缓存命中/过期/无配置三种路径
- 通过操作 _config_cache["ts"] 模拟时间流逝，避免 mock time.time
- 验证 DB 修改后缓存未过期时返回旧值（缓存生效）
- 验证缓存过期后返回新值（重新查库）
"""
import pytest

import app.services.ai.provider_factory as pf
from app.services.ai.provider_factory import (
    _load_active_config,
    _config_cache,
    _config_cache_ttl,
)


# ============================================================
# 辅助函数
# ============================================================
async def _insert_ai_config(db, cid="cfg1", provider="openai", is_active=1,
                            api_key="sk-test", base_url="https://api.test.com/v1",
                            model="gpt-4o"):
    """插入一条 ai_config 记录"""
    await db.execute(
        "INSERT INTO ai_config (id, provider_name, api_key_encrypted, base_url,"
        " model, is_active) VALUES (?,?,?,?,?,?)",
        (cid, provider, api_key, base_url, model, is_active))
    await db.commit()


# ============================================================
# _load_active_config 缓存逻辑测试
# ============================================================
class TestLoadActiveConfig:
    """_load_active_config 的 TTL 缓存逻辑"""

    async def test_no_active_config_returns_none(self, db_conn):
        """无活跃配置：返回 None，缓存写入 None"""
        result = await _load_active_config()
        assert result is None
        # 缓存被写入 None + 当前时间戳
        assert _config_cache["data"] is None
        assert _config_cache["ts"] > 0

    async def test_first_call_reads_from_db(self, db_conn):
        """首次调用：从 DB 读取活跃配置"""
        await _insert_ai_config(db_conn, cid="cfg1", provider="openai")

        cfg = await _load_active_config()
        assert cfg is not None
        assert cfg["id"] == "cfg1"
        assert cfg["provider_name"] == "openai"
        # 缓存已写入
        assert _config_cache["data"] == cfg

    async def test_cache_hit_within_ttl(self, db_conn):
        """TTL 内再次调用：返回缓存，不查 DB

        验证方式：首次调用后修改 DB，再次调用应返回旧值（缓存命中）。
        """
        await _insert_ai_config(db_conn, cid="cfg1", provider="openai")

        cfg1 = await _load_active_config()
        assert cfg1["provider_name"] == "openai"

        # 修改 DB（但缓存未过期）
        await db_conn.execute(
            "UPDATE ai_config SET provider_name=? WHERE id=?",
            ("deepseek", "cfg1"))
        await db_conn.commit()

        cfg2 = await _load_active_config()
        # 仍返回缓存的旧值
        assert cfg2 is cfg1  # 同一对象引用
        assert cfg2["provider_name"] == "openai"

    async def test_cache_expiry_reloads_from_db(self, db_conn):
        """TTL 过期后：重新从 DB 读取

        验证方式：首次调用后，手动将 _config_cache["ts"] 置 0
        模拟缓存过期，再修改 DB，再次调用应返回新值。
        """
        await _insert_ai_config(db_conn, cid="cfg1", provider="openai")

        cfg1 = await _load_active_config()
        assert cfg1["provider_name"] == "openai"

        # 修改 DB
        await db_conn.execute(
            "UPDATE ai_config SET provider_name=? WHERE id=?",
            ("deepseek", "cfg1"))
        await db_conn.commit()

        # 强制缓存过期
        pf._config_cache["ts"] = 0.0

        cfg2 = await _load_active_config()
        assert cfg2 is not None
        assert cfg2["provider_name"] == "deepseek"  # 新值

    async def test_cache_ttl_default_is_300_seconds(self, db_conn):
        """默认缓存 TTL 为 300 秒（与历史硬编码值一致，旧行为不变）"""
        assert _config_cache_ttl() == 300.0

    async def test_cache_ttl_follows_settings(self, db_conn):
        """✅ 回归：settings.ai_config_cache_ttl 必须真正被消费（原先该配置项是死配置）"""
        old = pf.settings.ai_config_cache_ttl
        try:
            pf.settings.ai_config_cache_ttl = 3600
            assert _config_cache_ttl() == 3600.0
            pf.settings.ai_config_cache_ttl = 0        # 非法值回落默认
            assert _config_cache_ttl() == 300.0
            pf.settings.ai_config_cache_ttl = "abc"    # 脏值不抛异常
            assert _config_cache_ttl() == 300.0
        finally:
            pf.settings.ai_config_cache_ttl = old

    async def test_picks_most_recently_updated(self, db_conn):
        """多条活跃配置：取 updated_at 最新的一条"""
        await _insert_ai_config(db_conn, cid="old", provider="openai")
        # 手动设置 old 的 updated_at 为较早时间
        await db_conn.execute(
            "UPDATE ai_config SET updated_at=? WHERE id=?",
            ("2024-01-01T00:00:00", "old"))

        await _insert_ai_config(db_conn, cid="new", provider="deepseek")
        await db_conn.execute(
            "UPDATE ai_config SET updated_at=? WHERE id=?",
            ("2025-06-01T00:00:00", "new"))
        await db_conn.commit()

        cfg = await _load_active_config()
        assert cfg["id"] == "new"
        assert cfg["provider_name"] == "deepseek"

    async def test_ignores_inactive_config(self, db_conn):
        """仅 is_active=1 的配置被选中"""
        await _insert_ai_config(db_conn, cid="active", provider="openai",
                                is_active=1)
        await _insert_ai_config(db_conn, cid="inactive", provider="deepseek",
                                is_active=0)
        await db_conn.commit()

        cfg = await _load_active_config()
        assert cfg["id"] == "active"

    async def test_none_not_cached_always_queries_db(self, db_conn):
        """无配置时 None 不被缓存，每次都查 DB

        代码设计：缓存条件为 `data is not None and ...`，
        None 被视为"未配置"的临时状态，不缓存，确保插入配置后立即可见。
        """
        cfg1 = await _load_active_config()
        assert cfg1 is None

        # 插入配置
        await _insert_ai_config(db_conn, cid="cfg1", provider="openai")

        # None 未缓存，立即查到新配置
        cfg2 = await _load_active_config()
        assert cfg2 is not None
        assert cfg2["id"] == "cfg1"

    async def test_cache_expiry_boundary(self, db_conn):
        """缓存过期边界：ts + TTL == now 时视为过期（重新查库）

        条件：now - ts < TTL 才命中，所以 now - ts == TTL 时不命中。
        """
        await _insert_ai_config(db_conn, cid="cfg1", provider="openai")

        cfg1 = await _load_active_config()
        original_ts = pf._config_cache["ts"]

        # 设置 ts 使 now - ts 恰好 == TTL（边界）
        import time
        now = time.time()
        pf._config_cache["ts"] = now - _config_cache_ttl()  # 差值恰好 == TTL

        # 修改 DB 以区分缓存命中 vs 重新查库
        await db_conn.execute(
            "UPDATE ai_config SET provider_name=? WHERE id=?",
            ("deepseek", "cfg1"))
        await db_conn.commit()

        cfg2 = await _load_active_config()
        # now - ts == TTL，不满足 < TTL，应重新查库
        assert cfg2["provider_name"] == "deepseek"

    async def test_returns_dict_with_all_columns(self, db_conn):
        """返回的 dict 包含 ai_config 表的所有列"""
        await _insert_ai_config(db_conn, cid="cfg1", provider="openai",
                                api_key="enc-key", base_url="https://api.x.com/v1",
                                model="gpt-4o")

        cfg = await _load_active_config()
        assert isinstance(cfg, dict)
        # 核心字段存在
        for key in ("id", "provider_name", "api_key_encrypted", "base_url",
                    "model", "is_active"):
            assert key in cfg