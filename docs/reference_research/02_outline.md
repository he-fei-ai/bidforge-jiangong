# 易标「目录生成模块」源码考古报告

**仓库**：`J:\编程\OpenBidKit 易标\OpenBidKit_Yibiao-main-2026-09-14`（只读，未做任何修改）
**主文件**：`client/electron/services/outlineGenerationTaskV2.cjs`（1289 行）
**审计日期**：2026-10-01
**审计范围**：目录生成模块全链路（Electron 主进程 + Renderer 页面 + 存储层 + Pi Agent 执行引擎）

---

## 0. 首要更正：任务书假设与源码事实的差异

任务书的若干路径/命名假设在本仓库**均不存在或不适用**。实际架构差异极大，这直接决定了第 12 项改造的可行性判断。

| 任务书假设 | 实际源码事实 |
|---|---|
| `outlineGenerationTask.cjs` | **不存在**。只有 `outlineGenerationTaskV2.cjs`（V2 唯一实现） |
| Pydantic / `OutlineResponse` / `OutlineChildrenResponse` / `OutlineReviewResponse` | **源码中未找到**。本项目是 Electron + Node.js，Schema 为 **手写 JSON Schema + Ajv**，非 Pydantic |
| `collect_json_response` 用于目录链路 | **目录链路完全不调用它**（详见第 3 项，重要发现） |
| `check_json` 校验函数 | **源码中未找到该函数名**。对应能力由 Agent 工具 `json-validation` 承担（`piJsonValidationTool.cjs`） |
| `other_outline` 参数 | **源码中未找到**。防重复靠 `branch_id` 稳定标识 + `score-directory-plan.json` 映射（详见第 2 项） |
| 审核返回 `{passed, suggestions}` | 实际为 `{status, issues[], user_feedback, summary}`（详见第 5.2 项） |
| 完整目录失败切换分步生成 | **源码中未找到该降级路径**（详见第 10.1 项） |

### 实际执行栈（与假设完全不同）

```
outlineGenerationTaskV2.cjs:974  agentService.runTask({...})
  → agentService.cjs:601  runTask(payload) → startTask
    → agentService.cjs:570  entry.runtime.runTask(runtimePayload)
      → pi/piRuntimeService.cjs:842  for (attemptIndex...maxRetries)  ← 真正的重试点
        → pi/piJsonValidationTool.cjs  Agent 自主调用的 json-validation 工具
```

**核心认知**：目录生成不是「函数调用 AI 接口」，而是「**宿主程序预置 JSON 骨架 + 写入工作区 → 持久化 Agent 会话自主读写文件 → 宿主通过 continueTask 状态机推进阶段**」的多阶段工作流。

---

## 1. 一级目录如何与技术评分要求一一对应

### 1.1 三种模式（`outlineGenerationTaskV2.cjs:757`）

```javascript
757:  const standaloneTechnical = storedPlan.outlineMode === 'standalone-technical';
```

| `outlineMode` | 一级目录来源 | 对应机制 |
|---|---|---|
| `standalone-technical` | 技术评分大项 | **严格一一对应** |
| `aligned` / `response-file` | 响应文件要求.md | 评分项落在二三级（`score_item_level` 可配） |

### 1.2 一一对应的实现是「提示词约束 + 机械复核」双层，**不是代码硬锁**

**第一层（提示词）** — `createInitialPrompt` `:518`：

```javascript
518:      : '我们的目标是为单独装订的技术文件准备一级目录。一级目录必须直接对应技术评分大项。'
```

`:526`：

```javascript
526:  7. 每个一级目录直接对应一个技术评分大项，并保持评分大项的原顺序和正式表述；不得创建“技术方案”“项目管理方案”“监理大纲”“监理大纲（暗标）”“施工组织设计”“技术标”等外层总目录，也不得加入商务、资信、投标函、授权委托书等非技术章节。
**第二层（代码机械复核）** — `collectScoreMappingCoverage` `:419-461`，这是**唯一的机械校验出口**：

```javascript
440:      actual_titles: actualTitles,
441:      missing_titles: uniqueExpectedTitles.filter((title) => !actualTitleSet.has(title)),
442:      unexpected_titles: actualTitles.filter((title) => !expectedTitleSet.has(title)),
```

`:453-458` 判定：

```javascript
453:  return {
454:    valid: branches.every((branch) => (
455:      branch.root_found
456:      && branch.missing_titles.length === 0
457:      && branch.unexpected_titles.length === 0
458:    )),
```

⚠️ **关键**：`valid` 只写进 `outline-review-context.json` 交给 Agent 自行判断（`:487`），**宿主代码从不据此阻断流程**。

### 1.3 标题锁定 / 改名的真实实现

**锁定点** — `applyConfirmedSelection` `:899-913`：

```javascript
899:  function applyConfirmedSelection(confirmed) {
900:    const selectedIdSet = new Set(confirmed.selectedIds);
901:    lockedRoots = renumberOutline(confirmed.items.filter((item) => selectedIdSet.has(item.id)));
```

`renumberOutline` `:291-313` 是**唯一的编号权威实现**：

```javascript
291: function renumberOutline(items, prefix = '') {
292:  return (items || []).map((item, index) => {
293:    const id = prefix ? `${prefix}.${index + 1}` : String(index + 1);
294:    const hasChildren = Array.isArray(item?.children) && item.children.length;
295:    const next = {
296:      id,
297:      title: String(item?.title || '').trim(),
298:      description: String(item?.description || '').trim(),
299:      ...(prefix ? {} : { attr: item?.attr }),
300:      ...(!prefix && String(item?.branch_id || '').trim() ? { branch_id: String(item.branch_id).trim() } : {}),
301:      ...(!hasChildren ? {
302:        content_mode: item?.content_mode,
303:        ...(item?.content_mode === 'other' && String(item?.content_mode_note || '').trim()
304:          ? { content_mode_note: String(item.content_mode_note).trim() }
305:          : {}),
306:      } : {}),
307:    };
308:    if (hasChildren) {
309:      next.children = renumberOutline(item.children, id);
310:    }
311:    return next;
312:  });
313: }
```

⚠️ 注意 `renumberOutline` **会重写 title/description**（trim 规范化），但不做标题映射。真正的「改名后仍能对上评分项」靠 `branch_id`。

**`branch_id` 稳定标识 = 防改名/重排断链的核心机制**（`outlineGenerationTaskV2.cjs:641` 原文）：

```javascript
641:  3. ${mappingInstruction} branch_id 是技术分支稳定标识：对应的最终一级目录必须保留同名 branch_id，即使一级目录新增、删除、改名、重排或重新编号也不得改变；非技术分支一级目录不要填写 branch_id。
```

- 附加：`attachBranchIdsToRoots` `:332-342`（按 `root_id` + 标题**双重**匹配才挂 `branch_id`）

```javascript
332: function attachBranchIdsToRoots(items, scoreDirectoryPlan) {
333:   const branchesByRootId = new Map();
334:   (scoreDirectoryPlan?.branches || []).forEach((branch) => {
335:     const rootId = String(branch.root_id || '').split('.')[0];
336:     if (!branchesByRootId.has(rootId)) branchesByRootId.set(rootId, branch);
337:   });
338:   return (items || []).map((item) => {
339:     const branch = branchesByRootId.get(item.id);
340:     return branch?.root_title === item.title ? { ...item, branch_id: branch.branch_id } : item;
341:   });
342: }
```

- 同步：`synchronizeScoreDirectoryPlan` `:345-358`（按 `branch_id` 反查，回写新 `root_id`/`root_title`）

```javascript
345: function synchronizeScoreDirectoryPlan(scoreDirectoryPlan, items) {
346:   const rootsByBranchId = new Map(
347:     (items || [])
348:       .filter((item) => String(item?.branch_id || '').trim())
349:       .map((item) => [String(item.branch_id).trim(), item]),
350:   );
351:   return {
352:     ...scoreDirectoryPlan,
353:     branches: (scoreDirectoryPlan?.branches || []).map((branch) => {
354:       const root = rootsByBranchId.get(branch.branch_id);
355:       return root ? { ...branch, root_id: root.id, root_title: root.title } : branch;
356:     }),
357:   };
358: }
```

- 落库前剥离：`stripOutlineInternalFields` `:321-329`

**改名审批门** — `:610`：

```javascript
610:  9. 默认锁定一级目录，allow_root_changes=false；只有用户明确批准一级目录调整时才设为 true。
```

消费点 `createChildrenPrompt` `:624-626`：

```javascript
624:  const rootInstruction = allowRootChanges
625:    ? `用户已批准 ${SCORE_DIRECTORY_PLAN_FILE} 中记录的一级目录调整，只能按该规划进行必要修改并重新编号。`
626:    : '一级目录的数量、顺序、id、title、description、attr 均已由用户确认，必须保持不变；未扩展为父节点的一级目录还必须保留其 content_mode。';
```

---

```

`createScorePlanningPrompt` `:593-594` 进一步锁死映射：

```javascript
593:    ? `4. 当前采用“技术文件独立成册”：${OUTLINE_OUTPUT_FILE} 中每个一级根节点本身就应对应一个技术评分大项。每个根节点建立一个 branch，score_item_level 固定为 1，mappings 只填写与该根标题对应的评分大项，target_title 必须与 root_title 完全一致；不得再创建“技术方案”“项目管理方案”“监理大纲”“监理大纲（暗标）”“施工组织设计”“技术标”等外层分支。
594:  5. 一级根节点与评分大项默认严格一一对应；发现缺失、重复、合并或顺序不一致时，必须作为一级目录调整向用户说明并取得批准。detail_points 只用于后续生成根节点以下的目录。`
```

---
## 2. 二三级目录生成：并发、other_outline、JSON 框架预拼接

### 2.1 「other_outline」在源码中未找到 —— 实际机制是「单一 Pi Session + 文件传递」

**不并发**：整个目录生成是**一个持久 Agent 会话**串行推进（`piRuntimeService.cjs:929-954` 的 `continueTask` 循环），不是多路并发调用。

**防重复的真实手段**：`branch_id` + `score-directory-plan.json`。提示词明确禁止重复（`:628`）：

```javascript
627:  const mappingInstruction = standaloneTechnical
628:    ? '每个 branch 的 score_item_level=1，现有一级根节点本身就是评分项映射节点。不得在根节点下面再次生成同名评分项；只根据 detail_points、招标要求和专业逻辑生成其二级及以下目录。'
629:    : '每个 branch 的 mappings 必须在该分支的 score_item_level 层级生成对应节点。';
```

`detail_points` 的用途被明确限定（`:642`）：

```javascript
642:  4. mappings 中的 target_title 是评分项对应节点标题，必须基本保持评分大项的专业表述；detail_points 主要用于生成其下级目录。
```

### 2.2 JSON 框架由代码预先拼接 —— 确认存在

**每阶段宿主预写 `outline.json`，Agent 只做覆盖**。`:1093-1097`（评分规划阶段输入）：

```javascript
1093:      : [
1094:          { path: OUTLINE_OUTPUT_FILE, content: JSON.stringify({ outline: lockedRoots }, null, 2) },
1095:          { path: '技术评分信息.md', content: storedPlan.techRequirements || '' },
1096:          ...knowledgeFiles,
1097:        ],
```

`:928-940`（子目录阶段输入，含预置的叶子分配文件）：

```javascript
928:      files: [
929:        { path: OUTLINE_OUTPUT_FILE, content: JSON.stringify({ outline: lockedRoots }, null, 2) },
930:        {
931:          path: LEAF_ALLOCATION_FILE,
932:          content: JSON.stringify({
933:            mode: targetLeafCount === null ? 'agent-decides' : 'allocated',
934:            target_ai_leaf_count: targetLeafCount,
935:            fixed_ai_leaf_count: fixedAiLeafCount,
936:            allocatable_ai_leaf_count: allocatedAiLeafCount,
937:            allocations,
938:          }, null, 2),
939:        },
940:      ],
```

`:958-969`（审核阶段输入，含确定性审核 context）：

```javascript
958:    return {
959:      stage: 'outline_review',
960:      message: 'Agent 正在审核并修复目录',
961:      prompt: noTechnicalScoreMode
962:        ? createNoTechnicalScoreReviewPrompt({ targetLeafCount, actualLeafCount, originalOnly })
963:        : createOutlineReviewPrompt({ targetLeafCount, actualLeafCount, allowRootChanges }),
964:      files: [
965:        { path: OUTLINE_OUTPUT_FILE, content: JSON.stringify(finalOutline, null, 2) },
966:        ...(!noTechnicalScoreMode ? [{ path: SCORE_DIRECTORY_PLAN_FILE, content: JSON.stringify(scoreDirectoryPlan, null, 2) }] : []),
967:        { path: OUTLINE_REVIEW_CONTEXT_FILE, content: JSON.stringify(reviewContext, null, 2) },
968:      ],
969:    };
```

---

### 2.3 二三级生成前的「叶子数分配」中间阶段

`:1172-1193`（多分支才触发）：

```javascript
1172:        if (allocatedAiLeafCount !== null && technicalBranches.length > 1) {
1173:          publish('技术方案目录已确认，Agent 正在分配 AI 生成小节', 50);
1174:          return {
1175:            stage: 'leaf_allocation',
1176:            message: 'Agent 正在分配 AI 生成小节',
1177:            prompt: createLeafAllocationPrompt({ standaloneTechnical }),
1178:            files: [{
1179:              path: LEAF_ALLOCATION_CONTEXT_FILE,
1180:              content: JSON.stringify({
1181:                mode: 'allocated',
1182:                target_ai_leaf_count: targetLeafCount,
1183:                fixed_ai_leaf_count: fixedAiLeafCount,
1184:                allocatable_ai_leaf_count: allocatedAiLeafCount,
1185:                technical_branches: technicalBranches,
1186:              }, null, 2),
1187:            }],
1188:          };
1189:        }
1190:        const allocations = allocatedAiLeafCount === null
1191:          ? technicalBranches.map((branch) => ({ branch_id: branch.branch_id }))
1192:          : [{ branch_id: technicalBranches[0].branch_id, leaf_count: allocatedAiLeafCount }];
1193:        return continueWithChildrenGeneration(allocations);
```

`:1196-1199`（分配结果读取并推进）：

```javascript
1196:      if (meta.workflow_stage === 'leaf_allocation') {
1197:        const allocationPayload = readJson(await meta.readFile(LEAF_ALLOCATION_FILE), LEAF_ALLOCATION_FILE);
1198:        return continueWithChildrenGeneration(allocationPayload.allocations);
1199:      }
```

分配约束 `createLeafAllocationPrompt` `:581-582`：

```javascript
581:  3. allocations 必须恰好覆盖 context 中 technical_branches 的全部 branch_id，每个 branch_id 只出现一次。${allocationInstruction}branch_id 是不会因目录重新编号而变化的内部稳定标识。
582:  4. 所有 leaf_count 之和必须等于 allocatable_ai_leaf_count。
```

⚠️ 分配仅在多分支时触发（`:1172` `technicalBranches.length > 1`），单分支直接兜底（`:1190-1192`）。

### 2.4 唯一的真并发：目录生成 ∥ 投标模版提取（`:1055-1079`）

```javascript
1055:  const parallelController = new AbortController();
1056:  const parallelSignal = AbortSignal.any([taskControl.signal, parallelController.signal]);
1057:  let firstParallelFailure = null;
1058:  const observeParallelBranch = (label, promise) => promise.catch((error) => {
1059:    if (!firstParallelFailure && !taskControl.signal.aborted) {
1060:      firstParallelFailure = { label, error };
1061:      const reason = new Error(`${label}失败，已取消同级任务`);
1062:      reason.code = 'TASK_CANCELLED';
1063:      parallelController.abort(reason);
1064:    }
1065:    throw error;
1066:  });
```

仅开发者模式触发（`:1018-1019`）：

```javascript
1018:  const extractTemplate =
1019:    !standaloneTechnical && Boolean(aiService?.isDeveloperMode?.());
```

---

## 3. ⚠️ `collect_json_response` 在目录链路中的用法 —— 重要否定发现

**目录生成链路零次调用 `collectJsonResponse`。**

全仓调用点检索结果（`client/electron/**/*.cjs`）中，**`outlineGenerationTaskV2.cjs` 与 `outlineAdjustmentTask.cjs` 均不在列表内**。`collectJsonResponse` 只被正文生成（`contentGenerationTask.cjs`）、知识库（`knowledgeBaseService.cjs`）、全局事实（`globalFactsTask.cjs`）、废标检查（`rejectionCheckTask.cjs`）使用。

目录链路走的是**完全不同的执行栈**（见第 0 节）。

### 3.1 目录链路的实际重试次数：**0**

`outlineGenerationTaskV2.cjs:988`（一级目录阶段）：

```javascript
985:      initial_stage: 'initial-outline',
986:      initial_stage_index: 0,
987:      json_validation_schemas: jsonValidationSchemas,
988:      max_retries: 0,
989:      onActivity: publishAgentActivity,
990:      onCheckpoint: syncAgentCheckpoint,
```

`outlineGenerationTaskV2.cjs:1106`（主体阶段）：

```javascript
1103:    initial_stage: directoryStage,
1104:    initial_stage_index: 2,
1105:    json_validation_schemas: jsonValidationSchemas,
1106:    max_retries: 0,
1107:    onActivity: publishAgentActivity,
1108:    onCheckpoint: syncAgentCheckpoint,
```

`outlineAdjustmentTask.cjs:105`（AI 调整阶段）同样是 `max_retries: 0`。

重试机制由 `piRuntimeService.cjs:842` 承载，但因 `normalizeMaxRetries(0) = 0` 而**恒不触发**：

```javascript
842:      for (let attemptIndex = 0; attemptIndex <= maxRetries; attemptIndex += 1) {
...
902:          } catch (error) {
903:            if (activeController.signal.aborted) throw activeController.signal.reason || error;
904:            if (attemptIndex >= maxRetries) throw error;
905:            const output = await readOutputAsync(workspaceDir, outputFile);
906:            retryAttempts.push(createRetrySummary(retryAttempts.length + 1, error, output.content));
907:            retryCount = retryAttempts.length;
908:            touchActivity({
909:              task_token: taskToken,
910:              stage: 'retry',
911:              message: `${runtimeName} 正在自动修复：${compactText(error?.message || error, 160)}`,
...
916:            stagePrompt = buildRetryPrompt(outputFile, error, attemptIndex + 1, maxRetries);
```

**对照参考**：`collectJsonResponse` 的默认重试是 2（`aiService.cjs:832-835`）：

```javascript
832: async function collectJsonResponseWithConfig(app, config, request) {
833:   const preparedMessages = await prepareMultimodalMessages(config, request.messages);
834:   const maxRetries = request.max_retries ?? 2;
835:   const totalAttempts = maxRetries + 1;
```

且它带 **JSON 修复重问**（`:855-880`），这是目录链路**没有**的能力：

```javascript
855:    } catch (error) {
856:      lastError = error;
857:      const issues = formatJsonIssues(error);
858:
859:      try {
860:        const repairedContent = await repairJsonResponse(
861:          app, config, content, issues, responseFormat,
866:          request.progressCallback, progressLabel, request.repairMessagesBuilder, logTitle, request.signal,
871:        );
872:        const repairedParsed = parseJsonContent(repairedContent);
873:        return normalizeJsonPayload(request, repairedParsed);
874:      } catch (repairError) {
875:        lastError = repairError;
876:
877:        if (attempt === maxRetries) {
878:          await emitProgress(request.progressCallback, `${progressLabel}连续 ${totalAttempts} 次校验失败。`);
879:          throw new Error(failureMessage);
880:        }
```

---

## 4. `check_json` 校验函数 —— 源码中未找到该名称，实际是 `json-validation` 工具

**文件**：`client/electron/services/pi/piJsonValidationTool.cjs`（143 行，**GBK 编码**，读取需转码）

### 4.1 校验什么：两级 + Ajv

`execute` 三段（`:75-136`）：

| stage | 校验内容 | 行号 |
|---|---|---|
| `read` | 文件可读 + 在工作区内（防路径穿越） | `:83-90` |
| `parse` | `JSON.parse` | `:95-102` |
| `schema` | Ajv 编译（预设 Schema 优先，否则用传入 `schema`） | `:104-120` |
| `validation` | `ajv.validate` + `allErrors: true, strict: true` | `:122-129` |

Ajv 实例配置（`:53`）：

```javascript
53:  const ajv = new Ajv({ allErrors: true, strict: true });
```

预设 Schema 优先（`:106-112`）：

```javascript
106:        const schema = presetSchemas.has(relativePath)
107:          ? presetSchemas.get(relativePath)
108:          : params.schema;
109:        if (schema === undefined) {
110:          throw new Error(`任务未为 ${relativePath} 预置 Schema，调用时必须提供 schema`);
111:        }
112:        validate = ajv.compile(schema);
```

Ajv 错误归一化（`:25-33`）：

```javascript
25: function normalizeAjvErrors(errors = []) {
26:   return errors.map((error) => ({
27:     instancePath: error.instancePath || '/',
28:     schemaPath: error.schemaPath || '',
29:     keyword: error.keyword || '',
30:     message: error.message || '字段不符合 JSON Schema',
31:     params: error.params || {},
32:   }));
33: }
```

路径穿越防护（`:15-23`）：

```javascript
17:  const workspaceRoot = path.resolve(workspaceDir);
18:  const resolvedPath = path.resolve(workspaceRoot, relativePath);
19:  if (resolvedPath !== workspaceRoot && !resolvedPath.startsWith(`${workspaceRoot}${path.sep}`)) {
20:    throw new Error(`file_path 超出当前工作区：${filePath}`);
21:  }
```

### 4.2 失败如何处理：**返回 isError 交还 Agent 自修，不抛异常**

`:35-50` `createToolResult`（原文为 GBK 乱码，此处按语义还原）：

```javascript
35: function createToolResult({ filePath, valid, stage, errors = [] }) {
36:   const payload = {
37:     valid,
38:     stage,
39:     file_path: filePath,
40:     errors,
41:     message: valid
42:       ? 'JSON.parse 和 Ajv 校验均已通过。'
43:       : '校验未通过，请根据 errors 修复文件后再次调用 json-validation。',
44:   };
45:   return {
46:     content: [{ type: 'text', text: JSON.stringify(payload, null, 2) }],
47:     details: payload,
48:     ...(valid ? {} : { isError: true }),
49:   };
50: }
```

**宿主侧兜底** — `readJson` `outlineGenerationTaskV2.cjs:491-497`（真正的失败哨兵处理）：

```javascript
491: function readJson(content, label) {
492:   try {
493:     return JSON.parse(String(content || '').trim());
## 5. 目录审核（outline review）

### 5.1 SystemPrompt 逐字原文

⚠️ **本仓库无 SystemPrompt/UserPrompt 之分** —— 只有一个 `prompt` 字符串传给 Agent SDK。`createOutlineReviewPrompt` `:711-732` 逐字原文：

```javascript
711:  return `请对当前完整技术方案目录执行最终审核，并在用户确认后完成必要修复。
712:
713: 开始审核时一次性并行读取 ${OUTLINE_REVIEW_CONTEXT_FILE}、${OUTLINE_OUTPUT_FILE}、技术评分信息.md 和 ${SCORE_DIRECTORY_PLAN_FILE}，不要探索工作区或读取其他文件。${OUTLINE_REVIEW_CONTEXT_FILE} 是宿主程序计算的确定性审核结果，叶子数量、内容模式数量、最大层级、父节点数量、单子节点和评分节点机械映射均直接采用其中结果，不要重新统计、编写脚本或执行额外结构检查；你只负责评分语义覆盖、近义重复和专业合理性审核。
714:
715: 审核维度：${leafCountReview}
716: - 评分覆盖：直接以技术评分信息.md 为原始依据，逐项检查其中适合技术方案响应的评分大项是否被目录准确覆盖；结构化评分项和目录规划用于核对已确认的映射，但不能掩盖原始评分信息中的遗漏。
717: - 重复目录：检查全部子目录中是否存在重复、近义、含义重叠或仅换一种说法的节点；不同专业分支下确有独立含义的同名标题不应机械判重。
718: - 专业合理性：评估目录层级、颗粒度、逻辑顺序、标题表达、节点归属以及内容处理模式是否适合正式技术投标文件。
719:
720: 审核与修复流程：
721: 1. 必须先完整审核并形成问题清单，不得边审核边修改。
722: 2. 如果没有问题，不要修改 ${OUTLINE_OUTPUT_FILE}；写入 ${OUTLINE_REVIEW_FILE}，status=passed、issues=[]、user_feedback=""，summary 说明通过原因。
```

叶子数审核维度与一级目录约束（`:705-710`，仅当 `targetLeafCount !== null` 时注入叶子数）：

```javascript
705:  const leafCountReview = targetLeafCount === null
706:    ? ''
707:    : `\n- “AI生成”叶子数量：程序计算目标为 ${targetLeafCount} 个，当前为 ${actualLeafCount} 个，可接受范围为 ${Math.max(1, targetLeafCount - 2)} 至 ${targetLeafCount + 2} 个。只统计 content_mode=ai-generate 的最终叶子节点；修复后仍须保持在此范围内。`;
708:  const rootRequirement = allowRootChanges
709:    ? `一级目录只能保持用户已批准的 ${SCORE_DIRECTORY_PLAN_FILE} 规划，不得提出规划之外的新调整。`
710:    : '一级目录已经由用户确认，数量、顺序、标题、描述和属性不得修改。';
```

`:724-729`（审批门与回灌规则）：

```javascript
724:  4. 只有以下问题可设 confirmation_required=false：标题或说明的专业化优化；不涉及评分项映射节点、目标层级和一级目录的明显重复或近义子目录合并。评分覆盖缺失、叶子数量超出合理范围、评分项目标层级调整、一级目录调整、增加或拆分目录、跨分支移动以及明显结构重排均必须设为 true。
725:  5. 如果全部问题都不需要确认，可以直接执行文案优化或轻微去重，不得调用 ask-user；完成后设置 status=simple_fix、user_feedback=""。静默修复不得改变评分项映射、评分项目标层级和一级目录，且不得使 AI 生成叶子数量超出程序给出的合理范围。
726:  6. 只要存在一个 confirmation_required=true 的问题，本轮所有问题都不得提前修改。集中调用一次 ask-user，question 使用多行文本完整列出问题及推荐修复方案；提供 2 至 5 个互斥选项，第一项是推荐修复方案，并提供保留当前目录的选项；另提供一个名为“调整修复方案”等明确业务名称的选项并设置 custom=true，让用户说明具体修改要求，其他选项均设置 custom=false。custom=true 的选项最多只能有一个。
727:  7. 根据 ask-user 返回的 answer 执行最终处理：用户要求全部或部分修改时更新 ${OUTLINE_OUTPUT_FILE} 并设置 status=user_feedback；用户明确拒绝修改或要求保留现状时不得修改目录并设置 status=user_refuse。将 answer 原文完整写入 user_feedback，修改完成后不得再次询问用户。
```

`:730-732`（边界与输出契约）：

```javascript
730:  10. 技术一级目录必须保留 ${SCORE_DIRECTORY_PLAN_FILE} 中对应的 branch_id；调整一级目录顺序或编号时不得修改 branch_id。结构事实以 ${OUTLINE_REVIEW_CONTEXT_FILE} 为准；如果其中确定性检查不通过，直接依据列出的节点和缺失项形成问题并修复，不要重新统计。任何语义修复仍必须保证叶子保留合法 content_mode、父节点不包含 content_mode 或 content_mode_note、父节点至少有两个 children 且目录最多六级。
731:  11. 最终将完整问题清单和处理结果写入 ${OUTLINE_REVIEW_FILE}。无问题时完整格式为 {"status":"passed","issues":[],"user_feedback":"","summary":"审核通过原因"}；有问题时完整格式为 {"status":"user_feedback","issues":[{"category":"score-coverage","problem":"问题说明","repair":"修复方案","confirmation_required":true}],"user_feedback":"用户回答原文","summary":"处理结果"}。category 只能是 leaf-count、score-coverage、duplicate-directory、professional-structure；status 按本流程选择 passed、simple_fix、user_feedback 或 user_refuse。
732:  12. 程序已为 ${OUTLINE_OUTPUT_FILE} 和 ${OUTLINE_REVIEW_FILE} 预置 Schema。分别调用 json-validation 校验，只传 file_path；校验失败后必须先修改文件，再重新校验。`;
```

---

494:   } catch (error) {
495:     throw new Error(`${label}不是合法 JSON：${error?.message || String(error)}`);
496:   }
497: }
```

⚠️ **`readJson` 只保证语法合法，不做 Schema 校验** —— Schema 校验完全依赖 Agent 自觉调用工具。宿主从不主动 validate。

### 4.3 工具描述原文（`:59-74`）

```javascript
59:    name: JSON_VALIDATION_TOOL_NAME,
60:    label: 'JSON 校验',
61:    description: '使用 JSON.parse 和 Ajv 校验当前工作区内的 JSON 文件。任务已预置 Schema 时只传 file_path；没有预置时根据输出要求提供完整 schema。校验失败后修复文件并再次调用。',
62:    promptSnippet: '使用 JSON.parse 和 Ajv 校验工作区内的 JSON 文件。',
```

---

### 5.2 返回结构：⚠️ **不是 `{passed, suggestions}`**

实际 Schema `OUTLINE_REVIEW_SCHEMA` `:157-183`：

```javascript
157: const OUTLINE_REVIEW_SCHEMA = {
158:   type: 'object',
159:   required: ['status', 'issues', 'user_feedback', 'summary'],
160:   additionalProperties: false,
161:   properties: {
162:     status: { type: 'string', enum: ['passed', 'simple_fix', 'user_feedback', 'user_refuse'] },
163:     issues: {
164:       type: 'array',
165:       items: {
166:         type: 'object',
167:         required: ['category', 'problem', 'repair', 'confirmation_required'],
168:         additionalProperties: false,
169:         properties: {
170:           category: {
171:             type: 'string',
172:             enum: ['leaf-count', 'score-coverage', 'duplicate-directory', 'professional-structure'],
173:           },
174:           problem: { type: 'string', minLength: 1 },
175:           repair: { type: 'string', minLength: 1 },
176:           confirmation_required: { type: 'boolean' },
177:         },
178:       },
179:     },
180:     user_feedback: { type: 'string' },
181:     summary: { type: 'string', minLength: 1 },
182:   },
183: };
```

| 任务书假设 | 实际 |
|---|---|
| `passed`（布尔） | `status` 枚举四值 |
| `suggestions`（数组） | `issues[]`，每项含 `category/problem/repair/confirmation_required` |

### 5.3 不通过如何回灌重生成

**审核不触发独立重生成循环**。流程是「审核 = 最后阶段，审核 + 修复在同一次 Agent 运行内完成」。见 `:1110-1146`：

```javascript
1110:      if (meta.workflow_stage === 'outline_review') {
1111:        const reviewedOutline = readJson(candidate.output_content, OUTLINE_OUTPUT_FILE);
1112:        const normalizedReviewedOutline = buildFinalOutline(reviewedOutline);
1113:        outlineReview = readJson(await meta.readFile(OUTLINE_REVIEW_FILE), OUTLINE_REVIEW_FILE);
1114:        finalOutline = normalizedReviewedOutline;
1115:        if (!noTechnicalScoreMode) {
1116:          scoreDirectoryPlan = synchronizeScoreDirectoryPlan(scoreDirectoryPlan, finalOutline.outline);
1117:        }
1118:        actualLeafCount = countAiLeaves(finalOutline.outline);
1119:        await meta.writeFiles([
1120:          { path: OUTLINE_OUTPUT_FILE, content: JSON.stringify(finalOutline, null, 2) },
1121:          ...(!noTechnicalScoreMode ? [{ path: SCORE_DIRECTORY_PLAN_FILE, content: JSON.stringify(scoreDirectoryPlan, null, 2) }] : []),
1122:        ]);
...
1145:        return { complete: true };
1146:      }
```

`status` 仅用于**日志文案**，不改变控制流（`:1130-1136`）：

```javascript
1130:        const reviewMessage = outlineReview.status === 'passed'
1131:          ? '目录审核通过'
1132:          : outlineReview.status === 'simple_fix'
1133:            ? '目录审核完成，Agent 已自动微调简单问题'
1134:            : outlineReview.status === 'user_feedback'
1135:              ? '目录审核完成，已按用户反馈修复'
1136:              : '目录审核完成，用户选择保留当前目录';
```

⚠️ **`outlineReview` 变量（`:822` 声明）在流程中只被赋值和用于日志，从未阻断或回灌**。

---

### 5.4 审核的确定性前置：宿主计算 context（`:464-489`）

```javascript
464: function buildOutlineReviewContext({ outline, scoreDirectoryPlan, targetLeafCount }) {
465:   const items = outline?.outline || [];
466:   const leafCounts = countLeavesByMode(items);
467:   const structure = collectOutlineStructure(items);
468:   const acceptableMin = targetLeafCount === null ? null : Math.max(1, targetLeafCount - 2);
469:   const acceptableMax = targetLeafCount === null ? null : targetLeafCount + 2;
470:   return {
471:     leaf_count: {
472:       target: targetLeafCount,
473:       current_ai_generate: leafCounts[AI_CONTENT_MODE],
474:       acceptable_min: acceptableMin,
475:       acceptable_max: acceptableMax,
476:       within_acceptable_range: targetLeafCount === null
477:         ? true
478:         : leafCounts[AI_CONTENT_MODE] >= acceptableMin && leafCounts[AI_CONTENT_MODE] <= acceptableMax,
479:       by_content_mode: leafCounts,
480:     },
481:     structure: {
482:       ...structure,
483:       valid: structure.max_depth <= 6
484:         && structure.single_child_nodes.length === 0
485:         && structure.invalid_leaf_content_modes.length === 0,
486:     },
487:     ...(scoreDirectoryPlan ? { score_mapping: collectScoreMappingCoverage(items, scoreDirectoryPlan) } : {}),
488:   };
489: }
```

**设计要点**（提示词 `:713` 明确要求 Agent 不得自行统计）：宿主把「叶子数量 / 内容模式 / 最大层级 / 父节点 / 单子节点 / 评分映射」的机械检查结果算好，Agent 只做语义审核。

结构统计实现 `collectOutlineStructure` `:380-404`：

```javascript
387:  const visit = (nodes, depth) => {
388:    (nodes || []).forEach((item) => {
389:      result.max_depth = Math.max(result.max_depth, depth);
390:      const children = Array.isArray(item?.children) ? item.children : [];
391:      if (children.length) {
392:        result.parent_count += 1;
393:        if (children.length < 2) {
394:          result.single_child_nodes.push({ id: item.id, title: item.title, child_count: children.length });
395:        }
396:        visit(children, depth + 1);
397:      } else if (!CONTENT_MODES.includes(item?.content_mode)) {
398:        result.invalid_leaf_content_modes.push({ id: item.id, title: item.title, content_mode: item?.content_mode || '' });
399:      }
400:    });
401:  };
402:  visit(items, 1);
```

### 5.5 无评分项模式的独立审核提示词（`:680-702`）

```javascript
684:  return `请对当前无技术评分项模式生成的完整技术方案目录执行最终审核，并在用户确认后完成必要修复。
685:
686: 开始审核时并行读取 ${OUTLINE_REVIEW_CONTEXT_FILE} 和 ${OUTLINE_OUTPUT_FILE}，不要探索工作区或读取评分相关文件。用户已确认招标文件没有技术评分项，不要判断、补造或检查评分项。
687:
688: 审核维度：${leafCountReview}
689: - 重复目录：检查子目录中是否存在重复、近义或含义重叠的节点。
690: ${originalOnly
691:     ? `- 来源与完整性：读取原方案.md，核对目录是否忠实覆盖原方案中的章节。${ORIGINAL_ONLY_DIRECTORY_RULE}不得以缺少通用技术主题为由新增目录。`
692:     : '- 专业合理性：检查目录是否覆盖项目实施所需的通用技术主题，层级、颗粒度、逻辑顺序、标题和内容处理模式是否适合正式投标文件。\n- 事实边界：专业经验只能补充通用目录结构，不得编造具体项目事实、参数、业绩或承诺。'}
693:
694: 审核与修复流程：
695: 1. 必须先完整审核并形成问题清单，不得边审核边修改。
696: 2. 没有问题时不要修改 ${OUTLINE_OUTPUT_FILE}；写入 ${OUTLINE_REVIEW_FILE}，status=passed、issues=[]、user_feedback=""。
697: 3. 仅标题专业化和明显重复子目录合并可静默修复并设置 status=simple_fix；一级目录调整、增加或拆分目录、明显结构重排和叶子数量超出合理范围必须先集中调用一次 ask-user。
698: 4. 用户要求修改时设置 status=user_feedback；用户要求保留现状时不修改目录并设置 status=user_refuse。修改完成后不得再次询问。
699: 5. 用户已确认的一级目录数量、顺序、标题、描述和属性不得修改。所有叶子保留合法 content_mode，父节点至少有两个 children，目录最多六级，id 与实际父子位置一致。
700: 6. issues 的 category 只能使用 leaf-count、duplicate-directory 或 professional-structure。最终将完整问题清单和处理结果写入 ${OUTLINE_REVIEW_FILE}。
701: 7. 程序已为 ${OUTLINE_OUTPUT_FILE} 和 ${OUTLINE_REVIEW_FILE} 预置 Schema。分别调用 json-validation 校验，只传 file_path；校验失败后必须先修改文件，再重新校验。`;
```

⚠️ 注意 `:700` —— 无评分项模式下 **`score-coverage` 类别被禁用**。

---

## 6. 目录可编辑性（`OutlineEditPage.tsx`，1586 行）

**文件**：`client/src/features/technical-plan/pages/OutlineEditPage.tsx`

### 6.1 保存统一入口（`:742-761`）—— **三步不破结构**

```javascript
742:  const saveOutlineChange = async (outline: OutlineItem[], reason: SaveOutlineRequest['reason'], affectedNodeIds: string[] = []) => {
743:    if (!outlineData) {
744:      return;
745:    }
746:    const lockMessage = getMutationLockMessage();
747:    if (lockMessage) {
748:      showToast(lockMessage, 'info');
749:      return;
750:    }
751:
752:    const normalizedOutline = normalizeOutlineContentModes(outline);
753:    assertLeafContentModes(normalizedOutline);
754:    const renumbered = renumberOutlineItemsWithIdMap(normalizedOutline);
755:    await onOutlineSaved({
756:      outlineData: { ...outlineData, outline: renumbered.outline },
757:      reason,
758:      idMap: renumbered.idMap,
759:      affectedNodeIds,
760:    });
761:  };
```

**① 结构规整** `normalizeOutlineContentModes` `:194-213`：

```javascript
194: function normalizeOutlineContentModes(items: OutlineItem[]): OutlineItem[] {
195:   return items.map((item) => {
196:     if (item.children?.length) {
197:       const branch = { ...item };
198:       delete branch.content_mode;
199:       delete branch.content_mode_note;
200:       return { ...branch, children: normalizeOutlineContentModes(item.children) };
201:     }
202:     const leaf = { ...item };
203:     delete leaf.children;
204:     const contentMode = item.content_mode;
205:     return {
206:       ...leaf,
207:       content_mode: contentMode,
208:       ...(contentMode === 'other' && item.content_mode_note?.trim()
209:         ? { content_mode_note: item.content_mode_note.trim() }
210:         : { content_mode_note: undefined }),
211:     };
212:   });
213: }
```

**② 硬断言** `assertLeafContentModes` `:215-223`：

```javascript
215: function assertLeafContentModes(items: OutlineItem[]) {
216:   items.forEach((item) => {
217:     if (item.children?.length) {
218:       assertLeafContentModes(item.children);
219:     } else if (!item.content_mode) {
220:       throw new Error(`目录“${item.title}”缺少内容处理模式，请重新生成目录`);
221:     }
222:   });
223: }
```

**③ 重编号 + idMap** `renumberOutlineItemsWithIdMap` `:174-191`：

```javascript
174: function renumberOutlineItemsWithIdMap(items: OutlineItem[], parentPrefix = ''): RenumberResult {
175:   const idMap: Record<string, string> = {};
176:   const outline = items.map((item, index) => {
177:     const id = parentPrefix ? `${parentPrefix}.${index + 1}` : `${index + 1}`;
178:     const childResult = item.children?.length ? renumberOutlineItemsWithIdMap(item.children, id) : null;
179:     idMap[item.id] = id;
180:     if (childResult) {
181:       Object.assign(idMap, childResult.idMap);
182:     }
183:     return {
184:       ...item,
185:       id,
186:       children: childResult?.outline,
187:     };
188:   });
189:
190:   return { outline, idMap };
191: }
```

`idMap` 用于**正文迁移**（旧 id → 新 id），`composeIdMap` `:235-237` 做链式映射：

```javascript
235: function composeIdMap(baseMap: Record<string, string>, stepMap: Record<string, string>) {
236:   return Object.fromEntries(Object.entries(baseMap).map(([oldId, currentId]) => [oldId, stepMap[currentId] || currentId]));
237: }
```

### 6.2 编辑保存：清空受影响节点正文（`:775-795`）

```javascript
775:  const saveEditing = async () => {
776:    if (!outlineData || !editingItemId || sorting || outlineMutationLocked) {
777:      return;
778:    }
779:
780:    try {
781:      await saveOutlineChange(updateOutlineItem(outlineData.outline, editingItemId, (item) => ({
782:        ...item,
783:        title: editTitle.trim() || item.title,
784:        description: editDescription.trim(),
785:        ...(!item.children?.length ? {
786:          content_mode: editContentMode,
787:          content_mode_note: editContentMode === 'other' ? editContentModeNote.trim() || undefined : undefined,
788:        } : {}),
789:      })), 'edit', [editingItemId]);
790:      setEditingItemId(null);
791:      showToast('目录项已更新，相关正文已清空', 'success');
```

⚠️ **父节点（children 非空）不可改 content_mode**（`:785` 的 `!item.children?.length` 守卫）—— 保证「父节点不得含 content_mode」的结构不变式。

### 6.3 拖拽排序（`:1017-1022`）

```javascript
1017:    const reordered = reorderOutlineSiblings(draftOutlineData.outline, sourceLocation.parentId, draggingItemId, item.id, position);
1018:    const renumbered = renumberOutlineItemsWithIdMap(reordered);
1019:    sortIdMapRef.current = composeIdMap(sortIdMapRef.current, renumbered.idMap);
1020:    setDraftOutlineData({ ...draftOutlineData, outline: renumbered.outline });
1021:    setExpandedItems((prev) => new Set([...prev].map((id) => renumbered.idMap[id] || id)));
1022:    setSelectedItemId((prev) => (prev ? renumbered.idMap[prev] || prev : prev));
```

`:973` 拖拽层级限制（**只允许同级**）：

```javascript
973:      return Boolean(dragged && target && dragged.parentId === target.parentId && dragged.level === target.level);
```

---

### 6.4 落库侧的正文失效策略（`technicalPlanStore.cjs:2244-2293`）

```javascript
2244:  function saveOutline(payload) {
2245:    const request = payload?.outlineData ? payload : { outlineData: payload, reason: 'replace' };
2246:    const outlineData = request?.outlineData;
2247:    const reason = normalizeOutlineSaveReason(request?.reason);
2248:    const idMap = normalizeStringMap(request?.idMap);
2249:    const reverseMap = reverseIdMap(idMap);
2250:    const affectedIds = normalizeStringSet(request?.affectedNodeIds);
2251:    const clearAll = reason === 'replace';
2252:    const invalidatesContentTask = reason !== 'sort';
...
2256:    const transaction = db.transaction(() => {
2257:      assertOutlineMutationAllowed();
2258:      if (reason === 'sort') {
2259:        saveSortedOutline(outlineData, idMap);
2260:        savedIllustrationPlan = loadContentIllustrationPlan();
2261:        return;
2262:      }
2263:      const snapshot = loadOutlinePersistenceSnapshot();
2264:      const outlineToSave = buildOutlineWithPersistedContent(outlineData, { snapshot, reverseMap, affectedIds, clearAll });
2265:      savedOutlineData = outlineToSave;
2266:      saveOutlineData(outlineToSave);
...
2272:      restoreMappedContentRows({ snapshot, idMap, affectedIds, nextIds, clearAll });
2273:      if (invalidatesContentTask) {
2274:        db.prepare("DELETE FROM technical_plan_tasks WHERE type = 'content-generation'").run();
2275:        clearTechnicalPlanMermaidCache();
2276:        updateMeta({ content_generation_runtime_json: null });
2277:      }
2278:      clearContentIllustrationPlan();
2279:    });
```

`reason` 值域（`technicalPlanStore.cjs:389-392`）：

```javascript
389: const outlineSaveReasons = new Set(['sort', 'edit', 'delete', 'add-root', 'add-child', 'replace']);
391: function normalizeOutlineSaveReason(value) {
392:   return outlineSaveReasons.has(value) ? value : 'replace';
```

| reason | 语义 | 正文处理 |
|---|---|---|
| `sort` | 仅排序 | 正文全部保留（`idMap` 迁移） |
| `edit`/`delete`/`add-root`/`add-child` | 结构变更 | 按 `affectedNodeIds` 定点清空 + 正文生成任务删除 |
| `replace` | AI 调整/重新生成 | `clearAll` 全清 |

⚠️ **前端无深度上限校验**（`MAX_DEPTH=6` 只存在于后端 Schema `:40` 与 `collectOutlineStructure :483`），但 `addChildItem`（`:837`）走的是同一条 `saveOutlineChange` 管线；**纯手工新增路径则无深度守卫**。

---

## 7. 全部提示词原文（逐字摘录）

### 7.1 提示词清单

| # | 函数 | 行号 | 触发阶段 | 模式 |
|---|---|---|---|---|
| P1 | `createInitialPrompt` | `514-547` | 一级目录生成 | 三态 |
| P2 | `createLeafAllocationPrompt` | `572-587` | 叶子数分配 | 有评分项 |
| P3 | `createScorePlanningPrompt` | `591-613` | 评分项结构化+规划 | 有评分项 |
| P4 | `createChildrenPrompt` | `615-655` | 二三级生成 | 有评分项 |
| P5 | `createLeafAdjustmentPrompt` | `657-677` | 叶子数差异调整 | 双态 |
| P6 | `createOutlineReviewPrompt` | `704-733` | 最终审核 | 有评分项 |
| P7 | `createNoTechnicalScoreChildrenPrompt` | `550-570` | 无评分项·子目录 | 无评分项 |
| P8 | `createNoTechnicalScoreReviewPrompt` | `680-702` | 无评分项·审核 | 无评分项 |
| P9 | `createOutlineAdjustmentPrompt` | `outlineAdjustmentTask.cjs:33-47` | AI 调整目录 | 复用会话 |

⚠️ **本仓库无 SystemPrompt / UserPrompt 二元结构** —— 每个函数返回**单一 prompt 字符串**，经 `agentService.runTask({ prompt })` 传入 Agent SDK。知识库文件（`技术评分信息.md` / `原方案.md` / `参考知识库/*.md`）通过 `files` 参数注入工作区，由 Agent 自行读取。

### P1 · `createInitialPrompt` 主体（`:530-546`）

```javascript
530:  return `请只在当前工作目录内工作。
531:
532: 任务：
533: ${goal}
534: ${taskInstruction}
535:
536: 请生成一级目录 JSON，并将结果写入 ${OUTLINE_OUTPUT_FILE}。
537:
538: 字段要求：
539: 1. 顶层必须是对象，唯一字段 outline 是一级目录数组；此阶段暂时不要生成 children。
540: 2. 一级目录 id 是从 1 开始且不重复的连续序号字符串。
541: 3. title 必须是可直接用于投标文件目录的正式标题，不得包含“附件1”“附件一”“第一章”等编号或前缀。
542: 4. description 是目录说明。
543: 5. attr 必须从“通用”“商务”“资信”“技术”“其他”中选择。
544: ${modeRequirements}
545: 8. ${OUTLINE_OUTPUT_FILE} 必须是纯 JSON，不包含 Markdown 代码块或解释文字。
546: 9. 程序已为 ${OUTLINE_OUTPUT_FILE} 预置 Schema。写入后调用 json-validation，只传 {"file_path":"${OUTLINE_OUTPUT_FILE}"}；校验失败后必须先修改文件，再重新校验。`;
```

`goal` 三态（`:515-519`）：

```javascript
515:  const goal = standaloneTechnical
516:    ? noTechnicalScoreMode
517:      ? '我们的目标是为没有技术评分项的单独装订技术文件准备一级目录。'
518:      : '我们的目标是为单独装订的技术文件准备一级目录。一级目录必须直接对应技术评分大项。'
519:    : '我们的目标是为编写响应文件/投标文件准备一级目录。';
```

`modeRequirements` 三态（`:520-529`，节选关键分支）：

```javascript
520:  const modeRequirements = standaloneTechnical
521:    ? noTechnicalScoreMode
522:      ? `6. 本模式只生成技术文件独立分册：一级目录必须是适合展开技术正文的专业主题，attr 必须为“技术”，content_mode 必须为 ai-generate。
523:  7. 招标文件已确认没有可用的技术评分项。优先采用已有资料中的明确要求；资料没有给出目录结构时，根据项目类型和专业经验补充通用、合理的技术方案主题，但不得编造具体项目事实、参数、业绩或承诺。
524:  8. 不得创建“技术方案”“项目管理方案”“监理大纲”“监理大纲（暗标）”“施工组织设计”“技术标”等外层总目录，也不得加入商务、资信、投标函、授权委托书等非技术章节。`
525:      : `6. 本模式只生成技术文件独立分册：只能保留适合展开技术正文的评分大项，attr 必须为“技术”，content_mode 必须为 ai-generate。
526:  7. 每个一级目录直接对应一个技术评分大项，并保持评分大项的原顺序和正式表述；不得创建“技术方案”“项目管理方案”“监理大纲”“监理大纲（暗标）”“施工组织设计”“技术标”等外层总目录，也不得加入商务、资信、投标函、授权委托书等非技术章节。
527:  8. 完整结构示例：{"outline":[{"id":"1","title":"评分大项一","description":"评分大项一的技术响应范围","attr":"技术","content_mode":"ai-generate"},{"id":"2","title":"评分大项二","description":"评分大项二的技术响应范围","attr":"技术","content_mode":"ai-generate"}]}。`
528:    : `6. 每个一级目录当前都是叶子节点，必须根据它后续应采用的内容处理方式填写 content_mode：技术方案正文使用 ai-generate；需要从招标文件提取并套用表格或格式的商务、资信材料使用 template-fill；需要在全部正文完成并确定 Word 页码后回填的点对点应答表使用 point-to-point；无法归类的特殊内容使用 other，并在 content_mode_note 说明原因。
529:  7. 完整结构示例：{"outline":[{"id":"1","title":"技术方案","description":"技术方案目录说明","attr":"技术","content_mode":"ai-generate"},{"id":"2","title":"特殊资料","description":"特殊资料目录说明","attr":"其他","content_mode":"other","content_mode_note":"说明特殊处理原因"}]}。content_mode_note 只在 content_mode=other 且确有说明时填写。`;
```

---

### P2 · `createLeafAllocationPrompt` 全文（`:572-587`）

```javascript
572: function createLeafAllocationPrompt({ standaloneTechnical = false } = {}) {
573:   const allocationInstruction = standaloneTechnical
574:     ? '优先为每个目录分配至少 2 个；总目标不足时允许部分目录分配 1 个，表示保留一级目录本身作为叶子且不生成 children。除 1 以外不得分配少于 2 个，禁止形成只有一个子节点的冗余层级。'
575:     : '每个目录至少分配 2 个。';
576:   return `请继续使用当前 Pi Session 已读取的技术评分信息、知识库、原方案和目录规划，为多个技术一级目录分配“AI生成”叶子数量。
577:
578: 要求：
579: 1. 阅读 ${OUTLINE_OUTPUT_FILE}、${TECHNICAL_SCORE_GROUPS_FILE}、${SCORE_DIRECTORY_PLAN_FILE} 和 ${LEAF_ALLOCATION_CONTEXT_FILE}。
580: 2. 综合各一级目录负责的评分项数量、评分细项数量、内容复杂度以及已读取的参考资料，合理分配 allocatable_ai_leaf_count。
581: 3. allocations 必须恰好覆盖 context 中 technical_branches 的全部 branch_id，每个 branch_id 只出现一次。${allocationInstruction}branch_id 是不会因目录重新编号而变化的内部稳定标识。
582: 4. 所有 leaf_count 之和必须等于 allocatable_ai_leaf_count。
583: 5. 将结果写入 ${LEAF_ALLOCATION_FILE}，保留 context 中的 mode、target_ai_leaf_count、fixed_ai_leaf_count 和 allocatable_ai_leaf_count。
584: 6. 不要修改 ${OUTLINE_OUTPUT_FILE}、${TECHNICAL_SCORE_GROUPS_FILE} 或 ${SCORE_DIRECTORY_PLAN_FILE}。
585: 7. 输出格式为 {"mode":"allocated","target_ai_leaf_count":20,"fixed_ai_leaf_count":1,"allocatable_ai_leaf_count":19,"allocations":[{"branch_id":"B1","leaf_count":10},{"branch_id":"B2","leaf_count":9}]}。
586: 8. 程序已为 ${LEAF_ALLOCATION_FILE} 预置 Schema。完成后调用 json-validation 校验，只传 file_path；校验失败后必须先修改文件，再重新校验。`;
587: }
```

### P3 · `createScorePlanningPrompt` 全文（`:591-613`）

```javascript
591: function createScorePlanningPrompt({ standaloneTechnical = false } = {}) {
592:   const placementInstruction = standaloneTechnical
593:     ? `4. 当前采用“技术文件独立成册”：${OUTLINE_OUTPUT_FILE} 中每个一级根节点本身就应对应一个技术评分大项。每个根节点建立一个 branch，score_item_level 固定为 1，mappings 只填写与该根标题对应的评分大项，target_title 必须与 root_title 完全一致；不得再创建“技术方案”“项目管理方案”“监理大纲”“监理大纲（暗标）”“施工组织设计”“技术标”等外层分支。
594:  5. 一级根节点与评分大项默认严格一一对应；发现缺失、重复、合并或顺序不一致时，必须作为一级目录调整向用户说明并取得批准。detail_points 只用于后续生成根节点以下的目录。`
595:     : `4. 判断技术方案位于哪些目录分支，以及每个分支内评分项对应节点应统一处于哪个层级。不同分支可以使用不同层级，不预设必须是二级目录。优先选择 attr=技术且 content_mode=ai-generate 的一级目录；template-fill、point-to-point 和 other 是特殊处理叶子，不得作为普通技术方案分支展开，除非先向用户说明并取得调整批准。
596:  5. 默认每个评分项对应一个独立同层级节点，节点标题与评分大项基本一一对应；detail_points 用于后续生成更下级目录。`;
597:   const planExample = standaloneTechnical
598:     ? `{"branches":[{"branch_id":"B1","root_id":"1","root_title":"评分大项一","score_item_level":1,"mappings":[{"requirement_id":"R1","target_title":"评分大项一"}]}],"extra_titles":[],"allow_root_changes":false}`
599:     : `{"branches":[{"branch_id":"B1","root_id":"2","root_title":"技术方案","score_item_level":2,"mappings":[{"requirement_id":"R1","target_title":"评分大项目录标题","additional_titles":["经批准拆分出的同级标题"],"adjustment_note":"用户批准的调整说明"}]}],"extra_titles":[{"branch_id":"B1","title":"经批准增加的同层级标题","reason":"增加原因"}],"allow_root_changes":false}`;
600:   return `用户已经确认最终保留的一级目录，${OUTLINE_OUTPUT_FILE} 已由程序重新整理并编号。工作区也已加入技术评分信息和用户选择的参考资料。
601:
602: 请完成技术评分项结构化和目录规划：
603: 1. 阅读 ${OUTLINE_OUTPUT_FILE}、技术评分信息.md，以及存在的原方案.md 和参考知识库目录。
604: 2. 程序已确认本任务存在技术评分项。只从技术评分信息.md 的“技术评分项”中提取适合在技术方案中一一响应、展开编写的评分大项。“技术评分要求”只能作为评分标准、扣分规则和编写约束，不得提取为评分项。
605: 3. 将评分大项写入 ${TECHNICAL_SCORE_GROUPS_FILE}，完整结构为 {"groups":[{"requirement_id":"R1","title":"评分大项","description":"关注内容","detail_points":["关键评分细项"]}]}。根对象只能包含 groups；保持原顺序、专业术语和关键评分细项，requirement_id 使用连续的 R1、R2 格式。
606: ${placementInstruction}
607:  6. 只有以下偏离需要用户批准：合并或拆分评分项、遗漏评分项对应节点、增加评分项中不存在的同层级大项、改变分支评分项目标层级，以及新增、删除、合并或调整用户已确认的一级目录。普通标题规范化和评分项下级目录扩展不需要询问。
608:  7. 存在至少一个有效评分项时，无论是否存在偏离，都必须调用一次 ask-user 让用户确认。没有偏离时，question 只说明你分析得出的技术方案所在目录和评分项所在层级，最多使用两句话且不要使用列表；存在偏离时，只补充实际需要用户批准的偏离及影响，存在多个实际确认事项时才使用简单 Markdown 分行列出。question、选项名称和选项说明不得复述、概括或改写本任务 Prompt 中的要求，只呈现你分析后确实需要用户确认的结论或不确定事项。第一项给出推荐方案；另提供一个名为“调整目录安排”等明确业务名称的选项并设置 custom=true，让用户说明希望调整的位置或层级，其他选项均设置 custom=false。
609:  8. 根据用户回答写入 ${SCORE_DIRECTORY_PLAN_FILE}。完整字段层级示例：${planExample}。branches 中每个分支填写唯一且后续保持不变的 branch_id，并用当前 ${OUTLINE_OUTPUT_FILE} 中尚未调整的一级目录编号和标题填写 root_id、root_title；统一填写 score_item_level，并让每个 requirement_id 在 mappings 中恰好出现一次。后续新增、重排或改名一级目录时，branch_id 仍用于稳定关联同一技术分支，不能随 root_id 改变；程序会在完整目录重新编号后同步 root_id 和 root_title。默认一一对应；经用户批准合并时，多个 mapping 可以使用相同 target_title；经用户批准拆分时才填写 mapping.additional_titles；合并或拆分时才填写 adjustment_note。extra_titles 必须位于根对象，经批准增加同层级大项时才写入条目，否则使用空数组。
610:  9. 默认锁定一级目录，allow_root_changes=false；只有用户明确批准一级目录调整时才设为 true。
611:  10. 程序已为 ${TECHNICAL_SCORE_GROUPS_FILE} 和 ${SCORE_DIRECTORY_PLAN_FILE} 预置 Schema。分别调用 json-validation 校验，调用时只传 file_path；校验失败后必须先修改对应文件，再重新校验。
612:  11. 此阶段不要修改 ${OUTLINE_OUTPUT_FILE}，也不要删除、清空或重命名任何任务文件。`;
613: }
```

---

### P4 · `createChildrenPrompt` 全文（`:615-655`）

```javascript
636:  return `请继续使用当前上下文，为 ${OUTLINE_OUTPUT_FILE} 生成完整目录。生成方式和处理顺序由你自主决定，但必须严格遵循评分项目录规划。
637:
638: 要求：
639: 1. ${branchInstruction}
640: 2. 以 ${TECHNICAL_SCORE_GROUPS_FILE} 为技术评分项权威清单，以 ${SCORE_DIRECTORY_PLAN_FILE} 为评分项与目录位置的权威规划。
641: 3. ${mappingInstruction} branch_id 是技术分支稳定标识：对应的最终一级目录必须保留同名 branch_id，即使一级目录新增、删除、改名、重排或重新编号也不得改变；非技术分支一级目录不要填写 branch_id。默认每个评分项形成一个独立节点；多个 mapping 使用相同 target_title 表示用户已批准合并，additional_titles 表示用户已批准将该评分项拆成多个同级节点。
642: 4. mappings 中的 target_title 是评分项对应节点标题，必须基本保持评分大项的专业表述；detail_points 主要用于生成其下级目录。
643:  5. extra_titles 是用户已批准增加的同层级大项；除此之外不得自行增加技术评分项中不存在的同层级标题。
644:  6. “技术评分要求”只能作为评分标准、扣分口径、判定规则和目录说明约束，不能生成独立评分项节点。
645:  7. ${rootInstruction}
646:  8. 未纳入评分项目录规划的一级目录和分支保持原样，不得增加子目录。
647:  9. 如果存在参考知识库或原方案，只能用于完善评分项对应节点的下级结构，不得改变评分项映射或引入未经批准的同层级大项。
648: 10. ${leafInstruction}${LEAF_ALLOCATION_FILE} 中 allocations 使用 branch_id 指向技术分支，不使用可能变化的 root_id。${standaloneLeafInstruction}评分项完整对应和目录质量优先于数量目标。
649: 11. 每个最终叶子节点必须填写 content_mode：技术方案正文为 ai-generate；从招标文件提取后按模板填写为 template-fill；需要在 Word 页码确定后回填为 point-to-point；其他特殊内容为 other，并用 content_mode_note 说明。父节点不得包含 content_mode 或 content_mode_note。
650: 12. 任意非叶子节点的 children 至少包含两个节点，不要创建只有一个子节点的冗余层级。
651: 13. 目录层级可变，但最多六级；一级目录包含 attr，子目录不包含 attr。所有 id 必须使用层级点号编号：一级为 1、2，二级为 2.1、2.2，三级为 2.1.1、2.1.2，后续层级依此类推，并与实际父子位置一致。
652: 14. title 只写纯标题，不包含章节编号或 Markdown 标记。
653: 15. ${OUTLINE_OUTPUT_FILE} 的完整结构示例：${outlineExample}。branch_id 只写在评分规划对应的技术一级目录上；示例只说明字段位置和编号方式，实际层级与标题必须按任务材料生成。
654: 16. 程序已为 ${OUTLINE_OUTPUT_FILE} 预置 Schema。直接覆盖写回该文件，完成后调用 json-validation 校验，只传 file_path；校验失败后必须先修改文件，再重新校验。`;
```

条件片段原文（`:616-635`）：

```javascript
616:  const branchInstruction = !hasOriginalPlan
617:    ? '没有原方案时，以技术评分信息.md 为主要依据生成目录。'
618:    : originalOnly
619:      ? '已选择仅使用原方案目录：以原方案.md 为主建立规划层级及以下目录，再用技术评分信息.md 补充原方案语义上确实缺失的技术要求，意思相近的内容不要重复添加。'
620:      : '已提供原方案且允许 AI 补充：以技术评分信息.md 为主，在评分项目录规划指定的层级覆盖关键大项，原方案.md 用于辅助生成更下级目录。';
621:  const leafInstruction = targetLeafCount === null
622:    ? '本次未设置总字数目标，请根据材料复杂度自主确定合理的“AI生成”叶子节点数量。'
623:    : `严格参考 ${LEAF_ALLOCATION_FILE} 中的分配，使最终完整目录合计约有 ${targetLeafCount} 个 content_mode=ai-generate 的叶子节点。`;
...
630:  const standaloneLeafInstruction = standaloneTechnical
631:    ? 'leaf_count=1 表示保留对应一级目录本身作为叶子，不得为其生成 children；leaf_count>=2 时才向下展开。'
632:    : '';
633:  const outlineExample = standaloneTechnical
634:    ? `{"outline":[{"id":"1","title":"评分大项一","description":"评分大项说明","attr":"技术","branch_id":"B1","children":[{"id":"1.1","title":"响应内容一","description":"具体响应内容","content_mode":"ai-generate"},{"id":"1.2","title":"响应内容二","description":"具体响应内容","content_mode":"ai-generate"}]}]}`
635:    : `{"outline":[{"id":"1","title":"技术应答表","description":"应答表说明","attr":"技术","content_mode":"point-to-point"},{"id":"2","title":"技术方案","description":"技术方案说明","attr":"技术","branch_id":"B1","children":[{"id":"2.1","title":"评分大项","description":"评分大项说明","children":[{"id":"2.1.1","title":"具体方案一","description":"具体方案说明","content_mode":"ai-generate"},{"id":"2.1.2","title":"具体方案二","description":"具体方案说明","content_mode":"ai-generate"}]},{"id":"2.2","title":"另一评分大项","description":"评分大项说明","content_mode":"ai-generate"}]}]}`;
```

### P5 · `createLeafAdjustmentPrompt` 全文（`:657-677`）

```javascript
657: function createLeafAdjustmentPrompt(targetLeafCount, actualLeafCount, { noTechnicalScoreMode = false, originalOnly = false } = {}) {
658:   const adjustmentBoundary = noTechnicalScoreMode
659:     ? `2. 用户已确认采用无技术评分项模式。选择调整时，只能根据现有资料和专业逻辑调整技术目录，不得编造具体项目事实、参数、业绩或承诺。
660:  3. 只通过合理调整 ai-generate 叶子的目录结构满足数量目标，不得为了凑数把 template-fill、point-to-point 或 other 改成 ai-generate，也不得改变非 AI 叶子的处理模式。
661:  4. 调整后仍须保持完整根结构 {"outline":[一级目录节点]}，id 必须使用与父子位置一致的层级点号编号；用户已确认的一级目录不得修改；父节点只含 children，不含 content_mode，叶子节点只含 content_mode，不含 children。`
662:     : `2. 用户选择“允许 Agent 自行调整”或“自定义需求”时，必须继续遵循 ${SCORE_DIRECTORY_PLAN_FILE}：不得删除、移动或改变评分项对应节点的目标层级，不得新增未经批准的同层级大项；优先调整评分项节点下面的更深层目录。
663:  3. 只通过合理调整 ai-generate 叶子的目录结构满足数量目标，不得为了凑数把 template-fill、point-to-point 或 other 改成 ai-generate，也不得改变非 AI 叶子的处理模式。
664:  4. 调整后仍须保持完整根结构 {"outline":[一级目录节点]}，id 必须使用与父子位置一致的层级点号编号；技术一级目录必须保留 ${SCORE_DIRECTORY_PLAN_FILE} 中对应的 branch_id，不能因增删、移动或重新编号而改变；父节点只含 children，不含 content_mode，叶子节点只含 content_mode，不含 children。`;
665:   return `程序计算当前完整目录共有 ${actualLeafCount} 个“AI生成”叶子节点，目标是 ${targetLeafCount} 个。
666:
667: 请先调用一次 ask-user，说明目标数、当前数、差距及目录质量影响，只能按以下顺序提供三个固定选项，不得改名、增删或调整顺序：
668: 1. “接受当前结果”，custom=false：保持当前目录并进入最终审核。
669: 2. “允许 Agent 自行调整”，custom=false：由你在不破坏目录质量的前提下合理调整一次。
670: 3. “自定义需求”，custom=true：按用户填写的具体要求调整。
671:
672: 根据本轮 ask-user 回答处理：
673: 1. 用户选择“接受当前结果”时，不要修改 ${OUTLINE_OUTPUT_FILE}。
674: ${adjustmentBoundary}
675: ${noTechnicalScoreMode && originalOnly ? ORIGINAL_ONLY_DIRECTORY_RULE : ''}
676:  5. 不要机械增加重复、空泛或近义目录。程序已为 ${OUTLINE_OUTPUT_FILE} 预置 Schema；完成调整后覆盖写回该文件，并调用 json-validation 校验，只传 file_path；校验失败后必须先修改文件，再重新校验。`;
677: }
```

---

### P7 · `createNoTechnicalScoreChildrenPrompt` 全文（`:550-570`）

```javascript
550: function createNoTechnicalScoreChildrenPrompt({ targetLeafCount, standaloneTechnical, originalOnly = false }) {
551:   const leafInstruction = targetLeafCount === null
552:     ? '本次未设置总字数目标，请根据项目复杂度自主确定合理的“AI生成”叶子节点数量。'
553:     : `最终完整目录合计应有约 ${targetLeafCount} 个 content_mode=ai-generate 的叶子节点。`;
554:   const scopeInstruction = standaloneTechnical
555:     ? '所有一级目录均属于技术文件，只生成技术方案正文相关的二级及以下目录。'
556:     : '只扩展 attr=技术 且 content_mode=ai-generate 的一级目录；其他一级目录保持原样，不得增加子目录。';
557:   return `用户已确认招标文件没有技术评分项，请直接根据确定的无技术评分项规则生成完整目录，不要判断是否存在评分项，也不要创建评分清单或评分映射。
558:
559: 请阅读 ${OUTLINE_OUTPUT_FILE}、${originalOnly ? '原方案.md' : '响应文件要求.md、项目概述.md，以及存在的原方案.md 和参考知识库目录'}，然后覆盖写回 ${OUTLINE_OUTPUT_FILE}。
560:
561: 要求：
562: 1. ${scopeInstruction}
563: 2. 一级目录的数量、顺序、id、title、description 和 attr 已由用户确认，必须保持不变；未扩展为父节点的一级目录还必须保留原 content_mode。
564: 3. ${originalOnly ? ORIGINAL_ONLY_DIRECTORY_RULE : '优先采用资料中的明确要求，并根据项目类型、实施内容和专业逻辑组织技术方案目录；资料不足时可以用专业经验补充通用、合理的章节，但不得编造具体项目事实、参数、业绩或承诺。'}
565: 4. ${leafInstruction}目录质量优先于机械凑数。
566: 5. 每个最终叶子节点必须填写 content_mode：技术方案正文为 ai-generate；从招标文件提取后按模板填写为 template-fill；需要在 Word 页码确定后回填为 point-to-point；其他特殊内容为 other，并用 content_mode_note 说明。父节点不得包含 content_mode 或 content_mode_note。
567: 6. 任意非叶子节点的 children 至少包含两个节点；目录最多六级，所有 id 使用与父子位置一致的层级点号编号。
568: 7. title 只写正式、专业的纯标题，不包含章节编号或 Markdown 标记；避免重复、近义和空泛目录。
569: 8. 程序已为 ${OUTLINE_OUTPUT_FILE} 预置 Schema。完成后调用 json-validation，只传 file_path；校验失败后必须先修改文件，再重新校验。`;
570: }
```

### P9 · `createOutlineAdjustmentPrompt` 全文（`outlineAdjustmentTask.cjs:33-47`）

```javascript
33: function createOutlineAdjustmentPrompt(requirement) {
34:   return `用户已经在最终目录基础上提出新的调整要求。程序已把当前最新的完整目录覆盖写入 ${OUTLINE_OUTPUT_FILE}（用户可能在主界面手动修改过目录，请以该文件为准，不要沿用你记忆中的旧目录）。
35:
36: 用户的调整要求：
37: ${requirement}
38:
39: 请按以下要求完成目录调整：
40: 1. 先读取 ${OUTLINE_OUTPUT_FILE}，理解当前目录结构，再严格按照用户的调整要求修改目录；与要求无关的目录保持原样，不要顺带重写。
41: 2. 修改后仍须保持完整根结构 {"outline":[一级目录节点]}：一级目录包含 attr（从"通用""商务""资信""技术""其他"中选择），子目录不包含 attr；所有 id 使用与父子位置一致的层级点号编号（一级为 1、2，二级为 2.1、2.2，依此类推）。
42: 3. 每个最终叶子节点必须填写 content_mode：技术方案正文为 ai-generate；从招标文件提取后按模板填写为 template-fill；需要在 Word 页码确定后回填为 point-to-point；其他特殊内容为 other，并用 content_mode_note 说明。父节点只包含 children，不包含 content_mode 或 content_mode_note。
43: 4. 任意非叶子节点的 children 至少包含两个节点，目录最多六级；title 只写纯标题，不包含章节编号或 Markdown 标记。
44: 5. 如果用户要求含糊或存在多种理解，选择最符合投标文件专业惯例的做法直接执行，不要调用 ask-user 反复确认；只有当要求明显违反上述结构规则且无法合理变通时，才在最终回复中说明未执行的部分及原因。
45: 6. 将调整后的完整目录覆盖写回 ${OUTLINE_OUTPUT_FILE}。程序已为该文件预置 Schema，写入后调用 json-validation，只传 {"file_path":"${OUTLINE_OUTPUT_FILE}"}；校验失败后必须先修改文件，再重新校验。
46: 7. 全部完成后，用简体中文输出一段简短的最终总结（不超过 200 字，不使用 Markdown 标题），说明本次实际做了哪些目录调整；如有未能执行的要求，一并说明原因。该总结会直接展示给用户。`;
47: }
```

⚠️ **P9 有一个与主链路不一致的点**：`buildAgentOutlineInput`（`outlineAdjustmentTask.cjs:12-31`）**剥离了 `branch_id`**，因此 AI 调整后 `branch_id` 丢失，`scoreDirectoryPlan` 的关联在调整场景下不再维护。

```javascript
12: function buildAgentOutlineInput(outlineData) {
13:   const strip = (items, root) => (items || []).map((item) => {
14:     const hasChildren = Array.isArray(item?.children) && item.children.length;
15:     return {
16:       id: String(item?.id || ''),
17:       title: String(item?.title || '').trim(),
18:       description: String(item?.description || '').trim() || String(item?.title || '').trim(),
19:       ...(root ? { attr: item?.attr } : {}),
20:       ...(hasChildren
21:         ? { children: strip(item.children, false) }
22:         : {
23:           content_mode: item?.content_mode,
24:           ...(item?.content_mode === 'other' && String(item?.content_mode_note || '').trim()
25:             ? { content_mode_note: String(item.content_mode_note).trim() }
26:             : {}),
27:         }),
28:     };
29:   });
```

落库时再次剥离（`outlineAdjustmentTask.cjs:109-121`）：

```javascript
109:  const adjustedOutline = buildFinalOutline(readJson(agentResult.output_content, OUTLINE_OUTPUT_FILE));
110:  const persistedOutline = stripOutlineInternalFields(adjustedOutline);
111:  const summary = String(agentResult.assistant_text || '').trim() || '目录已按要求调整完成。';
112:
113:  // 目录调整属于目录变更，saveOutline(replace) 会按既有规则清空旧正文与生成缓存。
114:  const saved = workspaceStore.saveOutline({
115:    outlineData: {
116:      ...persistedOutline,
117:      project_name: storedPlan.outlineData.project_name,
118:      project_overview: storedPlan.outlineData.project_overview,
119:    },
120:    reason: 'replace',
121:  });
```

---

## 8. 数据结构完整字段定义

> ⚠️ **本仓库无 Pydantic**。以下为手写 JSON Schema（Ajv）+ TypeScript 接口 + SQLite 落库行三层定义。

### 8.1 目录节点 Schema（`createDirectoryNodeSchema:19-57` + `OUTLINE_JSON_SCHEMA:61-72`）

```javascript
19: function createDirectoryNodeSchema(level, root = false) {
20:   const baseProperties = {
21:     id: { type: 'string', pattern: `^[1-9]\\d*(?:\\.[1-9]\\d*){${level - 1}}$` },
22:     title: { type: 'string', minLength: 1 },
23:     description: { type: 'string', minLength: 1 },
24:     ...(root ? {
25:       attr: { type: 'string', enum: ['通用', '商务', '资信', '技术', '其他'] },
26:       branch_id: { type: 'string', minLength: 1 },
27:     } : {}),
28:   };
29:   const baseRequired = ['id', 'title', 'description', ...(root ? ['attr'] : [])];
30:   const leafSchema = {
31:     type: 'object',
32:     required: [...baseRequired, 'content_mode'],
33:     additionalProperties: false,
34:     properties: {
35:       ...baseProperties,
36:       content_mode: { type: 'string', enum: CONTENT_MODES },
37:       content_mode_note: { type: 'string' },
38:     },
39:   };
40:   if (level < 6) {
41:     const branchSchema = {
42:       type: 'object',
43:       required: [...baseRequired, 'children'],
44:       additionalProperties: false,
45:       properties: {
46:         ...baseProperties,
47:         children: {
48:           type: 'array',
49:           minItems: 2,
50:           items: createDirectoryNodeSchema(level + 1),
51:         },
52:       },
53:     };
54:     return { oneOf: [leafSchema, branchSchema] };
55:   }
56:   return leafSchema;
57: }
```

顶层（`:61-72`）：

```javascript
61: const OUTLINE_JSON_SCHEMA = {
62:   type: 'object',
63:   required: ['outline'],
64:   additionalProperties: false,
65:   properties: {
66:     outline: {
67:       type: 'array',
68:       minItems: 1,
69:       items: ROOT_NODE_SCHEMA,
70:     },
71:   },
72: };
```

**节点字段表**：

| 字段 | 类型 | 约束 | 行号 | 出现位置 |
|---|---|---|---|---|
| `id` | string | `^[1-9]\d*(?:\.[1-9]\d*){level-1}$` | `:21` | 全部层级 |
| `title` | string | minLength 1 | `:22` | 全部层级 |
| `description` | string | minLength 1 | `:23` | 全部层级 |
| `attr` | string | enum `通用/商务/资信/技术/其他` | `:25` | **仅 root** |
| `branch_id` | string | minLength 1 | `:26` | **仅 root**（非必填） |
| `content_mode` | string | enum 4 值，**必填** | `:36` | **仅 leaf** |
| `content_mode_note` | string | 可选 | `:37` | **仅 leaf** |
| `children` | array | **minItems: 2** | `:47-51` | **仅 branch** |

**递归终止**：`level < 6` 时 `oneOf: [leaf, branch]`；`level === 6` 强制 `leafSchema`（`:56`）—— 目录**最多六级**由 Schema 层面保证。

**Schema 注册**（`:766-774`）：

```javascript
766:  const jsonValidationSchemas = {
767:    [OUTLINE_OUTPUT_FILE]: OUTLINE_JSON_SCHEMA,
768:    ...(!noTechnicalScoreMode ? {
769:      [TECHNICAL_SCORE_GROUPS_FILE]: TECHNICAL_SCORE_GROUPS_SCHEMA,
770:      [SCORE_DIRECTORY_PLAN_FILE]: SCORE_DIRECTORY_PLAN_SCHEMA,
771:      [LEAF_ALLOCATION_FILE]: createLeafAllocationSchema(standaloneTechnical ? 1 : 2),
772:    } : {}),
773:    [OUTLINE_REVIEW_FILE]: OUTLINE_REVIEW_SCHEMA,
774:  };
```

⚠️ `createLeafAllocationSchema(standaloneTechnical ? 1 : 2)`（`:771`）—— 独立成册模式允许 `leaf_count=1`。

---

### 8.2 `TECHNICAL_SCORE_GROUPS_SCHEMA`（`:74-99`）

```javascript
74: const TECHNICAL_SCORE_GROUPS_SCHEMA = {
75:   type: 'object',
76:   required: ['groups'],
77:   additionalProperties: false,
78:   properties: {
79:     groups: {
80:       type: 'array',
81:       minItems: 1,
82:       items: {
83:         type: 'object',
84:         required: ['requirement_id', 'title', 'description', 'detail_points'],
85:         additionalProperties: false,
86:         properties: {
87:           requirement_id: { type: 'string', pattern: '^R[1-9]\\d*$' },
88:           title: { type: 'string', minLength: 1 },
89:           description: { type: 'string', minLength: 1 },
90:           detail_points: {
91:             type: 'array',
92:             minItems: 1,
93:             items: { type: 'string', minLength: 1 },
94:           },
95:         },
96:       },
97:     },
98:   },
99: };
```

### 8.3 `SCORE_DIRECTORY_PLAN_SCHEMA`（`:101-155`）—— **「一一对应」的载体**

```javascript
101: const SCORE_DIRECTORY_PLAN_SCHEMA = {
102:   type: 'object',
103:   required: ['allow_root_changes', 'branches', 'extra_titles'],
104:   additionalProperties: false,
105:   properties: {
106:     allow_root_changes: { type: 'boolean' },
107:     branches: {
108:       type: 'array',
109:       minItems: 1,
110:       items: {
111:         type: 'object',
112:         required: ['branch_id', 'root_id', 'root_title', 'score_item_level', 'mappings'],
113:         additionalProperties: false,
114:         properties: {
115:           branch_id: { type: 'string', minLength: 1 },
116:           root_id: { type: 'string', pattern: '^[1-9]\\d*(?:\\.[1-9]\\d*)*$' },
117:           root_title: { type: 'string', minLength: 1 },
118:           score_item_level: { type: 'integer', minimum: 1, maximum: 6 },
119:           mappings: {
120:             type: 'array',
121:             minItems: 1,
122:             items: {
123:               type: 'object',
124:               required: ['requirement_id', 'target_title'],
125:               additionalProperties: false,
126:               properties: {
127:                 requirement_id: { type: 'string', pattern: '^R[1-9]\\d*$' },
128:                 target_title: { type: 'string', minLength: 1 },
129:                 additional_titles: {
130:                   type: 'array',
131:                   minItems: 1,
132:                   items: { type: 'string', minLength: 1 },
133:                 },
134:                 adjustment_note: { type: 'string' },
135:               },
136:             },
137:           },
138:         },
139:       },
140:     },
141:     extra_titles: {
142:       type: 'array',
143:       items: {
144:         type: 'object',
145:         required: ['branch_id', 'title', 'reason'],
146:         additionalProperties: false,
147:         properties: {
148:           branch_id: { type: 'string', minLength: 1 },
149:           title: { type: 'string', minLength: 1 },
150:           reason: { type: 'string', minLength: 1 },
151:         },
152:       },
153:     },
154:   },
155: };
```

**一一对应的三态**：

| 关系 | Schema 表达 | 检查点 |
|---|---|---|
| 一一对应 | 一个 `requirement_id` → 一个 `target_title`，`score_item_level=1` | `:593-594` 提示词约束 |
| 合并（多对一） | 多个 mapping 共享同一 `target_title` | `:609` 需用户批准 |
| 拆分（一对多） | 填 `additional_titles[]` | `:609` 需用户批准 |

⚠️ `minItems: 1` 出现在 `branches`（`:109`）、`mappings`（`:121`）、`detail_points`（`:92`）、`additional_titles`（`:131`）—— **Schema 层面禁止「评分项没有对应节点」**，但**不禁止「一个 requirement_id 映射到多个 mapping 条目」**（该约束由提示词 `:609`「让每个 requirement_id 在 mappings 中恰好出现一次」保证，非 Schema）。

⚠️ **`branch_id` 无格式约束**（仅 `minLength: 1`，`:115`）—— 提示词约定为 `B1/B2/...`（`:598`、`:585`），但 Schema 不强制。**这是专项方案改造时可利用的灵活性**（可直接用 `H1..H6` / `C1..C9` 作 branch_id）。

### 8.4 `OUTLINE_REVIEW_SCHEMA`（`:157-183`）

见第 5.2 节全文。

---

### 8.5 `createLeafAllocationSchema`（`:185-237`）—— `oneOf` 双形态

```javascript
185: function createLeafAllocationSchema(minimumLeafCount = 2) {
186:   return {
187:     oneOf: [
188:       {
189:         type: 'object',
190:         required: ['mode', 'target_ai_leaf_count', 'fixed_ai_leaf_count', 'allocatable_ai_leaf_count', 'allocations'],
191:         additionalProperties: false,
192:         properties: {
193:           mode: { type: 'string', enum: ['allocated'] },
194:           target_ai_leaf_count: { type: 'integer', minimum: 1 },
195:           fixed_ai_leaf_count: { type: 'integer', minimum: 0 },
196:           allocatable_ai_leaf_count: { type: 'integer', minimum: 1 },
197:           allocations: {
198:             type: 'array',
199:             minItems: 1,
200:             items: {
201:               type: 'object',
202:               required: ['branch_id', 'leaf_count'],
203:               additionalProperties: false,
204:               properties: {
205:                 branch_id: { type: 'string', minLength: 1 },
206:                 leaf_count: { type: 'integer', minimum: minimumLeafCount },
207:               },
208:             },
209:           },
210:         },
211:       },
212:       {
213:         type: 'object',
214:         required: ['mode', 'target_ai_leaf_count', 'fixed_ai_leaf_count', 'allocatable_ai_leaf_count', 'allocations'],
215:         additionalProperties: false,
216:         properties: {
217:           mode: { type: 'string', enum: ['agent-decides'] },
218:           target_ai_leaf_count: { type: 'null' },
219:           fixed_ai_leaf_count: { type: 'integer', minimum: 0 },
220:           allocatable_ai_leaf_count: { type: 'null' },
221:           allocations: {
222:             type: 'array',
223:             minItems: 1,
224:             items: {
225:               type: 'object',
226:               required: ['branch_id'],
227:               additionalProperties: false,
228:               properties: {
229:                 branch_id: { type: 'string', minLength: 1 },
230:               },
231:             },
232:           },
233:         },
234:       },
235:     ],
236:   };
237: }
```

| mode | `target_ai_leaf_count` | `allocatable_ai_leaf_count` | allocations 项 |
|---|---|---|---|
| `allocated` | integer ≥1 | integer ≥1 | `{branch_id, leaf_count ≥ minimumLeafCount}` |
| `agent-decides` | **null** | **null** | `{branch_id}` |

### 8.6 前端 `OutlineItem`（`client/src/shared/types/outline.ts:1-45`）—— **业务字段超集**

```typescript
1: export type OutlineContentMode = 'ai-generate' | 'template-fill' | 'point-to-point' | 'other';
2:
3: export const OUTLINE_CONTENT_MODE_LABELS: Record<OutlineContentMode, string> = {
4:   'ai-generate': 'AI生成',
5:   'template-fill': '模板填写',
6:   'point-to-point': '点对点应答表',
7:   other: '其他模式',
8: };
9:
10: export interface OutlineItem {
11:   id: string;
12:   title: string;
13:   description: string;
14:   attr?: '通用' | '商务' | '资信' | '技术' | '其他';
15:   content_mode?: OutlineContentMode;
16:   content_mode_note?: string;
17:   source_requirement_id?: string;
18:   source_requirement_title?: string;
19:   knowledge_item_ids?: string[];
20:   children?: OutlineItem[];
21:   content?: string;
22: }
23:
24: export type OutlineMode = 'aligned' | 'response-file' | 'standalone-technical';
25: export type OutlineExpansionMode = 'original-only' | 'ai-complement';
26:
27: export interface OutlineWordControlOptions {
28:   minimumWords: number;
29:   maximumWords: number;
30:   sectionWords: number;
31:   strictSectionWords: boolean;
32: }
```

⚠️ **`source_requirement_id` / `source_requirement_title` / `content` / `knowledge_item_ids` 是后端目录 Schema 中不存在的字段** —— 评分项回溯锚点与正文，由下游（内容生成）回填，**不在目录生成阶段写入**。

`OutlineData`（`:41-45`）：

```typescript
41: export interface OutlineData {
42:   outline: OutlineItem[];
43:   project_name?: string;
44:   project_overview?: string;
45: }
```

---

### 8.7 一级目录选择态（`client/src/features/technical-plan/types.ts:16-47`）

```typescript
16: export type SaveOutlineReason = 'sort' | 'edit' | 'delete' | 'add-root' | 'add-child' | 'replace';
17: export type OutlineAttribute = '通用' | '商务' | '资信' | '技术' | '其他';
...
27: export interface OutlineSelectionItem {
28:   id: string;
29:   title: string;
30:   description: string;
31:   attr: OutlineAttribute;
32:   content_mode: OutlineContentMode;
33:   content_mode_note?: string;
34: }
35:
36: export interface OutlineSelectionState {
37:   items: OutlineSelectionItem[];
38:   selected_ids: string[];
39:   confirmed: boolean;
40:   auto_answer_at?: string;
41: }
42:
43: export interface SaveOutlineSelectionRequest {
44:   taskId: string;
45:   items: OutlineSelectionItem[];
46:   selectedIds: string[];
47: }
```

⚠️ `OutlineSelectionItem` **不含 `branch_id`** —— 因为选择阶段（`:974-1014`）早于 `score-planning` 阶段，`branch_id` 尚未生成。

### 8.8 落库行（`technicalPlanStore.cjs:351-374`）

```javascript
351: function flattenOutlineItems(items, parentNodeId = null, level = 1, rows = []) {
352:   (items || []).forEach((item, index) => {
353:     const nodeId = String(item?.id || '').trim();
354:     if (!nodeId) return;
355:     rows.push({
356:       node_id: nodeId,
357:       parent_node_id: parentNodeId,
358:       sort_order: index,
359:       level,
360:       title: String(item?.title || '未命名章节').trim() || '未命名章节',
361:       description: String(item?.description || '').trim(),
362:       content_mode: item?.children?.length ? null : String(item?.content_mode || '').trim() || null,
363:       content_mode_note: item?.children?.length || item?.content_mode !== 'other' ? null : String(item?.content_mode_note || '').trim() || null,
364:       source_requirement_id: item?.source_requirement_id ? String(item.source_requirement_id) : null,
365:       source_requirement_title: item?.source_requirement_title ? String(item.source_requirement_title) : null,
366:       knowledge_item_ids_json: Array.isArray(item?.knowledge_item_ids) && item.knowledge_item_ids.length ? JSON.stringify(item.knowledge_item_ids) : null,
367:       content: String(item?.content || ''),
368:     });
369:     if (item?.children?.length) {
370:       flattenOutlineItems(item.children, nodeId, level + 1, rows);
371:     }
372:   });
373:   return rows;
374: }
```

⚠️ **无 `attr` 列** —— `attr` 只存在于 JSON `outlineData` 中（`:2266` `saveOutlineData(outlineToSave)`），未平铺进关系表。

### 8.9 常量定义（`:7-17`）

```javascript
7: const DEFAULT_ESTIMATED_SECTION_WORDS = 3000;
8: const OUTLINE_OUTPUT_FILE = 'outline.json';
9: const TECHNICAL_SCORE_GROUPS_FILE = 'technical-score-groups.json';
10: const SCORE_DIRECTORY_PLAN_FILE = 'score-directory-plan.json';
11: const LEAF_ALLOCATION_FILE = 'leaf-allocation.json';
12: const LEAF_ALLOCATION_CONTEXT_FILE = 'leaf-allocation-context.json';
13: const OUTLINE_REVIEW_FILE = 'outline-review.json';
14: const OUTLINE_REVIEW_CONTEXT_FILE = 'outline-review-context.json';
15: const AI_CONTENT_MODE = 'ai-generate';
16: const ORIGINAL_ONLY_DIRECTORY_RULE = '目录来源仅限原方案.md：提取并补齐原方案中实际存在的目录，保留其顺序和层级；不得依据其他资料或专业经验新增原方案中不存在的章节，也不得为凑字数或小节数量新增、拆分章节。';
17: const CONTENT_MODES = ['ai-generate', 'template-fill', 'point-to-point', 'other'];
```

### 8.10 叶子目标数推导（`:258-288`）

```javascript
258: function deriveTargetLeafCount(options) {
259:   const sectionWords = options.sectionWords > 0 ? options.sectionWords : DEFAULT_ESTIMATED_SECTION_WORDS;
260:   if (options.minimumWords > 0 && options.maximumWords > 0) {
261:     return Math.ceil(((options.minimumWords + options.maximumWords) / 2) / sectionWords);
262:   }
263:   if (options.maximumWords > 0) {
264:     return Math.floor(options.maximumWords / sectionWords) - 2;
265:   }
266:   if (options.minimumWords > 0) {
267:     return Math.ceil(options.minimumWords / sectionWords) + 2;
268:   }
269:   return null;
270: }
271:
272: // 独立成册时每个技术分支至少保留根节点作为正文叶子；字数允许时再推荐向下展开。
273: function enforceMinimumLeafTarget(targetLeafCount, fixedAiLeafCount, technicalBranchCount, wordControlOptions = {}) {
274:   if (targetLeafCount === null) return null;
275:   const minimumLeafCount = fixedAiLeafCount + technicalBranchCount;
276:   const adjustedTarget = Math.max(targetLeafCount, minimumLeafCount);
277:   if (wordControlOptions.strictSectionWords && wordControlOptions.maximumWords > 0) {
278:     const sectionMinimumWords = Math.ceil(wordControlOptions.sectionWords * 0.8);
279:     const maximumLeafCount = Math.floor(wordControlOptions.maximumWords / sectionMinimumWords);
280:     if (maximumLeafCount < minimumLeafCount) {
281:       throw new Error(
282:         `当前严格字数配置最多容纳 ${maximumLeafCount} 个 AI 生成小节，但独立成册目录至少需要 ${minimumLeafCount} 个。请提高全文最大字数、降低单节字数或减少技术评分分支后重新生成目录。`,
283:       );
284:     }
285:     return Math.min(adjustedTarget, maximumLeafCount);
286:   }
287:   return adjustedTarget;
288: }
```

⚠️ `enforceMinimumLeafTarget` **仅在 `standaloneTechnical` 时调用**（`:1159-1170`）—— `aligned` 模式不做此调整。

---

## 9. 调用链路（函数名 + 文件:行号）

```
runOutlineGenerationTaskV2                       outlineGenerationTaskV2.cjs:744
│
├─[前置门禁]
│  ├─ isMissingTechnicalScoreItems                :736   ← 读 techRequirements
│  │   · status!=='success' → throw              :749-751  '请先完成技术评分要求解析，再生成目录'
│  │   · 评分项缺失 && !no_technical_score_mode → throw  :753-755
│  ├─ deriveTargetLeafCount                       :258
│  └─ enforceMinimumLeafTarget                    :273   （standalone 时于 :1161 调用）
│
├─[阶段1] initial-outline                          :972-1014
│  ├─ agentService.runTask(createInitialPrompt)   :974   max_retries:0 (:988)
│  │   └─ piRuntimeService.cjs:842  循环（0 次重试）
│  │       └─ json-validation 工具（Agent 自主调用）
│  ├─ readJson                                    :992
│  └─ 默认全选 attr==='技术'                       :994
│
├─ taskControl.waitForOutlineSelection()          :1016   ← 用户确认一级目录
│  └─ applyConfirmedSelection → renumberOutline   :899 / :901 / :291  ★目录锁定
│
├─[并发] parallelController                       :1055
│  ├─ templatePromise  (仅开发者模式)              :1068  runTemplateExtractionTask
│  └─ directoryPromise                            :1081
│      initial_stage: 'score-planning' | 'children_generation'   :1033
│
└─[阶段2..N] continueTask 状态机                   :1109
   │
   ├─ 'score-planning'                            :1148
   │  ├─ readJson(SCORE_DIRECTORY_PLAN_FILE)       :1149
   │  ├─ attachBranchIdsToRoots                    :1150 / :332
   │  ├─ enforceMinimumLeafTarget (standalone)     :1161
   │  └─ 分支>1 → 'leaf_allocation'                :1174
   │        └─ createLeafAllocationPrompt          :1177
   │
   ├─ 'leaf_allocation'                           :1196
   │  └─ continueWithChildrenGeneration            :1198 / :915
   │
   ├─ 'children_generation'（默认分支）             :1201
   │  ├─ readJson → buildFinalOutline              :1201-1202 / :316
   │  ├─ synchronizeScoreDirectoryPlan             :1204 / :345
   │  ├─ countAiLeaves                             :1206 / :360
   │  ├─ 相等 → 'outline_review'                    :1213
   │  ├─ 用户选"接受当前结果" → 'outline_review'    :1215
   │  └─ 否则 → 'leaf_adjustment'                   :1228
   │        └─ createLeafAdjustmentPrompt          :1231
   │
   ├─ 'leaf_adjustment' → 'outline_review'          :1213
   │
   └─ 'outline_review'                            :1110
      ├─ buildOutlineReviewContext                 :945 / :464
      │   ├─ collectScoreMappingCoverage            :487 / :419
      │   ├─ collectOutlineStructure                :467 / :380
      │   └─ countLeavesByMode                      :466 / :368
      ├─ createOutlineReviewPrompt                 :963 / :704
      └─ return { complete: true }                 :1145   ★终止
```

---

### 9.1 Pi 运行时的阶段推进机制（`piRuntimeService.cjs:929-954`）

```javascript
929:        if (typeof payload.continueTask !== 'function') break;
930:        const completedStageIndex = stageIndex;
931:        const completedWorkflowStage = activeTask.workflow_stage;
932:        const continuation = await payload.continueTask(candidate, createWorkflowMeta());
933:        if (!continuation || continuation.complete === true || !continuation.prompt) break;
...
944:        const continuationFiles = Array.isArray(continuation.files) ? continuation.files : [];
945:        if (continuationFiles.length) {
946:          await writeWorkspaceFilesAsync(workspaceDir, continuationFiles);
947:        }
948:        stageIndex = Number.isFinite(Number(continuation.stage_index))
949:          ? Number(continuation.stage_index)
950:          : stageIndex + 1;
951:        activeTask.stage_index = stageIndex;
952:        const continuationStage = continuation.stage || `workflow_stage_${stageIndex}`;
953:        activeTask.workflow_stage = continuationStage;
954:        stagePrompt = continuation.prompt;
```

**`meta` 契约**（`piRuntimeService.cjs:826-837`）：

```javascript
826:      const createWorkflowMeta = () => ({
...
834:        user_question_answers: activeTask.user_question_answers.map((item) => ({ ...item })),
835:        readFile: async (filePath) => (await readOutputAsync(workspaceDir, filePath)).content,
836:        writeFiles: async (files) => writeWorkspaceFilesAsync(workspaceDir, files),
```

**上下文压缩支持**（`:955-1000`）：`continuation.compact_before_prompt === true` 时先 `session.compact(...)` —— 目录链路**未使用**该能力。

### 9.2 复用关系（`outlineAdjustmentTask.cjs:1-9`）

```javascript
1: const { OUTLINE_AGENT_TASK_KEY } = require('./outlineGenerationAgentV2Config.cjs');
2: const {
3:   OUTLINE_OUTPUT_FILE,
4:   OUTLINE_JSON_SCHEMA,
5:   buildFinalOutline,
6:   stripOutlineInternalFields,
7:   readJson,
8:   formatProgressTitle,
9: } = require('./outlineGenerationTaskV2.cjs');
```

**旁路调用链**（AI 调整目录）：

```
runOutlineAdjustmentTask              outlineAdjustmentTask.cjs:50
  ├─ 前置门禁                          :52-61（空要求/无目录/无持久会话）
  ├─ updatePersistentTask(run_id 对齐) :81-87
  ├─ buildAgentOutlineInput            :96 / :12   （剥离 branch_id）
  ├─ agentService.runTask              :89-107     max_retries:0 (:105)
  ├─ buildFinalOutline                 :109 / :316
  ├─ stripOutlineInternalFields        :110 / :321
  └─ workspaceStore.saveOutline(replace) :114-121  ★全清正文
```

### 9.3 阶段划分与提示词对应关系

| workflow_stage | 触发位置 | 提示词 | 输出文件 |
|---|---|---|---|
| `initial-outline` | `:974` | P1 `createInitialPrompt` | `outline.json`（仅一级） |
| `score-planning` | `:1081` 初始 | P3 `createScorePlanningPrompt` | `technical-score-groups.json` + `score-directory-plan.json` |
| `leaf_allocation` | `:1177` | P2 `createLeafAllocationPrompt` | `leaf-allocation.json` |
| `children_generation` | `:927` | P4 / P7 | `outline.json`（完整） |
| `leaf_adjustment` | `:1231` | P5 `createLeafAdjustmentPrompt` | `outline.json` |
| `outline_review` | `:963` | P6 / P8 | `outline-review.json` |
| `outline-adjustment` | `outlineAdjustmentTask.cjs:92` | P9 | `outline.json` |

⚠️ **无评分项模式跳过 `score-planning` 与 `leaf_allocation` 两阶段**：

```javascript
1033:  const directoryStage = noTechnicalScoreMode ? 'children_generation' : 'score-planning';
```

直接进入 P7（`createNoTechnicalScoreChildrenPrompt`，`:1084-1086`）。

---

## 10. 容错机制

### 10.1 ⚠️ 「完整目录失败切换分步生成」—— 源码中未找到

**没有**「整体失败 → 降级为分步生成」的回退路径。二三级生成**本来就是分步的**（`score-planning` → `leaf_allocation` → `children_generation`），但这是设计而非降级。

失败即抛出，由 `piRuntimeService.cjs:904` 终结（`maxRetries=0` 时立即 throw）：

```javascript
902:          } catch (error) {
903:            if (activeController.signal.aborted) throw activeController.signal.reason || error;
904:            if (attemptIndex >= maxRetries) throw error;
```

### 10.2 重试次数

| 层级 | 次数 | 位置 |
|---|---|---|
| 阶段级自动修复 | **0** | `outlineGenerationTaskV2.cjs:988`, `:1106`；`piRuntimeService.cjs:842` 循环恒执行 1 次 |
| 叶子数调整 | **最多 1 次**（隐式） | `:1210-1213` 计数 `wordAdjustmentAttempts`，但 `:1213` 逻辑使第二次必然 `return` |
| `collectJsonResponse`（**未使用**） | 默认 2 | `aiService.cjs:834` |
| `json-validation` 工具 | Agent 自主决定 | 提示词要求「校验失败后必须先修改文件，再重新校验」，无次数上限 |

叶子调整的实际逻辑（`:1207-1218`）：

```javascript
1207:      const latestLeafAnswer = meta.workflow_stage === 'leaf_adjustment'
1208:        ? [...meta.user_question_answers].reverse().find((item) => item.workflow_stage === 'leaf_adjustment')
1209:        : null;
1210:      if (latestLeafAnswer && latestLeafAnswer.selected_option !== '接受当前结果') {
1211:        wordAdjustmentAttempts += 1;
1212:      }
1213:      if (targetLeafCount === null || actualLeafCount === targetLeafCount) return continueWithOutlineReview();
1214:
1215:      if (latestLeafAnswer?.selected_option === '接受当前结果') {
1216:        leafWarning = `AI 生成小节目标为 ${targetLeafCount}，用户已接受当前 ${actualLeafCount} 个。`;
1217:        return continueWithOutlineReview();
1218:      }
```

⚠️ **源码中无显式调整次数上限** —— 若 Agent 在 `leaf_adjustment` 后叶子数仍不等，理论上会再次进入该阶段（由 `piRuntimeService` 的 `stage_index` 单调递增隐式推进，无硬约束）。

### 10.3 失败哨兵

| 哨兵 | 实现 | 行号 |
|---|---|---|
| 评分项整项缺失 | `'未提取到'` | `:738` |
| 评分项分节缺失 | `## 技术评分项` 段 == `'没有提及'` | `:739-740` |
| JSON 解析失败 | `readJson` 抛 `${label}不是合法 JSON：...` | `:495` |
| 字数容量不足 | `enforceMinimumLeafTarget` 抛「最多容纳 N 个…」 | `:281-283` |
| 并行分支失败 | `Promise.allSettled` + 清理模版 | `:1254-1272` |
| Schema 校验失败 | `json-validation` 返回 `isError`（**非抛异常**） | `piJsonValidationTool.cjs:48` |
| 模版提取不完整 | `'投标模版提取任务已返回，但模版和字段清单不完整'` | `:1278` |

评分项缺失判定实现（`:736-741`）：

```javascript
736: function isMissingTechnicalScoreItems(content) {
737:   const text = String(content || '').trim();
738:   if (text === '未提取到') return true;
739:   const section = text.match(/^##[\t ]+技术评分项[\t ]*\r?\n([\s\S]*?)(?=^#{1,2}[\t ]|$(?![\s\S]))/m);
740:   return section?.[1].trim() === '没有提及';
741: }
```

### 10.4 并行失败的对称清理（`:1254-1279`）

```javascript
1254:    const [directorySettled, templateSettled] = await Promise.allSettled([
1255:      observedDirectoryPromise,
1256:      observedTemplatePromise,
1257:    ]);
1258:    if (directorySettled.status === 'rejected' || templateSettled.status === 'rejected') {
1259:      try { workspaceStore.clearBidTemplate(); } catch {}
1260:      if (firstParallelFailure) {
1261:        const failure = new Error(`${firstParallelFailure.label}失败：${firstParallelFailure.error?.message || String(firstParallelFailure.error)}`);
1262:        if (firstParallelFailure.error?.code) failure.code = firstParallelFailure.error.code;
1263:        throw failure;
1264:      }
...
1271:      throw new Error(messages.join('；') || '目录生成未完成');
1272:    }
...
1276:    if (templateResult.status !== 'skipped' && !workspaceStore.hasBidTemplate()) {
1277:      try { workspaceStore.clearBidTemplate(); } catch {}
1278:      throw new Error('投标模版提取任务已返回，但模版和字段清单不完整');
1279:    }
```

### 10.5 恢复机制

| 机制 | 实现 | 行号 |
|---|---|---|
| 一级目录确认状态恢复 | `restoringOutlineSelection` | `:746`, `:805-808` |
| Pi Session 断点续跑 | `persistent_task: { mode: 'resume' }` | `:1099-1102` |
| 阶段间文件传递 | `meta.readFile` / `meta.writeFiles` | `piRuntimeService.cjs:835-836` |
| 进度回显 | `updateAgentState` / `syncAgentCheckpoint` | `:824-846`, `:866-873` |

恢复逻辑（`:746`, `:805-808`）：

```javascript
746:  const restoringOutlineSelection = payload?.agent_resume?.phase === 'outline-selection';
...
805:  let logs = restoringOutlineSelection
806:    ? [...(Array.isArray(storedPlan.outlineGenerationTask?.logs) ? storedPlan.outlineGenerationTask.logs : []), '已恢复一级目录确认状态']
807:    : ['开始生成一级目录'];
808:  let currentProgress = restoringOutlineSelection ? Number(storedPlan.outlineGenerationTask?.progress || 30) : 10;
```

### 10.6 现有测试覆盖（`outlineGenerationTaskV2.test.cjs`，172 行）

| 用例 | 行号 | 锁定契约 |
|---|---|---|
| 无评分模式资料来源约束 | `:15-82` | originalOnly 时只注入 `原方案.md`；提示词含 `目录来源仅限原方案.md` |
| `isMissingTechnicalScoreItems` 主/渲染双侧一致 | `:88-101` | 8 个用例，`Main` 与 `Renderer` 同结果 |
| 无评分模式独立规则 | `:104-119` | `doesNotMatch(/一级目录必须直接对应技术评分大项/)` 等 |
| 独立成册一级目录对应评分大项 | `:121-127` | `assert.match(prompt, /一级目录必须直接对应技术评分大项/)` |
| 独立成册评分规划固定 level=1 | `:129-137` | `assert.match(prompt, /score_item_level 固定为 1/)` |
| 子目录不重复评分项根标题 | `:139-151` | `assert.match(prompt, /不得在根节点下面再次生成同名评分项/)` |
| `enforceMinimumLeafTarget` 边界 | `:153-172` | 含 `assert.throws` 容量不足 |

⚠️ **无任何测试覆盖**：并发分支、审核 `status` 处理、异常回灌、`max_retries=0` 的失败路径。

---

## 11. 与「标书/投标/招标/技术评分」业务语义耦合处（原文引用）

### 11.1 耦合点速查表

| # | 文件:行号 | 耦合强度 | 内容 |
|---|---|---|---|
| C1 | `outlineGenerationTaskV2.cjs:518` | **极高** | `一级目录必须直接对应技术评分大项` |
| C2 | `:526` | **极高** | `每个一级目录直接对应一个技术评分大项，并保持评分大项的原顺序和正式表述` |
| C3 | `:524`/`:593`/`:609` | **极高** | 禁止「技术方案」「项目管理方案」「监理大纲（暗标）」「施工组织设计」「技术标」等外层总目录 |
| C4 | `:604` | **极高** | `只从技术评分信息.md 的"技术评分项"中提取…"技术评分要求"只能作为评分标准、扣分规则和编写约束，不得提取为评分项` |
| C5 | `:644` | **极高** | `"技术评分要求"只能作为评分标准、扣分口径、判定规则和目录说明约束，不能生成独立评分项节点` |
| C6 | `:607` | 高 | 偏离审批门：合并/拆分评分项、遗漏对应节点、增加不存在的同层级大项 |
| C7 | `:716` | 高 | `- 评分覆盖：直接以技术评分信息.md 为原始依据…` |
| C8 | `:750`/`:754` | 高 | 门禁异常文案：`请先完成技术评分要求解析，再生成目录` |
| C9 | `bidAnalysisTask.cjs:87-89` | **极高** | 评分项/要求语义二分定义 |
| C10 | `outlineGenerationTaskV2.cjs:282` | 中 | `…减少技术评分分支后重新生成目录` |
| C11 | `types.ts:17` | 中 | `OutlineAttribute = '通用' | '商务' | '资信' | '技术' | '其他'` |
| C12 | `outlineGenerationTaskV2.cjs:25` | 中 | `attr` enum 同上（Schema 内） |
| C13 | `:36` + `outline.ts:1` | 中 | `content_mode` 含 `point-to-point`（点对点应答表） |
| C14 | `outline.ts:4-7` | 中 | 标签文案：`AI生成/模板填写/点对点应答表/其他模式` |
| C15 | `:605`/`:640`/`:1095` | 高 | 文件名 `技术评分信息.md` / `technical-score-groups.json` / `score-directory-plan.json` |
| C16 | `:541` | 中 | `title 必须是可直接用于投标文件目录的正式标题` |
| C17 | `outlineAdjustmentTask.cjs:44` | 中 | `选择最符合投标文件专业惯例的做法直接执行` |
| C18 | `:718` | 中 | `是否适合正式技术投标文件` |

### 11.2 C9 语义二分原文（`bidAnalysisTask.cjs:81-108`）—— **改造的关键锚点**

任务定义（`:81`）：

```javascript
81:    id: 'techRequirements', label: '技术评分要求', required: true, output: 'markdown', description: '提取技术评分项、权重分值、评分标准和招标文件中的位置。',
```

提示词原文（`:82-108`，节选核心）：

```javascript
82:    prompt: () => `任务：提取技术评分信息，并按语义区分“技术评分项”和“技术评分要求”。
...
87: 1. 技术评分项：指投标人需要在技术方案中一一响应、展开编写，并可对应形成技术方案章节的具体评分内容，例如方案类、措施类、团队类、实施类、服务类、保障类、运维类、应急类、检查类等评分内容。
88: 2. 技术评分要求：指用于约束评分、解释评分、定义扣分或判定规则的通用规则或说明，例如符合性要求、偏离扣分规则、判定口径、适用范围说明、表后说明、通用评审规则等。
89: 3. 判断依据是该内容是否要求投标人在技术方案中展开具体方案内容；如果不是具体方案内容，即使带有分值或扣分规则，也归入技术评分要求。
...
94: ## 技术评分项
...
101: ## 技术评分要求
...
108: 若某一类没有内容，请保留对应标题并写“没有提及”。直接返回提取结果。`,
```

缺失标注规范（`:195`）：

```javascript
195: 整体无结果规则：仅当当前任务完全未提取到任何相关内容时，只返回“${MARKDOWN_MISSING_RESULT}”，不要附加标题、标点、解释或其他文字。只要提取到任何有效内容，就正常返回结果；局部字段或局部分类缺失时写“没有提及”，不要使用“${MARKDOWN_MISSING_RESULT}”。`;
```

### 11.3 「评分项 vs 评分要求」与专项方案的概念同构

⚠️ **这个二分是易标最精妙的设计之一**，与专项施工方案的「**危大工程类别 vs 阈值参数**」高度同构：

| 易标（标书） | 专项方案（类比） |
|---|---|
| 技术评分**项**（可展开编写的章节内容） | 危大工程**类别**（基坑/模板支撑/脚手架/起重/拆除/其他） |
| 技术评分**要求**（扣分规则/判定口径） | **阈值参数**（基坑 3m、脚手架 24m、模板 8m） |
| 「不能生成独立评分项节点」（`:644`） | 「阈值参数不得成为独立一级目录，只能进 description / detail_points」 |

### 11.4 耦合的架构性后果

1. **`attr` 枚举是投标语义**（商务/资信/技术/其他，`:25`）—— 专项施工方案语境下无「资信」「投标函」概念。
2. **`content_mode` 含 `point-to-point`**（点对点应答表，`:36`）—— 这是**投标评审特有**产物（`:528` 解释为「需要在全部正文完成并确定 Word 页码后回填」）。
3. **前置门禁强依赖招标解析**（`:749-751`）：`techRequirements` 未成功则目录生成**完全不可启动**。
4. **评分项数量直接决定一级目录数量**（`:594`）—— 评分项是外部输入、不可控；危大六大类是**固定枚举**、可控。
5. **`enforceMinimumLeafTarget` 的容量约束按「技术分支数」计算**（`:275` `fixedAiLeafCount + technicalBranchCount`）—— 专项方案按「九大章节数=9」计算，量级更大。

---

## 12. 专项方案改造评估：一级目录 ↔ 危大工程类别 / 九大章节

### 12.1 结论

**技术上完全可行，且改造点高度收敛**（约 8 类），但**必须正面处理一个架构冲突**：现有代码的「一一对应」不是数据驱动的，而是**提示词 + 文件名 + 门禁 + 门禁文案**四重硬编码。

**推荐方案：九大章节作一级目录（固定枚举），危大六大类作 `detail_points` 约束。** 理由见 12.5 R2。

### 12.2 有利条件（为什么改造是「低成本高收益」）

| # | 依据 | 说明 |
|---|---|---|
| A1 | `requirement_id: '^R[1-9]\d*$'`（`:87`、`:127`） | **纯格式约束，零业务语义**。换 `H1..H6` / `C1..C9` 只需改 pattern |
| A2 | `branch_id: { minLength: 1 }`（`:115`） | **完全无格式约束** —— 可直接用 `H1`..`H6` / `C1`..`C9` 作稳定标识 |
| A3 | `collectScoreMappingCoverage`（`:419-461`） | **纯函数**，只认 `branch_id` / `target_title` / `score_item_level`，与「评分」无关 |
| A4 | `attachBranchIdsToRoots`（`:332`）/ `synchronizeScoreDirectoryPlan`（`:345`） | 纯标识映射，语义无关 |
| A5 | `renumberOutline`（`:291`）/`buildFinalOutline`（`:316`） | 纯编号规整，语义无关 |
| A6 | `extra_titles`（`:141`）/ `additional_titles`（`:129`） | 已支持**合并与拆分** —— 危大类别常有「一危多措施」，正是此场景 |
| A7 | `score_item_level: 1`（`:593` standalone 模式） | 「一级目录即分类本身」的模式**已存在**且已跑通 ✅ |
| A8 | 前端 `renumberOutlineItemsWithIdMap`（`OutlineEditPage.tsx:174`） | 纯编号，语义无关 |
| A9 | Ajv Schema 框架 + `json-validation` 工具 | 完全通用 |
| A10 | 危大六大类是**固定枚举** | 比技术评分项（外部输入、数量不可控）**更稳定** → `branch_id` 机制发挥更大价值 |

### 12.3 必须处理的 6 个改造点

| # | 现状（文件:行号） | 专项方案对应改造 | 风险 |
|---|---|---|---|
| **M1** | `:749-751` 门禁 `throw new Error('请先完成技术评分要求解析，再生成目录')` | 改为「请先完成危大工程辨识」；**若参考仓库无对应解析任务，需新增或复用 `globalFacts`** | 🔴 **高** |
| **M2** | `bidAnalysisTask.cjs:81-108` 整个 `techRequirements` 任务 | 需替换为危大辨识任务（六大类 + 阈值判定） | 🔴 **高** |
| **M3** | `:749` 的 `status !== 'success'` **强门禁** | 危大辨识**可能为空**（非危大工程）→ 需设计「无危大」的降级路径 | 🔴 **高** |
| **M4** | `:25` `attr` enum（商务/资信/技术/其他）+ `outline.ts:14` + `types.ts:17` | 建议 `attr` 改名 `outline-category`，值域改九大章节 | 🟡 中 |
| **M5** | `:604`、`:644`「技术评分要求不得成节点」 | 改「**阈值参数不得成节点**」—— 危大阈值（基坑 3m、脚手架 24m）应进 `description`/`detail_points` 而非独立目录 | 🟡 中 |
| **M6** | `:36` `point-to-point` + `:528` 解释 | 专项方案无点对点应答表，需决定保留（映射到「验收对照表」？）或移除 | 🟡 中 |

### 12.4 建议的改造形态（最小侵入）

**✅ 保留（零改动或仅改名）**：

```
branch_id 机制（attachBranchIdsToRoots / synchronizeScoreDirectoryPlan）
renumberOutline / buildFinalOutline / stripOutlineInternalFields
collectScoreMappingCoverage（纯函数）
buildOutlineReviewContext / collectOutlineStructure / countLeavesByMode
前端 renumberOutlineItemsWithIdMap / normalizeOutlineContentModes / assertLeafContentModes
Ajv Schema 框架 + json-validation 工具 + piRuntimeService 阶段推进
```

**🔄 替换（改名 + pattern）**：

```
TECHNICAL_SCORE_GROUPS_FILE  'technical-score-groups.json'  →  'hazard-groups.json'
SCORE_DIRECTORY_PLAN_FILE    'score-directory-plan.json'   →  'category-directory-plan.json'
LEAF_ALLOCATION_FILE         （不变）
OUTLINE_REVIEW_FILE          （不变）

requirement_id  '^R[1-9]\d*$'                              →  '^(H[1-6]|C[1-9])$'
branch_id       minLength:1（B1/B2）                        →  minLength:1（H1..H6 / C1..C9）
attr            ['通用','商务','资信','技术','其他']          →  九大章节码
isMissingTechnicalScoreItems (`:736`)                     →  isMissingHazardCategories
```

**➕ 新增（专项方案特有，当前架构缺失）**：

1. **危大阈值确定性注入**：六大类阈值（基坑 3/5m、脚手架 24m、模板 8/18/15/20m、起重、拆除）应作为**确定性前置**注入 `outline-review-context.json`（仿 `buildOutlineReviewContext:464`），**而非交给 Agent 判断** —— 与本仓 `HAZARD_THRESHOLDS` 单一事实源口径一致。
2. **`noHazardMode` 旁路**（见 12.5 R1）。
3. **`score_item_level` 保持 1**：九大章节本身即一级，与 `standalone` 模式完全同构 ✅

**📝 提示词重写（工作量最大）**：

9 个提示词函数（P1-P9）共约 **220 行文案**需按专项施工方案语境重写，其中 P3/P4 的「评分项 ↔ 目录映射」段落需整体替换为「危大类别 / 九大章节 ↔ 目录映射」。

### 12.5 风险与遗留判断

| 风险 | 说明 | 处置建议 |
|---|---|---|
| **R1 · 无危大降级**（M3） | 现有 `noTechnicalScoreMode`（`:756`）是**为「无评分项」设计的完整旁路**，含独立的 P7/P8 两套提示词。危大为空时**不能**直接复用 —— 那套提示词通篇是投标语境（如 `:692`「是否适合正式投标文件」） | **照此模式新增 `noHazardMode`**，而非硬套。工作量约 60 行 |
| **R2 · 九大章节 vs 危大六大类是交叉关系** | 建办质〔2018〕31号中「危大六大类」按**工程类型**分（基坑/模板支撑/脚手架/起重/拆除/其他），「九大章节」按**方案内容**分（工程概况/编制依据/施工计划/工艺/安全/人员/验收/应急/计算图纸）。二者**不是包含关系** | **一级目录用九大章节**（固定 9 个、内容导向、覆盖完整）；**危大六大类降级为 `detail_points` 的约束条件**。若项目无危大工程，九大章节仍然成立 ✅ —— 这正是选九大章节而非危大类别作一级目录的关键理由 |
| **R3 · `branch_id` 在无危大时** | 现有 `:1172` 单分支走兜底（`:1190-1192`），多分支才走 `leaf_allocation` | 九大章节固定 9 个分支 → **必走 `leaf_allocation` 阶段** ✅ 机制天然适配 |
| **R4 · `enforceMinimumLeafTarget` 容量**（`:275`） | `minimumLeafCount = fixedAiLeafCount + technicalBranchCount` → 九大章节 = 9，比典型技术分支数（3-6）更大 | ⚠️ 严格字数配置下更易触发 `:281-283` 的「最多容纳 N 个」异常。**需重新标定字数上下限默认值** |
| **R5 · 导出/正文连带影响** | `attr` 决定导出分册策略；`content_mode` 决定正文生成方式（`contentGenerationTask.cjs` 31 万字符，**本次未审计**） | 🔴 **需单独立项**。本报告的改造评估**仅覆盖目录生成模块** |
| **R6 · `piJsonValidationTool.cjs` 是 GBK 编码** | 修改该文件有编码风险（见本仓 AGENTS.md §5.9 同类事故） | 改造时**优先避免触碰该文件**（仅需改传入的 schema，无需改工具本身） |

### 12.6 改造工作量估算（基于实际代码行数）

| 阶段 | 内容 | 规模 | 难度 |
|---|---|---|---|
| 1 | Schema 换名 + pattern 改（`:9-10`、`:74-155`、`:185-237`） | ~30 行 | 🟢 低 |
| 2 | 9 个提示词函数文案替换（`:514-733` + `outlineAdjustmentTask.cjs:33-47`） | ~220 行 | 🔴 **高（文案需重写）** |
| 3 | 门禁 + 缺失判定（`:736-756`、`:749-755`） | ~25 行 | 🟡 中 |
| 4 | `noHazardMode` 旁路（仿 `noTechnicalScoreMode`，新增 P7'/P8' 两套提示词） | ~60 行 + 2 套文案 | 🟡 中 |
| 5 | 前端 `attr` enum（`outline.ts:14`、`types.ts:17`） | ~10 行 | 🟢 低 |
| 6 | 危大阈值确定性注入（新增，仿 `buildOutlineReviewContext:464`） | ~80 行 | 🟡 中 |
| 7 | **导出/正文连带影响审计与改造** | **未知** | 🔴 **需单独立项** |
| 8 | 测试更新（`outlineGenerationTaskV2.test.cjs` 7 个用例全部含 `doesNotMatch(/技术评分/)` 类断言） | ~172 行改写 | 🟡 中 |

⚠️ **第 8 项易被忽略**：`outlineGenerationTaskV2.test.cjs:112-151` 有 5 处 `assert.match(prompt, /技术评分/)` 与 `assert.doesNotMatch(...)` 断言，改造后**必然全部失败**，必须同步改写 —— 否则 CI 红灯会掩盖真实问题。

### 12.7 最终建议

1. **一级目录 = 九大章节**（固定枚举、内容导向、无条件成立），**不直接用危大六大类**（因 R2 交叉关系 + 非危大项目无内容可生成）。
2. **危大六大类 = `detail_points` 约束 + 确定性阈值前置注入**，对应易标「技术评分要求」的位置 —— 这是对易标设计最忠实的迁移。
3. **保留全部 `branch_id` 机制**（A2/A10）—— 它是本模块最有价值的资产，且对固定枚举比对外部分类**更可靠**。
4. **先做目录生成模块单独立项**（阶段 1-6、8），把阶段 7（导出/正文连带）作为**前置依赖调研**并行推进。
5. **编码风险提示**：`piJsonValidationTool.cjs` 为 GBK，若必须修改请先转码为 UTF-8（阶段 1 原则上无需触碰该文件）。

---

## 标书语义耦合点清单

> 本节按要求逐条列出与「标书 / 投标 / 技术评分要求 / 评分项一一对应」耦合的原文位置（file:line）+ 原文摘录 + **改写为专项施工方案语义（一级目录 ↔ 危大工程六大类 / 九大章节）时应改为什么**，并给出可行性评估。
>
> 约定：`OG` = `client/electron/services/outlineGenerationTaskV2.cjs`；`OA` = `client/electron/services/outlineAdjustmentTask.cjs`；`BA` = `client/electron/services/bidAnalysisTask.cjs`；`outline.ts` = `client/src/shared/types/outline.ts`；`types.ts` = `client/src/features/technical-plan/types.ts`。

---

### 耦合点 S1 —— 一级目录 ↔ 技术评分大项「一一对应」的核心声明

**位置**：`OG:518`（`createInitialPrompt` 的 `goal`）

```javascript
517:      ? '我们的目标是为没有技术评分项的单独装订技术文件准备一级目录。'
518:      : '我们的目标是为单独装订的技术文件准备一级目录。一级目录必须直接对应技术评分大项。'
```

**应改为**：

> `我们的目标是为专项施工方案准备一级目录。一级目录必须直接对应九大章节（工程概况、编制依据及标准、施工部署与计划、关键工艺与操作要点、安全与质量保证、劳动力与机械设备配备、验收与试验、应急处置预案、计算书与附图）。`

**可行性评估**：🟢 **高**。本行是纯字符串，`goal` 仅用于 P1 提示词的第 1 段（`:533` 插入）。无下游代码消费该值。**唯一需连带确认**：`standaloneTechnical` 分支条件（`:515`）在专项方案语境下恒为 true（专项方案无「装订分册」概念），可将三元简化为常量，属可选简化。

---

### 耦合点 S2 —— 顺序与表述的强约束

**位置**：`OG:526`（`modeRequirements` standalone + 有评分项分支）

```javascript
526:  7. 每个一级目录直接对应一个技术评分大项，并保持评分大项的原顺序和正式表述；不得创建“技术方案”“项目管理方案”“监理大纲”“监理大纲（暗标）”“施工组织设计”“技术标”等外层总目录，也不得加入商务、资信、投标函、授权委托书等非技术章节。
```

**应改为**：

> `7. 每个一级目录直接对应一个九大章节，并保持九大章节的固定顺序（按建办质〔2018〕31号附件二顺序）和规范表述；不得创建"施工组织设计""专项施工方案""技术标"等外层总目录；不得加入商务、资信、投标函、授权委托书等非技术章节。`

**可行性评估**：🟢 **高**。纯文案。注意「监理大纲（暗标）」在专项方案语境无对应物，可直接删除；禁列表需保留「施工组织设计」这一条（专项方案常被误加为一级目录）。

---

### 耦合点 S3 —— 独立成册模式的分支映射锁定 ★ 改造最重要的落点

**位置**：`OG:593-594`（`createScorePlanningPrompt` 的 `placementInstruction`）

```javascript
593:    ? `4. 当前采用“技术文件独立成册”：${OUTLINE_OUTPUT_FILE} 中每个一级根节点本身就应对应一个技术评分大项。每个根节点建立一个 branch，score_item_level 固定为 1，mappings 只填写与该根标题对应的评分大项，target_title 必须与 root_title 完全一致；不得再创建“技术方案”“项目管理方案”“监理大纲”“监理大纲（暗标）”“施工组织设计”“技术标”等外层分支。
594:  5. 一级根节点与评分大项默认严格一一对应；发现缺失、重复、合并或顺序不一致时，必须作为一级目录调整向用户说明并取得批准。detail_points 只用于后续生成根节点以下的目录。`
```

**应改为**：

> `4. 当前采用"专项施工方案"模式：`outline.json` 中每个一级根节点本身就应对应一个九大章节。每个章节建立一个 branch，branch_id 使用固定的 H1..H9，score_item_level 固定为 1，mappings 只填写与该根标题对应的章节分类码（category_code 取 C1..C9），target_title 必须与 root_title 完全一致；不得再创建"施工组织设计""专项施工方案""技术标"等外层分支。
> 5. 一级根节点与九大章节默认严格一一对应；发现缺失、重复、合并或顺序不一致时，必须作为一级目录调整向用户说明并取得批准。detail_points 用于承载该章节下的危大工程类别（深基坑/模板支撑/脚手架/起重吊装/拆除工程/其他）与对应阈值判定要求。`

**可行性评估**：🟢 **高**。这是**整个改造最重要的落点** —— 它同时定义了「一级目录 ↔ 分类」的一一对应关系**和** `detail_points` 的新职责（承载危大信息，对应易标原 `detail_points` 承载「评分细项」的位置）。🟢 `branch_id` 无格式约束（`OG:115`），`H1..H9` 可直接使用，**无需改 Schema**。

⚠️ **配套需改**：Schema 字段名 `score_item_level`（`OG:118`）语义变为「章节层级」—— 建议**保留字段名**（改字段名会波及 `OG:118`、`:428`、`:437`、`:593`、`:628`、`:662`、`:682`、`:707` 共 8 处），或统一改名为 `category_level`。

---

### 耦合点 S4 ——「技术评分要求」二分：只取「项」，不取「要求」

**位置**：`OG:604`（P3 第 2 条）

```javascript
604:  2. 程序已确认本任务存在技术评分项。只从技术评分信息.md 的“技术评分项”中提取适合在技术方案中一一响应、展开编写的评分大项。“技术评分要求”只能作为评分标准、扣分规则和编写约束，不得提取为评分项。
```

**应改为**：

> `2. 程序已确认本任务存在危大工程。只从 危大工程辨识.md 的"危大工程类别"中提取适合在专项施工方案中一一展开编写的危大类别。"危大工程阈值参数"（如基坑深度 3m 及以上、落地式钢管脚手架搭设高度 24m 及以上）只能作为判定依据、专项措施约束和编写要求，不得提取为独立的危大类别节点。`

**可行性评估**：🟢 **高**，且**这是易标设计最值得迁移的一点**。「项 vs 要求」的二分恰好对应「危大类别 vs 阈值参数」。源码侧仅需改文案；⚠️ 但**上游必须先产出 `危大工程辨识.md`**（当前 `BA:81-108` 产出的是 `techRequirements`）—— 见耦合点 S10。

---

### 耦合点 S5 ——「技术评分要求」不得成节点（子目录阶段）

**位置**：`OG:644`（P4 第 6 条）

```javascript
644:  6. “技术评分要求”只能作为评分标准、扣分口径、判定规则和目录说明约束，不能生成独立评分项节点。
```

**应改为**：

> `6. "危大工程阈值参数"只能作为判定依据、专项措施约束、计算书引用和目录说明约束，不能生成独立的一级目录节点或独立评分项节点。阈值必须写进所属章节的 description 或该章节下 detail_points 对应的子目录标题中。`

**可行性评估**：🟢 **高**。纯文案。⚠️ 注意：改造后**不能**简单照搬 —— 因为专项方案中「危大工程」往往**确实需要**独立章节（如「深基坑工程专项方案」），这与「阈值参数不得成节点」不矛盾（前者是类别、后者是参数），但需在文案中明确区分，避免 Agent 混淆。

---

### 耦合点 S6 —— 审核维度的「评分覆盖」

**位置**：`OG:716`（P6 审核维度）

```javascript
716: - 评分覆盖：直接以技术评分信息.md 为原始依据，逐项检查其中适合技术方案响应的评分大项是否被目录准确覆盖；结构化评分项和目录规划用于核对已确认的映射，但不能掩盖原始评分信息中的遗漏。
```

**应改为**：

> `- 危大覆盖：直接以 危大工程辨识.md 为原始依据，逐项检查其中识别出的危大工程类别是否被目录准确覆盖（每个危大类别的专项措施与计算书是否都有对应章节）；结构化危大类别和目录规划用于核对已确认的映射，但不能掩盖原始辨识结果中的遗漏。`

**可行性评估**：🟡 **中**。文案可改，但 ⚠️ **必须同步改 Schema 的 `category` 枚举**（`OUTLINE_REVIEW_SCHEMA` 的 `category` enum，`OG:170-173`）：

```javascript
170:          category: {
171:            type: 'string',
172:            enum: ['leaf-count', 'score-coverage', 'duplicate-directory', 'professional-structure'],
173:          },
```

`'score-coverage'` 应改为 `'hazard-coverage'`（或 `'category-coverage'`）。**注意**：这会影响 `collectOutlineReviewContext` 产出的 context（`OG:487`）与前端对审核结果的展示（若前端有 category 映射，需同步）。

⚠️ 另注：P8（无评分项审核）已禁用该类别（`OG:700`）：

```javascript
700:  6. issues 的 category 只能使用 leaf-count、duplicate-directory 或 professional-structure。最终将完整问题清单和处理结果写入 ${OUTLINE_REVIEW_FILE}。
```

---

### 耦合点 S7 —— 一级目录的数据依赖

**位置**：`OG:792-795`（P1 阶段的输入文件）

```javascript
792:    initialFiles = [
793:      { path: '响应文件要求.md', content: responseFileRequirements },
794:      ...(standaloneTechnical ? [{ path: '技术评分信息.md', content: storedPlan.techRequirements || '' }] : []),
795:      { path: '项目概述.md', content: storedPlan.projectOverview || '' },
```

**应改为**：

```javascript
{ path: '危大工程辨识.md', content: storedPlan.hazardIdentification || '' }
```

**可行性评估**：🟡 **中**。字段名 `techRequirements` 来自 `technicalPlanStore.cjs` 的持久化 plan（由 `BA:407` 写入）：

```javascript
407:    if (task.id === 'techRequirements') technicalPlanPatch.techRequirements = trimmedContent;
```

🟢 **但若一级目录改用九大章节（固定枚举），则一级目录根本不需要外部输入** —— 九大章节是规范固定的，可直接由代码枚举输出，`initialFiles` 只需注入 `危大工程辨识.md`（供 `detail_points` 使用）。

**这是本改造收益最大的一处简化**：易标的一级目录数量取决于招标文件评分项数量（真正的外部输入、不可控），而九大章节恒为 9（代码可枚举）→ **省掉整个「一级目录生成」阶段的 AI 调用**。

---

### 耦合点 S8 —— 前置门禁与异常文案 ★ 架构级耦合

**位置**：`OG:747-756`

```javascript
747:  const technicalScoreTask = storedPlan.bidAnalysisTasks?.techRequirements;
748:  const technicalScoreContent = String(technicalScoreTask?.content || '').trim();
749:  if (technicalScoreTask?.status !== 'success' || !technicalScoreContent) {
750:    throw new Error('请先完成技术评分要求解析，再生成目录');
751:  }
752:  const technicalScoreMissing = isMissingTechnicalScoreItems(technicalScoreContent);
753:  if (technicalScoreMissing && payload?.no_technical_score_mode !== true) {
754:    throw new Error('请先确认是否以无技术评分项模式生成目录');
755:  }
756:  const noTechnicalScoreMode = technicalScoreMissing;
```

**应改为**：

```javascript
throw new Error('请先完成危大工程辨识，再生成目录');
...
throw new Error('请先确认是否以无危大工程模式生成目录');
```

**可行性评估**：🔴 **高风险**。这是**架构级耦合点**，不只是文案：

1. `:749` 是**硬门禁** —— `status !== 'success'` 即抛错，整个目录生成不可启动。
2. `:753-754` 依赖 `no_technical_score_mode` 这个 **payload 字段**，由前端在用户确认「无评分项」时传入（并在 `OG:839` 的 `resume_payload` 中透传）。
3. **「无危大工程」是常态而非异常** —— 多数专项方案项目不是危大工程。若沿用「强门禁 + 需用户确认」的交互，会让**大量正常项目被卡在门禁上**。
4. `isMissingTechnicalScoreItems`（`OG:736-741`）的正则硬编码了二级标题名：

```javascript
739:  const section = text.match(/^##[\t ]+技术评分项[\t ]*\r?\n([\s\S]*?)(?=^#{1,2}[\t ]|$(?![\s\S]))/m);
740:  return section?.[1].trim() === '没有提及';
```

🟢 **强烈建议**：若一级目录改用**九大章节（固定枚举）**，则 `:749-755` 门禁**应整体删除或降级为可选**（危大辨识仅影响 `detail_points`，不影响一级目录骨架）。这样**同时消除了 S8、S10 两个高风险点**。

---

### 耦合点 S9 —— 字数容量异常中的业务术语

**位置**：`OG:281-283`

```javascript
281:      throw new Error(
282:        `当前严格字数配置最多容纳 ${maximumLeafCount} 个 AI 生成小节，但独立成册目录至少需要 ${minimumLeafCount} 个。请提高全文最大字数、降低单节字数或减少技术评分分支后重新生成目录。`,
283:      );
```

**应改为**：

> `当前严格字数配置最多容纳 ${maximumLeafCount} 个 AI 生成小节，但九大章节骨架至少需要 ${minimumLeafCount} 个。请提高全文最大字数或降低单节字数后重新生成目录。`

**可行性评估**：🟢 **高**（文案）+ 🟡 **中**（逻辑）。⚠️ **需重新标定**：九大章节固定 9 个（`technicalBranchCount = 9`），比典型技术分支数（3-6）更大，`minimumLeafCount` 恒 ≥ 9（`OG:275` `fixedAiLeafCount + technicalBranchCount`），严格字数配置下**更易触发此异常**（风险 R4）。建议同步调整字数默认值或 `enforceMinimumLeafTarget` 的下限系数。

---

### 耦合点 S10 —— 上游解析任务的语义二分定义

**位置**：`BA:81-108`（整个 `techRequirements` 任务定义）

```javascript
81:    id: 'techRequirements', label: '技术评分要求', required: true, output: 'markdown', description: '提取技术评分项、权重分值、评分标准和招标文件中的位置。',
82:    prompt: () => `任务：提取技术评分信息，并按语义区分“技术评分项”和“技术评分要求”。
...
87: 1. 技术评分项：指投标人需要在技术方案中一一响应、展开编写，并可对应形成技术方案章节的具体评分内容，例如方案类、措施类、团队类、实施类、服务类、保障类、运维类、应急类、检查类等评分内容。
88: 2. 技术评分要求：指用于约束评分、解释评分、定义扣分或判定规则的通用规则或说明，例如符合性要求、偏离扣分规则、判定口径、适用范围说明、表后说明、通用评审规则等。
89: 3. 判断依据是该内容是否要求投标人在技术方案中展开具体方案内容；如果不是具体方案内容，即使带有分值或扣分规则，也归入技术评分要求。
...
94: ## 技术评分项
...
101: ## 技术评分要求
...
108: 若某一类没有内容，请保留对应标题并写“没有提及”。直接返回提取结果。`,
```

**应改为**（新增一个 `hazardIdentification` 任务）：

```javascript
id: 'hazardIdentification', label: '危大工程辨识', required: false, output: 'markdown',
description: '辨识本项目涉及的危大工程类别及对应阈值参数，输出可展开编写的专项方案章节清单。'
```

提示词二分改为：

> `1. 危大工程类别：指需要在专项施工方案中展开具体技术措施、可对应形成独立章节的危大工程类型，例如深基坑工程、模板支撑体系、起重吊装工程、脚手架工程、拆除工程等。
> 2. 危大工程阈值参数：指用于判定是否构成危大工程、定义专项措施适用范围的量化指标，例如基坑开挖深度 3m 及以上、落地式钢管脚手架搭设高度 24m 及以上、高大模板支撑体系高度 8m 及以上。
> 3. 判断依据是该参数是否需要投标人在专项方案中展开具体技术措施；若只是判定口径或适用范围说明，归入阈值参数。`

**可行性评估**：🔴 **高风险 / 需单独立项**。

- 🟢 `required: true` → 建议改 `false`（多数项目非危大，见 S8）
- 🟡 产出的 markdown 需有稳定的 `## 危大工程类别` / `## 危大工程阈值参数` 二级标题结构，**否则 `isMissingTechnicalScoreItems`（`OG:736-741`）的正则无法复用**
- 🔴 **`OG:740` 的判定是严格等于** `=== '没有提及'` —— 若 Agent 写成「无危大工程」「本项目不涉及危大工程」等，判定会返回 false（认为有内容），门禁**静默失效**。这与本仓 AGENTS.md §4.11.2 记录的 `_json_all_empty` 哨兵假绿是**同类问题**。改造时必须一并处理（改为「空 / 未提取到 / 没有提及 / 无XX」四态判定）

**缺失标注规范原文**（`BA:195`，改造时需同步）：

```javascript
195: 整体无结果规则：仅当当前任务完全未提取到任何相关内容时，只返回“${MARKDOWN_MISSING_RESULT}”，不要附加标题、标点、解释或其他文字。只要提取到任何有效内容，就正常返回结果；局部字段或局部分类缺失时写“没有提及”，不要使用“${MARKDOWN_MISSING_RESULT}”。`;
```

---

### 耦合点 S11 —— `attr` 枚举（投标分类）

**位置**：`OG:25`（Schema）、`outline.ts:14`、`types.ts:17`

```javascript
25:      attr: { type: 'string', enum: ['通用', '商务', '资信', '技术', '其他'] },
```

```typescript
14:   attr?: '通用' | '商务' | '资信' | '技术' | '其他';   // outline.ts
```

```typescript
17: export type OutlineAttribute = '通用' | '商务' | '资信' | '技术' | '其他';   // types.ts
```

**应改为**：

> 专项方案目录中**所有**章节都是技术内容，「商务/资信」分类无意义。两个选项：
> - **选项 1（推荐，低风险）**：保留字段，改值域为九大章节码
>   ```javascript
>   attr: { type: 'string', enum: ['overview', 'basis', 'plan', 'technique', 'safety', 'personnel', 'acceptance', 'emergency', 'calc_drawings'] }
>   ```
> - **选项 2（彻底但风险高）**：删除该字段

**可行性评估**：🟡 **中**。删除的连带影响面：

```
OG:522、:525、:556、:595、:628、:994、:999（6 处）
outline.ts:14、types.ts:17、types.ts:31、OutlineEditPage.tsx:31（4 处）
exportService.cjs / contentGenerationTask.cjs 的分册策略（未审计）
```

其中 `OG:994` 是默认全选逻辑：

```javascript
994:    const defaultSelectedIds = items.filter((item) => item.attr === '技术').map((item) => item.id);
```

**建议**：先按选项 1 改值域保留字段，删除留作后续独立项。

---

### 耦合点 S12 —— `content_mode` 的 `point-to-point`（点对点应答表）

**位置**：`OG:17`（常量）、`OG:36`（Schema）、`OG:528`（解释）、`outline.ts:6`（标签）

```javascript
17: const CONTENT_MODES = ['ai-generate', 'template-fill', 'point-to-point', 'other'];
```

```javascript
528:    : `6. 每个一级目录当前都是叶子节点，必须根据它后续应采用的内容处理方式填写 content_mode：技术方案正文使用 ai-generate；需要从招标文件提取并套用表格或格式的商务、资信材料使用 template-fill；需要在全部正文完成并确定 Word 页码后回填的点对点应答表使用 point-to-point；无法归类的特殊内容使用 other，并在 content_mode_note 说明原因。
```

```typescript
6:   'point-to-point': '点对点应答表',   // outline.ts
```

**应改为**：

> `point-to-point` 在专项方案语境下**无对应业务**（它是投标评审的「逐条应答」产物）。三选一：
> - **方案 A（推荐，低成本）**：保留枚举值，仅改标签为「验收对照表」，映射到专项方案的「规范条文对照表 / 验收标准对照表」
> - **方案 B**：从 `CONTENT_MODES`（`OG:17`）删除，同时清理 `OG:36`、`:528`、`:566`、`:649`、`:660`、`:663`、`:676`、`:698`、`outline.ts:1`、`:6`、`OutlineEditPage.tsx:65` 共 11 处
> - **方案 C**：保留但标记 deprecated

**可行性评估**：🟡 **中**。方案 A 成本最低（1 行标签 + 1 段文案），但语义拉伸；方案 B 彻底但需清理 11 处 + 核查 `contentGenerationTask.cjs` 的消费逻辑（未审计）。**建议方案 A 起步**。

⚠️ 注意 `template-fill`（模板填写）在专项方案语境下同样弱相关（它指「从招标文件提取并套用表格」），但可用于「套用标准工艺卡/工法样板」，保留即可。

---

### 耦合点 S13 —— 一级目录确认的默认全选逻辑

**位置**：`OG:994`

```javascript
994:    const defaultSelectedIds = items.filter((item) => item.attr === '技术').map((item) => item.id);
```

**应改为**（若保留 `attr`）：`items.filter((item) => item.attr !== '商务' && item.attr !== '资信')`；或（若删除 `attr`）：`items.map((item) => item.id)` 全选。

**可行性评估**：🟢 **高**。单行。⚠️ 需确认前端 `OutlineSelectionState`（`types.ts:36-41`）的交互是否依赖 `attr` 做过滤展示。

---

### 耦合点 S14 —— 「投标文件」术语（散布于多处提示词）

**位置与原文**：

| file:line | 原文摘录 |
|---|---|
| `OG:541` | `3. title 必须是可直接用于投标文件目录的正式标题，不得包含"附件1""附件一""第一章"等编号或前缀。` |
| `OG:718` | `- 专业合理性：评估目录层级、颗粒度、逻辑顺序、标题表达、节点归属以及内容处理模式是否适合正式技术投标文件。` |
| `OG:692` | `- 专业合理性：检查目录是否覆盖项目实施所需的通用技术主题，层级、颗粒度、逻辑顺序、标题和内容处理模式是否适合正式投标文件。` |
| `OA:44` | `5. 如果用户要求含糊或存在多种理解，选择最符合投标文件专业惯例的做法直接执行，...` |
| `OG:1017` | `taskInstruction = noTechnicalScoreMode ? ORIGINAL_ONLY_DIRECTORY_RULE : '只根据原方案材料提取一级目录。';` |
| `OG:799` | `'严格按照技术评分信息.md 中适合技术方案响应的评分大项组织一级目录，只生成技术方案独立分册。评分大项原文、顺序和数量是一级目录的权威依据；...'` |
| `OG:1023` | `'一级目录已确认，目录生成与投标模版提取并行开始'` |
| `OG:1047` | `title: '投标模版提取'` |

**应改为**：

| file:line | 改后 |
|---|---|
| `OG:541` | `title 必须是可直接用于专项施工方案目录的正式标题，不得包含"附件1""附件一""第一章"等编号或前缀。` |
| `OG:718` | `...是否适合正式专项施工方案。` |
| `OG:692` | `...是否适合正式专项施工方案。` |
| `OA:44` | `...选择最符合专项施工方案专业惯例的做法直接执行，...` |
| `OG:799` | `'严格按照九大章节规范组织一级目录，章节顺序与表述是一级目录的权威依据；危大工程辨识.md 仅用于补充各章节下的 detail_points，不改变一级目录骨架。'` |
| `OG:1023`/`:1047` | 🟢 随 S15 一并处理 |

**可行性评估**：🟢 **高**（均为纯文案）。🟢 注意 `OG:541` 的「不得包含附件1/第一章」约束在专项方案中**依然有效**（编号由 `renumberOutline` 统一生成），建议保留。

---

### 耦合点 S15 —— 投标模版提取（标书特有功能）

**位置**：`OG:1018-1019`、`:1041-1053`、`:1068-1079`、`:1249-1285`

```javascript
1018:  const extractTemplate =
1019:    !standaloneTechnical && Boolean(aiService?.isDeveloperMode?.());
```

```javascript
1046:        run_id: templateTaskId,
1047:        title: '投标模版提取',
```

**应改为**：专项方案**无投标模版提取需求** → 🟢 建议**保留但不启用**。

**可行性评估**：🟡 **中**。🟢 **该功能已受双重门控**：`!standaloneTechnical`（`:1019`）在专项方案语境下恒为 false（因为 `standaloneTechnical` 语义变为「专项方案模式」恒 true）→ **自然失效，无需改一行代码**。

🔴 若要彻底删除，需处理 `OG:1055-1066`（`parallelController` + `observeParallelBranch`）与 `:1249-1285`（`Promise.allSettled` 双分支收敛 + `clearBidTemplate` 清理）的**成对逻辑**，改动面约 80 行，并需删除 `templateExtractionTask.cjs`（5276 字节）与 `TEMPLATE_EXTRACTION_AGENT_TASK_KEY`。

**建议**：**不删**（低收益、有风险），依赖 `standaloneTechnical` 恒 true 自然短路。

---

### 耦合点 S16 —— 文件名与工作区约定

**位置**：`OG:9-10`（常量）、`:603`（读取指令）、`:640`（权威清单声明）

```javascript
9: const TECHNICAL_SCORE_GROUPS_FILE = 'technical-score-groups.json';
10: const SCORE_DIRECTORY_PLAN_FILE = 'score-directory-plan.json';
```

```javascript
603:  1. 阅读 ${OUTLINE_OUTPUT_FILE}、技术评分信息.md，以及存在的原方案.md 和参考知识库目录。
```

```javascript
640:  2. 以 ${TECHNICAL_SCORE_GROUPS_FILE} 为技术评分项权威清单，以 ${SCORE_DIRECTORY_PLAN_FILE} 为评分项与目录位置的权威规划。
```

**应改为**：

```javascript
9: const HAZARD_GROUPS_FILE = 'hazard-groups.json';           // 或 category-groups.json
10: const CATEGORY_DIRECTORY_PLAN_FILE = 'category-directory-plan.json';
```

```javascript
603:  1. 阅读 ${OUTLINE_OUTPUT_FILE}、危大工程辨识.md，以及存在的原方案.md 和参考知识库目录。
640:  2. 以 ${HAZARD_GROUPS_FILE} 为危大工程类别权威清单，以 ${CATEGORY_DIRECTORY_PLAN_FILE} 为类别与目录位置的权威规划。
```

**可行性评估**：🟢 **高**。🟢 文件名是常量（`OG:9-14`），改名后需同步：Schema 注册（`OG:769-770`）、`jsonValidationSchemas`（`OG:766-774`）、4 个提示词函数内的模板引用。

🟢 **关键优势**：`piJsonValidationTool.cjs` 的 `presetSchemas` key（`:54-57`）由**调用方传入的文件路径**决定，**工具本身无需修改** → 完全规避了 GBK 编码风险（风险 R6）。

⚠️ **注意**：工作区**残留旧文件**（`technical-score-groups.json` / `score-directory-plan.json`）会导致 Agent 读到过期数据。建议改名时同步清理持久化 Agent 工作区（`technicalPlanStore` 的 agent workspace 目录）。

---

### 耦合点 S17 —— 测试断言中的业务术语

**位置**：`outlineGenerationTaskV2.test.cjs:112-151`

```javascript
112:  assert.match(initialPrompt, /专业经验补充通用、合理的技术方案主题/);
113:  assert.doesNotMatch(initialPrompt, /一级目录必须直接对应技术评分大项/);
...
124:  assert.match(prompt, /一级目录必须直接对应技术评分大项/);
125:  assert.match(prompt, /不得创建“技术方案”“项目管理方案”“监理大纲”“监理大纲（暗标）”“施工组织设计”“技术标”/);
126:  assert.match(prompt, /不得加入商务、资信、投标函、授权委托书/);
...
132:  assert.match(prompt, /程序已确认本任务存在技术评分项/);
134:  assert.match(prompt, /score_item_level 固定为 1/);
135:  assert.match(prompt, /target_title 必须与 root_title 完全一致/);
...
148:  assert.match(prompt, /现有一级根节点本身就是评分项映射节点/);
149:  assert.match(prompt, /不得在根节点下面再次生成同名评分项/);
150:  assert.doesNotMatch(prompt, /"title":"技术方案"/);
```

**应改为**：全部 `/技术评分/` → `/九大章节|危大工程/`；`/评分项/` → `/章节分类/`；禁列表断言保留但删「监理大纲（暗标）」项。

**可行性评估**：🟡 **中**。🔴 **改造后这 6 处断言必然全部失败**（`:113`、`:124`、`:125`、`:132`、`:148`、`:149`、`:150`），必须同步改写。

⚠️ **易被忽略** —— CI 红灯会掩盖真实问题。建议改造时**先改测试、后改实现**，用测试驱动锁定新契约。另注：`:154-171` 的 `enforceMinimumLeafTarget` 边界测试与术语无关，**无需改动**（但因 `technicalBranchCount` 从 3-6 变 9，需补 9 分支的用例）。

---

## 改造可行性总评

### 分层评估

| 层次 | 耦合点 | 可行性 | 说明 |
|---|---|---|---|
| **L1 纯文案**（无代码依赖） | S1、S2、S3、S4、S5、S6（部分）、S9（部分）、S14 | 🟢 **高** | 约 220 行文案，涉及 `createInitialPrompt` / `createScorePlanningPrompt` / `createChildrenPrompt` / `createOutlineReviewPrompt` / `createLeafAdjustmentPrompt` / `createOutlineAdjustmentPrompt` 六个函数 |
| **L2 标识与 Schema** | S6（category enum）、S7（文件名）、S16（常量） | 🟢 **高** | `requirement_id` pattern、`branch_id`、`category` enum、文件常量。🟢 **避开 `piJsonValidationTool.cjs`（GBK 编码）** |
| **L3 数据契约** | S11（`attr`）、S12（`content_mode`）、S13（默认全选） | 🟡 **中** | 需连带核查 `exportService.cjs` / `contentGenerationTask.cjs`（**本次未审计**） |
| **L4 架构耦合** | S8（前置门禁）、S10（上游解析任务） | 🔴 **高风险** | 需产品决策（是否保留强门禁）+ 新增/改造上游任务。**建议单独立项** |
| **L5 功能短路** | S15（投标模版提取） | 🟢 **高** | 🟢 依赖 `standaloneTechnical` 恒 true 自然短路，**无需改代码** |
| **L6 测试** | S17 | 🟡 **中** | 6 处断言必失败，须同步改写 |

### 核心判断

> **「一级目录 ↔ 技术评分项一一对应」这个机制，与专项施工方案「一级目录 ↔ 九大章节」是 100% 同构的** —— 两者都是「外部权威清单 → 目录骨架 → `branch_id` 稳定映射 → 机械覆盖校验（`collectScoreMappingCoverage`）」的四段式。差异只在清单来源：
>
> - **技术评分项**：外部输入、数量不可控（取决于招标文件有几个评分项）
> - **九大章节**：规范固定、数量恒为 9（建办质〔2018〕31号附件二）
>
> 这个差异对本仓**是净收益**，有三重体现：
>
> 1. **机制更可靠** —— 固定枚举意味着 `branch_id` 机制（`OG:332`/`:345`/`:641`）无需处理「评分项被合并/拆分/遗漏」的复杂审批流（`OG:607`/`:609` 的五类偏离审批可大幅简化）
> 2. **可省掉整个旁路** —— `no_technical_score_mode`（`OG:756`）的三套提示词（P7/P8 + `isMissingTechnicalScoreItems`）**可以整体删除**，因为九大章节永远存在
> 3. **可省掉一级目录的 AI 调用** —— 章节骨架由代码枚举输出，`initial-outline` 阶段（`OG:972-1014`）不再是 AI 猜测，而是确定性构造
>
> **推荐路径**：
> 1. **一级目录 = 九大章节固定枚举**（代码直接输出，不经 AI 猜测）→ 对应 S1/S2/S3/S7/S8
> 2. **危大六大类 = `detail_points` 约束 + 确定性阈值前置注入** → 对应 S4/S5/S9
> 3. **消除对上游解析任务的强依赖**（`OG:749-755` 删除或降级）→ 对应 S8/S10，**收益最大**
> 4. L3 需在动代码前，先补一轮 `exportService.cjs` / `contentGenerationTask.cjs` 的 `attr` / `content_mode` 消费方审计
> 5. **测试先行**：先改 `outlineGenerationTaskV2.test.cjs` 锁定新契约（S17），再改实现

### 风险提示

| 风险 | 说明 | 缓解 |
|---|---|---|
| R1 | 「无危大」降级路径需照 `noTechnicalScoreMode` 模式**新增**而非复用（`OG:692`「是否适合正式投标文件」等通篇投标语境） | 仿写 P7'/P8' 两套提示词，约 60 行 |
| R2 | 九大章节 vs 危大六大类是**交叉关系**（前者按方案内容分，后者按工程类型分），非包含关系 | 一级目录用九大章节；危大六大类降级为 `detail_points` |
| R3 | `technicalBranchCount = 9` 使 `enforceMinimumLeafTarget` 下限提高（`OG:275`） | 重新标定字数默认值 |
| R4 | 导出/正文链路对 `attr` / `content_mode` 的消费未审计 | 动代码前先补审计（单独立项） |
| R5 | `piJsonValidationTool.cjs` 是 GBK 编码 | 🟢 阶段 1-6 原则上无需触碰该文件 |
| R6 | `OG:740` 的 `=== '没有提及'` 严格判定在新语境下易失效（同本仓 §4.11.2 假绿教训） | 改为四态判定并补测试 |

---

## 附：未找到项汇总

| 任务书要求 | 状态 |
|---|---|
| `outlineGenerationTask.cjs`（非 V2） | **源码中未找到**（只有 V2） |
| Pydantic 模型（`OutlineItem`/`OutlineResponse`/`OutlineChildrenResponse`/`OutlineReviewResponse`） | **源码中未找到**；实际为手写 JSON Schema + Ajv |
| `check_json` 函数 | **源码中未找到**；实际为 Agent 工具 `json-validation`（`piJsonValidationTool.cjs`） |
| `other_outline` 参数 | **源码中未找到**；实际为 `branch_id` + `score-directory-plan.json` |
| 目录链路使用 `collect_json_response` | **确认不使用**；目录链路走 Pi Agent SDK（`piRuntimeService.cjs`） |
| 目录链路的重试机制 | **确认 `max_retries: 0`**（`OG:988`/`:1106`/`OA:105`），`piRuntimeService.cjs:904` 的重试分支恒不触发 |
| 审核返回 `{passed, suggestions}` | 实际为 `{status, issues[], user_feedback, summary}`（`OG:157-183`） |
| 「完整目录失败切换分步生成」 | **源码中未找到该降级路径** |
| SystemPrompt / UserPrompt 二元结构 | **源码中未找到**；本仓库为单一 `prompt` 字符串 |

**审计范围声明**：

- ✅ **全文精读**：`outlineGenerationTaskV2.cjs`（1289 行）、`outlineAdjustmentTask.cjs`（141 行）、`piJsonValidationTool.cjs`（143 行）、`outlineGenerationTaskV2.test.cjs`（172 行）、`shared/types/outline.ts`（45 行）
- ✅ **关键段精读**：`OutlineEditPage.tsx`（1586 行中约 300 行）、`technicalPlanStore.cjs`（约 150 行）、`aiService.cjs`（约 80 行）、`piRuntimeService.cjs`（约 150 行）、`agentService.cjs`（约 30 行）、`bidAnalysisTask.cjs`（关键行）
- ❌ **未审计**：`contentGenerationTask.cjs`（31 万字符）、`exportService.cjs`（9.1 万字符）、`feasibilityOutlineTask.cjs`（可行性报告的**平行实现**，**可能有可复用的目录生成逻辑**，建议后续单独立项对比）

**只读承诺**：本次审计**未修改参考仓库任何文件**，所有操作均为读取与检索。

---

*报告完毕。*

