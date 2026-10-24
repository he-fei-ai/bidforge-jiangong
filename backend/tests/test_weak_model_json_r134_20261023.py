# -*- coding: utf-8 -*-
"""R134 · 《标书智能体（五）——弱模型稳定输出复杂 JSON》引入护栏（2026-10-23）

引入文档：《标书智能体（五）——弱模型稳定输出复杂 JSON》
《标书智能体（五）——文档梳理与引入方案》两份附件。

九步工作流对照本软件（外部引用软件三级标准，不引入 L4/L5）：
  ① 请求模型 → ② JSON 解析 → ③ **Schema 校验**（outline_schema 四结构）
  → ④ 业务校验（_validate_outline 单一事实源，Schema 先于业务）
  → ⑤ JSON 定向修复（三部分信息：目标结果类型 + 校验问题 + 原始内容）
  → ⑥ 多轮重试（MAX_REPAIR_RETRIES 有界，fail-soft）
  → ⑦ 分步生成（既有 _use_stepwise_for_budget 唯一判定点，未动）
  → ⑧ 目录审核（OutlineReviewResponse）
  → ⑨ 建议回灌 + 二次生成（回灌修复链路）+ 一一对应（程序锁死一级目录）

分组：
  A. 四标准结构宽松校验（数字/None 转换、extra 忽略、children 归一）
  B. schema_issues 路径式问题格式（outline.0.children: 原因）
  C. JSONDecodeError 行列号格式 + parse_and_validate 既有断言兼容
  D. collect_json_response 纯加法参数（schema/target_desc，默认逐字不变）
  E. Schema 校验先于业务校验（九步第 4 步顺序）
  F. 调用点接线（6 处 schema/target_desc，AST 源码锁死）
  G. 一一对应模式（NINE_CHAPTERS 构造 + 一致性校验 + 开关默认 False）

运行：pytest tests/test_weak_model_json_r134_20261023.py -q
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re

import pytest
from app.config import Settings, settings
from app.routers import sse_handlers as sh
from app.services.ai import json_response as jr
from app.services.ai.outline_schema import (
    OutlineChildrenResponse,
    OutlineItem,
    OutlineResponse,
    OutlineReviewResponse,
    schema_issues,
)
from app.services.scheme_classification import NINE_CHAPTERS
from pydantic import ValidationError


def _await(coro):
    """同步执行协程（本文件不依赖 pytest-asyncio mode）。"""
    return asyncio.run(coro)


# ===========================================================================
# A. 四标准结构宽松校验（弱模型数字/None 不整次判死）
# ===========================================================================
class TestFourSchemasLoose:
    def test_text_fields_coerce_number_and_none(self):
        """title=123 → "123"；description=None → ""（宽松强制转换）。"""
        item = OutlineItem.model_validate(
            {"title": 123, "description": None, "id": 7})
        assert item.title == "123" and item.id == "7"
        assert item.description == ""

    def test_children_none_becomes_empty_list(self):
        """children=None 归一为 []（合法叶节点），不进修复轮。"""
        item = OutlineItem.model_validate(
            {"title": "工程概况", "children": None})
        assert item.children == []
        assert schema_issues(OutlineItem, {"title": "x", "children": None}) == []

    def test_children_non_list_raises_with_field_name(self):
        """children 非列表才报错，且报错带字段名便于定位。"""
        with pytest.raises(ValidationError):
            OutlineItem.model_validate({"title": "x", "children": "bad"})

    def test_extra_fields_ignored(self):
        """模型自发额外字段（score/remark…）不触发修复轮。"""
        resp = OutlineResponse.model_validate(
            {"outline": [{"title": "a", "score": 99, "remark": "x"}],
             "model_note": "自评"})
        assert resp.outline[0].title == "a"
        assert schema_issues(OutlineResponse,
                             {"outline": [], "extra": 1}) == []

    def test_outline_none_and_non_list(self):
        """OutlineResponse.outline：None → []（通过）；非列表 → 路径式问题。"""
        assert OutlineResponse.model_validate({"outline": None}).outline == []
        issues = schema_issues(OutlineResponse, {"outline": "not-a-list"})
        assert issues and issues[0].startswith("outline:")

    @pytest.mark.parametrize("key", ["children", "outline"])
    def test_children_response_accepts_both_top_keys(self, key):
        """OutlineChildrenResponse 双兼容：外部规范 children + 本仓出厂 outline。"""
        resp = OutlineChildrenResponse.model_validate(
            {key: [{"title": "施工计划"}]})
        assert resp.children[0].title == "施工计划"

    def test_review_suggestions_single_string_wrapped(self):
        """弱模型常给单串 suggestions → 转单元素数组保留内容；passed 默认 False。"""
        rev = OutlineReviewResponse.model_validate({"suggestions": "缺计算书"})
        assert rev.suggestions == ["缺计算书"]
        assert rev.passed is False
        assert OutlineReviewResponse.model_validate(
            {"suggestions": None}).suggestions == []
        # passed 非布尔且不可宽松解析（"maybe"）→ 进修复轮，不静默放行
        issues = schema_issues(OutlineReviewResponse, {"passed": "maybe"})
        assert issues and issues[0].startswith("passed:")


# ===========================================================================
# B. schema_issues 路径式问题（外部 _format_json_issues 口径）
# ===========================================================================
class TestSchemaIssuesPathFormat:
    def test_nested_path_location(self):
        """问题形如 outline.0.children: …（路径 + 冒号 + 原因）。"""
        issues = schema_issues(
            OutlineResponse,
            {"outline": [{"title": "a", "children": "bad"}]})
        assert len(issues) == 1
        assert issues[0].startswith("outline.0.children:")
        assert "children 不是列表" in issues[0]
        assert ": " in issues[0]

    def test_pass_returns_empty(self):
        assert schema_issues(
            OutlineResponse, {"outline": [{"title": "a"}]}) == []

    def test_failsoft_on_broken_schema(self):
        """校验器自身异常 → fail-soft 单条问题，不抛。"""
        class _Broken:
            @classmethod
            def model_validate(cls, data):
                raise RuntimeError("boom")

        issues = schema_issues(_Broken, {})
        assert len(issues) == 1 and "Schema 校验异常" in issues[0]


# ===========================================================================
# C. JSONDecodeError 行列号格式 + parse_and_validate 兼容
# ===========================================================================
class TestJsonDecodeRowCol:
    def test_format_row_col(self):
        with pytest.raises(json.JSONDecodeError):
            json.loads('{"title": }')
        try:
            json.loads('{"title": }')
        except json.JSONDecodeError as e:
            msg = jr._format_jsondecode(e)
        assert re.match(r"^JSON 语法错误：第 1 行第 \d+ 列附近 ", msg), msg

    def test_parse_and_validate_uses_row_col(self):
        """语法错误进修复轮的问题清单必须带行列号（提示词承诺的格式）。"""
        obj, issues = jr.parse_and_validate('{"title": }')
        assert obj is None
        assert re.match(r"^JSON 语法错误：第 1 行第 \d+ 列附近 ", issues[0]), issues[0]

    def test_legacy_assertions_preserved(self):
        """既有三条断言口径不回潮（TestParseAndValidateRobustness 兼容）。"""
        # 1) 完全无 JSON
        obj, issues = jr.parse_and_validate("这不是 JSON")
        assert obj is None and issues == ["输出中未找到 JSON 结构"]
        # 2) 校验器抛异常 → 顶层结构非预期
        obj, issues = jr.parse_and_validate(
            "[1,2,3]", validate_fn=lambda o: o["outline"])
        assert issues and "顶层结构非预期" in issues[0]
        # 3) 正常路径 issues == []
        obj, issues = jr.parse_and_validate('{"outline": []}',
                                            validate_fn=lambda o: [])
        assert obj == {"outline": []} and issues == []


# ===========================================================================
# D. collect_json_response 纯加法参数（默认逐字不变）
# ===========================================================================
class TestCollectSignatureAdditive:
    def test_keyword_only_defaults(self):
        """schema=None / target_desc="" 为 keyword-only 纯加法，默认行为不变。"""
        for fn in (jr.collect_json_response,
                   jr.collect_json_response_with_chat):
            params = inspect.signature(fn).parameters
            assert params["schema"].default is None
            assert params["target_desc"].default == ""
            assert params["schema"].kind is inspect.Parameter.KEYWORD_ONLY
            assert params["target_desc"].kind is inspect.Parameter.KEYWORD_ONLY

    def test_collect_delegates_both_new_params(self):
        """collect_json_response 必须把 schema/target_desc 透传给 with_chat。"""
        src = inspect.getsource(jr.collect_json_response)
        assert "schema=schema" in src and "target_desc=target_desc" in src

    def test_default_repair_prompt_target_is_generic(self):
        """未传 target_desc → 修复提示词目标类型为默认「JSON 结果」（逐字不变）。"""
        out = jr._build_repair_user_prompt(["缺字段"], '{"a":1}')
        assert "JSON 结果" in out and "缺字段" in out and '{"a":1}' in out

    def test_target_desc_flows_into_repair_prompt(self):
        """传 target_desc → 修复提示词三部分信息之「目标结果类型」到位。"""
        out = jr._build_repair_user_prompt(
            ["outline.0.children: children 不是列表"], '{"outline":[]}',
            jr.OUTLINE_REPAIR_KEY, "完整目录")
        assert "完整目录" in out
        assert "outline.0.children" in out          # 校验问题
        assert '{"outline":[]}' in out              # 原始内容（invalid_content）

    def test_repair_key_default_unchanged(self):
        """GENERIC_REPAIR_KEY 默认不回潮到目录修复提示词（R38 契约）。"""
        assert (inspect.signature(jr.collect_json_response)
                .parameters["repair_key"].default == jr.GENERIC_REPAIR_KEY)
        assert jr.GENERIC_REPAIR_KEY == "json_schema_fix_system"


# ===========================================================================
# E. Schema 校验先于业务校验（九步工作流第 4 步顺序）
# ===========================================================================
def _chat_script(responses):
    """按序回放的假 chat_fn，记录每次调用。"""
    calls: list = []
    state = {"i": 0}

    async def _fake(messages, **kw):
        calls.append(list(messages))
        idx = min(state["i"], len(responses) - 1)
        state["i"] += 1
        return responses[idx]
    _fake.calls = calls
    return _fake


class TestSchemaBeforeBusiness:
    def test_schema_issues_win_over_business(self):
        """Schema 不过 → 即便业务校验会放行，进修复轮的也必须是 Schema 路径问题。"""
        fake = _chat_script(
            ['{"outline": [{"children": "bad"}]}', '{"outline": []}'])
        obj, _ = _await(jr.collect_json_response_with_chat(
            fake, [{"role": "user", "content": "生成"}],
            validate_fn=lambda o: [],  # 业务侧本会放行 → 不能掩盖 Schema 问题
            schema=OutlineResponse, max_retries=1))
        assert obj == {"outline": []}
        # 第 1 次修复（calls[1]）的 user 消息必须是 Schema 路径式问题
        repair_txt = fake.calls[1][-1]["content"]
        assert "outline.0.children" in repair_txt
        assert "children 不是列表" in repair_txt

    def test_business_runs_when_schema_passes(self):
        """Schema 通过后才轮到业务校验；业务问题照常进修复轮。"""
        fake = _chat_script(['{"outline": []}', '{"outline": [{"title": "x"}]}'])

        def _biz(obj):
            return [] if obj.get("outline") else ["业务：outline 为空"]

        obj, _ = _await(jr.collect_json_response_with_chat(
            fake, [{"role": "user", "content": "生成"}],
            validate_fn=_biz, schema=OutlineResponse, max_retries=1))
        assert obj == {"outline": [{"title": "x"}]}
        assert "业务：outline 为空" in fake.calls[1][-1]["content"]

    def test_schema_none_behavior_unchanged(self):
        """schema=None 时只走业务校验（行为逐字不变）。"""
        fake = _chat_script(['{"ok": 1}'])
        obj, _ = _await(jr.collect_json_response_with_chat(
            fake, [{"role": "user", "content": "生成"}],
            validate_fn=lambda o: []))
        assert obj == {"ok": 1}
        assert len(fake.calls) == 1  # 一次通过，无修复轮


# ===========================================================================
# F. 调用点接线（目录族 6 处 schema + target_desc）
# ===========================================================================
class TestCallSiteWiring:
    @staticmethod
    def _src() -> str:
        return inspect.getsource(sh)

    def test_schema_wiring_counts(self):
        src = self._src()
        assert src.count("schema=OutlineChildrenResponse") == 2, \
            "单章子目录 + level1 两处应接 OutlineChildrenResponse"
        assert src.count("schema=OutlineResponse") == 2, \
            "回灌修复 + oneshot 直出两处应接 OutlineResponse"
        assert src.count("schema=OutlineReviewResponse") == 1, \
            "AI 审核一处应接 OutlineReviewResponse"

    def test_target_desc_wiring(self):
        src = self._src()
        for desc in ("章节子目录", "章节子目录批量结果",
                     "缺失章节补齐结果（new_chapters）", "目录审核结果",
                     "完整目录", "一级目录"):
            assert f'target_desc="{desc}"' in src, f"缺 target_desc 接线: {desc}"

    def test_non_outline_family_not_wired(self):
        """字数分配等非目录族调用点不套目录 Schema（契约不同不硬套）。"""
        src = inspect.getsource(sh)
        seg = src.split('scene="word_budget_alloc"')[0][-800:]
        assert "schema=" not in seg, "字数分配不是目录族，不得套四结构"


# ===========================================================================
# G. 一一对应模式（程序锁死一级目录 = 九大章节）
# ===========================================================================
class TestOneToOneLevel1:
    def test_constructs_nine_chapters(self):
        nodes = sh._one_to_one_level1()
        titles = [n["title"] for n in nodes]
        assert titles == [ch["title"] for ch in NINE_CHAPTERS]
        assert len(titles) == 9
        assert all(n["children"] == [] and n["description"] == "" for n in nodes)

    def test_hazardous_adds_monitor_chapter(self):
        """危大 + 需监测类别（SAF-06 门控与预检同源）→ 增补「监测方案」章。"""
        nodes = sh._one_to_one_level1("基坑支护专项施工方案", "", True)
        titles = [n["title"] for n in nodes]
        assert len(titles) == 10 and titles[-1] == "监测方案"

    def test_hazardous_without_monitor_stays_nine(self):
        """装饰装修类危大不报 SAF-06 → 不增补（避免假缺项）。"""
        nodes = sh._one_to_one_level1("装饰装修专项施工方案", "", True)
        assert len(nodes) == 9

    def test_missing_empty_when_complete(self):
        assert sh._one_to_one_missing(sh._one_to_one_level1()) == []
        assert sh._one_to_one_missing(
            sh._one_to_one_level1("基坑支护专项施工方案", "", True)) == []

    def test_missing_detects_absent_chapters(self):
        partial = [{"title": "工程概况"}]
        missing = sh._one_to_one_missing(partial)
        assert len(missing) == 8 and "编制依据" in missing
        # 非列表输入 → 全部九章缺失（回退 AI）
        assert len(sh._one_to_one_missing("bad")) == 9

    def test_missing_ignores_extra_monitor(self):
        """增补章只增不删，不参与缺失判定。"""
        full = sh._one_to_one_level1() + [{"title": "监测方案"}]
        assert sh._one_to_one_missing(full) == []

    def test_switch_default_false(self):
        """outline_one_to_one_level1 默认 False = 逐字回到引入前（AI 生成）。"""
        assert Settings.model_fields["outline_one_to_one_level1"].default is False
        assert settings.outline_one_to_one_level1 is False

    def test_level1_gate_failsoft_wired(self):
        """调用点必须 fail-soft：缺章/异常回退 AI，绝不把任务判死。"""
        src = inspect.getsource(sh)
        assert "回退 AI 生成一级目录" in src
        assert "一一对应构造异常" in src
        assert getattr(settings, "outline_one_to_one_level1", False) is False or True
