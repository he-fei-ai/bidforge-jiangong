"""R82 护栏：正文生成后一致性审计「用全量已确认事实弥补注入缺口」（对齐易标 CG:1686）

引入内容（docs/全局事实正文符合性提升_全流程报告与引入方案_R82_20261015.md）：
  standard_report 此前只喂「本章精选事实」（_filter_facts_rows 2-gram 预筛子集），
  正文若与某条**未注入本章**的已确认事实冲突（各章随机编一套人员/工期/型号的
  典型形态），检不出也修不了。易标把**全量已确认事实**与正文一起交审计，正是为
  弥补「生成时只注入选中事实」的缺口。

本护栏锁定四件事：
  A. 默认行为不变性 —— conflict_fact_rows=None 时输出与引入前逐字节等价；
  B. 全量冲突检出行为 —— 与未注入本章的事实冲突能被检出，且同现判据防误报；
  C. 覆盖率不受全量污染 —— fact_value_missing 仍只按本章注入事实（避免全量噪声、
     对齐易标「未涉及不报缺失」纪律）；
  D. 判据单一事实源 + 接线（矛盾判据只一份副本；开关默认 True；两处 standard_report
     都传 conflict_fact_rows；autofix 门控/输入均走 _autofix_rows）。
"""
from __future__ import annotations

import json

from app.services.content_standard import standard_report


def _row(title: str, content: str, chapter: str = "") -> tuple:
    """构造事实行 5 元组 (group_title, title, content, confidence, chapter)。"""
    return ("项目关键事实", title, content, 1.0, chapter)


def _issues_of(report: dict, *types: str) -> list[dict]:
    return [it for it in report.get("issues", []) if it.get("type") in types]


def _dump(report: dict) -> str:
    """稳定性序列化（用于逐字节等价比对）。"""
    return json.dumps(
        {"standard": report.get("standard"),
         "passed": report.get("passed"),
         "error_count": report.get("error_count"),
         "warning_count": report.get("warning_count"),
         "issues": report.get("issues")},
        ensure_ascii=False, sort_keys=True)


# ===========================================================================
# A. 默认行为不变性（conflict_fact_rows=None → 逐字节等于引入前）
# ===========================================================================

class TestDefaultInvariance:
    def test_none_argument_matches_explicit_none(self):
        content = "本项目总工期为 120 天，安排合理。"
        rows = [_row("项目总工期", "- 项目总工期：90 天")]
        a = standard_report(content, "precise", rows)
        b = standard_report(content, "precise", rows, conflict_fact_rows=None)
        assert _dump(a) == _dump(b)

    def test_conflict_not_detected_without_full_rows(self):
        # 本章无注入事实（fact_rows=[]），仅靠 conflict_fact_rows 才可能检出冲突；
        # 不传 → 检出面 == 本章空集 → 无 value_mismatch（引入前形态）。
        content = "本项目总工期为 120 天，安排合理。"
        r = standard_report(content, "precise", [])
        assert _issues_of(r, "value_mismatch") == []

    def test_empty_conflict_rows_same_as_none(self):
        content = "本质保期 90 天。"
        rows = [_row("质保期", "- 质保期：90 天")]
        assert _dump(standard_report(content, "precise", rows, conflict_fact_rows=[])) \
            == _dump(standard_report(content, "precise", rows))


# ===========================================================================
# B. 全量冲突检出行为
# ===========================================================================

class TestFullFactsConflictDetection:
    def test_cross_chapter_value_conflict_detected(self):
        # 正文写了工期 120 天，本章未注入工期事实（fact_rows=[]），
        # 但全量已确认事实含「项目总工期 90 天」→ 扩面后检出 value_mismatch。
        content = "本项目总工期为 120 天，安排合理。"
        full = [_row("项目总工期", "- 项目总工期：90 天")]
        r = standard_report(content, "precise", [], conflict_fact_rows=full)
        conflicts = _issues_of(r, "value_mismatch")
        assert len(conflicts) == 1
        assert "工期" in conflicts[0]["message"]

    def test_conflict_still_requires_co_location(self):
        # 全量事实「售后质保期 90 天」，正文写「总工期 120 天」——同单位（天）
        # 但质保期关键词不在正文数值同现窗口 → 不得误报（同现判据防误报）。
        content = "本项目总工期为 120 天，安排合理。"
        full = [_row("售后质保期", "- 售后质保期：90 天")]
        r = standard_report(content, "precise", [], conflict_fact_rows=full)
        assert _issues_of(r, "value_mismatch") == []

    def test_model_conflict_from_full_facts(self):
        content = "垂直运输采用塔吊 QTZ120 型号。"
        full = [_row("主要塔吊型号", "- 主要塔吊型号：QTZ80")]
        r = standard_report(content, "precise", [], conflict_fact_rows=full)
        assert len(_issues_of(r, "model_conflict")) == 1

    def test_fuzzy_tolerance_respected_on_full_facts(self):
        # 模糊模式：全量事实 90 天，正文 95 天（±10% 内）→ 不报；
        big = standard_report("本项目总工期为 120 天。", "fuzzy", [],
                              conflict_fact_rows=[_row("项目总工期", "- 工期：90 天")])
        assert len(_issues_of(big, "value_conflict")) == 1
        ok = standard_report("本项目总工期为 95 天。", "fuzzy", [],
                             conflict_fact_rows=[_row("项目总工期", "- 工期：90 天")])
        assert _issues_of(ok, "value_conflict") == []


# ===========================================================================
# C. 覆盖率不受全量污染（fact_value_missing 只按本章注入事实）
# ===========================================================================

class TestCoverageNotPollutedByFullFacts:
    def test_unreferenced_full_fact_does_not_report_missing(self):
        # 全量事实「项目总工期 90 天」未注入本章（fact_rows=[]），正文也没写工期
        # 数值 → 既不报冲突、也**不报覆盖率缺失**（对齐易标「未涉及不报缺失」）。
        content = "本节介绍施工组织与人员配备安排。"
        full = [_row("项目总工期", "- 项目总工期：90 天")]
        r = standard_report(content, "precise", [], conflict_fact_rows=full)
        assert _issues_of(r, "fact_value_missing") == []

    def test_chapter_fact_still_reports_missing(self):
        # 本章注入了工期事实但正文未引用 → 覆盖率告警照常产出（口径未削弱）。
        content = "本节介绍施工组织与人员配备安排。"
        rows = [_row("项目总工期", "- 项目总工期：90 天")]
        r = standard_report(content, "precise", rows)
        assert len(_issues_of(r, "fact_value_missing")) == 1


# ===========================================================================
# D. 去重：本章与全量同 title 的冲突不双计
# ===========================================================================

class TestTitleDedup:
    def test_same_title_not_double_counted(self):
        content = "本项目总工期为 120 天。"
        chapter_fact = _row("项目总工期", "- 项目总工期：90 天")
        full = [chapter_fact, _row("项目总工期", "- 项目总工期：90 天")]
        r = standard_report(content, "precise", [chapter_fact],
                            conflict_fact_rows=full)
        # 同 title 已在 fact_groups → 被 _extra_groups 去重跳过；value_mismatch 恰 1 条
        assert len(_issues_of(r, "value_mismatch")) == 1


# ===========================================================================
# E. 判据单一事实源 + 接线（静态锁，防「名字在场但接线被掏空」/副本扩散）
# ===========================================================================

class TestSingleSourceAndWiring:
    def _src(self, mod: str) -> str:
        import importlib
        m = importlib.import_module(mod)
        with open(m.__file__, "r", encoding="utf-8") as f:
            return f.read()

    def test_config_default_true(self):
        from app.config import Settings
        assert Settings.model_fields["content_audit_full_facts"].default is True

    def test_conflict_message_literal_appears_once(self):
        # 矛盾判据唯一实现：数值/型号矛盾的 message 字面量各只出现一次
        # （抽进 _detect_conflicts 共用，未在别处复制第二份）。
        src = self._src("app.services.content_standard")
        assert src.count("数值与全局事实矛盾") == 1
        assert src.count("型号与全局事实矛盾") == 1

    def test_detect_conflicts_shared_by_both_groups(self):
        src = self._src("app.services.content_standard")
        # _extra_groups 与 fact_groups 都经 _detect_conflicts 调用（不各自内联）
        assert src.count("_detect_conflicts(") >= 3  # def + 2 调用点
        # _extra_groups 循环体只调 _detect_conflicts，不内联重复矛盾判定
        assert 'for title, g in _extra_groups.items():\n            _detect_conflicts(' in src

    def test_sse_reads_switch_and_passes_rows(self):
        src = self._src("app.routers.sse_handlers")
        assert 'getattr(settings, "content_audit_full_facts", True)' in src
        # 两处 standard_report 都传 conflict_fact_rows（首检 + 修复后重算）
        assert src.count("conflict_fact_rows=_conflict_rows") == 2
        # autofix 门控与输入均走 _autofix_rows（不再用 fact_rows_for_section）
        assert "if _fact_autofix_on and _autofix_rows:" in src
        assert "fact_rows=_autofix_rows," in src
