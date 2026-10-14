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
def resolve_system_variables(*, stable: bool, section_number: str,
                            standards_text: str,
                            subheading_rule: str) -> dict:
    """决定注入 ``content_generation_system`` 模板的**逐章变量**取值（纯函数）。

    ✅ R57（2026-10-09）：从 ``sse_handlers._build_generation_context`` 下沉。
    该决策原本埋在 SSE 闭包里，无法被任何运行时用例覆盖 —— 一旦后人把
    "stable" 分支改成返回真实值，system 又会自首字符起逐章变化、整条前缀缓存
    失效，而**没有任何测试会红**（本仓 A/B 反向验证正是为此存在的纪律）。

    Args:
        stable: True = 前缀稳定化开启。逐章变量一律注入**空串**（变量仍传，
            故模板占位符既不残留也不会被整行丢弃），其真实值由
            :func:`build_chapter_extras_block` 原样补给模型 —— 信息零丢失。
            False = 逐字沿用旧行为（开关关闭时的回退路径）。

    Returns:
        ``{"section_number", "standards_text", "subheading_rule"}``。
    """
    if stable:
        return {"section_number": "", "standards_text": "", "subheading_rule": ""}
    return {"section_number": section_number,
            "standards_text": standards_text,
            "subheading_rule": subheading_rule}


def build_chapter_extras_block(*, section_number: str, standards_text: str,
                               subheading_rule: str,
                               chapter_checkpoint_block: str = "") -> str:
    """把从 system 中剥离出来的逐章提示内容，原样组装成本章任务差异的末段。

    ⚠️ 纪律：这里是**唯一**组装点 —— 若同时在 system 里保留一份，
    提示词会出现两处相同的编制依据/检查点，既浪费 token 又给模型重复约束。

    Args:
        section_number: 本章展示态编号（即使前缀稳定化也必须告知模型，
            否则 AI 无从决定本章内部子标题编号）。
        standards_text / subheading_rule / chapter_checkpoint_block:
            逐章量，空串表示该段不参与（不产生空标题）。

    Returns:
        以换行结尾的多行文本；只有编号时最短为「【本章编写定位】：」段。
    """
    parts = ["\n【本章编写定位】：\n", f"本章目录编号：{section_number}\n"]
    if standards_text:
        parts.append(f"\n【本章编制依据】：\n{standards_text}\n")
    if subheading_rule:
        parts.append(f"\n【本章子标题编号规范】：\n{subheading_rule}\n")
    if chapter_checkpoint_block:
        parts.append("\n" + chapter_checkpoint_block.lstrip("\n") + "\n")
    return "".join(parts)


def build_chapter_layered_messages(
    *,
    sys_prompt: str,
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
    chapter_extras: str = "",
    chart_quota_hint: str = "",
    chapter_facts_text: str = "",
) -> tuple[list[dict], str]:
    """「前缀稳定化」版章节 messages：同一方案各章节共享最长公共前缀。

    与 :func:`build_chapter_user_content` 的区别**只是组织方式**，段落文案逐字沿用：

    ============================  ============================================
    消息                          内容
    ============================  ============================================
    ``system``                    仅整案常量（逐章变量已由调用方注入为空串）
    ``user`` ①整案稳定上下文      方案名称/类型/项目概述/施工内容/全局事实/知识库/生成标准块
    ``user`` ②章节树上下文        上级章节链/上级要点/同级章节
    ``user`` ③本章任务差异        当前章节编号/标题/目标字数/前序结尾 + ``chapter_extras``
    ============================  ============================================

    ✅ 为什么必须分层（依据《标书智能体（四）》）：各厂商 Prompt Cache 命中的是
    **请求前缀**，而不是「某段内容看起来一样」。旧实现把「上级章节链→当前章节→
    目标字数→全局事实→知识库」串在同一条 user 里，稳定大块被排在逐章差异之后，
    于是同方案每换一章前缀即失效 —— 几十万字方案里同一份项目概述/施工内容/
    全局事实/知识库被反复全价计费。分层后它们成为**可复用前缀**。

    Args:
        sys_prompt: 已渲染好的 system 文本（调用方负责「逐章变量注入为空串」）。
        chapter_extras: 从 system 中剥离出来的**逐章**提示片段（编制依据 /
            子标题编号规范 / 本章审核检查点要求等），由调用方拼装；为空时
            本函数不额外追加任何内容。
        chart_quota_hint: ✅ R58「本章图表配额」提示（程序实时核算的剩余配额）。
            同样是逐章变化量，故追加在**任务差异段末尾**，不影响前缀稳定性。
        chapter_facts_text: ✅ R58「本章专属事实」文本（已排除整案核心事实）。
            与 chart_quota_hint 同属逐章变化量 → 追加到任务差异段。
            ``facts_text`` 则应是**整案核心**事实（逐章相同），才配得上「稳定」二字。
        其余参数与 :func:`build_chapter_user_content` 同义同名，便于一一对照。

    Returns:
        ``(messages, user_content)``：
        - ``messages``：供首轮 AI 调用；空段会被跳过（不发空串 user，
          部分 provider 显式拒绝空 content）。
        - ``user_content``：三段按最终顺序拼接的完整文本，供**续写轮**复用
          （``build_continuation_messages(user_ctx=...)``），语义等价于旧的
          单条 user 串。
    """
    # ① 整案稳定上下文：同一方案下**所有章节逐字相同**
    stable = (
        f"【方案名称】：{scheme.get('name', '')}\n"
        f"【方案类型】：{scheme.get('type', '')}\n"
        f"【项目概述】：{project_brief}\n"
    )
    if content_scope:
        stable += (
            "【方案名称主要施工内容】（本章内容必须服务于其中对应的"
            "施工内容项；与本章无关的项不得写入）：\n"
            f"{content_scope}\n")
    if facts_text:
        stable += (
            "\n【全局事实变量（唯一可信数据源）】：\n"
            + build_facts_header(eff_standard)
            + f"{facts_text}\n")
    if knowledge_text:
        stable += (
            "\n【项目知识库素材】：\n"
            "（企业管理制度/工艺要点/既有素材，"
            "与本项目相关的表述应遵循其口径，数据仍以上方全局事实为准）\n"
            f"{knowledge_text}\n")
    stable += "\n\n" + build_user_block(eff_standard)

    # ② 章节树上下文：同一父章节下**各兄弟章节逐字相同**
    tree = f"【上级章节链】：{parent_chain}\n"
    if parent_points:
        tree += "【上级章节要点】：\n" + "\n".join(parent_points) + "\n"
    tree += (
        "【同级章节（请避免内容重复）】：\n" + sibling_lines + "\n"
        if sibling_lines else "【同级章节（请避免内容重复）】：（无）\n")

    # ③ 本章任务差异：每次请求唯一不同的部分，必须放在**最后**
    task = f"【当前章节编号】：{section_number}\n"
    task += f"【当前章节】：{leaf['title']} — {leaf.get('description', '')}\n"
    task += f"【目标字数】：{word_budget_hint or f'{word_budget}字'}\n"
    if prev_sibling_summary:
        task += (
            f"【前序同级章节结尾参考（衔接风格，勿重复）】：{prev_sibling_summary}\n")
    if chapter_extras:
        task += chapter_extras if chapter_extras.endswith("\n") else chapter_extras + "\n"
    if chapter_facts_text:
        task += ("\n【本章专属事实（仅本章相关；整案通用事实见上方稳定上下文）】：\n"
                 + (chapter_facts_text if chapter_facts_text.endswith("\n")
                    else chapter_facts_text + "\n"))
    if chart_quota_hint:
        task += (chart_quota_hint if chart_quota_hint.endswith("\n")
                 else chart_quota_hint + "\n")

    segments = [s for s in (stable, tree, task) if s.strip()]
    user_content = "".join(segments)
    messages: list[dict] = [{"role": "system", "content": sys_prompt}]
    messages.extend({"role": "user", "content": seg} for seg in segments)
    return messages, user_content


def build_continuation_messages(
    *,
    user_ctx: str,
    cont_tail: str,
    wc: int,
    word_budget: int,
    cont_prompt: str,
    eff_standard: str,
    total_cap_factor: float = 1.1,
) -> list[dict]:
    """构造续写轮 messages（与首轮同口径的定位 + 续写指令 + 模式提醒）。

    Args:
        user_ctx: 本轮 user 上下文（首轮用完整上下文，后续轮用章节定位）。
        cont_tail: 前文尾部（``_safe_tail`` 截断后的结尾片段）。
        wc: 当前正文字数（不含图表代码块）。
        word_budget: 本章目标字数。
        cont_prompt: ``content_continue_system`` 渲染出的续写 system 提示词。
        eff_standard: 生效生成标准（precise / fuzzy；驱动续写轮模式提醒）。
        total_cap_factor: ✅ R66（2026-10-11）续写后总字数上限系数，
            默认 1.1（= 1 + 容差 0.1），与旧实现硬编码 1.1 逐字一致；
            调用方可按 settings.word_budget_tolerance 传 1+tol。

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
            f"补充后总字数上限为 {int(word_budget * total_cap_factor)} 字"
            f"（不得超出，接近上限时自然收尾）。"
            if wc < word_budget else
            f"当前字数{wc}已达到目标字数，请仍以上文为基础继续补充实质性内容"
            "（具体工序细节、控制要点、检验标准、安全注意事项等），"
            f"但**补充后总字数不得超过 {int(word_budget * total_cap_factor)} 字**，"
            "不要重复前文，不要输出图表代码块。")
            + std_hint}]


def should_continue_round(
    *,
    wc: int,
    word_budget: int,
    continue_count: int,
    min_passes: int,
    max_rounds: int,
    min_floor_chars: int = 0,
) -> bool:
    """续写轮次判定：不足 80% 或未达最少轮次、且未超过最大轮次。

    与正文生成循环 ``while (...)` 的口径逐字一致（P1-1：续写不重试失败调用；
    这里只决定"要不要再补一轮"）。

    ✅ R66（2026-10-11）``min_floor_chars``：三级目录正文下限（300 字）。
    低于下限同样触发续写 —— 否则预算 800 的叶子写到 650（<0.8×800=640 才触发，
    650 恰好放行）没问题，但**预算极小**的脏数据章（如 budget=100 → 阈值 80）
    写 90 字就"达标"，违反 300 字要求。判据取
    ``max(budget × WORD_UNDER_RATIO, min_floor_chars)``；
    缺省 0 时与旧实现逐字等价（`max(x, 0) == x` 对非负成立）。
    """
    try:
        budget = int(word_budget or 0)
    except (TypeError, ValueError):
        budget = 0
    try:
        floor = int(min_floor_chars or 0)
    except (TypeError, ValueError):
        floor = 0
    under_line = max(budget * WORD_UNDER_RATIO, floor)
    return ((wc < under_line or continue_count < min_passes)
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