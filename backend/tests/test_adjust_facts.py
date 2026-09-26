"""全局事实 AI 自然语言调整端点回归测试（2026-09-22）。

引入背景：对齐参考软件 `globalFactsAdjustmentTask.cjs`——用自然语言批量修改已有
事实（"把涉及的年份统一改成 2026""新增一条：项目经理=张伟"）。本软件此前只能逐条
CRUD 或全量重提取。

与参考软件的取舍：本软件事实库带溯源/矛盾/模拟值闸门等不变式，AI 不重写整库，
而是产出**按 fact_id 定位的最小操作计划**；应用时全走既有写路径保住不变式。

覆盖：
1. 校验函数 _validate_adjust_ops 单元（幻觉 id 丢弃、空 add 丢弃、非法 category→other、
   无实质变更的 update 丢弃）；
2. 入参校验（缺 instruction 400 / 无作用域 400 / 无事实 400）；
3. 默认 apply=False 只返回计划、不写库；
4. apply=True：update 改值走 _apply_item_updates、delete 行级删除、add 单行插入。
"""
import json
import uuid

import pytest
from fastapi import HTTPException

import app.routers.global_facts as gf
from app.services.facts_extractor import extract_value_from_markdown_line


async def _seed_scheme(db) -> tuple[str, str]:
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    return pid, sid


async def _seed_fact(db, pid, sid, fid, name, value, category="tech_param",
                     is_simulated=0):
    content = f"- **{name}**: {value}"
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, group_title,"
        " title, content, category, source_ref, is_simulated, confidence, is_resolved,"
        " has_conflict, conflict_keys, fact_key)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, pid, sid, "g_" + fid, name, name, content, category,
         json.dumps([{"file": "招标文件", "quote": "x"}], ensure_ascii=False),
         is_simulated, 0.9, 0 if is_simulated else 1, 0, "", name))
    await db.commit()


def _patch_ai(monkeypatch, obj):
    async def fake(messages, validate_fn, **kwargs):
        issues = validate_fn(obj)
        if issues:
            raise ValueError("校验不通过: " + ";".join(issues))
        return obj, ""
    monkeypatch.setattr(gf, "collect_json_response", fake)


# ---------------------------------------------------------------------------
# 1. 操作计划校验单元
# ---------------------------------------------------------------------------

def test_validate_adjust_ops_unit():
    valid = {"f1", "f2"}
    obj = {
        "operations": [
            {"op": "update", "fact_id": "f1", "value": "5.8m"},
            {"op": "update", "fact_id": "ghost", "value": "x"},       # 幻觉 id → 丢
            {"op": "update", "fact_id": "f2"},                          # 无变更 → 丢
            {"op": "update", "fact_id": "f1", "value": "y", "category": "badcat"},
            {"op": "add", "name": "项目经理", "value": "张伟", "category": "personnel"},
            {"op": "add", "name": "空值", "value": "   "},              # 空 value → 丢
            {"op": "delete", "fact_id": "f2"},
            {"op": "delete", "fact_id": "ghost"},                       # 幻觉 id → 丢
        ],
        "summary": "改了两处",
    }
    ops, summary = gf._validate_adjust_ops(obj, valid)
    assert summary == "改了两处"
    # ghost 从未出现
    assert all(o.get("fact_id") != "ghost" for o in ops)
    # 无变更 update 被丢
    assert not any(o["op"] == "update" and o["fact_id"] == "f2" and "value" not in o
                   for o in ops)
    # badcat 归一到 other
    bad = [o for o in ops if o["op"] == "update" and o.get("value") == "y"]
    assert bad and bad[0]["category"] == "other"
    # 合法 add 保留
    adds = [o for o in ops if o["op"] == "add"]
    assert len(adds) == 1 and adds[0]["category"] == "personnel"
    # 合法 delete 保留（仅 f2）
    dels = [o for o in ops if o["op"] == "delete"]
    assert len(dels) == 1 and dels[0]["fact_id"] == "f2"


def test_validate_adjust_ops_non_list():
    ops, summary = gf._validate_adjust_ops({"summary": "s"}, {"f1"})
    assert ops == [] and summary == "s"
    ops2, _ = gf._validate_adjust_ops({}, {"f1"})
    assert ops2 == []


# ---------------------------------------------------------------------------
# 2. 入参校验
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adjust_facts_requires_instruction(db_conn):
    with pytest.raises(HTTPException) as ei:
        await gf.adjust_facts({"instruction": "  "}, db=db_conn)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_adjust_facts_no_scope_400(db_conn):
    with pytest.raises(HTTPException) as ei:
        await gf.adjust_facts({"instruction": "改值", "scheme_id": "", "project_id": ""},
                              db=db_conn)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_adjust_facts_empty_facts_400(db_conn):
    _pid, sid = await _seed_scheme(db_conn)
    with pytest.raises(HTTPException) as ei:
        await gf.adjust_facts({"instruction": "改值", "scheme_id": sid}, db=db_conn)
    assert ei.value.status_code == 400


# ---------------------------------------------------------------------------
# 3. 默认只返回计划，不写库
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adjust_facts_preview_does_not_write(db_conn, monkeypatch):
    pid, sid = await _seed_scheme(db_conn)
    await _seed_fact(db_conn, pid, sid, "f1", "基坑深度", "5.6m")

    _patch_ai(monkeypatch, {
        "operations": [{"op": "update", "fact_id": "f1", "value": "5.8m"}],
        "summary": "把基坑深度改为 5.8m",
    })
    res = await gf.adjust_facts(
        {"instruction": "基坑深度改成5.8m", "scheme_id": sid}, db=db_conn)
    assert res["applied"] is None
    assert len(res["operations"]) == 1
    # 库中值未变
    cur = await db_conn.execute("SELECT content FROM global_facts WHERE id='f1'")
    row = await cur.fetchone()
    assert extract_value_from_markdown_line(row["content"])[1] == "5.6m"


# ---------------------------------------------------------------------------
# 4. apply=True 落库
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adjust_facts_apply_update(db_conn, monkeypatch):
    pid, sid = await _seed_scheme(db_conn)
    await _seed_fact(db_conn, pid, sid, "f1", "基坑深度", "5.6m")
    _patch_ai(monkeypatch, {
        "operations": [{"op": "update", "fact_id": "f1", "value": "5.8m"}],
        "summary": "改值",
    })
    # apply=true + operations：直接应用用户确认过的计划，且不得再次调用 AI。
    async def should_not_call_ai(*args, **kwargs):
        raise AssertionError("已确认操作计划不应再次调用 AI")
    monkeypatch.setattr(gf, "collect_json_response", should_not_call_ai)
    res = await gf.adjust_facts({
        "instruction": "基坑深度改成5.8m", "scheme_id": sid, "apply": True,
        "operations": [{"op": "update", "fact_id": "f1", "value": "5.8m"}],
    }, db=db_conn)
    assert res["applied"]["updated"] == 1
    cur = await db_conn.execute("SELECT content FROM global_facts WHERE id='f1'")
    row = await cur.fetchone()
    assert extract_value_from_markdown_line(row["content"])[1] == "5.8m"


@pytest.mark.asyncio
async def test_adjust_facts_apply_add_and_delete(db_conn, monkeypatch):
    pid, sid = await _seed_scheme(db_conn)
    await _seed_fact(db_conn, pid, sid, "f1", "旧事实", "1", category="other")
    _patch_ai(monkeypatch, {
        "operations": [
            {"op": "delete", "fact_id": "f1"},
            {"op": "add", "name": "项目经理", "value": "张伟", "category": "personnel"},
        ],
        "summary": "删旧增新",
    })
    res = await gf.adjust_facts(
        {"instruction": "删掉旧事实并新增项目经理", "scheme_id": sid, "apply": True},
        db=db_conn)
    assert res["applied"]["deleted"] == 1
    assert res["applied"]["added"] == 1
    cur = await db_conn.execute("SELECT COUNT(*) AS c FROM global_facts WHERE scheme_id=?",
                                (sid,))
    assert (await cur.fetchone())["c"] == 1
    cur2 = await db_conn.execute(
        "SELECT title, content, category, is_simulated, is_resolved FROM global_facts"
        " WHERE scheme_id=?", (sid,))
    row = dict(await cur2.fetchone())
    assert row["title"] == "项目经理"
    assert row["category"] == "personnel"
    assert row["is_simulated"] == 0 and row["is_resolved"] == 1


@pytest.mark.asyncio
async def test_adjust_facts_ai_failure_maps_502(db_conn, monkeypatch):
    pid, sid = await _seed_scheme(db_conn)
    await _seed_fact(db_conn, pid, sid, "f1", "基坑深度", "5.6m")

    async def boom(messages, validate_fn, **kwargs):
        raise RuntimeError("provider down")
    monkeypatch.setattr(gf, "collect_json_response", boom)

    with pytest.raises(HTTPException) as ei:
        await gf.adjust_facts({"instruction": "改值", "scheme_id": sid}, db=db_conn)
    assert ei.value.status_code == 502
