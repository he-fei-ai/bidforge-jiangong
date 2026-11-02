"""R130（2026-10-24）——《标书智能体（六）——超长文本生成和图文控制》非图表缺口引入护栏。

按用户裁定：图表/配图类机制（正文与配图分离、配图计划、全局拍板、Mermaid
校验修复提示词等）维持本软件「图表与正文一体生成」标准，不在本轮范围。
本文件只覆盖核对后确认的两项真缺口（外部代码依据见
docs/reference_research/04_content.md）：

1. regenerateRequirement（用户重新生成时填写的额外要求）
   —— 外部 contentGenerationTask.cjs:2989 ``payload.requirement``，
      04_content.md §4.1 编排链路 + §4.2 正文生成链路两处注入；
2. 扩写无进展退出 ``MAX_EXPANSION_NO_PROGRESS_ROUNDS = 3``
   —— 外部 :4778-4796 / §6.2「连续多轮没有增加字数时退出」。

所有断言均附代码依据；不传新参数时缺省路径与引入前逐字节一致（纯加法式扩展）。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.services.content_plan import plan_section_content
from app.services.content_runtime import should_continue_round

_SSE_SRC = (
    Path(__file__).resolve().parents[1] / "app" / "routers" / "sse_handlers.py"
).read_text(encoding="utf-8")

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _fake_plan_json() -> str:
    return json.dumps({
        "writing_focus": "聚焦", "knowledge_names": [], "fact_titles": [],
        "table_needed": False, "table_purpose": "",
    })


# ── A. 编排链路：额外要求以独立 user 消息插在编排任务消息之前 ──────────
async def test_plan_messages_include_requirement_before_task():
    """对齐外部 §4.1 分层：…同级 → 用户额外要求 → 编排任务（消息 3 条）。"""
    captured: dict = {}

    async def _chat(messages, **kwargs):
        captured["messages"] = messages
        return _fake_plan_json()

    plan = await plan_section_content(
        _chat,
        leaf={"title": "支护", "description": "说明"},
        word_budget=1500, parent_chain="上级", sibling_lines="同级",
        project_brief="概述", knowledge_names=[], fact_titles=[],
        regenerate_requirement="重点补充雨季施工措施",
    )
    assert plan.writing_focus == "聚焦"          # 编排本体不受影响
    msgs = captured["messages"]
    assert len(msgs) == 3                        # system + 额外要求 + 编排任务
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == (
        "用户对本次重新生成的额外要求：\n重点补充雨季施工措施")
    # 编排任务消息仍在最后（外部要求消息位于其之前）
    assert "请**仅输出严格 JSON**" in msgs[2]["content"]


async def test_plan_messages_default_unchanged_without_requirement():
    """未传额外要求 → 消息结构与本项引入前逐字一致（2 条）。"""
    captured: dict = {}

    async def _chat(messages, **kwargs):
        captured["messages"] = messages
        return _fake_plan_json()

    await plan_section_content(
        _chat, leaf={"title": "支护"}, word_budget=1500,
        parent_chain="", sibling_lines="", project_brief="",
        knowledge_names=[], fact_titles=[],
    )
    assert len(captured["messages"]) == 2


# ── B. 生成链路接线（sse_handlers 为巨型闭包，按仓内惯例做源码锚点） ──────
def test_requirement_parsed_from_request_body():
    """generate_content 必须读取新参数，并兼容外部载荷命名 requirement。"""
    assert 'body.get("regenerate_requirement")' in _SSE_SRC
    assert 'body.get("requirement")' in _SSE_SRC


def test_requirement_passed_to_plan_stage():
    """编排链路注入点：plan_section_content 调用点显式传参
    （逐个显式书写，遵守 RenderDynamicBlindspots 静态护栏纪律）。"""
    assert "regenerate_requirement=regenerate_requirement" in _SSE_SRC


def test_requirement_injected_into_both_generation_branches():
    """生成链路两个分支（stable-prefix 开/关）都必须拿到额外要求。

    R169 D2 收口后：注入头 ``_req_block`` 只构造**一次**并 append 进
    ``_chapter_increments`` 单一装配点，双分支同源消费（stable 经
    ``_incs_stable_text``、非 stable 经 ``_incs_plain_text``）——
    旧「两处独立注入头」的双口径已铲除。
    """
    assert _SSE_SRC.count("【用户对本次重新生成的额外要求】") == 1, (
        "额外要求注入头应只构造一次（单一装配点，杜绝双口径分叉）")
    assert '_chapter_increments.append(("\\n", _req_block))' in _SSE_SRC
    assert "_incs_stable_text" in _SSE_SRC, "stable 分支消费点丢失"
    assert "_incs_plain_text" in _SSE_SRC, "非 stable 分支消费点丢失"


def test_requirement_block_ordered_before_plan_guidance():
    """顺序对齐外部 §4.2：额外要求位于编排引导语之前（装配序即注入序）。"""
    idx_req = _SSE_SRC.find('_chapter_increments.append(("\\n", _req_block))')
    idx_plan = _SSE_SRC.find(
        '_chapter_increments.append(("\\n", _plan_guidance))')
    assert idx_req != -1 and idx_plan != -1 and idx_req < idx_plan


# ── C. 扩写无进展退出（外部 MAX_EXPANSION_NO_PROGRESS_ROUNDS = 3） ────────
def test_no_progress_constant_matches_external():
    from app.routers import sse_handlers as sh
    assert sh.CONTENT_EXPAND_NO_PROGRESS_ROUNDS == 3   # 外部 cjs:30


def test_no_progress_gate_in_should_continue_round():
    """判定点收口在唯一判定点 should_continue_round（两参数缺省 0 即旧行为）。"""
    base = dict(wc=100, word_budget=1200, continue_count=0,
                min_passes=0, max_rounds=10)
    # 未启用（max=0）：不看无进展计数，与本项引入前逐字等价
    assert should_continue_round(
        **base, no_progress_rounds=99, max_no_progress_rounds=0) is True
    # 启用但未达上限：正常放行
    assert should_continue_round(
        **base, no_progress_rounds=2, max_no_progress_rounds=3) is True
    # 连续 3 轮无进展：即使远未达字数触发线也终止（外部 :4792-4793）
    assert should_continue_round(
        **base, no_progress_rounds=3, max_no_progress_rounds=3) is False
    # 既有语义不回归：字数已达触发线仍不续写
    assert should_continue_round(
        wc=1000, word_budget=1200, continue_count=0, min_passes=0,
        max_rounds=10, no_progress_rounds=0, max_no_progress_rounds=3) is False


def test_continue_loop_wired_for_no_progress():
    """_continue_if_needed 接线：轮初快照、增长清零、停滞递增、判定入 while。"""
    assert "max_no_progress_rounds=CONTENT_EXPAND_NO_PROGRESS_ROUNDS" in _SSE_SRC
    assert "_round_start_wc = wc" in _SSE_SRC
    assert "no_progress_rounds = 0" in _SSE_SRC      # 初始化与增长清零
    assert "no_progress_rounds += 1" in _SSE_SRC     # 停滞递增


# ── D. 前端入口（全链路：弹窗输入 → 请求体字段） ─────────────────────
def test_frontend_regenerate_dialog_passes_requirement():
    tsx = (_REPO_ROOT / "frontend" / "src" / "pages"
           / "SchemeWorkbenchPage.tsx").read_text(encoding="utf-8")
    assert "本次重新生成的额外要求" in tsx      # 重写本章弹窗有输入位
    assert "regenerate_requirement" in tsx     # 随请求体下发（extra + body 两处）
    assert "body.regenerate_requirement = extra.regenerate_requirement.trim()" in tsx
