"""R186 清零轮：把棘轮里的 3 条存量基线修掉（2026-11-07）。

原 `_BASELINE_DEBT` 三条：
1. ``global_facts.adjust_facts`` 的 ``instruction[:3000]``
2. ``sections.adjust_outline`` 的 ``instruction[:3000]``
3. ``_try_outline_patch`` 的章节标题串 ``[:2000]``

判断依据（《派单》："先看这个上限本身有没有必要"）：
- 调整指令是**用户自己写的任务本体**，砍尾等于用户话没说完模型就动手。
  R186 曾据此删除任意上限，但漏了「``prompt_context_budget`` 默认关闭时
  预算器直通」—— 无界指令会直接超模型窗口（502）。现改为 **6000 字物理
  上限**（``INSTRUCTION_PROMPT_MAX_CHARS``，与开关无关），超限用
  ``clip_text_notice`` 留模型可见说明；预算器开启时再在其后按优先级收紧。
- 章节标题串是程序拼出的外部数据，保留 2000 字上限，但改走
  ``clip_text_notice``：未超限逐字节原样，超限附「原文/保留/省略」账面。

本用例锁住：正常长度逐字节不变、超长时指令被物理上限收刀且有模型可见账面、
物理上限先于预算器生效、三处接线不再出现裸切片、棘轮白名单已收缩为 2 条无关限长。
"""

import pathlib
import re

from app.services.ai.prompts._registry import render
from app.services.prompt_material_guard import (
    INSTRUCTION_PROMPT_MAX_CHARS,
    clip_text_notice,
    clip_text_notice_report,
)

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
_SECTIONS = (_BACKEND / "app" / "routers" / "sections.py").read_text(encoding="utf-8")
_FACTS = (_BACKEND / "app" / "routers" / "global_facts.py").read_text(encoding="utf-8")
_REVIEW = (_BACKEND / "app" / "routers" / "_sse_outline_review.py").read_text(encoding="utf-8")
_RATCHET = (_BACKEND / "tests" / "test_prompt_truncation_ratchet_20261107.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. 调整指令：6000 字物理上限（先于预算器），且已交给预算器
# ---------------------------------------------------------------------------
def test_adjust_instructions_are_not_hard_clipped():
    for name, src in (("sections", _SECTIONS), ("global_facts", _FACTS)):
        assert "instruction[:3000]" not in src, name
        assert "instruction=instruction," in src, name
        # 交给预算器（「调整要求」优先级 -1；开关关闭时原样返回）。
        # sections 的 adjust_outline 走 report 变体（需要逐段账面回传用户），
        # global_facts 仍走只取结果的便捷入口。
        assert (
            "prompt = _apply_prompt_context_budget(prompt)" in src
            or "prompt, _budget_info = _apply_prompt_context_budget_report(prompt)" in src
        ), name


def test_long_adjust_instruction_survives_render():
    """只锁**模板渲染层**：``render`` 自身不做任何裁剪。

    ⚠️ 端点层另有 6000 字物理上限（``INSTRUCTION_PROMPT_MAX_CHARS``，与预算
    开关无关）—— 本用例的 >3000 字长文本在 render 后仍完整出现，不代表端点
    会原样下发；端点行为见下方物理上限用例。
    """
    marker = "【必须完整执行的关键要求】" + "不得省略任何一条。" * 600
    assert len(marker) > 3000

    facts_prompt = render("global_facts_adjust_system", current_facts="【现有全局事实】占位", instruction=marker)
    assert marker in facts_prompt

    outline_prompt = render(
        "outline_adjust_system",
        scheme_name="测试方案",
        scheme_type="深基坑",
        current_outline="【现有目录】占位",
        instruction=marker,
    )
    assert marker in outline_prompt


def test_instruction_physical_cap_is_wired_before_budget():
    """两个端点都必须先走物理上限、再走预算器（顺序不可颠倒）。"""
    for name, src in (("sections", _SECTIONS), ("global_facts", _FACTS)):
        cap_call = "instruction, _cap_meta = clip_text_notice_report("
        assert "INSTRUCTION_PROMPT_MAX_CHARS" in src, name
        assert cap_call in src, name
        assert '"调整要求"' in src, name
        assert src.index(cap_call) < src.index("_apply_prompt_context_budget"), (
            f"{name}: 物理上限必须先于预算器调用"
        )


def test_instruction_cap_under_limit_byte_identical():
    text = "把第3章拆成两章，并删除应急预案章。" * 40
    assert 0 < len(text) <= INSTRUCTION_PROMPT_MAX_CHARS
    out, meta = clip_text_notice_report(text, INSTRUCTION_PROMPT_MAX_CHARS, "调整要求")
    assert out == text and meta is None, "未超限必须逐字节原样，且不产生账面"
    # 薄包装与 report 版本输出必须完全一致
    assert clip_text_notice(text, INSTRUCTION_PROMPT_MAX_CHARS, "调整要求") == text


def test_instruction_cap_over_limit_has_ledger():
    # 13 字前缀 + 9 字/句 × 700 = 6313 字 > 6000，确保确实越过物理上限。
    text = "【必须完整执行的关键要求】" + "不得省略任何一条。" * 700
    assert len(text) > INSTRUCTION_PROMPT_MAX_CHARS
    out, meta = clip_text_notice_report(text, INSTRUCTION_PROMPT_MAX_CHARS, "调整要求")
    assert meta is not None
    assert meta["from"] == len(text)
    assert meta["limit"] == INSTRUCTION_PROMPT_MAX_CHARS
    assert meta["to"] <= INSTRUCTION_PROMPT_MAX_CHARS
    assert meta["omitted"] == len(text) - meta["to"]
    assert out.startswith(text[: meta["to"]])
    m = _NOTICE_RE.search(out)
    assert m and int(m.group("orig")) == len(text), out[-120:]
    assert clip_text_notice(text, INSTRUCTION_PROMPT_MAX_CHARS, "调整要求") == out, (
        "薄包装必须与 report 版本逐字节一致"
    )


# ---------------------------------------------------------------------------
# 2. 章节标题串：未超限逐字节不变；超限有账面
# ---------------------------------------------------------------------------
_NOTICE_RE = re.compile(r"已截断：原文 (?P<orig>\d+) 字，此处保留前 (?P<kept>\d+) 字，已省略 (?P<omitted>\d+) 字")


def test_chapter_titles_under_limit_byte_identical():
    joined = "; ".join(f"第{i}章 施工组织设计" for i in range(1, 30))
    assert len(joined) < 2000
    assert clip_text_notice(joined, 2000, "现有一级章节") == joined


def test_chapter_titles_over_limit_has_ledger():
    joined = "; ".join(f"第{i}章 现浇混凝土结构施工及质量控制要点" for i in range(1, 100))
    assert len(joined) > 2000
    out = clip_text_notice(joined, 2000, "现有一级章节")
    m = _NOTICE_RE.search(out)
    assert m, out[-120:]
    assert int(m.group("orig")) == len(joined)
    assert int(m.group("kept")) <= 2000
    assert int(m.group("omitted")) == len(joined) - int(m.group("kept"))
    assert out.startswith(joined[: int(m.group("kept"))])


def test_patch_chapter_titles_wired_to_notice():
    assert '"现有一级章节"' in _REVIEW
    assert 'chapter_titles="; ".join(str(n.get("title") or "") for n in (outline or []) if isinstance(n, dict))[:2000]' not in _REVIEW
    assert "chapter_titles=clip_text_notice(" in _REVIEW


# ---------------------------------------------------------------------------
# 3. 白名单收缩：存量基线字典必须已删除，只剩 2 条无关限长
# ---------------------------------------------------------------------------
def test_baseline_debt_is_cleared_from_ratchet():
    assert re.search(r"^_BASELINE_DEBT\s*[:=]", _RATCHET, re.M) is None, "失效条目必须删除，而不是留着当僵尸豁免"
    # 只数 `_IRRELEVANT_LIMITS` 块内的条目（`_LOGGED_ONLY_IN` 是 R186 新增的
    # 内部判定作用域白名单，不是"无关限长"豁免，不应计入）。
    _irrelevant_block = _RATCHET.split("_IRRELEVANT_LIMITS:", 1)[1].split("\n}\n", 1)[0]
    assert len(re.findall(r'\n    \(\n        "app/', _irrelevant_block)) == 2
