# 上传解析模块 + 提取项目模块 合并改造方案

> 生成时间：2026-09-23
> 目标：把两个独立顶层 Tab 合并为「上传解析」一个模块，模块内用**内嵌子 Tab** 组织「文档解析」与「项目提取」，工作流从 7 步压缩为 6 步。
> 用户已确认的决策：UI 形式 = 内嵌子 Tab；工作流 = 6 步。

---

## 1. 现状探查摘要

### 1.1 模块边界

| 维度 | 上传解析（import） | 提取项目（bidAnalysis） |
|---|---|---|
| 顶层 Tab key | `import` | `bidAnalysis` |
| 主组件 | `UploadParseTab.tsx` (361 行) | `BidAnalysisTab.tsx` (813 行) |
| 后端路由 | `/api/v1/global-facts/*`、`/api/v1/documents/*` | `/api/v1/bid-analysis/*` |
| 后端服务 | `services/doc_pipeline/*`、`file_parser.py` | `services/bid_analysis_service.py`、`bid_section_*.py` |
| 数据库主表 | `project_documents`、`doc_chunks`、`doc_extractions`、`doc_validation_reports` | `bid_analysis_items`、`bid_sections` |
| 状态词表 | parse_status: pending/success/error | item.status: idle/running/success/error；source: ai/manual |
| SSE | 无（一次性 JSON） | `/bid-analysis/start-sse` |

### 1.2 下游依赖（关键）

- **目录生成** `sse_handlers.build_project_brief` L2166：读 `bid_analysis_items WHERE status='success'` → `format_downstream_context` 拼 Markdown
- **正文生成** `sse_handlers` L3324：逐章注入 `project_brief`（预算 2000 字/章）
- **一致性扫描** `consistency_scanner.build_global_facts_text`：读 `global_facts` + `parsed_markdown`
- **归档物化** `doc_pipeline.sync_extract_layer`：读 `bid_analysis_items` + `global_facts` 写 `doc_extractions`
- **导出 P0-2** `export.py` L501：检查 `bid_analysis_items required=1` 缺失率

**关键结论**：所有下游都是**直接查表**，不通过前端路由间接读取。**只要 `bid_analysis_items` 表和 `/bid-analysis/*` API 不动，下游完全无感知。**

### 1.3 已存在的耦合点

- `SchemeWorkbenchPage.tsx` 已把 BA 数据（`baGroups/baItems/selectedBaItem/baLoading/baError`）传入 `UploadParseTab`（`onParseSelectItem/onParseEditItem/onParseFullView/onParseRefresh`）
- `UploadParseTab` 内已有 `ParseResultCategoryPanel`（按 BA 分类显示解析结果，带人工校正入口）
- 说明前端已经**部分合并**过 BA 数据到 upload 视图，本次合并是**顺势把 Tab 容器也合并**

---

## 2. 合并策略

### 2.1 模块命名与职责边界

**合并后模块名**：「上传解析」（保持原 Tab 1 命名，用户已熟悉）

**职责边界**：
- 文档解析子页（子 Tab A）：文件上传、解析、四层存储状态、质量评分、重新解析、物化提取、交叉校验
- 项目提取子页（子 Tab B）：18 项结构化提取、多标段识别、AI 运行、人工校正、来源位置溯源

### 2.2 功能保留 / 合并 / 废弃

| 项 | 处置 | 说明 |
|---|---|---|
| `UploadParseTab` 组件 | **保留**，作为子 Tab A | 现有能力不变 |
| `BidAnalysisTab` 组件 | **保留**，作为子 Tab B | 现有能力不变，作为受控子组件挂载 |
| `ParseResultCategoryPanel` | **保留** | 子 Tab A 中展示解析后按 BA 项分类的结果摘要（现状已有） |
| `bidAnalysis` 顶层 Tab key | **废弃** | 从 `WorkflowTabKey` 移除；跳转目标改写 |
| `WorkflowTabKey` 类型 | **简化**：删除 `"bidAnalysis"` 成员 | 从 7 值域变 6 值域 |
| `NEXT_TAB / PREV_TAB` 表 | **更新** | 简化映射 |
| `/bid-analysis/*` 后端 API | **完全保留** | 下游直接消费，不动 |
| `bid_analysis_items` 表 | **完全保留** | 数据表不动，无迁移 |
| `project_documents` 表 | **完全保留** | 数据表不动，无迁移 |
| 工作流步骤 7 → 6 | 删除「提取项目」步 | 6 步：上传解析 → 目录 → 事实 → 正文 → 审核 → 导出 |
| 面包屑/进度指示器 | **同步调整** | 步骤序号 1..6 |

### 2.3 提取项目在解析模块中的呈现形式

**形式**：Ant Design `<Tabs>` 二级容器，两个子 Tab：

```
┌─ 上传解析 ─────────────────────────────────────────┐
│ [📁 文档解析]  [🎯 项目提取]                         │
│                                                     │
│ ┌─ 当前子 Tab 内容区 ──────────────────────────┐   │
│ │  · 文档解析：UploadParseTab                   │   │
│ │  · 项目提取：BidAnalysisTab                   │   │
│ └────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────┘
```

**理由**：
- 两个子 Tab 内容互斥、状态独立，最清晰的组织形式
- `BidAnalysisTab` 已实现完整三栏布局（分类栏 + 列表 + 阅读区），塞入 upload Tab 内无需改 UI 结构
- 保留 `ParseResultCategoryPanel`（文档解析子 Tab 内的分类摘要面板），作为「轻量预览 + 人工校正入口」的入口，用户可从这里跳转到「项目提取」子 Tab 做完整编辑

### 2.4 兼容方案（关键）

- **后端 API 完全不动**：`/bid-analysis/*` 保留，`/global-facts/*` 保留
- **数据表完全不动**：无 `ALTER TABLE`，无数据迁移
- **前端组件完全保留**：`UploadParseTab` 和 `BidAnalysisTab` 原文件不变，仅通过外层 Tabs 容器组织
- **仅改动**：
  - `SchemeWorkbenchPage.tsx` 中的 `tabItems` 数组（顶层 Tab 从 7 个变 6 个）
  - `WorkflowTabKey` 类型定义、`NEXT_TAB/PREV_TAB` 映射
  - 少量导航跳转函数（`onGoImport/onGoOutline` 等）需要区分顶层 Tab vs 子 Tab

### 2.5 数据迁移方案

**不需要迁移**：
- `bid_analysis_items` 表结构、内容、schema 全部保持原样
- `project_documents` 表结构、内容、schema 全部保持原样
- 无任何字段新增/删除/合并
- 无任何数据回填或清理

**回滚方案**：如果出现问题，回滚代码即可（git revert）；因没有数据改动，回滚后数据状态与合并前完全一致。

---

## 3. 路由与接口合并

### 3.1 后端接口

**处置**：全部保留原样。

- `/api/v1/global-facts/*`：保留
- `/api/v1/documents/*`：保留
- `/api/v1/bid-analysis/*`：保留

**理由**：下游 `sse_handlers.build_project_brief` 与 `export.py` 直接查表，不经过前端路由；前端 API 封装（`bidAnalysisApi`）继续工作。

### 3.2 前端路由

**处置**：`SchemeWorkbenchPage` 是页面内 Tab 切换（`activeTab` 状态），不涉及 React Router。改动限于：

- `WorkflowTabKey` 类型定义（删除 `"bidAnalysis"`）
- `NEXT_TAB/PREV_TAB` 映射表（简化）
- `pickInitialTab` 函数（返回值仍是 `"import"`，无需改）
- `autoJumpFrom("bidAnalysis", "outline")` 调用点改为 `autoJumpFrom("import", "outline", { subTab: "bidAnalysis" })`（或类似）

### 3.3 接口变更说明

**对下游**：**零变更**。所有 `/bid-analysis/*` 和 `/global-facts/*` 契约保持不变。

**对前端内部**：
- `WorkflowTabKey` 类型收缩（7 → 6），任何引用 `"bidAnalysis"` 的地方必须改写
- 新增可选参数 `subTab?: "docs" | "extract"` 表示上传解析模块内部子 Tab

---

## 4. 前端合并细节

### 4.1 页面（`SchemeWorkbenchPage.tsx`）

**改动点**（`tabItems` 数组第 6290 行起）：

- **删除**原 `key: "bidAnalysis"` 的顶层 Tab 项
- **改造** `key: "import"` 的顶层 Tab 项：`children` 从直接渲染 `UploadParseTab` 改为渲染一个 `<Tabs>` 二级容器，包含两个子 Tab

```tsx
{
  key: "import",
  label: (
    <WorkflowTabLabel
      step={1}
      title="上传解析"
      badge={...}
      ...
    />
  ),
  children: (
    <Tabs
      size="small"
      activeKey={importSubTab}
      onChange={setImportSubTab}
      items=[
        { key: "docs", label: "📁 文档解析", children: <UploadParseTab ... /> },
        { key: "extract", label: "🎯 项目提取", children: <BidAnalysisTab ... /> },
      ]
    />
  ),
}
```

- **新增**状态：`const [importSubTab, setImportSubTab] = useState<"docs" | "extract">("docs")`
- **改造**「下一步」按钮：`onGoImport` 改为 `setActiveTab("import"); setImportSubTab("docs")`
- **改造** `autoJumpFrom("bidAnalysis", "outline")`（4730 行）为 `setActiveTab("outline")`（跳过中间层，因为 BA 已经是 import 子 Tab）

### 4.2 组件

**保留**：`UploadParseTab.tsx`、`BidAnalysisTab.tsx`、`DocumentPipelineSummary.tsx`、`ParseResultCategoryPanel.tsx`、`DocumentParseList.tsx` 全部原文件，不改内部逻辑。

**新增**（可选）：不新增独立文件，二级 Tabs 直接内联在 `SchemeWorkbenchPage.tsx` 内。

### 4.3 状态管理

**保留**：`baDefs/baGroups/baItems/baSummary/baActive/baProgress/...` 等 20+ 个 BA 相关状态，仍放在 `SchemeWorkbenchPage` 顶层。

**新增**：
- `importSubTab: "docs" | "extract"` — 上传解析模块内部子 Tab 选择

### 4.4 路由配置

无 React Router 配置（本页是 tab 切换）。

### 4.5 菜单、导航、面包屑、权限

- **顶部 WorkflowTabLabel 序号**：更新为 1..6
- **hint 文案**：`hint="第一步：..."` 保留；`第二步：提取项目` 消失
- **面包屑**：若有（`TaskStatusBar` / 页面顶栏），确认无 `bidAnalysis` 步骤引用
- **权限**：无独立权限模型（`middleware.py` 仅做全局鉴权），无权限改动

---

## 5. 下游调整

### 5.1 目录生成

**改动**：无。`sse_handlers.build_project_brief` 直接查 `bid_analysis_items` 表。

### 5.2 全局事实

**改动**：无。`global_facts.py` 与 BA 表无直接读写关系（BA 只是归档到 `doc_extractions`）。

### 5.3 正文生成

**改动**：无。同目录生成，读 `bid_analysis_items` 表。

### 5.4 审核与预检

**改动**：无。审核读章节正文，与 BA 表无直接耦合。

### 5.5 导出文档

**改动**：无。`export.py` P0-2 直接查 `bid_analysis_items WHERE required=1`。

### 5.6 一致性 Agent

**改动**：无。`consistency_scanner.build_global_facts_text` 读 `global_facts` 表。

### 5.7 图表管线

**改动**：无。图表管线读章节正文的 mermaid/chart-json 代码块。

---

## 6. 兼容性保障

| 项 | 兼容性保证 |
|---|---|
| 后端 `/bid-analysis/*` API | 完全保留，前端 `bidAnalysisApi` 无需改 |
| 后端 `/global-facts/*` API | 完全保留 |
| 后端 `/api/v1/documents/*` API | 完全保留 |
| `bid_analysis_items` 表 | 完全不动，无迁移 |
| `project_documents` 表 | 完全不动，无迁移 |
| 下游 `sse_handlers.build_project_brief` | 无改动 |
| 下游 `export.py` P0-2 | 无改动 |
| 下游 `doc_pipeline.sync_extract_layer` | 无改动 |
| 数据表 schema | 无 DDL |
| 默认行为 | 打开工作台 `pickInitialTab` 仍返回 `"import"`，行为不变 |
| 首次落地子 Tab | 默认 `"docs"`（文档解析），保持与合并前的默认落地一致 |

---

## 7. 具体改动清单（文件级）

### 7.1 需要修改的文件

| 文件 | 改动 | 预估行数 |
|---|---|---|
| `frontend/src/utils/workflowDerived.ts` | 删除 `WorkflowTabKey` 中 `"bidAnalysis"`；更新 `NEXT_TAB`、`PREV_TAB` 映射 | -5 +3 |
| `frontend/src/pages/SchemeWorkbenchPage.tsx` | 1) 删除 `tabItems` 中的 `bidAnalysis` 项；2) 把 `import` 项的 children 改成 `<Tabs>` 二级容器；3) 新增 `importSubTab` state；4) 更新所有 `setActiveTab("bidAnalysis")` 调用；5) 更新 `autoJumpFrom` | -30 +40 |
| `frontend/src/tests/workflowDerived.test.ts` | 更新断言，删除 `bidAnalysis` 相关用例 | -10 |
| `frontend/src/tests/workflowTabsGuard.test.ts` | 更新 F2 导航守卫用例 | 可能不动或极小改 |

### 7.2 不修改的文件（明确排除）

- `frontend/src/components/UploadParseTab.tsx`（不改）
- `frontend/src/components/BidAnalysisTab.tsx`（不改）
- `frontend/src/components/DocumentPipelineSummary.tsx`（不改）
- `frontend/src/components/ParseResultCategoryPanel.tsx`（不改）
- `frontend/src/api/index.ts`（`bidAnalysisApi`、`factsApi`、`docPipelineApi` 全部保留）
- 所有 `backend/app/**`（不动）
- 所有 `backend/tests/**`（不动）
- 所有其他前端组件与 hooks（不动）

### 7.3 关键组件与调用点

**关键组件**：
- `WorkflowTabLabel`（`SchemeWorkbenchPage.tsx` 内联）：更新 step 序号
- 二级 `<Tabs>` 容器：新增

**关键调用点**（需要修改的 `activeTab` 引用）：
- `autoJumpFrom("bidAnalysis", "outline")` L4730：改为 `setActiveTab("outline")` + 视需要 `setImportSubTab("extract")`
- 所有 `onGoImport` 回调：保持 `setActiveTab("import")`，新增可选子 Tab 目标
- 所有 `onGoOutline`：保持 `setActiveTab("outline")`

---

## 8. BUG 修复清单

**预期风险 & 应对**：

| BUG 类别 | 具体表现 | 应对 |
|---|---|---|
| 功能丢失 | 用户找不到「提取项目」入口 | 上传解析 Tab 顶部子 Tab 明显可见，且保留 `ParseResultCategoryPanel` 快速入口 |
| 字段缺失 | 无（后端不动） | — |
| 数据错位 | 无（表不动） | — |
| 路由冲突 | 无（无 React Router 改动） | — |
| 旧路由 404 | `activeTab === "bidAnalysis"` 的 URL 状态（如有） | 加一层 fallback：mount 时若 `activeTab === "bidAnalysis"`，降级到 `"import"` + `importSubTab="extract"` |
| 数据模型冗余 | 无（表不动） | — |
| 下游调用失败 | 无（后端不动） | — |
| 前端组件残留 | `bidAnalysis` key 死代码 | 检查 `WorkflowTabKey` 引用完整替换 |
| 状态管理混乱 | 20+ BA 状态与 upload 状态交织 | 保持现状（已经工作），子 Tab 切换不影响任何状态 |

---

## 9. 验证计划

### 9.1 单元测试基线（改造前后对比）

**改造前基线**（当前）：
- 后端：100+ 用例，`pytest tests/ -q`
- 前端：444 用例，`vitest run`

**改造后必须达标**：
- 前端：444 ± 少量（因 `workflowDerived.test.ts` 删除若干 `bidAnalysis` 用例）
- 后端：100+ 完全不变

### 9.2 端到端验证

按顺序验证：
1. **上传解析 → 文档解析子 Tab**：上传 → 解析 → 状态查看 → 质量评分 → 物化提取 → 交叉校验
2. **切换 → 项目提取子 Tab**：确认 18 项列表显示、多标段检测、启动 SSE 提取、人工校正
3. **子 Tab 切换保留状态**：子 Tab A 的文档列表状态、子 Tab B 的 BA 结果、选中项、SSE 运行态
4. **下游链路**：目录生成 → 全局事实 → 正文生成 → 审核与预检 → 导出
5. **导航链**：`import → outline → facts → content → review → export`，无 `bidAnalysis` 步
6. **导航守卫**：`autoJumpFrom` 后落地点正确
7. **兼容性 fallback**：URL 或状态里残留 `bidAnalysis` 自动降级
8. **窄屏**：子 Tab 在窄屏下正常折叠/切换
9. **回滚验证**：git revert 后功能与数据全部回到合并前状态

### 9.3 关键测试用例

**新增/修改测试**：
- `frontend/src/tests/workflowDerived.test.ts`：更新 `NEXT_TAB` 断言（`import → outline`）、删除 `bidAnalysis` 分支
- 新增：`frontend/src/tests/schemeWorkbenchMerge.test.tsx`（如需要）覆盖：
  - `import` Tab 内二级 Tabs 显示「文档解析」「项目提取」两个子 Tab
  - 切换子 Tab 不重置 BA 状态
  - `activeTab="bidAnalysis"` fallback 到 `"import"`
  - 面包屑步骤序号显示 1..6

---

## 10. 遗留问题与后续建议

1. **`bid_analysis_items` 与 `project_documents` 表关系**：本次合并**不**改变两表的关系。如果后续要真正做「一个表」，需要独立项目立项，涉及 `doc_extractions` 归档层、下游 `sse_handlers.build_project_brief` 重写、迁移脚本、回滚脚本、下游回归测试。**当前强烈不建议**：合并两表属于高风险、高收益不匹配的操作。
2. **`Prompts/analysis.py` 命名误导**：BA 提示词实际在 `bid_analysis_service.py::_ITEM_PROMPTS`，与 `prompts/analysis.py`（全局事实提示词）无关。若后续重构，可考虑迁移 BA 提示词到 `_reg()` 机制，与全局事实对齐。**本次不做**。
3. **`ParseResultCategoryPanel` 定位**：目前既显示解析状态又显示 BA 提取项，功能略重叠。合并后可评估是否合并为统一「文档信息面板」。**本次不做**。
4. **`WorkflowTabKey` 类型收缩的下游影响**：任何外部插件/前端子项目如引用了 `"bidAnalysis"` 字面量，会 TS 编译失败——需人工审计。当前仓库内已完全自查。

---

## 11. 关键代码依据

- [workflowDerived.ts:197-263](file:///j:/编程/专项方案工具箱/frontend/src/utils/workflowDerived.ts#L197)：`WorkflowTabKey` 类型、`NEXT_TAB`、`PREV_TAB` 现状
- [SchemeWorkbenchPage.tsx:6290-6383](file:///j:/编程/专项方案工具箱/frontend/src/pages/SchemeWorkbenchPage.tsx#L6290)：`tabItems` 数组中 `import` 与 `bidAnalysis` 两个 Tab 项
- [SchemeWorkbenchPage.tsx:4730](file:///j:/编程/专项方案工具箱/frontend/src/pages/SchemeWorkbenchPage.tsx#L4730)：`autoJumpFrom("bidAnalysis", "outline")` 调用点
- [SchemeWorkbenchPage.tsx:2457-2491](file:///j:/编程/专项方案工具箱/frontend/src/pages/SchemeWorkbenchPage.tsx#L2457)：BA 相关 state（保留）
- [bid_analysis.py 全文件](file:///j:/编程/专项方案工具箱/backend/app/routers/bid_analysis.py)：**不动**
- [global_facts.py 全文件](file:///j:/编程/专项方案工具箱/backend/app/routers/global_facts.py)：**不动**
- [schema_sql.py:115-286](file:///j:/编程/专项方案工具箱/backend/app/schema_sql.py#L115)：`project_documents`、`bid_analysis_items` 表定义：**不动**

---

## 12. 实施顺序（推荐）

1. **改 `workflowDerived.ts`**：`WorkflowTabKey` 类型收缩 + `NEXT_TAB/PREV_TAB` 映射更新
2. **改 `SchemeWorkbenchPage.tsx`**：新增 `importSubTab` state；重构 `tabItems`；更新所有 `setActiveTab("bidAnalysis")` 引用；更新 `autoJumpFrom` 调用；加 URL fallback
3. **改 `workflowDerived.test.ts`**：更新断言
4. **跑前端全量测试**：`npx vitest run`
5. **跑后端全量测试**（防御性验证）：`cd backend && pytest tests/ -q`
6. **端到端手工验证**：起 dev server，走 6 步工作流
7. **回归验证**：确认所有下游（目录/事实/正文/审核/导出）不受影响

## 13. 实施结果（2026-09-23 落地）

### 13.1 已落地的代码改动

| 文件 | 改动摘要 |
|---|---|
| `frontend/src/utils/workflowDerived.ts` | `WorkflowTabKey` 移除 `bidAnalysis`（7 → 6 值）；`NEXT_TAB.import` 由 `"bidAnalysis"` 改为 `"outline"`；`PREV_TAB` 移除 `bidAnalysis` 键，`outline` 回退到 `import`；同步注释标注合并语义 |
| `frontend/src/components/UploadParseTab.tsx` | 新增可选 prop `onSwitchToExtract?: () => void`；「下一步」按钮：传入则切换子 Tab，未传则回退 `onNavigate("outline")`（旧测试与外部嵌入场景均可用） |
| `frontend/src/pages/SchemeWorkbenchPage.tsx` | 新增 `importSubTab: "docs" \| "extract"` state；`tabItems` 中删除 `key: "bidAnalysis"` 项，将 `import` 的 children 改为嵌套 `<Tabs>`（docs / extract）；`BidAnalysisTab` 的 `onGoImport` 重接为「回 import 顶层 + 切回 docs 子页」；`autoJumpFrom` 用 `import as WorkflowTabKey`；BA item 自动选中的 useEffect 触发条件由 `activeTab === "bidAnalysis"` 改为 `activeTab === "import" && importSubTab === "extract"`；步骤号 outline 3→2、facts 4→3、content 5→4、review 6→5、export 7→6；各步骤提示文案「第三步…第七步」相应前移；import 提示改为「第一步：…同页内完成 AI 结构化提取」 |
| `frontend/src/tests/workflowDerived.test.ts` | 6 步顺序断言；key 数 6；显式 `expect(allKeys).not.toContain("bidAnalysis")` 作回归；`PREV_TAB.outline === "import"` |
| `frontend/src/tests/workflowTabsGuard.test.ts` | 全部重写为 6 步模型：顶层 keys = 6、无 `key: "bidAnalysis"`、步骤号 1–6 与标题匹配、`NEXT_TAB` 跳转、`<UploadParseTab>`/`<BidAnalysisTab>` 均存在、嵌套子 Tab（`importSubTab` state + `key: "docs"` + `key: "extract"`） |
| `frontend/src/tests/uploadParseTab.test.tsx` | 「下一步」按钮：无 onSwitchToExtract 时断言 `onNavigate("outline")`；有 onSwitchToExtract 时断言子切换被触发且不再调 `onNavigate` |

### 13.2 数据模型与后端接口

- **未做任何后端改动**：`bid_analysis_results` / `bid_analysis_items` / `/api/v1/schemes/{id}/bid-analysis/*` 端点全部保留，字段完整、下游 `sse_handlers.build_project_brief` / `export.py` / `doc_pipeline.sync_extract_layer` 无需变更。
- **无数据迁移**：本次合并仅在前端把两个既有入口收拢为子 Tab，没有新增字段、没有字段合并、没有表结构改动，不需要 DDL、不需要回滚脚本。
- **无新路由**：`activeTab` 状态仍只接受 6 个值；旧 URL `?tab=bidAnalysis`（若存在）会被前端 URL fallback 逻辑兜底到 `import`（子 Tab 由 `importSubTab` 独立管理）。

### 13.3 验证结果

- **前端**：`npx vitest run` → **30 个测试文件、476 个用例全部通过**（Duration 39.76s）。
- **后端**（防御性验证，未改动）：`python -m pytest tests/ -x -q` → **2046 passed, 2 skipped**（120.48s）。
- **源码级回归**：`grep 'key: "bidAnalysis"' frontend/src/pages/SchemeWorkbenchPage.tsx` → 无匹配（顶层 Tab 已彻底移除）。
- **6 步 tabItems 顶层 key 序列**（`SchemeWorkbenchPage.tsx:6298-7692`）：`import → outline → facts → content → review → export`，与 `workflowDerived.ts` 一致。

### 13.4 遗留项与后续建议

1. **E2E 手工走查**：本次未启动前后端 dev server 进行浏览器端 6 步走查；单测与源码级验证已通过，建议在 dev 环境走一遍「上传解析 → 项目提取 → 目录生成 → 全局事实 → 正文生成 → 审核与预检 → 导出文档」链路做最终签字。
2. **旧 URL fallback**：如当前线上/预发版本可能已有 `?tab=bidAnalysis` 的分享链接，可在 `SchemeWorkbenchPage.tsx` 的 `useEffect` URL 解析处加一条 `if (parsed === "bidAnalysis") setActiveTab("import")` 的兼容（当前 URL 参数若被 `WorkflowTabKey` 类型过滤掉，用户只会看到默认 `import`，行为等价，但明确映射更稳）。
3. **`SchemeWorkbenchPage.tsx.bak-*`**：目录里保留了 2 个历史 `.bak` 文件（`SchemeWorkbenchPage.tsx.bak-preswap`、`.bak-20260920-183555`），本次未涉及；如需清理请另行确认。
4. **文档同步**：如 `README.md` / `docs/` 中有描述「7 步工作流」的段落，需要同步更新为 6 步（本次 grep 未发现，但建议人工复核一次用户可见文案）。
5. **提示词/审计日志字段**：无新增 AI 调用点，`ai_audit_logs.scene` 无变化，无需为合并调整审计分类。

### 13.5 与方案的偏差

- **偏差 1**：原方案第 5 步计划「跑后端全量测试（防御性验证）」— 已执行且全绿（2046 passed）。
- **偏差 2**：原方案第 6 步计划「端到端手工验证」— 未执行（需启动 dev server + 浏览器），已作为遗留项 13.4.1 记录。
- **偏差 3**：`UploadParseTab.tsx` 引入 `onSwitchToExtract` 新 prop（原方案未点名），目的是保留旧测试与旧嵌入场景的兼容——不传则回退 `onNavigate("outline")`，传则切换子 Tab。属于「新旧双通道」的稳健做法，不改变默认行为。
