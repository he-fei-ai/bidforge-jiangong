# 集成 Oh My Pi (omp) 作为 AGENT 的可行性报告

> 目标：将 GitHub 开源项目 `can1357/oh-my-pi`（以下简称 **omp**）作为 AGENT 集成进本平台（j:\编程\专项方案工具箱 / 招投标方案平台），辅助**目录生成**、**正文生成**、**审核与修复**三大环节。
> 工作机制/流程参考：`OpenBidKit_pi_agent_工作机制报告.md`、`OpenBidKit_pi_agent_优化建议方案.md`
> 分析性质：只读探查 + 架构研判，**未修改任何源码**
> 分析日期：2026-11-12
> 信息来源：两份参考文档（静态）、omp 仓库 README 与 v18.18.6 发布页（web 抓取）、本平台 `backend/app` 源码探查（code-explorer 代理 + 直读 `provider_factory.py`）

---

## 0. 执行摘要（TL;DR）

| 项 | 结论 |
|----|------|
| **核心前提** | ⚠️ 必须纠正概念混淆：**omp ≠ 参考文档里的"Pi Agent"**。参考文档的 Pi Agent 是 OpenBidKit 基于**商汤日日新**的投标领域专用 Agent 运行时；omp 是**通用 AI 编程 Agent**（类 Claude Code/Aider），与商汤无关。 |
| **omp 能否承担参考文档中 Pi Agent 的角色？** | ✅ **能**。omp 天然是"文件工作区 + 多步推理 + 工具调用"的 Agent，与参考文档描述的文件工作区模式（files[]+output_file 回收）高度同构，可直接扮演该角色。 |
| **当前平台 AI 编排现状** | 平台已自研一套**非常成熟**的 Provider 抽象层（`provider_factory.py` + `BaseProvider.chat/stream` + 多 provider 自动降级链 + 熔断/限流/对冲/JSON 修复），所有生成能力统一收敛到 `chat_with_fallback()` / `collect_json_response()`。**没有任何外部 AI Agent 进程集成先例**。 |
| **可行性** | ✅ **可行，但有条件**。推荐 **混合架构**：轻量结构化调用留在现有 Provider 层；把 omp 作为"重任务文件工作区 Agent"（`OmpTaskAgent`）承担超长上下文/多步编排任务，复用参考文档的设计模式。 |
| **最大风险** | ① 进程边界（Python 后端 ↔ Rust 二进制 omp）的会话/冷启动管理；② 安全沙箱（omp 有写文件/执行命令能力，必须隔离工作区、禁止触碰业务库）；③ 概念误用（若仅把 omp 包成 `chat()` Provider，会浪费其 Agent 工具/多步推理价值）；④ omp 版本活跃（v18.18.6 距 main 已 1101 提交），RPC 协议需锁定版本。 |
| **推荐落地路径** | 先以 **RPC stdio（无 Node 依赖，匹配现有 subprocess CLI 先例）** 做 PoC，验证 Windows 下 `omp --mode rpc` 可用；再在 `BaseProvider` 上挂 `OmpProvider`（chat 包装，零改动接入降级链）作为低成本入口；最后实现 `OmpTaskAgent`（文件工作区模式，对齐参考文档 Pi Agent 角色）。全程**默认关闭、开关可控、零回归**（对齐优化建议方案原则 B）。 |

---

## 1. 概念澄清：两个"Pi"不是一回事（最重要前提）

用户指令把"参考工作机制报告"与"集成 omp"并列，但二者指向**两个不同实体**，必须先讲清，否则整份方案会建立在错误前提上。

### 1.1 参考文档中的 "Pi Agent"（OpenBidKit）

- 来源：`J:\编程\OpenBidKit 易标\OpenBidKit_Yibiao-main-2026-09-14`（第三方软件，非本仓库）。
- 本质：**投标领域专用 Agent 运行时**，封装**商汤日日新融合模态大模型**（SenseTime Pi）。
- 四层管线：业务层（`outlineGenerationTaskV2.cjs` 等）→ 调度层（`agentService.cjs`）→ 运行时层（`piRuntimeService.cjs`，Pi SDK 主循环）→ 接入层（`piSessionFactory` / `agentOpenAiProxy` 等）。
- 核心模式（本方案要**复用其设计思想**的部分）：
  - **文件工作区模式**：`files:[{path,content}]` 写入工作区，Agent 读文件、把结果写回 `output_file`，业务层从文件回收。
  - **阈值切换**：prompt 超过上下文窗口比例时，从普通对话切到 Agent 文件模式，突破单次上下文上限。
  - **持久任务断点**：`persistent_task` + `resume` + `continueTask` 实现多阶段串行、可续跑。
  - **JSON 校验重试**：`json_validation_schemas` + `buildRetryPrompt`。
  - **业务库保护**：Agent 不写库，程序读输出文件后自行回写。
  - **自检与诊断**：启动前环境自检、失败诊断。
  - **OpenAI 兼容代理**：Pi SDK → 本地代理 → 真实 LLM。

### 1.2 目标集成物 "oh-my-pi" (omp)

- 来源：`https://github.com/can1357/oh-my-pi`（MIT 许可，由 Stencil Labs 维护，fork 自 Mario Zechner 的 `pi-mono`）。
- 本质：**通用 AI 编程 Agent**（约 8 万行 Rust + TypeScript）。关键词：`coding-agent`、`multi-provider`、`terminal`、`tui`、`rust`。**与商汤无关，与投标领域无关**。
- 能力清单（README 实证）：
  - **60+ 模型提供商 / 1000+ 模型**：Anthropic、OpenAI、Gemini、DeepSeek、Qwen、MiniMax、Moonshot、Bedrock、Azure、OpenRouter、硅基流动、本地 Ollama/vLLM/LM Studio（OpenAI 兼容 `/v1/models`）等。
  - **31 个内置工具**：`read`/`write`/`edit`/`ast_edit`/`grep`/`glob`（均为进程内实现，非 fork/exec）、`bash`（自研 bash 5.3 引擎，含 58 个内置命令）、`lsp`、`debug`（DAP）、`task`（子代理扇出）、`web_search`（25 个后端）、`github`、`generate_image`、`browser`、`computer` 等。
  - **原生跨平台**：linux-x64/arm64、darwin-x64/arm64、**win32-x64/arm64**（Windows 无需 WSL）。
  - **四种接入方式**：① 交互式 TUI（`omp`）；② 一次性 `omp -p "..."`；③ **RPC（`omp --mode rpc`，stdio NDJSON 双向）**；④ ACP（Agent Client Protocol，JSON-RPC）；另有 **Node SDK** `@oh-my-pi/pi-coding-agent`（`createAgentSession` / `session.prompt()` / 类型化事件）。
  - **会话持久化**：`--resume`、磁盘 session 存储、内存/项目级 `retain/recall` 记忆。
  - **多角色路由 / 顾问模型 / 子代理**：天然支持"主模型 + advisor 旁观模型 + 并行 subagent"。

### 1.3 结论

> **omp 不是"商汤 Pi 模型"，而是一个可以"扮演"参考文档中 Pi Agent 角色的通用 Agent 运行时。** 参考文档的价值在于贡献了**设计模式**（文件工作区、阈值切换、断点续跑、JSON 校验、库隔离、自检）——这些模式与 omp 的能力**天然同构**，因此可以作为 omp 在本平台落地的蓝图，而不是照搬 OpenBidKit 代码。

---

## 2. 当前平台 AI 编排现状（探查结论）

> 探查范围：`backend/app/services/ai/provider_factory.py`（231KB 核心）、`providers/base.py`、`routers/_sse_outline_build.py`、`routers/sections.py`、`services/review_autofix.py`、`routers/sse_handlers.py`、`config.py` 等。

### 2.1 现状一句话

**现有 AI 编排是"自己直接调 LLM API"，没有任何外部 Agent 运行时。** 统一收敛到：

```
业务工作流（_sse_outline_build / sections / review_autofix / consistency_*）
   └─ collect_json_response(messages, validate_fn)  /  chat_with_fallback(messages)
        └─ provider_factory.chat_with_fallback()
             ├─ _resolve_chat_config(scene)         # 场景级路由
             ├─ _fallback_chain()                   # 主配置 + DB 其余配置(priority) + 内置 agnes 兜底
             ├─ 熔断 / 限流 / 对冲请求 / 配额冷却 / 思考吞噬重试
             └─ _attempt_candidate() → BaseProvider.chat()/stream()
                  ├─ OpenAICompatibleProvider
                  └─ AnthropicCompatibleProvider
```

### 2.2 关键事实（带路径/行号）

| 事实 | 证据 |
|------|------|
| 唯一 Provider 插槽是 `BaseProvider.chat()/stream()` | `services/ai/providers/base.py:182/199`；仅两个实现 `openai_compatible.py`、`anthropic_compatible.py` |
| 多 provider 降级链已成熟 | `provider_factory.py:_fallback_chain()` L1474、`_chat_with_fallback_inner()` L4374、熔断 `workflows_base.py:113`、对冲 L4404、`ai_fallback_chain_max=3`（`config.py:272`） |
| 目录生成是纯内存 prompt，无文件 workspace | `_sse_outline_build.py:1250` `collect_json_response(..._validate_outline)`；知识库在内存拼接；超长由 `prompt_context_budget` 预算器裁切 |
| 正文生成有超长上下文处理但无 Agent | `sections.py`：`max_tokens_for_budget` L104、`clip_text_notice` L83、`context_length_limit` L2480 |
| 审核修复是场景化 `chat_with_fallback` 调用 | `review_autofix.py:806` 直接 `chat_with_fallback(...scene="review_autofix")` |
| **无外部 AI Agent 进程集成先例** | grep `omp/oh-my/stdio/node_sdk/child_process`（AI 语义）= 0 命中；现有 `subprocess/Popen` 仅用于 `soffice`/`mmdc`/`ruff` 等 CLI 工具 |
| 本地/内网模型已支持 | `config.py:243 ai_allow_private_base_url` + `PROVIDER_PRESETS` 的 custom base_url（`provider_factory.py:427`） |

### 2.3 对"集成 omp"的含义

集成 omp **不是替换某个 agent 运行时**（因为不存在），而是三选一：

- **(A) 替换/新增 Provider**：在 `BaseProvider` 上包一个 `OmpProvider`，实现 `chat()/stream()`，内部走 omp RPC。**零改动**接入所有可靠性基建（降级链、熔断、限流、审计、`/ai/stats`）。
- **(B) 旁路 / 任务级 Agent**：在工作流层直接驱动 omp（文件工作区模式），绕过轻量 LLM 抽象，承担重任务。对齐参考文档 Pi Agent 角色。
- **(C) 混合（推荐）**：轻量结构化调用留现有层；omp 以 `OmpTaskAgent` 形式承担超长上下文/多步编排。

---

## 3. 可行性研判：omp 能否承担参考文档中 Pi Agent 的角色

逐个对照参考文档的设计模式，看 omp 是否具备对应能力：

| 参考文档 Pi Agent 设计模式 | omp 是否有对应能力 | 落地可行性 |
|--------------------------|-------------------|-----------|
| 文件工作区（files[] + output_file 回收） | ✅ 原生：`read`/`write`/`edit` 工具，Agent 读写工作区文件 | 直接可用 |
| 阈值切换（超上下文切 Agent） | ✅ omp 文件工作区天然突破窗口；也可由后端按 token 数决定路由 | 直接可用 |
| 持久任务断点（persistent + resume） | ✅ omp 有磁盘 session + `--resume` + 记忆 | 需后端管理 session 生命周期 |
| 多阶段串行（continueTask） | ⚠️ omp 自身是单轮 Agent；多阶段由**后端编排**（参考 OpenBidKit 的 `continueTask` 由业务层实现，omp 不必内置） | 后端复用现有阶段机 |
| JSON 校验重试 | ✅ 后端已有 `_validate_outline` / `collect_json_response` 修复；omp 输出经后端校验 | 直接复用 |
| 业务库保护（Agent 不写库） | ✅ 靠**工作区沙箱**实现：omp 只在隔离目录写，后端读输出回写 DB | 安全设计必做 |
| 自检与诊断 | ✅ omp 自管理；后端加 preflight（`omp --version`、模型可达性） | 轻量 |
| OpenAI 兼容代理 | ➖ omp 本身是 OpenAI 兼容客户端/服务端，但本场景是"把 omp 当 Agent 用"，不需要这层 | 不适用 |

**研判结论**：omp 的能力面**覆盖并超出**参考文档 Pi Agent 的角色需求。唯一需要"自建"的是多阶段编排与 session 生命周期，而这两件事 OpenBidKit 本就是由**业务层**（`continueTask` / `persistent_task`）实现的——本平台也已有 SSE 阶段机（`_sse_outline_build.py:_advance_outline_phase` 等），可直接承载。

---

## 4. 集成架构方案

### 4.1 方案 A：OmpProvider（chat 包装，低成本入口）

```
BaseProvider (抽象)
   └─ OmpProvider.chat()/stream()
        └─ 启动/复用 omp 子进程（omp --mode rpc --no-session --no-ui）
             └─ 发送 {"id","type":"prompt","message": <拼接后的 messages 文本>}
             └─ 读取 {"type":"response"} 帧 → 返回文本
```

- **优点**：改动最小，自动接入降级链/熔断/审计；可作为"又一个 provider"配置进 `ai_config` 参与降级。
- **代价/局限**：仅用到了 omp 的"LLM 对话"外壳，**浪费了工具/子代理/文件工作区/多步推理**等核心 Agent 价值。omp 在此模式下等于一个"带壳的 LLM 客户端"。
- **适用**：作为"降级链候选之一"或"特定场景 provider"，快速验证进程边界可行性。

### 4.2 方案 B：OmpTaskAgent（文件工作区模式，对齐参考文档角色）⭐推荐主体

这是参考文档 Pi Agent 的**忠实实现**，也是 omp 价值最大化的形态：

```
后端工作流（如 _sse_outline_build / sections / review_autofix）
   │
   ├─ 1. 构造隔离工作区 dir = workspace/scheme_{id}/task_{ts}/
   ├─ 2. 写入输入文件：
   │      outline-skeleton.json / knowledge-base.md / global-facts.md
   │      / section-context.md / restored-content.md / ...
   │      （内容复用平台已有 prompt 构建函数，如 createChildrenPrompt 的素材）
   ├─ 3. 写入领域 system 指令（复用平台现有 prompt 模板，
   │      追加"只写 output_file、不碰业务库、不输出图表代码块"等约束）
   ├─ 4. 驱动 omp（长生命周期 session，--resume 复用）：
   │      omp --mode rpc --no-ui  （或 Node SDK createAgentSession）
   │      发 prompt："读取 X.md，按约束产出 Y.md"
   ├─ 5. 从 output_file 回收结果 → 平台校验器（_validate_outline 等）
   ├─ 6. 校验失败 → 追加修复指令重试（对齐 json_validation_schemas）
   ├─ 7. 成功后后端把结果写回业务 DB（omp 绝不直连 DB）
   └─ 8. SSE 上报进度（复用现有 _advance_outline_phase / _await_with_stats）
```

- **关键设计**：后端持有 **一个长生命周期 omp session**（避免每章冷启动），用 `task` 子代理做并行章节、用 advisor 模型做质量旁审——这些正是 omp 相对现有 Provider 层的**增量价值**。
- **与现有架构关系**：不替换 `provider_factory`，而是新增一个**服务**（`services/omp_agent.py`）。轻量调用（知识抽取、JSON 解析、快速检查）继续走现有 Provider 层；重任务（整卷目录、整章扩写、整卷一致性修复）走 `OmpTaskAgent`。

### 4.3 方案 C：混合（推荐总体路线）

```
轻量/结构化/高并发调用 ──▶ 现有 provider_factory（不变）
                                │
重/长上下文/多步编排调用 ──▶ OmpTaskAgent（新增服务）
                                │
                          omp 子进程（文件工作区，长生命周期 session）
```

- 通过开关（如 `OMP_AGENT_ENABLED`、`OMP_AGENT_TASKS=outline_full,consistency_fullbook`）控制哪些任务走 omp，**默认关闭、零回归**（对齐优化建议方案原则 B）。
- 若 `OmpProvider`（方案 A）也挂上，可让 omp 同时作为降级链候选，两条路径互不冲突。

---

## 5. 三大环节集成可行性逐域分析

### 5.1 目录生成（Outline）

| 维度 | 分析 |
|------|------|
| 现状 | `_sse_outline_build.py` 已多阶段（`outline_draft/level1/sublevel/review/fix/adjust`）；纯内存 prompt，超长靠预算器裁切；已有 `_validate_outline` 校验。 |
| omp 价值 | 把"整卷目录 + 知识库 + 全局事实"放进工作区文件，**突破单章上下文上限**；用 `task` 子代理并行生成不同章；用 advisor 模型做目录复核（对齐参考文档 `outline_review` 阶段）。 |
| 可行性 | **高**。直接套用参考文档 5 阶段（score-planning→children_generation→outline_review→leaf_adjustment），每阶段一次 omp 运行，后端用 `continueTask` 式逻辑串联。 |
| 风险 | 159 章规模下若每章独立 session 会冷启动爆炸 → 必须**单 session 多轮**或**按父节点批量**。JSON 校验重试需后端把关。 |

### 5.2 正文生成（Content）

| 维度 | 分析 |
|------|------|
| 现状 | `sections.py` 逐章 `collect_json_response`；有 `max_tokens_for_budget`/`clip_text_notice` 等超长处理但无 Agent；普通正文生成**不走 Agent**（参考优化建议方案 C-1 指出的溢出风险）。 |
| omp 价值 | ① "知识库/事实变量极大 + 无原方案"场景（C-1 痛点）可直接走 omp 文件工作区，消除上下文溢出；② "已还原正文优化扩写"（参考 `buildAgentRestoredChapterContentPrompt`）天然是文件工作区任务；③ 用 `task` 子代理批量章节（对齐优化方案 C-3）。 |
| 可行性 | **高**，但需控制成本。建议**仅"超阈值章节"或"批量合并章节"走 omp**，普通章节保留现有轻量路径（开关默认关）。 |
| 风险 | 进程边界 + 逐章调用的延迟/吞吐；需复用现有 `context_length_limit` 阈值判定"是否路由到 omp"。 |

### 5.3 审核与修复（Audit & Repair）

| 维度 | 分析 |
|------|------|
| 现状 | `review_autofix.py` / `consistency_scanner` / `consistency_fullbook_repair` / `compliance` 均走 `chat_with_fallback`；"全文一致性修复"是最适合 Agent 的任务。 |
| omp 价值 | **最契合**。整卷全文 + 全局事实放进工作区，omp 多步推理定位并修复不一致（数字/名称/单位冲突），`task` 子代理并行扫描不同章节，`advisor` 模型做修复质量旁审。完全对应参考文档 §5.2 "Agent 全文一致性修复"路径。 |
| 可行性 | **最高**。修复任务本就是"读全文→推理→回写修复后全文"，与 omp 文件工作区模式同构；且 omp 的 `edit`/`ast_edit`（hashline 锚点）比纯文本替换更精准。 |
| 风险 | 修复必须"只改冲突、不动无关正文"——需严格约束 prompt + 后端 diff 校验（参考 `validate_fixed` 不写库机制）。 |

---

## 6. 集成接缝规格（Implementation Seam）

### 6.1 进程边界选型：**RPC stdio（推荐）而非 Node SDK**

| 方式 | 优劣 |
|------|------|
| `omp --mode rpc --no-session --no-ui`（subprocess + NDJSON） | ✅ 无 Node 依赖；Python `subprocess` 天然支持；匹配现有 CLI 子进程先例（soffice/mmdc）；双向 NDJSON 易解析。**推荐**。 |
| Node SDK `@oh-my-pi/pi-coding-agent` | ❌ 引入 Node/bun 运行时到 Python 后端，运维负担重；除非已有 Node 环境，否则不推荐。 |
| `omp -p "..."` 一次性 | ❌ 每调用启停进程，冷启动 + 无法复用 session/上下文，不适合多阶段。 |

**RPC 协议（README 实证）**：
```
$ omp --mode rpc --no-session
> {"id":"r1","type":"prompt","message":"..."}      # 入：发起一轮
< {"id":"r1","type":"response", ...}               # 出：结果（含 prompt_result）
> {"id":"r2","type":"set_model","provider":...,"modelId":...}
> {"id":"r3","type":"abort"}
```
后端维护一个**持久子进程**：启动时 `omp --mode rpc --no-ui`，用 `asyncio` 子进程桥接 stdin 写 NDJSON / stdout 读 NDJSON 帧；每轮 `prompt` 以 `prompt_result` 收尾。`--no-ui` 关掉工具卡片/对话框，纯数据通道。

### 6.2 会话生命周期（避免冷启动）

- 后端为**每个 scheme / 每个重任务类型**维护一个长生命周期 omp session（磁盘 session store + `--resume`），而非每次调用新建。
- 任务结束/超时后归档 workspace（对齐参考文档 `writeDeveloperLog` 归档）。
- 并发：用 `asyncio.Lock` 或连接池限制同时活动的 omp 子进程数（参考 `config.py:361 max_concurrency=5`）。

### 6.3 工作区契约（对齐参考文档"业务库保护"）

```
workspace/
  input/            # 后端写入：skeleton / knowledge / facts / context / restored
  output/           # omp 写回：outline.json / section.md / repaired.md
  .omp/             # omp 自身 session/记忆（项目级隔离）
```
- omp **只允许读写 `workspace/`**，通过启动目录 + 工具约束 + 系统指令三重限制。
- 后端读 `output/` → 校验 → 写业务 DB。**omp 永不直连 SQLite**。

### 6.4 安全沙箱（必做）

- omp 拥有 `bash`/`write`/`computer` 等高危工具。生产环境**必须**：
  - 运行时 `--no-tools` 或 `--tools read,write,edit,grep,glob`（收窄到文件工作区最小集）；禁用 `bash`/`computer`/`browser`/`generate_image`（除非显式需要）。
  - 工作区目录与业务代码/数据库**物理隔离**（临时盘或专用 `workspace/` 根）。
  - 系统指令显式声明"禁止访问 workspace 以外路径、禁止执行网络请求、禁止修改业务库"。
  - 输出经**后端校验 + diff 审计**后才落库（复用 `validate_fixed` 式不写库机制）。

### 6.5 模型路由

- omp 多 provider：可直接复用平台已有的 API Key（DeepSeek/Qwen/等），在 `~/.omp/agent/models.yml` 或环境变量配置；也可指向**本地 OpenAI 兼容服务**（Ollama/vLLM）做离线/低成本 Agent。
- 建议把 omp 的"主模型"与"advisor 模型"分开（advisor 用便宜模型做旁审，降本）。

---

## 7. 风险、Blockers 与缓解

| # | 风险 / Blocker | 严重度 | 缓解 |
|---|---------------|--------|------|
| R1 | **概念误用**：把 omp 仅当 `chat()` Provider，浪费 Agent 价值 | 中 | 明确"混合架构"，重任务走 `OmpTaskAgent`（文件工作区） |
| R2 | **进程边界冷启动/会话管理**：Python↔Rust 子进程，多章规模下性能 | 高 | 长生命周期 session + `--resume`；按父节点批量；连接池限并发 |
| R3 | **Windows 兼容性**：用户是 win32，omp 声称原生支持 win32-x64，但需实测 RPC | 高 | PoC 第一步即在目标 Windows 验证 `omp --mode rpc` 可用 |
| R4 | **安全**：omp 有写文件/执行命令能力 | 高 | 收窄 `--tools`、工作区隔离、系统指令约束、输出校验后落库 |
| R5 | **版本活跃**：v18.18.6 距 main 1101 提交，RPC 协议可能变动 | 中 | 锁定版本（安装 `--ref v18.18.6`），升级需回归 RPC 协议 |
| R6 | **成本/延迟**：Agent 多步推理比单次 LLM 调用贵且慢 | 中 | 仅重/超阈值任务走 omp；开关默认关；advisor 用廉价模型；用现有 `prompt_context_budget` 判定路由 |
| R7 | **质量回归**：omp 输出质量可能低于现有多步提示工程 | 中 | 保留现有 prompt 模板与校验器；omp 输出经 `_validate_*` 把关；日志标记 `omp_agent:true` 供审计 |
| R8 | **可观测性脱节**：现有 `/ai/stats`、熔断、限流只覆盖 Provider 层 | 中 | `OmpProvider`（方案 A）自动接入；`OmpTaskAgent` 需单独埋点（耗时/字数/失败） |
| R9 | **现有成熟基建的替代冲动**：不要为集成而重写 `_sse_outline_build` 等 | 低 | 严守"新增服务、不改现有主链路"，开关可控零回归 |

---

## 8. 验证 / 验收标准与分阶段路线图

### 8.1 阶段 0：PoC（可行性闸门，必须先过）

1. 在目标 Windows 环境安装 omp（`irm https://omp.sh/install.ps1 | iex`，或 `bun install -g @oh-my-pi/pi-coding-agent`）。
2. 验证 `omp --version` 与 `omp --mode rpc --no-ui` 能收发 NDJSON（`prompt`→`response`）。
3. 验证 omp 能在受限 `--tools read,write,edit,grep,glob` 下读写一个临时工作区文件并回写。
4. **闸门**：以上任一项失败 → 可行性降级为"需解决环境/版本问题后再评估"，不进入阶段 1。

### 8.2 阶段 1：OmpProvider（chat 包装，低风险）

- 实现 `OmpProvider(BaseProvider)`，接入 `provider_factory` 降级链。
- 验收：配置 omp 为某场景 provider，日志出现 `omp_agent` 调用；失败自动降级到现有 provider（对齐优化方案 G-1）。

### 8.3 阶段 2：OmpTaskAgent + 目录生成试点

- 实现 `services/omp_agent.py`（workspace 构造、session 管理、RPC 桥、输出回收、校验重试）。
- 选**目录生成**作为首个试点（价值高、结构化好）：复用 `_validate_outline`。
- 验收：构造"知识库极大"用例，omp 产出合法目录 JSON；`< 10%` 样本与现有产出 diff 可接受；开关关时行为不变。

### 8.4 阶段 3：正文生成 + 审核修复扩展

- 正文：仅"超阈值章节/批量合并"走 omp（对齐 C-1/C-3）。
- 审核：整卷一致性修复走 omp（最契合，对齐参考 §5.2）。
- 验收：一致性修复后全文回写，冲突项下降；日志标记 `omp_agent:true` + `degraded` 口径统一。

### 8.5 验收底线（对齐优化建议方案 §7.2）

- **零回归**：所有 omp 相关改动以**开关默认关闭**为前提；默认配置下输出与现状一致。
- **降级**：omp 不可用时关键路径仍能由现有 Provider 层兜底完成，日志含 `omp_failed` 标记。
- **安全**：omp 进程无法触达业务数据库与 workspace 外路径（渗透式测试）。
- **可观测**：omp 任务耗时/字数/失败率纳入现有统计或新增轻量埋点。

---

## 9. 结论与建议

1. **前提纠偏（必须）**：omp 是**通用 AI 编程 Agent**，不是参考文档里基于商汤的"Pi Agent"。参考文档贡献的是**设计模式蓝图**（文件工作区、阈值切换、断点续跑、JSON 校验、库隔离、自检），而非可直接搬运的代码。

2. **可行性结论**：✅ **可行**。omp 的能力面完整覆盖并能扩展参考文档 Pi Agent 的角色；本平台已具备所有"承接"条件——成熟的 Provider 抽象（插槽干净）、SSE 阶段机（可承载多阶段编排）、领域 prompt 与校验器（域智能不丢失）、subprocess CLI 先例（进程集成有样板）。

3. **推荐架构**：**混合**（方案 C）——轻量调用留现有 Provider 层（不变），新增 `OmpTaskAgent` 服务以**文件工作区模式**承担超长上下文/多步编排重任务；同时可挂 `OmpProvider` 让 omp 作为降级链候选（低风险入口）。全程默认关闭、开关可控、零回归。

4. **最大工程挑战**不在"能不能调 omp"，而在**进程边界的会话/冷启动管理**与**安全沙箱**——这两项是 PoC 阶段（阶段 0）必须先验证的闸门。

5. **价值定位**：omp 相对现有自研层的**增量价值**是——多步文件工作区推理（突破上下文窗口）、`task` 子代理并行、advisor 模型旁审、本地模型可接入。但它**不替代**现有成熟的降级链/熔断/限流/审计；这些必须保留。

6. **不建议**为一时的"集成时髦"重写 `_sse_outline_build.py` / `sections.py` 等已历经 20+ 轮加固的主链路——新增服务、旁路集成、受控灰度才是稳妥路径。

---

## 10. 阶段 0 实测结论（2026-11-12 · 已执行）

> 目的：验证报告中 Stage 0 闸门（安装 / RPC 收发 / 受限工具下文件工作区写回）在**目标 Windows 环境**真实可通过。全部在 `C:\Users\Administrator\AppData\Local\Temp\omp_stage0\` 完成，未污染项目代码；探针脚本与日志留存于该临时目录。

### 10.1 环境基线

| 项 | 实测值 |
|----|--------|
| OS | Windows 10.0.19045.0（win32-x64 / AMD64） |
| 已装运行时 | `node` v24.21.0；`bun` 未装 |
| omp 安装方式 | 官方 PowerShell 脚本 `irm https://omp.sh/install.ps1 \| iex` |
| omp 版本 | **18.19.0**（安装器默认 latest；参考的 v18.18.6 可用 `--Binary -Ref v18.18.6` 锁定） |
| omp 路径 | `C:\Users\Administrator\AppData\Local\omp\omp.exe`（已写入 PATH，需新开终端生效） |

> 注：用户原参考页是 v18.18.6，距 main 1101 提交；安装器默认拉 latest（18.19.0）。**生产集成务必锁定版本**，避免 RPC 协议漂移（见 10.4 协议事实）。

### 10.2 三项闸门实测结果

| 闸门 | 结果 | 证据 |
|------|------|------|
| G1 安装 | ✅ 通过 | `omp/18.19.0`；安装器自动配置 Git bash 路径到 `~/.omp/agent/settings.json` |
| G2 `omp --mode rpc` NDJSON 收发 | ✅ 通过 | 启动即发 `ready` 握手帧（见 10.4）；`prompt` 受理返回 `response{command:prompt,success:true}`；结尾 `prompt_result{status:completed}` |
| G3 受限工具下文件工作区写回 | ✅ 通过 | **完整 agent 循环跑通**：mock 模型后端返回 `write` 工具调用 → omp 解析并执行 → 工作区 `ws/output/result.md` 被创建，内容 `OMP_STAGE0_WRITE_OK` |

### 10.3 完整闭环证据链（实测帧摘要）

```
[launch] omp --mode rpc --no-ui --no-session --cwd <ws> --model mock/mock-model --tools write,read
[frame] {"type":"ready","protocolVersion":1,"supportedProtocolVersions":[1,2],
         "maxFrameBytes":1048576,"maxReassembledFrameBytes":67108864,
         "supportsBashExcludeFromContext":true}
[frame] {"id":"r1","type":"response","command":"prompt","success":true}      ← 受理确认（非结束）
[frame] {"type":"agent_start"} / {"type":"turn_start"}
[frame] {"type":"message_start","message":{"role":"assistant","content":[
         {"type":"toolCall","id":"call_1","name":"write",
          "arguments":{"path":"...ws\\output\\result.md","content":"OMP_STAGE0_WRITE_OK\n","i":"..."}}]}
[frame] ... toolcall_start / toolcall_delta / toolcall_end ...              ← omp 执行 write 工具
[frame] {"type":"prompt_result","id":"r1","status":"completed"}            ← 真正结束标记
[probe] marker_written=True content='OMP_STAGE0_WRITE_OK\n'
[RESULT] LOOP_OK
```

> 验证用模型后端：本地 Python 标准库 `http.server` 伪装 OpenAI 兼容 `/v1/chat/completions` + `/v1/models`（因环境无真实 API key）。**omp 不需要真实 LLM 即可验证进程边界与工具链路**——真实集成时把 mock 替换为平台既有 provider 或本地 vLLM/Ollama 即可。

### 10.4 对真实集成极有价值的协议事实（抓取自实测）

1. **RPC 启动命令形态**（已验证可用）：
   `omp --mode rpc --no-ui --no-session --cwd <workspace> --model <provider>/<model> --api-key <k> --tools write,read`
   - `--no-ui`：关闭工具卡片/对话框，纯数据通道（自动化必需）。
   - `--no-session`：临时会话不落盘（生产应用 `--resume` 做长生命周期，见报告 §6.2）。
   - `--tools write,read`：收窄到文件工作区最小集（对齐报告 §6.4 安全沙箱）。
2. **握手帧 `ready`**：`protocolVersion:1`、`maxFrameBytes:1048576`（单帧 1MB）、`maxReassembledFrameBytes:67108864`（重组 64MB）。**大输出（如整卷正文）需分片或走文件，不能塞单帧**。
3. **结束标记是 `prompt_result`，不是 `response`**：`response{command:prompt}` 仅是受理确认，驱动端必须等 `prompt_result`/`error` 才能判定任务完成（这是写驱动时踩过的坑）。
4. **omp 始终以 `stream:true` 请求模型**：集成适配层**必须实现 SSE**（`Content-Type: text/event-stream`，每个 `data:` 事件以 `\n\n` 分隔，结尾 `data: [DONE]\n\n`）。用非流式或漏 `\n\n` 分隔符会导致 omp 解析不出工具调用（实测两次失败原因）。
5. **流式工具调用规范**：第一 delta 给 `tool_calls[{index,id,type:"function",function:{name,arguments:""}}]`，后续 delta 给 `function.arguments`（完整 JSON 字符串）。omp 按 index 累加 arguments 后解析。
6. **omp 暴露的工具 schema（实测 `write`）**：`required:["path","i"]`，属性 `i`(意图)/`path`/`content`。集成时返回工具调用必须带 `i`。
7. **omp 内置自动重试（3 次）**：模型/工具瞬时失败会被自恢复；适配层无需自造重试。
8. **多 provider 自定义**：`~/.omp/agent/models.yml` 的 `providers.<name>` 配 `baseUrl`/`api: openai-completions`/`apiKey`/`models[]` 即可把 omp 指向任意 OpenAI 兼容端点（本地 vLLM/Ollama 或平台既有服务）。

### 10.5 风险重估（基于实测）

| 报告原风险 | 实测后结论 |
|-----------|-----------|
| R3 Windows 兼容性（高） | ✅ **已证伪为风险**：omp 原生 win32-x64，安装/运行/RPC 全程正常 |
| R2 进程边界冷启动/会话（高） | 🟡 进程边界已证明可用；**长生命周期 session 管理**（避免逐章冷启动）仍需在阶段 1/2 落实 |
| R4 安全沙箱（高） | 🟡 `--tools` 收窄 + `--cwd` 隔离已验证有效；生产仍需系统指令 + 输出校验后落库三重防护 |
| R5 版本活跃（中） | ⚠️ 维持：18.19.0 已与参考 18.18.6 不同；生产必须锁版本 |
| 概念误用 R1（中） | ✅ 本次实测即按"通用 Agent + 文件工作区"理解落地，无偏差 |

### 10.6 阶段 0 结论

> **Stage 0 闸门全部通过**：omp 可在目标 Windows 环境安装、以 RPC 模式被 Python 后端驱动、并在受限工具下完成"模型指令 → 文件工作区写回"的 agent 闭环。**集成 omp 的技术可行性已从"架构研判"升级为"实测确认"**。剩余工作为工程化（长生命周期 session、安全沙箱加固、真实 provider 接入、与 `OmpTaskAgent` 服务对接），不再存在原理性阻塞。

---

## 附录 A：信息源与证据

- 参考文档：`OpenBidKit_pi_agent_工作机制报告.md`（只读静态分析，含 OpenBidKit 四层管线与文件工作区模式）、`OpenBidKit_pi_agent_优化建议方案.md`（O-1~O-3、C-1~C-4、G-1~G-4 优化清单）。
- omp 仓库：`https://github.com/can1357/oh-my-pi`（README：60+ provider、31 工具、RPC/ACP/Node SDK、win32 支持、MIT）；发布页 `v18.18.6`（2026-11-09，距 main 1101 提交）。
- 本平台探查：`backend/app/services/ai/provider_factory.py`、`providers/base.py`、`routers/_sse_outline_build.py`、`routers/sections.py`、`services/review_autofix.py`、`config.py`（证据见 §2.2）。

## 附录 B：术语对照

| 参考文档术语 | 本平台对应 / omp 对应 |
|------------|----------------------|
| Pi Agent（商汤） | 目标集成物 = omp（通用 Agent）；本平台无对应运行时 |
| 文件工作区 files[]/output_file | omp `read/write/edit` 工具 + 后端 `workspace/` 隔离目录 |
| persistent_task / resume | omp 磁盘 session + `--resume` |
| continueTask 多阶段 | 本平台 `_sse_outline_build` 阶段机 + 后端串联 |
| json_validation_schemas | 本平台 `_validate_outline` / `collect_json_response` 修复 |
| agentOpenAiProxy | 不适用（omp 直接是 Agent，不需代理） |
| aiService.chat | 本平台 `chat_with_fallback` / `collect_json_response` |
| AGENT_CONTEXT_THRESHOLD_RATIO | 本平台 `context_length_limit` / `prompt_context_budget` |

> 报告说明：本报告基于只读探查与公开信息。omp 的实际 RPC 协议细节以 `omp.sh/docs/sdk` 与运行时为准；落地前须在阶段 0 PoC 中实测 Windows 环境下的 `omp --mode rpc` 可用性，确认无环境/版本阻塞后再进入实质开发。

---

## 11. 阶段 1 / 阶段 2 骨架落地状态（2026-11-12 · 已执行）

> 阶段 0 闸门全过后，按路线图落地了两层集成骨架。**两者默认全部关闭**（配置开关为 `False`），对现有主链路零影响；仅在显式开启并具备真实 LLM provider 后才参与。

### 11.1 阶段 1 —— OmpProvider（降级链「对话模型代理」）
- 文件：`backend/app/services/ai/providers/omp_provider.py`（`OmpProvider(BaseProvider)`，实现 `chat()`/`stream()`/`supports_vision()=False`）。
- 接线：`provider_factory.py` 新增 `PROVIDER_PRESETS["omp"]` 预设（type=`omp`）+ `_build_provider` 分支 + 放宽 omp 空 `api_key` 校验。
- 配置：`config.py` 新增 `omp_provider_enabled`(默认 False) / `omp_binary_path` / `omp_workspace_dir` / `omp_rpc_timeout` / `omp_tools`(默认空=纯对话不挂工具)。
- 安全闸门：未启用 / 二进制缺失 → `chat()` 抛清晰错误 → 降级链自动跳过。
- 验证：`tests/test_omp_provider_20261112.py` 11 passed（纯逻辑 + 两道闸门 + 接线回归），ruff 全绿。

### 11.2 阶段 2 —— OmpTaskAgent（文件工作区 Agent，目录生成试点）
- 共享模块：`backend/app/services/ai/omp_rpc.py`（`OmpRpcSession` + `_try_json`/`_extract_assistant_text`/`binary_exists`，被 OmpProvider 与 OmpTaskAgent 共用；消除重复实现）。
- 服务：`backend/app/services/ai/omp_task_agent.py`（`OmpTaskAgent.generate_outline`）：
  - 在**隔离工作区**内让 omp 借助受控工具白名单（`write,read,edit,grep,glob`，**不含 bash/exec**）生成 `OUTLINE.md`；
  - 任务结束后读回产物，解析为 `GeneratedSection` 列表（支持 Markdown 标题树与 JSON 嵌套/扁平）；
  - 安全闸门：未启用 / 二进制缺失 / 未配置模型（`omp_task_model`）均抛错且不拉起子进程；构造参数 `enabled/binary/model/tools` 可覆盖（便于测试）。
- 配置：`config.py` 新增 `omp_task_enabled`(默认 False) / `omp_task_model` / `omp_task_tools` / `omp_task_timeout`(300) / `omp_task_keep_workspace`。
- **关键设计**：OmpTaskAgent **不触碰**已历经 20+ 轮加固的目录生成主链路（`sse_outline` / `workflows_outline_v51`），仅作为旁路服务由调用方在受控灰度下调用。
- 验证：`tests/test_omp_task_agent_20261112.py` 11 passed（解析 / prompt / 默认值 / 三道闸门）+ 阶段 1 共 **22 passed**，ruff 全绿。

### 11.3 阶段 2 集成 seam —— 目录生成旁路入口（2026-11-12 · 已落地）
- 转换桥：`backend/app/services/ai/omp_outline_bridge.py`（**router-free**，遵守 `test_services_never_import_routers` 分层契约）：
  - `fold_generated_sections_to_tree(sections)` 把 `OmpTaskAgent` 产出的**扁平** `GeneratedSection` 列表按 `level` 折叠成**嵌套**目录树 `{title, level, description, children}`（就近父 / 跳级 / 层级回退均已处理）；不预填 `parent_id`/`sort_order`/`path`，由落库器内部算。
  - `generate_outline_tree(...)` 调 `OmpTaskAgent().generate_outline` 并折树，**不直接落库**。
- 旁路端点：`backend/app/routers/sections.py` 新增 `POST /api/v1/schemes/{scheme_id}/sections/generate-outline-omp`（`generate_outline_via_omp`）：
  - 门控 `settings.omp_task_enabled`：**禁用 → 409 提示回退主链路**；
  - 复用与 `save-outline` **完全相同**的整表重建机器 `_save_outline_to_db`（内部 `normalize_outline` + `_process_nodes` 自动算父子/排序/点分编号）；
  - 与 `save-outline` 同口径的 **content / outline 生成互斥 409 守卫**，避免与在跑 SSE 主链路互踩；
  - **完全不碰** `sse_handlers` 里的硬化 SSE 目录编排。
- **生产前加固（同日落地）**：
  - **brief 自动派生**：`brief` 改为可缺省，缺省时从 `schemes` 表 `name / type / profession / config_json(project_profile) / word_budget` 派生；`project_type` / `industry` 缺省也取方案 `type` / `profession`；brief 长度上限 8000；方案不存在 → 404。
  - **同型防重入**：omp 是同步长任务且不注册 `task_registry`，主链路 `outline_generation_in_progress` 感知不到它 ⇒ 新增模块级 per-scheme `_OMP_OUTLINE_INFLIGHT` 集合，检查与 `add` 之间无 `await`（单事件循环内原子），`try/finally` 保证 502 / 异常 / 中断都释放标记（不产生僵尸 409）；并发第二个 omp 请求 → 409。
  - **两处 bug 自查修复**：① `row.get("type")` —— `db` 行是 `aiosqlite.Row`，**没有 `.get()`**，改 `row["type"]`；② `config_json` 为合法但非 dict 的 JSON（list/str）时 `cfg.get` 抛 `AttributeError`，加 `isinstance(_parsed, dict)` 降级。
  - **feature-flags 下发**：`routers/system.py` 新增 `GET /api/v1/system/feature-flags` 返回 `{omp_task_enabled}`，让前端**预显隐**入口（仓库本无聚合 flag 端点，既有开关靠各功能 404/409 软隐藏）。
  - **前端入口**：`api/index.ts` 加 `sectionsApi.generateOutlineOmp` + `systemApi.getFeatureFlags`；`types/api.ts` 加 `GenerateOutlineOmpPayload` / `FeatureFlags`；`SchemeWorkbenchPage.tsx` 启动拉 flag、成功后 `load()` 刷新目录树；`OutlineTab.tsx` 在「AI 生成目录」后加受控「🤖 Agent 生成目录」按钮。
  - **长任务超时（P1）**：全局 axios 仅 60s，而后端 `omp_task_timeout=300s` + 内置重试 —— 前端会先超时、后端仍整表写入，用户重试即覆盖。新增 `OMP_OUTLINE_TIMEOUT_MS=360000` 并显式传入；超时文案明确「后端可能仍在生成，请勿重复点击，稍后刷新目录树」。
  - **并发互斥（P1）**：omp 生成期间目录 Tab **所有**写目录树入口（主链生成 / 导入 / 套用 / 存库 / 新增 / 保存 / 清除）统一 `outlineBusy = generating || generatingOmp` 禁用；SSE 专属 `loading` / `TaskControlButtons` 判据保持不变。
- 验证：后端 `tests/test_omp_outline_bridge_20261112.py` **12 passed**（折树 / 折叠 mock / 禁用 409 / feature-flags / brief 派生 + Row 契约 + 非 dict config_json / 防重入 + finally / 404 / 源码棘轮）；**omp 三阶段合计 34 passed** + 分层契约 1；ruff 全绿。前端 `tsc --noEmit` EXIT 0、`tests/ompOutlineEntry20261112.test.ts` **6 passed**（长超时 > 全局、显式 timeout、契约类型、flag 门控、outlineBusy 互斥、超时文案）；新增前端文件按 LF 落盘。
- ⚠️ 本机前端 eslint 的 `@typescript-eslint` 解析器缺模块（基础设施故障，非代码错误），以 tsc 为准。

### 11.4 生产前待办与已知局限（运维 / 设计取舍）
1. `omp_task_model` 指向真实 LLM provider（在 omp `models.yml` 配 baseUrl/apiKey）—— 纯运维，代码三道闸门已给出清晰报错。
2. **锁 omp 版本**（默认 latest 18.19.0 ≠ 参考 v18.18.6）—— 部署动作。
3. 清理 `~/.omp/agent/models.yml` 中指向死 mock（端口 8799）的 PoC provider 配置 —— 用户本机文件，不在仓库。
4. omp 产物语义校验（编号唯一 / 合规）：`_save_outline_to_db` 内 `normalize_outline` / `validate_outline_numbering` / 标题清洗 / 空壳一级章闸门 / 三级深度裁剪已承担，刻意**不造第二份校验器**。
5. **已知局限 A · 多 worker**：`_OMP_OUTLINE_INFLIGHT` 是进程内集合，单进程 uvicorn 有效；多 worker 部署需换分布式锁（Redis / DB）。
6. **已知局限 B · 单向并发窗口**：omp 在跑时不注册主链路任务表；反向「omp 启动时主链路在跑」已被 content/outline 双 409 拦下、前端 `outlineBusy` 也禁用主链按钮，但**绕过 UI 直连 SSE 端点**仍可在 omp 期间启动主链。彻底闭合需改 `outline_generation_in_progress` / `task_registry`（已加固主链路，本轮刻意不碰），留待生产灰度评估。
7. **已知局限 C · 同步端点**：omp 目录生成是同步 POST（前端最长 6 分钟），非 SSE、无进度回报；生产建议迁后台任务 + 轮询 / SSE 进度。
8. 前端 omp 入口未做「生成中刷新页面」接管态（同步请求无 task_id），刷新后按钮恢复可用；后端防重入会拦住仍在跑的重复请求。

### 11.5 阶段 2 第二试点 —— omp 主 Agent 全文跨节一致性检查（2026-11-12 · 旁路灰度已落地）

> 需求：正文生成后做全文一致性检查，由**主 AGENT** 跨节比对并统一修复，**READ 工具执行**。
> 探查结论（READ 实证）：平台**已有并默认开启**该 pass —— `sse_handlers.py:3016` `auto_consistency_repair=True`，
> `:6710-6943` 扫描→仲裁→定向/整卷统一修复；跨节比对来自 `consistency_scanner.py:340/780` 全局主题桶，
> 统一修复来自 `consistency_fullbook_repair.py:164` 与 `review_autofix.py` CON-01~04。
> 参考工作流原文 `docs/reference_research/03_global_facts.md:1679-1701` 要求 Agent 在工作区里
> 「搜索、分段读取、建立索引、多轮编辑」；本仓此前是**程序级等价物**
> （`consistency_fullbook_repair.py:7-9` 明写「不引入持久 Agent 会话基建」），
> 缺的正是「omp 主 Agent 用 read 工具真正读文件多轮修复」这一层。

**设计原则：不造第二条修复管线。** `run_fullbook_repair` 已封装「整卷工作区装配 → 四道小节闸门 → 前置快照 →
`save_repair` → confirm/rollback」，本轮只给它加一个**加法式可选形参** `workspace_agent_fn=None`：
传入时把单次 `chat_with_fallback` 换成 omp 文件工作区执行，回包仍是整卷工作区文本，
故 `parse_fullbook_workspace` / 闸门 A(未知ID) B(缺章) C(非空改空) D(结构与取值) /
快照 / 落库 / 前端 CR 工作台**全部逐字复用**；不传则行为逐字不变（既有 219 例回归实证）。

- 新增服务 `backend/app/services/ai/omp_consistency_agent.py`（**router-free**，遵守 AST 分层契约）：
  - 预写隔离工作区：`global-facts.md`（唯一权威口径）/ `conflicts.md`（已仲裁冲突）/ `technical-plan.md`
    （带 `<!-- §:id -->` 标记的整卷正文，**唯一允许 edit 的回写目标**）/ `TASK.md` / `sections/<sid>.md`
    （按标记切分的**只读分节索引**，供 read/grep 跨节比对；文件名经白名单过滤防路径穿越）。
    正文走**文件而非 prompt 单帧**（omp RPC `maxFrameBytes=1MB`，整卷塞单帧必被截断）。
  - **两轮自包含 prompt**（`--no-session` 不保留对话上下文，轮次间状态一律经文件传递）：
    ① `read` 分段读取 + `grep`/`glob` 跨节检索 → 写出 `FINDINGS.md`；
    ② 读回 FINDINGS.md 与相关章节，用 `edit` 对 `technical-plan.md` 做「同一主题在**所有**出现章节
    统一改成同一权威值」的最小必要修改，保留全部标记/编号/标题、禁止增删重排章节。
    两轮均硬性禁止 bash/exec、网络、工作区外读取、业务数据库访问。
  - 单进程连续多轮共享工作区；`try/finally` 保证 RPC 超时或回包读回失败也回收子进程。
  - 复用 `OmpTaskAgent._assert_ready()` 三道闸门（抽为共用方法，原行为逐字不变），
    并暴露 `check_ready()` 供 router 预检。
- 独立旁路端点 `POST /api/v1/schemes/{scheme_id}/consistency/omp-agent-check`（追加在
  `consistency_repair.py` 末尾，**既有 `/repair` 一行未改**，**完全不碰 `sse_handlers`**）：
  - **双闸** `omp_task_enabled` + 新增二级灰度 `omp_consistency_enabled`（默认都 False，禁用 → 409 提示回退）；
  - 复用 `ensure_no_generation_write` 单一事实源守卫（R98）、`_load_scheme` / `_load_conflicts` /
    `build_global_facts_text` / `_make_chart_resync` / G9 审核失效重置；
  - per-scheme `_OMP_CONSISTENCY_INFLIGHT` 同型防重入 + `finally` 释放；
  - 响应加 `engine="omp_agent"`，`repair_id` / `snapshot_id` / `items` 与既有完全一致 ⇒
    **前端零改动**，检查后直接走现有 `/confirm` 接受或 `/rollback` 回滚。
- **加固项**：`check_ready()` 预检避免「配置缺失被重试循环吞成 completed 但 items 全 failed」的假成功；
  闸门拒绝原因经 `repair_nudge` 透传给第 2 轮 prompt 纠正结构问题；分节 id 白名单防路径穿越；
  `try/finally` 防子进程泄漏。
- **验证**：新增 `tests/test_omp_consistency_20261112.py` **16 passed**（prompt READ/edit 契约、nudge 只进第 2 轮、
  工作区预写与穿越防护、三道闸门、单进程多轮回包读回、异常回收、回调接线行为实证×3、
  端点 409 / 源码锁 / 签名加法式）；**全量 270 passed / 0 failed**（omp 四阶段 50 + 分层契约 1
  + 既有整卷修复与守卫回归 219）；ruff 全绿、`py_compile` 通过、后端 6 文件纯 CRLF。
- **已知局限（登记）**：① 仍受 `FULLBOOK_MAX_CHARS=200000` 整卷上限约束（Agent 分段读本可突破，
  但为复用闸门语义保持）；② 每轮 `rpc_timeout=300s`、两轮最长 600s，同步端点无进度回报；
  ③ 重试每轮都会新建 omp 进程与工作区（未复用，成本偏高）；④ `_OMP_CONSISTENCY_INFLIGHT`
  为进程内集合，多 worker 需分布式锁；⑤ 端点为 HTTP 入口，前端暂无触发按钮（按本轮
  「前端零改动」约定，复用 CR 工作台）。
