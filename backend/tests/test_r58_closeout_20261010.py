"""R58（2026-10-10）· 标书智能体方法论四项未落地项收口护栏

承接 R57 §6 的 4 条未落地项，本轮全部落地并锁死：

┌── 项 ─────────────────────────────────────────────────────────────┐
│ F3  提纲「一次性失败 → 切换分步生成」兜底边                        │
│ F4  正文图表**配额感知**（程序实时核算剩余配额 → 告知 AI）          │
│ F5  全局事实**分段注入**（整案核心进稳定前缀 / 本章专属进任务段）    │
│ F6  bid_analysis._repair_json 自建劣化版 → 统一 JSON 通道           │
└───────────────────────────────────────────────────────────────────┘

⚠️ A/B 反向验证见 ``docs/标书智能体方法论四项未落地项收口_R58_20261010.md`` §5。
   每条护栏都必须能红；本文件同时承担「防改回去」与「防恒绿」两个职责。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from app.config import settings
from app.routers import bid_analysis as ba
from app.routers._chart_pipeline import build_chart_quota_hint
from app.routers import sse_handlers as sh
from app.services.content_runtime import build_chapter_layered_messages
from app.services.facts_builder import core_facts_rows, exclude_rows

# ===========================================================================
# F3 · 提纲一次性失败 → 分步兜底
# ===========================================================================
class TestOutlineOneshotFallback:
    """``_build_generation_context`` 式闭包无法实例化，故用源码级判据钉住接线。"""

    @staticmethod
    def _src() -> str:
        import io
        return io.open(sh.__file__, encoding="utf-8").read()

    def test_switch_default_is_on(self):
        assert bool(getattr(settings, "outline_oneshot_fallback_stepwise", False)) is True
        assert bool(getattr(sh, "OUTLINE_ONESHOT_FALLBACK", False)) is True

    def test_stepwise_block_is_entered_on_oneshot_failure(self):
        """核心判据：分步块不再由 else 独占，一次性失败也要能进来。"""
        src = self._src()
        assert "if use_stepwise or not _one_shot_ok:" in src
        assert "while _one_shot_ok:" in src

    def test_hard_failures_no_longer_return(self):
        """两处硬失败必须改为「置标志 + break」，不得再 finish_task(failed)+return。"""
        src = self._src()
        assert 'finish_task(task_id, "failed", "AI 返回空目录")' not in src
        assert ('finish_task(task_id, "failed", '
                '"AI 返回目录节点格式非法（裁剪后为空）")') not in src
        assert "_one_shot_ok = False" in src

    def test_quota_errors_do_not_switch(self):
        """配额/限流/认证类错误不切分步 —— 否则十几章全白烧。"""
        src = self._src()
        assert "if OUTLINE_ONESHOT_FALLBACK and not _is_quota_error(e):" in src

    def test_cancelled_error_is_not_swallowed(self):
        """用户主动取消必须照旧冒泡，不能被兜底逻辑吃掉。"""
        src = self._src()
        assert "except asyncio.CancelledError:\n                    raise" in src

    def test_progress_and_partial_are_reset_before_switching(self):
        """切档必须重置进度（否则进度条卡在 55%）并清空脏成果。"""
        src = self._src()
        for frag in ('_prog["stepwise"] = True', '_prog["last_p"] = 0.05',
                     '_prog["sub_total"] = 0', '_partial_holder["outline"] = []'):
            assert frag in src, frag

    def test_success_path_still_returns(self):
        """成功路径末尾必须 return，保证与原来 if/else 互斥语义一致。"""
        src = self._src()
        assert "                    return\n            if use_stepwise or not _one_shot_ok:" in src


# ===========================================================================
# F4 · 图表配额感知
# ===========================================================================
class TestChartQuotaHint:
    def test_empty_counts_reports_full_quota(self):
        hint = build_chart_quota_hint({})
        assert "【本章图表配额" in hint
        assert "剩余 3 个" in hint          # 默认上限 3
        assert "已达全方案上限" not in hint
        assert hint.endswith("\n")

    def test_exhausted_type_is_flagged_as_will_be_deleted(self):
        hint = build_chart_quota_hint({"gantt": 3})
        assert "gantt：已达全方案上限（3/3）" in hint
        assert "必被删除" in hint

    def test_ai_image_has_its_own_limit(self):
        assert "剩余 6 个" in build_chart_quota_hint({})   # ai_image 上限 6
        assert "ai_image：已达全方案上限（6/6）" in build_chart_quota_hint(
            {"ai_image": 6})

    def test_short_chapter_suggests_no_chart(self):
        assert "建议不配图" in build_chart_quota_hint({}, word_budget=150)
        assert "建议不配图" not in build_chart_quota_hint({}, word_budget=1200)

    def test_hint_is_constraint_not_orchestration(self):
        """必须是「配额约束」而非「预编排指令」—— 不指定本章该画什么。"""
        hint = build_chart_quota_hint({"gantt": 1})
        for banned in ("本章应画", "必须画", "请画一张", "推荐画"):
            assert banned not in hint, banned
        assert "是否配图" in hint

    def test_hint_lands_in_task_segment_only(self):
        """逐章变化量只能进任务差异段，否则会击穿 R57 的前缀稳定。"""
        kw = dict(
            sys_prompt="S", scheme={"name": "n", "type": "t"},
            project_brief="p", content_scope="c", parent_chain="pc",
            parent_points=[], sibling_lines="", section_number="3.3",
            leaf={"id": "s1", "title": "降水施工", "description": "d"},
            word_budget=800, word_budget_hint="800字", prev_sibling_summary="",
            facts_text="F", eff_standard="precise", knowledge_text="K",
            chapter_extras="",
        )
        hint = build_chart_quota_hint({"gantt": 2})
        messages, joined = build_chapter_layered_messages(
            **kw, chart_quota_hint=hint)
        assert "本章图表配额" in messages[-1]["content"], "必须落在任务差异段"
        assert "本章图表配额" not in messages[1]["content"], "不得污染稳定段"
        assert "本章图表配额" in joined

    def test_switch_default_is_on(self):
        assert bool(getattr(settings, "content_chart_quota_hint_enabled", False)) is True


# ===========================================================================
# F5 · 事实分段注入
# ===========================================================================
#: (group_title, title, content, confidence, chapter)
_ROWS = [
    ("工程概况", "项目名称", "某某基坑工程", 0.9, "overview"),
    ("工程概况", "工期", "60 天", 0.9, "plan"),
    ("降水设计", "管井深度", "12m", 0.8, "technique"),
    ("安全管理", "值班电话", "1380000", 0.7, "safety"),
]


class TestFactsSplitInject:
    def test_core_rows_are_generic_group_only(self):
        core = core_facts_rows(_ROWS)
        assert len(core) == 2
        assert all("概况" in r[0] for r in core)

    def test_exclude_rows_removes_core_and_keeps_order(self):
        core = core_facts_rows(_ROWS)
        rest = exclude_rows(_ROWS, core)
        assert [r[1] for r in rest] == ["管井深度", "值班电话"]

    def test_exclude_rows_handles_unhashable_rows(self):
        rows = [["组", "标题", "内容"], ["组2", "标题2", "内容2"]]  # list 行
        assert len(exclude_rows(rows, [rows[0]])) == 1

    def _build(self, leaf, **over):
        kw = dict(
            sys_prompt="S", scheme={"name": "n", "type": "t"},
            project_brief="p", content_scope="c", parent_chain="pc",
            parent_points=[], sibling_lines="", section_number="3.3",
            leaf=leaf, word_budget=800, word_budget_hint="800字",
            prev_sibling_summary="", facts_text="【整案核心事实】",
            eff_standard="precise", knowledge_text="K", chapter_extras="",
        )
        kw.update(over)
        return build_chapter_layered_messages(**kw)

    def test_chapter_facts_go_to_task_segment(self):
        """本章专属事实是逐章变化量 → 只能进最后一条 user。"""
        leaf = {"id": "s1", "title": "降水施工", "description": "管井"}
        messages, _ = self._build(leaf, chapter_facts_text="管井深度：12m\n")
        assert "本章专属事实" in messages[-1]["content"]
        assert "本章专属事实" not in messages[1]["content"]

    def test_switch_default_is_on(self):
        assert bool(getattr(settings, "content_facts_split_inject", False)) is True

    def test_wiring_uses_single_exit(self):
        """sse_handlers 必须走 core_facts_rows 单一出口（不得各写一份分堆判据）。"""
        import io
        src = io.open(sh.__file__, encoding="utf-8").read()
        assert "core_facts_rows(facts_rows)" in src
        assert "chapter_facts_text=_chapter_facts_text" in src


# ===========================================================================
# F6 · bid_analysis 统一 JSON 通道
# ===========================================================================
def _await(coro):
    return asyncio.run(coro)


class TestBidJsonUnifiedChannel:
    def test_self_built_repair_prompt_is_gone(self):
        """旧实现把修复提示词硬编码在函数里（DB 里改不了）—— 必须已移除。"""
        src = _src_of(ba._repair_json)
        assert "repair_prompt = (" not in src
        assert "collect_json_response_with_chat" in _src_of(ba._collect_json_str)

    def test_repair_retry_budget_matches_legacy_cost(self):
        """修复轮 = 1 → 与旧「1 次生成 + 1 次修复」调用次数完全相等。"""
        assert ba.BID_JSON_REPAIR_RETRIES == 1

    def test_validator_rejects_non_object(self):
        assert ba._validate_json_object({"a": 1}) == []
        assert ba._validate_json_object([]) == ["顶层必须是 JSON 对象"]
        assert ba._validate_json_object("x") == ["顶层必须是 JSON 对象"]

    def test_collect_returns_model_raw_text(self):
        """成功路径必须返回模型**原文**（下游全是 str 契约，且保逐字断言）。"""
        calls: list = []

        async def fake_chat(messages, **kwargs):
            calls.append(kwargs.get("scene"))
            return '{"ok":true}'

        orig = ba.chat_with_fallback
        ba.chat_with_fallback = fake_chat
        try:
            out = _await(ba._collect_json_str(
                [{"role": "user", "content": "给我 JSON"}],
                scene="bid_analysis", timeout=60))
        finally:
            ba.chat_with_fallback = orig
        assert out == '{"ok":true}', out
        assert calls == ["bid_analysis"]

    def test_non_object_triggers_repair_then_succeeds(self):
        """顶层不是对象（裸数组）→ 结构校验失败 → 进修复轮（旧实现会直接放行）。"""
        calls: list = []
        responses = ["[1,2,3]", '{"ok":1}']

        async def fake_chat(messages, **kwargs):
            calls.append(kwargs.get("scene"))
            return responses[len(calls) - 1]

        orig = ba.chat_with_fallback
        ba.chat_with_fallback = fake_chat
        try:
            out = _await(ba._collect_json_str(
                [{"role": "user", "content": "给我 JSON"}],
                scene="bid_analysis_merge", timeout=60))
        finally:
            ba.chat_with_fallback = orig
        assert len(calls) == 2, "必须进入定向修复轮"
        assert out == '{"ok":1}'

    def test_all_rounds_fail_falls_back_to_empty_object_sentinel(self):
        """兜底必须是 "{}" —— _json_all_empty / is_missing_result 的失败哨兵契约。"""
        async def fake_chat(messages, **kwargs):
            return "不是 JSON"

        orig = ba.chat_with_fallback
        ba.chat_with_fallback = fake_chat
        try:
            out = _await(ba._repair_json("{bad", [{"role": "user", "content": "x"}],
                                         scene="bid_analysis_merge"))
        finally:
            ba.chat_with_fallback = orig
        assert out == "{}", out

    def test_repair_shell_recovers_json_without_ai(self):
        """确定性下界：AI 全挂时仍能从原文里抠出可解析的 JSON。"""
        async def fake_chat(messages, **kwargs):
            raise RuntimeError("provider 全挂")

        orig = ba.chat_with_fallback
        ba.chat_with_fallback = fake_chat
        try:
            out = _await(ba._repair_json(
                '前缀{"project_name": "甲"}后缀', [{"role": "user", "content": "x"}]))
        finally:
            ba.chat_with_fallback = orig
        assert json.loads(out)["project_name"] == "甲"

    def test_single_call_never_raises(self):
        """JSON 项失败必须兜底 "{}"，绝不能把 ValueError 抛给上层
        （会被 SSE 版放大成整项 3 次重跑）。"""
        async def fake_chat(messages, **kwargs):
            return "不是 JSON"

        orig = ba.chat_with_fallback
        ba.chat_with_fallback = fake_chat
        try:
            out = _await(ba._run_single_call(
                {"item_id": "projectBasicInfo", "label": "基本信息",
                 "output_type": "json", "prompt": "p"},
                "招标文件正文", "json"))
        finally:
            ba.chat_with_fallback = orig
        assert out == "{}"

    def test_both_failure_paths_use_empty_object_sentinel(self):
        """两处调用点的失败兜底都必须是 ``"{}"``。

        ⚠️ 这条是 A/B M10 撞出来的**零覆盖路径**：护栏原先只测了 `_repair_json`
        薄壳与单项提取，合并路径（分段合并的 JSON 分支）的哨兵没人守 ——
        变异成 ``""`` 时护栏全绿。而 ``""`` 会被 `_json_all_empty` 判为
        「非 dict → False」→ `is_missing_result` 判「不缺失」→ **假绿**：
        用户看到解析成功，实际什么都没提取到。
        """
        # ⚠️ `_run_single_item` 里有**两处**同形哨兵（分段模板兜底 + 合并失败兜底），
        #    故不能用 `in` 粗判 —— 必须用「合并失败日志之后必须紧跟哨兵」的顺序
        #    判据，否则其中一处被改坏时护栏仍全绿（A/B M10 实测）。
        src_item = _src_of(ba._run_single_item)
        i_log = src_item.find("[bid] 合并项 JSON 生成/修复失败")
        i_sentinel = src_item.find('result_text = "{}"', max(i_log, 0))
        assert i_log >= 0, "合并失败必须打日志"
        assert i_sentinel > i_log, "合并失败日志之后必须以 {} 兜底"
        assert 'return "{}"' in _src_of(ba._run_single_call)

    def test_repair_key_is_named_constant_not_literal(self):
        """AST 护栏（test_json_repair_key_parity）要求传具名常量，禁止字面量。"""
        src = _src_of(ba._collect_json_str)
        assert "repair_key=GENERIC_REPAIR_KEY" in src
        # 注释里会提到 OUTLINE_REPAIR_KEY（说明为什么不用它），故只判**实参**
        assert "repair_key=OUTLINE_REPAIR_KEY" not in src, (
            "提取族不得用目录族修复提示词")


def _src_of(fn) -> str:
    import inspect
    return inspect.getsource(fn)


# ===========================================================================
# E 组 · 源文件字节体检（R57 踩过、本轮又踩到 → 必须变成判据）
# ===========================================================================
_TOUCHED_R58 = [
    "app/config.py",
    "app/routers/sse_handlers.py",
    "app/routers/bid_analysis.py",
    "app/routers/_chart_pipeline.py",
    "app/services/content_runtime.py",
    "app/services/facts_builder.py",
    "tests/test_r58_closeout_20261010.py",
]


class TestSourceByteIntegrity:
    """⚠️ 本轮（R58）**第二次**踩到行尾坑，形态与 R57 不同、后果同源：

    R57 是「文本编辑工具把行尾再处理一遍」→ 4 个文件每行变成 ``\r\r\n``；
    R58 是补丁脚本用 ``Path.read_text()`` 读取 —— 它默认开启 universal
    newlines，把 CRLF 读成 ``\n``，写回后**整个文件被改成 LF**（工作树行尾
    与 HEAD 完全不同，``git diff`` 判为全文重写，真实改动无法 review）。

    两次的共同判据：**孤立 CR 必须为 0、不得混用行尾**。
    本仓 ``backend/app/**.py`` 与 ``backend/tests/**.py`` 均为 CRLF 仓库文件，
    任何改写都必须走 ``read_bytes`` / ``write_bytes``。
    """

    @staticmethod
    def _read(rel: str) -> bytes:
        for base in (Path(__file__).resolve().parent.parent, Path.cwd()):
            p = base / rel
            if p.exists():
                return p.read_bytes()
        raise AssertionError(f"找不到待测源文件：{rel}")

    def test_no_lone_cr_or_control_bytes(self):
        for rel in _TOUCHED_R58:
            data = self._read(rel)
            crlf = data.count(b"\r\n")
            lone = data.count(b"\r") - crlf
            assert lone == 0, f"{rel} 有 {lone} 个孤立 CR"
            for name, b in (("NUL", b"\x00"), ("BEL", b"\x07"), ("FF", b"\x0c")):
                assert data.count(b) == 0, f"{rel} 含 {name}"
            assert data.decode("utf-8", errors="replace").count("\ufffd") == 0, rel

    def test_line_endings_are_not_mixed(self):
        for rel in _TOUCHED_R58:
            data = self._read(rel)
            crlf = data.count(b"\r\n")
            bare = data.count(b"\n") - crlf
            assert not (crlf and bare), f"{rel} 混用行尾：CRLF={crlf} LF={bare}"

    def test_every_touched_file_compiles(self):
        import py_compile
        for rel in _TOUCHED_R58:
            for base in (Path(__file__).resolve().parent.parent, Path.cwd()):
                p = base / rel
                if p.exists():
                    py_compile.compile(str(p), doraise=True)
                    break
            else:
                raise AssertionError(f"找不到：{rel}")
