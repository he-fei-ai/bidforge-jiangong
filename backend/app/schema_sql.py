"""数据库建表 SQL（核心表）"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    engineering_type TEXT DEFAULT '',
    location TEXT DEFAULT '',
    client_name TEXT DEFAULT '',
    contractor_name TEXT DEFAULT '',
    project_period TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS schemes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    type TEXT DEFAULT '',
    profession TEXT DEFAULT '',
    status TEXT DEFAULT '草稿',
    -- ✅ 2026-09-17：人工审核状态与编译状态解耦（此前 /submit 直接覆盖 status，
    --    评审通过后方案编译态「草稿/目录已确认」被英文 approved/rejected 覆盖）
    review_status TEXT DEFAULT '',
    word_budget INTEGER DEFAULT 30000,
    word_count INTEGER DEFAULT 0,
    outline_source TEXT DEFAULT '',
    config_json TEXT DEFAULT '{}',
    export_round INTEGER DEFAULT 0,
    -- ✅ 2026-09-26（F-CONTENT-STANDARD）：方案级正文生成标准
    --    precise=精准内容（默认，与既有数据真实性红线口径一致）/ fuzzy=模糊内容。
    generation_standard TEXT DEFAULT 'precise',
    -- ✅ 2026-09-29：最近一次「全局事实变更」时刻（章节失效标记的数据源）。
    --    由 invalidate_export_cache(..., facts_touched=True) 在事实写操作后写入；
    --    章节树读路径据此派生 facts_stale（空串 = 无从判定，不标记任何章节）。
    facts_updated_at TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_schemes_project ON schemes(project_id);

CREATE TABLE IF NOT EXISTS sections (
    id TEXT PRIMARY KEY,
    scheme_id TEXT NOT NULL REFERENCES schemes(id) ON DELETE CASCADE,
    project_id TEXT DEFAULT '',
    parent_id TEXT DEFAULT '',
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    level INTEGER DEFAULT 1,
    status TEXT DEFAULT 'empty',
    word_count INTEGER DEFAULT 0,
    word_budget INTEGER DEFAULT 1500,
    word_status TEXT DEFAULT 'normal',
    content TEXT DEFAULT '',
    outline_json TEXT DEFAULT '',
    chart_predictions TEXT DEFAULT '',
    review_status TEXT DEFAULT '',
    sort_order INTEGER DEFAULT 0,
    locked INTEGER DEFAULT 0,
    -- ✅ 2026-09-26（F-CONTENT-STANDARD）：章节级生成标准。
    --    '' = 沿用方案级设置；precise / fuzzy = 章节覆盖。
    generation_standard TEXT DEFAULT '',
    --    最近一次正文生成实际使用的标准（由生成链路写入，供生成报告确定性复算；
    --    PATCH 章节不接受修改此列）。
    last_generation_standard TEXT DEFAULT '',
    --    最近一次正文生成的校验报告 JSON（standard_report 产物，供前端 Popover 展示）。
    last_generation_report TEXT DEFAULT '',
    -- ✅ 修复（2026-09-18）：以下 8 个内联图表列原先**只存在于 db._migrate**，
    --    不在建表 DDL 里。正常 init_db() 会先 executescript 再 _migrate 故不报错，
    --    但任何只跑 SCHEMA_SQL 的场景会得到缺列的 sections，而 charts.py / export.py
    --    均 `SELECT ... flowchart_json ...` → 运行期 no such column。
    --    声明与迁移**共存**（迁移保留以兼容旧库）。
    flowchart_json TEXT DEFAULT '',
    gantt_json TEXT DEFAULT '',
    architecture_json TEXT DEFAULT '',
    labor_json TEXT DEFAULT '',
    comparison_json TEXT DEFAULT '',
    layout_json TEXT DEFAULT '',
    timeline_json TEXT DEFAULT '',
    inlined_chart_json TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_sections_scheme ON sections(scheme_id);
CREATE INDEX IF NOT EXISTS idx_sections_parent ON sections(parent_id);

CREATE TABLE IF NOT EXISTS outline_library (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT DEFAULT '',
    engineering_type TEXT DEFAULT '',
    profession TEXT DEFAULT '',
    applicable_conditions TEXT DEFAULT '',
    basis TEXT DEFAULT '',
    outline_json TEXT DEFAULT '[]',
    tags TEXT DEFAULT '',
    version TEXT DEFAULT 'v1.0',
    source TEXT DEFAULT '手动创建',
    review_status TEXT DEFAULT '待审核',
    ref_count INTEGER DEFAULT 0,
    created_by TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_outline_library_type ON outline_library(type);

CREATE TABLE IF NOT EXISTS outline_library_versions (
    id TEXT PRIMARY KEY,
    library_id TEXT NOT NULL REFERENCES outline_library(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    outline_json TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
-- ✅ 性能优化（2026-09-24 · 遗留项 #3）：版本列表/回滚查询均按 library_id 访问
--    （outline_library.py L218/L264/L358/L373），此前无索引 → 随归档增长全表扫。
CREATE INDEX IF NOT EXISTS idx_olv_library ON outline_library_versions(library_id);

CREATE TABLE IF NOT EXISTS uploaded_outlines (
    id TEXT PRIMARY KEY,
    project_id TEXT DEFAULT '',
    scheme_id TEXT DEFAULT '',
    file_name TEXT DEFAULT '',
    file_type TEXT DEFAULT '',
    raw_text TEXT DEFAULT '',
    parsed_json TEXT DEFAULT '{}',
    confidence REAL DEFAULT 0,
    status TEXT DEFAULT 'parsed',
    -- ✅ 修复（2026-09-18）：该列原仅由 db._migrate 添加，但 upload_outline.py
    --    直接 INSERT 该列 —— 补进 DDL，与迁移声明保持一致。
    parse_warnings TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS project_documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    file_name TEXT DEFAULT '',
    file_type TEXT DEFAULT '',
    doc_type TEXT DEFAULT '',
    parsed_markdown TEXT DEFAULT '',
    file_path TEXT DEFAULT '',
    doc_category TEXT DEFAULT '',
    file_size INTEGER DEFAULT 0,
    parse_time REAL DEFAULT 0,
    parse_warnings TEXT DEFAULT '',               -- JSON: 解析器诊断告警（截断/OCR 兜底等）
    parse_truncated INTEGER DEFAULT 0,            -- 解析器级截断（PDF 截页/表格截行）持久化标记
    -- ✅ 四层存储架构（规范 §2/§4.2 时效性）：文件指纹 + 版本 + 解析/提取状态
    --   注：旧库仅存本文 DDL 不生效，同名列已全部同步声明进 db._migrate 补列
    file_hash_md5 TEXT DEFAULT '',
    file_hash_sha256 TEXT DEFAULT '',
    page_count INTEGER DEFAULT 0,
    parse_status TEXT DEFAULT 'pending',          -- pending | success | failed（前端 isDocFailed 依赖 'failed'）
    parse_version TEXT DEFAULT 'v1',              -- 解析代次，每次重解析 vN+1
    parsed_at TEXT DEFAULT '',                    -- 本次解析完成时间（ISO8601 UTC）
    parse_duration_ms INTEGER DEFAULT 0,
    parse_engine TEXT DEFAULT '',
    extract_status TEXT DEFAULT 'pending',        -- pending | success
    extract_time TEXT DEFAULT '',
    quality_score REAL DEFAULT -1,                -- 完整性质量评分（-1=未评估）
    completeness_json TEXT DEFAULT '',            -- 解析层覆盖率快照
    expires_at TEXT DEFAULT '',                   -- 解析结果有效期（超期提示重解析）
    status TEXT DEFAULT 'valid',                  -- valid | file_changed | expired | not_parsed（compute_freshness 未解析时回写 not_parsed）
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_project_documents_project ON project_documents(project_id);

-- ===========================================================================
-- ✅ 文件上传解析结果四层存储（规范 §5 数据库设计）
-- ===========================================================================
-- 解析层：内容分块（阶段3），每块携带 source_ref 可回溯原文页码/段落，
-- hash 为块级指纹（增量更新时只重处理变更块）。
-- 【下游消费边界（数据流审计 2026-09-23 文档化）】本表服务于溯源/完整性报告/交叉校验，
-- 目录生成与正文生成不直接读它（生成链读的是 project_documents.parsed_markdown，
-- 经 load_parsed_texts 取回），故它属于「解析/归档层」而非生成时实时数据源。
CREATE TABLE IF NOT EXISTS doc_chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    chunk_type TEXT NOT NULL,                     -- section | page | table | semantic
    title TEXT DEFAULT '',
    level INTEGER DEFAULT 1,
    page_num INTEGER DEFAULT 1,
    text TEXT DEFAULT '',
    source_ref TEXT DEFAULT '',                   -- {doc_id}#page:N#section:标题
    parent_chunk_id TEXT DEFAULT '',              -- 预留列：当前分块为扁平结构（仅 prev/next 链），恒为空串
    prev_chunk_id TEXT DEFAULT '',
    next_chunk_id TEXT DEFAULT '',
    tables_json TEXT DEFAULT '[]',
    images_json TEXT DEFAULT '[]',
    hash TEXT DEFAULT '',
    meta_json TEXT DEFAULT '{}',                  -- formulas/offset/length 等扩展元信息
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_doc_chunks_doc ON doc_chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_doc_chunks_doc_type ON doc_chunks(doc_id, chunk_type);

-- 提取层：AI 提取结果按标准类别存储（project_info/engineering/design_params/
-- geology/standards/boq/global_facts），与磁盘 extracted/{doc_id}_*.json 双份同步。
-- 【下游消费边界（数据流审计 2026-09-23 文档化）】本表为归档层：由 sync_extract_layer 从
-- bid_analysis_items + global_facts 物化而来，供完整性报告/交叉校验/未来回溯使用；
-- 目录与正文生成的实时源仍是 bid_analysis_items 与 global_facts 两张活跃表，不读本表。
CREATE TABLE IF NOT EXISTS doc_extractions (
    extraction_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    project_id TEXT DEFAULT '',
    extract_type TEXT NOT NULL,
    extract_data TEXT NOT NULL DEFAULT '{}',      -- 规范 §2.3 三要素结构（value/confidence/source）
    confidence REAL DEFAULT 0,
    source_refs TEXT DEFAULT '[]',                -- JSON: 溯源引用列表
    extract_time TEXT DEFAULT '',
    extract_engine TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
-- 去重冲突目标用唯一索引表达（而非表内 UNIQUE 子句）：旧库补列路径下
-- ALTER TABLE 无法追加 UNIQUE 列约束，索引两侧都可建、口径一致。
CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_extractions_uniq
    ON doc_extractions(doc_id, extract_type);
CREATE INDEX IF NOT EXISTS idx_doc_extractions_doc ON doc_extractions(doc_id);

-- 校验层：完整性（completeness）/交叉（cross_check）校验报告归档，
-- 支撑「可校验 + 可追溯」（每次跑批落一条，按时间可回退历史）。
CREATE TABLE IF NOT EXISTS doc_validation_reports (
    id TEXT PRIMARY KEY,
    doc_id TEXT DEFAULT '',
    project_id TEXT DEFAULT '',
    kind TEXT NOT NULL,                           -- completeness | cross_check
    report_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_doc_val_reports ON doc_validation_reports(doc_id, kind, created_at);

-- ===========================================================================
-- ✅ 招标文件结构化解析（对齐 OpenBidKit 18 项解析体系）
-- ===========================================================================
-- 招标文件 → 18 项结构化提取的结果存储。每一行是一个解析项的完整输出。
-- 解析项定义统一由后端维护（bid_analysis_service.py），避免前后端双份定义
-- 导致的 Prompt 分叉风险。
CREATE TABLE IF NOT EXISTS bid_analysis_items (
    id TEXT PRIMARY KEY,                          -- {project_id}_{item_id} 格式
    project_id TEXT NOT NULL,
    scheme_id TEXT DEFAULT '',
    item_id TEXT NOT NULL,                        -- 18 个解析项 id（见 bid_analysis_service.ANALYSIS_ITEMS）
    label TEXT DEFAULT '',                        -- 中文名
    output_type TEXT DEFAULT 'markdown',          -- markdown | json
    required INTEGER DEFAULT 0,                   -- 是否必选项（17 个必选项，见 REQUIRED_ITEM_IDS）
    status TEXT DEFAULT 'idle',                   -- idle | running | success | error
    content TEXT DEFAULT '',                      -- Markdown 文本 或 JSON 字符串
    error TEXT DEFAULT '',
    sort_order INTEGER DEFAULT 0,
    source TEXT DEFAULT 'ai',                     -- ai | manual（manual = 人工校正结果）
    evidence TEXT DEFAULT '',                     -- 来源位置 JSON（提取结果反查原文的出处列表，2026-09-23）
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    domain TEXT DEFAULT 'scheme'                 -- 提取域：scheme(方案编制域) | bid_response(招标响应域)
);
CREATE INDEX IF NOT EXISTS idx_bid_analysis_project ON bid_analysis_items(project_id);
CREATE INDEX IF NOT EXISTS idx_bid_analysis_scheme ON bid_analysis_items(scheme_id);
-- ✅ 2026-10-01 修复启动失败：idx_bid_analysis_project_domain 引用 domain 列，而 domain
--     由 _migrate 补列（旧库 CREATE TABLE 被 IF NOT EXISTS 跳过、domain 尚不存在），
--     原索引写在 SCHEMA_SQL 的 executescript 中、早于 _migrate 执行，会对「已存在旧库」
--     抛 "no such column: domain" 致 init_db 整体失败、后端无法启动。故该索引改由
--     db.py::_migrate 在补列之后创建（见 _migrate 内同名块），新旧库均安全幂等。

-- 多标段检测结果（招标文件疑似多标段时存储识别结果供前端展示）
CREATE TABLE IF NOT EXISTS bid_sections (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    scheme_id TEXT DEFAULT '',
    is_multi INTEGER DEFAULT 0,                   -- 是否多标段
    total_declared INTEGER DEFAULT 0,             -- 显式声明的标段总数
    detected_sections TEXT DEFAULT '[]',          -- JSON: 识别到的标段标识 [{unit,title,line_range}]
    selected_section_id TEXT DEFAULT '',          -- 用户选中的本次投标标段 id
    selected_section_title TEXT DEFAULT '',       -- 选中标段标题
    selected_section_json TEXT DEFAULT '',        -- JSON: 选中标段明细（title/headLine/description/evidence）
    status TEXT DEFAULT 'idle',                   -- idle | running | success | error
    error TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_bid_sections_project ON bid_sections(project_id);

CREATE TABLE IF NOT EXISTS global_facts (
    id TEXT PRIMARY KEY,
    project_id TEXT DEFAULT '',
    scheme_id TEXT DEFAULT '',
    group_id TEXT DEFAULT '',
    group_title TEXT DEFAULT '',
    title TEXT DEFAULT '',
    content TEXT DEFAULT '',
    category TEXT DEFAULT '',
    source_ref TEXT DEFAULT '',
    is_simulated INTEGER DEFAULT 0,
    confidence REAL DEFAULT 1.0,
    is_resolved INTEGER DEFAULT 1,
    has_conflict INTEGER DEFAULT 0,
    conflict_keys TEXT DEFAULT '',
    fact_key TEXT DEFAULT '',
    chunk_hash TEXT DEFAULT '',
    -- ✅ 数据流审计 2026-09-23：FactItem 提取期已计算的溯源/语义扩展字段
    --    此前仅存于内存，从未落库（重新拉取即丢失）。补列保留证据链，
    --    为后续按证据类型/页码溯源与审计提供依据（向后兼容，新增列均有默认值）。
    value_unit TEXT DEFAULT '',
    fact_type TEXT DEFAULT '',
    evidence_kind TEXT DEFAULT '',
    page_ref INTEGER,
    zone_type TEXT DEFAULT '',
    is_safety_critical INTEGER DEFAULT 0,
    norm_group TEXT DEFAULT '',
    -- ✅ 2026-09-24：全局事实「九大章节分类体系」四维标注（正交于既有 22 类 category）。
    --    chapter     九大章节归属（overview/basis/plan/technique/safety/personnel/
    --                acceptance/emergency/calc_drawings，空串=未分类）
    --    fact_attr   事实属性（quantitative/qualitative/relation/norm）
    --    source_kind 数据来源（bid_doc/drawing/survey/overall_plan/manual）
    --    is_shared   跨章节共性事实（1=多章节复用，避免重复提取）
    --    新增列均有默认值，向后兼容；历史行由读路径惰性派生兜底。
    chapter TEXT DEFAULT '',
    fact_attr TEXT DEFAULT '',
    source_kind TEXT DEFAULT '',
    is_shared INTEGER DEFAULT 0,
    -- 资料重解析/删除后标记旧事实来源已变化；下游生成必须排除，需重新提取或人工核对。
    is_stale INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_global_facts_scheme ON global_facts(scheme_id);
-- ⚠️ idx_global_facts_chapter 不得写在这里：init_db 先 executescript(SCHEMA_SQL)
--    后跑 _migrate()，2026-09-24 之前的旧库 global_facts 尚无 chapter 列，
--    在此 CREATE INDEX 会直接 "no such column: chapter" 导致应用启动失败
--    （2026-09-25 实测回归）。统一由 db.py::_migrate 在补列之后幂等创建。

-- ✅ 增量提取进度（2026-09-17）：记录每个项目「已成功提取过」的分段指纹
-- （sha1(chunk_text)）。二次提取时按指纹跳过已完成段，只补提取新增/变更/
-- 上次失败的段；文档重解析或删除后指纹自然失配，对应残留行会被清理。
CREATE TABLE IF NOT EXISTS facts_extracted_chunks (
    project_id TEXT NOT NULL,
    scheme_id TEXT NOT NULL DEFAULT '',
    chunk_hash TEXT NOT NULL,
    extracted_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (project_id, scheme_id, chunk_hash)
);
-- group_id 和 is_simulated 索引在迁移完成后由 _migrate_indexes 创建
-- 避免旧表没有这些列时 executescript 失败

-- 对应《产品需求文档》§3.9「知识库与素材库」（路由 /knowledge）。
-- ✅ 业务读写已实现（2026-09 修正注释：旧注释称"读写尚未实现"已过期）——
--    知识条目 CRUD 走 routers/knowledge.py，正文生成链路逐章精选注入
--    （sse_handlers._load_knowledge_rows + build_knowledge_text）。
CREATE TABLE IF NOT EXISTS knowledge_base (
    id TEXT PRIMARY KEY,
    project_id TEXT DEFAULT '',
    scheme_id TEXT DEFAULT '',
    name TEXT NOT NULL,
    usage_hint TEXT DEFAULT '',
    content TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
-- ✅ 性能优化（2026-09-24 · 遗留项 #3）：知识条目 CRUD 与正文生成逐章注入
--    （knowledge.py L26/L31、sse_handlers._load_knowledge_rows）均按
--    project_id(+scheme_id) 过滤，此前无索引 → 条目增多时全表扫。
CREATE INDEX IF NOT EXISTS idx_kb_project_scheme ON knowledge_base(project_id, scheme_id);

CREATE TABLE IF NOT EXISTS chart_predictions (
    id TEXT PRIMARY KEY,
    section_id TEXT NOT NULL,
    scheme_id TEXT DEFAULT '',
    chart_type TEXT DEFAULT '',
    needed INTEGER DEFAULT 0,
    purpose TEXT DEFAULT '',
    priority INTEGER DEFAULT 0,
    status TEXT DEFAULT '',
    data_json TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS compliance_check (
    id TEXT PRIMARY KEY,
    project_id TEXT DEFAULT '',
    scheme_id TEXT DEFAULT '',
    check_type TEXT DEFAULT 'compliance',
    rule_id TEXT DEFAULT '',
    item TEXT DEFAULT '',
    severity TEXT DEFAULT '',
    result TEXT DEFAULT '',
    suggestion TEXT DEFAULT '',
    -- ✅ 根治批次截头（2026-09-23）：一次 /check 调用 = 一个 batch_id。
    --   旧口径用 rowid 连续段锚定批次（created_at 仅秒级精度），两次调用写入
    --   跨秒交错时 rowid 段会错切（多读的批次被截头/混批）。历史行空串，
    --   读取端遇空回退 rowid 锚定（向后兼容，无需数据迁移）。由 db.py::_migrate 补列。
    batch_id TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_compliance_scheme ON compliance_check(scheme_id);

-- ✅ 全文一致性审计（AI 语义判定）—— 由 routers/compliance.py 的
--    /consistency-audit 与 /consistency-audit/{scheme_id}/latest 完整实现：
--    AI 将「项目关键事实」与正文逐项比对，输出 0-100 评分与不一致项清单，
--    持久化到本表；overview 聚合器会读取最近一条并把每条 issue 计入
--    一致性维度（CON-04-1, CON-04-2 …），参与就绪度评分。
--    ⚠️ 请勿当作死表删除：readiness_overview 与 readiness_report 均依赖本表。
CREATE TABLE IF NOT EXISTS consistency_audit (
    id TEXT PRIMARY KEY,
    project_id TEXT DEFAULT '',
    scheme_id TEXT DEFAULT '',
    score REAL DEFAULT 0,
    issues TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
-- ✅ 性能优化（2026-09-24 · 遗留项 #3）：一致性审计「最近一次」与历史列表
--    （compliance.py L414/L434/L719）均按 scheme_id + created_at 倒序取，
--    此前无索引 → 每次扫描都会积累的审计行全部扫一遍。
CREATE INDEX IF NOT EXISTS idx_consistency_audit_scheme
    ON consistency_audit(scheme_id, created_at);

-- ✅ 全文一致性 Agent 修复（F-AGENT-CONSISTENCY-REPAIR v1.0）
--    扫描 → 仲裁 → 定向修复 → 校验 → 确认/回滚，见 routers/consistency_repair.py
CREATE TABLE IF NOT EXISTS consistency_conflicts (
    id TEXT PRIMARY KEY,
    scheme_id TEXT NOT NULL,
    scan_id TEXT NOT NULL,
    conflict_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    topic TEXT,
    occurrences TEXT NOT NULL DEFAULT '[]',
    authoritative_value TEXT,
    authoritative_source TEXT,
    repair_instruction TEXT,
    reason TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cc_scheme_scan ON consistency_conflicts(scheme_id, scan_id);
CREATE INDEX IF NOT EXISTS idx_cc_scheme_status ON consistency_conflicts(scheme_id, status);

-- ✅ 一致性扫描增量缓存（2026-09-22 · P0-1）：
--    正文生成收尾的全文一致性扫描原本**逐章一次 AI 调用**且串行（N 章 = N 次）。
--    这里按「章节正文指纹 + 上下文指纹」缓存该章的 AI 扫描候选，内容未变的章节
--    重跑时直接命中、不再调用 AI —— 改一章只重扫一章。
CREATE TABLE IF NOT EXISTS consistency_scan_cache (
    section_id TEXT PRIMARY KEY,
    scheme_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    rows_json TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_cscan_scheme ON consistency_scan_cache(scheme_id);

CREATE TABLE IF NOT EXISTS consistency_repairs (
    id TEXT PRIMARY KEY,
    scheme_id TEXT NOT NULL,
    scan_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'auto',
    total_conflicts INTEGER DEFAULT 0,
    repaired INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    items TEXT NOT NULL DEFAULT '[]',
    snapshot_id TEXT,
    status TEXT DEFAULT 'pending_confirm',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cr_scheme ON consistency_repairs(scheme_id);

CREATE TABLE IF NOT EXISTS scheme_snapshots (
    id TEXT PRIMARY KEY,
    scheme_id TEXT NOT NULL,
    type TEXT NOT NULL,
    sections TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_scheme ON scheme_snapshots(scheme_id);

CREATE TABLE IF NOT EXISTS export_cache (
    id TEXT PRIMARY KEY,
    project_id TEXT DEFAULT '',
    scheme_id TEXT DEFAULT '',
    config_hash TEXT DEFAULT '',
    content_fingerprint TEXT DEFAULT '',
    cache_key TEXT DEFAULT '',
    result_path TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- ✅ 导出格式预设库（对标 OpenBidKit export_templates / exportFormatPresets）：
--    同一项目（业主/标书）通常有固定的排版格式要求（字体/页眉页脚/封面/编号），
--    把一套导出配置存为命名预设，可在多个方案间一键复用，避免每次重复填写。
--    按 project_id 维度共享；is_default 标记项目默认预设。
CREATE TABLE IF NOT EXISTS export_presets (
    id TEXT PRIMARY KEY,
    project_id TEXT DEFAULT '',
    name TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    is_default INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_export_presets_project ON export_presets(project_id);

CREATE TABLE IF NOT EXISTS ai_config (
    id TEXT PRIMARY KEY,
    provider_name TEXT NOT NULL,
    plan TEXT DEFAULT 'pay_as_you_go',
    api_key_encrypted TEXT DEFAULT '',
    base_url TEXT DEFAULT '',
    model TEXT DEFAULT '',
    max_tokens INTEGER DEFAULT 8192,
    temperature REAL DEFAULT 0.7,
    timeout INTEGER DEFAULT 900,
    concurrency INTEGER DEFAULT 4,
    -- 请求方式：normal（普通请求，等待完整响应）/ stream（流式请求，后端边收边拼）。
    -- 仅影响后端与厂商之间的调用方式，应用侧仍等待完整结果后继续流程。
    request_mode TEXT DEFAULT 'normal',
    -- ✅ 2026-09-23 新增（多环境支持）：环境标签（dev / test / prod …），
    --    空串 = 通用（可被任何环境使用）。旧库经 db._migrate 幂等补列，
    --    历史行全部为空串 → 未设置「当前环境」时行为与引入前完全一致。
    env TEXT DEFAULT '',
    is_active INTEGER DEFAULT 0,
    priority INTEGER DEFAULT 0,
    remark TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
-- 主配置选取（is_active=1 ORDER BY updated_at DESC）与降级链排序
-- （ORDER BY priority ASC, updated_at DESC）都是每次 AI 调用前的热点查询，
-- 原表仅主键索引，配置增多后为全表扫描 + 排序。
CREATE INDEX IF NOT EXISTS idx_ai_config_active ON ai_config(is_active, updated_at);
-- ⚠️ idx_ai_config_priority 不放 SCHEMA_SQL：priority 由 db.py::_migrate 幂等补列，
--    极旧版本库在 executescript 阶段还没有该列，先建索引会启动失败。
--    统一由 _migrate 的 index_migrations 在补列之后创建。

-- 注：原 provider_history 表已移除 —— 其唯一写入点（保存 AI 配置时）已删除，
-- 全库无任何读取方，属死表。旧库若已存在该表不影响运行（无代码引用）。
CREATE TABLE IF NOT EXISTS task_registry (
    id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    project_id TEXT DEFAULT '',
    scheme_id TEXT DEFAULT '',
    status TEXT DEFAULT 'running',
    progress REAL DEFAULT 0,
    message TEXT DEFAULT '',
    checkpoint_json TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS ai_audit_logs (
    id TEXT PRIMARY KEY,
    provider_name TEXT DEFAULT '',
    model TEXT DEFAULT '',
    action TEXT DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    duration REAL DEFAULT 0,
    success INTEGER DEFAULT 1,
    error TEXT DEFAULT '',
    -- ✅ 2026-09-21：业务场景标记（outline_draft/level1/sublevel/review/fix 等），
    --    支撑「一次目录生成烧了多少次调用」的精确度量（AI 调用次数优化 E）。
    scene TEXT DEFAULT '',
    -- ✅ 2026-09-24：可靠性统计粒度（同一 provider 的不同配置/模型/地址独立统计）
    config_id TEXT DEFAULT '',
    base_url TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- ===========================================================================
-- ✅ 2026-09-23 新增：AI 配置变更审计（补齐「配置改动无留痕」缺口）
-- ===========================================================================
-- 背景：ai_audit_logs 只记录「AI 调用」，配置的新增/修改/删除/切换主配置/
--   改降级链/导入/清除密钥完全没有留痕 —— 出问题时无法回答「谁在什么时候
--   把主配置换成了哪条」「Key 是什么时候被清掉的」。
-- 约定：detail 列**只允许写非敏感摘要**（如 key_hint 后 4 位），严禁写入明文 API Key。
CREATE TABLE IF NOT EXISTS ai_config_audit_logs (
    id TEXT PRIMARY KEY,
    action TEXT DEFAULT '',        -- create / update / delete / toggle / fallback_chain / import / clear_key / scene_route
    config_id TEXT DEFAULT '',
    provider_name TEXT DEFAULT '',
    model TEXT DEFAULT '',
    detail TEXT DEFAULT '',        -- 变更摘要（不含明文密钥）
    client_ip TEXT DEFAULT '',
    -- ✅ 2026-09-23 新增：结构化变更快照（JSON），支撑「变更 diff」与「配置回滚」。
    --    结构：{"before": {...非敏感字段...}, "after": {...}, "has_key": 0/1}
    --    硬约束：**只允许存非敏感字段**（provider/plan/base_url/model/数值/request_mode/
    --    env/remark + has_key 标记），严禁写入明文或密文 API Key。
    --    历史行为空串 = 无快照（仅能看 detail 摘要），读取端须容忍。
    snapshot_json TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- ✅ 2026-09-23 新增：场景 → 配置 的模型路由（补齐「多模型/模型路由」缺口）
-- 语义：某个业务场景（scene，如 content_draft / facts_extract）指定使用某条
--   ai_config 配置；表为空（默认）时所有场景依旧共用「当前使用」配置，
--   即默认行为与引入该功能前完全一致。
CREATE TABLE IF NOT EXISTS ai_scene_routes (
    scene TEXT PRIMARY KEY,
    config_id TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

-- ✅ 2026-09-23 新增：运行时可变设置（极简键值表）。
-- 背景：`settings.*` 来自环境变量，改了必须重启；而「当前生效环境」（多环境切换）
--   需要在界面上即时生效。本表只承载**运行时可改**的少量设置：
--   * 行不存在 → 回落 settings 中的同名配置项（= 旧行为，向后兼容）；
--   * 行存在（含值为空串）→ 以表内值为准（空串是「显式选择通用环境」的合法取值）。
CREATE TABLE IF NOT EXISTS ai_runtime_settings (
    key TEXT PRIMARY KEY,
    value TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

-- ✅ 2026-10-05 新增：AI Provider 运行时状态持久化（补齐「重启即遗忘」缺口）
-- 背景：`_quota_cool_until` / `_quota_last_probe` 与
--   ``AnalysisCircuitBreaker._per_provider`` 是**进程内 dict**，重启即清零 ——
--   实测生产环境一次配额冷却期（默认 120s）内的进程重启会让冷却「蒸发」，
--   下一次调用直接又烧一次 429；熔断器状态同理（刚被熔断的 provider 重启即变
--   CLOSED，导致死配置在第一次调用前没被降级链剔除）。
--   可靠性统计已有 warmup_reliability_from_db（从 ai_audit_logs 24h 聚合），
--   但「当前状态」类字段（冷却时刻、熔断窗口、探测时刻）无法从审计反推。
-- 约定：
--   * kind='quota_cooldown'   payload_json={"cool_until": <ts>, "last_probe": <ts>}
--   * kind='circuit_breaker'  payload_json={"state","failures","opened_at",
--                            "effective_cooldown","last_failure_at"}
--   * updated_at 用于清理长期未更新的陈旧行（flush 时 DELETE WHERE updated_at < now - 24h）。
--   * 表缺失时读写路径全部静默降级（DEBUG 日志），绝不影响服务启动。
CREATE TABLE IF NOT EXISTS ai_provider_state (
    provider_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider_name, kind)
);
CREATE INDEX IF NOT EXISTS idx_ai_provider_state_kind
    ON ai_provider_state(kind, updated_at);

CREATE TABLE IF NOT EXISTS prompt_templates (
    key TEXT PRIMARY KEY,
    -- 以下三列为**遗留冗余，已停止写入**（2026-09-14）：全库无任何读取方 ——
    -- 提示词的分类/名称来自 `_registry._ALL_PROMPTS` 的 Python 常量，
    -- 出厂默认基线同样是常量 `get_default_prompt()`，DB 里再存一份纯属重复。
    -- 保留列定义仅为兼容已有旧库（不写不影响运行），勿重新启用。
    category TEXT DEFAULT '',
    label TEXT DEFAULT '',
    content TEXT DEFAULT '',
    default_content TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 提示词配置变更审计：仅存内容指纹与变量集合，不复制完整提示词正文。
CREATE TABLE IF NOT EXISTS prompt_audit_logs (
    id TEXT PRIMARY KEY,
    prompt_key TEXT NOT NULL,
    action TEXT DEFAULT '',
    before_hash TEXT DEFAULT '',
    after_hash TEXT DEFAULT '',
    variables_before TEXT DEFAULT '[]',
    variables_after TEXT DEFAULT '[]',
    client_ip TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);


-- 性能索引
CREATE INDEX IF NOT EXISTS idx_sections_scheme_sort ON sections(scheme_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_sections_parent ON sections(parent_id);
CREATE INDEX IF NOT EXISTS idx_chart_pred_scheme ON chart_predictions(scheme_id);
CREATE INDEX IF NOT EXISTS idx_chart_pred_section_type ON chart_predictions(section_id, chart_type);
CREATE INDEX IF NOT EXISTS idx_export_cache_lookup ON export_cache(scheme_id, config_hash, content_fingerprint);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON ai_audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_ai_cfg_audit_created ON ai_config_audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_ai_cfg_audit_config ON ai_config_audit_logs(config_id);
CREATE INDEX IF NOT EXISTS idx_prompt_audit_key_created ON prompt_audit_logs(prompt_key, created_at);
CREATE INDEX IF NOT EXISTS idx_task_registry_status ON task_registry(status);
-- 性能优化（深度审计新增）：高频复合查询索引
CREATE INDEX IF NOT EXISTS idx_chart_pred_scheme_status ON chart_predictions(scheme_id, status);
CREATE INDEX IF NOT EXISTS idx_global_facts_scheme_conflict ON global_facts(scheme_id, has_conflict);
CREATE INDEX IF NOT EXISTS idx_task_registry_scheme_status ON task_registry(scheme_id, status);

-- ===========================================================================
-- ✅ 审核与预检（商业级增强）
-- ===========================================================================
-- 预检运行记录：每次「一键总检」落一条，用于
--   ① 分数趋势对比（本次 vs 上次，整改是否有成效）；
--   ② 审计留痕（谁在什么时候、按哪版规则、判定了什么结论）。
-- 此前 compliance_check 只存 AI 判定明细，既无"总分"也无法回溯单次运行。
CREATE TABLE IF NOT EXISTS preflight_runs (
    id TEXT PRIMARY KEY,
    scheme_id TEXT NOT NULL,
    -- ✅ G4/G5（2026-09-21）：项目维度审计追溯。此前 compliance_check 有该列而
    -- 本表没有，"这个项目有几个方案过了审"只能 JOIN schemes，口径与
    -- compliance_check / consistency_audit 不一致。历史行由 db.py::_migrate 补列。
    project_id TEXT DEFAULT '',
    -- ✅ G3（2026-09-21）：正文/图表/字数指纹。判定"这份结论是否已过期"的唯一
    -- 依据（此前只有 /submit 的门控会 422，看板与报告里看到的永远是旧结论）。
    content_fingerprint TEXT DEFAULT '',
    rule_version TEXT DEFAULT '',
    total REAL DEFAULT 0,
    grade TEXT DEFAULT '',
    verdict TEXT DEFAULT '',
    released INTEGER DEFAULT 0,
    blocked INTEGER DEFAULT 0,
    counts TEXT DEFAULT '{}',          -- JSON: {block,high,medium,low,total}
    dimensions TEXT DEFAULT '[]',      -- JSON: 各维度得分
    findings TEXT DEFAULT '[]',        -- JSON: 完整发现清单
    stats TEXT DEFAULT '{}',           -- JSON: 字数/章节/图表等客观统计
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_preflight_scheme ON preflight_runs(scheme_id, created_at);
-- ✅ BUG 修复（2026-09-21）：此前只有 (scheme_id, created_at) 复合索引，
--    run_consistency_audit / readiness_report / _persist_run 里频繁按单列
--    scheme_id 查询/插入。SQLite 会用复合索引的最左前缀命中，但显式加一条
--    单列索引让查询计划更明确，也让运维 EXPLAIN 时看得懂。
CREATE INDEX IF NOT EXISTS idx_preflight_scheme_only ON preflight_runs(scheme_id);

-- 章节审核工作流：《产品需求文档》§3.12.5 规划的状态机
--   pending（待审核）→ reviewing（审核中）→ approved（已通过）/ rejected（已驳回）
-- 此前 sections.review_status 是无写入方的死字段，状态机从未落地；
-- review_records 同时承担评审留痕（谁、何时、什么意见、结论），
-- 这是"可追溯"在工程软件里的硬性要求（评审意见要能追溯到人与时间）。
CREATE TABLE IF NOT EXISTS review_records (
    id TEXT PRIMARY KEY,
    scheme_id TEXT NOT NULL,
    -- ✅ G4/G5（2026-09-21）：项目维度审计追溯（同 preflight_runs 说明）。
    project_id TEXT DEFAULT '',
    section_id TEXT DEFAULT '',        -- 空串表示方案级评审
    section_title TEXT DEFAULT '',
    from_status TEXT DEFAULT '',
    to_status TEXT NOT NULL,
    reviewer TEXT DEFAULT '',
    comment TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_review_scheme ON review_records(scheme_id, created_at);
CREATE INDEX IF NOT EXISTS idx_review_section ON review_records(section_id);

-- ✅ 《待补充清单》监控基线（2026-09-24，六层方案第 6 层：统计与回归）：
--    每次导出预检（POST /export/check）落一行占位符统计快照，
--    供「优化前后对比 total 是否下降」的趋势追踪（GET /export/placeholder-history）。
--    只增不改、按 scheme 保留最近 50 行（写入端裁剪），纯监控旁路：
--    写入失败只记日志，绝不阻断预检主流程。
CREATE TABLE IF NOT EXISTS placeholder_baselines (
    id TEXT PRIMARY KEY,
    scheme_id TEXT NOT NULL REFERENCES schemes(id) ON DELETE CASCADE,
    total INTEGER DEFAULT 0,
    formatted_total INTEGER DEFAULT 0,
    bare_total INTEGER DEFAULT 0,
    fuzzy_total INTEGER DEFAULT 0,
    field_count INTEGER DEFAULT 0,
    section_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_placeholder_baselines_scheme
    ON placeholder_baselines(scheme_id, created_at);
"""
