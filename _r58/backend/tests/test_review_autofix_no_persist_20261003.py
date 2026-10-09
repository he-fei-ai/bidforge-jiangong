"""自动修复链路「重算不落总检历史」回归护栏（2026-10-03 数据链收口）。

缺陷形态（有代码证据）：``compliance._readiness_overview_compute`` 末尾
无条件 ``_persist_run`` 落 ``preflight_runs``（供 ReadinessDashboard 画分数
趋势线），而 ``routers/review_autofix.py`` 的 ``_resolve_finding``（plan/apply
共用）、``collect``、``stage`` 三处直接调用它重算 findings —— 用户每点一次
「定位 / 修复 / 收集 / 暂存」就往历史趋势灌一条总检记录（污染 G2 要保护的
趋势线），且不持 ``_overview_lock`` 存在并发写竞态。

修法：``_readiness_overview_compute`` 增加 ``persist: bool = True`` 关键字参数
（**默认 True，既有端点调用逐字不变** —— 向后兼容红线），autofix 三处传
``persist=False``。

覆盖：
1. 行为：persist=False 不落库 / 缺省照落库（兼容红线）；
2. 路由：collect / stage / _resolve_finding 的重算调用必须带 persist=False；
3. 接线 parity：/overview 端点本体不得被顺手改成 persist=False
   （主链路的落库是分数趋势的数据来源，摘掉就是新断链）；
4. A/B 反向锚点：若把路由三处改回无参调用（还原缺陷形态），
   TestRouterCallSites 三例与 TestWiringParity 首例必然定向失败。
"""
from __future__ import annotations

import pathlib
import re
import uuid

import app.db as _appdb
import pytest
from app.db import close_db, get_conn, init_db
from app.routers import review_autofix as ra
from app.routers.compliance import _readiness_overview_compute
from fastapi import HTTPException

pytestmark = pytest.mark.asyncio

_ROUTER_SRC = (pathlib.Path(__file__).resolve().parent.parent
               / "app" / "routers" / "review_autofix.py")
_COMPLIANCE_SRC = (pathlib.Path(__file__).resolve().parent.parent
                   / "app" / "routers" / "compliance.py")


@pytest.fixture
async def db_ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "autofix-nopersist.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    sid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status,word_budget) VALUES(?,?,?,?,0)",
        (sid, pid, "深基坑支护专项方案", "目录已确认"))
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
        " description, level, status, word_count, word_budget, content,"
        " review_status, sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, "", "", "工程概况", "", 1, "generated",
         300, 0, "基坑开挖深度 5.6m，周边环境详见图纸。", "", 0))
    await db.commit()
    yield db, sid
    await close_db()


async def _run_count(db) -> int:
    cur = await db.execute("SELECT COUNT(*) AS n FROM preflight_runs")
    return (await cur.fetchone())["n"]


# ===========================================================================
# 一、行为契约：persist 参数
# ===========================================================================
class TestPersistBehavior:
    async def test_default_still_persists(self, db_ctx):
        """缺省行为逐字不变（向后兼容红线）：/overview 主链路照常落库。"""
        db, sid = db_ctx
        before = await _run_count(db)
        await _readiness_overview_compute(db, sid)
        assert await _run_count(db) == before + 1

    async def test_persist_false_writes_no_run(self, db_ctx):
        """persist=False：结论照常返回、历史表零写入。"""
        db, sid = db_ctx
        before = await _run_count(db)
        payload = await _readiness_overview_compute(db, sid, persist=False)
        assert await _run_count(db) == before
        assert "findings" in payload and "total" in payload


# ===========================================================================
# 二、路由调用点：三处重算必须 persist=False
# ===========================================================================
class TestRouterCallSites:
    async def _record_calls(self, monkeypatch) -> list[dict]:
        calls: list[dict] = []

        async def fake_compute(db, scheme_id, **kw):
            calls.append(kw)
            return {"findings": [], "content_fingerprint": "fp", "stale": False}

        monkeypatch.setattr(
            "app.routers.compliance._readiness_overview_compute", fake_compute)
        return calls

    async def test_resolve_finding_uses_persist_false(self, db_ctx, monkeypatch):
        db, sid = db_ctx
        calls = await self._record_calls(monkeypatch)
        with pytest.raises(HTTPException):  # findings 为空 → 404，属预期路径
            await ra._resolve_finding(db, sid, "STD-05")
        assert calls and all(kw.get("persist") is False for kw in calls)

    async def test_collect_uses_persist_false(self, db_ctx, monkeypatch):
        db, sid = db_ctx
        calls = await self._record_calls(monkeypatch)
        res = await ra.collect(sid, {"scope": "all"}, db)
        assert res["total"] == 0
        assert calls and all(kw.get("persist") is False for kw in calls)

    async def test_stage_uses_persist_false(self, db_ctx, monkeypatch):
        db, sid = db_ctx
        calls = await self._record_calls(monkeypatch)
        res = await ra.stage(sid, {"rule_ids": ["STD-05"]}, db)
        assert res["status"] == "empty"
        assert calls and all(kw.get("persist") is False for kw in calls)


# ===========================================================================
# 三、接线 parity 静态锁（防「顺手全改」或「改回旧形态」）
# ===========================================================================
class TestWiringParity:
    async def test_autofix_router_has_no_bare_compute_call(self):
        """review_autofix.py 内所有重算调用必须显式 persist=False。"""
        src = _ROUTER_SRC.read_text(encoding="utf-8")
        bare = re.findall(
            r"_readiness_overview_compute\(db, scheme_id\)", src)
        assert not bare, "存在未带 persist=False 的重算调用（会污染分数趋势）"
        assert src.count(
            "_readiness_overview_compute(db, scheme_id, persist=False)") == 3

    async def test_overview_endpoint_keeps_persist(self):
        """compliance.py /overview 端点本体仍走缺省落库（主链路数据源）。

        锚定形态：端点里对 compute 的调用**不带** persist 参数（缺省 True）。
        若有人把它改成 persist=False，运行历史与分数趋势当场断链。
        """
        src = _COMPLIANCE_SRC.read_text(encoding="utf-8")
        m = re.search(
            r"async def readiness_overview\(.*?\n(?:.*\n)*?.*?_readiness_overview_compute\(([^)]*)\)",
            src)
        assert m and "persist=False" not in m.group(1)

    async def test_compute_signature_defaults_compatible(self):
        """persist 必须是**关键字参数且默认 True**（旧调用逐字不变）。"""
        import inspect

        sig = inspect.signature(_readiness_overview_compute)
        p = sig.parameters["persist"]
        assert p.kind is inspect.Parameter.KEYWORD_ONLY
        assert p.default is True
