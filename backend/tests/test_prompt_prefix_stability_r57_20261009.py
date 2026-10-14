"""R57（2026-10-09）· 标书智能体方法论六篇对齐 · 增强落地护栏

依据一次性读入的 8 份《标书智能体》设计文档，本轮落地两处**真实缺口**，
本文件把「已落地」这件事变成可执行判据，防止后续重构把它悄悄改回去。

┌── 落地项 ────────────────────────────────────────────────────────────┐
│ F1 正文提示词前缀稳定化（对应《（四）提示词顺序优化，输入成本直降10倍》）│
│    A 组 = 分层构造器本体的行为判据；B 组 = sse_handlers 接线存在的判据 │
│ F2 定点编辑接入统一 JSON 修复通道（对应《（五）弱模型稳定输出复杂json》 │
│    §四「失败后不是重新问，而是定向修复」）                             │
│    C 组 = consistency_edits 行为；D 组 = 统一通道本体                   │
└──────────────────────────────────────────────────────────────────────┘

⚠️ A/B 反向验证（本仓库例行的「护栏必须能红」纪律）：
   见 ``docs/标书智能体六篇方法论对齐_增强落地与验证_R57_20261009.md`` §5。
"""
from __future__ import annotations

import inspect
import json
import re

from pathlib import Path

import pytest
from app.config import settings
from app.routers import sse_handlers
from app.services import consistency_edits
from app.services.ai import json_response
from app.services.content_runtime import (
    build_chapter_extras_block,
    build_chapter_layered_messages,
    build_chapter_user_content,
    resolve_system_variables,
)

# ---------------------------------------------------------------------------
# 构造入参
# ---------------------------------------------------------------------------
SCHEME = {
    "name": "深基坑支护及降水工程专项施工方案",
    "type": "危大工程专项方案",
}
PROJECT_BRIEF = "本工程地下两层，基坑深度 9.6m，周边有既有住宅楼。"
CONTENT_SCOPE = "- 钻孔灌注桩支护\n- 三轴搅拌桩止水帷幕\n- 管井降水"
FACTS_TEXT = "【项目实施周期】\n- 基坑支护施工工期为 60 个自然日"
KNOWLEDGE_TEXT = "企业标准：灌注桩充盈系数控制在 1.05~1.20。"


def _leaf(no: str, title: str, desc: str = "") -> dict:
    return {"id": f"s-{no}", "title": title, "description": desc}


def _base_kwargs(leaf: dict, **over) -> dict:
    kw = dict(
        sys_prompt="你是专业施工方案编制专家。",
        scheme=SCHEME,
        project_brief=PROJECT_BRIEF,
        content_scope=CONTENT_SCOPE,
        parent_chain="1 工程概况 > 3 施工计划",
        parent_points=["- 施工计划：总体部署与资源配置"],
        sibling_lines="3.1 施工进度计划\n3.2 劳动力配置计划",
        section_number="3.3",
        leaf=leaf,
        word_budget=800,
        word_budget_hint="800字",
        prev_sibling_summary="",
        facts_text=FACTS_TEXT,
        eff_standard="precise",
        knowledge_text=KNOWLEDGE_TEXT,
    )
    kw.update(over)
    return kw


def _labels(text: str) -> list:
    """提取文本中出现的所有【标签】：（按出现顺序）。"""
    return re.findall(r"【([^】]{1,40})】(?:（[^）]*）)?(?:（[^）]*）)?：", text)


# ===========================================================================
# A 组 · 分层构造器本体（F1）
# ===========================================================================
class TestChapterLayeredMessages:
    """「大且稳定的上下文前置、本轮任务差异放最后」的可执行判据。"""

    def test_role_sequence_is_system_plus_three_users(self):
        messages, _ = build_chapter_layered_messages(**_base_kwargs(_leaf("1", "降水施工")))
        assert [m["role"] for m in messages] == ["system", "user", "user", "user"]

    def test_system_is_exactly_the_rendered_prompt(self):
        messages, _ = build_chapter_layered_messages(**_base_kwargs(_leaf("1", "降水施工")))
        assert messages[0]["content"] == "你是专业施工方案编制专家。"

    def test_stable_prefix_identical_across_sibling_chapters(self):
        """同父兄弟章节：除**最后一条** user 外，前面全部逐字节相同。

        这是 Prompt Cache 能命中的直接判据 —— 服务商看的是前缀，
        只要「每条消息 + 消息内每个字符」在逐章间不变，前缀即可复用。
        """
        msgs_a, _ = build_chapter_layered_messages(**_base_kwargs(_leaf("1", "降水施工")))
        msgs_b, _ = build_chapter_layered_messages(
            **_base_kwargs(_leaf("2", "土方开挖", "分层分段开挖")))
        assert len(msgs_a) == len(msgs_b) == 4
        assert msgs_a[:-1] == msgs_b[:-1], "兄弟章节的非任务段必须逐字节相同"
        assert msgs_a[-1] != msgs_b[-1]

    def test_stable_prefix_identical_across_different_parent_chapters(self):
        """跨父章节：system + 第一条 user（整案上下文）仍逐字节相同。

        真实写、伙食到父母章节不同仍是常态，此时至少要保证「最长的一段」
        （项目概述 / 施工内容 / 全局事实 / 知识库，通常是全文最重的部分）
        仍处于可复用前缀上。
        """
        kw = _base_kwargs(_leaf("1", "降水施工"))
        msgs_a, _ = build_chapter_layered_messages(**kw)
        kw.update(parent_chain="1 工程概况 > 5 安全保证措施",
                  parent_points=["- 安全保证措施：组织与技术措施"],
                  sibling_lines="5.1 组织保障\n5.2 技术措施")
        msgs_b, _ = build_chapter_layered_messages(**kw)
        assert msgs_a[0] == msgs_b[0], "system 必须整案稳定"
        assert msgs_a[1] == msgs_b[1], "整案稳定上下文段必须逐章稳定"

    def test_stable_blocks_come_before_task_differences(self):
        """《（四）》§五.2：最大、最稳定的上下文必须前置。

        旧实现把这些块排在【当前章节编号】【目标字数】之后 —— 本用例
        一旦把「全局事实 / 知识库」挪回任务段之后即定向失败。
        """
        _, user_content = build_chapter_layered_messages(
            **_base_kwargs(_leaf("1", "降水施工")))
        idx_facts = user_content.find("【全局事实变量")
        idx_knowledge = user_content.find("【项目知识库素材")
        idx_section = user_content.find("【当前章节编号】")
        assert idx_facts >= 0 and idx_knowledge >= 0 and idx_section >= 0
        assert idx_facts < idx_section, "全局事实必须排在「当前章节」之前"
        assert idx_knowledge < idx_section, "知识库素材必须排在「当前章节」之前"

    def test_information_is_not_lost_compared_with_flat_builder(self):
        """分层只改变组织方式，不得丢段落：与旧构造器**标签集合**一致。

        注意：比较的是集合而非列表 —— 顺序不同恰恰是本轮的收益来源
        （稳定块前置），差异只允许出现在顺序上，不允许出现在有无上。
        """
        leaf = _leaf("1", "降水施工", "管井施工与运行")
        kw = _base_kwargs(leaf)
        layered_kwargs = dict(kw)
        _, layered_text = build_chapter_layered_messages(**layered_kwargs)
        flat_text = build_chapter_user_content(
            **{k: v for k, v in kw.items() if k != "sys_prompt"})
        assert set(_labels(layered_text)) == set(_labels(flat_text))
        assert set(_labels(flat_text))
        for marker in (FACTS_TEXT, KNOWLEDGE_TEXT, CONTENT_SCOPE, PROJECT_BRIEF,
                       SCHEME["name"], "管井施工与运行", "800字"):
            assert marker in layered_text, marker

    def test_empty_optional_blocks_do_not_produce_empty_user_messages(self):
        """部分 provider 显式拒绝空 content → 空段必须跳过。"""
        kw = _base_kwargs(_leaf("1", "降水施工"))
        kw.update(content_scope="", facts_text="", knowledge_text="",
                  parent_points=[], sibling_lines="", prev_sibling_summary="")
        messages, _ = build_chapter_layered_messages(**kw)
        assert all(str(m["content"]).strip() for m in messages)
        # 每条消息都必须非空；段数可以少于 3，但不能有空串
        assert [m["role"] for m in messages][1:] == ["user"] * (len(messages) - 1)

    def test_chapter_extras_are_appended_to_last_user_message(self):
        """从 system 剥离出来的逐章变量，必须落在**任务差异**段里而不是丢失。"""
        extras = ("\n【本章编写定位】：\n本章目录编号：3.3\n"
                  "\n【本章编制依据】：\nGB 51004-2015\n")
        messages, user_content = build_chapter_layered_messages(
            **_base_kwargs(_leaf("1", "降水施工"), chapter_extras=extras))
        assert "本章目录编号：3.3" in messages[-1]["content"]
        assert "GB 51004-2015" in messages[-1]["content"]
        assert "本章目录编号：3.3" not in messages[1]["content"]
        assert "本章目录编号：3.3" in user_content

    def test_leaf_without_description_is_tolerated(self):
        leaf = {"id": "s-1", "title": "降水施工"}  # 无 description
        messages, _ = build_chapter_layered_messages(**_base_kwargs(leaf))
        assert messages[-1]["content"].count("【当前章节】：") == 1

    def test_user_content_is_the_join_of_all_user_segments(self):
        """续写轮复用 user_content，必须与 messages 同步（不可各探一路）。"""
        messages, user_content = build_chapter_layered_messages(
            **_base_kwargs(_leaf("1", "降水施工")))
        assert user_content == "".join(
            m["content"] for m in messages if m["role"] == "user")


# ===========================================================================
# A2 组 · system 变量决策（下沉后的纯函数，必须能被 A/B 变异抓到）
# ===========================================================================
class TestSystemVariablesDecision:
    """这段决策原本埋在 SSE 闭包里，无法运行时验证，故下沉为纯函数。"""

    def test_stable_mode_clears_every_per_chapter_variable(self):
        got = resolve_system_variables(
            stable=True, section_number="3.3",
            standards_text="GB 51004-2015", subheading_rule="- 二级小标题")
        assert set(got) == {"section_number", "standards_text", "subheading_rule"}
        assert all(v == "" for v in got.values()), got

    def test_stable_mode_result_is_invariant_to_chapter(self):
        """同一方案下 system 必须逐字节相同 —— 命中缓存的前提。"""
        a = resolve_system_variables(stable=True, section_number="3.3",
                                     standards_text="GB-A", subheading_rule="R1")
        b = resolve_system_variables(stable=True, section_number="5.1",
                                     standards_text="GB-B", subheading_rule="R2")
        assert a == b

    def test_legacy_mode_returns_values_verbatim(self):
        got = resolve_system_variables(
            stable=False, section_number="3.3",
            standards_text="GB 51004-2015", subheading_rule="- 二级小标题")
        assert got == {"section_number": "3.3",
                       "standards_text": "GB 51004-2015",
                       "subheading_rule": "- 二级小标题"}

    def test_extras_block_carries_the_stripped_values_verbatim(self):
        """system 里被清空的内容必须**一字不差**地回到 user，不得丢段。"""
        block = build_chapter_extras_block(
            section_number="3.3", standards_text="GB 51004-2015",
            subheading_rule="- 二级小标题：1.1 标题",
            chapter_checkpoint_block="\n【本章审核检查点要求】\n- 必须写明值班电话")
        assert "本章目录编号：3.3" in block
        assert "GB 51004-2015" in block
        assert "- 二级小标题：1.1 标题" in block
        assert "必须写明值班电话" in block
        assert block.count("【本章审核检查点要求】") == 1

    def test_extras_block_skips_empty_sections(self):
        block = build_chapter_extras_block(
            section_number="3.3", standards_text="", subheading_rule="",
            chapter_checkpoint_block="")
        assert "【本章编制依据】" not in block
        assert "【本章子标题编号规范】" not in block
        assert block.endswith("\n")


# ===========================================================================
# B 组 · sse_handlers 接线（F1）
# ===========================================================================
class TestSseHandlersWiring:
    """``_build_generation_context`` 是 generate_content 的内部闭包，无法直接实例化，
    故本组用**源码级判据**钉住接线关系（与本仓 ``test_json_repair_key_routing``
    同型护栏）。
    """

    @staticmethod
    def _src() -> str:
        import io
        return io.open(sse_handlers.__file__, encoding="utf-8").read()

    def test_switch_default_is_on(self):
        assert bool(getattr(settings, "content_stable_prefix_enabled", False)) is True

    def test_switch_is_read_from_settings(self):
        src = re.sub(r"\s+", " ", self._src())
        assert 'getattr( settings, "content_stable_prefix_enabled", True)' in src

    def test_render_no_longer_inlines_per_chapter_variables(self):
        """逐章变量不得再直接注入 system —— 否则 system 自首字符起就逐章变化。"""
        src = self._src()
        assert "_sys_vars" in src
        assert "_ck_sys_kwargs" in src
        # 旧形态：render(... section_number=_section_outline_number(leaf) ...)
        assert "section_number=_section_outline_number(leaf)" not in src

    def test_both_branches_remain_available(self):
        """开关关闭时必须能瞬时回到旧路径（不得删代码实现单向升级）。"""
        src = self._src()
        assert "build_chapter_layered_messages(" in src
        assert "build_chapter_user_content(" in src

    def test_stripped_variables_are_handed_back_to_the_model(self):
        """system 清空的变量必须经 build_chapter_extras_block 回到最后一条 user。"""
        src = self._src()
        assert "build_chapter_extras_block(" in src
        assert "chapter_extras=_chapter_extras" in src

    def test_governance_applies_to_every_user_message(self):
        """分层后治理必须覆盖**每条** user；只治理末条会让稳定段绕过预算/注入防护。"""
        src = self._src()
        assert 'if isinstance(m, dict) and m.get("role") == "user"' in src
        assert "_guard_external_material" in src and "_apply_prompt_context_budget" in src

    def test_user_content_is_resynced_after_governance(self):
        """治理改写 user 后 user_content 必须同步，否则续写轮拿到未被治理的原文。"""
        src = self._src()
        assert 'user_content = "".join(' in src


# ===========================================================================
# C 组 · 一致性定点编辑接入统一修复通道（F2）
# ===========================================================================
_CONTENT = "本工程总工期为 120 日历天，混凝土强度等级为 C30。"
_FIXED_TEXT = "本工程总工期为 90 日历天，混凝土强度等级为 C30。"
_OLD = "本工程总工期为 120 日历天"
_NEW = "本工程总工期为 90 日历天"

_CALLS = "calls"


def _fake_chat_factory(responses: list, calls: list):
    async def _fake(messages, **kwargs):
        calls.append(list(messages))
        if len(calls) > len(responses):
            raise AssertionError("调用次数超出预期")
        return responses[len(calls) - 1]
    return _fake


def _conflicts():
    return [{"id": "C1", "conflict_type": "numeric", "current": _OLD,
             "authoritative": _NEW}]


class TestConsistencyEditsRepairChannel:
    """《（五）》§四：JSON 坏一次不要推倒重来 —— 定向修复后照样能用。"""

    def test_invalid_json_then_repaired_json_is_applied(self):
        """旧实现：模型多说一句话 → 整节放弃 → 回落整章重写（改掉本没问题的段落）。"""
        calls: list = []
        responses = [
            "好的，以下是修改：\n" + json.dumps({"edits": [{"old_text": _OLD,
                                                            "new_text": _NEW}]},
                                               ensure_ascii=False) + "\n希望有帮助。",
        ]
        fake = _fake_chat_factory(responses, calls)

        async def _run():
            return await consistency_edits.collect_repair_edits(
                section_id="sec-1", section_title="工程概况",
                section_content=_CONTENT, conflicts_in_section=_conflicts(),
                facts="工期=90天", sources="招标文件", chat_fn=fake)

        # 解释文字包裹的 JSON 必须被 extract_json 救回（成功路径零额外调用）
        res = _await(_run())
        assert res.applied == 1, res.to_dict()
        assert res.content == _FIXED_TEXT

    def test_repair_round_is_used_when_first_output_is_unparsable(self):
        calls: list = []
        responses = [
            "抱歉，我无法输出 JSON。",  # 完全无 JSON 结构
            json.dumps({"edits": [{"old_text": _OLD, "new_text": _NEW}]},
                       ensure_ascii=False),
        ]
        fake = _fake_chat_factory(responses, calls)

        res = _await(consistency_edits.collect_repair_edits(
            section_id="sec-1", section_title="工程概况",
            section_content=_CONTENT, conflicts_in_section=_conflicts(),
            facts="工期=90天", sources="招标文件", chat_fn=fake))

        assert len(calls) == 2, "必须真正进入定向修复轮（旧实现只调 1 次即放弃）"
        assert res.applied == 1
        assert res.content == _FIXED_TEXT
        # 修复轮必须把「模型最初的输出」连同具体问题一起回喂
        repair_user = calls[1][-1]["content"]
        assert "抱歉，我无法输出 JSON。" in repair_user

    def test_success_path_costs_exactly_one_call(self):
        """成功时不得因为接入通道而多花一次 AI 调用（经济性护栏）。"""
        calls: list = []
        responses = [json.dumps({"edits": [{"old_text": _OLD, "new_text": _NEW}]},
                                ensure_ascii=False)]
        fake = _fake_chat_factory(responses, calls)
        res = _await(consistency_edits.collect_repair_edits(
            section_id="sec-1", section_title="工程概况",
            section_content=_CONTENT, conflicts_in_section=_conflicts(),
            facts="工期=90天", sources="招标文件", chat_fn=fake))
        assert len(calls) == 1
        assert res.applied == 1

    def test_all_rounds_fail_falls_back_softly(self):
        """修复轮也失败 → applied=0 + 原文不变，由 repair_agent 回落整章重写。"""
        calls: list = []
        responses = ["还是写不出来。", "真的写不出来。"]
        fake = _fake_chat_factory(responses, calls)
        res = _await(consistency_edits.collect_repair_edits(
            section_id="sec-1", section_title="工程概况",
            section_content=_CONTENT, conflicts_in_section=_conflicts(),
            facts="工期=90天", sources="招标文件", chat_fn=fake))
        assert res.applied == 0
        assert res.content == _CONTENT
        assert len(calls) == 1 + consistency_edits.CONSISTENCY_EDITS_REPAIR_RETRIES

    def test_schema_violation_triggers_repair(self):
        """合法 JSON 但字段名错（engine → edits），走修复轮而不是直接放弃。"""
        calls: list = []
        responses = [
            json.dumps({"engine": [{"old_text": _OLD, "new_text": _NEW}]}),
            json.dumps({"edits": [{"old_text": _OLD, "new_text": _NEW}]},
                       ensure_ascii=False),
        ]
        fake = _fake_chat_factory(responses, calls)
        res = _await(consistency_edits.collect_repair_edits(
            section_id="sec-1", section_title="工程概况",
            section_content=_CONTENT, conflicts_in_section=_conflicts(),
            facts="工期=90天", sources="招标文件", chat_fn=fake))
        assert len(calls) == 2
        assert res.applied == 1

    def test_minimal_signature_chat_fn_is_supported(self):
        """只认 messages 的 chat 实现（既有测试替身常见形态）不得炸。"""
        calls: list = []

        async def _minimal(messages):  # 无 **kwargs
            calls.append(list(messages))
            return json.dumps({"edits": [{"old_text": _OLD, "new_text": _NEW}]},
                              ensure_ascii=False)

        res = _await(consistency_edits.collect_repair_edits(
            section_id="sec-1", section_title="工程概况",
            section_content=_CONTENT, conflicts_in_section=_conflicts(),
            facts="工期=90天", sources="招标文件", chat_fn=_minimal))
        assert res.applied == 1, "必须以「逐层降级」方式兼容最小签名"

    def test_unique_hit_semantics_unchanged(self):
        """接入通道不得放宽「唯一命中」纪律 —— 多处命中仍必须拒绝。"""
        ambiguous = "工期 120 天。" + _CONTENT + " 工期 120 天。"
        calls: list = []
        responses = [json.dumps(
            {"edits": [{"old_text": "工期 120 天", "new_text": "工期 90 天"}]},
            ensure_ascii=False)]
        fake = _fake_chat_factory(responses, calls)
        res = _await(consistency_edits.collect_repair_edits(
            section_id="sec-1", section_title="工程概况",
            section_content=ambiguous, conflicts_in_section=_conflicts(),
            facts="工期=90天", sources="招标文件", chat_fn=fake))
        assert res.applied == 0
        assert res.rejected and res.rejected[0][1] == "ambiguous"
        assert res.content == ambiguous


# ===========================================================================
# D 组 · 统一 JSON 通道本体（单一出口，消除多副本）
# ===========================================================================
class TestUnifiedJsonChannel:
    def test_collect_json_response_delegates_to_shared_implementation(self):
        """两处实现任何一处漏改都会分叉 → 用源码锁死「只有一份编排」。"""
        src = inspect.getsource(json_response.collect_json_response)
        assert "return await collect_json_response_with_chat(" in src
        assert "record_ai_failure" not in src, "编排逻辑必须下沉，不得留副本"

    def test_max_retries_zero_skips_repair(self):
        calls: list = []

        async def _fake(messages, **kwargs):
            calls.append(list(messages))
            return "not json at all"

        with pytest.raises(ValueError):
            _await(json_response.collect_json_response_with_chat(
                _fake, [{"role": "user", "content": "给我 JSON"}],
                max_retries=0))
        assert len(calls) == 1

    def test_repair_target_is_always_the_first_output(self):
        """第 2 轮必须带着**最初**的输出去修（R38 修复点 1，下沉后不得丢）。"""
        calls: list = []
        responses = ["garbage-1", "garbage-2", '{"ok": 1}']
        state = {"i": 0}

        async def _fake(messages, **kwargs):
            calls.append(list(messages))
            out = responses[state["i"]]
            state["i"] += 1
            return out

        obj, raw = _await(json_response.collect_json_response_with_chat(
            _fake, [{"role": "user", "content": "给我 JSON"}], max_retries=2))
        assert obj == {"ok": 1} and raw == '{"ok": 1}'
        # 第 2 轮修复的 user 内容里必须是 garbage-1（首轮产出），不是 garbage-2
        assert "garbage-1" in calls[2][-1]["content"]

    def test_validator_exception_enters_repair_instead_of_crashing(self):
        def _boom(obj):  # 校验器假定 dict，模型却返回数组
            return obj["outline"]

        calls: list = []
        responses = ["[1,2,3]", '{"outline": []}']

        async def _fake(messages, **kwargs):
            calls.append(list(messages))
            return responses[len(calls) - 1]

        obj, _ = _await(json_response.collect_json_response_with_chat(
            _fake, [{"role": "user", "content": "给我 JSON"}],
            validate_fn=_boom, max_retries=1))
        assert obj == {"outline": []}


def _await(coro):
    """同步执行协程（本文件不依赖 pytest-asyncio mode）。"""
    import asyncio
    return asyncio.run(coro)

# ===========================================================================
# E 组 · 源文件字节体检（把 R56/R57 两次踩到的坑变成判据）
# ===========================================================================
_TOUCHED = [
    "app/config.py",
    "app/routers/sse_handlers.py",
    "app/services/content_runtime.py",
    "app/services/ai/json_response.py",
    "app/services/consistency_edits.py",
    "tests/test_prompt_prefix_stability_r57_20261009.py",
]


def _repo_root() -> Path:
    """tests/ 的上一级即 backend/ 根目录。"""
    return Path(__file__).resolve().parent.parent


class TestSourceByteIntegrity:
    """⚠️ 本轮（R57）真实踩坑：用文本工具编辑 CRLF 仓库文件后，被改文件的
    **每一行**都被写成 ``\r\r\n`` —— 工作树 loneCR 由 0 变成「行数」。

    危害：
      1. Python universal newlines 把 ``\r\r\n`` 拆成两个换行；
      2. ``git diff`` 把整份文件判为全文重写，真实改动无法 review；
      3. R56 曾因同形态把前端 JS 正则字面量写坏（模块整体 SyntaxError）。

    本组锁死「本轮触碰的源文件必须有干净字节」，让同类损伤立刻变红。
    """

    @staticmethod
    def _read(rel: str) -> bytes:
        for base in (_repo_root(), Path.cwd()):
            p = base / rel
            if p.exists():
                return p.read_bytes()
        raise AssertionError(f"找不到待测源文件：{rel}")

    def test_no_lone_cr_or_control_bytes(self):
        for rel in _TOUCHED:
            data = self._read(rel)
            crlf = data.count(b"\r\n")
            lone_cr = data.count(b"\r") - crlf
            assert lone_cr == 0, f"{rel} 存在 {lone_cr} 个孤立 CR（如 \\r\\r\\n 污染）"
            for name, b in (("NUL", b"\x00"), ("BEL", b"\x07"), ("FF", b"\x0c")):
                assert data.count(b) == 0, f"{rel} 含 {name}"
            assert data.decode("utf-8", errors="replace").count("\ufffd") == 0, rel

    def test_line_endings_are_not_mixed(self):
        """同一文件内不得 LF 与 CRLF 混用（混用是'被工具改了一半'的典型特征）。"""
        for rel in _TOUCHED:
            data = self._read(rel)
            crlf = data.count(b"\r\n")
            bare_lf = data.count(b"\n") - crlf
            assert not (crlf and bare_lf), f"{rel} 混用行尾：CRLF={crlf} LF={bare_lf}"

    def test_every_touched_file_compiles(self):
        """语法正确性最终判据：行尾被玩坏的文件往往连 py_compile 都过不去。"""
        import py_compile
        for rel in _TOUCHED:
            p = None
            for base in (_repo_root(), Path.cwd()):
                cand = base / rel
                if cand.exists():
                    p = cand
                    break
            assert p is not None
            py_compile.compile(str(p), doraise=True)
