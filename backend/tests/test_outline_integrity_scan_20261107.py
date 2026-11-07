"""R184（2026-11-07）· 目录父子关系完整性扫描（方案 B：只扫描不修复）用例。

覆盖：
  1. 空库：三类全 0 且 status=completed —— 与「未扫描」可区分（not_scanned 时
     counts 为 None，completed 时 counts 是真实计数，0 就是 0）；
  2. 三类脏数据：SELF_PARENT / ORPHAN / CYCLE_NODE 各自被识别且计数精确，
     且三类互斥（自父不被重复计入 CYCLE_NODE）；
  3. 扫描异常：status=failed、counts=None、error 非空 —— 绝不折叠成「0 条」。
"""

import aiosqlite
from app.db import _migrate, _setup_connection
from app.schema_sql import SCHEMA_SQL
from app.services.outline_integrity import get_last_scan, reset_scan_state, scan_section_links

PROJECT = "p-scan"
SCHEME = "s-scan"


async def _make_db(tmp_path, sections: list[tuple[str, str]]) -> aiosqlite.Connection:
    """建完整库并写入 ``(id, parent_id)`` 章节行（全部挂在同一 scheme 下）。"""
    path = tmp_path / "scan.sqlite3"
    conn = await aiosqlite.connect(path)
    await conn.executescript(SCHEMA_SQL)
    await _migrate(conn)
    await conn.execute("INSERT OR IGNORE INTO projects(id,name) VALUES(?,?)", (PROJECT, "扫描用例"))
    await conn.execute(
        "INSERT OR IGNORE INTO schemes(id,project_id,name,status) VALUES(?,?,?,?)",
        (SCHEME, PROJECT, "扫描用例", "草稿"),
    )
    for sid, pid in sections:
        await conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level, sort_order)"
            " VALUES (?,?,?,?,?,?,?)",
            (sid, SCHEME, PROJECT, pid, f"节点{sid}", 1, 0),
        )
    await conn.commit()
    await _setup_connection(conn)
    return conn


async def test_empty_db_is_scanned_with_zero_counts(tmp_path):
    """空库：明确标记「已扫描」，三类全 0；与「未扫描」可区分。"""
    reset_scan_state()
    before = get_last_scan()
    assert before["status"] == "not_scanned"
    assert before["counts"] is None, "未扫描不得以 0 计数冒充已扫描"

    conn = await _make_db(tmp_path, [])
    try:
        res = await scan_section_links(conn)
    finally:
        await conn.close()

    assert res["status"] == "completed"
    assert res["error"] is None
    assert res["total_sections"] == 0
    assert res["counts"] == {"SELF_PARENT": 0, "ORPHAN": 0, "CYCLE_NODE": 0}
    assert get_last_scan()["status"] == "completed"


async def test_three_dirty_kinds_detected_with_exact_counts(tmp_path):
    """三类脏数据各被识别；计数精确且互斥（自父不计入 CYCLE_NODE）。"""
    reset_scan_state()
    # r 正常根 + c 正常子；s 自父；o 孤儿；a、b 组成二元环
    conn = await _make_db(
        tmp_path,
        [
            ("r", ""),
            ("c", "r"),
            ("s", "s"),
            ("o", "ghost"),
            ("a", "b"),
            ("b", "a"),
        ],
    )
    try:
        res = await scan_section_links(conn)
    finally:
        await conn.close()

    assert res["status"] == "completed"
    assert res["total_sections"] == 6
    assert res["counts"] == {"SELF_PARENT": 1, "ORPHAN": 1, "CYCLE_NODE": 2}
    samples = res["samples"]
    assert [s["id"] for s in samples["SELF_PARENT"]] == ["s"]
    assert [s["id"] for s in samples["ORPHAN"]] == ["o"]
    assert {s["id"] for s in samples["CYCLE_NODE"]} == {"a", "b"}


async def test_scan_failure_is_explicit_not_zero(tmp_path):
    """扫描异常：status=failed、counts=None（不是 0 条）、error 显性可见。"""
    reset_scan_state()
    conn = await _make_db(tmp_path, [])
    await conn.close()  # 制造读取失败（连接已关闭）

    res = await scan_section_links(conn)

    assert res["status"] == "failed"
    assert res["counts"] is None, "扫描失败不得折叠成 0 条"
    assert res["total_sections"] is None
    assert res["error"]
    assert get_last_scan()["status"] == "failed"
    assert get_last_scan()["counts"] is None
