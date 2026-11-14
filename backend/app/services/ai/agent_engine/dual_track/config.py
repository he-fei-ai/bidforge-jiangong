"""双轨 Agent 引擎 · 配置中心适配层（R240 · 路径 B 注入执行器 POC）。

架构约束（不可违背）：

* **零硬编码阈值**：引擎模式 / 上下文压缩阈值 / 最大重试次数 / omp 二进制
  路径 / Provider 授权路径 / 超时 / 灰度比例，一律从配置中心读取；
* **动态读取（可热更新）**：每次任务调度调用 :func:`load_dual_track_config`，
  顺序为「实时 ``os.environ`` → ``Settings``（``config.py`` + ``.env``）→
  极端兜底」。运维改环境变量后新任务立即生效，无需重启进程；
* **默认最安全**：``mode`` 缺省 ``pi``，与改造前行为一致；非法配置一律
  归一并打 WARN，绝不静默吞掉；
* **可开关 / 可回退 / 可灰度**：``enabled``（全局 Feature Flag）、
  ``fallback_enabled``（默认回退）、``mode=hybrid`` + ``gray_percent``（灰度）。

本仓配置中心是 pydantic-settings（``Settings`` + ``.env`` + 环境变量），
因此**不新增 config.yaml** —— 避免产生「代码不读的死配置」。方案文档中的
``agent_engine:`` YAML 仅为逻辑视图，实际落点是 ``AGENT_ENGINE_*`` 环境变量
与 ``Settings.agent_engine_*`` 字段。
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger("ai_provider.agent_engine.dual_track")

#: 环境变量前缀（与 ``Settings`` 字段同名大写，便于运维直接覆盖）。
ENV_PREFIX = "AGENT_ENGINE_"

MODE_PI = "pi"
MODE_OMP = "omp"
MODE_HYBRID = "hybrid"
VALID_MODES = frozenset({MODE_PI, MODE_OMP, MODE_HYBRID})

ENGINE_PI = "pi"
ENGINE_OMP = "omp"
ENGINE_MOCK = "mock_omp"

IMPL_REAL = "real"
IMPL_MOCK = "mock"
VALID_IMPL = frozenset({IMPL_REAL, IMPL_MOCK})
VALID_GRAY_KEYS = frozenset({"scheme_id", "task_id"})

# ⚠️ 仅当 ``app.config`` 完全不可导入（例如脱离后端包单独跑契约测试）时的极端兜底。
#    生产取值一律来自配置中心；这里不是可调业务参数，禁止当作阈值来源使用。
_FALLBACK_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "fallback_enabled": True,
    "mode": MODE_PI,
    "omp_binary_path": "omp",
    "context_threshold_ratio": 0.7,
    "max_retries": 2,
    "retry_backoff_base": 1.5,
    "compaction_threshold": 120000,
    "provider_auth": "",
    "task_timeout": 300,
    "gray_percent": 0,
    "gray_key": "scheme_id",
    "omp_executor_impl": IMPL_REAL,
    "mock_response_path": "",
}

#: 新键为空时回落的既有配置字段（保持「单一事实源」，不复制第二份值）。
_LEGACY_KEYS: dict[str, str] = {
    "omp_binary_path": "omp_binary_path",
    "task_timeout": "omp_task_timeout",
}

#: 非法配置告警去重，避免每个任务刷一次 WARN。
_WARNED: set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    logger.warning(message, *args)


def _blank(value: Any) -> bool:
    """空值判定：None 与空白字符串视为未配置（bool False 不算空）。"""
    return value is None or (isinstance(value, str) and not value.strip())


def _settings() -> Any | None:
    try:
        from app.config import settings

        return settings
    except Exception as exc:  # noqa: BLE001 — 配置模块不可用时不能阻断引擎层
        _warn_once(
            "settings_import",
            "agent_engine.dual_track: 配置中心不可导入，启用极端兜底值（%s）",
            exc,
        )
        return None


def _raw(name: str) -> Any:
    """按「实时环境变量 → Settings 新键 → Settings 既有键 → 兜底」读取单个配置。

    ``AGENT_ENGINE_MODE=omp`` 这类环境变量在**每次任务**时重新读取，
    这是「热更新 / 动态读取」的实现点。
    """
    env_val = os.environ.get(ENV_PREFIX + name.upper())
    if env_val is not None and not _blank(env_val):
        return str(env_val).strip()
    settings = _settings()
    if settings is not None:
        value = getattr(settings, "agent_engine_" + name, None)
        if _blank(value):
            legacy = _LEGACY_KEYS.get(name)
            value = getattr(settings, legacy, None) if legacy else None
        if not _blank(value):
            return value
    return _FALLBACK_DEFAULTS.get(name)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class DualTrackConfig:
    """引擎层配置快照（**全部字段由 :func:`load_dual_track_config` 注入**）。

    刻意不给字段设默认值：任何阈值都必须经由配置中心产生，杜绝「代码里
    顺手写个 0.7」「顺手写个 3 次重试」这类硬编码回潮。
    """

    enabled: bool
    fallback_enabled: bool
    mode: str
    omp_binary_path: str
    context_threshold_ratio: float
    max_retries: int
    retry_backoff_base: float
    compaction_threshold: int
    provider_auth: str
    task_timeout: int
    gray_percent: int
    gray_key: str
    omp_executor_impl: str
    mock_response_path: str


def load_dual_track_config() -> DualTrackConfig:
    """加载并归一化双轨配置（每次任务调用 ⇒ 动态读取、无进程级缓存）。

    归一规则：非法值一律回落到配置中心的安全缺省 + WARN，不抛异常。
    """
    # 1) 引擎模式：非法值一律回落 pi（默认回退到最安全的旧轨）。
    raw_mode = str(_raw("mode") or MODE_PI).strip().lower()
    mode = raw_mode if raw_mode in VALID_MODES else MODE_PI
    if mode != raw_mode:
        _warn_once(
            "mode_" + raw_mode,
            "agent_engine.mode=%r 非法（需 pi/omp/hybrid），已回落 %s",
            raw_mode,
            MODE_PI,
        )

    # 2) 上下文压缩阈值比率：必须落在 (0, 1]。
    raw_ratio = _raw("context_threshold_ratio")
    ratio = _as_float(raw_ratio, float(_FALLBACK_DEFAULTS["context_threshold_ratio"]))
    if not (0 < ratio <= 1):
        _warn_once(
            "ratio_" + str(raw_ratio),
            "agent_engine.context_threshold_ratio=%r 非法（需 (0,1]），已回落配置缺省",
            raw_ratio,
        )
        ratio = float(_FALLBACK_DEFAULTS["context_threshold_ratio"])

    # 3) 最大重试次数：负数非法；上限钳制防止误配打穿额度。
    raw_retries = _raw("max_retries")
    retries = _as_int(raw_retries, int(_FALLBACK_DEFAULTS["max_retries"]))
    if retries < 0:
        _warn_once(
            "retries_" + str(raw_retries),
            "agent_engine.max_retries=%r 非法（需 >=0），已回落配置缺省",
            raw_retries,
        )
        retries = int(_FALLBACK_DEFAULTS["max_retries"])
    retries = min(retries, 10)

    raw_backoff = _raw("retry_backoff_base")
    backoff = _as_float(raw_backoff, float(_FALLBACK_DEFAULTS["retry_backoff_base"]))
    if backoff <= 0:
        backoff = float(_FALLBACK_DEFAULTS["retry_backoff_base"])

    raw_compaction = _raw("compaction_threshold")
    compaction = _as_int(raw_compaction, int(_FALLBACK_DEFAULTS["compaction_threshold"]))
    if compaction <= 0:
        compaction = int(_FALLBACK_DEFAULTS["compaction_threshold"])

    # 4) omp 二进制路径：新键为空 ⇒ 回落既有 omp_binary_path（单一事实源）。
    binary = str(_raw("omp_binary_path") or "").strip() or "omp"

    # 5) 超时：来自 omp_task_timeout（或显式 AGENT_ENGINE_TASK_TIMEOUT）。
    timeout = _as_int(_raw("task_timeout"), int(_FALLBACK_DEFAULTS["task_timeout"]))
    if timeout <= 0:
        timeout = int(_FALLBACK_DEFAULTS["task_timeout"])

    # 6) 灰度：百分比钳制 [0,100]；哈希键限定白名单，避免误用随机键导致抖动。
    raw_percent = _raw("gray_percent")
    percent = _as_int(raw_percent, int(_FALLBACK_DEFAULTS["gray_percent"]))
    percent = max(0, min(100, percent))
    raw_key = str(_raw("gray_key") or "scheme_id").strip().lower()
    gray_key = raw_key if raw_key in VALID_GRAY_KEYS else "scheme_id"
    if gray_key != raw_key:
        _warn_once(
            "gray_key_" + raw_key,
            "agent_engine.gray_key=%r 非法（需 scheme_id/task_id），已回落 scheme_id",
            raw_key,
        )

    # 7) 路径 B 注入开关：非法值回落 real，绝不误把生产流量打进 Mock。
    raw_impl = str(_raw("omp_executor_impl") or IMPL_REAL).strip().lower()
    impl = raw_impl if raw_impl in VALID_IMPL else IMPL_REAL
    if impl != raw_impl:
        _warn_once(
            "impl_" + raw_impl,
            "agent_engine.omp_executor_impl=%r 非法（需 real/mock），已回落 real",
            raw_impl,
        )

    return DualTrackConfig(
        enabled=_as_bool(_raw("enabled"), bool(_FALLBACK_DEFAULTS["enabled"])),
        fallback_enabled=_as_bool(
            _raw("fallback_enabled"), bool(_FALLBACK_DEFAULTS["fallback_enabled"])
        ),
        mode=mode,
        omp_binary_path=binary,
        context_threshold_ratio=ratio,
        max_retries=retries,
        retry_backoff_base=backoff,
        compaction_threshold=compaction,
        provider_auth=str(_raw("provider_auth") or "").strip(),
        task_timeout=timeout,
        gray_percent=percent,
        gray_key=gray_key,
        omp_executor_impl=impl,
        mock_response_path=str(_raw("mock_response_path") or "").strip(),
    )


def make_config(**overrides: Any) -> DualTrackConfig:
    """基于实时配置构造变体（测试 / 依赖注入用；不绕过配置中心）。"""
    data = asdict(load_dual_track_config())
    data.update(overrides)
    return DualTrackConfig(**data)


def public_config() -> dict[str, Any]:
    """只读、脱敏的配置快照（诊断端点 / 前端 Feature Flag 消费）。"""
    cfg = load_dual_track_config()
    return {
        "enabled": cfg.enabled,
        "mode": cfg.mode,
        "fallback_enabled": cfg.fallback_enabled,
        "gray_percent": cfg.gray_percent,
        "gray_key": cfg.gray_key,
        "omp_executor_impl": cfg.omp_executor_impl,
        "omp_binary_path": cfg.omp_binary_path,
        "context_threshold_ratio": cfg.context_threshold_ratio,
        "max_retries": cfg.max_retries,
        "retry_backoff_base": cfg.retry_backoff_base,
        "compaction_threshold": cfg.compaction_threshold,
        "task_timeout": cfg.task_timeout,
        # 只回报「是否配置」，绝不回传凭据路径或内容。
        "provider_auth_configured": bool(cfg.provider_auth),
        "mock_response_configured": bool(cfg.mock_response_path),
    }
