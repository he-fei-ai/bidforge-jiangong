"""边界感知长文本截断（对齐 OpenBidKit userTextSplitter 思路，仅复用成熟思路）

场景：正文生成时单章 AI 上下文（方案概述 + 上级/同级要点 + 全局事实 + 知识库素材
+ 编制依据）可能因极端超长的事实/知识库被撑爆，导致厂商 400（超上下文）或超时。

本模块提供 ``truncate_to_boundary``：当文本超过安全上限时，优先在**自然边界**处
截断，避免切断 Markdown 围栏、中文标点或代理对（surrogate pair）。仅用于「超长兜底」，
正常长度文本零改动，因此默认关闭（context_length_limit=0）时完全向后兼容。

与参考软件的差异（按需适配，不照搬）：
- 参考软件按 context_length_limit * ratio 切分成多段再逐段调用；本软件正文按章单次调用，
  故这里只做「最近自然边界前缀截断」而非分段，语义更贴合单章上下文。
- 边界优先级：段落空行 → 换行 → 句末标点（。！？；） → 逗号/顿号（，、），
  比纯按字符硬截断更不易出现半截句子/半截表格。
"""
import re

# 边界优先级（从高到低）：空行(段落/标题) → 换行 → 句末标点 → 逗号/顿号
_BOUNDARY_PATTERNS = [
    re.compile(r"\n\s*\n"),       # 段落 / 标题之间的空行
    re.compile(r"\n"),            # 任意换行
    re.compile(r"[。！？!?；;]"),  # 句末
    re.compile(r"[，、,]"),       # 逗号 / 顿号
]


def truncate_to_boundary(text: str, max_len: int) -> str:
    """返回 ``text`` 在不超过 ``max_len`` 字符前提下、最接近上限的自然边界前缀。

    - 长度未超 ``max_len``：原样返回（零改动）。
    - 超过：从最粗粒度边界（段落）向细粒度（逗号）依次寻找不超过上限的最近切点；
      找到即在该边界之后截断。
    - 没有任何自然边界可切（如单个超长无标点 token）：硬截断到 ``max_len``。

    ✅ 代码审查 m5（2026-09-23）：入参守卫，把契约写进实现，避免下游隐式崩溃：
    - ``max_len`` 非数字/空 → 视为 0；``max_len <= 0`` → 返回空串（旧行为：
      负值会返回远长于预算的前缀，越契约）；
    - ``text is None``/非字符串 → 降级为空串/ ``str(text)``，避免 None 原样透传
      到 ``render_prompt`` 后该 kwarg 被丢弃、模板里残留 ``{占位符}``。
    注：“默认关闭”（context_length_limit=0）由调用方 ``_truncate_context`` 自行
    拦截，不依赖本函数 ``max_len<=0`` 的行为，因此此守卫不破坏向后兼容。
    """
    try:
        max_len = int(max_len)
    except (TypeError, ValueError):
        max_len = 0
    if max_len <= 0:
        return ""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    if not text or len(text) <= max_len:
        return text
    region = text[:max_len]  # 只在「允许长度内」寻找边界，限定扫描开销
    for pat in _BOUNDARY_PATTERNS:
        last = -1
        for m in pat.finditer(region):
            last = m.end()
        if last > 0:
            return text[:last]
    # 无任何自然边界：硬截断（兜底，避免超出上下文）
    return text[:max_len]
