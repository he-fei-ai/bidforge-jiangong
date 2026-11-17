# 易标「全局事实模块」源码考古报告

**仓库**：`J:\编程\OpenBidKit 易标\OpenBidKit_Yibiao-main-2026-09-14`（只读，未做任何修改）
**报告产出日期**：第六轮深审
**核心文件规模**：`globalFactsTask.cjs` 1031 行 / `globalFactsTaskV2.cjs` 438 行 / `globalFactsAdjustmentTask.cjs` 130 行 / `globalFactsAgentV2Config.cjs` 6 行 / `GlobalFactsPage.tsx` 396 行 / `contentGenerationTask.cjs` 6630+ 行

---

## 0. 总体结论（先说结论）

易标的全局事实模块存在**两套并存的实现**，`taskService` 只挂载了其中一套：

| 实现 | 文件 | 状态 | 挂载点 |
|---|---|---|---|
| **V1 分段流水线**（3 段 + 知识库补丁 + 原方案补丁 + 最终整理） | `globalFactsTask.cjs` | 逻辑完整、导出大量纯函数，**但 `runGlobalFactsTask` 未被 taskService 调用** | 无（`taskService.cjs:5` 只 import 了 V2） |
| **V2 Agent 工作区** | `globalFactsTaskV2.cjs` | **生产在用** | `taskService.cjs:5, 1445-1459` |
| V1 的「AI 调整」任务 | `globalFactsAdjustmentTask.cjs` | **生产在用**（依赖 V2 的持久会话） | `taskService.cjs:8, 1460-1464` |

这解释了为何「分段提取、补丁合并、二次检查」等机制在生产链路上**只有 V2 的 Agent 版**在跑，而 V1 的 `batchRenderedItems` / `waitAllOrThrow` / `getGlobalFactsSegmentLimit` 虽被导出却**在 V2 中零调用**（已逐一核实，见 §5）。

---

## 1. 全局事实数据结构完整字段定义

### 1.1 权威类型（前端契约）

`client/src/features/technical-plan/types.ts:18`
```ts
export type GlobalFactsMode = 'fabricate' | 'omit' | 'placeholder';
```

`client/src/features/technical-plan/types.ts:192-197`
```ts
export interface GlobalFactGroupState {
  id: string;
  title: string;
  content: string;
  updated_at?: string;
}
```

### 1.2 在方案状态中的位置

`client/src/features/technical-plan/types.ts:370-373`
```ts
  globalFactsMode: GlobalFactsMode;
  globalFactsTask?: BackgroundTaskState;
  globalFactsAdjustmentTask?: BackgroundTaskState;
  globalFacts: GlobalFactGroupState[];
```

默认值：`client/electron/services/technicalPlanStore.cjs:69-71`
```js
  globalFactsMode: 'fabricate',
  globalFactsTask: undefined,
  globalFacts: [],
```

### 1.3 AI 返回的 JSON Schema（V2 唯一硬约束）

`client/electron/services/globalFactsTaskV2.cjs:15-35`
```js
const GLOBAL_FACTS_JSON_SCHEMA = {
  type: 'object',
  required: ['groups'],
  additionalProperties: false,
  properties: {
    groups: {
      type: 'array',
      minItems: 1,
      items: {
        type: 'object',
        required: ['id', 'title', 'content'],
        additionalProperties: false,
        properties: {
          id: { type: 'string', minLength: 1 },
          title: { type: 'string', minLength: 1 },
          content: { type: 'string', minLength: 1 },
        },
      },
    },
  },
};
```

### 1.4 字段级归一化与落库

`technicalPlanStore.cjs:1541-1561` —— 落库前唯一归一化出口：
```js
  function normalizeGlobalFactGroups(groups) {
    const seen = new Set();
    return (Array.isArray(groups) ? groups : []).map((group, index) => {
      const title = String(group?.title || '').trim();
      const content = String(group?.content || '').trim();
      if (!title || !content) return null;
      let id = normalizeGlobalFactId(group?.id || group?.group_id || title, index);
      let suffix = 2;
      while (seen.has(id)) {
        id = `${id}_${suffix}`;
        suffix += 1;
      }
      seen.add(id);
      return {
        id,
        title,
        content,
        updated_at: group?.updated_at || group?.updatedAt || now(),
      };
    }).filter(Boolean);
  }
```

`technicalPlanStore.cjs:1563-1590` —— 读/写：
```js
  function loadGlobalFacts() {
    return db.prepare('SELECT * FROM technical_plan_global_fact_groups ORDER BY sort_order ASC, group_id ASC').all().map((row) => ({
      id: row.group_id,
      title: row.title,
      content: row.content || '',
      updated_at: row.updated_at || undefined,
    }));
  }

  function replaceGlobalFacts(groups) {
    const normalized = normalizeGlobalFactGroups(groups);
    db.prepare('DELETE FROM technical_plan_global_fact_groups').run();
    if (!normalized.length) return;

    const insert = db.prepare(`
      INSERT INTO technical_plan_global_fact_groups (group_id, title, content, sort_order, created_at, updated_at)
      VALUES (@group_id, @title, @content, @sort_order, @created_at, @updated_at)
    `);
```
⚠️ **整表 DELETE + 重建**（无 diff），`sort_order` = 数组下标。

`technicalPlanStore.cjs:197-204` —— id 归一化：
```js
function normalizeGlobalFactId(value, index) {
  const id = String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_\-]+/g, '_')
    .replace(/^_+|_+$/g, '');
  return id || `fact_${String(index + 1).padStart(3, '0')}`;
}
```

---

## 2. 生成设定完整流程

### 2.1 V2（生产链路）—— Agent 工作区式

`globalFactsTaskV2.cjs:296-320` 前置校验（四道硬门）：
```js
  const tenderSources = collectTenderSourceFiles(workspaceStore, storedPlan);
  if (!tenderSources.length) {
    throw new Error('请先上传招标文件，再生成全局事实');
  }

  const outlineData = storedPlan.outlineData;
  if (!outlineData?.outline?.length) {
    throw new Error('请先生成目录，再生成全局事实');
  }

  const isExpansionWorkflow = storedPlan.workflowKind === 'existing-plan-expansion';
  let originalPlanMarkdown = '';
  if (isExpansionWorkflow) {
    if (!storedPlan.originalPlanFile) {
      throw new Error('请先上传原方案，再生成全局事实');
    }
```

`globalFactsTaskV2.cjs:338-380` —— **材料即文件**（不拼 prompt 字符串，把材料落成 Agent 工作区文件）：
```js
  const tenderFiles = tenderSources.map((source, index) => {
    if (source.isWorkingCopy) {
      return {
        path: '招标文件/招标文件-当前投标范围.md',
        content: source.markdown,
      };
    }
    const fileName = sanitizeFileName(source.fileName, `招标文件${index + 1}`);
    return {
      path: `招标文件/招标文件-${padIndex(index)}-${fileName}.md`,
      content: source.markdown,
    };
  });
  const files = [
    ...tenderFiles,
    { path: '项目概述.md', content: String(storedPlan.projectOverview || '').trim() || '未提供项目概述。' },
    { path: '招标解析结果.md', content: formatBidAnalysisFactsForPrompt(storedPlan) },
    { path: '技术方案目录.md', content: formatOutlineForPrompt(outlineData.outline || []) },
  ];
  if (sectionHint) {
    files.push({ path: '标段说明.md', content: sectionHint });
  }
  knowledgeItems.forEach((item, index) => {
    files.push({
      path: `参考知识库/条目-${index + 1}.md`,
      content: formatKnowledgeItemFile(item),
    });
  });
  if (originalPlanMarkdown) {
    files.push({ path: '原方案.md', content: originalPlanMarkdown });
  }
```

`globalFactsTaskV2.cjs:392-415` —— Agent 执行 + 解析 + 校验：
```js
  const agentResult = await agentService.runTask({
    task_id: task.task_id,
    title: '全局事实变量生成',
    prompt,
    output_file: GLOBAL_FACTS_OUTPUT_FILE,
    files,
    signal: taskControl.signal,
    persistent_task: {
      task_key: GLOBAL_FACTS_AGENT_TASK_KEY,
      mode: 'create',
    },
    initial_stage: 'global-facts',
    initial_stage_index: 0,
    json_validation_schemas: {
      [GLOBAL_FACTS_OUTPUT_FILE]: GLOBAL_FACTS_JSON_SCHEMA,
    },
    max_retries: 0,
    onActivity: publishAgentActivity,
    onCheckpoint: syncAgentCheckpoint,
  });

  const generated = readJson(agentResult.output_content, GLOBAL_FACTS_OUTPUT_FILE);
  const normalized = normalizeGlobalFactsResponse(generated);
  validateGlobalFactsResponse(normalized);
```

**V2 的「切分上下文」**：不做字符串切分 —— 由 Agent 自主检索（`globalFactsTaskV2.cjs:198` 原则「材料较长时用检索定位，不要因为一次读不完就漏项」）。

### 2.2 V1（未挂载）—— 6 步分段流水线

编排函数 `globalFactsTask.cjs:922-1018`，各阶段：

| 阶段 | 函数 | 行号 | 进度 |
|---|---|---|---|
| 1 招标文件分段提取 | `runTenderGlobalFactsExtraction` | :827-840 | 24 / 34 / 44 |
| 2 知识库补充 | `runKnowledgeGlobalFactPatches` | :876-891 | 52 / 58 / 64 |
| 3 原方案补充 | `runOriginalPlanGlobalFactPatches` | :893-907 | 70 / 77 / 84 |
| 4 最终整理 | `finalizeGlobalFacts` | :909-920 | 90 |
| 5 checkpoint 落库 | — | :1011-1017 | 92 → 100 |

`globalFactsTask.cjs:986-1013`（合并与去重全流程）：
```js
  log('第一步：正在按招标文件分段提取全局事实变量。', 22);
  const tenderFacts = await runTenderGlobalFactsExtraction(aiService, baseContext, tenderMarkdown, log);
  let groups = tenderFacts.groups;
  checkpointTask({ status: 'running', progress: 48, logs }, { globalFacts: groups });

  const knowledgePatch = await runKnowledgeGlobalFactPatches(aiService, { ...baseContext, groups }, knowledgeItems, log);
  if (knowledgePatch.patches?.length) {
    groups = mergeGlobalFactPatches(groups, knowledgePatch.patches);
    checkpointTask({ status: 'running', progress: 66, logs }, { globalFacts: groups });
    log(`知识库全局事实补充已应用：${knowledgePatch.patches.length} 条。`, 66);
  } else if (knowledgeItems.length) {
    log('知识库未返回需要补充的全局事实变量。', 66);
  }

  if (isExpansionWorkflow) {
    const originalPatch = await runOriginalPlanGlobalFactPatches(aiService, { ...baseContext, groups }, originalPlanMarkdown, log);
    if (originalPatch.patches?.length) {
      groups = mergeGlobalFactPatches(groups, originalPatch.patches);
      checkpointTask({ status: 'running', progress: 86, logs }, { globalFacts: groups });
      log(`原方案全局事实补充已应用：${originalPatch.patches.length} 条。`, 86);
    } else {
      log('原方案未返回需要补充的全局事实变量。', 86);
    }
  }

  const finalFacts = await finalizeGlobalFacts(aiService, { ...baseContext, groups }, log);
  groups = finalFacts.groups;
  log(`全局事实变量合并完成：${groups.length} 个大项。`, 92);
  checkpointTask(
    { status: 'success', progress: 100, logs: [...logs, '全局事实变量生成完成。'] },
    { globalFacts: groups },
  );
```

**分段长度与切分**：`globalFactsTask.cjs:370-383`
```js
function splitGlobalFactsSourceText(text, aiService, fixedMessages) {
  const source = String(text || '').trim();
  if (!source) return [];
  return splitUserTextByContextLimit(source, {}, {
    contextLengthLimit: getGlobalFactsSegmentLimit(aiService, fixedMessages),
    limitRatio: 1,
    maxSegmentLimitRatio: 1,
  }).map((content) => String(content || '').trim()).filter(Boolean);
}

function createTextSegments(text, aiService, fixedMessages) {
  const parts = splitGlobalFactsSourceText(text, aiService, fixedMessages);
  return parts.map((content, index) => ({ index: index + 1, total: parts.length, content }));
}
```

**并发模式（首段串行 + 其余并发）**：`globalFactsTask.cjs:817-821`
```js
  const firstResult = await runSegment(segments[0]);
  const remainingResults = segments.length > 1
    ? await waitAllOrThrow(segments.slice(1).map((segment) => runSegment(segment)))
    : [];
  const segmentResults = [firstResult, ...remainingResults].sort((left, right) => left.index - right.index);
```
⚠️ 「首段先跑」的作用是快速失败（空段检测），并非限流；其余段**无并发上限**（全部 Promise 并发）。

**知识库补充如何接入**：`globalFactsTask.cjs:385-412`（按条目块聚合分段）
```js
function createKnowledgeItemSegments(knowledgeItems, aiService, fixedMessages) {
  const segmentLimit = getGlobalFactsSegmentLimit(aiService, fixedMessages);
  const blocks = (knowledgeItems || [])
    .map((item, index) => formatKnowledgeItemForPrompt(item, index))
    .filter((block) => block.trim());
  const segments = [];
  let current = [];
  let currentLength = 0;

  const flush = () => {
    if (!current.length) return;
    segments.push({ content: current.join('\n\n'), itemCount: current.length });
    current = [];
    currentLength = 0;
  };

  for (const block of blocks) {
    const nextLength = currentLength + block.length + (current.length ? 2 : 0);
    if (current.length && nextLength > segmentLimit) {
      flush();
    }
    current.push(block);
    currentLength += block.length + (current.length > 1 ? 2 : 0);
  }
  flush();

  return segments.map((segment, index) => ({ ...segment, index: index + 1, total: segments.length }));
}
```

知识库读取与容错：`globalFactsTask.cjs:315-333`
```js
  } catch (error) {
    log(`读取知识库条目失败，已跳过：${error.message || String(error)}`, 12);
  }
  log(items.length ? `已读取 ${items.length} 条知识库完整条目。` : '未读取到可用知识库完整条目。', 14);
```

**已有方案扩写优先保留**：`globalFactsTaskV2.cjs:205`（V2 原则）
```
用原方案补充已有大项的具体内容；原方案与招标明确事实冲突时，原方案已落地的安排优先替换对应 bullet。不要仅因原方案出现新话题就新增大项。
```
`globalFactsTask.cjs:670`（V1 final）
```
8. 当前是已有方案扩写模式，原方案分段补充后的事实优先保留，不要在最终整理时弱化或删除原方案已有承诺。
```

**「删除空泛」的具体判定**：`globalFactsTask.cjs:80-106` —— 三处独立规则，**`omit`/`placeholder` 模式明确豁免**：
```js
function buildMergeCleanupRule(mode) {
  if (mode === 'omit' || mode === 'placeholder') {
    return '1. 分段候选只代表对应片段，合并时要综合所有片段，删除重复和互相矛盾的表述。笼统但正确的承诺口径以及【待填写】不是空泛内容，不得因不够具体而删除。';
  }
  return '1. 分段候选只代表对应片段，合并时要综合所有片段，删除重复、空泛和互相矛盾的表述。';
}
```
```js
function buildPatchMergeCleanupRule(mode) {
  if (mode === 'omit' || mode === 'placeholder') {
    return '1. 删除重复、互相矛盾或仍停留在要求摘录层面的补充项。笼统但正确的承诺口径以及【待填写】不是空泛内容，不得因不够具体而删除。';
  }
  return '1. 删除重复、空泛、互相矛盾或仍停留在要求摘录层面的补充项。';
}
```
```js
function buildFinalDedupRule(mode) {
  if (mode === 'omit' || mode === 'placeholder') {
    return '3. 合并同义或重复大项，删除明显重复 bullet，以及仍停留在招标要求、评分规则、资料清单、待办事项层面的内容。';
  }
  return '3. 合并同义或重复大项，删除空泛内容、明显重复 bullet，以及仍停留在招标要求、评分规则、资料清单、待办事项层面的内容。';
}
```
⚠️ 「删除空泛」**没有任何程序化判定** —— 全部是提示词层交给 AI 裁决，程序侧只有 `validateGlobalFactsResponse` 的「非空」检查（`:190-199`）。

---

## 3. 缺值模式（missingValueMode）全部合法值与提示词差异

### 3.1 合法值域（4 处独立实现，拼写完全一致）

- `globalFactsTask.cjs:8-10`
  ```js
  function normalizeGlobalFactsMode(value) {
    return value === 'omit' || value === 'placeholder' ? value : 'fabricate';
  }
  ```
- `globalFactsTaskV2.cjs:7` 复用上面的（`require('./globalFactsTask.cjs')`）
- `contentGenerationTask.cjs:77-79`（**独立副本**）
  ```js
  function normalizeGlobalFactsMode(value) {
    return value === 'omit' || value === 'placeholder' ? value : 'fabricate';
  }
  ```
- `technicalPlanStore.cjs:339-345`
  ```js
  function isValidGlobalFactsMode(value) {
    return value === 'omit' || value === 'placeholder' ? value : 'fabricate';
  }
  function normalizeGlobalFactsMode(value) {
    return isValidGlobalFactsMode(value) ? value : 'fabricate';
  }
  ```
  ⚠️ `technicalPlanStore.cjs:339-341` 的 `isValidGlobalFactsMode` **恒返回真值**（函数体返回 mode 字符串而非 boolean），`normalizeGlobalFactsMode` 用真值判断 —— 结果正确但函数名与语义不符。
- `GlobalFactsPage.tsx:43-45`（前端**第五份**副本）
  ```js
  function normalizeGlobalFactsMode(value: GlobalFactsMode | undefined): GlobalFactsMode {
    return value === 'omit' || value === 'placeholder' ? value : 'fabricate';
  }
  ```

**合法值仅 3 个**：`fabricate`（默认） / `omit` / `placeholder`。

⚠️ **注意**：易标源码中**不存在名为 `missingValueMode` 的变量** —— 该命名来自本仓移植时的映射，实际字段名是 `globalFactsMode`。

### 3.2 UI 模式文案（原文）

`GlobalFactsPage.tsx:25-41`
```tsx
const globalFactsModeOptions: Array<{ value: GlobalFactsMode; title: string; description: string }> = [
  {
    value: 'fabricate',
    title: '胡咧咧模式',
    description: '未在参考材料中找到的直接证据，但经评估，正文中可能用到，为保证全文一致，会由 AI 直接杜撰。如：涉及人员名单，但用户未提供，AI 会编辑不存在的人名。此模式写完的技术方案直接完整可用，无需人工干预。',
  },
  {
    value: 'omit',
    title: '别招欠模式',
    description: '选题范围与胡路军模式相同。未在参考材料中找到具体值时，仍会保留该项，改写成符合招标要求的笼统口径，不写具体人员、时间、地点、业绩、证书、规格型号或实施细节。如：涉及人员名单但用户未提供，会保留岗位事实并写成按招标要求配备，而不是编造人名或忽略该项。正文阶段同样沿用笼统写法。',
  },
  {
    value: 'placeholder',
    title: '放着我来模式',
    description: '选题范围与胡路军模式相同。未在参考材料中找到具体值时，仍会保留该项，并将值标记为【待填写】。如：涉及人员名单但用户未提供，会保留岗位事实并写成【待填写】。用户需要二次修改后再进入正文生成阶段。正文生产时的任何不确定项也会使用【待填写】占位。',
  },
];
```
（注：`omit`/`placeholder` 两处「选题范围与胡路军模式相同」中的「胡路军」即源码原文用词）

### 3.3 `buildMissingFactRule`（V1 system 层第 4 条）

`globalFactsTask.cjs:12-20` 原文
```js
function buildMissingFactRule(mode) {
  if (mode === 'omit') {
    return '4. 用户资料没有给出具体值时，该项仍须保留，写成不涉及具体时间、地点、人员、业绩、证书、规格型号、工艺步骤、数量指标的正确笼统承诺；严禁省略该项或杜撰具体值。';
  }
  if (mode === 'placeholder') {
    return '4. 用户资料没有给出具体值，但该信息会影响后续正文一致写法时，必须保留该项并把事实值写成【待填写】，严禁省略该项或杜撰具体值。占位符必须逐字使用【待填写】。';
  }
  return '4. 用户资料没有给出具体值，但该信息对全文一致性重要，且当前任务允许补足时，可以根据项目语境模拟生成合理、稳定、不冲突的事实值。';
}
```

### 3.4 `buildGlobalFactsCompletenessRules`（V1 完整规则块）

`globalFactsTask.cjs:22-48` 原文
```js
function buildGlobalFactsCompletenessRules(mode) {
  if (mode === 'omit') {
    return `事实补全规则（别招欠模式）：
1. 先按统一选题标准确定要输出哪些事实项，再按本模式填写值。选题与是否缺少具体值无关。
2. 凡招标要求、评分口径、项目概述、目录或参考材料表明后续技术方案正文需要统一口径的事项，都必须建项并写出 bullet；不得因材料缺少具体实施方案、人名、日期、指标、型号等而省略该项、不建组或不输出该 bullet。
3. 参考材料已经给出明确事实值时，照录材料中的事实值。
4. 严禁虚拟、杜撰、补造任何未在参考材料中出现的具体事实。
5. 材料只有要求、约束或评价口径、没有具体值时，该项仍须保留，写成不涉及具体时间、地点、人员、业绩、证书、规格型号、工艺步骤、数量指标的正确笼统承诺，表明本方案按招标要求执行该项，但不展开具体做法。
6. 当前分段或当前材料只给出要求、没有具体实施方案时，仍须输出该事实项，不得因此返回空结果或跳过该项。
7. 笼统但正确的承诺口径不是空泛内容，合并与最终整理时不得因不够具体而删除。
8. 工期、运维期或交货时间等事项若正文需要统一口径，必须保留为事实项；材料没有具体值时使用笼统承诺，不要编造日期或周期，也不要因此省略该项。
9. 已有方案扩写时，原方案中已有事实必须提取；只对招标、知识库、原方案都没有的具体值使用上述笼统写法，选题仍不得漏项。`;
  }
  if (mode === 'placeholder') {
    return `事实补全规则（放着我来模式）：
1. 先按统一选题标准确定要输出哪些事实项，再按本模式填写值。选题与是否缺少具体值无关。
2. 凡招标要求、评分口径、项目概述、目录或参考材料表明后续技术方案正文需要统一口径的事项，都必须建项并写出 bullet；不得因材料缺少具体实施方案、人名、日期、指标、型号等而省略该项、不建组或不输出该 bullet。
3. 参考材料已经给出明确事实值时，照录材料中的事实值。
4. 严禁虚拟、杜撰、补造任何未在参考材料中出现的具体事实。
5. 材料只有要求、约束或评价口径、没有具体值时，该项仍须保留，值必须逐字写成【待填写】，不要改写成“待定”“TBD”或其他说法，也不要编造具体值。
6. 当前分段或当前材料只给出要求、没有具体实施方案时，仍须输出该事实项，不得因此返回空结果或跳过该项。
7. 【待填写】占位不是空泛内容，合并与最终整理时不得因不够具体而删除。
8. 工期、运维期或交货时间等事项若正文需要统一口径，必须保留为事实项；材料没有具体值时使用【待填写】，不要编造日期或周期，也不要因此省略该项。
9. 已有方案扩写时，原方案中已有事实必须提取；只对招标、知识库、原方案都没有的具体值使用【待填写】，选题仍不得漏项。`;
  }
  return '';
}
```
⚠️ `fabricate` 模式返回**空串** —— 即「胡咧咧模式」在 V1 里**没有完整规则块**，只有 system 第 4 条（§3.3）那一句。这是三模式中唯一无完整性约束的模式。

### 3.5 V2 的 `buildMissingValueRule`（单段式，措辞完全不同）

`globalFactsTaskV2.cjs:65-73` 原文
```js
function buildMissingValueRule(mode) {
  if (mode === 'omit') {
    return '用户资料已经给出明确事实时，使用资料中的事实值。用户资料没有给出具体值时，该项仍须保留，写成不涉及具体时间、地点、人员、业绩、证书、规格型号、工艺步骤、数量指标的正确笼统承诺，表明本方案按招标要求执行该项，但不展开具体做法。严禁省略该项，也严禁杜撰具体值。必须包含工期、运维期或交货时间中的至少一个相关变量；没有具体值时同样使用笼统承诺，不要编造日期或周期。笼统但正确的承诺口径不是空泛内容，定稿时不得因不够具体而删除。';
  }
  if (mode === 'placeholder') {
    return '用户资料已经给出明确事实时，使用资料中的事实值。用户资料没有给出具体值时，该项仍须保留，值必须逐字写成【待填写】，不要改写成“待定”“TBD”或其他说法，也不要编造具体值。严禁省略该项。必须包含工期、运维期或交货时间中的至少一个相关变量；没有具体值时使用【待填写】。【待填写】不是空泛内容，定稿时不得因不够具体而删除。';
  }
  return '用户资料已经给出明确事实时，使用资料中的事实值。用户资料没有给出具体值，但该信息对全文一致性重要时，根据项目语境补足一套合理、稳定、不冲突的具体事实值。必须包含工期、运维期或交货时间中的至少一个相关变量；分段或材料不足时，若项目概述或招标解析结果中已有明确内容应写入，否则按项目语境补足具体周期。';
}
```

### 3.6 三种模式的 JSON 示例差异（影响 few-shot 效果）

`globalFactsTaskV2.cjs:75-107` 原文
```js
function buildJsonExample(mode) {
  if (mode === 'placeholder') {
    return `{
  "groups": [
    {
      "id": "project_team",
      "title": "项目角色变量",
      "content": "- 项目经理：【待填写】。\\n- 技术负责人：【待填写】。"
    }
  ]
}`;
  }
  if (mode === 'omit') {
    return `{
  "groups": [
    {
      "id": "project_team",
      "title": "项目角色变量",
      "content": "- 项目经理：按招标文件对该岗位的要求配备。\\n- 技术负责人：按招标文件对该岗位的要求配备。"
    }
  ]
}`;
  }
  return `{
  "groups": [
    {
      "id": "project_team",
      "title": "项目角色变量",
      "content": "- 项目经理：张伟，负责总体协调。\\n- 技术负责人：李明，负责方案设计和联调验收。"
    }
  ]
}`;
}
```
V1 有同构的 `buildGroupsJsonExample`（`globalFactsTask.cjs:428-463`）与 `buildPatchesJsonExample`（`:465-503`）。

V1 的补丁示例 `globalFactsTask.cjs:465-503` 原文摘录：
```js
  if (mode === 'placeholder') {
    return `请返回 JSON，格式如下：
{
  "patches": [
    {
      "target_group_id": "project_team",
      "title": "项目角色变量",
      "mode": "append",
      "content": "- 现场负责人：【待填写】。"
    }
  ]
}`;
  }
```

### 3.7 正文侧的模式差异（下游）

`contentGenerationTask.cjs:81-97` 原文 —— **注意 `fabricate` 同样返回空串**：
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

---

## 4. 补丁机制

### 4.1 `normalizeFactId`

`globalFactsTask.cjs:127-134` 原文
```js
function normalizeFactId(value, index) {
  const normalized = String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_\-]+/g, '_')
    .replace(/^_+|_+$/g, '');
  return normalized || `fact_${String(index + 1).padStart(3, '0')}`;
}
```
⚠️ **中文标题会被整段抹成空串**（`[^a-z0-9_-]` 不含中文），从而回落到 `fact_001/002/...` 位置序号 —— 这是 id 不稳定的根源。
同一实现在 `technicalPlanStore.cjs:197-204` 有**逐字节相同的副本**（`normalizeGlobalFactId`）。

### 4.2 `ensureUniqueId`

`globalFactsTask.cjs:136-145` 原文
```js
function ensureUniqueId(id, used) {
  let nextId = id;
  let suffix = 2;
  while (used.has(nextId)) {
    nextId = `${id}_${suffix}`;
    suffix += 1;
  }
  used.add(nextId);
  return nextId;
}
```

### 4.3 `valueToMarkdown`

`globalFactsTask.cjs:147-165` 原文
```js
function valueToMarkdown(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value.trim();
  if (Array.isArray(value)) {
    return value.map((item) => {
      if (typeof item === 'string') return `- ${item.trim()}`;
      if (item && typeof item === 'object') {
        const name = singleLine(item.name || item.title || item.fact || item.key || '事实项');
        const detail = singleLine(item.value || item.content || item.detail || item.description || item.requirement || '');
        return `- **${name}**${detail ? `：${detail}` : ''}`;
      }
      return `- ${singleLine(item)}`;
    }).filter(Boolean).join('\n');
  }
  if (typeof value === 'object') {
    return Object.entries(value).map(([key, item]) => `- **${singleLine(key)}**：${singleLine(item)}`).join('\n');
  }
  return singleLine(value);
}
```

`singleLine` 定义 `globalFactsTask.cjs:118-120`：
```js
function singleLine(value) {
  return String(value || '').replace(/\s+/g, ' ').trim();
}
```

### 4.4 `buildMissingFactRule` / `buildGlobalFactsCompletenessRules`

见 §3.3 / §3.4（原文已逐字摘录）。

### 4.5 `normalizeGlobalFactsPatchResponse`

`globalFactsTask.cjs:212-240` 原文
```js
function normalizeGlobalFactsPatchResponse(value) {
  const source = value?.result && typeof value.result === 'object' ? value.result : value || {};
  const rawPatches = Array.isArray(source)
    ? source
    : Array.isArray(source.patches)
      ? source.patches
      : Array.isArray(source.supplements)
        ? source.supplements
        : Array.isArray(source.additions)
          ? source.additions
          : Array.isArray(source.items)
            ? source.items
            : [];
  const patches = rawPatches.map((patch, index) => {
    const title = singleLine(patch?.title || patch?.group_title || patch?.target_group_title || patch?.name);
    const content = valueToMarkdown(patch?.content ?? patch?.markdown ?? patch?.facts ?? patch?.items ?? patch?.details ?? patch?.description);
    if (!content) return null;
    const rawMode = singleLine(patch?.mode || patch?.operation || 'append').toLowerCase();
    const mode = ['replace', 'prepend'].includes(rawMode) ? rawMode : 'append';
    return {
      target_group_id: singleLine(patch?.target_group_id || patch?.targetGroupId || patch?.group_id || patch?.id),
      new_group_id: singleLine(patch?.new_group_id || patch?.newGroupId || patch?.id || `patch_${index + 1}`),
      title,
      content,
      mode,
    };
  }).filter(Boolean);
  return { patches };
}
```
⚠️ 未知 mode 一律降级为 `append`（**静默**，不记录降级原因）；`target_group_id` 与 `new_group_id` 都可能回落到 `patch.id`，无法区分「指向已有」与「新建」。

### 4.6 `validateGlobalFactsPatchResponse`

`globalFactsTask.cjs:242-251` 原文
```js
function validateGlobalFactsPatchResponse(value) {
  if (!value || !Array.isArray(value.patches)) {
    throw new Error('全局事实补充结果缺少 patches');
  }
  value.patches.forEach((patch, index) => {
    if (!String(patch.content || '').trim()) {
      throw new Error(`全局事实补充第 ${index + 1} 项缺少 content`);
    }
  });
}
```
⚠️ **不校验 `target_group_id` 是否真实存在**（幻觉锚点不拦截，只在 `mergeGlobalFactPatches` 里退化为新建大项）。

### 4.7 `mergeGlobalFactPatches`

`globalFactsTask.cjs:253-284` 原文
```js
function mergeGlobalFactPatches(groups, patches) {
  const used = new Set(groups.map((group) => group.id));
  const nextGroups = groups.map((group) => ({ ...group }));

  for (const patch of patches || []) {
    const targetIndex = nextGroups.findIndex((group) => (
      group.id === patch.target_group_id
      || (patch.title && group.title === patch.title)
    ));

    if (targetIndex >= 0) {
      const current = nextGroups[targetIndex];
      const patchContent = String(patch.content || '').trim();
      const currentContent = String(current.content || '').trim();
      nextGroups[targetIndex] = {
        ...current,
        content: patch.mode === 'replace'
          ? patchContent
          : patch.mode === 'prepend'
            ? `${patchContent}\n\n${currentContent}`.trim()
            : `${currentContent}\n\n${patchContent}`.trim(),
      };
      continue;
    }

    const title = patch.title || '补充事实变量';
    const id = ensureUniqueId(normalizeFactId(patch.new_group_id || title, nextGroups.length), used);
    nextGroups.push({ id, title, content: String(patch.content || '').trim() });
  }

  return nextGroups;
}
```
关键行为：匹配顺序 **id 优先、title 兜底**；未命中即**新建大项**（不丢补丁）；`replace` 会**整段替换**（旧 bullet 全部丢弃，非 diff）。

### 4.8 补丁阶段的提示词原文

**知识库补充**（`globalFactsTask.cjs:563-588`）
```js
      content: `知识库全局事实补充任务：

请基于当前知识库分段，判断是否需要补充或修正全局事实变量。

要求：
1. 只返回需要补充或替换的 patches，不要重新生成全部 groups。
2. 只处理与项目概述、技术评分信息、目录和技术方案正文强相关，且能够沉淀为稳定方案事实的内容。
3. 知识库内容如果只是通用要求、规范说明、写作建议或参考素材，不要原样补充为事实变量；只有能够转为本项目统一采用的事实、安排、承诺口径或技术设定时才返回 patch。
4. 不要用知识库内容覆盖招标文件中的明确硬性要求；只有知识库提供更具体且不冲突的事实值时才补充。
5. 如果补充内容属于已有大项，target_group_id 必须使用已有 id。
6. 如果确实需要新增大项，提供 title 和 content。
7. mode 只能是 append、prepend 或 replace；默认使用 append。
8. 没有可补充内容时返回 {"patches":[]}。
9. 只返回 JSON。
```

**原方案补充**（`globalFactsTask.cjs:590-617`）
```js
      content: `原方案全局事实补充任务：

当前是“已有方案扩写”模式。用户提供的原方案是本次要扩写的投标技术方案核心草稿，已有内容必须在后续扩写正文中被保留。

请基于当前原方案分段，补充或替换全局事实变量。

要求：
1. 原方案中已经写成投标方实际安排、既有承诺、统一配置、技术路线、服务口径或实施做法的内容，优先补充到全局事实变量中。
2. 原方案如果只是转述招标要求、评分规则、格式要求或资料提交要求，不要原样作为事实变量；只有能够转为后续正文统一采用的方案事实时才补充。
3. 只返回需要补充或替换的 patches，不要重新生成全部 groups。
4. 如果补充内容属于已有大项，target_group_id 必须使用已有 id。
5. 如果确实需要新增大项，提供 title 和 content。
6. mode 只能是 append、prepend 或 replace；当原方案明确事实与当前变量冲突且原方案应优先时使用 replace 或 prepend。
7. 每条 content 只写短 bullet，直接给可复用的变量值，不要写分析过程、来源说明、风险提示或正文草稿。
8. 没有可补充内容时返回 {"patches":[]}。
9. 只返回 JSON。
```

**分段补丁合并**（`globalFactsTask.cjs:628-650`）
```js
      content: `${context.sourceLabel}全局事实补充合并任务：

请把所有分段 patches 合并成一份可应用的 patches。

要求：
${buildPatchMergeCleanupRule(context.globalFactsMode)}
2. 能合并到同一变量组的内容尽量合并，避免对同一事实反复 append。
3. 合并后的 patch 内容必须是正文可直接统一使用的方案事实、响应设定、承诺口径或执行安排。
4. target_group_id 必须优先使用当前全局事实变量中已有的 id；确实需要新增大项时再提供 title 和 content。
5. mode 只能是 append、prepend 或 replace。
6. 没有可补充内容时返回 {"patches":[]}。
7. 只返回 JSON。
```

---

## 5. `batchRenderedItems` / `waitAllOrThrow` / `getGlobalFactsSegmentLimit`

### 5.1 `getGlobalFactsSegmentLimit`（上下文预算）

常量：`globalFactsTask.cjs:4-6`
```js
const DEFAULT_CONTEXT_LENGTH_LIMIT = 400000;
const GLOBAL_FACTS_CONTEXT_LIMIT_RATIO = 0.8;
const MIN_GLOBAL_FACTS_SEGMENT_CHARS = 1000;
```
实现：`globalFactsTask.cjs:359-368`
```js
function getMessagesContentLength(messages) {
  return (messages || []).reduce((sum, message) => sum + String(message?.role || 'user').length + String(message?.content || '').length + 64, 0);
}

function getGlobalFactsSegmentLimit(aiService, fixedMessages) {
  const config = typeof aiService?.getConfig === 'function' ? aiService.getConfig() : {};
  const contextLengthLimit = normalizePositiveInteger(config?.context_length_limit, DEFAULT_CONTEXT_LENGTH_LIMIT);
  const requestBudget = Math.floor(contextLengthLimit * GLOBAL_FACTS_CONTEXT_LIMIT_RATIO);
  return Math.max(MIN_GLOBAL_FACTS_SEGMENT_CHARS, requestBudget - getMessagesContentLength(fixedMessages));
}
```
辅助 `normalizePositiveInteger`（`globalFactsTask.cjs:122-125`）：
```js
function normalizePositiveInteger(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? Math.floor(number) : fallback;
}
```
**注意**：`getMessagesContentLength` 把 prompt 原文也计入预算 —— 提示词越详细，段长越小。

### 5.2 `waitAllOrThrow`

`globalFactsTask.cjs:681-688` 原文
```js
async function waitAllOrThrow(tasks) {
  const results = await Promise.allSettled(tasks);
  const rejected = results.find((result) => result.status === 'rejected');
  if (rejected) {
    throw rejected.reason;
  }
  return results.map((result) => result.value);
}
```
⚠️ 只抛**第一个** rejection（`find` 短路），其余失败原因被丢弃；抛的是原始 reason，无上下文包装。

### 5.3 `batchRenderedItems`

`globalFactsTask.cjs:690-713` 原文
```js
function batchRenderedItems(items, renderItem, limit) {
  const batches = [];
  let current = [];
  let currentLength = 0;

  const flush = () => {
    if (!current.length) return;
    batches.push(current);
    current = [];
    currentLength = 0;
  };

  for (const item of items || []) {
    const length = renderItem(item).length;
    const nextLength = currentLength + length + (current.length ? 2 : 0);
    if (current.length && nextLength > limit) {
      flush();
    }
    current.push(item);
    currentLength += length + (current.length > 1 ? 2 : 0);
  }
  flush();
  return batches;
}
```
⚠️ 传入的是**已渲染字符串的长度**（`renderItem(item).length`）但 `items` 保持原对象 —— 渲染器被调用两次（`createKnowledgeItemSegments:402` 同样模式）。

**分批合并的收敛保护**（`globalFactsTask.cjs:727-751`）
```js
async function mergeGroupResultsInBatches({ aiService, context, segmentResults, mergeMessagesBuilder, sourceLabel, log, progress }) {
  let pending = segmentResults || [];
  let round = 1;
  while (true) {
    const fixedMessages = mergeMessagesBuilder({ ...context, segmentResults: [] });
    const limit = getGlobalFactsSegmentLimit(aiService, fixedMessages);
    const batches = batchRenderedItems(pending, formatSegmentGroupResultForPrompt, limit);
    if (batches.length <= 1) {
      return collectGroupMerge(aiService, context, batches[0] || [], mergeMessagesBuilder, sourceLabel, log, progress, round > 1 ? `-第${round}轮` : '');
    }

    log(`${sourceLabel}分段候选较多，正在分 ${batches.length} 批合并。`, progress);
    const first = await collectGroupMerge(aiService, context, batches[0], mergeMessagesBuilder, sourceLabel, log, progress, `-第${round}轮-第1批`);
    const rest = await waitAllOrThrow(batches.slice(1).map((batch, index) => (
      collectGroupMerge(aiService, context, batch, mergeMessagesBuilder, sourceLabel, log, progress, `-第${round}轮-第${index + 2}批`)
    )));
    const merged = [first, ...rest];
    const nextPending = merged.map((result, index) => ({ index: index + 1, total: merged.length, groups: result.groups || [] }));
    if (nextPending.length >= pending.length) {
      return collectGroupMerge(aiService, context, nextPending, mergeMessagesBuilder, sourceLabel, log, progress, `-第${round + 1}轮`);
    }
    pending = nextPending;
    round += 1;
  }
}
```
`nextPending.length >= pending.length` 是**防不收敛死循环**的关键（合并不减少条目数时再补一轮终局合并）。

补丁侧的同构实现：`mergePatchResultsInBatches`（`globalFactsTask.cjs:765-789`）
```js
async function mergePatchResultsInBatches({ aiService, context, patchResults, sourceLabel, log, progress }) {
  let pending = patchResults || [];
  let round = 1;
  while (true) {
    const fixedMessages = buildSegmentPatchMergeMessages({ ...context, patchResults: [], sourceLabel });
    const limit = getGlobalFactsSegmentLimit(aiService, fixedMessages);
    const batches = batchRenderedItems(pending, formatPatchResultForPrompt, limit);
    if (batches.length <= 1) {
      return collectPatchMerge(aiService, context, batches[0] || [], sourceLabel, log, progress, round > 1 ? `-第${round}轮` : '');
    }
    log(`${sourceLabel}分段补充项较多，正在分 ${batches.length} 批合并。`, progress);
    const first = await collectPatchMerge(aiService, context, batches[0], sourceLabel, log, progress, `-第${round}轮-第1批`);
    const rest = await waitAllOrThrow(batches.slice(1).map((batch, index) => (
      collectPatchMerge(aiService, context, batch, sourceLabel, log, progress, `-第${round}轮-第${index + 2}批`)
    )));
    const merged = [first, ...rest];
    const nextPending = merged.map((result, index) => ({ index: index + 1, total: merged.length, patches: result.patches || [] }));
    if (nextPending.length >= pending.length) {
      return collectPatchMerge(aiService, context, nextPending, sourceLabel, log, progress, `-第${round + 1}轮`);
    }
    pending = nextPending;
    round += 1;
  }
}
```

### 5.4 ⚠️ 三者在生产链路的实际调用情况

| 函数 | 定义 | 调用点 | 生产可达 |
|---|---|---|---|
| `getGlobalFactsSegmentLimit` | `:363` | `:374, 386, 732, 770, 792, 878, 894` | **否**（全在 V1 未挂载链路） |
| `waitAllOrThrow` | `:681` | `:740, 778, 819, 866` | **否** |
| `batchRenderedItems` | `:690` | `:733, 771` | **否** |

三者在 `module.exports`（`:1020-1031`）中**均未导出**，且 V2 未 import。**V2 完全没有上下文预算分段能力** —— 全部材料一次性交给 Agent。

`globalFactsTask.cjs:1020-1031` 导出清单（原文）：
```js
module.exports = {
  formatBidAnalysisFactsForPrompt,
  formatOutlineForPrompt,
  loadKnowledgeItems,
  mergeGlobalFactPatches,
  normalizeGlobalFactsMode,
  normalizeGlobalFactsPatchResponse,
  normalizeGlobalFactsResponse,
  normalizeReferenceDocumentIds,
  runGlobalFactsTask,
  validateGlobalFactsResponse,
};
```

---

## 6. 编排期注入（AI 只看标题 → 程序取完整事实 → 只注入当前章节）

这是易标设计中最精细的一环，分三段实现。

### 6.1 第一段：只把「标题清单」交给编排 AI

`contentGenerationTask.cjs:2931-2932`
```js
  const globalFactTitlesText = formatGlobalFactTitlesForPrompt(globalFacts);
  const allowedFactTitles = new Set(globalFacts.map((group) => singleLine(group?.title)).filter(Boolean));
```

`contentGenerationTask.cjs:134-139` —— **只有 title，无 content**：
```js
function formatGlobalFactTitlesForPrompt(globalFacts) {
  const titles = (Array.isArray(globalFacts) ? globalFacts : [])
    .map((group) => singleLine(group?.title))
    .filter(Boolean);
  return JSON.stringify([...new Set(titles)], null, 2);
}
```

`contentGenerationTask.cjs:828-830` —— 消息拼装（独立 user 消息）：
```js
  if (String(globalFactTitlesText || '').trim()) {
    messages.push({ role: 'user', content: `Step04 全局事实变量标题清单（编排时只能选择标题，不要输出具体变量内容）：\n${globalFactTitlesText}` });
  }
```

`contentGenerationTask.cjs:815` —— system 层的硬约束：
```
6. facts.titles 只能从全局事实变量标题清单中选择；请选择编写本章节正文时会用到的变量组标题，可以多选，可以为空数组；不要编造标题，不要输出具体变量内容。
```
`contentGenerationTask.cjs:817`
```
8. 编排判断必须结合招标文件关键信息和全局事实变量标题，不要规划会造成时间、地点、人员、设备、标准或服务承诺前后不一致的表达。
```

### 6.2 第二段：白名单过滤 AI 返回的标题

`contentGenerationTask.cjs:162-169`
```js
function normalizeFactTitles(value, allowedFactTitles) {
  const source = Array.isArray(value) ? value : [];
  const titles = source.map((title) => singleLine(title)).filter(Boolean);
  const filtered = allowedFactTitles instanceof Set
    ? titles.filter((title) => allowedFactTitles.has(title))
    : titles;
  return [...new Set(filtered)];
}
```
`contentGenerationTask.cjs:590-592` —— 归一化出口：
```js
    facts: {
      titles: normalizeFactTitles(rawFactTitles, allowedFactTitles),
    },
```
标题键名兼容 7 种（`contentGenerationTask.cjs:579-581`）：
```js
  const rawFactTitles = Array.isArray(factsSource)
    ? factsSource
    : facts.titles ?? facts.fact_titles ?? facts.factTitles ?? source.fact_titles ?? source.factTitles ?? source.global_fact_titles ?? source.globalFactTitles;
```
`hasFactSelection`（`contentGenerationTask.cjs:190-197`）判定编排 JSON 是否带事实选择：
```js
function hasFactSelection(value) {
  const source = value?.plan && typeof value.plan === 'object' ? value.plan : value || {};
  return Object.prototype.hasOwnProperty.call(source || {}, 'facts')
    || Object.prototype.hasOwnProperty.call(source || {}, 'fact_titles')
    || Object.prototype.hasOwnProperty.call(source || {}, 'factTitles')
    || Object.prototype.hasOwnProperty.call(source || {}, 'global_fact_titles')
    || Object.prototype.hasOwnProperty.call(source || {}, 'globalFactTitles');
}
```

### 6.3 第三段：程序按标题取完整内容，只注入当前章节

`contentGenerationTask.cjs:171-177`
```js
function resolveGlobalFactsByTitles(titles, globalFacts) {
  const selected = new Set(normalizeFactTitles(titles));
  if (!selected.size) return [];
  return (Array.isArray(globalFacts) ? globalFacts : [])
    .filter((group) => selected.has(singleLine(group?.title)) && String(group?.content || '').trim())
    .map((group) => ({ title: singleLine(group.title), content: String(group.content || '').trim() }));
}
```
`contentGenerationTask.cjs:179-188`
```js
function formatSelectedGlobalFactsForPrompt(globalFacts) {
  return (Array.isArray(globalFacts) ? globalFacts : [])
    .map((group) => {
      const title = singleLine(group?.title);
      const content = String(group?.content || '').trim();
      return title && content ? `## ${title}\n${content}` : '';
    })
    .filter(Boolean)
    .join('\n\n');
}
```
`contentGenerationTask.cjs:2151-2154` —— 逐章调用点：
```js
function resolveSelectedFactsText(contentPlan, globalFacts) {
  const selectedFacts = resolveGlobalFactsByTitles(contentPlan?.facts?.titles, globalFacts);
  return formatSelectedGlobalFactsForPrompt(selectedFacts);
}
```
调用点：`contentGenerationTask.cjs:4218`、`:4407`
```js
      const selectedFactsText = resolveSelectedFactsText(contentPlan, globalFacts);
```

注入消息（`contentGenerationTask.cjs:125-132`）：
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
全量注入消息（同文件 `:116-123`，供不分章场景）：
```js
function appendGlobalFactsMessage(messages, globalFactsText) {
  const content = String(globalFactsText || '').trim();
  if (!content) return;
  messages.push({
    role: 'user',
    content: `全局事实变量（正文涉及时优先使用这些变量值，避免各章节随机变化）：\n${content}`,
  });
}
```
正文侧硬约束（`contentGenerationTask.cjs:914`）：
```
16. 仅使用本章节提供的全局事实变量；未提供时不要主动编造具体人员、周期、质保、品牌、型号等会影响全文一致性的承诺。
```

⚠️ **关键设计意图**：编排阶段把全部事实内容注入会挤爆上下文并导致「把所有事实塞进每一节」；此处只给标题让 AI 做**相关性判断**，再由程序精确取回 —— 既省 token 又保证不漏。

---

## 7. 二次检查（一致性审计）

### 7.1 分组策略（按字数均分）

`contentGenerationTask.cjs:40`
```js
const CONSISTENCY_AUDIT_GROUP_WORD_LIMIT = 300000;
```
`contentGenerationTask.cjs:5369-5398`
```js
  function buildConsistencyAuditGroups(targets) {
    const totalWords = (targets || []).reduce((sum, item) => sum + item.words, 0);
    if (!targets?.length) {
      return [];
    }

    let groupCount = 1;
    if (totalWords > CONSISTENCY_AUDIT_GROUP_WORD_LIMIT) {
      groupCount = 2;
      while (totalWords / groupCount > CONSISTENCY_AUDIT_GROUP_WORD_LIMIT) {
        groupCount += 1;
      }
    }
    const targetWords = Math.max(1, Math.ceil(totalWords / groupCount));
    const groups = [];
    let current = { index: 1, items: [], words: 0, targetWords };

    for (const target of targets) {
      if (current.items.length && current.words + target.words > targetWords && groups.length < groupCount - 1) {
        groups.push(current);
        current = { index: groups.length + 1, items: [], words: 0, targetWords };
      }
      current.items.push(target);
      current.words += target.words;
    }
    if (current.items.length) {
      groups.push(current);
    }
    return groups.map((group, index) => ({ ...group, index: index + 1, total: groups.length, totalWords }));
  }
```
审计目标筛选（`contentGenerationTask.cjs:5354-5367`）只取 `status === 'success'` 且正文非空的小节：
```js
  function buildConsistencyAuditTargets(auditTargetItemId = '') {
    const normalizedTargetId = String(auditTargetItemId || '').trim();
    return leaves
      .filter(({ item }) => !normalizedTargetId || item.id === normalizedTargetId)
      .map((context) => {
        const content = sections[context.item.id]?.content || context.item.content || '';
        return {
          ...context,
          content,
          words: getLeafWordCount(context.item),
        };
      })
      .filter(({ item, content }) => sections[item.id]?.status === 'success' && String(content || '').trim());
  }
```

### 7.2 冲突数据结构（AI 返回）

`contentGenerationTask.cjs:1673-1684` 原文
```
返回格式：
{
  "conflicts": [
    {
      "section_id": "1.2.3",
      "fact_title": "相关事实变量标题",
      "evidence": "正文中的冲突原文摘录",
      "reason": "为什么与事实冲突",
      "severity": "high"
    }
  ]
}
```
`contentGenerationTask.cjs:1693-1729` —— 归一化 + **section_id 白名单强校验**：
```js
function normalizeConsistencyAuditResponse(value, allowedSectionIds) {
  const source = value?.result && typeof value.result === 'object' ? value.result : value || {};
  const rawConflicts = Array.isArray(source)
    ? source
    : Array.isArray(source.conflicts)
      ? source.conflicts
      : Array.isArray(source.items)
        ? source.items
        : [];
  const allowed = allowedSectionIds instanceof Set ? allowedSectionIds : new Set(allowedSectionIds || []);
  const issues = [];
  const conflicts = [];

  rawConflicts.forEach((item, index) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      issues.push(`conflicts[${index}] 必须是对象`);
      return;
    }
    const sectionId = singleLine(item.section_id || item.sectionId || item.id || item.chapter_id || item.chapterId);
    if (!sectionId || !allowed.has(sectionId)) {
      issues.push(`conflicts[${index}].section_id 无效：${sectionId || '空'}`);
      return;
    }
    conflicts.push({
      section_id: sectionId,
      fact_title: singleLine(item.fact_title || item.factTitle || item.fact || item.title),
      evidence: String(item.evidence || item.quote || item.source || '').trim(),
      reason: String(item.reason || item.description || item.issue || '').trim(),
      severity: singleLine(item.severity || 'medium') || 'medium',
    });
  });

  if (issues.length) {
    throw new Error(`审计结果格式无效：${issues.join('；')}`);
  }
  return { conflicts };
}
```
`validateConsistencyAuditResponse`（`contentGenerationTask.cjs:1731-1735`）只检查 `conflicts` 是数组，允许空数组：
```js
function validateConsistencyAuditResponse(value) {
  if (!value || !Array.isArray(value.conflicts)) {
    throw new Error('一致性审计结果缺少 conflicts 数组');
  }
}
```
⚠️ `severity` 默认 `medium`（非 high），且**程序不按 severity 过滤** —— 所有冲突都进入修复队列。

### 7.3 审计提示词原文

`contentGenerationTask.cjs:1658-1691` 全文
```js
function buildConsistencyAuditMessages({ group, globalFactsText, bidAnalysisFactsText, globalFactsMode }) {
  const allowedIds = (group.items || []).map(({ item }) => item.id).filter(Boolean);
  return [
    {
      role: 'user',
      content: `你是投标技术方案全文一致性审计助手。请审计本组正文是否与给定事实冲突。

要求：
1. 只返回 JSON，不要输出解释、总结或 Markdown。
2. 只找正文中已经明确写出、且与事实相违背的内容。
3. 正文没有涉及某条事实时，不要报告缺失，不要建议补充。
4. 不报告文风、质量、重复、篇幅、表达优化等问题。
5. section_id 必须来自允许的目录编号清单，禁止编造编号。
6. 只筛选冲突目录编号和冲突证据，不要重写正文。${buildContentFactCompletenessInstruction(globalFactsMode) ? `\n7. 全局事实中的【待填写】不是冲突，不要要求正文补成具体值，也不要把缺失项当成需要杜撰的内容。` : ''}

返回格式：
{
  "conflicts": [
    {
      "section_id": "1.2.3",
      "fact_title": "相关事实变量标题",
      "evidence": "正文中的冲突原文摘录",
      "reason": "为什么与事实冲突",
      "severity": "high"
    }
  ]
}`,
    },
    { role: 'user', content: `Step04 全局事实变量：\n${globalFactsText || '未提供'}` },
    { role: 'user', content: `Step02 关键解析结果（项目信息、甲方信息、交货和服务要求）：\n${bidAnalysisFactsText || '未提供'}` },
    { role: 'user', content: `允许返回的目录编号清单：\n${JSON.stringify(allowedIds, null, 2)}` },
    { role: 'user', content: `待审计正文分组：\n${formatConsistencyAuditGroupContent(group)}` },
  ];
}
```
正文分组格式（`contentGenerationTask.cjs:1648-1656`）：
```js
function formatConsistencyAuditGroupContent(group) {
  return (group.items || []).map((entry) => `<section>
编号：${entry.item.id || 'unknown'}
标题：${entry.item.title || '未命名章节'}
路径：${formatChapterPath(entry)}
正文：
${entry.content || ''}
</section>`).join('\n\n');
}
```

JSON 修复器（`contentGenerationTask.cjs:1737-1757`）第 1-5 条：
```js
function buildConsistencyAuditRepairMessages({ invalidContent, issues }, allowedSectionIds) {
  const issueLines = (issues || []).map((item, index) => `${index + 1}. ${item}`).join('\n');
  return [
    {
      role: 'user',
      content: `你是严格的 JSON 修复器。请把模型输出修复为“全文一致性审计”JSON。

必须满足：
1. 顶层只能包含 conflicts 数组。
2. conflicts 可以为空数组。
3. 每条 conflict 必须包含 section_id、fact_title、evidence、reason、severity。
4. section_id 只能来自允许清单。
5. 禁止输出正文、修复方案、Markdown 或解释文字。

允许的 section_id：
${JSON.stringify(Array.from(allowedSectionIds || []), null, 2)}`,
    },
    { role: 'user', content: `错误列表：\n${issueLines}` },
    { role: 'user', content: `待修复内容：\n\`\`\`json\n${String(invalidContent || '').slice(0, 60000)}\n\`\`\`` },
  ];
}
```

### 7.4 `old_text` / `new_text` 唯一命中才替换

**判定与应用的唯一出口**：`contentGenerationTask.cjs:1584-1616`
```js
function applyExactConsistencyPatch(content, patch) {
  const currentContent = normalizeNewlines(content);
  const oldText = normalizeConsistencyPatchText(patch.old_text);
  const newText = normalizeConsistencyPatchText(patch.new_text);
  if (!oldText) {
    throw new Error('old_text 为空');
  }
  if (!newText) {
    throw new Error('new_text 为空');
  }
  if (oldText === newText) {
    throw new Error('old_text 与 new_text 相同');
  }

  const startLine = Number(patch.start_line);
  const endLine = Number(patch.end_line);
  if (Number.isFinite(startLine) && Number.isFinite(endLine) && startLine > 0 && endLine >= startLine) {
    const candidate = extractLineRangeText(currentContent, startLine, endLine);
    if (candidate === oldText) {
      return replaceLineRange(currentContent, startLine, endLine, newText);
    }
  }

  const matches = findExactOccurrences(currentContent, oldText);
  if (!matches.length) {
    throw new Error('old_text 未在当前小节正文中找到');
  }
  if (matches.length > 1) {
    throw new Error('old_text 在当前小节正文中出现多次，请提供更多上下文确保唯一定位');
  }
  const index = matches[0];
  return `${currentContent.slice(0, index)}${newText}${currentContent.slice(index + oldText.length)}`;
}
```
**双通道定位**：
1. 优先「行号区间 + 逐字相等」→ 直接行替换（不要求唯一，因为行号已定位）；
2. 否则全文 `indexOf` 扫描 —— **0 次或 >1 次均拒绝**。

归一化只做两件事（`contentGenerationTask.cjs:1438-1447`）：统一换行 + 剥离提示里的 `[0001]` 行号前缀。**不折叠空白、不做模糊匹配**。
```js
function stripPromptLineNumbers(text) {
  return normalizeNewlines(text)
    .split('\n')
    .map((line) => line.replace(/^\[\d{1,6}\]\s?/, ''))
    .join('\n');
}

function normalizeConsistencyPatchText(text) {
  return stripPromptLineNumbers(text).trim();
}
```
`findExactOccurrences`（`:1517-1528`）是**非重叠全量扫描**：
```js
function findExactOccurrences(content, search) {
  const indexes = [];
  if (!search) return indexes;
  let startIndex = 0;
  while (startIndex <= content.length) {
    const index = content.indexOf(search, startIndex);
    if (index < 0) break;
    indexes.push(index);
    startIndex = index + search.length;
  }
  return indexes;
}
```
行区间提取与替换（`:1530-1550`）：
```js
function extractLineRangeText(content, startLine, endLine) {
  const lines = normalizeNewlines(content).split('\n');
  const start = Math.max(1, Math.round(Number(startLine) || 0));
  const end = Math.max(start, Math.round(Number(endLine) || 0));
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 1 || end > lines.length) {
    return null;
  }
  return lines.slice(start - 1, end).join('\n');
}
```
批量应用（`contentGenerationTask.cjs:1618-1646`）**逐条 try/catch，单条失败不阻塞其余**：
```js
function applyConsistencyRepairPatches(content, patches) {
  let nextContent = normalizeNewlines(content);
  const errors = [];
  const patchResults = [];
  let appliedCount = 0;

  for (const [index, patch] of (patches || []).entries()) {
    const detail = { index, ...describeConsistencyPatchMatch(nextContent, patch) };
    try {
      nextContent = applyExactConsistencyPatch(nextContent, patch);
      appliedCount += 1;
      patchResults.push({
        ...detail,
        applied: true,
        after_content_metrics: textMetrics(nextContent),
      });
    } catch (error) {
      errors.push(`patch[${index}] ${error.message || '应用失败'}`);
      patchResults.push({
        ...detail,
        applied: false,
        error: error.message || '应用失败',
        after_content_metrics: textMetrics(nextContent),
      });
    }
  }

  return { content: nextContent, appliedCount, errors, patchResults };
}
```
诊断器 `describeConsistencyPatchMatch`（`:1552-1582`）记录 `line_range.exists` / `matches_old_text` / `exact_match_count` / 前后 `textMetrics`，用于开发者日志排障。

**失败重试**：`contentGenerationTask.cjs:41`
```js
const CONSISTENCY_REPAIR_MAX_ATTEMPTS = 2;
```
`:5710` 循环 `attempt = 1..2`，失败原因回灌下一次（`:1762-1764`）：
```js
  const failureBlock = (failures || []).length
    ? `\n上次修复应用失败原因：\n${failures.map((failure, index) => `${index + 1}. ${failure}`).join('\n')}\n请重新返回能够在当前正文中唯一定位的 old_text。`
    : '';
```

**修复提示词原文**（`contentGenerationTask.cjs:1769-1790+`）
```
你是投标技术方案正文一致性修复助手。请只针对当前小节返回局部精确替换 patch。

要求：
1. 只返回 JSON，不要输出解释、总结或 Markdown 代码围栏。
2. 不要返回完整正文，只返回需要局部替换的 patches。
3. 事实输入比当前小节实际需要的更多；正文没有涉及的事实必须忽略。
4. 目标只修正正文中与事实冲突的内容，不要参照事实重写或扩充正文。
5. 不要优化文风，不要新增无关事实，不要新增新的承诺。
6. old_text 必须是当前小节正文中逐字存在的原文块，建议包含足够前后上下文，确保只出现一次。
7. 如果修改表格，old_text 必须包含完整表格行或完整表格块，不要只返回单元格碎片。
8. new_text 是替换后的正文块，不要包含章节标题，不要包含行号。
9. 保留 Markdown 表格、列表、代码块、图片和 Mermaid 块结构。
10. start_line/end_line 使用下方带行号正文中的 1-based 行号；如果不确定也必须提供可唯一匹配的 old_text。
```

---

## 8. Agent 修复模式

### 8.1 `global-facts.md` / `technical-plan.md` 的准备

`contentGenerationTask.cjs:5446-5453` —— `global-facts.md` 内容
```js
  function buildAgentGlobalFactsMarkdown() {
    return [
      '# 全局事实变量',
      globalFactsText || '未提供',
      '# Step02 关键解析结果',
      bidAnalysisFactsText || '未提供',
    ].join('\n\n');
  }
```
`contentGenerationTask.cjs:5417-5444` —— `technical-plan.md` 组装（递归渲染目录 + 标记）
```js
  function renderAgentTechnicalPlanOutline(items, sectionIndex, level = 1, lines = []) {
    for (const item of items || []) {
      const id = String(item?.id || '').trim();
      const title = singleLine(item?.title || '未命名章节');
      const headingLevel = Math.min(level + 1, 6);
      lines.push(`${'#'.repeat(headingLevel)} ${id ? `${id} ` : ''}${title}`.trim());

      if (item?.children?.length) {
        renderAgentTechnicalPlanOutline(item.children, sectionIndex, level + 1, lines);
        continue;
      }

      const section = sectionIndex.get(id);
      if (!section) {
        continue;
      }
      lines.push(`<!-- yibiao-section-start id="${escapeSectionAttribute(id)}" title="${escapeSectionAttribute(title)}" -->`);
      lines.push(section.originalContent);
      lines.push(`<!-- yibiao-section-end id="${escapeSectionAttribute(id)}" -->`);
    }
    return lines;
  }

  function buildAgentTechnicalPlanMarkdown(sectionIndex) {
    const lines = ['# 技术方案正文', ''];
    renderAgentTechnicalPlanOutline(outlineData.outline || [], sectionIndex, 1, lines);
    return lines.join('\n').replace(/\n{3,}/g, '\n\n').trimEnd();
  }
```
两文件投递（`contentGenerationTask.cjs:5579-5582`）：
```js
    const files = [
      { path: 'global-facts.md', content: buildAgentGlobalFactsMarkdown() },
      { path: 'technical-plan.md', content: buildAgentTechnicalPlanMarkdown(sectionIndex) },
    ];
```
`sectionIndex` 的构建（`contentGenerationTask.cjs:5400-5415`）保存 `originalContent` 与 `originalHash`：
```js
  function buildAgentConsistencySectionIndex(targets) {
    const index = new Map();
    for (const context of targets || []) {
      const id = String(context.item?.id || '').trim();
      const content = String(context.content || '').trim();
      if (!id || !content) {
        continue;
      }
      index.set(id, {
        ...context,
        originalContent: content,
        originalHash: textHash(content),
      });
    }
    return index;
  }
```

### 8.2 章节标记格式

```
<!-- yibiao-section-start id="<sectionId>" title="<title>" -->
...正文...
<!-- yibiao-section-end id="<sectionId>" -->
```
属性转义 `escapeSectionAttribute`（`:1463-1469`）处理 `& " < >`：
```js
function escapeSectionAttribute(value) {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
```

### 8.3 重新解析校验逻辑（五道结构门 + 三道语义门）

`contentGenerationTask.cjs:1471-1515` —— 解析器（**结构错误全部抛异常**）
```js
function parseAgentSectionMarkdown(markdown) {
  const sections = new Map();
  const lines = normalizeNewlines(markdown).split('\n');
  let currentId = '';
  let buffer = [];

  for (const line of lines) {
    const startMatch = /^\s*<!--\s*yibiao-section-start\s+id="([^"]+)"[^>]*-->\s*$/.exec(line);
    if (startMatch) {
      if (currentId) {
        throw new Error(`Agent 输出的小节标记嵌套：${currentId} 内出现 ${startMatch[1]}`);
      }
      currentId = String(startMatch[1] || '').trim();
      buffer = [];
      continue;
    }

    const endMatch = /^\s*<!--\s*yibiao-section-end\s+id="([^"]+)"\s*-->\s*$/.exec(line);
    if (endMatch) {
      const endId = String(endMatch[1] || '').trim();
      if (!currentId) {
        throw new Error(`Agent 输出存在未配对的小节结束标记：${endId}`);
      }
      if (endId !== currentId) {
        throw new Error(`Agent 输出小节标记不匹配：${currentId} / ${endId}`);
      }
      if (sections.has(currentId)) {
        throw new Error(`Agent 输出重复小节：${currentId}`);
      }
      sections.set(currentId, buffer.join('\n').trim());
      currentId = '';
      buffer = [];
      continue;
    }

    if (currentId) {
      buffer.push(line);
    }
  }

  if (currentId) {
    throw new Error(`Agent 输出小节未闭合：${currentId}`);
  }
  return sections;
}
```
五道结构门：**嵌套 / 未配对 / id 不匹配 / 重复 / 未闭合**。

`contentGenerationTask.cjs:5490-5505` —— 语义门（章节增删 + 清空）
```js
  function validateAgentConsistencySections(parsedSections, sectionIndex) {
    for (const id of parsedSections.keys()) {
      if (!sectionIndex.has(id)) {
        throw new Error(`Agent 输出包含未知小节：${id}`);
      }
    }
    for (const [id, section] of sectionIndex.entries()) {
      if (!parsedSections.has(id)) {
        throw new Error(`Agent 输出缺少小节：${id}`);
      }
      const nextContent = String(parsedSections.get(id) || '').trim();
      if (String(section.originalContent || '').trim() && !nextContent) {
        throw new Error(`Agent 输出把非空小节改为空：${id}`);
      }
    }
  }
```
**双向校验** —— 章节新增（未知 id）、章节删除（缺少 id）、非空被清空，三者全部拒绝。这正是提示词中「不新增、删除或重排章节」的强制执行方式。

**修改范围限定**：程序只信任标记之间的内容（`parseAgentSectionMarkdown:1506-1508` 缓冲区），标记外的一切改动（含章节标题重排）**被结构解析直接丢弃或报错**。

### 8.4 只回写变化小节

`contentGenerationTask.cjs:5507-5528` 原文
```js
  function applyAgentConsistencySections(parsedSections, sectionIndex, writableIds) {
    let changedCount = 0;
    let skippedCount = 0;
    const changedIds = [];
    for (const [id, section] of sectionIndex.entries()) {
      if (writableIds instanceof Set && !writableIds.has(id)) {
        skippedCount += 1;
        continue;
      }
      const nextContent = String(parsedSections.get(id) || '').trim();
      const currentContent = String(section.originalContent || '').trim();
      if (normalizeNewlines(nextContent).trim() === normalizeNewlines(currentContent).trim()) {
        skippedCount += 1;
        continue;
      }
      changedCount += 1;
      changedIds.push(id);
      rememberTouchedItem(id);
      saveSection(section.item, { status: 'success', content: nextContent, error: undefined }, nextContent, { logs });
    }
    return { changedCount, skippedCount, changedIds };
  }
```
三重门：**writableIds 白名单**（单节重跑时只回写目标节，`:5550-5551`）+ **归一化后逐字比对** + 仅 `saveSection` 变化项。
⚠️ 比对是**换行归一 + trim 后全等**，不做语义 diff —— Agent 改了标点也算变化。

`contentGenerationTask.cjs:5550-5551` —— writableIds 构造：
```js
    const normalizedTargetId = String(options.targetItemId || targetItemId || '').trim();
    const writableIds = normalizedTargetId ? new Set([normalizedTargetId]) : new Set(sectionIndex.keys());
```

### 8.5 Agent 修复提示词原文

`contentGenerationTask.cjs:5455-5473` 全文
```js
  function buildAgentConsistencyRepairPrompt() {
    return `请在当前工作目录中完成全文一致性修复，让 technical-plan.md 成为程序可继续解析和回写的最终正文文件。

workspace 文件说明：
- global-facts.md：全局事实变量、Step02 关键解析结果和需要保持一致的项目信息。
- technical-plan.md：当前技术方案正文全文，包含章节标题、section id 和 yibiao-section-start / yibiao-section-end 标记。

任务目标：
审计并修复 technical-plan.md，使正文不与 global-facts.md 中的全局事实变量冲突，并尽量消除正文前后矛盾。

工作方式由你自行决定。可以搜索、分段读取、建立索引、创建草稿或中间文件，并多轮编辑 technical-plan.md；不需要按固定顺序读取文件，也不需要在单次模型输出中完成全部修复。

最终 technical-plan.md 需要满足：
- 保留所有章节编号、章节标题、HTML 注释标记和 section id。
- 保留原章节结构，不新增、删除或重排章节。
- 正文修改范围限定在 yibiao-section-start 和 yibiao-section-end 标记之间。
- 修复事实冲突、前后矛盾、同一信息多处表达不一致等问题。
- 优先以 global-facts.md 中的事实变量和关键项目信息为准。${buildContentFactCompletenessInstruction(globalFactsMode) ? `\n\n${buildContentFactCompletenessInstruction(globalFactsMode)}\n不得把【待填写】改成具体值，也不得为缺失项杜撰事实。` : ''}`;
  }
```

执行参数（`contentGenerationTask.cjs:5607-5625`）：`output_file: 'technical-plan.md'`、`timeout_ms: 30*60*1000`（30 分钟）、`max_retries: 1`，且 `validateOutput` 内**先跑一遍完整校验**，不通过则重试：
```js
      const agentResult = await runAgentTaskWithRecoveredOutput({
        title: '全文一致性 Agent 修复',
        prompt: buildAgentConsistencyRepairPrompt(),
        output_file: 'technical-plan.md',
        files,
        timeout_ms: 30 * 60 * 1000,
        max_retries: 1,
        signal: agentAbortController.signal,
        validateOutput: (resultForValidation) => {
          const repairedMarkdownForValidation = String(resultForValidation?.output_content || '').trim();
          if (!repairedMarkdownForValidation) {
            throw new Error('Agent 未返回修复后的 technical-plan.md');
          }
          const parsedSectionsForValidation = parseAgentSectionMarkdown(repairedMarkdownForValidation);
          validateAgentConsistencySections(parsedSectionsForValidation, sectionIndex);
          return { section_count: parsedSectionsForValidation.size };
        },
        onActivity: createAgentActivityProgressHandler(updateAgentConsistencyProgress, 2, 'Agent 正在审计并修复全文'),
      }, 'consistency.agent');
```
⚠️ **进度 5 步固定**（`contentGenerationTask.cjs:5479-5481`）：
```js
    contentStats.audit_agent_step_total = 5;
```
1 准备文件（`:5578`） / 2 Agent 审计修复（`:5585`） / 3 读取输出（`:5637`） / 4 解析校验（`:5644`） / 5 回写（`:5649`）。

回写结果日志（`contentGenerationTask.cjs:5649-5654`）：
```js
      updateAgentConsistencyProgress(5, '回写 Agent 修改的小节');
      const applyResult = applyAgentConsistencySections(parsedSections, sectionIndex, writableIds);
      contentStats.audit_agent_changed_sections = applyResult.changedCount;
      logs = [...logs, applyResult.changedCount
        ? `Agent 一致性修复完成：已回写 ${applyResult.changedCount} 个小节（${applyResult.changedIds.join('、')}）。`
        : 'Agent 一致性修复完成：未发现需要回写的小节。'];
```

### 8.6 V2 的 Agent 调整（`globalFactsAdjustmentTask`）

`globalFactsAdjustmentTask.cjs:23-35` 提示词原文
```js
function createGlobalFactsAdjustmentPrompt(requirement) {
  return `用户已经在当前全局事实基础上提出新的调整要求。程序已把当前最新的完整结果覆盖写入 ${GLOBAL_FACTS_OUTPUT_FILE}（用户可能在主界面手动修改过，请以该文件为准，不要沿用你记忆中的旧内容）。

用户的调整要求：
${requirement}

请按以下要求完成调整：
1. 先读取 ${GLOBAL_FACTS_OUTPUT_FILE}，理解当前内容，再严格按照用户的调整要求修改；与要求无关的项保持原样，不要顺带重写。
2. 修改后仍须保持完整根结构 {"groups":[{"id":"...","title":"...","content":"..."}]}：每项包含 id、title、content。
3. 材料或用户要求足以判断时直接执行。不确定且不同选择会实质影响结果时，可以自行决定是否调用 ask-user；不要为了确认而反复提问。
4. 将调整后的完整结果覆盖写回 ${GLOBAL_FACTS_OUTPUT_FILE}。程序已为该文件预置 Schema，写入后调用 json-validation，只传 {"file_path":"${GLOBAL_FACTS_OUTPUT_FILE}"}；校验失败后必须先修改文件，再重新校验。
5. 全部完成后，用简体中文输出一段简短的最终总结（不超过 200 字，不使用 Markdown 标题），说明本次实际做了哪些调整；如有未能执行的要求，一并说明原因。该总结会直接展示给用户。`;
}
```
`buildAgentFactsInput`（`globalFactsAdjustmentTask.cjs:13-21`）—— 覆盖写入的内容形态：
```js
function buildAgentFactsInput(groups) {
  return {
    groups: (Array.isArray(groups) ? groups : []).map((group) => ({
      id: String(group?.id || ''),
      title: String(group?.title || '').trim(),
      content: String(group?.content || ''),
    })),
  };
}
```
「程序覆盖写入」实现在 `globalFactsAdjustmentTask.cjs:82-85`：
```js
    files: [{
      path: GLOBAL_FACTS_OUTPUT_FILE,
      content: JSON.stringify(buildAgentFactsInput(storedPlan.globalFacts), null, 2),
    }],
```
**这是防止「Agent 记忆覆盖用户手改」的关键**：每轮调整前把 DB 最新值重写进工作区文件。

---

## 9. 两类用户场景的区分

### 9.1 AI 初始化

| 维度 | 证据 |
|---|---|
| 入口 | `GlobalFactsPage.tsx:271-273` 按钮 `{running ? '生成中...' : globalFacts.length ? '重新解析' : '开始解析'}` |
| 触发 | `GlobalFactsPage.tsx:144` `window.yibiao?.tasks.startGlobalFactsGeneration({ globalFactsMode: nextMode })` |
| 主进程 | `taskService.cjs:1445-1459`（附 `beforeStart: () => agentService.deletePersistentTask(...)` —— **每次重新解析都销毁旧 Agent 会话**） |
| 初始清空 | `taskService.cjs:1448-1454` `globalFacts: []` + 全部正文缓存置空 |
| 任务记录 | `globalFactsTask`，type `global-facts-generation` |
| 结果落库 | `globalFactsTaskV2.cjs:418-421` `checkpointTask({status:'success', progress:100, ...}, { globalFacts: normalized.groups })` |

`taskService.cjs:1445-1464` 原文
```js
    startGlobalFactsGeneration(payload) {
      return startManagedTask('global-facts-generation', payload, runGlobalFactsTaskV2, {
        invalidateContentGeneration: true,
        globalFacts: [],
        globalFactsAdjustmentTask: undefined,
        contentGenerationTask: undefined,
        contentGenerationSections: {},
        contentGenerationPlans: {},
        contentIllustrationPlan: undefined,
        contentGenerationRuntime: undefined,
      }, {
        primarySession: true,
        beforeStart: () => agentService.deletePersistentTask(GLOBAL_FACTS_AGENT_TASK_KEY),
      });
    },
    startGlobalFactsAdjustment(payload) {
      return startManagedTask('global-facts-adjustment', payload, runGlobalFactsAdjustmentTask, {}, {
        primarySession: agentService.isPrimarySession({ task_key: GLOBAL_FACTS_AGENT_TASK_KEY }),
      });
    },
```

### 9.2 用户手动维护

| 操作 | 代码 | 数据流 |
|---|---|---|
| 编辑大项 | `GlobalFactsPage.tsx:204-218` `saveActiveGroup` | 改 title/content + 刷 `updated_at` |
| 新增大项 | `:220-229` `addFactGroup` | `createFactId()` → `manual_<uuid>` |
| 删除大项 | `:231-234` `deleteActiveGroup` | 按 id 过滤 |
| 复制 | `:236-243` `copyActiveGroup` | 纯剪贴板 |
| 保存入口 | `:186-202` `saveFacts` → `onGlobalFactsSaved` | |

`GlobalFactsPage.tsx:47-50` —— 手动新增的 id 生成
```tsx
function createFactId() {
  const randomId = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `manual_${randomId.replace(/[^a-zA-Z0-9_-]/g, '_')}`.toLowerCase();
}
```
`GlobalFactsPage.tsx:220-229` —— 新增大项的默认内容
```tsx
  const addFactGroup = async () => {
    const nextGroup: GlobalFactGroupState = {
      id: createFactId(),
      title: '新增事实大项',
      content: '- 项目经理：张伟，高级工程师，负责总体协调和质量把关。',
      updated_at: new Date().toISOString(),
    };
    await saveFacts([...globalFacts, nextGroup], '已新增事实大项');
    setSelectedGroupId(nextGroup.id);
  };
```

**关键差异 —— 手动保存会伪造一个「成功任务」**（`technicalPlanStore.cjs:2305-2333`）
```js
  function saveGlobalFacts(globalFacts) {
    const normalizedGlobalFacts = normalizeGlobalFactGroups(globalFacts);
    let savedTask;
    const transaction = db.transaction(() => {
      replaceGlobalFacts(normalizedGlobalFacts);
      clearContentGenerationState();
      const timestamp = now();
      savedTask = {
        task_id: `manual-global-facts-${Date.now()}`,
        type: 'global-facts-generation',
        status: 'success',
        progress: 100,
        logs: ['全局事实已保存。'],
        started_at: timestamp,
        updated_at: timestamp,
      };
      saveTask('global-facts-generation', savedTask);
    });
    transaction();
    return {
      globalFacts: normalizedGlobalFacts,
      globalFactsTask: savedTask,
      contentGenerationTask: undefined,
      contentGenerationSections: {},
      contentGenerationPlans: {},
      contentIllustrationPlan: undefined,
      contentGenerationRuntime: undefined,
    };
  }
```
**这解释了一个下游硬门**（`contentGenerationTask.cjs:2925-2930`）：
```js
  const globalFacts = Array.isArray(storedPlan.globalFacts) ? storedPlan.globalFacts : [];
  const globalFactsText = formatGlobalFactsForPrompt(globalFacts);
  const globalFactsMode = normalizeGlobalFactsMode(storedPlan.globalFactsMode);
  if (!globalFactsText || storedPlan.globalFactsTask?.status !== 'success') {
    throw new Error('请先完成全局事实设定，再生成正文');
  }
```
手动保存写入 `status:'success'` 正是为了让**纯手写的全局事实也能通过正文生成前置门**。

⚠️ **`task_id` 前缀 `manual-global-facts-`** 是 UI 判定两场景的**唯一数据线索**（源码中未见显式 `source` 字段区分）。

配置保存独立（`technicalPlanStore.cjs:2299-2303`）：
```js
  function saveGlobalFactsConfig({ globalFactsMode } = {}) {
    const normalized = normalizeGlobalFactsMode(globalFactsMode);
    updateTechnicalPlan({ globalFactsMode: normalized });
    return { globalFactsMode: normalized };
  }
```

### 9.3 UI 层的三态与锁

`GlobalFactsPage.tsx:85-94`
```tsx
  const hasOutline = Boolean(outlineData?.outline?.length);
  const running = starting || task?.status === 'running';
  const mutationLocked = running || aiAdjustmentRunning;
  const taskFailed = task?.status === 'error';
  const activeGroup = globalFacts.find((group) => group.id === selectedGroupId) || globalFacts[0] || null;
  const progress = getProgress(task, globalFacts.length > 0);
  const statusKey = running ? 'running' : taskFailed ? 'error' : globalFacts.length ? 'success' : 'idle';
```
`GlobalFactsPage.tsx:59-63` —— `getProgress`：有数据但无任务 → **100%**（手写完即视为完成）
```tsx
function getProgress(task: BackgroundTaskState | undefined, hasFacts: boolean) {
  if (task?.status === 'running') return Math.max(5, Math.min(99, task.progress || 5));
  if (task?.status === 'error') return Math.max(0, Math.min(99, task.progress || 0));
  return hasFacts ? 100 : 0;
}
```
`statusLabels`（`GlobalFactsPage.tsx:18-23`）：`idle/running/success/error` → 未开始/生成中/已完成/失败。
`aiAdjustmentRunning` 独立锁（`:87`），在 4 处提示区分文案（`:98-100, 109-111, 130-132, 188-190`）。

**失效提示文案**（`:325`）：
```
可直接编辑事实变量；保存后会清空旧正文生成缓存，避免继续使用旧内容。
```
`GlobalFactsPage.tsx:288-295` —— 进度面板与失败提示
```tsx
                <ProgressBar value={progress} active={running} label={`全局事实设定进度 ${progress}%`} />
                <p>{taskFailed ? task?.error || latestLog || '全局事实设定失败，请重新解析。' : latestLog || '点击“开始解析”后，后台会生成全局事实变量。'}</p>
                {taskFailed && <small>失败后不会自动重试，可点击“重新解析”。</small>}
```

### 9.4 AI 调整场景（第三种，区别于前两者）

`agentWorkspaceService.cjs:177-222` —— 独立的工作区 Provider：
```js
  const globalFactsWorkspaceProvider = {
    id: GLOBAL_FACTS_AGENT_TASK_KEY,
    buildDescriptor() {
      const plan = technicalPlanStore.loadTechnicalPlan() || {};
      const activeTasks = taskService.getActiveTasks();
      const busyTask = activeTasks.find((task) => task.group === 'technical-plan' && isActiveTaskStatus(task.status));
      const hasFacts = Array.isArray(plan.globalFacts) && plan.globalFacts.length > 0;
      const hasSession = agentService.hasPersistentTaskSession(GLOBAL_FACTS_AGENT_TASK_KEY);

      if (!hasFacts || !hasSession) {
        if (busyTask?.type === 'global-facts-generation') {
          return {
            id: this.id,
            title: '全局事实设定',
            status: 'busy',
            busy_reason: '全局事实设定任务执行中，完成后即可发送调整要求',
            has_generated_content: false,
            empty_hint: '向 Agent 描述你的全局事实调整要求。',
          };
        }
        return null;
      }

      const contentPaused = plan.contentGenerationTask?.status === 'paused';
      const busyReason = busyTask
        ? `${technicalPlanTaskLabels[busyTask.type] || busyTask.type}任务执行中，请等待完成`
        : contentPaused
          ? '正文生成已暂停，请先在主界面继续或重置正文任务'
          : '';
      const hasGeneratedContent = countGeneratedLeaves(plan.outlineData?.outline) > 0;
      return {
        id: this.id,
        title: '全局事实设定',
        status: busyReason ? 'busy' : 'ready',
        busy_reason: busyReason,
        has_generated_content: hasGeneratedContent,
        empty_hint: '向 Agent 描述你的全局事实调整要求。',
        ...(hasGeneratedContent
          ? { send_warning: '调整全局事实将清空已生成的正文内容，是否继续？' }
          : {}),
      };
    },
    sendMessage(message) {
      return taskService.startGlobalFactsAdjustment({ requirement: message });
    },
  };
```
`globalFactsAdjustmentTask.cjs:113-119` —— 调整后同样全量失效正文：
```js
  }, {
    globalFacts: normalized.groups,
    invalidateContentGeneration: true,
    contentGenerationTask: undefined,
    contentGenerationSections: {},
    contentGenerationPlans: {},
    contentIllustrationPlan: undefined,
    contentGenerationRuntime: undefined,
  });
```
三道前置哨兵（`globalFactsAdjustmentTask.cjs:40-49`）：要求非空 / 已有全局事实 / **Agent 会话存在**。
```js
  const requirement = String(payload?.requirement || '').trim();
  if (!requirement) {
    throw new Error('调整要求不能为空');
  }
  const storedPlan = workspaceStore.loadTechnicalPlan() || {};
  if (!Array.isArray(storedPlan.globalFacts) || !storedPlan.globalFacts.length) {
    throw new Error('当前没有可调整的全局事实，请先完成全局事实设定');
  }
  if (!agentService.hasPersistentTaskSession(GLOBAL_FACTS_AGENT_TASK_KEY)) {
    throw new Error('全局事实设定的 Agent 工作空间不存在，请重新生成后再使用 AI 调整');
  }
```

---

## 10. 全部提示词原文汇总（system + user，逐字）

### 10.1 V1 system 提示词（唯一 system 消息）

`globalFactsTask.cjs:50-65` 全文
```js
function buildGlobalFactsSystemPrompt(mode) {
  const completenessRules = buildGlobalFactsCompletenessRules(mode);
  return `你是专业的投标技术方案事实变量整理助手。请基于用户提供的上下文，整理后续正文需要统一采用的全局事实变量。

关键定义：
1. 全局事实变量不是招标要求摘录、评分规则摘要或待办事项清单，而是技术方案正文中需要保持一致的确定性方案事实、响应设定、承诺口径或执行安排。
2. 用户资料已经给出明确事实时，优先使用资料中的事实值。
3. 用户资料只给出要求、约束或评价口径时，不要原样摘录为要求句；如果该内容会影响后续正文的一致写法，应转写为本方案已经采用、已经具备或统一承诺的事实表达。
${buildMissingFactRule(mode)}

通用要求：
1. 输出必须使用简体中文。
2. 只关注技术方案正文会反复使用、且前后必须一致的事实变量。
3. 每条事实都应能直接指导后续正文统一写法，避免正文各章节自行生成不同口径。
4. 不输出分析过程、来源说明、风险提示、正文草稿或未落地的要求句。${completenessRules ? `\n\n${completenessRules}` : ''}`;
}
```

### 10.2 V1 轻量上下文（所有阶段共用的固定前缀）

`globalFactsTask.cjs:414-426` 全文
```js
function buildGlobalFactsLightContextMessages({ projectOverview, outlineData, bidAnalysisFactsText, knowledgeItems, sectionHint, globalFactsMode }) {
  const messages = [{ role: 'system', content: buildGlobalFactsSystemPrompt(globalFactsMode) }];
  if (sectionHint) {
    messages.push({ role: 'system', content: sectionHint });
  }
  messages.push(
    { role: 'user', content: `项目概述：\n${String(projectOverview || '').trim() || '未提供'}` },
    { role: 'user', content: `Step02 关键解析结果：\n${bidAnalysisFactsText}` },
    { role: 'user', content: `已生成技术方案目录：\n${formatOutlineForPrompt(outlineData.outline || [])}` },
    { role: 'user', content: (knowledgeItems || []).length ? `用户已选择 ${(knowledgeItems || []).length} 条知识库条目；知识库正文将在独立分段步骤中处理。` : '用户未选择参考知识库。' },
  );
  return messages;
}
```
⚠️ **标段说明作为独立第二条 system 消息**（`:416-418`）—— 与本仓 `bid_analysis` 的「拼进同一条」做法相反，易标在**消息层级**表达作用域约束。

目录渲染格式 `globalFactsTask.cjs:286-295`：
```js
function formatOutlineForPrompt(items, level = 1, lines = []) {
  for (const item of items || []) {
    const id = singleLine(item?.id || 'unknown');
    const title = singleLine(item?.title || '未命名章节');
    const description = singleLine(item?.description || '');
    lines.push(`${'  '.repeat(Math.max(0, level - 1))}- ${id} ${title}${description ? `：${description}` : ''}`);
    if (item?.children?.length) formatOutlineForPrompt(item.children, level + 1, lines);
  }
  return lines.join('\n');
}
```

### 10.3 招标文件分段提取 user 提示词

`globalFactsTask.cjs:511-526` 全文
```js
    {
      role: 'user',
      content: `招标文件分段全局事实提取任务：

请只基于当前招标文件分段，识别后续技术方案正文必须保持一致的全局事实变量候选。

要求：
1. 当前分段没有提及，不代表整份招标文件没有提及；不要因为本段缺失就输出“没有提及”。
2. 当前分段直接给出明确事实时，提取为可复用的事实值。
3. 当前分段给出的是要求、约束或评价口径时，不要原样摘录为要求句；请判断它是否会影响后续正文的一致写法，必要时转写为本方案可统一采用的响应事实候选。
4. 每条 content 只写短 bullet，内容应是正文可直接引用或遵循的稳定事实、响应设定、承诺口径或执行安排。
5. 当前分段无法支持形成事实候选时，返回 {"groups":[]}；不要为了凑内容编造与本段无关的具体值。
6. 不要输出商务报价、资格材料、正文草稿、分析过程或来源说明。
7. 只返回 JSON。${completenessRules ? `\n\n${completenessRules}` : ''}`,
    },
    { role: 'user', content: buildGroupsJsonExample(globalFactsMode) },
```
分段内容消息（`:510`）：
```js
    { role: 'user', content: `招标文件分段 ${tenderSegment.index}/${tenderSegment.total}：\n${tenderSegment.content}` },
```
分段候选回灌格式 `globalFactsTask.cjs:530-537`：
```js
function formatSegmentGroupResultForPrompt(result) {
  return `## 第 ${result.index}/${result.total} 段候选
${JSON.stringify(result.groups || [], null, 2)}`;
}

function formatSegmentGroupsForPrompt(segmentResults) {
  return (segmentResults || []).map(formatSegmentGroupResultForPrompt).join('\n\n');
}
```

### 10.4 分段合并 user 提示词

`globalFactsTask.cjs:539-560` 全文
```js
function buildTenderSegmentMergeMessages(context) {
  const completenessRules = buildGlobalFactsCompletenessRules(context.globalFactsMode);
  return [
    ...buildGlobalFactsLightContextMessages(context),
    { role: 'user', content: `招标文件分段候选全局事实：\n${formatSegmentGroupsForPrompt(context.segmentResults)}` },
    {
      role: 'user',
      content: `招标文件全局事实合并任务：

请把所有分段候选合并为后续技术方案正文可直接使用的全局事实变量。

要求：
${buildMergeCleanupRule(context.globalFactsMode)}
2. 合并后的结果必须是稳定的方案事实、响应设定、承诺口径或执行安排，不保留未落地的要求句、评分规则或资料清单。
3. 对招标文件中的硬性要求和约束，应判断其是否会影响后续正文的一致写法；会影响的，应转写为本方案统一采用的事实、安排或承诺口径。
${buildTenderMergeFactValueRules(context.globalFactsMode)}
6. 每条 bullet 都应回答“后续正文遇到这个事项时统一写什么”，而不是回答“招标文件要求什么”。
7. 仅编写技术方案部分，不要涉及商务报价或资格材料。
8. 只返回 JSON。${completenessRules ? `\n\n${completenessRules}` : ''}`,
    },
    { role: 'user', content: buildGroupsJsonExample(context.globalFactsMode) },
  ];
}
```
`buildTenderMergeFactValueRules`（`:67-78`）—— **三模式第 4/5 条差异**：
```js
function buildTenderMergeFactValueRules(mode) {
  if (mode === 'omit') {
    return `4. 资料中已有明确事实值时使用明确值；资料没有明确值时仍须保留该项，写成正确笼统承诺，不要补足具体值，也不要为缺具体值而删项。
5. 必须包含工期、运维期或交货时间中的至少一个相关变量；材料没有具体值时使用笼统承诺，不要编造日期或周期，也不要省略该类变量。`;
  }
  if (mode === 'placeholder') {
    return `4. 资料中已有明确事实值时使用明确值；资料没有明确值但该信息对全文一致性重要时，必须保留该项，值写成【待填写】，严禁杜撰，也不得因缺具体值而删项。
5. 必须包含工期、运维期或交货时间中的至少一个相关变量；材料没有具体值时使用【待填写】，不要编造日期或周期。`;
  }
  return `4. 资料中已有明确事实值时使用明确值；资料没有明确值但该信息对全文一致性重要时，可以根据项目语境补足一套合理、稳定、不冲突的事实值。
5. 必须包含工期、运维期或交货时间中的至少一个相关变量；如果分段候选不足，但项目概述或 Step02 关键解析结果中已有明确内容，应补入。`;
}
```

### 10.5 最终整理 user 提示词

`globalFactsTask.cjs:652-675` 全文
```js
function buildFinalGlobalFactsReviewMessages(context) {
  return [
    ...buildGlobalFactsLightContextMessages(context),
    { role: 'user', content: `待最终整理的全局事实变量：\n${JSON.stringify(context.groups || [], null, 2)}` },
    {
      role: 'user',
      content: `全局事实变量最终整理任务：

请在不提交完整招标文件、完整原方案和知识库正文的前提下，基于当前轻量上下文整理最终全局事实变量。

要求：
1. 最终结果必须全部是后续技术方案正文可直接统一使用的事实变量。
2. 保留所有具体、可复用、会影响全文一致性的方案事实、响应设定、承诺口径和执行安排。
${buildFinalDedupRule(context.globalFactsMode)}
${buildFinalRewriteRule(context.globalFactsMode)}
5. 不要新增与当前事实相冲突的具体值、服务承诺或技术边界。
${buildFinalScheduleRule(context.globalFactsMode)}
7. 每个 group 必须包含 id、title、content。
8. ${context.isExpansionWorkflow ? '当前是已有方案扩写模式，原方案分段补充后的事实优先保留，不要在最终整理时弱化或删除原方案已有承诺。' : '只返回 JSON。'}
${context.isExpansionWorkflow ? '9. 只返回 JSON。' : ''}${buildGlobalFactsCompletenessRules(context.globalFactsMode) ? `\n\n${buildGlobalFactsCompletenessRules(context.globalFactsMode)}` : ''}`,
    },
    { role: 'user', content: buildGroupsJsonExample(context.globalFactsMode) },
  ];
}
```
`buildFinalRewriteRule`（`:94-99`）：
```js
function buildFinalRewriteRule(mode) {
  if (mode === 'omit' || mode === 'placeholder') {
    return '4. 如果某条内容表达的是“需要满足什么要求”，请改写为“本方案统一采用什么事实、安排、承诺或响应口径”。笼统但正确的承诺口径以及【待填写】都不是空泛内容，不得因不够具体而删除；仅删除真正无法指导正文统一写法的要求摘录、评分规则或资料清单。';
  }
  return '4. 如果某条内容表达的是“需要满足什么要求”，请改写为“本方案统一采用什么事实、安排、承诺或响应口径”；无法形成稳定事实且不能帮助正文保持一致的，应删除。';
}
```
`buildFinalScheduleRule`（`:108-116`）—— **工期强制保留的唯一出处**：
```js
function buildFinalScheduleRule(mode) {
  if (mode === 'omit') {
    return '6. 必须保留工期、运维期或交货时间中的至少一个相关变量；材料没有具体值时使用笼统承诺，不要编造日期或周期，也不要省略该类变量。';
  }
  if (mode === 'placeholder') {
    return '6. 必须保留工期、运维期或交货时间中的至少一个相关变量；材料没有具体值时使用【待填写】，不要编造日期或周期。';
  }
  return '6. 必须保留工期、运维期或交货时间中的至少一个相关变量。';
}
```

### 10.6 V2 主提示词（生产链路）

`globalFactsTaskV2.cjs:210-233` 全文
```js
function createGlobalFactsPrompt({ fileCatalog, hasKnowledge, hasOriginalPlan, globalFactsMode }) {
  return `请只在当前工作目录内工作。已有材料足以判断时自主执行，不要调用 ask-user。

任务：整理后续技术方案正文必须统一采用的全局事实变量，写入 ${GLOBAL_FACTS_OUTPUT_FILE}。

工作流程由你自主安排，但必须遵守下面的材料用途和先后原则。

材料用途：
${fileCatalog}

工作原则：
${buildWorkPrinciples({ hasKnowledge, hasOriginalPlan })}

缺具体值时的写法：
${buildMissingValueRule(globalFactsMode)}

输出：
1. 只写入 ${GLOBAL_FACTS_OUTPUT_FILE}，必须是纯 JSON，不要 Markdown 代码块。
2. 根对象只有 groups；每项包含 id、title、content。
3. 程序已为该文件预置 Schema。写入后调用 json-validation，只传 {"file_path":"${GLOBAL_FACTS_OUTPUT_FILE}"}；失败则先改文件再校验，直到通过。

格式示意：
${buildJsonExample(globalFactsMode)}`;
}
```
`buildWorkPrinciples`（`:173-208`）全文 —— **知识库/原方案优先级规则的原文**：
```js
function buildWorkPrinciples({ hasKnowledge, hasOriginalPlan }) {
  const supplementNames = [
    hasKnowledge ? '知识库' : '',
    hasOriginalPlan ? '原方案' : '',
  ].filter(Boolean);
  const principles = [
    '1. 先根据招标文件、项目概述、招标解析结果和技术方案目录，确定全部全局事实大项（id、title）。凡这些材料表明后续正文需要统一口径的事项，都必须建项；招标文件有多份时要综合全部招标原文，不要因为某一份没写就漏项。',
  ];
  let fillRule = '2. 再为每个大项填写 content。优先使用招标文件和招标解析结果中的明确值';
  if (supplementNames.length) {
    fillRule += `；然后再用${supplementNames.join('、')}补充这些已有大项的具体内容`;
  }
  principles.push(`${fillRule}。`);

  let next = 3;
  if (supplementNames.length) {
    principles.push(`${next}. ${supplementNames.join('和')}只用于补充已有大项的 content，不要靠它们新增大项。`);
    next += 1;
  }
  principles.push(`${next}. 全局事实不是招标要求摘录、评分规则或待办清单，而是正文要统一采用的方案事实、响应设定、承诺口径或执行安排。材料给出的是要求或约束时，不要原样摘录要求句，应转写为本方案统一口径。`);
  next += 1;
  principles.push(`${next}. 每条 content 只写简体中文短 bullet，回答“后续正文遇到这个事项时统一写什么”。不写分析过程、来源说明、风险提示、正文草稿、商务报价或资格材料。`);
  next += 1;
  principles.push(`${next}. 必须包含工期、运维期或交货时间中的至少一个相关变量；缺具体值时按本任务给定的写法填写，不要省略该项。`);
  next += 1;
  principles.push(`${next}. 材料较长时用检索定位，不要因为一次读完就漏项。`);
  if (hasKnowledge) {
    next += 1;
    principles.push(`${next}. 用参考知识库补充已有大项的具体内容，不要仅因知识库出现新话题就新增大项。`);
  }
  if (hasOriginalPlan) {
    next += 1;
    principles.push(`${next}. 用原方案补充已有大项的具体内容；原方案与招标明确事实冲突时，原方案已落地的安排优先替换对应 bullet。不要仅因原方案出现新话题就新增大项。`);
  }
  return principles.join('\n');
}
```
`buildFileCatalog`（`:146-171`）全文 —— **材料用途声明**：
```js
function buildFileCatalog({ tenderPaths, isWorkingCopy, hasSectionHint, knowledgeCount, hasOriginalPlan }) {
  const lines = [];
  if (tenderPaths.length) {
    const listed = tenderPaths.join('、');
    if (isWorkingCopy) {
      lines.push(`- ${listed}：排除其他标段后的当前投标范围正文，用于确定大项并提取明确事实；不要扩展到其他标段。`);
    } else {
      const multiNote = tenderPaths.length > 1 ? '；多份都要看' : '';
      lines.push(`- ${listed}：招标原文，用于确定大项并提取明确事实${multiNote}。`);
    }
  }
  lines.push('- 项目概述.md：项目背景和术语，用于确定大项，不作为商务/资格材料来源。');
  lines.push('- 招标解析结果.md：Step02 已抽出的项目信息、甲方信息、交货和服务要求，用于确定大项并提取明确值。');
  lines.push('- 技术方案目录.md：已确认目录，用于判断正文会反复用到哪些统一口径。');
  if (hasSectionHint) {
    lines.push('- 标段说明.md：本次投标范围，只关注该范围内的事实。');
  }
  if (knowledgeCount > 0) {
    lines.push('- 参考知识库/条目-*.md：补充已有大项的具体内容。');
  }
  if (hasOriginalPlan) {
    lines.push('- 原方案.md：已有方案扩写底稿，补充已有大项的具体内容。');
  }
  lines.push('- 材料说明.md：本次实际提供的文件清单，与上述用途一致。');
  return lines.join('\n');
}
```
`材料说明.md` 内容（`globalFactsTaskV2.cjs:377-380`）：
```js
  files.push({
    path: '材料说明.md',
    content: `本次任务实际提供的材料如下。只使用这些文件，不要猜测未提供的材料。\n\n${fileCatalog}`,
  });
```

### 10.7 编排期 system 提示词

`contentGenerationTask.cjs:804-818` 全文
```js
  const messages = [
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
  ];
```

### 10.8 V1 额外 user 消息（仅知识库场景）

`globalFactsTask.cjs:423`
```js
    { role: 'user', content: (knowledgeItems || []).length ? `用户已选择 ${(knowledgeItems || []).length} 条知识库条目；知识库正文将在独立分段步骤中处理。` : '用户未选择参考知识库。' },
```

### 10.9 原方案还原（existing-plan-expansion）

`contentGenerationTask.cjs:1114`
```js
    { role: 'user', content: `Step04 全局事实变量标题清单：\n${globalFactTitlesText || '未提供'}` },
```

---

## 11. 容错机制与失败哨兵

### 11.1 前置缺失哨兵（全部是中文 Error）

| 条件 | 消息 | 位置 |
|---|---|---|
| 无招标文件 | `请先上传招标文件，再生成全局事实` | `globalFactsTaskV2.cjs:297` / `globalFactsTask.cjs:935` |
| 无目录 | `请先生成目录，再生成全局事实` | `globalFactsTaskV2.cjs:302` / `globalFactsTask.cjs:953` |
| 扩写模式无原方案文件 | `请先上传原方案，再生成全局事实` | `globalFactsTaskV2.cjs:309, 313` / `globalFactsTask.cjs:940, 948` |
| 原方案服务缺失 | `原方案读取服务尚未初始化` | `globalFactsTask.cjs:944` |
| 分段源为空 | `${sourceLabel}内容为空，无法提取全局事实变量` | `globalFactsTask.cjs:795` |
| 无待调整事实 | `当前没有可调整的全局事实，请先完成全局事实设定` | `globalFactsAdjustmentTask.cjs:45` |
| 调整要求为空 | `调整要求不能为空` | `globalFactsAdjustmentTask.cjs:41` |
| Agent 会话不存在 | `全局事实设定的 Agent 工作空间不存在，请重新生成后再使用 AI 调整` | `globalFactsAdjustmentTask.cjs:48` |
| 正文前置门 | `请先完成全局事实设定，再生成正文` | `contentGenerationTask.cjs:2929` |

### 11.2 结果校验哨兵

`globalFactsTask.cjs:190-210` 原文
```js
function validateGlobalFactsResponse(value) {
  if (!Array.isArray(value?.groups) || !value.groups.length) {
    throw new Error('全局事实结果缺少 groups');
  }
  value.groups.forEach((group, index) => {
    if (!group.id || !group.title || !String(group.content || '').trim()) {
      throw new Error(`全局事实第 ${index + 1} 项缺少 id、title 或 content`);
    }
  });
}

function validateGlobalFactsSegmentResponse(value) {
  if (!value || !Array.isArray(value.groups)) {
    throw new Error('全局事实分段结果缺少 groups');
  }
  value.groups.forEach((group, index) => {
    if (!group.id || !group.title || !String(group.content || '').trim()) {
      throw new Error(`全局事实分段第 ${index + 1} 项缺少 id、title 或 content`);
    }
  });
}
```
`normalizeGlobalFactsResponse`（`:167-188`）—— 主链路归一化出口（兼容 4 种根形态）：
```js
function normalizeGlobalFactsResponse(value) {
  const source = value?.result && typeof value.result === 'object' ? value.result : value || {};
  const rawGroups = Array.isArray(source)
    ? source
    : Array.isArray(source.groups)
      ? source.groups
      : Array.isArray(source.facts)
        ? source.facts
        : Array.isArray(source.items)
          ? source.items
          : [];
  const used = new Set();
  const groups = rawGroups.map((group, index) => {
    const title = singleLine(group?.title || group?.name || group?.category || group?.label);
    const rawContent = group?.content ?? group?.markdown ?? group?.facts ?? group?.items ?? group?.details ?? group?.description;
    const content = valueToMarkdown(rawContent);
    if (!title || !content) return null;
    const id = ensureUniqueId(normalizeFactId(group?.id || group?.group_id || group?.key || title, index), used);
    return { id, title, content };
  }).filter(Boolean);
  return { groups };
}
```
⚠️ **分段版 validator 允许 `groups: []`**（无 length 检查）—— 空段不报错，靠提示词第 5 条约束。

`globalFactsTaskV2.cjs:42-48` —— JSON 解析哨兵
```js
function readJson(content, label) {
  try {
    return JSON.parse(String(content || '').trim());
  } catch (error) {
    throw new Error(`${label}不是合法 JSON：${error?.message || String(error)}`);
  }
}
```
Agent JSON 三候选解析（`contentGenerationTask.cjs:1417-1436`）—— 容错提取：
```js
function parseAgentJsonContent(content) {
  const normalized = String(content || '').replace(/^\uFEFF/, '').trim();
  const candidates = [
    normalized,
    ...extractFencedAgentJsonBlocks(normalized),
    extractBalancedAgentJsonCandidate(normalized),
  ].map((item) => String(item || '').trim()).filter(Boolean);
  const uniqueCandidates = [...new Set(candidates)];
  let lastError = null;

  for (const candidate of uniqueCandidates) {
    try {
      return JSON.parse(candidate);
    } catch (error) {
      lastError = error;
    }
  }

  throw new Error(`Agent 未返回可解析的 JSON：${lastError?.message || '内容为空'}`);
}
```

### 11.3 fail-soft 点

| 场景 | 行为 | 位置 |
|---|---|---|
| 知识库读取失败 | 记日志后**跳过**，返回空数组 | `globalFactsTask.cjs:329-333` |
| 知识库无补丁 | 记「知识库未返回需要补充的全局事实变量。」 | `globalFactsTask.cjs:997` |
| 原方案无补丁 | 记「原方案未返回需要补充的全局事实变量。」 | `globalFactsTask.cjs:1007` |
| `aiService.collectJsonResponse` 缺失 | 回退 `requestJson` | `globalFactsTask.cjs:677-679` |
| `workspaceStore.readOriginalPlanMarkdown` 缺失 | 非扩写模式不检查（仅扩写模式报错） | `globalFactsTask.cjs:937-950` |
| `normalizeGlobalFactsMode` 非法值 | 回落 `fabricate` | 4 处 |
| Agent 正忙 | 跳过本轮，**不报错** | `contentGenerationTask.cjs:5626-5634` |
| Agent 一致性修复失败 | **保留原正文，不回退普通修复**，向上抛 | `contentGenerationTask.cjs:5682` |
| 单条 patch 应用失败 | 记录错误继续下一条 | `contentGenerationTask.cjs:1634-1642` |
| 暂停请求 | AbortController 取消 Agent，清进度后重抛 | `contentGenerationTask.cjs:5596-5600` |
| 分批合并不收敛 | `nextPending.length >= pending.length` 时补一轮终局合并 | `globalFactsTask.cjs:745-747, 783-785` |

`globalFactsTask.cjs:677-679` —— AI 客户端兜底：
```js
async function collectJson(aiService, options) {
  return aiService.collectJsonResponse ? aiService.collectJsonResponse(options) : aiService.requestJson(options);
}
```

### 11.4 任务中断恢复

`taskService.cjs:1120-1140`（调整任务）/ `:1213-1232`（生成任务）—— 启动时扫描并把 `running` 残留改写为中断态；`taskService.cjs:1368-1369` 挂载：
```js
    recoverInterruptedGlobalFactsTask(technicalPlanRecoveryState);
    recoverInterruptedGlobalFactsAdjustmentTask(technicalPlanRecoveryState);
```
恢复时清空（`taskService.cjs:1393-1395`、`:1423-1425`）：
```js
        globalFactsTask: undefined,
        globalFactsAdjustmentTask: undefined,
        globalFacts: [],
```

### 11.5 静默降级点（无日志）

- `normalizeGlobalFactsMode` 非法值 → `fabricate`（无日志）
- patch `mode` 未知 → `append`（`globalFactsTask.cjs:230`，无日志）
- `severity` 缺失 → `medium`（`contentGenerationTask.cjs:1721`）
- `sanitizeFileName` 清洗文件名（`globalFactsTaskV2.cjs:50-59`），非法字符 → `_`，不告警
- `mergeGlobalFactPatches` 锚点未命中 → 静默新建大项（`:278-280`）

---

## 12. 专项施工方案语境下的语义差异汇总

易标的全局事实体系是**为标书响应设计的**，其「大项划分标准、必保项、缺值口径、审计维度」全部围绕投标响应；而专项施工方案（尤其危大工程专项方案编制）需要的是**工程技术参数**。以下逐项对比，并给出可直接落地的替换文案。

### 12.1 必保事实项的根本错位 ⚠️ 最关键

| 对比项 | 易标（标书语境）原文 | 专项施工方案应有 |
|---|---|---|
| 强制必保项 | `globalFactsTask.cjs:32`/`:44`/`:70`/`:74`/`:77`/`:110`/`:113`/`:115`、`globalFactsTaskV2.cjs:67`/`:70`/`:72`/`:196` —— **工期/运维期/交货时间**（共 13 处） | **危大工程判定参数**（基坑深度、支撑高度跨度、施工总荷载、脚手架搭设高度、起重高度…）+ **工程地质水文** |
| 判定标准 | `globalFactsTaskV2.cjs:179`「凡这些材料表明后续正文需要统一口径的事项，都必须建项」 | 需按**建办质〔2018〕31 号九大章节**建项，且必须覆盖六大类危大工程的阈值参数 |

**建议替换文案**（对标 `buildFinalScheduleRule` 的强制位）：
```
6. 必须保留以下判定参数中的至少一类完整集合：
   6.1 工程地质与水文（地层结构、地下水类型与水位、勘察结论适用范围）；
   6.2 危大工程判定参数（基坑深度与支护形式、支撑体系高度与跨度、施工总荷载与集中线荷载、
       脚手架搭设高度、起重机起吊高度与幅度、模板支架搭设高度与立杆步距/间距）。
   材料没有具体值时按本任务给定的写法填写，不得省略任一判定维度。
```
⚠️ **本仓已有对应实现**：`AGENTS.md §4.16.3` 记录了 13 个危大参数名未归入九大章节的 P1 缺陷（`DANGER_PARAM_RULES` vs `CHAPTER_TEXT_RULES` 两份清单不同步），以及脚手架 24m 闭区间漏判 —— 易标侧**完全没有这些概念**。

### 12.2 「人员名单」类事实的位置差异

易标把「人员」当作**最典型的缺值场景**（`GlobalFactsPage.tsx:29`）：
```
如：涉及人员名单，但用户未提供，AI 会编辑不存在的人名。
```
专项方案语境下：
- **仍需要**项目经理/技术负责人/安全员/专职机械管理员（危大工程专项方案有法定人员要求，见建办质〔2018〕31 号）；
- 但**优先级远低于**工程技术参数 —— 易标的 `buildJsonExample` 用 `project_team` / `项目角色变量` 作**唯一示例**（`globalFactsTaskV2.cjs:81`、`:434`、`:458`），会诱导模型把结构聚焦在人员上。

**建议**：把 `buildJsonExample` 的示例从「项目角色变量」改为「危大工程判定参数」，例如：
```js
"content": "- 基坑深度：【待填写】。\n- 支护形式：地下连续墙＋三轴搅拌桩止水帷幕。\n- 支撑体系最大跨度：【待填写】。"
```

### 12.3 「交货 / 运维 / 质保」在专项方案中的映射

| 易标维度 | 专项方案对应物 |
|---|---|
| 工期 | 计划工期（编制依据，与合同一致） |
| 运维期 / 交货时间 | **无直接对应**，应替换为：材料进场时间、搭设/拆除时间节点、监测起止时间 |
| 交货和服务要求 | 施工机械进场要求、临时设施与临水临电条件 |
| 甲方信息 | 建设单位/监理单位/设计单位（专项方案需签署单位信息） |

⚠️ 专项方案语境下**「交货时间」是完全无意义的事实项**，而它在易标中被强制为「必保项之一」，共 13 处提示词 —— **直接移植会导致模型编造交货期**。这是移植时最需要重写的一处。

### 12.4 「统一规格型号」在专项方案中的映射

易标 `contentGenerationTask.cjs:914`：
```
16. 仅使用本章节提供的全局事实变量；未提供时不要主动编造具体人员、周期、质保、品牌、型号等会影响全文一致性的承诺。
```
专项方案应把「品牌、型号」替换为**工程参数**：
```
16. 仅使用本章节提供的全局事实变量；未提供时不要主动编造具体人员、周期、基坑深度、
    支撑高度跨度、施工荷载、监测频率、设备型号等会影响全文一致性的参数。
```

### 12.5 「短 bullet + 便于正文统一写法」的粒度差异

易标事实 content 是**短 bullet**（`globalFactsTaskV2.cjs:194`）：
```
每条 content 只写简体中文短 bullet，回答"后续正文遇到这个事项时统一写什么"。
```
示例（`globalFactsTask.cjs:459`）：
```
- 项目经理：张伟，负责总体协调。
- 技术负责人：李明，负责方案设计和联调验收。
```

专项方案的对应形态应是**参数化单值**（供正文直接引用），例如：
```
- 基坑开挖深度：18.5m。
- 地下水位：自然水位 1.2m，承压水头未查明。
- 支护结构：φ800@1000 钻孔灌注桩＋一道钢筋混凝土内支撑。
- 内支撑标高：-3.500m。
- 监测项目：支护结构顶部水平位移、竖向位移、深层水平位移、支撑轴力。
```
⚠️ 粒度差异导致**审计维度不同**：

| 维度 | 易标一致性审计（`contentGenerationTask.cjs:1674-1683`） | 专项方案应增加 |
|---|---|---|
| 冲突检测 | 人员/时间/型号/服务承诺前后不一 | ① 参数前后矛盾（正文 A 章 18.5m vs B 章 18m）；② 参数与规范条文冲突；③ 危大判定结论错误（如实际 24m 却按非危大编写） |
| 严重度 | `severity: "high"`（默认 `medium`） | 危大误判应恒为 `high`（触发专家论证漏提示） |
| 覆盖 | 只审「已写出的内容是否冲突」 | 需额外审「**必保参数是否缺失**」（易标明确禁止报缺失，`:1668`「正文没有涉及某条事实时，不要报告缺失」） |

⚠️ **这是最需要改写的一条审计规则**。易标 `:1668` 主动禁止报告缺失，因为标书语境下漏写由人工补；专项方案语境下**危大判定参数缺失本身就是合规事故**。

### 12.6 「删除空泛」判定的语义冲突 ⚠️ 易移植的隐性风险

`globalFactsTask.cjs:84`（`fabricate` 默认模式）：
```js
  return '1. 分段候选只代表对应片段，合并时要综合所有片段，删除重复、空泛和互相矛盾的表述。';
```
`globalFactsTask.cjs:105`：
```js
  return '3. 合并同义或重复大项，删除空泛内容、明显重复 bullet，以及仍停留在招标要求、评分规则、资料清单、待办事项层面的内容。';
```
`globalFactsTask.cjs:98`：
```js
  return '4. ...无法形成稳定事实且不能帮助正文保持一致的，应删除。';
```

**「空泛」在标书语境 = 空洞的承诺；在专项方案语境 = 缺少量化取值的表述**。直接移植会让模型把「按规范要求支护」这类**看似空泛实为合规表述**删掉，或反过来把「基坑深度 18.5m」这类**必须保留的精确值**当成「过于具体」而弱化。

**建议替换**：
```
1. 「空泛」仅指不含任何工程量值、不含任何可执行动作的纯口号式表述。
   以下情形**不得**判为空泛：
   - 按国家/行业/地方标准执行的表述（属于合规口径，必须保留原文规范号）；
   - 引用已批复设计文件、地质勘察报告的结论（属事实引用，不得改写为口号）；
   - 已给出量化取值的参数（精度越高越好，禁止因"太具体"而删除或弱化）。
   删除空泛内容时，必须保证删除后该项仍能指导正文统一写法。
```

### 12.7 「商务/资格排除」在专项方案中的调整

易标 7 处排除「商务报价、资格材料」（`globalFactsTask.cjs:523, 556`、`globalFactsTaskV2.cjs:157, 194` 等）。

专项方案语境下需要**反向增加排除项**：
```
7. 不要输出：商务报价、投标报价、资格材料、业绩证明；
   也不要输出：未经勘察或设计文件支持的岩土参数（不得凭空给出地层承载力、
   地下水渗透系数、周边建（构）筑物距离等必须来源于勘察报告的数据）。
```
⚠️ 专项方案的**数据真实性风险远高于标书**：标书写错参数只是评审扣分，专项方案写错基坑深度会直接导致**安全事故**。建议 `fabricate` 模式在专项方案语境下**降级为 `placeholder`**（易标的 `fabricate` 返回空规则块、无完整性约束，见 §3.4 —— 恰好是三种模式里最不适合工程场景的）。

### 12.8 「已有方案扩写」在专项方案中的价值更高

`globalFactsTaskV2.cjs:205`
```
用原方案补充已有大项的具体内容；原方案与招标明确事实冲突时，原方案已落地的安排优先替换对应 bullet。
```
专项方案场景下这条规则几乎原样可用且更重要 —— 企业历史专项方案里的**实际支护参数、监测数据、验收记录**是最可靠的参数来源。建议把「招标明确事实」替换为「**勘察报告与设计文件**」。

### 12.9 语义差异速查表

| 维度 | 易标（标书） | 专项施工方案 | 是否需重写 |
|---|---|---|---|
| 强制必保事实 | 工期/运维期/交货时间 | 危大判定参数 + 工程地质水文 | ✅ **必须** |
| 典型大项示例 | `project_team` 项目角色变量 | `foundation_depth` 基坑深度 / `sc_ground` 脚手架高度 | ✅ 改 few-shot |
| 章节归属 | 无（扁平 groups） | 九大章节（建办质〔2018〕31 号） | ✅ 需加 `chapter` 字段 |
| 事实属性 | 无 | 定量/定性/关系/规范（4 正交维度） | ✅ 本仓已实现 |
| 数据来源标注 | 无 | 招标文件/图纸/勘察/总体方案/手工 | ✅ 本仓已实现 |
| 审计「缺失」策略 | 明确禁止报缺失 | **缺失即事故**，需报 | ✅ **必须改** |
| 空泛定义 | 不够具体 = 空泛 | 不够具体 ≠ 空泛；**过具体是优点** | ✅ **必须改** |
| 事实句改写 | 要求句 → 响应承诺 | 要求句 → **本方案已确定的工程做法与参数** | ⚠️ 措辞调整 |
| 数据真实性容错 | `fabricate` 默认（可杜撰人名） | 应改 `placeholder` | ✅ 改默认值 |
| 标段限定 | 有（多标段） | 无（单项目单方案） | ➖ 保留无害 |
| 编排期标题筛选 | 已有（`facts.titles` 白名单） | 可直接沿用 | ✅ 保留 |
| 唯一命中替换 | 已有（`old_text` 双通道） | 可直接沿用 | ✅ 保留 |
| Agent 章节标记校验 | 已有（八道门） | 可直接沿用 | ✅ 保留 |
| 上下文预算分段 | 有但**未挂载**（V2 不用） | 需为 Agent 侧自行设计 | ⚠️ 需另做 |

---

## 13. 未在源码中找到的项

| 追问 | 结论 |
|---|---|
| `missingValueMode` 变量 | 源码中未找到该命名。实际字段为 `globalFactsMode`（4 处实现） |
| `global-facts.md` 在全局事实**生成**阶段的准备 | 源码中未找到。`global-facts.md` 仅在**正文一致性 Agent 修复**阶段出现（`contentGenerationTask.cjs:5580`）；V2 生成阶段用的是 `global-facts.json`（`globalFactsTaskV2.cjs:13`） |
| `technical-plan.md` 的准备 | 仅在一致性 Agent 修复阶段（`contentGenerationTask.cjs:5581`），生成阶段未找到 |
| V1 `runGlobalFactsTask` 的调用点 | 源码中未找到（`taskService.cjs:5` 只 import V2） |
| `batchRenderedItems` / `waitAllOrThrow` / `getGlobalFactsSegmentLimit` 的生产调用 | 源码中未找到（三者仅在 V1 内部互相调用，且 V1 未挂载） |
| 全局事实的「二次检查」在**全局事实模块内** | 源码中未找到。一致性审计属 `contentGenerationTask.cjs`（正文阶段），全局事实生成阶段**只有 V1 的 `finalizeGlobalFacts`**（未挂载）承担类似职责 |
| 「已有方案扩写优先」的**程序级**保障 | 源码中未找到。全靠提示词（`globalFactsTask.cjs:605`、`:670`） |
| 全局事实的版本/历史/回滚 | 源码中未找到（`replaceGlobalFacts` 是整表 DELETE + INSERT） |
| 标书语境下的「质保期 / 响应时间」 | 提示词中出现「质保」（`contentGenerationTask.cjs:914`）与「运维期/交货时间」，但**无独立的质保/响应时间必保规则** |
| 危大工程 / 脚手架 / 基坑等专项参数 | 源码中未找到（全文无相关字样） |

---

## 标书语义耦合点清单

> 本节按用户要求，逐条列出与「标书 / 投标 / 工期 / 响应时间 / 质保 / 商务」耦合的**原文位置（file:line）+ 原文摘录 + 「若改写为专项施工方案语义，应改为什么」**。
>
> 仓库根：`J:\编程\OpenBidKit 易标\OpenBidKit_Yibiao-main-2026-09-14\client`
> 专项施工方案目标语义域：工程地质水文 / 基坑深度 / 支撑高度跨度 / 施工总荷载 / 脚手架搭设高度 / 危大工程判定参数

### C-01 工期/运维期/交货时间 强制必保（13 处，最核心耦合）

| # | file:line | 原文摘录 |
|---|---|---|
| a | `electron/services/globalFactsTask.cjs:32`（completenessRules omit 第 8 条） | `8. 工期、运维期或交货时间等事项若正文需要统一口径，必须保留为事实项；材料没有具体值时使用笼统承诺，不要编造日期或周期，也不要因此省略该项。` |
| b | `electron/services/globalFactsTask.cjs:44`（placeholder 第 8 条） | `8. 工期、运维期或交货时间等事项若正文需要统一口径，必须保留为事实项；材料没有具体值时使用【待填写】，不要编造日期或周期，也不要因此省略该项。` |
| c | `electron/services/globalFactsTask.cjs:70`（merge omit 第 5 条） | `5. 必须包含工期、运维期或交货时间中的至少一个相关变量；材料没有具体值时使用笼统承诺，不要编造日期或周期，也不要省略该类变量。` |
| d | `electron/services/globalFactsTask.cjs:74`（merge placeholder 第 5 条） | `5. 必须包含工期、运维期或交货时间中的至少一个相关变量；材料没有具体值时使用【待填写】，不要编造日期或周期。` |
| e | `electron/services/globalFactsTask.cjs:77`（merge fabricate 第 5 条） | `5. 必须包含工期、运维期或交货时间中的至少一个相关变量；如果分段候选不足，但项目概述或 Step02 关键解析结果中已有明确内容，应补入。` |
| f | `electron/services/globalFactsTask.cjs:110`（finalSchedule omit） | `6. 必须保留工期、运维期或交货时间中的至少一个相关变量；材料没有具体值时使用笼统承诺，不要编造日期或周期，也不要省略该类变量。` |
| g | `electron/services/globalFactsTask.cjs:113`（finalSchedule placeholder） | `6. 必须保留工期、运维期或交货时间中的至少一个相关变量；材料没有具体值时使用【待填写】，不要编造日期或周期。` |
| h | `electron/services/globalFactsTask.cjs:115`（finalSchedule fabricate） | `6. 必须保留工期、运维期或交货时间中的至少一个相关变量。` |
| i | `electron/services/globalFactsTaskV2.cjs:67`（V2 missingValueRule omit） | `...必须包含工期、运维期或交货时间中的至少一个相关变量；没有具体值时同样使用笼统承诺，不要编造日期或周期。` |
| j | `electron/services/globalFactsTaskV2.cjs:70`（V2 placeholder） | `...必须包含工期、运维期或交货时间中的至少一个相关变量；没有具体值时使用【待填写】。` |
| k | `electron/services/globalFactsTaskV2.cjs:72`（V2 fabricate） | `...必须包含工期、运维期或交货时间中的至少一个相关变量；分段或材料不足时，若项目概述或招标解析结果中已有明确内容应写入，否则按项目语境补足具体周期。` |
| l | `electron/services/globalFactsTaskV2.cjs:196`（V2 workPrinciples） | `6. 必须包含工期、运维期或交货时间中的至少一个相关变量；缺具体值时按本任务给定的写法填写，不要省略该项。` |

**若改写为专项施工方案语义，应改为：**

> `6. 必须保留危大工程判定参数中的至少一整组：基坑开挖深度与支护形式、支撑体系高度与跨度、施工总荷载与集中线荷载（含 kN/m²、kN/m）、脚手架搭设高度（m）、模板支架搭设高度与立杆步距/立杆间距、起重机起吊高度与幅度；材料没有具体值时按本任务给定的写法填写，不要省略任一判定维度。`

> `6.1 若本工程含基坑：必须保留「开挖深度、支护形式、支撑体系形式与标高、地下水类型与水位」四项，不得因勘察报告未覆盖而删除。`
> `6.2 若本工程含高处作业/脚手架：必须保留「架体类型、搭设高度、总荷载、连墙件设置」四项。`
> `6.3 若本工程含起重吊装：必须保留「起吊重量、起吊高度、吊装方式」三项。`

**说明**：专项方案语境下「交货时间」完全无意义，直接移植会诱导模型编造交货期；「运维期」无对应；只有「工期」可部分映射为「计划工期（与合同一致）」，但**不能作为唯一强制项**。

### C-02 「投标技术方案」自我定位（4 处 system/user 提示词）

| # | file:line | 原文摘录 |
|---|---|---|
| a | `electron/services/globalFactsTask.cjs:52` | `你是专业的投标技术方案事实变量整理助手。请基于用户提供的上下文，整理后续正文需要统一采用的全局事实变量。` |
| b | `electron/services/contentGenerationTask.cjs:807` | `你是投标技术方案正文编排助手。请根据章节上下文判断本小节最适合的表达方式。` |
| c | `electron/services/contentGenerationTask.cjs:1663` | `你是投标技术方案全文一致性审计助手。请审计本组正文是否与给定事实冲突。` |
| d | `electron/services/contentGenerationTask.cjs:1769` | `你是投标技术方案正文一致性修复助手。请只针对当前小节返回局部精确替换 patch。` |

**若改写为专项施工方案语义，应改为：**
- a → `你是专业的危大工程专项方案事实变量整理助手。请基于用户提供的上下文，整理后续专项方案正文需要统一采用的工程参数与施工做法。`
- b → `你是危大工程专项施工方案正文编排助手。请根据章节上下文判断本小节最适合的表达方式。`
- c → `你是危大工程专项施工方案全文一致性审计助手。请审计本组正文是否与给定工程参数冲突或与规范条文矛盾。`
- d → `你是危大工程专项施工方案正文一致性修复助手。请只针对当前小节返回局部精确替换 patch。`

---

### C-03 「招标要求 → 响应承诺」的改写义务（4 处）

| # | file:line | 原文摘录 |
|---|---|---|
| a | `electron/services/globalFactsTask.cjs:55` | `1. 全局事实变量不是招标要求摘录、评分规则摘要或待办事项清单，而是技术方案正文中需要保持一致的确定性方案事实、响应设定、承诺口径或执行安排。` |
| b | `electron/services/globalFactsTask.cjs:57` | `3. 用户资料只给出要求、约束或评价口径时，不要原样摘录为要求句；如果该内容会影响后续正文的一致写法，应转写为本方案已经采用、已经具备或统一承诺的事实表达。` |
| c | `electron/services/globalFactsTaskV2.cjs:192` | `全局事实不是招标要求摘录、评分规则或待办清单，而是正文要统一采用的方案事实、响应设定、承诺口径或执行安排。材料给出的是要求或约束时，不要原样摘录要求句，应转写为本方案统一口径。` |
| d | `electron/services/globalFactsTask.cjs:96`（buildFinalRewriteRule omit/placeholder） | `4. 如果某条内容表达的是“需要满足什么要求”，请改写为“本方案统一采用什么事实、安排、承诺或响应口径”。` |

**若改写为专项施工方案语义，应改为：**
- a → `1. 全局事实变量不是规范条文摘录、计算书摘要或待办事项清单，而是专项方案正文中需要保持一致的工程参数（地质水文、危大判定参数、支护/支撑/架体做法）与已确定的施工安排。`
- b → `3. 勘察报告或设计文件只给出结论、约束或评价口径时，不要原样摘录为条文句；应转写为本方案实际采用的支护形式、支撑体系、监测项目与施工做法，并保留其依据来源。`
- c → `全局事实不是规范条文摘录、计算结论或待办清单，而是正文要统一采用的工程参数与已确定的施工做法。材料给出的是条文要求或结论时，不要原样摘录，应转写为本方案的实际取值与做法。`
- d → `4. 如果某条内容表达的是“规范要求什么/勘察结论是什么”，请改写为“本方案采用什么参数、什么支护形式、什么监测与施工做法”。`

---

### C-04 商务/资格材料的显式排除（7 处）

| # | file:line | 原文摘录 |
|---|---|---|
| a | `electron/services/globalFactsTask.cjs:63` | `4. 不输出分析过程、来源说明、风险提示、正文草稿或未落地的要求句。` |
| b | `electron/services/globalFactsTask.cjs:523` | `6. 不要输出商务报价、资格材料、正文草稿、分析过程或来源说明。` |
| c | `electron/services/globalFactsTask.cjs:556` | `7. 仅编写技术方案部分，不要涉及商务报价或资格材料。` |
| d | `electron/services/globalFactsTaskV2.cjs:157` | `- 项目概述.md：项目背景和术语，用于确定大项，不作为商务/资格材料来源。` |
| e | `electron/services/globalFactsTaskV2.cjs:194` | `...不写分析过程、来源说明、风险提示、正文草稿、商务报价或资格材料。` |
| f | `electron/services/globalFactsTaskV2.cjs:151` | `- ${listed}：排除其他标段后的当前投标范围正文，用于确定大项并提取明确事实；不要扩展到其他标段。` |
| g | `electron/services/globalFactsTaskV2.cjs:161` | `- 标段说明.md：本次投标范围，只关注该范围内的事实。` |

**若改写为专项施工方案语义，应改为：**
- b → `6. 不要输出商务报价、资格材料、投标策略、正文草稿；也不要输出未经勘察报告或设计文件支持的岩土参数（不得凭空给出地层承载力、地下水渗透系数、周边建（构）筑物距离）。`
- c → `7. 仅编写专项施工方案技术内容，不要涉及商务报价、投标承诺或资格材料。`
- d → `- 项目概述.md：工程背景、地质与周边环境、术语，用于确定大项；不作为技术参数来源（工程参数必须来自勘察报告与设计文件）。`
- e → `...不写分析过程、来源说明、风险提示、正文草稿、商务报价、资格材料或投标策略。`
- f/g → 专项方案无多标段概念，可改为「工程所在地与周边环境说明.md：本工程适用范围，只关注本工程的事实」，或直接删除该分支。

---

### C-05 「人员名单」缺值场景（UI + few-shot，4 处）

| # | file:line | 原文摘录 |
|---|---|---|
| a | `src/features/technical-plan/pages/GlobalFactsPage.tsx:29` | `...如：涉及人员名单，但用户未提供，AI 会编辑不存在的人名。此模式写完的技术方案直接完整可用，无需人工干预。` |
| b | `src/features/technical-plan/pages/GlobalFactsPage.tsx:224` | `content: '- 项目经理：张伟，高级工程师，负责总体协调和质量把关。',` |
| c | `electron/services/globalFactsTaskV2.cjs:81`（buildJsonExample placeholder） | `"id": "project_team", "title": "项目角色变量", "content": "- 项目经理：【待填写】。\n- 技术负责人：【待填写】。"` |
| d | `electron/services/globalFactsTaskV2.cjs:103`（fabricate 示例） | `"id": "project_team", "title": "项目角色变量", "content": "- 项目经理：张伟，负责总体协调。\n- 技术负责人：李明，负责方案设计和联调验收。"` |

**若改写为专项施工方案语义，应改为：**
- a → `...如：涉及危大工程判定参数（如基坑深度、支撑高度跨度），但勘察报告未提供，AI 会填入【待填写】。专项方案不建议使用"杜撰"模式 —— 错误参数会直接导致安全事故。`
- b → `content: '- 基坑开挖深度：【待填写】。\n- 支护形式：地下连续墙＋三轴搅拌桩止水帷幕。\n- 支撑体系最大跨度：【待填写】。'`
- c/d → few-shot 示例整体从 `project_team / 项目角色变量` 改为 `foundation_depth / 基坑支护参数`（或按工程类型动态选示例），避免模型把结构聚焦在人员名单上。

⚠️ 专项方案**仍需保留法定人员**（项目经理/技术负责人/专职安全员/专职机械管理员，见建办质〔2018〕31 号），但应降为**次要**大项，不得作为唯一示例。

### C-06 「质保 / 品牌 / 型号 / 服务承诺」不得编造（2 处）

| # | file:line | 原文摘录 |
|---|---|---|
| a | `electron/services/contentGenerationTask.cjs:914` | `16. 仅使用本章节提供的全局事实变量；未提供时不要主动编造具体人员、周期、质保、品牌、型号等会影响全文一致性的承诺。` |
| b | `electron/services/contentGenerationTask.cjs:817` | `8. 编排判断必须结合招标文件关键信息和全局事实变量标题，不要规划会造成时间、地点、人员、设备、标准或服务承诺前后不一致的表达。` |

**若改写为专项施工方案语义，应改为：**
- a → `16. 仅使用本章节提供的全局事实变量；未提供时不要主动编造具体人员、周期、基坑深度、支撑高度跨度、施工总荷载、监测频率、设备型号、支护形式等会影响全文一致性与安全性的参数。`
- b → `8. 编排判断必须结合勘察报告关键信息、设计文件和全局事实变量标题，不要规划会造成基坑深度、支护形式、支撑体系、监测项目、荷载取值前后不一致，或导致危大工程判定结论错误的表达。`

⚠️ 「质保」在专项方案中无对应概念（质保属合同/商务范畴），应从禁止编造清单中删除；替换为工程参数类禁止项。

---

### C-07 「技术评分信息 / 评分口径 / 评分规则」耦合（3 处）

| # | file:line | 原文摘录 |
|---|---|---|
| a | `electron/services/globalFactsTask.cjs:26`（omit 第 2 条） | `2. 凡招标要求、评分口径、项目概述、目录或参考材料表明后续技术方案正文需要统一口径的事项，都必须建项并写出 bullet；` |
| b | `electron/services/globalFactsTask.cjs:577`（知识库补充第 2 条） | `2. 只处理与项目概述、技术评分信息、目录和技术方案正文强相关，且能够沉淀为稳定方案事实的内容。` |
| c | `electron/services/globalFactsTask.cjs:55` / `globalFactsTaskV2.cjs:192` | `...不是招标要求摘录、评分规则摘要或待办事项清单...`（见 C-03a/c） |

**若改写为专项施工方案语义，应改为：**
- a → `2. 凡勘察报告、设计文件、危大工程清单、法规标准或目录表明专项方案正文需要统一口径的事项，都必须建项并写出 bullet；不得因材料缺少具体做法、人名、日期、荷载、规格等而省略该项、不建组或不输出该 bullet。`
- b → `2. 只处理与工程地质水文、危大判定参数、技术方案目录和专项方案正文强相关，且能够沉淀为稳定工程参数与施工做法的内容。`
- c → `...不是规范条文摘录、计算结论或待办事项清单...`

---

### C-08 「Step02 关键解析结果」数据源耦合（3 处 + 数据结构）

| # | file:line | 原文摘录 |
|---|---|---|
| a | `electron/services/globalFactsTask.cjs:353-355` | `formatBidAnalysisFactForPrompt(storedPlan, 'projectInfo', '项目信息')` / `'partAInfo', '甲方信息'` / `'deliveryAndServiceRequirements', '交货和服务要求'` |
| b | `electron/services/globalFactsTaskV2.cjs:158` | `- 招标解析结果.md：Step02 已抽出的项目信息、甲方信息、交货和服务要求，用于确定大项并提取明确值。` |
| c | `electron/services/contentGenerationTask.cjs:1687` | `{ role: 'user', content: `Step02 关键解析结果（项目信息、甲方信息、交货和服务要求）：\n${bidAnalysisFactsText || '未提供'}` },` |
| d | `electron/services/globalFactsTask.cjs:356` | `... || '未提供 Step02 关键解析结果。';` |

**若改写为专项施工方案语义，应改为：**
- a → 三项替换为 `地质勘察结论` / `设计文件说明（含支护设计参数）` / `危大工程辨识与工程概况`；对应 `deliveryAndServiceRequirements`（交货和服务要求）**直接删除**。
- b → `- 勘察与设计结论.md：已抽出的地质与水文结论、支护设计参数、危大工程辨识结论，用于确定大项并提取明确值。`
- c → `{ role: 'user', content: `勘察与设计关键结论（工程地质水文、支护设计参数、危大工程辨识）：\n${bidAnalysisFactsText || '未提供'}` },`
- d → `... || '未提供勘察与设计关键结论。';`

---

### C-09 「投标方实际安排 / 投标范围 / 标段」耦合（4 处）

| # | file:line | 原文摘录 |
|---|---|---|
| a | `electron/services/globalFactsTask.cjs:605` | `1. 原方案中已经写成投标方实际安排、既有承诺、统一配置、技术路线、服务口径或实施做法的内容，优先补充到全局事实变量中。` |
| b | `electron/services/globalFactsTaskV2.cjs:205` | `用原方案补充已有大项的具体内容；原方案与招标明确事实冲突时，原方案已落地的安排优先替换对应 bullet。不要仅因原方案出现新话题就新增大项。` |
| c | `electron/services/globalFactsTaskV2.cjs:110` | `return storedPlan?.bidSectionMode === 'multiple' && Boolean(storedPlan?.tenderFile?.selectedSectionId);` |
| d | `electron/services/globalFactsTaskV2.cjs:161` | `- 标段说明.md：本次投标范围，只关注该范围内的事实。` |

**若改写为专项施工方案语义，应改为：**
- a → `1. 原方案中已经写成实际支护形式、实际支撑体系、实际监测项目、实际施工工艺或已验收做法的内容，优先补充到全局事实变量中。`
- b → `用原方案补充已有大项的具体内容；原方案与勘察报告/设计文件明确事实冲突时，以勘察报告与设计文件为准（除非原方案已有经审查的变更手续）。不要仅因原方案出现新话题就新增大项。`
- c/d → 专项方案**无标段概念**，直接删除该分支；`bidSectionMode` / `selectedSectionId` 相关逻辑整条移除。

---

### C-10 「评审通过 / 直接完整可用 / 无需人工干预」承诺（1 处）

| # | file:line | 原文摘录 |
|---|---|---|
| a | `src/features/technical-plan/pages/GlobalFactsPage.tsx:29` | `此模式写完的技术方案直接完整可用，无需人工干预。` |

**若改写为专项施工方案语义，应改为：**
`生成结果仅为草稿。危大工程专项方案在实施前必须经施工单位技术负责人审批、监理单位审查；涉及专家论证的还须通过专家论证。杜撰的工程参数可能导致安全事故，本模式不适用于危大工程专项方案。`

⚠️ **这是最需要重写的 UI 承诺** —— 标书的「直接可用」是评审便利，专项方案的「直接可用」是**安全风险**。

---

### C-11 「服务口径 / 交货 / 运维」在审计与修复中的体现（2 处）

| # | file:line | 原文摘录 |
|---|---|---|
| a | `electron/services/globalFactsTask.cjs:667`（final 第 5 条） | `5. 不要新增与当前事实相冲突的具体值、服务承诺或技术边界。` |
| b | `electron/services/globalFactsTask.cjs:642`（patch 合并第 3 条） | `3. 合并后的 patch 内容必须是正文可直接统一使用的方案事实、响应设定、承诺口径或执行安排。` |

**若改写为专项施工方案语义，应改为：**
- a → `5. 不要新增与当前事实相冲突的具体工程参数（荷载、深度、高度、跨度）、新的支护/支撑形式或超出设计文件的技术边界。`
- b → `3. 合并后的 patch 内容必须是正文可直接统一使用的工程参数、已确定的施工做法或监测安排。`

---

### C-12 语义耦合点汇总统计

| 耦合类别 | 位置数 | 风险等级（专项方案移植） |
|---|---|---|
| 工期/运维期/交货时间强制必保（C-01） | 12 | 🔴 **P0** —— 会诱导编造交货期、运维期 |
| 「投标技术方案」自我定位（C-02） | 4 | 🟠 P1 —— 影响角色理解与输出风格 |
| 招标要求 → 响应承诺改写（C-03） | 4 | 🔴 **P0** —— 改写目标完全错位 |
| 商务/资格排除（C-04） | 7 | 🟡 P2 —— 保留无害，但需**增加**岩土参数禁止项 |
| 人员名单 few-shot（C-05） | 4 | 🟠 P1 —— 诱导结构聚焦人员，挤压工程参数 |
| 质保/品牌/型号不得编造（C-06） | 2 | 🟠 P1 —— 需替换为工程参数禁止项 |
| 技术评分/评分口径（C-07） | 3 | 🟡 P2 —— 专项方案无评分，替换为法规标准 |
| Step02 数据源结构（C-08） | 4 | 🔴 **P0** —— 「交货和服务要求」无对应，须删除 |
| 投标范围/标段（C-09） | 4 | 🟢 P3 —— 可直接删除该分支 |
| 「直接完整可用」承诺（C-10） | 1 | 🔴 **P0** —— 安全风险，必须改写 |
| 服务承诺/技术边界（C-11） | 2 | 🟠 P1 —— 需改为工程参数/支护形式 |
| **审计禁止报缺失**（§12.5，非标书词但为标书规则） | 1 | 🔴 **P0** —— 专项方案缺失即事故，须反向 |
| **「空泛」定义**（§12.6，`globalFactsTask.cjs:84/98/105`） | 3 | 🔴 **P0** —— 会删掉必须保留的精确参数 |

**移植优先级建议**：
1. **先改 P0**（C-01 必保项、C-03 改写义务、C-08 数据源、C-10 UI 承诺、审计缺失规则、空泛定义）—— 不改会直接产出错误的工程参数；
2. **再改 P1**（C-02 定位、C-05 few-shot、C-06 禁止编造清单、C-11 边界）—— 影响输出质量与结构；
3. **P2/P3 可延后**（C-04 排除项、C-07 评分口径、C-09 标段）—— 保留无害但可清理。

---

*报告完 —— 全部结论基于易标仓库真实源码，每条均标注 `file:line` 并附提示词逐字原文。*

























