"""正文生成 · 章节运行时纯函数（2026-09-28 自 sse_handlers.generate_content 下沉）

背景（架构债 T-1 收口）：`routers/sse_handlers.py` 单文件 390KB，`generate_content`
内部闭包有：消息装配（system + user 完整上下文）、续写消息构造、续写轮次判定、
续写 token 上限折算。这些片段**不依赖 SSE 闭包状态**（只依赖入参与既有服务层
常量/文案），本模块把它们搬为模块级纯函数，供 generate_content 调用：

- ``build_chapter_user_content``：首轮 user 上下文的唯一构造器（方案/施工内容/
  章节链/同级/字数/事实/知识库/生成标准块）。
- ``build_continuation_messages``：续写轮 messages 的唯一构造器（与首轮同口径：
  续写 system + 章节定位 + 前文尾部 + 续写指令 + 模式提醒）。
- ``should_continue_round``：续写轮次判定（<80% 或未达最少轮次、且未超最大轮次）。
- ``continue_max_tokens``：续写轮输出 token 上限（按「剩余待补字数」折算）。

⚠️ 行为契约：函数**逐行搬迁、不做任何逻辑修改** —— 正文生成结果必须与搬迁前
完全一致（由 tests/test_content_runtime.py 钉住 + 全量正文生成测试回归）。
"""
from __future__ import annotations

from app.services.content_standard import (
    build_continue_hint,
    build_facts_header,
    build_user_block,
)
from app.services.content_utils import (
    WORD_UNDER_RATIO,
    max_tokens_for_budget,
)


def build_chapter_user_content(
    *,
    scheme: dict,
    project_brief: str,
    content_scope: str,
    parent_chain: str,
    parent_points: list[str],
    sibling_lines: str,
    section_number: str,
    leaf: dict,
    word_budget: int,
    word_budget_hint: str,
    prev_sibling_summary: str,
    facts_text: str,
    eff_standard: str,
    knowledge_text: str,
) -> str:
    """构造章节生成的首轮 user 上下文（纯字符串拼接，无 IO / 无 AI）。

    Args:
        scheme: 方案行 dict（name/type/generation_standard）。
        project_brief: 项目概述文本（已由调用方构建好）。
        content_scope: 方案名称主要施工内容（空串=不注入该段）；由调用方用
            ``sse_handlers._outline_construction_scope`` 预计算后传入。
        parent_chain: 上级章节链文本（``_build_parent_chain`` 产出）。
        parent_points: 上级章节要点行（含描述），按根→叶顺序。
        sibling_lines: 同级章节提示文本（可为空）。
        section_number: 本节存储态编号的展示形态（``_section_outline_number`` 产出）。
        leaf: 叶子章节 dict（title/description/id）。
        word_budget: 本章目标字数（整数）。
        word_budget_hint: 目标字数提示文案（``_word_budget_hint`` 产出；空串时
            本函数回落为 ``{word_budget}字``，与旧实现 ``hint or f'{budget}字'``
            逐字一致）。
        prev_sibling_summary: 前序同级章节结尾参考（可为空）。
        facts_text: 本章相关全局事实文本（可为空）。
        eff_standard: 生效生成标准（precise / fuzzy；驱动事实引导语与标准块）。
        knowledge_text: 本章知识库素材文本（可为空）。

    Returns:
        首轮 user 上下文（多段「【标签】：」结构的完整文本）。
    """
    user_content = (
        f"【方案名称】：{scheme.get('name', '')}\n"
        f"【方案类型】：{scheme.get('type', '')}\n"
        f"【项目概述】：{project_brief}\n"
    )
    # 方案名称主要施工内容：正文生成复用的四项依据之一（_outline_construction_scope）。
    if content_scope:
        user_content += (
            "【方案名称主要施工内容】（本章内容必须服务于其中对应的"
            "施工内容项；与本章无关的项不得写入）：\n"
            f"{content_scope}\n")
    user_content += f"【上级章节链】：{parent_chain}\n"
    if parent_points:
        user_content += "【上级章节要点】：\n" + "\n".join(parent_points) + "\n"
    # 同级章节：明确告知"本层还有哪些兄弟章节、各自负责什么"。
    user_content += (
        "【同级章节（请避免内容重复）】：\n" + sibling_lines + "\n"
        if sibling_lines else "【同级章节（请避免内容重复）】：（无）\n")
    user_content += f"【当前章节编号】：{section_number}\n"
    user_content += f"【当前章节】：{leaf['title']} — {leaf.get('description', '')}\n"
    # 目标字数：调用方传入的提示文案优先，回落 "N字"（与旧实现逐字一致）。
    user_content += f"【目标字数】：{word_budget_hint or f'{word_budget}字'}\n"
    if prev_sibling_summary:
        user_content += (
            f"【前序同级章节结尾参考（衔接风格，勿重复）】：{prev_sibling_summary}\n")
    if facts_text:
        # 分段标签：必须 `【标签】：` 形式（G5 预算分配器切段依据）。
        user_content += (
            "\n【全局事实变量（唯一可信数据源）】：\n"
            + build_facts_header(eff_standard)
            + f"{facts_text}\n")
    if knowledge_text:
        user_content += (
            "\n【项目知识库素材】：\n"
            "（企业管理制度/工艺要点/既有素材，"
            "与本项目相关的表述应遵循其口径，数据仍以上方全局事实为准）\n"
            f"{knowledge_text}\n")
    # F-CONTENT-STANDARD: 生成标准 user 块（DB 定制模板缺占位符时的兜底通道）。
    user_content += "\n\n" + build_user_block(eff_standard)
    return user_content
def build_continuation_messages(
    *,
    user_ctx: str,
    cont_tail: str,
    wc: int,
    word_budget: int,
    cont_prompt: str,
    eff_standard: str,
) -> list[dict]:
    """构造续写轮 messages（与首轮同口径的定位 + 续写指令 + 模式提醒）。

    Args:
        user_ctx: 本轮 user 上下文（首轮用完整上下文，后续轮用章节定位）。
        cont_tail: 前文尾部（``_safe_tail`` 截断后的结尾片段）。
        wc: 当前正文字数（不含图表代码块）。
        word_budget: 本章目标字数。
        cont_prompt: ``content_continue_system`` 渲染出的续写 system 提示词。
        eff_standard: 生效生成标准（precise / fuzzy；驱动续写轮模式提醒）。

    Returns:
        4 个 role 的完整问话序列（system / user 定位 / assistant 前文 / user 指令）。
    """
    std_hint = build_continue_hint(eff_standard)
    return [
        {"role": "system", "content": cont_prompt},
        {"role": "user", "content": user_ctx},
        {"role": "assistant", "content": f"（前文已省略，以下为正文结尾部分）\n{cont_tail}"},
        {"role": "user", "content": (
            f"当前字数{wc}，目标{word_budget}字，请继续补充。"
            f"补充后总字数上限为 {int(word_budget * 1.1)} 字"
            f"（不得超出，接近上限时自然收尾）。"
            if wc < word_budget else
            f"当前字数{wc}已达到目标字数，请仍以上文为基础继续补充实质性内容"
            "（具体工序细节、控制要点、检验标准、安全注意事项等），"
            f"但**补充后总字数不得超过 {int(word_budget * 1.1)} 字**，"
            "不要重复前文，不要输出图表代码块。")
            + std_hint}]


def should_continue_round(
    *,
    wc: int,
    word_budget: int,
    continue_count: int,
    min_passes: int,
    max_rounds: int,
) -> bool:
    """续写轮次判定：不足 80% 或未达最少轮次、且未超过最大轮次。

    与正文生成循环 ``while (...)` 的口径逐字一致（P1-1：续写不重试失败调用；
    这里只决定"要不要再补一轮"）。
    """
    try:
        budget = int(word_budget or 0)
    except (TypeError, ValueError):
        budget = 0
    return ((wc < budget * WORD_UNDER_RATIO or continue_count < min_passes)
            and continue_count < max_rounds)


def continue_max_tokens(*, word_budget: int, wc: int) -> int:
    """续写轮输出 token 上限：按「剩余待补字数」折算（P1-2，不含首稿）。

    与旧实现 ``max_tokens_for_budget(word_budget,
    chars=max(1, int(word_budget*1.1)-wc))`` 逐字一致；下限保护由
    ``max_tokens_for_budget`` 内建（MAX_TOKENS_FLOOR）。
    """
    try:
        budget = int(word_budget or 0)
    except (TypeError, ValueError):
        budget = 0
    try:
        cur = int(wc or 0)
    except (TypeError, ValueError):
        cur = 0
    return max_tokens_for_budget(budget, chars=max(1, int(budget * 1.1) - cur))