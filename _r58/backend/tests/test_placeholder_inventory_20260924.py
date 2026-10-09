# -*- coding: utf-8 -*-
"""《待补充清单》回归测试（2026-09-24，治 F 层：人工补录兜底）

覆盖（对应「待补充问题分析」第四层 / 第六层）：
1. scan_occurrences：三类占位符逐条扫描 —— 规范字段占位【待补充：X】、
   裸标记【待补充】/【待填写】、模糊占位 ××/xx；反例（正文/标识符不误报）；
2. build_placeholder_report：按字段 / 按章节聚合、排序确定性、cap 截断、
   非法输入降级为空报告；
3. build_report_for_scheme + GET /export/placeholder-report 端点函数（直调，
   不落真实库）：库读取、DB 异常降级；
4. collect_export_issues 附带 placeholder_report 精简键（additive，向后兼容）。
"""
import pytest
from app.services.placeholder_inventory import (
    build_placeholder_report,
    build_report_for_scheme,
    scan_occurrences,
)


# ===========================================================================
# 一、单章逐条扫描
# ===========================================================================
class TestScanOccurrences:

    def test_formatted_half_and_full_width_colon(self):
        """规范占位：半角/全角冒号、两端空白均容忍，字段名原样保留"""
        hits = scan_occurrences("基础埋深【待补充:基础埋深】（以勘察报告为准）。"
                                "桩长【待补充： 桩长 】未定。")
        assert [h["field"] for h in hits] == ["基础埋深", "桩长"]
        assert all(h["kind"] == "formatted" for h in hits)

    def test_bare_marks(self):
        """裸标记：【待补充】/【待填写】（facts placeholder 模式产出后者）"""
        hits = scan_occurrences("本参数为【待补充】，另一项为【待填写】。")
        assert [h["kind"] for h in hits] == ["bare", "bare"]
        assert all(h["field"] == "" for h in hits)

    def test_fuzzy_marks(self):
        """模糊占位：×× / 独立 xx / 独立 XX（提示词禁止写法）"""
        hits = scan_occurrences("桩径 ××mm；间距 xxm；标高 XX.m。")
        assert len([h for h in hits if h["kind"] == "fuzzy"]) >= 2

    def test_no_false_positive_on_normal_text(self):
        """反例回归：正常中文、含 xx 的标识符、单个 x 不得误报"""
        assert scan_occurrences("采用 JGJ 120-2012 计算最大位移 max。") == []
        # axxb：xx 前是字母（标识符片段），lookbehind 排除
        assert scan_occurrences("变量 axxb 保持原样") == []

    def test_snippet_contains_context(self):
        """上下文片段：保留匹配点前后文字并折叠空白，便于人工定位"""
        hits = scan_occurrences("基坑深度取【待补充：基坑深度】m，具体见勘察报告。")
        assert hits and "基坑深度取" in hits[0]["snippet"]

    def test_illegal_input_no_raise(self):
        """非法输入不抛异常（纯函数约定）"""
        assert scan_occurrences(None) == []
        assert scan_occurrences(123) == []
        assert scan_occurrences("") == []


# ===========================================================================
# 二、多章聚合
# ===========================================================================
def _sec(sid, title, content, sort_order=0):
    return {"id": sid, "title": title, "content": content,
            "sort_order": sort_order, "level": 1, "status": "generated"}


class TestBuildPlaceholderReport:

    def test_grouping_and_sorting(self):
        """按字段 / 按章节聚合；次数降序，同数次按名称升序（确定性输出）"""
        report = build_placeholder_report([
            _sec("a", "工程概况", "深度【待补充：基坑深度】，埋深【待补充:基础埋深】"),
            _sec("b", "基坑支护", "深度【待补充：基坑深度】。"),
            _sec("c", "监测方案", "无占位符的正文"),
        ])
        assert report["total"] == 3 and report["formatted_total"] == 3
        assert report["field_count"] == 2 and report["section_count"] == 2
        by_field = {e["field"]: e for e in report["by_field"]}
        assert by_field["基坑深度"]["count"] == 2
        assert set(by_field["基坑深度"]["section_ids"]) == {"a", "b"}
        # 次数相同时次键名称升序
        assert [e["field"] for e in report["by_field"]] == ["基坑深度", "基础埋深"]
        # 按章节聚合：a 两处排前
        assert report["by_section"][0]["section_id"] == "a"
        assert report["by_section"][0]["fields"] == ["基坑深度", "基础埋深"]

    def test_kind_totals_split(self):
        """三类占位分口径计数：formatted / bare / fuzzy"""
        report = build_placeholder_report([
            _sec("a", "s", "【待补充：桩长】与【待补充】与 ×× 与【待填写】")])
        assert (report["formatted_total"], report["bare_total"],
                report["fuzzy_total"]) == (1, 2, 1)
        assert report["total"] == 4

    def test_occurrence_cap_truncation(self):
        """逐条记录截断至 cap，并标记 truncated=True（聚合不受影响）"""
        secs = [_sec(f"s{i}", f"章{i}", "【待补充：参数】") for i in range(5)]
        report = build_placeholder_report(secs, occurrence_cap=3)
        assert len(report["occurrences"]) == 3
        assert report["truncated"] is True
        assert report["total"] == 5          # 聚合计数不受 cap 影响
        assert report["field_count"] == 1

    def test_illegal_and_empty_sections(self):
        """非法输入 / 空章节列表 → 形状完整的空报告（不抛异常）"""
        for bad in (None, 123, "x"):
            r = build_placeholder_report(bad)
            assert r["total"] == 0 and r["by_field"] == []
        assert build_placeholder_report([_sec("a", "t", "正文"), None])["total"] == 0


# ===========================================================================
# 三、库读取 + 端点
# ===========================================================================
async def _seed_sections(db_conn, scheme_id="s1"):
    await db_conn.execute(
        "INSERT INTO sections(id, scheme_id, title, level, sort_order, "
        "status, content) VALUES "
        "('a',?, '工程概况',1,1,'generated','深度【待补充：基坑深度】'),"
        "('b',?, '基坑支护',1,2,'generated','桩长【待补充：桩长】，桩径 ××mm'),"
        "('c',?, '监测方案',1,3,'generated','正常正文')",
        (scheme_id, scheme_id, scheme_id))
    await db_conn.commit()


class _BoomDB:
    """任意 execute 均抛错，用于验证 DB 异常降级路径。"""

    async def execute(self, *a, **k):
        raise RuntimeError("db down")


@pytest.mark.asyncio
async def test_build_report_for_scheme(db_conn):
    """库读取：a 1 处规范占位；b 1 规范 + 1 模糊；无占位章节不进 by_section"""
    await _seed_sections(db_conn)
    rep = await build_report_for_scheme("s1", db_conn)
    assert rep["total"] == 3
    assert rep["formatted_total"] == 2 and rep["fuzzy_total"] == 1
    assert rep["section_count"] == 2 and rep["field_count"] == 2
    assert {e["field"] for e in rep["by_field"]} == {"基坑深度", "桩长"}
    assert len(rep["occurrences"]) == 3


@pytest.mark.asyncio
async def test_placeholder_report_endpoint(db_conn):
    """GET /export/placeholder-report 端点函数直调：含逐条 occurrences 定位信息"""
    from app.routers.export import placeholder_report as endpoint
    await _seed_sections(db_conn)
    data = await endpoint("s1", db=db_conn)
    assert data["total"] == 3
    occ = data["occurrences"]
    assert all({"section_id", "section_title", "kind", "snippet"} <= set(o) for o in occ)
    assert any(o["field"] == "基坑深度" and o["section_id"] == "a" for o in occ)


@pytest.mark.asyncio
async def test_build_report_for_scheme_degrades_on_db_error():
    """DB 异常降级为空报告（不抛异常，绝不阻断调用方）"""
    rep = await build_report_for_scheme("s1", _BoomDB())
    assert rep["total"] == 0 and rep["occurrences"] == []


@pytest.mark.asyncio
async def test_collect_export_issues_includes_placeholder_report(db_conn):
    """导出预检附带 placeholder_report 精简键（additive，既有键不受影响）"""
    from app.routers.export import collect_export_issues
    await _seed_sections(db_conn)
    data = await collect_export_issues("s1", db_conn)
    pr = data["placeholder_report"]
    assert pr["total"] == 3 and pr["formatted_total"] == 2
    assert {"by_field", "by_section"} <= set(pr)
    assert "occurrences" not in pr     # 精简版不带逐条记录（响应体控制）
    # 既有键不受影响（向后兼容）
    assert {"issues", "section_count", "content_audit", "review_summary"} <= set(data)
