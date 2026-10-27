"""正文 Agent 修复的提示词（R135，中文）。

对齐易标 ``contentGenerationConsistencyTools.cjs`` 的修复纪律
（REPAIR_INSTRUCTIONS / RULE_CHECK_INSTRUCTIONS）与本仓「数据真实性红线」
（``SHARED_SCOPE_RULES`` 一致口径）：以全局事实为唯一真相来源、最小必要
修改、不碰程序标记、不新增/删除/重排小节。
"""
from __future__ import annotations

from app.services.content_agent.workspace import FACTS_FILE, TASK_FILE

SYSTEM_PROMPT = """你是建筑工程专项施工方案的正文一致性修复 Agent。工作区内的小节正文文件由程序写入，每个文件形如：

# 小节标题

<!-- SECTION:小节ID:BEGIN -->
（正文 Markdown）
<!-- SECTION:小节ID:END -->

修复纪律（必须全部遵守）：
1. 只修改目标小节文件中标记对**内部**的正文；标记行、文件名、文件集合、小节顺序一律不得改动。
2. 不得新增或删除小节文件；不得重排小节；不得清空任何小节正文。
3. 以 `""" + FACTS_FILE + """`（全局事实设定）为唯一真相来源：正文与全局事实冲突时，把正文改为与全局事实一致；全局事实本身未覆盖的事项保持正文原样，不得凭空编造数值、人名、日期、型号或引用。
4. 只改与冲突直接相关的语句，其他用词、称谓和表述保持原样；不做润色、不检查或调整总字数。
5. 先用 search-sections 或 read 定位冲突原文，再用 edit 做最小修改；找不到明确依据或无法在本轮修复的矛盾，保持原样并在最终答复中说明，不要强行改写。
6. 每次修改后不需要回读全文确认；修改完成后直接用普通文本简短总结（修了什么、保留什么），不再调用任何工具。

可用工具以本轮下发的工具列表为准：read/write/edit/grep/find/ls 用于读取和修改工作区文件；search-sections 用于按关键词定位小节与原文；report-failure 仅在无法继续执行时调用并说明原因。"""

USER_TEMPLATE = """【修复任务】
按 `""" + TASK_FILE + """` 中的冲突清单修复正文小节。全局事实见 `""" + FACTS_FILE + """`。

要求：
- 冲突清单中每一条都给出小节 ID、冲突描述与权威值；只修这些列出的矛盾。
- 修改只发生在对应小节文件的标记对内部。
- 完成后简短总结每处矛盾的处理结果（已修复 / 保持原样及原因）。"""


def build_task_instructions(targets: list[dict]) -> str:
    """把冲突清单渲染成 ``任务说明.md`` 内容（程序写入、基线保护）。"""
    lines = ["# 正文一致性修复任务", "", "按下列清单逐条修复；权威值是唯一依据。", ""]
    for target in targets:
        lines.append(f"## 小节 {target.get('section_id', '')}（{target.get('title', '')}）")
        conflicts = target.get("conflicts") or []
        if not conflicts:
            lines.append("- （本小节无单独指定矛盾，按全局事实核对全文表述）")
        for index, conflict in enumerate(conflicts, 1):
            topic = conflict.get("topic") or conflict.get("description") or ""
            current = conflict.get("current_value") or ""
            authority = (conflict.get("authoritative_value")
                         or conflict.get("authoritative") or "")
            instruction = conflict.get("repair_instruction") or ""
            line = f"{index}. {topic}"
            if current:
                line += f"（正文现值：{current}）"
            if authority:
                line += f" → 权威值：{authority}"
            lines.append(f"- {line}")
            if instruction:
                lines.append(f"  修复要求：{instruction}")
        lines.append("")
    return "\n".join(lines).strip()
