"""AI 厂商错误统一分类 —— **单一事实源**（叶子模块，零内部依赖）

为什么独立成模块（架构约束，同 ``json_mode_compat`` 先例）
----------------------------------------------------------
同一个「这条失败到底是什么类型的错误」的判据，本轮之前散落在四处且口径不一：

  1. ``provider_factory.is_non_retryable_error`` —— 只判「确定性死错误」；
  2. ``provider_factory._status_from_error`` —— 只从文本抠 HTTP 状态码；
  3. ``routers/ai_config/usage.py`` —— ``by_error`` 只做「原文截断 TOP8」，
     无法回答「429 限流一共多少次 / 超时多少次 / 格式错误多少次」；
  4. 各业务层重试逻辑（sse_handlers / facts_extractor）各自用
     ``"429" in err_s`` 之类的内联判断。

本轮（R150 统一 AI 网关）新增的两个消费方让它必须收敛：

  - **网关级瞬态退避**（``_attempt_candidate``）：需要「哪些错误值得
    同候选退避重试」的判定 —— 判错方向代价不对称：把 401 判成瞬态会
    白烧重试（与 R109「agnes 401 被当瞬态重试 49 次」同病灶），
    把 429 判成非瞬态会放弃可恢复的调用；
  - **用量盘点**（``/ai/stats`` 的 ``by_error_class``）：需要把 22k 行
    审计失败记录归入稳定类别，产出成功率/限流/超时的结构化基线。

判据**只认异常文本**（``str(e)``），与异常类型解耦 —— 因为跨层传递中
异常常被包成 ``RuntimeError``（如 ``所有 AI 提供商调用失败：[...]``），
类型信息会丢失。因此 ``providers.base.VendorHTTPError`` 附加的结构化字段
（``status`` / ``retry_after``）由调用方按需读取，本模块只做文本兜底。

⚠️ 新增厂商 / 新增报错形态时，**只改本文件**；调用方不得内联关键词。
"""
from __future__ import annotations

__all__ = [
    "ERROR_CLASSES", "TRANSIENT_CLASSES", "SKIP_CLASSES",
    "classify_error", "error_class_label",
]

#: 全部错误类别（封闭集合 —— 新类别必须显式登记，护栏锁口径）。
#: 命名与 ``ai_audit_logs.error`` 列的文本形态一一对应，见各分支注释。
ERROR_CLASSES: tuple[str, ...] = (
    # —— 真实网络调用失败（action='chat'）——
    "rate_limit",         # HTTP 429 / Too Many Requests / 限流
    "server_error",       # HTTP 5xx / Bad Gateway / Service Unavailable
    "timeout",            # ReadTimeout / ConnectTimeout / 超时
    "network",            # ConnectError / Connection reset / SSL / DNS
    "auth",               # HTTP 401/402/403 / 令牌失效 / invalid api key
    "not_found",          # HTTP 404/410 / 模型或端点不存在（死配置特征）
    "quota_depleted",     # 余额不足 / 额度用完（未包裹在 429 里的形态）
    "json_mode",          # response_format/json_object 参数被拒
    "empty_content",      # HTTP 200 但正文为空（空 content / 空 choices / 空流）
    "thinking_exhausted",  # 推理模型把 max_tokens 全部耗在思考过程
    # —— 网关门禁跳过（action='circuit_skipped' / 'provider_disabled'）——
    "cooldown_skip",          # 配额冷却，跳过本次调用
    "thinking_cooldown_skip",  # 思考吞噬冷却，跳过本次调用
    "circuit_open",           # 熔断器 OPEN，跳过本次调用
    "provider_disabled",      # 运行时开关禁用
    # —— 编排层汇总 ——
    "all_failed",         # 「所有 AI 提供商调用失败」（候选链耗尽）
    "other",              # 兜底
)

#: 瞬态错误 —— 网关级退避重试的判定集合（R150 `_attempt_candidate` 消费）。
#: ⚠️ auth / not_found / quota_depleted **刻意不在内**：重试 100% 无用
#: （``provider_factory.is_non_retryable_error`` 的生产实证，R109）。
TRANSIENT_CLASSES: tuple[str, ...] = (
    "rate_limit", "server_error", "timeout", "network",
)

#: 门禁跳过类（非真实网络调用）—— 盘点侧与真实失败分开统计。
SKIP_CLASSES: tuple[str, ...] = (
    "cooldown_skip", "thinking_cooldown_skip", "circuit_open", "provider_disabled",
)

# --------------------------------------------------------------------------
# 文本判据（顺序即优先级：先命中先返回；与生产库实拍错误文本对齐）
# --------------------------------------------------------------------------

#: 门禁跳过类：审计 action 已区分，但文本判据保留（跨表聚合时无需带 action）。
_SKIP_MARKERS: tuple[tuple[str, str], ...] = (
    ("配额冷却", "cooldown_skip"),
    ("思考吞噬冷却", "thinking_cooldown_skip"),
    ("熔断器 open", "circuit_open"),
    ("运行时开关已禁用", "provider_disabled"),
    ("运行时开关禁用", "provider_disabled"),
)

_TIMEOUT_TOKENS: tuple[str, ...] = (
    "readtimeout", "connecttimeout", "writetimeout", "timeoutexception",
    "timed out", "timeout", "请求超时",
)

_NETWORK_TOKENS: tuple[str, ...] = (
    "connecterror", "connectionerror", "connection reset", "connection refused",
    "sslerror", "getaddrinfo failed", "name or service not known",
    "network error", "networkerror", "远程主机", "网络错误",
)

_AUTH_TOKENS: tuple[str, ...] = (
    "http 401", "http 402", "http 403",
    "该令牌状态不可用", "invalid api key", "no valid authorization",
    "unauthorized", "authentication", "鉴权失败",
)

_NOT_FOUND_TOKENS: tuple[str, ...] = (
    "http 404", "http 410", "unsupportedmodel", "not found for account",
    "function not found", "no endpoints found", "model_not_found",
)

_QUOTA_DEPLETED_TOKENS: tuple[str, ...] = (
    "insufficient balance", "quota_exceeded", "quota exceeded",
    "exceeded your current quota", "余额不足", "额度已用完", "欠费",
)

_SERVER_ERROR_TOKENS: tuple[str, ...] = (
    "internal server error", "bad gateway", "service unavailable",
    "gateway timeout", "server error",
)


def _has_status(text: str, code: str) -> bool:
    """「HTTP <code>:」形态命中（``HTTP 429: {...}``）。"""
    return f"http {code}" in text


def _has_5xx(text: str) -> bool:
    for seg in text.split("http "):
        head = seg[:3]
        if len(head) == 3 and head[0] == "5" and head[1:].isdigit():
            return True
    return False


def classify_error(err) -> str:
    """把异常 / 错误文本归入 :data:`ERROR_CLASSES` 之一（先命中先返回）。

    优先级设计（误判代价不对称，边界上从严）：

      1. 门禁跳过类（这些文本不会与厂商错误混淆，最先分流）；
      2. 429 限流 —— 状态码优先于正文语义：``HTTP 429: 余额不足…``
         仍算限流（账号维度限速窗口），盘点口径「429 频次」以此为准；
      3. 5xx / auth / not_found（HTTP 状态码家族，互斥）；
      4. quota_depleted（未包裹 429 的余额类）；
      5. timeout → network（传输层）；
      6. thinking_exhausted → empty_content（业务层 200 空响应家族）；
      7. json_mode（复用 ``json_mode_compat`` 中文判据，见该模块）；
      8. all_failed（编排层汇总文本）；
      9. other 兜底。
    """
    s = str(err or "").strip().lower()
    if not s:
        # ``ReadTimeout`` 等 httpx 异常 str() 为空（R137 教训：约 52% 失败
        # 文本为空）—— 网关退避直接拿**异常对象**分类，文本空时按异常
        # **类型名**兜底（TimeoutError / ReadTimeout → timeout）；
        # 纯字符串输入无类型信息，按 other 处理，绝不凭空猜。
        if err is not None and not isinstance(err, str):
            name = type(err).__name__.lower()
            if "timeout" in name:
                return "timeout"
        return "other"
    for marker, cls in _SKIP_MARKERS:
        if marker in s:
            return cls
    if _has_status(s, "429") or "too many requests" in s:
        return "rate_limit"
    if _has_5xx(s) or any(tok in s for tok in _SERVER_ERROR_TOKENS):
        return "server_error"
    if any(tok in s for tok in _AUTH_TOKENS):
        return "auth"
    if any(tok in s for tok in _NOT_FOUND_TOKENS):
        return "not_found"
    if any(tok in s for tok in _QUOTA_DEPLETED_TOKENS):
        return "quota_depleted"
    if any(tok in s for tok in _TIMEOUT_TOKENS):
        return "timeout"
    if any(tok in s for tok in _NETWORK_TOKENS):
        return "network"
    if "思考过程" in s:
        return "thinking_exhausted"
    if ("返回空 content" in s or "空 choices" in s or "返回空正文" in s
            or "未返回任何内容" in s or "无文本内容" in s):
        return "empty_content"
    # json_mode 判据下沉复用：中文「暂不支持 JSON」等 20+ 形态都在那边
    from app.services.ai.json_mode_compat import json_mode_unsupported
    if json_mode_unsupported(s):
        return "json_mode"
    if "所有 ai 提供商调用失败" in s or "所有支持 tools 的 provider 调用失败" in s:
        return "all_failed"
    return "other"


#: 中文标签（供 /ai/stats 与盘点报表直接展示，不必每个消费方各配一份）
_ERROR_CLASS_LABELS: dict[str, str] = {
    "rate_limit": "限流(429)",
    "server_error": "服务端错误(5xx)",
    "timeout": "超时",
    "network": "网络错误",
    "auth": "认证/授权失败",
    "not_found": "模型或端点不存在",
    "quota_depleted": "余额/配额耗尽",
    "json_mode": "JSON模式不兼容",
    "empty_content": "空响应",
    "thinking_exhausted": "思考吞噬(max_tokens)",
    "cooldown_skip": "配额冷却跳过",
    "thinking_cooldown_skip": "思考吞噬冷却跳过",
    "circuit_open": "熔断器跳过",
    "provider_disabled": "运行时开关禁用",
    "all_failed": "候选链耗尽",
    "other": "其他",
}


def error_class_label(cls: str) -> str:
    """类别的中文展示标签（未知类别原样返回，不抛错）。"""
    return _ERROR_CLASS_LABELS.get(cls, cls)
