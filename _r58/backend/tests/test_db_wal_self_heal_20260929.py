"""WAL 损坏启动自愈回归（2026-09-29 · 全局事实「提取失败」根因修复）。

背景：线上 ``scheme_assistant.db`` 的 ``-wal`` 文件损坏，主库本身完好，但 SQLite 在
打开时会把损坏的 WAL 一并读入，导致整库被判定为 ``database disk image is malformed``。
后果：所有 DB 操作（load_completed_chunks → no such table / persist → malformed /
task 更新 → database is locked）全线失败，全局事实等重度依赖 DB 的模块「提取失败」。

修复：``app.db._self_heal_corrupt_wal`` 在 ``init_db`` 首次建连前，若发现
「主库完好但 WAL/SHM 损坏」，则备份并丢弃 WAL/SHM（主库不丢），使主库以回滚日志
模式正常打开。

测试覆盖：
- 无 WAL：不误删、返回 False（no-op）；
- 成功自愈的控制流（检测→备份→删 WAL→复检 ok→返回 True），用白盒桩驱动
  （SQLite 会在打开时静默忽略/自动 checkpoint 掉损坏 WAL，无法在进程内精确复现
  「WAL 应用后 B-tree 不一致」的真实生产损坏，但生产日志证实 integrity_check 当时
  确实返回了错误，自愈逻辑与之同构）；
- 安全网：主库本身损坏时，自愈失败会**还原备份、返回 False、绝不删除主库**
  （最关键的不变量：绝不雪上加霜、绝不谎报成功）。
"""
import sqlite3 as _s3
from unittest.mock import patch

from app import db as _db


def _make_healthy_db(p):
    """造一个含数据的健康主库（WAL 模式写入后关闭）。"""
    c = _s3.connect(str(p))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    c.execute("INSERT INTO t(v) VALUES('hello')")
    c.commit()
    c.close()


def _reset_module_conn():
    # G12-7 口径：隔离模块级连接缓存，避免污染其它测试
    _db._conn = None
    _db._conn_path = None


def test_no_wal_is_noop(monkeypatch, tmp_path):
    """无 WAL 文件（干净关闭）时不应误删任何东西，返回 False。"""
    p = tmp_path / "heal_no_wal.db"
    _make_healthy_db(p)
    for suf in ("-wal", "-shm"):
        f = p.with_name(p.name + suf)
        if f.exists():
            f.unlink()
    monkeypatch.setattr(_db, "DB_PATH", p)
    _reset_module_conn()

    before = p.stat().st_size
    healed = _db._self_heal_corrupt_wal()
    assert healed is False
    # 主库未被触碰、未生成备份目录
    assert p.exists() and p.stat().st_size == before
    baks = [d for d in tmp_path.iterdir()
            if d.is_dir() and d.name.startswith("recov_bak_")]
    assert not baks, "无 WAL 时不应生成备份"


def test_self_heal_removes_corrupt_wal(monkeypatch, tmp_path):
    """损坏 WAL 应被备份并丢弃，主库完整保留、可正常打开（白盒驱动控制流）。"""
    p = tmp_path / "heal.db"
    _make_healthy_db(p)
    # 造一个 WAL 文件，使 _self_heal 判定需要自愈
    p.with_name(p.name + "-wal").write_bytes(b"\x00" * 64)
    monkeypatch.setattr(_db, "DB_PATH", p)
    _reset_module_conn()

    # 第一次自检（带损坏 WAL）报非 ok；第二次（删 WAL 后）报 ok
    calls = {"n": 0}

    class _FakeConn:
        def execute(self, sql, *a):
            calls["n"] += 1

            class _R:
                def fetchone(self):
                    return ("ok",) if calls["n"] >= 2 else ("corrupt_tree", "")
            return _R()

        def close(self):
            pass

    with patch.object(_db.sqlite3, "connect", lambda *a, **k: _FakeConn()):
        healed = _db._self_heal_corrupt_wal()

    assert healed is True
    # WAL 已被删除
    assert not p.with_name(p.name + "-wal").exists()
    # 备份目录已生成（含主库副本）
    baks = [d for d in tmp_path.iterdir()
            if d.is_dir() and d.name.startswith("recov_bak_")]
    assert baks, "自愈应生成 recov_bak 备份目录"
    assert (baks[0] / "heal.db").exists()
    # 主库数据未丢
    c = _s3.connect(str(p))
    try:
        assert c.execute("PRAGMA integrity_check(1)").fetchone() == ("ok",)
        assert c.execute("SELECT v FROM t").fetchone()[0] == "hello"
    finally:
        c.close()


def test_self_heal_restores_when_main_still_corrupt(monkeypatch, tmp_path):
    """主库本身损坏时：自愈失败应还原备份、返回 False，绝不删除/谎报。

    真实场景：WAL 损坏导致 integrity 非 ok，但丢弃 WAL 后主库本身仍是坏的
    （例如主库页也被写坏）。此时必须把备份**还原**、返回 False，绝不能删主库或谎报成功。
    SQLite 对单行字节翻转常在 integrity_check 下被忽略，故用白盒桩确定性驱动「主库仍损坏」分支。
    """
    p = tmp_path / "heal_main_corrupt.db"
    _make_healthy_db(p)
    # 另有 WAL 存在（触发「WAL 存在」分支 → 进入自愈流程）
    p.with_name(p.name + "-wal").write_bytes(b"\x01" * 64)
    monkeypatch.setattr(_db, "DB_PATH", p)
    _reset_module_conn()

    size_before = p.stat().st_size

    # 第一次（带损坏 WAL）：integrity 非 ok → 触发自愈
    # 丢弃 WAL 后第二次：主库本身仍损坏 → 应还原备份、返回 False
    class _FakeConn:
        def __init__(self, n):
            self._n = n
        def execute(self, sql, *a):
            class _R:
                def fetchone(self):
                    # 两次都非 ok：模拟「主库本身也损坏」
                    return ("corrupt_btree", "")
            return _R()
        def close(self):
            pass

    with patch.object(_db.sqlite3, "connect", lambda *a, **k: _FakeConn(0)):
        healed = _db._self_heal_corrupt_wal()

    # 不应谎报成功
    assert healed is False
    # 主库文件未被删除、大小不变（未被截断/移除，备份被还原）
    assert p.exists() and p.stat().st_size == size_before
    # 应生成备份目录（含主库副本，便于事后人工介入）
    baks = [d for d in tmp_path.iterdir()
            if d.is_dir() and d.name.startswith("recov_bak_")]
    assert baks, "主库损坏时应仍生成备份（便于人工介入）"
    assert (baks[0] / "heal_main_corrupt.db").exists()
