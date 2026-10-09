"""提示词缓存跟随 app.db.DB_PATH 切换回归（2026-09-23 · 缓存路径漂移修复）。

背景：_load_prompt_cache_sync 固定读 DATA_DIR/"scheme_assistant.db"，
而真实连接路径以 app.db.DB_PATH 为唯一事实源（测试/多实例部署会切换该值）。
路径切换后缓存仍指向旧库 → DB 里编辑过的提示词"不生效"且无告警。
现缓存记录加载时路径，路径变化后自动重载。
"""
import sqlite3

import app.db as _appdb
import pytest
from app.services.ai.prompts import _cache

# 探针 key：不进 _ALL_PROMPTS 注册表，get_prompt 命中 DB 缓存时原样返回，
# 不受 {SHARED_*} 解析与出厂默认回退影响，断言稳定。
PROBE_KEY = "test_cache_probe_key"


def _seed_prompt(db_path, content: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS prompt_templates ("
            "key TEXT PRIMARY KEY, category TEXT DEFAULT '', label TEXT DEFAULT '',"
            " content TEXT DEFAULT '', default_content TEXT DEFAULT '',"
            " updated_at TEXT)")
        conn.execute(
            "INSERT OR REPLACE INTO prompt_templates(key, content) VALUES(?,?)",
            (PROBE_KEY, content))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
async def ctx(tmp_path):
    prev = _appdb.DB_PATH
    _cache.reload_prompt_cache()
    yield tmp_path
    _appdb.DB_PATH = prev
    _cache.reload_prompt_cache()


class TestPromptCacheFollowsDbPath:
    async def test_cache_reloads_on_db_path_switch(self, ctx):
        tmp_path = ctx
        db1, db2 = tmp_path / "a.sqlite", tmp_path / "b.sqlite"
        _seed_prompt(db1, "FROM-A")
        _seed_prompt(db2, "FROM-B")

        _appdb.DB_PATH = db1
        assert _cache.get_prompt(PROBE_KEY) == "FROM-A"

        # ✅ 关键：不显式 reload，切换 DB_PATH 后必须自动重载
        _appdb.DB_PATH = db2
        assert _cache.get_prompt(PROBE_KEY) == "FROM-B", (
            "DB_PATH 切换后缓存必须跟随新库，不得静默指向旧库")

        _appdb.DB_PATH = db1
        assert _cache.get_prompt(PROBE_KEY) == "FROM-A"

    async def test_missing_db_file_falls_back_to_registry(self, ctx, tmp_path):
        """库文件不存在：回退出厂默认（旧行为不变），且不抛异常。"""
        _appdb.DB_PATH = tmp_path / "not-exists.sqlite"
        got = _cache.get_prompt("outline_short_system")
        assert got.strip(), "回退必须拿到注册表出厂默认内容"

    async def test_same_path_no_spurious_reload(self, ctx, tmp_path, monkeypatch):
        """路径未变时不得反复重载（回归：get_prompt 高频调用不能每次开连接）。"""
        db = tmp_path / "c.sqlite"
        _seed_prompt(db, "FROM-C")
        _appdb.DB_PATH = db
        _cache.reload_prompt_cache()
        assert _cache.get_prompt(PROBE_KEY) == "FROM-C"
        calls = []
        real_load = _cache._load_prompt_cache_sync

        def spy():
            calls.append(1)
            real_load()

        monkeypatch.setattr(_cache, "_load_prompt_cache_sync", spy)
        for _ in range(3):
            assert _cache.get_prompt(PROBE_KEY) == "FROM-C"
        assert not calls, "同一路径重复读取不得触发重新加载"


class TestPromptGovernance:
    async def test_update_and_reset_write_hash_only_audit(self, db_conn):
        from app import main as app_main
        from app.services.ai.prompts._registry import get_default_prompt

        key = "outline_short_system"
        default = get_default_prompt(key)
        before_vars = set(__import__(
            "app.services.ai.prompts._registry", fromlist=["extract_variables"]
        ).extract_variables(default))
        changed = default + "\n新增要求：{custom_rule}"
        try:
            res = await app_main.update_prompt(
                key, {"content": changed}, db=db_conn)
            assert res["ok"] is True
            assert set(res["added_variables"]) == {"custom_rule"}

            cur = await db_conn.execute(
                "SELECT action,before_hash,after_hash,variables_before,variables_after"
                " FROM prompt_audit_logs WHERE prompt_key=? ORDER BY rowid DESC", (key,))
            row = await cur.fetchone()
            assert row["action"] == "update"
            assert row["after_hash"] == res["content_hash"]
            assert "custom_rule" in row["variables_after"]
            # 审计表不得复制完整提示词正文
            dumped = str(dict(row))
            assert changed not in dumped and default not in dumped

            listed = await app_main.list_prompts(db=db_conn)
            item = next(i for i in listed["items"] if i["key"] == key)
            assert item["updated_at"]
            assert item["content_hash"] == res["content_hash"]
            assert item["audit_count"] == 1

            reset = await app_main.reset_prompt(key, db=db_conn)
            assert reset["ok"] is True
            cur = await db_conn.execute(
                "SELECT action,after_hash FROM prompt_audit_logs"
                " WHERE prompt_key=? ORDER BY rowid DESC LIMIT 1", (key,))
            row = await cur.fetchone()
            assert row["action"] == "reset"
            assert row["after_hash"] == reset["content_hash"]
            logs = await app_main.prompt_audit_logs(key=key, db=db_conn)
            assert logs["total"] == 2
        finally:
            from app.services.ai.prompts._cache import reload_prompt_cache
            from app.services.ai.prompts._registry import reset_prompt
            reset_prompt(key)
            reload_prompt_cache()

    async def test_rejects_non_string_and_oversized_content(self, db_conn):
        from app import main as app_main
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            await app_main.update_prompt("outline_short_system", {"content": 123}, db=db_conn)
        assert ei.value.status_code == 400
        with pytest.raises(HTTPException) as ei:
            await app_main.update_prompt(
                "outline_short_system", {"content": "测" * 200001}, db=db_conn)
        assert ei.value.status_code == 400
        cur = await db_conn.execute("SELECT COUNT(*) FROM prompt_audit_logs")
        assert (await cur.fetchone())[0] == 0

