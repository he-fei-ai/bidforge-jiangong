# AGENTS.md — 专项方案工具箱 · AI Agent 工作边界

> **最近校准：2026-10-14（引入易标全局事实机制 · 正文事实冲突自动定点修复 + 目录按章事实精选 R77）** —— **【用户任务：对参考软件 OpenBidKit 易标做全局事实提取/目录调用/正文调用/一致性保障四部分深度考古（4 个并行只读子代理，结论全部回源码 file:line 复核），产出全流程报告并引入本软件；零新增依赖、零数据迁移、零 DDL、返回契约加法式】**：
> ① **【考古核心结论】** 易标全局事实双实现：V1 分段流水线（`globalFactsTask.cjs`，逻辑完整但 `taskService.cjs:5` 只 import V2 ⇒ **未挂载**）与 V2 Agent 工作区（`globalFactsTaskV2.cjs:235-430`，生产在用）；**生产顺序是「标书分析→目录→全局事实→正文」，目录生成对全局事实读取点为 0 处**（`outlineGenerationTaskV2.cjs` 全文零 facts 匹配——目录是事实上游）；事实进正文靠**两级漏斗**（编排只见标题清单 `CG:828-830` → 生成按标题取完整内容只注本章 `CG:171-177/924`）；一致性保障 = 「只查明确写出冲突的审计（`CG:1667-1671`）→ old_text/new_text → **唯一命中才替换**（`CG:1584-1616`）→ 失败回灌重试≤2 → 转人工」+ Agent 修复（标记包裹全文 + 程序解析校验 + 只回写差异小节 `CG:5490-5528`）。
> ② **【差距定稿（本软件现状盘点）】** 本软件已有统一注入门控（`_FACTS_INJECT_WHERE` fail-closed 唯一出口）、逐章精选（2-gram 预筛 + chapter 前置 + 通用/本章组拆分）、交叉校验 XV 五规则、`consistency_edits` 唯一命中替换；差距在**接线深度**：G1 `standard_report` 检出 error 级事实冲突（value_mismatch/value_conflict/model_conflict）**只写报告不进修复链路**；G2 目录子目录对所有章节下发同一份整案事实头部 1000 字（长方案尾部事实对任何子目录不可见）；G3~G7（AI 编排 opt-in / fact_id 溯源 / enrich 开关 / 受影响章节重生成 / 要求vs事实类型）记录递延。
> ③ **【P0-1 已落地：生成后事实冲突自动定点修复】** 新模块 `services/content_fact_autofix.py`（`select_fact_conflicts` 纯函数筛 error 级三类 + `autofix_section_fact_conflicts`：AI 只产 edits → 复用 `consistency_edits.apply_unique_edits` 唯一命中应用 → 失败原因回灌重试上限 2 → 空 edits 安全阀 → fail-soft）；新提示词 `content_fact_autofix_system/_user`（8 条纪律，含「并非真实矛盾可返回空 edits」）；新 scene `content_fact_autofix`（KNOWN_SCENES 登记 + 真实使用）；`sse_handlers._persist_section` 在 standard_report 之后接线：仅在**检出冲突**的章节触发（无冲突章节零 AI 调用，成本中性），修复生效后**重算 standard_report** + 重算 wc/ws + 写报告新键 `fact_autofix`（{attempted,applied,rejected,conflicts,attempts}）；配置 `content_fact_autofix_enabled` **默认 True**（False 逐字回退）。
> ④ **【P0-2 已落地：目录子目录按章相关性注入事实】** `facts_builder` 新增 `relevant_first_rows`（按章节关键字**重排**事实行：通用组+命中进头部、其余垫后、**绝不丢事实**——与 `_filter_facts_rows` 的「筛选+回退」分工明确勿合并）+ `render_outline_chapter_facts`（逐章纯渲染，dd_block 计入预算）；`sse_handlers` 一级目录成功后循环外加载事实行一次 + basis 排序 + dd 块一次，闭包 `_sublevel_facts_text(unit_idx)`（单章按本章、批处理按并集相关性）供 `_build_unit_prompts` 两处注入点；配置 `outline_sublevel_facts_relevant` **默认 True**（False 或加载失败逐字回退整案截断）。
> ⑤ **【护栏 + 验证】** 新护栏 `tests/test_content_fact_autofix_r77_20261014.py` **21 例**（A 值域 4 / B 与 standard_report 真实集成 2 / C 行为 7——多处命中拒绝+回灌消息含「上次修复应用失败原因」、空 edits 一字不改、持续异常 fail-soft attempts 打满、部分成功不倒退 / D 静态接线与开关面 8——断言全部锚**调用形态**防假护栏）；修订 `test_content_standard_sse_wiring.py` 护栏：standard_report 调用点 1→**恰好 2**（第二次为条件性重算，必须位于修复块内、必须在写锁前——原「每章双扫」成本缺陷不回归）；facts 簇 **1009 passed/5 skipped/0 failed**；提示词/目录/一致性/content_runtime 定向簇全绿；**后端全量排他串行 7970 passed/4 failed（550.02s）——4 失败全部与本轮无关**（`test_export_closeout_20261006.py::TestAtomicReplaceCacheGovernance` 是 `inspect.getsource` 源码扫描护栏，export.py 正被并行会话重构：未提交 795+/311-，会话期间 22:59 仍在改，`export_cache` SQL 已不在原处；隔离复跑同样失败 ⇒ 非本次引入）；ruff 9 文件 All passed；9 触碰文件 BOM=False/FFFD=0/loneCR=0。详见 `docs/参考软件全局事实机制全流程报告与引入方案_R77_20261014.md`。
> ⚠️ **运维**：需重启后端（`content_fact_autofix` 新模块 + `facts_builder`/`sse_handlers`/`config`/提示词注册表常驻）；前端无需重建；**无需清库**。⚠️ **monkeypatch 表面**：测试替换修复行为 patch `app.services.content_fact_autofix.autofix_section_fact_conflicts`（或注入 `chat_fn`）；开关面 `settings.content_fact_autofix_enabled` / `settings.outline_sublevel_facts_relevant`。⚠️ **遗留**：并行会话 export.py 重构完成后需复跑 `test_export_closeout_20261006.py`（其护栏语义可能需随重构同步更新，由该会话自行收口）；P1/P2 项（AI 编排 opt-in、受影响章节一键重生成、fact_id 溯源、要求/事实类型字段）见报告 §9。

> **上一轮校准：2026-10-13（R76 正文生成模块深度审计 · 围栏闭合判据单一事实源收口）** —— **【用户点名模块深度任务：功能清单/结构探查/基线测试/深度审计/增强优化/回归验证全链走完；零新增依赖、零数据迁移、零 DDL、返回契约加法式；4 个既有 BUG + 实现期新发现 3 副本 + 本轮自引入 1 回归全部修掉】**：
> ① **【缺陷定稿（探针矩阵 `_probe_fence_matrix_out.txt` 实证）】** BUG-F1 闭合判据无单一事实源 ⇒ 预检 DLV-07 假阳性 + `auto_fix` 永远空转 + `review_autofix` 定位用 strict/修复用 lenient 结构上无法闭环；BUG-F2 `content_blocks.read_fenced_block:1099` 只判 `pf[0]==open_char` 未判信息串为空 ⇒ 带标签行当闭合、**导出把后续正文吞进代码块**（与前端 CommonMark 反向分叉），且被 `auto_fix`「文末追加」固化为自检绿灯的静默缺陷；BUG-F3 严格（字数/压缩禁区）与宽松（渲染）两套判据同时作用于同一份正文 ⇒ 实测语料 A `text_word_count=0`（落库字数 0、DLV-16 超字数判定失效）、`fence_spans=[]`（AI 压缩禁区为空、会改写 Mermaid 源码）而导出正常成图；F4 补齐日志只记「补齐 N 处」看不出正文被吞。
> ② **【定稿判据 + 唯一事实源】** 新增叶子模块 `services/fence_common.py`（仅依赖 `re`；`content_utils` 与 `content_blocks` 互不引用 ⇒ 无循环导入）：闭合 = **同种字符 + 信息串必须为空 + 长度容错**；长度错配在**写入前**由 `normalize_fence_closes` 补齐 ⇒ 严格口径（预检 DLV-07/字数剔除/压缩禁区）与容错口径（渲染/登记）对**落库正文结论恒等**。消费点全量改造：`read_fenced_block` 加信息串空判、`_find_unclosed_fences_lenient`/`find_unclosed_fences` 双双委托同源谓词（签名与白名单不动）、`auto_fix_unclosed_fences` 改**两步**（归一→文末追加，log 增 `action` 键 `normalize_close_length`/`append_close`）、DLV-07 文案拆「真未闭合」/「长度错配」两种（rule_id 不变）、删 `_chart_pipeline.py:197` 悬空死副本（全文件零引用实测）。新配置 `content_fence_normalize_close`（env `CONTENT_FENCE_NORMALIZE_CLOSE`，**默认 True**，置 False 逐字回退旧行为）。
> ③ **【实现期新发现：保护区间三份副本 F5-a/F5-b/F6】** 收敛过程中查出 `content_rewrite._split_fences` / `content_checkpoint` / `content_polish` 各自自持一套围栏区间提取（同族「同一判据多份副本」最高频反模式），改由 `fence_common.iter_all_blocks` / `fence_char_ranges` 单一供给；`content_shrink`/`repair_validator`/`content_standard` 等「剥离/解析用途」非闭合判据副本登记不扩张。
> ④ **【BUG-F7（本轮自引入回归 · 端到端实抓 · 最大教训）】** 前端 `utils/fencePreprocess.replaceChartFences` 段间用 `parts.join("\n")` 单换行 —— `<div>` 在 CommonMark 里**开启 HTML block，只有空行才结束它**，紧随围栏的正文被吸进 block 丢掉 `<p>`（D/E 两例实测 `proseInParagraph=false`）；HEAD 旧实现靠正则替换串自带 `\n…\n` 恰好留了空行。修：`stripBlankEdges` 剥首尾空行 + `join("\n\n")`（顺带保证不叠出 `\n\n\n` 空白 `<p>`）。⚠️ **纯单测抓不到**：既有语料每块前后本来就带空行 ⇒ 新旧实现**行为等价化**，14 例全绿而真实脏语料仍坏；只有走「写路径 → 落库回读 → esbuild 真转译并执行前端 TS → marked 渲染」的端到端才暴露。**判据：前后端同语义的拼接/边界函数，单测语料必须含「源里本来就没有空行」的形态。**
> ⑤ **【护栏 + A/B 5/5 CAUGHT + 端到端 31 判据】** 后端 `tests/test_content_fence_single_source_r76_20261013.py` 41 例（A 契约归一+棘轮白名单锁副本不增长；B 五类脏语料与 list_facts 逐字 parity；C 保护区间三消费点；D 静态接线与开关面）；前端 `fencePreprocessR76.test.ts` **14 例**（含 F7 回归锁「图表块紧随正文必须补出空行」+ 相邻两块不得叠 `\n\n\n`）。驱动 `artifacts/r76/_ab_driver.py` + `_ab_f7_driver.py`：M1 摘归一只留追加 / M2 read_fenced_block 旁路信息串判 / M3 前端回退单换行 / M4 删前端预处理接线 / M5 `segs.join("\n\n")` 退回 `parts.join("\n")` → **mutated_rc=1 CAUGHT、restore_ok true**。端到端 `artifacts/r76/_e2e_fence_pipeline.py`（真实 `sanitize_ai_content`→`auto_fix_unclosed_fences`→五读口径回读）+ `_e2e_fe_render.js`（esbuild 真转译执行）共 **31 项判据（后端 22 + 前端 9）全 PASS、FAILCNT=0**。
> **验证**：正文簇 **1091→1135 passed/0 failed（39.96s）**（+44 = 新护栏 41 + `test_fence_autofix` 3，零用例消失）；保护区间簇 **103 passed**；**后端全量排他串行 7838 passed/17 skipped/1 xfailed/0 failed（675.32s，EXITCODE 0）**（首轮 7832+6 failed 逐字同数，耗时腰斩实锤互扰）；前端 **65 文件/1173 passed（504.85s）**、`tsc` rc=0、eslint **0 errors**/1218 warnings；ruff（F821,E9,F841,F401）13 文件 All passed；17 触碰文件字节体检 BOM=False/FFFD=0/loneCR=0/ZWSP=0。详见 `docs/正文生成模块深度审计与围栏判据收口_R76_20261013.md` §8。
> ⚠️ **运维**：需重启后端（`fence_common`/`content_utils`/`content_blocks`/`content_rewrite`/`content_checkpoint`/`content_polish`/`preflight_engine`/`sse_handlers`/`config` 常驻）；前端需重建；**无需清库**（零迁移、历史存量不回填 —— 用户已确认）。⚠️ **monkeypatch 表面**：替换围栏判定/归一行为须 patch `app.services.fence_common`（patch 各消费模块的委托薄包装会被委托链吸收）；开关面 patch `fence_common.normalize_close_enabled`。
> ⚠️ **本轮工具教训（三条同族复现）**：① **全量与前端工具链必须排他串行** —— 首轮 6 failed 全部隔离复跑通过，根因是并行会话并发 npm/node 争用 `frontend/node_modules`（同一条 eslint 用例三次报不同错而 `Test-Path` 全 True）+ 源码扫描类护栏运行时 `rglob` 对在改文件敏感；批量前先 `kill_zombie_pytest.ps1 -DryRun` 清僵尸。② PowerShell `>` 重定向默认写 **UTF-16LE**，python 以 utf-8 读回即乱码/零命中 —— 取 `git show HEAD:path` 字节须走 python `subprocess` 落盘。③ **SearchReplace 报 `save failed, reason: unknown` 可能已实际写入**（本仓文档三连报，Grep/Read 复核确认已生效）—— 报错后必须复核再决定，盲目重试会重复插入。

> **上一轮校准：2026-10-13（R75b 后续需修改项收口 · G6–G9 定向修复与加固）** —— **【用户指令「后续需修改项按最优方案执行修复和加固处理」⇒ R75 §7 遗留项全部收口；零新增依赖、零数据迁移、零判定行为变更（除 G7 不可达死分支改 fail-closed，可达入口行为零变化）**：
> ① **【G6 · 读路径唯一出口权威定义下沉 services（§7.5 建议完成形态）】** `fact_name_value` 本体迁到 `services/facts_extractor.py`，routers `_fact_name_value` 改纯委托薄包装（6 处调用点与护栏锚名不动）；根因：旧定义在 routers 层，跨模块只读桥接 `load_resolved_facts_for_scope` 无法正向引用（routers→services 是唯一合法依赖方向），只能在底层原语上自建第二份回解（G-C 同族）。口径辨析入 docstring：本体=显示/审计口径（保注释）、extract=比较/输入解析口径（剥注释），两套既定职责不是分叉。护栏连锁订正两处锚点漂移（AGENTS 反复记账同族）：R75 护栏 A 组白名单摘掉 `_fact_name_value` + 反向锁改指 services 本体（实测下沉瞬间 1 failed）；R54 `test_bold_line_regex_used_by_one_parser_only` 锚改 services、反向锁 routers 代码行不引用正则名（排除注释行）。新护栏 r75b G6 组 3 例（routers 无副本 AST 锁/桥接必消费本体/包装与本体逐行同值行为锁）。
> ② **【G7 · batch_resolve 不可达死分支换方案：不删也不留地雷 → fail-closed（§7.4 递延项）】** 旧 `else: scope, params = "", fact_ids`（因 :2305 守卫不可达）改显式 `raise HTTPException(400)` —— 不变式做成可执行断言，未来放松前置守卫即 fail-closed 而非静默发无作用域跨域查询。护栏 G7 组 3 例：拼串形态不回流（⚠️ **只扫非注释行** —— 本轮实踩：源码处置注释逐字引用旧形态作反例，全文扫描自咬）+ AST 锁 else 必 raise + 可达入口 400 行为锁（既有安全测试文件从未覆盖的形态）。`ack_fact_stale` 等其余批量端点无同族拼串，核实免重复排查。
> ③ **【G8 · 历史误伤行只读盘点（§7.2 建议脚本落地，零回填）】** `artifacts/r75b_g8/inventory_g8.py`（sqlite URI `mode=ro` 零写语句，护栏锁住只读形态防被改成推断回填工具）。五口径实测：**总 129 行，口径 1（真带 `*(注释)*`）=0、口径 1b（分叉但无注释）=1、带模拟标记但列被清零=0、闸门不变式违规=0、段落行 G-C 影响面=0** —— 无真实历史误伤残留。⚠️ **10-14 复盘订正**：口径 1 首版用「两口径分叉」当「值带注释」的**代理判据，代理不等价** —— `_RE_BOLD_FACT_LINE` 取值组 `(.*)` 不跨换行，多行分组行被**首行截断**同样分叉却无注释可保护，原报告据此把 1 行误述成「G-A 注释保护面」，而库里真带注释的行实为 **0**；现拆口径 1 / 1b 并补护栏 + A/B **M4** 锁判别力（口径 1b 那 1 行是手工分组测试行，非注释抹除受害者，维持不回填）。✅ **10-14 R75c 收口**：口径 1b 暴露的「多行容器行显示口径只取首行、第 2..N 行静默丢」已修复 —— `fact_name_value` 对含换行 content 回退`(title, 完整原文)`，单行粗体行（128/129）逐字不变；盘点脚本 `_COMMENT_MARK_RE` 同步排除粗体对。新护栏 `TestMultiLineRowDisplaysFullContent`（4 例）+ A/B **M5**（退回首行截断 → 3 例定向失败）。
> ④ **【G9 · `_eslint_r75.json` 存根删除（§7.3 工具故障收口）】** DeleteFile 两次 `recordFileDelete returned false` 再现，Test-Path 复核确认文件仍在 ⇒ shell `Remove-Item -LiteralPath` 精确删 + Test-Path=False 实锤（凡删除必复核惯例再验证）。
> ⑤ **【护栏 + A/B 5/5 CAUGHT】** 新护栏 `tests/test_global_facts_closeout_r75b_20261013.py` **12 例/4 组**（G6 契约 3 + G7 fail-closed 3 + G8 只读锁 1 + G8 口径 1 判据锁 1 + R75c 多行容器行 4）；R75 护栏 9→10 例。驱动 `artifacts/_ab_r75b_driver.py`：M1 桥接退回直调 extract / M2 死分支回流无作用域拼串 / M3 包装回流自持切分 / M4 口径 1 退回分叉代理 / M5 多行容器行退回首行截断，逐次还原 restore_ok 全 True、基线绿、汇总 ascii-safe。⚠️ **另两个实踩**：`facts_extractor.py` 是全 LF 仓内孤本，`\r\n` 跨行锚 count=0 直接 MISSED（单行锚不碰行尾惯例再验证）；⚠️ **10-14 复盘实踩③**：**「名字在场」是比「源码含字符串」更隐蔽的假护栏** —— 新护栏首版断言 `"_COMMENT_MARK_RE" in src` / `"cal1b" in src`（锚点指向正确的符号名，人读完全像有效判据），A/B M4 把三元路由改回 `cal1.append(r)` 后**仍 18 例全绿**（定义行与报告文字里的名字都还在）。修法：AST 锁**真实调用**（`_COMMENT_MARK_RE.search` 必在 `main` 调用图内）+ AST 锁**路由**（`cal1b` 必真被 `append`，含 `IfExp` 展开），M4 转 CAUGHT。**新增护栏后必须立刻做一次「把接线改回旧形态」的反向验证，否则不知道自己的护栏是真还是假。**A/B 驱动脚本在 artifacts 下，路径推导要多算一层 dirname（本轮首跑 NotADirectoryError）。
> **验证**：R75+R75b+batch_resolve 安全+R45 四文件 **44 passed**（10-14 复验 **74 passed**，并行会话向 r54/batch_resolve 补 30 例）；事实簇 `-k "fact"` **983 passed/5 skipped/0 failed（47.93s）**（R54 订正前该簇 1 failed 即锚点漂移实锤）；后端全量 **7765 passed/17 skipped/1 xfailed/0 failed（508.97s）**；10-14 复验 **7969 passed / 17 skipped / 1 xfailed / 0 failed（533.47s，rc=0）**零回归（=R75 基线 7711+本轮护栏+并行会话新增）；ruff（F821,E9,F841,F401）5 触碰文件 All passed；7 触碰文件字节体检 BOM=False/FFFD=0/loneCR=0。前端本轮零代码改动。详见 `docs/全局事实读路径口径收口_R75_20261013.md` §8。
> ⚠️ **运维**：需重启后端（`global_facts`/`facts_extractor` 常驻）；无需清库；前端无需重建。⚠️ **monkeypatch 表面迁移**：测试替换读路径回解行为须改 patch `services.fact_name_value`（patch 薄包装会被委托链吸收）；生产无影响。

> **上一轮校准：2026-10-13（全局事实模块 · 读路径 name/value 口径收口 R75）** —— **【用户指定模块深度任务：功能清单/架构审计/缺口清单全量完成后，真实剩余缺口收敛四项 G-A/G-B/G-C/G-D；零新增依赖、零数据迁移、零新增配置项】**：
> ① **【G-A/B/C · 六处 name/value 回解统一走 `_fact_name_value`（global_facts.py :1112/:1482/:1776-82/:1843/:2194/:2506）】** 旧实现直调底层原语 `extract_value_from_markdown_line` 构成读路径第二份切分：adjust 提示词喂 AI 剥注释值（照抄回传即抹注释，R54 §8.2 T4 收口）、裁决/更新旧值被剥注释后与新值比较误判「已换值」（is_simulated 误清零）、危大判定把非粗体段落行整段当名称。写路径保留底层原语（拆用户提交行职责不同，白名单显式登记）。行构造/回解均注意 sqlite Row→dict 转换（:1482/:2194）。
> ② **【G-D · 前端事实诊断 Promise.all → allSettled（SchemeWorkbenchPage.tsx）】** 章节覆盖度与危大阈值两个独立诊断，旧实现任一失败连带丢弃另一侧成果（danger-check 对空方案 400 实证形态）；新增导出纯函数 `mergeFactsDiagnosticsSettled`（成功侧各自落板、双侧 Abort 静默、单侧 Abort 不静默真实失败、null≡展示/undefined≡缺席）+ 竞态守卫保留。
> ③ **【既有护栏锚点订正（护栏失锚形态实锤）】** R45 `test_group_rebuild_still_reads_old_value_from_stored_content` 锁死旧字面量，单一出口迁移即恒红 → 改锁「经 `_fact_name_value` 回解或旧字面量」+补原意图锁（old_val 不得取新值）。
> ④ **【护栏 + A/B 4/4 CAUGHT】** 后端 `tests/test_global_facts_closeout_r75_20261013.py` 9 例/3 组（A 六落点 AST 契约锁+全模块直调白名单+反向锁出口被掏空即红；B 五类脏语料与 list_facts 组合公式逐字 parity；C 新旧出口差异真实存在的前提锁）；前端 `factsDiagnosticsR75.test.ts` 13 例/2 组（纯函数值域 + glob 静态接线锁）。驱动 `artifacts/_ab_r75_driver.py`：M1 adjust 回退旧口径 / M2 裁决旁路 / M3 前端回退 Promise.all / M4 摘成功侧落板，逐次还原 restore_ok=True、汇总 ascii-safe（R73 教训沿用）。
> **验证**：事实簇 **803 passed/1 skipped**（与本轮基线逐字一致零回归）；后端全量单进程 **7711 passed/17 skipped/1 xfailed/0 failed（527.79s）**=R74 复验基线 7702+新护栏 9；前端 Vitest **64 文件/1157 passed**（基线 63/1144+新护栏）；tsc rc=0；eslint **0 errors**；ruff（F821,E9,F841,F401）3 后端文件 All passed；5 触碰文件字节体检无 BOM/FFFD/loneCR。详见 `docs/全局事实读路径口径收口_R75_20261013.md`。
> ⚠️ **运维**：需重启后端（`global_facts` 常驻）；前端需重建；无需清库。⚠️ **遗留**：仓库根 `_eslint_r75.json` 存根待手工删（DeleteFile 两次明确失败 `recordFileDelete returned false`，Test-Path 复核仍在，§10.5 同族再现；**R75b 已收口**）；历史已被误伤行不做自动回填（无法区分人工改值与误清；**R75b G8 已出只读盘点报告：无真实误伤残留**）。

> **上一轮校准：2026-10-12（R74 后续修改项收口 · G1–G5 定向修复与加固）** —— **【用户指令「后续需修改项按最优方案执行修复和加固处理」⇒ R74 §8 遗留项 1/2/5/6/7 全部收口；零新增依赖、零数据迁移、除 G3 登记过的缺陷修复本体外零判定行为变更】**：
> ① **【G1 · 审核依据前端呈现（§8.1）】** `SchemeWorkbenchPage.tsx` 新增纯函数 `summarizeOutlineReviewBasis`（消费后端一直在发但前端静默丢弃的 `review.review_mode`/`suggestions`）+ 三处接线（在线 completed / 刷新接管 / 断线重挂），只进生成日志不弹 toast；未登记模式 token 照原文透出。⚠️ 模板字符串里的全角空格 U+3000 会触 eslint `no-irregular-whitespace`（`skipTemplates` 默认 false）⇒ 前缀走普通字符串拼接。
> ② **【G2 · 补齐提示词逐条覆盖硬要求（§8.2）】** `outline_patch_system` 要求 1–5 重写为 1–7：「逐条对应、一项不漏」+「条数不得少于缺失项」+「严禁充数（程序会逐条复检、未盖住整轮作废）」+「输出前自检」；占位符零变更（r38/r39 契约不受影响）。选 §8.2 建议的提示词侧强化、不放宽复检。
> ③ **【G3 · `_plan_guidance` 静默丢弃修复本体（§8.5，R71 前既有登记）】** `sse_handlers.py` 三处：初始化提到 try 外（旧实现中途异常会在 else 分支 NameError）+ `_chapter_extras` if/elif 重构 + **stable-prefix 关闭分支把引导语拼进 `user_content` 末尾**（在 messages 组装与注入/预算治理之前，与开启链路同受治理链）。仅双重 opt-in 组合生效，默认配置产物逐字不变。
> ④ **【G4 · CFB「跨用例污染」诊断订正 + 中心化治理（§8.6）】** `validate_ole_compound_header` 是纯函数，进程内污染不成立；真实同族缺陷是全仓**约 20 个测试文件裸改 `app.db.DB_PATH` 不还原** ⇒ 选 **conftest 函数级 autouse 快照-复位**（`_restore_app_db_path`）中心化治理，**刻意不逐改 20 文件**（改动面风险大；`get_conn` 已有失配重连 db.py:272，复位变量即换回正确连接）；`gf_ctx` 另改 `monkeypatch` 双保险。本轮另实锤：多次「批量挂起」是并行会话跨进程互扰 + PowerShell 管道进程滞留（同批 `-v`+工作区内 Tee 复跑 85 passed/4.80s、CFB 绿）。
> ⑤ **【G5 · eslint 代修（§8.6）】** `importParseWarningWiringR72.test.ts`（并行会话新文件）四连空格正则改 ` {4}` 量词，r55 `test_eslint_zero_errors` 护栏复绿。
> ⑥ **【护栏 + A/B 4/4 CAUGHT】** 后端 `tests/test_r74_followup_guards_20261012.py` 12 例/3 组（G3 接线锁/G2 判据+占位符迁移锁/G4 静态+**c3a→c3b 行为实证**）；前端 `outlineReviewBasisR74.test.ts` 12 例/2 组（纯函数值域 + glob 静态接线锁）。⚠️ **M4 首轮 MISSED 的最大踩坑**：行为实证基线若在模块收集期快照，autouse 失效时先前脏残值被当成基线 ⇒ c3b 永真式漏检；改由 c3a 改脏**之前**取样、A/B 的 `-k` 必须跑整类（单跑无基线护栏显式 assert 拒绝）。驱动 `artifacts/r74_followup/_ab_driver.py` 字节补丁（CRLF 仓跨行锚必写 `\r\n`、单行锚不碰行尾）、跑完即还原 sha 全同。
> **验证**：目录簇 48 项 **1115 passed/43.77s**（与 R74 §6 基线逐字一致零回归）；污染组合批量 **85 passed/4.80s**（CFB 绿）；**后端全量单进程复跑 7697 passed / 17 skipped / 1 xfailed / 0 failed（498.51s，R74 主轮那两处并行会话失败均转绿）**；新护栏 12 passed 无弃用告警；`ruff`（F821,E9,F841,F401）5 文件 All passed；`tsc --noEmit` rc=0；eslint 全量 **0 errors**；Vitest 全量 **63 文件/1144 passed**；8 触碰文件字节体检 BOM=False/FFFD=0/loneCR=0。详见 `docs/目录占位标题闸门与外科式重写收口_R74_20261012.md` §10。
> ⚠️ **运维**：需重启后端（`sse_handlers` 常驻）；无需清库；前端需重建。⚠️ **递延**：§8.4 长方案语料结构性不足（真实多方案端到端复测需用户生成新方案后补）；§8.3 纯语义空话标题漏检属检出率上限；全仓裸改 DB_PATH 的其余 ~19 文件按 autouse 兜底不再逐个改。
> **【次日复验 2026-10-13，零代码改动】**：僵尸清场（`kill_zombie_pytest.ps1` DryRun 审计→KILL 5/5，均为隔夜挂起 pytest；⚠️ 惯例化：**每轮批量测试前先 DryRun 一遍**，防「批量挂起」误诊重演）；`_eslint_out.json` 存根删除（§8.7 收口；⚠️ DeleteFile 同文件两次假成功再现，凡删除必 Test-Path 复核）；隔夜并行会话改动（工作树 167 项 M/??）后全量复验 **7702 passed / 17 skipped / 1 xfailed / 0 failed（517s）** + 前端 **63/1144** 与基线逐字一致、tsc rc=0、eslint 0 errors。详见 R74 文档 §10.5。

> **上一轮校准：2026-10-12（解析提取模块 R73 续作二：G5 上传配额第三份副本收敛 + G4 红线硬化）** —— **【承接 R73 续作 §12.7 未落地项：item ③（settings 第三处口径）落地 + item ②（内容语义级损坏）红线硬化；零新增依赖、零数据迁移、返回契约加法式】**：
> ① **【G5 · /system/upload-limits 第三份字面量副本收敛（item ③ 落地）】** `routers/system.py` 的 `/upload-limits` 端点此前自带 `_UPLOAD_LIMIT_DEFAULTS`（30MB/20/200MB 第三份字面量副本）+ 独立读取器 `_positive_setting`，与 `global_facts.MAX_UPLOAD_*`（上传路径真正消费的运行时权威）**各自读 settings** ⇒ 一旦 `monkeypatch.setattr(gf, "MAX_UPLOAD_BYTES", N)`，上传路径按 N 执行、端点仍下发旧值，**前端拿到的上限与后端实际执行的上限不一致**（R73/G2 同族「同一判据两份取值」第三处残留）。现端点直接读 `_gf.MAX_UPLOAD_*`（与上传路径同一对象），删 `_UPLOAD_LIMIT_DEFAULTS` + `_positive_setting`；**默认值逐字不变**（30MB/20/200MB），前端契约键名一字不动。回落逻辑（非正数/非整数配置）由 `global_facts._positive_int_setting` 在模块加载时统一兜底（端点不再自带副本）。
> ② **【G4 红线硬化（item ② 刻意不做 → 做成可执行断言）】** 「内容语义级损坏（头合法但 Workbook 流损坏）」刻意不做 —— 做语义解析就有误拒正常文件的风险，违背 G4 §12.3 「只判 CFB 必要条件」红线。现把这条红线硬化为 `TestOlePreflightHeaderOnly` 两条护栏：① 字符串扫描 `validate_ole_compound_header` 不得引用 `olefile`/`ZipFile`/`open(`/`io.BytesIO`/`.read(`；② **AST 调用扫描**：函数体所有 `ast.Call` 只允许 `struct.unpack_from` + 纯内置（`len`/`bytes`/`isinstance`/`range`）—— 字符串扫描抓不住「改名 import / mmap / memoryview 切片做语义解析」的引入，AST 扫描补上（A/B 用 `_io_muted.BytesIO(content)` 验证：字符串扫描漏、AST 扫描定向失败）。
> ③ **【护栏 + A/B 3/3】** 新增/更新护栏：`tests/test_import_parse_contract_r73_20261012.py` **+8 例**（G4 红线 2 + G5 回落兜底 6：零/负/非数/None/正数透传/缺失字段）；`tests/test_import_parse_closeout_20261005.py` 改造 parity 测试（四源→三源 + `test_upload_limits_endpoint_reads_runtime_authority` AST+行为锁）；`tests/test_system_activity.py` 改造 3 例（`reflect_settings`→`reflect_runtime_authority` monkeypatch `gf.MAX_UPLOAD_*`；`invalid_falls_back` 参数化 4 例 → `does_not_read_settings_directly` 1 例，证明端点不再独立读 settings）。**A/B 3/3 全部定向失败**（G5 端点退回 `settings.upload_*` 直读 → AST+行为锁定向失败 / G4-② 塞 `io.BytesIO` 内容流入口 → AST 调用扫描定向失败（字符串扫描漏，AST 扫描补上）/ 还原 `restore_ok: True`）。
> **验证**：`pytest tests/test_import_parse_contract_r73_20261012.py tests/test_import_parse_closeout_r72_20261012.py tests/test_import_parse_closeout_20261005.py tests/test_import_parse_closeout_20261006.py -q` **90 passed / 0 failed（3.90s）**；G5 回归簇 27 文件 **527 passed / 2 skipped / 0 failed（50.54s）**（skip 均环境缺件：LibreOffice/tesseract）；`ruff check --select F821,E9,F841,F401` 5 文件 All passed；9 个触碰文件字节体检 **0 异常**（BOM=False / U+FFFD=0 / loneCR=0 / CRLF 保持 / AST OK / 顶层无重名）。
> ⚠️ **运维**：需重启后端（`system` / `global_facts` 常驻模块）；无需清库。**行为变化（重要）**：① `/system/upload-limits` 的 monkeypatch 表面从 `settings.upload_*` 迁到 `gf.MAX_UPLOAD_*` —— 测试/运行时覆盖配额须改 patch 目标（生产无影响：`.env` 配的 `UPLOAD_MAX_*` 仍在模块加载时经 `gf._positive_int_setting` 生效）；② `settings.upload_max_bytes=0` 等非法配置的回落仍生效（在 `gf` 侧，不在端点侧）。
> ⚠️ **未落地（记录以免重复排查）**：① item ①（非 CFB / ZIP 容器完整性最小校验）**刻意递延 → 已硬化**：调查确认 ZIP 容器走 `file_parser._parse_docx` 的 Python `zipfile` 快路径、**不经 LibreOffice**，损坏 ZIP 毫秒级抛错经 R73 G1 收敛为 ParseError，「白等 120s」症状结构上不存在；`TestZipContainerNoPreflightNeeded`（3 例）锁住 `LEGACY_WORD_EXTENSIONS` 不得含 ZIP 扩展名 + 损坏 ZIP → ParseError 行为级验证。② item ④（真实 Word 97 `.doc` 语料）**刻意递延 → 已硬化**：`validate_ole_compound_header` 只看容器头部、**不读流名**（格式无关），`test_v3_excel_shape_accepted` 用真实 `.xls` 字段布局已结构性覆盖 v3 `.doc` 头形态；`TestHeaderCheckIsFormatAgnostic`（2 例）锁住「不读流名」性质。两条递延决策的架构依据均已做成可执行护栏（A/B 1/1 CAUGHT，见 §13.7），防未来会话「修复」不存在的问题。③ `config.py` 的 pydantic Settings 字段默认值（30MB/20/200MB）与 `global_facts._DEFAULT_UPLOAD_*` 回落值仍是**两份字面量** —— 但前者是 pydantic 声明必需、后者是 `_positive_int_setting` 的兜底入参，结构上不可合并（合并需让 pydantic field 无默认，破坏 `Settings` 契约）；由 parity 测试 `test_upload_total_limit_default_is_consistent_across_sources` 锁住两者同值。
> **详见** `docs/解析提取模块收口_R73_20261012.md` §13（G5 收敛 file:symbol / G4 红线硬化两条护栏互补性 / A/B 3 表 / item ①②④ 递延理由）。


> **最近校准：2026-10-12（解析提取模块 R73 续作：R73 §8 遗留项 G2/G3/G4 收口）** —— **【上传配额字面量第二份副本删除 + 测试 docstring 路径漂移护栏化 + 旧版 Word OLE 头结构预检（早退免 120s 白等）；新增配置项 1 个默认 True、零数据迁移、返回契约加法式】**：
> ① **【G2 · 同一判据两份取值（本仓最高频反模式）】** `routers/global_facts_docs.py` 顶部三个静态占位 `MAX_UPLOAD_BYTES / MAX_UPLOAD_FILES_PER_REQUEST / MAX_UPLOAD_TOTAL_BYTES`（30MB / 20 / 200MB）是权威值的**第二份字面量副本**，且**从未被任何代码或测试读取**（上传路径一直动态读 `gf.MAX_UPLOAD_*`）；配套的 `_gf_const()` 延迟读取 helper 也是死代码。删掉三占位 + 一 helper（27 行），并把「不得再回流」做成 AST 护栏：扫 `global_facts_docs` 顶层 `Assign` 目标名与三个配额名求交、断言 `_gf_const` 不在函数集合、并**反向锁**权威值仍在 `global_facts`（30MB / 20 / 200MB 数值不变）+ `MAX_PARSED_CHARS` 与 `gf` 同值（R70 收敛的落库上限单一事实源，删占位不得误伤）+ 上传路径 `gf.<名>` 逐个在位（保住 monkeypatch 覆盖配额的能力）。
> ② **【G3 · 文档路径漂移做成可执行断言】** R72 测试 docstring 引用的前端测试文件名 `outlineLibraryImport.test.ts` **在本仓不存在**（真实文件是 `OutlineLibraryEditModal.test.tsx`）——「文档指错路径」这类缺陷纯人工审查才抓得住，且护栏一旦失去锚点会**恒绿**。现改正文本名，并新增 `TestDocstringTestPathDrift`：正则抽出 docstring 里所有 `` `*.test.ts(x)` `` 引用、逐个 `Path.is_file()` 断言存在，且**断言引用列表非空**（防「删掉引用 → 护栏变成空转」的静默失效）。
> ③ **【G4 · 损坏 .doc 白等 120s 的廉价早退】** 旧版 Word 转换前无结构预检，损坏/被截断的 `.doc` 要走完「探测后端 → 120s 超时 → 最多两后端」≈ 4 分钟才报「转换失败」，并占住一个线程池 worker。新增纯函数 `validate_ole_compound_header()`（`services/legacy_office.py`，唯一出口 `ole_preflight_reason()`）：**只判定「合法 CFB 的必要条件」**——头字段值域（小版本 0x3E / 大版本 ∈{3,4} / 字节序 0xFFFE / 扇区位移 ∈[7,12] / 迷你扇区位移=6）+ **扇区算术**（头部声明的 FAT+目录+迷你FAT+DIFAT 总数 ≤ 文件实际扇区数 ⇒ 截断判据）+ 目录扇区索引非保留值/不越界；**绝不做内容语义解析**，因此结构上不可能误拒正常的老版 Word。接线在 `detect_office_backends` **之前**（连组件探测开销都省）；开关 `legacy_ole_preflight`（**默认 True**，置 False 逐字回退旧行为）。失败文案明确指向「用 Word/WPS 重新打开另存为 .docx 后重传」。
> ④ **【G4 的两个真实陷阱（本轮回退两次）】** ⚠️ 其一：**扇区计数偏移读错** —— CFB 头部 0x34~0x3C 是「事务签名 / 迷你扇区上限 / 首个迷你 FAT 扇区」而**不是扇区计数**，首版从这里连读 4 个计数，把一份 **1.23MB 的真实 Excel 97 `.xls`（v3 · 512B 扇区 · 19 个 FAT 扇区 · 2408 扇区）误判成「已截断」**。修：按 MS-CFB 2.2 严格取 0x28/0x2C/0x40/0x48。⚠️ 其二：**v3 与 v4 语义不同** —— CFB v3 允许 FAT 内联在 DIFAT 数组（`num_fat=0`）且规范要求目录扇区数写 0，真实的 Excel 97 `.xls` 就是这种形态；据此「v3 也要求 FAT/目录扇区 ≥1」会误拒正常文件。修：`if major == 4 and n_fat < 1` / `if major == 4 and n_dir < 1` **只对 v4 强制**，并用那份真实 `.xls` 的字段布局写成护栏 `test_v3_excel_shape_accepted` 钉住。⚠️ 预检自身任何异常一律 **fail-open**（`ole_preflight_reason` 包 try/except 记 WARNING 后返回 None）—— 预检只是加速用辅助判定，绝不能因为预检自己出错而阻断原本可解析的老版 Word（并有 `test_preflight_reason_fail_open_on_own_exception` + `test_switch_off_skips_validation_entirely` 双锁）。
> ⑤ **【护栏 + A/B 5/5】** `tests/test_import_parse_contract_r73_20261012.py` 10 → **36 例**（A 契约归一 3 + B 透传 2 + C 防御纵深 3 + D 静态接线 1 + **E 新增 26**：G2 4 + G3 1 + G4 头字段/扇区算术 14 + 接线与开关 5）。**A/B 5/5 全部定向失败**（M1 摘预检接线 → 收集期 ERROR / **M1′ 行为级：`if _ole_reason:` → `if False:`（预检仍在场但被架空，G4 的真实回归形态）→ `test_convert_short_circuits_before_backend_probe` 定向失败** / M2 v3 分档退回误拒 → `test_v3_excel_shape_accepted` 定向失败 / M3 配额副本回流 → `test_docs_module_has_no_local_quota_constant` 定向失败 / M4 docstring 路径漂移回流 → `TestDocstringTestPathDrift` 定向失败），`try/finally` 逐次还原 + `restore_ok=True` + 还原后复跑全绿。⚠️ 教训（同 §4.24.5）：M1 的**语法级**变异（摘整块 → IndentationError）证明不了护栏对「预检被架空」的判别力，必须补 **M1′ 行为级**变异才成立。
> **验证**：`pytest tests/test_import_parse_contract_r73_20261012.py tests/test_import_parse_closeout_r72_20261012.py -q` **44 passed / 0 failed（2.52s）**；解析提取模块簇 **22 文件 476 passed / 2 skipped / 0 failed（41.29s）**（skip 均为环境缺件：LibreOffice `soffice` 未装、tesseract 未装）；`ruff check --select F821,E9,F841,F401` 6 文件 **All checks passed**；6 个触碰文件字节体检 **BOM=False / U+FFFD=0 / CRLF==LF / loneCR=0 / AST OK / 顶层无重名**。
> ⚠️ **【本轮工具教训（同 §5.16 同族，第二次踩中）】** A/B 汇总脚本 `print()` 到 GBK 控制台遇到 pytest 输出里的 `U+FFFD` 直接 `UnicodeEncodeError` 崩在**汇总阶段** —— 四条变异的判定结果已 append、`finally` 也已逐次还原文件，但**汇总行一行都没打出来**，看起来像「跑了个寂寞」。修法：打印前 `tail.encode("ascii","replace").decode()`。**判据：A/B 脚本的输出通道与还原通道必须解耦 —— 汇总崩不得影响还原，还原断言必须独立可判（本轮 `restore_ok: True` 是二次脚本单独确认的，不能依赖汇总打印）。**
> ⚠️ **运维**：需重启后端（`legacy_office` / `config` / `global_facts_docs` 均为常驻模块）；**无需清库**（零数据迁移、落库口径未动）。行为变化：① 损坏/截断的 `.doc` 由「最长等 4 分钟后报转换失败」变为**毫秒级**报「旧版 Word 文件已损坏（…），请用 Word/WPS 重新打开并另存为标准 .docx 后重新上传」；② 新增环境变量 `LEGACY_OLE_PREFLIGHT`（兼容 `legacy_ole_preflight`），默认开启。
> ⚠️ **未落地（记录以免重复排查）**：① G4 只覆盖 CFB 容器（`.doc` / `.xls` 老格式），**WPS 新格式 / ZIP 容器明确不在职责内**（返回 None 交后端判定，保持旧行为）—— 要覆盖需另立「ZIP 容器完整性最小校验」；② **内容语义级**损坏（头合法但 Workbook 流损坏）仍靠 LibreOffice 慢路径判定，本轮刻意不做（做语义解析就有误拒风险，违背「只判必要条件」红线）；③ 累计配额 200MB 与单文件 30MB 的实际取值来自 `settings.upload_*`（`global_facts.py` 权威解析），本轮只删了 `global_facts_docs` 的死副本，**未统一 `settings` 侧的第三处口径**（`routers/system.py` 的 `/system/upload-limits` 另有一份下发视图，属独立一轮）。
> **详见** `docs/解析提取模块收口_R73_20261012.md` §12（G2/G3/G4 逐项落点与 file:symbol / 两个真实陷阱全记录 / 5 条 A/B 表 / 验证基线 / 未落地）。


> **最近校准：2026-10-12（目录生成模块：占位标题闸门 + 外科式重写两轮收口 · 代码侧 R73/R73b，文档位 R74）** —— **【用户点名缺陷「TOC 三级标题大量占位泛化名（专项6 / 总体实施策划（2）（3））+ 编号冲突后缀」的根因链收口；零新增依赖、零数据迁移、历史脏标题不自动强改】**：
> ① **【根因 A/B/C（第一轮）】** `detect_generic_titles` 于 2026-10-10 接在 `_review_and_fix_outline` 的 **AI 审核之后**，而两条程序化早退（`programmatic` / `programmatic+surgical`）在其**之前** return ⇒ 填了编制要求/有方案解析（长方案常态）时占位标题**从未被检出、从未被修**；修复采用后一律 `passed=True` 且无复检（谎报）。落：两出口前加**占位闸门** + 新增**外科式标题重写**（`_try_outline_title_fix`：只送占位节点+上下文≤40 条、1 次小调用、按点分 path 回填，不受 `OUTLINE_FIX_MIN_COVERAGE` 退化护栏牵连）+ 采用后复检残留则 `passed=False` + `generic_residual` + 透明「请人工核对」。配置 **`outline_title_fix`（默认 True）**，与既有 `outline_check_generic_titles`（默认 True）**分层**：后者管「检不检」、前者管「检出后走不走小调用」，各自置 False 即逐字回退。
> ② **【F4（内容损坏 · 双重编号）】** `_apply_title_updates` 把模型回传标题**原样**写入 —— 实测 `'2.1 灌注桩支护施工'`、`'第一章 灌注桩施工'`、`'灌注桩\n支护施工'` 全部照收，而展示层按位置再套一次编号。本仓**其余每一条标题写入路径**（sections PATCH / save-outline / 上传落库 / 审核修复经 `normalize_outline`）都剥离内嵌编号，唯独新出口漏写。修：先清洗再判定 —— 含 `\n`/`\r` 整条拒收，编号剥离**复用唯一事实源** `numbering.strip_outline_numbering`（函数级 try-import 退化，绝不自造正则），剥后为空/仍泛化则拒收（红线四→五条）。
> ③ **【F5（日志不准确 + 前后端不同步，本轮最大教训）**】**同一条判据的多副本标签表**又踩中：`check_outline_continuity` 的 issues 实为 **7 类**，后端 `render_continuity_notice` 只有 6 类、前端 `summarizeOutlineQuality` 只有 4 类 ⇒ 占位标题计数双双被静默过滤，实测 `render_continuity_notice` 返回 `''`（WARNING 尾部空白）而界面零提示而后端已判 `ok=false`。落：`CONTINUITY_ISSUE_LABELS` 唯一事实源 + `render_continuity_notice` 改由 **`issue_counts`**（与前端同一份数据）驱动 + 未登记类别兜底 + 无明细时诚实现式；前端补齐三类并加未登记键兜底。⚠️ **刻意不给 `similar_titles` 这类「组织建议附录键」（不进 issues、不改 ok）贴缺陷标签** —— 混进缺陷摘要会误导用户（新护栏一条专门锁住）。跨语言 parity 放 pytest 侧正则抓前端 `labels` 表比键集合（前端未装 @types/node，同 R68 惯例）。
> ④ **【F6/F7（程序化出口不诚实）】** 改名与补齐都会动标题集，而出口的「过」字是在动作**之前**判的：一 占位标题曾以规则 3（≥4 字公共子串、全层级）替某条编制要求顶住落点，换成具体标题后该要求静默失去落点，旧实现照样 `passed=True` 早退（实测缺口 `['总体实施策划及相关安排']`）；二 外科补齐只看「有没有产出新章」，从不验证产出是否覆盖 missing，模型回任意一章即可骗到「✅ 已外科式补齐 N 项」。落：新增纯确定性 `_coverage_recheck(...)`（**复用** `_check_requirements_coverage` 同一把尺子、零 AI 调用、不改树、异常 fail-soft `(True,[])`）接进两处改名/补齐出口，不过则带已有成果落回完整 AI 审核（不丢已补的章、不写 `review_mode`）。
> ⑤ **【核实为无缺口（免重复排查）】** 编号无需重算（编号由**位置**派生，只改 title 不动结构）；`_try_outline_patch`/`fixed_outline` 各出口已 normalize，调用方不必重复；`numbering.py` 口径与 fail-soft 完备；`_merge_unit_results` 停止丢成果已修；`checkpoint_schema` 白名单含 `review` 故 `generic_residual` 可断线重挂。⚠️ **既有测试订正 1 处**：`test_outline_call_optimization.py::test_missing_triggers_surgical_patch` 补 `outline_checkpoint_check=False` 隔离九章维度（该用例测「补齐只发 1 次小调用」的调用纪律，F7 后 base 只 2 章必诚实地多走一轮审核）。
> ⑥ **【护栏 + A/B 4/4 & 6/6】** `tests/test_outline_placeholder_title_gate_r73_20261012.py` **14 例**（第一轮，A/B 4/4）+ `tests/test_outline_title_fix_chain_r73b_20261012.py` **16 例/4 组**（A 标题强制清洗·B 标签单源与跨语言 parity·C 出口诚实·D 不变式）。A/B 驱动 `artifacts/r73b_exit_honesty/_ab_driver.py` **6/6 CAUGHT**（M1 不剥编号 / M2 不拒换行 / M3 后端漏 generic / M4 前端漏 generic / M5 改名不复检 / M6 补齐不复检），锚串计数==1、跑完立即还原、三文件 sha 字节一致（`sse_handlers fd3e930a4330`、`outline_quality f432ecaa4500`、`SchemeWorkbenchPage.tsx 6fffd1ad5694`）。
> **验证**：目录簇（46 文件）基线 **1085 passed / 44.26s** → 簇+两轮护栏 **1115 passed / 0 failed / 43.58s**（零回归）；`ruff`（F821,E9,F841,F401）4 后端文件 All passed；触碰文件无 BOM/FFFD、CR==LF；前端 Vitest `outlineQuality` 7→**9 passed**、目录相关 5 文件 **119 passed**、`tsc --noEmit` rc=0；后端全量 **7677 passed / 2 failed（488.51s）** —— 两处失败均属并行会话的【解析提取模块】且本轮未碰（CFB 预检用例**单独跑 passed**、属顺序依赖；eslint 唯 1 error 在 `importParseWarningWiringR72.test.ts:63 no-regex-spaces`）。详见 `docs/目录占位标题闸门与外科式重写收口_R74_20261012.md`（槽位 R73 已被解析模块占用，文档顺延 R74；代码标识仍用 R73/R73b）。
> ⚠️ **运维**：需重启后端（`sse_handlers`/`outline_quality` 常驻）；无需清库（落库口径未变，历史脏标题需用户重生或手改）。**遗留**：`review_mode`/`suggestions` 前端无消费方（程序化程度用户看不到）；纯语义空话标题（「有关要求」）仍会漏检；本机仅一个真实方案语料，F4/F6 红态取自最小夹具。

> **最近校准：2026-10-12（字数收尾透明警告 + 前端并列口径收口 · R71）** —— **【承接 R70，用户明确决定不切 `text_word_count` 权威口径（那会推翻 R70 反向护栏 + 触发落库迁移），只做加法式：F2 五处前端并列口径 + R12 advisory 收尾警告；零判定行为变更、零迁移、零新增依赖】**：
> ① **【R12 · `word_closeout_notice`（content_utils）】** 纯函数复用唯一判定 `word_status_for`（同 0.8×/1.3× 阈值，不自造尺子），under/over 产「请人工核对」透明文案（over 明说「宁可超字数，不可损坏正文」未硬裁）；接线 `sse_handlers` 落库报告段 `report["word"]`（仿 R65 structure advisory 模式：只 info-log、重建 report_json、不改 error/warning 计数、不阻断），函数体 AST 锁死绝不引用 `readable_word_count`/`has_cjk`。
> ② **【F2 · 前端不再用 `.length` 伪造第三种口径】** `charts.py` fix_mermaid 回带权威 `word_count`（源出 `text_word_count(patched)`，回滚→None 绝不谎报）→ MarkdownRenderer→SectionContentCard→SchemeWorkbenchPage `sectionChartFixed` 三层透传，有值才 patch；保存响应 2 处 `data.word_count || content.length` → `?? 0`（`||` 会把合法 0 吞进伪造值）；一致性预览去掉 `hit.after.length` 本地写死（交 `load()` 回拉真值）；BidAnalysisTab/ParseResultCategoryPanel 「字」改标「字符」（该处显示的确就是原始字符数，改标比改算诚实）。
> ③ **【护栏 + A/B】** `tests/test_word_caliber_transparency_r71_20261012.py` **16 例/5 组**，核心是 D 组反向锁：全仓 `word_closeout_notice` 调用文件 == `{routers/sse_handlers.py}`、任何 `word_count=` 落库赋值右值 AST 扫不得引用收尾/诊断读数。A/B 1/1：把收尾读数塞进 `sections.py` 落库侧 → D 组 2 例定向红，`finally` 还原 sha256 字节一致。
> **验证**：ruff（F821,E9,F841,F401）三后端文件 All passed；后端全量 `pytest tests -q -x` **7604 passed / 17 skipped / 1 xfailed / 0 failed（577s）**（分簇 `-k "caliber or chart_fix or fix_mermaid or sections or content_utils or preflight or word"` 568 passed，R70 26 例未破坏）；`tsc --noEmit` rc=0；前端 Vitest 4 文件 **79 passed**（chartTypesParity 独立 mock 读 `word_count`→undefined→只更正文不动字数，行为正确）；六前端文件字节体检无 BOM/FFFD、CR==LF。详见 `docs/字数收尾透明警告与前端并列口径收口_R71_20261012.md`。
> ⚠️ **递延（未动）**：口径切换本体需「预算重标定（÷1.285）+ 系数逐章分布 + 落库回填」三件套同轮评审；`content_stable_prefix_enabled=False` 且 D1 开启时 `_plan_guidance` 静默丢弃（R70 前既有，影响面小未改）。

> **上一轮校准：2026-10-12（正文链体检 + 易标字数标准对账·R70 两段式）** —— **【两份外部审计报告（审的都不是本仓）先对账再只补真实缺口；F1 按「加法式诊断读数」收口，零行为变更、零新增依赖、零数据迁移、零新增配置项】**：
> ① **【先否掉「照搬」这个误读】** 报告_易标 审 `J:\编程\OpenBidKit 易标`（Electron+JS）、报告_章节编写 审 `J:\编程\招投标方案平台`（FastAPI+Celery）——**都不是本仓** ⇒「引用至本软件」只能是对账 + 补真实缺口。15 条标准逐项对账后 **12 项早已落地**，只有 9 条真缺口（R4/R5/R6/R9/R10/R12/R13/R14 等）；首轮用正则判「已在场」的 9 条同名异义误报全部靠上下文复核推翻。报告二的 BUG-2/3/4（CRLF 图表丢失 / 压缩不落库 / SSE 事件不消费）在本仓**均不存在**，逐项有证据。
> ② **【F1·P1 · 口径噪声被当成模型缺陷】** `text_word_count` 是「剔围栏后的**字符数**」，中文标点 / `| - # * >` / URL / 空白换行全计入，而预算（800/1200/2000/2500）是按人的「字数」直觉定的。33 章真实语料实测：字符口径 mean **136.9%** → 可读口径 **106.5%**（虚高 30.4pt）；缺口 15,050 字中标点占 8,744（58%）；换算系数 **1.285**；`>1.3×` 触发压缩章节 **19/33 → 4/33**；word_status 翻转 19/33。后端 18 处 import 同一函数 + 真实库落库不一致 **0/33** ⇒ 后端单口径成立（这一项是绿的）。
> ③ **【最优方案 ≠ 翻默认值（本轮最大判断修正）】** 一度把 F1 表述成「`auto_shrink_over` 默认值建立在虚高读数上 ⇒ 该翻」。实施前重新对账后**否定**：R66「设了 tier 就开」是刻意决策、可读口径下仍有 4/33 真实超写、翻默认会重排每章 AI 调用费用（越出「加法式零行为变更」授权边界）；真根因是**口径与预算配对**问题，切口径必须与 tier 数值（÷1.285）**同轮**决策，只动一头会反向偏差（只切口径不降预算 ⇒ 系统性判「低于目标」、触发多余续写）。⇒ 只新增**诊断专用**读数 `readable_word_count`/`has_cjk`（`content_utils`，剔围栏**复用** `strip_fenced_code_blocks` 不重造正则）+ 接到预检 **DLV-16 evidence 尾部**（`content` 在手边、本就是 advisory、用户正好看字数偏离）+ 订正 `sse_handlers:4266` 那条以「系统性超写 136.9%」为论据的注释；**默认值 / tier / `text_word_count` / 任何判定一字未动**。
> ④ **【两处范围收缩（偏离已批准设计，登记理由）】** 原设计②「`word_audit_summary` 增 `readable_*` 键」不可行：其 rows 契约只有 `{section_id,title,level,word_budget,word_count}`、**不带 content**，而 sse 侧把 `items` 落进 checkpoint 字段白名单（装正文既不合规也撑大存档）⇒ 改接 preflight。`word_like_count`（Word 式：CJK+标点+西文按词，实测 117.5%）app 侧**无人消费** ⇒ 按 YAGNI 摘除（连同独占常量 `_RW_CJK_PUNCT_RE` 与 docstring 交叉引用），探针侧可逐字复现。⚠️ **纯 ASCII fixture 陷阱**：既有测试 `_leaf` 用 `"x"*wc`，按词合并只剩 1 token ⇒ 新读数必须 `has_cjk(content)` 前置守卫，否则打挂 3 条既有 evidence 断言（实测：有守卫后 R66 簇 **85 passed** 逐字不变）。
> ⑤ **【护栏 + A/B 2/2】** `tests/test_word_caliber_diagnostic_r70_20261012.py` **26 passed / 6 组**，核心是**反向 AST 锁**：全仓 `app/**` 中 `readable_word_count`/`has_cjk` 消费点**只允许** `{(services/preflight_engine.py, check_deliverability)}`，另锁判定函数黑名单（`word_status_for`/`_should_auto_shrink`/`word_deviation_of`/`should_continue_round`）、任何 `word_count = ` 赋值右侧、`_shrink_default` 表达式逐字在场、`word_audit_summary` 键集合。A/B：M1 把诊断读数塞进 `_should_auto_shrink` → E 组 **3 failed**（定向）；M2 把 DLV-16 判定入参换成诊断读数 → C+D 组 **3 failed**（283% → 200%）；`try/finally` 还原后两文件 sha 与开局逐字一致。字节级 `repr()` 自检：33 章 `readable=52717` 与归档探针 **delta=0**、LF≡CRLF 同值。
> ⑥ **【本轮三条可复用教训】** ⚠️ **`ast.Name` 的属性是 `.id` 不是 `.name`** —— 探针写 `getattr(call.func,'name','')` 会**静默 0 命中**（假阴性），几乎把「已正确落盘的接线」误判成缺失；凡匹配调用名必须 `isinstance(f, ast.Name) and f.id == ...`。⚠️ **工具「保存失败」同一会话内两种相反形态都出现过**：一次**真未落盘**（内容计数 0 命中，改用整文件重写）、一次**假失败但两次都已落盘**，造成 `has_cjk`/`readable_word_count` 各两份重复定义（R65 撞名同族）⇒ 唯一可信判据是**按内容计数 + AST 顶层重名扫描**、绝不是返回报文；去重靠字节级补丁（起点锚计数==2 / 终点锚==1 / 删除范围双向断言 / round-trip 字节相等）。⚠️ **Grep 会把补丁新增行的首列缩进剥掉显示** ⇒ 疑似损坏先按 `repr` 复核再动手（R68「先证伪再动手」同族）。
> **验证**：分簇回归 6 簇 **697 passed**（口径压缩 178 / 围栏 62 / 预检 86 / 导出 172 / 章节与 SSE 79 / 目录 120，与体检基线逐簇一致）+ 新护栏 26 ⇒ **723 passed / 0 failed**；`ruff check --select F821,E9,F841,F401` 4 文件 All passed；三触碰文件 CR==LF / 无 BOM / U+FFFD=0 / AST+compile OK / 顶层无重名（content_utils `4102253e27b8`、preflight `5afe307b89ed`、sse `3e8c8392605e`）。⚠️ **需重启后端**（preflight 常驻模块）；**无需清库**（`word_count` 落库口径未动，导出产物逐字一致）。前端 5 处并列口径（F2）实测唯一真分叉是「正文只剩围栏图表」（0 vs 72）、真实库 **0/33 触发** ⇒ 降级 P3 只登记未修。
> ⑦ **【Round 7 复测（同日晚间、仍 0 行业务代码）：1.285 系数能不能用 —— 分层答案 + 结构性上限先说清】** 盘库（`probe_r70_round6_inventory.py`）扫全机 8 候选库：有正文的库**全部指向同一方案 d3c1a897**（主库 33 / `pre_r28backfill` 97 / `_r27_check_copy` 35 / `pre_r50` 与主库逐字重复；`corrupt_r27` 库 sections 表 malformed 跳过），word_budget 全池单档 1500 ⇒ §2.6 要求的「多方案/多 tier 复测」在本机**结构性不可得**（要补齐只能由用户生成新方案）。能做满的维度改走：跨章去重池 **165 独立章**（较 33 章扩样 5 倍）+ **人为套用四档**。三组读数（`probe_r70_round7.py`，口径全部复用生产函数、未新造尺子）：**A** 逐章 text/readable 系数 mean 1.283 / median **1.267**（与单库 1.285 仅差 1.4% ⇒ **总量级换算稳健**）、但 CV 9.5%、极差 **1.118~1.929**（⇒ **逐章折算禁用固定系数**）；**B** 系数形态依赖：表格重 1.313 / 列表重 1.336 vs 表格轻 1.199 / 含围栏 1.196（主因 = 结构符密度；图片/URL 维度仍未覆盖，缺口继续登记）；**C** 四档模拟（standard 1200 档）字符口径触发的压缩 **43%（60/139）属无谓纠偏**、**漏触发恒为 0**（readable≤text 恒成立 ⇒ 切可读口径不会新增「该压没压」，风险全在虚高单侧）、in_band 两口径翻转 22%~43%（「口径与预算必须同轮」断言获跨章样本支撑，§11.1 三条全部保持）。真决策轮依据面已齐，待补：第二方案语料 + 图片/URL 形态维度。
> ⚠️ **Round 7 三条踩坑登记**：① 探针全量池把多库 content 一次性 fetchall → 进程胀爆被 **OOM 杀**；改逐库连接 + `fetchmany(8)` 流式 + 只留指标后 165 章数秒跑完（探针内存红线：全文不同时驻留）。② `pathlib.read_text()` 的 universal-newlines 把 CRLF 转 LF，含 `\r\n` 锚串的补丁**静默 0 命中** —— 本仓文档字节补丁一律 `read_bytes().decode()` + `write_bytes()`。③ `·`（U+00B7）经 PowerShell `-c` 传参会被破坏致锚串失配 → 含非常规字符的补丁改走脚本文件；`DeleteFile` 假成功残留再现（§11.5 形态③，已按惯例收缩成无害注释存根）。
> **详见** `docs/正文链体检与易标字数标准对账_R70_20261012.md`（§5 15 条对账表 / §6 误报复核方法论 / §8 回归基线 / §11 实施收口与偏离登记 / §12 Round 7 复测：盘库·系数分层答案·形态分组·四档敏感性；探针归档 `artifacts/r70_wordcount_audit/` round1–7）。

> **最近校准：2026-10-11（导出模板残留清洗接线收口 + 离散编排 D1 登记与集成缺陷修复）** —— **【导出 P0-B 五个出口收敛 + D1 两处登记缺口补齐 + 1 处 D1 集成 P1 缺陷（功能全灭）修复；新增 3 条护栏，A/B 2/2】**（零新增依赖、零新增配置项、零数据迁移）：
> ① **【P0-B · 模板残留绝不进交付件第一页 / 下载名】** 五个出口统一到「残留清洗 + 项目名兜底」：`_scheme_form_header`（前置表单「工程名称 / 方案名称」抬头）、`_add_compilation_note_page`（编制说明页双标题）、`_build_docx_sync` 封面标题与页眉、`export_docx` / `export_pdf` 下载文件名 —— 封面与下载名共用 `_display_scheme_name`，避免「封面印残留串、下载名却是导出文档」两份署名对不上。四条保守红线：① **只整串判空、绝不裁剪**（把真实工程名剪掉一截是无声内容损坏，用户无从察觉）；② 词汇表**刻意不含** `示例`/`模板`/`项目`/`工程` 四个泛词（「示例工程」可能是真实落库名）；③ 项目名兜底**只认**事实标题明确写「项目名称 / 工程名称」的条目、绝不猜，取不到 → 文件名回退 `导出文档`（与前置表单「签字栏不预填」同一红线）；④ 残留页眉置空 = 未配置页眉，不编造替代内容。
> ② **【D1 登记缺口 → 4 个既有护栏失败】** `content_plan_system` / `content_plan_user` 未进 `PROMPT_VARIABLE_CONTRACTS`（r38 / r39 两处双向护栏红），`scene="content_plan"` 未进 `provider_factory.KNOWN_SCENES`（漂移护栏红）。补登记后相关 5 个测试文件 **96 passed**。⚠️ scene 必须用**字符串字面量**书写（常量会被 AST parity 护栏判「登记了却无调用点」，§R57 同坑）。
> ③ **【P1 · D1 编排阶段实际 100% 空转（本轮最大发现）】** `sse_handlers` 里事实候选池写成 `[str(r.get("title") or "") for r in facts_rows]`，而 `_load_facts_rows` 的行契约是**5 元组** `(gt, title, content, confidence, chapter)`（docstring 明写「必须投影回 5 元组，不得直接外泄」）→ **每章必抛 `AttributeError`**，被 D1 外层 fail-soft 吞掉 ⇒ 编排阶段功能全灭、日志只剩一条 WARNING。实测复现后按 `_row_chapter` / `_render_facts_text` 同口径改 `r[1]` 取 title。D1 原有 13 例单测**全部直调纯模块**、无一触及 sse_handlers 集成点 ⇒ 「实现缺失表现为功能不生效」这类缺陷只有端到端 / 形态护栏能抓（AGENTS §5 同族教训）。
> ④ **【护栏 + A/B】** `tests/test_content_plan_d1_20261011.py` 13 → **15 passed**（新增 2 条静态护栏：事实池必须下标取 title 且全文件不得再出现 `_f_pool = [str(r.get("title")`、知识库池必须 dict 取 `name`）；`tests/test_doc_quality_report_fixes.py` **30 passed**（含 TestResidueWiring 4 例：文件名绝不带残留 / 项目名兜底 / 两个出口一致 / 五处出口 AST 接线）。A/B 2/2：把事实池退回 `r.get("title")` → 新增 2 条护栏**同时定向失败**，`try/finally` 还原后 sha256 字节一致。
> **验证**：全量 `pytest tests -q` **7481 passed / 17 skipped / 1 xfailed / 0 failed（476s）** —— 此前 8 处失败**全部归零**（4 处导出残留测试 + 2 处契约覆盖 + 1 处场景漂移 + 1 处 D1 收集错误）；簇回归 6 文件 **135 passed**；`ruff check --select F821,E9,F841,F401` All checks passed；触碰文件 CR==LF / 无 BOM / U+FFFD=0 / AST OK。
> ⚠️ **未落地（记录以免重复排查）**：`content_stable_prefix_enabled=False` **且** D1 开启时，`_plan_guidance` 只被拼进 `_chapter_extras`、而后者仅在 stable-prefix 分支被消费 ⇒ 引导语静默丢弃（LLM 调用照跑、`plan_json` 照落库，正文只是看不到建议）。双重 opt-in 组合、影响面小，本轮只记录未改；改法是在 `else` 分支把 `_plan_guidance` 拼到 `user_content` 末尾（并需把 `_plan_guidance = ""` 提到 try 之外，否则未赋值会 NameError）。
>

> **最近校准：2026-10-11（作用域谓词单一事实源收敛 + 静态护栏 · R69）** —— **【R68 遗留项①②收口：3 份字面量副本→1 份；零行为变更、零新增依赖、零数据迁移】**：
> ① **【收敛】** 在 `facts_extractor.py` 新增 `FACT_SCOPE_PREDICATE = f"(scheme_id=? OR {FACT_SCOPE_SHARED_FRAGMENT})"`（组合自共享半句，非新字面量）；`global_facts.py` 的 `_FACT_SCOPE_PREDICATE` 改为 import 别名（7 处引用零改动）；`export.py:1159` 的内联全范围字面量改为 `"FROM global_facts WHERE " + FACT_SCOPE_PREDICATE`；`facts_extractor.py:2972` 的内联组合改用常量。全仓 `ast.Constant` 扫描确认作用域谓词字面量从 **3 份 → 1 份**（仅 `FACT_SCOPE_SHARED_FRAGMENT` 定义处）。
> ② **【为何定义留在 facts_extractor】** `global_facts` 已 import `facts_extractor`，反向 import 会循环依赖 —— `facts_extractor`（services 层）是两侧共同下层，沿 `json_mode_compat.py` 先例。
> ③ **【护栏 12 例 / 3 组】** `tests/test_fact_scope_single_source_r69_20261011.py`：TestFactScopeSingleSource（全范围字面量全仓 0 命中 / 共享半句仅 facts_extractor 1 处 / 定义唯一 / FACT_SCOPE_PREDICATE 组合自 SHARED_FRAGMENT）+ TestImportWiring（global_facts 别名化 / 双侧 import / export 无内联 / 无循环依赖）+ TestRuntimeEquivalence（三处常量值逐字一致 / 形状正确 / 共享半句是全范围子串）。判据锚定 `ast.Constant` 字符串字面量（§5.14 教训），不锚定源码全文。R54 既有护栏 `test_no_inline_scope_predicate_copy_left_in_module` 同步更新（`== 1` → `== 0`）。
> **A/B 3/3**：A1 global_facts 改回字面量→rc=4 收集错误；A2 export 改回内联→**3 failed**；A3 facts_extractor 改回纯字面量→**3 failed**；还原后 13 passed + sha match。
> **验证**：`pytest tests -k fact` **949 passed / 5 skipped / 0 failed（41.14s）**；`-k export` **581 passed / 1 xfailed（65.89s）**；新护栏+R54 修复 **13 passed**；`ruff check` 5 文件 All passed。5 文件字节体检 BOM=False / U+FFFD=0 / lone_CR=0 / py_compile OK。
> **详见** `docs/全局事实作用域谓词单一事实源治理_R68_20261011.md` §6（R69 续作：收敛方案 / 护栏 / A/B / 验证基线 / 未落地更新）。

> **最近校准：2026-10-11（全局事实「作用域谓词」单一事实源盘点 + export.py `layout_preset` 移植恢复 · R68）** —— **【零代码行为变更、零新增依赖、零数据迁移；核心产物是 3 份判据副本的盘点 + 一次真实的 `git checkout` 事故记录】**：
> ① **【盘点 · 同一条作用域谓词有 3 份各自内联的副本】** 「判断一行 `global_facts` 是否属于本方案可见范围」是本仓**安全关键**判据（安全关键标注 / 项目共享层去重 / 提取后回退清 stale / 导出交付门控全部依赖它），实测有 3 份副本：① `routers/global_facts.py:527` `_FACT_SCOPE_PREDICATE`（全范围，7 处内联副本已在 R54 收口）；② `services/facts_extractor.py:2911` `FACT_SCOPE_SHARED_FRAGMENT`（仅共享半句，供导出侧复用）；③ `routers/export.py:1159` **再内联一次全范围字面量**。三份语义逐字等价，不是行为缺陷，而是结构性风险（本仓反复记录的「同一判据多副本」同族）。
> ② **【刻意不收敛 ③ 的真实原因 · 循环依赖】** 理想是把 ③ 改为 import ①，但 `global_facts` 已 import `facts_extractor`，反向 import 会形成循环依赖 —— 正确方向是**下沉到共同下层**（沿用「JSON 模式兼容性判定」的 `json_mode_compat.py` 先例），属跨模块重构需全量回归，且本轮并行会话正在改写 `global_facts.py`（-1594 行重构在途），动 import 结构极易「后写者赢」⇒ **本轮只盘点 + 记录**。缺的是「不得再新增作用域谓词字面量」的静态扫仓护栏，记为下轮最小动作。
> ③ **【本轮真实事故 · `git checkout` 丢并行会话工作，已恢复】** 为恢复 `export.py` 中疑似残缺的语法行执行 `git checkout HEAD -- export.py`，结果**并行会话的 R65 `layout_preset` 改动（11 处）整体丢失** → `TestLayoutPreset` 8 例 + `TestFrontendBackendConfigParity` 1 例 **9 例全红**。从 `%APPDATA%\Qoder\SharedClientCache\cache\workingSpace\94dbb906…__export.py` 快照按行号提取 4 个代码块（LAYOUT_PRESETS 常量 + `_resolve_layout_preset` / `_prepare_export` 读取 / `_build_docx_task` 收敛点 / `_EXPORT_CONFIG_KEYS` 白名单），4 处锚点全部 `anchor_count==1` 校验后移植回 → **`layout_preset=11` / `LAYOUT_PRESETS=2`** 与快照逐字一致，**89 例全部转绿**。
> ⚠️ **【三条可复用教训】** ① `git checkout` 前必须 `git status --short` **明确列出文件名**确认 diff 是否承载未入库工作 —— 本轮首查用 `git status --short -- <3 个文件名>`，**检查范围不含 export.py，等于没查**（§「行尾损坏」记录的第二次踩中）。② **快照可能是中间态**：9 份快照 `layout_preset` 计数 4→7→8→11 递增，必须按时间戳取最新并按内容计数交叉验证。③ **「残缺语法行」要先证伪再动手** —— 最初诊断出 `global_facts.py:1707` 疑似残缺，复核后发现 `FACT_SCOPE_SHARED_FRAGMENT` 全文件**仅 2 处**（import + 正常用法），**残缺行根本不存在**，那是并行会话写入的瞬时中间态被其自身修正。读到疑似损坏内容后若文件正被其它会话写入，**先重读原文再下结论**。
> **验证**：`pytest tests -k fact` **503 passed / 79 xfailed / 0 failed（67.67s）**；`-k export` **581 passed / 1 xfailed（67.52s）**；R65+closeout 双文件 **89 passed**；`ruff check export.py --select F821,E9,F841,F401` All passed。四触碰文件字节体检 **BOM=False / U+FFFD=0 / lone_CR=0 / py_compile OK**（`facts_extractor.py` bare_LF=3052 与 `SchemeWorkbenchPage.tsx` bare_LF=11855 是本仓既有形态，`lone_CR=0` 即证明行尾未被破坏，**刻意不统一行尾**）。⚠️ `tests/test_doc_quality_report_fixes.py` 收集期 ImportError（引用并行会话尚未实现的 `export_audit` / `unescape_markdown_literals`），与本轮无关。
> ⚠️ **运维**：需重启后端（`LAYOUT_PRESETS` 是启动期常量）；**无需清库**（`layout_preset` 已纳入指纹白名单，默认 `""` 产物逐字一致）。
> ⚠️ **`docs/技术债治理报告.md` 不存在**（全盘 `os.walk` 零命中），按仓库惯例另立 `docs/全局事实作用域谓词单一事实源治理_R68_20261011.md`（R66/R67 已被并行会话占用，本轮顺延 R68）。
> **详见** `docs/全局事实作用域谓词单一事实源治理_R68_20261011.md`（3 份副本盘点表 / 循环依录取舍 / 事故与恢复全过程 / 三条教训 / 验证基线表 / 4 条未落地）。

> **最近校准：2026-10-11（流程图渲染可读性收口 · R67）** —— **【用户实测样张（深基坑流程图 4600×1550 PNG）暴露四类渲染缺陷，逐像素取证 → 代码根因 → 全部修复；单链几何逐字不动、R66 配色一字未动、`_RENDERER_VERSION` 6→7】**（零新增依赖、零数据迁移、零新增配置项）：
> ① **【P1 · 菱形版式】** v2 渲染器菱形原按 `d=min(w,h)/2` 画成 190×190 小方块（居中于 500×200 形状盒），文字却按整个外接盒排版 ⇒ 长标签横向溢出菱形轮廓压到相邻节点（样张 `CheckBIM -- \"通过\"`）；且菱形贴列左缘、与同列矩形错位 100px。修复：菱形四点改 `(cx,y)/(x+w,cy)/(cx,y+h)/(x,cy)` **铺满形状盒**（与 `_render_flowchart_with_edges` 同构，消除两渲染器口径分裂）+ 列内居中偏移 `(node_w-w)/2·scale`；新增 **`_draw_diamond_label`**：文字按**内接矩形**（外接盒一半扣 4·scale 内边距）排版 + 字号 16→12 design 逐级自适应 ⇒ 文字绝不越出菱形。两渲染器共用同一实现。
> ② **【P1 · 连线分道（`_plan_edge_lanes` 唯一共用实现）】** 原 TD 边折线中点恒为 `(sy+ey)/2` ⇒ 同一层间间隙内 N 条边共线并轨成一条贯穿长轨（样张 6 条分支边并成 3900px 长轨）。现分带：前进边→目标层上方带（band=目标层-1）；TD 同层边→源层下方带；LR 同层/回退边→图底母线带（band=-1）；**TD 跨层回退边刻意不分带**（保持旧直连，绕行母线会把箭头藏进中间节点后面）。带内按 `(k+1)/(n+1)` 均分通道；带净高不足 `(n+1)×16 design` 时层间间隙自动加高。**单链不变式：每带 1 条边时加宽量=0，通道 y=旧 `(sy+ey)/2`，R66 单链几何逐字保持**（R66 护栏 5 条几何探针原样通过，唯版本断言就地 6→7）。
> ③ **【P2 · 标签转义清洗】** AI 偶发把 JSON 风格 `\"` 原样写进节点标签，渲染把反斜杠印上图。新增 `_unescape_label_text`（`\"`→`"`、`\'`→`'`、`\\`→`\`，纯机械）+ `_strip_quote_pair`（**成对包裹**才剥引号，替换原 `strip('"')` 的破坏式剥法）；清洗顺序 = **先还原转义再剥引号**（`_extract_mermaid_node_label_raw` 入口 + `_extract_mermaid_node_label` 包装层兜底 + `_clean_edge_label` 重排）。**顺序教训**：反了会把 ``label\"`` 的尾部引号先剥掉留孤悬反斜杠（本轮实测踩中）。
> ④ **【A/B 四变异全 CAUGHT（`artifacts/r67_flowchart/_ab_driver.py`）】** M1 菱形退回小方块 / M2 分道退回公共 mid / M3 摘除全部转义清洗 / M4 内接文字退回外接盒排版。**两条教训**：① 文字探针必须量**整体跨度**而非最长连续暗像素段 —— 字形内部有空白，最长段永远只有一笔画宽，首轮 M4 假绿；② CRLF 仓库多行变异点要 `\r\n` 适配，且**每条变异跑完立即还原** —— 首轮驱动把 M1/M2 变异和一次裸 LF 留在源文件里，靠字节体检护栏抓回。
> 护栏：`tests/test_flowchart_legibility_r67_20261011.py` **28 例 / 5 组**（A 菱形版式 5 · B 连线分道 6 · C 转义清洗 5 · D 版本与单一实现 6 · E 字节体检 6），像素探针全部对**真正交付的 PNG**（R64 教训）。
> **验证**：`ruff check app/services/ai/{mermaid_flowchart,mermaid_renderer}.py … --select F821,E9` All passed；图表关联簇 `-k "chart or mermaid or flowchart"`（排除并行作业半成品 `test_word_budget_tiers_r66_20261011.py`，与本轮无关）**626 passed / 1 skipped / 1 xfailed** 零回归。⚠️ **需重启后端**；`_RENDERER_VERSION` 6→7 已令旧缓存失效，无需清库。行为变化：含判定分支的流程图层间间隙按分支数自动加高（图变高、连线可分辨）、菱形变大且文字内接、标签无 `\"` 残留。
> ⚠️ **刻意未做**：① TD 跨层回退边保持旧直连路由；② `with_edges` 的 TD 跨层回退边同列正反重叠属既有形态（修复需"边级通道唯一化"，建议与提示词侧"回退边必须绕行"合并一轮）；③ 标签里 `-- "通过"` 只做转义清洗、不做语义拆分（把分支结果搬回边标签属生成端问题，R66 提示词已禁止标签含引号）；④ 流程图未接 `paper_floor_note` 纸面毫米播报（不在 R64 五类渲染器清单内）。
> **详见** `docs/流程图渲染可读性_菱形版式与连线分道_R67_20261011.md`（截图逐像素取证 → 结构反推 → 复现/A-B 脚本归档 `artifacts/r67_flowchart/`）。

> **最近校准：2026-10-11（引入《文档生成规则分析与软件标准建立》四项生成标准 · R65）** —— **【把外部"文档生成标准"文档与本仓既有能力逐项对账，只补真实缺口、且全部做成加法式建议/可选预设，不动摇"建筑工程专项施工方案编写"定位】**（零新增依赖、零数据迁移、新增配置项 **1 个（`layout_preset`，缺省空 = 逐字节回退）**、正文/目录既有 `ok` 结论与产物版式**一字不动**）：
> ① **【先做对账，再动手：15 项清单里 12 项本仓早已落地】** 外部文档的 目录/正文/图表/导出 四类标准，逐一映射到现有模块后确认**大部分已是既有契约**：命名规范与禁用语（`ai/prompts/_shared.py SHARED_FORBIDDEN_WORDS` + `outline_quality.detect_generic_titles`）、空话/占位/套话检测（`content_standard.py` + `content_fuzzy.py`）、表格三线表 + 表头加粗底纹（`export.py` 底纹 `B6B1D1`）、图题"图 章-序"编号（`export.py`）、配色 `#f0edff/#a89aff/#7C63D8`（`mermaid_flowchart.py`）、工期"日历天"归一（`facts_extractor.normalize_unit`）、术语归一（`_norm_dicts.py`）、宋体正文/黑体标题/正文 12pt（`export.py` 默认）。**重复实现同一判据 = 制造第二套口径**（R55/R62 反复强调的反例），故本轮只补三类真缺口。
> ② **【P2 · 目录结构规范"建议"检查（`outline_quality.py`）】** 新增纯函数 `check_structure_standards(outline)->dict`（`:432`）+ 递归深度 `_max_depth()`（`:488`）+ 文案 `render_structure_notice()`（`:500`）；常量 `OUTLINE_CHAPTER_MIN/MAX=5/10`、`_RECOMMENDED=7`、`OUTLINE_SECTION_MIN/MAX=2/15`、`OUTLINE_MAX_DEPTH=4`（`:422`）。三类告警 `chapter_count/max_depth/section_count` **只产 warning 文案，绝不参与、绝不翻转** `check_outline_continuity` 的 `ok`（该函数被 AST 护栏锁定不得侵入）。接线 `sse_handlers._attach_quality_report`：报告新增 `"structure"` 兄弟键 + `logger.info("…（仅建议，不阻断）")`，用 info 而非 warning 以免被误读成阻断项。
> ③ **【P2 · 正文段落结构建议（`content_standard.py`）】** 纯函数 `paragraph_structure_report(content)->dict`（`:707`）产 `long_paragraph`（>500 字，区间常量 `PARA_MIN/MAX/RECOMMENDED_CHARS=50/500/150`，`:684`）、`date_format`（`2026/1/5`、`2026-1-5` 这类非 ISO 日期）、`duration_unit`（"120天"疑似该写"日历天"，靠语义词共现 `_PS_DURATION_CTX` 降噪）三类**咨询级** warning（`severity:"advisory"`）；跳过表格行/代码围栏。**接线**在正文生成后自检段：`report["structure"]=_struct_rep` 并重建 `report_json`，同样只 info 不阻断。**⚠️ 本轮最大教训：新常量首版命名 `_FENCE_RE/_TABLE_ROW_RE` 与模块既有同名符号（`:181` 供 `strip_code_blocks` 用）撞名，静默覆盖后打挂 3 条既有用例 ⇒ 全部改 `_PS_` 前缀；补回归锁 `test_no_duplicate_module_level_names`（范围限本轮真正追加的 3 文件，避开 sse_handlers 里既有的条件式重定义）+ `test_content_standard_fence_regex_single_definition`。判据：往老模块**追加**纯函数时，模块级名字空间是共享的，先 `grep` 再取名。
> ④ **【P1 · 导出版式预设 `layout_preset`（`export.py`，唯一新增配置项）】** `LAYOUT_PRESETS={"engineering":{line_spacing 1.5, margins 上下2.54/左右3.18}}`（`:1796`，取通用 Word"工程公文"档）+ 纯函数 `_resolve_layout_preset(config, base_opts)->dict`（`:1804`，**返回新 dict**、缺省/未知名原样返回、用户 `config.margins` 显式键**赢过**预设补缺）。在 DOCX/PDF **两条链路唯一收敛点** `_build_docx_task`（`:4184`）套用一次即双侧同生效；并在 `_prepare_export` 的 `docx_options` 内 `config.get("layout_preset")` **真实读取**（`:4120`，满足前后端 parity 护栏"白名单键必须被消费"）。`"layout_preset"` 纳入 `_EXPORT_CONFIG_KEYS` 指纹白名单（`:4232`，改它即失效导出缓存）。**既有默认（四边 2.5cm / 行距 1.15）是历轮锁定的字节契约，一字未动** —— 缺省 `""` 时产物与改造前逐字一致。前端在导出"正文样式"面板补 `layout_preset` Select（"不启用/工程公文"）+ `initialValues.layout_preset=""`，满足 `test_backend_whitelist_keys_are_reachable_from_ui`。
> 护栏：`tests/test_doc_generation_standards_r65_20261011.py` **48 例 / 5 类**（A `TestOutlineStructureStandards` + B `TestParagraphStructure` + C `TestLayoutPreset` + D `TestGenerationWiring` + E `TestByteHealthAndShadowing`），并同步 `test_outline_name_line_20260927.py`（报告键集新增 `"structure"`）。**关键回归锁**：`test_no_duplicate_module_level_names`（防再次撞名）、`test_content_standard_fence_regex_single_definition`（`strip_code_blocks` 用的 `_FENCE_RE` 唯一定义）、版式缺省逐字节回退 + 用户边距覆盖优先。`test_export_closeout_20261006.py::TestFrontendBackendConfigParity` 全绿（`layout_preset` 既被 UI 触达又被源码消费）。
> **验证**：`ruff check app/services/{outline_quality,content_standard}.py app/routers/{export,sse_handlers}.py --select F821,E9` All checks passed；`tsc --noEmit` rc=0；分簇回归 **89 passed**（closeout+r65）/ **172 passed**（content_standard 全簇+outline+r65）/ **119 passed**（export+docx_smoke），三处端到端冒烟脚本输出 `ALL OK` 后已删除临时文件。**注**：同机存在并行作业（AGENTS 顶部另有 2026-10-11「global_facts Pydantic 迁移」块），本轮未跑全量 `pytest tests` 单次到底 —— 多次并跑会抢 SQLite 造成 `rc=-1` 假失败，改分簇单跑稳定全绿；跨轮减法对账按惯例不做。
> ⚠️ **刻意未做（避免第二套口径 / 高风险改动）**：① 目录/正文**硬阻断**（章数、字数上下限设为失败）—— 专项方案的规模因项目而异，外部文档的数字是通用建议不是本仓法规，一律仅 `advisory`；② 图表配色/三线表"再实现"（早已在位）；③ 提示词侧再写一遍段落结构规则（`ai/prompts/content.py` 已含观点句/措施句/不空话约束）。
> **详见** `docs/文档生成标准引入_目录结构与段落建议与版式预设_R65_20261011.md`。

> **上一轮校准：2026-10-11（全局事实路由契约改造 · Pydantic 请求体迁移 · 收口 #2 遗留项）** —— **【`global_facts` 路由「裸 `data: dict` 收 body」债的零风险切刀：6 个写/诊断端点全部迁到 Pydantic 请求体 + 9 个测试文件 ~50 处直接调用同步改写 + D 组护栏锁死「不得再新增裸 dict」并反向锚防回退】**（零新增依赖、零数据迁移、前端零改动、HTTP 侧行为逐字不变）：
> ① **【P1 · #2 遗留项收口：6 端点体由裸 dict 改为 `*Request` 模型】** `test_global_facts_route_contract_20261011.py` 头部点名的 #2 债（15 条路由全用裸 `data: dict`，换成模型会触发 422 + Pydantic JSON 牵动 922 用例与前端 `factsApi` 错误展示，属**契约变更**）**已对收 body 的 6 个端点完成迁移**：`app/models.py` 新增 `DangerCheckRequest` / `ClearFactsRequest` / `AckStaleRequest` / `ResolveConflictRequest` / `BatchResolveRequest` / `AdjustRequest`，**全部 `ConfigDict(extra="allow")` + 字段默认空串** —— 历史/前端多传字段不 422、缺可选字段不报错，400 仍由各端点体内显式判定（向后兼容逐字）。`global_facts.py` 中 `check_danger_scheme` / `clear_all_facts` / `batch_ack_stale` / `resolve_conflict` / `batch_resolve` / `adjust_facts` 签名 `data: dict` → 对应模型，函数体改用属性访问；`batch_ack_stale` 顺手删掉冗余的 `isinstance(data, dict)` 守卫。其余 9 条 GET 路由本就不收 body，未动。
> ② **【关键决策 · `fact_ids` 用裸 `list` 而非 `list[str]`】** `AckStaleRequest` / `BatchResolveRequest` 的 `fact_ids` 刻意写成裸 `list`：历史调用会在 `fact_ids` 里塞 `None` / 空串（测试 `test_global_facts_batch_resolve_safety.py` 直接传 `["", "  ", None]`），端点体内仍逐条 `str(f).strip()` 过滤。若写成 `list[str]`，Pydantic 对 `[None]` 会 422，直接打挂该边界测试 ⇒ 用裸 `list` 保旧行为。
> ③ **【护栏升级 · D 组（在既有契约测试文件内）】** 原 D 组只反向锚「4 个裸 dict 端点仍存在」；现改为**双向**：`test_D1_no_bare_dict_body_endpoints` 扫 `gf.router` 全部路由，**任何** `data: dict` 收 body 立即红（防回退/防复制粘贴新增）；`test_D2_migrated_endpoints_use_pydantic_body` 反向锚这 6 个端点**确实声明了 Pydantic 模型**（`typing.get_type_hints` 判 `data` 注解为 `BaseModel` 子类，防悄然改回 `dict`）。前端 docstring #2 说明同步更新为「6 个端点已全部迁移」。
> ④ **【测试同步 · 9 文件 ~50 处直接调用】** 绕过 HTTP 直接以 `dict` 调这 4 个函数的测试全部改为传模型实例：直接 `import` 模型（`AckStaleRequest` / `BatchResolveRequest`）或走 `gf.<Model>(...)`。HTTP 侧（`client.post` 发 json）不受影响，行为逐字不变 ⇒ 922 用例与前端 `factsApi` 无需改动（契约变更按最优方案递延到与前端协同一轮）。
> 验证：受影响 **10 个测试文件全部通过**（含新增 D 组 `test_D1` / `test_D2`），无回归；全量 `pytest tests` 中本模块相关簇零失败（完整 10 文件套件后台复核中，作为权威确认）。
> ⚠️ **【递延（按最优方案，未动）】** ① **Item 3（`response_model`）**：仍是契约级改动（响应体会从 dict 变 Pydantic 序列化），与 #2 剩余部分合并为一轮「前端 + 后端契约同步」最稳妥；② **Item 5（32-bit 残余）**：迁库风险，维持文档化递延。
> ⚠️ **【本轮教训 · 多行 dict 调用改模型时重复行要靠上下文消歧】** `replace_in_file` 对「`await gf.resolve_conflict(fid, {"value": "18.0m"}, db=db_conn)`」这类**逐字重复**的多处调用会 `found N times` 失败；必须用前后断言（如跟随的 `assert ei.value.status_code == 400/502`、或调用前的 `await db.commit()` / `monkeypatch.setattr(...)`）消歧。同族坑：多行 dict 的开括号 `{` 换行后，单纯的正则 `func\({` 搜不到 ⇒ 最后靠测试运行抛 `AttributeError: 'dict' object has no attribute 'fact_ids'` 兜底抓出漏改点（`test_global_facts_batch_resolve_safety.py:384`）。
> **详见** `docs/全局事实路由契约改造_Pydantic请求体迁移_20261011.md`（6 模型清单 / 端点改动 file:symbol / `fact_ids` 裸 list 决策 / D 组双向护栏 / 9 文件调用改写清单 / 验证与递延 / 教训）。
>
> **最近校准：2026-10-11（时间轴纸面毫米折行 + 全图种可观测性 · R64）** —— **【R62 §8 未落地第 4 项（AGENTS 的 R62 块列作 ⑥）收口：时间轴真正的布局修复 + 五类 PIL 渲染器接上「印到纸上多大字」+ 一次被本轮自己的取证表撞出的「文案说谎」缺陷 + 102 例新护栏 + A/B 16/16】**（零新增依赖、零数据迁移、新增配置项 **3 个全部缺省为「开」**、`TIMELINE_AUTO_WRAP=0` 时 PNG **12/12 逐字节**等于改造前）：
> ① **【P1 · 时间轴行数改由「纸面毫米」算出来，不是拍一个「每行最多 6 个」】** 时间轴是全仓最差的一张图：画布宽 `max(960, 80 + n×230 + (n-1)×24)` 随事件数**线性膨胀且无上界** ⇒ 纸面字号按 **1/n** 衰减（n=24 名称档 0.38mm / 最小档 0.28mm）。卡片宽度**不许缩**（R61 刚把 `min_node_w` 180→230 防截断），所以**行数是唯一杠杆**，而折行的代价恰好是「用高度换宽度」：宽度按 `ceil(n/R)` 收窄、高度每行 `+170` 设计 px ⇒ 16cm 宽上限与 22cm 高上限**交叉 ⇒ 最优存在且闭式可解**（就是 R62 那条「先升后降」曲线）。新增纯函数 `plan_timeline_rows()`（`mermaid_timeline.py:77`）在 `rows ∈ [2, min(max_rows, n)]` 穷举日期档毫米（`_DATE_FONT_DESIGN=12`，常驻最小档），**只有最优值 ≥ 单行 ×1.2 才真的折**（`_WRAP_GAIN`，`:44`）、**严格更好才换**（`:114`，平局保留更少行）。实测收益 1.65×(n=6) / 2.19×(8) / 3.25×(12) / 4.33×(16) / 5.24×(20) / 5.82×(24) / **6.90×(30)**，n=6…16 折后主文字 **2.47mm 达标 ⇒ 不再告警**。
> ② **【P1 · 五类渲染器统一接上「纸面毫米」可观测出口 + 一次口径分裂】** `paper_floor_note()`（`mermaid_common.py:488`）**只写日志**：不改一个像素、不改返回值、不改任何分支，内部兜住 logger 自身故障；字号**直接取真正用于绘制的那些字体对象**（抄常量表 ⇒ 日后改字号播报静默失真，R57 同类坑）。接线 5 处：时间轴 `:491` / 布局 `mermaid_layout.py:356` / 对比 `mermaid_comparison.py:479` / 甘特 `mermaid_gantt.py:856` / 劳动力 `mermaid_labor.py:401`；架构图**刻意不接**（已有预算告警，接了就是两条红灯）。**下限读数唯一落点** `PAPER_FONT_MIN_MM = 2.4`（`:405`）。⚠️ 口径分裂（本轮最大的一次口径决策，`:516` `main = min(judge) if judge else worst`）：960 设计 px 的宽度地板使日期/说明档**数学上永远**到不了 2.4mm（2.00/1.80）⇒ 若 warning 判最差档则**每张时间轴都红灯**、告警失去信号价值；现 **info 记最差档（观测不打折）、warning 判主文字档（判定不滥用）**，两条同在一行日志里。这是 R55「假绿」的镜像问题 —— **假红**。
> ③ **【P0 · 播报文案必须说真话：同一处修了两次，第二次由自产取证表撞出】** ⑪：`rows==1` 时原文写「单行已是最优形态」—— 同一句话先说字太小、再说没办法。⑬（本轮最大发现）：跑 `all_renderers_report.md` 看到 n=8 **关掉开关**那行播报写「8 事件保持单行：折行收益未达 20% 门槛」，而 n=8 开着开关会折 3 行、赢 2.19 倍 —— 把「没人让它折」说成「折了不值」，运维会去调 `_WRAP_GAIN`，而调它对本例**完全不起作用**。修法：纯函数 `_single_row_reason()`（`:121`）把单行的**三种成因**分开交代（① 开关被关 ⇒ 报出 `TIMELINE_AUTO_WRAP=0`；② 行数上限被钉死 ⇒ 报出 `TIMELINE_MAX_ROWS_PER_FIGURE`；③ 真算过而收益不够），只产文案、绝不参与决策、绝不抛。三条成因各一条端到端护栏 + 一条纯函数例（A/B M13/M14/M15 **分别**摘除其一，另配**反向断言**：本例不是某种成因时，那种成因的环境变量名**不许出现**）。
> ④ **【纪律 · `_RENDERER_VERSION` 3→4】**（`mermaid_renderer.py:130`）本轮改的是**产物尺寸**，不升版本号则历史方案继续从 `ChartCache` 拿折行前的窄图 = 修复从未生效（R62 补上这一环后的第二次应用）。回退证明：12 个事件数各渲染一次取 PNG md5，`TIMELINE_AUTO_WRAP=0` vs 改造前 **12/12 逐字节相同**；护栏 `test_switch_off_equals_forced_single_row` 再把「env 关」与「决策恒返回 (1,n)」两条旧形态路径做成同一张 PNG。
> 护栏：`tests/test_timeline_paper_wrap_r64_20261011.py` **60 方法 / 102 收集 / 5 组**（A 公共层 18 + B 折行决策 34（含 8 参数化判定表）+ C 渲染接线与逐字回退 12 + D 全图种可观测性 18 + E 源文件字节体检 20），与 R61 簇合跑 **123 passed**。**A/B 16/16**（M0 无变异对照全绿；M1–M15 逐条定向失败，锚点全部恰命中 1 次、`restore_sha_match=True`）。首轮曾报 **12/14**，两条 MISSED **都不是护栏缺口而是我的期望名单写错**（详见轮文档 §7）。
> **基线对比**：全量 `pytest tests` **7263 passed / 17 skipped / 1 xfailed / 0 failed**（补丁⑬⑭后 555.10s；**最终树复跑 508.28s，逐字相同**） —— 同树逐次 7259(⑪前)→7260(⑪ +1 条文案护栏)→7260(⑫ 纯空行)→7263(⑭ +3 条成因护栏)，**差值可精确核对**。图表关联选择集 `-k "chart or mermaid or architecture"` **忽略本轮新护栏文件 = 549 passed / 1 skipped / 1 xfailed**，与 R62 记录**逐字相同** ⇒ 零回归；含新护栏 = 577 passed，且 `577−549=28`、`6702−6628=74=102−28` 互相咬合。`ruff check app/services/ai --select F821,E9` 与两份测试均 All checks passed；12 个触碰文件（8 生产 .py + 2 测试 .py + `AGENTS.md` + 本轮轮文档）字节体检**孤立 CR=0 / 裸 LF=0 / BOM=无 / U+FFFD=0 / NUL=BEL=FF=0 / .py compile OK，异常文件数 0 / 12**（`_r64_final_sweep.py` → `final_sweep.md`）。**不做跨轮减法对账**（沿 R59/R62 口径）。
> ⚠️ **【本轮最大教训 · 纯字面量的几何锁对「模型 ≠ 产物」是盲的】** A/B M2（把 planner 的 `h` 写成常数 330）首轮**全绿**：`test_wrapped_canvas_geometry_is_exactly_as_planned` 断言的是**硬编码字面量**（2400、`(330+5×170)×5//2`），根本不读 `_timeline_canvas_design()` ⇒ planner 模型被改坏时它照样绿，真实交付却与规划分叉（planner 优化的是一张**没被画出来**的图）。唯一有效的桥是 `test_planner_model_matches_the_delivered_png`（planner 算出的尺寸 == **真正交付的 PNG**）。与 R62 §3.5 同源，本轮再撞一次。
> ⚠️ **【第二条教训 · A/B 的期望名单要按「这条判据能否改变该用例结论」挑】** M1 MISSED 的原因是我把 `test_known_decision_table` 列为 catcher，而那 8 个参数（n=5/6/8/9/12/16/24/30）**全都本来就会折行** ⇒ 对收益门槛**结构失明**。离那段代码近 ≠ 抓得住它。判据写进驱动脚本注释，防止后人「恢复」成错的名单。
> ⚠️ **【第三条教训 · 注释级补丁同样会让全文 file:line 失真】** 补丁⑮（纯注释/文档串，零行为变化）把 `mermaid_common.py`/`mermaid_timeline.py` 的行号整体推后 2 行 ⇒ 文档里 20 余处引用全部过期。行号**只能由取证脚本产出**（`artifacts/r64_timeline/_r64_locations.py` → `locations.txt`），手抄必错。同轮还修掉一处 R62 第三条教训的**原样复发**：护栏 docstring 写的「R62 未落地⑤」在两套编号下**都不是**「五个渲染器」那条（轮文档 §8 第 4 项 / AGENTS 块 ⑥），已改为同时给出两套坐标。
> ⚠️ **运维**：需重启后端（`Get-NetTCPConnection -LocalPort 8000 -State Listen` 返回空 ⇒ 无残留进程需 kill，本轮未代为拉起）；`_RENDERER_VERSION` 3→4 已让旧渲染缓存键自动失效 ⇒ **无需清库**。行为变化：① n≥5 且折行收益 ≥20% 时同一份数据会产出**多行版式**的图（字变大、版式变）；② 五类渲染器每次出图多一条 info（低于下限时多一条 warning）；③ 新增 `TIMELINE_AUTO_WRAP` / `TIMELINE_MAX_ROWS_PER_FIGURE` / `CHART_PAPER_MIN_MM`（兼容 `ARCH_LEAF_PAPER_MIN_MM`）。
> ⚠️ **未落地（记录以免重复排查）**：① 布局/甘特/劳动力/对比**只接播报、未修布局** —— 实测甘特 1.68mm、劳动力 1.75mm 在**条目极少**时就已低于下限 ⇒ 它们的病不是「一行太长」（甘特被工期跨度撑宽、劳动力被阶段数撑高），不能照搬折行；② 960 设计 px 宽度地板使日期/说明档永远 ≤1.80mm，治它要动**字号与卡片容量**（R60 在架构图做过的那件事）而不是行数；③ n≥20 折完仍低于下限（2.41/2.21/2.14），正确动作是**拆成多张图**，本轮只让播报说实话、未自动拆（那要改 `get_or_render` 一图变多图的返回契约，属 R59 遗留①那一类高风险改动）；④ primary 档各只锁一个字体对象，将来更小常驻档出现时告警会挪位 —— 沿 R62 §8 第 7 项「纯描述性字段不设护栏」；⑤ **工具级**：抓 `paper_floor_note` 的日志必须把 `logging.Handler` 挂到 **root logger**（播报打在 `mermaid_common` 的 logger 上，propagation 只往根走、**不会往兄弟模块走**），挂错会打印空串、看起来像「该渲染器没告警」= 假绿。
> **详见** `docs/时间轴纸面毫米折行与全图种可观测性收口_R64_20261011.md`（机理闭式 / 决策取舍 / 落地全附 file:line / 实测收益表与 R60 0.31mm 对账 / 逐字节回退证明 / 五类渲染器现状播报 / 16 条 A/B 表与两条期望名单教训 / 基线对账 / 运维与未落地）。

> **最近校准：2026-10-11（架构图拆图纸面收益决策与告警口径 · R62）** —— **【三处并行轮遗留项并集收口 + 2 处真实缺陷修复 + 72 例新护栏 + A/B 四轮 29 例（1 条盲区自我暴露、1 条未覆盖如实披露）】**（零新增依赖、**零新增配置项**、零数据迁移、返回契约全为加法式）：
> ① **【P1 · 拆图份数改由「纸面毫米」决定，不再由分组步长说了算】** R61 按「每组 ≤8 叶」贪心分组后**无条件**竖向拼接，隐含假设「越窄越好」；但 DOCX 插图同时受 16cm 宽 / 22cm 高**双上限取更紧者**，而竖向拼接正是**拿高度换宽度** ⇒ 份数一多高上限反超，整张拼接图（含**每一张**子图）被**二次缩放**，越拆越小。逐份数真实测绘 8 组超预算树形（`artifacts/r62_leftover/_r62_curve_probe.py`）：**R61 的自然份数只有 2/8 落在曲线顶点，4 组拆完比不拆还差**（4枝×7叶 1.43→1.37、6×6 1.12→0.91、8×6 0.84→0.68、12×4 仅 +9%），插入宽度被压到 3.25~7.52cm（右半页空白凭空消失）。现新增纯函数 `plan_architecture_split()`（`mermaid_architecture.py:340`）扫 `parts∈[2,hi]` 取 `delivered_worst_mm`（**最差那张子图**，不是平均）**最大者**，且**只在严格更好时才拆**（`mm > single_mm + 1e-9`）；`_arch_split_max_leaves=8` 降级为**份数上界**（R61 的 env 契约与 monkeypatch 翻转测试原样保留），每份硬约束改用 `ARCH_MAX_LEAVES=24` —— **用 8 做硬约束会把 4枝×10叶 的全部候选一起否掉**（每个单分支 10>8）⇒ 拆无可拆、纸面停在 1.01mm。判语（含 `paper_height_binds`）下沉到**所有 PIL 渲染器的公共层** `mermaid_common.py:380-459`（`PAPER_*` 常量 `:395-398`、`paper_font_mm :401`、`stitch_geometry :424`、`delivered_worst_mm :447`），架构图 `paper_font_mm:544` 改为**委派**，不留两套口径。8/8 用例严格变好（**+65% ~ +195%**）。
> ② **【P2 · 落图前用真实 PNG 复核（第④步安全网），以及「一条薄壳出口」为什么必须存在】** 探针与落图理论上同源，但归一化/字体度量都可能让二者偏离 ⇒「拆图绝不变差」必须靠**真实交付尺寸**成立，不能靠预估自证。首版把复核直接写成共享符号，A/B **M6「摘掉复核」全绿**：patch 共享符号会在第①步（份数决策）就把候选全否决，**根本走不到第④步**。现拆出 `review_delivered_mm()`（`:391`）作复核**动作**的唯一出口（内部仍是同一份 `delivered_worst_mm`），安全网失效与出口形同虚设才**分别**可测。
> ③ **【P2 · 告警口径改到「你真的拿到什么」（本轮最大行为修复，一次修掉 2 处真实缺陷）】** 旧第②步按**整树**口径打唯一一条告警 ⇒ ⓐ 拆成功后仍打印「纸面字号仅 1.01mm … 建议拆分为约 2 张子图」—— 数字是**过期值**（真实交付 1.98mm），并且**叫用户去做程序刚刚做过的事**；ⓑ 中途否决回落单图时第②步已经打过、单图路径再打一次 ⇒ 同一张图 **2 条一模一样的 warning**，打破 R60「同一张图只告警 1 次」（实测日志 439 + 916 同帧出现）。现告警推迟到第⑤步（`_render_architecture_split:401` 的 ①420 ②430 ③441 ④464 **⑤492**），用「最差那张子图的几何 + 第④步真实交付毫米」组文案；`compose_readability_warning(..., delivered_parts: int = 0)`（`:765`）新增形参，在预算半句后追加「（本轮已自动拆分为 N 张子图）」，**默认 0 时整条文案与旧实现逐字一致** ⇒ 单图路径零改动，跨轮字面契约 `架构图超出规模预算` / `建议拆分` 仍在位（`test_arch_size_budget_20261010.py::TestBudgetRenderIntegration`）。两条出口各自**恒为 1 条** warning（AST 级护栏锁）。
> ④ **【复测结论（写清以免重复排查）】** R60 §4 逐行复核：**16叶 2.45mm / 24叶 1.65mm 未失效**（24 叶**恰在**预算上限，`24 > 24` 不成立 ⇒ 不触发拆图，该行今天仍是交付值）；**40叶/8组 1.00mm 那行失效** ⇒ 真实交付为 2 张子图 **1.98mm（+98%）**，已就地更正并在 §4.1 补合并态 10 行表。R61「三图加宽」**没有**让其它渲染器再变小：timeline/layout/comparison/gantt/labor 9 个样本逐字复测相同（5220px = `200 + 8×575 + 7×60` 本就是 `min_node_w=230` 那一档，加宽前是 4220px→1.14mm）—— 反过来成立：**加宽本身就是拿纸面毫米换「不丢字」**，timeline 24 事件 0.31mm 仍是全仓最差。⚠️ **达标口径是「叶子/有效字号」档**：40叶拆到 1.98mm 时同一份交付里的**节点字只有 1.27mm**（三档表：标题 2.12 / 叶子 1.98 / 节点 1.27）。
> 护栏：`tests/test_arch_split_legibility_r62_20261010.py` **41 方法 / 72 收集 / 5 组**（A 公共纸面层 8 + B 单一事实源 5 + C planner 13 + D 端到端不劣化 10 + E 字节+AST 5），并把 R61 的「份数硬锁」升级为语义判据（`test_r61_autosplit_and_label_widen_20261010.py` 21 例）。**A/B 四轮 29 例**：v1 10 例 → 7 红（M1 闭式摘掉高上限 / M6 摘安全网 / M7 允许空组 = **3 条盲区**）→ 补护栏；v2 11 例 → 10 红（**M11 仍 GREEN = 新盲区**）；v3 4 例 → **4/4 红**；v4 4 例 → 3 红 + **M16 未覆盖（如实披露）**。四轮均含 M0 无变异对照（全绿）、每条 `anchor_count==1` 且 `restore_sha_match=True`。
> **基线对比**：全量 `pytest tests` **7149 passed / 17 skipped / 1 xfailed / 0 failed（937.25s）** —— 收集 **7165→7167**、passed **7147→7149**，两处 +2 恰为本轮新增的 2 条告警口径用例，**零新增失败**（R55/R56 记录过的 5 项既有失败本轮全绿）。架构图簇 `r62(72) + r61(21) = 93 passed / 0 failed`；图表关联选择集 `-k "chart or mermaid or architecture"` **549 passed / 1 skipped / 1 xfailed / 0 failed（43.26s）**（与 R61 记录的 543 同集可比，零新增失败）。`ruff check app/services/ai --select F821,E9` 通过；8 个触碰文件（3 生产 + 2 测试 + 2 文档 + AGENTS.md）字节体检**孤立 CR=0 / bare LF=0 / NUL=BEL=FF=0 / U+FFFD=0 / py_compile 通过，异常文件数 0**。
> ⚠️ **【本轮最大教训 · monkeypatch 掉一个符号，就永远看不见它的实现】** A/B v2 的 M11 把 `review_delivered_mm` 的**函数体**换成 `float("inf")`，护栏**全绿** —— 因为安全网用例本身就 `monkeypatch.setattr` 这个符号（与 R57「逻辑埋在 SSE 闭包里」是**同一族**盲区：护栏看着覆盖、实为盲区）。修法不是再补一条端到端用例，而是补**不 monkeypatch 的值级等式**：`outlet == delivered_worst_mm(fonts, sizes, GAP)`、`0 < got < 目标下限`（fixture 自身可信）、`got < 同判据 gap=0`（**拼接间距真的被传进去了**）、空输入 `== 0.0` 精确。**判据：每被 monkeypatch 一次，就要为该符号自身补一条不 monkeypatch 的等式。**（v3 中 M11 与 M12「间距写成 0」双双红在这同一条用例上）
> ⚠️ **【第二条教训 · spy 的计数语义要先验证，别拿「众数」当「正文」】** 本轮自己的探针首版用 `Counter.most_common` 取众数字号，而字体对象**每种只创建一次**、被记录次数恒为 1 ⇒ 「众数」实际抓到的是**标题**，联合口径整体**虚高 7%**（2.12 vs 1.98 = 30/28）。凡「取最大 / 取众数」先问一句「次数从哪来」。
> ⚠️ **【第三条 · 遗留项编号是跨轮沟通协议，指错号比不写更糟】** 代码注释一度把本轮 2 处告警缺陷标成「R61 遗留③」，而 R61 登记的③其实是结尾那句「本图仍按完整数据渲染，不做删减」（语义仍成立、刻意不改）—— 补丁⑰已把注释与文档改标为 R62 新发现，并在注释里写明「**不属于** R61 登记的三条未落地项」。
> ⚠️ **运维**：需重启后端（本轮 01:47 核查 `Get-NetTCPConnection -LocalPort 8000 -State Listen` 返回空 + `/api/v1/health` 连不上 ⇒ **无残留旧进程需 kill**；因同机有并行作业，本轮不代为拉起服务）；`_RENDERER_VERSION` **2→3**（`mermaid_renderer.py:129`；R60/R61 两轮改渲染行为都没踢缓存，属既有隐患，本轮补上）⇒ 旧渲染缓存键自动失效，**无需清库**。行为变化披露：① 超预算图可能比 R61 **拆得更少**（8 组里 6 组如此）但每张交付严格更大；② 拆图无收益时保持单图且**只报 1 条**告警；③ 拆成功后的告警数字变成**真实交付值**，并声明「（本轮已自动拆分为 N 张子图）」；④ 交付日志新增「缩放绑定=宽/高」，出现「高」即表示这次又被 22cm 二次缩放（本函数已尽力避免，留痕防回归）。回退：`ARCH_AUTO_SPLIT=0` 逐字回 R60 行为。
> ⚠️ **未落地（记录以免重复排查）**：① R61①「递归拆分支」**评估后本轮刻意不做** —— 份数搜索域会从「顶层连续切分」变成**树形搜索**，`delivered_worst_mm` 的目标函数需按整棵树重定义并防组合爆炸，且 `_allow_split=False` 现在是**有意的防环设计**；② R61②三图长标签换行（需同步改行数/块高，沿 R61 结论）；③ R61③「本图仍按完整数据渲染」不改（拆分≠删减，语义仍成立）；④ **「预算内但低于下限」（24叶=1.65mm）不自动拆** —— 触发它要把拆图判据从「超预算」改成「毫米不足」，会直接反转开关回退护栏 `test_under_budget_chart_byte_identical_across_switch`，属**设计冲突**，需单独一轮；⑤ 拆完后**节点档仍 < 2.4mm**（该改的是节点框容量而不是份数）；⑥ 五个低于下限的 PIL 渲染器（公共层已就位，排期 timeline→layout→gantt→labor/comparison）；⑦ M16 一类**纯描述性字段**（「最差那张」挑哪一张只影响文案里的画布 px 数）不设护栏 —— 为它立断言将来必成假红。
> **详见** `docs/架构图拆图纸面收益决策与告警口径收口_R62_20261010.md`（收益曲线表 / 三档口径表 / 决策取舍 / 落地全附 file:line / 四轮 A/B 表 / 基线对账 / 教训与未落地）。

> **最近校准：2026-10-10（架构图纸面可读性下限 · R60 可读性半边）** —— **【R59 §9 遗留项①「竖排不是万能解」的正解 + 双告警合并 + 61 例新护栏 + A/B 11/11】**（零新增依赖、新增配置项 **2 个环境变量（默认开启）**、零数据迁移、前端零改动、**只放大不缩小**）：
> ① **【本轮最重要的不是代码，是一条推导】** DOCX 插图走 `routers/export.py:1829 _fit_image_cm`（栏宽 16cm / 高 22cm 双上限 + PNG 自带 300DPI），于是 `纸面字号(mm) = 字号px × scale × 2.54 / DPI × fit × 10`；宽度受限时 `fit = 16cm ÷ 纸面宽` ⇒ **scale 与 DPI 全部约掉 → 纸面字号 = 160 × 字号px ÷ 画布宽(设计px)**。闭式已用**真实 PNG + 真实 `_fit_image_cm`** 逐例核对（护栏 A 组），不是推演。⇒ 遗留项①的正解**不是「限制规模」（那要删内容）**，而是**让字号跟着画布宽走**；「限制规模」只是字号封顶后仍救不回来时的第二步（并行的预算守卫 + 自动拆图恰好补在第二步）。
> ② **【落地 5 件（行号为两路合并态实测）】** R60-1 抽出竖排容量**单一事实源** `_vertical_text_capacity()`（`mermaid_common.py:304`），绘制侧与字号选择侧从此共用一套公式；`_draw_text_vertical_center()`（`:320`）改为**回传真正画出来的文字**（`-> str`，空输入也返回 `text` 而不再 `None`）—— 这是「端到端不新增省略号」护栏的前提（判据必须是实际落墨，不能用近似公式自证）。R60-2 常量块 `mermaid_architecture.py:56-88` + `paper_font_mm():366` + `resolve_paper_font_px():388`（不变量 `≥base` / `≤ceiling` / `min_mm≤0` 直接回 base）+ `resolve_leaf_vertical_font_px():411`（**放大不得新增省略号**：字号↑⇒容量↓，逐 px 回退到最长标签装得下）。硬上限全部来自**物理 box**而不是审美：横排节点 220×52 里 `line_h = 字高+4` ⇒ 2 行需 `2(f+4) ≤ 44` ⇒ **f ≤ 18 恰好**（19 就压边框）；竖排叶子 96×232 余量大 ⇒ 28；标题画在框外 ⇒ 30。R60-3 **可读性驱动的竖排升级** `resolve_leaf_vertical_mode():650`：旧规则只看叶子数，现叠加「按横排版式量 `horizontal_canvas_width():638`，即使字号封顶也印不到下限 → 切竖排」，两条规则**各自可关**（`ARCH_LEAF_AUTO_VERTICAL=0`）。R60-4 完成日志报纸面毫米数。R60-5 **双告警合并** `compose_readability_warning():592` + 渲染器单一出口 `:750-759`。
> ③ **【实测效果（单张图口径；截断=0 覆盖 14 个场景，含 40 字标签）】** 16叶/4 组 1.31mm → **2.45mm（+87%）**；「6 叶 + 1 直属岗」1.36mm → **2.52mm**；6叶/3 组横排 1.58mm → **3.11mm（自动切竖排）**；11叶 2.00 → 2.53；4叶 2.33 → 2.48。⚠️ **R59 只看叶子数的切换规则是真实缺陷**：7 个叶子的横排图曾比 16 叶的竖排图还小。长标签会把字号**自己钉回去**（24 字 28→19、40 字 →15）—— 宁可字小也不丢字。关闭态 `ARCH_LEAF_PAPER_MIN_MM=0` **逐字复现 R59**（3.17/2.33/1.58/1.36/2.38/2.00/1.31/0.88）。
> ④ **【§9 遗留项④ 的量化结论：本轮之前架构图并不是最差的】** 探针 monkeypatch 各模块 `_image_font` 记真实像素字号 + 读真实 PNG/DPI + 走真实 `_fit_image_cm`：**timeline 8/16/24 事件 = 0.92 / 0.47 / 0.31mm**（`mermaid_timeline.py:134` 画布宽随事件数线性增长**无上界**，最严重）；layout 6/16 区 2.08 / 1.27；gantt **1.85mm 且与任务数无关**（`mermaid_gantt.py:369 axis_w` 固定，加行只长高 ⇒ 病永远在「宽」这一侧）；labor 1.64；comparison 2.09。**六类渲染器机理通用**（都是 scale 2.5 + 300DPI + 同一个 16/22cm 上限），`paper_font_mm` 可直接复用。**刻意不在本轮改这五个**：每类都需要自己的 box 容量分析 + 自己的硬上限 + 自己的端到端护栏（架构一项就 61 例护栏 + 11 轮变异），一轮改五处是本仓反例。建议排期 timeline → layout → gantt → labor/comparison。
> 护栏：`tests/test_arch_paper_font_r60_20261010.py` **7 组 / 50 方法 / 61 收集**（A 模型 5 + B 不变量 9 + C 不新增省略号 5 + D 升级 9 + E 接线可观测 7 + G 合并告警 12 + F 跨层 parity/字节 3）。**A/B 11/11**（M0 无变异对照 130 passed；M1–M10 逐条定向失败且 `restore_sha_match=True`；M9「warning→info」连并行护栏 `test_over_budget_emits_warning_and_still_renders` 一起打红）。
> **基线**：全量 `pytest tests` **7060 passed / 17 skipped / 1 xfailed / 0 failed（682.89s）**，为**两路合并态**（含并行预算守卫 31 例、labor 竖排 22 例、超宽自动拆图）；架构簇 4 文件单跑 152 passed；图表大簇 `-k "chart or mermaid or arch or labor or gantt or timeline or layout or comparison or flowchart"` 692 passed / 1 skipped / 1 xfailed。R55/R56 记录过的 5 项既有失败本轮全绿。字节体检：`mermaid_common.py` 376 / `mermaid_architecture.py` 859 / `mermaid_renderer.py` 1205 / 新测试 525 行，全部 CRLF、裸 LF=0、孤立 CR=0、无 BOM、无 U+FFFD、AST 通过。
> ⚠️ **【本轮最大收获 · 可复用判据】** `resolve_paper_font_px(..., min_mm=None)` 的默认值只能二选一：取**常量** → 忘记传参的调用点对运维的 `ARCH_LEAF_PAPER_MIN_MM=0` **静默失明**且没有任何测试会红；取**环境** → 函数不再是纯函数、无法逐例对账。改成**必填关键字参数**后「忘传 = TypeError」，并由 `pytest.raises(TypeError)` 钉住。**判据：当一个默认值会让某类错误静默消失时，取消默认值就是最便宜的修复。**（由本轮自己的失败测试撞出，不是审出来的）
> ⚠️ **【第二条教训 · 判据要落在「条数」上，不是「文本」上】** 合并双告警若只断言「日志里两个短语都在」，拆回两条仍然全绿。真判据 = `len(warnings) == 1` **加上** AST 级「渲染函数体内 `logger.warning` 恰 1 处」。同理，`mermaid_architecture.py` 一度出现**两条都在喊「请拆分」**的 warning，用户读起来像两个独立故障（实为一因一果：超预算=因、纸上印不下=果）；合并时必须写明**跨轮字面契约** —— `架构图超出规模预算` / `建议拆分` 是他轮护栏锁的字面量，本轮 docstring 已点名出处，否则下一轮「润色文案」就会打挂别人的护栏。
> ⚠️ **【第三条 · 同轮双 agent 撞车，留档以免后人误判】** 本轮与另一路在 22:23–23:16 于**同一文件并行作业**：对方落地「规模预算守卫」`architecture_over_budget():357`（`ARCH_MAX_LEAVES=24 / ARCH_MAX_NODES=60`）、labor 竖排（`mermaid_labor.py` + `test_labor_vertical_r60_20261010.py` 22 例），以及「超宽自动拆图」`_should_split_arch():241 / _render_architecture_split():249`（并在子图递归里复用本轮 `compose_readability_warning`，`_allow_split=False` 时不重复告警）。**轮次号 R60 被两路同时使用**；24/40 叶两行的实测数字已被拆图行为改写。本轮 23:16 之后**停止再改 `mermaid_architecture.py`**（继续动必然「后写者赢」丢改动），已逐字节核对 R60 的 8 个关键符号全部在位、双方都守字节级补丁纪律。**「字号自适应 × 自动拆图」的联合效果需在两路都收敛后单独复测**，不在本轮抢着定稿。
> ⚠️ **运维**：需重启后端；行为变化 ① 标题/节点/叶子字号随画布宽自适应（窄图逐字不变）；② 「横排印不清」也会切竖排；③ 超大图由**一条** warning 同时说清「纸面还剩多少毫米 + 建议拆成几张」；④ 新增环境变量 2 个，默认均开启。回退：`ARCH_LEAF_PAPER_MIN_MM=0` 关整套，`ARCH_LEAF_AUTO_VERTICAL=0` 只关升级。
> **详见** `docs/架构图纸面可读性下限_字号自适应与告警合并_R60_20261010.md`（机理推导 / 决策取舍表 / 14 场景实测表 / 其余五类渲染器纸面毫米实测表 / 护栏与 A/B 表 / 撞车时间线）。

> **最近校准：2026-10-10（超宽自动拆图 + 三图非竖排修复 · R61）** —— **【R59 §9 遗留项①「超宽自动拆图」真解落地 + 遗留项④三图「正确的非竖排修复」】**（零新增依赖、新增配置项 **3 个环境变量（自动拆图默认开）**、零数据迁移、**前端零改动、导出侧零改动**）：
> ① **【P0 · 超宽自动拆图真解（收口 R59 §9 遗留项①）】** R60 只做「预算守卫 + 建议拆分」告警，本轮落地**真拆图**。关键决策：**「顶层分支分组 → 递归渲染子图 → 竖向拼接回单张 PNG」而非「改契约返回多张 bytes 列表」** —— 管线测绘结论（子代理 full-pipeline 测绘）：唯一外部入口 `render_mermaid_to_bytes`（mermaid_renderer.py:178）返回单 BytesIO；内存 LRU 键（:127）与磁盘键（:814）**只含 chart_type+payload、不含尺寸**；导出 `_insert_image_with_caption`（export.py:2090）按 `_fit_image_cm` 16×22cm **双上限等比缩放** ⇒ 拆后「更高更窄」的 PNG 对缓存/预览/导出/PDF/图号**全部零改动**。实现：给 `_render_architecture_image_v2` 加 `_allow_split` 形参（默认 True），超预算且顶层 ≥2 分支时递归以 `_allow_split=False` 渲染各子图（**防二次拆图/无限递归**），贪心按 `ARCH_SPLIT_MAX_LEAVES=8` 分组、`ARCH_SPLIT_MAX_PARTS=12` 封顶，拼接调 `_image_to_stream` 保 300 DPI。实测 40 叶/4 分支：单图 4424px 宽 → 拆后 **2870×6030**（更窄更高）。**仅 1 个顶层分支时不拆**（拆单链不缓解宽度，只会换来同宽图）。
> ② **【P1 · 三图非竖排修复（收口 R59 §9 遗留项④）】** comparison/timeline/layout 是宽矮几何、竖排破坏语义，走**加宽标签容器 + 省略号兜底**（不换行、不竖排，护栏已锁「不得引入 `_draw_text_vertical_center`」）：comparison 标签槽 `120→160*scale`（7 处）+ `bar_chart_w 480→540` + `_label_max_w 112→152`；timeline `min_node_w 180→230`；layout `zone_w 220→260`。刻意**不做换行**（换行须同步改行数/块高，churn 大于收益）。
> ③ **【运维 · 缓存版本号必须 bump】** 渲染逻辑已变，而磁盘缓存**跨重启存活**，故 `_RENDERER_VERSION` **1→2**（mermaid_renderer.py:124，使内存 LRU 与磁盘双失效；该常量有「只能定义一次」parity 护栏，测试均动态引用，bump 安全）。⚠️ **上线必须重启后端**；紧急回退 `ARCH_AUTO_SPLIT=0`。
> **基线对比**：图表关联簇 `-k "chart or mermaid or architecture"` **543 passed / 1 skipped / 1 xfailed / 0 failed（75.78s）**（基线 532，+11 = R61 入选用例，**零新增失败**）；全量 `pytest tests` **7060 passed / 17 skipped / 1 xfailed / 0 failed（680.99s）**；新护栏 `tests/test_r61_autosplit_and_label_widen_20261010.py` **21 passed**（A 判定/几何/契约 + B 行为翻转 M0–M3 + C 三图加宽几何锁 + D 字节体检）。字节体检：4 个触碰文件孤立 CR=0 / bare LF=0 / NUL=BEL=FF=0 / py_compile 通过；`ruff check app --select F821` 通过。
> ⚠️ **【本轮最大教训 ·「给纯函数传假参数省事」会丢掉被护栏依赖的分支】** 拆图路径初版为省事给 `compose_readability_warning` 传 `min_mm=0` 只产预算段 → R60 两条护栏红（`test_oversized_chart_warns_exactly_once` / `test_warning_fires_only_for_unfixable_charts`），它们断言告警**同时**含「纸面字号仅」+「超出规模预算」。修法：拆图路径**按整树口径复算真实诊断**（整树宽/高 + 该版式字号 → 纸面毫米），仍交给同一纯函数产出。⚠️ **判据：给纯函数传哨兵/假参数前，先查是否有关护栏依赖「该函数被真实参数触发的分支」。**
> ⚠️ **【第二条教训 · 新增日志本身会打破「告警条数」类护栏】** 拆图动作初版用 `logger.warning` → 超预算图落 2 条告警，打破 R60「同一张图只告警 1 次」（含 AST 级「渲染函数体内 logger.warning 恰 1 处」护栏）。修法：动作日志降为 `info`，且**子图递归渲染静音**（`if _msg and _allow_split:`），由父级统一发 1 条。⚠️ **判据：任何新加 `logger.warning` 前先搜 `warns_exactly_once` / 单告警点类护栏。**
> ⚠️ **未落地（记录以免重复排查）**：① 拆图**只在顶层分支分组**，不递归拆分支（单分支内仍超预算时该子图仍偏宽）；② 三图仅「加宽 + 省略号」，**长标签换行未做**（需同步改行数/块高，见 ②）；③ `compose_readability_warning` 在拆图后仍含「本图仍按完整数据渲染，不做删减」句式 —— 语义上仍成立（拆分≠删减），刻意不改以保 R60 字面契约。
> **详见** `app/services/ai/mermaid_architecture.py` 超宽自动拆图段注释 + 三图加宽常量处。

> **最近校准：2026-10-10（架构图规模预算守卫 + 劳动力图竖排 · R60）** —— **【R59 §9 遗留项①「限制单张架构图规模」安全切片 + ④「其它图表接竖排」评估落地】**（零新增依赖、新增配置项 **3 个环境变量（架构图叶/节点上限默认开 + 劳动力工期列竖排默认开）**、零数据迁移、**前端零改动**）：
> ① **【P1 · 架构图规模预算守卫（收口 R59 §9 遗留项①安全切片）】** AI 侧「单图节点 4~16 超则拆多张」约束已接线（content_checkpoint.py:1129，DLV-04），但服务端此前零检测。现加 ARCH_MAX_LEAVES=24 / ARCH_MAX_NODES=60（env 可配、非法回落默认、绝不抛），纯函数 _arch_counts 复用 _build_nodes 单一遍历口径（环/超深/非 dict 根安全返回），超限时 logger.warning 提示「建议拆分约 N 张」但**不拆图、不丢数据**（拆图需改 get_or_render 契约、跨 7 类图表高风险，列为独立授权轮）。护栏 tests/test_arch_size_budget_20261010.py 31 passed。
> ④ **【P1 · 劳动力图标签竖排（收口 R59 §9 遗留项④）】** 4 类候选图逐一测绘后**仅 mermaid_labor.py 真正受益**；comparison/timeline/layout 为宽矮几何且竖排破坏语义，不接竖排（防 churn）。底部折线图 x 轴工期列 col_w=lc_w/n_phases 随阶段变窄、<120*scale 时走 _draw_text_vertical_center（复用 mermaid_common.py:320），否则横排省略号；env LABOR_PHASE_VERTICAL=0 关闭。开启时 total_h 预留 _LABOR_PHASE_LABEL_H=130*scale 竖向带、图例下移互不重叠（防竖排溢出轴外）。左侧列/顶部工期列/图例的 [:8]/[:10] 硬切全部升级 _fit_text_with_ellipsis（不再静默丢字）。护栏 tests/test_labor_vertical_r60_20261010.py 22 passed。
> **A/B 行为锁**：① monkeypatch 翻转阈值断言超预算判定翻转；④ monkeypatch _draw_text_vertical_center 断言「窄列(8 阶段)→竖排、宽列(3 阶段)/关闭→不竖排」、_fit_text_with_ellipsis 断言左侧列宽度 label_w-8*scale。
> **基线对比**：图表关联簇 -k "chart or mermaid or architecture" **532 passed / 1 skipped / 1 xfailed / 0 failed（40.29s）**；R59 架构图回归 38 passed；图表渲染增强 26 passed。字节体检：触碰 mermaid_architecture.py/mermaid_labor.py 孤立 CR=0 / bare LF=0 / NUL=BEL=FF=0 / py_compile 通过。
> ⚠️ **【教训复刻 · CRLF】** 两轮均用**字节级补丁脚本**改 .py（读数→定行尾→锚点插入→写回字节），E 组判据固化；R56/R57/R58 的 \r\r\n/整文件 LF 化坑不再复现。
> ⚠️ **未落地（记录以免重复排查）**：① R59 未落地①「超宽自动拆图」真解仍待独立授权轮（改 get_or_render 契约，跨 7 类图表高风险）；② 对比表/时间轴/平面布置各自「长标签」应走加宽列/换行，非竖排，属独立小修、未做。
> **详见** app/services/ai/mermaid_architecture.py 预算守卫段 + app/services/ai/mermaid_labor.py 竖排守卫段注释。

> **最近校准：2026-10-10（架构图 L3 叶子层竖排版式 · R59）** —— **【文档 6 条增强项落地 5 条（1 条结构性不适用）+ 顺带修 1 个既有 P0 + 38 例新护栏 + A/B 8/8】**（零新增依赖、新增配置项 **2 个环境变量（竖排默认开启）**、零数据迁移、**前端零改动**）：
> ① **【P0 · 架构图环检测形同虚设 → 含环数据整张静默丢失（本轮最大发现）】** 旧 `_dfs` 用 `_unique_id()` 产出的 ID 做 `visited` 判重，而 `_unique_id` **保证任何两次调用返回值都不同**（撞车即追加 `#2/#3`）⇒ `nid in visited` **恒为 False**，环检测从未生效过一次；子节点引用祖先 dict 时无限递归 → `RecursionError` → 被 `render_mermaid_to_bytes` 兜底 `except` 吞掉，用户侧表现为「架构图块凭空消失」。现改用**对象身份** `id(node)` 判重 + 深度上限 `_MAX_DEPTH = 30`（`mermaid_architecture.py:192-203`），两者均按叶子截断且**不登记 `leaf_counter`**（与旧可观测行为逐字一致）。⚠️ **可复用判据：用「全局唯一化函数」的返回值做 visited 判重，逻辑上永远不成立** —— 判重键必须与唯一化目标相反（原始键或对象身份）。
> ② **【P1 · L3 叶子竖排（把 CSS 规格翻译成 PIL 绘制）】** 本仓架构图**不在前端渲染**：服务端 PIL 出 PNG，预览与 DOCX/PDF 共用同一渲染器 ⇒ **改一处三处生效**，文档增强项 5 自动满足、6（hover）**结构性不适用**。新增原语 `_draw_text_vertical_center()`（`mermaid_common.py:304`，逐字直立 + 多列自右向左 + 整组水平居中 + 每列垂直居中 + **容量收敛 `…` 截断**，绝不越界画字）；版式常量 `LEAF_VERTICAL_W/H/GAP = 96/232/12`（按 2.5× 超采样折算文档 60/200/8）。**只竖排 `depth ≥ 2` 的叶子**（`LEAF_VERTICAL_MIN_DEPTH`）—— 二级节点画成竖条会丢掉中间层横向标题的可读性。字号**刻意不改**（`LEAF_VERTICAL_FONT_SIZE = 15` 与横排同值）：真正收益是**画布宽度减半 ⇒ DOCX 按 16cm 栏等比缩放时同一字号的纸面相对尺寸放大约 2 倍**（实测 16 叶样张 −53.7% 宽 / 放大 2.16×；文档样张 11 叶 −52.0%）。
> ③ **【P1 · 布局三项口径收敛】** ⓐ 四份内联的「取子节点」别名判断收进 `_children_of()`（`:78`）**唯一出口**，且 `count_vertical_leaf_candidates()`（`:260`）**复用 `_build_nodes` 而非另写树遍历** —— 否则「计数说 8 个叶子、实际只竖排 6 个」的口径分叉必然出现；ⓑ DFS 返回值由「右边界（混进一个 gap）」改为 `(occupied_end, own_root_w)`，尾随间距由调用方经 `_gap_after(child_root_w)` 补（旧约定在**混合间距**下算错步距）；ⓒ `_assign_rows_y()`（`:239`）层高改「按层取最大高」，**各层等高时精确退化为旧公式** `depth*(NODE_H+V_GAP)+TOP_PAD` —— 这条退化性质被护栏锁死，是「横排回退零风险」的核心证据。契约：`_layout_architecture_tree(root, vertical=None)` 仍返回三元组、节点记录**只加一个 `vertical` 键**。
> ④ **【运维通道 · 2 个环境变量】** `ARCH_LEAF_VERTICAL=0` 整体关闭并**逐字回退旧横排版式**（`_OFF_VALUES` 认 `0/false/no/off/关闭`）；`ARCH_LEAF_VERTICAL_MIN=N` 调阈值（非法/负值回落默认 6，**绝不抛**）。自动切换是**严格大于 6**（6 叶保持横排、7 叶才竖排）。⚠️ **渲染缓存按 chart-code 键控，代码未变不会自动重刷** —— 上线需重启后端；紧急回退 = 设环境变量后重启，无需改代码或回滚数据。
> 护栏：`tests/test_arch_leaf_vertical_r59_20261010.py` **38 例 / 5 组**（A 版式判定 14 + B 布局几何 8 + C 竖排绘制 6 + D 契约 4 + E 字节体检 6）。**A/B 8/8**（M0 对照全绿；M1–M8 逐条定向失败且 `restore_sha_match=True`，含 M7「注入 `\r\r\n` 行尾污染」由 E 组抓住）。
> **基线对比**：全量 `pytest tests` **6910 passed / 17 skipped / 1 xfailed / 0 failed（468.59s，collected 6928）**；同树同环境仅 `--ignore` 本轮护栏文件 **6868 passed / 0 failed（collected 6886）**；`--collect-only` 双跑 **6928 / 6890 ⇒ Δ 恰为 38**，逐模块 diff 显示只有新文件计数变化、其余模块一项不差。图表关联簇 `-k "chart or mermaid or architecture"` 524 passed / 1 skipped / 1 xfailed / 0 failed；`ruff check app --select F821` 通过。字节体检：4 个触碰文件孤立 CR=0 / bare LF=0 / NUL=BEL=FF=0 / U+FFFD=0；`git diff --numstat` arch +234/−117、common +38/−0、renderer +1/−0（全为内容级增量）。⚠️ **【新发现 · 跨轮 passed 数字不可再做减法对账】** 同一工作树先后两次全量执行分别报 **collected 6917** 与 **collected 6928**（`--collect-only` 稳定 6928），已实验排除「仓库散落文件数」因素（放 5 个 `.txt` 前后恒为 6928）。⇒ 对账口径改为**同会话 `--collect-only` 双跑 + 逐模块 diff**。顺带解释历史不自洽：R58 自称「33 例新护栏」却记「+30 恰为新增数」，是同一现象、不是漏算。
> ⚠️ **【本轮最大教训 · A/B 抓出两个「看着覆盖、实为盲区」的护栏】** **M2（取消 depth 限定）与 M3（层高退回固定值）首轮变异后 38 例护栏全绿**。根因不是变异太弱，而是**测试数据把被变异的行为等价化了**：所有测试树的叶子恰好都落在最底层一行 ⇒「只有 L3 竖排」与「所有叶子竖排」结果相同、「按层取最大高」与「固定层高」在只有一层需要高行时结果相同。修法**不是加断言而是加树形**：引入 `_mixed_depth_tree()`（depth-1 叶子 + depth-2 叶子与内部节点混布 + depth-3 叶子），并对浅层树**显式传 `vertical=True`**（把「版式判定」与「版式渲染」拆开测，否则浅层树永远走不到竖排分支）；M2 的变异锚点还须**包含紧随的 `return` 行**，否则 `in` 粗判会匹配到别处同形片段。⚠️ **写布局/分支类护栏前先自问「是否存在一种输入让这个分支与它的对照组产生相同输出」—— 若有，那个分支就是零覆盖。**
> ⚠️ **未落地（记录以免重复排查）**：① **竖排不是万能解** —— 16 叶 / 4 组的极端宽树竖排后纸面字号仍仅约 1.3mm，真正解法是**限制单张架构图规模**（AI 侧约束叶子数或超宽拆图），本轮未做；② 竖排标签硬容量（当前 box 约容 15 字）超出即 `…`，**刻意不做「自动增高节点」**（层高反向影响整案画布，收益不稳定）；③ 文档 §3.3 `word-break: break-all` 未实现（中文逐字堆叠天然等价，英文/数字串占满整列实测可接受）；④ **其它数据类型图表（劳动组织 / 对比表 / 平面布置 / 时间轴）未接竖排** —— 原语已放 `mermaid_common` 供复用，但各图宽度瓶颈与语义不同，需按图单独评估。
> **详见** `docs/架构图L3叶子层竖排版式落地与验证_R59_20261010.md`（渲染栈校正表 / 6 条增强项逐条落点 / CSS→PIL 翻译 / 环检测 P0 全附 file:line / 基线与 A/B 表 / 实测几何与耗时 / 局限清单）。

> **最近校准：2026-10-10（标书智能体方法论四项未落地项收口 · R58）** —— **【R57 §6 的 4 条未落地项全部落地 + 33 例新护栏 + A/B 11/11】**（零新增依赖、新增配置项 **3 个全部默认 True**、零数据迁移）：
> ① **【P1 · F3 提纲「一次性失败 → 切换分步」兜底边】** 短方案（≤5 万字）一次性直出失败（异常/空目录/节点全非法）旧行为是 `finish_task failed` + error 事件 + return，用户只看到「请重试」且**无任何部分成果可挽救**。现加 `outline_oneshot_fallback_stepwise`（默认 True）：一次性路径包进 `while _one_shot_ok:`，失败点 `break`，分步块由 `else:` 改为 `if use_stepwise or not _one_shot_ok:`（`sse_handlers.py:3656/3737`）。⚠️ **刻意不抽取分步代码**（内联 366 行）—— `test_outline_module_fixes_20261007.py:97`（`_attach_quality_report(` 计数==2）、`test_outline_workflow_logs.py:111/120` 三条源码级护栏会拦。三个必须做对的点：配额/认证类错误**不切**分步（`_is_quota_error`，否则十几章全白烧）；切档重置 `last_p/nodes/sub_done/sub_total` 并把 `stepwise` 置真（否则进度卡在 55%）；清空 `_partial_holder` 防脏成果落库。
> ② **【P2 · F4 正文图表「配额感知」（不做预编排）】** 真正的缺口不是「预编排指定画什么」（那必须读 `chart_predictions` 的规划字段 = 陈旧结论陷阱，`sse_handlers.py:4645-4651` 已刻意删除），而是**反向通知缺失**：程序会在生成后按上限**删**图，AI 写正文时却完全不知道配额还剩多少 → 前半本用光 3 张 gantt、后半本再画必被删。现新增纯函数 `build_chart_quota_hint()`（`_chart_pipeline.py:122`），每章生成前统计真实占用并追加到**任务差异段**（不进稳定段，不破坏前缀）。只统计不规划、不指定画什么、不动硬限额；并发下偏乐观属**软提示**，代价远小于无提示。
> ③ **【P1 · F5 事实分段注入（含修复一个 P0）】** R57 把事实放进 user 第①段「整案稳定上下文」，但 `facts_text` 是按 `_filter_facts_rows(rows, leaf)` **逐章**算的 → 所谓稳定段每章都变，**Prompt Cache 前缀根本无法命中，F1 收益被抵消大半**。现拆为「整案核心（通用组，恒定 → 稳定段）+ 其余按章命中（→ 任务段）」（`facts_builder.py:57/87` + `sse_handlers.py:5411`）。**信息零丢失**（两集合相等，只是分处两段）；`_sec_facts` 校验口径刻意不改（更宽）。两阶段 AI 方案**不做**：每章 +1 次调用，且漏召回会直接变成正文【待补充】（本仓红线）。
> ④ **【P2 · F6 bid_analysis._repair_json 统一化】** 旧实现 1 轮 + 贪婪正则兜底、无结构校验、提示词硬编码（DB 改不了）、零指标。现委托 `collect_json_response_with_chat` 并**注入 `ba.chat_with_fallback`**（直接用 `collect_json_response` 会让 6 处 monkeypatch 失效）。必须守住：返回**模型原文 str**（下游全是 str 契约，`json.dumps` 会打断逐字断言）、**永不向上抛**（否则被 SSE 放大成整项 3 次重跑）、失败兜底必须是 **`"{}"`**（失败哨兵；`""` 会被判「不缺失」→ 假绿）、`repair_key=GENERIC_REPAIR_KEY` 具名常量、`BID_JSON_REPAIR_RETRIES=1` 与旧成本相等。
> 护栏：`tests/test_r58_closeout_20261010.py` **33 例 / 5 组**（F3 7 + F4 7 + F5 5 + F6 8 + E 字节体检 3）。**A/B 11/11**（M0 无变异对照全绿；M1–M10 逐条定向失败且 `restore=True`）。
> **基线对比**：全量 `pytest tests` **6838 passed / 17 skipped / 1 xfailed / 0 failed（387.10s）** —— 对比 R57 基线 6808 passed，+30 恰为本轮新增护栏用例数，**零新增失败**。`ruff check app --select F821` 通过。字节体检：本轮触碰 7 个文件孤立 CR=0 / bare LF=0 / NUL=BEL=FF=0 / U+FFFD=0 / py_compile 通过。
> ⚠️ **【本轮最大教训 · 行尾坑二次踩中，形态不同】** R57 是「编辑工具二次处理行尾」→ `\r\r\n`；R58 是补丁脚本用 `Path.read_text()` —— **它默认开启 universal newlines，把 CRLF 读成 `\n`**，写回后**整个文件被改成 LF**（`git diff` 判全文重写，真实改动无法 review）。**唯一可靠做法：`read_bytes` / `write_bytes`**，改完立即体检。判据已固化（R57 + R58 两个 E 组）。
> ⚠️ **【第二条教训 · `in` 判据在多处同形代码前是假护栏】** A/B M10 首轮变异后护栏**全绿**，暴露「合并路径哨兵」零覆盖；补判据后又发现 `_run_single_item` 里有**两处同形 `result_text = "{}"`**，`in` 粗判抓不到 —— 改为「合并失败日志之后必须紧跟哨兵」的**顺序判据**才真红。断言必须带位置/顺序/计数约束。
> ⚠️ **运维**：需重启后端；① 短方案一次性失败会自动切分步（新增 `outline_level1` 等调用，可开关关闭）；② 正文新增「本章图表配额」软提示；③ 事实现分两段下发（内容集合不变）；④ `/ai/stats` 的 `bid_analysis` 失败数会比历史**多**（原实现是审计缺口，不是新增故障）。
> **详见** `docs/标书智能体方法论四项未落地项收口_R58_20261010.md`（四项决策与取舍 / 全附 file:line / 基线表 / A/B 表 / 两条教训）。

> **最近校准：2026-10-09（标书智能体六篇方法论对齐 · R57）** —— **【2 处真实缺口落地（F1/F2）+ 3 项回归自行撞出并修复 + 36 例新护栏 + A/B 8/8】**（零新增依赖、新增配置项 **1 个默认 True**、零数据迁移、返回契约加法式）：
> ① **【P0 · F1 正文提示词前缀稳定化（《标书智能体（四）》核心）】** 正文首轮 messages 长年是「1 条逐章变化的 system + 1 条 user 大串」，且 user 串里**最重的**施工内容/全局事实/知识库被排在「当前章节编号/标题/目标字数」等逐章差异**之后** → 同方案每换一章前缀整体失效，同一份上下文反复全价计费（全仓此前只在 `bid_analysis` 有缓存预热）。现新增 `content_stable_prefix_enabled`（**默认 True**）：逐章变量从 system 剥离（注入空串、变量照传以免占位符残留或整行丢弃），其真实值经 `build_chapter_extras_block` **原样**落到最后一条 user；user 拆为「整案稳定 / 章节树 / 任务差异」三条，同方案各章共享最长公共前缀。配套纯函数 `resolve_system_variables` / `build_chapter_extras_block` / `build_chapter_layered_messages`（`services/content_runtime.py:112/138/165`），开关关闭即逐字回退。
> ② **【P1 · F2 一致性定点编辑接入统一 JSON 修复通道（《标书智能体（五）》§四）】** `consistency_edits.collect_repair_edits` 此前自己 `extract_json+json.loads`，**一次机会**：模型多一句解释就放弃定点编辑 → 回落整章重写（顺带改写无关段落；>12000 字超长章**禁止重写**，等于永远修不了）。现把 `collect_json_response` 的编排下沉为 `collect_json_response_with_chat(chat_fn, …)`（**chat 来源可注入**），定点编辑复用完整修复链路（1 轮定向修复），成功路径零额外调用。
> ③ **【3 项回归均为跑回归撞出，不是审出】** ⓐ 用 `**_sys_vars`/字典推导展开 render kwargs → 该调用点被 `TestRenderDynamicBlindspots` 判为**动态盲区**，漏传变量从此测不出来；已改回显式关键字 + 逐条 Subscript 赋值，**盲区由 2 条降到 0**（并按护栏设计同步注销快照条目）。ⓑ 修复轮使定点编辑多一次调用，`TestRepairRewriteHardCap` 裸计数断言失效；保留增强（一次定点修复成本 ≪ 整章重写），把测试升级为**语义判据**。ⓒ 为做语义判据，定点编辑 scene 拆为独立 `consistency_repair_edits` 并登记 `KNOWN_SCENES` —— ⚠️ **scene 必须以字符串字面量书写**（用常量传入会被 AST parity 护栏判「登记了却无调用点」）。
> 护栏：`tests/test_prompt_prefix_stability_r57_20261009.py` **36 例 / 6 组**（A 分层 10 + A2 变量决策 5 + B 接线 7 + C 定点编辑 7 + D 统一通道 4 + **E 源文件字节体检 3**）。**A/B 8/8**（M0 无变异对照全绿；M1–M7 逐条定向失败且 `restore_sha_match=True`，含 M7「注入 `\r\r\n` 行尾污染」）。
> **基线对比**：全量 `pytest tests` **6808 passed / 17 skipped / 1 xfailed / 0 failed（402.03s）** —— 对比 R56 基线 6725 passed / **5 failed**，**零新增失败**；关联 `-k "content or consistency or repair or json or prompt or outline or facts or scene"` 3671 passed / 12 skipped。字节体检：本轮触碰 6 个文件孤立 CR=0 / bare LF=0 / NUL=BEL=FF=0 / U+FFFD=0 / py_compile 通过。
> ⚠️ **【本轮最大教训 · R56 §F6 重犯】** 用文本工具编辑 CRLF 仓库的 `.py`，4 个文件**每行**被写成 `\r\r\n`（loneCR 由 0 变成行数），自救脚本又把孤立 `\r` 连同换行吃掉 → 4 个文件 `py_compile` **全部挂掉**，最终靠 `git checkout` + **字节级重放脚本**恢复。**CRLF 仓库的 `.py` 一律用字节级补丁脚本改**（锚点 count==1 + 改后体检）；E 组判据与 A/B M7 已固化。
> ⚠️ **【第二条教训】** 追求代码简洁用 `**kwargs` 展开 render 参数，会把该调用点**踢出静态检测范围**（漏传变量再也测不出来）——「可静态判定」是设计目标，不是风格偏好。
> ⚠️ **【第三条教训】** 逻辑埋在 SSE 闭包里（如本轮的 system 变量决策）时，A/B 变异它**没有任何测试会红** —— 护栏看着覆盖、实为盲区。已下沉为纯函数。
> ⚠️ **运维**：需重启后端；行为变化 ① 正文首轮 messages 2 条 → 4 条（可开关瞬时回退）；② 定点编辑首次不合规时多一次定向修复；③ 新增场景 `consistency_repair_edits`。
> ⚠️ **未落地（以免重复排查）**：① 提纲「一次性失败 → 切换分步生成」兜底边（现只有事前按字数分叉，短方案硬失败；分步代码内联在 generate_outline 闭包约 370 行，抽取需单独一轮）；② 正文「预编排」（文档（六））—— 本仓**刻意删除过**（`sse_handlers.py:4642-4648` 实证：预查 chart_predictions 会把陈旧结论当新规划），现「图表与正文一体生成 + 落库前代码限额」不是缺陷；③ 全局事实两阶段按需注入（现为规则预筛，两阶段需每章多一次 AI 调用，且 F1 已让事实进入可复用前缀，收益被稀释）；④ `bid_analysis._repair_json` 仍是自建劣化版，待单独一轮。
> **详见** `docs/标书智能体六篇方法论对齐_增强落地与验证_R57_20261009.md`（8 篇对标表 / F1+F2 全附 file:line / 3 项回归 / 基线表 / A/B 表 / 三条教训）。

> **最近校准：2026-10-09（审核预检 R55 遗留收口 · R56）** —— **【2 处真实缺陷修复（F2c + F6）+ 6 例新护栏 + A/B 4/4 + 1 项证伪】**（零新增依赖、**零新增配置项**、零数据迁移、返回契约全为**加法式**）：
> ① **【P1 · F2c：/confirm 重算解析不带 section_id（收口 R55 遗留③）】** 重算分支逐条 `_resolve_finding(db, scheme_id, rid)`，同规则跨章时**拿别章的 finding 重算本章正文**。现先按**本章**精确解析 → 404 才回退规则级（保 CON-01 族「整篇型 finding 无 section_id」的历史可用性）→ 回退命中的行若仍属别章则**拒绝越章改写**并记 `skipped`（detail 含「不属于本章」），正文逐字节不动。护栏 `TestSectionPreciseResolve` 2 例；A/B 摘除 `sid` → E1 定向 FAIL、C1/E2 仍 PASS、`restore_sha_match=True`。
> ② **【P0 · F6：R55 的字节补丁已把生产前端文件写坏（本轮最大发现）】** R55 §6.6 只把「文本编辑工具会展开转义字面量」当**工具教训**记着，本轮字节体检发现它**已经落在 `frontend/src/api/index.ts` 里**：`parseSseEventBlock` 的 `block.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n")` 与 `const separator = /\r\n\r\n|\n\n|\r\r/;` 两条语句中的转义成了**裸 CR/LF 字节**（工作树 CR=6、3 处孤立 CR；HEAD blob 为 0）。JS 正则字面量内不允许行终止符 → **SyntaxError，api 模块整体编译不过，SSE 正文进度流全断** —— 下次前端构建必炸，而本机零 Node、tsc/vitest 一条没跑，全绿。修法：按 HEAD 的**转义文本**形态逐字节还原（锚点 count==1 + 还原后全文 CR 必须为 0 才写盘）。
> ③ **【加固 · 前端源码字节体检】** 新增 `TestFrontendByteIntegrity` 4 例：6 个被改动前端文件 ①无孤立 CR/无 NUL/BEL/FF/无 U+FFFD；②SSE 两条语句**转义文本各在位 1 次**且 api 全文 CR=0；③收尾行必须像收尾行（补丁截断形态）；④新增 describe 各 1 次 + 用例总量不缩水（`it(` ≥20/30、`expect(` ≥80）。**A/B 3/3** 注入原损坏形态全部定向失败，另跑「无变异对照」防恒红。⚠️ **A/B 测出判据边界（已写进护栏 docstring 防后人「去重」）**：`count(CR)==count(CRLF)` 的孤立 CR 判据**抓不到**「展开成 CRLF 对」的损坏（M1 中它 PASS、转义文本锁 FAIL）—— 两条互补而非冗余。
> ④ **【加固 · 前端可执行性自动闭环（收口 R55 遗留①）】** 新增 `TestFrontendRealExecution`：`shutil.which("node")` 存在就真跑 `tsc --noEmit -p tsconfig.json` + `vitest run` 本模块两个前端测试文件，缺失则 skip（当前 **2 skipped**）—— 装了 Node 即自动生效，不再靠文档里「记得手动跑」。同时更正 R49 记录：**本机 Node 现已彻底不可用**（`%LOCALAPPDATA%\nvm` 整目录已删、`C:\nvm4w\nodejs` 是指向已删 v25.8.0 的**悬空符号链接**，`node`/`npx`/`npm`/`deno`/`bun` 全无）；CI 侧 `.github/workflows/ci.yml` frontend job（Node 22）本就跑 `npm run lint` + `npx tsc --noEmit` + `npm test`，故新增用例**推送即被执行**，F6 这类 SyntaxError 也归 CI 抓得住。
> ⑤ **【证伪 · 免后人重复排查】** 「R55 把 4 个前端文件从 LF 改成 CRLF」**不是缺陷**：`core.autocrlf=true` 且无 `.gitattributes` → 检出 LF→CRLF、提交 CRLF→LF，工作树与 HEAD blob 行尾不同是常态。`git diff --numstat` 佐证（ReadinessDashboard.tsx 只 +17、audit.ts +20/-1、api/index.ts +46/-27，全是内容级增量，无整文件重写）→ **刻意不做「统一行尾」改动**（纯 churn）。唯一有效签名是**孤立 CR**，已进 ③ 的护栏。
> ⑥ **【运维 · R55 遗留②：后端已重启】** 旧 uvicorn **PID 23700 仍在监听 8000** —— R55 在沙箱内跑 `stop_backend.ps1` 是**静默失败**（沙箱无权 kill，脚本 `$ErrorActionPreference='SilentlyContinue'` 把失败全吞，日志照常打「port(s) released」）。提权重跑 `stop_backend.ps1` → `start_backend.ps1`（单进程无 --reload）→ **新 PID 19468，`/api/v1/health` 200 / 718ms**。⚠️ 运维教训：这类脚本的成功文案不可信，务必用 `Get-NetTCPConnection -LocalPort 8000 -State Listen` 复查端口是否真空。
> 护栏合计：`tests/test_review_autofix_closeout_r55_20261008.py` **17 → 23 passed / 2 skipped（25 例 / 6 组：A 上限 4 + B 粒度 4 + C 非前缀 2 + D parity 7 + E 精确 2 + F6 字节 4 + I 真跑 2）**。
> **基线对比**：关联选择集 `-k "preflight or readiness or review or autofix or audit or compliance"` **909 → 915 passed / 5 skipped / 0 failed（76.83s）**（909 + 2 F2c + 4 F6 = 915，+2 skipped 即 Node 门控，数值可精确核对）；全量 `pytest tests` **6725 passed / 5 failed / 19 skipped / 1 xfailed（431.54s）** ＝ R55 全量基线 6719 + 6（F2c 2 + F6 4）+ 2 skipped，5 项失败与 R54/R55 记录**同集零新增**（1 项 CON-06 既有 + 4 项 J 盘 NTFS ACL 写 PNG/.ruff_cache 被拒）。字节体检：本轮触碰的 9 个文件**孤立 CR=0 / NUL=0 / BEL=0 / U+FFFD=0 / AST 或结构检查通过**，异常文件数=0。
> ⚠️ **【本轮最大教训】** 「补丁脚本会把转义展开成真实字节」这件事，R55 **已经观察到两次**却只落成文字教训，损坏就此留在生产文件里一整轮，直到本轮做字节体检才发现。**观察到的缺陷形态必须立刻变成判据**（这次是「转义文本在位 + 全文 CR=0」两条断言），否则教训本身会变成新的盲区。同类：纯静态 parity 锁只验「符号在位」，与「字面量是否被展开」**正交** —— 缺 Node 时不能只靠 parity。
> ⚠️ **未落地（记录以免重复排查）**：① `_r56_balance_probe2.py` 的 TS/TSX 结构体检器**不进护栏** —— 控制集 117 文件仍有 7 处假阳性（正则字面量内含 `/`、JSX 文本引号），零误报得写真正的 JS lexer，成本/收益不划算；语法正确性交 CI 的 tsc。② `api/index.ts` 与 HEAD 一样**无文件末尾换行** —— 已核到底是**全仓常态**（`frontend/src` 8/117 个 .ts/.tsx 如此）且 `eslint.config.js` **未启用 `eol-last`**，CI 三条（lint/tsc/vitest）均不报 → 不属缺陷，补齐应与另 7 个文件同一轮做，单改一个是纯 churn。③ 本地跑 vitest/tsc 仍待用户装 Node（护栏已就位，装完自动生效）。
> **详见** `docs/审核预检模块收口_修复落地与验证_R55_20261008.md` §7（R56 遗留收口全记录：F2c / F6 / 证伪 / 运维 / 基线对账表）。

> **最近校准：2026-10-08（审核与预检模块收口 · R55）** —— **【5 处真实缺陷修复（F1–F5，其中 F2 拆 2a/2b）+ 17 例新护栏 + A/B 5/5】**（零新增依赖、零新增配置项、零数据迁移、返回契约全为**加法式**）：
> ① **【P1 · 批量路径绕过章节上限 + 假 skipped:0】** 配置护栏 `review_autofix_max_sections`（默认 10，0=不限）只在单条 `_fix_each_section` 生效，`stage_fixes`（触达章节最多的批量入口）零截断且 `stats` 硬编码 `"skipped": 0` —— 把「没截断」伪装成「截断后无跳过」。修法：与单条路径**同判据**（问题最多的章节优先）截断，超出章节记入新键 `skipped_sections`/`max_sections`、`stats.skipped` 如实计数 + `logger.warning`；router `/stage` 空返回补齐同名字段。
> ② **【P1 · /confirm 接受粒度与 UI 勾选粒度分叉】** accept/reject 只按裸 `rule_id` 匹配，而 stage 会把无 section_id 的 finding（CON-01 等）按定位归到多章、产生多条同 rule_id items —— 前端勾任意一章 = 接受该规则**全部章节**，「只接受这一章」在协议上无法表达。修法：条目新增复合键 `rule_id|section_id`（`_item_key` 唯一出口），裸 rule_id 旧语义**逐字不变**；`accepted` 改条目计数；`skipped` 明细新增 `section_id`。
> ③ **【P1 · 非前缀检测在生产路径整体失效（本轮最大行为修复）】** 旧 confirm 先把 `by_section` 预过滤成「只剩被接受项」再喂 `_merged_after_if_prefix` —— 「未接受项出现在被接受项之后」的分支**永不出现**（只有静态单测能触发），接受链 [A,B,C] 中的 {C} 时直接取 C.after（链式累积、含被用户拒绝的 B 的改写）→ R39 越权写入在第 4 处复发。修法：按章喂**完整 repaired 链**，非前缀 → None → 走「以当前库内正文重新链式改写、只应用被接受项」分支（该分支此前为死代码）。
> ④ **【P2 · 批量契约三处前端静默（BatchFixModal）】** stage 上限信息零消费、「一键修复全部」实为修前 10 章且零提示；confirm 的 `skipped`（R39 起就有）整段丢弃 →「以为修了、实际没修」；payload 按裸 rule_id 下发。修法：`acceptedKeys` 复合键下发 + `skippedList` 消费 + 上限/跳过双 toast + 清单上方/完成阶段两个 Alert；`audit.ts` 补可选字段（旧后端零影响）。
> ⑤ **【P2 · stale_ai_sources 前端零消费（收口 R50 未落地③）】** 后端早已回传、全仓 grep 零出现 —— 陈旧 AI 结论被跳过后用户不知「要重跑哪个 AI 检查」，对应维度问题永久静默漏报。修法：`ReadinessOverview.stale_ai_sources?` + 告警 Alert，来源中文名**复用 `SOURCE_LABEL` 单一映射**（不另写一份中文表）。
> ⑥ **【P3 · REST 拦截器不认 422 数组形 detail】** FastAPI 422 的 `detail` 是 `[{loc,msg,type}]` 数组，SSE 侧 `describeSseHttpError` 早已处理、REST 侧最后一处漏网 → 用户只见 "status code 422"，缺哪个参数在响应体里却看不到。修法：新增 `detailArrayToText()`（scopeCn 口径唯一实现），拦截器与 SSE 共同委托。
> 护栏：`tests/test_review_autofix_closeout_r55_20261008.py` **17 例 / 4 组**（A 上限 4 + B 粒度 4 + C 非前缀端到端 2 + D 前后端接线 parity 7 —— Node 缺失沿 R54 源码级锁）；前端 `BatchFixModal.test.tsx` 既有 4 处断言升级复合键 + 新增 3 例、`ReadinessDashboard.test.tsx` 新增 2 例（待装 Node 后 vitest 执行）。**A/B 反向验证 5/5**（M1 完整链退预过滤→C 组两例定向失败 / M2+M2b accept、reject 复合键分别摘除 / M3 上限摘除，全部 restore_sha_match=true）。
> **基线对比**：关联回归 `-k "preflight or readiness or review or autofix or audit or compliance"` **892 → 909 passed / 3 skipped / 0 failed（72.54s）**（892+17=909 可精确核对，零回归）；autofix 五文件簇 75 passed；新护栏+既有批量契约合跑 27 passed。
> ⚠️ **【本轮两条工具级教训】** ① 编辑含 `\n`/`\x07` 转义字面量的 TS/测试文件时，文本编辑工具会把**未触及区域**的转义展开成真实换行（api/index.ts 两次中招、25 处语法错）—— 前端文件一律改字节级 Python 补丁脚本（锚点 count==1 强校验 + CRLF 自适应），磁盘真相以 Python 读字节为准（IDE 缓冲可能陈旧）。② `routers/review_autofix.py` 实测 **CRLF**（524 CRLF/0 裸 LF），与 R54 记录「router 原生 LF」不符 —— 多行锚点务必按文件真实行尾拼装（A/B M1 首跑即因 `\n` 锚点 count=0 被 SKIP）。
> ⚠️ **运维提醒**：需重启后端进程加载新代码；行为变化披露：>10 章的批量修复本轮只处理「问题最多的前 10 章」（上限本就是设计意图），其余章节 UI 明示可再次点击；`/confirm` 响应 `accepted` 由规则命中语义改为条目计数。
> ⚠️ **未落地（记录以免重复排查）**：① 前端新增 5 例组件测试本机零执行（Node 完全缺失）；② confirm 重算分支 `_resolve_finding` 仍不带 section_id（重算复用 findings 归章更严谨，独立小项）；③ 「本应有某要素却没有」缺失型检测不做（沿 R49/R54 结论）。
> **详见** `docs/审核预检模块收口_修复落地与验证_R55_20261008.md`（模块现状 / F1–F5 全附 file:line / 基线表 / A/B 表 / 兼容性说明）。

> **最近校准：2026-10-08（全局事实模块深度收口 · R54）** —— **【7 处真实缺陷修复（F1–F7）+ 3 项缺口补齐 + 47 例新护栏 + A/B 11/11】**（零新增依赖、**零新增配置项**、零数据迁移、返回契约全为**加法式**）：
> ① **【P1 · 冲突裁决换值后未重派生维度】** `resolve_conflict` 是第 4 条「改事实取值」写路径，却漏了维度重算门 —— 旧 `chapter`/`fact_attr` 被当权威一直带下去（读路径一律「库值优先」）。修法：「判据 + 应用」收敛进 `_rederive_dimension_columns` 单一出口，与条目级更新、分组重建三条路径共用；SET 片段与占位符**同源生成**（先追加维度赋值再补 WHERE 的 fact_id，避免「N 个 ? vs M 列」静默错位）。值不变（前端「保留当前值」）时不覆盖人工归类。
> ② **【P2 · `/chapters` 把事实名当取值】** `_fact_dimension_fields(r, r["name"], r["name"], …)` → **同一行**在 `GET /global-facts` 判 `quantitative`、在 `/chapters` 判 `qualitative`（覆盖率口径与分类视图双双分叉）；且行内不写 `name` 键，`chapter_field_completeness` 看不到事实名 → 本端点最核心产出 `covered_fields` 偏低、`missing_fields` 虚高。改走 `_fact_name_value` 统一解名/值，`_chapter_stats_for_items` 由 2 次调用收敛 1 次（4 遍全表扫描→2 遍），响应补 `by_fact_attr`/`by_source_kind`。
> ③ **【P2 · `/adjust` 作用域排除项目共享事实 + 丢弃零可观测】** 内联作用域少写「共享半句」→ 项目共享事实不可见不可改、`add` 能造出同名第二份；被安全校验丢弃的操作**完全静默**（用户以为 3 条都改了）。现退回 `_fact_scope_where` 单一谓词，`_validate_adjust_ops` 新增 `existing_keys=`/`ignored_out=`（默认 `None`，既有调用方逐字不变），丢弃理由收敛**封闭 6 枚举**（`op_not_object / unknown_fact_id / no_effective_change / missing_name_or_value / duplicate_fact_key / unknown_op`），响应加 `ignored`/`ignored_count` + `logger.warning`。
> ④ **【P3 · `/adjust` 非原子】** G-04 注释宣称「整批原子提交」，实际每条 update 内部各自 commit → 中途 500 留下**半截生效**。改为循环内 `commit=False`、末尾统一 commit / `except: rollback`；新关键字参数默认 `True`，其余调用方零变化。
> ⑤ **【P2 · 提取只按方案层去重】** `persist_extraction` 只查方案私有行 → 项目共享层已确认的同 `fact_key` 不参与去重，**两份取值不同的同名事实同时满足注入门控** → 正文自相矛盾。新增 `shared_protected_by_key`（只读共享层 + `_is_protected`）：同值→清 `is_stale`、异值→回写冲突候选到共享行，**绝不用新值覆盖用户已确认值**。⚠️ **刻意未改**：删除候选 `non_protected_ids` 仍只从方案私有 `existing` 计算 —— 否则一次单方案重提取会删掉项目共享事实、波及同项目其它方案。
> ⑥ **【P3 · 同一判据多副本】** 读作用域 SQL 在 `global_facts.py` 内有 **7 份内联副本**（`list_facts`/`_load_fact_rows`/`adjust_facts`/`ack_fact_stale_batch`/`clear_all_facts`×2/`batch_resolve`×2），粗体事实行正则有 2 份 → 收敛为 `_FACT_SCOPE_PREDICATE` + `_fact_scope_where` + `_fact_name_value` 单出口，片段与旧字面量**逐字一致**、行为零变化。
> ⑦ **【P2 · `scheme→project` 反查 9 副本 + 位置索引取值（本轮最大发现）】** `SELECT project_id FROM schemes WHERE id=?` 在本文件**逐字重复 9 次**，取值清一色 `row[0]`，另有 2 处「为取 project_id 再查一次 global_facts」的二次 SELECT。生产 `aiosqlite.Row` 键位皆可 → 线上不报错；**一旦行形态换成 Mapping，`prow[0]` 抛 `KeyError: 0`，而这些反查全在 fail-soft 分支里**：① 吞成 `""` → 项目共享事实从注入/目录/导出/预检作用域整体消失；② `_invalidate_fact_scope_cache(db,"","")` 因 `not project_id` **静默 return** → 项目下全部方案 `facts_updated_at` 不推进，「事实已变更」标记永久停在旧值（用户看过期正文零提示）。修法：`facts_extractor` 新增 `row_field(row, key, index=0)`（Mapping 按键名 / Row 键名优先 / 序列按位兜底 / **绝不抛**）+ `SCHEME_PROJECT_ID_SQL` 常量；`global_facts` 新增**三态**出口 `_scheme_project_id(db, scheme_id) -> str | None`（`None`=方案不存在→404、`""`=存在但未绑定项目→作用域退化不是错误、游标 None→**503** 按本文件既有 R13 约定而非降级 404），9 处副本全改走出口、2 处二次 SELECT 复用已在手的首查行。
> 护栏：`tests/test_facts_derivation_closeout_r54_20261008.py` **47 例 / 7 组**（A 重派生 6 + B 两端点同行同判 6 + C `/adjust` 作用域与原子性 7 + D 跨 namespace 去重 5 + E 单一出口 6 + F 前后端枚举 parity 2 + G 行形态/三态/503/失效链端到端 15）；`test_facts_deep_audit_r45_20261006.py` **+2 例**。**A/B 反向验证 11/11**（M1–M11 覆盖 F1–F7，每条 `anchor_count: 1` → 定向失败 → `restore_sha_match: true`，还原后 47 passed）。
> **基线对比**：facts 选择集 `-k "fact or Fact"` **849 → 898 passed / 5 skipped / 0 failed（83.03s）**（849 + 47 + 2 = 898，数值可精确核对）；全量 `pytest tests` **6702 passed / 5 failed / 17 skipped / 1 xfailed**，复跑逐字一致、失败集合与修复前**完全相同**（零新增失败）。5 项失败已核因：4 项 = J 盘 NTFS ACL 损坏（`[Errno 13]` 写图表 PNG / `.ruff_cache` 被拒，同一份代码在 `D:\wt_r54base` 工作树**通过**），1 项 = `test_con06_finding_actually_reaches_the_report` **HEAD 工作树同样失败**（正文生成 CON-06 接线域，非本轮、非事实模块）。字节体检：`global_facts.py` CRLF=4183/裸 LF=0、`facts_extractor.py` 原生 LF=3002、新测试文件 CRLF=904/裸 LF=0，均无 BOM、U+FFFD=0、AST OK。
> ⚠️ **【本轮最大教训】** F7 不是审出来的、是**全量跑撞出来的** —— 一个不相关模块（正文生成 CON-06）的失败日志里露出 `KeyError: 0`，顺藤摸到 9 份副本。**「位置索引 + fail-soft 分支」是 lethal 组合**：异常被吞成空值、空值又恰好是「什么都不做」的合法入参，于是危害面从「一次报错」升级为「整条失效链静默停摆」。此类判据务必配**行形态无关的取值出口** + **Mapping 行形态的专项用例**（生产 `aiosqlite.Row` 下新旧行为一致，只有 dict 行才能判别 F7）。
> ⚠️ **运维提醒**：需重启后端进程加载新代码（当前 uvicorn PID 23700 占 8000，端口无监听时 `start_all.bat` 即生效）；本轮零数据迁移、无补列。前端 F3 接线（`FACTS_ADJUST_IGNORE_REASONS`/`_LABELS`、`normalizeFactsAdjustPlan`、告警 `Alert`、toast 文案）**已写但未执行验证** —— 本机 Node.js 完全缺失（`node`/`npx` 不在 PATH 且 `%NVM_HOME%`/`%NVM_SYMLINK%` 是未展开占位符），故 F 组改为在 Python 侧做源码级枚举 parity 锁；装 Node 后跑 `npx tsc --noEmit` + `vitest`。
> ⚠️ **未落地（记录以免重复排查）**：① **跨模块**的同一条 `SELECT project_id FROM schemes WHERE id=?` 仍有 14 份副本（`placeholder_inventory`/`scheme_fingerprint`/`bid_analysis`/`export`/`review`/`sections`/`sse_handlers`/`upload_outline`），作用域谓词在别处仍有 5 + 4 份 —— 本轮只在**事实模块内**收口，跨 6 文件改公共出口需单独回归；② 「某要素该有事实却没有」的检测不做（判据只存在于提示词散文，抽成常量即落入多副本反模式）；③ 前端「哪几条事实变了」缺逐条可点清单；④ 完备度不区分必填/可选缺失（需产品口径）；⑤ `is_safety_critical_name` 的 `value` 形参**已验证是死参而非闸门漏洞**（`_is_safety_critical` 只按名称/归一化键判定，docstring 写明移除原因），`list_facts` 传值 vs `batch_resolve` 传 `""` 结果必然一致 —— 属签名卫生，不是缺陷；⑥ `_facts_for_adjust_prompt` 仍用旧解析器喂 AI（改动会变更 AI 可见文本，需配提示词回归）。
> **详见** `docs/全局事实模块收口_修复落地与验证_R54_20261008.md`（24 端点清单 / 缺口 G1–G6 / 技术债 T1–T7 / 修复 F1–F7 全附 file:line 与 A/B 表）。

> **最近校准：2026-10-07（R50 遗留收口 + 深层伪标题编号 + 标准库 openstd 实证 · R51）** —— **【3 处遗留修复 + 1 处行为修复 + 16 例新护栏】**（零新增依赖、零数据迁移）：
> ① **【P1 · consistency_conflicts 陈旧判定接入（R50 未落地①）】** 一致性扫描冲突此前**不参与**陈旧排除 —— 正文已变后旧扫描的未解决冲突仍按「最近一批」计入就绪度评分。修法：指纹实现下沉为 `services/scheme_fingerprint.content_fingerprint` **单一事实源**（`compliance._content_fingerprint` 改为委托，既有测试零改动；避免「同一判据两处实现」再次分叉）；`consistency_conflicts` 新增 `content_fingerprint` 列（DDL + `_migrate` 幂等，历史行空串 fail-open）；`persist_conflicts` 落指纹、总检聚合比对跳过 + 回传 `stale_ai_sources`。
> ② **【P1 · 深层点分伪标题（≥4 段）识别与重排】** AI 在正文写 `7.3.3.1.1 材料计划` 式伪标题时旧实现只识别到 3 段（`_RE_HEADING_NNN`），更深的行被当普通段落 → 既不参与编号规范化（父章节重排后错误前缀残留成稿）也不按标题样式渲染（生产 docx 实证多处）。新增 `_RE_HEADING_DEEP`（4~7 段）+ `_detect_plain_heading` 按「段数 + 1」映射层级（N.N.N.N→5…封顶 7），重排按展示域前缀（存储 "3.2" → 展示 "2" → 子标题 "2.1"），与既有 3 段同算法、幂等。年份（2023.5.x）/超深（>7 段）/句末标点仍拒识。
> ③ **【P2 · STD-03 装饰装修材料标准库补齐（openstd.samr.gov.cn 逐条实证，2026-10-07 核实时点）】** `装饰保温` 补入现行 4 条：GB 18583-2008（胶粘剂有害物质限量，**无 2020 版**）、GB 30981.1-2025（涂料中有害物质限量 第1部分：建筑涂料，强标）、GB 18580-2025（人造板甲醛释放限量，强标）、GB/T 39600-2021（人造板甲醛释放量分级）；`ABOLISHED_STANDARDS` 补入 **GB 18582-2020 → GB 30981.1-2025**、**GB 18580-2017 → GB 18580-2025**（均 2026-06-01 实施/废止）；`STANDARD_DB_VERSION` → **2026.10.7**。⚠️ 我的初稿「GB 18583-2020」为记忆错误，实证是 **2008 现行** —— 沿「不凭记忆编造」红线已纠正。
> 护栏：`tests/test_r51_remaining_closeout_20261007.py` **16 例**（指纹单一事实源 2 + 扫描陈旧门控 3（stale 排除 / fresh 计入 / 历史 fail-open）+ 深层标题 7（识别/3 段不回退/年份/超深/句末/parse 类型/重排前缀/幂等）+ 标准库 3）。**A/B 反向验证**：`_RE_HEADING_DEEP` 分支摘除 → 深层重排用例定向失败。
> ⚠️ **行为影响提示（需重新生成/重排正文才可见）**：深层伪标题修复作用于**落库前规范化**（`content_subheading_renumber` 默认开）。已在库里的旧正文需跑 `/numbering-consistency` 修复端点或重存章节才重排；导出成稿从下次导出起生效。
> **基线对比**：关联回归 15 文件 **xxx passed / 0 failed**；新文件单跑 **16 passed**。源码文件行尾保持原样（compliance.py 原生 LF，其余 CRLF）、无 BOM、`py_compile` 全过。
> ⚠️ **运维提醒**：需重启后端进程加载新代码（端口 8000 无监听时 `start_all.bat` 即生效）；`consistency_conflicts` 新列由 `_migrate` 幂等补列。
> ⚠️ **未落地（记录以免重复排查）**：① 图题/表题「图 X-1」与正文「图 1-1」的连字符风格统一（两套编号格式并存，属导出层格式收口，低风险独立子项）；② 深层伪标题修复对**已落库历史正文**的重排（数据迁移，经修复端点或重新生成）；③ 前端 `preflight_summary` 尚未渲染 `stale_ai_sources` 提示（加法式字段已回传，前端增强独立子项）。

> **最近校准：2026-10-07（审核预检误报 + 陈旧 AI 结论 + 正文截断收口 · R50）** —— **【3 处真实缺陷修复 + 1 处日志源隔离 + 22 例新护栏 + 1 项 A/B 反向验证】**（零新增依赖、零数据迁移；新增配置项 1 个默认 True）：
> ① **【P0 · 预检假 block：TRC-01 是 R22「有效正文」收口的唯一漏网检查】** `check_traceability` 仍只看章节自身 content —— 生产方案「计算书及相关图纸」是**父节点**（正文只落叶子、公式全在二级子节），父节点正文恒空被报「未见计算公式或参数代入过程」（**block → released=False**），与 `check_completeness` / `preflight_stats` 的口径分叉（同一根因第 4 处残留）。已统一到 `build_section_tree_index` 的「有效正文」（自身 + 全部子孙），并保留「整体有过程、个别验算章节真缺过程仍报」的反向语义（CMP-09 互斥不破坏）。
> ② **【P0 · 陈旧 AI 结论计入评分（生产库实证）】** 生产方案 d3c1a897… 的 10-01 AI 合规/一致性审计结论，在正文已多次修改的 10-07 总检里仍被计入：CON-04「与项目关键事实一致」detail 指向已不存在的「抹灰底层及接茬验收」、SAF-02/05/07 声称缺失的专项安全措施实际存在 —— 用户据旧清单整改不存在的缺陷。修法：`compliance_check` / `consistency_audit` 新增 `content_fingerprint` 列（schema DDL + `_migrate` 幂等补列；历史行空串 **fail-open**，与 `_run_is_stale` 同约定），三条 AI 写路径（/check、expert_review、consistency_audit）落当前正文指纹，总检聚合读路径比对后**跳过过期行**并把来源名回传 `stale_ai_sources`（加法式字段，前端可提示重跑对应 AI 检查）。
> ③ **【P1 · 截断正文被静默接受（生产库实证）】** AI 返回**非空**正文但 finish_reason=length（被 max_tokens 截断）时旧实现直接返回且零日志 —— 生产方案 5 个章节句尾停在半句，用户无从得知、审核与导出照常用。现：先记 WARNING 让截断**可观测**，再按 `ai_reasoning_max_tokens` 上限翻倍 max_tokens **重试一次**（新开关 `ai_retry_on_partial_truncation` **默认 True**，与 O3 同构严格一次；重试仍截断/失败时返回已获取的**较长**正文，绝不丢内容）。空正文截断（O3）路径不受影响。
> ④ **【P2 · 唯一生产日志源被测试污染】** 大量 API 测试会 `import app.main` → 测试噪声写进 `logs/backend.log`（P95/P99、ERROR Top、HTTP 延迟口径全部失真，轮转配额被测试日志消耗）。`main.py` 在 pytest 进程（`PYTEST_CURRENT_TEST` / `sys.modules['pytest']`）下不再挂生产文件 handler，生产进程零变化。
> 护栏：`tests/test_ai_staleness_and_truncation_r49_20261007.py` **15 例**（指纹判据 fail-open 4 + 总检聚合端到端 6（stale 排除 / fresh 计入 / 历史行 fail-open / 同批混合只计新鲜行 / consistency_audit 双向）+ 截断重试 5（翻倍成功 / 关闭回旧行为 / 正常不重试 / 严格一次 / 重试失败返回半截））；`tests/test_audit_false_positive_20261002.py` **+7 例**（TRC-01 有效正文 6 + A/B 判别力 1）。**A/B 反向验证**：`check_traceability` 判据退回「只看章节自身 content」→ **4 failed / 2 passed 定向失败**，`try/finally` 还原后 sha256 字节一致；另留 monkeypatch 永久护栏（own-content-only 逻辑必然误报）。
> ⚠️ **本轮两处「假阳性」已被探针证伪并撤销**（避免后人重复排查）：① docx oMath「结构泄漏」经 lxml 良构校验为**扫描器假阳性**（document.xml 良构，0 个 `m:t` 含裸角括号；`latex_to_omml_display` 的 `re.search` 边界提取在单公式下正确）；② preflight 与 export 的 `content_fingerprint` 算法差异是**注释明示的两种设计**（判定时效 vs 缓存命中），非缺陷，跨模块不可比属预期。
> **基线对比**：关联回归 13 文件 **226 passed / 13.96s**（preflight / readiness / callopt×5 / audit_false_positive / review_r13 / review_export_closeout）；新文件单跑 **15 passed**；`test_audit_false_positive_20261002.py` **46 passed**。改动的 7 个源码文件行尾保持原样（compliance.py 为 LF，其余 CRLF）、无 BOM、`py_compile` 全过。
> ⚠️ **运维提醒**：本轮全部在后端，需重启后端进程加载新代码（端口 8000 无监听时 `start_all.bat` 即生效）；`compliance_check` / `consistency_audit` 新列由 `_migrate` 幂等补列，无需人工迁移。
> ⚠️ **未落地（记录以免重复排查）**：① `consistency_conflicts`（一致性扫描冲突源）的陈旧判定未接指纹 —— 需改扫描写路径并落 `content_fingerprint`，独立子项；② STD-03 标准库补齐（GB 18582-2020 / GB 18583-2020 / GB/T 39600-2021 等装饰装修材料标准）待联网核实后落库（沿「不凭记忆编造」红线）；③ 正文深层伪标题编号（纯文本 `1.1.1.1` 不随父章节重排）与 DLV-14 节号引用格式属提示词/编号规范化域，未在本轮范围。

> **最近校准：2026-10-07（图表模块遗留收口 · R49）** —— **【口径漂移消除（改文案不改行为）+ 修 R48 遗留的日志键名错配 + 图表产出闭环 + 39 例护栏 + 4 项 A/B 反向验证】**（零新增依赖、零新增配置项、零数据迁移、返回契约零变化；`chart_count` / `chart_yield` 全部**加法式**，旧前端不消费也不报错）：
> ① **【口径漂移消除 · 改文案不改行为】提示词「每章」vs 代码 per-`section_id`**：R48 的 ⑤ 只把口径说明写进**注释**，但提示词本身仍写「每章合计最多 1 个图表块」/「仍受"每章 1 块"约束」/「同一章节一旦选择某类型」—— 文档与行为的漂移没消掉。实测 `sse_handlers` 生产路径已传 `enforce_limits=True`，**行为本身正确**（不是 P0/P1 缺陷）→ 按最小改动面原则**改文案、不动行为**：三处提示词统一为「本节」口径（`content.py:198/201/285`），`_chart_pipeline.py` 两处注释同步。**仍不落地「按一级章计数」**（沿用 R48 ⑤ 结论：需 `sections.parent_id` 树根祖先解析 + 属行为变更会删掉当前合法图表）。
> ② **【R48 遗留真实缺陷 · 删图日志图表类型恒为 "?"】**：`_record_chart_drop` 写入的记录形状是 `{"type": …, "reason": …}`（键名 `type`），而 R48 在 `sse_handlers` 写的删图日志用 `d.get('chart_type', '?')` 读取 → **键名不匹配，图表类型恒打印 "?"**，只剩理由。日志本是删图可观测性的兜底通道（report 与 `section_done` 之外的最后一道），类型静默丢失会让排障者无法判断「到底是哪类图被删」—— 而这正是删图排查最需要的信息。修法：新增 `chart_drop_label(drop) -> "类型/理由"` 作**唯一读出口**，与写侧 `_record_chart_drop` 同模块（读写同源，键名不可能再分叉），兼容 `type` / `chart_type` 双键名、缺值回退 "?" 不抛异常。
> ③ **【图表产出闭环 · `count_inline_charts`】**：R48 只解决了「被删的图」可观测（`charts_dropped_count`），但**「留下的图」始终不可见** —— 用户无法区分「本应配图但 AI 零产出」（提示词/模型问题）与「产出了但被删」（限流/校验问题），两者处置方向完全相反。新增 `count_inline_charts(content) -> int` 作**计数唯一出口**：`has_inline_charts` 是布尔（答不了「几张」）、`extract_inline_charts` 按 `chart_type` **去重**（登记清单口径，同类型第 2/3 块会丢），都不能当计数用。两者共用 `iter_inline_chart_fences` 与同一「未闭合 = 不是图」口径，`has_inline_charts` 收敛为 `count > 0` 的**薄包装**（R49 前两个出口各自遍历，存在分叉风险）。`chart_count` 落 `sections.last_generation_report` 并随 `section_done` **实时**下发，与 `charts_dropped_count` 配对即得「AI 提名 ≈ 保留 + 删除」的完整口径。⚠️ **时机语义变化**：R48 只在「有删图」时重算 `report_json`，R49 新增**恒有值**的 `chart_count` 后必须**总是**重算，否则它同样永远不会落库（加键但没生效）。
> ④ **【前端 · 复用 `section_done`，零新 SSE 事件类型】**：`section_done` 已是逐章实时通道（`quality_issues` / `standard_report` 都走它），无需新事件类型。新增 `ChartYield` / `ChartDropItem` 类型 + `normalizeChartYield`（防御式归一：缺键 / 类型不符 / 旧版后端一律回退 0 与 []，**绝不抛异常** —— 事件循环里任何异常都会中断整个正文生成会话的日志更新）+ `chartDropTexts` + `CHART_DROP_REASON_LABELS`（8 项封闭枚举的中文标签，属展示层映射而非判据副本）。日志行按「配图 N」/「配图 N（已删 M）」渲染，后者挂 Tooltip 列出每张图的删除理由；零图表且无删图的普通章节不占位（那是最常见的正常情况）。
> ⑤ **【护栏 · 39 例 / 4 组】** `tests/test_chart_yield_closure_r49_20261006.py`：A 计数（逐块计数 / 未闭合不计 / 与 `has_inline_charts` **布尔⇔计数恒等** parity / 与 `extract_inline_charts` 去重语义**必须不同**）+ B `chart_drop_label`（真实记录形状 / 双键名 / `type` 优先 / 7 种缺值形态回退 / 8 枚举全冒烟）+ C 提示词口径（「本节」在位、`每章合计最多 1 个图表块` 与 `每章 1 块` **不得回流**、单位三方一致）+ D 跨文件接线（helpers 已 import / `report["chart_count"]` 在位 / `section_done` 三键 / `report_json` 重算**不得嵌回** `if _chart_dropped:` / 删图日志**不得回退裸** `d.get('chart_type'` / 读写两侧同模块）。前端 `contentEvents.test.ts` **28 → 39 例**（+11：归一化正常 / 缺键 / 脏数值 / 计数兜底 / 脏数组 + reason 枚举与后端**逐项一致**的 parity 锁，后端新增枚举值会红）。**A/B 反向验证 4/4 全部定向失败**（A1 日志回退裸 get / A2 提示词回退「每章」/ A3 计数退化为去重 / A4 摘掉 `chart_count` 写入），每次 `anchor_count: 1` → 定向失败 → `restore_sha_match: True`，三文件 sha256 与变异前一致。
> **基线对比**：图表 8 文件合跑（R48 删图可观测 + R49 产出闭环 + inline_charts + 注册 parity + 围栏 EOF 三文件 + 图号回退）= **200 passed / 8.19s**；R49 新文件单跑 **40 passed**（含后补的一条 NameError 护栏）；内容域四文件合跑（checkpoint 闭环 / 前置 / 反哺 / 增强）= **221 passed**；前端 `contentEvents.test.ts` **39 passed**、`npx tsc --noEmit` **EXITCODE=0**。⚠️ 本机 Node 不在 PATH（`%NVM_HOME%` / `%NVM_SYMLINK%` 是**未展开的占位符**），实测 `C:\nvm4w\nodejs\node.exe`（v25.8.0）存在，须用完整路径调用 —— AGENTS §6「Node v25.8.0 已可用」结论仍成立，但 PATH 配置在本机已失效。
> ⚠️ **【本轮两次护栏自身缺陷（与前序「假判据」同族，务必记录）】** ① A/B 脚本首版用 `\n` 拼多行锚点，在 **CRLF 文件上一个都匹配不到**（A3 / A4 均 `anchor_count: 0` 被 SKIP = 白跑）—— 与 AGENTS §5.15 同坑**第三次**重演。修法：比对前 `replace("\r\n","\n")` 归一、写回时再转回 `\r\n`。② R49 首版护栏 `test_log_uses_chart_drop_label_not_bare_get` 只锁「裸 `d.get('chart_type'` 不得回流」，**没锁函数名本身** → 我把实现写成 `_chart_drop_label(d)`（多了下划线，模块内不存在）时护栏全绿，**靠 R48 的端到端用例 `test_section_persists_and_completes` 当场抓住 `NameError`** —— 这印证了 §4.14 的判断：「形态护栏 + 端到端用例」两层缺一不可，纯形态护栏会放过「语法合法但符号不存在」这一类。已补双向断言（裸 get 不得回流 + `_chart_drop_label(` 不得出现 + `chart_drop_label(d)` 必须在位）。
> ⚠️ **【本轮最大教训】** A/B 反向验证只验证了 4 个护栏，但**真正抓到缺陷的是既有的端到端测试**（`test_content_fence_contract_20260929` 的 SSE 全链路）。**改动跨模块的调用点（新增 helper 并接线）后，必须跑一遍「真实事件流」的端到端用例**，不能只跑自己新写的静态护栏 —— 静态护栏验证「源码长什么样」，端到端用例验证「真能跑起来」。
> ⚠️ **【未落地（记录以免重复排查）】** ① **「本应配图但 AI 零产出」检测不做** —— 它需要一份「标题→配图相关性」判据，而该判据目前**只存在于提示词散文里**（`content_generation_system` 的「触发关键词映射表」），抽成代码常量就是本仓反复踩的「同一判据多副本」反模式（R48 引导语第 3 份副本同族）。要它时应**先在提示词层收敛出唯一来源**再落代码；届时 `has_inline_charts` / `count_inline_charts` 已就位可直接消费。② **「每章≤1」按一级章计数** —— 沿用 R48 ⑤ 结论，不落地。③ **图表产出闭环目前只有逐章粒度**（`last_generation_report` + 日志行），未做整批聚合（如「本批 N 章共 M 张图」）—— 数据已在库里，属可选增强。

> **最近校准：2026-10-06（图表生成与渲染模块深度审查收口 · R48）** —— **【3 处真实缺陷修复（引导语判据第 3 份副本 / 僵尸图 / 删图零可观测）+ 3 处不变式加固 + 31 例护栏 + 1 项 A/B 反向验证】**（零新增依赖、零新增配置项、零数据迁移、返回契约零变化；`dropped_out` / `strict` 全部**加法式**，不传即旧行为逐字一致）：
> ① **【P1 · 引导语判据第 3 份副本】8 备选 vs 5 备选正则 + 3 处裸 `60`**：`_chart_pipeline._ORPHAN_LEAD_IN_RE`（8 备选）与 `content_blocks.LEAD_IN_HINT_RE`（5 备选）判定**完全等价**（8 个备选全被 5 个经子串覆盖，探针实证），但两侧各写一份；长度上限另有 3 处裸 `60` 字面量。任一侧改动都会造成「登记侧删、导出侧留」的悬空引用（登记侧删了「如下图所示：」、导出侧的 `_pop_orphan_lead_in` 判不出来，于是这句孤立引导语留在成稿里却无图）。现收敛为 `content_blocks` 唯一真值（`LEAD_IN_MAX_CHARS` / `LEAD_IN_HINT_RE` / `ORPHAN_LEAD_IN_RE`），两侧均为别名或 import；`export.py` 与 `_chart_pipeline.py` 的裸 `60` 全部改常量。
> ② **【P1 · 僵尸图】图表登记计算异常时只跳过登记，上一轮旧登记残留**：`apply_inline_chart_plan` 的**第一步**是 `DELETE FROM chart_predictions WHERE section_id=?`，计算失败时该 DELETE 从不执行、而下方正文 UPDATE **无条件**提交 → 「正文是这一轮新值、图表登记是上一轮旧值」：导出把已不在正文里的旧图追加到章末（`chart_predictions` 是导出主数据源）、图表清单显示正文里根本不存在的图（幽灵图）、预检图表统计虚高，三者全静默。旧日志文案「本次跳过图表登记，不影响正文」还**没提旧登记残留**，排障者会误判库是干净的。修法：异常分支也做「仅清理」调用（`rows=[]` + `content=None` → 只执行 DELETE），清理失败仅告警不阻断正文落库（正文是主产物，且此处处于写锁临界区，二次异常会让整章正文都丢）。
> ③ **【P1 · 删图零可观测】4 条删图理由全部只进 backend.log**：登记侧每次删块只打日志，**不进 SSE 事件、不进正文生成 report、不进导出预检** → 用户看到一份「少了几张图却毫无理由」的正文。现 `_record_chart_drop(dropped_out, chart_type, reason)` 为唯一记录点，`build_inline_chart_plan` / `apply_inline_chart_plan` 各增 `dropped_out: list | None = None`；reason **封闭枚举 8 项**（`per_section_limit` / `scheme_type_limit` / `validation_failed` / `missing_prompt` / `empty_envelope` / `invalid_json` / `insert_failed` / `insert_none`）；sse 侧汇总进 `report["charts_dropped"]` + `charts_dropped_count`（落 `sections.last_generation_report`，前端 Popover 已消费该字段）并写带 scheme/task 标识的 INFO。**⚠️ 时序坑：`report_json` 的 `json.dumps` 原本发生在写锁之前**，删图汇总在锁内 → 不重算就永远不会落库，已在锁内补一次 dumps（护栏 `test_report_json_redumped_after_drop_collection` 锁定顺序）。`apply` 侧按「类型+理由」**去重**（`skipped_types` 本身按类型去重，不去重会让计数虚高）。
> ④ **【不变式加固 · 幽灵图防线】「continue 即不登记 ⇒ 必删块」三处缺口**：`build_inline_chart_plan` 里 3 个信封失败分支（ai_image `_payload` 空 / JSON `json.loads` 失败 / mermaid `payload_json` 空）**只 `continue` 不删块** → 块留在正文却无 `chart_predictions` 登记，导出照渲且绕过「每章≤1 / 同类型≤3」上限。⚠️ **这三条当前调用图下都不可达**（扫描器 `_scan_chart_fences_full` 与 `_validate_inline_chart` 已各自先做过 `json.loads` 并跳过/原样返回），属**防御纵深缺口**而非现行缺陷 —— 但「不登记就必须删块」是结构性不变式，靠上游守卫维持太脆，任何一次改动绕过上游就会静默造出幽灵图。
> ⑤ **【刻意不改行为 · 口径说明落注释】** 「每章≤1」提示词与代码的 per-`section_id`（叶子小节）计数是两回事：一个一级章挂 N 个二级小节时可各自合法产出 1 张 ⇒ 该章最多 N 张。按一级章计数需解析 `sections.parent_id` 树根祖先，而 `build_inline_chart_plan` 是**无 DB 访问的纯计算**（锁外执行），且收紧限额会删掉目前合法的图表 —— 属行为变更，按「默认关闭 + 单列开关」原则**本轮不落地**，仅在代码注释与护栏 `test_per_section_limit_semantics_documented` 里锁死该口径说明，防止后人误改行为。
> ⑥ **【已核实为「既有能力，非缺陷」——避免重复排查】** 跨章借图（`_resolve_chart_code` 三级取码的「同类型兜底借用」）**已由 D1（2026-10-06）收口**：借图来源经 `_BORROWED_CHARTS` → `X-Chart-Render-Stats` 的 `fallback_borrowed` / `fallback_borrowed_details` → 前端 `parseChartRenderStats` 展示，并有 `test_export_hardening_d1_d4_20261006.py` 与 `frontend/src/tests/exportResponse.test.ts` 双向护栏；`chart_fail_placeholder` 开关、`_EXPORT_CONFIG_KEYS` 双向 parity（漏登记 / 死键双向断言）、图号不占号回退（R35）均**已在位**。
> ⑦ **【护栏 · 31 例 / 6 组】** `tests/test_chart_drop_observability_r48_20261006.py`：A `_record_chart_drop` 契约（None 空转 / 记录形状 / build 侧不去重）+ B 四理由各自可观测（每章超限 / 同类型超限 / 校验失败 / ai_image 缺 prompt，每条都断言**块真的从正文消失**）+ C 不变式加固三例（空信封 / 非法 JSON / 空 mermaid 信封必须删块，其中非法 JSON 需同时放开扫描器与校验器两层前置守卫才能触达，注释已写明这是防御纵深）+ D `apply` 侧（限额跳过上报 / 类型+理由去重 / 仅清理路径只 DELETE 不 INSERT / 不传 `dropped_out` 零开销）+ E 静态护栏（理由封闭枚举双向 / 每条删块出口接线在位 / 分支体删块赋值判定）+ F 跨文件接线（sse 双调用点传 `dropped_out` / report 双键 / **`report_json` 重算顺序** / 僵尸图仅清理分支存在 / 引导语常量单一来源）。
> ⚠️ **【本轮两条护栏自身缺陷（与前序「假判据」同族）】** ① 首版 `test_every_envelope_empty_branch_deletes_the_block` 取**固定 500 字符窗口**判断分支体内是否有 `edits[` —— 相邻的 `if not _prompt:` 分支那条赋值会把窗口填满，**删掉目标分支的赋值仍通过**（假护栏）。改为按 8 空格缩进取分支体 + 剥掉注释行后判定，并做 A/B 反向验证（`anchor_count: 1` → 变异 → **1 failed 定向失败** → `restore_sha_match: True`）。② `_apply_plan_body()` 的切分锚点写成 `\n\ndef register_inline_charts(`，实际是 `async def` → `ValueError: substring not found`，两个静态用例收不了。
> ⚠️ **【本轮 3 例测试桩自身缺陷（非源码缺陷）】** ① `rows[0][4]` 取的是 title 不是 chart_type（应为 `[3]`，元组序 `(id, section_id, scheme_id, chart_type, title, ...)`）；② 构造「校验失败」样本时用了裸 `mermaid` 关键字代码 → 扫描器 `_scan_chart_fences_full` 直接跳过（返回 `[]`），到不了校验分支；改用 `flowchart TD\n ]invalid[[ broken` 才能识别出类型且校验不可修复；③ `invalid_json` 分支需同时放开扫描器 + 校验器 + 让信封返回非空，否则分别走到「跳过」与 `empty_envelope`。**教训（与前序 R43 同族）：断言失败先证伪桩再指控源码。**
> **基线对比**：图表套件 `python -m pytest tests/ -q -p no:cacheprovider -k chart` = **453 passed / 1 xfailed / 0 failed（32.85s）**（= 修复前基线 422 + 本轮新增 31，**零回归**）；新文件单跑 **31 passed / 1.56s**；改动波及面专项（content_standard_sse_wiring / report_endpoints / fuzzy_trace / export_closeout / outline_fixes / export_hardening 六文件）= **224 passed / 36.73s**。A/B 脚本与探针已清理（`r48_ab_guard.py` / `r48_probe2.py` / 锁文件），源文件 sha256 `bf2226ad1fadf8e2` 与变异前一致、`AST OK`。⚠️ 字节体检：4 个被改文件均 U+FFFD=0、BOM=False、CRLF 一致（`content_blocks.py` 的 1 处裸 LF 为**既有**形态，备份同样如此）。
> ⚠️ **运维提醒**：本轮全部在后端，需重启后端进程加载新代码（端口 8000 无监听时 `start_all.bat` 即生效）。
> ⚠️ **未落地（记录以免重复排查）**：① 「每章≤1」按一级章计数（需章节树根祖先解析 + 新开关，见 ⑤）；② `charts_dropped` 目前只落 `last_generation_report`（前端 Popover 可见），**未做实时 SSE 事件** —— 生成过程中不弹提示，属新增事件类型的独立子项；③ `has_inline_charts` 生产零消费仍维持 R38-L 结论（AI 对该配图的章节未产出图表时无检测无提示，是产品级空窗而非代码缺陷）。


> **最近校准：2026-10-06（导出孤儿产物永久磁盘泄漏收口 · R47）** —— **【1 处真实 P2 磁盘泄漏修复 + 1 处实现期自引入安全缺陷被既有护栏当场拦下 + 51 例护栏（含 5 例护栏自身缺陷修正）+ 1 处前端逐行状态可视】**（零新增依赖、零新增配置项、零数据迁移、返回契约零变化；GC 全程 **fail-soft**，任何异常不阻断本次导出交付）：
> ① **【P2 · 永久磁盘泄漏】`_prune_export_cache` 只清「DB 有行但已陈旧」的文件，两类产物在 DB 里根本没有行 → 永久留在 `EXPORTS_DIR` 且零告警**：(a) DOCX 原子替换 3 次重试均失败 → 降级为「本次不写缓存」，`out_path = tmp_out_path`（`.tmp.docx`）直接返回给用户，之后再无任何路径清理它；(b) PDF 分支 `os.replace` 成功、随后 INSERT 抛异常 → `out_path` 已在盘上、DB 无对应行（`/cache-status` 也看不见）。一份 DOCX 可达数十 MB，每失败一次永久泄漏一份。新增 `_gc_orphan_exports(db, scheme_id, protect=None) -> int`，**三重收窄**（只扫顶层文件不递归 → `charts/` PNG 缓存目录绝不受影响；只处理 `{scheme_id}_` 前缀 → 跨方案误删在结构上不可能；排除现存缓存行 + `protect`），`.tmp.*` 额外 600s 宽限期（`_ORPHAN_TMP_TTL_SECONDS`）—— 因为 **FileResponse 在 handler 返回之后才开始流式读该文件**，刚交付的降级产物必须留一个宽限期否则下次导出会顺手清掉；非临时产物无宽限。接 3 处调用点：DOCX degraded（`protect={out_path}`）/ DOCX 正常（`protect={out_path}`）/ PDF（**无 protect** —— PDF 响应体是内存字节，磁盘文件与交付无关）。**缓存命中分支刻意不接**：命中分支有活跃缓存行，GC 会在下次导出补跑。
> ② **【本轮自引入又当场拦下的安全缺陷】GC 初版复用 `_db_fetch_all` → DB 读失败时 `live` 为空集 → 把本方案「所有」现存产物判成孤儿并全删**：DB 短暂不可用一次即可让用户秒级缓存全部作废、数十 MB 成稿静默消失。**是既有 R13 单一出口护栏当场抓住**（`test_export_closeout_20261006.py::TestR13SingleSource` 两条：`test_no_bare_fetchall_or_fetchone_left` 命中 5009 行、`test_no_inline_none_guard_left` 命中 5007 行 —— 我为了区分「查询失败」与「确实无行」绕开了单一出口）。**正确解法不是破坏既有架构约定，而是给约定加一个显式收窄开关**：`_db_fetch_all` 增加 `strict: bool = False` + 新增 `DBReadError`，默认 fail-soft **逐字不变**（既有调用方零行为变化），破坏性调用方显式 `strict=True` → 读失败抛 `DBReadError` → GC 捕获后整体跳过。判据：`「查询失败」与「确实无行」是两条完全不同的语义`，混在一起等于把磁盘上的成稿判成孤儿。
> ③ **【护栏 · 51 例 / 7 组】** `tests/test_export_orphan_gc_20261006.py`：A 行为（超 TTL 孤儿删除 / TTL 宽限期内保留 / `protect` 保护 / 活跃缓存行保留 / 跨方案前缀隔离 / `charts/` 子目录跳过 / 计数准确 / 目录不存在 fail-soft / DB 读失败一个都不删 / 同名前缀目录跳过但不中断扫描）+ TestGcWiring（三调用点位置与 protect 口径 / 缓存命中分支不得调 GC / degraded 是唯一 `.tmp` 返回者且必须 protect）+ TestDbReadStrictMode（默认 fail-soft 不变 / strict 双失败路径抛错 / 成功返回行 / **AST 锁「全仓只允许 GC 一处 opt-in」**）+ B issue type ↔ rule map 双向 parity + C 配置白名单 ↔ `config.get` 双向 parity（含「新增渲染行为不登记白名单 → config_hash 不变 → 命中旧缓存」的历史真实坑）+ D 配图签名不得读密钥 / 必须覆盖 4 个像素维度 + E `cache_status` 契约（`total`/`stale`/`items[].exists` / 同秒 `rowid DESC` / 拒绝空 `result_path` / prune 与 GC 互补）。
> ⚠️ **【本轮 5 例护栏自身缺陷（与前序「假判据」同族，务必记录）】** ① `test_missing_exports_dir_is_fail_soft` 断言 0 实得 5 —— **忘了 monkeypatch `EXPORTS_DIR`**，测的其实是真实导出目录。② `test_same_prefix_directory_is_not_touched` 断言 `good.exists()` —— 但 `good` 恰恰是**应当被删**的孤儿文件，断言方向写反。③ `test_mapping_covers_all_16_probes` 写死 16 —— 实测只有 15 项，是我自己先前数错；已改为「≥15 且 == 实际产出集合大小」双判据。④ `test_signature_reads_exactly_the_four_pixel_dimensions` 只用 `ast.Attribute` 扫描 —— 但 `image_model`/`image_default_size`/`image_base_url` 是以**字符串字面量**传给 `getattr(_s, "image_model", "")` 的，只有 `image_enabled` 是真属性访问 → 新增 `_code_names()` 助手同时收属性名与字符串常量。⑤ `test_strict_is_only_opted_in_by_gc` 初版用 `"strict=True" not in src` 文本匹配 → 被 `export_docx` **注释里的** `numbering_consistency_strict=True` 误伤假失败 → 改用 `ast.Call` 遍历找真正的 `_db_fetch_all(..., strict=True)` 调用节点（与 §5.14「源码含字符串是假判据」同族）。⑥ `_code_names` 必须 **AST 剥掉首个 docstring 节点**，否则「绝不取 api_key」这句说明本身含该字样，防密钥护栏自我触发。
> ④ **【前端 +1 处可视】** `SchemeWorkbenchPage.tsx` 导出缓存表新增「状态」列（按 `items[].exists` 渲染 有效/失效 Tag）：此前失效行（文件已被保留策略清理或为导出失败遗留孤儿）与有效行长得一模一样，用户会误以为任何一行都能秒级复用；「失效 N 条」Tooltip 同步补「或为导出失败遗留的孤儿产物…无需手工处理」。
> **基线对比**：导出套件 21 文件 `python -m pytest tests/test_export*.py -q -p no:cacheprovider` = **411 passed / 1 xfailed / 0 failed（61.23s）**；新文件单跑 **51 passed / 3.16s**。⚠️ 首轮跑同一套件时 **2 failed** —— 正是 §② 的实现期缺陷被 `TestR13SingleSource` 定向拦下（402 passed / 2 failed / 405 collected），修正后 411/0。**后端全量 `python -m pytest -q -p no:cacheprovider` = 6230 passed / 17 skipped / 1 xfailed / 0 failed / 1 warning（309.56s）**（= R46 基线 6179 + 本轮新增 51，**零回归**）。探针已清理（`tests/_r47_*.out|err`）。
> ⚠️ **本轮环境限制**：**本机 Node 不在 PATH**（`npx` 报 `not recognized`；`%NVM_HOME%`/`%NVM_SYMLINK%` 在 PATH 中是未展开的占位符，`C:\Program Files\nodejs` 与 nvm 目录均不存在）→ AGENTS.md 里「Node v25.8.0 已可用」的记录在本机**已失效**，前端 `tsc --noEmit` 与 vitest **未能实跑**，④ 的前端改动需在有 Node 的环境复核（改动仅为 antd Table 追加一个 column 定义，`Tag` 已在 `:5` 导入，渲染风格与同表既有列一致，已静态核对）。
> ⚠️ **运维提醒**：需重启后端进程加载新代码（端口 8000 无监听时 `start_all.bat` 即生效）。
> ⚠️ **未落地（记录以免重复排查）**：① GC 只在**导出时**触发，长期不导出的方案其孤儿产物不会被清（磁盘孤儿目录 GC 仍是独立子项）；② `export.py` 5500+ 行的 god-function 分解仍未做；③ 并发同指纹导出的冲突处理仍是「后写者覆盖 + INSERT OR IGNORE」，无版本号守卫。
>
> **最近校准：2026-10-06（审核预检 + 导出双模块收口 · R46）** —— **【2 处真实孤儿缓存缺陷 + 4 处次序语义加固 + 14 例三层护栏 + 1 例前端组件级测试】**（零新增依赖、零新增配置项、零数据迁移、返回契约零变化；缓存失效全部 **fail-soft**，失效抛错不阻断重置/压缩/新增结果）：
> ① **【P1 · 孤儿缓存（真实用户可见缺陷）】`reset_content` / `shrink_section` 漏接 `invalidate_export_cache`**：重置正文清空目录树全部章节正文、压缩改写单章正文，两者都会改变 `content_fingerprint`，但旧 `export_cache` 行与磁盘 .docx/.pdf **不失效** → `/cache-status` 仍报「有可复用缓存」，用户点「复用上次成果」导出的是**正文已全空的旧产物**。`update_section` / `delete_section` / `reorder_sections` / `save-outline` 五条结构变更路径均已失效，唯此两处漏接。两处均包 `try/except` + `logger.warning`（失效是幂等 DELETE，失败不得阻断主操作）。
> ② **【P2 · 次序语义加固】4 处 `ORDER BY created_at DESC` 补 `rowid DESC` 兜底**（`review.py` ×3、`export.py::cache_status` ×1）：`created_at` 是 **TEXT 秒级精度**，同一秒内多条记录在 SQL 语义下次序**未定义**（当前索引下恰好稳定，故属语义正确性加固而非实测缺陷）。
> ⚠️ **【本轮最大教训 · `git checkout` 不可逆破坏未入库修复】** 中途 A/B 脚本把 `sections.py` 写成 0 字节，用 `git checkout HEAD -- sections.py` 恢复，**同时丢弃了 3 处未提交的 R41 修复**：F2 `create_section` 的导出生效、F3 `_save_outline_to_db` 空目录分支的导出生效、F4 `reset_content` 的目录生成 409 守卫。**是 R41 既有护栏当场抓住**（`test_outline_fixes_20261005.py` 6 例定向失败：`...FF..FFF.F`），已逐处补回并复跑全绿。**教训：本仓大量修复未入库，`git checkout` 对未跟踪/已改文件是单向破坏；恢复写坏文件前必须先 `git status --short` 确认目标文件的 diff 是否承载未入库工作，优先从字节级备份（`%APPDATA%\Qoder\SharedClientCache`）取回而非 checkout。**
> ③ **【护栏 · 14 例三层结构】** `tests/test_review_export_closeout_r46_20261006.py`：行为锁（submit 取最新行 / 门禁不被绕过 / 分页连续 / cache_status 最新 5 行 / reset 失效 + fail-soft）+ 静态锁（AST 扫 `sections.py` 所有 `UPDATE sections ... content=` 写路径必须真调用 `invalidate_export_cache`；`_renormalize_all_section_contents` 登记为 helper，另设「调用方全部失效」护栏兜住每个调用点）+ 跨语言 parity（真解析 `ReviewWorkflowPanel.tsx` 的 `NEXT_ACTIONS` 字面量，断言前端 ⊆ 后端 `_ALLOWED_TRANSITIONS` 且每个状态有入口、label 非空）。
> ⚠️ **【本轮两条假护栏教训（与前序同构，务必记录）】** ① **「源码是否包含字符串 `invalidate_export_cache`」是假判据**：删掉调用但保留 import 时该判据仍为真 → 已改用 `ast.Call` 遍历（与 §4.14「AST 扫全仓调用点」同型）。② **本仓既有前端状态流转测试是「手写镜像 vs 手写镜像」**：两侧同步漂移恒通过 → 新护栏直接正则解析 `.tsx` 字面量（注意同一行既声明 key 又带 items，不能用 `elif` 分流）。③ **helper 例外表不是万能出口**：登记 `_renormalize_all_section_contents` 为例外后若不补「调用方全部失效」护栏，例外即空洞（A7 实证）。
> ⚠️ **【静态锁排除误判的两把尺子】** `_is_inside_comment`（tokenize 逐行）+ `_in_multiline_string`（AST 一次解析并**缓存区间**，避免 5000 行文件逐行重解析）—— 否则 `export.py` 5407 行注释里说明问题的示例代码会被误报。
> ⚠️ **【本轮再次踩中行尾损坏坑（本仓第二次，§4.21.5 同坑）】** 用 `split("\n")` 处理 AGENTS.md 后 `"\r\n".join(...)` 写回 —— CRLF 文件每行已带尾随 `\r`，join 又补 `\r\n` → 全部行尾变 `\r\r\n`，**11463 行被撑成 22925 行**（`Get-Content` 把裸 `\r` 也当换行符，行数正好翻倍）。已用**字节级** `raw.replace(b"\r\r\n", b"\r\n")` 还原，并断言 `crcr == 0` / 无 U+FFFD / 关键内容探针各出现预期次数后才 `os.replace`。**教训（第三次强调）：行尾只能用 `splitlines()` + 一次性换算，绝不能 `split("\n")` 后对含 `\r` 的行再 join CRLF；任何整文件行级批量重写前必须先留字节级备份 —— 本轮正是靠 `%TEMP%` 里的备份副本才敢安全替换。**
> ④ **【前端组件级测试 +1 例】** `frontend/src/tests/ReviewWorkflowPanel.test.tsx` 补「各行操作按钮严格按 `review_status` 渲染」：pending→通过/驳回、**approved→仅复审（不再出现通过/驳回）**、rejected→仅重提、skipped→复审。与后端 parity 护栏互补（一个测「声明是否漂移」，一个测「渲染是否符合声明」）。⚠️ **本机 Node 不在 PATH（`npx` 报 not recognized），该用例未能实跑**，需在有 Node 的环境复核（模式与本文件既有用例一致，已静态核对 `ReviewWorkflowPanel.tsx:458` 的渲染分支）。
> **基线对比**：后端全量 `python -m pytest tests/ -q -p no:randomly` = **6179 passed / 17 skipped / 1 xfailed / 0 failed（291.59s）**（= R43 基线 6165 + 本轮新增 14，**零回归**）；双文件合跑 `test_outline_fixes_20261005.py + test_review_export_closeout_r46_20261006.py` = **36 passed / 7.00s**。探针已清理（`_probe_r46_ordering.py` / `_probe_r46_hostile.py` / `_r46_probe.py` / `_r46_result.txt`）。
> ⚠️ **运维提醒**：本轮全部在后端（前端仅测试），需重启后端进程加载新代码（端口 8000 无监听时 `start_all.bat` 即生效）。
> **最近校准：2026-10-06（目录生成窗口步进缺陷收口 + AI 配置快照字段漂移 + 护栏多自由度加固 · R43）** —— **【真实 P0 缺陷修复 + 参数网格发现护栏盲区 + 测试桩自身缺陷当场证伪】**（零新增依赖、零新增配置项、零数据迁移、返回契约零变化；`_merge_k=1` 出厂默认档行为**逐字节不变**）：
> ① **【P0 · 目录生成窗口 × 单元合并重叠】`_round_step = max(1, _window) * _merge_k`**：分步生成编排（`sse_handlers.generate_outline` 的 `event_stream`）用「并发窗口」提速：`_window = OUTLINE_CHAPTER_CONCURRENCY`（本轮并行单元数）、`_merge_k = OUTLINE_CHAPTER_BATCH_SIZE`（每单元合并章数）。每轮实际覆盖 `_window × _merge_k` 章，但窗口外层循环写成 `range(0, _total, _window)` —— **步进只用窗口宽度**。`_merge_k == 1`（出厂默认）时两者恰好相等，缺陷完全不可见（既有 `test_outline_stepwise_window.py` 只覆盖 `_merge_k=1`）；一旦用户升批（配置注释明确鼓励），**相邻两轮窗口重叠 `_merge_k - 1` 章**：`_window=2, _merge_k=2, _total=6` → 轮1 章 1,2,3,4 / 轮2 章 3,4,5,6。三条用户可见后果：重复章被 `_merge_unit_results` 二次 append（目录出现同名章节、编号 1..N+K）／子目录 AI 重复烧（成本翻倍）／后一次成果静默覆盖前一次。修法：步进取整轮覆盖量，`_merge_k=1` 时数值逐字不变。
> ② **【P2 · 全仓同型扫描】** 扫 `for ... in range(0, X, Y)` 全仓仅命中这一处；一致性扫描 / 修复 / 正文生成走「**先一次 chunk 再 `asyncio.gather` 全派发**」的正确结构，无同型缺陷。**结论：缺陷是孤立的，但「双自由度配置只测单档」是普遍盲区**（见 ④）。
> ③ **【P2 · `ai_config.priority` 未登记进快照白名单】** `audit_service.SNAPSHOT_FIELDS` 缺 `priority`（`schema_sql:517` 有该列，`ai_config_debt_20261006` 的漂移护栏已报）。**最优解是「进快照 ≠ 可回滚」**：`priority` 与 `is_active` 同在快照里，但都不进 `ROLLBACK_FIELDS` —— 前者由 `PUT /fallback-chain` 整体维护、`_resequence_priority` 在增删配置后把全表重排为 0..n-1，回滚时把某条配置写回**过期序号**会在降级链留下空洞（0,2,3,5）或重复（两条同为 2），候选链顺序失真而用户无从察觉；`is_active` 则由 `include_active` 分支单独处理（主配置唯一性守卫）。快照留存二者只为审计可追溯（「这次改动发生时排在降级链第几」）。已在 `SNAPSHOT_FIELDS` 与 `ROLLBACK_FIELDS` **两侧**各落注释锁死该分工。
> ④ **【P1 · 护栏盲区加固】参数网格取代单点用例**：窗口护栏原只有 2 个用例且都是 `_merge_k ≤ _window` 的温和档。改为 **8 组 `(window, merge_k, total)` 参数网格端到端跑真实 SSE 流**，覆盖四类风险形态：① 出厂默认档 ② `merge_k > window` ③ `window > merge_k` ④ 章数非整轮倍数（末轮残余单元）。配套**静态锁**直接断言步进由二者共同导出 —— 功能断言的失败信息是「章被处理两次」，静态锚点能直接指到根因。
> ⚠️ **【本轮最大教训 · 护栏自身缺陷当场证伪】** 网格首跑 **`w4xk2_t11` 失败**：`unit_calls=[1..10, 1]`（第 11 章漏、第 1 章重复），看着像一条比原缺陷更深的生产 bug。逐层 monkeypatch 打点（`render` → `_fetch_unit_children` → `_fetch_chapter_children`）定位到：**是测试桩错，不是源码错**。桩对 `collect_json_response` 的 `messages` 一律取 `messages[0]`，但**单章单元的 messages 是裸字符串**（`_build_unit_prompts` 的 `single_prompts` 存的是 `render(...)` 的返回 `str`；`_fetch_chapter_children` 的 `sub_prompt` 原样透传；`json_response._ensure_user_message` 对非 list 直接原样返回）→ `"11"` 被截成首字符 `"1"`。在 `_fetch_chapter_children` 入口看到 `'11'`、到 `collect_json_response` 变成 `'1'`，两层夹逼才确认。修法：桩按 `isinstance(messages, str)` 分流整串使用，绝不 `messages[0]`。**教训：桩必须锚定真实契约（两种形态都要覆盖），而不是只覆盖自己预期的那一种；断言失败时先证伪桩再指控源码** —— 否则会像本轮一样为「假缺陷」重写一遍定位流程。该教训已写入测试文件 docstring。
> 护栏：`tests/test_outline_window_step_bug.py` **9 例**（8 组网格 + 1 静态锁）+ `test_ai_config_debt_20261006.py::TestConfigSnapshot` **14 例**（原 10 + 本轮 4：snapshot-only 字段锁 / `ROLLBACK_FIELDS ⊆ SNAPSHOT_FIELDS` / priority diff 可追溯 / **端到端回滚不得写回过期序号**）。**A/B 反向验证**：步进还原成旧形态 → **8 例定向失败**，唯一通过的是 `(1,1,5)` 出厂默认档 —— 正是「旧步进恰好正确」的那一档，实证它无法靠默认档发现该缺陷；还原后 **9/9 全绿** + sha256 字节一致。
> **基线对比**：目录生成关联回归（`-k "outline_..."`）**401 passed / 0 failed**；AI 配置 10 份测试文件 **353 passed / 0 failed**；本轮两文件合跑 **63 passed / 5.14s**。
> ⚠️ **A/B 脚本踩坑（本仓第二次踩同一坑，务必记录）**：把 FIX 与 BUG 形态都做子串计数守卫时，`BUG = "_round_step = max(1, _window)"` 是 `FIX = "... * _merge_k"` 的**前缀**，`norm.count(BUG)` 恒为 1 → 守卫恒失败。必须**按行尾锚定**（`BUG_LINE = "..._window)\n"`）才能区分父子串形态。另：PowerShell `> file 2>&1` 对 python 子进程**吞掉全部输出**（重定向文件 0 字节），改用 `cmd /c "python script.py > out 2>&1"` 才拿到 traceback。
> ⚠️ **未落地（沿用前序结论）**：生产库「修复前后对比测量」仍阻塞（`sections_with_content=0`，需用户在 UI 重新生成正文）；目录树结构编辑的乐观并发（`updated_at` CAS）仍缺；`review_records.section_id` 删章残留、`PRAGMA foreign_keys=0` 按 R32 结论刻意不动。
> ⚠️ **运维提醒**：本轮全部在后端，需重启后端进程加载新代码（端口 8000 无监听时 `start_all.bat` 即生效）。

> **最近校准：2026-10-05（解析提取模块收口 · R42 · R13/B3/B4 + B5~B8/B10 修复 · B9 核实为非缺陷）** —— **【解析提取 / 文件导入 / 文件解析 全链排查与补齐】**（零新增依赖、零新增配置项、零数据迁移、返回契约零变化；**只改前端预检接线与 `db.execute` 判空，不改解析算法、提取提示词与后端上传契约**）：
> ① **【P0 · R13】`db.execute` 未判空三处**：`services/doc_pipeline/pipeline.py::ingest_parse_result`（解析→分块唯一入口，命中即 `AttributeError` 被上层 `except Exception: return {}` 整段吞掉 → doc_chunks 全丢、响应 layers 消失、日志只剩「解析成功」）与 `routers/global_facts.py` 写侧 / 读侧（回写静默丢失）。补判空并按「无历史块 / 无可用缓存」降级，均 fail-soft。
> ② **【P1 · B4】`services/ocr.py::vision_available` 探测异常仍写缓存**：失败结果被缓存 → 后续判定恒走同一条降级路径，进程内无法自愈。改为**只缓存成功探测**，失败当次降级不落缓存。
> ③ **【P1 · B3】累计配额被拒文件名未回报**：`global_facts` 累计触顶后 `quota_files` 只记数不记名 → 前端 `buildUploadNotice` 只能报「N 个未保存」，用户不知是哪些。补 `quota_files` 明细回传（加法式，既有键一字未动）。
> ④ **【P1 · B5】卸载 cleanup 缺两路 abort**：`SchemeWorkbenchPage.tsx` 组件卸载 effect 未 abort `factsListAbortRef` / `factsDocsListAbortRef` → 卸载后仍 setState。补两行（`[id]` effect 的清单未动）。
> ⑤ **【P1 · B6】切换方案不重置提取选中态**：`[id]` effect 缺 `setSelectedBaItem(null)` / `setSectionCheckResult(null)` → 右侧残留上一个方案的提取项详情与多标段检测结果。补两行 reset。
> ⑥ **【P2 · B7】左面板标题写死「18 项结构化提取」**：后端增减提取项即失真。改 `${baDefs.length} 项结构化提取`，定义未加载时不报数。
> ⑦ **【P2 · B8】无累计体积前端预检**：超 200MB 的批次要整批传完才被后端扣下。新增纯函数 `splitByUploadTotalQuota`（**逐字复刻**后端「累计 + 本份 > 上限 → 该文件及其后全部 held」的顺序语义）+ `MAX_UPLOAD_TOTAL_FALLBACK`，在 `handleUploadDocuments` 内预检并 `msg.warning` 列出被扣文件。
> ⑧ **【P2 · B10 · 本轮新发现】「① 上传文件保存」入口绕过单文件硬校验**：`accept` 属性只过滤文件选择框、拖拽可绕过；`UploadParseTab` 入口已有 `partitionUploadFiles` 先验，页面按钮入口**没有** → 超限/非法扩展名整批发给后端再逐个拒（白等一轮上传，且两套提示口径）。修法与 B8 同构：把 `partitionUploadFiles` 收敛进两条入口的共同上游 `handleUploadDocuments`（单文件过滤 → 累计配额 → `setUploadingFacts`），import Tab 侧原预检保留（文案口径一致由护栏锁定）。
> ⑨ **【已核实非缺陷 · B9】启动按钮双击**：`BidAnalysisTab.tsx` 只在 `!running` 时渲染启动按钮（`{!running ? (`），`runBaSse` 同步设置 `baSseAbortRef` 守卫 + `finally` 成对清理 → **不是缺陷**，只加渲染条件防回退锁，**后人勿再当缺陷修**。
> 护栏：`tests/test_import_parse_closeout_20261005.py` **19 例**（B5 卸载 abort + `AbortController` 写入点→卸载清单**全覆盖扫仓** / B6 重置 / B7 动态标题且**剥注释后不得残留「18 项」** / B8 配额接线与顺序 / **B10 唯一出口 `factsApi.uploadDocuments` 计数==1 + 预检顺序 + 两入口拒绝文案同口径 + import Tab 预检不得摘除** / 四处 200MB 默认值 parity = `Settings.model_fields["upload_max_total_bytes"].default` / `/system/upload-limits` 回传 `max_total_bytes` / B9 渲染条件锁）+ `tests/test_import_parse_r13_closeout_20261005.py` **21 例**（R13 判空与缓存口径）。**A/B 反向验证 6 项全部定向失败**（卸载摘 abort 2 例 / 摘重置 1 / 标题回写死 1 / 摘配额 1 / 摘 partition 1 / 第二入口改名不接 handler 1），单实例锁 + `try/finally` **逐次**还原 + sha256 字节一致断言 + 还原后复跑全绿；脚本落 `%TEMP%\opencode\_ab_*.py` 不入库。前端 `uploadAccept.test.ts` **+6 例**（累计配额闭区间/触顶后全部 held/单份超限/200MB 兜底/NaN fail-soft），`schemeWorkbench.test.tsx` 的 B7 用例改为注入 3 条 defs 断言「3 项结构化提取」并反断言「18 项」不复现。
> **基线对比**：后端开工 5635 → 中段 5661 → **终态 5737 passed / 16 skipped / 1 xfailed / 0 failed（281.20s）**；前端 **56 文件 / 873 passed（186.35s）** + `npx tsc --noEmit` **0 错误**（并行会话同期新增用例使总数上浮，失败数恒 0）。
> ⚠️ **本轮踩坑**：① **PowerShell `Select-String -Path '*.ts','*.tsx'` 默认不递归**（不带 `-Recurse` 会直接报错）→ 前端源码搜索一律用 grep 工具，别图省事；② `Start-Process -FilePath npx` **静默不启动**（npx 是 `.cmd`）→ 用 `cmd /c npx vitest run > file 2> err`，并以「输出文件在涨 + 进程表有 node」确认真的起来了；③ 工具 `ChildProcess.kill` 报错是已知误报，**进程是否启动以输出文件/进程表为准**，不要据报错重跑（否则起两个全量）。
> ⚠️ **未落地（沿用前序结论）**：真实 AI 联调与生产库「修复前后对比测量」仍阻塞（正文需用户在 UI 重新生成）；`review_records.section_id` 删章残留、`PRAGMA foreign_keys=0` 按 R32 结论刻意不动。

> **最近校准：2026-10-05（目录生成模块增强 · R41 · F1~F6 六项收口）** —— **【用户修复清单逐项落地】终态进度事件 · 导出缓存失效漏接线 · 重置并发守卫 · 任务类型冲突互斥 · 日志准确性**（零新增依赖、零新增配置项、零数据迁移、返回契约零变化；**只改事件/缓存/守卫/日志，不改任何 AI 提示词与正文产出逻辑**）：
> ① **【F1 · 终态进度事件缺失】** `generate_outline` / `generate_content` 的**收尾两条 `update_progress`** 未带 `event="completed"` → 前端收不到终态事件，只能靠超时或下一次轮询感知「跑完了」，进度条停在 99%。两条终态写入补 `event="completed"`（与目录/正文 SSE 其余进度事件同口径）。
> ② **【F2/F3 · 导出缓存失效漏接线】** `sections.py` 的结构变更与正文写路径共 **6 处**已接 `invalidate_export_cache`，本轮补齐此前**只清部分派生物**的 2 处（中断清理路径 / 内容清空路径），现共 6 处 `:570/809/890/1030/1226/1635`。⚠️ 根因仍是本仓高频陷阱 ——「同一失效判据在多处各自接线、漏一处」，故以「6 处」为不变量写入护栏，日后新增写路径若漏接即失败。
> ③ **【F4 · 重置正文缺目录并发守卫】** `sections.py::reset_content` 直接清正文，未校验同方案**目录生成**是否在跑（`_tasks` 里的 `outline_generation_in_progress`）→ 目录生成途中被重置，落库结果互相覆盖（与 `update_section` / `delete_section` 已有的 409 守卫**分叉**）。补同口径 409 守卫（`:918-927`），复用既有守卫常量，语义与其它结构变更入口一致。
> ④ **【F5 · 任务类型冲突无互斥（TOCTOU）】** `register_task` 只按 `task_id` 记内存态，**不同任务类型**（目录生成 / 正文生成 / 事实提取）可同时占用同一方案：`generate_outline` 与 `generate_content` 并发时各自写 `sections`，正文落库与目录整表重建交叉 → 章节被覆盖 / 编号错位。修法：`task_registry.py` 新增 `TaskTypeConflict`(L34) 与返回字段 `conflict_types`；`sse_handlers.py` 新增 `_register_task_exclusive`(L3952) 作**唯一互斥出口**，两条 SSE 链路（`:4208` 目录 / `:4942` 正文）改调它，冲突即 409 并回传在跑的任务类型。⚠️ 判据只认**同方案 + 异类型**（同类型重入仍由各自 `*_generation_in_progress` 守卫负责），避免与既有重入守卫重叠拒绝。
> ⑤ **【F6 · 日志准确性（用户清单第 9 项）】** 四类「日志说不清 / 说错话」：**级别口径错位**（`目录生成上下文装配失败（降级为空上下文继续）` 用 `logger.exception` = ERROR，与 AGENTS §3.1.6「ERROR=业务阻断 / WARNING=可恢复」冲突）→ 改 `warning` + `exc_info=True`；**失败日志缺身份**（目录/正文/事实三条顶层 `exception` 与章节级失败一律不带 `scheme_id`/`task_id`/`section_id`，多方案并发只能靠时间窗猜）→ 6 处补全标识；**「失败 checkpoint 失败」双失败歧义**（`保存正文生成失败 checkpoint 失败` 一句两次「失败」，且 13 处各写各的、grep 不中）→ 统一前缀 `检查点写入失败（task=… · <业务> · <场景>）`，**恰 13 处**（`:7663` 的 DEBUG「内存终态补挂 checkpoint 失败（忽略）」按 fail-soft 设计**刻意保留**）；**成功路径零日志**（`finish_task` 是所有任务唯一终态收口点，任务正常完成后日志里查不到任何一行，无法区分「没跑」与「跑完没记录」）→ 补 INFO `任务完成/任务终止（type·scheme·task）`，并以新增的 `_terminal_db_ok` 门控 —— **仅在终态落库成功后记录**，避免与「终态落库失败 ERROR」构成同一事件的矛盾双条。审核/修复链 7 处告警另补 `（scheme=%s）`。
> 护栏：新增 `tests/test_outline_fixes_20261005.py`（**22 例**：终态事件 / 缓存失效 6 处不变量 / 重置 409 守卫 / 任务类型互斥含 TOCTOU 复现）、`tests/test_logging_accuracy_20261005.py`（**25 例**：级别口径 + 6 处身份 + 13 处统一前缀 + 双失败句式归零 + 7 处审核链身份 + `finish_task` 时序门控 + **caplog 行为实证**含「落库失败时不得出现矛盾 INFO」）。**A/B 反向验证 10 项全部定向失败**（F5 五项 + F6 五项：级别退回→2 例、顶层去身份→1 例、checkpoint 句式退回→2 例、去掉 `_terminal_db_ok` 门控→1 例、审核链去 scheme→1 例），单实例锁 + `try/finally` 逐次还原 + sha256 字节一致 + 终态复跑全绿、全仓零残留。
> **基线对比**：本轮开工基线 **5662 passed / 16 skipped / 1 xfailed（224.29s）** → F1~F5 终态 **5707** → F6 终态 **5732 passed / 16 skipped / 1 xfailed / 0 failed（226.02s）**（= 5707 + F6 新增 25 例），**零回归**；关联回归 157 passed（fence 契约 / task_registry / deep_audit / outline 日志 / task 控制语义 / F5 护栏）。
> ⚠️ **本轮三处踩坑（记以免重复）**：① **护栏窗口与信息量冲突**：F6c 给失败日志加标识后把 `exc_info=True` 挤出 `test_content_fence_contract` 的 **800 字符源码窗口**（实测 806）—— 修法是**把该处历史注释块整体移到锚点 `_reason = f"生成异常:` 之前**（内容一字未改），而不是放宽护栏窗口（放宽＝削弱既有回归防线）；② **批量编辑脚本的三个预检缺一不可**：行首锚定（16 空格缩进的模式会被 20 空格行**以子串方式命中**，`count` 报 2 实为误命中）、行尾归一化（`task_registry.py` 是 **CRLF**，`\n` 锚点在 CRLF 文件上一个都匹配不到）、AST 作用域必须用 **`end_lineno` 判包含**（只按 `lineno <= target` 取最大会误中**同级先定义的兄弟函数** `_notify`/`_do_fix`，假报 7 处）；③ **护栏里含 ASCII 引号的字符串字面量必须用单引号**：写成 `"...: %s" ", args"` 时第二个字面量的首 `"` 被当定界符、拼接结果**少一个引号** → 首版 4 例误报（与 §5.14「护栏判据锚点」同族：护栏自身写错比不写更糟）。
> ⚠️ **运维提醒**：F1~F6 全部在后端，需重启后端进程加载新代码（端口无监听时 `start_all.bat` 即生效）。
> ⚠️ **未落地（记录以免重复排查）**：目录树结构编辑的乐观并发（`updated_at` 版本号 CAS）仍缺 —— F4 只补了「目录在跑」的 409 守卫，**未加版本号比对**，两人同时打开同一方案拖拽仍是「后写覆盖」；前端目录树 `treeFingerprint` 轮询粒度与新事件口径需再核一轮。

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







> **最近校准：2026-10-13（R75b 后续需修改项收口 · G6–G9 定向修复与加固）** —— **【用户指令「后续需修改项按最优方案执行修复和加固处理」⇒ R75 §7 遗留项全部收口；零新增依赖、零数据迁移、零判定行为变更（除 G7 不可达死分支改 fail-closed，可达入口行为零变化）**：
> ① **【G6 · 读路径唯一出口权威定义下沉 services（§7.5 建议完成形态）】** `fact_name_value` 本体迁到 `services/facts_extractor.py`，routers `_fact_name_value` 改纯委托薄包装（6 处调用点与护栏锚名不动）；根因：旧定义在 routers 层，跨模块只读桥接 `load_resolved_facts_for_scope` 无法正向引用（routers→services 是唯一合法依赖方向），只能在底层原语上自建第二份回解（G-C 同族）。口径辨析入 docstring：本体=显示/审计口径（保注释）、extract=比较/输入解析口径（剥注释），两套既定职责不是分叉。护栏连锁订正两处锚点漂移（AGENTS 反复记账同族）：R75 护栏 A 组白名单摘掉 `_fact_name_value` + 反向锁改指 services 本体（实测下沉瞬间 1 failed）；R54 `test_bold_line_regex_used_by_one_parser_only` 锚改 services、反向锁 routers 代码行不引用正则名（排除注释行）。新护栏 r75b G6 组 3 例（routers 无副本 AST 锁/桥接必消费本体/包装与本体逐行同值行为锁）。
> ② **【G7 · batch_resolve 不可达死分支换方案：不删也不留地雷 → fail-closed（§7.4 递延项）】** 旧 `else: scope, params = "", fact_ids`（因 :2305 守卫不可达）改显式 `raise HTTPException(400)` —— 不变式做成可执行断言，未来放松前置守卫即 fail-closed 而非静默发无作用域跨域查询。护栏 G7 组 3 例：拼串形态不回流（⚠️ **只扫非注释行** —— 本轮实踩：源码处置注释逐字引用旧形态作反例，全文扫描自咬）+ AST 锁 else 必 raise + 可达入口 400 行为锁（既有安全测试文件从未覆盖的形态）。`ack_fact_stale` 等其余批量端点无同族拼串，核实免重复排查。
> ③ **【G8 · 历史误伤行只读盘点（§7.2 建议脚本落地，零回填）】** `artifacts/r75b_g8/inventory_g8.py`（sqlite URI `mode=ro` 零写语句，护栏锁住只读形态防被改成推断回填工具）。五口径实测：**总 129 行，口径 1（真带 `*(注释)*`）=0、口径 1b（分叉但无注释）=1、带模拟标记但列被清零=0、闸门不变式违规=0、段落行 G-C 影响面=0** —— 无真实历史误伤残留。⚠️ **10-14 复盘订正**：口径 1 首版用「两口径分叉」当「值带注释」的**代理判据，代理不等价** —— `_RE_BOLD_FACT_LINE` 取值组 `(.*)` 不跨换行，多行分组行被**首行截断**同样分叉却无注释可保护，原报告据此把 1 行误述成「G-A 注释保护面」，而库里真带注释的行实为 **0**；现拆口径 1 / 1b 并补护栏 + A/B **M4** 锁判别力（口径 1b 那 1 行是手工分组测试行，非注释抹除受害者，维持不回填）。✅ **10-14 R75c 收口**：口径 1b 暴露的「多行容器行显示口径只取首行、第 2..N 行静默丢」已修复 —— `fact_name_value` 对含换行 content 回退`(title, 完整原文)`，单行粗体行（128/129）逐字不变；盘点脚本 `_COMMENT_MARK_RE` 同步排除粗体对。新护栏 `TestMultiLineRowDisplaysFullContent`（4 例）+ A/B **M5**（退回首行截断 → 3 例定向失败）。
> ④ **【G9 · `_eslint_r75.json` 存根删除（§7.3 工具故障收口）】** DeleteFile 两次 `recordFileDelete returned false` 再现，Test-Path 复核确认文件仍在 ⇒ shell `Remove-Item -LiteralPath` 精确删 + Test-Path=False 实锤（凡删除必复核惯例再验证）。
> ⑤ **【护栏 + A/B 5/5 CAUGHT】** 新护栏 `tests/test_global_facts_closeout_r75b_20261013.py` **12 例/4 组**（G6 契约 3 + G7 fail-closed 3 + G8 只读锁 1 + G8 口径 1 判据锁 1 + R75c 多行容器行 4）；R75 护栏 9→10 例。驱动 `artifacts/_ab_r75b_driver.py`：M1 桥接退回直调 extract / M2 死分支回流无作用域拼串 / M3 包装回流自持切分 / M4 口径 1 退回分叉代理 / M5 多行容器行退回首行截断，逐次还原 restore_ok 全 True、基线绿、汇总 ascii-safe。⚠️ **另两个实踩**：`facts_extractor.py` 是全 LF 仓内孤本，`\r\n` 跨行锚 count=0 直接 MISSED（单行锚不碰行尾惯例再验证）；⚠️ **10-14 复盘实踩③**：**「名字在场」是比「源码含字符串」更隐蔽的假护栏** —— 新护栏首版断言 `"_COMMENT_MARK_RE" in src` / `"cal1b" in src`（锚点指向正确的符号名，人读完全像有效判据），A/B M4 把三元路由改回 `cal1.append(r)` 后**仍 18 例全绿**（定义行与报告文字里的名字都还在）。修法：AST 锁**真实调用**（`_COMMENT_MARK_RE.search` 必在 `main` 调用图内）+ AST 锁**路由**（`cal1b` 必真被 `append`，含 `IfExp` 展开），M4 转 CAUGHT。**新增护栏后必须立刻做一次「把接线改回旧形态」的反向验证，否则不知道自己的护栏是真还是假。**A/B 驱动脚本在 artifacts 下，路径推导要多算一层 dirname（本轮首跑 NotADirectoryError）。
> **验证**：R75+R75b+batch_resolve 安全+R45 四文件 **44 passed**（10-14 复验 **74 passed**，并行会话向 r54/batch_resolve 补 30 例）；事实簇 `-k "fact"` **983 passed/5 skipped/0 failed（47.93s）**（R54 订正前该簇 1 failed 即锚点漂移实锤）；后端全量 **7765 passed/17 skipped/1 xfailed/0 failed（508.97s）**；10-14 复验 **7969 passed / 17 skipped / 1 xfailed / 0 failed（533.47s，rc=0）**零回归（=R75 基线 7711+本轮护栏+并行会话新增）；ruff（F821,E9,F841,F401）5 触碰文件 All passed；7 触碰文件字节体检 BOM=False/FFFD=0/loneCR=0。前端本轮零代码改动。详见 `docs/全局事实读路径口径收口_R75_20261013.md` §8。
> ⚠️ **运维**：需重启后端（`global_facts`/`facts_extractor` 常驻）；无需清库；前端无需重建。⚠️ **monkeypatch 表面迁移**：测试替换读路径回解行为须改 patch `services.fact_name_value`（patch 薄包装会被委托链吸收）；生产无影响。

> **上一轮校准：2026-10-13（全局事实模块 · 读路径 name/value 口径收口 R75）** —— **【用户指定模块深度任务：功能清单/架构审计/缺口清单全量完成后，真实剩余缺口收敛四项 G-A/G-B/G-C/G-D；零新增依赖、零数据迁移、零新增配置项】**：
> ① **【G-A/B/C · 六处 name/value 回解统一走 `_fact_name_value`（global_facts.py :1112/:1482/:1776-82/:1843/:2194/:2506）】** 旧实现直调底层原语 `extract_value_from_markdown_line` 构成读路径第二份切分：adjust 提示词喂 AI 剥注释值（照抄回传即抹注释，R54 §8.2 T4 收口）、裁决/更新旧值被剥注释后与新值比较误判「已换值」（is_simulated 误清零）、危大判定把非粗体段落行整段当名称。写路径保留底层原语（拆用户提交行职责不同，白名单显式登记）。行构造/回解均注意 sqlite Row→dict 转换（:1482/:2194）。
> ② **【G-D · 前端事实诊断 Promise.all → allSettled（SchemeWorkbenchPage.tsx）】** 章节覆盖度与危大阈值两个独立诊断，旧实现任一失败连带丢弃另一侧成果（danger-check 对空方案 400 实证形态）；新增导出纯函数 `mergeFactsDiagnosticsSettled`（成功侧各自落板、双侧 Abort 静默、单侧 Abort 不静默真实失败、null≡展示/undefined≡缺席）+ 竞态守卫保留。
> ③ **【既有护栏锚点订正（护栏失锚形态实锤）】** R45 `test_group_rebuild_still_reads_old_value_from_stored_content` 锁死旧字面量，单一出口迁移即恒红 → 改锁「经 `_fact_name_value` 回解或旧字面量」+补原意图锁（old_val 不得取新值）。
> ④ **【护栏 + A/B 4/4 CAUGHT】** 后端 `tests/test_global_facts_closeout_r75_20261013.py` 9 例/3 组（A 六落点 AST 契约锁+全模块直调白名单+反向锁出口被掏空即红；B 五类脏语料与 list_facts 组合公式逐字 parity；C 新旧出口差异真实存在的前提锁）；前端 `factsDiagnosticsR75.test.ts` 13 例/2 组（纯函数值域 + glob 静态接线锁）。驱动 `artifacts/_ab_r75_driver.py`：M1 adjust 回退旧口径 / M2 裁决旁路 / M3 前端回退 Promise.all / M4 摘成功侧落板，逐次还原 restore_ok=True、汇总 ascii-safe（R73 教训沿用）。
> **验证**：事实簇 **803 passed/1 skipped**（与本轮基线逐字一致零回归）；后端全量单进程 **7711 passed/17 skipped/1 xfailed/0 failed（527.79s）**=R74 复验基线 7702+新护栏 9；前端 Vitest **64 文件/1157 passed**（基线 63/1144+新护栏）；tsc rc=0；eslint **0 errors**；ruff（F821,E9,F841,F401）3 后端文件 All passed；5 触碰文件字节体检无 BOM/FFFD/loneCR。详见 `docs/全局事实读路径口径收口_R75_20261013.md`。
> ⚠️ **运维**：需重启后端（`global_facts` 常驻）；前端需重建；无需清库。⚠️ **遗留**：仓库根 `_eslint_r75.json` 存根待手工删（DeleteFile 两次明确失败 `recordFileDelete returned false`，Test-Path 复核仍在，§10.5 同族再现；**R75b 已收口**）；历史已被误伤行不做自动回填（无法区分人工改值与误清；**R75b G8 已出只读盘点报告：无真实误伤残留**）。

> 本文件定义 AI 编程代理（Coder/Agent）在本仓库工作时的行为边界。参考 OpenBidKit Yibiao 的 AGENTS.md 引入（2026-09-22）。



>



>



> **最近校准：2026-10-04（登记遗留项收口复核 · R40）** —— **【AGENTS 登记清单逐项复核：代码级债已归零，唯一可执行遗留为生产对比测量 + 运维】本轮零生产代码改动，纯复核与运维执行**：
> ① **代码级遗留盘点**：R38-D 五项债（D2/D4/D6/D7/契约全覆盖）与 R39 孤儿数据清零均已在盘闭合（R39 磁盘复核 + 本轮全量复验）；R32 ⚠️仍未落地三项中 ①历史孤儿数据 已随 R39 清零、③磁盘孤儿目录 GC 已 --apply，仅剩 ②`review_records.section_id` 删章残留为**刻意设计保留**（评审留痕可追溯性硬要求，非待修项）。结论：**登记在案的代码级技术债全部归零，无新的建议下一轮清单**。
> ② **生产库判据探针复核（`_r40_db_probe.py`，mode=ro 只读）**：`sections` 实测 88 章 / with_content=**0** / total_chars=**0** → R29/R30/R39 挂起的「修复前后对比测量」**仍阻塞**（判据 `with_content>0 且 total_chars>0` 需用户在 UI 重新生成正文后方可执行，非代理可代做——生成正文依赖真实 AI 调用与额度）；连带核实：`uploaded_outlines` 孤儿 **0**（R39 结论维持）、`preflight_runs` 仅 2 行 10-01 真实总检（无污染行；其 `content_fingerprint` 与当前空正文不一致 → `/submit` 将判「结论已过期」，属 stale 判据正确行为非缺陷）、`global_facts` 2 行、在役项目 5 个。
> ③ **运维**：端口 8000 无监听 → 下次 `start_all.bat` 即加载 R38-D/R39 全部新代码；捕获并清理 **2 个挂死 12~15h 的僵尸 pytest 进程**（PID 6168 跑 charts 专组 / PID 25232 跑 consistency_repair+audit 组，12s 窗口 CPU 增量均 **0s** 确认僵死，R38-L ⑤ 同类先例；僵死进程占用测试临时目录句柄，会干扰后续并行会话的 .pt_* 清理与复跑）。
> **基线对比**：本轮零代码改动 → 全量即收口复验：**5508 passed / 16 skipped / 3 xfailed / 0 failed（208.30s，退出码正常）**（R39 终态 5495 后并行会话新增 13 例全绿，零回归）。探针 `_r40_db_probe.py` / 输出 `_r40_probe_out.txt` / 全量日志 `_r40_full*.txt` 按仓库惯例留存。

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







- 后端：**2026-10-06 审核预检 + 导出双模块收口（R46）实测基线 `6179 passed, 17 skipped, 1 xfailed, 0 failed`（291.59s）** —— = R43 基线 6165 + R46 新增护栏 **14 例**（`tests/test_review_export_closeout_r46_20261006.py`），**零回归**。⚠️ 同轮全量首跑曾暴露 **6 例失败**，全部来自 `git checkout HEAD -- sections.py` 意外丢弃的 R41 未入库修复（F2 `create_section` 导出缓存失效 / F3 `_save_outline_to_db` 空目录分支失效 / F4 `reset_content` 目录生成 409 守卫）；既有 R41 护栏当场抓住并已逐处补回，详见顶部 R46 校准块的事故记录。双文件合跑（`test_outline_fixes_20261005.py + test_review_export_closeout_r46_20261006.py`）= **36 passed / 7.00s**。此前记录的 5140（R32）/ 3697 / 3886 / 3976 / 4023 / 4048 / 4077 / 4313 / 4578 / 4667 / 4706 / 4746 / 4800 / 4871 / 4936 / 4937 / 5012 / 5036 / 5075 / 6165 均已过期 —— 改动时请**以实测为准**、不要沿用文档里的旧数字。



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



