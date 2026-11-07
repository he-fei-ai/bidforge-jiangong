"""R186（2026-11-07）目录链路截断留痕：静默硬切片 → ``clip_text_notice``。

修复前 ``sse_handlers.py`` 目录链路对 ``project_brief`` / ``reference_outline``
直接 ``[:3000]`` / ``[:2000]`` / ``[:1000]`` / ``[:1500]`` 静默硬切：
模型只拿到半截资料却无从得知后面还有内容，日志与指标也没有任何痕迹。

本用例锁住四件事：
1. 未超限时 ``clip_text_notice`` 逐字节返回原文（接线不改既有行为）；
2. 超限时按边界收刀，并附「原文 N 字 / 保留 K 字 / 省略 M 字」的可见账面，
   同时写入 warning 日志（可检索）；
3. 五处 ``project_brief`` / ``reference_outline`` 硬切片全部替换为
   ``clip_text_notice``（逐点断言，退回裸切片即红）；
4. ``requirements_text[:1500]`` 与 G6 优先级表冲突（表登记「编制要求」=2 先保），
   以表为准删除该硬切，统一交给预算器。
"""

import logging
import pathlib
import re

from app.services.prompt_governance import segment_priority_of
from app.services.prompt_material_guard import clip_text_notice

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
_SRC = (_BACKEND / "app" / "routers" / "sse_handlers.py").read_text(encoding="utf-8")

#: 五处接线点：(源码中的精确调用, 上限, 标签)
_WIRED_SITES = (
    ("clip_text_notice(project_brief, 3000, '项目资料摘要')", 3000, "项目资料摘要"),
    ("clip_text_notice(reference_outline, 2000, '目录库参考')", 2000, "目录库参考"),
    ('project_brief=clip_text_notice(project_brief, 2000, "项目资料摘要")', 2000, "项目资料摘要"),
    ('reference_outline=clip_text_notice(reference_outline, 1000, "参考提纲")', 1000, "参考提纲"),
    ('project_brief=clip_text_notice(project_brief, 1500, "项目资料摘要")', 1500, "项目资料摘要"),
)


# ---------------------------------------------------------------------------
# 1. 未超限：逐字节不变（含恰好等于上限的边界）
# ---------------------------------------------------------------------------
def test_clip_notice_under_limit_is_byte_identical():
    for _call, limit, label in _WIRED_SITES:
        text = "资" * limit
        assert clip_text_notice(text, limit, label) == text, (label, limit)
    # 空文本 / 零上限：原样返回
    assert clip_text_notice("", 3000, "项目资料摘要") == ""
    assert clip_text_notice("任何内容", 0, "项目资料摘要") == "任何内容"


# ---------------------------------------------------------------------------
# 2. 超限：可见账面 + 日志可查
# ---------------------------------------------------------------------------
_NOTICE_RE = re.compile(r"\[……(?P<label>.+?)已截断：原文 (?P<orig>\d+) 字，此处保留前 (?P<kept>\d+) 字，已省略 (?P<omitted>\d+) 字\]")


def test_clip_notice_over_limit_leaves_ledger():
    text = "混凝土 C30 浇筑。" * 400  # 远超 3000
    out = clip_text_notice(text, 3000, "项目资料摘要")
    m = _NOTICE_RE.search(out)
    assert m, out[-120:]
    assert m.group("label") == "项目资料摘要"
    assert int(m.group("orig")) == len(text)
    assert int(m.group("kept")) <= 3000
    assert int(m.group("omitted")) == len(text) - int(m.group("kept"))
    # 保留部分确实是原文前缀（按边界收刀，不是随机截断）
    assert out.startswith(text[: int(m.group("kept"))])


def test_clip_notice_over_limit_logs_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="prompt_material_guard"):
        clip_text_notice("资" * 4000, 1500, "项目资料摘要")
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "上下文截断（项目资料摘要）" in joined
    assert "已省略" in joined


# ---------------------------------------------------------------------------
# 3. 逐点接线锁：任何一处退回裸切片都必须红
# ---------------------------------------------------------------------------
def test_short_outline_project_brief_uses_clip_notice():
    assert "clip_text_notice(project_brief, 3000, '项目资料摘要')" in _SRC
    assert "project_brief[:3000]" not in _SRC


def test_short_outline_reference_outline_uses_clip_notice():
    assert "clip_text_notice(reference_outline, 2000, '目录库参考')" in _SRC
    assert "reference_outline[:2000]" not in _SRC


def test_level1_project_brief_uses_clip_notice():
    assert 'project_brief=clip_text_notice(project_brief, 2000, "项目资料摘要")' in _SRC
    assert "project_brief[:2000]" not in _SRC


def test_level1_reference_outline_uses_clip_notice():
    assert 'reference_outline=clip_text_notice(reference_outline, 1000, "参考提纲") or "无"' in _SRC
    assert "reference_outline[:1000]" not in _SRC


def test_sublevel_project_brief_uses_clip_notice():
    # 单章 + 批量两处同口径
    assert _SRC.count('project_brief=clip_text_notice(project_brief, 1500, "项目资料摘要")') == 2
    assert "project_brief[:1500]" not in _SRC


# ---------------------------------------------------------------------------
# 4. 编制要求：与 G6 优先级表冲突 → 以表为准，删除重复硬切
# ---------------------------------------------------------------------------
def test_requirements_follow_priority_table_not_hard_slice():
    # G6 表已登记「编制要求」=2（先保）；代码里不得再出现 1500 字硬切
    assert segment_priority_of("编制要求") == 2
    assert "requirements_text[:" not in _SRC
    assert _SRC.count('requirements=requirements_text or "无"') == 2
