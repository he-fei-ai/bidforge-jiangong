"""R205 · 六模块跨模块数据链收口护栏（2026-11-09）。

全部为行为级 / 单一事实源 parity / 跨端源码棘轮，覆盖本轮 6 项后端修复：
  B1 GET /api/v1/system/governance 注册（前端 systemApi.getGovernance 唯一出口）
  B2 checkpoint content_result 白名单 processed/charts/fact_mapping + 生成 TS parity
  B3 导出预检成功计数与解析模块 is_missing_result 同源（失败哨兵不得计入）
  B4 附录 extract_data JSON 格式化（不再把半截 JSON 当正文）
  B5/B6 目录 / 事实链 stopped/error 载荷 message 同构
  跨端：前端 degraded 消费 / loadFacts / word_adjust / warnings / cache status
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestGovernanceGetRoute:
    """B1：治理开关读取端点必须与前端调用路径一致。"""

    def test_get_governance_is_registered(self):
        from app.routers import system

        hits = [
            r
            for r in system.router.routes
            if getattr(r, "path", "") == "/api/v1/system/governance"
            and "GET" in getattr(r, "methods", set())
        ]
        assert hits, (
            "GET /api/v1/system/governance 未注册：前端 systemApi.getGovernance 会 404，"
            "治理面板静默显示默认态并在下次保存时覆盖真实配置"
        )

    async def test_governance_get_reads_persisted_values(self, monkeypatch):
        from app.routers import system

        async def _fake_load():
            return {"prompt_context_budget": "12345", "prompt_injection_defense": "true"}

        monkeypatch.setattr(system, "_governance_kv_load", _fake_load)
        assert await system.governance_get() == {
            "prompt_context_budget": 12345,
            "prompt_injection_defense": True,
        }

    def test_frontend_calls_same_path(self):
        assert 'api.get<GovernanceSettings>("/system/governance")' in _src("frontend/src/api/index.ts")


class TestCheckpointWhitelist:
    """B2：断线重挂 content_result 不得再丢配图 / 已处理章数 / 章节事实映射。"""

    def test_three_keys_in_single_source(self):
        from app.services.checkpoint_schema import CHECKPOINT_FIELD_TYPES, CHECKPOINT_KINDS

        kind, fields = CHECKPOINT_KINDS["content_generation"]
        assert kind == "content_result"
        assert {"processed", "charts", "fact_mapping"} <= set(fields)
        types = CHECKPOINT_FIELD_TYPES["content_result"]
        assert types["processed"] == "int"
        assert types["charts"] == "int"
        assert types["fact_mapping"] == "list[dict]"

    def test_attach_checkpoint_result_keeps_new_keys_and_still_filters(self):
        from app.routers import sse_handlers as sh

        mapping = [{"section_id": "s1", "facts": ["f1"]}]
        payload = {
            "kind": "content_result",
            "event": "completed",
            "done": 3,
            "total": 3,
            "processed": 4,
            "charts": 2,
            "fact_mapping": mapping,
            "words": 1200,
            "internal_secret": "must-not-leak",
        }
        row = {
            "id": "t1",
            "task_type": "content_generation",
            "checkpoint_json": json.dumps(payload, ensure_ascii=False),
        }
        result: dict = {}
        sh._attach_checkpoint_result(row, result)
        ck = result["content_result"]
        assert ck["processed"] == 4
        assert ck["charts"] == 2
        assert ck["fact_mapping"] == mapping
        assert "internal_secret" not in ck, "白名单语义不得被放宽为整体透传"

    def test_generated_ts_contract_in_sync(self):
        from app.services.checkpoint_schema import generate_typescript_schema

        ts = _src("frontend/src/types/checkpointSchema.ts")
        assert generate_typescript_schema().strip() == ts.strip(), (
            "TS 契约须由 backend/tools/generate_checkpoint_schema.py 重新生成"
        )
        assert "processed?: number;" in ts
        assert "charts?: number;" in ts
        assert "fact_mapping?: unknown[];" in ts


class TestExportPreflightCounting:
    """B3：解析→导出，status=success 的空壳哨兵不得计入成功数。"""

    def test_failure_sentinel_not_counted(self):
        from app.routers.export import _ba_result_has_content

        assert _ba_result_has_content(
            {"status": "success", "content": '{"工期":"120天"}', "output_type": "json"}
        )
        assert not _ba_result_has_content({"status": "success", "content": "{}", "output_type": "json"})
        assert not _ba_result_has_content(
            {"status": "success", "content": "未提取到", "output_type": "markdown"}
        )
        assert not _ba_result_has_content(
            {"status": "failed", "content": "正常正文", "output_type": "markdown"}
        )
        assert not _ba_result_has_content({"status": "success", "content": "", "output_type": "json"})

    def test_select_and_count_wired(self):
        src = _src("backend/app/routers/export.py")
        assert "content, output_type FROM bid_analysis_items" in src
        assert "sum(1 for r in ba_required if _ba_result_has_content(r))" in src


class TestAppendixExtractFormatting:
    """B4：解析层 JSON 对象字符串不得再被硬截 500 字当附录正文。"""

    def test_json_object_is_flattened(self):
        from app.routers.export import _format_extract_data_for_appendix

        raw = json.dumps(
            {
                "doc_id": "d1",
                "extract_schema_version": 1,
                "global_facts": [
                    {"title": "工期", "content": "120 天"},
                    {"title": "质量", "content": "合格"},
                ],
                "source_items": ["a", "b"],
            },
            ensure_ascii=False,
        )
        out = _format_extract_data_for_appendix(raw)
        assert "doc_id" not in out
        assert "extract_schema_version" not in out
        assert "工期：120 天" in out
        assert "质量：合格" in out
        assert not out.startswith("{"), "不得再把 JSON 原文印进附录"
        assert '"' not in out

    def test_plain_text_fallback_and_empty(self):
        from app.routers.export import _format_extract_data_for_appendix

        assert _format_extract_data_for_appendix("纯文本要点") == "纯文本要点"
        assert _format_extract_data_for_appendix("") == ""
        assert _format_extract_data_for_appendix(None) == ""

    def test_limit_is_visible(self):
        from app.routers.export import _format_extract_data_for_appendix

        out = _format_extract_data_for_appendix(
            json.dumps({"k": "x" * 5000}, ensure_ascii=False), limit=300
        )
        assert len(out) < 400
        assert "已截断" in out

    def test_load_appendix_uses_formatter(self):
        src = _src("backend/app/routers/export.py")
        assert '_format_extract_data_for_appendix(d.get("extract_data") or "")' in src
        assert 'str(d.get("extract_data") or "")[:500]' not in src


class TestTerminalPayloadParity:
    """B5/B6：目录与事实链终态载荷必须带 message（前端统一层只读 message）。"""

    def test_facts_stopped_and_error_payload(self):
        src = _src("backend/app/routers/sse_handlers.py")
        assert '"event": "stopped", "task_id": task_id, "message": "用户已停止"' in src
        assert '"failed_count": 0,' in src

    def test_outline_stopped_payload(self):
        src = _src("backend/app/routers/_sse_outline_review.py")
        assert '{"event": "stopped", "task_id": task_id, "message": "用户已停止"}' in src
        assert 'payload["message"] = "用户已停止（检查点写入失败' in src


class TestFrontendSyncLocks:
    """前后端同步：修复的消费点必须留在前端源码，防后续被「顺手优化」删掉。"""

    def test_outline_degraded_consumed(self):
        src = _src("frontend/src/pages/schemeWorkbench/useOutlineGeneration.tsx")
        assert 'evt.event === "degraded"' in src
        assert 'evt.event === "completed" || _isDegradedOutline' in src
        assert 'setProgressMsg(evt.message || "已停止")' in src

    def test_facts_terminal_refresh(self):
        src = _src("frontend/src/pages/schemeWorkbench/useFactsGeneration.ts")
        assert src.count("loadFacts();") >= 3, "completed / stopped / error 三条终态都要刷新事实列表"

    def test_page_consumes_warnings_and_word_adjust(self):
        src = _src("frontend/src/pages/SchemeWorkbenchPage.tsx")
        assert "_warnParts" in src
        assert "word_adjust_start" in src
        assert "word_adjust_done" in src
        assert "_errStdHint" in src
        assert "_errFaHint" in src

    def test_cache_status_consumed(self):
        assert "parseCacheStatus(resp.headers)" in _src(
            "frontend/src/pages/schemeWorkbench/useExportRun.ts"
        )
        assert "X-Cache-Status" in _src("frontend/src/utils/exportResponse.ts")

    def test_checkpoint_contract_has_three_keys(self):
        src = _src("frontend/src/types/checkpointSchema.ts")
        assert "processed?: number;" in src
        assert "charts?: number;" in src
        assert "fact_mapping?: unknown[];" in src
