"""正文生成 SSE 对外契约金样（B1 拆分前置护栏 · 2026-11-03）

本文件只锁 `generate_content` 的**对外可观测行为**，不锁内部函数名、嵌套结构
或源码文本 —— 目的正是为了让后续把 3,500 行的 `event_stream` 拆成多个模块时，
契约保持逐字节可比对。

锁定内容：
  1. HTTP 包装：`text/event-stream` + 防缓冲响应头。
  2. 帧协议：每个业务帧是可解析 JSON（兼容 `data: ` 前缀与历史裸 JSON 帧），
     心跳帧是 `: heartbeat` 注释。
  3. 事件生命周期：`section_start` → `section_done`（每章一次）→ `completed`；
     成功链路不得出现 `section_error`，终态必须是最后一帧。
  4. 终态载荷字段集与计数口径（done/total/failed_count/word_count/...）。
  5. 章节落库：两章正文均写入 `sections.content`。

⚠️ 本文件属于 `source_lock` 的反面：断言全部来自真实流式调用（桩 DB + 桩 AI），
拆分后若契约漂移，这里会给出可读的事件序列 diff。
"""

import json

import pytest
from app.routers import sse_handlers as sh


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _MultiSectionDB:
    """最小桩 DB：2 个叶子章节 + 可控的 execute 返回值。"""

    def __init__(self, sections=None):
        self.saved: list[str] = []
        self._sections = sections if sections is not None else [
            self._sec("s1", "1.1", 1, "", 0, 20),
            self._sec("s2", "1.2", 1, "", 1, 20),
        ]

    @staticmethod
    def _sec(sid, num, level, parent, sort, budget):
        return {
            "id": sid, "scheme_id": "sc1", "title": f"章节 {sid}",
            "level": level, "parent_id": parent, "sort_order": sort,
            "word_budget": budget, "content": "", "status": "empty",
            "description": "d", "outline_json": json.dumps({"id": num},
                                                          ensure_ascii=False),
            "generation_standard": "", "review_status": "", "word_count": 0,
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
            return self._Cur([dict(x) for x in self._sections])
        if s.strip().upper().startswith("UPDATE SECTIONS SET CONTENT"):
            self.saved.append(params[0])
            return self._Cur([])
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


_LONG_AI_TEXT = "## 编制说明\n\n" + ("本章正文内容用于统计超字数。" * 60)


@pytest.fixture()
def stub_env(monkeypatch):
    """打桩任务注册/进度/AI；返回 finished 与 checkpoint 记录。"""
    finished: list[tuple] = []
    ckpts: list[dict] = []
    box: dict = {"finished": finished, "ckpts": ckpts}

    async def _register(*a, **k):
        return "task-x", None

    async def _finish(task_id, status="", message="", **k):
        finished.append((status, message))

    async def _save_ckpt(task_id, payload):
        ckpts.append(payload)

    async def _chat(messages, **k):
        return _LONG_AI_TEXT

    monkeypatch.setattr(sh, "register_task", _register)
    monkeypatch.setattr(sh, "_register_task_exclusive",
                        lambda *a, **k: _register())
    monkeypatch.setattr(sh, "update_progress", _noop)
    monkeypatch.setattr(sh, "update_task_stats", _noop)
    monkeypatch.setattr(sh, "finish_task", _finish)
    monkeypatch.setattr(sh, "_save_content_checkpoint", _save_ckpt)
    monkeypatch.setattr(sh, "settle_global_conn", _noop)
    monkeypatch.setattr(sh, "wait_resume", lambda *a, **k: _noop())
    monkeypatch.setattr(sh, "is_stopped", lambda *a, **k: False)
    monkeypatch.setattr(sh, "has_active_task", lambda *a, **k: False)
    monkeypatch.setattr(sh, "chat_with_fallback", _chat)
    monkeypatch.setattr(sh, "_write_review_record", _noop, raising=False)
    return box


async def _run(db, body=None):
    """执行一次正文生成，返回 (frames, events)。"""
    payload = {"mode": "all", "concurrency": 1, "auto_consistency_repair": False}
    payload.update(body or {})
    resp = await sh.generate_content("sc1", _FakeRequest(payload), db=db)
    chunks: list[str] = []
    async for piece in resp.body_iterator:
        chunks.append(piece.decode("utf-8", "replace")
                      if isinstance(piece, bytes) else str(piece))
    frames = [f for f in "".join(chunks).split("\n\n") if f.strip()]
    events = []
    for frame in frames:
        line = frame.strip()
        if line.startswith(":"):
            continue  # 心跳注释帧
        if line.startswith("data: "):
            line = line[6:]
        events.append(json.loads(line))
    return frames, events


def _terminal(events) -> dict:
    for e in reversed(events):
        if e.get("event") in ("completed", "degraded", "stopped", "error"):
            return e
    raise AssertionError(f"未收到终态事件: {[e.get('event') for e in events]}")


def _names(events) -> list:
    return [e.get("event") for e in events]


# ============================================================================
# 1 · HTTP 包装
# ============================================================================

@pytest.mark.asyncio
async def test_response_is_sse_with_anti_buffer_headers(stub_env):
    db = _MultiSectionDB()
    resp = await sh.generate_content("sc1", _FakeRequest({
        "mode": "all", "concurrency": 1, "auto_consistency_repair": False}), db=db)
    assert resp.media_type == "text/event-stream"
    assert resp.headers["Cache-Control"] == "no-cache"
    assert resp.headers["X-Accel-Buffering"] == "no"


# ============================================================================
# 2 · 帧协议
# ============================================================================

@pytest.mark.asyncio
async def test_every_frame_is_json_or_heartbeat_comment(stub_env):
    db = _MultiSectionDB()
    frames, events = await _run(db)
    assert frames, "必须产生至少一个 SSE 帧"
    assert len(events) >= 5, f"两章成功链路事件过少: {_names(events)}"
    for frame in frames:
        line = frame.strip()
        # 允许历史形态：裸 JSON（section_start/section_done）或 data: 前缀
        body = line[6:] if line.startswith("data: ") else line
        if body.startswith(":"):
            continue
        json.loads(body)  # 不可解析即失败


# ============================================================================
# 3 · 成功链路生命周期
# ============================================================================

@pytest.mark.asyncio
async def test_success_lifecycle_is_start_done_completed(stub_env):
    db = _MultiSectionDB()
    frames, events = await _run(db)
    names = _names(events)
    term = _terminal(events)

    assert term["event"] in ("completed", "degraded"), names
    assert events[-1] is term, f"终态必须是最后一帧: {names}"
    assert "section_error" not in names, f"AI 正常时不得有章节失败: {names}"

    starts = [e for e in events if e.get("event") == "section_start"]
    dones = [e for e in events if e.get("event") == "section_done"]
    assert [e["section_id"] for e in starts] == ["s1", "s2"], starts
    assert [e["section_id"] for e in dones] == ["s1", "s2"], dones

    # 章节事件必须全部先于终态（拆分时最容易断的顺序契约）
    for e in events:
        if e.get("event") in ("section_start", "section_done",
                              "section_error", "progress"):
            assert events.index(e) < events.index(term), (
                f"{e.get('event')} 出现在终态之后: {names}")


@pytest.mark.asyncio
async def test_section_start_and_done_payload_contract(stub_env):
    db = _MultiSectionDB()
    _, events = await _run(db)
    starts = [e for e in events if e.get("event") == "section_start"]
    dones = [e for e in events if e.get("event") == "section_done"]

    for e in starts:
        for key in ("section_id", "title", "index", "total",
                    "stage", "stage_label", "progress"):
            assert key in e, f"section_start 缺 {key}: {e}"
    for e in dones:
        for key in ("section_id", "title", "word_count", "word_budget",
                    "word_status", "continue_failed", "word_deviation",
                    "word_ratio", "word_within_band", "content"):
            assert key in e, f"section_done 缺 {key}: {e}"
        assert isinstance(e["content"], str) and e["content"], e


# ============================================================================
# 4 · completed 终态载荷
# ============================================================================

@pytest.mark.asyncio
async def test_completed_payload_keys_and_counts(stub_env):
    db = _MultiSectionDB()
    _, events = await _run(db)
    term = _terminal(events)
    assert term["event"] in ("completed", "degraded")

    required = {
        "event", "task_id", "progress", "done", "total", "word_count",
        "failed_count", "run_words", "over_count", "message",
        "standard_summary", "fact_autofix_summary", "word_audit",
        "order_check", "fact_mapping_stats",
    }
    missing = required - set(term)
    assert not missing, f"completed 载荷缺字段: {missing}"

    assert term["total"] == 2
    assert term["done"] == 2
    assert term["failed_count"] == 0
    assert term["progress"] == 1.0
    assert term["word_count"] > 0
    assert term["run_words"] > 0
    assert "failed_sections" not in term, "无失败章节时不得下发失败明细"
    assert term["standard_summary"]["precise"] + term["standard_summary"]["fuzzy"] >= 0

    # checkpoint 与在线终态同口径（断线重挂依赖）
    ck = stub_env["ckpts"][-1]
    assert ck["done"] == term["done"]
    assert ck["total"] == term["total"]
    assert ck["run_words"] == term["run_words"]


# ============================================================================
# 5 · 落库契约
# ============================================================================

@pytest.mark.asyncio
async def test_both_sections_persisted(stub_env):
    db = _MultiSectionDB()
    _, events = await _run(db)
    assert len(db.saved) == 2, (
        f"两章都应落库: {len(db.saved)} / {_names(events)}")
    for content in db.saved:
        assert content.strip(), "落库正文不得为空"
        assert "编制说明" in content, f"落库正文异常: {content[:80]}"


@pytest.mark.asyncio
async def test_finished_status_matches_terminal_event(stub_env):
    db = _MultiSectionDB()
    _, events = await _run(db)
    term = _terminal(events)
    assert stub_env["finished"][-1][0] == term["event"], (
        f"任务终态与事件不一致: {stub_env['finished']} vs {term['event']}")
