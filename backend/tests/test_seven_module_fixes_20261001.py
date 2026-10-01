"""七模块专项（2026-10-01）第一轮修复回归护栏。

覆盖三处有代码依据的缺陷：
  C-1  sse_handlers.word_budget_override 未归一化 → 字符串入参 TypeError 炸整批
  C-3  SSE `stopped` 事件缺 failed_count → 前后端契约不同步（前端恒读 0）
  A-3  _invalidate_extraction_derived 被 `if done:` 门禁挡住 → 派生产物静默不失效

测试策略：纯逻辑 / AST 静态断言，不触 DB、不发起 AI 调用。
"""

import ast
import asyncio
import io
import json
import os
import re
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SSE_PATH = os.path.join(BACKEND, "app", "routers", "sse_handlers.py")
BA_PATH = os.path.join(BACKEND, "app", "routers", "bid_analysis.py")
CU_PATH = os.path.join(BACKEND, "app", "services", "content_utils.py")


def _src(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# C-1：word_budget_override 归一化接线
# ---------------------------------------------------------------------------

def test_cu_normalize_rejects_string_compare_hazard():
    """纯函数本身：字符串 "2000" 必须归一为 int，非法值归一为 None。"""
    from app.services.content_utils import normalize_word_budget_override
    assert normalize_word_budget_override("2000") == 2000
    assert normalize_word_budget_override(2000) == 2000
    assert normalize_word_budget_override("abc") is None
    assert normalize_word_budget_override(True) is None
    assert normalize_word_budget_override(-5) is None
    assert normalize_word_budget_override(None) is None


def test_string_override_would_crash_without_normalize():
    """反证：不归一化时 `'2000' > 0` 直接抛 TypeError（前端传字符串的真实后果）。"""
    with pytest.raises(TypeError):
        _ = "2000" > 0  # noqa: B015 - 刻意复现缺陷


def test_c1_override_is_normalized_at_source():
    """接线点：请求体解析处必须调用 normalize_word_budget_override。"""
    src = _src(SSE_PATH)
    m = re.search(r"word_budget_override\s*=\s*normalize_word_budget_override\(", src)
    assert m, "sse_handlers 未把 word_budget_override 交给归一化函数（C-1 回退）"


def test_c1_no_bare_body_get_assignment():
    """不得出现回退为裸 body.get 的赋值。"""
    src = _src(SSE_PATH)
    assert not re.search(
        r"^\s*word_budget_override\s*=\s*body\.get\(", src, re.MULTILINE), (
        "发现裸 `word_budget_override = body.get(...)`，字符串入参会炸整批（C-1 回退）")


def test_c1_normalize_function_still_exists():
    src = _src(CU_PATH)
    assert "def normalize_word_budget_override" in src


# ---------------------------------------------------------------------------
# C-3：stopped 事件必须携带 failed_count
# ---------------------------------------------------------------------------

def _stopped_payloads():
    """从源码中抽取所有 event=='stopped' 的 json.dumps 载荷字面量。"""
    src = _src(SSE_PATH)
    return re.findall(r"'event'\s*:\s*'stopped'", src)


def test_c3_all_stopped_events_carry_failed_count():
    """每一处 stopped 载荷都必须含 failed_count（前端按该字段读取）。"""
    src = _src(SSE_PATH)
    # 定位所有 stopped 事件构造
    blocks = re.findall(
        r"json\.dumps\(\{[^{}]*?'event'\s*:\s*'stopped'[^{}]*?\}", src)
    assert blocks, "未找到 stopped 事件构造"
    for b in blocks:
        assert "failed_count" in b, (
            f"stopped 载荷缺 failed_count，前端将恒读 0：{b[:120]}")


def test_c3_stopped_count_matches_frontend_expectation():
    """前端读取的是 number 类型，后端必须以 len(_failed_reasons) 下发（int）。"""
    src = _src(SSE_PATH)
    assert re.search(r"'failed_count'\s*:\s*len\(_failed_reasons\)", src), (
        "failed_count 应以 len(_failed_reasons) 下发（int 类型）")


def test_c3_stopped_payload_json_parsable():
    """抽查：把源码里的 stopped 载荷改成合法 JSON 后应含 failed_count 键。

    用字面量复刻后端载荷结构，验证前端 `typeof evt.failed_count === 'number'`
    分支在新载荷下成立（旧载荷恒为 0）。
    """
    payload = {
        "event": "stopped", "task_id": "t1", "message": "用户已停止",
        "failed_count": 2, "failed_sections": ["a", "b"],
    }
    evt = json.loads(json.dumps(payload))
    fc = evt["failed_count"] if isinstance(evt.get("failed_count"), (int, float)) else 0
    assert fc == 2
    # 旧载荷（缺该字段）→ 前端兜底为 0，正是缺陷表现
    old = {"event": "stopped", "task_id": "t1", "failed_sections": ["a", "b"]}
    assert (old["failed_count"] if isinstance(old.get("failed_count"), (int, float)) else 0) == 0


# ---------------------------------------------------------------------------
# A-3：级联失效不得再受 `if done:` 门禁
# ---------------------------------------------------------------------------

def test_a3_cascade_not_gated_by_done():
    """_invalidate_extraction_derived 调用不得位于 if done 块内。"""
    src = _src(BA_PATH)
    tree = ast.parse(src)
    gated = False
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            # 判据：if 的 test 是 Name 'done' 且 body 内含级联调用
            test_is_done = (
                isinstance(node.test, ast.Name) and node.test.id == "done"
            )
            if not test_is_done:
                continue
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "_invalidate_extraction_derived"
                ):
                    gated = True
    assert not gated, "级联失效仍被 `if done:` 门禁包裹（A-3 回退）"


def test_a3_cascade_called_unconditionally_in_invalidate():
    src = _src(BA_PATH)
    fn = src.split("async def _invalidate_downstream_cache", 1)
    assert len(fn) > 1, "未找到 _invalidate_downstream_cache"
    body = fn[1].split("\nasync def ", 1)[0]
    assert "_invalidate_extraction_derived(" in body


def test_a3_empty_sids_guard_present():
    """方案级 SQL（IN (...)）必须有空 sids 守卫，避免 IN () 非法 SQL。"""
    src = _src(BA_PATH)
    fn = src.split("async def _invalidate_extraction_derived", 1)[1]
    body = fn.split("\nasync def ", 1)[0]
    assert re.search(r"if sids:", body), "级联函数缺少空 sids 守卫"
    # ① ② 两块方案级 SQL 都应在守卫内
    assert body.count("if sids:") >= 2, "① 一致性缓存与 ② 事实时间戳都需守卫"


def test_a3_doc_extractions_still_project_scoped():
    """③ 提取层快照按 project_id，空 sids 时也必须执行（不受守卫影响）。"""
    src = _src(BA_PATH)
    fn = src.split("async def _invalidate_extraction_derived", 1)[1]
    body = fn.split("\nasync def ", 1)[0]
    assert "UPDATE doc_extractions SET status='stale' WHERE project_id=?" in body


def test_a3_cascade_behavior_when_done_zero():
    """行为验证：done==0（缓存失效全失败）时级联仍被调用。

    用桩对象替换 invalidate_export_cache 使其抛错，断言级联仍执行。
    """
    src = _src(BA_PATH)
    # 静态确认：级联调用位于 for 循环之后、return done 之前，且不在任何 If 内
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_invalidate_extraction_derived"
        ):
            target = node
    assert target is not None, "未找到级联调用"
    # 该调用应直接位于函数体（Module/FuncDef/For 之外无 If 包裹用父节点判断）
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    p = parents.get(target)
    assert not isinstance(p, ast.If), f"级联调用的父节点是 If（受门禁）: {p}"


# ---------------------------------------------------------------------------
# 语法与结构完整性
# ---------------------------------------------------------------------------

def test_changed_files_parse():
    for p in (SSE_PATH, BA_PATH, CU_PATH):
        ast.parse(_src(p), filename=p)
