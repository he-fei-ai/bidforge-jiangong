"""全局事实「解除过期标记」入口（G3 · 2026-10-04）回归护栏。

死锁背景：``_mark_project_facts_stale``（global_facts.py）在项目资料重新解析/
删除时把**整个项目**的事实一次性置 ``is_stale=1``；而解除入口此前只有
「改值」（PATCH，``value_changed`` 为真才清）与「裁决矛盾」两条 —— 都要求
**值必须先变**；``resolve`` 与 ``batch-resolve`` 对 stale 行还直接跳过/409。

后果：用户核对过、取值并未变化（资料重传是常见操作），却被永久排除在
``_FACTS_INJECT_WHERE``（含 ``is_stale=0``）之外 —— 正文与导出都拿不到这些
事实，红色「来源过期 N」Tag 也消不掉。

本文件锁死新增的 ``PATCH /{fact_id}/ack-stale`` 与 ``POST /ack-stale``：
既验「能解」，也验「不得借机绕过既有安全闸门」。
"""
import uuid

import httpx
import pytest

import app.db as _appdb
from app.db import close_db, get_conn, init_db
from app.main import app

MAIN_PID = "proj-main"
MAIN_SID = "scheme-main"
OTHER_PID = "proj-other"
OTHER_SID = "scheme-other"


async def _seed(db):
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (MAIN_PID, "主项目"))
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (OTHER_PID, "他项目"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
                     (MAIN_SID, MAIN_PID, "主方案"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
                     (OTHER_SID, OTHER_PID, "他方案"))
    await db.commit()


async def _fact(db, *, pid=MAIN_PID, sid=MAIN_SID, stale=1, resolved=1,
                simulated=0, conflict=0, title="基坑开挖深度"):
    fid = "f-" + uuid.uuid4().hex[:10]
    await db.execute(
        "INSERT INTO global_facts(id, project_id, scheme_id, group_id, title,"
        " content, is_resolved, is_stale, is_simulated, has_conflict)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (fid, pid, sid, "g1", title, "5.6m", int(resolved), int(stale),
         int(simulated), int(conflict)))
    await db.commit()
    return fid


async def _get(db, fid):
    cur = await db.execute(
        "SELECT is_stale, is_resolved, is_simulated, has_conflict"
        " FROM global_facts WHERE id=?", (fid,))
    row = await cur.fetchone()
    return dict(row) if row else None


# ===========================================================================
# 一、单条 ack-stale
# ===========================================================================
class TestAckSingle:
    async def test_clears_stale_and_marks_resolved(self, db_conn):
        from app.routers.global_facts import ack_fact_stale

        await _seed(db_conn)
        fid = await _fact(db_conn, stale=1, resolved=0)
        res = await ack_fact_stale(fid, MAIN_SID, db_conn)
        assert res["changed"] is True and res["is_resolved"] is True
        row = await _get(db_conn, fid)
        assert row["is_stale"] == 0, "过期标记未被解除"
        assert row["is_resolved"] == 1

    async def test_not_stale_is_idempotent_no_error(self, db_conn):
        """幂等：未过期的行不报错（前端可安全重复点击）。"""
        from app.routers.global_facts import ack_fact_stale

        await _seed(db_conn)
        fid = await _fact(db_conn, stale=0, resolved=1)
        res = await ack_fact_stale(fid, MAIN_SID, db_conn)
        assert res["ok"] is True and res["changed"] is False
        assert (await _get(db_conn, fid))["is_resolved"] == 1

    async def test_simulated_keeps_gate(self, db_conn):
        """不变量 is_simulated=1 ⟹ is_resolved=0：不得借 ack 绕过模拟值闸门。"""
        from app.routers.global_facts import ack_fact_stale

        await _seed(db_conn)
        fid = await _fact(db_conn, stale=1, resolved=0, simulated=1)
        res = await ack_fact_stale(fid, MAIN_SID, db_conn)
        assert res["changed"] is True
        assert res["blocked_reason"], "模拟值必须回传仍需逐条处理的原因"
        row = await _get(db_conn, fid)
        assert row["is_stale"] == 0, "过期标记仍应解除（两件事互不阻塞）"
        assert row["is_resolved"] == 0, "模拟值不得被顺带确认为已审核"

    async def test_conflict_keeps_gate(self, db_conn):
        from app.routers.global_facts import ack_fact_stale

        await _seed(db_conn)
        fid = await _fact(db_conn, stale=1, resolved=0, conflict=1)
        res = await ack_fact_stale(fid, MAIN_SID, db_conn)
        assert res["blocked_reason"]
        row = await _get(db_conn, fid)
        assert row["is_stale"] == 0 and row["is_resolved"] == 0

    async def test_missing_fact_is_404(self, db_conn):
        from fastapi import HTTPException

        from app.routers.global_facts import ack_fact_stale

        await _seed(db_conn)
        with pytest.raises(HTTPException) as ei:
            await ack_fact_stale("not-exist", MAIN_SID, db_conn)
        assert ei.value.status_code == 404


# ===========================================================================
# 二、批量 ack-stale：作用域与安全
# ===========================================================================
class TestAckBatch:
    async def test_clears_scope_and_touches_project_shared(self, db_conn):
        """本方案私有 + 同项目共享（scheme_id=''）都要解；其它项目的不动。"""
        from app.routers.global_facts import batch_ack_stale

        await _seed(db_conn)
        own = await _fact(db_conn, stale=1, resolved=0)
        shared = await _fact(db_conn, sid="", stale=1, resolved=0)
        foreign = await _fact(db_conn, pid=OTHER_PID, sid=OTHER_SID, stale=1, resolved=0)

        res = await batch_ack_stale({"scheme_id": MAIN_SID}, db_conn)
        assert res["changed"] == 2, f"应只处理本方案可见范围内 2 条，实际 {res['changed']}"
        assert res["gated"] == 0
        assert (await _get(db_conn, own))["is_stale"] == 0
        assert (await _get(db_conn, shared))["is_stale"] == 0
        assert (await _get(db_conn, foreign))["is_stale"] == 1, "他项目事实不得被越域修改"

    async def test_gated_rows_reported(self, db_conn):
        """模拟值/矛盾值也清 stale，但必须计入 gated 供前端提示。"""
        from app.routers.global_facts import batch_ack_stale

        await _seed(db_conn)
        await _fact(db_conn, stale=1, resolved=0, simulated=1)
        await _fact(db_conn, stale=1, resolved=0, conflict=1)
        await _fact(db_conn, stale=1, resolved=0)

        res = await batch_ack_stale({"scheme_id": MAIN_SID}, db_conn)
        assert res["changed"] == 3
        assert res["gated"] == 2, f"应回传 2 条仍被闸门挡住，实际 {res['gated']}"

        cur = await db_conn.execute(
            "SELECT is_stale, is_resolved, is_simulated, has_conflict"
            " FROM global_facts WHERE project_id=?", (MAIN_PID,))
        rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            assert r["is_stale"] == 0
        gated_rows = [r for r in rows if r["is_simulated"] or r["has_conflict"]]
        assert all(r["is_resolved"] == 0 for r in gated_rows), "闸门行不得被顺带确认"

    async def test_fact_ids_narrows_scope(self, db_conn):
        from app.routers.global_facts import batch_ack_stale

        await _seed(db_conn)
        a = await _fact(db_conn, stale=1, resolved=0)
        b = await _fact(db_conn, stale=1, resolved=0)
        res = await batch_ack_stale({"scheme_id": MAIN_SID, "fact_ids": [a]}, db_conn)
        assert res["changed"] == 1
        assert (await _get(db_conn, a))["is_stale"] == 0
        assert (await _get(db_conn, b))["is_stale"] == 1

    async def test_requires_scheme_scope(self, db_conn):
        from fastapi import HTTPException

        from app.routers.global_facts import batch_ack_stale

        await _seed(db_conn)
        with pytest.raises(HTTPException) as ei:
            await batch_ack_stale({}, db_conn)
        assert ei.value.status_code == 400, "缺 scheme_id 必须拒绝（越域写防护）"

    async def test_unknown_scheme_is_404(self, db_conn):
        from fastapi import HTTPException

        from app.routers.global_facts import batch_ack_stale

        with pytest.raises(HTTPException) as ei:
            await batch_ack_stale({"scheme_id": "nope"}, db_conn)
        assert ei.value.status_code == 404


# ===========================================================================
# 三、HTTP 层：路由确实挂上了（"函数对但没接线"是最常见的假修复）
# ===========================================================================
class TestAckStaleHttpWiring:
    @pytest.fixture
    async def wired(self, tmp_path, monkeypatch):
        original = _appdb.DB_PATH
        _appdb.DB_PATH = tmp_path / "ack-stale-20261004.sqlite"
        await init_db()
        db = await get_conn()
        await _seed(db)
        fid = await _fact(db, stale=1, resolved=0)
        yield fid
        await close_db()
        _appdb.DB_PATH = original

    async def test_single_endpoint_over_http(self, wired):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver",
                timeout=30.0) as client:
            resp = await client.patch(
                f"/api/v1/global-facts/{wired}/ack-stale",
                params={"scheme_id": MAIN_SID})
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:200]}"
        assert resp.json().get("changed") is True

    async def test_batch_endpoint_over_http(self, wired):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver",
                timeout=30.0) as client:
            resp = await client.post("/api/v1/global-facts/ack-stale",
                                     json={"scheme_id": MAIN_SID})
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:200]}"
        assert resp.json().get("changed") == 1

    async def test_batch_without_scope_is_400_over_http(self, wired):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver",
                timeout=30.0) as client:
            resp = await client.post("/api/v1/global-facts/ack-stale", json={})
        assert resp.status_code == 400, f"缺作用域应 400，实际 {resp.status_code}"
