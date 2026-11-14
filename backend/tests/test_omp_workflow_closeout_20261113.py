"""阶段 3 遗留项收口护栏（2026-11-13）。

针对上一轮登记的剩余项，逐条做行为级实证：
  1. 空产物 / 未检查被渲染成「可导出通过」（假成功）；
  2. 前序阶段失败仍空跑下游、无失败传播；
  3. 部分章节缺失不报 missing_sections（静默丢章）；
  4. 目录校验漏判「L1~L3 未展开子节点」；
  5. 端点缺配置预检（二进制/模型缺失被编排层吞成假成功）；
  6. `system_prompt` 覆盖参数是死旋钮；
  7. 审核工作区在 run_batch 异常时泄漏；
  8. 进程内 INFLIGHT 集合无跨 worker 保护；
  9. 无可执行的就绪诊断；
 10. 导出契约未复用既有 audit_content 判据；
 11. 原生 task / bash 白名单与分层契约棘轮。

⚠️ 所有外部依赖均延迟导入，避免收集期 ImportError 阻断整文件。
"""

from __future__ import annotations

import asyncio
import json
import os
import re

import pytest
from app.services.ai import omp_agent_roles as roles
from app.services.ai import omp_pipeline as pipeline
from app.services.ai import omp_workflow as wf

_SERVICES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "services", "ai"
)


def _valid_tree():
    def l4(k):
        return [{"title": f"工艺{k}-{j}", "level": 4, "children": []} for j in range(1, 5)]

    def l3(m):
        return [{"title": f"环节{m}-{k}", "level": 3, "children": l4(k)} for k in range(1, 4)]

    def l2(i):
        return [{"title": f"分部{i}-{m}", "level": 2, "children": l3(m)} for m in range(1, 4)]

    return [{"title": f"工程要素{i}", "level": 1, "children": l2(i)} for i in range(1, 2)]


def _body(budget: int) -> str:
    unit = "本工程采用分段施工工艺并严格控制质量验收。"
    return unit * (int(budget) // len(unit) + 2)


async def _good_executor(spec, prompt, workspace):  # noqa: ANN001
    role = spec.metadata.get("role")
    key = role.key if role is not None else ""
    if key == "outline":
        return json.dumps({"sections": _valid_tree()}, ensure_ascii=False)
    if key == "content":
        return _body(spec.metadata.get("budget") or 300)
    return json.dumps({"fixes": []}, ensure_ascii=False)


# ===================== 1. 空产物不得宣称可导出 =====================
async def test_empty_documents_must_not_claim_export_ready():
    result = await pipeline.run_full_chain(
        stage="audit", documents={}, executor_fn=_good_executor
    )
    ec = result["export_contract"]
    assert ec["ready"] is False
    assert ec["checked"] is False
    assert ec["undetermined"] is True
    assert ec["undetermined_reason"]


# ===================== 2. 前序失败必须阻断下游 =====================
async def test_failed_outline_blocks_downstream_stages():
    async def bad(spec, prompt, workspace):  # noqa: ANN001
        return "{BROKEN"

    result = await pipeline.run_full_chain(stage="full", brief="x", executor_fn=bad)
    assert result["status"] == "blocked"
    assert [s["stage"] for s in result["stages"]] == ["outline"]
    assert "目录" in result["blocked_reason"]
    assert result["export_contract"]["ready"] is False


# ===================== 3. 部分章节缺失必须显式登记 =====================
async def test_partial_content_reports_missing_sections():
    leaves = pipeline._flatten_leaves(_valid_tree())[:4]

    async def partial(spec, prompt, workspace):  # noqa: ANN001
        role = spec.metadata.get("role")
        if role is not None and role.key == "content" and spec.task_id.endswith(".4"):
            return ""  # 该节产物缺失
        return await _good_executor(spec, prompt, workspace)

    result = await pipeline.run_full_chain(
        stage="content", leaves=leaves, documents={}, executor_fn=partial
    )
    ec = result["export_contract"]
    assert ec["missing_sections"], "缺章必须显式登记，不能静默丢弃"
    assert ec["expected_document_count"] == len(leaves)
    assert ec["ready"] is False
    assert result["status"] in ("degraded", "blocked")


# ===================== 4. 目录校验：未展开节点漏判 =====================
def test_verify_outline_rejects_unexpanded_node():
    tree = [{"title": "工程概况", "level": 1, "children": []}]
    issues = roles.verify_outline(tree)
    assert any("子节点 0 个" in i for i in issues)


# ===================== 5. 基类公开 check_ready =====================
def test_base_agent_has_public_check_ready():
    from app.services.ai.omp_task_agent import OmpTaskAgent

    assert hasattr(OmpTaskAgent, "check_ready")
    agent = OmpTaskAgent(enabled=False)
    with pytest.raises(RuntimeError):
        agent.check_ready()


# ===================== 6. system_prompt 覆盖必须真正下传 =====================
async def test_executor_merges_system_prompt_override(monkeypatch):
    import app.services.ai.omp_rpc as rpc_mod
    import app.services.ai.omp_task_agent as agent_mod

    captured: dict = {}

    class _FakeSession:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        async def start(self):
            return None

        async def run_prompt(self, text):
            return ""

        async def stop(self, **kwargs):
            return None

    monkeypatch.setattr(rpc_mod, "OmpRpcSession", _FakeSession)
    monkeypatch.setattr(agent_mod.OmpTaskAgent, "check_ready", lambda self: None)
    spec = wf.OmpTaskSpec(
        task_id="t1",
        instruction="做事",
        metadata={"role": roles.OUTLINE_ROLE, "system_prompt": "OVERRIDE_MARK"},
    )
    await wf.default_omp_executor(spec, "prompt", os.path.join("j:", os.sep, "tmp"))
    assert "OVERRIDE_MARK" in (captured.get("system_prompt") or "")
    assert "事实铁律" in (captured.get("system_prompt") or "")


# ===================== 7. 端点配置预检 422 =====================
async def test_endpoint_preflight_rejects_missing_config(monkeypatch):
    import app.config as cfg
    from app.routers import sections as sec_mod
    from app.services.ai import omp_pipeline

    monkeypatch.setattr(cfg.settings, "omp_task_enabled", True)
    monkeypatch.setattr(cfg.settings, "omp_workflow_enabled", True)
    monkeypatch.setattr(sec_mod, "ensure_no_generation_write", lambda *a, **k: None)
    monkeypatch.setattr(sec_mod, "content_generation_in_progress", lambda _sid: None)
    monkeypatch.setattr(sec_mod, "outline_generation_in_progress", lambda _sid: None)
    called = []

    async def _chain(**kwargs):
        called.append(kwargs)
        return {}

    monkeypatch.setattr(omp_pipeline, "run_full_chain", _chain)
    with pytest.raises(Exception) as ei:
        await sec_mod.run_omp_workflow(
            "s1", {"stage": "audit", "brief": "x", "documents": {"a": "t"}}, None
        )
    assert ei.value.status_code == 422
    assert "配置不完整" in str(ei.value.detail)
    assert not called, "预检失败时绝不能进入编排链"
    assert "s1" not in sec_mod._OMP_WORKFLOW_INFLIGHT


# ===================== 8. 跨 worker 文件租约 =====================
def test_cross_worker_lease_conflict_and_release(tmp_path):
    from app.services.ai.omp_lease import OmpWorkflowLease

    root = str(tmp_path / "leases")
    a = OmpWorkflowLease("scheme-A", root=root)
    b = OmpWorkflowLease("scheme-A", root=root)
    first = a.acquire()
    assert first.status == "acquired" and first.token
    assert b.acquire().status == "conflict"
    assert b.release(first.token) is False, "不得用他人 token 释放锁"
    assert a.release(first.token) is True
    assert b.acquire().status == "acquired"


def test_cross_worker_lease_ttl_reclaims_stale(tmp_path):
    from app.services.ai import omp_lease

    root = str(tmp_path / "leases")
    a = omp_lease.OmpWorkflowLease("scheme-B", root=root, ttl_seconds=1)
    first = a.acquire()
    assert first.status == "acquired"
    # 人为把锁文件时间写成过期，模拟持锁进程崩溃后的僵尸锁
    path = a._lock_path
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"token": "dead", "pid": -1, "ts": 1.0}))
    b = omp_lease.OmpWorkflowLease("scheme-B", root=root, ttl_seconds=1)
    assert b.acquire().status == "acquired", "过期租约必须可被回收"


# ===================== 9. 就绪诊断端点 =====================
async def test_readiness_endpoint_reports_gaps():
    from app.routers import system as sys_mod

    res = await sys_mod.omp_readiness_get()
    assert res["ready"] is False
    assert res["gaps"]
    assert "binary_exists" in res and "model_configured" in res
    assert res["native_task_enabled"] is False


# ===================== 10. 导出契约复用既有 audit_content =====================
async def test_endpoint_reuses_existing_content_audit(monkeypatch):
    import app.config as cfg
    from app.routers import sections as sec_mod
    from app.services.ai import omp_pipeline
    from app.services.ai.omp_task_agent import OmpTaskAgent

    monkeypatch.setattr(cfg.settings, "omp_task_enabled", True)
    monkeypatch.setattr(cfg.settings, "omp_workflow_enabled", True)
    monkeypatch.setattr(OmpTaskAgent, "check_ready", lambda self: None)
    monkeypatch.setattr(sec_mod, "ensure_no_generation_write", lambda *a, **k: None)
    monkeypatch.setattr(sec_mod, "content_generation_in_progress", lambda _sid: None)
    monkeypatch.setattr(sec_mod, "outline_generation_in_progress", lambda _sid: None)

    async def _chain(**kwargs):
        return {"engine": "omp_agent", "stages": [], "status": "completed",
                "documents": {"s1": "正文包含 ×× 占位与 ```mermaid 未闭合围栏"}}

    monkeypatch.setattr(omp_pipeline, "run_full_chain", _chain)
    res = await sec_mod.run_omp_workflow(
        "s1", {"stage": "audit", "brief": "x", "documents": {"s1": "t"}}, None
    )
    assert res["export_gate"]["delegate_checked"] is True
    assert "total" in res["export_gate"]["content_audit"]
    assert "items" in res["export_gate"]["content_audit"]


def test_export_issue_scan_first_touch_import_is_safe():
    """R198 断环行为锁：首触 _export_issue_scan 不得 ImportError。"""
    import app.routers._export_issue_scan as scan

    assert scan.audit_content([]) == {"total": 0, "items": []}


# ===================== 11. 审核工作区异常回收 =====================
async def test_audit_workspace_cleaned_when_run_batch_raises(monkeypatch):
    import app.services.ai.omp_audit_agent as audit_mod

    made: list[str] = []
    real_mkdtemp = audit_mod.tempfile.mkdtemp

    def _tracking_mkdtemp(*args, **kwargs):
        p = real_mkdtemp(*args, **kwargs)
        made.append(p)
        return p

    async def _boom(self, request):  # noqa: ANN001
        raise RuntimeError("模拟 RPC 超时")

    monkeypatch.setattr(audit_mod.tempfile, "mkdtemp", _tracking_mkdtemp)
    monkeypatch.setattr(wf.OmpBatchOrchestrator, "run_batch", _boom)
    agent = audit_mod.OmpAuditAgent()
    with pytest.raises(RuntimeError):
        await agent.audit(documents={"s1": "正文"}, facts="", executor_fn=_good_executor)
    assert made and not os.path.isdir(made[0]), "异常路径必须回收工作区"


# ===================== 12. 设计取舍棘轮（防后续顺手改掉） =====================
def test_security_and_layering_ratchets():
    import app.config as cfg
    from app.routers import system as sys_mod

    tools = [t.strip() for t in cfg.settings.omp_task_tools.split(",") if t.strip()]
    # 2026-11-13 · 放开命令执行：bash 已加入默认白名单
    assert "bash" in tools
    assert "task" not in tools, "原生 task 未经实测不得默认启用"
    assert cfg.settings.omp_workflow_enabled is False
    # services 层不得 import routers / export 渲染链
    pat = re.compile(r"^\s*(?:from|import)\s+\S*(?:routers|export\.py)", flags=re.M)
    for name in ("omp_agent_roles.py", "omp_workflow.py", "omp_content_agent.py",
                 "omp_audit_agent.py", "omp_pipeline.py", "omp_lease.py"):
        with open(os.path.join(_SERVICES_DIR, name), "rb") as f:
            assert not pat.search(f.read().decode("utf-8")), name
    assert callable(sys_mod.feature_flags_get)


async def test_feature_flags_shape_unchanged():
    """加法式新增 omp-readiness，不得污染既有精确形状断言。"""
    from app.routers.system import feature_flags_get

    assert await feature_flags_get() == {"omp_task_enabled": False}


def test_new_backend_sources_byte_health():
    for name in ("omp_lease.py",):
        with open(os.path.join(_SERVICES_DIR, name), "rb") as f:
            raw = f.read()
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert b"\xef\xbf\xbd" not in raw
        assert raw.count(b"\r\n") == raw.count(b"\n"), "后端源码须保持纯 CRLF"


async def test_full_chain_success_still_ready():
    """回归锚：全链路成功时 ready 必须仍为 True（防加固过度）。"""
    result = await pipeline.run_full_chain(
        stage="full", brief="某桥梁专项施工方案", facts="", executor_fn=_good_executor
    )
    assert result["status"] == "completed"
    assert result["export_contract"]["ready"] is True
    assert result["export_contract"]["checked"] is True
    assert all(result["export_contract"]["checks"].values())
