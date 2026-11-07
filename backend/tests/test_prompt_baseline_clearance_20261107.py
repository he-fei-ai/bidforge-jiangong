"""R186 清零轮：把棘轮里的 3 条存量基线修掉（2026-11-07）。

原 `_BASELINE_DEBT` 三条：
1. ``global_facts.adjust_facts`` 的 ``instruction[:3000]``
2. ``sections.adjust_outline`` 的 ``instruction[:3000]``
3. ``_try_outline_patch`` 的章节标题串 ``[:2000]``

判断依据（《派单》："先看这个上限本身有没有必要"）：
- 调整指令是**用户自己写的任务本体**，砍尾等于用户话没说完模型就动手，
  且此前无任何提示。该链路下游没有硬性窗口约束（只有超时控制），
  因此**删除任意字符上限**，完整指令直接进 prompt；预算器开启时按
  G6 优先级表「调整要求 = 1」处理，未开启时逐字节原样。
- 章节标题串是程序拼出的外部数据，保留 2000 字上限，但改走
  ``clip_text_notice``：未超限逐字节原样，超限附「原文/保留/省略」账面。

本用例锁住：正常长度逐字节不变、超长时完整保留（指令）/ 有账面（标题）、
三处接线不再出现裸切片、棘轮白名单已收缩为 2 条无关限长。
"""

import pathlib
import re

from app.services.ai.prompts._registry import render
from app.services.prompt_material_guard import clip_text_notice

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
_SECTIONS = (_BACKEND / "app" / "routers" / "sections.py").read_text(encoding="utf-8")
_FACTS = (_BACKEND / "app" / "routers" / "global_facts.py").read_text(encoding="utf-8")
_REVIEW = (_BACKEND / "app" / "routers" / "_sse_outline_review.py").read_text(encoding="utf-8")
_RATCHET = (_BACKEND / "tests" / "test_prompt_truncation_ratchet_20261107.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. 调整指令：不再有字符上限，且已交给预算器
# ---------------------------------------------------------------------------
def test_adjust_instructions_are_not_hard_clipped():
    for name, src in (("sections", _SECTIONS), ("global_facts", _FACTS)):
        assert "instruction[:3000]" not in src, name
        assert "instruction=instruction," in src, name
        # 交给预算器（「调整要求」优先级 1；开关关闭时原样返回）。
        # sections 的 adjust_outline 走 report 变体（需要逐段账面回传用户），
        # global_facts 仍走只取结果的便捷入口。
        assert (
            "prompt = _apply_prompt_context_budget(prompt)" in src
            or "prompt, _budget_info = _apply_prompt_context_budget_report(prompt)" in src
        ), name


def test_long_adjust_instruction_survives_render():
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
