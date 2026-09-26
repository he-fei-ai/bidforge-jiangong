"""提示词缓存「外部写入自动重载」回归（2026-09-24 · 遗留项 5 闭环）。

背景：旧 _get_prompt_cache 只看「缓存是否为空」与「DB 路径是否变化」。
同一进程内的保存会显式调 reload_prompt_cache()，但**外部进程 / 脚本 /
迁移工具直接写 prompt_templates** 时路径没变、缓存也没被失效 ——
读取方永远拿不到新值，表现为「提示词已保存却不生效」且无任何告警。

修复：_load_prompt_cache_sync 记录加载时的库文件 mtime；路径未变但
库文件已变新时自动重载。正常路径仅多一次 os.stat。
"""
from __future__ import annotations

import os
import sqlite3
import time

import pytest

import app.db as _appdb
from app.services.ai.prompts import _cache

PROBE_KEY = "test_external_write_probe"


def _seed(db_path, content: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS prompt_templates ("
            "key TEXT PRIMARY KEY, content TEXT DEFAULT '')")
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


class TestExternalWriteReload:
    async def test_external_write_is_picked_up_without_explicit_reload(self, ctx):
        """核心反例：外部进程改库后，读取方必须自动看到新值。"""
        db = ctx / "ext.sqlite"
        _seed(db, "VERSION-A")
        _appdb.DB_PATH = db
        _cache.reload_prompt_cache()
        assert _cache.get_prompt(PROBE_KEY) == "VERSION-A"

        # 模拟外部进程直写（不走 reload_prompt_cache）
        time.sleep(0.02)
        _seed(db, "VERSION-B")
        # 关键：不调用 reload_prompt_cache()
        assert _cache.get_prompt(PROBE_KEY) == "VERSION-B", (
            "外部直写 prompt_templates 后，读取方必须自动重载缓存")

    async def test_same_path_no_write_no_reload(self, ctx, tmp_path, monkeypatch):
        """库文件未变时不得反复重载（get_prompt 高频调用不能每次开连接）。"""
        db = tmp_path / "same.sqlite"
        _seed(db, "STABLE")
        _appdb.DB_PATH = db
        _cache.reload_prompt_cache()
        assert _cache.get_prompt(PROBE_KEY) == "STABLE"
        calls = []
        real = _cache._load_prompt_cache_sync

        def spy():
            calls.append(1)
            real()

        monkeypatch.setattr(_cache, "_load_prompt_cache_sync", spy)
        for _ in range(3):
            assert _cache.get_prompt(PROBE_KEY) == "STABLE"
        assert not calls, "库未变化时不得触发重新加载"

    async def test_reload_resets_mtime_marker(self, ctx, tmp_path):
        """显式 reload 后 mtime 标记必须一并失效（状态机口径一致）。"""
        db = tmp_path / "m.sqlite"
        _seed(db, "X1")
        _appdb.DB_PATH = db
        _cache.reload_prompt_cache()
        assert _cache.get_prompt(PROBE_KEY) == "X1"
        assert _cache._loaded_db_mtime is not None

        _cache.reload_prompt_cache()
        assert _cache._loaded_db_mtime is None
        assert _cache._loaded_db_path is None

    async def test_db_file_deleted_keeps_cache(self, ctx, tmp_path):
        """库文件被删（stat 失败）→ 保留现有缓存，不抛异常。"""
        db = tmp_path / "gone.sqlite"
        _seed(db, "GONE")
        _appdb.DB_PATH = db
        _cache.reload_prompt_cache()
        assert _cache.get_prompt(PROBE_KEY) == "GONE"
        os.remove(db)
        assert _cache.get_prompt(PROBE_KEY) == "GONE", "stat 失败应沿用现有缓存"
