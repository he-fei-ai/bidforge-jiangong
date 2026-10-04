"""export_cache 并发原子性回归测试。

查询与唯一约束必须使用同一三元组：
(scheme_id, config_hash, content_fingerprint)。同一内容使用不同排版配置时，
两种配置都应保留并可分别命中缓存；完全相同的三元组并发写入只保留一行。
"""
import uuid

import app.db as _appdb
import pytest
from app.db import close_db, get_conn, init_db


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "export_cache_uniq.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    sid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
        (sid, pid, "s"))
    try:
        yield db, sid
    finally:
        await close_db()


async def _insert_cache(db, sid, config_hash, fp, path):
    await db.execute(
        "INSERT OR IGNORE INTO export_cache "
        "(id, project_id, scheme_id, config_hash, content_fingerprint, cache_key, result_path) "
        "VALUES (?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, "", sid, config_hash, fp, f"{sid}_{config_hash}", path))


async def test_same_triple_is_idempotent_but_configs_are_kept(ctx):
    """同三元组第二次写入不新增行；不同 config_hash 均保留。"""
    db, sid = ctx
    fp = "fp_abc123"
    await _insert_cache(db, sid, "cfg1", fp, "/x/1.docx")
    await _insert_cache(db, sid, "cfg1", fp, "/x/1-retry.docx")
    await _insert_cache(db, sid, "cfg2", fp, "/x/2.docx")
    await db.commit()

    cur = await db.execute(
        "SELECT config_hash, COUNT(*) AS n FROM export_cache "
        "WHERE scheme_id=? AND content_fingerprint=? GROUP BY config_hash", (sid, fp))
    assert {r["config_hash"]: r["n"] for r in await cur.fetchall()} == {"cfg1": 1, "cfg2": 1}


async def test_distinct_fingerprints_both_kept(ctx):
    """不同指纹互不干扰，各自保留。"""
    db, sid = ctx
    for i, fp in enumerate(("fp_a", "fp_b")):
        await _insert_cache(db, sid, f"cfg{i}", fp, f"/x/{i}.docx")
    await db.commit()
    cur = await db.execute("SELECT COUNT(*) FROM export_cache WHERE scheme_id=?", (sid,))
    assert dict(await cur.fetchone())["COUNT(*)"] == 2
