"""图像模型配置模块 · R238 护栏（建表 / custom 适配器 / 路由 / 消费点 / 协同决策展示）。"""
import pytest
from app.schema_sql import SCHEMA_SQL
from app.services.ai.image_providers import CustomAdapter, ProviderRegistry
from app.services.ai.image_providers.custom import build_openai_payload


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


def test_custom_adapter_registered():
    assert "custom" in ProviderRegistry.list_providers()
    assert CustomAdapter.provider_id == "custom"
    assert CustomAdapter.supported_sizes == ["1K", "2K", "3K", "4K"]


def test_custom_payload_shape():
    """custom 走 OpenAI images/generations 协议：model/prompt/n/size/quality。"""
    p = build_openai_payload("一段提示词", "dall-e-3", "1024x1024")
    assert p["model"] == "dall-e-3"
    assert p["prompt"] == "一段提示词"
    assert p["n"] == 1
    assert p["size"] == "1024x1024"
    assert p["quality"] == "standard"
