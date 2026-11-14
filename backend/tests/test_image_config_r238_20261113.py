"""图像模型配置模块 · R238 护栏（建表 / custom 适配器 / 路由 / 消费点 / 协同决策展示）。"""
import httpx
import pytest
from app.db import get_db, read_db
from app.main import app
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


class _Settings:
    image_enabled = True
    image_api_key = "sk-env"
    image_base_url = "https://env.example/v1"
    image_model = "env-model"
    image_default_size = "2K"
    image_price_per_image = 0.1
    image_provider = "dashscope"
    image_max_concurrency = 2
    max_ai_images = 0


@pytest.mark.asyncio
async def test_resolve_image_config_no_row_falls_back_to_settings():
    """无 DB 行：逐字段回落 settings（与现状一致，默认行为零变化）。"""
    from app.services.ai.image_engine import resolve_image_config
    out = await resolve_image_config(None, _Settings())
    assert out["image_provider"] == "dashscope"
    assert out["image_base_url"] == "https://env.example/v1"
    assert out["image_model"] == "env-model"
    assert out["image_default_size"] == "2K"
    assert out["image_enabled"] is True


@pytest.mark.asyncio
async def test_resolve_image_config_has_row_prefers_db(monkeypatch):
    """有 DB 行：以 DB 为准（DB provider/base_url 覆盖 settings）。"""
    from app.services.ai.image_engine import resolve_image_config

    class _Row(dict):
        """模拟 aiosqlite.Row（支持 keys() 与 row[列名]）。"""
        def __init__(self):
            super().__init__(
                provider="custom", model="dall-e-3",
                base_url="https://api.openai.com/v1",
                api_key_encrypted="ENC:xyz", enabled=1, default_size="3K",
                concurrency=4, price_per_image=0.02, max_ai_images=6,
            )

    async def _fetchone(*a, **k):
        return _Row()

    class _Cursor:
        def __init__(self):
            self.row = None
        async def fetchone(self, *a, **k):
            return self.row

    class _Db:
        def __init__(self):
            self.cur = _Cursor()
        async def execute(self, *a, **k):
            self.cur.row = _Row()
            return self.cur

    out = await resolve_image_config(_Db(), _Settings())
    assert out["image_provider"] == "custom"
    assert out["image_model"] == "dall-e-3"
    assert out["image_base_url"] == "https://api.openai.com/v1"
    assert out["image_default_size"] == "3K"
    assert out["max_ai_images"] == 6


@pytest.mark.asyncio
async def test_resolve_image_config_enabled_zero_short_circuits():
    """DB enabled=0 → image_enabled=False（关闭生图语义保持）。"""
    from app.services.ai.image_engine import resolve_image_config

    class _Row(dict):
        """模拟 aiosqlite.Row（支持 keys() 与 row[列名]）。"""
        def __init__(self):
            super().__init__(
                provider="custom", model="", base_url="", api_key_encrypted="",
                enabled=0, default_size="2K", concurrency=2,
                price_per_image=0.0, max_ai_images=0,
            )

    class _Db:
        async def execute(self, *a, **k):
            class _C:
                async def fetchone(self, *a, **k):
                    return _Row()
            return _C()

    out = await resolve_image_config(_Db(), _Settings())
    assert out["image_enabled"] is False


# ===================== E. 路由契约（HTTP 级） =====================
# 走 httpx.ASGITransport（无 lifespan）——TestClient 的 `with` 上下文会触发
# lifespan → init_db() + 各启动钩子，在测试进程里副作用大且依赖真实库；这里
# 只测「路由已注册 + 依赖注入假连接 + 响应契约」，不触发生命周期。
# FastAPI 依赖在路由装饰时已捕获函数对象，monkeypatch 模块属性无效，故用
# app.dependency_overrides 注入假连接（标准机制，经 monkeypatch.setitem 自动还原）。


class _FakeCursor:
    """模拟 aiosqlite.Cursor：fetchone 返回预置行。"""

    def __init__(self, row):
        self._row = row

    async def fetchone(self, *a, **k):
        return self._row


class _FakeDb:
    """模拟 aiosqlite.Connection：execute 记录 SQL、返回预置行。"""

    def __init__(self, row=None):
        self._row = row
        self.executed = []
        self.committed = False

    async def execute(self, sql, *a, **k):
        self.executed.append(sql)
        return _FakeCursor(self._row)

    async def commit(self):
        self.committed = True


def _primary_row():
    """单条 primary 配置行（dict 形态，resolve_image_config 走 keys() 分支）。"""
    return {
        "provider": "custom",
        "model": "dall-e-3",
        "base_url": "https://api.openai.com/v1",
        "api_key_encrypted": "ENC:abc",
        "enabled": 1,
        "default_size": "2K",
        "concurrency": 2,
        "price_per_image": 0.0,
        "max_ai_images": 0,
    }


async def _client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.asyncio
async def test_image_config_get_returns_contract(monkeypatch):
    """GET /api/v1/image-config 返回 {config, presets, decision} 三面。"""
    async def _fake_read_db():
        return _FakeDb(row=_primary_row())

    monkeypatch.setitem(app.dependency_overrides, read_db, _fake_read_db)
    async with await _client() as c:
        r = await c.get("/api/v1/image-config")
    assert r.status_code == 200, f"应 200：{r.text[:300]}"
    body = r.json()
    for key in ("config", "presets", "decision"):
        assert key in body, f"缺 {key}"
    # presets 含 5 家展示厂商
    for v in ("dalle3", "tongyi_wanxiang", "doubao_seedream", "tencent_hunyuan", "custom"):
        assert v in body["presets"], f"presets 缺 {v}"
    # GET 不回传明文密钥（api_key 恒空 + has_key 反映是否已配置）
    assert body["config"].get("api_key") == ""
    assert body["config"].get("has_key") is True


@pytest.mark.asyncio
async def test_image_config_put_validates_provider(monkeypatch):
    """PUT 非法 provider → 400。"""
    async def _fake_get_db():
        return _FakeDb()

    monkeypatch.setitem(app.dependency_overrides, get_db, _fake_get_db)
    async with await _client() as c:
        r = await c.put(
            "/api/v1/image-config",
            json={"provider": "not_a_provider", "default_size": "2K"},
        )
    assert r.status_code == 400
