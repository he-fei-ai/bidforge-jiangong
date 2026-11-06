"""冲突级最小范围回滚（R181 · B-06，2026-11-06）。

背景
----
R98 起「拒绝某条冲突」的语义是**整章恢复快照原文**：同一章里若另有 3 处
修复是用户认可的，拒绝第 4 处会把它们一起抹掉；修复过程中对全章做的格式
整理/衔接改写也一并丢失。用户只能「全章回退 → 手工重贴 3 处修复」。

口径
----
本模块提供**纯函数**级的最小范围回滚：以修复前快照 ``before`` 与当前正文
``current`` 的字符级 diff 为事实来源，**只撤销**那些能被本条冲突取值归因
的范围：

  · 先在 ``before`` 中定位本条冲突的原取值（``wrong_values``），再用 diff
    的偏移映射把该取值的起止边界映射到 ``current``，只替换这一小段。这样
    即使 SequenceMatcher 把相邻两个数值拆成多个碎片段，也不会把别的数值
    卷进来；
  · 若 ``before`` 已找不到原取值（模型改写措辞），则退一步在 ``current``
    中定位权威取值（``authoritative_value``）并反向映射回 ``before`` ——
    仅在权威取值在 ``before`` 中不存在时启用，避免误伤原文已有的同值；
  · 共享保护：若目标范围在 ``before`` 或 ``current`` 中还牵涉**其它未拒绝
    冲突**的取值（``preserve_values``），则不动该范围（避免撤回别人已接受
    的修复）——宁可报告「无法定位」，也不越权改内容；
  · 无归因范围 → ``not_located``，调用方**不得**退化成整章回退（降级只允许
    发生在历史批次缺少取值元数据时，由调用方显式选择并记账）。

返回 ``(text, reason)``：
  · ``reverted``    —— 已按最小范围回滚；
  · ``unchanged``   —— 当前文已是快照原文 / 无可撤销内容；
  · ``not_located`` —— 无法把本条冲突归因到任何变更范围（正文未改动）。
"""

from __future__ import annotations

import difflib
from typing import Iterable


def _build_offset_maps(
    opcodes: list[tuple[str, int, int, int, int]],
    src_len: int,
    dst_len: int,
) -> tuple[list[int], list[int]]:
    """把 `src` 偏移映射到 `dst` 偏移。

    返回 ``(start_map, end_map)``：
      · ``start_map[p]`` 用于一个范围的**起点**；
      · ``end_map[p]`` 用于一个范围的**终点**（独占边界）。
    两者在 insert 边界处刻意不同：起点取插入前的位置（把插入内容纳入目标
    范围），终点取插入后的位置（同样纳入），这样「50 m → 45 m」中新增的
    ``4`` 不会被留在区间外，形成 ``450 m``。
    """
    start_map = [0] * (src_len + 1)
    end_map = [0] * (src_len + 1)
    for tag, s1, s2, d1, d2 in opcodes:
        if tag == "equal":
            for k in range(s2 - s1 + 1):
                start_map[s1 + k] = d1 + k
                end_map[s1 + k] = d1 + k
        elif tag == "replace":
            start_map[s1] = d1
            end_map[s2] = d2
            span = s2 - s1
            if span > 1:
                for k in range(1, span):
                    pos = s1 + k
                    mapped = d1 + round(k * (d2 - d1) / span)
                    start_map[pos] = mapped
                    end_map[pos] = mapped
        elif tag == "delete":
            for k in range(s1, s2 + 1):
                start_map[k] = d1
                end_map[k] = d1
        elif tag == "insert":
            # 起点包含插入内容；终点也包含插入内容（见上方「50 m → 45 m」）。
            start_map[s1] = d1
            end_map[s1] = d2
    return start_map, end_map


def _collect_spans(
    source: str,
    target: str,
    values: set[str],
    *,
    start_map: list[int],
    end_map: list[int],
    preserve: set[str],
) -> list[tuple[int, int, str]]:
    """在 `source` 中定位 `values`，映射为 `target` 上的待替换范围。"""
    spans: list[tuple[int, int, str]] = []
    for value in values:
        if not value:
            continue
        search_from = 0
        while True:
            idx = source.find(value, search_from)
            if idx < 0:
                break
            search_from = idx + 1
            src_end = idx + len(value)
            old = source[idx:src_end]
            dst_start, dst_end = start_map[idx], end_map[src_end]
            if dst_end <= dst_start:
                continue
            new = target[dst_start:dst_end]
            if any(p in old or p in new for p in preserve):
                # 与其它未拒绝冲突共享范围 → 不动，避免越权撤回。
                continue
            spans.append((dst_start, dst_end, old))
    return spans


def _dedupe_spans(spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """去重并丢弃重叠范围：重叠意味着归因不唯一，保守跳过后者。"""
    kept: list[tuple[int, int, str]] = []
    for span in sorted(spans, key=lambda s: (s[0], s[1])):
        dst_start, dst_end, _old = span
        if any(dst_start < k_end and k_start < dst_end for k_start, k_end, _ in kept):
            continue
        kept.append(span)
    return kept


def revert_conflict_changes(
    before: str,
    current: str,
    *,
    wrong_values: Iterable[str],
    authoritative_value: str = "",
    preserve_values: Iterable[str] = (),
) -> tuple[str, str]:
    """只撤销与 ``wrong_values`` / ``authoritative_value`` 相关的最小范围。

    参数均为字符串；空串/None 会被忽略。纯函数、确定性，不读写库。
    """
    before = before or ""
    current = current or ""
    if before == current:
        return current, "unchanged"

    wrong = {str(v) for v in (wrong_values or ()) if str(v or "")}
    auth = str(authoritative_value or "")
    keep = {str(v) for v in (preserve_values or ()) if str(v or "")}

    matcher = difflib.SequenceMatcher(None, before, current, autojunk=False)
    opcodes = matcher.get_opcodes()
    start_map, end_map = _build_offset_maps(opcodes, len(before), len(current))

    spans = _collect_spans(
        before,
        current,
        wrong,
        start_map=start_map,
        end_map=end_map,
        preserve=keep,
    )

    # 原取值在快照里已不存在（模型改写措辞）：用权威取值反向定位。
    wrong_found = any(bool(v) and v in before for v in wrong)
    if not wrong_found and auth and auth not in before:
        rev_opcodes = [(tag, j1, j2, i1, i2) for tag, i1, i2, j1, j2 in opcodes]
        rev_start_map, rev_end_map = _build_offset_maps(rev_opcodes, len(current), len(before))
        search_from = 0
        while True:
            idx = current.find(auth, search_from)
            if idx < 0:
                break
            search_from = idx + 1
            cur_end = idx + len(auth)
            before_start = rev_start_map[idx]
            before_end = rev_end_map[cur_end]
            old = before[before_start:before_end]
            if any(p in auth or p in old for p in keep):
                continue
            spans.append((idx, cur_end, old))

    spans = _dedupe_spans(spans)
    if not spans:
        return current, "not_located"

    text = current
    # 逆序应用（索引基于原 current，倒序保证前面的偏移不受影响）
    for dst_start, dst_end, old in reversed(spans):
        text = text[:dst_start] + old + text[dst_end:]
    if text == current:
        return current, "unchanged"
    return text, "reverted"
