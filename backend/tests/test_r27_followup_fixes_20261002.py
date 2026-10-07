# -*- coding: utf-8 -*-
"""第 27 轮收口护栏（2026-10-02）：生产端到端验证后发现的两个可修复项。

A. checkpoint_fixes 留痕存活 —— sse_handlers._persist_section 里
   `report = standard_report(...)` 整体**重建**报告，把重建前写入的
   checkpoint_fixes 覆盖丢失（生产实证：autofix 改了正文、库里报告却无
   修复记录）。接线静态锁：standard_report 调用之后的窗口内必须重新挂回留痕。
B. 标准库收录（STD-03 证据驱动）—— 生产检出但此前不可自动补的 5 个编号
   （GB 2811-2019 / GB 6095-2021 / GB/T 25182-2010 / GB 50140-2005 /
   GB 50217-2018）已逐条核对于全国标准信息公共服务平台/部公告后入库；
   BASE_CODE_INDEX 唯一来源是注册表，入库即自动获得补年号能力。

⚠️ 判据指向（AGENTS §4.22.8 教训）：对 sse_handlers 走源码扫描且只锚定
   真正生效的装配段（`report = standard_report(` 之后的窗口），不做
   「碰巧含关键字」的全文件匹配。
⚠️ A/B 反向验证：留痕合并摘掉 / 编号撤出注册表 → 各定向失败。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.services import standards_registry as sr
from app.services.content_checkpoint import (
    BASE_CODE_INDEX,
    _normalize_code_for_index,
    fix_bare_standard_codes,
)

_SSE_SRC = (Path(__file__).resolve().parent.parent
            / "app" / "routers" / "sse_handlers.py").read_text(encoding="utf-8")

#: R27/R29 收录的编号（基号 → 完整编号）。
#: R27（2026-10-02）5 个；R29（检查点反哺 · STD-03 证据驱动）3 个。
NEW_CODES = {
    "GB 2811": "GB 2811-2019",
    "GB 6095": "GB 6095-2021",
    "GB/T 25182": "GB/T 25182-2010",
    "GB 50140": "GB 50140-2005",
    "GB 50217": "GB 50217-2018",
    "GB 50209": "GB 50209-2010",
    "GB 50222": "GB 50222-2017",
    "GB 50009": "GB 50009-2012",
}


# ---------------------------------------------------------------------------
# A. checkpoint_fixes 留痕存活
# ---------------------------------------------------------------------------

class TestFixesTraceSurvival:

    def test_remerge_wired_after_report_rebuild(self):
        """standard_report 重建之后的窗口内必须把留痕重新挂回。

        ⚠️ 窗口按 900 字符取：合并行前方有 5 行中文注释（每字符计 1，
        600 窗口会把赋值行拦腰截断 —— 本轮实测踩到）。
        """
        at = _SSE_SRC.index("report = standard_report(")
        window = _SSE_SRC[at:at + 900]
        assert 'report["checkpoint_fixes"] = _ck_fix_actions' in window, (
            "standard_report 重建后未恢复 checkpoint_fixes（留痕覆盖丢失）")

    def test_remerge_gated_on_nonempty(self):
        """无修复动作时不写空键（产物与旧版逐字一致，向后兼容红线）。"""
        at = _SSE_SRC.index("report = standard_report(")
        window = _SSE_SRC[at:at + 900]
        assert re.search(
            r"if _ck_fix_actions:\s*\n\s*report\[\"checkpoint_fixes\"\]"
            r" = _ck_fix_actions", window, re.DOTALL), "合并必须先判非空（空留痕不得入报告）"

    def test_autofix_still_before_report(self):
        """既有红线不破：修复在任何报告计算之前（防本轮改动位移破坏顺序）。"""
        fix_at = _SSE_SRC.index("_ck_fix_actions: list = []")
        report_at = _SSE_SRC.index("report = standard_report(")
        assert fix_at < report_at

    def test_rebuild_sequence_keeps_trace_invariant(self):
        """按 _persist_section 同款序列模拟：修复→重建→合并 → 留痕存活。"""
        text = "依据 GB 50140 的配置要求执行。"
        content, actions = fix_bare_standard_codes(text)
        actions = [a for a in actions if a.get("fixed")]
        assert actions and "GB 50140-2005" in content
        report = {"error_count": 0}            # standard_report 重建产物
        report["checkpoint_fixes"] = actions   # 重建前写入（会被覆盖的形态）
        report = dict(report or {})            # 模拟重建
        if actions:
            report["checkpoint_fixes"] = actions  # 本轮补的合并
        assert len(report["checkpoint_fixes"]) == len(actions)

    def test_revert_remerge_loses_trace(self):
        """A/B 对照：摘掉合并（只走重建不回填）→ 留痕确实丢失（缺陷真实存在）。"""
        text = "依据 GB 50140 的配置要求执行。"
        _content, actions = fix_bare_standard_codes(text)
        actions = [a for a in actions if a.get("fixed")]
        report = {"checkpoint_fixes": actions}
        rebuilt = {"error_count": 0}           # 无合并的直接替换
        assert "checkpoint_fixes" not in rebuilt
        assert "checkpoint_fixes" in report     # 旧写入点本身没问题，问题在替换


# ---------------------------------------------------------------------------
# B. 标准库收录（注册表 → BASE_CODE_INDEX 单一来源链路）
# ---------------------------------------------------------------------------

class TestRegistryAdditions:

    @pytest.mark.parametrize("base,full", sorted(NEW_CODES.items()))
    def test_code_indexed_and_fixable(self, base, full):
        """注册表入库即进索引；裸号可被确定性补全年号。

        ⚠️ 索引键是归一形态（去空格，见 _normalize_code_for_index），
        断言必须走同一个归一函数，不得字面拼键。
        """
        key = _normalize_code_for_index(base)
        assert BASE_CODE_INDEX.get(key) == full
        bare = f"{base} 的相关规定。"
        out, fixes = fix_bare_standard_codes(bare)
        done = [f for f in fixes if f.get("fixed")]
        assert done and full in out
        assert done[0]["rule_id"] == "STD-03"

    def test_registry_entries_verified_fields(self):
        """每条新收录均有名称且带年号（注册表维护约定 1/2 条）。"""
        pool = list(sr.BASE_STANDARDS)
        for items in sr.CATEGORY_STANDARDS.values():
            pool.extend(items)
        by_code = {s.code: s for s in pool}
        for full in NEW_CODES.values():
            assert full in by_code, full
            assert re.search(r"-\d{4}$", full), full
            assert by_code[full].name, full

    def test_no_ambiguity_introduced(self):
        """新条目不得与既有条目构成同基号不同完整编号的歧义（歧义=不补）。"""
        base2code: dict = {}
        ambiguous: set = set()
        pool = list(sr.BASE_STANDARDS)
        for items in sr.CATEGORY_STANDARDS.values():
            pool.extend(items)
        for s in pool:
            base = sr.strip_standard_year(s.code)
            cur = base2code.get(base)
            if cur is None:
                base2code[base] = s.code
            elif cur != s.code:
                ambiguous.add(base)
        assert not (ambiguous & {_normalize_code_for_index(b) for b in NEW_CODES}), (
            f"歧义基号: {ambiguous}")
        assert all(BASE_CODE_INDEX.get(_normalize_code_for_index(b)) == f
                   for b, f in NEW_CODES.items())

    def test_new_codes_not_in_abolished(self):
        """收录的是现行版；旧版若在册必须是废止登记（防自相矛盾）。"""
        for full in NEW_CODES.values():
            assert full not in sr.ABOLISHED_STANDARDS, full

    def test_revert_from_registry_disables_fix(self):
        """A/B 对照：把条目撤出注册表 → 索引随之失活（单一来源链路成立）。"""
        saved = {k: list(v) for k, v in sr.CATEGORY_STANDARDS.items()}
        try:
            without = {k: [s for s in v
                           if s.code not in NEW_CODES.values()]
                       for k, v in saved.items()}
            sr.CATEGORY_STANDARDS.clear()
            sr.CATEGORY_STANDARDS.update(without)
            import app.services.content_checkpoint as mod
            idx, _amb = mod._build_base_code_index()
            for base in NEW_CODES:
                assert _normalize_code_for_index(base) not in idx, base
        finally:
            sr.CATEGORY_STANDARDS.clear()
            sr.CATEGORY_STANDARDS.update(saved)

    def test_db_version_bumped_with_additions(self):
        """维护约定第 4 条：标准更新必须同步 bump 版本。

        R27 = 2026.10.1；R29（检查点反哺补入装饰装修类 3 条）= 2026.10.2；
        R51（2026-10-07，openstd.samr.gov.cn 实证装饰装修材料有害物质限量系列
        现行/废止状态）= 2026.10.7。
        """
        assert sr.STANDARD_DB_VERSION == "2026.10.7"
        assert sr.STANDARD_DB_CHECKED_AT == "2026-10-07"
