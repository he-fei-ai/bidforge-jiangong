r"""解析提取 / 目录生成模块 · 深度探查修复护栏（2026-09-29 第五轮）

本轮先复现、后修复的 6 个真实缺陷（每条都有「修复前必失败」的反向验证）：

1. **P1 · 目录生成整链崩溃** `routers/sse_handlers.py`
   `_sublevel_validate_fn` 与 `_merge_unit_results` 用 `str(n.get("title", ""))`
   判空 —— 只覆盖「键缺失」；弱模型返回**键存在但值为 JSON null**（→ Python None）
   时 `str(None)` 得 `"None"`、`.strip()` 仍为真值，`{"title": null}` 被误判为合法
   标题节点放行，随后 `_sub['title'].strip()` 对 None 调方法抛 AttributeError，
   整次目录生成以「目录生成失败」告终（用户白跑数分钟）。同文件另有 20 处同类
   模式均写作 `str(x.get("title") or "")`，唯独这两处漏写 `or ""`。

2. **P1 · 失败哨兵被当成「已完成且有效」** `services/bid_analysis_service.py`
   `_json_all_empty` 把空对象判为「不缺失」，而 `routers/bid_analysis.py` 在
   「全部分段无有效结果」时恰好产出 `{}` 作为 JSON 项失败哨兵。哨兵与判缺失口径
   正面冲突，产生三重假绿：/results 的 all_required_done=true、finish_task
   ("completed") 前端徽标变绿、format_downstream_context 下发只有标题没有键值的
   空小节（input_coverage 因命中锚点反而报「已覆盖」）。

3. **P1 · 新建章节 parent_id 零校验 + 深度无上限** `routers/sections.py`
   `create_section` 只查 level、不校验存在性与方案归属，任意 UUID 原样入库 →
   悬挂引用（_build_tree 当孤儿挂根，章节「凭空移到顶层」）；而 `update_section`
   早有「存在 + 同方案 + 不成环」强校验，两条路径口径分叉。另外
   `MAX_OUTLINE_DEPTH=3` 是唯一事实源、save-outline 按它裁剪，唯独手工新增无上限
   → 落库四级章节，前端三级渲染下该章节已入库却不可见。

4. **P2 · 清空目录后一致性扫描缓存未失效** `routers/sections.py::_save_outline_to_db`
   空分支删章节后漏调 `invalidate_consistency_scan_cache`（非空分支有），
   被删章节的扫描行成为孤儿残留，下次预检可能把不存在的章节报成「仍有冲突」。

5. **P2 · 陈旧提取层残留** `services/doc_pipeline/pipeline.py::sync_extract_layer`
   只 upsert 有源的提取类别，本轮无源的类别既不更新也不清理 → 清空解析项后再物化，
   doc_extractions 里旧行仍是 `status='success'` + 旧内容，
   GET /documents/{id}/extractions 返回「成功」的过期结果、完整性报告字段覆盖率虚高；
   同时 project_documents.extract_status 被写成 'pending'，与表内 success 行自相矛盾。

6. **P1 · R13 判空漏改点** `routers/doc_pipeline.py`
   同文件 `_load_doc` / `get_extractions` / `get_chunks` 已对 `db.execute()` 返回
   None 加守卫，`document_completeness` 与 `project_documents_index` 两处漏改 →
   命中即 AttributeError → 500（前者是「解析质量体检」入口，后者是前端资料列表数据源）。
"""
import json
import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException

from app.services.bid_analysis_service import (
    MARKDOWN_MISSING_RESULT,
    format_downstream_context,
    is_missing_result,
)
from app.routers.sse_handlers import _merge_unit_results, _sublevel_validate_fn
from app.services.outline_utils import MAX_OUTLINE_DEPTH, clamp_outline_depth


# ============================================================
# 1. 目录生成：title 为 JSON null 不得放行 / 不得崩溃
# ============================================================
class TestSublevelValidateFnNullTitle:
    """`_sublevel_validate_fn` 必须同时覆盖「键缺失」与「值为 null」两种缺标题形态。"""

    def test_null_title_rejected(self):
        """修复前：`str(None)` = "None" 为真值 → 返回 []（误判合法），放行后崩溃。"""
        errs = _sublevel_validate_fn({"outline": [{"title": None, "description": "x"}]})
        assert errs, "title=null 必须判为非法节点，不能放行"

    @pytest.mark.parametrize("title", ["", "   ", "\t"])
    def test_empty_and_blank_title_rejected(self, title):
        errs = _sublevel_validate_fn({"outline": [{"title": title}]})
        assert errs, f"title={title!r} 必须判为非法节点"

    def test_valid_title_accepted(self):
        assert _sublevel_validate_fn({"outline": [{"title": "工程概况"}]}) == []

    @pytest.mark.parametrize("bad", ["工程概况", 1, None])
    def test_non_dict_node_rejected(self, bad):
        errs = _sublevel_validate_fn({"outline": [bad]})
        assert errs, f"非对象节点 {bad!r} 必须判为非法"

    @pytest.mark.parametrize("payload", [{}, {"outline": []}, {"outline": "工程概况"}])
    def test_empty_or_non_list_outline_rejected(self, payload):
        assert _sublevel_validate_fn(payload)

    def test_mixed_batch_reports_bad_count(self):
        """混合批：1 个合法 + 2 个缺标题 → 报错且点出数量（便于 AI 修复轮定向重试）。"""
        errs = _sublevel_validate_fn({
            "outline": [{"title": "工程概况"}, {"title": None}, {"title": "  "}]})
        assert errs and "2 个" in errs[0]

    def test_null_and_missing_key_equivalent(self):
        """同一「缺标题」语义，两种形态判据必须一致（旧实现二者不一致）。"""
        a = _sublevel_validate_fn({"outline": [{"title": None}]})
        b = _sublevel_validate_fn({"outline": [{}]})
        assert bool(a) == bool(b) is True


class TestMergeUnitResultsNullTitle:
    """`_merge_unit_results` 遇到 null 标题不得抛异常、不得把字面量 "None" 灌进上下文。"""

    @staticmethod
    def _run(level1, per):
        full, prior, failed = [], [], []
        stopped, added = _merge_unit_results(
            level1, list(range(len(level1))), per, full, prior, failed)
        return stopped, added, full, prior, failed

    def test_null_sub_title_does_not_raise(self):
        """修复前：`_sub['title'].strip()` 对 None 调方法 → AttributeError。"""
        level1 = [{"title": "工程概况"}]
        per = [("success", [{"title": None, "description": "d"},
                            {"title": "工程性质", "description": "d"}])]
        stopped, added, full, prior, failed = self._run(level1, per)
        assert stopped is False and failed == []
        assert prior == ["工程概况 / 工程性质"], "合法子节保留、null 子节跳过"
        assert added == 2, "节点计数沿用既有口径（按列表长度），不因 null 抛错"

    def test_null_chapter_title_not_leaked_as_literal_none(self):
        """修复前：`str(None)` = "None" → prior_l2 出现 "None / 子节标题"，
        随后灌入下一级「已生成小节」提示词，污染上下文。"""
        level1 = [{"title": None}]
        per = [("success", [{"title": "施工部署"}])]
        _, _, _, prior, _ = self._run(level1, per)
        for entry in prior:
            assert "None" not in entry, f"null 章标题不得泄漏为字面量：{entry!r}"
        assert prior == [" / 施工部署"]

    def test_non_dict_children_ignored(self):
        level1 = [{"title": "工程概况"}]
        per = [("success", ["脏数据", None, 3, {"title": "工程性质"}])]
        stopped, added, full, prior, failed = self._run(level1, per)
        assert stopped is False and failed == []
        assert prior == ["工程概况 / 工程性质"], "仅合法字典子节进入 prior_l2"
        assert added == 4, "节点计数沿用既有口径（按列表长度），不因脏数据抛错"

    def test_stopped_unit_preserves_prior_valid_children(self):
        """回归：停止语义不被本轮加固破坏（既有 2026-09-26 行为）。"""
        level1 = [{"title": "工程概况"}, {"title": "施工方法"}]
        per = [("success", [{"title": "工程性质"}]), ("stopped", [])]
        stopped, added, full, prior, failed = self._run(level1, per)
        assert stopped is True and added == 1
        assert len(full) == 1 and full[0]["title"] == "工程概况"

    def test_missing_unit_recorded_as_failed(self):
        """回归：单元结果缺失按失败记账（既有 2026-09-26 行为）。"""
        level1 = [{"title": "工程概况"}]
        stopped, added, full, prior, failed = self._run(level1, [])
        assert stopped is False
        assert full[0]["children"] == [] and full[0]["title"] in failed


# ============================================================
# 2. 目录服务层：null 标题不得在裁剪路径变成字面量 "None"
# ============================================================
class TestOutlineServicesNullTitle:
    """clamp / reference / reorganize 三处标题取值统一 `str(x or "")` 口径。"""

    def test_clamp_null_descendant_title_not_merged_as_none(self):
        """深度裁剪把后代标题并入父节点 description —— null 不得以 "None" 落入目录树。"""
        tree = [{"title": "一级", "description": "d", "children": [
            {"title": "二级", "description": "e", "children": [
                {"title": None, "description": "x"}, {"title": "细项A"}]}]}]
        clamp_outline_depth(tree)
        assert "None" not in json.dumps(tree, ensure_ascii=False), \
            "null 标题不得以字面量 'None' 落入目录树"

    def test_clamp_still_merges_real_deep_titles(self):
        """回归：真实深层标题仍被并入父节点（内容线索不丢）。

        树深 4 级、上限 3 级：第 4 级标题应被并入第 3 级父节点的描述，
        既不留空壳、也不丢标题。
        """
        tree = [{"title": "一级", "children": [
            {"title": "二级", "children": [
                {"title": "三级A", "description": "原有描述",
                 "children": [{"title": "四级A1"}, {"title": "四级A2"}]}]}]}]
        clamp_outline_depth(tree)
        l3 = tree[0]["children"][0]["children"][0]
        assert l3.get("children") == [], "超深子树必须被裁剪"
        assert "原有描述" in str(l3.get("description") or ""), "原有描述不得被覆盖"
        blob = json.dumps(tree, ensure_ascii=False)
        assert "四级A1" in blob and "四级A2" in blob, "被裁剪标题必须并入父节点"

    def test_clamp_depth_limit_unchanged(self):
        """回归：目录深度唯一事实源仍为 MAX_OUTLINE_DEPTH。"""
        tree = [{"title": "1", "children": [
            {"title": "1.1", "children": [
                {"title": "1.1.1", "children": [{"title": "1.1.1.1"}]}]}]}]
        clamp_outline_depth(tree)
        node = tree[0]
        for _ in range(MAX_OUTLINE_DEPTH - 1):
            assert node["children"], "前三级必须保留"
            node = node["children"][0]
        assert node.get("children") == [], "超深子树必须被裁剪"
        assert "1.1.1.1" in json.dumps(tree, ensure_ascii=False), \
            "被裁剪的标题必须并入父节点，不得丢失"


# ============================================================
# 3. 解析提取：JSON 失败哨兵 { } 必须判为「无有效内容」
# ============================================================
class TestJsonSentinelMissing:
    """`is_missing_result` 对空对象哨兵的判定，与全部消费方语义一致。"""

    @pytest.mark.parametrize("raw", ["{}", "{ }", "  { }  "])
    def test_empty_object_is_missing(self, raw):
        """修复前返回 False → all_required_done 假绿、下游拿到空小节。"""
        assert is_missing_result(raw, "json") is True

    def test_fence_wrapped_empty_object_is_missing(self):
        assert is_missing_result("```json\n{}\n```", "json") is True

    def test_all_placeholder_values_still_missing(self):
        """回归：原有「所有字段均没有提及」语义不回归。"""
        assert is_missing_result('{"a": "没有提及", "b": ""}', "json") is True
        assert is_missing_result('{"a": null, "b": "无", "c": []}', "json") is True

    @pytest.mark.parametrize("raw", [
        '{"项目名称": "某项目", "建筑面积": 1000}',
        '{"a": 1, "b": [2]}',
    ])
    def test_valid_json_not_missing(self, raw):
        """回归：真实内容不被误判。"""
        assert is_missing_result(raw, "json") is False

    @pytest.mark.parametrize("raw", ["not json", "[1,2,3]", '"str"', "123"])
    def test_malformed_or_non_object_json_not_missing(self, raw):
        """回归：解析失败/非对象仍返回 False（不误伤「格式坏但有正文」）。"""
        assert is_missing_result(raw, "json") is False

    def test_markdown_and_empty_semantics_unchanged(self):
        assert is_missing_result("未提取到", "markdown") is True
        assert is_missing_result("{}", "markdown") is False
        assert is_missing_result("", "markdown") is True
        assert is_missing_result("   ", "") is True
        assert is_missing_result(None, "json") is True


class TestSentinelNotEmittedDownstream:
    """哨兵项不得以「只有标题、没有键值」的空小节进入下游提示词。"""

    def test_empty_json_sentinel_skipped(self):
        items = {"projectBasicInfo": {
            "status": "success", "output_type": "json",
            "content": "{}", "label": "项目基本信息"}}
        text = format_downstream_context(items)
        assert "## 项目基本信息" not in text, \
            "空壳 JSON 不得产出「只有标题没有内容」的小节"

    def test_valid_json_still_emitted(self):
        items = {"projectBasicInfo": {
            "status": "success", "output_type": "json",
            "content": json.dumps({"项目名称": "某项目"}, ensure_ascii=False),
            "label": "项目基本信息"}}
        text = format_downstream_context(items)
        assert "## 项目基本信息" in text and "项目名称" in text

    @pytest.mark.parametrize("content", ["{}", '{"a": "没有提及"}'])
    def test_success_status_alone_not_enough_to_green_light(self, content):
        """status='success' 不能单独放行缺失内容（三重假绿的共同前置）。"""
        assert is_missing_result(content, "json"), \
            f"status=success 仍必须识别出缺失内容：{content!r}"
        assert not is_missing_result('{"a": "有值"}', "json")

    def test_markdown_sentinel_const_unchanged(self):
        """回归：markdown 哨兵常量不变（下游多处依赖）。"""
        assert MARKDOWN_MISSING_RESULT == "未提取到"
        assert is_missing_result(MARKDOWN_MISSING_RESULT, "markdown") is True


# ============================================================
# 4. 目录生成：新建章节的 parent_id 校验与深度上限
# ============================================================
@pytest_asyncio.fixture
async def scheme_ctx(db_conn):
    """项目 + 方案 + 三级章节链 + 另一方案的章节，供 create_section 校验用例复用。"""
    db = db_conn
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id, project_id, name) VALUES(?,?,?)", (sid, pid, "s"))
    l1, l2, l3 = (uuid.uuid4().hex for _ in range(3))
    for _id, _parent, _title, _lv in ((l1, None, "工程概况", 1),
                                      (l2, l1, "工程性质", 2),
                                      (l3, l2, "规模标准", 3)):
        await db.execute(
            "INSERT INTO sections(id, scheme_id, project_id, parent_id, title,"
            " level, sort_order) VALUES(?,?,?,?,?,?,?)",
            (_id, sid, pid, _parent, _title, _lv, 1))
    other_sid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO schemes(id, project_id, name) VALUES(?,?,?)",
        (other_sid, pid, "s2"))
    other_sec = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO sections(id, scheme_id, project_id, parent_id, title,"
        " level, sort_order) VALUES(?,?,?,?,?,?,?)",
        (other_sec, other_sid, pid, None, "别方案章节", 1, 1))
    await db.commit()
    return {"db": db, "scheme_id": sid, "project_id": pid, "l1": l1, "l2": l2,
            "l3": l3, "other_section_id": other_sec}


@pytest.mark.asyncio
class TestCreateSectionParentGuard:
    """`create_section` 的 parent_id 校验必须与 `update_section` 同口径。"""

    async def test_valid_parent_accepted(self, scheme_ctx):
        from app.models import SectionCreate
        from app.routers.sections import create_section
        c = scheme_ctx
        out = await create_section(c["scheme_id"], SectionCreate(title="工程地点",
                                                                 parent_id=c["l1"]),
                                   c["db"])
        assert out["id"]
        cur = await c["db"].execute(
            "SELECT parent_id, level FROM sections WHERE id=?", (out["id"],))
        row = await cur.fetchone()
        assert row["parent_id"] == c["l1"] and row["level"] == 2

    async def test_nonexistent_parent_rejected(self, scheme_ctx):
        """修复前：任意 UUID 原样入库 → 悬挂引用，章节被 _build_tree 当孤儿挂根。"""
        from app.models import SectionCreate
        from app.routers.sections import create_section
        c = scheme_ctx
        bogus = uuid.uuid4().hex
        with pytest.raises(HTTPException) as ei:
            await create_section(c["scheme_id"],
                                 SectionCreate(title="孤儿", parent_id=bogus), c["db"])
        assert ei.value.status_code == 400
        cur = await c["db"].execute(
            "SELECT COUNT(*) AS n FROM sections WHERE parent_id=?", (bogus,))
        assert (await cur.fetchone())["n"] == 0, "不得产生悬挂父引用"

    async def test_cross_scheme_parent_rejected(self, scheme_ctx):
        """修复前：跨方案 parent_id 无拦截 → 跨方案篡改 + 悬挂引用。"""
        from app.models import SectionCreate
        from app.routers.sections import create_section
        c = scheme_ctx
        with pytest.raises(HTTPException) as ei:
            await create_section(c["scheme_id"],
                                 SectionCreate(title="越权",
                                               parent_id=c["other_section_id"]), c["db"])
        assert ei.value.status_code == 400
        cur = await c["db"].execute(
            "SELECT COUNT(*) AS n FROM sections"
            " WHERE scheme_id=? AND parent_id=?",
            (c["scheme_id"], c["other_section_id"]))
        assert (await cur.fetchone())["n"] == 0, "跨方案父节点不得被引用"

    async def test_depth_limit_enforced(self, scheme_ctx):
        """修复前：三级下新增落库四级，前端按三级渲染 → 数据与展示分叉。"""
        from app.models import SectionCreate
        from app.routers.sections import create_section
        c = scheme_ctx
        with pytest.raises(HTTPException) as ei:
            await create_section(c["scheme_id"],
                                 SectionCreate(title="越界", parent_id=c["l3"]), c["db"])
        assert ei.value.status_code == 400
        assert f"{MAX_OUTLINE_DEPTH} 级" in str(ei.value.detail)
        cur = await c["db"].execute(
            "SELECT MAX(level) AS m FROM sections WHERE scheme_id=?", (c["scheme_id"],))
        assert (await cur.fetchone())["m"] == MAX_OUTLINE_DEPTH, \
            "不得超过目录深度唯一事实源"

    async def test_root_creation_unchanged(self, scheme_ctx):
        """回归：不带 parent_id 的一级新增行为不变（向后兼容）。"""
        from app.models import SectionCreate
        from app.routers.sections import create_section
        c = scheme_ctx
        out = await create_section(c["scheme_id"], SectionCreate(title="新章"), c["db"])
        cur = await c["db"].execute(
            "SELECT parent_id, level, title FROM sections WHERE id=?", (out["id"],))
        row = await cur.fetchone()
        assert row["parent_id"] in (None, "") and row["level"] == 1
        assert row["title"] == "新章"

    async def test_inlined_numbering_still_stripped(self, scheme_ctx):
        """回归：标题内嵌编号仍按统一口径剥离（避免落库后二次编号）。"""
        from app.models import SectionCreate
        from app.routers.sections import create_section
        c = scheme_ctx
        out = await create_section(
            c["scheme_id"], SectionCreate(title="2.1 工程地点",
                                          parent_id=c["l1"]), c["db"])
        cur = await c["db"].execute(
            "SELECT title FROM sections WHERE id=?", (out["id"],))
        assert (await cur.fetchone())["title"] == "工程地点"

    async def test_new_section_gets_numbering(self, scheme_ctx):
        """回归：新建章节立即纳入编号命名空间（不得把 UUID 注入提示词）。"""
        from app.models import SectionCreate
        from app.routers.sections import create_section
        c = scheme_ctx
        out = await create_section(c["scheme_id"], SectionCreate(title="新章"), c["db"])
        cur = await c["db"].execute(
            "SELECT outline_json FROM sections WHERE id=?", (out["id"],))
        raw = (await cur.fetchone())["outline_json"] or ""
        assert raw, "新建章节必须立即写入编号"
        assert out["id"] not in raw, "编号列不得写入 UUID 主键"



# ============================================================
# 4b. 目录深度上限：「移动」路径不得绕过（第九轮 P0，2026-09-30）
# ============================================================
@pytest.mark.asyncio
class TestMoveSectionDepthGuard:
    """`update_section` 改 parent_id 的深度上限必须与 `create_section` 同口径。

    根因（同仓反复出现的「判据在 2 处各自实现」模式）：MAX_OUTLINE_DEPTH=3 是
    唯一事实源，create_section / save-outline / reorganize / _outline_skeleton
    四条路径都按它裁剪或校验，唯独 `update_section` 的 parent_id 分支缺这一项
    —— 原来只有「不自引用 / 父存在且同方案 / 不成环」三项。

    后果：把一级章节移动到三级章节下**静默成功**，随后
    `renumber_sections_after_reorder` 把 sections.level 写成 4，而前端目录树
    按三级渲染 → 该章节已入库却在界面上彻底消失（看不到也无法恢复）。
    这是比「丢失」更难发现的数据/展示分叉，且发生在用户主动拖拽这种高频操作里。
    """

    async def test_move_below_leaf_rejected(self, scheme_ctx):
        """修复前：移动成功 + renumber 写成 level=4 → 界面不可见。"""
        from app.models import SectionUpdate
        from app.routers.sections import update_section
        c = scheme_ctx
        sid, pid, db = c["scheme_id"], c["project_id"], c["db"]
        # 再造一个一级章节作为被移动者
        moved = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO sections(id, scheme_id, project_id, parent_id, title,"
            " level, sort_order) VALUES(?,?,?,?,?,?,?)",
            (moved, sid, pid, None, "被移动的章", 1, 2))
        await db.commit()

        with pytest.raises(HTTPException) as ei:
            await update_section(sid, moved, SectionUpdate(parent_id=c["l3"]), db)
        assert ei.value.status_code == 400
        assert f"{MAX_OUTLINE_DEPTH} 级" in str(ei.value.detail)

    async def test_move_below_leaf_must_not_write(self, scheme_ctx):
        """被拒的移动不得改动任何 level / 不得新增章节（校验必须在写入前）。"""
        from app.models import SectionUpdate
        from app.routers.sections import update_section
        c = scheme_ctx
        sid, db = c["scheme_id"], c["db"]
        moved = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO sections(id, scheme_id, project_id, parent_id, title,"
            " level, sort_order) VALUES(?,?,?,?,?,?,?)",
            (moved, sid, uuid.uuid4().hex, None, "被移动的章", 1, 2))
        await db.commit()
        cur = await db.execute("SELECT id, level FROM sections")
        before = {r["id"]: r["level"] for r in await cur.fetchall()}

        with pytest.raises(HTTPException):
            await update_section(sid, moved, SectionUpdate(parent_id=c["l3"]), db)

        cur = await db.execute("SELECT id, level FROM sections")
        after = {r["id"]: r["level"] for r in await cur.fetchall()}
        assert before == after, "被拒的更新不得改写任何章节层级"

    async def test_move_into_level3_still_allowed(self, scheme_ctx):
        """回归：三级以内的移动必须放行（新校验不得误伤合法操作）。"""
        from app.models import SectionUpdate
        from app.routers.sections import update_section
        c = scheme_ctx
        sid, db = c["scheme_id"], c["db"]
        moved = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO sections(id, scheme_id, project_id, parent_id, title,"
            " level, sort_order) VALUES(?,?,?,?,?,?,?)",
            (moved, sid, uuid.uuid4().hex, None, "被移动的章", 1, 2))
        await db.commit()
        await update_section(sid, moved, SectionUpdate(parent_id=c["l2"]), db)
        cur = await db.execute(
            "SELECT level, parent_id FROM sections WHERE id=?", (moved,))
        r = await cur.fetchone()
        assert r["level"] == 3 and r["parent_id"] == c["l2"]

    async def test_move_to_root_and_shallow_still_allowed(self, scheme_ctx):
        """回归：移到顶层、向浅层移动均不得被误拦。"""
        from app.models import SectionUpdate
        from app.routers.sections import update_section
        c = scheme_ctx
        sid, db = c["scheme_id"], c["db"]
        # l3(level 3) 移到顶层 → level 1
        await update_section(sid, c["l3"], SectionUpdate(parent_id=""), db)
        cur = await db.execute("SELECT level FROM sections WHERE id=?", (c["l3"],))
        assert (await cur.fetchone())["level"] == 1
        # l2(level 2) 移到 l3 下（此时 l3 为顶层）→ l2 仍为 level 2
        await update_section(sid, c["l2"], SectionUpdate(parent_id=c["l3"]), db)
        cur = await db.execute("SELECT level FROM sections WHERE id=?", (c["l2"],))
        assert (await cur.fetchone())["level"] == 2

    async def test_cycle_and_existence_guards_preserved(self, scheme_ctx):
        """回归：新增深度校验不得削弱既有的环检测 / 存在性校验。"""
        from app.models import SectionUpdate
        from app.routers.sections import update_section
        c = scheme_ctx
        sid, db = c["scheme_id"], c["db"]
        with pytest.raises(HTTPException) as ei:
            await update_section(sid, c["l1"], SectionUpdate(parent_id=c["l2"]), db)
        assert ei.value.status_code == 400 and "循环" in ei.value.detail
        with pytest.raises(HTTPException) as ei2:
            await update_section(
                sid, c["l1"], SectionUpdate(parent_id=uuid.uuid4().hex), db)
        assert ei2.value.status_code == 400 and "不存在" in ei2.value.detail

    async def test_depth_guard_message_single_sourced(self, scheme_ctx):
        """静态护栏：两条路径共用同一文案常量，防止再次分叉。"""
        import inspect
        from app.routers import sections as _sec
        assert inspect.getsource(_sec).count("_DEPTH_EXCEEDED_MSG") >= 3
        assert inspect.getsource(_sec.update_section).count(
            "_DEPTH_EXCEEDED_MSG") >= 1, "update_section 必须引用共享深度文案"


# ============================================================
# 5. 目录生成：清空目录同样必须作废一致性扫描缓存
# ============================================================
@pytest.mark.asyncio
class TestClearOutlineInvalidatesScanCache:
    """`_save_outline_to_db` 空分支与非空分支的缓存失效应同口径。"""

    @staticmethod
    async def _seed(db, sid, pid):
        sec = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO sections(id, scheme_id, project_id, title, level, sort_order)"
            " VALUES(?,?,?,?,?,?)", (sec, sid, pid, "工程概况", 1, 1))
        await db.execute(
            "INSERT INTO consistency_scan_cache(section_id, scheme_id, content_hash,"
            " context_hash, rows_json) VALUES(?,?,?,?,?)",
            (sec, sid, "fp1", "ctx1", "[]"))
        await db.commit()
        return sec

    @staticmethod
    async def _cache_count(db, sid):
        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM consistency_scan_cache WHERE scheme_id=?", (sid,))
        return (await cur.fetchone())["n"]

    async def test_empty_outline_clears_cache(self, db_conn):
        """修复前：空分支漏调 invalidate → 被删章节的扫描行孤儿残留。"""
        from app.routers.sections import save_outline
        db = db_conn
        pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db.execute(
            "INSERT INTO schemes(id, project_id, name) VALUES(?,?,?)", (sid, pid, "s"))
        await db.commit()
        await self._seed(db, sid, pid)
        assert await self._cache_count(db, sid) == 1
        out = await save_outline(sid, {"outline": []}, db)
        assert out["ok"] is True and out["count"] == 0
        assert await self._cache_count(db, sid) == 0, "清空目录后缓存必须一并作废"

    async def test_non_empty_outline_still_clears_cache(self, db_conn):
        """回归：非空分支的既有失效行为不被破坏。"""
        from app.routers.sections import save_outline
        db = db_conn
        pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db.execute(
            "INSERT INTO schemes(id, project_id, name) VALUES(?,?,?)", (sid, pid, "s"))
        await db.commit()
        await self._seed(db, sid, pid)
        await save_outline(sid, {"outline": [{"title": "工程概况"},
                                             {"title": "施工方法"}]}, db)
        assert await self._cache_count(db, sid) == 0

    async def test_clear_outline_does_not_touch_other_scheme_cache(self, db_conn):
        """回归：缓存作废必须按 scheme 隔离，不得误删其它方案。"""
        from app.routers.sections import save_outline
        db = db_conn
        pid = uuid.uuid4().hex
        await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        sid, sid2 = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute(
            "INSERT INTO schemes(id, project_id, name)"
            " VALUES(?,?,?), (?, ?, ?)", (sid, pid, "s", sid2, pid, "s2"))
        await db.commit()
        await self._seed(db, sid, pid)
        await self._seed(db, sid2, pid)
        await save_outline(sid, {"outline": []}, db)
        assert await self._cache_count(db, sid) == 0
        assert await self._cache_count(db, sid2) == 1, "不得越界清理其它方案缓存"


# ============================================================
# 6. 解析提取：提取层物化的哨兵过滤与陈旧行标记
# ============================================================
@pytest_asyncio.fixture
async def extract_ctx(db_conn, tmp_path, monkeypatch):
    """提取层物化用例：重定向磁盘存储根 + 建项目/文档行。"""
    import app.routers.global_facts as gf
    import app.services.doc_pipeline.doc_storage as ds
    uploads = tmp_path / "uploads"
    docs_root = tmp_path / "projects"
    monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)
    monkeypatch.setattr(ds, "DOCS_ROOT", docs_root)
    db = db_conn
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    doc_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO project_documents(id, project_id, file_name, parse_status,"
        " parsed_markdown) VALUES(?,?,?,?,?)",
        (doc_id, pid, "a.docx", "success", "正文"))
    await db.commit()
    return {"db": db, "project_id": pid, "doc_id": doc_id}


def _insert_item(db, project_id, item_id, content, output_type="json",
                 label="项目基本信息", required=1):
    """插入一条 bid_analysis_items（sync_extract_layer 的唯一物化来源）。"""
    return db.execute(
        "INSERT INTO bid_analysis_items(id, project_id, item_id, label,"
        " output_type, required, status, content, sort_order, source)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f"{project_id}_{item_id}", project_id, item_id, label,
         output_type, required, "success", content, 1, "ai"))


@pytest.mark.asyncio
class TestSyncExtractLayerSentinel:
    """失败哨兵 `{}` 不得被物化为有效提取结果。"""

    async def test_empty_json_sentinel_not_materialized(self, extract_ctx):
        """修复前：status='success' 即物化 → 空壳 JSON 计入字段覆盖率。"""
        from app.services.doc_pipeline import pipeline
        c = extract_ctx
        await _insert_item(c["db"], c["project_id"], "projectBasicInfo", "{}")
        await c["db"].commit()
        res = await pipeline.sync_extract_layer(
            c["db"], doc_id=c["doc_id"], project_id=c["project_id"])
        assert res["written_types"] == [], \
            f"失败哨兵不得物化：written_types={res['written_types']}"
        cur = await c["db"].execute(
            "SELECT COUNT(*) AS n FROM doc_extractions WHERE doc_id=?",
            (c["doc_id"],))
        assert (await cur.fetchone())["n"] == 0, "doc_extractions 不得出现哨兵行"

    async def test_valid_json_still_materialized(self, extract_ctx):
        """回归：真实 JSON 提取结果仍正常物化。"""
        from app.services.doc_pipeline import pipeline
        c = extract_ctx
        await _insert_item(c["db"], c["project_id"], "projectBasicInfo",
                           json.dumps({"project_name": "某项目"}, ensure_ascii=False))
        await c["db"].commit()
        res = await pipeline.sync_extract_layer(
            c["db"], doc_id=c["doc_id"], project_id=c["project_id"])
        assert "project_info" in res["written_types"]
        cur = await c["db"].execute(
            "SELECT status FROM doc_extractions"
            " WHERE doc_id=? AND extract_type='project_info'", (c["doc_id"],))
        assert (await cur.fetchone())["status"] == "success"

    async def test_markdown_sentinel_not_materialized(self, extract_ctx):
        """回归：markdown 的「未提取到」哨兵同样不得物化（既有语义）。"""
        from app.services.doc_pipeline import pipeline
        c = extract_ctx
        await _insert_item(c["db"], c["project_id"], "overviewParams",
                           MARKDOWN_MISSING_RESULT, output_type="markdown",
                           label="工程概况")
        await c["db"].commit()
        res = await pipeline.sync_extract_layer(
            c["db"], doc_id=c["doc_id"], project_id=c["project_id"])
        assert "engineering" not in res["written_types"]

    async def test_partial_batch_filters_only_sentinel(self, extract_ctx):
        """一批解析项中混有哨兵时，只过滤哨兵项，有效项照常物化。"""
        from app.services.doc_pipeline import pipeline
        c = extract_ctx
        await _insert_item(c["db"], c["project_id"], "projectBasicInfo", "{}")
        await _insert_item(c["db"], c["project_id"], "schemeBasicInfo",
                           json.dumps({"project_name": "某项目"}, ensure_ascii=False),
                           label="方案基本信息")
        await c["db"].commit()
        res = await pipeline.sync_extract_layer(
            c["db"], doc_id=c["doc_id"], project_id=c["project_id"])
        assert "project_info" in res["written_types"]
        assert res["item_count"] == 1, f"item_count 应只统计有效项：{res}"


@pytest.mark.asyncio
class TestStaleExtractionMarking:
    """本轮已无源的提取类别必须被标记陈旧，不得继续报「成功」。"""

    async def _write_then_clear(self, extract_ctx):
        from app.services.doc_pipeline import pipeline
        c = extract_ctx
        await _insert_item(c["db"], c["project_id"], "projectBasicInfo",
                           json.dumps({"project_name": "某项目"}, ensure_ascii=False))
        await c["db"].commit()
        first = await pipeline.sync_extract_layer(
            c["db"], doc_id=c["doc_id"], project_id=c["project_id"])
        await c["db"].execute(
            "UPDATE bid_analysis_items SET status='idle', content=''"
            " WHERE project_id=? AND item_id='projectBasicInfo'",
            (c["project_id"],))
        await c["db"].commit()
        second = await pipeline.sync_extract_layer(
            c["db"], doc_id=c["doc_id"], project_id=c["project_id"])
        cur = await c["db"].execute(
            "SELECT status, extract_data FROM doc_extractions"
            " WHERE doc_id=? AND extract_type='project_info'", (c["doc_id"],))
        return c, first, second, await cur.fetchone()

    async def test_cleared_source_marked_stale(self, extract_ctx):
        """修复前：旧行保持 status='success' + 旧内容 → 过期结果仍报成功。"""
        _, _, _, row = await self._write_then_clear(extract_ctx)
        assert row is not None, "提取层行应保留以便回溯"
        assert row["status"] == "stale", \
            f"清空解析项后必须标记陈旧，实际 status={row['status']!r}"
        assert row["extract_data"], "陈旧行必须保留原始数据（不物理删除）"

    async def test_stale_extract_status_reflects_empty(self, extract_ctx):
        """修复前：extract_status='pending' 与表内 success 行自相矛盾。"""
        from app.services.doc_pipeline import pipeline
        c = extract_ctx
        await _insert_item(c["db"], c["project_id"], "projectBasicInfo",
                           json.dumps({"project_name": "某项目"}, ensure_ascii=False))
        await c["db"].commit()
        await pipeline.sync_extract_layer(
            c["db"], doc_id=c["doc_id"], project_id=c["project_id"])
        await c["db"].execute(
            "DELETE FROM bid_analysis_items WHERE project_id=?", (c["project_id"],))
        await c["db"].commit()
        res = await pipeline.sync_extract_layer(
            c["db"], doc_id=c["doc_id"], project_id=c["project_id"])
        cur = await c["db"].execute(
            "SELECT extract_status FROM project_documents WHERE id=?",
            (c["doc_id"],))
        assert res["extract_status"] == "pending"
        assert (await cur.fetchone())["extract_status"] == "pending"

    async def test_stale_not_counted_in_completeness(self, extract_ctx):
        """修复前：被清空的 project_info 仍以旧内容计入字段覆盖率。"""
        from app.services.doc_pipeline import pipeline
        c, _, _, _row = await self._write_then_clear(extract_ctx)
        report = await pipeline.build_completeness_report(
            c["db"], doc_id=c["doc_id"], project_id=c["project_id"])
        fc = report.get("field_coverage")
        assert fc in (None, 0, 0.0), \
            f"陈旧提取层不得计入字段覆盖率，实际 field_coverage={fc!r}"

    async def test_stale_can_be_revived(self, extract_ctx):
        """回归：重新提取后陈旧行必须恢复为 success（不是单向劣化）。"""
        from app.services.doc_pipeline import pipeline
        c = extract_ctx
        await _insert_item(c["db"], c["project_id"], "projectBasicInfo", "{}")
        await c["db"].commit()
        res = await pipeline.sync_extract_layer(
            c["db"], doc_id=c["doc_id"], project_id=c["project_id"])
        assert res["extract_status"] == "pending"
        await c["db"].execute(
            "UPDATE bid_analysis_items SET content=?, status='success'"
            " WHERE project_id=? AND item_id='projectBasicInfo'",
            (json.dumps({"project_name": "某项目"}, ensure_ascii=False),
             c["project_id"]))
        await c["db"].commit()
        res2 = await pipeline.sync_extract_layer(
            c["db"], doc_id=c["doc_id"], project_id=c["project_id"])
        cur = await c["db"].execute(
            "SELECT status FROM doc_extractions"
            " WHERE doc_id=? AND extract_type='project_info'", (c["doc_id"],))
        assert res2["extract_status"] == "success"
        assert (await cur.fetchone())["status"] == "success"

    async def test_reserved_type_never_marked_stale(self, extract_ctx):
        """回归：预留类别（boq）历史行不得被陈旧化语义波及。"""
        from app.services.doc_pipeline import pipeline
        from app.services.doc_pipeline import doc_storage as ds
        c = extract_ctx
        assert "boq" in ds.RESERVED_EXTRACT_TYPES
        await c["db"].execute(
            "INSERT INTO doc_extractions(extraction_id, doc_id, project_id,"
            " extract_type, extract_data, status) VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, c["doc_id"], c["project_id"], "boq", "{}", "success"))
        await c["db"].commit()
        await pipeline.sync_extract_layer(
            c["db"], doc_id=c["doc_id"], project_id=c["project_id"])
        cur = await c["db"].execute(
            "SELECT status FROM doc_extractions"
            " WHERE doc_id=? AND extract_type='boq'", (c["doc_id"],))
        assert (await cur.fetchone())["status"] == "success", \
            "预留类别的历史行不得被标记陈旧"

    async def test_stale_marking_is_idempotent(self, extract_ctx):
        """回归：连续多轮空物化不报错、状态稳定且只有一行。"""
        from app.services.doc_pipeline import pipeline
        c, _first, _second, _row = await self._write_then_clear(extract_ctx)
        third = await pipeline.sync_extract_layer(
            c["db"], doc_id=c["doc_id"], project_id=c["project_id"])
        cur = await c["db"].execute(
            "SELECT status, COUNT(*) AS n FROM doc_extractions"
            " WHERE doc_id=? AND extract_type='project_info'"
            " GROUP BY status", (c["doc_id"],))
        rows = await cur.fetchall()
        assert third["extract_status"] == "pending"
        assert len(rows) == 1 and rows[0]["n"] == 1
        assert rows[0]["status"] == "stale"


# ============================================================
# 7. 解析提取：R13 判空漏改点（同文件已有守卫，此处漏改）
# ============================================================
class _R13Db:
    """包装真实连接：仅对命中关键字的 SQL 返回 None，模拟 R13 连接瞬时故障。

    真实库中 `db.execute()` 在 WAL/孤儿句柄场景可能返回 None，随后
    `.fetchone()` 直接 AttributeError → 500。这里做最小化模拟，
    不影响同一次请求内的其它查询。
    """

    def __init__(self, real, needles):
        self._real = real
        self._needles = needles
        self.hit = False

    async def execute(self, sql, *args, **kwargs):
        if any(n in sql for n in self._needles):
            self.hit = True
            return None
        return await self._real.execute(sql, *args, **kwargs)

    async def commit(self):
        return await self._real.commit()

    async def rollback(self):
        return await self._real.rollback()

    async def close(self):
        return await self._real.close()


@pytest_asyncio.fixture
async def doc_ctx(db_conn):
    """建一个已解析成功的文档，供 doc_pipeline 路由用例复用。"""
    db = db_conn
    pid, doc_id = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO project_documents(id, project_id, file_name, parse_status,"
        " parsed_markdown) VALUES(?,?,?,?,?)",
        (doc_id, pid, "a.docx", "success", "第一章 工程概况"))
    await db.commit()
    return {"db": db, "project_id": pid, "doc_id": doc_id}


@pytest.mark.asyncio
class TestDocPipelineRouteR13Guard:
    """`db.execute()` 返回 None 时路由必须降级而不是 500。"""

    async def test_completeness_fail_soft_on_none(self, doc_ctx):
        """修复前：`row = await cur.fetchone()` → AttributeError → 500。"""
        from app.routers.doc_pipeline import document_completeness
        c = doc_ctx
        fake = _R13Db(c["db"], ["doc_validation_reports"])
        out = await document_completeness(c["doc_id"], True, fake)
        assert fake.hit, "测试必须真实命中被拦截的查询"
        assert isinstance(out, dict), "缓存读失败必须降级为实时计算，而非抛错"
        assert "quality_score" in out

    async def test_completeness_returns_cache_when_available(self, doc_ctx):
        """回归：缓存命中分支行为不变。"""
        from app.routers.doc_pipeline import document_completeness
        c = doc_ctx
        await c["db"].execute(
            "INSERT INTO doc_validation_reports(id, doc_id, kind, report_json)"
            " VALUES(?,?,?,?)",
            (uuid.uuid4().hex, c["doc_id"], "completeness",
             json.dumps({"quality_score": 0.5, "parsed_pages": 3},
                        ensure_ascii=False)))
        await c["db"].commit()
        out = await document_completeness(c["doc_id"], False, c["db"])
        assert out.get("quality_score") == 0.5
        assert out.get("cached_at"), "缓存命中必须回传 cached_at（既有契约）"

    async def test_documents_index_fail_soft_on_none(self, doc_ctx):
        """修复前：`[dict(r) for r in await cur.fetchall()]` → AttributeError → 500。"""
        from app.routers.doc_pipeline import project_documents_index
        c = doc_ctx
        fake = _R13Db(c["db"], ["project_documents"])
        out = await project_documents_index(c["project_id"], fake)
        assert fake.hit
        assert out["count"] == 0 and out["documents"] == []
        assert out["project_id"] == c["project_id"]

    async def test_documents_index_normal_path_unchanged(self, doc_ctx):
        """回归：正常路径仍返回项目全部文档 + 磁盘索引合并。"""
        from app.routers.doc_pipeline import project_documents_index
        c = doc_ctx
        for i in range(2):
            await c["db"].execute(
                "INSERT INTO project_documents(id, project_id, file_name,"
                " parse_status) VALUES(?,?,?,?)",
                (uuid.uuid4().hex, c["project_id"], f"a{i}.docx", "success"))
        await c["db"].commit()
        out = await project_documents_index(c["project_id"], c["db"])
        assert out["count"] == 3 and len(out["documents"]) == 3
        assert out["project_id"] == c["project_id"]

    async def test_index_returns_orphan_disk_entries(self, doc_ctx):
        """回归：磁盘索引里的孤儿条目（DB 已无行）仍如实回传。"""
        from app.routers.doc_pipeline import project_documents_index
        from app.services.doc_pipeline.doc_storage import update_index
        c = doc_ctx
        # 直接写磁盘索引里一个 DB 中不存在的 doc_id，构造孤儿条目
        from app.services.doc_pipeline.doc_storage import update_index
        update_index(c["project_id"], {"doc_id": uuid.uuid4().hex})
        out = await project_documents_index(c["project_id"], c["db"])
        assert len(out["orphan_disk_entries"]) == 1, \
            "孤儿磁盘条目必须回传，便于人工排查"

    async def test_load_doc_none_gives_503(self, doc_ctx):
        """既有守卫回归：`_load_doc` 命中 None 时返回 503（前端可重试）。"""
        from app.routers.doc_pipeline import document_status
        c = doc_ctx
        fake = _R13Db(c["db"], ["project_documents"])
        with pytest.raises(HTTPException) as ei:
            await document_status(c["doc_id"], fake)
        assert ei.value.status_code == 503






