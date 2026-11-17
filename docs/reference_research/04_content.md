# 易标「正文生成模块」源码考古报告

**仓库**：`J:\编程\OpenBidKit 易标\OpenBidKit_Yibiao-main-2026-09-14`（只读，未修改任何文件）
**主体文件**：`client/electron/services/contentGenerationTask.cjs`（6,758 行 / 319KB）

> 约定：下文所有 `文件:行号` 均为实际读取所得；未在源码中找到的，标注「**源码中未找到**」。

---

## 0. 关键命名对不上的三项（先说结论，避免误导后续设计）

| 提问中的名词 | 实际源码 |
|---|---|
| `ChapterContentRequest` | **源码中未找到**。等价物是 IPC 载荷 `payload`（`contentGenerationTask.cjs:2915` 解构）+ 前端 `ContentGenerationOptions`（`types.ts:49-62`） |
| `should_continue_round` | **源码中未找到**。最接近的三个机制：① 上下文超限转 Agent（`shouldUseAgentForMessages`，`:473`）② 字数调整轮次 `MAX_WORD_ADJUSTMENT_ROUNDS`（`:28`）③ 扩写无进展退出 `MAX_EXPANSION_NO_PROGRESS_ROUNDS`（`:30`） |
| `priority 3/4/5 语义` | 仅存在于**配图计划**而非正文编排。值域 1-5 整数，`5`=最值得配图（`contentIllustrationPlanning.cjs:160`、`:242`）；**3/4 的具体语义源码中未定义** |

---

## 1. 请求结构完整字段定义（`ChapterContentRequest` 的真实等价物）

### 1.1 Main 进程入口解构 —— `contentGenerationTask.cjs:2915-2917`

```js
async function runContentGenerationTask({ aiService, agentService, workspaceStore, knowledgeBaseService, updateTask: updateManagedTask, checkpointTask: checkpointManagedTask, payload, taskControl, previousState }) {
  const resume = Boolean(payload.resume);
  const storedPlan = resume ? (previousState || {}) : (workspaceStore.loadTechnicalPlan() || {});
```

### 1.2 `payload` 全部被读取的字段 —— `contentGenerationTask.cjs:2916-2992, 3132`

```js
2916:  const resume = Boolean(payload.resume);
2960:  const retryContentCorrection = !resume && Boolean(payload.retryContentCorrection ?? payload.retry_content_correction);
2961:  const rerunIllustrations = !resume && Boolean(payload.rerunIllustrations ?? payload.rerun_illustrations);
2962:  const retryFailedSections = !resume && Boolean(payload.retryFailedSections ?? payload.retry_failed_sections);
2963:  const continuePostProcessing = !resume && Boolean(payload.continuePostProcessing ?? payload.continue_post_processing);
2973:  const regenerate = !resume && !retryContentCorrection && !rerunIllustrations && !retryFailedSections && !continuePostProcessing && Boolean(payload.regenerate);
2974:  const targetItemId = resume ? contentRuntime.target_item_id : String(payload.targetItemId || '').trim();
2989:  const regenerateRequirement = resume ? contentRuntime.regenerate_requirement : String(payload.requirement || '').trim();
2992:    : payload.generationOptions || payload.generation_options || storedPlan.contentGenerationOptions || {};
3132:    && Boolean(payload.simulatePartialFailures ?? payload.simulate_partial_failures)
```

字段清单（camelCase 与 snake_case 双写兼容）：

| 字段 | 类型 | 语义 | 引用 |
|---|---|---|---|
| `resume` | bool | 继续已暂停任务（互斥其余全部开关） | `:2916` |
| `regenerate` | bool | 全量重新生成（会清 Mermaid 缓存 + 清空正文） | `:2973, 2978-2982` |
| `targetItemId` | string | 单小节重生成 | `:2974` |
| `requirement` | string | 用户对本次的额外要求 | `:2989` |
| `generationOptions` / `generation_options` | object | 12 项生成配置（见 §8） | `:2992` |
| `retryContentCorrection` | bool | 只跑内容矫正，跳过生成 | `:2960, 3105` |
| `retryFailedSections` | bool | 只重跑失败/未完成小节 | `:2962, 3120` |
| `continuePostProcessing` | bool | 用户确认忽略失败后继续后处理 | `:2963, 3122` |
| `rerunIllustrations` | bool | 只重新配图 | `:2961, 2967` |
| `simulatePartialFailures` | bool | 开发者模式随机失败注入 | `:3126-3140` |

### 1.3 前端类型契约 —— `client/src/features/technical-plan/types.ts:49-62`

```ts
export interface ContentGenerationOptions {
  useAiImages: boolean;
  maxAiImages: number;
  useMermaidImages: boolean;
  maxMermaidImages: number;
  useHtmlImages: boolean;
  maxHtmlImages: number;
  htmlImageTypes: string;
  tableRequirement: ContentTableRequirement;
  enableConsistencyAudit: boolean;
  consistencyRepairMode: ConsistencyRepairMode;
  enableOriginalPlanCoverageAudit: boolean;
  originalPlanCoverageRepairMode: OriginalPlanCoverageRepairMode;
}
```

### 1.4 调用链入口 —— `preload.cjs:276` → `taskService.cjs:1465-1471`

```js
276:  startContentGeneration: (payload) => ipcRenderer.invoke('tasks:start-content-generation', payload),
```

```js
1465:  startContentGeneration(payload) {
1466:    const technicalPlan = technicalPlanStore.loadTechnicalPlan();
1467:    if (!technicalPlan.outlineWordControlSnapshot) {
1468:      throw new Error('当前目录没有字数控制生效快照，请重新生成目录');
1469:    }
1470:    return startManagedTask('content-generation', payload, runContentGenerationTask);
1471:  },
```

前端三处调用点：`ContentEditPage.tsx:717 / 730 / 741 / 753 / 768 / 822 / 891`。

---

## 2. 四步流程的实际函数调用链

主控流在 `contentGenerationTask.cjs:6516-6674`（`try { ... }` 块）。真实链路比「四步」多出 6 个后处理阶段：

```js
6536:    if (!runOnlyIllustrationStage && tasksToRun.length) {
6537:      if (targetItemId) {
6538:        await prepareSingleSectionPlan();                              // 步骤2（单小节分支）
6539:        pauseIfRequested('正文生成已在正文编排后暂停，可导出当前已完成内容，稍后继续。');
6540:        await restoreOriginalMaterialsIfNeeded(tasksToRun);
6541:        pauseIfRequested('正文生成已在原方案还原阶段暂停，可导出当前已完成内容，稍后继续。');
6542:        await runItemsWithWorkerPool(tasksToRun, contentConcurrency, runOne, isPauseRequested);
6543:        pauseIfRequested('正文生成已在正文生成阶段暂停，可导出当前已完成内容，稍后继续。');
6544:      } else {
6545:        await planAll();                                                // 步骤2（批量分支）
6546:        pauseIfRequested('正文生成已在正文编排后暂停，可导出当前已完成内容，稍后继续。');
6547:        await restoreOriginalMaterialsIfNeeded(tasksToRun);
6548:        pauseIfRequested('正文生成已在原方案还原阶段暂停，可导出当前已完成内容，稍后继续。');
6549:        if (tasksToRun.length) {
6550:          await runContentTargetsWithWarmup(tasksToRun);               // 步骤3
6551:          pauseIfRequested('正文生成已在正文生成阶段暂停，可导出当前已完成内容，稍后继续。');
6552:        }
6553:      }
6554:    }
```

### 四步对应关系

| 步 | 函数 | 定义行 | 调用行 |
|---|---|---|---|
| ① 收集叶子节点 | `collectLeafContexts` | `:2080-2091` | `:2984` |
| ② 正文编排 | `planAll` → `planOne` → `buildChapterContentPlanMessages` | `:3952` / `:3896` / `:793` | `:6545`、`:3902` |
| ③ 并发生成正文 | `runContentTargetsWithWarmup` → `runItemsWithWorkerPool` → `runOne` → `buildChapterContentMessages` | `:4342` / `:2639` / `:4187` / `:888` | `:6550`、`:4384`、`:4222` |
| ④ 执行配图 | `runIllustrationPlanning` + `runIllustrationGeneration` | `:6232` / `:6330` | `:6670-6673` |

### ④ 内部再次二分（`:6666-6674`）

```js
6666:    if (!targetItemId) {
6667:      let illustrationPlan = runOnlyIllustrationGeneration ? storedPlan.contentIllustrationPlan : null;
6668:      if (!runOnlyIllustrationGeneration) {
6669:        pauseIfRequested('正文生成已在全文图片编排前暂停，可导出当前已完成内容，稍后继续。');
6670:        illustrationPlan = await runIllustrationPlanning();
6671:      }
6672:      pauseIfRequested('正文生成已在图片生成前暂停，可导出当前已完成内容，稍后继续。');
6673:      await runIllustrationGeneration(illustrationPlan);
6674:    }
```

### 实际完整阶段序列（12 段，`:6569-6609`）

```js
6569:    if (!runOnlyIllustrationStage && !targetItemId && !retryContentCorrection && !completedStages.has('section-word-adjusting')) {
6570:      await runSectionWordAdjustments(leaves, 'section');
6571:      markStageCompleted('section-word-adjusting');
...
6580:      if (!completedStages.has('original-auditing')) {   // 原方案覆盖审计/修复
6589:      if (!completedStages.has('auditing')) {             // 全文一致性审计/修复
6597:      if (!completedStages.has('table-cleaning')) {      // 去表格
6599:        await removeTablesBeforeIllustration();
6604:        : await runSectionWordAdjustments(leaves, 'final-section');
6606:      if (!completedStages.has('total-word-adjusting')) {  // 全文字数调整
6607:        await runTotalWordAdjustments();
```

阶段中文标签与进度区间：`CONTENT_PHASE_LABELS`（`:2695-2708`）、`CONTENT_PROGRESS_PROFILES`（`:2710-2754`）。

---

## 3. 叶子节点收集算法、并发上限、分批策略

### 3.1 叶子收集 —— `contentGenerationTask.cjs:2080-2091`（递归，携带父链与同级）

```js
function normalizeChildren(item) {
  return Array.isArray(item.children) ? item.children : [];
}

function collectLeafContexts(items, parents = []) {
  const results = [];
  for (const item of items || []) {
    const children = normalizeChildren(item);
    if (!children.length) {
      results.push({ item, parentChapters: parents, siblingChapters: items || [] });
      continue;
    }
    results.push(...collectLeafContexts(children, [...parents, item]));
  }
  return results;
}
```

- `parentChapters`：根→父的**全链**（`[...parents, item]`）
- `siblingChapters`：**当前层级全部兄弟**（含自己，渲染时再排除）
- 过滤条件 —— `:2984-2988`：

```js
2984:  let leaves = collectLeafContexts(outlineData.outline)
2985:    .filter(({ item }) => item?.content_mode === 'ai-generate');
2986:  if (!leaves.length) {
2987:    throw new Error('当前目录没有标记为“AI生成”的正文小节');
2988:  }
```

### 3.2 并发上限

常量 —— `:23-26`：

```js
const DEFAULT_CONTEXT_LENGTH_LIMIT = 400000;
const AGENT_CONTEXT_THRESHOLD_RATIO = 0.7;
const DEFAULT_TEXT_CONCURRENCY_LIMIT = 10;
const DEFAULT_IMAGE_CONCURRENCY_LIMIT = 2;
```

归一化 —— `:478-486`：

```js
function normalizeContentConcurrency(value) {
  const concurrency = Number(value);
  return Math.max(1, Number.isFinite(concurrency) ? Math.round(concurrency) : DEFAULT_TEXT_CONCURRENCY_LIMIT);
}

function normalizeImageConcurrency(value) {
  const concurrency = Number(value);
  return Math.max(1, Number.isFinite(concurrency) ? Math.round(concurrency) : DEFAULT_IMAGE_CONCURRENCY_LIMIT);
}
```

**实际来源不是硬编码，而是 AI 配置** —— `:2993-2995`：

```js
2993:  const aiConfig = aiService.getConfig ? aiService.getConfig() : {};
2994:  const contentConcurrency = normalizeContentConcurrency(aiConfig.concurrency_limit);
2995:  const imageConcurrency = normalizeImageConcurrency(aiConfig.image_model?.concurrency_limit);
```

工作池实现 —— `:2602-2656`（`Promise.all` 固定 N 个 worker，**首个错误即整批中止**）：

```js
async function runWorkerPool({ limit, getNextItem, worker, shouldStop, onItemStart, onItemComplete }) {
  const workerCount = Math.max(1, Math.floor(Number(limit) || 1));
  let activeCount = 0;
  let firstError = null;

  async function runWorker() {
    while (true) {
      if (firstError || shouldStop?.()) { return; }
      const item = getNextItem();
      if (!item) { return; }
      activeCount += 1;
      onItemStart?.(item, activeCount);
      try {
        const result = await worker(item);
        activeCount -= 1;
        await onItemComplete?.(item, result, activeCount);
      } catch (error) {
        activeCount -= 1;
        if (!firstError) { firstError = error; }
        return;
      }
    }
  }

  await Promise.all(Array.from({ length: workerCount }, runWorker));
  if (firstError) { throw firstError; }
}
```

### 3.3 分批策略（三种，语义各不相同）

| 场景 | 批量 | 定义 |
|---|---|---|
| 提示词缓存预热 | 每组 1 个，**串行先行**，再并发其余 | `:4347-4385` |
| 全文字数调整 | 每轮 10 个小节 | `TOTAL_WORD_ADJUSTMENT_BATCH_SIZE = 10`（`:31`） |
| 去表格 | 单批 ≤ 30000 字符 | `TABLE_CLEANUP_BATCH_CHAR_LIMIT`（`:45`），`createTableCleanupBatches`（`:354-372`） |
| 图片生成 | mermaid/短 HTML 并发 `contentConcurrency`；超长 HTML（>50000 字符）**串行**；AI 生图并发 `imageConcurrency` | `:6349-6351, 6476-6488` |

预热分组键 —— `:4327-4333`（按 4 类提示词形态各预热一次，提高 KV cache 命中）：

```js
function getContentPromptWarmupKey(context) {
  const originalState = getOriginalMaterialRuntimeState(context.item);
  const contentPlan = getContentPlanForItem(context.item.id);
  const branch = originalState.needsOptimization ? 'restored' : 'normal';
  const tableMode = contentPlan?.table?.needed ? 'table' : 'plain';
  return `${branch}:${tableMode}`;
}
```

图片阶段的三队列分流 —— `:6347-6351`：

```js
6347:    const executions = buildIllustrationExecutionContexts(illustrationPlan, leaves, sections);
6348:    const aiExecutions = executions.filter(({ planItem }) => planItem.kind === 'ai');
6349:    const normalTextExecutions = executions.filter(({ planItem, reference }) => planItem.kind === 'mermaid'
6350:      || (planItem.kind === 'html' && reference.length <= HTML_AGENT_THRESHOLD_CHARS));
6351:    const agentHtmlExecutions = executions.filter(({ planItem, reference }) => planItem.kind === 'html' && reference.length > HTML_AGENT_THRESHOLD_CHARS);
```

（`HTML_AGENT_THRESHOLD_CHARS = 50000`，`contentIllustrationGeneration.cjs:14`）

---

## 4. 分层消息的实际数组结构

**注意：分层只在「正文编排」提示词中完整存在（system / 项目概述 / 上级 / 同级 / 当前任务各一条独立消息）。正文生成提示词的分层不同 —— 没有上级、没有同级。**

### 4.1 正文编排（`buildChapterContentPlanMessages`，`:793-880`）—— 完整分层

```js
804:  const messages = [
805:    {
806:      role: 'system',
807:      content: `你是投标技术方案正文编排助手。请根据章节上下文判断本小节最适合的表达方式。
...`,
818:    },
819:  ];
820:
821:  messages.push({
822:    role: 'user',
823:    content: `参考知识库轻量条目（只包含 id、标题和简介，不包含正文；如无合适条目，knowledge.item_ids 返回空数组）：
824: ${renderKnowledgeItemsForPrompt(knowledgeItems)}`,
825:  });
826:
827:  messages.push({ role: 'user', content: `招标文件关键信息（用于判断正文需要引用哪些事实）：\n${formatBidKeyInfoForPrompt(projectOverview, bidAnalysisFactsText)}` });
828:  if (String(globalFactTitlesText || '').trim()) {
829:    messages.push({ role: 'user', content: `Step04 全局事实变量标题清单（编排时只能选择标题，不要输出具体变量内容）：\n${globalFactTitlesText}` });
830:  }
831:
832:  if (parentChapters?.length) {
833:    messages.push({
834:      role: 'user',
835:      content: ['上级章节信息：', ...parentChapters.map((parent) => `- ${parent.id || 'unknown'} ${parent.title || '未命名章节'}\n  ${parent.description || ''}`)].join('\n'),
836:    });
837:  }
838:
839:  if (siblingChapters?.length) {
840:    const siblingLines = ['同级章节信息：'];
841:    for (const sibling of siblingChapters) {
842:      if (sibling.id !== chapterId) {
843:        siblingLines.push(`- ${sibling.id || 'unknown'} ${sibling.title || '未命名章节'}\n  ${sibling.description || ''}`);
844:      }
845:    }
846:    if (siblingLines.length > 1) {
847:      messages.push({ role: 'user', content: siblingLines.join('\n') });
848:    }
849:  }
850:
851:  if (String(regenerateRequirement || '').trim()) {
852:    messages.push({ role: 'user', content: `用户对本次重新生成的额外要求：\n${regenerateRequirement}` });
853:  }
854:
855:  messages.push({
856:    role: 'user',
857:    content: `请为以下章节返回正文编排 JSON：
...`,
877:  });
878:
879:  return messages;
880: }
```

| 语义层 | 消息下标 | role | 条件 |
|---|---|---|---|
| system | `[0]` | `system` | 恒定 |
| 参考知识库 | `[1]` | `user` | 恒定（可为空 JSON 数组） |
| 项目概述 + 招标关键信息 | `[2]` | `user` | 恒定 |
| 全局事实标题清单 | `[3]` | `user` | `globalFactTitlesText` 非空 |
| **上级章节** | 变长 | `user` | `parentChapters.length` |
| **同级章节** | 变长 | `user` | 排除自身后仍 >1 行 |
| 用户额外要求 | 变长 | `user` | `regenerateRequirement` 非空 |
| **当前章节任务** | 末条 | `user` | 恒定 |

### 4.2 正文生成（`buildChapterContentMessages`，`:888-967`）—— 6 层，**无上级/同级**

```js
893:  const messages = [
894:    { role: 'system', content: `你是一个专业的标书编写专家...` },
916:  ];
917:
918:  if (String(projectOverview || '').trim()) {
919:    messages.push({ role: 'user', content: `项目概述信息：\n${projectOverview}` });
920:  }
921:  if (String(preSectionInstruction || '').trim()) {
922:    messages.push({ role: 'user', content: String(preSectionInstruction || '').trim() });
923:  }
924:  appendSelectedFactsMessage(messages, selectedFactsText);
925:
926:  if (knowledgeContents?.length) {
927:    messages.push({
928:      role: 'user',
929:      content: '参考正文素材使用规则：以下内容只作为可吸收的技术素材。请改写为当前项目语境下的投标技术方案正文，不要照抄，不要提到“知识库”“历史文档”“参考资料”或素材来源。',
930:    });
931:    messages.push({
932:      role: 'user',
933:      content: `参考正文素材：\n${formatKnowledgeContentsForPrompt(knowledgeContents)}`,
934:    });
935:  }
936:
937:  if (String(regenerateRequirement || '').trim()) {
938:    messages.push({ role: 'user', content: `用户对本次重新生成的额外要求：\n${regenerateRequirement}` });
942:  }
943:
944:  if (contentPlan) {
945:    messages.push({ role: 'user', content: `正文编排决策：\n${formatContentPlanForPrompt(contentPlan)}` });
949:  }
950:
951:  messages.push({
952:    role: 'user',
953:    content: `请为以下标书章节生成具体内容：...`,
962:  });
963:  const sectionWordRequirement = buildSectionWordRequirement(wordControl, false, generationTarget);
964:  if (sectionWordRequirement) messages.push({ role: 'user', content: sectionWordRequirement });
965:
966:  return messages;
967: }
```

事实变量注入是**独立消息**（`:125-132`）：

```js
function appendSelectedFactsMessage(messages, selectedFactsText) {
  const content = String(selectedFactsText || '').trim();
  if (!content) return;
  messages.push({
    role: 'user',
    content: `本章节需要使用的全局事实变量（正文涉及时优先使用这些变量值，保证全文一致）：\n${content}`,
  });
}
```

**关键发现（潜在缺陷）**：`buildChapterContentMessages` 的形参表（`:888`）**不含 `parentChapters` / `siblingChapters`**，调用点 `:4222` 也未传。即「避免车轱辘话」所需的同级信息**只在编排阶段可见，正文写作阶段不可见** —— 模型必须完全依赖编排阶段产出的 `writing_focus` 来避免重复。同级信息在正文中另有两处使用：字数调整（`:2405, 2441`）与原方案还原（`:1077-1087`）。

### 4.3 原方案还原的「消息重排」技巧 —— `:991-1005`

```js
  const finalMessage = messages.pop();
  if (finalMessage) { messages.push(finalMessage); }
  messages.push({
    role: 'user',
    content: `已还原正文底稿：\n${String(restoredContent || '').trim()}`,
  });
  messages.push({
    role: 'user',
    content: '请基于已还原正文底稿输出当前章节完整正文。必须保留底稿中的实质内容，可以优化扩写，但不要从零重写；...',
  });
```

即「先给任务、再给底稿、再给重申」三段式，避免模型在看到底稿后忘记任务。

---

## 5. 正文 System / User Prompt 逐字原文

### 5.1 正文 System 消息 —— `contentGenerationTask.cjs:893-916`（逐字完整保留）

```js
      role: 'system',
      content: `你是一个专业的标书编写专家，负责为投标文件的技术标部分生成具体内容。

要求：
1. 内容要专业、准确，与章节标题和描述保持一致。
2. 这是技术方案，不是宣传报告，注意朴实无华，不要假大空。
3. 语言要正式、规范，符合标书写作要求，但不要使用奇怪的连接词，不要让人觉得内容像是 AI 生成的。
4. 内容要详细具体，避免空泛的描述。
5. 围绕当前章节标题、描述和正文编排重点展开，保持内容聚焦。
6. ${tableAllowed ? '可以使用 Markdown 段落、列表和表格；表格必须服务于内容表达，不要为了形式硬插。' : '只能使用 Markdown 段落、普通列表和加粗引导语，严禁输出 Markdown 表格或 HTML 表格。'}
7. ${tableAllowed ? '正文只生成文字、列表、表格等内容，配图由系统另行处理。' : '正文只生成文字和普通列表，配图由系统另行处理。'}
8. 严禁输出 Mermaid、PlantUML、Graphviz、flowchart、graph、sequenceDiagram 等图表代码块、mermaid.ink 链接或图片 Markdown；配图由系统另行处理。
9. ${tableAllowed ? '表格单元格内如有多项内容，优先使用编号、顿号、分号或短句，不要使用 HTML <br> 标签。' : '如需表达多项参数、职责、流程或措施，请改用分段文字或普通列表，不要用表格模拟。'}
10. 严禁使用 Markdown 标题语法（#、##、###、####、#####、######），也不要生成与当前章节同级或下级的伪目录标题。
11. 如需在正文中分层表达，只能使用普通段落、无编号列表、表格或无编号加粗引导语，例如 **实施要点：**。
12. 加粗引导语只允许写简短主题词，禁止使用任何形式的编号。
13. 只有步骤、流程、时间顺序、操作顺序等连续性非常强的内容，才可以使用有序列表；其他分段一律使用自然段、无编号列表或无编号加粗引导语，禁止使用任何形式的编号。
14. 直接返回章节内容，不生成标题，不要任何额外说明。
15. 如果本章节需要使用的全局事实变量中包含相关内容，必须优先使用变量值，不得前后矛盾。
16. 仅使用本章节提供的全局事实变量；未提供时不要主动编造具体人员、周期、质保、品牌、型号等会影响全文一致性的承诺。${buildContentFactCompletenessInstruction(globalFactsMode) ? `\n\n${buildContentFactCompletenessInstruction(globalFactsMode)}` : ''}`,
```

### 5.2 尾部任务消息（User）—— `:951-962`（逐字完整保留）

```js
  messages.push({
    role: 'user',
    content: `请为以下标书章节生成具体内容：

当前章节信息：
章节ID: ${chapterId}
章节标题: ${chapterTitle}
章节描述: ${chapterDescription}

请结合项目概述信息、本章节全局事实变量、参考正文素材和正文编排决策，围绕当前章节标题、描述和写作重点生成详细的专业内容。
直接返回编写的正文内容，不要输出标题、Markdown 标题、带任何形式编号的加粗引导语、伪目录标题、解释、总结等任何其他内容`,
  });
```

### 5.3 编排 System 消息（逐字完整保留）—— `:805-818`

```js
    {
      role: 'system',
      content: `你是投标技术方案正文编排助手。请根据章节上下文判断本小节最适合的表达方式。

要求：
1. 只返回 JSON，不要输出解释、总结或 Markdown。
2. ${tablePlanningAllowed ? '由你自行判断是否适合使用表格，判断要克制、合情合理，不要为了形式而硬插。' : '本次不编排表格，table.needed 必须为 false。'}
3. ${tableLimitInstruction}
4. ${tablePlanningAllowed ? '表格仅在能明显提升表达清晰度时使用，例如归纳职责、步骤、参数、风险、措施、成果等。' : '不要为了满足 JSON 格式而编造表格目的。'}
5. knowledge.item_ids 只能从参考知识库轻量条目的 id 中选择；可以多选，可以为空数组；不要编造 id，不要输出 reason。
6. facts.titles 只能从全局事实变量标题清单中选择；请选择编写本章节正文时会用到的变量组标题，可以多选，可以为空数组；不要编造标题，不要输出具体变量内容。
7. writing_focus 用 1-2 句话概括本节正文重点，只围绕当前章节标题和描述，不展开成正文，不编造具体承诺、参数、周期、品牌或型号。
8. 编排判断必须结合招标文件关键信息和全局事实变量标题，不要规划会造成时间、地点、人员、设备、标准或服务承诺前后不一致的表达。`,
    },
```

### 5.4 字数要求消息（独立一条）—— `:427-437`

```js
function buildSectionWordRequirement(wordControl, preserveOriginalMaterial = false, generationTarget = 0) {
  if (wordControl.sectionWords <= 0) return '';
  // 传入 generationTarget（按全文上限倒推的折后目标）时用它替代预设字数，允许范围展示保持不变，从源头压低初稿总量。
  const targetWords = generationTarget > 0 ? generationTarget : wordControl.sectionWords;
  const base = wordControl.strictSectionWords
    ? `本小节目标字数约 ${targetWords} 字，硬性上限 ${wordControl.sectionMaximumWords} 字，绝对不得超过上限；超出上限属于不合格输出。请在信息完整、专业、不重复的前提下贴近目标字数，宁可略短也不要为凑字数扩写、堆砌或重复表达。`
    : `本小节建议字数 ${wordControl.sectionMinimumWords} 至 ${wordControl.sectionMaximumWords} 字（目标约 ${targetWords} 字）。请在内容完整、专业、不重复的前提下控制篇幅，避免明显超出该范围；如确有必要可略有出入，最终由全文字数流程统一调整。`;
  return preserveOriginalMaterial
    ? `${base}\n字数要求不能覆盖保留原方案实质内容的要求；可以消除重复和冗余，但不得删除技术路线、参数、周期、人员、验收、售后和承诺。`
    : base;
}
```

### 5.5 事实补全模式追加段（拼接进 system 尾部）—— `:81-97`

```js
function buildContentFactCompletenessInstruction(mode) {
  if (mode === 'omit') {
    return `事实补全规则（别招欠模式）：
1. 严禁虚拟、杜撰任何未在本章节全局事实变量和参考材料中明确给出的具体信息。
2. 全局事实变量中已经给出的笼统口径必须沿用，不得自行补成具体工艺、人名、日期、地点、业绩、证书、规格型号或实施细节。
3. 如果有不确定的，尽量使用笼统的方式表达，不涉及不确定的时间、地点、人员、业绩、证书、规格型号等任何事实项内容。
4. 不要为了写得具体而编造人名、日期、地点、业绩、证书编号、规格型号。`;
  }
  if (mode === 'placeholder') {
    return `事实补全规则（放着我来模式）：
1. 严禁虚拟、杜撰任何未在本章节全局事实变量和参考材料中明确给出的具体信息。
2. 任何不确定项必须使用【待填写】作为占位符，不要改写成“待定”或其他说法。
3. 如果全局事实变量中已有【待填写】，正文必须原样沿用，不得改成具体值。
4. 不要杜撰不确定的时间、地点、人员、业绩、证书、规格型号等任何事实项内容。`;
  }
  return '';
}
```

`mode` 归一化（`:77-79`）：`omit | placeholder` 之外一律 `fabricate`（**默认放开编造**）。

---

## 6. 超长文本「续写」机制

**`should_continue_round` 在本仓不存在。** 实际有三套长度/轮次机制：

### 6.1 上下文超限 → 切换 Agent 文件模式

```js
463: function getTextContextLengthLimit(aiService) {
464:   let config = {};
465:   try {
466:     config = aiService?.getConfig?.() || {};
467:   } catch {
468:     config = {};
469:   }
470:   return normalizePositiveInteger(config.contextLengthLimit, DEFAULT_CONTEXT_LENGTH_LIMIT);
471: }
472:
473: function shouldUseAgentForMessages(aiService, messages) {
474:   const contextLengthLimit = getTextContextLengthLimit(aiService);
475:   return getMessagesContentLength(messages) > Math.floor(contextLengthLimit * AGENT_CONTEXT_THRESHOLD_RATIO);
476: }
```

判定阈值 = `contextLengthLimit × 0.7`（默认 400000 × 0.7 = 280000 字符）。**没有截断、没有分段重发、没有续写 —— 是整体切换执行引擎。**

调用点：正文生成还原扩写（`:4225`）、原方案还原（`:4058` 附近）。日志明示：

```js
4228:        logs = [...logs, `已还原正文优化扩写提示词 ${messagesLength} 字符，超过上下文阈值 ${Math.floor(contextLengthLimit * AGENT_CONTEXT_THRESHOLD_RATIO)}，切换 Agent 文件模式：${item.id} ${item.title || '未命名章节'}。`];
```

Agent 模式消息被**改写为文件**（`buildAgentRestoredChapterContentFiles`，`:1194-1229`）：`chapter-context.md` / `restored-content.md` / `knowledge-contents.md`，输出到 `optimized-section.md`。

### 6.2 字数调整轮次（真正的「多轮续做」）

```js
28: const MAX_WORD_ADJUSTMENT_ROUNDS = 3;
29: // 全文扩写不限制有效轮数，仅在连续多轮没有增加字数时退出。
30: const MAX_EXPANSION_NO_PROGRESS_ROUNDS = 3;
```

单小节轮次 —— `:4476-4488`：

```js
4476:    let rounds = Math.min(MAX_WORD_ADJUSTMENT_ROUNDS, Math.max(0, Number(itemRounds[item.id]) || 0));
4477:    while (rounds < MAX_WORD_ADJUSTMENT_ROUNDS) {
...
4480:      rounds += 1;
...
4485:      contentStats.section_adjustment_round = rounds;
4486:      itemRounds[item.id] = rounds - 1;
4487:      setWordAdjustmentRuntime(stage, item.id, rounds - 1, completedItemIds, itemRounds);
4488:      logs = [...logs, `调整小节字数：${item.id} ${item.title || '未命名章节'}，第 ${rounds}/${MAX_WORD_ADJUSTMENT_ROUNDS} 轮，当前 ${currentWords} 字。`];
```

全文缩写轮次上限同为 3（`:4678`：`if (!isExpansion && round > MAX_WORD_ADJUSTMENT_ROUNDS) return;`）；**扩写无固定上限**，仅靠无进展计数退出 —— `:4778-4796`：

```js
4778:      if (isExpansion) {
4779:        if (currentWords > roundStartWords) {
4780:          noProgressRounds = 0;
4781:        } else {
4782:          noProgressRounds += 1;
4783:          logs = [...logs, `全文扩写第 ${round} 轮未增加有效字数，连续无进展 ${noProgressRounds}/${MAX_EXPANSION_NO_PROGRESS_ROUNDS} 轮。`];
4784:        }
4785:      }
...
4792:      if (isExpansion && noProgressRounds >= MAX_EXPANSION_NO_PROGRESS_ROUNDS) {
4793:        logs = [...logs, `全文扩写连续 ${MAX_EXPANSION_NO_PROGRESS_ROUNDS} 轮没有增加有效字数，停止自动扩写。`];
```

### 6.3 轮次状态持久化（断点续跑）

`contentRuntime`（`:2564-2583`）持久化 `word_adjustment_round` / `word_adjustment_item_rounds` / `word_adjustment_no_progress_rounds` / `word_adjustment_round_start_words` / `word_adjustment_completed_item_ids`，续跑时恢复（`:4665-4691`、`:4514-4519`）。

### 6.4 单次 AI 调用的 JSON 校验重试

`aiService.cjs:834-885`：`maxRetries = request.max_retries ?? 2` → 总 3 次，每次失败先走 `repairJsonResponse` 修复，仍失败才重试。本模块显式传 `max_retries: 1`（`:3609, 4428, 4919, 5062, 5214, 5613, 5747, 5903, 6091`），字数调整传 `max_retries: 0`（`:4428`）。

**结论：本仓没有「生成到一半被 max_tokens 截断 → 拼上下文续写」的机制。**

---

## 7. 正文编排（planning）的 JSON 结构、提示词、priority 语义

### 7.1 System 消息逐字 —— `:805-818`

```js
805:    {
806:      role: 'system',
807:      content: `你是投标技术方案正文编排助手。请根据章节上下文判断本小节最适合的表达方式。
808:
809: 要求：
810: 1. 只返回 JSON，不要输出解释、总结或 Markdown。
811: 2. ${tablePlanningAllowed ? '由你自行判断是否适合使用表格，判断要克制、合情合理，不要为了形式而硬插。' : '本次不编排表格，table.needed 必须为 false。'}
812: 3. ${tableLimitInstruction}
813: 4. ${tablePlanningAllowed ? '表格仅在能明显提升表达清晰度时使用，例如归纳职责、步骤、参数、风险、措施、成果等。' : '不要为了满足 JSON 格式而编造表格目的。'}
814: 5. knowledge.item_ids 只能从参考知识库轻量条目的 id 中选择；可以多选，可以为空数组；不要编造 id，不要输出 reason。
815: 6. facts.titles 只能从全局事实变量标题清单中选择；请选择编写本章节正文时会用到的变量组标题，可以多选，可以为空数组；不要编造标题，不要输出具体变量内容。
816: 7. writing_focus 用 1-2 句话概括本节正文重点，只围绕当前章节标题和描述，不展开成正文，不编造具体承诺、参数、周期、品牌或型号。
817: 8. 编排判断必须结合招标文件关键信息和全局事实变量标题，不要规划会造成时间、地点、人员、设备、标准或服务承诺前后不一致的表达。`,
818:    },
```

`tableLimitInstruction` 三分支 —— `:799-803`：

```js
799:  const tableLimitInstruction = tableRequirement === 'heavy'
800:    ? '表格需求为“大量”，保持现有编排逻辑；仍然只有明显适合表格的小节才将 table.needed 设为 true。'
801:    : tableRequirement === 'none'
802:      ? '表格需求为“不要”，table.needed 必须为 false，table.purpose 留空。'
803:      : `表格需求为“${tableRequirementLabel}”，table.needed 表示进入表格候选池，不代表最终一定生成；全文表格上限为 ${maxTables || 0} 个，共 ${tableTotalSections || totalSections || 0} 个叶子小节，系统后续会全局择优。`;
```

### 7.2 JSON 结构 —— `:855-877`（逐字）

```
请为以下章节返回正文编排 JSON：

章节ID: ${chapterId}
章节标题: ${chapterTitle}
章节描述: ${chapterDescription}

JSON 格式：
{
  "writing_focus": "1-2 句话说明本节正文重点展开什么，只聚焦当前章节，不写成正文",
  "knowledge": {
    "item_ids": ["从参考知识库轻量条目中选择的 id；没有合适条目时返回空数组"]
  },
  "facts": {
    "titles": ["从全局事实变量标题清单中选择正文会用到的变量组标题；没有需要引用的变量时返回空数组"]
  },
  "table": {
    "needed": true,
    "purpose": "说明表格在本小节中要表达什么；不需要表格时留空"
  }
}
```

### 7.3 程序侧强制归一 —— `:569-599`

```js
function normalizeContentPlan(value, allowedKnowledgeItemIds, allowedFactTitles) {
  ...
  return {
    writing_focus: singleLine(source.writing_focus || source.writingFocus || writing.focus || writing.writing_focus || writing.writingFocus),
    knowledge: {
      item_ids: normalizeKnowledgeItemIds(rawKnowledgeItemIds, allowedKnowledgeItemIds),
    },
    facts: {
      titles: normalizeFactTitles(rawFactTitles, allowedFactTitles),
    },
    table: {
      needed: tableNeeded,
      purpose: tableNeeded ? singleLine(table.purpose) : '',
    },
    original_material: normalizeOriginalMaterial(source.original_material || source.originalMaterial),
  };
}
```

**白名单过滤**：`knowledge.item_ids` 交集 `allowedKnowledgeItemIds`（`:537-544`）、`facts.titles` 交集 `allowedFactTitles`（`:162-169`）—— 模型编造的 id/标题被静默丢弃。

校验器（缺一即抛）—— `:678-694`：

```js
function validateContentPlan(plan) {
  if (!plan || typeof plan !== 'object') { throw new Error('正文编排决策必须是对象'); }
  if (!plan.knowledge || !Array.isArray(plan.knowledge.item_ids)) { throw new Error('正文编排决策缺少 knowledge.item_ids'); }
  if (!plan.facts || !Array.isArray(plan.facts.titles)) { throw new Error('正文编排决策缺少 facts.titles'); }
  if (typeof plan.writing_focus !== 'string' || !plan.writing_focus.trim()) { throw new Error('正文编排决策缺少 writing_focus'); }
  if (!plan.table || typeof plan.table.needed !== 'boolean') { throw new Error('正文编排决策缺少 table.needed'); }
}
```

### 7.4 存盘结构与版本门 —— `:601-608, 611-642`

```js
function createStoredContentPlan(plan, tableRequirement) {
  const normalizedTableRequirement = tableRequirement ? normalizeTableRequirement(tableRequirement) : '';
  return {
    plan_version: CONTENT_PLAN_VERSION,          // = 4，:47
    plan: normalizeContentPlan(plan),
    ...(normalizedTableRequirement ? { table_requirement: normalizedTableRequirement } : {}),
    updated_at: now(),
  };
}
```

`normalizeStoredContentPlan` 在 `plan_version !== 4`、`!hasFactSelection`、`!plan.writing_focus`、校验失败任一情形下**返回 null（丢弃缓存、重新编排）**（`:611-642`）。

### 7.5 复用门：表格需求变更即作废 —— `:644-651`

```js
function isStoredContentPlanReusableForTableRequirement(storedContentPlan, tableRequirement) {
  const currentRequirement = normalizeTableRequirement(tableRequirement);
  const storedRequirement = storedContentPlan?.table_requirement || '';
  if (storedRequirement) {
    return storedRequirement === currentRequirement;
  }
  return currentRequirement === 'none';
}
```

### 7.6 编排决策回灌正文（`formatContentPlanForPrompt`，`:696-704`）

```js
function formatContentPlanForPrompt(plan) {
  const lines = [
    `写作重点：${plan.writing_focus || '围绕当前章节标题和描述展开'}`,
    `事实变量：${plan.facts?.titles?.length ? plan.facts.titles.join('；') : '无'}`,
    `表格：${plan.table.needed ? `需要，目的：${plan.table.purpose || '提升正文表达清晰度'}` : '不需要，本小节不要输出 Markdown 表格'}`,
    `原方案还原：${plan.original_material?.restored ? `已还原 ${plan.original_material.restored_chars || 0} 字` : '未还原'}`,
  ];
  return lines.join('\n');
}
```

### 7.7 priority 3/4/5 语义 —— **属配图计划，不属正文编排**

`contentIllustrationPlanning.cjs:160`（提示词原文）：

```
10. priority 只能是 1-5 的整数，5 表示最值得配图。
```

校验 —— `contentIllustrationPlanning.cjs:242-244`：

```js
  if (!Number.isInteger(candidate.priority) || candidate.priority < 1 || candidate.priority > 5) {
    throw new Error('图片候选 priority 必须是 1-5 的整数');
  }
```

排序消费 —— `:298-300`（priority 降序 → 目录序 → 输出序）：

```js
    const sorted = candidates
      .filter((candidate) => candidate.kind === kind)
      .sort((a, b) => b.priority - a.priority || a.firstOrder - b.firstOrder || a.outputIndex - b.outputIndex);
```

**3 与 4 的差异语义源码中未找到**（仅作为连续整数参与降序比较）。

---

## 8. 配置项完整定义与默认值

### 8.1 后端归一 —— `contentGenerationTask.cjs:379-389`

```js
function normalizeTableRequirement(value) {
  const text = String(value || '').trim();
  if (['none', 'light', 'moderate', 'heavy'].includes(text)) {
    return text;
  }
  if (text === '不要') return 'none';
  if (text === '少量') return 'light';
  if (text === '适中') return 'moderate';
  if (text === '大量') return 'heavy';
  return 'heavy';
}
```

中文标签 —— `:48-53`：

```js
const TABLE_REQUIREMENT_LABELS = {
  none: '不要',
  light: '少量',
  moderate: '适中',
  heavy: '大量',
};
```

**非法值一律回落 `heavy`（fail-open 到「大量」）** —— 与 `globalFactsMode` 回落 `fabricate`（`:77-79`）同属「宽松默认」取向。

### 8.2 配图配置（后端）—— `contentIllustrationPlanning.cjs:99-121`

```js
  const config = {
    ai: {
      enabled: Boolean(options?.useAiImages) && Boolean(aiImagesAvailable),
      limit: normalizeLimit(options?.maxAiImages, 6, eligibleCount),
      allowed_types: [...AI_IMAGE_TYPES],
      type_descriptions: AI_IMAGE_TYPE_DESCRIPTIONS,
    },
    mermaid: {
      enabled: Boolean(options?.useMermaidImages),
      limit: normalizeLimit(options?.maxMermaidImages, 5, eligibleCount),
      allowed_types: [...MERMAID_IMAGE_TYPES],
      type_descriptions: MERMAID_IMAGE_TYPE_DESCRIPTIONS,
    },
    html: {
      enabled: Boolean(options?.useHtmlImages) && allowedHtmlTypes.length > 0,
      limit: normalizeLimit(options?.maxHtmlImages, 10, eligibleCount),
      allowed_types: allowedHtmlTypes,
    },
    eligible_section_ids: eligibleSectionIds,
  };
  for (const kind of ILLUSTRATION_KINDS) {
    if (config[kind].limit <= 0) config[kind].enabled = false;
  }
```

`limit` 上限硬钳制在可编排小节数内 —— `:36-39`：

```js
function normalizeLimit(value, fallback, sectionCount) {
  const number = Number(value);
  return Math.max(0, Math.min(Number.isFinite(number) ? Math.round(number) : fallback, sectionCount));
}
```

允许的类型（`:7-17`）：
- AI：`engineering_diagram` / `realistic_photo`
- Mermaid：`process` / `hierarchy` / `responsibility`
- HTML：用户自定义，**默认为空即 disabled**（`:113`）

### 8.3 前端默认值 —— `ContentEditPage.tsx:105-120`（逐字）

```tsx
const DEFAULT_HTML_IMAGE_TYPES = '甘特图、进度网络图、组织架构图、泳道图、RACI 职责矩阵、风险矩阵、系统架构与拓扑图、WBS 工作分解结构图、鱼骨图、柱状图、折线图、饼图';

const defaultContentGenerationOptions: ContentGenerationOptions = {
  useAiImages: false,
  maxAiImages: 6,
  useMermaidImages: true,
  maxMermaidImages: 5,
  useHtmlImages: true,
  maxHtmlImages: 10,
  htmlImageTypes: DEFAULT_HTML_IMAGE_TYPES,
  tableRequirement: 'heavy',
  enableConsistencyAudit: true,
  consistencyRepairMode: 'agent',
  enableOriginalPlanCoverageAudit: false,
  originalPlanCoverageRepairMode: 'agent',
};
```

### 8.4 前端归一 —— `ContentEditPage.tsx:134-167`

```tsx
function buildDefaultGenerationOptions(imageModelAvailable: boolean, leafCount: number): ContentGenerationOptions {
  const imageLimit = Math.max(1, leafCount);
  return {
    ...defaultContentGenerationOptions,
    useAiImages: imageModelAvailable,
    maxAiImages: Math.min(defaultContentGenerationOptions.maxAiImages, imageLimit),
    maxMermaidImages: Math.min(defaultContentGenerationOptions.maxMermaidImages, imageLimit),
    maxHtmlImages: Math.min(defaultContentGenerationOptions.maxHtmlImages, imageLimit),
  };
}

function normalizeGenerationOptions(options: ContentGenerationOptions | undefined, imageModelAvailable: boolean, leafCount: number, isExpansionWorkflow = false): ContentGenerationOptions {
  const fallback = buildDefaultGenerationOptions(imageModelAvailable, leafCount);
  const maxAiImagesLimit = Math.max(1, leafCount);
  const requestedMaxAiImages = Number(options?.maxAiImages ?? fallback.maxAiImages);
  const requestedMaxMermaidImages = Number(options?.maxMermaidImages ?? fallback.maxMermaidImages);
  const requestedMaxHtmlImages = Number(options?.maxHtmlImages ?? fallback.maxHtmlImages);
  const tableRequirement = options?.tableRequirement;

  return {
    useAiImages: Boolean(options?.useAiImages ?? fallback.useAiImages) && imageModelAvailable,
    maxAiImages: Math.max(0, Math.min(Number.isFinite(requestedMaxAiImages) ? Math.round(requestedMaxAiImages) : fallback.maxAiImages, maxAiImagesLimit)),
    useMermaidImages: Boolean(options?.useMermaidImages ?? fallback.useMermaidImages),
    maxMermaidImages: Math.max(0, Math.min(Number.isFinite(requestedMaxMermaidImages) ? Math.round(requestedMaxMermaidImages) : fallback.maxMermaidImages, maxAiImagesLimit)),
    useHtmlImages: Boolean(options?.useHtmlImages ?? fallback.useHtmlImages),
    maxHtmlImages: Math.max(0, Math.min(Number.isFinite(requestedMaxHtmlImages) ? Math.round(requestedMaxHtmlImages) : fallback.maxHtmlImages, maxAiImagesLimit)),
    htmlImageTypes: String(options?.htmlImageTypes ?? fallback.htmlImageTypes),
    tableRequirement: isContentTableRequirement(tableRequirement) ? tableRequirement : fallback.tableRequirement,
    enableConsistencyAudit: Boolean(options?.enableConsistencyAudit ?? fallback.enableConsistencyAudit),
    consistencyRepairMode: isConsistencyRepairMode(options?.consistencyRepairMode) ? options.consistencyRepairMode : fallback.consistencyRepairMode,
    enableOriginalPlanCoverageAudit: isExpansionWorkflow ? Boolean(options?.enableOriginalPlanCoverageAudit ?? fallback.enableOriginalPlanCoverageAudit) : false,
    originalPlanCoverageRepairMode: isExpansionWorkflow && isOriginalPlanCoverageRepairMode(options?.originalPlanCoverageRepairMode) ? options.originalPlanCoverageRepairMode : fallback.originalPlanCoverageRepairMode,
  };
}
```

### 8.5 前后端默认值对照

| 项 | 前端默认 | 后端 fallback | 是否一致 |
|---|---|---|---|
| `useAiImages` | `false`，但 `buildDefaultGenerationOptions` 改为 `imageModelAvailable`（`:138`） | `Boolean(options?.useAiImages)` 且 `&& aiImagesAvailable` | ✅ |
| `maxAiImages` | 6，钳制到 `min(6, leafCount)` | fallback 6，再钳制到 `eligibleCount` | ✅ |
| `useMermaidImages` | `true` | 透传 | ✅ |
| `maxMermaidImages` | 5 | fallback 5 | ✅ |
| `useHtmlImages` / `maxHtmlImages` | `true` / 10 | 透传 / fallback 10 | ✅ |
| `tableRequirement` | `heavy` | 非法值回落 `heavy` | ✅ |
| `enableConsistencyAudit` | `true` | `?? true`（`:3000`） | ✅ |
| `consistencyRepairMode` | `agent` | `normalizeConsistencyRepairMode` 非法回落 `agent`（`:391-393`） | ✅ |
| `enableOriginalPlanCoverageAudit` | `false` | `?? false` **且** `isExpansionWorkflow` 门控（`:3003`） | ✅ |
| `originalPlanCoverageRepairMode` | `agent` | 非法回落 `agent`（`:395-397`） | ✅ |

---

## 9. 表格控制策略（少量 ≤20% / 适中 ≤40% / 大量宽松）

核心函数 —— `contentGenerationTask.cjs:520-525`：

```js
function maxTablesForRequirement(requirement, leafCount) {
  if (requirement === 'none') return 0;
  if (requirement === 'light') return Math.floor(Math.max(0, leafCount) * 0.2);
  if (requirement === 'moderate') return Math.floor(Math.max(0, leafCount) * 0.4);
  return null;                      // heavy = 不限
}
```

**`null` 是「无上限」哨兵，不是数字** —— 全文三处判 `=== null`：

编排阶段择优 —— `:3989-3999`：

```js
3989:    const tableCandidates = tasksToRun.filter(({ item }) => contentPlans.get(item.id)?.table.needed);
3990:    const selectedTableIds = runLimits.maxTablesForRun === null
3991:      ? new Set(tableCandidates.map(({ item }) => item.id))
3992:      : pickDistributedTableTargets(tableCandidates, runLimits.maxTablesForRun);
3993:    if (runLimits.maxTablesForRun !== null) {
3994:      for (const { item } of tableCandidates) {
3995:        if (!selectedTableIds.has(item.id)) {
3996:          contentPlans.set(item.id, clearContentPlanTable(contentPlans.get(item.id)));
3997:        }
3998:      }
3999:    }
```

额度扣减（已成功章节的表格占额）—— `:3169-3178`：

```js
  function refreshRunLimits(targets = tasksToRun) {
    const taskItemIds = new Set(targets.map(({ item }) => item.id));
    maxTables = maxTablesForRequirement(tableRequirement, leaves.length);
    const retainedTableCount = maxTables === null ? 0 : countRetainedTablePlans(storedContentPlans, taskItemIds);
    runLimits = {
      maxTablesForRun: maxTables === null ? null : Math.max(0, maxTables - retainedTableCount),
      retainedTableCount,
    };
    return runLimits;
  }
```

**分布均匀择优**（不取前 N 个，避免表格全堆在前几章）—— `:2516-2535`：

```js
function pickDistributedTableTargets(plannedItems, limit) {
  if (limit <= 0 || !plannedItems.length) { return new Set(); }
  if (plannedItems.length <= limit) { return new Set(plannedItems.map(({ item }) => item.id)); }

  const selected = new Map();
  for (let slot = 0; slot < limit; slot += 1) {
    const start = Math.floor((slot * plannedItems.length) / limit);
    const end = Math.floor(((slot + 1) * plannedItems.length) / limit);
    const group = plannedItems.slice(start, Math.max(start + 1, end));
    const candidate = group[Math.floor(group.length / 2)] || group[0];
    selected.set(candidate.item.id, candidate);
  }
  return new Set(selected.keys());
}
```

用户可见日志 —— `:3197-3201`：

```js
3197:  logs = [...logs, tableRequirement === 'heavy'
3198:    ? '表格需求：大量，保持现有表格编排逻辑。'
3199:    : tableRequirement === 'none'
3200:      ? '表格需求：不要，本次正文编排不会安排表格。'
3201:      : `表格需求：${TABLE_REQUIREMENT_LABELS[tableRequirement]}，全文最多 ${maxTables} 个表格，本轮最多新增 ${runLimits.maxTablesForRun} 个。`];
```

`none` 的**双保险**（编排强制清 + 事后 AI 去表格）—— `:3929-3931`、`:6174-6177`：

```js
3929:    if (tableRequirement === 'none') {
3930:      contentPlan = clearContentPlanTable(contentPlan);
3931:    }
```
```js
6174:  async function removeTablesBeforeIllustration(options = {}) {
6175:    if (tableRequirement !== 'none') {
6176:      return { ran: false, rewrittenCount: 0, skippedCount: 0 };
6177:    }
```

`clearContentPlanTable` —— `:527-535`：

```js
function clearContentPlanTable(contentPlan) {
  return {
    ...contentPlan,
    table: {
      needed: false,
      purpose: '',
    },
  };
}
```

去表格的 AI 指令（把表格转文字）—— `:724-741`（逐字）：

```
你是投标技术方案正文编辑助手。请把指定小节中的表格转换为普通文字描述。

要求：
1. 只返回 JSON，不要输出解释、总结或 Markdown 代码围栏。
2. 必须逐个处理输入中的 table_id；允许按表格内容改写为普通段落或普通列表。
3. 不改变原文意思，不删除数字、参数、工期、标准、职责、流程、承诺、验收要求、频次和数量。
4. replacement_text 只写用于替换该表格块的正文片段，不返回完整小节正文。
5. replacement_text 严禁包含 Markdown 表格、HTML <table>、代码块、章节标题或伪目录标题。
6. 如表格本身为空或无法理解，也要用一句普通文字概括其表达意图，不要返回空字符串。
```

表格提取会**跳过代码围栏内内容**（`:334-348` 的 `collectFencedCodeRanges`），批次**逆序应用**（`:6070`，避免前面的替换使后面偏移失效）：

```js
6070:    const batches = createTableCleanupBatches(originalTables).reverse();
```

分批切分 —— `:354-372`：

```js
function createTableCleanupBatches(tables) {
  const batches = [];
  let current = [];
  let currentSize = 0;
  for (const table of tables || []) {
    const size = String(table.text || '').length + String(table.before || '').length + String(table.after || '').length;
    if (current.length && currentSize + size > TABLE_CLEANUP_BATCH_CHAR_LIMIT) {
      batches.push(current);
      current = [];
      currentSize = 0;
    }
    current.push(table);
    currentSize += size;
  }
  if (current.length) {
    batches.push(current);
  }
  return batches;
}
```

---

## 10. 「严禁输出 Mermaid」在提示词与后处理中的实现

### 10.1 提示词层：正文生成（`:906`）

```
8. 严禁输出 Mermaid、PlantUML、Graphviz、flowchart、graph、sequenceDiagram 等图表代码块、mermaid.ink 链接或图片 Markdown；配图由系统另行处理。
```

### 10.2 提示词层：Agent 优化扩写（`:1185`）

```
7. 严禁输出 Mermaid、PlantUML、Graphviz、flowchart、graph、sequenceDiagram 等图表代码块、mermaid.ink 链接或图片 Markdown。
```

### 10.3 相关的其它禁令（同一治理思路）

| 行号 | 场景 | 原文 |
|---|---|---|
| `:1350` | 正文扩写 patch | `7. content 不得包含章节标题、Markdown 标题、图片 Markdown、Mermaid、代码块或解释文字。` |
| `:2057` | 原方案覆盖修复 | `10. 不要新增图片 Markdown、Mermaid、代码块或伪目录标题，也不要选择图片 Markdown、Mermaid 或代码块作为 replace 的 target_text。` |
| `:2380-2382` | 字数调整校验（**代码级硬拦**） | ``/```|~~~|\bmermaid\b/i.test(operation.content)`` → `throw new Error('字数调整 content 不能包含标题、图片、Mermaid、代码块或表格');` |
| `:2481` | 插入位置校验 | `throw new Error('字数调整不能在图片、Mermaid、代码块或表格内部插入内容');` |
| `:2498` | 替换范围校验 | `throw new Error('字数调整不能修改图片、Mermaid、代码块或表格');` |
| `:2438` | 字数调整指令 | `8. 不修改图片、Mermaid、代码块、表格结构、列表编号层级和资源路径，不生成 Markdown 标题或伪目录标题。` |

### 10.4 后处理层：**对 Mermaid 代码块本身没有任何剥离逻辑**

全仓检索结果：
- `contentGenerationTask.cjs` 落库前唯一的清洗是 `normalizeLeafContentForSave`（`:2348-2352`），只做**三件事**：`stripRepeatedChapterTitle`（删重复标题行）+ `stripMarkdownHeadingsFromLeafContent`（`#` 标题降级为 `**加粗**`）+ `normalizeGeneratedMarkdown`（`<br>` 归一）。**不检测 mermaid**。
- 唯一涉及 mermaid 代码块剥离的是**配图生成侧**的 `normalizeMermaidCode`（`contentIllustrationGeneration.cjs:28-30`），它剥离的是**系统自己产出的**图块围栏，不是正文里的。
- `stripGeneratedIllustrations`（`contentIllustrationGeneration.cjs:446-448`）只删 `<!-- yibiao-illustration:start -->…end -->` 系统标记块。

**结论：「严禁输出 Mermaid」是纯提示词软约束 + 字数调整链路的代码级硬拦（`:2380/2481/2498`），正文落库路径上没有兜底剥离。** 若模型违规输出 mermaid 围栏，会原样进入 `sections.content`；`exportService.cjs:167` 存在 mermaid 围栏计数（说明导出侧会识别，但生成侧不清理）。

落库清洗链原文 —— `:2348-2352`：

```js
function normalizeLeafContentForSave(content, chapter) {
  return stripMarkdownHeadingsFromLeafContent(
    stripRepeatedChapterTitle(normalizeGeneratedMarkdown(content), chapter),
  );
}
```

Mermaid 反向的**唯一来源**是配图系统自己插入 —— `contentIllustrationGeneration.cjs:450-462`：

```js
function buildGeneratedIllustrationMarkdown(planItem) {
  const generation = planItem.generation || {};
  const caption = singleLine(planItem.title);
  if (!caption) throw new Error(`图片计划缺少 title：${planItem.item_id || 'unknown'}`);
  let body = '';
  if (planItem.kind === 'mermaid' && generation.code) {
    body = `\`\`\`mermaid\n${normalizeMermaidCode(generation.code)}\n\`\`\`\n\n*图：${caption}*`;
  } else if (generation.asset_url) {
    body = `![${caption}](${generation.asset_url})\n\n*图：${caption}*`;
  }
  if (!body) return '';
  return `<!-- yibiao-illustration:start id="${planItem.item_id}" -->\n${body}\n<!-- yibiao-illustration:end -->`;
}
```

---

## 11. 同级章节信息注入的具体实现（如何避免车轱辘话）

### 11.1 注入点全表

| # | 位置 | 行号 | 形式 | 是否有去车轱辘指令 |
|---|---|---|---|---|
| 1 | 正文编排 system | `:817` | 第 8 条要求「不要规划会造成时间、地点、人员、设备、标准或服务承诺前后不一致的表达」 | ✅ 一致性导向 |
| 2 | 正文编排 user | `:832-837` | 上级章节：id + 标题 + **描述**（不含正文） | ❌ |
| 3 | 正文编排 user | `:839-849` | 同级章节：id + 标题 + **描述**，排除自身 | ❌ |
| 4 | **正文生成** | — | **不注入** | — |
| 5 | 字数调整 | `:2403-2405, 2441` | 章节路径 + 描述 + 同级「id 标题」拼接串 | ✅（见下） |
| 6 | 原方案还原 | `:1077-1087` | 同级「id 标题」拼接串 | ❌ |

### 11.2 核心实现（`:839-849`）—— 只给**标题 + 描述**，绝不给正文

```js
  if (siblingChapters?.length) {
    const siblingLines = ['同级章节信息：'];
    for (const sibling of siblingChapters) {
      if (sibling.id !== chapterId) {
        siblingLines.push(`- ${sibling.id || 'unknown'} ${sibling.title || '未命名章节'}\n  ${sibling.description || ''}`);
      }
    }
    if (siblingLines.length > 1) {
      messages.push({ role: 'user', content: siblingLines.join('\n') });
    }
  }
```

**这就是「避免车轱辘话」的主要手段 —— 用「同级标题+描述」划定边界，而不是让模型去比对同级正文。** 描述来自目录阶段的人工/AI 确认，比正文更稳定、更省 token。同级自身被 `sibling.id !== chapterId` 排除；`siblingLines.length > 1` 保证排除后非空才发消息。

### 11.3 上级章节给的是**全链**而非直接父（`:832-837`）

```js
      content: ['上级章节信息：', ...parentChapters.map((parent) => `- ${parent.id || 'unknown'} ${parent.title || '未命名章节'}\n  ${parent.description || ''}`)].join('\n'),
```

配合 `collectLeafContexts` 的 `[...parents, item]`（`:2088`），模型能看到完整路径。

### 11.4 字数调整里的显式「不越界」指令 —— `:2437`

```
9. 不把其他目录应承载的内容移动到当前小节。
```

同级串拼接 —— `:2403-2405`：

```js
  const { item, parentChapters, siblingChapters } = context;
  const chapterPath = [...(parentChapters || []), item].map((chapter) => `${chapter.id} ${chapter.title}`).join(' > ');
  const siblings = (siblingChapters || []).filter((chapter) => chapter.id !== item.id).map((chapter) => `${chapter.id} ${chapter.title}`).join('；') || '无';
```

### 11.5 系统层第二道防线：全文一致性审计

去车轱辘的**兜底**是 `runConsistencyAuditIfEnabled`（`:6589-6595`），其提示词明确**排除**重复类问题（`:1669`）：

```
4. 不报告文风、质量、重复、篇幅、表达优化等问题。
```

**即：重复问题被显式排除在一致性审计范围之外** —— 重复控制完全依赖提示词层（编排阶段的同级边界 + `writing_focus`），无程序兜底。

### 11.6 落库层的重复清理

- `stripRepeatedChapterTitle`（`:2291-2321`）：只删**首行等于本章标题**的情况（含去章节号、去中文序号、`**` 包裹等归一）
- `formatContentPlanForPrompt`（`:696-704`）：编排决策回灌时含「写作重点」，起二次聚焦作用

`stripRepeatedChapterTitle` 原文 —— `:2291-2321`：

```js
function stripRepeatedChapterTitle(content, chapter) {
  const title = String(chapter?.title || '').trim();
  if (!title) {
    return content;
  }

  const rawLines = String(content || '').replace(/^\uFEFF/, '').split(/\r?\n/);
  let firstContentLine = rawLines.findIndex((line) => line.trim());
  if (firstContentLine < 0) {
    return content;
  }

  const chapterId = String(chapter?.id || '').trim();
  const firstLine = unwrapMarkdownTitle(rawLines[firstContentLine]);
  let comparable = firstLine;

  if (chapterId) {
    comparable = comparable.replace(new RegExp(`^${escapeRegExp(chapterId)}\\s+`), '').trim();
  }
  comparable = comparable.replace(/^[一二三四五六七八九十]+[、.．]\s*/, '').trim();

  if (comparable !== title && firstLine !== `${chapterId} ${title}`.trim()) {
    return content;
  }

  const nextLines = rawLines.slice(firstContentLine + 1);
  while (nextLines.length && !nextLines[0].trim()) {
    nextLines.shift();
  }
  return [...rawLines.slice(0, firstContentLine), ...nextLines].join('\n').trimStart();
}
```

---

## 12. 容错：重试、失败、用户可见提示

### 12.1 单小节生成失败的降级链 —— `:4299-4324`（逐字）

```js
    } catch (error) {
      if (isPauseLikeError(error)) {
        saveSection(item, {
          status: previousStatus,
          content: previousContent,
          error: previousSection.error,
        }, previousContent, { logs });
        throw error;
      }
      const message = error.message || '正文生成失败';
      const fallbackContent = isSingleSectionRegeneration
        ? previousContent
        : countContentWords(content) > 0
          ? content
          : previousContent;
      const hasReadableFallback = !isSingleSectionRegeneration && countContentWords(fallbackContent) > 0;
      logs = [...logs, hasReadableFallback
        ? `生成请求未产生可用新内容：${item.id} ${item.title || '未命名章节'}，${message}。已保留当前有效正文。`
        : `生成失败：${item.id} ${item.title || '未命名章节'}，${message}${isSingleSectionRegeneration ? '。已保留原正文。' : ''}`];
      markGenerationCompleted(item.id);
      saveSection(item, {
        status: hasReadableFallback ? 'success' : 'error',
        content: fallbackContent,
        error: hasReadableFallback ? undefined : message,
      }, fallbackContent, { logs });
    }
```

**关键：`runOne` 内部不抛出普通错误 —— 单章失败被完全吞掉、状态置 `error`、继续跑其它章。** 只有 `isPauseLikeError` 才上抛。这是「38 章各自成败独立」的实现基础。

### 12.2 编排失败降级为「纯正文」 —— `:3921-3927`

```js
    } catch (error) {
      if (isPauseLikeError(error)) {
        throw error;
      }
      contentPlan = normalizeContentPlan({}, allowedKnowledgeItemIds, allowedFactTitles);
      logs = [...logs, `编排失败：${item.id} ${item.title || '未命名章节'}，${error.message || '模型返回无效'}，将按纯正文生成。`];
    }
```

### 12.3 部分失败的**闸门**：后续流程不自动继续 —— `:6556-6561`

```js
6556:    if (!runOnlyIllustrationStage && !targetItemId && !retryContentCorrection && !continuePostProcessing) {
6557:      const unresolvedContexts = leaves.filter(({ item }) => isUnresolvedContentSection(sections[item.id]));
6558:      if (unresolvedContexts.length) {
6559:        persistContentDecisionWait(unresolvedContexts);
6560:        return;
6561:      }
```

判定 —— `:2891-2894`：

```js
// 后续流程开始前，正文小节只能是已成功或用户明确忽略。
function isUnresolvedContentSection(section) {
  return section?.status !== 'success' && section?.status !== 'ignored';
}
```

用户可见提示（**唯一一处把失败小节 id 全部列出**）—— `:3538-3561`：

```js
  // 所有正文请求结束后存在失败时，保存等待用户重试或忽略的稳定状态。
  function persistContentDecisionWait(unresolvedContexts) {
    const unresolvedIds = unresolvedContexts.map(({ item }) => item.id);
    const message = `正文小节生成结束，${unresolvedIds.length} 个小节失败或未完成。请重试失败小节，或确认忽略后继续后续流程。`;
    logs = [...logs, message, `失败或未完成小节：${unresolvedIds.join('、')}。`];
    contentStats.phase = 'generating';
    contentStats.awaiting_content_decision = true;
    contentStats.ignored_section_count = leaves.filter(({ item }) => sections[item.id]?.status === 'ignored').length;
    const runtime = syncRuntime({ phase: 'generating', awaiting_content_decision: true });
    const taskPatch = {
      status: 'error',
      error: message,
      progress: progressFor(leaves, sections),
      logs,
      stats: statsSnapshot(),
      pause_requested: false,
    };
    checkpointTask(taskPatch, {
      outlineData,
      contentGenerationSections: sections,
      contentGenerationPlans: storedContentPlans,
      contentGenerationRuntime: runtime,
    });
  }
```

### 12.4 用户两条出路（前端）—— `ContentEditPage.tsx:738-760`

```tsx
  // 只重新生成当前失败的正文小节，全部成功后由 Main 自动进入后续流程。
  const retryFailedSections = async () => {
    if (!awaitingContentDecision || !unresolvedCount || taskBlocksGeneration) return;
    try {
      await window.yibiao?.tasks.startContentGeneration({ retryFailedSections: true });
      trackConfigUsage({ content_generation_action: 'retry_failed_sections' });
      showToast('失败小节重试任务已在后台启动', 'success');
    } catch (error) {
      showToast(error instanceof Error ? error.message : '启动失败小节重试失败', 'error');
    }
  };

  // 用户确认后忽略剩余失败或未完成小节，直接执行检查、字数调整和配图。
  const continuePostProcessing = async () => {
    if (!awaitingContentDecision || taskBlocksGeneration) return;
    try {
      await window.yibiao?.tasks.startContentGeneration({ continuePostProcessing: true });
      trackConfigUsage({ content_generation_action: 'continue_with_ignored_sections' });
      setContinuePostProcessingDialogOpen(false);
      showToast('后续处理任务已在后台启动', 'success');
    } catch (error) {
      showToast(error instanceof Error ? error.message : '启动后续处理失败', 'error');
    }
  };
```

「忽略」落库 —— `:6517-6533`：

```js
6517:    if (continuePostProcessing) {
6518:      const ignoredContexts = leaves.filter(({ item }) => isUnresolvedContentSection(sections[item.id]));
6519:      for (const { item } of ignoredContexts) {
6520:        const content = String(sections[item.id]?.content || item.content || '');
6521:        saveSection(item, { status: 'ignored', content, error: undefined }, content, { logs });
6525:      }
6527:      contentStats.ignored_section_count = ignoredContexts.length;
6530:      logs = [...logs, `已按用户确认忽略 ${ignoredContexts.length} 个失败或未完成小节，开始执行后续流程。`];
```

### 12.5 配图失败：不影响正文 —— `:6445-6462`

```js
      } catch (error) {
        if (isPauseLikeError(error) || isPauseRequested()) throw error;
        const partial = error?.illustrationGeneration || {};
        persistIllustrationGeneration(planItem.item_id, {
          status: 'error',
          ...partial,
          error: compactError(error?.message || error),
        }, '正在继续生成其他图片');
        writeDeveloperLog(`illustration.${planItem.kind}.failed`, {
          item_id: planItem.item_id,
          section_ids: planItem.section_ids,
          image_type: planItem.image_type,
          title: planItem.title,
          error: compactError(error?.message || error),
        });
        const kindLabel = planItem.kind === 'ai' ? 'AI' : planItem.kind === 'mermaid' ? 'Mermaid' : 'HTML';
        logs = [...logs, `${kindLabel} 配图失败：${planItem.section_ids[0]}，${error.message || '生成失败'}，已保留正文。`];
      }
```

正文插入只取 `status === 'success'` 的项（`contentIllustrationGeneration.cjs:498`）。

### 12.6 Mermaid 渲染修复重试 —— `contentIllustrationGeneration.cjs:15, 205-237`

```js
15: const MERMAID_REPAIR_ATTEMPTS = 3;
```
```js
205: async function prepareRenderableMermaid({ aiService, execution, mermaidPlan, isPauseLikeError }) {
...
209:   try {
210:     assertSupportedMermaidDiagramType(execution.planItem.image_type);
211:     await validateMermaidRender(currentPlan.code);
212:     return { code: currentPlan.code, attempts: 0 };
213:   } catch (error) { lastError = error; }
214:
217:   for (let attempt = 1; attempt <= MERMAID_REPAIR_ATTEMPTS; attempt += 1) {
218:     try {
219:       const repaired = await aiService.collectJsonResponse({
...
226:         max_retries: 1,
227:       });
228:       currentPlan = { ...currentPlan, code: repaired.code };
229:       await validateMermaidRender(currentPlan.code);
230:       return { code: currentPlan.code, attempts: attempt };
231:     } catch (error) {
232:       if (isPauseLikeError?.(error)) throw error;
233:       lastError = error;
234:     }
235:   }
236:   throw new Error(compactError(lastError?.message || lastError || 'Mermaid 渲染失败'));
237: }
```

**先本地渲染验证（`renderMermaidToPng`），失败才问 AI 修** —— 最多 3 轮，实测有效轮数回传日志（`:6406-6408`）。

### 12.7 Agent 部分输出抢救 —— `:3363-3406`

```js
    } catch (error) {
      if (isPauseRequested() || isPauseLikeError(error)) { throw error; }
      const diagnostics = agentErrorDiagnostics(error);
      writeDeveloperLog(`${eventPrefix}.agent.error`, diagnostics);
      if (error?.agentValidationFailed) { throw error; }
      const recoveredOutput = String(error?.agentPartialOutput || '').trim();
      if (!recoveredOutput) { throw error; }
      const seededOutputContent = findSeededOutputContent();
      if (seededOutputContent !== null
        && normalizeNewlines(recoveredOutput).trim() === normalizeNewlines(seededOutputContent).trim()) {
        writeDeveloperLog(`${eventPrefix}.output.recovered_rejected`, { ...diagnostics, reason: 'same_as_seeded_output', ... });
        throw error;
      }
      writeDeveloperLog(`${eventPrefix}.output.recovered`, { ... });
      return { success: true, recovered: true, ..., output_content: recoveredOutput, ... };
    }
```

**若抢救内容与种子输入逐字相同（= 模型没干活，直接回抄）则拒绝** —— 防「假成功」。

### 12.8 中断恢复 —— `:2662-2681`（`createInitialSections`）

```js
2668:  for (const { item } of leaves) {
2669:    const existing = next[item.id];
2670:    const interrupted = existing?.status === 'running';
2671:    const content = interrupted ? '' : existing?.content || item.content || '';
2672:    const existingStatus = interrupted ? 'error' : existing?.status;
2673:    next[item.id] = {
...
2678:      error: interrupted ? INTERRUPTED_SECTION_ERROR : existing?.error,
```

`INTERRUPTED_SECTION_ERROR = '上次生成被中断，请继续生成。'`（`:27`）。进程崩溃留下的 `running` 残留**在下次启动时被改判为 error**，从而进入重跑队列。

### 12.9 字数控制失败的用户提示 —— `:38-39`

```js
const CONTENT_WORD_CONTROL_WARNING = '经多轮修复，字数仍未达预期，请您人工核对';
const SECTION_WORD_CONTROL_WARNING = '字数未达预期，请您人工核对';
```

写入 —— `:6697-6707`（进 `contentStats.word_control_warning` + 追加到 `logs`）：

```js
6697:    contentStats.word_control_warning = finalSectionViolations.length || finalTotalDirection
6698:      ? (targetItemId ? SECTION_WORD_CONTROL_WARNING : CONTENT_WORD_CONTROL_WARNING)
6699:      : undefined;
6700:    const failedCount = statusLeaves.filter(({ item }) => sections[item.id]?.status === 'error').length;
...
6707:    if (contentStats.word_control_warning) logs = [...logs, contentStats.word_control_warning];
```

### 12.10 顶层错误三分支 —— `:6721-6745`

```js
  } catch (error) {
    if (isAiQueueScopePausedError(error)) {
      persistPausedContentGeneration('正文生成已暂停，未发起的 AI 请求已从队列丢弃，可导出当前已完成内容，稍后继续。');
      ...
      return;
    }
    if (isContentGenerationPausedError(error)) {
      ...
      return;
    }
    writeDeveloperLog('content.task.error', { error: ..., stack: ..., stats: ... });
    throw error;
  }
```

**注意：非暂停异常会 `throw`，由 `startManagedTask` 统一转 task `error` 状态** —— 与「单章失败被吞」形成两层容错边界。

---

## 附：核心常量速查（`contentGenerationTask.cjs:23-53`）

| 常量 | 值 | 行 |
|---|---|---|
| `DEFAULT_CONTEXT_LENGTH_LIMIT` | 400000 | 23 |
| `AGENT_CONTEXT_THRESHOLD_RATIO` | 0.7 | 24 |
| `DEFAULT_TEXT_CONCURRENCY_LIMIT` | 10 | 25 |
| `DEFAULT_IMAGE_CONCURRENCY_LIMIT` | 2 | 26 |
| `MAX_WORD_ADJUSTMENT_ROUNDS` | 3 | 28 |
| `MAX_EXPANSION_NO_PROGRESS_ROUNDS` | 3 | 30 |
| `TOTAL_WORD_ADJUSTMENT_BATCH_SIZE` | 10 | 31 |
| `DEFAULT_SECTION_WORD_GUIDANCE` | 3000 | 32 |
| `TOTAL_WORD_SHRINK_SECTION_RATIO` | 0.25 | 33 |
| `GENERATION_WORD_TARGET_RATIO` | 0.8 | 35 |
| `TOTAL_WORD_SHRINK_MIN_CAPACITY_RATIO` | 0.3 | 37 |
| `CONSISTENCY_AUDIT_GROUP_WORD_LIMIT` | 300000 | 40 |
| `CONSISTENCY_REPAIR_MAX_ATTEMPTS` | 2 | 41 |
| `ORIGINAL_PLAN_SEGMENT_MAX_CHARS` | 6000 | 42 |
| `ORIGINAL_COVERAGE_REPAIR_MAX_ATTEMPTS` | 2 | 43 |
| `TABLE_CLEANUP_CONTEXT_CHARS` | 600 | 44 |
| `TABLE_CLEANUP_BATCH_CHAR_LIMIT` | 30000 | 45 |
| `CONTENT_PLAN_VERSION` | 4 | 47 |
| `HTML_AGENT_THRESHOLD_CHARS` | 50000 | `contentIllustrationGeneration.cjs:14` |
| `MERMAID_REPAIR_ATTEMPTS` | 3 | `contentIllustrationGeneration.cjs:15` |
| `HTML_LAYOUT_REPAIR_ATTEMPTS` | 2 | `contentIllustrationGeneration.cjs:16` |
| `ILLUSTRATION_PLAN_VERSION` | 3 | `contentIllustrationPlanning.cjs:3` |

---

## 考古结论摘要（5 条最值得注意的）

1. **无「续写」机制**。超长文本靠「转 Agent 文件模式」（0.7×上下文阈值）整体换引擎，而非截断续写；多轮只发生在**字数调整**（缩≤3 轮 / 扩无上限但 3 轮无进展即退）。
2. **正文生成阶段看不到同级章节**。`buildChapterContentMessages` 签名不含 `parentChapters`/`siblingChapters`（`:888`），去车轱辘完全依赖编排阶段产出的 `writing_focus`；且一致性审计显式排除重复问题（`:1669`），**无程序兜底**。
3. **「严禁输出 Mermaid」是软约束**。落库清洗链（`normalizeLeafContentForSave`）不检测 mermaid 围栏，仅字数调整链有代码级硬拦（`:2380/2481/2498`）。
4. **表格上限用 `null` 表达「不限」**（`:520-525`），全文三处判 `=== null`；分布择优取每段中位（`:2516-2535`）避免表格堆在前几章；`none` 走「编排强制清 + 事后 AI 转文字」双保险。
5. **失败模型是「章节级隔离 + 全局闸门」**：`runOne` 吞掉普通异常只置 `error`（`:4299-4324`），跑完后由 `persistContentDecisionWait` 拦下并列出全部失败 id（`:3539-3542`），用户二选一（`retryFailedSections` / `continuePostProcessing`）后才继续后处理。

---

## 13. 与「标书/投标/招标」业务语义耦合处（原文引用）

### 13.1 提示词中的角色设定（系统级耦合）

| 行号 | 原文 |
|---|---|
| `:807` | `你是投标技术方案正文编排助手。请根据章节上下文判断本小节最适合的表达方式。` |
| `:896` | `你是一个专业的标书编写专家，负责为投标文件的技术标部分生成具体内容。` |
| `:724` | `你是投标技术方案正文编辑助手。请把指定小节中的表格转换为普通文字描述。` |
| `:1663` | `你是投标技术方案全文一致性审计助手。请审计本组正文是否与给定事实冲突。` |
| `:1769` | `你是投标技术方案正文一致性修复助手。请只针对当前小节返回局部精确替换 patch。` |
| `:1902` | `你是投标技术方案原方案覆盖审计助手。请检查当前小节正文是否保留了原方案来源段中的实质内容。` |
| `:2045` | `你是投标技术方案正文原方案覆盖修复助手。请只针对当前小节返回一次局部补写 patch，用于补回原方案中缺失的实质内容。` |
| `:2429` | `你是投标技术方案正文局部编辑助手。请对当前小节执行${mode === 'expand' ? '扩写' : '缩写'}，只返回 JSON，不返回完整重写正文。` |
| `:1095` | `你是投标技术方案原文归属判断助手。用户提供的原方案是本次要扩写的核心草稿。请判断每个原方案段落应该还原到当前目录的哪个叶子小节。` |
| `:1171` | `你是投标技术方案正文优化扩写 Agent。当前章节已经从用户原方案中还原出正文底稿，该底稿是用户已经写好的真实技术方案内容，必须作为本章节的基础保留。` |
| `contentIllustrationGeneration.cjs:121` | `你是投标技术方案 Mermaid 图生成助手。请根据最终正文生成一张${typeLabel}。` |
| `contentIllustrationGeneration.cjs:179` | `你是 Mermaid 图代码修复助手。请根据渲染错误和最终正文修复现有 Mermaid 代码。` |

### 13.2 「技术标」定位与文体约束 —— `:899-901`

```
1. 内容要专业、准确，与章节标题和描述保持一致。
2. 这是技术方案，不是宣传报告，注意朴实无华，不要假大空。
3. 语言要正式、规范，符合标书写作要求，但不要使用奇怪的连接词，不要让人觉得内容像是 AI 生成的。
```

第 2 条「**不是宣传报告**」是**投标文体专用约束**（区别于宣传/产品文档场景）。

### 13.3 「招标」语义：招标文件信息作为事实来源 —— `:827, 817, 1113`

```js
827:  messages.push({ role: 'user', content: `招标文件关键信息（用于判断正文需要引用哪些事实）：\n${formatBidKeyInfoForPrompt(projectOverview, bidAnalysisFactsText)}` });
```
```
8. 编排判断必须结合招标文件关键信息和全局事实变量标题，不要规划会造成时间、地点、人员、设备、标准或服务承诺前后不一致的表达。
```

### 13.4 「标书章节」措辞 —— `:953`

```js
953:    content: `请为以下标书章节生成具体内容：
```

### 13.5 投标「承诺」类一致性红线（贯穿多处）

`:914` 第 15/16 条：

```
15. 如果本章节需要使用的全局事实变量中包含相关内容，必须优先使用变量值，不得前后矛盾。
16. 仅使用本章节提供的全局事实变量；未提供时不要主动编造具体人员、周期、质保、品牌、型号等会影响全文一致性的承诺。
```

`:432`（字数）：

```
...请在信息完整、专业、不重复的前提下贴近目标字数，宁可略短也不要为凑字数扩写、堆砌或重复表达。
```

`:435`（还原时）：

```
字数要求不能覆盖保留原方案实质内容的要求；可以消除重复和冗余，但不得删除技术路线、参数、周期、人员、验收、售后和承诺。
```

`:2429-2436`（字数调整）：

```
6. 不改变核心意思，不修改参数、数量、日期、周期和标准，不删除技术路线、职责、流程、风险措施、人员安排、验收要求、售后和服务承诺。
7. 不新增未提供的品牌、型号、人员、承诺和服务期限。
```

### 13.6 「标书素材来源隐匿」耦合 —— `:929`

```js
929:      content: '参考正文素材使用规则：以下内容只作为可吸收的技术素材。请改写为当前项目语境下的投标技术方案正文，不要照抄，不要提到"知识库""历史文档""参考资料"或素材来源。',
```

同类约束另见 `:987`（`6. 不要提到"原方案""历史文档""用户原文"或"底稿"。`）、`:1184`、`:1186`。

### 13.7 「投标图题」规范 —— `contentIllustrationPlanning.cjs:144, 153`

```
请基于当前工作目录中的三个输入文件完成投标文件技术方案的全文图片编排，即按要求设计投标文件应该在哪个位置，添加什么样的图片：
```
```
3. 为每项生成 title，title 是最终写入正文的完整图注文本，建议控制在4-15个字，禁止冗长。
```

程序侧双保险（提示词 15 字 vs 校验 20 字）—— `:236-241`：

```js
  if (candidate.title.length > 20) {
    throw new Error(`图片候选 title 不能超过 20 个字：${candidate.title}`);
  }
  if (/^图\s*[:：]/u.test(candidate.title)) {
    throw new Error(`图片候选 title 不应包含"图："前缀：${candidate.title}`);
  }
```

### 13.8 AI 生图的「投标风格」约束 —— `contentIllustrationGeneration.cjs:82`

```
不要有太多文字，专业、克制，适合投标技术方案。
```

HTML 图商务风格 —— `:93`：

```
不要有太多文字描述，专业商务风格。
```

### 13.9 前端文案耦合 —— `ContentEditPage.tsx:851-853`

```tsx
    showToast(simulatePartialFailures
      ? '随机失败模式正文生成任务已在后台启动'
      : regenerate ? '正文重新生成任务已在后台启动' : '正文生成任务已在后台启动', 'success');
```

埋点字段把「标书配置」作为分析维度 —— `analytics.ts:50-52`：

```ts
    ['table_requirement', 'tableRequirements'],
    ['use_mermaid_images', 'useMermaidImages'],
    ['use_ai_images', 'useAiImages'],
```

### 13.10 工作流业务身份 —— `types.ts:4`

```ts
export type TechnicalPlanWorkflowKind = 'technical-plan' | 'existing-plan-expansion';
```

`existing-plan-expansion` 触发「原方案还原 + 覆盖审计」全链路（`:2934-2952, 4007`），是投标「已有方案改标」场景的代码化。

---

## 标书语义耦合点清单

> **用途**：本节逐条列出易标「正文生成模块」中与「标书 / 投标 / 标书编写专家 / 技术方案优势」耦合的原文位置，给出原文摘录，并给出**若改写为专项施工方案语义**（专项施工方案编写专家 / 危大工程针对性 / 施工工艺严谨性 / 符合编制规范）应改为什么。
>
> **改写目标语义**：
> - **身份**：标书编写专家（投标竞争性文件，胜出导向） → 专项施工方案编写专家（内控/报审导向，合规与可执行性优先）
> - **价值锚点**：技术方案优势（优于对手、突出亮点） → 危大工程针对性（危险性分部分项、专项治理）+ 施工工艺严谨性（工序/参数/检验）+ 符合编制规范（GB 50202、危大工程管理规定、建办质〔2018〕31 号等）
>
> ⚠️ **共性原则**：以下改写只动**业务语义与约束条款**，不动**任何结构控制条款**（第 4~14 条的消息分层、禁止 Markdown 标题、表格控制、字数控制、Mermaid 禁令等），否则会连带破坏本报告 §2/§4/§9/§10 所述的实现契约。

### A 组：角色身份（system 消息首句）—— 优先级最高

| # | 文件:行 | 原文摘录 | 若改为专项施工方案语义，应改为 |
|---|---|---|---|
| A-1 | `contentGenerationTask.cjs:896` | `你是一个专业的标书编写专家，负责为投标文件的技术标部分生成具体内容。` | `你是一个专业的专项施工方案编写专家，负责为危大工程及分部分项工程编制施工方案的具体内容。` |
| A-2 | `contentGenerationTask.cjs:807` | `你是投标技术方案正文编排助手。请根据章节上下文判断本小节最适合的表达方式。` | `你是专项施工方案正文编排助手。请根据章节上下文、危大工程属性和施工工艺要求判断本小节最适合的表达方式。` |
| A-3 | `contentGenerationTask.cjs:724` | `你是投标技术方案正文编辑助手。请把指定小节中的表格转换为普通文字描述。` | `你是专项施工方案正文编辑助手。请把指定小节中的表格转换为普通文字描述。` |
| A-4 | `contentGenerationTask.cjs:1663` | `你是投标技术方案全文一致性审计助手。请审计本组正文是否与给定事实冲突。` | `你是专项施工方案全文一致性审计助手。请审计本组正文是否与给定事实冲突。` |
| A-5 | `contentGenerationTask.cjs:1769` | `你是投标技术方案正文一致性修复助手。请只针对当前小节返回局部精确替换 patch。` | `你是专项施工方案正文一致性修复助手。请只针对当前小节返回局部精确替换 patch。` |
| A-6 | `contentGenerationTask.cjs:1902` | `你是投标技术方案原方案覆盖审计助手。请检查当前小节正文是否保留了原方案来源段中的实质内容。` | `你是专项施工方案原方案覆盖审计助手。请检查当前小节正文是否保留了原方案来源段中的实质内容。` |
| A-7 | `contentGenerationTask.cjs:2045` | `你是投标技术方案正文原方案覆盖修复助手。请只针对当前小节返回一次局部补写 patch，用于补回原方案中缺失的实质内容。` | `你是专项施工方案正文原方案覆盖修复助手。请只针对当前小节返回一次局部补写 patch，用于补回原方案中缺失的实质内容。` |
| A-8 | `contentGenerationTask.cjs:2429` | `你是投标技术方案正文局部编辑助手。请对当前小节执行${mode === 'expand' ? '扩写' : '缩写'}，只返回 JSON，不返回完整重写正文。` | `你是专项施工方案正文局部编辑助手。请对当前小节执行${mode === 'expand' ? '扩写' : '缩写'}，只返回 JSON，不返回完整重写正文。` |
| A-9 | `contentGenerationTask.cjs:1095` | `你是投标技术方案原文归属判断助手。用户提供的原方案是本次要扩写的核心草稿。请判断每个原方案段落应该还原到当前目录的哪个叶子小节。` | `你是专项施工方案原文归属判断助手。用户提供的原方案是本次要扩写的核心底稿。请判断每个原方案段落应该还原到当前目录的哪个叶子小节。` |
| A-10 | `contentGenerationTask.cjs:1122` | `你是投标技术方案原文归属判断 Agent。用户提供的原方案是本次已有方案扩写的核心草稿，请基于 workspace 输入文件判断每个原方案段落应该还原到当前目录的哪个叶子小节。` | `你是专项施工方案原文归属判断 Agent。用户提供的原方案是本次已有方案扩写的核心底稿，请基于 workspace 输入文件判断每个原方案段落应该还原到当前目录的哪个叶子小节。` |
| A-11 | `contentGenerationTask.cjs:1171` | `你是投标技术方案正文优化扩写 Agent。当前章节已经从用户原方案中还原出正文底稿，该底稿是用户已经写好的真实技术方案内容，必须作为本章节的基础保留。` | `你是专项施工方案正文优化扩写 Agent。当前章节已经从用户原方案中还原出正文底稿，该底稿是用户已经写好的真实施工方案内容，必须作为本章节的基础保留。` |
| A-12 | `contentIllustrationGeneration.cjs:121` | `你是投标技术方案 Mermaid 图生成助手。请根据最终正文生成一张${typeLabel}。` | `你是专项施工方案 Mermaid 图生成助手。请根据最终正文生成一张${typeLabel}。` |

### B 组：文体与价值导向（system 正文条款）

| # | 文件:行 | 原文摘录 | 若改为专项施工方案语义，应改为 |
|---|---|---|---|
| B-1 | `contentGenerationTask.cjs:896` | （同 A-1 身份句，其后紧跟「这是技术方案，不是宣传报告」） | **保留反宣传条款但改锚点**：`这是施工方案，不是宣传材料和汇报材料，聚焦工序、参数、检验与责任分工，不写口号、不写愿景。` |
| B-2 | `contentGenerationTask.cjs:900` | `2. 这是技术方案，不是宣传报告，注意朴实无华，不要假大空。` | `2. 这是专项施工方案，不是宣传报告或汇报材料，文字要朴实可核查，不写假大空的内容。` |
| B-3 | `contentGenerationTask.cjs:901` | `3. 语言要正式、规范，符合标书写作要求，但不要使用奇怪的连接词，不要让人觉得内容像是 AI 生成的。` | `3. 语言要正式、规范，符合专项施工方案的编制规范要求（章节齐备、依据明确、可执行、可检验），但不要使用奇怪的连接词，不要让人觉得内容像是 AI 生成的。` |
| B-4 | `contentGenerationTask.cjs:899` | `1. 内容要专业、准确，与章节标题和描述保持一致。` | **建议新增针对性约束**（原文无危大工程针对性要求）：`1. 内容要专业、准确，与章节标题和描述保持一致；属于危大工程或超过一定规模的分部分项工程时，必须结合危险性等级给出针对性的技术措施、监测与验收要求。` |
| B-5 | `contentGenerationTask.cjs:902` | `4. 内容要详细具体，避免空泛的描述。` | **建议改为可核查性要求**：`4. 内容要详细具体、可核查，涉及工序的必须写明操作步骤与工艺参数，涉及部位的必须写明位置/标高/规格，涉及风险的必须写明控制指标与监测频次，避免空泛的描述。` |
| B-6 | `contentGenerationTask.cjs:903` | `5. 围绕当前章节标题、描述和正文编排重点展开，保持内容聚焦。` | **保持**（该条为通用结构约束，无标书语义）。 |
| B-7 | `contentGenerationTask.cjs:953` | `请为以下标书章节生成具体内容：` | `请为以下专项施工方案章节生成具体内容：` |
| B-8 | `contentGenerationTask.cjs:960` | `请结合项目概述信息、本章节全局事实变量、参考正文素材和正文编排决策，围绕当前章节标题、描述和写作重点生成详细的专业内容。` | `请结合工程概况与项目概述信息、本章节全局事实变量、参考正文素材和正文编排决策，围绕当前章节标题、描述和写作重点生成详细、严谨、可执行的专业内容。` |
| B-9 | `contentGenerationTask.cjs:929` | `参考正文素材使用规则：以下内容只作为可吸收的技术素材。请改写为当前项目语境下的投标技术方案正文，不要照抄，不要提到"知识库""历史文档""参考资料"或素材来源。` | `参考正文素材使用规则：以下内容只作为可吸收的技术素材。请改写为当前项目语境下的专项施工方案正文，不要照抄，不要提到"知识库""历史文档""参考资料"或素材来源。` |
| B-10 | `contentGenerationTask.cjs:1164` | `内容改写为当前项目语境下的投标技术方案正文`（同 B-9 语义） | `内容改写为当前项目语境下的专项施工方案正文` |

### C 组：招标/投标业务事实来源

| # | 文件:行 | 原文摘录 | 若改为专项施工方案语义，应改为 |
|---|---|---|---|
| C-1 | `contentGenerationTask.cjs:827` | `招标文件关键信息（用于判断正文需要引用哪些事实）：` | `工程概况与编制依据关键信息（用于判断正文需要引用哪些事实）：` |
| C-2 | `contentGenerationTask.cjs:817` | `8. 编排判断必须结合招标文件关键信息和全局事实变量标题，不要规划会造成时间、地点、人员、设备、标准或服务承诺前后不一致的表达。` | `8. 编排判断必须结合工程概况关键信息和全局事实变量标题，不要规划会造成工期、部位、材料规格、构配件型号、施工机具、验收标准或质量安全责任前后不一致的表达。` |
| C-3 | `contentGenerationTask.cjs:1113` / `:1151` | `招标文件关键信息：` | `工程概况与编制依据关键信息：` |
| C-4 | `contentGenerationTask.cjs:829` | `Step04 全局事实变量标题清单（编排时只能选择标题，不要输出具体变量内容）：` | **保持**（全局事实是本仓自有能力，与标书语义正交）。 |
| C-5 | `contentGenerationTask.cjs:2934` | `const isExpansionWorkflow = storedPlan.workflowKind === 'existing-plan-expansion';` | **保持**（工作流枚举，值域可继续保留，仅在展示层改中文名，如「已有专项方案改写」）。 |

### D 组：承诺/质保/售后类一致性红线

| # | 文件:行 | 原文摘录 | 若改为专项施工方案语义，应改为 |
|---|---|---|---|
| D-1 | `contentGenerationTask.cjs:914` | `16. 仅使用本章节提供的全局事实变量；未提供时不要主动编造具体人员、周期、质保、品牌、型号等会影响全文一致性的承诺。` | `16. 仅使用本章节提供的全局事实变量；未提供时不要主动编造具体人员、机械设备型号规格、监测频率、验收频次、检测试验数值等会影响全文一致性的承诺。` |
| D-2 | `contentGenerationTask.cjs:913` | `15. 如果本章节需要使用的全局事实变量中包含相关内容，必须优先使用变量值，不得前后矛盾。` | **保持**（通用一致性条款）。 |
| D-3 | `contentGenerationTask.cjs:435` | `字数要求不能覆盖保留原方案实质内容的要求；可以消除重复和冗余，但不得删除技术路线、参数、周期、人员、验收、售后和承诺。` | `字数要求不能覆盖保留原方案实质内容的要求；可以消除重复和冗余，但不得删除技术路线、工艺参数、工期节点、关键岗位人员、检验批与验收要求、应急处置措施。` |
| D-4 | `contentGenerationTask.cjs:2435` | `6. 不改变核心意思，不修改参数、数量、日期、周期和标准，不删除技术路线、职责、流程、风险措施、人员安排、验收要求、售后和服务承诺。` | `6. 不改变核心意思，不修改参数、数量、工期、标准和规范条文号，不删除技术路线、工种职责、施工流程、风险管控措施、关键岗位人员、验收要求、监测与应急处置要求。` |
| D-5 | `contentGenerationTask.cjs:2436` | `7. 不新增未提供的品牌、型号、人员、承诺和服务期限。` | `7. 不新增未提供的品牌、构配件与机具型号、关键岗位人员、检测试验数值、监测频率和承诺工期。` |
| D-6 | `contentGenerationTask.cjs:905` | `7. ${tableAllowed ? '正文只生成文字、列表、表格等内容，配图由系统另行处理。' : '正文只生成文字和普通列表，配图由系统另行处理。'}` | **保持**（结构控制条款，配图由系统处理，与业务语义正交）。 |
| D-7 | `contentGenerationTask.cjs:904` | `6. ${tableAllowed ? '可以使用 Markdown 段落、列表和表格；表格必须服务于内容表达，不要为了形式硬插。' : '只能使用 Markdown 段落、普通列表和加粗引导语，严禁输出 Markdown 表格或 HTML 表格。'}` | **保持**（结构控制条款，勿动）。 |

### E 组：配图/图题语义

| # | 文件:行 | 原文摘录 | 若改为专项施工方案语义，应改为 |
|---|---|---|---|
| E-1 | `contentIllustrationPlanning.cjs:144` | `请基于当前工作目录中的三个输入文件完成投标文件技术方案的全文图片编排，即按要求设计投标文件应该在哪个位置，添加什么样的图片：` | `请基于当前工作目录中的三个输入文件完成专项施工方案的全文图片编排，即按要求设计方案应该在哪个位置，添加什么样的图片：` |
| E-2 | `contentIllustrationPlanning.cjs:151` | `1. 图片有三类：AI生成图片、mermaid图片、html生成类图网页，具体应用哪种，可以查看illustration-config.json的配置，自行判断。` | **保持**（三类型机制与业务语义正交）。 |
| E-3 | `contentIllustrationPlanning.cjs:153` | `3. 为每项生成 title，title 是最终写入正文的完整图注文本，建议控制在4-15个字，禁止冗长。` | **保持**（图注格式规范，属编制规范的一部分）。 |
| E-4 | `contentIllustrationPlanning.cjs:156` | `AI 图片适合设备、现场、工程空间、实体部署等具象内容；Mermaid 只用于简单流程、层级和职责关系；HTML 用于配置允许的复杂图表类型。` | **建议替换 `设备/部署` 语义为施工语义**：`AI 图片适合施工现场、构配件、材料、塔吊与脚手架、临建布置、基坑支护等具象内容；Mermaid 只用于简单工序流程、组织层级和职责关系；HTML 用于配置允许的复杂图表类型（如进度网络图、危大工程分部分项一览图）。` |
| E-5 | `contentIllustrationPlanning.cjs:10-11` | `engineering_diagram: '专业工程图示：用于展示设备、系统组件、部署位置、连接关系或工程实施场景，强调结构与关系；不用于步骤流转、组织层级或职责分工。'` / `realistic_photo: '专业实景图片：用于表现设备、机房、监控中心、施工、巡检或维护现场等可真实拍摄的对象和环境；不用于抽象系统架构、流程或组织关系。'` | `engineering_diagram: '专业工程图示：用于展示施工机具、构配件节点、支护结构、搭设剖面、连接关系或施工部署场景，强调结构与受力关系；不用于工序流转、组织层级或职责分工。'` / `realistic_photo: '专业实景图片：用于表现施工现场、材料堆场、加工棚、塔吊与脚手架、基坑与支护、监测点布置、巡检或维护现场等可真实拍摄的对象和环境；不用于抽象组织架构、工序流程或组织关系。'` |
| E-6 | `contentIllustrationGeneration.cjs:82` | `不要有太多文字，专业、克制，适合投标技术方案。` | `不要有太多文字，专业、克制，适合专项施工方案。` |
| E-7 | `contentIllustrationGeneration.cjs:93` | `不要有太多文字描述，专业商务风格。` | `不要有太多文字描述，专业规范风格，标注可读，适合打印装订成册。` |
| E-8 | `contentIllustrationGeneration.cjs:121` | （同 A-12） | `你是专项施工方案 Mermaid 图生成助手。请根据最终正文生成一张${typeLabel}。` |
| E-9 | `contentIllustrationPlanning.cjs:161` | `11. 同一小节只允许编排一张图片，包含在html多节图组中，也算该小节已编排，三种图片优先级html>AI生成图片>mermaid，如果一个小节同时适配多种图片，按以上优先级执行。` | **保持**（去重与优先级策略与业务语义正交）。 |

### F 组：前端文案 / 埋点 / 枚举

| # | 文件:行 | 原文摘录 | 若改为专项施工方案语义，应改为 |
|---|---|---|---|
| F-1 | `ContentEditPage.tsx:851-853` | `随机失败模式正文生成任务已在后台启动` / `正文重新生成任务已在后台启动` / `正文生成任务已在后台启动` | `随机失败模式方案生成任务已在后台启动` / `方案重新生成任务已在后台启动` / `方案生成任务已在后台启动` |
| F-2 | `ContentEditPage.tsx:743` | `'失败小节重试任务已在后台启动'` | `'失败章节重试任务已在后台启动'` |
| F-3 | `ContentEditPage.tsx:756` | `'后续处理任务已在后台启动'` | **保持**（无标书语义）。 |
| F-4 | `analytics.ts:50-52` | `['table_requirement', 'tableRequirements']` / `['use_mermaid_images', 'useMermaidImages']` / `['use_ai_images', 'useAiImages']` | **保持**（纯字段映射，与业务语义正交；改字段名会断历史数据）。 |
| F-5 | `types.ts:4` | `export type TechnicalPlanWorkflowKind = 'technical-plan' \| 'existing-plan-expansion';` | **保持值域**（改值域会破坏存量数据反序列化），仅在展示层改中文标签：`technical-plan → 专项方案新建`、`existing-plan-expansion → 已有专项方案改写`。 |
| F-6 | `contentGenerationTask.cjs:2937-2950` | `if (isExpansionWorkflow) { ... throw new Error('请先上传原方案，再生成正文'); ... throw new Error('原方案正文为空，无法执行已有方案扩写'); }` | **保持**（原方案是施工方案底稿，语义自然成立）。 |
| F-7 | `contentGenerationTask.cjs:3209` | `已有方案扩写模式：已读取原方案并拆分为 ${originalPlanSegments.length} 个原文段。` | `已有专项方案改写模式：已读取原方案并拆分为 ${originalPlanSegments.length} 个原文段。` |

### G 组：显式**无**标书语义、不应改动的条款（防误改清单）

以下条款虽出现在标书场景的提示词中，但**与标书语义正交**，改写时会破坏本报告所述的实现契约，必须原样保留：

| 文件:行 | 条款 | 为何不能动 |
|---|---|---|
| `contentGenerationTask.cjs:906` | `8. 严禁输出 Mermaid、PlantUML、Graphviz…` | 配图由系统统一生成（§10），动了会导致正文出现裸图表代码块 |
| `contentGenerationTask.cjs:908-911` | 第 10~13 条 Markdown 标题禁令、加粗引导语规则、有序列表规则 | 目录编号由程序统一渲染，正文带标题会与目录冲突（`:2303` 注释明示） |
| `contentGenerationTask.cjs:797-803` | `tableLimitInstruction` 三分支 | 表格上限与 `maxTables` 的 `null` 哨兵契约（§9） |
| `contentGenerationTask.cjs:125-132` | `appendSelectedFactsMessage` 的消息文案 | 全局事实注入是跨章一致性的唯一事实源，措辞改动会影响事实复用率 |
| `contentGenerationTask.cjs:427-437` | `buildSectionWordRequirement` | 字数控制契约（`sectionWords`/`strictSectionWords`） |
| `contentGenerationTask.cjs:888` | `buildChapterContentMessages` 的形参表 | **若要改，应同时考虑给正文生成阶段补 `siblingChapters`** —— 见 §4.2「关键发现」，这才是修车轱辘的正确位置 |

### H 组：改造工作量与优先级建议

| 优先级 | 改造项 | 涉及条目 | 风险 |
|---|---|---|---|
| **P0** | system 首句身份改写（A-1）+ 反宣传条款改锚点（B-2）+ 任务消息标书措辞（B-7） | 3 处 | 极低（纯文案，不改结构） |
| **P0** | 补充「危大工程针对性」与「可核查性」条款（B-4、B-5） | 2 处新增 | 低（新增条款需实测 token 与风格影响） |
| **P1** | 招标事实来源改工程概况（C-1、C-2、C-3） | 3 处 | 中（`formatBidKeyInfoForPrompt` 的数据源本身来自招标分析模块，改文案不改数据源可先落地） |
| **P1** | 承诺红线改施工参数/监测验收口径（D-1、D-3、D-4、D-5） | 4 处 | 中（涉及字数调整与还原链路，需回归 §12 容错） |
| **P2** | 配图类型描述改施工语义（E-4、E-5、E-6、E-7） | 4 处 | 低（`type_descriptions` 本就是配置项，位于 `illustration-config.json`） |
| **P2** | 前端文案与埋点（F-1、F-2、F-7） | 3 处 | 极低 |
| **P3** | 借机修复「正文生成阶段无同级章节」缺陷（G 组备注） | — | 中（需同步 `buildChapterContentMessages` 形参与调用点，并加 token 预算评估） |


















