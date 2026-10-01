"""正文生成 · 章节运行时纯函数单元测试（2026-09-28）

钉住 `services/content_runtime.py` —— 这些函数自 `sse_handlers.generate_content`
闭包中**逐行搬迁**而来（无逻辑改动），本测试保证：
1. user 上下文的每段「【标签】：」结构都存在且传参正确；
2. 续写 messages 在 under / 达标两种字数口径下文案区别正确；
3. 轮次判定与 token 上限折算与旧实现逐字一致。

回归护栏：若有人改动搬运后的函数导致与 generate_content 行为漂移，
正文生成提示词与续写请求链路的既有测试（test_content_*）也应同时暴露。
"""
from app.services.content_runtime import (
    build_continuation_messages,
    build_chapter_user_content,
    continue_max_tokens,
    should_continue_round,
)
from app.services.content_standard import (
    build_continue_hint,
    build_facts_header,
    build_user_block,
)


def _leaf(**ov):
    base = {
        "id": "s1", "title": "土方开挖",
        "description": "基坑土方开挖与运输组织",
    }
    base.update(ov)
    return base


def _base_kwargs(**ov):
    kw = {
        "scheme": {"name": "XX项目施工方案", "type": "深基坑", "generation_standard": ""},
        "project_brief": "项目概述文本。",
        "content_scope": "深基坑支护工程",
        "parent_chain": "1 工程概况",
        "parent_points": ["- 工程概况：本章概述内容", "- 总体部署：总体安排说明"],
        "sibling_lines": "- 2.1 基坑排水\n- 2.2 井点降水",
        "section_number": "2.3",
        "leaf": _leaf(),
        "word_budget": 1500,
        "word_budget_hint": "",
        "prev_sibling_summary": "前序同级章节结尾参考段落。",
        "facts_text": "### 工程概况\n- **基坑深度**: 12.5m\n",
        "eff_standard": "precise",
        "knowledge_text": "企业标准模板 K-1 段落。",
    }
    kw.update(ov)
    return kw
class TestBuildChapterUserContent:
    def test_assembles_all_sections(self):
        out = build_chapter_user_content(**_base_kwargs())
        assert "【方案名称】：XX项目施工方案" in out
        assert "【方案类型】：深基坑" in out
        assert "【项目概述】：项目概述文本。" in out
        assert "【方案名称主要施工内容】（本章内容必须服务于其中对应的施工内容项；与本章无关的项不得写入）：\n深基坑支护工程" in out
        assert "【上级章节链】：1 工程概况" in out
        assert "【上级章节要点】：\n- 工程概况：本章概述内容\n- 总体部署：总体安排说明" in out
        assert "【同级章节（请避免内容重复）】：\n- 2.1 基坑排水\n- 2.2 井点降水" in out
        assert "【当前章节编号】：2.3" in out
        assert "【当前章节】：土方开挖 — 基坑土方开挖与运输组织" in out
        # word_budget_hint 为空 → 回落 "N字"（与旧实现 `hint or f'{budget}字'` 逐字一致）
        assert "【目标字数】：1500字" in out
        assert "【前序同级章节结尾参考（衔接风格，勿重复）】：前序同级章节结尾参考段落。" in out
        assert "【全局事实变量（唯一可信数据源）】：" in out
        assert "- **基坑深度**: 12.5m" in out
        assert "【项目知识库素材】：" in out
        assert "企业标准模板 K-1 段落。" in out
        # 末尾追加生成标准 user 块（精准）
        assert out.rstrip().endswith(build_user_block("precise").rstrip())

    def test_facts_header_follows_standard(self):
        out = build_chapter_user_content(**_base_kwargs(eff_standard="fuzzy"))
        assert build_facts_header("fuzzy") in out
        assert build_user_block("fuzzy") in out
        # 模糊模式下使用方向参考措辞
        assert "方向参考" in out

    def test_empty_scope_and_knowledge(self):
        empties = {k: "" for k in ("content_scope", "knowledge_text", "facts_text",
                                   "prev_sibling_summary", "sibling_lines")}
        out = build_chapter_user_content(**{**_base_kwargs(), **empties})
        assert "【方案名称主要施工内容】" not in out
        assert "【项目知识库素材】：" not in out
        # 注意：标准 user 块文案含「上方【全局事实变量】中…」，必须用带标签的
        #    完整头「（唯一可信数据源）」判定事实段未注入。
        assert "【全局事实变量（唯一可信数据源）】：" not in out
        assert "【同级章节（请避免内容重复）】：（无）" in out
        # 目标字数提示仍存在
        assert "【目标字数】：1500字" in out


class TestBuildContinuationMessages:
    def test_under_budget_instruction(self):
        msgs = build_continuation_messages(
            user_ctx="【当前章节】：土方开挖",
            cont_tail="……结尾段落",
            wc=800, word_budget=1500,
            cont_prompt="续写系统提示",
            eff_standard="precise",
        )
        assert msgs[0] == {"role": "system", "content": "续写系统提示"}
        assert msgs[1] == {"role": "user", "content": "【当前章节】：土方开挖"}
        assert msgs[2]["role"] == "assistant"
        assert "（前文已省略，以下为正文结尾部分）\n……结尾段落" == msgs[2]["content"]
        assert "当前字数800，目标1500字，请继续补充。" in msgs[3]["content"]
        assert "补充后总字数上限为 1650 字" in msgs[3]["content"]
        # 精准模式提醒
        assert build_continue_hint("precise") in msgs[3]["content"]

    def test_at_or_over_budget_instruction(self):
        msgs = build_continuation_messages(
            user_ctx="【当前章节】：土方开挖",
            cont_tail="……结尾",
            wc=1600, word_budget=1500,
            cont_prompt="续写系统提示",
            eff_standard="fuzzy",
        )
        assert "已达到目标字数" in msgs[3]["content"]
        assert "但**补充后总字数不得超过 1650 字**" in msgs[3]["content"]
        assert "不要重复前文，不要输出图表代码块。" in msgs[3]["content"]
        assert build_continue_hint("fuzzy") in msgs[3]["content"]


class TestShouldContinueRound:
    def test_under_threshold_continues(self):
        assert should_continue_round(wc=1000, word_budget=1500,
                                     continue_count=0, min_passes=1, max_rounds=3) is True

    def test_at_threshold_with_min_passes(self):
        # wc == 1200 = budget*0.8 不触发 under，但 min_passes 未满足仍续写
        assert should_continue_round(wc=1200, word_budget=1500,
                                     continue_count=0, min_passes=2, max_rounds=3) is True

    def test_both_satisfied_stops(self):
        assert should_continue_round(wc=1400, word_budget=1500,
                                     continue_count=0, min_passes=0, max_rounds=3) is False

    def test_rounds_cap(self):
        assert should_continue_round(wc=1000, word_budget=1500,
                                     continue_count=3, min_passes=1, max_rounds=3) is False

    def test_zero_budget_falls_back(self):
        # budget=0 → 0*0.8=0，wc<0 恒假；由 min_passes 决定，保证不抛异常
        assert should_continue_round(wc=100, word_budget=0,
                                     continue_count=0, min_passes=0, max_rounds=3) is False


class TestContinueMaxTokens:
    def test_matches_legacy_formula(self):
        from app.services.content_utils import max_tokens_for_budget
        for budget, wc in ((1500, 800), (1500, 1400), (5000, 1500), (800, 100)):
            assert continue_max_tokens(word_budget=budget, wc=wc) == max_tokens_for_budget(
                budget, chars=max(1, int(budget * 1.1) - wc))

    def test_floor_guard(self):
        # 剩余字数很小 → 不足下限时回退 MAX_TOKENS_FLOOR（1536），不产生 0/负值
        assert continue_max_tokens(word_budget=800, wc=780) >= 1536

    def test_bad_inputs_fallback(self):
        assert continue_max_tokens(word_budget=None, wc=None) > 0
        assert continue_max_tokens(word_budget="abc", wc="xyz") > 0