"""Oh My Pi (omp) 阶段 3 —— 审核与修复 Agent（全量审计 + 结构化闭环）。

用户规格落地：

* 角色 = 「严苛的标书质量审核专家」；
* 全量审计：``scan_documents`` 用确定性判据（编号连贯 / 图表引用 / 未闭合围栏 /
  排版跳级 / 占位符）扫全部 Markdown，Agent 用 ``read``/``grep``/``glob`` 交叉复核；
* 结构化修复：**不重写全文**，只产出精确 ``old_text``/``new_text`` 指令
  （``FIXES.json``），由后端 ``apply_fix_directives`` 唯一命中等价替换；
* 兜底：连续多轮 fix 失败 → 编排层 ``paused`` + 标记需人工介入的异常片段
  （omp ``agentPauseGate`` 的平台侧等价）；
* 输出：修订后正文 + 完整审核与修复报告 ``AUDIT_REPORT.md``。

⚠️ 不落库、不改 ``consistency_fullbook_repair`` 四道闸门、不碰既有 ``/repair``；
   要写库时由 routers 层复用既有 ``save_repair`` / ``confirm`` / ``rollback``。
⚠️ 分层约束：services 层禁止 import routers。
"""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import dataclass, field

from app.services.ai import omp_agent_roles as roles
from app.services.ai.omp_task_agent import OmpTaskAgent
from app.services.ai.omp_workflow import (
    BatchRequest,
    OmpBatchOrchestrator,
    OmpTaskSpec,
    WorkflowReport,
    per_id_conflict_directives,
    safe_task_id,
)

logger = logging.getLogger("ai_provider.omp_audit")


@dataclass
class AuditResult:
    """审核与修复闭环的完整产出（未落库）。"""

    findings: list[dict] = field(default_factory=list)
    fixes: list[dict] = field(default_factory=list)
    revised_documents: dict[str, str] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    report: WorkflowReport | None = None
    report_md: str = ""
    manual_review: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "findings": self.findings,
            "fixes": self.fixes,
            "failures": self.failures,
            "manual_review": self.manual_review,
            "report": self.report.to_dict() if self.report else {},
            "audit_report_md": self.report_md,
            "revised_documents": self.revised_documents,
        }


def _parse_fixes(payload: str) -> list[dict]:
    """从 FIXES.json 文本提取修复指令（兼容带围栏 / 顶层数组两种形态）。"""
    ok, _err, data = roles.check_json(payload)
    if not ok:
        return []
    if isinstance(data, dict):
        fixes = data.get("fixes") or []
    elif isinstance(data, list):
        fixes = data
    else:
        return []
    return [f for f in fixes if isinstance(f, dict)]


def _build_report_md(
    *, findings: list[dict], fixes: list[dict], failures: list[str],
    manual: list[dict], totals: dict
) -> str:
    """生成完整审核与修复报告（同时是写回工作区的 AUDIT_REPORT.md 契约）。"""
    lines = [
        "# 审核与修复报告（omp 主 Agent）",
        "",
        f"- 程序侧全量审计发现：{len(findings)} 项",
        f"- 结构化修复指令：{len(fixes)} 条",
        f"- 编排统计：{json.dumps(totals, ensure_ascii=False)}",
        "",
        "## 一、全量审计发现",
    ]
    if findings:
        for i, f in enumerate(findings, 1):
            lines.append(f"{i}. [{f.get('type')}] {f.get('section_id')}：{f.get('detail')}")
    else:
        lines.append("（无）")
    lines += ["", "## 二、结构化修复指令（old_text → new_text）"]
    if fixes:
        for i, f in enumerate(fixes, 1):
            lines.append(
                f"{i}. {f.get('section_id')}｜{f.get('reason')}｜"
                f"{str(f.get('old_text'))[:60]} → {str(f.get('new_text'))[:60]}"
            )
    else:
        lines.append("（无）")
    lines += ["", "## 三、修复失败与需人工介入片段"]
    if failures or manual:
        lines += [f"- 失败：{x}" for x in failures]
        lines += [
            f"- 暂停任务 {m.get('id')}：{m.get('reason')}；片段：{str(m.get('snippet'))[:120]}"
            for m in manual
        ]
    else:
        lines.append("（无，全部修复指令已精确命中）")
    lines += [
        "",
        "## 四、验收口径",
        "编号唯一性与点分链、图表登记、导出前预检仍由平台既有闸门单一持有；",
        "本报告只记录 omp Agent 的发现与最小改写，不替代任何落库闸门。",
        "",
    ]
    return "\n".join(lines)


class OmpAuditAgent(OmpTaskAgent):
    """整卷审计 + 精确修复指令 + 暂停兜底。"""

    name = "omp_audit"

    async def audit(
        self,
        *,
        documents: dict[str, str],
        facts: str = "",
        executor_fn=None,
        max_concurrency: int | None = None,
        apply: bool = True,
    ) -> AuditResult:
        """执行审核闭环；``apply=False`` 时只产指令不改写（dry-run）。"""
        from app.config import settings

        if executor_fn is None:
            self._assert_ready()  # noqa: SLF001 — 复用三道闸门单一事实源

        docs = {str(k): str(v or "") for k, v in (documents or {}).items()}
        findings = roles.scan_documents(docs)
        prompts = roles.build_audit_prompts(document_count=len(docs))
        context = "\n\n".join(
            [
                "# global-facts.md（唯一权威事实）",
                facts or "（无全局事实）",
                "# 程序侧全量审计发现（Agent 需 read/grep 交叉复核）",
                "\n".join(
                    f"- [{f.get('type')}] {f.get('section_id')}：{f.get('detail')}"
                    for f in findings
                )
                or "（无）",
            ]
        )

        tasks: list[OmpTaskSpec] = []
        for sid, text in docs.items():
            safe = safe_task_id(sid)
            plan = {
                "title": f"审计章节 {sid}",
                "steps": [
                    "用 read 分段读取本章正文与 CONTEXT.md",
                    "用 grep/glob 跨节复核编号、图表引用、单位与术语",
                    "产出 FIXES.json（精确 old_text/new_text）与 AUDIT_REPORT.md",
                ],
                "files": [f"sections/{safe}.md", "FIXES.json", "AUDIT_REPORT.md"],
                "acceptance": list(roles.AUDIT_ROLE.acceptance),
            }
            tasks.append(
                OmpTaskSpec(
                    task_id=safe,
                    instruction=(
                        f"审计章节 {sid}（共 {len(text)} 字）。只针对 **{sid}** 产出修复指令，"
                        "section_id 必须等于该 id，禁止重写整章。"
                    ),
                    output_file="FIXES.json",
                    metadata={
                        "role": roles.AUDIT_ROLE,
                        "plan": plan,
                        "prompts": prompts,
                        "document": text,
                    },
                )
            )
            # 每任务独占工作区：把该章正文预写为只读索引，供 read/grep 使用。
            # 由 _prepare 钩子在编排器建好工作区后补写（见 _write_section_index）。

        # ✅ R232（2026-11-13）：``docs`` 以**原始** section id 为键，而 task_id 已过
        #    ``safe_task_id`` 白名单——非 ASCII id（如中文标题型 id）经白名单替换成
        #    下划线后，旧 ``docs.get(safe)`` 恒取到空文档，且回包 section_id（指令
        #    要求用原始 id）不在 ``{safe: ""}`` 索引中 ⇒ verify 恒失败 → 任务恒
        #    paused（fix 轮白烧后仍判失败）。此处反查回原始 id，使 verify 索引与
        #    回包契约对齐（ASCII id 下两键相同，行为逐字节不变）。
        raw_by_safe = {safe_task_id(k): k for k in docs}

        def _verify(spec: OmpTaskSpec, payload: str) -> list[str]:
            raw = raw_by_safe.get(safe_task_id(spec.task_id), "")
            return roles.verify_audit_fixes(payload, {raw: docs.get(raw, "")})

        # ⚠️ 固定工作区根目录：预写的只读分节索引与执行期必须落在同一目录。
        #    若留空，_prepare_workspace 每次都新建临时目录，预写索引会失效。
        workspace_root = tempfile.mkdtemp(prefix="omp_audit_")
        orchestrator = OmpBatchOrchestrator(
            stage="audit",
            executor_fn=executor_fn,
            verify_fn=_verify,
            workspace_root=workspace_root,
            max_concurrency=max_concurrency or int(getattr(settings, "omp_workflow_max_concurrency", 4)),
            max_fix_rounds=int(getattr(settings, "omp_workflow_max_fix_rounds", 2)),
            pause_after_failures=int(getattr(settings, "omp_workflow_pause_after_failures", 3)),
            context_limit=int(getattr(settings, "omp_workflow_context_limit", 200_000)),
        )
        # 为每个任务预写只读分节索引（每任务独立目录，天然无并发写冲突）。
        try:
            self._write_section_index(orchestrator, tasks)
            report = await orchestrator.run_batch(BatchRequest(context=context, tasks=tasks))
        except BaseException:
            # RPC 超时 / 编排异常同样必须回收工作区，否则失败任务会持续堆积临时目录。
            self._cleanup_workspace(workspace_root, settings)
            raise

        fixes: list[dict] = []
        for r in report.results:
            if r.status == "paused":
                continue
            fixes.extend(_parse_fixes(r.payload))
        revised, failures = (
            roles.apply_fix_directives(docs, fixes) if apply else (dict(docs), [])
        )
        manual = per_id_conflict_directives(report)
        report_md = _build_report_md(
            findings=findings,
            fixes=fixes,
            failures=failures,
            manual=manual,
            totals=report.totals,
        )
        logger.info(
            "omp 审核闭环完成：发现 %d 项 / 指令 %d 条 / 应用失败 %d / 暂停 %d",
            len(findings), len(fixes), len(failures), len(report.paused),
        )
        result = AuditResult(
            findings=findings,
            fixes=fixes,
            revised_documents=revised,
            failures=failures,
            report=report,
            report_md=report_md,
            manual_review=manual,
        )
        self._cleanup_workspace(workspace_root, settings)
        return result

    @staticmethod
    def _cleanup_workspace(workspace_root: str, settings) -> None:  # noqa: ANN001
        """按 ``omp_task_keep_workspace`` 口径回收工作区（成功 / 异常同一出口）。"""
        if bool(getattr(settings, "omp_task_keep_workspace", False)):
            return
        import shutil

        shutil.rmtree(workspace_root, ignore_errors=True)

    @staticmethod
    def _write_section_index(orchestrator: OmpBatchOrchestrator, tasks: list[OmpTaskSpec]) -> None:
        """在批式执行前把各章正文写到各自隔离工作区（避免共享可变文件）。

        复用编排器的 ``_prepare_workspace`` 同一路径计算；文件名经白名单过滤，
        防 section id 路径穿越。此方法只做**只读输入**预写，不承担任何判据。
        """
        from app.services.ai.omp_workflow import _write_file  # noqa: PLC2701

        for spec in tasks:
            sid = safe_task_id(spec.task_id)
            ws = orchestrator._prepare_workspace(spec, "", "")  # noqa: SLF001
            _write_file(ws, f"sections/{sid}.md", str(spec.metadata.get("document") or ""))
