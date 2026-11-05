"""D-39（R176）：上下文截断/装配段失败的 SSE 可见信号回归。

锁三件事：
1. 只在**真实降级**时登记，未截断的正常路径零记录、零事件；
2. `context_degraded` payload 含 field / stage / original_chars / kept_chars
   / reason（枚举）/ segment_id；
3. 同键多次降级合并为一条事件，避免逐章刷屏。
"""

import json  # noqa: I001 — 下方 app.routers 导入顺序即运行时契约（sse_handlers 必须先行）
import uuid

import pytest

from app.routers import sse_handlers as sh
from app.routers import _sse_outline_build as build
from app.routers import _sse_outline_review as review


class _Cur:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _FakeDb:
    """极简 async db：按顺序返回预置结果，或首调即抛（模拟读失败）。"""

    def __init__(self, results=None, raise_on_execute=False):
        self._results = list(results or [])
        self._raise = raise_on_execute

    async def execute(self, *_a, **_k):
        if self._raise:
            raise RuntimeError("db read failed")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_record_budget_noop_when_not_truncated():
    out = []
    sh._record_context_degraded(out, field="f", stage="s", original_chars=100, kept_chars=100)
    assert out == []


def test_record_segment_failed_keeps_zero_lengths_and_segment_id():
    out = []
    sh._record_context_degraded(
        out,
        field="knowledge_rows",
        stage="outline.assemble.knowledge_text",
        original_chars=0,
        kept_chars=0,
        reason=sh.CONTEXT_DEGRADED_SEGMENT,
        segment_id="knowledge_rows",
    )
    assert out == [
        {
            "field": "knowledge_rows",
            "stage": "outline.assemble.knowledge_text",
            "reason": "segment_failed",
            "original_chars": 0,
            "kept_chars": 0,
            "dropped_chars": 0,
            "segment_id": "knowledge_rows",
        }
    ]


def test_context_events_empty_on_normal_path():
    assert sh._context_degraded_events("t1", None) == []
    assert sh._context_degraded_events("t1", []) == []


def test_context_event_payload_and_merge():
    records = [
        {
            "field": "raw_brief",
            "stage": "content.assemble.raw_brief",
            "reason": "budget_truncated",
            "original_chars": 5000,
            "kept_chars": 4000,
            "dropped_chars": 1000,
        },
        {
            "field": "raw_brief",
            "stage": "content.assemble.raw_brief",
            "reason": "budget_truncated",
            "original_chars": 5000,
            "kept_chars": 4000,
            "dropped_chars": 1000,
        },
        {
            "field": "parsed_texts",
            "stage": "outline.assemble.parsed_texts",
            "reason": "segment_failed",
            "original_chars": 0,
            "kept_chars": 0,
            "dropped_chars": 0,
            "segment_id": "parsed_texts",
        },
    ]
    events = sh._context_degraded_events("task-1", records)
    assert len(events) == 2
    raw = events[0]
    assert raw.startswith("data: ") and raw.endswith("\n\n")
    payload = json.loads(raw[len("data: ") :])
    assert payload["event"] == "context_degraded"
    assert payload["task_id"] == "task-1"
    assert payload["field"] == "raw_brief"
    assert payload["stage"] == "content.assemble.raw_brief"
    assert payload["reason"] in sh.CONTEXT_DEGRADED_REASONS
    assert payload["original_chars"] == 10000
    assert payload["kept_chars"] == 8000
    assert payload["dropped_chars"] == 2000
    assert payload["occurrences"] == 2


@pytest.mark.asyncio
async def test_structured_brief_within_budget_no_degrade():
    db = _FakeDb(
        [
            _Cur(
                rows=[
                    {
                        "item_id": "project_overview",
                        "label": "工程概况",
                        "output_type": "markdown",
                        "content": "基坑深度 6.5m。",
                    }
                ]
            )
        ]
    )
    degraded: list = []
    got = await review._build_structured_brief(
        db, "p1", "原始资料", max_chars=4000, degraded_out=degraded, degraded_stage="content.assemble.structured_brief"
    )
    assert "基坑深度" in got
    assert degraded == []


@pytest.mark.asyncio
async def test_structured_brief_truncation_records_budget():
    long_text = "工程概况：" + "基坑支护参数说明。" * 200
    db = _FakeDb(
        [
            _Cur(
                rows=[
                    {
                        "item_id": "project_overview",
                        "label": "工程概况",
                        "output_type": "markdown",
                        "content": long_text,
                    }
                ]
            )
        ]
    )
    degraded: list = []
    got = await review._build_structured_brief(
        db,
        "p1",
        "原始资料",
        max_chars=500,
        basis=None,
        degraded_out=degraded,
        degraded_stage="content.assemble.structured_brief",
    )
    assert len(got) <= 500 + 900 + 20  # 摘要预算 + 附录，不硬切正文
    rec = next(r for r in degraded if r["field"] == "structured_brief")
    assert rec["reason"] == "budget_truncated"
    assert rec["stage"] == "content.assemble.structured_brief"
    assert rec["original_chars"] > rec["kept_chars"]
    assert "segment_id" in rec


@pytest.mark.asyncio
async def test_structured_brief_appendix_truncation_records():
    db = _FakeDb(
        [
            _Cur(
                rows=[
                    {
                        "item_id": "project_overview",
                        "label": "工程概况",
                        "output_type": "markdown",
                        "content": "短摘要。",
                    }
                ]
            )
        ]
    )
    degraded: list = []
    await review._build_structured_brief(
        db,
        "p1",
        "原始资料" * 500,
        max_chars=4000,
        degraded_out=degraded,
        degraded_stage="outline.assemble.structured_brief",
    )
    rec = next(r for r in degraded if r["field"] == "raw_brief_appendix")
    assert rec["reason"] == "budget_truncated"
    assert rec["original_chars"] == 2000
    assert rec["kept_chars"] == 800


@pytest.mark.asyncio
async def test_load_knowledge_rows_failure_records_segment():
    db = _FakeDb(raise_on_execute=True)
    degraded: list = []
    rows = await build._load_knowledge_rows(
        db, "s1", degraded_out=degraded, degraded_stage="content.assemble.knowledge_rows"
    )
    assert rows == []
    assert degraded[0]["reason"] == "segment_failed"
    assert degraded[0]["stage"] == "content.assemble.knowledge_rows"
    assert degraded[0]["segment_id"] == "knowledge_rows"


@pytest.mark.asyncio
async def test_load_knowledge_rows_normal_no_event():
    db = _FakeDb(
        [_Cur(row={"project_id": "p1"}), _Cur(rows=[{"name": "模板工程", "usage_hint": "", "content": "规范要求"}])]
    )
    degraded: list = []
    rows = await build._load_knowledge_rows(db, "s1", degraded_out=degraded)
    assert len(rows) == 1
    assert degraded == []


# ============================================================
# 流级回归：正常路径零事件 / 真截断才出现 context_degraded
# ============================================================


async def _seed_scheme(db_conn):
    sid, pid = uuid.uuid4().hex, uuid.uuid4().hex
    await db_conn.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)", (pid, "测试项目"))
    await db_conn.execute(
        "INSERT OR IGNORE INTO schemes (id, project_id, name, config_json)" " VALUES (?,?,?,?)",
        (sid, pid, "深基坑支护方案", json.dumps({"requirements": "必须包含监测章节"})),
    )
    await db_conn.commit()
    return sid


@pytest.mark.asyncio
async def test_outline_normal_path_emits_no_context_degraded(db_conn, monkeypatch):
    sid = await _seed_scheme(db_conn)

    async def fake_collect(messages, validate_fn=None, **kwargs):
        outline = [{"title": "第一章 工程概况", "description": "d", "children": []}]
        if kwargs.get("scene") == "outline_draft":
            return ({"outline": outline}, json.dumps({"outline": outline}, ensure_ascii=False))
        return ({"passed": True, "suggestions": []}, json.dumps({"passed": True}, ensure_ascii=False))

    monkeypatch.setattr(sh, "render", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(sh, "collect_json_response", fake_collect)
    resp = await sh.generate_outline(sid, None, db_conn)
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
    events = [json.loads(line[len("data: ") :]) for line in "".join(chunks).splitlines() if line.startswith("data: ")]
    assert "completed" in [e.get("event") for e in events]
    assert [e for e in events if e.get("event") == "context_degraded"] == []


@pytest.mark.asyncio
async def test_outline_raw_brief_truncation_emits_event(db_conn, monkeypatch):
    sid = await _seed_scheme(db_conn)

    _long_doc = "基坑支护设计参数说明。" * 500  # >4000 字 → [:4000] 必切

    async def fake_load(_db, _project_id, limit=5, truncated_out=None):
        return [_long_doc]

    async def fake_collect(messages, validate_fn=None, **kwargs):
        outline = [{"title": "第一章 工程概况", "description": "d", "children": []}]
        if kwargs.get("scene") == "outline_draft":
            return ({"outline": outline}, json.dumps({"outline": outline}, ensure_ascii=False))
        return ({"passed": True, "suggestions": []}, json.dumps({"passed": True}, ensure_ascii=False))

    import app.routers.global_facts as gfacts

    monkeypatch.setattr(gfacts, "load_parsed_texts", fake_load)
    monkeypatch.setattr(sh, "render", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(sh, "collect_json_response", fake_collect)
    resp = await sh.generate_outline(sid, None, db_conn)
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
    events = [json.loads(line[len("data: ") :]) for line in "".join(chunks).splitlines() if line.startswith("data: ")]
    degraded = [e for e in events if e.get("event") == "context_degraded"]
    raw = next(e for e in degraded if e.get("field") == "raw_brief")
    assert raw["stage"] == "outline.assemble.raw_brief"
    assert raw["reason"] == "budget_truncated"
    assert raw["original_chars"] == len(_long_doc)
    assert raw["kept_chars"] == 4000
    assert raw["dropped_chars"] == len(_long_doc) - 4000
