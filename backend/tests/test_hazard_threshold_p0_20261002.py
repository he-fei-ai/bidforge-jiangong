"""第二十三轮（2026-10-02）：危大阈值判定 —— 阈值复用解析 P0 + 人工挖孔桩超规模口径。

两处修复，均引部文原文（建办质〔2018〕31号 附件一/附件二，住建部官网
2018-05-17 印发件），不凭记忆编造阈值：

① 【P0 阈值复用从未生效】子类表声明了阈值复用（``sc_cuplock``/``sc_disc``
   → ``sc_ground``，``ho_tower_crane``/``ho_construction_hoist`` → ``ho_crane``，
   ``fw_disc`` → ``fw_support``），但 ``classify_scheme`` 一直直接把 ``sub_id``
   当阈值键传：``HAZARD_THRESHOLDS`` 里没有这些复用子类的键 →
   ``evaluate_hazard_level`` 落进「非参数型」分支 → **无条件判超规模**。
   实测：30m 碗扣式脚手架（部文 50m 才超规模）被要求组织专家论证；
   50kN 塔机（部文 300kN）同样被误判。数据声明了复用却没人在判定侧读它，
   属 AGENTS.md 反复记录的「判据分叉」。

② 【L-2 人工挖孔桩超规模过判】原 ``threshold=None`` → 落「非参数型」分支 →
   凡人工挖孔桩一律判超规模。部文口径是：
     · 附件一 七(三) 人工挖孔桩工程 → 危大（**无深度门槛**）；
     · 附件二 七(三) **开挖深度 16m 及以上** → 超过一定规模（闭区间）。
   10m 挖孔桩本不需要专家论证。

判据注意（AGENTS.md §5.14）：本文件断言的是**行为**（判定结果），
不是源码文本 —— 阈值数字改对改错都应被这套行为断言抓住。
"""
import ast
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CLS_PATH = os.path.join(BACKEND, "app", "services", "scheme_classification.py")

from app.services.scheme_classification import (  # noqa: E402
    HAZARD_CATEGORIES, HAZARD_THRESHOLDS, classify_scheme, evaluate_hazard_level,
    resolve_threshold_key,
)


# ---------------------------------------------------------------------------
# 1. 阈值复用解析出口
# ---------------------------------------------------------------------------

#: 子类 id → 其声明的阈值复用键（部文口径相同的有意复用）
EXPECTED_REUSE = {
    "sc_cuplock": "sc_ground",
    "sc_disc": "sc_ground",
    "ho_tower_crane": "ho_crane",
    "ho_construction_hoist": "ho_crane",
    "fw_disc": "fw_support",
}


@pytest.mark.parametrize("sub_id,expect", sorted(EXPECTED_REUSE.items()))
def test_resolve_threshold_key_resolves_reuse(sub_id, expect):
    assert resolve_threshold_key(sub_id) == expect


def test_resolve_threshold_key_identity_and_unknown():
    """自身有阈值的子类原样返回；未知 id 也原样返回（不抛异常）。"""
    assert resolve_threshold_key("sc_ground") == "sc_ground"
    assert resolve_threshold_key("ot_bored_pile") == "ot_bored_pile"
    assert resolve_threshold_key("不存在") == "不存在"
    assert resolve_threshold_key(None) is None
    assert resolve_threshold_key("") is None


def test_every_declared_threshold_resolves_to_known_rule():
    """子类表声明的每个阈值键都必须在 HAZARD_THRESHOLDS 里真实存在。

    否则「复用」是悬空声明：判定侧会落回「非参数型」兜底，
    而数据表看起来一切正常 —— 与本轮 P0 同根。
    """
    for cat in HAZARD_CATEGORIES:
        for s in cat.get("subs", ()):
            key = resolve_threshold_key(s["id"])
            if key is None:
                continue
            if key not in HAZARD_THRESHOLDS:
                # 允许显式声明为「无阈值」（None 表示非参数型危大）
                if s.get("threshold") is None:
                    continue
                pytest.fail(
                    f"子类 {s['id']} 声明阈值键 {key}，"
                    f"但 HAZARD_THRESHOLDS 无此键（悬空复用声明）")


def test_classify_scheme_does_not_pass_raw_subid():
    """静态护栏：判定必须过 resolve_threshold_key，不得直接传 sub_id。

    这是本轮 P0 的根因（数据声明了复用、判定侧没读）。
    """
    with io.open(CLS_PATH, encoding="utf-8") as f:
        src = f.read()
    fn = [n for n in ast.parse(src).body
          if isinstance(n, ast.FunctionDef) and n.name == "classify_scheme"][0]
    seg = ast.get_source_segment(src, fn) or ""
    assert "evaluate_hazard_level(" in seg
    call = seg[seg.find("evaluate_hazard_level("):
               seg.find("evaluate_hazard_level(") + 90]
    assert "resolve_threshold_key(" in call, (
        f"classify_scheme 仍直接传 sub_id 做阈值判定（P0 回退）：{call.strip()}")


# ---------------------------------------------------------------------------
# 2. 复用子类的判定行为（部文口径）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "碗扣式钢管脚手架专项施工方案",
    "盘扣式钢管脚手架专项施工方案",
    "盘扣式脚手架专项施工方案",
    "落地式钢管脚手架专项施工方案",
])
def test_scaffold_30m_is_hazardous_not_oversize(name):
    """30m（<50m）应为危大但**非**超规模。旧实现误判超规模。"""
    c = classify_scheme(name, params={"height": 30})
    assert c.is_hazardous is True, "30m 脚手架应属危大（24m 及以上）"
    assert c.is_oversize is False, (
        f"30m 脚手架被误判为超过一定规模（部文口径 50m）：{c.hazards}")


@pytest.mark.parametrize("name", [
    "碗扣式钢管脚手架专项施工方案",
    "落地式钢管脚手架专项施工方案",
])
def test_scaffold_50m_is_oversize(name):
    """50m 闭区间临界 → 超规模（部文「50m 及以上」）。"""
    c = classify_scheme(name, params={"height": 50})
    assert c.is_oversize is True


def test_scaffold_24m_boundary_still_hazardous():
    """24m 闭区间不得回退（本文件 2026-09-30 修复的 P0）。"""
    c = classify_scheme("落地式钢管脚手架专项施工方案", params={"height": 24})
    assert c.is_hazardous is True
    assert c.is_oversize is False


@pytest.mark.parametrize("cap,expect", [(50, False), (299, False),
                                        (300, True), (350, True)])
def test_tower_crane_capacity_threshold(cap, expect):
    """塔机 300kN 闭区间；旧实现下 50kN 小塔机也被误判超规模。"""
    c = classify_scheme("塔式起重机安装拆卸专项施工方案",
                        params={"crane_capacity": cap})
    assert c.is_hazardous is True, "起重机械安装拆卸本身即危大（部文附件一）"
    assert c.is_oversize is expect, (
        f"{cap}kN 塔机超规模判定错误（部文口径 300kN）")


def test_construction_hoist_uses_ho_crane_threshold():
    """施工升降机复用 ho_crane 口径：低起重量不应超规模。"""
    c = classify_scheme("施工升降机安装拆卸专项施工方案",
                        params={"crane_capacity": 80})
    assert c.is_oversize is False, "施工升降机未复用 ho_crane 阈值"


def test_disc_formwork_uses_fw_support_threshold():
    """盘扣式模板支撑复用 fw_support：4.5m 未达 8m → 非超规模。"""
    c = classify_scheme("盘扣式模板支撑专项施工方案", params={"height": 4.5})
    assert c.is_oversize is False, "盘扣式模板支撑未复用 fw_support 阈值"
    c = classify_scheme("盘扣式模板支撑专项施工方案", params={"height": 9})
    assert c.is_oversize is True


# ---------------------------------------------------------------------------
# 3. 人工挖孔桩（L-2）
# ---------------------------------------------------------------------------

def test_bored_pile_rule_shape():
    rule = HAZARD_THRESHOLDS.get("ot_bored_pile")
    assert rule is not None, "人工挖孔桩缺少阈值规则（L-2 未落地）"
    # 附件一：本身即危大，无深度门槛
    assert rule.get("hazard_always") is True
    assert rule.get("hazard_when") == []
    # 附件二：开挖深度 16m 及以上 → 超规模（闭区间）
    assert ("pile_depth", ">=", 16) in rule["oversize_when"], \
        "超规模阈值必须是 16m（部文附件二七(三)原文）"
    # 缺参时必须保守判超规模（不漏判才是安全红线）
    assert rule.get("oversize_conservative_missing") is True, \
        "缺参时若判非超规模，会漏判真正的 16m+ 深孔"


@pytest.mark.parametrize("depth,expect", [
    (10, False), (15.9, False), (16, True), (20, True),
])
def test_bored_pile_oversize_threshold(depth, expect):
    r = evaluate_hazard_level("ot_bored_pile", {"pile_depth": depth})
    assert r["is_hazardous"] is True, "人工挖孔桩本身即危大（附件一无门槛）"
    assert r["is_oversize"] is expect, \
        f"挖孔深度 {depth}m 超规模判定应为 {expect}（部文 16m 及以上）"


def test_bored_pile_missing_depth_is_conservative():
    """缺参：危大 + 保守超规模 + 列入 missing_params 供上游补全。"""
    r = evaluate_hazard_level("ot_bored_pile", {})
    assert r["is_hazardous"] is True
    assert r["is_oversize"] is True, "缺参时判非超规模会漏判深孔（安全红线）"
    assert r["missing_params"] == ["pile_depth"], "缺参未上报，上游无从补全"
    assert r["oversize_reasons"], "保守判定必须留原因说明（不静默）"


def test_bored_pile_end_to_end():
    """端到端：10m 非超规模 / 16m 超规模 / 缺参保守。"""
    assert classify_scheme("人工挖孔桩专项施工方案",
                           params={"pile_depth": 10}).is_oversize is False
    assert classify_scheme("人工挖孔桩专项施工方案",
                           params={"pile_depth": 16}).is_oversize is True
    assert classify_scheme("人工挖孔桩专项施工方案").is_oversize is True


def test_bored_pile_sub_threshold_declared():
    """子类表必须声明阈值键（否则数据与判定再次分叉）。"""
    subs = {s["id"]: s for c in HAZARD_CATEGORIES for s in c.get("subs", ())}
    assert subs["ot_bored_pile"]["threshold"] == "ot_bored_pile"


# ---------------------------------------------------------------------------
# 4. 回归：非参数型与既有阈值口径不变
# ---------------------------------------------------------------------------

def test_non_param_subs_still_always_oversize():
    """无阈值声明的子类（拆除/暗挖/四新）仍走「出现即危大即超规模」。"""
    for name in ("人工拆除专项施工方案", "地下暗挖专项施工方案"):
        c = classify_scheme(name)
        assert c.is_hazardous is True
        assert c.is_oversize is True, f"{name} 应保持保守判定"


def test_ho_crane_missing_param_not_flagged_missing():
    """ho_crane 未开启保守标志 → 缺参时 missing_params 仍为空（不误报缺参）。"""
    r = evaluate_hazard_level("ho_crane", {})
    assert r["missing_params"] == []
    assert r["is_oversize"] is False


def test_existing_thresholds_unchanged():
    for key, params, hazard, oversize in (
        ("fp_support_drain", {"depth": 3}, True, False),
        ("fp_support_drain", {"depth": 5}, True, True),
        ("fw_tall", {"height": 8}, True, True),
        ("fw_support", {"height": 5}, True, False),
        ("ot_slope", {"slope_height": 6}, True, True),
        ("ho_lift", {"single_weight": 10}, True, False),
        ("ho_lift", {"single_weight": 100}, True, True),
    ):
        r = evaluate_hazard_level(key, params)
        assert (r["is_hazardous"], r["is_oversize"]) == (hazard, oversize), \
            f"{key} {params} 判定被改动：{r}"


def test_no_strict_greater_than_left_in_thresholds():
    """闭区间口径：部文「及以上」不得写成严格大于（脚手架 24m 事故防线）。"""
    for key, rule in HAZARD_THRESHOLDS.items():
        for conds in (rule.get("hazard_when", []), rule.get("oversize_when", [])):
            for _, op, _v in conds:
                assert op == ">=", (
                    f"{key} 出现严格大于号 {op}（部文口径为闭区间）")


def test_module_parses():
    ast.parse(io.open(CLS_PATH, encoding="utf-8").read(), filename=CLS_PATH)
