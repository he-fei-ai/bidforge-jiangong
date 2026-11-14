"""OMP 运维诊断路由（引入 oh-my-pi 底层 Agent 引擎 · 可观测闭环）。

暴露三组**只读**诊断面（对应《专项方案编制软件引入 oh-my-pi 底层 Agent 引擎.md》
§5.12「诊断与可观测」+ §8「成本失控」）：

* ``GET /api/v1/omp/diagnostics`` —— 启动预检结论 + 环境快照 + 沙箱契约自检
  + 单轨网关配置 + **R240 双轨层配置与三链路路由预演**（``dual_track`` 字段）；
* ``GET /api/v1/omp/safety``     —— 工具白名单判定 / 红线覆盖 / 禁用工具清单；
* ``GET|POST /api/v1/omp/metrics`` —— 运行指标汇总 + 成本估算 + 阈值告警。

⚠️ 全程**只读**：零业务库访问、零子进程拉起、零工作区写入；不注册任何写端点。
⚠️ omp 未启用时端点**照常可达**并如实回报 ``enabled=false``：排障需要看到判据，
   404 会把「服务没起」与「配置缺失」混为一谈（与既有 ``/api/v1/health``
   的迁移状态暴露同款思路）。
⚠️ 默认态对既有链路零影响：本路由不触碰任何既有判据与写路径。
⚠️ 分层约束：routers 只做参数编排，判据全部在 ``app.services.ai.omp_*``。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["omp-ops"])


class _ResultShim:
    """把请求体里的任务结果字典适配成指标汇总所需的对象形态。"""

    __slots__ = ("status", "attempts", "fix_rounds", "payload")

    def __init__(self, raw: dict):
        data = raw if isinstance(raw, dict) else {}
        self.status = str(data.get("status") or "")
        try:
            self.attempts = int(data.get("attempts") or 0)
        except (TypeError, ValueError):
            self.attempts = 0
        try:
            self.fix_rounds = int(data.get("fix_rounds") or 0)
        except (TypeError, ValueError):
            self.fix_rounds = 0
        self.payload = str(data.get("payload") or "")


class _ReportShim:
    """``collect_task_metrics`` 期望的报告形态（只读适配器）。"""

    __slots__ = ("results",)

    def __init__(self, results: list):
        self.results = list(results or [])


def _cost_payload(
    *,
    input_chars: int,
    output_chars: int,
    input_per_1k: float,
    output_per_1k: float,
    threshold: float,
) -> dict:
    """成本估算 + 阈值告警（阈值缺省 0 = 不告警，不臆造单价）。"""
    from app.services.ai.omp_diagnostics import estimate_cost

    cost = estimate_cost(
        input_chars=input_chars,
        output_chars=output_chars,
        input_per_1k=input_per_1k,
        output_per_1k=output_per_1k,
    )
    limit = max(0.0, float(threshold or 0.0))
    estimated = float(cost.get("estimated_cost") or 0.0)
    alarm = bool(limit > 0 and estimated >= limit)
    out = dict(cost)
    out["threshold"] = limit
    out["alarm"] = alarm
    out["alarm_message"] = (
        f"成本估算 {estimated} 已达阈值 {limit}（请复核灰度比例与上下文体积）" if alarm else ""
    )
    return out


def _dual_track_diagnostic() -> dict:
    """R240 双轨层只读诊断：脱敏配置 + 三链路路由预演 + 真 omp 就绪预检。

    ⚠️ 只调用 services 层纯函数（零执行、零写入、零子进程、零业务库）；
       预演使用的固定示例 ID 仅用于演示稳定哈希分桶，不代表任何真实项目；
       readiness 只判定前置条件，绝不产生「真 omp 验收通过」结论。
    ⚠️ 收敛式容错：双轨层不可导入时如实回报 ``available=false`` 与错误原因，
       绝不拖垮整个诊断端点（排障时「服务没起」与「配置缺失」必须可分）。
    """
    import logging

    try:
        from app.services.ai.agent_engine.dual_track import AgentRouter, AgentTask
        from app.services.ai.agent_engine.dual_track import (
            public_config as dual_public_config,
        )
        from app.services.ai.agent_engine.dual_track import (
            readiness as dual_readiness,
        )

        router = AgentRouter()
        preview = []
        for task_type in ("outline", "content", "audit"):
            task = AgentTask(task_id=f"preview-{task_type}", task_type=task_type)
            preview.append({"task_type": task_type, **router.route_plan(task)})
        return {
            "available": True,
            "config": dual_public_config(),
            "route_preview": preview,
            "readiness": dual_readiness(),
            "preview_only": True,
        }
    except Exception as exc:  # noqa: BLE001 — 诊断面不得因单层故障整体不可用
        logging.getLogger("ai_provider.omp_ops").warning(
            "omp_ops: 双轨层诊断不可用：%s: %s", type(exc).__name__, exc
        )
        return {
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
            "route_preview": [],
            "preview_only": True,
        }


@router.get("/omp/diagnostics")
async def omp_diagnostics():
    """omp 启动预检 + 环境快照 + 沙箱契约自检（只读）。

    R239 追加：``engine_config``（单轨网关脱敏配置快照）与 ``paused``（挂起任务列表），
    把 agent_engine 层从「服务层就绪」提升到「功能可达」，供运维排障。
    R240 追加：``dual_track``（双轨层脱敏配置 + 目录/正文/审核三条链路的路由预演
    + 未知配置键点名 + 真 omp 验收前置就绪预检），让「可开关 / 可回退 / 可灰度」
    在运维面真实可查，而非只存在于服务层；ready=false 时不得声称验收通过。
    """
    from app.services.ai.agent_engine import get_gateway, public_config
    from app.services.ai.omp_diagnostics import preflight, session_snapshot
    from app.services.ai.omp_safety import safety_summary

    report = preflight()
    gw = get_gateway()
    return {
        "enabled": bool(report.snapshot.get("enabled")),
        "ok": bool(report.ok),
        "checks": dict(report.checks),
        "problems": list(report.problems),
        "snapshot": dict(report.snapshot),
        "session": session_snapshot(),
        "safety": safety_summary(),
        # R239 可达性收口：单轨网关配置与挂起任务列表（只读，零写入）
        "engine_config": public_config(),
        "paused": gw.list_paused(),
        # R240 可达性收口：双轨层配置与路由预演（只读，零写入）
        "dual_track": _dual_track_diagnostic(),
    }


@router.get("/omp/safety")
async def omp_safety():
    """沙箱契约自检（工具白名单 / 红线覆盖 / 禁用工具清单）。"""
    from app.services.ai.omp_safety import safety_summary

    return safety_summary()


@router.get("/omp/metrics")
async def omp_metrics(
    input_chars: int = 0,
    output_chars: int = 0,
    input_per_1k: float = 0.0,
    output_per_1k: float = 0.0,
    threshold: float = 0.0,
):
    """按字数估算运行成本（单价缺省 = 未知，不臆造）。"""
    return _cost_payload(
        input_chars=input_chars,
        output_chars=output_chars,
        input_per_1k=input_per_1k,
        output_per_1k=output_per_1k,
        threshold=threshold,
    )


@router.post("/omp/metrics")
async def omp_metrics_post(
    body: dict | None = None,
    input_per_1k: float = 0.0,
    output_per_1k: float = 0.0,
    threshold: float = 0.0,
):
    """汇总一批任务结果指标（完成/降级/暂停/尝试/修复轮/产物字数）+ 成本估算。

    请求体形如 ``{"results": [{"status": "completed", "attempts": 1,
    "fix_rounds": 0, "payload": "..."}]}``；缺省结果列表时只回成本估算。
    """
    from app.services.ai.omp_diagnostics import collect_task_metrics

    payload = body if isinstance(body, dict) else {}
    raw_results = payload.get("results")
    results = [_ResultShim(r) for r in raw_results] if isinstance(raw_results, list) else []
    metrics = collect_task_metrics(_ReportShim(results))
    cost = _cost_payload(
        input_chars=int(payload.get("input_chars") or 0),
        output_chars=int(payload.get("output_chars") or metrics.get("output_chars") or 0),
        input_per_1k=input_per_1k,
        output_per_1k=output_per_1k,
        threshold=threshold,
    )
    out = {"metrics": metrics}
    out.update(cost)
    return out
