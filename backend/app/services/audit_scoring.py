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

from dataclasses import dataclass

from app.services.audit_rules import (
    DIMENSIONS, DIMENSION_MAP, RULE_VERSION, SEVERITY_ORDER, SEVERITY_PENALTY,
    grade_of,
)

#: 存在阻断项时的等级上限（"其他维度再好也不能放行"）
BLOCKED_MAX_GRADE = "C"


@dataclass
class DimensionScore:
    key: str
    label: str
    weight: int
    score: float          # 0-100
    penalty: float        # 累计扣分
    findings: list        # 该维度下的发现

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "weight": self.weight,
            "score": round(self.score, 1),
            "penalty": round(self.penalty, 1),
            "issue_count": len(self.findings),
            "block_count": sum(1 for f in self.findings
                               if f.get("severity") == "block"),
        }


@dataclass
class ReadinessResult:
    total: float                     # 0-100 综合分
    grade: str                       # A / B / C / D
    verdict: str                     # 结论文案
    released: bool                   # 是否放行（可交付）
    blocked: bool                    # 是否存在阻断项
    blockers: list                   # 阻断项清单
    dimensions: list                 # 各维度得分
    findings: list                   # 全部发现（已排序）
    weakest: str                     # 最弱维度 key
    rule_version: str = RULE_VERSION
    #: ✅ 新增（2026-09-21）：dimension 字段既不在 DIMENSIONS 里、也无映射兜底的
    #: 发现数量；非 0 意味着规则目录与维度定义分叉，运维需要关注。
    unknown_dimension_count: int = 0

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
        }


def _count_by_severity(findings: list) -> dict:
    out = {"block": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings:
        sev = f.get("severity") or "low"
        if sev in out:
            out[sev] += 1
    out["total"] = len(findings)
    return out


def score_findings(findings: list) -> ReadinessResult:
    """把发现列表聚合成就绪度评分。

    Args:
        findings: 统一结构的发现列表（见 ``preflight_engine`` 输出契约）。

    Returns:
        ``ReadinessResult``：含总分、等级、放行结论、各维度得分与阻断项。
    """
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
        dim_scores.append(DimensionScore(
            key=dim.key, label=dim.label, weight=dim.weight,
            score=score, penalty=penalty, findings=items))
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
    released = (not blocked) and grade in ("A", "B")

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
        findings,
        key=lambda f: (-SEVERITY_ORDER.get(f.get("severity") or "low", 0),
                       f.get("rule_id") or "zzz"))


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


def merge_findings(*groups: list) -> list:
    """合并多来源发现（程序化预检 + AI 判定），按规则 ID 去重。

    同一规则被两条链路同时命中时保留严重度更高的一条，避免同一问题重复扣分。
    **被丢弃那条的证据会并入保留者**（见 :func:`_merge_dropped_evidence`），
    不再整条消失。

    ✅ 契约更新（2026-09-21）：本函数按 ``rule_id`` 去重，因此**产出方**必须
    保证 ``rule_id`` 在同一份 findings 内唯一。此前 preflight_engine 的 CON-05
    （章节查重）会为每一对重复章节都复用 rule_id="CON-05"，导致 N 对重复在
    merge 阶段被塌缩成 1 条 —— 现 preflight_engine 已改为 CON-05-1、CON-05-2 …
    编号，本函数保持按 rule_id 去重的语义不变。

    若某维度允许多条 finding 共用 rule_id（如"某规则命中 3 个不同章节"），
    产出方应显式加后缀区分；否则会被本函数视为重复并合并。
    """
    merged: dict = {}
    for group in groups:
        for f in group or []:
            key = f.get("rule_id") or (f.get("title") or "")
            if not key:
                # 无 rule_id 的临时发现（如检查项异常）直接保留
                merged[f"__anon_{len(merged)}"] = f
                continue
            prev = merged.get(key)
            if prev is None:
                merged[key] = f
                continue
            if SEVERITY_ORDER.get(f.get("severity"), 0) > SEVERITY_ORDER.get(
                    prev.get("severity"), 0):
                # 新者胜出：把旧者的证据并入新者
                _merge_dropped_evidence(f, prev)
                merged[key] = f
            else:
                # 旧者胜出：把新者的证据并入旧者（此前是**整条丢弃**）
                _merge_dropped_evidence(prev, f)
    return _sort_by_severity(list(merged.values()))


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
        severity = (r.get("severity") or "medium").lower()
        if hit is None and severity == "low":
            continue  # "疑似"且低风险：不扣分，仅提示
        out.append({
            "rule_id": r.get("rule_id") or "",
            "dimension": _guess_dimension(r.get("rule_id") or ""),
            "severity": severity if severity in SEVERITY_PENALTY else "medium",
            "title": r.get("item") or "AI 检查项",
            "detail": r.get("evidence") or "AI 判定为缺失 / 疑似缺失",
            "evidence": [r.get("evidence")] if r.get("evidence") else [],
            "section_id": "", "section_title": "",
            "suggestion": r.get("suggestion") or "",
            "basis": "AI 语义判定",
            "mode": "ai",
        })
    return out


def _guess_dimension(rule_id: str) -> str:
    """按规则号前缀推断维度（兼容 AI 自由生成的 rule_id）。"""
    prefix = (rule_id or "").split("-")[0].upper()
    mapping = {
        "CMP": "completeness", "STD": "compliance", "SAF": "safety",
        "CON": "consistency", "TRC": "traceability", "DLV": "deliverability",
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
        out.append({
            "rule_id": rule_id or "CMP-00",
            "dimension": rule.dimension if rule else "completeness",
            "severity": rule.severity if rule else "high",
            "title": f"论证必要项缺失：{item}",
            "detail": f"专家论证预检判定「{item}」尚未覆盖",
            "evidence": [], "section_id": "", "section_title": "",
            "suggestion": rule.detail if rule else f"请补充「{item}」内容",
            "basis": rule.basis if rule else "",
            "mode": "ai",
        })
    return out


__all__ = [
    "ReadinessResult", "DimensionScore", "score_findings", "merge_findings",
    "ai_results_to_findings", "expert_result_to_findings", "grade_of",
]
