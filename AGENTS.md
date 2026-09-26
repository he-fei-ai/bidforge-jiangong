# AGENTS.md — 专项方案工具箱 · AI Agent 工作边界

> 本文件定义 AI 编程代理（Coder/Agent）在本仓库工作时的行为边界。参考 OpenBidKit Yibiao 的 AGENTS.md 引入（2026-09-22）。
>
> **最近校准：2026-09-23** —— 按代码现状逐条复核。变更要点：① 工作流由 7 步合并为 **6 步**（上传解析 / 项目提取 合并）；② AI 保护机制改为**真实数值**（原「10 分钟恢复」等描述与代码不符，已修正）；③ 图表/配图口径更新为**导出时全自动生图**；④ 目录结构按实际模块重写（原 `services/export/` 不存在）。

## 1. 项目定位

- **产品**：建筑工程专项方案 AI 编制平台（"专项方案工具箱"，前端版本 `5.3.0`）
- **架构**：FastAPI 后端（Python 3.12+，SQLite/WAL）+ React 18 + TypeScript + Vite 前端 + 独立 `chart-gap-filler` 图表子项目
- **核心场景（6 步工作流，2026-09-23 起）**：

  `上传解析`（内嵌子 Tab：文档解析 + 项目提取）→ `目录生成` → `全局事实` → `正文生成` → `审核与预检` → `导出文档`

  - 原「上传解析」「提取项目」两个顶层 Tab 已合并为 **1 个顶层 Tab（`import`）+ 2 个内嵌子 Tab**（`docs` / `extract`）；`WorkflowTabKey` 值域固定为 6 个：`import / outline / facts / content / review / export`。
  - 合并方案与落地记录见 `docs/merge_upload_parse_and_bid_analysis_plan.md`（第 13 节）。
- **默认端口**：后端 `8000`，前端 `5175`（Vite，见 `frontend/package.json`）。

## 2. 目录结构

```
backend/app/
├── routers/            # FastAPI 路由（18 个模块 + ai_config/ 子包）
│   ├── projects.py / schemes.py / sections.py     # 项目 → 方案 → 章节
│   ├── sse_handlers.py         # /api/v1/sse：目录生成、正文生成、事实提取（核心大文件）
│   ├── outline_library.py      # /api/v1/outline-library（专项方案目录库）
│   ├── upload_outline.py       # /api/v1/upload-outline（上传目录识别）
│   ├── global_facts.py         # /api/v1/global-facts（全局事实变量）
│   ├── bid_analysis.py         # /api/v1/bid-analysis（18 项结构化提取）
│   ├── doc_pipeline.py         # /api/v1/documents（解析四层存储）
│   ├── compliance.py           # /api/v1/compliance（符合性/专家论证预检/就绪度）
│   ├── consistency_repair.py   # /api/v1/schemes/{id}/consistency（全文一致性 Agent）
│   ├── review.py               # /api/v1/schemes/{id}/review（审核工作流）
│   ├── charts.py               # /api/v1/charts（预测/Mermaid 生成/渲染/修复）
│   ├── export.py               # /api/v1/schemes/{id}/export（DOCX 导出，最大路由）
│   ├── scheme_catalog.py       # /api/v1/scheme-catalog
│   ├── knowledge.py            # /api/v1/knowledge
│   ├── system.py               # /api/v1/system（后台任务状态栏 + 活动 SSE）
│   ├── ai_config/              # /api/v1/ai 子包（config/connectivity/models/usage/audit/scene_routes）
│   └── _chart_pipeline.py      # 非路由：正文内嵌图表扫描 / 校验 / 登记
├── services/
│   ├── ai/                     # AI 调用链：provider_factory、workflows_base（熔断+自适应并发）、
│   │                           #   prompts/（模板注册表+DB 缓存）、mermaid_*.py、image_providers/
│   ├── doc_pipeline/           # 文档解析四层存储管线（pipeline/doc_chunker/doc_storage/md_structured）
│   └── *.py                    # 图表(chart_payload/chart_validators)、事实(facts_*)、
│                               #   一致性(consistency_scanner/repair_*)、正文(content_*)、
│                               #   目录(outline_*)、审核(audit_*/preflight_engine)、解析(file_parser/ocr)
├── utils/              # safe_log_handler（安全轮转）、log_context（trace 关联 ID）
├── db.py               # 连接初始化（WAL/foreign_keys/busy_timeout）+ 幂等 _migrate()
├── schema_sql.py       # 33 张表结构定义
├── middleware.py       # ApiAuthMiddleware（鉴权）
├── models.py / seed_data.py
├── main.py             # 应用入口（另含 /api/v1/health、/diagnostics/capabilities、/prompts*）
└── config.py           # pydantic-settings 配置

frontend/src/
├── pages/              # 7 个页面：ProjectList / ProjectDetail / SchemeWorkbench(最大文件) /
│                       #   OutlineLibrary / AIConfig / PromptEditor / SecuritySettings
├── components/         # 工作台各 Tab 组件（UploadParseTab/BidAnalysisTab/ContentGenerationTab/
│   │                   #   ParseResultCategoryPanel/DocumentPipelineSummary/TaskStatusBar/...）
│   ├── review/         # ReadinessDashboard（就绪度评分）、ReviewWorkflowPanel（章节审核）
│   ├── MarkdownRenderer.tsx / mermaidRuntime.ts
│   └── Layout.tsx / ActivityHint.tsx
├── hooks/              # useContentGeneration（正文 SSE）、useSchemeLiveTask（后台任务轮询）
├── api/index.ts        # axios 客户端 + 19 个按业务域分组的 API 集合 + sseFetch（30s 建连超时）
├── utils/              # 派生纯函数层（workflowDerived/bidAnalysis/contentEvents/...）+ theme/ui
├── types/              # 前后端类型契约（audit.ts、checkpointSchema.ts 自动生成勿手改）
└── tests/              # vitest 单测（33 个文件）

docs/                   # 改造/迁移方案文档（如 merge_upload_parse_and_bid_analysis_plan.md）
平台智能体设计/          # 产品与技术设计文档 + 模块探索报告（只读参考，非代码）
chart-gap-filler/       # 独立图表补全子项目（docchart/ + tests/）
backend/tests/          # pytest 单测（135 个文件）
logs/backend.log        # 后端日志（5MB × 3 轮转，SafeRotatingFileHandler）
start_all.bat           # Windows 一键启动（GBK + CRLF，勿改编码）
```

## 3. AI Agent 工作规则

### 3.1 必须遵守

1. **代码注释用中文**：所有新注释、docstring、错误消息使用中文（对齐既有风格）。
2. **不改变业务边界**：图表保持全自动生成，**禁止引入人工配图流程**。
3. **配置项默认向后兼容**：新增配置项默认值必须与既有行为等价（"旧行为不变"），可通过环境变量显式开启新行为。
4. **测试先行**：任何修复必须附带 pytest（backend）或 vitest（frontend）用例，验证「不抛异常 + 语义正确 + 反例回归」。
5. **不新增依赖**：新增库需先说明为什么已有依赖不能解决，并更新 `requirements.txt`。
6. **日志级别规范**：ERROR 用于业务阻断；WARNING 用于可恢复异常；DEBUG/INFO 用于正常追踪。全局日志落盘 `logs/backend.log`。
7. **数据库迁移幂等**：所有 DDL 走 `app/db.py::_migrate()` 的「`PRAGMA table_info` 探测 + 条件 `ALTER TABLE ADD COLUMN`」模式；表不存在时跳过而非报错。
8. **改工作流必须同步 3 处**：`frontend/src/utils/workflowDerived.ts`（`WorkflowTabKey` / `NEXT_TAB` / `PREV_TAB`）、`frontend/src/pages/SchemeWorkbenchPage.tsx`（`tabItems` 与步骤号文案）、`frontend/src/tests/workflowDerived.test.ts` + `workflowTabsGuard.test.ts`。三者必须同时改，否则导航链断裂。
9. **不制造临时产物**：不要往仓库里新增调试脚本/输出（`backend/_*.txt`、`_*.py`、根目录 `_exports/` 等均为历史产物，勿扩充）；调试输出请写系统临时目录。

### 3.2 禁止事项

1. **禁止破坏向后兼容**：不允许删除既有 API 端点或修改既有字段的默认值而不提供迁移路径。
2. **禁止绕过鉴权**：所有 `/api/v1/*` 路由必须经过 `app/middleware.py` 的 `ApiAuthMiddleware`（豁免仅 `/api/v1/health`、`/docs`、`/redoc`、`/openapi.json` 及 `OPTIONS` 预检）。
3. **禁止引入人工图表**：违反既定产品约束（全自动 mermaid + chart-json + 导出时自动配图）。
4. **禁止直接 commit 到 main**：所有 AI 产出必须先自测、后由人审核合并。
5. **禁止编辑/删除 `*.bak-*` / `*.bak-preswap`**：这些是人工"文件切换(swap)"备份，用于在旧实现与新实现之间整体替换；它们可能在任何时刻被替换，属于用户的工作流，不是垃圾文件。
6. **禁止手改 `frontend/src/types/checkpointSchema.ts`**：该文件标注 `AUTO-GENERATED`，源为 `backend/app/services/checkpoint_schema.py`，需通过生成工具重出。

### 3.3 常用命令

```bash
# 一键启动（推荐，Windows；自动清端口/装依赖/探活/开浏览器）
start_all.bat

# 端口清不掉时（uvicorn --reload 孤儿 worker 继承监听句柄）先手动清理再启动
powershell -NoProfile -ExecutionPolicy Bypass -File kill_port.ps1 -Ports 8000,5175
# 改动 kill_port.ps1 或 start_all.bat 的清端口段后必须回归（约 30s，需本机可跑 uvicorn）
powershell -NoProfile -ExecutionPolicy Bypass -File kill_port_selftest.ps1

# 后端
cd backend
pip install -r requirements.txt
python -m pytest tests/ -q                    # 全量测试
python -m pytest tests/test_xxx.py -v         # 单文件
# ⚠️ 必须带 --reload-exclude：pytest 的 .pytest_cache / __pycache__ / .pt_* 临时目录
#    会触发 watchfiles 重载风暴，且 pytest 进程会占用 logs/backend.log 句柄
#    （2026-09-23 事故：日志冻结 7 小时 + worker 不接管 socket）
python -m uvicorn app.main:app --reload --reload-exclude *.pytest_cache* --reload-exclude *__pycache__* --reload-exclude *.pt_* --host 0.0.0.0 --port 8000

# 前端
cd frontend
npm install
npm run dev                                   # Vite dev server（:5175）
npm run test                                  # vitest run（33 文件 / 524 用例）
npm run build                                 # tsc && vite build

# 图表子项目
cd chart-gap-filler
python -m pytest tests/ -x -q
```

## 4. 关键设计约束

### 4.1 并发体系

- 全局 AI 并发硬上限：`max_concurrency = 5`（不可超过）。实际执行者是 `AdaptiveConcurrencyController`（`services/ai/workflows_base.py`）：`initial=3, min_c=1, max_c=5`，按 429 / 失败率自适应升降，并被夹在 `[max(min_c, target-2), target]` 区间（防"单向棘轮"式永久钉死在 1）。
- 正文生成：`outline_chapter_concurrency = 2`（2026-09-22 O7 降级）
- 一致性扫描：`consistency_scan_concurrency = 2`
- 一致性修复：`consistency_repair_concurrency = 2`
- 配图：`image_max_concurrency = 2`

**为什么降级**：多个 Semaphore 交叉叠加时，同一时刻理论上能到 9 路并发（3+3+3），会打爆 provider 配额（2026-09-17 单日 769 次 429）。降级到 2 是保守默认，可显式恢复到 3。

### 4.2 AI 调用链

```
sse_handlers.collect_json_response
  → provider_factory.chat (并发信号量 + 熔断器 + 配额冷却 + 对冲)
    → ai_config 候选链
      → 具体 provider (agnes / deepseek / zhipu / sensetime / ...)
```

关键保护机制（数值取自 `app/config.py` 与 `services/ai/workflows_base.py`）：

- **熔断器**（`AnalysisCircuitBreaker`）：失败阈值 `failure_threshold=5`，基础冷却 `cooldown_seconds=15`，**429 时冷却加倍（封顶 120s）**；失败计数窗口 `FAILURE_WINDOW=10s`（窗口外重置）。状态机 CLOSED → OPEN → HALF_OPEN → CLOSED；跳过时审计 `action="circuit_skipped"`。
- **配额冷却**（O2）：`ai_fail_cooldown_seconds=120`（429/402/403/404 后该 provider 冷却 120s），冷却期内每 `ai_fail_probe_every_seconds=10` 才放行 1 次探测。
- **重试分级**（O8）：`ai_retry_on_quota_error=False`（配额/认证类确定性错误直接放弃并切 provider）、`ai_retry_on_non_retryable=True`；章节级 `content_section_retries=1`，退避 `content_retry_backoff=6.0`、限流退避 `content_rate_limit_backoff=20.0`。
- **推理模型兜底**（O3）：`ai_reasoning_max_tokens=4096`，`ai_retry_on_thinking_exhausted=True`（`finish_reason=length` 且正文为空 → `max_tokens × 2` 重试一次）。
- **对冲请求**：`ai_hedge_enabled=True`，`ai_hedge_delay_seconds=20` 后启动下一候选，先成功者胜。
- **降级候选链**：`ai_fallback_chain_max=3`，候选单次超时 `ai_fallback_attempt_timeout=120`；候选按实时成功率排序，成功率 < `ai_provider_dead_success_rate=0.20` 剔除、< `ai_provider_demote_success_rate=0.60` 后置。
- **超时**：`ai_request_timeout=900`，正文 `content_request_timeout=300` / 总时长 `content_total_timeout=660`；SSE 总时长默认 `sse_total_timeout_default=1800`、硬上限 `sse_total_timeout_hard_max=14400`。

### 4.3 图表管线

- **7 类图表（唯一值域）**：`flowchart / gantt / architecture / labor / comparison / layout / timeline`。
- **唯一事实来源**：
  - `services/chart_validators.py`：各类型归一器（`normalize_*`）、校验器（`validate_*`）、类型标签、Mermaid 关键字 → 类型映射、`PIL_RENDERABLE_CHART_TYPES`。
  - `services/chart_payload.py`：图表载荷唯一构造器 `build_chart_envelope` + 唯一解析器 `extract_chart_payload`（兼容全部历史形状，**无需数据迁移**）。
- 全自动流程：正文生成时扫描 `mermaid` / `chart-json` / `ai_image` 代码块 → 校验 → 修复 → 登记到 `chart_predictions`（`routers/_chart_pipeline.py`）。
- 数量上限：每章同类型 **1** 个；全方案同类型默认 **3** 个；`ai_image` 放宽到 **6** 个（仅 `enforce_limits=True` 时生效）。
- **图题优先级**：载荷 `title` > Mermaid `title` > 引导语 > 类型通用名。
- **配图（ai_image）已全自动（2026-09-23 v17）**：
  - 前端 **无"生成配图"按钮、无 `/charts/generate-ai-image` 调用**（`chartsApi.generateAiImage` 已删除）；`MarkdownRenderer.tsx` 中的 `ai_image` 占位卡片仅作信息展示。
  - 真实生图发生在**导出 DOCX 时**：`routers/export.py::_auto_generate_ai_image_blocks`，由导出 config `ai_image_auto_generate` 控制（默认 **True**）。
  - 兼容端点 `POST /api/v1/charts/generate-ai-image` 保留，但 `ai_image_manual_enabled=False`（默认）时直接返回 **409**；设 `AI_IMAGE_MANUAL_ENABLED=true` 可恢复人工/脚本触发。
- **禁止**：任何新增"用户上传/插入图片"或"前端触发生图"的路径。

### 4.4 文档解析四层存储

- 管线：`services/doc_pipeline/pipeline.py` —— `ingest_upload` / `ingest_parse_result` / `sync_extract_layer`（物化到 `doc_extractions`）/ `build_completeness_report` / `compute_freshness` / `run_cross_check` / `detect_cross_source_conflicts` / `purge_document`。
- 层次：原文层 `project_documents` → 解析层 `doc_chunks` → 提取层 `doc_extractions` → 语义层；质量报告在 `doc_validation_reports`。
- **下游全部直接查表**，不经过前端路由：`sse_handlers.build_project_brief` 读 `bid_analysis_items`（`status='success'`）、正文生成逐章注入 `project_brief`（预算 2000 字/章）、`export.py` 校验必填项缺失率、`consistency_scanner.build_global_facts_text` 读 `global_facts`。
- **推论**：改前端 Tab 组织**不涉及**后端契约；反之，改 `bid_analysis_items` / `project_documents` 表结构 = 高风险，需单独立项。

### 4.5 审计日志

- 表：`ai_audit_logs`（provider, model, action, tokens, success, error, **scene**）
- 场景标记 `scene` 是 **`provider_factory.KNOWN_SCENES`** 的键（唯一事实来源，含中文名），
  如 `outline_draft / outline_sublevel / content_draft / content_continue / consistency_repair / facts_extract / chart_fix` 等；
  另有 `ai_config_audit_logs`（**配置**变更审计，见 §4.6 —— 两者不可混用）
- `/api/v1/ai/...` 的 stats / audit-logs 接口按 scene 聚合
- **重要**：任何新增 AI 调用点必须显式传 `scene` 参数（漏传 → 统计归入空场景、且无法被「场景模型路由」单独指定模型）
- 写入为**攒批落库**（满 50 条或 10s flush；应用关闭时 `flush_audit_buffer()`）

### 4.6 AI 配置治理（「文本模型配置」模块）

**存储与密钥**（`ai_config` 表 / `services/crypto.py`）
- 密钥列 `api_key_encrypted` = Fernet(AES-256) 密文；对外只回**末 4 位**（`key_hint`），
  并以 `has_key`（有密文且可解密）/ `key_broken`（有密文解不开）表达状态。
- 密钥来源优先级：`FERNET_KEY` 环境变量 > `data/secret_key.key`（持久化，自动生成）> 兜底临时密钥。
  ⚠️ **`FERNET_KEY` 非法时改用持久化密钥而非换一把随机密钥**（否则库里已存 Key 永久解不开）。
- 密钥回收：`DELETE /api/v1/ai/config/{id}/key`（清空密文；清的是「当前使用」配置时回 `warning`）。

**热更新与缓存**
- 主配置 / 降级链 / 场景路由 / 生效环境 共 4 份缓存，全部由 `invalidate_config_cache()` 失效，
  且**所有写路径都会调用**（保存/删除/切换/降级链/导入/清 Key/场景路由/环境切换）→ 配置改动即时生效，无需重启。
- 缓存写入带**代际号**（`_cache_generation`）：读库期间若发生失效，过期结果**不写回缓存**（防 TOCTOU 钉住一整个 TTL）。
- `AI_CONFIG_CACHE_TTL`（`settings.ai_config_cache_ttl`，默认 300）是唯一 TTL 来源（曾出现「文档写 3600 / 代码硬编码 300」的配置项静默失效）。TTL 只作「外部直接改库」兜底。

**参数校验**
- `provider_factory._RANGE` + `clamp_warnings()` 是数值区间的唯一事实来源（并发 1~5、Max Tokens 256~200000、温度 0~2、超时 10~3600）。
- 前端表单区间必须与之一致；越界值后端会收敛并通过 `warnings[]`（导入为 `clamped_items`）如实回传，**前端必须提示**。

**多环境（可选，默认关闭）**
- `ai_config.env`（空串 = 通用）+ 运行时「当前生效环境」：`ai_runtime_settings.active_env` > `settings.active_env`（`ACTIVE_ENV`）> `''`。
- 环境为空 = **零过滤**（与引入前逐字节一致）；非空时：主配置优先取该环境专用（取不到回落通用）、
  降级链只纳入「通用 + 该环境」、场景路由指向其它环境的配置自动跳过。
- 端点：`GET/PUT /api/v1/ai/env`（写运行时表 + 失效缓存，无需重启）。

**场景模型路由（可选，默认关闭）**
- 表 `ai_scene_routes(scene, config_id)`；选取优先级：调用方显式 `ai_config` > 场景路由配置 > 主配置；表为空 = 行为与旧版一致。
- scene 必须取自 `KNOWN_SCENES`（白名单），且**必须与代码里 `scene="..."` 字面量一致** ——
  双向漂移护栏：`backend/tests/test_ai_config_security_routing.py::TestKnownScenesDrift`。
- 端点：`GET/PUT /api/v1/ai/scene-routes`。

> ✅ **2026-09-25 缺口闭环（三处「配了不生效 / 改了看不见」的静默失效）**
>
> 1. **环境值损坏不再让展示端点 500**：环境标签决定密钥/地址隔离，运行时
>    `resolve_active_env()` 遇非法值必须继续 **fail-closed 抛 `ValueError`**（不可当通用环境用）；
>    但 `GET /ai/config`、`GET /ai/env`、`GET /ai/runtime`、`GET /ai/scene-routes` **不得跟着 500** ——
>    它们正是用户「把环境重置为通用」的恢复入口，改为按通用环境返回数据 + 回传 `env_error`（空串=正常）。
>    `GET /ai/health` 另给独立状态 **`env_corrupt`**，**绝不能混报成 `not_configured`**
>    （否则用户会去重建原本完好的配置）。护栏：`tests/test_ai_config_gapfix_20260925.py::TestEnvCorruptRecovery`。
> 2. **跨环境场景路由必须可见**：`GET /ai/scene-routes` 回传 `active_env`，每项带
>    `config_env` / `env_mismatch`（判定与 `resolve_scene_config` 严格同口径：当前环境非空
>    且配置环境非空且不同 → 运行时跳过并回落主配置）；`PUT` 回传 `warning`
>    （跨环境 / 目标配置没有可用 Key）——**保存成功 ≠ 生效**，必须说清楚。
>    前端显示「跨环境·暂不生效」而不是「已指定」。
> 3. **删配置 / 收密钥不再悄悄改路由**：`DELETE /ai/config/{id}` **级联解除**引用该配置的
>    场景路由并回传 `cleared_scene_routes`（此前残留僵尸行，前端永远显示「配置已删除」红标）；
>    `DELETE /ai/config/{id}/key` 在配置被路由引用时回传 `warning`；导入时 `set_first_active`
>    必须重放 `apply_config_concurrency()`（否则界面上的并发数要等下一次手动保存才生效）。

**调用审计的场景下钻（2026-09-25）**
- `GET /ai/audit-logs` 新增 `scene` 精确筛选 + 回传 `scenes` 可选值（此前 `stats.by_scene` 有聚合、
  明细却只能按供应商筛，看到「某场景失败率高」查不到具体请求）；空白值视为未筛选。
- 配套索引 `idx_ai_audit_scene_created(scene, created_at)`（`_migrate` 幂等创建）：
  既有的 `(action, scene, created_at)` 对 `WHERE scene=?` **用不上**（前导列不在条件里），必须单独建。
- 前端「用量统计 → 按业务场景」标签可点击下钻（自动切 Tab + 回填场景筛选），场景码统一显示中文名，
  历史无埋点的行显示「未标记」而不是空白。


**配置变更审计与版本回滚**
- 表 `ai_config_audit_logs`（action / config_id / detail / **snapshot_json** / client_ip）。
  动作：`create / update / delete / toggle / fallback_chain / import / clear_key / scene_route / env / rollback`。
- `snapshot_json` = `{"before":…, "after":…}` 结构化快照，由 `sanitize_config_snapshot()` 产出，
  **只含字段白名单、绝不含密钥**（密钥仅以 `has_key` 布尔体现）；`GET /ai/config/audit-logs` 另返回 `changes[]`（结构化 diff）与 `rollbackable`。
- `POST /api/v1/ai/config/{id}/rollback {audit_id}` 回滚到某条记录之前：**密钥不参与回滚**、
  数值仍走白名单归一、`is_active` 默认不还原（`include_active=true` 显式开启，且带「零主配置」守卫）、回滚本身也留痕。

### 4.7 提示词治理（✅ 2026-09-24 遗留问题闭环）

**模块与唯一口径**
- 模板注册：`services/ai/prompts/_registry.py`（`_reg()` → `_ALL_PROMPTS`）；
  运行时缓存：`_cache.py`（DB `prompt_templates` 优先、硬编码兜底）。
- **变量口径三函数别混用**：
  - `extract_variables()` —— 原始提取，**含** `{SHARED_*}`（供残留检测）；
  - `extract_user_variables()` —— 排除 `{SHARED_*}`（注册表 `variables` 字段、
    前端列表展示、审计 diff 一律用它；`{SHARED_*}` 由 `_resolve_shared_keys`
    运行时解析，不是调用方入参）；
  - `validate_prompt_variables()` —— 缺失校验，套 `_is_false_positive` 过滤 JSON 示例误报。
- **变量契约**：`PROMPT_VARIABLE_CONTRACTS`（集中声明，勿散落到 `_reg()` 调用处）+
  `check_prompt_variables()` 启动期双向校验（声明↔占位符），漂移打 WARNING。
  新增/修改模板后**必须同步契约表**，否则 `tests/test_prompt_governance.py::TestVariableContracts` 失败。

**版本回滚（G2）**
- `prompt_audit_logs.snapshot_json`（`db._migrate` 幂等补列）存
  `{"before": 正文, "after": 正文}`，由 `settings.prompt_audit_snapshot_enabled`（默认 True）控制；
  超过 `prompt_audit_snapshot_max_chars`（默认 200000）截断并标记 `truncated=True`。
- `POST /api/v1/prompts/{key}/rollback {audit_id}`：无快照的历史行 / 截断快照 / 跨 key → 400/404 拒绝；
  回滚本身也写 `action="rollback"` 审计（可再回滚）。
- `GET /api/v1/prompts/audit-logs` 返回 `changes[]` + `rollbackable`，**不回传原始快照**（减小载荷）。

**上下文预算分配器（G5，默认关闭）**
- `services/prompt_governance.py::allocate_context_budget`：按
  「全局事实 > 目录树 > 资料摘要 > 知识库」优先级水填，下限不超过平均份额。
- 入口 `sse_handlers._apply_prompt_context_budget`，由 `prompt_context_budget`（默认 0）开启。
- ⚠️ 正文 user 上下文的全局事实/知识库**必须**用 `【标签】：` 形式，否则分配器识别不到段
  （回归护栏：`tests/test_prompt_governance.py::TestContextBudget`）。

**提示词注入防护（G6，默认关闭）**
- `prompt_governance.guard_material / scan_prompt_injection / redact_sensitive`，
  入口 `sse_handlers._guard_external_material`，由 `prompt_injection_defense`（默认 False）开启。
- 只加围栏 + 告警，**绝不阻断生成**、**绝不改写资料正文**（与「数据真实性红线」冲突）。
- 脱敏为保守匹配（sk- / gsk_ / api_key= 等），不误伤 GB/JGJ 标准编号。

**缓存新鲜度**
- `_cache._get_prompt_cache` 在「缓存为空 / DB 路径变化 / **库文件 mtime 变新**」三种情况自动重载；
  `reload_prompt_cache()` 会一并重置 `_loaded_db_path` 与 `_loaded_db_mtime`。
  外部进程直写 `prompt_templates` 后读取方无需重启即可看到新值
  （护栏：`tests/test_prompt_cache_external_write.py`）。

### 4.8 全局事实九大章节分类体系（2026-09-24）

**定位与口径**
- 依据：建办质〔2018〕31号（九大章节）、建办质〔2021〕48号、住建部37号令及地方实施细则。
- 九大章节定义、危大工程六大类分类、阈值判定（`HAZARD_THRESHOLDS`）的唯一单一事实源在
  `services/scheme_classification.py`；`services/facts_classification.py` 只做「全局事实侧」
  映射与派生，两侧口径天然一致，**禁止在另一处重复维护阈值表**。
- 全局事实在既有 22 类 `category` 之上叠加 4 个**正交维度**（不替换 category，前端下拉/
  分组排序/自动分类器口径完全不变）：
  - `chapter`：九大章节归属（overview/basis/plan/technique/safety/personnel/acceptance/
    emergency/calc_drawings，空串=未分类）；
  - `fact_attr`：事实属性（quantitative 定量 / qualitative 定性 / relation 关系 / norm 规范）；
  - `source_kind`：数据来源（bid_doc / drawing / survey / overall_plan / manual）；
  - `is_shared`：跨章节共性事实（多章节复用，避免重复提取；`shared_chapters` 记录复用章节集合）。

**落库与派生**
- `global_facts` 表新增 4 列（`db.py::_migrate` 幂等补列，均有默认值）+ `idx_global_facts_chapter` 索引。
- 提取管线在 `run_post_extract_normalize`（`facts_extractor.py`）内 `apply_fact_dimensions` 自动标注
  （确定性规则、无 AI、无 DB 往返），由 `facts_chapter_classification`（默认 True）控制；设 False 回旧行为。
- **历史行惰性派生兜底**：读路径 `_fact_dimension_fields`（`routers/global_facts.py`）在列值为空时
  原地派生（不写库），历史事实不会因未标注而在章节视图/按章节精选中被丢弃。
- `FactItem.to_db_row` / `persist_extraction` 的 INSERT 列必须与 `schema_sql` 严格一致（尾部 4 列）。

**新端点（`routers/global_facts.py`）**
- `GET /api/v1/global-facts/category-map`：九大章节码-中文名、category→chapter、事实属性/来源枚举（前端单一事实源）。
- `GET /api/v1/global-facts/chapters`：按章节重组事实 + 每章应提取字段/已覆盖/缺失字段差集（字段完整性的唯一出口）。
- `POST /api/v1/global-facts/danger-check`：方案名称关键词 → 六大类危大识别 + 从事实抽取定量参数 →
  危大/超规模阈值判定（只读诊断，不改数据）。

**下游生成接入（默认关闭，向后兼容）**
- 正文生成：`sse_handlers._render_facts_text` 新增 `chapter` 参数做「章节内事实前置、其余作补充」的
  稳定分区（绝不丢事实）；由 `facts_chapter_inject`（默认 False）控制。
- `_load_facts_rows` 多读一列 `chapter`（5 元组，旧 3/4 元组按长度自适应不破坏既有调用方）。

## 5. 常见坑

1. **`ai_audit_logs` 列不齐**：运行库可能未迁移出 `scene` 列，代码已做降级 INSERT（`provider_factory._flush_audit_buffer`）。
2. **`consistency_scan_cache` 表缺失**：需要重启后端触发 `_migrate`（表不存在时 `_migrate` 记 debug 跳过，不报错，容易被忽略）。
3. **`{max}` 类误报**：提示词里 JSON 示例 `{max: ...}` 会被误识别为变量占位符。修 `prompts/_registry._VARIABLE_PATTERN` 而非移除示例。✅ 2026-09-24 已全链路对齐：`validate_prompt_variables` / `render_prompt` 残留检测 / `check_prompt_variables`（变量契约）三条告警路径共用同一判据 `_is_false_positive`，不会再因该已知误报刷 WARNING。
4. **前端 401 短路**（R14 已修复）：`api/index.ts` 的 `authGuard` 会在收到 401 后短路所有请求，改凭据前必须先 `clearAuthShortCircuit()`。鉴权规则：token 取 `X-API-Key` 优先、其次 `Authorization: Bearer`；`api_auth_token` 为空时**完全放行**（本地单机默认）。
5. **R13 事务内 `cur is None`**（已修复）：全局单连接 + aiosqlite 下 `execute()` 可能返回 None，`_chart_pipeline.py` 已加判空。
6. **日志冻结 / worker 不接管 socket**（2026-09-23 事故）：僵尸 pytest 持有 `logs/backend.log` 句柄导致 5MB 轮转 rename 失败 → 日志永久冻结。**日志时间戳停止增长 = 先怀疑日志系统冻结，而非业务代码 bug**；排障顺序：看日志 mtime → 查端口 8000 归属进程 → `py-spy dump` → 清理僵尸 pytest → 重启 uvicorn。已在 `utils/safe_log_handler.py` 做轮转失败降级追加，前端 `sseFetch` 加 30s 建连超时。
7. **本仓的"文件切换(swap)"工作流**：目录内存在 `*.bak-preswap`、`*.bak-<时间戳>` 备份；会话进行中文件可能被**整体替换**。改动前先 `read_file` 重读目标文件，改后复验自己的写入仍在（搜索自己新增的标识符）；不要把 `.bak-*` 当源码编辑或删除。
8. **临时产物与生成物**：根目录 `_exports/`、`_tmp_screenshot.png`、`vitest_result.json`、`_openapi.json`、`patch_preview_pipeline_summary.py`，以及 `backend/_*.txt`、`backend/_*.py` 均为历史调试产物，**不要当源码维护、也不要新增同类文件**。
9. **改非 UTF-8 文件前必须先探测编码**（`start_all.bat` 是 GBK/CP936）：编辑类工具按 UTF-8 读 GBK 文件会把每个非法字节变成 U+FFFD 再整文件写回，**中文永久丢失**（2026-09-23 由 AI 编辑造成过一次）。正确做法：只做字节级 / 显式 CP936 编解码修改，或保证新增内容纯 ASCII；改完用两条断言验收——「UTF-8 严格解码必须失败」+「字节里不得出现 EF BF BD」。误改后可从 `%APPDATA%\Qoder\SharedClientCache\cache\workingSpace\<uuid>__<文件名>` 的快照找回原始字节（本仓无 git，这是唯一可靠备份源）。
10. **`netstat` 报的端口占用者可能早已死去**：`uvicorn --reload` 的监听 socket 由 reloader 父进程创建、worker 子进程继承句柄；父进程一死，`taskkill /PID <父>` 报「找不到进程」，而端口仍长期 LISTENING，看着像「权限不足」，真正持句柄的是活着的孤儿 worker（命令行带 `--multiprocessing-fork`，`ParentProcessId` 指向那个已死 PID）。必须按 PID 递归找出**存活后代**再杀 —— 见根目录 `kill_port.ps1`（`start_all.bat` 第 1 步已接入）。
11. **路由签名里的 `Request` 不能写成 `Request | None`**：ai_config 的多条端点写成 `request: Request = None`（默认值只为兼容「单测直接调用路由函数」）。**注解写联合类型会让 FastAPI 识别不出 Request**，转而把它当请求参数（注入失效、审计拿不到 `client_ip`、OpenAPI 多出一个 `request` 参数）。护栏：`tests/test_api_contract.py::test_ai_config_request_injection_not_exposed_as_param`。
12. **`GET /ai/config` 的 `presets` 与 `GET /ai/models` 的 `providers` 形状不同**：前者是厂商字典**原样**（`model` 键、`plans` 可缺省），后者被归一为 `AIProviderPreset`（`default_model` 键）。前端混用会「取值为 undefined 但不报错」。契约类型已分别建模为 `AIConfigPresetRaw` / `AIProviderPreset`（`frontend/src/types/aiConfig.ts`）。

## 6. 测试基线

- 后端：`backend/tests/` **163 个文件**（`test_*.py`），运行 `python -m pytest tests/ -q`；2026-09-26 基线 **2944 passed, 4 skipped, 3 xfailed**（含新增 `test_ai_config_gapfix_20260925.py` 的 31 个用例）。
- 前端：`frontend/src/tests/` **45 个文件**（`*.test.ts(x)`），运行 `npm run test`（= `vitest run`）；2026-09-26 基线 **668 用例全通过**（含新增 `aiConfigGapfix20260925.test.tsx` 的 9 个用例）。`vitest.config.ts` 默认 `environment: "node"`，需 DOM 的用例在文件头声明 `// @vitest-environment jsdom`。
  - ⚠️ 页面级用例若依赖 `/ai/stats` 等**模块级 TTL 缓存**（`AIConfigPage` 的 `statsCache` 是模块单例），
    跨用例会互相污染（上一个用例缓存的 `null` 会让下一个用例永远拿不到 mock 数据）；
    需要「按 mock 数据渲染」时在用例里 `vi.mock("../utils/ttlCache", ...)` 换成永不命中的空实现。
- 前端类型检查：`npx tsc --noEmit`（`npm run build` 内已含）必须为 0 错误。
- AI 配置模块专项：`python -m pytest tests/test_ai_config_module.py tests/test_ai_config_request_mode.py tests/test_ai_config_security_routing.py tests/test_ai_config_env_rollback.py tests/test_ai_config_gapfix_20260925.py tests/test_api_contract.py tests/test_provider_factory.py tests/test_crypto.py -q`（2026-09-26 基线 **274 passed**）。
- **提示词模块专项**：`python -m pytest tests/test_prompt_governance.py tests/test_prompt_rollback_governance.py tests/test_prompt_shared_variables.py tests/test_prompt_variables.py tests/test_prompt_variables_r6.py tests/test_prompt_cache_db_path.py tests/test_prompt_cache_external_write.py tests/test_content_deep_audit_20260923_b.py -q`。
- **全局事实九大章节分类专项（2026-09-24）**：`python -m pytest tests/test_facts_classification.py tests/test_scheme_classification.py tests/test_global_facts_routes.py tests/test_global_facts_field_completeness.py -q`。
- 图表子项目：`cd chart-gap-filler && python -m pytest tests/ -x -q`。
- 每次提交前必须跑相关测试；跑 pytest 尽量在 dev server 停止时进行，并带 `--reload-exclude`（见 §3.3、§5.6）。

## 7. 变更记录模板

提交 AI 修复时，PR 描述需包含：

```
【变更】<文件路径>
【背景】<BUG ID 或现象描述>
【根因】<一句话>
【修复】<一句话>
【默认行为】向后兼容 / 需显式开启
【验证】pytest/vitest 命令 + 通过数
【影响面】列出涉及的其他模块
```
