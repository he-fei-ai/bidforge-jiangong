"""F-CONTENT-STANDARD · 模糊生成改造 + 内部标记（2026-10-01）

两条主线：
一、正文必须完整生成 —— 不留占位标记、不留空（八类规则表为唯一事实源，
   提示词与校验共用同一份文案与判据）。
二、内部标记（可追溯性）—— 只落库、不落正文：标记在 ``trace`` 键里，
   ``sections.content`` 与导出 DOCX 均查不到。
"""
import json

import pytest

from app.services.content_fuzzy import (
    BARE_PLACEHOLDER_PHRASES, FUZZY_CATEGORIES, FUZZY_CATEGORY_ORDER,
    HEDGE_PREFIX_RE, HEDGE_SUFFIX_RE, LIMIT_PHRASES,
    MISSING_REVEAL_PHRASES, VAGUE_STATEMENT_RULES,
    build_data_availability_block, build_fuzzy_rules_block,
    build_fuzzy_rules_for_standard, build_generation_strategy_block,
    build_no_placeholder_block, detect_fuzzy_expressions,
    scan_fabricated_dates, scan_missing_reveal, scan_placeholder_marks,
    scan_vague_statements,
)
from app.services.content_standard import (
    FUZZY, PRECISE, _BARE_PHRASES, _HEDGE_PREFIX_RE, _HEDGE_SUFFIX_RE,
    _LIMIT_PHRASES, _empty_report, build_continue_hint, build_facts_header,
    build_system_block, build_user_block, extract_model_tokens,
    extract_number_tokens, strip_code_blocks, standard_report,
)
from app.services.content_trace import (
    GENERATION_FUZZY, GENERATION_PLACEHOLDER, GENERATION_PRECISE,
    TRACE_ITEM_KEYS, build_trace, collect_scheme_trace,
)


def _fact(group="参数", title="基坑深度", content="基坑深度 12.5m"):
    """构造 5 元组事实行（gt, title, content, confidence, chapter）。"""
    return (group, title, content, 0.9, "technique")


def _trace_inputs(body, facts):
    """构造 build_trace 的事实侧 / 正文侧 token（复用校验器同一抽取器）。"""
    fnum, fmod = [], []
    for row in facts:
        title, ftext = str(row[1] or ""), str(row[2] or "")
        joined = f"{title} {ftext}"
        fnum += [{"value": t["value"], "unit": t["unit"], "title": title}
                 for t in extract_number_tokens(joined)]
        fmod += [{"token": t["token"], "title": title}
                 for t in extract_model_tokens(joined)]
    scanned = strip_code_blocks(body)
    return dict(scanned=scanned, fact_numbers=fnum, fact_models=fmod,
                body_numbers=extract_number_tokens(scanned),
                body_models=extract_model_tokens(scanned),
                fuzzy_expressions=detect_fuzzy_expressions(scanned))


def _trace_row(section_id="a", section_title="第一章",
               gtype=GENERATION_FUZZY, category="number", severity="warn"):
    """构造一条合法 trace 条目（键集合严格等于 TRACE_ITEM_KEYS）。"""
    return dict(zip(TRACE_ITEM_KEYS, [
        "tr-0001", section_id, section_title, "L1 P1", 0, 5, "…约 13m…",
        gtype, category, "行业惯例与设计允许范围", "事实未给出该数值", severity,
    ]))


class TestFuzzyCategoryTable:
    """模糊生成规则表（八类）是文案与标记的唯一事实源。

    2026-10-02（第二十六轮）：按需求文档「模糊生成规则」补齐材料规格类/工序流程类，
    六类 → 八类（数值/名称/时间/数量/承诺/技术参数/材料规格/工序流程）。
    """

    def test_eight_categories_with_all_fields(self):
        assert len(FUZZY_CATEGORIES) == 8
        assert set(FUZZY_CATEGORY_ORDER) == set(FUZZY_CATEGORIES)
        for key, c in FUZZY_CATEGORIES.items():
            assert c["label"], key
            assert c["precise"] and c["fuzzy"] and c["forbid"], key
            # data_source / reason 供内部标记 trace 使用，必须齐全
            assert c["data_source"], f"{key} 缺 data_source"
            assert c["reason"], f"{key} 缺 reason"

    def test_category_keys_are_stable_vocabulary(self):
        assert set(FUZZY_CATEGORIES) == {"number", "name", "time", "quantity",
                                          "promise", "tech", "material", "process"}

    def test_every_category_forbids_fabrication(self):
        for c in FUZZY_CATEGORIES.values():
            assert "禁止" in c["forbid"] or "严禁" in c["forbid"]


class TestFuzzyRulesBlockText:
    """提示词文案必须完全由规则表渲染 —— 防止文案与判据分叉。"""

    def test_block_contains_every_category_row(self):
        s = build_fuzzy_rules_block()
        for key, c in FUZZY_CATEGORIES.items():
            assert c["label"] in s, key
            assert c["precise"] in s, f"{key}.precise 未出现在提示词"
            assert c["fuzzy"] in s, f"{key}.fuzzy 未出现在提示词"
            assert c["forbid"] in s, f"{key}.forbid 未出现在提示词"

    def test_block_forbids_placeholder_and_fabrication(self):
        s = build_fuzzy_rules_block()
        for phrase in ("完整生成", "占位标记", "空话"):
            assert phrase in s, phrase

    def test_no_placeholder_block_lists_all_three_kinds(self):
        s = build_no_placeholder_block()
        for phrase in ("【待补充】", "此处省略", "由于资料不足", "严禁"):
            assert phrase in s, phrase

    def test_generation_strategy_block_has_three_tiers(self):
        s = build_generation_strategy_block()
        for phrase in ("有明确数据", "有部分数据", "无明确数据", "完整正文"):
            assert phrase in s, phrase

    def test_data_availability_block_empty_inputs_return_empty(self):
        assert build_data_availability_block(None, None) == ""
        assert build_data_availability_block([], []) == ""

    def test_data_availability_block_renders_both_sides(self):
        s = build_data_availability_block(["基坑深度 12.5m"], ["地下水位"])
        assert "基坑深度 12.5m" in s and "地下水位" in s and "明确数据" in s

    def test_for_standard_includes_strategy_and_ban_in_both_modes(self):
        for std in (PRECISE, FUZZY):
            s = build_fuzzy_rules_for_standard(std)
            for phrase in ("模糊生成规则", "分级生成策略", "禁止占位标记"):
                assert phrase in s, f"{std}: {phrase}"


class TestCriteriaAliases:
    """判据下沉后的别名一致性 —— 两处对象必须是同一个，改一处即全局生效。"""

    def test_private_aliases_are_the_same_objects(self):
        assert _HEDGE_PREFIX_RE is HEDGE_PREFIX_RE
        assert _HEDGE_SUFFIX_RE is HEDGE_SUFFIX_RE
        assert _BARE_PHRASES is BARE_PLACEHOLDER_PHRASES
        assert _LIMIT_PHRASES is LIMIT_PHRASES

    def test_hedge_prefix_still_excludes_contract_word(self):
        assert HEDGE_PREFIX_RE.search("约 13m")
        assert HEDGE_PREFIX_RE.search("大概5天")
        assert not HEDGE_PREFIX_RE.search("合约12")
        assert not HEDGE_PREFIX_RE.search("约定12")

    def test_hedge_suffix_matches_range_words(self):
        assert HEDGE_SUFFIX_RE.search("12.5 米左右")
        assert HEDGE_SUFFIX_RE.search("300 上下")


class TestScanPlaceholderMarks:
    def test_formatted_mark_detected(self):
        hits = scan_placeholder_marks("地下水位见【待补充：地勘报告】。")
        assert len(hits) == 1
        assert hits[0]["kind"] == "formatted"
        assert "待补充" in hits[0]["mark"]
        assert hits[0]["char_start"] < hits[0]["char_end"]

    def test_bare_and_extended_marks_detected(self):
        for mark in ("【待定】", "TBD", "N/A"):
            assert scan_placeholder_marks(f"该项为 {mark}。"), mark

    def test_empty_bracket_detected(self):
        assert scan_placeholder_marks("参数【】。")

    def test_empty_and_non_str_input(self):
        assert scan_placeholder_marks("") == []
        assert scan_placeholder_marks(None) == []
        assert scan_placeholder_marks(123) == []

    def test_no_false_positive_on_normal_text(self):
        assert scan_placeholder_marks("基坑深度 12.5m，按设计要求施工。") == []


class TestScanVagueStatements:
    @pytest.mark.parametrize("text", [
        "此处省略。", "此处略。", "具体细节另见。", "详见附件。",
        "详见相关章节。", "后续补充。", "待后续完善。", "另有章节介绍。",
    ])
    def test_each_vague_rule_has_a_hit(self, text):
        hits = scan_vague_statements(text)
        assert hits, text
        assert hits[0]["kind"] == "vague" and hits[0]["label"]

    def test_bare_phrase_with_anchor_is_not_vague(self):
        """有实质锚点（数值+单位）的「按合同要求」属正常条件式写法，不报。"""
        assert scan_vague_statements("设备按合同要求进场，采用 800mm 灌注桩。") == []
        assert scan_vague_statements("按合同要求施工，工期 450 日历天。") == []

    @pytest.mark.parametrize("phrase", BARE_PLACEHOLDER_PHRASES)
    def test_each_bare_phrase_flags_hanging_sentence(self, phrase):
        """每一条空泛指代在「整句悬空」时都必须报。"""
        hits = scan_vague_statements(f"该项{phrase}。")
        assert hits, phrase
        assert hits[0]["label"] == "悬空空泛指代"

    @pytest.mark.parametrize("phrase", BARE_PLACEHOLDER_PHRASES)
    def test_each_bare_phrase_exempt_when_anchored(self, phrase):
        """同一条指代在「句中有数值+单位锚点」时不得报（防满篇误报）。"""
        assert scan_vague_statements(f"该项{phrase}，控制值 800mm。") == [], phrase

    @pytest.mark.parametrize("anchor", ["按 GB 50300 验收", "不小于 0.35"])
    def test_norm_and_limit_anchor_exempt(self, anchor):
        """规范编号 / 限值措辞也算实质锚点。"""
        assert scan_vague_statements(f"质量要求：{anchor}。") == []

    def test_bare_phrase_hanging_is_vague(self):
        """整句无锚点的「按合同要求」才是空话。"""
        hits = scan_vague_statements("设备按合同要求进场。")
        assert hits and hits[0]["label"] == "悬空空泛指代"

    def test_no_false_positive_on_normal_sentence(self):
        assert scan_vague_statements("基坑采用灌注桩支护，桩径 800mm。") == []

    def test_empty_input(self):
        assert scan_vague_statements("") == []
        assert scan_vague_statements(None) == []


class TestScanMissingReveal:
    def test_each_reveal_phrase_detected(self):
        for phrase in MISSING_REVEAL_PHRASES:
            assert scan_missing_reveal(f"说明：{phrase}，按常规处理。"), phrase

    def test_overlap_contains_dedup(self):
        """「由于资料不足」与「资料不足」同时命中时只保留最长的一条。"""
        hits = scan_missing_reveal("由于资料不足，参数按行业惯例表述。")
        marks = [h["mark"] for h in hits]
        assert "由于资料不足" in marks
        assert marks.count("资料不足") == 0

    def test_normal_text_clean(self):
        assert scan_missing_reveal("主体结构完成后浇筑混凝土。") == []


class TestScanFabricatedDates:
    def test_full_calendar_date_without_fact_is_hit(self):
        hits = scan_fabricated_dates("进度按 2026年8月15日 安排。", "")
        assert len(hits) == 1 and "2026" in hits[0]["mark"]

    def test_date_present_in_fact_is_exempt(self):
        text = "主体结构 2026年8月15日 完成。"
        facts = "关键节点：主体结构 2026年8月15日 完成"
        assert scan_fabricated_dates(text, facts) == []

    def test_partial_date_not_flagged(self):
        """只有年月（无日）不判编造 —— 方案正文常写「2026 年 3 月开工」。"""
        assert scan_fabricated_dates("计划 2026 年 3 月开工。", "") == []

    def test_no_false_positive_on_numbers(self):
        assert scan_fabricated_dates("混凝土强度 C30，厚 200mm。", "") == []


class TestDetectFuzzyExpressions:
    def test_category_and_span_present(self):
        body = "基坑深度约 13m，塔吊 300 左右。"
        for h in detect_fuzzy_expressions(body):
            assert h["category"] in FUZZY_CATEGORIES
            assert 0 <= h["char_start"] < h["char_end"] <= len(body)

    def test_same_start_positions_are_unique(self):
        starts = [h["char_start"] for h in detect_fuzzy_expressions("约 12m 以内")]
        assert len(starts) == len(set(starts))

    def test_empty_input(self):
        assert detect_fuzzy_expressions("") == []


class TestPromptBlocksFuzzyFill:
    """生成标准文案：禁止占位指令（历史回归护栏）。"""

    def test_precise_block_points_to_fuzzy_rules_not_placeholder(self):
        s = build_system_block(PRECISE)
        assert "精准内容" in s and "原样引用" in s
        assert "模糊生成规则" in s
        assert "【待补充：参数名】" not in s

    def test_fuzzy_block_points_to_fuzzy_rules_not_placeholder(self):
        s = build_system_block(FUZZY)
        assert "模糊内容" in s and "±10%" in s
        assert "模糊生成规则" in s
        assert "【待补充：参数名】" not in s

    def test_continue_hint_forbids_placeholder(self):
        for std in (PRECISE, FUZZY):
            h = build_continue_hint(std)
            assert "占位标记" in h and "【待补充" not in h

    def test_facts_header_forbids_placeholder_in_both_modes(self):
        for std in (PRECISE, FUZZY):
            h = build_facts_header(std)
            assert "占位标记" in h and "空话" in h
            assert h.endswith("：\n")
            assert "使用占位符或条件式表述" not in h

    def test_user_block_has_no_placeholder_instruction(self):
        for std in (PRECISE, FUZZY):
            assert "【待补充：参数名】" not in build_user_block(std)


class TestRegistryPromptsInjected:
    """AI 系统提示词必须实际包含模糊生成段落（接线护栏）。"""

    def test_generation_system_injected(self):
        from app.services.ai.prompts._registry import get_default_prompt
        t = get_default_prompt("content_generation_system")
        for phrase in ("模糊生成规则", "分级生成策略",
                       "禁止占位标记与空话", "数据真实性红线"):
            assert phrase in t, phrase
        assert "<<FUZZY_FILL>>" not in t
        assert "【待补充：参数名】" not in t

    def test_continue_system_injected(self):
        from app.services.ai.prompts._registry import get_default_prompt


# ============================================================
# 内部标记（可追溯性）：build_trace / collect_scheme_trace
# ============================================================
class TestBuildTrace:

    def test_trace_structure_and_summary_keys(self):
        tr = build_trace(scanned="", standard=PRECISE,
                         section_id="s1", section_title="基坑工程")
        assert tr["available"] is True
        for k in ("available", "degraded", "standard", "section_id",
                  "section_title", "items", "summary"):
            assert k in tr, k
        for k in ("total", "precise_count", "fuzzy_count", "warn_count",
                  "error_count", "placeholder_residue_count", "by_category",
                  "by_reason", "truncated"):
            assert k in tr["summary"], k

    def test_precise_number_backed_by_fact(self):
        tr = build_trace(standard=PRECISE, section_id="s1",
                         section_title="基坑工程",
                         **_trace_inputs("基坑深度 12.5m。", [_fact()]))
        assert tr["summary"]["precise_count"] >= 1
        it = next(i for i in tr["items"] if i["generation_type"] == GENERATION_PRECISE)
        assert "全局事实" in it["data_source"]
        assert it["severity"] == "info"
        assert it["excerpt"] and it["position"]
        assert it["section_id"] == "s1"

    def test_unbacked_number_is_warn(self):
        tr = build_trace(standard=FUZZY,
                         **_trace_inputs("基坑深度 13.2m。",
                                         [_fact("设备", "桩基", "桩径 800mm")]))
        fuzzy = [i for i in tr["items"] if i["generation_type"] == GENERATION_FUZZY]
        assert fuzzy
        assert any(i["severity"] == "warn" for i in fuzzy)
        assert any("编造" in i["fuzzy_reason"] for i in fuzzy)

    def test_unbacked_model_is_fuzzy(self):
        tr = build_trace(standard=FUZZY,
                         **_trace_inputs("采用 QTZ63 塔吊。",
                                         [_fact("设备", "塔吊", "塔吊 QTZ80")]))
        assert any(i["generation_type"] == GENERATION_FUZZY for i in tr["items"])

    def test_placeholder_residue_is_error(self):
        body = "地下水位见【待补充：地勘报告】。"
        inp = _trace_inputs(body, [])
        inp["placeholder_hits"] = scan_placeholder_marks(inp["scanned"])
        tr = build_trace(standard=PRECISE, **inp)
        residues = [i for i in tr["items"] if i["generation_type"] == GENERATION_PLACEHOLDER]
        assert len(residues) == 1
        assert residues[0]["severity"] == "error"
        assert tr["summary"]["placeholder_residue_count"] == 1
        assert tr["summary"]["error_count"] == 1

    def test_item_keys_are_exact(self):
        """键集合严格锁定，防止字段漂移破坏下游消费方。"""
        tr = build_trace(standard=PRECISE, **_trace_inputs("基坑深度 12.5m。", [_fact()]))
        for it in tr["items"]:
            assert tuple(sorted(it)) == tuple(sorted(TRACE_ITEM_KEYS))
            assert it["id"].startswith("tr-")

    def test_overlap_dedup_number_inside_fuzzy(self):
        """「约 13m」只算一条模糊，不再重复记「13m 无事实支撑」。"""
        tr = build_trace(standard=FUZZY, **_trace_inputs("基坑深度约 13m。", []))
        assert len([i for i in tr["items"] if i["category"] == "number"]) == 1

    def test_max_items_cap_and_truncated(self):
        body = "\n".join(f"参数{i}约 {i}m。" for i in range(1, 300))
        tr = build_trace(standard=FUZZY, **_trace_inputs(body, []))
        assert len(tr["items"]) <= 200
        assert tr["summary"]["truncated"] is True

    def test_degraded_on_bad_input(self):
        tr = build_trace(scanned=None, standard="bad")
        assert tr["available"] is True
        assert isinstance(tr["items"], list)

    def test_empty_text_gives_empty_items(self):
        assert build_trace(scanned="")["summary"]["total"] == 0


class TestCollectSchemeTrace:

    def test_aggregates_across_sections(self):
        secs = [
            {"id": "a", "title": "第一章", "last_generation_report": json.dumps(
                {"trace": {"items": [_trace_row()], "summary": {
                    "total": 1, "precise_count": 0, "fuzzy_count": 1,
                    "warn_count": 1, "error_count": 0,
                    "placeholder_residue_count": 0,
                    "by_category": {"number": 1}, "by_reason": {},
                    "truncated": False}}})},
            {"id": "b", "title": "第二章", "last_generation_report": ""},
        ]
        out = collect_scheme_trace(secs)
        assert out["section_count"] == 1
        assert out["total"] == 1
        assert out["precise_count"] == 0
        assert out["fuzzy_count"] == 1
        assert out["by_category"]["number"] == 1
        assert out["sections"][0]["section_id"] == "a"
        assert out["sections"][0]["total"] == 1

    def test_item_cap_enforced(self):
        secs = [{"id": str(i), "title": f"第{i}章",
                 "last_generation_report": json.dumps(
                     {"trace": {"items": [_trace_row(section_id=str(i))],
                                "summary": {"total": 1, "precise_count": 0,
                                            "fuzzy_count": 1, "warn_count": 1,
                                            "error_count": 0,
                                            "placeholder_residue_count": 0,
                                            "by_category": {"number": 1},
                                            "by_reason": {}, "truncated": False}}})}
                for i in range(10)]
        out = collect_scheme_trace(secs, item_cap=5)
        assert out["total"] == 5
        assert out["truncated"] is True
        assert len(out["sections"]) == 10

    def test_bad_rows_are_skipped(self):
        out = collect_scheme_trace(["not-a-dict", None, {}])
        assert out["skipped"] >= 2
        assert out["total"] == 0



# ============================================================
# standard_report：新 issue 类型 + trace 输出
# ============================================================
class TestReportNewIssueTypes:

    def test_placeholder_mark_is_error_issue(self):
        rep = standard_report("地下水位见【待补充：地勘报告】。", PRECISE, [_fact()])
        marks = [i for i in rep["issues"] if i["type"] == "placeholder_mark"]
        assert len(marks) == 1
        assert marks[0]["severity"] == "error"
        assert marks[0]["excerpt"]
        assert rep["stats"]["placeholder_marks"] == 1
        assert rep["passed"] is False

    def test_vague_statement_is_warning_issue(self):
        rep = standard_report("此处省略。", PRECISE, [])
        hits = [i for i in rep["issues"] if i["type"] == "vague_statement"]
        assert hits and hits[0]["severity"] == "warning"
        assert rep["stats"]["vague_statements"] == 1

    def test_missing_reveal_is_warning_issue(self):
        rep = standard_report("由于资料不足，参数按常规取值。", PRECISE, [])
        hits = [i for i in rep["issues"] if i["type"] == "missing_reveal"]
        assert hits and hits[0]["severity"] == "warning"
        assert rep["stats"]["missing_reveals"] == 1

    def test_fabricated_date_is_error_issue(self):
        rep = standard_report("进度按 2026年8月15日 安排。", PRECISE, [])
        hits = [i for i in rep["issues"] if i["type"] == "fabricated_date"]
        assert hits and hits[0]["severity"] == "error"
        assert rep["stats"]["fabricated_dates"] == 1

    def test_fact_backed_date_not_fabricated(self):
        facts = [_fact("节点", "结构完成时间", "主体结构 2026年8月15日 完成")]
        rep = standard_report("进度按 2026年8月15日 安排。", PRECISE, facts)
        assert not [i for i in rep["issues"] if i["type"] == "fabricated_date"]

    def test_new_stats_keys_present_in_both_paths(self):
        rep = standard_report("基坑深度 12.5m。", PRECISE, [_fact()])
        for k in ("placeholders", "placeholder_marks", "vague_statements",
                  "missing_reveals", "fabricated_dates", "fuzzy_expressions",
                  "trace_items", "trace_precise", "trace_fuzzy"):
            assert k in rep["stats"], k
        # 降级路径同样给出完整字段（防前端分支处理）
        er = _empty_report(PRECISE)
        assert er["trace_available"] is False
        assert er["trace"]["available"] is False
        for k in ("placeholders", "placeholder_marks", "vague_statements",
                  "missing_reveals", "fabricated_dates", "fuzzy_expressions",
                  "trace_items", "trace_precise", "trace_fuzzy", "degraded"):
            assert k in er["stats"], k

    def test_report_carries_trace(self):
        rep = standard_report("基坑深度 12.5m。", PRECISE, [_fact()])
        assert rep["trace_available"] is True
        assert rep["trace"]["items"]
        assert rep["stats"]["trace_items"] == len(rep["trace"]["items"])

    def test_section_id_and_title_flow_into_trace(self):
        rep = standard_report("基坑深度 12.5m。", PRECISE, [_fact()],
                              section_id="s9", section_title="基坑工程")
        assert rep["trace"]["section_id"] == "s9"
        assert rep["trace"]["section_title"] == "基坑工程"
        for it in rep["trace"]["items"]:
            assert it["section_id"] == "s9"
            assert it["section_title"] == "基坑工程"


# ============================================================
# 关键不变式：标记只落库、不落正文（导出文档看不到）
# ============================================================
class TestTraceNeverLeaksIntoContent:

    def test_content_text_unchanged_after_report(self):
        """报告（含 trace）派生自正文但绝不修改正文 —— 落库正文即原始正文。"""
        body = "基坑深度 12.5m，基坑支护采用灌注桩。地下水位见【待补充：地勘报告】。"
        rep = standard_report(body, PRECISE, [_fact()], section_id="s1",
                              section_title="基坑工程")
        assert rep["trace"]["items"]
        for leak in ("generation_type", "fuzzy_reason", "data_source",
                     "tr-", "placeholder_residue", "char_start"):
            assert leak not in body

    def test_marker_strings_not_written_to_content(self):
        body = "设备按合同要求进场。此处省略。由于资料不足，参数按常规取值。"
        rep = standard_report(body, FUZZY, [])
        assert [i["type"] for i in rep["issues"]]
        for leak in ("生成标记", "内部标记", "trace", "fuzzy_reason"):
            assert leak not in body

    def test_export_only_reads_content_field(self):
        """导出 DOCX 只读 sections.content；标记位于 last_generation_report。"""
        import inspect

        from app.routers import export as export_router
        export_src = inspect.getsource(export_router)
        assert "last_generation_report" not in export_src

    def test_report_trace_endpoint_is_read_only(self):
        """新增端点只读 last_generation_report，不写正文。"""
        import inspect
        from app.routers import sections as sec
        src = inspect.getsource(sec.scheme_report_trace)
        assert "SELECT" in src
        assert "UPDATE" not in src and "INSERT" not in src


# ============================================================
# 历史回归：数值/型号提取不受本轮改动影响
# ============================================================
class TestLegacyExtractorsUnchanged:

    def test_number_tokens(self):
        toks = extract_number_tokens("基坑深度 12.5m，厚 200mm，工期 450 日历天。")
        vals = {t["value"] for t in toks}
        assert 12.5 in vals and 200 in vals and 450 in vals

    def test_model_tokens(self):
        toks = extract_model_tokens("采用 QTZ80 塔吊，混凝土 C30。")
        assert any("QTZ80" in t["token"] for t in toks)
        assert any(t["token"] == "C30" for t in toks)

    def test_code_blocks_stripped(self):
        out = strip_code_blocks("正文 ```mermaid\nA-->B|13m|\n``` 后续")
        assert "mermaid" not in out and "后续" in out


    def test_prompt_markers_replaced_at_registration(self):
        from app.services.ai.prompts import content as c
        assert "模糊生成规则" in c._FUZZY_FILL_SECTION

