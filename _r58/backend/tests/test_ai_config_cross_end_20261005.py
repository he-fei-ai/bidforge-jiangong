"""AI 配置跨端契约护栏（2026-10-05）

背景：前端 AIConfigPage 的数值字段区间、PromptEditorPage 的提示词上限，
历史上多次与后端硬编码双份并漂移（见 `aiConfigGovernance.test.tsx` 头部记录：
「并发界面可填 8、后端上限 5，提交后被静默收敛」）。随后后端把并发上限
配置化（`settings.max_concurrency`），但前端仍写死 5 —— 形成一个**新的**
漂移面（管理员上调后界面「填不上去」，下调后「填了被静默收敛」）。

本护栏锁定三件事：
  1. 并发上限必须是**动态口径** —— 读 `GET /ai/health` 的 `max_concurrency`，
     经 `resolveConcurrencyMax` 解析（非法值回落兜底），不得再写死；
  2. 仍硬编码在两端的区间（max_tokens / temperature / timeout）必须逐值
     对齐后端 `provider_factory._RANGE`；
  3. 前端 `PromptEditorPage.PROMPT_MAX_CHARS` 必须对齐后端
     `audit_service.PROMPT_MAX_CHARS`（唯一源，`routers/prompts.py` 为 re-export）。

⚠️ 前端测试不能用 `fs`（本仓未装 @types/node，tsc 会 TS2307），故跨语言的
**源码接线护栏**放 pytest 侧（与 `test_import_parse_closeout_20261005.py` 同构）；
纯函数行为测试在 `frontend/src/tests/aiConfigGovernance.test.tsx`。
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_AI_PAGE = _REPO / "frontend" / "src" / "pages" / "AIConfigPage.tsx"
_PROMPT_PAGE = _REPO / "frontend" / "src" / "pages" / "PromptEditorPage.tsx"


def _src(path: Path) -> str:
    assert path.exists(), f"缺少 {path}"
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _strip_comments(src: str) -> str:
    """去行注释与块注释（判「是否还有硬编码」时必须先剥离注释示例）。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    lines = [re.sub(r"//.*$", "", ln) for ln in src.split("\n")]
    return "\n".join(lines)


def _form_item_input(src: str, name: str) -> str:
    """取 ``name="X"`` 的 Form.Item 到其结束标签之间的源码片段。"""
    i = src.find(f'name="{name}"')
    assert i >= 0, f'AIConfigPage 缺少 Form.Item name="{name}"'
    j = src.find("</Form.Item>", i)
    assert j > i, f'Form.Item name="{name}" 未见 </Form.Item>'
    return src[i:j]


def _num(text: str, prop: str) -> float:
    m = re.search(rf"{prop}=\{{([\d.]+)\}}", text)
    assert m, f"未找到 {prop} 的数值（{text[:80]!r}）"
    return float(m.group(1))


# --------------------------------------------------------------------------- #
# 1 · 并发上限必须动态（读 /ai/health · max_concurrency），不得写死
# --------------------------------------------------------------------------- #
def test_concurrency_max_is_dynamic_from_health():
    src = _strip_comments(_src(_AI_PAGE))
    assert "resolveConcurrencyMax(health)" in src, (
        "并发上限必须由 GET /ai/health 的 max_concurrency 动态解析"
        "（否则管理员改 settings.max_concurrency 后界面与之脱钩）"
    )
    block = _form_item_input(src, "concurrency")
    assert "max={concurrencyMax}" in block, (
        "并发 Form.Item 的 max 必须绑定 concurrencyMax（动态口径）"
    )
    # 旧缺陷形态：写死 max={5}
    assert not re.search(r"max=\{\s*5\s*\}", block), (
        "并发上限被写死为 5 —— 后端可配置时会产生漂移"
    )


# --------------------------------------------------------------------------- #
# 2 · 仍硬编码的区间必须与后端 _RANGE 逐值对齐
# --------------------------------------------------------------------------- #
def test_hardcoded_numeric_ranges_match_backend_range():
    from app.services.ai.provider_factory import _RANGE

    src = _strip_comments(_src(_AI_PAGE))

    mt = _form_item_input(src, "max_tokens")
    assert (_num(mt, "min"), _num(mt, "max")) == tuple(map(float, _RANGE["max_tokens"])), (
        "前端 Max Tokens 区间与后端 _RANGE['max_tokens'] 不一致"
    )

    tp = _form_item_input(src, "temperature")
    assert (_num(tp, "min"), _num(tp, "max")) == tuple(map(float, _RANGE["temperature"])), (
        "前端 Temperature 区间与后端 _RANGE['temperature'] 不一致"
    )

    to = _form_item_input(src, "timeout")
    assert (_num(to, "min"), _num(to, "max")) == tuple(map(float, _RANGE["timeout"])), (
        "前端超时区间与后端 _RANGE['timeout'] 不一致"
    )


# --------------------------------------------------------------------------- #
# 3 · 提示词上限前后端同源
# --------------------------------------------------------------------------- #
def test_prompt_max_chars_frontend_matches_backend():
    from app.services.audit_service import PROMPT_MAX_CHARS

    src = _strip_comments(_src(_PROMPT_PAGE))
    m = re.search(r"\bPROMPT_MAX_CHARS\s*=\s*(\d+)", src)
    assert m, "PromptEditorPage 未定义 PROMPT_MAX_CHARS 常量"
    assert int(m.group(1)) == PROMPT_MAX_CHARS, (
        f"前端提示词上限 {m.group(1)} != 后端 {PROMPT_MAX_CHARS}"
        "（不一致时用户粘贴超长内容才会被后端 400 拦下，整包上传白跑）"
    )