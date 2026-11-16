# -*- coding: utf-8 -*-
"""图像模型配置模块 · R252 护栏（SSRF / 场景路由 / 密钥三态脱敏 / 多模型 CRUD / 参数模板）。

五修复面（IM-1~IM-5）：
  IM-1  SSRF 防护：保存路径复用 normalize_base_url（内网/回环拦截 + 逃生开关）。
  IM-2  场景路由：image_scene_routes 表 + IMAGE_SCENES 白名单 + resolve_scene_image_config。
  IM-3  密钥三态脱敏：key_hint（后4位）+ key_broken（密文损坏标记）；明文恒不回传。
  IM-4  多模型多配置：image_config 多行 CRUD + set-default + 热更新缓存失效代际号。
  IM-5  参数模板：default_params JSON（分辨率/采样/CFG/负面词/风格）解析与适配器注入。
"""
import json
import uuid

import httpx
import pytest
from app.db import get_db, read_db
from app.main import app
from app.schema_sql import SCHEMA_SQL
from app.services.ai.image_engine import resolve_scene_image_config

# ============================================================================
# A. 结构契约
# ============================================================================


def test_schema_contains_image_multiconfig_tables():
    """SCHEMA_SQL 必须含 image_config / image_scene_routes / image_config_audit_logs 建表。"""
    for table in ("image_config", "image_scene_routes", "image_config_audit_logs"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in SCHEMA_SQL, f"缺表: {table}"
    for col in (
        "id TEXT PRIMARY KEY", "is_default INTEGER DEFAULT 0", "status TEXT DEFAULT 'active'",
        "env TEXT DEFAULT ''", "priority INTEGER DEFAULT 0", "remark TEXT DEFAULT ''",
        "default_params TEXT DEFAULT '{}'", "config_version INTEGER DEFAULT 1",
    ):
        assert col in SCHEMA_SQL, f"image_config 缺列: {col}"
    assert "CREATE TABLE IF NOT EXISTS image_scene_routes" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS image_config_audit_logs" in SCHEMA_SQL


def test_image_scenes_whitelist_defined():
    """IMAGE_SCENES 白名单必须含四大场景，且 engineering_diagram/realistic_photo 为首要。"""
    from app.routers.image_config import IMAGE_SCENES

    for s in ("engineering_diagram", "realistic_photo", "technical_drawing", "custom"):
        assert s in IMAGE_SCENES, f"IMAGE_SCENES 缺 {s}"


# ============================================================================
# B. IM-1 SSRF 防护
# ============================================================================


class _FakeCursor:
    def __init__(self, rows=None):
        self._rows = rows or []

    async def fetchone(self, *a, **k):
        return self._rows[0] if self._rows else None

    async def fetchall(self, *a, **k):
        return self._rows


class _FakeDb:
    """模拟 aiosqlite.Connection：记录 SQL 与参数，返回预置行。"""

    def __init__(self, rows=None):
        self._rows = rows or []
        self.executed = []
        self.params = []
        self.committed = False

    async def execute(self, sql, *a, **k):
        self.executed.append(sql)
        self.params.append(a)
        return _FakeCursor(self._rows)

    async def commit(self):
        self.committed = True


async def _client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _valid_payload():
    return {
        "provider": "custom",
        "model": "dall-e-3",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test-1234",
        "enabled": True,
        "default_size": "2K",
        "concurrency": 2,
        "price_per_image": 0.0,
        "max_ai_images": 0,
        "env": "",
        "priority": 0,
        "remark": "",
        "default_params": {},
    }


@pytest.mark.asyncio
async def test_im1_ssrf_blocks_internal_url(monkeypatch):
    """IM-1：保存 image-models 时 base_url 指向 127.0.0.1 → 400。"""
    async def _fake_get_db():
        return _FakeDb()

    monkeypatch.setitem(app.dependency_overrides, get_db, _fake_get_db)
    payload = _valid_payload()
    payload["base_url"] = "http://127.0.0.1:8080/v1"
    async with await _client() as c:
        r = await c.post("/api/v1/image-models", json=payload)
    assert r.status_code == 400, f"应 400：{r.text[:300]}"
    assert "内网" in r.text or "安全策略" in r.text or "回环" in r.text


@pytest.mark.asyncio
async def test_im1_ssrf_allows_public_url(monkeypatch):
    """IM-1：合法公网 URL 放行（写库成功）。"""
    # ✅ R252 延续（2026-11-16 · 测试环境 DNS 解耦）：本用例验证「路由对公网 URL
    #    不误拒」。SSRF 拦截语义由 test_im1_ssrf_blocks_internal_url（字面
    #    127.0.0.1，零 DNS）覆盖；此处若依赖真实域名解析，受限沙箱 DNS 偶发把
    #    api.openai.com 解析为内网 sinkhole，会被 SSRF 正确拦截造成间歇假红。
    #    monkeypatch 仅隔离 DNS 解析层，路由仍走 normalize_base_url 全链路。
    import app.services.ai.provider_factory as _pf

    monkeypatch.setattr(_pf, "_is_internal_host", lambda host: False)
    async def _fake_get_db():
        return _FakeDb()

    monkeypatch.setitem(app.dependency_overrides, get_db, _fake_get_db)
    async with await _client() as c:
        r = await c.post("/api/v1/image-models", json=_valid_payload())
    assert r.status_code == 200, f"应 200：{r.text[:300]}"
    body = r.json()
    assert body.get("ok") is True
    assert body.get("id")


@pytest.mark.asyncio
async def test_im1_ssrf_respects_escape_hatch(monkeypatch):
    """IM-1：AI_ALLOW_PRIVATE_BASE_URL=1 时内网地址放行（逃生开关）。"""
    from app.config import settings as _s

    monkeypatch.setattr(_s, "ai_allow_private_base_url", True)
    async def _fake_get_db():
        return _FakeDb()

    monkeypatch.setitem(app.dependency_overrides, get_db, _fake_get_db)
    payload = _valid_payload()
    payload["base_url"] = "http://127.0.0.1:8080/v1"
    async with await _client() as c:
        r = await c.post("/api/v1/image-models", json=payload)
    assert r.status_code == 200, f"应 200：{r.text[:300]}"


# ============================================================================
# C. IM-2 场景路由
# ============================================================================


def _default_row():
    return {
        "id": "cfg-main",
        "provider": "custom", "model": "dall-e-3",
        "base_url": "https://api.openai.com/v1", "api_key_encrypted": "ENC:abc",
        "enabled": 1, "default_size": "2K", "concurrency": 2,
        "price_per_image": 0.0, "max_ai_images": 0,
        "is_default": 1, "status": "active", "env": "",
        "priority": 0, "remark": "", "default_params": "{}", "config_version": 1,
    }


@pytest.mark.asyncio
async def test_im2_resolve_scene_hit(monkeypatch):
    """IM-2：场景已路由 → resolve_scene_image_config 返回该配置。"""
    from app.routers.image_config import resolve_scene_image_config as _r

    row = _default_row()
    row["id"] = "cfg-scene"
    row["provider"] = "dashscope"
    scene_row = {"config_id": "cfg-scene"}

    class _Db:
        async def execute(self, sql, *a, **k):
            class _C:
                async def fetchone(self, *a2, **k2):
                    if "image_scene_routes" in sql:
                        return scene_row
                    if "image_config" in sql:
                        return row
                    return None
                async def fetchall(self, *a2, **k2):
                    return [row]
            return _C()

    out = await resolve_scene_image_config(_Db(), None, "engineering_diagram")
    assert out is not None
    assert out.get("image_provider") == "dashscope"


@pytest.mark.asyncio
async def test_im2_resolve_scene_miss_falls_back(monkeypatch):
    """IM-2：场景未路由 → None（运行时回落主配置，与文本模型同口径）。"""
    class _Db:
        async def execute(self, sql, *a, **k):
            class _C:
                async def fetchone(self, *a2, **k2):
                    return None
                async def fetchall(self, *a2, **k2):
                    return []
            return _C()

    out = await resolve_scene_image_config(_Db(), None, "realistic_photo")
    assert out is None


@pytest.mark.asyncio
async def test_im2_scene_routes_endpoint(monkeypatch):
    """IM-2：GET /image-models/scene-routes 返回已知场景白名单。"""
    async def _fake_read_db():
        return _FakeDb()

    monkeypatch.setitem(app.dependency_overrides, read_db, _fake_read_db)
    async with await _client() as c:
        r = await c.get("/api/v1/image-models/scene-routes")
    assert r.status_code == 200, f"应 200：{r.text[:300]}"
    body = r.json()
    scenes = {i["scene"] for i in body.get("items", [])}
    for s in ("engineering_diagram", "realistic_photo", "technical_drawing", "custom"):
        assert s in scenes, f"scene-routes 缺 {s}"


# ============================================================================
# D. IM-3 密钥三态脱敏
# ============================================================================


@pytest.mark.asyncio
async def test_im3_list_masks_key_and_never_returns_plaintext(monkeypatch):
    """IM-3：列表接口回传 key_hint（后4位），明文/密文恒不回传。"""
    row = _default_row()
    row["api_key_encrypted"] = "ENC:fake"

    async def _fake_read_db():
        return _FakeDb([row])

    monkeypatch.setitem(app.dependency_overrides, read_db, _fake_read_db)
    async with await _client() as c:
        r = await c.get("/api/v1/image-models")
    assert r.status_code == 200
    items = r.json().get("items", [])
    assert items, "应至少一条配置"
    it = items[0]
    # 明文密钥与完整密文均不得回传
    assert "api_key" not in it or not it.get("api_key")
    assert "api_key_encrypted" not in it
    # key_hint 为后 4 位形态；key_broken 为布尔
    assert "key_hint" in it
    assert "key_broken" in it


@pytest.mark.asyncio
async def test_im3_key_broken_flag_when_corrupt(monkeypatch):
    """IM-3：密文损坏（解不开）→ key_broken=True + has_key=False。"""
    from app.services.crypto import encrypt_api_key

    row = _default_row()
    row["api_key_encrypted"] = "gAAAA" + "x" * 80  # 非法 Fernet token（解不开）

    async def _fake_read_db():
        return _FakeDb([row])

    monkeypatch.setitem(app.dependency_overrides, read_db, _fake_read_db)
    async with await _client() as c:
        r = await c.get("/api/v1/image-models")
    items = r.json().get("items", [])
    it = items[0]
    assert it["key_broken"] is True
    assert it["has_key"] is False
    assert it["key_hint"] == ""


# ============================================================================
# E. IM-4 多模型 CRUD + set-default + 热更新
# ============================================================================


@pytest.mark.asyncio
async def test_im4_create_update_delete_lifecycle(monkeypatch):
    """IM-4：POST 创建 → PUT 更新 → DELETE 删除全链路（含热更新代际号）。"""
    # ✅ R252 延续（2026-11-16 · 测试环境 DNS 解耦）：与 test_im1_ssrf_allows_public_url
    #    同理 —— 公网放行护栏不依赖真实 DNS，避免受限沙箱 sinkhole 解析的间歇假红。
    import app.services.ai.provider_factory as _pf

    monkeypatch.setattr(_pf, "_is_internal_host", lambda host: False)
    async def _fake_get_db():
        return _FakeDb()

    monkeypatch.setitem(app.dependency_overrides, get_db, _fake_get_db)
    cid = str(uuid.uuid4())[:8]
    async with await _client() as c:
        r = await c.post("/api/v1/image-models", json=_valid_payload())
    assert r.status_code == 200
    new_id = r.json().get("id")
    assert new_id

    payload = _valid_payload()
    payload["model"] = "dall-e-4"
    async with await _client() as c:
        r = await c.put(f"/api/v1/image-models/{new_id}", json=payload)
    assert r.status_code == 200, f"更新应 200：{r.text[:300]}"
    assert r.json().get("ok") is True
    assert r.json().get("generation") >= 1

    async with await _client() as c:
        r = await c.delete(f"/api/v1/image-models/{new_id}")
    assert r.status_code == 200
    assert r.json().get("ok") is True


@pytest.mark.asyncio
async def test_im4_set_default_endpoint(monkeypatch):
    """IM-4：POST /image-models/{id}/set-default 返回 ok + 触发失效。"""
    row = _default_row()
    row["id"] = "cfg-x"

    async def _fake_get_db():
        return _FakeDb([row])

    monkeypatch.setitem(app.dependency_overrides, get_db, _fake_get_db)
    async with await _client() as c:
        r = await c.post("/api/v1/image-models/cfg-x/set-default")
    assert r.status_code == 200, f"应 200：{r.text[:300]}"
    body = r.json()
    assert body.get("ok") is True
    assert body.get("default_id") == "cfg-x"


@pytest.mark.asyncio
async def test_im4_invalidate_generation_monotonic():
    """IM-4：热更新代际号单调递增（每次写路径 +1）。"""
    from app.routers import image_config as _ic

    before = _ic._image_generation
    _ic.bump_image_generation()
    assert _ic._image_generation == before + 1


# ============================================================================
# F. IM-5 参数模板
# ============================================================================


def test_im5_default_params_parse_and_inject():
    """IM-5：default_params JSON 解析 + 注入适配器 payload 字段。"""
    from app.routers.image_config import parse_default_params

    raw = {"resolution": "16:9", "steps": 30, "cfg": 7.5, "negative_prompt": "低清,模糊", "style": "写实"}
    out = parse_default_params(raw)
    assert out["resolution"] == "16:9"
    assert out["steps"] == 30
    assert out["cfg"] == 7.5
    assert out["negative_prompt"] == "低清,模糊"
    assert out["style"] == "写实"


def test_im5_default_params_ignores_invalid_json():
    """IM-5：default_params 存非法 JSON 时解析不崩（回退空模板）。"""
    from app.routers.image_config import parse_default_params

    out = parse_default_params("not-json{{")
    assert out == {}


@pytest.mark.asyncio
async def test_im5_engine_injects_default_params(monkeypatch):
    """IM-5：generate_illustration_image 收到 scene 后解析 default_params 并注入适配器。"""
    from app.services.ai import image_engine
    from app.services.ai.image_providers import registry as _reg
    from app.services.crypto import encrypt_api_key

    # 记录适配器收到的 kwargs
    captured = {}

    class _Adapter:
        provider_id = "custom"
        supported_sizes = ["1K", "2K", "3K", "4K"]

        async def generate(self, prompt, model, size, ratio, **kw):
            captured.update(kw)
            return {"image_url": "https://img.example/a.png"}

        async def test_connection(self, *a, **k):
            return True

    monkeypatch.setattr(_reg.ProviderRegistry, "get_adapter", lambda name, config: _Adapter())

    cfg = {
        "image_enabled": True,
        "image_api_key_enc": encrypt_api_key("sk-test-1234"),
        "image_base_url": "https://api.openai.com/v1",
        "image_model": "dall-e-3",
        "image_default_size": "2K",
        "image_price_per_image": 0.0,
        "image_provider": "custom",
        "image_max_concurrency": 2,
        "max_ai_images": 0,
        "image_default_params": {"steps": 28, "cfg": 7.0, "negative_prompt": "低清"},
    }

    class _Prov:
        api_key = ""
        base_url = ""
        model = ""

    out = await image_engine.generate_illustration_image(
        _Prov(), "测试提示词", style="engineering_diagram",
        section_size="16:9", provider_name="custom", image_config=cfg,
    )
    assert out == "https://img.example/a.png"
    assert captured.get("steps") == 28
    assert captured.get("cfg") == 7.0
    assert captured.get("negative_prompt") == "低清"


# ============================================================================
# G. TB-3 展示面补齐（延续轮 · 已注册适配器前端下拉补齐）
# ============================================================================


def test_tb3_provider_presets_cover_registered_adapters():
    """TB-3：PROVIDER_PRESETS / DISPLAY_TO_PROVIDER 覆盖 9 家展示厂商（含 4 家新补）。"""
    from app.routers.image_config import DISPLAY_TO_PROVIDER, PROVIDER_PRESETS

    for key in ("zhipu", "stepfun", "baidu", "agnes"):
        assert key in PROVIDER_PRESETS, f"presets 缺已注册适配器 {key}"
        assert PROVIDER_PRESETS[key]["provider"] == key, f"{key} 预设 provider 映射错误"
        assert DISPLAY_TO_PROVIDER[key] == key, f"{key} 展示键映射错误"
    # 既有 5 家保持在场（加法式，零破坏）
    for key in ("dalle3", "tongyi_wanxiang", "doubao_seedream", "tencent_hunyuan", "custom"):
        assert key in PROVIDER_PRESETS, f"presets 缺既有展示厂商 {key}"
    assert len(PROVIDER_PRESETS) >= 9
