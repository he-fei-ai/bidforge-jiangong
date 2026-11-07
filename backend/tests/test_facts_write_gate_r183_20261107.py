"""R183（2026-11-07）全局事实模块 · 写入闸门旁路回归。

R179 T-01 把「同名不同值的可注入事实不得并存」做进了三个入口：
手工新增、单条确认、矛盾裁决。但本轮的写入矩阵复核发现**四条写路径没有
过这道闸门**，它们在存量脏数据 / 多来源场景下可以把互相矛盾的取值同时
送进正文注入与导出：

1. ``POST /batch-resolve``：批量确认逐条过安全门槛，但**不过同名唯一性闸门**
   —— 单条确认会 409 的两条事实，批量确认能一次放行。
2. ``PATCH /{fact_id}/ack-stale``（单条）：解除过期标记时顺带 ``is_resolved=1``，
   同样不过闸门 —— stale 行恢复注入后与既有同名事实双值并存。
3. ``POST /ack-stale``（批量）：同上，SQL CASE 顺带确认，整批绕过。
4. ``PATCH /{fact_id}`` 条目级改名 / 分组重建：改名或重建后新行可注入，
   但未校验同名不同值 —— 编辑一个名称即可制造双值注入。

另外两个「静默丢弃」边界：

5. ``POST /global-facts`` 新增时，若既有同名同值行**未通过注入门控**
   （模拟值 / 待确认 / 过期 / 矛盾），旧实现按「同值幂等去重」返回成功，
   但用户补录的真实值根本没有落库，既有行仍被门控挡在正文外。
6. 历史行 ``fact_key=''``（fact_key 列引入前的存量数据）不在新增守卫的
   查询范围内 —— 同名不同值可静默插入。

本文件按「先证伪、再修复」写法：每条用例在修复前必须红，修复后绿；
保留的反向用例（同名同值、唯一改名、唯一过期解除）用于防止过度拦截。
"""

import uuid

import app.routers.global_facts as gf
import pytest
from app.models import (
    AckStaleRequest,
    BatchResolveRequest,
    FactGroupIn,
    FactGroupUpdate,
    FactItem,
)
from app.services.facts_extractor import normalize_key
from fastapi import HTTPException

PID = "proj-r183"
SID = "scheme-r183"


async def _seed_scope(db):
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (PID, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (SID, PID, "s"))
    await db.commit()


async def _fact(
    db,
    *,
    title: str,
    value: str,
    resolved: int = 1,
    simulated: int = 0,
    conflict: int = 0,
    stale: int = 0,
    fact_key: str | None = None,
    group_id: str = "g1",
    pid: str = PID,
    sid: str = SID,
    fid: str | None = None,
) -> str:
    fid = fid or "f-" + uuid.uuid4().hex[:10]
    content = f"- **{title}**: {value}"
    if simulated:
        content += " *(模拟值)*"
    await db.execute(
        "INSERT INTO global_facts(id, project_id, scheme_id, group_id, group_title, title,"
        " content, category, is_simulated, is_resolved, has_conflict, is_stale, fact_key)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fid,
            pid,
            sid,
            group_id,
            "人员",
            title,
            content,
            "personnel",
            int(simulated),
            int(resolved),
            int(conflict),
            int(stale),
            normalize_key(title) if fact_key is None else fact_key,
        ),
    )
    await db.commit()
    return fid


async def _get(db, fid: str) -> dict:
    cur = await db.execute(
        "SELECT id, title, content, is_resolved, is_stale, is_simulated, has_conflict"
        " FROM global_facts WHERE id=?",
        (fid,),
    )
    return dict(await cur.fetchone())


# ---------------------------------------------------------------------------
# 1. 批量确认不得绕过同名唯一性闸门
# ---------------------------------------------------------------------------


async def test_batch_resolve_skips_duplicate_injectable_value(db_conn):
    """存量两条同名不同值且均未确认：批量确认只能放行其中一条。

    修复前：两条都被 UPDATE，正文注入拿到「张三/李四」两个值。
    """
    await _seed_scope(db_conn)
    a = await _fact(db_conn, title="项目经理", value="张三", resolved=0)
    b = await _fact(db_conn, title="项目经理", value="李四", resolved=0)

    res = await gf.batch_resolve(BatchResolveRequest(fact_ids=[a, b], scheme_id=SID), db=db_conn)

    assert res["ok"] is True
    assert res["changed"] == 1, f"批量确认放行了互相矛盾的两条事实：{res}"
    blocked = [s for s in res["skipped_safety"] if s.get("reason_code") == "duplicate_injectable_fact"]
    assert len(blocked) == 1 and blocked[0]["id"] in {a, b}
    ra, rb = await _get(db_conn, a), await _get(db_conn, b)
    assert sum(int(r["is_resolved"]) for r in (ra, rb)) == 1
    assert res["safety_blocked"] is True


async def test_batch_resolve_blank_ids_returns_normal_contract(db_conn):
    """空白 id 列表不得返回另一套响应形状（前端只读 changed）。"""
    await _seed_scope(db_conn)
    res = await gf.batch_resolve(
        BatchResolveRequest(fact_ids=["   ", ""], scheme_id=SID), db=db_conn
    )
    assert res["changed"] == 0 and res["skipped"] == 0
    assert "updated" not in res, "旧形状 updated 会让前端批量确认提示失效"


async def test_batch_resolve_allows_same_name_same_value(db_conn):
    """同名同值不是矛盾：重复行允许一起确认（防止过度拦截）。"""
    await _seed_scope(db_conn)
    a = await _fact(db_conn, title="项目经理", value="张三", resolved=0)
    b = await _fact(db_conn, title="项目经理", value="张三", resolved=0)
    res = await gf.batch_resolve(BatchResolveRequest(fact_ids=[a, b], scheme_id=SID), db=db_conn)
    assert res["changed"] == 2
    assert not [s for s in res["skipped_safety"] if s.get("reason_code") == "duplicate_injectable_fact"]


# ---------------------------------------------------------------------------
# 2. 单条 / 批量解除过期不得绕过同名唯一性闸门
# ---------------------------------------------------------------------------


async def test_ack_stale_single_rejects_duplicate_injectable_value(db_conn):
    await _seed_scope(db_conn)
    await _fact(db_conn, title="项目经理", value="张三", resolved=1, stale=0, group_id="g1")
    b = await _fact(db_conn, title="项目经理", value="李四", resolved=0, stale=1, group_id="g2")

    with pytest.raises(HTTPException) as ei:
        await gf.ack_fact_stale(b, SID, db_conn)
    assert ei.value.status_code == 409
    row = await _get(db_conn, b)
    assert row["is_stale"] == 1 and row["is_resolved"] == 0, "闸门拒绝后不得留下半更新状态"


async def test_ack_stale_batch_blocks_duplicate_and_keeps_others(db_conn):
    await _seed_scope(db_conn)
    await _fact(db_conn, title="项目经理", value="张三", resolved=1, group_id="g1")
    dup = await _fact(db_conn, title="项目经理", value="李四", resolved=0, stale=1, group_id="g2")
    ok = await _fact(db_conn, title="安全员", value="王五", resolved=0, stale=1, group_id="g3")

    res = await gf.batch_ack_stale(AckStaleRequest(fact_ids=[dup, ok], scheme_id=SID), db=db_conn)

    assert res["changed"] == 1
    assert res["blocked_duplicates_count"] == 1
    assert res["blocked_duplicates"][0]["id"] == dup
    assert (await _get(db_conn, dup))["is_stale"] == 1, "被拦条目应保持过期状态，等用户先处理同名矛盾"
    assert (await _get(db_conn, ok))["is_stale"] == 0


async def test_ack_stale_unique_fact_still_works(db_conn):
    """反向用例：无同名矛盾的过期事实照旧可解除并确认。"""
    await _seed_scope(db_conn)
    fid = await _fact(db_conn, title="安全员", value="王五", resolved=0, stale=1)
    res = await gf.ack_fact_stale(fid, SID, db_conn)
    assert res["changed"] is True and res["is_resolved"] is True


# ---------------------------------------------------------------------------
# 3. 条目级改名 / 分组重建不得绕过同名唯一性闸门
# ---------------------------------------------------------------------------


async def test_item_update_rename_to_existing_fact_rejected(db_conn):
    await _seed_scope(db_conn)
    await _fact(db_conn, title="项目经理", value="张三", resolved=1, group_id="g1")
    b = await _fact(db_conn, title="技术负责人", value="李四", resolved=1, group_id="g2")

    with pytest.raises(HTTPException) as ei:
        await gf.update_fact(
            b,
            FactGroupUpdate(id=b, item_updates=[{"fact_id": b, "name": "项目经理"}]),
            scheme_id=SID,
            db=db_conn,
        )
    assert ei.value.status_code == 409
    row = await _get(db_conn, b)
    assert row["title"] == "技术负责人", "拒绝后不得落库改名"


async def test_group_rebuild_to_existing_fact_marks_conflict(db_conn):
    """分组重建遇到跨组同名不同值：保留行与溯源，但新行转入矛盾待裁决。"""
    await _seed_scope(db_conn)
    await _fact(db_conn, title="项目经理", value="张三", resolved=1, group_id="g1")
    await _fact(db_conn, title="技术负责人", value="李四", resolved=1, group_id="g2", fid="row-b")

    res = await gf.update_fact(
        "g2",
        FactGroupUpdate(id="g2", title="人员", content="- **项目经理**: 王五"),
        scheme_id=SID,
        db=db_conn,
    )
    assert res["ok"] is True
    row = dict(
        await (
            await db_conn.execute(
                "SELECT title, content, is_resolved, has_conflict FROM global_facts WHERE group_id='g2'"
            )
        ).fetchone()
    )
    assert row["title"] == "项目经理"
    assert row["has_conflict"] == 1 and row["is_resolved"] == 0, "同名不同值的新行必须退出注入，等用户裁决"
    injectable = await gf._load_fact_rows(db_conn, SID, PID, injectable_only=True)
    assert [(gf._fact_name_value(r)) for r in injectable] == [("项目经理", "张三")]


async def test_group_rebuild_intra_group_duplicate_keeps_one_injectable(db_conn):
    """组内粘贴同名不同值：两行与各自溯源都保留，但只有第一行可注入。"""
    await _seed_scope(db_conn)
    await _fact(db_conn, title="基坑深度", value="12.5", resolved=1, group_id="g9")
    await gf.update_fact(
        "g9",
        FactGroupUpdate(id="g9", title="技术参数", content="- **基坑深度**: 12.5 m\n- **基坑深度**: 14.0 m"),
        scheme_id=SID,
        db=db_conn,
    )
    rows = [
        dict(r)
        for r in await (
            await db_conn.execute(
                "SELECT content, is_resolved, has_conflict FROM global_facts WHERE group_id='g9' ORDER BY rowid"
            )
        ).fetchall()
    ]
    assert len(rows) == 2
    assert int(rows[0]["is_resolved"]) == 1 and int(rows[0]["has_conflict"]) == 0
    assert int(rows[1]["is_resolved"]) == 0 and int(rows[1]["has_conflict"]) == 1
    injectable = await gf._load_fact_rows(db_conn, SID, PID, injectable_only=True)
    assert len(injectable) == 1 and gf._fact_name_value(injectable[0])[1] == "12.5 m"


async def test_item_update_unique_rename_still_works(db_conn):
    await _seed_scope(db_conn)
    b = await _fact(db_conn, title="技术负责人", value="李四", resolved=1)
    res = await gf.update_fact(
        b,
        FactGroupUpdate(id=b, item_updates=[{"fact_id": b, "name": "项目技术负责人"}]),
        scheme_id=SID,
        db=db_conn,
    )
    assert res["ok"] is True
    assert (await _get(db_conn, b))["title"] == "项目技术负责人"


# ---------------------------------------------------------------------------
# 4. 手工新增：既有同名行未通过注入门控时不得静默「去重成功」
# ---------------------------------------------------------------------------


async def test_create_fact_same_value_as_simulated_is_not_silent_dedupe(db_conn):
    """用户补录真实值，既有同名同值行是模拟值：应 409 引导先处理既有行。

    修复前：返回 deduplicated=1，用户以为已保存，正文仍拿不到该事实。
    """
    await _seed_scope(db_conn)
    await _fact(db_conn, title="项目经理", value="张三", resolved=0, simulated=1)

    with pytest.raises(HTTPException) as ei:
        await gf.create_fact(
            FactGroupIn(title="人员", content="", category="personnel", items=[FactItem(name="项目经理", value="张三")]),
            scheme_id=SID,
            project_id="",
            db=db_conn,
        )
    assert ei.value.status_code == 409
    assert "模拟" in str(ei.value.detail) or "门控" in str(ei.value.detail)


async def test_create_fact_detects_legacy_row_with_empty_fact_key(db_conn):
    """存量行 fact_key='' 时必须仍按 title 参与同名守卫。"""
    await _seed_scope(db_conn)
    await _fact(db_conn, title="项目经理", value="张三", resolved=1, fact_key="")

    with pytest.raises(HTTPException) as ei:
        await gf.create_fact(
            FactGroupIn(title="人员", content="", category="personnel", items=[FactItem(name="项目经理", value="李四")]),
            scheme_id=SID,
            project_id="",
            db=db_conn,
        )
    assert ei.value.status_code == 409


async def test_create_fact_same_value_as_resolved_is_still_idempotent(db_conn):
    """反向用例：既有行可注入且同值 → 仍按幂等去重（不回归 R179 契约）。"""
    await _seed_scope(db_conn)
    first = await gf.create_fact(
        FactGroupIn(title="人员", content="", category="personnel", items=[FactItem(name="项目经理", value="张三")]),
        scheme_id=SID,
        project_id="",
        db=db_conn,
    )
    again = await gf.create_fact(
        FactGroupIn(title="人员", content="", category="personnel", items=[FactItem(name="项目经理", value="张三")]),
        scheme_id=SID,
        project_id="",
        db=db_conn,
    )
    assert again["deduplicated"] == 1
    assert again["id"] == first["id"]
