"""全局事实提取 SSE 端点 · 端到端 422 回归护栏（2026-10-03）。

事故背景（logs/backend.log 实证 9 次 422，2026-10-01 ~ 2026-10-02）：
``@router.post("/generate-facts/{scheme_id}")`` 装饰器错位挂在内部辅助函数上，
其 ``db`` 形参无 ``Depends`` 默认值 → FastAPI 把 db 当**必需查询参数**
→ 前端「③ AI 提取事实」点击后恒 422，界面显示「SSE 请求失败: 422」。

``test_sse_route_decorator_20261001.py`` 的 AST 护栏锁的是「装饰器形态」；
本文件走**真实 ASGI HTTP 请求**（httpx.ASGITransport，与测试同事件循环，
规避 TestClient 跨事件循环持有 aiosqlite 连接的坑），直接锁定用户可见症状：

1. 真实方案 → 提取请求必须 200 + text/event-stream（**不得 422**）；
2. 不存在方案 → 404（业务错误，同样不得落进 422 校验形态）。

无资料文档的项目会在流内以 error 事件收尾（业务提示），不发起 AI 调用，
因此本测试毫秒级完成、不依赖任何 AI provider。
"""
import uuid

import app.db as _appdb
import httpx
import pytest
from app.db import close_db, get_conn, init_db
from app.main import app


@pytest.fixture
async def facts_sse_db(tmp_path, monkeypatch):
    """临时库 + 一套 (project, scheme)；结束恢复 DB_PATH 并清理连接池。"""
    original = _appdb.DB_PATH
    _appdb.DB_PATH = tmp_path / "facts-sse-422.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
        (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid
    await close_db()
    _appdb.DB_PATH = original


async def test_generate_facts_real_http_returns_event_stream(facts_sse_db):
    """真实方案：提取端点必须 200 事件流（历史事故症状 = 恒 422）。"""
    _db, _pid, sid = facts_sse_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
            timeout=30.0) as client:
        resp = await client.post(
            f"/api/v1/sse/generate-facts/{sid}",
            json={"missing_value_mode": "fabricate"})
    assert resp.status_code != 422, (
        f"提取事实端点返回 422（装饰器错位/签名回归，用户将看到"
        f"「SSE 请求失败: 422」）：{resp.text[:300]}")
    assert resp.status_code == 200, (
        f"提取事实端点应 200，实际 {resp.status_code}：{resp.text[:300]}")
    assert "text/event-stream" in resp.headers.get("content-type", "")
    # 无资料文档 → 流内 error 事件收尾（业务提示，不是 HTTP 失败）
    assert "event" in resp.text


async def test_generate_facts_missing_scheme_is_404_not_422(facts_sse_db):
    """不存在方案：业务 404，不得落进 422 校验形态。"""
    _db, _pid, _sid = facts_sse_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
            timeout=30.0) as client:
        resp = await client.post(
            "/api/v1/sse/generate-facts/not-exist-id", json={})
    assert resp.status_code == 404, (
        f"不存在方案应 404，实际 {resp.status_code}：{resp.text[:300]}")


async def test_generate_facts_rejects_no_such_route_as_404(facts_sse_db):
    """路由本身缺失（被摘除/重命名）时是 404，而非静默 422 ——
    防止未来把端点改签名时把 422 当成正常返回。"""
    _db, _pid, _sid = facts_sse_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
            timeout=30.0) as client:
        resp = await client.post("/api/v1/sse/no-such-route", json={})
    assert resp.status_code in (404, 405)
