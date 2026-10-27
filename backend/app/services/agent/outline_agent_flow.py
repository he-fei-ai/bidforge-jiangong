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
  留待六阶段全量接线（报告遗留 新1 剩余部分）。

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
from typing import Any, AsyncIterator, Callable

from app.config import DATA_DIR

from .agent_loop import (
    STOP_TIMEOUT,
    STOP_TOOL_STOP,
    STOP_TURN_LIMIT,
    run_agent_loop,
)
from .outline_identity import collect_outline_ids
from .outline_schemas import build_json_validation_schemas, build_output_validators
from .stage_prompts import (
    create_initial_prompt,
    create_no_technical_score_children_prompt,
    create_no_technical_score_review_prompt,
    create_output_repair_prompt,
)
from .stage_runner import StageExecutionResult, StageStoppedError, run_stage
from .validation import (
    AI_CONTENT_MODE,
    CONTENT_MODES,
    OUTLINE_OUTPUT_FILE,
    OUTLINE_REVIEW_CONTEXT_FILE,
    OUTLINE_REVIEW_FILE,
    STAGE_OUTPUT_FILES,
    StageMeta,
    validate_stage_outputs,
)
from .workspace_baseline import create_workspace_baseline
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

#: 遗留⑤ 消歧目标子串（stage_prompts L597/L673 逐字）与替换词。
_BASH_PROMPT_TOKEN = "grep、read 或 bash"
_FIND_PROMPT_TOKEN = "grep、read 或 find"

#: 易标 noTech 分支 seed 文件名（outlineGenerationTaskV2.cjs L935–939）。
_REQUIREMENTS_FILE = "响应文件要求.md"
_OVERVIEW_FILE = "项目概述.md"
_ORIGINAL_PLAN_FILE = "原方案.md"

_DEFAULT_MAX_TURNS = 40


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


def build_outline_review_context(*, outline, target_leaf_count) -> dict:
    """宿主程序确定性审核结果（易标 ``buildOutlineReviewContext`` L562-588）。

    有意偏离（登记）：``score_mapping``（易标 L586，技术评分模式专用）
    随 ``collectScoreMappingCoverage`` 一并留待六阶段接线 —— 本仓单链恒为
    ``no_technical_score_mode=True``，``scoreDirectoryPlan`` 恒为空。
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
    return {
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


def review_status_message(status) -> str:
    """审核状态 → 结论文案（易标 L1317-1323 逐字；未登记状态走兜底分支）。"""
    if status == "passed":
        return "目录审核通过"
    if status == "simple_fix":
        return "目录审核完成，Agent 已自动微调简单问题"
    if status == "user_feedback":
        return "目录审核完成，已按用户反馈修复"
    return "目录审核完成，用户选择保留当前目录"


def build_task_instruction(*, has_original_plan: bool) -> str:
    """initial-outline 任务指示（易标 L951–955，noTech 非 standalone 二分支逐字）。"""
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
) -> OutlineAgentOutcome:
    """执行 initial-outline → children_generation → outline_review 单链（业务层，无 SSE 依赖）。

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
    """
    ws = Path(workspace_dir) if workspace_dir else workspace_root_for(task_id)
    baseline_dir = ws.parent / "baseline"
    ws.mkdir(parents=True, exist_ok=True)
    files = seed_files if seed_files is not None else seed_files_from_context(context)
    for item in files:
        resolved = resolve_workspace_file(str(ws), item["path"])
        Path(resolved.resolved_path).write_text(
            str(item.get("content") or ""), encoding="utf-8")

    # 输入材料受保护：Agent 改动在每次提交校验前由 baseline.restore 还原。
    baseline = create_workspace_baseline(str(ws), str(baseline_dir))
    if files:
        baseline.protect("inputs", [item["path"] for item in files])

    schemas = build_json_validation_schemas(
        no_technical_score_mode=NO_TECHNICAL_SCORE_MODE,
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
        if emit_activity is not None:
            emit_activity(dict(event))

    async def _report(progress: float, message: str) -> None:
        state["last_progress"] = max(state["last_progress"], float(progress))
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
            max_turns=_DEFAULT_MAX_TURNS)
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
        output_content = (_read_file(OUTLINE_OUTPUT_FILE)
                          if OUTLINE_OUTPUT_FILE in STAGE_OUTPUT_FILES[stage]
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
            await _hold(nodes)
            await _report(0.45, "一级目录已确认，开始生成完整目录")
            next_prompt = disambiguate_prompt_tools(
                create_no_technical_score_children_prompt(
                    target_leaf_count=None,
                    standalone_technical=STANDALONE_TECHNICAL,
                    original_only=False,
                ))
            next_stage_index = (meta.get("stage")
                                 if isinstance(meta.get("stage"), int)
                                 else state["stage_index"]) + 1
            # 校验上下文随流转推进：children 阶段的 _check_submission 必须
            # 用新 workflow_stage 与累积 known_ids（否则按旧阶段表校验）。
            state["stage"] = "children_generation"
            state["stage_index"] = next_stage_index
            return {
                "stage": "children_generation",
                "stage_index": next_stage_index,
                "prompt": next_prompt,
                "files": [{
                    "path": OUTLINE_OUTPUT_FILE,
                    "content": json.dumps(
                        {"outline": nodes}, ensure_ascii=False, indent=2),
                }],
                "prepare_output_files": [OUTLINE_OUTPUT_FILE],
                "message": "一级目录已确认，开始生成完整目录",
            }
        if stage == "children_generation":
            # 易标 L1394-1406：children accept → target_leaf_count=None 时恒走
            # continueWithOutlineReview（L1099-1129）：锁定全部节点 id、回传
            # 成果、写审核上下文与最新目录，进入 outline_review 阶段。
            state["known_ids"] = {
                item for item in collect_outline_ids(nodes) if item}
            await _hold(nodes)
            actual_leaf_count = count_ai_leaves(nodes)
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
            next_stage_index = (meta.get("stage")
                                if isinstance(meta.get("stage"), int)
                                else state["stage_index"]) + 1
            # 校验上下文随流转推进：review 阶段的 _check_submission 必须用
            # 累积 known_ids（审核产物沿用全部已确认节点 id）。
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
    if initial_stage == "initial-outline":
        first_prompt = disambiguate_prompt_tools(create_initial_prompt(
            build_task_instruction(has_original_plan=has_original_plan),
            standalone_technical=STANDALONE_TECHNICAL,
            no_technical_score_mode=NO_TECHNICAL_SCORE_MODE,
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

    async def _report_progress(progress: float, message: str) -> None:
        await update_progress(task_id, progress, message)
        await queue.put(("__event__", {
            "event": "progress", "task_id": task_id,
            "progress": progress, "message": message,
        }))

    def _emit_activity(event: dict) -> None:
        message = event.get("message")
        if not message:
            return
        queue.put_nowait(("__event__", {
            "event": "progress", "task_id": task_id,
            "progress": None, "message": str(message),
        }))

    def _emit_event(event: dict) -> None:
        queue.put_nowait(("__event__", {
            "event": "progress", "task_id": task_id,
            "progress": None,
            "message": str(event.get("message") or "Agent 正在自动修复"),
        }))

    async def _hold(tree: list) -> None:
        holder["outline"] = list(tree)

    async def _runner() -> None:
        try:
            outcome = await run_outline_agent(
                task_id=task_id, scheme=scheme, context=context,
                word_budget=word_budget, hold_partial=_hold,
                emit_activity=_emit_activity, emit_event=_emit_event,
                report_progress=_report_progress, **run_kwargs)
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
