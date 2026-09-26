"""全局事实「条目级更新 + 分类白名单端点」回归测试（2026-09-20 补齐）。

锁定两处修复：
- BUG D：`_apply_item_updates` 此前对任何提交都无条件清除 has_conflict，
  而前端单条编辑弹窗总是全量提交 value —— 导致「只改分类 / 只改名 /
  切换模拟值」等未真正改值的操作静默吞掉尚未裁决的矛盾。修复后仅在
  值真正变化（strip 后不同且 normalize_key 不同）时才清冲突标记，口径与
  persist_extraction 的冲突登记一致。
- 技术债 E：新增 `GET /global-facts/categories`，以 CATEGORY_TITLES 为
  单一事实源向前端下发分类白名单，消除前后端分类双份维护。

测试直接调用路由函数（与既有路由测试风格一致），避免 TestClient 跨事件
循环持有 aiosqlite 连接导致的偶发失败。
"""
import uuid

import pytest

import app.db as _appdb
import app.routers.global_facts as gf
from app.db import get_conn, init_db
import app.services.facts_extractor as fe


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "item-upd.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid
    await db.execute("DELETE FROM global_facts WHERE scheme_id=?", (sid,))
    await db.execute("DELETE FROM schemes WHERE id=?", (sid,))
    await db.execute("DELETE FROM projects WHERE id=?", (pid,))
    await db.commit()


async def _seed_conflict_fact(db, pid, sid, *, value="365"):
    """插入一条带未裁决矛盾的事实，返回其行 id。"""
    fid = uuid.uuid4().hex
    content = gf._build_fact_content("合同工期", value, False)
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
        "group_title, title, content, category, source_ref, is_simulated, "
        "confidence, is_resolved, has_conflict, conflict_keys, fact_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, pid, sid, "g1", "工期安排", "合同工期", content, "schedule",
         '[{"file":"招标文件","quote":"工期365天"}]', 0, 0.9, 1, 1,
         "k_a|k_b", "合同工期"))
    await db.commit()
    return fid


async def _row(db, fid):
    cur = await db.execute(
        "SELECT title, content, category, has_conflict, conflict_keys, fact_key "
        "FROM global_facts WHERE id=?", (fid,))
    r = await cur.fetchone()
    return dict(r)


# ---------------------------------------------------------------------------
# _apply_item_updates：仅在值真正变化时清除矛盾标记
# ---------------------------------------------------------------------------

async def test_value_unchanged_keeps_conflict(ctx):
    """只改分类、value 原样回填 → 矛盾标记必须保留（旧实现会被静默清除）。"""
    db, pid, sid = ctx
    fid = await _seed_conflict_fact(db, pid, sid, value="365")
    n, _ = await gf._apply_item_updates(
        db, [{"fact_id": fid, "value": "365", "category": "basic"}])
    assert n == 1
    r = await _row(db, fid)
    assert r["has_conflict"] == 1, "值未变不应清矛盾"
    assert r["conflict_keys"] == "k_a|k_b", "值未变不应清 conflict_keys"
    assert r["category"] == "basic", "分类应被更新"


async def test_value_whitespace_only_keeps_conflict(ctx):
    """仅首尾空白差异视为未改值 → 矛盾标记保留。"""
    db, pid, sid = ctx
    fid = await _seed_conflict_fact(db, pid, sid, value="365")
    await gf._apply_item_updates(db, [{"fact_id": fid, "value": " 365 "}])
    r = await _row(db, fid)
    assert r["has_conflict"] == 1, "首尾空白差异不应被当作改值而清矛盾"


async def test_value_changed_clears_conflict(ctx):
    """人工改值即视为裁决 → 清除 has_conflict 与 conflict_keys。"""
    db, pid, sid = ctx
    fid = await _seed_conflict_fact(db, pid, sid, value="365")
    await gf._apply_item_updates(db, [{"fact_id": fid, "value": "400"}])
    r = await _row(db, fid)
    assert r["has_conflict"] == 0, "改值应清矛盾"
    assert r["conflict_keys"] == ""
    assert "400" in r["content"]


async def test_rename_only_keeps_conflict(ctx):
    """只改名不提交 value → 矛盾标记保留，fact_key 随名重算。"""
    db, pid, sid = ctx
    fid = await _seed_conflict_fact(db, pid, sid, value="365")
    await gf._apply_item_updates(db, [{"fact_id": fid, "name": "总工期"}])
    r = await _row(db, fid)
    assert r["has_conflict"] == 1, "改名不应吞掉矛盾"
    assert r["title"] == "总工期"
    assert r["fact_key"] == fe.normalize_key("总工期")


# ---------------------------------------------------------------------------
# GET /global-facts/categories：CATEGORY_TITLES 单一事实源
# ---------------------------------------------------------------------------

async def test_list_fact_categories_matches_source():
    res = await gf.list_fact_categories()
    cats = res["categories"]
    assert isinstance(cats, list) and cats
    assert len(cats) == len(fe.CATEGORY_TITLES), "端点应完整覆盖 CATEGORY_TITLES"
    # 顺序与 value/label 均与单一事实源一致
    assert [c["value"] for c in cats] == list(fe.CATEGORY_TITLES.keys())
    assert all(c["label"] == fe.CATEGORY_TITLES[c["value"]] for c in cats)


# ---------------------------------------------------------------------------
# update_fact：多行判定收紧（2026-09-21 回归锁）
#   旧判定 `count("-") >= 1` 把含连字符的普通多行文本（如日期区间
#   "2024-2025"）误判为 Markdown 列表 → 走「分组重建」（DELETE+INSERT，
#   行 id 全换），前端持有旧行 id 的后续编辑会 404。
#   收紧后：只有存在以列表标记（-/*/•）开头的非空行才转分组重建。
# ---------------------------------------------------------------------------

async def _seed_plain_fact(db, pid, sid):
    fid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
        "group_title, title, content, category, is_simulated, confidence, "
        "is_resolved, has_conflict, fact_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?)",
        (fid, pid, sid, "gp1", "工期安排", "实施周期",
         "实施周期为365天", "schedule", 0, 0.9, 1, fe.normalize_key("实施周期")))
    await db.commit()
    return fid


async def _group_rows(db, gid):
    cur = await db.execute(
        "SELECT id FROM global_facts WHERE group_id=?", (gid,))
    return [dict(r) for r in await cur.fetchall()]


async def test_multiline_value_with_hyphen_keeps_row_id(ctx):
    """含连字符的多行普通文本 → 行级原地更新，行 id 不变、不触发分组重建。"""
    db, pid, sid = ctx
    fid = await _seed_plain_fact(db, pid, sid)
    await gf.update_fact(
        fid,
        gf.FactGroupUpdate(
            id=fid,
            content="实施周期为365天\n（服务期覆盖2024-2025年度）"),
        db=db)
    rows = await _group_rows(db, "gp1")
    assert [r["id"] for r in rows] == [fid], "含连字符的多行文本不得触发分组重建"
    cur = await db.execute("SELECT content FROM global_facts WHERE id=?", (fid,))
    row = dict(await cur.fetchone())
    assert "2024-2025" in row["content"], "多行文本必须完整落库（不得只留首行）"


async def test_real_markdown_list_still_rebuilds_group(ctx):
    """真正的多行 Markdown 列表仍走分组重建（旧行为不回退）。"""
    db, pid, sid = ctx
    fid = await _seed_plain_fact(db, pid, sid)
    await gf.update_fact(
        fid,
        gf.FactGroupUpdate(
            id=fid,
            title="工期安排",
            content="- **总工期**: 365 日历天\n- **试运行期**: 30 日历天"),
        db=db)
    rows = await _group_rows(db, "gp1")
    assert len(rows) == 2, "多行列表必须重建为多行（防静默丢行）"
    assert fid not in [r["id"] for r in rows], "旧行应被重建替换"


# ---------------------------------------------------------------------------
# update_fact 模式 1（行级）：title / content 同步口径（2026-09-21 回归锁）
#   旧实现把 fields 裸写进 SQL，三处失同步：
#   ① 改 title 只写 title 列，content 里的「**旧名**」与 fact_key 都不跟着改
#     → 列表接口按 content 回解，界面永远显示旧名；
#   ② 改 content 为模拟值文本，is_simulated / is_resolved 不动
#     → 模拟值闸门（is_simulated=1 ⟹ is_resolved=0）失效；
#   ③ 改值不清 has_conflict（与分组重建 / 条目级口径漂移）。
#   修复后：结构化 title / content 统一走 _apply_item_updates。
# ---------------------------------------------------------------------------

async def _seed_structured_fact(db, pid, sid, *, name="合同工期", value="365"):
    fid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
        "group_title, title, content, category, is_simulated, confidence, "
        "is_resolved, has_conflict, conflict_keys, fact_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
        (fid, pid, sid, "g1", "工期安排", name,
         gf._build_fact_content(name, value, False), "schedule",
         0, 0.9, 1, "", fe.normalize_key(name)))
    await db.commit()
    return fid


async def _full_row(db, fid):
    cur = await db.execute(
        "SELECT title, content, category, fact_key, is_simulated, is_resolved, "
        "has_conflict, conflict_keys FROM global_facts WHERE id=?", (fid,))
    return dict(await cur.fetchone())


async def _group_content(db, gid):
    cur = await db.execute(
        "SELECT id, title, content FROM global_facts WHERE group_id=? "
        "ORDER BY title", (gid,))
    return [dict(r) for r in await cur.fetchall()]


async def test_row_level_rename_syncs_content_and_fact_key(ctx):
    """行级改名 → content 内的加粗名与 fact_key 必须同步（旧实现两处滞留）。"""
    db, pid, sid = ctx
    fid = await _seed_structured_fact(db, pid, sid)
    await gf.update_fact(fid, gf.FactGroupUpdate(id=fid, title="总工期"), db=db)
    r = await _full_row(db, fid)
    assert r["title"] == "总工期"
    assert "**总工期**" in r["content"], "content 内的名称必须跟着改（列表回解依赖它）"
    assert "**合同工期**" not in r["content"], "旧名不得残留在 content"
    assert r["fact_key"] == fe.normalize_key("总工期"), "fact_key 必须随名重算"


async def test_row_level_simulated_content_enforces_gate(ctx):
    """行级把 content 改成模拟值 → is_simulated=1 且 is_resolved 回落为 0（闸门）。"""
    db, pid, sid = ctx
    fid = await _seed_structured_fact(db, pid, sid, value="365")
    await gf.update_fact(
        fid,
        gf.FactGroupUpdate(id=fid,
                           content=f"- **合同工期**: 300{fe.SIMULATED_MARKER}"),
        db=db)
    r = await _full_row(db, fid)
    assert r["is_simulated"] == 1, "模拟值标记必须回写到 is_simulated 列"
    assert r["is_resolved"] == 0, "模拟值必须回到待审核，不得直接越过注入闸门"
    assert "300" in r["content"]


async def test_row_level_clear_simulated_marker_releases_gate(ctx):
    """行级去掉模拟值标记 → is_simulated 归 0（闸门解除，且已确认状态不被回退）。"""
    db, pid, sid = ctx
    fid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
        "group_title, title, content, category, is_simulated, confidence, "
        "is_resolved, has_conflict, conflict_keys, fact_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
        (fid, pid, sid, "g1", "工期安排", "合同工期",
         gf._build_fact_content("合同工期", "300", True), "schedule",
         1, 0.3, 0, "", fe.normalize_key("合同工期")))
    await db.commit()
    await gf.update_fact(
        fid, gf.FactGroupUpdate(id=fid, content="- **合同工期**: 365"), db=db)
    r = await _full_row(db, fid)
    assert r["is_simulated"] == 0, "去掉标记后不得残留模拟值"
    assert "模拟值" not in r["content"]
    assert r["is_resolved"] == 0, "本就不确认的事实不应因去标记而被误置为已确认"


async def test_row_level_value_change_clears_conflict(ctx):
    """行级改值即视为裁决 → 清 has_conflict 与 conflict_keys（与条目级同口径）。"""
    db, pid, sid = ctx
    fid = await _seed_conflict_fact(db, pid, sid, value="365")
    await gf.update_fact(
        fid, gf.FactGroupUpdate(id=fid, content="- **合同工期**: 400"), db=db)
    r = await _full_row(db, fid)
    assert r["has_conflict"] == 0 and r["conflict_keys"] == ""
    assert "400" in r["content"]


async def test_row_level_rename_keeps_conflict(ctx):
    """行级只改名（无 value）→ 不得吞掉未裁决矛盾。"""
    db, pid, sid = ctx
    fid = await _seed_conflict_fact(db, pid, sid, value="365")
    await gf.update_fact(fid, gf.FactGroupUpdate(id=fid, title="总工期"), db=db)
    r = await _full_row(db, fid)
    assert r["has_conflict"] == 1 and r["conflict_keys"] == "k_a|k_b"


async def test_row_level_unknown_field_is_ignored(ctx):
    """未知字段不得拼接进 SQL（旧裸写字段名存在越权写列 / 注入面）。"""
    db, pid, sid = ctx
    fid = await _seed_structured_fact(db, pid, sid)
    await gf.update_fact(
        fid,
        gf.FactGroupUpdate(id=fid, category="basic", **{"hack": "x'};DROP TABLE global_facts;--"}),
        db=db)
    r = await _full_row(db, fid)
    assert r["category"] == "basic"


async def test_plus_and_dot_bullets_rebuild_group(ctx):
    """「+ / ·」列表符与「- / *」同等对待：多行分组编辑不得被塞成单行丢行。"""
    db, pid, sid = ctx
    await _seed_plain_fact(db, pid, sid)
    for bullet in ("+", "·"):
        # 用 group_id（gp1）而非行 id：分组重建后行 id 会换成新 uuid，
        # 仍持旧行 id 的后续编辑会 404；按分组编辑正是前端「编辑分组」的口径。
        await gf.update_fact(
            "gp1",
            gf.FactGroupUpdate(
                id="gp1", title="工期安排",
                content=f"{bullet} **总工期**: 365 日历天\n{bullet} **试运行期**: 30 日历天"),
            db=db)
        rows = await _group_content(db, "gp1")
        assert len(rows) == 2, f"{bullet} 开头的多行列表必须重建为多行"
        content = "".join(r["content"] for r in rows)
        assert "365" in content and "30" in content, "不得丢失任一行"
        assert all(not r["content"].lstrip().startswith(bullet) for r in rows), \
            "列表符必须在入库前剥离，不得混进 content"
