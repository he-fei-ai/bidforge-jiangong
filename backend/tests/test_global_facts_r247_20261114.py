# -*- coding: utf-8 -*-
"""全局事实模块收口（R247 · 2026-11-14）护栏。

承接 R225 §六 遗留登记中仍真实存在的三项缺口，按「行为实证 / 源码锁 / 字节体检」
三层锁定：

  A. 行为实证
     1. BE-1  _assert_fact_in_scheme_scope 稳定排序：fact_id 同时命中「本行 id」
              与「跨方案残留的同名 group 行」时，必须取本行 id（旧实现无 ORDER BY
              ⇒ SQLite 取行不确定 ⇒ 归属校验时而 409 时而放行）。
     2. BE-2  items_to_prompt_json 超 _MAX_PROMPT_FACTS 留 WARNING（长库漏改可归因）；
              未超限零 warning（不制造噪声）。
     3. A9    adjust_facts 仅传 project_id（无 scheme_id）必须 400（禁止跨方案批量改写）。
     4. A7    list_cross_conflicts 方案不存在 → 404（区分「零冲突」与「查无此方案」）。
     5. A6    _LOCKS 取锁/释放配对后空闲条目即被驱逐（长期运行进程锁映射有界）；
              锁仍被持有时绝不驱逐（不破坏互斥）。

  B. 源码锁（锁调用点，不锁定义）
     3. BE-1  SQL 含确定性 ORDER BY（id 命中优先 + scheme_id + id）。
     4. FE-1  前端护栏见 frontend/src/tests/factsR247.test.ts（本文件只锁后端面）。
"""

import asyncio
import logging
from pathlib import Path

import app.routers.global_facts as gf
import pytest
from app.models import FactItem
from app.services import facts_write_guard as fwg
from app.services.facts_enrich import _MAX_PROMPT_FACTS, items_to_prompt_json
from fastapi import HTTPException

_GF = Path(__file__).resolve().parents[1] / "app" / "routers" / "global_facts.py"
_EN = Path(__file__).resolve().parents[1] / "app" / "services" / "facts_enrich.py"
_WG = Path(__file__).resolve().parents[1] / "app" / "services" / "facts_write_guard.py"


async def _seed_group_collision(db):
    """构造「本行 id 命中 + 跨方案残留同名 group 行」并存的数据集。

    fact_id='f-1' 同时是：
      - scheme s1（当前方案）某行的 id；
      - scheme s0（其它方案）残留行的 group_id（历史迁移/手工改库的悬空 group）。
    旧实现 WHERE id=? OR group_id=? LIMIT 1 无 ORDER BY，取哪行不确定。
    """
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", ("p1", "项目"))
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", ("p2", "其它项目"))
    await db.executemany(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
        [("s1", "p1", "方案一"), ("s0", "p2", "其它方案")],
    )
    rows = [
        # 当前方案：本行 id 命中
        ("f-1", "p1", "s1", "g-own", "本行事实"),
        # 其它方案残留行：group_id=f-1，scheme_id 字典序更小，无序时更可能被误取
        ("a-0", "p2", "s0", "f-1", "残留组事实"),
    ]
    for fid, pid, sid, gid, name in rows:
        await db.execute(
            "INSERT INTO global_facts(id,project_id,scheme_id,group_id,title,content,"
            "category,is_simulated,is_resolved,has_conflict,conflict_keys,fact_key) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, pid, sid, gid, name, f"- **{name}**: v", "basic", 0, 1, 0, "", name),
        )
    await db.commit()


async def test_scope_assert_prefers_id_row_over_cross_scheme_group_row(db_conn):
    await _seed_group_collision(db_conn)
    # s0 的 f-1 是其它方案私有事实 ⇒ 必须判 409，绝不能取到 s1 的本行放行。
    with pytest.raises(HTTPException) as exc:
        await gf._assert_fact_in_scheme_scope(db_conn, "f-1", "s0")
    assert exc.value.status_code == 409
    # s1 视角：本行 id 命中，正常放行并返回本行。
    row = await gf._assert_fact_in_scheme_scope(db_conn, "f-1", "s1")
    assert row["id"] == "f-1"
    assert row["scheme_id"] == "s1"


async def test_scope_assert_returns_the_id_row_itself(db_conn):
    await _seed_group_collision(db_conn)
    row = await gf._assert_fact_in_scheme_scope(db_conn, "f-1", "")
    assert row["id"] == "f-1"


def test_scope_assert_sql_has_deterministic_order():
    """源码锁：SQL 必须含确定性 ORDER BY（id 命中优先 + scheme_id + id）。"""
    src = _GF.read_text(encoding="utf-8")
    assert "ORDER BY (CASE WHEN id=? THEN 0 ELSE 1 END), scheme_id, id LIMIT 1" in src


def test_items_to_prompt_json_warns_over_cap(caplog):
    items = [FactItem(name=f"事实{i}", value=str(i)) for i in range(_MAX_PROMPT_FACTS + 5)]
    with caplog.at_level(logging.WARNING, logger="facts_enrich"):
        out = items_to_prompt_json(items)
    assert len(out) == _MAX_PROMPT_FACTS
    assert any("超出上限" in r.message for r in caplog.records)


def test_items_to_prompt_json_no_warning_under_cap(caplog):
    items = [FactItem(name=f"事实{i}", value=str(i)) for i in range(3)]
    with caplog.at_level(logging.WARNING, logger="facts_enrich"):
        out = items_to_prompt_json(items)
    assert len(out) == 3
    assert not any("超出上限" in r.message for r in caplog.records)


def test_items_to_prompt_json_shape_unchanged():
    """返回形态逐字不变（fact_key/name/value 三键）。"""
    items = [FactItem(name="总工期", value="450 天", key="total_duration")]
    (out,) = items_to_prompt_json(items)
    assert set(out) == {"fact_key", "name", "value"}
    assert out["fact_key"] == "total_duration"
    assert out["name"] == "总工期"


class TestByteIntegrity:
    TOUCHED = [_GF, _EN, _WG]

    def test_line_endings_and_bytes(self):
        for p in self.TOUCHED:
            raw = p.read_bytes()
            assert b"\r\n" in raw, f"{p.name} 应保持 CRLF"
            lf_only = raw.replace(b"\r\n", b"")
            assert b"\n" not in lf_only, f"{p.name} 混入裸 LF"
            assert not raw.startswith(b"\xef\xbb\xbf"), f"{p.name} BOM=0"
            assert b"\xef\xbf\xbd" not in raw, f"{p.name} U+FFFD=0"


def test_touched_files_exist():
    for p in [_GF, _EN, _WG]:
        assert p.exists(), f"{p} 不存在"


# ===========================================================================
# R247 补充收口（承接 §八 遗留登记按最优方案执行）
# ===========================================================================


async def _seed_project_scheme(db):
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", ("p9", "项目"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", ("s9", "p9", "方案"))
    await db.commit()


@pytest.mark.asyncio
async def test_adjust_requires_scheme_id_blocks_cross_scheme_bulk(db_conn):
    """A9：仅传 project_id（无 scheme_id）必须 400 —— 关闭跨方案改写越权面。"""
    await _seed_project_scheme(db_conn)
    with pytest.raises(HTTPException) as exc:
        await gf.adjust_facts(
            gf.AdjustRequest(instruction="把年份统一改成 2026", scheme_id="", project_id="p9"),
            db=db_conn,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_cross_conflicts_unknown_scheme_404(db_conn):
    """A7：方案不存在 → 404（旧实现 `or ""` 吞成空 pid 返回空列表）。"""
    with pytest.raises(HTTPException) as exc:
        await gf.list_cross_conflicts(scheme_id="no-such-scheme", project_id="", db=db_conn)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cross_conflicts_existing_scheme_without_conflicts_ok(db_conn):
    """A7 反向：方案存在但零冲突 → 200 空列表（不是 404，作用域退化非错误）。"""
    await _seed_project_scheme(db_conn)
    resp = await gf.list_cross_conflicts(scheme_id="s9", project_id="", db=db_conn)
    assert resp["total"] == 0
    assert resp["conflicts"] == []


def test_locks_evicted_after_release_when_idle():
    """A6：取锁→释放配对后，空闲锁条目从 _LOCKS 驱逐（锁映射有界）。"""

    async def _run():
        key = None
        async with fwg.project_facts_inproc_guard("p-evict", holder="t") as acquired:
            assert acquired is True
            loop = asyncio.get_running_loop()
            key = (id(loop), "p-evict")
            # 持有期间绝不驱逐（破坏互斥）
            assert key in fwg._LOCKS
        # 释放后无等待者 → 驱逐
        assert key not in fwg._LOCKS
        assert fwg._LOCKS_WAITERS.get(key, 0) == 0

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_locks_not_evicted_while_waiter_pending(monkeypatch):
    """A6：取锁引用未配对释放前（别的协程可能正要 acquire），空闲锁也不得驱逐。"""

    loop = asyncio.get_running_loop()
    key = (id(loop), "p-waiter")
    lock = fwg._lock_for("p-waiter")
    try:
        # 取锁引用未配对释放 ⇒ 等待者计数 > 0 ⇒ 不得驱逐（否则别的协程刚拿到手的
        # lock 对象被从映射摘掉，互斥被破坏）。
        assert key in fwg._LOCKS, "有等待者时不得驱逐"
        assert fwg._LOCKS.get(key) is lock
    finally:
        fwg._release_lock_ref("p-waiter")
    # 引用配对释放后 → 驱逐
    assert key not in fwg._LOCKS

    # 正向判别：同一批里「无等待者」的空闲条目必须被清掉，带等待者的必须留下。
    kept = (id(asyncio.get_running_loop()), "p-keep")
    idle = (id(asyncio.get_running_loop()), "p-idle")
    fwg._lock_for("p-keep")  # 有等待者
    fwg._LOCKS.setdefault(idle, asyncio.Lock())  # 空闲、无等待者
    # 压低规模阈值，强制清理路径真正执行（否则 len(_LOCKS) <= _LOCKS_MAX 早退）
    monkeypatch.setattr(fwg, "_LOCKS_MAX", 0)
    with fwg._LOCKS_GUARD:
        fwg._evict_idle_locks_locked()
    assert idle not in fwg._LOCKS, "空闲且无等待者的锁条目必须被驱逐"
    assert kept in fwg._LOCKS, "带等待者的锁条目绝不得被驱逐"
    fwg._release_lock_ref("p-keep")
