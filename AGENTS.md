# AGENTS.md — 专项方案工具箱 · AI Agent 工作边界

> **最近校准：2026-10-04（下一轮修复收口 · 全量回归验证基线 + G3 前端死按钮回归修复）** —— **【接未提交工作树的验证与一处真实回归修复】**（零新增依赖、零新增配置项、零数据迁移）：
> ① **【全量回归验证】** 工作树含多轮未提交改动（G3 `ack-stale` 解锁 / G1·G2 编号端点接线 / D4 围栏口径 / 导出缓存与轮次 / content·outline checkpoint 模块 / R39 跨模块链），本轮只验证不新增功能：后端 pytest **5527 passed / 0 failed / 0 error / 19 skipped（259.97s）**；前端 vitest **843 passed / 0 failed（117.35s）**；`tsc --noEmit` 零错误。A/B 反向验证随 `test_prompt_techdebt_r38_20261003.py`、`test_facts_ack_stale_20261004.py` 等一并跑通，无回归。
> ② **【修复真实回归 · G3 前端死按钮】** `SchemeWorkbenchPage.tsx`：「来源已变化 ✓」Tag 的 `onClick` 在独立子组件 `FactsGroupList` 内直接调用了仅父组件才定义的 `handleAckStaleOne` → `tsc` 报 `TS2304`/`TS2741`/`TS2722`，**生产构建必失败（死按钮）**。修法：给 `FactsGroupList` 加可选 prop `onAckStaleOne?: (itemId: string, title?: string) => void;`，调用点改可选链 `onAckStaleOne?.(it.id, it.title)`，两处 `<FactsGroupList>` 渲染点补 `onAckStaleOne={handleAckStaleOne}`（设可选以兼容既有测试渲染点 `perfOptimizations0925.test.tsx`）。复跑 `tsc` 零错误。
> ⚠️ **教训（与 R38-L 同题）**：子组件内引用父组件作用域函数不会在编辑期报错（linter 对本工作区中文全角字符误报不可依赖），必须 `tsc --noEmit` 全程把关；跨组件事件回调统一走 props 透传，勿在子组件闭包里直接引用父级 handler。

> **最近校准：2026-10-04（七模块跨链路收口 · R39）** —— **【跨模块深度探索后的 P0/P1 缺陷修复】冲突 ID 跨方案碰撞 · 合规空清单 · 事实提取重入 · 四层存储判空 · 下游缓存失效 · 证据合并**（零新增依赖、零新增配置项、零数据迁移、返回契约零变化）：
> ① **【P0 · 跨方案数据泄漏】`consistency_conflicts` 主键碰撞**：冲突 ID 是全局流水号 `C{idx:03d}`，每次扫描都从 C001 起编，而 `id` 是**全局主键**（不含 scheme_id）；`persist_conflicts` 的 `ON CONFLICT(id) DO UPDATE` 又**不更新 scheme_id** → 方案 B 扫描把 B 的 scan_id/occurrences 写进**方案 A 的行**：B 查不到任何冲突（exists=false，用户看到「尚未扫描」），A 取「最新 scan_id」时拿到 B 的 → **B 方案正文与章节 ID 展示在 A 方案下**。修法：`merge_conflicts` 增加 `scheme_id: str = ""` 参数（默认空 → ID 形态与历史**逐字一致**，既有纯函数单测零影响），ID 加方案作用域短哈希前缀；`persist_conflicts` 的 DO UPDATE 补 `scheme_id=excluded.scheme_id` 作为兜底层。历史落库行不迁移（get_conflicts 按最新 scan_id 取数，旧行自然隔离）。
> ② **【P1 · 空清单进 AI】`compliance_check` 的 `else` 分支漏兜底**：`ComplianceCheckIn.checklist` 默认 `[]`（models.py:302），而旧实现只在 `rule_ids` 非空分支做兜底 → 前端不传清单时提示词是「逐项检查：[]」，AI 自行编造检查项。真实触发面：前端 `useRules` 判据 `checklist.length === aiRules.length`（10 条自由文本 vs `ai_rules()` 8 条）恒 false → **永远走这条路径**。修法：`checklist = body.checklist or [r.title for r in ai_rules()]`。
> ③ **【P1 · 事实提取可并发重入】`generate-facts` 是三条 SSE 链路里唯一没有自我重入守卫的**（outline/content 都有）→ 连点两次会起两路管线，各自跑完整条提取，`persist_extraction` 的「DELETE+INSERT」由**后提交者覆盖前者**（事实集合取决于谁最后提交），且两路 `save_extracted_chunks` 互相污染增量指纹。修法：新增 `sections.facts_generation_in_progress`（与 `outline_/content_generation_in_progress` 同口径，只拦 running/paused），端点命中即 409。
> ④ **【P1 · 四层存储两处漏 R13 判空】** `pipeline.ingest_parse_result`（解析→分块唯一入口）与 `doc_pipeline.document_freshness`（该文件唯一**写路径**）：`db.execute` 返回 None 时前者 `AttributeError` 被上层 `except Exception: return {}` 整段吞掉（doc_chunks 全丢、响应 layers 消失、日志只有「解析成功」），后者回写静默丢失。均已补守卫（前者按「无历史块」降级，后者告警后跳过 commit）。
> ⑤ **【P1 · 下游缓存失效只在 force 路径】** `_invalidate_downstream_cache` 文档自称「提取结果变化后失效下游缓存」，但**只有 `_reset_items_for_rerun`（force_rerun）调用它** —— 而高频路径恰恰是**非强制重跑**：结果照样写库，export_cache / consistency_scan_cache / schemes.facts_updated_at / doc_extractions 全部停留旧值（界面显示「已重新提取」，导出内容却是旧的）。修法：新增 `_safe_invalidate_downstream`（fail-soft 到极致），同步与 SSE 两条收尾路径的任务终态写入**之前**各调一次。
> ⑥ **【P1 · 前后端契约】quality_issues 载荷是对象不是数组**：后端 `content_polish.quality_issues` 返回 `{colloquial_hits:[], abolished_standards:[]}`，前端三处一律 `Array.isArray` 判定 → 恒 undefined → 日志区「质量告警 N」Tag **从未出现过**。新增单一归一出口 `normalizeQualityIssues`（对象展平 / 数组兼容 / 空载荷 undefined），页面与 `useContentGeneration` 双实现同时接入（消除两份实现的进一步漂移）。
> ⑦ **【P1 · 去重吞证据】`merge_findings`** 同 rule_id 只保留严重度更高的一条，被丢弃那条的 `evidence`/`section_ids`/`source` **整条消失** —— 导出侧（`export_issues_to_findings`，带 20 章清单）与程序侧 `check_deliverability` 同产 DLV-01~04 时导出侧清单被静默丢弃。修法：新增 `_merge_dropped_evidence` 把丢弃方证据并入保留者（**只做并集、不做计数相加**，避免虚增）。⚠️ **评分零变化**：`score_findings` 只按 severity 扣分、不读 count（audit_scoring.py:138），护栏 `test_merging_does_not_change_score` 锁定。
> ⑧ **【P2 契约/一致性四则】** ① 导出 cache_key：DOCX 与 PDF 写入**逐字相同**的值（`/cache-status` 与运维无法辨识格式）→ PDF 侧补 `|pdf`，DOCX 侧逐字不变（既有缓存行**零失效**；命中键本就是三元组，cache_key 不参与命中）。② 目录整表重建（`_save_outline_to_db`）补 `invalidate_export_cache`（此前目录侧一条失效都没有）。③ 压缩按钮阈值：前端 `word_count > word_budget` vs 后端 `> 预算*1.3`（100%~130% 按钮可点、后端必 400）→ 抽出 `WORD_OVER_RATIO=1.3` + `canShrinkSection`（workflowDerived.ts），前后端同口径。④ `global_facts.list_facts` 的 docstring 被写在**函数体内** → `__doc__` 恒 None（FastAPI OpenAPI 描述丢失），已移到函数体首行。
> 护栏：新增 `tests/test_cross_module_chain_20261004.py` **23 例**（DB 级两方案落库互不覆盖真断言 / 真实 ASGI HTTP 校验提示词内容与 409 守卫 / 代理 DB 命中 None 分支 / 静态接线锁 / 评分不变性）。**A/B 反向验证 4 项全部定向失败**（A1 摘方案前缀 → 2 例；A2 摘清单兜底 → 1 例；A3 守卫改 `if False` → 1 例；A4 摘证据合并 → 1 例），`try/finally` 还原 + sha256 字节一致 + 残留扫描零命中。前端 `contentEvents.test.ts` +4 例、`workflowDerived.test.ts` +5 例（**74 passed**），`tsc --noEmit` 零错误。后端全量跑满 100% 零 F/E。
> ⚠️ **刻意不改（记录以免重复排查）**：`_PROGRAM_EMITTED_RULE_IDS` 不补 CON-07 —— 该集合的契约是「**预检引擎**产出全集」，而 `CON-SCAN-N` 由 `compliance.py` 总检聚合产出，不在该契约内；且 `test_audit_rules_namespace_20261003.py` 的**双向相等静态锁**（scanned ↔ registered）会因补登而失败。若要收口，应先把 compliance 聚合侧纳入扫描或另立集合。
>







> 本文件定义 AI 编程代理（Coder/Agent）在本仓库工作时的行为边界。参考 OpenBidKit Yibiao 的 AGENTS.md 引入（2026-09-22）。



>



>



> **最近校准：2026-10-04（遗留技术债核实与孤儿数据收口 · R39）** —— **【R38-D 五项债磁盘复核已闭合 + 生产实测孤儿数据清零】本轮零代码改动，纯验证与运维执行**：

> **基线对比**：R39 前 **5447 passed / 16 skipped / 3 xfailed / 0 failed（203.88s）** → 终态 **5495 passed / 16 skipped / 3 xfailed / 0 failed（209.69s，退出码 0）**，**+48 例全绿、零回归**；关联回归（`-k "bid or prompt or outline or facts or repair or consistency"`）**2368 passed / 11 skipped / 0 failed**；提示词专组 7 文件 **272 passed / 0 failed**；审核自动修复域 6 文件 **99 passed**。
> **【顺带收口】并行会话在基线之后引入的真实 BUG**：`review_autofix.py::confirm` 新增返回字段 `skipped` 时把 `skipped = []` 放在**循环体内**初始化，函数末尾却无条件读它 → `UnboundLocalError` → `/review-autofix/confirm` **500**。实测「全部接受」与「部分接受」两条**主干**流程均必崩（基线全绿 → 改动后 2 failed）。已提到循环外初始化 + 新增护栏 `tests/test_review_autofix_skipped_init_20261004.py`（3 例，AST 静态判定）。
> ⚠️ **并行会话交叉风险实例**：该 BUG 不在本轮修改范围内（`review_autofix.py` 修改时间 0:41 早于本轮全部改动 1:02+），但它会**把本轮全量回归拉红**，故一并处理并记录。
> ② **生产实测孤儿数据清零（R32 数据迁移类遗留落地）**：`uploaded_outlines` 孤儿行实测 **0**（88 行已随库重建消失，无需清理）；磁盘 `data/projects/` 实测 387 目录中 **384 棵孤儿四层文档树**已由 `tools/gc_orphan_doc_trees.py --apply`（回收站模式，同卷 rename + manifest 留档，可整体移回）移入 `.trash/orphan-gc-20261004_001013/`，复验 **orphans=0**、moved=384 failed=0；`.trash` 自身与在役项目目录保护不碰；执行前核实 8000 无监听、后端服务未运行（无句柄占用/并发写风险）。工具护栏 `test_gc_orphan_doc_trees_20261003.py` + `test_cleanup_preflight_runs_20261003.py` **25 passed**。

> ③ **生产库前后对比测量仍阻塞（非缺陷）**：重建后实测 `sections_with_content=0` / `total_chars=0`（R29/R30 挂起项的判据 `with_content>0 且 total_words>0` 仍不满足，正文尚未重新生成）；`preflight_runs` 仅存 2 行真实总检（10-01），无污染行。待用户在 UI 重新生成正文后再跑一次 /overview 即得修复后基线。

> 本轮零代码变更 → 无需全量；上轮基线 5447/0 维持有效。⚠️ 临时诊断件（\_r38debt\_\*、\_r39\_db\_probe.py、R37 系列等）仍因 DeleteFile 工具失效（recordFileDelete returned false，本轮复测依旧）按仓库惯例留存。



> **最近校准：2026-10-03（提示词模块 R38 遗留技术债五项收口 · R38-D）** —— **【R38 条目「仍未修复」清单逐项落地】D2 整章重写超长硬上限 · D4 事实预算单一事实源 · D6 零占位显式声明+死代码删除 · D7 AST 盲区静态解析化 · 契约表全注册表覆盖**（零新增依赖、零新增配置项、零数据迁移、全部生产行为向后兼容或经核零行为变化）：

> ① **【D2 · 修复侧截断纪律】扫描侧 `PER_SECTION_LIMIT=6000` 截断 vs 修复侧**有意不截断**（定点编辑要逐字抄 old_text、整章重写会拿输出覆盖 sections.content，截断输入=用半截内容覆盖整章）此前只存于口头：`consistency_edits.collect_repair_edits` L209 落「勿改成截断」注释锁；真正补齐的防护是 `repair_agent.REPAIR_REWRITE_MAX_CHARS=12000`（对齐扫描侧 `SECTION_CHUNK_LIMIT`）—— 超长章在重写兜底**前**直接返回原文（模型单轮输出必被截短 → `validate_repair` MIN_LEN_RATIO 必拦 → 旧行为每次白烧一次大调用），用户可见结果（failed 状态）逐字不变，仅省必败调用；定点编辑路径不受影响（输出是编辑列表非整章）。

> ② **【D4 · 魔法数五处分叉】`project_facts` 注入预算 1500/1000 散落五处**：新建 `PROJECT_FACTS_LIMIT_OUTLINE=1500`/`PROJECT_FACTS_LIMIT_SUBLEVEL=1000` 单一事实源（**数值逐字沿用旧值，零行为变化**）；并把两处「此前无人说明的**有意不截断**」显式化：审核（`outline_review_system`，只读核对事实越全越准）与短方案主目录（规模小全量收益大于成本）各落注释 + 护栏静态扫 project_facts 行不得残留字面量切片 + 常量真被消费（防只定义不接线空断言）。

> ③ **【D6 · 空声明语义 + 死代码】`requires=[]` 在旧判据下恒等于「不校验」**：`_reg` 的 `sorted(requires) if requires else None` 使 `if not requires` 无法区分「未声明(None)」与「显式声明零占位([])」→ 12 个零占位 system/共享块模板全部脱管。现 `check_prompt_variables` 改 `requires is None` 才跳过、`validate_prompt_content` 的 `or []` 压扁保留区分；10 个零占位模板（SHARED_*×4 + consistency_*×5 + review_autofix_system）登记进契约表作**显式空声明** —— 日后谁往零占位模板里加 {x} 即启动期漂移告警 + 前端编辑告警。连带修一个**时序 bug**：`SHARED_*` 在 `prompts/__init__.py` 注册、晚于 `_registry` 尾部首次 `_apply_variable_contracts()` → 空声明永不下发 meta（首版护栏当场抓出），注册完成后幂等补套一次。死代码：`image_engine.generate_illustration_prompt/_arrange` 两零调用函数（全仓 grep 仅命中 def）+ 专属模板 `ILLUSTRATION_PLAN_SYSTEM/ARRANGE_SYSTEM` 删除（在役配图链路消费的是 `ILLUSTRATION_PROMPT_OPTIMIZE`，未动），悬空引用护栏改走 AST（ImportFrom/Name/get_prompt 字面量实参，文本扫行会被自身 docstring 说明误报）。

> ④ **【残余未登记模板归零】实测仅剩 3 个**（上一轮报告记 11 个，并行会话已接大部分）：`consistency_repair_edits_user`（5 变量，`build_edits_prompt` 全传）/ `facts_finalize_system` / `facts_knowledge_patch_system`（`facts_enrich.py` 核过全传）登记后 **`requires is None` 的模板全仓归零 —— 契约表首次达成全注册表覆盖**；治理护栏 `test_contracts_are_non_empty_and_registered` 两条旧断言（「契约不得为空」「SHARED 未登记」）按新口径更新（空=合法显式声明；未声明跳过红线由临时伪模板用例担守）。

> ⑤ **【D7 · AST 护栏盲区消化】`**kwargs` 展开调用点不再整体跳过**：新增 `_resolve_star_expr` 静态解析三种在仓形态（Dict/IfExp 字面量、同模块 helper 函数体递归展开含下标赋值与嵌套调用——getattr/bool 等未知调用**跳过而非判盲**、局部变量下标收集文件级过近似），D7 点名的 5 个零覆盖调用点（outline_review/short/level1/content_generation/content_continue）全部接入校验；真解不了的改由**盲区快照锁**登记（仅剩 charts.py 动态 key×2 + json_response.py 动态 key×1，均有替代护栏担守），新增盲区即红。**新不变量**：helper 内部任何普通函数调用若被误当「不可解析」会把 5 个点重退化为盲区 —— 首版实跑当场暴露（getattr 调用使解析整体返回 None）。

> 护栏：新增 `tests/test_prompt_techdebt_r38_20261003.py` **27 例**（D2 超长拦调用/正常章两次照发/编辑侧 30000 字原样进提示词行为锁 + D4 值锁/静态扫/消费数 + D6 删除/悬空 AST/10 键空声明/check 零漂移/**承重例：往零声明模板出厂基线加占位符必被检出**/未声明跳过语义（临时伪模板）+ 3 模板契约双向一致 + 全注册表覆盖完整性（排除探针键防串扰））+ `test_outline_generation_fixes_20260926.py` **+3 例**（helper 展开漏传可检出/盲区快照/5 点不再盲）+ 治理 2 例更新。A/B 五项全部定向失败（A1 改上限值→2 例 · A2 检查判据回退 `if not requires`→1 例 · A3 解析器改恒返回 None→3 例 · A4 改 D4 常量值→1 例 · A5 摘 __init__ 补套 apply→5 例（含 SHARED×4+治理）），finally 全还原 + `MUTATED_AB` 全仓 grep 零残留。⚠️ **本轮踩坑**：① SearchReplace 跨块替换把前一段 docstring 闭合 `"""` 吃掉 → 语法错误直到 pytest 收集才暴露（**编辑后必须立即 py_compile**，IDE linter 对本工作区中文全角字符误报不可依赖）；② A5 首版变异写成了「注释前缀式无效变异」（调用仍在场）—— 变异必须先看目标用例**确实定向失败**才算成立，无效变异会给出虚假安全感；③ 契约表旧注释「未列入=避免误报」的保守策略在本轮收口后失效（全覆盖），后人勿再把新模板留白。前端零改动。关联回归（repair/consistency/prompt/illustration/image/outline_checkpoint/facts/sse_route/inline_charts/json_repair）1353 passed；后端全量 **5447 passed, 16 skipped, 3 xfailed, 0 failed（211.65s）**（上轮基线 5417/0，新增 30 例全绿）。

>

> **最近校准：2026-10-03（图表模块 R38 遗留清单五项收口 · R38-L）** —— **【上一轮图表深度探索报告遗留项逐项落地】AI 配置 flaky 隔离 · 手动保存接围栏补齐 · has_inline_charts 口径收紧 · 幽灵登记核查 · 运维核实**（零新增依赖、零新增配置项、零数据迁移、生产判定口径只改一处且经零消费核实）：

> ① **【遗留① · AI 配置既有失败归零】`test_build_candidates_marks_broken_key` 全量失败但单跑通过的顺序依赖 flaky**：根因探针（`tests/_r38_probe_flaky_repro.py`）实证 `_order_candidates` 的「死配置剔除/主配置后置」依赖进程级 `_provider_reliability`（由同文件其它测试直接写入、或启动预热从审计日志近 24h 真实失败史加载），全量套件中 deepseek 历史低成功率把主候选后置/剔除 → `cands[0]` 断言定向失败。**生产代码零改动**（排序是编排层合理行为）：`TestKeyBrokenGate` 加 autouse fixture monkeypatch 关闭 `_is_dead_provider`/`_provider_reliability_snapshot`，另加回归锁用例向 `_provider_reliability` 注入死亡统计（finally 必还原）断言隔离后首位仍是主配置；排序规则自身由既有测试显式注入态另行锁定。整类 7 passed、整文件 29 passed。

> ② **【遗留⑤ · 手动保存接未闭合围栏补齐】`sections.py` update_section 正文分支**在竞态守卫后、word_count 计算与图表登记**之前**接入 `content_utils.auto_fix_unclosed_fences`（与生成链路 `_persist_section` **同一函数**同口径，幂等/仅追加/失败降级不阻断保存）；护栏 +2 例（未闭合→落库前补齐且正常登记 / 已闭合→逐字节不动）。⚠️ 首版用例拿单边 `A --> B` 被 `_validate_inline_chart` 拒导致方向误判，换用既有 VALID_FLOWCHART。

> ③ **【遗留④ · has_inline_charts 口径收紧】「未闭合=不是图」第四处统一**：登记/导出/改写三侧 R38 已跳 eof/truncated，判定侧 `has_inline_charts` 仍按「围栏存在」计数；全仓核实当前**生产零消费**（仅护栏测试引用）后收紧为仅闭合且属图表家族的围栏返回 True，零行为影响面；新护栏用例双向往返（未闭合 False / 闭合 True）。

> ④ **【遗留③ · 幽灵登记核查】生产库 `chart_predictions` 实测 0 行**（88 章节/有正文 0，历史污染行已随库重建消失，同 R36 `preflight_runs` 结论）→ 无需数据清理，登记侧 eof 跳过 + 护栏即闭合；只读探针 `_r38_ghost_probe.py`（mode=ro，envelope-aware 语义比较）在位兜底未来回归。

> ⑤ **【遗留② · 运维】**端口 8000 无监听 → 下次 `start_all.bat` 即加载新代码；清理挂死的遗弃 A/B 变异脚本（`_ab_r38_flakyfix.py`，PID 7228）与退出阶段挂死的背景 pytest 僵尸；本轮触及文件 grep `MUTATED` 零残留（历史测试文件中 `"__MUTATED__"` 字符串为合法测试数据）。

> ⑥ **【全量中捕获并修复 · 与并行会话同题共收敛】facts 提示词封顶失效**：`_join_with_budget` 在 n=200 时被分配器「每项保底 2×等比」拖垮（quotas 总和达 2×budget，总长 6199 > CAP 3000）且 quota=0 分支整行保留 —— 新增**仅在超支时介入**的消费侧纠偏（均匀硬上限，不丢条、少量条目等比份额不受影响）；护栏 +2 例（≤CAP 且 200 条全在 / 小 n 逐行等于分配器份额），A/B 摘纠偏 → 2 例定向失败。与并行会话的 P1-e 修复在全量套件中字节级合并无冲突。

> 护栏合计：新增/改造 8 例（AI 配置回归锁 1 + 手动保存 2 + has_inline_charts 1 + facts 封顶 2 + 既有 cap 例转正）。**A/B 反向验证 3 项全部定向失败**（摘 fixture 隔离 / 摘 sections 接线 / 摘超支纠偏），均即时还原 + grep 零残留复验。⚠️ 本轮踩坑：① **并行会话在 pytest 全量运行中途修订测试文件** → 报告行号与磁盘内容错位（显示已修的旧断言），两次被误导；判定手段是比对文件 LastWriteTime 与 pytest 起止时刻 + 单文件复跑，而非盯失败文本；② 遗弃的 in-place A/B 脚本进程可能延迟醒来二次变异源文件，弃用脚本路线后必须 `Get-Process` 核实并 kill；③ PowerShell 双引号内反引号是转义符，`python -c` 探针喂 mermaid 围栏样本会被吃字符（探针失真非代码失真），改用脚本文件 + 落盘输出。

> **基线对比**：上轮全量 5329 passed/1 failed（AI 配置 flaky）+ 图表域 883 基线 → 本轮首次全量 5409/2（新暴露封顶 + 旧版文件错位两失败，均已处置）→ 最终全量 **5417 passed, 18 skipped, 3 xfailed, 0 failed（228.82s）**；关联回归（inline_charts/deep_audit/registration_parity/ai_facts/prompt_r38/crosscut/ai_config_enhance）全绿；前端本轮零改动（上轮 tsc/vitest 全绿维持有效）。

>

> ⚠️ **【本轮最严重的非代码缺陷】行尾损坏 + 无备份的救援不可达**：修 `review_autofix.py` 的恢复脚本用了 `text.split("\n")` + 写回时 `payload.replace(b"\n", b"\r\n")` —— split 后的每行**已带尾随 `\r`**，replace 又把 `\n` 换成 `\r\n`，每行变成 `\r\r\n`；后续归一化把 `\r` 当独立换行，**501 行的文件被撑成 1994 行**（866 行 = 433 内容行 + 433 空行，显式续行 `\` 被空行打断 → `SyntaxError: expected ':'`）。很不幸于本轮只备份了提示词模块的 4 个文件，**漏了 review_autofix.py** → 损坏后无法直接回滚，只能靠「内容行数 == 空行数」反推还原（归因后 AST 校验通过：15 个函数 / 5 个路由函数 / `skipped` 唯一初始化且在循环外 / 返回体字段齐全 + 99 例相关回归全绿）。**行尾自适应只能用 `splitlines()` + `"\n".join()` + 一次性换算**，**绝不能 `split("\n")` 后再对含 `\r` 的行做 replace`**；任何对整份源码做行级批量重写的脚本，**改前必须留字节级备份**（本轮教训：没备份的文件损坏后修复成本高、而且可能失败）。
> **最近校准：2026-10-03（提示词模块专项审查 · R38）** —— **【模板/变量契约/上下文组装/输出解析全链】共享简档断链 · compliance 校验消费字段错位 · 非目录任务误用目录修复提示词 · 子层目录丢编制依据 · 事实注入头部切片 · 审核提示词悬空指令 · 安全关键标记被预算切碎**（零新增依赖、**零新增配置项**、零数据迁移、返回契约零变化；既有默认开关一律不动）：

> ① **【P1-a · 共享红线简档断链】改简档对正文永远不生效**：`content.py` 在**导入期**做 `.replace("<<SCOPE_RULES>>", SHARED_SCOPE_RULES_BRIEF)` 生成值拷贝 —— 用户在提示词后台改简档，改的是注册表里的值，正文模板里早已固化的字符串纹丝不动（典型「改了没反应」）。改为在 `prompts/__init__.py` 用 `_reg` 把 `SHARED_SCOPE_RULES_BRIEF` 注册为**可编辑提示词**，两个正文模板改用 `{SHARED_SCOPE_RULES_BRIEF}`，由 `_cache._resolve_shared_keys()` 在运行时解析 → 用户改动当场命中；**默认渲染逐字不变**（探针 `_probe_scope_rules_r38.py`）。正文用简版（token 成本）、目录用完整版，术语一致性由既有测试锁定。

> ② **【P1-b · 校验器与消费端字段错位】**`compliance.py` 的 `_validate_consistency_audit` **不检查** `issues`，而 `run_consistency_audit` 消费的是 `obj["issues"]` → AI 只回 `{"score":..}` 时校验通过、下游空扫描/异常；`_validate_check_results` 不拒空壳行（`{"item":"","hit":false}` 会混进审核结论）。两条硬校验补齐；`expert_review` 采用「消费端 `_normalize_expert_review_result` 归一 + 告警」而非硬校验（避免既有 provider 的自由格式输出被整段拒掉）。三处 AI 调用补 `json_mode=True, temperature=0.2`。

> ③ **【P1-c · 非目录任务误用目录修复提示词】**`collect_json_response` 默认 `repair_key` 是 `outline_json_fix_system`（目录专用「把目录 JSON 修好」指令），内容/事实/审核等非目录任务拿到的修复提示词方向完全错。新增 Schema 自适应的 `json_schema_fix_system` + `GENERIC_REPAIR_KEY` 作默认；目录族 3 个调用点（`sse_handlers.py` / `sections.py` / `upload_outline.py`）显式传 `OUTLINE_REPAIR_KEY`，配 AST 护栏防新增调用点退化。

> ④ **【P2 · 修复轮加固】**修复目标恒为 `first_raw`（此前传 `raw or ""` = **上轮**结果，等于让模型基于自己上一轮的错输出继续修）；修复轮 `chat_with_fallback` 包 try/except 并并入「修复轮异常」（瞬时故障不再丢弃首轮成果，首轮异常仍上抛）；`_provider_chat_safe` 此前**没有**透传 `max_tokens`/`extra_body`（调用处写了、函数签名收了、又没往 provider 传）→ 补真正透传 + 旧签名逐个降级；`collect_json_response_with_provider` 新增 `repair_key`。

> ⑤ **【P1-d · 子层目录丢编制依据】**`outline_sublevel_system` / `outline_sublevel_batch_system` 未注入 `standards_text` → 二、三级目录生成看不到编制依据。补独占行 + 契约登记。

> ⑥ **【P1-e · 事实注入头部切片】**`compliance.py` 用 `"\n".join(lines)[:FACTS_PROMPT_CAP]` 头部硬切片 —— 事实条数一多，第 N 条之后**整条消失**，模型看到的是「只有关键前半段事实」的错觉，进而在正文里补出并不存在的【待补充】；而一致性链走 `sse_handlers._allocate_char_budgets` 按比例分配，**同名变量 `{global_facts}` 在两条链上方向相反**（仓库自己已在该函数注释里记录过同一教训）。新增 `_join_with_budget` 复用同一分配器（导入失败降级旧行为，fail-soft），并补**消费侧超支纠偏**：分配器的「每项保底 min_chars」在 n 大时让 quotas 总和可达 `2×budget`，「总量封顶」承诺会被破坏，故仅在按 quota 截完仍超支时才介入均匀硬上限。

> ⑦ **【D5 · 审核提示词悬空指令】**`outline_review_system` 模板里固定写死「8.1 目录是否覆盖上方【审核检查点前置要求】列出的全部必备法定章节」，而那份清单由 `_outline_checkpoint_kwargs()` 注入、**受开关控制** —— 开关关闭时清单根本不存在，指令却仍要求模型逐条核对（无从判定 → 产出假 `passed=false`）。改为独占行 `{outline_checkpoint_audit_hint}` + `_outline_review_checkpoint_kwargs()`：块为空就返回空字典，**指令与清单同生共死**；生成链路不携带审核语义指令。⚠️ 刻意**不**选「软化措辞」方案 —— 那会削弱默认开启态的检查力度。

> ⑧ **【E2E 新发现 · P2 · 安全关键标记被预算切碎】**120 条事实下每条配额仅 `budget//n*2`，`（安全关键）` 被切成 `（安` —— 模型既读不到「安全关键」三个字、还会读到半个括号，而安全关键恰是预算压力下最不该丢的信息。新增 `SAFETY_MARK` / `_drop_partial_suffix`（裁掉被切碎的后缀残段，避免拼出 `（安（安全关键）`）/ `_keep_suffixes`，`_join_with_budget` 增加**纯增量**参数 `suffixes`（缺省 `None` → 与引入前逐字一致，既有调用方零影响）。

> **护栏**：新增 `tests/test_prompt_module_r38_20261003.py`（63 收集 / 50 passed / 13 skipped；13 个 skip 是 AST 护栏对**非目录族**场景的显式跳过，不是静默失效）+ 新增 `tests/test_prompt_e2e_crosscut_r38.py`（**26 passed**，解析提取→目录→全局事实→正文→图表→审核预检→导出 DOCX 七段横切 + 4 项横切断言）+ 改造 `tests/test_scheme_repositioning_20261001.py`。**A/B 反向验证 16/16 全部定向失败**（A1–A16，覆盖简档运行时消费/简档注册/consistency 缺 issues/空壳行/事实按比例分配/子层 standards_text/目录族显式 repair_key/first_raw/修复轮异常/provider 丢参/通用默认 repair_key/D5 接线/D5 独占行/安全标记保底/残段裁剪/未超预算早退），每次变异还原写在 `finally` + 还原后 sha256 字节一致断言，`_r38_residue.py` 终态扫描 `dirty: []`。**基线对比：5329 passed / 1 failed（既有失败属并行会话 AI 配置域）→ 5417 passed / 0 failed，18 skipped（+13 为本轮 AST 护栏），3 xfailed 不变，208.51s。**

> ⚠️ **本轮踩坑**：① **期望值写错被全量回归当场拦下** —— `test_suffixes_none_is_passthrough` 原写法拿「超预算输入」断言输出等于未截断的原拼接，而 `total > budget` 时截断**本就是** P1-e 的目的，cap=(3000-39)//40=74 与失败 diff 的 74 完全吻合；已拆成「缺省/None/[] 三者一致」+「未超预算逐字早退」两段各自可判别的断言，并补 A16。② **A/B 脚本自重启漏传 argv** —— `_ab_r38_one.py 16` 静默跑成了默认用例 A5，一度被误读成「A16 失败」，已修 `subprocess.call(... + sys.argv[1:])`。③ **`read` 工具返回陈旧缓存快照**：一次读到早已还原的 `# MUTATED_AB` 变异行并据此误判「源码泄漏」，而 ripgrep 与字节级 sha256 探针双双证明磁盘干净 —— **判断源码是否被污染必须用 ripgrep/字节探针，不能只信 `read`**。④ CASES 续行是 **5 空格**缩进，按 4 空格写锚点恒 `ANCHOR-MISS`。⑤ 背景 A/B 与源码编辑**发生重叠**：A5 报 ANCHOR-MISS 正是 A14/A15 变异刚改完该文件的时刻，已用 `_ab_r38_one.py 5` 单独复跑确认（定向失败成立）。

> **遗留技术债（记录以免重复排查，均未修）**：**D2** `repair_agent.py:171` / `consistency_edits.py:209` 无章节正文截断，而 `consistency_scanner.py` 有 `PER_SECTION_LIMIT=6000` —— 截断可能让整章被覆盖成半截内容；**D4** `project_facts` 截断长度不一致（`_try_outline_patch` 1500 / level1 1500 / review 不截断 / `_fetch_unit_children` 1000）；**D6** 8 个零占位符 system 模板 `requires=[]` 在 `_registry.py` 四处被当作「不校验」，其中 `ILLUSTRATION_PLAN_SYSTEM` / `ILLUSTRATION_ARRANGE_SYSTEM` 是死代码；**D7** AST 护栏对 `**kwargs` 展开的调用点全部跳过（`test_outline_generation_fixes_20260926.py:140-151`），5 个调用点零覆盖；另有 11 个模板未纳入 `PROMPT_VARIABLE_CONTRACTS`（建议 requires 已备好，见 `test_prompt_module_r38_20261003.py`）。**已核实不是缺陷**：`{max}` 是 LaTeX 示例误报、`prompt_templates` 表 0 行、大小写不敏感匹配有效、一致性审计链的事实变量名本就是 `{facts}`。

> ⚠️ **环境限制（非代码缺陷）**：本机 Mermaid 渲染后端不可用（实测「所有渲染后端均不可用」），故端到端图表段改为「管线契约 + 合成 PNG 插入 + R35 降级不占图号」验证；真实 AI 联调依赖本地 provider 配置与额度，本轮未验证。

> **最近校准：2026-10-03（遗留问题六项收口 · R37）** —— **【R36 报告遗留清单逐项落地】CON-SCAN 派生编号回退 · 孤儿 API 清理 · Dashboard/同章多条 UI 补锁 · preflight_runs 清理工具 · 运维重启核实**（零新增依赖、零新增配置项、零数据迁移、返回契约零变化）：



> ① **【L3 · CON-SCAN-N 收口】族前缀派生编号接回注册表**：`compliance.py` 总检聚合产出的 `CON-SCAN-<n>`（一致性扫描未解决冲突，mode=program 自带维度/严重度）此前不在注册表 —— `XXX-NN` 格式红线（`^[A-Z]{3}-\d{2}$`）使族串不能直接登记，且 `test_review_status_decouple` 锁死生成侧字面量 → **生成侧字符串与历史落库行一字不动**，注册 `CON-07`（consistency/program/medium）+ 新增 `_RULE_ID_ALIASES = {"CON-SCAN": "CON-07"}` 接入 `_resolve_base_rule` 剥离链，「规则说明」抽屉与 autofix 归一链由此命中；`capability_of("CON-SCAN-1")` 归一前后文案同为 CON 族兜底 → **用户可见行为零变化**（护栏锁死）。`RULE_VERSION` 1.8.0 → **1.9.0**。



> ② **【L4 · 孤儿 API 清理】`api/index.ts` 删除 4 个 `@deprecated` 零消费方法**（consistencyHistory / dimensions / preflight / statuses）：删除前全仓复核零消费（含测试），后端端点保留供脚本诊断，原位留注释；`tsc --noEmit` 零错误。



> ③ **【L5/L6 · UI 双保险补锁】**`ReadinessDashboard.test.tsx` +3 例「自动修复入口接线」（可修行→打开 AutoFixModal→定位按钮按该行 rule_id 调 plan（section_id:undefined）/ 不可修行→「需人工」Tag 无按钮且批量按钮不出现 / block+fixable→「一键修复全部阻断项」发 `collect(scope:"all_blocking")`）；`BatchFixModal.test.tsx` +2 例**同章两条**链式（`itemKey = rule|section` 防塌化：checkbox 数=2、勾选独立性、部分接受走 `accept:["DLV-05"]`）——此前 STAGE_MULTI 只覆盖异章形态，同章 key 语义无锁。



> ④ **【L1 · 数据清理工具】`tools/cleanup_preflight_runs.py`**（仿 gc_orphan_doc_trees）：**11 列全等**才算「零信息重复」保留最早（min rowid），AI 数值波动行一律保留；**dry-run 默认** + `--apply` 前 sqlite backup API 在线快照 + `DELETE_HARD_CAP=2000` 超限中止 + `--before` 时间窗。生产库实测 **0 组污染行**（重建后仅存 2 行，逐列 diff 证实是 10-01 两次真实不同的总检）→ R36 假记录已随库重建消失，工具在位兜底未来回归。



> ⑤ **【L2 · 运维核实】** 端口 8000 无监听、无后端 python 进程 → 重启提醒天然满足，下次 `start_all.bat` 即加载新代码。



> 护栏：新增/扩充 `test_audit_rules_namespace_20261003.py` **13→17 例**（+TestFamilyPrefixAlias 4：别名解析/别名目标必须真实注册/capability 行为零变化锁/静态扫 `XXX-YYY-<n>` 族前缀必须已登记或建别名）+ `test_cleanup_preflight_runs_20261003.py` **12 例**（真 schema 建表：判据 5 + 时间窗 2 + CLI 安全 5）+ 前端 +5 例。**A/B 反向验证 4 项全部定向失败**（A1 摘别名分支→namespace 1 例 / A2 摘 stats 列→cleanup 1 例 / A3 放宽 blockingFixable 门控→Dashboard 1 例 / A4 itemKey 退化为裸 section_id→BatchFix 1 例），finally 还原 + 字节一致 + AB_MUTATED 全仓零残留。后端全量 **5277 passed, 5 skipped, 3 xfailed, 0 failed（245s）**；关联回归 112 passed；前端审核模块 6 份 **135 passed** + `tsc --noEmit` 零错误。⚠️ **本轮踩坑**：① 源文件实为 **CRLF** 而 Python `read_text` 通用换行把 CRLF 显示成 LF —— A/B 字节级 `read_bytes` 锚点匹配恒 0，`read_text` 结果不可作为字节层依据，变异脚本必须做**行尾自适应**；② SearchReplace 删孤儿 API 时**误伤两处无关 SSE 正则**（字面 `\n` 被展开成真实换行，tsc 报 10 个语法错误当场拦截），用 node `.cjs` 脚本逐字恢复 —— 编辑后必须跑 tsc 而不信工具自报成功；③ DeleteFile 工具在本工作区持续失效（recordFileDelete returned false），本轮临时诊断件（backend/\_ab\_r37.py、\_diag\_\*、\_probe\_\*、\_r37\_full\_\*、frontend/\_ab\_r37\_fe.cjs、\_fix\_sse\_regex.cjs、\_r37\_tsc.txt、根目录 \_proc\_probe2.ps1 等）按仓库既有惯例留存。



>



> **最近校准：2026-10-03（审核与预检 · 规则命名空间与修复链路数据污染收口 · R36）** —— **【审核规则通道分叉 + 分数趋势污染】STD-05 注册为 ai 但被程序化产出；自动修复三端点重算时每次灌一条 preflight_runs 历史**（零新增依赖、零新增配置项、零数据迁移、返回契约零变化）：



> ① **【P1 · 判据分叉第四份副本形态】STD-05 的「声明通道」与「实际判定」矛盾**：`preflight_engine.check_standards`（L446，按 `CATEGORY_STANDARDS` 归一化比对编号，零 AI）与 `content_checkpoint` 两条程序链路一直在判 STD-05，而 `audit_rules` 却注册 `CHECK_MODE_AI`（STD-01~04 全是 program，唯它例外）→ `ai_rules()`（`/check` fallback 清单唯一来源）把已判规则再送一遍 AI，违背 `ai_checklist` docstring 自己声明的「程序已判不送 AI」原则（浪费 token + 可能矛盾结论；双通道各报一份又被 `merge_findings` 按 rule_id 去重掩盖，用户无从感知）。修法：mode 改 `CHECK_MODE_PROGRAM`（只改声明不改实现，判定行为零变化）。连带发现同族漂移：`_PROGRAM_EMITTED_RULE_IDS`（契约 = 「引擎实际产出全集」）虚登引擎从不产出的 SAF-01/02/07（三条本就是 AI 语义规则）、又漏登 2026-10-03 新产出 SAF-08 时违背 L529 维护约定 —— 两个方向的失真都会削弱「产出⇒program」新不变量的判据力，一并修正。



> ② **【P1 · 新不变量】`validate_rule_registry` 此前只验「产出编号可解析」不验「通道一致」** —— STD-05 这类漂移潜伏至今的根因。新增：引擎产出的规则若 mode≠program 即报问题；同时 `RULE_VERSION` 1.7.0 → **1.8.0**（/check 默认清单变化属行为修正；已落库的历史结果 `rule_id`/维度/评分口径零变化）。`review_autofix._CAPABILITY["STD-05"]` 的 ai 修复能力与规则 mode 无关（修复方式≠判定通道），不受影响未改。



> ③ **【P1 · 数据链污染】`routers/review_autofix.py` 的 `_resolve_finding`（/plan /apply 共用）、/collect、/stage 直接调 `compliance._readiness_overview_compute` 重算 findings，而该函数末尾无条件 `_persist_run` 落 `preflight_runs`** → 用户每点一次「定位/修复/收集/暂存」就往分数趋势灌一条总检历史（污染 G2 要保护的趋势线），且不持 `_overview_lock` 存在并发写竞态；/collect docstring 自述「不落库」实为撒谎。修法：`_readiness_overview_compute` 加 `persist: bool = True` 关键字参数（**默认 True，/overview 端点调用逐字不变** —— 向后兼容红线），autofix 三处传 `persist=False`；只读重算不落库，两个问题同时消除且不改任何判定口径。



> ④ **端到端验证方式**：无新增端点/契约字段；用户可见变化 = 修复链路不再产生假总检记录（历史趋势更干净）+ /check 默认清单不再含程序已判的 STD-05。⚠️ **运维提醒**：需重启后端进程后新代码才生效（同 R34 教训）。



> 护栏：新增 `tests/test_audit_rules_namespace_20261003.py` **13 例**（STD-05 通道归属/登记集诚实性/AST 静态扫引擎产出集合↔登记表**双向相等**（含 CMP 族展开、docstring 排除防误报）/mode 不变量内置 A/B（把 STD-05 改回 ai 即定向报问题，还原后健康断言兼做残留检测））+ `tests/test_review_autofix_no_persist_20261003.py` **8 例**（persist=False 零写入/缺省照落库（兼容红线）/三端点重算必带 persist=False/接线 parity 静态锁（/overview 本体不得被顺手改掉）/签名 KEYWORD_ONLY 默认 True）。**A/B 反向验证 2 项**：路由三处去掉 persist=False → 4 例定向失败；STD-05 变异回 ai → 8 例定向失败（含既有 registry 健康锁被新不变量接管）；均还原后字节级零残留复验全绿。前端 `BatchFixModal.test.tsx` 补 **5 例**组件级交互（部分勾选接受走 accept 数组/全不选禁用/全选切换/失败项无勾选框/收集失败兜底）——此前只测 accept_all 与拒绝全部两条极端路径，前缀链式语义的 UI 入口无锁。后端全量 **5241 passed, 5 skipped, 3 xfailed, 0 failed（302s）**；关联回归 12 份 302 passed；前端审核模块 6 份 **60 passed** + `tsc --noEmit` 零错误。



>



> **最近校准：2026-10-03（图号虚跳 · 损坏图片回退收口 · R35）** —— **【导出层图号分配 · 残留边缘情形收口】图字节「格式合法但文件损坏」时 `doc.add_picture` 在占号之后抛异常，被旧实现函数内 `except` 吞掉并写出「图 X-Y — 插入失败」红字（图号已占用）**（零新增依赖、零数据迁移、零新增配置项）：



> `_add_inline_chart_from_bytes` 与 `_add_illustration_from_bytes` 改为**插入成功才返回 `True`、失败返回 `False`**，不再内部写红字；调用方（`write_section` 的 chart / image 分支）在**成功后才递增 `figure_counters`**，失败（默认模式）则**回退图号 + 回收孤儿引导语**，彻底消除「占号却无图」的错号/虚跳。`chart_fail_placeholder=True` 占位模式保留红字（图号照占、可见错误），与 9-26/27 既有「决策在占号之前」纪律互补。



> 护栏：新增 `tests/test_figure_number_no_skip_on_render_fail.py` **9 例**（场景一 渲染失败不占号 4 例，含 PPM 确定性复现 WEBP/AVIF/HEIC 类「能解不能插」路径 + 正向对照；场景二 删除图表紧凑重排 + 指纹失效 2 例；可修复项 损坏图片回退 3 例，monkeypatch `doc.add_picture` 抛异常确定性模拟损坏）。**A/B 反向验证**：临时禁用两处回退 → 2 个默认模式用例定向失败（旧行为占号留字）、还原字节一致；关联回归 6 份 **145 passed**。



>



> **最近校准：2026-10-03（全局事实 · SSE 提取事实 422 收口 · R34）** —— **【P0 用户可见症状】「提取事实」SSE 请求失败: 422**（零新增依赖、零新增配置项、零数据迁移）：



> ① **根因日志实证**：`logs/backend.log` 中 `POST /api/v1/sse/generate-facts/{scheme_id}` 共 **10 次 422**（10-01 14:07 ×2 / 19:41 ×4 / 19:46 ×1 / 21:24 ×1；10-02 23:19 ×1 / 23:33 ×1，全部 1~22ms = FastAPI 参数校验拒绝，未进业务逻辑）。根因是 10-01 事故的**装饰器错位**（`db` 形参无 `Depends` 默认值被当必需查询参数 → 恒 422）；装饰器已归位 `generate_facts`（AST 护栏 `test_sse_route_decorator_20261001.py` 4 例在位），本轮实测当前代码端到端 200 + event-stream，**当前代码不存在该 422 路径**。



> ② **本轮新修 · 症状零线索**：FastAPI 422 响应体是 `{"detail": [{loc, msg, type}, ...]}`（**数组**），前端 `sseFetch` 旧实现只认 `detail` 字符串 → 用户只看到「SSE 请求失败: 422」，无法定位缺哪个参数（10-01/10-02 事故全靠翻后端日志定根因）。新增 `describeSseHttpError` + `readSseHttpError`（`frontend/src/api/index.ts`）：`path/query/body` → 路径参数/查询参数/请求体字段，`Field required` → 「缺少必填X」，其余 FastAPI 消息原样透传、非 JSON 兜底默认文案；**两处** `!resp.ok` 分支统一接入（`sseFetch` 与 `sseGetStream` —— 后者此前连字符串 detail 都不解析）。



> ③ **端到端回归护栏**：新增 `tests/test_facts_sse_endpoint_no_422_20261003.py` **3 例**（httpx.ASGITransport 真实 HTTP、与测试同事件循环，规避 TestClient 跨事件循环持有 aiosqlite 连接的坑）：真实方案 → 200 + text/event-stream（显式断言**不得 422**）/ 不存在方案 → 404（不得落 422 形态）/ 路由被摘除 → 404/405。AST 护栏锁「装饰器形态」，本文件锁「用户可见症状」，两层互补（形态对但签名变仍可 422 的盲区由此覆盖）。



> 护栏：前端 `sseFetch.test.ts` **+2 例**（422 数组明细翻译 / 非 JSON 兜底）**12 passed**；`factsApi.test.ts` + `factsTab.test.tsx` **85 passed**；`tsc --noEmit` 零错误。后端新增 **3 例** + 事实模块关联回归（decorator / parity / parse-truncation / seven-module / dataflow）**174 passed**。



> ⚠️ **运维提醒（非代码缺陷）**：422 由**进程启动时加载的代码**产生 —— 代码修复后必须**重启后端服务**才生效（10-02 23:19/23:33 的 422 是长跑旧进程所为，其后 10-03 无任何 generate-facts 422 记录）。



>



> **最近校准：2026-10-03（全局事实 · AI 语义检查侧消费收口 · R33）** —— **【G3 断链 AI 侧收口】compliance `/check` 与 `/expert-review` 两条 AI 链路接入已确认事实**（零新增依赖、零数据迁移、零配置项、提示词契约加法式）：



> ① **review_autofix 的 AI 链路其实早已消费事实**（勘察发现记忆中「仍未消费」半句过期）：`routers/review_autofix.py` `/apply` 与批量侧均经 `consistency_scanner.build_global_facts_text` → `sse_handlers._build_facts_text`（其 `_load_facts_rows` 自 2026-09-29 P2 修复起已复用 `build_injectable_facts_query` 单一出口）→ 提示词 `{global_facts}` 占位符。本轮不改行为，只补 parity 静态锁防静默摘除（`test_review_autofix_facts_chain_parity`）。



> ② **真正断链的两条 AI 链路**：`compliance_check_system`（`/check`）与 `expert_review_system`（`/expert-review`）此前变量仅 scheme 名/类型/清单/正文，完全无事实。现统一经唯一只读桥接 `load_resolved_facts_for_scope` 装配（helper `_load_facts_prompt_text`，fail-soft：桥接异常/无事实降级为「（无）」，不阻断 AI 检查），格式化 helper `_facts_prompt_text`（单条 value 截 120 字、总量 `FACTS_PROMPT_CAP=3000`、单位缺失时补 `value_unit`、安全关键打标）。提示词各加一节「项目关键事实（用户已确认，最高优先级依据）」+ `/check` 判定第 6 条（正文与事实矛盾 → hit=false + high + suggestion 注明事实值）；`PROMPT_VARIABLE_CONTRACTS` 两处同步补 `global_facts`。



> ③ **真实 AI 联调已跑通**（生产库复制到临时副本，不动生产库、脚本已删）：`prompt_assembly`（事实文本 `- 基坑开挖深度：5.6m`）/ `check_real_ai`（2 results）/ `expert_review_real_ai`（score=10）三步全过。**关键证据**：正文写「约 5.5m」而事实为 5.6m，AI 的 STD-05 结论 evidence 明确引用「本项目实际开挖深度5.6m…已超过5m阈值」—— 新增判定第 6 条的「事实为权威值」语义被模型真实执行。



> 护栏：新增 `tests/test_compliance_ai_facts_20261003.py` **11 例**（契约表 + 格式化/封顶 + `/check` `/expert-review` 集成注入/门控 parity（模拟值矛盾值不进提示词）/无事实降级/桥接异常 fail-soft/接线静态锁/review_autofix parity）；后端全量 pytest 后台跑至 **[100%] 零 F/E 标记**（汇总行因进程末尾被截断未落盘，逐项结果完整），另 `-k` 关联回归 **631 passed**。



>



> **最近校准：2026-10-03（删除链路数据一致性 · R32）** —— **【删除项目 / 删除章节全链路清理】登记表漏条目 + 全局 `PRAGMA foreign_keys=0` 导致所有级联为空声明**（零新增依赖、零配置项、零数据迁移、返回契约加法式）：



> ① **不是没有清理代码，而是登记表漏了条目**：`delete_project` 有两本登记表（`_PROJECT_SCOPED_TABLES` / `_SCHEME_SCOPED_TABLES`），但 **`uploaded_outlines` 完全不在任何一本里** → 上传识别的目录（raw_text / parsed_json 整份留库）永久孤儿。生产库实证 **88/88 全部为孤儿**（`bid_analysis_items` / `bid_sections` / `project_documents` / `schemes` 均 0 孤儿，就它一个）。



> ② **【根因性】运行时 `PRAGMA foreign_keys` 恒为 0**（`app/db.py` 与 `schema_sql.py` 全仓零命中 `foreign_keys`）→ **所有 `ON DELETE CASCADE` 都是空声明**，删项目后二十余张派生表的「靠级联」假设**全部不成立**。schema_sql 里标 CASCADE 的只有 3 张表（`bid_analysis_items`/`scheme_snapshots`/`placeholder_baselines`），前两张碰巧已被显式删除掩盖，第三张漏删。修法：补显式 DELETE（`uploaded_outlines` / `placeholder_baselines`；`schemes` 保留在表内）。⚠️ **`PRAGMA foreign_keys=0` 本身刻意不修** —— 那是全局连接配置，开启后所有依赖外键的表行为都会变（含外键约束开始报错），属高风险全局变更；显式 DELETE 已让级联是否生效不影响正确性。



> ③ **四层存储目录树此前从无人清理**：`delete_project_docs_root`（`doc_storage.py`，递归清理整个 `data/projects/{pid}/documents/`）实现齐全，但 `delete_project` 的调用链**根本没调它** → 项目删除时磁盘四层目录树永久残留。本轮补上（try/except 包裹，失败只 WARNING 不阻断删除）。



> ④ **返回契约加法式扩展**：新增 `cleanup_errors`（逐表 fail-soft 失败的表名）与 `doc_trees_removed`；`ok` / `files_removed` **一字未动**，且实测前端**零消费** `files_removed`（全仓 `Select-String` 0 命中），故零影响面。



> ⑤ **磁盘实证**：`data/projects/` 下 **216 个目录** vs `projects` 表 1 行 → 215 个孤立目录（含 `.trash/` 回收站，属设计内）；四层存储目录**内容全空**（0 个非空文件）。



> 护栏：新增 `tests/test_project_delete_cascade_20261003.py` **14 例**（全量级联清理 5 + 静态 parity 3 + 清理失败回传 2 + 返回契约 2 + 静态防分叉 2）。**parity 护栏是本轮关键**：扫 `schema_sql.py` 自动找出所有含 `project_id` / `scheme_id` 列的表，与两本登记表比对 —— 未来新增表若漏登记即失败（**不是写死的字符串断言**）。**A/B 反向验证 4 项全部定向失败**（移除 uploaded_outlines 2 例 / 移除 schemes 显式删除 3 例 / 移除 placeholder_baselines 1 例 / 移除 consistency_scan_cache 3 例；单实例锁 + `try/finally` 逐次还原 + sha256 字节一致断言）。关联回归 8 份 **103 passed**。详见 §4.30。



> ⚠️ **仍未落地（记录以免重复排查）**：① 历史孤立数据未清理（生产库 88 行 `uploaded_outlines` / 215 个孤立目录，属数据迁移）；② `review_records.section_id` 在删章节后残留 —— 刻意不删（评审留痕是可追溯性硬要求）；③ 磁盘孤立目录无自动 GC，需区分回收站与真残留，建议单立子项。详见 §4.30.6。







> **最近校准：2026-10-02（CON-04 中文计量单位收口 · R30）** —— **【P1】占位符改写遗留孤立单位** + **生产 findings 过期性逐条核实**（零新增依赖、零数据迁移、零新增配置项）：



> ① **【P1】`_PLACEHOLDER_TRAILING_UNIT_RE` 只收拉丁单位，中文单位一个都没有**：占位符改写机制本身是对的（`【待补充】` → 条件式表述 `按设计文件及现场实际确定`，紧随其后的单位收成括号注记），但中文单位不被识别 → 直接连在条件短语后面，句子读不通。生产 findings 实证：`堆放区面积【待补充：堆放区面积】平方米` → 「…确定**平方米**」、`每周清运不少于【待补充：清运频次】次` → 「…确定**次**」。现补 `平方米`/`立方米`/`公斤`/`小时`/`米`/`遍` 与 `次(?!日)`。**`次` 必须带 `(?!日)` 守卫** —— `次日`（next day）是常用词，不加守卫会把「【待定】次日恢复施工」改成「…（次）日恢复施工」。⚠️ **刻意不补 `周`/`月`**：`周边`/`月末` 会以它们起首，误收风险高于收益 —— 该取舍按**行为**锁定（`test_week_and_month_words_deliberately_not_absorbed`），不锁正则字面量（避免后人改写法即误伤）。



> ② **生产 findings 大多是**过期数据（本轮逐条核实，无需改代码）**：上轮「生产库重建中」无法前后对比，本轮重新拉取最新一条 `preflight_runs` 的 20 条 findings 逐条核实：`STD-03`（报 6 个未收录编号 → 当前只报 **1 个**）、`CON-04`（7 种生产形态全部改写、0 残留、幂等）、`SAF-02/07`（生成侧检查点已含）、`STD-04`（已按 `is_hazardous_by_keywords` 门控）**全部为过期数据**。STD-03 的 6 个编号逐个核对：`GB 55034`/`GB 55032`/`GB 50210`（基号豁免）、`GB 50325-2020`/`GB 12523-2011`（在库）、`GB 18581`（**已被 GB 30981-2014 替代 → 该报**）。护栏 `TestStd03BaseNumberExemption` 锁两端：真实现行规范一律放行、作废与杜撰编号照报。



> 护栏：`tests/test_checkpoint_feedback_20261002.py` **52 → 71 例**（+19）。**A/B 反向验证**：删掉新增的两行单位分支 → **8 例定向失败**（7 个中文单位 + 1 个孤立单位残留扫描），还原后 **sha256 断言字节一致** 且 **71 全绿**。其余 13 例在变异下仍通过是**预期**（`次日` 守卫测试不依赖新分支，它保护的是守卫本身）。**后端全量 5036 passed, 4 skipped, 3 xfailed, 0 failed（195.22s）**。⚠️ 本轮踩坑（§5.16）：① 断言要**锚定实现、不要锚定字符** —— 用 `'\uXXXX' not in mut` 判「变异未生效」会被**注释里的同名示例**误伤，应断言待删片段本身归零（`flat.count(DEL) == 0`）；② **pytest 子进程在 Windows 下输出是 GBK**，`encoding="utf-8"` 会抛 `UnicodeDecodeError` 致 `r.stdout is None`，若**还原步骤写在读取输出之后 → 变异残留进主源码**（本轮实际发生一次，靠事后比对字节数才发现）—— 还原必须与读输出**解耦**、放 `try/finally` 且先于读输出。详见 §4.28。



> ⚠️ **仍未完成**：生产库**前后对比测量**仍阻塞 —— `backend/data/scheme_assistant.db` 本轮复查时**仍在重建**（88 章节 / 有正文 0 / 总字数 0），最后一次 preflight 仍是 2026-10-01 15:55。正文全空时跑 preflight 会得到无意义结果，故不做前后对比；判据为 `with_content > 0` 且 `total_words > 0`。但 ② 的逐条核实已达同一结论：**当前代码已正确处置该批 findings**。







> **最近校准：2026-10-02（检查点反哺 R29）** —— **【检查点反哺 · 剩余 P0/P1 收口】生成即按审核标准执行**（**判据同源**：生成侧直接 import 预检侧判据函数 / 常量，**不在生成侧重抄正则与阈值**；全部新增检查零 AI、fail-soft、只读，1 个新开关默认开）：



> ① **【P0 · 判据分叉】TRC-01 只有「词」没有「过程」**：预检 `check_traceability` 判的是**公式与参数代入过程**（`_FORMULA_RES`，**block**），生成侧只判「计算」这个**词** → 词过而过程没有时生成侧自检全绿、预检照报 block。新增 `preflight_engine.has_calc_process = _has_calc_process`（**同一函数对象**的公开别名）供 `content_checkpoint._calc_process_findings` 复用。⚠️ **接线时发现并多修一处真实分叉**：只按 `chapter_key == "calc_drawings"` 判章节也会漏检 —— 预检 `CALC_TITLE_KEYWORDS` 含「承载力计算」「安全系数」而章节归类补表只到「计算书/验算」。新增 `_is_calc_section`（**并集**判据，两个维度都取预检同一出口）。



> ② **【P1】CON-06 跨章搬运是唯一「无法在提示词预防」的检查点**：生成单章时模型看不到其他章节正文，system 级「禁止成段雷同」只能提高概率。生产库 3 条 CON-06 全部「骨架归一后相似度 100%」（整段照抄、连数字没换）。新增 `cross_section_copy_findings`（复用 `duplicate_detection.find_cross_section_copies` 同一实现）接入 `_persist_section` 的 `checkpoint_selfcheck` **之后**、锁外只读、R13 判空、fail-soft；新开关 `content_crosscheck_duplicate`（默认 True）。



> ③ **【P1】STD-03 装饰保温误报消除（补库而非放宽判据）**：被报的 GB 50209-2010 / GB 50222-2017 / GB 50009-2012 **都是真实现行规范**，根因是标准库覆盖不足。`CATEGORY_STANDARDS["装饰保温"]` 补入三条，`STANDARD_DB_VERSION` → **2026.10.2**；杜撰编号仍判未收录（护栏锁定）。



> 护栏：新增 `tests/test_checkpoint_feedback_20261002.py` **52 例**（判据同源：别名同一对象 + monkeypatch 打补丁后生成侧结论随之改变 + 静态扫仓禁止本地公式正则 + `_is_calc_section` 对 `CALC_TITLE_KEYWORDS` 逐项 parity / CON-06 透传共享检测器 + 仅报本章 + fail-soft / STD-03 补库 + 杜撰仍判缺失 / `_persist_section` 接线静态锁 / `validate_constraint_map()` 回归）。**后端全量 5012 passed, 4 skipped, 3 xfailed, 0 failed（237.45s）**；关联回归 814 passed。⚠️ 本轮踩坑：工具命令有 **30s 硬上限**（`timeout` 参数被忽略，全量 pytest 须走 `Start-Process` 后台起 + 轮询）；`\r` 在命令字符串里被当转义吃掉（`Temp\r29…` 真的建成了带 CR 的文件名）；诊断脚本必须 ASCII + `\u` 转义（GBK 控制台打印 `²` 直接 `UnicodeEncodeError`）；**护栏防空转**（`not findings` 类阳性用例若判据本身判否会静默通过，helper 内需先断言样本确实被判为该章节）；`checkpoint_selfcheck` 形参是 `is_hazardous_basis` 而非 `is_hazardous`。



> ⚠️ **仍未完成**：生产库修复前后对比测量阻塞 —— `backend/data/scheme_assistant.db` 正被后台进程重建（实测 88 章节 / 有正文 0 / 总字数 0）。重建前最后一条 preflight（2026-10-01 15:55）为 **20 findings / D 级 / released=0**。⚠️ `preflight_runs` **没有 `score` 列**（分数在 `dimensions`/`counts`）。详见 §4.27。



> **最近校准：2026-10-02（第二十五轮）** —— **【审核检查点反向增强 · 目录生成侧】把预检判据前置到目录编排**（全部**默认向后兼容**、零新增依赖、零数据迁移、**1 个新开关默认开**）：



> ① **【P0 · 判据分叉·第三份副本】九大章节关键词三处各写一份**：`sse_handlers._DANGEROUS_REQUIRED_KEYWORDS` 是手抄副本，与 `audit_rules` 注册表**双向分叉**（目录侧认「工程概述/施工部署/组织机构/图纸」，审核侧**不认**）→ 目录侧程序化预检**放行**、预检照报 `CMP-01/03/06/07`。新增 `services/outline_checkpoint.py` 作**单一出口**（判据 = 注册表 `keywords` ∪ `preflight_engine` 实际谓词常量），旧表降级为派生别名。



> ② **【P0 · 门控不对齐】非危大专项方案目录侧从未被要求过九章**：预检 `check_completeness` 对 CMP-01~09 **无条件**检查；目录侧整段程序化预检却被 `if (requirements or basis is not None)` 门控 —— 未填「编制要求」时**整段跳过**。生产库实证：该方案为「装饰装修专项施工方案」（非危大），6 条 CMP 命中（3 block）。现恒定参与；关闭 `outline_checkpoint_check` 完整回退。



> ③ **【P1 · 分叉是双向的】`preflight_engine` 也内联了与注册表不一致的元组**：`check_safety` 的 SAF-03~06、`check_traceability` 的 TRC-03 判据与 `audit_rules.keywords` 双向不一致。已把这 6 组字面量**提取为模块级常量**（取值逐字不变，审核行为零变化）供两侧共用，并把注册表 keywords 按引擎实际谓词补齐（只增不减）。



> ④ **【P1 · 并集方向陷阱】并入旧关键词只会固化分叉**：首版把旧目录侧关键词并入判据以「只增不减」，实测「工程概述」仍放行而审核报 CMP-01 —— 并集让目录侧判据 ⊇ 审核侧判据，**方向正好相反**。正确方向是目录侧判据**完全等于或略宽于**审核侧（护栏 `test_keywords_superset_of_audit_registry` + `test_legacy_only_word_not_in_any_keywords` 双向锁定）。



> ⑤ **【P1 · 内容侧编号级判据】`basis` 章只判「法律法规/标准/规范」等词，而预检判的是编号**：新增 `_basis_standard_findings`（STD-02 须含 GB 55xxx、STD-05 须含本类别现行标准编号，判据直接复用 `preflight_engine._STANDARD_CODE_RE`）。⚠️ **不可复用 `_BARE_CODE_RE`**：它是「裸编号」检测器（带 `(?![-\s]*\d{4})` 负向断言），写全年号的编号一个都提取不到 → 会把合规正文判成缺失（本轮当场踩到并修掉）。



> 护栏：新增 `tests/test_outline_checkpoint_20261002.py` **43 例**（判据同源穷举 2000 组 0 分叉 / 分叉方向锁死 / 门控对齐 / 监测类别门控 / 接线静态锁 / 编号级自检）。⚠️ 本轮踩坑：editor 分段插入多次**静默截断**（`insert_line` 落在函数中间），造成测试文件结构错乱、5 个 `TestMonitorGate` 用例丢失 —— 已逐段核对 `ast` 解析出的类/方法数复核。详见 §4.26。



>



> **最近校准：2026-10-02（第二十四轮）** —— **【审核检查点前置闭环】生成侧按审核标准写 + 生成后自检 + 确定性自动修复**（全部**默认向后兼容**、零新增依赖、零新增配置项、零数据迁移）：



> ① **【P1 · 真闭环】`checkpoint_findings` 此前只写不读**：上一轮把检查点前置进提示词 + 落库自检，但 `report["checkpoint_findings"]` 全仓**无任何消费方**，自检跑完即丢、修复动作无处落地。本轮补 `fix_bare_standard_codes`（STD-03 裸编号补年号）+ `rewrite_placeholder_marks`（CON-04 占位标记改写为条件式表述）两个**确定性**修复并接到 `_persist_section`（受既有 `content_selfcheck_autofix` 控制，默认关）。



> ② **【P1 · 章节归类漏映射】SAF-03/04/05 在生成侧从未被要求过**：正文只落叶子，而「应急组织机构及职责 / 应急物资装备保障 / 应急演练」三节标题**既不等于也不互含**九大章节标准名，主判据 `chapter_key_of_title` 一律返回空串（生产库 13 个真实叶子标题实测）。新增 `infer_chapter_key`（补充表兜底，**只补不覆盖**）。⚠️ 配套 `subsection_scope`：审核侧 SAF-03/04/05 是**章级聚合**判据，逐叶子不区分会把要求拆到每个小节上**产出假缺项**。



> ③ **【P1 · 过度匹配】占位符正则会吞掉正文**：`【待(补充|定|确认)[：:】]?[^】\n]{0,20}】` 的内部字符类不含 `【`，导致「深度【待定】m 宽度【待补充：宽度】mm」被当成**一个**占位符 —— 改写时会把中间的「m 宽度」这段正常正文一起删掉。排除 `【` 后各自独立匹配。



> ④ **【P1 · 交付形态】「检查点 → 生成约束 → 实现方式」映射表落为代码**：44 条（30 跨章 + 14 章级派生）、12 分组、6 覆盖通道，**章级条目的 constraint 直接派生自 `CHAPTER_CHECKPOINT_REQUIREMENTS[].note`**（不另抄措辞）；`validate_constraint_map()` 锁定「rule_id 锚点必须真实存在于 audit_rules」（36 个锚点全部校验通过）；新增 `GET /api/v1/compliance/checkpoints`。



> 护栏：新增 `tests/test_content_checkpoint_closure_20261002.py` **66 例**；**A/B 反向验证 8 项全部定向失败**（11 例定向失败）。⚠️ 本轮踩坑：① A/B 脚本首版被 30s timeout 打断后**仍在后台改源码**，与第二次运行并发 → 「定向失败」变随机噪声且**变异残留进主源码**（A5 的变异把 `infer_chapter_key` 的 `if key: return key` 删掉）。已加**单实例锁文件** + 每次变异 `finally` 立即还原 + 字节一致断言。详见 §4.25。



>



> **最近校准：2026-10-02（第二十三轮）** —— **【定位复查 + 三项收口】导出法定前置表单（L-0）· 危大阈值复用 P0 · 人工挖孔桩超规模口径（L-2）**（全部**默认向后兼容**、零新增依赖、零数据迁移）：



> ① **【P1 · L-0】导出补齐四张法定前置表单**：`编制说明 / 专项施工方案审批表 / 专家论证报告 / 施工图纸附件清单` —— 此前全仓零命中。§4.22.9 记录的「当前环境未装 Node.js」是当时**唯一**阻塞，**2026-10-02 实测 Node v25.8.0 已可用**，本轮落地。依据：住建部令第37号第十一条（审核签字+盖章 / 总监理审查签字+执业印章 / 分包共同签字）、第十二条（专家**不得少于 5 名**）、第十三条（结论**通过·修改后通过·不通过**三选一）+ 建办质〔2018〕31号第三/四条与第(九)项。开关 `config.scheme_forms` 四项**默认全关** → 产物逐字不变。⚠️ 本轮自引入又当场修掉 2 个缺陷（`Pt` 未 import 被 fail-soft 吞掉致表单静默不渲染；`_add_form_table` 的「空值跳过」把**签字空栏整片过滤**——签字栏的「空」是正确产物不是缺失）。



> ② **【P0 判据分叉】阈值复用声明从未被读取**：`classify_scheme` 一直直接传 `sub_id` 给 `evaluate_hazard_level`，而 `sc_cuplock`/`sc_disc`（→`sc_ground`）、`ho_tower_crane`/`ho_construction_hoist`（→`ho_crane`）、`fw_disc`（→`fw_support`）这 5 个子类在 `HAZARD_THRESHOLDS` 里**没有键** → 落进「非参数型」分支 → **无条件判超规模**。实测 **30m 碗扣式脚手架**（部文 50m 才超规模）、**50kN 塔机**（部文 300kN）均被误判需专家论证。数据声明了复用却没人在判定侧读它。修法：新增 `resolve_threshold_key()` 作为**唯一**出口。



> ③ **【L-2 收口】人工挖孔桩超规模口径**（挂起理由「无部文原文不编造阈值」已解除，已核对住建部官网 2018-05-17 印发件）：附件一七(三) 人工挖孔桩工程 → 危大**无深度门槛**；附件二七(三) **开挖深度 16m 及以上** → 超规模（闭区间）。旧行为凡人工挖孔桩一律判超规模。⚠️ 新增 `oversize_conservative_missing`：`hazard_always` 分支原「缺参→不判超规模」会**漏判**真正的 16m+ 深孔（漏判是安全红线），故本规则显式要求缺参时保守判超规模并记 `missing_params`。



> ④ **复查结论：定位切换已九成到位** —— 六大类危大 **31/31** 方案名识别正确、27 子类目录模板齐备、目录库 13 分类/178 条、前端 6 Tab 无招投标语境（800 用例通过）、九大章节模板与审核规则完备。本轮**只做证据支持的三项收口**，不重复改造。⚠️ **仍缺**：图表 3 类（监测点布置/应急处置流程/脚手架平面·剖面·立面）、计算模型 scene、脚手架有限元模块（全新独立立项）、审核侧「危大判定」「施工图纸」独立规则码。`CON-SCAN-1`「或」择一语义**本轮刻意不做**（无法确定来自程序预扫描还是 AI 扫描，且 §4.23.7 明确禁止用放宽判据消除真实缺陷）。



> 护栏：新增 `tests/test_export_scheme_forms_20261002.py` **19 例** + `tests/test_hazard_threshold_p0_20261002.py` **34 例**；**A/B 反向验证 7 项全部定向失败**（还原 P0 根因 8 例 / 移除人工挖孔桩阈值 6 例 / 关闭缺参保守 3 例 / 中段插入 1 / 换序 1 / 重名 1 / 元组错位 1）。⚠️ 本轮踩坑记录：A/B 脚本用 `read_text`+`write_bytes` 往返**把源文件 CRLF 静默改成 LF**；A/B 首版**未逐次还原**且崩溃后未走收尾，把混合态留在源文件里；两处变异形态本身是 SyntaxError 而非真实错位形态。详见 §4.24。



>



> **最近校准：2026-10-01（第二十一轮）** —— **【软件定位切换】招投标编写软件 → 建筑工程各类专项施工方案编写软件**（零新增依赖、零数据迁移）：



> ① **【P0 删减】「技术评分要求」提取项下线**：`techScoring`（`ANALYSIS_ITEMS` 第 19 项 / `_ITEM_PROMPTS` / `GROUPS.scoring` 组）三处全部移除 → scheme 域回到 **18 项 / 17 必选 / 13 分组**。选它下手的关键前提：它是 `required=0`（**必选口径 17 项不变**）且实测**目录生成完全不消费**（G3 从未接线）→ 业务影响面为零。已落库的历史行不清理（保留可追溯）。



> ② **【P0 门禁】招标响应域改硬门禁下线**：`bid_response`（评标方法响应 / 商务条款提取 / 废标项）原是**软开关**（`config.bid_response_domain_enabled` 默认 False），一旦有人为调试显式设为 True 就**静默复活**。现新增 `_assert_domain_available()`（路由层唯一出口）接入 `/items`、`/items/{id}`、`/start`、`/start-sse`，**无论配置怎么写都 404**；`/domains` 回 `retired`/`retired_reason` 让前端隐藏而非渲染必 404 的选项。**保留**域代码（删它要连带清理主键双格式 + 73 个域测试，收益为零）——关掉**入口**才是「去掉招投标功能」的实质。



> ③ **【P1 覆盖度】六大类危大子类补 7 个**：对照用户清单实测发现 `fp_monitoring`（基坑监测）、`fw_disc`（盘扣式模板支撑）、`ho_tower_crane`（塔机）、`ho_construction_hoist`（施工升降机）、`sc_cuplock`（碗扣式脚手架）、`sc_disc`（盘扣式脚手架）、`ot_bored_pile`（人工挖孔桩）缺失 → 方案名写「碗扣式脚手架」「人工挖孔桩」时 `is_hazardous` 判为 **False**、九大必要章节约束与目录模板全不触发。⚠️ **不凭记忆编造阈值**：盘扣/碗扣复用 `sc_ground`（24m/50m）、塔机与升降机复用 `ho_crane`、盘扣模板复用 `fw_support`；基坑监测与人工挖孔桩设为 `threshold=None`（按「出现即危大」保守判定，数值阈值待引部文原文核对后再补）。



> ④ **【P1 同一判据三份措辞】文件性质红线收敛**：`SHARED_SCOPE_RULES`（目录）+ `content.py` **内联两份副本**（首轮第 7 条 / 续写红线）共三份 → 正文侧统一改引 `SHARED_SCOPE_RULES_BRIEF`（新增简档，走 `<<SCOPE_RULES>>` 占位符 + 注册期替换，与既有 `<<FUZZY_FILL>>` 同款机制）。**为何不直接用完整版**：正文提示词每章下发一次（38 章 = 38 次），3 条编号细则不改变约束强度、只增 token；两档的**禁用术语集合必须一致**（护栏锁定）。



> ⑤ **【P1 措辞即输入】清除注入 AI 的招投标语境**：`bid_section_context` 的 system 消息（「当前**招标文件**已按用户选择的**投标范围**处理」，每次 AI 调用都注入）、`image_engine` 三处配图风格前缀（「适合**投标**技术方案插图」，直接影响生图）、`facts_patches` 承诺措辞与补全规则、`content_fuzzy` 承诺类口径、`bid_analysis_service` 的 JSON 任务模板 —— 改为中性的「项目资料 / 施工范围 / 设计与合同要求」。⚠️ 「多标段检测 / 施工范围选择」**功能保留只改名**（本质是长文档作用域切分，专项方案同样需要）。



> ⑥ **【P1 契约红线】导出封面「投标单位」→「编制单位」**：只改 `Form.Item` 的 `label`，**字段名 `bidder_name` 一律不动** —— 它是导出 config 的历史契约（`export.py` 参数名 + 已存配置），改名字段会让老方案封面单位变空白（§3.2 禁止破坏契约）。



> 护栏：新增 `tests/test_scheme_repositioning_20261001.py` **38 例**（清单/提示词同源 · 域门禁 AST 扫描 · 六大类覆盖 · 九章完整性 · 红线渲染后校验 · 导出无投标模板），**A/B 反向验证 6 项**（提示词回流 / 索引表残留 / 门禁旁路 / 恢复门禁 / 摘掉注入点 → 各定向失败）。⚠️ 本轮护栏自身踩了两次**判据过宽**的坑并当场修正：`test_content_prompt_has_no_inline_copy` 先用纯文本匹配被本文件顶部注释误伤（§5.7 同构陷阱），改 AST 后又被 `_wrap_fuzzy_fill` 的 docstring 误伤，最终收敛为「只取 `_reg()` 第 4 个位置参数」——**判据要指向真正下发给模型的那份文本**，而不是碰巧含关键字的一切字符串。



> 详见 §4.22。



>



> **最近校准：2026-10-01（第二十轮）** —— **【目录库模块】六大类危大工程逐型式全面更新**（依据住建部令第 37 号 + 建办质〔2018〕31 号，全部**默认向后兼容**、零新增依赖、零数据迁移）：



> ① **内建模板库 22→48 个 builder**（`outline_templates.py`）：新增声明式装配助手 `_chapter`/`_nine_chapter`（九章骨架与既有 builder 同源，编号仍由 `renumber_outline` 统一重排）；脚手架拆 9（落地/附着/悬挑/门式/碗扣/盘扣/吊篮/卸料平台/操作平台）、模板支撑拆 2（高大/盘扣）、起重拆 2（塔机/升降机）、拆除拆 3（人工/机械/爆破）、其他危大拆 10（幕墙/钢结构/网架索膜/预应力/暗挖/顶管/水下/挖孔桩/边坡/四新），未指明型式的通用名仍落原兼容兜底模板。



> ② **RULES 细粒度路由前置 + 跨类误伤收口**：「落地式钢管脚手架」此前被误路由到通用 scaffold（不含「落地式脚手架」子串）；卸料/操作平台规则必须前置于架体型式规则。新增条目 TEMPLATE_META 编制依据**四层齐备**（法律法规/部门规章/强制性规范/专项技术规范，标准号仅复用条目内已确认的，不杜撰）。



> ③ **seed 预置清单 `SEED_VERSION` v2.0→v3.0**：脚手架/模板/起重/拆除分类补齐逐型式条目，新增「其他危大工程」分类（13 分类 / 178 条）；⚠️ seed 以「{方案名}标准目录」为唯一键，跨分类**重名会互相覆盖 type**（本轮曾引入幕墙/钢结构重名，已改差异化名）。`seed_catalog` 新增**进程内重入锁 + INSERT 前按名复查**双层并发护栏（旧实现基于开头 existing 快照判存在，并发重入各自全量 INSERT → 清单重复）。



> ④ **【P1 断链】目录生成「按类别自动匹配目录库」此前形同虚设**（开关 `scheme_auto_match_outline` 默认关未暴露）：a) `bid_analysis` 写入的 `schemes.hazard_category` 是**逗号连接多类别码**，`sse_handlers` 旧代码整串当单个 type 传 IN 查询 → 多类别方案恒不命中，现按逗号拆分；b) 类别码（foundation_pit/scaffold…）与预置库 type（中文分类名「基坑与土方」…）**两套体系不同源**，`outline_reference` 旧版按 `type IN (类别码)` 对预置库恒不命中，新增 `CATEGORY_CODE_TO_SEED_TYPES` 映射（仅追加候选，原始码/中文 type 向后兼容）。



> 护栏：新增 `tests/test_outline_library_full_update_20261001.py` **169 例**（注册表 parity + 九章骨架 + builder 无状态/零缓存 + 42 路由探针 + meta 四层 + seed 端到端幂等/force/stale 清理/并发重入 + 自动匹配断链锁），**A/B 反向验证 3 项**（还原逗号拆分/破坏映射键/去锁去插前复查 → 各 1 例定向失败；恢复 → 全绿）。另修掉 2 处挡基线的**上一轮 WIP 遗留缺陷**：`sse_handlers._persist_section` 缺 `section_title` 形参致 F821（正文全章落库路径 NameError）；`content_fuzzy._sentence_at` 的 `lo` 初值误为 `len(text)`（句中无前置句末符时整句切空 → 有锚点的「按合同要求」被误判空话）+「细节另见」漏规则。全量 **4578 passed, 0 failed**（基线 3886→4578 含第 16~19 轮增量，无回归）。



>



> **最近校准：2026-10-02（第二十二轮）** —— **【P0 误报】审核与预检把「生成成功」报成「六章全空」**：依据生产库 `preflight_runs`（scheme=`d3c1a897…`，rule_version 1.6.0）实证，**D 级 46.8 分、`released=False` 的根因不是正文没写，而是预检判据分叉**：



> ① **【P0 判据分叉】三处各自实现「本章是否有正文」**：`section_count=63 / leaf_count=36 / generated_count=36` —— **叶子 36/36 全部生成成功**，而 `empty_ratio` 报 42.9%（= 27/63，**恰为父节点数**）。根因是正文**只对叶子落库**（`generate_content` 遍历 leaves），父章节 `content` 恒为空是**结构必然**；但 `check_completeness` 只看**标题命中章节自身**的 content，于是把 6 个 L1 法定章节（编制依据/安全保证措施/计算书/工程概况/施工计划/施工工艺技术）判成「正文为空」，**报出 3 block + 3 high** → 等级封顶 C、completeness 维度 **0 分**、`released=False`。同文件 `check_deliverability` 用 `has_children` 判空（正确）、`preflight_stats` 把「空」等同「未生成」（错）—— **两对一错**。现新增 `preflight_engine.build_section_tree_index`（**有效正文 = 自身 + 全部子孙**，含 parent_id 成环的 fail-soft 兜底）作单一事实源，三处统一改调它。



> ② **【P1 漏报变误报】人名正则把谓语当人名**：`consistency_scanner._PERSON_RE` 第 2 组是「岗位后任意 2~4 汉字」，于是「技术负责人**组织各专**业」「项目技术负责人**签发**」「现场负责人**接到报告**」的谓语全被当成「人名」，再因「同一岗位多个不同人名」判为跨章节矛盾 —— 生产 `CON-SCAN-4/5/6` 三条 medium 冲突**全部**由此产生（捕获值为「组织各专/审核后归/批准后/签发/重排对应/接到报告/或专职安」）。现加 `_looks_like_person_name` 谓语前缀拦截 + `_ROLE_ALIASES` 岗位简称归一（技术负责人 ≡ 项目技术负责人）。真实人名（张三/李四/王五）**零漏报**。



> ③ **【P1 自身缺陷】护栏登记表把行号写进匹配键**：`test_outline_name_line_20260927.KNOWN_LAYER_LEAKS` 值是 `"L92 from app.routers…"` 而匹配取 `.split()[0]`（即 `L92`）做子串比较 —— 该 import **一旦因任何编辑位移，登记静默失效**，把已登记的遗留项重新报成「新增泄漏」，改的人无从下手（本轮 `consistency_scanner.py` 仅插入一段注释就从 L92 移到 L131 即触发）。现改为登记**行号无关**的 `模块路径 + 符号名`，并用新增 `_leak_key` 提取；**A/B 反向验证：注入一条未登记的新泄漏仍被拦下**（护栏未被架空）。



> **改进效果（同一批生产 findings 重算，未改生产库）**：问题 **16→7 / 20→11**，**block 3→0**，总分 **55.2→83.8（D→B）** 与 **46.8→75.4（D→B）**，`released` **False→True**，completeness **0→100**。护栏新增 `tests/test_audit_false_positive_20261002.py` **39 例**，**A/B 反向验证 3 项**（分别 2 / 2 / 1 例定向失败，恢复 → 39 全绿）。全量 **4706 passed, 4 skipped, 3 xfailed**（0 failed，194.60s）。



> ⚠️ **仍未落地（属真实内容缺陷，需目录生成 + 正文生成侧改进）**：`DLV-09`（review_pending 36）、`DLV-15`（global_facts_blocked 21）、`CON-04`（`【待补充】`占位符 88 处 / 64 字段）、`SAF-02`（缺高处作业·临电·防火·机械防护等专项安全技术措施）、`SAF-07`（未提特种作业持证）、`STD-03~05`（6 个标准编号未收录 / 未列编制依据清单 / 未引 37 号令与 31 号文）、`CON-SCAN-1~3`（`18mm 多层板 or 12mm 竹胶板` 被误判为矛盾，实为不同部位选材）。详见 §4.23。



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







### 4.22 第二十一轮（2026-10-01）：软件定位切换 —— 招投标 → 专项施工方案







> 依据用户《软件重新定位：专项施工方案编写软件》执行。全部**默认向后兼容**、零新增依赖、零数据迁移、零新增配置项。







#### 4.22.1 盘点结论：定位其实已九成到位







审计全仓后发现，**提示词层早已写入红线**，真正残留且仍启用的只有 3 处：







| 已有能力 | 代码依据 |



|---|---|



| 文件性质红线 | `_shared.py::SHARED_SCOPE_RULES`「专项施工方案 ≠ 投标文件」 |



| 正文禁止投标内容 | `prompts/content.py`（本轮收敛，见 4.22.4） |



| 目录审核判投标章节不合格 | `prompts/outline.py:112` |



| 导出拦截投标用语 | `export.py:170` `bidding_terms` |



| 提取时排除商务/评分条款 | `prompts/analysis.py:20,22` |



| 六大类危大 + 九大章节 | `scheme_classification.py`（910 行） |



| 目录库 48 个 builder | `outline_templates.py`（4686 行） |



| 专家论证预检 + 就绪度 | `audit_rules.py` / `compliance.py` / `audit_scoring.py` |







⚠️ **用户清单里有 3 项在代码中从未存在**（只出现在用户上传的招标文件解析产物里）：投标函、报价表、投标文件封面；「评分项一一对应」（G3）也从未实现。→ 无需删除，已改为**防回退护栏**（`test_no_cover_pages_of_tender_document`）。







#### 4.22.2 P0 ·「技术评分要求」提取项下线







- **落点**：`ANALYSIS_ITEMS[19]`、`_ITEM_PROMPTS["techScoring"]`、`GROUPS.scoring` 三处同时移除 → **18 项 / 17 必选 / 13 分组**。



- **为什么敢动**：它是 `required=0` → `REQUIRED_ITEM_IDS`（`:318` 由清单派生）**仍是 17 项，必选口径不变**；且实测**只被提取与展示，目录生成完全不消费**（与九大章节之间没有任何代码路径相连），业务影响面为零。



- **历史数据**：`bid_analysis_items` 里已落库的行**不清理**（保留可追溯），只是不再执行、不再展示。



- **连带改动**：`test_reference_alignment_20260930::TestTechScoringItem` 的 **10 个正向断言逐条翻转为反向护栏**（不删用例 —— 删掉护栏就丢了，日后有人照参考软件再加回来无人拦截）。







#### 4.22.3 P0 · 招标响应域改硬门禁







- **原状**：`bid_response`（评标方法响应 / 商务条款提取 / 废标项 / 投标关键节点 / 投标保证金）以「加法引入」接入，用 `config.bid_response_domain_enabled`（默认 False）做**软开关**。



- **软开关的真实风险**：一旦有人为调试显式设为 True（或环境变量写错），招投标域**静默复活** —— 前端拿到 18 项清单、`/start` 真的能跑完评标方法/商务条款提取。



- **修法**：新增 `_assert_domain_available()`（**路由层唯一出口**），接入 `/items`、`/items/{item_id}`、`POST /start`、`GET /start-sse`（门禁放在 `_start_sse_inner` 最前面：不查文档、不建任务即拒绝，不留孤儿任务）。



- **未知域不变**：未知 domain 仍返回空清单 + `domain_unknown=True`（既有契约，fail-closed 但不算错误），门禁只拦「注册过但已下线」的域。



- **保留域代码**：删它要连带清理主键双格式（`build_item_pk`）、下游跨域消费、73 个域测试，收益为零。**关掉入口才是实质**。



- `/domains` 新增 `retired` / `retired_reason` / `label` 后缀，让前端隐藏而不是渲染出一个点击必 404 的选项。







#### 4.22.4 P1 · 文件性质红线「三份措辞 → 一份来源」







- **原状**：`SHARED_SCOPE_RULES`（目录生成）+ `content.py:93`（正文首轮第 7 条）+ `content.py:294-295`（正文续写「数据真实性红线」）——**同一判据三份措辞**，改一处漏两处。



- **修法**：`_shared.py` 新增 `SHARED_SCOPE_RULES_BRIEF`（简档）；`content.py` 改用 `<<SCOPE_RULES>>` 占位符 + `_wrap_fuzzy_fill` 注册期替换（与既有 `<<FUZZY_FILL>>` / `<<NO_PLACEHOLDER>>` **同款机制**，零新增机制）。



- **为何不直接用完整版**：正文提示词**每章下发一次**（38 章 = 38 次），完整版的 3 条编号细则不改变约束强度、只增 token。两档的**禁用术语集合必须一致**，由 `test_scope_rules_cover_same_banned_terms` 锁住。







#### 4.22.5 P1 · 六大类危大子类覆盖度补齐（对照用户清单实测）







| 大类 | 原子类 | 现子类 | 本轮补齐 |



|---|---|---|---|



| 基坑工程 | 2 | 3 | `fp_monitoring` 基坑监测 |



| 模板工程及支撑体系 | 2 | 3 | `fw_disc` 盘扣式模板支撑 |



| 起重吊装及安装拆卸 | 2 | 4 | `ho_tower_crane` 塔机、`ho_construction_hoist` 施工升降机 |



| 脚手架工程 | 4 | 6 | `sc_cuplock` 碗扣式、`sc_disc` 盘扣式 |



| 拆除、爆破 | 3 | 3 | — |



| 其他危大工程 | 7 | 8 | `ot_bored_pile` 人工挖孔桩 |







⚠️ **阈值取值原则：不凭记忆编造**。盘扣/碗扣复用 `sc_ground`（24m/50m 部文口径）、塔机与升降机复用 `ho_crane`（300kN/200m）、盘扣模板复用 `fw_support`；基坑监测与人工挖孔桩设 `threshold=None`（按「出现即危大」保守判定 —— **不漏判才是安全红线**），其数值阈值待引建办质〔2018〕31号原文核对后再补。







#### 4.22.6 P1 ·「措辞即输入」：清除注入 AI 的招投标语境







用户最容易忽略的一类残留 —— **不是 UI 文案，是喂给模型的文案**：







| 位置 | 原文 | 影响 |



|---|---|---|



| `bid_section_context.py` | 「当前**招标文件**已按用户选择的**投标范围**处理」 | 作为 **system 消息注入每一次 AI 调用**（单项 + 分段合并），与红线正面冲突 |



| `image_engine.py:734/738/742` | 「适合**投标**技术方案插图」 | 直接进入**生图 prompt**，影响成稿配图 |



| `facts_patches.py:78/118` | 「按**招标要求**执行该项」「凡**招标要求、评分口径**…」 | 进入事实补全规则 → 事实库措辞 |



| `content_fuzzy.py:92` | 「原样引用合同与**招标文件**中的承诺口径」 | 进入正文模糊生成规则 |



| `bid_analysis_service.py:356/946` | 「**招标文件**中没有的字段」「请从**招标文件**中提取」 | 进入 JSON 项提取任务模板 |







统一改为中性的「项目资料 / 施工范围 / 设计与合同要求」。**只改措辞，不改语义与触发条件**。







⚠️ **「多标段检测 / 施工范围选择」功能保留、只改名**（用户明确要求）：它本质是**长文档作用域切分**（一份项目资料含多个标段时，AI 不该把各段参数混进同一份方案），专项方案同样需要；删除会连带 25 个前端测试且丢掉真实能力。已改：前端 Tooltip / Alert 文案、后端 `HTTPException` 与日志、**以及注入 AI 的 system 消息**。







#### 4.22.7 契约红线：导出封面「投标单位」→「编制单位」







- 只改 `Form.Item` 的 `label`，**字段名 `bidder_name` 一律不动** —— 它是导出 config 的历史契约（`export.py` 参数名 + 已存配置），改名字段会让**老方案的封面单位变空白**（§3.2 禁止破坏契约）。



- 同样地，`doc_categories.py` 的「招标文件」**文档类别**、`facts_classification.py:357` 的「招标/标书/投标」**来源识别关键词**均**保留** —— 用户确实会传招标文件，资料里确实会出现这些词。



- `export.py:170` 的 `bidding_terms` **拦截规则必须保留** —— 那是红线执行，不是残留。







#### 4.22.8 护栏与 A/B 反向验证







`tests/test_scheme_repositioning_20261001.py` **38 例**：







| 分组 | 锁定什么 |



|---|---|



| `TestTechScoringOffline` | 清单↔提示词↔索引表同源（防幽灵入口）+ AST 判 `techScoring` 字面量不回流 + 18/17/13 口径锚点 |



| `TestBidResponseDomainGate` | 四入口恒 404（含定位说明）+ `/domains` 恒 `retired` + **AST 扫描所有暴露 `domain` 的路由端点是否过门禁**（接受直接调用或转调已门禁 helper 两种形态） |



| `TestHazardCategoryCoverage` | 六大类 id/名称精确、用户清单每个子类可识别、新增 7 子类端到端、threshold 键全部可解析、`threshold=None` 必保守判危大 |



| `TestNineChaptersIntegrity` | 章节号连续 / key 唯一 / `source_items` **不拆链** / 第一章 `category_fields` 覆盖六大类 |



| `TestAntiRebidRedlines` | 共享红线四要素完整、两模块均引用、**渲染后**提示词确实带红线且占位符替换干净、两档禁用术语集合一致、导出无投标模板 |







| 还原的修复点 | 结果 |



|---|---|



| 提示词回流（只加回 `_ITEM_PROMPTS`，不动清单） | 护栏命中 |



| 索引表残留（`_ITEM_MAP` 多一条） | 护栏命中 |



| 门禁被旁路（`_assert_domain_available` 置空） | 招投标域放行 → 证明门禁是唯一防线 |



| 恢复门禁 | 404 复现 |



| 摘掉 `<<SCOPE_RULES>>` 注入点 | **2 例**定向失败（两个正文模板） |







⚠️ **本轮护栏自身踩了两次「判据过宽」的坑并当场修正**（记录以免重复）：



1. `test_content_prompt_has_no_inline_copy` 先用**纯文本匹配**，被本文件顶部的说明注释误伤 —— §5.7 记录的「注释含关键词误伤字面量判定」同构陷阱。



2. 改用 AST 取 `Constant` 后，又被 `_wrap_fuzzy_fill` 的 **docstring** 误伤（docstring 同为 `Constant`）。



3. 最终收敛为「**只取 `_reg()` 第 4 个位置参数**」—— 判据必须指向**真正下发给模型的那份文本**，而不是碰巧含关键字的一切字符串。







**教训**：护栏判据要选「与风险同构」的锚点。选一个更宽的锚点（源码全文 / 全部常量）看似更严格，实际会因为「说明文字也含关键词」而恒失败，**逼着后人把护栏改松** —— 比不写护栏更糟。







#### 4.22.9 本轮未落地项（记录以免重复排查）







- **L-0（P1，最高）导出「专项施工方案封面 / 编制说明 / 审批表 / 专家论证表」**：需改 `export.py`（4499 行）+ 前端导出配置页，且**当前环境未装 Node.js、跑不了前端测试**，强行半接线风险高。设计已就绪：复用 `_add_table_from_markup`，加 `export_scheme_approval_pages` 开关。



- **L-2（P2）人工挖孔桩超规模数值阈值**：需引建办质〔2018〕31号原文核对后再补，**不凭记忆写**。



- **L-3（P2）脚手架有限元计算模块**：全新独立模块（数据模型 + 计算引擎 + 规范库 + 前端），独立立项。







### 4.23 第二十二轮（2026-10-02）：审核与预检误报收口 —— D 级 46.8 分的根因不是正文没写







> 依据生产库 `backend/data/scheme_assistant.db` 的 `preflight_runs` / `consistency_conflicts` /



> `compliance_check` 实证。**重要前提**：`logs/backend.log` 里 99% 是 pytest 输出



> （`simulated disk full` / `模拟 AI 超时` 等桩数据），**审核结论的真源是数据库**，



> 排障时应直接读 `preflight_runs.findings`，不要从日志里捞结论。







#### 4.23.1 生产事实（scheme=d3c1a897…，两次运行）







| 指标 | 值 | 解读 |



|---|---|---|



| `section_count` / `leaf_count` / `generated_count` | 63 / 36 / **36** | **叶子 100% 生成成功** |



| `empty_ratio` | **42.9%** | = 27/63，**恰为父节点数** |



| `total_words` / `word_budget` | 43224 / 30000 | 篇幅达标 |



| `chart_total` / `chart_done` | 5 / 5 | 图表全渲染成功 |



| 总分 / 等级 | 55.2 → 46.8，**D** | `released=False` |







**六条 CMP 发现（3 block + 3 high）全部是「存在章节，但正文为空」**：



编制依据 / 安全保证措施 / 计算书及相关图纸 / 工程概况 / 施工计划 / 施工工艺技术。







#### 4.23.2 【P0】三处各自实现「本章是否有正文」（两对一错）







| 位置 | 原实现 | 判定 |



|---|---|---|



| `check_deliverability` | `has_children = id in parent_ids` → 父节点不判空 | 正确 |



| `preflight_stats` | `generated = content 非空的章节数`；分母 = **全部**章节 | 错 |



| `check_completeness` | 只看**标题命中章节自身**的 content | **错（误报 block）** |







**根因**：目录是「L1 法定章节 + 二级子节」两层结构，而 `sse_handlers.generate_content`



**只对叶子落正文**，父章节 `content` 恒为空是**结构必然**而非生成失败。







**修复**：新增 `preflight_engine.build_section_tree_index(sections) -> (parent_ids, effective)`



作单一事实源，**有效正文 = 自身 + 全部子孙**；三处统一改调。



`parent_id` 成环 / 悬挂 / 自环均有 fail-soft 兜底（预检不可抛异常）。



`empty_ratio` 分母同步改用**叶子数**（父章节本就无正文，计入分母会让结构正常的方案恒显示高比例空章节）。







#### 4.23.3 【P1】人名正则把谓语当人名







`_PERSON_RE` 第 2 组 = 「岗位后任意 2~4 汉字」。生产捕获值实证：







| 原文 | 捕获的「人名」 |



|---|---|



| 技术负责人**组织各专**业进行图纸会审 | `组织各专` |



| 项目技术负责人**签发** | `签发` |



| 现场负责人**接到报告**立即赶赴现场 | `接到报告` |



| 现场负责人**或专职安**全员 | `或专职安` |







`CON-SCAN-4/5/6` 三条 medium 冲突**全部**由此产生。



修复：`_looks_like_person_name` 谓语前缀/全词拦截 + `_ROLE_ALIASES`（技术负责人 ≡ 项目技术负责人）。



**真实人名（张三/李四/王五/赵六/刘强）零漏报**，并有反向用例锁定。







#### 4.23.4 【P1】护栏登记表把行号写进匹配键（本轮自己踩到）







`test_outline_name_line_20260927.KNOWN_LAYER_LEAKS` 值为 `"L92 from app.routers…"`,



匹配取 `.split()[0]`（即 `L92`）做子串比较 → **该 import 一旦位移，登记静默失效**。



本轮 `consistency_scanner.py` 仅插入一段注释 + 人名守卫就从 L92 移到 L131，



立刻把已登记遗留项报成「新增泄漏」，而报错信息完全不提示原因。



**教训与 §5.14 同源且更进一步**：护栏的**登记表本身**也会因行号漂移而失效。



凡「用行号 / 字符偏移做匹配键」的登记，一律改用稳定标识。



修复后已 A/B 验证：注入一条**未登记**的新泄漏仍被拦下（护栏未被架空）。







#### 4.23.5 改进效果验证（同一批生产 findings 重算，未改生产库）







| 运行时刻 | 问题总数 | block | 总分 | 等级 | released | completeness |



|---|---|---|---|---|---|---|



| 2026-10-01 14:23 | 16 → **7** | 3 → **0** | 55.2 → **83.8** | D → **B** | False → **True** | 0 → **100** |



| 2026-10-01 15:55 | 20 → **11** | 3 → **0** | 46.8 → **75.4** | D → **B** | False → **True** | 0 → **100** |







#### 4.23.6 护栏与 A/B 反向验证







`tests/test_audit_false_positive_20261002.py` **39 例**（有效正文单一事实源 6 + CMP 不误报 5 +



统计口径 4 + 人名判定 19 + 静态防分叉 4，含成环/悬挂/自环 fail-soft 与「改宽松了」反向用例）。







| 还原的修复点 | 定向失败 |



|---|---|



| `check_completeness` 退回只看自身 content | **2 例** |



| `preflight_stats` 的 effective 退回自身 content | **2 例** |



| 人名谓语守卫摘掉 | **1 例** |



| 全部恢复 | **39 passed** |







⚠️ **A/B 脚本自身踩了两次坑**（记录以免重复）：① 第一版只改内存 `BAK` 没 `write_bytes`，



「还原」根本没落盘 → 出现「还原后仍全绿」的假通过；② 源文件是 **CRLF**，



按 `\n` 拼锚点永远匹配不上（A1/A2 显示「未找到锚点」却被当成通过）。



两处都已修，并按 §4.21.5 加了「非空 + 无 U+FFFD + `ast.parse` 通过」三断言后才允许写回。







#### 4.23.7 未落地项（真实内容缺陷，需目录生成 + 正文生成侧改进）







| 发现 | 生产数据 | 建议归属 |



|---|---|---|



| `DLV-09` review_pending 36 | 36 章待人工审核 | 流程（非缺陷） |



| `DLV-15` global_facts_blocked 21 | 21 项事实未确认/模拟/冲突 | 全局事实页核对 |



| `CON-04` 占位符 | 88 处 `【待补充：…】` / 64 字段 / 15 章 | **正文生成**：模糊填充未覆盖 |



| `SAF-02` 专项安全技术措施 | 缺高处作业/临电/防火/机械防护 | **正文生成**：安全章要素清单 |



| `SAF-07` 特种作业持证 | 未提电工/焊工/架子工持证 | **正文生成**：人员分工章 |



| `STD-03~05` 标准编号 | 6 个未收录 / 无编制依据清单 / 未引 37 号令 | **目录生成**：编制依据章 + 规范注入 |



| `CON-SCAN-1` 板材厚度 | `18mm 多层板 or 12mm 竹胶板` 实为不同部位选材 | 一致性扫描：需支持「或」语义 |







⚠️ 这些**不属于本轮两个误报**，不得用「放宽判据」的方式消除 —— 那会把真实缺陷



一起放过（`SAF-02`/`SAF-07`/`STD-05` 正是靠 AI 语义判定抓出来的）。







### 4.24 第二十三轮（2026-10-02）：导出法定前置表单（L-0）+ 危大阈值复用 P0 + 人工挖孔桩超规模口径（L-2）







> 依据用户重申的《软件重新定位：专项施工方案编写软件》执行。全部**默认向后兼容**、零新增依赖、零数据迁移。



> ⚠️ **本轮是对前序二十三轮的验收式复查**，不是从零改造：重新审计后确认定位切换已九成到位



> （六大类危大 31/31 方案名识别正确、27 子类目录模板齐备、目录库 178 条、前端 6 Tab 无招投标语境、



> 九大章节模板与审核规则完备），故本轮**只做证据支持的三项收口**，不做重复改造。







#### 4.24.1 复查结论：12 模块方向的真实状态







| 用户方向 | 状态 | 证据 |



|---|---|---|



| 1 解析提取 | ✅ 已完成 | 18 项 / 17 必选；`techScoring` 已下线；招标响应域硬门禁 404（§4.22） |



| 2 提取项目 | ✅ 已完成 | 六大类 27 子类实测 31/31 命中（§4.24.6） |



| 3 目录生成 | ✅ 已完成 | 48 builder + 51 条路由；目录以**方案名为主线**（`match_template(scheme_name)`） |



| 4 全局事实 | ✅ 已完成 | 九大章节事实分类 + 危险参数归章（§4.8） |



| 5 正文生成 | ✅ 已完成 | 46 条审核规则 + **检查点前置**（`content_checkpoint.py`，§4.23 WIP 已接线） |



| 6 图表生成 | ⚠️ **4/7** | 已有工艺流程/甘特/劳动力/组织机构；缺监测点布置图、应急处置流程图、脚手架平面·剖面·立面图 |



| 7 审核预检 | ✅ 基本完成 | CMP-01~09 九章 + STD-01~05 + 专家论证 10 项 + 计算书；缺「危大判定」「施工图纸」独立规则码 |



| 8 导出文档 | ✅ **本轮补齐** | 见 §4.24.2 |



| 9 提示词模块 | ✅ 已完成 | 50 模板全部专项方案化；九章要素由 `<<CHAPTER_CHECKPOINT>>` 前置注入 |



| 10 文本模型配置 | ⚠️ 部分 | 28 scene；**缺计算模型**；图表只有 `chart_fix`（修复）无生成 scene |



| 11 目录库模块 | ✅ 已完成 | 13 分类 / 178 条；四字段（名称·章节·适用条件·编制依据）齐备 |



| 12 脚手架有限元 | ❌ **未做** | 全新独立模块（数据模型 + 计算引擎 + 规范库 + 前端），需独立立项 |







#### 4.24.2 【P1 · L-0】导出补齐四张法定前置表单







AGENTS.md §4.22.9 记录的 L-0（「当前环境未装 Node.js、跑不了前端测试」为当时的**唯一**阻塞）。



**2026-10-02 实测 Node v25.8.0 已可用**，阻塞解除，本轮落地。







此前导出链路只有「封面 + 目录 + 正文」，`编制说明 / 审批表 / 专家论证报告 / 施工图纸附件清单`



四项**全仓零命中** —— 专项施工方案的实际报审件形态缺失。







| 函数 | 落点 | 规范依据（部文原文） |



|---|---|---|



| `_add_compilation_note_page` | `export.py` | 37 号令第十一条（审核/审查签字后方可实施） |



| `_add_scheme_approval_table` | `export.py` | 37 号令第十一条：施工单位技术负责人审核签字 + 加盖单位公章、总监理工程师审查签字 + 加盖执业印章；分包时总承包与分包技术负责人**共同**签字 |



| `_add_expert_review_form` | `export.py` | 37 号令第十二条（专家**不得少于 5 名**）、第十三条（论证结论**通过/修改后通过/不通过**三选一）；31 号文第三条（参会人员五类）、第四条（论证内容三项，逐字） |



| `_add_drawing_appendix_page` | `export.py` | 31 号文 第(九)项「计算书及相关施工图纸」 |







- **接线**：`_build_docx_sync` 签名**末尾**追加 `scheme_forms: dict | None`（四个独立开关，



  合并为单参以保持签名尾部稳定）；顺序 封面 → 目录 → 前置表单 → 正文；



  纳入 `_EXPORT_CONFIG_KEYS`（否则切换开关不失效缓存 → 用户看不到刚打开的表单）。



- **默认全关** → 产物与本轮之前**逐字一致**（向后兼容）。



- **红线**：签字栏一律留空（表单交付态即「待签」），**不预填人名 / 证书编号 / 单位名称**；



  fail-soft（异常只记 WARNING，不阻断正文导出）。







⚠️ **本轮自引入又当场修掉的两个缺陷**（均由新护栏当场抓出）：



1. `Pt` 未在三个表单函数内 import → `NameError` 被 fail-soft 吞掉，**表单静默不渲染**。



   教训：fail-soft 兜底会让「实现缺失」表现为「功能不生效」而日志里只有一条 WARNING，



   端到端断言（校验产物里真的有页题与表格）是唯一能抓住它的判据。



2. `_add_form_table` 原按「空值跳过」过滤，把**签字空栏整片过滤掉** ——



   签字栏的「空」不是缺失而是正确产物。已加 `keep_empty` 参数区分两类行



   （抬头行跳过空值 / 签字行保留空值）。







#### 4.24.3 【P0】阈值复用声明从未被读取 —— 30m 碗扣脚手架被误判需专家论证







- **根因**：子类表声明了阈值**复用**（`sc_cuplock`/`sc_disc` → `sc_ground`、



  `ho_tower_crane`/`ho_construction_hoist` → `ho_crane`、`fw_disc` → `fw_support`），



  但 `classify_scheme` 一直**直接传 `sub_id`**：`HAZARD_THRESHOLDS` 里没有这些复用子类的键 →



  `evaluate_hazard_level` 落进「非参数型」分支 → **无条件判超规模**。



  数据声明了复用却没人在判定侧读它 —— AGENTS.md 反复记录的「判据分叉」原样复现。



- **实测影响**：30m 碗扣式/盘扣式脚手架（部文 50m 才超规模）被要求组织专家论证；



  50kN 塔机（部文 300kN）同样误判。属**过判**（多花成本），但判定依据完全失真。



- **修法**：新增 `resolve_threshold_key(sub_id)` 作为**唯一**出口；



  `classify_scheme` 改调它。数据表里的复用声明从此真正生效。







#### 4.24.4 【L-2】人工挖孔桩超规模口径（引部文原文，不凭记忆）







§4.22.9 的 L-2 此前挂起，理由是「需引建办质〔2018〕31号原文核对后再补」。



本轮已核对**住建部官网 2018-05-17 印发件原文**：







- 附件一 七(三)：**人工挖孔桩工程** → 危大工程，**无深度门槛** → `hazard_always=True`



- 附件二 七(三)：**开挖深度 16m 及以上**的人工挖孔桩工程 → 超过一定规模（闭区间）







- **旧行为**：`threshold=None` → 落「非参数型」分支 → 凡人工挖孔桩**一律**判超规模。



- **新行为**：10m 挖孔桩 = 危大但**非**超规模；16m 临界判超规模（「及以上」闭区间，



  与本文件此前对基坑 3m/5m、脚手架 24m/50m 的闭区间修正同源）。



- ⚠️ **新增 `oversize_conservative_missing`**：`hazard_always` 分支原本「缺参 → 不判超规模」，



  对人工挖孔桩会**漏判**真正的 16m+ 深孔（漏判是安全红线，过判只是多花成本）。



  故显式开启「缺参按超规模保守判定 + 记入 `missing_params` 供上游补全」。



  ⚠️ 该标志**只对本规则生效**，`ho_crane` 等其它 `hazard_always` 规则的 `missing_params`



  仍为空（否则会把「本来就没有超规模参数」误报成缺参）。







#### 4.24.5 护栏与 A/B 反向验证







| 护栏文件 | 例数 | 覆盖 |



|---|---|---|



| `tests/test_export_scheme_forms_20261002.py` | 19 | 四表默认关闭 / 逐项独立 / 端到端 DOCX 渲染（页题、表格、5 名专家栏、论证三项、抬头信息）/ 条文不得改写 / 签字栏不预填 / 空值行过滤不误伤签字栏 / fail-soft |



| `tests/test_hazard_threshold_p0_20261002.py` | 34 | 阈值复用解析（5 个复用子类）/ 悬空声明检测 / 静态护栏（判定必须过 `resolve_threshold_key`）/ 30m·50m·24m 行为 / 塔机 300kN 闭区间 / 人工挖孔桩 10·16·20·缺参 / 既有阈值口径不变 / 全表无严格大于号 |







**A/B 反向验证 7 项，全部定向失败**：







| 还原的修复点 | 定向失败 |



|---|---|



| 判定改回直传 `sub_id`（P0 根因） | **8 例** |



| 移除 `ot_bored_pile` 阈值规则 | **6 例** |



| 关闭 `oversize_conservative_missing` | **3 例** |



| `_build_docx_sync` 中段插入必填参数 | 1 例 |



| 交换历史参数次序 | 1 例 |



| 追加参数与历史参数重名 | 1 例 |



| 元组内追加元素排到历史元素之前 | 1 例 |







⚠️ **A/B 脚本自身踩的三个坑（记录以免重复）**：



1. 第一版用 `read_text` / `write_bytes` 往返，**把源文件 CRLF 静默改成了 LF**



   （text 模式默认通用换行转换 + 原样写出 = 单方面改写行尾）。



   修正：全程字节级操作，断言「无裸 LF」。



2. 第一版**没有逐次还原**，A2 的变异叠加在 A1 之上，且崩溃后未走收尾还原，



   **把 A1+A2 的混合态留在源文件里**。修正：`try/finally` + 每次变异后立即 `restore()`。



3. 两处变异形态本身是 **SyntaxError**（在带默认值段中间插入带默认值参数、



   去掉末位参数默认值）→ 被 `ast.parse` 断言拦下。真正的错位风险形态是



   **在必填段中段插入必填参数**（语法合法但后续参数整体右移），A1 改用该形态。







⚠️ **护栏判据演进（§5.14 同源，第四例）**：`test_d3_builder_param_is_last` 原本断言



「`appendix_sources` 必须是最后一个位置参数」。该锚点锁的是**名字**，而护栏真正要防的风险是



**既有位置参数错位**；AGENTS.md 签名处明写的「追加参数统一放末尾」是**可重复执行**的约定，



每轮新增末尾参数都会把「末位」代理顶掉（本轮加 `scheme_forms` 即触发）。



放宽到「随便追加」同样危险（会放过中段插入）。故改为锁定**历史参数相对顺序**



（冻结清单逐字比对）+ 追加参数必须带默认值 —— 中段插入 / 换序 / 重名 / 元组错位四类回归**仍全部被拦下**。







#### 4.24.6 六大类危大覆盖度实测（本轮复查，31/31 命中）







用 31 个真实方案名跑 `classify_scheme` + `evaluate_hazard_level`：



基坑支护与降水 / 土方开挖 / 基坑监测 / 模板支撑体系 / 高大模板 / 盘扣式模板支撑 /



起重吊装 / 塔机安装拆卸 / 施工升降机安装拆卸 / 落地式 / 附着式升降 / 悬挑式 / 门式 /



碗扣式 / 盘扣式 / 吊篮 / 卸料平台 / 操作平台 / 人工拆除 / 机械拆除 / 爆破拆除 /



幕墙 / 钢结构 / 网架索膜 / 预应力张拉 / 地下暗挖 / 顶管 / 水下作业 / 人工挖孔桩 /



边坡 / 新技术新工艺 —— **NO-HIT = 0**，27 个子类**全部**有 ≥1 个目录模板 builder。







#### 4.24.7 本轮未落地项（记录以免重复排查）







- **图表 3 类**（监测点布置图 / 应急处置流程图 / 脚手架平面·剖面·立面图）：



  需新增类型码并同步 **4 处白名单**（`chart_validators.PIL_RENDERABLE_CHART_TYPES`、



  `_chart_pipeline._ALL_CHART_TYPES`、前端 `chartTypes.ts`、**`charts.py GET /types` 硬编码 7 条**）



  + 提示词 + 校验器 + 前端，属独立一轮。



- **脚手架有限元计算模块**（用户方向 12）：全新独立模块，需独立立项（数据模型 + 计算引擎 +



  规范库 + 前端）。⚠️ 属专业级结构计算，**不应由本轮顺带实现**。



- **计算模型 scene**：新增 scene 最小改动 = 2 处（`provider_factory.KNOWN_SCENES` +



  调用点 `scene=` 打标），前端零改动，`TestKnownScenesDrift` 护栏自动覆盖。可下轮顺带做。



- **CON-SCAN-1「或」择一语义**（`18mm 多层板 or 12mm 竹胶板` 误判为矛盾）：**本轮刻意不做**。



  理由：① 无法确定该冲突是程序预扫描还是 AI 分片扫描产出（`_NUM_TOPICS` / `_MODEL_RE` 都不匹配



  「18mm 多层板」形态）；② §4.23.7 已明确警告「不得用放宽判据的方式消除」。



  正确前置动作：先从 `consistency_conflicts` 读出该条的 `conflict_type` 与



  `source`（`program_prescan` 还是 `ai_scan_section`），再决定改判定还是改仲裁提示。



- **人工挖孔桩超规模的 `pile_depth` 参数自动提取**：阈值已就位，但参数目前靠 API 传入，



  未接「从项目资料自动抽取挖孔深度」。缺参时按保守口径判超规模并记 `missing_params`。



- **`fp_monitoring`（基坑监测）阈值**：仍为 `None`（出现即危大）。部文附件一未把基坑监测



  单列为带阈值的危大项，**不凭记忆编造数值**，维持保守判定。







### 4.25 第二十四轮（2026-10-02）：审核检查点前置的**闭环**收口







> 上一轮只做完了「检查点 → 生成约束」的前置注入与自检**埋点**，本轮把



> 「生成后自检 → 自动修复 → 对外可见」三段补齐，形成真正闭环。



> **零新增配置项**（全部复用 `content_checkpoint_prepend` / `content_selfcheck` /



> `content_selfcheck_autofix`）、零新增依赖、零数据迁移。







#### 4.25.1 生产证据（读 `preflight_runs.findings`，两轮运行合计 20 项）







| 规则 | 生产发现 | 本轮处置 |



|---|---|---|



| `CON-04` | **88 处占位标记 / 64 个字段**（全部问题中体量最大） | ✅ `rewrite_placeholder_marks` 确定性改写 |



| `STD-03` | 6 个标准编号缺年号 | ✅ `fix_bare_standard_codes` 确定性补年号 |



| `SAF-03/04/05` | 审核侧未报，但**生成侧从未要求过**（见 §4.25.3） | ✅ `infer_chapter_key` + `subsection_scope` |



| `CMP-01~05/09` | 6 章「正文为空」= 父节点误报 | ⏭ §4.23 已收口（`build_section_tree_index`） |



| `STD-04` | 未引 37 号令 / 31 号文 | ⚠️ **审核侧对非危大方案误报**（见 §4.25.7） |



| `DLV-09/15` | `review_pending 36` / `global_facts_blocked 21` | ⏭ 属流程状态，非正文缺陷 |



| `CON-SCAN-1~6` | 跨章数值/术语冲突 | ⏭ 章级聚合判据，生成侧刻意不前置 |







#### 4.25.2 【P1】`checkpoint_findings` 只写不读 → 两个确定性自动修复







`report["checkpoint_findings"]` 全仓**无消费方**（`grep` 仅命中写入点）——



自检跑完即丢。新增两个**纯函数确定性**变换并接到 `_persist_section`：







| 函数 | 规则 | 红线 |



|---|---|---|



| `fix_bare_standard_codes` | `STD-03` | 年号**只取自** `standards_registry` 现行库（`BASE_CODE_INDEX`，66 基号 / 0 歧义）；库外**不补**；歧义基号只报不补；围栏内不动；幂等 |



| `rewrite_placeholder_marks` | `CON-04` | 只改写 `【…】`/`[待补充…]` → `按设计文件及现场实际确定`（**不引入任何数值**）；紧随单位保留为括号注记；`××` 形态**只报不改**；围栏内不动；幂等 |







- **接线位置是硬约束**：必须在 `standard_report(` **之前**就地修复，否则



  `standard_report` / 检查点自检 / 字数 / SSE 载荷 / 落库正文**五处口径不一致**



  —— 与本文件上方「编号规范化必须在 `standard_report` 之前」是同一条结论。



  修复动作留痕进 `report["checkpoint_fixes"]`（可观测）。



- 整个修复块包 `try/except`（fail-soft），受既有 `content_selfcheck_autofix`



  控制（默认 `False`）→ **关闭时正文逐字不变**。



- ⚠️ **零新增配置项**：不引入第 4 个开关，避免「开关套开关」。







#### 4.25.3 【P1】章节归类漏映射 → `SAF-03/04/05` 在生成侧从未被要求过







生产库 13 个真实叶子标题实测：主判据 `chapter_key_of_title`



（`TITLE_TO_CHAPTER` + `CHAPTER_TITLE_ALIASES`）对下列标题**一律返回空串**：







```



应急组织机构及职责 / 应急物资装备保障 / 应急演练 / 触电事故急救及疏散



照明及手持电动工具管理 / 装修动火作业审批 / 环保检测及竣工资料移交



装饰装修主要材料进场计划 / 1.1 工程基本情况 / 1.2 周边环境 ……



```







根因：正文**只对叶子落库**，而这些是二级/三级小节，标题既不等于也不互含



九大章节标准名「应急处置措施」。于是 `CHAPTER_CHECKPOINT_REQUIREMENTS` 里



锚定 SAF-03/04/05 的三条要求在生成侧**既没被要求、也没被自检**。







- **修法**：新增 `infer_chapter_key(title, primary_key)` —— 主判据命中**直接



  返回不覆盖**（杜绝改变 facts 注入与提取分类的既有归类），未命中才按补充表



  `CHAPTER_TITLE_SUPPLEMENT` 兜底；仍未命中返回空串（宁可漏注入也不猜）。



- ⚠️ **不直接改 `chapter_key_of_title`**：它是**结构化提取按章注入**与**全局事实



  按章前置**共用的分类器（§4.11 / §4.15 契约），改它的返回会连带改变那两条链路。







#### 4.25.4 ⚠️ 配套 `subsection_scope`：不做这一步就是**用假缺项换真漏判**







审核侧 `preflight.check_safety` 是把标题含「应急/救援/预案」的章节正文**聚合**



后判定 SAF-03/04/05 —— **章级聚合**判据。而生成侧自检是**逐叶子**跑的。



若只补映射不区分作用域，「应急组织机构及职责」这个小节会被要求同时具备应急



物资与演练（它本来就不该写那两件事）→ 产出 **2 条假缺项**。







`_req_in_scope()` 判据：小节标题须与该要求的 `must_include`/`alt_include` 或



`label` 相关。默认 `subsection_scope=False` **保持旧行为不变**（向后兼容红线）。







#### 4.25.5 【P1】占位符正则过度匹配会**吞掉正文**







```python



r"【待(补充|定|确认)[：:】]?[^】\n]{0,20}】"   # 内部字符类不含 【



```







输入「深度【待定】m 宽度【待补充：宽度】mm」→ 匹配到



`【待定】m 宽度【待补充：宽度】`（**一个**占位符），中间的「m 宽度」这段正常



正文被吞。自检报告会把 2 个占位符报成 1 个；`rewrite_placeholder_marks` 更会把



那段正文一起删掉。修法：内部字符类排除 `【`（`[^】\n【]{0,20}`），两处消费方



（自检 + 改写）**共用** `_PLACEHOLDER_RES`，修一处即两处生效。







#### 4.25.6 「检查点 → 生成约束 → 实现方式」映射表（交付形态落为代码）







`checkpoint_constraint_map()` = **30 条跨章条目**（`CROSS_CHAPTER_CONSTRAINTS`）



+ **14 条章级派生条目**（派生自 `CHAPTER_CHECKPOINT_REQUIREMENTS`，`constraint`



逐字取其 `note` → **不另抄措辞**）。







- **12 类分组**（与需求清单一致）：内容完整性 / 事实一致性 / 章节编号 / 图表要求 /



  语言质量 / 格式规范 / 逻辑连贯 / 数据真实性 / 专业深度 / 合规性 / 危大工程 / 其他。



- **6 类覆盖通道**（封闭枚举）：`system_preprompt`（system 硬约束）、



  `chapter_preprompt`（逐章注入）、`selfcheck`（生成后自检）、



  `autofix`（确定性自动修复）、`export_pipeline`（导出期兜底）、



  `audit_fallback`（只能事后审核 —— **刻意不前置**，如跨章重复段落、语义级判定）。



- **判据同源**：`rule_ids` 只作**指针**指向 `audit_rules` 注册表，措辞与阈值一律



  不在本表复制；`validate_constraint_map()` 锁定「每个 rule_id 真实存在」+



  「12 组全覆盖」+ 「通道枚举封闭」，**36 个锚点全部校验通过**。



- 端点：`GET /api/v1/compliance/checkpoints`（`anchor_problems` **非空即分叉告警**）。







#### 4.25.7 ⚠️ 已知遗留（本轮**刻意不落地**，记录以免重复排查）







1. **审核侧 `STD-04` 对非危大方案误报**：生产方案是「装饰装修专项施工方案」，



   `is_hazardous_by_keywords` 判为**非危大**，生成侧据此**正确地**不要求引



   37 号令；但审核侧仍报了 `STD-04`。属**审核侧**判据未尊重危大前提 ——



   按 §4.23.7 的教训，**不得**在本轮用「放宽生成侧判据」的方式消除，应单独收敛



   审核侧（下一轮）。



2. **`CON-SCAN-1`「或」择一语义**（`18mm 多层板 or 12mm 竹胶板` 误判矛盾）：



   沿用 §4.24.7 结论，仍**不做** —— 无法确定该冲突来自程序预扫描还是 AI 分片扫描。



3. **AI 级重生成**：占位符改写只做**确定性**条件式改写；「×× 形态」与「本章缺



   要素」需要 AI 重写，属**新增 AI 调用**，按「默认关闭 + 单列开关」原则不落地。



4. **映射表前端未接**：`/compliance/checkpoints` 已有，但前端「审核规则」页尚未



   渲染该表（本轮有意零前端改动 —— 避免在一个未验证的表上铺 UI）。







#### 4.25.8 护栏与 A/B 反向验证







`tests/test_content_checkpoint_closure_20261002.py` **66 例**（映射表完整性 17 +



章节 key 补充推断 7 + 小节作用域 8 + 裸编号修复 12 + 占位符改写 10 + 接线 8 +



默认值 4 + 端点 2）。







**A/B 反向验证 8 项全部定向失败**（合计 11 例定向失败）：







| 还原的修复点 | 定向失败 |



|---|---|



| A1 注入点退回 `chapter_key_of_title`（还原补充推断） | **1** `test_injection_uses_infer_chapter_key` |



| A2 自检移除 `subsection_scope` | **1** `test_selfcheck_receives_subsection_scope` |



| A3 自动修复块整体移到 `standard_report` **之后** | **1** `test_autofix_runs_before_any_report` |



| A4 占位符正则恢复过度匹配（内部允许 `【`） | **1** `test_adjacent_placeholders_not_swallowed` |



| A5 `infer_chapter_key` 覆盖主判据（只补不覆盖失效） | **3** |



| A6 裸编号库外也硬补年号（编造年号） | **2** |



| A7 `××` 形态也自动改写（改错比不改更糟） | **1** `test_xx_form_reported_not_rewritten` |



| A8 自动修复不再受开关控制（默认即改写正文） | **1** `test_autofix_gated_by_switch` |







⚠️ **A/B 脚本自身踩坑（本文件 §5 的第五例，记以免重复）**：首版 A/B 脚本被工具的



30s timeout 打断后**仍在后台改源码**，未察觉就发起了第二次运行 → 两个实例并发



改同一份源码，「定向失败」变成随机噪声，且 **A5 的变异残留进了主源码**（把



`infer_chapter_key` 的 `if key: return key` 删掉，主源码少了 2 行）。



**教训**：① A/B 脚本必须**单实例锁**（本轮用锁文件，`已存在则拒绝启动`）；



② 任何后台起的进程，被 timeout 打断后**必须先确认它真的死了**再发起下一次；



③ 变异必须 `try/finally` **逐次**还原（不是全部跑完再还），并断言**字节一致**；



④ 还原后必须**再跑一次全绿**确认 —— 本轮正是靠这一步发现源码被污染。







### 4.26 第二十五轮（2026-10-02）：审核检查点反向增强 · **目录生成侧**







> 第二十四轮只把检查点前置到了**正文生成**。本轮补齐**目录生成侧** —— 而预检里



> 有一整类问题**只能在目录阶段预防**（标题里没有法定关键词 → 章节就不存在）。



> 生产库 `preflight_runs`（scheme=d3c1a897…「装饰装修专项施工方案」）实证：



> `CMP-01/02/03/04/05/09` 六条命中（3 block），**都不是审核侧误报**。







#### 4.26.1 【P0】九大章节关键词有**三份**副本，且已实证分叉







| 副本 | 位置 | CMP-01 认的词 |



|---|---|---|



| 审核注册表 | `audit_rules._COMPLETENESS_RULES[].keywords` | 工程概况 / 工程基本 / 周边环境 |



| 预检引擎 | `preflight.check_completeness` 直读上表 | 同上 |



| **目录侧手抄** | `sse_handlers._DANGEROUS_REQUIRED_KEYWORDS` | 工程概况 / **工程概述** |







实测分叉（全部会导致「目录侧放行、预检照报」）：







| 规则 | 目录侧独有的词（审核侧不认） | 审核侧独有的词（目录侧漏判） |



|---|---|---|



| CMP-01 | **工程概述** | 工程基本、周边环境 |



| CMP-03 | **施工部署** | 进度计划、施工进度、材料计划、设备计划 |



| CMP-06 | **组织机构** | 人员配备、作业人员、岗位职责、管理人员 |



| CMP-09 | **图纸** | 验算、受力计算、附图、相关图纸 |







#### 4.26.2 【P0】门控不对齐：非危大专项方案目录侧**从未**被要求过九章







- 预检 `check_completeness` 对 CMP-01~09 **无条件**检查；



- 目录侧「必须包含九章」只写在提示词的**危大分支**里；



- 且整段程序化覆盖预检被 `if OUTLINE_REVIEW_MODE != "always" and (requirements or basis is not None)` 门控 —— **未填「编制要求」时整段跳过**。







定位切换（§4.22）后非危大专项方案是**主力场景**，于是目录侧这条约束对主力场景



**从未生效**。现改为恒定参与（受 `settings.outline_checkpoint_check` 控制，默认



True；设 False 完整回退到旧门控与旧判据）。







⚠️ 原有「危大 10 章」检查**保留不动**（它是**结构性**要求：须独立**一级**章节，



刻意只判 L1），新检查与之**并存互补**（对齐预检的**全层级**标题匹配）。







⚠️ **「跳过 AI 审核」的早退条件刻意保持原样**（仍要求用户填了编制要求 /



提供了方案解析）：AI 审核除九章齐备外还查标题质量、层级、跨章重复、投标章节，



无条件跳过会把这些检查一并丢掉。所以「未填编制要求 + 目录完整」= 旧行为；



只有**确有缺失**时才改走外科补齐（本轮要达成的效果）。







#### 4.26.3 【P1】分叉是**双向**的：`preflight_engine` 也内联了不一致的元组







`check_safety` 的 SAF-03~06、`check_traceability` 的 TRC-03 判定读的是**函数内联的



元组字面量**，与 `audit_rules.keywords` 双向不一致：







| 规则 | 注册表有、引擎无 | 引擎有、注册表无 |



|---|---|---|



| SAF-03 | 职责 | 领导小组 |



| SAF-04 | 应急物资、救援器材 | 器材、储备 |



| SAF-06 | 预警值 | 变形、沉降 |



| TRC-03 | — | 图纸 |







修法：把这 6 组字面量**提取为 `preflight_engine` 模块级常量**（取值**逐字不变**，



审核行为零变化），供目录侧与引擎共用；并把注册表 `keywords` 按引擎实际谓词



**取并集补齐**（只增不减，对引擎零影响 —— 引擎读的是自己的常量）。







#### 4.26.4 ⚠️【P1】并集方向陷阱（本轮踩到并当场纠正）







首版实现把旧目录侧关键词**并入**判据，理由是「只增不减、无回归」。**实测证伪**：



「工程概述」仍放行而审核报 CMP-01 —— 并集让**目录侧判据 ⊇ 审核侧判据**，



方向**正好相反**，等于把分叉固化下来。正确方向：目录侧判据**完全等于或略宽于**



审核侧，使「目录侧判覆盖」**蕴含**「预检也判覆盖」。







护栏双向锁定：`test_keywords_superset_of_audit_registry`（不得漏注册表词）+



`test_legacy_only_word_not_in_any_keywords`（旧独有词不得回流）。







同理 `CMP-*` **不得**并入引擎常量：`check_completeness` 读的就是注册表，再并入



`CALC_TITLE_KEYWORDS`（含「承载力计算/稳定性验算/安全系数」等 CMP-09 从不用于标题



匹配的词）会让目录侧判据宽于审核侧 —— **实测 4000 组穷举中 1834 组如此**。







#### 4.26.5 【P1】内容侧：词级判据 vs 编号级判据







`basis` 章的检查点只要求出现「法律法规/标准/规范」三个**词**，而预检判的是



**编号**（STD-02 至少一个 GB 55xxx；STD-05 至少一个本类别现行标准编号）。



词过了而编号没有 → 正文侧全绿、预检照样报 STD-05 high（生产库实证）。







新增 `_basis_standard_findings`（挂在 `basis` 章的生成后自检，随



`content_selfcheck` 开关，默认关），判据直接复用 `preflight_engine._STANDARD_CODE_RE`。







⚠️ **本轮当场踩到的坑**：首版复用了本模块的 `_BARE_CODE_RE`，结果**合规正文被判缺失**



—— 它是「裸编号（无年号）」检测器，带 `(?![-\s]*\d{4})` 负向断言，



`GB 55034-2022` **根本匹配不到**。**同一个文件里两个「标准编号正则」语义相反**，



这本身就是分叉。教训：判「**有没有引用**」与判「**是不是裸编号**」是两个问题，



必须用两个不同的判据（护栏 `test_bare_code_regex_cannot_be_reused`）。







#### 4.26.6 护栏与 A/B 反向验证







`tests/test_outline_checkpoint_20261002.py` **44 例**（判据同源 7 / 分叉方向 4 /



门控对齐 8 / 监测类别门控 5 / 接线静态锁 7 / 编号级自检 11 + 1 参数化）。







| 还原的修复点 | 结果 |



|---|---|



| 并入旧目录侧关键词（首版错误方向） | `test_legacy_only_word_not_in_any_keywords` 定向失败 |



| `CMP-09` 并入 `CALC_TITLE_KEYWORDS` | `test_engine_predicates_not_registered_for_cmp` 定向失败 |



| 门控退回 `(requirements or basis is not None)` | `test_non_hazardous_still_checked_in_coverage` 定向失败 |



| 提示词占位符不独占行 | `test_prompt_placeholder_on_own_line` 定向失败 |



| 关闭开关后仍传空串（非逐字回退） | `test_prompt_renders_and_block_absent_when_off` 定向失败 |



| `_basis_standard_findings` 改用 `_BARE_CODE_RE` | `test_bare_code_regex_cannot_be_reused` 定向失败 |



| `checkpoint_selfcheck` 不再转发方案上下文 | `test_selfcheck_forwards_scheme_context_to_basis_check` 定向失败 |







**核心不变量**：`test_no_over_report_in_exhaustive_search` —— 穷举 **2000 组**



标题子集，断言「目录侧判缺失 ⊆ 审核侧判缺失」。这是「降低预检问题」的**充分



条件**：目录侧放行的目录，预检不会报。反向（目录侧多报）会产生假缺项、白跑



一次外科补齐 AI 调用。







**A/B 反向验证 7 项全部定向失败**（合计 14 例定向失败，每次变异 `try/finally`



立即还原并断言字节一致，收尾再跑一次确认 0 失败）：







| 编号 | 变异 | 定向失败数 |



|---|---|---|



| A1 | 门控退回 `(requirements or basis is not None)` | 1 |



| A2 | 旧目录侧关键词回流进判据（并集方向反了） | 4 |



| A3 | `CMP-09` 并入 `CALC_TITLE_KEYWORDS`（判据宽于审核侧） | 1 |



| A4 | 编制依据自检改用裸编号检测器 | 3 |



| A5 | 提示词占位符不独占行 | 2 |



| A6 | 关闭开关后仍传变量（非逐字回退） | 2 |



| A7 | `checkpoint_selfcheck` 不转发方案上下文 | 1 |







⚠️ **A/B 反向验证本身抓出了一个护栏缺口**（这是做 A/B 的最大价值）：



A7 首跑时**变异后 43 例仍全绿 = 护栏架空**。原因是 `TestBasisStandardSelfcheck`



的 10 个用例**全部直调** `_basis_standard_findings`，而



`test_wired_into_selfcheck_for_basis_chapter` 只断言 STD-02 ——



**STD-02 不需要方案上下文**，于是 `checkpoint_selfcheck` 停止转发



`scheme_name` / `scheme_type` 时没有任何用例会失败（STD-05 会静默永久跳过）。



已补 `test_selfcheck_forwards_scheme_context_to_basis_check`：**走 selfcheck



端到端**、只断言必须转发上下文的 STD-05。



**教训**：接线类护栏要选**必须依赖被转发参数才有结论**的断言，而不是选一个



「恰好不需要该参数也能通过」的规则码 —— 否则断链永远不可见。







⚠️ **本轮工具踩坑（三例，记以免重复）**：



1. **`editor` 分段插入静默截断**：`insert_line` 落在函数中间时，工具



   **不报错但只写入 `new_text` 的前半段**，造成源码/测试**结构错乱**（函数体丢尾行、



   一个类的用例被挪到另一个类）。**教训**：分段插入后**必须** `ast.parse` + 统计每个



   类的 `FunctionDef` 数量复核（本轮正是靠 `ast` 发现 `TestMonitorGate` 从 5 个



   变成 1 个、以及 `test_empty_outline_reports_all` 整段丢失）。



2. **PowerShell `Set-Content -Encoding UTF8` 会写 BOM**：用它做「读-改-写」后



   `app/services/preflight_engine.py` 头部多出 `U+FEFF`，`ast.parse` 直接



   `SyntaxError: invalid non-printable character U+FEFF`（3 个用例定向失败）。



   **教训**：改源码一律走字节级或编辑工具；必须用 PowerShell 时改用



   `[System.IO.File]::WriteAllBytes`。另：本仓另有 4 个 **既有**（非本轮引入）



   含 BOM 的测试文件 —— `tests/test_charts_ai_image_gate.py` /



   `tests/test_content_fence_contract_20260929.py` / `tests/test_export_cache_unique.py` /



   `tests/test_global_facts_fact_lines.py`，建议后续统一清理。



3. **A/B 脚本的两个锚点坑**（本轮各踩一次）：① **多行锚点必须按行尾归一化匹配** ——



   `sse_handlers.py` 是 **CRLF**，用 `\n` 拼锚点会**一个都匹配不到**（A1 首次直接报



   「锚点出现 0 次」）；正确做法是解码后先 `.replace("\r\n","\n")` 再匹配与替换，



   写回时按该文件原本的行尾还原。② **锚点出现次数要按实际写** —— `outline.py` 里



   `{outline_checkpoint_block}` 出现 **3 次**（三个模板），`expect=1` 会被自己的



   断言误判成「锚点缺失」。③ 两条工程习惯本轮都生效了：锚点断言放在**写文件之前**



   （两次锚点错误因此零污染），以及 `try/finally` **逐次**还原（脚本中途异常退出时



   已执行的变异仍被原样写回，收尾 `0 失败` 即证明还原干净）。







### 4.27 检查点反哺（R29 · 2026-10-02）：生成即按审核标准执行 —— 剩余 P0/P1 收口







> 上一轮（第二十五轮 §4.26）把检查点前置到了目录生成侧，§4.25 做完了正文侧闭环。



> 本轮收口的是**生成侧与审核侧判据仍不一致**的剩余 3 项（全部按**判据同源**落地：



> 生成侧直接 import 预检侧判据函数 / 常量，**不在生成侧重抄正则与阈值**）。



> 全部新增检查零 AI、fail-soft、只读；新开关 1 个默认开、可完整回退。







#### 4.27.1 【P0 判据分叉】TRC-01 只有「词」没有「过程」







预检 `check_traceability` 的 TRC-01 判的是**公式与参数代入过程**（`_FORMULA_RES`），



是 **block** 级；而生成侧 `CHAPTER_CHECKPOINT_REQUIREMENTS` 只判「计算」这个**词**。



**词过了而过程没有 → 生成侧自检全绿、预检照报 block**（典型缺陷：`本节进行稳定性



计算，结果详见附表`）。







- 落点：`preflight_engine.has_calc_process = _has_calc_process`（**同一函数对象**的



  公开别名，保留原名让模块内既有调用点零改动）；



  `content_checkpoint._calc_process_findings` 直接调它，**不重抄正则**。







⚠️ **接线时发现并修掉一处真实分叉（比原计划多修一项）**：只按



`chapter_key == "calc_drawings"` 判「这节是不是计算书章节」也会漏检 ——



预检的 `CALC_TITLE_KEYWORDS` 含「承载力计算」「安全系数」，而章节归类补表



（`CHAPTER_TITLE_SUPPLEMENT`）只到「计算书 / 验算」。新增 `_is_calc_section(title,



chapter_key)`：**并集**判据（归类命中 **或** 标题命中预检同一常量），



常量读取失败 fail-soft 回落到归类判据。



**两个维度都取预检同一出口**，分叉在结构上不可能发生。







#### 4.27.2 【P1】CON-06 跨章搬运是**唯一无法在提示词预防**的检查点







生成单章时模型**看不到**其他章节正文，system 级「禁止成段雷同」只能提高概率、



无法保证。生产库 3 条 CON-06 全部是「骨架归一后相似度 100%」（整段照抄，连数字



都没换），属评审硬伤。故该判据只能挂**生成后自检**：







- `content_checkpoint.cross_section_copy_findings` 直接复用



  `duplicate_detection.find_cross_section_copies`（预检 CON-06 的同一实现），



  不复制相似度阈值与骨架归一规则；



- 接线在 `sse_handlers._persist_section` 的 `checkpoint_selfcheck` **之后**，



  结果并入同一份 `_ck_findings`；**锁外只读**一次同方案章节，`db.execute()`



  按 R13 判空，异常只打 WARNING（绝不阻断正文落库）；



- 本章尚未落库，用**内存里的最终正文**参与比对（否则本章 content 为空）；



- 只报涉及本章的搬运组（避免把历史搬运重复报出）。



- 新开关 `config.content_crosscheck_duplicate`（**默认 True**）；关它完整回退。







#### 4.27.3 【P1】STD-03 装饰保温类误报消除（补库而非放宽判据）







生产方案「装饰装修专项施工方案」的 STD-03 报「未收录标准编号」，但被报的



GB 50209-2010 / GB 50222-2017 / GB 50009-2012 **都是真实现行规范** —— 根因是



**标准库覆盖不足**，属审核侧误报。`CATEGORY_STANDARDS["装饰保温"]` 补入三条，



`STANDARD_DB_VERSION` bump 至 **2026.10.2**。



⚠️ 补库不等于放宽判据：杜撰编号（`GB 99999-2099`）仍判未收录（有护栏锁定）。







#### 4.27.4 护栏与本轮实测







`tests/test_checkpoint_feedback_20261002.py` **52 例**：



判据同源（别名同一对象 / monkeypatch 打补丁后结论随之改变 / 静态扫仓禁止本地



公式正则 / `_is_calc_section` 对 `CALC_TITLE_KEYWORDS` 逐项 parity + 反例 +



并集 + fail-soft）/ CON-06（透传共享检测器 + 仅报本章 + limit + 坏输入 fail-soft



+ 检测器抛错 fail-soft）/ STD-03（补库三条 + 版本 + 注入清单 + 杜撰仍判缺失）/



接线静态锁（顺序 / 双重门控 / 判空 / 锁外 / 并入 `_ck_findings`）/



`validate_constraint_map()` 回归。







**后端全量 `python -m pytest tests/ -q` = 5012 passed, 4 skipped, 3 xfailed,



0 failed（237.45s）**。







#### 4.27.5 本轮工具踩坑（记以免重复）







1. **工具命令有 30s 硬上限**，`timeout` 参数被忽略 —— 全量 pytest（~240s）必须走



   `Start-Process ... -RedirectStandardOutput` 后台起 + 轮询进程表，不能直接



   `run_commands` 跑。



2. **`\r` / `\n` / `\t` 在命令字符串里会被当作转义吃掉**：`...\Temp\r29_prod_check.py`



   被解析成 CR + `29_prod_check.py`（文件真的被建成带 CR 的名字）；



   稳妥写法是 `$env:TEMP/name.py`（正斜杠 + 变量展开）。



3. **诊断脚本一律用 ASCII + `\uXXXX` 转义写**：控制台是 GBK，打印含 `²`（`\xb2`）



   的字符串直接 `UnicodeEncodeError` 中断（本轮 `repr()` 一个正则就炸了）。



4. **护栏要防空转**：断言 `not findings` 的阳性用例，若标题/章节判据本身判否，



   `not []` 恒真 → 用例**静默通过但什么也没测**。修法：helper 内先断言



   「样本确实被判为该章节」再做结论断言。



5. **`checkpoint_selfcheck` 的形参是 `is_hazardous_basis`**（不是 `is_hazardous`）——



   写护栏时务必先 `inspect.signature` 核对，否则整组用例报 TypeError。







#### 4.27.6 未完成（记录以免重复排查）







- **生产库修复前后对比测量仍阻塞**：`backend/data/scheme_assistant.db` 正被后台



  进程重建（实测 **88 章节 / 有正文 0 / 总字数 0**）。重建前最后一条 preflight



  运行（2026-10-01 15:55）为 **20 findings / D 级 / released=0**。



  ⚠️ 注意 `preflight_runs` **没有 `score` 列**（分数在 `dimensions`/`counts` 里，



  查询别写 `score`）。待重建完成后用稳定基准重测。











### 4.28 检查点反哺续（R30 · 2026-10-02）：CON-04 中文计量单位吸收 + 生产 findings 过期性核实







> 起点是**重新拉取生产库 findings 明细**（上轮记录为「生产库重建中、无法测量」）。



> 结论分两部分：**一个真实缺陷**（已修 + 护栏 + A/B）与**一批过期数据**（已核实、无需改代码）。







#### 4.28.1 【P1】占位符后的**中文**计量单位全部漏收 → 改写后遗留孤立单位







`content_checkpoint._PLACEHOLDER_TRAILING_UNIT_RE` 此前只收拉丁计量单位



（`m|mm|cm|km|MPa|kPa|kN|kg|t|%|元|万元|天|日历天|人|台|套|个|件`），中文单位一个都没有。



改写机制本身是对的（`【待补充】` → 条件式表述 `按设计文件及现场实际确定`，紧随其后的



单位收成括号注记），但**中文单位不被识别**，于是直接连在条件短语后面：







| 原文（生产 findings 实证） | 旧输出（读不通） | 新输出 |



|---|---|---|



| 堆放区面积【待补充：堆放区面积】平方米 | …确定**平方米** | …确定**（平方米）** |



| 每周清运不少于【待补充：清运频次】次 | …确定**次** | …确定**（次）** |



| 开挖深度【待定】米 | …确定**米** | …确定**（米）** |







**补的是无歧义单位**：`平方米`/`立方米`/`公斤`/`小时`/`米`/`遍` —— 不存在以它们起首的



常用词，紧随占位标记时几乎必然是单位。**`次` 加了 `(?!日)` 守卫**：`次日`（next day）



是常用词，不加守卫会把「【待定】次日恢复施工」改写成「…（次）日恢复施工」。



**刻意不补 `周`/`月`**：`周边`/`月末` 会以它们起首，误收风险高于收益 —— 该取舍按



**行为**锁定（`test_week_and_month_words_deliberately_not_absorbed`），不锁正则字面量，



避免后人改写法即误伤。







#### 4.28.2 生产 findings 大多是**过期数据**（逐条核实，无需改代码）







上轮因「生产库重建中」无法做前后对比。本轮重新拉取 `preflight_runs` 最新一条的



20 条 findings 逐条核实：







| 生产 finding | 当前代码行为 | 判定 |



|---|---|---|



| STD-03 报 6 个「未收录编号」 | 只报 **1 个**（`GB 18581`） | 过期 |



| CON-04 占位符残留 | 7 种生产形态全部改写、0 残留、幂等 | 过期 |



| SAF-02 / SAF-07 缺要素 | 生成侧检查点已含（`must_include` 高处作业/临电/消防/机械 + 特种作业/持证） | 过期 |



| STD-04 报非危大方案缺 37 号令 | 已按 `is_hazardous_by_keywords` 门控 | 过期（R26~R28 已修） |







**STD-03 的 6 个编号逐个核对**（本轮唯一需要人工判断的一项）：







| 编号 | 性质 | 当前是否报 |



|---|---|---|



| `GB 55034` / `GB 55032` | 全文强制性规范，现行 | ❌ 基号豁免 |



| `GB 50210` | 建筑装饰装修工程质量验收标准，现行 | ❌ 基号豁免 |



| `GB 50325-2020` | 民用建筑工程室内环境污染控制标准，现行 | ❌ 在库 |



| `GB 12523-2011` | 建筑施工场界环境噪声排放标准，现行 | ❌ 在库 |



| `GB 18581` | 已被 GB 30981-2014 替代 | ✅ **该报** |







→ 该 findings 为过期数据；当前代码只报真正作废的 `GB 18581`，是**正确行为**。



护栏 `TestStd03BaseNumberExemption` 锁两端：真实现行规范一律放行、作废与杜撰编号照报。







#### 4.28.3 护栏与 A/B 反向验证







`tests/test_checkpoint_feedback_20261002.py` **52 → 71 例**（+19）：



`TestPlaceholderTrailingChineseUnits`（11 例 = 7 个中文单位参数化 + 拉丁单位不回退 +



孤立单位残留扫描 + 幂等 + `次日` 守卫 + `周边/月末` 刻意不收）与



`TestStd03BaseNumberExemption`（8 例 = 5 个现行编号 + 作废编号 + 杜撰编号）。







**A/B 反向验证**：删掉新增的两行单位分支 → **8 例定向失败**（7 个中文单位 +



1 个孤立单位残留扫描）；还原后字节一致（sha256 断言）且 **71 全绿**。



其余 13 例在变异下仍通过是**预期** —— `次日` 守卫测试不依赖新分支，它保护的是守卫



本身（若把 `次(?!日)` 写成裸 `次` 就会失败）。







#### 4.28.4 仍未完成







- **生产库前后对比测量**：本轮复查时 `backend/data/scheme_assistant.db` **仍在重建**



  （88 章节 / 有正文 0 / 总字数 0），最后一次 preflight 仍是 2026-10-01 15:55。



  正文全空时跑 preflight 会得到无意义结果，故**不做前后对比**。判据：



  `with_content > 0` 且 `total_words > 0` 后再测。



  ⚠️ 但 §4.28.2 的逐条核实已达同一结论：**当前代码已正确处置该批 findings**。



- **CON-SCAN-1「或」择一语义**（`18mm 多层板 or 12mm 竹胶板` 误判矛盾）：沿用



  §4.24.7 / §4.25.7 结论，仍**不做** —— 无法确定该冲突来自程序预扫描还是 AI 分片扫描，



  且不得用放宽判据的方式消除真实缺陷。











### 4.29 目录生成 + 正文生成增强（R31 · 2026-10-03）：判据收敛 + 要素/法规清单接入生成链路







> 依据「目录与正文生成模块增强」需求执行。全部**默认向后兼容**、零新增依赖、零数据迁移；



> 新增配置项 1 个（默认 True）、新增护栏 1 份 39 例。







#### 4.29.1 【P1】占位标记判据收敛为单一事实源（「检出却修不掉」）







`content_fuzzy.PLACEHOLDER_MARK_PATTERNS` 成为占位标记检测的**唯一事实源**：



5 条形态（3 基础 `RE_FORMATTED`/`RE_BARE`/`RE_FUZZY` + 2 扩展）配 `(kind, rewritable)` 标记，



派生 `placeholder_rewritable_patterns()` / `placeholder_nonrewritable_patterns()` 两个出口。



`content_checkpoint._PLACEHOLDER_RES` 从**自带副本**改为表推导



（`tuple(rx for rx, _kind, _rw in PLACEHOLDER_MARK_PATTERNS)`）—— 「检出却修不掉 / 自检漏报」



的分叉在结构上不可能发生。







**新增可检测形态**（旧判据完全漏检）：`【待完善】【待确定】【待补录】【待补】【略】【】`、



`[待完善][待确定][待确认][待补录][待定][数值][参数][TBD][]`，以及裸 `TBD` / `N/A`。



护栏 `test_no_local_placeholder_regex_copy_in_checkpoint` 静态锁死：`_PLACEHOLDER_RES`



必须是表推导，且任何 `re.compile(...)` 参数内不得再出现 `待确认` 字面量。







⚠️ **子集比较必须用 pattern 字符串多重集合**：两个派生子集各自只保留自己的条目，



按序拼起来与原表顺序不同（`RE_FUZZY` 在原表第 3 位、拼接后第 4 位）。首版护栏写成



`list(rw) + list(nrm) == all_rx` → **恒假失败**。







#### 4.29.2 【P1】法规清单（含 48 号 + 2024 新规）注入生成链路







`get_standards_text(include_regulations=True)`（**默认开启，沿用既有开关**）把 `REGULATIONS`



全部注入 `{standards_text}` 占位符；目录模板与正文模板**都已具备**该占位符



（本轮静态护栏 `test_standards_text_injected_into_both_prompts` 锁定，防静默断链）。



本轮补齐三条：`建办质〔2021〕48号`（编制指南）、`建质规〔2024〕5号`、`建办质〔2024〕63号`。







⚠️ **核实状态只写注释、不写进字符串**：后两条文号的正式标题在 2026-10-03 实测**未能联网核实**



（mohurd.gov.cn / Bing 均无可靠结果）。按「不编造」红线只登记文号；更关键的是 `REGULATIONS`



全文会进入 AI 生成提示词 —— 若把「待核实」写进字符串，AI 可能原样抄进交付方案的编制依据章节。



护栏 `test_regulation_caveat_not_leaked_into_prompt` 双向锁定（无状态字样 + 文号后不跟括号说明）。







#### 4.29.3 【P0】九大章节**完整**必含要素清单 → 正文提示词







新增 `content_checkpoint.chapter_required_elements(chapter_key, scheme_name, scheme_type)`：



**完全委托** `scheme_classification.required_fields_for_chapter`



（`NINE_CHAPTERS.base_fields` + 命中的危大类别 `category_fields`，按顺序去重合并），



本模块**不复制清单**。`build_chapter_element_block()` 渲染提示段落；



`build_chapter_checkpoint_block(..., include_elements=True, scheme_name=, scheme_type=)` 追加。







⚠️ 要素块**必须重申红线**：不得只写要素名称不写内容；涉及事实数据以【全局事实变量】为准，



缺数值按模糊生成规则用条件式 / 范围式写完整，**严禁留占位标记**。







#### 4.29.4 【P0】同一份要素清单 → 目录生成提示词







新增 `outline_checkpoint.build_outline_element_block()`（取同一 `chapter_required_elements`



出口，两侧不可能分叉），经 `_outline_checkpoint_kwargs` 追加到**既有**



`outline_checkpoint_block` 变量 → **不改任何提示词模板、不改变量契约**。



这是本轮刻意选择的接线方式：模板已声明的独占行占位符是唯一锚点，新增占位符要同步改



`PROMPT_VARIABLE_CONTRACTS`，风险面更大。







⚠️ **为什么目录侧也要注入**：正文生成**只按目录给的标题写**。目录里没有「材料计划 /



劳动力配置」这类落点，正文阶段再要求「本章必须落实」也只能靠模型自己临时补标题 ——



覆盖率取决于模型自觉。







#### 4.29.5 新增配置项







| 配置项 | 默认 | 说明 |



|---|---|---|



| `content_chapter_elements_inject` | `True` | 九大章节必含要素清单注入正文 + 目录提示词；设 False = 提示词逐字回到本轮之前 |







#### 4.29.6 护栏与实测







`tests/test_content_outline_enhancement_20261003.py` **39 例** = 判据单一源 7 +



改写分流 8（含 `次日` 守卫 / 围栏不动 / fail-soft）+ 要素清单 5 + 正文注入 7 +



目录注入 7 + 法规贯通 5。关联回归（checkpoint / outline / placeholder / fuzzy /



content_standard 八份）共 **308 passed**。







⚠️ **本轮自引入又当场修掉的 2 个缺陷**：① 子集比较按序写 → 恒假失败（见 4.29.1）；



② 把 2024 新规的核实状态写进 `REGULATIONS` 字符串 → 会被 AI 抄进交付件（见 4.29.2）。







## 4.30 删除项目 / 删除章节的数据一致性（R32 · 2026-10-03）







> 依据「删除项目或章节时，确保相关文档、提取结果、目录、事实、正文、图表、



> 导出缓存和四层存储全部清理；避免出现孤儿数据或脏缓存」执行。



> 起点是**静态扫仓**：把 `schema_sql` 里所有含 `project_id` / `scheme_id` 列的表



> 列出来，与 `delete_project` 的清理登记表逐一比对 —— **不靠猜、不靠「我记得改过」**。







#### 4.30.1 结论：不是没有清理代码，而是**登记表漏了条目**







`delete_project` 的机制本身是对的（`_PROJECT_SCOPED_TABLES` +



`_SCHEME_SCOPED_TABLES` 两本登记表 + `doc_chunks` 按 `doc_id` 定向删），



但**登记表不完整**：







| 表 | 问题 | 后果 |



|---|---|---|



| `uploaded_outlines` | **完全不在任何登记表** | 上传识别的目录（raw_text / parsed_json 整份留库）**永久孤儿** |



| `schemes` | 在表内但被注释标为「自带 CASCADE」 | 见下条 —— 级联不生效 |



| `placeholder_baselines` | 自带 CASCADE，同上 | 导出预检的占位符统计快照残留 |







⚠️ **本轮实测：运行时 `PRAGMA foreign_keys` 恒为 0**（`app/db.py` 与



`schema_sql.py` 全仓零命中 `foreign_keys`）。即**所有 ON DELETE CASCADE 都是空声明**



—— 删项目后二十余张派生表的「靠级联」假设**全部不成立**，只有显式 DELETE 的



才真的被删。这是本仓删除链路的**根因性**风险，不是某一轮的漏改。







#### 4.30.2 生产库实证（`backend/data/scheme_assistant.db`）







| 表 | 行 | 孤儿 |



|---|---|---|



| `bid_analysis_items` | 41 | 0 |



| `bid_sections` | 4 | 0 |



| `uploaded_outlines` | **88** | **88（全部）** |



| `project_documents` | 105 | 0 |



| `schemes` | 16 | 0 |







`uploaded_outlines` **88/88 全部孤儿** —— 「上传目录」功能从上线起就没被删过。







**磁盘侧**：`data/projects/` 下 216 个目录 vs `projects` 表 1 行 → 215 个孤立目录



（含 `.trash/` 回收站，属设计内）；四层存储目录**内容全空**。



⚠️ 但 `delete_project` 的调用链**原本根本没调 `delete_project_docs_root`** ——



本轮已补上（try/except 包裹，返回体新增 `doc_trees_removed` 提示）。







#### 4.30.3 修法（全部**加法式**、零行为回退、零配置项）







1. `_PROJECT_SCOPED_TABLES` 补 `uploaded_outlines`；



2. `_SCHEME_SCOPED_TABLES` 补 `placeholder_baselines`；



3. `schemes` 保留在表内（显式删，不依赖失效的级联）；



4. 返回体新增 `cleanup_errors`（逐表 fail-soft 失败的表名）与 `doc_trees_removed`



   —— **`ok` / `files_removed` 契约一字未动**，且实测前端**零消费** `files_removed`



   （全仓 `Select-String` 0 命中），故加法式改动零影响面；



5. `import asyncio` 从函数内提到模块顶（与全仓风格一致）。







#### 4.30.4 护栏与 A/B 反向验证







`tests/test_project_delete_cascade_20261003.py` **14 例** = 全量级联清理 5



（建真实数据全表灌入 → 删项目 → 断言全部清零）+ 静态 parity 3



（**扫 schema_sql 自动比对登记表**，新增带 `project_id`/`scheme_id` 的表若未登记即失败）



+ 清理失败回传 2 + 返回契约 2 + 静态防分叉 2。







**A/B 反向验证 4 项全部定向失败**（单实例锁 + `try/finally` 逐次还原 + sha256 字节一致）：







| 变异 | 定向失败 |



|---|---|



| 移除 `uploaded_outlines` 登记 | **2** |



| 移除 `schemes` 显式删除 | **3** |



| 移除 `placeholder_baselines` 登记 | **1** |



| 移除 `consistency_scan_cache` 登记 | **3** |







⚠️ **parity 护栏的价值**：A1 定向失败的正是



`test_all_project_id_tables_are_cleaned_by_delete_project` —— 它扫 schema_sql



**自动**发现漏登记的表。这证明它能发现**未来新增表**的漏登记，



而不是只锁定当前已知的两个名字（否则就退化成两条写死的字符串断言）。







#### 4.30.5 实测







关联回归 8 份共 **103 passed**（doc_pipeline / sections / sections_reset /



export_cache / fact_lines / file_import_pipeline / status_vocab + 本轮 14 例）。



后端全量 `python -m pytest tests/ -q` 见 §6。







#### 4.30.6 ⚠️ 未落地项（记录以免重复排查）







1. **`PRAGMA foreign_keys=0` 本身没修** —— 那是全局连接配置，开启后**所有**依赖



   外键的表行为都会变（包括**外键约束开始报错**），属高风险全局变更。



   当前所有删除链路已改为**显式 DELETE**，级联是否生效已不影响正确性 ——



   保持现状，仅记录。



2. **`review_records.section_id` 在删章节后残留** —— `delete_section` 刻意



   **不删** review_records（评审留痕是可追溯性的硬要求，删章节不该销毁评审意见），



   接受 `section_id` 成为历史引用。



3. **历史孤立数据未清理**（生产库 88 行 `uploaded_outlines` / 215 个孤立目录）



   —— 属数据迁移，不在本轮范围；**新产生的删除已闭环**。



4. **`global_facts` 同时出现在项目级与方案级两本登记表**（重复 DELETE 一次）



   —— 无害，因涉及既有行为不在本轮改动。



5. **磁盘孤立目录无自动 GC**：建议单独立项 —— 需区分「回收站 `.trash/`」与真残留，



   否则会误删用户的回收站内容。















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



14. **护栏判据选错锚点，比不写护栏更糟**（2026-10-01 第二十一轮踩了两次）：写静态护栏时，「锚点越宽越严格」是错觉 —— 锚点选宽了会因为「说明文字 / 注释 / docstring 也含关键词」而**恒失败**，逼着后人把护栏改松或删掉，护栏就此名存实亡。



    - 案例 A（§4.22.8）：判 `content.py` 是否内联红线副本，用**纯文本匹配** → 被本文件顶部的说明注释误伤。改 AST 取全部 `Constant` → 又被 `_wrap_fuzzy_fill` 的 **docstring** 误伤（docstring 同为 `Constant`）。最终收敛为「**只取 `_reg()` 第 4 个位置参数**」。



    - 案例 B：判清单与提示词是否同源，用 `==` 全等比对 → 日后新增任何一个项都会因「列表顺序」失败。应只断言**集合相等**。



    - **判据必须与风险同构**：要拦的是「会下发给模型的那份文本」，就锚定那份文本本身（`_reg` 的模板实参、渲染后的 `get_prompt` 结果），而不是碰巧含关键字的一切字符串。



    - 附带：**护栏本身跑不过就先怀疑护栏**，别急着改实现 —— 先确认是不是自己刚写的注释把它挡住了。







15. **A/B 反向验证脚本被 timeout 打断后仍在后台改源码**（2026-10-02 第二十四轮）：



   A/B 脚本要做 8 次「变异 → 跑护栏 → 还原」，单次 pytest 约 5s，**必然超过工具的



   30s 命令超时**。把脚本后台起来（`Start-Process`）后，**工具超时并不杀掉那个



   python 进程** —— 它继续改源码；我未察觉就发起了第二次运行，两个实例并发改同一



   份文件。后果有三：① 「定向失败」变成随机噪声（每个变异都报出同一批与变异无关的



   失败）；② 锚点计数失真（A5 被误报「锚点出现 0 次」）；③ **变异残留进主源码**



   （A5 把 `infer_chapter_key` 的 `if key: return key` 删掉，主源码少了 2 行）。



   **教训**：① A/B 脚本必须**单实例锁**（锁文件，已存在则拒绝启动）；② 后台起的



   进程被 timeout 打断后，**必须先确认它真的死了**（查进程表）再发起下一次；



   ③ 变异必须 `try/finally` **逐次**还原并断言**字节一致**，不是全部跑完再还；



   ④ 还原后**必须再跑一次全绿** —— 本轮正是靠这一步发现源码被污染；



   ⑤ CRLF 源文件必须走「decode → 归一化 `\n` → 改 → encode → 恢复 `\r\n`」



   往返，直接用 `\n` 锚点在 CRLF 文件上**一个都匹配不到**（会误判成「锚点缺失」）。



16. **A/B 脚本的两个编码陷阱**（2026-10-02 第三十轮，各实际踩一次）：



    ① **断言要锚定实现、不要锚定字符**：用 `'\uXXXX'` 表示「应不存在」的字符时，



    若该字符同时出现在**注释里**（注释是文档、不是实现），断言会误报「变异未生效」。



    本轮首版断言 `'\u5e73\u65b9\u7c73' not in mut` 恒失败 —— 因为新加的正则注释里



    本来就在举这个单位的例子。正确写法是断言**待删片段本身**归零



    （`flat.count(DEL) == 0`）+ 只在正则块区域内查。



    ② **pytest 子进程在 Windows 下输出是 GBK**：`subprocess.run(..., encoding="utf-8")`



    会抛 `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xb6`，`r.stdout` 变 `None`，



    紧接着 `.strip()` 抛 `AttributeError`。若**还原步骤写在读取输出之后**，



    **变异就残留进主源码**（本轮实际发生一次，靠事后比对字节数 + sha256 才发现）。



    修法：`capture_output=True`（**不传 encoding**，拿 bytes）+ `.decode("utf-8","replace")`，



    且还原必须放在 `try/finally` 里、**先于**读取输出。



    ⚠️ 与 §5.15 同构：**还原必须与读输出解耦** —— 读输出失败绝不能阻断还原。











## 6. 测试基线







- 后端：`backend/tests/`（`test_*.py`），运行 `python -m pytest tests/ -q`；**2026-10-03 删除链路数据一致性（R32）实测基线 5140 passed, 4 skipped, 3 xfailed, 0 failed**。⚠️ 该基线 = R31 的 5075 + R32 新增护栏 **14 例**（§4.30）+ 历史波动 51。此前记录的 3697 / 3886 / 3976 / 4023 / 4048 / 4077 / 4313 / 4578 / 4667 / 4706 / 4746 / 4800 / 4871 / 4936 / 4937 / 5012 / 5036 / 5075 均已过期 —— 改动时请**以实测为准**、不要沿用文档里的旧数字。



- **删除链路数据一致性专项（R32 · 2026-10-03）**：`python -m pytest tests/test_project_delete_cascade_20261003.py -q`（**14 passed**）。覆盖全量级联清理（建真实数据全表灌入 → 删项目 → 断言 20+ 张派生表清零 + 四层存储磁盘目录消失）、**静态 parity 护栏**（扫 `schema_sql.py` 自动比对两本登记表，新增带 `project_id`/`scheme_id` 的表若未登记即失败）、清理失败回传与返回契约、磁盘四层目录接线、静态防分叉。已做 **4 项 A/B 反向验证**（移除 `uploaded_outlines` 2 例 / 移除 `schemes` 显式删除 3 例 / 移除 `placeholder_baselines` 1 例 / 移除 `consistency_scan_cache` 3 例定向失败；单实例锁 + `try/finally` 逐次还原 + sha256 字节一致断言）。关联回归 8 份 **103 passed**。详见 §4.30。



- **目录与正文生成增强专项（R31 · 2026-10-03）**：`python -m pytest tests/test_content_outline_enhancement_20261003.py -q`（**39 passed**）。覆盖占位标记判据单一事实源（表形态 / 可改写与只报不改子集互补 / `_PLACEHOLDER_RES` 表推导静态锁 / 旧形态回归 / 新增形态全检出 / 检测侧⇔自检侧 parity）、改写分流（可改写全形态 / 只报不改 / 幂等 / 相邻标记不吞正文 / 中文单位保留 / `次日` 守卫 / 围栏不动 / fail-soft）、九大章节要素清单（= `NINE_CHAPTERS.base_fields` / 危大类别追加 / 去重 / 空 key / 九章非空）、正文与目录双侧重注入（开关开闭 / 未知章节 / `STD-04` 门控不变 / `_outline_checkpoint_kwargs` / 总开关回退 / 模板占位符接线）、法规贯通（48 号 + 2024 新规 / `include_regulations` 双向 / 核实状态不泄漏）。关联回归八份共 **308 passed**。详见 §4.29。



- **CON-04 中文计量单位专项（R30 · 2026-10-02）**：`python -m pytest tests/test_checkpoint_feedback_20261002.py -q`（**71 passed**）。覆盖 7 个中文单位吸收（平方米/立方米/公斤/小时/米/遍/次）、拉丁单位不回退、孤立单位残留扫描、幂等、`次日` 守卫、`周边/月末` 刻意不收，以及 STD-03 基号豁免两端锁定（5 个真实现行规范放行 + 作废与杜撰编号照报）。已做 **A/B 反向验证**：删掉新增的两行单位分支 → **8 例定向失败**，还原后 sha256 断言字节一致且 **71 全绿**（详见 §4.28.3）。⚠️ A/B 脚本的还原必须与读输出解耦（Windows 下 pytest 子进程输出是 GBK，UTF-8 解码会崩，见 §5.16）。详见 §4.28。



- **检查点反哺专项（R29 · 2026-10-02）**：`python -m pytest tests/test_checkpoint_feedback_20261002.py -q`（**52 passed**）。覆盖 TRC-01 判据同源（`has_calc_process` 同一函数对象 + monkeypatch 打补丁后生成侧结论随之改变 + 静态扫仓禁止本地公式正则 + `_is_calc_section` 对 `CALC_TITLE_KEYWORDS` 逐项 parity）、CON-06 跨章搬运（透传共享检测器 + 仅报本章 + fail-soft）、STD-03 装饰保温补库、`_persist_section` 接线静态锁（顺序 / 双重门控 / R13 判空 / 锁外 / 并入 `_ck_findings`）、`validate_constraint_map()` 回归。关联回归：审核预检 + 内容生成 + 目录共 **814 passed**。详见 §4.27。



- **第二十五轮专项**：`python -m pytest tests/test_outline_checkpoint_20261002.py -q`（**44 passed**）。已做 **7 项 A/B 反向验证**（合计 14 例定向失败，还原后 0 失败），其中 A7 **当场抓出一个护栏缺口**并补了 `test_selfcheck_forwards_scheme_context_to_basis_check`（详见 §4.26.6）。核心不变量 = 穷举 2000 组标题子集的「目录侧判缺失 ⊆ 审核侧判缺失」（离线穷举 4000 组实测 0 分叉）。



- ⚠️ **全量跑测期间若有别的会话在改源码会得到随机失败**（本轮实测）：某次全量跑出 5 例失败（`FileNotFoundError` / `inspect.getsource` 拿到错位源码），单跑全部通过 —— 排查发现正是并发编辑（`test_outline_call_optimization.py` 等文件在跑测期间被写入）。**判据：单测单跑通过、仅全量失败 → 先怀疑并发编辑 / 边跑边改，而不是先怀疑自己的改动**（§6 末条的旧结论同样适用于「别人在改」）。



- **第二十四轮专项**：`python -m pytest tests/test_content_checkpoint_closure_20261002.py tests/test_content_checkpoint_prepend_20261002.py -q`（**111 passed** = 检查点闭环 66 + 检查点前置 45）。已做 **8 项 A/B 反向验证**（A1 注入点退回主判据 1 / A2 移除 `subsection_scope` 1 / A3 自动修复移到报告之后 1 / A4 占位符正则恢复过度匹配 1 / A5 补充推断覆盖主判据 3 / A6 库外编号硬补年号 2 / A7 `××` 也自动改写 1 / A8 修复不受开关控制 1 —— 全部定向失败，还原后字节一致且全绿）。⚠️ A/B 脚本必须**单实例锁**（被 30s timeout 打断的后台进程仍在改源码，见 §5.15）。详见 §4.25。



- **第二十三轮专项**：`python -m pytest tests/test_export_scheme_forms_20261002.py tests/test_hazard_threshold_p0_20261002.py -q`（**53 passed**）。已做 **7 项 A/B 反向验证**（还原 P0 根因 8 例 / 移除人工挖孔桩阈值 6 例 / 关闭缺参保守 3 例 / `_build_docx_sync` 中段插入必填参数 1 / 交换历史参数次序 1 / 追加参数重名 1 / 元组内追加元素错位 1 —— 全部定向失败，还原后 53 全绿且字节一致）。详见 §4.24。



- ⚠️ **改共享写路径必须跑全量**：第十三轮首次跑全量时前 4 个相关测试文件**全绿**，全量却暴露 4 条 `sqlite3.Row` 回归（§4.17.3）；第二十一轮改 `bid_analysis_service` / `routers/bid_analysis` / `scheme_classification` 三个共享模块，同样只跑定向子集不够。



- ⚠️ **Node.js 已可用（2026-10-02 第二十三轮更正）**：`node -v` = **v25.8.0**、npm 11.11.0。`frontend/node_modules` 齐备，`npx tsc --noEmit`（**0 错误**）与 `npm run test`（vitest 3.2.4，**53 文件 / 800 用例全通过**，379.68s）均可跑。此前记录的「本机未安装 Node.js、前端改动无法验证」**已过期** —— 前端改动现在可以且应当补跑 tsc + vitest。



- **定位切换专项（2026-10-01 第二十一轮）**：`python -m pytest tests/test_scheme_repositioning_20261001.py -q`（**38 passed**）。覆盖 `techScoring` 下线（四出口同时失效 + 清单↔提示词↔索引表同源）、招标响应域硬门禁（四入口恒 404 + AST 扫描全端点）、六大类危大子类覆盖（用户清单逐项可识别 + 新增 7 子类端到端）、九大章节完整性（`source_items` 不拆链 + 第一章覆盖六大类）、红线渲染后校验（占位符替换干净 + 两档禁用术语集合一致）、导出无投标模板。已做 **6 项 A/B 反向验证**（提示词回流 / 索引表残留 / 门禁旁路 / 恢复门禁 / 摘掉 `<<SCOPE_RULES>>` 注入点 → 各定向失败，恢复 → 全绿）。详见 §4.22。



- **参考能力对齐专项（2026-09-30 第十六轮）**：`python -m pytest tests/test_reference_alignment_20260930.py -q`（**33 passed**）。覆盖 old_text/new_text 定点替换（唯一命中才替换 / 多处命中拒绝 / 定点优先+整章重写兜底 / 依赖注入）。⚠️ 其中的「技术评分要求提取」组已随定位切换**翻转为反向护栏**（`TestTechScoringItem` → `TestTechScoringRemoved`，锁定「已下线且不可回流」），已做 **3 项 A/B 反向验证**（恢复 → 33 全绿）。



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



