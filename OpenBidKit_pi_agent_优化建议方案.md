# OpenBidKit 易标 — Pi Agent 优化建议方案（目录生成 & 正文生成）

> 依据文档：《OpenBidKit_pi_agent_工作机制报告.md》（2026-11-11，只读探查）
> 分析对象：`J:\编程\OpenBidKit 易标\OpenBidKit_Yibiao-main-2026-09-14`
> 方案范围：**目录生成**（`outlineGenerationTaskV2.cjs`）与**正文生成**（`contentGenerationTask.cjs`）两大环节
> 优化框架：基于报告 §1 的四层管线（业务层 → 调度层 → 运行时层 → 接入层）
> 方案性质：**优化建议清单**，不修改源码；所有建议均附报告/源码证据，可评审后落地

---

## 0. 结论速览（TL;DR）

本方案围绕四层管线，对目录生成与正文生成提出 **6 项高优先级 + 4 项中优先级** 优化建议。核心判断：

- **目录生成**：功能最"原生"（全程走 Agent + 多阶段持久任务），但存在 `max_retries: 0` 无重试、5 阶段串行往返偏多的两个短板 → 建议 **P1 补重试**、**P2 合并阶段**。
- **正文生成**：机制最"条件化"（阈值切换），但普通正文生成完全不走 Agent、正文编排不走 Agent → 建议 **P1 阈值动态化 + 普通生成可选 Agent**、**P2 批量合并生成**。
- **共性**：SDK 失败降级链已存在但缺"Agent 运行失败自动降级"、缺"输入缓存复用" → 建议 **P1 降级链扩展**、**P2 缓存复用**。

---

## 1. 现状评估（基于四层管线）

### 1.1 四层管线对照（优化切入点定位）

| 层 | 关键文件 | 现状（报告证据） | 优化空间 |
|----|---------|-----------------|---------|
| 业务层 | `outlineGenerationTaskV2.cjs` / `contentGenerationTask.cjs` / `contentIllustrationPlanning.cjs` | 构建 prompt + workspace 文件 + JSON 校验器 + 阶段状态机 | prompt 可模板化复用、阶段可合并、校验器可降级 |
| 调度层 | `agentService.cjs` | `normalizeAgentTaskPayload` / `startTask` / 断点持久化 / 结果归一化 | 可加"输入指纹缓存"、"运行统计聚合" |
| 运行时层 | `piRuntimeService.cjs` | `runTask` 主循环 / 多阶段推进 / 重试 / 诊断 / 归档 | `max_retries` 可参数化、失败可触发降级钩子 |
| 接入层 | `piSessionFactory.cjs` / `piEnvironment.cjs` / `agentToolEnvironment.cjs` / `agentOpenAiProxy.cjs` / `aiService.cjs` | SDK 加载 / Session 创建 / 指令注入 / 工具环境 / OpenAI 代理 / 真实 LLM | SDK 失败降级已存在；可加"启动预检" |

### 1.2 目录生成现状（关键证据）

- **引擎**：默认全程走 Pi Agent，`agentService.runTask`（`outlineGenerationTaskV2.cjs:1081`）。
- **编排**：`persistent_task: { task_key, mode: 'resume' }` + `continueTask` 串行推进（`outlineGenerationTaskV2.cjs:1099-1109`）。
- **阶段**：`score-planning`（或 `children_generation` 无技术评分）→ `children_generation` → `outline_review` → `leaf_adjustment`，共 5 阶段（报告 §3.2）。
- **并行**：目录生成与投标模版提取并行，`AbortController` + `AbortSignal.any` 级联取消（`outlineGenerationTaskV2.cjs:1050-1079`）。
- **校验**：`json_validation_schemas` 校验目录 JSON，失败触发 `buildRetryPrompt`（报告 §2.2.3）。
- **短板**：`max_retries: 0`（`outlineGenerationTaskV2.cjs:1106`）——JSON 校验失败或单阶段 Agent 输出异常时**不重试直接失败**，与"校验→重试"机制设计意图（报告 §2.2.3）脱节。

### 1.3 正文生成现状（关键证据）

- **阈值切换**：`shouldUseAgentForMessages(aiService, messages)`，超 `context_length_limit * AGENT_CONTEXT_THRESHOLD_RATIO` 才走 Agent（`contentGenerationTask.cjs:4225`，报告 §4.2）。
- **参与矩阵**（报告 §4.1 / §7）：

| 子阶段 | 引擎 | 调用点 |
|--------|------|--------|
| 正文编排 | `aiService.collectJsonResponse`（**不走 Agent**） | `:3901` |
| 原方案还原 | Pi Agent 文件模式（超阈值） | `:4073` |
| 正文生成主流程 | `aiService.chat`（普通）/ Pi Agent（已还原优化扩写+超阈值） | `:4239` |
| 原方案覆盖修复 | Pi Agent 文件模式 | `:5056` |
| 全文一致性修复 | Pi Agent（Agent 路径） | `:5607` |
| 图片编排 / HTML 图片 | Pi Agent（常驻） | `:6276` / `:6421` |

- **短板**：
  1. **普通正文生成不走 Agent**（`:4270`）——单章正文本身过长（无原方案但知识库/事实变量很大）时，仍走 `aiService.chat`，存在单次上下文溢出风险。
  2. **阈值比率固定**——`AGENT_CONTEXT_THRESHOLD_RATIO` 为常量，未按模型窗口/任务类型自适应。
  3. **正文编排不走 Agent**（`:3901`）——编排决策本身依赖大量知识库上下文，却用普通对话。

---

## 2. 优化原则与优先级框架

- **原则 A（不改架构）**：仅调整参数、扩展降级/缓存钩子、优化 prompt 编排，不重写四层管线结构。
- **原则 B（可回退）**：每项建议带独立开关/参数，默认关闭或保持现状行为，避免回归。
- **原则 C（有据可依）**：每项建议必须指向报告或源码中的具体机制，禁止无依据的"改进"。
- **优先级**：
  - **P1（高）**：解决"会失败 / 会溢出"的可靠性问题，优先落地。
  - **P2（中）**：解决"延迟 / 成本 / 体验"的效率问题，可排期。
  - **P3（低）**：优化体验与可观测性，择机实施。

---

## 3. 目录生成环节优化建议

### O-1【P1】`max_retries` 参数化：让"校验→重试"机制真正生效

- **现状**：`outlineGenerationTaskV2.cjs:1106` 硬编码 `max_retries: 0`。报告 §2.2.3 显示运行时已实现 `buildRetryPrompt`（追加"你的输出不是合法 JSON"指令重新运行），但 `max_retries: 0` 使该机制**永远不触发**——单阶段 JSON 校验失败即整体失败。
- **建议**：
  1. 将 `max_retries` 改为可配置（如从任务配置读取，默认 1）。
  2. 针对 `children_generation`（子节点生成，输出量大、最易触发 JSON 越界）单独允许 `max_retries: 2`。
  3. 重试前把上一阶段输出快照写入 workspace（如 `outline-attempt-N.md`），供 `buildRetryPrompt` 引用，形成"失败上下文"。
- **证据**：`outlineGenerationTaskV2.cjs:1106`；`piRuntimeService.cjs` `buildRetryPrompt`（报告 §2.2.3）。
- **收益**：目录生成失败率下降；重试成本 ≤ 2 次额外 Agent 运行，可接受。
- **风险**：无——仅放开已存在的重试机制。

### O-2【P2】阶段合并：减少串行往返

- **现状**：5 阶段串行（`score-planning` → `children_generation` → `outline_review` → `leaf_adjustment`，报告 §3.2），每阶段一次 Pi Agent 运行 + workspace 读写，延迟与 token 消耗线性叠加。
- **建议**：
  1. 将 `score-planning` 与首次 `children_generation` 合并为一个阶段（`planning_and_children`），计分决策与子节点生成共享一次运行（通过 workspace 双文件 `score-plan.json` + `outline.json` 双输出）。
  2. 保留 `outline_review`（需人工/模型复核，不宜合并）。
  3. 通过 `continueTask` 的 `meta.workflow_stage` 增加新阶段名，保持 `persistent_task` 断点兼容（旧任务可续跑）。
- **证据**：`outlineGenerationTaskV2.cjs:1148`（`score-planning` 阶段处理）与 `:1110`（`outline_review` 处理）。
- **收益**：目录生成端到端延迟预计减少 1 次 Agent 往返（约 20%-30% 阶段数）；token 省一次主指令重述。
- **风险**：中——需改 `continueTask` 分支与 `workflow_stage` 命名，注意断点兼容（建议保留旧阶段名别名）。

### O-3【P2】校验失败降级：Agent 失败 → `aiService` 兜底

- **现状**：目录生成无降级——Agent 失败即任务失败（报告 §7 "目录生成=唯一生成引擎"）。
- **建议**：为 `children_generation` 阶段增加降级钩子——若 Agent 连续 2 次重试仍失败，回退到 `aiService` 单轮对话生成子节点（复用 `createChildrenPrompt` 生成的 messages 结构），并把结果以相同 schema 写回 `OUTLINE_OUTPUT_FILE`。
- **证据**：报告 §1.2 调用链显示 `aiService` 与 Agent 共用同一业务层 prompt 体系，具备降级可行性；`piSessionFactory.cjs` 的 `AGENT_REPORTED_FAILURE_CODE` 已预留降级路径（报告 §2.3.1）。
- **收益**：目录生成可靠性从"单引擎"升级为"Agent 主 + aiService 兜底"，关键路径不因 SDK 环境问题中断。
- **风险**：中——降级输出质量可能低于 Agent 多步推理，需在日志标注 `degraded: true` 供审计。

---

## 4. 正文生成环节优化建议

### C-1【P1】阈值动态化 + 普通生成可选 Agent

- **现状**：
  1. `AGENT_CONTEXT_THRESHOLD_RATIO` 为常量，所有子阶段共用同一比率（`contentGenerationTask.cjs:4228`）。
  2. **普通正文生成不走 Agent**（`:4270` `aiService.chat`），即便单章上下文（知识库+事实变量+项目概述）已很大。
- **建议**：
  1. **动态阈值**：把 `context_length_limit` 与 `AGENT_CONTEXT_THRESHOLD_RATIO` 的乘积改为按模型配置读取（如 `getAgentThreshold(aiService, taskType)`），允许不同模型/任务类型用不同比率（如正文生成 0.7、一致性修复 0.85）。
  2. **普通生成可选 Agent**：为 `buildChapterContentMessages`（非已还原优化扩写）也接入 `shouldUseAgentForMessages` 判断——超阈值时走 `runContentAgentTask`（`outputFile: 'chapter-section.md'`），复用 `buildAgentRestoredChapterContentPrompt` 的"只写正文不写标题/数据库"约束。
  3. 新增任务配置开关 `enablePlainContentAgent`（默认 false，保持现状行为，符合原则 B）。
- **证据**：`:4225`（条件仅 `needsRestoredOptimization && shouldUseAgent...`）、`:4270`（普通路径硬编码 `aiService.chat`）。
- **收益**：消除"知识库/事实变量极大但无原方案"场景的单次上下文溢出；普通与已还原两条路径共用 Agent 契约。
- **风险**：中——普通正文生成走 Agent 会增加 workspace 读写开销；通过开关默认关闭控制。

### C-2【P2】正文编排可选 Agent：决策上下文大时升级

- **现状**：正文编排（`contentGenerationTask.cjs:3901`）走 `aiService.collectJsonResponse`，**从不走 Agent**。但编排需读取知识库、全局事实、项目概述等大上下文（报告 §4.1）。
- **建议**：将 `shouldUseAgentForMessages` 也应用到正文编排——超阈值时改为 `runContentAgentTask`（`outputFile: 'content-plan.json'`，files 含 `knowledge-contents.md` + `global-facts.md` + `project-overview.md`），Agent 输出编排 JSON 后走现有 `resolveContentPlan` 校验。
- **证据**：`:3901`；报告 §4.1。
- **收益**：编排决策不再受单次上下文窗口限制，可容纳更大知识库。
- **风险**：低——编排输出已是结构化 JSON，Agent 文件模式天然契合。

### C-3【P2】批量章节合并生成：一次 Agent 运行多章节

- **现状**：正文生成逐章节调用（`runOne` 循环，`:4201-4274`），每章节一次 Agent 往返（超阈值时）。
- **建议**：
  1. 对连续若干章节（如同一父节点的多个子节点，数量 ≤ 5）合并为一次 Agent 运行：workspace 写入 `sections/{id}.md` 多文件 + 一个 `section-index.md` 索引，Agent 逐节输出到 `sections/{id}.out.md`。
  2. 业务层按索引回收各章节输出，`saveSection` 逐节回写（复用现有回写逻辑）。
  3. 仅当章节数 ≥ 阈值且均已超阈值时启用，否则保持逐节。
- **证据**：`:4204-4274`（逐节循环）；报告 §1.3（文件模式天然支持多文件）。
- **收益**：正文生成 Agent 往返次数显著下降；token 省多次主指令重述与上下文重复装载。
- **风险**：中——单次运行失败会影响整批，需在批量内实现"部分成功"（成功章节已写、失败章节重试/降级）。

### C-4【P3】一致性修复与正文生成衔接：复用修复结果

- **现状**：全文一致性修复（`:5607`）是独立 Agent 运行，修复后全文回写，但**不回流**为下一轮正文生成的 `restored-content` 输入（报告 §4.4/§5.2）。
- **建议**：修复完成并回写后，将该章节修复结果标记为 `source: repaired`，作为后续再生成/优化扩写的 `restoredContent` 优先输入，避免重复生成。
- **证据**：`:5607` 修复路径；`:4221`（`buildRestoredChapterContentMessages` 以 `restoredContent: previousContent` 为输入）。
- **收益**：减少"修复后又被重新生成覆盖"的无效往返。
- **风险**：低——仅调整数据流标记，不改生成逻辑。

---

## 5. 四层管线通用优化建议

### G-1【P1】运行时失败自动降级（调度层/运行时层）

- **现状**：仅有 **SDK 加载失败**降级（`piSessionFactory.cjs` 返回 `AGENT_REPORTED_FAILURE_CODE`，业务层可降级 `aiService.chat`，报告 §2.3.1）；**Agent 运行中失败**（模型错误、代理超时、输出非 JSON）无自动降级。
- **建议**：在 `agentService.runTask` 或 `piRuntimeService.runTask` 捕获失败后，若 `payload.fallback_to_chat` 为真，则用 `aiService.chat({ messages: 由 prompt+files 重建 })` 兜底，并将 `agent_failed: true` 写入结果与开发者日志。
- **证据**：报告 §2.1.3（结果/错误归一化）、§2.3.1（已有降级先例）。
- **收益**：关键业务路径（目录生成、正文生成、一致性修复）不因 Agent 环境/运行时故障而整体失败。
- **风险**：低——复用已有降级语义，新增 `fallback_to_chat` 开关。

### G-2【P2】输入指纹缓存（调度层）

- **现状**：相同输入（同任务、同知识库、同原方案）会重复触发 Agent 运行，无缓存（报告 §2.1.2）。
- **建议**：在 `startTask` 前计算输入指纹（`hash(prompt + files 内容摘要 + 模型)`），存入 `piPersistentTaskStore` 旁路缓存；命中且未过期（如 TTL 30 分钟）则直接返回上次 `output_content`，跳过 Agent 运行。
- **证据**：`agentService.cjs` 调度层 + `piPersistentTaskStore.cjs`（持久化键已存在）；报告 §2.1.1/§2.1.2。
- **收益**：重复生成/续跑场景节省整段 Agent 调用；与断点持久化互补（断点=中断续跑，缓存=重复跳过）。
- **风险**：低——仅命中确定性输入，需含模型名与 prompt 版本防止陈旧。

### G-3【P3】启动预检与诊断前置（接入层/运行时层）

- **现状**：诊断自检（`piDiagnostics.cjs`：回环/文本模型/工具环境）在**运行时**触发（报告 §2.5），但主任务已开始。
- **建议**：在 `createPiSession` 前先跑 `runPiTextModelSelfCheck` + `runPiToolEnvironmentSelfCheck`（快速、非破坏），失败则提前降级/告警，避免主任务在坏环境里空转。
- **证据**：报告 §2.5（`diagnosePiSelfCheck` / `createPiDiagnosticSections`）。
- **收益**：环境故障时主任务快速失败/降级，不浪费整轮 Agent 调用。
- **风险**：低——预检本身轻量；可加"仅 debug 模式"开关。

### G-4【P3】运行时指标扩展（可观测性）

- **现状**：`agentRuntimeAnalytics.cjs` 的 `trackAgentRuntime` 已记录运行时指标（报告 §2.5 引用），但无成本估算与阈值告警。
- **建议**：在 `runTask` 完成后基于 `output_metrics`（字数/字符数）+ 模型单价估算 token 成本，超阈值写入开发者日志并上报 `onActivity`。
- **证据**：报告 §2.2.4（`writeDeveloperLog` 已记录 `output_metrics`）。
- **收益**：长任务成本可追溯，为 P1/P2 优化提供量化验收依据。
- **风险**：低。

---

## 6. 建议优先级汇总表

| 编号 | 建议 | 环节 | 优先级 | 收益 | 风险 |
|------|------|------|--------|------|------|
| O-1 | `max_retries` 参数化，启用"校验→重试" | 目录生成 | P1 | 降失败率 | 低 |
| O-2 | 阶段合并，减少串行往返 | 目录生成 | P2 | 降延迟/成本 | 中 |
| O-3 | 校验失败降级 `aiService` 兜底 | 目录生成 | P2 | 提可靠性 | 中 |
| C-1 | 阈值动态化 + 普通生成可选 Agent | 正文生成 | P1 | 防溢出 | 中 |
| C-2 | 正文编排可选 Agent | 正文生成 | P2 | 扩决策上下文 | 低 |
| C-3 | 批量章节合并生成 | 正文生成 | P2 | 降往返/成本 | 中 |
| C-4 | 一致性修复结果回流复用 | 正文生成 | P3 | 减无效往返 | 低 |
| G-1 | 运行时失败自动降级 | 通用 | P1 | 提可靠性 | 低 |
| G-2 | 输入指纹缓存 | 通用 | P2 | 省调用 | 低 |
| G-3 | 启动预检与诊断前置 | 通用 | P3 | 快失败 | 低 |
| G-4 | 运行时指标扩展 | 通用 | P3 | 可观测性 | 低 |

---

## 7. 实施路线图与验收标准

### 7.1 实施顺序（P1 先行）

1. **第一批（可靠性，P1）**：O-1（目录重试）、C-1（阈值动态化+普通 Agent 可选）、G-1（失败自动降级）。
2. **第二批（效率，P2）**：O-2（阶段合并）、O-3（目录降级）、C-2（编排 Agent）、C-3（批量合并）、G-2（缓存）。
3. **第三批（体验/观测，P3）**：C-4、G-3、G-4。

### 7.2 验收标准（可量化）

- **O-1**：目录生成失败率（统计 N≥50 次）较改造前下降 ≥50%。
- **C-1**：构造"知识库极大 + 无原方案"用例，普通正文生成不再因上下文溢出报错，且走 Agent 时输出非空。
- **G-1**：模拟 Agent 运行时故障（断网/超时），目录与正文仍能通过 `aiService` 兜底完成，日志含 `agent_failed: true`。
- **O-2/C-3**：目录与正文生成端到端 Agent 往返次数下降 ≥20%；输出内容与改造前逐节结果一致（抽样 diff）。
- **G-2**：同输入重复调用命中缓存，跳过 Agent 运行，返回一致 `output_content`。
- **回归底线**：所有 P1/P2 改动以**开关默认关闭或保持现状行为**为前提（原则 B），默认配置下输出与改造前一致。

### 7.3 风险控制

- **断点兼容**：O-2 改 `workflow_stage` 命名时，保留旧阶段名别名，确保进行中/已持久化任务可续跑。
- **质量回退**：O-3 / C-1 的降级路径输出质量可能低于 Agent 多步推理，统一在结果与日志标注 `degraded: true`，供事后审计。
- **成本上升**：O-1 重试、C-3 批量合并可能改变 token 消耗，用 G-4 指标追踪验证净收益为正。

---

## 8. 方案说明

本方案基于对 `OpenBidKit_Yibiao-main-2026-09-14` 源码的**只读静态分析**与《OpenBidKit_pi_agent_工作机制报告.md》，所有建议均引用报告或源码行号证据。`AGENT_CONTEXT_THRESHOLD_RATIO`、`max_retries` 等参数的实际值以软件运行时配置为准；如与软件实际运行行为存在出入，应以实际运行行为为准。落地前需在测试环境验证 7.2 节验收标准，确认无回归后再灰度上线。
