"""接线层 —— 以 ``stage_runner`` 为骨架把 Agent 目录生成接入本仓 SSE 主流程。

任务出处：《目录生成工具修正移植_实施报告_P0P4_20261025.md》第五章遗留①②⑤
与第六章建议 1–4（接线阶段）。

架构与注入点：

- 入口缝位于 ``sse_handlers.generate_outline`` 的 event_stream 内、截断
  warning 之后与 word_budget 分档判定之前：``settings.outline_agent_enabled``
  （默认 False，死代码，legacy 链路逐字不变）为真时转入
  :func:`outline_agent_event_stream`，逐事件 ``yield`` 后 ``return``；
- 目录成果落库不在本模块：沿用本仓既有链路 —— 前端收到
  ``completed`` / ``stopped`` / ``error(带 outline)`` 事件后调用
  ``/sections/save-outline``（``source="ai"``），本模块只负责产出与
  legacy 同形状的事件与 checkpoint；
- 终态顺序与 legacy 同契约：**checkpoint → finish_task → yield**；
  checkpoint 失败降级 ``degraded`` + ``checkpoint_failed``（对齐 R89 S2）。

单链范围（第六章建议 2 打通；2026-10-27 补齐 noTech 全链审核阶段）：

- 阶段 1 ``initial-outline``：``create_initial_prompt`` 生成一级目录，
  accept 后按易标 ``applyConfirmedSelection`` 语义自动确认（``numberOutline``
  已由 ``validate_outline_output`` → ``accept_agent_outline`` 完成，选择集
  取全量一级节点，见文末有意偏离 1）；
- 阶段 2 ``children_generation``：写回已确认一级目录到 ``outline.json`` 后
  转 ``create_no_technical_score_children_prompt``；
- 阶段 3 ``outline_review``（易标 L1099–1129 / L1394–1406 / L1298–1332）：
  children accept 后 ``target_leaf_count=None`` 恒走
  ``continueWithOutlineReview`` 等价流转 —— 写入
  ``outline-review-context.json``（宿主确定性审核结果）与最新
  ``outline.json``，转 ``create_no_technical_score_review_prompt``；
  accept 后按 ``outline-review.json`` 状态映射审核结论文案，并经
  ``outcome.review`` / completed 事件 ``review`` 键回传审核结论；
- score-planning / leaf_allocation / leaf_adjustment 三阶段属技术评分模式，
  已接线（2026-10-26 遗留 1）：``no_technical_score_mode=False`` 走六阶段
  ``initial-outline → score-planning → leaf_allocation → children_generation →
  leaf_adjustment → outline_review``。评分项来源为 ``requirements``（编制要求/
  评审要点，宿主预置 ``技术评分信息.md`` 与 ``technical-score-groups.json``，
  模型在 score-planning 完成目录映射）；``NO_TECHNICAL_SCORE_MODE`` 模块常量
  恒 ``True``，作为 ``run_outline_agent`` 的默认形参 —— 默认走无技术评分单链
  （既有调用方逐字不变），技术评分单链仅在显式传 ``no_technical_score_mode=False``
  时启用。

有意偏离（与外部引用软件易标冲突时以易标为准，以下为本仓环境约束）：

1. 本仓前端无 outline-selection 人工勾选 UI，单链采用易标
   ``defaultSelectedIds`` 规则的等价物 —— 一级目录全量自动确认；
   人工选择通道待前端具备勾选能力后接入；
2. ``ask-user`` 工具在未注入 ``request_user_question`` 通道时**不构建**
   （本仓暂无问答 SSE 应答端点），模型调用将得到 agent_loop 的
   fail-soft「未知工具」回灌并自主继续；``report-failure`` 走简单形态
   （缺省通道记日志，工具本身保留 ``stop`` 收束语义）；
3. ``word_budget`` → ``targetLeafCount`` 映射未接入（易标由
   ``word_control_options`` 推导，本仓无该配置），children 阶段传
   ``target_leaf_count=None``（对应「未设置总字数目标」分支）。

提示词消歧（遗留⑤）：构造器逐字保真不动，本模块在提示词出口统一把
``"grep、read 或 bash"`` 替换为 ``"grep、read 或 find"``（P1 无 bash 实现）。

失败边界（P2/P0 红线）：

- ``OutlineAgentStopped``：用户停止（prompt_once 前置检查命中），
  在事件流内收尾为 ``stopped``（checkpoint 带 partial）；
- ``StageStoppedError``：三档提交止损兜底，**原样上抛**由 SSE 外层
  既有 error 分支收尾（partial 成果经 ``hold_partial`` 已在
  ``_partial_holder``）；
- ``OutlineAgentReportedFailure``：report-failure 简单形态收束，上抛走
  error 分支；
- 执行异常经 ``create_output_repair_prompt``（execution 档返回 None）→
  立即上抛，不吞执行错误。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable

from app.config import DATA_DIR

# ✅ R162（2026-11-01）：层级硬性标准完成前终检（单一事实源 outline_rules）。
from app.services.outline_rules import (
    format_violations_message,
    validate_outline_nodes,
)

from .agent_loop import (
    STOP_TIMEOUT,
    STOP_TOOL_STOP,
    STOP_TURN_LIMIT,
    run_agent_loop,
)
from .outline_identity import collect_outline_ids
from .outline_schemas import build_json_validation_schemas, build_output_validators
from .stage_prompts import (
    create_children_prompt,
    create_initial_prompt,
    create_leaf_adjustment_prompt,
    create_leaf_allocation_prompt,
    create_no_technical_score_children_prompt,
    create_no_technical_score_review_prompt,
    create_outline_review_prompt,
    create_output_repair_prompt,
    create_score_planning_prompt,
)
from .stage_runner import StageExecutionResult, StageStoppedError, run_stage
from .validation import (
    AI_CONTENT_MODE,
    CONTENT_MODES,
    LEAF_ALLOCATION_CONTEXT_FILE,
    LEAF_ALLOCATION_FILE,
    OUTLINE_OUTPUT_FILE,
    OUTLINE_REVIEW_CONTEXT_FILE,
    OUTLINE_REVIEW_FILE,
    SCORE_DIRECTORY_PLAN_FILE,
    STAGE_OUTPUT_FILES,
    TECHNICAL_SCORE_GROUPS_FILE,
    StageMeta,
    validate_stage_outputs,
)
from .workspace_baseline import cleanup_workspace_and_baseline, create_workspace_baseline
from .workspace_tools import (
    build_edit_tool,
    build_find_tool,
    build_grep_tool,
    build_ls_tool,
    build_read_tool,
    build_write_tool,
    resolve_workspace_file,
)

logger = logging.getLogger("outline_agent_flow")

#: 易标 persistent task_key（outlineGenerationAgentV2Config.cjs 逐字）。
OUTLINE_AGENT_TASK_KEY = "technical-plan-outline-generation"

#: 本仓接线单链的模式常量：无技术评分两件套、非单独装订技术文件
#: （对应易标 initialFiles/taskInstruction 三分支的 noTechnicalScoreMode）。
NO_TECHNICAL_SCORE_MODE = True
STANDALONE_TECHNICAL = False

#: 六阶段骨架（单一事实源，2026-10-26 最小接线）：易标
#: outlineGenerationTaskV2.cjs 六阶段 workflow 顺序，键名与
#: ``validation.STAGE_OUTPUT_FILES`` 逐字一致（前两个连字符、后四个下划线）。
#: 本仓当前只接线「无技术评分」单链（initial-outline → children_generation →
#: outline_review）；技术评分三阶段（score-planning / leaf_allocation /
#: leaf_adjustment）已登记为骨架但**未接线**（本仓无技术评分数据源
#: score_mapping / scoreDirectoryPlan，见 build_outline_review_context 有意偏离）。
#: 护栏测试锁 ``set(_OUTLINE_FLOW_STAGES) == set(STAGE_OUTPUT_FILES)``。
_OUTLINE_FLOW_STAGES: tuple[str, ...] = (
    "initial-outline",
    "score-planning",
    "leaf_allocation",
    "children_generation",
    "leaf_adjustment",
    "outline_review",
)

#: 遗留⑤ 消歧目标子串（stage_prompts L597/L673 逐字）与替换词。
_BASH_PROMPT_TOKEN = "grep、read 或 bash"
_FIND_PROMPT_TOKEN = "grep、read 或 find"

#: 易标 noTech 分支 seed 文件名（outlineGenerationTaskV2.cjs L935–939）。
_REQUIREMENTS_FILE = "响应文件要求.md"
_OVERVIEW_FILE = "项目概述.md"
_ORIGINAL_PLAN_FILE = "原方案.md"

_DEFAULT_MAX_TURNS = 40

#: ✅ R154（R149 遗留 L-6）：``run_agent_loop`` 的 ``total_timeout`` 墙钟预算。
#:    旧实现只传 ``max_turns``（40 轮 × 单轮最长 180s 模型调用 ≈ 2h 无墙钟上限，
#:    病态/卡死模型会挂死整个任务）。1800s 与正文 Agent 的
#:    ``content_agent_repair_timeout=900`` 同量级、放宽一倍（目录阶段单循环
#:    工作量更大）；超时走 ``STOP_TIMEOUT`` → 上层 RuntimeError → 任务 failed，
#:    不静默当成功（agent_loop 契约）。
_LOOP_WALL_CLOCK_BUDGET = 1800.0


class OutlineAgentStopped(RuntimeError):
    """用户停止（task_registry stop 命中）——事件流内收尾为 stopped。"""


class OutlineAgentReportedFailure(RuntimeError):
    """report-failure 简单形态收束（Agent 自报无法继续）。"""


@dataclass
class OutlineAgentOutcome:
    """一次单链目录生成的产物与统计。"""

    outline: list = field(default_factory=list)
    review: dict = field(default_factory=dict)
    result: StageExecutionResult | None = None
    stage: str = ""


def disambiguate_prompt_tools(prompt: str) -> str:
    """遗留⑤：提示词出口统一 bash 消歧（构造器逐字保真不动）。"""
    return str(prompt or "").replace(_BASH_PROMPT_TOKEN, _FIND_PROMPT_TOKEN)


def count_ai_leaves(items) -> int:
    """统计 ``content_mode == 'ai-generate'`` 的叶子数（易标 ``countAiLeaves`` L459-465）。"""
    total = 0
    for item in (items or []):
        if not isinstance(item, dict):
            continue
        children = item.get("children")
        if isinstance(children, list) and children:
            total += count_ai_leaves(children)
        elif item.get("content_mode") == AI_CONTENT_MODE:
            total += 1
    return total


def count_leaves_by_mode(items, counts: dict | None = None) -> dict:
    """按内容模式统计叶子数（易标 ``countLeavesByMode`` L467-476）。"""
    if counts is None:
        counts = {mode: 0 for mode in CONTENT_MODES}
    for item in (items or []):
        if not isinstance(item, dict):
            continue
        children = item.get("children")
        if isinstance(children, list) and children:
            count_leaves_by_mode(children, counts)
        elif item.get("content_mode") in counts:
            counts[item.get("content_mode")] += 1
    return counts


def collect_outline_structure(items) -> dict:
    """汇总层级 / 父节点 / 单子节点 / 非法叶子内容模式（易标 L478-503 逐结构移植）。"""
    result = {"max_depth": 0, "parent_count": 0,
              "single_child_nodes": [], "invalid_leaf_content_modes": []}

    def visit(nodes, depth: int) -> None:
        for item in (nodes or []):
            if not isinstance(item, dict):
                continue
            result["max_depth"] = max(result["max_depth"], depth)
            children = item.get("children")
            children = children if isinstance(children, list) else []
            if children:
                result["parent_count"] += 1
                if len(children) < 2:
                    result["single_child_nodes"].append({
                        "id": item.get("id"), "title": item.get("title"),
                        "child_count": len(children)})
                visit(children, depth + 1)
            elif item.get("content_mode") not in CONTENT_MODES:
                result["invalid_leaf_content_modes"].append({
                    "id": item.get("id"), "title": item.get("title"),
                    "content_mode": item.get("content_mode") or ""})

    visit(items, 1)
    return result


def build_outline_review_context(
    *, outline, target_leaf_count, score_mapping=None, scoreDirectoryPlan=None
) -> dict:
    """宿主程序确定性审核结果（易标 ``buildOutlineReviewContext`` L562-588）。

    技术评分单链（2026-10-26 遗留 1 接线）：``score_mapping`` / 
    ``scoreDirectoryPlan`` 由调用方（leaf_adjustment accept）从评分目录规划
    聚合后注入；无技术评分单链缺省 ``None``，返回结构逐字不变（既有调用方
    零影响）。
    """
    items = (outline or {}).get("outline") or []
    leaf_counts = count_leaves_by_mode(items)
    structure = collect_outline_structure(items)
    if target_leaf_count is None:
        acceptable_min = None
        acceptable_max = None
        within_range = True
    else:
        acceptable_min = max(1, target_leaf_count - 2)
        acceptable_max = target_leaf_count + 2
        current = leaf_counts.get(AI_CONTENT_MODE, 0)
        within_range = acceptable_min <= current <= acceptable_max
    context = {
        "leaf_count": {
            "target": target_leaf_count,
            "current_ai_generate": leaf_counts.get(AI_CONTENT_MODE, 0),
            "acceptable_min": acceptable_min,
            "acceptable_max": acceptable_max,
            "within_acceptable_range": within_range,
            "by_content_mode": leaf_counts,
        },
        "structure": {
            **structure,
            "valid": (structure["max_depth"] <= 6
                      and len(structure["single_child_nodes"]) == 0
                      and len(structure["invalid_leaf_content_modes"]) == 0),
        },
    }
    if scoreDirectoryPlan is not None:
        context["scoreDirectoryPlan"] = scoreDirectoryPlan
    if score_mapping is not None:
        context["score_mapping"] = score_mapping
    return context


def review_status_message(status) -> str:
    """审核状态 → 结论文案（易标 L1317-1323 逐字；未登记状态走兜底分支）。"""
    if status == "passed":
        return "目录审核通过"
    if status == "simple_fix":
        return "目录审核完成，Agent 已自动微调简单问题"
    if status == "user_feedback":
        return "目录审核完成，已按用户反馈修复"
    return "目录审核完成，用户选择保留当前目录"


def build_task_instruction(*, has_original_plan: bool,
                           has_requirements: bool = True) -> str:
    """initial-outline 任务指示（易标 L951–955，noTech 非 standalone 二分支逐字）。

    ✅ R149（2026-10-31 · FIX-F）：新增 ``has_requirements``（默认 True ⇒
    既有调用方/测试逐字不变）。``seed_files_from_context`` 只在 requirements
    非空时才落盘 ``响应文件要求.md`` —— 为空时该文件不存在，而旧指示词仍
    宣称它是「目录结构和标题来源的唯一依据」，模型 read 必然 is_error，
    只能凭 概述/经验 生成（与「唯一依据」声明矛盾）或反复重试烧 turn 预算。
    requirements 为空时改为与工作区实际输入一致的指示。
    """
    if has_requirements:
        if has_original_plan:
            return (
                "严格按照响应文件要求.md 组织一级目录，它是目录结构和标题来源的唯一依据。"
                "项目概述.md 仅用于理解背景和术语，不得据此新增一级目录；"
                "原方案.md 仅用于参考标题表达。"
            )
        return (
            "严格按照响应文件要求.md 组织一级目录，它是目录结构和标题来源的唯一依据。"
            "项目概述.md 仅用于理解背景和术语，不得据此新增一级目录。"
        )
    # requirements 为空：响应文件要求.md 未 seed，指示词不得再指向它。
    if has_original_plan:
        return (
            "本次未提供响应文件要求.md，请以项目概述.md 与专业经验组织一级目录；"
            "原方案.md 仅用于参考标题表达。"
        )
    return "本次未提供响应文件要求.md，请以项目概述.md 与专业经验组织一级目录。"


def seed_files_from_context(context: dict) -> list[dict]:
    """把 SSE 装配材料写成易标 initialFiles 形态（noTech 分支，L935–939）。

    - ``响应文件要求.md`` ← ``requirements_text``
    - ``项目概述.md`` ← ``project_brief``
    - ``原方案.md`` ← ``reference_outline``（有则写）
    """
    files: list[dict] = []
    requirements = str(context.get("requirements_text") or "").strip()
    overview = str(context.get("project_brief") or "").strip()
    original = str(context.get("reference_outline") or "").strip()
    if requirements:
        files.append({"path": _REQUIREMENTS_FILE, "content": requirements})
    if overview:
        files.append({"path": _OVERVIEW_FILE, "content": overview})
    if original:
        files.append({"path": _ORIGINAL_PLAN_FILE, "content": original})
    return files


def _build_technical_score_groups(requirements_text: str) -> list[dict]:
    """宿主从 requirements（编制要求/评审要点）构造技术评分项分组。

    每条非空行 = 一个评分大项分组（``R{n}`` 顺序编号）。对齐
    ``TECHNICAL_SCORE_GROUPS_SCHEMA``（requirement_id 须 ``^R[1-9]\\d*$``、
    title/description/detail_points 均非空）。本仓无易标 技术评分信息.md，
    requirements 是评分大项来源（对齐 legacy L4215），故宿主确定性预置，
    模型在 score-planning 阶段读取确认并完成目录映射。
    """
    groups: list[dict] = []
    for line in (requirements_text or "").splitlines():
        text = str(line).strip()
        if not text:
            continue
        groups.append({
            "requirement_id": f"R{len(groups) + 1}",
            "title": text,
            "description": text,
            "detail_points": [text],
        })
    return groups


def _build_leaf_allocation_context(plan: dict) -> dict:
    """宿主构建 ``leaf-allocation-context.json``（易标 createLeafAllocationContext）。

    技术评分单链：模式暂为 ``agent-decides``（偏离 3 word_budget→targetLeafCount
    仍登记），让模型自主分配叶子；``technical_branches`` 来自评分目录规划的
    branch（root_id/root_title）。目标计数保留 None/0 占位，与 agent-decides 模式
    的 leaf-allocation.json Schema（``target_ai_leaf_count`` 可为 null）一致。
    """
    branches = (plan or {}).get("branches") or []
    return {
        "mode": "agent-decides",
        "target_ai_leaf_count": None,
        "fixed_ai_leaf_count": 0,
        "allocatable_ai_leaf_count": None,
        "technical_branches": [
            {"root_id": b.get("root_id"), "root_title": b.get("root_title")}
            for b in branches
        ],
    }


def _collect_score_mapping(plan: dict) -> dict:
    """从评分目录规划聚合评分项映射（collectScoreMappingCoverage 本仓子集）。

    key = 一级分支 root_id，value = 该分支下评分项对应的 ``target_title`` 列表；
    仅用于技术审核上下文（宿主确定性计算，供模型核对覆盖）。
    """
    mapping: dict = {}
    for b in (plan or {}).get("branches") or []:
        rid = b.get("root_id")
        if rid is None:
            continue
        mapping[rid] = [
            m.get("target_title") for m in (b.get("mappings") or [])
            if m.get("target_title")
        ]
    return mapping


def workspace_root_for(task_id: str) -> Path:
    """Agent 任务工作区根（DATA_DIR/agent_workspaces/<task_id>）。"""
    return DATA_DIR / "agent_workspaces" / str(task_id)


def build_outline_agent_tools(
    workspace_dir: str,
    *,
    validation_schemas: dict,
    request_user_question: Callable | None = None,
    report_task_failure: Callable | None = None,
) -> list:
    """P1 工具表（易标 pi 运行时工具集的本仓子集）。

    - 基础六件：read/write/edit/grep/find/ls；
    - ``json-validation``：预置 ``validation_schemas``（outline.json 等）；
    - ``ask-user``：仅在注入 ``request_user_question`` 通道时构建
      （有意偏离 2：本仓暂无应答端点，缺省不下发该工具）；
    - ``report-failure``：简单形态；缺省通道记日志（工具 ``stop`` 收束语义不变）。
    """
    from .json_validation import build_json_validation_tool
    from .report_failure import build_report_failure_tool
    from .user_question import build_ask_user_tool

    tools = [
        build_read_tool(workspace_dir),
        build_write_tool(workspace_dir),
        build_edit_tool(workspace_dir),
        build_grep_tool(workspace_dir),
        build_find_tool(workspace_dir),
        build_ls_tool(workspace_dir),
        build_json_validation_tool(workspace_dir, validation_schemas),
    ]
    if request_user_question is not None:
        tools.append(build_ask_user_tool(request_user_question))

    async def _default_report_failure(reason: str) -> None:
        logger.warning("目录生成 Agent 报告无法继续：%s", reason)

    tools.append(build_report_failure_tool(
        report_task_failure or _default_report_failure))
    return tools


async def _default_chat_fn(messages, *, tools, tool_choice=None, **kwargs):
    """默认 LLM 通道：复用本仓 ``provider_factory.chat_with_tools``。"""
    from app.services.ai.provider_factory import chat_with_tools

    return await chat_with_tools(
        messages, tools=tools, tool_choice=tool_choice,
        scene="outline_agent", **kwargs)


async def run_outline_agent(
    *,
    task_id: str,
    scheme: dict,
    context: dict,
    workspace_dir: str | Path | None = None,
    seed_files: list[dict] | None = None,
    word_budget: int = 30000,
    initial_stage: str = "initial-outline",
    initial_stage_index: int = 0,
    messages: list | None = None,
    hold_partial: Callable | None = None,
    emit_activity: Callable[[dict], None] | None = None,
    emit_event: Callable[[dict], None] | None = None,
    report_progress: Callable | None = None,
    request_user_question: Callable | None = None,
    report_task_failure: Callable | None = None,
    chat_fn: Callable | None = None,
    no_technical_score_mode: bool = NO_TECHNICAL_SCORE_MODE,
    stage_ref: dict | None = None,
) -> OutlineAgentOutcome:
    """执行 Agent 目录生成单链（业务层，无 SSE 依赖）。

    默认（``no_technical_score_mode=True``）走无技术评分单链
    ``initial-outline → children_generation → outline_review``；
    ``False`` 走技术评分单链 ``initial-outline → score-planning →
    leaf_allocation → children_generation → leaf_adjustment → outline_review``
    （评分项来源：``requirements`` 编制要求/评审要点，宿主预置
    ``技术评分信息.md`` 与 ``technical-score-groups.json``，模型完成映射）。

    Args:
        task_id: 已注册的 task_registry 任务 id（停止/暂停控制锚点）。
        scheme: 方案行（``name`` / ``type``）。
        context: ``_assemble_context`` 装配材料（requirements_text /
            project_brief / reference_outline 等）。
        workspace_dir: 工作区目录；缺省 ``DATA_DIR/agent_workspaces/<task_id>``。
        seed_files: 覆盖式输入材料；``None`` 按 ``context`` 自动 seed。
        messages: resume 时注入的既有会话；缺省全新 messages。
        hold_partial: ``async (tree) -> None``，阶段 accept 后回传目录树
            （SSE 侧写入 ``_partial_holder``，供停止/异常兜底带出成果）。
        report_progress: ``async (progress, message) -> None`` 阶段进度上报。
        no_technical_score_mode: 默认 ``True``（无技术评分单链，既有调用方
            逐字不变）；``False`` 走六阶段技术评分单链（见 docstring）。
        stage_ref: ✅ R155-O（L-1）宿主共享字典，实时回传当前工作流阶段
            （透传给实现层，见其 docstring）；不传零影响。

    ✅ R154（R149 遗留 L-4）：薄包装——委托 ``_run_outline_agent_impl`` 并在
    终态后回收工作区与基线目录。默认路径（未传 workspace_dir）工作区归本
    函数所有，连同 ``<task_id>_baseline`` 一并回收；调用方自备目录时只回收
    基线。任何收束路径（completed/stopped/failed/异常）都清理（fail-soft）。
    """
    _ws = Path(workspace_dir) if workspace_dir else workspace_root_for(task_id)
    try:
        return await _run_outline_agent_impl(
            task_id=task_id, scheme=scheme, context=context,
            workspace_dir=workspace_dir, seed_files=seed_files,
            word_budget=word_budget, initial_stage=initial_stage,
            initial_stage_index=initial_stage_index, messages=messages,
            hold_partial=hold_partial, emit_activity=emit_activity,
            emit_event=emit_event, report_progress=report_progress,
            request_user_question=request_user_question,
            report_task_failure=report_task_failure, chat_fn=chat_fn,
            no_technical_score_mode=no_technical_score_mode,
            stage_ref=stage_ref)
    finally:
        if workspace_dir is None:
            cleanup_workspace_and_baseline(_ws, _ws.parent / f"{task_id}_baseline")
        else:
            cleanup_workspace_and_baseline(None, _ws.parent / f"{task_id}_baseline")


async def _run_outline_agent_impl(
    *,
    task_id: str,
    scheme: dict,
    context: dict,
    workspace_dir: str | Path | None = None,
    seed_files: list[dict] | None = None,
    word_budget: int = 30000,
    initial_stage: str = "initial-outline",
    initial_stage_index: int = 0,
    messages: list | None = None,
    hold_partial: Callable | None = None,
    emit_activity: Callable[[dict], None] | None = None,
    emit_event: Callable[[dict], None] | None = None,
    report_progress: Callable | None = None,
    request_user_question: Callable | None = None,
    report_task_failure: Callable | None = None,
    chat_fn: Callable | None = None,
    no_technical_score_mode: bool = NO_TECHNICAL_SCORE_MODE,
    stage_ref: dict | None = None,
) -> OutlineAgentOutcome:
    """``run_outline_agent`` 的实际实现（清理由包装层负责）。

    ✅ R155-O（2026-11-01 · R149 遗留 L-1）：``stage_ref`` 是宿主（SSE 流层）
    提供的共享字典 —— ``_emit_activity`` / ``_report`` 两个回调入口把当前
    ``state["stage"]`` 实时同步进去，流层据此给每个 progress 事件携带
    ``workflow_stage``（Agent 分支统计接线的唯一数据源）。不传（历史直调方/
    单测桩）零影响。
    """
    ws = Path(workspace_dir) if workspace_dir else workspace_root_for(task_id)
    # ✅ R149（2026-10-31 · FIX-E）：baseline 目录按任务隔离。
    #    旧实现 ``ws.parent / "baseline"`` 恒为固定名
    #    ``DATA_DIR/agent_workspaces/baseline`` —— 所有 outline Agent 任务
    #    共享同一份 manifest.json + files/：① 并发时 B 的 protect/_save 会
    #    按自己陈旧的引用集清掉 A 的原件副本（_save 清理「不再被引用」副本），
    #    A 的 restore() copyfile 命中已删副本 → FileNotFoundError → 整任务失败；
    #    ② 顺序复用时 restore() 会把前序任务的同名登记**复活**进本任务工作区
    #    （restore 对不存在文件也重建）→ 跨任务输入污染。seed 文件名固定
    #    （响应文件要求.md 等），同名相对路径必然互踩。对齐
    #    content_agent/repair.py:199 的 ``<task_id>_baseline`` 命名（按任务
    #    隔离，暂停/恢复后 manifest 仍随任务持久生效）。
    baseline_dir = ws.parent / f"{task_id}_baseline"
    ws.mkdir(parents=True, exist_ok=True)
    files = seed_files if seed_files is not None else seed_files_from_context(context)
    # 技术评分单链：宿主从 requirements（编制要求/评审要点）预置评分项来源，
    # 让 score-planning 阶段的模型读取并确认（本仓无易标 技术评分信息.md，
    # requirements 是评分大项来源——对齐 legacy L4215）。
    if not no_technical_score_mode:
        requirements_text = str(context.get("requirements_text") or "").strip()
        if requirements_text:
            groups = _build_technical_score_groups(requirements_text)
            files = [*files, {
                "path": TECHNICAL_SCORE_GROUPS_FILE,
                "content": json.dumps(
                    {"groups": groups}, ensure_ascii=False, indent=2),
            }, {
                "path": "技术评分信息.md",
                "content": requirements_text,
            }]
    for item in files:
        resolved = resolve_workspace_file(str(ws), item["path"])
        Path(resolved.resolved_path).write_text(
            str(item.get("content") or ""), encoding="utf-8")

    # 输入材料受保护：Agent 改动在每次提交校验前由 baseline.restore 还原。
    baseline = create_workspace_baseline(str(ws), str(baseline_dir))
    if files:
        baseline.protect("inputs", [item["path"] for item in files])

    schemas = build_json_validation_schemas(
        no_technical_score_mode=no_technical_score_mode,
        standalone_technical=STANDALONE_TECHNICAL)
    validators = build_output_validators(schemas)
    tools = build_outline_agent_tools(
        str(ws), validation_schemas=schemas,
        request_user_question=request_user_question,
        report_task_failure=report_task_failure)
    chat = chat_fn or _default_chat_fn
    chat_messages: list = list(messages or [])

    # 停止/暂停控制锚点（函数内 import：task_registry 拉起广播重依赖，
    # 保持 services.agent 包级惰性导入哲学）。
    from app.services.ai.task_registry import is_stopped, wait_resume

    state: dict = {"stage": initial_stage, "stage_index": initial_stage_index,
                   "known_ids": set(), "last_progress": 0.1}

    def _read_file(name: str) -> str:
        path = ws / name
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    def _read_score_directory_plan() -> dict:
        """读工作区 score-directory-plan.json；损坏/缺失回退空 dict（fail-soft）。"""
        try:
            parsed = json.loads(_read_file(SCORE_DIRECTORY_PLAN_FILE) or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}

    # ✅ 加固（2026-10-25 · 接线阶段建议 3「resume 断点」）：断点直入
    #    children_generation 等后续阶段时，工作区 outline.json 里已确认的
    #    目录节点 id 必须恢复进 known_ids —— 否则续跑产物沿用根节点 id 会被
    #    validate_outline_output 判为「未知 ID」，陷入提交修复直至三档止损。
    #    fail-soft：文件缺失 / 损坏 / 非预期结构一律静默回退空集，
    #    全新任务（工作区无该文件）行为与此前逐字一致。
    try:
        _seeded_tree = json.loads(_read_file(OUTLINE_OUTPUT_FILE) or "null")
        if isinstance(_seeded_tree, dict) and isinstance(
                _seeded_tree.get("outline"), list):
            state["known_ids"] = {
                item for item in collect_outline_ids(_seeded_tree["outline"])
                if item}
    except Exception:  # noqa: BLE001 — 断点状态恢复失败不得阻塞任务启动
        logger.debug("工作区断点 known_ids 恢复失败（按空集继续）",
                     exc_info=True)

    def _emit_activity(event: dict) -> None:
        # ✅ R155-O（L-1）：回调入口实时同步当前工作流阶段（流层读 stage_ref
        #    给事件携带 workflow_stage；回调在每个阶段内高频触发，至多滞后一拍）。
        if stage_ref is not None:
            stage_ref["stage"] = str(state.get("stage") or "")
        if emit_activity is not None:
            emit_activity(dict(event))

    async def _report(progress: float, message: str) -> None:
        state["last_progress"] = max(state["last_progress"], float(progress))
        if stage_ref is not None:
            stage_ref["stage"] = str(state.get("stage") or "")
        if report_progress is not None:
            await report_progress(state["last_progress"], message)

    async def _hold(tree: list) -> None:
        if hold_partial is not None:
            await hold_partial(list(tree))

    async def _chat_with_control(messages, *, tools, tool_choice=None, **kwargs):
        # 每个模型轮次都过一次停止/暂停闸门：长工具循环期间用户点停止
        # 最迟在一个轮次内生效（对齐易标 signal 在 pi 层逐轮检查）。
        await wait_resume(task_id)
        if is_stopped(task_id):
            raise OutlineAgentStopped("任务已停止")
        return await chat(messages, tools=tools, tool_choice=tool_choice,
                          **kwargs)

    async def _prompt_once(prompt: str):
        # 停止/暂停控制：等待恢复 → 停止即专用异常（run_stage 执行档
        # build_retry_prompt 返回 None → 立即上抛，不吞执行错误）。
        await wait_resume(task_id)
        if is_stopped(task_id):
            raise OutlineAgentStopped("任务已停止")
        chat_messages.append(
            {"role": "user", "content": disambiguate_prompt_tools(prompt)})
        result = await run_agent_loop(
            chat_messages, tools=tools, chat_fn=_chat_with_control,
            max_turns=_DEFAULT_MAX_TURNS,
            total_timeout=_LOOP_WALL_CLOCK_BUDGET)
        if result.stop_reason == STOP_TOOL_STOP:
            raise OutlineAgentReportedFailure(
                "Agent 报告无法按要求继续任务（report-failure）")
        if result.stop_reason in (STOP_TURN_LIMIT, STOP_TIMEOUT):
            raise RuntimeError(
                f"Agent 工具循环以 {result.stop_reason} 收束，未产出阶段结果")
        return result

    async def _check_submission(attempt: int):
        stage = state["stage"]
        # 易标 checkSubmission：先还原受保护文件，再读产物、跑阶段校验。
        restored = baseline.restore()
        if restored:
            # 有意偏离 1（报告第六章建议 1）：restored 活动由 check_submission
            # 注入方上报（stage_runner 只负责把文案拼进修复提示）。
            _emit_activity({
                "kind": "restored",
                "stage_index": state.get("stage_index", 0),
                "workflow_stage": stage,
                "message": ("程序已还原被改动的文件：" + "、".join(restored)
                            + "。这些文件由程序维护，不要修改。"),
            })
        # score-planning 消费（非产物）outline.json：validate_score_planning_roots
        # 需用它核对已确认根与规划分支的身份对应，故即便 STAGE_OUTPUT_FILES
        # 不含它也要读（STAGE_OUTPUT_FILES[score-planning] 只有评分两件套）。
        output_content = (_read_file(OUTLINE_OUTPUT_FILE)
                          if OUTLINE_OUTPUT_FILE in STAGE_OUTPUT_FILES[stage]
                          or stage == "score-planning"
                          else "")
        candidate = {
            "output_file": OUTLINE_OUTPUT_FILE,
            "output_content": output_content,
            "workspace": str(ws),
        }
        meta = StageMeta(
            workflow_stage=stage,
            read_file=_read_file,
            known_node_ids=set(state["known_ids"]),
            # score-planning 校验（validate_score_planning_roots）需「用户已确认
            # 的一级目录」根节点 dict 列表；无技术评分链恒空，零影响。
            locked_roots=list(state.get("confirmed_roots") or []),
        )
        report = validate_stage_outputs(candidate, meta, validators)
        return candidate, report, restored

    def _write_workspace_files(files_to_write: list, prepare_files: list) -> None:
        for item in files_to_write:
            resolved = resolve_workspace_file(str(ws), item.get("path") or "")
            Path(resolved.resolved_path).write_text(
                str(item.get("content") or ""), encoding="utf-8")

    def _prepare_output_files(prepare_files: list) -> None:
        # 必需产物预置空壳：缺失才建（continuation 已写入的不覆盖）。
        for name in prepare_files or []:
            path = ws / str(name)
            if not path.exists():
                path.write_text("", encoding="utf-8")

    async def _continue_task(candidate, meta) -> dict | None:
        stage = meta.get("workflow_stage") or state["stage"]
        value = meta.get("validation_result") or {}
        tree = value.get(OUTLINE_OUTPUT_FILE) if isinstance(value, dict) else None
        nodes = (tree or {}).get("outline") or []
        if stage == "initial-outline":
            # 易标 applyConfirmedSelection：numberOutline 后锁定一级目录；
            # 本仓自动确认（有意偏离 1），选择集 = 全量一级节点。
            state["known_ids"] = set(collect_outline_ids(nodes))
            state["confirmed_roots"] = list(nodes)
            await _hold(nodes)
            next_stage_index = (meta.get("stage")
                                if isinstance(meta.get("stage"), int)
                                else state["stage_index"]) + 1
            outline_file = {
                "path": OUTLINE_OUTPUT_FILE,
                "content": json.dumps(
                    {"outline": nodes}, ensure_ascii=False, indent=2),
            }
            if no_technical_score_mode:
                await _report(0.45, "一级目录已确认，开始生成完整目录")
                next_prompt = disambiguate_prompt_tools(
                    create_no_technical_score_children_prompt(
                        target_leaf_count=None,
                        standalone_technical=STANDALONE_TECHNICAL,
                        original_only=False,
                    ))
                next_stage = "children_generation"
                prepare = [OUTLINE_OUTPUT_FILE]
                message = "一级目录已确认，开始生成完整目录"
            else:
                # 技术评分单链：进入 score-planning（宿主已预置
                # technical-score-groups.json，模型确认分组并写目录映射）。
                await _report(0.30, "一级目录已确认，开始评分项与目录映射")
                next_prompt = disambiguate_prompt_tools(
                    create_score_planning_prompt(
                        standalone_technical=STANDALONE_TECHNICAL,
                    ))
                next_stage = "score-planning"
                prepare = [TECHNICAL_SCORE_GROUPS_FILE,
                           SCORE_DIRECTORY_PLAN_FILE]
                message = "Agent 正在规划评分项与目录映射"
            # 校验上下文随流转推进：下一阶段 _check_submission 必须用新
            # workflow_stage 与累积 known_ids（否则按旧阶段表校验）。
            state["stage"] = next_stage
            state["stage_index"] = next_stage_index
            return {
                "stage": next_stage,
                "stage_index": next_stage_index,
                "prompt": next_prompt,
                "files": [outline_file],
                "prepare_output_files": prepare,
                "message": message,
            }
        if stage == "score-planning":
            # 模型已写 technical-score-groups.json + score-directory-plan.json；
            # 宿主据此构建 leaf-allocation-context.json 并进入叶子分配。
            plan = (value.get(SCORE_DIRECTORY_PLAN_FILE)
                    if isinstance(value, dict) else None) or {}
            allocation_context = _build_leaf_allocation_context(plan)
            await _report(0.55, "评分项与目录映射完成，开始叶子分配")
            next_stage_index = (meta.get("stage")
                                if isinstance(meta.get("stage"), int)
                                else state["stage_index"]) + 1
            next_prompt = disambiguate_prompt_tools(
                create_leaf_allocation_prompt(
                    standalone_technical=STANDALONE_TECHNICAL,
                ))
            state["stage"] = "leaf_allocation"
            state["stage_index"] = next_stage_index
            return {
                "stage": "leaf_allocation",
                "stage_index": next_stage_index,
                "prompt": next_prompt,
                "files": [{
                    "path": LEAF_ALLOCATION_CONTEXT_FILE,
                    "content": json.dumps(
                        allocation_context, ensure_ascii=False, indent=2),
                }],
                "prepare_output_files": [LEAF_ALLOCATION_FILE],
                "message": "Agent 正在分配叶子节点",
            }
        if stage == "leaf_allocation":
            plan = _read_score_directory_plan()
            await _report(0.65, "叶子分配完成，开始生成完整目录")
            next_stage_index = (meta.get("stage")
                                if isinstance(meta.get("stage"), int)
                                else state["stage_index"]) + 1
            next_prompt = disambiguate_prompt_tools(
                create_children_prompt(
                    has_original_plan=has_original_plan,
                    original_only=False,
                    # 偏离 3（word_budget→targetLeafCount）仍登记：技术单链
                    # 暂以 agent-decides 让模型自主分配，不硬映射字数。
                    target_leaf_count=None,
                    allow_root_changes=(plan.get("allow_root_changes") is True),
                    standalone_technical=STANDALONE_TECHNICAL,
                ))
            state["stage"] = "children_generation"
            state["stage_index"] = next_stage_index
            return {
                "stage": "children_generation",
                "stage_index": next_stage_index,
                "prompt": next_prompt,
                "files": [],
                "prepare_output_files": [OUTLINE_OUTPUT_FILE],
                "message": "Agent 正在生成完整目录",
            }
        if stage == "children_generation":
            # 易标 L1394-1406：children accept → target_leaf_count=None 时恒走
            # continueWithOutlineReview（L1099-1129）：锁定全部节点 id、回传
            # 成果、写审核上下文与最新目录，进入 outline_review 阶段。
            state["known_ids"] = {
                item for item in collect_outline_ids(nodes) if item}
            await _hold(nodes)
            actual_leaf_count = count_ai_leaves(nodes)
            next_stage_index = (meta.get("stage")
                                if isinstance(meta.get("stage"), int)
                                else state["stage_index"]) + 1
            if no_technical_score_mode:
                await _report(0.88, "子目录生成完成，正在准备最终审核")
                review_context = build_outline_review_context(
                    outline={"outline": nodes}, target_leaf_count=None)
                review_files = [
                    {"path": OUTLINE_OUTPUT_FILE,
                     "content": json.dumps(
                         {"outline": nodes}, ensure_ascii=False, indent=2)},
                    {"path": OUTLINE_REVIEW_CONTEXT_FILE,
                     "content": json.dumps(
                         review_context, ensure_ascii=False, indent=2)},
                ]
                next_prompt = disambiguate_prompt_tools(
                    create_no_technical_score_review_prompt(
                        target_leaf_count=None,
                        actual_leaf_count=actual_leaf_count,
                        original_only=False,
                        input_files=review_files,
                    ))
                next_stage = "outline_review"
                prepare = [OUTLINE_REVIEW_FILE]
                message = "Agent 正在审核并修复目录"
            else:
                # 技术评分单链：children accept → leaf_adjustment（先按评分
                # 规划调整叶子数量，再进入技术审核）。
                await _report(0.80, "子目录生成完成，开始叶子数量调整")
                next_prompt = disambiguate_prompt_tools(
                    create_leaf_adjustment_prompt(
                        target_leaf_count=None,
                        actual_leaf_count=actual_leaf_count,
                        no_technical_score_mode=False,
                        original_only=False,
                    ))
                next_stage = "leaf_adjustment"
                prepare = [OUTLINE_OUTPUT_FILE]
                message = "Agent 正在调整叶子数量"
                review_files = []
            # 校验上下文随流转推进：review 阶段的 _check_submission 必须用
            # 累积 known_ids（审核产物沿用全部已确认节点 id）。
            state["stage"] = next_stage
            state["stage_index"] = next_stage_index
            return {
                "stage": next_stage,
                "stage_index": next_stage_index,
                "prompt": next_prompt,
                "files": review_files,
                "prepare_output_files": prepare,
                "message": message,
            }
        if stage == "leaf_adjustment":
            # 技术评分单链：叶子调整 accept → 技术审核（易标 L825-858）。
            state["known_ids"] = {
                item for item in collect_outline_ids(nodes) if item}
            await _hold(nodes)
            actual_leaf_count = count_ai_leaves(nodes)
            plan = _read_score_directory_plan()
            await _report(0.88, "叶子调整完成，正在准备最终审核")
            review_context = build_outline_review_context(
                outline={"outline": nodes},
                target_leaf_count=None,
                score_mapping=_collect_score_mapping(plan),
                scoreDirectoryPlan=plan,
            )
            review_files = [
                {"path": OUTLINE_OUTPUT_FILE,
                 "content": json.dumps(
                     {"outline": nodes}, ensure_ascii=False, indent=2)},
                {"path": OUTLINE_REVIEW_CONTEXT_FILE,
                 "content": json.dumps(
                     review_context, ensure_ascii=False, indent=2)},
            ]
            next_stage_index = (meta.get("stage")
                                if isinstance(meta.get("stage"), int)
                                else state["stage_index"]) + 1
            next_prompt = disambiguate_prompt_tools(
                create_outline_review_prompt(
                    target_leaf_count=None,
                    actual_leaf_count=actual_leaf_count,
                    allow_root_changes=(plan.get("allow_root_changes") is True),
                    input_files=review_files,
                ))
            state["stage"] = "outline_review"
            state["stage_index"] = next_stage_index
            return {
                "stage": "outline_review",
                "stage_index": next_stage_index,
                "prompt": next_prompt,
                "files": review_files,
                "prepare_output_files": [OUTLINE_REVIEW_FILE],
                "message": "Agent 正在审核并修复目录",
            }
        # outline_review accept（易标 L1298-1332 noTech 分支）→ 单链完成。
        review = (value.get(OUTLINE_REVIEW_FILE)
                  if isinstance(value, dict) else None)
        state["known_ids"] = {
            item for item in collect_outline_ids(nodes) if item}
        await _hold(nodes)
        await _report(0.95, review_status_message((review or {}).get("status")))
        # 易标 L1306-1309：accept 分支把验证后的最终目录写回工作区（noTech
        # 无评分规划文件）。stage_runner 对 complete=True 的 continuation 在
        # 写 files 之前短路，故在此显式写回（与 meta.writeFiles 等价）；
        # 失败 fail-soft —— 工作区本就保留 Agent 写入的同一份目录。
        try:
            _write_workspace_files([{
                "path": OUTLINE_OUTPUT_FILE,
                "content": json.dumps(
                    {"outline": nodes}, ensure_ascii=False, indent=2),
            }], [])
        except Exception:  # noqa: BLE001 — 写回失败不阻断成果交接
            logger.warning("审核通过后写回最终目录失败（工作区保留 Agent 写入版本）",
                           exc_info=True)
        # ✅ R162 层级硬性标准完成前终检（用户指令 二.2「生成后必须用代码校验」）：
        #    最终目录若仍有 L4 及更深或子节点数量越界（L1/L2 各 3~5）→ 报错终止，
        #    不带病完成（与 legacy 生成链路的合规终检同语义）。数量约束在
        #    children 阶段已由 Schema（minItems/maxItems）前置强制，此处兜底。
        _violations = validate_outline_nodes(nodes)
        if _violations:
            raise OutlineAgentReportedFailure(format_violations_message(_violations))
        return {"complete": True}

    async def _write_files(files_to_write, prepare_files) -> None:
        _write_workspace_files(files_to_write, prepare_files)

    async def _prepare_files(prepare_files) -> None:
        _prepare_output_files(prepare_files)

    async def _read_output() -> str:
        return _read_file(OUTLINE_OUTPUT_FILE)

    # 预置本阶段必需产物空壳（易标 initial runTask prepare_output_files）。
    _prepare_output_files(STAGE_OUTPUT_FILES.get(initial_stage, []))

    has_original_plan = bool(str(context.get("reference_outline") or "").strip())
    # ✅ R149（FIX-F）：指示词与工作区实际 seed 的文件保持一致（requirements
    #    为空时 响应文件要求.md 不会落盘，见 seed_files_from_context）。
    has_requirements = bool(str(context.get("requirements_text") or "").strip())
    if initial_stage == "initial-outline":
        first_prompt = disambiguate_prompt_tools(create_initial_prompt(
            build_task_instruction(has_original_plan=has_original_plan,
                                   has_requirements=has_requirements),
            standalone_technical=STANDALONE_TECHNICAL,
            no_technical_score_mode=no_technical_score_mode,
        ))
    else:
        # resume / 直入 children：与易标 directory runTask 初始 prompt 同构
        # （L1269–1271 noTech 分支），当前工作区 outline.json 已含已确认目录。
        first_prompt = disambiguate_prompt_tools(
            create_no_technical_score_children_prompt(
                target_leaf_count=None,
                standalone_technical=STANDALONE_TECHNICAL,
                original_only=False,
            ))

    await _report(0.10, "正在生成目录...")
    _emit_activity({
        "kind": "start",
        "stage_index": initial_stage_index,
        "workflow_stage": initial_stage,
        "message": "目录生成 Agent 开始执行",
    })

    try:
        result = await run_stage(
            first_prompt,
            prompt_once=_prompt_once,
            check_submission=_check_submission,
            continue_task=_continue_task,
            build_retry_prompt=create_output_repair_prompt,
            read_output=_read_output,
            write_files=_write_files,
            prepare_output_files=_prepare_files,
            max_retries=1,
            output_file=OUTLINE_OUTPUT_FILE,
            runtime_name="目录生成 Agent",
            initial_stage_index=initial_stage_index,
            initial_stage=initial_stage,
            meta_extra={"task_id": task_id, "scheme_id": scheme.get("id", "")},
            on_activity=_emit_activity,
            on_event=emit_event,
        )
    except StageStoppedError as error:
        # 建议4（接线报告第六章）：三档提交止损显式留痕 —— 此前止损只体现在
        # 修复提示与最终 error 文案里，用户无从感知「已尽力修复仍未通过」。
        _emit_activity({
            "kind": "submission.stopped",
            "stage_index": state.get("stage_index", 0),
            "workflow_stage": state.get("stage", ""),
            "message": str(error)[:300],
        })
        logger.warning("目录生成 Agent 三档提交止损（task=%s · stage=%s）",
                       task_id, state.get("stage"))
        raise
    state["stage"] = result.stage
    validation_result = result.validation_result or {}
    final_tree = (validation_result.get(OUTLINE_OUTPUT_FILE)
                  if isinstance(validation_result, dict) else None) or {}
    outline = list(final_tree.get("outline") or [])
    review = (validation_result.get(OUTLINE_REVIEW_FILE)
              if isinstance(validation_result, dict) else None)
    await _report(1.0, "目录生成完成")
    return OutlineAgentOutcome(
        outline=outline,
        review=review if isinstance(review, dict) else {},
        result=result, stage=result.stage)


async def outline_agent_event_stream(
    *,
    task_id: str,
    scheme_id: str,
    scheme: dict,
    context: dict,
    word_budget: int = 30000,
    partial_holder: dict | None = None,
    save_checkpoint: Callable | None = None,
    **run_kwargs,
) -> AsyncIterator[dict]:
    """SSE 桥：跑单链并产出与 legacy 同形状的事件（含停止/终态收尾）。

    事件形状（对齐 generate_outline legacy）：

    - ``{'event': 'progress', 'task_id', 'progress', 'message'}``；
    - ``{'event': 'stopped', 'task_id', 'outline', 'partial'}``（用户停止，
      checkpoint → finish_task → yield 顺序同 ``_finish_stopped_with_partial``）；
    - ``{'event': 'completed', 'task_id', 'outline', 'review'}``；
    - ``StageStoppedError`` / 其他异常**原样上抛**，由 SSE 外层既有
      error 分支（带 ``_partial_holder`` 成果）收尾。
    """
    from app.services.ai.task_registry import finish_task, update_progress

    holder = partial_holder if partial_holder is not None else {}
    queue: asyncio.Queue = asyncio.Queue()
    # ✅ R155-O（R149 遗留 L-1）：工作流阶段共享字典 —— run_outline_agent 的
    #    回调入口实时写入当前 stage，本层三个 progress 构造器据此给事件携带
    #    ``workflow_stage``（SSE 消费侧映射进进度阶段模型，修「准备中 0/0」
    #    全程失真）。Agent 未产出任何回调前的早期事件携带空串，消费侧不映射。
    stage_ref: dict = {}

    async def _report_progress(progress: float, message: str) -> None:
        await update_progress(task_id, progress, message)
        await queue.put(("__event__", {
            "event": "progress", "task_id": task_id,
            "progress": progress, "message": message,
            "workflow_stage": str(stage_ref.get("stage") or ""),
        }))

    def _emit_activity(event: dict) -> None:
        message = event.get("message")
        if not message:
            return
        queue.put_nowait(("__event__", {
            "event": "progress", "task_id": task_id,
            "progress": None, "message": str(message),
            "workflow_stage": str(stage_ref.get("stage") or ""),
        }))

    def _emit_event(event: dict) -> None:
        queue.put_nowait(("__event__", {
            "event": "progress", "task_id": task_id,
            "progress": None,
            "message": str(event.get("message") or "Agent 正在自动修复"),
            "workflow_stage": str(stage_ref.get("stage") or ""),
        }))

    async def _hold(tree: list) -> None:
        holder["outline"] = list(tree)

    async def _runner() -> None:
        try:
            outcome = await run_outline_agent(
                task_id=task_id, scheme=scheme, context=context,
                word_budget=word_budget, hold_partial=_hold,
                emit_activity=_emit_activity, emit_event=_emit_event,
                report_progress=_report_progress, stage_ref=stage_ref,
                **run_kwargs)
            await queue.put(("__done__", outcome))
        except asyncio.CancelledError:
            raise  # 消费侧断开/收尾取消：不转事件，保持任务取消语义
        except BaseException as error:  # noqa: BLE001 — 透传后由消费侧处置
            await queue.put(("__error__", error))

    pipeline = asyncio.ensure_future(_runner())
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "__event__":
                if payload.get("progress") is None:
                    payload.pop("progress", None)
                yield payload
                continue
            if kind == "__done__":
                outline = payload.outline
                await update_progress(task_id, 1.0, "目录生成完成",
                                      event="completed")
                done_payload = {
                    "event": "completed", "task_id": task_id,
                    "outline": outline,
                    "review": payload.review if isinstance(payload.review, dict) else {},
                }
                status, message = "completed", "目录生成完成"
                if save_checkpoint is not None:
                    try:
                        await save_checkpoint(task_id, done_payload)
                    except Exception:  # noqa: BLE001 — R89 S2 降级而非静默
                        logger.warning(
                            "检查点写入失败（task=%s · 目录 Agent · 终态成果）→ 降级 degraded",
                            task_id, exc_info=True)
                        status = "degraded"
                        done_payload["checkpoint_failed"] = True
                        message = ("目录已生成，但成果未能写入检查点："
                                   "刷新或断线后可能无法恢复，建议立即手动保存")
                await finish_task(task_id, status, message)
                yield done_payload
                return
            # __error__：OutlineAgentStopped 单独收尾；其余原样上抛。
            error = payload
            if isinstance(error, OutlineAgentStopped):
                stopped_payload: dict = {"event": "stopped", "task_id": task_id}
                outline = holder.get("outline") or []
                if outline:
                    stopped_payload["outline"] = outline
                    stopped_payload["partial"] = True
                    if save_checkpoint is not None:
                        try:
                            await save_checkpoint(task_id, stopped_payload)
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "检查点写入失败（task=%s · 目录 Agent · 停止时成果）",
                                task_id, exc_info=True)
                await finish_task(task_id, "stopped", "用户已停止")
                yield stopped_payload
                return
            raise error
    finally:
        if not pipeline.done():
            pipeline.cancel()
