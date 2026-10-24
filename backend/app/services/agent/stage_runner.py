"""P3 — 阶段执行编排（移植自易标 Yibiao 2.30.20 的 continueTask 状态机）。

移植来源（仓库外只读参照 ``C:\\Users\\Administrator\\AppData\\Local\\Temp\\yibiao_outline\\``）：

1. ``electron_services_pi_piRuntimeService.cjs`` L850–1073：
   ``checkSubmission``（先还原受保护文件 → 读产物 → 调用
   ``payload.validateOutput`` → 返回 ``{candidate, report, restored}``）、
   ``buildRetryPrompt`` 默认实现（L215 执行档 / L1155 提交档）、
   ``recordRetry``（重试摘要 + ``stagePrompt`` 替换 + 活动/监控事件）、
   ``restored`` 还原文件语义。
2. ``electron_services_pi_piRuntimeService.cjs`` L1073–1240（本次核心）：
   双层 ``while`` —— 外层阶段循环、内层执行重试循环；
   ``executionRetries`` 与 ``maxRetries`` 预算（L1107 超限上抛）；
   ``submission.inspect(report)`` 的 stop / accept / 修复三分支
   （L1124–1147）；修复提示拼装（restored 还原说明 + businessPrompt →
   ``buildSubmissionRepairPrompt``，L1149–1161）；``continueTask`` 回调
   流转（L1164–1231：``complete`` / ``prompt`` / ``stage_index`` /
   stage 变化时 ``submission = createSubmissionPolicy()`` 重置 L1193、
   ``await_before_prompt`` / ``compact_before_prompt`` 可选钩子）。
3. ``outlineGenerationTaskV2.cjs`` L1140–1470：``continueTask`` 的两种形态
   （初始阶段写回 ``validation_result`` → ``complete:true``，L1157；
   主流程按 ``workflow_stage`` 分派下一阶段，L1297 起）。
   阶段流转规则抽象为本模块注入的 ``continue_task`` 回调参数；
   **本模块不复刻易标业务阶段表**（业务阶段表由调用方注入）。

注入点设计（全部为可调用/回调；无 Electron、无文件系统默认实现）：

- ``prompt_once(prompt)``：发一次阶段提示词（内部可接 P0
  ``run_agent_loop``）；返回值含 ``turns``（dict 或属性）时计入统计。
- ``check_submission(attempt)``：提交校验，返回
  ``(candidate, report, restored)`` —— 易标 ``checkSubmission`` 三元组；
  ``attempt`` 为 ``submission.attempts + 1``（易标 L1104）。
- ``build_retry_prompt(request, meta)``：易标 ``payload.buildRetryPrompt``。
  ``request['kind']`` 为 ``'execution'`` / ``'submission'``。
  **执行档**：回调缺省或返回 ``None`` 视为不可修复 → 直接上抛原始异常
  （易标 L1117 ``if (retryPrompt === null) throw error``）。
  **提交档**：回调缺省时用易标 L1155 默认业务提示；回调返回 ``None``/
  空串按易标 ``filter(Boolean)``（L1159）丢弃，修复提示仍照常拼装。
- ``continue_task(candidate, meta)``：易标 ``payload.continueTask``，
  返回 ``dict | None``；字段语义见 :func:`run_stage` 的说明。
- ``read_output()``：执行重试记录摘要时读取当前产物文本（易标
  ``readOutputAsync(workspaceDir, outputFile).content``）。
- ``write_files(files, prepare_output_files)`` /
  ``prepare_output_files(files)``：``continue_task`` 返回 ``files`` /
  ``prepare_output_files`` 时执行；缺省 ``None`` 跳过。
- ``run_compaction(stage, continuation)``：``compact_before_prompt``
  钩子；本仓默认无压缩，缺省 ``None`` 时跳过（易标 ``runStageCompaction``
  的内部状态与“压缩失败补做”逻辑未移植，见文末偏离点）。
- ``await_before_prompt``：由 ``continue_task`` 返回，可 await 对象或
  可调用（易标 ``Promise.resolve(...)``）；异常原样上抛。
- ``submission_policy_factory`` / ``build_repair_prompt``：缺省复用 P2
  ``create_submission_policy`` / ``build_submission_repair_prompt``。
- ``on_activity`` / ``on_event``：对应易标 ``touchActivity`` /
  ``emitMonitorEvent``，透传事件 dict（同步回调）；按易标
  ``try{}catch{}``（L291/L366）吞掉观测回调自身异常，不影响主流程。

失败边界与 P0 一致：工具层错误已由 ``agent_loop`` 收束，本模块只处理
阶段/提交层错误 —— 执行异常按预算重试、提交 ``stop`` 上抛专用异常、
其余异常原样上抛（不吞执行异常）。

与原文的偏离点（已知且有意）：

1. 执行档 ``build_retry_prompt`` **缺省**时按任务红线视为不可修复直接
   上抛，不复用易标 L215 内置默认提示（易标是“缺省函数 → 内置默认，
   返回 null → 上抛”；本仓把两种情形合并为“无可用提示 → 上抛”）。
2. ``await_before_prompt``/``compact_before_prompt`` 只保留“先并行启动 →
   可选压缩 → 等待 → 异常上抛”的主干；易标依赖会话内部状态的
   “压缩未完成补做一次”（L1209–1211）不移植。
3. 易标 ``touchActivity`` 内的 ``restored`` 还原活动由 ``check_submission``
   注入方自行上报（该还原发生在易标 ``checkSubmission`` 内部，L1007–1018）；
   本模块只负责把 ``restored`` 文案拼进修复提示。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

try:  # 包内运行（镜像 app/services/agent/）
    from .submission_policy import (
        build_submission_repair_prompt,
        create_submission_policy,
    )
except ImportError:  # 仓库外临时目录单文件直跑
    from submission_policy import (  # type: ignore[no-redef]
        build_submission_repair_prompt,
        create_submission_policy,
    )

logger = logging.getLogger("stage_runner")

#: 全部阶段流转结束（continue_task 缺省 / complete / 无 prompt）后的收束原因。
STOP_COMPLETED = "completed"

#: ``stop`` 兜底止损异常消息前缀（易标 piRuntimeService.cjs L1127 逐字）。
STOP_BLOCKING_PREFIX = "已按最低完成目标再修复一轮，仍存在阻塞流程的问题，任务已停止："

#: ``restored`` 还原文件说明模板（易标 L1157 逐字；``{files}`` 为 ``、`` 连接的文件名）。
RESTORED_NOTE_TEMPLATE = "程序已还原被改动的文件：{files}。这些文件由程序维护，不要修改。"


class StageStoppedError(RuntimeError):
    """提交策略 ``stop`` 兜底止损（易标 L1125–1131 的专用 Error）。

    - ``issues``：整个提交报告的 issues（对齐易标 ``error.issues``，
      含非阻塞项）；
    - ``agent_validation_failed``：对齐易标 ``error.agentValidationFailed``。
    """

    agent_validation_failed = True

    def __init__(self, message: str, issues: list) -> None:
        super().__init__(message)
        self.issues = list(issues)


@dataclass
class StageExecutionResult:
    """一次 ``run_stage`` 的产物与统计（易标 ``runTask`` result 的阶段子集）。"""

    #: 最近一次 accept 的 ``report.value``（易标 ``validationResult``）
    validation_result: Any = None
    #: 最近一次提交校验的 candidate
    candidate: Any = None
    #: 最近一个阶段 accept 时携带的非阻塞 issues（易标 ``acceptedIssues``）
    accepted_issues: list = field(default_factory=list)
    #: 全部阶段的 accept 质量问题流水（易标 ``acceptedSubmissionIssues``）
    accepted_submission_issues: list = field(default_factory=list)
    #: 最终阶段序号 / workflow 阶段名
    stage_index: Any = 0
    stage: str = ""
    #: ``prompt_once`` 调用次数（含失败的尝试）
    prompt_calls: int = 0
    #: ``prompt_once`` 返回值累计 ``turns``
    turns: int = 0
    #: 重试摘要流水（易标 ``retryAttempts``，执行档与提交档共用全局序号）
    retry_attempts: list = field(default_factory=list)
    stop_reason: str = STOP_COMPLETED

    @property
    def execution_retries(self) -> int:
        """执行异常重试总次数（易标 ``executionRetries`` 逐阶段累加）。"""
        return sum(1 for item in self.retry_attempts
                   if item.get("kind") == "execution")

    @property
    def submission_retries(self) -> int:
        """提交修复总次数（易标 ``submission.attempts`` 跨阶段累加）。"""
        return sum(1 for item in self.retry_attempts
                   if item.get("kind") == "submission")


async def _maybe_await(value: Any) -> Any:
    """同步/异步回调统一入口（注入点允许普通函数或协程）。"""
    if inspect.isawaitable(value):
        return await value
    return value


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """易标对象属性访问的 dict/对象双形态兜底（candidate 可为 dict 或对象）。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _extract_turns(result: Any) -> int:
    """从 ``prompt_once`` 返回值提取 ``turns``（P0 ``AgentLoopResult.turns``）。"""
    turns = _field(result, "turns")
    return turns if isinstance(turns, int) and not isinstance(turns, bool) else 0


def _compact_text(value: Any, max_length: int = 300) -> str:
    """易标 ``compactText``（piRuntimeService.cjs L170–172）逐行为移植。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > max_length:
        return text[:max_length - 1] + "…"
    return text


def _js_number(value: Any) -> float:
    """对齐 JS ``Number()`` 转换（覆盖常见形态；其余按 ``NaN``）。"""
    if value is None:
        return 0.0  # Number(null)
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0  # Number('')
        try:
            return float(text)
        except ValueError:
            return math.nan  # Number('abc')
    return math.nan


def _advance_stage_index(current: Any, continuation: dict) -> Any:
    """易标 L1188–1190：``Number.isFinite(Number(stage_index)) ? Number(...) : stage_index + 1``。"""
    if "stage_index" in continuation:
        number = _js_number(continuation["stage_index"])
        if math.isfinite(number):
            return int(number) if float(number).is_integer() else number
    return current + 1


def _file_entry(item: Any) -> dict:
    """易标 L1221：task_input 事件里的文件按 ``{path, content}`` 字符串化。"""
    if isinstance(item, dict):
        return {"path": str(item.get("path") or ""),
                "content": str(item.get("content") or "")}
    return {"path": str(item), "content": ""}


def _default_submission_business_prompt(output_file: str) -> str:
    """易标 L1155 提交档缺省业务提示（逐字，``${outputFile}`` 由注入参数替换）。"""
    return (f"本次结果文件：{output_file}。"
            "在当前会话和工作区修复，保留已完成内容。")


def _consume_task_exception(task: "asyncio.Future") -> None:
    """提前挂 ``.catch(()=>{})`` 等价物：标记异常已取走，``await`` 时仍上抛。"""
    if not task.cancelled():
        task.exception()


async def run_stage(
    prompt: str,
    *,
    prompt_once: Callable[[str], Any],
    check_submission: Callable[[int], Any],
    continue_task: Callable[[Any, dict], Any] | None = None,
    build_retry_prompt: Callable[[dict, dict], Any] | None = None,
    read_output: Callable[[], Any] | None = None,
    write_files: Callable[[list, list], Any] | None = None,
    prepare_output_files: Callable[[list], Any] | None = None,
    run_compaction: Callable[[str, dict], Any] | None = None,
    max_retries: int = 1,
    output_file: str = "",
    runtime_name: str = "Agent",
    initial_stage_index: Any = 0,
    initial_stage: str = "",
    meta_extra: dict | None = None,
    submission_policy_factory: Callable[[], Any] = create_submission_policy,
    build_repair_prompt: Callable[..., str] = build_submission_repair_prompt,
    on_activity: Callable[[dict], Any] | None = None,
    on_event: Callable[[dict], Any] | None = None,
) -> StageExecutionResult:
    """执行「阶段循环 + 执行重试循环 + continueTask 流转」状态机。

    结构 1:1 对应易标 piRuntimeService.cjs L1073–1231 的双层 ``while``：

    - 内层：``prompt_once`` → ``check_submission``（try 内）；执行异常按
      ``max_retries`` 预算重试（每阶段清零，易标 L1077）；成功后
      ``submission.inspect(report)`` 三分支 —— ``stop`` 抛
      :class:`StageStoppedError`、``accept`` 携带全部 issues 跳出、
      否则拼修复提示重试（提交修复无次数上限，靠 stop 止损）；
    - 外层：``continue_task`` 返回 ``None`` / ``complete=True`` /
      无 ``prompt`` 即结束；否则写文件、推进 ``stage_index``、
      **stage 变化时重置提交策略**（L1193）、换提示词与钩子后进入下一阶段。

    ``continue_task`` 返回字段（易标 ``continuation`` 1:1）：

    - ``complete``：``True`` → 结束；
    - ``prompt``：下一阶段提示词；缺省/空 → 结束；
    - ``stage_index``：合法数字用之（``Number.isFinite(Number(...))`` 语义），
      否则 ``stage_index + 1``；
    - ``stage``：下一 ``workflow_stage``；缺省 → ``workflow_stage_{index}``；
      相对已完成阶段变化时 ``submission_policy_factory()`` 重置策略；
    - ``files``：``[{path, content}]``，经 ``write_files`` 钩子写入；
    - ``prepare_output_files``：产物文件表，随 ``write_files`` 第二参并
      交 ``prepare_output_files`` 钩子（钩子缺省时跳过）；
    - ``await_before_prompt``：可 await 对象/可调用，异常原样上抛；
    - ``compact_before_prompt``：``True`` → 调 ``run_compaction`` 钩子；
    - ``message``：续跑活动文案（缺省 ``Agent 正在继续执行下一阶段``）。

    ``meta`` 字段：``stage`` / ``workflow_stage`` / ``output_file`` 与
    ``meta_extra`` 打底，回调各自的 ``attempt`` / ``max_retries`` /
    ``retry_attempts`` / ``validation_result`` / ``accepted_issues`` 覆盖其上。

    Returns:
        :class:`StageExecutionResult`；``stop`` 与超限异常直接上抛。
    """
    stage_index: Any = initial_stage_index
    stage = initial_stage
    stage_prompt = prompt
    # 易标 L911：策略在进入阶段循环前创建一次，仅在 stage 变化时重置。
    submission = submission_policy_factory()
    validation_result: Any = None
    candidate: Any = None
    accepted_issues: list = []
    accepted_submission_issues: list = []
    retry_attempts: list = []
    prompt_calls = 0
    turns = 0

    def base_meta() -> dict:
        base = dict(meta_extra or {})
        base.update({"stage": stage_index, "workflow_stage": stage,
                     "output_file": output_file})
        return base

    def emit_activity(event: dict) -> None:
        if on_activity is None:
            return
        try:
            on_activity(event)
        except Exception:  # noqa: BLE001 —— 易标 touchActivity 的 try{}catch{}
            logger.debug("on_activity 观测回调异常（已忽略）", exc_info=True)

    def emit_event(event: dict) -> None:
        if on_event is None:
            return
        try:
            on_event(event)
        except Exception:  # noqa: BLE001 —— 易标 emitMonitorEvent 的 try{}catch{}
            logger.debug("on_event 观测回调异常（已忽略）", exc_info=True)

    def record_retry(message: Any, output_content: Any, retry_prompt: str,
                     maximum: int | None, kind: str) -> None:
        """易标 ``recordRetry``（L1050–1071）：摘要 + 换提示词 + 观测事件。"""
        nonlocal stage_prompt
        attempt = len(retry_attempts) + 1
        retry_attempts.append({
            "attempt": attempt,
            "at": datetime.now().isoformat(timespec="seconds"),
            "error": _compact_text(message, 600),
            "output_chars": len(str(output_content or "")),
            "kind": kind,
            "prompt": retry_prompt,
        })
        stage_prompt = retry_prompt
        activity = {
            "kind": "retry",
            "stage_index": stage_index,
            "workflow_stage": stage,
            "attempt": attempt,
            "message": f"{runtime_name} 正在自动修复：{_compact_text(message, 160)}",
        }
        if maximum is not None:
            activity["maximum"] = maximum  # 易标 L1065 条件展开
        emit_activity(activity)
        event = {
            "type": "retry",
            "attempt": attempt,
            "message": _compact_text(message, 600),
            "prompt": stage_prompt,
        }
        if maximum is not None:
            event["maximum"] = maximum  # 易标 L1066 条件展开
        emit_event(event)

    while True:  # ── 外层：阶段循环（易标 L1073）─────────────────────────
        candidate = None
        accepted_issues = []
        # 执行异常只使用原 max_retries；提交策略在同一业务阶段的续接中保持（L1076–1077）。
        execution_retries = 0

        while True:  # ── 内层：执行重试循环（易标 L1078）─────────────────
            try:
                prompt_calls += 1
                prompt_result = await _maybe_await(prompt_once(stage_prompt))
                turns += _extract_turns(prompt_result)
                checked = await _maybe_await(
                    check_submission(submission.attempts + 1))
                candidate, report, restored = checked
            except Exception as error:  # 易标 L1105 catch —— 执行层错误
                if execution_retries >= max_retries:
                    raise  # 超限上抛原始异常（L1107）
                execution_retries += 1
                retry_prompt = None
                if build_retry_prompt is not None:
                    meta = base_meta()
                    meta.update({
                        "attempt": execution_retries,
                        "max_retries": max_retries,
                        "retry_attempts": [dict(item) for item in retry_attempts],
                    })
                    retry_prompt = await _maybe_await(build_retry_prompt(
                        {"kind": "execution", "error": error}, meta))
                if retry_prompt is None:
                    raise  # 不可修复：直接上抛（L1117 null 判定）
                output_content = (""
                                  if read_output is None
                                  else await _maybe_await(read_output()))
                record_retry(str(error), output_content, retry_prompt,
                             max_retries, "execution")
                continue

            # 预期产物问题由报告决定；停止决策在执行 catch 之外，不消耗执行重试预算（L1123）。
            decision = submission.inspect(report)
            if decision.get("action") == "stop":
                blocking = [issue for issue in report["issues"]
                            if issue.get("severity") == "blocking"]
                message = STOP_BLOCKING_PREFIX + "\n" + "\n".join(
                    str(issue.get("message", "")) for issue in blocking)
                raise StageStoppedError(message, report["issues"])  # L1127–1130

            if decision.get("action") == "accept":
                validation_result = report.get("value")
                accepted_issues = report.get("issues") or []
                if accepted_issues:
                    accepted_submission_issues.append(
                        {"stage": stage, "issues": accepted_issues})
                    joined = "；".join(
                        str(issue.get("message", "")) for issue in accepted_issues)
                    emit_activity({
                        "kind": "submission.accepted",
                        "stage_index": stage_index,
                        "workflow_stage": stage,
                        "message": (
                            f"仍有 {len(accepted_issues)} 项质量问题，"
                            "保留当前结果继续后续流程："
                            f"{_compact_text(joined, 200)}"
                        ),
                    })
                break  # L1146

            # 修复分支（L1149–1161）：businessPrompt + restored 还原说明 → 提交修复提示
            if build_retry_prompt is not None:
                meta = base_meta()
                meta.update({
                    "attempt": decision["attempt"],
                    "retry_attempts": [dict(item) for item in retry_attempts],
                })
                business_prompt = await _maybe_await(build_retry_prompt(
                    {"kind": "submission", "report": report,
                     "mode": decision.get("mode")}, meta))
            else:
                business_prompt = _default_submission_business_prompt(output_file)
            restored_note = (""
                             if not restored
                             else RESTORED_NOTE_TEMPLATE.format(
                                 files="、".join(str(item) for item in restored)))
            repair_details = "\n".join(
                part for part in (restored_note, business_prompt) if part)
            retry_prompt = build_repair_prompt(report, decision, repair_details)
            record_retry("\n".join(str(issue.get("message", ""))
                                   for issue in report["issues"]),
                         _field(candidate, "output_content"), retry_prompt,
                         None, "submission")

        # ── continueTask 流转（易标 L1164–1231）───────────────────────────
        if continue_task is None:
            break
        completed_stage_index = stage_index
        completed_stage = stage
        meta = base_meta()
        meta.update({"validation_result": validation_result,
                     "accepted_issues": accepted_issues})
        continuation = await _maybe_await(continue_task(candidate, meta))
        if (not continuation or not isinstance(continuation, dict)
                or continuation.get("complete") is True
                or not continuation.get("prompt")):
            break  # L1172
        emit_event({
            "type": "task_output",
            "stage_index": completed_stage_index,
            "workflow_stage": completed_stage,
            "output_file": _field(candidate, "output_file") or output_file,
            "output_content": _field(candidate, "output_content") or "",
        })
        files = continuation.get("files")
        files = files if isinstance(files, list) else []
        prepare_files = list(continuation.get("prepare_output_files") or [])
        if files and write_files is not None:
            await _maybe_await(write_files(files, prepare_files))  # L1184–1186
        if prepare_output_files is not None:
            await _maybe_await(prepare_output_files(prepare_files))  # L1187
        stage_index = _advance_stage_index(stage_index, continuation)  # L1188–1190
        continuation_stage = (continuation.get("stage")
                              or f"workflow_stage_{stage_index}")
        if continuation_stage != completed_stage:
            submission = submission_policy_factory()  # L1193 stage 变化才重置
        stage = continuation_stage
        stage_prompt = continuation["prompt"]

        # await_before_prompt 先行启动（易标 L1197–1198 提前挂 catch），
        # 再做可选压缩（L1199），最后等待并原样上抛异常（L1200–1206）。
        raw_before = continuation.get("await_before_prompt")
        if callable(raw_before) and not inspect.isawaitable(raw_before):
            raw_before = raw_before()
        before_task = (asyncio.ensure_future(raw_before)
                       if inspect.isawaitable(raw_before) else None)
        if before_task is not None:
            before_task.add_done_callback(_consume_task_exception)
        if (continuation.get("compact_before_prompt") is True
                and run_compaction is not None):
            await _maybe_await(run_compaction(continuation_stage, continuation))
        if before_task is not None:
            await before_task

        emit_event({
            "type": "task_input",
            "stage_index": stage_index,
            "workflow_stage": stage,
            "prompt": stage_prompt,
            "files": [_file_entry(item) for item in files],
        })
        message = continuation.get("message")
        emit_activity({
            "kind": "workflow.continue",
            "stage_index": stage_index,
            "workflow_stage": stage,
            "message": message or "Agent 正在继续执行下一阶段",
            "visible": bool(message),
            "activity": True,
        })

    return StageExecutionResult(
        validation_result=validation_result,
        candidate=candidate,
        accepted_issues=accepted_issues,
        accepted_submission_issues=accepted_submission_issues,
        stage_index=stage_index,
        stage=stage,
        prompt_calls=prompt_calls,
        turns=turns,
        retry_attempts=retry_attempts,
        stop_reason=STOP_COMPLETED,
    )


__all__ = [
    "RESTORED_NOTE_TEMPLATE",
    "STOP_BLOCKING_PREFIX",
    "STOP_COMPLETED",
    "StageExecutionResult",
    "StageStoppedError",
    "run_stage",
]
