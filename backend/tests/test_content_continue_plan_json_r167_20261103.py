"""R167 · 正文生成「续写本章」模式 plan_json 绑定缺失复现与回归护栏（2026-11-03）。

BUG（P0 · 活体路径）：``generate_content`` 的 ``gen_one`` 闭包内，``_plan_json``
只在非续写分支（mode != "continue"）由 ``_build_generation_context`` 的五元组
解包赋值；而落库调用 ``_persist_and_notify(..., plan_json=_plan_json)`` 为
**两种模式共用**。Python 把「函数体内任意位置赋值过的名字」判为局部变量 ——
续写分支执行到落库点时该名字从未绑定 → UnboundLocalError → 被 guarded_gen
的 except Exception 兜成 section_error("生成异常: …")。

影响面：前端「续写本章」（SchemeWorkbenchPage → POST generate-content
mode="continue"，sse_handlers 一等模式）自 R70（2026-10-11，plan_json
CASE WHEN 透传引入）起 100% 失败 —— 每章报「生成异常」、正文零续写；又因
continue 模式的叶子必有旧正文（mark_db=False），章节状态与字数都不变，
只有日志区一条红字，极难被用户感知。continue 模式此前**零端到端测试覆盖**。

修复：续写分支同样绑定 ``_plan_json``（``_plan_enabled=False`` ⇒ 恒为空串，
与 R70 注释声明的「续写分支显式以 _plan_enabled=False 构建上下文，同样拿到
''」语义一致）。plan_json="" 时落库 SQL 为 ``CASE WHEN ? <> ''`` 保留原列，
编排决策跨轮存活语义不变。

护栏两层：
  A 组 端到端：mode="continue" 全链路跑通（修复前红：每章 section_error
       「生成异常: … '_plan_json'」；修复后绿：completed + 正文确实续写）。
  B 组 静态锁：续写分支五元组解包必须绑定 _plan_json（不得退回丢弃
       形态）；落库调用必须透传 plan_json=_plan_json。
"""

import inspect
import json

import pytest
from app.routers import sse_handlers as sh

# ============================================================================
# 端到端 harness（沿用 test_content_terminal_payload_20261005 的桩 DB 思路）
# ============================================================================


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _ContinueDB:
    """最小桩 DB：1 个**已有正文**的叶子章节（continue 模式的前置条件）。"""

    OLD_CONTENT = "这是续写前的旧正文第一段，用于验证续写链路保留原文。"

    def __init__(self):
        self.saved: list[str] = []
        self._sections = [self._sec("s1", "1.1")]

    @classmethod
    def _sec(cls, sid, num):
        return {
            "id": sid,
            "scheme_id": "sc1",
            "title": f"章节 {sid}",
            "level": 1,
            "parent_id": "",
            "sort_order": 0,
            "word_budget": 200,
            "content": cls.OLD_CONTENT,
            "status": "generated",
            "description": "d",
            "outline_json": json.dumps({"id": num}, ensure_ascii=False),
            "generation_standard": "",
            "review_status": "",
            "word_count": 0,
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
            return self._Cur(
                [
                    {
                        "id": "sc1",
                        "project_id": "p1",
                        "name": "测试方案",
                        "type": "深基坑",
                        "word_budget": 3000,
                        "config_json": "{}",
                        "auto_consistency_repair": 0,
                        "generation_standard": "",
                    }
                ]
            )
        if "FROM sections" in s and "ORDER BY sort_order" in s:
            return self._Cur([dict(x) for x in self._sections])
        if s.strip().upper().startswith("UPDATE SECTIONS SET CONTENT"):
            self.saved.append(params[0])
            return self._Cur([])
        if "COUNT(*)" in s:
            return self._Cur([(0,)])
        if "SUM(word_count)" in s:
            return self._Cur([(sum(len(x) for x in self.saved),)])
        return self._Cur([])

    async def executemany(self, *a, **k):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


async def _noop(*a, **k):
    return None


_CONTINUATION = "这是续写新增的补充段落，用于验证续写链路确实写入了新内容。"


@pytest.fixture()
def stub_env(monkeypatch):
    """打桩任务注册/进度/AI；返回 (finished, ckpts, saved) 观测槽。"""
    finished: list[tuple] = []
    ckpts: list[dict] = []

    async def _register(*a, **k):
        return "task-x", None

    async def _finish(task_id, status="", message="", **k):
        finished.append((status, message))

    async def _save_ckpt(task_id, payload):
        ckpts.append(payload)

    async def _chat(messages, **k):
        return _CONTINUATION

    monkeypatch.setattr(sh, "register_task", _register)
    monkeypatch.setattr(sh, "_register_task_exclusive", lambda *a, **k: _register())
    monkeypatch.setattr(sh, "update_progress", _noop)
    monkeypatch.setattr(sh, "update_task_stats", _noop)
    monkeypatch.setattr(sh, "finish_task", _finish)
    monkeypatch.setattr(sh, "_save_content_checkpoint", _save_ckpt)
    monkeypatch.setattr(sh, "settle_global_conn", _noop)
    monkeypatch.setattr(sh, "wait_resume", lambda *a, **k: _noop())
    # 真实 is_stopped 对未注册 task_id 返回 True（fail-stop 防御），
    # 不打桩会让生成循环一进来就判「用户已停止」（前序轮次踩过）。
    monkeypatch.setattr(sh, "is_stopped", lambda *a, **k: False)
    monkeypatch.setattr(sh, "has_active_task", lambda *a, **k: False)
    monkeypatch.setattr(sh, "chat_with_fallback", _chat)
    monkeypatch.setattr(sh, "_write_review_record", _noop, raising=False)
    return {"finished": finished, "ckpts": ckpts}


async def _run(db, body=None):
    payload = {"mode": "continue", "concurrency": 1, "auto_consistency_repair": False}
    payload.update(body or {})
    resp = await sh.generate_content("sc1", _FakeRequest(payload), db=db)
    chunks: list[str] = []
    async for piece in resp.body_iterator:
        chunks.append(piece.decode("utf-8", "replace") if isinstance(piece, bytes) else str(piece))
    text = "".join(chunks)
    events = [json.loads(ln[6:]) for ln in text.splitlines() if ln.startswith("data: ")]
    return events, text


def _terminal(events) -> dict:
    for e in reversed(events):
        if e.get("event") in ("completed", "stopped", "error", "degraded"):
            return e
    raise AssertionError(f"未收到终态事件: {[e.get('event') for e in events]}")


# ============================================================================
# A 组 · 端到端：mode="continue" 必须跑通并真正续写
# ============================================================================


class TestContinueModeEndToEnd:
    @pytest.mark.asyncio
    async def test_continue_mode_completes_without_unbound_error(self, stub_env):
        """修复前：每章 section_error「生成异常: … '_plan_json'」+ 终态 failed。

        修复后：终态 completed、无任何 section_error、正文 = 旧文 + 续写。
        """
        db = _ContinueDB()
        events, text = await _run(db)
        errors = [e for e in events if e.get("event") == "section_error"]
        assert not errors, (
            "续写模式不得产生 section_error（UnboundLocalError 被 guarded_gen "
            f"兜底成『生成异常』即为本 BUG 复现）: {errors} / {text[:400]}"
        )
        term = _terminal(events)
        assert term["event"] == "completed", f"续写模式终态应为 completed: {term} / {text[:400]}"
        # 正文确实续写：落库内容同时包含旧文与续写新增段
        assert db.saved, "续写模式必须落库一次"
        assert _ContinueDB.OLD_CONTENT in db.saved[0], "续写必须保留旧正文"
        assert _CONTINUATION in db.saved[0], "续写新增内容必须进入落库正文"
        done_ev = [e for e in events if e.get("event") == "section_done"]
        assert done_ev, "续写成功必须下发 section_done"

    @pytest.mark.asyncio
    async def test_continue_mode_empty_leaf_yields_no_targets(self, stub_env):
        """空正文叶子被 continue 选章规则排除（与 missing 互为补集）——
        正常 completed 终态「没有待生成的章节」，不进 gen_one、不报错。"""
        db = _ContinueDB()
        db._sections[0]["content"] = ""
        events, text = await _run(db)
        errs = [e for e in events if e.get("event") == "section_error"]
        assert not errs, f"选章排除不应产生 section_error: {errs} / {text[:300]}"
        term = _terminal(events)
        assert term["event"] == "completed", f"空正文叶子应走「没有待生成的章节」正常终态: {term}"
        assert "没有待生成的章节" in (term.get("message") or ""), f"终态消息应为选章补集语义: {term}"


# ============================================================================
# B 组 · 静态锁：续写分支必须绑定 _plan_json
# ============================================================================


def _src() -> str:
    return inspect.getsource(sh.generate_content)


class TestContinueBranchPlanJsonBinding:
    def test_continue_branch_binds_plan_json(self):
        """续写分支五元组解包不得把第 5 位丢弃（丢弃即复现 UnboundLocalError）。"""
        src = _src()
        anchor = "_eff_std_c, _sec_facts"
        i = src.index(anchor)
        line_end = src.index("\n", i)
        line = src[i:line_end]
        assert "_plan_json" in line, (
            "续写分支的 _build_generation_context 解包必须绑定 _plan_json"
            "（continue 模式落库点无条件引用该名字，丢弃即 UnboundLocalError）"
        )

    def test_persist_call_passes_plan_json(self):
        """落库调用必须透传 plan_json（R70 CASE WHEN 保列语义的输入端）。"""
        src = _src()
        assert "plan_json=_plan_json" in src, "gen_one 落库调用必须透传 plan_json=_plan_json"
