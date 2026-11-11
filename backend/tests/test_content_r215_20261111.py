"""R215 · 正文生成模块深度审计护栏（2026-11-11）。

覆盖本轮 21 项落地修复（另 3 项登记见报告 §遗留）：
- A1  CancelledError 穿透锁内事务兜底（P1）—— except BaseException 回滚棘轮
- A2  回滚链丢弃 resync 校正后正文（P2）—— rollback_snapshot 消费返回值 + N1 回退落库
- A3  numeric_lock 作用域路径把 undetermined 句整句删除（P2）—— 行为实证
- A4  numeric_lock enforce/check 逐句循环围栏掩码失效（P2）—— 行为实证（scoped + 非 scoped）
- B1  正文链预算逐条全量套用 3×（P2）—— R204 helper 零调用激活
- B2  paused_ms 三处未接线（P2）—— 暂停期间耗时照涨
- B3  db_written 窗口 done/failed 双集合双计（P3）
- B4  checkpoint failed_count 与在线终态口径分叉（P3）
- C1  fix-mermaid / generate-ai-image 改正文后不退审核 + 缓存不失效（P3）
- C2  _rewrite_code_block 未命中块归一化改写（P3）
- C3  image 块下方「图 X-Y 题名」被当残留删除（P3）
- C4  min_words 读净化前字数（P3）
- C5  generate-ai-image 按类型去重枚举（P3）
- D6  build_sibling_context 前序摘要不剔围栏（P3）
- D8  _adjust_one 装配降级事件不 flush（P3）
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from app.routers._chart_pipeline import (
    _rewrite_code_block,
    _scan_chart_fences_full,
    build_inline_chart_plan,
)
from app.routers._sse_content_payloads import build_content_checkpoint
from app.services.content_blocks import (
    _attach_trailing_figure_captions,
    _drop_dangling_figure_captions,
)
from app.services.content_utils import build_sibling_context
from app.services.numeric_lock import (
    build_allowed_numbers,
    build_scoped_allowed_numbers,
    check_text,
    enforce_text,
)

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"


def _read(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A3 + A4 · numeric_lock 三态语义与围栏掩码（行为级）
# ---------------------------------------------------------------------------


class TestNumericLockScoped:
    """作用域（ScopedAllowedNumbers）路径的三态语义。"""

    SCOPED = build_scoped_allowed_numbers(
        clause_items=(("uid1", "条款5.2.1", ("5.2.1", "第5.2.1条"), "允许偏差 30mm"),),
        fact_texts=("12.5m",),
    )

    def test_undetermined_sentence_must_survive(self):
        """✅ A3：未绑定条款的合法句只记账，不删正文（旧实现整句删除 removed=2）。"""
        text = "基坑开挖深度为 12.5m。第5.2.1条允许偏差 50mm。采用挖掘机 2 台进场。"
        new_text, res = enforce_text(text, self.SCOPED)
        assert "采用挖掘机 2 台进场。" in new_text
        assert "第5.2.1条允许偏差 50mm。" not in new_text
        assert res.removed_segments == 1
        assert len(res.undetermined) == 1

    def test_violation_still_blocked(self):
        """修复不得放走真违规（引用条款但数值越界的句仍被删）。"""
        new_text, res = enforce_text("第5.2.1条垫层厚度偏差 50mm。", self.SCOPED)
        assert "50mm" not in new_text
        assert res.removed_segments >= 1
        assert len(res.violations) == 1

    def test_check_text_bucket_semantics(self):
        """check_text 的分桶语义不变：越界句进 violations、未绑定句进 undetermined。"""
        res = check_text("第5.2.1条偏差 50mm。采用挖掘机 2 台进场。", self.SCOPED)
        assert len(res.violations) == 1 and len(res.undetermined) == 1

    @pytest.mark.parametrize("allowed", ["scoped", "plain"], ids=["scoped", "non-scoped"])
    def test_fence_code_never_deleted(self, allowed):
        """✅ A4：围栏内代码行不参与逐句比对 —— 同章存在真违规触发重建循环时，
        mermaid 代码必须原样保留（旧实现被掏空只剩围栏壳，removed=6/4）。"""
        text = (
            "施工总体安排如下。\n\n```mermaid\ngantt\n"
            "    基坑支护 :task1, 0, 14d\n    主体结构 :task2, 14, 30d\n```\n\n"
            "第5.2.1条垫层厚度偏差 50mm。\n"
        )
        allowed_set = (
            self.SCOPED
            if allowed == "scoped"
            else build_allowed_numbers(("12.5m", "30mm", "14d", "30d", "0", "14"))
        )
        new_text, res = enforce_text(text, allowed_set)
        assert "基坑支护 :task1, 0, 14d" in new_text
        assert "主体结构 :task2, 14, 30d" in new_text
        assert "50mm" not in new_text
        if allowed == "scoped":
            # 围栏内数值不再污染 undetermined 报告
            assert len(res.undetermined) == 0

    def test_scoped_undetermined_report_not_polluted_by_fence(self):
        """check_text（scoped）不再把围栏内数值记入 undetermined。"""
        res = check_text("```mermaid\ngantt\n    基坑支护 :task1, 0, 14d\n```", self.SCOPED)
        assert len(res.undetermined) == 0


# ---------------------------------------------------------------------------
# C2 · _rewrite_code_block 未命中块保真（行为级）
# ---------------------------------------------------------------------------


class TestRewriteCodeBlockFidelity:
    """✅ C2：只动目标块；无关围栏（含普通代码块）逐字保真。"""

    MIXED = (
        "```mermaid\nflowchart TD\n  A-->B\n```\n\n"
        "```python\nx=1\n\n\ny=2\n```\n"
    )

    def test_unrelated_block_byte_identical(self):
        out = _rewrite_code_block(self.MIXED, "flowchart TD\n  A-->B", "flowchart TD\n  A-->B\n  C-->D")
        # python 块连同块内空行、闭合围栏逐字保留（旧实现剥空行 + 归一化重写）
        assert "```python\nx=1\n\n\ny=2\n```" in out
        assert "C-->D" in out

    def test_same_code_blocks_all_replaced(self):
        """同码多块全部替换是刻意保留：同样的坏码修出的就是同一好码。"""
        two = "```mermaid\nflowchart TD\n  A-->B\n```\n\n```mermaid\nflowchart TD\n  A-->B\n```\n"
        out = _rewrite_code_block(two, "flowchart TD\n  A-->B", "NEW")
        assert out.count("NEW") == 2

    def test_no_hit_returns_content_unchanged(self):
        assert _rewrite_code_block(self.MIXED, "nonexistent-code", "NEW") == self.MIXED


# ---------------------------------------------------------------------------
# C3 · image 块下方题注升格（行为级）
# ---------------------------------------------------------------------------


class TestImageTrailingCaption:
    def test_image_below_caption_absorbed(self):
        """✅ C3：GB/T 图题在图下方 —— image 块下方的「图 X-Y 题名」写入 alt，
        不再流到 _drop_dangling_figure_captions 被整段删除。"""
        blocks = [
            {"type": "image", "alt": "", "url": "u"},
            {"type": "paragraph", "text": "图 3-1 基坑支护剖面示意"},
        ]
        out = _attach_trailing_figure_captions(blocks)
        assert out[0]["alt"] == "基坑支护剖面示意"
        assert len(out) == 1  # 题注段已升格移除
        assert not any(b.get("type") == "paragraph" for b in _drop_dangling_figure_captions(out))

    def test_chart_below_caption_regression(self):
        """chart 块下方题注（R179 G-08）不得回归：仍写 title。"""
        blocks = [
            {"type": "chart", "title": ""},
            {"type": "paragraph", "text": "图 5-2 施工总平面布置图"},
        ]
        out = _attach_trailing_figure_captions(blocks)
        assert out[0]["title"] == "施工总平面布置图"


# ---------------------------------------------------------------------------
# C4 · min_words 净化后计字（行为级）
# ---------------------------------------------------------------------------


class TestMinWordsSanitizedBasis:
    def test_footer_padding_no_longer_rescues_short_section(self):
        """✅ C4：短章 + 分页页脚杂质 —— 旧判据读净化前文本（195 字正文 + 页脚
        杂质 ≥ 200）保住图表；新判据与落库 word_count 同源（净化后 < 200 → 拦）。"""
        body = "基" * 195
        content = f"{body}\n第3页共12页\n\n```mermaid\nflowchart TD\n  A-->B\n```\n"
        _new_content, rows = build_inline_chart_plan("s-r215", "sec-r215", content)
        assert rows == []


# ---------------------------------------------------------------------------
# B4 · checkpoint failed_count 与在线终态同口径（行为级）
# ---------------------------------------------------------------------------


class TestCheckpointFailedCount:
    def _build(self, done_ids, failed_ids, total):
        return build_content_checkpoint(
            "stopped",
            "测试",
            ckpt={},
            task_id="t",
            total=total,
            done_ids=done_ids,
            failed_ids=failed_ids,
            failed_reasons={k: {"section_id": k, "title": "", "reason": "x"} for k in failed_ids},
            prog={},
            run_stats={},
            word_tol=0.1,
            fact_autofix_summary={},
            dispatched_order=[],
            expected_order=[],
            fact_audit_by_leaf={},
            order_warned={},
        )

    def test_count_uses_done_ids_basis(self):
        """✅ B4：失败计数 = total - done（与 _stopped_payload 同口径），
        不得用 len(failed_ids)（差额 = 用户停止/断线时未处理章节）。"""
        payload = self._build(done_ids={"a", "b", "c"}, failed_ids={"x"}, total=5)
        assert payload["failed_count"] == 2

    def test_all_done_zero_failed(self):
        payload = self._build(done_ids={"a", "b"}, failed_ids=set(), total=2)
        assert payload["failed_count"] == 0


# ---------------------------------------------------------------------------
# D6 · build_sibling_context 前序摘要剔围栏（行为级）
# ---------------------------------------------------------------------------


class TestSiblingSummaryStripsFence:
    def test_prev_summary_excludes_fenced_code(self):
        """✅ D6：上一章以 mermaid 块收尾时，前序摘要不得塞进图表源码。"""
        prev = {
            "id": "p1",
            "title": "第一章",
            "sort_order": 1,
            "content": "本章要点已述。\n\n```mermaid\nflowchart TD\n  A-->B\n```\n",
        }
        leaf = {"id": "p2", "title": "第二章", "sort_order": 2}
        _lines, prev_summary = build_sibling_context([prev], leaf, generated_contents={})
        assert "flowchart" not in prev_summary and "A-->B" not in prev_summary


# ---------------------------------------------------------------------------
# 棘轮 / 源码锁（防回流）
# ---------------------------------------------------------------------------


class TestSourceRatchets:
    def test_a1_persist_section_base_exception_rollback(self):
        """✅ A1 棘轮：_persist_section 锁内兜底与占位章锁内写必须捕 BaseException
        （CancelledError 是 BaseException，except Exception 捕不住 ⇒ 半途事务被
        同连接后续 commit 顺带提交，击穿 R108 不变量）。"""
        src = _read("routers/sse_handlers.py")
        assert src.count("except BaseException:") >= 2
        assert "BaseException，Python 3.8" in src  # 修复锚注释在场

    def test_a2_rollback_chain_consumes_fixed(self):
        """✅ A2 棘轮：三处回滚写点必须消费登记管线校正后正文。"""
        rr = _read("services/repair_record.py")
        assert "_resynced = await resync_fn(db, sid, restored_content)" in rr
        assert "rollback_snapshot:消费登记校正正文" in rr
        sec = _read("routers/sections.py")
        assert sec.count("_n, _fixed = await resync_inline_charts") >= 2
        assert "return _fixed" in sec  # _resync_rollback_charts 契约升级

    def test_b1_budget_helper_wired(self):
        """✅ B1 棘轮：R204 整份分配 helper 必须在生产链被调用；
        逐条全量套用形态不得回流。"""
        src = _read("routers/sse_handlers.py")
        assert "messages = _apply_prompt_context_budget_to_messages(messages)" in src
        assert "_apply_prompt_context_budget(_" not in src

    def test_b2_paused_ms_wired(self):
        """✅ B2 棘轮：三处统计快照必须接入暂停时长（B1 设计落地）。

        心跳通道（_stats_provider）经 _content_holder 晚注入 task_id（其定义处
        task_id 尚未生成），另两处（_push_stats / 收尾快照）直接用闭包 task_id。
        """
        src = _read("routers/sse_handlers.py")
        assert src.count("_snapshot_stats(_prog, get_task_paused_ms(task_id))") == 2
        assert "get_task_paused_ms(_tid) if _tid else 0.0" in src
        # 旧单参形态（paused_ms=0.0 恒默认）不得回流
        assert "_snapshot_stats(_prog)" not in src

    def test_b3_done_ids_dedup(self):
        """✅ B3 棘轮：失败计数与取消分支必须按 done_ids 去重。"""
        src = _read("routers/sse_handlers.py")
        assert src.count("if section_id in done_ids:") >= 1
        assert "leaf_id not in done_ids" in src

    def test_c1_chart_writeback_review_reset(self):
        """✅ C1 棘轮：图表两端点改正文后必须退审核 + 失效一致性缓存。"""
        src = _read("routers/charts.py")
        assert src.count("reset_review_on_content_change") >= 3  # import + 两端点
        assert src.count("invalidate_consistency_scan_cache") >= 2
        assert 'actor="图表修复"' in src and 'actor="AI 配图"' in src

    def test_c5_ai_image_full_scan(self):
        """✅ C5 棘轮：generate-ai-image 必须用全量块扫描枚举（按类型去重口径
        会让同章第二个 ai_image 块永不生图）。"""
        src = _read("routers/charts.py")
        assert "_scan_chart_fences_full(content)" in src

    def test_d8_adjust_one_flushes_degraded(self):
        """✅ D8 棘轮：字数批调度两分支重建上下文后必须 flush 装配降级事件。"""
        src = _read("routers/sse_handlers.py")
        assert src.count("for _cd_event in _flush_context_degraded():") >= 4

    def test_c2_unclosed_fence_branch_untouched(self):
        """回归锚：eof/truncated 分支原样保留（R169 契约）不得被 C2 波及。"""
        tree = ast.parse(_read("routers/_chart_pipeline.py"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_rewrite_code_block")
        assert any(
            isinstance(n, ast.If) and any(
                isinstance(c, ast.Compare) and getattr(c.left, "id", None) == "state"
                for c in ast.walk(n.test)
            )
            for n in ast.walk(fn)
        )


# ---------------------------------------------------------------------------
# 字节体检（本轮触碰的 9 个后端源文件；护栏文件自身按 R208 先例用 LF）
# ---------------------------------------------------------------------------


class TestByteIntegrity:
    @pytest.mark.parametrize(
        "rel",
        [
            "routers/sse_handlers.py",
            "routers/sections.py",
            "routers/charts.py",
            "routers/_chart_pipeline.py",
            "routers/_sse_content_payloads.py",
            "services/repair_record.py",
            "services/numeric_lock.py",
            "services/content_blocks.py",
            "services/content_utils.py",
        ],
    )
    def test_touched_sources_crlf_no_bom_no_fffd(self, rel):
        raw = (APP / rel).read_bytes()
        crlf = raw.count(b"\r\n")
        bare_lf = raw.count(b"\n") - crlf
        assert bare_lf == 0, f"{rel} 存在裸 LF（本仓后端源规范 CRLF）"
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} 带 BOM"
        assert raw.count(b"\xef\xbf\xbd") == 0, f"{rel} 存在 U+FFFD"
