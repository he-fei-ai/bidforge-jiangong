"""R208 · 遗留项收口与加固护栏（2026-11-09）。

两类断言：
  A. 设计取舍 / 技术债**棘轮**：R205 报告 §7 登记了「按最优方案维持不改」的项，
     文字登记会被后续会话顺手改掉，本组把它们锁成可执行断言（形态 + 留痕），
     任何「统一成 fail-closed」「顺手删死旋钮」都会当场变红。
  B. 本轮 R208 落地项的回归锁（行为级 + 源码锁 + 跨端 parity）。
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
DEAD_KNOBS = ("image_max_per_project", "image_quality", "outline_relevance_boost")


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestDesignTradeoffRatchets:
    """登记项：维持现状是产品/历史契约，不是待修 BUG。"""

    def test_facts_write_scope_failure_stays_fail_open_with_warning(self):
        src = _src("backend/app/routers/global_facts.py")
        i = src.index("project_id = await _facts_write_scope_project_id")
        seg = src[i : i + 900]
        assert "无锁放行" in seg, "写锁作用域解析失败的有意 fail-open 被改成抛错"
        assert "logger.warning" in seg, "fail-open 必须留 WARNING，不得静默"
        assert "redact_sensitive_text" in seg, "异常文本必须脱敏"
        assert "if project_id:" in seg and "HTTPException" not in seg.split("if project_id:")[0]

    def test_export_cache_invalidation_degrades_with_error_log(self):
        src = _src("backend/app/services/facts_extractor.py")
        # ⚠️ 文件里有多个 `if cur is None:`（persist_extraction 那处是 raise，
        #    与本登记项无关）——锚点必须先定位到 invalidate_export_cache。
        i = src.index("def invalidate_export_cache")
        j = src.index("if cur is None:", i)
        seg = src[j : j + 500]
        assert "rows: list[dict] = []" in seg, "R189 有意 fail-soft 被改成抛 503"
        assert "logger.error" in seg, "降级必须留 ERROR 日志，不得静默跳过清缓存"
        assert "R13" in seg

    def test_export_gate_fail_open_stays_visible(self):
        src = _src("backend/app/routers/export.py")
        assert "允许继续导出" in src, "B-17 设计取舍（fail-open）被改成 fail-fast"
        assert '"checked": False' in src
        assert "X-Export-Gate" in src and "unchecked" in src, "fail-open 可见化响应头不得删"

    def test_dead_knobs_documented_and_still_unread(self):
        cfg = _src("backend/app/config.py")
        for key in DEAD_KNOBS:
            assert f"{key}:" in cfg
            i = cfg.index(f"{key}:")
            assert "死配置" in cfg[max(0, i - 400) : i] or "预留" in cfg[max(0, i - 400) : i], (
                f"{key} 缺诚实化注释（R199/R208 要求标注为死旋钮）"
            )
        hits: list[tuple[str, int]] = []
        for path in (ROOT / "backend" / "app").rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in DEAD_KNOBS:
                    hits.append((str(path.relative_to(ROOT)), node.lineno))
        assert hits == [], f"死旋钮被接线但注释/护栏仍称零读点：{hits[:5]}"

    def test_large_projects_still_registered_not_silently_rewritten(self):
        """大工程登记锁：这些形态当前有意保留，改动须走专门轮次并同步本护栏。"""
        pipeline = _src("backend/app/services/doc_pipeline/pipeline.py")
        assert "sync_extract_layer" in pipeline or "sync_extract" in pipeline
        versions = _src("backend/app/services/outline_versions.py")
        assert "DELETE FROM sections" in versions, "快照回退整表重建形态变化 → 需重新评估关联表清理"
        sse = _src("backend/app/routers/sse_handlers.py")
        assert "_knowledge_text_for" in sse, "R204 L-1 知识库稳定段形态变化 → 需单列 Prompt Cache 轮次"


class TestR208BackendFixes:
    """本轮 12 项修复的回归锁。"""

    async def test_governance_put_returns_delay_ms(self, monkeypatch):
        from app.routers import system

        async def _load():
            return {}

        async def _save(_kv):
            return None

        monkeypatch.setattr(system, "_governance_kv_load", _load)
        monkeypatch.setattr(system, "_governance_kv_save", _save)
        out = await system.outline_auto_confirm_put(SimpleNamespace(enabled=True))
        assert out["enabled"] is True
        assert isinstance(out["delay_ms"], int) and out["delay_ms"] > 0

    def test_parse_status_success_branch(self):
        src = _src("backend/app/routers/global_facts_docs.py")
        assert '"parse_status": "success"' in src
        assert 'result: dict = {"ok": True, "text_len"' not in src

    def test_bid_analysis_start_errors_are_stable_503(self):
        src = _src("backend/app/routers/bid_analysis.py")
        for marker in ("结构化解析 /start 崩溃", "结构化解析 SSE 入口崩溃"):
            i = src.index(marker)
            seg = src[i : i + 320]
            assert "HTTPException(503" in seg
            assert "str(e)" not in seg, "内部异常原文不得回显"
        assert "BID_ANALYSIS_START_FAILED" in src
        assert "BID_ANALYSIS_SSE_START_FAILED" in src

    def test_outline_progress_done_total_single_source(self):
        src = _src("backend/app/routers/sse_handlers.py")
        i = src.index("**_snapshot_outline_stats(_prog)")
        seg = src[max(0, i - 600) : i]
        assert '"done": done,' not in seg, "被快照二次覆盖的字面量不得回归"
        assert '"total": _total,' not in seg
        assert '_snapshot_outline_stats' in src

    def test_auto_confirm_failure_returns_visible_blocker(self):
        src = _src("backend/app/routers/sse_handlers.py")
        assert src.count('"code": "evaluate_failed"') == 2, "短/长两处 except 都要回传 blocker"
        labels = _src("frontend/src/pages/schemeWorkbench/outlineAutoConfirm.ts")
        assert "evaluate_failed:" in labels, "前端必须有该 blocker 的中文标签"

    def test_terminal_stats_failures_are_loud(self):
        src = _src("backend/app/routers/_sse_content_payloads.py")
        for key in ("word_audit_error", "order_check_error", "fact_injection_summary_error"):
            assert f'"{key}"' in src
        assert src.count("计算失败（task=%s）") >= 3, "三处统计异常都必须留 WARNING"

    def test_r205_checkpoint_whitelist_not_regressed(self):
        from app.services.checkpoint_schema import CHECKPOINT_KINDS

        fields = CHECKPOINT_KINDS["content_generation"][1]
        assert {"processed", "charts", "fact_mapping"} <= set(fields)
        assert {"order_check", "fact_injection_summary", "word_audit"} <= set(fields)


class TestCrossEndContractR208:
    """前后端字段契约双向锁（后端侧读前端源码）。"""

    def test_parse_status_and_delay_ms_contracts(self):
        facts_ts = _src("frontend/src/types/facts.ts")
        assert "parse_status: string;" in facts_ts
        api_ts = _src("frontend/src/api/index.ts")
        assert "delay_ms: number;" in api_ts
        assert "signal?: AbortSignal," in api_ts, "评分项抽取 signal 透传不得回归"
        assert '"AbortError" : "TimeoutError"' in api_ts, "sseGetStream 建连取消 race 修复不得回归"

    def test_bid_stats_and_word_deviation_fields_declared(self):
        bid_types = _src("frontend/src/utils/bidAnalysis.ts")
        events = _src("frontend/src/pages/schemeWorkbench/useBidAnalysis.tsx")
        for key in ("section_title", "section_hint_applied", "multi_section_unselected"):
            assert key in bid_types and key in events
        assert "未选择标段" in events
        content_events = _src("frontend/src/utils/contentEvents.ts")
        wb_types = _src("frontend/src/pages/schemeWorkbench/workbenchTypes.ts")
        for key in ("word_deviation", "word_ratio", "word_within_band"):
            assert key in content_events and key in wb_types
        log_list = _src("frontend/src/pages/schemeWorkbench/SectionLogList.tsx")
        assert "容差外" in log_list and "容差内" in log_list

    def test_dead_branch_and_error_toasts_cleaned(self):
        facts_hook = _src("frontend/src/pages/schemeWorkbench/useFactsGeneration.ts")
        assert 'evt.event === "progress" || evt.event === "connecting"' not in facts_hook
        prompt_page = _src("frontend/src/pages/PromptEditorPage.tsx")
        assert prompt_page.count("promptErrorText(e") >= 3
        assert ".catch(() => {});" not in prompt_page
        recovery = _src("frontend/src/pages/schemeWorkbench/useUnknownTaskRecovery.tsx")
        assert "err?.message" in recovery

    def test_extract_long_task_guard_wired(self):
        panel = _src("frontend/src/pages/schemeWorkbench/tabs/ScoringCoveragePanel.tsx")
        for token in ("extractSeqRef", "extractAbortRef", "ac.signal", "extractAbortRef.current?.abort()"):
            assert token in panel
        assert "scoringApi.extract(projectId, undefined, ac.signal)" in panel
