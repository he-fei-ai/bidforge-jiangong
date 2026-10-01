# OpenBidKit 易标 — 审核预检 / 导出文档 / 知识库 三模块源码考古报告

> 仓库根：`J:\编程\OpenBidKit 易标\OpenBidKit_Yibiao-main-2026-09-14`（只读考古，未修改任何文件）
> 报告日期：2026-10-01
> 技术栈：Electron Main（`.cjs`）+ React Renderer（`.tsx`）+ 本地 .NET 10 助手（`openxmlhelper/`）+ SQLite（`sqliteDatabase.cjs`）
> 所有路径为该仓库相对路径；行号以本次实际读取的文件内容为准。

---

## 0. 总体地图（先看这张表）

| 需求项 | 真实落点 | 与「题面假设」的差异 |
|---|---|---|
| 全文一致性审计 | `client/electron/services/contentGenerationTask.cjs:1658-1900, 5354-6038` | 在**正文生成任务内部**，不在独立 service |
| 重复率检测 | `client/electron/services/duplicateCheckService.cjs`（2622 行） | **无 AI 提示词**，纯算法（n-gram/Dice/LCS/哈希） |
| 废标项检查 | `client/electron/services/rejectionCheckTask.cjs`（1441 行） | 3 个检查（废标/错别字/逻辑谬误），各含「整包」与「滚动分段」双流程 |
| 可行性报告 | `feasibilityReportPrompts.cjs` / `feasibilityReportTasks.cjs` / `feasibilityOutlineTask.cjs` | 独立于标书主线的一套任务 |
| 目录审核 | `outlineGenerationTaskV2.cjs:157-181, 680-733` | 返回结构是 `{status, issues, user_feedback, summary}`，**不是** `{passed, suggestions}` |
| 导出文档 | `exportService.cjs`（2213 行）+ `checkResultExportService.cjs` | Markdown → HTML(cheerio) → docx |
| Mermaid 三处 | 预览 `MarkdownRenderer.tsx:211` / 存储 `contentIllustrationGeneration.cjs:450-462` / 转图 `localImageRenderService.cjs:686` | ✅ 三处齐全 |
| 配图 | `contentIllustrationPlanning.cjs`（Agent 提名）+ `contentIllustrationGeneration.cjs`（程序拍板） | ✅ Agent 提名 + 程序校验，类型为 `html/ai/mermaid`（**不是** `ai/mermaid/none`） |
| 知识库 | `knowledgeBaseService.cjs`（2098 行）+ `knowledgeBaseStore.cjs` | ✅ 非 RAG，7 步流水线齐备 |

### 关键文件规模

| 文件 | 行数 |
|---|---|
| `client/electron/services/contentGenerationTask.cjs` | 312 KB（最大文件） |
| `client/electron/services/duplicateCheckService.cjs` | 2622 |
| `client/electron/services/exportService.cjs` | 2213 |
| `client/electron/services/knowledgeBaseService.cjs` | 2098 |
| `client/electron/services/rejectionCheckTask.cjs` | 1441 |
| `client/electron/services/knowledgeBaseStore.cjs` | 1138 |
| `client/electron/services/rejectionCheckStore.cjs` | 1073 |
| `client/electron/services/feasibilityReportStore.cjs` | 673 |
| `client/electron/services/feasibilityReportTasks.cjs` | 453 |
| `client/electron/services/checkResultExportService.cjs` | 420 |
| `client/electron/services/contentIllustrationGeneration.cjs` | 439 |
| `client/electron/services/localImageRenderService.cjs` | 759 |
| `client/electron/services/contentIllustrationPlanning.cjs` | 305 |
| `client/electron/services/feasibilityReportPrompts.cjs` | 309 |
| `client/electron/services/feasibilityOutlineTask.cjs` | 299 |
| `client/electron/services/openXmlHelperService.cjs` | 335 |
| `client/electron/utils/textEdit.cjs` | 417 |
| `client/electron/utils/mermaidPolicy.cjs` | 44 |

---

# 一、审核预检

## 1. 全文一致性审计

### 1.1 编排入口与分组策略

`contentGenerationTask.cjs:5826-6038` `runConsistencyAuditIfEnabled`。分组在 `buildConsistencyAuditGroups`：

**`contentGenerationTask.cjs:5354-5398`**
```js
  function buildConsistencyAuditTargets(auditTargetItemId = '') {
    const normalizedTargetId = String(auditTargetItemId || '').trim();
    return leaves
      .filter(({ item }) => !normalizedTargetId || item.id === normalizedTargetId)
      .map((context) => {
        const content = sections[context.item.id]?.content || context.item.content || '';
        return { ...context, content, words: getLeafWordCount(context.item) };
      })
      .filter(({ item, content }) => sections[item.id]?.status === 'success' && String(content || '').trim());
  }

  function buildConsistencyAuditGroups(targets) {
    const totalWords = (targets || []).reduce((sum, item) => sum + item.words, 0);
    if (!targets?.length) return [];

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
    if (current.items.length) groups.push(current);
    return groups.map((group, index) => ({ ...group, index: index + 1, total: groups.length, totalWords }));
  }
```

- 阈值常量：`contentGenerationTask.cjs:40-41`
  ```js
  const CONSISTENCY_AUDIT_GROUP_WORD_LIMIT = 300000;
  const CONSISTENCY_REPAIR_MAX_ATTEMPTS = 2;
  ```
- 策略：**先按总字数算最少组数 → 贪心装箱**，单组硬上限 30 万字。
- 并发：`contentConcurrency`（`:5949` `runItemsWithWorkerPool(remainingGroups, contentConcurrency, ...)`）。
- **预热机制**：多组时第 1 组串行跑完再并发剩余，用于吃掉 prompt cache 冷启动。

**`contentGenerationTask.cjs:5937-5953`**
```js
    if (auditGroups.length > 1) {
      const [warmupGroup, ...remainingGroups] = auditGroups;
      logs = [...logs, `开始全文一致性审计预热：第 ${warmupGroup.index}/${warmupGroup.total} 组。`];
      publishTaskUpdate({ status: 'running', progress: progressFor(leaves, sections), logs, stats: statsSnapshot() });

      await auditConsistencyGroup(warmupGroup);
      pauseIfRequested('正文生成已在一致性审计预热后暂停，可导出当前已完成内容，稍后继续。');

      if (remainingGroups.length) {
        continueAfterPromptCacheWarmup(`全文一致性审计预热完成，开始并发审计剩余 ${remainingGroups.length} 组。`);
        logs = [...logs, `开始并发审计剩余 ${remainingGroups.length} 组。`];
        publishTaskUpdate({ status: 'running', progress: progressFor(leaves, sections), logs, stats: statsSnapshot() });
        await runItemsWithWorkerPool(remainingGroups, contentConcurrency, auditConsistencyGroup, isPauseRequested);
      }
    } else {
      await runItemsWithWorkerPool(auditGroups, contentConcurrency, auditConsistencyGroup, isPauseRequested);
    }
```

---


### 1.2 审计提示词原文（逐字）

**`contentGenerationTask.cjs:1658-1691`**
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

> 全部消息都是 `role: 'user'`，**无 system 消息**——与 `全功能模块.txt` 描述的 system 分层不一致。

分组正文格式（`:1648-1656`）：
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

### 1.3 冲突数据结构

**`contentGenerationTask.cjs:1693-1729`** 归一化 + 硬校验：
```js
function normalizeConsistencyAuditResponse(value, allowedSectionIds) {
  const source = value?.result && typeof value.result === 'object' ? value.result : value || {};
  const rawConflicts = Array.isArray(source) ? source
    : Array.isArray(source.conflicts) ? source.conflicts
    : Array.isArray(source.items) ? source.items : [];
  const allowed = allowedSectionIds instanceof Set ? allowedSectionIds : new Set(allowedSectionIds || []);
  const issues = [];
  const conflicts = [];

  rawConflicts.forEach((item, index) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) { issues.push(`conflicts[${index}] 必须是对象`); return; }
    const sectionId = singleLine(item.section_id || item.sectionId || item.id || item.chapter_id || item.chapterId);
    if (!sectionId || !allowed.has(sectionId)) { issues.push(`conflicts[${index}].section_id 无效：${sectionId || '空'}`); return; }
    conflicts.push({
      section_id: sectionId,
      fact_title: singleLine(item.fact_title || item.factTitle || item.fact || item.title),
      evidence: String(item.evidence || item.quote || item.source || '').trim(),
      reason: String(item.reason || item.description || item.issue || '').trim(),
      severity: singleLine(item.severity || 'medium') || 'medium',
    });
  });

  if (issues.length) throw new Error(`审计结果格式无效：${issues.join('；')}`);
  return { conflicts };
}
```

**`contentGenerationTask.cjs:1731-1735`**
```js
function validateConsistencyAuditResponse(value) {

### 1.4 old_text / new_text 唯一命中替换（核心）

**`contentGenerationTask.cjs:1584-1616`**
```js
function applyExactConsistencyPatch(content, patch) {
  const currentContent = normalizeNewlines(content);
  const oldText = normalizeConsistencyPatchText(patch.old_text);
  const newText = normalizeConsistencyPatchText(patch.new_text);
  if (!oldText) { throw new Error('old_text 为空'); }
  if (!newText) { throw new Error('new_text 为空'); }
  if (oldText === newText) { throw new Error('old_text 与 new_text 相同'); }

  const startLine = Number(patch.start_line);
  const endLine = Number(patch.end_line);
  if (Number.isFinite(startLine) && Number.isFinite(endLine) && startLine > 0 && endLine >= startLine) {
    const candidate = extractLineRangeText(currentContent, startLine, endLine);
    if (candidate === oldText) {
      return replaceLineRange(currentContent, startLine, endLine, newText);
    }
  }

  const matches = findExactOccurrences(currentContent, oldText);
  if (!matches.length) { throw new Error('old_text 未在当前小节正文中找到'); }
  if (matches.length > 1) { throw new Error('old_text 在当前小节正文中出现多次，请提供更多上下文确保唯一定位'); }
  const index = matches[0];
  return `${currentContent.slice(0, index)}${newText}${currentContent.slice(index + oldText.length)}`;
}
```

**双通道命中**：
1. **行号通道**（`:1598-1605`）：`start_line/end_line` 抽出的行区间文本**逐字等于** `old_text` → 直接按行号切片替换。
2. **全文唯一通道**（`:1607-1615`）：`indexOf` 全量扫描，**必须恰好 1 次**；0 次或 >1 次一律抛错。

`findExactOccurrences`（`:1517-1528`）是无重叠的全量扫描：
```js
function findExactOccurrences(content, search) {
  const indexes = [];
  if (!search) return indexes;
  let startIndex = 0;
  while (startIndex <= content.length) {
    const index = content.indexOf(search, startIndex);
    if (index < 0) break;
    indexes.push(index);
    startIndex = index + search.length;   // 前进整个 search 长度，避免自重叠误计
  }
  return indexes;
}
```

行号通道依赖的两个工具（`:1530-1550`）：
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

function replaceLineRange(content, startLine, endLine, replacement) {
  const lines = normalizeNewlines(content).split('\n');
  const start = Math.max(1, Math.round(Number(startLine) || 0));
  const end = Math.max(start, Math.round(Number(endLine) || 0));
  const nextLines = [
    ...lines.slice(0, start - 1),
    ...normalizeNewlines(replacement).split('\n'),
    ...lines.slice(end),
  ];
  return nextLines.join('\n');
}
```

**归一化只有一条：去行号前缀**（`:1445-1447` + `:1439-1443`）：
```js
function normalizeConsistencyPatchText(text) {
  return stripPromptLineNumbers(text).trim();
}
```
```js
    .split('\n')
    .map((line) => line.replace(/^\[\d{1,6}\]\s?/, ''))
    .join('\n');
```

> ⚠️ 与题面「宽松匹配仅折叠空白」的描述**不符**：本仓**不折叠空白**，只剥 `[nnn]` 行号前缀，匹配是**逐字节严格相等**。这是刻意的——宽松匹配反而会引入「猜错改哪一处」的风险。

带行号正文渲染（`:1455-1460`）：
```js
function formatContentWithLineNumbers(content) {
  const lines = normalizeNewlines(content).split('\n');
  const width = Math.max(3, String(lines.length).length);
  return lines
    .map((line, index) => `[${String(index + 1).padStart(width, '0')}] ${line}`)
    .join('\n');
}
```

**诊断信息**（`:1552-1582`）——失败时把匹配细节全量留痕：
```js
function describeConsistencyPatchMatch(content, patch) {
  const currentContent = normalizeNewlines(content);
  const oldText = normalizeConsistencyPatchText(patch.old_text);
  const newText = normalizeConsistencyPatchText(patch.new_text);
  const startLine = Number(patch.start_line);
  const endLine = Number(patch.end_line);
  const detail = {
    section_id: singleLine(patch.section_id),
    start_line: Number.isFinite(startLine) ? startLine : 0,
    end_line: Number.isFinite(endLine) ? endLine : 0,
    old_text: oldText,
    new_text: newText,
    old_text_metrics: textMetrics(oldText),
    new_text_metrics: textMetrics(newText),
    before_content_metrics: textMetrics(currentContent),
    line_range: null,
    exact_match_count: 0,
  };

**修复提示词原文**（`contentGenerationTask.cjs:1759-1804`）：
```js
function buildConsistencyRepairMessages({ context, conflicts, globalFactsText, bidAnalysisFactsText, currentContent, attempt, failures, tableRequirement, globalFactsMode }) {
  const { item } = context;
  const tableAllowed = normalizeTableRequirement(tableRequirement) !== 'none';
  const failureBlock = (failures || []).length
    ? `\n上次修复应用失败原因：\n${failures.map((failure, index) => `${index + 1}. ${failure}`).join('\n')}\n请重新返回能够在当前正文中唯一定位的 old_text。`
    : '';

  return [
    {
      role: 'user',
      content: `你是投标技术方案正文一致性修复助手。请只针对当前小节返回局部精确替换 patch。

要求：
1. 只返回 JSON，不要输出解释、总结或 Markdown 代码围栏。
2. 不要返回完整正文，只返回需要局部替换的 patches。
3. 事实输入比当前小节实际需要的更多；正文没有涉及的事实必须忽略。
4. 目标只修正正文中与事实冲突的内容，不要参照事实重写或扩充正文。
5. 不要优化文风，不要新增无关事实，不要新增新的承诺。
6. old_text 必须是当前小节正文中逐字存在的原文块，建议包含足够前后上下文，确保只出现一次。
7. ${tableAllowed ? '如果修改表格，old_text 必须包含完整表格行或完整表格块，不要只返回单元格碎片。' : '本次配置为不要表格；如果冲突位于表格中，new_text 必须把相关内容改为普通文字或普通列表，不得继续返回 Markdown 表格或 HTML 表格。'}
8. new_text 是替换后的正文块，不要包含章节标题，不要包含行号。
9. ${tableAllowed ? '保留 Markdown 表格、列表、代码块、图片和 Mermaid 块结构。' : '保留普通列表、代码块、图片和 Mermaid 块结构；不得新增或保留 Markdown 表格、HTML 表格。'}
10. start_line/end_line 使用下方带行号正文中的 1-based 行号；如果不确定也必须提供可唯一匹配的 old_text。${buildContentFactCompletenessInstruction(globalFactsMode) ? `\n\n${buildContentFactCompletenessInstruction(globalFactsMode)}\n不得把【待填写】改成具体值，也不得为缺失项杜撰事实。` : ''}

返回格式：
{
  "patches": [
    {
      "section_id": "当前小节编号",
      "start_line": 2,
      "end_line": 4,
      "old_text": "当前正文中逐字存在且唯一的原文块，不包含行号",
      "new_text": "替换后的正文块，不包含行号",
      "reason": "修复了哪个事实冲突"
    }
  ]
}`,
    },
    { role: 'user', content: `Step04 全局事实变量：\n${globalFactsText || '未提供'}` },
    { role: 'user', content: `Step02 关键解析结果（项目信息、甲方信息、交货和服务要求）：\n${bidAnalysisFactsText || '未提供'}` },
    { role: 'user', content: `当前小节：${item.id || 'unknown'} ${item.title || '未命名章节'}\n路径：${formatChapterPath(context)}\n描述：${item.description || ''}` },
    { role: 'user', content: `审计发现的冲突：\n${JSON.stringify(conflicts || [], null, 2)}` },
    { role: 'user', content: `当前小节正文（带行号；patch 的 old_text/new_text 不要包含这些行号）：\n${formatContentWithLineNumbers(currentContent)}` },
    { role: 'user', content: `patches[*].section_id 必须是 ${item.id || 'unknown'}。修复尝试次数：${attempt}/${CONSISTENCY_REPAIR_MAX_ATTEMPTS}${failureBlock}\n请只返回 JSON。` },
  ];
}
```

> **失败反馈是闭环的**：`failures` 来自上一轮 `applyConsistencyRepairPatches` 的 `errors`，逐条拼进下一轮提示词，明确要求「返回能唯一定位的 old_text」。

响应归一化（`:1806-1834`）——吸收 4 种键名写法：
```js
function normalizeConsistencyRepairResponse(value, expectedSectionId) {
  const source = value?.result && typeof value.result === 'object' ? value.result : value || {};
  const rawPatches = Array.isArray(source)
    ? source
    : Array.isArray(source.patches)
      ? source.patches
      : Array.isArray(source.operations)
        ? source.operations
        : (source.old_text || source.oldText || source.new_text || source.newText)
          ? [source]
          : [];
  return {
    patches: rawPatches.map((patch) => {
      const rawSectionId = singleLine(patch?.section_id || patch?.sectionId || patch?.id || '');
      const sectionId = rawSectionId && rawSectionId !== '当前小节编号' ? rawSectionId : expectedSectionId;

### 1.5 失败重试

**`contentGenerationTask.cjs:5697-5824`**
```js
  async function repairConsistencySection({ context, conflicts }) {
    const { item } = context;
    let currentContent = sections[item.id]?.content || item.content || '';
    let failures = [];
    let appliedTotal = 0;
    writeDeveloperLog('consistency.repair.section.start', {
      section_id: item.id,
      title: item.title || '未命名章节',
      conflict_count: (conflicts || []).length,
      conflicts,
      content_metrics: textMetrics(currentContent),
    });

    for (let attempt = 1; attempt <= CONSISTENCY_REPAIR_MAX_ATTEMPTS; attempt += 1) {
      if (isPauseRequested()) {
        writeDeveloperLog('consistency.repair.section.paused', {
          section_id: item.id, title: item.title || '未命名章节', applied_count: appliedTotal,
        });
        return { appliedCount: appliedTotal, failed: false, paused: true };
      }

      try {
        writeDeveloperLog('consistency.repair.attempt.start', {
          section_id: item.id, title: item.title || '未命名章节', attempt,
          max_attempts: CONSISTENCY_REPAIR_MAX_ATTEMPTS,
          previous_failures: failures, content_metrics: textMetrics(currentContent),
        });
        const response = await aiService.collectJsonResponse({
          messages: buildConsistencyRepairMessages({
            context, conflicts, globalFactsText, bidAnalysisFactsText, currentContent,
            attempt, failures, tableRequirement, globalFactsMode,
          }),
          logTitle: `一致性修复-${item.id}-${item.title || '未命名章节'}`,
          progressLabel: '正文一致性修复',
          failureMessage: '模型返回的正文一致性修复结果格式无效',
          normalizer: (value) => normalizeConsistencyRepairResponse(value, item.id),
          validator: validateConsistencyRepairResponse,
          repairMessagesBuilder: (contextForRepair) => buildConsistencyRepairJsonRepairMessages(contextForRepair, item.id),
          max_retries: 1,
        });
        writeDeveloperLog('consistency.repair.response', {
          section_id: item.id, title: item.title || '未命名章节', attempt,
          patch_count: response.patches.length, patches: response.patches,
        });

        if (!response.patches.length) {
          failures = ['模型未返回可应用的 patches'];
          writeDeveloperLog('consistency.repair.no_patches', {
            section_id: item.id, title: item.title || '未命名章节', attempt,
          });
        } else {
          const result = applyConsistencyRepairPatches(currentContent, response.patches);
          writeDeveloperLog('consistency.repair.apply_result', {
            section_id: item.id, title: item.title || '未命名章节', attempt,
            applied_count: result.appliedCount, errors: result.errors, patch_results: result.patchResults,
          });
          if (result.appliedCount > 0) {
            currentContent = result.content;
            appliedTotal += result.appliedCount;
            rememberTouchedItem(item.id);
            saveSection(item, { status: 'success', content: currentContent, error: undefined }, currentContent, { logs });
            writeDeveloperLog('consistency.repair.section.saved', {
              section_id: item.id, title: item.title || '未命名章节', attempt,
              applied_total: appliedTotal, content_metrics: textMetrics(currentContent),
            });
          }
          if (!result.errors.length) {
            writeDeveloperLog('consistency.repair.section.done', {
              section_id: item.id, title: item.title || '未命名章节',
              applied_count: appliedTotal, failed: false,
            });
            return { appliedCount: appliedTotal, failed: false, paused: false };
          }
          failures = result.errors;
        }

单组审计调用（`:5886-5935`）：
```js
    async function auditConsistencyGroup(group) {
      const allowedIds = new Set(group.items.map(({ item }) => item.id).filter(Boolean));
      try {
        writeDeveloperLog('consistency.audit.group.start', {
          index: group.index, total: group.total, words: group.words, allowed_ids: [...allowedIds],
        });
        const response = await aiService.collectJsonResponse({
          messages: buildConsistencyAuditMessages({ group, globalFactsText, bidAnalysisFactsText, globalFactsMode }),
          logTitle: `一致性审计-${group.index}-${group.total}`,
          progressLabel: '全文一致性审计',
          failureMessage: '模型返回的一致性审计结果格式无效',
          normalizer: (value) => normalizeConsistencyAuditResponse(value, allowedIds),
          validator: validateConsistencyAuditResponse,
          repairMessagesBuilder: (contextForRepair) => buildConsistencyAuditRepairMessages(contextForRepair, allowedIds),
          max_retries: 1,
        });

        for (const conflict of response.conflicts) {
          const list = conflictsBySectionId.get(conflict.section_id) || [];
          list.push(conflict);
          conflictsBySectionId.set(conflict.section_id, list);
        }
        contentStats.audit_conflict_total = conflictsBySectionId.size;
        logs = [...logs, `一致性审计完成：第 ${group.index}/${group.total} 组，发现 ${response.conflicts.length} 条冲突，累计 ${conflictsBySectionId.size} 个冲突小节。`];
        writeDeveloperLog('consistency.audit.group.success', {
          index: group.index, total: group.total,
          conflict_count: response.conflicts.length, conflicts: response.conflicts,
          conflict_section_count: conflictsBySectionId.size,
        });
      } catch (error) {
        if (isPauseLikeError(error)) { throw error; }
        logs = [...logs, `一致性审计失败：第 ${group.index}/${group.total} 组，${error.message || '模型返回无效'}，已跳过该组。`];
        writeDeveloperLog('consistency.audit.group.error', {
          index: group.index, total: group.total, error: error.message || '模型返回无效', stack: error.stack || '',
        });
      } finally {
        contentStats.audit_group_completed += 1;
        publishTaskUpdate({ status: 'running', progress: progressFor(leaves, sections), logs, stats: statsSnapshot() });
      }
    }
```
> ⚠️ **单组失败只 `log` 不抛**，其余组继续（`:5924` 「已跳过该组」）。

修复目标筛选（`:5957-5966`）：
```js
    const repairTargets = Array.from(conflictsBySectionId.entries())
      .map(([sectionId, conflicts]) => ({ context: targetById.get(sectionId), conflicts }))
      .filter((target) => target.context);
    contentStats.audit_step = 'fixing';
    contentStats.audit_fix_total = repairTargets.length;
    contentStats.audit_fix_completed = 0;
    contentStats.audit_fix_failed = 0;
    logs = [...logs, repairTargets.length
      ? `一致性审计发现 ${repairTargets.length} 个冲突小节，开始局部修复，并发 ${contentConcurrency}。`
      : '一致性审计未发现需要修复的事实冲突。'];
```

单目标修复 + 预热（`:5985-6025`）：
```js
    let fixedCount = 0;
    async function repairConsistencyTarget(target) {
      const item = target.context.item;
      try {

### 1.6 JSON 校验与修复原则在代码中的落地

统一实现在 `aiService.cjs:782-788`：
```js
function normalizeJsonPayload(request, parsed) {
  const normalized = request.normalizer ? request.normalizer(parsed) : parsed;
  if (request.validator) { request.validator(normalized); }
  return normalized;
}
```

**`aiService.cjs:832-888`（核心重试机）**
```js
async function collectJsonResponseWithConfig(app, config, request) {
  const preparedMessages = await prepareMultimodalMessages(config, request.messages);
  const maxRetries = request.max_retries ?? 2;
  const totalAttempts = maxRetries + 1;
  const responseFormat = request.response_format || { type: 'json_object' };
  const progressLabel = request.progressLabel || 'JSON结果';
  const failureMessage = request.failureMessage || '模型返回的 JSON 数据格式无效';
  const logTitle = resolveAiLogTitle(request, progressLabel);
  let lastError = null;

  for (let attempt = 0; attempt < totalAttempts; attempt += 1) {
    const content = await chatWithConfig(app, config, {
      messages: preparedMessages,
      response_format: responseFormat,
      timeout_ms: request.timeout_ms,
      timeout_message: request.timeout_message,
      logTitle,
      signal: request.signal,
    });

    try {
      const parsed = parseJsonContent(content);
      return normalizeJsonPayload(request, parsed);
    } catch (error) {
      lastError = error;
      const issues = formatJsonIssues(error);

      try {
        const repairedContent = await repairJsonResponse(
          app, config, content, issues, responseFormat, request.progressCallback,
          progressLabel, request.repairMessagesBuilder, logTitle, request.signal,
        );
        const repairedParsed = parseJsonContent(repairedContent);
        return normalizeJsonPayload(request, repairedParsed);
      } catch (repairError) {
        lastError = repairError;

        if (attempt === maxRetries) {
          await emitProgress(request.progressCallback, `${progressLabel}连续 ${totalAttempts} 次校验失败。`);
          throw new Error(failureMessage);
        }

        await emitProgress(request.progressCallback, `${progressLabel}第 ${attempt + 1}/${totalAttempts} 次校验失败，正在重试。`);
      }
    }
  }

一致性审计的修复提示词（`contentGenerationTask.cjs:1737-1757`）：
```js
function buildConsistencyAuditRepairMessages({ invalidContent, issues }, allowedSectionIds) {
  const issueLines = (issues || []).map((item, index) => `${index + 1}. ${item}`).join('\n');
  return [
    {
      role: 'user',
      content: `你是严格的 JSON 修复器。请把模型输出修复为"全文一致性审计"JSON。

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

正文一致性局部修复的修复提示词（`:1856-1874`）：
```js
function buildConsistencyRepairJsonRepairMessages({ invalidContent, issues }, expectedSectionId) {
  const issueLines = (issues || []).map((item, index) => `${index + 1}. ${item}`).join('\n');
  return [
    {
      role: 'user',
      content: `你是严格的 JSON 修复器。请把模型输出修复为"正文一致性局部修复"JSON。

必须满足：
1. 顶层只能包含 patches 数组。
2. 每条 patch 必须包含 section_id、start_line、end_line、old_text、new_text、reason。
3. section_id 必须是 ${expectedSectionId}。
4. old_text 和 new_text 都不能包含行号，不能相同，不能为空。
5. 不要返回完整正文，不要输出 Markdown 或解释文字。
6. 如果无法修复，返回 {"patches":[]}。`,
    },
    { role: 'user', content: `错误列表：\n${issueLines}` },
    { role: 'user', content: `待修复内容：\n\`\`\`json\n${String(invalidContent || '').slice(0, 60000)}\n\`\`\`` },
  ];
}
```
> `slice(0, 60000)` 是统一的坏内容截断上限（两处 + `aiService.cjs:765` 三处一致）。

### 1.7 SSE / 进度反馈

**Electron 进程内不是 SSE**，是 `publishTaskUpdate`（渲染进程通过 IPC 订阅）。

**`contentGenerationTask.cjs:5846-5881`（阶段初始化 + 启动日志）**
```js
    contentStats.phase = 'auditing';
    contentStats.audit_step = 'checking';
    contentStats.audit_repair_mode = 'normal';
    contentStats.audit_group_total = auditGroups.length;
    contentStats.audit_group_completed = 0;
    contentStats.audit_conflict_total = 0;
    contentStats.audit_fix_total = 0;
    contentStats.audit_fix_completed = 0;
    contentStats.audit_fix_failed = 0;
    contentStats.audit_agent_step_total = 0;
    contentStats.audit_agent_step_completed = 0;
    contentStats.audit_agent_step_label = '';
    contentStats.audit_agent_changed_sections = 0;
    contentStats.audit_agent_failed_sections = 0;
    logs = [...logs, `开始全文一致性审计：${auditTargets.length} 个小节，拆分为 ${auditGroups.length} 组，并发 ${contentConcurrency}。`];
    const auditRuntime = syncRuntime({ phase: 'auditing' });
    writeDeveloperLog('consistency.audit.start', {
      target_item_id: options.targetItemId || targetItemId || '',
      target_count: auditTargets.length,
      group_count: auditGroups.length,
      concurrency: contentConcurrency,
      group_word_limit: CONSISTENCY_AUDIT_GROUP_WORD_LIMIT,
      groups: auditGroups.map((group) => ({
        index: group.index,
        total: group.total,
        words: group.words,
        target_words: group.targetWords,
        total_words: group.totalWords,

## 2. 重复率检测（duplicateCheck）

**结论先行：全模块无任何 AI 提示词，100% 纯算法。** 目标是「多份投标文件之间 + 投标文件 vs 招标文件」的双向查重。

### 2.1 四条分析线（各有独立状态机）

`duplicateCheckService.cjs:2122-2186`：
```js
function createInitialAnalysis(signature, bidFiles) {
  const total = bidFiles.length;
  return {
    status: 'running',
    progress: 0,
    message: '正在启动元数据分析',
    signature,
    started_at: now(),
    updated_at: now(),
    contentExtraction: { status: 'running', completed: 0, total: 0 },
    metadataExtraction: { status: total ? 'running' : 'success', completed: 0, total },
    files: [],
    rows: [],
    contentFiles: [],
    logs: [],
  };
}

function createInitialOutlineAnalysis(signature, bidFiles) {
  return {
    status: 'pending',
    progress: 0,
    message: '等待元数据提取完成后开始目录分析',
    signature,
    started_at: now(),
    updated_at: now(),
    tenderSentenceCount: 0,
    tenderMatchedItemCount: 0,
    extraction: { status: bidFiles.length ? 'pending' : 'success', completed: 0, total: bidFiles.length },
    files: [],
    duplicateGroups: [],
    pairwiseSimilarities: [],
  };
}

function createInitialContentAnalysis(signature, bidFiles) {
  return {
    status: 'pending',
    progress: 0,
    message: '等待正文内容提取完成后开始正文比对',
    signature,
    started_at: now(),
    updated_at: now(),
    tenderSentenceCount: 0,
    tenderMatchedSentenceCount: 0,
    totalSentenceCount: 0,
    extraction: { status: bidFiles.length ? 'pending' : 'success', completed: 0, total: bidFiles.length },
    duplicateSentences: [],
  };
}

function createInitialImageAnalysis(signature, bidFiles) {
  return {
    status: 'pending',
    progress: 0,
    message: '等待正文内容提取完成后开始图片比对',
    signature,
    started_at: now(),
    updated_at: now(),
    extraction: { status: bidFiles.length ? 'pending' : 'success', completed: 0, total: bidFiles.length },
    totalImageCount: 0,
    files: [],
    duplicateImages: [],
  };
}
```
整体进度是四条线的均值（`:2235-2243`）：
```js
  function overallProgress(state) {
    const values = [
      analysisProgress(state?.metadataAnalysis),
      analysisProgress(state?.outlineAnalysis),
      analysisProgress(state?.contentAnalysis),
      analysisProgress(state?.imageAnalysis),
    ];
    return Math.round(values.reduce((sum, value) => sum + value, 0) / values.length);
  }
```

### 2.2 算法一：字符 2-gram Dice（标题相似）


### 2.3 算法二：LCS 顺序相似度 + 加权综合分

**`duplicateCheckService.cjs:1350-1359`**
```js
function lcsSimilarity(left, right) {
  if (!left.length || !right.length) return 0;
  const dp = Array.from({ length: left.length + 1 }, () => Array(right.length + 1).fill(0));
  for (let i = 1; i <= left.length; i += 1) {
    for (let j = 1; j <= right.length; j += 1) {
      dp[i][j] = left[i - 1] === right[j - 1] ? dp[i - 1][j - 1] + 1 : Math.max(dp[i - 1][j], dp[i][j - 1]);
    }
  }
  return dp[left.length][right.length] / Math.max(left.length, right.length);
}
```
**综合分权重**（`:1436`）：`pathOverlap*0.45 + titleOverlap*0.35 + orderSimilarity*0.2`

**风险分级**（`:1361-1366`）：
```js
function riskFromScore(score) {
  if (score >= 0.75) return 'high';
  if (score >= 0.55) return 'medium';
  if (score >= 0.35) return 'low';
  return 'none';
}
```

### 2.4 算法三：倒排索引 + Dice（正文句子 vs 招标文件）

**`duplicateCheckService.cjs:1896-1907`**
```js
function charBigramsFromLooseText(value) {
  const text = String(value || '');
  if (!text) return new Set();
  if (text.length === 1) return new Set([text]);
  const grams = new Set();
  for (let index = 0; index < text.length - 1; index += 1) grams.add(text.slice(index, index + 2));
  return grams;
}

function diceSimilarityFromShared(shared, leftSize, rightSize) {
  return (2 * shared) / Math.max(leftSize + rightSize, 1);
}
```
**倒排索引 + 候选计数**（`:1946-1978`）：
```js
  function matchNear(sentence) {
    const looseText = buildTenderLooseText(sentence.normalized);
    if (!shouldApplyNearTenderMatch(sentence.normalized, looseText)) return null;
    const grams = charBigramsFromLooseText(looseText);
    if (grams.size < 4) return null;
    const candidates = [];
    for (const gram of grams) {
      for (const index of gramIndex.get(gram) || []) {
        if (candidateCounts[index] === 0) candidates.push(index);
        candidateCounts[index] += 1;
      }
    }
    let best = null;
    const compactLength = looseText.length;
    for (const index of candidates) {
      const shared = candidateCounts[index];
      candidateCounts[index] = 0;
      const entry = entries[index];
      if (!entry?.grams?.size) continue;
      const shorter = Math.min(grams.size, entry.grams.size);
      const longer = Math.max(grams.size, entry.grams.size);
      const containment = shared / Math.max(shorter, 1);
      const dice = diceSimilarityFromShared(shared, grams.size, entry.grams.size);
      const lengthRatio = shorter / Math.max(longer, 1);
      const allowed = compactLength >= 30
        ? containment >= 0.9 && dice >= 0.82 && lengthRatio >= 0.5
        : containment >= 0.95 && dice >= 0.88 && lengthRatio >= 0.55;
      if (!allowed) continue;
      if (!best || dice > best.dice) best = { reason: 'near', dice, containment, tender: entry.normalized };
    }
    return best;
  }
```
**双阈值（按句子长度分档）**：
| 句子长度 | containment | dice | lengthRatio |
|---|---|---|---|
| ≥30 字 | ≥0.90 | ≥0.82 | ≥0.50 |
| <30 字 | ≥0.95 | ≥0.88 | ≥0.55 |

### 2.5 算法四：5 级级联匹配（exact→strict→field→skeleton→near）

**`duplicateCheckService.cjs:1980-1996`**
```js
  return {
    tenderSentenceCount: exactSet.size,
    match(sentence) {
      const normalized = sentence?.normalized || '';
      if (!normalized) return null;
      if (exactSet.has(normalized)) return { reason: 'exact' };
      const strictKey = buildTenderStrictKey(normalized);
      if (strictKey && strictSet.has(strictKey)) return { reason: 'strict' };
      const parsedField = parseTenderFormatField(normalized);
      if (parsedField && isTenderFieldAllowed(parsedField.field) && fieldSet.has(parsedField.field) && isSafeTenderFieldTail(parsedField.tail)) {
        return { reason: 'field' };
      }
      const skeletonKey = buildTenderSkeletonKey(normalized);
      if (isTenderSkeletonAllowed(normalized, skeletonKey) && skeletonSet.has(skeletonKey)) return { reason: 'skeleton' };
      return matchNear(sentence);
    },
  };
```

**skeleton 占位符归一**（`:1826-1842`）——把可变数字/日期/金额/页码全替换成语义占位符：
```js
function buildTenderSkeletonKey(value) {
  let text = normalizeTenderComparableText(value)
    .replace(/\b\d{4}年\d{1,2}月\d{1,2}日\b/g, '{date}')
    .replace(/\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b/g, '{date}')
    .replace(/\b[A-Z]{2,}[-A-Z0-9]{4,}\b/gi, '{code}')
    .replace(/\d+(?:\.\d+)?\s*万元/g, '{money}')
    .replace(/\d+(?:\.\d+)?\s*元/g, '{money}')
    .replace(/\d+(?:\.\d+)?\s*%/g, '{percent}')
    .replace(/\d+(?:\.\d+)?\s*分/g, '{score}')
    .replace(/P\s*\d+(?:\s*[-~至]\s*P?\s*\d+)?/gi, '{page}')
    .replace(/\b\d+(?:\.\d+)?\b/g, '{num}');
  text = text
    .replace(/[\s　]+/g, '')
    .replace(/[.,，。;；:：、!！?？"'“”‘’《》<>〈〉()[\]【】{}]/g, '')

### 2.6 算法五：图片哈希

`buildDuplicateImages`（`:2115-2120`）按 `hash` 分组、`file_ids.length > 1` 过滤：
```js
function buildDuplicateImages(globalImages) {
  return Array.from(globalImages.values())
    .filter((item) => item.file_ids.length > 1)
    .sort((a, b) => b.file_ids.length - a.file_ids.length || Object.values(b.occurrences).reduce((sum, count) => sum + count, 0) - Object.values(a.occurrences).reduce((sum, count) => sum + count, 0))
    .map((item, index) => ({ ...item, id: `I${String(index + 1).padStart(6, '0')}` }));
}
```
图片上下文提取：`:2048-2060`（剥离代码围栏 → 逐行找 `![...]()` / `<img src>` → 维护 heading 栈定位所在目录 → `getPreviousImageSentence` 取图片前一句）。

### 2.7 返回结构

目录（`buildOutlineComparison:1450`）：
```js
  return { duplicateGroups: groups.sort((a, b) => b.score - a.score || b.file_ids.length - a.file_ids.length), pairwiseSimilarities };
```
分组对象（`:1389-1391`）：`{ id, type: 'duplicate'|'similar', title, score, file_ids, item_ids, paths }`。
成对相似度（`:1437-1446`）：`{ file_a_id, file_b_id, score, title_overlap, path_overlap, order_similarity, shared_count, risk }`。
重复句子（`:1999-2004`）：
```js
function buildDuplicateSentences(globalSentences) {
  return Array.from(globalSentences.values())
    .filter((item) => item.file_ids.length > 1)
    .sort((a, b) => b.file_ids.length - a.file_ids.length || b.sentence.length - a.sentence.length || a.first_order - b.first_order)
    .map((item, index) => ({ ...item, id: `S${String(index + 1).padStart(6, '0')}` }));
}
```
其中 `item = { sentence, normalized, file_ids, occurrences, first_order }`。

## 3. 废标项检查（rejectionCheck）

`rejectionCheckTask.cjs` 实际做 **3 件事**（`runOptions` 三开关，`:1616-1620`）：
```js
  const enabledTasks = [
    runOptions.rejectionCheck ? 'rejection' : '',
    runOptions.typoCheck ? 'typo' : '',
    runOptions.logicCheck ? 'logic' : '',
  ].filter(Boolean);
  if (!enabledTasks.length) throw new Error('请至少启用一种检查');
```
三任务 `Promise.all` 并发（`:1709`），各自独立 `inputSignature` 防串档。

### 3.1 检查项清单（前置产物）

废标检查的**输入不是检查项本身，而是从招标文件解析出的「无效投标」+「废标项」文本**（`invalidBidAndRejectionItems`），由 `runInvalidBidAndRejectionItemsExtractionTask`（`:1505`）调用 `bidAnalysisTask.cjs` 的 `runInvalidBidAndRejectionItemsExtraction` 产出。缺失直接拒绝启动（`:1622-1624`）：
```js
  if (runOptions.rejectionCheck && (!invalidBidAndRejectionItems.trim() || !rejectionInputSignature)) {
    throw new Error('请先完成无效与废标项解析');
  }
```

公共输入三段（`:73-105`）：
```js
function buildCommonRejectionCheckMessages(input) {
  const messages = [
    {
      role: 'user',
      content: `【废标项检查输入 v1｜检查项】
以下内容来自招标文件"无效投标"和"废标项"解析结果。后续任务必须优先基于这些检查口径，不要自行扩大到无法从电子投标文件判断的事项。

${input.invalidBidAndRejectionItems}`,
    },
  ];
```
（自定义检查项段与原文段落见 §17 语义耦合清单 `:84-102`）

### 3.2 判定逻辑：三轮法（整包未超上下文时）

`runRejectionItemCheck`（`:1437-1464`）：
```js
async function runRejectionItemCheck(aiService, input, onProgress) {
  if (shouldUseSegmentedRejectionFlow(aiService, input)) {
    return runRollingRejectionItemCheck(aiService, input, onProgress);
  }

  onProgress('第一轮：正在分析检查范围。');
  const analysis = await runText(
    aiService,
    { messages: buildRejectionCheckAnalysisMessages(input) },
    onProgress,
    '第一轮分析',
  );
  onProgress('第二轮：正在逐项检查投标文件。');
  const draftFindings = await runText(
    aiService,
    { messages: buildRejectionCheckInspectionMessages(input, analysis) },
    onProgress,
    '第二轮检查',
  );
  onProgress('第三轮：正在补充、去重并生成结果。');
  const payload = await runJson(aiService, {
    messages: buildRejectionCheckFinalMessages(input, analysis, draftFindings),
    schemaName: 'RejectionCheckFindings',
    progressLabel: '废标项检查结果',
    failureMessage: '废标项检查结果格式无效，请重新检查',
  }, onProgress, '第三轮定稿');
  return normalizeRejectionCheckFindings(payload, input.bidDocuments);
}
```

**第 1 轮：分析范围**（`:112-122`）
```js
      content: `【废标项检查任务 v1｜第一轮：分析】
请先分析检查范围，不要输出最终风险列表。

分析要求：
1. 梳理"无效投标"和"废标项"中哪些能通过电子投标文件判断。
2. 明确排除签字、盖章、密封、纸质正副本、现场递交、开标现场授权到场、纸质文件封装等纸质或线下事项。
3. 结合各投标文件目录和正文结构，指出重点核查章节、附件、报价、资格材料、技术/商务响应位置，并说明是否存在不同文件需要分别关注的风险。
4. 判断材料是否缺失时，先识别章节标题、目录项、附件标题、材料清单项、表格条目、页码线索、图片占位线索等结构性文本线索；只要存在这类线索，就不能因为图片或扫描件正文不可见而判定缺失。
5. 如果某项检查需要外部事实、现场行为或纸质原件才能判断，标记为"不纳入电子文件检查"。
6. 仅输出分析结论，使用简体中文。`,
```

**第 2 轮：逐项检查**（`chat` 纯文本，允许 Markdown 草稿）——关键约束 `:137-143`：
```js
检查要求：
1. 每条风险必须有某一份投标文件中的明确证据，并写明 bidDocumentId；证据不足不要输出。
2. 不检查签字、盖章、密封、纸质正副本、现场递交、纸质原件等事项。
3. 重点关注实质性条款未响应、必要章节或附件缺失、资格材料明显缺失/过期、报价或关键承诺前后矛盾、技术/商务偏离未说明等电子正文可判断风险。
4. 判断"材料缺失"时，只有在目录、章节标题、附件标题、材料清单、正文、表格和其他结构性线索中均找不到对应材料痕迹，才可以输出疑似缺失；不得仅因图片内容、扫描件正文或附件正文不可见而输出缺失风险。
5. 如果投标文件中已有对应材料的结构性文本线索，应视为至少有提交线索，可提示人工复核内容完整性，但不要判定为缺失。
6. 区分风险类型：无效标使用 invalidBid，废标项使用 rejectionItem。
7. 暂不要求 JSON，可用结构化 Markdown 输出初步结果。`,
```

**第 3 轮：定稿 JSON**（`:157-186`）
```js
      content: `【废标项检查任务 v1｜第三轮：补充与定稿】
请对第二轮结果去重、合并、补漏，并删除不符合要求的条目，最终只输出 JSON。

### 3.3 滚动分段流程（超上下文时自动切换）

判定（`:387-392`）：`shouldUseSegmentedRejectionFlow` → `splitUserTextByContextLimit(...).length > 1`。
比例常量（`:6-14`）：
```js
const checkRunStatus = ['idle', 'running', 'success', 'error'];
const typoExcerptRadius = 8;
const fullPromptLimitRatio = 0.6;
const rollingSegmentLimitRatio = 0.55;
const typoSegmentLimitRatio = 0.7;
const rollingSummaryEvidenceLimit = 60;
const rollingSummaryResolvedLimit = 40;
const rollingSummaryConfirmedLimit = 60;
const finalCandidateBatchSize = 20;
```

**核心状态机**（`runRollingRejectionItemCheck:1318-1362`）— 4 桶 + 增量 patch：
```js
async function runRollingRejectionItemCheck(aiService, input, onProgress) {
  const config = getCurrentAiConfig(aiService);
  const segments = createBidPackageSegments(input.bidDocuments, config, rollingSegmentLimitRatio);
  let state = createEmptyRollingRejectionState();
  onProgress('正在按上下文长度滚动审阅投标包。');

  for (const segment of segments) {
    onProgress(`${segment.documentLabel}：正在滚动审阅投标包第 ${segment.segmentIndex}/${segment.totalSegments} 段。`);
    const stateSummary = createRollingRejectionStateSummary(state);
    const payload = await runJson(aiService, {
      messages: buildRollingRejectionSegmentMessages(input, segment, stateSummary),
      schemaName: 'RollingRejectionCheckPatch',
      progressLabel: '投标包废标项滚动审阅',
      failureMessage: '废标项滚动审阅状态格式无效，请重新检查',
    }, onProgress, '投标包废标项滚动审阅');
    state = applyRollingRejectionPatch(state, normalizeRollingRejectionPatch(payload, input.bidDocuments, segment.documentId));
  }

  onProgress('正在基于全投标包状态定稿废标项风险。');
  const candidates = createRejectionFinalCandidates(state);
  if (!candidates.length) return [];
  const batches = chunkItems(candidates, finalCandidateBatchSize);
  const findings = [];
  const finalSummary = createFinalRejectionStateSummary(state);
  for (const [batchIndex, batch] of batches.entries()) {
    onProgress(`正在定稿废标项风险第 ${batchIndex + 1}/${batches.length} 批。`);
    const finalPayload = await runJson(aiService, {
      messages: buildRejectionFinalBatchMessages(input, batch, finalSummary, batchIndex + 1, batches.length),
      schemaName: 'RejectionCheckFindings',
      progressLabel: '投标包废标项检查定稿',
      failureMessage: '废标项检查结果格式无效，请重新检查',
    }, onProgress, '投标包废标项检查定稿');
    findings.push(...normalizeRejectionCheckFindings(finalPayload, input.bidDocuments));
  }
  const mergedFindings = dedupeRejectionFindings(findings);
  if (!mergedFindings.length) return [];
  onProgress('正在全局合并废标项风险。');
  const mergedPayload = await runJson(aiService, {
    messages: buildRejectionGlobalMergeMessages(input, mergedFindings, finalSummary),
    schemaName: 'RejectionCheckGlobalMergeFindings',
    progressLabel: '废标项风险全局合稿',
    failureMessage: '废标项风险全局合稿结果格式无效，请重新检查',
  }, onProgress, '废标项风险全局合稿');
  return dedupeRejectionFindings(normalizeRejectionCheckFindings(mergedPayload, input.bidDocuments));
}
```

**4 桶状态**（`createEmptyRollingRejectionState:675-684`）：
```js
function createEmptyRollingRejectionState() {
  return {
    nextEvidenceSeq: 1,
    nextRiskSeq: 1,
    submittedEvidence: [],   // 已发现的「提交线索」——用于反证「材料缺失」
    pendingRisks: [],         // 待后续片段确认
    resolvedRisks: [],       // 已被后续片段排除
    confirmedRisks: [],      // 已闭环
  };
}
```
候选生成（`:999-1004`）：
```js
function createRejectionFinalCandidates(state) {
  return [
    ...state.confirmedRisks.map((item) => ({ ...item, candidateStatus: 'confirmed' })),
    ...state.pendingRisks.map((item) => ({ ...item, candidateStatus: 'pending' })),

**滚动审阅提示词原文**（`buildRollingRejectionSegmentMessages:1037-1087`，核心段）：
```js
    {
      role: 'user',
      content: `【废标项滚动检查｜当前状态摘要】
你正在按顺序审阅同一个投标包。完整权威状态由程序维护，你只能基于当前片段返回增量 patch。
下面是前序片段累计状态的精简摘要；如需更新或排除 pendingRisks，只能引用摘要中已有的 id。

${JSON.stringify(stateSummary, null, 2)}`,
    },
    {
      role: 'user',
      content: `【废标项滚动检查｜当前投标包片段】
投标包片段：第 ${segment.segmentIndex}/${segment.totalSegments} 段
当前片段所属文件：${segment.documentLabel}
当前片段默认 bidDocumentId：${segment.documentId}

本投标包有效 bidDocumentId：
${bidDocumentIdList}

重要限制：当前内容只是整个投标包的一段，不是全部投标文件。不得因为当前段或当前文件没有出现某项材料、附件、承诺或响应，就确认该材料缺失；这类问题只能先放入 pendingRisks，后续片段或其他投标文件可能会补充或推翻。

${segment.content}`,
    },
    {
      role: 'user',
      content: `【废标项滚动检查任务】
请基于当前片段输出增量 patch，只输出 JSON。不要返回完整累计状态，程序会负责合并和保留历史状态。

Patch 要求：
1. evidenceAdds 只放当前片段新增的章节标题、目录项、附件标题、材料清单项、表格条目、页码线索、图片占位线索、承诺或响应线索。
2. pendingRiskAdds 只放当前片段发现、但需要后续片段或其他文件继续确认的问题。
3. pendingRiskUpdates 只能引用状态摘要中 pendingRisks 的 id，用于补充或修正该待确认项。
4. pendingRiskResolves 只能引用状态摘要中 pendingRisks 的 id；如果当前片段证明某个"缺失/未响应"不成立，就在这里给出排除原因。
5. confirmedRiskAdds 只放当前片段或累计摘要已经提供明确投标文件证据，且不依赖纸质、线下或外部事实的风险。
6. 不检查签字、盖章、密封、纸质正副本、现场递交、纸质原件等事项。
7. 每条风险、证据和事实必须保留 bidDocumentId，且只能使用上方有效 bidDocumentId；如果当前片段没有明确切换文件，默认使用 ${segment.documentId}。

JSON 格式：
{
  "evidenceAdds": [{"bidDocumentId":"有效 bidDocumentId","name":"材料或响应线索名称","evidence":"原文线索或摘要","locationHint":"章节/表格/位置线索","source":"对应检查项或说明"}],
  "pendingRiskAdds": [{"bidDocumentId":"有效 bidDocumentId","type":"invalidBid","severity":"medium","title":"待确认问题","summary":"摘要","requirement":"检查依据","bidEvidence":"当前证据或缺口","riskReason":"为什么需要继续确认","suggestion":"建议","statusReason":"仍需后续片段确认的原因"}],
  "pendingRiskUpdates": [{"id":"pendingRisks 中已有 id","bidEvidence":"补充证据或缺口","riskReason":"更新原因","statusReason":"当前仍需确认的原因"}],
  "pendingRiskResolves": [{"id":"pendingRisks 中已有 id","reason":"被当前片段或累计线索排除的原因","locationHint":"位置线索"}],
  "confirmedRiskAdds": [{"bidDocumentId":"有效 bidDocumentId","type":"invalidBid","severity":"high","title":"风险标题","summary":"摘要","requirement":"检查依据","bidEvidence":"明确投标文件证据","riskReason":"风险原因","suggestion":"建议"}]
}`,
    },
```

**定稿提示词**（`:1103-1117`）——证据索引反证缺失：
```js
定稿规则：
1. 只保留能从电子投标文件原文或累计状态判断且有明确证据的风险。
2. candidateStatus 为 pending 且仍未形成完整证据闭环的问题不得输出为最终风险。
3. 如果某项"缺失/未响应"已经在 submittedEvidenceIndex 中出现章节、目录、附件标题、材料清单、表格条目、页码线索、图片占位线索或其他提交线索，不能定稿为缺失。
4. 删除签字、盖章、密封、纸质正副本、现场递交、纸质原件、开标现场行为等纸质或线下事项。
5. 同一问题合并为一条，bidDocumentId 必须来自上方有效 bidDocumentId。
6. 如果没有符合条件的风险，返回 {"findings":[]}。
```

**全局合稿提示词**（`:1137-1151`）：
```js
合稿规则：
1. 合并跨批次重复或高度相似的风险，保留证据更明确、表述更完整的一条。
2. 如果 submittedEvidenceIndex 已经出现对应材料、章节、附件标题、目录、表格、页码线索、图片占位线索或其他提交线索，不得把该材料定稿为缺失。
3. 删除 resolvedRisks 已排除或证据不足的风险。
4. 删除签字、盖章、密封、纸质正副本、现场递交、纸质原件、开标现场行为等纸质或线下事项。
5. bidDocumentId 必须来自上方有效 bidDocumentId。
6. 如果没有符合条件的风险，返回 {"findings":[]}。
```

### 3.4 结果归一化与硬过滤

**`rejectionCheckTask.cjs:238-261`**
```js
function normalizeRejectionCheckFindings(parsed, bidDocuments) {
  const bidDocumentIds = new Set((Array.isArray(bidDocuments) ? bidDocuments : []).map((document) => document.id).filter(Boolean));
  return getArrayPayload(parsed, ['findings', 'items', 'risks'])
    .filter((item) => item && typeof item === 'object' && !Array.isArray(item))
    .map((item) => {
      const bidDocumentId = getBidDocumentIdFromItem(item, bidDocumentIds);
      const title = normalizeText(item.title).slice(0, 80);
      const bidEvidence = normalizeText(item.bidEvidence || item.evidence || item.bid_evidence);
      const riskReason = normalizeText(item.riskReason || item.reason || item.risk_reason);
      return {
        id: normalizeText(item.id) || createId('rejection_finding'),
        bidDocumentId,
        type: normalizeFindingType(item.type),
        severity: normalizeSeverity(item.severity),
        title,
        summary: normalizeText(item.summary) || title,
        requirement: normalizeText(item.requirement || item.source) || '未明确引用具体检查依据，请人工复核。',
        bidEvidence,
        riskReason,
        suggestion: normalizeText(item.suggestion) || '请结合招标文件要求和投标文件原文人工复核后处理。',
      };
    })
    .filter((item) => item.bidDocumentId && item.title && item.bidEvidence && item.riskReason);
}
```
**四要素缺一即丢弃**（`bidDocumentId` + `title` + `bidEvidence` + `riskReason`）。
中文类型/级别容错（`:60-71`）：
```js
function normalizeFindingType(value) {
  const raw = String(value || '').trim();
  if (raw === 'invalidBid' || raw.includes('无效')) return 'invalidBid';
  return 'rejectionItem';
}

function normalizeSeverity(value) {
  const raw = String(value || '').trim().toLowerCase();
  if (raw === 'high' || raw.includes('高')) return 'high';
  if (raw === 'low' || raw.includes('低')) return 'low';
  return 'medium';
}
```
> 注意 `getBidDocumentIdFromItem`（`:36-44`）在只有 1 份文档时会**兜底回填**，多份时**必须命中白名单**：
```js
function getBidDocumentIdFromItem(item, bidDocumentIds) {
  const candidates = [item.bidDocumentId, item.bid_document_id, item.documentId, item.document_id, item.fileId, item.file_id, item.sourceFile, item.source_file]
    .map((value) => normalizeText(value))
    .filter(Boolean);
  for (const candidate of candidates) {
    if (bidDocumentIds.has(candidate)) return candidate;
  }
  return bidDocumentIds.size === 1 ? Array.from(bidDocumentIds)[0] : '';
}
```

### 3.5 错别字检查（额外亮点：程序级原文定位校验）

`normalizeTypoCheckFindings`（`:300-345`）不只信 AI：`findVerifiedTypoPosition`（`:263-…`）在原文中定位 `originalExcerpt` → 再在其中找 `wrongText`，找不到就**丢弃该条**；`typoExcerptRadius = 8`（`:7`）取前后 8 字做摘录回填。

提示词（`:197-209`）：
```js
      content: `【错别字检查任务 v1】
请检查投标文件中的错别字、明显别字、同音错字、形近错字和明显录入错误，并输出 JSON。

检查要求：
1. 只输出你高度确信的错别字，不输出风格建议、标点偏好、表达优化或术语争议。
2. 每条必须来自某一份投标文件原文，wrongText 必须是原文中出现的原始错字或短词，bidDocumentId 必须是输入中提供的真实 ID。
3. correctText 是建议改成的正确字词。
4. originalExcerpt 尽量摘录包含 wrongText 的原文短片段，便于程序校验；不要改写原文。
5. 如果没有明确错别字，返回 {"findings":[]}。

JSON 格式：{"findings":[{"bidDocumentId":"对应投标文件的 bidDocumentId","wrongText":"原文中的错别字或短词","correctText":"建议正确字词","originalExcerpt":"包含错别字的原文短片段","reason":"为什么判断为错别字"}]}

仅输出 JSON，不要输出 Markdown、代码块或解释。`
```

### 3.6 逻辑谬误检查

提示词（`:219-234`）：
```js
      content: `【逻辑谬误检查任务 v1】
请检查投标文件中的逻辑谬误和前后不一致问题，并输出 JSON。

检查范围：
1. 句子本身存在逻辑漏洞、因果不成立、条件互相矛盾或结论无法由前文推出。
2. 全文前后不一致，包括但不限于处理相同工作的人员名单、设备型号、工期、金额、数量、服务期限、项目名称、技术参数等应高度一致的内容前后不一致。

输出要求：
1. 只保留有明确文本依据的问题，避免泛泛而谈。
2. 问题可能涉及同一份投标文件内的多处原文，originalText 可摘录关键原文，locationHint 写明大概位置、章节、表格或上下文线索，bidDocumentId 必须是输入中提供的真实 ID。
3. title 必须简短明确，便于作为折叠列表标题。
4. 如果没有明确逻辑谬误，返回 {"findings":[]}。

JSON 格式：{"findings":[{"bidDocumentId":"对应投标文件的 bidDocumentId","title":"不超过 28 个中文字符的简短标题","originalText":"关键原文摘录，可包含同一份文件内多处摘录","locationHint":"大概位置、章节、表格或上下文线索","fallacyReason":"谬误原因或前后不一致原因","suggestion":"修改建议"}]}

仅输出 JSON，不要输出 Markdown、代码块或解释。`
```
滚动版维护 `factRegister`（关键事实登记，用于「A 处说 30 天、B 处说 45 天」的矛盾判定），状态摘要 `createFinalLogicStateSummary:963-988`；定稿与合稿提示词分别为 `buildLogicFinalBatchMessages:1219-1232`、`buildLogicGlobalMergeMessages:1251-1264`，同样用 `factRegisterIndex` / `resolvedIssues` 排除误判：
```js
定稿规则：
1. 只保留有明确投标文件证据、无法由上下文解释或修正的问题。
2. candidateStatus 为 pending 且仍未形成完整证据闭环的问题不得输出为最终问题。
3. 如果 resolvedIssues 或 factRegisterIndex 已经说明疑似矛盾不成立，必须删除。
4. 同一问题合并为一条，bidDocumentId 必须来自上方有效 bidDocumentId。
5. 如果没有明确逻辑谬误，返回 {"findings":[]}。
```

### 3.7 进度反馈

`:1649-1652` 总体进度 = `5 + (completed/enabledTasks)*90`：
```js
  function updateOverall(label, partial, persist = false) {
    const progress = Math.min(95, Math.round(5 + (completed / enabledTasks.length) * 90));
    updateCheckWorkspace(updateTask, checkpointTask, { status: 'running', progress, logs: [...logs, label] }, partial, persist);
  }
```
每个 runner 通过 `onProgress(message)` 逐段上报（例：`` `${segment.documentLabel}：正在滚动审阅投标包第 ${i}/${n} 段。` ``、`` `正在定稿废标项风险第 ${i}/${n} 批。` ``）。

编排与汇总（`:1698-1722`）：
```js
  const tasks = [];
  if (runOptions.rejectionCheck) {
    tasks.push(runOne('rejection', '废标项检查', (onProgress) => runRejectionItemCheck(aiService, { invalidBidAndRejectionItems, customCheckItems, bidDocuments: currentBidDocuments }, onProgress), 'rejectionCheckResult', rejectionInputSignature));
  }
  if (runOptions.typoCheck) {
    tasks.push(runOne('typo', '错别字检查', (onProgress) => runTypoCheck(aiService, { bidDocuments: currentBidDocuments }, onProgress), 'typoCheckResult', bidSignature));
  }
  if (runOptions.logicCheck) {
    tasks.push(runOne('logic', '逻辑谬误检查', (onProgress) => runLogicCheck(aiService, { bidDocuments: currentBidDocuments }, onProgress), 'logicCheckResult', bidSignature));
  }

  const results = await Promise.all(tasks);
  const failed = results.filter((item) => item.status === 'error');
  updateCheckWorkspace(updateTask, checkpointTask, {
    status: failed.length ? 'error' : 'success',
    progress: 100,
    logs: failed.length ? [`检查完成，${failed.length} 个任务失败。`] : ['检查完成。'],
    error: failed.length ? `${failed.length} 个检查任务失败` : undefined,
  }, {}, true);
```

## 4. 可行性报告（feasibilityReport）

**独立任务线**，与标书主线无共享编排。5 个 task：
`runFeasibilityAnalysisTask`（`feasibilityReportTasks.cjs:123`）→ 目录（`feasibilityOutlineTask.cjs`）→ 参数（`:187`）→ 正文（`:212` `generateLeafContent`）→ 自然化审校（`rewriteLeafContent`）。

### 4.1 流程

**① 资料分析（分段 + 合并）** — `feasibilityReportTasks.cjs:123-185`：
```js
async function runFeasibilityAnalysisTask({ aiService, workspaceStore, updateTask, checkpointTask }) {
  const state = workspaceStore.loadFeasibilityReport();
  const sources = String(workspaceStore.readCombinedSourceMarkdown() || '').trim();
  if (!state.projectInfo?.projectName) throw new Error('请先填写项目名称');

  let logs = [sources ? '开始分析项目资料。' : '未导入资料文件，仅根据项目参数分析。'];
  updateTask({ progress: 8, logs });
  const config = typeof aiService.getConfig === 'function' ? aiService.getConfig() : {};
  const segments = splitUserTextByContextLimit(sources, config);
  const system = buildAnalysisSystemPrompt();
  const projectBlock = formatProjectInfo(state.projectInfo);

  async function analyzeSegment(content, index, total) {
    logs = [...logs, total > 1 ? `正在分析第 ${index}/${total} 段资料。` : '正在提取资料事实。'];
    updateTask({ progress: 12 + Math.round((index - 1) / total * 50), logs });
    const sourceBlock = String(content || '').trim() || '未导入资料文件';
    return aiService.requestJson({
      messages: [
        { role: 'system', content: system },
        { role: 'user', content: `项目基础参数：\n${projectBlock}\n\n当前资料分段：${index}/${total}\n\n${sourceBlock}` },
        { role: 'user', content: buildAnalysisUserInstruction(index, total) },
      ],
      progressLabel: '可研资料分析',
      failureMessage: '资料分析结果不是有效 JSON',
      logTitle: total > 1 ? `可研资料分析-第${index}段` : '可研资料分析',
    });
  }

  let payload;
  if (segments.length <= 1) {
    payload = await analyzeSegment(segments[0] || sources, 1, 1);
  } else {
    const parts = [];
    for (let index = 0; index < segments.length; index += 1) {
      parts.push(await analyzeSegment(segments[index], index + 1, segments.length));
    }
    logs = [...logs, '正在合并分段分析结果。'];
    updateTask({ progress: 72, logs });
    payload = await aiService.requestJson({
      messages: [
        { role: 'system', content: buildAnalysisMergeSystemPrompt() },
        { role: 'user', content: `项目基础参数：\n${projectBlock}\n\n分段分析结果：\n${JSON.stringify(parts, null, 2)}` },
        { role: 'user', content: buildAnalysisMergeUserInstruction() },
      ],
      progressLabel: '可研资料分析合并',
      failureMessage: '合并分析结果不是有效 JSON',
      logTitle: '可研资料分析合并',
    });
  }

  const analysisMarkdown = analysisToMarkdown(payload);
  logs = [...logs, '资料分析完成。'];
  checkpointTask({ status: 'success', progress: 100, logs }, {
    analysisMarkdown,
    outlineData: null,
    keyParametersMarkdown: '',
    outlineTask: null,
    outlineAdjustmentTask: null,
    parametersTask: null,
    contentTask: null,
    humanWritingTask: null,
  });
}
```

**② 目录生成** — `feasibilityOutlineTask.cjs`，6 大纲模板（`feasibilityReportPrompts.cjs:1-179`）：
`government`（2023版标准，10 章）/ `enterprise` / `industrial` / `hi_tech` / `infrastructure` / `eco_environmental` / `commercial_realestate`。

大纲骨架示例（`:2-28`）：
```js
  government: {
    label: '政府投资项目通用大纲（2023版标准）',
    chapters: [
      '概述',
      '项目建设背景和必要性',
      '项目需求分析与产出方案',
      '项目选址与要素保障',
      '项目建设方案',
      '项目运营方案',
      '项目投融资与财务方案',

**④ 逐叶正文**（`:212-236`）：
```js
async function generateLeafContent({ aiService, state, leaf, knowledge, targetWords }) {
  const chapterPath = (leaf.trail || []).join(' > ');
  const messages = [
    { role: 'system', content: buildContentSystemPrompt() },
    { role: 'user', content: `项目基础参数：\n${formatProjectInfo(state.projectInfo)}` },
    { role: 'user', content: `项目资料分析：\n${state.analysisMarkdown}` },
    { role: 'user', content: `全文关键参数与编制口径：\n${state.keyParametersMarkdown}` },
    {
      role: 'user',
      content: `当前章节路径：${chapterPath}\n章节写作重点：${leaf.description || '围绕章节标题展开充分论证。'}\n参考目标字数：约 ${targetWords} 字。`,
    },
  ];
  if (knowledge) {
    messages.push({
      role: 'user',
      content: `可吸收的知识库素材如下。请改写到本项目语境，不要提及"知识库""历史文档"或资料来源：\n\n${knowledge}`,
    });
  }
  messages.push({ role: 'user', content: buildContentWritingRules() });
  const response = await aiService.chat({
    messages,
    logTitle: `可研正文-${leaf.title}`,
  });
  return stripMarkdownFence(response);
}
```

**⑤ 自然化审校**（`:383-445`）逐叶重写 + `protectFacts` 守恒。

### 4.2 提示词原文（全部 5 组）

**资料分析 system**（`feasibilityReportPrompts.cjs:236-241`）：
```js
function buildAnalysisSystemPrompt() {
  return [
    '你是严谨的中国建设项目可行性研究资料分析专家。只能基于项目参数和用户资料提取事实；不得编造金额、规模、地点、期限、政策名称或技术参数。',
    '无资料文件时只使用项目参数（含建设内容与规模）；此时资料块可能为"未导入资料文件"。',
  ].join('');
}
```
**资料分析 user**（`:243-262`）：
```js
function buildAnalysisUserInstruction(segmentIndex, totalSegments) {
  return `请把当前资料中可用于编制可行性研究报告的信息整理为 JSON。

要求：
1. project_overview：项目名称、单位、地点、性质、规模、建设内容、工期等。
2. background_and_necessity：背景、问题、规划政策关系和建设必要性。
3. demand_and_output：需求对象、现状缺口、建设规模依据和预期产出。
4. site_and_conditions：选址、用地、交通、市政、资源、审批和建设条件。
5. construction_and_technical_conditions：技术路线、工程、设备、数字化和建设管理资料。
6. operation_conditions：运营模式、组织、安全、绩效和运维资料。
7. investment_and_financing：投资、费用、资金来源、收益、成本和融资资料。
8. impact_and_risks：经济、社会、生态、能源、碳排放和风险资料。
9. missing_information：只列出本段明显缺失、矛盾或需要用户确认的关键资料；不要把当前分段未出现但可能存在于其他分段的内容武断判定为缺失。
10. 每个字段输出 Markdown 文本；没有信息时输出空字符串。只返回 JSON。

当前资料分段：${segmentIndex}/${totalSegments}

JSON 格式：
${JSON.stringify(Object.fromEntries(ANALYSIS_FIELDS.map(([key]) => [key, 'Markdown 文本'])), null, 2)}`;
}
```
> ⚠️ 第 9 条的「不要武断判定为缺失」与废标检查的「证据不足不要输出」是同一设计哲学。

**合并**（`:264-270`）：
```js
function buildAnalysisMergeSystemPrompt() {
  return '你是严谨的可行性研究资料合并专家。只能合并用户提供的分析结果，不得创造新事实。';
}

function buildAnalysisMergeUserInstruction() {
  return '请合并重复信息、保留具体事实并标明资料矛盾。missing_information 只保留综合全部分段后仍然缺失、矛盾或需要确认的关键项。只返回与输入相同字段的 JSON。';
}
```
**目录**（`:282-284`）：
```js
function buildOutlineSystemPrompt() {
  return '你是可行性研究报告总编。请基于项目实际资料，在通用大纲框架内形成完整、可执行、可编辑的三级以内报告目录。';
}
```
大纲 markdown 渲染（`:222-234`）：
```js
function buildOutlineTemplateMarkdown(templateId, targetWords) {
  const template = OUTLINE_TEMPLATES[templateId] || OUTLINE_TEMPLATES.government;
  return [
    `# 选用的通用大纲：${template.label}`,
    '',
    `目标总字数约 ${Number(targetWords) || 30000} 字。`,
    '',
    '一级目录原则上保留本大纲主框架，可根据项目明显不适用的内容合并或调整，但不得遗漏结论、风险、影响和投资相关内容。',
    '二、三级目录必须结合本项目资料具体化；下列二级标题是大纲组成部分，应作为细化起点写入目录，而不是可忽略的参考清单。',
    '',
    formatOutlineTemplateTree(templateId),
  ].join('\n');
}
```

**关键参数**（`:286-299`）：
```js
function buildParametersSystemPrompt() {
  return '你是可行性研究报告的关键参数审校专家。你必须区分已知事实与缺失信息，严禁自行编造数字、政策、设备参数、地点或资金安排。';
}

function buildParametersUserInstruction() {
  return `请生成"关键参数与编制口径"Markdown，至少包含：项目身份信息、建设目标与规模、建设地点与条件、建设期与进度、技术路线与主要设备、投资与资金来源、运营期与组织、安全环保能源口径、经济社会效益口径、风险与待确认事项。

规则：
1. 已有明确事实直接写入。
2. 未提供的关键参数统一写"【待补充】"，不要填常见值或经验值。
3. 资料存在冲突时写"【待确认】"并列出冲突内容。
4. 本阶段不自动计算 NPV、IRR、回收期等财务指标。
5. 使用二级标题和简短 bullet，直接输出 Markdown。`;
}
```

**正文**（`:301-316`）：
```js
function buildContentSystemPrompt() {
  return '你是专业的可行性研究报告编制专家。正文必须基于用户提供的项目事实和资料，论证清晰、语言正式。不得编造金额、规模、地点、期限、批复、政策名称、设备参数或财务指标。';
}

function buildContentWritingRules() {
  return `写作规则：
1. 只生成当前叶子章节正文，不重复输出章节标题。
2. 对已有资料进行分析、论证和结构化表达；可以使用 Markdown 小标题、列表和必要表格。
3. 没有依据的关键数据明确写"【待补充】"或采用不含虚构数字的定性表达。
4. 不把需求、建议或通用规范写成已经完成的事实。
5. 与全文关键参数保持一致。
6. 若当前章节涉及项目选址与建设条件、总图布置、工艺流程、环保设施或实施进度等工程技术/选址章节，请在最佳位置嵌入且仅嵌入 1 处插图指引框，格式固定为：
> 📸 **【插图指引】：图片名称**
> *说明：此处请插入...*
7. 直接输出 Markdown 正文。`;
}
```

**自然化审校**（`:318-326`）：

### 4.3 输出格式常量

`analysisToMarkdown`（`:272-280`）把 9 字段转 Markdown，空值回填：
```js
function analysisToMarkdown(payload = {}) {
  return ANALYSIS_FIELDS.map(([key, title]) => {
    const raw = String(payload[key] || '').trim();
    const body = raw || (key === 'missing_information'
      ? '- 【待补充】尚未识别到明确缺失项，请人工核对资料完整性。'
      : '【待补充】现有资料未提供足够信息。');
    return `## ${title}\n\n${body}`;
  }).join('\n\n');
}
```
9 字段定义（`:181-191`）：
```js
const ANALYSIS_FIELDS = [
  ['project_overview', '项目概况'],
  ['background_and_necessity', '建设背景与必要性'],
  ['demand_and_output', '需求分析与产出方案'],
  ['site_and_conditions', '项目选址与要素保障'],
  ['construction_and_technical_conditions', '建设内容与技术条件'],
  ['operation_conditions', '运营条件'],
  ['investment_and_financing', '投资与资金资料'],
  ['impact_and_risks', '影响效果与风险'],
  ['missing_information', '缺失资料清单'],
];
```

## 5. 目录审核

**在此仓实现，但返回结构不是 `{passed, suggestions}`。** 实际是 Agent 写文件 + JSON Schema 校验的四态结构。

**Schema 定义**（`outlineGenerationTaskV2.cjs:157-181`）：
```js
const OUTLINE_REVIEW_SCHEMA = {
  type: 'object',
  required: ['status', 'issues', 'user_feedback', 'summary'],
  additionalProperties: false,
  properties: {
    status: { type: 'string', enum: ['passed', 'simple_fix', 'user_feedback', 'user_refuse'] },
    issues: {
      type: 'array',
      items: {
        type: 'object',
        required: ['category', 'problem', 'repair', 'confirmation_required'],
        additionalProperties: false,
        properties: {
          category: {
            type: 'string',
            enum: ['leaf-count', 'score-coverage', 'duplicate-directory', 'professional-structure'],
          },
          problem: { type: 'string', minLength: 1 },
          repair: { type: 'string', minLength: 1 },
          ...
```
文件常量（`:13-14`）：
```js
const OUTLINE_REVIEW_FILE = 'outline-review.json';
const OUTLINE_REVIEW_CONTEXT_FILE = 'outline-review-context.json';
```
**核对**：`additionalProperties: false` + `required` 齐全——`passed` 是 `status` 的一个枚举值，不是独立布尔字段；`suggestions` 对应物是 `issues[]`（`problem`/`repair`/`confirmation_required`）+ `user_feedback` + `summary`。

**审核提示词原文**（`createOutlineReviewPrompt:704-733`）：
```js
  return `请对当前完整技术方案目录执行最终审核，并在用户确认后完成必要修复。

开始审核时一次性并行读取 ${OUTLINE_REVIEW_CONTEXT_FILE}、${OUTLINE_OUTPUT_FILE}、技术评分信息.md 和 ${SCORE_DIRECTORY_PLAN_FILE}，不要探索工作区或读取其他文件。${OUTLINE_REVIEW_CONTEXT_FILE} 是宿主程序计算的确定性审核结果，叶子数量、内容模式数量、最大层级、父节点数量、单子节点和评分节点机械映射均直接采用其中结果，不要重新统计、编写脚本或执行额外结构检查；你只负责评分语义覆盖、近义重复和专业合理性审核。

审核维度：${leafCountReview}
- 评分覆盖：直接以技术评分信息.md 为原始依据，逐项检查其中适合技术方案响应的评分大项是否被目录准确覆盖；结构化评分项和目录规划用于核对已确认的映射，但不能掩盖原始评分信息中的遗漏。
- 重复目录：检查全部子目录中是否存在重复、近义、含义重叠或仅换一种说法的节点；不同专业分支下确有独立含义的同名标题不应机械判重。
- 专业合理性：评估目录层级、颗粒度、逻辑顺序、标题表达、节点归属以及内容处理模式是否适合正式技术投标文件。

审核与修复流程：
1. 必须先完整审核并形成问题清单，不得边审核边修改。
2. 如果没有问题，不要修改 ${OUTLINE_OUTPUT_FILE}；写入 ${OUTLINE_REVIEW_FILE}，status=passed、issues=[]、user_feedback=""，summary 说明通过原因。
3. 如果发现问题，先为每个问题记录 category、problem、推荐 repair 和 confirmation_required，不得提前修改目录。
4. 只有以下问题可设 confirmation_required=false：标题或说明的专业化优化；不涉及评分项映射节点、目标层级和一级目录的明显重复或近义子目录合并。评分覆盖缺失、叶子数量超出合理范围、评分项目标层级调整、一级目录调整、增加或拆分目录、跨分支移动以及明显结构重排均必须设为 true。
5. 如果全部问题都不需要确认，可以直接执行文案优化或轻微去重，不得调用 ask-user；完成后设置 status=simple_fix、user_feedback=""。静默修复不得改变评分项映射、评分项目标层级和一级目录，且不得使 AI 生成叶子数量超出程序给出的合理范围。
6. 只要存在一个 confirmation_required=true 的问题，本轮所有问题都不得提前修改。集中调用一次 ask-user，question 使用多行文本完整列出问题及推荐修复方案；提供 2 至 5 个互斥选项，第一项是推荐修复方案，并提供保留当前目录的选项；另提供一个名为"调整修复方案"等明确业务名称的选项并设置 custom=true，让用户说明具体修改要求，其他选项均设置 custom=false。custom=true 的选项最多只能有一个。
7. 根据 ask-user 返回的 answer 执行最终处理：用户要求全部或部分修改时更新 ${OUTLINE_OUTPUT_FILE} 并设置 status=user_feedback；用户明确拒绝修改或要求保留现状时不得修改目录并设置 status=user_refuse。将 answer 原文完整写入 user_feedback，修改完成后不得再次询问用户。
8. ${rootRequirement}
9. 修复必须继续遵守 ${SCORE_DIRECTORY_PLAN_FILE} 中用户确认的评分项映射、目标层级和一级目录调整边界。补回遗漏映射、合并重复目录或优化层级时，不得引入未经用户批准的评分大项规划变更。
10. 技术一级目录必须保留 ${SCORE_DIRECTORY_PLAN_FILE} 中对应的 branch_id；调整一级目录顺序或编号时不得修改 branch_id。结构事实以 ${OUTLINE_REVIEW_CONTEXT_FILE} 为准；如果其中确定性检查不通过，直接依据列出的节点和缺失项形成问题并修复，不要重新统计。任何语义修复仍必须保证叶子保留合法 content_mode、父节点不包含 content_mode 或 content_mode_note、父节点至少有两个 children 且目录最多六级。
11. 最终将完整问题清单和处理结果写入 ${OUTLINE_REVIEW_FILE}。无问题时完整格式为 {"status":"passed","issues":[],"user_feedback":"","summary":"审核通过原因"}；有问题时完整格式为 {"status":"user_feedback","issues":[{"category":"score-coverage","problem":"问题说明","repair":"修复方案","confirmation_required":true}],"user_feedback":"用户回答原文","summary":"处理结果"}。category 只能是 leaf-count、score-coverage、duplicate-directory、professional-structure；status 按本流程选择 passed、simple_fix、user_feedback 或 user_refuse。
12. 程序已为 ${OUTLINE_OUTPUT_FILE} 和 ${OUTLINE_REVIEW_FILE} 预置 Schema。分别调用 json-validation 校验，只传 file_path；校验失败后必须先修改对应文件，再重新校验。`;
```
叶子数量容差（`:705-707`）：
```js
  const leafCountReview = targetLeafCount === null
    ? ''
    : `\n- "AI生成"叶子数量：程序计算目标为 ${targetLeafCount} 个，当前为 ${actualLeafCount} 个，可接受范围为 ${Math.max(1, targetLeafCount - 2)} 至 ${targetLeafCount + 2} 个。只统计 content_mode=ai-generate 的最终叶子节点；修复后仍须保持在此范围内。`;
```
无技术评分项模式的精简版在 `createNoTechnicalScoreReviewPrompt:680-702`（`issues.category` 收敛为 3 类，去掉 `score-coverage`）：
```js
审核维度：${leafCountReview}
- 重复目录：检查子目录中是否存在重复、近义或含义重叠的节点。
${originalOnly
    ? `- 来源与完整性：读取原方案.md，核对目录是否忠实覆盖原方案中的章节。${ORIGINAL_ONLY_DIRECTORY_RULE}不得以缺少通用技术主题为由新增目录。`
    : '- 专业合理性：检查目录是否覆盖项目实施所需的通用技术主题，层级、颗粒度、逻辑顺序、标题和内容处理模式是否适合正式投标文件。\n- 事实边界：专业经验只能补充通用目录结构，不得编造具体项目事实、参数、业绩或承诺。'}
```
结果落地（`:1110-1134`）：
```js
    if (meta.workflow_stage === 'outline_review') {
      const reviewedOutline = readJson(candidate.output_content, OUTLINE_OUTPUT_FILE);
      const normalizedReviewedOutline = buildFinalOutline(reviewedOutline);
      outlineReview = readJson(await meta.readFile(OUTLINE_REVIEW_FILE), OUTLINE_REVIEW_FILE);

# 二、导出文档

## 6. 完整链路：Markdown → HTML → DOCX

```
technicalPlanStore.outlineData.outline[].content (Markdown)
  ↓ normalizeMarkdownTablesForDocx / normalizeMarkdownListMarkersForDocx
  ↓ renderMarkdownHtml(markdown, { allowRawHtml: true, enableGfm: true })
  ↓ htmlToDocxBlocks: cheerio.load(source, null, false) → htmlNodesToDocxBlocks
  ↓   ├─ <pre><code class="language-mermaid"> → mermaidCodeToDocxBlocks → PNG → ImageRun
  │   ├─ <h1>~<h6>      → Paragraph(heading)
  │   ├─ <table>        → Table
  │   ├─ <img>          → ImageRun（读本地/远程 buffer）
  │   └─ <p>/<div>/...  → Paragraph(TextRun[])
  ↓ docx.Document → Packer.toBuffer(doc) → Buffer
  ↓ dialog.showSaveDialog → fs.writeFileSync
```

**入口**（`exportService.cjs:1785-1789`）：
```js
async function markdownToDocxBlocks(content, context = {}) {
  const markdown = normalizeMarkdownTablesForDocx(normalizeMarkdownListMarkersForDocx(content));
  const html = await renderMarkdownHtml(markdown, { allowRawHtml: true, enableGfm: true });
  return htmlToDocxBlocks(html, context);
}
```
**HTML 渲染器**（`client/electron/utils/renderMarkdownHtml.cjs`）— markdown-it + cjk-friendly + task-lists：
```js
async function createMarkdownRenderer(options) {
  const { MarkdownIt, cjkFriendly, taskLists } = await loadMarkdownModules();
  const renderer = new MarkdownIt(options.enableGfm ? 'default' : 'commonmark', {
    html: options.allowRawHtml,
    linkify: false,
    typographer: false,
    breaks: false,
  });

  renderer.use(cjkFriendly);
  if (options.enableGfm) {
    renderer.use(taskLists, { enabled: true, label: true, labelAfter: true });
  }

  return renderer;
}
```
**HTML→块**（`exportService.cjs:1771-1783`）：
```js
async function htmlToDocxBlocks(html, context = {}, options = {}) {
  const source = String(html || '').trim();
  if (!source) {
    return [];
  }

  const $ = cheerio.load(source, null, false);
  const blocks = await htmlNodesToDocxBlocks($, $.root().contents().toArray(), context, options);
  if (!blocks.length) {
    addWarning(context, '部分 HTML 内容未能导出，请核对 Word 内容。');
  }
  return blocks;
}
```
> `cheerio.load(source, null, false)` 第三参 `false` = **fragment 模式**，不生成 `<html>/<body>` 包装——因为只要 body 内容块。

**分派核心**（`:1716-1731`）：
```js
  if (tag === 'pre') {
    const codeNode = $(node).children('code').first();
    if (codeNode.length && isMermaidCodeElement($, codeNode[0])) {
      return mermaidCodeToDocxBlocks(codeNode.text(), context);
    }
    return [paragraph([new TextRun({ text: cleanText($(node).text()), font: 'Consolas', size: 21, color: '243048' })], {
      shading: { type: ShadingType.CLEAR, fill: 'F6F9FF' },
      indent: { left: 260, right: 260 },
    })];
  }
  if (tag === 'br') {
    return [paragraph([lineBreakRun()])];
  }
  if (tag === 'hr') {
    return [paragraph([textRun('────────────────────────', { color: 'DCDFF6' })], { alignment: AlignmentType.CENTER })];
  }
```

**版式常量**（`:41-48`）：
```js
const MAX_IMAGE_WIDTH = 520;
const MAX_IMAGE_HEIGHT_PERCENT = 90;
const NUMBERING_REFERENCE_PREFIX = 'technical-plan-numbering';
const HEADING_NUMBERING_REFERENCE = 'technical-plan-heading-numbering';
const DOCX_TABLE_WIDTH_TWIPS = 9000;
const CHAPTER_LEAF_TITLE_WIDTH_TWIPS = 1800;
const CHAPTER_LEAF_CONTENT_WIDTH_TWIPS = DOCX_TABLE_WIDTH_TWIPS - CHAPTER_LEAF_TITLE_WIDTH_TWIPS;
const DEFAULT_HEADING_BORDER_CELL_COLORS = ['#e0ecff', '#e9f1ff', '#f2f7ff', '#f8fbff', '#ffffff', '#ffffff'];
```

**Mermaid 计数**（`:166-185`）：
```js
function countMermaidBlocks(content) {
  return (String(content || '').match(/```mermaid[\s\S]*?```/gi) || []).length;
}
```
进度分母（`:128-131`）：`leafCount + mermaidCount`。

**Document 组装**（`:2353-2372`）：
```js
  const doc = new Document({
    ...(numbering ? { numbering } : {}),
    styles: {
      default: {
        document: {
          run: { font: bodyFont, size: bodySizeHalfPt },

### 6.1 openxmlhelper 的作用（**与 DOCX 导出无关**）

**关键澄清**：`openxmlhelper/` **不参与技术方案 DOCX 导出**。它是独立的 **.NET 10 控制台程序**，专门做 **Word 模板（招标投标文件模板）的内容控件填充**。

`openxmlhelper/开发说明.md:12-14`：
> - 仓库根目录 `openxmlhelper/` 是独立 .NET 10 控制台，给 Electron Main 做 Word / Open XML。
> - Renderer 不直连助手，不经过 preload。
> - 主程序通过 `openXmlHelperService.runJob` 调用；Agent 通过 Pi 专用工具 `openxml` 调用同一套动作。不要把助手 exe 放进 PATH。

**进程模型**（`开发说明.md:16-20`）：长期驻留、单任务串行、懒启动、参数仅 `--workspace <userData/workspace>`。
**信号协议**（`:24-42`）：stdin/stdout 各一行 UTF-8 JSON 无 BOM，换行只用 `\n`（Windows 也如此），不要把文档正文塞进管道：
```json
{"v":1,"type":"run","job":"20260819153000-ab12"}
```
```json
{"v":1,"type":"done","job":"20260819153000-ab12","ok":true}
```
约束：`job` 只允许字母、数字、`.`、`_`、`-`，防止路径穿越；失败也只回 `ok:false`，错因写在任务目录的 `result.json`；信号无法解析时写 stderr，进程不退；助手忙时对后到的任务写失败结果并回 `done`；不加 shutdown 信号，退出靠 Main 杀进程。

**Job ID 防路径穿越**（`openXmlHelperService.cjs:17`）：`const JOB_ID_PATTERN = /^[A-Za-z0-9._-]+$/;`，`runJob` 写入前二次校验（`:281-283`）。
Job ID 生成（`:19-23`）：
```js
/** 生成任务编号：时间戳加短随机串。 */
function createJobId() {
  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, '').slice(0, 14);
  return `${stamp}-${crypto.randomBytes(2).toString('hex')}`;
}
```
**数据不走管道**（`开发说明.md:44-55`）：
> 双方自己读写文件，不靠管道传内容。
> `workspace/openxml-jobs/<job>/`
> | 文件 | 谁写 | 含义 |
> |---|---|---|
> | `request.json` | Main | 至少包含 `action` |
> | `result.json` | 助手 | 至少包含 `ok`；失败时写 `error` |
> 约定：先写齐输入再发 `run`；先写完 `result.json` 再发 `done`。

**5 个 action**（`开发说明.md:57-106`）：

| action | 作用 |
|---|---|
| `ping` | 存活探测（`PING_TIMEOUT_MS = 15000`） |
| `list-blocks` | 列出 Word 每块的序号/类型/是否标题/原文预览 → `blocks.json` |
| `extract-chapters` | 按 `sourceTitle` 或 `startBlock/endBlock` 从**原招标文件**抽章 → 模板 docx |
| `scan-template-fields` | 扫模板待填候选 → `template-field-candidates.json`（稳定 `candidate_id`） |
| `apply-template-fields` | 写 `w:sdt` 内容控件 |

`apply-template-fields` 细节（`开发说明.md:106`）：
> `apply-template-fields` 要求每个候选恰好归入 `fields` 或 `ignored_candidate_ids`。助手重新扫描源模版，按候选位置写入 `w:sdt`：业务 ID 保存在 `w:tag=yibiao:field:<id>`，字段名保存在 `w:alias`；AI 与人工字段都使用淡红底 `FCE8E6` 和黑字 `000000`。最终同时生成 `bid-template.docx` 和精简字段 JSON，相同 `name` 允许对应多个不同 ID。

**并发与取消**（`openXmlHelperService.cjs:239-269` `enqueue`）：串行队列 + AbortSignal；取消/超时**杀进程**（`:305-314`）：
```js
        const stopAndReject = (error, event) => {
          if (!pending.has(jobId)) return;
          pending.delete(jobId);
          cleanup();
          writeLog(event, { job: jobId, action: jobAction });
          void terminateHelper().then(
            () => reject(error),
            () => reject(error),
          );
        };
```
`runJob` 主干（`:272-332`）：写 `request.json` → `prepare(jobDir)` → `ensureStarted()` → 发 `run` → 等 `done`（带 timeout）。

源码位置：`openxmlhelper/src/OpenXmlHelper/Jobs/{JobRunner,WordWorkspace,ExtractChaptersAction,TemplateFieldScanner,TemplateFieldSdtWriter,ScanTemplateFieldsAction,ApplyTemplateFieldsAction,ListBlocksAction,PingAction,JobFolder}.cs`，宿主 `Host/{StdioLoop,SignalProtocol}.cs`，入口 `Program.cs`。


## 7. Mermaid 执行链路（三处）

### 7.1 预览渲染（Renderer）

`client/src/shared/ui/MarkdownRenderer.tsx:211-216`：
```tsx
      if (tag === 'pre' && renderMermaid) {
        const code = element.querySelector('code');
        if (code && /\blanguage-mermaid\b/i.test(code.getAttribute('class') || '')) {
          return <MermaidPreview key={key} code={(code.textContent || '').replace(/\n$/, '')} />;
        }
      }
```
开关：`MarkdownRenderer.tsx:15`（prop 声明）、`:128`（默认 `false`）、`:282`（useMemo 依赖）；消费方 `client/src/pages/ContentEditPage.tsx:281`。
Mermaid 检测双写（兼容 mermaid.ink 远端图）：`ContentEditPage` / `TechnicalPlanHome.tsx:134-136` / `ExportFormatPage.tsx:121-123`：
```tsx
  const mermaidBlocks = (String(content || '').match(/```mermaid[\s\S]*?```/gi) || []).length;
  const mermaidInkImages = (String(content || '').match(/https:\/\/mermaid\.ink\/img\//gi) || []).length;
  return mermaidBlocks + mermaidInkImages;
```
导出前提示（`ExportFormatPage.tsx:546-547`）：
```tsx
    message: mermaidCount
      ? `检测到 ${mermaidCount} 张 Mermaid 图，导出时会转换为 Word 图片，可能需要稍等。`
```

### 7.2 存储代码块（写回正文）

`contentIllustrationGeneration.cjs:450-462`：
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
**Mermaid 以代码块落盘**（非图片），用哨兵注释包裹以便幂等清除。配套 `stripGeneratedIllustrations`（`:446-448`）+ `GENERATED_ILLUSTRATION_PATTERN`（`:17`）：
```js
const GENERATED_ILLUSTRATION_PATTERN = /<!-- yibiao-illustration:start\b[^>]*-->[\s\S]*?<!-- yibiao-illustration:end -->/gi;
```
```js
function stripGeneratedIllustrations(content) {
  return String(content || '').replace(GENERATED_ILLUSTRATION_PATTERN, '\n').replace(/\n{3,}/g, '\n\n').trim();
}
```
插入位置（`applyGeneratedIllustrationsToDocument:488-…`）：
```js
    const targetId = planItem.kind === 'html' && planItem.placement === 'before'
      ? planItem.section_ids[0]
      : planItem.section_ids[planItem.section_ids.length - 1];
    if (nextSections[targetId]?.status !== 'success') continue;
    const current = String(nextSections[targetId]?.content || '').trim();
    const content = planItem.placement === 'before' ? `${block}\n\n${current}`.trim() : `${current}\n\n${block}`.trim();
    nextSections[targetId] = { ...nextSections[targetId], content, updated_at: new Date().toISOString() };
```

### 7.3 导出转图片（Main）

`exportService.cjs:1646-1649` 识别：
```js
function isMermaidCodeElement($, codeNode) {
  const className = String($(codeNode).attr('class') || '').toLowerCase();
  return /\blanguage-mermaid\b/.test(className) || /\bmermaid\b/.test(className);
}
```
完整转图+缓存（`:1588-1644`）：
```js
async function mermaidCodeToDocxBlocks(code, context) {
  const value = String(code || '').trim();
  if (!value) return [];

  const nextIndex = (context.convertedMermaidCount || 0) + 1;
  const total = context.stats?.mermaidCount || nextIndex;
  let cacheEntry = null;

  try {
    // 导出阶段不拦截语法：正文已有代码块则直接尝试本地渲染。
    cacheEntry = getMermaidCacheEntry(app, value);
    writeExportLog(context, 'export.mermaid.started', {
      mermaid_index: nextIndex, total, cache_hash: cacheEntry.hash,
      cache_hit: cacheEntry.exists, code_metrics: textMetrics(value),

**本地渲染 + 缓存**（`exportService.cjs:1177-1237`）：
```js
async function resolveMermaidImageForExport(code, context = {}, options = {}) {
  const cacheEntry = options.cacheEntry || getMermaidCacheEntry(app, code);
  if (cacheEntry.exists) {
    return { source: cacheEntry.assetUrl, cacheHit: true, cacheHash: cacheEntry.hash };
  }

  const retryAttempts = Math.max(0, Number(options.loadRetry?.retryAttempts ?? REMOTE_IMAGE_RETRY_ATTEMPTS) || 0);
  const retryDelayMs = Math.max(0, Number(options.loadRetry?.retryDelayMs ?? REMOTE_IMAGE_RETRY_DELAY_MS) || 0);
  let attempt = 0;
  let lastError = null;
  let loaded = null;

  while (attempt <= retryAttempts) {
    try {
      const rendered = await getLocalImageRenderService().renderMermaidToPng(cacheEntry.code);
      if (!rendered?.buffer?.length) {
        throw new Error('Mermaid 本地转换未生成有效图片');
      }
      loaded = { buffer: rendered.buffer, type: 'png', width: rendered.width, height: rendered.height };
      lastError = null;
      break;
    } catch (error) {
      lastError = error;
      attempt += 1;
      if (attempt > retryAttempts) break;
      if (typeof options.loadRetry?.onRetry === 'function') {
        options.loadRetry.onRetry(attempt, error);
      }
      if (retryDelayMs > 0) await delay(retryDelayMs);
    }
  }

  if (!loaded?.buffer?.length) {
    throw lastError || new Error('Mermaid 本地转换失败');
  }

  try {
    saveMermaidCacheImage(app, cacheEntry.hash, loaded.buffer);
  } catch (error) {
    writeExportLog(context, 'export.mermaid.cache_write_failed', {
      cache_hash: cacheEntry.hash, error: compactLogError(error),
    });

**本地渲染实现**（`localImageRenderService.cjs:686-735`）：
```js
  async function renderMermaidToPng(code, options = {}) {
    return runMermaid(async () => {
      throwIfPaused(options, 'Mermaid 转图已暂停');
      const mermaidScriptPath = resolveMermaidBrowserScript();
      const mermaidScriptUrl = pathToFileURL(mermaidScriptPath).href;
      const html = buildMermaidDocument(code, mermaidScriptUrl);
      const win = createRenderWindow(WORD_FRIENDLY_RENDER_WIDTH, 480);
      try {
        await withTimeout(
          loadHtmlDocument(win, html, MERMAID_RENDER_TIMEOUT_MS, options),
          MERMAID_RENDER_TIMEOUT_MS,
          'Mermaid 页面加载超时',
        );
        // 先给足够视口，让 SVG 按固有尺寸排版后再量内容包围盒。
        await setDeviceMetrics(win.webContents, WORD_FRIENDLY_RENDER_WIDTH, 1200);
        const ready = await withTimeout((async () => {
          const started = Date.now();
          while (Date.now() - started < MERMAID_RENDER_TIMEOUT_MS) {
            throwIfPaused(options, 'Mermaid 转图已暂停');
            const state = await win.webContents.executeJavaScript(`({
              ready: Boolean(window.__yibiaoMermaidReady),
              error: String(window.__yibiaoMermaidError || ''),
            })`, true);
            if (state.ready) return state;
            await delay(PAUSE_POLL_MS);
          }
          throw new Error('Mermaid 渲染超时');
        })(), MERMAID_RENDER_TIMEOUT_MS, 'Mermaid 渲染超时');
        if (ready.error) throw new Error(ready.error);
        const metrics = await waitForLayoutReady(win.webContents, MERMAID_RENDER_TIMEOUT_MS, 1, {
          ...options, contentOnly: true,
        });
        // 按内容包围盒截图，不强制铺满 680；过宽已在页面内等比缩小。
        const rawWidth = Math.ceil(metrics.width || 0);
        const rawHeight = Math.ceil(metrics.height || 0);
        if (rawWidth < 24 || rawHeight < 24) {
          throw new Error(`Mermaid 内容尺寸异常（${rawWidth}x${rawHeight}），可能未正确渲染`);
        }
        const width = Math.min(WORD_FRIENDLY_RENDER_WIDTH, Math.max(1, rawWidth));
        const height = Math.max(1, rawHeight);
        return await captureFullContent(win.webContents, width, height, {
          ...options, captureScale: MERMAID_CAPTURE_SCALE,
        });
      } finally {
        destroyWindow(win);
      }
    });
  }
```
参数常量（`:11-26`）：
```js
const WORD_FRIENDLY_RENDER_WIDTH = 680;
const MERMAID_CAPTURE_SCALE = 3;
const HTML_DESIGN_WIDTH = 1240;
const HTML_CAPTURE_SCALE = 2;
const HTML_MIN_TEXT_FONT_SIZE = 24;
const HTML_MAX_DESIGN_HEIGHT = 1800;
const MERMAID_RENDER_TIMEOUT_MS = 30000;
const HTML_RENDER_TIMEOUT_MS = 120000;
const MAX_CAPTURE_SEGMENT_HEIGHT = 8192;
const LAYOUT_SETTLE_MS = 120;
const PAUSE_POLL_MS = 100;
```
**尺寸解析的三级兜底**（`:530-554`，有重要注释）：
```js
        if (svgEl) {
          // 解析 mermaid 给出的固有尺寸；禁止直接删除 width/height，否则会塌成白图小黑点。
          const parseSize = (value) => {
            const n = parseFloat(String(value || '').replace('px', '').trim());
            return Number.isFinite(n) && n > 0 ? n : 0;
          };
          let w = parseSize(svgEl.getAttribute('width'));

## 8. Mermaid 校验与修复（含修复提示词逐字原文）

### 8.1 语法策略（唯一事实源）

`client/electron/utils/mermaidPolicy.cjs:1-44`（全文）：
```js
const MERMAID_DIAGRAM_TYPES = new Set(['process', 'hierarchy', 'responsibility']);
const MERMAID_DIAGRAM_TYPE_LABELS = {
  process: '流程图',
  hierarchy: '层级图',
  responsibility: '职责关系图',
};
const SUPPORTED_MERMAID_SYNTAX_PATTERN = /^flowchart\s+(?:TD|TB|LR|RL|BT)\b/i;

// 归一化 Mermaid 业务图表类型。
function normalizeMermaidDiagramType(value) {
  const type = String(value || '').trim();
  return MERMAID_DIAGRAM_TYPES.has(type) ? type : '';
}

// 返回 Mermaid 业务图表类型的中文名称。
function getMermaidDiagramTypeLabel(value) {
  const type = normalizeMermaidDiagramType(value);
  return type ? MERMAID_DIAGRAM_TYPE_LABELS[type] : '';
}

// 确保业务图表类型属于当前支持范围。
function assertSupportedMermaidDiagramType(value) {
  const type = normalizeMermaidDiagramType(value);
  if (!type) {
    throw new Error('Mermaid 图表类型无效，仅支持流程图、层级图和职责关系图');
  }
  return type;
}

// 确保 Mermaid 代码使用受支持的 flowchart 语法。
function assertSupportedMermaidSyntax(code) {
  const normalized = String(code || '').trim();
  if (!SUPPORTED_MERMAID_SYNTAX_PATTERN.test(normalized)) {
    throw new Error('仅支持流程图、层级图和职责关系图，且必须使用 flowchart TD/TB/LR/RL/BT 语法');
  }
  return normalized;
}

module.exports = {
  assertSupportedMermaidDiagramType,
  assertSupportedMermaidSyntax,
  getMermaidDiagramTypeLabel,
  normalizeMermaidDiagramType,
};
```
**只支持 flowchart 5 个方向**，不允许 `graph` 别名或其他语法族。

### 8.2 校验分两层：静态正则 + 真实渲染

`contentIllustrationGeneration.cjs:153-171`：
```js
function assertMermaidPreviewCompatible(code) {
  const normalized = normalizeMermaidCode(code);
  if (!normalized) throw new Error('Mermaid 代码为空');
  assertSupportedMermaidSyntax(normalized);
  if (/[;；]/.test(normalized)) throw new Error('Mermaid 代码不能使用分号');
  if (/\s&\s/.test(normalized) && /-->|---|==>/.test(normalized)) throw new Error('Mermaid 代码不能使用多节点 & 连接简写');
  if (/\[[^\]\n"']*[\u3400-\u9fff][^\]\n"']*\]/u.test(normalized)) throw new Error('Mermaid 中文节点标签必须使用双引号');
  if (/^\s*[\u3400-\u9fff][\w\u3400-\u9fff-]*\s*(?:-->|---|==>)/mu.test(normalized)) throw new Error('Mermaid 节点 ID 不能直接使用中文');
}

// 通过本地渲染校验 Mermaid 是否可出图。
async function validateMermaidRender(code) {
  const normalized = normalizeMermaidCode(code);
  assertMermaidPreviewCompatible(normalized);
  const rendered = await getLocalImageRenderService().renderMermaidToPng(normalized);
  if (!rendered?.buffer?.length) {
    throw new Error('Mermaid 本地渲染失败：未生成有效图片');
  }
}
```
> **正则只是廉价预筛，最终判据是「真渲染出 PNG」**。5 条正则各自对应模型最常见的 5 类语法错误。

代码围栏剥离（`:28-30`）：
```js
function normalizeMermaidCode(value) {
  return String(value || '').replace(/^```mermaid\s*/i, '').replace(/```$/i, '').trim();
}
```

### 8.3 生成提示词原文

`contentIllustrationGeneration.cjs:114-138`：
```js
function buildMermaidGenerationMessages(execution) {
  const type = assertSupportedMermaidDiagramType(execution.planItem.image_type);
  const typeLabel = getMermaidDiagramTypeLabel(type);
  const title = getPlannedTitle(execution);

### 8.4 修复提示词逐字原文 ⭐

`contentIllustrationGeneration.cjs:173-193`：
```js
function buildMermaidRepairMessages(execution, mermaidPlan, errorMessage, attempt) {
  const typeLabel = getMermaidDiagramTypeLabel(execution.planItem.image_type);
  const title = getPlannedTitle(execution);
  return [
    {
      role: 'system',
      content: `你是 Mermaid 图代码修复助手。请根据渲染错误和最终正文修复现有 Mermaid 代码。

要求：
1. 只返回 JSON，不要输出解释、总结或 Markdown。
2. 保持"${typeLabel}"业务类型，忠实于参考正文。
3. 必须使用 flowchart TD/TB/LR/RL/BT 语法。
4. 中文节点标签必须使用双引号，不使用 & 简写和分号。
5. code 不包含 Markdown 代码围栏。`,
    },
    {
      role: 'user',
      content: `参考正文：\n${execution.reference}\n\n最终图题：${title}\n修复轮次：${attempt}/${MERMAID_REPAIR_ATTEMPTS}\n渲染错误：${errorMessage}\n\n待修复代码：\n${mermaidPlan.code}\n\n请返回：\n{ "code": "修复后的 Mermaid 代码" }`,
    },
  ];
}
```
修复结果校验（`:195-203`）：
```js
function normalizeMermaidRepairResult(value) {
  const source = value?.result && typeof value.result === 'object' ? value.result : value || {};
  return { code: normalizeMermaidCode(source.code || source.fixed_code || source.mermaid_code || '') };
}

function validateMermaidRepairResult(result) {
  if (!result?.code || /```/.test(result.code)) throw new Error('Mermaid 修复结果缺少有效 code');
  assertSupportedMermaidSyntax(result.code);
}
```
**修复闭环**（`prepareRenderableMermaid:205-237`）：
```js
async function prepareRenderableMermaid({ aiService, execution, mermaidPlan, isPauseLikeError }) {
  const title = getPlannedTitle(execution);
  let currentPlan = { code: normalizeMermaidCode(mermaidPlan.code) };
  let lastError = null;
  try {
    assertSupportedMermaidDiagramType(execution.planItem.image_type);
    await validateMermaidRender(currentPlan.code);
    return { code: currentPlan.code, attempts: 0 };
  } catch (error) {
    lastError = error;
  }

  for (let attempt = 1; attempt <= MERMAID_REPAIR_ATTEMPTS; attempt += 1) {
    try {
      const repaired = await aiService.collectJsonResponse({
        messages: buildMermaidRepairMessages(execution, currentPlan, compactError(lastError?.message || lastError), attempt),
        logTitle: `Mermaid配图修复-${execution.planItem.item_id}-${title}`,
        progressLabel: 'Mermaid 配图修复',
        failureMessage: '模型返回的 Mermaid 修复结果格式无效',
        normalizer: normalizeMermaidRepairResult,
        validator: validateMermaidRepairResult,
        max_retries: 1,
      });
      currentPlan = { ...currentPlan, code: repaired.code };
      await validateMermaidRender(currentPlan.code);
      return { code: currentPlan.code, attempts: attempt };
    } catch (error) {
      if (isPauseLikeError?.(error)) throw error;
      lastError = error;

## 9. AI 生图

### 9.1 image.prompt 使用与风格约束（逐字）

`contentIllustrationGeneration.cjs:75-86`：
```js
function buildAiImagePrompt(execution) {
  const styleLabel = execution.planItem.image_type === 'realistic_photo' ? '专业实景图片' : '专业工程图示';
  const title = getPlannedTitle(execution);
  return `阅读并理解以下技术方案正文，生成一张${styleLabel}。
最终图题：${title}
必须围绕最终图题限定的对象、场景和关系重点组织画面，不要生成泛化的章节概览；图题用于限定画面主题，不要求把完整图题作为文字绘制在图片中。
图片需要准确表达正文中的设备、环境、部署关系或实施场景，不要编造正文中没有的关键对象。
不要有太多文字，专业、克制，适合投标技术方案。
参考内容如下：

${execution.reference}`;
}
```
类型语义（`contentIllustrationPlanning.cjs:9-12`）：
```js
const AI_IMAGE_TYPE_DESCRIPTIONS = {
  engineering_diagram: '专业工程图示：用于展示设备、系统组件、部署位置、连接关系或工程实施场景，强调结构与关系；不用于步骤流转、组织层级或职责分工。',
  realistic_photo: '专业实景图片：用于表现设备、机房、监控中心、施工、巡检或维护现场等可真实拍摄的对象和环境；不用于抽象系统架构、流程或组织关系。',
};
```
> **风格由 `image_type` 决定**（两种：`realistic_photo` / 其余），传给 `aiService.generateImage` 的 `style` 字段。

图题强校验（`:69-73`）：
```js
function getPlannedTitle(execution) {
  const title = singleLine(execution.planItem.title);
  if (!title) throw new Error(`图片计划缺少 title：${execution.planItem.item_id || 'unknown'}`);
  return title;
}
```

### 9.2 生图模型调用

`contentIllustrationGeneration.cjs:240-250`：
```js
// 使用生图模型基于最终正文生成 AI 图片。
async function generateAiIllustration(aiService, execution) {
  const title = getPlannedTitle(execution);
  const generated = await aiService.generateImage({
    title,
    logTitle: `AI生图-${execution.planItem.item_id}-${title}`,
    prompt: buildAiImagePrompt(execution),
    style: execution.planItem.image_type,
  });
  if (!generated?.asset_url) throw new Error('生图模型未返回本地图片地址');
  return { asset_url: generated.asset_url, attempts: 1 };
}
```
> ⚠️ **AI 生图无重试**（`attempts: 1` 硬编码）——与 Mermaid 的 3 轮修复形成对比。

### 9.3 HTML 配图（额外一条链）

阈值 `HTML_AGENT_THRESHOLD_CHARS = 50000`（`:14`）：超过 5 万字符走 Agent 模式，否则直连 `aiService.chat`。
文本模式提示词（`:88-96`）：
```js
function buildHtmlImagePrompt(execution) {
  const title = getPlannedTitle(execution);
  return `阅读并理解以下内容，用html绘制一张${execution.planItem.image_type}。
最终图题：${title}
必须围绕最终图题限定的对象、范围和关系重点设计图形，不要生成泛化的章节概览。
不要有太多文字描述，专业商务风格。这是一个类图片的html，所以注意仔细检查显示效果、文字换行、拥挤等问题。正文和节点文字不得小于24px，优先控制在12个主要信息节点以内，不得通过缩小字号强塞复杂内容。文字不得旋转、倒置、镜像或缩放变形，不得相互重叠、被前景元素遮挡或被容器裁切。不要使用固定或粘性文字布局，文字容器应随内容增长。宽度固定${HTML_DESIGN_WIDTH}px，高度自适应且原则上不超过${HTML_MAX_DESIGN_HEIGHT}px，不依赖在线字体或外部资源。参考内容如下：

${execution.reference}`;
}
```
Agent 模式提示词（`:98-112`）：
```js
function buildHtmlAgentPrompt(execution) {
  const title = getPlannedTitle(execution);
  return `请读取当前工作目录中的 reference.md，阅读并理解全部内容，用 HTML 绘制一张${execution.planItem.image_type}。

最终图题：${title}

要求：
1. 必须围绕最终图题限定的对象、范围和关系重点设计图形，不要生成泛化的章节概览。
2. 不要有太多文字描述，使用专业商务风格。
3. 这是一个类图片的 HTML，必须仔细检查显示效果、文字换行和内容拥挤问题；正文和节点文字不得小于 24px，优先控制在 12 个主要信息节点以内，不得通过缩小字号强塞复杂内容；文字不得旋转、倒置、镜像或缩放变形，不得相互重叠、被前景元素遮挡或被容器裁切。
4. 不要使用固定或粘性文字布局，文字容器应随内容增长；不依赖在线字体或外部资源。
5. 页面宽度固定为 ${HTML_DESIGN_WIDTH}px，高度自适应且原则上不超过 ${HTML_MAX_DESIGN_HEIGHT}px。
6. 生成完整 HTML 文档，包含 html、head、body，不依赖本地文件。
7. 只创建 illustration.html，不要修改 reference.md，不要创建其他结果文件。`;
}
```
布局修复（`HTML_LAYOUT_REPAIR_ATTEMPTS = 2`，`:16`）：
```js
function buildHtmlLayoutRepairPrompt(execution, html, issues, attempt) {
  return `请修复以下用于投标文件的 HTML 图片布局。\n最终图题：${getPlannedTitle(execution)}\n修复轮次：${attempt}/${HTML_LAYOUT_REPAIR_ATTEMPTS}\n渲染诊断：${issues.join('；')}\n\n要求：保持图题和正文事实不变；宽度固定 ${HTML_DESIGN_WIDTH}px，高度原则上不超过 ${HTML_MAX_DESIGN_HEIGHT}px；正文和节点文字不得小于 24px，优先控制在 12 个主要信息节点以内，不得通过缩小字号强塞复杂内容；禁止横向溢出、文字拥挤、重叠、遮挡和截断；文字不得旋转、倒置、镜像或缩放变形；不要使用固定或粘性文字布局，文字容器应随内容增长；保留专业商务风格；输出完整 HTML 文档且不依赖网络、本地文件、在线字体或外部资源。\n\n当前 HTML：\n${String(html || '').slice(0, 60000)}`;
}
```
程序化质检（`getHtmlLayoutIssues:299-309`）：
```js
function getHtmlLayoutIssues(screenshot) {
  const width = Number(screenshot?.width) || 0;
  const height = Number(screenshot?.height) || 0;
  const issues = Array.isArray(screenshot?.layout_issues)
    ? screenshot.layout_issues.map((issue) => String(issue || '').trim()).filter(Boolean)
    : [];
  if (width > HTML_DESIGN_WIDTH + 4) issues.push(`出现横向溢出：实际宽度 ${width}px，设计宽度 ${HTML_DESIGN_WIDTH}px`);

## 10. 配图类型最终过滤（AI 提名 + 程序拍板）

**实现与题面描述不同**：类型是 `html / ai / mermaid` 三种（无 `none`），由 **Agent 提名 → 程序 `validateCandidate` 逐条硬校验**。

### 10.1 类型常量

`contentIllustrationPlanning.cjs:3-17`：
```js
const ILLUSTRATION_PLAN_VERSION = 3;
const ROOT_PARENT_ID = '__root__';
const ILLUSTRATION_KINDS = ['html', 'ai', 'mermaid'];
const ILLUSTRATION_KIND_ORDER = new Map(ILLUSTRATION_KINDS.map((kind, index) => [kind, index]));
const AI_IMAGE_TYPES = new Set(['engineering_diagram', 'realistic_photo']);
const MERMAID_IMAGE_TYPES = new Set(['process', 'hierarchy', 'responsibility']);
const AI_IMAGE_TYPE_DESCRIPTIONS = {
  engineering_diagram: '专业工程图示：用于展示设备、系统组件、部署位置、连接关系或工程实施场景，强调结构与关系；不用于步骤流转、组织层级或职责分工。',
  realistic_photo: '专业实景图片：用于表现设备、机房、监控中心、施工、巡检或维护现场等可真实拍摄的对象和环境；不用于抽象系统架构、流程或组织关系。',
};
const MERMAID_IMAGE_TYPE_DESCRIPTIONS = {
  process: '流程图：用于表达按先后顺序发生的步骤、判断、流转和闭环处理过程；不用于静态系统拓扑或人员层级。',
  hierarchy: '层级图：用于表达组织、系统模块、资源分类等上下级或包含关系；不用于时间顺序或职责矩阵。',
  responsibility: '职责关系图：用于表达角色、岗位、责任边界和协作关系；不用于设备拓扑或纯流程步骤。',
};
```

### 10.2 Agent 提名提示词原文

`contentIllustrationPlanning.cjs:143-177`：
```js
// 构建 Agent 全文图片编排任务说明。
function buildIllustrationPlanningPrompt() {
  return `请基于当前工作目录中的三个输入文件完成投标文件技术方案的全文图片编排，即按要求设计投标文件应该在哪个位置，添加什么样的图片：

- technical-plan.md：投标文件全文，叶子小节由 yibiao-section-start / yibiao-section-end 标记。
- outline-tree.json：目录树，用于核对小节 ID、父子关系和顺序，要确保配图的位置一定是真实存在于目录树中的。
- illustration-config.json：三类图片是否启用、允许类型、类型中文说明、上限和可编排小节 ID。

工作要求：
1. 图片有三类：AI生成图片、mermaid图片、html生成类图网页，具体应用哪种，可以查看illustration-config.json的配置，自行判断。
2. illustration-config.json中limit是每类图片的配图上限，如果投标文件实在不适合配图，可以低于limit，但绝不能高于limit。
3. 为每项生成 title，title 是最终写入正文的完整图注文本，建议控制在4-15个字，禁止冗长。
4. 统一编排 title，标准化后不得重复；相同 image_type 可以使用多次，但每张图的标题、业务对象和视觉重点必须明显不同，避免在不同章节编排相同或相似图片。
5. kind 只能是 html、mermaid、ai；image_type 必须来自对应 allowed_types。遇到英文类型标识时，必须先阅读对应 type_descriptions 的中文含义、适用场景和不适用场景，再决定是否选用，不得仅按英文单词猜测。
6. AI 图片适合设备、现场、工程空间、实体部署等具象内容；Mermaid 只用于简单流程、层级和职责关系；HTML 用于配置允许的复杂图表类型。html也可以生成流程、层级和职责关系，根据内容判断如果生成内容较复杂，改用html替代mermaid。
7. AI 和 Mermaid 每项只能引用一个正文叶子小节，placement 必须为 after。
8. HTML 可以引用一个小节，也可以引用同一直接父目录下顺序连续的多个叶子小节；单节 placement 必须为 after。
9. HTML 多节说明类图片使用 before，表示插入组内第一节正文前；总结类图片使用 after，表示插入组内最后一节正文后。
10. priority 只能是 1-5 的整数，5 表示最值得配图。
11. 同一小节只允许编排一张图片，包含在html多节图组中，也算该小节已编排，三种图片优先级html>AI生成图片>mermaid，如果一个小节同时适配多种图片，按以上优先级执行。
12. 输出前必须重新读取 outline-tree.json，确认所有 section_ids 真实存在、属于可编排叶子，并确认 HTML 多节组同父且连续；同时通读全部 title，确认没有重复标题或仅替换章节名称的相似主题。
13. 只创建 illustration-plan.json，不要修改输入文件，不要输出其他结果文件。

illustration-plan.json 只能使用以下结构：
{
  "items": [
    {
      "kind": "html",
      "image_type": "进度网络图",
      "title": "核心业务上线实施进度网络图",
      "section_ids": ["3.2.1", "3.2.2"],
      "placement": "before",
      "priority": 5
    }
  ]
}`;
}
```
> Agent 拿的是**三个工作区文件**（`buildIllustrationPlanningContext:127-138`），不是拼在 prompt 里——上下文在文件里、指令在 prompt 里，这是 Agent 化的标准做法：
```js
    files: [
      { path: 'technical-plan.md', content: markdownLines.join('\n').trim() },
      {
        path: 'outline-tree.json',
        content: JSON.stringify({
          project_name: singleLine(outlineData?.project_name),
          project_overview: String(outlineData?.project_overview || '').trim(),
          outline,
        }, null, 2),
      },

### 10.3 程序拍板：`validateCandidate` 逐条硬校验

`contentIllustrationPlanning.cjs:225-273`：
```js
function validateCandidate(candidate, context) {
  const config = context.config[candidate.kind];
  if (!ILLUSTRATION_KIND_ORDER.has(candidate.kind) || !config?.enabled) {
    throw new Error(`图片候选类型未启用或无效：${candidate.kind || 'empty'}`);
  }
  if (!config.allowed_types.includes(candidate.image_type)) {
    throw new Error(`图片候选 image_type 无效：${candidate.image_type || 'empty'}`);
  }
  if (!candidate.title) {
    throw new Error('图片候选 title 不能为空');
  }
  if (candidate.title.length > 20) {
    throw new Error(`图片候选 title 不能超过 20 个字：${candidate.title}`);
  }
  if (/^图\s*[:：]/u.test(candidate.title)) {
    throw new Error(`图片候选 title 不应包含"图："前缀：${candidate.title}`);
  }
  if (!Number.isInteger(candidate.priority) || candidate.priority < 1 || candidate.priority > 5) {
    throw new Error('图片候选 priority 必须是 1-5 的整数');
  }
  if (!['before', 'after'].includes(candidate.placement)) {
    throw new Error('图片候选 placement 必须是 before 或 after');
  }
  if (!candidate.section_ids.length || new Set(candidate.section_ids).size !== candidate.section_ids.length) {
    throw new Error('图片候选 section_ids 不能为空或重复');
  }
  const sections = candidate.section_ids.map((id) => context.sectionMap.get(id));
  if (sections.some((section) => !section?.eligible)) {
    throw new Error(`图片候选包含无效正文小节：${candidate.section_ids.join(', ')}`);
  }
  if (candidate.kind !== 'html' && candidate.section_ids.length !== 1) {
    throw new Error(`${candidate.kind} 图片只能编排到一个小节`);
  }
  if (candidate.section_ids.length === 1 && candidate.placement !== 'after') {
    throw new Error('单节图片 placement 必须为 after');
  }
  if (candidate.kind === 'html' && candidate.section_ids.length > 1) {
    const parentId = sections[0].parentId;
    if (!parentId || sections.some((section) => section.parentId !== parentId)) {
      throw new Error('HTML 多节图片必须属于同一直接父目录');
    }
    for (let index = 1; index < sections.length; index += 1) {
      if (sections[index].siblingIndex !== sections[index - 1].siblingIndex + 1) {
        throw new Error('HTML 多节图片的小节必须按目录顺序连续');
      }
    }
  }
  return { ...candidate, firstOrder: sections[0].order };
}
```
**9 条硬约束**，任一违反**整份计划作废**（不是静默丢一条）。
`eligible` 判定（`:58-63`）：必须是**叶子** + `content_mode === 'ai-generate'` + `sections[id].status === 'success'` + 有正文：
```js
      const content = isLeaf && item?.content_mode === 'ai-generate' && sections?.[id]?.status === 'success'
        ? resolveSectionContent(item, sections)
        : '';
      const eligible = Boolean(isLeaf
        && content
        && sections?.[id]?.status === 'success');
```
**额外字段检查**（`:276-289`）：
```js
function resolveIllustrationPlan(content, context) {
  const parsed = typeof content === 'string' ? extractJsonObject(content) : content;
  if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.items)) {
    throw new Error('Agent 图片编排结果缺少 items 数组');
  }
  const extraRootFields = Object.keys(parsed).filter((key) => key !== 'items');
  if (extraRootFields.length) throw new Error(`Agent 图片编排结果包含多余字段：${extraRootFields.join(', ')}`);

  const allowedFields = new Set(['kind', 'image_type', 'title', 'section_ids', 'placement', 'priority']);
  const candidates = parsed.items.map((item, index) => {
    const extraFields = Object.keys(item || {}).filter((key) => !allowedFields.has(key));
    if (extraFields.length) throw new Error(`图片候选包含多余字段：${extraFields.join(', ')}`);
    return validateCandidate(normalizeCandidate(item, index), context);
  });
```
容错解析（`:180-210` `extractJsonObject`）：剥围栏 → 直接 `JSON.parse` → 失败则手写括号配平扫描。

---

### 10.4 冲突消解（HTML > AI > Mermaid）

`contentIllustrationPlanning.cjs:291-305`：
```js
  const occupiedSectionIds = new Set();
  const selected = [];
  const candidateStats = { ai: 0, mermaid: 0, html: 0 };
  const selectedStats = { ai: 0, mermaid: 0, html: 0 };
  for (const candidate of candidates) candidateStats[candidate.kind] += 1;

  for (const kind of ILLUSTRATION_KINDS) {          // 顺序 = ['html','ai','mermaid']
    const sorted = candidates
      .filter((candidate) => candidate.kind === kind)
      .sort((a, b) => b.priority - a.priority || a.firstOrder - b.firstOrder || a.outputIndex - b.outputIndex);
    for (const candidate of sorted) {
      if (selectedStats[kind] >= context.config[kind].limit) continue;
      if (candidate.section_ids.some((id) => occupiedSectionIds.has(id))) continue;
      selected.push(candidate);
      selectedStats[kind] += 1;
```
**三重排序**：`priority` 降序 → 目录 `firstOrder` 升序 → 输出序 `outputIndex` 升序。
**`kind` 遍历顺序 = 优先级**（`ILLUSTRATION_KINDS` 的数组顺序），html 先占小节，ai/mermaid 后续遇到冲突即 `continue` 跳过。**冲突不报错，静默让位。**
**`candidateStats` vs `selectedStats`** 同时保留，供 UI 展示「提名 N 条 / 采纳 M 条」。

### 10.5 maxAiImages 分段择优算法

**题面描述「分段择优避免前段耗尽」在本仓并非如此** —— 实际是**全局一次性择优**，没有分段。

`contentIllustrationPlanning.cjs:99-121`：
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

# 三、知识库（非 RAG）

## 11. 完整构建流程（7 步）

主流程 `prepareDocument`（`knowledgeBaseService.cjs:1165-1506`）+ `matchDocument`（`:1508-2000+`）。

```
[步骤0] copy_source            复制原始文件到工作区
[步骤0] convert_markdown       解析为 Markdown
  ↓
[步骤1] createRawBlocks        切分 block（R000001…，含 heading_path）
  ↓ mergeSemanticBlocks       语义合并到 ~500 字
  ↓
[步骤2] filterBlocks           清理无效 block（7 类 reason）→ 重编号 P000001…
  ↓
[步骤3] extract_first_items    第一轮提取条目（title+summary，无 id）
  ↓
[步骤4] extract_supplement_items 第二轮补漏（带 first_round_items 去重）
  ↓ mergeTitleSummaryItems 按标题归一 → mergeCandidateItems 分配 K000001…
  ↓
[步骤5] match_batches          分批匹配原文（闭区间 ranges）
  ↓
[步骤6] recover_missing        遗漏 block 补漏（≤2 轮，可新增条目）
  ↓
[步骤7] save_result            createFinalItems 拼正文 → knowledge_items
```

每步都通过 `runDocumentStep(documentId, stepKey, worker)`（`:1151`）记录状态，配合 `stepCanReuse`（`:1128`）实现**断点续跑**；重跑上游会 `clearDocumentProcessingFromStep` 级联作废下游。

### 11.1 步骤 1-2：block 切分与清理

**切分**（`createRawBlocks:274-335`）——按行扫描，按类型（`heading`/`paragraph`/`table`/`list`）分桶，维护 heading 栈：
```js
    const chunks = content.length > oversizedBlockChars ? splitOversizedText(content, Math.floor(oversizedBlockChars * 0.75)) : [content];
    for (const chunk of chunks) {
      blocks.push({
        id: `R${String(blocks.length + 1).padStart(6, '0')}`,
        type: currentType,
        heading_path: headings.filter(Boolean),
        content: chunk,
      });
    }
```
类型判定（`:321-325`）：
```js
    const nextType = /^\s*\|.*\|\s*$/.test(line)
      ? 'table'
      : /^\s*(?:[-*+]\s+|\d+[.)、]\s+)/.test(line)
        ? 'list'
        : 'paragraph';
```
常量（`:12-20`）：
```js
const supportedExtensions = new Set(['.doc', '.docx', '.wps', '.pdf', '.md', '.markdown', '.xls', '.xlsx']);
const oversizedBlockChars = 8000;
const semanticMergeTargetChars = 500;
const recoveryMaxAttempts = 2;
const DEFAULT_CONTEXT_LENGTH_LIMIT = 400000;
const KNOWLEDGE_CONTEXT_LIMIT_RATIO = 0.8;
const TASK_AND_ITEMS_RESERVE_RATIO = 0.2;
const PROMPT_CACHE_WARMUP_DELAY_MS = 5000;
```

**语义合并**（`mergeSemanticBlocks:210-272`）——`semanticMergeTargetChars = 500`：
```js
  for (const block of rawBlocks) {
    if (isTableBlock(block)) { flushBuffer(); pushStandalone(block); continue; }   // 表格永不合并
    if (isSemanticHeadingBlock(block)) {
      if (buffer.length && !bufferHasOnlyHeadings() && getContentCharCount(bufferText()) >= 100) { flushBuffer(); }
      buffer.push(block);
      continue;
    }
    const blockChars = getContentCharCount(block.content);
    if (!buffer.length && blockChars >= semanticMergeTargetChars) { pushStandalone(block); continue; }
    buffer.push(block);
    if (getContentCharCount(bufferText()) >= semanticMergeTargetChars) { flushBuffer(); }
  }
```
> 表格**独立成块**是刻意的——表格的语义完整性不能被拆开。

**清理**（`filterBlocks:337-380`）——7 类 reason，重编号为 `P`：
```js
  rawBlocks.forEach((block, index) => {
    const repeatedKey = normalizeRepeatedText(block.content);
    const repeated = repeatedKey && repeatedKey.length <= 80 && repeatedCounts.get(repeatedKey) >= 3;
    const reason = !String(block.content || '').trim()
      ? 'empty'
      : isPageNumberBlock(block.content)
        ? 'page_number'
        : getContentCharCount(block.content) < 100
          ? 'too_short'
          : isCatalogBlock(block.content)
            ? 'catalog'
            : repeated
              ? 'repeated_header_footer'
              : isCoverBlock(block.content, index)
                ? 'cover'
                : isSignatureBlock(block.content)
                  ? 'signature_page'
                  : '';
    if (reason) { filtered.push({ ...block, reason }); return; }
    kept.push({ ...block, id: `P${String(kept.length + 1).padStart(6, '0')}` });
  });
```
| reason | 判定 | 位置 |
|---|---|---|
| `empty` | 去空白后为空 | — |
| `page_number` | `12` / `第3页共5页` / `3/4` / `page3of4` | `:123-130` |
| `too_short` | **有效字符数 < 100** | `:178-180` |
| `catalog` | 目录页：≥60% 行以 `....数字` 结尾 | `:132-146` |
| `repeated_header_footer` | 归一后 ≤80 字且**出现 ≥3 次** | `:337-344` |
| `cover` | 前 12 块内、≤220 字、含投标文件/招标编号/正本等标记、无长句 | `:148-163` |
| `signature_page` | ≤260 字且含盖章/法定代表人/年月日等，且无 20 字以上长句 | `:165-176` |

**归一化**（`normalizeRepeatedText:114-121`）：
```js
function normalizeRepeatedText(text) {
  return String(text || '')
    .replace(/^#+\s*/, '')
    .replace(/\s+/g, '')
    .replace(/[\-—_·.。:：|第页共]/g, '')
    .trim()
    .toLowerCase();
}
```
**Prompt 渲染**（`renderBlocksForPrompt:382-393`）：
```js
function renderBlocksForPrompt(blocks) {
  return blocks.map((block) => {
    const headingPath = block.heading_path?.length ? block.heading_path.join(' > ') : '无';
    return [`[${block.id}]`, `type: ${block.type}`, `heading_path: ${headingPath}`, 'text:', block.content].join('\n');
  }).join('\n\n');
}
```


### 11.2 统一分段（Prompt Cache 关键设计）

预算计算（`:826-833`）：
```js
function getRequestBudget(aiService) {
  const config = typeof aiService?.getConfig === 'function' ? aiService.getConfig() : {};
  const rawLimit = Number(config?.context_length_limit);
  const contextLengthLimit = Number.isFinite(rawLimit) && rawLimit > 0
    ? Math.floor(rawLimit)
    : DEFAULT_CONTEXT_LENGTH_LIMIT;
  return Math.floor(contextLengthLimit * KNOWLEDGE_CONTEXT_LIMIT_RATIO);
}
```
**`buildUnifiedBlockSegments:839-850`**（注释即设计说明）：
```js
/**
 * 统一 block 分段（策略 B）：提取前按保守预留切一次，提取/补充/匹配共用同一段表
 * blockBudget = requestBudget * (1 - TASK_AND_ITEMS_RESERVE_RATIO)
 */
function buildUnifiedBlockSegments(blocks, aiService) {
  const requestBudget = getRequestBudget(aiService);
  const reserve = Math.max(1, Math.floor(requestBudget * TASK_AND_ITEMS_RESERVE_RATIO));
  const blockSegmentLimit = Math.max(1, requestBudget - reserve);
  const segments = packBlocksIntoSegments(blocks, blockSegmentLimit);
  return { segments, blockSegmentLimit, requestBudget, reserve };
}
```
**L1 前缀跨三步字节级一致**（`:678-697`）是整个设计的核心：
```js
/**
 * L1：本段 block 前缀（跨提取/补充/匹配必须字节级一致，才能吃到 block 缓存）
 * 引导句固定，段号写入 L1 时三步共用同一 segmentMeta
 */
function buildDocumentBlocksPrefixMessage(blockText, segmentMeta = null) {
  const segmentLine = segmentMeta?.total > 1
    ? `当前是第 ${segmentMeta.index}/${segmentMeta.total} 段。`
    : '当前文档仅此一段。';
  return {
    role: 'user',
    content: [
      '以下是接下来要处理的主要内容 block 列表，请先完整阅读理解。',
      '只能使用本段出现的 block id；不要假设未见过的其它段内容。',
      segmentLine,
      '<document_blocks>',
      blockText,
      '</document_blocks>',
    ].join('\n'),
  };
}
```
**L1' 遗漏 block 前缀**（`:699-715`）——补漏独立 pack，**不与全文段混用**：
```js
/** L1'：遗漏 block 前缀（补漏独立 pack，不与全文段混用） */
function buildMissingBlocksPrefixMessage(missingBlocks, segmentMeta = null) {
  const segmentLine = segmentMeta?.total > 1
    ? `当前是第 ${segmentMeta.index}/${segmentMeta.total} 段遗漏 block。`
    : '以下是当前需要处理的遗漏 block。';
  return {
    role: 'user',
    content: [
      '以下是接下来要处理的遗漏 block 列表，请先完整阅读理解。',
      '必须覆盖本段收到的全部遗漏 block；只能使用本段出现的 block id。',
      segmentLine,
      '<missing_blocks>',
      renderBlocksForPrompt(missingBlocks),
      '</missing_blocks>',
    ].join('\n'),
  };
}
```
配合**预热 5 秒**（`:1357-1369`）：
```js
          const firstSegmentItems = await runSegment(segments[0]);
          if (segments.length > 1) {
            debugLog(documentId, 'ai:first-items:warmup-wait', { delay_ms: PROMPT_CACHE_WARMUP_DELAY_MS });
            updateDocument(documentId, {
              status: 'extracting',
              progress: Math.min(54, 35 + Math.round((1 / segments.length) * 18)),
              message: `提示词缓存预热完成，等待后并发提取剩余 ${segments.length - 1} 段`,
            }, webContents);
            await waitForPromptCacheWarmup();
          }
          const remainingItems = segments.length > 1
            ? await runParallelAndThrowAfterSettled(segments.slice(1).map((segment) => () => runSegment(segment)))
            : [];
```
> **先串行跑第 1 段让服务端缓存这段超长 block 前缀，再等 5 秒，然后并发剩余段**——因为所有段的 L1 结构完全一致，只是段号不同。这样第 2~N 段只付 L2 的增量 token。

条目侧预算（`:852-856`）：
```js

### 11.3 第一轮提取（提示词原文）

`knowledgeBaseService.cjs:718-730`：
```js
function buildInitialItemTaskMessage(documentName) {
  return {
    role: 'user',
    content: [
      `文档名：${documentName}`,
      '你是投标资料知识库分析助手。你只负责从历史投标资料中提取对后续编写标书有复用价值的知识条目。',
      '任务：基于上文已给出的本段 block，提取有意义的知识条目数组。条目应覆盖技术方案、项目管理、质量、安全、进度、服务、应急、人员设备、类似业绩等可复用内容。',
      '本段没有可复用知识时必须返回 {"items":[]}。',
      '只返回 JSON：{"items":[{"title":"","summary":""}]}',
      '要求：title 简洁明确；summary 说明该条目可如何用于编写投标文件；不要输出 id、content、段落编号、Markdown 或解释文字。',
    ].join('\n'),
  };
}
```
消息组装（`:732-737`）：
```js
function buildInitialItemMessages(documentName, blockText, segmentMeta = null) {
  return [
    buildDocumentBlocksPrefixMessage(blockText, segmentMeta),
    buildInitialItemTaskMessage(documentName),
  ];
}
```
**关键设计：这一轮只出 `title` + `summary`，不分配 id、不找原文。** id 由程序在合并阶段分配（`:670`），原文在第三步才匹配。**职责彻底分离**。

调用（`:1329-1339`）：
```js
            const first = await aiService.collectJsonResponse({
              messages: firstMessages,
              response_format: { type: 'json_object' },
              logTitle: segments.length > 1
                ? `知识库条目提取-${document.file_name}-第${segment.index}段`
                : `知识库条目提取-${document.file_name}`,
              normalizer: (value) => ({ items: normalizeCandidateItems(value) }),
              validator: validateCandidateItems,
              failureMessage: '知识库条目提取失败，AI 未返回有效 JSON',
              progressLabel: '知识库条目提取',
            });
```
> ⚠️ 未传 `max_retries` → 走默认 2（`aiService.cjs:834`），即最多 3 次尝试。

归一化与校验（`:632-645`）：
```js
function normalizeCandidateItems(parsed) {
  const items = Array.isArray(parsed) ? parsed : parsed?.items;
  if (!Array.isArray(items)) return [];
  return items.map((item) => ({
    title: String(item?.title || '').trim(),
    summary: String(item?.summary || item?.resume || '').trim(),
  })).filter((item) => item.title && item.summary);
}

function validateCandidateItems(value) {
  if (!Array.isArray(value?.items)) {
    throw new Error('AI 返回结果缺少 items 数组');
  }
}
```

### 11.4 第二轮补漏（提示词原文）

`knowledgeBaseService.cjs:740-757`：
```js
function buildSupplementItemTaskMessage(documentName, firstItems) {
  return {
    role: 'user',
    content: [
      `文档名：${documentName}`,
      '你是投标资料知识库补漏助手。你只判断已有知识条目是否遗漏了重要主题，并补充缺失条目。',
      'first_round_items 是全文已有结果，不要重复首轮已有条目。',
      '任务：基于上文本段 block，只输出本段可见且首轮未覆盖的新增条目；如果没有遗漏，返回空 items 数组。',
      '只返回 JSON：{"items":[{"title":"","summary":""}]}',
      '如果没有新增条目，必须返回 {"items":[]}，这属于正常结果。',
      '不要重复已有条目，不要输出 id、content、段落编号、Markdown 或解释文字。',
      '',
      '<first_round_items>',
      renderCandidateItemsJson(firstItems),
      '</first_round_items>',
    ].join('\n'),
  };
}
```
> **注意**：第二轮拿到的是**全文** `firstItems`（不是本段的），所以是真正的「全文补漏」。
渲染函数（`:616-630`）：
```js
function renderCandidateItemsJson(items) {
  return JSON.stringify(
    (items || []).map(({ title, summary }) => ({ title, summary })),
    null,
    2,

### 11.5 合并候选条目

`mergeTitleSummaryItems:648-660`（分段结果按标题去重）→ `mergeCandidateItems:662-676`（分配 id）：
```js
/** 分段提取结果按标题去重合并（仅 title/summary，不含 id） */
function mergeTitleSummaryItems(itemLists) {
  const merged = [];
  const seen = new Set();
  for (const item of (itemLists || []).flat()) {
    const title = String(item?.title || '').trim();
    const summary = String(item?.summary || item?.resume || '').trim();
    const key = title.replace(/\s+/g, '').toLowerCase();
    if (!key || !summary || seen.has(key)) continue;
    seen.add(key);
    merged.push({ title, summary });
  }
  return merged;
}

function mergeCandidateItems(firstItems, supplementItems) {
  const merged = [];
  const seen = new Set();
  for (const item of [...firstItems, ...supplementItems]) {
    const key = item.title.replace(/\s+/g, '').toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    merged.push({
      id: `K${String(merged.length + 1).padStart(6, '0')}`,
      title: item.title,
      summary: item.summary,
    });
  }
  return merged;
}
```
**顺序即优先级**：`firstItems` 在前 → 首轮提取的条目永远赢同名竞争。
空结果直接抛错（`:1473` / `:1479`）：`throw new Error('AI 未提取出可用知识条目');`
阶段交界（`:1482-1495`）：
```js
      updateDocument(documentId, {
        status: 'ready_for_matching',
        progress: 65,
        message: isDeveloperMode()
          ? `已提取 ${candidateItems.length} 条候选知识，可开始自动分段匹配`
          : `已提取 ${candidateItems.length} 条候选知识，正在自动匹配段落`,
        candidate_item_count: candidateItems.length,
        item_count: 0,
      }, webContents);

      if (!isDeveloperMode()) {
        debugLog(documentId, 'prepare:auto-match');
        await matchDocument(documentId, webContents);
      }
```

### 11.6 分批匹配原文（提示词原文）

`knowledgeBaseService.cjs:767-794`：
```js
function buildMatchTaskMessage(documentName, batchItems) {
  return {
    role: 'user',
    content: [
      `文档名：${documentName}`,
      '你是投标知识库段落匹配助手。你只根据知识条目的标题和摘要，为其匹配强相关 block 范围。',
      '规则：',
      '1. 只处理本次给出的知识条目。',
      '2. 只匹配与条目强相关、可直接支撑该条目的 block（基于上文本段 block）。',
      '3. 如果某些 block 更可能属于其他主题或条目，不要强行匹配。',
      '4. 只返回 id 和 ranges，不要输出正文，不要解释。',
      '5. ranges 使用闭区间：["P000001","P000003"] 表示连续 block；单个 block 写成 ["P000001","P000001"]。',
      '6. 只允许使用本段存在的 block 编号和本次条目 id。',
      '输出 JSON：{"matches":[{"id":"K000001","ranges":[["P000001","P000003"]]}]}',
      '',
      '以下是本次需要匹配的知识条目。只处理这些条目：',
      renderKnowledgeItemsJson(batchItems),
    ].join('\n'),
  };
}

/** 匹配：block 前缀在前，任务+条目在后（item-split 仅改 L2 条目，L1 不变） */
function buildMatchMessages(documentName, blockText, batchItems, segmentMeta = null) {
  return [
    buildDocumentBlocksPrefixMessage(blockText, segmentMeta),
    buildMatchTaskMessage(documentName, batchItems),
  ];
}
```
> **闭区间 `ranges` 压缩表示**（而非直接给 block_ids 数组）——把 N 个离散 block 压成最少区间，显著降低输出 token。

归一化与校验（`:926-941`）只接受白名单 id + 有效 ranges：
```js
function normalizeMatchResult(parsed, itemIds, blocks, blockOrder) {
  const matches = Array.isArray(parsed?.matches) ? parsed.matches : [];
  return {
    matches: matches.map((match) => {
      const id = String(match?.id || '').trim();
      const ranges = normalizeRanges(match?.ranges || match?.paragraph_ranges || match?.block_ranges || [], blockOrder);
      return itemIds.has(id) && ranges.length ? { id, ranges, block_ids: expandRanges(ranges, blocks, blockOrder) } : null;
    }).filter(Boolean),
  };
}

function validateMatchResult(value) {
  if (!Array.isArray(value?.matches)) {
    throw new Error('AI 返回结果缺少 matches 数组');
  }
}
```

**匹配指纹与失效**（`:1547-1585`）——block 或条目一变，整步清空重跑：
```js
      // 指纹：本段 block_ids + 全部候选条目 id；任一已存段不一致则清空整步重跑
      const buildMatchFingerprint = (blockIds, itemIds) => ({
        block_ids: [...(blockIds || [])],
        item_ids: [...(itemIds || [])],
      });
      const readMatchFingerprint = (raw) => {
        if (!raw || Array.isArray(raw) || !Array.isArray(raw.block_ids) || !Array.isArray(raw.item_ids)) {
          return null;
        }
        return { block_ids: raw.block_ids.map(String), item_ids: raw.item_ids.map(String) };
      };
      const isSameFingerprint = (left, right) => (
        left
        && right
        && isSameStringList(left.block_ids, right.block_ids)
        && isSameStringList(left.item_ids, right.item_ids)
      );
      ...
      if (fingerprintMismatch && existingBatches.length) {
        knowledgeBaseStore.clearMatchBatches(documentId);
        debugLog(documentId, 'match:clear-batches', {
          reason: 'fingerprint_mismatch',

### 11.7 遗漏 block 补漏（提示词原文）

遗漏判定（`:977-988`）：
```js
function collectHandledBlockIds(matches, discarded, systemDiscarded) {
  const handled = new Set();
  matches.forEach((match) => match.block_ids.forEach((id) => handled.add(id)));
  discarded.forEach((item) => item.block_ids.forEach((id) => handled.add(id)));
  systemDiscarded.forEach((item) => item.block_ids.forEach((id) => handled.add(id)));
  return handled;
}

function getMissingBlocks(blocks, matches, discarded, systemDiscarded) {
  const handled = collectHandledBlockIds(matches, discarded, systemDiscarded);
  return blocks.filter((block) => !handled.has(block.id));
}
```
**三分类强制覆盖**提示词（`:797-816`）：
```js
function buildRecoveryTaskMessage(documentName, items) {
  return {
    role: 'user',
    content: [
      `文档名：${documentName}`,
      '你是投标知识库遗漏段落补漏助手。必须把上文收到的遗漏 block 明确归入已有条目、新增条目或舍弃段落。',
      '任务：必须覆盖所有遗漏 block。每个遗漏 block 只能进入以下三类之一：',
      '1. matches：归入已有知识条目，只返回已有 id 和 ranges。',
      '2. new_items：如果没有合适的已有条目但内容有复用价值，则新增知识条目，并给出 title、summary、ranges。',
      '3. discarded：如果内容质量低、重复、格式残留或无投标复用价值，则推荐舍弃，并给出 reason。',
      '输出 JSON：{"matches":[{"id":"K000001","ranges":[["P000001","P000003"]]}],"new_items":[{"title":"","summary":"","ranges":[["P000004","P000005"]]}],"discarded":[{"ranges":[["P000006","P000006"]],"reason":""}]}',
      '不要输出正文、Markdown 或解释文字。',
      '只能使用本段存在的 block 编号和本次给出的条目 id。',
      '',
      '<knowledge_items>',
      renderKnowledgeItemsJson(items),
      '</knowledge_items>',
    ].join('\n'),
  };
}

/** 补漏：missing 前缀在前，任务+条目在后 */
function buildRecoveryMessages(documentName, items, missingBlocks, segmentMeta = null) {
  return [
    buildMissingBlocksPrefixMessage(missingBlocks, segmentMeta),
    buildRecoveryTaskMessage(documentName, items),
  ];
}
```
补漏结果归一化（`:943-975`）：
```js
function normalizeRecoveryResult(parsed, itemIds, blocks, blockOrder) {
  const matches = Array.isArray(parsed?.matches) ? parsed.matches : [];
  const newItems = Array.isArray(parsed?.new_items) ? parsed.new_items : [];
  const discarded = Array.isArray(parsed?.discarded) ? parsed.discarded : [];

  return {
    matches: matches.map((match) => {
      const id = String(match?.id || '').trim();
      const ranges = normalizeRanges(match?.ranges || [], blockOrder);
      return itemIds.has(id) && ranges.length ? { id, ranges, block_ids: expandRanges(ranges, blocks, blockOrder) } : null;
    }).filter(Boolean),
    new_items: newItems.map((item) => {
      const title = String(item?.title || '').trim();
      const summary = String(item?.summary || item?.resume || '').trim();
      const ranges = normalizeRanges(item?.ranges || [], blockOrder);
      return title && summary && ranges.length ? { title, summary, ranges, block_ids: expandRanges(ranges, blocks, blockOrder) } : null;
    }).filter(Boolean),
    discarded: discarded.map((item) => {
      const ranges = normalizeRanges(item?.ranges || [], blockOrder);
      return ranges.length ? {
        ranges,
        block_ids: expandRanges(ranges, blocks, blockOrder),
        reason: String(item?.reason || 'AI 建议舍弃').trim() || 'AI 建议舍弃',
      } : null;
    }).filter(Boolean),
  };
}

function validateRecoveryResult(value) {
  if (!Array.isArray(value?.matches) || !Array.isArray(value?.new_items) || !Array.isArray(value?.discarded)) {
    throw new Error('AI 返回结果缺少 matches/new_items/discarded 数组');
  }
}
```
**循环 2 轮**（`recoveryMaxAttempts = 2`），每轮后仍遗漏的记 `systemDiscarded`（`:1983-1990`）：
```js
          const remaining = getMissingBlocks(blocks, recoveredMatches, discarded, systemDiscarded);
          debugLog(documentId, 'match:remaining-after-recovery', { remaining_block_count: remaining.length });
          if (remaining.length) {
            systemDiscarded.push({
              block_ids: remaining.map((block) => block.id),
              reason: 'system_discarded_after_retry',
            });
          }
```
**多子批归属唯一性**（`mergeRecoverySegmentResults:520-535`）——优先级 `matches > new_items > discarded`，且**全局按优先级认领**：
```js
/** 补漏多子批时，每个 block 只保留一种归属：matches > new_items > discarded */
function mergeRecoverySegmentResults(parsedList, itemIds, blocks, blockOrder) {
  const ownership = new Map();
  const matchRangesByItem = new Map();
  const newItems = [];
  const discarded = [];
  const sourceList = parsedList || [];

  const claimBlock = (blockId, kind, payload) => {
    if (!blockId || ownership.has(blockId)) return false;
    ownership.set(blockId, { kind, payload });
    return true;
  };

  // 必须全局按优先级认领，避免子批顺序导致 discarded 抢先占住 matches
  for (const parsed of sourceList) {
    for (const match of parsed.matches || []) {
```
> 这是**顺序无关性**的正确实现：不是「按子批顺序先到先得」，而是「先扫完所有 matches，再扫所有 new_items，最后 discarded」。

新条目 id 递增（`nextKnowledgeItemId:990-997`）：
```js
function nextKnowledgeItemId(items) {
  let max = 0;
  items.forEach((item) => {
    const match = /^K(\d+)$/.exec(item.id || '');
    if (match) max = Math.max(max, Number(match[1]));
  });
  return `K${String(max + 1).padStart(6, '0')}`;
}
```
item-split 降级路径（`:1851-1894`）——单段过长时再按条目切子批：

### 11.8 保存条目

`createFinalItems:999-1019`——**block 原文按匹配顺序拼接**：
```js
function createFinalItems(items, matches, blocks, fileName) {
  const blockMap = new Map(blocks.map((block) => [block.id, block]));
  const blocksByItem = new Map();
  matches.forEach((match) => {
    const current = blocksByItem.get(match.id) || [];
    blocksByItem.set(match.id, [...new Set([...current, ...match.block_ids])]);
  });

  return items.map((item) => {
    const sourceBlockIds = blocksByItem.get(item.id) || [];
    const content = sourceBlockIds.map((id) => blockMap.get(id)?.content || '').filter(Boolean).join('\n\n').trim();
    return {
      id: item.id,
      title: item.title,
      resume: item.summary,
      content,
      source_block_ids: sourceBlockIds,
      source_file: fileName,
    };
  }).filter((item) => item.content);
}
```
**`filter((item) => item.content)`** —— 匹配不到原文的条目不落库。

覆盖报告（`createReport:1021-1046`）：
```js
function createReport({ blocks, filteredBlocks, candidateItems, finalItems, matches, discarded, systemDiscarded, recoveryAttempts, batchSize }) {
  const matched = new Set();
  matches.forEach((match) => match.block_ids.forEach((id) => matched.add(id)));
  const discardedSet = new Set();
  discarded.forEach((item) => item.block_ids.forEach((id) => discardedSet.add(id)));
  const systemSet = new Set();
  systemDiscarded.forEach((item) => item.block_ids.forEach((id) => systemSet.add(id)));
  const handled = new Set([...matched, ...discardedSet, ...systemSet]);
  const total = blocks.length || 1;

  return {
    total_blocks: blocks.length,
    filtered_blocks_count: filteredBlocks.length,
    candidate_items_count: candidateItems.length,
    final_items_count: finalItems.length,
    matched_blocks_count: matched.size,
    discarded_blocks_count: discardedSet.size,
    system_discarded_after_retry_count: systemSet.length,
    new_items_from_recovery_count: recoveryAttempts.reduce((sum, attempt) => sum + attempt.new_items.length, 0),

## 12. 知识条目数据结构完整字段

### 12.1 最终条目（`knowledge_items` 表）

`sqliteDatabase.cjs`：
```sql
    CREATE TABLE IF NOT EXISTS knowledge_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      document_id TEXT NOT NULL,
      item_id TEXT NOT NULL,
      title TEXT NOT NULL,
      resume TEXT NOT NULL,
      content TEXT NOT NULL,
      source_file TEXT,
      content_chars INTEGER NOT NULL DEFAULT 0,
      sort_order INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      FOREIGN KEY (document_id) REFERENCES knowledge_documents(document_id) ON DELETE CASCADE,
      UNIQUE(document_id, item_id)
    );

    CREATE INDEX IF NOT EXISTS idx_knowledge_items_title
    ON knowledge_items(title);
```
| 字段 | 语义 |
|---|---|
| `item_id` | `K000001` 格式，**文档内**唯一 |
| `title` | 条目标题（AI 产出） |
| `resume` | 摘要（AI 产出，**说明该条目可如何用于编写投标文件**） |
| `content` | 匹配的 block 原文拼接（`\n\n` 分隔） |
| `source_block_ids` | 来源 block（存 `knowledge_item_blocks` 关联表） |
| `source_file` | 源文件名 |

**读取结构**（`knowledgeBaseStore.cjs:1090-1096`）：
```js
        items.push({
          id: row.item_id,
          title: row.title,
          resume: row.resume,
          content: row.content,
          source_block_ids: blocksByItem.get(`${row.document_id}::${row.item_id}`) || [],
          source_file: row.source_file || undefined,
        });
```
**对外主键 = `${documentId}::${itemId}`**（`contentGenerationTask.cjs:2119`）：
```js
        if (documentId && itemId && content) contentMap.set(`${documentId}::${itemId}`, { content });
```

### 12.2 候选条目（`knowledge_candidate_items`）

```sql
    CREATE TABLE IF NOT EXISTS knowledge_candidate_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      document_id TEXT NOT NULL,
      item_id TEXT NOT NULL,
      title TEXT NOT NULL,
      summary TEXT NOT NULL,
      source TEXT,
      sort_order INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      FOREIGN KEY (document_id) REFERENCES knowledge_documents(document_id) ON DELETE CASCADE,
      UNIQUE(document_id, item_id)
    );
```
只有 `title` + `summary`（无 `content`）——**这正是「非 RAG」的数据形态：条目是「标题+摘要」索引，内容靠 ranges 关联回原文。**

### 12.3 block

```sql
    CREATE TABLE IF NOT EXISTS knowledge_blocks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      document_id TEXT NOT NULL,
      block_id TEXT NOT NULL,          -- P000001
      type TEXT NOT NULL,              -- heading/paragraph/table/list
      heading_path_json TEXT,
      content TEXT NOT NULL,

### 12.4 文档与步骤状态

`knowledge_documents` 表（`sqliteDatabase.cjs`）记录全过程计数：
```sql
      markdown_hash TEXT,
      markdown_chars INTEGER NOT NULL DEFAULT 0,
      source_extension TEXT,
      status TEXT NOT NULL,
      progress INTEGER NOT NULL DEFAULT 0,
      message TEXT NOT NULL DEFAULT '',
      error TEXT,
      item_count INTEGER NOT NULL DEFAULT 0,
      block_count INTEGER NOT NULL DEFAULT 0,
      filtered_block_count INTEGER NOT NULL DEFAULT 0,
      candidate_item_count INTEGER NOT NULL DEFAULT 0,
      discarded_block_count INTEGER NOT NULL DEFAULT 0,
      system_discarded_after_retry_count INTEGER NOT NULL DEFAULT 0,
      last_batch_size INTEGER,
```

`readReferences`（`knowledgeBaseStore.cjs:1060-1109`）返回：
```js
      return [{
        document,
        items: itemsByDocument.get(documentId) || [],
        ...(options.includeMarkdown ? { markdown: fs.existsSync(markdownPath) ? fs.readFileSync(markdownPath, 'utf-8') : '' } : {}),
      }];
```

## 13. 写正文时如何自动判断使用哪个知识条目（skill 元数据机制）

**核心机制：每个章节生成前，先跑一次「正文编排」（content plan），由 AI 从「轻量条目清单（id+标题+简介）」中挑选 `knowledge.item_ids`。**

### 13.1 编排数据结构（`CONTENT_PLAN_VERSION = 4`，`contentGenerationTask.cjs:47`）

`normalizeContentPlan:569-599`：
```js
function normalizeContentPlan(value, allowedKnowledgeItemIds, allowedFactTitles) {
  const source = value?.plan && typeof value.plan === 'object' ? value.plan : value || {};
  const writing = source.writing && typeof source.writing === 'object' && !Array.isArray(source.writing) ? source.writing : {};
  const knowledgeSource = source.knowledge;
  const knowledge = knowledgeSource && typeof knowledgeSource === 'object' && !Array.isArray(knowledgeSource) ? knowledgeSource : {};
  const rawKnowledgeItemIds = Array.isArray(knowledgeSource)
    ? knowledgeSource
    : knowledge.item_ids ?? knowledge.itemIds ?? knowledge.knowledge_item_ids ?? source.knowledge_item_ids ?? source.knowledgeItemIds;
  const factsSource = source.facts;
  const facts = factsSource && typeof factsSource === 'object' && !Array.isArray(factsSource) ? factsSource : {};
  const rawFactTitles = Array.isArray(factsSource)
    ? factsSource
    : facts.titles ?? facts.fact_titles ?? facts.factTitles ?? source.fact_titles ?? source.factTitles ?? source.global_fact_titles ?? source.globalFactTitles;
  const table = source.table && typeof source.table === 'object' ? source.table : {};
  const tableNeeded = Boolean(table.needed);

  return {
    writing_focus: singleLine(source.writing_focus || source.writingFocus || writing.focus || writing.writing_focus || writing.writingFocus),
    knowledge: {
      item_ids: normalizeKnowledgeItemIds(rawKnowledgeItemIds, allowedKnowledgeItemIds),   // ← 白名单过滤
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
**白名单强制**（`:537-544`）——AI 编造的 id 直接被丢弃：
```js
function normalizeKnowledgeItemIds(value, allowedKnowledgeItemIds) {
  const source = Array.isArray(value) ? value : [];
  const ids = source.map((id) => String(id || '').trim()).filter(Boolean);
  const filtered = allowedKnowledgeItemIds instanceof Set ? ids.filter((id) => allowedKnowledgeItemIds.has(id)) : ids;
  return [...new Set(filtered)];
}
```
**版本化存取**（`:601-641`）：`createStoredContentPlan` 写 `plan_version`，`normalizeStoredContentPlan` 读时校验版本 + `hasFactSelection` + `validateContentPlan`，任一不过返回 `null`（视为无计划）。

### 13.2 编排提示词原文

`contentGenerationTask.cjs:807-817`（system）：
```js
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
```
表格上限提示（`:799-803`）：
```js
  const tableLimitInstruction = tableRequirement === 'heavy'
    ? '表格需求为"大量"，保持现有编排逻辑；仍然只有明显适合表格的小节才将 table.needed 设为 true。'
    : tableRequirement === 'none'
      ? '表格需求为"不要"，table.needed 必须为 false，table.purpose 留空。'
      : `表格需求为"${tableRequirementLabel}"，table.needed 表示进入表格候选池，不代表最终一定生成；全文表格上限为 ${maxTables || 0} 个，共 ${tableTotalSections || totalSections || 0} 个叶子小节，系统后续会全局择优。`;
```
轻量条目输入（`:821-825`）——**只给 id + 标题 + 简介，不给正文**：
```js
  messages.push({
    role: 'user',
    content: `参考知识库轻量条目（只包含 id、标题和简介，不包含正文；如无合适条目，knowledge.item_ids 返回空数组）：
${renderKnowledgeItemsForPrompt(knowledgeItems)}`,
  });
```
返回格式（`:863-876`）：
```js
JSON 格式：
{
  "writing_focus": "1-2 句话说明本节正文重点展开什么，只聚焦当前章节，不写成正文",
  "knowledge": {
    "item_ids": ["从参考知识库轻量条目中选择的 id；没有合适条目时返回空数组"]
  },
  "facts": {
    "titles": ["从全局事实变量标题清单中选择正文会用到的变量组标题；没有需要引用的变量时返回空数组"]
  },

### 13.3 轻量条目加载与过滤

`loadContentKnowledgeReferences:2100-2134`：
```js
function loadContentKnowledgeReferences(knowledgeBaseService, documentIds, log) {
  if (!documentIds.length) {
    log('本次正文编排未选择参考知识库。');
    return { items: [], contentMap: new Map() };
  }
  if (!knowledgeBaseService?.readReferences) {
    log('未找到知识库读取服务，正文编排不使用知识库。');
    return { items: [], contentMap: new Map() };
  }

  try {
    const references = knowledgeBaseService.readReferences(documentIds);
    const items = [];
    const contentMap = new Map();
    for (const reference of Array.isArray(references) ? references : []) {
      const documentId = String(reference?.document?.id || '').trim();
      for (const item of Array.isArray(reference?.items) ? reference.items : []) {
        const itemId = String(item?.id || '').trim();
        const content = String(item?.content || '').trim();
        if (documentId && itemId && content) contentMap.set(`${documentId}::${itemId}`, { content });
        const title = String(item?.title || '').trim();
        const resume = String(item?.resume || '').trim();
        if (reference?.document?.status === 'success' && documentId && itemId && title && resume) {
          items.push({ id: `${documentId}::${itemId}`, title, resume });   // ← 轻量条目只带 3 字段
        }
      }
    }
    log(items.length ? `正文编排已读取 ${items.length} 条知识库轻量条目。` : '未读取到可用知识库轻量条目，正文编排不使用知识库。');
    if (contentMap.size) log(`正文生成可用知识库正文素材 ${contentMap.size} 条。`);
    return { items, contentMap };
  } catch (error) {
    log(`读取正文编排参考知识库失败，已跳过：${error.message || String(error)}`);
    return { items: [], contentMap: new Map() };
  }
}
```
**两阶段设计**：
- `items`（id + title + resume）→ 给编排 AI 做**选择**
- `contentMap`（`${docId}::${itemId}` → content）→ 给正文 AI 做**取材**

**只有 `status === 'success'` 的文档条目才进入编排**，且必须有 `title` + `resume`（`content` 缺失仍可进 items，只是无法取材）。

### 13.4 取出选中条目的正文

`resolveKnowledgeContents:2136-…`：
```js
function resolveKnowledgeContents(itemIds, knowledgeContentMap) {
  const selected = new Set(normalizeKnowledgeItemIds(itemIds));
  if (!selected.size || !(knowledgeContentMap instanceof Map) || !knowledgeContentMap.size) {
    return [];
  }
```
只对编排选中的 id 取正文——**按需加载，避免把整个知识库塞进上下文**。

### 13.5 正文提示词中的素材使用规则

`contentGenerationTask.cjs:926-935`：
```js
  if (knowledgeContents?.length) {
    messages.push({
      role: 'user',
      content: '参考正文素材使用规则：以下内容只作为可吸收的技术素材。请改写为当前项目语境下的投标技术方案正文，不要照抄，不要提到"知识库""历史文档""参考资料"或素材来源。',
    });
    messages.push({
      role: 'user',
      content: `参考正文素材：\n${formatKnowledgeContentsForPrompt(knowledgeContents)}`,
    });
  }
```
包装格式（`:882-886`）：
```js
function formatKnowledgeContentsForPrompt(contents) {
  return (contents || [])
    .map((content) => `<knowledge_content>\n${String(content || '').trim()}\n</knowledge_content>`)
    .join('\n\n');
}
```
正文还禁止 AI 自行配图（`:906`）：
```js
8. 严禁输出 Mermaid、PlantUML、Graphviz、flowchart、graph、sequenceDiagram 等图表代码块、mermaid.ink 链接或图片 Markdown；配图由系统另行处理。
```
正文写作 system 提示词全文（`:896-914`）：
```js
      content: `你是一个专业的标书编写专家，负责为投标文件的技术标部分生成具体内容。

要求：
1. 内容要专业、准确，与章节标题和描述保持一致。
2. 这是技术方案，不是宣传报告，注意朴实无华，不要假大空。
3. 语言要正式、规范，符合标书写作要求，但不要使用奇怪的连接词，不要让人觉得内容像是 AI 生成的。
4. 内容要详细具体，避免空泛的描述。

### 13.6 另一条 skill 元数据路径：目录侧 `knowledge_item_ids`

可行性报告的目录生成让 AI 在**叶子节点**直接挂 `knowledge_item_ids`（`feasibilityOutlineTask.cjs:76-116`）：
```js
  const knowledgeItemIds = Array.isArray(item.knowledge_item_ids)
    ? [...new Set(item.knowledge_item_ids.map((value) => String(value || '').trim()).filter(Boolean))]
    : [];
  ...
    ...(!children && knowledgeItemIds.length ? { knowledge_item_ids: knowledgeItemIds } : {}),
```
提示词（`:151` / `:169` / `:176` / `:189`）：
```js
  lines.push('- 参考知识库.md：轻量条目清单（id、标题、简介）。叶子节点 knowledge_item_ids 只能从这些 id 中选择。');
```
```js
  4. 只能在叶子节点填写 knowledge_item_ids，只能从参考知识库 id 中选择，可以为空数组。不要输出正文 content，不要编造项目事实。
```
```js
  2. 根对象只有 outline；每项包含 title、description，非叶子项再包含 children；叶子可包含 knowledge_item_ids。
```
```js
    "knowledge_item_ids": ["documentId::itemId"]
```
正文侧选择（`feasibilityReportTasks.cjs:101-121`）——**显式优先，无则字符打分**：
```js
function selectKnowledgeContents(chapter, knowledgeMap) {
  const explicitIds = Array.isArray(chapter.knowledge_item_ids) ? chapter.knowledge_item_ids : [];
  const explicit = explicitIds.map((id) => knowledgeMap.get(id)).filter(Boolean);
  const selected = explicit.length
    ? explicit
    : Array.from(knowledgeMap.values())
      .map((item) => ({ item, score: scoreKnowledge(item, chapter) }))
      .filter((entry) => entry.score > 1)
      .sort((a, b) => b.score - a.score)
      .slice(0, 3)
      .map((entry) => entry.item);
  let used = 0;
  const blocks = [];
  for (const item of selected) {
    const block = `### ${item.title}\n\n${item.content}`.trim();
    if (used + block.length > 24000) break;
    blocks.push(block);
    used += block.length;
  }
  return blocks.join('\n\n');
}
```
打分函数（`:94-99`）——**字符集合交集**（非 RAG 的 fallback）：
```js
function scoreKnowledge(item, chapter) {
  const query = `${chapter.title || ''}${chapter.description || ''}`;
  const target = `${item.title || ''}${item.resume || ''}`;
  const chars = [...new Set(query.replace(/[\s，。；：、（）()《》“”]/g, ''))];
  return chars.reduce((score, char) => score + (target.includes(char) ? 1 : 0), 0);
}
```
**24KB 硬预算**（`:116`）——超出即停。
提示词中的素材规则（`feasibilityReportTasks.cjs:227`）：
```js

# 四、总结：可直接复用的设计模式

| 模式 | 落点 | 要点 |
|---|---|---|
| **唯一命中替换** | `contentGenerationTask.cjs:1584-1616` | 行号通道 + 全文唯一通道；0 次/多次一律拒绝，**不猜** |
| **失败原因回灌** | `:1762-1764`、`:189` | 上轮 errors 拼进下轮 prompt，明确要求「唯一定位」 |
| **归一化在前、校验在后** | `aiService.cjs:782-788` | normalizer 吸收键名漂移，validator 只管业务不变量 |
| **修复而非重跑** | `aiService.cjs:790-800` | 坏内容 + 错误列表回灌；`slice(0, 60000)` |
| **Agent 提名 + 程序拍板** | `contentIllustrationPlanning.cjs:225-305` | 9 条硬校验 + 三重排序 + kind 优先级静默让位 |
| **字节级前缀一致吃缓存** | `knowledgeBaseService.cjs:678-697` + `:839-850` | 统一 pack + 5 秒预热 + 串行首段后并发 |
| **三分类强制覆盖** | `knowledgeBaseService.cjs:802-807` | 每个遗漏 block 必属 matches/new_items/discarded 之一 |
| **顺序无关的归属** | `knowledgeBaseService.cjs:534-535` | 优先级顺序全局认领，不按子批顺序先到先得 |
| **程序级事实守恒** | `feasibilityReportTasks.cjs:59-64` | token 计数减少即整体回退原文 |
| **程序级证据校验** | `rejectionCheckTask.cjs:263-345` | AI 声称的错别字必须在原文定位得到，否则丢弃 |
| **证据索引反证缺失** | `rejectionCheckTask.cjs:1112`、`:1145` | `submittedEvidenceIndex` 出现线索即不得定稿为缺失 |
| **6 轮修复上限** | `rejectionCheckTask.cjs:9-14` | 滚动 patch → 分批定稿 → 全局合稿 |
| **静默让位 vs 硬拒绝** | 配图 `continue` 跳冲突；一致性修复 `throw` | 配图可替代→让位；正文改错→拒绝 |
| **真渲染作为最终判据** | `contentIllustrationGeneration.cjs:164-171` | 正则预筛 + 真出 PNG 才算通过 |
| **结构化线索反证缺失** | `rejectionCheckTask.cjs:99` + `duplicateCheckService.cjs` | 图片不可见 ≠ 材料不存在，看章节/目录/表格/页码线索 |

**三处「与题面描述不符」需注意**：
1. 一致性修复的归一化**只剥行号、不折叠空白**（严格逐字节匹配）；
2. 目录审核返回的是 `status/issues/user_feedback/summary` 四态，**非** `{passed, suggestions}`；
3. 配图数量控制是**全局一次性择优**（`priority` 主排序键 + 目录序兜底），**非分段轮询**。

**源码中未找到**：
- `duplicateCheckService` 内**任何 AI 提示词**（该模块纯算法）；
- 独立的「重复率百分比」指标（只有分文件两两相似度 + 重复句子计数，无单一 rate 字段）。


# 标书语义耦合点清单

> 逐条列出与「标书/投标/废标项/重复率/投标插图风格/可行性报告」耦合的原文位置、原文摘录，以及改写为**专项施工方案语义**时应对应改成什么。

## A. 角色定位与文风（提示词系统级耦合）

### A1. 全文一致性审计角色
- **位置**：`client/electron/services/contentGenerationTask.cjs:1663`
- **原文**：
  ```js
  content: `你是投标技术方案全文一致性审计助手。请审计本组正文是否与给定事实冲突。
  ```
- **专项方案语义**：改为「你是专项施工方案全文一致性审计助手。请审计本组正文是否与给定事实（施工图、勘察报告、危大工程清单、专项方案编制依据）冲突。」

### A2. 一致性修复角色
- **位置**：`contentGenerationTask.cjs:1769`
- **原文**：
  ```js
  content: `你是投标技术方案正文一致性修复助手。请只针对当前小节返回局部精确替换 patch。
  ```
- **专项方案语义**：「你是专项施工方案正文一致性修复助手。」——角色名去「投标」，其余不动。

### A3. 正文写作角色 + 文风（耦合最深的一条）
- **位置**：`contentGenerationTask.cjs:896-903`
- **原文**：
  ```js
  content: `你是一个专业的标书编写专家，负责为投标文件的技术标部分生成具体内容。

  要求：
  1. 内容要专业、准确，与章节标题和描述保持一致。
  2. 这是技术方案，不是宣传报告，注意朴实无华，不要假大空。
  3. 语言要正式、规范，符合标书写作要求，但不要使用奇怪的连接词，不要让人觉得内容像是 AI 生成的。
  ```
- **专项方案语义**：
  > 「你是一个专业的施工方案编制专家，负责为专项施工方案的具体章节生成内容。
  > 1. 内容要专业、准确，与章节标题和描述保持一致。
  > 2. 这是施工组织/专项方案，不是宣传材料或投标文件，注意朴实无华，不要假大空。
  > 3. 语言要正式、规范，符合工程文件写作要求（GB/T 1.1、危大工程管理规定），但不要使用奇怪的连接词，不要让人觉得内容像是 AI 生成的。」
  >
  > **注意第 2 条的隐含含义**：「技术方案」在本仓语境 = 投标技术标；改写时「技术方案」应保持原义（专项施工方案本身就是技术方案），但「不是宣传报告」的对照物要换成「投标文件/宣传材料」。

### A4. 正文编排角色
- **位置**：`contentGenerationTask.cjs:807`
- **原文**：`你是投标技术方案正文编排助手。请根据章节上下文判断本小节最适合的表达方式。`
- **专项方案语义**：「你是专项施工方案正文编排助手。」

### A5. Mermaid / 配图角色
- **位置**：`contentIllustrationGeneration.cjs:121`
- **原文**：`你是投标技术方案 Mermaid 图生成助手。请根据最终正文生成一张${typeLabel}。`
- **专项方案语义**：「你是专项施工方案 Mermaid 图生成助手。」

## B. 标书章节编号语义（Step02 / Step04）

### B1. 事实来源标签
- **位置**：`contentGenerationTask.cjs:1686-1687`
- **原文**：
  ```js
  { role: 'user', content: `Step04 全局事实变量：\n${globalFactsText || '未提供'}` },
  { role: 'user', content: `Step02 关键解析结果（项目信息、甲方信息、交货和服务要求）：\n${bidAnalysisFactsText || '未提供'}` },

### B2. 甲方信息 / 交货和服务要求
- **位置**：`contentGenerationTask.cjs:1687`
- **原文**：`项目信息、甲方信息、交货和服务要求`
- **专项方案语义**：改为 `项目概况、建设单位信息、工程条件与周边环境、既有设施保护要求`（交货/服务是货物采购语境，施工方案无此概念）。

## C. 废标项检查（最强业务耦合）

### C1. 检查项类型二分法
- **位置**：`client/electron/services/rejectionCheckTask.cjs:60-64`
- **原文**：
  ```js
  function normalizeFindingType(value) {
    const raw = String(value || '').trim();
    if (raw === 'invalidBid' || raw.includes('无效')) return 'invalidBid';
    return 'rejectionItem';
  }
  ```
- **专项方案语义**：直接**删除该枚举**。专项方案不是投标文件，不存在「无效标/废标项」。整块 `rejectionCheckTask.cjs` 应替换为**合规性审查**（见 §G 建议映射表）。

### C2. 检查依据来源
- **位置**：`rejectionCheckTask.cjs:77-81`
- **原文**：
  ```js
  content: `【废标项检查输入 v1｜检查项】
  以下内容来自招标文件"无效投标"和"废标项"解析结果。后续任务必须优先基于这些检查口径，不要自行扩大到无法从电子投标文件判断的事项。

  ${input.invalidBidAndRejectionItems}`,
  ```
- **专项方案语义**：
  > `【专项方案合规性审查输入 v1｜检查依据】`
  > `以下内容来自施工图纸、危大工程管理规范、施工组织设计审查清单等解析结果。后续任务必须优先基于这些检查口径，不要自行扩大到无法从方案正文判断的事项。`

### C3. 「纸质/线下事项」排除法（三轮重复 4 次）
- **位置**：`rejectionCheckTask.cjs:88`、`:117`、`:138`、`:162`、`:1074`、`:1113`、`:1147`
- **原文**（`:88`）：
  ```js
  content: `【废标项检查输入 v1｜自定义检查项】
  以下是用户补充的电子投标文件检查关注点。仅在能从电子投标文件正文、目录、附件文本或材料内容中判断时使用；如果涉及签字、盖章、密封、现场递交、纸质正副本等纸质或线下事项，必须忽略。
  ```
  （`:117`）：
  ```js
  2. 明确排除签字、盖章、密封、纸质正副本、现场递交、开标现场授权到场、纸质文件封装等纸质或线下事项。
  ```
- **专项方案语义**：整套「排除纸质/线下」的逻辑**不再成立**（专项方案就是纸面文件，无投标现场动作）。应替换为另一类排除法：
  > 「如果涉及需要现场实测、第三方检测、专家论证会后才能确定的事项，标记为"需现场复核"而不是"不纳入检查"。」
  >
  > 即：把「不可检查」从「纸质/线下」改为「需现场实测/专项检测/专家论证」——危大工程中这三类恰好是真实存在的边界。

### C4. 「非文本内容」容错声明
- **位置**：`rejectionCheckTask.cjs:99`
- **原文**：
  ```js
  重要限制：当前原文由文本解析得到，图片、扫描件、截图、附件页等非文本内容可能已被过滤或无法完整呈现。检查材料缺失时，不得要求必须看到图片内容、扫描件正文或附件正文；如果投标文件中已经出现某项材料的章节标题、目录项、附件标题、材料清单项、表格条目、页码线索、图片占位线索或其他可表明该材料已插入/已提交的结构性文本线索，应视为该材料至少存在提交线索。
  ```
- **专项方案语义**：**这一条几乎可原样复用**，只需替换名词：
  > 「…如果方案中已经出现某项内容的章节标题、计算书引用、图号、表格条目、平面布置图标注、验算引用等结构性文本线索，应视为该内容至少存在编制线索。」
  >
  > 关键词映射：`投标文件`→`方案正文`，`材料/附件`→`计算书/专项方案/施工图`，`已插入/已提交`→`已编制/已引用`。

### C5. 三类风险判定清单
- **位置**：`rejectionCheckTask.cjs:139`
- **原文**：
  ```js
  3. 重点关注实质性条款未响应、必要章节或附件缺失、资格材料明显缺失/过期、报价或关键承诺前后矛盾、技术/商务偏离未说明等电子正文可判断风险。
  ```
- **专项方案语义**：
  > 「3. 重点关注编制依据缺失或失效、专项方案/计算书缺项、危大工程判定结论缺失、专项参数前后矛盾、安全技术交底与方案不一致、施工工艺与现场条件不匹配等正文可判断风险。」

### C6. 错别字/逻辑检查的不一致项清单
- **位置**：`rejectionCheckTask.cjs:224`
- **原文**：
  ```js
  2. 全文前后不一致，包括但不限于处理相同工作的人员名单、设备型号、工期、金额、数量、服务期限、项目名称、技术参数等应高度一致的内容前后不一致。
  ```
- **专项方案语义**：
  > 「2. 全文前后不一致，包括但不限于同一工序的人员分工/持证要求、塔吊与施工升降机型号、流水段划分、层高与结构标高、混凝土强度等级、脚手架与模板支撑体系参数、机械进场数量与台班计划等应高度一致的内容前后不一致。」
  >
  > **删除** `工期、金额、服务期限`（施工方案无报价、无服务期限）；`工期` 可保留为「总工期与关键线路工期」。

---

  ```
  修复提示词中同样出现（`:1797-1798`）：
  ```js

## D. 重复率检测（招标文件 vs 投标文件）

### D1. 字段白名单（业务耦合最深 + 含生产数据泄漏）
- **位置**：`client/electron/services/duplicateCheckService.cjs:1852-1853`
- **原文**：
  ```js
  const tenderFieldDenyPattern = /(供应商名称|供应商地址|法定代表人|供应商代表|授权代表|被授权人|委托代理人|联系人|联系电话|电话|手机|邮政编码|邮箱|电子邮箱|开户|账号|银行|报价|投标报价|投标总价|合同金额|金额|总价)/;
  const tenderFieldAllowPattern = /^(投标日期|日期|项目名称|项目编号|采购人|采购代理机构|评分因素及评标标准页码检索|投标文件总目录|目录|附件\d*|投标书|开标一览表|报价分项一览表|投标产品配置清单|商务要求点对点应答表|技术要求点对点应答表|主要相关业绩一览表|政府采购政策情况表|中小微企业声明函|非残疾人福利性单位声明函)$/;
  ```
- **专项方案语义**：
  > **整段删除或替换**。这是「投标文件元数据查重」，专项方案没有这些字段。
  > 若要保留「与原始资料查重」的能力（如方案是否大段抄施工图说明/其他工程方案），应改为：
  > `方案DenyPattern = /^(施工单位|项目负责人|编制人|审核人|审批人|电话|日期)/`
  > `方案AllowPattern = /^(工程名称|工程编号|建设单位|设计单位|监理单位|施工地点|结构类型|建筑面积|层数|基础形式)/`
  >
  > ⚠️ **另注**：`:1893` 硬编码了三个真实项目名（`天津港保税区消防救援支队|消防装备管理系统项目|天津众信招标咨询有限公司`），这是生产数据泄漏，改写时应**直接删除**。

### D2. 骨架归一的领域量纲
- **位置**：`duplicateCheckService.cjs:1830-1836`
- **原文**：
  ```js
  .replace(/\b[A-Z]{2,}[-A-Z0-9]{4,}\b/gi, '{code}')
  .replace(/\d+(?:\.\d+)?\s*万元/g, '{money}')
  .replace(/\d+(?:\.\d+)?\s*元/g, '{money}')
  .replace(/\d+(?:\.\d+)?\s*%/g, '{percent}')
  .replace(/\d+(?:\.\d+)?\s*分/g, '{score}')
  .replace(/P\s*\d+(?:\s*[-~至]\s*P?\s*\d+)?/gi, '{page}')
  .replace(/\b\d+(?:\.\d+)?\b/g, '{num}');
  ```
- **专项方案语义**：
  > `{money}`/`{score}` 应删除（施工方案无报价、无评分）；`{score}` 的 `\d+分` 实际是**评分标准**，同样删除。
  > 新增施工量纲：`{kpa}`（`\d+\s*(?:kPa|MPa)`）、`{mm}`（钢筋规格 `\d+\s*mm`、`HRB\d{3}`）、`{m3}`、`{kn_per_m2}`（`\d+\s*kN/m²` 均布荷载）、`{grade}`（`C\d{2}` 混凝土强度等级）、`{spec}`（`GB\s*\d+` 标准号）、`{layer}`（`\d+\s*层` 楼栋号）。
  > `{page}` 保留（图纸/规范都是页码引用）。

### D3. 骨架门控关键词
- **位置**：`duplicateCheckService.cjs:1849`
- **原文**：
  ```js
  return /(评分|评标|分值|计分|内容无瑕疵|内容存在|页码检索|合同复印件|技术要求|招标要求)/.test(text);
  ```
- **专项方案语义**：
  > `/(危大|专家论证|超过一定规模|专项方案|计算书|施工图|图号|规范|条文|强制条文)/.test(text)`
  >
  > **注意**：`危大/超过一定规模/专家论证` 是建办质〔2018〕31号的核心术语，替换后这套「短文本骨架放行」规则才能正确识别「本方案是危大工程专项方案」这类标准句式。

### D4. 目录比对的方法论
- **位置**：`duplicateCheckService.cjs:1436`
- **原文**：`const score = Number((pathOverlap * 0.45 + titleOverlap * 0.35 + orderSimilarity * 0.2).toFixed(2));`
- **专项方案语义**：权重本身与业务无关，**可原样复用**（多份方案之间的目录雷同度检测，正是专项方案复用检查的核心需求）。仅需把「文件」概念从「投标文件」改为「专项方案」。

---

  { role: 'user', content: `Step02 关键解析结果（项目信息、甲方信息、交货和服务要求）：\n${bidAnalysisFactsText || '未提供'}` },
  ```
- **专项方案语义**：`Step02` / `Step04` 是**投标文件的评分项章节编号**（对应招标文件的「技术要求」「评分办法」）。应改为：
  > `{ role: 'user', content: `方案依据与工程资料（图纸、勘察报告、专项方案编制依据、危大工程判定结论）：\n${bidAnalysisFactsText}` }`
  >
  > 若保留 `StepXX` 编号体系，则需同步把解析链路的章节编号体系改成施工方案的（如 Step01 工程概况、Step02 编制依据、Step03 施工部署、Step04 专项参数）。


## E. 插图风格

### E1. AI 生图文风约束
- **位置**：`contentIllustrationGeneration.cjs:82`
- **原文**：
  ```js
  不要有太多文字，专业、克制，适合投标技术方案。
  ```
- **专项方案语义**：
  > 「不要有太多文字，专业、克制，适合专项施工方案。」
  >
  > 更贴合的施工图版本：「文字仅保留构件/工序名称与关键尺寸，不要出现装饰性文字；线型与配色应符合制图规范（黑白线条图优先），适合专项施工方案与专家论证材料。」

### E2. AI 生图的适用场景定义
- **位置**：`contentIllustrationPlanning.cjs:10-11`
- **原文**：
  ```js
  engineering_diagram: '专业工程图示：用于展示设备、系统组件、部署位置、连接关系或工程实施场景，强调结构与关系；不用于步骤流转、组织层级或职责分工。',
  realistic_photo: '专业实景图片：用于表现设备、机房、监控中心、施工、巡检或维护现场等可真实拍摄的对象和环境；不用于抽象系统架构、流程或组织关系。',
  ```
- **专项方案语义**：
  > `engineering_diagram` → 保留但改描述：「专业工程图示：用于展示施工机具布置、构件连接、脚手架与支撑体系布置、临建设施或施工部署关系，强调结构与关系；不用于工序流转、组织层级或职责分工。」
  > `realistic_photo` → 「专业实景图片：用于表现施工现场、加工场、材料堆场、样板段、成品保护或安全防护设施等可真实拍摄的对象和环境；不用于抽象体系、流程或组织关系。」
  >
  > **关键变化**：`部署位置/机房/监控中心` 是机房类工程词，施工方案应换成 `施工机具/临建/加工场/样板段`。

### E3. HTML 配图文风
- **位置**：`contentIllustrationGeneration.cjs:106`（Agent 版）、`:93`（文本版）
- **原文**：均含 `不要有太多文字描述，专业商务风格。`
- **专项方案语义**：`不要有太多文字描述，规范严谨风格。`（去掉「商务」，施工方案无商务属性）

### E4. 配图优先级与位置规则
- **位置**：`contentIllustrationPlanning.cjs:161`
- **原文**：
  ```js
  11. 同一小节只允许编排一张图片，包含在html多节图组中，也算该小节已编排，三种图片优先级html>AI生成图片>mermaid，如果一个小节同时适配多种图片，按以上优先级执行。
  ```
  以及 `:156`：
  ```js
  6. AI 图片适合设备、现场、工程空间、实体部署等具象内容；Mermaid 只用于简单流程、层级和职责关系；HTML 用于配置允许的复杂图表类型。
  ```
- **专项方案语义**：
  > `:156` 应改为：「AI 图片适合施工机具、临建设施、材料堆场、加工场、实体构件等具象内容；Mermaid 只用于简单工序流程、施工组织层级和管理责任关系；HTML 用于配置允许的复杂图表类型（如进度网络图、平面布置图、吊装工艺图）。」
  >
  > `:161` 的优先级规则（html>ai>mermaid）**可原样复用**。

---

## F. 目录审核（评分项映射）

### F1. 评分覆盖维度
- **位置**：`outlineGenerationTaskV2.cjs:716`
- **原文**：
  ```js
  - 评分覆盖：直接以技术评分信息.md 为原始依据，逐项检查其中适合技术方案响应的评分大项是否被目录准确覆盖；结构化评分项和目录规划用于核对已确认的映射，但不能掩盖原始评分信息中的遗漏。
  ```
- **专项方案语义**：
  > 「- 依据覆盖：直接以施工图纸、危大工程判定结论、专项方案编制要求、专家论证意见为原始依据，逐项检查其中需要在方案中专门成章的内容是否被目录准确覆盖；结构化检查清单和目录规划用于核对已确认的映射，但不能掩盖原始依据中的遗漏。」

### F2. 专业合理性维度
- **位置**：`outlineGenerationTaskV2.cjs:692`（无评分项模式）
- **原文**：
  ```js
  - 专业合理性：检查目录是否覆盖项目实施所需的通用技术主题，层级、颗粒度、逻辑顺序、标题和内容处理模式是否适合正式投标文件。
  - 事实边界：专业经验只能补充通用目录结构，不得编造具体项目事实、参数、业绩或承诺。
  ```
- **专项方案语义**：
  > 「- 专业合理性：检查目录是否覆盖本工程危大/超危大工程所需的专项技术主题（地基基础、主体结构、脚手架、模板支撑、起重吊装、拆除、基坑、幕墙等），层级、颗粒度、逻辑顺序、标题和内容处理模式是否适合正式专项施工方案。
  > - 事实边界：专业经验只能补充通用目录结构，不得编造具体项目参数、业绩或承诺。」

### F3. 评分项 branch_id 绑定
- **位置**：`outlineGenerationTaskV2.cjs:730`
- **原文**：
  ```js
  10. 技术一级目录必须保留 ${SCORE_DIRECTORY_PLAN_FILE} 中对应的 branch_id；调整一级目录顺序或编号时不得修改 branch_id。
  ```
- **专项方案语义**：`branch_id` 的语义是「技术评分大项」。专项方案应改为「**危大工程类别**」或「**专项方案类别**」：
  > 「专项一级目录必须保留 ${PLAN_FILE} 中对应的 category_id（即危大工程六大类之一）；调整一级目录顺序或编号时不得修改 category_id。」

### F4. 技术评分项缺项专判
- **位置**：`outlineGenerationTaskV2.cjs:736-740`
- **原文**：
  ```js
  // 按解析协议识别整项缺失或仅技术评分项缺失；与 Renderer 的同名判断保持一致。
  function isMissingTechnicalScoreItems(content) {
    const text = String(content || '').trim();
    if (text === '未提取到') return true;
    const section = text.match(/^##[\t ]+技术评分项[\t ]*\r?\n([\s\S]*?)(?=^#{1,2}[\t ]|$(?![\s\S]))/m);
    return section?.[1].trim() === '没有提及';
  }
  ```
- **专项方案语义**：锚点 `## 技术评分项` 改为 `## 危大工程判定结论`（或 `## 专项方案清单`），判定语义（`未提取到` / `没有提及` 两态）**原样保留**。

### F5. content_mode 业务枚举
- **位置**：`outlineGenerationTaskV2.cjs:663`
- **原文**：
  ```js
  3. 只通过合理调整 ai-generate 叶子的目录结构满足数量目标，不得为了凑数把 template-fill、point-to-point 或 other 改成 ai-generate，也不得改变非 AI 叶子的处理模式。
  ```
- **专项方案语义**：`ai-generate` / `template-fill` / `point-to-point` 是投标文件专属处理模式（AI 生成 / 模板填充 / 逐条应答）。专项方案应改为：
  > `ai-generate`（AI 撰写）/ `template-fill`（引用既有方案模板）/ `copy-from-standard`（引用规范条文）/ `calc-required`（必须含计算书）。

---


**两条路径对比**：
| | 技术方案（标书） | 可行性报告 |

## G. 可行性报告语义耦合

### G1. 角色与数据源
- **位置**：`feasibilityReportPrompts.cjs:238`、`:302`、`:283`
- **原文**：
  ```js
  '你是严谨的中国建设项目可行性研究资料分析专家。只能基于项目参数和用户资料提取事实；不得编造金额、规模、地点、期限、政策名称或技术参数。',
  ```
  ```js
  '你是专业的可行性研究报告编制专家。正文必须基于用户提供的项目事实和资料，论证清晰、语言正式。不得编造金额、规模、地点、期限、批复、政策名称、设备参数或财务指标。',
  ```
  ```js
  '你是可行性研究报告总编。请基于项目实际资料，在通用大纲框架内形成完整、可执行、可编辑的三级以内报告目录。',
  ```
- **专项方案语义**：
  > 这三个角色**本身与「投标」无关**（可研 ≠ 投标），是本仓中业务耦合最轻的一条线。
  > 唯一需改的是「建设项目可行性研究」→ 「**专项施工方案可实施性论证**」：
  > 「你是严谨的施工专项方案可实施性论证专家。只能基于图纸、勘察报告与现场条件提取事实；不得编造结构参数、机具型号、工期承诺、验收标准或计算结果。」
  >
  > **注意**：`不得编造金额…财务指标` 这条约束可**原样保留**（施工方案同样不得编造造价数据）。

### G2. 章节大纲（7 大模板）
- **位置**：`feasibilityReportPrompts.cjs:2-28`（government 模板节选）
- **原文**：
  ```js
    chapters: [
      '概述',
      '项目建设背景和必要性',
      '项目需求分析与产出方案',
      '项目选址与要素保障',
      '项目建设方案',
      '项目运营方案',
      '项目投融资与财务方案',
      '项目影响效果分析',
      '项目风险管控方案',
      '研究结论及建议',
    ],
  ```
- **专项方案语义**：整套 7 个模板（`government`/`enterprise`/`industrial`/`hi_tech`/`infrastructure`/`eco_environmental`/`commercial_realestate`）都是**投资项目**语境，与施工方案无关。建议：
  > 改按**危大工程六大类**（建办质〔2018〕31号）组织 6 套方案模板：
  > `foundation`（地基基础）、`structure`（主体结构）、`scaffold`（脚手架）、`formwork`（模板支撑体系）、`lifting`（起重吊装）、`demolition`（拆除工程），另加 `excavation`（基坑工程）、`curtain_wall`（幕墙）。
  >
  > 通用章节骨架可沿用：`编制依据 / 工程概况 / 施工部署 / 施工工艺 / 质量保证 / 安全保证 / 环保与文明施工 / 应急处置 / 验收要求`。

### G3. 插图指引格式
- **位置**：`feasibilityReportPrompts.cjs:312-314`
- **原文**：
  ```js
  6. 若当前章节涉及项目选址与建设条件、总图布置、工艺流程、环保设施或实施进度等工程技术/选址章节，请在最佳位置嵌入且仅嵌入 1 处插图指引框，格式固定为：
  > 📸 **【插图指引】：图片名称**
  > *说明：此处请插入...*
  ```
- **专项方案语义**：
  > 「6. 若当前章节涉及施工平面布置、工艺流程、吊装工艺、脚手架/支撑体系布置、临建设施或实施进度等章节，请在最佳位置嵌入且仅嵌入 1 处插图指引框，格式固定为：…」
  >
  > `项目选址` 应删除（施工方案无选址章节），改为 `施工平面布置`；`总图布置`→`施工总平面布置`。

---


## H. 知识库的投标语境

### H1. 知识条目提取目标
- **位置**：`knowledgeBaseService.cjs:723-724`
- **原文**：
  ```js
  '你是投标资料知识库分析助手。你只负责从历史投标资料中提取对后续编写标书有复用价值的知识条目。',
  '任务：基于上文已给出的本段 block，提取有意义的知识条目数组。条目应覆盖技术方案、项目管理、质量、安全、进度、服务、应急、人员设备、类似业绩等可复用内容。',
  ```
- **专项方案语义**：
  > 「你是专项方案知识库分析助手。你只负责从历史专项方案、施工组织设计、专项施工方案中提取对后续编制施工方案有复用价值的知识条目。」
  > 「任务：…提取有意义的知识条目数组。条目应覆盖施工工艺、施工部署、质量保证、安全保证、进度计划、环保与文明施工、应急处置、人员机具配备、计算书方法、类似工程经验等可复用内容。」
  >
  > **删除** `服务`、`类似业绩`（施工方案无商务业绩章节；若是「类似工程经验」可保留但需改名为「类似工程做法」）。
  > **新增** `计算书方法`（专项方案的灵魂，独有项）。

### H2. 补漏/匹配角色
- **位置**：`knowledgeBaseService.cjs:745`、`:772`、`:802`
- **原文**：
  ```js
  '你是投标资料知识库补漏助手。你只判断已有知识条目是否遗漏了重要主题，并补充缺失条目。',
  '你是投标知识库段落匹配助手。你只根据知识条目的标题和摘要，为其匹配强相关 block 范围。',
  '你是投标知识库遗漏段落补漏助手。必须把上文收到的遗漏 block 明确归入已有条目、新增条目或舍弃段落。',
  ```
- **专项方案语义**：三个角色名的「投标」全部去掉即可（`投标资料知识库`→`专项方案知识库`），其余逻辑不动。

### H3. 补漏中的复用价值判据
- **位置**：`knowledgeBaseService.cjs:806`
- **原文**：`'3. discarded：如果内容质量低、重复、格式残留或无投标复用价值，则推荐舍弃，并给出 reason。',`
- **专项方案语义**：`无投标复用价值` → `**无方案复用价值**`（或更准确：`与本工程无关的通用样板文字`）。

---

## I. 建议的整体映射表

| 易标（投标语义） | 专项方案工具箱（建议） | 依据 |
|---|---|---|
| 废标项检查（`rejectionCheckTask.cjs`） | **危大工程预检 + 合规性审查** | 排除法定（废标）与补充法定（危大/专家论证） |
| `invalidBid` / `rejectionItem` | `hazardCategory`（六大类）/ `complianceItem`（强制性条文） | 建办质〔2018〕31号 |
| 重复率检测（投标文件互查重） | **专项方案雷同度检测**（多方案间） | 治理「一套方案抄多家」 |
| Step02 招标文件解析 / Step04 全局事实 | **施工图解析 / 专项参数（危大判定结论）** | 数据源换成施工图与勘察报告 |
| 投标插图风格（专业商务） | **施工图规范风格** | `AI_IMAGE_TYPE_DESCRIPTIONS` 换词 |
| 可行性研究报告（7 套投资大纲） | **专项施工方案可实施性论证**（按危大六大类组织） | 本仓已有独立任务线，改造成本低 |
| `technical-plan`（投标技术标） | `special-construction-plan`（专项施工方案） | 全仓命名替换 |
| 评分大项 `branch_id` | **危大工程类别 `category_id`** | 六大类分类体系 |
| `ai-generate`/`template-fill`/`point-to-point` | `ai-generate`/`template-fill`/`copy-from-standard`/`calc-required` | 去掉「逐条应答」，加「计算书」 |

---

## J. 「结构性文本线索」方法论（可直接复用，无需改写）

这一套在两个模块独立实现，是本仓最值得迁移到专项方案的方法论：

- **位置 A**：`rejectionCheckTask.cjs:119`（第 1 轮分析第 4 条）
- **原文**：
  ```js
  4. 判断材料是否缺失时，先识别章节标题、目录项、附件标题、材料清单项、表格条目、页码线索、图片占位线索等结构性文本线索；只要存在这类线索，就不能因为图片或扫描件正文不可见而判定缺失。
  ```
- **位置 B**：`duplicateCheckService.cjs:1863-1894`（`buildOutlineItems` 用招标文件句子白名单判定 `from_tender`）
- **专项方案语义**：**完全适用**。施工方案被解析成 Markdown 后，图纸/计算书/照片同样只剩占位符，靠「章节标题、表格条目、图号、页码引用」即可判断内容存在与否。建议把线索类型扩充为：
  > `章节标题、目录项、附件标题、材料/机具清单项、表格条目、图号、计算书编号、规范条文引用、页码线索、图片占位线索`

---

## K. 与本仓（专项方案工具箱）已有能力的对应关系

> 供迁移排期参考；本仓对应项已按 `docs/reference_gap_analysis_20260930.md` 的口径统计。

| 易标能力 | 本仓对应 | 差距 |
|---|---|---|
| 一致性审计 + 定点替换 | `services/consistency_scanner.py` + `consistency_edits.py` | ✅ 已有（`AGENTS.md §4.20.3` G2） |
| 技术评分要求提取 | `ANALYSIS_ITEMS.techScoring` | ✅ 已有（同上 G1） |
| 废标项检查 | `routers/compliance.py` | ⚠️ 有「符合性/专家论证预检/就绪度」，但**无滚动分段 + 4 桶状态机** |
| 重复率检测 | ❌ 无 | P1 缺口：建议先落「危大参数雷同度」小目标 |
| Mermaid 三处链路 | `services/mermaid_*.py` + `localImageRenderService` 对应物 | ✅ 已有（`AGENTS.md §4.3`） |
| 配图 Agent 提名 + 程序拍板 | `routers/charts.py` | ✅ 已有（`§4.3` 三侧口径） |
| 知识库非 RAG | `routers/knowledge.py`（CRUD 已有） | ⚠️ 只有存储，**构建流水线缺失**（P1） |
| 可行性报告 | ❌ 无 | P2：按危大六大类重做后价值有限，可不迁移 |

---

*报告完。全部 file:line 均基于对 `J:\编程\OpenBidKit 易标\OpenBidKit_Yibiao-main-2026-09-14` 的只读实测读取，未修改该仓库任何文件。*

|---|---|---|
| 挂载位置 | `contentGenerationPlans[sectionId].plan.knowledge.item_ids` | 目录叶子 `item.knowledge_item_ids` |
| 决策者 | 章节级 `content plan` AI | 目录生成 AI |
| fallback | 无（不选就不给） | 字符打分 top3 |
| 预算 | 无显式（受上下文约束） | 24000 字 |

---

5. 围绕当前章节标题、描述和正文编排重点展开，保持内容聚焦。
6. ${tableAllowed ? '可以使用 Markdown 段落、列表和表格；表格必须服务于内容表达，不要为了形式硬插。' : '只能使用 Markdown 段落、普通列表和加粗引导语，严禁输出 Markdown 表格或 HTML 表格。'}
7. ${tableAllowed ? '正文只生成文字、列表、表格等内容，配图由系统另行处理。' : '正文只生成文字和普通列表，配图由系统另行处理。'}
8. 严禁输出 Mermaid、PlantUML、Graphviz、flowchart、graph、sequenceDiagram 等图表代码块、mermaid.ink 链接或图片 Markdown；配图由系统另行处理。
...
16. 仅使用本章节提供的全局事实变量；未提供时不要主动编造具体人员、周期、质保、品牌、型号等会影响全文一致性的承诺。${buildContentFactCompletenessInstruction(globalFactsMode) ? `\n\n${buildContentFactCompletenessInstruction(globalFactsMode)}` : ''}`,
```

---

  "table": {
    "needed": true,
    "purpose": "说明表格在本小节中要表达什么；不需要表格时留空"
  }
}
```

---

      content_chars INTEGER NOT NULL DEFAULT 0,
      is_filtered INTEGER NOT NULL DEFAULT 0,
      filter_reason TEXT,              -- 7 类 reason
      sort_order INTEGER NOT NULL DEFAULT 0,
      FOREIGN KEY (document_id) REFERENCES knowledge_documents(document_id) ON DELETE CASCADE,
      UNIQUE(document_id, block_id, is_filtered)
    );
```
`is_filtered=1` 的行**就是被清理掉的 block**（带 `filter_reason`），不物理删除——**可回溯**。

