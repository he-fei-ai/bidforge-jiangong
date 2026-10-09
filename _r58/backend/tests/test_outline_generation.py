"""目录生成模块单元测试

覆盖：
- sse_handlers._validate_outline（空title/children类型/深度限制/节点数量限制）
- sse_handlers._count_nodes（节点计数）
- sections._build_tree（children 排序）
- sections.save_outline（空 outline 拒绝）
"""
import json

import pytest
import pytest_asyncio
from app.routers.sse_handlers import _count_nodes, _validate_outline

# ============================================================
# _validate_outline
# ============================================================

class TestValidateOutline:
    def test_empty_outline(self):
        issues = _validate_outline({"outline": []})
        assert "outline 为空或格式错误" in issues

    def test_missing_outline_key(self):
        issues = _validate_outline({})
        assert "outline 为空或格式错误" in issues

    def test_outline_not_list(self):
        issues = _validate_outline({"outline": "not a list"})
        assert "outline 为空或格式错误" in issues

    def test_valid_outline(self):
        outline = [
            {"title": "第一章", "children": [
                {"title": "1.1", "children": []}
            ]}
        ]
        issues = _validate_outline({"outline": outline})
        assert issues == []

    def test_empty_title(self):
        outline = [{"title": "", "children": []}]
        issues = _validate_outline({"outline": outline})
        assert any("title 为空" in i for i in issues)

    def test_whitespace_title(self):
        outline = [{"title": "   ", "children": []}]
        issues = _validate_outline({"outline": outline})
        assert any("title 为空" in i for i in issues)

    def test_missing_title(self):
        outline = [{"children": []}]
        issues = _validate_outline({"outline": outline})
        assert any("title" in i for i in issues)

    def test_missing_children(self):
        # ✅ BUG-O4 修复：缺失 children 字段是合法叶节点，不应报错
        # 只有显式 children=None 才报错
        outline = [{"title": "第一章"}]
        issues = _validate_outline({"outline": outline})
        assert not any("children" in i for i in issues)

    def test_children_null(self):
        # 显式 children=None 应报错
        outline = [{"title": "第一章", "children": None}]
        issues = _validate_outline({"outline": outline})
        assert any("children" in i for i in issues)

    def test_children_not_list(self):
        outline = [{"title": "第一章", "children": "invalid"}]
        issues = _validate_outline({"outline": outline})
        assert any("children 不是列表" in i for i in issues)

    def test_node_not_dict(self):
        outline = ["not a dict"]
        issues = _validate_outline({"outline": outline})
        assert any("不是字典对象" in i for i in issues)

    def test_deep_nesting_protection(self):
        node = {"title": "deep", "children": []}
        current = node
        for _ in range(15):
            child = {"title": "deep", "children": []}
            current["children"] = [child]
            current = child
        issues = _validate_outline({"outline": [node]})
        assert any("层级过深" in i for i in issues)

    def test_node_count_limit(self):
        nodes = []
        for i in range(600):
            nodes.append({"title": f"node_{i}", "children": []})
        issues = _validate_outline({"outline": nodes})
        assert any("节点总数超过上限" in i for i in issues)

    def test_nested_validation(self):
        outline = [
            {"title": "第一章", "children": [
                {"title": "1.1", "children": [
                    {"title": "", "children": []}
                ]}
            ]}
        ]
        issues = _validate_outline({"outline": outline})
        assert any("title 为空" in i for i in issues)


# ============================================================
# _count_nodes
# ============================================================

class TestCountNodes:
    def test_empty(self):
        assert _count_nodes([]) == 0

    def test_flat(self):
        nodes = [{"title": "a"}, {"title": "b"}, {"title": "c"}]
        assert _count_nodes(nodes) == 3

    def test_nested(self):
        nodes = [
            {"title": "a", "children": [
                {"title": "b", "children": [
                    {"title": "c"}
                ]}
            ]}
        ]
        assert _count_nodes(nodes) == 3

    def test_deep_protection(self):
        node = {"title": "deep", "children": []}
        current = node
        for _ in range(20):
            child = {"title": "deep", "children": []}
            current["children"] = [child]
            current = child
        count = _count_nodes([node])
        assert count < 25


# ============================================================
# _build_tree（需要 db fixture）
# ============================================================

@pytest.mark.asyncio
class TestBuildTree:
    async def test_tree_structure(self, db_conn):
        import uuid

        from app.routers.sections import _build_tree

        scheme_id = str(uuid.uuid4())
        project_id = str(uuid.uuid4())

        await db_conn.execute(
            "INSERT INTO schemes (id, project_id, name, type, word_budget) VALUES (?,?,?,?,?)",
            (scheme_id, project_id, "test", "test", 10000))
        s1 = str(uuid.uuid4())
        s2 = str(uuid.uuid4())
        s3 = str(uuid.uuid4())
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level, sort_order) VALUES (?,?,?,?,?,?,?)",
            (s1, scheme_id, project_id, "", "第一章", 1, 0))
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level, sort_order) VALUES (?,?,?,?,?,?,?)",
            (s2, scheme_id, project_id, s1, "1.1", 2, 1))
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level, sort_order) VALUES (?,?,?,?,?,?,?)",
            (s3, scheme_id, project_id, s1, "1.2", 2, 0))
        await db_conn.commit()

        tree = await _build_tree(db_conn, scheme_id)
        assert len(tree) == 1
        assert tree[0]["title"] == "第一章"
        assert len(tree[0]["children"]) == 2
        assert tree[0]["children"][0]["title"] == "1.2"
        assert tree[0]["children"][1]["title"] == "1.1"

    async def test_empty_tree(self, db_conn):
        import uuid

        from app.routers.sections import _build_tree

        scheme_id = str(uuid.uuid4())
        project_id = str(uuid.uuid4())
        await db_conn.execute(
            "INSERT INTO schemes (id, project_id, name, type, word_budget) VALUES (?,?,?,?,?)",
            (scheme_id, project_id, "test", "test", 10000))
        await db_conn.commit()

        tree = await _build_tree(db_conn, scheme_id)
        assert tree == []


# ============================================================
# save_outline（空 outline 拒绝）
# ============================================================

@pytest.mark.asyncio
class TestSaveOutline:
    async def test_reject_empty_outline(self, db_conn):
        import uuid

        from app.routers.sections import save_outline
        from fastapi import HTTPException

        scheme_id = str(uuid.uuid4())
        project_id = str(uuid.uuid4())
        await db_conn.execute(
            "INSERT INTO schemes (id, project_id, name, type, word_budget) VALUES (?,?,?,?,?)",
            (scheme_id, project_id, "test", "test", 10000))
        await db_conn.commit()

        # 契约（2026-09）：空数组 = 清空全部目录（前端"清除所有目录"依赖此行为）
        result = await save_outline(scheme_id, {"outline": []}, db_conn)
        assert result.get("ok") is True
        cur = await db_conn.execute("SELECT COUNT(*) FROM sections WHERE scheme_id=?", (scheme_id,))
        assert (await cur.fetchone())[0] == 0

    async def test_reject_non_list_outline(self, db_conn):
        import uuid

        from app.routers.sections import save_outline
        from fastapi import HTTPException

        scheme_id = str(uuid.uuid4())
        project_id = str(uuid.uuid4())
        await db_conn.execute(
            "INSERT INTO schemes (id, project_id, name, type, word_budget) VALUES (?,?,?,?,?)",
            (scheme_id, project_id, "test", "test", 10000))
        await db_conn.commit()

        with pytest.raises(HTTPException) as exc_info:
            await save_outline(scheme_id, {"outline": "not a list"}, db_conn)
        assert exc_info.value.status_code == 400


# ============================================================
# simple_parse_outline（降级目录识别）
# ============================================================

class TestSimpleParseOutline:
    def test_arabic_chapter(self):
        from app.services.file_parser import simple_parse_outline
        out = simple_parse_outline("第1章 工程概况\n第2章 施工计划")
        assert len(out) == 2
        assert out[0]["level"] == 1
        assert out[0]["title"] == "工程概况"

    def test_arabic_section(self):
        from app.services.file_parser import simple_parse_outline
        out = simple_parse_outline("第3节 边坡支护\n1.1 测量放线")
        # 第3节 -> level1；1.1 -> level2（挂到最近一级下）
        assert out[0]["title"] == "边坡支护"
        assert out[0]["level"] == 1
        assert out[0]["children"][0]["title"] == "测量放线"
        assert out[0]["children"][0]["level"] == 2

    def test_page_marker_skipped(self):
        from app.services.file_parser import simple_parse_outline
        text = "第一章 工程概况\n第 12 页\n1.1 项目背景\n- 13 -\n1.2 地质条件"
        out = simple_parse_outline(text)
        titles = [n["title"] for n in out]
        assert "第 12 页" not in titles
        assert "- 13 -" not in titles
        # 子节点应正确挂到一级「工程概况」下
        assert out[0]["title"] == "工程概况"
        assert len(out[0]["children"]) == 2

    def test_dotted_subsection(self):
        from app.services.file_parser import simple_parse_outline
        out = simple_parse_outline("1. 总则\n1.1 施工准备\n1.2 资源配置")
        # 1. -> level1；1.1/1.2 -> level2（挂在 1. 下）
        assert out[0]["title"] == "总则"
        assert out[0]["level"] == 1
        assert len(out[0]["children"]) == 2
        assert out[0]["children"][0]["level"] == 2
        assert out[0]["children"][1]["level"] == 2

# ============================================================
# 2026-09-13 目录生成模块增强轮次
# ============================================================

class TestParseAndValidateRobustness:
    """BUG-1 修复：validate_fn 异常应转为 issues 进入修复轮，而非直接炸任务"""

    def test_validator_exception_becomes_issues(self):
        # 模型返回顶层数组时，校验器 obj.get(...) 抛 AttributeError——
        # 旧实现异常沿调用链传播炸掉整个任务；现应转为 issues 返回
        from app.services.ai.json_response import parse_and_validate
        obj, issues = parse_and_validate(
            '[{"title": "x"}]',
            lambda o: [] if o.get("outline") else ["缺少 outline"])
        assert obj is not None
        assert issues, "校验器异常应转为 issues"
        assert "顶层结构非预期" in issues[0]

    def test_scalar_output_no_crash(self):
        # 裸标量（非 {} / []）由 extract_json 判为"未找到 JSON 结构"，
        # parse_and_validate 自身不得抛出未处理异常
        from app.services.ai.json_response import parse_and_validate
        obj, issues = parse_and_validate('"just a string"', lambda o: o.get("k", []))
        assert obj is None
        assert issues == ["输出中未找到 JSON 结构"]

    def test_normal_path_unaffected(self):
        from app.services.ai.json_response import parse_and_validate
        obj, issues = parse_and_validate(
            '{"outline": [{"title": "a", "children": []}]}',
            lambda o: [] if o.get("outline") else ["缺少 outline"])
        assert obj is not None and issues == []


@pytest.mark.asyncio
class TestRegisterTaskStopsStale:
    """BUG-2 修复：register_task 防僵尸必须停止内存中的旧任务（否则旧流继续写库）"""

    async def test_stale_in_memory_task_stopped(self, db_conn):
        from app.services.ai import task_registry as tr

        scheme_id = "scheme-stale-test"
        old_tid = await tr.register_task("outline_generation", "", scheme_id)
        assert old_tid in tr._tasks
        assert tr.is_stopped(old_tid) is False

        new_tid = await tr.register_task("outline_generation", "", scheme_id)
        # 旧任务应已被触发 stop（stop_event set），新任务正常
        assert tr.is_stopped(old_tid) is True
        assert tr.is_stopped(new_tid) is False

    async def test_different_scheme_not_affected(self, db_conn):
        from app.services.ai import task_registry as tr

        t1 = await tr.register_task("outline_generation", "", "scheme-a")
        await tr.register_task("outline_generation", "", "scheme-b")
        # 不同 scheme 的任务互不影响
        assert tr.is_stopped(t1) is False


class TestOutlineReviewTimeouts:
    """BUG-5 修复：审核/修复超时常量化并放宽（旧内联 30s/45s 弱模型下形同虚设）"""

    def test_constants_exist_and_relaxed(self):
        from app.routers import sse_handlers
        assert sse_handlers.OUTLINE_REVIEW_TIMEOUT >= 60
        assert sse_handlers.OUTLINE_FIX_TIMEOUT >= 120


class TestOutlineSkeletonEnhancement:
    """增强：审核骨架携带 description（截断 60 字），提升审核信息量"""

    def test_skeleton_includes_truncated_description(self):
        from app.routers.sse_handlers import _outline_skeleton
        sk = _outline_skeleton([
            {"title": "工程概况", "description": "x" * 100,
             "children": [{"title": "1.1", "description": "y" * 80}]}])
        assert sk[0]["description"] == "x" * 60
        assert sk[0]["children"][0]["description"] == "y" * 60
        assert sk[0]["children"][0]["title"] == "1.1"

    def test_skeleton_omits_empty_description(self):
        from app.routers.sse_handlers import _outline_skeleton
        sk = _outline_skeleton([{"title": "a", "description": ""}])
        assert "description" not in sk[0]

    def test_skeleton_still_legal_json(self):
        import json as _json

        from app.routers.sse_handlers import _outline_skeleton
        sk = _outline_skeleton([{"title": "a", "children": [{"title": "b"}]}], max_nodes=1)
        # 预算耗尽也应输出合法 JSON（可序列化、结构完整）
        assert _json.loads(_json.dumps(sk, ensure_ascii=False)) == sk