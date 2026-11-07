"""R184（2026-11-07）· 目录保存并发写锁前移（BEGIN IMMEDIATE）实证用例。

背景（证据与事务边界说明见 ``docs/目录生成模块_R184_三处挂死修复与并发保存核查_20261107.md``）：
``_save_outline_to_db`` 是「读旧快照 → 内存组装 → 整表写」的复合写操作。旧实现
三步之间没有任何写锁保护（sqlite3 隐式 autocommit 下 SELECT 不开启事务），两路
并发保存各自基于同一旧快照组装，写锁只在各自第一条 DML 时串行，实测三类脏形态：

  S1：A 在 P 下新增子节点 / B 用新根替换并删掉 P → 丢更新 + 孤儿行
      （A 返回成功、最终库里 A 的子节点挂在已不存在的 P 下）；
  S2：A、B 同时把「同一旧章节」当**新节点**插入       → 同名重复行
      （两行同名同级、父相同，A 的返回与最终库都不一致）；
  S3：A 删 X / B 删 Y                                → 并集删除
      （双方各自的删除都生效，X、Y 全没了，谁都没得到自己想要的结果）。

修复：在首个决策读之前 ``BEGIN IMMEDIATE``（RESERVED 写锁前移，跨连接/跨进程
有效），并把内部的 ``invalidate_export_cache``（其命中缓存行时会 commit）移到
唯一一次提交之后。本用例每类跑 12 轮，断言三类脏形态全部消失，且最终状态必为
**某个串行顺序的完整结果**（不是并集、不是半途）。

轮次与连接：同一库文件上开两条独立连接，等价于两个写者（SQLite 写锁是文件级
的，跨连接与跨进程语义一致）。
"""

import asyncio
import copy

import aiosqlite
from app.db import _migrate, _setup_connection
from app.routers.sections import _save_outline_to_db
from app.schema_sql import SCHEMA_SQL

SCHEME = "s-184"
PROJECT = "p-184"
ROUNDS = 12


async def _make_pair(tmp_path):
    """建库（完整 schema + 迁移）并返回两个独立连接。"""
    path = tmp_path / "r184.sqlite3"
    setup = await aiosqlite.connect(path)
    await setup.executescript(SCHEMA_SQL)
    await _migrate(setup)
    await setup.execute("INSERT OR IGNORE INTO projects(id,name) VALUES(?,?)", (PROJECT, "并发保存验证"))
    await setup.execute(
        "INSERT OR IGNORE INTO schemes(id,project_id,name,status,word_budget) VALUES(?,?,?,?,?)",
        (SCHEME, PROJECT, "并发保存验证", "草稿", 30000),
    )
    await setup.commit()
    await setup.close()

    a = await aiosqlite.connect(path)
    b = await aiosqlite.connect(path)
    for conn in (a, b):
        await _setup_connection(conn)
    return a, b


def _node(sid: str, title: str, children: list | None = None, level: int = 1, order: int = 0) -> dict:
    """构造一个目录节点。

    ``sid`` 以 ``local_`` 开头 = 前端新节点（必然 is_new）；否则 = DB 主键
    （``_preserve_original_ids`` 会把它记为 ``__original_id``）。
    """
    return {
        "id": sid,
        "title": title,
        "level": level,
        "sort_order": order,
        "children": children or [],
    }


async def _reset(conn, rows: list[dict]) -> None:
    """每轮重置：清章节/缓存并重建初始数据。

    刻意埋一条 ``export_cache`` 行 —— ``invalidate_export_cache`` 命中缓存行时
    会内部 ``commit()``，这是旧实现「事务中途提前释放写锁」的触发条件；修复后
    该调用已移到提交之后，不再影响保存事务。
    """
    await conn.rollback()
    await conn.execute("DELETE FROM sections WHERE scheme_id=?", (SCHEME,))
    await conn.execute("DELETE FROM chart_predictions WHERE scheme_id=?", (SCHEME,))
    await conn.execute("DELETE FROM export_cache WHERE scheme_id=?", (SCHEME,))
    await conn.execute("DELETE FROM consistency_scan_cache WHERE scheme_id=?", (SCHEME,))
    for r in rows:
        await conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level, sort_order, word_budget)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (r["id"], SCHEME, PROJECT, r.get("parent", ""), r["title"], r["level"], r.get("order", 0), 1200),
        )
    await conn.execute(
        "INSERT INTO export_cache (id, project_id, scheme_id) VALUES(?,?,?)",
        ("ec-" + SCHEME, PROJECT, SCHEME),
    )
    await conn.commit()


async def _dump(conn) -> list[dict]:
    cur = await conn.execute(
        "SELECT id, parent_id, title, level, sort_order FROM sections WHERE scheme_id=?"
        " ORDER BY sort_order, id",
        (SCHEME,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def _two(conn_a, conn_b, outline_a: list, outline_b: list) -> tuple[dict, dict]:
    """两路并发保存（各自独立连接、各自深拷贝入参 —— 保存过程会就地改写节点）。"""
    results: list = [None, None]

    async def _one(idx: int, conn, outline: list) -> None:
        try:
            results[idx] = await _save_outline_to_db(conn, SCHEME, copy.deepcopy(outline), source="")
        except Exception as e:  # noqa: BLE001 — 记入结果，由断言统一判定
            results[idx] = {"error": f"{type(e).__name__}: {e}"}

    await asyncio.gather(_one(0, conn_a, outline_a), _one(1, conn_b, outline_b))
    return results[0], results[1]


async def _assert_no_orphan(conn) -> list[dict]:
    rows = await _dump(conn)
    ids = {r["id"] for r in rows}
    for r in rows:
        pid = r["parent_id"] or ""
        assert not (pid and pid not in ids), f"存在孤儿行（parent_id 指向不存在的章节）：{r}"
    return rows


# ---------------------------------------------------------------- S1
async def test_s1_add_child_vs_delete_parent_is_serialized(tmp_path):
    """S1：A 在 P 下新增 / B 用新根替换并删 P。修后不得出现「丢更新 + 孤儿」。"""
    a, b = await _make_pair(tmp_path)
    try:
        for round_no in range(ROUNDS):
            await _reset(a, [{"id": "p", "title": "第1章 工程概况", "level": 1}])
            outline_a = [
                _node("p", "第1章 工程概况", [_node("local_a1", "1.1 新增子节点", level=2, order=1)])
            ]
            outline_b = [
                _node(
                    "local_q",
                    "第1章 现场总平面布置",
                    [_node("local_q1", "1.1 布置原则", level=2, order=1)],
                )
            ]
            ra, rb = await _two(a, b, outline_a, outline_b)
            assert ra.get("ok") and rb.get("ok"), f"第 {round_no + 1} 轮出现异常：{ra} / {rb}"

            rows = await _assert_no_orphan(a)
            # 串行化后只剩「后到者」的完整子树：要么 A 的 P+A1，要么 B 的 Q+Q1。
            # 旧实现会留下 3 行（Q、Q1、以及挂在已删除 P 下的 A1 孤儿）。
            assert len(rows) == 2, f"第 {round_no + 1} 轮非串行结果，实际 {len(rows)} 行：{rows}"
            roots = [r for r in rows if not r["parent_id"]]
            children = [r for r in rows if r["parent_id"]]
            assert len(roots) == 1 and len(children) == 1, f"第 {round_no + 1} 轮结构异常：{rows}"
            assert children[0]["parent_id"] == roots[0]["id"]
    finally:
        await a.close()
        await b.close()


# ---------------------------------------------------------------- S2
async def test_s2_same_section_inserted_as_new_by_both_is_not_duplicated(tmp_path):
    """S2：A、B 同时把「同一旧章节」当新节点插入。修后不得出现同名重复行。

    变异口径：把 ``BEGIN IMMEDIATE`` 退回默认事务（删除该语句）后，本用例必须
    转红（两轮各自插入的同名子节点会同时留在库里）。
    """
    a, b = await _make_pair(tmp_path)
    try:
        for round_no in range(ROUNDS):
            await _reset(
                a,
                [
                    {"id": "p", "title": "第1章 工程概况", "level": 1},
                    {"id": "c1", "parent": "p", "title": "1.1 旧节点", "level": 2, "order": 1},
                ],
            )
            # 双方都把旧 c1 换成「自己新建的同名节点」：id 以 local_ 开头 → is_new
            outline_a = [
                _node(
                    "p",
                    "第1章 工程概况",
                    [_node("local_a", "1.1 同名节点", level=2, order=1)],
                )
            ]
            outline_b = [
                _node(
                    "p",
                    "第1章 工程概况",
                    [_node("local_b", "1.1 同名节点", level=2, order=1)],
                )
            ]
            ra, rb = await _two(a, b, outline_a, outline_b)
            assert ra.get("ok") and rb.get("ok"), f"第 {round_no + 1} 轮出现异常：{ra} / {rb}"

            rows = await _assert_no_orphan(a)
            children = [r for r in rows if r["parent_id"] == "p"]
            titles = [r["title"] for r in children]
            assert titles.count("1.1 同名节点") <= 1, f"第 {round_no + 1} 轮出现重复行：{rows}"
            assert len(children) == 1, f"第 {round_no + 1} 轮子节点数不为 1：{rows}"
    finally:
        await a.close()
        await b.close()


# ---------------------------------------------------------------- S3
async def test_s3_both_deletions_do_not_union(tmp_path):
    """S3：A 删 X / B 删 Y。修后只能留下「后到者」保留的那一个，不得并集删除。"""
    a, b = await _make_pair(tmp_path)
    try:
        for round_no in range(ROUNDS):
            await _reset(
                a,
                [
                    {"id": "p", "title": "第1章 工程概况", "level": 1},
                    {"id": "x", "parent": "p", "title": "1.1 任务A", "level": 2, "order": 1},
                    {"id": "y", "parent": "p", "title": "1.2 任务B", "level": 2, "order": 2},
                ],
            )
            outline_a = [_node("p", "第1章 工程概况", [_node("y", "1.2 任务B", level=2, order=1)])]
            outline_b = [_node("p", "第1章 工程概况", [_node("x", "1.1 任务A", level=2, order=1)])]
            ra, rb = await _two(a, b, outline_a, outline_b)
            assert ra.get("ok") and rb.get("ok"), f"第 {round_no + 1} 轮出现异常：{ra} / {rb}"

            rows = await _assert_no_orphan(a)
            children = [r for r in rows if r["parent_id"] == "p"]
            # 旧实现：A 删 X、B 删 Y → 只剩 P（1 行，并集删除）。
            assert len(children) == 1, f"第 {round_no + 1} 轮并集删除：{rows}"
            # 后到者保留的节点可能因先到者已删除而被**重建**（新 uuid），故按标题判定。
            assert children[0]["title"] in {"任务A", "任务B"}, f"第 {round_no + 1} 轮保留了错误节点：{rows}"
    finally:
        await a.close()
        await b.close()


# ---------------------------------------------------------------- 串行路径
async def test_serial_path_commits_once_and_releases_lock(tmp_path):
    """串行路径行为不变：一次保存 = 一次提交，返回时事务已结束、写锁已释放。"""
    a, b = await _make_pair(tmp_path)
    try:
        await _reset(
            a,
            [
                {"id": "p", "title": "第1章 工程概况", "level": 1},
                {"id": "c1", "parent": "p", "title": "1.1 旧节点", "level": 2, "order": 1},
            ],
        )
        outline = [_node("p", "第1章 工程概况", [_node("c1", "1.1 保留节点", level=2, order=1)])]
        res = await _save_outline_to_db(a, SCHEME, outline, source="")

        assert res["ok"] and res["count"] == 2
        assert getattr(a, "in_transaction", False) is False, "返回后仍持有事务：写锁未释放"
        # 另一条连接立即可见已提交结果（证明提交发生在返回之前）
        rows = await _dump(b)
        roots = [(r["title"], r["level"]) for r in rows if not r["parent_id"]]
        children = [(r["title"], r["level"]) for r in rows if r["parent_id"]]
        # 落库前统一剥离标题编号（既有写入口径），故只断言去号后的标题与层级。
        assert roots == [("工程概况", 1)]
        assert children == [("保留节点", 2)]
        # 导出缓存失效已生效（且发生在保存事务之外）
        cur = await b.execute("SELECT COUNT(*) FROM export_cache WHERE scheme_id=?", (SCHEME,))
        assert (await cur.fetchone())[0] == 0
    finally:
        await a.close()
        await b.close()
