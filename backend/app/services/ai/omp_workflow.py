"""Oh My Pi (omp) 三环节统一编排层：``plan → execute → verify → fix`` 流水线。

阶段 3 的核心服务（router-free）。把 omp 的 Agent 能力收敛到一套可验证的状态机：

* **多 Agent 编排**：``BatchRequest`` 对齐 omp ``task`` 工具的 batch 形态
  ``{ context, tasks[] }``；平台侧按 ``task_id`` 拆成隔离工作区并发委派，
  与 omp 子代理 "isolated worktree / no merge conflicts between siblings" 同构。
* **状态与上下文交接**：``--no-session`` 下轮次间状态一律经工作区文件传递
  （``CONTEXT.md`` / ``PLAN.md`` / 产物文件）；公共 context 只注入摘要，
  长文本走文件 —— 对应 omp RPC ``maxFrameBytes=1MB`` 单帧上限与 compaction 语义。
* **自我纠错**：``classify_error`` 分类限流 / 超时 / JSON 损坏 / 工具失败，
  瞬态错误指数退避重试，可注入 ``fallback_fn`` 降级；``verify`` 失败进入
  定向 ``fix`` 轮；连续失败达阈值进入 ``paused`` 并标记人工介入片段
  （omp 的 ``agentPauseGate`` 是进程内 API，此处给出**平台侧等价**实现）。
* **恢复**：``resume_paused`` 只重跑被暂停的 task_id，实现精准中断/续跑。

⚠️ 分层约束：services 层禁止 import routers；落库 / SSE / 互斥守卫由 routers 层承担。
⚠️ ``executor_fn`` / ``verify_fn`` 可注入，故全套流水线无需真实 omp 二进制即可回归。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any

from app.services.ai.omp_safety import resolve_workspace_path
from app.services.ai.omp_task_store import cache_key as _cache_key_for

logger = logging.getLogger("ai_provider.omp_workflow")

#: 与 omp 分节索引同一口径：id 白名单防路径穿越。
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
#: 错误分类用词边界正则，避免把「42980 tokens」误判为 429（R19x 教训同族）。
_RATE_RE = re.compile(r"\b(?:429|rate[\s_-]?limit|quota|限流|额度)\b", re.I)
_TIMEOUT_RE = re.compile(r"(?:timeout|timed\s*out|超时|超过\s*\d+\s*s)", re.I)
_JSON_RE = re.compile(r"(?:json|解析失败|结构无法识别|decode)", re.I)
_TOOL_RE = re.compile(r"\b(?:tool|write|edit|read|grep|glob)\b|工具调用|工具执行", re.I)

_MAX_RETRIES = 2
_MANUAL_SNIPPET_CHARS = 500


def safe_task_id(task_id: str) -> str:
    """把 task_id 归一为可用于文件名的安全 ID；非法字符替换为下划线。

    ⚠️ 纯 ``.`` / ``..`` 也在字符白名单里，却会与 ``os.path.join(workspace_root, id)``
    构成路径穿越；此类 id 一律加前缀，绝不原样返回。
    """
    raw = str(task_id or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:128]
    if not cleaned or not _SAFE_ID_RE.fullmatch(cleaned) or cleaned.startswith("..") or set(cleaned) <= {"."}:
        cleaned = "t_" + cleaned.lstrip(".") or "task"
    return cleaned


def classify_error(exc: BaseException) -> str:
    """把工具/模型异常归为可处理类别（决定重试、降级还是暂停）。"""
    text = f"{type(exc).__name__}: {exc}"
    if _RATE_RE.search(text):
        return "rate_limited"
    if _TIMEOUT_RE.search(text) or isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if _JSON_RE.search(text) or isinstance(exc, json.JSONDecodeError):
        return "json_invalid"
    if _TOOL_RE.search(text):
        return "tool_failed"
    return "unknown"


@dataclass
class OmpTaskSpec:
    """批式委派中的单个子任务。"""

    task_id: str
    instruction: str
    output_file: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BatchRequest:
    """对齐 omp ``task`` batch 形态：``{ context, tasks[] }``。"""

    context: str
    tasks: list[OmpTaskSpec] = field(default_factory=list)

    def to_omp_task_batch(self) -> dict[str, Any]:
        """导出为 omp 子代理委派形态（供将来启用原生 ``task`` 工具时复用）。"""
        return {
            "context": self.context,
            "tasks": [
                {
                    "id": safe_task_id(t.task_id),
                    "prompt": t.instruction,
                    "output_file": t.output_file,
                }
                for t in self.tasks
            ],
        }


@dataclass
class TaskResult:
    """单个子任务的流水线结果。"""

    task_id: str
    status: str = "completed"          # completed / degraded / paused
    payload: str = ""
    attempts: int = 0
    fix_rounds: int = 0
    errors: list[str] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)
    phase: str = "done"
    workspace: str = ""
    manual_review: str = ""            # 需人工介入的异常片段


@dataclass
class WorkflowReport:
    """一次批式委派的汇总报告（可直接序列化给前端 / 落审计日志）。"""

    stage: str
    results: list[TaskResult] = field(default_factory=list)
    paused: list[dict[str, Any]] = field(default_factory=list)
    context_digest: str = ""
    compaction_applied: bool = False
    totals: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "compaction_applied": self.compaction_applied,
            "context_digest": self.context_digest,
            "totals": self.totals,
            "paused": self.paused,
            "results": [
                {
                    "task_id": r.task_id,
                    "status": r.status,
                    "attempts": r.attempts,
                    "fix_rounds": r.fix_rounds,
                    "phase": r.phase,
                    "errors": r.errors,
                    "plan": r.plan,
                    "payload": r.payload,
                    "manual_review": r.manual_review,
                }
                for r in self.results
            ],
        }


def _compact_text(text: str, limit: int) -> tuple[str, bool, str]:
    """compaction：超长上下文压缩为「首尾保留 + 摘要」，完整内容仍写文件。

    返回 ``(写入 CONTEXT.md 的文本, 是否触发压缩, 注入 prompt 的摘要)``。
    关键：注入单帧的永远只是摘要，正文一律经文件由 ``read`` 分段读取。
    """
    limit = max(1024, int(limit))
    if len(text) <= limit:
        digest = text[:800]
        return text, False, digest
    head = text[: limit // 2]
    tail = text[-(limit // 2) :]
    compacted = "\n".join(
        [
            head,
            "",
            "<!-- 上下文已压缩：中段略去，请用 read 分段读取原始文件；"
            "本文件的首尾与下列要点构成完整工作约定 -->",
            "",
            tail,
        ]
    )
    digest = (
        f"上下文过长（{len(text)} 字 > {limit}），已触发压缩；"
        "完整约定见 CONTEXT.md，请用 read 分段读取。要点：" + text[:400]
    )
    return compacted, True, digest


def _write_file(ws: str, rel_path: str, text: str) -> str:
    """在工作区内写文件；拒绝越界路径（防 Agent 或脏 id 逃逸）。

    ✅ R235（2026-11-13）：路径判据收敛到 ``omp_safety.resolve_workspace_path``
    **单一事实源**（此前本函数与 ``_read_output`` 各持一份字符串判据 → 同族漏改
    风险），并补 realpath 兜底（符号链接逃逸）。既有语义保留：前导 ``/`` 视为
    工作区相对路径。
    """
    fp = resolve_workspace_path(ws, rel_path)
    os.makedirs(os.path.dirname(fp) or ws, exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(text)
    return fp


def _read_output(ws: str, rel_path: str) -> str:
    """读取工作区内的产物文件；越界路径一律拒绝（与写路径同判据）。"""
    fp = resolve_workspace_path(ws, rel_path)
    if not os.path.isfile(fp):
        return ""
    with open(fp, "r", encoding="utf-8") as f:
        return f.read()


async def default_omp_executor(spec: OmpTaskSpec, prompt: str, workspace: str) -> str:
    """默认执行器：拉起一个 omp RPC 会话，执行 prompt 后从工作区读回产物。

    三道安全闸门复用 ``OmpTaskAgent.check_ready()`` 单一事实源；
    ``try/finally`` 保证任何异常都回收子进程（不堆积常驻 omp 进程）。
    """
    from ...config import settings
    from .omp_rpc import OmpRpcSession
    from .omp_task_agent import OmpTaskAgent

    OmpTaskAgent().check_ready()
    role = spec.metadata.get("role")
    # ✅ 调用方传入的 system_prompt 必须真正下传，否则 generate_sections 的
    #    system_prompt 参数只是死旋钮；与角色 system prompt 叠加而非覆盖，
    #    保证事实铁律 / 工作区红线等硬约束不会被自定义指令冲掉。
    base_prompt = (role.system_prompt if role is not None else "") or ""
    override = str(spec.metadata.get("system_prompt") or "").strip()
    merged_prompt = "\n\n".join(p for p in (base_prompt, override) if p) or None
    session = OmpRpcSession(
        settings.omp_binary_path,
        settings.omp_task_model,
        "",
        workspace=workspace,
        tools=(role.tools if role is not None else settings.omp_task_tools),
        rpc_timeout=settings.omp_task_timeout,
        system_prompt=merged_prompt,
    )
    try:
        await session.start()
        await session.run_prompt(prompt)
        if spec.output_file:
            return _read_output(workspace, spec.output_file)
        return ""
    finally:
        await session.stop(cleanup_workspace=False)


async def platform_executor(
    spec: OmpTaskSpec,
    prompt: str,
    workspace: str,
    *,
    chat_fn=None,
) -> str:
    """本地编排档执行器（路径 B 的「本地 LLM」执行器 —— **不依赖 omp 进程**）。

    与真 omp（:func:`default_omp_executor`）的**语义差别**：

    * 真 omp 有**文件工具语义**（自己 read CONTEXT.md / PLAN.md、自己 write 产物）；
      本地档**没有** ⇒ 工作区交接文件的内容**内联进 prompt**（否则模型看不到上下文）；
    * 产物仍写回工作区的 ``spec.output_file``（路径守卫复用 ``_write_file``）
      ⇒ 编排器的「产物回收 / verify / fix」链路与真 omp 档**完全一致**；
    * 角色 system_prompt（事实铁律 / 工作区红线）与自定义 system_prompt **叠加**，
      与真 omp 档同口径（自定义指令不得冲掉硬约束）。

    ⚠️ 场景打标 ``scene="omp_local_executor"``（已登记 ``provider_factory.KNOWN_SCENES``），
    否则「场景模型路由」对它静默失效（AGENTS §4.5 铁律）。
    """
    role = spec.metadata.get("role")
    base_prompt = (role.system_prompt if role is not None else "") or ""
    override = str(spec.metadata.get("system_prompt") or "").strip()
    system_prompt = "\n\n".join(p for p in (base_prompt, override) if p) or None

    context_text = _read_output(workspace, "CONTEXT.md")
    plan_text = _read_output(workspace, "PLAN.md")
    blocks = [prompt]
    if plan_text:
        blocks.append("【PLAN.md】\n" + plan_text)
    if context_text:
        blocks.append("【CONTEXT.md】\n" + context_text)
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": "\n\n".join(blocks)})

    fn = chat_fn
    if fn is None:
        from .provider_factory import chat_with_fallback

        fn = chat_with_fallback
    text = await fn(messages, scene="omp_local_executor")
    text = text if isinstance(text, str) else str(text or "")
    if spec.output_file and text:
        _write_file(workspace, spec.output_file, text)
    return text


class OmpBatchOrchestrator:
    """批式、后台、结构化子 Agent 委派 + 四阶段流水线 + 自我纠错。

    ⚠️ **输入指纹缓存**（R236）：传入 ``store``（``omp_task_store.OmpTaskStore``）
    后，编排器按「方案 id + 阶段 + 输入摘要」派生缓存键：

    * 命中且产物非空 ⇒ **直接复用**产物（``phase="cache_hit"``），**零 AI 调用**；
    * 未命中 ⇒ 正常执行，完成后把产物写回缓存（下次同输入即命中）。

    ⚠️ ``store=None``（缺省）⇒ **完全不做缓存**，行为与引入前逐字节一致。
    """

    def __init__(
        self,
        *,
        stage: str = "generic",
        executor_fn=None,
        fallback_fn=None,
        verify_fn=None,
        max_concurrency: int = 4,
        max_fix_rounds: int = 2,
        pause_after_failures: int = 3,
        retry_backoff: float = 0.0,
        context_limit: int = 200_000,
        workspace_root: str | None = None,
        keep_workspace: bool = False,
        store=None,
        scheme_id: str = "",
    ):
        self.stage = stage
        self._executor_fn = executor_fn
        self._fallback_fn = fallback_fn
        self._verify_fn = verify_fn
        self.max_concurrency = max(1, int(max_concurrency))
        self.max_fix_rounds = max(0, int(max_fix_rounds))
        self.pause_after_failures = max(1, int(pause_after_failures))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self.context_limit = int(context_limit)
        self.workspace_root = workspace_root
        self.keep_workspace = bool(keep_workspace)
        self._store = store
        self.scheme_id = str(scheme_id or "")

    # ---------- 输入指纹缓存（R236） ----------
    def _cache_key(self, spec: OmpTaskSpec, digest: str) -> str:
        """派生缓存键；未接 store ⇒ 空串（表示「不做缓存」）。"""
        if self._store is None:
            return ""
        return _cache_key_for(
            scheme_id=self.scheme_id,
            stage=self.stage,
            inputs=f"{digest}|{spec.task_id}|{spec.output_file}|{spec.instruction}",
        )

    # ---------- 公开入口 ----------
    async def run_batch(self, request: BatchRequest) -> WorkflowReport:
        """并发执行 ``{context, tasks[]}``，逐任务跑完 plan→execute→verify→fix。"""
        compacted, applied, digest = _compact_text(request.context or "", self.context_limit)
        sem = asyncio.Semaphore(self.max_concurrency)

        async def _one(spec: OmpTaskSpec) -> TaskResult:
            async with sem:  # 并发上限：避免 15~30 路子 Agent 同时冷启动撞 429
                return await self._run_one(spec, compacted, digest)

        raw = await asyncio.gather(
            *(_one(s) for s in request.tasks), return_exceptions=True
        )
        results: list[TaskResult] = []
        for spec, item in zip(request.tasks, raw):
            if isinstance(item, BaseException):
                results.append(
                    TaskResult(
                        task_id=spec.task_id,
                        status="paused",
                        phase="orchestration_error",
                        errors=[f"[{classify_error(item)}] {item}"],
                        manual_review=str(item)[:_MANUAL_SNIPPET_CHARS],
                    )
                )
            else:
                results.append(item)

        paused = [
            {
                "task_id": r.task_id,
                "reason": "；".join(r.errors[-3:]),
                "snippet": r.manual_review,
                "phase": r.phase,
            }
            for r in results
            if r.status == "paused"
        ]
        totals = {
            "total": len(results),
            "completed": sum(1 for r in results if r.status == "completed"),
            "degraded": sum(1 for r in results if r.status == "degraded"),
            "paused": len(paused),
            "fix_rounds": sum(r.fix_rounds for r in results),
            "attempts": sum(r.attempts for r in results),
        }
        # 自建的临时工作区按 keep_workspace 口径回收；调用方显式传入
        # workspace_root 时由调用方负责回收（避免误删其固定目录）。
        if not self.keep_workspace and not self.workspace_root:
            import shutil

            for r in results:
                if r.workspace:
                    shutil.rmtree(r.workspace, ignore_errors=True)
        return WorkflowReport(
            stage=self.stage,
            results=results,
            paused=paused,
            context_digest=digest,
            compaction_applied=applied,
            totals=totals,
        )

    async def resume_paused(self, report: WorkflowReport, request: BatchRequest) -> WorkflowReport:
        """只重跑被暂停的 task_id（对应 ``/pause`` 后的精准恢复）。"""
        ids = {p["task_id"] for p in report.paused}
        subset = BatchRequest(
            context=request.context,
            tasks=[t for t in request.tasks if t.task_id in ids],
        )
        return await self.run_batch(subset)

    # ---------- 单任务流水线 ----------
    def _prepare_workspace(self, spec: OmpTaskSpec, compacted: str, plan_text: str) -> str:
        sid = safe_task_id(spec.task_id)
        ws = (
            os.path.join(self.workspace_root, sid)
            if self.workspace_root
            else tempfile.mkdtemp(prefix="omp_wf_")
        )
        os.makedirs(ws, exist_ok=True)
        # 公共 context 与结构化计划一律经文件交接，不进 prompt 单帧。
        _write_file(ws, "CONTEXT.md", compacted)
        _write_file(ws, "PLAN.md", plan_text)
        return ws

    def _make_plan(self, spec: OmpTaskSpec) -> tuple[dict[str, Any], str]:
        """plan 阶段：生成结构化计划（标题 / 步骤 / 文件 / 验收标准）。"""
        raw = spec.metadata.get("plan") or ""
        if isinstance(raw, dict):
            plan = dict(raw)
            lines = [f"# 计划：{plan.get('title', spec.task_id)}", ""]
            for key, label in (("steps", "步骤"), ("files", "涉及文件"), ("acceptance", "验收标准")):
                vals = plan.get(key) or []
                if vals:
                    lines += ["", f"## {label}"] + [f"- {v}" for v in vals]
            plan_text = "\n".join(lines) + "\n"
        else:
            plan_text = str(raw)
            plan = {"title": spec.task_id, "source": "PLAN.md"}
        return plan, plan_text

    async def _exec(self, spec: OmpTaskSpec, prompt: str, ws: str) -> str:
        fn = self._executor_fn or default_omp_executor
        payload = await fn(spec, prompt, ws)
        payload = payload if isinstance(payload, str) else str(payload or "")
        if not payload and spec.output_file:
            payload = _read_output(ws, spec.output_file)
        return payload

    async def _verify(self, spec: OmpTaskSpec, payload: str) -> list[str]:
        if self._verify_fn is None:
            return []
        out = self._verify_fn(spec, payload)
        if inspect.isawaitable(out):
            out = await out
        return list(out or [])

    async def _backoff(self, retry: int) -> None:
        if self.retry_backoff > 0:
            await asyncio.sleep(self.retry_backoff * (2**retry))

    async def _run_one(self, spec: OmpTaskSpec, compacted: str, digest: str) -> TaskResult:
        plan, plan_text = self._make_plan(spec)
        # ---------- 输入指纹缓存命中 ⇒ 零 AI 调用直接复用（R236） ----------
        cache_key = self._cache_key(spec, digest)
        if cache_key:
            cached = self._store.cached_artifact(cache_key) if self._store else None
            if cached and str(cached.get("payload") or ""):
                return TaskResult(
                    task_id=spec.task_id,
                    status="completed",
                    payload=str(cached.get("payload") or ""),
                    attempts=0,
                    fix_rounds=0,
                    plan=plan,
                    phase="cache_hit",
                    workspace="",
                )
        ws = self._prepare_workspace(spec, compacted, plan_text)
        if cache_key and self._store is not None:
            self._store.save(
                cache_key,
                {"status": "running", "task_id": spec.task_id, "stage": self.stage},
            )
        prompts = spec.metadata.get("prompts") or {}
        execute_prompt = prompts.get("execute") or spec.instruction
        fix_template = prompts.get("fix") or "【Fix】后端校验不通过：{issues}；请定向修复后重写产物文件。"
        # 每轮 prompt 自包含：显式要求读 PLAN.md / CONTEXT.md（--no-session 无对话记忆）。
        base_prompt = "\n".join(
            [
                "先读 PLAN.md（结构化计划）与 CONTEXT.md（公共上下文，必要时分段 read）。",
                f"上下文摘要：{digest}",
                "",
                execute_prompt,
            ]
        )
        prompt = base_prompt
        errors: list[str] = []
        attempts = 0
        fix_rounds = 0
        payload = ""
        phase = "plan"

        for _ in range(self.max_fix_rounds + 1):
            phase = "execute"
            payload = ""
            for retry in range(_MAX_RETRIES + 1):
                attempts += 1
                try:
                    payload = await self._exec(spec, prompt, ws)
                    break
                except Exception as e:  # noqa: BLE001 — 分类后决定是否重试/降级/暂停
                    kind = classify_error(e)
                    errors.append(f"[{kind}] {e}")
                    if kind in ("rate_limited", "timeout") and retry < _MAX_RETRIES:
                        await self._backoff(retry)
                        continue
                    break
            if not payload and self._fallback_fn is not None:
                phase = "fallback"
                try:
                    out = self._fallback_fn(spec, prompt, ws)
                    payload = (await out) if inspect.isawaitable(out) else str(out or "")
                    errors.append("[fallback] 已降级到备用执行器")
                except Exception as e:  # noqa: BLE001
                    errors.append(f"[fallback_failed] {e}")
            if not payload:
                phase = "execute_failed"
                break

            phase = "verify"
            issues = await self._verify(spec, payload)
            if not issues:
                self._save_cache(cache_key, spec, payload, "completed")
                return TaskResult(
                    task_id=spec.task_id,
                    status="completed",
                    payload=payload,
                    attempts=attempts,
                    fix_rounds=fix_rounds,
                    errors=errors,
                    plan=plan,
                    phase="done",
                    workspace=ws,
                )
            errors.extend(f"[verify] {i}" for i in issues[:8])
            if fix_rounds >= self.max_fix_rounds:
                phase = "verify_failed"
                break
            fix_rounds += 1
            phase = "fix"
            prompt = "\n".join(
                [
                    fix_template.replace("{issues}", "；".join(issues[:8])),
                    "",
                    "先读 PLAN.md / CONTEXT.md 与当前产物，只做定向修复，不要推倒重写。",
                    "",
                    spec.instruction,
                ]
            )

        # 连续多次修复失败 / 执行失败 → 暂停并标记人工介入片段。
        should_pause = phase in ("verify_failed", "execute_failed") or len(errors) >= self.pause_after_failures
        self._save_cache(
            cache_key,
            spec,
            payload,
            "paused" if should_pause else "degraded",
        )
        return TaskResult(
            task_id=spec.task_id,
            status="paused" if should_pause else "degraded",
            payload=payload,
            attempts=attempts,
            fix_rounds=fix_rounds,
            errors=errors,
            plan=plan,
            phase=phase,
            workspace=ws,
            manual_review=(payload or "；".join(errors[-3:]))[:_MANUAL_SNIPPET_CHARS],
        )

    def _save_cache(self, cache_key: str, spec: OmpTaskSpec, payload: str, status: str) -> None:
        """把任务状态/产物写入指纹缓存（未接 store ⇒ no-op；写失败不抛）。"""
        if not cache_key or self._store is None:
            return
        try:
            self._store.save(
                cache_key,
                {
                    "status": status,
                    "task_id": spec.task_id,
                    "stage": self.stage,
                    "artifact": {"payload": payload or ""},
                },
            )
        except Exception as exc:  # noqa: BLE001 — 缓存是增强，失败不阻断编排
            logger.warning("omp 指纹缓存写入失败（忽略）：%s", exc)


def per_id_conflict_directives(report: WorkflowReport) -> list[dict[str, Any]]:
    """按 task_id 归集冲突/暂停指令（平台侧等价于 omp ``conflict://N`` / ``conflict://*``）。

    omp 的 ``conflict://`` 解决的是 **git 合并冲突**；这里的「并发写冲突」由
    「每任务独占输出文件 + 按 id 归集」从根上避免，本函数只产出人工介入清单。
    """
    return [
        {
            "id": r.task_id,
            "directive": "manual_review" if r.status == "paused" else "accepted",
            "reason": "；".join(r.errors[-2:]),
            "snippet": r.manual_review,
        }
        for r in report.results
        if r.status != "completed"
    ]
