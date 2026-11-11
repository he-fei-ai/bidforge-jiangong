"""就绪度评分引擎（六维加权 + 阻断项 + 放行结论）

为什么需要统一评分
------------------
此前的审核与预检是「五个各自为政的检查」：规范符合性、一致性审计、专家预检、
导出预检、质量自检，各自出各自的结论，用户拿到 5 份互相不搭界的报告，
无法回答一个最朴素的问题：**"这份方案现在能不能交付？"**

商业级审查工具（如 OpenBidKit 的 readiness、各类 bid/no-bid 评分卡）的共同做法是
把分散检查聚合为一个**可解释的综合分**：

1. **分维度**：按评审关注的六个维度分别打分，用户一眼看出短板在哪；
2. **可解释**：每个维度为什么扣这么多分，能下钻到具体规则与证据；
3. **有结论**：给出 A/B/C/D 等级与"是否放行"的明确建议，而不是让用户自己判断；
4. **有红线**：存在阻断项（block）时无论总分多少都不放行 ——
   毕竟"引用已废止标准"这种硬伤不会因为其他章节写得好就变得可接受。

评分口径
--------
- 每个维度满分 100，按命中问题的严重度扣分：block 40 / high 20 / medium 8 / low 2；
- 维度分扣到 0 为止（不为负）；
- 总分 = Σ(维度分 × 维度权重) / 100；
- 等级：A ≥ 90（可直接交付）/ B ≥ 75（可提交论证）/ C ≥ 60（需整改）/ D < 60；
- **阻断规则**：存在 block 级问题时，等级最高为 C，且 ``released=False``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.audit_rules import (
    DIMENSIONS,
    RULE_VERSION,
    SEVERITY_ORDER,
    SEVERITY_PENALTY,
    get_rule,
    grade_of,
)

#: 存在阻断项时的等级上限（"其他维度再好也不能放行"）
BLOCKED_MAX_GRADE = "C"


@dataclass
class DimensionScore:
    key: str
    label: str
    weight: int
    score: float  # 0-100
    penalty: float  # 累计扣分
    findings: list  # 该维度下的发现

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "weight": self.weight,
            "score": round(self.score, 1),
            "penalty": round(self.penalty, 1),
            "issue_count": len(self.findings),
            "block_count": sum(1 for f in self.findings if f.get("severity") == "block"),
        }


@dataclass
class ReadinessResult:
    total: float  # 0-100 综合分
    grade: str  # A / B / C / D
    verdict: str  # 结论文案
    released: bool  # 是否放行（可交付）
    blocked: bool  # 是否存在阻断项
    blockers: list  # 阻断项清单
    dimensions: list  # 各维度得分
    findings: list  # 全部发现（已排序）
    weakest: str  # 最弱维度 key
    rule_version: str = RULE_VERSION
    #: ✅ 新增（2026-09-21）：dimension 字段既不在 DIMENSIONS 里、也无映射兜底的
    #: 发现数量；非 0 意味着规则目录与维度定义分叉，运维需要关注。
    unknown_dimension_count: int = 0
    #: ✅ B-04（R175，可信度）：检查是否完整。``incomplete=True`` 表示有来源
    #: 没跑成（failed_sources）或只覆盖了部分正文（partial_sources），
    #: 即便分数够高也**不得**给出「建议放行」。
    incomplete: bool = False
    failed_sources: list = field(default_factory=list)
    partial_sources: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total": round(self.total, 1),
            "grade": self.grade,
            "verdict": self.verdict,
            "released": self.released,
            "blocked": self.blocked,
            "blockers": self.blockers,
            "weakest": self.weakest,
            "rule_version": self.rule_version,
            "dimensions": [d.as_dict() for d in self.dimensions],
            "counts": _count_by_severity(self.findings),
            "findings": self.findings,
            # ✅ 新增字段（2026-09-21）：未知维度计数，前端可忽略
            "unknown_dimension_count": self.unknown_dimension_count,
            # ✅ B-04（R175，加法式）：未完整检查的来源清单。前端/消费方
            #    据此显示「部分检查」或「来源失败」，而不是把它当完整通过。
            "incomplete": self.incomplete,
            "failed_sources": self.failed_sources,
            "partial_sources": self.partial_sources,
        }


def _count_by_severity(findings: list) -> dict:
    out = {"block": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings:
        sev = f.get("severity") or "low"
        if sev in out:
            out[sev] += 1
    out["total"] = len(findings)
    return out


def score_findings(
    findings: list, *, failed_sources: list | None = None, partial_sources: list | None = None
) -> ReadinessResult:
    """把发现列表聚合成就绪度评分。

    Args:
        findings: 统一结构的发现列表（见 ``preflight_engine`` 输出契约）。
        failed_sources: ✅ B-04（R175）完全没跑成的检查来源（如 AI 超时）。
        partial_sources: 跑成但只覆盖部分正文的来源（如 /check 总长截断）。

    Returns:
        ``ReadinessResult``：含总分、等级、放行结论、各维度得分与阻断项。
    """
    failed_sources = list(failed_sources or [])
    partial_sources = list(partial_sources or [])
    # ✅ B-04（R175，可信度）：来源失败 / 只检查了一部分，都不能算「已检」。
    #   旧实现 failed_sources 只放进 payload 展示，released 仍可能为 True ——
    #   AI 超时的方案照样显示「建议放行」。
    incomplete = bool(failed_sources) or bool(partial_sources)
    grouped: dict = {d.key: [] for d in DIMENSIONS}
    unknown_dim_count = 0
    for f in findings:
        # ✅ BUG 修复（2026-09-21）：旧实现 f.get("dimension") 返回 None 时
        #    key = None（不是 ""），下面 if key in grouped 走 else 分支
        #    但 setdefault 用 "deliverability" 字符串，两处不一致。现统一
        #    走 `or ""` 兜底，同时统计未知维度数量便于运维发现规则分叉。
        key = f.get("dimension") or ""
        if key in grouped:
            grouped[key].append(f)
        else:
            unknown_dim_count += 1
            # 未知维度（如自定义规则）：计入可交付性，避免评分凭空丢失
            grouped.setdefault("deliverability", []).append(f)

    # ✅ BUG 修复（2026-09-21）：旧实现权重按 100 硬编码除数，DIMENSIONS
    #    权重和一旦漂移（如新加维度未减其它权重）会悄悄放大总分。现按
    #    DIMENSIONS 实际权重和归一化，保证总分永远在 0-100 之间。
    total_weight = sum(d.weight for d in DIMENSIONS) or 1

    dim_scores: list = []
    total = 0.0
    for dim in DIMENSIONS:
        items = grouped.get(dim.key, [])
        penalty = sum(SEVERITY_PENALTY.get(f.get("severity") or "low", 0) for f in items)
        score = max(0.0, 100.0 - penalty)
        dim_scores.append(
            DimensionScore(
                key=dim.key, label=dim.label, weight=dim.weight, score=score, penalty=penalty, findings=items
            )
        )
        total += score * dim.weight / total_weight

    total = max(0.0, min(100.0, total))
    grade, verdict = grade_of(total)

    blockers = [f for f in findings if (f.get("severity") or "") == "block"]
    blocked = bool(blockers)
    if blocked:
        # 红线：存在阻断项时不允许给出 A / B 结论
        if grade in ("A", "B"):
            grade = BLOCKED_MAX_GRADE
            verdict = "存在交付阻断项，须整改后重新预检"

    weakest = min(dim_scores, key=lambda d: d.score).key if dim_scores else ""

    # ✅ BUG 修复（2026-09-21）：旧实现 released = (not blocked) and total >= 75，
    #    blocked 时 grade 被上限到 C，但 released 仍由 total 数值决定 ——
    #    若 total >= 75 且同时存在 block，会出现 grade=C + released=True
    #    的矛盾状态（"需整改"却"建议放行"）。现改为以 grade 为准：
    #    只有 grade ∈ {A, B} 且未被阻断才建议放行。
    released = (not blocked) and grade in ("A", "B") and not incomplete
    if incomplete and not blocked:
        verdict = verdict + "；检查不完整（部分来源失败或未覆盖全文），" "结果仅供参考，不得据此放行"

    return ReadinessResult(
        total=total,
        grade=grade,
        verdict=verdict,
        released=released,
        blocked=blocked,
        blockers=[_brief(f) for f in blockers],
        dimensions=dim_scores,
        findings=_sort_by_severity(findings),
        weakest=weakest,
        unknown_dimension_count=unknown_dim_count,
        incomplete=incomplete,
        failed_sources=failed_sources,
        partial_sources=partial_sources,
    )


def _brief(f: dict) -> dict:
    """阻断项摘要（前端置顶展示用，避免把整份证据塞进头部卡片）。"""
    return {
        "rule_id": f.get("rule_id") or "",
        "title": f.get("title") or "",
        "detail": f.get("detail") or "",
        "suggestion": f.get("suggestion") or "",
        "section_title": f.get("section_title") or "",
    }


def _sort_by_severity(findings: list) -> list:
    return sorted(
        findings, key=lambda f: (-SEVERITY_ORDER.get(f.get("severity") or "low", 0), f.get("rule_id") or "zzz")
    )


def _merge_dropped_evidence(target: dict, dropped: dict) -> None:
    """把**被去重丢弃**那条 finding 的证据并入保留者（P1 · 2026-10-04）。

    ✅ BUG 修复：`merge_findings` 此前对同 rule_id 只做「保留严重度更高者」，
    被丢弃那条的 `evidence` / `section_ids` / `source` **整条消失**。真实影响面
    （生产可复现）：`export_issues_to_findings`（export.py:1128，source=export_check，
    携带逐章节清单）与 `check_deliverability`（preflight_engine.py:834）都会产出
    DLV-01/02/03/04 —— 两者同严重度时程序侧胜出（先出现者被后出现的同严重度替换？
    实际是保留先到者），导出侧那 20 个章节的清单被静默丢弃，用户在就绪度面板
    看到的「N 处」只有程序侧口径，导出的具体问题无处可查。

    ⚠️ **不影响评分**：`score_findings` 只按 severity 扣分、不读 count
    （audit_scoring.py:138），因此本次合并是纯证据补全，分数/等级/放行结论零变化。

    只做并集去重合并，不做计数相加 —— 两条链路可能命中同一处，相加会虚增数量。
    """
    ev = list(target.get("evidence") or [])
    for e in dropped.get("evidence") or []:
        if e and e not in ev:
            ev.append(e)
    if ev:
        target["evidence"] = ev
    sids = list(target.get("section_ids") or [])
    for sid in dropped.get("section_ids") or []:
        if sid and sid not in sids:
            sids.append(sid)
    # 兼容「只带单个 section_id」形态的产出方
    _sid = dropped.get("section_id") or ""
    if _sid and not sids and _sid not in sids:
        sids.append(_sid)
    if sids:
        target["section_ids"] = sids
    src = dropped.get("source") or ""
    if src:
        srcs = list(target.get("sources") or [])
        if src not in srcs:
            srcs.append(src)
            target["sources"] = srcs


#: ✅ B-13（R175，双计消除）：语义同族规则。CON-01（预检数值一致性）与
#: CON-07（程序预扫描数值冲突）描述的是**同一件事**，同一冲突过去按 rule_id
#: 各计一次 —— 一致性维度被扣两遍分，用户看到两条几乎相同的问题。
#: 去重键由此从「rule_id」升级为「规则族 + 目标定位」。
_RULE_FAMILY: dict[str, str] = {
    "CON-01": "CON-NUMERIC",
    "CON-07": "CON-NUMERIC",
}


def _base_rule_key(rule_id: str) -> str:
    """取规则基号（去掉 ``-1`` 这类实例后缀），用于族判定与去重键。

    ✅ R212（2026-11-10 · P1）：**收敛到注册表单一事实源**。

    旧实现是**第二份**基号解析：只看「第二段是否数字」，因此
    ``CON-SCAN-1``（一致性扫描的派生编号）解析出 ``CON-SCAN-1`` ⇒ 查
    ``_RULE_FAMILY`` 落空 ⇒ **不与 ``CON-01`` 同族** ⇒ 同一处数值冲突被
    预检（CON-01）与一致性扫描（CON-SCAN-N）各计一次 ⇒ consistency 维度
    **双扣分**、用户看到两条几乎相同的问题。

    而注册表侧早已有正确实现：``audit_rules._resolve_base_rule`` 带别名表
    （``_RULE_ID_ALIASES = {"CON-SCAN": "CON-07"}``）与「逐级剥离 -<数字>」
    回落，能把 ``CON-SCAN-1`` 解析为基规则 ``CON-07``。两处同族实现强度
    漂移（本仓反复登记的病根），本轮把本函数改为**委托注册表**：

      ① 注册表能解析 ⇒ 用注册表给出的基号（含别名回落）；
      ② 注册表解析不出（未登记的规则 id）⇒ 退回本地「剥离 -<数字>」逻辑，
         保持既有行为逐字不变（不改变未登记规则的族判定）。
    """
    try:
        from app.services.audit_rules import _resolve_base_rule

        resolved = _resolve_base_rule(rule_id)
        if resolved is not None:
            return resolved.rule_id
    except Exception:  # noqa: BLE001 - 注册表异常时退回本地解析，不影响去重
        pass
    parts = (rule_id or "").split("-")
    if len(parts) >= 2 and parts[1].isdigit():
        return f"{parts[0]}-{parts[1]}"
    return rule_id or ""


def _target_signature(finding: dict) -> str:
    """目标定位签名：优先 section_id，其次归一后的 evidence 集合。"""
    sid = str(finding.get("section_id") or "").strip()
    if sid:
        return sid
    ev = sorted({str(x).strip() for x in (finding.get("evidence") or []) if str(x).strip()})
    return "|".join(ev)[:240]


def _dedup_key(finding: dict) -> str:
    """去重键：默认仍按 ``rule_id``；仅显式登记的语义同族才带目标定位。

    ✅ B-13（R175）：只按 rule_id 去重会让「同一冲突、两个规则号」各计一次；
    只按规则族去重又会把**不同数值冲突**（工期 vs 强度）错误合并。故对同族
    规则追加目标定位（章节或 evidence 集合），做到「同一处冲突只计一次，
    不同冲突各自成条」。

    ✅ R175 回归修复：目标定位**只对显式登记的族生效**。此前对全部规则生效，
    导致同一 rule_id（如 DLV-01）命中不同章节时不再合并、证据也不再并入 ——
    违反本函数「同 rule_id 去重」的既有契约（见 docstring），并造成重复扣分。
    """
    rid = str(finding.get("rule_id") or "")
    family = _RULE_FAMILY.get(_base_rule_key(rid))
    if not family:
        # 非登记族：保持旧契约 —— 同 rule_id 即一条，证据由 merge 阶段并入。
        return rid
    target = _target_signature(finding)
    if not target:
        # ✅ R212（2026-11-10）：无目标定位（既无 section_id 也无 evidence）时
        #    **不得**塌缩成一条 —— 同族的多个实例（CON-SCAN-1 / CON-SCAN-2 …）
        #    本就是**不同冲突**，塌缩会把「多处数值冲突」压成一条 ⇒ 用户只看到
        #    一处、总检少扣分（实测：既有护栏
        #    test_readiness_overview_six_sources::test_consistency_scan_only_
        #    counts_pending_failed 当场抓红）。
        #    旧实现之所以不塌缩，是因为 ``CON-SCAN-N`` 的基号解析失败（族查表
        #    落空）；R212 把基号解析收敛到注册表后族判定生效 ⇒ 必须在此补回退，
        #    否则同族多实例会被**新引入**的塌缩吞掉。
        return rid
    return f"{family}::{target}" if target else family


def merge_findings(*groups: list) -> list:
    """合并多来源发现（程序化预检 + AI 判定），按「规则族 + 目标定位」去重。

    同一规则被两条链路同时命中时保留严重度更高的一条，避免同一问题重复扣分。
    **被丢弃那条的证据会并入保留者**（见 :func:`_merge_dropped_evidence`），
    不再整条消失。

    ✅ 契约更新（2026-09-21）：本函数按 ``rule_id`` 去重，因此**产出方**必须
    保证 ``rule_id`` 在同一份 findings 内唯一。此前 preflight_engine 的 CON-05
    （章节查重）会为每一对重复章节都复用 rule_id="CON-05"，导致 N 对重复在
    merge 阶段被塌缩成 1 条 —— 现 preflight_engine 已改为 CON-05-1、CON-05-2 …
    编号，本函数保持按 rule_id 去重的语义不变。

    ✅ B-13（R175）：**仅对登记的同族规则**把去重键扩展为「规则族 + 目标定位」
    （``CON-01`` 与 ``CON-07`` 同族），同一数值冲突只保留一条；不同目标/不同
    evidence 的冲突仍各自保留。其余规则仍按 ``rule_id`` 去重，产出方可用实例
    后缀区分，行为不变。

    若某维度允许多条 finding 共用 rule_id（如"某规则命中 3 个不同章节"），
    产出方应显式加后缀区分；否则会被本函数视为重复并合并。
    """
    merged: dict = {}
    for group in groups:
        for f in group or []:
            rid = f.get("rule_id") or ""
            key = _dedup_key(f) if rid else (f.get("title") or "")
            if not key:
                # 无 rule_id 的临时发现（如检查项异常）直接保留
                merged[f"__anon_{len(merged)}"] = f
                continue
            prev = merged.get(key)
            if prev is None:
                merged[key] = f
                continue
            if SEVERITY_ORDER.get(f.get("severity"), 0) > SEVERITY_ORDER.get(prev.get("severity"), 0):
                # 新者胜出：把旧者的证据并入新者
                _merge_dropped_evidence(f, prev)
                merged[key] = f
            else:
                # 旧者胜出：把新者的证据并入旧者（此前是**整条丢弃**）
                _merge_dropped_evidence(prev, f)
    return _sort_by_severity(list(merged.values()))


#: ✅ B-18（R176）：AI severity 锚定注册表 —— 允许 AI 在注册表档位上下浮动一档
#: （模型看到更具体的证据时可比注册表更严/更宽），但**永不允许 AI 产出 block**：
#: 阻断项只能由确定性程序规则给出，否则模型一句「严重」就能把方案锁在 D 级；
#: 注册表本身为 block 时也不允许 AI 下调（红线不可被模型观感降级）。
_AI_SEVERITY_UP = 1
_AI_SEVERITY_DOWN = 1
_SEVERITY_BY_RANK = {v: k for k, v in SEVERITY_ORDER.items()}


def _anchor_ai_severity(rule_id: str, ai_severity: str) -> str:
    """把 AI 自报严重度锚定到规则注册表（B-18）。"""
    _ai = ai_severity if ai_severity in SEVERITY_PENALTY else "medium"
    _rule = get_rule(rule_id) if rule_id else None
    if _rule is None or _rule.severity not in SEVERITY_ORDER:
        # 注册表外的规则号（AI 自由生成）：保留档位但不允许 block
        return "high" if _ai == "block" else _ai
    _base = _rule.severity
    if _base == "block":
        return "block"
    _lo = max(0, SEVERITY_ORDER[_base] - _AI_SEVERITY_DOWN)
    _hi = min(SEVERITY_ORDER["high"], SEVERITY_ORDER[_base] + _AI_SEVERITY_UP)
    _rank = min(max(SEVERITY_ORDER[_ai], _lo), _hi)
    return _SEVERITY_BY_RANK.get(_rank, "medium")


#: ✅ R197（B3）：严重度档位词归一 —— AI 偶发返回中文档位（「高/中/低/阻断」）
#: 或 "block"，旧实现的 ``(r.get("severity") or "medium").lower()`` 与
#: ``_SEVERITY_RANK`` 词表都不识别：中文「高」在 ``_anchor_ai_severity`` 被降为
#: medium（锚定失真），「低」使「疑似低风险不扣分」策略失效（照扣 medium 分），
#: "block" 在 ``_SEVERITY_RANK`` 合并口径恒判 0。三处消费统一走本函数。
_SEVERITY_CN_MAP = {"高": "high", "中": "medium", "低": "low", "阻断": "block", "严重": "high"}


def normalize_severity_text(value: object) -> str:
    """把任意来源的严重度档位词归一为注册表词表（high/medium/low/block）。"""
    s = str(value or "").strip().lower()
    return _SEVERITY_CN_MAP.get(s, s)


def ai_results_to_findings(results: list) -> list:
    """把 AI 规范符合性检查的历史结果转换为统一 finding 结构。

    使 AI 结论能参与综合评分（此前 AI 结果只展示、不进评分，导致
    "看了 5 份报告仍然不知道能不能交付"）。
    """
    out: list = []
    for r in results or []:
        hit = r.get("hit")
        if hit is True:
            continue  # 命中即合规，不产生发现
        # ✅ R197（B3）：档位词归一（中文「高/中/低/阻断」→ 注册表词表），
        #    「疑似低风险不扣分」的判定与 _anchor_ai_severity 的锚定自此对中文输出同样生效。
        severity = normalize_severity_text(r.get("severity") or "medium")
        # "疑似"且 AI 自报低风险：不扣分，仅提示（用 AI 原始档位判定，
        # 锚定只影响最终计分档位，不改变「疑似低风险不扣分」的策略）
        if hit is None and severity == "low":
            continue
        out.append(
            {
                "rule_id": r.get("rule_id") or "",
                "dimension": _guess_dimension(r.get("rule_id") or ""),
                "severity": _anchor_ai_severity(r.get("rule_id") or "", severity),
                "title": r.get("item") or "AI 检查项",
                "detail": r.get("evidence") or "AI 判定为缺失 / 疑似缺失",
                "evidence": [r.get("evidence")] if r.get("evidence") else [],
                "section_id": "",
                "section_title": "",
                "suggestion": r.get("suggestion") or "",
                "basis": "AI 语义判定",
                "mode": "ai",
            }
        )
    return out


def _guess_dimension(rule_id: str) -> str:
    """按规则号前缀推断维度（兼容 AI 自由生成的 rule_id）。"""
    prefix = (rule_id or "").split("-")[0].upper()
    mapping = {
        "CMP": "completeness",
        "STD": "compliance",
        "SAF": "safety",
        "CON": "consistency",
        "TRC": "traceability",
        "DLV": "deliverability",
    }
    # ✅ BUG 修复：AI 自由生成的 rule_id（无前缀）默认映射到 content 维度而非
    # compliance。compliance 维度是"引用现行标准"的语义，AI 无法判定的未知规则
    # 应归入 completeness（内容完整性）作为兜底，避免误扣 compliance 分数。
    return mapping.get(prefix, "completeness")


def expert_result_to_findings(expert: dict) -> list:
    """把专家论证预检结果（missing 项）转换为统一 finding 结构。"""
    from app.services.audit_rules import EXPERT_ITEM_RULES, get_rule

    out: list = []
    for item in (expert or {}).get("missing") or []:
        rule_id = EXPERT_ITEM_RULES.get(item, "")
        rule = get_rule(rule_id) if rule_id else None
        out.append(
            {
                "rule_id": rule_id or "CMP-00",
                "dimension": rule.dimension if rule else "completeness",
                "severity": rule.severity if rule else "high",
                "title": f"论证必要项缺失：{item}",
                "detail": f"专家论证预检判定「{item}」尚未覆盖",
                "evidence": [],
                "section_id": "",
                "section_title": "",
                "suggestion": rule.detail if rule else f"请补充「{item}」内容",
                "basis": rule.basis if rule else "",
                "mode": "ai",
            }
        )
    return out


__all__ = [
    "ReadinessResult",
    "DimensionScore",
    "score_findings",
    "merge_findings",
    "ai_results_to_findings",
    "expert_result_to_findings",
    "grade_of",
]
