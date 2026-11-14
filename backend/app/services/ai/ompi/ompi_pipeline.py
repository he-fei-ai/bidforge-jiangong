"""OMP 深度能力层编排入口：把 advisor / memory / hashline 接到既有编排路径。

本模块是 ``ompi_*`` 深度层的**组合根**——不重写既有 ``omp_workflow.OmpBatchOrchestrator``
（那是 R230~R233 已落地的成熟状态机），只做**加法式增强**：

* ``make_executor`` —— 包装既有执行器，产物落盘后取 hashline 锚（供后续 stale 安全修复）；
* ``make_verify`` —— 包装既有校验器，**叠加 AdvisorGuard 监督结果**（concern/blocker
  级 note 转成 verify issue → 编排器自动进入 fix 轮，主 Agent 在 fix prompt 里看到
  ``[advisor:blocker] …`` 从而纠偏——对应 omp advisor 旁观模型「注入 note」语义）；
* ``context_with_memory`` / ``retain_stage`` —— 记忆银行做**跨阶段上下文交接**
  （目录产出的约束/事实要点 retain，正文/审核 Agent 的 context 注入回忆要点）；
* ``stale_safe_fix`` —— 用 hashline 锚做 stale 安全的结构化修复（``apply_fix_directives``
  的深度层增强：锚不匹配判 stale 拒绝，防过期文件覆盖损坏）；
* ``run_stage`` —— 一次三环节之一的高层入口：建编排器 + 接线深度层 + 跑批 + 回收记忆。

对齐既有集成口径：全部经注入的 ``executor_fn``/``verify_fn`` 跑，无需真实 omp 二进制
即可回归；真实 omp 启用时只需换 executor_fn（``default_omp_executor``），深度层不变。

⚠️ 分层约束：services 层禁止 import routers；本模块零网络、零数据库访问。
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable

from .ompi_advisor import AdvisorGuard, AdvisorNote
from .ompi_fix import apply_hashline, hashline_anchor, is_hashline_anchor, stale_anchor_error
from .ompi_memory import MemoryBank
from .ompi_task_schema import pull_path, validate_yield

#: 可注入的执行器形态：``(spec, prompt, workspace) -> payload``（同步或异步）。
ExecutorFn = Callable[[Any, str, str], str | Awaitable[str]]
#: 可注入的校验器形态：``(spec, payload) -> list[str]``（同步或异步）。
VerifyFn = Callable[[Any, str], list[str] | Awaitable[list[str]]]


class OmpiPipeline:
    """深度能力层组合根（确定性、可注入、无副作用编排）。

    典型用法（三环节串联，上下文经记忆银行交接）：
        pipe = OmpiPipeline(memory_path="backend/data/ompi_memory.json")
        # ① 目录
        outline_ctx = pipe.context_with_memory(brief)
        outline_report = await pipe.run_stage(
            "outline", context=outline_ctx, tasks=outline_tasks,
            verify_fn=verify_outline, executor_fn=executor,
        )
        # ② 正文（注入目录要点 + 目录树摘要）
        content_ctx = pipe.context_with_memory(
            f"{outline_report.context_digest}\n{outline_digest}"
        )
        # ③ 审核（注入正文要点）
    """

    def __init__(
        self,
        *,
        memory_path: str | None = None,
        advisor_rules: list[tuple[str, Callable[[dict], str | None]]] | None = None,
        stage: str = "generic",
        yield_schema: dict | list | None = None,
    ):
        self.memory = MemoryBank(memory_path) if memory_path else None
        self.guard = AdvisorGuard(advisor_rules)
        self.stage = stage
        self.yield_schema = yield_schema

    # ---------- executor / verify 增强 ----------
    def make_executor(self, executor_fn: ExecutorFn) -> ExecutorFn:
        """包装执行器：产物写盘后取 hashline 锚，随 spec.metadata 携带。

        hashline 锚供 ``stale_safe_fix`` 使用：修复指令能校验「修复指令发出时的
        文档哈希 == 当前文档哈希」，不一致判 stale 拒绝。锚不改变产物内容。
        """

        async def _wrapped(spec: Any, prompt: str, workspace: str) -> str:
            payload = executor_fn(spec, prompt, workspace)
            if inspect.isawaitable(payload):
                payload = await payload
            text = payload if isinstance(payload, str) else str(payload or "")
            spec.metadata.setdefault("_hashline_anchor", hashline_anchor(text))
            return text

        return _wrapped

    def make_verify(self, verify_fn: VerifyFn | None) -> VerifyFn:
        """包装校验器：叠加 AdvisorGuard 监督 + task 扇出 schema 校验回收。

        * advisor note（concern/blocker 级）→ verify issue；
        * ``yield_schema`` 配置时，对能 ``json.loads`` 的 payload 跑
          ``validate_yield``（task 扇出结果的 schema 回收），缺键/类型不符 → verify issue。

        这些 issue 进入编排器 ``{issues}`` 传给 fix prompt；主 Agent 在下一轮看到
        ``[advisor:blocker] …`` / ``[schema] …`` 并据此纠偏或说明不改。
        """

        async def _wrapped(spec: Any, payload: str) -> list[str]:
            issues: list[str] = []
            if verify_fn is not None:
                out = verify_fn(spec, payload)
                if inspect.isawaitable(out):
                    out = await out
                issues.extend(str(i) for i in (out or []))
            # task 扇出 schema 校验回收：payload 是结构化结果文本时按契约校验。
            if self.yield_schema is not None and str(payload or "").strip():
                import json

                try:
                    data = json.loads(payload)
                except (ValueError, TypeError):
                    data = None
                if data is not None:
                    ok, errs = validate_yield(data, self.yield_schema)
                    if not ok:
                        issues.extend(f"[schema] {e}" for e in errs[:8])
            note = self.guard.review({"produced_text": payload, "payload": payload, "spec": spec})
            if note is not None and note.level in ("concern", "blocker"):
                issues.append(f"[advisor:{note.level}] {note.text}")
            return issues

        return _wrapped

    # ---------- 记忆银行：跨阶段上下文交接 ----------
    def context_with_memory(self, context: str) -> str:
        """把**最近沉淀**的记忆要点注入 context（跨阶段上下文交接）。

        注入「最近 active 要点」而非做语义匹配——记忆银行是项目作用域事实库，
        交接应把上一阶段的结构性要点（任务状态/修复轮数）前置给下一阶段，
        无论查询词是否能精确命中。``reflect`` 的语义检索语义保留在
        ``ompi_memory``，这里用 ``recall("")`` 取最新。
        """
        if not self.memory:
            return str(context or "")
        hits = self.memory.recall("", limit=6)
        if not hits:
            return str(context or "")
        lines = ["【记忆要点】"]
        for e in hits:
            tag = "、".join(e.tags[:4]) or e.scope
            lines.append(f"- [{tag}] {e.text}")
        return "\n".join(lines) + f"\n\n---\n{context}"

    def retain_stage(self, stage: str, report: Any, *, tags: list[str] | None = None) -> None:
        """把阶段产物的关键要点 retain 进记忆银行（供下一阶段 recall）。

        只沉淀**结构性要点**（任务状态、修复轮数、暂停项），不沉淀产物全文——
        产物经工作区文件交接，记忆只做轻量上下文。``report`` 是 WorkflowReport。
        """
        if not self.memory or report is None:
            return
        results = getattr(report, "results", None) or []
        completed = [r for r in results if getattr(r, "status", "") == "completed"]
        if not completed:
            return
        base_tags = [stage] + [str(t) for t in (tags or [])]
        self.memory.retain(
            f"阶段 {stage}：完成 {len(completed)}/{len(results)} 个任务，"
            f"修复轮共 {getattr(report, 'totals', {}).get('fix_rounds', 0)} 次",
            tags=base_tags + ["stage_progress"],
            source=f"ompi:{stage}",
        )

    # ---------- hashline stale 安全修复 ----------
    def stale_safe_fix(
        self, documents: dict[str, str], fixes: list[dict[str, Any]]
    ) -> tuple[dict[str, str], list[str]]:
        """用 hashline 锚做 stale 安全的结构化修复（``apply_fix_directives`` 深度增强）。

        每条 fix 可携带 ``anchor``（修复指令发出时的 ``hashline_anchor``）。提供锚时：
        - 当前文档哈希 == 锚哈希 → 应用修复（old_text/new_text 精确替换，恰 1 处）；
        - 不匹配 → 判 stale，跳过该条并记入 failures（不覆盖并发修改，宁漏勿误）。
        未提供锚时退化为纯精确替换（既有 ``apply_fix_directives`` 语义）。

        返回 ``(applied_documents, failures)``。
        """
        from ..omp_agent_roles import apply_fix_directives

        # 深度层增强：对带锚的 fix 做 stale 预检——锚不匹配判 stale，跳过该条并
        # 记入 failures（不覆盖并发修改，宁漏勿误）；通过或未带锚的交 apply_fix_directives
        # 完成精确替换。单次调用，无重复应用。
        # ✅ R234 加固：仅对**本层锚格式**（is_hashline_anchor）做 stale 校验；非本层锚
        #    直接交给 apply_fix_directives，不吞 ValueError（宁漏勿误，不掩盖异常）。
        safe: list[dict[str, Any]] = []
        stale_failures: list[str] = []
        for fix in fixes:
            anchor = fix.get("anchor")
            sid = str(fix.get("section_id") or "")
            doc = documents.get(sid)
            if anchor and doc is not None and is_hashline_anchor(str(anchor)):
                try:
                    apply_hashline(doc, str(anchor), doc, old_text=None)  # 仅校验 stale
                except stale_anchor_error as e:
                    stale_failures.append(f"{sid}: {e}")
                    continue
            safe.append(fix)
        applied, failures = apply_fix_directives(documents, safe)
        return applied, failures + stale_failures

    # ---------- 高层入口 ----------
    async def run_stage(
        self,
        stage: str,
        *,
        context: str,
        tasks: list[Any],
        verify_fn: VerifyFn | None = None,
        executor_fn: ExecutorFn | None = None,
        fallback_fn: ExecutorFn | None = None,
        max_concurrency: int = 4,
        max_fix_rounds: int = 2,
        pause_after_failures: int = 3,
        context_limit: int = 200_000,
        workspace_root: str | None = None,
        keep_workspace: bool = False,
    ) -> Any:
        """跑一次三环节之一：建编排器 + 接线深度层 + 跑批 + 回收记忆要点。

        返回 ``WorkflowReport``。``executor_fn`` 缺省时由编排器用
        ``default_omp_executor``（真实 omp 路径），本层不改变该默认。
        """
        from ..omp_workflow import BatchRequest, OmpBatchOrchestrator

        exec_fn = self.make_executor(executor_fn) if executor_fn is not None else executor_fn
        verify = self.make_verify(verify_fn)
        orch = OmpBatchOrchestrator(
            stage=stage,
            executor_fn=exec_fn,
            fallback_fn=fallback_fn,
            verify_fn=verify,
            max_concurrency=max_concurrency,
            max_fix_rounds=max_fix_rounds,
            pause_after_failures=pause_after_failures,
            context_limit=context_limit,
            workspace_root=workspace_root,
            keep_workspace=keep_workspace,
        )
        request = BatchRequest(context=context, tasks=list(tasks))
        report = await orch.run_batch(request)
        self.retain_stage(stage, report)
        return report


def format_advisor_notes(notes: list[AdvisorNote]) -> str:
    """便捷入口：把 note 列表格式化为注入下一轮 Prompt 的旁注块。"""
    return AdvisorGuard().format_notes(notes)


__all__ = [
    "OmpiPipeline",
    "format_advisor_notes",
    "pull_path",
    "validate_yield",
    "hashline_anchor",
    "apply_hashline",
    "stale_anchor_error",
]
