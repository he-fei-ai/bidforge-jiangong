# -*- coding: utf-8 -*-
"""待补充治理三项后续任务回归测试（2026-09-24）

覆盖：
1. facts_extractor 极端兜底不再伪造「项目名称=待补充」（治 A 类供给噪音）；
2. 重跑计划（治 F 层第 3 条）：字段可补性判定（子串命中可注入语料）、
   叶子章节判定、DB 封装与降级；
3. 监控基线（六层方案第 6 层）：预检落库快照、历史端点、export_check 接线。
"""
import pytest
from app.services.facts_extractor import (
    apply_heuristic_fallback,
    run_post_extract_normalize,
)
from app.services.placeholder_inventory import build_rerun_plan_from_report


# ===========================================================================
# 一、facts 极端兜底：绝不伪造「待补充」
# ===========================================================================
class TestHeuristicFallbackNoFabrication:

    def test_empty_text_returns_empty_list(self):
        """正则一无所获 → 空清单（omit 语义），绝不注入「项目名称=待补充」"""
        items = apply_heuristic_fallback([], "")
        assert items == []
        assert all(it.value != "待补充" for it in items)

    def test_no_text_still_empty(self):
        """fallback_text 为 None 也不抛、不伪造"""
        assert apply_heuristic_fallback([], None) == []

    def test_real_values_still_extracted(self):
        """有真实信息时启发式照常工作（修复不得削弱既有能力）"""
        items = apply_heuristic_fallback(
            [], "工程名称：滨江住宅楼深基坑工程\n建设地点：杭州市拱墅区\n")
        names = {it.name for it in items}
        assert "项目名称" in names and "建设地点" in names
        assert all(it.value != "待补充" for it in items)

    def test_normalize_pipeline_no_placeholder_value(self):
        """后处理管线：空输入产出不含「待补充」字样的模拟值
        （ensure_schedule_fact 的「按合同约定工期执行」是笼统表述，允许保留）"""
        merged = run_post_extract_normalize([], "")
        assert all("待补充" not in str(it.value) for it in merged)
        assert not any(
            it.key == "project_name" and it.source == "default" for it in merged)


# ===========================================================================
# 二、重跑计划
# ===========================================================================
def _report(by_field, by_section):
    return {"total": sum(e["count"] for e in by_section),
            "formatted_total": sum(e["count"] for e in by_section),
            "bare_total": 0, "fuzzy_total": 0,
            "field_count": len(by_field), "section_count": len(by_section),
            "by_field": by_field, "by_section": by_section,
            "occurrences": [], "truncated": False}


class TestBuildRerunPlanFromReport:

    def test_fillable_leaf_and_parent(self):
        """可补性子串命中；父章节即使字段可补也不可重跑（叶子才可重跑）"""
        report = _report(
            [{"field": "基坑深度", "count": 2, "section_ids": ["a", "c"],
              "section_titles": ["工程概况", "监测"]},
             {"field": "桩长", "count": 1, "section_ids": ["b"],
              "section_titles": ["支护设计"]}],
            [{"section_id": "a", "title": "工程概况", "count": 1, "fields": ["基坑深度"]},
             {"section_id": "b", "title": "支护设计", "count": 1, "fields": ["桩长"]},
             {"section_id": "c", "title": "监测", "count": 1, "fields": ["基坑深度"]}])
        # a 是 b 的父节点 → 非叶子；b、c 是叶子
        sections = [{"id": "a", "parent_id": ""},
                    {"id": "b", "parent_id": "a"},
                    {"id": "c", "parent_id": ""}]
        corpus = ["工程概况 基坑深度 8.5m 地下水位"]   # 只含基坑深度
        plan = build_rerun_plan_from_report(report, sections, corpus)
        fill = {f["field"]: f["fillable"] for f in plan["fields"]}
        assert fill == {"基坑深度": True, "桩长": False}
        by_sid = {s["section_id"]: s for s in plan["sections"]}
        assert by_sid["a"]["is_leaf"] is False
        assert by_sid["a"]["rerunnable"] is False     # 字段可补但非叶子
        assert by_sid["b"]["is_leaf"] is True
        assert by_sid["b"]["rerunnable"] is False     # 叶子但字段不可补
        assert by_sid["c"]["rerunnable"] is True
        assert plan["rerunnable_sections"] == ["c"]
        assert plan["rerunnable_count"] == 1
        assert plan["fillable_field_count"] == 1

    def test_bare_and_fuzzy_not_rerunnable(self):
        """只有裸标记/模糊占位的章节没有字段名 → 不进重跑建议（需人工回改）"""
        report = _report(
            [],
            [{"section_id": "x", "title": "其他", "count": 2, "fields": []}])
        plan = build_rerun_plan_from_report(
            report, [{"id": "x", "parent_id": ""}], ["任意语料"])
        assert plan["rerunnable_count"] == 0
        assert plan["sections"][0]["rerunnable"] is False

    def test_illegal_input_empty_plan(self):
        """非法输入降级为空计划（不抛异常）"""
        for bad in ((None, [], []), ({}, None, []), ({}, [], None)):
            plan = build_rerun_plan_from_report(*bad)
            assert plan["rerunnable_count"] == 0


# ===========================================================================
# 三、重跑计划 DB 封装 + 监控基线
# ===========================================================================
async def _seed(db_conn, scheme_id="s1"):
    await db_conn.execute(
        "INSERT INTO sections(id, scheme_id, parent_id, title, level,"
        " sort_order, status, content) VALUES "
        "('p',?,'', '工程概况',1,1,'generated',''),"
        "('a',?,'p', '基坑支护',2,1,'generated','深度【待补充：基坑深度】'),"
        "('b',?,'', '桩基工程',1,2,'generated','桩长【待补充：桩长】')",
        (scheme_id, scheme_id, scheme_id))
    # 可注入语料：基坑深度已确认；桩长存在但未确认（不得算已补齐）
    await db_conn.execute(
        "INSERT INTO global_facts(id, project_id, scheme_id, group_title,"
        " title, content, is_resolved, has_conflict) VALUES "
        "('f1','p1','s1','工程概况','基坑深度','8.5m',1,0),"
        "('f2','p1','s1','工程概况','桩长','待复核',0,0)")
    await db_conn.commit()


@pytest.mark.asyncio
async def test_build_rerun_plan_db(db_conn):
    """DB 封装：已确认事实命中 → a 可重跑；未确认事实不算已补齐 → b 不可重跑"""
    from app.services.placeholder_inventory import build_rerun_plan
    await _seed(db_conn)
    plan = await build_rerun_plan("s1", db_conn)
    assert plan["rerunnable_sections"] == ["a"]
    fill = {f["field"]: f["fillable"] for f in plan["fields"]}
    assert fill == {"基坑深度": True, "桩长": False}


@pytest.mark.asyncio
async def test_build_rerun_plan_degrades_on_db_error():
    """DB 异常降级为空计划（不抛异常）"""
    from app.services.placeholder_inventory import build_rerun_plan

    class _BoomDB:
        async def execute(self, *a, **k):
            raise RuntimeError("db down")

    plan = await build_rerun_plan("s1", _BoomDB())
    assert plan["rerunnable_count"] == 0 and plan["fields"] == []


@pytest.mark.asyncio
async def test_baseline_record_and_history(db_conn):
    """基线：手动落两行 → 历史端点按时间倒序返回"""
    from app.routers.export import _record_placeholder_baseline, placeholder_history
    await _record_placeholder_baseline(
        "s1", {"total": 5, "formatted_total": 3, "bare_total": 1,
               "fuzzy_total": 1, "field_count": 2, "section_count": 2}, db_conn)
    await _record_placeholder_baseline(
        "s1", {"total": 3, "formatted_total": 3, "bare_total": 0,
               "fuzzy_total": 0, "field_count": 1, "section_count": 1}, db_conn)
    data = await placeholder_history("s1", db=db_conn)
    assert len(data["history"]) == 2
    assert data["history"][0]["total"] == 3       # 最新在前
    assert data["history"][-1]["total"] == 5


@pytest.mark.asyncio
async def test_export_check_records_baseline(db_conn):
    """export_check 接线：预检一次 → 基线表多一行（total 与清单一致）"""
    from app.routers.export import export_check, placeholder_history
    await _seed(db_conn)
    result = await export_check("s1", db=db_conn)
    assert result["placeholder_report"]["total"] == 2
    data = await placeholder_history("s1", db=db_conn)
    assert len(data["history"]) == 1
    assert data["history"][0]["total"] == 2
    assert data["history"][0]["field_count"] == 2


@pytest.mark.asyncio
async def test_baseline_write_failure_does_not_raise(db_conn):
    """基线写入失败（表缺失等）只降级：不抛异常、不影响调用方"""
    from app.routers.export import _record_placeholder_baseline

    class _BoomDB:
        async def execute(self, *a, **k):
            raise RuntimeError("no such table")

        async def rollback(self):
            pass

    await _record_placeholder_baseline("s1", {"total": 1}, _BoomDB())  # 不抛即通过


