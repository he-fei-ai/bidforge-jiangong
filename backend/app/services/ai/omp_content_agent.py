"""Oh My Pi (omp) 阶段 3 —— 正文生成 Agent（按 L4 叶子节点批式委派）。

用户规格落地：

* 角色 = 「资深工程技术方案撰写专家」；
* Plan：读目录树与全局事实，编排每个 L4 的写作要点 / 图表 / 知识库素材；
* Execute：按 L4 逐节生成，**每节独占一个隔离工作区与输出文件**
  （``sections/<section_id>.md``），由 ``task`` batch 形态并发处理；
* Verify：字数均分（L3 总字数 1200 / 4 个 L4 → 每节 300）、
  全局事实一致性、非法占位符、未闭合围栏；
* Fix：字数不足触发「扩展循环」，事实冲突触发「定向修复」。

⚠️ 不落库、不碰 ``sse_handlers`` 正文主链；产物由 routers 层决定如何写入
   （复用既有 ``update_section`` / checkpoint 单一写入口）。
⚠️ 分层约束：services 层禁止 import routers。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.services.ai import omp_agent_roles as roles
from app.services.ai.omp_task_agent import OmpTaskAgent
from app.services.ai.omp_workflow import (
    BatchRequest,
    OmpBatchOrchestrator,
    OmpTaskSpec,
    WorkflowReport,
    safe_task_id,
)
from app.services.content_utils import WORD_BUDGET_TIERS, text_word_count

logger = logging.getLogger("ai_provider.omp_content")


@dataclass
class ContentDraft:
    """一节正文的产物（未落库）。"""

    section_id: str
    title: str
    markdown: str = ""
    word_count: int = 0
    budget: int = 0
    status: str = "draft"
    errors: list[str] = field(default_factory=list)


@dataclass
class ContentBatchResult:
    """一次批式正文生成的汇总。"""

    drafts: list[ContentDraft] = field(default_factory=list)
    report: WorkflowReport | None = None
    facts: str = ""
    tier: str = "standard"

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "report": self.report.to_dict() if self.report else {},
            "drafts": [
                {
                    "section_id": d.section_id,
                    "title": d.title,
                    "word_count": d.word_count,
                    "budget": d.budget,
                    "status": d.status,
                    "errors": d.errors,
                    "markdown": d.markdown,
                }
                for d in self.drafts
            ],
        }


class OmpContentAgent(OmpTaskAgent):
    """按目录叶子节点批式生成正文 Markdown 文件集。"""

    name = "omp_content"

    async def generate_sections(
        self,
        *,
        leaves: list[dict],
        facts: str = "",
        tier: str = "standard",
        outline_digest: str = "",
        executor_fn=None,
        system_prompt: str | None = None,
        max_concurrency: int | None = None,
    ) -> ContentBatchResult:
        """生成正文；``executor_fn`` 注入时不拉起真实 omp 进程（便于回归）。"""
        from app.config import settings

        if executor_fn is None:
            self._assert_ready()  # noqa: SLF001 — 复用三道闸门单一事实源

        budget_total = WORD_BUDGET_TIERS.get(tier, WORD_BUDGET_TIERS["standard"])
        # 按 L3 父节点分组：同一章的总字数均分给其 L4 叶子。
        groups: dict[str, list[dict]] = {}
        for leaf in leaves or []:
            sid = str(leaf.get("id") or "").strip()
            if not sid:
                continue
            parent = str(leaf.get("parent_id") or leaf.get("parent") or "_root")
            groups.setdefault(parent, []).append(leaf)

        prompts = roles.build_content_prompts(
            outline_digest=outline_digest, facts=facts, tier=tier
        )
        context = "\n\n".join(
            [
                "# global-facts.md（唯一权威事实）",
                facts or "（无全局事实）",
                "# 目录树摘要 / 图表编号登记",
                outline_digest or "（无目录摘要）",
            ]
        )

        tasks: list[OmpTaskSpec] = []
        meta: dict[str, dict] = {}
        for _parent, group in groups.items():
            budgets = roles.allocate_leaf_budgets(budget_total, len(group))
            for leaf, budget in zip(group, budgets):
                # ✅ R232（2026-11-13）：叶子原始 id 与文件安全名分离——``safe_task_id``
                #    会把非 ASCII id 替换成下划线（如「基础工程-1」→「____-1」），
                #    旧版让 ContentDraft.section_id / documents 键直接携带安全名，
                #    下游（落库应用 / run_full_chain 的 missing_sections 比对，其
                #    expected_ids 用叶子原始 id）全都对不上号。产物文件与工作区
                #    仍用安全名（防路径穿越不变），只有回包身份用原始 id。
                raw_sid = str(leaf.get("id") or "").strip()
                sid = safe_task_id(raw_sid)
                title = str(leaf.get("title") or raw_sid or sid)
                plan = {
                    "title": f"撰写 L4 节点：{title}",
                    "steps": [
                        "用 read 读取 CONTEXT.md 中的全局事实与目录树摘要",
                        f"确定本章写作要点、需引用的图表编号（目标 {budget} 字）",
                        f"用 write 写出 sections/{sid}.md 并自检字数 / 事实 / 占位符",
                    ],
                    "files": ["CONTEXT.md", "PLAN.md", f"sections/{sid}.md"],
                    "acceptance": list(roles.CONTENT_ROLE.acceptance),
                }
                instruction = "\n".join(
                    [
                        f"当前 L4 节点：{title}（id={sid}）",
                        f"本章目标字数：{budget} 字（不得低于 {int(budget * roles.WORD_UNDER_RATIO)} 字）",
                        f"唯一产物：sections/{sid}.md；不要修改其它章节文件。",
                    ]
                )
                spec = OmpTaskSpec(
                    task_id=sid,
                    instruction=instruction,
                    output_file=f"sections/{sid}.md",
                    metadata={
                        "role": roles.CONTENT_ROLE,
                        "plan": plan,
                        "prompts": prompts,
                        "budget": budget,
                        "title": title,
                        "system_prompt": system_prompt,
                    },
                )
                tasks.append(spec)
                meta[sid] = {"title": title, "budget": budget, "raw_id": raw_sid or sid}

        def _verify(spec: OmpTaskSpec, payload: str) -> list[str]:
            return roles.verify_content_draft(
                payload,
                budget=int(spec.metadata.get("budget") or budget_total),
                facts=facts,
            )

        orchestrator = OmpBatchOrchestrator(
            stage="content",
            executor_fn=executor_fn,
            verify_fn=_verify,
            max_concurrency=max_concurrency or int(getattr(settings, "omp_workflow_max_concurrency", 4)),
            max_fix_rounds=int(getattr(settings, "omp_workflow_max_fix_rounds", 2)),
            pause_after_failures=int(getattr(settings, "omp_workflow_pause_after_failures", 3)),
            context_limit=int(getattr(settings, "omp_workflow_context_limit", 200_000)),
        )
        report = await orchestrator.run_batch(BatchRequest(context=context, tasks=tasks))

        drafts: list[ContentDraft] = []
        for r in report.results:
            m = meta.get(r.task_id, {})
            drafts.append(
                ContentDraft(
                    # ✅ R232：回包身份用叶子原始 id（缺省回退安全名），见上注。
                    section_id=str(m.get("raw_id") or r.task_id),
                    title=m.get("title", r.task_id),
                    markdown=r.payload,
                    word_count=text_word_count(r.payload),
                    budget=int(m.get("budget") or 0),
                    status=r.status,
                    errors=list(r.errors),
                )
            )
        logger.info(
            "omp 正文批式生成完成：%d 节 / 完成 %d / 暂停 %d",
            len(drafts),
            report.totals.get("completed", 0),
            report.totals.get("paused", 0),
        )
        return ContentBatchResult(drafts=drafts, report=report, facts=facts, tier=tier)
