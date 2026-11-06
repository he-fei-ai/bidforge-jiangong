"""Phase A（R179 · 2026-11-06）：文种标签引擎验收。

护栏目标
--------
1. 阈值边界（逐项，数值全部来自存量 ``HAZARD_THRESHOLDS``，不自造）；
2. 缺参 → ``undetermined`` + ``missing_params``（不得沿用旧「缺参判危大」塌缩）；
3. 规则零命中 → LLM 仅补类别，``hazard`` 保持 ``undetermined``；
4. 旧 ``classify_scheme`` / ``evaluate_hazard_level`` 输出键与行为逐字保留；
5. 开关关闭时新路由 404（与「路由不存在」的现状一致）。
"""

from __future__ import annotations

import json

import pytest
from app.config import settings
from app.routers.schemes import evaluate_scheme_genre, get_scheme_genre
from app.services import scheme_classification as sc
from app.services.genre_assist import suggest_category
from fastapi import HTTPException

# ---------------------------------------------------------------- 阈值边界


@pytest.mark.parametrize(
    "key,params,exp_hazard,exp_oversize",
    [
        # 基坑：3m 危大 / 5m 超规模（闭区间）
        ("fp_support_drain", {"depth": 2.9}, "no", "no"),
        ("fp_support_drain", {"depth": 3}, "yes", "no"),
        ("fp_support_drain", {"depth": 5}, "yes", "yes"),
        ("fp_earthwork", {"depth": 3}, "yes", "no"),
        ("fp_earthwork", {"depth": 5}, "yes", "yes"),
        # 模板支撑：高 5/8、跨 10/18、总荷载 10/15、线荷载 15/20
        ("fw_support", {"height": 4.9, "span": 9, "total_load": 9, "line_load": 14}, "no", "no"),
        ("fw_support", {"height": 5, "span": 9, "total_load": 9, "line_load": 14}, "yes", "no"),
        ("fw_support", {"height": 8, "span": 9, "total_load": 9, "line_load": 14}, "yes", "yes"),
        ("fw_support", {"height": 4.9, "span": 10, "total_load": 9, "line_load": 14}, "yes", "no"),
        ("fw_support", {"height": 4.9, "span": 18, "total_load": 9, "line_load": 14}, "yes", "yes"),
        ("fw_support", {"height": 4.9, "span": 9, "total_load": 10, "line_load": 14}, "yes", "no"),
        ("fw_support", {"height": 4.9, "span": 9, "total_load": 15, "line_load": 14}, "yes", "yes"),
        ("fw_support", {"height": 4.9, "span": 9, "total_load": 9, "line_load": 15}, "yes", "no"),
        ("fw_support", {"height": 4.9, "span": 9, "total_load": 9, "line_load": 20}, "yes", "yes"),
        # 起重：单件 10kN 危大 / 100kN 超规模；起重机械 300kN 超规模
        ("ho_lift", {"single_weight": 9}, "no", "no"),
        ("ho_lift", {"single_weight": 10}, "yes", "no"),
        ("ho_lift", {"single_weight": 100}, "yes", "yes"),
        ("ho_crane", {"crane_capacity": 299, "crane_height": 50}, "yes", "no"),
        ("ho_crane", {"crane_capacity": 300, "crane_height": 50}, "yes", "yes"),
        ("ho_crane", {"crane_capacity": 50, "crane_height": 200}, "yes", "yes"),
        # 落地式脚手架：24m 危大 / 50m 超规模
        ("sc_ground", {"height": 23.9}, "no", "no"),
        ("sc_ground", {"height": 24}, "yes", "no"),
        ("sc_ground", {"height": 50}, "yes", "yes"),
    ],
)
def test_threshold_boundaries(key, params, exp_hazard, exp_oversize):
    """阈值边界逐项断言：值只取 ``HAZARD_THRESHOLDS`` 现状口径。"""
    lvl = sc.evaluate_hazard_tristate(key, params)
    assert lvl["hazard"] == exp_hazard, (key, params, lvl)
    assert lvl["oversize"] == exp_oversize, (key, params, lvl)
    assert lvl["missing_params"] == [], (key, params, lvl)


def test_threshold_values_come_from_single_source():
    """护栏自检：测试断言的边界值必须与存量阈值表一致（防测试自造数值）。"""
    expected = {
        ("fp_support_drain", "depth"): (3, 5),
        ("sc_ground", "height"): (24, 50),
        ("ho_lift", "single_weight"): (10, 100),
    }
    for (key, param), (haz, over) in expected.items():
        rule = sc.HAZARD_THRESHOLDS[key]
        assert rule["hazard_when"][0][:3] == (param, ">=", haz)
        assert rule["oversize_when"][0][:3] == (param, ">=", over)


# ---------------------------------------------------------------- 缺参三态


def test_missing_params_undetermined_not_hazardous():
    """缺参 → 两个维度均 undetermined，并列出 missing_params（不伪造确定结论）。"""
    lvl = sc.evaluate_hazard_tristate("fp_support_drain", {})
    assert lvl["hazard"] == "undetermined"
    assert lvl["oversize"] == "undetermined"
    assert lvl["missing_params"] == ["depth"]


def test_hit_wins_over_missing_other_params():
    """任一条件命中即确定；其它参数缺失只影响未命中的维度。"""
    lvl = sc.evaluate_hazard_tristate("fw_support", {"height": 5})
    assert lvl["hazard"] == "yes"  # 高度已命中 5m
    assert lvl["oversize"] == "undetermined"  # 8m/18m/15/20 均缺参
    assert set(lvl["missing_params"]) == {"span", "total_load", "line_load"}


def test_old_evaluate_hazard_level_still_collapses_on_missing():
    """遗留待决锁定：旧函数缺参仍保守判危大（本轮不改，新接口已分道）。"""
    old = sc.evaluate_hazard_level("fp_support_drain", {})
    assert old["is_hazardous"] is True
    assert old["missing_params"] == ["depth"]
    new = sc.evaluate_hazard_tristate("fp_support_drain", {})
    assert new["hazard"] == "undetermined"

    old_keys = {"is_hazardous", "is_oversize", "hazard_reasons", "oversize_reasons", "missing_params"}
    assert set(old.keys()) == old_keys
    assert set(sc.classify_scheme("基坑支护专项方案").to_dict().keys()) == {
        "scheme_name",
        "category_ids",
        "category_names",
        "sub_ids",
        "sub_names",
        "is_hazardous",
        "is_oversize",
        "hazards",
        "standards_keys",
        "source_text",
    }


# ---------------------------------------------------------------- 标签构建


def test_genre_tag_contains_rule_trace_and_labels():
    tag = sc.build_genre_tag("深基坑支护及降水专项施工方案", {"depth": 5})
    assert "foundation_pit" in tag.category_ids
    assert tag.hazard == "yes"
    assert tag.oversize == "yes"
    assert tag.expert_review_required is True
    assert tag.template_key in {"foundation_pit", "monitoring"}
    assert "需附计算书" in tag.labels
    ids = {t["rule_id"] for t in tag.rule_trace}
    assert "HZ-fp_support_drain" in ids
    assert any(t["rule_id"].startswith("TPL-") for t in tag.rule_trace)


def test_genre_tag_no_category_is_undetermined():
    """未识别类别 ≠ 确认非危大：保持 undetermined 并标注未识别。"""
    tag = sc.build_genre_tag("土体稳定处理专项方案")
    assert tag.hazard == "undetermined"
    assert tag.oversize == "undetermined"
    assert tag.missing_params == []
    assert tag.labels[0] == "未识别类别"
    assert "无法判定" in tag.labels[1]


def test_layer3_empty_text_is_undetermined():
    tag = sc.build_genre_tag("深基坑支护专项施工方案", {"depth": 5})
    conclusions = {r["rule_id"]: r["conclusion"] for r in tag.layer3}
    assert conclusions["GR-L3-001"] == "undetermined"
    assert conclusions["GR-L3-003"] == "undetermined"
    # 超规模方案且无文本 → 论证前置亦 undetermined
    assert conclusions["GR-L3-004"] == "undetermined"


def test_layer3_forbidden_expression_detected():
    tag = sc.build_genre_tag(
        "深基坑支护专项施工方案",
        {"depth": 5},
        text="本方案由项目总工主持编制，签字可代签。经专家论证通过后实施。",
    )
    rec = {r["rule_id"]: r for r in tag.layer3}
    assert rec["GR-L3-001"]["conclusion"] == "no"
    assert rec["GR-L3-003"]["conclusion"] == "yes"
    assert rec["GR-L3-004"]["conclusion"] == "yes"
    assert "代签" in rec["GR-L3-001"]["evidence"]


# ---------------------------------------------------------------- LLM 辅助


@pytest.mark.asyncio
async def test_llm_assist_only_supplies_category():
    """LLM 建议只补类别：source=llm_suggested，危大/超规模仍 undetermined。"""

    async def fake_chat(messages, **kwargs):
        assert kwargs.get("scene") == "genre_tag_assist"
        return json.dumps(
            {"category_ids": ["foundation_pit"], "sub_ids": ["fp_earthwork"], "reason": "名称含土体处理"},
            ensure_ascii=False,
        )

    assist = await suggest_category("土体稳定处理专项方案", chat_fn=fake_chat)
    assert assist["category_ids"] == ["foundation_pit"]
    assert assist["source"] == "llm_suggested"

    tag = sc.build_genre_tag("土体稳定处理专项方案", assist=assist)
    assert tag.source == "llm_suggested"
    assert tag.category_ids == ["foundation_pit"]
    assert tag.hazard == "undetermined"
    assert tag.oversize == "undetermined"
    assert any(t["rule_id"] == "LLM-SUGGEST" for t in tag.rule_trace)


@pytest.mark.asyncio
async def test_llm_assist_rejects_unknown_ids_and_failsoft():
    async def fake_bad(messages, **kwargs):
        return json.dumps({"category_ids": ["not_a_category"], "sub_ids": ["nope"]})

    assert await suggest_category("某工程专项方案", chat_fn=fake_bad) is None

    async def fake_boom(messages, **kwargs):
        raise RuntimeError("network down")

    assert await suggest_category("某工程专项方案", chat_fn=fake_boom) is None


# ---------------------------------------------------------------- 路由开关


@pytest.mark.asyncio
async def test_routes_disabled_by_default(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "genre_tag_enabled", False)
    await db_conn.execute("INSERT INTO projects (id, name) VALUES (?,?)", ("p1", "项目"))
    await db_conn.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", ("s1", "p1", "基坑支护方案"))
    await db_conn.commit()

    with pytest.raises(HTTPException) as e1:
        await get_scheme_genre("p1", "s1", db=db_conn)
    assert e1.value.status_code == 404
    with pytest.raises(HTTPException) as e2:
        await evaluate_scheme_genre("p1", "s1", {}, db=db_conn)
    assert e2.value.status_code == 404


@pytest.mark.asyncio
async def test_route_evaluate_persists_tag(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "genre_tag_enabled", True)
    await db_conn.execute("INSERT INTO projects (id, name) VALUES (?,?)", ("p2", "项目"))
    await db_conn.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", ("s2", "p2", "基坑支护方案"))
    await db_conn.commit()

    res = await evaluate_scheme_genre("p2", "s2", {"params": {"depth": 5}, "allow_llm": False}, db=db_conn)
    assert res["genre"]["hazard"] == "yes"
    assert res["genre"]["oversize"] == "yes"

    got = await get_scheme_genre("p2", "s2", db=db_conn)
    assert got["genre"]["oversize"] == "yes"
    assert got["genre"]["labels"][0].startswith("基坑")
