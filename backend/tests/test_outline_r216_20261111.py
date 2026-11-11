"""R216（2026-11-11）· 大纲/SSE 方向深度探索增强 —— 护栏。

覆盖范围（均基于活体代码或纯函数行为，不采信报告结论）：
- 编号全角归一 + 前导零折叠（纯函数行为）：防成稿双重编号、防数量型被剥坏、
  防存储编号展示分叉（preview≠成稿）。
- sections 兜底匹配去掉对 original_id 的真值要求（**P1 数据丢失**：模板实例化
  空 __original_id 节点此前被短路进 new_inserts ⇒ 实例化一次整批删旧章+已生成正文）。
- 标题清洗空闸门（create/update）+ 清洗前移到落库闸门之前（与目录库口同序）。
- 保存/套用双 409 守卫（写锁前复检，与入口守卫同口径）。
- unmounted 不可达节点统计收口为模块级唯一实现（读路径暴露缺章）。
- sse_handlers 输入指纹追加第 11 part（可注入事实行摘要）+ R217 修复 F821
  （`_facts_char_rows` 未定义名 → `_load_facts_rows`）；oneshot 收尾停止复检；
  holder 非空才覆盖；SSE-5 截断/复用信号（all_failed 出口 + error 事件）。
- _sse_outline_review 熔断后批量整单元 failed + 每次尝试前暂停/停止复检 +
  子目录阶段已熔断时审核链跳过（不白烧 AI 调用）。
- outline_library review_status 白名单 + 版本列表 LIMIT 50。
- schemes 级联清单与 projects._SCHEME_SCOPED_TABLES 收口为单一事实源。
- outline_versions 快照回退 status/outline_source 口径分叉修复。
- repair_record create_snapshot commit 语义交调用方（保 undo 快照原子性）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BACKEND_APP = REPO / "backend" / "app"


def _src(rel: str) -> str:
    return (BACKEND_APP / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A · 编号全角归一 + 前导零折叠（纯函数行为，R216 编号改动核心）
# ---------------------------------------------------------------------------


class TestNumberingFullwidthAndLeadingZero:
    def test_fullwidth_strip_no_double_numbering(self):
        """AI/上传输出全角编号必须被剥离，否则成稿双重编号（"1.2 １.２ 施工布置"）。"""
        from app.services.numbering import strip_outline_numbering

        assert strip_outline_numbering("１.２ 施工布置") == "施工布置"

    def test_fullwidth_decimal_quantity_protected(self):
        """全角小数数量（"２.５m 深"）不得被剥成 "５m 深"（数量型修复的全角复现）。"""
        from app.services.numbering import strip_outline_numbering

        assert strip_outline_numbering("２.５m 深") == "２.５m 深"

    def test_leading_zero_folded(self):
        """段内前导零折叠："1.01" → [1, 1]，与导出 int 渲染口径一致。"""
        from app.services.numbering import _stored_id_parts

        assert _stored_id_parts("1.01") == ["1", "1"]

    def test_fullwidth_normalized_in_stored_id(self):
        """存储编号全角数字/点 → ASCII 后再解析。"""
        from app.services.numbering import _stored_id_parts

        assert _stored_id_parts("１.２") == ["1", "2"]

    @pytest.mark.parametrize("bad", ["０.３", "0.3", "abc1.2", "1.a", ""])
    def test_stored_id_fail_closed(self, bad):
        """真垃圾（含 0 号段/非点分/类型不符）仍 fail-closed 返回 []。"""
        from app.services.numbering import _stored_id_parts

        assert _stored_id_parts(bad) == []

    def test_display_follows_folded(self):
        """展示编号随前导零折叠收敛（"1.01"→"1"，不再 "01"）。"""
        from app.services.numbering import stored_id_to_display

        assert stored_id_to_display("1.01") == "1"
        assert stored_id_to_display("0.3") == ""

    def test_pure_number_title_re_knows_fullwidth(self):
        """纯编号标题正则同步认全角（"１．２" 整体即编号 → 原样返回契约一致）。"""
        from app.services.numbering import _PURE_NUMBER_TITLE_RE

        assert bool(_PURE_NUMBER_TITLE_RE.fullmatch("１．２"))
        assert bool(_PURE_NUMBER_TITLE_RE.fullmatch("1.2"))

    def test_year_prefix_re_knows_fullwidth(self):
        """年份前缀正则同步认全角（"２０２３年施工计划" 同保护）。"""
        from app.services.numbering import _YEAR_PREFIX_RE

        assert bool(_YEAR_PREFIX_RE.search("２０２３年施工计划"))
        assert bool(_YEAR_PREFIX_RE.search("2023年施工计划"))


# ---------------------------------------------------------------------------
# A2 · 编号正则字面量棘轮（防全角支持被删、防负向断言回退 ASCII-only）
# ---------------------------------------------------------------------------


class TestNumberingRegexRatchet:
    def test_strip_number_re_includes_fullwidth(self):
        s = _src("services/numbering.py")
        # 数字类并入全角 ０-９；负向断言同步认全角（否则数量型全角复现）
        assert "０-９" in s
        assert "(?![0-9０-９])" in s

    def test_fullwidth_trans_defined(self):
        s = _src("services/numbering.py")
        assert "_FULLWIDTH_DIGITS_TRANS = str.maketrans(" in s


# ---------------------------------------------------------------------------
# B · sections P1 兜底匹配去掉对 original_id 的真值要求（数据丢失级）
# ---------------------------------------------------------------------------


class TestSectionsFallbackMatchNoTruthiness:
    def test_uses_original_id_or_empty(self):
        """修复核心：空串 __original_id 不再被 `original_id and ...` 短路。"""
        s = _src("routers/sections.py")
        assert 'not (original_id or "").startswith("local_")' in s

    def test_structural_matcher_invoked(self):
        """空串节点进入结构感知匹配器（而非落进 new_inserts 整批删旧章）。"""
        s = _src("routers/sections.py")
        assert "_outline_matcher.take(" in s

    def test_old_short_circuit_gone(self):
        """反证：旧 `if is_new and original_id and not original_id.startswith("local_")`
        代码形态不得回流（仅 R216 注释中描述旧条件，不含 `.startswith` 代码）。"""
        s = _src("routers/sections.py")
        assert "original_id and not original_id.startswith" not in s


# ---------------------------------------------------------------------------
# C · sections P2 标题清洗空闸门（create / update）
# ---------------------------------------------------------------------------


class TestSectionTitleEmptyGuard:
    def test_create_and_update_guard_present(self):
        s = _src("routers/sections.py")
        # 出现两次：create_section + update_section（与 save-outline 空标题闸门同口径）
        assert s.count("章节标题不能为空（不可见字符清洗后不得为空）") >= 2


# ---------------------------------------------------------------------------
# D · sections P2 标题清洗前移到落库闸门之前（与目录库口同序）
# ---------------------------------------------------------------------------


class TestTitleSanitizeBeforeGate:
    def test_sanitize_before_validate(self):
        s = _src("routers/sections.py")
        p_san = s.index("_sanitize_outline_titles(outline)")
        p_val = s.index("validate_outline_numbering(outline)")
        # 清洗（前移）必须在编号合法性闸门之前，闸门才能看到清洗后标题
        assert p_san < p_val


# ---------------------------------------------------------------------------
# E · sections / upload_outline P3 取锁前双 409 守卫
# ---------------------------------------------------------------------------


class TestSaveOutlineDouble409:
    def test_save_outline_guard(self):
        s = _src("routers/sections.py")
        assert "content_generation_in_progress(scheme_id)" in s
        assert "outline_generation_in_progress(scheme_id)" in s

    def test_upload_outline_guard(self):
        s = _src("routers/upload_outline.py")
        assert "content_generation_in_progress(scheme_id)" in s
        assert "outline_generation_in_progress(scheme_id)" in s


# ---------------------------------------------------------------------------
# F · sections P3 unmounted 不可达节点统计收口为模块级唯一实现
# ---------------------------------------------------------------------------


class TestUnmountedSectionIdsUnified:
    def test_function_defined(self):
        s = _src("routers/sections.py")
        assert "async def _unmounted_section_ids" in s

    def test_used_in_list_sections(self):
        s = _src("routers/sections.py")
        assert "await _unmounted_section_ids(db, scheme_id, tree)" in s

    def test_reuses_collect_not_second_traversal(self):
        s = _src("routers/sections.py")
        # 复用 _collect_section_ids，不新增第二份树遍历
        assert "mounted: set[str] = _collect_section_ids(tree)" in s


# ---------------------------------------------------------------------------
# G · sse_handlers 输入指纹第 11 part（可注入事实行摘要）+ R217 修复 F821
# ---------------------------------------------------------------------------


class TestOutlineFingerprintFactsRows:
    def test_facts_digest_is_11th_part(self):
        """第 11 个 part 是事实行摘要 digest，追加在末尾（升级后旧 checkpoint 首次全量重算）。"""
        s = _src("routers/sse_handlers.py")
        assert "_fp_facts_digest," in s

    def test_load_facts_rows_used_not_undefined(self):
        """R217 修复 F821：指纹用 `_load_facts_rows` 而非未定义名 `_facts_char_rows`。"""
        s = _src("routers/sse_handlers.py")
        assert "_load_facts_rows(db, scheme_id)" in s

    def test_no_undefined_facts_char_rows_as_call(self):
        """反证：`_facts_char_rows(` 不得作为函数调用残留（仅 R217 标记的注释提及）。"""
        s = _src("routers/sse_handlers.py")
        assert "_facts_char_rows(" not in s


# ---------------------------------------------------------------------------
# H · sse_handlers P2 oneshot 收尾前停止复检（审核链 AI 调用未注册 child_task）
# ---------------------------------------------------------------------------


class TestOneshotStopRecheck:
    def test_stop_recheck_before_completed(self):
        s = _src("routers/sse_handlers.py")
        assert "if is_stopped(task_id):" in s
        assert "yield await _finish_stopped_with_partial(task_id, _partial_holder)" in s


# ---------------------------------------------------------------------------
# I · sse_handlers P3 holder 非空才覆盖（停止窗口内 full_outline 为空不抹快照）
# ---------------------------------------------------------------------------


class TestPartialHolderNonEmptyCover:
    def test_nonempty_cover(self):
        s = _src("routers/sse_handlers.py")
        # 非空才覆盖（停止窗口内 full_outline 为空不抹掉一级树快照）
        assert "if full_outline:" in s
        assert '_partial_holder["outline"] = full_outline' in s


# ---------------------------------------------------------------------------
# J · sse_handlers P3 SSE-5 截断/复用信号（all_failed 出口 + error 事件）
# ---------------------------------------------------------------------------


class TestSse5TruncationSignals:
    def test_all_failed_checkpoint_signals(self):
        s = _src("routers/sse_handlers.py")
        # all_failed 出口（checkpoint）补指纹 + resumed_chapters/level1_truncated/level1_total
        assert 'for _k5 in ("resumed_chapters", "level1_truncated", "level1_total")' in s

    def test_error_event_signals(self):
        s = _src("routers/sse_handlers.py")
        # error 事件（在线 + 异常出口）补截断/复用信号；指纹不进 SSE（前端白名单外）
        assert 'for _k6 in ("resumed_chapters", "level1_truncated", "level1_total")' in s
        assert 'for _k7 in ("resumed_chapters", "level1_truncated", "level1_total")' in s


# ---------------------------------------------------------------------------
# K · _sse_outline_review 熔断后批量整单元 failed + 暂停/停止复检 + 审核跳过
# ---------------------------------------------------------------------------


class TestOutlineReviewBreakerAndRecheck:
    def test_batch_failed_on_breaker(self):
        s = _src("routers/_sse_outline_review.py")
        assert "if _is_quota_blocked(task_id):" in s

    def test_pause_stop_recheck_in_loop(self):
        s = _src("routers/_sse_outline_review.py")
        # 每次尝试前复检暂停/停止闸门（与单章路径对称）
        assert "await _sh.wait_resume(task_id)" in s
        assert 'if _sh.is_stopped(task_id):' in s
        assert 'return "stopped"' in s

    def test_review_skip_on_breaker(self):
        s = _src("routers/_sse_outline_review.py")
        # 子目录阶段已熔断时审核链不再按序发必然失败的 AI 调用（纯白烧）
        assert 'if task_id and _is_quota_blocked(task_id):' in s
        assert '"skip_reason": "quota_blocked"' in s


# ---------------------------------------------------------------------------
# L · outline_library review_status 白名单 + 版本列表 LIMIT 50
# ---------------------------------------------------------------------------


class TestOutlineLibraryReviewStatusGuard:
    def test_whitelist_guard(self):
        s = _src("routers/outline_library.py")
        assert "REVIEW_STATUSES = (" in s
        assert "not in REVIEW_STATUSES" in s

    def test_version_limit(self):
        s = _src("routers/outline_library.py")
        assert "LIMIT 50" in s


# ---------------------------------------------------------------------------
# M · schemes 级联清单与 projects._SCHEME_SCOPED_TABLES 收口为单一事实源
# ---------------------------------------------------------------------------


class TestSchemeCascadeUnified:
    def test_import_single_source(self):
        s = _src("routers/schemes.py")
        assert "from app.routers.projects import _SCHEME_SCOPED_TABLES" in s
        assert (
            'for table in (*_SCHEME_SCOPED_TABLES, "facts_chunk_failures", "facts_cross_conflicts")'
            in s
        )


# ---------------------------------------------------------------------------
# N · outline_versions 快照回退 status/outline_source 口径分叉修复
# ---------------------------------------------------------------------------


class TestRestoreSnapshotStatusParity:
    def test_empty_restore_resets_status(self):
        s = _src("services/outline_versions.py")
        # 0 章 → 目录待生成 + 清空来源（与 save-outline 清空分支同不变式）
        assert "status='目录待生成', outline_source=''" in s

    def test_nonempty_upgrade_only(self):
        s = _src("services/outline_versions.py")
        # >0 章 → 仅从「目录待生成」升级「目录已确认」（CASE 条件，不降级更高状态）
        assert "CASE WHEN status='目录待生成' THEN '目录已确认' ELSE status END" in s


# ---------------------------------------------------------------------------
# O · repair_record create_snapshot commit 语义交调用方（保 undo 快照原子性）
# ---------------------------------------------------------------------------


class TestRepairRecordCommitSemantics:
    def test_commit_param_and_guard(self):
        s = _src("services/repair_record.py")
        assert "commit: bool = True" in s
        assert "if commit:" in s
        assert "await db.commit()" in s
