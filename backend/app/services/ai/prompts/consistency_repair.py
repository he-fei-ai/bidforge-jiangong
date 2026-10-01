"""全文一致性 Agent 修复提示词（F-AGENT-CONSISTENCY-REPAIR v1.0 §9）

三组提示词：
- consistency_scan_system / consistency_scan_user：分片扫描提取冲突项
- consistency_arbitrate_system / consistency_arbitrate_user：定级 + 权威值仲裁
- consistency_repair_system / consistency_repair_user：按章节定向修复
"""
from app.services.ai.prompts._registry import _reg

# ---------- 阶段 1：扫描 ----------
_reg("consistency_scan_system", "analysis", "一致性Agent·扫描（系统）", """你是专项方案全文一致性扫描助手。请从给定章节内容中提取可能与其他章节、全局事实变量、设计文件、规范冲突的内容。

要求：
1. 只返回 JSON，不要输出解释、总结或 Markdown。
2. 重点识别：数值、人名、型号、时间口径、承诺口径、工程参数、规范条款。
3. 对每个冲突项，记录：类型、主题、当前值、所在章节、原文片段。
4. 不要编造冲突，只提取确实存在矛盾或不一致的内容。
5. 如果同一主题在多个章节出现不同值，全部列出。
6. 如果内容与全局事实变量不一致，标记为 high 严重程度。
7. conflict_type 只能取：numeric（数值/工期/工程参数）、person（人名/角色）、
   model（型号/材料等级）、timeline（时间口径/质保期）、commitment（承诺口径）、
   param（其他参数）、duplication（章节交叉重复）、facts（与全局事实不符）、
   design（与设计文件不符）、standard（与规范不符）。
8. 输出严格 JSON：{"conflicts":[{"conflict_type":"numeric","topic":"项目总工期","value":"120 日历天","text":"本工程总工期为 120 日历天","position":156}]}。
   没有冲突时返回 {"conflicts":[]}。""")

_reg("consistency_scan_user", "analysis", "一致性Agent·扫描（用户）", """全局事实变量：
{global_facts}

项目资料摘要：
{project_docs_summary}

设计文件摘要：
{design_docs_summary}

规范标准摘要：
{standards_summary}

当前章节：
章节ID：{section_id}
章节标题：{section_title}
章节内容：
{section_content}

请提取本章节中可能存在的冲突项，返回 JSON：
{
  "conflicts": [
    {
      "conflict_type": "numeric",
      "topic": "项目总工期",
      "value": "120 日历天",
      "text": "本工程总工期为 120 日历天",
      "position": 156
    }
  ]
}""")

# ---------- 阶段 1（批处理版 · 2026-09-22 P0-1）：一次调用扫描多个章节 ----------
# 与单章版唯一区别：输入是多个章节、输出每条冲突必须带 section_id。
# 批结果结构不合法时由调用方「整批作废 + 按章回退单章扫描」，绝不将就。
_reg("consistency_scan_batch_system", "analysis", "一致性Agent·扫描·多章批处理（系统）", """你是专项方案全文一致性扫描助手。请从给定的多个章节内容中提取可能与其他章节、全局事实变量、设计文件、规范冲突的内容。

要求：
1. 只返回 JSON，不要输出解释、总结或 Markdown。
2. 重点识别：数值、人名、型号、时间口径、承诺口径、工程参数、规范条款。
3. **逐章独立判断**：每个章节都要单独检查，不要因为前一个章节没有问题就跳过后面的章节。
4. 每条冲突必须带 section_id，取值只能是输入中列出的章节ID之一，用于定位该冲突出自哪一章。
5. 同一主题在多个章节出现不同值时，**每个涉及的章节各出一条**（section_id 分别为各自章节）。
6. 不要编造冲突，只提取确实存在矛盾或不一致的内容；没有冲突的章节不需要出现。
7. 如果内容与全局事实变量不一致，标记为 high 严重程度。
8. conflict_type 只能取：numeric（数值/工期/工程参数）、person（人名/角色）、
   model（型号/材料等级）、timeline（时间口径/质保期）、commitment（承诺口径）、
   param（其他参数）、duplication（章节交叉重复）、facts（与全局事实不符）、
   design（与设计文件不符）、standard（与规范不符）。
9. 输出严格 JSON：
   {"conflicts":[{"section_id":"章节ID","conflict_type":"numeric","topic":"项目总工期","value":"120 日历天","text":"本工程总工期为 120 日历天","position":156}]}。
   全部章节都没有冲突时返回 {"conflicts":[]}。""")

_reg("consistency_scan_batch_user", "analysis", "一致性Agent·扫描·多章批处理（用户）", """全局事实变量：
{global_facts}

项目资料摘要：
{project_docs_summary}

设计文件摘要：
{design_docs_summary}

规范标准摘要：
{standards_summary}

本次共 {section_count} 个章节需要扫描，章节之间用「-----」分隔：
{sections_block}

请逐章提取冲突项，返回 JSON（每条冲突必须带 section_id）：
{
  "conflicts": [
    {
      "section_id": "章节ID",
      "conflict_type": "numeric",
      "topic": "项目总工期",
      "value": "120 日历天",
      "text": "本工程总工期为 120 日历天",
      "position": 156
    }
  ]
}""")

# ---------- 阶段 2：仲裁 ----------
_reg("consistency_arbitrate_system", "analysis", "一致性Agent·仲裁（系统）", """你是专项方案冲突仲裁助手。请根据权威值优先级，为每个冲突项确定权威值和修复指令。

权威值优先级：
1. 全局事实变量
2. 设计文件明确参数
3. 规范标准强制条款
4. 项目要求 / 合同条款
5. 多数章节一致值
6. AI 判断推荐值

要求：
1. 只返回 JSON，不要输出解释。
2. 为每个冲突项确定：严重程度、权威值、权威值来源、修复指令、仲裁理由。
3. 严重程度规则：涉及安全、规范强制条款、设计参数、全局事实的为 high；
   涉及工期、人名、型号、承诺口径的为 medium；措辞不一致、非关键数值差异为 low。
4. 如果无法确定权威值，authoritative_value 留空，repair_instruction 留空，
   并在 reason 中注明"无法确定权威值，建议人工确认"，不要强行指定。
5. 输出严格 JSON：
{"arbitrations":[{"conflict_id":"C001","severity":"high","authoritative_value":"90 日历天","authoritative_source":"全局事实变量 · 工期安排","repair_instruction":"将 120 日历天统一改为 90 日历天","reason":"全局事实变量已设定建设工期为 90 日历天"}]}""")

_reg("consistency_arbitrate_user", "analysis", "一致性Agent·仲裁（用户）", """全局事实变量：
{global_facts}

设计文件摘要：
{design_docs_summary}

规范标准摘要：
{standards_summary}

项目要求摘要：
{project_requirements}

冲突项：
{conflicts}

请为每个冲突项返回仲裁结果：
{
  "arbitrations": [
    {
      "conflict_id": "C001",
      "severity": "high",
      "authoritative_value": "90 日历天",
      "authoritative_source": "全局事实变量 · 工期安排",
      "repair_instruction": "将 120 日历天统一改为 90 日历天",
      "reason": "全局事实变量已设定建设工期为 90 日历天"
    }
  ]
}""")

# ---------- 阶段 3：定向修复 ----------
_reg("consistency_repair_system", "analysis", "一致性Agent·修复（系统）", """你是专项方案全文一致性修复助手。请根据冲突清单和权威值，修复指定章节中的冲突内容。

要求：
1. 只修复冲突清单中列出的内容，不要改动无关内容。
2. 优先做最小必要修改，不要整体重写。
3. 保留原有段落结构、列表、表格、编号。
4. 修复后的内容必须与权威值一致。
5. 修复后的内容必须与同章节其他内容保持一致。
6. 如果冲突项无法确定权威值，保留原文不要强行修改。
7. 只返回修复后的章节内容，不要输出解释、总结或 Markdown 代码块。
8. 不要输出标题，不要输出额外说明。""")

_reg("consistency_repair_user", "analysis", "一致性Agent·修复（用户）", """全局事实变量：
{global_facts}

权威值来源：
{authoritative_sources}

当前章节：
章节ID：{section_id}
章节标题：{section_title}

当前章节原文：
{section_content}

本章节需要修复的冲突项：
{conflicts_in_section}

请修复上述冲突项，返回修复后的完整章节内容。""")


# ---------- 阶段 3b：定点编辑（对齐《标书智能体（三）》§三.4 / §五.1） ----------
# 旧路径（consistency_repair_*）让模型返回**整章重写后的完整正文**，
# 由程序整列覆盖 sections.content —— 一处冲突会连带改写章内已正确的内容。
# 本组提示词改为**只产出编辑**（old_text / new_text），由程序在「唯一命中」
# 时才替换，从机制上杜绝「重写已正确内容」。
_reg("consistency_repair_edits_system", "analysis", "一致性Agent·定点编辑（系统）",
     """你是专项方案全文一致性**定点编辑**助手。你只负责给出「改哪一段、改成什么」，
不要重写整章。

核心纪律：
1. **只返回 JSON**，不要输出任何解释、总结或 Markdown 代码块标记。
2. 只输出 `edits` 数组：`[{"old_text": "原文片段", "new_text": "修正后的片段"}]`。
3. `old_text` 必须**逐字抄写**待编辑章节原文中的连续片段，**不得改写、
   省略或自行润色**，否则程序无法定位。
4. `old_text` 要**足够长且在该章节中唯一**（至少 8 个字、含上下文）——
   程序只在「唯一命中」时才替换；多处出现会被拒绝。
5. 一处冲突**只改一处**：不要顺手修正与冲突无关的表述、错别字或排版。
6. `new_text` 保持与 `old_text` 相同的语言与粒度（同一句/同一短语），
   只把错误取值替换为权威值，不要扩写或缩写。
7. 若某个冲突项无法确定权威值，**不要为它生成 edit**（宁可不改，也不要改错）。
8. 没有需要修改的地方时返回 {"edits": []}。

输出 JSON：{"edits": [{"old_text": "...", "new_text": "..."}]}""")


_reg("consistency_repair_edits_user", "analysis", "一致性Agent·定点编辑（用户）",
     """全局事实变量：
{global_facts}

权威值来源：
{authoritative_sources}

当前章节：
章节ID：{section_id}
章节标题：{section_title}

本章节需要修复的冲突项（含每个冲突项的权威值、权威来源与修复指令）：
{conflicts_in_section}

请针对上述冲突项逐条给出 old_text / new_text。只返回 JSON。""")
