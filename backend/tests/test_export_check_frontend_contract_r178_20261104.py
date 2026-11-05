"""R178 补强 · 导出预检前后端契约（后端 pytest 读前端源文件）。

背景（R178 验收报告 §6 断点候选②）：
    后端算出问题清单、前端是否正确展示，中间**没有任何断言**。``export_check``
    的响应体与前端 ``handleExportCheck`` 的消费点分属两种语言、两个模块，任何
    一方改字段名/改语义都不会有测试变红 —— 最坏形态是「预检假通过」：
    后端 503 降级（没读到数据），前端却渲染成「无问题」。

范式：本仓前端未装 ``@types/node``（AGENTS.md §6），跨端一致性由**后端 pytest
读前端源文件**锁定 —— 与 ``test_charts_r13_and_types_parity_20261004.py::
TestExportIssueLabelParity``、``TestExportGateHighSeverityParity`` 同一手法，
不自创第二套。

覆盖：
  A 成功响应：逐字段与前端实际读取键对齐（含字段类型与空态）
  B issue 明细：前端消费的键后端确实产出；``type`` 键不得缺失（否则标签回退成裸代号）
  C 统计口径：前端 ``ExportStats`` 接口字段 ⊆ 后端产出键
  D 降级态：503 体键与前端 catch 读取逐字段对齐，且语义上**不得**被当成「无问题」
  E 三态分支：无问题 / 有问题 / 降级，前端各走不同可见出口
"""

import asyncio
import json
import re
from pathlib import Path

import app.routers.export as E

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONT_PAGE = REPO_ROOT / "frontend" / "src" / "pages" / "SchemeWorkbenchPage.tsx"
FRONT_WB_DIR = REPO_ROOT / "frontend" / "src" / "pages" / "schemeWorkbench"


def _front_sources() -> str:
    """facade 页面 + schemeWorkbench/ 子模块聚合源码（与既有 parity 测试同口径）。"""
    parts = [FRONT_PAGE.read_text(encoding="utf-8")]
    # ✅ R176（2026-11-04）：页面已拆分到 schemeWorkbench/tabs/ 子目录，
    #    非递归 glob 会漏掉后移的可见出口（本文件曾因此误报「降级态无出口」）。
    for p in sorted(list(FRONT_WB_DIR.rglob("*.ts")) + list(FRONT_WB_DIR.rglob("*.tsx"))):
        parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _export_check_handler_src() -> str:
    """只取 ``handleExportCheck`` 函数体 —— 避免把别的 API 的 ``data.xxx`` 算进来。"""
    src = _front_sources()
    start = src.index("const handleExportCheck = async () => {")
    end = src.index("\n  };\n", start)
    return src[start:end]


async def _seed(db, scheme_id: str, charts: list[tuple[str, str]]) -> None:
    await db.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", (scheme_id, "p1", "契约补强方案"))
    await db.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, title,"
        " status, content, word_count, review_status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
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
    )
    for idx, (chart_type, status) in enumerate(charts):
        await db.execute(
            "INSERT INTO chart_predictions (id, section_id, scheme_id, chart_type,"
            " needed, purpose, priority, status, data_json)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (f"cp{idx}", "sec1", scheme_id, chart_type, 1, "", 0, status, ""),
        )
    await db.commit()


# ===========================================================================
# A 组 · 成功响应逐字段对齐
# ===========================================================================


class TestSuccessResponseFieldAlignment:
    async def test_every_frontend_data_read_has_backend_field(self, db_conn):
        """前端 handler 里每一个 ``data.<key>`` 都必须在后端响应里有同名键。"""
        await _seed(db_conn, "s1", [("layout", "pending"), ("flowchart", "failed")])
        result = await E.export_check("s1", db=db_conn)

        used = set(re.findall(r"data\.([A-Za-z_][A-Za-z0-9_]*)", _export_check_handler_src()))
        missing = {k for k in used if k not in result}
        assert not missing, "前端读取的导出预检字段后端未返回（界面会拿到 undefined）：" + ", ".join(sorted(missing))
        # 关键消费点必须真的被前端读取（防止本测试只剩空集合式空转）
        assert {
            "issues",
            "section_count",
            "generated_count",
            "total_words",
            "empty_ratio",
            "chart_total",
            "chart_done",
            "preflight_summary",
            "placeholder_report",
        } <= used

    async def test_success_value_types_match_frontend_usage(self, db_conn):
        """字段类型：计数是整数、占比是数值、明细是数组、摘要/清单是对象。"""
        await _seed(db_conn, "s1", [("layout", "pending"), ("flowchart", "failed")])
        result = await E.export_check("s1", db=db_conn)

        assert isinstance(result["issues"], list)
        for key in ("section_count", "generated_count", "total_words", "chart_total", "chart_done", "chart_pending"):
            assert isinstance(result[key], int), (key, type(result[key]))
        assert isinstance(result["empty_ratio"], (int, float))
        for key in (
            "preflight_summary",
            "placeholder_report",
            "content_audit",
            "review_summary",
            "global_facts_summary",
        ):
            assert isinstance(result[key], dict), (key, type(result[key]))
        # bid_analysis_summary 是**可空**字段：方案未关联项目提取时后端如实返回 None。
        # 空态与「提取失败」不得靠「缺键」区分（缺键会让前端读到 undefined）。
        assert result["bid_analysis_summary"] is None or isinstance(result["bid_analysis_summary"], dict)
        assert isinstance(result["read_degraded"], bool)
        assert result["read_degraded"] is False

    async def test_empty_state_is_empty_list_not_missing(self, db_conn):
        """无问题空态：``issues`` 必须是 ``[]``（不是缺键），计数为 0。"""
        await _seed(db_conn, "s1", [])
        result = await E.export_check("s1", db=db_conn)
        assert result["issues"] == []
        assert result["chart_total"] == 0
        assert result["chart_done"] == 0
        assert result["chart_pending"] == 0
        # 空态仍与降级态可区分（成功路径显式声明数据完整）
        assert result["read_degraded"] is False


# ===========================================================================
# B 组 · issue 明细的键
# ===========================================================================


class TestIssueFieldAlignment:
    async def test_frontend_consumed_issue_fields_are_produced(self, db_conn):
        """``describeExportIssue`` 读 title/detail/chart_type —— 图表告警必须给足。"""
        await _seed(db_conn, "s1", [("layout", "pending"), ("flowchart", "failed")])
        issues = (await E.collect_export_issues("s1", db_conn))["issues"]
        chart_issues = [i for i in issues if i["type"] in ("chart_failed", "chart_ungenerated")]
        assert chart_issues
        for issue in chart_issues:
            # 页面以 ``EXPORT_ISSUE_LABEL[iss.type!] || iss.type`` 取标签：
            # type 缺失/空串会让界面显示裸 ``undefined``，这是可见性破口。
            assert issue.get("type"), issue
            assert issue.get("chart_type"), issue
        ungen = next(i for i in chart_issues if i["type"] == "chart_ungenerated")
        # 未生成告警有 detail，前端 describeExportIssue 的兜底链才有内容可显示
        assert ungen.get("detail"), ungen

    def test_chart_alarms_have_chinese_labels(self):
        """两类图表告警在中文标签表里必须各有独立标签（能区分，非同一文案）。"""
        src = _front_sources()
        m = re.search(r"const EXPORT_ISSUE_LABEL[^=]*=\s*\{(.*?)\n\};", src, re.S)
        assert m, "未找到前端 EXPORT_ISSUE_LABEL 定义"
        labels = dict(re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"([^"]*)"', m.group(1)))
        cjk = re.compile(r"[\u4e00-\u9fff]")
        for key in ("chart_failed", "chart_ungenerated"):
            assert key in labels, f"图表告警 {key} 缺中文标签（界面会显示英文裸代号）"
            assert cjk.search(labels[key]), labels[key]
        assert (
            labels["chart_failed"] != labels["chart_ungenerated"]
        ), "「生成失败」与「未生成」共用同一标签 → 用户无法区分两种状态"


# ===========================================================================
# C 组 · 统计口径：前端类型定义 vs 后端产出
# ===========================================================================


class TestStatsInterfaceAlignment:
    def test_export_stats_interface_fields_are_all_produced(self):
        src = _front_sources()
        m = re.search(r"export interface ExportStats\s*\{(.*?)\n\}", src, re.S)
        assert m, "未找到前端 ExportStats 接口定义"
        fields = set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\??\s*:", m.group(1), re.M))
        assert fields, "ExportStats 接口为空"
        # 后端产出键（这里用真实响应，不用源码字面量）
        result = asyncio.run(_probe_backend())
        missing = fields - set(result)
        assert not missing, "前端 ExportStats 声明了后端不返回的字段（类型定义与响应形状漂移）：" + ", ".join(
            sorted(missing)
        )


async def _probe_backend():
    """独立内存库探测一次 ``export_check`` 的键集合（避开 fixture 依赖）。"""
    import aiosqlite
    from app.db import _migrate
    from app.schema_sql import SCHEMA_SQL

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_SQL)
    await _migrate(conn)
    await _seed(conn, "probe", [])
    result = await E.export_check("probe", db=conn)
    await conn.close()
    return result


# ===========================================================================
# D 组 · 降级态：503 体 ↔ 前端 catch
# ===========================================================================


class TestDegradedStateAlignment:
    async def test_degraded_body_shape_and_semantics(self, monkeypatch):
        async def _boom(*_a, **_k):
            raise E.DBReadError("db.execute 返回 None（collect_export_issues:sections）")

        monkeypatch.setattr(E, "collect_export_issues", _boom)

        resp = await E.export_check("s1", db=object())
        assert resp.status_code == 503
        body = json.loads(resp.body)
        assert body["read_degraded"] is True
        assert body["error"] == "export_check_read_failed"
        assert body["retry_after"] == 5
        assert resp.headers["Retry-After"] == "5"
        # 语义：必须明说「本次结果不代表无问题」，否则前端有借口渲染成通过
        assert "不代表「无问题」" in body["detail"] or "不代表" in body["detail"]

    def test_frontend_catch_reads_same_key_and_blocks_no_problem_ui(self):
        src = _front_sources()
        assert (
            "err.response?.data?.read_degraded" in src
        ), "前端 catch 未读取 503 体的 read_degraded —— 降级会被当成普通失败/无问题"
        assert (
            "setExportCheckIncomplete(err.response?.data?.read_degraded === true)" in src
        ), "read_degraded 未接线到「预检未完成」状态（写了不接线等于无护栏）"
        assert "⚠️ 预检未完成" in src, "降级态没有用户可见出口"
        assert "不能视为「无问题」" in src, "降级态文案未明确否定「无问题」→ 用户会误以为预检通过"
        # 新一轮预检开始必须清掉旧降级标记（否则一次失败后永远显示未完成）
        assert "setExportCheckIncomplete(false)" in src


# ===========================================================================
# E 组 · 三态分支各有可见出口
# ===========================================================================


class TestThreeStateBranches:
    def test_issue_presence_branch_has_both_outcomes(self):
        handler = _export_check_handler_src()
        assert "if (!data.issues?.length)" in handler, "前端未按 issues 长度分支 —— 无问题与有问题无法区分展示"
        assert "预检通过" in handler and "发现" in handler and "个问题" in handler

    def test_issue_alert_uses_label_and_describe(self):
        src = _front_sources()
        assert (
            "EXPORT_ISSUE_LABEL[iss.type!] || iss.type" in src
        ), "问题清单未用 type→中文标签（缺标签时再把 type 作为最后兜底）"
        assert (
            "describeExportIssue(iss)" in src
        ), "问题清单未展示可读描述（只有类型标签，用户不知道具体是哪一章/哪张图）"
        assert "导出预检发现" in src, "有问题态缺用户可见的问题计数出口"
