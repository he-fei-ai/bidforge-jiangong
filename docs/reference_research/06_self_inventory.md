# 06 · 本仓（专项方案工具箱）现状盘点报告

> 生成日期：2026-10-01 · 角色：资深架构师（只读盘点）
> 依据：`AGENTS.md`（工作边界）、`docs/OpenBidKit六模块解析与比对.md`、`docs/reference_gap_analysis_20260930.md` + 全量代码实读
> 代码基线：`git log` HEAD = `e128ca4`（master），工作区有 **108 个文件未提交改动**
> ⚠️ **AGENTS.md 仅记录到第十六轮（§4.20），工作区改动尚无文档记录** —— 详见末节「未提交改动的文档缺口」
> 姊妹篇：01_parse_extract / 02_outline / 03_global_facts / 04_content / 05_review_export_knowledge

---

## 0. 先说三处「文档与代码不同步」的事实（影响后续吸收参考软件）

| 事实 | 证据 | 影响 |
|---|---|---|
| **工作区有第 17 轮未记录改动** | `git status --porcelain` 显示 108 个 `M` + 大量 `??`；`backend/tests/test_seven_module_fixes_20261001.py`、`test_seven_module_batch2_20261001.py`、`test_export_pdf_cache_d2_20261001.py`、`test_export_appendix_d3_20261001.py`、`test_image_budget_g2_20260930.py` | G2（maxAiImages 分段择优）与 D2/D3（PDF 缓存指纹 + 导出附录）**已落地但 AGENTS.md 无记录** |
| **scheme 域提取项已是 19 项**（非 18） | `backend/app/services/bid_analysis_service.py:122-131` 新增 `techScoring`；实测 `len(ANALYSIS_ITEMS)==19`、`len(GROUPS)==14` | 一切「18 项」表述需更新 |
| **测试基线数字已过期** | `backend/tests/` 实测 **213 个 `test_*.py`**（AGENTS.md §2/§6 记 135）；前端 `src/tests/` 实测 **52 文件** | 需重跑全量取实测数 |

---

## 1. 六模块核心实现文件与关键函数清单

### ① 解析提取

| 核心文件 | 关键入口 / 关键函数（行号） |
|---|---|
| `services/file_parser.py`(85KB) | `parse_file_content:228` / `parse_file_content_ex:238`、`_resolve_pdf_max_pages:161`、`MAX_PDF_PAGES:201`、`MAX_CSV_ROWS:206` / `MAX_EXCEL_ROWS:207`、`_note_truncation:365`、`dump_parse_warnings:396`、`_guard_zip_archive:419`、`normalize_pdf_text:811`、`has_informative_text:773` |
| `routers/bid_analysis.py` | `start_bid_analysis:808` / `start_bid_analysis_sse:869` / `_start_sse_inner:896`、`_run_single_item:1459`、`_run_single_call:1574`、`_repair_json:1621`、`_split_tender_text:93`、`_cfg_int:102`、`_item_concurrency:119` / `_segment_concurrency:124` / `_item_retries:129`、`_combine_doc_texts:1835` / `_combine_doc_texts_report:1900`、`_invalidate_downstream_cache:2023`、`_invalidate_extraction_derived:2084`、`_reset_items_for_rerun:2129`、`clear_interrupted_items:2184`、`_resolve_classification_hint:2307`、`classify:2341` |
| `services/bid_analysis_service.py`(98KB) | 见第 2 节 |
| `services/doc_pipeline/pipeline.py` | `ingest_upload:122`、`ingest_parse_result:175`、`build_completeness_report:331`、`compute_freshness:480`、`sync_extract_layer:518`、`_merge_item_contents:673`、`detect_cross_source_conflicts:765`、`run_cross_check:827`、`purge_document:931`、`backfill_document:944` |
| `routers/doc_pipeline.py` | `document_status:64`、`get_extractions:123`、`get_chunks:164`、`reparse_document:218`、`document_completeness:258`、`document_freshness:295`、`sync_extractions:326`、`cross_check:335`、`project_documents_index:347` |
| 标段辅助 | `services/bid_section_detector.py`、`bid_section_context.py`、`bid_section_extraction.py` |
| 解析文档端点 | `routers/global_facts.py`：`upload_documents:2281`、`parse_document:2680`、`_reconcile_parse_status:2559` / `_all:2601`、`_mark_project_facts_stale:2643` |

### ② 目录生成

| 核心文件 | 关键入口 / 关键函数（行号） |
|---|---|
| `routers/sse_handlers.py`（核心大文件） | `generate_outline:3646`；校验器 `_validate_outline:1947`、`_outline_validate_fn:2019`、`_outline_fix_validate_fn:2029`、`_sublevel_validate_fn:2040`、`_level1_validate_fn:2066`、`_sublevel_batch_validate_fn:2087`、`_outline_patch_validate_fn:2109`；编排 `_outline_skeleton:2124`、`_build_partial_preview:2261`、`_merge_unit_results:2795`、`_split_requirement_items:2938`、`_check_requirements_coverage:2981`、`_restore_descriptions:3065`、`_try_outline_patch:3165`、`_review_and_fix_outline:3207`；上下文 `_build_structured_brief:3426`、`_outline_input_audit:3489`、`_scheme_basis_obj:3509`、`_scheme_is_dangerous:3525`、`_outline_construction_scope:3548`、`_outline_scheme_basis:3579`、`_outline_standards_text:3629`；**危大必备 10 章 `_DANGEROUS_REQUIRED_KEYWORDS`（约 `:2921`）** |
| `routers/sections.py` | `_build_tree:64`、`create_section:418`、`update_section:490`、`delete_section:702`、`reset_content:751`、`_save_outline_to_db:791`、`save_outline:1067`、`adjust_outline:1179`、`renumber_sections_after_reorder:1244`、`_renormalize_all_section_contents:1262`、`numbering-consistency` 端点 `:1332/:1342/:1352/:1359` |
| 领域服务 | `scheme_basis.py`（`parse_scheme_basis:294`）、`scheme_scope.py`（`extract_construction_scope:40`）、`scheme_classification.py`、`outline_templates.py`(162KB)、`outline_utils.py`、`outline_reference.py`、`outline_reorganize.py`、`outline_quality.py`、`numbering.py` |
| 路由 | `routers/outline_library.py`（`library_stats:59`、`list_standard_templates:105`、`create_from_scheme:394`、`export_library:511`、`apply_to_scheme:572`、`apply_library_and_save:592`）、`routers/upload_outline.py` |

### ③ 全局事实

| 核心文件 | 关键入口 / 关键函数（行号） |
|---|---|
| `services/facts_extractor.py`(134KB) | `FactItem:110` / `FactGroup:221` / `ExtractionResult:237`、`resolve_chunk_size:667`、`split_into_chunks:705`、`_parse_fact_dict:1060`、`apply_norm_dicts:1274`、`merge_and_deduplicate:1302`、`group_facts:1349`、`apply_category_auto_classify:1816`、`ensure_schedule_fact:1879`、`run_post_extract_normalize:1980`、`format_for_frontend:2022`、**`get_facts_inject_where:2529`**、`build_injectable_facts_query:2553`、`resolve_scheme_project_id:2543` |
| `services/facts_classification.py` | `classify_chapter_from_text:223`、`classify_fact_attr:306`、`classify_source_kind:373`、`shared_chapters_for:431`、`extract_danger_params:506`、`danger_check:550`、`classify_fact_dimensions:591`、`apply_fact_dimensions:618`、`dimensions_for_row:646`、`chapter_of_row:677`、`chapter_field_completeness:693`、`nine_chapter_summary:760`、`category_map_payload:802` |
| `services/facts_patches.py`(27KB) | 23 个顶层纯函数（易标 parity）：`normalize_fact_id` / `ensure_unique_id` / `value_to_markdown` / `build_missing_value_rule` / `build_completeness_rules` / `normalize_patches_response` / `validate_patches_response` / `merge_fact_patches` / `apply_patches_to_fact_items` / `batch_rendered_items` / `wait_all_or_throw` / `get_segment_limit` / `normalize_missing_value_mode` |
| `services/facts_enrich.py` | `apply_knowledge_patches:109`、`_build_patches:153`、`apply_finalize_result:201`、`finalize_facts:241` |
| 其它 | `facts_cross_validators.py`(20KB)、`conflict_arbiter.py`、`input_coverage.py`、`placeholder_inventory.py` |
| 路由 `routers/global_facts.py` | `_fact_dimension_fields:154`、`_load_fact_rows:318`、`list_facts:367`、`list_fact_categories:655`、`get_fact_category_map:670`、`list_facts_by_chapters:681`、`check_danger_scheme:759`、`create_fact:828`、`_apply_item_updates:955`、`update_fact:1134`、`resolve_fact:1411`、`resolve_conflict:1450`、`clear_all_facts:1516`、`batch_resolve:1571`、`adjust_facts:1798`、`delete_fact:1903`、`load_parsed_docs:1987` |
| SSE `sse_handlers.py` | `generate_facts:6384`、`_project_doc_diag_cols:6282`、`_load_facts_source_docs:6297`、`_count_docs_pending_parse:6365`、`_load_facts_rows:1384`、`_build_facts_text:1480`、`_render_facts_text:1271`、`_filter_facts_rows:1223`、`_row_chapter:1257`、`_rank_facts_by_basis:952` |

### ④ 正文生成

| 核心文件 | 关键入口 / 关键函数（行号） |
|---|---|
| `routers/sse_handlers.py` | `generate_content:4368`、`_persist_section`（落库路径，§4.14 事故点）、流内 `gen_one` / `guarded_gen`、`_apply_word_budget_allocations:1642`、`_build_parent_chain:1586`、`_split_md_sections:1012`、`_allocate_char_budgets:1033`、`_dedup_continuation:1148`、`_load_knowledge_rows:1506` / `_build_knowledge_text:1542`、`_should_auto_shrink:505`、`_await_with_stats:522` |
| `services/content_*.py` | `content_runtime.build_chapter_user_content`；`content_standard.py`（`resolve_effective_standard:45`、`build_system_block:110`、`build_user_block:115`、`standard_report:339`，`precise`/`fuzzy` 双标准）；`content_utils.py`（`max_tokens_for_budget:38`、`normalize_word_budget_override:65`、`text_word_count:106`、`fence_spans:148`、`strip_fenced_code_blocks:206`、`find_unclosed_fences:260`、`auto_fix_unclosed_fences:322`、`word_status_for:388`、`resolve_concurrency:408`、`leaf_word_budget:438`、`order_sections_dfs:455`、`select_target_leaves:493`、`build_sibling_context:566`）；`content_shrink.py` / `content_rewrite.py` / `content_polish.py` / `content_progress.py` / `content_blocks.py`(51KB) |
| `services/numbering.py` | `strip_outline_numbering:86`、`renumber_outline_nodes:113`、`renumber_section_outline_ids:160`、`renumber_section_body_subheadings:261`、`load_scheme_section_index:401`、`normalize_section_content_subheadings:482`、`validate_section_content_numbering:559`、`validate_scheme_numbering_consistency:599`、`repair_scheme_numbering_consistency:630`、`rollback_numbering_version:725` |

### ⑤ 审核预检

| 核心文件 | 关键入口 / 关键函数（行号） |
|---|---|
| `services/audit_rules.py`(35KB) | `ALL_RULES:398`（**实测 45 条**）、`DIMENSIONS:129`（六维）、`RULE_MAP:403`、`SEVERITY_PENALTY:147`、`GRADE_BANDS:150`、`grade_of:158`、`EXPERT_ITEM_RULES:460`（10 项）、`expert_items:476`、`_PROGRAM_EMITTED_RULE_IDS:496`（33 条）、`validate_rule_registry:539` |
| `services/preflight_engine.py` | `check_completeness:217`、`check_standards:267`、`check_safety:326`、`check_consistency:385`、`_find_duplicates:446`、`check_traceability:474`、`check_deliverability:512`、`run_preflight:606`、`preflight_stats:673` |
| `services/review_autofix.py`(40KB) | `capability_of:294`、`capability_summary:302`、`_locate_value:383`、`_locate_term:417`、`_locate_standard:440`、`_locate_first_hit:461`、`FIX_MODE_AUTO/AI/MANUAL:82-84`、`_CAPABILITY:120` |
| `services/consistency_scanner.py` | `build_global_facts_text:89`、`build_project_docs_text:99`、`build_standards_text:125`、`load_leaf_sections:133`、`program_prescan:155`、`ai_scan_section:219`、`ai_scan_batch:294`、`chunk_sections_for_scan:327`、`merge_conflicts:448`、`persist_conflicts:536`、`run_scan:559` |
| `services/repair_agent.py` | `filter_conflicts:78`、`group_by_section:98`、`repair_section:130`、`load_repaired_marks:183`、`filter_already_repaired:214`、`limit_repair_sections:248`、`run_repair:261` |
| `services/repair_validator.py` | `structure_signature:25`、`validate_repair:36`（`MIN_LEN_RATIO=0.85` / `MAX_LEN_RATIO=1.15`） |
| `services/audit_scoring.py` / `audit_service.py` | 六维加权评分与留痕 |
| 路由 | `routers/compliance.py`（`list_rules:83`、`get_expert_items:102`、`compliance_check:111`、`expert_review:240`、`get_results:309`、`run_consistency_audit:344`、`latest_consistency_audit:413`、`readiness_overview:621`、`_readiness_overview_compute:660`、`list_preflight_runs:886`、`readiness_report:920`）；`routers/review.py`（`review_summary:178`、`review_checklist:235`、`batch_review_sections:280`、`review_section:363`、`submit_scheme_review:405`、`review_records:519`）；`routers/review_autofix.py`（`capabilities:80`、`plan:97`、`apply:175`、`rollback:210`、`list_repairs:243`） |

### ⑥ 导出文档

| 核心文件 | 关键入口 / 关键函数（行号） |
|---|---|
| `routers/export.py`(最大路由) | `export_check:670`、`placeholder_report:696`、`placeholder_rerun_plan:713`、`placeholder_history:768`、`collect_export_issues:791`、`_EXPORT_ISSUE_RULE_MAP:1041`、`export_issues_to_findings:1067`、`_readiness_preflight_summary:1141`、presets 端点 `:1217-1284`、`_get_or_add_appendix_heading_style:2538`、`_load_appendix_sources:3814`、`export_docx:4079`、`export_pdf:4371`、`audit_content:224`、`_detect_duplicate_sections:308`、`_detect_section_number_mismatch:417`、`_detect_stale_cross_references:537`、`_add_inline_chart_from_bytes:2014`、`_add_illustration_from_bytes:2070`、`_add_omml_formula:1523`、`_add_cover_info_table:1749`、`_fit_image_cm:1866`、`_auto_generate_ai_image_blocks` |
| `services/docx_math.py`(29KB) | OMML 公式转换 |
| `services/chart_payload.py` | `build_chart_envelope` / `extract_chart_payload`（唯一构造器/解析器，兼容全部历史形状） |
| 提示词/图表 | `services/ai/mermaid_renderer.py`、`mermaid_*.py`(7 类)、`image_engine.py`、`image_providers/*`(8 家) |

---

## 2. 提取项 `ANALYSIS_ITEMS` —— **实测 19 项**（非 18）

**唯一事实源**：`backend/app/services/bid_analysis_service.py:39`（`scheme` 域，恒启用）
**域注册表**：`:283` `EXTRACTION_DOMAINS = {"scheme": ANALYSIS_ITEMS, "bid_response": BID_RESPONSE_ITEMS}`
**分组**：`:321` `GROUPS`（**实测 14 组**）

| # | item_id | 名称(label) | 类型 | 必选 | sort | 分组 | 行号 |
|---|---|---|---|---|---|---|---|
| 1 | `projectBasicInfo` | 项目级基本信息 | json | ✅ | 1 | project_info | `:41` |
| 2 | `schemeBasicInfo` | 方案级基本信息 | markdown | ✅ | 2 | scheme_info | `:45` |
| 3 | `overviewParams` | 工程概况与设计参数 | markdown | ✅ | 3 | overview_params | `:49` |
| 4 | `compilationBasis` | 编制依据 | markdown | ✅ | 4 | basis | `:53` |
| 5 | `siteConditions` | 施工条件与环境 | markdown | ✅ | 5 | condition | `:57` |
| 6 | `deploymentSchedule` | 施工部署与进度 | markdown | ✅ | 6 | deployment | `:61` |
| 7 | `constructionTechnique` | 施工工艺与技术 | markdown | ✅ | 7 | technique | `:65` |
| 8 | `resourceAllocation` | 资源配置 | markdown | ❌ | 8 | resource | `:69` |
| 9 | `safetyMeasures` | 安全保证措施 | markdown | ✅ | 9 | safety | `:73` |
| 10 | `qualityAcceptance` | 质量管理与验收 | markdown | ✅ | 10 | quality | `:77` |
| 11 | `emergencyResponse` | 应急处置 | markdown | ✅ | 11 | emergency | `:81` |
| 12 | `calcAndDrawings` | 计算书与图纸 | markdown | ✅ | 12 | calc_drawing | `:85` |
| 13 | `materialManagement` | 材料管理 | markdown | ✅ | 13 | construction | `:94` |
| 14 | `equipmentManagement` | 机械管理 | markdown | ✅ | 14 | construction | `:98` |
| 15 | `constructionDeployment` | 施工部署 | markdown | ✅ | 15 | construction | `:102` |
| 16 | `constructionProcess` | 施工流程 | markdown | ✅ | 16 | construction | `:106` |
| 17 | `workInterfaceDivision` | 工作界面划分 | markdown | ✅ | 17 | construction | `:110` |
| 18 | `engineeringMethods` | 工程做法 | markdown | ✅ | 18 | construction | `:114` |
| 19 | **`techScoring`** | **技术评分要求** | **json**（4 字段） | ❌ | **19** | **scoring** | `:122` |

- 必选 **18 / 19**，可选 1（`resourceAllocation`、`techScoring`）；`REQUIRED_ITEM_IDS:318`。
- 关键出口：`build_item_pk:880`、`get_item_domain:861`、`get_items_by_domain:870`、`get_item_fields:945`、`build_json_template:957`、`build_task_prompt:969`（缺失标注规范唯一出口）、`is_missing_result:1589`、`is_missing_technical_score_items:390`、`build_system_prompt:1040` / **`build_system_messages:1060`**（第十五轮新增加法式）。
- 三态缺失语义：`MARKDOWN_MISSING_RESULT="未提取到"`(`:341`)、`PARTIAL_MISSING_TEXT="没有提及"`(`:354`)、JSON 失败哨兵 `{}`（`_json_all_empty:1540`）。
- 分段策略：`split_for_analysis:1373` / `_split_even:1309` / `_can_use_candidate:1174`（均分分段，默认 `bid_analysis_segment_even=False`）。
- 下游上下文：`format_downstream_context:1423` / `_apply_downstream_budget:1493`（12k 总量 / 2k 单项）。
- 另一域 `BID_RESPONSE_ITEMS:146`（**18 项 / 6 必选 / 11 组**，开关 `bid_response_domain_enabled` 默认 `False`），与 scheme 域 `item_id` **零交集**（实测交集为空集）。

---

## 3. 全局事实数据结构 + 九大章节 + 危大六大类 + 四维标注

### 3.1 数据结构 `FactItem`（`facts_extractor.py:110`）
字段组：`name / value(str|list) / key / category / source / source_ref / is_simulated / confidence / has_conflict / conflict_values`；扩展（研究报告 v2.0）`value_unit / fact_type / evidence_kind / page_ref / zone_type / is_safety_critical / norm_group / chunk_hash`；四维 `chapter / fact_attr / source_kind / is_shared`。落库见同文件内 `to_db_row`。
- 22 类 `category` 分组标题单一事实源：`CATEGORY_TITLES:1167`（如 `safety_critical:"安全关键参数"`、`risk:"风险与危大工程"`），排序 `_CATEGORY_ORDER:1194`。
- 21 类 `fact_type` 枚举定义在提示词 `services/ai/prompts/analysis.py:81`（P7~P20 逐类示例；`:223` P20「风险与危大工程」）。
- 安全关键项禁模拟：`_filter_safety_sensitive:1203` / `is_safety_critical_name:1236` / `SAFETY_CRITICAL_MAX_SIMULATED=0`(`:50`)。

### 3.2 九大章节体系（`scheme_classification.NINE_CHAPTERS:353`，实测 9 条）
| chapter | key | **title 原文** | source_items |
|---|---|---|---|
| 1 | `overview` | **工程概况** | projectBasicInfo, overviewParams |
| 2 | `basis` | **编制依据** | compilationBasis |
| 3 | `plan` | **施工计划** | deploymentSchedule, resourceAllocation |
| 4 | `technique` | **施工工艺技术** | constructionTechnique |
| 5 | `safety` | **施工安全保证措施** | safetyMeasures |
| 6 | `personnel` | **施工管理及作业人员配备和分工** | resourceAllocation, projectBasicInfo |
| 7 | `acceptance` | **验收要求** | qualityAcceptance |
| 8 | `emergency` | **应急处置措施** | emergencyResponse |
| 9 | `calc_drawings` | **计算书及相关施工图纸** | calcAndDrawings |

每章含 `base_fields` + `category_fields`（按 6 大危大类别追加，`:359-366`）。
⚠️ **双口径**：目录生成侧另有一套「危大必备 **10** 章」（`sse_handlers._DANGEROUS_REQUIRED_KEYWORDS`，约 `:2921`）= 九章 + **监测方案**，与 `NINE_CHAPTERS` 的 9 章**不是同一份数据**。

### 3.3 危大工程六大类（`HAZARD_CATEGORIES:39`，实测 6 类 / 20 子类）
| 大类 id / 名称 | 子类 id |
|---|---|
| `foundation_pit` 基坑工程 | `fp_support_drain` 基坑支护与降水工程 / `fp_earthwork` 土方开挖工程 |
| `formwork` 模板工程及支撑体系 | `fw_support` 模板支撑体系工程 / `fw_tall` 高大模板工程 |
| `hoisting` 起重吊装及起重机械安装拆卸工程 | `ho_lift` 起重吊装工程 / `ho_crane` 起重机械安装拆卸工程 |
| `scaffold` 脚手架工程 | `sc_ground` 落地式钢管脚手架（高度>24m）/ `sc_attached` 附着式升降脚手架 / `sc_cantilever` 悬挑式脚手架 / `sc_other` 门型·挂·吊篮·卸料平台 |
| `demolition` 拆除、爆破工程 | `dm_manual` 人工拆除 / `dm_machine` 机械拆除 / `dm_blast` 爆破拆除 |
| `other` 其他危大工程 | `ot_curtain` 建筑幕墙安装 / `ot_steel` 钢结构（网架、索膜结构）安装 / `ot_prestress` 预应力结构张拉 / `ot_underground` 地下暗挖·顶管·水下作业 / `ot_slope` 6m 以上边坡施工 / `ot_confined` 有限空间作业（2026-09-26 补）/ `ot_newtech` 四新 |

### 3.4 `HAZARD_THRESHOLDS` 完整表（`:220`，**实测 14 个键**，全表 `>=` 闭区间）
单位约定（`:215`）：depth/height/span → m；total_load → kN/m²；line_load → kN/m；single_weight → kN；crane_capacity → kN；crane_height → m。

| key | params | hazard_when | oversize_when | 行号 |
|---|---|---|---|---|
| `fp_support_drain` | depth | ≥3 | ≥5 | `:227` |
| `fp_earthwork` | depth | ≥3 | ≥5 | `:233` |
| `fw_support` | height,span,total_load,line_load | ≥5 / ≥10 / ≥10 / ≥15 | ≥8 / ≥18 / ≥15 / ≥20 | `:240` |
| `fw_tall` | 同上 | ≥8 / ≥18 / ≥15 / ≥20 | 同 hazard（附件二即超规模） | `:254` |
| `ho_lift` | single_weight | ≥10 | ≥100 | `:263` |
| `ho_crane` | crane_capacity,crane_height | `hazard_always=True` | ≥300 或 ≥200 | `:276` |
| `sc_ground` | height | ≥24 | ≥50 | `:291` |
| `sc_attached` | — | 空（本身即危大） | 空 | `:297` |
| `sc_cantilever` | — | 空 | 空 | `:303` |
| `sc_other` | — | 空 | 空 | `:309` |
| `ot_curtain` | install_height | ≥50 | ≥50 | `:316` |
| `ot_steel` | span | ≥36 | ≥36 | `:322` |
| `ot_prestress` | — | 空 | 空 | `:328` |
| `ot_slope` | slope_height | ≥6 | ≥6 | `:334` |

判定函数：`evaluate_hazard_level:522`（缺参**保守判危大**、参数缺失列入 `missing_params`）、`_check_oversize:507`、`_OPS:498`；`classify_scheme:634`、`classify_scheme_name:486`、`match_category_keywords:454`、`format_classification_hint:703`；类别→标准键 `CATEGORY_STANDARDS_KEYS:433`。
⚠️ **8 个子类不区分危大与超规模**：`hazard_when` 为空或 `threshold=None`（拆除 3 + 附着/悬挑/门型 3 + 暗挖 + 有限空间 + 四新 + 预应力）时，`evaluate_hazard_level:554-564` 直接令 `is_oversize == is_hazardous`。

### 3.5 四维标注（`facts_classification.py`）
- `chapter`：`CHAPTER_TEXT_RULES:131`（**实测 9 条**，顺序即优先级，`calc_drawings` 优先）+ `FACT_KEY_TO_CHAPTER:195`（**实测 24 个英文归一化键**）+ `classify_chapter_from_text:223`
- `fact_attr`：`FACT_ATTR_TITLES:265` + `classify_fact_attr:306`（quantitative / qualitative / relation / norm）
- `source_kind`：`SOURCE_KIND_TITLES:342` + `SOURCE_KIND_RULES:352` + `DEFAULT_SOURCE_KIND="bid_doc":361`
- `is_shared`：`shared_chapters_for:431`（`_chapter_order:445` 排序）
- 落库列：`DIMENSION_COLUMNS:578` = `("chapter","fact_attr","source_kind","is_shared")`；`global_facts` 表由 `db.py::_migrate` 幂等补列 + `idx_global_facts_chapter`
- 派生链：写侧 `apply_fact_dimensions:618`（幂等，不覆盖已有值）；读侧 `dimensions_for_row:646`（惰性派生不写库）→ `routers/global_facts.py::_fact_dimension_fields:154`
- 开关：`facts_chapter_classification`（默认 **True**，`config.py:449`）；正文注入 `facts_chapter_inject`（默认 **False**，`config.py:455`）
- 危大参数抽取：`DANGER_PARAM_RULES:461` / `_LENGTH_PARAMS:478` / `extract_danger_params:506` / `danger_check:550`

---

## 4. 事实注入门控 `get_facts_inject_where` 的四条件与全部调用点

**唯一出口**：`facts_extractor.py:2529`
**主常量**：`:2512` `_FACTS_INJECT_WHERE = "has_conflict=0 AND is_resolved=1 AND is_simulated=0 AND is_stale=0"`
**fail-closed 兜底**：`:2524` `FACTS_INJECT_WHERE_FALLBACK`（与主口径**逐字相同**，故意不放宽）

| # | 调用点 | 行号 | 用途 |
|---|---|---|---|
| 1 | `routers/global_facts.py` | `:355`（import `:354`） | `/global-facts` 列表（`injectable_only`） |
| 2 | `routers/global_facts.py` | `:795` 注释 | danger-check 危大阈值判定**排除编造值/过期值**（P0，见 `:325`） |
| 3 | `services/input_coverage.py` | `:161`（import `:159`） | 覆盖度台账；新增加法式状态 `filtered_stale` / `filtered_simulated` |
| 4 | `services/placeholder_inventory.py` | `:295`（import `:294`） | 占位符清单（曾因两条件口径分叉产生假「可重跑」） |
| 5（间接）| `routers/sse_handlers.py` | `:1448`（import `:1424`） | 经 `build_injectable_facts_query:2553` 构造 SQL（目录 + 正文共用） |
| 6（间接）| `routers/export.py` | `:3406`（import `:3402`） | 经同一 `build_injectable_facts_query` 构造 SQL（导出附录/清单） |
| 参考 | `export.py:930` 注释 | — | 明确 `is_resolved` 用**严格相等**（与旧 `COALESCE(...,1)=0` 口径相反） |

**护栏**：`tests/test_fact_gate_parity_20260930.py`（13 例）、`test_facts_inject_gate_failclosed_20260927.py`。

---

## 5. 提示词治理体系能力清单

**注册表**：`services/ai/prompts/_registry.py`（`_reg:153` → `_ALL_PROMPTS:20`），7 个分域文件；**实测模板总数 50**：analysis 20 / outline 10 / charts 10 / content 4 / 配图 3 / 审核 3。

| 能力 | 实现 | 行号 |
|---|---|---|
| 运行时缓存（DB `prompt_templates` 优先 + 硬编码兜底） | `prompts/_cache.py`（`_get_prompt_cache`、`reload_prompt_cache`） | — |
| 渲染（**逐行**渲染，注入的多行内容原样保留） | `render_prompt:326` / `_render_by_line:282` / `render:407` | `_registry.py` |
| 变量三口径 | `extract_variables:252`（含 SHARED）/ `extract_user_variables:266` / `validate_prompt_variables:223` | 同上 |
| 残留检测 / 清洗 | `has_residual_placeholders:277`、`clean_prompt_text:245`、`_ZERO_WIDTH_PATTERN:150` | 同上 |
| SHARED 片段 | `prompts/_shared.py`：**3 个** —— `SHARED_FORBIDDEN_WORDS:8`、`SHARED_OUTPUT_SPEC:14`、`SHARED_SCOPE_RULES:23`（后者 = **「专项施工方案 ≠ 投标文件」红线**）；解析 `_resolve_shared_keys`（含环检测 + `_SHARED_MAX_DEPTH=4`） | `_shared.py` / `_registry.py` |
| 变量契约 | `PROMPT_VARIABLE_CONTRACTS:471`（**实测 36 个模板**）+ 启动期双向校验 `check_prompt_variables:612`（`strict` 时抛 `PromptContractError:686`）+ `main.py:80` 启动调用 | |
| 保存期静态体检 | `validate_prompt_content:702`（`unknown_shared_ref` / `contract_var_removed` / `contract_var_added`，标签 `PROMPT_ISSUE_LABELS:695`） | |
| 上下文预算分配器 | `prompt_governance.allocate_context_budget:171`（`CONTEXT_PRIORITY:38`、`split_labeled_segments:83`、`apply_context_budget:272`）；入口 `sse_handlers._apply_prompt_context_budget:2491`；开关 `prompt_context_budget`（默认 0=关，`config.py:553`） | |
| 注入防护 | `guard_material:363` / `scan_prompt_injection:348` / `guard_external_segments:399` / `redact_sensitive:495`（`INJECTION_PATTERNS:327`）；入口 `sse_handlers._guard_external_material:2511`；开关 `prompt_injection_defense`（默认 False，`config.py:548`） | |
| 版本回滚 | `routers/prompts.py::_write_with_before`（`BEGIN IMMEDIATE` + 事务内重读 + CAS 冲突 409）；`audit_service.prompt_snapshot_is_rollbackable`；`GET/POST /api/v1/prompts/{key}/rollback`；开关 `prompt_audit_snapshot_enabled:377` / `prompt_audit_snapshot_max_chars:380` | |
| 写路径判空（R13） | `_fetch_content_row` 统一判空（写 503 / 读回退出厂清单） | `routers/prompts.py` |

---

## 6. AI 调用链与并发 / 熔断 / 降级

**链路**：`sse_handlers.collect_json_response` → `services/ai/provider_factory.chat_with_fallback`（信号量 + 熔断 + 配额冷却 + 对冲 + 降级链）→ `ai_config` 候选链 → `providers/openai_compatible.py` / `anthropic_compatible.py`

| 机制 | 实现 | 关键数值（`config.py`） |
|---|---|---|
| 自适应并发 | `workflows_base.AdaptiveConcurrencyController:257`（`initial=3,min=1,max=5`；降并**仅**由 429 / 失败率触发；`FAST=8s`/`SLOW=30s`/`WINDOW=20`/`MAX_DOWNGRADE_FROM_TARGET=2`；夹在 `[target-2, target]`） | `max_concurrency=5`（`:212`）、`outline_chapter_concurrency=2`（`:218`） |
| 可变信号量 | `ResizableSemaphore:15`（`set_value` 不丢等待者、`reject_waiters:41` 按任务隔离） | — |
| 全局实例 | `concurrency_controller:333` / `circuit_breaker:334`；`current_ai_task_id:10`（ContextVar） | |
| 熔断器 | `AnalysisCircuitBreaker:115`，CLOSED→OPEN→HALF_OPEN→CLOSED | `FAILURE_WINDOW=10.0`(`:131`)、`failure_threshold=5`、`cooldown_seconds=15`（429 加倍，上限 120s） |
| 配额冷却 | `_is_quota_error:1476` / `_quota_cooldown_active:1482` / `_note_quota_failure:1499` / `_note_quota_success:1506` | `ai_fail_cooldown_seconds=120`、`ai_fail_probe_every_seconds=10` |
| 降级链 | `_fallback_chain:1030`（按实时成功率排序/剔除；`_PROVIDER_RELIABILITY_WINDOW=50`、`_PROVIDER_MIN_SAMPLES=5`） | `ai_fallback_chain_max=3`、`ai_fallback_attempt_timeout=120`、`ai_provider_dead_success_rate=0.20`、`ai_provider_demote_success_rate=0.60` |
| 对冲 | `chat_with_fallback` | `ai_hedge_enabled=True`、`ai_hedge_delay_seconds=20` |
| 重试分级 | 分级 | `ai_retry_on_quota_error=False`、`ai_retry_on_non_retryable=True`、`ai_reasoning_max_tokens=4096`、`ai_retry_on_thinking_exhausted=True` |
| 超时 | — | `ai_request_timeout=900`、`content_request_timeout=300`、`content_total_timeout=660`、`content_section_retries=1`、`content_retry_backoff=6.0`、`content_rate_limit_backoff=20.0`、`sse_total_timeout_default=1800`/`hard_max=14400` |
| 并发（其它域） | — | `consistency_scan_concurrency=2`(`:285`)、`consistency_repair_concurrency=2`(`:291`)、`image_max_concurrency=2`(`:101`)、`bid_analysis_item_concurrency=2`(`:414`)/`segment_concurrency=3`(`:415`)/`item_retries=2`(`:416`)/`segment_budget=400000`(`:428`) |
| 参数区间单一事实源 | `provider_factory._RANGE:412` + `clamp_warnings:507` + `clamp_config_numbers:460` | 并发 1~5 / max tokens 256~200000 / 温度 0~2 / 超时 10~3600 |
| 场景路由 | `KNOWN_SCENES:1174` **实测 28 个**：outline 7 / content 4（含 `word_budget_alloc`）/ facts 4（`facts_knowledge_patch`、`facts_finalize`）/ bid 4 / consistency 3 / compliance 4 / chart·image 2；`resolve_scene_config:1262` | 双向漂移护栏 `TestKnownScenesDrift` |
| 多环境 | `resolve_active_env:868` / `RUNTIME_ACTIVE_ENV_KEY:793` / `resolve_disabled_providers:951` | `active_env`（`config.py:575`） |
| 缓存 | `invalidate_config_cache:762`、`_config_cache_ttl:689`、`_cache_generation:682` | `ai_config_cache_ttl=300`（`config.py:564`） |
| 审计 | `ai_audit_logs` 攒批落库（`_AUDIT_BATCH_SIZE=50`、`_AUDIT_FLUSH_INTERVAL=10.0`），`flush_audit_buffer` | — |

---

## 7. 图表管线

- **7 类唯一值域**：`flowchart / gantt / architecture / labor / comparison / layout / timeline`
  - `chart_validators.PIL_RENDERABLE_CHART_TYPES:1915`（**实测 7 类**）、`CHART_TYPE_LABELS:1868`、`MERMAID_KEYWORD_TO_CHART_TYPE:1887`、`TRIGGER_KEYWORDS:1388`（触发词）、`INDUSTRY_TERMS:1600`
- **登记 / 导出 / 预览三侧判据已收敛**（AGENTS.md §4.3 + 2026-09-27 修复）：
  - 登记：`routers/_chart_pipeline.py:210` `detect_mermaid_chart_type(code, default="")`；chart-json 分支 `:242` `infer_chart_type_from_payload(obj)`
  - 导出：`services/content_blocks.py:449` `detect_mermaid_chart_type(code, default="")`；`:495` `infer_chart_type_from_payload`
  - 预览：前端 `frontend/src/utils/chartTypes.ts`（只取显式且合法类型，否则传空串交后端推断；不再 `obj.type || "labor"` 凭空捏造）
  - 护栏：`tests/test_chart_json_registration_parity_20260927.py`(28) / `test_chart_prompt_parity_20260930.py`(30) / `chartTypesParity.test.tsx`(8)
- 登记管线：`_scan_chart_fences_full:193` → `_validate_inline_chart:473` → `build_inline_chart_plan:561` → `apply_inline_chart_plan:714` / `register_inline_charts:796` → `chart_predictions`；`_load_scheme_type_counts:532`
- 数量上限（`_chart_pipeline.py:94-96`）：每章同类型 **1**；全方案同类型 **3**；`ai_image` **6**
- 类型集合常量：`_ALL_CHART_TYPES:47` / `_JSON_CHART_TYPES:58` / `_JSON_VALIDATORS:70` / `_MERMAID_TYPE_MAP:86`
- 图题优先级：载荷 `title` > Mermaid `title` > 引导语 > 类型通用名（`export.py:111` v11）
- 渲染与修复：`services/ai/mermaid_renderer.py`、`image_engine.validate_mermaid:199` / `repair_mermaid:452`、`routers/charts.py`（`render_chart:48`、`list_chart_types:108`、`list_charts:132`、`fix_mermaid:228`）
- **自动生图**：`export.py::_auto_generate_ai_image_blocks`，开关 `ai_image_auto_generate`（默认 True，`export.py:123` v17 说明）；前端**无**人工生图入口，兼容端点 `POST /charts/generate-ai-image`（`charts.py:526`）在 `ai_image_manual_enabled=False`（`config.py:138`）时返 **409**
- **`[未入 AGENTS.md]` G2 已落地**：`config.max_ai_images:123`（默认 0=关闭）+ `image_engine.apply_image_budget:870` + `select_ai_image_codes:929`（跨章分段择优，避免前段耗尽额度），接线 `export._auto_generate_ai_image_blocks`；护栏 `tests/test_image_budget_g2_20260930.py`

---

## 8. 一致性修复 `consistency_edits` 定点替换能力现状

`backend/app/services/consistency_edits.py`（9.8KB，第十六轮新增，**已落地**）

| 能力 | 实现 | 行号 |
|---|---|---|
| 唯一命中定位（精确 → 折叠空白宽松两级） | `find_unique_span` | `:69` |
| 应用（顺序应用、拒绝即跳过、**不猜**） | `apply_unique_edits` → `EditResult{content, applied, rejected}` | `:118` / `:48` |
| 拒绝原因枚举 | `not_found` / `ambiguous` / `too_short` / `empty` | `:53` |
| 长度下限 | `MIN_OLD_TEXT_CHARS=8` | `:38` |
| 单次上限 | `MAX_EDITS=20` | `:42` |
| 结构校验 | `_validate_edits:161` | |
| 提示词组装 | `build_edits_prompt:171`（章节原文**后置**，`collect_repair_edits:209`） | |
| AI 封装（`chat_fn` **依赖注入**） | `collect_repair_edits`（`scene="consistency_repair"`、`temperature=0.1`、`timeout=120`；失败 fail-soft 返回 `applied=0`） | `:186` |
| 提示词 | `consistency_repair_edits_system` / `consistency_repair_edits_user` | `prompts/consistency_repair.py`（该文件 10 个 `_reg`） |
| 接线 | `repair_agent.repair_section:130` = **定点优先 + 整章重写兜底** | |
| 护栏 | `tests/test_reference_alignment_20260930.py`（33 例，含接线与降级 6 例） | |

⚠️ 仍保留的整章路径副作用：`/consistency/confirm` 拒绝**单条**冲突时，仍会把该章**整体**恢复修复前快照（同章其它已修好的冲突一起丢）—— `consistency_edits` 只解决了「整章重写」问题，**未解决「按条回滚」**。

---

## 9. 【重点】本仓「专项施工方案」领域特有业务概念清单

### 9.1 九大章节**确切名称原文**（`scheme_classification.py:353`，`NINE_CHAPTERS[i]["title"]`）
1. 工程概况 / 2. 编制依据 / 3. 施工计划 / 4. 施工工艺技术 / 5. 施工安全保证措施 / 6. 施工管理及作业人员配备和分工 / 7. 验收要求 / 8. 应急处置措施 / 9. 计算书及相关施工图纸
- 法定出处（`audit_rules.py:20-22` 引 37 号令第十七条）：①工程概况 ②编制依据 ③施工计划 ④施工工艺技术 ⑤安全保证措施 ⑥施工管理及作业人员配备和分工 ⑦验收要求 ⑧应急处置措施 ⑨计算书及相关图纸
- **本仓额外加的第 10 章 = 「监测方案」**（不在 31 号文九章内），仅存在于 `sse_handlers._DANGEROUS_REQUIRED_KEYWORDS` 与 `audit_rules.EXPERT_ITEM_RULES`（映射到 `SAF-06`）。

### 9.2 各类方案分类体系（**四套并存，语义不同**）

**(a) 六大类危大 + 20 子类**（法规分类）→ `scheme_classification.HAZARD_CATEGORIES:39`（见 3.3）。子类名称原文例：`sc_ground`「落地式钢管脚手架（高度>24m）」、`sc_other`「门型脚手架、挂脚手架、吊篮脚手架、卸料平台」、`ot_curtain`「建筑幕墙安装」、`ot_steel`「钢结构（网架、索膜结构）安装」、`ot_newtech`「采用新技术、新工艺、新材料且无技术标准的工程」。

**(b) 22 个标准目录模板 + 危大分级元信息**（`outline_templates.py`，161KB）
- `BUILDERS:2140` 22 个 builder；`RULES:2166` 关键词→模板；`TEMPLATE_META:2209`（**每模板三段**：`basis` 编制依据摘要 / `applicable` 适用条件 / `risk` 危大分级）；`_TEMPLATE_NAMES:2394` 中文名；`match_template:2360` / `build_outline:2370` / `get_meta:2377` / `list_templates:2384`
- 22 类：基坑与土方 / 模板与支撑 / 脚手架 / 起重吊装与机械安拆 / 施工现场临时用电 / 高处作业与临边防护 / 有限空间作业 / 临时设施与现场消防 / 装配式与钢结构 / 防水与渗漏治理 / 装饰装修与幕墙 / 机电安装与智能化 / 质量计划与创优 / 安全文明与风险管理 / 应急预案与处置 / 冬雨季与高温季节施工 / 绿色施工与环境保护 / 土方开挖与回填 / 砌体与二次结构 / 拆除工程 / 监测与变形观测 / 通用方案骨架
- `risk` 字段实际取值（代码内出现）：`危大工程` / `一般危大` / `专项监测` / `季节性` / `管理类` / `一般`
- 章节构造器：`_t_foundation_pit:175`、`_t_formwork:284`、`_t_scaffold:384`、`_t_lifting:479`、`_t_temp_electricity:574`、`_t_work_at_height:664`、`_t_confined_space:750`、`_t_fire_water:834`、`_t_prefabricated:920`、`_t_waterproof:1014`、`_t_decoration:1103`、`_t_mep:1195`、`_t_quality:1286`、`_t_safety_management:1372`、`_t_emergency:1458`、`_t_seasonal:1544`、`_t_green:1630`、`_t_earthwork:1722`、`_t_masonry:1806`、`_t_demolition:1896`、`_t_monitoring:1983`、`_t_general:2067`；共用构造器 `_basis_chapter:48`、`_plan_chapter:75`、`_personnel_chapter:99`、`_acceptance_chapter:121`、`_emergency_chapter:141`、`_calc_chapter:165`
- ⚠️ **映射靠 `RULES` 关键词而非声明式映射表**：`HAZARD_CATEGORIES` 的 `category_id` 与 `outline_templates` 的 `template_key` **无显式对应关系**（`hoisting` → `lifting`、`formwork` → `formwork` 均靠关键词命中）

**(c) 12 个一级业务分类 + 预置方案清单**（`seed_data.SCHEME_CATALOG:34`）
基坑与土方 / 模板与支撑 / 脚手架 / 起重吊装 / 临时设施 / 安全文明 / 质量与创优 / 防水与渗漏 / 装配式与结构 / 装饰装修 / 机电与智能化 / 应急与专项；每条附 `CATEGORY_PROFILE:136`（工程类型/专业）与 `_tags:152`（分类+方案名+risk+template）。`SEED_VERSION="v2.0"`(`:31`)。

**(d) 方案名称确定性解析（六维）**（`scheme_basis.parse_scheme_basis:294`，`SchemeBasis:209`）
`raw_name / core / scope_items / scheme_type / is_dangerous / hazard_hits / template_key / process_steps / techniques / objects`；三套字面关键词库 `PROCESS_KEYWORDS:58`（工序）、`TECHNIQUE_KEYWORDS:81`（工法）、`OBJECT_KEYWORDS:110`（对象/部位），`match_terms:161`（长度倒序 + 长词抑制短词）。
配套 `scheme_scope.extract_construction_scope:40`（复合名拆分：`_SPLIT_RE:25` = `、，,;/+&()|及|与|和|暨`）。
相关性工具：`text_keywords:183`（2-gram + ASCII≥3）、`relevance_score:201`、`rank_by_relevance:368`、`relevance_weights:411`。
开关：`outline_name_basis:501` / `outline_standards_inject:508` / `outline_basis_relevance:520` / `outline_relevance_boost:523` / `outline_name_coverage_check:528`（均默认 True）。

### 9.3 编制依据（**三重实现**）
1. **标准库**（`standards_registry.py`）：`STANDARD_DB_VERSION="2026.09"`(`:28`)、`STANDARD_DB_CHECKED_AT:29`、`Standard:32`（`code`/`name`/`mandatory`）、`BASE_STANDARDS:55`（7 条，含 `GB 55034-2022`/`GB 55032-2022` 等**全文强制**）、`CATEGORY_KEYWORDS:68`（**15 类**）、`CATEGORY_STANDARDS:86`（15 组标准清单）、`REGULATIONS:189`（**5 部法规**：住建部令 37 号、建办质〔2018〕31 号、国务院令 393 号、279 号、建质〔2008〕75 号）、**`ABOLISHED_STANDARDS:200`（20 条已废止/被替代标准）** + `find_abolished_codes:328`（正文禁引，生成后扫描告警）、`is_known_standard:340`、`normalize_standard_code:298`
2. **模板级依据**：`outline_templates.TEMPLATE_META[key]["basis"]`（每模板一段自然语言依据串，如 `foundation_pit` 引 GB 51004-2015 / GB 50202-2018 / JGJ 120-2012 / JGJ 311-2013 / GB 50497-2019 / 住建部令 37 号 / 建办质〔2018〕31 号）
3. **提取项级**：`ANALYSIS_ITEMS[3] compilationBasis`（JSON 模板，`bid_analysis_service.py:427+`，字段含 `supervision_unit:440`、`chief_supervision_engineer:471` 等）
注入入口：`sse_handlers._outline_standards_text:3629`（开关 `outline_standards_inject` 默认 True）
⚠️ **无省市字段**：`Standard` 只有 `code/name/mandatory`，无「适用地区/实施日期/替代关系链」。

### 9.4 专家论证（**专项方案特有，标书域完全没有**）
- **必要项 10 项**（`audit_rules.EXPERT_ITEM_RULES:460`）：工程概况→CMP-01、编制依据→CMP-02、施工计划→CMP-03、施工工艺技术→CMP-04、安全保证措施→CMP-05、人员分工→CMP-06、验收要求→CMP-07、应急处置措施→CMP-08、计算书及相关图纸→CMP-09、**监测方案→SAF-06**
- 论证要点（`audit_rules.py:25-26` 引 31 号文附件二）：①内容完整可行 ②计算书和验算依据符合标准 ③安全施工基本条件满足现场实际
- 端点：`compliance.expert_review:240`（`EXPERT_OUTLINE_MAX_NODES=300`、`EXPERT_ATTACH_MAX_COUNT=8`、`EXPERT_ATTACH_MAX_LEN=500`）；`GET /compliance/expert-review/items:102`（`get_expert_items`）
- 提示词：`prompts/analysis.py:376` `expert_review_system`（角色 = 「危大工程专项方案专家论证组专家」）
- **超规模（是否必须专家论证）**：`schemes.is_oversize` 落库（`bid_analysis.py:2392-2397`，`1 if classification.is_oversize else 0`）、`scheme_classification_json` 同存；前端红标展示（`global_facts.py:330` 注释：误报会「在前端红标展示」）；`is_hazardous` 判定 `_scheme_is_dangerous:3525`（`schemes.type` 下拉值 **或** 方案名称字面命中六大类危大）
- 方案名中的专家评审变体已在 `seed_data.SCHEME_CATALOG` 中大量预置（`基坑工程专家评审方案:36`、`高大支模专家评审方案:45`、`塔吊基础专家评审方案:60`、`预制装配构件吊装专家评审方案:64` 等）
- ⚠️ **只做到「预检」**：全仓 `论证报告`=0 命中、`危大工程公示`=0、`应急预案备案`=0、`建设单位审`=0 —— **无「专家论证意见回填 / 论证报告归档 / 论证结论跟踪」闭环**；`schema_sql.py` 33 表中**无论证意见表**

### 9.5 验收要求
- 章节维度：`NINE_CHAPTERS[6] acceptance`（base_fields：验收标准编号 / 验收程序步骤 / 验收内容清单 / 验收人员组成）
- 规则维度：`CMP-07`（完整性）+ `DLV-*`（可交付性）
- 内容维度：`outline_templates._acceptance_chapter:121`；`preflight_engine._NUM_TOPICS:107`（数值主题表）；`_has_calc_process:187`
- 事实维度：`CATEGORY_TITLES["acceptance"]="验收要求"`(`facts_extractor.py:1186`)、`fact_type=acceptance`（`prompts/analysis.py:204` P17）
- ⚠️ 专项方案特有的验收要素（三检制 / 旁站 / 见证取样 / 第三方检测 / 首件验收 / 灌浆旁站 / 危险源清单 / 环境因素评价）**只作为 `outline_templates` 的章节描述文案存在**（`:84`、`:108`、`:224`、`:961`、`:1303`、`:1346`），**没有对应的规则条目或结构化字段** → 预检查不出、导出拦不住

### 9.6 其它专项方案特有概念（代码中确有，标书域无对应）
| 概念 | 位置 |
|---|---|
| 危大工程必备 10 章 | `sse_handlers._DANGEROUS_REQUIRED_KEYWORDS`（含「监测方案」章），由 `_check_requirements_coverage:2981` 使用 |
| 「专项施工方案 ≠ 投标文件」红线 | `prompts/_shared.py:23` `SHARED_SCOPE_RULES` |
| 标题禁用词 | `prompts/_shared.py:8` `SHARED_FORBIDDEN_WORDS` |
| 模拟值闸门 + 17 类安全关键白名单（禁模拟） | `facts_extractor._filter_safety_sensitive:1203`、`is_safety_critical_name:1236`、`SAFETY_CRITICAL_MAX_SIMULATED=0`(`:50`)、`SIMULATED_MARKER:397` |
| `safety_critical` / `risk` 事实类别 | `facts_extractor.CATEGORY_TITLES:1178-1179` |
| 六维审核评分 + 等级带 | `audit_rules.DIMENSIONS:129`（完整性 25 / 规范 20 / 安全 20 / 一致性 15 / 可追溯 10 / 可交付 10）、`SEVERITY_PENALTY:147`、`GRADE_BANDS:150`（A≥90 可直接交付 / B≥75 可提交论证 / C≥60 需整改 / D 不具备交付条件） |
| 可追溯性维度（计算书与验算依据，31 号文论证要点②） | `audit_rules.py:138-139`、`preflight_engine.check_traceability:474`、`_CALC_KEYWORDS:94`、`_MANDATORY_CODE_RE:84`（GB 55xxx 全文强制） |
| 禁编造责任主体 | `prompts/content.py:58`（不得生成项目经理/技术负责人/安全员姓名与证书编号、监理/设计/勘察单位名称、设备出厂编号/检定证书编号/专利号） |
| 资料 9 分类 + 提取优先级 | `doc_categories.DOC_CATEGORIES:26`（招标文件/合同文件/设计文件/地勘报告/报价清单/资质材料/人员资料/财务资料/业绩证明）、`EXTRACT_PRIORITY:78`、`auto_classify_document:95`、`extract_priority:113`（施工组织设计明确归入「招标文件」优先级 0） |
| 方案名 19 类下拉 | `frontend/src/pages/ProjectDetailPage.tsx:14-19`（深基坑/高支模/脚手架/塔吊/施工电梯/临时用电/消防/安全文明/绿色施工/质量创优/进度计划/应急预案/施工组织设计/钢结构吊装/降水/土方开挖/模板工程/混凝土工程/有限空间）；同表另见 `OutlineLibraryEditModal.tsx:19` |
| 编号统一与快照回滚 | `numbering.py` 全套（`rollback_numbering_version:725`、`list_numbering_versions:748`、`_NUMBERING_SNAPSHOT_TYPES:722`） |
| 方案/项目双层事实作用域 + fail-closed 安全门槛 | `global_facts._validate_fact_scope:246`、`_assert_fact_in_scheme_scope:272`、`_invalidate_fact_scope_cache:299`（`:1652` 注释：表存在但探测失败 → 漏拦危大事实，必须留痕） |
| 事实变更 → 章节失效标记（只提示不静默重写） | `schemes.facts_updated_at` 单点时间戳 + `sections._build_tree:64` 读侧派生 `facts_stale` |
| 章节深度上限 `MAX_OUTLINE_DEPTH=3` | `routers/sections.py`（create/update/save-outline/reorganize/`_outline_skeleton` 四条路径统一校验，文案常量 `_DEPTH_EXCEEDED_MSG`） |
| 正文质量双标准（precise / fuzzy） | `content_standard.PRECISE:25` / `FUZZY:26` / `DEFAULT_STANDARD:29` / `FUZZY_VALUE_TOLERANCE:213`；前端 `utils/contentStandard.ts` |
| 解析截断诊断 | `file_parser._note_truncation:365` + `parse_truncated` / `parse_warnings` 两列 + `pdf_text_max_pages:500`（`config.py:44`） |

---

## 10. 现有测试基线

- **后端 `backend/tests/`：实测 213 个 `test_*.py`**（AGENTS.md §2「135 个文件」与 §6 记录**均已过期**）
- 前端 `frontend/src/tests/`：**52 个测试文件**（与 AGENTS.md 一致）
- AGENTS.md 记录的最近基线：**4110 passed / 4 skipped / 3 xfailed**（第十六轮，2026-09-30，210.65s）—— **当前工作区已新增 5 个测试文件，该数字必然已变**
- 关键专项测试文件（本报告 12 项相关）：
  - 参考对齐：`test_reference_alignment_20260930.py`(33)、`test_image_budget_g2_20260930.py`
  - 解析提取：`test_bid_response_domain_20260930.py`(73)、`test_bid_analysis_legacy_closeout_20260930.py`(29)、`test_parse_truncation_fix_20260930.py`(25)、`test_bid_analysis.py`、`test_bid_analysis_evidence.py`、`test_bid_section_context.py`
  - 全局事实：`test_global_facts_reference_parity_20260930.py`(90)、`test_global_facts_legacy_closeout_20260930.py`(47)、`test_fact_gate_parity_20260930.py`(13)、`test_facts_inject_gate_failclosed_20260927.py`
  - 图表：`test_chart_json_registration_parity_20260927.py`(28)、`test_chart_prompt_parity_20260930.py`(30)、`test_chart_prompt_coverage_20260927.py`
  - 审核：`test_review_autofix_20260930.py`、`test_review_export_fixes_20260930.py`、`test_audit_fixes_20260927.py` / `_b.py`
  - 导出：`test_export_pdf_cache_d2_20261001.py`、`test_export_appendix_d3_20261001.py`、`test_export.py`
  - 七模块：`test_seven_module_fixes_20260927.py`、`test_seven_module_fixes_20261001.py`、`test_seven_module_batch2_20261001.py`
  - 提示词：`test_prompt_governance.py`、`test_prompt_module_fixes_20260927.py`、`test_prompt_rollback_governance.py`
- 前端：`chartTypesParity.test.tsx`、`exportGateParity20260927.test.ts`、`outlineQuality.test.ts`、`parseSourceNotice.test.ts`、`reviewAutoFix.test.tsx`、`bidAnalysisApi.test.ts`
- ⚠️ 本次盘点为**只读**任务，**未执行 pytest / vitest**，**未验证当前实际通过数**，需重跑取实测基线。

---

## 11. 第十六轮已落地的 `techScoring` / `consistency_edits` 现状

**G1 技术评分要求提取**（`docs/reference_gap_analysis_20260930.md` §5 → 代码）
- `bid_analysis_service.py:122-131` 新增 `techScoring`（`json`；4 字段 `item_name` / `weight`（统一为分或 %）/ `criteria` / `source`；`required=0`；`sort_order=19`；`group=scoring`）
- `GROUPS:337` 追加 `scoring` 组（实测 GROUPS 共 14 组）
- `_ITEM_PROMPTS:426` 新增自我反思式五段式提示词（目标定位 / 提取内容 / 处理规则 / 验证 / 只返结果）
- 取舍已落地且有护栏：`sort_order` **追加末尾**（历史数据契约，`bid_analysis_service.py:119-121` 注释）、`required=0` 不卡流程、与 `bid_response.techRequirements` **保持两域零交集**（实测交集为空集）
- **仍未闭环**：G3「一级目录 ← 评分大类一一对应」未做（`reference_gap_analysis_20260930.md` §6 明列「待做」）—— `techScoring` 目前**只被提取与展示，目录生成不消费**（`generate_outline` 的渲染参数中无 `techScoring` 相关入参），即新增项与九大章节之间**没有任何代码路径相连**

**G2 old_text/new_text 定点替换**（见第 8 节）——已落地、已接线、已护栏；**限制**：`/consistency/confirm` 的按条回滚仍为整章级快照恢复。

---

## 12. 【重点】本仓在「专项方案域」相比参考软件明显缺失的能力（基于代码事实）

> 判据：只列**在代码里查不到实现**的，且**属于专项方案域语义**的。参考软件（易标）**没有**这些能力，因此无法靠「对齐易标」补上 —— 必须按本仓业务自建。

| # | 缺失能力 | 代码证据（查不到 = 缺失） | 为什么专项方案域必须有 |
|---|---|---|---|
| **1** | **专家论证闭环**（论证意见回填 → 逐条整改 → 论证结论归档） | 全仓 `论证报告`=0 命中、`危大工程公示`=0、`应急预案备案`=0、`建设单位审`=0；`compliance.expert_review:240` 只产出一次性预检结果，无「意见-整改-销项」数据模型（`schema_sql.py` 33 表中无论证意见表） | 超规模危大（`is_oversize=1`）方案**法定必须**经专家论证；不闭环 = 方案不可交付 |
| **2** | **危大/超规模阈值的分省差异化** | `scheme_classification.py:8` 注释「阈值以部文为基准，**地方从严时以地方为准**」、`:19`「后续若需按省市细则差异化，可经 config 注入覆盖（**当前默认部文口径**）」——**无省市维度表**，阈值全表硬编码部文值（`:227-339`） | 31 号文明确「地方可加严」，多地细则普遍严于部文；本仓接地方项目会**系统性漏判**。本仓最大的合规性风险敞口 |
| **3** | **非参数型危大的超规模子情形** | `HAZARD_THRESHOLDS` 中 `dm_manual/dm_machine/dm_blast/ot_underground/ot_confined/ot_newtech/ot_prestress` 全部无独立 `oversize_when`；`evaluate_hazard_level:554-564` 直接返回 `is_oversize=True`（与危大同值）；`ot_curtain/ot_steel/ot_slope` 的 `hazard_when == oversize_when`（永不可区分） | 拆除爆破、地下暗挖、有限空间在部文附件二里**各有独立的超规模条件**，本仓一律等同危大 → 专家论证提示会**误报或漏报** |
| **4** | **验收 / 旁站 / 见证的结构化承载** | `三检制/旁站/见证取样/第三方检测/首件验收/灌浆旁站` 仅出现在 `outline_templates` 的**章节描述字符串**（`:84`、`:108`、`:224`、`:961`、`:1303`、`:1346`）；`audit_rules` **实测 45 条**规则中无一条针对验收执行环节；`global_facts` 无对应 `category` | 专项方案的验收/旁站是**法定动作**，当前只能靠目录模板「提到」，**预检查不出、导出拦不住** |
| **5** | **危大工程台账 / 一方案多危大并存** | `schemes.is_hazardous` / `is_oversize` 是**单值布尔**（`bid_analysis.py:2392`），`scheme_classification_json` 存命中列表但无独立台账表；`HAZARD_CATEGORIES` 支持多子类命中（`match_category_keywords:454` 返回 list）但落库被压成 1/0 | 「基坑+高支模+脚手架」复合专项方案极常见；当前**丢失「哪几类危大、哪几类超规模」的组合信息** |
| **6** | **方案审批/会审流转** | `会审`=5 命中**全部**是「图纸会审」（设计文件概念）；`审批`30 命中中方案相关的仅提示词文案（`bid_analysis_service.py:496`「项目级审批、公司级审批、专家论证、监理审核」）；`签字`=1；**无审批流路由/表/状态机** | 专项方案须「企业技术负责人审批 + 监理审核 + 专家论证」三段流转才可实施 |
| **7** | **施工监测数据闭环**（监测值 ↔ 预警值 ↔ 报警值 实际数据回填） | 「监测方案」是第 10 章、`SAF-06` 是论证必要项、`ot_*` 阈值只从**方案文本**抽参（`extract_danger_params:506`），**无监测数据实体表**、无「本期监测值 vs 预警值」判定 | 危大工程的核心风险控制手段；参考软件做标书不需要，本仓缺这块 = 危大判定只能停留在设计阶段 |
| **8** | **计算书 / 验算的结构化承载** | `preflight_engine._CALC_KEYWORDS:94` + `_has_calc_process:187` + `TRC-01~04` 只做**关键词存在性**检查；`docx_math.py`(29KB) 只在**导出时**把 LaTeX 转 OMML；**无验算输入/输出/安全系数**的数据结构，无安全系数自动复核 | 31 号文论证要点②即「计算书和验算依据是否符合标准」；当前只能判「有没有」，判不了「算得对不对」 |
| **9** | **危大阈值参数的人工补录闭环** | `evaluate_hazard_level:593` 返回 `missing_params`，`check_danger_scheme:759` 是**只读诊断**（注释明写「只读诊断，不改数据」）；**无「缺参 → 补录 → 重判」的写路径** | 缺参时保守判危大但用户无法消解 → 危大红标无法消除 |
| **10** | **九章 / 10 章 / 22 类 / 12 分类 四套体系的一致性护栏** | 仓内**无任何**「`NINE_CHAPTERS` 9 章 ↔ `_DANGEROUS_REQUIRED_KEYWORDS` 10 章」parity 断言（第九轮只对事实门控/深度/rowcount 做了 parity）；`HAZARD_CATEGORIES.category_id` ↔ `outline_templates.template_key` **无声明式映射表** | 四套分类同时存在且已被历史轮次证明「多处实现必然分叉」；无护栏 = 下次改动必然分叉 |
| **11** | **地方实施细则的标准库维度** | `standards_registry.CATEGORY_KEYWORDS:68` 仅 15 类、**无省市字段**；`Standard:32` 只有 `code/name/mandatory`，无「适用地区/实施日期/替代关系链」 | 标准适用性、强制性（GB 550xx 全文强制的地区差异）无法表达 |
| **12** | **单位归一化口径收敛** | `preflight_engine._UNIT_CANON:371` / `_norm_unit:378`、`facts_classification._UNIT_TO_METER:475` / `_NUM_RE:480`、`content_standard._UNIT_ALIASES:177` 是**三份独立单位表** | 「6m」vs「6000mm」在危大判定里是同一参数；单位口径分裂会直接导致**阈值误判** |

**判定为「本仓已超出、不可反向迁移」的能力**（供吸收参考软件时避坑）：九大章节体系、危大分类与阈值（闭区间口径已修）、双提取域、模拟值 fail-closed 门控、编号统一+快照回滚、六维审核评分、方案名六维确定性解析、22 模板 × 编制依据 × 适用条件 × 危大分级。

---

## 未提交改动的文档缺口

> 判据：`git status --porcelain` 中 `??` 状态（工作区新增、**从未纳入版本控制**）的文件。
> HEAD = `e128ca4`（「首次纳入版本控制 - 专项方案工具箱 v5.3.0」），此后 4 个 commit 均为小改。
> **结论：AGENTS.md 只记录到第十六轮（§4.20，2026-09-30），下列能力中标注 ⚠️ 的部分已落地但无任何文档记录。**
> 这直接决定「吸收参考软件时哪些能力其实已经做完、不能再排进差距清单」。

### A. ⚠️ **2026-10-01 新增：AGENTS.md 完全无记录（第 17 轮，能力已落地）**

| 新增文件（`??`） | 对应能力 | 证据 | AGENTS.md 状态 |
|---|---|---|---|
| `backend/tests/test_seven_module_fixes_20261001.py` | 七模块专项 · 第一轮修复护栏：①`sse_handlers.word_budget_override` 未归一化 → 字符串进 `int()` 抛 `TypeError` 点崩；②SSE `stopped` 事件在 `failed_count` 之前 → 前后端契约不同步（前端恒见 0）；③`_invalidate_extraction_derived` 被 `if done:` 门禁挡住 → 派生产物永久残留失效 | 文件 docstring 列出 C-1 / C-3 / A-3 | ❌ **无记录** |
| `backend/tests/test_seven_module_batch2_20261001.py` | 七模块专项 · 第二轮护栏：①18 项提取「多文档合计」字符预算默认 30000 → 400000（与解析/落库三级对齐）+ 修正注释与实现分叉；②`global_facts` 两处裸 `except Exception: pass` 补日志与坐标；③高频失败点（分段提取异常 / PDF 主通道失败）补 `exc_info`；④`export_docx` 编号守卫前置到 `_prepare_export` 之前 | 文件 docstring 列出 A-1 / A-4 / A-9 / D-1 | ❌ **无记录** |
| `backend/tests/test_export_pdf_cache_d2_20261001.py` | **D-2 · PDF 导出链路缓存 + 脏缓存守卫**：①PDF 指纹必须带 `fmt=pdf` 后缀开启，否则与 DOCX 共用 `(scheme_id, config_hash, content_fingerprint)` 唯一键 → `INSERT OR IGNORE` 让 PDF 永远写不进缓存，每次导出重跑最慢的 PDF 转换（Word COM / LibreOffice），无秒级命中；②DOCX 指纹计算方式不得改变（零缓存失效代价）；③缓存命中路径存在并带 `X-Cache-Status: hit`；④脏缓存守卫：degraded 时不写缓存并回传 `X-Cache-Status: degraded` | `export.py:3105-3108` 注释、`:3770` P1 说明、`:4087` 守卫顺序注释 | ❌ **无记录** |
| `backend/tests/test_export_appendix_d3_20261001.py` | **D-3 · 导出附录（数据源章节）**：①`config.export_appendix_sources`（`config.py:132`，**默认 False**）；②`_build_docx_task` 元组槽位/位置参数顺序不得错位；③**补充附录必须纳入内容指纹**，否则开关打开后缓存不失效；④数据源加载 fail-soft（表缺失/查询失败不阻断导出） | `export.py:2538` `_get_or_add_appendix_heading_style`、`:2601-2602` 参数、`:3012/:3054-3063` 渲染、`:3779` prep 回传、`:3814` `_load_appendix_sources`、`:3833` 开关判定、`:3890/:3996` 消费 | ❌ **无记录** |
| `backend/tests/test_image_budget_g2_20260930.py` | **G2 · AI 配图全局预算 · 分段择优**：避免「前面章节把图片额度全部用光」。配置 `config.max_ai_images:123`（**默认 0 = 关闭，零迁移**）+ 纯函数 `image_engine.apply_image_budget:870` + 封装 `select_ai_image_codes:929` + 接线 `export._auto_generate_ai_image_blocks` | 文件 docstring 明示「对应缺口：六（OpenBidKit 易标《标书智能体（六）》§6）要求 AI 可提名很多生图候选，但最终只按 maxAiImages 择优执行」 | ❌ **无记录**（AGENTS.md §4.20 只在 `docs/OpenBidKit六模块解析与比对.md` 第三部分列为「待确认方案 G2」，未落地记录） |

**⚠️ 上述 5 项必须先补进 AGENTS.md（建议新增 §4.21），再做任何参考能力差距分析** —— 否则会重复评估已完成项。

### B. ✅ 已落地但**已有** AGENTS.md 记录（`??` 仅因未纳入 git，不代表无文档）

| 新增文件（`??`） | 能力 | AGENTS.md 出处 |
|---|---|---|
| `backend/app/services/consistency_edits.py` | old_text/new_text 定点替换 | §4.20.3（G2） |
| `backend/app/services/facts_patches.py` | 易标补丁机制 23 纯函数 | §4.16 |
| `backend/app/services/facts_enrich.py` | 知识库补充 + finalize | §4.17 |
| `backend/app/services/review_autofix.py` | 审核预检问题定向自动修复 | 无专章（`review_autofix` 路由已在 §2 目录中列出） |
| `backend/app/routers/review_autofix.py` | 同上路由 | 同上 |
| `backend/app/services/ai/prompts/review_autofix.py` | 审核自动修复提示词 | 无专章 |
| `backend/app/services/content_blocks.py` / `content_runtime.py` | 内容块解析 / 章节上下文组装 | 无专章 |
| `backend/app/services/outline_quality.py` | 目录连续性 + 名称覆盖分析（`analyze_name_coverage`） | §4.13 提及（`check_outline_continuity`） |
| `backend/tests/test_reference_alignment_20260930.py` | 第十六轮护栏（33 例） | §4.20.4 |
| `backend/tests/test_bid_response_domain_20260930.py` | 第十一轮护栏（73 例） | §4.15 |
| `backend/tests/test_global_facts_*_20260930.py` | 第十二/十三轮护栏（90 / 47 例） | §4.16.4 / §4.17.6 |
| `backend/tests/test_parse_truncation_fix_20260930.py` | 第十四轮护栏（25 例） | §4.18.4 |
| `backend/tests/test_bid_analysis_legacy_closeout_20260930.py` | 第十五轮护栏（29 例） | §4.19.6 |
| `backend/tests/test_chart_*_parity_20260930.py`、`test_chart_prompt_coverage_20260927.py` | 图表三侧口径护栏 | §4.3 |
| `backend/tests/test_fact_gate_parity_20260930.py` | 事实门控 parity（13 例） | §4.13.1 |
| `backend/tests/test_audit_fixes_20260927*.py` | 审核注册表一致性护栏 | §4.12 / §4.13 |
| `backend/tests/test_content_fence_contract_20260929.py` | 正文围栏契约（14 例） | §4.14 |
| `backend/tests/test_task_activity_projection_20260930.py` | 活动快照（5 例） | §4.13.3 |
| `backend/tests/test_review_export_fixes_20260930.py` | 审核/导出修复护栏 | 无专章 |
| `backend/tests/test_prompt_module_fixes_20260927.py` | 提示词模块 8 缺陷护栏（63 例） | §4.7 |
| `backend/tests/test_*_20260927.py`（10 余个） | 第九/十轮各模块护栏 | §4.13 / §4.14 |
| `frontend/src/utils/chartTypes.ts` + `chartTypesParity.test.tsx` | 图表类型前端白名单单一事实源 | §4.3（2026-09-27 修复） |
| `frontend/src/utils/parseSourceNotice.ts` + `parseSourceNotice.test.ts` | 解析来源提示 | 无专章 |
| `frontend/src/components/review/AutoFixModal.tsx` + `reviewAutoFix.test.tsx` | 审核自动修复前端 | 无专章 |
| `frontend/src/tests/exportGateParity20260927.test.ts` | 导出门禁 high 类型前后端 parity | §4.9.7 |
| `frontend/src/tests/outlineQuality.test.ts` / `bidAnalysisApi.test.ts` | 目录质量 / 提取 API | 无专章 |
| `cleanup_guard.ps1` + `cleanup_guard_selftest.ps1` + `stop_all.bat` | 统一进程清理机制 | §3.3 / §5.13 |
| `docs/OpenBidKit六模块解析与比对.md`、`docs/reference_gap_analysis_20260930.md`、`docs/process_guard_mechanism.md`、`docs/目录生成_*.md` | 调研与机制文档 | §4.20 引用前两份；`docs/process_guard_mechanism.md` 见 §5.13 |

### C. 其它需同步的事实性偏差（非 `??` 文件，但同样影响后续判断）

| 偏差 | 事实 | 依据 |
|---|---|---|
| 提取项数量 | AGENTS.md 多处写「18 项」，实测 **19 项** | `bid_analysis_service.py:122-131`；实测 `len(ANALYSIS_ITEMS)==19` |
| 分组数量 | AGENTS.md 写「13 分组」，实测 **14 组** | `bid_analysis_service.py:321` `GROUPS` |
| 后端测试文件数 | AGENTS.md §2/§6 写「135 个文件」，实测 **213 个** | `Get-ChildItem tests -Filter test_*.py` |
| 测试通过数 | AGENTS.md §6 记 4110 passed（第十六轮），**工作区已增 5 个测试文件** | 新增文件见 A 节 |
| 第 17 轮改动 | AGENTS.md **完全无记录** | A 节 |

---

## 附：两条行动建议（不含实施）

1. **先补文档再吸收**：工作区 108 个未提交文件（第 17 轮 G2/D2/D3 等）无文档记录，任何基于 AGENTS.md 的差距分析都会**重复评估已完成项**。建议先补 §4.21 并重跑全量取实测基线。
2. **第 12 项的 1/2/3/9 四条属「专项方案域独占、参考软件帮不上」类型**，应单独立项而非放进「参考能力对齐」框架 —— 否则会再次得出「易标没有 → 判定为非缺陷 → 永远不修」的结论。










