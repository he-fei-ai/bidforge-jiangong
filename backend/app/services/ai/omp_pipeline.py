"""Oh My Pi (omp) 阶段 3 —— 三环节全自动流水线装配（services 层，router-free）。

把「目录生成 → 正文生成 → 审核与修复」串成一条可验证的链路，并明确
**上下文交接契约**：

```
OUTLINE.json（目录树）
   └─ _flatten_leaves → leaves[{id,title,parent_id,level}]
        └─ OmpContentAgent → documents{section_id: markdown}
             └─ OmpAuditAgent → revised_documents + AUDIT_REPORT.md
                  └─ export_contract → 既有 POST /api/v1/schemes/{sid}/export/docx
```

⚠️ 本模块**不落库**、不改 SSE 主链、不碰既有 ``/repair``。产物只是结构化数据；
   落库由 routers 层复用 ``_save_outline_to_db`` / ``update_section`` /
   ``save_repair`` 等既有单一写入口完成。
⚠️ ``executor_fn`` 注入时不拉起真实 omp 二进制，整条链路可离线回归。
⚠️ 阶段 3 遗留项收口（2026-11-13）：
   - 前序阶段失败必须**阻断下游空跑**（不再把失败目录喂给正文生成）；
   - 顶层显式 ``status``（completed / degraded / blocked），对齐本仓
     ``partial / failed_* / degraded / undetermined`` 状态契约；
   - 缺章必须显式登记 ``missing_sections``，禁止静默丢章；
   - 导出契约不得把「未检查」渲染成「通过」：``checked=False`` +
     ``undetermined_reason``（对齐 R195 ``X-Export-Gate: unchecked`` 口径）。
"""

from __future__ import annotations

import logging

from app.services.ai import omp_agent_roles as roles
from app.services.ai.omp_audit_agent import OmpAuditAgent
from app.services.ai.omp_content_agent import OmpContentAgent
from app.services.ai.omp_workflow import BatchRequest, OmpBatchOrchestrator, OmpTaskSpec

logger = logging.getLogger("ai_provider.omp_pipeline")

#: DOCX 导出由既有端点承担（本模块只交付「可导出」的正文与前置检查结论）。
EXPORT_DELEGATE = "POST /api/v1/schemes/{scheme_id}/export/docx"
VALID_STAGES = ("outline", "content", "audit", "full")


def _setting(key: str, default):
    from app.config import settings

    return getattr(settings, key, default)


def _flatten_leaves(tree: list[dict]) -> list[dict]:
    """把嵌套目录树的**末级节点**展平为正文生成所需的 L4 任务列表。

    节点无显式 id 时按前序位置生成稳定 id；``parent_id`` 用于按 L3 分组均分字数。
    """
    out: list[dict] = []

    def walk(nodes, level: int, parent: str, prefix: str) -> None:
        for i, node in enumerate(nodes or []):
            if not isinstance(node, dict):
                continue
            # ⚠️ id 必须全局唯一：只用「同父内序号」会让不同 L3 下的 L4 撞 id，
            #    批式委派时两个任务写同一个输出文件 → 并发写冲突。
            sid = str(node.get("id") or f"{prefix}.{i + 1}")
            title = str(node.get("title") or sid)
            children = node.get("children") or []
            if children:
                walk(children, level + 1, sid, sid)
            else:
                out.append({"id": sid, "title": title, "parent_id": parent, "level": level})

    walk(tree, 1, "_root", "L1")
    return out


def verify_outline_payload(spec: OmpTaskSpec, payload: str) -> list[str]:
    """目录阶段的 ``verify``：``check_json`` 结构完整性 + 层级/数量约束。"""
    ok, err, data = roles.check_json(payload, required_keys=("sections",))
    if not ok:
        return [err]
    return roles.verify_outline(data.get("sections"))


def build_outline_batch(
    *, brief: str, project_type: str = "", industry: str = "", facts: str = ""
) -> BatchRequest:
    """构造目录环节的 batch 委派（单任务，但走同一编排器与同一流水线）。"""
    prompts = roles.build_outline_prompts(
        brief=brief, project_type=project_type, industry=industry
    )
    plan = {
        "title": "专项施工方案标准化目录树（L1~L4）",
        "steps": [
            "用 read 读取 CONTEXT.md 与 global-facts.md",
            "按九大要素规划 L1，逐层展开 L2/L3/L4 并核对数量约束",
            "用 write 写出 OUTLINE.json 并自检结构",
        ],
        "files": ["CONTEXT.md", "PLAN.md", "OUTLINE.json"],
        "acceptance": list(roles.OUTLINE_ROLE.acceptance),
    }
    spec = OmpTaskSpec(
        task_id="outline",
        instruction="生成符合 L1~L4 层级与数量约束的标准化目录树，唯一产物为 OUTLINE.json。",
        output_file="OUTLINE.json",
        metadata={"role": roles.OUTLINE_ROLE, "plan": plan, "prompts": prompts},
    )
    context = "\n\n".join(
        [
            "# global-facts.md（唯一权威事实）",
            facts or "（无全局事实）",
            "# 项目简报",
            brief,
            "# 项目类型 / 行业",
            f"{project_type or '未指定'} / {industry or '未指定'}",
        ]
    )
    return BatchRequest(context=context, tasks=[spec])


def _make_orchestrator(stage: str, *, executor_fn, verify_fn) -> OmpBatchOrchestrator:
    return OmpBatchOrchestrator(
        stage=stage,
        executor_fn=executor_fn,
        verify_fn=verify_fn,
        max_concurrency=int(_setting("omp_workflow_max_concurrency", 4)),
        max_fix_rounds=int(_setting("omp_workflow_max_fix_rounds", 2)),
        pause_after_failures=int(_setting("omp_workflow_pause_after_failures", 3)),
        context_limit=int(_setting("omp_workflow_context_limit", 200_000)),
    )


async def run_outline_stage(
    *, brief: str, project_type: str = "", industry: str = "", facts: str = "", executor_fn=None
) -> tuple[list[dict], dict]:
    """环节一：Plan / Execute / Verify / Fix → 标准化目录树。"""
    request = build_outline_batch(
        brief=brief, project_type=project_type, industry=industry, facts=facts
    )
    report = await _make_orchestrator(
        "outline", executor_fn=executor_fn, verify_fn=verify_outline_payload
    ).run_batch(request)
    tree: list[dict] = []
    if report.results and report.results[0].status == "completed":
        ok, _err, data = roles.check_json(report.results[0].payload, required_keys=("sections",))
        if ok and isinstance(data, dict):
            tree = data.get("sections") or []
    return tree, report.to_dict()


async def run_content_stage(
    *, leaves: list[dict], facts: str, tier: str = "standard",
    outline_digest: str = "", executor_fn=None
) -> tuple[dict[str, str], dict]:
    """环节二：按 L4 叶子批式生成正文，返回 ``{section_id: markdown}``。"""
    result = await OmpContentAgent().generate_sections(
        leaves=leaves,
        facts=facts,
        tier=tier,
        outline_digest=outline_digest,
        executor_fn=executor_fn,
    )
    documents = {d.section_id: d.markdown for d in result.drafts if d.markdown}
    return documents, result.to_dict()


async def run_audit_stage(
    *, documents: dict[str, str], facts: str = "", executor_fn=None, apply: bool = True
) -> tuple[dict[str, str], dict]:
    """环节三：全量审计 + 精确修复指令 + 暂停兜底 + 完整报告。"""
    result = await OmpAuditAgent().audit(
        documents=documents, facts=facts, executor_fn=executor_fn, apply=apply
    )
    return result.revised_documents, result.to_dict()


def _stage_totals(stage_entry: dict) -> dict:
    """兼容两种报告包装：outline 阶段直接是 WorkflowReport.to_dict()，
    content / audit 阶段外层还包了一层业务汇总。
    """
    r = stage_entry.get("report") or {}
    return r.get("totals") or (r.get("report") or {}).get("totals") or {}


def stage_is_complete(entry: dict, *, artifact_key: str | None = None) -> tuple[bool, str]:
    """阶段是否真正完成（有统计、无暂停/降级、至少一个成功产物）。"""
    totals = _stage_totals(entry)
    if not totals:
        return False, "阶段报告缺少统计（未执行）"
    paused = int(totals.get("paused", 0) or 0)
    degraded = int(totals.get("degraded", 0) or 0)
    if paused or degraded:
        return False, f"存在暂停/降级任务（paused={paused}, degraded={degraded}）"
    if int(totals.get("completed", 0) or 0) <= 0:
        return False, "无成功产物"
    if artifact_key is not None and not entry.get(artifact_key):
        return False, f"阶段产物为空（{artifact_key}）"
    return True, ""


def build_export_contract(
    documents: dict[str, str],
    stages: list[dict],
    *,
    expected_section_ids: list[str] | None = None,
    status: str = "completed",
    blocked_reason: str = "",
) -> dict:
    """导出 DOCX 的前置契约（**不替代**既有 export.py 的可导出性闸门）。

    ⚠️ ``ready`` 只表达「Agent 侧自检通过且全链路完成」；真正的 high 级阻断、
       编号一致性校验与 DOCX 渲染由既有 ``export.py`` 单一持有。
    ⚠️ 无产物 / 前序阻断时显式 ``checked=False`` + ``undetermined_reason``，
       绝不把「没检查」渲染成「通过」。
    """
    findings = roles.scan_documents(documents)
    paused = sum(int(_stage_totals(s).get("paused", 0) or 0) for s in stages)
    expected_ids = list(expected_section_ids or [])
    missing = [sid for sid in expected_ids if sid not in documents]
    checks = {
        "numbering": not any(f["type"] == "numbering" for f in findings),
        "figure_ref": not any(f["type"] == "figure_ref" for f in findings),
        "fence_closed": not any(f["type"] == "fence" for f in findings),
        "no_placeholder": not any(f["type"] == "placeholder" for f in findings),
        "format": not any(f["type"] == "format" for f in findings),
        "no_paused_stage": paused == 0,
        "no_blocked_stage": not blocked_reason,
        "no_missing_sections": not missing,
        "documents_present": bool(documents),
        "all_stages_completed": status == "completed",
    }
    undetermined_reason = ""
    if blocked_reason:
        undetermined_reason = blocked_reason
    elif not documents:
        undetermined_reason = "无正文产物或未执行生成阶段，未做可导出性检查"
    checked = bool(documents) and not undetermined_reason
    return {
        "delegate_to": EXPORT_DELEGATE,
        "ready": bool(checked and all(checks.values())),
        "checked": bool(checked),
        "undetermined": not checked,
        "undetermined_reason": undetermined_reason,
        "checks": checks,
        "expected_document_count": len(expected_ids),
        "document_count": len(documents),
        "missing_sections": missing[:50],
        "findings": findings[:20],
        "agent_selfcheck": "scan_documents 仅为 omp Agent 侧确定性自检",
        "note": (
            "真正的可导出性闸门（high 级阻断 / 编号一致性 / DOCX 渲染）由既有 "
            "export.py 单一持有；本契约只交付自检结论，不得据此绕过导出闸门。"
        ),
    }


async def run_full_chain(
    *,
    stage: str = "full",
    brief: str = "",
    project_type: str = "",
    industry: str = "",
    facts: str = "",
    tier: str = "standard",
    documents: dict[str, str] | None = None,
    leaves: list[dict] | None = None,
    outline_tree: list[dict] | None = None,
    executor_fn=None,
    apply_audit: bool = True,
) -> dict:
    """三环节全自动流水线入口，返回可序列化的完整报告。

    ``stage``：``outline`` / ``content`` / ``audit`` / ``full``。
    ``full`` 时前一环节的产物自动成为后一环节的输入；**前序失败即阻断下游**，
    不把空目录 / 空正文喂给后续阶段制造「全绿空跑」。
    """
    stage = str(stage or "full").strip().lower()
    if stage not in VALID_STAGES:
        raise ValueError(f"未知 stage：{stage}（合法值 {VALID_STAGES}）")

    out: dict = {
        "engine": "omp_agent",
        "stage": stage,
        "status": "completed",
        "blocked_reason": "",
        "stages": [],
        "missing_sections": [],
    }
    tree = list(outline_tree or [])
    docs = dict(documents or {})
    expected_ids: list[str] = []
    blocked = False

    if stage in ("outline", "full"):
        tree, report = await run_outline_stage(
            brief=brief, project_type=project_type, industry=industry,
            facts=facts, executor_fn=executor_fn,
        )
        entry = {"stage": "outline", "report": report, "tree": tree}
        out["stages"].append(entry)
        ok, reason = stage_is_complete(entry, artifact_key="tree")
        if not ok:
            if stage == "full":
                blocked = True
                out["status"] = "blocked"
                out["blocked_reason"] = f"目录生成未通过：{reason}"
            elif out["status"] == "completed":
                out["status"] = "degraded"

    if stage in ("content", "full") and not blocked:
        target_leaves = list(leaves or []) or _flatten_leaves(tree)
        expected_ids = [str(x["id"]) for x in target_leaves]
        if not target_leaves and stage == "full":
            blocked = True
            out["status"] = "blocked"
            out["blocked_reason"] = "目录未产出任何 L4 叶子节点，无法生成正文"
        else:
            outline_digest = "\n".join(
                f"L{n.get('level', 1)} {n.get('title', '')} (id={n.get('id')})"
                for n in target_leaves[:80]
            )
            generated, report = await run_content_stage(
                leaves=target_leaves, facts=facts, tier=tier,
                outline_digest=outline_digest, executor_fn=executor_fn,
            )
            if generated:
                docs = generated
            entry = {"stage": "content", "report": report}
            out["stages"].append(entry)
            ok, _reason = stage_is_complete(entry)
            if not ok and out["status"] == "completed":
                out["status"] = "degraded"

    if stage in ("audit", "full") and not blocked:
        if docs:
            revised, report = await run_audit_stage(
                documents=docs, facts=facts, executor_fn=executor_fn, apply=apply_audit
            )
            docs = revised
            entry = {"stage": "audit", "report": report}
            out["stages"].append(entry)
            ok, _reason = stage_is_complete(entry)
            if not ok and out["status"] == "completed":
                out["status"] = "degraded"
        else:
            blocked = True
            out["status"] = "blocked"
            out["blocked_reason"] = "无正文产物，审核阶段未执行（未做可导出性检查）"

    missing = [sid for sid in expected_ids if sid not in docs]
    out["missing_sections"] = missing
    if missing and out["status"] == "completed":
        out["status"] = "degraded"
    out["documents"] = docs
    out["export_contract"] = build_export_contract(
        docs,
        out["stages"],
        expected_section_ids=expected_ids,
        status=out["status"],
        blocked_reason=out["blocked_reason"],
    )
    logger.info(
        "omp 全链路完成：stage=%s status=%s 阶段=%d 正文=%d 缺章=%d 可导出检查=%s",
        stage, out["status"], len(out["stages"]), len(docs), len(missing),
        out["export_contract"]["checked"],
    )
    return out
