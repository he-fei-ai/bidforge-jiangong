"""Agent 工具循环子系统（移植自易标 Yibiao 2.30.20 的 pi 运行时层）。

分层对应可移植报告《易标目录生成工具修正_可移植报告_20261024.md》第五章：

- P0 ``agent_loop``  —— ``messages → LLM → tool_calls → 执行 → 回灌 → 循环``
- P1 ``tools``       —— read/write/edit/grep/find/ls/json-validation/ask-user/report-failure
- P2 ``submission``  —— 阶段必需文件表 + blocking issues + 三档提交止损
- P3 ``outline_*``   —— 阶段提示词 + continueTask 状态机 + 目录节点 UUID 身份
- P4 ``workspace``   —— 任务工作区 + 输入基线还原 + 持久 task_key

红线（与外部引用软件易标冲突时一律以易标为准）：
1. 目录身份 ``id`` 与编号 ``number`` 分离，编号只反映树位置，绝不改身份；
2. 提示词产物约束必须保留「只在响应中输出 JSON 不算写入」；
3. 提交修复必须有 ``stop`` 兜底，禁止无限修复循环。
"""

__all__ = [
    # P0 —— 工具循环契约（agent_loop）
    "AgentTool",
    "ToolCallOutcome",
    "AgentLoopResult",
    "run_agent_loop",
    # P1 —— 工作区路径防护（移植自 piJsonValidationTool.cjs 顶部函数）
    "normalize_workspace_file_path",
    "resolve_workspace_file",
    # P1 —— 原生文件工具（移植自 pi read/write/edit/grep/find/ls）
    "build_read_tool",
    "build_write_tool",
    "build_edit_tool",
    "build_grep_tool",
    "build_find_tool",
    "build_ls_tool",
    # P1 —— 易标专用工具（移植自 piJsonValidation/UserQuestion/TaskFailure 工具）
    "build_json_validation_tool",
    "build_ask_user_tool",
    "build_report_failure_tool",
    # P2 —— 提交策略（移植自 piSubmissionPolicy.cjs）
    "create_submission_policy",
    "build_submission_repair_prompt",
    # P2 —— 阶段产物校验（移植自 outlineGenerationTaskV2.cjs L10–L24 / L300–460 / L909）
    "OUTLINE_OUTPUT_FILE",
    "TECHNICAL_SCORE_GROUPS_FILE",
    "SCORE_DIRECTORY_PLAN_FILE",
    "LEAF_ALLOCATION_FILE",
    "LEAF_ALLOCATION_CONTEXT_FILE",
    "OUTLINE_REVIEW_FILE",
    "OUTLINE_REVIEW_CONTEXT_FILE",
    "STAGE_OUTPUT_FILES",
    "StageMeta",
    "validate_json_output",
    "validate_outline_output",
    "validate_score_planning_roots",
    "validate_stage_outputs",
    # P2 —— 目录节点身份（移植自 technicalPlanOutline.cjs）
    "NODE_ID_PATTERN",
    "number_outline",
    "collect_outline_ids",
    "accept_agent_outline",
    # P4 —— 任务工作区受保护文件基线（移植自 piWorkspaceBaseline.cjs）
    "create_workspace_baseline",
    # P4 —— 持久 task_key 的 create/resume（映射易标 persistent_task，复用 task_registry）
    "AGENT_CHECKPOINT_KIND",
    "create_agent_task",
    "checkpoint_agent_task",
    "load_agent_checkpoint",
    "resume_agent_task",
    # P3 —— 阶段提示词构造器（移植自 outlineGenerationTaskV2.cjs L599–858）
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
    # P3 —— 阶段执行编排（continueTask 状态机，移植自 piRuntimeService.cjs L1073–1240）
    "run_stage",
    "StageExecutionResult",
    "StageStoppedError",
    "STOP_COMPLETED",
    "STOP_BLOCKING_PREFIX",
    "RESTORED_NOTE_TEMPLATE",
]

#: 名字 → 所属模块（惰性解析用，避免 import 本包时拉起全部工具实现）。
_LAZY_MODULES: dict[str, str] = {
    "AgentTool": "agent_loop",
    "ToolCallOutcome": "agent_loop",
    "AgentLoopResult": "agent_loop",
    "run_agent_loop": "agent_loop",
    "normalize_workspace_file_path": "workspace_tools",
    "resolve_workspace_file": "workspace_tools",
    "build_read_tool": "workspace_tools",
    "build_write_tool": "workspace_tools",
    "build_edit_tool": "workspace_tools",
    "build_grep_tool": "workspace_tools",
    "build_find_tool": "workspace_tools",
    "build_ls_tool": "workspace_tools",
    "build_json_validation_tool": "json_validation",
    "build_ask_user_tool": "user_question",
    "build_report_failure_tool": "report_failure",
    "create_submission_policy": "submission_policy",
    "build_submission_repair_prompt": "submission_policy",
    "OUTLINE_OUTPUT_FILE": "validation",
    "TECHNICAL_SCORE_GROUPS_FILE": "validation",
    "SCORE_DIRECTORY_PLAN_FILE": "validation",
    "LEAF_ALLOCATION_FILE": "validation",
    "LEAF_ALLOCATION_CONTEXT_FILE": "validation",
    "OUTLINE_REVIEW_FILE": "validation",
    "OUTLINE_REVIEW_CONTEXT_FILE": "validation",
    "STAGE_OUTPUT_FILES": "validation",
    "StageMeta": "validation",
    "validate_json_output": "validation",
    "validate_outline_output": "validation",
    "validate_score_planning_roots": "validation",
    "validate_stage_outputs": "validation",
    "NODE_ID_PATTERN": "outline_identity",
    "number_outline": "outline_identity",
    "collect_outline_ids": "outline_identity",
    "accept_agent_outline": "outline_identity",
    "create_workspace_baseline": "workspace_baseline",
    "AGENT_CHECKPOINT_KIND": "task_state",
    "create_agent_task": "task_state",
    "checkpoint_agent_task": "task_state",
    "load_agent_checkpoint": "task_state",
    "resume_agent_task": "task_state",
    "ORIGINAL_ONLY_DIRECTORY_RULE": "stage_prompts",
    "create_output_file_requirements": "stage_prompts",
    "create_output_repair_prompt": "stage_prompts",
    "create_initial_prompt": "stage_prompts",
    "create_no_technical_score_children_prompt": "stage_prompts",
    "create_leaf_allocation_prompt": "stage_prompts",
    "create_score_planning_prompt": "stage_prompts",
    "create_children_prompt": "stage_prompts",
    "create_leaf_adjustment_prompt": "stage_prompts",
    "create_no_technical_score_review_prompt": "stage_prompts",
    "create_outline_review_prompt": "stage_prompts",
    "run_stage": "stage_runner",
    "StageExecutionResult": "stage_runner",
    "StageStoppedError": "stage_runner",
    "STOP_COMPLETED": "stage_runner",
    "STOP_BLOCKING_PREFIX": "stage_runner",
    "RESTORED_NOTE_TEMPLATE": "stage_runner",
}


def __getattr__(name: str):
    # 惰性导出：避免 services.agent 被 import 时把工作区/工具实现全部拉起
    # （部分模块依赖文件系统与 ai.provider_factory，属重导入）。
    module_name = _LAZY_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module = import_module(f"{__name__}.{module_name}")
    return getattr(module, name)
