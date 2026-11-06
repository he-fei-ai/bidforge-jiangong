"""R178 补强 · 图表告警可见性（chart_ungenerated / chart_failed）。

背景（R178 验收报告 §6 断点候选①）：
    ``chart_ungenerated`` / ``chart_failed`` 是降级专项认定的「静默失真」高危位 ——
    图表没生成或渲染失败时告警字段存在，但**没有任何断言**。仓库里唯一一次出现
    这两个词只在一段 docstring 里。一旦退化，症状全是静默的：预检照样「通过」、
    导出照样成功、交付文档只是少几张图。

本文件锁**行为**而不是字段名：
  A 两种状态产出**可区分**的告警（未生成 vs 生成失败），完成态不误报；
  B 失败不计入 done / pending（统计口径不被吞并，总数守恒）；
  C 告警经 ``export_check`` 响应体（终态字段）对用户可见，不只写日志；
  D 告警经 ``export_issues_to_findings`` 进入就绪度评分（不被静默跳过）；
  E 渲染失败时失败计数进响应头 ``X-Chart-Render-Stats``，且**不占图号**、
    后续图编号连续（不虚跳）。
"""

from __future__ import annotations

import json

import app.routers.export as E
import pytest


def _section_rows(scheme_id: str) -> list[tuple]:
    return [
        (
            "sec1",
            scheme_id,
            "",
            0,
            1,
            "施工工艺技术",
            "generated",
            "脚手架搭设与验收按本方案执行。" * 40,
            300,
            "approved",
        ),
    ]


async def _seed(db, scheme_id: str, charts: list[tuple[str, str]]) -> None:
    """种一个章节 + 若干图表登记；charts = [(chart_type, status), ...]。"""
    await db.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", (scheme_id, "proj1", "图表告警补强方案")
    )
    for row in _section_rows(scheme_id):
        # 列顺序：id, scheme_id, parent_id, sort_order, level, title, status,
        #         content, word_count, review_status
        await db.execute(
            "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level,"
            " title, status, content, word_count, review_status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            row,
        )
    for idx, (chart_type, status) in enumerate(charts):
        await db.execute(
            "INSERT INTO chart_predictions (id, section_id, scheme_id, chart_type,"
            " needed, purpose, priority, status, data_json)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (f"cp{idx}", "sec1", scheme_id, chart_type, 1, "", 0, status, ""),
        )
    await db.commit()


def _chart_issues(result: dict) -> list[dict]:
    return [i for i in result["issues"] if i.get("type") in ("chart_failed", "chart_ungenerated")]


# ===========================================================================
# A 组 · 两种状态可区分，完成态静默
# ===========================================================================


class TestChartAlarmDistinguishable:
    async def test_pending_reported_as_ungenerated(self, db_conn):
        """未生成 ≠ 生成失败：类型与 detail 必须能让用户看出「还没生成」。"""
        await _seed(db_conn, "s1", [("layout", "pending")])
        result = await E.collect_export_issues("s1", db_conn)
        issues = _chart_issues(result)
        assert len(issues) == 1, issues
        assert issues[0]["type"] == "chart_ungenerated"
        assert issues[0]["status"] == "pending"
        assert issues[0]["chart_type"] == "layout"
        assert issues[0]["detail"], "未生成告警必须带可读说明（不能只有裸类型）"
        assert "占位" in issues[0]["detail"] or "未生成" in issues[0]["detail"]

    async def test_failed_reported_as_chart_failed(self, db_conn):
        """生成失败落 chart_failed（不是 pending，也不被当成完成）。"""
        await _seed(db_conn, "s1", [("flowchart", "failed")])
        result = await E.collect_export_issues("s1", db_conn)
        issues = _chart_issues(result)
        assert len(issues) == 1, issues
        assert issues[0]["type"] == "chart_failed"
        assert issues[0]["status"] == "failed"
        assert issues[0]["chart_type"] == "flowchart"

    async def test_two_states_have_distinct_types(self, db_conn):
        """同一方案同时存在两种状态 → 两类告警并存，且能按 type 分开计数。"""
        await _seed(db_conn, "s1", [("layout", "pending"), ("flowchart", "failed")])
        result = await E.collect_export_issues("s1", db_conn)
        issues = _chart_issues(result)
        types = sorted(i["type"] for i in issues)
        assert types == ["chart_failed", "chart_ungenerated"]
        by_type = {i["type"]: i for i in issues}
        assert by_type["chart_ungenerated"]["detail"] != by_type["chart_failed"].get("detail", "")
        assert by_type["chart_ungenerated"]["status"] == "pending"
        assert by_type["chart_failed"]["status"] == "failed"

    @pytest.mark.parametrize("done_status", ["done", "generated"])
    async def test_completed_statuses_are_silent(self, db_conn, done_status):
        """done / generated 是历史两套写法，二者都不得误报（否则正文内嵌图被喊缺图）。"""
        await _seed(db_conn, "s1", [("architecture", done_status)])
        result = await E.collect_export_issues("s1", db_conn)
        assert _chart_issues(result) == []
        assert result["chart_done"] == 1


# ===========================================================================
# B 组 · 统计口径：失败既不进 done 也不进 pending，总数守恒
# ===========================================================================


class TestChartCountsKeepFailedSeparate:
    async def test_failed_is_not_absorbed_into_done_or_pending(self, db_conn):
        await _seed(
            db_conn,
            "s1",
            [
                ("architecture", "done"),
                ("layout", "pending"),
                ("flowchart", "failed"),
            ],
        )
        result = await E.collect_export_issues("s1", db_conn)
        assert result["chart_total"] == 3
        assert result["chart_done"] == 1
        assert result["chart_pending"] == 1
        failed = [i for i in result["issues"] if i.get("type") == "chart_failed"]
        assert len(failed) == 1
        # 守恒：总数 = 完成 + 未生成 + 失败（失败不得被任一桶吞掉）
        assert result["chart_total"] == result["chart_done"] + result["chart_pending"] + len(failed)


# ===========================================================================
# C 组 · 用户可见：export_check 响应体（终态字段）
# ===========================================================================


class TestAlarmVisibleInExportCheck:
    async def test_export_check_body_carries_both_alarms(self, db_conn):
        """只写日志的告警等于没有告警：必须能在预检响应里读到。"""
        await _seed(db_conn, "s1", [("layout", "pending"), ("flowchart", "failed")])
        result = await E.export_check("s1", db=db_conn)
        # 成功路径必须显式声明「数据完整」，与 503 降级体区分（B1-1 契约）
        assert result["read_degraded"] is False
        issues = _chart_issues(result)
        assert {i["type"] for i in issues} == {"chart_failed", "chart_ungenerated"}
        assert result["chart_total"] == 2
        assert result["chart_done"] == 0
        assert result["chart_pending"] == 1
        # 未生成的可读说明随响应一起返回（前端据此展示原因）
        ungen = next(i for i in issues if i["type"] == "chart_ungenerated")
        assert ungen["detail"]


# ===========================================================================
# D 组 · 告警进入就绪度评分（不被静默跳过）
# ===========================================================================


class TestAlarmReachesFindings:
    def test_both_types_bridge_to_registered_rule(self):
        findings = E.export_issues_to_findings(
            [
                {"type": "chart_failed", "chart_type": "flowchart", "status": "failed"},
                {"type": "chart_ungenerated", "chart_type": "layout", "status": "pending", "detail": "占位"},
            ]
        )
        assert len(findings) == 1, "同类告警应聚合为一条 finding"
        finding = findings[0]
        assert finding["rule_id"] == "DLV-04"
        # 聚合取最严重：生成失败(medium) 不得被未生成(low) 拉低
        assert finding["severity"] == "medium"
        assert finding["count"] == 2
        assert "共 2 处" in finding["detail"]
        assert finding["title"], "finding 必须有可读标题（规则已登记）"

    def test_ungenerated_alone_is_low(self):
        findings = E.export_issues_to_findings(
            [
                {"type": "chart_ungenerated", "chart_type": "layout", "status": "pending"},
            ]
        )
        assert findings[0]["rule_id"] == "DLV-04"
        assert findings[0]["severity"] == "low"


# ===========================================================================
# E 组 · 渲染失败：计入统计头 + 不占图号 + 后续编号连续
# ===========================================================================

CHART_OK_A = (
    "```chart-json\n"
    '{"type":"architecture","title":"施工管理组织架构图",'
    '"root":{"label":"项目经理","children":[{"label":"技术负责人"}]}}\n'
    "```\n"
)
CHART_FAIL = (
    "```chart-json\n"
    '{"type":"labor"}\n'  # 空壳 labor：合法白名单类型，但渲染必然失败
    "```\n"
)
CHART_OK_B = (
    "```chart-json\n" '{"type":"architecture","title":"悬挑层管理组织架构图",' '"root":{"label":"项目经理"}}\n' "```\n"
)


class TestRenderFailureFigureNumbering:
    async def test_fail_in_middle_does_not_consume_number(self, db_conn):
        """三图中间一张必然渲染失败：失败入统计、图号不虚跳、后续编号连续。"""
        await db_conn.execute(
            "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", ("sr", "pr", "渲染失败编号连续性方案")
        )
        content = (
            "施工管理组织架构如下图所示：\n\n"
            + CHART_OK_A
            + "\n劳动力配置如下图所示：\n\n"
            + CHART_FAIL
            + "\n悬挑层管理组织架构如下图所示：\n\n"
            + CHART_OK_B
        )
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, title,"
            " status, content) VALUES (?,?,?,?,?,?,?,?)",
            ("sr1", "sr", "", 0, 1, "施工工艺技术", "generated", content),
        )
        await db_conn.commit()

        resp = await E.export_docx(
            "sr",
            {"config": {"allow_pil_fallback": True}, "override_high_issues": True},
            db=db_conn,
        )
        # 覆写路径必须留响应头，证明本轮是「已知悉并继续」而非闸门失效
        assert resp.headers.get("X-Export-Override") == "high-issues"

        from docx import Document

        joined = "\n".join(p.text for p in Document(resp.path).paragraphs)
        # 失败图不印红字占位（默认跳过形态）
        assert "渲染失败" not in joined, joined
        # 中间失败不占号：后一张顶上 1-2（若虚跳则会是 1-3）
        assert "图 1-1 施工管理组织架构图" in joined, joined
        assert "图 1-2 悬挑层管理组织架构图" in joined, joined
        assert "图 1-3" not in joined, joined
        # 失败计数进系统侧统计头（前端 exportResponse 消费该头）
        stats = json.loads(resp.headers["X-Chart-Render-Stats"])
        assert stats["failed"] >= 1, stats
        assert stats["backend_ok"] >= 2, stats
