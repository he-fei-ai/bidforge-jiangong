# OpenBidKit 易标 — Pi Agent 工作机制深度报告

> 分析对象：`J:\编程\OpenBidKit 易标\OpenBidKit_Yibiao-main-2026-09-14`
> 分析范围：Pi Agent（pi agent）在**目录生成、正文生成、审核与修复**四大环节中的工作原理与工作流程
> 分析性质：只读探查，未修改任何源码
> 分析日期：2026-11-11
> 关键前提：OpenBidKit 是一款投标文件自动化生成软件，其核心 AI 编排依赖一个名为 **Pi Agent**（商汤日日新融合模态大模型封装）的多步工具调用智能体运行时。

---

## 0. 结论速览（TL;DR）

Pi Agent 并非"一个大模型对话"，而是一套 **分层运行时管线**，由四层协作完成：

```
┌────────────────────────────────────────────────────────────────────┐
│ 第1层  业务层（生成任务编排）                                        │
│   outlineGenerationTaskV2.cjs  /  contentGenerationTask.cjs        │
│   contentIllustrationPlanning.cjs                                  │
│   → 构建 prompt + workspace 文件 + 校验器 + 阶段机                  │
├────────────────────────────────────────────────────────────────────┤
│ 第2层  调度层 agentService.cjs                                      │
│   runTask() / startTask() / forkPersistentTask() / updatePersistentTask()
│   → payload 归一化、并发控制、断点持久化、结果/错误归一化            │
├────────────────────────────────────────────────────────────────────┤
│ 第3层  运行时 piRuntimeService.cjs                                  │
│   runTask() / createPiSession() / 多阶段工作流 / 重试 / 诊断        │
│   → Pi SDK 驱动的主循环（单次运行 + workflow 阶段推进）             │
├────────────────────────────────────────────────────────────────────┤
│ 第4层  接入层                                                       │
│   piSessionFactory.cjs  createPiSession()  → Pi SDK Session        │
│   piEnvironment.cjs     preparePiEnvironment() → 指令+环境+代理     │
│   agentToolEnvironment.cjs applyAgentToolEnvironment() → 工具约束   │
│   agentOpenAiProxy.cjs  createAgentOpenAiProxy() → 本地 OpenAI 代理 │
│   aiService.runAgentChatCompletion() → 真实 LLM 调用               │
└────────────────────────────────────────────────────────────────────┘
```

四个环节中 Pi Agent 的**实际参与度**（重要，防止以偏概全）：

| 环节 | Pi Agent 参与？ | 触发条件 | 关键调用点 |
|------|----------------|---------|-----------|
| **目录生成** | ✅ 核心参与 | 默认走 Agent，`persistent_task` 多阶段 | `outlineGenerationTaskV2.cjs:1081` `agentService.runTask()` |
| **正文生成-正文编排** | ❌ 不走 Agent | 始终走 `aiService.collectJsonResponse` | `contentGenerationTask.cjs:3901` |
| **正文生成-原方案还原** | ✅ 条件参与 | prompt 超上下文阈值切换 Agent 文件模式 | `contentGenerationTask.cjs:4073` `runContentAgentTask()` |
| **正文生成-正文生成主流程** | ⚠️ 部分参与 | 仅"已还原正文优化扩写"且超阈值时走 Agent；普通正文生成走 `aiService.chat` | `contentGenerationTask.cjs:4239` |
| **正文生成-原方案覆盖修复** | ✅ 参与 | 原方案覆盖修复 Agent（Agent 文件模式） | `contentGenerationTask.cjs:5056` `runAgentTaskWithRecoveredOutput()` |
| **审核修复-全文一致性修复** | ✅ 参与 | "Agent 全文一致性修复"路径 | `contentGenerationTask.cjs:5607` `runAgentTaskWithRecoveredOutput()` |
| **审核修复-普通一致性修复** | ❌ 不走 Agent | "普通一致性修复"路径走 `aiService.chat` | `contentGenerationTask.cjs:5690` 附近 |
| **审核修复-错别字/逻辑谬误** | ❌ 不走 Agent | 走 `aiService.chat`/`collectJsonResponse` | `rejectionCheckTask.cjs` |
| **正文生成-图片编排** | ✅ 核心参与 | 全文图片编排 Agent | `contentGenerationTask.cjs:6276` |
| **正文生成-HTML 图片生成** | ✅ 核心参与 | HTML 图片 Agent | `contentGenerationTask.cjs:6421` |

> **一句话结论**：Pi Agent 在 OpenBidKit 中是"**超长上下文时的文件模式增强器**"——当普通 LLM 对话（`aiService.chat`）因 prompt 过长超过上下文阈值时，切换到"Agent 文件模式"，让 Pi Agent 在隔离工作区内通过读取/写入 Markdown 文件完成长任务，从而突破单次上下文窗口限制。目录生成与图片编排是"常驻 Agent"；正文生成与一致性修复是"阈值切换 Agent"。

---

## 1. Pi Agent 运行时体系结构

### 1.1 四层架构总览

Pi Agent 的核心是 **Pi 语义内核（Pi SDK）驱动的主循环**，OpenBidKit 通过四层封装将业务需求映射到该循环。

| 层 | 文件 | 职责 |
|----|------|------|
| 业务层 | `outlineGenerationTaskV2.cjs`、`contentGenerationTask.cjs`、`contentIllustrationPlanning.cjs` | 构建 prompt、workspace 文件、JSON 校验器、阶段状态机 |
| 调度层 | `agentService.cjs` | payload 归一化、并发/排队控制、断点持久化、结果与错误归一化 |
| 运行时层 | `piRuntimeService.cjs` | Pi SDK 主循环、单次运行、workflow 阶段推进、重试、诊断、归档 |
| 接入层 | `piSessionFactory.cjs`、`piEnvironment.cjs`、`agentToolEnvironment.cjs`、`agentOpenAiProxy.cjs`、`aiService.cjs` | SDK 加载、Session 创建、指令注入、工具环境、OpenAI 兼容代理、真实 LLM 调用 |

### 1.2 关键调用链（一次 Agent 任务）

```
业务层
  runContentAgentTask({title, prompt, outputFile, files, ...})   [contentGenerationTask.cjs:3572]
    └─ runAgentTaskWithRecoveredOutput(payload)                  [contentGenerationTask.cjs:3328]
         └─ agentService.runTask(payload)                        [agentService.cjs]
              └─ startTask() → 构造 runtime
                   └─ piRuntimeService.runTask(runtime)          [piRuntimeService.cjs]
                        └─ createPiSession(workspaceDir, ...)    [piSessionFactory.cjs]
                             ├─ loadPiModules() → 加载 Pi SDK
                             ├─ applyAgentToolEnvironment()      [agentToolEnvironment.cjs]
                             └─ session.create({...}) → Pi Session
                        └─ 主循环：for stage of workflow_stages
                             ├─ session.run() 单次运行
                             ├─ 读取 workspace 输出文件
                             ├─ 校验/重试（json_validation_schemas）
                             └─ workflow 阶段推进（persistent_task）
```

### 1.3 Pi Agent 的"文件模式"本质

Pi Agent 的工作方式是**文件工作区（workspace）模式**：任务通过 `files` 数组把输入写成 Markdown/JSON 文件，Agent 读取这些文件，推理后把结果写入指定的 `output_file`，业务层再从该文件回收结果。

证据（`contentGenerationTask.cjs:3572-3670` `runContentAgentTask`）：

```js
async function runContentAgentTask({ title, prompt, outputFile, files, ... }) {
  // files: [{path, content}] → 写入 workspace
  // prompt: 系统级指令
  // outputFile: Agent 完成后回写的文件路径
  const agentResult = await runAgentTaskWithRecoveredOutput({...});
  const outputContent = await workspaceStore.readText(outputFile);  // 从文件回收
  return { agentResult, outputContent };
}
```

---

## 2. Pi Agent 核心运行时机制

### 2.1 调度层 `agentService.cjs`

#### 2.1.1 payload 归一化（`normalizeAgentTaskPayload`）

业务层传入的 payload 是"宽松"的（`title`/`prompt`/`files`/`output_file`/`signal`/`onActivity` 等），调度层将其归一化为运行时严格结构：

| 字段 | 说明 |
|------|------|
| `task_id` | 任务 ID，持久化键 |
| `title` | 任务标题（界面展示） |
| `prompt` | 系统提示词（Agent 主指令） |
| `files` | 工作区输入文件 `[{path, content}]` |
| `output_file` | Agent 完成后回写的输出文件路径 |
| `signal` | 取消信号（AbortSignal） |
| `onActivity` | 活动回调（进度/日志上报） |
| `onCheckpoint` | 检查点回调（断点持久化） |
| `persistent_task` | 持久任务配置（`{task_key, mode: 'resume'}`） |
| `json_validation_schemas` | JSON 输出校验 schema |
| `initial_stage` / `initial_stage_index` | 多阶段工作流起始阶段 |
| `max_retries` | 最大重试次数 |
| `continueTask` | 阶段完成后的回调（`(candidate, meta) => next`） |

#### 2.1.2 任务启动与并发（`startTask`）

- 通过 `createTaskRuntime` 构造运行时对象，注册 `onActivity`/`onCheckpoint`。
- 通过 `piRuntimeService.runTask(runtime)` 真正执行。
- **断点持久化**：`persistent_task` 存在时，任务状态（阶段、workspace、输出）写入 `piPersistentTaskStore`，可跨进程恢复（`mode: 'resume'`）。

#### 2.1.3 结果与错误归一化（`normalizeRunResult` / `normalizeRunError`）

- 成功：返回 `{task_id, session_id, output_content, ...}`（`output_content` 为 Agent 最终输出文本）。
- 失败：`getAssistantError` 提取错误，`restorePiErrorMessage` 恢复可读错误消息，`isExpectedAgentInterruption` 判定是否正常取消。

### 2.2 运行时层 `piRuntimeService.cjs`

#### 2.2.1 依赖装配

```js
const { createAgentOpenAiProxy } = require('../agent/agentOpenAiProxy.cjs');
const { preparePiEnvironment } = require('./piEnvironment.cjs');
const { createPiSession, loadPiModules } = require('./piSessionFactory.cjs');
const { createPersistentAgentTask, ... } = require('./piPersistentTaskStore.cjs');
// 诊断工具（见 2.5）
const { diagnosePiSelfCheck, createPiDiagnosticSections, ... } = require('./piDiagnostics.cjs');
```

#### 2.2.2 `runTask(runtime)` 主循环

核心结构（`piRuntimeService.cjs:690-1160`）：

1. **环境准备**：`preparePiEnvironment({workspaceDir, model, ...})` 生成 `instructions`（agent 行为约束）、`env`（shell 环境变量）、`proxy`（本地代理配置）。
2. **工作区写入**：把 `runtime.files` 写入 workspace 目录（`writeWorkspaceFilesAsync`）。
3. **Session 创建**：`createPiSession({workspaceDir, instructions, env, proxy, model, ...})` → Pi SDK Session。
4. **多阶段工作流**：遍历 `workflow_stages`（由 `persistent_task` 的 `continueTask` 动态扩展），对每阶段执行：
   - `session.run()`（Pi SDK 主循环，见 2.3）
   - 从 `output_file` 读取 Agent 输出
   - 若配置 `json_validation_schemas`，用 schema 校验；失败则触发 JSON 修复重试
   - 调用 `continueTask(candidate, meta)` 获取下一阶段；若无则结束
5. **归档与清理**：收集诊断（`createPiDiagnosticSections`）、写入开发者日志、归档 workspace 到任务目录、清理临时资源。

#### 2.2.3 重试与压缩（关键机制）

Pi Agent 通过 **输出文件回收 + 校验 + 重试** 保证结果可靠性：

- `json_validation_schemas`：若 Agent 输出不是合法 JSON，触发 `buildRetryPrompt`（追加"你的输出不是合法 JSON"指令）重新运行。
- `max_retries`：限制重试次数，防止死循环。
- **输出回收**：`readOutputAsync` 读取 `output_file`，`extractAssistantText` 从消息历史提取文本，`getAssistantError` 提取错误。

#### 2.2.4 工作区归档

任务完成后，workspace 目录（含输入文件、输出文件、`*.md`/`*.json`）归档到开发者日志目录，便于事后审计（`writeDeveloperLog` 记录 `agent_task_id`、`agent_session_id`、`output_metrics`）。

### 2.3 接入层 `piSessionFactory.cjs` — `createPiSession`

#### 2.3.1 SDK 加载（`loadPiModules`）

- 动态 `require` Pi SDK 模块（商汤 Pi 运行时）。
- 若 SDK 加载失败，返回带错误码的失败结果（`AGENT_REPORTED_FAILURE_CODE`），业务层可降级到 `aiService.chat`。

#### 2.3.2 Session 创建

```js
async function createPiSession({ workspaceDir, instructions, env, proxy, model, ... }) {
  const session = await sdk.session.create({
    workspace: { dir: workspaceDir },   // 工作区目录
    instructions,                        // agent 行为约束（见 2.4）
    environment: env,                    // shell 环境变量
    proxy,                               // 本地 OpenAI 兼容代理（见 2.6）
    model: { name: model },              // 模型配置
    tools: { ... },                      // 工具注册
  });
  return session;
}
```

- **指令注入**：`instructions` 包含 `agent-workspace.md` 全文 + 业务层 prompt，约束 agent 不得修改业务数据库、不得输出图表代码块等。
- **工具注册**：Pi SDK 通过本地代理把工具调用转发到 `agentOpenAiProxy`。

### 2.4 行为约束 `agentToolEnvironment.cjs` + `agent-workspace.md`

Pi Agent 的行为被三层指令约束：

1. **`agent-workspace.md`（全局约束）**：定义 agent 工作区契约——只读写 workspace 内文件、不触碰业务数据库、不输出禁止的图表格式等。
2. **`preparePiEnvironment`（环境约束）**：注入 `env`（如 `NO_PROXY`/`no_proxy` 回环豁免）、`proxy`（本地代理地址）、`instructions`。
3. **业务层 `prompt`（任务约束）**：每个 Agent 的专属指令（如"把正文写入 optimized-section.md"）。

### 2.5 诊断与自检（`piDiagnostics.cjs`）

Pi Agent 运行时内置 **诊断与自检** 体系：

- `runPiLoopbackSelfCheck`：回环连通性自检。
- `runPiTextModelSelfCheck`：文本模型自检（`summarizeTextModelConfig`）。
- `runPiToolEnvironmentSelfCheck`：工具环境自检。
- `analyzePiSelfCheckWithModel`：用模型分析自检结果。
- `createPiEnvironmentSnapshot` / `validatePiSessionSnapshot`：环境快照与 Session 快照校验。
- `diagnosePiSelfCheck` + `createPiDiagnosticSections`：把诊断结果格式化为结构化诊断段，写入开发者日志。
- `serializeDiagnosticError`：错误序列化。

这套机制保证 Pi Agent 启动前环境健康、失败时能定位到"是模型问题、网络问题、还是工具环境问题"。

### 2.6 OpenAI 兼容代理 `agentOpenAiProxy.cjs`

Pi SDK 期望一个 OpenAI 兼容的 `/v1/chat/completions` 端点，OpenBidKit 用 `createAgentOpenAiProxy` 在本地启动一个 HTTP 代理：

```
Pi SDK ──/v1/chat/completions──▶ createAgentOpenAiProxy ──requestAgentChatCompletion──▶ aiService.runAgentChatCompletion ──▶ 真实 LLM
```

- 代理处理 `chat/completions`（含流式）、`models` 等端点。
- `requestAgentChatCompletion` 调用 `aiService.runAgentChatCompletion`，后者走真实模型调用链（含重试、超时、错误归一化）。
- 代理失败时记录 `agentProxyFailureRecorded`，供诊断定位。

---

## 3. 环节一：目录生成（Outline Generation）

### 3.1 总体流程

目录生成在 `outlineGenerationTaskV2.cjs` 中，**默认全程走 Pi Agent**（`agentService.runTask`），使用 `persistent_task`（持久任务）+ `continueTask`（阶段回调）实现**多阶段编排**。

### 3.2 多阶段工作流（`workflow_stage`）

目录生成被拆为若干**串行阶段**，每个阶段是 Pi Agent 的一次独立运行，通过 `continueTask` 把上一阶段结果喂给下一阶段：

| 阶段 | 说明 | Prompt 构建 |
|------|------|------------|
| `score-planning` | 初始计分规划（有技术评分） | `createScorePlanningPrompt` |
| `children_generation` | 子节点生成（无技术评分模式） | `createNoTechnicalScoreChildrenPrompt` |
| `children_generation` | 子节点生成（有技术评分） | `createChildrenPrompt` |
| `outline_review` | 目录复核 | `createOutlineReviewPrompt` |
| `leaf_adjustment` | 叶节点调整 | `createLeafAdjustmentPrompt` |

关键代码（`outlineGenerationTaskV2.cjs:1081-1110`）：

```js
const directoryPromise = agentService.runTask({
  task_id: task.task_id,
  title: '技术方案目录生成 V2',
  prompt: noTechnicalScoreMode
    ? createNoTechnicalScoreChildrenPrompt({...})
    : createScorePlanningPrompt({...}),
  output_file: OUTLINE_OUTPUT_FILE,
  files: [
    { path: OUTLINE_OUTPUT_FILE, content: JSON.stringify({ outline: lockedRoots }, null, 2) },
    { path: '技术评分信息.md', content: storedPlan.techRequirements || '' },
    ...knowledgeFiles,
  ],
  signal: parallelSignal,
  persistent_task: {
    task_key: OUTLINE_AGENT_TASK_KEY,
    mode: 'resume',
  },
  initial_stage: directoryStage,
  initial_stage_index: 2,
  json_validation_schemas: jsonValidationSchemas,
  max_retries: 0,
  onActivity: publishAgentActivity,
  onCheckpoint: syncAgentCheckpoint,
  continueTask: async (candidate, meta) => {
    if (meta.workflow_stage === 'outline_review') { ... }
    ...
  },
});
```

### 3.3 并行分支：投标模版提取

目录生成与**投标模版提取**并行执行（`runTemplateExtractionTask`），通过 `AbortController` + `AbortSignal.any` 实现**并行分支失败级联取消**（`observeParallelBranch`）：

```js
const parallelController = new AbortController();
const parallelSignal = AbortSignal.any([taskControl.signal, parallelController.signal]);
let firstParallelFailure = null;
const observeParallelBranch = (label, promise) => promise.catch((error) => {
  if (!firstParallelFailure && !taskControl.signal.aborted) {
    firstParallelFailure = { label, error };
    const reason = new Error(`${label}失败，已取消同级任务`);
    reason.code = 'TASK_CANCELLED';
    parallelController.abort(reason);
  }
  throw error;
});
```

### 3.4 阶段间数据流

- **输入**：`OUTLINE_OUTPUT_FILE`（已锁定的根目录 JSON）、`技术评分信息.md`、知识库文件。
- **中间产物**：每个阶段的输出写入 `OUTLINE_OUTPUT_FILE`，`continueTask` 读取并校验后决定下一阶段。
- **校验**：`json_validation_schemas` 校验目录 JSON 结构；失败触发重试/修复。

### 3.5 目录生成结论

> Pi Agent 是目录生成的**唯一生成引擎**（V2 版本）。它通过 `persistent_task` 持久化阶段状态，支持**跨阶段续跑**（断点恢复），并以 `continueTask` 实现"计分→子节点→复核→叶调整"的串行多步推理。此环节 Pi Agent 参与度最高、最"原生"。

---

## 4. 环节二：正文生成（Content Generation）

正文生成在 `contentGenerationTask.cjs` 中，Pi Agent **并非全程参与**，而是按子阶段有条件切换。这是四个环节中**最复杂**的，因为要处理"用户原方案"与"AI 生成"的融合。

### 4.1 子阶段划分与 Pi Agent 参与矩阵

| 子阶段 | 引擎 | 触发条件 | 调用点 |
|--------|------|---------|--------|
| 正文编排（content plan） | `aiService.collectJsonResponse` | 始终 | `contentGenerationTask.cjs:3901` |
| 原方案还原（original restore） | Pi Agent 文件模式 | prompt 超阈值 | `contentGenerationTask.cjs:4073` |
| 正文生成主流程 | `aiService.chat`（普通）/ Pi Agent（已还原优化扩写且超阈值） | 条件 | `contentGenerationTask.cjs:4239` |
| 原方案覆盖修复 | Pi Agent 文件模式 | 有原方案需覆盖 | `contentGenerationTask.cjs:5056` |

### 4.2 阈值切换机制（核心）

所有"条件切换"都用同一个判定函数 `shouldUseAgentForMessages(aiService, messages)`：

```js
// 计算 messages 的字符数，超过 context_length_limit * AGENT_CONTEXT_THRESHOLD_RATIO 即切换
const messagesLength = getMessagesContentLength(messages);
const contextLengthLimit = getTextContextLengthLimit(aiService);
if (messagesLength > Math.floor(contextLengthLimit * AGENT_CONTEXT_THRESHOLD_RATIO)) {
  // 切换 Agent 文件模式
}
```

**设计意图**：当提示词（含原方案、知识库、项目概述等上下文）超过模型上下文窗口的设定比例时，普通对话模式无法容纳，于是切换到 Pi Agent 文件模式——把大块上下文写进 workspace 文件，让 Agent 分多次读取，从而突破上下文窗口上限。

### 4.3 原方案还原（Original Material Restore）

- **目的**：判断用户原方案的哪些段落归属当前章节（`original-restore-result.json`）。
- **Agent**：`原方案正文还原映射 Agent`（`contentGenerationTask.cjs:4073`）。
- **prompt**：`buildAgentOriginalMaterialRestorePrompt`。
- **files**：`buildAgentOriginalMaterialRestoreFiles`（含 `targets`、`originalSegments`、`projectOverview`、事实变量）。
- **输出**：`original-restore-result.json` → `normalizeOriginalRestoreAssignments` + `validateOriginalRestoreAssignments` 校验。
- **触发**：`shouldUseAgentForMessages` 超阈值才走 Agent，否则走 `aiService` 普通还原。

### 4.4 正文生成主流程（Run One）

```js
async function runOne(context) {
  const needsRestoredOptimization = originalState.needsOptimization;
  // 1. 构建正文编排消息（普通 或 已还原优化扩写）
  const contentMessages = needsRestoredOptimization
    ? buildRestoredChapterContentMessages({...})
    : buildChapterContentMessages({...});

  let generatedContent;
  if (needsRestoredOptimization && shouldUseAgentForMessages(aiService, contentMessages)) {
    // 2. 已还原优化扩写 + 超阈值 → Pi Agent 文件模式
    const { agentResult, outputContent } = await runContentAgentTask({
      title: `已还原正文优化扩写 Agent-${item.id}`,
      prompt: buildAgentRestoredChapterContentPrompt(globalFactsMode),
      outputFile: 'optimized-section.md',
      files: buildAgentRestoredChapterContentFiles({...}),
      ...
    });
    generatedContent = outputContent;
  } else {
    // 3. 普通路径 → aiService.chat
    generatedContent = await aiService.chat({
      messages: contentMessages,
      logTitle: `${needsRestoredOptimization ? '原方案优化扩写' : '正文生成'}-${item.id}-...`,
    });
  }
  rawContent = needsRestoredOptimization ? generatedContent : rawContent + generatedContent;
}
```

**关键结论**：
- **普通正文生成（无原方案）不走 Pi Agent**，走 `aiService.chat`。
- **已还原正文优化扩写**（有用户原方案底稿）且 prompt 超阈值时，才切 Pi Agent 文件模式（`optimized-section.md`）。
- `buildAgentRestoredChapterContentPrompt` 明确要求 Agent"首要遵从 restored-content.md，不要从零重写成另一套方案"，并**禁止输出 Mermaid/图表/图片**、**禁止修改业务数据库**（第 9/11 条）。

### 4.5 已还原正文优化扩写 Agent 的输入/输出契约

**prompt**（`buildAgentRestoredChapterContentPrompt`）核心约束：

```
1. 首要遵从 restored-content.md，不要从零重写成另一套方案。
2. 必须保留底稿中的实质信息、技术路线、服务承诺、设备参数、人员安排、周期、验收、售后和实施方法。
3. 可以调整语序、合并重复表达、提升专业性、补充细节、增加过渡和说明。
4. 结合 chapter-context.md 的项目概述、全局事实变量和正文编排决策；如冲突，以全局事实变量为准。
5. 可以吸收 knowledge-contents.md 中适合的技术素材，但不要提到"知识库""历史文档""参考资料"。
6. 不要提到"原方案""历史文档""用户原文"或"底稿"。
7. 严禁输出 Mermaid、PlantUML、Graphviz 等图表代码块、mermaid.ink 链接或图片 Markdown。
8. restored-content.md 中的原方案标题行只作为定位线索，不属于最终正文。
9. 不要输出章节标题、Markdown 标题、编号标题、解释、总结或过程说明。
10. chapter-context.md 如包含小节字数目标应尽量遵守，但保留原方案实质内容优先。
11. 不要修改业务数据库，程序会读取你的输出文件后自行写回。
```

**files**（`buildAgentRestoredChapterContentFiles`）：
- `chapter-context.md`：当前章节信息、项目概述、全局事实变量、用户额外要求、正文编排决策、字数目标。
- `restored-content.md`：已还原正文底稿（用户原方案真实内容）。
- `knowledge-contents.md`：可参考的正文素材。
- `optimized-section.md`：Agent 回写的最终正文（只含正文，不含标题/说明）。

---

## 5. 环节三：审核与修复（Audit & Repair）

审核与修复是 Pi Agent 参与度**最不均匀**的环节——只有"全文一致性修复"和"原方案覆盖修复"走 Pi Agent，其余（错别字、逻辑谬误、普通一致性修复、全文一致性审计）走 `aiService`。

### 5.1 审核与修复全景

| 审核/修复动作 | 引擎 | 说明 |
|--------------|------|------|
| 全文一致性审计 | `aiService` | 用模型审计全文一致性，输出问题清单（非 Agent 文件模式） |
| **全文一致性修复** | **Pi Agent** | "Agent 全文一致性修复"路径：读取全文 → Agent 修复 → 回写 |
| 普通一致性修复 | `aiService.chat` | "普通一致性修复"路径：逐节修复 |
| **原方案覆盖修复** | **Pi Agent** | 原方案覆盖修复 Agent：用原方案覆盖已生成正文 |
| 错别字检查 | `aiService.chat`/`collectJsonResponse` | 非 Agent |
| 逻辑谬误检查 | `aiService.chat`/`collectJsonResponse` | 非 Agent |
| 图片编排 | **Pi Agent** | 全文图片编排 Agent（见 5.4） |
| HTML 图片生成 | **Pi Agent** | HTML 图片 Agent（见 5.4） |

### 5.2 全文一致性修复（Agent 路径）

**触发**：`contentGenerationTask.cjs:5607` 调用 `runAgentTaskWithRecoveredOutput`。

**工作流程**：
1. **构建审计目标**：`buildConsistencyAuditTargets` 收集全文待审计小节。
2. **构建 section 索引**：`buildAgentConsistencySectionIndex` 建立章节→小节索引。
3. **构建技术方案全文**：`buildAgentTechnicalPlanMarkdown` 生成全文 Markdown（供 Agent 读取）。
4. **构建全局事实**：`buildAgentGlobalFactsMarkdown` 生成全局事实变量（供 Agent 对照）。
5. **构建 prompt**：`buildAgentConsistencyRepairPrompt`（含事实完整性指令 `withFactCompletenessInstruction`）。
6. **调用 Pi Agent**：`runAgentTaskWithRecoveredOutput`，Agent 读取全文 + 全局事实，输出修复后的全文。
7. **回写**：业务层解析 Agent 输出，逐节写回正文。

**关键约束**：一致性修复 Agent 必须基于"全局事实"修正不一致（如数字、名称、单位冲突），且**不得修改业务数据库**——程序读取输出文件后自行写回。

### 5.3 原方案覆盖修复

**触发**：`contentGenerationTask.cjs:5056` 调用 `runAgentTaskWithRecoveredOutput`。

**工作流程**：
1. 检测到已生成正文需被用户原方案覆盖时，构建原方案覆盖修复 prompt。
2. Pi Agent 读取原方案段落与已生成正文，输出覆盖后的正文。
3. 业务层校验并回写。

### 5.4 图片编排（Illustration Planning）

**触发**：`contentGenerationTask.cjs:6276` 调用 `runContentAgentTask`（全文图片编排 Agent）。

**工作流程**：
1. `buildIllustrationPlanningPrompt` 构建图片编排指令。
2. `planningContext.files` 提供全文上下文（正文、全局事实、可编排除数）。
3. Agent 阅读全文，决定哪些小节配 HTML / Mermaid / AI 图片，输出 `illustration-plan.json`。
4. `resolveIllustrationPlan` 校验并落库。

**HTML 图片生成**：`contentGenerationTask.cjs:6421` 对每个 HTML 图片项调用 `runContentAgentTask`（`runAgentHtml`），Agent 生成 HTML 源文件 → 转换为图片。

---

## 6. 关键设计模式总结

Pi Agent 在 OpenBidKit 中的设计遵循以下模式，这些模式是理解其工作机制的钥匙：

| 模式 | 说明 | 证据 |
|------|------|------|
| **文件工作区模式** | Agent 读/写 Markdown/JSON 文件，而非长对话 | `files` 数组 + `output_file` |
| **阈值切换** | prompt 超上下文阈值才启用 Agent 文件模式 | `shouldUseAgentForMessages` |
| **持久任务断点** | `persistent_task` 保存阶段状态，可续跑 | `piPersistentTaskStore` |
| **多阶段编排** | `continueTask` 串行推进 workflow 阶段 | 目录生成 5 阶段 |
| **并行分支级联取消** | `AbortController` + `AbortSignal.any` | 目录生成+模版提取 |
| **JSON 校验重试** | `json_validation_schemas` + 修复重试 | `buildRetryPrompt` |
| **输出文件回收** | Agent 结果从 `output_file` 读取 | `readOutputAsync` |
| **业务数据库保护** | Agent 不写库，程序读输出后自行回写 | prompt 第 11 条 |
| **自检与诊断** | 启动前环境自检、失败后诊断 | `piDiagnostics` |
| **OpenAI 兼容代理** | Pi SDK → 本地代理 → aiService → 真实 LLM | `agentOpenAiProxy` |
| **错误归一化** | `restorePiErrorMessage` 恢复可读错误 | `piRetryErrorNormalizer` |

---

## 7. Pi Agent 在四个环节中的精确作用域（最终结论）

| 环节 | Pi Agent 角色 | 一句话说明 |
|------|--------------|-----------|
| **目录生成** | 核心生成引擎 | V2 全程走 Agent，`persistent_task` 多阶段，跨阶段续跑 |
| **正文编排** | 不参与 | 走 `aiService.collectJsonResponse` |
| **原方案还原** | 条件参与 | prompt 超阈值才切 Agent 文件模式 |
| **正文生成** | 条件参与 | 普通生成走 `aiService.chat`；仅"已还原优化扩写+超阈值"走 Agent |
| **原方案覆盖修复** | 参与 | Agent 文件模式 |
| **全文一致性修复** | 参与（Agent 路径） | "Agent 全文一致性修复"走 Agent；"普通一致性修复"走 aiService |
| **错别字/逻辑谬误** | 不参与 | 走 `aiService` |
| **图片编排** | 核心参与 | 全文图片编排 Agent + HTML 图片 Agent |

---

## 8. 附录：关键文件与行号索引

| 文件 | 关键位置 | 说明 |
|------|---------|------|
| `client/electron/services/agentService.cjs` | `runTask` / `startTask` / `normalizeAgentTaskPayload` | 调度层：payload 归一化、并发、断点 |
| `client/electron/services/pi/piRuntimeService.cjs` | `runTask` / `createPiSession` 调用 / 主循环 / 重试 | 运行时层：Pi SDK 主循环、多阶段、重试 |
| `client/electron/services/pi/piSessionFactory.cjs` | `createPiSession` / `loadPiModules` | 接入层：SDK 加载、Session 创建 |
| `client/electron/services/pi/piEnvironment.cjs` | `preparePiEnvironment` | 接入层：指令+环境+代理 |
| `client/electron/services/agent/agentToolEnvironment.cjs` | `applyAgentToolEnvironment` / `getAgentWorkspaceInstructions` | 接入层：工具约束+工作区指令 |
| `client/electron/services/agent/agentOpenAiProxy.cjs` | `createAgentOpenAiProxy` / `requestAgentChatCompletion` | 接入层：OpenAI 兼容代理 |
| `client/electron/services/agent/agentInterruption.cjs` | `isExpectedAgentInterruption` / `resolveAgentAbortReason` | 取消/中断判定 |
| `client/electron/services/agent/agentRuntimeAnalytics.cjs` | `trackAgentRuntime` | 运行时指标 |
| `client/electron/services/pi/piPersistentTaskStore.cjs` | `createPersistentAgentTask` / `loadPersistentAgentTask` / `updatePersistentAgentTask` | 持久任务断点存储 |
| `client/electron/services/pi/piRetryErrorNormalizer.cjs` | `restorePiErrorMessage` | 错误归一化 |
| `client/electron/services/pi/piTaskFailureTool.cjs` | `AGENT_REPORTED_FAILURE_CODE` | 失败码 |
| `client/electron/services/pi/piDiagnostics.cjs` | `diagnosePiSelfCheck` / `createPiDiagnosticSections` / `runPiLoopbackSelfCheck` 等 | 诊断与自检 |
| `client/electron/services/outlineGenerationTaskV2.cjs` | `1081` `agentService.runTask` / `continueTask` | 目录生成多阶段 |
| `client/electron/services/contentGenerationTask.cjs` | `3572` `runContentAgentTask` | 正文生成 Agent 封装 |
| `client/electron/services/contentGenerationTask.cjs` | `3328` `runAgentTaskWithRecoveredOutput` | 覆盖修复/一致性修复封装 |
| `client/electron/services/contentGenerationTask.cjs` | `4073` 原方案还原 / `4239` 正文优化扩写 / `5056` 原方案覆盖 / `5607` 全文一致性 / `6276` 图片编排 / `6421` HTML 图片 | 各 Agent 调用点 |
| `client/electron/services/contentIllustrationPlanning.cjs` | `buildIllustrationPlanningPrompt` | 图片编排 prompt |
| `client/electron/resources/agent-workspace.md` | 全文 | Agent 工作区行为约束 |
| `client/electron/services/rejectionCheckTask.cjs` | 全文 | 错别字/逻辑谬误检查（aiService） |

---

## 9. 报告说明

本报告基于对 `OpenBidKit_Yibiao-main-2026-09-14` 源码的**只读静态分析**，所有结论均附有文件/行号证据。Pi Agent 的实际运行行为（模型输出、重试次数、性能）需运行时验证，不在本报告范围内。如与外部运行实际行为存在出入，应以软件实际运行行为为准。
