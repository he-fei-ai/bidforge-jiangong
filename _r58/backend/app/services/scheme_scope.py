"""方案名称 → 主要施工内容清单（确定性抽取，纯函数）

背景（2026-09-23 四项依据改造）：目录与正文的章节划分/内容展开必须严格
依据四项输入，其中第 2 项是「专项方案名称包含的主要施工内容」。旧实现只把
方案名称原样注入提示词，由模型自行领会，复合名称（如「基坑支护及土方开挖」
「高支模与脚手架」）常被漏掉一半内容，或退化为通用模板章节。

本模块用确定性规则从方案名称中拆出主要施工内容清单，供目录生成
（章节划分锚点）与正文生成（内容展开锚点）统一消费：
- 只依据名称字面拆分，绝不推断/编造名称之外的施工内容；
- 拆不出多项时回退为「名称主体」单条（与旧行为等价的信息量，不丢语义）；
- 纯函数、零依赖、结果稳定（同输入必同输出），便于单测锁定。
"""
from __future__ import annotations

import re

# 方案名称通用后缀（安全/专项/施工/组织 + 方案/设计/预案/措施），从尾部剥离；
# 循环剥离直至稳定（如「…工程安全专项施工方案」→ 先剥「安全专项施工方案」）。
_SUFFIX_RE = re.compile(
    r"(?:安全)?(?:专项)?(?:施工)?(?:组织)?(?:方案|设计|预案|措施)$")
# 复合名称分隔符：顿号/逗号/斜杠等标点 + 连词「及/与/和/暨/、」。
# 「和」「及」在工程名词内部连写的概率极低（如「调和」「以及」不会出现在
# 方案名称的实义位置），拆分收益远大于误拆风险。
_SPLIT_RE = re.compile(r"[、，,;/+&()]|及|与|和|暨")
# 残留的尾部通配词（拆分后片段仍带的「工程」「相关」等前缀噪声）
_TAIL_NOISE_RE = re.compile(r"(?:相关|有关|等)$")


def _strip_suffix(text: str) -> str:
    """循环剥离方案名称尾部通用后缀（专项施工方案/安全专项方案/…）。"""
    out = text.strip()
    while True:
        stripped = _SUFFIX_RE.sub("", out)
        if stripped == out:
            return out
        out = stripped


def extract_construction_scope(scheme_name: str) -> list[str]:
    """从专项方案名称拆出「主要施工内容」清单（字面依据，不编造）。

    规则：
    1. 剥离通用后缀（安全/专项/施工/组织 + 方案/设计/预案/措施）；
    2. 按顿号/逗号/斜杠与连词（及/与/和/暨）拆分复合名称；
    3. 片段去首尾噪声（「相关」「有关」「等」「工程」尾部），长度 ≥2 才保留；
    4. 拆出 ≥2 项 → 返回全部片段（如「基坑支护及土方开挖」→ 两项）；
       仅 1 项 → 返回单元素清单（单内容方案，与旧「整名注入」信息等价）；
    5. 名称为空/剥无可剥时返回空清单，由调用方决定回退文案。

    纯函数：任何输入都不抛异常（非法类型按空处理）。
    """
    if not scheme_name or not isinstance(scheme_name, str):
        return []
    core = _strip_suffix(scheme_name)
    if not core:
        # 整名只有后缀（如「专项施工方案」）：回退原名的去后缀形态
        core = scheme_name.strip()
    frags = [f.strip() for f in _SPLIT_RE.split(core) if f and f.strip()]
    cleaned: list[str] = []
    seen: set[str] = set()
    for f in frags:
        frag = _TAIL_NOISE_RE.sub("", f)
        # 片段尾部残留「工程」且去掉后仍 ≥2 字（"基坑支护工程"→"基坑支护"）
        if frag.endswith("工程") and len(frag) > 4:
            frag = frag[:-2]
        if len(frag) < 2:
            continue
        if frag not in seen:
            seen.add(frag)
            cleaned.append(frag)
    return cleaned


def render_scope_for_prompt(scheme_name: str) -> str:
    """渲染供提示词注入的「方案名称主要施工内容」一行文本。

    拆不出任何条目时回退为去后缀后的名称主体（保守表述，绝不虚构内容项）；
    名为空时给出显式占位，提示模型以章节标题与实际资料为准。
    """
    scope = extract_construction_scope(scheme_name or "")
    if scope:
        return "、".join(scope)
    core = _strip_suffix(scheme_name or "")
    return core or (scheme_name or "").strip() or "（未提供：以章节标题与项目资料为准）"
