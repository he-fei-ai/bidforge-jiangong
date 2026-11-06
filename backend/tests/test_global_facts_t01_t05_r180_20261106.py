"""R180（2026-11-06）：全局事实模块遗留项 T-01 ~ T-05 回归。

每条用例都带「变异验证」说明：把被防行为改坏后必须变红。
"""

import uuid
from datetime import datetime, timedelta

import app.db as _appdb
import app.routers.global_facts as gf
import app.services.facts_write_guard as fwg
import pytest
from app.db import get_conn, init_db
from app.models import FactGroupIn, FactItem, RetryFailedFactsRequest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "facts-r180.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid
    await db.close()


async def _insert_fact(
    db,
    fid: str,
    *,
    title: str = "基坑深度",
    value: str = "12.0m",
    resolved: int = 1,
    conflict: int = 0,
    sim: int = 0,
    stale: int = 0,
    sid: str = "",
    pid: str = "",
    category: str = "tech_param",
    page_ref=None,
    fact_key: str = "depth",
) -> str:
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, group_title,"
        " title, content, category, source_ref, is_simulated, confidence,"
        " is_resolved, has_conflict, conflict_keys, fact_key, is_stale, page_ref) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fid,
            pid,
            sid,
            "g1",
            "技术参数",
            title,
            gf._build_fact_content(title, value, bool(sim)),
            category,
            "",
            sim,
            0.9,
            resolved,
            conflict,
            "",
            fact_key,
            stale,
            page_ref,
        ),
    )
    await db.commit()
    return fid


# ---------------------------------------------------------------------------
# T-01：闸门 fail-closed（读异常 → 503，绝不放行）
# ---------------------------------------------------------------------------


class _BoomDB:
    async def execute(self, *args, **kwargs):
        raise RuntimeError("db unavailable")


async def test_t01_duplicate_gate_fail_closed_on_read_error():
    """读取同作用域事实失败时必须 503，不能把异常吞成「无重复」放行。

    变异验证：删除 ``except Exception`` 分支后本用例会抛 RuntimeError → 红。
    """
    row = {"scheme_id": "s1", "project_id": "p1", "title": "基坑深度"}
    with pytest.raises(HTTPException) as ei:
        await gf._assert_no_conflicting_injectable_duplicate(_BoomDB(), "f1", row, name="基坑深度", value="12.0m")
    assert ei.value.status_code == 503
    assert "稍后重试" in str(ei.value.detail)


async def test_t01_create_dedupe_is_case_insensitive(ctx):
    """新增去重与确认闸门共用同一归一（大小写不敏感）。

    变异验证：把 ``_norm_dup_value`` 还原为大小写敏感实现后，第二条
    ``c30`` 会被判为不同值 → 409/两行，本用例变红。
    """
    db, pid, sid = ctx
    first = await gf.create_fact(
        FactGroupIn(
            title="技术参数", content="", category="tech_param", items=[FactItem(name="混凝土强度", value="C30")]
        ),
        scheme_id=sid,
        project_id="",
        db=db,
    )
    second = await gf.create_fact(
        FactGroupIn(
            title="技术参数", content="", category="tech_param", items=[FactItem(name="混凝土强度", value="c30")]
        ),
        scheme_id=sid,
        project_id="",
        db=db,
    )
    assert second["deduplicated"] == 1
    cur = await db.execute("SELECT COUNT(*) FROM global_facts WHERE scheme_id=?", (sid,))
    assert (await cur.fetchone())[0] == 1
    assert first["id"] == second["id"]


# ---------------------------------------------------------------------------
# T-02：page_ref 统一 int | None
# ---------------------------------------------------------------------------


def test_t02_normalize_page_ref_helper():
    assert gf._normalize_page_ref(None) is None
    assert gf._normalize_page_ref("") is None
    assert gf._normalize_page_ref("   ") is None
    assert gf._normalize_page_ref("12") == 12
    assert gf._normalize_page_ref(12) == 12
    # 历史脏值（非页码文本）一律回落 None，不让前端拿到非法类型
    assert gf._normalize_page_ref("P12") is None


async def test_t02_list_facts_returns_none_for_legacy_empty(ctx):
    """旧库里的 ``''`` 必须在读取侧归一为 None（不是显示层兜底）。

    变异验证：读路径改回 ``r.get("page_ref") or ""`` 后本用例变红。
    """
    db, pid, sid = ctx
    await _insert_fact(db, "empty_ref", page_ref="", sid=sid, pid=pid)
    await _insert_fact(db, "num_ref", page_ref=12, sid=sid, pid=pid)

    out = await gf.list_facts(scheme_id=sid, project_id="", db=db)
    items = {it["fact_id"]: it for g in out["groups"] for it in g["items"]}
    assert items["empty_ref"]["page_ref"] is None
    assert items["num_ref"]["page_ref"] == 12


# ---------------------------------------------------------------------------
# T-03：章节覆盖度按可注入事实统计，全量口径加法保留
# ---------------------------------------------------------------------------


async def test_t03_chapter_stats_use_injectable_scope(ctx):
    """totals.facts / 章内 facts 只算可注入事实；facts_all 保留全量。

    变异验证：把 stats 换回 ``_chapter_stats_for_items(enriched)`` 后
    ``totals.facts`` 会变成 5（= facts_all），本用例变红。
    """
    db, pid, sid = ctx
    # 2 条可注入
    await _insert_fact(db, "ok1", title="基坑深度", value="12m", category="tech_param", sid=sid, pid=pid)
    await _insert_fact(db, "ok2", title="支护形式", value="桩锚", category="tech_param", sid=sid, pid=pid)
    # 3 条不可注入（冲突未决 / 未确认 / 过期）
    await _insert_fact(
        db, "bad_conflict", title="地下水位", value="2m", conflict=1, category="tech_param", sid=sid, pid=pid
    )
    await _insert_fact(
        db, "bad_unresolved", title="安全等级", value="一级", resolved=0, category="tech_param", sid=sid, pid=pid
    )
    await _insert_fact(
        db, "bad_stale", title="计划工期", value="90天", stale=1, category="tech_param", sid=sid, pid=pid
    )

    out = await gf.list_facts_by_chapters(scheme_id=sid, project_id="", db=db)

    assert out["totals"]["facts"] == 2, "主口径必须只统计可注入事实"
    assert out["totals"]["facts_all"] == 5, "全量口径必须加法式保留"
    assert out["totals"]["totals_all"]["facts"] == 5

    all_items = [it for ch in out["chapters"] for it in ch["items"]]
    assert len(all_items) == 5
    assert sum(1 for it in all_items if it["is_injectable"]) == 2

    for ch in out["chapters"]:
        if any(it["fact_id"] == "ok1" for it in ch["items"]):
            assert ch["facts"] == 2
            assert ch["facts_all"] == len(ch["items"])
            assert ch["count_all"] >= ch["facts"]
            assert "coverage_all" in ch and "covered_fields_all" in ch
            break
    else:  # pragma: no cover - 只为让断言失败时信息明确
        raise AssertionError("ok1 未落入任何章节")


# ---------------------------------------------------------------------------
# T-04：项目级写保护（进程内锁 + DB 租约）
# ---------------------------------------------------------------------------


async def test_t04_db_lease_blocks_second_writer(ctx):
    """模拟另一进程已持有租约：第二次获取必须抛可见冲突。

    变异验证：删除 `_acquire_lease` 的 token 校验后第二次会成功 → 红。
    """
    db, pid, _sid = ctx
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO facts_write_leases(project_id, token, holder, acquired_at) VALUES(?,?,?,?)",
        (pid, "other-token", "其它进程", now),
    )
    await db.commit()

    with pytest.raises(fwg.FactsWriteConflict) as ei:
        await fwg.acquire_project_facts_write(db, pid, holder="ai_extraction")
    assert ei.value.reason == "lease_held"
    assert "其它进程" in str(ei.value)


async def test_t04_stale_lease_can_be_stolen(ctx):
    """过期租约（> TTL）可被抢占，避免进程崩溃后永久锁死。"""
    db, pid, _sid = ctx
    stale = (datetime.now() - timedelta(seconds=fwg.LEASE_TTL_SECONDS + 60)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO facts_write_leases(project_id, token, holder, acquired_at) VALUES(?,?,?,?)",
        (pid, "stale-token", "已崩溃进程", stale),
    )
    await db.commit()

    token = await fwg.acquire_project_facts_write(db, pid, holder="manual")
    assert token
    await fwg.release_project_facts_write(db, pid, token)


async def test_t04_guard_releases_on_exception(ctx):
    """写入异常路径必须释放租约与进程内锁，否则后续所有写入被永久阻塞。"""
    db, pid, _sid = ctx
    with pytest.raises(RuntimeError):
        async with fwg.project_facts_write_guard(db, pid, holder="manual"):
            raise RuntimeError("boom")

    token = await fwg.acquire_project_facts_write(db, pid, holder="after_boom")
    assert token
    await fwg.release_project_facts_write(db, pid, token)


async def test_t04_inprocess_lock_blocks_same_project(ctx, monkeypatch):
    """同一进程内并行写入方在等待超时后必须得到冲突（而不是静默排队后覆盖）。"""
    db, pid, _sid = ctx
    monkeypatch.setattr(fwg, "LOCK_WAIT_SECONDS", 0.05)
    token1 = await fwg.acquire_project_facts_write(db, pid, holder="manual")
    try:
        with pytest.raises(fwg.FactsWriteConflict) as ei:
            await fwg.acquire_project_facts_write(db, pid, holder="ai_extraction")
        assert ei.value.reason == "lock_wait_timeout"
    finally:
        await fwg.release_project_facts_write(db, pid, token1)


# ---------------------------------------------------------------------------
# T-05：手动重试失败/退避段
# ---------------------------------------------------------------------------


async def test_t05_retry_failed_bypasses_backoff_and_keeps_history(ctx):
    """手动重试必须清空 next_retry_at（立即可重试），且保留失败历史。

    变异验证：UPDATE 不写 next_retry_at 时，退避仍生效 → 本用例变红。
    """
    db, pid, sid = ctx
    future = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO facts_chunk_failures(project_id, scheme_id, chunk_hash, fail_count,"
        " last_failed_at, next_retry_at) VALUES(?,?,?,?,?,?)",
        (pid, sid, "h-backoff", 3, "2026-11-06 06:00:00", future),
    )
    await db.execute(
        "INSERT INTO facts_chunk_failures(project_id, scheme_id, chunk_hash, fail_count,"
        " last_failed_at, next_retry_at) VALUES(?,?,?,?,?,?)",
        (pid, sid, "h-immediate", 1, "2026-11-06 06:00:00", ""),
    )
    await db.execute(
        "INSERT INTO facts_chunk_failures(project_id, scheme_id, chunk_hash, fail_count,"
        " last_failed_at, next_retry_at) VALUES(?,?,?,?,?,?)",
        (pid, sid + "-other", "h-other-scheme", 2, "2026-11-06 06:00:00", future),
    )
    await db.commit()

    out = await gf.retry_failed_segments(RetryFailedFactsRequest(scheme_id=sid), db=db)
    assert out["ok"] is True
    assert out["released"] == 2
    assert out["skipped_source"] == "manual_retry_bypass"
    assert out["diagnostics"]["bypassed_backoff"] == 1
    assert out["diagnostics"]["already_immediate"] == 1

    cur = await db.execute(
        "SELECT chunk_hash, next_retry_at, last_manual_retry_at FROM facts_chunk_failures"
        " WHERE project_id=? AND scheme_id=? ORDER BY chunk_hash",
        (pid, sid),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    assert all(r["next_retry_at"] == "" for r in rows)
    assert all(r["last_manual_retry_at"] for r in rows)
    # fail_count 必须保留（下次失败继续按既有退避续算）
    cur = await db.execute(
        "SELECT fail_count FROM facts_chunk_failures WHERE project_id=? AND scheme_id=? AND chunk_hash='h-backoff'",
        (pid, sid),
    )
    assert (await cur.fetchone())[0] == 3
    # 其它方案的失败段不受影响
    cur = await db.execute("SELECT next_retry_at FROM facts_chunk_failures WHERE scheme_id=?", (sid + "-other",))
    assert (await cur.fetchone())[0] == future


async def test_t05_retry_released_chunks_no_longer_deferred(ctx):
    """释放后退避查询不再把这些段当「本轮跳过」，下一轮提取会真正重跑。"""
    from app.services.facts_extractor import load_completed_chunks

    db, pid, sid = ctx
    future = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO facts_chunk_failures(project_id, scheme_id, chunk_hash, fail_count,"
        " last_failed_at, next_retry_at) VALUES(?,?,?,?,?,?)",
        (pid, sid, "h1", 2, "2026-11-06 06:00:00", future),
    )
    await db.commit()

    done = await load_completed_chunks(db, pid, sid)
    assert "h1" in done.deferred

    out = await gf.retry_failed_segments(RetryFailedFactsRequest(scheme_id=sid), db=db)
    assert out["released"] == 1

    done2 = await load_completed_chunks(db, pid, sid)
    assert "h1" not in done2.deferred
    assert "h1" not in done2


async def test_t05_retry_no_failed_segments_is_explicit(ctx):
    """没有失败段时返回显式 skipped_source，而不是假装重试成功。"""
    db, pid, sid = ctx
    out = await gf.retry_failed_segments(RetryFailedFactsRequest(scheme_id=sid), db=db)
    assert out["released"] == 0
    assert out["skipped_source"] == "manual_retry_no_failed_segments"


async def test_t05_retry_filter_by_chunk_hash(ctx):
    """指定 chunk_hashes 时只释放匹配段，未命中的继续退避。"""
    db, pid, sid = ctx
    future = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    for h in ("h1", "h2"):
        await db.execute(
            "INSERT INTO facts_chunk_failures(project_id, scheme_id, chunk_hash, fail_count,"
            " last_failed_at, next_retry_at) VALUES(?,?,?,?,?,?)",
            (pid, sid, h, 1, "2026-11-06 06:00:00", future),
        )
    await db.commit()

    out = await gf.retry_failed_segments(RetryFailedFactsRequest(scheme_id=sid, chunk_hashes=["h1"]), db=db)
    assert out["released"] == 1
    assert out["segments"][0]["chunk_hash"] == "h1"
    cur = await db.execute(
        "SELECT next_retry_at FROM facts_chunk_failures WHERE scheme_id=? AND chunk_hash='h2'", (sid,)
    )
    assert (await cur.fetchone())[0] == future


async def test_t05_retry_route_rejects_unknown_scheme(ctx):
    db, _pid, _sid = ctx
    with pytest.raises(HTTPException) as ei:
        await gf.retry_failed_segments(RetryFailedFactsRequest(scheme_id="missing"), db=db)
    assert ei.value.status_code == 404


# ---------------------------------------------------------------------------
# T-04（接线）：冲突必须转成**可见** 409，且后台提取走同一把锁
# ---------------------------------------------------------------------------


class _FakeRequest:
    """最小 Request 替身：只暴露写保护依赖实际读取的属性。"""

    def __init__(self, method: str, path: str, *, scheme_id: str = ""):
        self.method = method
        self.path_params: dict = {}
        self.query_params = {"scheme_id": scheme_id} if scheme_id else {}
        self.url = type("_Url", (), {"path": path})()

    async def json(self):
        return {}


async def test_t04_write_guard_dependency_turns_conflict_into_visible_409(ctx):
    """另一写入方持有 DB 租约时，写请求依赖必须抛 409；只读请求直接放行。

    变异验证：把依赖里的 409 分支改为放行（或从 router 摘掉该依赖）后本用例变红。
    """
    db, pid, sid = ctx
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO facts_write_leases(project_id, token, holder, acquired_at) VALUES(?,?,?,?)",
        (pid, "other-token", "ai_extraction", now),
    )
    await db.commit()

    gen = gf._facts_write_guard_dependency(
        _FakeRequest("POST", "/api/v1/global-facts/retry-failed", scheme_id=sid), db=db
    )
    with pytest.raises(HTTPException) as ei:
        await gen.__anext__()
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "facts_write_conflict"
    assert "正在被" in ei.value.detail["message"]

    # 只读请求不抢锁（GET 直接放行）
    gen_get = gf._facts_write_guard_dependency(_FakeRequest("GET", "/api/v1/global-facts"), db=db)
    await gen_get.__anext__()
    await gen_get.aclose()

    await db.execute("DELETE FROM facts_write_leases WHERE project_id=?", (pid,))
    await db.commit()


def test_t04_router_and_extraction_share_the_same_guard():
    """接线锚点：主路由挂了写保护依赖，后台提取落库也调用同一 acquire 出口。

    变异验证：从 APIRouter 移除 dependencies 或删掉 sse_handlers 里的 guard 调用
    后本用例变红。
    """
    import inspect

    from app.routers import sse_handlers

    assert any(
        getattr(d, "dependency", None) is gf._facts_write_guard_dependency for d in gf.router.dependencies
    ), "主路由必须挂 _facts_write_guard_dependency（否则手工写路径无项目级互斥）"
    src = inspect.getsource(sse_handlers)
    assert "acquire_project_facts_write(" in src, "后台提取落库必须复用同一把项目级写锁"
    assert 'holder="ai_extraction"' in src
