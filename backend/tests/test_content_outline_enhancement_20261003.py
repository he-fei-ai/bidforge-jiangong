"""目录生成 + 正文生成增强护栏（2026-10-03）

覆盖本轮四项改造，全部为**默认向后兼容**的新增能力：

P1-1  占位标记判据收敛为单一事实源
      ``content_fuzzy.PLACEHOLDER_MARK_PATTERNS`` 是检测与改写的唯一出口；
      ``content_checkpoint._PLACEHOLDER_RES`` 由该表**派生**、不再自带副本
      —— 「检出却修不掉 / 自检漏报」的分叉在结构上不可能发生。
P1-2  法规清单（含建办质〔2021〕48号 及 2024 新规）经 ``get_standards_text``
      注入目录与正文提示词（``include_regulations`` 默认 True）。
P0-1  九大章节**完整**必含要素清单（``NINE_CHAPTERS.base_fields`` + 命中的
      危大类别 ``category_fields``）注入正文逐章提示词。
P0-2  同一份要素清单注入**目录生成**提示词（正文只按目录标题写，目录无
      落点 = 正文无内容），让二三级小节逐项覆盖要素。

判据纪律：本文件只做「行为断言 + 判据同源断言」，**不锁正则字面量**
（见 AGENTS.md §4.28 关于 `周边`/`月末` 刻意不收的取舍）。
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from app.services import content_checkpoint as cc
from app.services import content_fuzzy as cf
from app.services import outline_checkpoint as ocp
from app.services import standards_registry as sr
from app.services.scheme_classification import NINE_CHAPTERS

REPO = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    """读后端源码（相对 backend/），供判据同源静态断言使用。"""
    return io.open(REPO / "app" / rel, encoding="utf-8").read()


# ---------------------------------------------------------------------------
# 占位标记形态样本网（检测侧 / 自检侧 / 改写侧三条链路的共同输入）
# ---------------------------------------------------------------------------
#: 旧判据（content_checkpoint 自带副本）**收不到**的形态 —— 本轮收敛后必须全部检出
NEWLY_DETECTABLE = [
    "【待完善】", "【待确定】", "【待补录】", "【待补】",
    "【略】", "【】",
    "[待完善]", "[待确定]", "[待确认]", "[待补录]",
    "[待定]", "[数值]", "[参数]", "[TBD]", "[]",
    "TBD", "N/A",
]
#: 旧判据**本就能收**的形态（不得回退）
LEGACY_FORMS = ["【待补充：宽度】", "【待补充】", "【待填写】",
                "【待定】", "【待确认】", "[待补充]", "××"]
#: 「只报不改」形态（上下文不明，自动改写会误伤正常正文）
NON_REWRITABLE_FORMS = ["××", "TBD", "N/A"]


def _assert_detected(form: str) -> None:
    text = f"本章正文说明。{form}。施工时按现场实际组织。"
    hits = cf.scan_placeholder_marks(text)
    assert hits, f"检测侧漏检占位标记 {form!r}"
    findings = cc.checkpoint_selfcheck(text, chapter_key="")
    assert any(f["rule_id"] == "CON-04" for f in findings), (
        f"自检侧漏报占位标记 {form!r}（与检测侧口径不一致）")


# ===========================================================================
# P1-1 · 占位标记判据单一事实源
# ===========================================================================
class TestPlaceholderCriteriaSingleSource:
    """检测 / 自检 / 改写三条链路共用一份正则表。"""

    def test_pattern_table_shape(self):
        table = cf.PLACEHOLDER_MARK_PATTERNS
        assert len(table) == 5, "占位标记形态表应为 5 条（3 基础 + 2 扩展）"
        for rx, kind, rewritable in table:
            assert hasattr(rx, "finditer"), "表内第一元素必须是编译好的正则"
            assert kind in {"formatted", "bare", "fuzzy", "extended"}, kind
            assert isinstance(rewritable, bool)

    def test_rewritable_partition_is_complementary(self):
        """可改写 + 只报不改 == 全表（不丢形态、不重复）。

        ⚠️ 比较用 **pattern 字符串多重集合**：两个子集各自只保留自己的条目，
        拼起来后顺序与原表不同（fuzzy 在原表第 3 位、却排在子集拼接的第 4 位），
        按序比较会误判分叉。
        """
        rw = cf.placeholder_rewritable_patterns()
        nrm = cf.placeholder_nonrewritable_patterns()
        assert rw and nrm, "两个子集都不应为空"
        all_pats = [rx.pattern for rx, _k, _w in cf.PLACEHOLDER_MARK_PATTERNS]
        part_pats = ([rx.pattern for rx in rw] + [rx.pattern for rx in nrm])
        assert sorted(all_pats) == sorted(part_pats), (
            "两个子集合并后必须与全表多重集合相等，否则收敛判据后丢形态/重复")
        assert not (set(rw) & set(nrm)), "可改写与只报不改不得有交集"
        assert sum(1 for _r, _k, w in cf.PLACEHOLDER_MARK_PATTERNS if w) == len(rw)

    def test_checkpoint_res_derived_from_table(self):
        """自检侧的正则表必须是全表派生（同一批对象，同序）。"""
        expect = [rx for rx, _k, _w in cf.PLACEHOLDER_MARK_PATTERNS]
        assert list(cc._PLACEHOLDER_RES) == expect, (
            "content_checkpoint._PLACEHOLDER_RES 必须由 PLACEHOLDER_MARK_PATTERNS "
            "派生；若此处自带副本，分叉会重新出现")

    def test_no_local_placeholder_regex_copy_in_checkpoint(self):
        """content_checkpoint 不得再自带「【待补充/待确认/待定】」字面量正则。

        判据指向**真正下发给模型/判定的那份正则定义**：只检查
        ``_PLACEHOLDER_RES`` 这一行的定义形态（必须是表推导），
        不用纯文本匹配整文件（注释与文档字符串里必然提到这些字样）。
        """
        src = _src("services/content_checkpoint.py")
        needle = "tuple(\n    rx for rx, _kind, _rw in PLACEHOLDER_MARK_PATTERNS\n)"
        assert needle in src, (
            "_PLACEHOLDER_RES 必须是 PLACEHOLDER_MARK_PATTERNS 的推导式派生；"
            "改回手写正则列表 = 判据重新分叉")
        # 反面：旧的窄口径字面量不得出现在任何 re.compile 参数里
        import re as _re
        for m in _re.finditer(r"re\.compile\((.{0,200}?)\)\s*(?:\n|#)", src,
                              flags=_re.S):
            assert "待确认" not in m.group(1), (
                f"content_checkpoint 内出现自带占位正则副本: {m.group(0)[:60]}")

    def test_legacy_forms_still_detected(self):
        """收敛判据不得导致旧形态漏检（回归护栏）。"""
        for form in LEGACY_FORMS:
            _assert_detected(form)

    def test_newly_detectable_forms_all_reported(self):
        """此前「检出却修不掉」的形态必须全部由检测侧与自检侧同时报出。"""
        for form in NEWLY_DETECTABLE:
            _assert_detected(form)

    def test_scan_and_selfcheck_parity(self):
        """全形态 parity：检测侧报 ⟺ 自检侧报 CON-04。"""
        for form in NEWLY_DETECTABLE + LEGACY_FORMS:
            text = f"某节施工说明。{form}。后续按现场情况组织。"
            scan_hit = bool(cf.scan_placeholder_marks(text))
            self_hit = any(f["rule_id"] == "CON-04"
                           for f in cc.checkpoint_selfcheck(text))
            assert scan_hit == self_hit, (
                f"占位标记判据分叉：{form!r} 检测侧={scan_hit} 自检侧={self_hit}")


# ===========================================================================
# P1-1 · 占位标记确定性改写（可改写 / 只报不改 分流）
# ===========================================================================
class TestRewritePlaceholderMarks:
    """改写侧共用同一份正则表：可改写子集改写、只报不改子集报 pending。"""

    def test_rewrites_all_rewritable_forms(self):
        for form in ["【待补充：宽度】", "【待补充】", "【待定】",
                     "【待完善】", "【待补录】", "【待确定】", "【略】",
                     "【】", "[待补充]", "[待定]", "[数值]", "[TBD]"]:
            new_text, fixes = cc.rewrite_placeholder_marks(f"此处{form}。")
            assert "【" not in new_text and "[" not in new_text, (
                f"改写后仍残留括号占位标记：{form!r} → {new_text!r}")
            fixed = [f for f in fixes if f.get("fixable") and f.get("fixed")]
            assert fixed, f"可改写形态 {form!r} 未产生 fixable 记录"

    def test_nonrewritable_reported_not_rewritten(self):
        """×× / TBD / N/A 上下文明确性不足：保留原文 + 报 fixable=False。"""
        for form in NON_REWRITABLE_FORMS:
            original = f"此处{form}。"
            new_text, fixes = cc.rewrite_placeholder_marks(original)
            assert new_text == original, (
                f"只报不改形态 {form!r} 不应被改写：{new_text!r}")
            pending = [f for f in fixes if not f.get("fixable")]
            assert pending, f"只报不改形态 {form!r} 未报出 pending 记录"

    def test_idempotent(self):
        text = ("深度【待定】m、宽度【待补充：宽度】mm、堆放区面积"
                "【待补充：堆放区面积】平方米，另有【待完善】项。")
        once, _ = cc.rewrite_placeholder_marks(text)
        twice, fixes2 = cc.rewrite_placeholder_marks(once)
        assert twice == once, "改写不幂等（第二次调用仍改动正文）"
        assert not [f for f in fixes2 if f.get("fixable")], (
            "第二次改写不得再产出 fixable 记录")

    def test_adjacent_marks_not_swallowed(self):
        """回归护栏：相邻两个占位标记不得被当成一个（会吞掉中间正常正文）。"""
        text = "深度【待定】m 宽度【待补充：宽度】mm"
        hits = cf.scan_placeholder_marks(text)
        assert len(hits) == 2, f"相邻占位标记应检出 2 处，实际 {len(hits)}"
        new_text, _ = cc.rewrite_placeholder_marks(text)
        assert "宽度" in new_text, (
            f"中间正常正文被吞掉：{new_text!r}")

    def test_trailing_unit_preserved(self):
        """改写后紧随的单位收进括号注记，不遗留孤立单位。"""
        for text, unit in [("面积【待补充：面积】平方米", "平方米"),
                           ("每周清运不少于【待补充：频次】次", "次"),
                           ("深度【待定】m", "m"),
                           ("高度【待定】米", "米")]:
            new_text, _ = cc.rewrite_placeholder_marks(text)
            assert unit in new_text, (
                f"单位 {unit!r} 未保留：{new_text!r}")
            assert f"确定{unit}" not in new_text, (
                f"遗留孤立单位（未收进括号注记）：{new_text!r}")

    def test_cix_ri_word_not_absorbed_as_unit(self):
        """`次` 不得被吸收进「次日」（next day）—— 这是刻意的守卫取舍。"""
        text = "【待定】次日恢复施工"
        new_text, _ = cc.rewrite_placeholder_marks(text)
        assert "次日" in new_text, f"「次日」被拆坏：{new_text!r}"
        assert "（次）" not in new_text, f"误把「次日」的「次」当单位：{new_text!r}"

    def test_fence_blocks_untouched(self):
        fenced = "前文。\n```json\n{\"k\": \"【待补充：x】\"}\n```\n后文【待定】。"
        new_text, fixes = cc.rewrite_placeholder_marks(fenced)
        assert "【待补充：x】" in new_text, "围栏代码块内的占位标记不得改写"
        assert "【待定】" not in new_text, "围栏外的占位标记应正常改写"

    def test_fail_soft_on_dirty_input(self):
        assert cc.rewrite_placeholder_marks("") == ("", [])
        new_text, fixes = cc.rewrite_placeholder_marks(None)  # type: ignore[arg-type]
        assert new_text == "" and fixes == []


# ===========================================================================
# P0-1 · 九大章节「完整」必含要素清单（单一事实源 = NINE_CHAPTERS）
# ===========================================================================
class TestChapterRequiredElements:
    """要素清单必须**完全等于** NINE_CHAPTERS，本模块不得复制清单。"""

    def test_equals_base_fields_when_no_category_hit(self):
        """无危大类别命中时：要素 == base_fields（逐字、同序）。"""
        from app.services.scheme_classification import required_fields_for_chapter
        for ch in NINE_CHAPTERS:
            key = ch["key"]
            assert cc.chapter_required_elements(key) == required_fields_for_chapter(key)
            assert cc.chapter_required_elements(key) == list(ch["base_fields"]), (
                f"章节 {key} 的要素清单应与 base_fields 完全一致")

    def test_category_fields_appended_in_order(self):
        """命中危大类别时：base_fields 在前、类别追加在后。"""
        base = cc.chapter_required_elements("overview")
        with_cat = cc.chapter_required_elements("overview", "深基坑支护与降水工程")
        assert len(with_cat) > len(base), "危大类别未追加专属要素"
        assert with_cat[:len(base)] == base, "追加不得改动 base_fields 顺序"
        assert with_cat[len(base):], "危大类别追加要素为空"
        assert len(with_cat) == len(set(with_cat)), "合并后出现重复要素"

    def test_dedup_when_multiple_categories_share_field(self):
        elements = cc.chapter_required_elements(
            "overview", "深基坑支护与降水工程专项施工方案")
        assert elements and len(elements) == len(set(elements))

    def test_unknown_or_empty_key_returns_empty(self):
        assert cc.chapter_required_elements("") == []
        assert cc.chapter_required_elements("no_such_chapter") == []

    def test_all_nine_chapters_have_elements(self):
        """九大章节每章都必须有非空要素清单（否则提示词无法要求）。"""
        for ch in NINE_CHAPTERS:
            key = ch["key"]
            assert key, "章节 key 不得为空"
            assert cc.chapter_required_elements(key), f"章节 {key} 无要素清单"


class TestChapterElementBlockInjection:
    """要素清单 → 正文逐章提示词的注入（开关 content_chapter_elements_inject）。"""

    def test_element_block_lists_every_element(self):
        blk = cc.build_chapter_element_block("safety", "某深基坑工程")
        assert "【施工安全保证措施 · 本章必含要素清单】" in blk
        for el in cc.chapter_required_elements("safety", "某深基坑工程"):
            assert el in blk, f"要素 {el} 未写入提示块"
        assert "严禁留占位标记" in blk, "必须重申「不留占位标记」红线"

    def test_switch_off_returns_empty_string(self):
        assert cc.build_chapter_element_block(
            "safety", "某深基坑工程", include_elements=False) == ""

    def test_unknown_chapter_returns_empty(self):
        assert cc.build_chapter_element_block("no_such_chapter") == ""

    def test_checkpoint_block_appends_elements_when_enabled(self):
        on = cc.build_chapter_checkpoint_block(
            "safety", scheme_name="某深基坑工程", include_elements=True)
        off = cc.build_chapter_checkpoint_block(
            "safety", scheme_name="某深基坑工程", include_elements=False)
        assert "本章必含要素清单" in on, "开关打开时未追加要素清单"
        assert "本章必含要素清单" not in off, "开关关闭时仍追加要素清单"
        assert len(on) > len(off)
        # 关闭后原有「检查点要求」主体必须逐字保留（只丢追加段）
        assert off.strip() in on.strip()

    def test_checkpoint_block_default_is_backward_compatible_shape(self):
        """默认（不传新参数）仍返回完整块；未知章节仍返回空串。"""
        assert cc.build_chapter_checkpoint_block("no_such_chapter") == ""
        assert cc.build_chapter_checkpoint_block("safety") != ""

    def test_hazard_only_requirement_unaffected_by_elements(self):
        """要素追加不得改变 hazard_only 要求（STD-04）的门控行为。"""
        non_hazard = cc.build_chapter_checkpoint_block(
            "basis", is_hazardous=False, include_elements=False)
        hazard = cc.build_chapter_checkpoint_block(
            "basis", is_hazardous=True, include_elements=False)
        assert "STD-04" not in non_hazard
        assert "STD-04" in hazard

    def test_sse_handlers_wires_elements_switch(self):
        """正文链路必须把开关与方案名透传给要素追加（否则增强永不生效）。"""
        src = _src("routers/sse_handlers.py")
        assert "content_chapter_elements_inject" in src, "正文链路未读取要素注入开关"
        call_site = src[src.find("build_chapter_checkpoint_block("):]
        assert call_site, "正文链路未调用 build_chapter_checkpoint_block"
        assert "include_elements=" in call_site[:1200], (
            "build_chapter_checkpoint_block 调用点未透传 include_elements")
        assert "scheme_name=" in call_site[:1200], (
            "build_chapter_checkpoint_block 调用点未透传 scheme_name")


# ===========================================================================
# P0-2 · 同一份要素清单 → 目录生成提示词
# ===========================================================================
class TestOutlineElementBlock:
    """目录侧要素注入：正文只按目录标题写，目录无落点 = 正文无内容。"""

    def test_blocks_lists_all_nine_chapters(self):
        blk = ocp.build_outline_element_block("某深基坑工程")
        assert "九大章节必含要素" in blk
        for ch in NINE_CHAPTERS:
            assert f"**{ch['title']}**" in blk, f"目录侧缺章节 {ch['title']}"

    def test_elements_come_from_same_source(self):
        """目录侧与正文侧必须取同一份要素清单（防止两侧清单分叉）。"""
        from app.services.scheme_classification import required_fields_for_chapter
        blk = ocp.build_outline_element_block("")
        for ch in NINE_CHAPTERS:
            fields = required_fields_for_chapter(ch["key"])
            assert fields, f"章节 {ch['key']} 无 base_fields"
            for field in fields:
                assert field in blk, (
                    f"目录侧未列出 {ch['title']} 的要素 {field}")

    def test_switch_off_returns_empty_string(self):
        assert ocp.build_outline_element_block(
            "某深基坑工程", include_elements=False) == ""

    def test_outline_checkpoint_kwargs_appends_elements(self, monkeypatch):
        from app.routers import sse_handlers as sh
        kwargs_on = sh._outline_checkpoint_kwargs(scheme_name="某深基坑工程")
        assert kwargs_on, "开关默认开启时应注入 outline_checkpoint_block"
        assert "九大章节必含要素" in kwargs_on["outline_checkpoint_block"]
        monkeypatch.setattr(sh.settings, "content_chapter_elements_inject",
                            False, raising=False)
        kwargs_off = sh._outline_checkpoint_kwargs(scheme_name="某深基坑工程")
        assert kwargs_off, "要素开关关闭不应连带关掉检查点块"
        assert "九大章节必含要素" not in kwargs_off["outline_checkpoint_block"]

    def test_outline_checkpoint_kwargs_master_switch_off(self, monkeypatch):
        """旧总开关关闭时整段不注入（既有向后兼容契约）。"""
        from app.routers import sse_handlers as sh
        monkeypatch.setattr(sh.settings, "outline_checkpoint_check", False,
                            raising=False)
        assert sh._outline_checkpoint_kwargs(scheme_name="某深基坑工程") == {}

    def test_outline_template_has_checkpoint_placeholder(self):
        """目录模板必须存在 {outline_checkpoint_block} 占位符（唯一接线锚点）。"""
        src = _src("services/ai/prompts/outline.py")
        assert "{outline_checkpoint_block}" in src, (
            "目录模板缺 {outline_checkpoint_block} 占位符，注入无处落地")

    def test_standards_text_injected_into_both_prompts(self):
        """P1-2 接线：目录模板与正文模板都须有 {standards_text} 占位符。"""
        assert "{standards_text}" in _src("services/ai/prompts/outline.py")
        assert "{standards_text}" in _src("services/ai/prompts/content.py")


# ===========================================================================
# P1-2 · 法规清单（含 48 号 与 2024 新规）注入生成链路
# ===========================================================================
class TestRegulationsFlow:
    """REGULATIONS → get_standards_text → 提示词 的端到端贯通。"""

    def test_regulations_table_contains_new_docs(self):
        joined = "".join(sr.REGULATIONS)
        assert "建办质〔2018〕31号" in joined, "缺失 31 号文"
        assert "建办质〔2021〕48号" in joined, "缺失 48 号文（编制指南）"
        assert "建质规〔2024〕5号" in joined, "缺失 2024 新规 5 号"
        assert "建办质〔2024〕63号" in joined, "缺失 2024 新规 63 号"
        assert "37号" in joined, "缺失住建部令第 37 号"

    def test_get_standards_text_includes_regulations_by_default(self):
        txt = sr.get_standards_text(scheme_name="某深基坑工程")
        for reg in sr.REGULATIONS:
            assert reg in txt, f"法规 {reg} 未进入生成提示词文本"

    def test_include_regulations_false_excludes_all(self):
        txt = sr.get_standards_text(
            scheme_name="某深基坑工程", include_regulations=False)
        for reg in sr.REGULATIONS:
            assert reg not in txt, f"开关关闭后仍出现法规 {reg}"

    def test_regulation_caveat_not_leaked_into_prompt(self):
        """未核实标题的 2024 新规只能以纯文号出现，不得带核实状态字样。"""
        txt = sr.get_standards_text(scheme_name="某深基坑工程")
        for token in ("待核实", "文号已确认", "待核实官方原文"):
            assert token not in txt, f"核实状态字样泄漏进提示词：{token}"
        for doc in ("建质规〔2024〕5号", "建办质〔2024〕63号"):
            lines = [l for l in txt.splitlines() if doc in l]
            assert lines, f"文号 {doc} 未出现在提示词中"
            tail = lines[0].split(doc)[-1]
            assert "（" not in tail and "(" not in tail, (
                f"文号后不应追加带括号的说明：{lines[0]}")

    def test_stale_reference_comments_removed(self):
        """facts_extractor 不得再声称「正文侧按三级策略标注【待补充】」。"""
        assert "三级策略标注" not in _src("services/facts_extractor.py"), (
            "残留过期的占位策略描述")



