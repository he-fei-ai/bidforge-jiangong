"""F-CONTENT-STANDARD：生成标准校验报告 REST 端点测试。"""
import json

import pytest


async def _setup_scheme_and_section(db):
    """准备好一个有 scheme + section + 生成标准数据的 fixture。"""
    await db.execute(
        "INSERT OR IGNORE INTO projects (id, name) VALUES (?, ?)", ("p1", "测试项目"))
    await db.execute(
        "INSERT INTO schemes (id, project_id, name, generation_standard) VALUES (?,?,?,?)",
        ("s1", "p1", "测试方案", "precise"))
    await db.execute(
        "INSERT INTO sections (id, scheme_id, title, generation_standard, "
        "  last_generation_standard, last_generation_report, status) "
        "VALUES (?,?,?,?,?,?,?)",
        ("sec1", "s1", "基坑支护", "", "precise",
         json.dumps({"standard": "precise", "passed": True,
                     "error_count": 0, "warning_count": 0, "issues": []},
                    ensure_ascii=False),
         "generated"))
    await db.execute(
        "INSERT INTO sections (id, scheme_id, title, generation_standard, "
        "  last_generation_standard, last_generation_report, status) "
        "VALUES (?,?,?,?,?,?,?)",
        ("sec2", "s1", "土方开挖", "", "fuzzy",
         json.dumps({"standard": "fuzzy", "passed": False,
                     "error_count": 1, "warning_count": 2,
                     "issues": [{"type": "value_conflict", "severity": "error",
                                 "message": "矛盾1"},
                                {"type": "fuzzy_expression", "severity": "warning",
                                 "message": "模糊1"},
                                {"type": "fuzzy_expression", "severity": "warning",
                                 "message": "模糊2"}],
                     "stats": {"placeholders": 0, "checked_facts": 1}},
                    ensure_ascii=False),
         "generated"))
    await db.execute(
        "INSERT INTO sections (id, scheme_id, title, status) VALUES (?,?,?,?)",
        ("sec3", "s1", "未生成章节", "empty"))
    await db.commit()


class TestReportEndpoint:
    """单个章节报告端点。"""

    async def test_section_report_ok(self, db_conn):
        await _setup_scheme_and_section(db_conn)
        from app.routers.sections import section_generation_report
        rep = await section_generation_report("s1", "sec1", db=db_conn)
        assert rep["section_id"] == "sec1"
        assert rep["section_title"] == "基坑支护"
        assert rep["last_generation_standard"] == "precise"
        assert rep["report"]["passed"] is True
        assert rep["report"]["error_count"] == 0

    async def test_section_report_with_issues(self, db_conn):
        await _setup_scheme_and_section(db_conn)
        from app.routers.sections import section_generation_report
        rep = await section_generation_report("s1", "sec2", db=db_conn)
        assert rep["last_generation_standard"] == "fuzzy"
        assert rep["report"]["passed"] is False
        assert rep["report"]["error_count"] == 1
        assert rep["report"]["warning_count"] == 2
        assert len(rep["report"]["issues"]) == 3

    async def test_section_report_empty_fallback(self, db_conn):
        """未生成章节 → 空报告（默认 precise）。"""
        await _setup_scheme_and_section(db_conn)
        from app.routers.sections import section_generation_report
        rep = await section_generation_report("s1", "sec3", db=db_conn)
        assert rep["report"]["passed"] is True
        assert rep["report"]["error_count"] == 0

    async def test_section_report_404(self, db_conn):
        from app.routers.sections import section_generation_report
        from fastapi import HTTPException
        try:
            await section_generation_report("s1", "nonexistent", db=db_conn)
            pytest.fail("should raise HTTPException")
        except HTTPException as e:
            assert e.status_code == 404


class TestReportSummaryEndpoint:
    """方案汇总端点。"""

    async def test_summary_aggregates(self, db_conn):
        await _setup_scheme_and_section(db_conn)
        from app.routers.sections import scheme_report_summary
        rep = await scheme_report_summary("s1", db=db_conn)
        assert rep["scheme_id"] == "s1"
        assert rep["total"] == 3
        # sec1 precise, sec2 fuzzy, sec3 precise（回落默认）
        assert rep["by_standard"] == {"precise": 2, "fuzzy": 1}
        assert rep["total_errors"] == 1
        assert rep["total_warnings"] == 2
        assert rep["total_issues"] == 3
        # issue_types 汇总
        assert rep["issue_type_counts"]["fuzzy_expression"] == 2
        assert rep["issue_type_counts"]["value_conflict"] == 1
        # sections 数组
        sec2 = [s for s in rep["sections"] if s["section_id"] == "sec2"][0]
        assert sec2["passed"] is False
        assert sec2["error_count"] == 1
        assert sec2["warning_count"] == 2

    async def test_summary_empty_scheme(self, db_conn):
        await db_conn.execute(
            "INSERT OR IGNORE INTO projects (id, name) VALUES (?, ?)", ("p2", "p"))
        await db_conn.execute(
            "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
            ("s2", "p2", "空方案"))
        await db_conn.commit()
        from app.routers.sections import scheme_report_summary
        rep = await scheme_report_summary("s2", db=db_conn)
        assert rep["total"] == 0
        assert rep["by_standard"] == {}
        assert rep["sections"] == []
