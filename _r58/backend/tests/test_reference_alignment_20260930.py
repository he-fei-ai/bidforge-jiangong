"""参考软件能力引入护栏（2026-09-30 第十六轮 · 差距分析报告落地）

本轮依据用户提供的《全功能模块.txt》（易标九篇系列文档）产出
``docs/reference_gap_analysis_20260930.md``，并落地其中**两个 P0 差距**：

============================  ==========================================
文档能力                        本轮落点
============================  ==========================================
§一.4 技术评分要求提取          ``bid_analysis_service.ANALYSIS_ITEMS``
（评分项名称/权重/评分标准/      ⚠️ **2026-10-01 已下线**：软件重新定位为
数据来源 + 覆盖性与权重自检）    「专项施工方案编写软件」，该能力属招投标
                                评审语境，提取项/分组/提示词全部移除；
                                本文件第一组用例已**翻转为反向护栏**。
§三.4 / §五.1 定点替换          ``services/consistency_edits.py``
（old_text/new_text，            —— 唯一命中才替换，找不到/多处命中一律
找不到/多处命中则拒绝）          拒绝；``repair_agent`` 定点优先、
                               整章重写兜底
============================  ==========================================
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest
from app.services import bid_analysis_service as svc
from app.services import consistency_edits as ce


# =========================================================================
# 一、G1 · 技术评分要求 —— ⚠️ 2026-10-01 定位切换后为**反向护栏**
# =========================================================================
# 本类原为第十六轮「新增 techScoring」写的正向断言（共 11 例）。软件重新定位
# 为「建筑工程专项施工方案编写软件」后，技术评分要求属招投标评审语境、已从
# scheme 域移除。此处不删除用例（否则护栏丢失、日后有人照参考软件再加回来
# 就无人拦截），而是**逐条翻转语义**：现在断言「已下线且不可回流」。
# 保留 4 例与定位无关的历史契约护栏（sort_order 递增 / 18 项逐字不变 /
# 两域零交集），它们对任何增删项改动仍然有效。
class TestTechScoringRemoved:
    def test_item_removed_from_scheme_domain(self):
        """技术评分要求不得出现在专项方案编制域提取清单中。"""
        ids = {i["item_id"] for i in svc.ANALYSIS_ITEMS}
        assert "techScoring" not in ids, "techScoring 已随定位切换下线，不得回流"

    def test_item_contract_fields_all_empty(self):
        """清单条目 / 提示词 / 字段字典 / 域归属四个出口必须**全部失效**。

        只删 `ANALYSIS_ITEMS` 而漏删 `_ITEM_PROMPTS` / `_ITEM_FIELDS` 会出现
        「清单里没有该项、但按 id 仍能取到提示词与字段模板」的幽灵入口 ——
        前端拿不到定义，脚本却仍可触发一次注定失败的 AI 调用。
        """
        assert svc.get_item_def("techScoring") is None
        assert svc.get_item_prompt("techScoring") is None
        assert svc.get_item_fields("techScoring") == []
        assert svc.get_item_domain("techScoring") == "", (
            "已下线项必须 fail-closed 返回空串，不得被当成 scheme 域")

    def test_group_removed(self):
        """scoring 分组不得残留（空分组会让前端渲染出一个空白分组卡）。"""
        groups = {g["group"] for g in svc.GROUPS}
        assert "scoring" not in groups
        # 分组 items 必须与 ANALYSIS_ITEMS 严格对应，不得出现空分组
        for g in svc.GROUPS:
            assert g["items"], "分组 %s 的 items 为空" % g["group"]

    def test_source_has_no_scoring_prompt_template(self):
        """静态护栏：提示词模板里不得残留技术评分的 JSON 结构。

        用 AST 之外的文本判据即可（模板是字符串常量），但只查
        「评分项名称 + coverage_note」这对**双关键字**，避免误伤
        bid_response 域里仍在的 `businessScoring` / 评分相关合法文本。
        """
        src = inspect.getsource(svc)
        assert "coverage_note" not in src, "techScoring 提示词模板残留"
        assert "scoring_items" not in src, "techScoring JSON 键结构残留"

    def test_sort_order_appended_not_inserted(self):
        """既有 sort_order 必须保持递增且无重复（历史数据契约）。"""
        orders = [i["sort_order"] for i in svc.ANALYSIS_ITEMS]
        assert orders == sorted(orders), "sort_order 必须保持递增"
        assert len(orders) == len(set(orders)), "sort_order 不得重复"

    def test_existing_18_items_untouched(self):
        """既有 18 项的 item_id 与 sort_order 必须逐字不变。"""
        legacy = {
            "projectBasicInfo": 1, "schemeBasicInfo": 2, "overviewParams": 3,
            "compilationBasis": 4, "siteConditions": 5, "deploymentSchedule": 6,
            "constructionTechnique": 7, "resourceAllocation": 8,
            "safetyMeasures": 9, "qualityAcceptance": 10,
            "emergencyResponse": 11, "calcAndDrawings": 12,
            "materialManagement": 13, "equipmentManagement": 14,
            "constructionDeployment": 15, "constructionProcess": 16,
            "workInterfaceDivision": 17, "engineeringMethods": 18,
        }
        got = {i["item_id"]: i["sort_order"] for i in svc.ANALYSIS_ITEMS}
        for k, v in legacy.items():
            assert got[k] == v, f"{k} 的 sort_order 被改动"
        # 集合必须精确相等：既不允许多项，也不允许缺项
        assert got == legacy

    def test_bid_response_domain_untouched(self):
        """招标响应域（默认关闭）不得被本轮改动波及。"""
        assert len(svc.BID_RESPONSE_ITEMS) == 18
        assert "techRequirements" in {i["item_id"] for i in svc.BID_RESPONSE_ITEMS}
        assert "techScoring" not in {i["item_id"] for i in svc.BID_RESPONSE_ITEMS}

    def test_domains_still_disjoint(self):
        a = {i["item_id"] for i in svc.ANALYSIS_ITEMS}
        b = {i["item_id"] for i in svc.BID_RESPONSE_ITEMS}
        assert not (a & b), "两域 item_id 必须零交集"


# =========================================================================
# 二、G2 · 定点编辑（old_text / new_text）
# =========================================================================
_CONTENT = "本工程总工期为 120 日历天，混凝土强度等级为 C30。\n基底承载力 200kPa。"


class TestUniqueSpan:
    def test_exact_unique_hit(self):
        span = ce.find_unique_span(_CONTENT, "总工期为 120 日历天")
        assert span is not None
        s, e = span
        assert _CONTENT[s:e] == "总工期为 120 日历天"

    def test_multiple_hit_is_rejected(self):
        """文档明确：多处相同内容**拒绝修改**（无法确定改哪一处）。"""
        dup = "AAA规范AAA规范"
        assert ce.find_unique_span(dup, "AAA规范") is None

    def test_not_found(self):
        assert ce.find_unique_span(_CONTENT, "根本不存在的内容") is None

    def test_whitespace_insensitive_unique_hit(self):
        """模型折行时按空白宽松匹配，但仍唯一才命中。"""
        c = "第一行内容\n第二行内容\n第三行"
        assert ce.find_unique_span(c, "第一行内容 第二行内容") is not None

    def test_empty_inputs(self):
        assert ce.find_unique_span("", "x") is None
        assert ce.find_unique_span("x", "") is None


class TestApplyUniqueEdits:
    def test_single_edit_changes_only_that_span(self):
        r = ce.apply_unique_edits(_CONTENT, [
            {"old_text": "总工期为 120 日历天", "new_text": "总工期为 90 日历天"}])
        assert r.applied == 1
        assert "总工期为 90 日历天" in r.content
        # 其余内容逐字保留（这是与整章重写的核心差异）
        assert "混凝土强度等级为 C30" in r.content
        assert "基底承载力 200kPa" in r.content

    def test_ambiguous_edit_rejected_content_unchanged(self):
        c = "工期 120 天，工期 120 天"
        r = ce.apply_unique_edits(c, [{"old_text": "工期 120 天",
                                       "new_text": "工期 90 天"}])
        assert r.applied == 0
        assert r.content == c
        assert r.rejected and r.rejected[0][1] == "ambiguous"

    def test_not_found_rejected(self):
        r = ce.apply_unique_edits(_CONTENT, [{"old_text": "完全不存在的一段文字",
                                              "new_text": "X"}])
        assert r.applied == 0 and r.rejected[0][1] == "not_found"

    def test_too_short_rejected(self):
        """过短的 old_text 几乎必然多处命中，直接拒绝省一次 AI 重试。"""
        r = ce.apply_unique_edits(_CONTENT, [{"old_text": "工期",
                                              "new_text": "总工期"}])
        assert r.applied == 0 and r.rejected[0][1] == "too_short"

    def test_empty_fields_rejected(self):
        r = ce.apply_unique_edits(_CONTENT, [
            {"old_text": "   ", "new_text": "x"},
            {"old_text": "总工期为 120 日历天", "new_text": "  "},
        ])
        assert r.applied == 0
        assert {reason for _o, reason in r.rejected} == {"empty", "empty_new"}

    def test_multiple_edits_applied_in_order(self):
        r = ce.apply_unique_edits(_CONTENT, [
            {"old_text": "总工期为 120 日历天", "new_text": "总工期为 90 日历天"},
            {"old_text": "混凝土强度等级为 C30", "new_text": "混凝土强度等级为 C35"},
        ])
        assert r.applied == 2
        assert "90 日历天" in r.content and "C35" in r.content

    def test_non_dict_entries_skipped(self):
        r = ce.apply_unique_edits(_CONTENT, ["junk", None, 42])
        assert r.applied == 0 and r.content == _CONTENT

    def test_max_edits_cap(self):
        """AI 返回上百条时必须截断（防止整章被改面目全非）。"""
        anchors = [f"第{i}段落内容用于测试唯一性锚点" for i in range(ce.MAX_EDITS + 10)]
        edits = [{"old_text": a, "new_text": "X"} for a in anchors]
        r = ce.apply_unique_edits(" ".join(anchors), edits)
        assert r.applied <= ce.MAX_EDITS

    def test_empty_content_and_edits(self):
        assert ce.apply_unique_edits("", [{"old_text": "a", "new_text": "b"}]).applied == 0
        assert ce.apply_unique_edits(_CONTENT, []).content == _CONTENT

    def test_result_to_dict_shape(self):
        r = ce.apply_unique_edits(_CONTENT, [{"old_text": "不存在的内容在这里",
                                              "new_text": "X"}])
        d = r.to_dict()
        assert set(d) == {"applied", "rejected"}
        assert "reason" in d["rejected"][0]



# =========================================================================
# 三、G2 接线：定点优先 + 整章重写兜底
# =========================================================================
class TestRepairWiring:
    def test_repair_section_tries_edits_first(self):
        src = inspect.getsource(
            __import__("app.services.repair_agent", fromlist=["x"]).repair_section)
        assert "collect_repair_edits" in src
        # 兜底路径必须仍在（模型不支持 JSON 时不让修复能力整体失效）
        assert "consistency_repair_system" in src

    def test_edits_path_is_fail_soft(self):
        src = inspect.getsource(
            __import__("app.services.repair_agent", fromlist=["x"]).repair_section)
        assert "except Exception" in src
        assert "回落整章重写" in src

    def test_chat_fn_is_injected(self):
        """必须注入 repair_agent 的模块级引用，否则既有单测的 monkeypatch 拦不到。"""
        src = inspect.getsource(
            __import__("app.services.repair_agent", fromlist=["x"]).repair_section)
        assert "chat_fn=chat_with_fallback" in src

    def test_prompts_registered(self):
        from app.services.ai.prompts._registry import render
        sys_p = render("consistency_repair_edits_system")
        usr_p = render("consistency_repair_edits_user",
                       global_facts="F", authoritative_sources="S",
                       section_id="s1", section_title="T",
                       conflicts_in_section="[]")
        assert "old_text" in sys_p and "new_text" in sys_p
        assert "逐字抄写" in sys_p, "必须要求模型逐字抄写原文（否则无法定位）"
        assert "唯一" in sys_p, "必须告知唯一命中约束"
        assert "s1" in usr_p and "T" in usr_p

    def test_validate_edits_fail_closed(self):
        assert ce._validate_edits({"edits": []}) == []
        for bad in (None, "x", {}, {"edits": None}, {"edits": "no"}):
            assert ce._validate_edits(bad) != []

    def test_collect_falls_back_on_bad_json(self):
        """模型返回非 JSON → applied=0，由调用方回落到整章重写。"""
        async def bad_chat(messages, **kw):
            return "这不是 JSON"

        res = asyncio.run(ce.collect_repair_edits(
            section_id="s1", section_title="T", section_content=_CONTENT,
            conflicts_in_section=[], facts="", sources="", chat_fn=bad_chat))
        assert res.applied == 0
        assert res.content == _CONTENT

    def test_collect_falls_back_on_ai_exception(self):
        async def boom(messages, **kw):
            raise RuntimeError("provider down")

        res = asyncio.run(ce.collect_repair_edits(
            section_id="s1", section_title="T", section_content=_CONTENT,
            conflicts_in_section=[], facts="", sources="", chat_fn=boom))
        assert res.applied == 0 and res.content == _CONTENT

    def test_collect_applies_valid_edits(self):
        async def good_chat(messages, **kw):
            return json.dumps({"edits": [
                {"old_text": "总工期为 120 日历天",
                 "new_text": "总工期为 90 日历天"}]}, ensure_ascii=False)

        res = asyncio.run(ce.collect_repair_edits(
            section_id="s1", section_title="T", section_content=_CONTENT,
            conflicts_in_section=[], facts="", sources="", chat_fn=good_chat))
        assert res.applied == 1
        assert "90 日历天" in res.content
        assert "C30" in res.content, "未冲突内容必须原样保留"

