"""JSON 模式（``response_format=json_object``）兼容性判定 —— **唯一事实源**

为什么独立成模块（架构约束，不是过度设计）
--------------------------------------------
判据原先在**两处**各自实现，且已经分叉：

  1. ``services/ai/provider_factory.py::_json_mode_unsupported(err)``
     —— 编排层看异常文本，只认英文关键词；
  2. ``services/ai/providers/openai_compatible.py
     ::_is_response_format_unsupported(status, body)``
     —— HTTP 层看状态码 + 响应体，另一套英文 marker 列表。

两端互相 import 会形成**循环依赖**（``provider_factory`` 本就 import
``providers.*``），所以判据下沉到这里：``providers/*`` 与 ``provider_factory``
都只 import 本模块，依赖方向保持单向。

共同后果（两条路径都漏）
------------------------
国内厂商拒绝 JSON 模式时返回的多是**中文**（「该模型暂不支持 JSON 输出」、
「参数错误：不支持 response_format」等），英文硬编码关键词匹配不上，于是：
  - HTTP 层不摘字段重试 → 直接抛 ``RuntimeError``；
  - 编排层不回退普通模式，且把该错误计入**熔断失败**与**配额冷却** ——
    一次「参数不兼容」被当成「provider 故障」，污染降级链健康度；
  - 整条候选链用同一原因逐个失败，目录生成 / 事实提取 / 一致性审计等
    **全部 JSON 类任务**同时挂死。

⚠️ 新增厂商 / 新增报错形态时，**只改本文件**；两处调用方不得内联关键词。
"""
from __future__ import annotations

__all__ = [
    "JSON_MODE_TOKENS", "JSON_MODE_NEGATION_TOKENS", "JSON_MODE_HINT_TOKENS",
    "json_mode_unsupported", "response_format_rejected",
]

#: 直接命中关键词：token 本身已唯一指向 JSON 模式，单独出现即可判定。
#: 一律小写（判定前统一 ``lower()``）。
JSON_MODE_TOKENS: tuple[str, ...] = (
    # ---- 英文（历史行为，逐字保留） ----
    "response_format",
    "json_object",
    "json mode",
    "json mode is not supported",
    "json output",
    "json schema",
    # ---- 中文 ----
    "json 输出",
    "json输出",
    "json 模式",
    "json模式",
    "json 结构",
    "json结构",
    "不支持json",
    "不支持 json",
    "暂不支持json",
    "暂不支持 json",
    "仅支持文本",
    "text-only",
)

#: 泛化否定词：必须与 :data:`JSON_MODE_HINT_TOKENS` **同时**出现才判定。
#: 否则「不支持该地域」「invalid region」等无关错误会被误判为 JSON 模式
#: 问题，白烧一次普通模式重试。
#:
#: ``does not support`` / ``not support`` / ``unknown parameter`` /
#: ``must be`` 四项来自 HTTP 层旧 marker 列表，收敛时**必须保留**，
#: 否则属于行为回退。
JSON_MODE_NEGATION_TOKENS: tuple[str, ...] = (
    "not supported",
    "not support",
    "does not support",
    "unsupported",
    "unknown parameter",
    "invalid parameter",
    "must be",
    "不支持",
    "暂不支持",
    "参数错误",
    "参数无效",
    "无效参数",
    "参数非法",
)

#: JSON 语义提示词：与否定词组合判定的必要条件。
#: 另收录去下划线形态（``json_mode`` / ``json_object`` 含下划线，
#: 厂商报错里两种写法都存在）。
JSON_MODE_HINT_TOKENS: tuple[str, ...] = (
    "json", "jsonmode", "jsonobject", "json_mode", "json_object",
)

#: HTTP 层「参数被拒」的兜底关键词：厂商直接点名被拒参数名。
_RESPONSE_FORMAT_TOKEN = "response_format"


def json_mode_unsupported(err) -> bool:
    """错误文本是否表明「厂商不支持 JSON 模式」。

    判定顺序（先强后弱，命中即返回）：
      1. 直接命中 :data:`JSON_MODE_TOKENS`（唯一指向 JSON 模式）；
      2. 否定词 + JSON 提示词**同时**出现（覆盖中文错误）；
      3. 兜底：文本含 ``400`` 且含 ``json``（厂商未给出清晰描述）。

    ⚠️ 误判成本评估（刻意放宽第 2 条的理由）：
    误判为 True 只多烧一次「普通模式重试」，而漏判会让整条候选链用同一
    原因逐个失败（每次都是数十秒的真实网络往返）。故边界上宁松勿紧。
    """
    s = str(err or "").lower()
    if not s:
        return False
    if any(tok in s for tok in JSON_MODE_TOKENS):
        return True
    if (any(tok in s for tok in JSON_MODE_NEGATION_TOKENS)
            and any(tok in s for tok in JSON_MODE_HINT_TOKENS)):
        return True
    return "400" in s and "json" in s


def response_format_rejected(status, body) -> bool:
    """HTTP 层判定：厂商是否因 ``response_format`` 参数拒绝了本次请求。

    Args:
        status: HTTP 状态码。``None`` 表示未知（如异常文本里已含状态码），
            此时不按状态码过滤，只看正文。
        body: 响应体 / 异常文本。

    逻辑：
      1. 已知状态码且 **非 400** → 一定不是「参数不兼容」；
      2. 正文直接点名 ``response_format`` → 判定命中
         （保留 HTTP 层旧行为：即使没有否定词也命中）；
      3. 否则回落到 :func:`json_mode_unsupported` 的中文/英文综合判据。

    这一层比编排层更早介入：命中即可**摘掉字段就地重试**，
    省掉「抛异常 → 编排层重建 Provider → 再发一次」的一整轮往返。
    """
    if status is not None:
        try:
            if int(status) != 400:
                return False
        except (TypeError, ValueError):
            pass  # 状态码不可解析 → 不按状态码过滤
    text = body or ""
    if _RESPONSE_FORMAT_TOKEN in str(text).lower():
        return True
    return json_mode_unsupported(text)