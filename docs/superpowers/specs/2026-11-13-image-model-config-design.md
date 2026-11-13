# 图像模型配置（AI 配图协同）设计文档

> 日期：2026-11-13 ｜ 轮次：R238（暂定）
> 用户需求：在主界面菜单栏「文本模型配置」TAB 下新增「图像模型配置」；主要配置图像模型，解决 AI 配图协同：
> 明确「数据驱动结构化图表 vs AI 概念配图（DALL-E-3 / 通义万相 / 豆包 Seedream / 腾讯混元 / 自定义厂商）」
> 的分工决策流与配套文生图 Prompt 模板。

---

## 一、目标与范围

### 1.1 目标

在现有「文本模型配置」（`/settings/ai` → `AIConfigPage.tsx`）之外，新增独立的「图像模型配置」入口，让用户：

1. **配置图像模型**（主 provider + 参数，可测试连接，持久化到数据库）；
2. **看清 AI 配图协同决策流**（结构化图表 vs AI 概念配图 vs 纯文字的分工）；
3. **了解/配置文生图 Prompt 模板**（`[IMG_GEN_PROMPT: …]` 格式）与全局配图预算。

### 1.2 用户已确认的关键决策

| 决策 | 选择 |
|---|---|
| 入口形式 | **侧边菜单新增独立菜单项**（「图像模型配置」，紧跟「文本模型配置」下方）→ 新路由 `/settings/ai-image` → 新页面 |
| 配置存储 | **数据库持久化**（新建 `image_model_config` 表，单条活动配置） |
| DALL-E-3 | **复用自定义厂商（base_url）路径**——新增通用 OpenAI 兼容 `custom` 适配器，DALL-E-3 与自定义厂商共用；不建 DALL-E-3 专用适配器 |
| 图像模型形态 | **单条活动配置**（非多条 + 可配降级链；当前架构即单 provider + 内部降级链） |
| 协同决策与模板 | **仅前端展示 + 配预算旋钮**；不改动后端 `route_visual_strategy` / `parse_img_gen_prompt` 的判定逻辑 |

### 1.3 非目标（YAGNI）

- 不支持多条图像配置 + 手动拖拽降级链（改 `image_engine` 降级链逻辑，超出本次范围）。
- 不把协同决策的关键词表/风格映射做成可配置（保持 `route_visual_strategy` 纯函数单一事实源）。
- 不新增 DALL-E-3 专用适配器（`custom` 已覆盖 OpenAI 兼容协议）。
- 不改动文本模型配置页 / 既有图像生成链路行为（仅消费点读取方式改为「DB 优先、settings 兜底」，默认行为不变）。

---

## 二、现状盘点（R237 后）

### 2.1 后端已具备

- `backend/app/services/ai/image_engine.py`：
  - `generate_illustration_image(provider, prompt, style, section_size, provider_name, image_config)` —— 主 provider + 内部降级链 `["qwen_image","dashscope","agnes"]`，MD5 缓存（成功 12h / 失败 10min TTL）。
  - `route_visual_strategy(text)`（§8.2）：协同决策纯函数，返回 `{"route":"structured"|"illustration"|"none", "chart_type":…, "reason":…}`。
  - `parse_img_gen_prompt(text)`（§8.3）：解析 `[IMG_GEN_PROMPT: 主题=…；风格=…；构图=…；比例=…；负面词=…]`。
  - `IMAGE_SCALE_PRESETS`（§8.1）：`{"screen":2,"print":3,"vector":4}`。
- `backend/app/services/ai/image_providers/`：7 家适配器（`agnes`/`dashscope`/`volcengine`/`hunyuan`/`zhipu`/`stepfun`/`baidu`），基类 `ImageProviderAdapter`（`provider_id`/`default_model`/`supported_sizes`/`supported_ratios`/`generate`/`test_connection`/`list_models`）。
  - `dashscope` 已是 **OpenAI 兼容协议**（`POST {base_url}/images/generations`，payload `{model,prompt,n,size,quality}`，解析 `data[0].url`）——`custom` 适配器以此为蓝本。
- `backend/app/config.py` Settings：`image_provider`/`image_model`/`image_base_url`/`image_api_key`/`image_enabled`/`image_default_size`/`image_max_concurrency`/`image_price_per_image`/`max_ai_images`（**当前全部 env 驱动，无 DB**）。

### 2.2 配置消费点（本次需改为「DB 优先、settings 兜底」）

| 文件 | 消费方式 |
|---|---|
| `backend/app/routers/export.py`（L4636） | `_settings.image_base_url` 等 → 组装 `_img_cfg` → `generate_illustration_image(...)` |
| `backend/app/routers/charts.py`（L864） | `settings.image_*` → `image_config` → `generate_ai_image` 端点 |
| `backend/app/routers/_export_cache.py`（L96） | `getattr(_s, "image_provider"/"image_model"/"image_base_url")` → 导出指纹 |

### 2.3 前端已具备

- 侧边菜单（`Layout.tsx`）已有「文本模型配置」`/settings/ai`（`SettingOutlined`）。
- 路由（`App.tsx`）已有 `/settings/ai`、`/settings/prompts`、`/settings/prompt-metrics`、`/settings/security`。
- `AIConfigPage.tsx` 页内 Tabs：`configs`（模型配置）/ `audit`（用量日志）。
- 无任何图像模型配置界面。

---

## 三、架构设计

### 3.1 总体分层

```
前端 AIImageConfigPage.tsx（/settings/ai-image）
        │  GET/PUT /api/v1/image-config、POST /api/v1/image-config/test
        ▼
后端 routers/image_config.py（新）
        │  读/写
        ▼
schema_sql.py 新增 image_model_config 表（单行 id='primary'）
        ▲
        │ 消费点（export.py / charts.py / _export_cache.py）改为 DB 优先、settings 兜底
        ▼
image_engine.generate_illustration_image(provider_name, image_config)
        │
        ▼
ProviderRegistry.get_adapter(provider_id)  ← 新增 custom 适配器（OpenAI 兼容，覆盖 DALL-E-3）
```

### 3.2 后端组件

#### 3.2.1 表：`image_model_config`（`schema_sql.py` 新增）

单行活动配置（`id TEXT PRIMARY KEY = 'primary'`），`CREATE TABLE IF NOT EXISTS`，无需 `_migrate` 补列（新表）。

```sql
CREATE TABLE IF NOT EXISTS image_model_config (
    id TEXT PRIMARY KEY,              -- 固定 'primary'（单条活动配置）
    provider TEXT DEFAULT 'custom',   -- custom / dashscope / volcengine / hunyuan / zhipu / stepfun / baidu / agnes
    model TEXT DEFAULT '',            -- 留空用厂商默认模型
    base_url TEXT DEFAULT '',         -- 留空用厂商默认端点
    api_key_encrypted TEXT DEFAULT '',-- ENC: 加密前缀（复用 app.crypto）
    enabled INTEGER DEFAULT 1,        -- 1 = 开启生图（image_enabled 语义）
    default_size TEXT DEFAULT '2K',   -- 1K/2K/3K/4K
    concurrency INTEGER DEFAULT 2,    -- image_max_concurrency
    price_per_image REAL DEFAULT 0.0, -- 单张成本（仅计量/展示）
    max_ai_images INTEGER DEFAULT 0,  -- 全局配图预算（0 = 关闭预算）
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
```

说明：`max_ai_images` 原为 `settings.max_ai_images`（G2 预算），现一并纳入表，前端预算旋钮读写它。

#### 3.2.2 适配器：`custom`（`image_providers/custom.py` 新增）

- `CustomAdapter(ImageProviderAdapter)`，`provider_id="custom"`，`default_model=""`，`supported_sizes=["1K","2K","3K","4K"]`，`supported_ratios=["1:1","3:4","4:3","16:9","9:16","2:3","3:2","21:9"]`。
- `generate()`：`POST {base_url}/images/generations`，payload `{model, prompt, n:1, size, quality:"standard", style?}`；解析 `data[0].url`，无 `url` 时尝试 `data[0].b64_json`（转 data URL）。
- `test_connection()`：`GET {base_url}/models` 200 判定（与 dashscope 同法）。
- `list_models()`：`GET {base_url}/models` 取 `data[].id`，失败回落 `[default_model]`。
- 注册进 `image_providers/__init__.py` 的 `initialize_registry()`：`ProviderRegistry.register("custom", CustomAdapter)`。

**DALL-E-3 预设**（前端展示，非硬编码适配器）：选择「DALL-E-3」→ 自动填 `base_url=https://api.openai.com/v1`、`model=dall-e-3`、`provider=custom`；用户可改。DALL-E-3 的 `size` 合法值（`1024x1024`/`1792x1024`/`1024x1792`）由 `custom` 透传，`default_size` 档位映射不强制。

#### 3.2.3 路由：`routers/image_config.py`（新增）

- `GET /api/v1/image-config`：读 `image_model_config` 行；**无行则回落**组装 `settings.image_*`（行为与现状一致，前端首开有数据）。返回 `{config: {...}, presets: {...}, decision: {...}}`：
  - `presets`：5 家展示厂商的预设，**由适配器类的 `default_model` / 默认 `base_url` 派生**（DALL-E-3 → `custom` + `base_url=https://api.openai.com/v1`、`model=dall-e-3`；通义万相 → `dashscope`；豆包 Seedream → `volcengine`；腾讯混元 → `hunyuan`；自定义厂商 → `custom` 空值，用户手填）。
  - `decision`：`route_visual_strategy` 的决策说明 + `parse_img_gen_prompt` 模板格式说明（只读展示数据）。
- `PUT /api/v1/image-config`：校验（provider 合法 + size 档位 + 数字范围）→ 加密 key → UPSERT 单行。返回 `{ok, warnings?}`。
- `POST /api/v1/image-config/test`：body `{provider, base_url, api_key}` → `ProviderRegistry.get_adapter(provider).test_connection(...)`。返回 `{ok, message}`。
- 注册进 `main.py`（挂 `/api/v1` 前缀，与既有 AI 路由一致）。

#### 3.2.4 消费点改造（DB 优先、settings 兜底）

抽一个共享函数 `image_engine.resolve_image_config(db, settings) -> dict`（`db` 可为 None），三处消费点改调它：
- `export.py`：`_img_cfg = resolve_image_config(db, settings)`（该端点有 `db=Depends(get_db)`）；`generate_illustration_image(None, prompt, style, "16:9", provider, _img_cfg)`。
- `charts.py`：`image_config = resolve_image_config(db, settings)`（`generate_ai_image` 有 `db`）。
- `_export_cache.py`：指纹读取同源（传入其可用的 db 连接或 None）。

**默认行为不变**：无 DB 行时回落 `settings`，逐字段一致；有 DB 行时以 DB 为准（DB 行 `enabled=0` → `image_enabled=False` 短路语义保持）。

### 3.3 前端组件

#### 3.3.1 路由与菜单

- `App.tsx`：`const AIImageConfigPage = lazy(() => import("./pages/AIImageConfigPage"))`；`<Route path="/settings/ai-image" element={<AIImageConfigPage />} />`。
- `Layout.tsx`：`{ key: "/settings/ai-image", icon: <PictureOutlined />, label: "图像模型配置" }`，置于 `{ key: "/settings/ai", ... }` 之后、`/settings/prompts` 之前。

#### 3.3.2 页面：`AIImageConfigPage.tsx`（新，三段式）

- **① 图像模型配置**（`Card` + `Form`）：
  - 启用开关（`enabled`）。
  - 提供商下拉：`DALL-E-3` / `通义万相` / `豆包 Seedream` / `腾讯混元` / `自定义厂商`；选择后自动回填 `provider`/`base_url`/`model` 预设（`presets`），用户可改。
  - API Key（`Input.Password`，后端加密存储，回显掩码）、Base URL、模型名、默认尺寸（`1K/2K/3K/4K`）、并发数（`1..8`）、单张价格（`InputNumber`）。
  - 「测试连接」按钮（POST `/image-config/test`）+ 「保存」按钮（PUT `/image-config`）。
- **② AI 配图协同决策流**（只读 `Card` + `Alert`/`Timeline`）：
  - 展示 `route_visual_strategy` 决策树：正文段 → 命中结构化图表关键词（`gantt`/`architecture`/`flowchart`）→ `structured`；否则需氛围/示意（效果图/示意图/三维/实景/渲染/氛围/鸟瞰/透视/封面）→ `illustration`；否则 `none`。
  - 全局预算 `max_ai_images` 旋钮（`InputNumber`，0=关闭，>0=分段择优上限），读写 `config.max_ai_images`。
- **③ 文生图 Prompt 模板**（只读 `Card` + `Typography`）：
  - 展示 `[IMG_GEN_PROMPT: 主题=…；风格=…；构图=…；比例=…；负面词=…]` 模板格式与字段说明（对应 `parse_img_gen_prompt` 的 `subject/style/composition/ratio/negative`）。

#### 3.3.3 API 封装（`frontend/src/api/index.ts`）

- `imageApi.getConfig()` → GET `/image-config`；`imageApi.saveConfig(data)` → PUT `/image-config`；`imageApi.testConnection(data)` → POST `/image-config/test`。
- 类型定义：`frontend/src/types/imageConfig.ts`（`ImageModelConfig`/`ImageConfigResponse`/`ImageConfigPreset`/`ImageDecisionInfo`）。

### 3.4 数据流

```
挂载 → GET /image-config → config（DB 或 settings 兜底）+ presets + decision
   │
   ├─ 选 provider → 回填 presets[provider]（base_url/model）
   ├─ 点测试连接 → POST /image-config/test → ok/message
   └─ 点保存 → PUT /image-config（key 加密入库）→ 成功回显
导出时 → resolve_image_config(settings) → 读 DB（有行）或 settings（无行）→ generate_illustration_image
```

---

## 四、错误处理

- **PUT 校验失败**：provider 不在合法集 / size 非档位 / 数字越界 → 400 + `{message}`（前端 `msg.error` 展示）。
- **测试连接失败**：适配器 `test_connection` 返回 False / 网络异常 → `{ok:false, message}`（前端警示，不阻断保存）。
- **DB 写失败**：`retry_db_op` 兜底重试（database is locked / disk I/O error），耗尽后 500 + `{message}`。
- **密钥**：`PUT` 用 `app.crypto.encrypt_api_key` 加密；`GET` 不回传明文（回传 `has_key: bool` 掩码）。
- **无 DB 行回落 settings**：`resolve_image_config` 逐字段用 `getattr(settings, ...)` 兜底，绝不抛异常。

---

## 五、测试策略

### 5.1 后端护栏（新测试文件 `backend/tests/test_image_config_r238_20261113.py`）

| 组 | 用例 |
|---|---|
| A · 表/路由契约 | GET 无行回落 settings；PUT 写入后 GET 读回（key 掩码、非明文）；PUT 校验失败 400 |
| B · custom 适配器 | `provider_id=="custom"`；`generate` 命中 `data[0].url` / `b64_json`（mock aiohttp）；`test_connection` 200/非200；`supported_sizes` 档位 |
| C · 消费点 | `resolve_image_config` 无行==settings / 有行==DB（enabled=0 短路语义）；三消费点同源（`_export_cache` 指纹含 provider/model 变化） |
| D · 协同决策展示 | GET 返回 `decision` 含 `route_visual_strategy` 三态说明 + `parse_img_gen_prompt` 模板格式；`route_visual_strategy` 未改（回归） |

### 5.2 前端（`frontend/src/tests/imageConfigR238.test.tsx`）

- provider 下拉 5 项（DALL-E-3/通义万相/豆包 Seedream/腾讯混元/自定义厂商）渲染；选 DALL-E-3 回填 `base_url=https://api.openai.com/v1`、`model=dall-e-3`。
- 决策流三段（structured/illustration/none）文案在场；`max_ai_images` 旋钮读写。
- 测试连接/保存调用对应 API。

### 5.3 回归

- 既有图像相关测试簇：`test_charts_ai_image_gate` / `test_chart_r184_image_provider_redaction` / `test_chart_r196_20261109` / `test_export_high_risk_regressions` / `test_export_fixes_r175_20261103` / `test_export_orphan_gc_20261006` —— 零回归（消费点改造默认行为不变）。
- `tsc`、`ruff`、R57 静态卫生棘轮。

---

## 六、行为变化披露

| 项 | 变化 | 默认影响 |
|---|---|---|
| `image_model_config` 表 | 新增 | 零（无行时回落 settings，行为与现状一致） |
| `custom` 适配器 | 新增注册 | 零（新符号；未选 custom 不触发） |
| `GET/PUT/POST /api/v1/image-config` | 新增端点 | 零（纯加法式） |
| 消费点 | 改调 `resolve_image_config` | **零**（无 DB 行逐字段回落 settings；有行时才覆盖） |
| 侧边菜单/路由/页面 | 新增 | 零（新入口） |

全部**加法式**：新表/新适配器/新端点/新页面，不改既有函数签名、不改 `route_visual_strategy`/`parse_img_gen_prompt` 判定逻辑、不改既有图像生成默认行为。

---

## 七、运维说明

- **需重启后端**（新增 `image_config` 路由 + `custom` 适配器注册 + `schema_sql` 建表；若服务热载则热载）。`CREATE TABLE IF NOT EXISTS` 幂等，旧库自动建表，零迁移。
- **前端需重建**（新增路由/页面/菜单项 + `api/index.ts` 封装）。
- 零 DDL 迁移脚本（新表由 `SCHEMA_SQL` 幂等创建）；零新增环境变量（DB 配置优先，`.env` 仍作兜底）。
