"""正文/图表模块 2026-09 修复回归测试

覆盖：
- mermaid_gantt._parse_mermaid_gantt：dateFormat X 数字轴（起始天, 天数）
  此前纯数字 token 被整体丢弃，所有任务退化为「每任务 5 天顺序排列」，
  甘特图与真实进度计划完全不符。
- _chart_pipeline._rewrite_code_block：未闭合块保护（与 extract 同口径），
  不再吞噬/改写剩余正文。
- charts.fix_mermaid：修复结果写回必须保持 chart_payload 载荷形状契约
  （数据信封不得被覆盖成"双载荷 unknown"形态）。
"""
import json
import uuid

import pytest

from app.services.ai.mermaid_gantt import _parse_mermaid_gantt
from app.services.chart_payload import (
    build_chart_envelope,
    chart_payload_shape,
    extract_chart_payload,
    is_canonical_chart_payload,
)


# ============================================================
# 甘特图 dateFormat X 数字轴
# ============================================================
class TestGanttNumericAxis:
    def test_numeric_start_and_duration(self):
        code = (
            "gantt\n"
            "    title 进度计划\n"
            "    dateFormat X\n"
            "    axisFormat %d\n"
            "    施工准备 :a1, 1, 10\n"
            "    基础施工 :a2, 11, 20\n"
            "    主体施工 :a3, 31, 30\n"
        )
        tasks = _parse_mermaid_gantt(code)
        assert tasks is not None
        assert [(t["start"], t["end"]) for t in tasks] == [(1, 10), (11, 30), (31, 60)]

    def test_numeric_with_dep_and_milestone(self):
        code = (
            "gantt\n"
            "    dateFormat X\n"
            "    准备 :a1, 1, 5\n"
            "    基础 :after a1, 8, 10\n"
            "    验收 :milestone, m1, 30, 0\n"
        )
        tasks = _parse_mermaid_gantt(code)
        assert tasks is not None
        assert tasks[0]["start"] == 1 and tasks[0]["end"] == 5
        # 显式起始天优先于依赖推算
        assert tasks[1]["start"] == 8 and tasks[1]["end"] == 17
        # 0 工期 = 里程碑单点
        assert tasks[2]["start"] == 30 and tasks[2]["end"] == 30

    def test_date_axis_unaffected(self):
        """dateFormat YYYY-MM-DD 场景行为不得回归。"""
        code = (
            "gantt\n"
            "    dateFormat YYYY-MM-DD\n"
            "    准备 :a1, 2026-01-01, 10d\n"
            "    基础 :a2, 2026-01-11, 5d\n"
        )
        tasks = _parse_mermaid_gantt(code)
        assert tasks is not None
        assert [(t["start"], t["end"]) for t in tasks] == [(1, 10), (11, 15)]

    def test_numeric_token_not_taken_as_task_id(self):
        """纯数字不得被当成任务 id（id 需字母/下划线开头）。"""
        code = "gantt\n    dateFormat X\n    任务A :t1, 5, 15\n"
        tasks = _parse_mermaid_gantt(code)
        assert tasks is not None
        assert tasks[0]["task"] == "任务A"
        assert (tasks[0]["start"], tasks[0]["end"]) == (5, 19)


# ============================================================
# _rewrite_code_block 未闭合块保护
# ============================================================
class TestRewriteCodeBlockUnclosed:
    def test_unclosed_block_preserved_and_no_fence_added(self):
        from app.routers._chart_pipeline import _rewrite_code_block
        content = "正文段落\n```mermaid\nflowchart TD\n    A --> B\n```\n结尾段\n```chart-json\n{\"type\": \"labor\""
        result = _rewrite_code_block(content, "不存在的代码", "新代码")
        # 无匹配 → 正文不变（未闭合块既不被吞噬也不补围栏）
        assert result == content

    def test_unclosed_block_does_not_swallow_remaining_text(self):
        """超 500 行的未闭合块：正文不被整体包进围栏、不补闭合围栏。

        说明：与 extract_inline_charts 同口径——EOF 视为隐式闭合，
        真正的"未闭合"判定是行数超限。
        """
        from app.routers._chart_pipeline import _rewrite_code_block
        content = (
            "```mermaid\nflowchart TD\n    A --> B\n```\n"
            "中间正文\n"
            "```chart-json\n" + "\n".join(f"line{i}" for i in range(600))
        )
        # 删除前面的合法块后，未闭合块与"中间正文"必须原样保留
        result = _rewrite_code_block(content, "flowchart TD\n    A --> B", None)
        assert "flowchart TD" not in result
        assert "中间正文" in result
        assert "line599" in result
        # 未闭合块原样保留（不补闭合围栏）
        assert not result.rstrip().endswith("```")

    def test_closed_block_still_replaced(self):
        from app.routers._chart_pipeline import _rewrite_code_block
        content = "```mermaid\nflowchart TD\n    A --> B\n```\n正文"
        result = _rewrite_code_block(
            content, "flowchart TD\n    A --> B", "flowchart LR\n    X --> Y")
        assert "flowchart LR" in result and "正文" in result


# ============================================================
# fix_mermaid 载荷形状契约
# ============================================================
async def _seed_chart(db, data_json: str, chart_type: str = "labor") -> str:
    pid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO chart_predictions (id, section_id, scheme_id, chart_type,"
        " needed, purpose, priority, status, data_json) VALUES (?,?,?,?,1,?,?,?,?)",
        (pid, "sec1", "s1", chart_type, "测试", 5, "done", data_json))
    await db.commit()
    return pid


@pytest.mark.asyncio
class TestFixMermaidPayloadShape:
    async def test_data_envelope_preserved_when_ai_returns_json(self, db_conn, monkeypatch):
        """数据信封 + AI 返回 JSON → 写回必须仍是 envelope:data（不得双载荷）。"""
        import app.routers.charts as charts_mod
        from app.routers.charts import fix_mermaid

        old = build_chart_envelope(
            data={"type": "labor", "phases": ["准备"], "categories": ["普工"],
                  "data": [[2]]},
            title="劳动力配置", reason="测试")
        pid = await _seed_chart(db_conn, old, chart_type="labor")

        fixed = json.dumps({"type": "labor", "phases": ["准备", "主体"],
                            "categories": ["普工"], "data": [[2], [8]]},
                           ensure_ascii=False)
        async def _fake_chat(messages, **kw):
            return f"```json\n{fixed}\n```"
        monkeypatch.setattr(charts_mod, "chat_with_fallback", _fake_chat)

        await fix_mermaid({"code": '{"type":"labor"}', "prediction_id": pid,
                           "chart_type": "labor"}, db_conn)
        cur = await db_conn.execute(
            "SELECT data_json FROM chart_predictions WHERE id=?", (pid,))
        stored = (await cur.fetchone())[0]
        assert chart_payload_shape(stored) == "envelope:data"
        assert is_canonical_chart_payload(stored)
        # data 被替换为修复后的数据，mermaid_code 保持空
        payload = json.loads(stored)
        assert payload["mermaid_code"] == ""
        assert payload["data"]["phases"] == ["准备", "主体"]
        assert payload["title"] == "劳动力配置"
        assert extract_chart_payload(stored) == fixed

    async def test_code_envelope_when_ai_returns_text(self, db_conn, monkeypatch):
        import app.routers.charts as charts_mod
        from app.routers.charts import fix_mermaid

        old = build_chart_envelope(code="graph TD\n    A --> B", title="流程")
        pid = await _seed_chart(db_conn, old, chart_type="flowchart")

        async def _fake_chat(messages, **kw):
            return "graph TD\n    A --> B\n    B --> C"
        monkeypatch.setattr(charts_mod, "chat_with_fallback", _fake_chat)

        await fix_mermaid({"code": "graph TD\n    A --> B", "prediction_id": pid},
                          db_conn)
        cur = await db_conn.execute(
            "SELECT data_json FROM chart_predictions WHERE id=?", (pid,))
        stored = (await cur.fetchone())[0]
        assert chart_payload_shape(stored) == "envelope:mermaid"
        assert is_canonical_chart_payload(stored)
        assert json.loads(stored)["mermaid_code"] == "graph TD\n    A --> B\n    B --> C"

    async def test_mermaid_text_for_data_chart_stays_canonical(self, db_conn, monkeypatch):
        """数据信封 + AI 返回 Mermaid 文本 → 至少不得产生双载荷 unknown 形态。"""
        import app.routers.charts as charts_mod
        from app.routers.charts import fix_mermaid

        old = build_chart_envelope(
            data={"type": "labor", "phases": ["P"], "categories": ["C"], "data": [[1]]},
            title="劳动力")
        pid = await _seed_chart(db_conn, old, chart_type="labor")

        async def _fake_chat(messages, **kw):
            return "graph TD\n    A --> B"
        monkeypatch.setattr(charts_mod, "chat_with_fallback", _fake_chat)

        await fix_mermaid({"code": '{"type":"labor"}', "prediction_id": pid}, db_conn)
        cur = await db_conn.execute(
            "SELECT data_json FROM chart_predictions WHERE id=?", (pid,))
        stored = (await cur.fetchone())[0]
        assert chart_payload_shape(stored) != "unknown"
        assert is_canonical_chart_payload(stored)
