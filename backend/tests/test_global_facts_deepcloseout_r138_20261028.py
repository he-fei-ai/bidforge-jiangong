"""R138 · 全局事实模块深度收口回归测试（2026-10-28）

覆盖本轮六处缺陷（每处都先修了「静默失效」，护栏断的是**健康路径**而非
「不抛异常」—— 承接 R137 教训⑧）：

  A · D1  ``PATCH /{fact_id}`` 只传 ``category`` 时绕过分类白名单与维度重派生
  B · D6  ``title`` + **非结构化**单行 ``content`` 同时提交时 content 被静默丢弃
  C · D2  分组重建未传 ``title`` 时，用户自定义分组名被分类默认名覆盖
  D · D3  空条目 / 空载荷落库成 ``- ****: `` 垃圾事实（假成功）
  E · D4  读 / 缓存失效路径零 R13 判空（``db.execute`` 返回 None → AttributeError 500）
  F · D5  ``limit=0`` 时丢弃 ``offset``，却在 ``pagination.offset`` 回显

风格与 ``test_global_facts_item_updates.py`` 一致：直接调用路由函数，避免
TestClient 跨事件循环持有 aiosqlite 连接导致的偶发失败。
"""
import uuid

import app.db as _appdb
import app.routers.global_facts as gf
import pytest
from app.db import get_conn, init_db
from app.models import FactGroupIn, FactGroupUpdate
from fastapi import HTTPException


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "r138-gf.sqlite"
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
    # ✅ R138（进程不退出）：aiosqlite 连接持有后台工作线程，漏关会让 pytest
    #    在**全部用例通过之后**仍不退出——实测「923 passed」已打印、终端却
    #    一直不返回 CLUSTER_EXIT，A/B 脚本的 subprocess.run 同步阻塞为此卡死
    #    （R137 教训⑦的「慢/挂」形态）。与 conftest.db_conn 的 teardown 同口径。
    await db.close()


async def _seed(db, pid, sid, *, gid="g1", gtitle="分组", name="事实",
                value="值", category="other", chapter="", fact_attr="",
                fid=None):
    """插入一条最小事实行，返回行 id（列默认值由 schema 提供）。"""
    fid = fid or uuid.uuid4().hex
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
        "group_title, title, content, category, is_simulated, confidence, "
        "is_resolved, has_conflict, conflict_keys, fact_key, chapter, fact_attr) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, pid, sid, gid, gtitle, name, gf._build_fact_content(name, value, False),
         category, 0, 1.0, 1, 0, "", gf.normalize_key(name), chapter, fact_attr))
    await db.commit()
    return fid


async def _row(db, fid):
    cur = await db.execute(
        "SELECT title, content, category, chapter, fact_attr, group_title, "
        "is_resolved, is_simulated FROM global_facts WHERE id=?", (fid,))
    return dict(await cur.fetchone())


async def _count(db, sid):
    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM global_facts WHERE scheme_id=?", (sid,))
    return int((await cur.fetchone())["n"])


# ===========================================================================
# A · D1：category-only PATCH 必须过白名单 + 重派生 chapter
# ===========================================================================
async def test_a1_category_only_patch_rederives_chapter(ctx):
    """只传 category → chapter 必须按新分类重派生（旧实现原地不动）。"""
    db, pid, sid = ctx
    # 「班组长」不命中任何 CHAPTER_TEXT_RULES 关键词 → chapter 只能来自 category
    fid = await _seed(db, pid, sid, name="班组长", value="张三",
                      category="other", chapter="", fact_attr="qualitative")
    await gf.update_fact(fid, FactGroupUpdate(id=fid, category="personnel"),
                         "", db=db)
    r = await _row(db, fid)
    assert r["category"] == "personnel"
    assert r["chapter"] == "personnel", "分类是派生输入，改了必须重派生章节归属"


async def test_a2_category_only_patch_normalizes_unknown_category(ctx):
    """未知分类必须经白名单回落 other（旧实现原样写入，事实从章节视图消失）。"""
    db, pid, sid = ctx
    fid = await _seed(db, pid, sid, name="班组长", value="张三",
                      category="personnel", chapter="personnel")
    await gf.update_fact(fid, FactGroupUpdate(id=fid, category="bogus"),
                         "", db=db)
    r = await _row(db, fid)
    assert r["category"] == "other", "未知分类必须归一为 other，不能原样落库"


async def test_a3_empty_category_normalizes_to_other(ctx):
    """category 传空串 → 按白名单口径归一 other（不写空串）。"""
    db, pid, sid = ctx
    fid = await _seed(db, pid, sid, name="班组长", value="张三",
                      category="personnel", chapter="personnel")
    await gf.update_fact(fid, FactGroupUpdate(id=fid, category=""), "", db=db)
    assert (await _row(db, fid))["category"] == "other"


async def test_a4_rederived_chapter_visible_on_read_path(ctx):
    """读路径（列表接口）必须看到重派生后的 chapter —— 这才是用户可见的症状。"""
    db, pid, sid = ctx
    fid = await _seed(db, pid, sid, name="班组长", value="张三",
                      category="other", chapter="")
    await gf.update_fact(fid, FactGroupUpdate(id=fid, category="personnel"),
                         "", db=db)
    out = await gf.list_facts(scheme_id=sid, project_id="", limit=0, offset=0, db=db)
    item = next(it for g in out["groups"] for it in g["items"] if it["fact_id"] == fid)
    assert item["chapter"] == "personnel"
    # 章节统计随之同步：本方案仅此一条事实，重派生后不应落「未分类」
    assert out["stats"]["by_chapter"]["totals"]["uncategorized"] == 0


# ===========================================================================
# B · D6：非结构化 content 不得被「改名」路径吞掉
# ===========================================================================
async def test_b1_plain_content_with_title_is_not_dropped(ctx):
    """title + 非结构化单行 content 同时提交 → 正文必须落库（旧实现被覆盖回旧值）。"""
    db, pid, sid = ctx
    fid = await _seed(db, pid, sid, name="基坑深度", value="6.0m")
    await gf.update_fact(
        fid, FactGroupUpdate(id=fid, title="基坑开挖深度",
                             content="基坑深度约 12.5 米"), "", db=db)
    r = await _row(db, fid)
    assert r["title"] == "基坑开挖深度"
    assert r["content"] == "基坑深度约 12.5 米", "用户提交的正文不能被静默丢弃"


async def test_b2_plain_content_only_still_raw_updates(ctx):
    """仅传非结构化 content（无 title / category）→ 分支行为逐字不变。"""
    db, pid, sid = ctx
    fid = await _seed(db, pid, sid, name="备注", value="旧值")
    await gf.update_fact(fid, FactGroupUpdate(id=fid, content="新正文"), "", db=db)
    r = await _row(db, fid)
    assert r["content"] == "新正文"
    assert r["title"] == "备注", "只改正文不应改事实名"


# ===========================================================================
# C · D2：分组重建未传 title 时沿用自定义分组名
# ===========================================================================
async def test_c1_custom_group_title_survives_content_only_patch(ctx):
    """只提交 content 的分组编辑 → 自定义分组名必须保留。"""
    db, pid, sid = ctx
    await _seed(db, pid, sid, gid="gC1", gtitle="招标文件关键参数",
                name="A", value="1", category="basic")
    await _seed(db, pid, sid, gid="gC1", gtitle="招标文件关键参数",
                name="B", value="2", category="basic")
    await gf.update_fact(
        "gC1", FactGroupUpdate(id="gC1", content="- **A**: 10\n- **B**: 20"),
        "", db=db)
    cur = await db.execute(
        "SELECT group_title, content FROM global_facts WHERE scheme_id=?", (sid,))
    rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 2
    assert {r["group_title"] for r in rows} == {"招标文件关键参数"}, \
        "自定义分组名被分类默认名覆盖（D2）"
    assert all("10" in r["content"] or "20" in r["content"] for r in rows)


async def test_c2_default_group_title_still_follows_new_category(ctx):
    """旧分组名恰是分类默认名 → 改分类后仍跟随新分类（保留旧行为）。"""
    db, pid, sid = ctx
    old_default = gf.CATEGORY_TITLES["basic"]
    await _seed(db, pid, sid, gid="gC2", gtitle=old_default,
                name="验收人", value="李四", category="basic")
    await gf.update_fact(
        "gC2", FactGroupUpdate(id="gC2", category="acceptance",
                               content="- **验收人**: 李四"), "", db=db)
    cur = await db.execute(
        "SELECT group_title, category FROM global_facts WHERE scheme_id=?", (sid,))
    r = dict(await cur.fetchone())
    assert r["category"] == "acceptance"
    assert r["group_title"] == gf.CATEGORY_TITLES["acceptance"]


# ===========================================================================
# D · D3：空条目 / 空载荷不得落库成垃圾事实
# ===========================================================================
async def test_d1_blank_items_are_skipped_and_counted(ctx):
    """混合载荷：空条目跳过并计数，有效条目正常落库。"""
    db, pid, sid = ctx
    out = await gf.create_fact(FactGroupIn(
        title="人员", content="",
        items=[{"name": "", "value": ""},
               {"name": "项目经理", "value": "张伟"}]),
        sid, "", db=db)
    assert out["ok"] is True and out["inserted"] == 1
    assert out["skipped_invalid"] == 1
    assert await _count(db, sid) == 1


async def test_d2_all_blank_items_raise_400(ctx):
    """全空载荷 → 400，且一行都不落库（旧实现回 ok=true 并写 ``- ****: ``）。"""
    db, pid, sid = ctx
    with pytest.raises(HTTPException) as ei:
        await gf.create_fact(FactGroupIn(
            title="", content="", items=[{"name": "", "value": ""}]),
            sid, "", db=db)
    assert ei.value.status_code == 400
    assert await _count(db, sid) == 0


async def test_d3_blank_legacy_payload_raises_400(ctx):
    """旧版单条全空（title/content 皆空）→ 400，不写「未命名」空事实。"""
    db, pid, sid = ctx
    with pytest.raises(HTTPException) as ei:
        await gf.create_fact(FactGroupIn(title="", content=""), sid, "", db=db)
    assert ei.value.status_code == 400
    assert await _count(db, sid) == 0


async def test_d4_noise_lines_do_not_create_empty_facts(ctx):
    """多行 content 里的纯列表符噪音行不产生空事实（既有口径不回退）。"""
    db, pid, sid = ctx
    out = await gf.create_fact(
        FactGroupIn(title="说明", content="-\n- **A**: 1\n* *"), sid, "", db=db)
    assert out["inserted"] == 1
    assert await _count(db, sid) == 1


# ===========================================================================
# E · D4：读 / 缓存失效路径 R13 判空
# ===========================================================================
class _NoneDB:
    """execute 恒返回 None（R13 事故形态）。"""

    async def execute(self, *a, **k):
        return None


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)


class _FirstOkThenNoneDB:
    """第一次 execute 正常（供作用域反查），之后返回 None。"""

    def __init__(self):
        self.calls = 0

    async def execute(self, sql, params=None):
        self.calls += 1
        if self.calls == 1:
            return _Cur([{"project_id": "p"}])
        return None


async def test_e1_validate_fact_scope_none_cursor_is_503():
    with pytest.raises(HTTPException) as ei:
        await gf._validate_fact_scope(_NoneDB(), "", "p1")
    assert ei.value.status_code == 503


async def test_e2_assert_fact_in_scheme_scope_none_cursor_is_503():
    with pytest.raises(HTTPException) as ei:
        await gf._assert_fact_in_scheme_scope(_NoneDB(), "f1", "s1")
    assert ei.value.status_code == 503


async def test_e3_invalidate_scope_cache_none_cursor_is_503():
    """最关键的一处：它在「写已 commit」之后执行，绝不能裸抛 AttributeError。"""
    with pytest.raises(HTTPException) as ei:
        await gf._invalidate_fact_scope_cache(_NoneDB(), "", "p1")
    assert ei.value.status_code == 503


async def test_e4_list_facts_none_cursor_is_503():
    """作用域反查成功、列表查询游标为空 → 503（而非 AttributeError 500）。"""
    with pytest.raises(HTTPException) as ei:
        await gf.list_facts(scheme_id="s1", project_id="", limit=0, offset=0,
                            db=_FirstOkThenNoneDB())
    assert ei.value.status_code == 503


# ===========================================================================
# F · D5：offset 无 limit 时不再被丢弃
# ===========================================================================
async def test_f1_offset_without_limit_is_applied(ctx):
    db, pid, sid = ctx
    for i in range(3):
        await _seed(db, pid, sid, gid=f"gF{i}", name=f"事实{i}", value=str(i))
    out = await gf.list_facts(scheme_id=sid, project_id="", limit=0, offset=1, db=db)
    assert out["pagination"]["offset"] == 1
    assert out["pagination"]["total_groups"] == 3
    assert out["pagination"]["returned"] == 2, "offset 必须生效，否则回显与实际不一致"


async def test_f2_no_offset_returns_all(ctx):
    """回归锚：不传 offset 时逐字不变（返回全量）。"""
    db, pid, sid = ctx
    for i in range(3):
        await _seed(db, pid, sid, gid=f"gG{i}", name=f"事实{i}", value=str(i))
    out = await gf.list_facts(scheme_id=sid, project_id="", limit=0, offset=0, db=db)
    assert out["pagination"]["returned"] == 3
