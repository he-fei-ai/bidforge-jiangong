"""OMP hashline 内容哈希锚点适配层：结构化修复的 stale 检测。

对应 omp 上游能力（README「conflict://…」批量冲突解决与 hashline 内容哈希锚点）：
  - hashline 用内容哈希作锚点，修复指令携带「发出时的内容哈希锚」；
  - 应用时先校验当前内容哈希 == 锚哈希，一致才写回，不一致判 stale（过期），
    拒绝应用，防「并发修改导致的过期文件覆盖损坏」。

本层语义（确定性实现，等价于 omp hashline 的平台侧）：
  - ``hashline_anchor(text)``     —— 计算内容锚：``hashline:sha256:<hex>``；
  - ``apply_hashline(document, anchor, new_text, old_text=None)`` —— 先做 stale
    检测（锚哈希 vs 当前 document），再执行**精确局部替换**（old_text → new_text，
    ``count=1``，宁漏勿误）；old_text 未提供时整文替换为 new_text；
  - ``stale_anchor_error``        —— stale 锚拒绝时抛出的异常（导出为类，可 raise / except）。

与 omp 的区别：omp 的 hashline 锚定「每行内容哈希」以支持行级冲突解决；本层用
**整文 sha256** 作锚（单文档粒度），语义更保守——任意外部修改都判 stale，拒绝
应用。若日后需行级粒度，把 ``_digest`` 换成行级哈希表即可，接口不变。

⚠️ 分层约束：services 层禁止 import routers；本层纯函数，零网络、零数据库访问。
"""

from __future__ import annotations

import hashlib
import re

#: 锚前缀：``hashline:sha256:<hex>``（OMP hashline 的确定性等价形态）。
_ANCHOR_PREFIX = "hashline:sha256:"


class _StaleAnchorError(ValueError):
    """stale 锚拒绝：当前文档内容哈希与修复指令携带的锚不一致。

    语义：修复指令是在旧内容上发出的，若文档已被外部修改（并发写 / 其他 Agent
    落盘），直接应用会覆盖他人修改。因此拒绝，交由调用方重新取锚再修。
    """

    def __init__(self, expected: str, actual: str):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"stale hashline 锚：期望 {expected}，当前 {actual}——文档已被修改，拒绝应用"
        )


#: 导出名（__init__ 引用）：既可用 ``raise stale_anchor_error(...)`` 也可
#: ``except stale_anchor_error``。
stale_anchor_error = _StaleAnchorError


def _digest(text: str) -> str:
    """整文 sha256 摘要（十六进制，小写）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def hashline_anchor(text: str) -> str:
    """计算内容哈希锚：``hashline:sha256:<hex>``。

    用法：
        anchor = hashline_anchor(body)          # 修复指令发出时
        body2 = apply_hashline(body, anchor, "...", old_text="图1 编号")
    """
    return _ANCHOR_PREFIX + _digest(text)


def _parse_anchor(anchor: str) -> str:
    """解析锚，取回哈希部分；非本层锚格式抛 ValueError（宁漏勿误）。"""
    if not isinstance(anchor, str) or not anchor.startswith(_ANCHOR_PREFIX):
        raise ValueError(f"非本层 hashline 锚格式：{anchor!r}")
    return anchor[len(_ANCHOR_PREFIX):]


def apply_hashline(
    document: str, anchor: str, new_text: str, old_text: str | None = None
) -> str:
    """按锚应用结构化修复；stale（内容已变）时抛 ``stale_anchor_error``。

    参数：
        document —— 当前文档内容（拟被修复的载体）；
        anchor   —— 修复指令携带的内容锚（``hashline_anchor`` 产出）；
        new_text —— 替换目标文本；
        old_text —— 精确定位片段（默认 None 表示整文替换）。提供时先确认
                    ``document`` 恰好含 1 处 ``old_text``，否则 ValueError（宁漏勿误）。

    返回：
        应用修复后的新文档内容（不落盘——写盘由调用方决定）。

    抛出：
        stale_anchor_error —— 锚哈希 != 当前 document 哈希（内容已被外部修改）；
        ValueError         —— old_text 在 document 中不是恰好 1 处（定位失败）。
    """
    expected = _parse_anchor(anchor)
    actual = _digest(document)
    if expected != actual:
        raise stale_anchor_error(expected=expected, actual=actual)
    if old_text is None:
        return str(new_text)
    # 精确局部替换：恰好 1 处才应用（宁漏勿误，防止误伤多处同形片段）。
    count = str(document).count(str(old_text))
    if count != 1:
        raise ValueError(f"old_text 定位失败：期望恰好 1 处，实际 {count} 处")
    return str(document).replace(str(old_text), str(new_text), 1)


def is_hashline_anchor(anchor: str) -> bool:
    """判断一个字符串是否是本层格式的内容哈希锚。"""
    return bool(re.fullmatch(re.escape(_ANCHOR_PREFIX) + r"[0-9a-f]{64}", str(anchor or "")))
