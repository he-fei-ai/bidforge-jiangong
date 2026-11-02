# -*- coding: utf-8 -*-
"""R164（2026-11-02）：正文编排阶段改为 JSON 模式 + 校验器 + 单次回退。

背景（生产日志实证）：旧调用 `collect_json_response_with_chat(..., validate_fn=None,
max_tokens=600, json_mode=False)` —— 提示词要求「仅输出严格 JSON」，却既不开
JSON 模式也不给校验器，600 tokens 还会截断；失败后还要空转 2 轮修复
（日志中 36/36 两轮均失败，108 次「回退为无编排」）。

本文件锁定：
  A. 调用参数：json_mode=True、validate_fn 指向 ContentPlanData.validate、
     max_tokens 复用模块常量、max_retries=0（单次回退，不空转修复）；
  B. 非 JSON 产物：只调用 chat 一次即回退为空编排（不产生修复轮）；
  C. 合法 JSON：正常解析并按候选池过滤（行为未退化）。
"""
import json

import app.services.ai.json_response as jr
import pytest
from app.services import content_plan as cp


def _leaf() -> dict:
    return {"title": "基坑支护", "description": "基坑开挖与支护体系", "outline_number": "3.2"}


async def _call_plan(chat_fn) -> cp.ContentPlanData:
    return await cp.plan_section_content(
        chat_fn,
        leaf=_leaf(),
        word_budget=1200,
        parent_chain="第三章 施工方案",
        sibling_lines="3.1 土方开挖",
        project_brief="某市政道路工程",
        knowledge_names=["基坑支护工艺", "降水方案"],
        fact_titles=["基坑深度"],
    )


@pytest.mark.asyncio
async def test_call_params_lock(monkeypatch):
    """A：参数按预期传入（JSON 模式 / 校验器 / 模块常量 / 单次回退）。"""
    captured: dict = {}

    async def _fake_collect(chat_fn, messages, **kwargs):
        captured["kwargs"] = kwargs
        return {"writing_focus": "聚焦基坑支护选型"}, "raw"

    monkeypatch.setattr(jr, "collect_json_response_with_chat", _fake_collect)

    plan = await _call_plan(_noop_chat)

    kwargs = captured["kwargs"]
    assert kwargs["json_mode"] is True
    assert kwargs["max_tokens"] == cp.CONTENT_PLAN_MAX_TOKENS
    assert kwargs["max_retries"] == 0
    assert kwargs["scene"] == "content_plan"
    assert callable(kwargs["validate_fn"])
    # 校验器复用 ContentPlanData.validate：非对象才报问题
    assert kwargs["validate_fn"]({"writing_focus": "x"}) == []
    assert kwargs["validate_fn"]([])  # 顶层非对象 → 有问题
    assert plan.writing_focus == "聚焦基坑支护选型"


async def _noop_chat(messages, **kwargs) -> str:
    return "{}"


@pytest.mark.asyncio
async def test_invalid_json_falls_back_without_repair_rounds():
    """B：非 JSON 产物只调 chat 一次 → 直接回退空编排（无 2 轮空转）。"""
    calls: list[int] = []

    async def _chat(messages, **kwargs) -> str:
        calls.append(1)
        return "抱歉，我无法输出 JSON。"

    plan = await _call_plan(_chat)

    assert len(calls) == 1
    assert plan == cp.ContentPlanData.empty()


@pytest.mark.asyncio
async def test_valid_json_parsed_and_pool_filtered():
    """C：合法 JSON 正常解析，池外条目被过滤（既有行为未退化）。"""
    payload = {
        "writing_focus": "说明支护形式与降水措施",
        "knowledge_names": ["基坑支护工艺", "池外素材"],
        "fact_titles": ["基坑深度"],
        "table_needed": True,
        "table_purpose": "对比支护参数",
    }

    async def _chat(messages, **kwargs) -> str:
        return json.dumps(payload, ensure_ascii=False)

    plan = await _call_plan(_chat)

    assert plan.writing_focus == "说明支护形式与降水措施"
    assert plan.knowledge_names == ["基坑支护工艺"]
    assert plan.fact_titles == ["基坑深度"]
    assert plan.table_needed is True
    assert plan.table_purpose == "对比支护参数"
