"""图像模型配置模块 · R238 护栏（建表 / custom 适配器 / 路由 / 消费点 / 协同决策展示）。"""
import pytest
from app.schema_sql import SCHEMA_SQL


def test_schema_contains_image_model_config_table():
    """SCHEMA_SQL 必须含 image_model_config 建表语句与关键列。"""
    assert "CREATE TABLE IF NOT EXISTS image_model_config" in SCHEMA_SQL
    for col in (
        "id TEXT PRIMARY KEY", "provider TEXT", "model TEXT DEFAULT ''",
        "base_url TEXT DEFAULT ''", "api_key_encrypted TEXT DEFAULT ''",
        "enabled INTEGER DEFAULT 1", "default_size TEXT DEFAULT '2K'",
        "concurrency INTEGER DEFAULT 2", "price_per_image REAL DEFAULT 0.0",
        "max_ai_images INTEGER DEFAULT 0", "updated_at TEXT DEFAULT",
    ):
        assert col in SCHEMA_SQL, f"image_model_config 缺列: {col}"
