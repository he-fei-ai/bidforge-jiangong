"""R125（2026-10-21）事实库版本化 专项回归测试。

覆盖（与三项大改造方案 R122c · R125 节对齐）：
- 迁移补列：空库 init 后 global_facts 含 version/version_label，快照/历史表存在。
- 默认关闭零行为变化：versioning=False → 无快照行、version 恒 1。
- 开启建快照 + 递增版本：二次抽取 version 1→2，快照数 2。
- 回滚恢复前一版本：抽取 v2 后回滚到 v1 快照 → global_facts 等于 v1、history 留痕。
- 回滚原子性：还原中途注入异常 → 事务回滚，global_facts 未被破坏。
- 端点默认关闭：enable_fact_versioning=False 时 rollback/snapshots 返回 409。
"""
import uuid

import app.routers.global_facts as gf
import pytest
import pytest_asyncio
from app.config import settings
from app.services.facts_extractor import (
    ExtractionResult,
    FactGroup,
    FactItem,
    list_fact_snapshots,
    persist_extraction,
    rollback_facts_to_snapshot,
)


def _make_result(name="项目经理", value="张伟", key="project_manager",
                 category="personnel") -> ExtractionResult:
    item = FactItem(name=name, value=value, key=key, category=category)
    grp = FactGroup(title="人员角色", category=category, items=[item])
    return ExtractionResult(groups=[grp], total_items=1)


async def _find_snapshot(db, needle: str):
    """找到捕获了含 needle 内容的快照（即「重抽前一状态」快照）。"""
    cur = await db.execute(
        "SELECT snapshot_id, fact_rows_json FROM global_facts_snapshots")
    for r in await cur.fetchall():
        if needle in (r["fact_rows_json"] or ""):
            return r["snapshot_id"]
    return None


@pytest_asyncio.fixture
async def ctx(db_conn, tmp_path, monkeypatch):
    """建一个项目并返回 db 连接（R125 直接测 persist_extraction，无需重定向文档存储）。"""
    uploads = tmp_path / "uploads"
    docs_root = tmp_path / "projects"
    monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)
    pid = uuid.uuid4().hex
    await db_conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db_conn.commit()
    return db_conn, pid, docs_root, uploads


async def test_migration_adds_version_columns(ctx):
    """空库 init 后 global_facts 含 version/version_label，快照/历史表存在。"""
    db, _pid, _dr, _up = ctx
    cur = await db.execute("PRAGMA table_info(global_facts)")
    cols = {r[1] for r in await cur.fetchall()}
    assert "version" in cols, "迁移须补 version 列"
    assert "version_label" in cols, "迁移须补 version_label 列"
    for t in ("global_facts_snapshots", "global_facts_history"):
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,))
        assert await cur.fetchone(), f"迁移须创建表 {t}"


async def test_versioning_off_is_noop(ctx):
    """默认关闭：versioning=False → 无快照行、version 恒 1（零行为变化）。"""
    db, pid, _dr, _up = ctx
    await persist_extraction(_make_result(), db, pid, "", versioning=False)
    cur = await db.execute(
        "SELECT COUNT(*) n FROM global_facts_snapshots")
    assert (await cur.fetchone())["n"] == 0, "关闭时不应建快照"
    cur = await db.execute("SELECT version FROM global_facts")
    for r in await cur.fetchall():
        assert r["version"] == 1, "关闭时 version 恒 1"


async def test_extraction_creates_snapshot_and_increments_version(ctx):
    """开启：首次 version=1 有快照；二次抽取 version=2、新快照。"""
    db, pid, _dr, _up = ctx
    await persist_extraction(_make_result(value="张伟"), db, pid, "", versioning=True)
    await persist_extraction(_make_result(value="李强"), db, pid, "", versioning=True)

    cur = await db.execute("SELECT COUNT(*) n FROM global_facts_snapshots")
    assert (await cur.fetchone())["n"] == 2, "每次重抽应建一个快照"
    # 快照自身记录的版本号应 1→2（每次重抽前拍下「前一状态」）
    cur = await db.execute("SELECT version FROM global_facts_snapshots ORDER BY version")
    snap_versions = [r["version"] for r in await cur.fetchall()]
    assert snap_versions == [1, 2], "快照版本应递增 1→2"

    # 当前事实均为最新版本 v2（李强）；v1 行已被增量合并删除
    cur = await db.execute("SELECT DISTINCT version FROM global_facts")
    assert [r["version"] for r in await cur.fetchall()] == [2]
    cur = await db.execute("SELECT content FROM global_facts")
    rows = await cur.fetchall()
    assert any("李强" in (r["content"] or "") for r in rows)


async def test_rollback_restores_previous_version(ctx):
    """抽取 v2 后回滚到 v1 快照 → global_facts 等于 v1、history 留痕。"""
    db, pid, _dr, _up = ctx
    await persist_extraction(_make_result(value="张伟"), db, pid, "", versioning=True)
    await persist_extraction(_make_result(value="李强"), db, pid, "", versioning=True)

    # 回滚目标 = 捕获了「v1 内容（张伟）」的快照（即第二次抽取前拍下的那一版）
    sid = await _find_snapshot(db, "张伟")
    assert sid is not None
    res = await rollback_facts_to_snapshot(db, sid, pid)
    assert res["ok"] is True

    # 回滚后当前事实应等于 v1（张伟），不再有李强
    cur = await db.execute("SELECT content FROM global_facts")
    contents = [r["content"] or "" for r in await cur.fetchall()]
    assert any("张伟" in c for c in contents)
    assert not any("李强" in c for c in contents)
    # history 留痕
    cur = await db.execute("SELECT COUNT(*) n FROM global_facts_history")
    assert (await cur.fetchone())["n"] == 1, "回滚须归档当前版本到 history"


async def test_rollback_is_atomic_on_failure(ctx):
    """回滚中途注入异常 → 事务回滚，global_facts 未被破坏。"""
    db, pid, _dr, _up = ctx
    await persist_extraction(_make_result(value="张伟"), db, pid, "", versioning=True)
    await persist_extraction(_make_result(value="李强"), db, pid, "", versioning=True)

    sid = await _find_snapshot(db, "张伟")
    assert sid is not None

    before = await db.execute("SELECT COUNT(*) n FROM global_facts")
    before_n = (await before.fetchone())["n"]

    # 还原 INSERT 注入异常（不影响 archive/DELETE 的同一事务）
    real_execute = db.execute

    async def fake_execute(sql, params=None):
        if str(sql).strip().startswith("INSERT INTO global_facts ("):
            raise RuntimeError("injected restore failure")
        return await real_execute(sql, params)

    db.execute = fake_execute
    try:
        with pytest.raises(RuntimeError):
            await rollback_facts_to_snapshot(db, sid, pid)
    finally:
        db.execute = real_execute

    # 事务回滚：行数不变、内容仍是 v2（李强），history 无新增
    cur = await db.execute("SELECT COUNT(*) n FROM global_facts")
    assert (await cur.fetchone())["n"] == before_n
    cur = await db.execute("SELECT content FROM global_facts")
    assert any("李强" in (r["content"] or "") for r in await cur.fetchall())
    cur = await db.execute("SELECT COUNT(*) n FROM global_facts_history")
    assert (await cur.fetchone())["n"] == 0, "失败回滚须不留下 history 记录"


async def test_endpoints_disabled_when_off(ctx):
    """默认关闭：rollback/snapshots 端点返回 409。"""
    db, pid, _dr, _up = ctx
    monkeypatch_off = None
    orig = settings.enable_fact_versioning
    settings.enable_fact_versioning = False
    try:
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            await gf.rollback_facts(pid, {"snapshot_id": "x"}, db=db)
        assert ei.value.status_code == 409
        with pytest.raises(HTTPException) as ei2:
            await gf.get_fact_snapshots(pid, db=db)
        assert ei2.value.status_code == 409
    finally:
        settings.enable_fact_versioning = orig


async def test_endpoints_enabled_roundtrip(ctx, monkeypatch):
    """开启：回滚端点端到端可用（建快照 → 列出 → 回滚）。"""
    db, pid, _dr, _up = ctx
    monkeypatch.setattr(settings, "enable_fact_versioning", True)
    await persist_extraction(_make_result(value="张伟"), db, pid, "", versioning=True)
    await persist_extraction(_make_result(value="李强"), db, pid, "", versioning=True)

    snaps = await gf.get_fact_snapshots(pid, db=db)
    assert len(snaps["snapshots"]) == 2
    sid = await _find_snapshot(db, "张伟")
    res = await gf.rollback_facts(pid, {"snapshot_id": sid}, db=db)
    assert res["ok"] is True
    cur = await db.execute("SELECT content FROM global_facts")
    assert any("张伟" in (r["content"] or "") for r in await cur.fetchall())
