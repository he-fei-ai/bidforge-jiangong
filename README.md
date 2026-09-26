# 专项方案工具箱 · BidForge

> 建筑工程专项方案 AI 编制平台 · v5.3.0

一键把招标文件、图纸、地勘、总控方案等资料，转化为**规范合规、图表齐全、可导出 DOCX** 的专项施工方案。

- **后端**：FastAPI · Python 3.12+ · SQLite (WAL)
- **前端**：React 18 · TypeScript · Vite · Ant Design 5
- **图表**：全自动 Mermaid + Chart-JSON + AI Image（导出时自动生图）
- **合规**：对齐建办质〔2018〕31 号 · 建办质〔2021〕48 号 · 住建部 37 号令

---

## 目录

- [功能亮点](#功能亮点)
- [工作流](#工作流)
- [快速开始](#快速开始)
- [项目结构](#项目结构)
- [环境配置](#环境配置)
- [AI 配置与场景路由](#ai-配置与场景路由)
- [测试基线](#测试基线)
- [关键设计约束](#关键设计约束)
- [常见问题](#常见问题)
- [贡献指南](#贡献指南)
- [许可证](#许可证)

---

## 功能亮点

| 能力 | 说明 |
|---|---|
| **6 步工作流** | 上传解析 → 目录生成 → 全局事实 → 正文生成 → 审核预检 → 导出文档 |
| **文档解析四层存储** | 原文层 → 解析层 → 提取层 → 语义层，支持 Word / Excel / PDF / 扫描件 / 旧版 `.doc` / `.wps` |
| **18 项结构化提取** | 从招标资料里自动抽取项目关键指标（工程规模、地点、工期、质量目标、危大工程分类等） |
| **全局事实 22 类 + 九大章节分类** | 事实按 category 归类，并叠加九大章节 (chapter) / 事实属性 (fact_attr) / 数据来源 (source_kind) / 跨章节复用 (is_shared) 四个正交维度 |
| **全自动图表管线** | 7 类图表：flowchart / gantt / architecture / labor / comparison / layout / timeline；正文生成时扫描、校验、修复、登记，导出 DOCX 时自动生成配图 |
| **审核与就绪度评分** | 章节审核工作流 + ReadinessDashboard 就绪度评分 + 符合性预检 |
| **提示词治理** | 模板注册表 + 变量契约校验 + 版本回滚（snapshot 快照）+ 上下文预算分配器 + 提示词注入防护 |
| **AI 配置治理** | Fernet AES-256 加密密钥、多环境隔离、场景模型路由、配置变更审计 + 一键回滚 |
| **AI 熔断 + 自适应并发** | `AdaptiveConcurrencyController` + `AnalysisCircuitBreaker` + 配额冷却 + 对冲请求 + 降级候选链 |
| **DOCX 导出** | 图表自动配图、目录编号收敛、超字数压缩、正文一致性修复 Agent |

---

## 工作流

```
┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
│ 上传解析 │→│ 目录生成 │→│ 全局事实 │→│ 正文生成 │→│ 审核预检 │→│ 导出文档 │
│ (docs +  │  │  (outline)│  │  (facts)  │  │ (content) │  │  (review) │  │  (export) │
│  extract)│  │          │  │          │  │          │  │          │  │          │
└──────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────┘
```

- **上传解析**（1 个顶层 Tab + 2 个内嵌子 Tab）：文档解析 + 18 项结构化提取，共享同一份项目资料
- **目录生成**：AI 生成/优化方案目录，可上传已有目录识别、可查专项方案目录库
- **全局事实**：从项目资料抽取全局变量，自动标注九大章节归属
- **正文生成**：按目录逐章生成 Markdown，SSE 实时推送进度、字数、失败明细
- **审核预检**：符合性检查 + 专家论证预检 + 就绪度评分
- **导出文档**：一键导出 DOCX，图表自动配图、目录编号统一

---

## 快速开始

### 前置依赖

- **Python 3.12+**
- **Node.js 20+**（含 npm）
- **Windows 10/11** 或 Linux/macOS

### 一键启动（Windows，推荐）

```bash
start_all.bat
```

自动完成：清端口 → 装依赖 → 探活 → 打开浏览器。首次运行会在 `backend/.env` 生成默认配置（从 `.env.example` 复制），需手动填写 AI API Key。

### 手动启动

```bash
# 后端
cd backend
pip install -r requirements.txt
python -m uvicorn app.main:app \
  --reload \
  --reload-exclude *.pytest_cache* \
  --reload-exclude *__pycache__* \
  --reload-exclude *.pt_* \
  --host 0.0.0.0 --port 8000

# 前端
cd frontend
npm install
npm run dev    # Vite dev server at :5175
```

启动完成后：
- 前端页面：<http://localhost:5175>
- 后端 API：<http://localhost:8000>
- API 文档：<http://localhost:8000/docs>

> ⚠️ **必须**带 `--reload-exclude`：pytest 的 `.pytest_cache / __pycache__ / .pt_*` 会触发 watchfiles 重载风暴，占用 `logs/backend.log` 句柄导致日志冻结（2026-09-23 事故）。

---

## 项目结构

```
.
├── backend/                        # FastAPI 后端
│   ├── app/
│   │   ├── routers/                # 18 个路由模块 + ai_config/ 子包
│   │   │   ├── sse_handlers.py     # SSE 核心（目录/正文/事实生成，最大文件）
│   │   │   ├── outline_library.py  # 专项方案目录库
│   │   │   ├── global_facts.py     # 全局事实（九大章节分类）
│   │   │   ├── bid_analysis.py     # 18 项结构化提取
│   │   │   ├── doc_pipeline.py     # 文档解析四层存储
│   │   │   ├── compliance.py       # 符合性预检 / 专家论证
│   │   │   ├── consistency_repair.py # 全文一致性 Agent
│   │   │   ├── review.py           # 审核工作流
│   │   │   ├── charts.py           # 图表生成/渲染/修复
│   │   │   ├── export.py           # DOCX 导出（最大路由）
│   │   │   ├── _chart_pipeline.py  # 正文内嵌图表扫描/校验/登记
│   │   │   └── ai_config/          # AI 配置治理（config/connectivity/models/usage/audit/scene_routes）
│   │   ├── services/
│   │   │   ├── ai/                 # AI 调用链：provider_factory、workflows_base、prompts、mermaid_*、image_providers
│   │   │   ├── doc_pipeline/       # 文档解析四层管线
│   │   │   ├── chart_validators.py # 图表类型校验（唯一事实源）
│   │   │   ├── chart_payload.py    # 图表载荷构造/解析
│   │   │   ├── scheme_classification.py # 九大章节 & 危大工程六大类（唯一事实源）
│   │   │   └── prompt_governance.py # 提示词治理
│   │   ├── db.py                   # 连接初始化 + 幂等 _migrate
│   │   ├── schema_sql.py           # 33 张表结构定义
│   │   ├── middleware.py           # ApiAuthMiddleware
│   │   ├── main.py                 # 应用入口
│   │   └── config.py               # pydantic-settings 配置（APP_VERSION = "5.3.0"）
│   ├── tests/                      # 167 个 pytest 文件
│   ├── requirements.txt
│   ├── requirements-ocr.txt        # 可选：扫描件 OCR
│   ├── requirements-windows.txt    # 可选：Windows PDF 导出
│   └── pytest.ini
├── frontend/                       # React 18 + TypeScript + Vite
│   ├── src/
│   │   ├── pages/                  # 7 个页面：ProjectList / ProjectDetail / SchemeWorkbench / OutlineLibrary / AIConfig / PromptEditor / SecuritySettings
│   │   ├── components/             # 26+ 组件（含 review/ 子目录）
│   │   ├── hooks/                  # useContentGeneration / useSchemeLiveTask
│   │   ├── api/index.ts            # axios + 19 个按业务域分组的 API 集合 + sseFetch
│   │   ├── utils/                  # 派生纯函数层
│   │   ├── types/                  # 前后端类型契约
│   │   └── tests/                  # 46 个 vitest 文件
│   └── package.json
├── chart-gap-filler/               # 独立图表补全子项目
├── docs/                           # 迁移方案与变更记录
├── 平台智能体设计/                  # 产品与技术设计文档 + 模块探索报告
├── AGENTS.md                       # AI Agent 工作边界（重要！）
├── 产品需求文档.MD
├── 技术架构文档.MD
├── 软件编译计划书.MD
├── start_all.bat                   # Windows 一键启动（GBK 编码，勿改）
├── start_backend.ps1               # PowerShell 单独启动后端
├── stop_backend.ps1
├── kill_port.ps1                   # 端口强制清理（识别孤儿 worker）
└── .gitignore
```

---

## 环境配置

`backend/.env`（从 `.env.example` 复制）：

| 变量 | 说明 | 默认 |
|---|---|---|
| `FERNET_KEY` | 加密 AI API Key 的密钥（留空则自动生成持久化密钥） | 空 |
| `API_AUTH_TOKEN` | 后端接口鉴权 Token（空 = 本地单机放行） | 空 |
| `OCR_ENGINE` | OCR 引擎：`auto / tesseract / rapidocr / vision / off` | `auto` |
| `MINERU_PROVIDER` | 云端解析兜底：`agent / accurate`（空 = 关闭） | 空 |
| `LEGACY_OFFICE_ENABLED` | 是否启用旧版 Word 转换（`.doc`/`.wps`） | `True` |
| `ACTIVE_ENV` | 当前 AI 生效环境（空 = 通用） | 空 |
| `PROMPT_CONTEXT_BUDGET` | 提示词上下文预算（默认 0 = 关闭） | 0 |
| `PROMPT_INJECTION_DEFENSE` | 提示词注入防护（默认关闭） | `False` |
| `FACTS_CHAPTER_INJECT` | 正文按章节注入全局事实（默认关闭） | `False` |
| `AI_IMAGE_MANUAL_ENABLED` | 允许人工触发 AI 配图（默认关闭，全自动化） | `False` |

---

## AI 配置与场景路由

在页面 **AI 配置** Tab 内维护多套 provider 配置：

1. **主配置**：`agnes / deepseek / zhipu / sensetime / ...` 至少激活一个
2. **降级候选链**：主配置失败时按成功率自动切换（`ai_fallback_chain_max=3`）
3. **场景模型路由**：可为不同业务场景（如正文生成 vs 图表修复）指定不同模型
4. **多环境隔离**：可按 `dev / staging / prod` 隔离密钥，避免跨环境泄露

**内置 AI 保护机制**：
- **熔断器**：失败阈值 5 次 → 冷却 15s（429 时加倍至 120s 封顶）
- **配额冷却**：429/402/403/404 后该 provider 冷却 120s，每 10s 放行 1 次探测
- **重试分级**：配额类错误直接切 provider，章节级重试 1 次（退避 6s）
- **对冲请求**：20s 后启动下一候选，先成功者胜
- **推理模型兜底**：`finish_reason=length` 且正文为空时自动 `max_tokens × 2` 重试
- **自适应并发**：`AdaptiveConcurrencyController` 初始 3、[1, 5] 之间按失败率升降

**图表全自动生图（v17）**：
- 前端**无"生成配图"按钮**，配图在导出 DOCX 时自动触发
- 兼容端点 `POST /api/v1/charts/generate-ai-image` 保留，默认返回 409
- 需要人工/脚本触发生图时，设 `AI_IMAGE_MANUAL_ENABLED=true`

---

## 测试基线

### 后端

```bash
cd backend
python -m pytest tests/ -q
```

- **2026-09-26 基线**：**2944 passed, 4 skipped, 3 xfailed**
- 167 个测试文件

专项：

```bash
# AI 配置模块（8 个文件，274 用例）
python -m pytest tests/test_ai_config_module.py tests/test_ai_config_request_mode.py \
  tests/test_ai_config_security_routing.py tests/test_ai_config_env_rollback.py \
  tests/test_ai_config_gapfix_20260925.py tests/test_api_contract.py \
  tests/test_provider_factory.py tests/test_crypto.py -q

# 提示词治理
python -m pytest tests/test_prompt_governance.py tests/test_prompt_rollback_governance.py \
  tests/test_prompt_shared_variables.py tests/test_prompt_variables.py \
  tests/test_prompt_variables_r6.py tests/test_prompt_cache_db_path.py \
  tests/test_prompt_cache_external_write.py -q

# 全局事实九大章节分类
python -m pytest tests/test_facts_classification.py tests/test_scheme_classification.py \
  tests/test_global_facts_routes.py tests/test_global_facts_field_completeness.py -q
```

### 前端

```bash
cd frontend
npm run test    # vitest run，668 用例
npm run build   # tsc && vite build（类型检查 + 打包）
```

- **2026-09-26 基线**：**668 用例全通过**
- 46 个 vitest 文件

### 图表子项目

```bash
cd chart-gap-filler
python -m pytest tests/ -x -q
```

---

## 关键设计约束

### 1. 图表类型唯一值域

只允许 7 类：`flowchart / gantt / architecture / labor / comparison / layout / timeline`

- 事实源：`services/chart_validators.py`（校验器）+ `services/chart_payload.py`（构造/解析器）
- 数量上限：每章同类型 **1** 个；全方案同类型默认 **3** 个；`ai_image` 放宽到 **6** 个
- 图题优先级：载荷 `title` > Mermaid `title` > 引导语 > 类型通用名

### 2. 并发上限

- **全局 AI 硬上限**：`max_concurrency = 5`
- 正文生成：`outline_chapter_concurrency = 2`
- 一致性扫描 / 修复：均为 `2`
- 配图：`image_max_concurrency = 2`

### 3. 审计日志

- `ai_audit_logs`：AI 调用审计（含 `scene` 场景字段，唯一事实源 `provider_factory.KNOWN_SCENES`）
- `ai_config_audit_logs`：AI 配置变更审计（`snapshot_json` 白名单字段，密钥仅以 `has_key` 布尔体现）
- 攒批落库：满 50 条或 10s flush

### 4. 数据库迁移幂等

所有 DDL 走 `app/db.py::_migrate()` 的 `PRAGMA table_info` 探测 + 条件 `ALTER TABLE ADD COLUMN` 模式；表不存在时跳过。

### 5. 日志落盘

- 全局落盘 `logs/backend.log`
- 5MB × 3 轮转（`SafeRotatingFileHandler`）
- 轮转失败自动降级为追加模式（2026-09-23 事故修复）

### 6. 合规分类

- **九大章节**（建办质〔2018〕31 号）：overview / basis / plan / technique / safety / personnel / acceptance / emergency / calc_drawings
- **危大工程六大类**（建办质〔2021〕48 号）：`scheme_classification.HAZARD_THRESHOLDS` 是唯一事实源
- **禁止**在其它模块重复维护阈值表

---

## 常见问题

### Q1：端口被占用

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File kill_port.ps1 -Ports 8000,5175
```

`uvicorn --reload` 的监听 socket 由父进程创建、worker 子进程继承句柄。父进程一死，`netstat` 仍会报死 PID 为占用者，`taskkill /PID <死 PID>` 报「找不到进程」，端口却长期 LISTENING。`kill_port.ps1` 会递归找出存活后代再杀。

### Q2：日志时间戳停止增长

**先怀疑日志系统冻结**，不是业务代码 bug：
1. 看 `logs/backend.log` 的 mtime
2. 查端口 8000 归属进程
3. `py-spy dump` 或 `tasklist /v` 查僵尸 pytest
4. 清理僵尸 pytest → 重启 uvicorn

### Q3：`.env` 里 FERNET_KEY 变了，历史密钥解不开

**不要**换一把新的 FERNET_KEY。改成用 `data/secret_key.key`（持久化自动生成）——那是历史密钥的来源。否则库里已存的所有 provider Key 永久解不开。

### Q4：SSE 连接无响应

- 检查 `sse_total_timeout_default=1800s` / 硬上限 `sse_total_timeout_hard_max=14400s`
- 前端 `sseFetch` 有 30s 建连超时保护
- 日志里查 `circuit_skipped` / `quota_cooldown` 事件

### Q5：AI 提示词变量校验报警告

见 `AGENTS.md` §4.7。新增/修改模板后必须同步 `PROMPT_VARIABLE_CONTRACTS` 表，否则 `tests/test_prompt_governance.py::TestVariableContracts` 会失败。

### Q6：导出的 DOCX 图表缺失

- 图表在**导出时**才生成，不是正文生成时
- 检查导出 config 里的 `ai_image_auto_generate=True`（默认开启）
- 检查 `chart_predictions` 表是否有对应登记记录

### Q7：改工作流步骤需要动哪里

**必须同时改 3 处**，否则导航链断裂：
1. `frontend/src/utils/workflowDerived.ts`（`WorkflowTabKey` / `NEXT_TAB` / `PREV_TAB`）
2. `frontend/src/pages/SchemeWorkbenchPage.tsx`（`tabItems` 与步骤号文案）
3. `frontend/src/tests/workflowDerived.test.ts` + `workflowTabsGuard.test.ts`

---

## 贡献指南

### 开发前

1. **必读**：`AGENTS.md` —— 定义了 AI Agent 工作边界、禁止事项、常见坑
2. **必读**：`技术架构文档.MD`、`产品需求文档.MD`
3. 参考 `平台智能体设计/` 下的模块探索报告理解各模块设计意图

### 提交规范

```
【变更】<文件路径>
【背景】<BUG ID 或现象描述>
【根因】<一句话>
【修复】<一句话>
【默认行为】向后兼容 / 需显式开启
【验证】pytest/vitest 命令 + 通过数
【影响面】列出涉及的其他模块
```

### 硬性规则

- **代码注释用中文**（docstring / 错误消息对齐既有风格）
- **禁止破坏向后兼容**：不允许删除既有 API 端点或修改字段默认值
- **禁止绕过鉴权**：所有 `/api/v1/*` 路由必须走 `ApiAuthMiddleware`
- **禁止引入人工图表**：图表保持全自动（唯一例外是 `AI_IMAGE_MANUAL_ENABLED=true` 时的兼容端点）
- **禁止手改** `frontend/src/types/checkpointSchema.ts`（`AUTO-GENERATED`，源为 `backend/app/services/checkpoint_schema.py`）
- **禁止编辑/删除** `*.bak-*` / `*.bak-preswap`（用户「文件切换」备份）
- **不新增依赖**：先说明为什么已有依赖不能解决，并更新 `requirements.txt`
- **数据库迁移必须幂等**：走 `_migrate()` 的探测 + 条件 ALTER 模式
- **日志级别规范**：ERROR 业务阻断 / WARNING 可恢复异常 / DEBUG-INFO 正常追踪

---

## 许可证

内部使用，遵循项目内部授权条款。

---

## 关联资源

- [AGENTS.md](./AGENTS.md) —— AI Agent 工作边界
- [产品需求文档.MD](./产品需求文档.MD)
- [技术架构文档.MD](./技术架构文档.MD)
- [软件编译计划书.MD](./软件编译计划书.MD)
- [docs/](./docs/) —— 迁移方案与变更记录
- [平台智能体设计/](./平台智能体设计/) —— 产品与技术设计文档
