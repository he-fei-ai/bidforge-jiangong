"""目录编号命名空间同步与竞态守卫回归测试（2026-09-22 审计修复批次）

覆盖缺陷（均有历史代码依据）：
- BUG-1 delete_section 删除后剩余章节 outline_json.id 断号 → 现在删除即整树重排
- BUG-2 update_section 改 parent_id/level/sort_order 后子树编号不重算 → 现在结构变更即重排
- BUG-3 create_section 不写 outline_json（UUID 泄漏进正文提示词）+ sort_order 默认 0 恒排最前
- BUG-4 upload_outline.save_as_outline / outline_library.apply-and-save 竞态守卫缺口
- BUG-5 outline_json 三处写入口径不一（缺 level / confidence=None）
- 附加：_section_outline_number 不再回退 UUID 主键；_build_parent_chain 编号折算
  与前端/导出同口径（二级 "N"、三级 "N.M"）

测试策略：直接调用路由处理函数 + 内存 sqlite（conftest.db_conn），绕过 FastAPI 依赖注入。
"""
import json

import pytest
from app.models import SectionCreate, SectionUpdate
from app.routers import outline_library as _ol
from app.routers import upload_outline as _uo
from app.routers.sections import _save_outline_to_db, create_section, delete_section, update_section


# ============================================================
# 辅助函数
# ============================================================
async def _seed(db, sid, pid, nodes: list):
    """nodes: [(dbid, parent_id, title, sort_order, level, outline_json_or_None)]"""
    for nid, parent, title, so, level, oj in nodes:
        await db.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level,"
            " sort_order, status, outline_json, word_budget)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (nid, sid, pid, parent, title, level, so, "empty",
             oj if oj is not None else json.dumps(
                 {"id": str(level), "confidence": 0.9}, ensure_ascii=False), 1500))
    await db.commit()


async def _outline_ids(db, sid) -> dict:
    cur = await db.execute(
        "SELECT id, outline_json FROM sections WHERE scheme_id=? ORDER BY sort_order", (sid,))
    out = {}
    for r in await cur.fetchall():
        try:
            obj = json.loads(r["outline_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            obj = {}
        out[r["id"]] = obj.get("id", "") if isinstance(obj, dict) else ""
    return out


async def _outline_objs(db, sid) -> dict:
    cur = await db.execute(
        "SELECT id, outline_json, level FROM sections WHERE scheme_id=?", (sid,))
    res = {}
    for r in await cur.fetchall():
        try:
            obj = json.loads(r["outline_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            obj = {}
        res[r["id"]] = (obj if isinstance(obj, dict) else {}, r["level"])
    return res


async def _base(db, sid="s1", pid="p1"):
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)", (pid, "项目"))
    await db.execute("INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES (?,?,?)",
                     (sid, pid, "方案"))
    await db.commit()


# ============================================================
# BUG-1：删除后编号重排
# ============================================================
class TestDeleteRenumber:
    async def test_delete_middle_no_gap(self, db_conn):
        """删除中间章节后剩余编号连续（1/2/3 删 2 → 1/2）"""
        await _base(db_conn)
        await _seed(db_conn, "s1", "p1", [
            ("a", "", "第一章", 0, 1, json.dumps({"id": "1"})),
            ("b", "", "第二章", 1, 1, json.dumps({"id": "2"})),
            ("c", "", "第三章", 2, 1, json.dumps({"id": "3"})),
            ("c1", "c", "子节", 0, 2, json.dumps({"id": "3.1"})),
        ])
        r = await delete_section("s1", "b", db=db_conn)
        assert r["ok"] is True
        ids = await _outline_ids(db_conn, "s1")
        assert ids == {"a": "1", "c": "2", "c1": "2.1"}

    async def test_delete_last_keeps_prefix(self, db_conn):
        """删除末尾章节不影响前序编号"""
        await _base(db_conn)
        await _seed(db_conn, "s1", "p1", [
            ("a", "", "A", 0, 1, json.dumps({"id": "1"})),
            ("b", "", "B", 1, 1, json.dumps({"id": "2"})),
        ])
        await delete_section("s1", "b", db=db_conn)
        assert await _outline_ids(db_conn, "s1") == {"a": "1"}

    async def test_delete_all_no_error(self, db_conn):
        """删空全部章节：重排对空树无操作、不抛异常"""
        await _base(db_conn)
        await _seed(db_conn, "s1", "p1", [("a", "", "A", 0, 1, json.dumps({"id": "1"}))])
        r = await delete_section("s1", "a", db=db_conn)
        assert r["ok"] is True
        assert await _outline_ids(db_conn, "s1") == {}


# ============================================================
# BUG-2：结构变更后整树重算
# ============================================================
class TestUpdateStructRenumber:
    async def test_move_subtree_renumbers(self, db_conn):
        """把第 1 章的子章节移动到第 2 章下 → 编号随位置重算"""
        await _base(db_conn)
        await _seed(db_conn, "s1", "p1", [
            ("a", "", "第一章", 0, 1, json.dumps({"id": "1"})),
            ("a1", "a", "子节", 0, 2, json.dumps({"id": "1.1"})),
            ("b", "", "第二章", 1, 1, json.dumps({"id": "2"})),
        ])
        r = await update_section("s1", "a1", SectionUpdate(parent_id="b"), db=db_conn)
        assert r["ok"] is True
        ids = await _outline_ids(db_conn, "s1")
        assert ids["a1"] == "2.1"  # 挂到 b（编号 2）下
        # level 列同步为实际深度
        objs = await _outline_objs(db_conn, "s1")
        assert objs["a1"][1] == 2
        assert objs["a1"][0].get("level") == 2

    async def test_title_only_keeps_numbering(self, db_conn):
        """仅改标题不触发重排（编号不变、无多余写）"""
        await _base(db_conn)
        await _seed(db_conn, "s1", "p1", [
            ("a", "", "旧标题", 0, 1, json.dumps({"id": "1", "confidence": 0.7})),
        ])
        await update_section("s1", "a", SectionUpdate(title="新标题"), db=db_conn)
        objs = await _outline_objs(db_conn, "s1")
        assert objs["a"][0]["id"] == "1"
        assert objs["a"][0]["confidence"] == 0.7  # 保留既有字段

    async def test_rename_strips_embedded_numbering(self, db_conn):
        """改名 PATCH 剥离标题内嵌编号（防前端/导出双重编号）"""
        await _base(db_conn)
        await _seed(db_conn, "s1", "p1", [
            ("a", "", "概况", 0, 1, json.dumps({"id": "1"})),
        ])
        await update_section("s1", "a", SectionUpdate(title="1.1 编制依据"), db=db_conn)
        cur = await db_conn.execute("SELECT title FROM sections WHERE id='a'")
        assert (await cur.fetchone())[0] == "编制依据"
        # 正常标题不受影响（无非歧义编号前缀不剥离）
        await update_section("s1", "a", SectionUpdate(title="2023年规范解读"), db=db_conn)
        cur = await db_conn.execute("SELECT title FROM sections WHERE id='a'")
        assert (await cur.fetchone())[0] == "2023年规范解读"
        # 剥离后为空（标题就是裸编号）→ 回退入库不报错，保留剥离前原值
        await update_section("s1", "a", SectionUpdate(title="第三章"), db=db_conn)
        cur = await db_conn.execute("SELECT title FROM sections WHERE id='a'")
        assert (await cur.fetchone())[0] == "第三章"

    async def test_move_keeps_confidence(self, db_conn):
        """结构重排仅刷新 id/level，confidence 等既有键保留"""
        await _base(db_conn)
        await _seed(db_conn, "s1", "p1", [
            ("a", "", "A", 0, 1, json.dumps({"id": "1", "confidence": 0.55})),
            ("b", "", "B", 1, 1, json.dumps({"id": "2"})),
        ])
        await update_section("s1", "a", SectionUpdate(sort_order=5), db=db_conn)
        objs = await _outline_objs(db_conn, "s1")
        assert objs["a"][0]["id"] == "2" and objs["a"][0]["confidence"] == 0.55
        assert objs["b"][0]["id"] == "1"


# ============================================================
# BUG-3：新建章节纳入编号命名空间
# ============================================================
class TestCreateSectionNumbering:
    async def test_create_writes_outline_json(self, db_conn):
        """新建章节 outline_json.id 立即为点分编号（不再留空给 UUID 回退留口子）"""
        await _base(db_conn)
        await _seed(db_conn, "s1", "p1", [
            ("a", "", "第一章", 0, 1, json.dumps({"id": "1"})),
        ])
        r = await create_section("s1", SectionCreate(title="第二章"), db=db_conn)
        ids = await _outline_ids(db_conn, "s1")
        assert ids[r["id"]] == "2"
        # 提示词读取端不再可能拿到 UUID
        from app.routers.sse_handlers import _section_outline_number
        cur = await db_conn.execute(
            "SELECT id, outline_json FROM sections WHERE id=?", (r["id"],))
        row = dict(await cur.fetchone())
        assert _section_outline_number(row) == "2"

    async def test_create_child_numbering(self, db_conn):
        """在已有章节下新建子章节 → 编号为 父.N"""
        await _base(db_conn)
        await _seed(db_conn, "s1", "p1", [
            ("a", "", "第一章", 0, 1, json.dumps({"id": "1"})),
            ("a1", "a", "子节", 0, 2, json.dumps({"id": "1.1"})),
        ])
        r = await create_section("s1", SectionCreate(title="新子节", parent_id="a"),
                                 db=db_conn)
        ids = await _outline_ids(db_conn, "s1")
        assert ids[r["id"]] == "1.2"

    async def test_create_appends_sort_order(self, db_conn):
        """默认 sort_order=0 时追加到同级末尾（旧实现恒排最前）"""
        await _base(db_conn)
        await _seed(db_conn, "s1", "p1", [
            ("a", "", "A", 0, 1, json.dumps({"id": "1"})),
            ("b", "", "B", 1, 1, json.dumps({"id": "2"})),
        ])
        r = await create_section("s1", SectionCreate(title="C"), db=db_conn)
        cur = await db_conn.execute("SELECT sort_order FROM sections WHERE id=?", (r["id"],))
        assert (await cur.fetchone())[0] == 2
        # 显式传非零 sort_order 仍以传参为准（向后兼容）
        r2 = await create_section("s1", SectionCreate(title="D", sort_order=7), db=db_conn)
        cur = await db_conn.execute("SELECT sort_order FROM sections WHERE id=?", (r2["id"],))
        assert (await cur.fetchone())[0] == 7

    async def test_create_on_empty_scheme(self, db_conn):
        """空方案首章：编号为 1，不抛异常"""
        await _base(db_conn)
        r = await create_section("s1", SectionCreate(title="唯一章节"), db=db_conn)
        assert await _outline_ids(db_conn, "s1") == {r["id"]: "1"}


# ============================================================
# BUG-4：整表重建入口的竞态守卫
# ============================================================
async def _fake_running_task(scheme_id: str, task_type: str):
    from app.services.ai.task_registry import _tasks
    key = f"_test_{task_type}_{scheme_id}"
    _tasks[key] = {"type": task_type, "scheme_id": scheme_id, "status": "running"}
    return key


def _drop_task(key: str):
    from app.services.ai.task_registry import _tasks
    _tasks.pop(key, None)


class TestRaceGuards:
    async def test_save_as_outline_blocks_content(self, db_conn):
        """上传落库：正文生成在跑 → 409"""
        await _base(db_conn)
        await db_conn.execute(
            "INSERT INTO uploaded_outlines (id, project_id, scheme_id, file_name)"
            " VALUES ('u1','p1','s1','f.docx')")
        await db_conn.commit()
        key = await _fake_running_task("s1", "content_generation")
        try:
            with pytest.raises(Exception) as ei:
                await _uo.save_as_outline(
                    "u1", {"scheme_id": "s1", "outline": [{"title": "A"}]}, db=db_conn)
            assert getattr(ei.value, "status_code", None) == 409
        finally:
            _drop_task(key)

    async def test_save_as_outline_blocks_outline(self, db_conn):
        """上传落库：目录生成在跑 → 409（旧实现完全无守卫）"""
        await _base(db_conn)
        await db_conn.execute(
            "INSERT INTO uploaded_outlines (id, project_id, scheme_id, file_name)"
            " VALUES ('u2','p1','s1','f.docx')")
        await db_conn.commit()
        key = await _fake_running_task("s1", "outline_generation")
        try:
            with pytest.raises(Exception) as ei:
                await _uo.save_as_outline(
                    "u2", {"scheme_id": "s1", "outline": [{"title": "A"}]}, db=db_conn)
            assert getattr(ei.value, "status_code", None) == 409
        finally:
            _drop_task(key)

    async def test_save_as_outline_no_task_passes_and_unified_shape(self, db_conn):
        """无在跑任务 → 放行；且 outline_json 统一为 id/level/confidence 三键"""
        await _base(db_conn)
        await db_conn.execute(
            "INSERT INTO uploaded_outlines (id, project_id, scheme_id, file_name)"
            " VALUES ('u3','p1','s1','f.docx')")
        await db_conn.commit()
        r = await _uo.save_as_outline(
            "u3", {"scheme_id": "s1", "outline": [{"title": "概况", "children": [
                {"title": "编制依据"}]}]}, db=db_conn)
        assert r.get("ok") is True
        objs = await _outline_objs(db_conn, "s1")
        for oid, (oj, _lvl) in objs.items():
            assert {"id", "level", "confidence"} <= set(oj.keys()), (oid, oj)
            assert oj["confidence"] is not None
        assert sorted(o["id"] if isinstance(o, dict) else "" for o, _ in objs.values()) in (
            ["1", "1.1"], ["1.1", "1"])

    async def test_apply_and_save_blocks_outline_generation(self, db_conn):
        """目录库套用：目录生成在跑 → 409（旧实现只拦正文生成）"""
        await _base(db_conn)
        await db_conn.execute(
            "INSERT INTO outline_library (id, name, outline_json, review_status)"
            " VALUES ('lib1','模板',?, '已通过')",
            (json.dumps([{"title": "第一章", "children": []}]),))
        await db_conn.commit()
        key = await _fake_running_task("s1", "outline_generation")
        try:
            with pytest.raises(Exception) as ei:
                await _ol.apply_library_and_save("lib1", {"scheme_id": "s1"}, db=db_conn)
            assert getattr(ei.value, "status_code", None) == 409
        finally:
            _drop_task(key)

    async def test_finished_task_does_not_block(self, db_conn):
        """终态残留条目不阻塞（G12-4 口径反例回归：status=completed 放行）"""
        await _base(db_conn)
        await db_conn.execute(
            "INSERT INTO uploaded_outlines (id, project_id, scheme_id, file_name)"
            " VALUES ('u4','p1','s1','f.docx')")
        await db_conn.commit()
        from app.services.ai.task_registry import _tasks
        key = "zombie_task"
        _tasks[key] = {"type": "outline_generation", "scheme_id": "s1", "status": "completed"}
        try:
            r = await _uo.save_as_outline(
                "u4", {"scheme_id": "s1", "outline": [{"title": "A"}]}, db=db_conn)
            assert r.get("ok") is True
        finally:
            _drop_task(key)


# ============================================================
# BUG-5：_save_outline_to_db 写入口径统一
# ============================================================
class TestSaveOutlineShape:
    async def test_outline_json_has_level(self, db_conn):
        """save-outline 落库的 outline_json 必含 id/level/confidence 三键"""
        await _base(db_conn)
        outline = [{"title": "概况", "children": [
            {"title": "依据", "children": [{"title": "细则"}]}]}]
        r = await _save_outline_to_db(db_conn, "s1", outline, source="测试")
        assert r["count"] == 3
        objs = await _outline_objs(db_conn, "s1")
        for sid, (oj, col_level) in objs.items():
            assert {"id", "level", "confidence"} <= set(oj.keys()), (sid, oj)
            assert oj["level"] == oj["id"].count(".") + 1
            assert col_level == oj["level"]  # level 列与 outline_json.level 同步

    async def test_confidence_preserved_on_resave(self, db_conn):
        """带 confidence 的节点重存后 confidence 不丢"""
        await _base(db_conn)
        outline = [{"title": "A", "confidence": 0.42}]
        await _save_outline_to_db(db_conn, "s1", outline, source="测试")
        objs = await _outline_objs(db_conn, "s1")
        oj = next(iter(objs.values()))[0]
        assert oj["confidence"] == 0.42 and oj["id"] == "1"


# ============================================================
# 提示词读取端：编号折算与 UUID 拒绝
# ============================================================
class TestPromptNumberReader:
    def test_valid_dotted_id(self):
        from app.routers.sse_handlers import _section_outline_number
        assert _section_outline_number(
            {"id": "uuid-x", "outline_json": '{"id": "1.2.3"}'}) == "1.2.3"

    def test_uuid_never_leaks(self):
        """outline_json 缺失 / id 为 UUID / JSON 非法 → 一律空串，不再回退主键"""
        from app.routers.sse_handlers import _section_outline_number
        uuid_pk = "550e8400-e29b-41d4-a716-446655440000"
        assert _section_outline_number({"id": uuid_pk}) == ""
        assert _section_outline_number(
            {"id": uuid_pk, "outline_json": "not-json"}) == ""
        assert _section_outline_number(
            {"id": uuid_pk, "outline_json": json.dumps({"id": uuid_pk})}) == ""

    def test_parent_chain_display_form(self):
        """上级链编号折算：一级「第X章」、二级「N」、三级「N.M」（与前端/导出同源）"""
        from app.routers.sse_handlers import _build_parent_chain
        sections = [
            {"id": "a", "parent_id": "", "title": "第一章", "level": 1, "sort_order": 0,
             "outline_json": '{"id": "1", "level": 1}'},
            {"id": "a1", "parent_id": "a", "title": "第一节", "level": 2, "sort_order": 0,
             "outline_json": '{"id": "1.1", "level": 2}'},
            {"id": "a1x", "parent_id": "a1", "title": "本节", "level": 3, "sort_order": 0,
             "outline_json": '{"id": "1.1.1", "level": 3}'},
        ]
        chain = _build_parent_chain(sections, "a1x")
        assert chain == "第一章 第一章 > 1 第一节"
        chain2 = _build_parent_chain(sections, "a1")
        assert chain2 == "第一章 第一章"

    def test_parent_chain_dirty_data_fallback(self):
        """历史脏数据（无 outline_json）：按同胞位置折算，不出现 UUID/空标签"""
        from app.routers.sse_handlers import _build_parent_chain
        sections = [
            {"id": "uuid-a", "parent_id": "", "title": "第一章", "level": 1,
             "sort_order": 0, "outline_json": ""},
            {"id": "uuid-b", "parent_id": "", "title": "第二章", "level": 1,
             "sort_order": 1, "outline_json": ""},
            {"id": "uuid-c", "parent_id": "uuid-b", "title": "小节", "level": 2,
             "sort_order": 0, "outline_json": ""},
            {"id": "uuid-d", "parent_id": "uuid-c", "title": "细目", "level": 3,
             "sort_order": 0, "outline_json": ""},
        ]
        # 直接父级为一级：链只含祖先「第二章」，二级自身不入链
        assert _build_parent_chain(sections, "uuid-c") == "第二章 第二章"
        # 二级/三级祖先无编号：按同胞位置折算 —— c 是第二章下第 1 个子节 → "1 小节"
        assert _build_parent_chain(sections, "uuid-d") == "第二章 第二章 > 1 小节"


# ============================================================
# 一致性扫描缓存失效（2026-09-23 · 缓存未失效修复）
# ============================================================
async def _seed_scan_cache(db, sid, section_id):
    await db.execute(
        "INSERT OR REPLACE INTO consistency_scan_cache"
        " (section_id, scheme_id, content_hash, context_hash, rows_json)"
        " VALUES (?,?,?,?,?)", (section_id, sid, "h1", "ctx1", "[]"))
    await db.commit()


async def _scan_cache_count(db, sid):
    cur = await db.execute(
        "SELECT COUNT(*) FROM consistency_scan_cache WHERE scheme_id=?", (sid,))
    return (await cur.fetchone())[0]


class TestScanCacheInvalidation:
    """结构变更入口必须清空本方案扫描缓存；仅改正文不得误清（防过度失效）"""

    async def _seeded(self, db):
        await _base(db)
        await _seed(db, "s1", "p1", [
            ("a", "", "A", 0, 1, json.dumps({"id": "1"})),
            ("b", "", "B", 1, 1, json.dumps({"id": "2"})),
        ])
        await _seed_scan_cache(db, "s1", "a")
        assert await _scan_cache_count(db, "s1") == 1

    async def test_reorder_clears(self, db_conn):
        await self._seeded(db_conn)
        from app.routers.sections import reorder_sections
        await reorder_sections("s1", {"order": ["b", "a"]}, db=db_conn)
        assert await _scan_cache_count(db_conn, "s1") == 0

    async def test_delete_clears(self, db_conn):
        await self._seeded(db_conn)
        await delete_section("s1", "b", db=db_conn)
        assert await _scan_cache_count(db_conn, "s1") == 0

    async def test_create_clears(self, db_conn):
        await self._seeded(db_conn)
        await create_section("s1", SectionCreate(title="C"), db=db_conn)
        assert await _scan_cache_count(db_conn, "s1") == 0

    async def test_struct_update_clears(self, db_conn):
        await self._seeded(db_conn)
        await update_section("s1", "a", SectionUpdate(parent_id="b"), db=db_conn)
        assert await _scan_cache_count(db_conn, "s1") == 0

    async def test_rename_clears(self, db_conn):
        await self._seeded(db_conn)
        await update_section("s1", "a", SectionUpdate(title="新标题"), db=db_conn)
        assert await _scan_cache_count(db_conn, "s1") == 0

    async def test_content_only_keeps_cache(self, db_conn):
        """仅改正文：content_hash 自动失效旧行，无需整方案清空（避免过度失效）"""
        await self._seeded(db_conn)
        await update_section("s1", "a", SectionUpdate(content="新正文内容"), db=db_conn)
        assert await _scan_cache_count(db_conn, "s1") == 1

    async def test_save_outline_clears(self, db_conn):
        await self._seeded(db_conn)
        await _save_outline_to_db(db_conn, "s1", [{"title": "A"}, {"title": "B"}], source="测试")
        assert await _scan_cache_count(db_conn, "s1") == 0

    async def test_save_as_outline_clears(self, db_conn):
        await self._seeded(db_conn)
        await db_conn.execute(
            "INSERT INTO uploaded_outlines (id, project_id, scheme_id, file_name)"
            " VALUES ('u9','p1','s1','f.docx')")
        await db_conn.commit()
        await _uo.save_as_outline(
            "u9", {"scheme_id": "s1", "outline": [{"title": "A"}]}, db=db_conn)
        assert await _scan_cache_count(db_conn, "s1") == 0

    async def test_apply_library_clears(self, db_conn):
        await self._seeded(db_conn)
        await db_conn.execute(
            "INSERT INTO outline_library (id, name, outline_json, review_status)"
            " VALUES ('lib9','模板',?,'已通过')",
            (json.dumps([{"title": "A", "children": []}]),))
        await db_conn.commit()
        r = await _ol.apply_library_and_save("lib9", {"scheme_id": "s1"}, db=db_conn)
        assert r.get("ok") is True
        assert await _scan_cache_count(db_conn, "s1") == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
