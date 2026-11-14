"""OMP 引擎路由与降级链（引入 AGENT 引擎方案 · 落盘骨架）。

对应《专项方案编制软件引入 AGENT 引擎方案》§5「引擎路由与兼容策略」：
在「目录生成 / 正文生成 / 审核与修复」入口提供统一的引擎选择，
按 ``auto / omp / main`` 路由，omp 不可用或失败时降级到平台主链路。

⚠️ 本模块是**决策层**（纯函数 + 只读探测），不调用任何执行器：

* ``select_engine``         —— 依据配置与就绪态给出 ``EngineChoice``；
* ``annotate_result``       —— 把 ``engine_used / degraded / engine_reason`` 加进结果；
* ``default_omp_available`` —— 探测 omp 是否可用（复用 settings + binary_exists）。

真正的 omp / 主链路由由 routers 层按 choice 分流（services 禁止 import routers）。

⚠️ 默认态：``prefer="auto"`` 且 omp 未就绪时一律走 main，结果与既有主链路**逐字节一致**
   （新增键只在 omp 被选中或发生降级时出现，加法式、不覆盖既有键）。

⚠️ 分层约束：services 层禁止 import routers；本模块零网络、零数据库访问。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 引擎顺序（降级链：omp → local → main）。
#: ⚠️ R236（2026-11-13）：补上**本地编排档**（路径 B，注入 executor_fn 用平台 AI 链
#:    跑同一套 plan→execute→verify→fix），对齐附件方案 §5.6「omp → 本地编排 →
#:    aiService.chat」三档链；``main`` ≡ 附件里的 ``chat``（平台主链路）。
ENGINE_ORDER = ("omp", "local", "main")
#: 默认路由偏好。
DEFAULT_PREFER = "auto"
#: 支持 omp 旁路的任务类型（与 ``omp_pipeline.VALID_STAGES`` 同口径）。
SUPPORTED_TASK_TYPES = ("outline", "content", "audit", "full")


@dataclass(frozen=True)
class EngineChoice:
    """一次引擎选择的结论。"""

    engine: str = "main"        # "omp" | "main"
    degraded: bool = False      # True = 偏好 omp 但降级到 main
    reason: str = ""


def default_omp_available() -> bool:
    """探测 omp 是否可用（``omp_task_enabled`` + 二进制存在）。

    只读探测，不拉起子进程；供 ``select_engine`` 缺省使用。
    """
    from app.config import settings
    from app.services.ai.omp_rpc import binary_exists

    if not bool(getattr(settings, "omp_task_enabled", False)):
        return False
    binary = (getattr(settings, "omp_binary_path", "") or "omp").strip() or "omp"
    return binary_exists(binary)


def select_engine(
    *,
    task_type: str,
    prefer: str = DEFAULT_PREFER,
    omp_available: bool | None = None,
    local_available: bool = True,
    overrides: dict[str, str] | None = None,
) -> EngineChoice:
    """选择本次任务走 omp 还是主链路。

    路由规则：

    * ``prefer="main"``         → 恒 main（不降级）；
    * ``prefer="omp"``          → omp 可用则 omp；否则降级 local（``degraded=True``），再不可用降级 main；
    * ``prefer="auto"``（默认） → 任务类型受支持且 omp 可用则 omp；否则 main（⚠️ auto **不**自动落到 local：平台主链路强于单次对话，auto 落 local 会降低产物质量；
      local 档由**显式指定**或阈值路由命中）；
    * ``overrides[task_type]``  → 任务级覆盖（优先级高于 ``prefer``）。
    """
    task = str(task_type or "").strip().lower()
    pref = str((overrides or {}).get(task) or prefer or DEFAULT_PREFER).strip().lower()
    if omp_available is None:
        omp_available = default_omp_available()

    if pref == "main":
        return EngineChoice(engine="main", degraded=False, reason="显式指定主链路")
    if pref == "local":
        return EngineChoice(engine="local", degraded=False, reason="显式指定本地编排档（路径 B）")
    if pref == "omp":
        if omp_available:
            return EngineChoice(engine="omp", degraded=False, reason="显式指定 omp 且可用")
        if local_available:
            return EngineChoice(
                engine="local", degraded=True, reason="显式指定 omp 但 omp 不可用，降级本地编排档"
            )
        return EngineChoice(
            engine="main", degraded=True, reason="显式指定 omp 但 omp 与本地编排均不可用，降级主链路"
        )
    if task in SUPPORTED_TASK_TYPES and omp_available:
        return EngineChoice(engine="omp", degraded=False, reason=f"auto：{task} 受支持且 omp 可用")
    return EngineChoice(
        engine="main", degraded=False, reason=f"auto：omp 不可用或任务类型 {task} 不受支持"
    )


def parse_overrides(raw: str) -> dict[str, str]:
    """解析任务级引擎覆盖（形态 ``"outline=omp,content=main"``；空 = 无覆盖）。"""
    out: dict[str, str] = {}
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        k, v = item.split("=", 1)
        k, v = k.strip().lower(), v.strip().lower()
        if k and v:
            out[k] = v
    return out


def parse_whitelist(raw: str) -> set[str]:
    """解析方案级灰度白名单（逗号分隔 scheme_id）。"""
    return {s.strip() for s in str(raw or "").split(",") if s.strip()}


def gray_enabled(scheme_id: str, *, whitelist: str = "") -> bool:
    """方案级灰度判定：白名单为空 = 全量放行；非空 = 仅白名单内方案可用 omp。"""
    ids = parse_whitelist(whitelist)
    if not ids:
        return True
    return str(scheme_id or "") in ids


def resolve_engine(
    *,
    scheme_id: str = "",
    task_type: str,
    prefer: str = DEFAULT_PREFER,
    overrides: dict[str, str] | None = None,
    whitelist: str = "",
) -> EngineChoice:
    """灰度 + 任务级覆盖 + 偏好的综合路由（``auto`` 入口的统一决策）。

    灰度未命中直接回落主链路（``degraded=False``：这是**有意不放量**，不是故障降级）；
    命中后委托 ``select_engine`` 做可用性判定。
    """
    if not gray_enabled(scheme_id, whitelist=whitelist):
        return EngineChoice(
            engine="main", degraded=False, reason="未命中灰度白名单（omp_gray_scheme_ids）"
        )
    return select_engine(task_type=task_type, prefer=prefer, overrides=overrides)


def annotate_result(result, choice: EngineChoice) -> dict:
    """把引擎选择标注进结果（加法式，不覆盖既有键）。

    非 dict 结果会被包成 ``{"result": ...}``，绝不丢原始产物。
    """
    if not isinstance(result, dict):
        return {
            "result": result,
            "engine_used": choice.engine,
            "degraded": choice.degraded,
            "engine_reason": choice.reason,
        }
    out = dict(result)
    out.setdefault("engine_used", choice.engine)
    if choice.degraded:
        out.setdefault("degraded", True)
        out.setdefault("engine_reason", choice.reason)
    return out
