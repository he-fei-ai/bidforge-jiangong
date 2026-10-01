"""目录生成模块 BUG 修复回归测试

覆盖本轮修复与增强：
- sse_handlers._validate_outline(strict_depth) / _outline_validate_fn
  → 层级超限不再中断目录生成（改由三级裁剪兜底）
- sse_handlers._outline_skeleton
  → 审核提示词改用合法 JSON 骨架，不做字符截断
- outline_utils.clamp_outline_depth / normalize_outline
  → 重复裁剪不叠加"（含：...）"、统一裁剪+重排编号
- json_response.renumber_outline
  → 非字典节点健壮性 + 编号连续
- sections._save_outline_to_db
  → word_budget 保留、非法 outline 拒绝、重挂子节点不被误删、正文保留
- upload_outline._outline_seems_valid / save_as_outline
  → 递归层级探测、同名章节一一消费
- file_parser.simple_parse_outline → 年份正文行保护
"""
import copy
import json
import uuid

import pytest

from fastapi import HTTPException


# ============================================================
# 辅助数据
# ============================================================
def _deep_outline(levels: int = 4):
    """构造 levels 层的线性目录树"""
    node = {"title": f"L{levels}", "description": "", "children": []}
    for lv in range(levels - 1, 0, -1):
        node = {"title": f"L{lv}", "description": "", "children": [node]}
    return [node]


async def _seed_scheme(db, sid="s1", pid="p1"):
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)",
                     (pid, "测试项目"))
    await db.execute("INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES (?,?,?)",
                     (sid, pid, "测试方案"))
    await db.commit()


# ============================================================
# _validate_outline / _outline_validate_fn
# ============================================================
class TestValidateOutlineDepth:
    def test_strict_mode_reports_depth_over_limit(self):
        from app.routers.sse_handlers import _validate_outline
        issues = _validate_outline({"outline": _deep_outline(6)})
        assert any("3级目录上限" in i for i in issues)

    def test_non_strict_mode_ignores_depth(self):
        """生成主链路：超三级目录不应作为"问题"上报（否则会中断生成）。"""
        from app.routers.sse_handlers import _validate_outline
        assert _validate_outline({"outline": _deep_outline(6)}, strict_depth=False) == []

    def test_validate_fn_used_by_generation_accepts_deep_tree(self):
        from app.routers.sse_handlers import _outline_validate_fn
        assert _outline_validate_fn({"outline": _deep_outline(6)}) == []

    def test_non_strict_still_reports_structural_errors(self):
        """非严格模式仍必须拦截真正的结构性问题。"""
        from app.routers.sse_handlers import _validate_outline
        issues = _validate_outline(
            {"outline": [{"title": "", "children": None}]}, strict_depth=False)
        assert any("title 为空" in i for i in issues)
        assert any("children 为 null" in i for i in issues)

    def test_empty_outline_still_rejected(self):
        from app.routers.sse_handlers import _outline_validate_fn
        assert _outline_validate_fn({"outline": []}) != []
        assert _outline_validate_fn({}) != []


# ============================================================
# _outline_skeleton
# ============================================================
class TestOutlineSkeleton:
    def test_keeps_structure_and_carries_description(self):
        """2026-09-13 增强：骨架携带 description（截断 60 字），供审核判断节点意图。"""
        from app.routers.sse_handlers import _outline_skeleton
        outline = [{"title": "A", "description": "很长的描述",
                    "children": [{"title": "B", "children": []}]}]
        sk = _outline_skeleton(outline)
        assert sk[0]["title"] == "A"
        assert sk[0]["description"] == "很长的描述"
        assert sk[0]["children"][0]["title"] == "B"
        assert "description" not in sk[0]["children"][0]

    def test_truncates_overlong_description(self):
        from app.routers.sse_handlers import _outline_skeleton
        sk = _outline_skeleton([{"title": "A", "description": "d" * 100}])
        assert sk[0]["description"] == "d" * 60

    def test_always_valid_json_even_when_huge(self):
        """旧实现 json[...][:3000] 会截断成非法 JSON；骨架必须始终可解析。"""
        from app.routers.sse_handlers import _outline_skeleton
        big = [{"title": f"节点{i}" * 20,
                "children": [{"title": f"子{i}", "children": []}]}
               for i in range(300)]
        sk = _outline_skeleton(big)
        # 不抛异常即为合法 JSON
        assert json.loads(json.dumps(sk, ensure_ascii=False))
        assert len(sk) <= 300

    def test_node_budget_respected(self):
        from app.routers.sse_handlers import _outline_skeleton
        big = [{"title": f"n{i}", "children": []} for i in range(500)]
        sk = _outline_skeleton(big, max_nodes=10)
        assert len(sk) == 10


# ============================================================
# clamp_outline_depth / normalize_outline
# ============================================================
class TestClampOutlineDepth:
    def test_clamp_merges_descendant_titles(self):
        from app.services.outline_utils import clamp_outline_depth
        outline = [{"title": "一级", "description": "父描述", "children": [
            {"title": "二级", "children": [
                {"title": "三级", "children": [
                    {"title": "四级", "children": [
                        {"title": "五级", "children": []}]}]}]}]}]
        result = clamp_outline_depth(outline)
        lv3 = result[0]["children"][0]["children"][0]
        assert lv3["children"] == []
        # 孙节点及更深层标题都应并入描述
        assert "四级" in lv3["description"]
        assert "五级" in lv3["description"]

    def test_prev_merge_suffix_replaced_not_appended(self):
        """描述里已有"（含：...）"时必须替换而非叠加。"""
        from app.services.outline_utils import clamp_outline_depth
        outline = [{"title": "A", "children": [
            {"title": "B", "children": [
                {"title": "C", "description": "原描述（含：旧标题）",
                 "children": [{"title": "新标题", "children": []}]}]}]}]
        result = clamp_outline_depth(outline)
        lv3 = result[0]["children"][0]["children"][0]
        assert lv3["description"] == "原描述（含：新标题）"
        assert "旧标题" not in lv3["description"]

    def test_bare_merge_suffix_replaced(self):
        from app.services.outline_utils import clamp_outline_depth
        outline = [{"title": "A", "children": [
            {"title": "B", "children": [
                {"title": "C", "description": "含：旧标题",
                 "children": [{"title": "新标题", "children": []}]}]}]}]
        result = clamp_outline_depth(outline)
        assert result[0]["children"][0]["children"][0]["description"] == "含：新标题"

    def test_non_dict_nodes_are_dropped(self):
        from app.services.outline_utils import clamp_outline_depth
        result = clamp_outline_depth(["bad", 1, None])
        assert result == []

    def test_non_list_returns_as_is(self):
        from app.services.outline_utils import clamp_outline_depth
        assert clamp_outline_depth({"a": 1}) == {"a": 1}


class TestNormalizeOutline:
    def test_clamps_and_renumbers_and_fills_children(self):
        from app.services.outline_utils import normalize_outline
        outline = _deep_outline(4)
        result = normalize_outline(outline)
        lv1 = result[0]
        lv2 = lv1["children"][0]
        lv3 = lv2["children"][0]
        assert (lv1["id"], lv2["id"], lv3["id"]) == ("1", "1.1", "1.1.1")
        assert (lv1["level"], lv2["level"], lv3["level"]) == (1, 2, 3)
        assert lv3["children"] == []
        assert "L4" in lv3["description"]

    def test_strips_embedded_numbering_in_titles(self):
        from app.services.outline_utils import normalize_outline
        result = normalize_outline([{"title": "第一章 工程概况", "children": []}])
        assert result[0]["title"] == "工程概况"

    def test_non_list_passthrough(self):
        from app.services.outline_utils import normalize_outline
        assert normalize_outline(None) is None


# ============================================================
# renumber_outline 健壮性
# ============================================================
class TestRenumberOutlineRobustness:
    def test_skips_non_dict_and_keeps_numbering_continuous(self):
        from app.services.ai.json_response import renumber_outline
        nodes = ["bad", {"title": "A"}, None, {"title": "B"}]
        renumber_outline(nodes)
        assert nodes[1]["id"] == "1"
        assert nodes[3]["id"] == "2"

    def test_non_list_returns_as_is(self):
        from app.services.ai.json_response import renumber_outline
        assert renumber_outline("not a list") == "not a list"


# ============================================================
# _save_outline_to_db
# ============================================================
@pytest.mark.asyncio
class TestSaveOutlineToDb:
    async def test_preserves_existing_word_budget(self, db_conn):
        """请求未带 word_budget 时，不应把用户已设置预算重置为 1500。"""
        from app.routers.sections import _save_outline_to_db
        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, level,"
            " word_budget, sort_order) VALUES ('sec1','s1','p1','工程概况',1,2500,0)")
        await db_conn.commit()

        outline = [{"id": "sec1", "title": "工程概况",
                    "children": [{"title": "新子节点", "children": []}]}]
        await _save_outline_to_db(db_conn, "s1", outline)

        cur = await db_conn.execute("SELECT word_budget FROM sections WHERE id='sec1'")
        assert (await cur.fetchone())[0] == 2500

    async def test_explicit_word_budget_wins(self, db_conn):
        from app.routers.sections import _save_outline_to_db
        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, level,"
            " word_budget, sort_order) VALUES ('sec1','s1','p1','工程概况',1,2500,0)")
        await db_conn.commit()

        outline = [{"id": "sec1", "title": "工程概况", "word_budget": 800,
                    "children": []}]
        await _save_outline_to_db(db_conn, "s1", outline)

        cur = await db_conn.execute("SELECT word_budget FROM sections WHERE id='sec1'")
        assert (await cur.fetchone())[0] == 800

    async def test_preserves_content_when_id_matches(self, db_conn):
        """按 id 匹配的节点必须保留已有正文（不能被 normalize 覆盖 id 破坏）。"""
        from app.routers.sections import _save_outline_to_db
        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, content,"
            " word_count, status, level, sort_order)"
            " VALUES ('sec1','s1','p1','工程概况','已有正文内容',6,'generated',1,0)")
        await db_conn.commit()

        outline = [{"id": "sec1", "title": "工程概况（改名）", "children": []}]
        await _save_outline_to_db(db_conn, "s1", outline)

        cur = await db_conn.execute(
            "SELECT content, title FROM sections WHERE id='sec1'")
        row = await cur.fetchone()
        assert row["content"] == "已有正文内容"
        assert row["title"] == "工程概况（改名）"

    async def test_reparented_child_not_deleted_with_old_parent(self, db_conn):
        """子节点被移到顶层、父节点被移除时，子节点必须保留。"""
        from app.routers.sections import _save_outline_to_db
        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
            " content, level, sort_order)"
            " VALUES ('p1x','s1','p1','','旧父章','',1,0)")
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
            " content, level, sort_order)"
            " VALUES ('c1x','s1','p1','p1x','子章','子章正文',2,0)")
        await db_conn.commit()

        # 新目录：子章升为顶层，旧父章不再出现
        outline = [{"id": "c1x", "title": "子章", "children": []}]
        await _save_outline_to_db(db_conn, "s1", outline)

        cur = await db_conn.execute("SELECT id, content FROM sections WHERE scheme_id='s1'")
        rows = {r["id"]: r["content"] for r in await cur.fetchall()}
        assert "c1x" in rows
        assert rows["c1x"] == "子章正文"
        assert "p1x" not in rows

    async def test_rejects_all_invalid_nodes_without_wiping(self, db_conn):
        """全是非法节点时应 400，且不得把已有目录清空。"""
        from app.routers.sections import _save_outline_to_db
        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, level, sort_order)"
            " VALUES ('keep1','s1','p1','保留章节',1,0)")
        await db_conn.commit()

        with pytest.raises(HTTPException) as ei:
            await _save_outline_to_db(db_conn, "s1", ["bad", 1, None])
        assert ei.value.status_code == 400

        cur = await db_conn.execute(
            "SELECT COUNT(*) FROM sections WHERE scheme_id='s1'")
        assert (await cur.fetchone())[0] == 1

    async def test_empty_list_still_clears_all(self, db_conn):
        """契约：空数组 = 清空全部目录（前端"清除所有目录"依赖）。"""
        from app.routers.sections import _save_outline_to_db
        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, level, sort_order)"
            " VALUES ('x1','s1','p1','章节',1,0)")
        await db_conn.commit()

        result = await _save_outline_to_db(db_conn, "s1", [])
        assert result["count"] == 0
        cur = await db_conn.execute(
            "SELECT COUNT(*) FROM sections WHERE scheme_id='s1'")
        assert (await cur.fetchone())[0] == 0


# ============================================================
# upload_outline
# ============================================================
class TestOutlineSeemsValid:
    def test_empty_and_flat_are_invalid(self):
        from app.routers.upload_outline import _outline_seems_valid
        assert not _outline_seems_valid([])
        assert not _outline_seems_valid([{"title": "A", "children": []}])

    def test_branch_anywhere_is_valid(self):
        from app.routers.upload_outline import _outline_seems_valid
        outline = [{"title": "A", "children": [
            {"title": "B", "children": [{"title": "C", "children": []}]}]}]
        assert _outline_seems_valid(outline)

    def test_malformed_children_does_not_count_as_branch(self):
        """children 为字符串等畸形值时不应被当作有效层级。"""
        from app.routers.upload_outline import _outline_seems_valid
        assert not _outline_seems_valid(
            [{"title": "A", "children": "oops"}, {"title": "B", "children": ""}])

    def test_many_flat_nodes_is_valid(self):
        from app.routers.upload_outline import _outline_seems_valid
        assert _outline_seems_valid(
            [{"title": f"n{i}", "children": []} for i in range(5)])


@pytest.mark.asyncio
class TestSaveAsOutlineDuplicateTitles:
    async def test_duplicate_titles_consume_existing_sections_once(self, db_conn):
        """同名章节必须一一消费：两个"施工准备"各自保留自己的正文。"""
        from app.routers.upload_outline import save_as_outline
        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO uploaded_outlines (id, file_name, status) VALUES ('u1','x.docx','parsed')")
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, content,"
            " level, sort_order) VALUES ('a1','s1','p1','施工准备','正文A',2,0)")
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, content,"
            " level, sort_order) VALUES ('a2','s1','p1','施工准备','正文B',2,1)")
        await db_conn.commit()

        outline = [
            {"title": "第一章", "children": [{"title": "施工准备", "children": []}]},
            {"title": "第二章", "children": [{"title": "施工准备", "children": []}]},
        ]
        result = await save_as_outline("u1", {"scheme_id": "s1", "outline": outline}, db_conn)
        assert result["ok"] is True
        assert result["preserved_content"] == 2

        cur = await db_conn.execute(
            "SELECT id, content, parent_id FROM sections WHERE title='施工准备'")
        rows = {r["id"]: r for r in await cur.fetchall()}
        assert set(rows) == {"a1", "a2"}
        assert rows["a1"]["content"] == "正文A"
        assert rows["a2"]["content"] == "正文B"
        # 两个同名章节应挂到不同的父章节下
        assert rows["a1"]["parent_id"] != rows["a2"]["parent_id"]

    async def test_cascade_deletes_orphans(self, db_conn):
        """不在新目录中的父章节，其未复用子章节必须级联删除，不留孤儿。"""
        from app.routers.upload_outline import save_as_outline
        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO uploaded_outlines (id, file_name, status) VALUES ('u2','y.docx','parsed')")
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
            " content, level, sort_order) VALUES ('old','s1','p1','','旧章','',1,0)")
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
            " content, level, sort_order) VALUES ('oldc','s1','p1','old','旧子节','',2,0)")
        await db_conn.commit()

        outline = [{"title": "全新章节", "children": []}]
        await save_as_outline("u2", {"scheme_id": "s1", "outline": outline}, db_conn)

        cur = await db_conn.execute(
            "SELECT id FROM sections WHERE scheme_id='s1'")
        remaining = {r["id"] for r in await cur.fetchall()}
        assert "old" not in remaining
        assert "oldc" not in remaining


# ============================================================
# file_parser.simple_parse_outline
# ============================================================
class TestSimpleParseOutlineYearGuard:
    def test_year_like_line_not_treated_as_heading(self):
        from app.services.file_parser import simple_parse_outline
        out = simple_parse_outline("2023 年最新规范\n第一章 工程概况\n2024 年月度计划")
        titles = [n["title"] for n in out]
        assert titles == ["工程概况"]

    def test_short_number_heading_still_parsed(self):
        """编号仅 1 位的正常标题不受年份保护误伤。"""
        from app.services.file_parser import simple_parse_outline
        out = simple_parse_outline("1 年度计划\n2 月度计划")
        assert [n["title"] for n in out] == ["年度计划", "月度计划"]


# ============================================================
# outline_library
# ============================================================
@pytest.mark.asyncio
class TestOutlineLibrary:
    async def test_update_missing_library_returns_404(self, db_conn):
        from app.routers.outline_library import update_library
        from app.models import OutlineLibraryUpdate
        with pytest.raises(HTTPException) as ei:
            await update_library(str(uuid.uuid4()), OutlineLibraryUpdate(name="x"), db_conn)
        assert ei.value.status_code == 404

    async def test_apply_and_save_unwraps_dict_outline_json(self, db_conn):
        """历史目录库存 {"outline": [...]} 对象时也应能套用。"""
        from app.routers.outline_library import apply_library_and_save
        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO outline_library (id, name, outline_json, review_status)"
            " VALUES ('l1','库一',?,'已通过')",
            (json.dumps({"outline": [{"title": "A", "children": []}]}),))
        await db_conn.commit()

        result = await apply_library_and_save("l1", {"scheme_id": "s1"}, db_conn)
        assert result["ok"] is True
        assert result["count"] == 1

    async def test_apply_and_save_passes_through_cleared_content(self, db_conn):
        """P1-7：套用目录库必然整表重建（库节点 id 是编号而非主键，无法匹配旧章），
        旧正文会被级联删除 —— 量化结果必须透传给前端，否则用户静默丢正文。"""
        from app.routers.outline_library import apply_library_and_save
        await _seed_scheme(db_conn)
        # 先落一个已生成正文的章节
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, title, level, sort_order,"
            " content, word_count) VALUES (?,?,?,?,?,?,?)",
            ("old1", "s1", "旧章节", 1, 1, "已生成的正文内容", 10))
        await db_conn.commit()

        await db_conn.execute(
            "INSERT INTO outline_library (id, name, outline_json, review_status)"
            " VALUES ('l2','库二',?,'已通过')",
            (json.dumps([{"id": "1", "title": "新结构", "children": []}]),))
        await db_conn.commit()

        result = await apply_library_and_save("l2", {"scheme_id": "s1"}, db_conn)
        # 旧章节与新结构不匹配 → 正文被清除，必须如实回传
        assert result.get("cleared_content_sections", 0) >= 1, \
            "套用目录库清空正文时必须回传 cleared_content_sections"

    async def test_review_missing_library_returns_404(self, db_conn):
        """BUG：旧实现对不存在的 id 也返回 ok=True，审核"看似生效"实则无落库。"""
        from app.routers.outline_library import review_library
        with pytest.raises(HTTPException) as ei:
            await review_library(str(uuid.uuid4()), {"status": "已通过"}, db_conn)
        assert ei.value.status_code == 404

    async def test_review_invalid_status_returns_400(self, db_conn):
        from app.routers.outline_library import review_library
        with pytest.raises(HTTPException) as ei:
            await review_library("whatever", {"status": "瞎写"}, db_conn)
        assert ei.value.status_code == 400

    async def test_apply_preview_does_not_bump_ref_count(self, db_conn):
        """BUG：/apply 只返回 JSON 不落库，旧实现却把 ref_count+1，导致引用次数虚高。"""
        from app.routers.outline_library import apply_to_scheme
        await db_conn.execute(
            "INSERT INTO outline_library (id, name, outline_json, review_status, ref_count)"
            " VALUES ('l2','库二','[]','已通过',0)")
        await db_conn.commit()

        res = await apply_to_scheme("l2", {"scheme_id": "s1"}, db_conn)
        assert res["deprecated"] is True
        cur = await db_conn.execute("SELECT ref_count FROM outline_library WHERE id='l2'")
        assert (await cur.fetchone())["ref_count"] == 0


# ============================================================
# scheme_catalog.list_catalog
# ============================================================
@pytest.mark.asyncio
class TestSchemeCatalogList:
    async def test_list_groups_without_outline_field(self, db_conn):
        """清单列表不再 SELECT outline_json 大字段，但分组/字段仍需正确。"""
        from app.routers.scheme_catalog import list_catalog
        await db_conn.execute(
            "INSERT INTO outline_library (id, name, type, source, outline_json)"
            " VALUES ('c1','深基坑方案','基坑与土方','预置清单',?)",
            (json.dumps([{"title": "A", "children": []}], ensure_ascii=False),))
        await db_conn.commit()

        res = await list_catalog(db=db_conn)
        items = res["categories"]["基坑与土方"]
        assert items[0]["id"] == "c1"
        assert "outline_json" not in items[0]


# ============================================================
# _coerce_bool / _coerce_suggestions（审核结果稳健归一化）
# ============================================================
class TestCoerceBool:
    def test_bool_passthrough(self):
        from app.routers.sse_handlers import _coerce_bool
        assert _coerce_bool(True) is True
        assert _coerce_bool(False) is False

    def test_string_false_not_treated_as_passed(self):
        """BUG：弱模型把 passed 写成字符串 "false" 时，旧实现 `not "false"`=False → 误判通过。"""
        from app.routers.sse_handlers import _coerce_bool
        for v in ("false", "False", "no", "NO", "0", "否", "不通过", "failed"):
            assert _coerce_bool(v, default=True) is False

    def test_string_true_variants(self):
        from app.routers.sse_handlers import _coerce_bool
        for v in ("true", "True", "yes", "1", "是", "通过", "ok"):
            assert _coerce_bool(v) is True

    def test_none_and_unknown_use_default(self):
        from app.routers.sse_handlers import _coerce_bool
        assert _coerce_bool(None, default=True) is True
        assert _coerce_bool(None, default=False) is False
        assert _coerce_bool("看不懂", default=True) is True
        assert _coerce_bool(1) is True
        assert _coerce_bool(0) is False


class TestCoerceSuggestions:
    def test_string_is_kept_as_single_item(self):
        """BUG：旧实现 `"; ".join("补充监测方案章节")` 会按字符拆成 "补; 充; 监; ..."。"""
        from app.routers.sse_handlers import _coerce_suggestions
        assert _coerce_suggestions("补充监测方案章节") == ["补充监测方案章节"]

    def test_none_and_blank(self):
        from app.routers.sse_handlers import _coerce_suggestions
        assert _coerce_suggestions(None) == []
        assert _coerce_suggestions("") == []
        assert _coerce_suggestions("   ") == []

    def test_list_filters_blank_and_non_str(self):
        from app.routers.sse_handlers import _coerce_suggestions
        assert _coerce_suggestions(["A", "", "  ", "B", None]) == ["A", "B"]

    def test_dict_items_extract_text(self):
        from app.routers.sse_handlers import _coerce_suggestions
        assert _coerce_suggestions(
            [{"suggestion": "补验收"}, {"text": "补监测"}]) == ["补验收", "补监测"]


# ============================================================
# _build_partial_preview（长方案分步生成预览与主数据隔离）
# ============================================================
class TestBuildPartialPreview:
    def test_does_not_mutate_source_outline(self):
        """BUG：旧实现浅拷贝 → clamp 的就地修改污染 full_outline，深层标题线索丢失。"""
        from app.routers.sse_handlers import _build_partial_preview
        src = [{"title": "A", "description": "", "children": [
            {"title": "B", "description": "", "children": [
                {"title": "C", "description": "", "children": [
                    {"title": "D", "description": "", "children": []}]}]}]}]
        snapshot = copy.deepcopy(src)

        preview = _build_partial_preview(src)

        # 主数据完全未被改动（这是修复的核心不变量）
        assert src == snapshot
        # 预览按位置重排编号
        assert preview[0]["id"] == "1"
        assert preview[0]["children"][0]["id"] == "1.1"
        c = preview[0]["children"][0]["children"][0]
        assert c["id"] == "1.1.1"
        # 四级节点 D 被裁掉，其标题并入 C 的 description（保留内容线索）
        assert c["children"] == []
        assert "D" in c["description"]

    def test_empty_and_invalid_input(self):
        from app.routers.sse_handlers import _build_partial_preview
        assert _build_partial_preview([]) == []
        assert _build_partial_preview(None) == []


# ============================================================
# _review_and_fix_outline（弱模型审核结果容错 + 自动修复）
# ============================================================
@pytest.mark.asyncio
class TestReviewAndFixOutline:
    async def _run(self, monkeypatch, review_obj, fix_obj=None, fix_raises=None):
        from app.routers import sse_handlers as sh

        calls = {"review": 0, "fix": 0}

        async def fake_collect(messages, validate_fn=None, **kwargs):
            if calls["review"] == 0:
                calls["review"] += 1
                return review_obj, json.dumps(review_obj, ensure_ascii=False)
            calls["fix"] += 1
            if fix_raises is not None:
                raise fix_raises
            return fix_obj, json.dumps(fix_obj, ensure_ascii=False)

        monkeypatch.setattr(sh, "render", lambda *a, **k: "PROMPT")
        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        outline = [{"title": "工程概况", "description": "", "children": []}]
        return await sh._review_and_fix_outline(
            outline, "深基坑", True, "摘要", scheme_name="方案",
            project_facts="", requirements="")

    async def test_string_false_triggers_auto_fix(self, monkeypatch):
        fixed = {"outline": [{"title": "工程概况", "description": "d", "children": [
            {"title": "监测方案", "description": "d", "children": []}]}]}
        outline, review = await self._run(
            monkeypatch, {"passed": "false", "suggestions": "补充监测方案章节"}, fixed)
        assert review["passed"] is True
        # ✅ 语义变更（2026-09-16）：保留原始审核建议（追加而非覆盖）
        assert review["suggestions"][0] == "✅ 已根据审核意见自动修复"
        assert "补充监测方案章节" in review["suggestions"]
        assert outline[0]["id"] == "1"   # 修复结果已裁剪+重排编号

    async def test_string_suggestions_not_split_and_no_crash_on_fix_failure(self, monkeypatch):
        """BUG：旧实现在修复失败分支做 `str + list` 抛 TypeError，把整次目录生成打成失败。"""
        outline, review = await self._run(
            monkeypatch, {"passed": False, "suggestions": "补充监测方案章节"},
            fix_raises=RuntimeError("AI 挂了"))
        assert isinstance(review["suggestions"], list)
        assert review["suggestions"][0] == "补充监测方案章节"
        assert any("自动修复失败" in s for s in review["suggestions"])
        assert outline[0]["title"] == "工程概况"  # 原目录保留

    async def test_suggestions_list_of_dicts_normalized(self, monkeypatch):
        fixed = {"outline": [{"title": "工程概况", "children": []}]}
        _outline, review = await self._run(
            monkeypatch, {"passed": False, "suggestions": [{"suggestion": "补验收"}]}, fixed)
        # ✅ 语义变更（2026-09-16）：修复成功后保留原始建议（追加而非覆盖）
        assert review["suggestions"][0] == "✅ 已根据审核意见自动修复"
        assert "补验收" in review["suggestions"]

    async def test_review_non_dict_result_skips_gracefully(self, monkeypatch):
        _outline, review = await self._run(monkeypatch, ["bad"], None)
        assert review["passed"] is True
        assert isinstance(review["suggestions"], list)
