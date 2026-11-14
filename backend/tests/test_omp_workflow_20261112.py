"""阶段 3 · omp 三环节全自动工作流护栏（2026-11-12）。

覆盖：
  - 三大 Agent 角色 / Prompt 模板（事实铁律、plan 四要素、精确修复指令、pause 兜底）；
  - 结构化校验器 check_json / verify_outline / verify_content_draft /
    verify_audit_fixes / scan_documents / allocate_leaf_budgets；
  - 编排层 batch 形态、compaction、plan→execute→verify→fix、重试 / 降级 / 暂停 / 恢复、
    并发隔离与 per-ID 冲突指令；
  - 单元 / 集成（目录→正文→审核修复→导出契约）/ 异常（限流、超时、JSON 损坏）；
  - 端点双闸 409、分层契约、字节体检。

全程注入 ``executor_fn``，不拉起真实 omp 二进制（对齐既有 omp 测试口径）。
"""

from __future__ import annotations

import json
import os
import re

import pytest
from app.services.ai import omp_agent_roles as roles
from app.services.ai import omp_pipeline as pipeline
from app.services.ai import omp_workflow as wf
from app.services.ai.omp_audit_agent import OmpAuditAgent
from app.services.ai.omp_content_agent import OmpContentAgent

_NEW_SOURCES = [
    "omp_agent_roles.py",
    "omp_workflow.py",
    "omp_content_agent.py",
    "omp_audit_agent.py",
    "omp_pipeline.py",
]
_SERVICES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "services", "ai"
)


def _valid_tree():
    """构造满足 L1~L4 数量约束的目录树（1 章 ×3 节 ×3 子节 ×4 叶子 = 36 叶）。"""

    def l4(k):
        return [{"title": f"工艺要点{k}-{j}", "level": 4, "children": []} for j in range(1, 5)]

    def l3(m):
        return [{"title": f"施工环节{m}-{k}", "level": 3, "children": l4(k)} for k in range(1, 4)]

    def l2(i):
        return [{"title": f"分部工程{i}-{m}", "level": 2, "children": l3(m)} for m in range(1, 4)]

    return [{"title": f"工程要素{i}", "level": 1, "children": l2(i)} for i in range(1, 2)]


def _body_text(budget: int) -> str:
    """生成无占位符 / 无围栏 / 无数字单位的达标正文（21 字 ×N）。"""
    unit = "本工程采用分段施工工艺并严格控制质量验收。"
    return unit * (int(budget) // len(unit) + 2)


async def _chain_executor(spec, prompt, workspace):  # noqa: ANN001
    """按角色返回不同产物，模拟三环节 omp 子 Agent。"""
    role = spec.metadata.get("role")
    key = role.key if role is not None else ""
    if key == "outline":
        return json.dumps({"sections": _valid_tree()}, ensure_ascii=False)
    if key == "content":
        return _body_text(spec.metadata.get("budget") or 300)
    return json.dumps({"fixes": []}, ensure_ascii=False)


# ===================== 一、角色与 Prompt 模板 =====================
def test_roles_cover_three_stages():
    assert set(roles.ROLES) == {"outline", "content", "audit"}
    assert "目录编制专家" in roles.OUTLINE_ROLE.name
    assert "技术方案撰写专家" in roles.CONTENT_ROLE.name
    assert "质量审核专家" in roles.AUDIT_ROLE.name
    for r in roles.ROLES.values():
        # 2026-11-13 · 放开命令执行：bash 已加入默认白名单
        assert "bash" in r.tools
        assert r.output_file


def test_system_prompts_enforce_fact_sandbox_and_tool_use():
    for r in roles.ROLES.values():
        assert "严禁编造" in r.system_prompt
        # 2026-11-13 · 放开命令执行：提示词改为"可使用 bash"而非"禁止 shell"
        assert "bash" in r.system_prompt and "网络" in r.system_prompt
        assert "业务数据库" in r.system_prompt
        assert "read" in r.system_prompt and "grep" in r.system_prompt


def test_outline_prompts_have_plan_fields_and_level_constraints():
    p = roles.build_outline_prompts(brief="某桥梁专项施工方案", project_type="桥梁", industry="市政")
    assert set(p) == {"plan", "execute", "fix"}
    for field in ("# 计划", "## 步骤", "## 涉及文件", "## 验收标准"):
        assert field in p["plan"]
    assert "L2 3~5" in p["execute"] and "L3 3~5" in p["execute"] and "L4 4~6" in p["execute"]
    assert "OUTLINE.json" in p["execute"] and "编号前缀" in p["execute"]
    assert "{issues}" in p["fix"] and "补全或裁减" in p["fix"]


def test_content_prompts_require_extension_loop_and_fact_grounding():
    p = roles.build_content_prompts(outline_digest="L4 工艺要点", facts="工期 450 天", tier="standard")
    assert "word" not in p["plan"] or "## 步骤" in p["plan"]
    assert "1200" in p["execute"] and "sections/<section_id>.md" in p["execute"]
    assert "扩展循环" in p["fix"] and "grep" in p["fix"] and "global-facts.md" in p["fix"]
    assert "严禁编造" in p["execute"]


def test_audit_prompts_require_precise_fixes_and_pause_fallback():
    p = roles.build_audit_prompts(document_count=3)
    assert "共 3 个章节文件" in p["execute"]
    assert "old_text" in p["execute"] and "new_text" in p["execute"]
    assert "不要直接重写全文" in p["execute"]
    assert "AUDIT_REPORT.md" in p["execute"]
    assert "需人工介入" in p["fix"] and "暂停" in p["fix"]


# ===================== 二、结构化校验器 =====================
def test_check_json_accepts_plain_and_fenced():
    ok, err, data = roles.check_json('{"sections": []}', required_keys=("sections",))
    assert ok and err == "" and data["sections"] == []
    ok2, err2, data2 = roles.check_json('```json\n{"fixes": []}\n```', required_keys=("fixes",))
    assert ok2 and err2 == "" and data2["fixes"] == []


def test_check_json_reports_invalid_and_missing_keys():
    ok, err, _ = roles.check_json("{oops", required_keys=("sections",))
    assert not ok and "JSON 解析失败" in err
    ok2, err2, _ = roles.check_json("", required_keys=("sections",))
    assert not ok2 and "空" in err2
    ok3, err3, _ = roles.check_json('{"a":1}', required_keys=("sections",))
    assert not ok3 and "缺少必填键" in err3


def test_verify_outline_accepts_valid_tree():
    assert roles.verify_outline(_valid_tree()) == []


def test_verify_outline_rejects_bad_child_count():
    tree = _valid_tree()
    tree[0]["children"] = tree[0]["children"][:2]  # L2 少于 3
    issues = roles.verify_outline(tree)
    assert any("子节点 2 个" in i and "3~5" in i for i in issues)


def test_verify_outline_rejects_depth_and_number_prefix():
    tree = _valid_tree()
    tree[0]["children"][0]["children"][0]["children"][0]["title"] = "1.1.1.1 工艺要点"
    issues = roles.verify_outline(tree)
    assert any("编号前缀" in i for i in issues)
    deep = [{"title": "A", "level": 1, "children": [{"title": "B", "level": 2,
            "children": [{"title": "C", "level": 3, "children": [
                {"title": "D", "level": 4, "children": [
                    {"title": "E", "level": 5, "children": []}]}]}]}]}]
    assert any("层级越界" in i for i in roles.verify_outline(deep))


def test_allocate_leaf_budgets_1200_over_four():
    assert roles.allocate_leaf_budgets(1200, 4) == [300, 300, 300, 300]
    # 均分低于平台叶子地板时按地板（显式口径，见函数注释）
    assert roles.allocate_leaf_budgets(800, 4) == [300, 300, 300, 300]
    assert roles.allocate_leaf_budgets(1200, 0) == []


def test_verify_content_draft_passes():
    text = _body_text(300)
    assert roles.verify_content_draft(text, budget=300, facts="") == []


def test_verify_content_draft_flags_underlength_placeholder_fence_and_fact():
    assert any("字数不足" in i for i in roles.verify_content_draft("太短", budget=300, facts=""))
    assert any("占位符" in i for i in roles.verify_content_draft(_body_text(300) + " TODO", budget=300, facts=""))
    assert any("围栏" in i for i in roles.verify_content_draft(
        _body_text(300) + "\n```mermaid\ngraph TD;", budget=400, facts=""))
    issues = roles.verify_content_draft(
        _body_text(300) + "本工程工期 400 天。", budget=400, facts="工期：450 天"
    )
    assert any("脱离全局事实" in i and "400天" in i for i in issues)


def test_verify_audit_fixes_requires_unique_hit_and_minimal_change():
    docs = {"s1": "本工程工期 400 天，质量合格。"}
    good = {"fixes": [{"section_id": "s1", "old_text": "工期 400 天",
                       "new_text": "工期 450 天", "reason": "与全局事实冲突"}]}
    assert roles.verify_audit_fixes(json.dumps(good, ensure_ascii=False), docs) == []
    bad_missing = {"fixes": [{"section_id": "s1", "old_text": "不存在",
                              "new_text": "X", "reason": "r"}]}
    assert any("命中 0 次" in i for i in roles.verify_audit_fixes(json.dumps(bad_missing), docs))
    bad_big = {"fixes": [{"section_id": "s1", "old_text": "工期 400 天",
                          "new_text": "改" * 1000, "reason": "r"}]}
    assert any("整章重写" in i for i in roles.verify_audit_fixes(json.dumps(bad_big), docs))
    assert any("数组" in i for i in roles.verify_audit_fixes('{"fixes": "x"}', docs))


def test_apply_fix_directives_is_precise_and_marks_failures():
    docs = {"s1": "工期 400 天；工期 400 天。", "s2": "A"}
    fixes = [{"section_id": "s1", "old_text": "工期 400 天", "new_text": "工期 450 天"}]
    out, failures = roles.apply_fix_directives(docs, fixes)
    # old_text 命中 2 次 → 拒绝应用（防误改），需人工介入
    assert failures and out == docs
    out2, failures2 = roles.apply_fix_directives(
        {"s1": "工期 400 天。"}, fixes
    )
    assert not failures2 and out2["s1"] == "工期 450 天。"


def test_scan_documents_detects_numbering_figure_fence_and_format():
    docs = {
        "s1": "# 1 工程概况\n## 1.1 部署\n## 1.3 工艺\n图 1-1 见图 1-3\n```mermaid\ngraph TD;\nTODO",
        "s2": "# 2 质量\n#### 2.1.1.1 保证\n",
    }
    found = roles.scan_documents(docs)
    types = {f["type"] for f in found}
    assert {"numbering", "figure_ref", "fence", "placeholder", "format"} <= types


# ===================== 三、编排层 =====================
def test_batch_request_matches_omp_task_batch_shape():
    req = wf.BatchRequest(context="共享背景", tasks=[
        wf.OmpTaskSpec(task_id="a", instruction="写 A", output_file="sections/a.md"),
        wf.OmpTaskSpec(task_id="b", instruction="写 B", output_file="sections/b.md"),
    ])
    shape = req.to_omp_task_batch()
    assert set(shape) == {"context", "tasks"}
    assert shape["context"] == "共享背景"
    assert [t["id"] for t in shape["tasks"]] == ["a", "b"]
    assert shape["tasks"][0]["output_file"] == "sections/a.md"


def test_classify_error_categories_and_word_boundary():
    assert wf.classify_error(RuntimeError("HTTP 429 rate limit")) == "rate_limited"
    assert wf.classify_error(RuntimeError("token 42980 个")) == "unknown"  # 词边界防误判
    assert wf.classify_error(TimeoutError("timeout")) == "timeout"
    assert wf.classify_error(json.JSONDecodeError("x", "x", 0)) == "json_invalid"
    assert wf.classify_error(RuntimeError("write tool failed")) == "tool_failed"


def test_safe_task_id_blocks_path_traversal():
    assert wf.safe_task_id("s1") == "s1"
    assert not wf.safe_task_id("..").startswith("..")
    assert not wf.safe_task_id("../etc/passwd").startswith("..")
    assert "/" not in wf.safe_task_id("../etc/passwd")
    leaves = pipeline._flatten_leaves(_valid_tree())
    ids = [x["id"] for x in leaves]
    assert len(ids) == 36 and len(set(ids)) == 36  # 跨父节点不撞 id


async def test_orchestrator_happy_path_writes_plan_and_context():
    seen = {}

    async def ex(spec, prompt, ws):
        seen["prompt"] = prompt
        seen["plan"] = open(os.path.join(ws, "PLAN.md"), encoding="utf-8").read()
        seen["ctx"] = open(os.path.join(ws, "CONTEXT.md"), encoding="utf-8").read()
        return "OK"

    spec = wf.OmpTaskSpec(task_id="t1", instruction="做事", metadata={
        "plan": {"title": "计划 T", "steps": ["第一步"], "files": ["A.md"],
                 "acceptance": ["验收一"]},
        "prompts": {"execute": "执行指令"},
    })
    orch = wf.OmpBatchOrchestrator(stage="t", executor_fn=ex,
                                   verify_fn=lambda s, p: [], keep_workspace=True)
    report = await orch.run_batch(wf.BatchRequest(context="共享上下文", tasks=[spec]))
    r = report.results[0]
    assert r.status == "completed" and r.payload == "OK" and r.attempts == 1
    assert "## 步骤" in seen["plan"] and "验收一" in seen["plan"]
    assert seen["ctx"] == "共享上下文"
    assert "PLAN.md" in seen["prompt"] and "执行指令" in seen["prompt"]
    assert report.totals == {"total": 1, "completed": 1, "degraded": 0,
                             "paused": 0, "fix_rounds": 0, "attempts": 1}


async def test_compaction_writes_full_context_to_file_not_prompt():
    seen = {}

    async def ex(spec, prompt, ws):
        seen["prompt"] = prompt
        seen["ctx"] = open(os.path.join(ws, "CONTEXT.md"), encoding="utf-8").read()
        return "OK"

    orch = wf.OmpBatchOrchestrator(stage="t", executor_fn=ex, verify_fn=lambda s, p: [],
                                   context_limit=2048, keep_workspace=True)
    report = await orch.run_batch(wf.BatchRequest(
        context="甲" * 100000, tasks=[wf.OmpTaskSpec(task_id="t1", instruction="做事")]))
    assert report.compaction_applied is True
    assert "已触发压缩" in report.context_digest
    assert "上下文已压缩" in seen["ctx"] and "甲" * 500 in seen["ctx"]
    assert "甲" * 5000 not in seen["prompt"]  # 长文本绝不塞进单帧


async def test_orchestrator_fix_round_recovers():
    calls = []

    async def ex(spec, prompt, ws):
        calls.append(prompt)
        return "BAD" if len(calls) == 1 else "GOOD"

    orch = wf.OmpBatchOrchestrator(
        stage="t", executor_fn=ex,
        verify_fn=lambda s, p: [] if p == "GOOD" else ["结构校验不通过"],
        max_fix_rounds=2,
    )
    report = await orch.run_batch(wf.BatchRequest(context="c", tasks=[
        wf.OmpTaskSpec(task_id="t1", instruction="做事", metadata={"prompts": {"fix": "【Fix】{issues}"}})]))
    r = report.results[0]
    assert r.status == "completed" and r.fix_rounds == 1
    assert "【Fix】结构校验不通过" in calls[1]
    assert any("[verify]" in e for e in r.errors)


async def test_orchestrator_pauses_after_exhausted_fixes_and_can_resume():
    async def bad(spec, prompt, ws):
        return "BAD"

    async def good(spec, prompt, ws):
        return "GOOD"

    spec = wf.OmpTaskSpec(task_id="t1", instruction="做事")
    verify = lambda s, p: [] if p == "GOOD" else ["仍不合格"]  # noqa: E731
    orch = wf.OmpBatchOrchestrator(stage="t", executor_fn=bad, verify_fn=verify,
                                   max_fix_rounds=1)
    req = wf.BatchRequest(context="c", tasks=[spec])
    report = await orch.run_batch(req)
    r = report.results[0]
    assert r.status == "paused" and r.phase == "verify_failed"
    assert r.manual_review and len(report.paused) == 1
    assert report.paused[0]["task_id"] == "t1" and report.paused[0]["snippet"]
    resumed = await wf.OmpBatchOrchestrator(
        stage="t", executor_fn=good, verify_fn=verify, max_fix_rounds=1
    ).resume_paused(report, req)
    assert len(resumed.results) == 1 and resumed.results[0].status == "completed"


async def test_orchestrator_retries_rate_limit_and_timeout():
    seq = [RuntimeError("HTTP 429 rate limit exceeded"), TimeoutError("timeout"), "OK"]

    async def ex(spec, prompt, ws):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    orch = wf.OmpBatchOrchestrator(stage="t", executor_fn=ex,
                                   verify_fn=lambda s, p: [], retry_backoff=0.0)
    report = await orch.run_batch(wf.BatchRequest(context="c", tasks=[
        wf.OmpTaskSpec(task_id="t1", instruction="做事")]))
    r = report.results[0]
    assert r.status == "completed" and r.attempts == 3
    assert any("[rate_limited]" in e for e in r.errors)
    assert any("[timeout]" in e for e in r.errors)


async def test_orchestrator_falls_back_on_tool_failure():
    async def bad(spec, prompt, ws):
        raise RuntimeError("write tool failed: permission denied")

    async def fallback(spec, prompt, ws):
        return "FALLBACK"

    orch = wf.OmpBatchOrchestrator(stage="t", executor_fn=bad, fallback_fn=fallback,
                                   verify_fn=lambda s, p: [])
    report = await orch.run_batch(wf.BatchRequest(context="c", tasks=[
        wf.OmpTaskSpec(task_id="t1", instruction="做事")]))
    r = report.results[0]
    assert r.status == "completed" and r.payload == "FALLBACK"
    assert any("[tool_failed]" in e for e in r.errors)
    assert any("[fallback]" in e for e in r.errors)


async def test_concurrent_tasks_are_isolated_by_id():
    async def ex(spec, prompt, ws):
        path = os.path.join(ws, spec.output_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"payload-{spec.task_id}")
        return ""  # 强制编排器从各自工作区读回产物

    tasks = [wf.OmpTaskSpec(task_id=f"s{i}", instruction="写", output_file=f"sections/s{i}.md")
             for i in range(6)]
    orch = wf.OmpBatchOrchestrator(stage="t", executor_fn=ex,
                                   verify_fn=lambda s, p: [], max_concurrency=3)
    report = await orch.run_batch(wf.BatchRequest(context="c", tasks=tasks))
    assert report.totals["completed"] == 6
    assert {r.task_id: r.payload for r in report.results} == {
        f"s{i}": f"payload-s{i}" for i in range(6)
    }
    assert len({r.workspace for r in report.results}) == 6


def test_per_id_conflict_directives_are_grouped():
    report = wf.WorkflowReport(stage="t", results=[
        wf.TaskResult(task_id="ok", status="completed"),
        wf.TaskResult(task_id="bad", status="paused", errors=["失败"],
                      manual_review="片段", phase="fix"),
    ])
    direct = wf.per_id_conflict_directives(report)
    assert len(direct) == 1 and direct[0]["id"] == "bad"
    assert direct[0]["directive"] == "manual_review" and direct[0]["snippet"] == "片段"


# ===================== 四、三大环节 Agent + 全链路 =====================
async def test_content_agent_batch_produces_drafts():
    leaves = pipeline._flatten_leaves(_valid_tree())[:4]
    result = await OmpContentAgent().generate_sections(
        leaves=leaves, facts="", tier="standard", executor_fn=_chain_executor
    )
    assert len(result.drafts) == 4
    assert all(d.status == "completed" and d.word_count >= 240 for d in result.drafts)
    assert result.report.totals["completed"] == 4


async def test_audit_agent_full_scan_and_report():
    docs = {"s1": "# 1 工程概况\n## 1.1 部署\n## 1.3 工艺\nTODO",
            "s2": _body_text(300)}
    result = await OmpAuditAgent().audit(documents=docs, facts="", executor_fn=_chain_executor)
    assert any(f["type"] == "numbering" for f in result.findings)
    assert any(f["type"] == "placeholder" for f in result.findings)
    assert "审核与修复报告" in result.report_md
    assert "需人工介入" in result.report_md or "无" in result.report_md
    assert result.revised_documents == docs  # 无修复指令 → 原文保持不变
    assert result.report.totals["completed"] == 2


async def test_full_chain_outline_content_audit_and_export_contract():
    """集成：目录 → 正文 → 审核修复 → 导出 DOCX 前置契约，全链路上下文交接。"""
    result = await pipeline.run_full_chain(
        stage="full", brief="某桥梁专项施工方案", facts="", executor_fn=_chain_executor
    )
    assert result["engine"] == "omp_agent"
    assert [s["stage"] for s in result["stages"]] == ["outline", "content", "audit"]
    outline = result["stages"][0]
    assert outline["tree"] and len(pipeline._flatten_leaves(outline["tree"])) == 36
    assert outline["report"]["totals"]["completed"] == 1
    content = result["stages"][1]
    assert content["report"]["report"]["totals"]["completed"] == 36
    assert len(result["documents"]) == 36
    audit = result["stages"][2]
    assert "审核与修复报告" in audit["report"]["audit_report_md"]
    ec = result["export_contract"]
    assert ec["delegate_to"].endswith("/export/docx")
    assert ec["document_count"] == 36
    assert ec["ready"] is True and all(ec["checks"].values())
    # 产物可直接序列化（SSE/HTTP 契约）
    assert json.dumps(result, ensure_ascii=False)


async def test_full_chain_json_failure_triggers_fix_round():
    """异常：模型首轮输出坏 JSON → verify 判红 → fix 轮恢复。"""
    calls = {"outline": 0}

    async def ex(spec, prompt, workspace):
        role = spec.metadata.get("role")
        key = role.key if role is not None else ""
        if key == "outline":
            calls["outline"] += 1
            if "Fix" in prompt or "定向修复" in prompt:
                return json.dumps({"sections": _valid_tree()}, ensure_ascii=False)
            return "{坏掉的 JSON"
        return await _chain_executor(spec, prompt, workspace)

    result = await pipeline.run_full_chain(
        stage="outline", brief="x", facts="", executor_fn=ex
    )
    assert calls["outline"] == 2
    assert result["stages"][0]["tree"]
    r = result["stages"][0]["report"]["results"][0]
    assert r["status"] == "completed" and r["fix_rounds"] == 1
    assert any("JSON 解析失败" in e for e in r["errors"])


# ===================== 五、端点 / 分层 / 字节体检 =====================
async def test_workflow_endpoint_disabled_returns_409():
    from app.routers import sections as sec_mod

    with pytest.raises(Exception) as ei:
        await sec_mod.run_omp_workflow("scheme-x", {"brief": "x"}, None)
    assert ei.value.status_code == 409
    assert "未启用" in str(ei.value.detail)


async def test_workflow_endpoint_enabled_runs_chain_and_releases_inflight(monkeypatch):
    import app.config as cfg
    from app.routers import sections as sec_mod

    sec_mod._OMP_WORKFLOW_INFLIGHT.clear()
    # ✅ 端点新增配置预检（阶段 3 收口）：本用例模拟「配置完整」场景，
    #    闸门本身由 test_omp_workflow_closeout_20261113.py 的 422 用例行为实证。
    from app.services.ai.omp_task_agent import OmpTaskAgent

    monkeypatch.setattr(OmpTaskAgent, "check_ready", lambda self: None)
    monkeypatch.setattr(cfg.settings, "omp_task_enabled", True)
    monkeypatch.setattr(cfg.settings, "omp_workflow_enabled", True)
    monkeypatch.setattr(sec_mod, "ensure_no_generation_write", lambda *a, **k: None)
    monkeypatch.setattr(sec_mod, "content_generation_in_progress", lambda _sid: None)
    monkeypatch.setattr(sec_mod, "outline_generation_in_progress", lambda _sid: None)

    async def fake_chain(**kwargs):
        assert kwargs["stage"] == "audit" and kwargs["documents"] == {"a": "text"}
        return {"engine": "omp_agent", "stages": [], "documents": {},
                "export_contract": {"ready": True}}

    monkeypatch.setattr(pipeline, "run_full_chain", fake_chain)
    res = await sec_mod.run_omp_workflow(
        "s1", {"stage": "audit", "brief": "x", "documents": {"a": "text"}}, None
    )
    assert res["engine"] == "omp_agent" and res["scheme_id"] == "s1"
    assert "s1" not in sec_mod._OMP_WORKFLOW_INFLIGHT  # finally 必须释放


async def test_workflow_endpoint_rejects_unknown_stage(monkeypatch):
    import app.config as cfg
    from app.routers import sections as sec_mod

    # ✅ 端点新增配置预检（阶段 3 收口）：本用例模拟「配置完整」场景，
    #    闸门本身由 test_omp_workflow_closeout_20261113.py 的 422 用例行为实证。
    from app.services.ai.omp_task_agent import OmpTaskAgent

    monkeypatch.setattr(OmpTaskAgent, "check_ready", lambda self: None)
    monkeypatch.setattr(cfg.settings, "omp_task_enabled", True)
    monkeypatch.setattr(cfg.settings, "omp_workflow_enabled", True)
    monkeypatch.setattr(sec_mod, "ensure_no_generation_write", lambda *a, **k: None)
    monkeypatch.setattr(sec_mod, "content_generation_in_progress", lambda _sid: None)
    monkeypatch.setattr(sec_mod, "outline_generation_in_progress", lambda _sid: None)
    with pytest.raises(Exception) as ei:
        await sec_mod.run_omp_workflow(
            "s1", {"stage": "nope", "brief": "x", "documents": {"a": "t"}}, None
        )
    assert ei.value.status_code == 422
    assert "s1" not in sec_mod._OMP_WORKFLOW_INFLIGHT


def test_export_delegate_endpoint_really_exists():
    """集成链末端：export_contract 指向的 DOCX 端点必须在既有源码中真实存在。"""
    routers_dir = os.path.join(os.path.dirname(os.path.dirname(_SERVICES_DIR)), "routers")
    with open(os.path.join(routers_dir, "export.py"), "rb") as f:
        src = f.read().decode("utf-8")
    assert 'APIRouter(prefix="/api/v1/schemes/{scheme_id}/export"' in src
    assert '@router.post("/docx")' in src
    assert pipeline.EXPORT_DELEGATE == "POST /api/v1/schemes/{scheme_id}/export/docx"


def test_new_services_never_import_routers():
    """分层铁律：services 层禁止 import routers（AST 级契约的同款源码锁）。"""
    pat = re.compile(r"^\s*(?:from|import)\s+\S*routers", flags=re.M)
    for name in _NEW_SOURCES:
        with open(os.path.join(_SERVICES_DIR, name), "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8")
        assert not pat.search(text), f"{name} 出现 routers 导入"


def test_new_backend_sources_byte_health():
    """字节体检：BOM=0 / U+FFFD=0 / 换行符一致且不混入孤立 CR。"""
    for name in _NEW_SOURCES:
        with open(os.path.join(_SERVICES_DIR, name), "rb") as f:
            raw = f.read()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{name} 带 BOM"
        assert b"\xef\xbf\xbd" not in raw, f"{name} 含替换字符"
        lf = raw.count(b"\n")
        crlf = raw.count(b"\r\n")
        assert crlf == lf, f"{name} 换行符混用（CRLF={crlf}, LF={lf}）"
