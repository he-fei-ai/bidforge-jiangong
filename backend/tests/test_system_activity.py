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


@pytest.mark.asyncio
async def test_upload_limits_defaults():
    """默认（测试 settings 未显式覆盖时）返回三项正整数配额。"""
    from app.routers.system import upload_limits

    data = await upload_limits()
    assert set(data.keys()) == {
        "max_upload_bytes", "max_files_per_request", "max_total_bytes"}
    assert data["max_upload_bytes"] == 30 * 1024 * 1024
    assert data["max_files_per_request"] == 20
    assert data["max_total_bytes"] == 200 * 1024 * 1024
    for v in data.values():
        assert isinstance(v, int) and v > 0


@pytest.mark.asyncio
async def test_upload_limits_reflect_runtime_authority(monkeypatch):
    """✅ R73 续作 G5：端点读 ``gf.MAX_UPLOAD_*``（运行时权威），monkeypatch 对两侧同时生效。

    旧行为是端点自带 ``_positive_setting`` 直接读 ``settings.upload_*``，与上传路径
    各读一份 → ``monkeypatch(gf, "MAX_UPLOAD_BYTES", N)`` 时**前端拿旧值、后端按新值**
    （漂移）。收敛后端点读 ``gf.MAX_UPLOAD_*``，与上传路径同一对象，不再漂移。
    """
    from app.routers import global_facts as gf, system as sys_mod

    monkeypatch.setattr(gf, "MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
    monkeypatch.setattr(gf, "MAX_UPLOAD_FILES_PER_REQUEST", 7)
    monkeypatch.setattr(gf, "MAX_UPLOAD_TOTAL_BYTES", 99 * 1024 * 1024)

    data = await sys_mod.upload_limits()
    assert data["max_upload_bytes"] == 50 * 1024 * 1024
    assert data["max_files_per_request"] == 7
    assert data["max_total_bytes"] == 99 * 1024 * 1024


@pytest.mark.asyncio
async def test_upload_limits_does_not_read_settings_directly(monkeypatch):
    """✅ R73 续作 G5：端点不得再独立读 ``settings.upload_*``（防第三份副本回流）。

    monkeypatch ``settings`` 后端点**不应**反映该改动 —— 配额的运行时权威是
    ``gf.MAX_UPLOAD_*``，端点只是透传。回落逻辑由 ``global_facts._positive_int_setting``
    在模块加载时统一兜底，端点不再自带兜底读取器。
    """
    from app.routers import system as sys_mod

    monkeypatch.setattr(sys_mod.settings, "upload_max_bytes", 5 * 1024 * 1024)
    data = await sys_mod.upload_limits()
    # 仍反映 gf.MAX_UPLOAD_BYTES 的当前值（30MB 默认，未被 settings monkeypatch 影响）
    assert data["max_upload_bytes"] == 30 * 1024 * 1024, (
        "端点仍在独立读 settings.upload_max_bytes（G5 未收敛：应透传 gf.MAX_UPLOAD_BYTES）")
