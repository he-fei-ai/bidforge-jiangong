"""目录 AI 自然语言调整端点回归测试（2026-09-22）。

引入背景：对齐参考软件 `outlineAdjustmentTask.cjs`——支持用户对已有目录提自然语言
要求由 AI 定向修改。本软件此前只能手动编辑树或整表重新生成，缺此轻量入口。

覆盖：
1. 入参校验（缺 instruction 400 / 方案不存在 404 / 无目录 400）；
2. 成功路径：返回规范化新树，保留真实 DB id 到 __original_id，剔除幻觉 id；
3. 深度裁剪兜底（AI 回传四级 → 裁到三级）；
4. AI 调用异常 → 502；
5. 校验函数 _adjust_outline_validate_fn 单元（反例回归）。
"""
import uuid

import pytest
from fastapi import HTTPException

import app.routers.sections as sec


async def _seed_scheme(db) -> str:
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status) VALUES(?,?,?,?)",
        (sid, pid, "深基坑专项方案", "目录已确认"))
    await db.commit()
    return sid


async def _seed_section(db, sid, sec_id, title, level=1, parent_id="", sort=0):
    await db.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, title, level, status,"
        " word_count, word_budget, review_status, sort_order)"
        " VALUES (?,?,?,?,?, 'generated', 100, 1500, 'pending', ?)",
        (sec_id, sid, parent_id, title, level, sort))
    await db.commit()


def _patch_ai(monkeypatch, obj):
    async def fake(messages, validate_fn, **kwargs):
        issues = validate_fn(obj)
        if issues:
            raise ValueError("校验不通过: " + ";".join(issues))
        return obj, ""
    monkeypatch.setattr(sec, "collect_json_response", fake)


@pytest.mark.asyncio
async def test_adjust_outline_requires_instruction(db_conn):
    with pytest.raises(HTTPException) as ei:
        await sec.adjust_outline("any", {"instruction": "   "}, db=db_conn)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_adjust_outline_scheme_not_found(db_conn):
    with pytest.raises(HTTPException) as ei:
        await sec.adjust_outline("ghost", {"instruction": "改标题"}, db=db_conn)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_adjust_outline_empty_tree(db_conn):
    sid = await _seed_scheme(db_conn)
    with pytest.raises(HTTPException) as ei:
        await sec.adjust_outline(sid, {"instruction": "删掉第一章"}, db=db_conn)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_adjust_outline_preserves_ids_and_drops_hallucination(db_conn, monkeypatch):
    sid = await _seed_scheme(db_conn)
    await _seed_section(db_conn, sid, "R1", "工程概况", level=1, sort=0)
    await _seed_section(db_conn, sid, "C1", "项目简介", level=2, parent_id="R1", sort=0)
    await _seed_section(db_conn, sid, "R2", "施工工艺", level=1, sort=1)

    ai_obj = {
        "outline": [
            {"id": "R1", "title": "工程概况（改）",
             "children": [{"id": "C1", "title": "项目简介"}]},
            {"title": "新增：监测方案", "children": []},
            {"id": "FAKE_ID", "title": "幻觉 id 的章"},
        ],
        "summary": "改了第一章标题并新增监测章",
    }
    _patch_ai(monkeypatch, ai_obj)

    res = await sec.adjust_outline(sid, {"instruction": "把第一章标题改一下，新增监测方案章"}, db=db_conn)
    assert res["ok"] is True
    assert res["summary"] == "改了第一章标题并新增监测章"
    outline = res["outline"]
    by_title = {n["title"]: n for n in outline}
    # 真实 id → __original_id 保住正文关联
    assert by_title["工程概况（改）"]["__original_id"] == "R1"
    assert by_title["工程概况（改）"]["children"][0]["__original_id"] == "C1"
    # 新增节点不带 __original_id（save-outline 会创建）
    assert "__original_id" not in by_title["新增：监测方案"]
    # 幻觉 id 节点：id 被清除、按新增处理（无 __original_id）
    fake_node = by_title["幻觉 id 的章"]
    assert "__original_id" not in fake_node
    assert fake_node["id"] != "FAKE_ID"  # 已被重排为编号


@pytest.mark.asyncio
async def test_adjust_outline_clamps_depth_to_three(db_conn, monkeypatch):
    sid = await _seed_scheme(db_conn)
    await _seed_section(db_conn, sid, "R1", "工程概况", level=1, sort=0)
    deep = {
        "outline": [{
            "id": "R1", "title": "工程概况", "children": [{
                "title": "二级", "children": [{
                    "title": "三级", "children": [{"title": "四级应被裁掉"}]}]}]
        }],
        "summary": "x",
    }
    _patch_ai(monkeypatch, deep)
    res = await sec.adjust_outline(sid, {"instruction": "扩展子节"}, db=db_conn)
    node = res["outline"][0]
    l2 = node["children"][0]
    l3 = l2["children"][0]
    assert l3["children"] == []  # 四级被裁剪并入三级 description


@pytest.mark.asyncio
async def test_adjust_outline_ai_failure_maps_502(db_conn, monkeypatch):
    sid = await _seed_scheme(db_conn)
    await _seed_section(db_conn, sid, "R1", "工程概况", level=1, sort=0)

    async def boom(messages, validate_fn, **kwargs):
        raise RuntimeError("provider down")
    monkeypatch.setattr(sec, "collect_json_response", boom)

    with pytest.raises(HTTPException) as ei:
        await sec.adjust_outline(sid, {"instruction": "改标题"}, db=db_conn)
    assert ei.value.status_code == 502


def test_adjust_outline_validate_fn_unit():
    assert sec._adjust_outline_validate_fn({"outline": []}) != []
    assert sec._adjust_outline_validate_fn({}) != []
    assert sec._adjust_outline_validate_fn({"outline": ["字符串节点"]}) != []
    assert sec._adjust_outline_validate_fn(
        {"outline": [{"title": "  "}]}
    ) != []
    assert sec._adjust_outline_validate_fn({"outline": [{"title": "合格章"}]}) == []
