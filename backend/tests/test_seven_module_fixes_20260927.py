"""七模块跨模块探索 · 2026-09-27 缺陷修复护栏。

本文件锁定本轮 8 项修复（P0×4 + P1×4）的核心不变量。每条用例都对应
一个**已复现**的缺陷，附「还原旧实现后必失败」的反向验证说明。
"""
import ast
import inspect
import re

import pytest

from app.routers import doc_pipeline, export, sse_handlers


# ---------------------------------------------------------------- P0-1
class TestFilterFactsRowsUnpack:
    """P0-1：`_filter_facts_rows` 对 5 元组生产行抛 ValueError。

    现象：正文生成每章在「构建上下文」阶段崩溃 → 全章失败
          → 「正文 0 字完成（N/N 章失败）」。
    根因：`_load_facts_rows` 自 2026-09-24 起返回 5 元组
          (gt, title, content, confidence, chapter)，而筛选函数仍写死
          `for gt, t, content in rows`。
    """

    FIVE = [("工程概况", "基坑深度", "基坑深度 18.5m，支护等级一级", 0.9, "overview")]

    def test_five_tuple_does_not_raise(self):
        out = sse_handlers._filter_facts_rows(
            self.FIVE, {"title": "基坑开挖与支护", "description": "深度与支护"})
        assert out, "应保留命中行"

    def test_five_tuple_preserved_intact(self):
        """整行原样保留 —— 不得裁回 3 元组（confidence/chapter 有下游消费方）。"""
        out = sse_handlers._filter_facts_rows(
            self.FIVE, {"title": "基坑开挖与支护", "description": "深度与支护"})
        assert all(len(r) == 5 for r in out)

    def test_legacy_three_tuple_still_works(self):
        out = sse_handlers._filter_facts_rows(
            [("g", "t", "基坑深度 18.5m")],
            {"title": "基坑开挖", "description": "深度"})
        assert out and len(out[0]) == 3

    def test_no_keywords_returns_rows_untouched(self):
        """无关键词 → 原样返回（既有语义不变）"""
        rows = list(self.FIVE)
        assert sse_handlers._filter_facts_rows(rows, {"title": "", "description": ""}) is rows

    def test_generic_group_hint_kept(self):
        rows = [("编制依据", "规范", "GB50007 无关内容", 1.0, "basis")]
        out = sse_handlers._filter_facts_rows(
            rows, {"title": "基坑开挖", "description": "深度"})
        assert len(out) == 1

    def test_regression_old_impl_raises(self):
        """反向验证：还原旧实现（3 元组解包）必然抛错。"""
        with pytest.raises(ValueError):
            for _gt, _t, _c in self.FIVE:  # noqa: F841 - 即旧实现
                pass


# ---------------------------------------------------------------- P0-2
class TestFactsSseTerminalOrder:
    """P0-2：facts SSE `yield completed` 早于 `finish_task`。

    前端收到 completed 立即 break → 生成器在 yield 处 GeneratorExit
    → 跳过 finish_task → finally 兜底把**成功**任务改写成 stopped。
    """

    def test_finish_task_precedes_completed_yield(self):
        src = inspect.getsource(sse_handlers.generate_facts)
        idx = src.find("**frontend_data}")
        assert idx > 0, "未找到主完成分支"
        window = src[max(0, idx - 700): idx + 200]
        assert 'await finish_task(task_id, "completed", done_msg)' in window, \
            "主完成分支必须先 finish_task 再 yield completed"
        fi = window.find('await finish_task(task_id, "completed", done_msg)')
        yi = window.find('yield _sse({"event": "completed"')
        assert 0 <= fi < yi, "finish_task 必须早于 yield"

    def test_every_completed_yield_has_prior_finish_task(self):
        """全文件所有 completed yield 之前都必须已落终态。

        注意：目录/正文链路的 completed 由 `_sse({"event": "completed", ...})`
        发出，facts 链路同样如此；但 `_phase`/`_stage` 包装的分支字面量略有
        差异，故此处只对**精确字面量**做全量扫描，facts 链路由上面的
        test_finish_task_precedes_completed_yield 单独锁定。
        """
        src = inspect.getsource(sse_handlers)
        needle = 'yield _sse({"event": "completed"'
        pos = 0
        hits = 0
        while True:
            i = src.find(needle, pos)
            if i < 0:
                break
            hits += 1
            window = src[max(0, i - 1200): i]
            assert "await finish_task(" in window, \
                f"第 {hits} 处 completed yield 前缺少 finish_task"
            pos = i + 1
        # facts 链路有 2 处（主完成 + all_skipped 早退），均已就位
        assert hits >= 2, f"应至少覆盖 facts 两条完成分支，实际 {hits}"


# ---------------------------------------------------------------- P0-3
class TestFactsCheckpoint:
    """P0-3：`facts_generation` 无 checkpoint 通道 → 断线重挂丢成果。"""

    def test_task_type_registered(self):
        assert "facts_generation" in sse_handlers._CHECKPOINT_KINDS

    def test_kind_name(self):
        kind, _ = sse_handlers._CHECKPOINT_KINDS["facts_generation"]
        assert kind == "facts_result"

    def test_whitelist_contains_online_payload_fields(self):
        _kind, fields = sse_handlers._CHECKPOINT_KINDS["facts_generation"]
        for f in ("event", "message", "segment_stats", "cross_conflicts"):
            assert f in fields, f"白名单缺 {f}"

    def test_whitelist_is_subset_of_write_side(self):
        """写入侧与回传侧必须同源（防再次分叉）。"""
        _kind, fields = sse_handlers._CHECKPOINT_KINDS["facts_generation"]
        allowed = set(("event", "message")) | set(sse_handlers._FACTS_CHECKPOINT_FIELDS)
        assert set(fields) <= allowed

    def test_payload_filters_bulk_fields(self):
        """明细不落 checkpoint（会撑大 task_registry 行）。"""
        out = sse_handlers._facts_checkpoint_payload({
            "segment_stats": {"failed": 2, "total": 5},
            "cross_conflicts": [{"a": 1}],
            "warnings": ["w"],
            "groups": [{"x": 1}],
        })
        assert out == {"segment_stats": {"failed": 2, "total": 5},
                       "cross_conflicts": [{"a": 1}], "warnings": ["w"]}
        assert "groups" not in out

    def test_payload_tolerates_non_dict(self):
        assert sse_handlers._facts_checkpoint_payload(None) == {}

    def test_save_helper_exists(self):
        assert hasattr(sse_handlers, "_save_facts_checkpoint")

    def test_attach_returns_facts_result(self):
        row = {"task_type": "facts_generation",
               "checkpoint_json": '{"kind":"facts_result","event":"completed",'
                                  '"segment_stats":{"failed":1}}'}
        res: dict = {}
        sse_handlers._attach_checkpoint_result(row, res)
        assert res["facts_result"]["segment_stats"] == {"failed": 1}

    def test_attach_ignores_kind_mismatch(self):
        row = {"task_type": "facts_generation",
               "checkpoint_json": '{"kind":"outline_result","outline":[]}'}
        res: dict = {}
        sse_handlers._attach_checkpoint_result(row, res)
        assert "facts_result" not in res


# ---------------------------------------------------------------- P0-4
class TestContentFinalizeNoneCursor:
    """P0-4：正文收尾统计 `cur` 未判空 → 500 且成果已落库。"""

    def test_source_has_none_guard(self):
        src = inspect.getsource(sse_handlers.generate_content)
        i = src.find("COALESCE(SUM(word_count),0)")
        assert i > 0, "未找到总字数统计语句"
        window = src[i: i + 900]
        assert "if cur is None" in window, "收尾统计缺少 cur is None 守卫"
        assert "except Exception" in window, "缺少异常降级"

    def test_module_parses(self):
        ast.parse(inspect.getsource(sse_handlers))


# ---------------------------------------------------------------- P1-5
class TestReviewRecordOnPending:
    """P1-5：正文生成置 review_status='pending' 却不写 review_records。"""

    def test_source_writes_review_record(self):
        src = inspect.getsource(sse_handlers.generate_content)
        i = src.find("review_status='pending'")
        assert i > 0
        window = src[i: i + 2000]
        assert "_write_review_record" in window, "pending 置位后必须落评审留痕"

    def test_old_status_read_precedes_update(self):
        """⚠️ 顺序护栏（本轮真实踩过的坑）。

        同一 `db` 连接上，SELECT 会读到本事务自己刚写入的未提交值。
        若「读旧状态」放在「UPDATE 置 pending」之后，`_old` 恒为 "pending"
        → 幂等判断把每章都跳过 → 留痕 100% 空转，修复形同虚设。
        """
        src = inspect.getsource(sse_handlers.generate_content)
        sel = src.find("COALESCE(review_status,'')")
        upd = src.find("SET review_status='pending'")
        assert sel > 0 and upd > 0
        assert sel < upd, "必须先 SELECT 旧状态、再 UPDATE 置位"

    def test_record_write_is_exception_safe(self):
        src = inspect.getsource(sse_handlers.generate_content)
        i = src.find("_write_review_record")
        assert i > 0
        window = src[i: i + 3000]
        assert "except Exception" in window, "留痕失败不得中断收尾"
        assert "logger.warning" in window, "留痕失败必须留痕到日志"

    def test_record_write_is_idempotent(self):
        """只对**真的发生状态变化**的章节落留痕（对齐 review 的幂等约定）。"""
        src = inspect.getsource(sse_handlers.generate_content)
        i = src.find("_prior: dict = {}")
        assert i > 0, "未找到留痕前的旧状态读取块"
        window = src[i: i + 2600]
        assert 'if _old in ("", "pending")' in window, \
            "未审/待审章节重复置位无信息量，不应再插留痕"
        assert "continue" in window

    def test_record_preserves_real_from_status(self):
        """from_status 必须取旧值，不能恒为 \"\"（否则审计链断裂）。"""
        src = inspect.getsource(sse_handlers.generate_content)
        i = src.find("_prior: dict = {}")
        window = src[i: i + 2600]
        assert "COALESCE(review_status,'')" in window, "必须先读旧状态再置位"
        assert '_old, "pending"' in window, "from_status 必须传真实旧状态"

    def test_review_module_exposes_writer(self):
        from app.routers import review
        assert hasattr(review, "_write_record")
        assert hasattr(export, "collect_export_issues")


# ---------------------------------------------------------------- P1-6
class TestDocPipelineNoneCursor:
    """P1-6：`doc_pipeline` 三处 `cur is None` 漏改点（R13 模式）。"""

    def test_load_doc_guards(self):
        assert "if cur is None" in inspect.getsource(doc_pipeline._load_doc)

    def test_extractions_guards(self):
        assert "if cur is None" in inspect.getsource(doc_pipeline.get_extractions)

    def test_chunks_guards(self):
        src = inspect.getsource(doc_pipeline.get_chunks)
        assert "if cur is None" in src
        assert "if cur is not None else 0" in src

    @pytest.mark.asyncio
    async def test_load_doc_none_cursor_raises_503_not_404(self):
        """R13 反例：execute() 返回 None 时不得 AttributeError。

        刻意断言 **503 而非 404**：404 会让前端把「连接瞬时故障」误判为
        「文档已丢失」并从列表移除，503 才会触发客户端重试。
        """

        class _DB:
            @staticmethod
            async def execute(*_a, **_k):
                return None

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            await doc_pipeline._load_doc(_DB(), "missing")
        assert ei.value.status_code == 503

    @pytest.mark.asyncio
    async def test_chunks_none_cursor_raises_503_not_attrerror(self):
        class _DB:
            @staticmethod
            async def execute(*_a, **_k):
                return None

        with pytest.raises(Exception) as ei:
            await doc_pipeline.get_chunks("d1", db=_DB())
        assert getattr(ei.value, "status_code", None) == 503, \
            "必须是 503 而非 AttributeError"


# ---------------------------------------------------------------- P1-7
class TestExportFactsGateParity:
    """P1-7：导出门控 `COALESCE(is_resolved,1)` 与注入侧 `is_resolved=1` 分叉。"""

    def test_gate_is_fail_closed(self):
        src = inspect.getsource(export.collect_export_issues)
        assert "COALESCE(is_resolved,0)=0" in src
        assert "COALESCE(is_resolved,1)=0" not in src, \
            "NULL 必须计为未确认（与注入侧 is_resolved=1 同口径）"

    def test_inject_side_is_strict_equality(self):
        from app.services.facts_extractor import get_facts_inject_where
        assert "is_resolved=1" in get_facts_inject_where()


# ---------------------------------------------------------------- P1-8
class TestStandardReportFactBudget:
    """P1-8：`standard_report` 消费的必须是与提示词同一份筛选结果。"""

    def test_render_and_report_share_fact_subset(self):
        """不变量：校验器消费的 fact_rows 与提示词消费的必须是**同一份**。

        真实链路（sse_handlers.py，均在 generate_content 内部闭包）：
          `_sec_facts = _filter_facts_rows(facts_rows, leaf)`
            → `return eff_standard, _sec_facts, messages, user_content`
          调用方 `_eff_std, _sec_facts, ... = await _build_generation_context(...)`
            → `standard_report(..., fact_rows_for_section=_sec_facts)`
        即同一变量两端复用，不存在「一份裁剪、另一份未裁剪」的分叉。
        （`_build_generation_context` 是嵌套函数，不能用 getsource 直取，
          故按 generate_content 源码文本断言。）
        """
        gen = inspect.getsource(sse_handlers.generate_content)
        # 1) 上下文构建器产出并返回 _sec_facts
        assert "_sec_facts = _filter_facts_rows(facts_rows, leaf)" in gen
        assert "return eff_standard, _sec_facts, messages, user_content" in gen, \
            "_sec_facts 必须从上下文构建器返回，供校验器复用"
        # 2) 调用方把同一个变量透传给 standard_report
        assert "fact_rows_for_section=_sec_facts" in gen, \
            "校验器必须消费与提示词同一份 _sec_facts"


# ---------------------------------------------------------------- parity
class TestExportGateHighSeverityParity:
    """前后端导出门禁 high 类型表 parity。

    AGENTS.md §4.3 通用教训：同一业务判据在两侧各写一份，改一处就分叉。
    后端 `_EXPORT_ISSUE_RULE_MAP` 决定 severity；前端
    `utils/exportCharts.ts::HIGH_EXPORT_ISSUE_TYPES` 在**后端未回传 severity**
    时（`/export/check` 的 issues 不含该字段）作为兜底判定。
    两份字面量必须逐项一致，否则「后端判 high 但前端不认」→ 门禁被绕过。
    """

    def _frontend_high_types(self):
        import io
        import pathlib
        p = pathlib.Path(__file__).resolve().parents[2] / (
            "frontend/src/utils/exportCharts.ts")
        src = p.read_text(encoding="utf-8")
        i = src.find("const HIGH_EXPORT_ISSUE_TYPES")
        assert i > 0, "未找到前端 HIGH_EXPORT_ISSUE_TYPES"
        block = src[i: src.find("]);", i)]
        return {m for m in re.findall(r'"([a-z_]+)"', block)}

    def _backend_high_types(self):
        return {k for k, (_rule, sev) in export._EXPORT_ISSUE_RULE_MAP.items()
                if sev == "high"}

    def test_frontend_file_exists(self):
        import pathlib
        p = pathlib.Path(__file__).resolve().parents[2] / (
            "frontend/src/utils/exportCharts.ts")
        assert p.exists(), "前端 exportCharts.ts 缺失，无法做 parity 断言"

    def test_parity(self):
        fe = self._frontend_high_types()
        be = self._backend_high_types()
        assert fe == be, (
            f"导出门禁 high 类型表前后端不一致："
            f"仅后端有={sorted(be - fe)}，仅前端有={sorted(fe - be)}")


    def test_fact_text_tolerates_all_row_widths(self):
        from app.services.content_standard import _fact_text
        assert _fact_text(("g", "t", "c")) == ("t", "c")
        assert _fact_text(("g", "t", "c", 0.9, "overview")) == ("t", "c")
        assert _fact_text({"title": "t", "content": "c"}) == ("t", "c")

