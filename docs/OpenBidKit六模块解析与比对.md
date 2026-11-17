# OpenBidKit 易标 · 六模块全功能解析与本仓库比对

> 依据源文件：`J:\编程\OpenBidKit 易标\OpenBidKit_Yibiao-main-2026-09-14\全功能模块.txt`
> 比对对象：`j:/编程/专项方案工具箱`（专项方案工具箱，已多轮校准）
> 生成日期：2026-09-30
> 用法：第一部分 = 上游设计全功能说明（文档第十节要求的产出）；第二部分 = 上游 vs 本仓落地比对；第三部分 = 缺口实现方案（先方案后动手，不盲改）。

---

## 0. 六模块工作流程全景图

```
[招标文件 / 工程量清单 / 原方案 / 知识库]
            │
      ┌─────▼─────┐
      │ ①解析提取  │ docx2python / pdfplumber → 文本 + 表格标记 [表格n]/[表格结束]
      │           │ AI 流式: 项目概述 + 技术评分要求(提取域, 自我反思式结构化)
      └─────┬─────┘
            ▼
      ┌──────────┐
      │ ②目录生成  │ 一级(↔评分项"一一对应"锁死) → 二三级(分步并发) →
      │          │ JSON 解析 → Schema(Pydantic) → 业务校验 → JSON 修复 →
      │          │ 多轮重试 → 分步生成 → 目录审核 → 建议回灌 → 二次生成
      └─────┬─────┘
            ▼
      ┌──────────┐
      │ ③全局事实  │ 三步：生成设定(切片提取+知识库补充+优先保留原方案, 先给用户改)
      │          │       → 编排期(AI 看标题判需求, 程序按章节注入完整事实)
      │          │       → 生成后二次检查(分组查冲突, old_text/new_text 唯一命中才替换)
      └─────┬─────┘
            ▼
      ┌──────────┐  预编排 JSON: knowledge / table / mermaid / image
      │ ④正文生成  │ 叶子节点逐节 → 分层消息(system/概述/上级/同级/当前章 缓存命中)
      │          │              → 超长续写 → 图文控制(Mermaid 严禁输出)
      └─────┬─────┘
            ▼
      ┌──────────┐
      │ ⑤审核预检  │ 全文一致性审计(old_text/new_text 唯一命中替换) + 目录审核
      │          │ + JSON 校验修复原则 + SSE 实时反馈
      └─────┬─────┘
            ▼
      ┌──────────┐
      │ ⑥导出文档  │ Mermaid 渲染转图 / AI 生图 / 配图类型最终过滤(ai/mermaid/none)
      │          │ + 图片数量控制(maxAiImages 分段择优)
      └──────────┘
  支撑三章：知识库(非RAG) · opencode 集成 · 跨模块提示词顺序优化(缓存≈×10)
```

---

# 第一部分：六模块全功能说明（依据上游文档）

> 每个模块按：功能清单 / 工作流程 / 核心逻辑 / 提示词 / 数据结构 / 调用链路 / 容错机制 / 对应参考文档。

## 一、解析提取模块（对应《标书智能体（一）》）

### 功能清单
- 文件内容提取：Word(`docx2python`)、PDF(`pdfplumber` 按页+页码标识)、表格标记 `[表格n]/[表格结束]`。
- AI 流式请求封装：`AsyncOpenAI` 异步客户端、并发、`stream_chat_completion`。
- 项目概述提取：名称/背景/规模预算/时间/实施内容/技术特点。
- 技术评分要求提取：自我反思式结构化提示词，忽略商务/价格/资质。

### 工作流程
1. `parse_file_content` 抽文本（表格打标）→ 释放资源 `del content; gc.collect(); pdf.close()`。
2. `stream_chat_completion` 流式请求（temperature / response_format）。
3. 项目概述 System+User 提取。
4. 技术评分要求 System（自我反思式）+ 验证步骤（覆盖性、权重总和）。

### 核心逻辑
- 大且稳定上下文前置，任务差异放最后（提示词顺序优化，缓存命中）。
- 提取项分两类：项目概述（实施内容）、技术评分（技术评分/评标方法/评分标准/技术参数）。

### 提示词（要点）
- 项目概述 System：专业标书撰写专家，重点关注项目名称、背景目的、规模预算、时间安排、实施内容、技术特点；只关注实施相关内容，不提取商务信息；直接返回项目概述。
- 技术评分 System：目标定位技术评分章节，忽略商务价格资质；提取格式=评分项名称/权重分值/评分标准/数据来源；模糊表述按上下文判断；表格按行提取标 `[表格数据]`；分层结构用缩进或编号；单位统一分或%；验证覆盖与权重总和；只返回提取结果。

### 数据结构
```text
提取结果（结构化文本 / JSON）：
  项目概述: str
  技术评分要求: [{评分项名称, 权重/分值, 评分标准, 数据来源}]
```

### 调用链路
`上传文件 → file_parser → AI 流式封装 → 概述提取 → 评分提取 → 落库(解析文本/事实) → 下游(目录/事实/正文/导出)`

### 容错机制
- 流式异常 `yield` 错误信息，不中断整批。
- 资源释放保证（gc + close）。
- 提示词顺序优化减少重复输入成本（实测缓存命中后成本≈1/10）。

---

## 二、目录生成模块（对应《标书智能体（二）》《（五）》）

### 功能清单
- 短标书+强模型：直接输出三级 JSON 目录。
- 长标书+普通模型：先一级（↔评分项一一对应）→ 遍历二三级（并发不重复）→ 校验 JSON → 拼完整目录。
- 弱模型稳定输出复杂 JSON：自我反思 + 矫正机制。
- 目录审核 + 建议回灌。

### 工作流程（完整流程）
`用户输入 → 模型生成 → JSON 解析 → Schema 校验 → 业务校验 → JSON 修复 → 多轮重试 → 分步生成 → 目录审核 → 建议回灌 → 二次生成 → 返回结果`

### 核心逻辑
- 弱模型稳定输出不靠抽卡，靠机制：生成→JSON 语法校验→Pydantic Schema 校验→业务规则校验→失败定向修复→修复失败重试→完整失败切分步→生成后模型审核→不通过带建议重生成。
- 失败后**定向修复**：System 要求最小必要修改，保留结构/字段值/节点顺序，只返回修复后完整 JSON。
- **「一一对应模式」**：从技术评分提取评分大类，程序构造一级目录，**锁死标题顺序关联评分项**，模型只生成二三级；最后校验一级目录与评分大类一致。
- 编号/映射等确定性逻辑交给程序（`renumber_outline`）。

### 提示词（要点）
- 一级 System：只设计一级标题，数量与评分要求一一对应，标题简单修改，输出 JSON `{rating_item, new_title}`。
- 二三级：代码拼接 JSON 框架，仅让 AI 填内容；传入 `other_outline` 避免重复。
- 目录审核 System：严格目录审核专家；检查完整性/一级目录名称专业准确且与评分项原文一致/层级清晰达三级/遗漏错位重复；只返回 `JSON {passed, suggestions}`；不通过给具体可执行建议。
- JSON 修复提示词：只返回 JSON；最小必要修改；格式化错误与 Pydantic 错误明确告诉模型。

### 数据结构（Pydantic）
```python
class OutlineItem:        # 节点
    id: str
    title: str
    description: str
    source_requirement_id: str | None
    source_requirement_title: str | None
    children: list[OutlineItem]
    content: str | None
class OutlineResponse: outline: list[OutlineItem]
class OutlineChildrenResponse: children: list[OutlineItem]
class OutlineReviewResponse: passed: bool; suggestions: str
```

### 调用链路
`评分要求 → outline_level1(↔锁死) → outline_sublevel(分步并发, 带 other_outline) → check_json → collect_json_response(Pydantic+业务校验+修复+重试) → renumber → outline_review → 回灌重生成`

### 容错机制
- `check_json` 结构校验 + 3 次重试。
- 完整目录失败 → 自动切分步生成（程序统一重编号）。
- 校验错误明确回传模型，优先修已有结果而非推倒重来。
- 大 JSON 拆小 JSON。

---

## 三、全局事实模块（对应《AI生成标书，如何保证超长内容的全文一致性》）

### 功能清单
- 生成全局事实设定（切片提取 + 知识库补充 + 优先保留原方案 → 先给用户查看修改）。
- 生成期编排：AI 只看事实标题判需求，程序按标题取完整事实只注入当前章节。
- 生成后二次检查：分组查冲突 → 返回精确 `old_text/new_text` → 唯一命中才替换。
- Agent 修复模式（global-facts.md + technical-plan.md）。

### 工作流程（三步）
1. **生成设定**：按模型上下文限制切分招标文件 → 分段提取事实 → 知识库补充更具体不冲突的技术设定 → 已有方案扩写优先保留原人员/设备/周期/服务承诺 → 合并重复、删空泛 → 轻量事实清单 → **先让用户查看修改**。
2. **编排注入**：AI 只看到事实标题，判断本章需要哪些事实；真正生成时程序按标题取出完整内容，**只注入当前章节**。
3. **生成后二次检查**：按目录顺序把超长正文分组，每组 + 全局事实交 AI，只查已写出事实冲突；返回冲突章节/原文/违反事实；修复返回 `old_text/new_text`；**唯一命中才替换**。

### 核心逻辑
- 本质是「让 AI 有目的性的摘要」——不是招标要求摘要，是「本方案项目总工期为 90 天」这种可统一使用的事实。
- 根因：多线程并发，叶子节点相互独立 → 工期/响应/人员/设备/质保前后不一致。
- 纯靠长上下文不可行（《Lost in the Middle》关键信息在中间表现下降）；摘要方案行不通（必须串行、缺实质内容）。

### 提示词（要点）
- 生成设定：从招标文件分段提取后续正文反复使用、必须一致的内容；知识库补充不冲突设定；原方案优先；合并去重删空泛。
- 二次检查：只检查已明确写出的事实冲突，返回精确 `old_text/new_text`。

### 数据结构
```text
GlobalFact: { id, title, value, source, confidence, is_simulated, is_stale, is_resolved, has_conflict, chapter }
事实门控(注入条件): has_conflict=0 AND is_resolved=1 AND is_simulated=0 AND is_stale=0   # fail-closed
```

### 调用链路
`招标文件切片 → 事实提取 → 知识库补充 → 用户确认 → 编排期 per-chapter 注入(门控) → 正文生成 → 分组一致性审计 → old_text/new_text 修复`

### 容错机制
- 门控 fail-closed（四条件全满足才注入，漏一即不注入，绝不把模拟/矛盾事实喂给正文）。
- 修复唯一命中才替换；找不到或多处相同都拒绝并重新尝试。
- Agent 修复：程序重新解析校验（章节新增/删除/重排、ID/标记完整、修改范围、变化小节），校验通过只回写变化小节，失败保留原正文。
- 边界声明：不保证 100% 一致；全局事实本身错则一起写错；履约可行性/隐含逻辑/法律责任需人工复核。

---

## 四、正文生成模块（对应《标书智能体（三）》《（六）》）

### 功能清单
- 按提纲叶子节点逐节生成（分布式，避免一次性超长）。
- 分层消息提示词（缓存命中）。
- 超长文本生成 + 图文控制（四步：收集叶子 → 正文编排 → 并发生成 → 执行配图）。

### 工作流程
1. 收集所有叶子节点。
2. 对所有叶子节点做**正文编排**（JSON: knowledge/table/mermaid/image，priority 3/4/5）。
3. 根据编排并发生成正文。
4. 正文完成后执行配图。

### 核心逻辑
- 请求参数 `ChapterContentRequest(chapter, parent_chapters, sibling_chapters, project_overview)`：上级保连贯、同级避重复。
- 提示词顺序优化：system 稳定规则 / 项目概述单条 / 上级单条 / 同级单条 / 当前章任务最后（同父下多个叶子仅最后一条变化，缓存命中）。
- **预编排核心价值**：配图在正文生成前规划好，让正文围绕图片表达重点（避免两张皮），数量提前可控、不集中在前几章。
- 同级章节信息传给 AI 避免车轱辘话。

### 提示词（要点）
- 正文 System：专业准确、朴实无华不假大空、语言正式规范、详细具体、避免与同级重复、可使用 Markdown 段落列表表格、**严禁输出 Mermaid 等图表代码块**、表格单元格多项内容用编号顿号分号短句、直接返回章节内容（不生成标题、不额外说明）。
- 正文 User：项目概述 + 参考正文素材 + 上级章节 + 同级章节(避重复) + 正文编排决策 + 当前章节信息。
- 编排 System：只返回 JSON；判断克制合情合理；表格仅明显提升清晰度时用；Mermaid 只适合简单抽象文本节点型关系图；AI 生图适合设备/现场/机柜/系统架构/部署拓扑/施工运维场景；priority 3 候选/4 推荐/5 强推荐；`knowledge.item_ids` 只能从参考知识库轻量条目选。

### 数据结构
```python
class ChapterContentRequest:
    chapter: {id, title, description}
    parent_chapters: list           # 上级章节
    sibling_chapters: list          # 同级章节(避重复)
    project_overview: str
# 编排 JSON
编排 = {
  knowledge: {item_ids: [...]},
  table: {needed: bool},
  mermaid: {needed, title, code, priority, reason},
  image:  {needed, style, title, prompt, priority, reason},
}
```

### 调用链路
`叶子节点 → (编排: 返回 knowledge/table/mermaid/image) → per-leaf 分层消息 → 流式生成 → 超长续写 → 同步登记图表 → 配图执行(渲染/生图)`

### 容错机制
- 超长文本续写（多轮 `should_continue_round`）。
- 正文严禁 Mermaid 代码块（防止正文与图表两张皮、渲染失败）。
- 配图类型最终过滤：AI 提名、程序拍板，`illustration_type ∈ {ai, mermaid, none}`。
- 同级信息注入避免重复。

---

## 五、审核与预检模块（对应《AI生成标书…一致性》+ 审核原则）

### 功能清单
- 全文一致性审计（普通修复 + Agent 修复）。
- 目录审核（JSON `{passed, suggestions}` 回灌）。
- JSON 校验与修复原则。
- SSE 长流程反馈。

### 工作流程
- 普通修复：分组(目录顺序) + 全局事实 → AI 查冲突 → 返回冲突章节/原文/违反事实 → 修复返回 `old_text/new_text` → 唯一命中才替换。
- Agent 修复：global-facts.md + technical-plan.md，Agent 搜索关键词/分段读/建索引/多轮改；程序重新解析校验 → 只回写变化小节。

### 核心逻辑
- 自动审计擅长找工期/人员/数量/型号/质保/响应时间等明确冲突；履约可行性/隐含逻辑/法律责任需人工复核。
- 若全局事实本身错，后面会一致地一起写错（边界声明）。

### 提示词（要点）
- 目录审核 System：严格目录审核专家；完整性/一级名称专业准确且与评分项原文一致/层级清晰达三级/遗漏错位重复；只返回 `JSON {passed, suggestions}`；不通过给具体可执行建议。
- 一致性审计：只查已明确写出的事实冲突，返回精确 `old_text/new_text`。

### 数据结构
```text
冲突: { 章节, 冲突原文, 违反事实 }
修复: { old_text, new_text }   # 唯一命中才替换
目录审核: { passed: bool, suggestions: str }
```

### 调用链路
`正文分组 → 一致性审计(模型) → consistency_edits.find_unique_span → 唯一命中替换 / 失败重试 → 目录审核回灌 → SSE 进度`

### 容错机制
- 唯一命中才替换；找不到/多处相同拒绝并重新尝试。
- 编号/映射确定性逻辑交程序。
- 大 JSON 拆小 JSON；修复失败优先修已有结果。
- 长流程 SSE 告知用户系统正在做什么。

---

## 六、导出文档模块（对应《标书智能体（六）》）

### 功能清单
- Mermaid 执行链路：前端渲染 / 正文存代码块 / Word 导出转图。
- Mermaid 配图流程：先校验渲染，不能渲染再 AI 修复。
- AI 生图：用编排 `image.prompt` + 固定工程风格。
- 配图类型最终过滤（ai/mermaid/none，程序拍板）。
- 图片数量控制（maxAiImages 分段择优）。

### 工作流程
1. Mermaid：编排返回 code → 校验能否渲染 → 不能则 AI 修复（flowchart TD、ASCII 节点 ID、中文 `A["中文标签"]`、`最小必要修改`）→ 修复成功追加 ```mermaid ... ``` + 图标题到正文后。
2. AI 生图：用 `image.prompt` + 固定工程风格（结构清晰、专业克制、避免品牌水印营销无关文字；实景风格真实克制）。
3. 配图类型最终过滤：即使 AI 判定既可用 mermaid 也可用 AI 生图，程序过滤只保留一种。
4. 数量控制：AI 提名候选，按 `maxAiImages` 分段择优（避免前几章耗尽额度）。表格：少量≤20%/适中≤40%/大量宽松。

### 核心逻辑
- 存代码块比存图片更灵活（预览/存储/导出三层解耦）。
- Mermaid 语法敏感，AI 常写出渲染失败代码 → 必须校验+修复闭环。
- 配图不是写完再加，而是生成前规划（成本可预估、分布均匀、正文呼应）。

### 提示词（要点）
- Mermaid 修复 System：只返回 JSON；让 Mermaid 浏览器稳定渲染；优先最小必要修改；优先 flowchart TD；节点 ID 仅 ASCII 字母数字下划线；中文标签 `A["中文标签"]`；不用多节点连接简写；不用分号；不输出 Markdown 代码围栏；结构过复杂则简化为可渲染核心流程图。
- AI 生图：工程项目图示风格，结构清晰专业克制，适合投标技术方案插图；避免品牌标识水印夸张营销无关文字；实景风格真实克制。

### 数据结构
```text
illustration_type ∈ {ai, mermaid, none}      # 程序最终拍板
image: { prompt, style, title, priority }
mermaid: { code, title }
maxAiImages: int   # 分段择优上限
```

### 调用链路
`正文图表块 → Mermaid 校验渲染(mermaid_renderer) 失败→AI 修复 → AI 生图(image_engine) → 类型过滤 → 数量控额 → 插入 docx/PDF`

### 容错机制
- Mermaid 渲染失败降级为 AI 修复，再失败则跳过/告警（不阻断导出）。
- 配图类型互斥过滤，避免小节内图过多。
- 数量分段择优，防止前段耗尽预算。

---

## 七、知识库模块（非 RAG）（对应《新系列一》）

### 核心逻辑
- 不用 RAG：embedding 易召回语义接近但无关内容（消防改造被装修改造抢占），给 AI 错误参考不如不给。
- 灵感来自 skill 技术：元数据描述「何时使用」，请求时携带所有 skill 描述，AI 判断后才读真内容。
- 构建：切分 block(标题/段落/表格/列表+语义合并+编号) → 清理无效 block(页码/目录/封面/签章/碎片) → 第一轮提取条目(标题+使用方式) → 第二轮补漏 → 合并去重编号 → 分批匹配原文(返回 block 范围) → 遗漏 block 补漏 → 保存(标题+使用方式+原文素材)。

### 数据结构
```text
KnowledgeItem: { id, title, usage(使用方式), source_blocks: [block_id], content(原文素材) }
Block: { id, text, type }
```

---

## 八、opencode 集成（邪修）（对应《新系列二》）

### 核心逻辑
- 问题：不同模型生成多层级 JSON 暴露不同问题；开源软件无法逐模型定向优化；重试 3 次仍可能修不完/格式错/极个别模型删报错部分。
- 方案：内嵌 opencode runtime，http 端点 api 通讯；重写 AI 服务商配置代理到本工具箱 AI 配置共用；把 JSON 校验修复/全文一致性审计/原文覆盖率审计交给 opencode。
- 不用 Agent+Skill：超长上下文(100 万字 agent 处理不来)、步骤多(单轮对话处理不完)、缓存优化(固定工作流 DeepSeek 缓存 40–70%，agent 无法控制)。

---

## 九、跨模块提示词顺序优化（缓存命中）（对应《标书智能体（四）》）

### 核心规则
- system 只放稳定规则（角色/写作规范/输出要求）。
- 最大最稳定上下文前置：招标文件全文、项目概述、技术评分要求、目录树、上级章节链。
- 任务差异放最后：提取概述 / 提取评分 / 生成目录 / 生成 3.2.1 正文。
- 同一份数据组织格式稳定：标题写法/换行数/列表顺序一致，不随意 strip，不塞随机时间戳进共享上下文。

### 实测
- OpenRouter + gemini-2.5-flash 解析：第一次 prompt 19349 / cached 0 / cost 0.0058047；第二次 prompt 19737 / cached 19442 / cost 0.00067176 → 成本≈1/10。

---

# 第二部分：本仓库（专项方案工具箱）实现比对

## 2.1 落点映射表（来自代码探索）

| 模块 | 本仓关键文件 | 关键符号 |
|---|---|---|
| 解析提取 | `backend/app/services/file_parser.py` | `parse_file_content / normalize_pdf_text / find_table_header / render_table_with_header` |
| 解析提取 | `backend/app/services/bid_analysis_service.py`(~2000行，**非 bid_analysis.py**) | `get_items_by_domain / build_task_prompt / build_system_messages / split_for_analysis / AnalysisConfig` |
| 解析提取 | `backend/app/config.py` | `ANALYSIS_ITEMS / BID_RESPONSE_ITEMS / EXTRACTION_DOMAINS` |
| 解析提取 | `backend/app/services/ai/json_response.py` | `collect_json_response / extract_json / parse_and_validate` |
| 目录生成 | `backend/app/services/ai/prompts/outline.py` | `outline_level1_system / outline_sublevel_system / outline_review_system / outline_sublevel_batch_system` |
| 目录生成 | `backend/app/services/outline_utils.py` | `clamp_outline_depth / normalize_outline / normalize_outline_json` |
| 目录生成 | `backend/app/routers/sse_handlers.py` | 分步链路(一级→二三级→审核) + SSE |
| 全局事实 | `backend/app/services/facts_extractor.py` | `run_extraction_pipeline / get_facts_inject_where(门控唯一出口, L2529) / build_injectable_facts_query` |
| 全局事实 | `backend/app/services/facts_enrich.py` | `run_knowledge_global_fact_patches / finalize_facts` |
| 全局事实 | `backend/app/services/facts_patches.py`(~620行) | `FactPatch / apply_patches_to_fact_items / get_segment_limit` |
| 全局事实 | `backend/app/services/placeholder_inventory.py` | fail-closed 门控 |
| 正文生成 | `backend/app/routers/sse_handlers.py`(主循环 ~L4699–6290) | 叶子逐节 + 分层 + 超长续写 |
| 正文生成 | `backend/app/services/content_runtime.py` | `build_chapter_user_content / build_continuation_messages / should_continue_round` |
| 正文生成 | `backend/app/services/ai/prompts/content.py` | 分层提示词 + Mermaid 严禁/每章≤1图 |
| 审核预检 | `backend/app/services/consistency_scanner.py` | `run_scan / ai_scan_section / merge_conflicts` |
| 审核预检 | `backend/app/services/consistency_edits.py` | `find_unique_span`(唯一命中替换) |
| 审核预检 | `backend/app/services/repair_agent.py` `review_autofix.py` `preflight_engine.py` | old_text/new_text 修复 / 程序化+AI 修复 / 导出预检 |
| 导出 | `backend/app/routers/export.py`(~4300行，**非 services/export.py**) | `export_docx / export_pdf / audit_content / placeholder_report` |
| 导出 | `backend/app/services/ai/mermaid_renderer.py` | `render_mermaid_to_bytes / ChartCache` |
| 导出 | `backend/app/services/ai/image_engine.py` | `generate_illustration_prompt / arrange / image / validate_mermaid / repair_mermaid` |
| 公共 | `backend/app/db.py`(~850行) | `safe_rowcount` + 九大章节四维标注 schema |
| 公共 | `backend/app/services/content_utils.py` | 围栏/图片占位等通用工具 |

## 2.2 差异清单

### A. 本仓已覆盖（与上游对齐）
- 解析：docx/pdf 抽文本 + 表格标记 + 流式 AI 封装（`file_parser` + `providers`）。
- 目录：JSON 校验/修复/重编号/分步 + 目录审核回灌（`json_response.collect_json_response` + `sse_handlers` 分步链路 + `outline_review`）。
- 全局事实：三步法 + 门控 fail-closed 唯一出口（`get_facts_inject_where` 四条件）+ 生成后二次检查（`consistency_scanner` + `consistency_edits.find_unique_span`）。
- 正文：叶子逐节 + 分层消息 + 超长续写（`sse_handlers` 主循环 + `content_runtime`）。
- 审核：一致性审计 old_text/new_text 唯一命中 + 目录审核回灌 + SSE。
- 导出：Mermaid 渲染转图 + AI 生图 + 类型过滤。
- 提示词顺序优化：分层消息已在正文/目录落实。

### B. 上游有 / 本仓差异（重点核查项）
1. **预编排配图 vs 同步登记**：上游明确「正文生成前先做 knowledge/table/mermaid/image 预编排，预编排结果传给正文模型让正文围绕图写」。本仓 `sse_handlers.py` L4721 注释**已移除预编排，改为图表与正文一体生成、同步登记**。（见缺口 G1）
2. **`maxAiImages` 分段择优全局控额**：上游「全文 80 小节 AI 提 20 个，用户限 6 张，程序分段选每段优先级最高，避免前段耗尽」。本仓目前落在「每章≤1 图表块」硬约束 + 编排阶段上限，未确认有「跨章分段全局择优」逻辑。（见缺口 G2）
3. **「一一对应模式」程序锁死关联**：上游强调一级目录由程序构造、标题顺序锁死关联评分项。本仓 `outline.py` 有「一一对应」提示词，需核实是否有**程序侧**把一级目录 id 与评分项 id 持久化绑定、生成二三级后回校验一致的逻辑。（见缺口 G3）
4. **JSON 修复「定向最小必要修改 + 错误明确回传」**：上游要求修复时把 Schema/业务错误明确告诉模型、只做最小必要修改。需核实 `json_response` 的修复分支是否把 `parse_and_validate` 的具体错误文本回传。（见缺口 G4）
5. **知识库「非 RAG 轻量条目」在正文编排中的 `knowledge.item_ids` 选择**：上游编排 `knowledge.item_ids` 只能从参考知识库轻量条目选。本仓 `facts_enrich` 有知识库补充事实，但编排期的 `knowledge.item_ids` 选择机制需核实是否与上游一致。（见缺口 G5）

### C. 本仓已超出（扩展，上游无）
- 九大章节体系（建办质〔2018〕31号）+ 事实四维标注（`db.py` schema / `scheme_classification.NINE_CHAPTERS`）。
- 危大工程分类与阈值（`HAZARD_THRESHOLDS` 确定性阈值，闭区间修正）。
- 编号统一（`numbering.py` + 导出前严格一致性校验 + 回滚）。
- 双提取域（`scheme` / `bid_response`，`EXTRACTION_DOMAINS`，加法引入零迁移）。
- 模拟值闸门 / fail-closed 事实门控（`get_facts_inject_where` 唯一出口 + `global_facts` 模拟值禁注入）。
- 知识库补充 + finalize（默认关闭以零新增 AI 调用）。
- 解析截断修复（`pdf_text_max_pages=500`、截断 SSE 告警、`_invalidate_extraction_derived`）。

### D. 命名/架构差异（比对时用，非缺陷）
- 上游 `bid_analysis.py` → 本仓 `bid_analysis_service.py`；上游 `services/export.py` → 本仓 `routers/export.py`。
- 上游 `ChapterContentRequest` 在本仓无同名，等价由 `sse_handlers` 主循环 + `content_runtime.build_chapter_user_content` 承担。
- 上游 `charts.py` 在本仓拆分为 `routers/charts.py` + `services/ai/mermaid_renderer.py` + `chart_validators.py` + `chart_payload.py`。
- 前端目录因 `j:` 盘枚举超时未能精确列出（建议后续单独检索 `frontend/`）。

---

# 第三部分：实现方案（先方案后动手，不盲改）

> 下列为**方案**，待你确认后逐个实施。每项均标注：依据 / 本仓复用点 / 方案 / 风险 / 验证。优先做零迁移、加法式、默认关闭的改造。

## G1. 预编排配图 vs 同步登记（架构级取舍，建议「文档化」而非回退）
- 依据：上游 §四.5 预编排；本仓 `sse_handlers.py` L4721 已移除预编排。
- 本仓复用点：现有同步登记已稳定运行多轮（含 Mermaid 严禁、每章≤1 图护栏）。
- 方案：**不回退**。在 `content.py`/`sse_handlers` 注释中显式记录「本仓采用图表与正文一体生成，等价于上游预编排的效果（正文知道本小节会配图、可围绕表达），但实现路径不同」。属已超而非缺。
- 风险：无（不改行为）。验证：无需测试，仅补注释 + 在本文档 C 类登记。

## G2. `maxAiImages` 跨章分段全局控额（建议新增，默认关闭）
- 依据：上游 §六.5 分段择优，避免前段耗尽额度。
- 本仓复用点：`image_engine.generate_illustration_arrange` 已有编排；`config.py` 可加 `max_ai_images`（默认 0=沿用现有每章硬约束，行为不变）。
- 方案：新增 `config.max_ai_images`；当 >0 时，把候选小节按目录分段，每段内按 `priority` 选 top-k，跨段总额 ≤ `max_ai_images`；零迁移（默认 0 时旧行为逐字不变）。
- 风险：低（加法 + 默认关闭）。验证：单测覆盖「分段择优」「前段不耗尽」「默认 0 行为不变」；A/B 反向验证（还原 → 定向失败；恢复 → 全绿）。

## G3. 一级目录与评分项「程序锁死」关联（建议核查后补强）
- 依据：上游 §二.4「一一对应模式」程序构造一级目录、锁死标题顺序关联评分项。
- 本仓复用点：`outline.py` 已有 `outline_level1_system` + `outline_sublevel_batch_system`（要求一一对应）；`bid_analysis_service.get_items_by_domain("bid_response")` 提供评分项。
- 方案：核实是否已在 `sse_handlers` 分步链路里把一级目录 `source_requirement_id` 持久化；若无，在生成二三级后加一步回校验（一级标题顺序与评分项顺序一致），不一致则告警回灌。默认沿用现有行为，新增校验 fail-soft。
- 风险：中（涉及目录生成主链路）。验证：单测「一级↔评分项顺序一致校验」「缺失关联告警」；现有 outline 用例全绿回归。

## G4. JSON 修复「错误明确回传 + 最小必要修改」（建议核查后补强）
- 依据：上游 §二.4 / §五.3 把 Schema/业务错误明确告诉模型、最小必要修改。
- 本仓复用点：`ai/json_response.py` 的 `extract_json / parse_and_validate / collect_json_response`。
- 方案：核实 `collect_json_response` 修复分支是否把 `parse_and_validate` 抛出的具体错误（Pydantic field / 业务规则）拼入下一轮 prompt；若未拼，补「错误回传 + 最小必要修改」约束。默认关闭新约束（或仅追加提示词，零行为变更风险）。
- 风险：低–中。验证：单测「修复轮收到具体错误文本」「仍保留原结构与字段值」；A/B 反向验证。

## G5. 编排期 `knowledge.item_ids` 选择机制对齐（建议核查）
- 依据：上游 §四.5 编排 `knowledge.item_ids` 只能从参考知识库轻量条目选。
- 本仓复用点：`facts_enrich` 知识库补充；`content_runtime.build_chapter_user_content` 预取知识行。
- 方案：核实编排/正文期知识条目来源是否限定为「轻量知识库条目」而非全量事实；若范围过宽，收敛为轻量条目集合。默认行为不变，仅收敛数据源。
- 风险：低。验证：单测「知识条目来源为轻量集合」「不泄漏模拟/矛盾事实（复用门控）」。

## 实施顺序建议（待确认）
1. **G1 文档化**（零风险，立即）。
2. **G2 默认关闭新增**（零迁移，独立可测）。
3. **G4 错误回传**（核查+补强，低风险）。
4. **G3 / G5**（核查后补强，中风险，需回归 outline/知识库用例）。

> 全部改造遵循你的协作偏好：先出方案 → 零迁移复用既有表/引擎 → 每轮补回归测试并 `pytest` + `py_compile`/`tsc` 跑通后才收口。

---

## 附：参考文档索引（上游 9 篇 ↔ 本解析章节）
| 上游文档 | 对应本解析 |
|---|---|
| 《AI生成标书，如何保证超长内容的全文一致性》 | 三、全局事实；五、审核预检 |
| 《标书智能体（一）——AI解析招标文件》 | 一、解析提取 |
| 《标书智能体（二）——生成标书提纲》 | 二、目录生成 |
| 《标书智能体（三）——生成标书正文》 | 四、正文生成 |
| 《标书智能体（四）——提示词顺序优化》 | 九、跨模块提示词顺序优化 |
| 《标书智能体（五）——弱模型稳定输出复杂json》 | 二、目录生成（弱模型机制） |
| 《标书智能体（六）——超长文本生成和图文控制》 | 四、正文生成；六、导出文档 |
| 《新系列一：知识库非RAG》 | 七、知识库模块 |
| 《新系列二：集成opencode》 | 八、opencode 集成 |
