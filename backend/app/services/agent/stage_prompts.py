"""P3 — 阶段提示词构造器（移植自易标 Yibiao 2.30.20 ``outlineGenerationTaskV2.cjs`` L599–858）。

移植范围与函数对应（行号均指参照源码
``C:\\...\\Temp\\yibiao_outline\\outlineGenerationTaskV2.cjs``）：

- L599 ``createOutputFileRequirements`` → ``create_output_file_requirements``
- L604 ``createOutputRepairPrompt`` → ``create_output_repair_prompt``
- L624 ``createInitialPrompt`` → ``create_initial_prompt``
- L661 ``createNoTechnicalScoreChildrenPrompt`` → ``create_no_technical_score_children_prompt``
- L684 ``createLeafAllocationPrompt`` → ``create_leaf_allocation_prompt``
- L704 ``createScorePlanningPrompt`` → ``create_score_planning_prompt``
- L729 ``createChildrenPrompt`` → ``create_children_prompt``
- L772 ``createLeafAdjustmentPrompt`` → ``create_leaf_adjustment_prompt``
- L796 ``createNoTechnicalScoreReviewPrompt`` → ``create_no_technical_score_review_prompt``
- L825 ``createOutlineReviewPrompt`` → ``create_outline_review_prompt``
- L34 ``ORIGINAL_ONLY_DIRECTORY_RULE`` → 同名常量（见偏离点 2）

对原文的全部偏离点：

1. 文件名常量（L10–L16 的 7 个）与 ``STAGE_OUTPUT_FILES``（L17–L24）一律
   ``from .validation import ...`` 复用本仓 P2 实现；经逐一比对，本仓常量值
   与易标原文完全相同，提示词文本因此未发生任何变化。
2. ``ORIGINAL_ONLY_DIRECTORY_RULE``（L34）：已读确认本仓 ``validation.py``
   未导出该常量，故在本模块按 L34 逐字定义并导出。
3. ``CONTENT_MODES``（L27）：L599–858 的提示词正文从未引用它，故本模块
   不导入也不定义；该常量已按易标 L27 逐字补齐于 ``validation.py``
   （接线阶段遗留②修复），供 Schema 与产物校验层引用。
4. JS 模板字符串 ``${}`` 插值改为 Python f-string 或显式拼接；三元表达式改为
   显式 ``if`` 分支；``=== null`` 改为 ``is None``；``Math.max(a, b)`` 改为
   ``max(a, b)``；``join('、')`` / ``join('\\n\\n')`` 改为 ``str.join``。
   产出文本与 JS 在相同入参下逐字一致（已用 Node 提取原文函数做字节级比对）。
5. 注入参数保持为参数、不自行实现：
   - ``create_output_repair_prompt(request, meta)`` 原文按属性访问
     ``request.kind`` / ``meta.workflow_stage``（对应易标对象属性）；
     接线阶段（2026-10-25）为对齐 ``stage_runner`` 的 dict 注入契约
     扩展为 dict / 属性双形态读取，属性形态行为与原文逐字一致；
   - 两个审核构造器的 ``input_files`` 为 ``list[dict]``，每项含 ``path`` 与
     ``content`` 键（对应易标 ``inputFiles`` 数组的 ``.path`` / ``.content``）。
6. 提示词内的工作区文件名（原方案.md、响应文件要求.md、项目概述.md、
   技术评分信息.md、参考知识库目录）经比对与本仓运行期约定一致，未替换。

保留原文（刻意不替换，供主线复核）：

- L688「请继续使用当前 Pi Session 已读取的技术评分信息……」——易标私有运行时
  措辞，因产出逐字保真优先而原样保留；
- 工具名 ``json-validation`` / ``write`` / ``edit`` / ``grep`` / ``read`` /
  ``ask-user`` / ``report-failure``——本仓 P1 已按同名工具移植；
  ``bash`` 本仓 P1 **未**移植（工具表中不存在该工具），构造器产出保持
  逐字保真不改写，由接线层在提示词出口统一消歧（见
  ``outline_agent_flow.disambiguate_prompt_tools``）；
- L803/L835「宿主程序计算的确定性审核结果」等宿主程序措辞；
- 所有红线句（如「只在回复中输出 JSON 不算写入」「程序已为 X 预置 Schema，
  可用 json-validation 自查」）逐字保留。
"""
from __future__ import annotations

from .validation import (
    LEAF_ALLOCATION_CONTEXT_FILE,
    LEAF_ALLOCATION_FILE,
    OUTLINE_OUTPUT_FILE,
    OUTLINE_REVIEW_CONTEXT_FILE,
    OUTLINE_REVIEW_FILE,
    SCORE_DIRECTORY_PLAN_FILE,
    STAGE_OUTPUT_FILES,
    TECHNICAL_SCORE_GROUPS_FILE,
)

#: 仅原方案模式的目录红线（易标 L34 逐字，validation.py 未提供故本模块定义）。
ORIGINAL_ONLY_DIRECTORY_RULE = (
    "目录来源仅限原方案.md：提取并补齐原方案中实际存在的目录，保留其顺序和层级；"
    "不得依据其他资料或专业经验新增原方案中不存在的章节，也不得为凑字数或小节数量"
    "新增、拆分章节。"
)


def create_output_file_requirements(stage: str) -> str:
    """所有阶段共用的必需文件清单（易标 L599–601）。

    - ``stage``：``STAGE_OUTPUT_FILES`` 的键。
    """
    return (
        f"本阶段必需文件：{'、'.join(STAGE_OUTPUT_FILES[stage])}。"
        "程序已在当前工作目录根目录准备好这些文件，你只负责填入或修改内容，"
        "不要自行创建其他位置的副本、改名或删除文件。已有目录必须保留用户确认的内容和节点 ID。"
        "所有必需文件都必须包含完整、有效且符合 Schema 的纯 JSON；"
        "空文件不算完成，只在回复中输出 JSON 不算写入。"
        "全部文件写入并通过校验后才能结束本阶段。"
    )


def create_output_repair_prompt(request, meta):
    """仅将产物校验失败交回当前会话修复（易标 L604–607）。

    - ``request``：``kind == 'execution'`` 时返回 ``None``（执行错误不走
     产物修复通道）；
    - ``meta``：需含 ``workflow_stage``（阶段键）。

    ✅ 接线契约对齐（2026-10-25）：``stage_runner.run_stage`` 的两个调用点
    （执行档 / 提交档）注入的是 **dict**（docstring L33–35 明确
    ``request['kind']``，``base_meta()`` 同为 dict），而易标原文与本仓
    单测注入的是属性形态对象。此处按 dict / 属性双形态读取（对齐
    ``stage_runner._field`` 的兜底模式），两侧行为一致：
    dict 缺 ``kind`` 键视为非 execution，缺 ``workflow_stage`` 键时
    ``str(None)`` 会立刻被 ``STAGE_OUTPUT_FILES`` 拦成 KeyError —— 与
    属性形态缺属性抛 AttributeError 同属「调用方未按契约传参」，不吞错。
    """
    if isinstance(request, dict):
        kind = request.get("kind")
    else:
        kind = getattr(request, "kind", None)
    if kind == "execution":
        return None
    if isinstance(meta, dict):
        workflow_stage = meta.get("workflow_stage")
    else:
        workflow_stage = getattr(meta, "workflow_stage", None)
    return (
        f"{create_output_file_requirements(workflow_stage)}\n"
        "沿用已有材料和用户确认结果，只修复本次提交报告中的问题；"
        "已通过的文件保留，不要重复询问已确认事项。"
    )


def create_initial_prompt(
    task_instruction: str,
    *,
    standalone_technical: bool = False,
    no_technical_score_mode: bool = False,
) -> str:
    """初始一级目录生成提示（易标 L624–658）。

    - ``standalone_technical``：单独装订技术文件模式；
    - ``no_technical_score_mode``：无技术评分项模式（仅
      ``standalone_technical`` 同时为真时生效）。
    """
    if standalone_technical:
        if no_technical_score_mode:
            goal = "我们的目标是为没有技术评分项的单独装订技术文件准备一级目录。"
            mode_requirements = (
                "6. 本模式只生成技术文件独立分册：一级目录必须是适合展开技术正文的专业主题，"
                "attr 必须为“技术”，content_mode 必须为 ai-generate。\n"
                "7. 招标文件已确认没有可用的技术评分项。优先采用已有资料中的明确要求；"
                "资料没有给出目录结构时，根据项目类型和专业经验补充通用、合理的技术方案主题，"
                "但不得编造具体项目事实、参数、业绩或承诺。\n"
                "8. 不得创建“技术方案”“项目管理方案”“监理大纲”“监理大纲（暗标）”"
                "“施工组织设计”“技术标”等外层总目录，也不得加入商务/资信、投标函、"
                "授权委托书等非技术章节。"
            )
        else:
            goal = "我们的目标是为单独装订的技术文件准备一级目录。一级目录必须直接对应技术评分大项。"
            mode_requirements = (
                "6. 本模式只生成技术文件独立分册：只能保留适合展开技术正文的评分大项，"
                "attr 必须为“技术”，content_mode 必须为 ai-generate。\n"
                "7. 每个一级目录直接对应一个技术评分大项，并保持评分大项的原顺序和正式表述；"
                "不得创建“技术方案”“项目管理方案”“监理大纲”“监理大纲（暗标）”"
                "“施工组织设计”“技术标”等外层总目录，也不得加入商务/资信、投标函、"
                "授权委托书等非技术章节。\n"
                '8. 完整结构示例：{"outline":[{"id":null,"title":"评分大项一",'
                '"description":"评分大项一的技术响应范围","attr":"技术",'
                '"content_mode":"ai-generate"},{"id":null,"title":"评分大项二",'
                '"description":"评分大项二的技术响应范围","attr":"技术",'
                '"content_mode":"ai-generate"}]}。'
            )
    else:
        goal = "我们的目标是为编写响应文件/投标文件准备一级目录。"
        mode_requirements = (
            "6. 每个一级目录当前都是叶子节点，必须根据属性和后续处理方式填写 content_mode："
            "技术方案正文使用 ai-generate；attr=商务/资信或业绩时必须使用 template-fill；"
            "attr=目录时必须使用 directory-generate；attr=报价时必须使用 manual-fill；"
            "无法归类的特殊内容使用 other，并在 content_mode_note 说明原因。\n"
            '7. 完整结构示例：{"outline":[{"id":null,"title":"投标文件目录",'
            '"description":"投标文件目录说明","attr":"目录",'
            '"content_mode":"directory-generate"},{"id":null,"title":"技术方案",'
            '"description":"技术方案目录说明","attr":"技术",'
            '"content_mode":"ai-generate"},{"id":null,"title":"投标报价",'
            '"description":"投标报价目录说明","attr":"报价",'
            '"content_mode":"manual-fill"}]}。'
            "content_mode_note 只在 content_mode=other 且确有说明时填写。"
        )
    return (
        "请只在当前工作目录内工作。\n"
        f"{create_output_file_requirements('initial-outline')}\n"
        "\n"
        "任务：\n"
        f"{goal}\n"
        f"{task_instruction}\n"
        "\n"
        f"请生成一级目录 JSON，并将结果写入 {OUTLINE_OUTPUT_FILE}。\n"
        "\n"
        "字段要求：\n"
        "1. 顶层必须是对象，唯一字段 outline 是一级目录数组；此阶段暂时不要生成 children。\n"
        "2. 所有新节点 id 填 null，程序接收后分配唯一 ID。数组顺序表示目录顺序；"
        "number 由程序计算，不要用编号充当 ID。\n"
        "3. title 必须是可直接用于投标文件目录的正式标题，"
        "不得包含“附件1”“附件一”“第一章”等编号或前缀。\n"
        "4. description 是目录说明。\n"
        "5. attr 必须从“通用”“商务/资信”“技术”“其他”“目录”“报价”“业绩”中选择。\n"
        f"{mode_requirements}\n"
        f"8. {OUTLINE_OUTPUT_FILE} 必须是纯 JSON，不包含 Markdown 代码块或解释文字。\n"
        f"9. 程序已为 {OUTLINE_OUTPUT_FILE} 预置 Schema，可用 json-validation 自查，"
        "只传 file_path；本阶段结束后程序统一校验，不通过会退回问题清单继续修复。"
    )


def create_no_technical_score_children_prompt(
    *,
    target_leaf_count,
    standalone_technical: bool,
    original_only: bool = False,
) -> str:
    """无技术评分项模式的正文目录生成规则（易标 L661–682）。

    - ``target_leaf_count``：最终 ai-generate 叶子总数目标，``None`` 对应
      易标 ``null``（未设置总字数目标）；
    - ``original_only``：仅使用原方案目录模式（引入
      :data:`ORIGINAL_ONLY_DIRECTORY_RULE`）。
    """
    if target_leaf_count is None:
        leaf_instruction = (
            "本次未设置总字数目标，请根据项目复杂度自主确定合理的“AI生成”叶子节点数量。"
        )
    else:
        leaf_instruction = (
            f"最终完整目录合计应有约 {target_leaf_count} 个 "
            "content_mode=ai-generate 的叶子节点。"
        )
    if standalone_technical:
        scope_instruction = "所有一级目录均属于技术文件，只生成技术方案正文相关的二级及以下目录。"
    else:
        scope_instruction = (
            "只扩展 attr=技术 且 content_mode=ai-generate 的一级目录；"
            "其他一级目录保持原样，不得增加子目录。"
        )
    reading = (
        "原方案.md" if original_only
        else "响应文件要求.md、项目概述.md，以及存在的原方案.md 和参考知识库目录"
    )
    rule = (
        ORIGINAL_ONLY_DIRECTORY_RULE if original_only else
        "优先采用资料中的明确要求，并根据项目类型、实施内容和专业逻辑组织技术方案目录；"
        "资料不足时可以用专业经验补充通用、合理的章节，但不得编造具体项目事实、参数、业绩或承诺。"
    )
    return (
        "用户已确认招标文件没有技术评分项，请直接根据确定的无技术评分项规则生成完整目录，"
        "不要判断是否存在评分项，也不要创建评分清单或评分映射。\n"
        f"{create_output_file_requirements('children_generation')}\n"
        "\n"
        f"请阅读 {OUTLINE_OUTPUT_FILE}、{reading}，然后覆盖写回 {OUTLINE_OUTPUT_FILE}。\n"
        "\n"
        "要求：\n"
        f"1. {scope_instruction}\n"
        "2. 一级目录的数量、顺序、id、title、description 和 attr 已由用户确认，"
        "必须保持不变；未扩展为父节点的一级目录还必须保留原 content_mode。\n"
        f"3. {rule}\n"
        f"4. {leaf_instruction}目录质量优先于机械凑数。\n"
        "5. 每个最终叶子节点必须填写 content_mode：技术方案正文为 ai-generate；"
        "商务/资信和业绩材料为 template-fill；投标文件目录为 directory-generate；"
        "报价为 manual-fill；其他特殊内容为 other，并用 content_mode_note 说明。"
        "父节点不得包含 content_mode 或 content_mode_note。\n"
        "6. 任意非叶子节点的 children 至少包含两个节点；目录最多六级，已有节点保留 id，"
        "新节点 id 填 null；number 是程序计算的显示编号。\n"
        "7. title 只写正式、专业的纯标题，不包含章节编号或 Markdown 标记；"
        "避免重复、近义和空泛目录。\n"
        f"8. 程序已为 {OUTLINE_OUTPUT_FILE} 预置 Schema，可用 json-validation 自查，"
        "只传 file_path；本阶段结束后程序统一校验，不通过会退回问题清单继续修复。"
    )


def create_leaf_allocation_prompt(*, standalone_technical: bool = False) -> str:
    """为技术一级目录分配“AI生成”叶子数量的提示（易标 L684–700）。"""
    if standalone_technical:
        allocation_instruction = (
            "优先为每个目录分配至少 2 个；总目标不足时允许部分目录分配 1 个，"
            "表示保留一级目录本身作为叶子且不生成 children。除 1 以外不得分配少于 2 个，"
            "禁止形成只有一个子节点的冗余层级。"
        )
    else:
        allocation_instruction = "每个目录至少分配 2 个。"
    return (
        "请继续使用当前 Pi Session 已读取的技术评分信息、知识库、原方案和目录规划，"
        "为多个技术一级目录分配“AI生成”叶子数量。\n"
        f"{create_output_file_requirements('leaf_allocation')}\n"
        "\n"
        "要求：\n"
        f"1. 阅读 {OUTLINE_OUTPUT_FILE}、{TECHNICAL_SCORE_GROUPS_FILE}、"
        f"{SCORE_DIRECTORY_PLAN_FILE} 和 {LEAF_ALLOCATION_CONTEXT_FILE}。\n"
        "2. 综合各一级目录负责的评分项数量、评分细项数量、内容复杂度以及已读取的参考资料，"
        "合理分配 allocatable_ai_leaf_count。\n"
        "3. allocations 必须恰好覆盖 context 中 technical_branches 的全部 root_id，"
        f"每个 root_id 只出现一次。{allocation_instruction}"
        "root_id 是不会因目录重新编号而变化的内部稳定标识。\n"
        "4. 所有 leaf_count 之和必须等于 allocatable_ai_leaf_count。\n"
        f"5. 将结果写入 {LEAF_ALLOCATION_FILE}，保留 context 中的 mode、"
        "target_ai_leaf_count、fixed_ai_leaf_count 和 allocatable_ai_leaf_count。\n"
        f"6. 不要修改 {OUTLINE_OUTPUT_FILE}、{TECHNICAL_SCORE_GROUPS_FILE} "
        f"或 {SCORE_DIRECTORY_PLAN_FILE}。\n"
        '7. 输出格式为 {"mode":"allocated","target_ai_leaf_count":20,'
        '"fixed_ai_leaf_count":1,"allocatable_ai_leaf_count":19,'
        '"allocations":[{"root_id":"从当前目录复制根节点ID","leaf_count":10},'
        '{"root_id":"另一根节点ID","leaf_count":9}]}。\n'
        f"8. 程序已为 {LEAF_ALLOCATION_FILE} 预置 Schema，可用 json-validation 自查，"
        "只传 file_path；本阶段结束后程序统一校验，不通过会退回问题清单继续修复。"
    )


def create_score_planning_prompt(*, standalone_technical: bool = False) -> str:
    """技术评分项结构化和评分项目录规划提示（易标 L704–727）。"""
    if standalone_technical:
        placement_instruction = (
            "4. 当前采用“技术文件独立成册”："
            f"{OUTLINE_OUTPUT_FILE} 中每个一级根节点本身就应对应一个技术评分大项。"
            "每个根节点建立一个 branch，score_item_level 固定为 1，"
            "mappings 只填写与该根标题对应的评分大项，target_title 必须与 root_title 完全一致；"
            "不得再创建“技术方案”“项目管理方案”“监理大纲”“监理大纲（暗标）”"
            "“施工组织设计”“技术标”等外层分支。\n"
            "5. 一级根节点与评分大项默认严格一一对应；发现缺失、重复、合并或顺序不一致时，"
            "必须作为一级目录调整向用户说明并取得批准。"
            "detail_points 只用于后续生成根节点以下的目录。"
        )
        plan_example = (
            '{"branches":[{"root_id":"从当前目录复制根节点ID","root_title":"评分大项一",'
            '"score_item_level":1,"mappings":[{"requirement_id":"R1",'
            '"target_title":"评分大项一"}]}],"extra_titles":[],"allow_root_changes":false}'
        )
    else:
        placement_instruction = (
            "4. 判断技术方案位于哪些目录分支，以及每个分支内评分项对应节点应统一处于哪个层级。"
            "不同分支可以使用不同层级，不预设必须是二级目录。"
            "优先选择 attr=技术且 content_mode=ai-generate 的一级目录；"
            "template-fill、directory-generate、manual-fill 和 other 是特殊处理叶子，"
            "不得作为普通技术方案分支展开，除非先向用户说明并取得调整批准。\n"
            "5. 默认每个评分项对应一个独立同层级节点，节点标题与评分大项基本一一对应；"
            "detail_points 用于后续生成更下级目录。"
        )
        plan_example = (
            '{"branches":[{"root_id":"从当前目录复制根节点ID","root_title":"技术方案",'
            '"score_item_level":2,"mappings":[{"requirement_id":"R1",'
            '"target_title":"评分大项目录标题",'
            '"additional_titles":["经批准拆分出的同级标题"],'
            '"adjustment_note":"用户批准的调整说明"}]}],'
            '"extra_titles":[{"root_id":"从当前目录复制根节点ID",'
            '"title":"经批准增加的同层级标题","reason":"增加原因"}],'
            '"allow_root_changes":false}'
        )
    return (
        "用户已经确认最终保留的一级目录，"
        f"{OUTLINE_OUTPUT_FILE} 已由程序重新整理并编号。"
        "工作区也已加入技术评分信息和用户选择的参考资料。\n"
        f"{create_output_file_requirements('score-planning')}\n"
        "\n"
        "请完成技术评分项结构化和目录规划：\n"
        f"1. 阅读 {OUTLINE_OUTPUT_FILE}、技术评分信息.md，"
        "以及存在的原方案.md 和参考知识库目录。\n"
        "2. 程序已确认本任务存在技术评分项。只从技术评分信息.md 的“技术评分项”中"
        "提取适合在技术方案中一一响应、展开编写的评分大项。“技术评分要求”只能作为"
        "评分标准、扣分规则和编写约束，不得提取为评分项。\n"
        f"3. 将评分大项写入 {TECHNICAL_SCORE_GROUPS_FILE}，完整结构为 "
        '{"groups":[{"requirement_id":"R1","title":"评分大项",'
        '"description":"关注内容","detail_points":["关键评分细项"]}]}'
        "。根对象只能包含 groups；保持原顺序、专业术语和关键评分细项，"
        "requirement_id 使用连续的 R1、R2 格式。\n"
        f"{placement_instruction}\n"
        "6. 只有以下偏离需要用户批准：合并或拆分评分项、遗漏评分项对应节点、"
        "增加评分项中不存在的同层级大项、改变分支评分项目标层级，以及新增、删除、合并或"
        "调整用户已确认的一级目录。普通标题规范化和评分项下级目录扩展不需要询问。\n"
        "7. 存在至少一个有效评分项时，无论是否存在偏离，都必须调用一次 ask-user 让用户确认。"
        "没有偏离时，question 只说明你分析得出的技术方案所在目录和评分项所在层级，"
        "最多使用两句话且不要使用列表；存在偏离时，只补充实际需要用户批准的偏离及影响，"
        "存在多个实际确认事项时才使用简单 Markdown 分行列出。"
        "question、选项名称和选项说明不得复述、概括或改写本任务 Prompt 中的要求，"
        "只呈现你分析后确实需要用户确认的结论或不确定事项。第一项给出推荐方案；"
        "另提供一个名为“调整目录安排”等明确业务名称的选项并设置 custom=true，"
        "让用户说明希望调整的位置或层级，其他选项均设置 custom=false。\n"
        f"8. 根据用户回答写入 {SCORE_DIRECTORY_PLAN_FILE}。完整字段层级示例："
        f"{plan_example}。branches 中 root_id 必须复制当前 {OUTLINE_OUTPUT_FILE} 中"
        "对应根节点的 id，root_title 填其标题；统一填写 score_item_level，"
        "并让每个 requirement_id 在 mappings 中恰好出现一次。后续重排或改名一级目录时，"
        "root_id 保持不变；不得填写显示编号或自行编造 ID。经用户批准新增一级目录时，"
        f"该分支 root_id 填 null，root_title 与 {OUTLINE_OUTPUT_FILE} 中新增目录的标题"
        "完全一致，程序在本阶段结束后分配 ID 并回填。默认一一对应；经用户批准合并时，"
        "多个 mapping 可以使用相同 target_title；经用户批准拆分时才填写 "
        "mapping.additional_titles；合并或拆分时才填写 adjustment_note。extra_titles "
        "必须位于根对象，经批准增加同层级大项时才写入条目，否则使用空数组。\n"
        "9. 默认锁定一级目录，allow_root_changes=false；只有用户明确批准一级目录调整时"
        f"才设为 true。用户批准新增一级目录时，须在本阶段把新增目录写入 {OUTLINE_OUTPUT_FILE}。\n"
        f"10. 程序已为 {TECHNICAL_SCORE_GROUPS_FILE} 和 {SCORE_DIRECTORY_PLAN_FILE} "
        "预置 Schema。可用 json-validation 自查，只传 file_path；本阶段结束后程序统一校验"
        "两份文件及其与一级目录的对应关系，不通过会退回问题清单继续修复。"
        "如果现有材料无法在不编造评分项的情况下通过校验，调用 report-failure。\n"
        f"11. 此阶段对 {OUTLINE_OUTPUT_FILE} 只允许插入经用户批准新增的一级目录："
        "id 填 null，attr 为“技术”，content_mode 为 ai-generate，不包含 children，"
        "标题不得与其他一级目录重复；已有一级目录的 id、title、description、attr、"
        "content_mode 和相对顺序保持不变。删除、合并、改名、排序等其他已批准的一级目录"
        "调整留到生成完整目录时执行。不要删除、清空或重命名任何任务文件。"
    )


def create_children_prompt(
    *,
    has_original_plan: bool,
    original_only: bool,
    target_leaf_count,
    allow_root_changes: bool,
    standalone_technical: bool,
) -> str:
    """按评分项目录规划生成完整目录的提示（易标 L729–770）。"""
    if not has_original_plan:
        branch_instruction = "没有原方案时，以技术评分信息.md 为主要依据生成目录。"
    elif original_only:
        branch_instruction = (
            "已选择仅使用原方案目录：以原方案.md 为主建立规划层级及以下目录，"
            "再用技术评分信息.md 补充原方案语义上确实缺失的技术要求，"
            "意思相近的内容不要重复添加。"
        )
    else:
        branch_instruction = (
            "已提供原方案且允许 AI 补充：以技术评分信息.md 为主，"
            "在评分项目录规划指定的层级覆盖关键大项，原方案.md 用于辅助生成更下级目录。"
        )
    if target_leaf_count is None:
        leaf_instruction = (
            "本次未设置总字数目标，请根据材料复杂度自主确定合理的“AI生成”叶子节点数量。"
        )
    else:
        leaf_instruction = (
            f"严格参考 {LEAF_ALLOCATION_FILE} 中的分配，使最终完整目录合计约有 "
            f"{target_leaf_count} 个 content_mode=ai-generate 的叶子节点。"
        )
    if allow_root_changes:
        root_instruction = (
            f"用户已批准 {SCORE_DIRECTORY_PLAN_FILE} 中记录的一级目录调整，"
            "只能按该规划进行必要修改并重新编号。上一阶段新增的一级目录已写入当前目录"
            "并分配 id，保留其 id，不要重复新增。"
        )
    else:
        root_instruction = (
            "一级目录的数量、顺序、id、title、description、attr 均已由用户确认，"
            "必须保持不变；未扩展为父节点的一级目录还必须保留其 content_mode。"
        )
    if standalone_technical:
        mapping_instruction = (
            "每个 branch 的 score_item_level=1，现有一级根节点本身就是评分项映射节点。"
            "不得在根节点下面再次生成同名评分项；只根据 detail_points、招标要求和"
            "专业逻辑生成其二级及以下目录。"
        )
        standalone_leaf_instruction = (
            "leaf_count=1 表示保留对应一级目录本身作为叶子，不得为其生成 children；"
            "leaf_count>=2 时才向下展开。"
        )
        outline_example = (
            '{"outline":[{"id":null,"title":"评分大项一","description":"评分大项说明",'
            '"attr":"技术","children":[{"id":null,"title":"响应内容一",'
            '"description":"具体响应内容","content_mode":"ai-generate"},'
            '{"id":null,"title":"响应内容二","description":"具体响应内容",'
            '"content_mode":"ai-generate"}]}]}'
        )
    else:
        mapping_instruction = "每个 branch 的 mappings 必须在该分支的 score_item_level 层级生成对应节点。"
        standalone_leaf_instruction = ""
        outline_example = (
            '{"outline":[{"id":null,"title":"投标文件目录",'
            '"description":"投标文件目录说明","attr":"目录",'
            '"content_mode":"directory-generate"},{"id":null,"title":"技术方案",'
            '"description":"技术方案说明","attr":"技术","children":['
            '{"id":null,"title":"评分大项","description":"评分大项说明","children":['
            '{"id":null,"title":"具体方案一","description":"具体方案说明",'
            '"content_mode":"ai-generate"},{"id":null,"title":"具体方案二",'
            '"description":"具体方案说明","content_mode":"ai-generate"}]},'
            '{"id":null,"title":"另一评分大项","description":"评分大项说明",'
            '"content_mode":"ai-generate"}]}]}'
        )
    return (
        f"请继续使用当前上下文，为 {OUTLINE_OUTPUT_FILE} 生成完整目录。"
        "生成方式和处理顺序由你自主决定，但必须严格遵循评分项目录规划。\n"
        f"{create_output_file_requirements('children_generation')}\n"
        "\n"
        "要求：\n"
        f"1. {branch_instruction}\n"
        f"2. 以 {TECHNICAL_SCORE_GROUPS_FILE} 为技术评分项权威清单，"
        f"以 {SCORE_DIRECTORY_PLAN_FILE} 为评分项与目录位置的权威规划。\n"
        f"3. {mapping_instruction} 规划的 root_id 对应根节点 id，"
        "节点本身不增加 root_id 字段；已有节点 id 不得随排序、改名或层级变化而改变。"
        "默认每个评分项形成一个独立节点；多个 mapping 使用相同 target_title 表示"
        "用户已批准合并，additional_titles 表示用户已批准将该评分项拆成多个同级节点。\n"
        "4. mappings 中的 target_title 是评分项对应节点标题，必须基本保持评分大项的"
        "专业表述；detail_points 主要用于生成其下级目录。\n"
        "5. extra_titles 是用户已批准增加的同层级大项；除此之外不得自行增加技术评分项中"
        "不存在的同层级标题。\n"
        "6. “技术评分要求”只能作为评分标准、扣分口径、判定规则和目录说明约束，"
        "不能生成独立评分项节点。\n"
        f"7. {root_instruction}\n"
        "8. 未纳入评分项目录规划的一级目录和分支保持原样，不得增加子目录。\n"
        "9. 如果存在参考知识库或原方案，只能用于完善评分项对应节点的下级结构，"
        "不得改变评分项映射或引入未经批准的同层级大项。\n"
        f"10. {leaf_instruction}{LEAF_ALLOCATION_FILE} 中 allocations 使用 root_id "
        f"指向技术分支的稳定节点 ID。{standalone_leaf_instruction}"
        "评分项完整对应和目录质量优先于数量目标。\n"
        "11. 每个最终叶子节点必须填写 content_mode：技术方案正文为 ai-generate；"
        "商务/资信和业绩材料为 template-fill；投标文件目录为 directory-generate；"
        "报价为 manual-fill；其他特殊内容为 other，并用 content_mode_note 说明。"
        "父节点不得包含 content_mode 或 content_mode_note。\n"
        "12. 任意非叶子节点的 children 至少包含两个节点，不要创建只有一个子节点的冗余层级。\n"
        "13. 目录层级可变，但最多六级；一级目录包含 attr，子目录不包含 attr。"
        "已有节点必须原样保留 id，新节点 id 填 null；数组顺序决定排序，"
        "number 由程序计算，不得把编号填入 id。\n"
        "14. title 只写纯标题，不包含章节编号或 Markdown 标记。\n"
        f"15. {OUTLINE_OUTPUT_FILE} 的完整结构示例：{outline_example}。"
        "示例中的 null 仅表示新节点；已有根节点必须从当前目录复制原 id 和 number，"
        "不能照抄 null。实际层级与标题按任务材料生成。\n"
        f"16. 程序已为 {OUTLINE_OUTPUT_FILE} 预置 Schema。可使用 write 覆盖写回或 edit "
        "修改该文件，内容较多时可分多次写入：首次用 write，之后用 edit 补充。"
        "可用 json-validation 自查，只传 file_path；本阶段结束后程序统一校验，"
        "不通过会退回问题清单继续修复。"
    )


def create_leaf_adjustment_prompt(
    target_leaf_count,
    actual_leaf_count,
    *,
    no_technical_score_mode: bool = False,
    original_only: bool = False,
) -> str:
    """叶子数量调整与三选项 ask-user 提示（易标 L772–793）。"""
    if no_technical_score_mode:
        adjustment_boundary = (
            "2. 用户已确认采用无技术评分项模式。选择调整时，只能根据现有资料和专业逻辑"
            "调整技术目录，不得编造具体项目事实、参数、业绩或承诺。\n"
            "3. 只通过合理调整 ai-generate 叶子的目录结构满足数量目标，"
            "不得为了凑数把 template-fill、directory-generate、manual-fill 或 other "
            "改成 ai-generate，也不得改变非 AI 叶子的处理模式。\n"
            "4. 调整后仍须保持完整根结构 "
            '{"outline":[一级目录节点]}，已有节点必须保留 id，新节点 id 填 null，'
            "number 由程序计算；用户已确认的一级目录不得修改；父节点只含 children，"
            "不含 content_mode，叶子节点只含 content_mode，不含 children。"
        )
    else:
        adjustment_boundary = (
            f"2. 用户选择“允许 Agent 自行调整”或“自定义需求”时，必须继续遵循 "
            f"{SCORE_DIRECTORY_PLAN_FILE}：不得删除、移动或改变评分项对应节点的目标层级，"
            "不得新增未经批准的同层级大项；优先调整评分项节点下面的更深层目录。\n"
            "3. 只通过合理调整 ai-generate 叶子的目录结构满足数量目标，"
            "不得为了凑数把 template-fill、directory-generate、manual-fill 或 other "
            "改成 ai-generate，也不得改变非 AI 叶子的处理模式。\n"
            "4. 调整后仍须保持完整根结构 "
            '{"outline":[一级目录节点]}，已有节点必须保留 id，新节点 id 填 null，'
            "number 由程序计算；技术一级目录的 id 必须与 "
            f"{SCORE_DIRECTORY_PLAN_FILE} 中对应的 root_id 一致，不能因增删、移动或"
            "重新编号而改变；父节点只含 children，不含 content_mode，"
            "叶子节点只含 content_mode，不含 children。"
        )
    only_rule_line = (
        ORIGINAL_ONLY_DIRECTORY_RULE
        if no_technical_score_mode and original_only else ""
    )
    return (
        f"程序计算当前完整目录共有 {actual_leaf_count} 个“AI生成”叶子节点，"
        f"目标是 {target_leaf_count} 个。\n"
        f"{create_output_file_requirements('leaf_adjustment')}\n"
        "\n"
        "请先调用一次 ask-user，说明目标数、当前数、差距及目录质量影响，"
        "只能按以下顺序提供三个固定选项，不得改名、增删或调整顺序：\n"
        "1. “接受当前结果”，custom=false：保持当前目录并进入最终审核。\n"
        "2. “允许 Agent 自行调整”，custom=false：由你在不破坏目录质量的前提下合理调整一次。\n"
        "3. “自定义需求”，custom=true：按用户填写的具体要求调整。\n"
        "\n"
        "根据本轮 ask-user 回答处理：\n"
        f"1. 用户选择“接受当前结果”时，不要修改 {OUTLINE_OUTPUT_FILE}。\n"
        f"{adjustment_boundary}\n"
        f"{only_rule_line}\n"
        f"5. 不要机械增加重复、空泛或近义目录。程序已为 {OUTLINE_OUTPUT_FILE} 预置 Schema，"
        "可用 json-validation 自查，只传 file_path；本阶段结束后程序统一校验，"
        "不通过会退回问题清单继续修复。"
    )


def create_no_technical_score_review_prompt(
    *,
    target_leaf_count,
    actual_leaf_count,
    original_only: bool = False,
    input_files,
) -> str:
    """无技术评分项模式的最终审核提示（易标 L796–822）。

    - ``target_leaf_count``：``None``（易标 ``null``）时不输出叶子数量行；
    - ``input_files``：``list[dict]``，每项含 ``path`` / ``content``。
    """
    if target_leaf_count is None:
        leaf_count_review = ""
    else:
        leaf_count_review = (
            "\n- “AI生成”叶子数量：程序计算目标为 "
            f"{target_leaf_count} 个，当前为 {actual_leaf_count} 个，"
            f"可接受范围为 {max(1, target_leaf_count - 2)} 至 "
            f"{target_leaf_count + 2} 个。"
        )
    if original_only:
        dimension = (
            "- 来源与完整性：根据下附原方案.md，核对目录是否忠实覆盖原方案中的章节。"
            f"{ORIGINAL_ONLY_DIRECTORY_RULE}不得以缺少通用技术主题为由新增目录。"
        )
    else:
        dimension = (
            "- 专业合理性：检查目录是否覆盖项目实施所需的通用技术主题，层级、颗粒度、"
            "逻辑顺序、标题和内容处理模式是否适合正式投标文件。\n"
            "- 事实边界：专业经验只能补充通用目录结构，不得编造具体项目事实、参数、"
            "业绩或承诺。"
        )
    return (
        "请对当前无技术评分项模式生成的完整技术方案目录执行最终审核，"
        "并在用户确认后完成必要修复。\n"
        f"{create_output_file_requirements('outline_review')}\n"
        "\n"
        "以下已提供本阶段最新完整材料。"
        f"{OUTLINE_REVIEW_CONTEXT_FILE} 是宿主程序计算的确定性审核结果，"
        "直接采用其中的叶子数量和结构检查。优先使用本次已提供的最新材料和程序计算结果，"
        "通常无需重复读取或重新统计。需要确认具体节点、补足上下文或排查结果异常时，"
        "可使用 grep、read 或 bash 定位相关部分；已有结果足以判断时直接继续审核。"
        "用户已确认招标文件没有技术评分项，不要判断、补造或检查评分项。\n"
        "\n"
        f"审核维度：{leaf_count_review}\n"
        "- 重复目录：检查子目录中是否存在重复、近义或含义重叠的节点。\n"
        f"{dimension}\n"
        "\n"
        "审核与修复流程：\n"
        "1. 必须先完整审核并形成问题清单，不得边审核边修改。\n"
        f"2. 没有问题时不要修改 {OUTLINE_OUTPUT_FILE}；写入 {OUTLINE_REVIEW_FILE}，"
        'status=passed、issues=[]、user_feedback=""。\n'
        "3. 仅标题专业化和明显重复子目录合并可静默修复并设置 status=simple_fix；"
        "一级目录调整、增加或拆分目录、明显结构重排和叶子数量超出合理范围"
        "必须先集中调用一次 ask-user。\n"
        "4. 用户要求修改时设置 status=user_feedback；用户要求保留现状时不修改目录并"
        "设置 status=user_refuse。修改完成后不得再次询问。\n"
        "5. 用户已确认的一级目录数量、顺序、标题、描述和属性不得修改。"
        "所有叶子保留合法 content_mode，父节点至少有两个 children，目录最多六级，"
        "id 与实际父子位置一致。\n"
        "6. issues 的 category 只能使用 leaf-count、duplicate-directory 或 "
        f"professional-structure。最终将完整问题清单和处理结果写入 {OUTLINE_REVIEW_FILE}。\n"
        f"7. 程序已为 {OUTLINE_OUTPUT_FILE} 和 {OUTLINE_REVIEW_FILE} 预置 Schema，"
        "可用 json-validation 自查，只传 file_path；本阶段结束后程序统一校验两份文件，"
        f"不通过会退回问题清单继续修复。未修改 {OUTLINE_OUTPUT_FILE} 时"
        "不要为了校验而重写目录。\n"
        "\n"
        "本阶段输入材料：\n"
        + "\n\n".join(
            f"【文件开始：{item['path']}】\n{item['content']}\n【文件结束：{item['path']}】"
            for item in input_files
        )
    )


def create_outline_review_prompt(
    *,
    target_leaf_count,
    actual_leaf_count,
    allow_root_changes: bool,
    input_files,
) -> str:
    """评分项模式的最终审核提示（易标 L825–858）。

    - ``target_leaf_count``：``None``（易标 ``null``）时不输出叶子数量行；
    - ``allow_root_changes``：对应 ``rootRequirement`` 三元分支；
    - ``input_files``：``list[dict]``，每项含 ``path`` / ``content``。
    """
    if target_leaf_count is None:
        leaf_count_review = ""
    else:
        leaf_count_review = (
            "\n- “AI生成”叶子数量：程序计算目标为 "
            f"{target_leaf_count} 个，当前为 {actual_leaf_count} 个，"
            f"可接受范围为 {max(1, target_leaf_count - 2)} 至 "
            f"{target_leaf_count + 2} 个。"
            "只统计 content_mode=ai-generate 的最终叶子节点；"
            "修复后仍须保持在此范围内。"
        )
    if allow_root_changes:
        root_requirement = (
            f"一级目录只能保持用户已批准的 {SCORE_DIRECTORY_PLAN_FILE} 规划，"
            "不得提出规划之外的新调整。"
        )
    else:
        root_requirement = "一级目录已经由用户确认，数量、顺序、标题、描述和属性不得修改。"
    return (
        "请对当前完整技术方案目录执行最终审核，并在用户确认后完成必要修复。\n"
        f"{create_output_file_requirements('outline_review')}\n"
        "\n"
        f"以下已提供 {OUTLINE_REVIEW_CONTEXT_FILE}、{OUTLINE_OUTPUT_FILE}、"
        f"技术评分信息.md 和 {SCORE_DIRECTORY_PLAN_FILE} 在本阶段开始时的最新完整内容。"
        "后续修改文件后，以修改后的内容为准。"
        f"{OUTLINE_REVIEW_CONTEXT_FILE} 是宿主程序计算的确定性审核结果，"
        "叶子数量、内容模式数量、最大层级、父节点数量、单子节点和评分节点机械映射"
        "均直接采用其中结果；你只负责评分语义覆盖、近义重复和专业合理性审核。"
        "优先使用本次已提供的最新材料和程序计算结果，通常无需重复读取或重新统计。"
        "需要确认具体节点、补足上下文或排查结果异常时，可使用 grep、read 或 bash "
        "定位相关部分；已有结果足以判断时直接继续审核。\n"
        "\n"
        f"审核维度：{leaf_count_review}\n"
        "- 评分覆盖：直接以技术评分信息.md 为原始依据，逐项检查其中适合技术方案响应的"
        "评分大项是否被目录准确覆盖；结构化评分项和目录规划用于核对已确认的映射，"
        "但不能掩盖原始评分信息中的遗漏。\n"
        "- 重复目录：检查全部子目录中是否存在重复、近义、含义重叠或仅换一种说法的节点；"
        "不同专业分支下确有独立含义的同名标题不应机械判重。\n"
        "- 专业合理性：评估目录层级、颗粒度、逻辑顺序、标题表达、节点归属以及内容处理"
        "模式是否适合正式技术投标文件。\n"
        "\n"
        "审核与修复流程：\n"
        "1. 必须先完整审核并形成问题清单，不得边审核边修改。\n"
        f"2. 如果没有问题，不要修改 {OUTLINE_OUTPUT_FILE}；写入 {OUTLINE_REVIEW_FILE}，"
        'status=passed、issues=[]、user_feedback=""，summary 说明通过原因。\n'
        "3. 如果发现问题，先为每个问题记录 category、problem、推荐 repair 和 "
        "confirmation_required，不得提前修改目录。\n"
        "4. 只有以下问题可设 confirmation_required=false：标题或说明的专业化优化；"
        "不涉及评分项映射节点、目标层级和一级目录的明显重复或近义子目录合并。"
        "评分覆盖缺失、叶子数量超出合理范围、评分项目标层级调整、一级目录调整、"
        "增加或拆分目录、跨分支移动以及明显结构重排均必须设为 true。\n"
        "5. 如果全部问题都不需要确认，可以直接执行文案优化或轻微去重，不得调用 ask-user；"
        "完成后设置 status=simple_fix、user_feedback=\"\"。静默修复不得改变评分项映射、"
        "评分项目标层级和一级目录，且不得使 AI 生成叶子数量超出程序给出的合理范围。\n"
        "6. 只要存在一个 confirmation_required=true 的问题，本轮所有问题都不得提前修改。"
        "集中调用一次 ask-user，question 使用多行文本完整列出问题及推荐修复方案；"
        "提供 2 至 5 个互斥选项，第一项是推荐修复方案，并提供保留当前目录的选项；"
        "另提供一个名为“调整修复方案”等明确业务名称的选项并设置 custom=true，"
        "让用户说明具体修改要求，其他选项均设置 custom=false。custom=true 的选项最多只能有一个。\n"
        "7. 根据 ask-user 返回的 answer 执行最终处理：用户要求全部或部分修改时更新 "
        f"{OUTLINE_OUTPUT_FILE} 并设置 status=user_feedback；用户明确拒绝修改或要求"
        "保留现状时不得修改目录并设置 status=user_refuse。将 answer 原文完整写入 "
        "user_feedback，修改完成后不得再次询问用户。\n"
        f"8. {root_requirement}\n"
        f"9. 修复必须继续遵守 {SCORE_DIRECTORY_PLAN_FILE} 中用户确认的评分项映射、"
        "目标层级和一级目录调整边界。补回遗漏映射、合并重复目录或优化层级时，"
        "不得引入未经用户批准的评分大项规划变更。\n"
        f"10. 技术一级目录的 id 必须与 {SCORE_DIRECTORY_PLAN_FILE} 中对应的 root_id 一致；"
        "调整一级目录顺序或编号时不得修改节点 id。"
        f"结构事实以 {OUTLINE_REVIEW_CONTEXT_FILE} 为准；如果其中确定性检查不通过，"
        "直接依据列出的节点和缺失项形成问题并修复，不要重新统计。任何语义修复仍必须"
        "保证叶子保留合法 content_mode、父节点不包含 content_mode 或 content_mode_note、"
        "父节点至少有两个 children 且目录最多六级。\n"
        f"11. 最终将完整问题清单和处理结果写入 {OUTLINE_REVIEW_FILE}。"
        "无问题时完整格式为 "
        '{"status":"passed","issues":[],"user_feedback":"","summary":"审核通过原因"}'
        "；有问题时完整格式为 "
        '{"status":"user_feedback","issues":[{"category":"score-coverage",'
        '"problem":"问题说明","repair":"修复方案","confirmation_required":true}],'
        '"user_feedback":"用户回答原文","summary":"处理结果"}'
        "。category 只能是 leaf-count、score-coverage、duplicate-directory、"
        "professional-structure；status 按本流程选择 passed、simple_fix、"
        "user_feedback 或 user_refuse。\n"
        f"12. 程序已为 {OUTLINE_OUTPUT_FILE} 和 {OUTLINE_REVIEW_FILE} 预置 Schema，"
        "可用 json-validation 自查，只传 file_path；本阶段结束后程序统一校验两份文件，"
        f"不通过会退回问题清单继续修复。未修改 {OUTLINE_OUTPUT_FILE} 时"
        "不要为了校验而重写目录。\n"
        "\n"
        "本阶段输入材料：\n"
        + "\n\n".join(
            f"【文件开始：{item['path']}】\n{item['content']}\n【文件结束：{item['path']}】"
            for item in input_files
        )
    )


__all__ = [
    "ORIGINAL_ONLY_DIRECTORY_RULE",
    "create_output_file_requirements",
    "create_output_repair_prompt",
    "create_initial_prompt",
    "create_no_technical_score_children_prompt",
    "create_leaf_allocation_prompt",
    "create_score_planning_prompt",
    "create_children_prompt",
    "create_leaf_adjustment_prompt",
    "create_no_technical_score_review_prompt",
    "create_outline_review_prompt",
]
