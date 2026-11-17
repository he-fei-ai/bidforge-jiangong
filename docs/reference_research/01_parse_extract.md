# 「解析提取模块」源码考古报告

**参考仓库**：`J:\编程\OpenBidKit 易标\OpenBidKit_Yibiao-main-2026-09-14`（Electron + React/TS）
**模块范围**：文件解析 → 标段检测 → 18 项结构化提取 → 校验/修复/落库
**分析角色**：代码考古专家（**只读**，未修改参考仓库任何文件）
**说明**：本报告所有结论均来自实际源码，行号为文件内 1-based 行号。**未在源码中找到的，标注「源码中未找到」。**

---

## 0. 文件清单与实际行数

| 文件 | 总行数 | 角色 |
|---|---|---|
| `client/electron/services/bidAnalysisTask.cjs` | 472 | 18 项提取任务定义 + 编排（Main 侧唯一真源） |
| `client/src/features/technical-plan/services/bidAnalysisWorkflow.ts` | 322 | 18 项任务定义的**渲染进程副本**（字段名 `buildTaskPrompt`） |
| `client/electron/services/bidSectionExtractionTask.cjs` | 270 | 多标段按行号提取 + AI 合并 |
| `client/electron/utils/bidSectionDetector.cjs` | 160 | 纯正则标段检测（快速判定，不产最终列表） |
| `client/electron/utils/bidSectionContext.cjs` | 37 | 标段上下文 hint 文本生成 |
| `client/electron/utils/userTextSplitter.cjs` | 215 | 均分分段器 |
| `client/electron/utils/segmentedAiResultMerger.cjs` | 69 | 分段结果 AI 合稿 |
| `client/electron/services/aiService.cjs` | 2600+ | AI 封装：`chat` / `requestJson` / `collectJsonResponse` / 流式 |
| `client/electron/utils/aiRetry.cjs` | 223 | 重试判定 + 退避 |
| `client/electron/utils/aiRequestQueue.cjs` | 232 | 并发队列 + scope 暂停 |
| `client/electron/services/fileService.cjs` | 716 | 文件导入 + 解析器选择 + 图片资源 |
| `client/electron/services/doc2markdown/convert.mjs` | 1714 | 本地格式 → Markdown |
| `client/electron/services/technicalPlanStore.cjs` | 2720+ | 落盘、标段工作副本、任务结果存储 |
| `client/electron/services/taskService.cjs` | 1676 | 后台任务托管（并发/暂停/恢复） |

> ⚠️ **角色提示中提到的 `bidSectionDetection*` / `bidSectionDetector*`**：实际只有 `bidSectionDetector.cjs` + `bidSectionContext.cjs` + `bidSectionExtractionTask.cjs` 三个文件，**不存在 `bidSectionDetectionTask.cjs` 或 `bidSectionDetector*` 目录**（`Get-ChildItem -Recurse -Filter 'bidSection*'` 已确认为 3 个文件）。

## 1. 文件 → 文本的完整链路

### 1.1 入口与解析器选择

`fileService.cjs:53-83` —— 按 `components.file_parser.provider` 选解析器，**格式不支持时回退本地**：

```js
// client/electron/services/fileService.cjs:70
function resolveFileParser(config, filePath) {
  const requestedProvider = config.components?.file_parser?.provider || 'local';
  const ext = path.extname(filePath).toLowerCase();
  const requestedSupported = getSupportedExtensions(requestedProvider).has(ext);
  if (requestedSupported) {
    return { provider: requestedProvider, requestedProvider, ext, supported: true, fallbackToLocal: false };
  }
  if (requestedProvider !== 'local' && localSupportedExtensions.has(ext)) {
    return { provider: 'local', requestedProvider, ext, supported: true, fallbackToLocal: true };
  }
  return { provider: requestedProvider, requestedProvider, ext, supported: false, fallbackToLocal: false };
}
```

### 1.2 支持的格式（三套白名单，互不相同）

```js
// client/electron/services/fileService.cjs:16-23
const localSupportedExtensions = new Set(['.txt', '.md', '.markdown', '.docx', '.pdf', '.doc', '.wps', '.xls', '.xlsx']);
const mineruAgentSupportedExtensions = new Set([
  '.pdf', '.doc', '.docx', '.ppt', '.pptx', '.png', '.jpg', '.jpeg', '.jp2', '.webp', '.gif', '.bmp',
]);
const mineruAccurateSupportedExtensions = new Set([
  '.pdf', '.doc', '.docx', '.ppt', '.pptx', '.png', '.jpg', '.jpeg', '.jp2', '.webp', '.gif', '.bmp', '.html',
]);
const duplicateCheckSupportedExtensions = new Set(['.doc', '.docx', '.wps', '.pdf', '.md', '.markdown', '.xls', '.xlsx']);
```

> **注意**：`.html` 只被 MinerU 精准 API 支持，**本地解析不支持 `.html`**（`convert.mjs` 无该分支）。`selectable` 是二者的并集（`fileService.cjs:63-68`），所以**可选 ≠ 一定能解析**，解析器层会再判一次 `parser.supported`（`:529-537`）。

### 1.3 魔数优先的格式识别

```js
// client/electron/services/doc2markdown/convert.mjs:134-166
export async function detectFileFormat(inputPath) {
  const suffix = path.extname(inputPath).toLowerCase();
  const header = await readFileHeader(inputPath, 8);

  if (SPREADSHEET_SUFFIXES.has(suffix)) return 'spreadsheet';   // ← 唯一按后缀优先
  if (isPdfHeader(header))  return 'pdf';        // %PDF-
  if (isZipHeader(header))  return 'docx';       // PK\x03\x04
  if (isOleCompoundHeader(header)) return 'legacy_word';  // D0CF11E0
  if (MARKDOWN_SUFFIXES.has(suffix)) return 'markdown';
  if (TEXT_SUFFIXES.has(suffix))      return 'text';
  if (PDF_SUFFIXES.has(suffix))       return 'pdf';
  if (DOCX_SUFFIXES.has(suffix))      return 'docx';
  if (LEGACY_WORD_SUFFIXES.has(suffix)) return 'legacy_word';
  return 'unknown';
}
```

魔数常量在 `convert.mjs:26-28`；`convertPathToMarkdown` 分发在 `:106-132`，不认识的格式抛 `ConversionError('unsupported_format', '不支持的文件格式')`。

### 1.4 各格式用的库

| 格式 | 库 | 代码位置 |
|---|---|---|
| `.md/.markdown/.txt` | `chardet` + `iconv-lite` | `convert.mjs:185-198` |
| `.xlsx/.xls` | `xlsx`（SheetJS） | `convert.mjs:200-224, 226-234` |
| `.docx` | `mammoth` → HTML → `cheerio` → `turndown` + `turndown-plugin-gfm` | `convert.mjs:487-497, 545-553` |
| `.pdf` | `pdf-parse`（`PDFParse`）文本+表格+图片；`pdfjs-dist/legacy` 二次抽表 | `convert.mjs:574-595, 605-628` |
| `.doc/.wps` | LibreOffice CLI / WPS COM / Word COM → docx → 走 docx 链路 | `convert.mjs:1161-1216, 1340-1342` |
| 图片（仅 MinerU） | 由远端返回 | — |

依赖 import 见 `convert.mjs:9-18`。

### 1.5 表格如何标记

**三条互不相同的表格策略**，是本模块最值得注意的分叉：

**(a) Excel —— 强制转 Markdown 表格，按 200 行分块**

```js
// client/electron/services/doc2markdown/convert.mjs:449-467
function renderSpreadsheetTables(header, bodyRows) {
  const totalChunks = Math.max(1, Math.ceil(bodyRows.length / SPREADSHEET_TABLE_CHUNK_ROWS)); // 200
  const chunks = [];
  for (let index = 0; index < totalChunks; index += 1) {
    const chunkRows = bodyRows.slice(index * SPREADSHEET_TABLE_CHUNK_ROWS, (index + 1) * SPREADSHEET_TABLE_CHUNK_ROWS);
    const lines = [];
    if (totalChunks > 1) {
      lines.push(`### 表格分段 ${index + 1}/${totalChunks}`);
      lines.push('');
    }
    lines.push(renderMarkdownTableRow(header));
    lines.push(renderMarkdownTableRow(Array(header.length).fill('---')));
    for (const row of chunkRows) {
      lines.push(renderMarkdownTableRow(row));
    }
    chunks.push(lines.join('\n'));
  }
  return chunks.join('\n\n');
}
```

表头靠启发式猜（`findSpreadsheetHeaderIndex` 只扫前 20 行、按「去重后取值个数 ≥2」取最优行，`:377-389`）；猜不到就 `列1/列2/...`（`:445-447`）。表头前的行被当说明渲染成 `> 表格说明：...`（`:364, 370-372`）。合并单元格**向下向右填充**（`fillSpreadsheetMergedCells`，`:304-327`）。单元格内换行转 `<br>`、竖线转义 `\|`（`:293-302`）。

**(b) DOCX —— 占位符保护，表格原样保留 HTML**

```js
// client/electron/services/doc2markdown/convert.mjs:555-572
function preserveTables(html) {
  const $ = cheerio.load(html, { decodeEntities: false });
  const placeholders = new Map();
  $('table').each((index, element) => {
    const placeholder = `TABLEPLACEHOLDER${String(index + 1).padStart(4, '0')}`;
    placeholders.set(placeholder, $.html(element));
    $(element).replaceWith(placeholder);
  });
  return { html: $.root().html() || '', placeholders };
}
function restoreTables(markdown, placeholders) {
  let restored = markdown;
  for (const [placeholder, tableHtml] of placeholders.entries()) {
    restored = restored.replaceAll(placeholder, `\n\n${tableHtml}\n\n`);
  }
  return restored;
}
```

→ **DOCX 表格在最终 Markdown 里是 HTML `<table>`，不是 Markdown 表格**。调用顺序 `convertDocxFile:493-495`：先摘出表格 → 转 Markdown → 再把 HTML 塞回。

**(c) PDF —— 双引擎抽表 + 去重 + 正文去重**

```js
// client/electron/services/doc2markdown/convert.mjs:1005-1040（节选）
const tables = [
  ...(tablePages[index]?.tables || []),        // pdf-parse
  ...(pdfJsTablePages[index]?.tables || []),   // pdfjs-dist 抽线框重建
];
const tableMarkdownList = [];
for (const table of tables) {
  const tableMarkdown = renderMarkdownTable(table);
  if (tableMarkdown && !hasSimilarPdfTable(tableMarkdownList, tableMarkdown)) {
    tableMarkdownList.push(tableMarkdown);
  }
}
const dedupedText = removePdfTableDuplicateText(pageText, tableMarkdownList);
```

- `hasSimilarPdfTable`（`:1075-1087`）按紧凑化文本相似度去重；
- `removePdfTableDuplicateText`（`:1048-1073`）把**已被表格覆盖的正文行删掉**，避免同一内容出现两遍；
- 单元格 `<br>` 转义在 `renderPdfJsCellText:993`；
- **表格与正文都按页合并，页与页之间用 `\n\n`，没有任何页码标记**（`parts.join('\n\n')`，`:1045`）—— 这直接导致后面「行号 → 原文」的映射只能落在「转换后 Markdown 的行」上，而非原始 PDF 页。

### 1.6 PDF 表格的坐标系重建（含一个已修复的踩坑注释）

```js
// client/electron/services/doc2markdown/convert.mjs:37-41
// pdf.js 的 draw-ops 操作码（worker 把路径序列化成扁平数字数组时的编码，
// 见 pdfjs-dist 的 makePathFromDrawOPS）。⚠️ 3 是 quadraticCurveTo（吃 4 个坐标）、
// 4 才是 closePath（0 个坐标）—— 旧实现把 3 当 closePath，导致一旦路径里
// 出现二次曲线，后续所有线段全部错位解析。
const PDF_DRAW_OPS = { moveTo: 0, lineTo: 1, curveTo: 2, quadraticCurveTo: 3, closePath: 4 };
```

### 1.7 资源释放

| 资源 | 释放方式 | 位置 |
|---|---|---|
| `PDFParse` 实例 | `finally { await parser.destroy(); }` | `convert.mjs:592-594` |
| pdfjs `document` | `finally { await document.destroy(); }` | `convert.mjs:625-627` |
| LibreOffice profile 目录 | `finally { await rm(profileDir, {recursive:true, force:true}); }` | `convert.mjs:1506-1508` |
| `.doc/.wps` 临时目录 | `finally { await rm(tempDir, {recursive:true, force:true}); }` | `convert.mjs:1191-1193` |
| 子进程 | 超时 `child.kill('SIGTERM')`；stdout/stderr 全量收集 | `convert.mjs:1511-1534` |
| Word/WPS COM 对象 | PowerShell 脚本内 `Release-ComObject` + `GC.Collect/WaitForPendingFinalizers` | `convert.mjs:45-90` |
| 导入图片目录 | 解析失败时 `deleteImportedImageAssets(assets)` | `fileService.cjs:556` |

COM 释放是内嵌脚本逐行写死的（`:52-56`, `:81-88`）：

```
'  if ($null -ne $doc) { try { $doc.Close($false) } catch {} }',
'  if ($null -ne $app) { try { $app.Quit() } catch {} }',
'  Release-ComObject $doc',
'  Release-ComObject $app',
'  [GC]::Collect()',
'  [GC]::WaitForPendingFinalizers()',
```

### 1.8 分段/分页策略汇总

- **PDF 分页**：按 `pageCount` 逐页 `parts.push(pageParts.join('\n\n'))`，**不写页码**（`convert.mjs:1005-1045`）。
- **PDF 空白页**：无 `pageParts` 的页直接跳过（`:1037`），即**页码不连续**。
- **PDF 文本层缺失**：抛 `ConversionError('pdf_text_layer_missing', 'PDF 未检测到可选中文字层')`（`:585-589`），判定函数 `hasInformativeText`（`:1615`）。
- **Excel 分页**：200 行一块 + `### 表格分段 i/n` 标题（见 1.5a）。
- **文本分段（送 AI 前）**：`userTextSplitter`，详见第 6 节。

### 1.9 远端解析（MinerU）——轮询与超时

```js
// client/electron/services/fileService.cjs:171-175
async function pollMineruAgent(taskId, fileName) {
  const startedAt = Date.now();
  const timeoutMs = 300000;   // 5 分钟
  const intervalMs = 3000;
```

精准 API：`timeoutMs = 600000`（10 分钟）、`intervalMs = 5000`（`:238-241`）。两者都是**裸 while 轮询，无指数退避**；进度用 `console.log('WAIT ...')` 输出（`:190`, `:260`）—— 不进 AI 日志体系。

MinerU 精准返回 zip，**优先取 `full.md`，否则任意 `.md`**：

```js
// client/electron/services/fileService.cjs:294-299
const fullMd = entries.find((entry) => /(^|[/\\])full\.md$/i.test(entry.entryName));
const anyMd = entries.find((entry) => entry.entryName.toLowerCase().endsWith('.md'));
const target = fullMd || anyMd;
if (!target) {
  throw new Error('MinerU 精准解析结果 zip 中未找到 Markdown 文件');
}
```

## 2. AI 调用封装

### 2.1 对外签名（service 对象）

```js
// client/electron/services/aiService.cjs:2466-2501（节选）
const service = {
  getConfig() {
    return configStore.load();
  },

  async chat(request) {
    return enqueueTextRequest(request, () => {
      const config = configStore.load();
      return chatWithConfig(app, config, request);
    }, { signal: request?.signal });
  },

  async requestJson(request) {
    return enqueueTextRequest(request, () => {
      const config = configStore.load();
      return collectJsonResponseWithConfig(app, config, request);
    }, { signal: request?.signal });
  },

  async collectJsonResponse(request) {
    return enqueueTextRequest(request, () => {
      const config = configStore.load();
      return collectJsonResponseWithConfig(app, config, request);
    }, { signal: request?.signal });
  },
  ...
```

**`requestJson` 与 `collectJsonResponse` 是同一个函数的两个别名**（`:2489` 与 `:2496` 都调 `collectJsonResponseWithConfig`）。这也解释了三处调用方的「二选一」写法：

```js
// client/electron/services/bidSectionExtractionTask.cjs:189-197
async function collectJson(aiService, options) {
  if (aiService?.collectJsonResponse) {
    return aiService.collectJsonResponse(options);
  }
  if (aiService?.requestJson) {
    return aiService.requestJson(options);
  }
  throw new Error('AI 服务尚未初始化');
}
```

同样的兼容写法见 `globalFactsTask.cjs:678`、`rejectionCheckTask.cjs:1315`。

前端类型契约（**不含 `collectJsonResponse`，只暴露 `requestJson`**）：

```ts
// client/src/shared/types/ipc.ts:606
requestJson: <TResult = unknown>(request: JsonCompletionRequest) => Promise<TResult>;
```

### 2.2 并发控制（三层）

**第 1 层：两个独立队列**

```js
// client/electron/services/aiService.cjs:2423-2435
const textRequestQueue = createAiRequestQueue({
  defaultLimit: 10,
  getLimit() {
    return configStore.load()?.concurrency_limit;
  },
});
const imageRequestQueue = createAiRequestQueue({
  defaultLimit: 2,
  getLimit() {
    return configStore.load()?.image_model?.concurrency_limit;
  },
});
```

默认值来自 `configStore.cjs:10-11`：`DEFAULT_TEXT_CONTEXT_LENGTH_LIMIT = 400000`、`DEFAULT_TEXT_CONCURRENCY_LIMIT = 10`；`request_mode` 默认 `'stream'`（`configStore.cjs:48`）。

**第 2 层：任务级 scope（暂停/取消）**。每个后台任务启动时生成 `queueScopeId = ${type}:${task.task_id}`（`taskService.cjs:684`），并注入 `signal`：

```js
// client/electron/services/taskService.cjs:876
const runnerAiService = aiService?.withQueueScope ? aiService.withQueueScope(queueScopeId, taskControl.signal) : aiService;
```

`withQueueScope` 返回一个**代理 service 对象**，把 scopeId/signal 注入到每个请求（`aiService.cjs:2539-2561`）。暂停时 `pauseQueueScope` 会**丢弃队列中与重试中的同 scope 任务**（`aiRequestQueue.cjs:166-200`，共 2 处 `settleJob(reject)`），错误码 `AI_QUEUE_SCOPE_PAUSED`。

**第 3 层：业务编排并发**。18 项提取是 `Promise.all(remainingTasks.map(runOneSafely))`（`bidAnalysisTask.cjs:450`）——**没有自己的并发上限**，全靠第 1 层的 10 并发兜底。分段内部也是 `Promise.all(segments.map(...))`（`bidAnalysisTask.cjs:237`）。→ **18 项 × N 段可能瞬间产生 18×N 个排队任务**，实际并发被队列限制为 10。

### 2.3 重试次数与退避（**两层，语义不同**）

**传输层重试**（`aiRetry.cjs`）：

```js
// client/electron/utils/aiRetry.cjs:1-4
const AI_REQUEST_MAX_ATTEMPTS = 3;
const AI_RETRY_DELAY_MS_BY_FAILED_ATTEMPT = [3000, 5000];

const RETRYABLE_HTTP_STATUS_CODES = new Set([408, 429]);
```

可重试判定 `isRetryableAiRequestError`（`:119-142`）：

```js
function isRetryableAiRequestError(error) {
  if (!error || error?.code === 'AI_QUEUE_SCOPE_PAUSED') return false;   // 主动暂停不重试
  if (error.aiRequestRetryable === false) return false;
  if (error.aiRequestRetryable === true)  return true;
  const status = getErrorStatus(error);
  if (status) return isRetryableHttpStatus(status);   // 408/429/5xx
  if (isAbortLikeError(error)) return true;           // AbortError/TimeoutError
  return hasRetryableNetworkCode(error) || isFetchNetworkError(error);
}
```

退避是**固定两档 `[3000, 5000]`，没有抖动、没有指数**：

```js
// client/electron/utils/aiRetry.cjs:144-149
function getAiRetryDelayMs(failedAttempt) {
  const attempt = Math.max(1, Number(failedAttempt) || 1);
  return AI_RETRY_DELAY_MS_BY_FAILED_ATTEMPT[
    Math.min(attempt, AI_RETRY_DELAY_MS_BY_FAILED_ATTEMPT.length) - 1
  ];
}
```

错误链递归遍历（含 `error.errors` 数组与 `error.cause`）在 `walkErrorChain`（`:44-63`），防环用 `seen` Set。

**队列层还有一次重试**：`aiRequestQueue.cjs:111-132` 的 `runJob` catch 里 `job.attempts += 1; scheduleRetry(job)`，`scheduleRetry` 又调 `getAiRetryDelayMs`（`:97-109`）。**队列重试 + 队列内部 runner 自身的 `runWithAiRetry` 相乘**，`chatWithConfig:1417` 已经套了 `runWithAiRetry`。→ 单次逻辑调用最坏 3×3 = 9 次 HTTP 请求。

**Pi Agent 特例：禁用队列重试**

```js
// client/electron/services/aiService.cjs:2482-2486
}, {
  signal: request?.signal,
  // Pi Session 保留回合级原生重试，本队列只负责统一调度和并发控制。
  maxAttempts: 1,
});
```

### 2.4 `response_format` / JSON 模式如何降级

**降级是「捕获错误 → 删掉字段 → 重发」，不是「提前探测能力」**：

```js
// client/electron/services/aiService.cjs:1417-1428
result = await runWithAiRetry(() => runWithOperationTimeout(async (signal) => {
  try {
    return await requestTextAi(app, config, requestBody, { signal, requestMode });
  } catch (error) {
    if (!preparedRequest.response_format || !error.responseFormatUnsupported) {
      throw error;
    }
    requestBody = createChatRequestBody(config, preparedRequest, { omitResponseFormat: true, stream: requestMode === 'stream' });
    return requestTextAi(app, config, requestBody, { signal, requestMode });
  }
}, timeoutMs, request.signal));
```

`responseFormatUnsupported` 的判定是**字符串关键词匹配**：

```js
// client/electron/services/aiService.cjs:80-91
function isResponseFormatUnsupported(message) {
  const normalized = String(message || '').toLowerCase();
  return normalized.includes('response_format') && [
    'not supported',
    'does not support',
    'not support',
    'unsupported',
    'unknown parameter',
    'invalid parameter',
    'must be',
  ].some((marker) => normalized.includes(marker));
}
```

→ 只要错误文本里**同时**出现 `response_format` 和上述任一标记才降级；厂商返回中文错误（"不支持 response_format 参数"）**匹配不上 `includes('response_format')`** → 不会降级，直接失败。

同一降级逻辑在图像接口也有独立实现（`fetchOpenAICompatibleImageResponse:533-539`）。

### 2.5 流式如何处理

模式由配置决定，**默认流式**：

```js
// client/electron/services/aiService.cjs:154-156
function normalizeTextRequestMode(config) {
  return config?.request_mode === 'normal' ? 'normal' : 'stream';
}
```

SSE 逐行消费（手动 buffer，`pop()` 保留半行）：

```js
// client/electron/services/aiService.cjs:1067-1094
while (!state.done) {
  const { value, done } = await reader.read();
  if (done) {
    break;
  }

  buffer += decoder.decode(value, { stream: true });
  const lines = buffer.split(/\r?\n/);
  buffer = lines.pop() || '';

  for (const line of lines) {
    await readSseJsonDataLine(line, state, options);
    if (state.done) {
      break;
    }
  }
}

buffer += decoder.decode();          // 收尾 flush
if (!state.done && buffer.trim()) {
  const lines = buffer.split(/\r?\n/);
  for (const line of lines) {
    await readSseJsonDataLine(line, state, options);
    if (state.done) {
      break;
    }
  }
}
```

- 结束标记 `[DONE]` → `state.done = true`（`:1033-1036`）；
- 兼容三种 delta 形态：`delta.content` / `message.content` / `text`（`appendStreamChoiceContent:990-1008`）；
- **`data:` 行的 JSON 解析失败与 payload 内 error 都标为 retryable**（`:1044`, `:1051`）→ 会触发整体重发。

**关键结论：流式只是传输方式，对上层完全透明** —— `chatWithConfig` 最终 `return content;`（`:1446`），全仓库的提取任务都拿**完整字符串**。**解析提取模块没有任何逐 token 回调 / 打字机输出**；进度靠 `progressCallback` 的阶段性文案（见第 3 节）。

### 2.6 错误如何 yield

提取任务链路**没有 yield**，全部是 `await` + throw，最终由 `taskService` 的 `.catch()` 转成任务终态：

```js
// client/electron/services/taskService.cjs:893-905
runner({ aiService: runnerAiService, ..., updateTask, checkpointTask, payload, taskControl, previousState }).catch((error) => {
  if (!taskControl.signal.aborted) {
    checkpointTask({ status: 'error', error: error.message || '任务执行失败' });
  }
}).finally(() => {
  taskControl.dispose();
  if (aiService?.resumeQueueScope) {
    aiService.resumeQueueScope(queueScopeId);
  }
  activeTasks.delete(type);
  activeTaskControls.delete(type);
  resolveSettled();
});
```

单个提取项的错误被就地吞掉、**不影响其它项**（见第 9 节）。

超时：`AI_REQUEST_TIMEOUT_MS = 600000`（10 分钟，`aiService.cjs:28`），`runWithOperationTimeout` 用 `AbortSignal.any([timeout, parentSignal])` 合并父取消信号（`:339-347`）。

## 3. `collectJsonResponse` 的完整实现（逐层判定）

> **命名说明**：`collect_json_response`（蛇形）**在本仓库源码中未找到**；实际函数名为 `collectJsonResponse` / `collectJsonResponseWithConfig`。全仓 27 处引用已用 `Select-String` 全量确认（`electron/` + `src/`，排除 node_modules）。

### 3.1 第 0 层：入队

`aiService.cjs:2496-2501` → `enqueueTextRequest` → `textRequestQueue.enqueue`（并发 10，scope 可暂停）。

### 3.2 第 1 层：多模态预处理 + 循环 3 次

```js
// client/electron/services/aiService.cjs:832-850
async function collectJsonResponseWithConfig(app, config, request) {
  const preparedMessages = await prepareMultimodalMessages(config, request.messages);
  const maxRetries = request.max_retries ?? 2;          // ← 默认 2 次重试 = 共 3 次
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
```

### 3.3 第 2 层：JSON 提取（**四级候选**）

```js
// client/electron/services/aiService.cjs:702-736
function parseJsonContent(content) {
  const normalized = String(content || '').replace(/^\uFEFF/, '').trim();
  const candidates = [
    normalized,                                     // ① 原样
    extractJsonContent(normalized),                 // ② 剥掉首尾 ```json 围栏
    ...extractFencedJsonBlocks(normalized),          // ③ 全文里所有 ```json 块
  ].filter(Boolean);

  const withBalancedCandidates = [];
  for (const candidate of candidates) {
    withBalancedCandidates.push(candidate);
    withBalancedCandidates.push(...extractBalancedJsonCandidates(candidate)); // ④ 花括号配平扫描
  }

  const repairedCandidates = [];
  for (const candidate of withBalancedCandidates) {
    const repaired = repairInvalidJsonStringEscapes(candidate);
    if (repaired !== candidate) {
      repairedCandidates.push(repaired);
    }
  }

  const uniqueCandidates = [...new Set([...withBalancedCandidates, ...repairedCandidates].map((item) => item.trim()).filter(Boolean))];
  let lastError = null;

  for (const candidate of uniqueCandidates) {
    try {
      return JSON.parse(candidate);
    } catch (error) {
      lastError = error;
    }
  }

  throw lastError || new Error('AI 返回内容为空，无法解析 JSON');
}
```

第 ④ 级 `extractBalancedJsonCandidates`（`:577-635`）逐字符扫描、处理 `inString`/`escaped`，**能从前言废话中抠出第一个完整对象**。额外的「非法转义修复」（`repairInvalidJsonStringEscapes`，`:640-700`）把 `1\.` 之类改回 `1.`。

### 3.4 第 3 层：Schema / 业务校验（**通过注入实现，非内置**）

```js
// client/electron/services/aiService.cjs:782-788
function normalizeJsonPayload(request, parsed) {
  const normalized = request.normalizer ? request.normalizer(parsed) : parsed;
  if (request.validator) {
    request.validator(normalized);
  }
  return normalized;
}
```

**`validator` 与 `normalizer` 在本仓库中没有任何调用点传入**（全仓 grep `validator:` / `normalizer:` 只在 `aiService.cjs` 自身命中；`rejectionCheckTask.cjs` 只传 `failureMessage`/`progressLabel`）。→ 实际只有 JSON 语法校验，业务校验下沉到各 task 自己解析（见 3.7）。

### 3.5 第 4 层：失败修复（独立的一次 AI 调用）

```js
// client/electron/services/aiService.cjs:855-884
try {
  const parsed = parseJsonContent(content);
  return normalizeJsonPayload(request, parsed);
} catch (error) {
  lastError = error;
  const issues = formatJsonIssues(error);

  try {
    const repairedContent = await repairJsonResponse(
      app, config, content, issues, responseFormat,
      request.progressCallback, progressLabel, request.repairMessagesBuilder, logTitle, request.signal,
    );
    const repairedParsed = parseJsonContent(repairedContent);
    return normalizeJsonPayload(request, repairedParsed);
  } catch (repairError) {
    lastError = repairError;

    if (attempt === maxRetries) {
      await emitProgress(request.progressCallback, `${progressLabel}连续 ${totalAttempts} 次校验失败。`);
      throw new Error(failureMessage);          // ← 抛的是 failureMessage，真实错误被吞
    }

    await emitProgress(request.progressCallback, `${progressLabel}第 ${attempt + 1}/${totalAttempts} 次校验失败，正在重试。`);
  }
}
```

⚠️ **注意 `throw new Error(failureMessage)` 丢掉了 `lastError`** —— 用户只看到「模型返回的 JSON 数据格式无效」，看不到 SyntaxError 原文与修复失败原因。

修复调用的提示词构造：

```js
// client/electron/services/aiService.cjs:746-772（buildJsonRepairMessages，节选）
{
  role: 'system',
  content: `你是一个严格的 JSON 修复助手。请根据给出的原始内容和校验问题，修复现有结果。

要求：
1. 优先在原结果基础上做最小必要修改，不要整体重写
2. 尽量保留原有结构、字段值、节点顺序和已生成内容
3. 若缺少必填字段，应结合现有上下文补齐合理内容，不要用空字符串敷衍
4. 若存在多余说明、代码块包裹、字段名错误、children 结构不规范或顶层包裹错误，应修正为合法 JSON
5. 必须修复 JSON 字符串中的非法反斜杠转义，例如将 1\\. 改为 1.，或将必须保留的反斜杠写成 \\\\n
6. 只返回修复后的完整 JSON，不要输出任何解释`,
},
{ role: 'user', content: `目标结果类型：${targetDescription}` },
{ role: 'user', content: `当前校验问题：\n${issueLines}` },
{
  role: 'user',
  content: `待修复内容：\n\`\`\`json\n${String(invalidContent || '').slice(0, 60000)}\n\`\`\``,
},
```

**待修复内容截断到 60000 字符**（`:765`）—— 超长 JSON 会被静默截断，修复必然失败。渲染进程另有一套更短的修复提示词（见第 4 节 §4.10）。

### 3.6 第 5 层：错误分类

```js
// client/electron/services/aiService.cjs:738-744
function formatJsonIssues(error) {
  if (error instanceof SyntaxError) {
    return [`JSON 语法错误：${error.message}`];
  }

  return [error?.message || String(error || '字段校验失败')];
}
```

### 3.7 提取模块实际走的路径

`bidSectionExtractionTask.cjs` 传的是最简请求（**无 validator / 无 progressCallback**）：

```js
// client/electron/services/bidSectionExtractionTask.cjs:230-235
const raw = await collectJson(aiService, {
  messages: buildExtractMessages(sourceSegments[index], index + 1, sourceSegments.length),
  response_format: { type: 'json_object' },
  logTitle: `多标段识别-第${index + 1}段`,
  progressLabel: `多标段识别第${index + 1}段`,
});
```

它传了 `progressLabel`，但 `progressCallback` 未传 → `emitProgress` 直接 return（`aiService.cjs:774-780`），**进度文案是死代码**（除非上层注入）。业务校验在 task 内自己做：

```js
// client/electron/services/bidSectionExtractionTask.cjs:115-119
function validateSectionsResponse(value) {
  if (!Array.isArray(value?.sections) || value.sections.length < 2) {
    throw new Error('未识别到至少两个有效标段');
  }
}
```

→ 这条 `>= 2` 校验**不在 `collectJsonResponse` 的 3 次重试内**，一次不过直接整个任务失败。

## 4. 全部提示词原文

> 分四组：① 通用 system；② 18 项任务提示词（Main 为准，附 TS 副本差异）；③ 分段/合并；④ 标段识别与 JSON 修复。

### 4.1 通用 system 提示词（唯一定义）

```js
// client/electron/services/bidAnalysisTask.cjs:12-18
const stableSystemPrompt = `你是专业的投标资料分析助手。请严格基于用户提供的上下文完成提取和总结。

通用要求：
1. 保持信息全面、准确，优先使用用户提供上下文中的内容；除非具体任务明确要求或允许根据经验补充，否则不要自行编造
2. 已提取到相关内容但局部信息没有提及时，明确写"没有提及"
3. 只输出最终结果，不输出过程、提示语或客套话
4. 始终使用简体中文`;
```

**注意第 2 条与任务级提示词里的「招标文件未提及」「原文未提及」「本段未提及」「- 原文未提及」是不同字面量**（见 4.2/4.3），下游判定函数 `isMissingTechnicalScoreItems` 只认其中一种。

### 4.2 消息分层（`buildTenderContextMessages` / `buildMessages`）

```js
// client/electron/services/bidAnalysisTask.cjs:202-227
function buildTenderContextMessages(fileContent, sectionHint) {
  const messages = [
    { role: 'system', content: stableSystemPrompt },
  ];
  if (sectionHint) {
    messages.push({ role: 'system', content: sectionHint });      // ← 独立第二条 system
  }
  messages.push({ role: 'user', content: `以下是完整招标文件。后续任务需要基于这份招标文件完成；如后续消息提供补充上下文，请按具体任务要求综合使用：\n\n${fileContent}` });
  return messages;
}

function buildMessages(fileContent, task, sectionHint) {
  const messages = buildTenderContextMessages(fileContent, sectionHint);
  messages.push(
    { role: 'user', content: buildTaskPrompt(task) },
  );
  return messages;
}

async function runSingleBidAnalysisPromptTask({ aiService, fileContent, task, sectionHint, logTitle }) {
  return aiService.chat({
    messages: buildMessages(fileContent, task, sectionHint),
    response_format: task.output === 'json' ? { type: 'json_object' } : undefined,
    logTitle: logTitle || `招标解析-${task.label}`,
  });
}
```

**架构 = [system 纪律] → [system 作用域约束] → [user 全文] → [user 任务]**。

标段 hint（第二条 system）生成器：

```js
// client/electron/utils/bidSectionContext.cjs:19-33
if (!title && !headLine && !description && !evidence.length) {
  return hasSelectedSection
    ? '本项目为多标段，当前招标文件已按用户选择的投标范围处理。请以当前输入内容为准，不要主动扩展到其他标段。'
    : '';
}

const lines = [
  '本项目为多标段，当前招标文件已按用户选择的投标范围处理。请仅关注当前选择标段和当前输入内容，不要主动扩展到其他标段。',
];
if (title) lines.push(`当前选择标段：${title}`);
if (headLine) lines.push(`AI 识别标题行：${headLine}`);
if (description) lines.push(`AI 识别描述：${description}`);
if (evidence.length) lines.push(`AI 识别依据：${evidence.join('；')}`);
return lines.join('\n');
```

evidence **最多保留 6 条**（`slice(0, 6)`，`:5-10`）。

### 4.3 缺失标注规范（Markdown 项唯一追加口）

```js
// client/electron/services/bidAnalysisTask.cjs:6
const MARKDOWN_MISSING_RESULT = '未提取到';

// client/electron/services/bidAnalysisTask.cjs:189-200
// 为 Markdown 解析项统一约定整项无结果标记，避免与局部缺失混淆。
function buildTaskPrompt(task) {
  const prompt = task.prompt();
  if (task.output !== 'markdown') return prompt;
  return `${prompt}

整体无结果规则：仅当当前任务完全未提取到任何相关内容时，只返回"${MARKDOWN_MISSING_RESULT}"，不要附加标题、标点、解释或其他文字。只要提取到任何有效内容，就正常返回结果；局部字段或局部分类缺失时写"没有提及"，不要使用"${MARKDOWN_MISSING_RESULT}"。`;
}

function isMissingMarkdownResult(task, content) {
  return task.output === 'markdown' && String(content || '').trim() === MARKDOWN_MISSING_RESULT;
}
```

配套重跑（fail-soft，第二次结果**原样**上抛，不判缺失）：

```js
// client/electron/services/bidAnalysisTask.cjs:261-266
// Markdown 整项无结果时完整重跑一次，第二次结果原样交给上层保存。
async function runBidAnalysisPromptTask(options) {
  const content = await runBidAnalysisPromptTaskOnce(options);
  if (!isMissingMarkdownResult(options.task, content)) return content;
  return runBidAnalysisPromptTaskOnce(options);
}
```

### 4.4 JSON 任务模板（两处，逐字不同）

**Main 版**（`bidAnalysisTask.cjs:20-34`）——注意第 3 条说的是「**招标文件**中没有的字段」：

```js
function jsonTask(title, goals, outputJson) {
  return `任务：${title}

目标：${goals}

约束：
1. 输出格式必须为 JSON。
2. 严格按照以下 JSON 格式输出，只修改 value，禁止修改 key 和结构。
3. 招标文件中没有的字段填充"没有提及"。

JSON 格式：
${outputJson}

仅输出 JSON，不要输出其他内容。`;
}
```

**渲染进程 TS 版**（`bidAnalysisWorkflow.ts:28-42`）——第 3 条是「**原文**中没有的字段」，且 JSON 模板带缩进换行：

```ts
function jsonTask(title: string, goals: string, outputJson: string) {
  return `任务：${title}

目标：${goals}

约束：
1. 输出格式必须为 JSON。
2. 严格按照以下 JSON 格式输出，只修改 value，禁止修改 key 和结构。
3. 原文中没有的字段填充"没有提及"。

JSON 格式：
${outputJson}

仅输出 JSON，不要输出其他内容。`;
}
```

→ **两份提示词不是同一份文本**，这是本模块最直接的「同一判据两份实现」。

### 4.5 18 项任务清单（字段：`id / label / required / output / description / prompt`）

定义在 `bidAnalysisTask.cjs:71-155`。完整逐项原文如下（JSON 项的模板已归并到 §4.4）：

| # | id | label | required | output | 行号 |
|---|---|---|---|---|---|
| 1 | `projectOverview` | 项目概述 | true | markdown | `:73-79` |
| 2 | `techRequirements` | 技术评分要求 | true | markdown | `:81-109` |
| 3 | `projectInfo` | 项目信息 | true | json | `:110` |
| 4 | `partAInfo` | 甲方信息 | true | json | `:111` |
| 5 | `deliveryAndServiceRequirements` | 交货和服务要求 | true | json | `:112` |
| 6 | `procurementList` | 采购清单 | false | markdown | `:114-126` |
| 7 | `responseFileRequirements` | 响应文件要求 | true | markdown | `:128-143` |
| 8 | `agentInfo` | 代理机构信息 | false | json | `:144` |
| 9 | `keyInfo` | 投标关键节点 | false | json | `:145` |
| 10 | `marginInfo` | 投标保证金 | false | json | `:146` |
| 11 | `qualificationReview` | 资格性审查 | false | markdown | `:147` |
| 12 | `complianceCheck` | 符合性检查 | false | markdown | `:148` |
| 13 | `openBid` | 开标要求 | false | json | `:149` |
| 14 | `evaluationBid` | 评标要求 | false | json | `:150` |
| 15 | `businessScoring` | 商务评分要求 | false | markdown | `:151` |
| 16 | `discardedBids` | 无效标与废标项 | false | markdown | `:152`（专用函数） |
| 17 | `signingProcess` | 合同授予与签订 | false | json | `:153` |
| 18 | `terminationCondition` | 合同解除和终止 | false | json | `:154` |

**必选项 6 个**：`projectOverview / techRequirements / projectInfo / partAInfo / deliveryAndServiceRequirements / responseFileRequirements`。

Markdown 项原文（7 条）：

```js
// client/electron/services/bidAnalysisTask.cjs:73-79
{
  id: 'projectOverview', label: '项目概述', required: true, output: 'markdown', description: '提取项目基本信息、背景目的、规模预算、时间安排、实施内容和技术特点等。',
  prompt: () => `任务：提取并总结项目概述信息。

请重点关注项目名称、基本信息、背景目的、规模预算、时间安排、实施内容、技术特点和其他关键要求。

工作要求：保持信息全面准确，尽量使用招标文件中的内容；只关注与项目实施有关的内容，不提取商务信息；直接返回整理好的项目概述。`,
},
```

```js
// client/electron/services/bidAnalysisTask.cjs:114-126
{
  id: 'procurementList', label: '采购清单', required: false, output: 'markdown', description: '采购内容、数量、规格参数、交付和验收要求。',
  prompt: () => `任务：提取招标文件、询比文件或采购文件中的采购清单/采购需求信息。

请从招标文件中识别与"采购清单、采购需求、采购内容、货物需求、服务内容、技术参数、规格要求、报价清单、分项报价、工程量清单"等含义相近的内容。

提取要求：
1. 优先保留招标文件中的表格、条目和字段含义，不要自行补充招标文件没有的信息。
2. 如果原文是表格，请尽量整理为 Markdown 表格；如果表格结构复杂，可以按"清单项 + 要求说明"的方式整理。
3. 如果不同章节分别描述采购内容、技术参数、数量、交付、验收、质保等要求，请合并整理，但要避免编造不存在的字段。
4. 字段名称不要求固定，按招标文件实际出现的信息组织，例如名称、规格型号、技术参数、单位、数量、预算/限价、交付地点、交付时间、验收要求、质保要求、备注等。
5. 如果没有找到明确采购清单，请说明"未找到明确采购清单"，并列出可能相关的采购需求段落摘要。
6. 只输出整理结果，不要输出分析过程。`,
},
```

```js
// client/electron/services/bidAnalysisTask.cjs:128-143
{
  id: 'responseFileRequirements', label: '响应文件要求', required: true, output: 'markdown', description: '响应文件组成、格式模板、签章、递交和偏离表要求。',
  prompt: () => `任务：提取招标文件、询比文件或采购文件中关于响应文件/投标文件编制与提交的要求。

请识别与"响应文件、投标文件、报价文件、资格证明文件、商务响应、技术响应、偏离表、响应文件格式、投标文件格式、递交要求、签字盖章、密封上传"等含义相近的内容。

提取要求：
1. 按招标文件实际结构整理，不要强制套用固定模板。
2. 重点提取响应文件需要包含哪些部分，例如报价文件、商务文件、技术文件、资格证明、承诺函、授权委托书、响应表、偏离表、分项报价表等。
3. 如果招标文件提供了固定格式、表格或附件模板，请提取模板名称、用途、填写要求和关键字段。
4. 提取签字盖章、文件命名、装订/密封、上传格式、份数、递交截止时间、递交方式等要求。
5. 保持投标文件中所列的响应文件顺序，保证后续编写响应文件时，可以直接按照你提取的结果一一对应编写。
6. 区分"必须提供"和"如适用/可选提供"的内容；如果招标文件没有明确区分，不要自行判断。
7. 不要生成供应商自己的最终响应文件，不要编造公司信息、报价、资质、承诺内容。
8. 如果没有找到明确响应文件要求，请说明"未找到明确响应文件要求"，并列出可能相关的投标/响应文件格式段落摘要。
9. 只输出整理结果，不要输出分析过程。`,
},
```

```js
// client/electron/services/bidAnalysisTask.cjs:147-152
{ id: 'qualificationReview', label: '资格性审查', required: false, output: 'markdown', description: '投标人资格条件和资格审查要求。', prompt: () => '任务：提取招标文件中关于投标人资格性审查的信息。整理成方便阅读的 Markdown，不要使用表格；如果招标文件是表格，请转换为列表。仅输出整理结果。' },
{ id: 'complianceCheck', label: '符合性检查', required: false, output: 'markdown', description: '文件完整性、有效性、规范和偏差处理要求。', prompt: () => '任务：总结招标文件中关于符合性检查的信息，包括文件完整性、文件有效性、文件规范、偏差处理等。整理成 Markdown，不要使用表格。仅输出整理结果。' },
{ id: 'businessScoring', label: '商务评分要求', required: false, output: 'markdown', description: '商务评分因素，为商务方案准备。', prompt: () => '任务：提取招标文件中的商务评分因素，为编写投标文件中的商务方案做准备。整理成 Markdown，不要使用表格。仅输出整理结果。' },
{ id: 'discardedBids', label: '无效标与废标项', required: false, output: 'markdown', description: '投标无效、废标相关风险项。', prompt: buildInvalidBidAndRejectionItemsPrompt },
```

### 4.6 ⚠️ 技术评分要求提取：「自我反思五段式」的真实状态

这是角色提示特别要求关注的点，**结论必须分两侧说清**：

#### (a) 源码里的 `techRequirements` —— **不是五段式，是「语义二分」版**

```js
// client/electron/services/bidAnalysisTask.cjs:81-109
{
  id: 'techRequirements', label: '技术评分要求', required: true, output: 'markdown', description: '提取技术评分项、权重分值、评分标准和招标文件中的位置。',
  prompt: () => `任务：提取技术评分信息，并按语义区分"技术评分项"和"技术评分要求"。

重点识别"技术评分""评标方法""评分标准""技术参数""技术要求""技术方案""技术部分""评审要素"相关章节，不要提取商务、价格、资质等无关条目。

分类原则：
1. 技术评分项：指投标人需要在技术方案中一一响应、展开编写，并可对应形成技术方案章节的具体评分内容，例如方案类、措施类、团队类、实施类、服务类、保障类、运维类、应急类、检查类等评分内容。
2. 技术评分要求：指用于约束评分、解释评分、定义扣分或判定规则的通用规则或说明，例如符合性要求、偏离扣分规则、判定口径、适用范围说明、表后说明、通用评审规则等。
3. 判断依据是该内容是否要求投标人在技术方案中展开具体方案内容；如果不是具体方案内容，即使带有分值或扣分规则，也归入技术评分要求。
4. 若原文存在层级关系，请保持顺序和来源，不要自行合并不相关条款。

输出格式：

## 技术评分项

【评分项名称】：<招标文件描述，保留专业术语>
【权重/分值】：<具体分值或占比>
【评分标准】：<详细规则>
【数据来源】：<章节、条款、页码或表格位置>

## 技术评分要求

【评分要求名称】：<要求或规则名称>
【适用范围】：<适用于哪些评分项或评审环节>
【要求/判定口径】：<具体要求、解释、扣分或判定规则>
【数据来源】：<章节、条款、页码或表格位置>

若某一类没有内容，请保留对应标题并写"没有提及"。直接返回提取结果。`,
},
```

结构为：**任务 → 重点识别 → 分类原则(4 条) → 输出格式(两个二级小节) → 缺失约定**。**无「输出示例」、无「验证步骤」**。

#### (b) 渲染进程副本 —— 是「目标定位 + 处理规则」三段

```ts
// client/src/features/technical-plan/services/bidAnalysisWorkflow.ts:73-91
buildTaskPrompt: () => `任务：提取技术评分要求。

目标定位：
1. 重点识别与"技术评分""评标方法""评分标准""技术参数""技术要求""技术方案""技术部分""评审要素"相关的章节。
2. 不要提取商务、价格、资质等与技术类评分项无关的条目。

每一项按以下结构输出，信息缺失时标注"没有提及"：
【评分项名称】：<原文描述，保留专业术语>
【权重/分值】：<具体分值或占比>
【评分标准】：<详细规则>
【数据来源】：<章节、条款、页码或表格位置>

处理规则：
1. 若没有明确"技术评分表"，根据上下文判断技术评分相关内容。
2. 若评分项以表格形式呈现，按行提取，并标注"[表格数据]"。
3. 若存在二级评分项，用缩进或编号体现层级关系。
4. 单位尽量统一为"分"或"%"，必要时注明原文单位。

直接返回提取结果，除此之外不输出任何其他内容。`,
```

**两份 `techRequirements` 提示词语义不同**：Main 要求**二分输出两个小节**，TS 只有**单一扁平列表**。而下游 `isMissingTechnicalScoreItems` 只认 `## 技术评分项` 这个二级标题（`bidAnalysisWorkflow.ts:24`）→ **两份提示词与判定函数三者不自洽**。

#### (c) 真正的「自我反思五段式」只存在于官方文章，不在代码里

`文章\标书智能体（一）——AI解析招标文件代码+提示词.md:169-217`（开源版 yibiao-simple 的原始提示词）：

> `:171` —— 在编写招标文件中的技术方案时，技术评分要求非常重要，基本要做到1对1应答式编写，所以评分要求的提取则尤为重要，我采用了**自我反思式的结构化提示词**进行提取处理。

`:175-210` 的 SystemPrompt 逐字原文：

```markdown
你是一名专业的招标文件分析师，擅长从复杂的招标文档中高效提取"技术评分项"相关内容。请严格按照以下步骤和规则执行任务：
### 1. 目标定位
- 重点识别文档中与"技术评分"、"评标方法"、"评分标准"、"技术参数"、"技术要求"、"技术方案"、"技术部分"或"评审要素"相关的章节（如"第X章 评标方法"或"附件X：技术评分表"）。
- 忽略商务、价格、资质等非技术类评分项。
### 2. 提取内容要求
对每一项技术评分项，按以下结构化格式输出（若信息缺失，标注"未提及"），如果评分项不够明确，你需要根据上下文分析并也整理成如下格式：
【评分项名称】：<原文描述，保留专业术语>
【权重/分值】：<具体分值或占比，如"30分"或"40%">
【评分标准】：<详细规则，如"≥95%得满分，每低1%扣0.5分">
【数据来源】：<文档中的位置，如"第5.2.3条"或"附件3-表2">

### 3. 处理规则
- **模糊表述**：有些招标文件格式不是很标准，没有明确的"技术评分表"，但一定都会有"技术评分"相关内容，请根据上下文判断评分项。
- **表格处理**：若评分项以表格形式呈现，按行提取，并标注"[表格数据]"。
- **分层结构**：若存在二级评分项（如"技术方案→子项1、子项2"），用缩进或编号体现层级关系。
- **单位统一**：将所有分值统一为"分"或"%"，并注明原文单位（如原文为"20点"则标注"[原文：20点]"）。

### 4. 输出示例
【评分项名称】：系统可用性 
【权重/分值】：25分 
【评分标准】：年平均故障时间≤1小时得满分；每增加1小时扣2分，最高扣10分。 
【数据来源】：附件4-技术评分细则（第3页） 

【评分项名称】：响应时间
【权重/分分】：15分 [原文：15%]
【评分标准】：≤50ms得满分；每增加10ms扣1分。
【数据来源】：第6.1.2条

### 5. 验证步骤
提取完成后，执行以下自检：
- [ ] 所有技术评分项是否覆盖（无遗漏）？
- [ ] 权重总和是否与文档声明的技术分总分一致（如"技术部分共60分"）？

直接返回提取结果，除此之外不输出任何其他内容
```

UserPrompt（`:214-217`）：

```markdown
请分析以下招标文件内容，提取技术评分要求信息：
{request.file_content}
```

**关键差异（若要把五段式迁回代码需注意）**：

| 段 | 文章五段式 | Main 源码 | TS 源码 |
|---|---|---|---|
| 1 目标定位 | ✅ | ✅ | ✅ |
| 2 提取内容要求 | ✅ | ✅（拆成两小节） | ✅ |
| 3 处理规则 | ✅ 4 条 | ❌ | ✅ 4 条 |
| 4 **输出示例** | ✅ 2 个样例 | ❌ | ❌ |
| 5 **验证步骤** | ✅ 自检清单 | ❌ | ❌ |
| 缺失标记 | `"未提及"` | `"没有提及"` | `"没有提及"` |

（文章 `:171` 那句「自我反思式」在**代码中搜索不到** —— 全仓 `Select-String '自我反思'` 仅命中两篇 `.md` 文章，`.cjs/.ts` 零命中。）

### 4.7 无效标与废标项（专用四象限提示词）

**Main 版**（`bidAnalysisTask.cjs:36-69`）：

```js
function buildInvalidBidAndRejectionItemsPrompt() {
  return `任务：提取并分析招标文件中的"无效投标"和"废标项"。

概念边界：
1. "无效投标"指投标人、投标文件、签章密封、递交时间、报价、保证金、资格条件、实质性响应等原因导致投标被认定为无效、否决、不予受理或按无效响应处理的情形。
2. "废标项"指可能导致项目废标、采购失败、重新招标、终止评审、有效投标人不足或实质性响应不足的条款或风险项。
3. 招标文件使用"否决投标""投标无效""不予受理""无效响应""重大偏差""实质性偏离""废标情形"等同义表达时，也要按上述边界归类。

输出要求：
1. 必须明确区分"无效投标"和"废标项"。
2. "招标文件中明确提到的"只能提取招标文件中明确出现或同义表达的内容，尽量保留招标文件中的关键句；如果没有提及，写"招标文件未提及"。
3. "此类标书还可能涉及的"需要根据你的经验，补充招标文件中未明确提及、但结合本招标文件类型和招投标经验判断非常重要的高风险遗漏项。
4. 不要罗列所有常见可能项，不要输出泛泛的通用清单；每个小节最多输出 3-5 条。
5. 不要使用表格，使用 Markdown 列表。
6. 仅输出下方格式，不要输出解释、过程或额外段落。
7. 不要输出三重引号、代码块标记或其他格式包裹符。

输出格式：
# 招标文件中明确提到的

## 无效投标
- ...

## 废标项
- ...

# 此类标书还可能涉及的

## 无效投标
- ...

## 废标项
- ...`;
}
```

**渲染进程版**（`client/src/shared/prompts/analysisPrompts.ts:1-36`，`bidAnalysisWorkflow.ts:286` 引用它）—— **9 条输出要求、多 2 条、措辞全改**：

```ts
export function buildInvalidBidAndRejectionItemsPrompt() {
  return `任务：提取并分析招标文件中的"无效投标"和"废标项"。

概念边界：
1. "无效投标"指投标人、投标文件、签章密封、递交时间、报价、保证金、资格条件、实质性响应等原因导致投标被认定为无效、否决、不予受理或按无效响应处理的情形。
2. "废标项"指可能导致项目废标、采购失败、重新招标、终止评审、有效投标人不足或实质性响应不足的条款或风险项。
3. 原文使用"否决投标""投标无效""不予受理""无效响应""重大偏差""实质性偏离""废标情形"等同义表达时，也要按上述边界归类。

输出要求：
1. 必须明确区分"无效投标"和"废标项"。
2. "原文中明确提到的"只能提取招标文件原文中明确出现或同义表达的内容，尽量保留原文关键句；如果没有提及，写"- 原文未提及"。
3. "此类标书还可能涉及的"只补充原文未明确提及、但结合本招标文件类型和招投标经验判断非常重要的高风险遗漏项。
4. 不要罗列所有常见可能项，不要输出泛泛的通用清单；每个小节最多输出 3-5 条。
5. 如果没有明显需要补充的关键项，写"- 暂未发现必须补充的高风险项"。
6. 经验补充项每条前缀使用"重点补充："，并用一句话说明为什么需要关注。
7. 不要使用表格，使用 Markdown 列表。
8. 仅输出下方格式，不要输出解释、过程或额外段落。
9. 不要输出三重引号、代码块标记或其他格式包裹符。

输出格式：
# 原文中明确提到的

## 无效投标
- ...

## 废标项
- ...

# 此类标书还可能涉及的

## 无效投标
- 重点补充：...

## 废标项
- 重点补充：...`;
}
```

⚠️ **两个版本的缺失字面量不同**：`招标文件未提及` vs `- 原文未提及`。下游若有正则依赖其一，换版会静默失配。

### 4.8 分段合稿提示词

```js
// client/electron/utils/segmentedAiResultMerger.cjs:21-45
const outputRequirement = output === 'json'
  ? '最终只返回一个 JSON 对象，不要输出 Markdown、代码块、解释或额外文字。'
  : '最终只返回整理后的 Markdown 内容，不要输出解释、过程或额外提示语。';

messages.push({
  role: 'user',
  content: `以下内容来自同一份招标文件按段分别解析后的结果。每段结果只代表该片段内的信息，不代表整份文件的完整结论。

当前合并任务：${taskLabel || '招标文件解析结果合并'}

合并要求：
1. 如果某段写"没有提及""原文未提及""本段未提及"或整段只有"未提取到"，只表示该片段没有相关信息；如果其他片段提供了有效信息，应以有效信息为准。
2. 删除重复、空泛、冲突的片段性表述，保留更完整、更具体的信息。
3. 保留所有有价值、可用于最终结果的信息，不要遗漏分段结果中的明确内容。
4. 不要新增分段结果中没有的信息，不要自行编造。
5. 如果所有分段都没有有效信息，遵守原始任务中的整体无结果规则。
6. 最终输出必须符合原始任务要求。
7. ${outputRequirement}

原始任务要求：
${taskPrompt}

分段解析结果：
${formatSegmentResults(segmentResults)}`,
});
```

分段结果用 `## 第 i/n 段解析结果` 分节（`segmentedAiResultMerger.cjs:7`）。**这里枚举了 4 种缺失字面量**（`没有提及` / `原文未提及` / `本段未提及` / `未提取到`）—— 是全仓对缺失语义覆盖最全的一处。

### 4.9 标段识别提示词（system + user）

**提取阶段**：

```js
// client/electron/services/bidSectionExtractionTask.cjs:121-163
function buildExtractMessages(segment, segmentIndex, totalSegments) {
  return [
    {
      role: 'system',
      content: `你是严谨的招标文件多标段识别专家。你只能基于用户提供的带行号文本识别标段、标包、分包、采购包、包件或标的。`,
    },
    {
      role: 'user',
      content: `当前是招标文件第 ${segmentIndex}/${totalSegments} 段。每行格式为"L000001 | 原文"。

任务：识别本段中明确属于某个标段/标包/分包/采购包/包件/标的的内容，并返回结构化 JSON。

要求：
1. 只识别明确属于某个标段的内容范围。
2. 通用条款不要归入某个标段；不确定归属的内容不要输出范围。
3. includeRanges 必须使用输入中的真实行号，startLine 和 endLine 都是不带 L 前缀的数字。
4. 不要编造标段，不要补写原文没有的范围。
5. 无法提供有效 includeRanges 的候选不要输出到 sections。
6. 如果本段没有明确标段内容，返回 {"sections":[]}。
7. 只返回 JSON，不要输出 Markdown、代码块、解释或额外文字。

返回格式：
{
  "sections": [
    {
      "id": "section-1",
      "index": 1,
      "unit": "标段",
      "title": "一标段",
      "headLine": "一标段：设备采购及安装",
      "description": "设备采购、安装、调试及售后服务。",
      "includeRanges": [
        { "startLine": 120, "endLine": 180, "reason": "一标段采购清单" }
      ],
      "evidence": ["一标段：设备采购及安装"]
    }
  ]
}

带行号文本：
${segment}`,
    },
  ];
}
```

**合并阶段**：

```js
// client/electron/services/bidSectionExtractionTask.cjs:166-187
function buildMergeMessages(segmentResults) {
  return [
    {
      role: 'system',
      content: '你是严谨的招标文件多标段识别结果合并专家。你只能合并用户提供的分段识别结果，不得编造新标段或新行号。',
    },
    {
      role: 'user',
      content: `以下是同一份招标文件各分段识别出的标段候选。请合并重复标段，保留所有明确属于各标段的 includeRanges 和 evidence。

要求：
1. 同一标段跨多个分段出现时合并为一个 sections 项。
2. 不要把通用条款合并到任何标段。
3. 不要新增分段结果中没有的行号范围。
4. 如果最终少于两个标段，返回已有结果。
5. 只返回 JSON，不要输出 Markdown、代码块、解释或额外文字。

分段结果：
${JSON.stringify(segmentResults, null, 2)}`,
    },
  ];
}
```

⚠️ **合并阶段把全部分段结果 JSON 化后一次性塞进一条 user 消息，无长度上限**（与 `buildJsonRepairMessages` 的 60000 截断形成对比）。

### 4.10 渲染进程版 JSON 修复提示词（更短，少 4 条要求）

```ts
// client/src/shared/prompts/jsonRepairPrompts.ts:16-24
return [
  {
    role: 'system',
    content: '你是一个严格的 JSON 修复助手。必须修复 JSON 字符串中的非法反斜杠转义，例如将 1\\. 改为 1.，或将必须保留的反斜杠写成 \\\\。只返回修复后的完整 JSON，不要输出任何解释。',
  },
  { role: 'user', content: `目标结果类型：${targetDescription}` },
  { role: 'user', content: `当前校验问题：\n${issueLines}` },
  { role: 'user', content: `待修复内容：\n\`\`\`json\n${invalidContent}\n\`\`\`` },
];
```

**无 60000 截断**（对比 `aiService.cjs:765`），也**没有第 5 条 user 消息**（`请在保留原有正确内容的前提下…`）。

---

## 5. 数据结构：提取项清单的完整字段

### 5.1 任务定义（Main，CJS）

```js
// client/electron/services/bidAnalysisTask.cjs:73（代表项）
{
  id: 'projectOverview',          // 唯一键，snake 命名，全小写下划线
  label: '项目概述',              // UI 显示名
  required: true,                 // 是否必选（决定 mode='key' 的集合 + 完成门禁）
  output: 'markdown',             // 'markdown' | 'json' —— 决定是否追加缺失规则、是否传 response_format
  description: '提取项目基本信息、…', // 前端列表描述
  prompt: () => `…`,              // 惰性函数，返回完整任务提示词
}
```

### 5.2 任务定义（渲染进程，TS）—— **字段名不同**

```ts
// client/src/features/technical-plan/services/bidAnalysisWorkflow.ts:4-11
export interface BidAnalysisTaskDefinition {
  id: string;
  label: string;
  description: string;
  required: boolean;
  output: 'markdown' | 'json';
  buildTaskPrompt: () => string;      // ← Main 侧叫 prompt()
}
```

⚠️ **两份定义结构不同（`prompt` vs `buildTaskPrompt`）、内容不同（见 §4.6）、排序不同**（TS 里 `procurementList` 在 `responseFileRequirements` 之前、Main 里相反）。

### 5.3 模式与归一化

```js
// client/electron/services/bidAnalysisTask.cjs:157-183
function getBidAnalysisTasks(mode) {
  return mode === 'full' ? tasks : tasks.filter((task) => task.required);
}
function normalizeBidAnalysisConfig(mode, selectedTaskIds) {
  const requiredTaskIds = getBidAnalysisTasks('key').map((task) => task.id);
  const requiredSet = new Set(requiredTaskIds);
  const selectedSet = new Set([...requiredTaskIds, ...normalizeBidAnalysisTaskIds(selectedTaskIds)]);
  const selectedIds = tasks.filter((task) => selectedSet.has(task.id)).map((task) => task.id);
  const hasOptional = selectedIds.some((taskId) => !requiredSet.has(taskId));
  const hasAll = selectedIds.length === tasks.length;

  if (mode === 'full' || hasAll) return { mode: 'full', taskIds: tasks.map((task) => task.id) };
  if (mode === 'custom' || hasOptional) return { mode: 'custom', taskIds: selectedIds };
  return { mode: 'key', taskIds: requiredTaskIds };
}
```

`mode` 值域：`'key' | 'full' | 'custom'`（`types.ts:5`），**`custom` 由「选了任一可选项」自动推导，不需显式传**。必选项**永远被并入** selectedSet（`selectedSet` 用 `...requiredTaskIds` 起手）。

### 5.4 前端分组（仅 UI 层，不影响执行）

```tsx
// client/src/features/technical-plan/pages/BidAnalysisPage.tsx:76-82
const taskGroups = [
  { title: '关键项', ids: ['projectOverview', 'techRequirements', 'projectInfo', 'partAInfo', 'deliveryAndServiceRequirements', 'responseFileRequirements'] },
  { title: '采购项', ids: ['procurementList'] },
  { title: '投标流程', ids: ['keyInfo', 'marginInfo', 'openBid'] },
  { title: '评标要求', ids: ['qualificationReview', 'complianceCheck', 'evaluationBid', 'businessScoring'] },
  { title: '主体与合同', ids: ['agentInfo', 'discardedBids', 'signingProcess', 'terminationCondition'] },
];
```

**5 组，18 项全覆盖，无重复无遗漏**（与 §4.5 清单核对一致）。

### 5.5 运行态与存储态

```ts
// client/src/features/technical-plan/types.ts:182-190
export interface BidAnalysisTaskState {
  id: string;
  label: string;
  status: BidAnalysisTaskStatus;   // 'idle' | 'running' | 'success' | 'error'
  content: string;
  error?: string;
}
export type BidAnalysisTasks = Record<string, BidAnalysisTaskState>;
```

运行时构造（`bidAnalysisTask.cjs:381` / `:402` / `:417`）：

- running：`{ id, label, status:'running', content:'' }`（`:381`）
- success：`{ id, label, status:'success', content: trimmedContent }`（`:402`）
- error：`{ ..., status:'error', content: currentTasks[task.id]?.content || '', error: error.message || '解析失败' }`（`:417`）—— **error 时保留旧 content**
- idle（forceRerun 重置）：`{ id, label, status:'idle', content:'' }`（`:339`）

两项结果单独透传到顶层字段（`bidAnalysisTask.cjs:406-407`）：

```js
const technicalPlanPatch = {};
if (task.id === 'projectOverview') technicalPlanPatch.projectOverview = trimmedContent;
if (task.id === 'techRequirements') technicalPlanPatch.techRequirements = trimmedContent;
```

### 5.6 标段数据结构

```ts
// client/src/features/technical-plan/types.ts:334-343
export interface DetectedBidSection {
  id: string;
  index: number;
  unit: string;          // '标段' | '标包' | '分包' | '包' | '采购包' | '包件' | '标的'
  title: string;
  headLine: string;      // AI 识别到的标题行原文
  description: string;
  includeRanges?: BidSectionLineRange[];
  evidence?: string[];
}
```

`BidSectionLineRange` = `{ startLine: number; endLine: number; reason?: string }`（`technicalPlanStore.cjs:218-226`）。

### 5.7 JSON 项的字段字典

`jsonTask` 的第三参数即字段字典，逐字列举于 §4.5 表格对应的行号。字段键全为 **snake_case 英文**，值为中文说明，示例：

```
{"project_name":"项目名称","project_number":"项目编号","project_type":"项目类型","project_budget":"项目预算","project_address":"项目地址"}
{"time_place":"时间地点","part_req":"参与要求","invalid_bid":"无效标认定","objection":"异议处理","bid_process":"开标流程"}
```

前端另有一份**显示用字典**（`BidAnalysisPage.tsx:91-120` `jsonFieldLabels`），⚠️ 与提示词里的键**不完全一致** —— 例如字典里有 `bidding_deposit`（`BidAnalysisPage.tsx:115`），而 `marginInfo` 的模板键是 `bidding_deposit`（`bidAnalysisTask.cjs:146`），但字典首项与模板首项在 `agentInfo` 里重复用了 `company_name/address/...` → **不同任务共享同名字段，前端字典无法区分归属**。

---

## 6. 分段策略 `userTextSplitter`

### 6.1 常量与边界优先级

```js
// client/electron/utils/userTextSplitter.cjs:1-14
const DEFAULT_CONTEXT_LENGTH_LIMIT = 400000;
const DEFAULT_CONTEXT_LIMIT_RATIO = 0.8;
const STRICT_WINDOW_RATIO = 0.12;
const RELAXED_WINDOW_RATIO = 0.25;
const MIN_SEGMENT_RATIO = 0.35;
const MAX_SEGMENT_LIMIT_RATIO = 1.1;

const BOUNDARY_GROUPS = [
  [/\r?\n(?=\s{0,3}#{1,6}\s+)/g, /\r?\n[ \t]*\r?\n/g],   // ① Markdown 标题前 / 空行
  [/\r?\n/g],                                            // ② 换行
  [/[。！？!?]/g],                                        // ③ 句末标点
  [/[；;]/g],                                            // ④ 分号
  [/[，,、：:]/g],                                        // ⑤ 逗号顿号冒号
];
```

**优先级严格按组顺序，先命中先赢**（`findNaturalCut:154-159` 顺序遍历 `BOUNDARY_GROUPS`）。

### 6.2 段长与段数计算

```js
// client/electron/utils/userTextSplitter.cjs:164-180
function splitUserTextByContextLimit(text, config = {}, options = {}) {
  const source = String(text ?? '');
  const contextLengthLimit = normalizeContextLengthLimit(config, options);
  const limitRatio = normalizePositiveRatio(options.limitRatio, DEFAULT_CONTEXT_LIMIT_RATIO);
  const segmentLimit = Math.max(1, Math.floor(contextLengthLimit * limitRatio));

  if (source.length <= segmentLimit) {
    return [source];
  }

  const segmentCount = Math.ceil(source.length / segmentLimit);
  const targetSize = Math.max(1, Math.ceil(source.length / segmentCount));
  const strictRadius = Math.max(1, Math.floor(targetSize * normalizePositiveRatio(options.strictWindowRatio, STRICT_WINDOW_RATIO)));
  const relaxedRadius = Math.max(strictRadius, Math.floor(targetSize * normalizePositiveRatio(options.relaxedWindowRatio, RELAXED_WINDOW_RATIO)));
  const maximumSegmentLength = Math.max(targetSize, Math.ceil(segmentLimit * normalizePositiveRatio(options.maxSegmentLimitRatio, MAX_SEGMENT_LIMIT_RATIO)));
  const minimumSegmentLength = Math.max(1, Math.floor(targetSize * normalizePositiveRatio(options.minSegmentRatio, MIN_SEGMENT_RATIO)));
  const fenceRanges = collectMarkdownFenceRanges(source);
```

数值（400000 上限时）：

- `segmentLimit = 400000 × 0.8 = 320000`
- `segmentCount = ceil(len / 320000)`，**均分**：`targetSize = ceil(len / segmentCount)`
- 严格搜索窗口半径 = `targetSize × 0.12`，放宽 = `targetSize × 0.25`（**两轮都试，先严格后放宽**）
- 段长合法区间 `[targetSize×0.35, max(targetSize, 320000×1.1)]`
- 切点理想位置 = `(len × i) / segmentCount`（**严格均分，非滑动窗口**）

### 6.3 两轮搜索

```js
// client/electron/utils/userTextSplitter.cjs:195-199
const naturalCut = findNaturalCut(source, idealCut, previousCut, state, fenceRanges, strictRadius)
  || findNaturalCut(source, idealCut, previousCut, state, fenceRanges, relaxedRadius);
const cut = naturalCut || clampHardCut(source, idealCut, previousCut, remainingSegments);
cuts.push(cut);
previousCut = cut;
```

`findNaturalCut` 在组内按 `|candidate - idealCut|` 取**最接近理想点**的那个（`:131-134`），窗口 `[ideal-radius, ideal+radius]`，且上界被 `text.length - remainingSegments` 夹住（`:149`）。

### 6.4 候选点四重约束（防尾段塌缩）

```js
// client/electron/utils/userTextSplitter.cjs:94-119
function canUseCandidate(candidate, state) {
  if (candidate <= state.previousCut || candidate >= state.totalLength) {
    return false;
  }

  const currentLength = candidate - state.previousCut;
  if (currentLength < state.minimumSegmentLength) {
    return false;
  }

  if (currentLength > state.maximumSegmentLength) {
    return false;
  }

  if (state.remainingSegments > 0) {
    const remainingLength = state.totalLength - candidate;
    if (remainingLength < state.remainingSegments) {
      return false;
    }
    if (remainingLength / state.remainingSegments > state.maximumSegmentLength) {
      return false;
    }
  }

  return true;
}
```

第 3、4 条是**前瞻性检查**：剩余长度既要够「每段至少 1 字符」，均分后也不能超上限 —— 防止最后几段塌缩成碎片。

### 6.5 围栏保护（不切开代码块）

```js
// client/electron/utils/userTextSplitter.cjs:33-69
function collectMarkdownFenceRanges(text) {
  const ranges = [];
  const regex = /(^|\n)(```|~~~)/g;
  let openedAt = -1;
  let openedMarker = '';
  let match = regex.exec(text);

  while (match) {
    const markerStart = match.index + match[1].length;
    const marker = match[2];

    if (openedAt < 0) {
      openedAt = markerStart;
      openedMarker = marker;
    } else if (marker === openedMarker) {
      ranges.push([openedAt, markerStart + marker.length]);
      openedAt = -1;
      openedMarker = '';
    }

    match = regex.exec(text);
  }

  if (openedAt >= 0) {
    ranges.push([openedAt, text.length]);
  }

  return ranges;
}
function isInsideRange(index, ranges) {
  for (const [start, end] of ranges) {
    if (index <= start) return false;
    if (index < end) return true;
  }
  return false;
}
```

切点落在围栏内直接否决（`findBoundaryForGroup:130`）。**未闭合围栏保护到文末** —— 若文档中段有个多余 ` ``` `，其后全部内容都不切。

### 6.6 代理对保护

```js
// client/electron/utils/userTextSplitter.cjs:71-92
function avoidsBreakingSurrogatePair(text, cut) {
  if (cut <= 0 || cut >= text.length) {
    return cut;
  }

  const previous = text.charCodeAt(cut - 1);
  const next = text.charCodeAt(cut);
  if (previous >= 0xd800 && previous <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) {
    return cut + 1;
  }
  return cut;
}
function clampHardCut(text, idealCut, previousCut, remainingSegments) {
  const minimumCut = previousCut + 1;
  const maximumCut = text.length - remainingSegments;
  if (maximumCut < minimumCut) {
    return Math.min(text.length, minimumCut);
  }
  const clamped = Math.min(maximumCut, Math.max(minimumCut, Math.round(idealCut)));
  return avoidsBreakingSurrogatePair(text, clamped);
}
```

⚠️ **代理对保护只在硬切路径生效**（`clampHardCut`），自然切点路径（`findBoundaryForGroup`）**不做代理对检查** —— 但自然切点都在标点/换行之后，实际不会劈开 emoji。

### 6.7 分段调用点（2 处）

```js
// client/electron/services/bidAnalysisTask.cjs:306-307
const currentConfig = typeof aiService.getConfig === 'function' ? aiService.getConfig() : {};
const fileSegments = splitUserTextByContextLimit(fileContent, currentConfig);
```

```js
// client/electron/services/bidAnalysisTask.cjs:229-235（懒分片兜底）
const segments = Array.isArray(fileSegments) && fileSegments.length
  ? fileSegments
  : splitUserTextByContextLimit(fileContent, typeof aiService.getConfig === 'function' ? aiService.getConfig() : {});
if (segments.length <= 1) {
  return runSingleBidAnalysisPromptTask({ aiService, fileContent: segments[0] || fileContent, task, sectionHint });
}
```

标段识别侧对**带行号文本**分段：

```js
// client/electron/services/bidSectionExtractionTask.cjs:223-225
const numberedMarkdown = numberMarkdownLines(cleanMarkdown);
const segments = splitUserTextByContextLimit(numberedMarkdown, typeof aiService.getConfig === 'function' ? aiService.getConfig() : {});
const sourceSegments = segments.length ? segments : [numberedMarkdown];
```

`contextLengthLimit` 取自配置 `config.context_length_limit`（默认 400000，`configStore.cjs:10,43`），也可被 `options.contextLengthLimit` 覆盖（`userTextSplitter.cjs:26-31`）。

### 6.8 分段 vs 合稿的额外 AI 开销

18 项 × N 段 = 18N 次分段调用 + 18 次合稿调用（仅当 N>1）。**分段调用是全并发的**（`bidAnalysisTask.cjs:237`），合稿串行在其后。

## 7. 标段检测与按行号提取

### 7.1 快速检测（纯正则，0 AI）

```js
// client/electron/utils/bidSectionDetector.cjs:45
const totalSectionPattern = /(?:本?项目)?(?:共|总计|共计|合计)?(?:划分|分|设|拆|分拆)?为?\s*(\d+|[一二三四五六七八九十]+)\s*个?\s*(?:标段|包|分包|标包|标的|子项目)/g;
```

判定优先级（`detectBidSections:137-156`）：

```js
const totalDeclared = detectTotalSectionCount(text);
if (totalDeclared === 1) {
  return { hasMultiple: false, totalDeclared };
}
if (totalDeclared && totalDeclared >= 2) {
  return { hasMultiple: true, totalDeclared };
}

const detectedCount = Math.max(countDefinitionSections(text), countBracketSections(text));
return {
  hasMultiple: detectedCount >= 2,
  totalDeclared,
};
```

三类证据：

1. **总数声明**：优先取含「标段」二字的匹配（`:58-60`），退化到任意单位；`===1` 立即短路为非多标段。
2. **16 条定义式模式**（`:66-83`）覆盖 `X标段/X标包/X分包/X包` × `中文数 / 阿拉伯数 / 第X / X后置` 四种语序，要求后接 `：:；;`。用 `Set` 计数去重（`:97-111`），并排除「一、二标段」这种**合并列举**（`isCombinedSectionMention:93-95`）。
3. **`【N】` / `【N-M】` 分组括号**（`:113-135`）：`【1】…【2】…` 计 2；`【1-1】…【1-2】…` 计子项数（`:134`）。排除「号文」类文档编号行（`isDocumentNumberLine:115-117`）。

中文数字归一 `normalizeChineseNumber`（`:19-43`）：阿拉伯 1–99 直取 → 小写/大写映射表（含 `壹贰叁肆伍`）→ `十X` → `X十` → `X十Y`。

**这一层只出 `{ hasMultiple, totalDeclared }`，不产出标段列表**（文件头注释 `:1-4` 明说）。

### 7.2 按行号提取（AI，两段式）

**第 1 步：加行号**

```js
// client/electron/services/bidSectionExtractionTask.cjs:8-13
function numberMarkdownLines(markdown) {
  return String(markdown || '')
    .split(/\r?\n/)
    .map((line, index) => `L${String(index + 1).padStart(6, '0')} | ${line}`)
    .join('\n');
}
```

6 位零填充，便于 AI 引用。⚠️ 行号是**转换后 Markdown 的行号**，不是原文件页/行。

**第 2 步：逐段 `collectJson`（串行 for + await，不是并发）**

```js
// client/electron/services/bidSectionExtractionTask.cjs:229-238
for (let index = 0; index < sourceSegments.length; index += 1) {
  const raw = await collectJson(aiService, {
    messages: buildExtractMessages(sourceSegments[index], index + 1, sourceSegments.length),
    response_format: { type: 'json_object' },
    logTitle: `多标段识别-第${index + 1}段`,
    progressLabel: `多标段识别第${index + 1}段`,
  });
  segmentResults.push(normalizeSectionsResponse(raw, totalLines));
  log(`已完成第 ${index + 1}/${sourceSegments.length} 段标段候选提取。`, Math.min(80, 12 + Math.round(((index + 1) / sourceSegments.length) * 60)));
}
```

**每段本地归一 + 丢弃越界行号**（fail-closed，不把脏行号交给 AI 合并）：

```js
// client/electron/services/bidSectionExtractionTask.cjs:15-26
function normalizeLineRange(range, totalLines) {
  const startLine = Math.floor(Number(range?.startLine ?? range?.start_line ?? 0));
  const endLine = Math.floor(Number(range?.endLine ?? range?.end_line ?? 0));
  if (!Number.isFinite(startLine) || !Number.isFinite(endLine) || startLine < 1 || endLine < startLine || startLine > totalLines || endLine > totalLines) {
    return null;
  }
  return {
    startLine,
    endLine,
    reason: range?.reason ? String(range.reason).trim() : undefined,
  };
}
```

**第 3 步：AI 合并（仅当多段）+ 本地去重**

```js
// client/electron/services/bidSectionExtractionTask.cjs:84-103
function dedupeSections(sections) {
  const map = new Map();
  for (const section of sections) {
    const key = getSectionMergeKey(section);
    const existing = map.get(key);
    if (!existing) {
      map.set(key, { ...section });
      continue;
    }
    existing.includeRanges = mergeRanges([...(existing.includeRanges || []), ...(section.includeRanges || [])]);
    existing.evidence = [...new Set([...(existing.evidence || []), ...(section.evidence || [])])];
    if (!existing.headLine && section.headLine) existing.headLine = section.headLine;
    if (!existing.description && section.description) existing.description = section.description;
  }
  return Array.from(map.values())
    .map((section) => ({ ...section, includeRanges: mergeRanges(section.includeRanges) }))
    .filter((section) => section.includeRanges.length > 0)      // ← 无有效行号的一律丢弃
    .sort((a, b) => getFirstRangeStart(a) - getFirstRangeStart(b) || a.index - b.index)
    .map((section, index) => ({ ...section, id: `section-${index + 1}` }));   // ← id 重排
}
```

合并键 `getSectionMergeKey`（`:36-42`）= `${unit}:${normalizeSectionTitle(title)}`，标题归一化会剥掉「第」前缀（`:28-34`）并小写。**`id` 在最后被按排序重排**，AI 返回的 id 不作数。

**第 4 步：门禁**

```js
// client/electron/services/bidSectionExtractionTask.cjs:115-119
function validateSectionsResponse(value) {
  if (!Array.isArray(value?.sections) || value.sections.length < 2) {
    throw new Error('未识别到至少两个有效标段');
  }
}
```

与合并提示词第 4 条「如果最终少于两个标段，返回已有结果」**语义冲突** —— 合并环节被要求返回 1 个时，第 4 步会整体抛错。

### 7.3 选定标段 → 生成工作副本（行号切原文）

```js
// client/electron/services/technicalPlanStore.cjs:261-290
function buildSelectedSectionMarkdown(markdown, sections, selectedSectionId) {
  const sourceLines = String(markdown || '').split(/\r?\n/);
  const totalLines = sourceLines.length;
  const selected = sections.find((section) => section.id === selectedSectionId);
  if (!selected) {
    throw new Error('未找到选择的投标范围');
  }
  if (!normalizeBidSectionRanges(selected.includeRanges).length) {
    throw new Error('当前标段缺少有效范围，请重新识别');
  }

  const selectedLines = expandLineRanges(selected.includeRanges, totalLines);
  const otherLines = new Set();
  for (const section of sections) {
    if (section.id === selected.id) continue;
    for (const line of expandLineRanges(section.includeRanges, totalLines)) {
      otherLines.add(line);
    }
  }

  const filtered = sourceLines.filter((_, index) => {
    const lineNumber = index + 1;
    return !otherLines.has(lineNumber) || selectedLines.has(lineNumber);
  }).join('\n').trim();

  if (!filtered) {
    throw new Error('生成投标范围工作副本失败，请重新提取标段');
  }
  return filtered;
}
```

**保留 = 去掉「属于其它标段的行」+ 保留「不属于任何标段的行」（通用条款）**。结果覆盖写入 `tenderMarkdownPath`，原文另存 `tender_original_markdown_path`（`technicalPlanStore.cjs:2647`, `readOriginalTenderMarkdown:781-793`）。

⚠️ **行号必须与当前 `readOriginalTenderMarkdown()` 的行严格对齐**。若解析产物在识别之后被重新生成，行号全部失效且无告警。

---

## 8. 提示词顺序优化在代码里的落地

### 8.1 四层消息结构

```
[0] system   stableSystemPrompt              通用纪律（约束语言/缺失标记/不编造）
[1] system   sectionHint（可选）              作用域限定（"只看这个标段"）
[2] user     招标文件全文                     语料（带"以下是完整招标文件…"引导句）
[3] user     buildTaskPrompt(task)            任务指令（追加"整体无结果规则"）
```

代码依据：`bidAnalysisTask.cjs:202-219`（见 §4.2）。

**优化点 1 —— 作用域约束用「独立 system 消息」而非拼进第一条**（`:206-208`）。合并阶段同样保持这个分层（`segmentedAiResultMerger.cjs:12-19`）：

```js
const messages = [];
if (systemPrompt) {
  messages.push({ role: 'system', content: systemPrompt });
}
if (sectionHint) {
  messages.push({ role: 'system', content: sectionHint });
}
```

标段识别的合并阶段同样先 system 后 user（`bidSectionExtractionTask.cjs:167-186`）。

**优化点 2 —— 任务指令放最后一条 user**（`:215-217`），利用「近因效应」让任务成为最新上下文。

**优化点 3 —— 通用纪律与任务规则分离**：通用纪律复用同一个常量（`stableSystemPrompt`，全 18 项共享同一字符串 → **前缀缓存可命中**）。

### 8.2 但服务层会再压平一层 ⚠️

```js
// client/electron/services/aiService.cjs:200-221（注释与实现）
// 校验多模态能力，并将本地图片串行转换为 OpenAI Chat Completions 图片内容块。
async function prepareMultimodalMessages(config, messages) {
  ensureMultimodalEnabled(config, messages);
  // 部分推理服务（如 LM Studio 加载 Qwen3 系列模型，官方 chat template 硬校验）要求
  // system 消息必须位于首位，否则直接报错。发送前把所有 system 消息按原顺序合并为
  // 一条并置于最前，其余消息保持原顺序。
  const sourceMessages = Array.isArray(messages) ? messages : [];
  const systemParts = sourceMessages
    .filter((message) => message?.role === 'system')
    .map((message) => message.content)
    .filter((content) => Array.isArray(content) ? content.length > 0 : content?.trim());
  const nonSystemMessages = sourceMessages.filter((message) => message?.role !== 'system');
  // 消息之间保留空行；结构化消息内部的内容块保持原样，供后续图片转换使用。
  const systemContent = systemParts.some(Array.isArray)
    ? systemParts.flatMap((content, index) => [
      ...(index > 0 ? [{ type: 'text', text: '\n\n' }] : []),
      ...(Array.isArray(content) ? content : [{ type: 'text', text: content }]),
    ])
    : systemParts.join('\n\n');
  const normalizedMessages = systemParts.length
    ? [{ role: 'system', content: systemContent }, ...nonSystemMessages]
    : nonSystemMessages;
```

👉 **两条独立 system 消息在发往模型前被合并成一条**，用 `\n\n` 分隔。业务代码的「消息层级优化」在最后一跳被**结构性抹平**（为兼容 LM Studio/Qwen3 的 chat template 硬校验）。

→ 净效果：最终请求形如

```
system:<stableSystemPrompt>\n\n<sectionHint>
user:   以下是完整招标文件。…\n\n<全文>
user:   <任务指令 + 整体无结果规则>
```

**顺序（纪律 → 作用域 → 语料 → 指令）与 4 层设计一致，只是 system 层的两条合成了一条。** 这是一次**有意的、且已在注释中记录理由的降级**，不是 bug。

### 8.3 标段识别侧的分层（无 system 纪律层）

标段识别只有 2 层（system 专家身份 + user 任务），没有 `stableSystemPrompt`，也没有合并阶段复用的纪律层（`bidSectionExtractionTask.cjs:121-163`、`:166-187`）。

## 9. 容错：逐处 try/catch、fail-open/closed、重试、哨兵值

### 9.1 提取编排层

| 位置 | 机制 | 判定 |
|---|---|---|
| `bidAnalysisTask.cjs:283-285` | 上传前置 | `throw new Error('请先上传招标文件，再开始解析')` — **fail-closed** |
| `:287-298` | 多标段前置（4 道门） | 未识别成功 / 标段数 <2 / 未选标段 / 选中标段已失效 → 逐条 throw — **fail-closed** |
| `:397-400` | 空结果 | `if (!trimmedContent) throw new Error(\`${task.label}解析结果为空，请重新解析\`)` — **fail-closed** |
| `:427-435` | 单项隔离 | `runOneSafely` catch 全部异常 → 置 error → `return false`，**不影响其它项**（fail-open per item） |
| `:416-425` | 错误态保留 | error 项保留 `content: currentTasks[task.id]?.content || ''` — **部分结果不丢** |
| `:452-458` | 必填门禁 | 任一 required 未 success 且 content 非空 → 整任务 `status:'error'`，**不做部分成功上报** — **fail-closed** |
| `:262-266` | 缺失重跑 | 首次返回 `未提取到` → 无条件重跑一次，**第二次结果原样上抛**（不再判缺失） — **重试上限 1 次** |
| `:370` | 断点续跑 | `scopedTasks.filter((task) => currentTasks[task.id]?.status !== 'success')` — **跳过已 success** |
| `:308, 336-364` | forceRerun | `payload.force_rerun === true \|\| payload.forceRerun === true`（两种拼写）；重置全部项为 idle **并清空 8 个下游字段** |
| `:315-317` | 非法重跑目标 | `requestedTaskIds` 过滤后为空 → throw `未找到可重新解析的招标文件解析项` — **fail-closed** |
| `:437-449` | 预热项先行 | `projectOverview` 先跑，成功后**固定等 5000ms** 再并发其余 |
| `:318-321` | 进度 | `Math.round(done / selectedTasks.length * 100)`，分母恒为 `selectedTasks`（不会因过滤而变） |

⚠️ **`getMissingRequiredTasks` 用的是 `tasks`（全部 18 项）而非 `selectedTasks`**：

```js
// client/electron/services/bidAnalysisTask.cjs:323-325
function getMissingRequiredTasks(nextTasks) {
  return tasks.filter((task) => task.required && !(nextTasks[task.id]?.status === 'success' && String(nextTasks[task.id]?.content || '').trim()));
}
```

→ **即使在 `mode='custom'` 只跑部分项时，全部 6 个必选项仍被计入门禁**，未跑到的必选项会导致整任务 error。

### 9.2 AI 调用层

| 位置 | 机制 |
|---|---|
| `aiService.cjs:1382-1390` | 缺 api_key / model_name / base_url → 中文 Error — fail-closed |
| `:1417-1428` | `response_format` 不支持 → 删字段重发一次（**该次重发在 `runWithAiRetry` 内部，即不受重试次数影响**） |
| `:832-888` | JSON 校验失败 → 修复调用 → 重试（共 3 轮）；末轮 `throw new Error(failureMessage)`，**丢弃 lastError** |
| `:774-780` | `emitProgress` 无 callback 时**静默 return** |
| `aiLog.cjs:72-74` | `writeAiLog` 整体包 try/catch，注释：`// AI 请求日志不能影响模型调用。` — **fail-open** |
| `aiLog.cjs:56-58` | **非开发者模式直接 return，不落任何日志**（`if (!config?.developer_mode) return;`） |
| `aiHttpError.cjs:96-101` | HTML 错误页 → 向所有窗口 `webContents.send('ai:http-error', …)` |

### 9.3 文件解析层

| 位置 | 机制 |
|---|---|
| `fileService.cjs:529-537` | 格式不支持 → throw（用户可见文案）— fail-closed |
| `:555-564` | 解析异常 → **先 `deleteImportedImageAssets(assets)` 清图片目录**再抛；`deleteImportedImageAssets` 本身 `.catch(() => undefined)` — 清理 fail-open |
| `:610-643` | 多文件循环，**逐文件 catch 进 `errors[]` 后 `continue`** — **部分成功**（fail-open per file） |
| `:645-647` | `parsedDocuments.length === 0` → `{ success:false, message: errors[0] \|\| … }` |
| `:651` | 有回退则追加 `当前格式已自动使用本地解析` |
| `convert.mjs:597-603` | `safePdfCall` — **表格/图片抽取失败静默返回 null**，不影响文本层 — **fail-open** |
| `convert.mjs:585-589` | 文本层无有效内容 → 抛 `pdf_text_layer_missing` — fail-closed |
| `convert.mjs:1024` | 某页无内容 → 静默跳过该页（**页码不连续，无提示**） |
| `convert.mjs:1191-1193` | `.doc` 多后端依次尝试，**全失败才抛** `createOfficeConversionFailedError`（汇总 attempts） — 逐级 fail-open |
| `documentParseErrors.cjs:15-28` | LibreOffice 缺失 → 改写为带下载链接的友好 Error，**保留原 stack** |
| `fileService.cjs:128-130` | zip 结构错误 → 改写为「不是有效的 DOCX 文档，请用 Word/WPS 另存为标准 DOCX」 |
| `fileService.cjs:392-403` | 远程图片 10s 超时（`AbortController`），`response.ok === false` 或非 `image/*` → `return null` — fail-open |

### 9.4 标段提取层

| 位置 | 机制 |
|---|---|
| `bidSectionExtractionTask.cjs:190-197` | `collectJson` 三级 fallback：`collectJsonResponse` → `requestJson` → throw `AI 服务尚未初始化` |
| `:203-204` | 原文为空 → throw — fail-closed |
| `:222` | `totalLines` 由转换后 Markdown 行数得出，**未与 AI 报告的行号范围交叉核对总数** |
| `:18-20`（`normalizeLineRange`） | 行号越界/倒序 → **丢弃该 range**（fail-closed per range） |
| `:63`（`normalizeSection`） | 无 title → 返回 `null` → 被 `.filter(Boolean)` 丢弃 |
| `:100` | `includeRanges` 为空的标段 → **丢弃** |
| `:116-118` | `< 2` 个标段 → throw — fail-closed |
| `:258-265` | 整体 try/catch → `checkpointTask({status:'error', progress:100, error: message, …})`，**并把 `bidSectionExtractionStatus` 也置 error** |
| `technicalPlanStore.cjs:792` | 原文文件缺失 → throw `原始招标文件缺失，请重新上传招标文件` |
| `technicalPlanStore.cjs:268-270` | 选中标段无有效范围 → throw `当前标段缺少有效范围，请重新识别` |
| `technicalPlanStore.cjs:286-288` | 过滤后为空 → throw |

### 9.5 日志与告警文案（原文）

```js
// client/electron/services/bidSectionExtractionTask.cjs:3-6
function pushLog(logs, message) {
  logs.push(message);
  return logs.slice(-80);          // ← 最近 80 条
}
```

```js
// client/electron/services/bidSectionExtractionTask.cjs:221,226,237,251
log('开始识别招标文件中的标段范围。', 5);
log(`招标文件已按上下文拆分为 ${sourceSegments.length} 段，正在提取标段候选。`, 12);
log(`已完成第 ${index + 1}/${sourceSegments.length} 段标段候选提取。`, Math.min(80, 12 + Math.round(((index + 1) / sourceSegments.length) * 60)));
const finalLogs = pushLog(logs, `已识别 ${merged.sections.length} 个标段，请选择本次投标范围。`);
```

```js
// client/electron/services/bidAnalysisTask.cjs:327-332, 445, 455, 460
const initialMessage = requestedTaskIds
  ? '开始重新解析选中的招标文件解析项。'
  : forceRerun
    ? '开始重新解析全部招标文件解析项。'
    : '开始解析招标文件。';
…
logs: ['提示词缓存预热完成，等待 5 秒后开始并发解析剩余项。'],
…
const message = `必填解析项未完成：${missingLabels}，请重新解析失败项。`;
checkpointTask({ status: 'error', progress: 100, error: message, logs: [message] });
…
checkpointTask({ status: 'success', progress: 100, error: undefined, logs: ['招标文件解析完成。'] });
```

```js
// client/electron/services/bidAnalysisTask.cjs:421
logs: [`${task.label}解析失败：${error.message || '未知错误'}`],
```

```js
// client/electron/services/taskService.cjs:1155, 1194（启动恢复）
const message = '上次招标文件解析未完成，请重新解析';
const message = '上次多标段识别未完成，请重新识别';
```

任务日志的 80 条上限有**双重保障**（业务侧 `slice(-80)` + 存储侧 `MAX_TASK_LOGS = 80`）：

```js
// client/electron/services/taskLogStore.cjs:1,3-11
const MAX_TASK_LOGS = 80;
function normalizeLogs(logs) {
  const normalized = [];
  for (const value of Array.isArray(logs) ? logs : []) {
    const message = String(value || '').trim();
    if (!message || normalized.at(-1) === message) continue;   // ← 去重相邻重复
    normalized.push(message);
  }
  return normalized.slice(-MAX_TASK_LOGS);
}
```

### 9.6 哨兵值清单

| 哨兵 | 位置 | 语义 |
|---|---|---|
| `'未提取到'` | `bidAnalysisTask.cjs:6` / `bidAnalysisWorkflow.ts:13` | Markdown 整项无结果（**与 `'没有提及'` 严格区分**） |
| `'没有提及'` | `bidAnalysisTask.cjs:195`, `stableSystemPrompt:17` | 局部/分类缺失 |
| `'"[表格数据]"'` | `bidAnalysisWorkflow.ts:87` | 表格来源标记 |
| `{"sections":[]}` | `bidSectionExtractionTask.cjs:139` | 本段无标段 |
| `'-原文未提及'` | `analysisPrompts.ts:11` | 废标项无原文依据 |
| `'- 重点补充：'` | `analysisPrompts.ts:32, 35` | 经验补充项前缀 |
| `'- 暂未发现必须补充的高风险项'` | `analysisPrompts.ts:14` | 无经验补充项 |
| `id: 'section-1'` / `section-${index+1}` | `bidSectionExtractionTask.cjs:71, 102` | 标段 id（**最终按排序重排**） |
| `'招标文件解析结果合并'` | `segmentedAiResultMerger.cjs:29` | 合稿任务缺名兜底 |
| `'AI 请求队列已暂停'` / `AI_QUEUE_SCOPE_PAUSED` | `aiRequestQueue.cjs:1, 9-11` | 主动暂停，**不重试** |

### 9.7 缺失判定的「两份实现」⚠️

```js
// client/electron/services/bidAnalysisTask.cjs:198-200（Main）
function isMissingMarkdownResult(task, content) {
  return task.output === 'markdown' && String(content || '').trim() === MARKDOWN_MISSING_RESULT;
}
```

```ts
// client/src/features/technical-plan/services/bidAnalysisWorkflow.ts:16-26（渲染进程）
export function isMissingBidAnalysisResult(task: BidAnalysisTaskDefinition | undefined, content: string | undefined) {
  return task?.output === 'markdown' && String(content || '').trim() === BID_ANALYSIS_MISSING_RESULT;
}
// 按解析协议识别整项缺失或仅技术评分项缺失；与 Main 的同名判断保持一致。
export function isMissingTechnicalScoreItems(content: string | undefined) {
  const text = String(content || '').trim();
  if (text === BID_ANALYSIS_MISSING_RESULT) return true;
  const section = text.match(/^##[\t ]+技术评分项[\t ]*\r?\n([\s\S]*?)(?=^#{1,2}[\t ]|$(?![\s\S]))/m);
  return section?.[1].trim() === '没有提及';
}
```

注释自称「与 Main 的同名判断保持一致」，但 **Main 侧不存在 `isMissingTechnicalScoreItems`**（全仓 grep 仅命中 TS 一处定义 + `TechnicalPlanHome.tsx:365` 一处调用）。**技术评分项的「小节级缺失」判定只在渲染进程存在**，依赖 Main 提示词里的 `## 技术评分项` 标题（`:101`）。

`isMissingTechnicalScoreItems` 的唯一消费点：

```ts
// client/src/features/technical-plan/pages/TechnicalPlanHome.tsx:364-365
const technicalScoreMissing = state.bidAnalysisTasks.techRequirements?.status === 'success'
  && isMissingTechnicalScoreItems(state.bidAnalysisTasks.techRequirements.content);
```

→ **只影响前端流程门禁，不影响 Main 的必填门禁**（后者只看 `content.trim()` 非空，`bidAnalysisTask.cjs:324`）。即：一个只输出 `## 技术评分项\n没有提及\n## 技术评分要求\n没有提及` 的结果，**Main 判成功，前端判「技术评分缺失」**。

## 10. 「标书/投标/招标」业务语义耦合点汇总

> 本节是重点。每条给出**原文引用 + 行号 + 语义角色**。逐条可执行的改写清单见文末「标书语义耦合点清单」。

### 10.1 A 类：提示词内的业务语义（必须逐字改写）

| 位置 | 原文 | 语义角色 |
|---|---|---|
| `bidAnalysisTask.cjs:12` | `你是专业的投标资料分析助手。请严格基于用户提供的上下文完成提取和总结。` | **全局角色定义** —— 18 项全部继承 |
| `bidAnalysisTask.cjs:28` | `3. 招标文件中没有的字段填充"没有提及"。` | **jsonTask 硬编码「招标文件」** |
| `bidAnalysisTask.cjs:37` | `任务：提取并分析招标文件中的"无效投标"和"废标项"。` | 废标项提取任务 |
| `bidAnalysisTask.cjs:40` | `"无效投标"指投标人、投标文件、签章密封、递交时间、报价、保证金、资格条件、实质性响应等原因…` | 概念边界 |
| `bidAnalysisTask.cjs:41` | `"废标项"指可能导致项目废标、采购失败、重新招标、终止评审、有效投标人不足…` | 概念边界 |
| `bidAnalysisTask.cjs:42` | `"招标文件"使用"否决投标""投标无效""不予受理""无效响应""重大偏差""实质性偏离""废标情形"等同义表达时…` | 同义词表 |
| `bidAnalysisTask.cjs:47` | `"此类标书还可能涉及的"需要根据你的经验，补充招标文件中未明确提及、但结合本招标文件类型和招投标经验判断非常重要的高风险遗漏项。` | 依赖领域经验 |
| `bidAnalysisTask.cjs:54,62` | `# 招标文件中明确提到的` / `# 此类标书还可能涉及的` | **输出格式一级标题**（下游按标题解析） |
| `bidAnalysisTask.cjs:73,78` | `label: '项目概述'`、`尽量使用招标文件中的内容；只关注与项目实施有关的内容，不提取商务信息` | |
| `bidAnalysisTask.cjs:81` | `label: '技术评分要求'`、`description: '提取技术评分项、权重分值、评分标准和招标文件中的位置。'` | |
| `bidAnalysisTask.cjs:82-90` | `按语义区分"技术评分项"和"技术评分要求"`、`"评标方法""评分标准""技术参数""技术要求""技术方案""技术部分""评审要素"`、`"技术评分项：指投标人需要在技术方案中一一响应…"`、`"偏离扣分规则"` | **整段依赖评分语义** |
| `bidAnalysisTask.cjs:96-106` | `【权重/分值】【评分标准】【数据来源】`、`## 技术评分项` / `## 技术评分要求` | **输出结构被判定函数依赖** |
| `bidAnalysisTask.cjs:111` | `label: '甲方信息'`、`description: '招标人公司、地址、联系人和电话。'` | |
| `bidAnalysisTask.cjs:112` | `label: '交货和服务要求'`、`"采购清单"`/`"分项报价"`/`"工程量清单"` | |
| `bidAnalysisTask.cjs:115,117,120-124` | `提取招标文件、询比文件或采购文件中的采购清单/采购需求信息`、`"报价清单、分项报价、工程量清单"` | 货物采购语义 |
| `bidAnalysisTask.cjs:128-142` | `id: 'responseFileRequirements'`、`label: '响应文件要求'`、`"响应文件、投标文件、报价文件、资格证明文件、商务响应、技术响应、偏离表…签字盖章、密封上传"`、`"装订/密封、上传格式、份数、递交截止时间、递交方式"` | **投标文件编制语义，耦合最深** |
| `bidAnalysisTask.cjs:144` | `id: 'agentInfo'`、`label: '代理机构信息'`、`"银行账户名称/账号/开户行/开户行地址"` | |
| `bidAnalysisTask.cjs:145` | `id: 'keyInfo'`、`label: '投标关键节点'`、`"招标公告发布日期、招标文件获取方式、售价…投标文件提交地点、投标截止时间、开标时间、开标地点"` | |
| `bidAnalysisTask.cjs:146` | `id: 'marginInfo'`、`label: '投标保证金'`、`"不予退还的情形"` | |
| `bidAnalysisTask.cjs:147` | `id: 'qualificationReview'`、`label: '资格性审查'`、`"关于投标人资格性审查的信息"` | |
| `bidAnalysisTask.cjs:148` | `id: 'complianceCheck'`、`"文件完整性、文件有效性、文件规范、偏差处理"` | |
| `bidAnalysisTask.cjs:149` | `id: 'openBid'`、`"无效标认定、异议处理、开标流程"` | |
| `bidAnalysisTask.cjs:150` | `id: 'evaluationBid'`、`label: '评标要求'`、`"评标委员会组成、职责、评分构成、评标方法类型、评标原则"` | |
| `bidAnalysisTask.cjs:151` | `id: 'businessScoring'`、`"为编写投标文件中的商务方案做准备"` | |
| `bidAnalysisTask.cjs:152` | `id: 'discardedBids'`、`label: '无效标与废标项'` | |
| `bidAnalysisTask.cjs:153` | `id: 'signingProcess'`、`label: '合同授予与签订'`、`"中标公示、合同签订、履约保证金、合同文本"` | |
| `bidAnalysisTask.cjs:154` | `id: 'terminationCondition'`、`label: '合同解除和终止'`、`"违约解除、不可抗力、合同终止、争议解决"` | |
| `bidAnalysisTask.cjs:209` | `以下是完整招标文件。后续任务需要基于这份招标文件完成…` | **用户消息引导语** |
| `bidAnalysisTask.cjs:225,245,257` | `logTitle: 招标解析-${task.label}` / `-第${index+1}段` / `招标解析合并-${task.label}` | **AI 日志标题**（开发模式下的文件名） |
| `segmentedAiResultMerger.cjs:27` | `以下内容来自同一份招标文件按段分别解析后的结果。` | 合稿引导语 |
| `segmentedAiResultMerger.cjs:29` | `当前合并任务：${taskLabel \|\| '招标文件解析结果合并'}` | |
| `bidSectionExtractionTask.cjs:125` | `你是严谨的招标文件多标段识别专家。` | |
| `bidSectionExtractionTask.cjs:131` | `识别本段中明确属于某个标段/标包/分包/采购包/包件/标的的内容` | 6 类单位名词 |
| `bidSectionExtractionTask.cjs:170` | `你是严谨的招标文件多标段识别结果合并专家。` | |
| `bidAnalysisWorkflow.ts:36` | `3. 原文中没有的字段填充"没有提及"。` | TS 版差异 |
| `bidAnalysisWorkflow.ts:110` | `description: '招标人公司、地址、联系人和电话。'` | |
| `bidAnalysisWorkflow.ts:278` | `为编写投标文件中的商务方案做准备` | |
| `analysisPrompts.ts:2,5-7,11-12,21,29` | 见 §4.7 全文 | 废标项第二版 |
| `jsonRepairPrompts.ts:19` | `你是一个严格的 JSON 修复助手。…` | TS 版 |

### 10.2 B 类：结构标识与字段名（技术契约，改写需同步下游）

| 位置 | 标识 | 下游依赖点 |
|---|---|---|
| `bidSectionDetector` `:45,67-82` | 正则含 `标段\|包\|分包\|标包\|标的\|子项目` | 标段单位词表 |
| `bidSectionExtractionTask.cjs:73` | `unit: String(section?.unit \|\| '标段')` | 默认单位 |
| `technicalPlanStore.cjs:236` | `unit: String(section?.unit \|\| '标段')` | 存储默认单位 |
| `bidAnalysisWorkflow.ts:24` | `isMissingTechnicalScoreItems` 正则依赖 `## 技术评分项` 标题 | **改标题即失配** |
| 全部 jsonTask 字段键 | `bidding_deposit` / `bid_file_price` / `bid_opening_time` / `invalid_bid` / `performance_bond` 等 | `BidAnalysisPage.tsx:91-120` 字典 + 下游消费 |
| `bidSectionContext.cjs:26,28` | `本项目为多标段…当前选择标段：` | system 消息内容 |
| `taskService.cjs:30,39` | `label: '多标段识别'` / `label: '招标文件解析'` | 任务组锁文案 |
| `technicalPlanStore.cjs:761` | `fileName: meta.tender_file_name \|\| '技术方案招标文件'` | 默认文件名 |

### 10.3 C 类：UI 文案与告警

| 位置 | 原文 |
|---|---|
| `fileService.cjs:32` | `/** 把招标 Word 原件落到工作区；.doc/.wps 先转成 .docx。 */` |
| `fileService.cjs:583` | `const label = String(documentLabel \|\| '招标文件').trim() \|\| '招标文件';` |
| `fileService.cjs:679` | `const documentLabel = documentRole === 'bid' ? '投标文件' : '招标文件';` |
| `bidAnalysisTask.cjs:284` | `'请先上传招标文件，再开始解析'` |
| `bidAnalysisTask.cjs:289,292,296` | `'请先完成多标段识别…'` / `'请先选择本次投标范围…'` / `'当前投标范围已失效，请重新选择标段'` |
| `bidAnalysisTask.cjs:271` | `'未找到无效投标与废标项解析任务'` |
| `bidSectionExtractionTask.cjs:203` | `'请先上传招标文件，再进行多标段识别'` |
| `bidSectionExtractionTask.cjs:117` | `'未识别到至少两个有效标段'` |
| `technicalPlanStore.cjs:266,269,287` | `'未找到选择的投标范围'` / `'当前标段缺少有效范围，请重新识别'` / `'生成投标范围工作副本失败，请重新提取标段'` |
| `technicalPlanStore.cjs:792` | `'原始招标文件缺失，请重新上传招标文件'` |
| `technicalPlanStore.cjs:2643` | `'原始招标文件内容为空，请重新上传'` |
| `taskService.cjs:1155,1194` | `'上次招标文件解析未完成，请重新解析'` / `'上次多标段识别未完成，请重新识别'` |
| `taskService.cjs:673` | `当前${definition.groupLabel \|\| '任务组'}正在执行"${definition.label \|\| type}"，请等待当前任务完成后再重新分析新的文件集合。` |
| `BidSectionSelectorDialog.tsx:33-38` | `'选择投标范围'` / `'检测到招标文件包含多个标段或包，请选择本次投标范围。'` / `'检测到本招标文件共包含 N 个，请选择您要投标的范围。后续解析和生成将只关注该范围相关内容。'` |
| `BidAnalysisPage.tsx:30,35,71-73` | `'只解析关键项'` / `'完整解析'` / `'自定义解析'` |
| `BidAnalysisPage.tsx:79-81` | 分组名 `'投标流程'` / `'评标要求'` / `'主体与合同'` |
| `BidAnalysisPage.tsx:85-88` | `'待解析' / '解析中' / '已完成' / '失败'` |
| `BidAnalysisPage.tsx:267-273` | `'关键项已解析完成，等待当前解析任务结束后进入下一步。'` / `` `${firstMissingSelectedTask.label}未提取到有效内容，请重新解析该项。` `` / `'招标文件解析任务已结束，可以进入下一步。'` / `'等待关键解析项完成'` / `'多标段 · X' / '多标段 · 待选择' / '单标段'` |
| `BidAnalysisPage.tsx:321` | `'多标段识别失败，请重新识别或改用单标段解析'` |
| `BidAnalysisPage.tsx:336,353` | `'招标文件解析任务正在运行，请等待任务结束后再调整配置'` / `'招标文件解析配置已保存'` |
| `BidAnalysisPage.tsx:840-841` | `'系统检测到招标文件疑似包含多个标段，建议切换为多标段解析…'` / `'系统没有通过规则检测到明确多标段结构，是否仍继续使用 AI 识别多标段？'` |
| `technicalPlanIpc.cjs` | （**该文件含 1 处 GBK 乱码**，见 10.5） |

### 10.4 D 类：领域知识型常量（`unit` 值域）

`bidSectionExtractionTask.cjs:148` 的返回格式示例 `"unit": "标段"`，以及提示词 `:131` 列举的 `标段/标包/分包/采购包/包件/标的` —— **这是 6 个招投标专用单位词**，映射到建筑工程场景需重新定义（如「施工区段 / 单位工程 / 分部工程 / 专业工程 / 子项目」）。

### 10.5 ⚠️ 附带发现：编码异常（只读记录，未修改）

`client/electron/ipc/technicalPlanIpc.cjs` 有一处中文乱码（用 PowerShell 默认编码读取时显示为 `杩樻病鏈夋姇鏍囨ā鏈垨锛岃`）：

```js
return { success: false, message: '杩樻病鏈夋姇鏍囨ā鏈垨锛岃璇峰厛纭涓€绾х洰褰?' };
```

按上下文应为「还没有投标模板，请先确认上级目录」。**该文件应为 UTF-8 但内容疑似被按 GBK 解读过**（或反之）。按只读约束**未做任何修改**，仅记录。

### 10.6 改造时的三个「同源分叉」陷阱（改写时必须同步）

1. **任务定义两份**：`bidAnalysisTask.cjs:71-155`（Main 真正执行的）与 `bidAnalysisWorkflow.ts:44-314`（前端展示/判定）。二者**顺序、措辞、jsonTask 第 3 条、技术评分提示词结构、废标项提示词 9 条要求全部不同**。改一处不改另一处 → 用户看到的和 AI 收到的不是一回事。
2. **缺失字面量五种**：`未提取到` / `没有提及` / `招标文件未提及` / `原文未提及` / `本段未提及` / `- 原文未提及`。全仓只有 `segmentedAiResultMerger.cjs:32` 一处完整枚举了四种。改动任一字面量必须同步判定函数与合并提示词。
3. **JSON 字段字典两份**：`jsonTask` 第三参数（提示词里给 AI 的 schema）与 `BidAnalysisPage.tsx:91-120` 的 `jsonFieldLabels`（前端展示字典）。二者键名有出入（如 `agentInfo` 与 `partAInfo` 共享 `company_name/address/contact_person/contact_phone`），**前端无法区分这两个任务的同名字段**。

---

## 附：未能确认的事项（源码中未找到）

| 事项 | 状态 |
|---|---|
| `collect_json_response` 蛇形命名 | **源码中未找到**（只有 `collectJsonResponse`） |
| `bidSectionDetectionTask.cjs` / `bidSectionDetector*` 目录 | **不存在**，只有 3 个 `bidSection*` 文件 |
| Main 侧 `isMissingTechnicalScoreItems` | **源码中未找到**（只在 TS 侧定义） |
| `collectJsonResponse` 的 `validator` / `normalizer` 实际调用方 | **源码中未找到**（能力存在但无人使用） |
| 代码中的「自我反思五段式」提示词 | **源码中未找到**，仅 `文章\标书智能体（一）……md:175-210` |
| 提取项的 token 预算 / 上下文预算截断 | **源码中未找到**（全文原样送入，仅靠分段） |
| 单项失败的重试机制 | **源码中未找到**（单项只有「缺失重跑 1 次」，异常直接置 error） |

## 标书语义耦合点清单

> 本节是**逐条可执行版**：位置（file:line）+ 原文摘录 + 「若改写为**建筑工程专项施工方案**语义，应改为什么」。
> 目标域设定：源文档 = 招标文件/技术要求文件；提取对象 = 工程概况、工程目标、施工范围与技术要求、工期与进度、编制依据与技术规范、评审要点、项目管理组织、资源配置与设备、质量与安全/危大工程、绿色施工、验收与资料交付、合同与结算；作用域切分单位 = 单位工程 / 分部工程 / 专业工程 / 施工区段。
> ⚠️ 每条都标了「同步点」，改写时必须一并处理，否则会出现静默失配。

### 一、提取项清单（id / label / description）

| # | 位置 | 原文摘录 | 若改写为专项施工方案语义，应改为什么 | 同步点 |
|---|---|---|---|---|
| 1 | `bidAnalysisTask.cjs:73` / `bidAnalysisWorkflow.ts:46` | `id: 'projectOverview', label: '项目概述'`，`description: '提取项目基本信息、背景目的、规模预算、时间安排、实施内容和技术特点等。'` | `id: 'projectOverview', label: '工程概况'`，`description: '提取工程名称、建设单位、建设地点、结构类型/规模、工程投资、工期安排、施工内容与技术特点。'` | 两份 `tasks` 数组同步 |
| 2 | `bidAnalysisTask.cjs:78` | `只关注与项目实施有关的内容，不提取商务信息` | `只关注与施工组织、方案编制有关的内容，不提取商务报价信息` | 仅 Main（TS 版已有类似句 `:64`） |
| 3 | `bidAnalysisTask.cjs:81` / `:90` | `label: '技术评分要求'`，`description: '提取技术评分项、权重分值、评分标准和招标文件中的位置。'` | `label: '技术要求与评审要点'`，`description: '提取技术要求项、强制性等级、执行标准、判定口径和原文位置。'` | ⚠️ **必须同时改 `bidAnalysisWorkflow.ts:24` 的正则** |
| 4 | `bidAnalysisTask.cjs:110` / `:99-105` | `id: 'projectInfo'`，`{"project_name":"项目名称","project_number":"项目编号","project_type":"项目类型","project_budget":"项目预算","project_address":"项目地址"}` | `{"project_name":"工程名称","dwg_no":"图纸编号","project_type":"工程类型（如基坑/主体/装饰/市政）","budget":"合同价或造价指标","address":"施工地址"}` | ⚠️ 同步 `BidAnalysisPage.tsx:92-96` 的 `jsonFieldLabels` |
| 5 | `bidAnalysisTask.cjs:111` / `:109-118` | `id: 'partAInfo', label: '甲方信息'`，`{"company_name":"公司名称","address":"地址","contact_person":"联系人","contact_phone":"联系电话"}` | `id: 'ownerInfo', label: '建设单位信息'`，`{"company_name":"建设单位名称","address":"建设单位地址","contact_person":"联系人","contact_phone":"联系电话"}` | ⚠️ 与第 8 项 `agentInfo` 键名冲突，需加前缀区分 |
| 6 | `bidAnalysisTask.cjs:112` | `label: '交货和服务要求'`，`"implementation_period":"实施周期/工期/交付期限"`、`"warranty_period":"质保期"`、`"after_sales_service":"售后服务要求"` | `label: '工期、交付与售后要求'`，`"implementation_period":"工期/开工与竣工时间"`、`"warranty_period":"质量保修期"`、`"after_sales_service":"保修期内的响应与维修要求"` | 同步 `BidAnalysisPage.tsx` 字典 |
| 7 | `bidAnalysisTask.cjs:114-125` | `label: '采购清单'`，`"报价清单、分项报价、工程量清单"`、`"预算/限价"`、`"货物需求、服务内容"` | `label: '主要材料设备与工程量清单'`，`"主要材料设备清单、人工与机械台班、措施项目清单"`，`预算/限价` → `暂估/限价（如有）`，`货物需求` → `施工资源需求` | 两份 `tasks` 同步 |
| 8 | `bidAnalysisTask.cjs:144` | `id: 'agentInfo', label: '代理机构信息'`，`"bank_account_name":"银行账户名称"`…`"bank_account_address_detail":"银行账户开户行地址"` | `label: '参建单位与报审单位信息'`，**删除全部 4 个银行账户字段**，改为 `"supervision_unit":"监理单位","design_unit":"设计单位","survey_unit":"勘察单位","authority_unit":"质量安全监督机构"` | ⚠️ 银行账户字段在施工方案场景**完全无意义**，建议直接删 |
| 9 | `bidAnalysisTask.cjs:145` | `id: 'keyInfo', label: '投标关键节点'`，`"bid_announcement_time":"招标公告发布日期"`、`"bid_file_price":"招标文件售价"`、`"bid_submission_deadline":"投标截止时间"`、`"bid_opening_time":"开标时间"` | `id: 'milestones', label: '实施关键节点'`，`"bid_announcement_time"` → `"start_date":"计划开工日期"`，`"bid_submission_deadline"` → `"completion_date":"计划竣工日期"`，**删除 `bid_file_price` / `bid_opening_time` / `bid_opening_address`** | ⚠️ 同步 `BidAnalysisPage.tsx:106-112` 字典 |
| 10 | `bidAnalysisTask.cjs:146` | `id: 'marginInfo', label: '投标保证金'`，`"bidding_deposit":"投标保证金"`、`"non_refundable_conditions":"不予退还的情形"` | **整项建议删除**（专项方案编制不需要保证金）。若需保留造价信息，改为 `label: '造价与计量支付要求'`，字段取 `计量规则/支付节点/结算方式` | ⚠️ 删除需处理 `required:false` 的前端选择列表 |
| 11 | `bidAnalysisTask.cjs:147` | `id: 'qualificationReview', label: '资格性审查'`，`关于投标人资格性审查的信息` | `label: '编制资质与人员资格要求'`，`投标人` → `编制单位/项目负责人/技术负责人/专职安全员`，并追加 `安全生产考核合格证`、`注册建造师执业资格` | 两份同步 |
| 12 | `bidAnalysisTask.cjs:148` | `label: '符合性检查'`，`文件完整性、文件有效性、文件规范、偏差处理` | `label: '强制性与禁止性要求'`，改为 `强制性条文、禁止性规定、必须专项论证的危大工程清单、方案审批与备案要求` | 两份同步 |
| 13 | `bidAnalysisTask.cjs:149` | `label: '开标要求'`，`"invalid_bid":"无效标认定"`、`"bid_process":"开标流程"` | **整项建议删除**；如需保留现场维度，改为 `label: '现场条件与协调要求'`，字段 `现场条件`、`既有建筑与地下管线`、`交通与作业面限制`、`周边协调事项` | ⚠️ 删除需同步 `BidAnalysisPage.tsx:79` 分组 ids |
| 14 | `bidAnalysisTask.cjs:150` | `label: '评标要求'`，`"committee":"评标委员会组成"`、`"method":"评标方法类型"`、`"principles":"评标原则"` | `label: '评审要点与评标办法'`，`committee` → `evaluation_method`（综合评估法/经评审的最低价法…）、`scoring` → `scoring_breakdown`（各评分项权重）、`principles` → `technical_review_focus`（技术评审关注点） | ⚠️ 同步 `BidAnalysisPage.tsx` 字典 |
| 15 | `bidAnalysisTask.cjs:151` | `label: '商务评分要求'`，`为编写投标文件中的商务方案做准备` | `label: '资源与成本相关要求'`，`人工、材料、机械、临建、临时设施与周转材料的配置要求及费用口径` | 两份同步（`bidAnalysisWorkflow.ts:278`） |
| 16 | `bidAnalysisTask.cjs:152` | `id: 'discardedBids', label: '无效标与废标项'` | `id: 'riskItems', label: '编制风险与退回触发项'`（详见「四」节） | ⚠️ id 变更需历史数据迁移 |
| 17 | `bidAnalysisTask.cjs:153` | `label: '合同授予与签订'`，`"bid_notice":"中标公示"`、`"performance_bond":"履约保证金"` | `label: '合同与结算约定'`，`bid_notice` → `payment_terms`（付款方式与节点）；`performance_bond` 可保留为合同条款或删除 | ⚠️ 同步字典 |
| 18 | `bidAnalysisTask.cjs:154` | `label: '合同解除和终止'`，`"breach_termination":"违约解除"`、`"dispute_resolution":"争议解决"` | 可保留（合同维度在施工方案中确有约束），但建议**降级为可选项**并要求 AI 标注来源条款号 | 无 |

### 二、提示词正文（global system / jsonTask / 任务指令）

| # | 位置 | 原文摘录 | 若改写为专项施工方案语义，应改为什么 | 同步点 |
|---|---|---|---|---|
| 19 | `bidAnalysisTask.cjs:12` | `你是专业的投标资料分析助手。请严格基于用户提供的上下文完成提取和总结。` | `你是专业的建筑工程施工资料分析助手。请严格基于用户提供的上下文完成提取和总结。` | **仅此一处，18 项全部继承** |
| 20 | `bidAnalysisTask.cjs:17` | `已提取到相关内容但局部信息没有提及时，明确写"没有提及"` | 中性，**建议保留**（与判定函数耦合） | ⚠️ 不可改字面量 |
| 21 | `bidAnalysisTask.cjs:28` | `3. 招标文件中没有的字段填充"没有提及"。` | `3. 原文中没有的字段填充"没有提及"。`（与 TS 版 `:36` 统一，**顺带消除两份分叉**） | `bidAnalysisWorkflow.ts:36` |
| 22 | `bidAnalysisTask.cjs:209` | `以下是完整招标文件。后续任务需要基于这份招标文件完成；如后续消息提供补充上下文，请按具体任务要求综合使用：` | `以下是完整源文档（招标文件 / 技术要求文件）。后续任务需要基于这份源文档完成；如后续消息提供补充上下文，请按具体任务要求综合使用：` | 仅 Main |
| 23 | `bidAnalysisTask.cjs:115,117` | `任务：提取招标文件、询比文件或采购文件中的采购清单/采购需求信息。` / `请从招标文件中识别…` | `任务：提取源文档中的主要材料设备清单与工程量清单。` / `请从原文中识别…` | 两份同步 |
| 24 | `bidAnalysisTask.cjs:120,122,123` | `不要自行补充招标文件没有的信息。` / `不要避免编造不存在的字段` / `按招标文件实际出现的信息组织` | `不要自行补充原文没有的信息。` / `不要编造不存在的字段` / `按原文实际出现的信息组织` | 仅 Main |
| 25 | `bidAnalysisTask.cjs:124` | `如果没有找到明确采购清单，请说明"未找到明确采购清单"` | `"未找到明确的材料设备与工程量清单"` | 仅 Main |
| 26 | `bidAnalysisTask.cjs:129,131` | `任务：提取招标文件、询比文件或采购文件中关于响应文件/投标文件编制与提交的要求。` / `请识别与"响应文件、投标文件、报价文件、资格证明文件、商务响应、技术响应、偏离表、响应文件格式、投标文件格式、递交要求、签字盖章、密封上传"等含义相近的内容。` | `任务：提取源文档中关于专项施工方案编制与报送的要求。` / `请识别与"专项施工方案、施工组织设计、技术方案、方案编制要求、必备章节、计算书、专项论证、附件要求、编制依据、审批与备案"等含义相近的内容。` | 两份同步（`bidAnalysisWorkflow.ts:162,164`） |
| 27 | `bidAnalysisTask.cjs:135` | `2. 重点提取响应文件需要包含哪些部分，例如报价文件、商务文件、技术文件、资格证明、承诺函、授权委托书、响应表、偏离表、分项报价表等。` | `2. 重点提取专项方案需要包含哪些部分，例如编制依据、工程概况、施工部署与进度计划、施工工艺与技术措施、资源配置、质量与安全保证、危大工程管理与专项论证、验收与资料交付、计算书与附图等。` | 仅 Main（TS 版缺此条） |
| 28 | `bidAnalysisTask.cjs:137` | `4. 提取签字盖章、文件命名、装订/密封、上传格式、份数、递交截止时间、递交方式等要求。` | `4. 提取方案文件的编制人/审核人/审批人签署要求、文件命名、装订与份数、报送单位与截止时间、报送方式（纸质/电子/系统）等要求。` | 仅 Main |
| 29 | `bidAnalysisTask.cjs:138` | `5. 保持投标文件中所列的响应文件顺序，保证后续编写响应文件时，可以直接按照你提取的结果一一对应编写。` | `5. 保持原文中所列章节顺序，保证后续编写专项方案时可以直接按提取结果一一对应编写。` | 仅 Main |
| 30 | `bidAnalysisTask.cjs:139` | `6. 区分"必须提供"和"如适用/可选提供"的内容；如果招标文件没有明确区分，不要自行判断。` | `6. 区分"必须包含"和"如适用/可选包含"的内容；如果原文没有明确区分，不要自行判断。` | 仅 Main |
| 31 | `bidAnalysisTask.cjs:140` | `7. 不要生成供应商自己的最终响应文件，不要编造公司信息、报价、资质、承诺内容。` | `7. 不要生成施工单位自己的最终方案正文，不要编造企业资质、人员业绩、工程参数与承诺内容。` | 仅 Main（TS 版缺此条） |
| 32 | `bidAnalysisTask.cjs:141` | `8. …请说明"未找到明确响应文件要求"，并列出可能相关的投标/响应文件格式段落摘要。` | `8. …请说明"未找到明确的方案编制要求"，并列出可能相关的方案编制格式段落摘要。` | 仅 Main |
| 33 | `bidAnalysisTask.cjs:147` | `关于投标人资格性审查的信息` | `关于编制单位资质与项目负责人资格要求的信息` | 仅 Main |
| 34 | `bidAnalysisTask.cjs:148` | `文件完整性、文件有效性、文件规范、偏差处理等` | `强制性条文、禁止性规定、方案审批与论证要求、危大工程专项论证要求等` | 仅 Main |
| 35 | `bidAnalysisTask.cjs:151` | `提取招标文件中的商务评分因素，为编写投标文件中的商务方案做准备。` | `提取原文中与资源投入、人工与机械配置、临建与临时设施、费用口径相关的要求，为编写专项方案的资源配置章节做准备。` | 仅 Main（TS `:278` 同步） |
| 36 | `bidAnalysisTask.cjs:153` | `提取中标公示、合同签订、履约保证金、合同文本等信息` | `提取付款方式与节点、计量与结算方式、合同价款调整条款、合同文本要求等信息` | 仅 Main |
| 37 | `bidAnalysisTask.cjs:154` | `提取违约解除、不可抗力、合同终止、争议解决等信息` | 可保留（改 `争议解决` → `争议解决与索赔`），但建议标注来源条款号 | 仅 Main |

### 三、技术评分要求（`techRequirements`）—— 耦合最深，改写风险最高

| # | 位置 | 原文摘录 | 若改写为专项施工方案语义，应改为什么 | 同步点 |
|---|---|---|---|---|
| 38 | `bidAnalysisTask.cjs:82` | `任务：提取技术评分信息，并按语义区分"技术评分项"和"技术评分要求"。` | `任务：提取技术要求与评审要点，并按语义区分"具体技术要求项"和"通用技术规则"。` | 两份同步 |
| 39 | `bidAnalysisTask.cjs:84` | `重点识别"技术评分""评标方法""评分标准""技术参数""技术要求""技术方案""技术部分""评审要素"相关章节，不要提取商务、价格、资质等无关条目。` | `重点识别"技术要求""技术标准""施工工艺""质量标准""安全要求""环境保护""验收标准""编制依据""评审要素"相关章节，不要提取商务报价、企业资质、人员业绩等无关条目。` | 两份同步 |
| 40 | `bidAnalysisTask.cjs:87` | `1. 技术评分项：指投标人需要在技术方案中一一响应、展开编写，并可对应形成技术方案章节的具体评分内容，例如方案类、措施类、团队类、实施类、服务类、保障类、运维类、应急类、检查类等评分内容。` | `1. 具体技术要求项：指需要在专项施工方案中逐条响应、展开编写，并可对应形成方案章节的具体要求内容，例如施工部署类、工艺措施类、资源配置类、质量控制类、安全管理类、环境保护类、应急处置类、进度保证类、验收检查类等。` | 仅 Main |
| 41 | `bidAnalysisTask.cjs:88` | `2. 技术评分要求：指用于约束评分、解释评分、定义扣分或判定规则的通用规则或说明，例如符合性要求、偏离扣分规则、判定口径、适用范围说明、表后说明、通用评审规则等。` | `2. 通用技术规则：指用于约束执行、解释口径、定义合格判定或处理原则的通用规则或说明，例如强制性要求、适用范围说明、表后说明、通用施工工艺原则、通用验收判定口径、强制性条文等。` | 仅 Main |
| 42 | `bidAnalysisTask.cjs:89` | `3. 判断依据是该内容是否要求投标人在技术方案中展开具体方案内容；如果不是具体方案内容，即使带有分值或扣分规则，也归入技术评分要求。` | `3. 判断依据是该内容是否要求编制人在专项方案中展开具体做法；如果不是具体做法，即使附带分值或扣分规则，也归入通用技术规则。` | 仅 Main |
| 43 | `bidAnalysisTask.cjs:90` | `4. 若原文存在层级关系，请保持顺序和来源，不要自行合并不相关条款。` | 中性，保留 | 仅 Main |
| 44 | `bidAnalysisTask.cjs:94` / `:101` | `## 技术评分项` / `## 技术评分要求` | `## 技术要求项` / `## 通用技术规则` | ⚠️ **必须同步 `bidAnalysisWorkflow.ts:24` 的正则**，否则前端把全部结果误判为「技术评分缺失」 |
| 45 | `bidAnalysisTask.cjs:96-99` | `【评分项名称】`/`【权重/分值】：<具体分值或占比>`/`【评分标准】：<详细规则>`/`【数据来源】` | `【要求项名称】`/`【强制等级】：<强制性/重要/一般>`/`【执行标准】：<可引用的标准号与判定规则>`/`【数据来源】：<章节、条款号、页码或表格位置>` | ⚠️ `【权重/分值】` 被 TS 版 `:82` 同样使用 |
| 46 | `bidAnalysisTask.cjs:103-106` | `【评分要求名称】`/`【适用范围】：<适用于哪些评分项或评审环节>`/`【要求/判定口径】` | `【规则名称】`/`【适用范围】：<适用于哪些工序、部位或验收环节>`/`【要求/判定口径】` | 仅 Main |
| 47 | `bidAnalysisTask.cjs:108` | `若某一类没有内容，请保留对应标题并写"没有提及"。` | 中性，**保留**（与判定函数 `:25` 耦合） | 不可改 |
| 48 | `bidAnalysisWorkflow.ts:73` | `任务：提取技术评分要求。` | `任务：提取技术要求与评审要点。` | — |
| 49 | `bidAnalysisWorkflow.ts:86` | `1. 若没有明确"技术评分表"，根据上下文判断技术评分相关内容。` | `1. 若没有明确的"技术要求表"，根据上下文判断技术要求相关内容。` | — |
| 50 | `bidAnalysisWorkflow.ts:87` | `2. 若评分项以表格形式呈现，按行提取，并标注"[表格数据]"。` | `2. 若要求项以表格形式呈现，按行提取，并标注"[表格数据]"。` | `[表格数据]` 哨兵保留 |
| 51 | `bidAnalysisWorkflow.ts:89` | `4. 单位尽量统一为"分"或"%"，必要时注明原文单位。` | **整条删除**（施工方案域无分值单位）。建议替换为：`4. 引用标准编号时统一写为"标准号 年号"，必要时保留原文表述。` | 仅 TS |
| 52 | `bidAnalysisWorkflow.ts:82` | `【权重/分值】：<具体分值或占比>` | `【强制等级】：<强制性/重要/一般>` | ⚠️ 与第 45 条同一字段 |
| 53 | `文章\标书智能体（一）…md:176-209` | `### 4. 输出示例` / `### 5. 验证步骤`（含 `- [ ] 权重总和是否与文档声明的技术分总分一致（如"技术部分共60分"）？`） | 若要引入"自我反思五段式"：**第 4 段输出示例改为施工语义**（如`【要求项名称】：基坑支护与降水`/`【强制等级】：强制性`/`【执行标准】：JGJ 120，支护结构…`）；**第 5 段验证步骤改为**`- [ ] 强制性条文是否全部覆盖？` `- [ ] 危大工程是否已识别并要求专项论证？` | ⚠️ 当前代码中**不存在**该五段式，引入是新增能力 |

### 四、无效标与废标项（`discardedBids`）—— 招投标特有，语义整体不存在

| # | 位置 | 原文摘录 | 若改写为专项施工方案语义，应改为什么 | 同步点 |
|---|---|---|---|---|
| 54 | `bidAnalysisTask.cjs:37` / `analysisPrompts.ts:2` | `任务：提取并分析招标文件中的"无效投标"和"废标项"。` | `任务：提取并分析源文档中的"编制风险项"和"方案退回触发项"。` | 两份同步 |
| 55 | `bidAnalysisTask.cjs:40` | `1. "无效投标"指投标人、投标文件、签章密封、递交时间、报价、保证金、资格条件、实质性响应等原因导致投标被认定为无效、否决、不予受理或按无效响应处理的情形。` | `1. "编制风险项"指因资料缺失、依据不足、参数与现场条件不符、与强制性条文冲突、计算书缺失等原因导致专项施工方案不满足审查要求、被要求补充或退回修改的情形。` | 两份同步 |
| 56 | `bidAnalysisTask.cjs:41` | `2. "废标项"指可能导致项目废标、采购失败、重新招标、终止评审、有效投标人不足或实质性响应不足的条款或风险项。` | `2. "退回触发项"指可能导致方案审查不通过、重新编制、专家论证不通过或施工报批受阻的条款或风险项。` | 两份同步 |
| 57 | `bidAnalysisTask.cjs:42` | `3. 招标文件使用"否决投标""投标无效""不予受理""无效响应""重大偏差""实质性偏离""废标情形"等同义表达时，也要按上述边界归类。` | `3. 源文档使用"不得""必须""应""严禁""不符合要求""不予受理""退回修改""重新编制""论证不通过""报审不通过"等同义表达时，也要按上述边界归类。` | 两份同步 |
| 58 | `bidAnalysisTask.cjs:45` / `analysisPrompts.ts:10` | `1. 必须明确区分"无效投标"和"废标项"。` | `1. 必须明确区分"编制风险项"和"退回触发项"。` | 两份同步 |
| 59 | `bidAnalysisTask.cjs:46` / `analysisPrompts.ts:11` | `"招标文件中明确提到的"…如果没有提及，写"招标文件未提及"。` / `"原文中明确提到的"…如果没有提及，写"- 原文未提及"。` | **统一为**：`"原文中明确提到的"…如果没有提及，写"- 原文未提及"。`（两份字面量必须统一，且与 §10.6-2 的缺失语义清单对齐） | ⚠️ 两份当前**不一致**，必须一并收敛 |
| 60 | `bidAnalysisTask.cjs:47` / `analysisPrompts.ts:12` | `"此类标书还可能涉及的"需要根据你的经验，补充招标文件中未明确提及、但结合本招标文件类型和招投标经验判断非常重要的高风险遗漏项。` | `"此类项目还可能涉及的"需要根据你的经验，补充原文中未明确提及、但结合本工程类型（如深基坑/高大模板/脚手架/起重吊装/暗挖/装配式）与本类工程常见风险判断非常重要的遗漏项。` | 两份同步 |
| 61 | `bidAnalysisTask.cjs:48` / `analysisPrompts.ts:13` | `不要罗列所有常见可能项…每个小节最多输出 3-5 条。` | 中性，保留 | — |
| 62 | `analysisPrompts.ts:14-15`（TS 版独有） | `如果没有明显需要补充的关键项，写"- 暂未发现必须补充的高风险项"。` / `经验补充项每条前缀使用"重点补充："` | 中性，保留 | 若统一两份，建议把这两条**回填到 Main 版** |
| 63 | `bidAnalysisTask.cjs:54,56,59,62,64,67` / `analysisPrompts.ts:21,23,26,29,31,34` | `# 招标文件中明确提到的` / `## 无效投标` / `## 废标项` / `# 此类标书还可能涉及的` | `# 原文中明确提到的` / `## 编制风险项` / `## 退回触发项` / `# 此类项目还可能涉及的` | ⚠️ **输出格式标题是下游解析锚点**（`rejectionCheckTask.cjs` / `rejectionPrompts.ts` 消费），改前必须全仓列出消费点 |
| 64 | `bidSectionExtractionTask.cjs:125` | `你是严谨的招标文件多标段识别专家。你只能基于用户提供的带行号文本识别标段、标包、分包、采购包、包件或标的。` | `你是严谨的施工资料范围识别专家。你只能基于用户提供的带行号文本识别标段、标包、分包、施工区段、单位工程或分部工程。` | — |
| 65 | `bidSectionExtractionTask.cjs:131` | `任务：识别本段中明确属于某个标段/标包/分包/采购包/包件/标的的内容` | `任务：识别本段中明确属于某个标段/标包/分包/施工区段/单位工程/分部工程的内容` | — |
| 66 | `bidSectionExtractionTask.cjs:170` | `你是严谨的招标文件多标段识别结果合并专家。` | `你是严谨的施工资料范围识别结果合并专家。` | — |
| 67 | `bidSectionExtractionTask.cjs:148-151` | `"unit": "标段"`、`"title": "一标段"`、`"headLine": "一标段：设备采购及安装"`、`"description": "设备采购、安装、调试及售后服务。"` | 示例替换为施工语义：`"unit": "施工区段"`、`"title": "一区段"`、`"headLine": "一区段：地下室基坑及降水施工"`、`"description": "基坑支护、降排水与土方开挖。"` | 属 few-shot 示例，不改会让模型继续输出设备采购类内容 |

### 五、结构标识、正则、字典与哨兵（改写会「静默失配」的高危项）

| # | 位置 | 原文摘录 | 若改写为专项施工方案语义，应改为什么 | 同步点 |
|---|---|---|---|---|
| 68 | `bidAnalysisWorkflow.ts:24` | `const section = text.match(/^##[\t ]+技术评分项[\t ]*\r?\n([\s\S]*?)(?=^#{1,2}[\t ]\|$(?![\s\S]))/m);` | 正则改为 `/^##[\t ]+技术要求项[\t ]*\r?\n…/m`（与第 44 条配套） | ⚠️ **不改正则 → 前端永久误判「技术评分缺失」并卡住流程** |
| 69 | `bidSectionDetector.cjs:45` | `const totalSectionPattern = /…\s*(?:标段\|包\|分包\|标包\|标的\|子项目)/g;` | 末组单位扩充为 `标段\|标包\|分包\|区段\|施工区段\|单位工程\|分部工程\|专业工程\|子项目` | — |
| 70 | `bidSectionDetector.cjs:67-82`（16 条模式） | `([一二三四五六七八九十壹贰叁肆伍]+)标段[：:；;]` … `包([一二三四五六七八九十壹贰叁肆伍\d]+)[：:；;]` | 逐条补 `区段/施工区段/单位工程/分部工程` 变体；**注意 `:79-82` 的裸「包」单字模式误命中率高**（工程语境下"…包"极常见，如"承包单位""作业包"），建议收紧为 `施工包\|标包` 等限定词 | ⚠️ 误命中会让 `checkBidSections()` 误报多范围 |
| 71 | `bidSectionDetector.cjs:3` | `只用于快速判断招标文件是否疑似多标段，不生成最终标段列表。` | `只用于快速判断源文档是否疑似包含多个施工范围，不生成最终范围列表。` | 注释 |
| 72 | `bidSectionExtractionTask.cjs:73` | `unit: String(section?.unit \|\| '标段').trim() \|\| '标段',` | 默认单位 `'施工区段'`（或保留 `标段` 以兼容历史数据） | ⚠️ 与第 73 条同源，**必须同改** |
| 73 | `technicalPlanStore.cjs:236` | `unit: String(section?.unit \|\| '标段').trim() \|\| '标段',` | 同第 72 条 | ⚠️ 漏改 → 存储层把施工区段强制写成「标段」，前端显示与 AI 输出不一致 |
| 74 | `types.ts:338` | `unit: string;`（注释 `'标段' \| '标包' \| '分包' \| '包' \| '采购包' \| '包件' \| '标的'`） | 注释更新为施工域值域 | 类型注释，不影响运行 |
| 75 | `bidAnalysisTask.cjs:6` | `const MARKDOWN_MISSING_RESULT = '未提取到';` | 中性，**保留** | ⚠️ 与 `bidAnalysisWorkflow.ts:13` 同值，**不可单边改** |
| 76 | `bidAnalysisWorkflow.ts:13` | `export const BID_ANALYSIS_MISSING_RESULT = '未提取到';` | 中性，保留 | 同上 |
| 77 | `bidAnalysisTask.cjs:195` / `bidAnalysisWorkflow.ts:25` | `局部字段或局部分类缺失时写"没有提及"` / `section?.[1].trim() === '没有提及'` | 中性，**保留**（两者必须同值） | ⚠️ 不可单边改 |
| 78 | `analysisPrompts.ts:11` / `bidAnalysisTask.cjs:46` | `- 原文未提及` / `招标文件未提及` | **统一为 `- 原文未提及`**（见第 59 条） | ⚠️ 两份当前不一致 |
| 79 | `segmentedAiResultMerger.cjs:32` | `如果某段写"没有提及""原文未提及""本段未提及"或整段只有"未提取到"…` | 中性，**保留**（这是唯一枚举全 4 种缺失字面量的地方） | ⚠️ 若统一缺失字面量，此处也要同步 |
| 80 | `bidSectionExtractionTask.cjs:139` | `6. 如果本段没有明确标段内容，返回 {"sections":[]}。` | 中性（结构不变），可改为「本段没有明确范围内容」 | — |
| 81 | `bidSectionExtractionTask.cjs:181` | `5. 只返回 JSON，不要输出 Markdown、代码块、解释或额外文字。` | 中性，保留 | — |
| 82 | `bidAnalysisWorkflow.ts:87` | `并标注"[表格数据]"` | 中性，保留 `[表格数据]` 哨兵 | — |
| 83 | 全部 jsonTask 字段键（`:110,111,112,144,145,146,149,150,153,154`） | `bidding_deposit`/`bid_file_price`/`bid_opening_time`/`invalid_bid`/`performance_bond`/`committee`/`method`/`bid_notice` … | 见「一、提取项清单」逐项给出 | ⚠️ 必须同步 `BidAnalysisPage.tsx:91-120` 的 `jsonFieldLabels` + 所有下游读取点（**改前全仓 grep 字段名**） |
| 84 | `bidSectionContext.cjs:21,26,28` | `'本项目为多标段，当前招标文件已按用户选择的投标范围处理…'` / `当前选择标段：${title}` | `'本项目包含多个施工范围，当前源文档已按用户选择的施工范围处理…'` / `当前选择范围：${title}` | 这是**独立第二条 system 消息**（`:25-31`），改写要整段一起改 |

### 六、日志标题、告警文案与 UI 文案（改写成本最低，建议第一批做）

| # | 位置 | 原文摘录 | 若改写为专项施工方案语义，应改为什么 |
|---|---|---|---|
| 85 | `bidAnalysisTask.cjs:225` | `logTitle: logTitle \|\| \`招标解析-${task.label}\`` | `资料解析-${task.label}`（影响开发者模式 AI 日志文件名） |
| 86 | `bidAnalysisTask.cjs:245` | `logTitle: \`招标解析-${task.label}-第${index + 1}段\`` | `资料解析-${task.label}-第${i}段` |
| 87 | `bidAnalysisTask.cjs:257` | `logTitle: \`招标解析合并-${task.label}\`` | `资料解析合并-${task.label}` |
| 88 | `segmentedAiResultMerger.cjs:27` | `以下内容来自同一份招标文件按段分别解析后的结果。` | `…同一份源文档按段分别解析后的结果。` |
| 89 | `segmentedAiResultMerger.cjs:29` | `当前合并任务：${taskLabel \|\| '招标文件解析结果合并'}` | `… \|\| '源文档解析结果合并'` |
| 90 | `bidAnalysisTask.cjs:284` | `'请先上传招标文件，再开始解析'` | `'请先上传源文档（招标文件/技术要求文件），再开始解析'` |
| 91 | `bidAnalysisTask.cjs:289` | `'请先完成多标段识别，再开始解析招标文件'` | `'请先完成施工范围识别，再开始解析源文档'` |
| 92 | `bidAnalysisTask.cjs:292` | `'请先选择本次投标范围，再开始解析招标文件'` | `'请先选择本次施工范围，再开始解析源文档'` |
| 93 | `bidAnalysisTask.cjs:296` | `'当前投标范围已失效，请重新选择标段'` | `'当前施工范围已失效，请重新选择区段'` |
| 94 | `bidAnalysisTask.cjs:271` | `'未找到无效投标与废标项解析任务'` | `'未找到编制风险与退回触发项解析任务'` |
| 95 | `bidAnalysisTask.cjs:328-331` | `'开始重新解析选中的招标文件解析项。'` / `'开始重新解析全部招标文件解析项。'` / `'开始解析招标文件。'` | `'开始重新解析选中的解析项。'` / `'开始重新解析全部解析项。'` / `'开始解析源文档。'` |
| 96 | `bidAnalysisTask.cjs:445` | `'提示词缓存预热完成，等待 5 秒后开始并发解析剩余项。'` | 中性，保留 |
| 97 | `bidAnalysisTask.cjs:455` | `` `必填解析项未完成：${missingLabels}，请重新解析失败项。` `` | 中性，保留 |
| 98 | `bidAnalysisTask.cjs:460` | `'招标文件解析完成。'` | `'源文档解析完成。'` |
| 99 | `bidAnalysisTask.cjs:421` | `` `${task.label}解析失败：${error.message}` `` | 中性，保留 |
| 100 | `bidSectionExtractionTask.cjs:117` | `'未识别到至少两个有效标段'` | `'未识别到至少两个有效施工范围'` |
| 101 | `bidSectionExtractionTask.cjs:203` | `'请先上传招标文件，再进行多标段识别'` | `'请先上传源文档，再进行施工范围识别'` |
| 102 | `bidSectionExtractionTask.cjs:221` | `'开始识别招标文件中的标段范围。'` | `'开始识别源文档中的施工范围。'` |
| 103 | `bidSectionExtractionTask.cjs:226` | `` `招标文件已按上下文拆分为 ${n} 段，正在提取标段候选。` `` | `` `源文档已按上下文拆分为 ${n} 段，正在提取范围候选。` `` |
| 104 | `bidSectionExtractionTask.cjs:251` | `` `已识别 ${n} 个标段，请选择本次投标范围。` `` | `` `已识别 ${n} 个施工范围，请选择本次编制范围。` `` |
| 105 | `technicalPlanStore.cjs:266` | `'未找到选择的投标范围'` | `'未找到选择的施工范围'` |
| 106 | `technicalPlanStore.cjs:269` | `'当前标段缺少有效范围，请重新识别'` | `'当前范围缺少有效行号范围，请重新识别'` |
| 107 | `technicalPlanStore.cjs:287` | `'生成投标范围工作副本失败，请重新提取标段'` | `'生成施工范围工作副本失败，请重新提取范围'` |
| 108 | `technicalPlanStore.cjs:792` | `'原始招标文件缺失，请重新上传招标文件'` | `'原始源文档缺失，请重新上传源文档'` |
| 109 | `technicalPlanStore.cjs:2643` | `'原始招标文件内容为空，请重新上传'` | `'原始源文档内容为空，请重新上传'` |
| 110 | `technicalPlanStore.cjs:761` | `fileName: meta.tender_file_name \|\| '技术方案招标文件'` | `\|\| '工程资料'` |
| 111 | `taskService.cjs:1155` | `'上次招标文件解析未完成，请重新解析'` | `'上次源文档解析未完成，请重新解析'` |
| 112 | `taskService.cjs:1194` | `'上次多标段识别未完成，请重新识别'` | `'上次施工范围识别未完成，请重新识别'` |
| 113 | `taskService.cjs:30` | `label: '多标段识别'` | `label: '施工范围识别'` |
| 114 | `taskService.cjs:39` | `label: '招标文件解析'` | `label: '源文档解析'` |
| 115 | `fileService.cjs:32` | `/** 把招标 Word 原件落到工作区；.doc/.wps 先转成 .docx。 */` | `/** 把 Word 原件落到工作区；.doc/.wps 先转成 .docx。 */`（仅注释） |
| 116 | `fileService.cjs:583` | `const label = String(documentLabel \|\| '招标文件').trim() \|\| '招标文件';` | `\|\| '源文档'`（影响文件选择弹窗标题） |
| 117 | `fileService.cjs:679` | `const documentLabel = documentRole === 'bid' ? '投标文件' : '招标文件';` | `documentRole === 'bid' ? '已生成的方案文件' : '源文档'` |
| 118 | `BidSectionSelectorDialog.tsx:33-34,37-38` | `选择投标范围` / `检测到招标文件包含多个标段或包，请选择本次投标范围。` / `检测到本招标文件共包含 N 个，请选择您要投标的范围。` | `选择施工范围` / `检测到源文档包含多个标段或施工区段，请选择本次编制范围。` / `检测到本文件共包含 N 个范围，请选择您要编制的范围。` |
| 119 | `BidAnalysisPage.tsx:79` | `{ title: '投标流程', ids: ['keyInfo', 'marginInfo', 'openBid'] }` | `{ title: '实施节点与造价', ids: [...] }`（⚠️ 与第 10、13 条的删除联动） |
| 120 | `BidAnalysisPage.tsx:80` | `{ title: '评标要求', ids: ['qualificationReview', 'complianceCheck', 'evaluationBid', 'businessScoring'] }` | `{ title: '资质与强制性要求', ids: [...] }` |
| 121 | `BidAnalysisPage.tsx:81` | `{ title: '主体与合同', ids: ['agentInfo', 'discardedBids', 'signingProcess', 'terminationCondition'] }` | `{ title: '参建单位、风险与合同', ids: [...] }` |
| 122 | `BidAnalysisPage.tsx:78` | `{ title: '采购项', ids: ['procurementList'] }` | `{ title: '材料设备与工程量', ids: ['procurementList'] }` |
| 123 | `BidAnalysisPage.tsx:77` | `{ title: '关键项', ids: [...] }` | 中性，可保留 `关键项` |
| 124 | `BidAnalysisPage.tsx:270` | `'招标文件解析任务已结束，可以进入下一步。'` | `'源文档解析任务已结束，可以进入下一步。'` |
| 125 | `BidAnalysisPage.tsx:272-273` | `'多标段 · X'` / `'多标段 · 待选择'` / `'单标段'` | `'多范围 · X'` / `'多范围 · 待选择'` / `'单范围'` |
| 126 | `BidAnalysisPage.tsx:321` | `'多标段识别失败，请重新识别或改用单标段解析'` | `'施工范围识别失败，请重新识别或改用单范围解析'` |
| 127 | `BidAnalysisPage.tsx:840-841` | `'系统检测到招标文件疑似包含多个标段，建议切换为多标段解析…'` / `'系统没有通过规则检测到明确多标段结构，是否仍继续使用 AI 识别多标段？'` | 同步替换 `招标文件`→`源文档`、`多标段`→`多施工范围` |
| 128 | `technicalPlanIpc.cjs`（乱码行） | `'杩樻病鏈夋姇鏍囨ā鏈垨锛岃璇峰厛纭涓€绾х洰褰?'`（GBK 乱码） | 修复编码后写为 `'还没有方案模板，请先确认上级目录'`（⚠️ 见 §10.5） |

### 七、改造顺序建议（按依赖与风险排序）

| 阶段 | 内容 | 涉及条目 | 风险 |
|---|---|---|---|
| **P0** | 只改文案层 + 修 `technicalPlanIpc.cjs` 乱码 | §六 全部（85-128）、§10.5 | **低**。纯字符串，不动 schema 与判定逻辑 |
| **P1** | 改 global system + jsonTask 第 3 条 + 语料引导 | 19-22 | **中**。需 A/B 验证弱模型提取质量 |
| **P1** | 改技术评分要求项，**必须同步改正则** | 38-52（尤其 44 + 68 配对） | **高**。只改提示词不改 `bidAnalysisWorkflow.ts:24` → 前端把全部结果误判为「技术评分缺失」并**永久卡住流程** |
| **P1** | 改废标项（含输出标题） | 54-63 | **中**。⚠️ 改前必须全仓列出 `rejectionCheckTask.cjs` / `rejectionPrompts.ts` 对标题的消费点 |
| **P2** | 重构 `responseFileRequirements`（耦合最深） | 26-32 | **中高**。语义变更最大，建议先做原型验证 |
| **P2** | 调整提取项集合（删 `marginInfo`/`openBid`，增 `编制依据与规范`/`危大工程与专项论证`/`现场条件与协调`/`资源配置与工期`） | 1-18 | **高**。id 是落库主键，**必须配一次性迁移映射表** |
| **P3** | 改 JSON 字段键 | 83 + 各表内 `同步点` | **高**。⚠️ 改前全仓 grep 字段名，列出全部下游读取点 |
| **P3** | 改标段单位值域与检测正则 | 69-74 | **中高**。`unit` 已落库，需兼容读；`:79-82` 裸「包」误命中需先量化 |

> **一句话结论**：本模块的**工程骨架**（文件解析 → 分段 → 并发 18 项 → 分段合稿 → 缺失重跑 → 断点续跑 → 标段工作副本 → 落盘）与招投标语义**几乎完全解耦，可直接复用**；真正耦合的是**提示词正文、提取项标签、JSON 字段字典、标段单位词表、UI 文案**五层，其中 3 处是「改一处必须改两处」的硬耦合陷阱：
> 1. `bidAnalysisTask.cjs` ↔ `bidAnalysisWorkflow.ts`（两份任务定义与提示词）
> 2. `## 技术评分项` 标题 ↔ `bidAnalysisWorkflow.ts:24` 的正则
> 3. 缺失字面量五种 ↔ 判定函数与合稿提示词