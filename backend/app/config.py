"""配置管理（pydantic-settings）"""
from pathlib import Path
from pydantic import ConfigDict, field_validator
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR.parent / "logs"
EXPORTS_DIR = DATA_DIR / "_exports"
CHARTS_DIR = EXPORTS_DIR / "charts"
FACT_UPLOADS_DIR = DATA_DIR / "uploads" / "facts"

# 版本唯一来源（FastAPI version + /health 接口共用）
APP_VERSION = "5.3.0"


class Settings(BaseSettings):
    database_url: str = f"sqlite+aiosqlite:///{DATA_DIR / 'scheme_assistant.db'}"
    fernet_key: str = ""
    tesseract_path: str = ""
    tesseract_data_path: str = ""

    # ---------- OCR（扫描件 / 图片型资料识别） ----------
    ocr_enabled: bool = True
    # auto | tesseract | rapidocr | vision | off
    #   auto      : 本地引擎(tesseract→RapidOCR) → 视觉大模型，依次尝试
    #   rapidocr  : 纯 pip 安装、无需系统二进制，Windows 零配置推荐
    #   vision    : 复用 AI 配置中的视觉模型（无需任何本地依赖）
    ocr_engine: str = "auto"
    ocr_lang: str = "chi_sim+eng"      # tesseract 语言包（需含 chi_sim）
    ocr_pdf_max_pages: int = 20        # 扫描件 PDF 的 OCR 页数上限
    ocr_pdf_dpi: int = 200             # PDF 渲染 DPI（越高越准、越慢）
    ocr_min_chars_per_page: int = 30   # 平均每页字符数低于该值 → 判定为扫描件

    # ---------- PDF 文本层解析页数上限（2026-09-30 第十四轮） ----------
    # ⚠️ 此前 MAX_PDF_PAGES 是 file_parser 里的**硬编码 50**，不可配置：
    #    而招标文件 / 施工组织设计常见 100~400 页 —— 一份 300 页的招标文件
    #    只提取前 50 页，**后面 250 页的工程参数、清单、图纸说明全部丢失**，
    #    且这些内容不会进入任何一级（目录 / 正文 / 事实 / 导出）。
    #    配合同仓 MAX_PARSED_CHARS=400000 的落库上限（约 800 字/页 ≈ 500 页），
    #    此处取 500：两者口径对齐，不会出现「解析了却又在落库环节截断」的
    #    二次浪费。逐页容错已就位（单页失败只跳过该页），提高上限不降稳健性。
    #    ⚠️ 显式设小该值即可恢复「只取前 N 页」的旧行为。
    pdf_text_max_pages: int = 500

    # ---------- MinerU 云端解析（扫描件 / 复杂 PDF 兜底，2026-09-17） ----------
    # provider 为空 = 关闭云端兜底；可选值：
    #   "agent"    MinerU-Agent 轻量解析（v1 API，免 Token，按 IP 限频）
    #   "accurate" MinerU 精准解析（v4 API，需 mineru_token）
    # 触发时机：PDF 判定无可选文字层（扫描件）且本地 OCR 兜底不可用/产出过少时，
    # 自动走云端解析；配置为空时保持纯本地行为（与旧版一致）。
    mineru_provider: str = ""
    mineru_token: str = ""
    mineru_agent_timeout: int = 300      # Agent 轮询超时（秒）
    mineru_accurate_timeout: int = 600   # 精准解析轮询超时（秒）
    mineru_poll_interval: float = 3.0    # 轮询间隔（秒）
    mineru_enabled: bool = True          # 总开关（provider 非空时才生效）
    # ✅ 增强（2026-09-23，审计报告 §4-4）：云端对单图大小常有限制（参考实现
    #    agent 模式约 100MB）。超过则直接跳过云端兜底、本地又无 OCR → 抛 ParseError
    #    让用户知道（而非把超大图塞给云端触发 413/超时）。默认 100MB，正常扫描件
    #    图片远低于此，不影响既有行为；设为 0 表示不限制。
    mineru_image_max_bytes: int = 100 * 1024 * 1024

    # ---------- 旧版 Word（.doc/.wps）解析（2026-09-22，对齐易标解析模块） ----------
    # 旧版 Word 是 OLE 复合文档，python-docx / OOXML 通道读不了，必须先用本地
    # Office 组件转成 .docx。参考 OpenBidKit：
    #   client/electron/services/doc2markdown/convert.mjs::withLegacyWordDocxFile
    #     └ LibreOffice(--convert-to docx) 优先；Windows 上再回退 Word/WPS COM
    # 转换成功后复用既有 `file_parser._parse_docx`，不新增第二套解析实现。
    # legacy_office_enabled=False 时行为与旧版本一致（.doc/.wps 解析报错，
    # 但上传仍被接受并给出可操作提示）。
    legacy_office_enabled: bool = True
    legacy_office_path: str = ""         # 显式指定 soffice 可执行文件（留空自动探测）
    legacy_office_timeout: int = 120     # 单后端转换超时（秒）
    legacy_office_com_enabled: bool = True   # Windows 下是否尝试 Word/WPS COM 转换

    # ---------- 事实提取·信息调用完整性（2026-09-23 审计报告 §4-4） ----------
    # 提取事实默认 is_resolved=0（待审核）才会被注入 目录/正文/导出（门控
    # `has_conflict=0 AND is_resolved=1`）。这是「模拟值闸门」安全不变式，默认保留：
    # 提取结果需经人工确认后才进入交付文档。但这意味着「提取完成」后若不手动逐条
    # 确认，结构化事实对下游生成完全不可见（有效信息未被完整调用）。
    # 开启本开关后，对【非模拟值、无矛盾】的提取事实在落库时自动置 is_resolved=1，
    # 无需人工逐条确认即可进入生成链路。模拟值（is_simulated）与矛盾值（has_conflict）
    # 仍保持待审核，不会被自动放行——安全闸门对高风险项依然生效。
    # 默认 False = 完全向后兼容（沿用既有「需人工确认」行为）。
    auto_resolve_extracted_facts: bool = False

    agnes_api_key: str = ""
    agnes_base_url: str = "https://api.agnes-ai.cn/v1"
    agnes_model: str = "agnes-2.5-flash"

    # ---------- 图片生成配置（自招投标方案平台移植） ----------
    # 支持：agnes / dashscope / volcengine / hunyuan / zhipu / stepfun / baidu
    image_provider: str = "agnes"
    image_model: str = ""            # 留空用各厂商默认模型
    image_base_url: str = ""         # 留空用各厂商默认端点
    image_api_key: str = ""          # 留空时回退到文本模型 key（Agnes 可免 key）
    image_enabled: bool = True
    image_default_size: str = "2K"   # 1K/2K/3K/4K
    image_quality: str = "standard"
    image_max_concurrency: int = 2
    # ---------- 导出远程图片下载安全配置 ----------
    # 仅允许 HTTP(S)；下载有明确超时、响应体大小和重定向次数上限。
    # localhost 默认允许以兼容本地开发/自建图片服务；RFC1918、链路本地和元数据地址仍拒绝。
    image_download_allow_localhost: bool = True
    image_download_timeout: float = 30.0
    image_download_max_bytes: int = 20 * 1024 * 1024
    image_download_max_redirects: int = 3
    image_max_per_project: int = 30
    image_price_per_image: float = 0.0
    # ---------- AI 配图全局预算（G2 · 2026-09-30，默认关闭、零迁移） ----------
    # 上游设计（OpenBidKit 易标《标书智能体（六）》）：AI 可提名很多生图候选，
    # 但最终只按 maxAiImages 择优执行；且把候选小节「分段」，在每一段里选优先级
    # 最高的，避免前面章节把图片额度全部用完（前文 20 个候选、限 6 张时，前面
    # 几章不应独占 6 张，后面章节完全无图）。
    # 本仓 AI 生图在导出时由 _auto_generate_ai_image_blocks 自动触发（每个
    # ```ai_image``` 占位码=一张图）。此处新增全局上限：
    #   <= 0  : 不限制（沿用旧行为，向后兼容）
    #   >  0  : 按文档位置分段择优，全文累计生图数不超过该值；
    #           分段逻辑在 image_engine.apply_image_budget（纯函数、可测）。
    # 注意：Mermaid 配图另有「每章≤1 图表块」硬约束（content.py），本预算
    # 只约束 AI 生图，二者正交、互不干扰。
    max_ai_images: int = 0
    # ---------- 导出补充附录（D-3 · 2026-10-01，默认关闭、零迁移） ----------
    # 数据链断点修复：`knowledge_base`（知识库条目）与 `doc_extractions`
    # （四层存储的提取成果）此前**从未被导出读取** —— 唯一消费点分别是正文生成
    # 注入（sse_handlers:1515）与 doc_pipeline 的完整性报告，用户在软件里维护的
    # 知识库与解析提取成果在交付文档中完全不可见。
    # 开启后，导出 DOCX/PDF 在「附录：项目关键事实」之后追加一份
    # 「附录：参考资料与提取成果」（按来源分组的二维表）。
    # 默认 False：不渲染、产物与旧版逐字一致（向后兼容）。
    export_appendix_sources: bool = False
    # ✅ P4（2026-09-23）：AI 配图人工入口门控。v17 产品约束为「图表全自动生成，
    #    无人工生图入口」——导出 DOCX 时由 _auto_generate_ai_image_blocks 自动生成。
    #    默认 False：/charts/generate-ai-image（兼容保留端点）拒绝人工/脚本触发，
    #    返回 409 说明已全自动；设为 true（AI_IMAGE_MANUAL_ENABLED=true）可恢复
    #    旧版手动重试链路（向后兼容，仅显式开启）。
    ai_image_manual_enabled: bool = False

    proxy_host: str = ""
    proxy_port: str = ""

    # 允许所有 origin（开发模式 + Vite proxy 同源转发，不会真的跨域）
    cors_origins: str = "*"

    # ---------- 可选 API 鉴权 ----------
    # 为空 = 关闭鉴权（本地单机默认，兼容既有前端与既有调用方式）；
    # 非空 = 所有 /api/v1/* 请求必须携带 X-API-Key 或 Authorization: Bearer <token>。
    # 适用于把服务暴露到内网 / 公网时防止未授权访问与 AI Key 滥用。
    api_auth_token: str = ""
    # 可信反向代理直连 IP 白名单（逗号分隔）。默认空 = 不信任 X-Forwarded-For，
    # 配置审计只记录 ASGI 直连对端，避免客户端伪造来源 IP。
    trusted_proxy_ips: str = ""

    # SSE / 超时
    sse_total_timeout_default: int = 1800
    sse_total_timeout_hard_max: int = 14400
    ai_request_timeout: int = 900

    # ---------- AI 调用链性能参数（正文生成链路，2026-09-17） ----------
    # 对冲请求：主候选超过 ai_hedge_delay_seconds 仍无响应时，并行启动下一候选，
    # 先成功者胜并取消其余在途调用。用于消除"慢候选长尾"（实测 p90 181s）。
    ai_hedge_enabled: bool = True
    ai_hedge_delay_seconds: float = 20.0
    # 降级候选链长度上限（实测曾达 13 个，失败时被逐个串行尝试 → 超时放大）。
    ai_fallback_chain_max: int = 3
    # 降级候选（非主候选）单次尝试的超时上限：避免调用方的长超时（300s）
    # 被下发到每一个降级候选；主候选不受此限制（长正文生成需要完整预算）。
    ai_fallback_attempt_timeout: int = 120
    # ✅ 2026-09-22（调用次数优化 O9）：配额/认证/模型不存在等「确定性错误」
    #    （HTTP 402/403/404、Insufficient Balance、UnsupportedModel 等）重试
    #    100% 无用（运行库实测 265 次纯浪费）。False（默认）= 章节级重试循环
    #    识别后直接放弃本章节（不重试）；True = 旧行为（任何错误都按原次数重试）。
    ai_retry_on_quota_error: bool = False
    # ✅ R4 修复（2026-09-22）：流式请求命中确定性错误（402/403/404/quota/invalid key 等）时
    # 是否走普通请求重试。True（默认）= 跳过普通请求重试，直接抛出让上层候选链切换；
    # 因为对同一 provider 而言确定性错误非流式同样 100% 失败，重试白烧一次 HTTP。
    # False = 旧行为（一律回退普通请求重试）。运行库实测 265 次这类重试纯浪费。
    ai_retry_on_non_retryable: bool = True
    # ✅ 2026-09-22（调用次数优化 O2）：429/402/403/404 等配额类错误命中后，
    #    该 provider 进入冷却窗口，窗口内的新调用直接跳过（不再发请求打配额）；
    #    每 ai_fail_probe_every_seconds 秒放行一次探测，配额恢复后自动接回。
    #    0 = 关闭冷却（旧行为：配额错误后照常尝试，仅受熔断器约束）。
    ai_fail_cooldown_seconds: int = 120
    ai_fail_probe_every_seconds: int = 10
    # ✅ 2026-09-22（调用次数优化 O3）：推理模型把 max_tokens 全部耗在思考
    #    过程（finish_reason=length、正文为空）时，自动翻倍 max_tokens 重试一次
    #    （上限 ai_reasoning_max_tokens）。仅「正文为空」触发，不影响正常响应。
    ai_reasoning_max_tokens: int = 4096
    ai_retry_on_thinking_exhausted: bool = True
    # ✅ 2026-09-22（对齐 OpenBidKit reasoning_effort 能力）：推理类模型
    #    （DeepSeek-R1 / o1 / QwQ 等）的「思考力度」透传。空串（默认）= 不发送该参数，
    #    沿用各 provider 默认（完全向后兼容，不改变任何现有调用行为）；非空前将作为
    #    ``extra_body["reasoning_effort"]`` 随每次 AI 调用下发（OpenAI 兼容协议）。
    #    注意：reasoning 模型通常要求 temperature=1 或不传温度，与 content_temperature
    #    共用时需用户自行保证二者不冲突。
    ai_reasoning_effort: str = ""
    # ✅ 2026-09-22（调用次数优化 O5）：流式能力记忆 TTL。模型对 stream 的
    #    支持不会一天内变化，3600s → 86400s 可把「每章白跑一次必败流式」的
    #    重探测频率降 24 倍；TTL 到期仍会自动重探测（平台修复可自愈），
    #    流式成功立即清除记忆。
    ai_stream_capability_ttl_seconds: int = 86400
    # 实时成功率门限：主配置低于 ai_provider_demote_success_rate 时被后置到候选链尾部；
    # 任何候选低于 ai_provider_dead_success_rate（且样本达标）时视为死配置直接剔除。
    ai_provider_demote_success_rate: float = 0.60
    ai_provider_dead_success_rate: float = 0.20

    # 并发
    # ✅ P0 修复 2026-09-17：全局 AI 并发上限严格 ≤5。此前 max_concurrency=8，
    #    加上各处自己的信号量（bid_analysis=2, 分段=3, facts=12），极限可达 20+
    #    并发，导致 AI 服务大规模 429 限流。现统一收敛到 5 上限。
    max_concurrency: int = 5
    default_concurrency: int = 3
    # ✅ 2026-09-20 增强（目录生成 P0 性能）：长方案分步链路逐章子目录的
    #    **批内并发数**。旧实现纯串行（30 章 × ~45s ≈ 22 分钟）。分批并发
    #    （批内并发、批间串行）在保留「跨章去重上下文」的同时把该阶段耗时
    #    降到约 1/K。<=0 视为 1（串行），读取侧统一 max(1, ...) 钳制并留注释。
    outline_chapter_concurrency: int = 2
    # ✅ 2026-09-22（调用次数优化 O7）：正文链路并发默认 3 → 2。基线实测
    #    （同一批语料）并发 2 时调用数只多 ~11%、墙钟慢 ~1.75 倍，但并发峰值
    #    下降可显著缓解 429 限流（运行库单日 769 次 429 的直接诱因）。
    #    旧行为可用环境变量显式恢复（OUTLINE_CHAPTER_CONCURRENCY=3 等）。

    # ---------- 目录生成 AI 调用次数优化（2026-09-21，方案见
    #     「目录生成AI调用次数优化方案_20260921.md」） ----------
    # 审核模式：
    #   auto   = 编制要求/危大必备章先跑程序化覆盖检查：全过则跳过 AI 审核
    #            （省 1~2 次调用）；有缺失则外科式补齐（1 次小调用替代
    #            「AI 审核 + 整目录重写」2 次大调用）；补齐失败自动回退完整 AI 审核。
    #   always = 总是走完整 AI 审核（旧行为）。
    outline_review_mode: str = "auto"
    # 长方案逐章子目录的「单次调用合并章数」：1 = 每章一次调用（默认，质量最稳，
    # 与旧行为完全一致）；>1 时一次调用生成 k 章的二三级目录（调用次数降为
    # ⌈N/k⌉），代价是批内跨章去重上下文变弱，建议仅在强模型下开启。
    outline_chapter_batch_size: int = 1

    # ---------- AI 自然语言调整（引入自参考软件目录/事实调整能力，2026-09-22） ----------
    # 目录 / 全局事实「AI 自然语言调整」单次调用的超时（秒）。这两个端点是
    # 新增的 opt-in 能力（不落库、返回待确认结果），默认值不影响任何既有链路；
    # 仅在弱模型/慢链路上调大可放宽调整请求，调小则更快失败回退给用户重试。
    ai_adjust_timeout: int = 120

    # ---------- 目录生成 · 超时与规模阈值配置化（2026-09-26） ----------
    # ✅ 收口「配了不生效 / 想调只能改代码」：这 6 项此前是 sse_handlers 里的
    #    模块级硬编码字面量，运维只能改源码调参。现绑定 Settings，
    #    **默认值与原硬编码逐字相同**（180/60/120/50000/0.8/45.0）：
    #    不设环境变量时行为与旧版完全一致，仅显式设环境变量时才改变。
    # 单次目录 AI 调用（短方案直出 / 一级目录 / 单章二三级）的 provider 超时（秒）。
    # 弱模型或慢链路（长降级链）下可上调；调小会更快失败并切下一个候选。
    outline_request_timeout: int = 180
    # 目录审核（AI 审核）单次调用超时（秒）；超时按「跳过审核直接完成」降级。
    outline_review_timeout: int = 60
    # 目录审核修复（按建议重写整份目录）单次调用超时（秒）；
    # 超时按「保留原目录」降级——宁可未修复，不可丢目录。
    outline_fix_timeout: int = 120
    # 分步生成阈值：字数预算 > 此值走「一级目录 → 逐章二三级 → 审核」分步链路，
    # 否则一次性直出。调小 => 更多方案走分步（更稳但调用次数更多）；
    # 调大 => 更多方案一次性直出（更快但弱模型下更易截断）。
    outline_stepwise_min_words: int = 50000
    # 修复结果相对原目录的最小节点覆盖率：低于该比例判为「模型截断/敷衍」，
    # 保留原目录（见 _outline_fix_looks_degraded）。
    outline_fix_min_coverage: float = 0.8
    # 单章子目录生成的出厂预期耗时（秒），用于进度渐近填充；
    # 运行中会被本任务实测 EMA 校准覆盖，只是首个样本前的初值。
    outline_chapter_expect_seconds: float = 45.0
    # ✅ P2-2（2026-09-27 · 补齐遗留项）：短方案一次性直出的目录节点数上限。
    #    原为 ``_validate_outline`` 的默认形参 500（写死在 services 层、无法调整）：
    #    30 章 × 20 节点的紧凑三级目录会触碰上限并被判非法 → 触发无意义的 JSON
    #    修复轮。修复轮早已放宽到 OUTLINE_FIX_MAX_NODES=1200，生成/修复两侧口径不一致。
    #    默认 500 = 与修复前**逐字相同**（不改任何默认行为），仅暴露为可调项：
    #    弱模型/大方案场景可设 OUTLINE_GENERATE_MAX_NODES=1200 与修复轮对齐。
    outline_generate_max_nodes: int = 500
    # ✅ P1-3（2026-09-27）：把 outline_templates 匹配到的**标准章节模板**注入
    #    目录提示词（24 套模板此前解析出却从未使用）。默认 True = 生效；
    #    置 False 回退到"仅用通用九大章节骨架"的旧行为。
    outline_template_inject: bool = True

    # ---------- 正文生成·全文一致性扫描优化（2026-09-22，P0-1） ----------
    # 扫描的「单次调用合并章数」：1 = 逐章一次调用（旧行为，质量最稳）；
    # >1 时一次调用扫描 k 章（调用次数降为 ⌈N/k⌉），代价是批内上下文变长。
    # 批结果结构不合法（缺 section_id / 归属到批次外章节）时**整批作废并按章回退**，
    # 绝不将就（与目录生成的「整批判死」防线同口径）。
    consistency_scan_batch_size: int = 4
    # 扫描批之间的并发数（旧实现纯串行）。<=0 视为 1。
    consistency_scan_concurrency: int = 2
    # ✅ 2026-09-22（O7）：扫描并发默认 3 → 2（理由同 outline_chapter_concurrency）。

    # ---------- 正文生成·全文一致性定向修复优化（2026-09-22） ----------
    # 修复按「涉及章节」分组，旧实现逐章串行调用（N 章 = N 次且串行等待）。
    # 并发只降墙钟、不降调用数；0 或负数视为 1（串行）。
    consistency_repair_concurrency: int = 2
    # ✅ 2026-09-22（O7）：修复并发默认 3 → 2（理由同 outline_chapter_concurrency）。
    # 单次修复的章节数上限（0 = 不限）。长方案一次修几十章既慢又容易超时，
    # 超出部分按严重度排序后本轮跳过（记为 skipped，可下一轮或人工处理）。
    consistency_repair_max_sections: int = 40
    # 校验未通过时是否再重试一次 AI（True = 旧行为，每个失败章节最多 2 次调用）。
    # 默认 False：AI 正常返回但内容不合格时，重试多半仍不合格；仅 AI 调用本身
    # 异常（超时/网络/空返回）才自动重试一次。
    consistency_repair_retry_on_invalid: bool = False
    # ✅ 2026-09-22（调用次数优化 O8）：按严重度分级重试。True（默认）时，
    #    即使上一项为 False，含 high 级冲突的章节校验不过仍重试一次 ——
    #    基线实测省重试会让 3/12 的高危冲突留在交付文档里。
    consistency_repair_retry_on_invalid_by_severity: bool = True

    # ---------- 审核与预检 · 问题定向自动修复（2026-09-30） ----------
    # 单次「自动修复」最多改写多少个章节（0 = 不限）。定位到的章节数可能很多
    # （如全文 CON-01 数值口径不一致散落在十几章），一次全改既有超时风险、
    # 也会让用户无法逐条复核；超出的按定位点数量降序跳过（可下一轮继续）。
    review_autofix_max_sections: int = 10

    # False（默认）= 完全向后兼容：批大小恒为配置值，行为与现状逐字一致。
    # True = 按 provider 实时成功率**防御性降批**：成功率低于
    #   ai_batch_min_success_rate 的 provider 强制逐章调用（批=1）。
    #   依据基线实测：批越大故障回退放大越严重（批=12 时最坏 ×13），而低成功率
    #   provider 恰恰最容易触发批量失败 → 逐章回退的调用放大。只降不升，
    #   「升批」仍由用户在 outline_chapter_batch_size / consistency_scan_batch_size
    #   里显式配置。
    ai_batch_by_success_rate: bool = False
    ai_batch_min_success_rate: float = 0.60

    # ---------- 正文生成链路参数（2026-09-20 配置化） ----------
    # ✅ 旧实现这 6 项硬编码在 sse_handlers.py 模块常量里，无法按部署环境
    #    （弱模型 / 慢链路 / 限流敏感）调优。现全部进 Settings，可环境变量覆盖；
    #    sse_handlers 侧保留同名模块常量（绑定为 settings 取值），既有测试不破。
    # 单个 provider 的超时（秒）：AI 配置里常见 timeout=60s，对 1500+ 字的
    # 章节明显偏短（线上曾表现为大量章节 ~62s 超时失败）。
    content_request_timeout: int = 300
    # 含降级链的总超时（秒）：首稿与续写共用。
    content_total_timeout: int = 660
    # 章节生成失败后的重试次数（首稿；续写不重试，见 sse_handlers P1-1 注释）。
    content_section_retries: int = 1
    # 普通错误退避基数（秒）。
    content_retry_backoff: float = 6.0
    # 429 限流退避基数（秒）。
    content_rate_limit_backoff: float = 20.0
    # 自动续写轮数上限（配合 max_tokens_for_budget 折算后的返工需求收敛为 2）。
    content_continue_max_rounds: int = 2
    # ✅ 编号统一（2026-09-25）：正文子标题编号落库前规范化（默认开启）。
    #    落库前用与导出 write_section 完全同一套算法（numbering.renumber_section_body_subheadings）
    #    重写正文内 Markdown/纯文本子标题编号，使「前端预览 = 落库正文 = 导出成稿」三处同源，
    #    消除存储态编号（3.2）与展示态编号（2）的格式映射差及 AI 写错号/跳号残留。
    #    设为 False 回退旧行为（正文保留 AI 原始编号，仅导出端重算，预览与成稿可能不一致）。
    content_subheading_renumber: bool = True
    # ✅ 编号统一（2026-09-26 · 显式跨校验器）：导出前编号一致性严格校验开关。
    #    False（默认）= 导出时若发现落库正文子标题与目录编号不一致（D4 类漂移残留），
    #    仅告警不阻断；导出成稿本身由 _compute_subheading 重算保证正确。
    #    True = 导出前一致性校验失败直接 409 报错，阻止产出不一致文档（CI 校验场景）。
    numbering_consistency_strict: bool = False
    # ✅ 编号统一（2026-09-25 · E3）：有 DB 子章节的章节，正文子标题降级为节内 body 命名空间。
    #    True（默认）= 统一行为：有子章节时，正文子标题第一层用「1）/2）」、更深层用「a、/b、」，
    #    与子章节的 X.X 命名空间彻底隔离，不再发生「1.1 正文标题」与「1.1 DB 子章节」撞号。
    #    False = 回退旧行为：正文子标题与 DB 子章节在同一命名空间，导出端靠计数器前移（右移续排）。
    #    存量内容无需迁移——落库规范化与导出渲染均从原始 Markdown 块重算，不依赖既有编号。
    body_subheading_demote_with_children: bool = True
    # ✅ 编号统一（2026-09-25 · E6 Tier 1）：导出预检检测失效交叉引用（图号/表号/节号不匹配）。
    #    True（默认）= DLV-14（medium 级，不阻断导出，只在预检清单里列出失效引用）；
    #    False = 关闭该预检（旧行为）。真实图号/表号/节号由导出端的 figure_counters/table_counters/
    #    heading_gen.counters 按分配顺序决定，与正文硬编码的「图3-2」可能漂移——本开关只控制预检，
    #    不做自动改写（改写见 E6 Tier 2 crossref_rewrite_enabled）。
    crossref_stale_detect_enabled: bool = True
    # ✅ 正文生成采样温度（2026-09-22 引入，对齐 OpenBidKit 可配置温度能力）：
    #    None = 沿用 provider 默认温度（旧行为，确定性由 AI 配置决定），保持向后兼容；
    #    非 None 时覆盖 provider 默认，用于弱模型 / 确定性要求高的专项方案稳定输出。
    #    环境变量 CONTENT_TEMPERATURE 可设（如 0.3）；留空或 "none"/"null" 解析为 None。
    content_temperature: float | None = None
    # ✅ 2026-09-22（对齐 OpenBidKit 边界感知长文本截断 userTextSplitter）：正文生成
    #    上下文的安全上限（字符数）。0（默认）= 关闭（沿用既有「逐章精选 + 组件级截断」行为，
    #    完全向后兼容）；>0 时若单章 user 上下文超过该值，按自然边界（段落→句→逗号）截断，
    #    避免极端超长知识库/事实把提示词撑爆导致 400/超时。截断只在超长时触发，正常长度不受影响。
    context_length_limit: int = 0

    # ---------- 提示词治理（2026-09-24 · 遗留问题闭环） ----------
    # G2 版本回滚：把变更前后完整提示词正文写入 prompt_audit_logs.snapshot_json，
    #   这是 POST /api/v1/prompts/{key}/rollback 的数据基础。默认 True —— 新增列、
    #   历史记录恒为空，因此旧行为不变（无快照的历史行仍只有哈希，前端按摘要展示）。
    #   合规上不允许审计表留存正文全文时设 False（哈希审计不受影响）。
    prompt_audit_snapshot_enabled: bool = True
    # 单侧快照最大字符数：超长则截断并标记 truncated=True（保证快照恒为合法 JSON，
    # 与 ai_config 快照「禁止字符级截断」原则一致）。
    prompt_audit_snapshot_max_chars: int = 200000

    # ---------- 提取项目模块 · 危大工程分类体系（2026-09-24） ----------
    # 总开关：是否启用「方案名称/资料 → 危大工程类别自动识别 + 九大章节字段完整性校验」。
    # 纯增量能力：仅在显式调用 /api/v1/bid-analysis/classify 或后续注入分类约束时生效，
    # 不影响既有 18 项提取的任何字段与默认值（向后兼容）。默认开启；设 False 即回到
    # 「不自动分类」的旧行为（旧库无 schemes 分类列，端点会优雅降级）。
    scheme_auto_classify: bool = True

    # ---------- 提取项目模块 · 招标响应域与分段策略（2026-09-30 · 第十一轮） ----------
    # ✅ 对齐 OpenBidKit 易标的「提取域」概念：本软件原有 18 项为「专项方案编制域」
    # （domain="scheme"），易标的 18 项为「招标响应域」（domain="bid_response"，
    # 含技术评分项/技术评分要求语义二分、无效标与废标项四象限等）。
    # 两域 item_id 零交集、业务域不同，故加法引入而非替换。
    # 默认 False：GET /bid-analysis/items 只返回 scheme 域，前端与既有调用点零变化。
    bid_response_domain_enabled: bool = False

    # 均分分段策略总开关。默认 False：沿用 16000 字符滑动窗口 + 500 overlap
    # （既有行为）。设 True 后按 context_length_limit × 比例推导段数并尽量均分，
    # 且断点做段长区间/剩余量校验与 Unicode 代理对保护（对齐 userTextSplitter.cjs）。
    # ⚠️ 与 context_length_limit（本文件下方，默认 0=关闭）配合：该项开启但
    # context_length_limit<=0 时仍回退滑动窗口，避免出现「0 段上限」的退化切分。
    bid_analysis_segment_even: bool = False
    # 均分模式的上下文上限（字符数）。对齐易标 DEFAULT_CONTEXT_LENGTH_LIMIT=400000。
    bid_analysis_segment_context_limit: int = 400000

    # 断点续跑总开关。默认 False：与旧版一致，重跑 = 全量重跑所选项。
    # 设 True 后 /start 在非 force_rerun 且未指定单项时跳过已成功落库的项
    # （对齐易标 tasksToRun 的 status!=='success' 过滤）；单项重跑与强制重跑
    # 恒忽略本开关，语义不受影响。
    bid_analysis_skip_done_when_rerun: bool = False

    # 提取项级并发 / 分段并发 / 单项重试次数 / 分段预算。调参不再需要改代码；
    # 调小可缓解 provider 配额打爆（AGENTS.md §4.1 降级历史）。
    bid_analysis_item_concurrency: int = 2
    bid_analysis_segment_concurrency: int = 3
    bid_analysis_item_retries: int = 2
    # ✅ A-1（2026-10-01 第七模块专项 · P0）：**多文档合计**参与 18 项提取的
    #    字符预算（_MAX_DOC_CHARS 的唯一来源，见 bid_analysis.py）。
    #    旧默认 30000 与上游解析能力严重错配：
    #      · pdf_text_max_pages=500（第十四轮）≈ 40 万字
    #      · MAX_PARSED_CHARS=400000（落库上限）
    #      · 而提取侧只取合计 3 万字（≈7.5%）→ 前面几轮的「解析不丢页」收益
    #        在提取环节被整体吃掉，招标文件中后段的清单/参数/技术要求全部不参与提取；
    #        且预算耗尽后剩余文档被**整份跳过**（_combine_doc_texts 的 break）。
    #    现默认对齐落库上限 400000，使「解析 → 落库 → 提取」三级口径一致。
    #    ⚠️ 代价：分段数 ≈ 预算/16000，预算放大将同比放大 AI 调用量与墙上时间
    #       （18 项 × 段数）。成本敏感场景显式调回 30000 即恢复旧行为（仍可配）。
    bid_analysis_segment_budget: int = 400000

    # 目录生成：按方案危大工程类别（schemes.hazard_category）自动匹配 outline_library
    # 目录库、追加为【目录库参考】。默认关闭：旧行为仅按 config.library_ids 选库；
    # 设 True 后额外按类别命中「已通过」目录库（仅追加，不影响用户显式选库结果）。
    scheme_auto_match_outline: bool = False

    # 正文生成：按九大章节字段映射（NINE_CHAPTERS.source_items）把对应提取项精准注入
    # 各章提示词（【本章专项提取成果】）。默认关闭：旧行为逐章注入整份 project_brief；
    # 设 True 后逐章追加该章专属提取成果（与整份 project_brief 并存，不替换）。
    scheme_auto_chapter_inject: bool = False

    # 全局事实模块：是否在提取管线中为每条事实自动标注「九大章节四维分类」
    # （chapter 九大章节归属 / fact_attr 事实属性 / source_kind 数据来源 /
    #  is_shared 跨章节共性事实）。
    # ✅ 2026-09-24 新增：依据建办质〔2018〕31号，专项方案分九大章节。
    # 纯增量、确定性规则派生（无 AI、无 DB 往返），叠加在既有 22 类 category
    # 之上而不替换它 —— 前端下拉、分组排序、apply_category_auto_classify 的
    # 口径全部不变。新增 4 列均有默认值，历史行由读路径惰性派生兜底。
    # 默认 True（新增能力，行为上只增加字段、不改动既有字段与下游注入）；
    # 设 False 即回到「不标注」的旧行为。
    facts_chapter_classification: bool = True

    # 正文生成：按九大章节把全局事实精准注入各章提示词（【本章相关事实】）。
    # 默认关闭：旧行为逐章注入按 22 类 category 分组的全量事实文本；
    # 设 True 后，章节标题可反推九大章节码时，优先注入该章归属的事实
    # （未命中该章的事实仍保留，仅作补充，不丢事实）。
    facts_chapter_inject: bool = False

    # ---------- 全局事实 · 参考软件易标能力落地（2026-09-30 第十三轮） ----------
    # 以下三项把 OpenBidKit 易标 globalFactsTask.cjs 的「补充 / 整理 / 预算分段」
    # 三段能力接进本仓事实管线，**默认全部关闭**：
    # 关闭时 facts_extractor 的行为与引入前逐字节一致（零新增 AI 调用、
    # 零分段行为变化），因此可在无回归风险的前提下按需灰度开启。

    # ① 知识库补充（对齐易标 runKnowledgeGlobalFactPatches，:876-891）：
    #    把项目知识库条目作为**第二事实来源**，让 AI 只产出「补丁」而不是
    #    重新生成全部事实。设 True 后每次事实提取多 1 次 AI 调用。
    #    ⚠️ 本仓 knowledge_base 表早已存在并被目录/正文生成消费（见
    #    sse_handlers._build_knowledge_text），但事实链路此前完全不读它——
    #    上一轮 AGENTS.md 曾误记为「本仓无对应数据源」，本轮已更正。
    facts_knowledge_patch_enabled: bool = False

    # ② 最终整理（对齐易标 finalizeGlobalFacts，:909-920）：
    #    一次 AI 调用做「同义项合并 + 要求句改写为事实句 + 强制保留工期类变量」。
    #    本仓既有 merge_and_deduplicate 是**纯程序**归一化，缺语义改写这一步。
    facts_finalize_enabled: bool = False

    # ③ 上下文预算分段（对齐易标 getGlobalFactsSegmentLimit，:363-377）：
    #    设 True 后分段上限改由「模型上下文窗口 × 0.8 - 固定消息」动态计算，
    #    取不到配置时回落到本仓等价的 CHUNK_SIZE 基准（8000），下限 1000。
    #    ⚠️ 关闭时 split_into_chunks 的切分行为与引入前完全一致。
    facts_context_budget_split: bool = False

    # 事实链路两次新增 AI 调用的超时（秒）。沿用 facts_extractor.FACTS_REQUEST_TIMEOUT
    # 的量级（240s），单独可配以便长资料场景下调。
    facts_enrich_timeout: int = 240

    # ---------- 目录生成 · 方案名称主线（2026-09-26 · 三项依据收敛） ----------
    # 以下三项对应需求「目录生成主要结合三项依据」，全部为**纯增量**改造：
    # 开关关闭时目录生成链路的输入与旧版逐字节一致（可回归验证）。
    #
    # ① outline_name_basis：把「方案名称」从"整串塞进提示词交给模型领会"
    #    升级为**确定性解析**（services/scheme_basis.parse_scheme_basis），
    #    向一级目录 / 短方案目录 / 目录审核三个提示词新增 {scheme_basis} 变量，
    #    内容 = 方案类型 + 主要施工内容 + 施工工序 + 施工工艺 + 施工对象
    #          + 危大六大类及子类分类（全部来自方案名称**字面**关键词，零 AI）。
    #    同时 is_dangerous 由「仅看 schemes.type」放宽为
    #    「type 命中 DANGEROUS_TYPES **或** 名称字面命中六大类危大」——
    #    修复「名称写『深基坑支护』而 type 选『其它』→ 危大必备章节约束不触发」。
    #    默认 True：新增的是提示词里**此前不存在**的区块，既有内容一字未改；
    #    名称无任何字面可解析时 {scheme_basis} 渲染为空串 = 不注入，
    #    自动回落到依据二/三，绝不编造。
    outline_name_basis: bool = True

    # ② outline_standards_inject：把 standards_registry.get_standards_text
    #    （按方案名/类型匹配的编制规范与标准）注入一级目录与短方案目录的
    #    {standards_text} 变量。改造前该函数**只在正文生成调用**
    #    （sse_handlers 4384/4588 行），目录生成完全没有编制规范依据。
    #    与 ① 同口径：纯新增区块、名称/类型无法匹配时渲染为空串即不注入。
    outline_standards_inject: bool = True

    # ③ outline_basis_relevance：让「解析提取」与「全局事实」**围绕方案名称**。
    #    实施方式刻意选择「重排 / 加权」而非「过滤」，以同时满足
    #    需求里的两条看似矛盾的约束 ——「相关的进入目录」与「完整调用不丢失不截断」：
    #      · 依据二（bid_analysis_items）：_budgeted_truncate_sections 的字符
    #        配额按「长度占比」分配，改造前相关项与无关项同权争抢预算；
    #        现给相关小节 ×outline_relevance_boost 倍份额（不删除任何小节，
    #        每节仍享 _allocate_char_budgets 的保底份额）；
    #      · 依据三（global_facts）：_render_facts_text 是**头部优先、遇预算即
    #        break**，故把相关事实**前置排序**即等效于"提高可见性"。
    #    ⚠️ 零信息损失：不删除任何条目，未命中时行为与旧版完全一致。
    outline_basis_relevance: bool = True
    # 相关项的预算份额放大倍数（仅在 outline_basis_relevance=True 时生效）。
    # 2.0 = 相关项份额约为无关项两倍；调大更激进，调到 1.0 等价于关闭加权。
    outline_relevance_boost: float = 2.0
    # ✅ 2026-09-27（要求四 · 全面性）：目录生成后做「方案名称关键词 → 目录章节」
    #    覆盖校验，缺口并入外科式补齐的 missing 列表（复用既有补齐链路，
    #    缺失时才多 1 次小调用；全覆盖时反而省掉 AI 审核）。
    #    默认 True = 新增能力；置 False 可回退到"只看编制要求/危大必备章节"口径。
    outline_name_coverage_check: bool = True

    # G4 变量契约：模板可声明 requires（必需变量白名单），启动期比对「声明」与
    #   「模板实际占位符」是否一致，不一致打 WARNING。默认 False = 不改变启动行为。
    #   ✅ 2026-09-25（BUG-B 修复）：本项现仅控制**是否逐条打印漂移明细日志**
    #   （默认 False 只打一条汇总告警，便于详细排查时开启）；
    #   「漂移时是否阻断启动」由下方 prompt_contract_fail_fast 单独控制。
    prompt_strict_variables: bool = False

    # ✅ 2026-09-25（BUG-B · 新增显式能力）：启动期发现「提示词变量契约漂移」
    #   （PROMPT_VARIABLE_CONTRACTS 与出厂模板占位符双向不一致）时是否**阻断启动**。
    #   默认 False = 向后兼容（只告警，服务正常启动）；
    #   设 True 后由 check_prompt_variables(strict=True) 抛 PromptContractError，
    #   适合 CI 门禁 / 生产部署校验「模板与契约表必须同步」。
    #   环境变量 PROMPT_CONTRACT_FAIL_FAST 可开启。
    prompt_contract_fail_fast: bool = False

    # G6 提示词注入防护：注入外部资料（项目资料摘要/全局事实/知识库素材/用户资料）
    #   时自动加「资料边界」围栏，并对资料做注入手法扫描（命中仅告警，不阻断生成）。
    #   默认 False = 完全向后兼容（不改变任何现有注入文本）。
    prompt_injection_defense: bool = False

    # G5 上下文预算分配器：正文生成时若单章 user 上下文超过预算，按
    #   「全局事实 > 目录树 > 资料摘要 > 知识库」优先级保重要、削次要。
    #   0（默认）= 关闭，沿用既有「逐章精选 + 组件级截断」行为。
    prompt_context_budget: int = 0

    # AI 配置（主配置 + 降级链 + 场景路由）内存缓存 TTL（秒）。
    # ✅ 2026-09-23 修复：该配置项此前**从未被消费** —— provider_factory 里
    #    另有一份硬编码的 `_CONFIG_CACHE_TTL = 300.0`，即「文档写 3600、
    #    实际生效 300」，属配置项静默失效。
    #    现由 provider_factory._config_cache_ttl() 统一读取本项；默认值取
    #    **300 以保持既有运行行为不变**（原先真实生效值就是 300s）。
    #    说明：所有写路径（保存/删除/切换/降级链/导入/清 Key/场景路由）都会
    #    调用 invalidate_config_cache() 立即失效，TTL 仅作「外部直接改库」兜底，
    #    因此调大该值不会造成配置改动不生效。
    ai_config_cache_ttl: int = 300

    # 多环境：AI 配置的「当前生效环境」。
    # ✅ 2026-09-23 新增（多环境支持）。语义：
    #    1. 每条 ai_config 可打环境标签 `env`（如 dev / test / prod），空串 = 通用；
    #    2. 本项为空（默认）时**完全沿用旧行为** —— 不做任何环境过滤，
    #       主配置与降级链的选取口径与引入该功能前逐字节一致；
    #    3. 非空时：主配置优先取「该环境专用」的 is_active=1，取不到则回落通用配置；
    #       降级链只纳入「通用 + 该环境」的候选，避免生产环境降级打到测试地址。
    # 本项仅作为**默认值**：运行时可通过 PUT /api/v1/ai/env 覆盖（落在
    #    ai_runtime_settings 表），便于前端「环境切换器」即时切换而无需重启。
    active_env: str = ""

    # ---------- 数据库性能（2026-09-24 · 性能基线优化） ----------
    # SQLite 连接级 PRAGMA 调优。**实测归因结论（2026-09-24 发布前复核，13334 行
    # 离线快照 × 50 轮对照）**：
    #   - mmap_size=256MB 是唯一纯收益：按场景聚合 5.18→1.96ms（↓62%）、
    #     失败原因 TOP 7.08→3.63ms（↓49%）、供应商 DISTINCT 4.64→4.11ms（↓11%）；
    #   - temp_store=MEMORY 在该平台实测反而变慢：供应商 DISTINCT +30%、
    #     动作 DISTINCT +51%、按场景聚合 +13%（Windows sqlite3 对磁盘临时
    #     B-树的 OS 页缓存路径更快），故默认关闭，不再作为启用项。
    # 默认 db_perf_pragmas_enabled=False = 完全沿用既有 PRAGMA 组合
    # （WAL + foreign_keys=ON + busy_timeout=15000 + synchronous=NORMAL），
    # 逐字节向后兼容。开启后仅追加 mmap_size（读路径映射文件避免逐页拷贝）。
    # 安全性：全部为连接级/读侧设置，不改变任何数据语义与并发语义
    # （WAL 与 busy_timeout 不变，写路径不变）。
    db_perf_pragmas_enabled: bool = False
    # mmap_size 字节数（仅在 db_perf_pragmas_enabled=True 时生效）
    db_mmap_size: int = 268435456
    # 是否追加 temp_store=MEMORY（**默认关闭**：实测对 DISTINCT/GROUP BY
    # 反效果 +30%~51%，见上注释；保留开关仅为兼容早期显式开启过该参数的环境）
    db_temp_store_memory: bool = False


    @field_validator("content_temperature", mode="before")
    @classmethod
    def _coerce_temperature_none(cls, v):
        """环境变量常见写法（空串 / none / null）统一收敛为 None，避免 float 解析报错。"""
        if v is None:
            return None
        if isinstance(v, str) and v.strip().lower() in ("", "none", "null"):
            return None
        return v

    # ✅ Pydantic V2/V3 正确写法：用 model_config 替代 class Config
    model_config = ConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()

for _d in (DATA_DIR, LOGS_DIR, EXPORTS_DIR, CHARTS_DIR, FACT_UPLOADS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
