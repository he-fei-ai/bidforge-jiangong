"""数值锁（R179 Phase F · 2026-11-06）。

职责
----
规范驱动段落里的数值、阈值与强制性条文表述，必须可溯源到**检索到的条款
摘录**或**项目事实值**。本模块用代码做机械比对，不依赖提示词约束：

1. :func:`extract_numbers` 抽取正文数值 token（含单位）；
2. :func:`build_allowed_numbers` 构造允许集合（条款数值 ∪ 项目事实值）；
3. :func:`check_text` 逐项比对，集合外数值一律记为违规；
4. :func:`enforce_text` 按句 / 表格行拦截违规数值（拒绝写出），保留其余正文。

边界与设计约定
--------------
* 纯函数：不读 DB、不调 LLM、不写文件，便于单测与变异验证；
* 代码围栏（mermaid / chart-json）整体跳过 —— 图内数值来自事实注入，
  不参与正文比对，避免把绘图语法误判为编造数值；
* 章节编号、表/图号、条款号、标准号、日期与时刻不是「正文数值」，一律跳过；
* 单位换算只覆盖长度 / 质量 / 力 / 压强 / 时间五族；未知单位按字面值比较；
* 无单位 ``5`` 与 ``5m`` 视为同一数值（宁可漏拦不可误杀）；
  ``5m`` → ``6m`` 因数值不同必被拦。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.services.fence_common import fence_char_ranges

#: 非「正文数值」的文本片段（掩码后不再参与抽取）。
_MASK_RES = (
    # 行首章节编号：``3.1.2 施工准备`` / ``### 3.1.2``
    re.compile(r"^(?:#{1,6}[ \t]*)?\d+(?:\s*\.\s*\d+)+[ \t\u3000]+", re.MULTILINE),
    # 表号 / 图号：``表 3-1`` / ``图 5.2-3``
    re.compile(r"(?:表|图)\s*\d+(?:[.\-—–]\d+)+"),
    # 条款号：``第5.2.1条``
    re.compile(r"第\s*\d+(?:\s*\.\s*\d+)*\s*[条款章节]"),
    # 标准号：``GB 50268-2008`` / ``GB/T 50300-2013`` / ``JGJ 130-2011``
    re.compile(r"(?<![A-Za-z])[A-Z]{2,}(?:\s*/\s*[A-Z])?[ \t]*-?[ \t]*\d+(?:[.\-/—–]\d+)*"),
    # 日期：``2026年11月6日`` / ``2026-11-06``
    re.compile(r"20\d{2}\s*[年./-]\s*\d{1,2}(?:\s*[月./-]\s*\d{1,2}\s*日?)?"),
    # 时刻：``08:30`` / ``08:30:00``
    re.compile(r"\d{1,2}:\d{2}(?::\d{2})?"),
)

#: 单位候选（长单位优先，避免 ``m`` 抢先吃掉 ``mm`` / ``min`` / ``m³``）。
_UNIT_ALT = "mm³|mm²|cm²|m³|m²|km|mm|cm|dm|kPa|MPa|kN·m|kN/m²|kN/m|kN|kg|min|t|m|g|h|s|Pa|N|%|℃|°C"

_NUMBER_RE = re.compile(r"(?<![0-9.])(\d+(?:\s*\.\s*\d+)?)(?:\s*(" + _UNIT_ALT + r"))?")

#: 单位 → (量纲族, 换算到族基准单位的倍率)。
_UNIT_FAMILY: dict[str, tuple[str, Decimal]] = {
    "mm": ("length", Decimal("0.001")),
    "cm": ("length", Decimal("0.01")),
    "dm": ("length", Decimal("0.1")),
    "m": ("length", Decimal("1")),
    "km": ("length", Decimal("1000")),
    "g": ("mass", Decimal("1")),
    "kg": ("mass", Decimal("1000")),
    "t": ("mass", Decimal("1000000")),
    "n": ("force", Decimal("1")),
    "kn": ("force", Decimal("1000")),
    "pa": ("pressure", Decimal("1")),
    "kpa": ("pressure", Decimal("1000")),
    "mpa": ("pressure", Decimal("1000000")),
    "s": ("time", Decimal("1")),
    "min": ("time", Decimal("60")),
    "h": ("time", Decimal("3600")),
    "%": ("percent", Decimal("1")),
}


@dataclass(frozen=True)
class NumericToken:
    """单个正文数值：原始写法 + 数值 + 单位 + 字符位置。"""

    raw: str
    value: Decimal
    unit: str = ""
    position: int = 0


def _masked_text(text: str) -> str:
    """把围栏与编号类片段替换为空格（保持字符下标不变）。

    ✅ R19x（2026-11-09）：围栏掩码改走 ``fence_common.fence_char_ranges``
    （R76 单一事实源）。旧的本地三反引号非贪婪正则有两处缺口：
    ① ``~~~`` 波浪围栏完全不掩 —— 图内数值被当正文数值参与比对；
    ② 非贪婪配对对「未闭合块 + 后续另一处围栏」会把两块间的**正文**
    一起掩掉、块内代码反而滑出保护区；行级判据（看信息串 + 长度容错）
    对两类围栏与未闭合块都给出正确区间。
    """
    chars = list(text or "")

    def _blank(start: int, end: int) -> None:
        for i in range(start, end):
            if chars[i] != "\n":
                chars[i] = " "

    for start, end in fence_char_ranges(text or ""):
        _blank(start, end)
    for pattern in _MASK_RES:
        # 掩码会改变文本内容，故每轮都基于当前字符数组重新匹配。
        for match in pattern.finditer("".join(chars)):
            _blank(match.start(), match.end())
    return "".join(chars)


def extract_numbers(text: str) -> list[NumericToken]:
    """抽取正文数值（跳过围栏、编号、条款号、标准号、日期与时刻）。"""
    if not text:
        return []
    masked = _masked_text(text)
    tokens: list[NumericToken] = []
    for match in _NUMBER_RE.finditer(masked):
        raw_num = match.group(1)
        unit = (match.group(2) or "").strip()
        try:
            value = Decimal(re.sub(r"\s+", "", raw_num))
        except InvalidOperation:  # pragma: no cover - 正则已保证可解析
            continue
        tokens.append(NumericToken(raw=(raw_num + unit), value=value, unit=unit, position=match.start(1)))
    return tokens


def _same_value(left: NumericToken, right: NumericToken) -> bool:
    """同量纲族按换算值比较，否则按字面值比较。"""
    lf = _UNIT_FAMILY.get(left.unit.lower())
    rf = _UNIT_FAMILY.get(right.unit.lower())
    if lf and rf and lf[0] == rf[0]:
        return left.value * lf[1] == right.value * rf[1]
    return left.value == right.value


@dataclass(frozen=True)
class AllowedNumbers:
    """允许写出的数值集合（条款摘录数值 ∪ 项目事实值）。"""

    tokens: tuple[NumericToken, ...] = ()

    def contains(self, token: NumericToken) -> bool:
        return any(_same_value(token, allowed) for allowed in self.tokens)

    def display(self, limit: int = 40) -> str:
        """去重后的可读清单（供提示词注入；超出 limit 时截断并注明）。"""
        seen: list[str] = []
        for token in self.tokens:
            label = token.raw.strip()
            if label and label not in seen:
                seen.append(label)
        if not seen:
            return ""
        if len(seen) > limit:
            return "、".join(seen[:limit]) + f" 等 {len(seen)} 项"
        return "、".join(seen)


def merge_allowed_numbers(*sets: AllowedNumbers) -> AllowedNumbers:
    """合并多个数值集合（按数值 + 单位去重）。"""
    unique: list[NumericToken] = []
    for one in sets:
        for token in one.tokens:
            if not any(_same_value(token, exist) and token.unit == exist.unit for exist in unique):
                unique.append(token)
    return AllowedNumbers(tokens=tuple(unique))


def build_allowed_numbers(
    clause_texts: tuple[str, ...] | list[str] = (),
    fact_texts: tuple[str, ...] | list[str] = (),
    extra_texts: tuple[str, ...] | list[str] = (),
) -> AllowedNumbers:
    """同一归组内的允许集合 = 条款摘录数值 ∪ 项目事实值（可附加上下文文本）。"""
    tokens: list[NumericToken] = []
    for group in (clause_texts, fact_texts, extra_texts):
        for text in group or ():
            tokens.extend(extract_numbers(str(text or "")))
    return merge_allowed_numbers(AllowedNumbers(tokens=tuple(tokens)))


@dataclass(frozen=True)
class AllowedGroup:
    """一条具体条款（或项目事实）对应的数值集合。"""

    uid: str
    label: str
    numbers: AllowedNumbers
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScopedAllowedNumbers:
    """按 ``clause_uid`` 分组的允许集合。

    绑定规则：一个句子只能落在**某一条具体条款**的数值集合内（组内已并入
    项目事实值），不跨条款合并。否则「条款 A 写 5m、条款 B 写 8m」时把引用
    A 的句子改成 8m 会因 8m 在并集里而漏拦 —— 这是本类存在的唯一理由。
    """

    groups: tuple[AllowedGroup, ...] = ()

    @property
    def tokens(self) -> tuple[NumericToken, ...]:
        """各组数值并集（仅用于提示词展示与「是否存在可比对数值」判定）。"""
        return merge_allowed_numbers(*(group.numbers for group in self.groups)).tokens

    def display_groups(self) -> list[tuple[str, str]]:
        return [(group.label, group.numbers.display()) for group in self.groups]


def build_scoped_allowed_numbers(
    clause_items: tuple[tuple[str, str, tuple[str, ...], str], ...] | list[tuple[str, str, tuple[str, ...], str]] = (),
    fact_texts: tuple[str, ...] | list[str] = (),
) -> ScopedAllowedNumbers:
    """按条款分组构造允许集合；每组 = 该条款摘录数值 ∪ 项目事实值。

    ``clause_items`` 每项为 ``(uid, label, aliases, excerpt)``；``aliases``
    用于识别句子引用了哪一条条款（条款号 / 条款标识）。
    """
    fact_numbers = build_allowed_numbers(fact_texts)
    groups: list[AllowedGroup] = []
    for uid, label, aliases, excerpt in clause_items or ():
        numbers = merge_allowed_numbers(build_allowed_numbers([excerpt]), fact_numbers)
        groups.append(AllowedGroup(uid=str(uid), label=str(label), numbers=numbers, aliases=tuple(aliases)))
    if fact_numbers.tokens:
        # 仅引用项目事实值（未引用任何条款）的句子归到这一组。
        groups.append(AllowedGroup(uid="project_facts", label="项目事实", numbers=fact_numbers))
    return ScopedAllowedNumbers(groups=tuple(groups))


@dataclass
class NumericLockResult:
    """三态校验结果：``ok`` / ``blocked`` / ``undetermined``。

    ``undetermined`` 指「句子含数值但绑定不到任何具体条款」——不得降级为通过：
    没有条款号的 RAG 句子既无法确认合规，也无法确认违规，只能如实记为无法判定。
    """

    checked: int = 0
    violations: list[dict] = field(default_factory=list)
    allowed_size: int = 0
    removed_segments: int = 0
    blocked_chars: int = 0
    undetermined: list[dict] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.violations:
            return "blocked"
        if self.undetermined:
            return "undetermined"
        return "ok"

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "checked": self.checked,
            "allowed_size": self.allowed_size,
            "violations": list(self.violations),
            "undetermined": list(self.undetermined),
            "removed_segments": self.removed_segments,
            "blocked_chars": self.blocked_chars,
        }


def _context(text: str, position: int, window: int = 14) -> str:
    start = max(0, position - window)
    end = min(len(text), position + window)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _group_cited(segment: str, group: AllowedGroup) -> bool:
    return any(alias and alias in segment for alias in group.aliases)


def _facts_group(scoped: ScopedAllowedNumbers) -> AllowedGroup | None:
    for group in scoped.groups:
        if group.uid == "project_facts":
            return group
    return None


def _scoped_segment_verdict(segment: str, scoped: ScopedAllowedNumbers) -> tuple[list[NumericToken], bool]:
    """句子级归组判定，返回 ``(需比对 token, 是否无法判定)``。

    * 无数值 → ``([], False)``；
    * 句子引用了具体条款（uid / 条款号 / 第…条）→ 只允许落在该条款组内，
      不命中即违规（``(tokens, False)``）；
    * 未引用条款 → 只有**全部数值都落在项目事实组**才算合法；否则即「绑定不到
      任何条款」，返回 ``(tokens, True)`` 由调用方记为 undetermined。
      这里绝不回退到各组并集 —— 否则不写条款号就能绕过锁。
    """
    tokens = extract_numbers(segment)
    if not tokens:
        return [], False
    cited = [group for group in scoped.groups if _group_cited(segment, group)]
    if cited:
        for group in cited:
            if all(group.numbers.contains(token) for token in tokens):
                return [], False
        return tokens, False
    facts = _facts_group(scoped)
    if facts is not None and all(facts.numbers.contains(token) for token in tokens):
        return [], False
    return tokens, True


def _segment_spans(text: str):
    """产出 ``(段文本, 绝对起始位置)``：表格行整行成段，其余按句切分。"""
    offset = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if body.lstrip().startswith("|"):
            yield body, offset
        else:
            for match in _SENTENCE_RE.finditer(body):
                yield match.group(0), offset + match.start()
        offset += len(line)


def _in_fence(ranges: list[tuple[int, int]], pos: int) -> bool:
    """位置是否落在代码围栏区间内（fence_common 单一事实源的行级消费）。"""
    return any(s <= pos < e for s, e in ranges)


def _segment_blocked(segment: str, allowed: AllowedNumbers | ScopedAllowedNumbers) -> bool:
    """仅「违规」才拦截；无法判定（未绑定条款）不删正文，只记 undetermined。"""
    if isinstance(allowed, ScopedAllowedNumbers):
        offending, unbound = _scoped_segment_verdict(segment, allowed)
        # ✅ R215（2026-11-11 · P2）：undetermined 句**只记账不删正文** —— 对齐
        #    本 docstring 与 enforce_text 前置闸（check_text 的 violations 桶
        #    不含 undetermined）的三态语义。旧实现丢弃 unbound 判据，使未绑定
        #    条款的合法句子（如设备台数、经验值）在重建循环中被整句静默删除
        #    （行为实证：「采用挖掘机 2 台进场。」被删，removed=2）。
        return bool(offending) and not unbound
    return not all(allowed.contains(token) for token in extract_numbers(segment))


def _check_scoped_text(text: str, allowed: ScopedAllowedNumbers) -> NumericLockResult:
    checked = 0
    violations: list[dict] = []
    undetermined: list[dict] = []
    _ranges = fence_char_ranges(text or "")
    for segment, start in _segment_spans(text or ""):
        # ✅ R215（2026-11-11 · P2）：围栏内行不参与逐句比对 —— 作用域路径此前
        #    对每个片段单独抽取数值，片段看不到外围围栏（全文路径的 _masked_text
        #    掩码在这里失效），mermaid/chart-json 代码行被当散文句记入
        #    undetermined 报告（噪音），且与 _segment_blocked 旧缺陷复合时整段
        #    删除图内代码。模块边界（文件头）明言「代码围栏整体跳过」。
        if _in_fence(_ranges, start):
            continue
        tokens = extract_numbers(segment)
        checked += len(tokens)
        bad, unbound = _scoped_segment_verdict(segment, allowed)
        bucket = undetermined if unbound else violations
        for token in bad:
            position = start + token.position
            bucket.append(
                {
                    "raw": token.raw,
                    "value": str(token.value),
                    "unit": token.unit,
                    "position": position,
                    "context": _context(text or "", position),
                }
            )
    return NumericLockResult(
        checked=checked,
        violations=violations,
        undetermined=undetermined,
        allowed_size=len(allowed.tokens),
    )


def check_text(text: str, allowed: AllowedNumbers | ScopedAllowedNumbers) -> NumericLockResult:
    """逐项比对：集合外（或跨条款拼接）的数值即为违规（不修改正文）。"""
    if isinstance(allowed, ScopedAllowedNumbers):
        return _check_scoped_text(text, allowed)
    tokens = extract_numbers(text or "")
    violations = [
        {
            "raw": token.raw,
            "value": str(token.value),
            "unit": token.unit,
            "position": token.position,
            "context": _context(text or "", token.position),
        }
        for token in tokens
        if not allowed.contains(token)
    ]
    return NumericLockResult(checked=len(tokens), violations=violations, allowed_size=len(allowed.tokens))


_SENTENCE_RE = re.compile(r"[^。；;！？!?\n]+[。；;！？!?]?")


def enforce_text(text: str, allowed: AllowedNumbers | ScopedAllowedNumbers) -> tuple[str, NumericLockResult]:
    """拦截违规数值：表格行整行删除，普通句删除命中句，其余正文保留。

    Returns:
        ``(改写后的正文, 校验结果)``；无违规时正文原样返回（逐字不变）。
    """
    result = check_text(text, allowed)
    if not result.violations:
        # 无违规即正文逐字返回；undetermined 只记账不改写。
        return text, result

    kept_lines: list[str] = []
    removed = 0
    # ✅ R215（2026-11-11 · P2）：行级围栏掩码 —— 重建循环逐句判定此前看不到
    #    外围围栏（全文路径的 _masked_text 只作用于 check_text 的 violations
    #    前置闸），同章存在真违规时 mermaid/chart-json 代码行被当散文句删除
    #    （行为实证：gantt 围栏被掏空只剩围栏壳，scoped 与非 scoped 双中招）。
    _fence_ranges = fence_char_ranges(text or "")
    _offset = 0
    for line in (text or "").splitlines(keepends=True):
        if _in_fence(_fence_ranges, _offset):
            kept_lines.append(line)
            _offset += len(line)
            continue
        _offset += len(line)
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        segments = [body] if body.lstrip().startswith("|") else _SENTENCE_RE.findall(body)
        if not segments or not any(_segment_blocked(segment, allowed) for segment in segments):
            kept_lines.append(line)
            continue
        if body.lstrip().startswith("|"):
            removed += 1
            continue
        kept_segments: list[str] = []
        for segment in segments:
            if _segment_blocked(segment, allowed):
                removed += 1
                continue
            kept_segments.append(segment)
        kept_body = "".join(kept_segments).strip()
        if kept_body:
            kept_lines.append(kept_body + ending)
        else:
            removed += 1

    new_text = "".join(kept_lines)
    result.removed_segments = removed
    result.blocked_chars = max(0, len(text or "") - len(new_text))
    return new_text, result
