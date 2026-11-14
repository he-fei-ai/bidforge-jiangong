"""双轨 Agent 引擎 · 配置契约与真 omp 就绪预检（R240 · 遗留项加固）。

回答两个此前只能靠「人工约定」的问题：

1. **未知配置键**：运维写错 ``AGENT_ENGINE_*`` 键名时，pydantic-settings 会
   静默忽略，配置看起来「改了」却完全不生效。:func:`unknown_env_keys` 从
   「配置快照字段 + Settings 字段」**单一事实源**派生合法键集合，把错别字与
   遗留死键点名出来，挂到只读诊断端点。
2. **真 omp 验收前置条件**：:func:`readiness` 把《集成 oh-my-pi_OMP_AGENT
   可行性报告》§8「阶段 0 闸门」做成可执行只读自检。它**不执行验收**，
   只判定前置条件；``ready=false`` 时 ``acceptance_allowed=false`` ——
   防止把「omp 不可用 ⇒ 回退 Pi 成功」包装成「真 omp 跑通」。

⚠️ 零写入、零子进程、零网络、零业务库；凭据只回报「是否可读 / 是否为合法
   JSON 对象」，**绝不回传路径或内容**；工具白名单判定复用
   ``omp_safety.check_tool_allowlist``（R235 单一事实源），二进制判定复用
   ``omp_rpc.binary_exists``，不另造第二份判据。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from .config import (
    ENV_PREFIX,
    IMPL_REAL,
    MODE_OMP,
    DualTrackConfig,
    load_dual_track_config,
)

logger = logging.getLogger("ai_provider.agent_engine.dual_track")

#: 闸门顺序（诊断面稳定输出顺序，不是业务阈值）。
_GATE_NAMES = ("binary", "auth", "mode", "executor_impl", "tools", "config_keys")


def _setting(key: str, default: Any = None) -> Any:
    """读取 Settings 字段；配置中心不可导入时静默回落（不阻断诊断面）。"""
    try:
        from app.config import settings

        return getattr(settings, key, default)
    except Exception:  # noqa: BLE001 — 诊断面不得因配置模块故障整体不可用
        return default


def known_env_keys() -> set[str]:
    """合法键集合（单一事实源）：配置快照字段 + Settings 的 agent_engine_* 字段。

    ⚠️ 新增配置字段会**自动**纳入，护栏不会随配置演进而漂移；R239 单轨的
       ``agent_engine_primary`` / ``agent_engine_fallback_order`` 等同样合法。
    """
    names = {ENV_PREFIX + f.upper() for f in DualTrackConfig.__dataclass_fields__}
    try:
        from app.config import settings

        holder = type(settings)
        fields = getattr(holder, "model_fields", None) or getattr(holder, "__fields__", {}) or {}
        for field in fields:
            name = str(field)
            if name.startswith("agent_engine_"):
                names.add(ENV_PREFIX + name[len("agent_engine_") :].upper())
    except Exception as exc:  # noqa: BLE001 — 极端环境下仍有快照字段兜底
        logger.warning("agent_engine.dual_track: Settings 字段枚举不可用（%s）", exc)
    return names


def unknown_env_keys(env: Mapping[str, str] | None = None) -> list[str]:
    """列出拼错 / 遗留的 ``AGENT_ENGINE_*`` 键，让静默失效变成可见告警。"""
    source = os.environ if env is None else env
    known = known_env_keys()
    return sorted(
        key
        for key in source
        if key.upper().startswith(ENV_PREFIX) and key.upper() not in known
    )


def readiness() -> dict[str, Any]:
    """真 omp 验收前置条件自检（只读；不拉起 omp、不代替验收）。

    返回 ``ready`` / ``acceptance_allowed`` / ``blocking_gates`` / ``reason`` /
    ``gates`` / ``note``。只有全部闸门通过才允许声称「具备真 omp 验收条件」，
    但**通过预检本身也不等于验收完成** —— 真实 compaction 与工具调用仍须
    在测试环境按阶段 0 四闸门逐条实测。
    """
    cfg = load_dual_track_config()
    gates: dict[str, dict[str, Any]] = {}

    # 闸门 1：二进制在位（复用 omp_rpc 单一事实源，不自行判路径）
    try:
        from app.services.ai.omp_rpc import binary_exists

        binary_ok = bool(binary_exists(cfg.omp_binary_path))
        binary_hint = (
            "" if binary_ok else
            "omp 二进制不存在或不可执行：请安装 omp 并配置 AGENT_ENGINE_OMP_BINARY_PATH 绝对路径"
        )
    except Exception as exc:  # noqa: BLE001
        binary_ok = False
        binary_hint = f"二进制探测不可用：{type(exc).__name__}: {exc}"
    gates["binary"] = {"ok": binary_ok, "hint": binary_hint}

    # 闸门 2：Provider 授权（只回报可读性 / 合法性，绝不回传路径或内容）
    auth_path = cfg.provider_auth
    if not auth_path:
        gates["auth"] = {
            "ok": False,
            "status": "unknown",
            "hint": (
                "未注入凭据（沿用 omp 自身 models.yml），平台侧无法确认授权；"
                "如需平台侧可判定，请配置 AGENT_ENGINE_PROVIDER_AUTH"
            ),
        }
    else:
        path = Path(auth_path)
        readable = path.is_file()
        valid = False
        if readable:
            try:
                valid = isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
            except (OSError, ValueError):
                valid = False
        gates["auth"] = {
            "ok": readable and valid,
            "status": "ok" if readable and valid else "failed",
            "readable": readable,
            "valid_json": valid,
            "hint": "" if readable and valid else "凭据文件不可读或不是 JSON 对象",
        }

    # 闸门 3：引擎模式（真 omp 验收必须显式切到 omp）
    gates["mode"] = {
        "ok": cfg.mode == MODE_OMP,
        "value": cfg.mode,
        "hint": "" if cfg.mode == MODE_OMP else "当前非 omp 模式；验收前请设 AGENT_ENGINE_MODE=omp",
    }

    # 闸门 4：执行器实现（路径 B 的 mock 档不得声称真 omp 验收）
    gates["executor_impl"] = {
        "ok": cfg.omp_executor_impl == IMPL_REAL,
        "value": cfg.omp_executor_impl,
        "hint": (
            "" if cfg.omp_executor_impl == IMPL_REAL else
            "当前注入 MockOmpExecutor（路径 B POC），不得据此验收真 omp"
        ),
    }

    # 闸门 5：工具白名单 fail-closed（复用 R235 单一事实源）
    try:
        from app.services.ai.omp_safety import check_tool_allowlist

        verdict = check_tool_allowlist(str(_setting("omp_task_tools", "") or ""))
        gates["tools"] = {
            "ok": bool(verdict["ok"]),
            "allowed": list(verdict["allowed"]),
            "problems": list(verdict["problems"]),
        }
    except Exception as exc:  # noqa: BLE001
        gates["tools"] = {
            "ok": False,
            "allowed": [],
            "problems": [f"白名单判定不可用：{type(exc).__name__}: {exc}"],
        }

    # 闸门 6：未知配置键（错键会静默失效，属阻塞项而非提示项）
    unknown = unknown_env_keys()
    gates["config_keys"] = {
        "ok": not unknown,
        "unknown": unknown,
        "hint": "" if not unknown else f"存在无法被读取的配置键：{', '.join(unknown)}",
    }

    blocking = [name for name in _GATE_NAMES if not gates[name]["ok"]]
    ready = not blocking
    reason = (
        "前置条件齐备：可在测试环境按阶段 0 四闸门执行真 omp 的 compaction / "
        "工具调用验收（本预检不代替验收）"
        if ready else
        "真 omp 验收前置条件未齐备（阻塞闸门：" + "、".join(blocking) + "）；"
        "回退成功 ≠ 真 omp 跑通，此时不得声称验收通过"
    )
    return {
        "ready": ready,
        "acceptance_allowed": ready,
        "blocking_gates": blocking,
        "reason": reason,
        "gates": gates,
        "note": (
            "本预检只读、不拉起 omp、不产生验收结论；ready=false 时任何"
            "「真 omp 上下文压缩 / 工具调用已通过」的结论均不成立。"
        ),
    }
