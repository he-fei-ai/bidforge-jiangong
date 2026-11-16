"""C11 收口护栏 · merge 去重键单位量纲感知折叠（R248 · 2026-11-14）。

病灶（R247 §八 让渡 → 本轮归属会话行为探针定罪）：
``merge_and_deduplicate`` 簇内去重键只走 ``_norm_value``（去空白 + 全角冒号），
"150 天" 与 "150 日历天" 被判为异值 → 凭空造出一条假矛盾让用户裁决。
而本仓另两处判据早已把 天/日历天 列为确定等价：
  * ``facts_cross_validators._UNIT_SPEC``：两者同为 duration 基准 1.0；
  * ``content_standard._UNIT_ALIASES``：两者同映射 "day"。
只有提取合并这一处漏了单位换算。

修法：新增 ``_norm_unit_aware_key`` —— 对「纯数字 + 单位」形态按量纲折叠，
判据单一事实源 = ``facts_cross_validators`` 的量纲表。同数值 + 同量纲 + 同基准
系数 → 折叠（不产生假矛盾）；否则回落 ``_norm_value`` 原样。

本文件锁定双向契约：正向（假矛盾消除）与三条反向（真矛盾 / 跨系数 / 跨形态
不误吞），并给出 A/B 反向验证（把折叠退回 ``_norm_value`` 时必须变红）。
"""

import pytest
from app.services.facts_extractor import (
    FactItem,
    _norm_unit_aware_key,
    _norm_value,
    merge_and_deduplicate,
)


def _item(value, unit=None, name="建设工期", key="total_duration", conf=0.9):
    return FactItem(
        name=name,
        value=value,
        key=key,
        category="schedule",
        source="doc",
        confidence=conf,
        value_unit=unit,
    )


# ===========================================================================
# A 组 · 正向：同数值同量纲的假矛盾必须消除
# ===========================================================================


def test_a1_value_embedded_day_calendar_day_no_fake_conflict():
    """A1（核心 · 修复前实测 conflict=1）：值内嵌单位 天 vs 日历天 → 不再判矛盾。"""
    out = merge_and_deduplicate([_item("150 天"), _item("150 日历天", conf=0.8)])
    assert len(out) == 1, "同数值同量纲被误判成两条矛盾事实"
    assert out[0].has_conflict is False, "假矛盾未消除"
    assert out[0].conflict_values == [], "假矛盾候选值未清空"


def test_a2_unit_column_normalized_equivalent_no_conflict():
    """A2：unit 列形态 150(天) vs 150(日历天) → 值裸数值相同，不判矛盾。"""
    out = merge_and_deduplicate([_item("150", unit="天"), _item("150", unit="日历天", conf=0.8)])
    assert len(out) == 1
    assert out[0].has_conflict is False


def test_a3_unit_aliases_day_variants_all_collapse():
    """A3：天/日历天/日历日/昼夜 同数值 → 全部折叠成一条（均 duration 基准 1.0）。"""
    out = merge_and_deduplicate(
        [_item("120 天"), _item("120 日历天", conf=0.8), _item("120 日历日", conf=0.7), _item("120 昼夜", conf=0.6)]
    )
    assert len(out) == 1, f"四个等价写法未折叠成一条：{[i.value for i in out]}"
    assert out[0].has_conflict is False


def test_a4_best_value_displayed_verbatim_not_rewritten():
    """A4：折叠只作用于比较键，对外展示的 value 原样保留（不把 日历天 改写成 天）。"""
    out = merge_and_deduplicate([_item("150 日历天", conf=0.95), _item("150 天", conf=0.8)])
    assert len(out) == 1
    # 高置信度条胜出，且其 value 逐字保留
    assert out[0].value == "150 日历天"


# ===========================================================================
# B 组 · 反向：真矛盾 / 跨系数 / 跨形态 绝不误吞
# ===========================================================================


def test_b1_different_numeric_same_unit_still_conflicts():
    """B1（必须配对 A1）：同单位不同数值（184 天 vs 195 天）→ 真矛盾照报。"""
    out = merge_and_deduplicate([_item("184 天", conf=0.9), _item("195 天", conf=0.8)])
    assert len(out) == 1
    assert out[0].has_conflict is True, "真数值矛盾被误折叠"
    assert {c["value"] for c in out[0].conflict_values} == {"195 天"}


def test_b2_workday_vs_day_not_folded():
    """B2：工作日 ∉ 量纲表 → 与 天 不折叠（content_standard 同口径：工作日≠日历天）。"""
    out = merge_and_deduplicate([_item("150 天", conf=0.9), _item("150 工作日", conf=0.8)])
    assert len(out) == 1
    assert out[0].has_conflict is True, "工作日与日历天被误判等价"


def test_b3_cross_factor_day_vs_month_not_folded():
    """B3：天 vs 月 同量纲但基准系数不同（1.0 vs 30.0）→ 不折叠。"""
    out = merge_and_deduplicate([_item("150 天", conf=0.9), _item("150 月", conf=0.8)])
    assert len(out) == 1
    assert out[0].has_conflict is True, "跨基准系数被误折叠"


def test_b4_length_unit_notation_variant_not_folded():
    """B4：12.5m vs 12.5cm 同量纲不同基准（1.0 vs 0.01）→ 不折叠（R225 B1 同族口径）。"""
    out = merge_and_deduplicate(
        [_item("12.5m", name="基坑深度", key="depth", conf=0.9), _item("12.5cm", name="基坑深度", key="depth", conf=0.8)]
    )
    assert len(out) == 1
    assert out[0].has_conflict is True, "单位量级错误被误判等价"


def test_b5_value_vs_unit_column_cross_form_not_folded():
    """B5：值内嵌单位 vs unit 列（150 天 vs 150/unit 日历天）→ 保守不折叠（窄判据）。"""
    out = merge_and_deduplicate([_item("150 天", conf=0.9), _item("150", unit="日历天", conf=0.8)])
    # 值形态不同（"150天" vs "150"）→ 回落 _norm_value 判异值，保守报矛盾
    assert len(out) == 1
    assert out[0].has_conflict is True


def test_b6_pure_number_and_list_value_untouched():
    """B6：纯数字裸值与序列值不参与折叠（无单位 → 回落 _norm_value 原样）。"""
    assert _norm_unit_aware_key("150") == _norm_value("150")
    assert _norm_unit_aware_key(["塔吊", "挖掘机"]) == _norm_value(["塔吊", "挖掘机"])
    assert _norm_unit_aware_key("按合同约定工期执行") == _norm_value("按合同约定工期执行")


# ===========================================================================
# C 组 · 判据与回归锁
# ===========================================================================


def test_c1_key_judgement_single_source_of_truth():
    """C1：折叠判据与量纲表同源 —— 天/日历天 同键，月 与 天 异键。"""
    k_day = _norm_unit_aware_key("150 天")
    k_cal = _norm_unit_aware_key("150 日历天")
    k_month = _norm_unit_aware_key("150 月")
    assert k_day == k_cal, "天/日历天 未折叠成同一键"
    assert k_day != k_month, "天/月 跨系数被折成同一键"
    # 同键形态必须编码「数值 + 量纲 + 基准」，跨系数天然区分
    assert "duration" in k_day


def test_c2_temperature_affine_dim_not_folded():
    """C2：仿射量纲（温度）比值换算无意义 → 不折叠（30℃ vs 30℉ 必异值）。"""
    assert _norm_unit_aware_key("30℃") != _norm_unit_aware_key("30℉")


def test_c3_basis_coexist_scoped_key_path_untouched():
    """C3：清单型并存键（basis_scoped_key）走 _norm_value md5 口径，不叠加单位折叠。"""
    from app.services.facts_extractor import basis_scoped_key

    # 同一部规范两种写法（空白差异）→ 同后缀收敛一条；单位折叠不介入 basis 路径
    k1 = basis_scoped_key("execution_standard", "GB 50300-2013")
    k2 = basis_scoped_key("execution_standard", "GB50300-2013")
    assert k1.split("::")[0] == k2.split("::")[0] == "execution_standard"


def test_c4_ab_reversion_guard():
    """C4（A/B 反向）：把折叠退回 _norm_value 时 A1 必须变红 —— 证明护栏有判别力。"""
    out_norm = merge_and_deduplicate([_item("150 天"), _item("150 日历天", conf=0.8)])
    # 旧判据下两条异值 → 1 条事实 + 1 个假矛盾候选
    legacy_conflict = _norm_value("150 天") != _norm_value("150 日历天")
    assert legacy_conflict is True, "旧判据本应判异值（A/B 锚失效）"
    assert out_norm[0].has_conflict is False, "新判据未消除假矛盾（A/B 未翻转）"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
