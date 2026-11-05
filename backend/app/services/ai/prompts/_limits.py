"""提示词 Token / 长度预算常量的单一事实源。

集中提示词 Token/长度预算常量；新增同类旋钮先登记在此；数值为出厂默认档，
改动需同步更新护栏与 AGENTS.md。

历史：这些旋钮此前散落在 ``routers/compliance.py`` / ``routers/sse_handlers.py`` /
``services/consistency_scanner.py`` / ``services/repair_agent.py`` /
``services/content_utils.py`` 五个模块，调一次参要跨文件 grep。本模块把它们
收拢到一处，各消费方改 ``from app.services.ai.prompts._limits import ...``，
并**保留原模块内的名字绑定**（``compliance.py`` 里仍可读
``FACTS_PROMPT_CAP``、``sse_handlers.py`` 里仍可读 ``PROJECT_FACTS_LIMIT_OUTLINE``），
下游消费代码一行都不用改。

⚠️ 本模块**只搬常量、不改数值**：所有值逐字沿用各文件旧值，出厂默认档行为
   与搬前逐字节一致。新增同类旋钮请先登记在此，不要在业务模块里新起字面量。
"""
from __future__ import annotations

#: 事实文本总量上限（字符）—— 提示词注入保护（旧名 ``compliance.FACTS_PROMPT_CAP``）。
FACTS_PROMPT_CAP = 3000

#: 单条事实值的截断长度（字符）—— value 兜底取整段 content 时防长文本撑爆
#: （旧名 ``compliance._FACT_PROMPT_VALUE_CAP``）。
FACT_PROMPT_VALUE_CAP = 120

#: project_facts 注入预算 · 目录档（字符）—— 外科式补齐 / 目录反馈修正 / 长方案
#: 一级目录（旧名 ``sse_handlers.PROJECT_FACTS_LIMIT_OUTLINE``）。
#: ✅ R177：本值现为**含数据字典的总预算**（facts + dd），由 ``facts_builder``
#: 统一分配（事实先算保底，字典只吃剩余额度），调用方直接作为 ``max_total``
#: 传入，禁止再对返回文本做二次切片。
PROJECT_FACTS_LIMIT_OUTLINE = 1500

#: project_facts 注入预算 · 子层档（字符）—— 二三级小节生成（提示词压力更大，
#: 预算更紧，旧名 ``sse_handlers.PROJECT_FACTS_LIMIT_SUBLEVEL``）。
#: ✅ R177：同目录档，本值是含数据字典的总预算，由 ``facts_builder`` 分配。
PROJECT_FACTS_LIMIT_SUBLEVEL = 1000

#: project_facts 整案档（字符）—— 短方案 / 目录评审使用完整事实（含知识库
#: 素材合并后的封顶）；长方案一级改用 PROJECT_FACTS_LIMIT_OUTLINE。
PROJECT_FACTS_FULL_CAP = 6000

#: 正文生成 · 单章事实注入总预算（字符）—— core（通用/总体事实）与 chapter
#: （本章+选中事实）共享同一上限；core 先算，chapter 只用剩余额度。
#: ✅ L7/BUG-12（R177 · 2026-11-04）：旧实现两侧各 6000，且选中事实可能与 core
#: 重复注入，双倍 token 换零信息。
CONTENT_FACTS_TOTAL_BUDGET = 6000

#: 一致性扫描：单片章节正文总量上限（字符），超出则切多片
#: （旧名 ``consistency_scanner.SECTION_CHUNK_LIMIT``）。
SECTION_CHUNK_LIMIT = 12000

#: 一致性扫描：单章截断上限（字符）（旧名 ``consistency_scanner.PER_SECTION_LIMIT``）。
PER_SECTION_LIMIT = 6000

#: 一致性修复：整章重写兜底的正文长度硬上限（字符）—— 对齐扫描侧
#: ``SECTION_CHUNK_LIMIT``，超长章在重写兜底前直接放弃（必败调用）
#: （旧名 ``repair_agent.REPAIR_REWRITE_MAX_CHARS``）。
REPAIR_REWRITE_MAX_CHARS = 12000

#: 字数口径：实际字数 / 预算 > 该比值即判 over（旧名 ``content_utils.WORD_OVER_RATIO``）。
WORD_OVER_RATIO = 1.3
