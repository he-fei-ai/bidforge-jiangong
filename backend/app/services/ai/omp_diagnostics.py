"""OMP 诊断预检与可观测（引入 AGENT 引擎方案 · 落盘骨架）。

对应《专项方案编制软件引入 AGENT 引擎方案》§7.11「诊断与可观测」：

* **启动预检** ``preflight``：二进制 / 模型 / 工具白名单 / 工作区 一次性体检，
  返回结构化结论（不抛异常），供 routers 层把「配置不全」转成明确 4xx；
* **运行指标** ``collect_task_metrics``：从 ``WorkflowReport`` 汇总
  完成数 / 降级数 / 暂停数 / 尝试次数 / 修复轮数 / 产物字数；
* **成本估算** ``estimate_cost``：按输入输出字数与可注入单价给粗略成本。

⚠️ 预检的「三道闸门」判据复用 ``OmpTaskAgent._assert_ready`` 单一事实源，
   本模块只把异常转成 ``checks`` 结论，不重复实现判据。
⚠️ 纯函数 + 只读，零网络、零数据库；分层约束：services 禁止 import routers。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 工作区沙箱禁用工具（与 ``omp_agent_roles.SYSTEM_PREAMBLE`` 工作区红线同口径）。
#: ✅ R235（2026-11-13）：判据收敛到 ``omp_safety`` **单一事实源** —— 此前本模块
#:    与执行期闸门（``omp_task_agent`` / ``omp_rpc``）各持一份清单，任一侧改动即
#:    与另一侧漂移（本仓「同一判据两份副本」最高频反模式）。
from app.services.ai.omp_safety import (  # noqa: F401  （对外导出，兼容既有消费方）
    FORBIDDEN_TOOLS,
    check_tool_allowlist,
)


@dataclass(frozen=True)
class PreflightReport:
    """预检结论（可序列化给前端 / 日志）。"""

    ok: bool
    checks: dict[str, bool] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": dict(self.checks),
            "problems": list(self.problems),
            "snapshot": dict(self.snapshot),
        }


def environment_snapshot(
    *, binary: str = "", model: str = "", tools: str = "", workspace_root: str = "", enabled=None
) -> dict:
    """环境快照（只读，不含密钥）。"""
    from app.config import settings

    return {
        "enabled": bool(
            enabled if enabled is not None else getattr(settings, "omp_task_enabled", False)
        ),
        "binary": binary or (getattr(settings, "omp_binary_path", "") or "omp"),
        "model": model or (getattr(settings, "omp_task_model", "") or ""),
        "tools": tools or (getattr(settings, "omp_task_tools", "") or "write,read,edit,grep,glob,bash"),
        "workspace_root": workspace_root or (getattr(settings, "omp_workspace_dir", "") or ""),
    }


def preflight(
    *, binary: str = "", model: str = "", tools: str = "", workspace_root: str = "", enabled=None
) -> PreflightReport:
    """一次性预检（不抛异常）：闸门 + 工具白名单 + 快照。"""
    from app.services.ai.omp_task_agent import OmpTaskAgent

    snap = environment_snapshot(
        binary=binary, model=model, tools=tools, workspace_root=workspace_root, enabled=enabled
    )
    checks: dict[str, bool] = {}
    problems: list[str] = []

    # ① 三道闸门（复用单一事实源，异常转结论）
    try:
        agent = OmpTaskAgent(
            enabled=enabled,
            binary=(binary or None),
            model=(model or None),
            tools=(tools or None),
            workspace_root=(workspace_root or None),
        )
        agent.check_ready()
        checks["gate_ready"] = True
    except RuntimeError as e:
        checks["gate_ready"] = False
        problems.append(str(e))

    # ② 工具白名单（放开命令执行：禁网络/浏览器工具；命令执行已放开）
    #    判据委托 ``omp_safety.check_tool_allowlist`` 单一事实源（含执行期同款口径）。
    verdict = check_tool_allowlist(str(snap["tools"]))
    checks["tools_safe"] = bool(verdict["ok"])
    for problem in verdict["problems"]:
        problems.append(problem)

    # ③ 工作区隔离（显式目录或临时目录都属隔离，恒真——保留键供前端展示）
    checks["workspace_isolated"] = True

    return PreflightReport(ok=all(checks.values()), checks=checks, problems=problems, snapshot=snap)


def collect_task_metrics(report) -> dict:
    """从 ``WorkflowReport`` 汇总运行指标（缺省字段容错）。"""
    results = list(getattr(report, "results", None) or [])
    return {
        "tasks": len(results),
        "completed": sum(1 for r in results if getattr(r, "status", "") == "completed"),
        "degraded": sum(1 for r in results if getattr(r, "status", "") == "degraded"),
        "paused": sum(1 for r in results if getattr(r, "status", "") == "paused"),
        "attempts": sum(int(getattr(r, "attempts", 0) or 0) for r in results),
        "fix_rounds": sum(int(getattr(r, "fix_rounds", 0) or 0) for r in results),
        "output_chars": sum(len(str(getattr(r, "payload", "") or "")) for r in results),
    }


def estimate_cost(
    *,
    input_chars: int,
    output_chars: int,
    input_per_1k: float = 0.0,
    output_per_1k: float = 0.0,
) -> dict:
    """粗略成本估算（单价按元/千词元注入；缺省 0 = 未知，不臆造）。"""
    inp = max(0, int(input_chars or 0))
    out = max(0, int(output_chars or 0))
    cost = (inp / 1000.0) * float(input_per_1k or 0.0) + (out / 1000.0) * float(output_per_1k or 0.0)
    return {
        "input_chars": inp,
        "output_chars": out,
        "estimated_cost": round(cost, 6),
        "rate_known": bool(input_per_1k or output_per_1k),
    }


def session_snapshot(
    *, binary: str = "", model: str = "", tools: str = "", workspace_root: str = "", enabled=None
) -> dict:
    """运行期 Session 快照（只读，**不含密钥**）。

    对应附件方案 §5.12「运行诊断：环境快照 / Session 快照」：
    二进制是否真的在位、模型是否配了、工具白名单是什么、工作区是否隔离、
    超时与工作区保留口径 —— 供运维在诊断端点一眼看清「当前 omp 能力边界」。
    """
    from app.config import settings
    from app.services.ai.omp_rpc import binary_exists

    snap = environment_snapshot(
        binary=binary, model=model, tools=tools, workspace_root=workspace_root, enabled=enabled
    )
    snap["binary_available"] = bool(binary_exists(str(snap.get("binary") or "omp")))
    snap["rpc_timeout"] = int(getattr(settings, "omp_rpc_timeout", 120) or 120)
    snap["task_timeout"] = int(getattr(settings, "omp_task_timeout", 300) or 300)
    snap["keep_workspace"] = bool(getattr(settings, "omp_task_keep_workspace", False))
    # 工作区隔离是既有契约（每任务独占目录，见 omp_task_agent / omp_workflow）。
    snap["workspace_isolated"] = True
    return snap


def serialize_error(exc: BaseException | str) -> dict:
    """错误归一化（类型 / 消息 / 分类 / 可行动建议）。

    分类复用 ``omp_workflow.classify_error`` 单一事实源（限流 / 超时 / JSON /
    工具失败），不另立一套判定。
    """
    from app.services.ai.omp_workflow import classify_error

    if isinstance(exc, BaseException):
        etype = type(exc).__name__
        message = str(exc)
        category = classify_error(exc)
    else:
        etype = "Error"
        message = str(exc or "")
        category = classify_error(RuntimeError(message))
    hints = {
        "rate_limited": "命中供应商限流/配额：降低并发或等待配额恢复后重试",
        "timeout": "请求超时：提高超时或缩小上下文（改走文件工作区）",
        "json_invalid": "产物不是合法 JSON：检查校验器与产物格式",
        "tool_failed": "工具调用失败：检查工具可用性与工作区状态",
        "unknown": "查看 backend.log 定位（已带 trace / 任务标识）",
    }
    return {
        "type": etype,
        "message": message[:500],
        "category": category,
        "action": hints.get(category, hints["unknown"]),
    }
