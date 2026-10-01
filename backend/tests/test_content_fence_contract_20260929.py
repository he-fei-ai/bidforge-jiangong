# -*- coding: utf-8 -*-
"""正文生成 · 未闭合围栏修复函数的**返回契约**防回归（2026-09-29 在线事故）

【事故】运行库 task_registry 记录：正文生成任务 38/38 章**全部失败**，终态消息为
「全部 38 章生成失败（AI 服务异常），请检查模型/AI 配置后重试：生成异常:
'tuple' object has no attribute 'split'」。

【根因】``services/content_utils.auto_fix_unclosed_fences`` 的返回契约是
``(fixed_content, fixes_log)``（导出侧 ``export.py`` 按 ``new, log = ...`` 解包），
而正文生成侧 ``sse_handlers._persist_section`` 沿用了「返回字符串」的旧写法::

    content = auto_fix_unclosed_fences(content)      # content 变成 (str, list)

紧接着的 ``text_word_count(content)`` → ``strip_fenced_code_blocks`` →
``content.split("\\n")`` 抛 ``AttributeError: 'tuple' object has no attribute
'split'``。该行位于**每一章**的落库路径上，且该函数**无论正文是否含未闭合围栏都恒
返回元组** → 全方案每一章 100% 失败。

【为什么没被及时发现】
1. 异常被 ``guarded_gen`` 的兜底 except 吞掉且**从不打印堆栈**，日志里查不到线索
   （本次已一并修复，见 TestGuardedGenObservability）；
2. 失败消息硬编码「AI 服务异常」，把排障方向直接误导到「换模型/改 AI 配置」；
3. 契约在 2 处使用、只有 1 处被同步修改 —— 与本仓反复出现的「同一契约在多处各自
   实现、改一处漏一处」同构。

【护栏】
- TestFenceFixReturnContract：函数契约本身；
- TestFenceFixCallSiteUnpacks：调用点必须解包成 2 元组（AST + 全仓扫描）；
- TestContentGenerationEndToEnd：端到端跑一遍正文生成，断言章节真的落库、正文
  是 str、未闭合围栏被补齐（修复前该组用例必然收到 section_error）；
- TestGuardedGenObservability：全章失败必须留下 ERROR 堆栈。
"""
import ast
import inspect
import json
import os

import pytest

import app.routers.sse_handlers as sh
from app.services.content_utils import (
    auto_fix_unclosed_fences,
    find_unclosed_fences,
)

APP_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")

_FENCE_FUNC = "auto_fix_unclosed_fences"


# ===========================================================================
# 1. 契约本身
# ===========================================================================
class TestFenceFixReturnContract:
    """``auto_fix_unclosed_fences`` 恒返回 ``(content, log)``。"""

    def test_empty_content_returns_tuple(self):
        assert auto_fix_unclosed_fences("") == ("", [])

    def test_no_unclosed_fence_returns_tuple(self):
        text = "## 标题\n\n正文一段。\n\n```python\nprint(1)\n```\n"
        out = auto_fix_unclosed_fences(text)
        assert isinstance(out, tuple) and len(out) == 2
        fixed, log = out
        assert fixed == text          # 无未闭合围栏时正文逐字不变
        assert log == []

    def test_unclosed_fence_returns_fixed_str_and_log(self):
        text = "## 标题\n\n```mermaid\ngraph TD;\nA-->B;\n"
        fixed, log = auto_fix_unclosed_fences(text)
        assert isinstance(fixed, str) and not isinstance(fixed, tuple)
        assert find_unclosed_fences(fixed) == []
        assert len(log) == 1 and log[0]["action"] == "append_close"

    def test_first_element_is_always_str(self):
        """所有分支的第一元素都必须是 str（落库/字数统计的前提）。"""
        for text in ("", "普通正文", "```mermaid\ngraph TD;\nA-->B;"):
            first = auto_fix_unclosed_fences(text)[0]
            assert isinstance(first, str), (text, type(first))

    def test_definition_site_is_tuple_annotated(self):
        """签名必须声明返回 ``tuple[str, list]``（文档与实现同源）。"""
        sig = inspect.signature(auto_fix_unclosed_fences)
        assert "tuple" in str(sig.return_annotation), sig.return_annotation


# ===========================================================================
# 2. 调用点必须解包（AST 精确断言 + 全仓扫描）
# ===========================================================================
def _bare_fence_assignments(tree: ast.AST) -> list[str]:
    """找出「把 auto_fix_unclosed_fences 返回值整体赋给单个变量」的赋值点。

    合法写法只有两类：
      · ``a, b = auto_fix_unclosed_fences(...)``（元组解包）
      · 直接使用/丢弃返回值（表达式语句、return、函数实参）
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not (isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == _FENCE_FUNC):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            if isinstance(t, (ast.Tuple, ast.List)):
                continue                      # 已解包
            offenders.append(f"{getattr(node, 'lineno', '?')}: {ast.dump(t)[:60]}")
    return offenders


class TestFenceFixCallSiteUnpacks:

    def test_persist_section_unpacks_the_tuple(self):
        """正文生成落库路径必须解包成 (content, log)。"""
        tree = ast.parse(inspect.getsource(sh.generate_content))
        offenders = _bare_fence_assignments(tree)
        assert not offenders, (
            "sse_handlers.generate_content 内出现未解包的 "
            f"{_FENCE_FUNC}() 调用（会把元组当正文 → 全章失败）：{offenders}")

    def test_whole_app_has_no_bare_assignment(self):
        """全仓扫描：任何「整体赋值」写法一律失败（防止新增调用点再踩）。"""
        offenders: list[str] = []
        for dirpath, dirnames, filenames in os.walk(APP_ROOT):
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "__"))]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                fp = os.path.join(dirpath, fn)
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                if _FENCE_FUNC + "(" not in text:
                    continue
                try:
                    tree = ast.parse(text)
                except SyntaxError:            # pragma: no cover
                    continue
                rel = os.path.relpath(fp, os.path.dirname(APP_ROOT))
                for item in _bare_fence_assignments(tree):
                    offenders.append(f"{rel}:{item}")
        assert not offenders, (
            f"发现未解包的 {_FENCE_FUNC}() 调用（返回契约是 (content, log)）：\n"
            + "\n".join(offenders))

    def test_export_side_still_unpacks(self):
        """导出侧（历史上正确的那处）不得被改回未解包写法。"""
        import app.routers.export as ex
        tree = ast.parse(inspect.getsource(ex))
        assert not _bare_fence_assignments(tree), "导出侧围栏修复调用点被改回错误写法"


# ===========================================================================
# 3. 端到端：正文生成真的能落库
# ===========================================================================
async def _noop(*a, **k):
    return None


class _FakeRequest:
    class _Client:
        host = "127.0.0.1"

    headers: dict = {}
    client = _Client()

    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


class _OneSectionDB:
    """单叶子章节的最小 DB 替身；记录落库正文供断言。"""

    def __init__(self):
        self.saved: list = []
        self._section = {
            "id": "s1", "scheme_id": "sc1", "parent_id": "", "level": 1,
            "sort_order": 0, "title": "测试章节", "description": "描述",
            "content": "", "word_count": 0, "status": "empty",
            "word_budget": 50, "generation_standard": "",
            "outline_json": None, "review_status": "pending",
        }

    class _Cur:
        def __init__(self, rows):
            self._rows = rows

        async def fetchone(self):
            return self._rows[0] if self._rows else None

        async def fetchall(self):
            return list(self._rows)

    async def execute(self, sql, params=()):
        s = str(sql)
        if "FROM schemes" in s:
            return self._Cur([{
                "id": "sc1", "project_id": "p1", "name": "测试方案",
                "type": "深基坑", "word_budget": 3000, "config_json": "{}",
                "auto_consistency_repair": 0, "generation_standard": "",
            }])
        if "FROM sections" in s and "ORDER BY sort_order" in s:
            return self._Cur([dict(self._section)])
        if s.strip().upper().startswith("UPDATE SECTIONS SET CONTENT"):
            self.saved.append(params[0])
            return self._Cur([])
        if "SUM(word_count)" in s:
            # 收尾统计按位置取值（_wc_row[0]），必须返回序列行而非 dict
            return self._Cur([(0,)])
        return self._Cur([])

    async def executemany(self, *a, **k):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


#: 桩 AI 返回的正文：末尾**故意留一个未闭合的 ```mermaid 围栏**
#: （真实事故里 max_tokens 截断就会造成这种正文）。
_STUB_AI_TEXT = (
    "## 编制说明\n\n"
    "本章说明测试内容。\n\n"
    "```mermaid\ngraph TD;\nA-->B;\n"
)


@pytest.fixture()
def stub_env(monkeypatch):
    """把任务注册/进度/AI 全部打桩，只留正文生成主链路。"""
    finished: list[tuple] = []

    async def _register(*a, **k):
        return "task-x"

    async def _progress(*a, **k):
        return None

    async def _stats(*a, **k):
        return None

    async def _finish(task_id, status="", message="", **k):
        finished.append((status, message))

    async def _save_ckpt(task_id, payload):
        return None

    async def _chat(messages, **k):
        return _STUB_AI_TEXT

    monkeypatch.setattr(sh, "register_task", _register)
    monkeypatch.setattr(sh, "update_progress", _progress)
    monkeypatch.setattr(sh, "update_task_stats", _stats)
    monkeypatch.setattr(sh, "finish_task", _finish)
    monkeypatch.setattr(sh, "_save_content_checkpoint", _save_ckpt)
    monkeypatch.setattr(sh, "settle_global_conn", _noop)
    monkeypatch.setattr(sh, "wait_resume", lambda *a, **k: _noop())
    monkeypatch.setattr(sh, "is_stopped", lambda *a, **k: False)
    monkeypatch.setattr(sh, "has_active_task", lambda *a, **k: False)
    monkeypatch.setattr(sh, "chat_with_fallback", _chat)
    return finished


async def _run(db):
    resp = await sh.generate_content(
        "sc1",
        _FakeRequest({"mode": "all", "concurrency": 1,
                      "auto_consistency_repair": False}),
        db=db,
    )
    chunks: list[str] = []
    async for piece in resp.body_iterator:
        chunks.append(piece.decode("utf-8", "replace")
                      if isinstance(piece, bytes) else str(piece))
    text = "".join(chunks)
    events = [json.loads(ln[6:]) for ln in text.splitlines()
              if ln.startswith("data: ")]
    return events, text


@pytest.mark.asyncio
class TestContentGenerationEndToEnd:

    async def test_section_persists_and_completes(self, stub_env):
        """端到端：一章正文必须真的落库并 completed（修复前必然 section_error）。"""
        db = _OneSectionDB()
        events, text = await _run(db)
        names = [e.get("event") for e in events]
        assert "section_error" not in names, (
            "章节生成失败（修复前为 'tuple' object has no attribute 'split'）："
            + text[:600])
        assert "section_done" in names, f"未收到 section_done：{names}"
        assert "completed" in names, f"未收到 completed：{names}"
        assert any(s == "completed" for s, _ in stub_env), stub_env

    async def test_persisted_content_is_str_and_fence_closed(self, stub_env):
        """落库正文必须是 str，且未闭合围栏已被补齐（本次修复的两条语义）。"""
        db = _OneSectionDB()
        await _run(db)
        assert db.saved, "没有任何章节落库（UPDATE sections SET content 未执行）"
        saved = db.saved[0]
        assert isinstance(saved, str), (
            f"落库正文类型错误：{type(saved)}（修复前是 tuple）")
        assert find_unclosed_fences(saved) == [], (
            f"落库正文仍有未闭合围栏：{saved[-80:]!r}")
        assert saved.startswith("## 编制说明"), "正文被围栏修复破坏"

    async def test_progress_reaches_full(self, stub_env):
        """进度必须真实推进（回归前终态 failed、done 恒为 0）。"""
        db = _OneSectionDB()
        events, _ = await _run(db)
        done = [e for e in events if e.get("event") == "completed"]
        assert done and done[0].get("failed_count") == 0, done
        assert done[0].get("done") == done[0].get("total") == 1, done


# ===========================================================================
# 4. 可观测性：全章失败必须留下堆栈
# ===========================================================================
class TestGuardedGenObservability:

    def _guarded_gen_src(self) -> str:
        src = inspect.getsource(sh.generate_content)
        i = src.index("async def guarded_gen(")
        return src[i:]

    def test_unexpected_exception_branch_logs_with_traceback(self):
        """guarded_gen 的兜底 except 必须 logger.error(..., exc_info=True)。

        事故当天 38 章全部以 `生成异常: ...` 收尾，而日志里**一条堆栈都没有**
        —— 异常被吞掉后仅剩一行事件文本，排查只能靠猜。
        """
        src = self._guarded_gen_src()
        i = src.index('_reason = f"生成异常:')
        tail = src[i:i + 800]
        assert "logger.error(" in tail, (
            "生成异常分支未落日志：全章失败将再次变成「零堆栈」黑洞")
        assert "exc_info=True" in tail, (
            "生成异常分支未带 exc_info=True：只有一行文本、没有堆栈")

    def test_all_failed_branch_keeps_reason_detail(self):
        """全章失败消息必须带失败原因（用户据此判断是 AI 还是本地缺陷）。"""
        src = inspect.getsource(sh.generate_content)
        i = src.index("if total > 0 and not done_ids:")
        block = src[i:i + 1400]
        assert "_failed_reasons" in block, "全章失败分支不再汇总失败原因"
        assert "_reason_txt" in block, "全章失败原因明细被移除，用户无从判断"

    def test_all_failed_message_distinguishes_local_bug_from_ai(self):
        """本地缺陷不得再被播报成「AI 服务异常」（本次事故的直接误导源）。"""
        src = inspect.getsource(sh.generate_content)
        i = src.index("if total > 0 and not done_ids:")
        block = src[i:i + 1400]
        assert "_all_local" in block, "全章失败未区分「本地异常」与「AI 异常」"
        assert "内部处理异常" in block, "本地缺陷缺少可据以排查的指引文案"
        assert "logs/backend.log" in block, "未指引用户/运维去看后端日志"
        # 判定必须以失败原因为依据，而不是无条件写死
        assert 'startswith("生成异常:")' in block, (
            "病因判定未基于失败原因（无条件写死会把本地缺陷说成 AI 故障）")

