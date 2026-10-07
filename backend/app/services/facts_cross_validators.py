"""全局事实交叉校验器（纯程序确定性判定，零 LLM 依赖）

来源：《全局事实变量提取功能可行性研究报告》v2.0 下篇·补充②
执行时机：步骤⑧ 程序硬规则阶段（先于 LLM 软判定，零成本先行）。

三组规则：
- XV-MAT-DESIGN  材料规格 ↔ 设计参数（等级序列比对，范围值兼容）
- XV-MACH-SCHED  机械进退场时间 ↔ 工期（时序约束链 C1~C5）
- XV-FLOW-SEQ    流程序列 ↔ 流程序列（LCS 相似度 + 换位专项检测）

设计原则：全部程序判定 → 100% 可复现、可测试、零边际成本；
所有规则输出统一进入"⚠ 待裁决冲突"，自动裁决恒为 false（裁决权在人工）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from datetime import date

from app.services.ai.prompts._norm_dicts import (
    grade_rank,
    is_range_value,
    normalize_machinery_name,
    normalize_material_spec,
    normalize_process_name,
)

logger = logging.getLogger("facts_cross_validators")


def _safe_confidence(v, default: float = 1.0) -> float:
    """置信度容错：脏值/None/NaN 回落默认，避免落库 float() 抛异常。

    ✅ 本地定义（不 import facts_extractor）：facts_extractor 反向 import 本模块
    （pipeline 调用 run_cross_validations），在此再 import 会形成循环依赖。
    """
    try:
        f = float(v)
        if f != f:  # NaN
            return default
        return f
    except (TypeError, ValueError):
        return default


# 时序越界 severity 分档（天）
_DAY_BANDS = [(3, "low"), (30, "medium")]  # ≤3 low；3~30 medium；>30 high
# 塔吊退场不得早于主体封顶节点（C5 安全相关约束）
_TAPOSTRING_KEYWORDS = ("封顶", "主体结构封顶", "主体封顶")


@dataclass
class CrossConflict:
    rule_id: str
    severity: str            # low / medium / high
    conflict_type: str
    side_a: dict             # {"name","value","source","confidence"}
    side_b: dict
    resolution_hint: str
    auto_resolvable: bool = False  # 恒为 False，裁决权在人工

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 日期归一化（约束 C4：异构格式兼容）
# ---------------------------------------------------------------------------
_DATE_PATTERNS = [
    (re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$"), (1, 2, 3)),
    (re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日?$"), (1, 2, 3)),
]


def parse_date_flexible(text: str) -> date | None:
    """异构日期归一：2027-03-15 / 2027/3/15 / 2027年3月15日 / 2027.03.15。
    归一失败返回 None（调用方标记 needs_review，不参与判定）。"""
    if not text:
        return None
    t = re.sub(r"\s+", "", str(text))
    for pat, idx in _DATE_PATTERNS:
        m = pat.match(t)
        if m:
            try:
                return date(int(m.group(idx[0])), int(m.group(idx[1])),
                            int(m.group(idx[2])))
            except ValueError:
                return None
    return None


def _severity_by_days(days: int) -> str:
    for band, sev in _DAY_BANDS:
        if days <= band:
            return sev
    return "high"


def _side(item, value: str | None = None) -> dict:
    return {
        "name": item.name,
        "value": value if value is not None else item.value,
        "source": item.source,
        "confidence": item.confidence,
    }


# ---------------------------------------------------------------------------
# 规则 XV-MAT-DESIGN：材料规格 vs 设计参数
# ---------------------------------------------------------------------------
def _check_material_vs_design(merged) -> tuple[list[CrossConflict], list]:
    """同名材料/规格事实多值冲突 → 等级序列比对三层递进判定。

    返回 (conflicts, range_merged)：range_merged 为
    "一方为范围值且另一方满足该范围"的合并项（撤销冲突标记）。
    ✅ 加固（2026-09-21）：range_merged 存【条目对象本身】而非 name 字符串 ——
    旧实现按 name 全文匹配撤销，若合并池中存在同名不同 key 的事实
    （归一化键不同但显示名相同），会把无关条目的矛盾标记一并误撤销。

    ✅ BUG 修复：
    1. 序列类（list）值不属材料规格维度，先跳过；旧实现对 list 调用
       grade_rank → _half_width → unicodedata.normalize 抛异常，
       使整组交叉校验被 except 吞掉（三组规则全部静默失效）。
    2. 范围值判定方向错误：旧实现用"范围侧基准等级"与自身比较，
       使"C20 vs 不低于C30"这类真实越界也被误判为兼容。现改为
       "范围侧基准等级 vs 实际取用值"单向比较。
    """
    conflicts: list[CrossConflict] = []
    range_merged: list = []
    for it in merged:
        if not it.has_conflict or not it.conflict_values:
            continue
        # ① BUG 修复：跳过序列类值（流程/部署等多值数组）
        if isinstance(it.value, (list, tuple)):
            continue
        best_rank = grade_rank(it.value)
        for cand in it.conflict_values:
            cand_val = cand.get("value", "")
            if isinstance(cand_val, (list, tuple)):
                continue
            other_v = str(cand_val)
            if _norm_eq(other_v, str(it.value)):
                continue
            other_rank = grade_rank(other_v)

            # ② 范围值兼容优先判定：一方为范围（"不低于C30"/"C30及以上"）
            rng_main = is_range_value(str(it.value))
            rng_other = is_range_value(other_v)
            if rng_main or rng_other:
                if rng_main and rng_other:
                    # 两侧均为范围值，无法单向判定（如 C30及以上 vs C35及以上）
                    conflicts.append(CrossConflict(
                        rule_id="XV-MAT-DESIGN", severity="low",
                        conflict_type="material_range_ambiguous",
                        side_a=_side(it), side_b=_side(it, other_v),
                        resolution_hint="两侧均为范围值，无法程序判定，请人工确认取用口径"))
                    continue
                floor_str = rng_main or rng_other       # 范围侧基准等级
                actual_str = other_v if rng_main else str(it.value)  # 实际取用值
                floor_rank = grade_rank(floor_str)
                actual_rank = grade_rank(actual_str)
                if floor_rank and actual_rank and floor_rank[0] == actual_rank[0]:
                    if actual_rank[1] >= floor_rank[1]:
                        # 实际值满足"不低于"约束 → 合并，不判冲突
                        range_merged.append(it)
                    else:
                        conflicts.append(CrossConflict(
                            rule_id="XV-MAT-DESIGN", severity="high",
                            conflict_type="material_below_range_floor",
                            side_a=_side(it), side_b=_side(it, other_v),
                            resolution_hint="实际取用值低于范围约束下限，"
                                            "疑似笔误或取值错误"))
                    continue

            # ③ 同类可比 → 档位差分档
            if best_rank and other_rank and best_rank[0] == other_rank[0]:
                dist = abs(best_rank[1] - other_rank[1])
                if dist == 0:
                    continue  # 同档不同写法（已归一不应出现，防御）
                sev = "medium" if dist == 1 else "high"
                hint = ("疑似笔误或规格替代，需确认" if dist == 1 else
                        "等级差 ≥2 档，禁止自动取舍")
                conflicts.append(CrossConflict(
                    rule_id="XV-MAT-DESIGN", severity=sev,
                    conflict_type="material_vs_design_param",
                    side_a=_side(it), side_b=_side(it, other_v),
                    resolution_hint=f"{hint}；设计文件值优先，请核对施工方案材料等级"))
                continue

            # ④ 不可比（如 C30 vs HRB400）→ 名称归一疑似错误，转人工复核。
            #    仅当至少一侧形似规格等级才进入本规则（防止流程字符串等
            #    普通同值冲突被误报）。
            if best_rank or other_rank or rng_main or rng_other:
                conflicts.append(CrossConflict(
                    rule_id="XV-MAT-DESIGN", severity="low",
                    conflict_type="material_name_normalize_review",
                    side_a=_side(it), side_b=_side(it, other_v),
                    resolution_hint="两值不同类且无法比对，疑似名称归一错误，转人工复核"))
    return conflicts, range_merged


def _norm_eq(a: str, b: str) -> bool:
    return normalize_material_spec(a) == normalize_material_spec(b)


# ---------------------------------------------------------------------------
# 规则 XV-MACH-SCHED：机械进退场时间 ↔ 工期
# ---------------------------------------------------------------------------
_ENTRY_PAT = re.compile(r"进场时间?$|计划进场")
_EXIT_PAT = re.compile(r"退场时间?$|计划退场")
_TOP_PAT = re.compile(r"开工日期?$|计划开工|总工期")
_END_PAT = re.compile(r"竣工日期?$|计划竣工|完工日期")


def _check_machinery_vs_schedule(merged) -> list[CrossConflict]:
    conflicts: list[CrossConflict] = []
    start_dt = end_dt = topout_dt = None
    start_item = end_item = topout_item = None
    mach_items: list = []

    for it in merged:
        d = parse_date_flexible(it.value)
        if d is None:
            continue
        if _ENTRY_PAT.search(it.name) or _EXIT_PAT.search(it.name):
            mach_items.append(it)
        elif _TOP_PAT.search(it.name) and start_dt is None:
            start_dt, start_item = d, it
        elif _END_PAT.search(it.name) and end_dt is None:
            end_dt, end_item = d, it
        elif any(k in it.name for k in _TAPOSTRING_KEYWORDS):
            topout_dt, topout_item = d, it

    for it in mach_items:
        d = parse_date_flexible(it.value)
        if d is None:
            logger.info("机械日期 %s=%s 归一失败，标记 needs_review",
                        it.name, it.value)
            continue
        is_entry = bool(_ENTRY_PAT.search(it.name))
        is_tower = "塔吊" in normalize_machinery_name(it.name)

        # C3：单台机械自身时序（同一机械名的进场/退场成对检查）
        pair_name = re.sub(r"(进场|退场)时间?$", "", it.name)
        for other in mach_items:
            if other is it:
                continue
            od = parse_date_flexible(other.value)
            if od is None:
                continue
            if other.name.startswith(pair_name) and \
                    is_entry != bool(_ENTRY_PAT.search(other.name)):
                if is_entry and d > od:
                    conflicts.append(CrossConflict(
                        rule_id="XV-MACH-SCHED", severity="high",
                        conflict_type="machinery_entry_after_exit",
                        side_a=_side(it), side_b=_side(other),
                        resolution_hint="单台机械进场晚于退场，自身时序矛盾"))
                break

        if is_entry:
            # C1：进场 ≥ 开工日期
            if start_dt and d < start_dt:
                days = (start_dt - d).days
                conflicts.append(CrossConflict(
                    rule_id="XV-MACH-SCHED",
                    severity=_severity_by_days(days),
                    conflict_type="machinery_entry_vs_schedule",
                    side_a=_side(it), side_b=_side(start_item),
                    resolution_hint=f"进场时间早于开工日期 {days} 天，"
                                    "核对进退场计划或工期取值"))
        else:
            # C2：退场 ≤ 竣工日期
            if end_dt and d > end_dt:
                days = (d - end_dt).days
                sev = _severity_by_days(days)
                hint = f"退场时间晚于竣工日期 {days} 天。" if days > 0 \
                    else "退场时间不早于竣工日期。"
                conflicts.append(CrossConflict(
                    rule_id="XV-MACH-SCHED", severity=sev,
                    conflict_type="machinery_exit_vs_schedule",
                    side_a=_side(it), side_b=_side(end_item),
                    resolution_hint=hint + "核对进退场计划或工期取值"))
            # C5：塔吊退场不得早于主体封顶节点（安全相关，独立判定）
            if is_tower and topout_dt and d < topout_dt:
                conflicts.append(CrossConflict(
                    rule_id="XV-MACH-SCHED", severity="high",
                    conflict_type="machinery_exit_before_topout",
                    side_a=_side(it), side_b=_side(topout_item),
                    resolution_hint="塔吊退场早于主体结构封顶节点，"
                                    "属安全相关时序冲突，必须人工裁决"))
    return conflicts


# ---------------------------------------------------------------------------
# 规则 XV-FLOW-SEQ：流程序列一致性（LCS + 换位专项）
# ---------------------------------------------------------------------------
_FLOW_ARROW = re.compile(r"\s*(?:→|->|—>|▼|↓)\s*")


def _split_flow(value: str | list) -> list[str]:
    """把箭头链字符串或 list value 拆为工序序列

    ✅ 升级（研究报告 v2.0 兼容）：新格式 fact_type=process_flow 的
    value 可能是有序数组，直接使用；旧格式箭头链字符串用箭头拆。
    """
    if isinstance(value, list):
        return [str(p).strip() for p in value if p is not None and str(p).strip()]
    if not value:
        return []
    return [p for p in (s.strip() for s in _FLOW_ARROW.split(str(value))) if p]


def _has_flow_content(item) -> bool:
    """判断一条事实是否是流程序列（兼容新/旧格式）"""
    v = item.value
    if isinstance(v, list):
        return len(v) >= 2  # 数组 ≥2 个元素就视为序列
    return bool(_FLOW_ARROW.search(str(v)))


def _lcs_len(a: list[str], b: list[str]) -> int:
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            dp[i][j] = (dp[i - 1][j - 1] + 1 if a[i - 1] == b[j - 1]
                        else max(dp[i - 1][j], dp[i][j - 1]))
    return dp[-1][-1]


def _has_adjacent_swap(a: list[str], b: list[str]) -> bool:
    """换位专项：LCS 相同但存在相邻两工序顺序相反"""
    for i in range(len(a) - 1):
        pair = (a[i], a[i + 1])
        for j in range(len(b) - 1):
            if pair == (b[j + 1], b[j]):
                return True
    return False


def _check_flow_sequence(merged) -> list[CrossConflict]:
    conflicts: list[CrossConflict] = []

    def norm_seq(seq: list[str]) -> list[str]:
        return [normalize_process_name(s) for s in seq]

    def _value_is_flow(val) -> bool:
        """判断一个 value（str 或 list）是否是流程序列"""
        if isinstance(val, list):
            return len(val) >= 2
        return bool(_FLOW_ARROW.search(str(val)))

    for it in merged:
        if not it.has_conflict or not it.conflict_values:
            continue
        # ✅ 升级：新格式 list value 和旧格式箭头链都能识别
        if not _value_is_flow(it.value) and not any(
                _value_is_flow(c.get("value", ""))
                for c in it.conflict_values):
            continue
        seq_a = norm_seq(_split_flow(it.value))
        for cand in it.conflict_values:
            cand_val = cand.get("value", "")
            # ✅ 清理：旧写法 isinstance(list, str) 恒真，else 分支是死代码；
            #    _split_flow 自身兼容 list/str/其它（str() 兜底）。
            seq_b_raw = _split_flow(cand_val)
            if not seq_b_raw:
                continue
            seq_b = norm_seq(seq_b_raw)
            if seq_a == seq_b:
                continue
            sim = _lcs_len(seq_a, seq_b) / max(len(seq_a), len(seq_b))
            if _has_adjacent_swap(seq_a, seq_b):
                # 换位专项：工艺顺序颠倒属安全敏感矛盾，无视 sim 直接 high
                conflicts.append(CrossConflict(
                    rule_id="XV-FLOW-SEQ", severity="high",
                    conflict_type="flow_adjacent_swap",
                    side_a=_side(it), side_b=_side(it, cand.get("value")),
                    resolution_hint="工序先后关系冲突，工序顺序直接影响"
                                    "施工质量与安全，必须人工裁决"))
                continue
            if sim >= 0.85:
                sev, hint = "low", "可能是详略差异，确认是否为同一流程的粗/细两级描述"
            elif sim >= 0.6:
                sev, hint = "medium", "可能为不同深度流程（总体流程 vs 工序级流程），" \
                                      "确认是否为粒度差异而非矛盾"
            else:
                sev, hint = "high", "序列相似度过低，属真矛盾（流程名归一请一并复核）"
            conflicts.append(CrossConflict(
                rule_id="XV-FLOW-SEQ", severity=sev,
                conflict_type="flow_sequence_divergence",
                side_a=_side(it), side_b=_side(it, cand.get("value")),
                resolution_hint=f"LCS 相似度 {sim:.0%}；{hint}"))
    return conflicts


# ---------------------------------------------------------------------------
# 规则 XV-SAME-NAME：同名条目取值不一致（通用兜底，覆盖普通文本值）
# ---------------------------------------------------------------------------
def _check_same_name_conflicts(merged, initial_had_candidate: dict) -> list[CrossConflict]:
    """按事实名归组，同组存在多个不同取值 → 判冲突并标记相关条目。

    XV-MAT-DESIGN 仅处理材料等级候选、XV-MACH-SCHED 处理机械时序，普通文本值
    （如总工期/人名）或双方均不带候选的同名冲突此前无规则可判，run_cross_check
    无法按当前校验重标初始 has_conflict。本规则作通用兜底，纯程序零 LLM。

    ⚠️ 身份口径（防误伤范围兼容撤销）：只标记「进入校验时**无候选**」的条目。
    携带候选的条目的冲突判定归 XV-MAT-DESIGN（其范围值兼容会就地撤销标记、
    清空 conflict_values）；若此处再按当时状态无候选就重标，会把已正确撤销
    的「不低于C30 ↔ C35」重新打回冲突。故判据用进入时快照
    initial_had_candidate，而非规则执行中的实时状态。
    """
    conflicts: list[CrossConflict] = []
    # ✅ 2026-10-06 改按 **fact_key** 分组（名称仅作兜底）。
    #
    # 旧实现 `by_name[(it.name or "").strip()]` 只按**显示名精确相等**分组，
    # 而 ``it.key`` 才是本仓事实的稳定身份标识（extract 的 normalize_key 产物、
    # persist 的 fact_key 列、merge_and_deduplicate 的聚类键、前端展示的 key）。
    # 后果：**同一个物理量只要中文名不同就永不被比对** ——
    #   「总工期 90日历天」vs「施工总工期 90日历天」→ 不报
    #   「基坑深度 12.5m」vs「基坑开挖深度 12.5m」→ 不报
    # 而这恰恰是跨来源抽取最常见的形态（不同文件对同一参数用了不同措辞）。
    #
    # 口径：key 非空 → 只按 key 分组（**不再**用 name 二次分裂，
    # 否则「总工期」与「施工总工期」若恰好共用一个 key 仍会被拆开）；
    # key 缺失（历史 shim / 手工替身）→ 回落按 name 分组，行为与旧版一致。
    by_key: dict[str, list] = {}
    by_name: dict[str, list] = {}
    for it in merged:
        key = str(getattr(it, "key", "") or "").strip()
        if key:
            by_key.setdefault(key, []).append(it)
            continue
        nm = (it.name or "").strip()
        if nm:
            by_name.setdefault(nm, []).append(it)
    # key 组在前、name 组在后：两者互不相交（key 非空的不进 name 组），
    # 合并成同一轮判定即可，无需两套循环。
    groups = list(by_key.values()) + list(by_name.values())

    for items in groups:
        valued = [(it, _norm_text_value(it.value)) for it in items]
        valued = [(it, v) for it, v in valued if v]
        distinct = []
        for _, v in valued:
            if v not in distinct:
                distinct.append(v)
        if len(distinct) < 2:
            continue
        # 仅给初始无候选的同名条目打标：它们不经 XV-MAT-DESIGN 的候选分支，
        # 靠本规则按组内取值分歧兜底。携带候选的条目交给 material 规则裁决，
        # 这里绝不触碰，防止误标已撤销的范围兼容条目。
        reps = []
        for d in distinct:
            rep = next(it for it, v in valued if v == d)
            reps.append((rep, d))
        for it, _ in valued:
            if not initial_had_candidate.get(id(it)):
                it.has_conflict = True
        for i in range(len(reps)):
            for j in range(i + 1, len(reps)):
                a, _ = reps[i]
                b, _ = reps[j]
                conflicts.append(CrossConflict(
                    rule_id="XV-SAME-NAME", severity="medium",
                    conflict_type="same_name_value_mismatch",
                    side_a=_side(a), side_b=_side(b),
                    resolution_hint="同名事实存在多个不同取值，需人工核对权威来源"))
    return conflicts


def _norm_text_value(v) -> str:
    if isinstance(v, (list, tuple)):
        return "|".join(str(x).strip() for x in v if str(x).strip())
    return normalize_material_spec(str(v or ""))


# ---------------------------------------------------------------------------
# 规则 XV-NUM-UNIT：数值/单位一致性（同名称事实的量纲一致性）
# ---------------------------------------------------------------------------
# ✅ 增强（2026-10-03）：XV-SAME-NAME 只按「归一化文本值不同」判同名冲突，
# 对「同一物理量但单位写法不同」的情形缺乏专门口径：
#   · "开挖深度 12.5m" vs "开挖深度 1250cm" —— 数值等价、仅单位不同，
#     XV-SAME-NAME 已按 generic mismatch 报 medium，但无「统一单位」的可执行提示；
#   · "开挖深度 12.5m" vs "开挖深度 12.5cm" —— 换算到基准后量级差恰为 100 倍
#     （cm↔m 换算比），实为「把 cm 当 m 写」的笔误，XV-SAME-NAME 只能报
#     泛化矛盾，无法点出根因。
#     ⚠️ 口径边界：**只比不同单位**的同量纲取值（同单位直接 continue，交给
#     XV-SAME-NAME 判「值不同」）。这是刻意的 —— 同单位的 100 倍差更像
#     「参数写错/取自不同工况」，按「单位换算错误」提示会把用户引向错误方向。
# 本规则在 XV-SAME-NAME 之上补「量纲一致性」视角，产出低/中危的可执行提示，
# 全部人工裁决（auto_resolvable=False），不改变既有冲突的判定结果。
#: ✅ 2026-10-06 单位模型从「长度+质量」两维扩到 9 维。
#:
#: 旧表只有 20 个长度/质量单位，其余工程量纲一律 ``_extract_quantity → None``
#: **静默跳过** —— 实测「施工总荷载 12.5kN」与「125kN」这类最典型的量级笔误
#: 完全不报（只剩泛化的 XV-SAME-NAME medium，用户看不出根因是单位）。
#: 本表以 ``单位 → (量纲, 基准换算系数)`` 单一结构同时驱动三件事：
#:   ① ``_UNIT_TO_BASE`` 基准换算（值等价判定）
#:   ② ``_UNIT_DIM`` 量纲分组（只与同量纲比对，不跨量纲乱比）
#:   ③ ``LENGTH_UNITS`` 长度单位集（供 facts_classification._UNIT_TO_METER 做 parity）
#: 拆成三个派生视图而不是三份手写清单 —— 否则又是一组「同判据多副本」。
#:
#: ⚠️ **仿射量纲**（temperature）有零点偏移，比值换算无意义（30℃ 与 86℉
#: 比值 0.35，不构成任何换算关系），故列入 :data:`_AFFINE_DIMS` 并在
#: 比对时**只判「同数值不同单位」**（30℃/30℉ 必是单位写错），不参与
#: 等价与量级比判定。
_UNIT_SPEC: dict[str, tuple[str, float]] = {
    # ---- 长度（基准 m）----
    "m": ("length", 1.0), "米": ("length", 1.0),
    "dm": ("length", 0.1), "分米": ("length", 0.1),
    "cm": ("length", 0.01), "厘米": ("length", 0.01),
    "mm": ("length", 0.001), "毫米": ("length", 0.001),
    "km": ("length", 1000.0), "千米": ("length", 1000.0),
    # ---- 质量（基准 kg）----
    "kg": ("mass", 1.0), "千克": ("mass", 1.0), "公斤": ("mass", 1.0),
    "g": ("mass", 0.001), "克": ("mass", 0.001),
    "mg": ("mass", 1e-6), "毫克": ("mass", 1e-6),
    "t": ("mass", 1000.0), "吨": ("mass", 1000.0),
    # ---- 力（基准 N）----
    "n": ("force", 1.0), "牛": ("force", 1.0),
    "kn": ("force", 1000.0), "千牛": ("force", 1000.0),
    "mn": ("force", 1e-3),
    # ---- 线荷载（基准 N/m）----
    "n/m": ("line_load", 1.0), "kn/m": ("line_load", 1000.0),
    # ---- 面荷载 / 压强（基准 Pa）----
    "pa": ("pressure", 1.0), "帕": ("pressure", 1.0),
    "kpa": ("pressure", 1000.0), "千帕": ("pressure", 1000.0),
    "mpa": ("pressure", 1e6), "兆帕": ("pressure", 1e6),
    "n/m2": ("pressure", 1.0), "kn/m2": ("pressure", 1000.0),
    # ---- 面积（基准 m²）----
    "m2": ("area", 1.0), "平方米": ("area", 1.0),
    "cm2": ("area", 1e-4), "平方厘米": ("area", 1e-4),
    "mm2": ("area", 1e-6), "平方毫米": ("area", 1e-6),
    # ---- 体积（基准 m³）----
    "m3": ("volume", 1.0), "立方米": ("volume", 1.0),
    "cm3": ("volume", 1e-6), "立方厘米": ("volume", 1e-6),
    "l": ("volume", 1e-3), "升": ("volume", 1e-3),
    # ---- 时间（基准 天）----
    "d": ("duration", 1.0), "天": ("duration", 1.0),
    "日历天": ("duration", 1.0),
    "月": ("duration", 30.0), "个月": ("duration", 30.0),
    # ---- 温度（仿射，仅判「同数值不同单位」）----
    "℃": ("temperature", 1.0), "°c": ("temperature", 1.0),
    "℉": ("temperature", 1.0), "°f": ("temperature", 1.0),
    # ---- 百分比 ----
    "%": ("ratio", 1.0), "％": ("ratio", 1.0),
}
_UNIT_TO_BASE = {u: b for u, (_d, b) in _UNIT_SPEC.items()}
_UNIT_DIM = {u: d for u, (d, _b) in _UNIT_SPEC.items()}
#: 长度单位集（供 facts_classification._UNIT_TO_METER 做跨模块 parity）
LENGTH_UNITS: tuple[str, ...] = tuple(
    u for u, (d, _b) in _UNIT_SPEC.items() if d == "length")
#: 仿射量纲：比值换算无意义，不参与「等价 / 量级比」判定
_AFFINE_DIMS: frozenset[str] = frozenset({"temperature"})

# 疑似单位换算错误的量级倍数（cm↔m=100、mm↔m=1000、kN↔N=1000 及其倒数）
_SUSPECT_FACTORS = (100.0, 1000.0, 0.01, 0.001)

#: ✅ 2026-10-06 单位字符类补 `/`、`²`、`³`、`㎡`、**数字**。
#: 旧类 `[A-Za-z\u4e00-\u9fff°%]+` 三处缺失，各自带一条真实漏检：
#:   ① 不含 `/`   → ``12.5kN/m²`` 只取到 ``kN``，线/面荷载永远解析不全；
#:   ② 不含 `²`   → ``120m²`` 只取到 ``m``，被误判成长度；
#:   ③ **不含数字** → ``2.0m3`` 只取到 ``m``，体积被误判成长度（最隐蔽，
#:      因为 ``m`` 恰好是合法单位，解析「成功」但量纲全错）。
_QUANTITY_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*"
    r"([A-Za-z\u4e00-\u9fff°%℃℉㎡/²³0-9]+)")

#: 单位写法归一（全角/上标/空白 → 表内键）
_UNIT_ALIASES: dict[str, str] = {
    "㎡": "m2", "m²": "m2", "M2": "m2", "M²": "m2",
    "㎥": "m3", "m³": "m3", "M3": "m3", "M³": "m3",
    "KN": "kn", "Kn": "kn", "kN": "kn",
    "KN/M2": "kn/m2", "KN/M²": "kn/m2", "kN/m²": "kn/m2",
    "％": "%",
}


def _normalize_unit(raw: str) -> str:
    """单位写法归一（去空白 → 别名 → 上标转数字 → 小写）。

    ⚠️ 上标必须在查表**前**转数字：表内键一律用 ASCII ``m2``/``m3``/
    ``kn/m2``，而中文工程文本常写 ``m²``/``kN/m²``。
    """
    u = str(raw or "").strip().replace(" ", "").replace("　", "")
    hit = _UNIT_ALIASES.get(u)
    if hit is not None:
        return hit
    # 上标 → ASCII 数字（未登记别名的写法也走这一步，如 ``cm²``）
    u = u.replace("²", "2").replace("³", "3")
    return u.lower()


def _extract_quantity(value) -> tuple[float, str, str] | None:
    """从事实值中提取 (数值, 单位, 量纲)；无已知单位返回 None。

    仅识别带【已知单位】的数值，避免把纯数字（人数、楼层数）误判为量纲冲突。
    值含多个数值时取首个匹配的已知单位。
    ✅ 2026-10-06：单位先经 :func:`_normalize_unit` 归一（``kN/m²``→``kn/m2``、
    ``㎡``→``m2``、``KN``→``kn``），且**跳过更长（复合）单位优先** ——
    否则 ``12.5kN/m²`` 会先匹配到 ``kN``，把面荷载误判成力。
    """
    if value is None or isinstance(value, (list, tuple)):
        return None
    text = str(value)
    best: tuple[float, str, str] | None = None
    for m in _QUANTITY_RE.finditer(text):
        unit = _normalize_unit(m.group(2))
        if unit not in _UNIT_TO_BASE:
            continue
        dim = _UNIT_DIM[unit]
        try:
            num = float(m.group(1))
        except ValueError:
            continue
        # 复合单位（面荷载/线荷载）优先于其前缀量纲（力）
        if best is None or len(unit) > len(best[1]):
            best = (num, unit, dim)
        if best is not None and "/" in unit:
            break
    return best


def _add_num_candidate(it, other_val: str, other_source: str,
                       other_conf: float) -> None:
    """把对侧数值登记为矛盾候选（去重），并保持 has_conflict 标记。"""
    it.has_conflict = True
    for c in it.conflict_values:
        if str(c.get("value", "")) == other_val:
            return
    it.conflict_values.append({
        "value": other_val,
        "source": other_source,
        "confidence": _safe_confidence(other_conf, 1.0),
    })


def _check_numeric_unit_consistency(merged) -> list[CrossConflict]:
    """同名事实的量纲一致性：单位不一致 / 疑似单位换算错误。

    ✅ 2026-10-06 覆盖 9 个量纲（长度/质量/力/线荷载/面荷载/面积/体积/
    时间/温度/百分比），此前只有长度+质量，``kN``、``kN/m²``、``㎡``、
    ``天``、``℃`` 一律静默跳过 —— 最典型的量级笔误（12.5kN vs 125kN）不报。

    与 XV-SAME-NAME 正交互补：XV-SAME-NAME 报「值不同」，本规则补「量纲根因」。

    ⚠️ 仿射量纲（温度）不参与比值换算（见 :data:`_AFFINE_DIMS`）：
    只在「数值相同、单位不同」时报错（30℃ 与 30℉ 必有一处写错），
    绝不按 base 比值判「等价」或「量级差」。
    """
    conflicts: list[CrossConflict] = []
    by_name: dict[str, list] = {}
    for it in merged:
        if isinstance(it.value, (list, tuple)):
            continue
        q = _extract_quantity(it.value)
        if not q:
            continue
        by_name.setdefault((it.name or "").strip(), []).append((it, q))

    for nm, items in by_name.items():
        if not nm:
            continue
        by_dim: dict[str, list] = {}
        for it, q in items:
            by_dim.setdefault(q[2], []).append((it, q))
        for _dim, pairlist in by_dim.items():
            if len(pairlist) < 2:
                continue
            affine = _dim in _AFFINE_DIMS
            for i in range(len(pairlist)):
                for j in range(i + 1, len(pairlist)):
                    it_a, qa = pairlist[i]
                    it_b, qb = pairlist[j]
                    if qa[1] == qb[1]:
                        continue  # 同单位 → 交给 XV-SAME-NAME 判值不同
                    if affine:
                        # 只判「同数值不同单位」；不同数值无法据单位判断对错
                        if abs(qa[0] - qb[0]) <= 1e-9:
                            conflicts.append(CrossConflict(
                                rule_id="XV-NUM-UNIT", severity="medium",
                                conflict_type="numeric_unit_inconsistent",
                                side_a=_side(it_a), side_b=_side(it_b),
                                resolution_hint="两值数值相同但温度单位不同"
                                                "（如 30℃ 与 30℉），"
                                                "至少有一处写错，需人工核对"))
                            _add_num_candidate(it_a, str(it_b.value),
                                               it_b.source, it_b.confidence)
                            _add_num_candidate(it_b, str(it_a.value),
                                               it_a.source, it_a.confidence)
                        continue
                    base_a = qa[0] * _UNIT_TO_BASE[qa[1]]
                    base_b = qb[0] * _UNIT_TO_BASE[qb[1]]
                    if base_a == 0 or base_b == 0:
                        continue
                    rel = abs(base_a - base_b) / max(abs(base_a), abs(base_b))
                    if rel <= 1e-6:
                        # 数值等价、仅单位不同
                        conflicts.append(CrossConflict(
                            rule_id="XV-NUM-UNIT", severity="low",
                            conflict_type="numeric_unit_inconsistent",
                            side_a=_side(it_a), side_b=_side(it_b),
                            resolution_hint="两值数值等价但单位写法不一致"
                                            "（如 12.5m 与 1250cm），建议统一单位"))
                        _add_num_candidate(it_a, str(it_b.value),
                                           it_b.source, it_b.confidence)
                        _add_num_candidate(it_b, str(it_a.value),
                                           it_a.source, it_a.confidence)
                    else:
                        ratio = base_a / base_b
                        # ⚠️ 相对容差，**不得**用减法容差。
                        # 旧实现 ``abs(1/ratio - f) <= 1e-3`` 是加法容差：
                        # ``1/ratio = 1e-9`` 时 ``|1e-9 - 0.001| = 0.000999 ≤ 1e-3``
                        # 也会命中 —— 即任意 ratio ≥ 1e6 都会被误判成
                        # 「恰好 1000 倍的单位换算错误」。本轮把单位表扩到
                        # mN/MPa/㎥ 后该误报从「理论可能」变成「随手可触发」。
                        # 改为相对判据 ``|x/f - 1| <= 1e-3``。
                        if (any(abs(ratio / f - 1.0) <= 1e-3
                                for f in _SUSPECT_FACTORS)
                                or any(abs((1.0 / ratio) / f - 1.0) <= 1e-3
                                       for f in _SUSPECT_FACTORS)):
                            conflicts.append(CrossConflict(
                                rule_id="XV-NUM-UNIT", severity="medium",
                                conflict_type="numeric_scale_suspect",
                                side_a=_side(it_a), side_b=_side(it_b),
                                resolution_hint="两值量级差恰为常见单位换算比"
                                                "（100/1000），疑似单位换算错误"))
                            _add_num_candidate(it_a, str(it_b.value),
                                               it_b.source, it_b.confidence)
                            _add_num_candidate(it_b, str(it_a.value),
                                               it_a.source, it_a.confidence)
    return conflicts


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def _is_adjudicable(item) -> bool:
    """该条目是否值得参与「冲突判定 + has_conflict 回写」。

    ✅ 2026-10-06 新增。判据与事实注入门控
    （facts_extractor.get_facts_inject_where，四条件 fail-closed）**同源**：
    ``is_simulated=1``（AI 编造值）或 ``is_stale=1``（来源资料已变化）的事实在
    注入门控里恒被排除 —— 也就是说**无论裁决结果如何，它都不会进入
    目录/正文/导出**。让这类条目参与冲突判定会产生两类纯负收益：

      ① 假冲突：AI 编造值与真实值「不一致」被判冲突，逼用户裁决噪声；
      ② 假闸门：has_conflict=1 会把该行锁在「未确认」态（前端 Tag、批量确认
      跳过、safety 门控），而它本来就不可能被注入 —— 用户做再多次裁决
      也无法让它进入交付物，却看不出为什么。

    同时也避免 `_mark_cross_conflicts_on_items` 按 name 回写时把
    ``has_conflict`` 打到真实条目上（它按 name 匹配，模拟值同名即命中）。

    缺属性（历史 shim / 测试替身）按**可裁决**处理（fail-open 到旧行为），
    保证既有调用方零行为变化。
    """
    return not (bool(getattr(item, "is_simulated", False))
                or bool(getattr(item, "is_stale", False)))


def run_cross_validations(merged) -> list[dict]:
    """对合并去重后的事实池执行交叉校验，返回统一冲突记录列表。

    副作用：range 值兼容的合并项撤销 has_conflict 标记。

    ✅ 2026-10-06：进入校验前先按 ``_is_adjudicable`` 过滤 —— 模拟值
    （is_simulated=1）与过期事实（is_stale=1）不参与冲突判定，也不接收
    has_conflict 回写。口径与注入门控同源（理由见 _is_adjudicable docstring）。
    """
    eligible = [it for it in merged if _is_adjudicable(it)]
    skipped = len(merged) - len(eligible)
    if skipped:
        logger.info(
            "交叉校验跳过 %d/%d 条非可裁决事实（is_simulated / is_stale，"
            "与注入门控同口径：这类事实本就不会进入生成链路）",
            skipped, len(merged))
    if not eligible:
        # 全池不可裁决 → 无可产出冲突，也不得回写任何 has_conflict
        return []

    conflicts: list[CrossConflict] = []

    # 进入校验前快照每个条目是否携带候选。XV-SAME-NAME 据此识别「初始无候选」
    # 条目，必须在 material 规则可能清空 conflict_values（范围兼容撤销）之前记录。
    initial_had_candidate = {
        id(it): bool(it.conflict_values) for it in eligible}

    mat_conflicts, range_merged = _check_material_vs_design(eligible)
    conflicts.extend(mat_conflicts)
    # ✅ 加固：按条目身份撤销（旧实现按 name 全文匹配，同名不同 key
    #    的无关条目会被误撤销矛盾标记）。
    for merged_it in range_merged:
        merged_it.has_conflict = False
        merged_it.conflict_values = []
        logger.info("范围值兼容合并：%s（撤销冲突标记）", merged_it.name)

    conflicts.extend(_check_machinery_vs_schedule(eligible))
    conflicts.extend(_check_flow_sequence(eligible))
    conflicts.extend(_check_same_name_conflicts(eligible, initial_had_candidate))
    conflicts.extend(_check_numeric_unit_consistency(eligible))

    # 跨条目冲突（机械↔工期）回写两侧事实的冲突标记，供前端并排展示裁决
    _mark_cross_conflicts_on_items(eligible, conflicts)

    logger.info("交叉校验完成：%d 处（high=%d）", len(conflicts),
                sum(1 for c in conflicts if c.severity == "high"))
    return [c.to_dict() for c in conflicts]


def _mark_cross_conflicts_on_items(merged, conflicts: list[CrossConflict]) -> None:
    """把跨条目冲突回写到涉及事实（MAT/FLOW 的源事实在合并阶段已带标记）"""
    by_name: dict = {}
    for it in merged:
        by_name.setdefault(it.name, []).append(it)

    def _mark(side_self: dict, side_other: dict) -> None:
        for it in by_name.get(side_self.get("name", ""), []):
            if it.value != side_self.get("value"):
                continue
            it.has_conflict = True
            if not any(c.get("value") == side_other.get("value")
                       for c in it.conflict_values):
                it.conflict_values.append({
                    "value": side_other.get("value"),
                    "source": side_other.get("source", ""),
                    "confidence": side_other.get("confidence", 1.0),
                })

    for c in conflicts:
        if c.rule_id == "XV-MACH-SCHED":
            _mark(c.side_a, c.side_b)
            _mark(c.side_b, c.side_a)
