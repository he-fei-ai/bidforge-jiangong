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

import app.routers.sections as sec
import pytest
from fastapi import HTTPException


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


# ============================================================
# 2026-09-26 BUG 修复：AI 调整目录后保存必须保住已生成正文
# ------------------------------------------------------------
# 缺陷链（前后端各有一环，缺一即复发）：
#   后端 _save_outline_to_db._preserve_original_ids 无条件执行
#       `node["__original_id"] = node.get("id", "")`，把 /adjust-outline
#       回传的真实 DB 主键冲掉、退化为用 renumber 覆写后的**展示编号**
#       （"1"/"1.1"）当主键 → 匹配不到已有 section → is_new 全为 True
#       → 整表重建 → **已生成正文全部丢失**。
#   前端 outlineToTreeNode / treeToOutline 同样丢弃 __original_id。
# ============================================================


class TestAdjustThenSavePreservesContent:
    async def test_preserve_original_ids_not_overwritten(self, db_conn):
        """已有 __original_id 时不得被 id 覆盖（后端核心修复点）。"""
        sid = await _seed_scheme(db_conn)
        await _seed_section(db_conn, sid, "R1", "工程概况", level=1, sort=0)
        # 模拟前端提交：id=展示编号、__original_id=真实主键
        await sec._save_outline_to_db(
            db_conn, sid, [{"id": "1", "__original_id": "R1", "title": "工程概况",
                            "children": []}])
        cur = await db_conn.execute(
            "SELECT id, title FROM sections WHERE scheme_id=?", (sid,))
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == "R1", [dict(r) for r in rows]

    async def test_adjust_then_save_keeps_generated_content(self, db_conn, monkeypatch):
        """端到端：adjust-outline 返回树 save-outline 后，正文完整保留。"""
        sid = await _seed_scheme(db_conn)
        await _seed_section(db_conn, sid, "R1", "工程概况", level=1, sort=0)
        await _seed_section(db_conn, sid, "R2", "施工工艺", level=1, sort=1)
        await db_conn.execute(
            "UPDATE sections SET content='工程概况正文,不可丢失' WHERE id='R1'")
        await db_conn.execute(
            "UPDATE sections SET content='施工工艺正文,不可丢失' WHERE id='R2'")
        await db_conn.commit()

        _patch_ai(monkeypatch, {
            "outline": [
                {"id": "R1", "title": "工程概况（已改名）", "children": []},
                {"id": "R2", "title": "施工工艺", "children": []},
            ],
            "summary": "仅改第一章标题",
        })
        res = await sec.adjust_outline(sid, {"instruction": "改第一章标题"}, db=db_conn)
        await sec._save_outline_to_db(db_conn, sid, res["outline"])

        cur = await db_conn.execute(
            "SELECT id, title, content FROM sections WHERE scheme_id=? ORDER BY sort_order",
            (sid,))
        rows = {r["id"]: dict(r) for r in await cur.fetchall()}
        assert set(rows) == {"R1", "R2"}, rows
        assert rows["R1"]["title"] == "工程概况（已改名）"
        assert rows["R1"]["content"] == "工程概况正文,不可丢失"
        assert rows["R2"]["content"] == "施工工艺正文,不可丢失"

    async def test_new_chapter_from_adjust_still_creates(self, db_conn, monkeypatch):
        """AI 新增的章（无 __original_id）仍应新建，且不影响已有章节正文。"""
        sid = await _seed_scheme(db_conn)
        await _seed_section(db_conn, sid, "R1", "工程概况", level=1, sort=0)
        await db_conn.execute("UPDATE sections SET content='原有正文' WHERE id='R1'")
        await db_conn.commit()

        _patch_ai(monkeypatch, {
            "outline": [
                {"id": "R1", "title": "工程概况", "children": []},
                {"title": "新增：监测方案", "children": []},
            ],
            "summary": "新增一章",
        })
        res = await sec.adjust_outline(sid, {"instruction": "新增监测方案章"}, db=db_conn)
        await sec._save_outline_to_db(db_conn, sid, res["outline"])

        cur = await db_conn.execute(
            "SELECT id, title, content FROM sections WHERE scheme_id=? ORDER BY sort_order",
            (sid,))
        rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 2
        assert rows[0]["content"] == "原有正文"
        assert any("监测方案" in r["title"] for r in rows)

    async def test_id_only_submit_backward_compatible(self, db_conn):
        """旧前端（只提交 id、不带 __original_id）行为不变：按 id 匹配已有章节。"""
        sid = await _seed_scheme(db_conn)
        await _seed_section(db_conn, sid, "R1", "工程概况", level=1, sort=0)
        await db_conn.execute("UPDATE sections SET content='原有正文' WHERE id='R1'")
        await db_conn.commit()

        await sec._save_outline_to_db(
            db_conn, sid, [{"id": "R1", "title": "工程概况", "children": []}])
        cur = await db_conn.execute(
            "SELECT id, content FROM sections WHERE scheme_id=?", (sid,))
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == "R1"
        assert rows[0]["content"] == "原有正文"
