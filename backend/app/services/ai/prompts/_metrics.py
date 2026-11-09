"""提示词运行时指标（进程内内存计数器）。

这是**运维观测指标**，不是持久化业务数据 —— 进程重启即清零。刻意不持久化、
不做历史时间序列、不引入 Prometheus：进程内 ``collections.Counter`` 足够回答
「哪个模板被渲染最多 / 哪类 JSON 修复触发最频繁 / 哪个业务场景 AI 失败最多」。

热路径纪律（``_cache.get_prompt`` 每次正文/目录生成被调几十次）：
  * 每个埋点包 try/except —— 计数失败**绝不**阻断 prompt 渲染主流程（fail-soft）；
  * 只做一行 Counter 自增，不打日志、不加锁（CPython GIL 下 Counter 自增是
    原子读改写，极端竞争下最多丢 1~2 次计数，观测指标可接受）。

对外仅暴露两个读路径：
  * :func:`get_snapshot` —— 结构化快照（HTTP 端点消费）；
  * :func:`reset` —— 清零（运维调试 / 测试隔离）。
"""

from __future__ import annotations

import logging
from collections import Counter

logger = logging.getLogger(__name__)

#: 每个模板 key 的渲染次数（成功返回即记一次）。
render_total: Counter = Counter()
#: 渲染异常次数（变量缺失导致抛错 / 模板解析失败等）。
render_errors: Counter = Counter()
#: 渲染时检出「调用方未提供变量」的次数（key = 模板 key）。
#: ✅ 2026-11-07：此前缺失变量只打 WARNING，端点无法回答「哪些模板正在
#:   带着未替换的 {占位符} 下发」；DB 定制版新增变量在旧校验基线（出厂内容）
#:   下更是完全不可观测。现在按**实际下发模板**判定并计数。
render_missing_vars: Counter = Counter()
#: token 预算截断触发次数（key = 被截断的资料段标签，如「全局事实」/「知识库」）。
#: ✅ 2026-10-07：唯一消费点是 `prompt_governance.allocate_context_budget`
#:   （原先全仓零调用方，端点永远回空 dict —— 运维无法区分「没截断」与「未埋点」）。
token_budget_truncated: Counter = Counter()
#: token 预算削减丢弃的字符总量（key = 资料段标签）。
#: ✅ 2026-11-07（R186）：`token_budget_truncated` 只回答「切了几次」，无法回答
#:   「这段被削掉了多少字、原文多长」—— 开启 `prompt_context_budget` 后，运维
#:   与用户看不到逐段账面。这里按标签累计**省略字数**，与截断次数互补。
token_budget_omitted_chars: Counter = Counter()
#: 预算分配器「未生效」次数（key = 未生效原因，见 ``prompt_governance`` 四个出口：
#: ``disabled`` / ``no_labeled_segments`` / ``within_budget`` / ``overhead_exceeds_budget``）。
#: ✅ 2026-11-07（第 4 条）：此前 `prompt_context_budget` 开了却没生效时**完全静默** ——
#:   运维只能看到「没有截断」，无法区分「预算足够」与「预算器根本没跑/跑不动」。
#:   现四类出口各记一次，`disabled` 只计数（合法关闭不算异常），其余三类另有日志分级。
token_budget_not_applied: Counter = Counter()
#: 留痕机制**自身**故障次数（key = 通道，如 ``truncation_counter``）。
#: ✅ 2026-11-07（第 5 条）：``prompt_material_guard.clip_text_logged`` 的指标留痕
#:   是设计上的 fail-soft（留痕坏掉不得改变裁剪结果），但此前 `except Exception: pass`
#:   让「计数根本没记上」完全不可见 —— 快照里看到的「零截断」与「没发生截断」无法区分。
#:   现把这类失败落到本计数器，保证留痕机制自己坏了也可查。
clip_ledger_failures: Counter = Counter()
#: JSON 修复轮触发次数（key = repair_key；首轮输出未过校验进入修复循环即记一次）。
repair_triggered: Counter = Counter()
#: AI 调用失败/降级次数（key = 业务场景 scene）。
ai_failure_by_scene: Counter = Counter()
#: 出站消息**注入手法扫描**次数（key = 扫描角色，如 ``user``）。
#: ✅ 2026-11-09（G1 · 检测与围栏解耦）：注入防护此前整体挂在
#:   ``prompt_injection_defense``（默认 False）之下 → 默认部署下**既无围栏、
#:   也无任何注入检测**，运维完全看不到「用户上传的资料里带了越狱指令」。
#:   现把**检测**从围栏开关里拆出来：检测是**只读**行为（不改任何提示词
#:   字节），因此可安全地默认常开；围栏仍受开关控制（改字节，需显式开启）。
prompt_injection_scans: Counter = Counter()
#: 出站消息注入手法命中次数（key = 手法名，见
#: ``prompt_governance.INJECTION_PATTERNS`` 的 name 字段，如 ``ignore_previous``）。
prompt_injection_hits: Counter = Counter()


#: 单个计数器的 **key 上限**（防无界增长）。
#: ✅ R204（2026-11-09 · 计数器 key 无界）：部分计数器的 key 含**动态值**
#:   （资料文件名 / 章节标题，如 ``项目资料正文（招标文件.pdf）``）—— 长期运行的
#:   服务每遇到一个新文件/新章节就新增一个 key、永不回收 ⇒ 进程内 Counter
#:   无界增长，且 ``GET /system/prompt-metrics`` 的响应体随之膨胀
#:   （几十万 key → 几十 MB 级响应，运维页直接超时）。
#:   现对**每个计数器**统一限流：达到上限后，新 key 归入 ``__overflow__``
#:   （保留「有增长」的事实，丢失逐条定位能力 —— 可接受的取舍）。
_MAX_COUNTER_KEYS = 256
#: 超过上限后的归并桶名。
_OVERFLOW_KEY = "__overflow__"


def _inc(counter: Counter, key: str) -> None:
    """一行自增；任何异常吞掉并 debug 级留痕（观测指标不得阻断主流程）。

    ✅ R204：新增 key 上限保护（见 ``_MAX_COUNTER_KEYS``）。
    """
    try:
        if not key:
            return
        if key not in counter and len(counter) >= _MAX_COUNTER_KEYS:
            counter[_OVERFLOW_KEY] += 1
            return
        counter[key] += 1
    except Exception:  # noqa: BLE001
        logger.debug("prompt metrics counter increment failed", exc_info=True)


def record_render(key: str) -> None:
    """记一次成功渲染（``_cache.get_prompt`` 正常返回时调用）。"""
    _inc(render_total, key)


def record_render_error(key: str) -> None:
    """记一次渲染异常（``_cache.get_prompt`` 抛错路径调用）。"""
    _inc(render_errors, key)


def record_render_missing_vars(key: str) -> None:
    """记一次「渲染时变量缺失」（``_cache.get_prompt`` 检出缺失时调用）。"""
    _inc(render_missing_vars, key)


def record_token_budget_truncated(key: str) -> None:
    """记一次 token/长度预算截断（facts/章节正文超预算被切时调用）。"""
    _inc(token_budget_truncated, key)


def record_token_budget_omitted(key: str, omitted: int) -> None:
    """记一次预算削减丢弃的字符数（key = 资料段标签，按标签累计）。"""
    try:
        n = int(omitted)
        if key and n > 0:
            token_budget_omitted_chars[key] += n
    except Exception:  # noqa: BLE001
        logger.debug("prompt metrics omitted-chars counter increment failed", exc_info=True)


def record_token_budget_not_applied(reason: str) -> None:
    """记一次「预算分配未生效」（key = 四类原因之一，见 ``token_budget_not_applied``）。"""
    _inc(token_budget_not_applied, reason or "unknown")


def record_clip_ledger_failure(channel: str) -> None:
    """记一次「留痕机制自身故障」（key = 通道，见 ``clip_ledger_failures``）。"""
    _inc(clip_ledger_failures, channel or "unknown")


def record_repair_triggered(repair_key: str) -> None:
    """记一次 JSON 修复轮被触发（首轮输出未过校验、进入修复循环时调用）。"""
    _inc(repair_triggered, repair_key)


def record_ai_failure(scene: str) -> None:
    """记一次 AI 调用失败/降级（首轮 chat 抛错路径调用）。"""
    _inc(ai_failure_by_scene, scene or "unknown")


def record_injection_scan(role: str) -> None:
    """记一次出站消息注入扫描（key = 被扫描的角色，如 ``user``）。

    ✅ 2026-11-09（G1）：与 :func:`record_injection_hit` 配对使用 ——
    「扫了多少」与「命中多少」分开计数，运维才能区分「没有风险」与
    「根本没扫」。
    """
    _inc(prompt_injection_scans, role or "unknown")


def record_injection_hit(pattern: str) -> None:
    """记一次注入手法命中（key = 手法名，如 ``ignore_previous``）。"""
    _inc(prompt_injection_hits, pattern or "unknown")


#: 提示词缓存加载**失败但保留上次成功缓存**的次数（key = 失败原因归类）。
#: ✅ R205（2026-11-09 · L-13 · 加载失败静默回退出厂默认）：旧实现加载异常即
#:   ``_prompt_cache = None`` → ``get_prompt`` 回退 ``_ALL_PROMPTS`` 出厂默认，且**无
#:   任何「本次用的是默认回退」指标**。现改为保留上次成功缓存（不丢已生效的定制），
#:   并打点本计数器（含缺失 key 列表），使「配置库挂了但服务仍跑、用的是旧缓存」
#:   这一事实可观测。
prompt_cache_degraded: Counter = Counter()


def record_prompt_cache_degraded(reason: str) -> None:
    """记一次提示词缓存加载失败（保留上次成功缓存）。"""
    _inc(prompt_cache_degraded, reason or "unknown")


def get_snapshot() -> dict:
    """导出结构化快照（全部为 dict 拷贝，调用方持有后修改不影响内部计数）。

    顶层为「若干计数器 dict + ``registered_keys``（注册表全部 key，供运维对照
    「哪些模板从无人渲染过」）」。计数器数量随功能演进**加法式增加**，此处
    **不再硬编码键数**（旧 docstring 写「固定 10 个键」，R202 加到 12 个后即
    与实现不符，运维按文档解析会漏字段）。
    2026-11-07 新增 ``render_missing_vars`` / ``token_budget_omitted_chars`` /
    ``token_budget_not_applied`` / ``clip_ledger_failures``（加法式，不改动既有键）。
    2026-11-09（G1）新增 ``prompt_injection_scans`` / ``prompt_injection_hits``
    （加法式；默认部署下的注入**检测**恒开，这两个键回答「扫了多少/命中多少」）。
    """
    from app.services.ai.prompts._registry import (
        _ALL_PROMPTS,
        register_lazy_prompts,
    )

    try:
        register_lazy_prompts()
    except Exception:  # noqa: BLE001 - 快照统计尽力而为，注册失败时返回部分快照即可
        pass
    try:
        registered = sorted(_ALL_PROMPTS.keys())
    except Exception:  # noqa: BLE001 - 同上，注册表不可读时按空清单导出
        registered = []
    return {
        "render_total": dict(render_total),
        "render_errors": dict(render_errors),
        "render_missing_vars": dict(render_missing_vars),
        "token_budget_truncated": dict(token_budget_truncated),
        "token_budget_omitted_chars": dict(token_budget_omitted_chars),
        "token_budget_not_applied": dict(token_budget_not_applied),
        "clip_ledger_failures": dict(clip_ledger_failures),
        "repair_triggered": dict(repair_triggered),
        "ai_failure_by_scene": dict(ai_failure_by_scene),
        "prompt_injection_scans": dict(prompt_injection_scans),
        "prompt_injection_hits": dict(prompt_injection_hits),
        "registered_keys": registered,
    }


def reset() -> None:
    """清零全部内存计数器（运维调试 / 测试隔离用）。"""
    for c in (
        render_total,
        render_errors,
        render_missing_vars,
        token_budget_truncated,
        token_budget_omitted_chars,
        token_budget_not_applied,
        clip_ledger_failures,
        repair_triggered,
        ai_failure_by_scene,
        prompt_injection_scans,
        prompt_injection_hits,
    ):
        try:
            c.clear()
        except Exception:  # noqa: BLE001 - 计数器清零尽力而为，失败不影响其余计数器
            pass
