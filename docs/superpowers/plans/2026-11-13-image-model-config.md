# 图像模型配置（AI 配图协同）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在侧边菜单新增「图像模型配置」页（`/settings/ai-image`），配置图像模型（DB 持久化、可测连接），并展示 AI 配图协同决策流（结构化图表 vs AI 概念配图）与文生图 Prompt 模板。

**Architecture:** 后端新建 `image_model_config` 表（单条活动配置）+ OpenAI 兼容 `custom` 适配器（覆盖 DALL-E-3/自定义厂商）+ `routers/image_config.py`（GET/PUT/test）+ `resolve_image_config` 共享函数（消费点改为 DB 优先、settings 兜底）；前端新增 `AIImageConfigPage.tsx`（三段式）+ 路由/菜单/API 封装。全部加法式，默认行为不变。

**Tech Stack:** 后端 FastAPI + aiosqlite（原始 SQL，`schema_sql.py` 建表）；前端 React + antd + vitest（源码锁 `import.meta.glob`）。

**Spec:** `docs/superpowers/specs/2026-11-13-image-model-config-design.md`

## Global Constraints

- 生产源 `.py`/`.html` 行尾**纯 CRLF**；新测试与文档**纯 LF**（R237 教训①：新建生产源默认 LF，需显式转 CRLF）。
- 新增 AI 调用点必须登记 `scene`（本任务无新增 AI 调用，仅复用既有 `generate_illustration_image`）。
- 不另立第三套类型词汇；`route_visual_strategy`/`parse_img_gen_prompt` **不改动**（仅展示）。
- 契约加法式：不改既有函数签名、不改既有图像生成默认行为（无 DB 行时逐字段回落 `settings`）。
- 密钥经 `app.services.crypto.encrypt_api_key` / `decrypt_api_key` / `is_encrypted`；`GET` 不回传明文（回传 `has_key`）。
- 后端测试用 `pytest.mark.asyncio` + `monkeypatch`（registry 适配器 mock，范式见 `test_chart_r196_20261109.py`）。
- 前端测试用 vitest 源码锁（`import.meta.glob("?raw")`，范式见 `tests/aiConfigR169.test.ts`）。
- 每个任务结束：ruff（后端）/ tsc（前端）通过后 commit。

---

### Task 1: 后端建表 `image_model_config`

**Files:**
- Modify: `backend/app/schema_sql.py`（`SCHEMA_SQL` 末尾追加 DDL）
- Test: `backend/tests/test_image_config_r238_20261113.py`（新建）

**Interfaces:**
- Produces: `SCHEMA_SQL` 含 `CREATE TABLE IF NOT EXISTS image_model_config`（列：id/provider/model/base_url/api_key_encrypted/enabled/default_size/concurrency/price_per_image/max_ai_images/updated_at）

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_image_config_r238_20261113.py
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest backend/tests/test_image_config_r238_20261113.py::test_schema_contains_image_model_config_table -v`
Expected: FAIL（AssertionError，表未定义）

- [ ] **Step 3: 实现建表**

`backend/app/schema_sql.py` `SCHEMA_SQL` 末尾（`ai_config` 表 DDL 之后）追加：

```python
CREATE TABLE IF NOT EXISTS image_model_config (
    id TEXT PRIMARY KEY,              -- 固定 'primary'（单条活动配置）
    provider TEXT DEFAULT 'custom',   -- custom / dashscope / volcengine / hunyuan / zhipu / stepfun / baidu / agnes
    model TEXT DEFAULT '',            -- 留空用厂商默认模型
    base_url TEXT DEFAULT '',         -- 留空用厂商默认端点
    api_key_encrypted TEXT DEFAULT '',-- ENC: 加密前缀
    enabled INTEGER DEFAULT 1,        -- 1 = 开启生图（image_enabled 语义）
    default_size TEXT DEFAULT '2K',   -- 1K/2K/3K/4K
    concurrency INTEGER DEFAULT 2,    -- image_max_concurrency
    price_per_image REAL DEFAULT 0.0, -- 单张成本
    max_ai_images INTEGER DEFAULT 0,  -- 全局配图预算（0 = 关闭）
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest backend/tests/test_image_config_r238_20261113.py::test_schema_contains_image_model_config_table -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/schema_sql.py backend/tests/test_image_config_r238_20261113.py
git commit -m "feat(image-config): R238 建 image_model_config 表"
```

---

### Task 2: `custom` OpenAI 兼容适配器

**Files:**
- Create: `backend/app/services/ai/image_providers/custom.py`
- Modify: `backend/app/services/ai/image_providers/__init__.py`（注册 `custom`）
- Test: `backend/tests/test_image_config_r238_20261113.py`（追加 C 组）

**Interfaces:**
- Produces: `CustomAdapter(ImageProviderAdapter)`，`provider_id="custom"`，`default_model=""`，`supported_sizes=["1K","2K","3K","4K"]`；`ProviderRegistry.list_providers()` 含 `"custom"`。

- [ ] **Step 1: 写失败测试（追加到测试文件）**

```python
from app.services.ai.image_providers import ProviderRegistry, CustomAdapter
from app.services.ai.image_providers.custom import build_openai_payload


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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest backend/tests/test_image_config_r238_20261113.py -k "custom" -v`
Expected: FAIL（ImportError: CustomAdapter / module not found）

- [ ] **Step 3: 实现适配器**

`backend/app/services/ai/image_providers/custom.py`：

```python
"""自定义厂商 / OpenAI 兼容图像生成适配器（覆盖 DALL-E-3 与任意 OpenAI 兼容端点）。"""
import base64
import logging
from typing import Any

import aiohttp

from app.services.redaction import redact_sensitive_text
from ....utils.proxy_config import aiohttp_session_kwargs
from .base import ImageProviderAdapter, build_endpoint

logger = logging.getLogger(__name__)


def build_openai_payload(prompt: str, model: str, size: str) -> dict:
    """构造 OpenAI images/generations 请求体（纯函数，可单测）。"""
    return {"model": model or "dall-e-3", "prompt": prompt, "n": 1, "size": size or "1024x1024", "quality": "standard"}


class CustomAdapter(ImageProviderAdapter):
    provider_id = "custom"
    default_model = ""
    supported_sizes = ["1K", "2K", "3K", "4K"]
    supported_ratios = ["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"]

    def __init__(self, config: dict[str, Any]):
        self.base_url = config.get("base_url", "")
        self.api_key = config.get("api_key") or ""
        self.model = config.get("model") or self.default_model
        self.default_size = config.get("default_size", "1024x1024")
        self.timeout = config.get("timeout", 60)
        self.price_per_image = config.get("price_per_image", 0.0)

    def _extract_url(self, data: dict) -> str | None:
        """从 OpenAI 兼容响应提取图片：data[0].url，无 url 则 b64_json → data URL。"""
        items = data.get("data") or []
        if not items:
            return None
        first = items[0]
        url = first.get("url")
        if url:
            return url
        b64 = first.get("b64_json")
        if b64:
            return f"data:image/png;base64,{b64}"
        return None

    async def generate(self, prompt, model=None, size=None, ratio=None, reference_images=None, **kwargs):
        use_model = model or self.model or "dall-e-3"
        pixel = size or self.default_size
        # 档位 1K/2K/3K/4K 转像素（OpenAI 兼容端点多为像素式；档位时按长边近似）
        if str(pixel) in {"1K", "2K", "3K", "4K"}:
            pixel = {"1K": "1024x1024", "2K": "1024x1024", "3K": "1024x1024", "4K": "2048x2048"}[str(pixel)]
        payload = build_openai_payload(prompt, use_model, pixel)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout), **aiohttp_session_kwargs()) as session:
                async with session.post(build_endpoint(self.base_url, "images", "generations"), headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        url = self._extract_url(data)
                        if url:
                            return {"success": True, "image_url": url, "image_base64": None,
                                    "cost": self.price_per_image, "provider": self.provider_id,
                                    "model": use_model, "raw_response": data, "error": None}
                        return {"success": False, "image_url": None, "image_base64": None, "cost": 0.0,
                                "provider": self.provider_id, "model": use_model, "raw_response": data,
                                "error": "API 返回中缺少图片 URL"}
                    err = redact_sensitive_text(await resp.text())[:200]
                    return {"success": False, "image_url": None, "image_base64": None, "cost": 0.0,
                            "provider": self.provider_id, "model": use_model,
                            "raw_response": {"status": resp.status, "error": err}, "error": f"HTTP {resp.status}: {err}"}
        except Exception as e:
            return {"success": False, "image_url": None, "image_base64": None, "cost": 0.0,
                    "provider": self.provider_id, "model": use_model, "raw_response": {}, "error": f"custom 调用失败: {e!s}"}

    async def test_connection(self, api_key, base_url):
        try:
            headers = {"Authorization": f"Bearer {api_key}"}
            async with aiohttp.ClientSession(**aiohttp_session_kwargs()) as session:
                async with session.get(build_endpoint(base_url, "models"), headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    return resp.status == 200
        except Exception as e:
            logger.debug("custom connection test failed: %s", e)
            return False

    async def list_models(self, api_key, base_url):
        try:
            headers = {"Authorization": f"Bearer {api_key}"}
            async with aiohttp.ClientSession(**aiohttp_session_kwargs()) as session:
                async with session.get(build_endpoint(base_url, "models"), headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return [m.get("id") for m in (data.get("data") or []) if m.get("id")]
        except Exception:
            pass
        return [self.default_model] if self.default_model else []
```

`backend/app/services/ai/image_providers/__init__.py`：导入并注册（`initialize_registry` 末尾加 `ProviderRegistry.register("custom", CustomAdapter)`；`__all__` 加 `"CustomAdapter"`）：

```python
from .custom import CustomAdapter
# 在 initialize_registry() 内：
ProviderRegistry.register("custom", CustomAdapter)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest backend/tests/test_image_config_r238_20261113.py -k "custom" -v`
Expected: PASS（`custom` 已注册 + payload 形状）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai/image_providers/custom.py backend/app/services/ai/image_providers/__init__.py backend/tests/test_image_config_r238_20261113.py
git commit -m "feat(image-config): R238 新增 custom OpenAI 兼容适配器"
```

---

### Task 3: `resolve_image_config` 共享函数

**Files:**
- Modify: `backend/app/services/ai/image_engine.py`（末尾追加）
- Test: `backend/tests/test_image_config_r238_20261113.py`（追加 D 组）

**Interfaces:**
- Produces: `resolve_image_config(db, settings) -> dict`，键 = `image_enabled`/`image_api_key_enc`/`image_base_url`/`image_model`/`image_default_size`/`image_price_per_image`/`image_provider`/`image_max_concurrency`/`max_ai_images`。
- Consumes: `db`（aiosqlite 连接或 None）、`settings`（`app.config.settings`）。

- [ ] **Step 1: 写失败测试**

```python
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

    class _Row:
        provider = "custom"
        model = "dall-e-3"
        base_url = "https://api.openai.com/v1"
        api_key_encrypted = "ENC:xyz"
        enabled = 1
        default_size = "3K"
        concurrency = 4
        price_per_image = 0.02
        max_ai_images = 6

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

    class _Row:
        provider = "custom"; model = ""; base_url = ""; api_key_encrypted = ""
        enabled = 0; default_size = "2K"; concurrency = 2
        price_per_image = 0.0; max_ai_images = 0

    class _Db:
        async def execute(self, *a, **k):
            class _C:
                async def fetchone(self, *a, **k):
                    return _Row()
            return _C()

    out = await resolve_image_config(_Db(), _Settings())
    assert out["image_enabled"] is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest backend/tests/test_image_config_r238_20261113.py -k "resolve_image_config" -v`
Expected: FAIL（ImportError: cannot import name 'resolve_image_config'）

- [ ] **Step 3: 实现**

`backend/app/services/ai/image_engine.py` 末尾追加：

```python
async def resolve_image_config(db, settings) -> dict:
    """读取当前图像模型配置（DB 优先、settings 兜底）。

    Args:
        db: aiosqlite 连接（可为 None，此时直接回落 settings）。
        settings: ``app.config.settings``（env 兜底）。

    Returns:
        dict，键 = image_enabled / image_api_key_enc / image_base_url / image_model /
        image_default_size / image_price_per_image / image_provider /
        image_max_concurrency / max_ai_images。
        无 DB 行时逐字段回落 settings，**默认行为与引入前完全一致**（零变化）。
    """
    def _get(name: str, default=""):
        return getattr(settings, name, default) if settings is not None else default

    row = None
    if db is not None:
        try:
            cur = await db.execute(
                "SELECT provider, model, base_url, api_key_encrypted, enabled, "
                "default_size, concurrency, price_per_image, max_ai_images "
                "FROM image_model_config WHERE id = 'primary'"
            )
            row = await cur.fetchone()
        except Exception as e:
            logger.warning("resolve_image_config: 读 image_model_config 失败，回落 settings: %s", e)
            row = None

    if row is None:
        return {
            "image_enabled": bool(_get("image_enabled", True)),
            "image_api_key_enc": _get("image_api_key", ""),
            "image_base_url": _get("image_base_url", ""),
            "image_model": _get("image_model", ""),
            "image_default_size": _get("image_default_size", "2K"),
            "image_price_per_image": float(_get("image_price_per_image", 0.0) or 0.0),
            "image_provider": _get("image_provider", "agnes"),
            "image_max_concurrency": int(_get("image_max_concurrency", 2) or 2),
            "max_ai_images": int(_get("max_ai_images", 0) or 0),
        }

    # aiosqlite Row 支持 keys()；兼容 tuple 形态用索引
    if hasattr(row, "keys"):
        _k = lambda key: row[key]  # noqa: E731
    else:
        _k = lambda key: row[("provider", "model", "base_url", "api_key_encrypted",
                              "enabled", "default_size", "concurrency",
                              "price_per_image", "max_ai_images").index(key)]  # noqa: E731

    return {
        "image_enabled": bool(_k("enabled")),
        "image_api_key_enc": _k("api_key_encrypted") or "",
        "image_base_url": _k("base_url") or "",
        "image_model": _k("model") or "",
        "image_default_size": _k("default_size") or "2K",
        "image_price_per_image": float(_k("price_per_image") or 0.0),
        "image_provider": _k("provider") or "custom",
        "image_max_concurrency": int(_k("concurrency") or 2),
        "max_ai_images": int(_k("max_ai_images") or 0),
    }
```

> ⚠️ 实现**必须 `async def`**：`db.execute(...)` 是 aiosqlite 协程（`await` 必需）。两个直接消费点（`export.py::_prepare_export` / `charts.py::generate_ai_image`）均为 async 上下文且**都有 db**，可直接 `await resolve_image_config(db, settings)`；`_export_cache.py` 经 `prep["image_config"]`（Task 5 由 `_prepare_export` 填充）间接消费，**不调** `resolve_image_config`。测试同步改为 `@pytest.mark.asyncio` + `await resolve_image_config(...)`。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest backend/tests/test_image_config_r238_20261113.py -k "resolve_image_config" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai/image_engine.py backend/tests/test_image_config_r238_20261113.py
git commit -m "feat(image-config): R238 resolve_image_config DB优先settings兜底"
```

---

### Task 4: `routers/image_config.py` + `main.py` 注册

**Files:**
- Create: `backend/app/routers/image_config.py`
- Modify: `backend/app/main.py`（import + include_router）
- Test: `backend/tests/test_image_config_r238_20261113.py`（追加 E 组路由契约）

**Interfaces:**
- Produces: `router`（prefix `/api/v1/image-config`）导出 `get_config`/`save_config`/`test_connection`。
- Consumes: `resolve_image_config`（Task 3）、`CustomAdapter`/`ProviderRegistry`（Task 2）、`encrypt_api_key`。

- [ ] **Step 1: 写失败测试**

```python
import pytest
from fastapi.testclient import TestClient
from app.main import app


def test_image_config_get_returns_contract():
    """GET /api/v1/image-config 返回 {config, presets, decision} 三面。"""
    client = TestClient(app)
    # ⚠️ 若 TestClient 触发 DB 初始化失败，用 monkeypatch 注入 read_db 假连接；
    #    此处契约测试聚焦结构，依赖 read_db 由实现注入。
    with client as c:
        r = c.get("/api/v1/image-config")
    assert r.status_code == 200
    body = r.json()
    for key in ("config", "presets", "decision"):
        assert key in body, f"缺 {key}"
    # presets 含 5 家展示厂商
    for v in ("dalle3", "tongyi_wanxiang", "doubao_seedream", "tencent_hunyuan", "custom"):
        assert v in body["presets"], f"presets 缺 {v}"


def test_image_config_put_validates_provider():
    """PUT 非法 provider → 400。"""
    client = TestClient(app)
    with client as c:
        r = c.put("/api/v1/image-config", json={"provider": "not_a_provider", "default_size": "2K"})
    assert r.status_code == 400
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest backend/tests/test_image_config_r238_20261113.py -k "image_config_get_returns_contract or image_config_put_validates_provider" -v`
Expected: FAIL（404/405，路由未注册）

- [ ] **Step 3: 实现**

`backend/app/routers/image_config.py`：

```python
"""图像模型配置路由（GET/PUT/test）。

契约：
- GET  /api/v1/image-config → {config, presets, decision}
- PUT  /api/v1/image-config → {ok, warnings?}
- POST /api/v1/image-config/test → {ok, message}
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.db import get_db, read_db
from app.services.ai.image_engine import resolve_image_config
from app.services.ai.image_providers.registry import ProviderRegistry
from app.services.crypto import encrypt_api_key

logger = logging.getLogger("image_config")
router = APIRouter(prefix="/api/v1/image-config", tags=["image_config"])

VALID_PROVIDERS = {"custom", "dashscope", "volcengine", "hunyuan", "zhipu", "stepfun", "baidu", "agnes"}
VALID_SIZES = {"1K", "2K", "3K", "4K"}
# 5 家展示厂商预设（DALL-E-3 → custom + OpenAI 端点；其余映射适配器默认）
PROVIDER_PRESETS = {
    "dalle3": {"provider": "custom", "base_url": "https://api.openai.com/v1", "model": "dall-e-3"},
    "tongyi_wanxiang": {"provider": "dashscope", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-image-3.0-pro"},
    "doubao_seedream": {"provider": "volcengine", "base_url": "", "model": ""},
    "tencent_hunyuan": {"provider": "hunyuan", "base_url": "", "model": ""},
    "custom": {"provider": "custom", "base_url": "", "model": ""},
}
# 展示厂商键 → provider_id（前端下拉值）
DISPLAY_TO_PROVIDER = {"dalle3": "custom", "tongyi_wanxiang": "dashscope",
                       "doubao_seedream": "volcengine", "tencent_hunyuan": "hunyuan", "custom": "custom"}


class ImageConfigIn(BaseModel):
    provider: str = Field("custom", max_length=32)
    model: str = Field("", max_length=200)
    base_url: str = Field("", max_length=500)
    api_key: str = Field("", max_length=2000)   # 明文或 ENC:（编辑不重填时传 "" 保持原样）
    enabled: bool = True
    default_size: str = Field("2K", max_length=8)
    concurrency: int = Field(2, ge=1, le=8)
    price_per_image: float = Field(0.0, ge=0.0, le=1000.0)
    max_ai_images: int = Field(0, ge=0, le=1000)


class ImageTestIn(BaseModel):
    provider: str = Field("custom", max_length=32)
    base_url: str = Field("", max_length=500)
    api_key: str = Field("", max_length=2000)


async def _load_row(db):
    cur = await db.execute("SELECT * FROM image_model_config WHERE id = 'primary'")
    return await cur.fetchone()


@router.get("")
async def get_config(db=Depends(read_db)):
    cfg = await resolve_image_config(db, settings)
    has_key = bool(cfg.get("image_api_key_enc"))
    cfg.pop("image_api_key_enc", None)  # 用户裁决(2026-11-13)：剥离敏感字段，不回传明文/密文
    # decision：协同决策流 + Prompt 模板说明（只读展示数据，不改判定逻辑）
    from app.services.ai.image_engine import parse_img_gen_prompt, route_visual_strategy
    return {
        "config": {**cfg, "has_key": has_key, "api_key": ""},
        "presets": PROVIDER_PRESETS,
        "decision": {
            "routes": [
                {"route": "structured", "desc": "命中图表生成关键词（gantt/architecture/flowchart）→ 结构化图表管线"},
                {"route": "illustration", "desc": "需氛围/示意配图（效果图/示意图/三维/实景/渲染/氛围/鸟瞰/透视/封面）→ AI 文生图"},
                {"route": "none", "desc": "既无图表关键词、也无配图需求 → 纯文字"},
            ],
            "decision_fn": "route_visual_strategy",
            "prompt_template": "[IMG_GEN_PROMPT: 主题=…；风格=…；构图=…；比例=…；负面词=…]",
            "prompt_fields": ["主题", "风格", "构图", "比例", "负面词"],
        },
    }


@router.put("")
async def save_config(body: ImageConfigIn, db=Depends(get_db)):
    if body.provider not in VALID_PROVIDERS:
        raise HTTPException(400, f"provider 不合法：{body.provider}")
    if body.default_size not in VALID_SIZES:
        raise HTTPException(400, f"default_size 不合法：{body.default_size}")
    # 加密（仅当传入明文新 key；为空保持原样）
    enc = ""
    if body.api_key:
        enc = encrypt_api_key(body.api_key) if not body.api_key.startswith("ENC:") else body.api_key
    else:
        cur = await db.execute("SELECT api_key_encrypted FROM image_model_config WHERE id='primary'")
        row = await cur.fetchone()
        if row:
            enc = row[0] if not hasattr(row, "keys") else row["api_key_encrypted"]
    await db.execute(
        "INSERT INTO image_model_config (id, provider, model, base_url, api_key_encrypted, enabled, "
        "default_size, concurrency, price_per_image, max_ai_images, updated_at) "
        "VALUES ('primary', ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime')) "
        "ON CONFLICT(id) DO UPDATE SET provider=excluded.provider, model=excluded.model, "
        "base_url=excluded.base_url, api_key_encrypted=excluded.api_key_encrypted, "
        "enabled=excluded.enabled, default_size=excluded.default_size, concurrency=excluded.concurrency, "
        "price_per_image=excluded.price_per_image, max_ai_images=excluded.max_ai_images, "
        "updated_at=datetime('now','localtime')",
        (body.provider, body.model, body.base_url, enc, 1 if body.enabled else 0,
         body.default_size, body.concurrency, body.price_per_image, body.max_ai_images),
    )
    await db.commit()
    return {"ok": True, "warnings": []}


@router.post("/test")
async def test_connection(body: ImageTestIn):
    if body.provider not in VALID_PROVIDERS:
        raise HTTPException(400, f"provider 不合法：{body.provider}")
    try:
        adapter = ProviderRegistry.get_adapter(body.provider, {"api_key": body.api_key, "base_url": body.base_url})
        ok = await adapter.test_connection(body.api_key, body.base_url)
    except Exception as e:
        return {"ok": False, "message": f"适配器异常：{e!s}"}
    return {"ok": ok, "message": "连接成功" if ok else "连接失败：请核对 Base URL / API Key / 模型"}
```

`backend/app/main.py`：`from app.routers import (... image_config, ...)`（在 routers 元组加 `image_config`），`app.include_router(image_config.router)`。

> ⚠️ `save_config` 用 `ON CONFLICT` 需 SQLite ≥3.24（本仓 aiosqlite 满足）；若目标 SQLite 版本不支持，改为「SELECT → UPDATE/INSERT」两段幂等。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest backend/tests/test_image_config_r238_20261113.py -k "image_config_get_returns_contract or image_config_put_validates_provider" -v`
Expected: PASS（注：若 `TestClient(app)` 触发 lifespan/DB 初始化问题，改用 `httpx.ASGITransport` 无 lifespan 模式，范式见 R235 `omp_ops` HTTP 级实证）

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/image_config.py backend/app/main.py backend/tests/test_image_config_r238_20261113.py
git commit -m "feat(image-config): R238 图像配置路由 + main 注册"
```

---

### Task 5: 消费点改造（export / charts / _export_cache 指纹）

**Files:**
- Modify: `backend/app/routers/export.py`（`_prepare_export` 解析并设 `prep["image_config"]` + 透传；`_auto_generate_ai_image_blocks` 签名加 `image_config` 参数）、`backend/app/routers/charts.py`（L861-868 改用 `await resolve_image_config`）、`backend/app/routers/_export_cache.py`（`_content_fingerprint` R-2 合并扩展为全键）
- Test: `backend/tests/test_image_config_r238_20261113.py`（追加 F 组消费点同源）
- 行为护栏：`backend/tests/test_export_high_risk_regressions.py`（追加 provider/budget 指纹用例）

**Interfaces:**
- Consumes: `resolve_image_config`（Task 3，DB 优先 settings 兜底）。
- Produces: `_prepare_export` 设 `prep["image_config"] = _img_cfg`（供 `_content_fingerprint` 全键合并）；`generate_illustration_image(..., provider_name=_img_cfg["image_provider"], image_config=_img_cfg)`。

- [ ] **Step 1: 写失败测试（源码锁）**

```python
from pathlib import Path

def _src(p):
    return Path(__file__).parent.parent / p

def test_consumption_points_use_resolve_image_config():
    """export.py / charts.py 均调用 resolve_image_config。"""
    for rel, marker in (
        ("app/routers/export.py", "resolve_image_config"),
        ("app/routers/charts.py", "resolve_image_config"),
    ):
        txt = _src(rel).read_text(encoding="utf-8")
        assert marker in txt, f"{rel} 未接入 resolve_image_config"


def test_fingerprint_merge_covers_provider_and_budget():
    """_export_cache.py R-2 合并扩展：provider / base_url / max_ai_images 亦纳入指纹。"""
    txt = _src("app/routers/_export_cache.py").read_text(encoding="utf-8")
    for key in ("image_provider", "image_base_url", "max_ai_images"):
        assert key in txt, f"_export_cache.py 指纹合并缺 {key}"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest backend/tests/test_image_config_r238_20261113.py -k "consumption_points_use_resolve_image_config or fingerprint_merge_covers_provider_and_budget" -v`
Expected: FAIL（export.py / charts.py 未引用 resolve_image_config；_export_cache.py 合并缺键）

- [ ] **Step 3: 实现**

`export.py::_prepare_export`（已有 `db` 参数）—— 渲染段之前解析一次、写指纹面、透传：

```python
# _prepare_export 内（渲染段调用 _prepare_export_render / _auto_generate_ai_image_blocks 之前）
from app.config import settings as _settings
from app.services.ai.image_engine import resolve_image_config as _resolve_img
_img_cfg = await _resolve_img(db, _settings)      # db 来自 _prepare_export 签名（async 上下文）
prep["image_config"] = _img_cfg                    # 供 _content_fingerprint 全键合并
# 调用点（原 L5512）：透传 _img_cfg
_ai_converted = await _auto_generate_ai_image_blocks(
    sections, _blocks_cache, config, image_config=_img_cfg, cancel_event=_cancel_ev
)
```

`export.py::_auto_generate_ai_image_blocks` —— 签名加 `image_config` 参数，替换 `_img_cfg` 构造与 provider/budget/concurrency 读取：

```python
async def _auto_generate_ai_image_blocks(
    sections, blocks_cache, config,
    image_config: dict | None = None,        # 新增：DB 解析后的配置；None → 回落 settings（零变化）
    cancel_event: threading.Event | None = None,
) -> int:
    ...
    from app.config import settings as _settings
    from app.services.ai.image_engine import generate_illustration_image
    # 无 image_config（直接调用方）→ 回落 settings，行为与引入前逐字一致
    _img_cfg = image_config if image_config is not None else {
        "image_enabled": _settings.image_enabled,
        "image_api_key_enc": _settings.image_api_key,
        "image_base_url": _settings.image_base_url,
        "image_model": _settings.image_model,
        "image_default_size": _settings.image_default_size,
        "image_price_per_image": _settings.image_price_per_image,
        "image_provider": _settings.image_provider,
        "image_max_concurrency": int(getattr(_settings, "image_max_concurrency", 2) or 2),
        "max_ai_images": int(getattr(_settings, "max_ai_images", 0) or 0),
    }
    _provider = _img_cfg.get("image_provider") or _settings.image_provider
    _max_ai = int(_img_cfg.get("max_ai_images", 0) or 0)                 # 原 _settings.max_ai_images
    ...
    _image_concurrency = int(_img_cfg.get("image_max_concurrency", 2) or 2)
    ...
    url = await generate_illustration_image(None, prompt, style, "16:9", _provider, _img_cfg)
```

`charts.py::generate_ai_image`（已有 `db=Depends(get_db)`）—— 替换 `image_config = {...}` 构造：

```python
from app.services.ai.image_engine import resolve_image_config
image_config = await resolve_image_config(db, settings)   # db 来自 generate_ai_image 签名
```

`_export_cache.py::_content_fingerprint`（R-2 合并，原 L179-188）—— 扩展为全键映射（`_image_generation_signature` 保持同步、不接 db）：

```python
_img_sig = _sh._image_generation_signature()
_img_cfg = prep.get("image_config") or {}
if isinstance(_img_cfg, dict):
    _mapping = {
        "image_enabled": "enabled",
        "image_provider": "provider",
        "image_model": "model",
        "image_base_url": "base_url",
        "max_ai_images": "max_images",
    }
    for _src_key, _dst_key in _mapping.items():
        _v = _img_cfg.get(_src_key)
        if _v is not None:
            _img_sig[_dst_key] = _v
    _ms = _img_cfg.get("image_default_size") or _img_cfg.get("image_size")
    if _ms is not None:
        _img_sig["size"] = str(_ms)
```

> ✅ 关键正确性：`prep["image_config"]` 由 `_prepare_export` 填为**解析后的配置**（无 DB 行时 = settings 兜底，与 `_image_generation_signature()` 的 settings 读取**同值**）→ 无 DB 行路径指纹逐字不变（零行为变化）；有 DB 行时 provider/model/size/base_url/max_images 全键进指纹 → 换库配置自动失效缓存（防命中旧图，R-2/2026-09-27 P1 缺陷类）。
>
> 📌 相对设计文档的机制细化：设计文档 §「消费点」L138 建议 `_export_cache.py`「传入其可用的 db 连接或 None」，但 `_image_generation_signature()` 是**同步函数、无 db 参数**（`_content_fingerprint` 只收 `prep`）。本计划改为**由 `_prepare_export` 填充 `prep["image_config"]` + 扩展 R-2 合并**，**意图不变**（设计文档 L198「`_export_cache` 指纹含 provider/model 变化」逐字达成），机制更干净：不把 `_image_generation_signature` 改 async、不向 `_content_fingerprint` 传 db。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest backend/tests/test_image_config_r238_20261113.py -k "consumption_points_use_resolve_image_config or fingerprint_merge_covers_provider_and_budget" -v`
Expected: PASS

- [ ] **Step 5: 行为护栏（provider/budget 换库 → 指纹变）**

`backend/tests/test_export_high_risk_regressions.py` 追加（复用既有 `_prep()` / `export_mod`）：

```python
def test_image_config_provider_and_budget_change_fingerprint():
    """换 provider / max_ai_images（DB 配置）必须使导出缓存失效（防命中旧图）。"""
    first = _prep()
    second = _prep()
    first["image_config"] = {"image_model": "m", "image_provider": "custom", "max_ai_images": 0}
    second["image_config"] = {"image_model": "m", "image_provider": "dashscope", "max_ai_images": 0}
    assert export_mod._content_fingerprint(first)[1] != export_mod._content_fingerprint(second)[1]
```

Run: `python -m pytest backend/tests/test_export_high_risk_regressions.py::test_image_config_provider_and_budget_change_fingerprint -v`
Expected: PASS

- [ ] **Step 6: 回归（消费点默认行为不变）**

Run: `python -m pytest backend/tests/test_export_high_risk_regressions.py backend/tests/test_export_fixes_r175_20261103.py backend/tests/test_chart_r196_20261109.py backend/tests/test_charts_ai_image_gate.py -v`
Expected: PASS（零回归；无 DB 行时回落 settings，行为一致）

- [ ] **Step 7: Commit**

```bash
git add backend/app/routers/export.py backend/app/routers/charts.py backend/app/routers/_export_cache.py backend/tests/test_image_config_r238_20261113.py backend/tests/test_export_high_risk_regressions.py
git commit -m "feat(image-config): R238 消费点接入 resolve_image_config + 指纹全键合并"
```

---

### Task 6: 前端 types + API 封装

**Files:**
- Create: `frontend/src/types/imageConfig.ts`
- Modify: `frontend/src/api/index.ts`（新增 `imageApi`）
- Test: `frontend/src/tests/imageConfigR238.test.tsx`（新建）

**Interfaces:**
- Produces: `imageApi.getConfig()` / `saveConfig(data)` / `testConnection(data)`；`ImageModelConfig`/`ImageConfigResponse`/`ImageConfigPreset`/`ImageDecisionInfo` 类型。

- [ ] **Step 1: 写失败测试（源码锁）**

```tsx
// frontend/src/tests/imageConfigR238.test.tsx
import { describe, expect, it } from "vitest";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v] as const),
);

const API = RAW["api/index.ts"];

describe("R238 · 图像模型配置前端 API 封装", () => {
  it("imageApi 封装三个端点", () => {
    expect(API, "api/index.ts 应在 glob 覆盖内").toBeTruthy();
    expect(API!).toContain("imageApi");
    expect(API!).toContain('getConfig: () => api.get("/image-config")');
    expect(API!).toContain('saveConfig: (data) => api.put("/image-config", data)');
    expect(API!).toContain('testConnection: (data) => api.post("/image-config/test", data)');
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npx vitest run src/tests/imageConfigR238.test.tsx`
Expected: FAIL

- [ ] **Step 3: 实现**

`frontend/src/types/imageConfig.ts`：

```ts
export interface ImageModelConfig {
  image_enabled: boolean;
  image_api_key_enc?: string;  // 用户裁决(2026-11-13)：GET 不回传，可选
  image_base_url: string;
  image_model: string;
  image_default_size: string;
  image_price_per_image: number;
  image_provider: string;
  image_max_concurrency: number;
  max_ai_images: number;
  has_key?: boolean;
  api_key?: string;
}
export interface ImageConfigPreset { provider: string; base_url: string; model: string }
export interface ImageDecisionInfo {
  routes: { route: string; desc: string }[];
  decision_fn: string;
  prompt_template: string;
  prompt_fields: string[];
}
export interface ImageConfigResponse {
  config: ImageModelConfig;
  presets: Record<string, ImageConfigPreset>;
  decision: ImageDecisionInfo;
}
export interface ImageTestResult { ok: boolean; message: string }
```

`frontend/src/api/index.ts`（`aiApi` 之后追加）：

```ts
import type { ImageConfigResponse, ImageModelConfig, ImageTestResult } from "../types/imageConfig";
// ...
export const imageApi = {
  getConfig: () => api.get<ImageConfigResponse>("/image-config"),
  saveConfig: (data: Partial<ImageModelConfig>) => api.put<{ ok: boolean; warnings: string[] }>("/image-config", data),
  testConnection: (data: { provider: string; base_url: string; api_key: string }) =>
    api.post<ImageTestResult>("/image-config/test", data, { timeout: 60000 }),
};
```

- [ ] **Step 4: 运行测试确认通过**

Run: `npx vitest run src/tests/imageConfigR238.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types/imageConfig.ts frontend/src/api/index.ts frontend/src/tests/imageConfigR238.test.tsx
git commit -m "feat(image-config): R238 前端 imageApi 封装 + 类型"
```

---

### Task 7: 前端页面 `AIImageConfigPage.tsx`

**Files:**
- Create: `frontend/src/pages/AIImageConfigPage.tsx`
- Test: `frontend/src/tests/imageConfigR238.test.tsx`（追加页面源码锁）

**Interfaces:**
- Consumes: `imageApi`（Task 6）、`ImageConfigResponse`（Task 6）。
- Produces: 默认导出 `AIImageConfigPage`，三段式（图像模型配置 / AI 配图协同决策流 / 文生图 Prompt 模板）。

- [ ] **Step 1: 写失败测试（源码锁：5 厂商下拉 + 三段式 + DALL-E-3 预设回填）**

```tsx
const PAGE = RAW["pages/AIImageConfigPage.tsx"];
const TYPES = RAW["types/imageConfig.ts"];

describe("R238 · 图像模型配置页", () => {
  it("页面渲染三段式（配置/协同决策/模板）", () => {
    expect(PAGE, "AIImageConfigPage.tsx 应在 glob 覆盖内").toBeTruthy();
    expect(PAGE!).toContain("图像模型配置");
    expect(PAGE!).toContain("AI 配图协同决策流");
    expect(PAGE!).toContain("文生图 Prompt 模板");
  });
  it("提供商下拉含 5 家 + DALL-E-3 回填 OpenAI 端点", () => {
    for (const v of ["DALL-E-3", "通义万相", "豆包 Seedream", "腾讯混元", "自定义厂商"]) {
      expect(PAGE!).toContain(v);
    }
    expect(PAGE!).toContain("https://api.openai.com/v1");
    expect(PAGE!).toContain("dall-e-3");
  });
  it("决策流三段 structured/illustration/none 与预算旋钮在场", () => {
    expect(PAGE!).toContain("structured");
    expect(PAGE!).toContain("illustration");
    expect(PAGE!).toContain("none");
    expect(PAGE!).toContain("max_ai_images");
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npx vitest run src/tests/imageConfigR238.test.tsx`
Expected: FAIL（页面未创建）

- [ ] **Step 3: 实现**

`frontend/src/pages/AIImageConfigPage.tsx`（三段式，antd `Card`/`Form`/`Select`/`Input`/`Switch`/`InputNumber`/`Alert`/`Typography`/`Button`）：

```tsx
import { useEffect, useState } from "react";
import { App, Button, Card, Form, Input, InputNumber, Select, Space, Switch, Typography, Alert, Divider } from "antd";
import { PictureOutlined, ThunderboltOutlined, FileTextOutlined } from "@ant-design/icons";
import { imageApi } from "../api";
import type { ImageConfigResponse, ImageModelConfig } from "../types/imageConfig";

const { Title, Text, Paragraph } = Typography;

const DISPLAY_OPTIONS = [
  { value: "dalle3", label: "DALL-E-3" },
  { value: "tongyi_wanxiang", label: "通义万相" },
  { value: "doubao_seedream", label: "豆包 Seedream" },
  { value: "tencent_hunyuan", label: "腾讯混元" },
  { value: "custom", label: "自定义厂商" },
];

export default function AIImageConfigPage() {
  const { message: msg } = App.useApp();
  const [form] = Form.useForm();
  const [data, setData] = useState<ImageConfigResponse | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const watchProvider = Form.useWatch("display_provider", form);

  const load = async () => {
    try {
      const { data: d } = await imageApi.getConfig();
      setData(d);
      form.setFieldsValue({
        display_provider: "custom",
        enabled: d.config.image_enabled,
        base_url: d.config.image_base_url,
        model: d.config.image_model,
        api_key: "",
        default_size: d.config.image_default_size,
        concurrency: d.config.image_max_concurrency,
        price_per_image: d.config.image_price_per_image,
        max_ai_images: d.config.max_ai_images,
      });
    } catch (e: unknown) {
      msg.error((e as { message?: string }).message || "加载图像配置失败");
    }
  };

  useEffect(() => { void load(); }, []);

  const onProviderChange = (v: string) => {
    const preset = data?.presets?.[v];
    if (preset) {
      form.setFieldsValue({ provider: preset.provider, base_url: preset.base_url, model: preset.model });
    }
  };

  const handleSave = async () => {
    const values = await form.validateFields();
    setSaving(true);
    try {
      await imageApi.saveConfig({
        provider: values.provider || "custom",
        model: values.model,
        base_url: values.base_url,
        api_key: values.api_key,
        enabled: values.enabled,
        default_size: values.default_size,
        concurrency: values.concurrency,
        price_per_image: values.price_per_image,
        max_ai_images: values.max_ai_images,
      });
      msg.success("图像模型配置已保存");
      void load();
    } catch (e: unknown) {
      msg.error((e as { message?: string }).message || "保存失败");
    } finally { setSaving(false); }
  };

  const handleTest = async () => {
    const values = await form.validateFields();
    setTesting(true);
    try {
      const { data: r } = await imageApi.testConnection({
        provider: values.provider || "custom", base_url: values.base_url, api_key: values.api_key,
      });
      if (r.ok) msg.success("连接成功"); else msg.warning(r.message || "连接失败");
    } catch (e: unknown) {
      msg.error((e as { message?: string }).message || "测试失败");
    } finally { setTesting(false); }
  };

  return (
    <div>
      <Title level={4}><PictureOutlined /> 图像模型配置</Title>
      <Paragraph type="secondary">配置图像模型（AI 概念配图），并明确「数据驱动结构化图表 vs AI 概念配图」的分工决策流与文生图 Prompt 模板。</Paragraph>

      <Card title="图像模型配置" size="small" style={{ marginBottom: 12 }}>
        <Form form={form} layout="vertical">
          <Space wrap>
            <Form.Item name="display_provider" label="提供商" rules={[{ required: true }]}>
              <Select style={{ width: 200 }} options={DISPLAY_OPTIONS} onChange={onProviderChange} />
            </Form.Item>
            <Form.Item name="enabled" label="开启生图" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
          <Form.Item name="base_url" label="Base URL" rules={[{ required: true, message: "请填写 Base URL" }]}>
            <Input placeholder="https://api.openai.com/v1" />
          </Form.Item>
          <Form.Item name="model" label="模型名">
            <Input placeholder="dall-e-3" />
          </Form.Item>
          <Form.Item name="api_key" label="API Key（留空保持原样）">
            <Input.Password placeholder="sk-..." />
          </Form.Item>
          <Space wrap>
            <Form.Item name="default_size" label="默认尺寸">
              <Select style={{ width: 120 }} options={[{ value: "1K", label: "1K" }, { value: "2K", label: "2K" }, { value: "3K", label: "3K" }, { value: "4K", label: "4K" }]} />
            </Form.Item>
            <Form.Item name="concurrency" label="并发数">
              <InputNumber min={1} max={8} />
            </Form.Item>
            <Form.Item name="price_per_image" label="单张价格">
              <InputNumber min={0} step={0.01} />
            </Form.Item>
            <Form.Item name="max_ai_images" label="全局配图预算（0=关闭）">
              <InputNumber min={0} max={1000} />
            </Form.Item>
          </Space>
          <Space>
            <Button type="primary" loading={saving} onClick={handleSave}>保存</Button>
            <Button loading={testing} onClick={handleTest}>测试连接</Button>
          </Space>
        </Form>
      </Card>

      <Card title="AI 配图协同决策流" size="small" style={{ marginBottom: 12 }}>
        {(data?.decision?.routes || []).map((r) => (
          <Alert key={r.route} type="info" showIcon style={{ marginBottom: 8 }} message={`${r.route} — ${r.desc}`} />
        ))}
      </Card>

      <Card title="文生图 Prompt 模板" size="small">
        <Paragraph><Text code>{data?.decision?.prompt_template || "[IMG_GEN_PROMPT: 主题=…；风格=…；构图=…；比例=…；负面词=…]"}</Text></Paragraph>
        <Paragraph type="secondary">字段：{data?.decision?.prompt_fields?.join(" / ")}</Paragraph>
      </Card>
    </div>
  );
}
```

> ⚠️ 若 `watchProvider` 未用，删掉（避免 eslint 未使用告警）；页面按 antd v5 现有页面（AIConfigPage）样式约定写。

- [ ] **Step 4: 运行测试确认通过**

Run: `npx vitest run src/tests/imageConfigR238.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/AIImageConfigPage.tsx frontend/src/tests/imageConfigR238.test.tsx
git commit -m "feat(image-config): R238 前端图像模型配置页"
```

---

### Task 8: 前端路由 + 菜单

**Files:**
- Modify: `frontend/src/App.tsx`（路由）、`frontend/src/components/Layout.tsx`（侧边菜单项）
- Test: `frontend/src/tests/imageConfigR238.test.tsx`（追加路由/菜单源码锁）

**Interfaces:**
- Produces: 路由 `/settings/ai-image` → `AIImageConfigPage`；菜单项「图像模型配置」（`/settings/ai-image`）。

- [ ] **Step 1: 写失败测试（源码锁）**

```tsx
const APP = RAW["App.tsx"];
const LAYOUT = RAW["components/Layout.tsx"];

describe("R238 · 路由与菜单", () => {
  it("App.tsx 注册 /settings/ai-image 懒加载页", () => {
    expect(APP!).toContain('path="/settings/ai-image"');
    expect(APP!).toContain("AIImageConfigPage");
  });
  it("Layout.tsx 侧边菜单含 图像模型配置 于 文本模型配置 之后", () => {
    const aiPos = LAYOUT!.indexOf("文本模型配置");
    const imgPos = LAYOUT!.indexOf("图像模型配置");
    expect(aiPos, "找不到文本模型配置菜单").toBeGreaterThan(-1);
    expect(imgPos, "找不到图像模型配置菜单").toBeGreaterThan(aiPos);
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npx vitest run src/tests/imageConfigR238.test.tsx`
Expected: FAIL

- [ ] **Step 3: 实现**

`App.tsx`：`const AIImageConfigPage = lazy(() => import("./pages/AIImageConfigPage"));` + `<Route path="/settings/ai-image" element={<AIImageConfigPage />} />`（`/settings/ai` 之后）。

`Layout.tsx`：菜单 items 在 `{ key: "/settings/ai", icon: <SettingOutlined />, label: "文本模型配置" }` 之后插入：

```tsx
{ key: "/settings/ai-image", icon: <PictureOutlined />, label: "图像模型配置" },
```

（`Layout.tsx` 顶部 icon import 加 `PictureOutlined`，与 `SettingOutlined` 等并列。）

- [ ] **Step 4: 运行测试确认通过**

Run: `npx vitest run src/tests/imageConfigR238.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/App.tsx frontend/src/components/Layout.tsx frontend/src/tests/imageConfigR238.test.tsx
git commit -m "feat(image-config): R238 路由与侧边菜单"
```

---

### Task 9: 全量验证 + 字节体检

**Files:**
- 无新文件（验证收尾）

- [ ] **Step 1: 后端护栏全跑**

Run: `python -m pytest backend/tests/test_image_config_r238_20261113.py -v`
Expected: ALL PASS

- [ ] **Step 2: 回归（既有图像/导出/图表簇）**

Run: `python -m pytest backend/tests/test_charts_ai_image_gate.py backend/tests/test_chart_r184_image_provider_redaction_20261107.py backend/tests/test_chart_r196_20261109.py backend/tests/test_export_high_risk_regressions.py backend/tests/test_export_fixes_r175_20261103.py backend/tests/test_export_orphan_gc_20261006.py backend/tests/test_charts_r13_and_types_parity_20261004.py -v`
Expected: PASS（零回归）

- [ ] **Step 3: ruff + R57 静态卫生棘轮**

Run: `python -m ruff check backend/app/services/ai/image_engine.py backend/app/services/ai/image_providers/custom.py backend/app/routers/image_config.py backend/app/routers/export.py backend/app/routers/charts.py backend/app/routers/_export_cache.py backend/app/main.py`
Expected: All checks passed

- [ ] **Step 4: 前端 tsc + vitest**

Run: `npx tsc --noEmit && npx vitest run src/tests/imageConfigR238.test.tsx`
Expected: EXIT 0 + PASS

- [ ] **Step 5: 字节体检（生产源纯 CRLF / 测试与文档纯 LF / BOM=0 / FFFD=0）**

Run（PowerShell）：对新增/改动生产源与测试逐文件检查行尾与 BOM（手写脚本或沿用既有 bytecheck 工具）。`custom.py`/`image_config.py` 若新建为 LF，需转 CRLF（`(Get-Content -Raw f) -replace "`r`n","`n" -replace "`n","`r`n" | Set-Content -NoNewline -Encoding utf8`）。
Expected: 生产源纯 CRLF、测试/文档纯 LF、无 BOM/FFFD/孤立 CR

- [ ] **Step 6: 最终 commit（若字节体检改动）**

```bash
git add -A
git commit -m "chore(image-config): R238 字节体检与收尾"
```
