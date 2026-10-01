"""一致性修复 · 定点编辑（old_text / new_text）

引入背景（对齐参考软件《标书智能体（三）/（五）》§三.4、§五.1）
--------------------------------------------------------------
文档给出的修复语义是**定点替换**：

    「修复时返回精确修改 old_text/new_text；程序只有在 old_text 能唯一
      命中当前小节时才替换；找不到或多处相同内容都拒绝修改并重新尝试。」

并明确其价值：「修改冲突时只动必要位置，**不重写已经正确的内容**」。

本仓旧实现（``repair_agent.repair_section``）走的是**整章重写**：
AI 返回修复后的**完整正文**，``UPDATE sections SET content=?`` 整列覆盖。
后果有两处：

1. 一处「工期 120 天 vs 90 天」的冲突 → 整章被重写，章内**已经正确**的
   段落也可能被改动（质量代价不可控）；
2. ``/consistency/confirm`` 拒绝**单条**冲突时，会把该章**整体**恢复修复前
   快照 —— 同章内其它已修好的冲突也一起丢。

本模块提供定点编辑的**纯逻辑**（唯一命中才替换）+ 一次 AI 调用封装，
由 ``repair_agent`` 在「定点编辑失败」时回落到既有的整章重写。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.services.ai.prompts._registry import render

logger = logging.getLogger("consistency_edits")

#: 单条 old_text 的最短长度。太短的片段在长正文里极易多处命中，会被
#: 「唯一命中」规则整体拒绝，等于白跑一次 AI。
MIN_OLD_TEXT_CHARS = 8

#: 单次最多接受的编辑条数（防止 AI 返回上百条把整章改面目全非，
#: 那与「只动必要位置」的初衷相反）。
MAX_EDITS = 20

_WS_RE = re.compile(r"\s+")


@dataclass
class EditResult:
    """一次定点编辑的落库结果（供 repair_agent 决定是否回落到整章重写）。"""

    content: str
    applied: int = 0
    #: [(old_text 摘要, 原因)]，原因 ∈ not_found / ambiguous / too_short / empty
    rejected: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.applied > 0

    def to_dict(self) -> dict:
        return {"applied": self.applied, "rejected": [
            {"old_text": o[:60], "reason": r} for o, r in self.rejected]}


def _normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip())


def find_unique_span(content: str, old_text: str):
    """在 ``content`` 中定位 ``old_text`` 的**唯一**命中区间。

    命中判定分两级（对齐文档「程序校验」而非纯字符串 find）：
    1. **精确匹配** —— 原文出现且仅出现一次；
    2. **宽松匹配** —— 仅在「空白/换行被折叠」后成立且折叠结果唯一命中，
       用于对付模型把原文折行的情况。

    Returns:
        ``(start, end)``（原文字符下标）；找不到或不唯一时返回 ``None``。
        ⚠️ 文档明确要求「找不到或多处相同内容都拒绝修改」——**不唯一即拒绝**，
        因为无法确定该改哪一处，猜错比不改更糟。
    """
    if not content or not old_text:
        return None
    n = content.count(old_text)
    if n == 1:
        i = content.index(old_text)
        return i, i + len(old_text)
    if n > 1:
        return None
    norm_old = _normalize_ws(old_text)
    if not norm_old:
        return None
    norm_chars: list = []
    index_map: list = []
    prev_space = False
    for idx, ch in enumerate(content):
        if ch.isspace():
            if prev_space:
                continue
            norm_chars.append(" ")
            index_map.append(idx)
            prev_space = True
        else:
            norm_chars.append(ch)
            index_map.append(idx)
            prev_space = False
    norm_content = "".join(norm_chars)
    if norm_content.count(norm_old) != 1:
        return None
    i = norm_content.index(norm_old)
    start = index_map[i]
    end = index_map[i + len(norm_old) - 1] + 1
    while end < len(content) and content[end] in " \t":
        end += 1
    return start, end


def apply_unique_edits(content: str, edits: Sequence) -> EditResult:
    """按顺序应用「唯一命中才替换」的定点编辑（纯函数，可单测穷举）。

    关键约束（对齐文档）：
    - ``old_text`` 必须**唯一命中**才替换；多处命中 / 找不到一律拒绝并记
      原因，**不猜**；
    - ``old_text`` 过短直接拒绝 —— 它几乎必然多处命中；
    - 每次替换后**立即**在**新内容**上继续，故后一条 ``old_text`` 必须对
      上一次的结果仍然成立（顺序由 AI 保证）。
    """
    result = EditResult(content=content or "")
    if not result.content or not edits:
        return result
    for raw in list(edits)[:MAX_EDITS]:
        if not isinstance(raw, dict):
            continue
        old_text = str(raw.get("old_text") or "")
        new_text = str(raw.get("new_text") or "")
        summary = _normalize_ws(old_text)[:40]
        if not old_text.strip():
            result.rejected.append((summary, "empty"))
            continue
        if not new_text.strip():
            result.rejected.append((summary, "empty_new"))
            continue
        if len(_normalize_ws(old_text)) < MIN_OLD_TEXT_CHARS:
            result.rejected.append((summary, "too_short"))
            continue
        span = find_unique_span(result.content, old_text)
        if span is None:
            reason = ("ambiguous" if result.content.count(old_text) > 1
                      else "not_found")
            result.rejected.append((summary, reason))
            continue
        s, e = span
        result.content = result.content[:s] + new_text + result.content[e:]
        result.applied += 1
    return result


# =========================================================================
# AI 调用：让模型只产出「编辑」而不是「整章重写」
# =========================================================================
def _validate_edits(obj: Any) -> list:
    if not isinstance(obj, dict):
        return ["顶层必须是 JSON 对象"]
    if obj.get("edits") is None:
        return ["缺少 edits 字段"]
    if not isinstance(obj.get("edits"), list):
        return ["edits 必须是数组"]
    return []


def build_edits_prompt(section_id: str, section_title: str,
                      conflicts_in_section: list, facts: str,
                      sources: str) -> str:
    """组装「只产出编辑」的 user 提示词（章节原文由调用方追加到最后）。"""
    return render(
        "consistency_repair_edits_user",
        global_facts=facts or "（无）",
        authoritative_sources=sources or "（见各冲突项权威值）",
        section_id=section_id,
        section_title=section_title,
        conflicts_in_section=json.dumps(conflicts_in_section, ensure_ascii=False,
                                       indent=2),
    )


async def collect_repair_edits(*, section_id: str, section_title: str,
                               section_content: str,
                               conflicts_in_section: list,
                               facts: str, sources: str,
                               chat_fn=None) -> EditResult:
    """调用 LLM 产出定点编辑并就地应用（失败时返回 ``applied=0`` 的结果）。

    与整章重写并存：调用方在 ``applied == 0`` 时回落到
    :func:`repair_agent.repair_section`。

    Args:
        chat_fn: 可选的 ``chat_with_fallback`` 同签名可调用对象。由
            ``repair_agent`` 注入其**模块级**引用，既有单测
            （``monkeypatch.setattr(repair_agent, "chat_with_fallback", ...)``）
            才能同时拦到「定点编辑」与「整章重写」两次调用；不传则用默认实现。
    """
    if chat_fn is None:
        from app.services.ai.provider_factory import chat_with_fallback as chat_fn

    user = build_edits_prompt(section_id, section_title, conflicts_in_section,
                              facts, sources)
    # 章节原文放在最后（与《标书智能体（四）》§四.4「任务差异后置」一致），
    # 模型据此逐字抄写 old_text。
    user += f"\n\n【待编辑章节原文】\n{section_content}"
    system = render("consistency_repair_edits_system")
    try:
        raw = await chat_fn(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            temperature=0.1, timeout=120, scene="consistency_repair")
    except Exception as e:  # noqa: BLE001 - fail-soft：回落到整章重写
        logger.warning("[定点编辑] 第 %s 章节 AI 调用失败: %s", section_id, e)
        return EditResult(content=section_content or "")

    try:
        from app.services.ai.json_response import extract_json
        raw_json = extract_json(raw)
        obj = json.loads(raw_json) if raw_json else None
    except Exception as e:  # noqa: BLE001
        logger.warning("[定点编辑] 第 %s 章节 JSON 解析失败: %s", section_id, e)
        return EditResult(content=section_content or "")

    issues = _validate_edits(obj)
    if issues:
        logger.info("[定点编辑] 第 %s 章节结果结构不合格: %s", section_id, issues)
        return EditResult(content=section_content or "")
    res = apply_unique_edits(section_content or "", obj.get("edits") or [])
    if res.rejected:
        logger.info("[定点编辑] 第 %s 章节应用 %d 条、拒绝 %d 条：%s",
                    section_id, res.applied, len(res.rejected), res.to_dict())
    return res

