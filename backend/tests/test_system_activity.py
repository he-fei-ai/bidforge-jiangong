"""GET /api/v1/system/activity 聚合端点 + SSE 实时流回归

「后台任务运行状态栏」的数据源契约：
- 空态不抛异常（常驻 UI）；
- DB 任务行与 task_registry 内存实时态合并（live / progress / elapsed / stats）；
- scheme_name 通过 LEFT JOIN 带出；
- 今日 AI 审计汇总可统计；
- SSE 流连接建立即推快照，任务变更触发刷新。
"""
import time

import pytest

from app.routers.system import activity


async def _patch_read_conn(db_conn, monkeypatch):
    """把只读连接池替换为测试内存库连接（activity 只读，不归还到池）。"""
    from app.routers import system as sys_mod

    async def fake_read_conn():
        return db_conn

    async def fake_release(_conn):
        return None

    monkeypatch.setattr(sys_mod, "get_read_conn", fake_read_conn)
    monkeypatch.setattr(sys_mod, "release_read_conn", fake_release)


@pytest.mark.asyncio
async def test_activity_empty_state(db_conn, monkeypatch):
    """空库时返回合法空态（状态栏常驻轮询，绝不能 500）。"""
    await _patch_read_conn(db_conn, monkeypatch)

    data = await activity(limit=5)
    assert data["server"]["version"]
    assert isinstance(data["server"]["uptime"], int)
    assert data["tasks"]["running"] == []
    assert data["tasks"]["recent"] == []
    assert data["ai"]["in_flight"] == 0
    assert data["ai"]["calls_today"] == 0


@pytest.mark.asyncio
async def test_activity_merges_live_task_and_ai_stats(db_conn, monkeypatch):
    """DB 任务行 + 内存实时态合并；scheme 名带出；今日 AI 汇总正确。"""
    await _patch_read_conn(db_conn, monkeypatch)

    await db_conn.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES ('s1', 'p1', '测试方案')")
    await db_conn.execute(
        "INSERT INTO task_registry (id, task_type, project_id, scheme_id, status, progress)"
        " VALUES ('t1', 'content_generation', 'p1', 's1', 'running', 0.1)")
    await db_conn.execute(
        "INSERT INTO ai_audit_logs (id, provider_name, model, action, duration, success)"
        " VALUES ('a1', 'deepseek', 'deepseek-chat', 'chat', 2.5, 1)")
    await db_conn.commit()

    # 模拟运行中任务的内存实时态（DB 里的 progress=0.1 应被内存 0.42 覆盖）
    import app.services.ai.task_registry as tr
    tr._tasks["t1"] = {
        "type": "content_generation",
        "status": "running",
        "progress": 0.42,
        "message": "正在生成 1.2 施工工艺",
        "scheme_id": "s1",
        "started_at": time.monotonic() - 30,
        "stats": {"done": 2, "total": 5, "words": 1234},
        "child_tasks": set(),
    }

    data = await activity(limit=8)
    running = data["tasks"]["running"]
    assert len(running) == 1, running
    t = running[0]
    assert t["id"] == "t1"
    assert t["scheme_name"] == "测试方案"
    assert t["progress"] == 0.42          # 内存实时态优先于 DB
    assert t["live"] is True
    assert t["elapsed"] >= 29             # 已耗时按内存 started_at 计算
    assert t["stats"]["done"] == 2

    ai = data["ai"]
    assert ai["calls_today"] == 1
    assert ai["ok_calls"] == 1
    assert ai["success_rate"] == 100.0
    assert ai["avg_duration"] == 2.5


class _FakeRequest:
    """模拟 FastAPI Request，用于测试 SSE 端点的 disconnect 检测。"""

    def __init__(self):
        self._disconnected = False

    async def is_disconnected(self):
        return self._disconnected

    def disconnect(self):
        self._disconnected = True


@pytest.mark.asyncio
async def test_activity_stream_initial_snapshot_and_task_update(db_conn, monkeypatch):
    """SSE 流建立时推送当前快照；任务状态变化触发二次刷新。"""
    await _patch_read_conn(db_conn, monkeypatch)

    await db_conn.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES ('s1', 'p1', '测试方案')")
    await db_conn.execute(
        "INSERT INTO task_registry (id, task_type, project_id, scheme_id, status, progress)"
        " VALUES ('t1', 'content_generation', 'p1', 's1', 'running', 0.1)")
    await db_conn.commit()

    import app.services.ai.task_registry as tr
    tr._tasks["t1"] = {
        "type": "content_generation",
        "status": "running",
        "progress": 0.42,
        "message": "正在生成",
        "scheme_id": "s1",
        "started_at": time.monotonic() - 30,
        "stats": {},
        "child_tasks": set(),
    }

    try:
        from app.routers.system import activity_stream

        req = _FakeRequest()
        resp = await activity_stream(req, limit=5)

        # 读取连接建立时发送的初始快照
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk)
            break

        assert len(chunks) == 1
        import json
        first = json.loads(chunks[0].split("data: ", 1)[1])
        assert first["event"] == "snapshot"
        assert len(first["data"]["tasks"]["running"]) == 1
        assert first["data"]["tasks"]["running"][0]["progress"] == 0.42

        # 触发任务更新广播，应产生第二个快照
        tr._tasks["t1"]["progress"] = 0.88
        from app.services import activity_broadcaster as ab
        ab.notify()

        async for chunk in resp.body_iterator:
            chunks.append(chunk)
            req.disconnect()

        assert len(chunks) >= 2
        second = json.loads(chunks[1].split("data: ", 1)[1])
        assert second["event"] == "snapshot"
        assert second["data"]["tasks"]["running"][0]["progress"] == 0.88
    finally:
        tr._tasks.pop("t1", None)
