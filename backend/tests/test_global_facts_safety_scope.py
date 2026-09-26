"""全局事实安全门控与作用域一致性回归测试。

锁住三条数据完整性不变式：
1. 模拟值/未裁决冲突不能被普通确认接口放行；
2. 下游可注入查询必须显式排除模拟值（即使历史脏数据 is_resolved=1）；
3. 事实页、章节视图与目录/正文/导出共享同一「方案 + 项目共享」作用域。
"""
import json
import uuid

import pytest
from fastapi import HTTPException

import app.routers.global_facts as gf
from app.services.facts_extractor import build_injectable_facts_query


async def _seed_scope(db):
    pid, sid, other_sid = "p1", "s1", "s2"
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "项目"))
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", ("p2", "其它项目"))
    await db.executemany(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
        [(sid, pid, "方案一"), (other_sid, pid, "方案二")])
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", ("s3", "p2", "其它方案"))

    async def add(fid, scheme, name, *, simulated=0, resolved=1, conflict=0):
        await db.execute(
            "INSERT INTO global_facts(id,project_id,scheme_id,group_id,title,content,"
            "category,is_simulated,is_resolved,has_conflict,conflict_keys,fact_key) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, pid if scheme in (sid, other_sid, "") else "p2", scheme,
             "g-" + fid, name, f"- **{name}**: 示例值", "basic",
             simulated, resolved, conflict,
             json.dumps([{"value": "候选值"}]) if conflict else "", name))

    await add("scheme-fact", sid, "方案事实")
    await add("project-fact", "", "项目共享事实")
    await add("other-scheme-fact", other_sid, "其它方案事实")
    await db.execute(
        "INSERT INTO global_facts(id,project_id,scheme_id,group_id,title,content,category) "
        "VALUES('other-project','p2','s3','g-other','其它项目事实','- **其它项目事实**: x','basic')")
    await db.commit()


async def test_resolve_rejects_simulated_or_conflicted_fact(db_conn):
    await _seed_scope(db_conn)
    for fid, label in (("scheme-fact", "模拟值"), ("other-scheme-fact", "未裁决冲突")):
        await db_conn.execute(
            "UPDATE global_facts SET is_resolved=0 WHERE id=?", (fid,))
        if label == "模拟值":
            await db_conn.execute("UPDATE global_facts SET is_simulated=1 WHERE id=?", (fid,))
        else:
            await db_conn.execute(
                "UPDATE global_facts SET has_conflict=1, conflict_keys=? WHERE id=?",
                (json.dumps([{"value": "另一值"}]), fid))
        await db_conn.commit()
        with pytest.raises(HTTPException) as exc:
            await gf.resolve_fact(fid, db=db_conn)
        assert exc.value.status_code == 409
        row = await (await db_conn.execute(
            "SELECT is_resolved FROM global_facts WHERE id=?", (fid,))).fetchone()
        assert row["is_resolved"] == 0


async def test_resolve_conflict_to_simulated_candidate_keeps_unresolved(db_conn):
    await _seed_scope(db_conn)
    await db_conn.execute(
        "UPDATE global_facts SET is_simulated=0, is_resolved=0, has_conflict=1, "
        "conflict_keys=? WHERE id='scheme-fact'",
        (json.dumps([{"value": "估算值", "is_simulated": True}]),))
    await db_conn.commit()
    await gf.resolve_conflict("scheme-fact", {"value": "估算值"}, db=db_conn)
    row = await (await db_conn.execute(
        "SELECT is_simulated,is_resolved,has_conflict FROM global_facts WHERE id='scheme-fact'"
    )).fetchone()
    assert dict(row) == {"is_simulated": 1, "is_resolved": 0, "has_conflict": 0}


def test_injectable_query_explicitly_excludes_simulated_and_conflicts():
    sql, _ = build_injectable_facts_query("s1", "p1", "title")
    assert "has_conflict=0" in sql
    assert "is_resolved=1" in sql
    assert "is_simulated=0" in sql
    assert "is_stale=0" in sql


async def test_list_facts_matches_downstream_scheme_and_project_scope(db_conn):
    await _seed_scope(db_conn)
    res = await gf.list_facts(scheme_id="s1", project_id="", db=db_conn)
    names = {it["name"] for g in res["groups"] for it in g["items"]}
    assert names == {"方案事实", "项目共享事实"}
    scopes = {it["name"]: it["scope"] for g in res["groups"] for it in g["items"]}
    assert scopes == {"方案事实": "scheme", "项目共享事实": "project"}
    assert res["stats"]["project_shared"] == 1


async def test_chapters_and_danger_reject_missing_or_unknown_scope(db_conn):
    await _seed_scope(db_conn)
    with pytest.raises(HTTPException) as exc:
        await gf.list_facts_by_chapters(scheme_id="", project_id="", db=db_conn)
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        await gf.list_facts_by_chapters(scheme_id="ghost", project_id="", db=db_conn)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await gf.check_danger_scheme(
            {"scheme_name": "基坑支护专项施工方案"}, scheme_id="", project_id="", db=db_conn)
    assert exc.value.status_code == 400



async def test_batch_resolve_includes_project_shared_facts(db_conn):
    await _seed_scope(db_conn)
    await db_conn.execute("UPDATE global_facts SET is_resolved=0 WHERE id='project-fact'")
    await db_conn.commit()
    res = await gf.batch_resolve({"scheme_id": "s1", "fact_ids": ["project-fact"]}, db=db_conn)
    assert res["changed"] == 1
    row = await (await db_conn.execute(
        "SELECT is_resolved FROM global_facts WHERE id='project-fact'")).fetchone()
    assert row["is_resolved"] == 1


async def test_resolve_rejects_stale_fact_until_rechecked(db_conn):
    await _seed_scope(db_conn)
    await db_conn.execute("UPDATE global_facts SET is_stale=1 WHERE id='scheme-fact'")
    await db_conn.commit()
    with pytest.raises(HTTPException) as exc:
        await gf.resolve_fact("scheme-fact", db=db_conn)
    assert exc.value.status_code == 409
    await gf.update_fact(
        "scheme-fact",
        gf.FactGroupUpdate(id="scheme-fact", content="- **方案事实**: 人工核对值"),
        db=db_conn,
    )
    row = await (await db_conn.execute(
        "SELECT is_stale FROM global_facts WHERE id='scheme-fact'")).fetchone()
    assert row["is_stale"] == 0


async def test_export_and_readiness_surface_blocked_facts(db_conn):
    from app.routers.export import collect_export_issues, export_issues_to_findings
    await _seed_scope(db_conn)
    await db_conn.execute(
        "UPDATE global_facts SET is_resolved=0, is_simulated=1, has_conflict=1, is_stale=1 "
        "WHERE id='scheme-fact'")
    await db_conn.commit()
    result = await collect_export_issues("s1", db_conn)
    assert result["global_facts_summary"] == {
        "total": 2, "unresolved": 1, "simulated": 1, "conflicted": 1, "stale": 1,
        "blocked": 1,
    }
    issue = next(i for i in result["issues"] if i["type"] == "global_facts_blocked")
    assert issue["count"] == 1
    finding = next(f for f in export_issues_to_findings([issue]) if f["rule_id"] == "DLV-15")
    assert finding["severity"] == "high"


async def test_batch_resolve_skips_conflicted_and_stale_facts(db_conn):
    await _seed_scope(db_conn)
    await db_conn.execute(
        "UPDATE global_facts SET is_resolved=0, has_conflict=1 WHERE id='scheme-fact'")
    await db_conn.execute(
        "UPDATE global_facts SET is_resolved=0, is_stale=1 WHERE id='project-fact'")
    await db_conn.commit()
    res = await gf.batch_resolve({"scheme_id": "s1"}, db=db_conn)
    assert res["changed"] == 0
    assert res["skipped_safety_count"] == 2
    reasons = " ".join(x["reason"] for x in res["skipped_safety"])
    assert "矛盾" in reasons and "来源资料已变化" in reasons





async def test_write_apis_reject_fact_from_other_scheme(db_conn):
    await _seed_scope(db_conn)
    for call in (
        lambda: gf.resolve_fact("other-scheme-fact", scheme_id="s1", db=db_conn),
        lambda: gf.delete_fact("other-scheme-fact", scheme_id="s1", db=db_conn),
        lambda: gf.update_fact(
            "other-scheme-fact", gf.FactGroupUpdate(id="other-scheme-fact", title="越权修改"),
            scheme_id="s1", db=db_conn),
    ):
        with pytest.raises(HTTPException) as exc:
            await call()
        assert exc.value.status_code == 409
    row = await (await db_conn.execute(
        "SELECT title FROM global_facts WHERE id='other-scheme-fact'")).fetchone()
    assert row["title"] == "其它方案事实"
