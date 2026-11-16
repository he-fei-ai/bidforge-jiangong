"""oh-my-pi (omp) 三环节全自动工作流 —— 端到端集成护栏（2026-11-15）。

覆盖「验证与回归」三类要求：

1. 单元测试：``task`` 工具 batch 形态 ``{context, tasks[]}`` 的结构化委派与回收；
2. 集成测试：目录生成 → 正文生成 → 审核修复 → 导出前置契约的全链路上下文交接；
3. 异常测试：AI 限流 / 超时 / JSON 解析失败，验证重试与 ``fix`` 环节触发并恢复。

⚠️ 全部执行器经 ``executor_fn`` 注入，不拉起真实 omp 子进程 —— 本机未安装
   omp 二进制与 Bun，故本文件可完全离线回归；真实二进制就绪度由
   ``omp_diagnostics.preflight`` 在 HTTP 入口单独把关，两者职责不重叠。
⚠️ 分层约束：services 层禁止 import routers，本文件只依赖 services 层模块。
⚠️ 本文件只验证「Agent 侧链路」，落库与 DOCX 渲染仍由既有单一写入口持有；
   交接边界由 ``test_export_delegate_is_contract_not_invocation`` 显式钉死。
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from app.services.ai import omp_agent_roles as roles
from app.services.ai import omp_pipeline as pipeline
from app.services.ai import omp_workflow as wf

_UNIT = "本工程采用分段施工工艺并严格控制质量验收。"


# ---------- 公共夹具：与既有 omp 护栏同口径的合法 L1~L4 目录树 ----------
def _valid_tree() -> list[dict]:
    def l4(k: int) -> list[dict]:
        return [{"title": f"工艺{k}-{j}", "level": 4, "children": []} for j in range(1, 5)]

    def l3(m: int) -> list[dict]:
        return [{"title": f"环节{m}-{k}", "level": 3, "children": l4(k)} for k in range(1, 4)]

    def l2(i: int) -> list[dict]:
        return [{"title": f"分部{i}-{m}", "level": 2, "children": l3(m)} for m in range(1, 4)]

    return [{"title": f"工程要素{i}", "level": 1, "children": l2(i)} for i in range(1, 2)]


def _body(budget: int) -> str:
    """生成恰好落在 [budget*0.8, budget*上限] 区间内的中文正文（无数字单位）。"""
    return _UNIT * (int(budget) // len(_UNIT) + 2)


def _read(ws: str, rel: str) -> str:
    with open(os.path.join(ws, rel), encoding="utf-8") as f:
        return f.read()


async def _good_executor(spec, prompt, workspace):  # noqa: ANN001
    """按角色返回合法产物：目录 JSON / 足量正文 / 空修复指令集。"""
    role = spec.metadata.get("role")
    key = role.key if role is not None else ""
    if key == "outline":
        return json.dumps({"sections": _valid_tree()}, ensure_ascii=False)
    if key == "content":
        return _body(int(spec.metadata.get("budget") or 300))
    return json.dumps({"fixes": []}, ensure_ascii=False)


def _outline_request(n: int = 1) -> wf.BatchRequest:
    """构造目录阶段 batch；n>1 时复制任务用于验证并发隔离与精准恢复。"""
    base = pipeline.build_outline_batch(brief="某深基坑专项施工方案", project_type="基坑")
    if n <= 1:
        return base
    tasks = [
        wf.OmpTaskSpec(
            task_id=f"outline-{i}",
            instruction=base.tasks[0].instruction,
            output_file=base.tasks[0].output_file,
            metadata=dict(base.tasks[0].metadata),
        )
        for i in range(n)
    ]
    return wf.BatchRequest(context=base.context, tasks=tasks)


def _outline_orchestrator(executor_fn, **kw) -> wf.OmpBatchOrchestrator:  # noqa: ANN001
    return wf.OmpBatchOrchestrator(
        stage="outline",
        executor_fn=executor_fn,
        verify_fn=pipeline.verify_outline_payload,
        **kw,
    )


# ===================== 1. task 工具 batch 结构化调用 =====================
def test_task_batch_exports_native_omp_shape():
    req = wf.BatchRequest(
        context="公共上下文",
        tasks=[wf.OmpTaskSpec(task_id="L1.1.1.1", instruction="生成目录", output_file="OUTLINE.json")],
    )
    batch = req.to_omp_task_batch()
    assert set(batch) == {"context", "tasks"}
    assert batch["context"] == "公共上下文"
    assert batch["tasks"] == [
        {"id": "L1.1.1.1", "prompt": "生成目录", "output_file": "OUTLINE.json"}
    ]


async def test_task_batch_result_is_structured_and_serializable():
    """batch 返回必须是可序列化结构（totals / results / paused 三件套）。"""
    report = await _outline_orchestrator(_good_executor).run_batch(_outline_request())
    data = report.to_dict()
    assert set(data) >= {"stage", "totals", "results", "paused", "compaction_applied"}
    assert data["stage"] == "outline"
    assert data["totals"]["completed"] == 1
    assert data["results"][0]["status"] == "completed"
    assert json.loads(data["results"][0]["payload"])["sections"]


def test_safe_task_id_blocks_path_traversal():
    assert wf.safe_task_id("..") != ".."
    assert wf.safe_task_id("L1.1") == "L1.1"
    assert not os.path.isabs(wf.safe_task_id("../../etc/passwd"))


# ===================== 2. 三环节全链路与上下文交接 =====================
async def test_full_chain_outline_content_audit_to_export_contract():
    """目录 → 正文 → 审核修复 → 导出前置契约，逐环节产物自动交接。"""
    result = await pipeline.run_full_chain(
        stage="full",
        brief="某深基坑专项施工方案",
        project_type="基坑",
        industry="建筑",
        tier="standard",
        executor_fn=_good_executor,
    )
    assert [s["stage"] for s in result["stages"]] == ["outline", "content", "audit"]
    assert result["status"] == "completed"
    assert result["blocked_reason"] == ""
    assert result["documents"], "三环节必须交付正文文件集"
    assert result["missing_sections"] == [], "静默丢章即上下文交接失败"
    ec = result["export_contract"]
    assert ec["checked"] is True
    assert ec["ready"] is True
    assert ec["delegate_to"] == pipeline.EXPORT_DELEGATE
    assert ec["document_count"] == ec["expected_document_count"]


async def test_workspace_handoff_writes_context_and_structured_plan():
    """状态经工作区文件交接：CONTEXT.md + PLAN.md（标题/步骤/文件/验收标准）。"""
    seen: dict[str, str] = {}

    async def spy(spec, prompt, workspace):  # noqa: ANN001
        seen["context"] = _read(workspace, "CONTEXT.md")
        seen["plan"] = _read(workspace, "PLAN.md")
        seen["prompt"] = prompt
        return await _good_executor(spec, prompt, workspace)

    req = pipeline.build_outline_batch(brief="某深基坑专项施工方案", facts="工期 450 天")
    report = await _outline_orchestrator(spy).run_batch(req)
    assert report.totals["completed"] == 1
    assert "工期 450 天" in seen["context"], "全局事实必须经文件交接，不得塞进单帧"
    for section in ("# 计划", "## 步骤", "## 涉及文件", "## 验收标准"):
        assert section in seen["plan"], f"结构化计划缺少 {section}"
    assert "PLAN.md" in seen["prompt"] and "CONTEXT.md" in seen["prompt"]
    assert dict(report.results[0].plan)["acceptance"] == list(roles.OUTLINE_ROLE.acceptance)


def test_role_prompts_cover_plan_execute_fix_for_three_stages():
    """三大环节的 Prompt 模板必须齐备 plan/execute/fix 三段且带硬约束。"""
    sets = [
        roles.build_outline_prompts(brief="x", project_type="基坑"),
        roles.build_content_prompts(outline_digest="L1 x", facts="工期 450 天"),
        roles.build_audit_prompts(document_count=3),
    ]
    for prompts in sets:
        assert set(prompts) == {"plan", "execute", "fix"}
        assert "{issues}" in prompts["fix"], "fix 段必须回填校验错误信息"
        joined = "\n".join(prompts.values())
        assert "严禁编造" in joined, "幻觉硬约束必须覆盖三个环节"
        assert "工作区红线" in joined, "沙箱红线必须覆盖三个环节"
    assert roles.OUTLINE_ROLE.name == "建筑工程专项施工方案目录编制专家"
    assert roles.CONTENT_ROLE.name == "资深工程技术方案撰写专家"
    assert roles.AUDIT_ROLE.name == "严苛的标书质量审核专家"


def test_leaf_budget_allocation_1200_over_4_is_300():
    assert roles.allocate_leaf_budgets(1200, 4) == [300, 300, 300, 300]
    assert roles.verify_outline(_valid_tree()) == []


def test_fact_grounding_rejects_invented_values():
    facts = "总工期 450 天；基坑深度 12 米。"
    assert roles.assert_fact_grounding("按总工期 450 天组织施工。", facts) == []
    issues = roles.assert_fact_grounding("混凝土强度 C40，养护 999 天。", facts)
    assert issues and "999天" in "".join(issues)


# ===================== 3. 异常测试：限流 / 超时 / JSON 损坏 =====================
async def test_rate_limit_retries_and_recovers():
    calls = {"n": 0}

    async def flaky(spec, prompt, workspace):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 rate limit exceeded")
        return await _good_executor(spec, prompt, workspace)

    r = (await _outline_orchestrator(flaky).run_batch(_outline_request())).results[0]
    assert r.status == "completed"
    assert r.attempts >= 2
    assert any("rate_limited" in e for e in r.errors)


async def test_timeout_retries_and_recovers():
    calls = {"n": 0}

    async def flaky(spec, prompt, workspace):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.TimeoutError("模型响应超时")
        return await _good_executor(spec, prompt, workspace)

    r = (await _outline_orchestrator(flaky).run_batch(_outline_request())).results[0]
    assert r.status == "completed"
    assert r.attempts >= 2
    assert any("timeout" in e for e in r.errors)


async def test_json_invalid_triggers_fix_round_and_recovers():
    """JSON 解析失败不得直接失败：必须进入定向 fix 轮并按错误信息修复。"""
    prompts: list[str] = []
    calls = {"n": 0}

    async def broken_then_fixed(spec, prompt, workspace):  # noqa: ANN001
        prompts.append(prompt)
        calls["n"] += 1
        if calls["n"] == 1:
            return "{BROKEN"
        return await _good_executor(spec, prompt, workspace)

    r = (await _outline_orchestrator(broken_then_fixed).run_batch(_outline_request())).results[0]
    assert r.status == "completed"
    assert r.fix_rounds >= 1
    assert any("【Fix" in p and "定向修复" in p for p in prompts[1:])
    assert any("json_invalid" in e or "verify" in e for e in r.errors)


async def test_consecutive_failures_pause_and_resume_only_paused_ids():
    """连续修复失败 → paused + 需人工介入片段；恢复只重跑被暂停的 task_id。"""
    async def always_broken(spec, prompt, workspace):  # noqa: ANN001
        return "{BROKEN"

    req = _outline_request(2)
    bad = _outline_orchestrator(always_broken, max_fix_rounds=1, pause_after_failures=1)
    report = await bad.run_batch(req)
    assert report.totals["paused"] == 2
    assert all(p["snippet"] for p in report.paused), "必须标记需人工介入的异常片段"
    assert wf.per_id_conflict_directives(report)[0]["directive"] == "manual_review"

    good = _outline_orchestrator(_good_executor, max_fix_rounds=1)
    resumed = await good.resume_paused(report, req)
    assert {r.task_id for r in resumed.results} == {p["task_id"] for p in report.paused}
    assert resumed.totals["completed"] == 2


async def test_context_compaction_applies_and_keeps_full_context_in_file():
    """上下文膨胀：注入单帧只给摘要，完整约定仍留在 CONTEXT.md 供 read 分段读取。"""
    seen: dict[str, str] = {}

    async def spy(spec, prompt, workspace):  # noqa: ANN001
        seen["context"] = _read(workspace, "CONTEXT.md")
        seen["prompt"] = prompt
        return await _good_executor(spec, prompt, workspace)

    long_context = "全局事实约定。" * 2000
    req = wf.BatchRequest(context=long_context, tasks=_outline_request().tasks)
    report = await _outline_orchestrator(spy, context_limit=1024).run_batch(req)
    assert report.compaction_applied is True
    assert "上下文已压缩" in seen["context"]
    assert "已触发压缩" in seen["prompt"]
    assert report.totals["completed"] == 1


# ===================== 4. 审核与修复闭环 =====================
async def test_audit_applies_precise_fixes_and_emits_report():
    """审核不重写全文：old_text/new_text 唯一命中替换 + 完整审核修复报告。"""
    old, new = "工期 400 天", "工期 450 天"
    docs = {"s1": f"本段{old}，需按专项方案执行。" + _UNIT * 20}

    async def fixer(spec, prompt, workspace):  # noqa: ANN001
        return json.dumps(
            {
                "fixes": [
                    {
                        "section_id": spec.task_id,
                        "old_text": old,
                        "new_text": new,
                        "reason": "与全局事实冲突",
                        "severity": "high",
                    }
                ]
            },
            ensure_ascii=False,
        )

    revised, report = await pipeline.run_audit_stage(documents=docs, executor_fn=fixer)
    assert old not in revised["s1"] and new in revised["s1"]
    assert len(report["fixes"]) == 1
    assert report["failures"] == []
    md = report["audit_report_md"]
    assert "# 审核与修复报告" in md
    assert "结构化修复指令（old_text → new_text）" in md


def test_fix_directive_requires_unique_hit():
    docs = {"s1": "重复片段。重复片段。"}
    assert roles.verify_audit_fixes(
        {"fixes": [{"section_id": "s1", "old_text": "重复片段", "new_text": "修正", "reason": "x"}]},
        docs,
    ), "old_text 命中 2 次必须被拒绝"
    revised, failures = roles.apply_fix_directives(
        docs, [{"section_id": "s1", "old_text": "重复片段", "new_text": "修正"}]
    )
    assert revised == docs and failures, "无法精确命中时不得改写正文"


# ===================== 5. 交接边界：服务层就绪 ≠ 功能可达 =====================
async def test_export_delegate_is_contract_not_invocation():
    """Agent 侧产物不落库；导出端点从 DB 读 sections，delegate 只是契约字符串。

    钉死「服务层就绪 ≠ 功能可达」：避免调用方把 ``export_contract.ready``
    误读成「已经可以直接导出 DOCX」。
    """
    assert isinstance(pipeline.EXPORT_DELEGATE, str)
    assert pipeline.EXPORT_DELEGATE.startswith("POST ")
    contract = pipeline.build_export_contract(
        {"s1": _UNIT}, [], expected_section_ids=["s1"], status="completed"
    )
    assert contract["delegate_to"] == pipeline.EXPORT_DELEGATE
    assert "不得据此绕过导出闸门" in contract["note"]
    assert contract["checked"] is True

    result = await pipeline.run_full_chain(
        stage="full", brief="x", executor_fn=_good_executor
    )
    assert "persisted" not in result and "scheme_id" not in result, (
        "services 层不得擅自落库；落库由 routers 层既有单一写入口承担"
    )


# ===================== 6. 遗留收口加固（契约锁） =====================
def test_process_cleanup_pid_alive_forces_utf8_subprocess():
    """Windows cp936 解码 tasklist 输出会抛 UnicodeDecodeError（环境型告警根因）。

    与 ``export.py``(soffice) / ``mermaid_service.py``(mmdc) 同口径：所有
    ``subprocess.run(text=True)`` 必须显式 ``encoding`` + ``errors="replace"``。
    源码锁而非运行时探针：本机不一定能复现非 GBK 进程名，源码契约可稳定判别。
    """
    import inspect

    from app.utils import process_cleanup as pc

    src = inspect.getsource(pc._pid_alive)
    assert "text=True" in src
    assert "encoding=" in src and 'errors="replace"' in src


def test_agent_engine_contract_retry_from_config_and_async_execute():
    """双轨契约：重试次数只能来自 ``AgentEngineConfig``，路由执行必须是协程。

    防两类回归：① 给执行器加会覆盖配置的 ``max_retries`` 构造旋钮（违反
    「阈值/重试均从配置读取」架构决策）；② 把 ``AgentRouter.execute`` 改回
    同步调用（会静默产出未 await 的 coroutine，护栏恒假红）。
    """
    import inspect

    pytest.importorskip("app.services.ai.agent_executor")
    from app.services.ai import agent_executor as ae

    params = inspect.signature(ae.OmpAgentExecutor.__init__).parameters
    assert "max_retries" not in params
    assert "config" in params
    assert "max_retries" in ae.AgentEngineConfig.__dataclass_fields__
    assert inspect.iscoroutinefunction(ae.AgentRouter.execute)


def test_text_mode_subprocess_always_forces_explicit_encoding():
    """全仓棘轮：subprocess 文本模式必须显式 ``encoding``（``errors`` 兜底）。

    Windows cp936 严格解码会在后台 ``_readerthread`` 抛 UnicodeDecodeError，
    产出 ``PytestUnhandledThreadExceptionWarning`` 并吞掉 stdout（探活/校验失真）。
    覆盖 ``app`` 与 ``tests`` 全量，防止 R249 收口后同类漏网面复发。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for base in ("app", "tests"):
        for path in (root / base).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
                if name not in ("run", "Popen", "check_output", "call", "check_call"):
                    continue
                kws = {k.arg for k in node.keywords}
                if not (kws & {"text", "universal_newlines", "encoding"}):
                    continue
                if "encoding" not in kws:
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, "文本模式 subprocess 缺显式 encoding：\n" + "\n".join(offenders)
