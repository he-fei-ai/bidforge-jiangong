# AGENTS.md — 专项方案工具箱 · AI Agent 工作边界

> 本文件定义 AI 编程代理（Coder/Agent）在本仓库工作时的行为边界。参考 OpenBidKit Yibiao 的 AGENTS.md 引入（2026-09-22）。
>
> **最近校准：2026-10-01（第二十轮）** —— **【目录库模块】六大类危大工程逐型式全面更新**（依据住建部令第 37 号 + 建办质〔2018〕31 号，全部**默认向后兼容**、零新增依赖、零数据迁移）：
> ① **内建模板库 22→48 个 builder**（`outline_templates.py`）：新增声明式装配助手 `_chapter`/`_nine_chapter`（九章骨架与既有 builder 同源，编号仍由 `renumber_outline` 统一重排）；脚手架拆 9（落地/附着/悬挑/门式/碗扣/盘扣/吊篮/卸料平台/操作平台）、模板支撑拆 2（高大/盘扣）、起重拆 2（塔机/升降机）、拆除拆 3（人工/机械/爆破）、其他危大拆 10（幕墙/钢结构/网架索膜/预应力/暗挖/顶管/水下/挖孔桩/边坡/四新），未指明型式的通用名仍落原兼容兜底模板。
> ② **RULES 细粒度路由前置 + 跨类误伤收口**：「落地式钢管脚手架」此前被误路由到通用 scaffold（不含「落地式脚手架」子串）；卸料/操作平台规则必须前置于架体型式规则。新增条目 TEMPLATE_META 编制依据**四层齐备**（法律法规/部门规章/强制性规范/专项技术规范，标准号仅复用条目内已确认的，不杜撰）。
> ③ **seed 预置清单 `SEED_VERSION` v2.0→v3.0**：脚手架/模板/起重/拆除分类补齐逐型式条目，新增「其他危大工程」分类（13 分类 / 178 条）；⚠️ seed 以「{方案名}标准目录」为唯一键，跨分类**重名会互相覆盖 type**（本轮曾引入幕墙/钢结构重名，已改差异化名）。`seed_catalog` 新增**进程内重入锁 + INSERT 前按名复查**双层并发护栏（旧实现基于开头 existing 快照判存在，并发重入各自全量 INSERT → 清单重复）。
> ④ **【P1 断链】目录生成「按类别自动匹配目录库」此前形同虚设**（开关 `scheme_auto_match_outline` 默认关未暴露）：a) `bid_analysis` 写入的 `schemes.hazard_category` 是**逗号连接多类别码**，`sse_handlers` 旧代码整串当单个 type 传 IN 查询 → 多类别方案恒不命中，现按逗号拆分；b) 类别码（foundation_pit/scaffold…）与预置库 type（中文分类名「基坑与土方」…）**两套体系不同源**，`outline_reference` 旧版按 `type IN (类别码)` 对预置库恒不命中，新增 `CATEGORY_CODE_TO_SEED_TYPES` 映射（仅追加候选，原始码/中文 type 向后兼容）。
> 护栏：新增 `tests/test_outline_library_full_update_20261001.py` **169 例**（注册表 parity + 九章骨架 + builder 无状态/零缓存 + 42 路由探针 + meta 四层 + seed 端到端幂等/force/stale 清理/并发重入 + 自动匹配断链锁），**A/B 反向验证 3 项**（还原逗号拆分/破坏映射键/去锁去插前复查 → 各 1 例定向失败；恢复 → 全绿）。另修掉 2 处挡基线的**上一轮 WIP 遗留缺陷**：`sse_handlers._persist_section` 缺 `section_title` 形参致 F821（正文全章落库路径 NameError）；`content_fuzzy._sentence_at` 的 `lo` 初值误为 `len(text)`（句中无前置句末符时整句切空 → 有锚点的「按合同要求」被误判空话）+「细节另见」漏规则。全量 **4578 passed, 0 failed**（基线 3886→4578 含第 16~19 轮增量，无回归）。
>
> **最近校准：2026-10-01（第十九轮）** —— **【需求变更】导出不再受审核 / 预检完成度限制**：用户明确要求「导出文档不再因审核与预检模块未完成而无法导出，不做限制」。此前唯一的门禁是**纯前端**的 `frontend/src/utils/exportCharts.ts::deriveExportGate`（未预检 / 存在 high 问题 / 就绪度未跑·过期·未放行五种情况均 `allowed:false`），并被 `SchemeWorkbenchPage` 用于 `disabled` 与 `handleExport` 的 `msg.warning` return —— 结果是**审核与预检没做完就完全无法导出**；而后端 `POST /export/docx`、`/export/pdf` 从来不校验这两项（唯一前置守卫是编号一致性，且默认只 warning）。现把门禁降级为**只提示不阻断**：`deriveExportGate` 恒 `allowed:true`（保留 `highIssueCount` 用于非阻断提示），导出页仅在存在 high 问题时显示一条可忽略的 warning Alert。⚠️ 注意：`HIGH_EXPORT_ISSUE_TYPES` 与后端 `_EXPORT_ISSUE_RULE_MAP` 的 **parity 护栏继续有效**——该表仍决定提示中的问题计数，且**不得**据此恢复任何阻断逻辑。既有门禁用例（`exportCharts.test.ts` / `exportGateParity20260927.test.ts` / `schemeWorkbench.test.tsx` 共 10 例）已同步改为「一律放行」的反向断言。
>
> **最近校准：2026-10-01（第十八轮）** —— 依据 `docs/专项方案六模块全功能规格_角色转换版.md` §4.1 缺口总表落地**第一批 + 第二批**（全部零风险 / 零迁移）：
> ① **【P0 判据分叉】JSON 模式兼容性判定收敛为单一出口**（G9）：该判据此前在 `provider_factory`（编排层）与 `providers/openai_compatible`（HTTP 层）**各有一份**、且都只认英文关键词 → 国内厂商返回**中文**错误（「该模型暂不支持 JSON 输出」）时两端均漏判：不摘字段重试、不回退普通模式，且把「参数不兼容」计入**熔断失败 + 配额冷却**，整条候选链用同一原因逐个失败，**全部 JSON 类任务**（目录生成 / 事实提取 / 一致性审计）同时挂死。因两端互相 import 会形成循环依赖，判据下沉到 `services/ai/json_mode_compat.py`，两处改为**薄包装**（保留原函数名与签名，调用点零改动）。
> ② **【P1 漏报】跨章节段落搬运检测**（G10，新增规则 `CON-06`）：`CON-05` 只做**整章级** Jaccard，漏掉「整章不同但成段照抄」——恰是 AI 生成最常见的雷同形态。新增 `services/duplicate_detection.py`（**骨架归一** + 2-gram Dice 双阈值分级，纯程序零 AI 零成本），能判定「深度 3m→5m / GB 50202→50204」这类**换数字照抄**；`RULE_VERSION` → 1.7.0。**本轮自引入又当场修掉 3 个缺陷**：句末符误含 `:`（把坡度 `1:0.75` 从中间切开）、先剔标点再套规则（`95%`→`95` 致百分数规则失效）、占位符花括号被清理吞掉。
> ③ **【P1 静默漂移】四套分类体系一致性护栏**（G11）：危大六大类 / 九章 `category_fields` / 危大 10 章 / 14 条阈值表**互相引用却零断言** → 新增 parity 护栏，锁定闭区间口径（脚手架 24m 漏判事故的防线）。
> 护栏 **118 例**（60 + 40 + 18），**A/B 反向验证 12 项**（7/5/9/1/3/1/1/3/2/1/1/1~2 例定向失败）。⚠️ 顺带修掉 2 个**既有回归**（`global_facts.py` 引用未定义的 `doc`/`e`；`test_parse_truncation_chain` 写死已被提升的 30000 预算 → 恒假失败）。详见 §4.21。
>
> **最近校准：2026-09-30（第十五轮）** —— **解析提取模块 · 遗留项收口**（第十一轮 §4.15.5 的 8 条遗留项全部收口或判定为非缺陷）：
> ① **【P1】标段上下文改为独立第二条 system 消息**（对齐易标 `buildTenderContextMessages`）：`build_system_prompt` 的 docstring 与 `bid_analysis.py` 注释都声称「独立 system 消息注入」，**实现却是 `prompt +=`** —— 注释与实现长期不符。拼进同一条时标段限定会与「必须逐条输出结构化结果」等硬性纪律混在一段里，弱模型容易当成可忽略的上下文。新增 `build_system_messages()`（加法式），`build_system_prompt` 一行未动；无 hint 时输出与旧版**逐字一致**。
> ② **【P1】`format_downstream_context` 预算上限**：两域共 36 项全量下发实测可达 10 万字以上且每章重发一次。新增 `_apply_downstream_budget`（12k 总量 / 2k 单项，两级裁剪**均留痕**）。
> ③ **【P2】目录 / 正文侧读截断诊断**：`load_parsed_docs`（limit=5/3）此前在**残缺文本**上工作且无任何提示。现三层降级读 `parse_truncated` / `parse_warnings`，并**恒记 WARNING**（可观测性不依赖调用方记得传参）。**截断不阻止使用**。
> ④ **【P2】提取项重跑的级联失效补齐**：此前**只**清 `export_cache`，漏掉 `consistency_scan_cache` / `schemes.facts_updated_at` / `doc_extractions` 三处同为「提取结果」派生的产物。新增 `_invalidate_extraction_derived`（幂等 + fail-soft + 按方案/项目精确限定）。
> ⑤ **两条判定为非缺陷**（避免重复排查）：「提取层物化只覆盖 5/18」实为**非预留类别已 100% 覆盖**（`boq` 是 RESERVED、`global_facts` 走 `sync_extract_layer` 独立分支）；「资料集合三链不一致」实为**按下游职责分别设定**。
> ⑥ **本轮自引入又当场修掉的 2 个缺陷**：预算首段 `head` 无条件保留 → 单一巨型小节时**完全绕过上限**；`_invalidate_extraction_derived` 里用了未定义的 `real_pid`（形参是 `project_id`）→ 6 个既有用例全挂。
> 护栏：新增 `tests/test_bid_analysis_legacy_closeout_20260930.py` **29 例**；A/B 反向验证 4 项（3/1/5/6 例定向失败，恢复 → 51 全绿）。详见 §4.19。
>
> **最近校准：2026-09-30（第十四轮）** —— **解析提取模块 · 截断与静默丢数据**（起点是读后台日志 `结构化提取依据不完整：… 被截断 招标文件.pdf`）：
> ① **【P0 数据丢失】PDF 文本层页数上限硬编码 50**：`file_parser.MAX_PDF_PAGES` 不可配置，而招标文件 / 施工组织设计常见 100~400 页 —— 一份 300 页的招标文件只提取前 50 页，**后面 250 页的工程参数、清单、图纸说明全部丢失**，且不进入目录 / 正文 / 事实 / 导出任何一级。改为读新增配置 `pdf_text_max_pages`（默认 **500**，与 `MAX_PARSED_CHARS=400000` 字对齐）；仍保留模块级常量，既有 `monkeypatch` 单测全部照常生效。
> ② **【P0 静默截断】截断文档被当成完整依据**：`generate_facts` 的源文档 SQL 只取 `(file_name, parsed_markdown)`，**从不读** `parse_truncated` / `parse_warnings` —— 而解析阶段早已写入该两列（`bid_analysis` 同类读路径也早已消费，本链路是漏改点）。新增 `_load_facts_source_docs()` 单一读取出口（三层降级：列存在 → `parse_warnings` 文本兜底 → 基础两列），并以 **SSE `warning` 事件**显式告知用户。
> ③ **【P1】「有 N 份文档尚未解析」告警恒不触发**：旧代码 `pending_docs = [d[0] for d in docs if not d[1]]`，而 `docs` 的 SQL 已 `AND parsed_markdown != ''`（每行正文都非空）→ 该列表**恒为 `[]`**；「全部文档未解析」时还会误报「项目下没有已上传的资料文档」。抽出 `_count_docs_pending_parse()`。
> ④ **防呆**：`_resolve_pdf_max_pages()` 对**非正数**与**非整数**均回落 50 —— 返回 0 会让 `doc.pages(0, 0)` 一页不解析且**不产生任何告警**；`int(3.7)=3` 会让「配了个小数」静默变成「只解析 3 页」。
> 护栏：新增 `tests/test_parse_truncation_fix_20260930.py` **25 例**；A/B 反向验证 4 项（2/3/4/4 例定向失败，恢复 → 25 全绿）。详见 §4.18。
>
> **最近校准：2026-09-30（第十三轮）** —— **全局事实模块遗留项全部收口**（12 轮 §4.16.5 记录的 5 条遗留项全落地，三个新增开关**默认全部关闭**，关闭时行为逐字节一致）：
> ① **缺值模式值域收敛为单一出口**：发现 `sse_handlers.generate_facts` 与 `facts_extractor` **两处**共 3 份合法值域字面量 → 全部改调 `facts_patches.normalize_missing_value_mode`。漏改一处 → 入口放行新模式而下游按 fabricate 处理，用户选了却看到「合理补全」结果且**无任何报错**。
> ② **知识库补充阶段**（易标 :876-891）：新增 `facts_enrich.apply_knowledge_patches`。⚠️ **更正第十二轮的一处事实性错误**——当时写「本仓无对应数据源」，实际 `knowledge_base` 表早已存在并被目录/正文生成消费，只是**事实链路从不读它**。
> ③ **最终整理阶段**（易标 :909-920）：新增 `facts_enrich.finalize_facts`（去重 + 要求句改写为事实句 + 强制保留工期）。**只改写不增删**，AI 臆造的事实一律丢弃并有 400 条上限。
> ④ **上下文预算分段接线**（易标 :363-377）：新增 `facts_extractor.resolve_chunk_size`；`max(预算, CHUNK_SIZE)` 保证窗口很小时不比历史基线更激进（否则段数爆炸）。
> ⑤ **英文归一化键走九大章节**：新增 `FACT_KEY_TO_CHAPTER`，把 `fact_key` 贯通 4 个调用点（新增参数可选、默认空串，既有 4 参调用逐字不变）。此前 `classify_fact_dimensions` 收了 `fact_key` 却从未传给 `classify_chapter_from_text`。
> ⑥ **2 个新 scene 已登记** `KNOWN_SCENES`（`facts_knowledge_patch` / `facts_finalize`）—— 漏登记会让场景模型路由静默失效。
> ⚠️ 两个设计取舍：两阶段均 **fail-soft**（失败记 warnings 后继续，绝不让已成功的提取整批丢失）；**AI 幻觉锚点一律降级为新建**（挂到不存在的锚点上会让补丁静默丢弃、用户却以为已补充）。
> ⑦ **本轮自己引入又当场修掉的缺陷**：`_apply_item_updates` 的 `row` 是 `sqlite3.Row`（**无 `.get()`**），该函数第 1066 行本就写着这条警告却仍踩中，打挂 4 个既有用例——**前 4 个相关测试文件全绿、只有全量才暴露**。
> 护栏：新增 `tests/test_global_facts_legacy_closeout_20260930.py` **47 例**；已做 **4 项 A/B 反向验证**（3/8/9/10 例定向失败，恢复 → 137 全绿）。详见 §4.17。
>
> **最近校准：2026-09-30（第十二轮）** —— **全局事实模块引入易标的「补丁机制 + 分批合并 + 上下文预算 + 缺值模式措辞」**（全部**默认向后兼容**、**无新增配置项**、**无新增依赖**、**零数据迁移**）：
> ① **只引纯逻辑、不引流程编排**：易标 `globalFactsTask.cjs`（1031 行）的 `runGlobalFactsTask` 是一条 6 步流水线，强依赖其 `workspaceStore` / `knowledgeBaseService` / `agentService` 三件套；本仓对应编排已在 `sse_handlers.generate_facts` 且经 11 轮校准，照搬会产生**第二套编排**。故只引入与编排无关的纯函数。
> ② **11 个纯函数逐条对齐**：新增 `app/services/facts_patches.py`（606 行，23 个顶层函数），`normalizeFactId` / `ensureUniqueId` / `valueToMarkdown` / `buildMissingFactRule` / `buildGlobalFactsCompletenessRules` / `normalizeGlobalFactsPatchResponse` / `validateGlobalFactsPatchResponse` / `mergeGlobalFactPatches` / `batchRenderedItems` / `waitAllOrThrow` / `getGlobalFactsSegmentLimit` 全部 1:1 引入。
> ③ **适配器保不变式（本仓新增）**：`apply_patches_to_fact_items` 把补丁落到本仓的**扁平 `FactItem` 行**上，只替换 `item.value`、**绝不重建对象** —— 重建会冲掉 `source` / `confidence` / `is_simulated` / `chapter` / `is_shared`（与本仓 `/global-facts/adjust` 拒绝让 AI 重写整库同源）。护栏用 AST 锁定「`FactItem(` 只允许出现在 `make_fact_item_from_patch` 一个函数里」。
> ④ **【P0 危大阈值】脚手架 24m 漏判**：`HAZARD_THRESHOLDS["sc_ground"]` 写成严格 `> 24`，而建办质〔2018〕31号 附件一是「搭设高度 **24m 及以上**」——闭区间。**恰好 24m 被判为「非危大」**：前端不红标、导出预检不拦、专家论证漏提示。2026-09-24 已对基坑(3/5)、高大模板(8/18/15/20) 做过同类修正，脚手架是最后一处遗漏；修正后全表 `>` 条件清零（静态护栏锁死）。
> ⑤ **【P1 事实分类】13 个危大参数名未归入九大章节**：`DANGER_PARAM_RULES` 与 `NINE_CHAPTERS[0].category_fields` 实际使用的规范名里，基坑深度 / 支撑高度 / 跨度 / 施工总荷载 / 集中线荷载 / 单件起吊重量 等 13 个未被第一章文本规则覆盖 → 落空串「未分类」→ **九大章节视图不显示、不计入任一章覆盖率**，用户看到「工程概况 0 条事实」却不知数据已提取。已按 `NINE_CHAPTERS[0].category_fields` 口径补齐。
> ⑥ **护栏 90 例 + A/B 反向验证**：新增 `tests/test_global_facts_reference_parity_20260930.py`；逐个还原 4 个修复点 → 分别 **4 / 4 / 6 / 1 例**定向失败，恢复 → **90 passed**。其中后 2 条是本轮写入时**实际发生**的缺陷（分段写入截断函数体），由「改完必跑」当场暴露。
> ⚠️ 5 条遗留项已记录（缺值模式值域两侧各一份、知识库/原方案补丁源未接、finalize 阶段未引入、上下文预算分段未接线、英文归一化键不走中文规则）——**均属需新增数据源 / 新增 AI 调用 / 行为变更，按「新增默认关闭」原则不落地**，详见 §4.16.5。
>
> **最近校准：2026-09-30（第十一轮）** —— **解析提取模块引入 OpenBidKit 易标的「提取域 + 断点续跑 + 均分分段 + 提取内容标准统一出口」**（全部**默认向后兼容**、无新增依赖）：
> ① **提取域（classification domain）**：本仓原有 18 项属「专项方案编制域」(`domain="scheme"`，`bid_analysis_service.ANALYSIS_ITEMS`)，易标的 18 项属「招标响应域」(`domain="bid_response"`，新增 `BID_RESPONSE_ITEMS`，6 必选 + 12 可选)。两域 `item_id` **零交集、业务域完全不同**（本仓的「编制依据/危大六大类/九大章节」不可丢弃），故按**加法引入**而非替换：`EXTRACTION_DOMAINS` 注册表 + `bid_response_domain_enabled`（默认 `False`）。
> ② **主键双格式（零数据迁移）**：`build_item_pk` 为唯一出口 —— scheme 域返回**旧格式** `{project_id}_{item_id}`（历史数据与既有 `WHERE id=?` 查询逐字节不变），其它域 `{project_id}__{domain}__{item_id}`。`bid_analysis_items` 幂等补 `domain` 列（`DEFAULT 'scheme'`，空串视同 scheme）。`_update_item_status` 的 `domain` **留空时按 `item_id` 自动派生** → 6 处调用点零改动。
> ③ **提取内容标准单一出口**：`build_task_prompt()` 唯一注入缺失标注规范 —— JSON 项套 3 条约束（对齐易标 `jsonTask`），Markdown 项追加**整体无结果规则**（对齐 `buildTaskPrompt`）。核心语义严格区分「整项无内容 → `未提取到`」与「局部缺失 → `没有提及`」。字段字典 `get_item_fields` 为 JSON 键结构的唯一事实源（`build_json_template` 由此生成）。
> ④ **技术评分项专判**：`is_missing_technical_score_items()`（对齐易标 `isMissingTechnicalScoreItems`）—— 技术评分**项**是技术方案编制的主要依据，小节缺失时该项整体不算「未提取到」，必须单独判定。
> ⑤ **均分分段策略**（对齐 `userTextSplitter.cjs`）：`400000×0.8` 段上限 → `ceil` 段数 → 尽量均分；断点先搜严格窗口（`0.12×段长`）再放宽（`0.25×段长`）；候选点须过 `_can_use_candidate` 四重约束（段长区间 + **剩余长度 ≥ 剩余段数** 且 `剩余/段数 ≤ max`，防尾段塌缩）；硬切点做 Unicode 代理对保护。**默认关闭**（`bid_analysis_segment_even=False`），旧滑动窗口（16000/500）行为逐字不变。
> ⑥ **断点续跑**（对齐易标 `tasksToRun` 的 `status!=='success'` 过滤）：`AnalysisConfig.skip_done`（默认 `False`）。`force_rerun` / `mode="item"` **恒忽略**；查库异常 **fail-open 为全量执行**（绝不静默跳过重跑）；按 `domain` 隔离。
> ⑦ **并发/重试/预算可配**（原全部硬编码）：`bid_analysis_item_concurrency=2` / `segment_concurrency=3` / `item_retries=2` / `segment_budget=30000` —— 默认值与原硬编码逐字一致，经 `_cfg_int` 单一出口读取。
> ⑧ **下游「提取即消费」跨域**：`format_downstream_context` 改按 `EXTRACTION_DOMAINS` 权威顺序遍历（scheme 在前，与旧版逐字一致），招标响应域的项不再只能靠兜底分支追加。
> ⑨ **A/B 反向验证 6 项全部定向失败**（还原修复点 → 分别 3 / 1 / 3 / 1 / 2 / 2 例失败；恢复 → 全绿）。
> ⑩ **两个本轮实测发现并修掉的缺陷**：(a) `get_task_items` 是**同步**方法却调 `aiosqlite` 的 `db.execute()`（协程）→ `.fetchall()` 抛 `AttributeError` 被 fail-open 吞掉，**断点续跑静默退化为全量执行**；现拆为同步 `get_task_items(done_item_ids)` + 异步 `get_task_items_async(db, project_id)`（路由层唯一入口），并抽出 `fetch_success_item_ids`（R13 `cur is None` + 旧库缺列双重 fail-soft）。(b) `getattr(settings, name, default) or default` 把用户显式配的 **`0` 当成「未配置」**而静默回落到 default；现改为显式 `is None` 判定。
> 护栏：新增 `backend/tests/test_bid_response_domain_20260930.py`（**73 例** = 域注册表 10 + 主键与唯一出口 8 + 内容标准 8 + 技术评分项 7 + 分段策略 12 + 断点续跑 9 + 路由契约 9 + 迁移 5 + 下游消费 5）。实测全量 **3886 passed, 4 skipped, 3 xfailed**（0 failed），前端 **52 文件 / 795 用例**全通过、`tsc --noEmit` 0 错误。详见 §4.15。
>
> **最近校准：2026-09-29（第十轮）** —— **正文生成 100% 全章失败线上事故**收口（全部**默认向后兼容**、无新增配置项）：
> ① **【P0 正文生成 38/38 章全失败 · 返回契约漏改】** `content_utils.auto_fix_unclosed_fences` 的返回契约是 `(fixed_content, fixes_log)`（导出侧 `export.py` 按 `new, log = ...` 解包），而正文生成侧 `sse_handlers._persist_section` 沿用「返回字符串」的旧写法 `content = auto_fix_unclosed_fences(content)` → `content` 变**元组** → 紧接着 `text_word_count(content)` → `strip_fenced_code_blocks` → `content.split("\n")` 抛 `AttributeError: 'tuple' object has no attribute 'split'`。该行在**每一章**的落库路径上，且该函数**无论正文是否含未闭合围栏都恒返回元组** → 全方案每一章 100% 失败。
> ② **【P1 异常被吞、全程零堆栈】** `gen_one` 只捕获 `CancelledError`，其余异常全由 `guarded_gen` 的兜底 `except Exception` 接住，而该分支原先**只发 SSE 事件、不打任何日志** —— 线上表现为终态 failed、失败明细只有一行 `生成异常: ...`，`logs/backend.log` 里**一条堆栈都没有**。现按 ERROR + `exc_info=True` 落盘并带上 `section_id`/标题。
> ③ **【P1 失败文案把本地缺陷说成 AI 故障】** 全章失败消息无条件写死「（AI 服务异常），请检查模型/AI 配置后重试」，而本次病因与 AI 毫无关系 → 排障方向被直接带偏。现按失败原因判定病因：全为未预期异常（reason 以 `生成异常:` 开头）时报「内部处理异常」并指引看 `logs/backend.log`，否则沿用原 AI 口径（消息结构与 200 字上限不变）。
> ④ 护栏：新增 `backend/tests/test_content_fence_contract_20260929.py`（**14 例** = 契约 5 + 调用点 AST/全仓扫描 3 + 端到端落库 3 + 可观测性 3），并做 **A/B 反向验证**：把修复点还原成 `content = auto_fix_unclosed_fences(content)` → **5 例定向失败**（含端到端复现出与线上逐字一致的堆栈）；恢复 → 全绿。复现手法：把运行库 `scheme_assistant.db` 复制到临时目录 + `app.db.DB_PATH` 重定向 + 桩掉 `chat_with_fallback`，即可在**零 AI 调用**下跑完整 SSE 流。详见 §4.14。
> ⚠️ 与前九轮同构的根因：**同一返回契约在 2 处各自使用、改一处漏一处**（`safe_rowcount`、`facts` 门控、`mermaid` 类型默认值都是同一模式）。教训不变 —— **改任何被多处调用的函数签名/返回类型前，先全仓列出调用点**（本轮护栏即按此做成静态扫描）。
>
> **最近校准：2026-09-30（第九轮）** —— **七模块端到端跨模块深审**收口 5 个缺陷（全部**默认向后兼容**、无新增配置项、无新增依赖）：
> ① **【P1 事实注入门控三侧分叉】** canonical 是 `facts_extractor.get_facts_inject_where()` 的四条件 fail-closed（`has_conflict=0 AND is_resolved=1 AND is_simulated=0 AND is_stale=0`）。`placeholder_inventory.py` 却把门控**写死成两条件**、且其 docstring **声称**与 `_render_facts_text` 对齐 —— `is_stale/is_simulated` 事实被计入可注入语料 → `fillable=true` → 章节 `rerunnable` → 用户被告知「重跑可清除占位符」而实际无法清除。现统一调用单一出口并带 fail-closed 兜底。
> ② **【P1 覆盖度台账假绿】** `input_coverage.py` 的 `ok_cnt` 只判两条件 → 报「已调用」，且 stale/simulated 在台账里**完全不可见**。现复用同一出口，并新增**加法式** `filtered_stale` / `filtered_simulated` 两种状态（仅新增、不改既有状态语义）。
> ③ **【P0 活动快照「先截断后过滤」】** `system.py::_build_activity_snapshot` 先 `ORDER BY created_at DESC LIMIT ?` 取最新一批、**再**在内存里过滤 `status='running'`。长时间运行的任务 `created_at` 最早 → 被 LIMIT 挤出 → `ai.in_flight > 0` 而界面显示「后台空闲」。现改为 `WHERE status IN ('running','paused')` 独立查询 + 与 recent 按 id 去重合并。
> ④ **【P0 目录深度上限「移动路径」绕过】** `MAX_OUTLINE_DEPTH=3` 是深度唯一事实源，`create_section` / save-outline / reorganize / `_outline_skeleton` 四条路径都按它裁剪或校验，**唯独 `update_section` 的 parent_id 分支缺这一项**（原只有「不自引用 / 父存在同方案 / 不成环」三项）。把一级章节拖到三级章节下会**静默成功**，随后 renumber 把 `level` 写成 4，而前端目录树按三级渲染 → 该章节已入库却在界面上**彻底消失**。现补同口径校验并把超限文案收敛为单一常量 `_DEPTH_EXCEEDED_MSG`。
> ⑤ **【P1 跨方案误删】** `delete_section` 的级联收集 `WHERE parent_id=?` 不带方案过滤，跨方案脏数据会被收进删除集合 —— `DELETE sections` 虽带方案限定，但下游 `chart_predictions` 清理**无方案限定** → 误删其它方案章节的图表登记（导出 fallback 挂上已消失的图）。`update_section` 环检测同病，一并修。另有 P1-7（套用目录库 `cleared_content_sections` 未透传 → 静默丢正文）与 §5.5（4 处 `getattr(...,"rowcount",0)` 绕过 `safe_rowcount` 且零日志，并修复由此暴露的 `-> bool` 返回契约破坏）一并收口。
> 护栏：新增后端 30 例（`test_fact_gate_parity_20260930.py` 13 + `test_task_activity_projection_20260930.py` 5 + 深度护栏 6 + 跨方案删除 1 + 台账透传 1），并**强化** `safe_rowcount` 静态护栏禁止 `getattr(rowcount)` 绕过；4 项做 A/B 反向验证（还原修复点 → 定向失败；恢复 → 全绿）。全量 **3766 passed, 0 failed**（+30）。详见 §4.13。
> ⚠️ 本轮另有 **2 条被证伪、未予采纳**的「疑似缺陷」（记录以免重复排查）：`outline_quality.check_outline_continuity` 的 `numbering_mismatch` 被判为「恒定死代码」—— 实测对真实点分错位（`id="2.1"` 而位置为 `1`）**能正确触发**，并非死代码，仅是否可达取决于调用点传入的 outline 是否已归一化；该检查只产出 warning 日志、无害，未改动。
>
> **最近校准：2026-09-30（第八轮）** —— **图表 + 提示词双模块**收口 3 个真实分叉（全部**默认向后兼容**、无新增配置项）：

> ① **【P1 幽灵图/限额失效 · 三侧口径分叉】** Mermaid 侧 `detect_mermaid_chart_type` 的 `default` 实参**三处不一致** —— 登记侧（`_chart_pipeline:210`）与 `charts.py`（`:261/:388`）用 `default=""`（认不出 → 整块跳过），导出侧（`content_blocks._parse_content_blocks:449`）却写死 `default="flowchart"`。首关键字不在映射表内的 mermaid 块因而在导出侧被当成流程图**产出 chart 块并占用图号**、在登记侧却被跳过 → 「成稿有图、清单/预检查无此图、且绕过每章 ≤1 与同类型 ≤3 上限」，与紧邻的 chart-json 分支自相矛盾。现收敛为同一 `default` + 同一跳过语义（认不出的块本就过不了 `validate_mermaid`，提前跳过只省一次注定失败的渲染，不改交付结果）。
> ② **【P1 保存期校验漏判】** `validate_prompt_content` ② 分支只读注册表里 SHARED 片段的**旧内容**，而 PATCH 路由是「先校验（`prompts.py:187`）→ 后落库（`:214`）」—— **首次**保存 `SHARED_X = 含 {SHARED_X}` 时读到的是不含自引用的出厂默认 → 放行 → 坏内容入库。现以**待保存的 `text`** 为准判断。
> ③ **【P2 假告警 · 判据收敛】** ③ 分支的契约增删检查直接用 `extract_user_variables` 原始结果，**未套 `_is_false_positive` / 可选区块豁免**，与运行期 `validate_prompt_variables` 不同口径 → 用户**原样保存出厂模板**也会弹「新增了未声明的变量 `max`」（`content_generation_system` 出厂正文含 `$p_{max}$`）。现复用同一判据；反向断言「真实增删仍必报」已入护栏。
> 护栏：`backend/tests/test_chart_prompt_parity_20260930.py`（**30 例**），三类缺陷均做 **A/B 反向验证**（逐个还原修复点 → 分别 9 / 3 / 2 例定向失败；恢复 → 全绿）。
> ⚠️ 本轮另有 **2 条被证伪、未予采纳**的「疑似缺陷」（记录以免重复排查）：`_db_newer_than_cache` 的 WAL 漏检（WAL 下主库文件 mtime **确实**会变，探针实测 `_db_newer_than_cache → True`）；`ai_config` 死参数/`rollbackable` 分叉等 AI 配置项缺口（当前代码已具备代际号 TOCTOU 保护与单一 `prompt_snapshot_is_rollbackable` 判据）。
>
> **最近校准：2026-09-29（第七轮）** —— **全局事实模块 4 个遗留项全部收口**：① `classify_fact_attr` 删掉从未被读取的 `category` 死参数（`inspect.signature` + AST 双护栏）；② `_fact_dimension_fields` 消除 `is_shared` 冗余双写 OR，并把 `source`/`source_ref` 补齐传入 `dimensions_for_row`（旧代码因漏传来源，派生的 `source_kind` 永远退化为默认 `bid_doc`）；③ **「事实变更 → 章节失效标记」由「需单立子项」落地** —— `schemes.facts_updated_at` 单点时间戳 + `_build_tree` 读侧派生 `facts_stale`（写侧仅 1 处、正文写路径有 13 处，读侧派生天然自愈；只提示、绝不静默重写）；④ 全仓 7 处裸 `.rowcount` 收敛到 `app.db.safe_rowcount` 单一出口（`clear_interrupted_items` 的 R13 静默残留、批量审核/审计清理的 500 一并修掉），并加静态护栏禁止新增裸取。新增后端 36 例 + 前端 1 例护栏，全部**默认向后兼容**，详见 §4.12。
>
> **最近校准：2026-09-29（第五轮）** —— **解析提取（import）** + **目录生成（outline）** 双模块深度探查再收口：修复 **3 个 P1**（`title` 为 JSON null 时判据漏判致目录生成整链 `AttributeError` 崩溃 / JSON 失败哨兵 `{}` 被当成「已完成且有效」造成三重假绿 / `create_section` 对 `parent_id` 零校验致悬挂引用 + 手工新增章节无深度上限）+ **3 个 P1/P2**（`create_section` 深度分叉 / 清空目录后扫描缓存未作废 / 提取层只 upsert 不陈旧化致覆盖率虚高），并补齐 `doc_pipeline` 两处 R13 判空漏改点，全部**默认向后兼容**；新增 64 例护栏并做 A/B 反向验证（还原修复点 → 24 例失败；恢复 → 全绿），详见 §4.11。

> **最近校准：2026-09-27（第四轮）** —— **解析提取（import）** + **目录生成（outline）** 双模块深度探查收口：修复 2 个 P1（`strip_outline_numbering` 点分编号贪婪回退把标题剥成残句 / `md_structured` 未知页引用抛 `KeyError('text')` 致四层存储整批丢失）+ 2 个 P2（校验器 `skipped` 契约失真 / 章节树同级排序非确定性），并消除结构变更路径的读+写双 N+1，全部**默认向后兼容**（详见 §4.10）。
>
> **最近校准：2026-09-27（第三轮）** —— **七模块端到端**（上传解析 → 提取项目 → 目录生成 → 全局事实 → 正文生成 → 审核预检 → 导出）跨模块探索收口：修复 **4 个 P0**（正文生成因 5 元组解包 100% 全章失败 / facts SSE 收尾顺序把成功任务改写成 stopped / facts_generation 无 checkpoint 通道 / 收尾 `cur` 未判空）+ **5 个 P1**，全部默认向后兼容（详见 §4.9）。
>
> **最近校准：2026-09-27** —— 图表与 AI 配置模块缺陷修复收口，新增 3 处**三侧口径分叉**的护栏（见下）。
>
> **最近校准：2026-09-27（第二轮）** —— **提示词模块**深度探索收口：修复 2 个 P0（渲染数据丢失 / SHARED 递归打爆调用栈）+ 4 个 P1（并发丢版本、`rollbackable` 口径分叉、保存时零校验、`cur is None` 漏改点）+ 2 个 P2（图表修复提示词 7 类覆盖缺口、前后端错误契约），全部**默认向后兼容**（详见 §4.7）。
>
> **最近校准：2026-09-23** —— 按代码现状逐条复核。变更要点：① 工作流由 7 步合并为 **6 步**（上传解析 / 项目提取 合并）；② AI 保护机制改为**真实数值**（原「10 分钟恢复」等描述与代码不符，已修正）；③ 图表/配图口径更新为**导出时全自动生图**；④ 目录结构按实际模块重写（原 `services/export/` 不存在）。

## 1. 项目定位

- **产品**：建筑工程专项方案 AI 编制平台（"专项方案工具箱"，前端版本 `5.3.0`）
- **架构**：FastAPI 后端（Python 3.12+，SQLite/WAL）+ React 18 + TypeScript + Vite 前端 + 独立 `chart-gap-filler` 图表子项目
- **核心场景（6 步工作流，2026-09-23 起）**：

  `上传解析`（内嵌子 Tab：文档解析 + 项目提取）→ `目录生成` → `全局事实` → `正文生成` → `审核与预检` → `导出文档`

  - 原「上传解析」「提取项目」两个顶层 Tab 已合并为 **1 个顶层 Tab（`import`）+ 2 个内嵌子 Tab**（`docs` / `extract`）；`WorkflowTabKey` 值域固定为 6 个：`import / outline / facts / content / review / export`。
  - 合并方案与落地记录见 `docs/merge_upload_parse_and_bid_analysis_plan.md`（第 13 节）。
- **默认端口**：后端 `8000`，前端 `5175`（Vite，见 `frontend/package.json`）。

## 2. 目录结构

```
backend/app/
├── routers/            # FastAPI 路由（18 个模块 + ai_config/ 子包）
│   ├── projects.py / schemes.py / sections.py     # 项目 → 方案 → 章节
│   ├── sse_handlers.py         # /api/v1/sse：目录生成、正文生成、事实提取（核心大文件）
│   ├── outline_library.py      # /api/v1/outline-library（专项方案目录库）
│   ├── upload_outline.py       # /api/v1/upload-outline（上传目录识别）
│   ├── global_facts.py         # /api/v1/global-facts（全局事实变量）
│   ├── bid_analysis.py         # /api/v1/bid-analysis（18 项结构化提取）
│   ├── doc_pipeline.py         # /api/v1/documents（解析四层存储）
│   ├── compliance.py           # /api/v1/compliance（符合性/专家论证预检/就绪度）
│   ├── consistency_repair.py   # /api/v1/schemes/{id}/consistency（全文一致性 Agent）
│   ├── review.py               # /api/v1/schemes/{id}/review（审核工作流）
│   ├── charts.py               # /api/v1/charts（预测/Mermaid 生成/渲染/修复）
│   ├── export.py               # /api/v1/schemes/{id}/export（DOCX 导出，最大路由）
│   ├── scheme_catalog.py       # /api/v1/scheme-catalog
│   ├── knowledge.py            # /api/v1/knowledge
│   ├── system.py               # /api/v1/system（后台任务状态栏 + 活动 SSE）
│   ├── ai_config/              # /api/v1/ai 子包（config/connectivity/models/usage/audit/scene_routes）
│   └── _chart_pipeline.py      # 非路由：正文内嵌图表扫描 / 校验 / 登记
├── services/
│   ├── ai/                     # AI 调用链：provider_factory、workflows_base（熔断+自适应并发）、
│   │                           #   prompts/（模板注册表+DB 缓存）、mermaid_*.py、image_providers/
│   ├── doc_pipeline/           # 文档解析四层存储管线（pipeline/doc_chunker/doc_storage/md_structured）
│   └── *.py                    # 图表(chart_payload/chart_validators)、事实(facts_*)、
│                               #   一致性(consistency_scanner/repair_*)、正文(content_*)、
│                               #   目录(outline_*)、审核(audit_*/preflight_engine)、解析(file_parser/ocr)
├── utils/              # safe_log_handler（安全轮转）、log_context（trace 关联 ID）
├── db.py               # 连接初始化（WAL/foreign_keys/busy_timeout）+ 幂等 _migrate()
├── schema_sql.py       # 33 张表结构定义
├── middleware.py       # ApiAuthMiddleware（鉴权）
├── models.py / seed_data.py
├── main.py             # 应用入口（另含 /api/v1/health、/diagnostics/capabilities、/prompts*）
└── config.py           # pydantic-settings 配置

frontend/src/
├── pages/              # 7 个页面：ProjectList / ProjectDetail / SchemeWorkbench(最大文件) /
│                       #   OutlineLibrary / AIConfig / PromptEditor / SecuritySettings
├── components/         # 工作台各 Tab 组件（UploadParseTab/BidAnalysisTab/ContentGenerationTab/
│   │                   #   ParseResultCategoryPanel/DocumentPipelineSummary/TaskStatusBar/...）
│   ├── review/         # ReadinessDashboard（就绪度评分）、ReviewWorkflowPanel（章节审核）
│   ├── MarkdownRenderer.tsx / mermaidRuntime.ts
│   └── Layout.tsx / ActivityHint.tsx
├── hooks/              # useContentGeneration（正文 SSE）、useSchemeLiveTask（后台任务轮询）
├── api/index.ts        # axios 客户端 + 19 个按业务域分组的 API 集合 + sseFetch（30s 建连超时）
├── utils/              # 派生纯函数层（workflowDerived/bidAnalysis/contentEvents/...）+ theme/ui
├── types/              # 前后端类型契约（audit.ts、checkpointSchema.ts 自动生成勿手改）
└── tests/              # vitest 单测（33 个文件）

docs/                   # 改造/迁移方案文档（如 merge_upload_parse_and_bid_analysis_plan.md）
平台智能体设计/          # 产品与技术设计文档 + 模块探索报告（只读参考，非代码）
chart-gap-filler/       # 独立图表补全子项目（docchart/ + tests/）
backend/tests/          # pytest 单测（135 个文件）
logs/backend.log        # 后端日志（5MB × 3 轮转，SafeRotatingFileHandler）
start_all.bat           # Windows 一键启动（GBK + CRLF，勿改编码）
stop_all.bat            # Windows 一键关闭 + 清理（ASCII；调 cleanup_guard.ps1 -Mode Shutdown）
cleanup_guard.ps1       # 统一进程检查/清理机制（Check/Clean/Startup/Shutdown；唯一事实源）
cleanup_guard_selftest.ps1  # 守卫回归测试：4 类残留（backend/frontend/stub/pytest）
kill_port.ps1 / kill_zombie_pytest.ps1 / *_selftest.ps1  # 独立清理工具（逻辑已内联进守卫）
start_backend.ps1 / stop_backend.ps1  # 单进程后端起停（历史路径，仍可用）
```

## 3. AI Agent 工作规则

### 3.1 必须遵守

1. **代码注释用中文**：所有新注释、docstring、错误消息使用中文（对齐既有风格）。
2. **不改变业务边界**：图表保持全自动生成，**禁止引入人工配图流程**。
3. **配置项默认向后兼容**：新增配置项默认值必须与既有行为等价（"旧行为不变"），可通过环境变量显式开启新行为。
4. **测试先行**：任何修复必须附带 pytest（backend）或 vitest（frontend）用例，验证「不抛异常 + 语义正确 + 反例回归」。
5. **不新增依赖**：新增库需先说明为什么已有依赖不能解决，并更新 `requirements.txt`。
6. **日志级别规范**：ERROR 用于业务阻断；WARNING 用于可恢复异常；DEBUG/INFO 用于正常追踪。全局日志落盘 `logs/backend.log`。
7. **数据库迁移幂等**：所有 DDL 走 `app/db.py::_migrate()` 的「`PRAGMA table_info` 探测 + 条件 `ALTER TABLE ADD COLUMN`」模式；表不存在时跳过而非报错。
8. **改工作流必须同步 3 处**：`frontend/src/utils/workflowDerived.ts`（`WorkflowTabKey` / `NEXT_TAB` / `PREV_TAB`）、`frontend/src/pages/SchemeWorkbenchPage.tsx`（`tabItems` 与步骤号文案）、`frontend/src/tests/workflowDerived.test.ts` + `workflowTabsGuard.test.ts`。三者必须同时改，否则导航链断裂。
9. **不制造临时产物**：不要往仓库里新增调试脚本/输出（`backend/_*.txt`、`_*.py`、根目录 `_exports/` 等均为历史产物，勿扩充）；调试输出请写系统临时目录。

### 3.2 禁止事项

1. **禁止破坏向后兼容**：不允许删除既有 API 端点或修改既有字段的默认值而不提供迁移路径。
2. **禁止绕过鉴权**：所有 `/api/v1/*` 路由必须经过 `app/middleware.py` 的 `ApiAuthMiddleware`（豁免仅 `/api/v1/health`、`/docs`、`/redoc`、`/openapi.json` 及 `OPTIONS` 预检）。
3. **禁止引入人工图表**：违反既定产品约束（全自动 mermaid + chart-json + 导出时自动配图）。
4. **禁止直接 commit 到 main**：所有 AI 产出必须先自测、后由人审核合并。
5. **禁止编辑/删除 `*.bak-*` / `*.bak-preswap`**：这些是人工"文件切换(swap)"备份，用于在旧实现与新实现之间整体替换；它们可能在任何时刻被替换，属于用户的工作流，不是垃圾文件。
6. **禁止手改 `frontend/src/types/checkpointSchema.ts`**：该文件标注 `AUTO-GENERATED`，源为 `backend/app/services/checkpoint_schema.py`，需通过生成工具重出。

### 3.3 常用命令

```bash
# 一键启动（推荐，Windows；自动清端口/装依赖/探活/开浏览器）
start_all.bat

# 关闭整个软件：后端 + 前端 + 僵尸 pytest + 超时残留 + 孤儿进程 + 临时文件
stop_all.bat

# 统一进程检查/清理机制（start_all.bat pass 4 与 stop_all.bat 内部即调用它）
#   -Mode Check    只检查不杀（排障），退出码 2=有残留
#   -Mode Startup  启动时清理
#   -Mode Shutdown 关闭时清理（含临时文件/锁/pytest .pt_* 残留清扫）
powershell -NoProfile -ExecutionPolicy Bypass -File cleanup_guard.ps1 -Mode Check
powershell -NoProfile -ExecutionPolicy Bypass -File cleanup_guard.ps1 -Mode Startup
powershell -NoProfile -ExecutionPolicy Bypass -File cleanup_guard.ps1 -Mode Shutdown

# 端口清不掉时（uvicorn --reload 孤儿 worker 继承监听句柄）先手动清理再启动
powershell -NoProfile -ExecutionPolicy Bypass -File kill_port.ps1 -Ports 8000,5175
# 改动 kill_port.ps1 / kill_zombie_pytest.ps1 / cleanup_guard.ps1 后必须回归
# （各约 30~60s；cleanup_guard_selftest 需本机可跑 uvicorn）
powershell -NoProfile -ExecutionPolicy Bypass -File kill_port_selftest.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File cleanup_guard_selftest.ps1

# 后端
cd backend
pip install -r requirements.txt
python -m pytest tests/ -q                    # 全量测试
python -m pytest tests/test_xxx.py -v         # 单文件
# ⚠️ 必须带 --reload-exclude：pytest 的 .pytest_cache / __pycache__ / .pt_* 临时目录
#    会触发 watchfiles 重载风暴，且 pytest 进程会占用 logs/backend.log 句柄
#    （2026-09-23 事故：日志冻结 7 小时 + worker 不接管 socket）
python -m uvicorn app.main:app --reload --reload-exclude *.pytest_cache* --reload-exclude *__pycache__* --reload-exclude *.pt_* --host 0.0.0.0 --port 8000

# 前端
cd frontend
npm install
npm run dev                                   # Vite dev server（:5175）
npm run test                                  # vitest run（33 文件 / 524 用例）
npm run build                                 # tsc && vite build

# 图表子项目
cd chart-gap-filler
python -m pytest tests/ -x -q
```

## 4. 关键设计约束

### 4.1 并发体系

- 全局 AI 并发硬上限：`max_concurrency = 5`（不可超过）。实际执行者是 `AdaptiveConcurrencyController`（`services/ai/workflows_base.py`）：`initial=3, min_c=1, max_c=5`，按 429 / 失败率自适应升降，并被夹在 `[max(min_c, target-2), target]` 区间（防"单向棘轮"式永久钉死在 1）。
- 正文生成：`outline_chapter_concurrency = 2`（2026-09-22 O7 降级）
- 一致性扫描：`consistency_scan_concurrency = 2`
- 一致性修复：`consistency_repair_concurrency = 2`
- 配图：`image_max_concurrency = 2`

**为什么降级**：多个 Semaphore 交叉叠加时，同一时刻理论上能到 9 路并发（3+3+3），会打爆 provider 配额（2026-09-17 单日 769 次 429）。降级到 2 是保守默认，可显式恢复到 3。

### 4.2 AI 调用链

```
sse_handlers.collect_json_response
  → provider_factory.chat (并发信号量 + 熔断器 + 配额冷却 + 对冲)
    → ai_config 候选链
      → 具体 provider (agnes / deepseek / zhipu / sensetime / ...)
```

关键保护机制（数值取自 `app/config.py` 与 `services/ai/workflows_base.py`）：

- **熔断器**（`AnalysisCircuitBreaker`）：失败阈值 `failure_threshold=5`，基础冷却 `cooldown_seconds=15`，**429 时冷却加倍（封顶 120s）**；失败计数窗口 `FAILURE_WINDOW=10s`（窗口外重置）。状态机 CLOSED → OPEN → HALF_OPEN → CLOSED；跳过时审计 `action="circuit_skipped"`。
- **配额冷却**（O2）：`ai_fail_cooldown_seconds=120`（429/402/403/404 后该 provider 冷却 120s），冷却期内每 `ai_fail_probe_every_seconds=10` 才放行 1 次探测。
- **重试分级**（O8）：`ai_retry_on_quota_error=False`（配额/认证类确定性错误直接放弃并切 provider）、`ai_retry_on_non_retryable=True`；章节级 `content_section_retries=1`，退避 `content_retry_backoff=6.0`、限流退避 `content_rate_limit_backoff=20.0`。
- **推理模型兜底**（O3）：`ai_reasoning_max_tokens=4096`，`ai_retry_on_thinking_exhausted=True`（`finish_reason=length` 且正文为空 → `max_tokens × 2` 重试一次）。
- **对冲请求**：`ai_hedge_enabled=True`，`ai_hedge_delay_seconds=20` 后启动下一候选，先成功者胜。
- **降级候选链**：`ai_fallback_chain_max=3`，候选单次超时 `ai_fallback_attempt_timeout=120`；候选按实时成功率排序，成功率 < `ai_provider_dead_success_rate=0.20` 剔除、< `ai_provider_demote_success_rate=0.60` 后置。
- **超时**：`ai_request_timeout=900`，正文 `content_request_timeout=300` / 总时长 `content_total_timeout=660`；SSE 总时长默认 `sse_total_timeout_default=1800`、硬上限 `sse_total_timeout_hard_max=14400`。

### 4.3 图表管线

- **7 类图表（唯一值域）**：`flowchart / gantt / architecture / labor / comparison / layout / timeline`。
- **唯一事实来源**：
  - `services/chart_validators.py`：各类型归一器（`normalize_*`）、校验器（`validate_*`）、类型标签、Mermaid 关键字 → 类型映射、`PIL_RENDERABLE_CHART_TYPES`。
  - `services/chart_payload.py`：图表载荷唯一构造器 `build_chart_envelope` + 唯一解析器 `extract_chart_payload`（兼容全部历史形状，**无需数据迁移**）。
- 全自动流程：正文生成时扫描 `mermaid` / `chart-json` / `ai_image` 代码块 → 校验 → 修复 → 登记到 `chart_predictions`（`routers/_chart_pipeline.py`）。
- 数量上限：每章同类型 **1** 个；全方案同类型默认 **3** 个；`ai_image` 放宽到 **6** 个（仅 `enforce_limits=True` 时生效）。
- **图题优先级**：载荷 `title` > Mermaid `title` > 引导语 > 类型通用名。
- **配图（ai_image）已全自动（2026-09-23 v17）**：
  - 前端 **无"生成配图"按钮、无 `/charts/generate-ai-image` 调用**（`chartsApi.generateAiImage` 已删除）；`MarkdownRenderer.tsx` 中的 `ai_image` 占位卡片仅作信息展示。
  - 真实生图发生在**导出 DOCX 时**：`routers/export.py::_auto_generate_ai_image_blocks`，由导出 config `ai_image_auto_generate` 控制（默认 **True**）。
  - 兼容端点 `POST /api/v1/charts/generate-ai-image` 保留，但 `ai_image_manual_enabled=False`（默认）时直接返回 **409**；设 `AI_IMAGE_MANUAL_ENABLED=true` 可恢复人工/脚本触发。
- **禁止**：任何新增"用户上传/插入图片"或"前端触发生图"的路径。

> ✅ **2026-09-27 缺陷修复：chart-json「无 type 字段」的三侧口径分叉（幽灵图）**
>
> 症状：AI 生成的 ```chart-json 块**省略 `type` 键**时（很常见），
> 导出 DOCX 能正常出图，但 `chart_predictions` 里**查无此图** —— 不进图表清单、
> 不进导出预检、用户无法定位/修复，且**绕过「每章 ≤1」「同类型全方案 ≤3」配图上限**
> （上限判定全在登记侧），等于"成稿有图、系统查不到、限额失效"三重错配。
>
> 根因：同一批代码块存在**三个**类型判据，且对"无 type"的处理不一致：
>   | 侧 | 函数 | 原行为 |
>   |---|---|---|
>   | 登记 | `_chart_pipeline._scan_chart_fences_full` | **只认显式 `type`**，缺失即跳过登记 |
>   | 导出 | `content_blocks._parse_content_blocks` | 调 `infer_chart_type_from_payload` **按结构兜底** |
>   | 预览 | 前端 `MarkdownRenderer` | `obj.type \|\| "labor"` —— **凭空捏造** labor 类型 |
>
> 修复（判据收敛到一处，行为向后兼容）：
> - 登记侧改用 `chart_validators.infer_chart_type_from_payload`，与导出侧**同一函数**；
> - `charts.py::_infer_payload_type` + `/charts/render` 对**结构化载荷**按结构纠正
>   `chart_type`（Mermaid 语法载荷与显式合法类型**完全不变**）；
> - 前端不再猜类型：新增 `frontend/src/utils/chartTypes.ts`（前端白名单唯一来源），
>   只取**显式且合法**的类型，其余传空串交后端推断 —— 前端不再复制推断逻辑。
> - 护栏：`tests/test_chart_json_registration_parity_20260927.py`（28 例，锁定登记/导出/预览
>   三侧同口径 + 前后端白名单逐项一致）、`frontend/src/tests/chartTypesParity.test.tsx`（8 例）。
>
> ⚠️ 通用教训：本模块历史上反复出现的"幽灵图/限额失效"类 BUG，**根因都是同一模式** ——
> 同一个业务判据在 2~3 处各自实现，改一处就分叉。**新增判据前先查是否已有唯一实现**，
> 并为跨侧一致性补一条 parity 断言，而不是各侧各写一份。

### 4.4 文档解析四层存储

- 管线：`services/doc_pipeline/pipeline.py` —— `ingest_upload` / `ingest_parse_result` / `sync_extract_layer`（物化到 `doc_extractions`）/ `build_completeness_report` / `compute_freshness` / `run_cross_check` / `detect_cross_source_conflicts` / `purge_document`。
- 层次：原文层 `project_documents` → 解析层 `doc_chunks` → 提取层 `doc_extractions` → 语义层；质量报告在 `doc_validation_reports`。
- **下游全部直接查表**，不经过前端路由：`sse_handlers.build_project_brief` 读 `bid_analysis_items`（`status='success'`）、正文生成逐章注入 `project_brief`（预算 2000 字/章）、`export.py` 校验必填项缺失率、`consistency_scanner.build_global_facts_text` 读 `global_facts`。
- **推论**：改前端 Tab 组织**不涉及**后端契约；反之，改 `bid_analysis_items` / `project_documents` 表结构 = 高风险，需单独立项。

### 4.5 审计日志

- 表：`ai_audit_logs`（provider, model, action, tokens, success, error, **scene**）
- 场景标记 `scene` 是 **`provider_factory.KNOWN_SCENES`** 的键（唯一事实来源，含中文名），
  如 `outline_draft / outline_sublevel / content_draft / content_continue / consistency_repair / facts_extract / chart_fix` 等；
  另有 `ai_config_audit_logs`（**配置**变更审计，见 §4.6 —— 两者不可混用）
- `/api/v1/ai/...` 的 stats / audit-logs 接口按 scene 聚合
- **重要**：任何新增 AI 调用点必须显式传 `scene` 参数（漏传 → 统计归入空场景、且无法被「场景模型路由」单独指定模型）
- 写入为**攒批落库**（满 50 条或 10s flush；应用关闭时 `flush_audit_buffer()`）

### 4.6 AI 配置治理（「文本模型配置」模块）

**存储与密钥**（`ai_config` 表 / `services/crypto.py`）
- 密钥列 `api_key_encrypted` = Fernet(AES-256) 密文；对外只回**末 4 位**（`key_hint`），
  并以 `has_key`（有密文且可解密）/ `key_broken`（有密文解不开）表达状态。
- 密钥来源优先级：`FERNET_KEY` 环境变量 > `data/secret_key.key`（持久化，自动生成）> 兜底临时密钥。
  ⚠️ **`FERNET_KEY` 非法时改用持久化密钥而非换一把随机密钥**（否则库里已存 Key 永久解不开）。
- 密钥回收：`DELETE /api/v1/ai/config/{id}/key`（清空密文；清的是「当前使用」配置时回 `warning`）。

**热更新与缓存**
- 主配置 / 降级链 / 场景路由 / 生效环境 共 4 份缓存，全部由 `invalidate_config_cache()` 失效，
  且**所有写路径都会调用**（保存/删除/切换/降级链/导入/清 Key/场景路由/环境切换）→ 配置改动即时生效，无需重启。
- 缓存写入带**代际号**（`_cache_generation`）：读库期间若发生失效，过期结果**不写回缓存**（防 TOCTOU 钉住一整个 TTL）。
- `AI_CONFIG_CACHE_TTL`（`settings.ai_config_cache_ttl`，默认 300）是唯一 TTL 来源（曾出现「文档写 3600 / 代码硬编码 300」的配置项静默失效）。TTL 只作「外部直接改库」兜底。

**参数校验**
- `provider_factory._RANGE` + `clamp_warnings()` 是数值区间的唯一事实来源（并发 1~5、Max Tokens 256~200000、温度 0~2、超时 10~3600）。
- 前端表单区间必须与之一致；越界值后端会收敛并通过 `warnings[]`（导入为 `clamped_items`）如实回传，**前端必须提示**。

**多环境（可选，默认关闭）**
- `ai_config.env`（空串 = 通用）+ 运行时「当前生效环境」：`ai_runtime_settings.active_env` > `settings.active_env`（`ACTIVE_ENV`）> `''`。
- 环境为空 = **零过滤**（与引入前逐字节一致）；非空时：主配置优先取该环境专用（取不到回落通用）、
  降级链只纳入「通用 + 该环境」、场景路由指向其它环境的配置自动跳过。
- 端点：`GET/PUT /api/v1/ai/env`（写运行时表 + 失效缓存，无需重启）。

**场景模型路由（可选，默认关闭）**
- 表 `ai_scene_routes(scene, config_id)`；选取优先级：调用方显式 `ai_config` > 场景路由配置 > 主配置；表为空 = 行为与旧版一致。
- scene 必须取自 `KNOWN_SCENES`（白名单），且**必须与代码里 `scene="..."` 字面量一致** ——
  双向漂移护栏：`backend/tests/test_ai_config_security_routing.py::TestKnownScenesDrift`。
- 端点：`GET/PUT /api/v1/ai/scene-routes`。

> ✅ **2026-09-25 缺口闭环（三处「配了不生效 / 改了看不见」的静默失效）**
>
> 1. **环境值损坏不再让展示端点 500**：环境标签决定密钥/地址隔离，运行时
>    `resolve_active_env()` 遇非法值必须继续 **fail-closed 抛 `ValueError`**（不可当通用环境用）；
>    但 `GET /ai/config`、`GET /ai/env`、`GET /ai/runtime`、`GET /ai/scene-routes` **不得跟着 500** ——
>    它们正是用户「把环境重置为通用」的恢复入口，改为按通用环境返回数据 + 回传 `env_error`（空串=正常）。
>    `GET /ai/health` 另给独立状态 **`env_corrupt`**，**绝不能混报成 `not_configured`**
>    （否则用户会去重建原本完好的配置）。护栏：`tests/test_ai_config_gapfix_20260925.py::TestEnvCorruptRecovery`。
> 2. **跨环境场景路由必须可见**：`GET /ai/scene-routes` 回传 `active_env`，每项带
>    `config_env` / `env_mismatch`（判定与 `resolve_scene_config` 严格同口径：当前环境非空
>    且配置环境非空且不同 → 运行时跳过并回落主配置）；`PUT` 回传 `warning`
>    （跨环境 / 目标配置没有可用 Key）——**保存成功 ≠ 生效**，必须说清楚。
>    前端显示「跨环境·暂不生效」而不是「已指定」。
> 3. **删配置 / 收密钥不再悄悄改路由**：`DELETE /ai/config/{id}` **级联解除**引用该配置的
>    场景路由并回传 `cleared_scene_routes`（此前残留僵尸行，前端永远显示「配置已删除」红标）；
>    `DELETE /ai/config/{id}/key` 在配置被路由引用时回传 `warning`；导入时 `set_first_active`
>    必须重放 `apply_config_concurrency()`（否则界面上的并发数要等下一次手动保存才生效）。

**调用审计的场景下钻（2026-09-25）**
- `GET /ai/audit-logs` 新增 `scene` 精确筛选 + 回传 `scenes` 可选值（此前 `stats.by_scene` 有聚合、
  明细却只能按供应商筛，看到「某场景失败率高」查不到具体请求）；空白值视为未筛选。
- 配套索引 `idx_ai_audit_scene_created(scene, created_at)`（`_migrate` 幂等创建）：
  既有的 `(action, scene, created_at)` 对 `WHERE scene=?` **用不上**（前导列不在条件里），必须单独建。
- 前端「用量统计 → 按业务场景」标签可点击下钻（自动切 Tab + 回填场景筛选），场景码统一显示中文名，
  历史无埋点的行显示「未标记」而不是空白。

> ✅ **2026-09-27 缺陷修复：运行时厂商开关的「恢复路径自我锁死」**
>
> `GET /ai/runtime` 的 `providers` = `已配置 ∪ 内置预设 ∪ **已禁用**`（注释明写
> "后者可能既非配置也非预设，需能取消勾选"），而 `PUT /ai/runtime/disabled-providers`
> 的白名单此前只取 `已配置 ∪ 内置预设` —— **两侧口径分叉**。
>
> 后果：某厂商被禁用后其配置被删除（或内置预设改名/下架），它就成了
> "界面仍列为可选项、但 PUT 报 400 未知厂商"的状态。该端点是**整体覆盖**语义，
> 前端提交的是当前勾选集合，而孤儿厂商默认处于**勾选**态 —— 于是用户点一次「保存」
> 就必然 400，**整个运行时开关页不可用**，禁用集成为不可逆脏数据（只能手工改库）。
>
> 修复：白名单并入 `resolve_disabled_providers()`，与展示端点**严格同口径**，
> 保证「界面能点 = 后端能存」；同时**不放宽**真正未知的厂商名与非法字符集（仍 400）。
> 护栏：`tests/test_ai_config_20260927_fixes.py`（6 例，含"GET 展示的每个厂商 PUT 都能接受"
> 的通用不变量，已做「还原旧实现后必失败」的反向验证）。


**配置变更审计与版本回滚**
- 表 `ai_config_audit_logs`（action / config_id / detail / **snapshot_json** / client_ip）。
  动作：`create / update / delete / toggle / fallback_chain / import / clear_key / scene_route / env / rollback`。
- `snapshot_json` = `{"before":…, "after":…}` 结构化快照，由 `sanitize_config_snapshot()` 产出，
  **只含字段白名单、绝不含密钥**（密钥仅以 `has_key` 布尔体现）；`GET /ai/config/audit-logs` 另返回 `changes[]`（结构化 diff）与 `rollbackable`。
- `POST /api/v1/ai/config/{id}/rollback {audit_id}` 回滚到某条记录之前：**密钥不参与回滚**、
  数值仍走白名单归一、`is_active` 默认不还原（`include_active=true` 显式开启，且带「零主配置」守卫）、回滚本身也留痕。

### 4.7 提示词治理（✅ 2026-09-24 遗留问题闭环）

**模块与唯一口径**
- 模板注册：`services/ai/prompts/_registry.py`（`_reg()` → `_ALL_PROMPTS`）；
  运行时缓存：`_cache.py`（DB `prompt_templates` 优先、硬编码兜底）。
- **变量口径三函数别混用**：
  - `extract_variables()` —— 原始提取，**含** `{SHARED_*}`（供残留检测）；
  - `extract_user_variables()` —— 排除 `{SHARED_*}`（注册表 `variables` 字段、
    前端列表展示、审计 diff 一律用它；`{SHARED_*}` 由 `_resolve_shared_keys`
    运行时解析，不是调用方入参）；
  - `validate_prompt_variables()` —— 缺失校验，套 `_is_false_positive` 过滤 JSON 示例误报。
- **变量契约**：`PROMPT_VARIABLE_CONTRACTS`（集中声明，勿散落到 `_reg()` 调用处）+
  `check_prompt_variables()` 启动期双向校验（声明↔占位符），漂移打 WARNING。
  新增/修改模板后**必须同步契约表**，否则 `tests/test_prompt_governance.py::TestVariableContracts` 失败。

**版本回滚（G2）**
- `prompt_audit_logs.snapshot_json`（`db._migrate` 幂等补列）存
  `{"before": 正文, "after": 正文}`，由 `settings.prompt_audit_snapshot_enabled`（默认 True）控制；
  超过 `prompt_audit_snapshot_max_chars`（默认 200000）截断并标记 `truncated=True`。
- `POST /api/v1/prompts/{key}/rollback {audit_id}`：无快照的历史行 / 截断快照 / 跨 key → 400/404 拒绝；
  回滚本身也写 `action="rollback"` 审计（可再回滚）。
- `GET /api/v1/prompts/audit-logs` 返回 `changes[]` + `rollbackable`，**不回传原始快照**（减小载荷）。

**上下文预算分配器（G5，默认关闭）**
- `services/prompt_governance.py::allocate_context_budget`：按
  「全局事实 > 目录树 > 资料摘要 > 知识库」优先级水填，下限不超过平均份额。
- 入口 `sse_handlers._apply_prompt_context_budget`，由 `prompt_context_budget`（默认 0）开启。
- ⚠️ 正文 user 上下文的全局事实/知识库**必须**用 `【标签】：` 形式，否则分配器识别不到段
  （回归护栏：`tests/test_prompt_governance.py::TestContextBudget`）。

**提示词注入防护（G6，默认关闭）**
- `prompt_governance.guard_material / scan_prompt_injection / redact_sensitive`，
  入口 `sse_handlers._guard_external_material`，由 `prompt_injection_defense`（默认 False）开启。
- 只加围栏 + 告警，**绝不阻断生成**、**绝不改写资料正文**（与「数据真实性红线」冲突）。
- 脱敏为保守匹配（sk- / gsk_ / api_key= 等），不误伤 GB/JGJ 标准编号。

**缓存新鲜度**
- `_cache._get_prompt_cache` 在「缓存为空 / DB 路径变化 / **库文件 mtime 变新**」三种情况自动重载；
  `reload_prompt_cache()` 会一并重置 `_loaded_db_path` 与 `_loaded_db_mtime`。
  外部进程直写 `prompt_templates` 后读取方无需重启即可看到新值
  （护栏：`tests/test_prompt_cache_external_write.py`）。

> ✅ **2026-09-27 缺陷修复：渲染数据丢失 / SHARED 递归 / 写路径竞态 / 前后端契约**
>
> 1. **【P0 数据丢失】`render_prompt` 整行删行作用在替换后的文本上。**
>    旧实现在变量替换**之后**对结果无条件删「整行就是一个 `{word}`」的行，
>    于是**注入值里**形如 `{heading}` 的行也被删掉。而调用方注入的恰恰是
>    **外部原文**（`facts_extractor` 的 `material`、`json_response` 的
>    `invalid_content`、`charts` 的 `code`、`consistency_scanner` 的
>    `section_content`…）。实测用真实模板 `facts_json_fix_system` 复现：
>    `invalid_content` 里的 `{heading}` 整行消失，AI 拿到的是**被悄悄篡改过
>    的「待修复原文」**，比不修更糟，且与紧邻的残留检测（那段明确用
>    `template` 而非 `result`）自相矛盾。
>    修法：新增 `_render_by_line()` **逐行**渲染 —— 「行」这个概念只作用于
>    **模板自身**，注入出的多行内容原样保留，结构上不可能再被删行逻辑扫到。
>    同时删行与 `_is_optional_block_var` 的豁免**共用** `_OPTIONAL_BLOCK_RE`
>    （此前渲染侧 `[A-Za-z0-9_]*` ≥1 字符、校验侧 `\w{1,}` ≥2 字符，
>    导致单字符 `{x}`「被删行却仍报缺失」）。
>    护栏：`tests/test_prompt_module_fixes_20260927.py`（注入值逐字节保留 +
>    44 个模板无实参渲染逐字节 parity + 删行/豁免双向一致）。
>
> 2. **【P0 可用性】`{SHARED_*}` 互相引用 → `RecursionError`。**
>    旧实现按注释假设「SHARED_* 内容本身不含 SHARED 占位符，无递归风险」——
>    但内容**由用户在提示词编辑器里自由编辑**，假设不成立。实测把
>    `SHARED_OUTPUT_SPEC` 内容改成 `see {SHARED_OUTPUT_SPEC}` 后，
>    `get_prompt('outline_short_system')` 直接抛 `RecursionError`，
>    **所有引用该共享片段的生成任务全部失败**（一次误编辑即阻断整条链路）。
>    修法：`_resolve_shared_keys` 加「解析链环检测 + 深度上限
>    （`_SHARED_MAX_DEPTH=4`）+ 未知 key 明确告警」，任一命中即**保留原字面量**
>    （不崩、也不静默丢内容）。环检测用解析链而非「结果里是否还含 SHARED_」，
>    后者会把「A 引用了拼错的 B」误判成环并**整段丢弃 A 已展开的内容**。
>
> 3. **【P1 竞态】三条写路径读 `before` 在事务外 → 并发丢版本、审计链断裂。**
>    pysqlite 默认 `isolation_level=""` 只在 DML 前隐式 BEGIN，**SELECT 走
>    autocommit**。两个并发 PATCH 同 key 时都读到 `before=X`，审计留下
>    `(X→A)` 与 `(X→B)`；而回滚语义是恢复 `before`，于是两条都只能回到 X，
>    **版本 A 永久丢失**，审计序列与实际生效历史不符。
>    修法：`routers/prompts.py::_write_with_before` 统一为
>    `BEGIN IMMEDIATE` + **事务内重读** before + `updated_at` CAS，
>    冲突返回 **409**（提示刷新）而非静默覆盖。
>
> 4. **【P1 契约】`rollbackable` 与回滚端点前置校验曾是两份实现。**
>    列表侧只判「`before` 非空」，端点侧有三条（非空/未截断/未超长）。
>    分叉后果：被截断的审计行回 `rollbackable=True` → 前端渲染出可点的
>    「回滚」按钮（`PromptEditorPage.tsx` 用它决定 disabled）→ 用户一点必 400。
>    修法：判据收敛到 `audit_service.prompt_snapshot_is_rollbackable()`，
>    并回传 `rollback_blocked_reason` 供前端 tooltip 直接展示 ——
>    **不变量：按钮可点 ⟺ 回滚必成功**。
>
> 5. **【P1 静默失真】保存时零校验 + 展示≠渲染≠hash。**
>    * 新增 `_registry.validate_prompt_content(key, content)` 保存期静态体检：
>      `error` 级（不存在的 `{SHARED_*}`、共享片段自引用）→ **400 拒绝**；
>      `warning` 级（契约变量增删）→ 照常保存但随响应 `warnings` 回传，
>      前端弹 warning。**「保存成功 ≠ 一定正确」必须说清楚。**
>    * 入库、列表展示、运行时缓存加载三处统一经 `clean_prompt_text`，
>      使**「展示 = 渲染 = hash」三者恒等**（此前只有缓存清洗，编辑器展示
>      未清洗原文，`content_hash` 也算的是原文哈希）。
>    * `PROMPT_MAX_CHARS` 收敛到 `audit_service` 单一常量
>      （此前 `routers/prompts.py` 与 `config` 各写一份 `200000`）。
>
> 6. **【P1 健壮性】`db.execute()` 返回 `None` 未判空。**
>    AGENTS.md §5.5 的 R13 事故（2026-09-22）在 `routers/prompts.py` 是
>    **漏改点**（`_chart_pipeline.py` 已加守卫）。命中即 `AttributeError` → 500，
>    且 PATCH 路径下内存注册表不更新、运行时缓存不失效（**内容存了却不生效**）。
>    修法：`_fetch_content_row` / 列表端统一判空（写路径 503、读路径回退出厂清单）。
>
> 7. **【P2 图表】图表修复提示词的 7 类 × 2 格式覆盖矩阵此前无任何断言。**
>    实测 4 组缺专项版回退通用版；其中 `chart_mermaid_fix` 通用版**完全没有提及
>    `timeline`** —— 「时间轴 + Mermaid 修复」链路拿不到任何时间轴结构约束，
>    只能照 flowchart 规则改，必然产出错误结构。已在通用版补齐 timeline 规则，
>    并新增 `tests/test_chart_prompt_coverage_20260927.py`：把
>    `PIL_RENDERABLE_CHART_TYPES`（7 类单一事实源）与「选中提示词是否含该类型
>    口径」绑成 parity 断言。
>
> 8. **【P2 前端】4 处 `catch {}` 丢弃后端 `detail` + 无长度上限提示。**
>    `api/index.ts` 的响应拦截器**已把 `detail` 解析进 `error.message`**
>    （见该文件 :135 注释），但 `PromptEditorPage` 的 4 个 `catch {}` 把它整个
>    丢掉、只显示「保存失败」。结果「引用了不存在的共享片段 / 被他人修改 /
>    内容过长」这三类**用户自己能解决**的问题全部退化成无信息量提示。
>    修法：`promptErrorText()` 统一透传 + `showCount`/`maxLength` 提前拦截
>    + 回滚 tooltip 直接用后端 `rollback_blocked_reason`。
>    另在 `utils/promptVariables.ts` 补 `isOptionalBlockLine` /
>    `extractOptionalBlockVars`，与后端 `_OPTIONAL_BLOCK_RE` 保持同口径。
>
> ⚠️ 本轮 8 条修复的根因高度同构 —— **都是「同一业务判据在 2~3 处各自实现」**
> （删行/豁免/残留三套正则；`rollbackable` 两份判据；清洗只在一处；
> `200000` 两份字面量；图表类型值域散落三处）。与 §4.3 的教训一致：
> **新增判据前先查是否已有唯一实现，并为跨侧一致性补 parity 断言。**

### 4.8 全局事实九大章节分类体系（2026-09-24）

**定位与口径**
- 依据：建办质〔2018〕31号（九大章节）、建办质〔2021〕48号、住建部37号令及地方实施细则。
- 九大章节定义、危大工程六大类分类、阈值判定（`HAZARD_THRESHOLDS`）的唯一单一事实源在
  `services/scheme_classification.py`；`services/facts_classification.py` 只做「全局事实侧」
  映射与派生，两侧口径天然一致，**禁止在另一处重复维护阈值表**。
- 全局事实在既有 22 类 `category` 之上叠加 4 个**正交维度**（不替换 category，前端下拉/
  分组排序/自动分类器口径完全不变）：
  - `chapter`：九大章节归属（overview/basis/plan/technique/safety/personnel/acceptance/
    emergency/calc_drawings，空串=未分类）；
  - `fact_attr`：事实属性（quantitative 定量 / qualitative 定性 / relation 关系 / norm 规范）；
  - `source_kind`：数据来源（bid_doc / drawing / survey / overall_plan / manual）；
  - `is_shared`：跨章节共性事实（多章节复用，避免重复提取；`shared_chapters` 记录复用章节集合）。

**落库与派生**
- `global_facts` 表新增 4 列（`db.py::_migrate` 幂等补列，均有默认值）+ `idx_global_facts_chapter` 索引。
- 提取管线在 `run_post_extract_normalize`（`facts_extractor.py`）内 `apply_fact_dimensions` 自动标注
  （确定性规则、无 AI、无 DB 往返），由 `facts_chapter_classification`（默认 True）控制；设 False 回旧行为。
- **历史行惰性派生兜底**：读路径 `_fact_dimension_fields`（`routers/global_facts.py`）在列值为空时
  原地派生（不写库），历史事实不会因未标注而在章节视图/按章节精选中被丢弃。
- `FactItem.to_db_row` / `persist_extraction` 的 INSERT 列必须与 `schema_sql` 严格一致（尾部 4 列）。

**新端点（`routers/global_facts.py`）**
- `GET /api/v1/global-facts/category-map`：九大章节码-中文名、category→chapter、事实属性/来源枚举（前端单一事实源）。
- `GET /api/v1/global-facts/chapters`：按章节重组事实 + 每章应提取字段/已覆盖/缺失字段差集（字段完整性的唯一出口）。
- `POST /api/v1/global-facts/danger-check`：方案名称关键词 → 六大类危大识别 + 从事实抽取定量参数 →
  危大/超规模阈值判定（只读诊断，不改数据）。

**下游生成接入（默认关闭，向后兼容）**
- 正文生成：`sse_handlers._render_facts_text` 新增 `chapter` 参数做「章节内事实前置、其余作补充」的
  稳定分区（绝不丢事实）；由 `facts_chapter_inject`（默认 False）控制。
- `_load_facts_rows` 多读一列 `chapter`（5 元组，旧 3/4 元组按长度自适应不破坏既有调用方）。

### 4.9 七模块端到端数据链（2026-09-27 第三轮校准）

**链路**：`project_documents` →（四层存储）→ `bid_analysis_items` → `sections`（目录）→ `global_facts` → `sections.content` → `review_records` → `chart_predictions` → DOCX

**新增不变量（均有护栏）**

1. **事实行宽必须长度自适应**：`_load_facts_rows` 自 2026-09-24 返回 **5 元组**
   `(gt, title, content, confidence, chapter)`。任何消费方（`_filter_facts_rows`、
   `_render_facts_text`、`content_standard._fact_text`）**一律按下标取列、整行透传**，
   严禁 `for a, b, c in rows` 式定长解包 —— 实测旧写法对生产行抛
   `ValueError: too many values to unpack`，正文生成**每章 100% 失败**。
   护栏：`tests/test_seven_module_fixes_20260927.py::TestFilterFactsRowsUnpack`。

2. **SSE 收尾顺序恒为「checkpoint → finish_task → yield」**：前端收到 `completed`
   立即 `break` 并关闭响应体，生成器在 `yield` 处收到 `GeneratorExit`，**其后的语句不会执行**。
   若 `finish_task` 写在 `yield` 之后，`finally` 的 `has_active_task` 兜底会把**已成功**的
   任务改写成 `stopped`。目录（`:4201`）/正文（`:5985`）/facts（`:6438`）三处同口径。
   护栏：`::TestFactsSseTerminalOrder`（全文件扫描，任何 `completed` yield 前必须有 finish_task）。

3. **checkpoint 是「锦上添花」，写失败不得改写业务结论**：`_save_*_checkpoint` 必须包
   `try/except` + `logger.warning`。事实已由 `persist_extraction` 落库，checkpoint 写盘失败
   若上抛，会落进外层 `except` → `finish_task(failed)` → 前端 `loadFacts()` 不刷新 ——
   「提取成功」被展示成「提取失败」。

4. ⚠️ **同一 `db` 连接上 SELECT 会读到本事务自己的未提交写入**：要拿「变更前的旧值」
   必须在 `UPDATE` **之前**读。正文收尾把章节置 `review_status='pending'` 并落
   `review_records` 时，若把「读旧状态」放在 `UPDATE` 之后，`_old` 恒为 `"pending"` →
   幂等判断跳过每一章 → **留痕 100% 空转**（本轮首版即踩此坑）。
   护栏：`::TestReviewRecordOnPending::test_old_status_read_precedes_update`。

5. **导出门控与注入门控必须同口径**：`export.collect_export_issues` 的
   `is_resolved` 判定用 `COALESCE(is_resolved,0)=0`（fail-closed，NULL 计未确认），
   与 `facts_extractor.get_facts_inject_where()` 的 `is_resolved=1` 严格相等一致。
   旧实现用 `COALESCE(...,1)` 把 NULL 当「已确认」→ 预检放行、产物缺该事实。

6. **facts 链路已接入心跳统计**：单段 AI 调用期间 `event_stream` 挂起无法 yield
   progress，靠 `with_heartbeat(..., stats_provider=...)` 周期推 `ping` 携带
   `progress`/`elapsed_ms`，前端 `SchemeWorkbenchPage.tsx` 消费该分支。目录/正文/facts
   三条链路现已同口径。

7. **导出门禁 high 类型表前后端 parity**：后端 `_EXPORT_ISSUE_RULE_MAP`（severity=high）
   与前端 `utils/exportCharts.ts::HIGH_EXPORT_ISSUE_TYPES` 必须逐项一致 ——
   `/export/check` 的 issues **不带 severity**，前端靠本地表兜底判定，不一致即门禁被绕过。
   护栏：`::TestExportGateHighSeverityParity`（pytest 侧直读前端源文件比对）。


### 4.10 解析提取 + 目录生成双模块缺陷修复（2026-09-27 第四轮）

> 本轮修复 4 个缺陷 + 2 处性能/测试健壮性优化，全部**默认向后兼容**（无新配置项、
> 无行为开关）。护栏：`backend/tests/test_import_outline_deep_fix_20260927.py`（46 例，
> 含「结构变更批量重规范化」O(1) 查询护栏）；前端 `outlineTreeLogic.test.ts` 同步新增 6 例。

1. **【P1 标题损坏】`strip_outline_numbering` 点分编号贪婪回退。**
   旧正则 `[0-9]+(?:\.[0-9]+){0,7}[分隔符]+` 在点分路径后**没有**分隔符时会回退成
   「短前缀 + 点当分隔符」，把标题剥成残句：
   `"1.2.3（1）细部构造" → "3（1）细部构造"`、`"2.4.1钢筋工程" → "1钢筋工程"`。
   前端 `SchemeWorkbenchPage.tsx::stripOutlineNumbering` 是同源副本，同样会触发。
   修法：用「前瞻捕获最长路径 + 反向引用整条吃满」（`(?=(\d+(?:\.\d+)*))\1`）模拟
   原子组禁止回退；路径后要求分隔符**或** CJK/全角字符边界；单段编号必须带分隔符且
   分隔符后不得仍是数字（`"1.5m 深"` 不再剥坏）；4 位 `年份` 前缀原样保留。
   ⚠️ **规则**：改 `numbering.py::_STRIP_NUMBER_RE` 必须同步改前端
   `LEADING_NUMBER_RE`（两处字符类及短路逻辑逐字符一致）。

2. **【P1 产物丢失】`md_structured` 未知页引用 → `KeyError('text')`。**
   `[IMAGE: xxx, page:99]` 引用本文档不存在的页时，旧实现 `_page_entry(99)` 凭空插入
   无 `text` 的页条目 → 收尾拼装抛 `KeyError` → 四层存储（解析层四份产物 + doc_chunks
   分块）整批落盘失败，且 page_count 被虚增到引用页号。修法：`pnum not in pages` 时
   归位到标记所在页（保留 debug 日志），拼装改 `e.get("text","")` 兜底。

3. **【P2 契约失真】`validate_section_content_numbering` 的 `skipped` 恒 False。**
   docstring 承诺 `content_subheading_renumber=False` 时返回 `skipped=True`，实现从未
   兑现。现按 `_renumber_disabled()` 显式回传；消费方（`validate_scheme_numbering_consistency`
   的漂移清单、repair 判定）对 `skipped` 的语义与「一致」等价，行为不回归。

4. **【P2 编号漂移】章节树同级排序非确定性。**
   `sections.py::_build_tree` 旧实现只排 children 且 key 仅 `sort_order`，根节点顺序
   完全依赖 SQL；等值 `sort_order` 时顺序取决于 SQLite 返回顺序 → 同一份数据两次
   重排可能得到不同编号。修法：统一按 `(sort_order, id)` 全序排序（roots 与 children
   同口径，`id` 主键保证等值组也有全序），SQL 侧补 `, id` 尾排序键。

5. **【性能】结构变更路径的唯一 N+1。**
   `_renormalize_all_section_contents` 在 **5 个结构变更入口**（create/update/delete/
   reorder/save-outline）都会跑，旧实现逐章 `normalize_section_content_subheadings` =
   每章 2 次读 + 1 次写 → 200 章 ≈ 601 次 execute（拖拽排序卡顿主因）。修法：
   读侧复用 `load_scheme_section_index` 预取（与 `validate_scheme_numbering_consistency`
   同一套 O(1) 优化），写侧收集后 `executemany` 一次写完（失败回退逐条，fail-soft）。
   护栏：`TestRenormalizeAllSectionContentsPerf`（10 章与 200 章查询数**相同**）。

6. **【测试】`test_image_parse_through_ocr_chain` 环境相关假失败。**
   旧断言假设「无 tesseract 必有 rapidocr」，两者都未装（也无视觉模型）的机器上
   必然失败。改为按引擎可用性分流：有 rapidocr → 断言产出文本；无任何引擎 →
   断言抛带「OCR」字样与启用方法的 `ParseError`（真实契约），两种环境都确定性通过。

## 4.11 解析提取 + 目录生成双模块缺陷修复（2026-09-29 第五轮）

> 本轮修 6 类缺陷，全部**默认向后兼容**（无新配置项、无行为开关）。
> 护栏：`backend/tests/test_import_outline_fixes_20260929.py`（**64 例**）。
> 已做 **A/B 反向验证**：把 11 个修复点全部临时还原成修复前写法后跑同一份护栏，
> **24 例失败**；还原修复后 **64 例全绿**（每类缺陷都有「修复前必失败」的用例）。

1. **【P1 目录生成整链崩溃】`title` 为 JSON null 时判据漏判。**
   `sse_handlers._sublevel_validate_fn` 与 `_merge_unit_results` 用
   `str(n.get("title", ""))` 判空 —— 该写法只覆盖「键缺失」；弱模型返回
   **键存在但值为 JSON null**（→ Python `None`）时 `str(None)` 得 `"None"`、
   `.strip()` 后仍为真值，`{"title": null}` 被误判为合法标题节点放行，
   随后 `_sub['title'].strip()` 对 `None` 调方法抛
   `AttributeError: 'NoneType' object has no attribute 'strip'`，
   **整次目录生成以「目录生成失败」告终（用户白跑数分钟）**。
   同文件另有 20 处同类模式均写作 `str(x.get("title") or "")`，唯独这两处漏写。
   修法：统一 `str(x or "")`，并让**守卫与取值用同一表达式**（杜绝两处再漂移）；
   `outline_utils` / `outline_reference` / `outline_reorganize` 共 11 处标题取值
   同步收敛（否则 null 标题会以字面量 `"None"` 落入 description 与匹配键）。

2. **【P1 三重假绿】JSON 失败哨兵 `{}` 被当成「已完成且有效」。**
   `bid_analysis_service._json_all_empty` 旧实现
   `if not isinstance(data, dict) or not data: return False`，把空对象判为「不缺失」；
   而 `routers/bid_analysis.py` 在「全部分段无有效结果」时**恰好产出 `{}` 作为
   JSON 项失败哨兵**（`_run_single_item` / `_repair_json` 兜底）。哨兵与判缺失口径
   正面冲突，产生三重假绿：① `/bid-analysis/results` 的 `all_required_done=true`；
   ② `finish_task("completed")`、前端 Tab 徽标变绿；③
   `format_downstream_context` 下发一个**只有 `## 标题`、没有任何键值**的空小节，
   且 `input_coverage` 因命中该锚点反而把此项报成「已覆盖」—— 差集审计暴露不出空洞。
   用户带着完全空白的项目级信息进入目录生成。修法：空对象归入「整体无有效信息」
   （与「所有字段均为没有提及」同义）；解析失败/非对象仍返回 `False`
   （保留既有容错语义，不误伤「格式坏但有正文」）。
   全仓 12 处 `is_missing_result` 调用点语义一致（True = 跳过/判缺失/不计入），无需联动改。

3. **【P1 悬挂引用 + 深度分叉】`create_section` 对 `parent_id` 零校验。**
   只查 level、不校验存在性与方案归属，直接把传入的 `parent_id` 原样入库。
   后果：客户端/脚本传入任意 UUID（或其它方案的 section_id）时
   `sections.parent_id` 出现悬挂引用 —— `_build_tree` 把它当孤儿挂到根
   （用户看到章节「凭空移到顶层」），且**跨方案篡改无任何拦截**；
   而 `update_section` 早有「存在 + 同方案 + 不成环」强校验，两条路径口径分叉。
   另外 `MAX_OUTLINE_DEPTH=3` 是目录深度的唯一事实源、save-outline 按它裁剪，
   唯独手工新增路径无上限 —— 在三级章节下新增会落库四级，
   前端目录树按三级渲染，**该章节已入库却在界面上不可见**。
   修法：补齐同口径校验（创建时 `sid` 尚未生成，无「自引用」可能，故无环检测）
   + `level > MAX_OUTLINE_DEPTH` 直接 400。

4. **【P2 缓存未失效】清空目录后一致性扫描缓存未作废。**
   `_save_outline_to_db` 空分支删章节后漏调 `invalidate_consistency_scan_cache`
   （非空分支、以及 create/update/delete/reorder 五处都有）。
   被删章节的扫描行成为孤儿残留，下次一致性扫描/预检可能命中脏结果，
   把早已不存在的章节报成「仍有冲突」。修法：空分支补齐同口径调用
   （按 `scheme_id` 隔离，不误删其它方案）。

5. **【P2 陈旧数据虚高】提取层物化只 upsert 不陈旧化。**
   `doc_pipeline.pipeline.sync_extract_layer` 只 upsert 有源的提取类别，
   本轮无源的类别既不更新也不清理 → 清空解析项后再物化，`doc_extractions` 里
   旧行仍是 `status='success'` + 旧内容，`GET /documents/{id}/extractions`
   返回「成功」的过期结果、`build_completeness_report` 字段覆盖率虚高；
   同时 `project_documents.extract_status` 被写成 `'pending'`，
   与表内 `success` 行**自相矛盾**。修法：本轮未写入的非预留类别标记
   `status='stale'`（**保留 `extract_data` 便于回溯**，彻底删除仍由
   `purge_document` 负责），并在完整性报告查询里加
   `COALESCE(status,'') != 'stale'` 过滤；预留类别（`boq`）历史行不波及，
   且哨兵过滤后重新提取可恢复为 `success`（非单向劣化）。
   物化侧同时用 `is_missing_result` 过滤失败哨兵，与 §4.11.2 同口径。

6. **【P1 R13 漏改点】`doc_pipeline.py` 两处判空缺失。**
   同文件 `_load_doc` / `get_extractions` / `get_chunks` 已对
   `db.execute()` 返回 `None` 加守卫，`document_completeness` 与
   `project_documents_index` 两处漏改 → 命中即 `AttributeError` → 500
   （前者是「解析质量体检」入口，后者是前端「资料列表」数据源）。
   语义选择均为 **fail-soft**：前者按「无可用缓存」继续实时计算（缓存是加速
   而非唯一来源），后者返回空索引（磁盘索引仍照常合并返回），
   调用方可据此区分「数据库暂不可用」与「项目确无资料」。
   ⚠️ **注意**：`clear_interrupted_items` 的 `cur.rowcount`（bid_analysis.py）
   在 R13 下同样会 `AttributeError`，但被外层 `except Exception` 吞掉 →
   中断项静默残留为 `running`。属全局性问题（见 §5.5），本轮未扩改。
    ✅ **第七轮（2026-09-29）已收口**：全仓 7 处裸 `.rowcount` 统一收敛到
    `app.db.safe_rowcount` 单一出口（含本处，并补 `exc_info=True`），
    另加静态护栏禁止新增裸取，详见 §4.12 第 4 条。

> ⚠️ 本轮 6 类修复的根因同构 —— **又是「同一业务判据在 2~3 处各自实现」**：
> 标题判空（`or ""` vs `, ""`）、缺失判定（哨兵 vs 判缺失口径）、
> 结构变更副作用（缓存作废六处中漏一处）、陈旧语义（upsert 与清理分离）。
> 与 §4.3 / §4.7 / §4.10 的教训一致：**新增判据前先查是否已有唯一实现，
> 并为跨侧一致性补一条 parity 断言。**

### 4.12 全局事实遗留项收口（✅ 2026-09-29 第七轮）

> 上一轮审计列出的 4 个「非阻断遗留项」本轮全部落地，全部**默认向后兼容**。
> 护栏：`backend/tests/test_facts_content_fixes_20260930.py`（**36 例**）
> + 前端 `perfOptimizations0925.test.tsx` 新增 1 例。

1. **【P2 死参数】`classify_fact_attr` 的 `category` 形参从未被读取。**
   声明了 `category: str = ""` 但函数体从不引用 —— 它向后续维护者暗示
   「事实属性依赖 22 类分类」，而属性判定是纯文本正则，与 `category` **正交**。
   修法：直接删参数（3 处调用点同步），护栏用 `inspect.signature` +
   AST `Name` 扫描双重锁定，防止死参数为「对称」被悄悄加回。

2. **【P2 冗余 OR + 隐性分叉】`_fact_dimension_fields` 对 `is_shared` 双写合并。**
   外层 `bool(stored) or dims` 与 `dimensions_for_row` 内层完全等价，看着像
   两套语义。更隐蔽的是它另调一次 `classify_source_kind` —— 因为传给
   `dimensions_for_row` 的 dict **忘了带 `source` / `source_ref`**，派生值只能
   落到默认 `bid_doc`（「地质勘察报告」提取的事实被标成「招标文件解析」）。
   修法：把来源一并传入，判据收敛到 `dimensions_for_row` 唯一实现，
   `chapter` / `fact_attr` / `source_kind` / `is_shared` 四个维度从此只有一份口径。

3. **【P2 → P1 级盲区】「事实变更 → 章节失效标记」由「需单立子项」落地。**
   全局事实变更后，已生成正文仍是生成当时的**事实快照**，导出缓存被清空，
   却**没有任何信号告知用户这一章引用的参数已经过期** —— 用户可能带着过时
   参数交付方案。此前设计文档只把它记为「既定设计，需单立子项」。
   落地方案：**方案级单点时间戳 + 读侧派生**（非布尔列）——
   - 写侧：`invalidate_export_cache(db, sid, facts_touched=True)` 推进
     `schemes.facts_updated_at`。事实写路径**只有 1 处**，而正文写路径有 13 处
     —— 若给 `sections` 加布尔列就要改 13 处，「漏改一处」就会让重生后的章节
     被永久标成过时（本仓反复踩的同类陷阱）；
   - 读侧：`sections._build_tree` 用 `LEFT JOIN schemes` +
     `CASE WHEN datetime(sch.facts_updated_at) <= datetime(sec.updated_at)` 派生
     `facts_stale`。**天然自愈**：任何一次正文重写都会推进 `updated_at`，
     标记自动消失，无需任何清理代码；
   - **只提示、绝不静默重写**用户已编辑的正文（既定设计红线）。
   ⚠️ **时间字符串格式并不统一**：Python `datetime.now().isoformat()` 用 `'T'`
   + 微秒，SQLite `datetime('now','localtime')` 用空格 —— 字符串直比会得出
   **相反**结论（`'T'` 0x54 > `' '` 0x20），必须统一交给 SQLite `datetime()`
   归一化。判定链四道 fail-soft（无正文 / 无时间戳 / 无法解析 / 已含最新事实
   → 一律判 0），历史方案无时间戳时不打扰用户。
   前端配套：`TreeNode.facts_stale` + 目录树「事实已变更」warning Tag；
   并**必须**纳入 `treeFingerprint` —— 否则 3s 轻量轮询因指纹相同保留旧树，
   徽标要等下一次完整 `load()` 才出现（「后端已标、界面没显示」）。
   护栏含「跨格式时间归一化」与「轮询瘦身仍带回标记」两条针对性断言。

4. **【P1 R13 漏改点】全仓 7 处 `.rowcount` 裸取，统一收敛到 `app.db.safe_rowcount`。**
   单连接 + aiosqlite 下 `execute()` 可能返回 `None`，命中后分两种结局：
   外层有 `except Exception` → 异常被吞、**写操作没生效却零日志**
   （`clear_interrupted_items` 即典型：中断遗留的 running 项静默残留，
   UI 永远「运行中」、18 项永久判缺失）；外层无守卫 → 直接 500
   （批量审核状态、AI 审计日志清理）。现全部走 `safe_rowcount(cur, what=...)`：
   `None` 时返回 0 并打带操作名的 WARNING；负数 rowcount 归一为 0，
   避免 `if affected > 0` 类守卫被 `-1` 意外触发。
   新增**静态护栏** `TestRowcountIsCentralized`：遍历 `backend/app/**/*.py`，
   任何 `.rowcount` 出现在 `app/db.py` 之外即失败 —— 让这条判据不可能再分叉。
   `clear_interrupted_items` 的告警同时补 `exc_info=True`（连接损坏 vs SQL
   语法错误 vs 锁竞争的处置完全不同，只打 `%s` 会丢堆栈）。

> ⚠️ 本轮 4 类修复的根因仍是同一条 —— **「同一业务判据在 2~3 处各自实现」**：
> 维度派生（router 自写 OR / service 唯一实现）、
> 行数口径（7 处各自裸取 rowcount）、失效语义（1 处写时间戳 vs 13 处写布尔列）。
> 与前六轮一致：**新增判据前先查是否已有唯一实现，
> 并为跨侧一致性补一条 parity 断言**。

### 4.13 七模块端到端深审查收口（✅ 2026-09-30 第九轮）

> 本轮为「上传解析 → 提取项目 → 目录生成 → 全局事实 → 正文生成 → 审核预检 → 导出」
> 七模块的跨模块深审。护栏：`tests/test_fact_gate_parity_20260930.py`（13 例）、
> `tests/test_task_activity_projection_20260930.py`（5 例）、
> `tests/test_import_outline_fixes_20260929.py::TestMoveSectionDepthGuard`（6 例）、
> `tests/test_sections.py::TestDeleteSection::test_delete_does_not_cross_scheme_on_dirty_parent`
> （1 例）、`tests/test_outline_fixes.py::TestOutlineLibrary`（+1 例）。
> 全部**默认向后兼容**（无新配置项、无新依赖、无响应契约破坏）。

1. **【P1 事实注入门控三侧分叉】** 唯一口径是
   `facts_extractor.get_facts_inject_where()` 的四条件 fail-closed：
   `has_conflict=0 AND is_resolved=1 AND is_simulated=0 AND is_stale=0`
   （`export.py` 已同口径）。`placeholder_inventory.py` 把门控**写死成两条件**，
   而它的 docstring 声称与 `_render_facts_text` 对齐 —— 于是 `is_stale` /
   `is_simulated` 事实被计入可注入语料 → `fillable=true` → 章节判为
   `rerunnable` → 用户被告知「重跑可清除占位符」，实际重跑不会清除。
   `is_stale=1 AND is_resolved=1 AND has_conflict=0` 是**可达状态**
   （`_mark_project_facts_stale` 批量置 stale 时不清 `is_resolved`），故不是理论分叉。
   修法：两处均改为调用单一出口 + fail-closed 兜底。

2. **【P1 覆盖度台账假绿】** `input_coverage.py` 的 `ok_cnt` 只判两条件 →
   报「已调用」，且 stale/simulated 在台账里**完全不可见**。现复用同一出口，
   并新增**加法式**状态 `filtered_stale` / `filtered_simulated`
   （已在 `FieldEntry` docstring 记录；仅新增，不改既有状态语义）。

3. **【P0 活动快照「先截断后过滤」】** `_build_activity_snapshot` 旧实现先
   `ORDER BY created_at DESC LIMIT ?` 取最新一批、**再**在内存过滤 running ——
   长运行任务 `created_at` 最早、被 LIMIT 挤出，于是 `ai.in_flight > 0` 而界面
   显示「后台空闲」。修法：running 走独立
   `WHERE status IN ('running','paused') ORDER BY created_at DESC LIMIT _RUNNING_MAX`
   （新常量 `_RUNNING_MAX = 50`，与 `limit` 解耦），与 recent 按 id 去重合并；
   返回 `tasks = {running, recent}`，`recent = tasks[:limit]`。
   ⚠️ **前端契约不变**：`TaskStatusBar.tsx` 消费 `tasks.recent` 并在本地过滤终态，
   故无需前端改动。

4. **【P0 目录深度上限「移动路径」绕过】** 详见顶部校准说明。要点：
   - 文案收敛为单一常量 `_DEPTH_EXCEEDED_MSG`，create / update 两条路径共用；
   - 校验**必须放在写入 + commit + renumber 之前**，否则结构变更事务已提交无法回滚；
   - 静态护栏 `inspect.getsource(update_section).count("_DEPTH_EXCEEDED_MSG") >= 1`
     防止该段被整段删除；
   - 前端 `SchemeWorkbenchPage.tsx` 已有拖拽深度拦截，本修复是 API/脚本路径的
     **纵深防御**，不改变 UI 行为。

5. **【P1 跨方案误删 / 误判成环】** `delete_section` 级联收集与
   `update_section` 环检测的 `WHERE parent_id=?` 均缺 `scheme_id` 限定。
   ⚠️ **易误判点**：单测只断言 sections 行数时**测不出**这个缺陷 ——
   因为 `DELETE FROM sections` 本身带方案限定，b1 行本身不会被删。
   真正越界的是 `all_ids` 里的跨方案 id 流向**下游无方案限定**的
   `DELETE FROM chart_predictions WHERE section_id IN (...)`。
   因此护栏必须同时断言 `chart_predictions` 存活（A/B 已验证：
   还原旧实现后该断言定向失败 `0 == 1`）。

6. **【P1 静默丢正文】** `apply_library_and_save` 手工组装返回体，丢弃了
   `_save_outline_to_db` 已算好的 `cleared_content_sections` / `roots_locked`。
   套用目录库必然整表重建（库节点 id 是编号而非主键，无法匹配旧章节），
   旧正文会被级联删除 —— 前端无从提示。现透传这两个字段
   （与 `upload_outline.py` 的 apply 路径同口径）。

7. **【P1 R13 绕过 + 返回契约破坏】** §4.12 收口 7 处裸 `.rowcount` 后，
   全仓仍有 4 处 `int(getattr(cur, "rowcount", 0) or 0)` —— 它**不抛**
   `AttributeError`，但 `execute()` 返回 `None` 时**静默返回 0、零日志**，
   正是 `safe_rowcount` 要消除的形态（`global_facts.py` ×2、
   `task_registry.py` ×2）。静态护栏新增 `getattr(..., "rowcount")` 正则扫描。
   ⚠️ **改这 4 处时暴露的坑**：`safe_rowcount` 返回 `int`，而
   `_reconcile_parse_status` 的签名与 docstring 承诺 `-> bool`
   （调用方用 truthiness、测试用 `is False`）。直接返回 int 会让
   `0 is False` 恒为 False → 契约与断言双双失真。现包 `bool(...)`。
   **通用教训：收敛到单一 helper 时，必须核对原返回类型契约，而非只看 truthiness。**

8. **遗留项（记录以免重复排查，本轮未采纳）**
   - **P0-1 上传目录幽灵匹配**：`upload_outline.save_as_outline` 用「归一化标题 +
     FIFO 队列」匹配章节，与 `_save_outline_to_db` 的 `__original_id` 判据分叉，
     同父不同名/同名不同父会错挂或级联删正文。**当前 UI 不可达**（前端
     `api/index.ts` 已移除 `saveAsOutline` 调用，走 `/sections/save-outline`），
     但端点仍存活，脚本/旧版前端可触发。修复成本约 180 行（复用
     `_save_outline_to_db`），建议单立子项。
   - **P0-3 结构变更跨两事务**：`update_section` 先 commit（结构），再做
     renumber + 规范化（第二个事务），中途异常会导致「新 parent 已提交、
     `outline_json.id` 仍是旧编号」的**不可自愈**错位。建议按
     `_save_outline_to_db` 的单事务边界改写，需单立子项。
   - **P1-6 `facts_stale` 被结构变更误清**：判定用 `sections.updated_at`，
     而它同时承载「正文写入时间」与「任意字段更新时间」两种语义 ——
     拖拽/改名/移动都会推进 `updated_at`，「事实已变更、正文过时」标记被静默清掉。
     正确修法是新增 `content_updated_at` 列（`_migrate` 幂等补列）并改读侧
     `COALESCE(content_updated_at, updated_at)`，但需改 **11 处 content 写路径**
     （`sections.py` ×3、`sse_handlers.py`、`charts.py` ×2、`consistency_repair.py`、
     `repair_agent.py`、`repair_record.py`、`numbering.py` 等），漏一处即回归。
     属「单立子项」级别，本轮记录不落地。
   - **P1-1 / P1-2 竞态守卫**：`outline_generation_in_progress` 只查进程内
     `_tasks`，进程重启 / 多 worker 后完全失效；SSE 目录生成入口自身无同方案互斥。
   - **P1-5 / P1-8 / P1-9**：`_save_outline_to_db` 整表重建不重置 `review_status`；
     `_restore_descriptions` 用「位置路径」做键（修复轮增删章节后大面积失效）；
     `_outline_skeleton` 预算记账空转泄漏（取样不均衡）。

> ⚠️ 本轮 8 类发现的根因仍是同一条 —— **「同一业务判据在 2~3 处各自实现」**：
> 事实门控（canonical 4 条件 / 台账 2 条件）、深度上限（4 条路径有 / 1 条没有）、
> 方案隔离（父节点校验有 / 级联遍历没有）、行数口径（`safe_rowcount` /
> `getattr(rowcount)`）、正文丢失量化（`upload_outline` 有 / `outline_library` 没有）。
> 与前八轮一致：**新增判据前先查是否已有唯一实现，并为跨侧一致性补一条 parity 断言。**

### 4.14 正文生成 100% 全章失败事故收口（✅ 2026-09-29 第十轮）

> 现象：任务列表里正文生成任务终态 `failed`，消息为
> 「全部 38 章生成失败（AI 服务异常），请检查模型/AI 配置后重试：生成异常:
> `'tuple' object has no attribute 'split'`」；`logs/backend.log` 中该时段**无任何
> 相关记录**（既无 AI 调用日志，也无异常堆栈）。

1. **【P0 根因】`auto_fix_unclosed_fences` 返回契约漏改 → 每章必失败。**
   `services/content_utils.py::auto_fix_unclosed_fences` 返回
   `(fixed_content, fixes_log)`（导出侧 `export.py` 的「导出前置自动补齐围栏」
   开关按 `new, log = auto_fix_unclosed_fences(raw)` 解包，行为正确）。
   `sse_handlers._persist_section` 却是：
   ```python
   content = auto_fix_unclosed_fences(content)   # content 变成 (str, list)
   ```
   紧接着的 `wc = text_word_count(content)` →
   `strip_fenced_code_blocks` → `content.split("\n")` 即抛
   `AttributeError: 'tuple' object has no attribute 'split'`。
   ⚠️ **该函数无论正文是否含未闭合围栏都恒返回元组**（无未闭合时返回
   `(content, [])`），所以缺陷与 AI 输出内容无关 —— **每一章、每一次正文生成
   都 100% 失败**，且必然发生在「清洗 → 落库」这段公共路径上。
   现按导出侧同口径解包，并顺带把补齐条数落成一条 INFO（原先连"补了几处"都看不到）。

2. **【P1】未预期异常被吞掉，全程零堆栈。**
   `gen_one` 只捕获 `asyncio.CancelledError`（用户停止语义），其余异常一律冒到
   `guarded_gen` 的兜底 `except Exception`；而该分支原先**只 `event_queue.put`
   一条 `section_error`、不打日志**。于是「38 章同因失败」在日志里完全隐形，
   只能看到任务列表里一行 `生成异常: ...`。现补
   `logger.error(..., exc_info=True)`（业务阻断级 → ERROR），带 `section_id` + 标题。

3. **【P1】失败文案把本地缺陷播报成「AI 服务异常」。**
   `_all_msg` 原先无条件写死「（AI 服务异常），请检查模型/AI 配置后重试」。
   本次病因与 AI 毫无关系，用户按提示去改模型/密钥只会绕圈。现按失败原因判定：
   全部为未预期异常（`reason` 以 `生成异常:` 开头）→ 「（内部处理异常），请查看
   后端日志 logs/backend.log 定位后重试」；否则沿用原 AI 口径。
   **消息结构与 200 字截断上限均未变**，前端 `contentEvents` 解析口径不受影响。

4. **复现与验证手法（可复用）。**
   把运行库 `scheme_assistant.db`（连 `-wal/-shm`）复制到临时目录 →
   `app.db.DB_PATH` 重定向 → 桩掉 `sh.chat_with_fallback` / `sh.collect_json_response`
   → `await sh.generate_content(sid, FakeRequest(body), db=conn)` 后遍历
   `resp.body_iterator`。**零真实 AI 调用、零生产库写入**，即可在数秒内复现
   「38/38 章全失败」并拿到完整堆栈（本次即用此法定位到 `text_word_count`）。
   ⚠️ 该手法比「读日志猜」高效得多，且能顺带验证修复（修复后同一脚本跑出
   `completed`、`done=38/38`、`failed_count=0`）。

> ⚠️ 本轮根因与前九轮同构：**同一个返回契约在 2 处各自使用、改一处漏一处**
> （与 `safe_rowcount`、`_FACTS_INJECT_WHERE`、mermaid 类型默认值同类）。
> 通用教训：**改动任何被多处调用的函数的签名或返回类型前，先全仓列出调用点**，
> 并把「调用点必须按新契约使用」做成静态扫描护栏 ——
> `test_content_fence_contract_20260929.py::test_whole_app_has_no_bare_assignment`
> 即按此实现（AST 扫描 `app/**`，任何「把返回值整体赋给单个变量」的写法都失败）。

### 4.15 解析提取模块引入易标「提取域 + 断点续跑 + 均分分段」（✅ 2026-09-30 第十一轮）

> 目标：把参考软件（`E:\编程\OpenBidKit 易标\...`）解析提取模块的**工作流程、分类
> 体系、内容标准、解析方案、AI 调用口径**引入本仓，并按本仓业务增强。全部
> **默认向后兼容**（无新增依赖、无响应契约破坏、历史数据零迁移）。

#### 4.15.1 参考软件能力清单（只读借鉴，非运行依赖）

| 能力 | 参考实现位置 | 本仓引入情况 |
|---|---|---|
| 18 项分类（key/full/custom 三态归一） | `client/src/features/technical-plan/services/bidAnalysisWorkflow.ts:44-314`、`bidAnalysisTask.cjs:168-183` | ✅ 加法引入为 `BID_RESPONSE_ITEMS`（6 必选） |
| JSON 统一模板 3 条约束 | `bidAnalysisWorkflow.ts:28-42`（`jsonTask`） | ✅ `_JSON_TASK_TEMPLATE` |
| Markdown「整体无结果规则」 | `bidAnalysisTask.cjs:190-196` | ✅ `MARKDOWN_MISSING_RULE_SUFFIX` |
| 技术评分**项** vs **要求** 语义二分 | `bidAnalysisTask.cjs:82-108` | ✅ 分类已引入；语义二分落在 `TECH_SCORE_ITEMS_HEADING` 锚点 + `is_missing_technical_score_items` |
| 无效标与废标项四象限 | `bidAnalysisTask.cjs:36-69` | ✅ `discardedBids` 分类 + `build_task_prompt` 统一出口 |
| 断点续跑（跳过 success） | `bidAnalysisTask.cjs:370` | ✅ `AnalysisConfig.skip_done` + `fetch_success_item_ids` |
| 预热项先行 + 5000ms | `bidAnalysisTask.cjs:437-450` | ✅ 本仓**早已有**（`projectBasicInfo` 预热） |
| 均分分段 + radius 窗口 + 代理对保护 | `client/electron/utils/userTextSplitter.cjs:1-211` | ✅ `_split_even`（默认关闭） |
| 5 组边界优先级 + 围栏保护 | 同上 `:8-14` / `:33-61` | ✅ 本仓**早已有**（`bid_analysis_service._BOUNDARY_GROUPS` / `_collect_fence_ranges`） |
| 分段结果 AI 合并 | `segmentedAiResultMerger.cjs:27-45` | ⚠️ 本仓 `SEGMENT_MERGE_PROMPT` 已对齐语义，独立模块化列为遗留项 |
| 日志保留最近 80 条 | `bidAnalysisTask.cjs:3-6` | ⚠️ 列为遗留项（未实施） |
| 多标段正则预检测（16 条 + 中文数字 + 【N】/【N-M】） | `bidSectionDetector.cjs:45-156` | ✅ 本仓**早已有** |
| 按行号 `L000001 | 原文` 提取 includeRanges + AI 合并去重 + ≥2 段校验 | `bidSectionExtractionTask.cjs:8-266` | ✅ 本仓**早已有** |
| sectionHint 作独立第二条 system 消息 | `bidAnalysisTask.cjs:202-211` | ⚠️ 本仓 `build_system_prompt` 仍是**拼进同一条**（见遗留项） |
| 魔数优先的格式识别（PDF/ZIP/OLE） | `doc2markdown/convert.mjs:134-166` | ⚠️ 本仓 `file_parser.py` + MinerU/OCR/旧版 Office 链路**更强**，**不反向迁移** |
| 三层嵌套重试（理论 27 次请求） | `aiRetry.cjs` × `aiRequestQueue` × `collectJsonResponse` | ❌ **明确不引入**：与本仓熔断器/配额冷却/降级链（§4.2）冲突，照搬会 27× 放大 |

#### 4.15.2 提取域（唯一事实源与向后兼容边界）

```
EXTRACTION_DOMAINS = {"scheme": ANALYSIS_ITEMS, "bid_response": BID_RESPONSE_ITEMS}
```

| 域 | 项数 | 必选 | 分组 | 开关 | 主键格式 |
|---|---|---|---|---|---|
| `scheme`（专项方案编制域） | 18 | 17 | 13 | 恒启用 | `{project_id}_{item_id}`（**旧格式，不变**） |
| `bid_response`（招标响应域） | 18 | 6 | 11 | `bid_response_domain_enabled`（默认 `False`） | `{project_id}__{domain}__{item_id}` |

**关键出口（改动前先全仓列调用点）**：
- `get_item_domain(item_id)` — 域归属唯一事实源；未知 id 返回**空串**（fail-closed，不默认 scheme）
- `build_item_pk` / `parse_item_pk` — 主键格式唯一出口；域名空串/None **fail-closed 到 scheme**
- `get_item_fields` / `build_json_template` — 字段字典唯一出口
- `build_task_prompt` — 缺失标注规范唯一出口
- `_split_tender_text` / `_cfg_int` — 分段策略 / 配置读取唯一出口
- `fetch_success_item_ids` — 断点续跑查询唯一出口（双 fail-soft）

⚠️ `_update_item_status` 的 `domain` 参数**留空即按 `item_id` 自动派生**，因此
6 处调用点（两个执行循环各 3 处）**零改动**即可正确落域 —— 避免「漏传一处」
（本仓 §4.3/§4.7/§4.13/§4.14 反复踩的同构陷阱）。护栏
`TestNoBarePkConstructionInRouter` 用 AST 扫描禁止路由层再出现裸主键拼接。

#### 4.15.3 缺失标注规范（三态，严格区分）

| 语义 | 取值 | 判定入口 |
|---|---|---|
| 整项无内容 | `未提取到`（`MARKDOWN_MISSING_RESULT`） | `is_missing_result(content, "markdown")` |
| 局部字段/小节缺失 | `没有提及`（`PARTIAL_MISSING_TEXT`） | `is_missing_technical_score_items` / JSON 字段级 |
| JSON 项整体无有效信息 | `{}` 或全字段空值 | `_json_all_empty`（含 `{}` 失败哨兵） |

⚠️ 前两者**逐字不同**是契约的一部分，混用会让 `is_missing_result` 无法区分
「真缺失」与「部分缺失」。护栏 `test_partial_vs_full_missing_are_distinct` 锁定。

#### 4.15.4 路由契约（加法式，旧调用点零改动）

| 端点 | 变更 | 兼容性 |
|---|---|---|
| `GET /items?domain=` | 新增 `domain`（默认 `scheme`）与返回体的 `domain` / `domain_unknown` 两个**加法式**键 | scheme 域返回体与旧版逐字一致（`items`/`groups` 仍走 `get_all_items()`/`get_groups()`） |
| `GET /items/{item_id}?domain=` | 返回体加 `domain` / `fields`；传 `domain` 时校验归属，跨域 404 | 旧字段全部保留 |
| `GET /domains` | **新增**端点，返回两域 + `enabled` + 项数/必选数 | 前端据此决定是否展示域切换器；**永不 404** |
| `POST /start`、`GET /start-sse` | 新增 `domain`、`skip_done` | 省略时 `domain="scheme"`、`skip_done=False`，行为逐字不变 |

⚠️ 未知域返回**空清单 + `domain_unknown=true`**（fail-closed，不静默回退）；
未启用的域返回 **404**（而非返回一套无法执行的清单）。

#### 4.15.5 遗留项（**已全部收口，详见 §4.19**）

- **P1 `section_hint` 仍是拼进同一条 system 消息**：易标是**独立第二条 system
  消息**（`buildTenderContextMessages`）。本仓 `build_system_prompt` 的 docstring
  声称「作为独立 system 消息注入」（`bid_analysis.py` 注释同样如此），**与实现不符**。
  ✅ **第十五轮已收口**（§4.19.2）—— 新增 `build_system_messages`，
  两处调用点改走它；`build_system_prompt` 保留不动（历史契约）。
- **P1 `format_downstream_context` 无预算上限**：两域共 36 项全量下发，无字数截断
  与优先级裁剪（对比 `facts` 链路的 2000 字/章预算）。
  ✅ **第十五轮已收口**（§4.19.3）—— `_apply_downstream_budget` 两级裁剪。
- **P2 生成链不读 `parse_status` / `parse_truncated`**：`global_facts.load_parsed_docs`
  与 `sse_handlers` 的 3 处 `parsed_markdown` 读取点均不带这两列 → 截断文档静默进入
  目录/正文/事实生成（唯一消费方是 `bid_analysis._list_parsed_documents`）。
  ✅ **第十四轮已收口**（§4.18.2）—— `generate_facts` 改走
  `sse_handlers._load_facts_source_docs`（三层降级），截断清单以 SSE `warning`
  事件显式告知。
  ✅ **第十五轮已收口剩余部分**（§4.19.4）—— 目录 / 正文侧的
  `load_parsed_docs` 同样改为三层降级并恒记 WARNING。
- **P2 提取项重跑级联失效不全**：`_invalidate_downstream_cache` 只清 `export_cache`，
  `consistency_scan_cache` / `doc_extractions` / `schemes.facts_updated_at` 均不动。
  ✅ **第十五轮已收口**（§4.19.4）—— 新增 `_invalidate_extraction_derived` 补齐三处。
- **P2 提取层物化仍只覆盖 5/18 项**：`doc_pipeline.pipeline._ITEM_TO_EXTRACT_TYPE`
  只映射 5 个 item_id。
  ❌ **第十五轮判定为非缺陷**（§4.19.5）——`boq` 是 RESERVED、`global_facts` 走
  `sync_extract_layer` **独立分支**特供，其余 5 个非预留类别**已 100% 覆盖**；
  要物化其余 13 项需先新增提取类别（产品决策 + schema 语义变更）。
- **P2 资料集合三链不一致**：目录 `limit=5`、正文 `limit=3`、事实提取**无 LIMIT**。
  ❌ **第十五轮判定为非缺陷**（§4.19.5）——三者按下游职责分别设定：事实提取要
  **全量**依据（少一份就可能漏关键参数），目录/正文只取摘要片段（超预算反而
  稀释指令），4000 字截断才是真正的护栏。
- **P1 日志无上限裁剪**：易标 `pushLog` 保留最近 80 条，本仓 SSE 事件与
  `task_registry` 日志无裁剪。18 项 × 多段时日志可膨胀。
  ❌ **第十五轮判定为无需改** —— 本仓任务日志走 `task_registry` 固定结构 + DB 持久化，
  不在进程内存里无限累积；且日志是排障依据，裁剪会引入「日志被静默丢弃」的新问题。
- **P2 36 项提示词未注册进 prompt registry**：`bid_analysis_service._ITEM_PROMPTS`
  硬编码，无版本/审计/回滚（易标同样硬编码，本仓 prompt 模块能力反而更强）。
  ❌ **不引入** —— 参考软件同样硬编码，按「与参考软件对齐」原则不额外改造。

> ⚠️ 本轮 8 类发现的根因仍是同一条 —— **「同一业务判据在 2~3 处各自实现」**：
> 主键格式（`build_item_pk` vs 裸 f-string）、缺失标注规范（`build_task_prompt`
> vs 各单项 prompt 手写）、分段策略（`split_for_analysis` vs 配置判定）、
> 配置读取（`_cfg_int` vs 各处 `getattr(...) or default`）、域遍历
> （`EXTRACTION_DOMAINS` vs `ANALYSIS_ITEMS` 单点）。
> 与前九轮一致：**新增判据前先查是否已有唯一实现，并为跨侧一致性补 parity 断言。**

### 4.16 全局事实模块引入易标「补丁机制 + 分批合并 + 上下文预算」（✅ 2026-09-30 第十二轮）

> 目标：把参考软件全局事实模块的**纯逻辑层**（补丁 / 分批 / 上下文预算 /
> 缺值模式措辞）引入本仓，并收口危大阈值与九大章节分类的既有两处缺陷。
> 全部**默认向后兼容**、**无新增配置项**、**无新增依赖**、**零数据迁移**。

#### 4.16.1 参考软件能力清单与本仓落点

参考实现：`client/electron/services/globalFactsTask.cjs`（1031 行）+
`globalFactsTaskV2.cjs`（Agent 版）+ `globalFactsAdjustmentTask.cjs`。

| 参考软件函数 | 本仓落点 | 状态 |
|---|---|---|
| `normalizeFactId`（:127-134） | `facts_patches.normalize_fact_id` | ✅ 引入 |
| `ensureUniqueId`（:136-144） | `facts_patches.ensure_unique_id` | ✅ 引入 |
| `valueToMarkdown`（:147-165） | `facts_patches.value_to_markdown` | ✅ 引入 |
| `buildMissingFactRule`（:12-20） | `facts_patches.build_missing_value_rule` | ✅ 引入 |
| `buildGlobalFactsCompletenessRules`（:22-48） | `facts_patches.build_completeness_rules` | ✅ 引入 |
| `normalizeGlobalFactsPatchResponse`（:212-240） | `facts_patches.normalize_patches_response` | ✅ 引入 |
| `validateGlobalFactsPatchResponse`（:242-251） | `facts_patches.validate_patches_response` | ✅ 引入 |
| `mergeGlobalFactPatches`（:253-284） | `facts_patches.merge_fact_patches` | ✅ 引入 |
| `batchRenderedItems`（:690-713） | `facts_patches.batch_rendered_items` | ✅ 引入 |
| `waitAllOrThrow`（:681-688） | `facts_patches.wait_all_or_throw` | ✅ 引入 |
| `getGlobalFactsSegmentLimit`（:363-368） | `facts_patches.get_segment_limit` | ✅ 引入 |
| `mergeGlobalFactPatches` 落到扁平事实行 | `facts_patches.apply_patches_to_fact_items` | ✅ **本仓新增适配器** |
| `normalizeGlobalFactsMode`（:8-10） | `sse_handlers` 既有 + `facts_patches` | ⚠️ 两侧各一份（见遗留项） |
| 知识库 / 原方案补丁源（:876-907） | — | ⚠️ 本仓无对应数据源，见遗留项 |
| `finalizeGlobalFacts`（:909-920） | — | ⚠️ 本仓已有 `merge_and_deduplicate`，见遗留项 |
| V2 持久 Agent 会话 | — | ❌ 不引入（与本仓熔断器/并发体系冲突） |

#### 4.16.2 关键设计取舍

1. **只引纯逻辑，不引流程编排**：参考软件的 `runGlobalFactsTask`（:922-1018）
   是一条 6 步流水线（读工作区 → 分段 → 知识库补丁 → 原方案补丁 → 整理 → checkpoint），
   强依赖其 `workspaceStore` / `knowledgeBaseService` / `agentService` 三件套。
   本仓对应的编排已在 `sse_handlers.generate_facts`（:6260）里存在且经过 11 轮校准，
   照搬会产生**第二套编排**。故本轮只引入**与编排无关的纯函数**。
2. **适配器保不变式**：`apply_patches_to_fact_items` 只替换 `item.value`，
   **绝不重建 `FactItem`**——重建会冲掉 `source` / `confidence` / `is_simulated` /
   `chapter` / `is_shared` 等标注（与本仓 `/global-facts/adjust` 拒绝让 AI 重写整库
   的设计同源）。护栏用 AST 锁定「`FactItem(` 只允许出现在 `make_fact_item_from_patch`
   一个函数里」。
3. **`replace` 必须清 `conflict_values`**：replace 后旧候选值可能已不存在，
   留着会让前端把已消失的值渲染成「备选值」误导裁决。append/prepend 保留原标志。
4. **`wait_all_or_throw` 与既有 `gather(return_exceptions=True)` 的差异是有意的**：
   分段提取阶段失败只记 `failed_details`（单段不拖垮整轮），而**合并阶段**失败
   意味着结果集本身不可信，静默继续会把未合并的重复项写进事实库 → 必须抛出。

#### 4.16.3 本轮修复的 2 个真实缺陷

1. **【P0 危大阈值】脚手架 24m 漏判（`scheme_classification.HAZARD_THRESHOLDS`）**
   建办质〔2018〕31号 附件一原文是「搭设高度**24m 及以上**的落地式钢管脚手架
   工程」——闭区间。旧实现写成严格 `> 24`，导致**恰好 24m 的脚手架被判为
   「非危大」**：前端不红标、导出预检不拦、专家论证漏提示，而 24m 正是部文
   划定的临界值。本仓 2026-09-24 已对基坑(3/5)、高大模板(8/18/15/20) 做过同类
   `> → >=` 修正，**脚手架是最后一处遗漏**。修正后全表 `>` 条件清零
   （护栏 `test_no_strict_greater_conditions_remain` 静态锁死）。
2. **【P1 事实分类】13 个危大参数名未归入九大章节（`facts_classification.CHAPTER_TEXT_RULES`）**
   `DANGER_PARAM_RULES`（危大参数唯一事实源）与 `NINE_CHAPTERS[0].category_fields`
   实际使用的规范名里，**基坑深度 / 支撑高度 / 跨度 / 施工总荷载 / 集中线荷载 /
   单件起吊重量 / 边坡高度 / 承载力 / 立杆步距 / 起重机高度** 等 13 个未被
   第一章「工程概况」的文本规则覆盖 → 落到空串 =「未分类」→ 在九大章节视图里
   **不显示**、不计入 `chapter_field_completeness` 任一章覆盖率。后果：用户看到
   「工程概况 0 条事实」，实际数据已提取却不知为何不显示。已按
   `NINE_CHAPTERS[0].category_fields` 口径补齐（基坑/模板支撑/起重/脚手架/其他
   五类专项工程特征均属第一章）。

#### 4.16.4 护栏与 A/B 反向验证

新增 `backend/tests/test_global_facts_reference_parity_20260930.py`（**90 例** =
标识与内容归一化 8 + 缺值模式 5 + 补丁归一化/校验 11 + 合并 8 + 分批与等待 9 +
上下文预算 6 + FactItem 适配 7 + 危大阈值闭区间 26 + 静态护栏 10）。

**A/B 反向验证（逐个还原修复点 → 跑同一份护栏）**：

| 还原的修复点 | 定向失败 |
|---|---|
| `sc_ground` 改回 `> 24` | **4 例** |
| `CHAPTER_TEXT_RULES` 删掉危大参数名 | **4 例** |
| `validate_patches_response` 改回空函数体 | **6 例** |
| `build_completeness_rules` 去掉 `return` | **1 例**（收集期 SyntaxError） |
| 全部恢复 | **90 passed** |

> ⚠️ 第 3、4 条是本轮引入代码时**实际发生**的缺陷（分段写入导致函数体被
> 截断），由「对每次修改都跑测试」当场暴露 —— 再次印证 AGENTS.md §3.1.4
> 「测试先行 / 改完必跑」的必要性。

#### 4.16.5 遗留项（记录以免重复排查，本轮未采纳）

- **P1 缺值模式值域两侧各一份**：`sse_handlers.generate_facts:6278` 的
  `not in ("fabricate","omit","placeholder")` 与 `facts_patches.MISSING_VALUE_MODES`
  是两份字面量。已补跨模块 parity 断言防漂移，但**未收敛为单一常量**。
  ✅ **第十三轮已收口**（§4.17）——实际查出是 **3 份**（`facts_extractor` 也有
  两处：提示词层 + 管线后处理层），全部收敛到 `normalize_missing_value_mode`。
- **P1 知识库 / 原方案补丁源未接**：~~本仓事实链路只读解析文档。补丁机制已就绪，
  接线需要新的数据源与产品决策。~~
  ⚠️ **本条前提是错的，已在第十三轮更正**：本仓 `knowledge_base` 表早已存在
  （`routers/knowledge.py` 完整 CRUD），`_build_knowledge_text` 早已被目录/正文
  生成消费——**只是事实链路从不读它**，根本不需要「新增数据源与产品决策」。
  ✅ **第十三轮已接线**（`facts_enrich.apply_knowledge_patches`，
  默认关闭 `facts_knowledge_patch_enabled`）。**原方案**（`existing-plan-expansion`
  工作流）本仓确无对应数据源，仍未接。
- **P1 最终整理（finalize）阶段未引入**：参考软件用一次 AI 调用做
  「去重 + 要求句改写为事实句 + 强制保留工期」。本仓 `merge_and_deduplicate`
  是纯程序归一化，缺「要求句 → 事实句」的语义改写。**属新增 AI 调用**。
  ✅ **第十三轮已收口**（`facts_enrich.finalize_facts`，默认关闭
  `facts_finalize_enabled`）。
- **P2 上下文预算分段未接线**：`get_segment_limit` 已引入但
  `facts_extractor` 仍用固定 `CHUNK_SIZE = 8000`。
  ✅ **第十三轮已接线**（`facts_extractor.resolve_chunk_size`，默认关闭
  `facts_context_budget_split`）。
- **P2 英文归一化键不走九大章节文本规则**：`DANGER_PARAM_RULES` 同时收录
  `foundation_depth` 等英文键，而 `CHAPTER_TEXT_RULES` 是纯中文匹配。
  ✅ **第十三轮已收口**（`FACT_KEY_TO_CHAPTER` + `fact_key` 贯通 4 个调用点）。
  注：这只解决「章节归属」；英文键走中文文本规则本身仍不适用。

> ⚠️ 本轮 2 类缺陷的根因仍是同一条 —— **「同一业务判据在 2~3 处各自实现」**：
> 部文阈值口径（2026-09-24 改了 3 处、漏了第 4 处）、
> 危大参数名（`DANGER_PARAM_RULES` 与 `CHAPTER_TEXT_RULES` 两份清单不同步）。
> 与前十一轮一致：**新增判据前先查是否已有唯一实现，并为跨侧一致性补 parity 断言。**

### 4.17 全局事实遗留项全部收口（✅ 2026-09-30 第十三轮）

> 第十二轮 §4.16.5 记录的 **5 条遗留项本轮全部落地**。三个「新增行为」开关
> **默认全部关闭**，关闭时既有链路行为逐字节一致（零新增 AI 调用）。

#### 4.17.1 遗留项落点

| 遗留项（§4.16.5） | 本轮落点 | 状态 |
|---|---|---|
| 缺值模式值域两侧各一份 | `facts_patches.normalize_missing_value_mode` 成唯一出口 | ✅ |
| 知识库/原方案补丁源未接 | `facts_enrich.apply_knowledge_patches`（易标 :876-891） | ✅ |
| 最终整理（finalize）未引入 | `facts_enrich.finalize_facts`（易标 :909-920） | ✅ |
| 上下文预算分段未接线 | `facts_extractor.resolve_chunk_size`（易标 :363-377） | ✅ |
| 英文归一化键不走九大章节规则 | `facts_classification.FACT_KEY_TO_CHAPTER` | ✅ |

#### 4.17.2 更正上一轮的一处事实性错误

§4.16.5 写「知识库 / 原方案补丁源**本仓无对应数据源**」——**这是错的**。
本仓 `knowledge_base` 表早已存在（产品需求 §3.9，`routers/knowledge.py` 完整
CRUD），`sse_handlers._load_knowledge_rows` / `_build_knowledge_text` 也早已被
**目录生成**（:3807）与**正文生成**（:5177）消费；只是**事实链路从不读它**
（实测 `generate_facts` 全文块内 `knowledge` 出现 0 次）。
⚠️ **教训**：上一轮把「本模块未接线」误判为「本仓无该数据源」，并据此写下
「需新增数据源与产品决策、不落地」——**结论的前提没核实就写进了文档**。
排查「某能力为何没接」前，必须先确认数据源是否真的不存在。

#### 4.17.3 本轮修复的 4 个真实缺陷

1. **【P1 值域分叉】缺值模式合法值域共 3 份**：`sse_handlers.generate_facts`
   与 `facts_extractor` 的**两处**（`extract_from_single_chunk` 提示词层 +
   `run_extraction_pipeline` 后处理层）。缺值模式是「用户可选的三种语义」，
   漏改一处 → 入口放行新模式而下游按 fabricate 处理，用户选了却看到
   「合理补全」结果且**无任何报错**。现 3 处全部收敛到单一出口。
   ⚠️ 第 2 处（提示词层）是**本轮新写的静态护栏当场查出**的——原先只盯着
   「路由入口 + 管线后处理」两处，`grep` 又受中文路径影响漏检。
2. **【P1 英文键从未参与章节判定】**`classify_fact_dimensions` 早已把
   `fact_key` 作为入参接收（并用于 `shared_chapters_for`），但
   `classify_chapter_from_text` 的**签名里没有这个参数** → `foundation_depth` /
   `span` / `total_load` 等 8 个英文归一化键对「章节归属」完全无效，落空串 =
   「未分类」。现新增 `FACT_KEY_TO_CHAPTER` 并把 `fact_key` 贯通 4 个调用点
   （新增参数**可选、默认空串**，既有 4 参调用点逐字不变）。
3. **【P1 单测护栏自身拖慢全量】**「仓库无调试产物」护栏最初用
   `APP_DIR.parent.rglob("_gen*.py")` 整树扫描 —— 会遍历 `data/` / `logs/` 等
   大目录，**实测单次 >30s**（连 PowerShell `Get-ChildItem -Recurse` 同样超时），
   直接把该用例拖到超时。现改为只扫本轮改动的两个目录的浅层 glob。
   ⚠️ **教训**：静态护栏本身也是性能敏感代码，写递归全仓扫描前先量一下耗时。
4. **【P1 `sqlite3.Row` 无 `.get()`】**（**本轮自己引入，全量跑才暴露**）
   给 `_apply_item_updates` 补 `fact_key` 参数时顺手写了 `row.get("fact_key")`，
   而该处 `row` 是 `sqlite3.Row` —— 直接
   `AttributeError: 'sqlite3.Row' object has no attribute 'get'`，
   打挂 **4 个**既有用例（改分类重派生 chapter/fact_attr 两条 + 章节 parity +
   值未变保留冲突）。**该函数第 1066 行原本就写着**
   「⚠️ row 是 sqlite3.Row（无 .get），必须按键取值」——**注释就在眼前仍踩了**。
   与 §5.5 R13 同类。已加源码文本护栏 `test_no_dot_get_on_sqlite_row` 锁定。
   ⚠️ **教训**：单文件测试全绿 ≠ 无回归。改了**共享写路径**就必须跑全量——
   本轮前 4 个相关测试文件全绿，只有全量才暴露这 4 条。

#### 4.17.4 新增配置项（全部默认关闭 = 旧行为）

| 配置项 | 默认 | 作用 |
|---|---|---|
| `facts_knowledge_patch_enabled` | `False` | 启用知识库补充阶段（+1 次 AI 调用） |
| `facts_finalize_enabled` | `False` | 启用最终整理阶段（+1 次 AI 调用） |
| `facts_context_budget_split` | `False` | 分段上限改由上下文窗口动态决定 |
| `facts_enrich_timeout` | `240` | 上述两次新增 AI 调用的超时（秒） |

⚠️ 三个开关关闭时：`run_extraction_pipeline` 行为与引入前**逐字节一致**
（`test_split_is_byte_identical_when_disabled` 断言切分结果完全相同），
且**零新增 AI 调用**、零新增 DB 查询。

#### 4.17.5 关键取舍

1. **两阶段 fail-soft 而非 fail-fast**：参考软件这两个阶段失败会让整轮失败；
   本仓按 fail-soft 处理（记 `warnings` 后用未加工结果继续）——绝不因补充阶段
   失败而让**已经成功提取**的事实整批丢失，与「分段提取失败不拖垮整轮」同策略。
2. **AI 幻觉锚点一律降级为新建**：`_build_patches` 校验 `target_fact_id` /
   `name` 是否命中既有事实，未命中即视为新增。挂到不存在的锚点上会让补丁被
   静默丢弃、用户却以为「已补充」——比误新增一条更难排查。
3. **整理阶段只改写、不增删**：`apply_finalize_result` 不新建事实（模型臆造的
   一律丢弃），并有 `_MAX_FINALIZE_FACT=400` 条数上限——AI 若误把本任务当成
   「重新提取」返回海量事实，不得撑大事实库。
4. **两个新 scene 已登记 `KNOWN_SCENES`**：`facts_knowledge_patch` /
   `facts_finalize`（AGENTS.md §4.5 强制要求，否则场景模型路由静默失效）。

#### 4.17.6 护栏

`backend/tests/test_global_facts_legacy_closeout_20260930.py`（**47 例** =
值域单一出口 5 + 知识库补充 9 + 最终整理 10 + 上下文预算分段 5 +
英文 fact_key 9 + 管线接线与静态护栏 9）。详见 §6 基线。

**A/B 反向验证（逐个还原修复点 → 跑两份护栏）**：

| 还原的修复点 | 定向失败 |
|---|---|
| `sse_handlers` 回到字面量值域 | **3 例** |
| 英文 `fact_key` 不参与章节判定 | **8 例** |
| 上下文预算分段默认改写为开启 | **9 例** |
| 知识库补丁挂到幻觉锚点上 | **10 例** |
| 全部恢复 | **137 passed** |

### 4.18 解析提取模块 · 截断与静默丢数据修复（✅ 2026-09-30 第十四轮）

> 起点是**读后台日志**：`bid_analysis: 结构化提取依据不完整：纳入 2/2 份文档，
> 被截断 招标文件.pdf`。顺着这条线索深挖 `file_parser` / `sse_handlers` /
> `global_facts`，发现两个 P0 级「静默丢数据」缺陷 + 一个恒空死分支。

#### 4.18.1 【P0 数据丢失】PDF 文本层页数上限硬编码 50

| 项 | 内容 |
|---|---|
| 位置 | `services/file_parser.py::MAX_PDF_PAGES`（原 `= 50`） |
| 现象 | 招标文件 / 施工组织设计常见 **100~400 页**。一份 300 页的招标文件只提取前 50 页，**后面 250 页的工程参数、清单、图纸说明全部丢失**，且这些内容不会进入目录 / 正文 / 事实 / 导出**任何一级** |
| 为什么没被发现 | 解析阶段确实调了 `_note_truncation` 写 `parse_warnings` + `parse_truncated`，但 `generate_facts` **从不读这两列**（见下条）→ 用户只看到「解析成功」，界面无任何提示 |
| 修复 | 改为读 `settings.pdf_text_max_pages`（**默认 500**），与 `MAX_PARSED_CHARS=400000` 字对齐（约 800 字/页 × 500 页 ≈ 400000），避免「解析了却在落库环节二次截断」 |
| 兼容性 | 仍保留为**模块级常量**、全部调用点按模块全局读取 → 既有 `monkeypatch.setattr(fp, "MAX_PDF_PAGES", N)` 单测全部照常生效；显式设小配置即可恢复「只取前 N 页」 |
| 防呆 | `_resolve_pdf_max_pages()` 对**非正数**与**非整数**都回落 50：返回 0 会让 `doc.pages(0, 0)` 一页不解析且因「页数 > 上限」不成立而**不产生任何告警**；`int(3.7)=3` 会让「配了个小数」静默变成「只解析 3 页」 |

#### 4.18.2 【P0 静默截断】截断文档被当成完整依据

- **位置**：`routers/sse_handlers.py::generate_facts` 的源文档 SQL
- **根因**：只取 `(file_name, parsed_markdown)`，**从不读** `parse_truncated` /
  `parse_warnings`。而 §4.15.5 早已记录「生成链不读 parse_status / parse_truncated」，
  `bid_analysis._list_parsed_documents` 也早已消费该列 —— **本链路是漏改点**。
- **后果**：一份只被解析了前 N 页的招标文件被当成**完整依据**参与事实提取，
  用户看到「提取完成」却无从得知依据是残缺的。
- **修复**：新增 `_load_facts_source_docs()` 单一读取出口，**三层降级**：
  ① 有 `parse_truncated` 列 → 直接读；② 仅有 `parse_warnings`（旧库）→ 按
  告警文本含「截断」兜底；③ 两列都没有 / 读失败 → 只取基础两列（即旧行为）。
  截断清单以 **SSE `warning` 事件**显式推给用户。

#### 4.18.3 【P1】「有 N 份文档尚未解析」告警恒不触发（死分支）

旧代码 `pending_docs = [d[0] for d in docs if not d[1]]` —— 而 `docs` 来自
`WHERE ... parsed_markdown IS NOT NULL AND parsed_markdown != ''`，**每行正文都非空**，
于是 `pending_docs` **恒为 `[]`**，该告警从未触发过；同理 `if docs else` 在
「全部文档未解析」时会误报「项目下没有已上传的资料文档」（用户明明传了文件）。
现抽出 `_count_docs_pending_parse()`（查全量、按正文为空判待解析）。

#### 4.18.4 护栏与 A/B 反向验证

新增 `backend/tests/test_parse_truncation_fix_20260930.py`（**25 例** =
PDF 页数上限 9 + 截断诊断三层降级 5 + 待解析死分支 4 + SSE 接线 7）。

| 还原的修复点 | 定向失败 |
|---|---|
| `pdf_text_max_pages` 回到 50 | **2 例** |
| 非整数配置不再回退（静默取小值） | **3 例** |
| `generate_facts` 不读 `parse_truncated` | **4 例** |
| 待解析告警回到恒空分支 | **4 例** |
| 全部恢复 | **25 passed** |

> ⚠️ 新增配置项 **`pdf_text_max_pages`（默认 500）**。这是本轮唯一的「默认即改变
> 行为」项 —— 但改变方向是**修复数据丢失**（多解析而非少解析），且逐页容错与
> 单页失败跳过均已就位，不降低稳健性；仍提供配置项以便按机器性能回收。

### 4.19 解析提取模块 · 遗留项收口（✅ 2026-09-30 第十五轮）

> 第十一轮 §4.15.5 记录的 8 条遗留项，本轮**全部收口或判定为非缺陷**。

#### 4.19.1 落地清单

| 遗留项（§4.15.5） | 本轮落点 | 状态 |
|---|---|---|
| P1 `section_hint` 拼进同一条 system 消息 | `bid_analysis_service.build_system_messages` | ✅ |
| P1 `format_downstream_context` 无预算上限 | `_apply_downstream_budget`（两级裁剪） | ✅ |
| P2 生成链不读截断诊断（目录/正文侧） | `global_facts.load_parsed_docs` 三层降级 | ✅ |
| P2 提取项重跑级联失效不全 | `_invalidate_extraction_derived`（三处补齐） | ✅ |
| P1 日志无上限裁剪 | `task_registry` 已有固定结构，无需改 | 判为非缺陷 |
| P2 提取层物化只覆盖 5/18 | 非预留类别已 100% 覆盖 | 判为非缺陷 |
| P2 资料集合三链不一致 | 5/3/无 LIMIT 各有职责 | 判为非缺陷 |
| P2 36 项提示词未注册进 registry | 易标同样硬编码 | 不引入 |

#### 4.19.2 【P1】标段上下文改为独立第二条 system 消息（易标口径）

- **问题**：`build_system_prompt` 的 docstring 声称「作为独立 system 消息注入」，
  `bid_analysis.py` 的注释同样如此，**但实现是 `prompt += f"\n\n【当前处理标段…】"`**
  —— 注释与实现长期不符。易标 `buildTenderContextMessages` 是**独立第二条**。
- **为什么要改**：拼进同一条时，标段限定会与「必须逐条输出结构化结果」等硬性
  纪律混在一段里，弱模型容易把作用域约束当成可忽略的上下文；独立消息则在
  **消息层级**上表达「这是作用域约束」。
- **落点**：新增 `build_system_messages()`（加法式）。`build_system_prompt`
  **一行未动**（历史契约由既有单测锁定）；两处调用点（`_run_single_call` /
  `_run_single_item` 的合并分支）改走新函数。
- **向后兼容**：无 hint → 单条 system 且与旧版**逐字一致**；只有
  `classification_hint` → 仍拼在第一条（它与通用纪律同属「提取要求」）。

#### 4.19.3 【P1】`format_downstream_context` 预算上限

两域共 36 项全量下发时**无上限**（实测 10 万字以上），而该文本每章都重发一次。
新增 `_apply_downstream_budget`（`DOWNSTREAM_CONTEXT_MAX_CHARS=12000` /
`DOWNSTREAM_PER_ITEM_MAX_CHARS=2000`），**两级裁剪且均留痕**：
① 逐项软截断（补「本项已截断，原 N 字」）；② 总量封顶时按小节整节丢弃，
并在末尾注明被省略的项数与查看入口。

⚠️ **本轮自引入又当场修掉的缺陷**：首段 `head` 曾被无条件保留，于是
「只有一个巨型小节」时总长**完全绕过上限**（12k 预算形同虚设）。已改为
首段同样受上限约束，并加护栏 `test_helper_direct` 锁定。

#### 4.19.4 【P2】`load_parsed_docs` 读截断诊断 + 级联失效补齐

- **读侧**：目录生成（limit=5）与正文生成（limit=3）此前都在**残缺文本**上工作
  且无任何提示。现按实际列集合动态拼装 SQL（三层降级同 §4.18.2），
  并**恒记一条 WARNING**——可观测性不依赖调用方记得传参；调用方可传
  `truncated_out` 自行处置。**截断不阻止使用**（残缺依据仍比没有依据好）。
- **写侧**：`_invalidate_extraction_derived` 补齐三处 —— `consistency_scan_cache`
  （否则「刚重跑完提取、一致性面板仍是旧冲突」）、`schemes.facts_updated_at`
  （否则收不到「章节需重写」提示）、`doc_extractions` 标 `stale`（否则完整性报告
  仍是旧数字）。全部幂等 + fail-soft。
  ⚠️ `doc_extractions` **没有** `updated_at` 列（见 `schema_sql`），SQL 带上会整条
  报错、连带这一轮失效全废；护栏用 **AST 取字符串常量**锁定（注释里必然提到该
  列名，纯文本匹配会假失败）。

⚠️ 本轮自引入又当场修掉的缺陷：`_invalidate_extraction_derived(db, real_pid, …)`
里 `real_pid` 在该函数中**未定义**（形参叫 `project_id`）→ 6 个既有用例全挂。
**教训：新增调用点后立刻 grep 形参名，不要凭上下文记忆写。**

#### 4.19.5 判定为「非缺陷」的两项（记录以免重复排查）

- **提取层物化 5/18**：`EXTRACT_TYPES` 7 类中 `boq` 是 RESERVED、
  `global_facts` 由 `sync_extract_layer` **独立分支**特供（不依赖解析项映射），
  其余 5 类**全部**已覆盖。既有护栏
  `test_doc_pipeline.py::TestExtractTypeCoverage` 已锁此契约。要物化其余
  13 项需先新增提取类别（产品决策 + schema 语义变更）。
- **资料集合三链不一致**：目录 5 / 正文 3 / 事实无 LIMIT 是**按下游职责分别设定**
  —— 事实提取要全量依据（少一份就可能漏关键参数），目录/正文只取摘要片段
  （超预算反而稀释指令），4000 字截断才是真正的护栏。

#### 4.19.6 护栏

`backend/tests/test_bid_analysis_legacy_closeout_20260930.py`（**29 例** =
独立 system 消息 6 + 下发预算 7 + load_parsed_docs 诊断 8 + 级联失效 6 +
非缺陷判定 2）。A/B 反向验证 4 项（3/1/5/6 例定向失败，恢复 → 51 全绿）。
相关回归 8 个文件 **253 passed**。

### 4.20 参考软件（易标九篇系列）差距分析与 P0 落地（✅ 2026-09-30 第十六轮）

> 依据用户提供的《全功能模块.txt》（易标《标书智能体》一~六 + 知识库非 RAG +
> opencode 集成 + 提示词顺序优化）产出差距分析报告
> `docs/reference_gap_analysis_20260930.md`，本轮落地其中**两个 P0 差距**。

#### 4.20.1 差距分析结论（摘要）

| 文档能力 | 本仓状态 |
|---|---|
| 统一 JSON 封装 / 目录分步生成 / 目录审核 / 项目概述 / Mermaid 链路 | ✅ **已具备且更强** |
| **技术评分要求提取（§一.4）** | ❌ 缺失 → **本轮落地** |
| **old_text/new_text 定点替换（§三.4/§五.1）** | ❌ 缺失 → **本轮落地** |
| 一级目录 ← 评分大类一一对应（§二.4） | ❌ 缺失（P1，待做） |
| 知识库非 RAG 构建流程（§七） | ❌ 缺失（P1，待做） |
| 正文编排 / 预编排（§四.5） | ❌ **已显式移除**（架构级对立，P1 待立项） |
| 提示词顺序优化（§四.4/§九） | ⚠️ 部分（第十五轮已做 system 分层） |
| opencode 集成 / Agent+Skill（§八） | ❌ **判定不引入**（理由见报告 §3） |

⚠️ **一处易误判**：另一域的 `BID_RESPONSE_ITEMS` 里**已有** `techRequirements`
（技术评分要求），但那属**招标响应域**（默认关闭、面向招标文件响应分析），
与本仓的**专项方案编制域**业务完全不同 —— 不能据此认为本仓已具备该能力。

#### 4.20.2 G1 · 技术评分要求提取（§一.4）

- **落点**：`ANALYSIS_ITEMS` 新增 `techScoring`（`json`，四字段
  `item_name`/`weight`/`criteria`/`source`）；`GROUPS` 新增 `scoring` 分组；
  `_ITEM_PROMPTS` 新增自我反思式五段式提示词（目标定位 / 提取内容 / 处理规则 /
  验证 / 只返结果），含「忽略商务报价、资格、资质、业绩」与「资料没有时不编造」。
- **`sort_order=19` 追加到末尾**而非插到前面：既有 18 项的 sort_order 是**历史
  数据契约**（前端展示顺序 + 已落库行顺序），插队会让老项目消费顺序错位。
  护栏 `test_sort_order_appended_not_inserted` + `test_existing_18_items_untouched`
  逐项锁定。
- `required=0`：招标文件可能没有技术评分章节，不得卡住提取流程。

#### 4.20.3 G2 · 定点替换（§三.4 / §五.1）

- **落点**：新增 `services/consistency_edits.py`
  （`find_unique_span` / `apply_unique_edits` 纯逻辑 + `collect_repair_edits`
  AI 封装）+ 提示词 `consistency_repair_edits_system/user`。
- **核心不变式**：`old_text` 必须**唯一命中**才替换；多处命中 / 找不到 / 过短
  （< `MIN_OLD_TEXT_CHARS=8`）一律拒绝并记原因，**不猜** —— 无法确定改哪一处时，
  猜错比不改更糟。宽松匹配仅折叠空白（应对模型折行），仍在**归一后唯一**才命中。
- **接线**：`repair_agent.repair_section` 改为**定点优先 + 整章重写兜底**。
  兜底是刻意的：部分模型不支持稳定 JSON 输出，没有兜底会让修复能力整体失效。
- ⚠️ **依赖注入是必须的**：`collect_repair_edits(chat_fn=...)` 接收调用方的
  `chat_with_fallback` 引用。既有单测 monkeypatch 的是
  `repair_agent.chat_with_fallback`；若在 `consistency_edits` 内部直接
  `from provider_factory import`，会**绕过** monkeypatch 打真实 provider
  （本轮首次实现即踩到，单测直接挂起）。

#### 4.20.4 护栏

`backend/tests/test_reference_alignment_20260930.py`（**33 例** =
技术评分项 11 + 唯一命中定位 5 + 编辑应用 11 + 接线与降级 6）。

### 4.21 第十八轮（2026-10-01）：重复率检测 + JSON 模式判据收敛 + 分类体系护栏

> 依据 `docs/专项方案六模块全功能规格_角色转换版.md` §4.1 缺口总表落地**第一批 + 第二批**（G1/G4/G9/G10/G11）。全部**默认向后兼容**、无新增依赖、零数据迁移。

#### 4.21.1 【P0 判据分叉】JSON 模式兼容性判定（缺口 G9）

**根因**：同一判据在**两处**各自实现，且都只认英文关键词。

| 位置 | 原实现 | 后果 |
|---|---|---|
| `provider_factory._json_mode_unsupported` | `response_format` / `json_object` / `json mode` | 编排层不回退普通模式 |
| `providers/openai_compatible._is_response_format_unsupported` | 另一套英文 marker | HTTP 层不摘字段，直接抛 `RuntimeError` |

国内厂商拒绝 JSON 模式时返回**中文**（「该模型暂不支持 JSON 输出」等），两端均漏判：
①不回退普通模式；②把「参数不兼容」计入**熔断失败 + 配额冷却**；③整条候选链用同一原因逐个失败，目录生成 / 事实提取 / 一致性审计等**全部 JSON 类任务同时挂死**。

**修复**：判据下沉到 `services/ai/json_mode_compat.py`（唯一事实源）。**不能放在任一侧**——`provider_factory` 本就 import `providers.*`，反向 import 会循环依赖，故下沉到两者共同的下层。
- `json_mode_unsupported(err)`：两级判定（直接 token → 否定词**与** JSON 语义词**组合**）。
  组合判定的理由：`不支持` / `invalid parameter` 单独出现会误伤「不支持该地域」。
  误判成本评估：误判只多烧一次普通模式重试；漏判是整条链失败 → **宁松勿紧**。
- `response_format_rejected(status, body)`：HTTP 层，保留「400 + 点名参数即命中」的旧行为。
- 两个调用方均改为**薄包装**（保留原函数名，调用点零改动）。

⚠️ 收敛时必须**保留** HTTP 层独有的 4 个 marker（`does not support` / `not support` / `unknown parameter` / `must be`），否则属静默行为回退（护栏 `test_provider_layer_markers_preserved_after_convergence`）。

#### 4.21.2 【P1 漏报】跨章节段落搬运检测（缺口 G10）

**缺口**：`CON-05` 只做**整章级** 4-gram Jaccard，两章整体不同时漏掉「成段照抄」——而这恰是 AI 生成最常见的雷同形态。纯程序、零 AI、零成本。

新增 `services/duplicate_detection.py`（对齐参考软件 `duplicateCheckService.cjs`，按本域裁剪）：
- **保留**：骨架归一（`{date}`/`{code}`/`{num}`/`{percent}`/`{money}`/`{page}`）、字符 2-gram Dice + 双阈值分档（长句 `0.90/0.82`、短句 `0.95/0.88`）。
- **裁剪**：投标语境专属的「字段白名单 / 骨架门控关键词」（本仓无该对立关系）、图片哈希（图表全自动生成）、跨方案分组。

新增规则 `CON-06`（consistency 维度、program 模式、派生编号 `CON-06-N`），`RULE_VERSION` → **1.7.0**。

**本轮自引入又当场修掉的 4 个缺陷**（均由「写完必跑」/ 压测暴露）：
1. **句末符含 `:`** —— 施工文本里 `:` 绝大多数是**比值**（坡度 `1:0.75`），把「边坡坡度1:0.75放坡」切成两半，造出大量无意义短句。改为不含 ASCII 冒号（保留全角 `；`）。
2. **先剔除标点再套字段规则** —— `95%` 已变成 `95`，百分数规则必然落空。改为 **NFKC 折叠 → 套规则 → 剔除标点**。
3. **占位符花括号被标点清理吞掉** —— `{num}` 退化成 `num`，与正文真实英文混淆。改为 `_keep_only(text, extra=...)` 白名单，**唯一清理实现**。
4. **【性能 P0】朴素两两比对跑不完** —— 80 章 × 600 句（4.8 万句）实测 **>30s 未完成**（11 亿次 bigram 集合运算）。改为**倒排索引 + 高 DF 剪枝**：仅「共享 ≥2 个 bigram」的句子对进入 Dice 精判，且 posting 长度 >2000 的高频 gram（`工程`/`要求`/`进行`）整条跳过（标准 IR 剪枝）。压测 4.8 万句 **2.29s** 完成。
   ⚠️ 这是**精确剪枝**而非近似采样：被照抄段落整段重复、必然共享大量**低频** bigram（具体构件名+参数组合），故不损失真重复 —— 护栏 `test_pairwise_pruning_is_lossless` 用「400 句高频噪声 + 1 段真搬运」构造验证。
   教训：**O(n²) 判据在真实数据量下必须实测**，不能只看「典型方案 38 章」就认为秒级。

⚠️ **与 CON-05 的边界（防双报）**：`CON-06` 的排除集合**直接取自** `check_duplication` 内的 `_find_duplicates(ctx.sections)`（同一函数、同一阈值），不得重写整章相似度口径。
⚠️ **目录侧 `similar_titles` 为加法式**：只作顶层附加键，**不进 `issues`** —— 近似标题是「建议明确区分」的组织建议而非结构缺陷，计入会让 `ok` 误变 False、误判目录不合格。

#### 4.21.3 【P1 静默漂移】四套分类体系一致性护栏（缺口 G11）

四套分类（危大六大类 / 九章 `category_fields` / 危大 10 章关键词 / 14 条阈值表）**互相引用却零断言**。新增 `tests/test_classification_parity_20261001.py` 锁定：
六大类 id 精确一致、阈值键必须是已声明子类、阈值**必须**闭区间（`>=`，脚手架 24m 事故防线）、危大 10 章标签序列按序比对、category_fields 键集合相等。

⚠️ 护栏口径的**三次修正**（护栏本身过严会把正确实现判成缺陷，后人只会把护栏改松）：
- `standards` 允许为空列表（拆除类本就没有独立标准清单），只锁**结构完整**；
- 「判得出危大」有**三种**合法表达（`hazard_when` / `hazard_always` / **空规则**=非参数型危大），缺一即死规则；
- 危大 10 章标签是**法定全称的简写**，不能用子串/逐字判定 → 显式列出期望序列按序比对（顺序漂移最危险：会把「安全」错配到「施工工艺」）。

#### 4.21.5 ⚠️ 本轮事故记录：`review_autofix.py` 被清空后按契约重建（必读）

**事故**：第十八轮做 A/B 反向验证时，临时脚本用 `io.open(p, "w")` 做「写回原文件」，
在一次运行中把 `backend/app/services/review_autofix.py`（约 40KB）**写成 0 字节**。
根因：脚本未对写回内容做**非空校验**，且恢复与打补丁共用同一路径、并发运行下互相覆盖。

**恢复过程与结论**：
- 该文件是 `??`（**从未纳入 git**），`git checkout` 无法恢复；
- 穷尽扫描 git 对象库全部 1565 个 blob，确认该文件**只存在一个版本** ——
  带批量功能（`stage_fixes` / `_chain_section_fixes` / `sentence_idx`）的版本
  **从未进入过 git**（cline 检查点拍在其写入之前），**无法原样恢复**；
- 从 gitee 远端（`refs/remotes/gitee/master` = `e128ca4`，与本地 HEAD 同）
  恢复到 blob `25f43b16`（39841 字节 = 2026-09-30 版，**不含批量功能**）。

**按契约重建的部分**（依据：routers 侧完好 ⇒ 调用签名与返回键固定；10 个测试 ⇒ 行为契约明确）：

| 新增 | 说明 |
|---|---|
| `_SENTENCE_SPLIT_RE` / `_sentence_pos` | 行 → 句细化，**1-based**；⚠️ 不含 ASCII 冒号（坡度 `1:0.75` 是比值不是句末） |
| `_target` / `_hit` 补 `sentence_idx` / `sentence_total` | 整章型问题显式给 `0`（让前端显示「整章」而非伪造「第 1 句」） |
| `_chain_section_fixes` | 同章多问题链式改写；**每轮把累积正文喂给下一条**（否则互相覆盖） |
| `stage_fixes` | 批量暂存（不落库、不做快照），返回 `{batch_id, items, stats, status}` |

⚠️ **重建 ≠ 原版**。这 4 处按契约实现，护栏只保证「routers 契约 + 10 个测试契约」
成立，不保证与原版逐字一致。

**两条由此得出的硬约束**：
1. **`backend/app/` 与 `backend/tests/` 必须纳入 git 跟踪** —— 当前 100+ 个 `??`
   文件，任何一次误写都无法回滚，这是本次事故无法自行恢复的根本原因。
2. **任何「写回原文件」的临时脚本必须做非空断言 + 写后校验**（`os.path.getsize > 0`
   且 `ast.parse` 通过），否则恢复动作本身会成为第二次破坏。

#### 4.21.6 护栏与 A/B 反向验证

| 文件 | 例数 | 覆盖 |
|---|---|---|
| `tests/test_duplicate_detection_20261001.py` | 63 | 归一化/骨架键/双阈值/两级判定/句末切分/搬运检测/标题近似/CON-06 接线/倒排剪枝无损性/静态护栏 |
| `tests/test_json_mode_unsupported_20261001.py` | 40 | 英文向后兼容 + 中文识别 + 反例 + 单一事实源 AST 扫描 + 两层薄包装一致 |
| `tests/test_classification_parity_20261001.py` | 18 | 四套分类对齐 + 闭区间 + 子类前缀 + 10 章序列 |

**A/B 反向验证 12 项**（逐个还原修复点 → 定向失败 → 恢复全绿）：

| 还原的修复点 | 定向失败 |
|---|---|
| 句末符重新含 ASCII 冒号 | **7 例** |
| 骨架键先归一再套规则 | **5 例** |
| 占位符花括号取消白名单 | **9 例** |
| 倒排剪枝改回朴素两两 | 3 例（性能类用例会超时，需单独计时） |
| CON-06 不排除 CON-05 章节对 | **1 例** |
| `similar_titles` 混入 `issues` | **3 例** |
| CON-06 不登记程序规则集 | **1 例** |
| JSON 模式判据退回纯英文 | **1 例** |
| providers 侧自带关键词表 | **3 例** |
| 六大类 id 漂移 | **2 例** |
| 脚手架 24m 改开区间 `>` | **1 例** |
| 危大 10 章调换第 5/6 章 | **1 例** |
| 删除「监测方案」额外章 / 阈值键游离 / 子类前缀错配 | 各 1~2 例 |

⚠️ **本轮顺带修掉的 3 个既有缺陷**（全量跑才暴露，AGENTS.md §4.17.3 同类教训）：
1. `global_facts.py:2360` `except OSError` 分支内引用了**未定义的 `doc` / `e`**（上轮 A-4 编辑时串位），`ruff F821` 报错 → 改为清理失败的真实上下文。
2. `review_autofix.py`（services）引用 `repair_record.save_repair` 但**该模块从未 import** → `ruff F821`；运行期表现为「批量修复走到留痕那一步才 NameError 500」，前面的 AI 修复全部白烧。改为顶部统一 import（并删除 `apply_fix` 里的函数内局部 import），两处收敛为**同一留痕出口**。
3. `test_parse_truncation_chain.py` 写死 `30000` 字预算构造用例，而 A-1 已把预算提到 `400000` → **恒假失败**。改为按 `ba._MAX_DOC_CHARS` 推导规模。
   教训：**修预算类配置时必须全仓搜「写死旧数值的测试」**，否则护栏会静默失效并掩盖该修复。
   ⚠️ 前两条都只被 `tests/test_outline_workflow_logs.py::TestNoUndefinedNames`（`ruff F821` 全仓扫描）捕获 —— **静态扫仓护栏能一次性发现跨轮的编辑串位**，其价值已连续两轮得到验证，不要因为「它只是 lint」而删。

## 5. 常见坑

1. **`ai_audit_logs` 列不齐**：运行库可能未迁移出 `scene` 列，代码已做降级 INSERT（`provider_factory._flush_audit_buffer`）。
2. **`consistency_scan_cache` 表缺失**：需要重启后端触发 `_migrate`（表不存在时 `_migrate` 记 debug 跳过，不报错，容易被忽略）。
3. **`{max}` 类误报**：提示词里 JSON 示例 `{max: ...}` 会被误识别为变量占位符。修 `prompts/_registry._VARIABLE_PATTERN` 而非移除示例。✅ 2026-09-24 已全链路对齐：`validate_prompt_variables` / `render_prompt` 残留检测 / `check_prompt_variables`（变量契约）三条告警路径共用同一判据 `_is_false_positive`，不会再因该已知误报刷 WARNING。
4. **前端 401 短路**（R14 已修复）：`api/index.ts` 的 `authGuard` 会在收到 401 后短路所有请求，改凭据前必须先 `clearAuthShortCircuit()`。鉴权规则：token 取 `X-API-Key` 优先、其次 `Authorization: Bearer`；`api_auth_token` 为空时**完全放行**（本地单机默认）。
5. **R13 事务内 `cur is None`**（✅ 2026-09-29 已全链路收口）：全局单连接 + aiosqlite 下 `execute()` 可能返回 None。`_chart_pipeline.py` 最早加判空，`doc_pipeline.py` / `routers/prompts.py` 的漏改点已补齐；**第七轮（2026-09-29）把全仓 7 处裸 `.rowcount` 收敛到 `app.db.safe_rowcount(cur, what=...)` 单一出口**（`None` → 0 + WARNING、负数 → 0），并加静态护栏 `TestRowcountIsCentralized` 禁止在 `app/db.py` 之外再裸取 —— 新增写操作时**必须**走 `safe_rowcount`。见 §4.12 第 4 条。
6. **日志冻结 / worker 不接管 socket**（2026-09-23 事故）：僵尸 pytest 持有 `logs/backend.log` 句柄导致 5MB 轮转 rename 失败 → 日志永久冻结。**日志时间戳停止增长 = 先怀疑日志系统冻结，而非业务代码 bug**；排障顺序：看日志 mtime → 查端口 8000 归属进程 → `py-spy dump` → 清理僵尸 pytest → 重启 uvicorn。已在 `utils/safe_log_handler.py` 做轮转失败降级追加，前端 `sseFetch` 加 30s 建连超时。
7. **本仓的"文件切换(swap)"工作流**：目录内存在 `*.bak-preswap`、`*.bak-<时间戳>` 备份；会话进行中文件可能被**整体替换**。改动前先 `read_file` 重读目标文件，改后复验自己的写入仍在（搜索自己新增的标识符）；不要把 `.bak-*` 当源码编辑或删除。
8. **临时产物与生成物**：根目录 `_exports/`、`_tmp_screenshot.png`、`vitest_result.json`、`_openapi.json`、`patch_preview_pipeline_summary.py`，以及 `backend/_*.txt`、`backend/_*.py` 均为历史调试产物，**不要当源码维护、也不要新增同类文件**。
9. **改非 UTF-8 文件前必须先探测编码**（`start_all.bat` 是 GBK/CP936）：编辑类工具按 UTF-8 读 GBK 文件会把每个非法字节变成 U+FFFD 再整文件写回，**中文永久丢失**（2026-09-23 由 AI 编辑造成过一次）。正确做法：只做字节级 / 显式 CP936 编解码修改，或保证新增内容纯 ASCII；改完用两条断言验收——「UTF-8 严格解码必须失败」+「字节里不得出现 EF BF BD」。误改后可从 `%APPDATA%\Qoder\SharedClientCache\cache\workingSpace\<uuid>__<文件名>` 的快照找回原始字节（本仓无 git，这是唯一可靠备份源）。
   - **2026-09-30 第十三轮发现存量残留**：`app/routers/sse_handlers.py:2602` 有 **2 个 U+FFFD**（早前某轮编辑损坏）。它能让**任何字节级校验脚本失明**——本轮首次扫描时因断言用中文字面量、控制台 GBK 编码不匹配而误判「未找到」，改用 `\uXXXX` 转义 + 纯 ASCII 脚本才定位到。已按上下文（2603 行以 `**` 起手）判定丢失的是 markdown 粗体标记并修复。**建议**：改完文件后固定跑一次「`open(p,'rb').read().count(b'\xef\xbf\xbd') == 0`」检查，且校验脚本**一律用 ASCII + `\u` 转义**写（控制台是 GBK，中文字面量会失配）。
10. **`netstat` 报的端口占用者可能早已死去**：`uvicorn --reload` 的监听 socket 由 reloader 父进程创建、worker 子进程继承句柄；父进程一死，`taskkill /PID <父>` 报「找不到进程」，而端口仍长期 LISTENING，看着像「权限不足」，真正持句柄的是活着的孤儿 worker（命令行带 `--multiprocessing-fork`，`ParentProcessId` 指向那个已死 PID）。必须按 PID 递归找出**存活后代**再杀 —— 见根目录 `kill_port.ps1`（`start_all.bat` 第 1 步已接入）。
11. **路由签名里的 `Request` 不能写成 `Request | None`**：ai_config 的多条端点写成 `request: Request = None`（默认值只为兼容「单测直接调用路由函数」）。**注解写联合类型会让 FastAPI 识别不出 Request**，转而把它当请求参数（注入失效、审计拿不到 `client_ip`、OpenAPI 多出一个 `request` 参数）。护栏：`tests/test_api_contract.py::test_ai_config_request_injection_not_exposed_as_param`。
12. **`GET /ai/config` 的 `presets` 与 `GET /ai/models` 的 `providers` 形状不同**：前者是厂商字典**原样**（`model` 键、`plans` 可缺省），后者被归一为 `AIProviderPreset`（`default_model` 键）。前端混用会「取值为 undefined 但不报错」。契约类型已分别建模为 `AIConfigPresetRaw` / `AIProviderPreset`（`frontend/src/types/aiConfig.ts`）。
13. **进程清理走 `cleanup_guard.ps1` 单一入口**（2026-09-28）：启动 = `start_all.bat` pass 4，关闭 = `stop_all.bat`，排障 = `-Mode Check`。四类残留各有判定口径（backend/frontend/stub/pytest）+ 孤儿兜底，见 `docs/process_guard_mechanism.md`。三个必须记住的坑：
    - **「命令行含工作区路径」不足以判定前端** —— 早期版本据此把 8 个同工作区的工具 shell 当成 vite 杀了。`cmd.exe` 必须额外命中 `npm run dev`/`vite` 特征；`node` 才可用根路径。
    - **不能把字符下标当字节下标** —— 对 GBK 的 `start_all.bat` 做字节级插入时，字符串下标 ≠ 字节偏移，会把注释从中间劈开。必须用「先整串解码 → 字符串 `Insert` → 整体重编码」，且插入内容保持纯 ASCII（2026-09-28 实际损坏过一次，靠字节级反 spliced 修复）。
    - **`powershell -File` 子进程在本机要 5~6s** —— 因此 `cleanup_guard.ps1` 内联了 `kill_port.ps1`/`kill_zombie_pytest.ps1` 的逻辑（单次清理 19~25s → 8~9s）。**两份实现必须同步改**，回归门是 `cleanup_guard_selftest.ps1` + `kill_port_selftest.ps1`。
    - 另：`Win32_Process.CreationDate` 在本机常为空字符串，进程年龄必须用 `[System.Diagnostics.Process].StartTime`，否则「超时残留」永远判定为 0 秒、**清理逻辑静默失效**。

## 6. 测试基线

- 后端：`backend/tests/`（`test_*.py`），运行 `python -m pytest tests/ -q`；**2026-10-01 第十八轮实测基线 4313 passed, 4 skipped, 3 xfailed**（0 failed；全量约 3 分 40 秒，实测 221.62s）。⚠️ 该基线含本轮新增护栏 121 例（重复检测 63 + JSON 模式 40 + 分类 parity 18）。= 第十五轮 4077 + 本轮新增护栏 33。此前记录的 3697 / 3886 / 3976 / 4023 / 4048 / 4077 已过期 —— 改动时请**以实测为准**、不要沿用文档里的旧数字。
- **参考能力对齐专项（2026-09-30 第十六轮）**：`python -m pytest tests/test_reference_alignment_20260930.py -q`（**33 passed**）。覆盖差距分析报告 `docs/reference_gap_analysis_20260930.md` 的两个 P0：技术评分要求提取（`techScoring` 新增项 + 自我反思式五段式提示词 + sort_order 历史契约不变）、old_text/new_text 定点替换（唯一命中才替换 / 多处命中拒绝 / 定点优先+整章重写兜底 / 依赖注入）。已做 **3 项 A/B 反向验证**（4/5/5 例定向失败，恢复 → 33 全绿）。
- **解析提取遗留项专项（2026-09-30 第十五轮）**：`python -m pytest tests/test_bid_analysis_legacy_closeout_20260930.py -q`（**29 passed**）。覆盖标段上下文独立第二条 system 消息（易标口径）、`format_downstream_context` 两级预算裁剪、`load_parsed_docs` 三层降级读截断诊断、提取项重跑的三处级联失效补齐、两条「非缺陷」判定。已做 **4 项 A/B 反向验证**（分别 3/1/5/6 例定向失败，恢复 → 51 全绿）。相关回归 8 个文件 **253 passed**。
- **解析提取截断专项（2026-09-30 第十四轮）**：`python -m pytest tests/test_parse_truncation_fix_20260930.py -q`（**25 passed**）。覆盖 PDF 页数上限可配置且默认 500、非法/非整数配置回落 50、截断诊断三层降级（`parse_truncated` → `parse_warnings` 文本兜底 → 基础两列）、待解析告警不再恒空、SSE 接线与「源文件名/正文成对」防错位。已做 **4 项 A/B 反向验证**（分别 2/3/4/4 例定向失败，恢复 → 25 全绿）。相关回归 11 个文件 **265 passed**。
- **全局事实模块专项（2026-09-30 第十三轮 · 遗留项收口）**：`python -m pytest tests/test_global_facts_legacy_closeout_20260930.py tests/test_global_facts_reference_parity_20260930.py -q`（**137 passed**）。覆盖 5 条遗留项收口（值域单一出口 / 知识库补充 / 最终整理 / 上下文预算分段 / 英文 fact_key）、三个开关默认关闭、切分逐字节一致、两个新 scene 登记、管线接线与 fail-soft 静态护栏、`sqlite3.Row` 无 `.get()` 陷阱。已做 **4 项 A/B 反向验证**（分别 3/8/9/10 例定向失败，恢复 → 全绿）。
- ⚠️ **改了共享写路径必须跑全量**：第十三轮首次跑全量时，前 4 个相关测试文件**全绿**，全量却暴露 4 条 `sqlite3.Row` 回归（见 §4.17.3 第 4 条）。单文件绿 ≠ 无回归。
- **全局事实模块专项（2026-09-30 第十二轮）**：`python -m pytest tests/test_global_facts_reference_parity_20260930.py -q`（**90 passed**）。覆盖与易标 `globalFactsTask.cjs` 的 11 项纯函数 parity、FactItem 适配器不变式、危大阈值 7 组参数闭区间、九大章节分类补齐、静态护栏。已做 **4 项 A/B 反向验证**（分别 4/4/6/1 例定向失败，恢复 → 全绿）。
- **第十八轮专项（2026-10-01）**：`python -m pytest tests/test_duplicate_detection_20261001.py tests/test_json_mode_unsupported_20261001.py tests/test_classification_parity_20261001.py -q`（**121 passed** = 重复检测 63 + JSON 模式判据 40 + 分类体系 18）。已做 **12 项 A/B 反向验证**（分别 7/5/9/1/3/1/1/3/2/1/1/1~2 例定向失败，恢复 → 全绿）。相关回归：AI 链 119 例、审核预检 58 例、目录/跨模块 147 例均通过。
- ⚠️ **新增 O(n²) 判据必须做真实数据量压测**：第十八轮查重的朴素两两实现，在 38 章典型方案下是 78ms（看着没问题），但 80 章 × 600 句 = 4.8 万句时 **>30s 跑不完**。压测脚本应写到系统临时目录，构造「典型 / 上限」两档规模。
- ⚠️ **改预算 / 阈值类配置后必须搜「写死旧数值的测试」**：第十八轮把 `bid_analysis_segment_budget` 提到 400000 后，`test_parse_truncation_chain.py` 里写死 `31000/20000` 的用例**恒假失败**——护栏静默失效还会掩盖该修复。正确做法是按 `模块常量` 推导构造规模。
- 前端：`frontend/src/tests/`（`*.test.ts(x)`），运行 `npm run test`（= `vitest run`）；2026-09-30 第十一轮实测基线 **52 个文件 / 795 用例**全通过（此前记录的 51 文件 / 781 用例已过期）。`vitest.config.ts` 默认 `environment: "node"`，需 DOM 的用例在文件头声明 `// @vitest-environment jsdom`。
  - ⚠️ 本仓前端**未装 `@types/node`**：在 `.ts`/`.tsx` 测试里 `import ... from "fs"` / `"path"` / 用 `__dirname` 都会让 `npx tsc --noEmit` 报 TS2307/TS2304 而**构建失败**。需要跨语言（如比对后端 Python 常量）的一致性断言，请放到 pytest 侧读盘。
  - ⚠️ 页面级用例若依赖 `/ai/stats` 等**模块级 TTL 缓存**（`AIConfigPage` 的 `statsCache` 是模块单例），
    跨用例会互相污染（上一个用例缓存的 `null` 会让下一个用例永远拿不到 mock 数据）；
    需要「按 mock 数据渲染」时在用例里 `vi.mock("../utils/ttlCache", ...)` 换成永不命中的空实现。
  - ⚠️ **路由函数直接调用时不要用 `Query(...)` 作默认值**：`Query` 是 FastAPI 的
    参数标记，单测绕过框架直接调函数时拿到的是 `Query` 对象本身而非默认值。
    第十一轮 `list_analysis_items(domain: str = "scheme")` 即按此写（纯 str 默认值
    既是合法 query 参数默认值、又能在单测里直接调用）。
- 前端类型检查：`npx tsc --noEmit`（`npm run build` 内已含）必须为 0 错误。
- **解析提取模块专项（2026-09-30 第十一轮）**：`python -m pytest tests/test_bid_response_domain_20260930.py -q`（**73 passed**）。覆盖域注册表零交集与分组全覆盖、scheme 域主键旧格式与 `domain` 列幂等迁移、AST 禁止裸主键拼接、缺失标注规范单一出口（只追加一次）、技术评分项四向专判、均分分段（不丢内容/不尾段塌缩/不切围栏/代理对保护/默认与旧滑动窗口逐字一致）、断点续跑（force_rerun 与 item 恒忽略 / 查库异常 fail-open / 域隔离）、路由契约（加法式键 / 未知域 fail-closed / 未启用域 404）、下游跨域消费顺序。已做 **6 项 A/B 反向验证**（分别 3/1/3/1/2/2 例定向失败）。
- AI 配置模块专项：`python -m pytest tests/test_ai_config_module.py tests/test_ai_config_request_mode.py tests/test_ai_config_security_routing.py tests/test_ai_config_env_rollback.py tests/test_ai_config_gapfix_20260925.py tests/test_api_contract.py tests/test_provider_factory.py tests/test_crypto.py -q`（2026-09-26 基线 **274 passed**）。
- **提示词模块专项（2026-09-27 更新）**：`python -m pytest tests/test_prompt_governance.py tests/test_prompt_rollback_governance.py tests/test_prompt_shared_variables.py tests/test_prompt_variables.py tests/test_prompt_variables_r6.py tests/test_prompt_cache_db_path.py tests/test_prompt_cache_external_write.py tests/test_prompt_module_regression_20260925.py tests/test_prompt_module_fixes_20260927.py tests/test_content_deep_audit_20260923_b.py tests/test_four_basis_inputs_20260923.py tests/test_outline_generation_fixes_20260926.py -q`（2026-09-27 基线 **300 passed**，其中新增 `test_prompt_module_fixes_20260927.py` 63 例）。
- **图表修复提示词覆盖矩阵专项（2026-09-27）**：`python -m pytest tests/test_chart_prompt_coverage_20260927.py -q`（**31 passed**；7 类 × 2 格式 parity）。
- **全局事实九大章节分类专项（2026-09-24）**：`python -m pytest tests/test_facts_classification.py tests/test_scheme_classification.py tests/test_global_facts_routes.py tests/test_global_facts_field_completeness.py -q`。
- **全局事实遗留项收口专项（2026-09-29 第七轮）**：`python -m pytest tests/test_facts_content_fixes_20260930.py tests/test_facts_content_audit_20260929.py tests/test_sections.py tests/test_content_generation_g12.py tests/test_export_cache_unique.py -q`（**104 passed / 8.3s** = 第七轮新增护栏 36 + 第六轮审计 23 + 相关回归 45）。覆盖 `safe_rowcount` 契约与「禁止裸 `.rowcount`」静态扫仓、`classify_fact_attr` 死参数防回退（`inspect.signature` + AST）、四维派生单一口径、`facts_stale` 早/等/晚三向判定与跨格式时间归一化、轮询瘦身仍带回标记、`treeFingerprint` 跨语言 parity。
- **图表三侧口径专项（2026-09-27）**：`python -m pytest tests/test_chart_json_registration_parity_20260927.py tests/test_inline_charts.py tests/test_chart_fence_consistency.py tests/test_chart_edge_parsing.py -q`（基线 **83 passed**）。
- **解析提取 + 目录生成双模块专项（2026-09-29 第五轮）**：`python -m pytest tests/test_import_outline_fixes_20260929.py tests/test_import_outline_deep_fix_20260927.py tests/test_sections.py tests/test_sections_reset.py tests/test_doc_pipeline.py tests/test_doc_pipeline_status_vocab.py tests/test_bid_analysis.py tests/test_bid_analysis_evidence.py tests/test_bid_section_context.py tests/test_bid_section_detector.py tests/test_bid_section_extraction.py tests/test_file_import_pipeline.py tests/test_import_module_fixes_20260925.py tests/test_import_module_hardening_20260923.py tests/test_parse_truncated_persist.py tests/test_parse_truncation_chain.py tests/test_upload_format_contract.py tests/test_outline_generation.py tests/test_outline_generation_fixes_20260926.py tests/test_outline_deep_audit.py tests/test_outline_deep_audit_20260925.py tests/test_outline_fixes.py tests/test_outline_guard_endpoints.py tests/test_adjust_outline.py tests/test_upload_outline_reorganize.py tests/test_upload_parse_module.py tests/test_numbering_unification.py tests/test_numbering_consistency_validator.py -q`（**675 passed** = 本轮新增护栏 64 + 相关回归 611）。
- ⚠️ **不要在测试运行期间编辑被测源码**：全量 pytest 需跑 3~4 分钟，若期间改动
  `routers/charts.py` 等文件，函数行号会整体位移，而
  `tests/test_e2e_chain_hardening.py::test_generate_ai_image_does_not_call_get_on_row`
  用 `inspect.getsource()` 做**源码文本断言**（行号在导入时固化、读取时是新的），
  会切出错误片段而**假失败**（该用例单独跑必通过）。判据：单测单独跑通过、
  仅全量跑失败 → 先怀疑"边跑边改"，重跑确认再下结论。
- 图表子项目：`cd chart-gap-filler && python -m pytest tests/ -x -q`。
- 每次提交前必须跑相关测试；跑 pytest 尽量在 dev server 停止时进行，并带 `--reload-exclude`（见 §3.3、§5.6）。

## 7. 变更记录模板

提交 AI 修复时，PR 描述需包含：

```
【变更】<文件路径>
【背景】<BUG ID 或现象描述>
【根因】<一句话>
【修复】<一句话>
【默认行为】向后兼容 / 需显式开启
【验证】pytest/vitest 命令 + 通过数
【影响面】列出涉及的其他模块
```
