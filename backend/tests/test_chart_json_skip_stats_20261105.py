"""解析期被跳过的 chart-json 块必须计入图表失败统计（2026-11-05 · R2-P1）。

生产证据：导出日志 2 处「跳过不可渲染的 chart-json 块」，而响应头
``X-Chart-Render-Stats`` 的 ``failed`` 仍为 0 —— 成稿缺图却报「失败 0」。
"""

from app.routers.export import _merge_chart_json_skip_stats
from app.services.content_blocks import _parse_content_blocks


class TestParseDiagnostics:
    def test_unrenderable_chart_json_is_counted(self):
        diag: dict = {}
        # JSON 顶层是数组（非对象、无法推断图表类型）→ 解析期丢弃
        blocks = _parse_content_blocks("```chart-json\n[1, 2, 3]\n```\n", diagnostics=diag)
        assert [b["type"] for b in blocks] == []
        assert diag["chart_json_skipped"] == 1
        assert diag["chart_json_skipped_unrenderable"] == 1

    def test_unclosed_chart_json_is_counted(self):
        diag: dict = {}
        content = '```chart-json\n{"type": "flowchart", "code": "graph TD"}\n'
        _parse_content_blocks(content, diagnostics=diag)
        assert diag.get("chart_json_skipped_unclosed") == 1
        assert diag.get("chart_json_skipped") == 1

    def test_valid_chart_json_not_counted(self):
        diag: dict = {}
        content = '```chart-json\n{"type": "flowchart", "code": "graph TD; A-->B"}\n```\n'
        blocks = _parse_content_blocks(content, diagnostics=diag)
        assert any(b["type"] == "chart" for b in blocks)
        assert diag == {}

    def test_diagnostics_optional_keeps_old_behavior(self):
        # 不传 diagnostics 时函数返回值与旧实现一致（不会被改成抛错）
        content = "```chart-json\n[1, 2, 3]\n```\n"
        assert _parse_content_blocks(content) == []


class TestMergeSkipStats:
    def test_skipped_counts_into_failed(self):
        stats = {"total": 3, "failed": 0, "ok": 3}
        _merge_chart_json_skip_stats(
            stats, {"chart_json_skipped": 2, "chart_json_skipped_unrenderable": 1, "chart_json_skipped_unclosed": 1}
        )
        assert stats["chart_json_skipped"] == 2
        assert stats["failed"] == 2
        assert stats["chart_json_skipped_unclosed"] == 1
        assert stats["chart_json_skipped_unrenderable"] == 1

    def test_no_skip_leaves_header_shape_unchanged(self):
        stats = {"total": 3, "failed": 0, "ok": 3}
        _merge_chart_json_skip_stats(stats, {})
        assert stats == {"total": 3, "failed": 0, "ok": 3}
