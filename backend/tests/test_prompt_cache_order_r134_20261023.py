# -*- coding: utf-8 -*-
"""R134 · 《标书智能体（4）——提示词顺序优化与缓存命中》四原则护栏（2026-10-23）

外部引用来源：《标书智能体（4）——提示词顺序优化，让缓存命中，输入成本直降
10倍》（GitHub: FB208/yibiao-simple）及其《文档梳理与引入方案》。
冲突裁决规则：与本仓旧习惯不一致时，以外部引用软件的工作机制/标准为准。

四原则（本文件逐条锁住）：
  ① system 只放稳定规则
  ② 最大最稳定的上下文尽量前置
  ③ 任务差异尽量放最后
  ④ 同一份数据组织格式保持稳定（渲染确定性，不掺时间戳 / request_id / 随机数）

分组：
  A. 解析提取（18 项链路）：资料全文一条 user 前置 + 任务说明一条 user 收尾，
     同一资料跨提取项共享逐字节相同的首条 user（前缀缓存命中结构）
  B. content_plan_user（离散正文编排）：整案稳定块在章节差异块之前
  C. review_autofix_user（审核定向修复）：全局稳定块在章节字段之前
  D. 目录 sublevel / sublevel_batch 模板：稳定头 → 差异尾，
     两章（两批）渲染的共同前缀必须覆盖到差异尾起点
  E. 原则④：同参两次渲染逐字节一致，模板不掺时间戳 / uuid / 随机数
  F. 可观测性：/ai/stats by_scene 纯加法补齐 cached_tokens / cache_hit_rate

运行：pytest tests/test_prompt_cache_order_r134_20261023.py -q
"""
from __future__ import annotations

import re

from app.services.ai.prompts._registry import get_default_prompt, render_prompt


def _common_prefix_len(a: str, b: str) -> int:
    """两个字符串的共同前缀长度（服务商缓存按 token 前缀判命中，字节前缀
    一致是必要条件）。"""
    i = 0
    n = min(len(a), len(b))
    while i < n and a[i] == b[i]:
        i += 1
    return i


# ===========================================================================
# A. 解析提取：全文前置 + 任务收尾（案例一结构）
# ===========================================================================
class TestExtractTwoUserSplit:
    # 用真实注册项（走 get_item_prompt → 出厂任务模板 + __CONTEXT__ 分支）
    _ITEM_A = {"item_id": "projectBasicInfo", "label": "项目基本信息"}
    _ITEM_B = {"item_id": "schemeBasicInfo", "label": "方案基本信息"}

    def test_two_user_messages_text_first_task_last(self):
        from app.services.bid_analysis_service import build_item_user_messages
        msgs = build_item_user_messages("资料全文XYZ", dict(self._ITEM_A))
        assert [m["role"] for m in msgs] == ["user", "user"]
        # ② 全文（最大最稳定上下文）在前
        assert "资料全文XYZ" in msgs[0]["content"]
        # ③ 任务在最后，且不再重复携带全文（占位指针文案固定）
        assert "资料全文XYZ" not in msgs[1]["content"]
        assert "（项目资料文本见上一条消息）" in msgs[1]["content"]

    def test_same_text_shares_identical_first_user_across_items(self):
        """同一份资料、不同提取项：首条 user 必须逐字节相同（= 共同前缀
        覆盖 system + 全部资料文本，正是文档实测「输入成本降至约 1/9」的结构）。"""
        from app.services.bid_analysis_service import build_item_user_messages
        a = build_item_user_messages("同一份资料全文", dict(self._ITEM_A))
        b = build_item_user_messages("同一份资料全文", dict(self._ITEM_B))
        assert a[0] == b[0], "资料全文消息跨提取项必须逐字节相同"
        assert a[1]["content"] != b[1]["content"], "任务差异必须只出现在最后一条"

    def test_text_message_has_no_dynamic_suffix(self):
        """原则④：全文消息不含时间戳 / request_id / 随机数（否则前缀必分叉）。"""
        from app.services.bid_analysis_service import build_item_user_messages
        c = build_item_user_messages("资料", dict(self._ITEM_A))[0]["content"]
        assert not re.search(
            r"\d{4}-\d{2}-\d{2}|request_id|uuid|time\.time|datetime\.now", c)


# ===========================================================================
# B / C. 编排与审核修复模板：稳定块前置、差异块后置
# ===========================================================================
class TestStableBeforeDiffTemplates:
    def test_content_plan_user_stable_blocks_before_section_diff(self):
        tpl = get_default_prompt("content_plan_user")
        stable = [tpl.index("【方案名称】"), tpl.index("【项目概述】"),
                  tpl.index("【知识库素材候选"), tpl.index("【全局事实候选标题")]
        diff = [tpl.index("【同级章节"), tpl.index("【章节编号】"),
                tpl.index("【当前章节】"), tpl.index("【目标字数】")]
        assert max(stable) < min(diff), (
            "原则②被破坏：整案稳定块（方案信息/知识/事实候选）必须整体位于"
            "章节差异块（同级章节/编号/当前章节/字数）之前")

    def test_review_autofix_user_global_stable_before_section_fields(self):
        tpl = get_default_prompt("review_autofix_user")
        stable = [tpl.index("方案名称："), tpl.index("全局事实变量"),
                  tpl.index("现行有效标准参考清单")]
        diff = [tpl.index("章节ID："), tpl.index("章节标题："),
                tpl.index("检查结论"), tpl.index("当前章节原文")]
        assert max(stable) < min(diff), (
            "原则②被破坏：方案级稳定块（方案信息/全局事实/标准清单）必须整体"
            "位于章节字段（章节ID/标题/检查结论/原文）之前")


# ===========================================================================
# D. 目录 sublevel / batch：稳定头 → 差异尾（跨章/跨批前缀共享）
# ===========================================================================
class TestOutlineSublevelPrefixSharing:
    _BASE = dict(
        scheme_name="X工程深基坑支护", scheme_type="基坑支护",
        construction_scope="围护桩+内支撑", standards_text="GB50202-2018",
        project_brief="基坑深12米", requirements="无")

    def _render_single(self, cid, title, desc, facts, other):
        return render_prompt(
            get_default_prompt("outline_sublevel_system"), **self._BASE,
            chapter_id=cid, chapter_title=title, chapter_desc=desc,
            project_facts=facts, other_outline=other, prior_chapters="无")

    def _render_batch(self, count, chapters_text, facts, other):
        return render_prompt(
            get_default_prompt("outline_sublevel_batch_system"), **self._BASE,
            chapter_count=count, chapters_text=chapters_text,
            project_facts=facts, other_outline=other, prior_chapters="无")

    def test_single_template_diff_tail_is_last(self):
        """模板序：稳定头（方案信息 + 要求 + SHARED）→ 差异尾（任务行 + 逐章块 + 框架块）。"""
        tpl = get_default_prompt("outline_sublevel_system")
        head = [tpl.index("【方案名称】"), tpl.index("【项目资料摘要】"),
                tpl.index("【编制要求"), tpl.index("【编制依据规范"),
                tpl.index("{SHARED_OUTPUT_SPEC}")]
        tail = [tpl.index("【本章任务】"), tpl.index("【项目关键事实】"),
                tpl.index("【其他章节标题"), tpl.index("【已生成的前序章节小节"),
                tpl.index("{outline_framework_block}")]
        assert max(head) < min(tail), "sublevel 模板差异尾必须整体位于稳定头之后"
        # ①：专家身份行不再携带章节差异（改造前首字符即「第【{chapter_id}」）
        assert tpl.startswith("你是资深的工程项目专项方案编制专家。\n")

    def test_two_chapters_share_prefix_through_stable_head(self):
        a = self._render_single("1", "第一章", "支护桩施工", "开挖深度12m", "第二章")
        b = self._render_single("2", "第二章", "土方开挖", "开挖深度12m", "第一章")
        k = _common_prefix_len(a, b)
        assert k >= a.index("【本章任务】"), (
            f"两章渲染在稳定头结束前就分叉（分叉点 {k} < 任务行 "
            f"{a.index('【本章任务】')}），缓存前缀共享被破坏")

    def test_batch_template_diff_tail_is_last(self):
        tpl = get_default_prompt("outline_sublevel_batch_system")
        head = [tpl.index("【方案名称】"), tpl.index("【项目资料摘要】"),
                tpl.index("【编制要求"), tpl.index("{SHARED_FORBIDDEN_WORDS}")]
        tail = [tpl.index("【本批任务】"), tpl.index("{chapter_count}"),
                tpl.index("【项目关键事实】"), tpl.rindex("【待生成章节列表】"),
                tpl.index("{chapters_text}"), tpl.index("{outline_framework_block}")]
        assert max(head) < min(tail), "batch 模板差异尾必须整体位于稳定头之后"
        assert tpl.startswith("你是资深的工程项目专项方案编制专家。\n")

    def test_two_batches_share_prefix_through_stable_head(self):
        a = self._render_batch(2, "- chapter_id=1｜标题：第一章\n- chapter_id=2｜标题：第二章",
                               "事实A", "第三章")
        b = self._render_batch(3, "- chapter_id=1｜标题：第一章\n- chapter_id=2｜标题：第二章\n- chapter_id=3｜标题：第三章",
                               "事实B", "第四章")
        k = _common_prefix_len(a, b)
        assert k >= a.index("【本批任务】"), (
            f"两批渲染在稳定头结束前就分叉（分叉点 {k} < 任务行 "
            f"{a.index('【本批任务】')}）")


# ===========================================================================
# E. 原则④：渲染确定性 + 无动态后缀
# ===========================================================================
class TestRenderDeterminism:
    _KW = {
        "outline_sublevel_system": dict(
            chapter_id="1", chapter_title="第一章", chapter_desc="说明",
            scheme_name="X工程", scheme_type="基坑支护", construction_scope="scope",
            standards_text="GB50000", project_brief="brief", other_outline="其他章",
            prior_chapters="无", requirements="无", project_facts="facts"),
        "outline_sublevel_batch_system": dict(
            chapter_count=2, chapters_text="- chapter_id=1",
            scheme_name="X工程", scheme_type="基坑支护", construction_scope="scope",
            standards_text="GB50000", project_brief="brief", other_outline="其他章",
            prior_chapters="无", requirements="无", project_facts="facts"),
        "content_plan_user": dict(
            scheme_name="X工程", scheme_type="基坑支护", project_brief="brief",
            knowledge_names_text="素材A", fact_titles_text="事实A",
            parent_chain="第一章", sibling_lines="1.1 节", section_number="1.1",
            section_title="施工准备", section_description="人机料准备",
            word_budget=800),
    }

    def test_same_inputs_render_byte_identical(self):
        for key, kw in self._KW.items():
            tpl = get_default_prompt(key)
            assert render_prompt(tpl, **kw) == render_prompt(tpl, **kw), (
                f"{key} 同参两次渲染必须逐字节一致（原则④）")

    def test_no_dynamic_suffix_in_templates(self):
        """模板不得掺时间戳 / uuid / request_id / 随机数（掺了前缀必分叉）。"""
        rx = re.compile(r"time\.time|datetime\.now|uuid|request_id|random\.|os\.urandom")
        for key in self._KW:
            assert not rx.search(get_default_prompt(key)), (
                f"{key} 含动态内容，破坏缓存前缀稳定性")


# ===========================================================================
# F. 可观测性：by_scene 纯加法补齐 cached_tokens / cache_hit_rate
# ===========================================================================
class TestBySceneCacheObservability:
    async def test_by_scene_reports_cached_tokens_and_hit_rate(self, db_conn):
        from app.routers.ai_config import usage as usage_mod
        await db_conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
            " prompt_tokens, completion_tokens, cached_tokens, duration, success, scene)"
            " VALUES ('r134a','deepseek','m','chat',100,50,40,1.0,1,'outline_draft')")
        await db_conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
            " prompt_tokens, completion_tokens, cached_tokens, duration, success, scene)"
            " VALUES ('r134b','deepseek','m','chat',100,50,0,1.0,1,'outline_draft')")
        await db_conn.commit()
        res = await usage_mod.ai_stats(days=30, db=db_conn)
        row = next(r for r in res["by_scene"] if r["scene"] == "outline_draft")
        # 纯加法：既有列不动（calls/tokens/success_count 照旧），新增两列
        assert row["calls"] == 2 and row["tokens"] == 300
        assert row["prompt_tokens"] == 200 and row["completion_tokens"] == 100
        assert row["cached_tokens"] == 40
        assert row["cache_hit_rate"] == 20.0, "命中率口径 = cached/prompt × 100（与 by_provider 一致）"

    async def test_by_scene_hit_rate_none_when_no_prompt_tokens(self, db_conn):
        from app.routers.ai_config import usage as usage_mod
        res = await usage_mod.ai_stats(days=30, db=db_conn)
        for row in res["by_scene"]:
            if not row.get("prompt_tokens"):
                assert row["cache_hit_rate"] is None
