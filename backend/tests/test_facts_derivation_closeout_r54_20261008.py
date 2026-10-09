"""全局事实模块 · 派生 / 作用域 / 跨 namespace 收口护栏（R54 · 2026-10-08）

覆盖本轮 5 处修复，每组都配「行为用例 + 反向判别」，不只锁源码形态：

A 【F2 / P1】``PATCH /{fact_id}/resolve-conflict`` 裁决换值后未重派生
    ``chapter`` / ``fact_attr`` —— 全仓第 4 条「改事实值」的写路径，R45 只补了
    条目级更新与分组重建两条。读路径一律「库值优先」，旧维度会被当权威一直
    带进章节视图 / 按章精选 / 属性统计，直到下次重新提取才纠正，而接口回 200。
B 【F1 / P2】``GET /chapters`` 把 ``title`` 同时当「事实名」和「取值」喂给维度
    派生 → 与 ``GET /global-facts`` 对同一行判出不同 ``fact_attr``；行内又没有
    ``name``/``value`` 键 → ``chapter_field_completeness`` 看不到事实名，
    ``covered_fields`` 偏低、``missing_fields`` 虚高（本端点最核心的产出）。
C 【F3 / F4 / P2-P3】``POST /adjust`` 内联了第三份作用域谓词且**少了「OR 项目
    共享」半句** → 共享事实改不了、引用它的操作被静默丢弃、还能 ``add`` 出同名
    第二份（两份同时通过注入门控）；同时 G-04 注释宣称的「整批原子提交」与实际
    不符（``_apply_item_updates`` 内部各自 commit，500 后部分生效）。
D 【F5 / P2】``persist_extraction`` 方案级提取只看方案私有行 → 项目共享层已确认
    的同 key 事实不参与去重，同名事实两份并存且都可注入正文。
E 【F6 / P3】「名称/取值怎么解」与「作用域谓词」各有多份副本 → 收敛为
    ``_fact_name_value`` / ``_fact_scope_where`` 单一出口，并锁死不再分叉。

⚠️ 新增字段（``ignored`` / ``ignored_count``）与新增关键字参数
   （``_apply_item_updates(commit=…)`` / ``_validate_adjust_ops(existing_keys=…,
   ignored_out=…)``）全部**加法式**：默认值下既有调用方与既有单测逐字不变，
   旧前端不消费新键也不报错。
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import uuid

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(APP))
sys.path.insert(0, APP)

from app.routers import global_facts as gf  # noqa: E402
from app.services.facts_classification import (  # noqa: E402
    classify_chapter_from_text,
    classify_fact_attr,
)
from app.services.facts_extractor import (  # noqa: E402
    ExtractionResult,
    FactGroup,
    FactItem,
    normalize_key,
    persist_extraction,
)

# ------------------------------------------------------------ 用例常量与基建 --
NAME = "基坑开挖深度"        # chapter=overview；值决定 fact_attr
CAT = "tech_param"
OLD_Q = "按设计要求"          # qualitative
NEW_Q = "8.5m"               # quantitative
STALE_CH, STALE_ATTR = "basis", "norm"   # 故意写入的「过期维度」（派生真值不同）


@pytest.fixture
async def dbf(db_conn):
    """内存库 + 一个项目 / 一个方案，返回 (db, project_id, scheme_id)。"""
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db_conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db_conn.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db_conn.commit()
    return db_conn, pid, sid


async def _ins(db, *, fid=None, pid="p1", sid="", name=NAME, value=OLD_Q,
               category=CAT, source_file="测试录入", is_resolved=True,
               is_simulated=False, conflict=None):
    """走模块自身的单一 INSERT 出口建行（列清单不会漂移），再按需覆盖维度列。"""
    fid = fid or uuid.uuid4().hex
    await db.execute(
        gf.MANUAL_FACT_INSERT_SQL,
        gf._manual_fact_row(
            fid=fid, pid=pid, sid=sid, group_id=uuid.uuid4().hex,
            group_title=gf.CATEGORY_TITLES.get(category, "") or "技术参数",
            name=name, content=gf._build_fact_content(name, value, is_simulated),
            category=category, source_file=source_file,
            is_simulated=is_simulated, confidence=1.0, is_resolved=is_resolved))
    if conflict is not None:
        await db.execute(
            "UPDATE global_facts SET has_conflict=1, conflict_keys=? WHERE id=?",
            (json.dumps(conflict, ensure_ascii=False), fid))
    await db.commit()
    return fid


async def _bake_dims(db, fid, chapter, fact_attr):
    """模拟「提取期已落库的维度」（读路径库值优先，过期不会被自动纠正）。"""
    await db.execute(
        "UPDATE global_facts SET chapter=?, fact_attr=? WHERE id=?",
        (chapter, fact_attr, fid))
    await db.commit()


async def _get(db, fid, cols=("title", "content", "chapter", "fact_attr",
                              "is_simulated", "is_resolved", "has_conflict",
                              "conflict_keys", "confidence", "is_stale",
                              "scheme_id")):
    cur = await db.execute(
        "SELECT %s FROM global_facts WHERE id=?" % ",".join(cols), (fid,))
    r = await cur.fetchone()
    return dict(r) if r else None


async def _count(db, sql, params=()):
    cur = await db.execute(sql, params)
    return (await cur.fetchone())[0]


def _ast_call_names(src: str) -> list[str]:
    """按 AST 取函数源码里的**真实调用点**名单（注释/文档字符串不计）。"""
    import ast
    names: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.append(f.id)
            elif isinstance(f, ast.Attribute):
                names.append(f.attr)
    return names


def _module_code_strings(module) -> list[str]:
    """模块里「代码位置」的字符串常量（排除模块/类/函数文档字符串）。

    静态锁若直接对 ``inspect.getsource`` 计数，会把修复说明里引用的旧字面量
    也数进去 —— 那类文本存在的意义恰恰是「解释这里曾经错了」。
    """
    import ast
    import inspect as _inspect
    tree = ast.parse(_inspect.getsource(module))
    doc_ids: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, holders):
            body = getattr(node, "body", [])
            first = body[0] if body else None
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                doc_ids.add(id(first.value))
    return [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and id(n) not in doc_ids
    ]


def _name_uses(module, sym: str) -> int:
    """模块内某符号的 AST 引用次数（赋值/定义名与读取名都计入）。"""
    import ast
    import inspect as _inspect
    return sum(
        1 for n in ast.walk(ast.parse(_inspect.getsource(module)))
        if isinstance(n, ast.Name) and n.id == sym)


# ============================================================================ A
class TestResolveConflictRederives:
    """F2：冲突裁决换值必须重派生 chapter / fact_attr（第 4 条写路径）。"""

    async def test_value_change_rederives_both_columns(self, dbf):
        db, pid, sid = dbf
        fid = await _ins(db, pid=pid, sid=sid, value=OLD_Q)
        await _bake_dims(db, fid, STALE_CH, STALE_ATTR)

        out = await gf.resolve_conflict(
            fact_id=fid, data={"value": NEW_Q}, scheme_id=sid, db=db)
        assert out["ok"] is True

        r = await _get(db, fid)
        assert r["fact_attr"] == classify_fact_attr(NAME, NEW_Q) == "quantitative"
        assert r["chapter"] == classify_chapter_from_text(
            NAME, NEW_Q, CAT, "", normalize_key(NAME))
        # 旧维度（basis / norm）必须被覆盖，而不是继续「库值优先」
        assert r["fact_attr"] != STALE_ATTR and r["chapter"] != STALE_CH

    async def test_keep_current_value_preserves_manual_classification(self, dbf):
        """值未变（前端「保留当前值」）→ 只清矛盾，人工归类不得被重算冲掉。"""
        db, pid, sid = dbf
        fid = await _ins(db, pid=pid, sid=sid, value=OLD_Q,
                         conflict=[{"value": "按设计要求", "source": "A.docx",
                                    "confidence": 0.8, "is_simulated": False}])
        await _bake_dims(db, fid, STALE_CH, STALE_ATTR)

        await gf.resolve_conflict(
            fact_id=fid, data={"value": OLD_Q}, scheme_id=sid, db=db)

        r = await _get(db, fid)
        assert r["chapter"] == STALE_CH and r["fact_attr"] == STALE_ATTR
        assert r["has_conflict"] == 0 and r["conflict_keys"] == ""

    async def test_simulated_candidate_keeps_gate_but_rederives(self, dbf):
        """裁决选中模拟候选：闸门（is_simulated=1 ⇒ is_resolved=0）不变，
        但维度仍要按新值重算 —— 两者互不干扰。"""
        db, pid, sid = dbf
        sim_value = "12.5m"
        fid = await _ins(db, pid=pid, sid=sid, value=OLD_Q,
                         conflict=[{"value": sim_value, "source": "AI 推测",
                                    "confidence": 0.3, "is_simulated": True}])
        await _bake_dims(db, fid, STALE_CH, STALE_ATTR)

        await gf.resolve_conflict(
            fact_id=fid, data={"value": sim_value}, scheme_id=sid, db=db)

        r = await _get(db, fid)
        assert r["is_simulated"] == 1 and r["is_resolved"] == 0
        assert r["fact_attr"] == classify_fact_attr(NAME, sim_value)
        assert r["fact_attr"] != STALE_ATTR

    async def test_fixed_columns_survive_extra_dimension_assigns(self, dbf):
        """SET 片段与占位符必须同源：新增两个维度赋值不能错位改掉既有 7 列。

        （本轮初版把维度参数排在固定参数之前 → 占位符错位，content 会收到
        ``is_resolved`` 的值；这条是那次错误的定向判别。）
        """
        db, pid, sid = dbf
        fid = await _ins(db, pid=pid, sid=sid, value=OLD_Q,
                         conflict=[{"value": NEW_Q, "source": "B.docx",
                                    "confidence": 0.9, "is_simulated": False}])
        await db.execute(
            "UPDATE global_facts SET is_stale=1, confidence=0.2 WHERE id=?", (fid,))
        await db.commit()

        await gf.resolve_conflict(
            fact_id=fid, data={"value": NEW_Q}, scheme_id=sid, db=db)

        r = await _get(db, fid)
        assert r["content"] == gf._build_fact_content(NAME, NEW_Q, False)
        assert r["is_simulated"] == 0 and r["is_resolved"] == 1
        assert r["has_conflict"] == 0 and r["conflict_keys"] == ""
        assert float(r["confidence"]) == 1.0 and r["is_stale"] == 0

    async def test_shared_fact_adjudication_also_rederives(self, dbf):
        """项目共享事实（scheme_id=''）从方案页裁决同样要重派生，且不被 409 拦下。"""
        db, pid, sid = dbf
        fid = await _ins(db, pid=pid, sid="", value=OLD_Q)
        await _bake_dims(db, fid, STALE_CH, STALE_ATTR)

        await gf.resolve_conflict(
            fact_id=fid, data={"value": NEW_Q}, scheme_id=sid, db=db)

        r = await _get(db, fid)
        assert r["scheme_id"] == "" and r["fact_attr"] == "quantitative"

    def test_exit_is_called_at_this_site(self):
        src = inspect.getsource(gf.resolve_conflict)
        assert "_rederive_dimension_columns(" in src
        # 判据不得在此处再写一遍（只允许出现在 _rederive_dimension_columns 内部）
        assert "derivation_inputs_changed(" not in src


# ============================================================================ B
def _find_item(resp, name):
    for ch in resp["chapters"]:
        for it in ch["items"]:
            if it["name"] == name:
                return it, ch
    for it in resp["uncategorized"]:
        if it["name"] == name:
            return it, None
    return None, None


class TestChapterViewParity:
    """F1：章节视图与列表接口必须同一口径（同一行不得判出两种属性）。"""

    async def test_fact_attr_matches_list_facts(self, dbf):
        db, pid, sid = dbf
        await _ins(db, pid=pid, sid=sid, value=NEW_Q)   # 维度列留空 → 读路径派生

        ch = await gf.list_facts_by_chapters(scheme_id=sid, project_id="", db=db)
        li = await gf.list_facts(scheme_id=sid, project_id="", db=db)
        list_item = li["groups"][0]["items"][0]

        item, _ = _find_item(ch, NAME)
        assert item is not None
        assert item["fact_attr"] == "quantitative"
        assert item["fact_attr"] == list_item["fact_attr"]
        assert item["chapter"] == list_item["chapter"]

    async def test_distributions_identical_across_two_read_paths(self, dbf):
        db, pid, sid = dbf
        await _ins(db, pid=pid, sid=sid, value=NEW_Q)
        await _ins(db, pid=pid, sid=sid, name="工程地点", value="北京市朝阳区",
                   category="basic")

        ch = await gf.list_facts_by_chapters(scheme_id=sid, project_id="", db=db)
        li = await gf.list_facts(scheme_id=sid, project_id="", db=db)
        assert ch["by_fact_attr"] == li["stats"]["by_chapter"]["by_fact_attr"]
        assert (ch["by_source_kind"]
                == li["stats"]["by_chapter"]["by_source_kind"])

    async def test_covered_fields_sees_the_fact_name(self, dbf):
        """旧实现行内无 ``name`` 键 → 只写在事实名里的字段永远算「未覆盖」。"""
        db, pid, sid = dbf
        await _ins(db, pid=pid, sid=sid, name="工程地点", value="北京市朝阳区",
                   category="basic")

        ch = await gf.list_facts_by_chapters(scheme_id=sid, project_id="", db=db)
        item, chapter = _find_item(ch, "工程地点")
        assert item is not None and chapter is not None
        assert "工程地点" in chapter["covered_fields"]
        assert "工程地点" not in chapter["missing_fields"]

    async def test_item_value_has_no_markdown_markup(self, dbf):
        db, pid, sid = dbf
        await _ins(db, pid=pid, sid=sid, value=NEW_Q)
        ch = await gf.list_facts_by_chapters(scheme_id=sid, project_id="", db=db)
        item, _ = _find_item(ch, NAME)
        assert item["value"] == NEW_Q
        assert "**" not in item["value"] and not item["value"].startswith("-")

    async def test_shared_facts_appear_in_chapter_view(self, dbf):
        """作用域一致性：章节视图与列表都必须含项目共享事实。"""
        db, pid, sid = dbf
        await _ins(db, pid=pid, sid="", name="项目经理", value="张伟",
                   category="personnel")
        ch = await gf.list_facts_by_chapters(scheme_id=sid, project_id="", db=db)
        item, _ = _find_item(ch, "项目经理")
        assert item is not None

    def test_stats_computed_once_only(self):
        """章节统计只算一次（旧实现调用两次，内部各自再跑一遍 nine_chapter_summary
        → 全量扫描 4 次）。⚠️ 必须按 AST 数真实调用点：函数体内的注释也写了
        ``_chapter_stats_for_items(enriched)``，纯字符串计数会把注释算进来
        （本轮护栏第一版就栽在这里，count 得 2）。"""
        src = inspect.getsource(gf.list_facts_by_chapters)
        calls = _ast_call_names(src)
        assert calls.count("_chapter_stats_for_items") == 1
        # 全量重扫的下游函数不得在本端点里再直接调一次
        assert "nine_chapter_summary" not in calls
        assert "_fact_name_value(" in src


# ============================================================================ C
class TestAdjustScopeAndAtomicity:
    """F3 / F4：/adjust 作用域与全模块读取口径同源，且整批原子提交。"""

    async def test_shared_fact_is_updateable(self, dbf):
        db, pid, sid = dbf
        fid = await _ins(db, pid=pid, sid="", value=OLD_Q)   # 项目共享

        out = await gf.adjust_facts({
            "instruction": "把基坑开挖深度改成 8.5m",
            "scheme_id": sid, "apply": True,
            "operations": [{"op": "update", "fact_id": fid, "value": NEW_Q}],
        }, db=db)

        assert out["applied"]["updated"] == 1
        assert out["ignored_count"] == 0
        r = await _get(db, fid, cols=("content", "scheme_id", "chapter",
                                      "fact_attr"))
        assert NEW_Q in r["content"]
        assert r["scheme_id"] == ""                    # 没有被「改归属」
        assert r["fact_attr"] == "quantitative"        # 顺带重派生（同一出口）

    async def test_duplicate_add_is_dropped_with_reason(self, dbf):
        db, pid, sid = dbf
        fid = await _ins(db, pid=pid, sid="", value=OLD_Q)   # 共享同名事实

        out = await gf.adjust_facts({
            "instruction": "新增基坑开挖深度",
            "scheme_id": sid, "apply": True,
            "operations": [{"op": "add", "name": NAME, "value": "9.9m",
                            "category": CAT}],
        }, db=db)

        assert out["applied"]["added"] == 0
        assert out["ignored_count"] == 1
        assert out["ignored"][0]["reason"] == "duplicate_fact_key"
        assert out["ignored"][0]["op"] == "add"
        # 绝不允许出现「共享一份 + 方案私有第二份」双注入
        n = await _count(db, "SELECT COUNT(*) FROM global_facts WHERE fact_key=?",
                         (normalize_key(NAME),))
        assert n == 1

    async def test_hallucinated_id_is_reported_not_silent(self, dbf):
        db, pid, sid = dbf
        await _ins(db, pid=pid, sid=sid, value=OLD_Q)
        ghost = uuid.uuid4().hex

        out = await gf.adjust_facts({
            "instruction": "改一条不存在的事实",
            "scheme_id": sid, "apply": True,
            "operations": [{"op": "update", "fact_id": ghost, "value": "3m"},
                           {"op": "delete", "fact_id": ghost}],
        }, db=db)

        assert out["applied"] == {"updated": 0, "added": 0, "deleted": 0}
        reasons = [i["reason"] for i in out["ignored"]]
        assert reasons.count("unknown_fact_id") == 2

    async def test_batch_is_atomic_rollback_on_later_failure(self, dbf, monkeypatch):
        """第 2 条失败时，第 1 条 update 必须一起回滚（旧实现已各自 commit）。"""
        db, pid, sid = dbf
        fid = await _ins(db, pid=pid, sid=sid, value=OLD_Q)
        real_build = gf._build_fact_content

        def _boom(name, value, is_sim):
            if str(name).startswith("触发异常项"):
                raise RuntimeError("模拟 add 失败")
            return real_build(name, value, is_sim)

        monkeypatch.setattr(gf, "_build_fact_content", _boom)
        with pytest.raises(RuntimeError):
            await gf.adjust_facts({
                "instruction": "改值并新增",
                "scheme_id": sid, "apply": True,
                "operations": [
                    {"op": "update", "fact_id": fid, "value": NEW_Q},
                    {"op": "add", "name": "触发异常项", "value": "1",
                     "category": CAT},
                ],
            }, db=db)

        r = await _get(db, fid, cols=("content", "chapter", "fact_attr"))
        assert OLD_Q in r["content"]          # update 已回滚
        assert r["fact_attr"] == ""           # 派生也未被部分提交
        n = await _count(db, "SELECT COUNT(*) FROM global_facts WHERE title=?",
                         ("触发异常项",))
        assert n == 0

    async def test_response_contract_is_additive(self, dbf):
        db, pid, sid = dbf
        fid = await _ins(db, pid=pid, sid=sid, value=OLD_Q)
        out = await gf.adjust_facts({
            "instruction": "预览不改库",
            "scheme_id": sid, "apply": False,
            "operations": [{"op": "update", "fact_id": fid, "value": NEW_Q}],
        }, db=db)
        # 旧契约键全部在位（旧前端不消费新键也不报错）
        for k in ("ok", "operations", "summary", "applied"):
            assert k in out
        assert out["applied"] is None         # apply=False 行为不变
        assert out["ignored"] == [] and out["ignored_count"] == 0
        r = await _get(db, fid, cols=("content",))
        assert OLD_Q in r["content"]

    def test_validate_ops_defaults_keep_legacy_behavior(self):
        """不传新 kwargs ⇒ 与旧实现逐字一致（不做同名去重、不登记理由）。"""
        fid = "f-legacy"
        ops, _summary = gf._validate_adjust_ops(
            {"operations": [{"op": "add", "name": NAME, "value": "1",
                             "category": CAT},
                            {"op": "update", "fact_id": fid, "value": "2"},
                            "not-a-dict"]},
            {fid})
        assert [o["op"] for o in ops] == ["add", "update"]   # add 未被去重拦下

        with_ignored = []
        gf._validate_adjust_ops(
            {"operations": ["still-bad"]}, set(),
            ignored_out=with_ignored)
        assert with_ignored == [{"op": "unknown", "reason": "op_not_object"}]

    def test_adjust_uses_scope_exit(self):
        assert "_fact_scope_where(" in inspect.getsource(gf.adjust_facts)


# ============================================================================ D
class TestPersistExtractionCrossNamespace:
    """F5：方案级提取必须与项目共享层同 key 去重，但绝不删共享行。"""

    async def _shared_confirmed(self, db, pid):
        return await _ins(db, pid=pid, sid="", name="项目经理", value="张伟",
                          category="personnel")

    async def test_conflicting_value_writes_back_to_shared_row(self, dbf):
        db, pid, sid = dbf
        fid = await self._shared_confirmed(db, pid)

        await persist_extraction(ExtractionResult(groups=[FactGroup(
            title="人员角色", category="personnel",
            items=[FactItem(name="项目经理", value="李强", key="project_manager",
                            category="personnel", source="new.docx")],
        )], total_items=1), db, pid, sid)

        # 不再插第二份方案私有的同名事实
        n = await _count(
            db, "SELECT COUNT(*) FROM global_facts WHERE fact_key='project_manager'")
        assert n == 1
        r = await _get(db, fid, cols=("content", "has_conflict", "is_stale",
                                      "conflict_keys", "is_resolved"))
        assert "张伟" in r["content"]
        assert r["has_conflict"] == 1 and r["is_stale"] == 1
        assert "李强" in r["conflict_keys"]
        assert r["is_resolved"] == 1          # 已确认事实不得被打回待审核

    async def test_same_value_clears_stale_without_conflict(self, dbf):
        db, pid, sid = dbf
        fid = await self._shared_confirmed(db, pid)
        await db.execute("UPDATE global_facts SET is_stale=1 WHERE id=?", (fid,))
        await db.commit()

        await persist_extraction(ExtractionResult(groups=[FactGroup(
            title="人员角色", category="personnel",
            items=[FactItem(name="项目经理", value="张伟", key="project_manager",
                            category="personnel")],
        )], total_items=1), db, pid, sid)

        n = await _count(
            db, "SELECT COUNT(*) FROM global_facts WHERE fact_key='project_manager'")
        assert n == 1
        r = await _get(db, fid, cols=("is_stale", "has_conflict"))
        assert r["is_stale"] == 0 and r["has_conflict"] == 0

    async def test_unconfirmed_shared_row_is_never_deleted(self, dbf):
        """共享行只参与去重/冲突登记；删除候选必须仍只来自方案私有行。"""
        db, pid, sid = dbf
        shared = await _ins(db, pid=pid, sid="", name="项目经理", value="王五",
                            category="personnel", source_file="AI提取",
                            is_resolved=False)

        await persist_extraction(ExtractionResult(groups=[FactGroup(
            title="人员角色", category="personnel",
            items=[FactItem(name="安全员", value="赵六", key="safety_officer",
                            category="personnel")],
        )], total_items=1), db, pid, sid)

        assert await _get(db, shared, cols=("id",)) is not None
        assert await _count(db, "SELECT COUNT(*) FROM global_facts WHERE id=?",
                            (shared,)) == 1

    async def test_project_scope_extraction_unchanged(self, dbf):
        """scheme_id='' 的项目级提取：走原有单查询分支，行为零变化。"""
        db, pid, sid = dbf
        fid = await self._shared_confirmed(db, pid)

        await persist_extraction(ExtractionResult(groups=[FactGroup(
            title="人员角色", category="personnel",
            items=[FactItem(name="项目经理", value="李强", key="project_manager",
                            category="personnel")],
        )], total_items=1), db, pid, "")

        n = await _count(
            db, "SELECT COUNT(*) FROM global_facts WHERE fact_key='project_manager'")
        assert n == 1
        r = await _get(db, fid, cols=("content", "has_conflict"))
        assert "张伟" in r["content"] and r["has_conflict"] == 1

    async def test_scheme_without_project_still_works(self, dbf):
        """project_id 为空（历史/未绑定）时不得因新增共享查询而报错或改行为。"""
        db, pid, sid = dbf
        await persist_extraction(ExtractionResult(groups=[FactGroup(
            title="人员角色", category="personnel",
            items=[FactItem(name="项目经理", value="钱七", key="project_manager",
                            category="personnel")],
        )], total_items=1), db, "", sid)
        assert await _count(
            db, "SELECT COUNT(*) FROM global_facts WHERE scheme_id=?", (sid,)) == 1


# ============================================================================ E
class TestSingleSourceExits:
    """F6：名称/取值解析与作用域谓词各只留一份实现。"""

    def test_name_value_three_shapes(self):
        assert gf._fact_name_value(
            {"title": "外行名", "content": "- **基坑开挖深度**: 12.5m"}
        ) == ("基坑开挖深度", "12.5m")
        # 模拟值标记必须整段剥离（含 ⚠ 与变体写法），不残留脏值
        sim = gf._build_fact_content("基坑开挖深度", "12.5m", True)
        name, value = gf._fact_name_value({"title": "x", "content": sim})
        assert name == "基坑开挖深度" and value == "12.5m"
        assert "⚠" not in value and "模拟值" not in value
        # 非粗体但含「名:值」→ 名称仍以 title 列为准，取值按统一出口回解
        assert gf._fact_name_value({"title": "备注", "content": "基坑深度：12.5m"}) \
            == ("备注", "12.5m")
        # 整段散文（回解不出取值）→ 不把它当名称，value 为空由调用方兜底 content
        assert gf._fact_name_value({"title": "备注", "content": "基坑深度 12.5m"}) \
            == ("备注", "")
        assert gf._fact_name_value({"title": "空值", "content": ""}) == ("空值", "")

    def test_scope_where_three_branches_byte_equal_legacy(self):
        legacy = (" WHERE (scheme_id=? OR (project_id=? "
                  "AND (scheme_id='' OR scheme_id IS NULL)))")
        assert gf._fact_scope_where("s", "p") == (legacy, ["s", "p"])
        assert gf._fact_scope_where("s", "") == (" WHERE scheme_id=?", ["s"])
        assert gf._fact_scope_where("", "p") == (" WHERE project_id=?", ["p"])

    def test_no_inline_scope_predicate_copy_left_in_module(self):
        """作用域谓词只允许有**一处**字面量（``_FACT_SCOPE_PREDICATE`` 定义处）。

        收口前本模块有 5 份内联副本，其中 ``adjust_facts`` 那份少了「OR 项目共享」
        半句（F3）—— 副本本身不是缺陷，**副本可以不一致**才是。
        ⚠️ 只扫「代码里的字符串常量」并排除文档字符串：出口自己的说明里引用了
        旧的内联字面量，纯文本计数会把「解释缺陷的注释」当成「缺陷本身」。
        """
        blob = "\n".join(_module_code_strings(gf))
        assert blob.count("scheme_id=? OR (project_id=?") == 1
        assert "WHERE project_id=? AND scheme_id=?" not in blob
        # 出口必须被真实使用（字面量只剩一份 + 多处引用，才叫收口而非删除）
        assert _name_uses(gf, "_fact_scope_where") >= 3
        assert _name_uses(gf, "_FACT_SCOPE_PREDICATE") >= 5

    def test_bold_line_regex_used_by_one_parser_only(self):
        src = inspect.getsource(gf)
        assert src.count("_RE_BOLD_FACT_LINE") == 2   # 定义 + 唯一引用
        assert "_RE_BOLD_FACT_LINE" in inspect.getsource(gf._fact_name_value)
        assert "_fact_name_value(" in inspect.getsource(gf.list_facts)

    def test_rederive_exit_is_the_only_applier(self):
        src = inspect.getsource(gf)
        assert src.count("derivation_inputs_changed(") == 1
        exit_src = inspect.getsource(gf._rederive_dimension_columns)
        assert "classify_chapter_from_text(" in exit_src
        assert "classify_fact_attr(" in exit_src

    def test_rederive_returns_empty_when_inputs_unchanged(self):
        assert gf._rederive_dimension_columns(
            old_name=NAME, new_name=NAME, old_value=OLD_Q, new_value=OLD_Q,
            old_category=CAT, new_category=CAT) == []
        got = dict(gf._rederive_dimension_columns(
            old_name=NAME, new_name=NAME, old_value=OLD_Q, new_value=NEW_Q,
            old_category=CAT, new_category=CAT))
        assert set(got) == {"chapter", "fact_attr"}
        assert got["fact_attr"] == classify_fact_attr(NAME, NEW_Q)


# ============================================================================ F
class TestFrontendContractParity:
    """跨侧契约：后端新增的 `ignored` / `ignored_count` 前端必须逐项消费。

    ⚠️ 本机 Node 未安装（`node.exe` 三处路径均不存在、`Get-Command node` 为空），
    vitest / tsc 无法执行 —— 所以把「前端枚举与后端枚举是否一致」这类
    **可用 Python 判定**的契约放到这里锁，而不是留给跑不动的前端用例。
    """

    _PAGE = os.path.join(os.path.dirname(APP), "frontend", "src", "pages",
                         "SchemeWorkbenchPage.tsx")

    def _page_src(self) -> str:
        with open(self._PAGE, encoding="utf-8") as f:
            return f.read()

    def test_ignore_reason_enum_matches_backend(self):
        import re
        backend_reasons = set(
            re.findall(r'_ignore\(\s*[^,]+,\s*"([a-z_]+)"',
                       inspect.getsource(gf._validate_adjust_ops)))
        assert backend_reasons == {
            "op_not_object", "unknown_fact_id", "no_effective_change",
            "missing_name_or_value", "duplicate_fact_key", "unknown_op",
        }
        src = self._page_src()
        block = src.split("FACTS_ADJUST_IGNORE_REASONS = [", 1)[1].split("]", 1)[0]
        front = set(re.findall(r'"([a-z_]+)"', block))
        # 双向：前端不得漏项（漏了就静默显示英文键名），也不得多项（说明后端已改名）
        assert front == backend_reasons
        labels = src.split("FACTS_ADJUST_IGNORE_REASON_LABELS", 1)[1]
        for r in backend_reasons:
            assert r + ":" in labels, f"理由 {r} 无中文标签"

    def test_frontend_consumes_the_new_fields(self):
        src = self._page_src()
        assert "ignored?: FactsAdjustIgnoredItem[]" in src        # 契约字段在位
        assert "normalizeFactsAdjustPlan(data)" in src            # 预览走归一出口
        assert "normalizeFactsAdjustPlan(res)" in src             # 应用回传也归一
        assert "data?.ignored" in src                             # api 层不再丢弃
        # 旧接线（apply 返回 void）必须仍然合法：归一函数要能吞掉 undefined
        assert "Promise<Partial<FactsAdjustPlanShape> | void>" in src


# ============================================================================ G
class _MapCur:
    """游标桩：行是 dict（Mapping 行工厂）。"""

    def __init__(self, rows):
        self._rows = list(rows)
        self.rowcount = len(self._rows)

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)


class _StubDB:
    """按 SQL 片段命中返回预设行的最桩库（行形态由调用方决定）。"""

    def __init__(self, rules, *, none_cursor=False):
        self._rules = list(rules)
        self._none_cursor = none_cursor
        self.sql_log: list[str] = []

    async def execute(self, sql, params=()):
        s = str(sql)
        self.sql_log.append(s)
        if self._none_cursor:
            return None
        for needle, rows in self._rules:
            if needle in s:
                return _MapCur(rows)
        return _MapCur([])

    async def commit(self):
        pass

    async def rollback(self):
        pass


class TestSchemeProjectIdSingleSource:
    """F7：「方案 → 所属项目」反查与「行形态取值」收敛为单一出口。

    同一句 ``SELECT project_id FROM schemes WHERE id=?`` 在本文件逐字重复 9 次，
    取值清一色用位置索引 ``row[0]``。生产连接是 ``aiosqlite.Row``（键位皆可），
    所以**线上不报错**；但取值写法一旦遇到 Mapping 行就抛 ``KeyError``，
    而它落点全在 fail-soft 分支里 —— 异常被吞成空 project_id 后：
      · ``_invalidate_fact_scope_cache(db, "", "")`` 因 ``not project_id`` 静默
        return，项目下**全部**方案 ``facts_updated_at`` 不推进（用户看不到
        「事实已变更」标记，正文过期而零提示）；
      · ``resolve_scheme_project_id`` 回 "" → 项目共享事实从正文注入 / 目录 /
        导出 / 预检的作用域里**整体消失**。
    实证（不是推演）：``tests/test_content_terminal_payload_20261005.py`` 的桩库
    ``FROM schemes`` 返回 dict 行，日志里就是这条
    ``反查方案所属项目异常（降级为仅方案级查询）· KeyError: 0``。
    """

    # ---------------------------------------------------------------- 取值出口
    def test_row_field_shapes(self):
        import sqlite3

        from app.services.facts_extractor import row_field
        # Mapping：只认键名（旧 prow[0] 在这一形下必炸）
        assert row_field({"project_id": "p1"}, "project_id") == "p1"
        assert row_field({"project_id": None}, "project_id") == ""
        assert row_field({"other": "x"}, "project_id") == ""
        assert row_field({}, "project_id") == ""
        # 序列：按 index 兜底
        assert row_field(("p1",), "project_id") == "p1"
        assert row_field(("p1", "extra"), "project_id", index=1) == "extra"
        # Row：键名优先，位置兜底
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        _one = con.execute("SELECT 'p1' AS project_id").fetchone()
        assert row_field(_one, "project_id") == "p1"
        assert row_field(_one, "no_such", 0) == "p1"
        con.close()
        # 空行 / 脏行：一律回空串，绝不抛（调用方 fail-soft 语义不变）
        assert row_field(None, "project_id") == ""
        assert row_field([], "project_id") == ""
        assert row_field(0, "project_id") == ""

    async def test_service_exit_accepts_mapping_rows(self):
        """服务层反查：dict 行必须取到真实 pid（旧实现吞成 ""）。"""
        from app.services.facts_extractor import resolve_scheme_project_id
        assert await resolve_scheme_project_id(
            _StubDB([("FROM schemes", [{"project_id": "p1"}])]), "s1") == "p1"

    async def test_service_exit_accepts_sequence_rows(self):
        """序列行不得回退 —— 生产就是这一形（aiosqlite.Row）。"""
        from app.services.facts_extractor import resolve_scheme_project_id
        assert await resolve_scheme_project_id(
            _StubDB([("FROM schemes", [("p1",)])]), "s1") == "p1"

    async def test_service_exit_missing_row_and_missing_scheme(self):
        from app.services.facts_extractor import resolve_scheme_project_id
        assert await resolve_scheme_project_id(_StubDB([]), "s1") == ""
        assert await resolve_scheme_project_id(_StubDB([]), "") == ""

    async def test_service_exit_cursor_none_still_fail_soft(self):
        """R13：游标为空时服务层仍回 ""（不抛），由调用方收窄作用域。"""
        from app.services.facts_extractor import resolve_scheme_project_id
        assert await resolve_scheme_project_id(
            _StubDB([], none_cursor=True), "s1") == ""

    # ------------------------------------------------------------ 路由层三态
    async def test_router_exit_is_tristate_not_two_in_one(self, dbf):
        """``None``=方案不存在、``""``=存在但未绑定项目 —— 旧副本把两者混谈。"""
        db, pid, sid = dbf
        assert await gf._scheme_project_id(db, sid) == pid

        sid_nop = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
            (sid_nop, "", "未绑定项目的方案"))
        await db.commit()
        assert await gf._scheme_project_id(db, sid_nop) == ""
        assert await gf._scheme_project_id(db, "no-such-scheme") is None

    async def test_router_exit_cursor_none_raises_503_not_404(self):
        """DB 瞬时故障不得被降级成「方案不存在」（本文件既有 R13 口径）。"""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            await gf._scheme_project_id(_StubDB([], none_cursor=True), "s1")
        assert ei.value.status_code == 503

    async def test_router_exit_on_mapping_rows(self):
        """路由层出口同样不吃行形态（dict 行 → 真实 pid）。"""
        assert await gf._scheme_project_id(
            _StubDB([("FROM schemes", [{"project_id": "p9"}])]), "s1") == "p9"

    # ---------------------------- 行为后果：共享事实的缓存失效链不能静默跳过
    async def test_empty_project_id_invalidates_nothing_at_all(
            self, dbf, monkeypatch):
        """反向锁死「为什么空 pid 是真问题」：一次缓存失效都不发生。"""
        db, pid, sid = dbf
        calls: list[str] = []
        orig = gf.invalidate_export_cache

        async def _spy(d, scheme_id, **kw):
            calls.append(scheme_id)
            return await orig(d, scheme_id, **kw)

        monkeypatch.setattr(gf, "invalidate_export_cache", _spy)
        await gf._invalidate_fact_scope_cache(db, "", "")
        assert calls == [], "空 pid 必须静默 return（这正是 F7 的危害面）"
        await gf._invalidate_fact_scope_cache(db, "", pid)
        assert calls == [sid], "同一库同一时刻，有 pid 就该失效到项目下方案"

    async def test_confirm_shared_fact_invalidates_project_scope(
            self, dbf, monkeypatch):
        """F7 端到端①：确认一条项目共享事实 → 失效出口必须拿到真实 project_id。"""
        db, pid, sid = dbf
        fid = await _ins(db, pid=pid, sid="", name="项目经理", value="张伟")
        seen: list[tuple] = []

        async def _spy(d, scheme_id, project_id=""):
            seen.append((scheme_id, project_id))

        monkeypatch.setattr(gf, "_invalidate_fact_scope_cache", _spy)
        out = await gf.resolve_fact(fid, "", db=db)
        assert out["ok"] is True
        # 旧实现：位置索引取值抛错 / 二次查询 → 传 ("", "") → 静默一个都不失效
        assert seen == [("", pid)], seen

    async def test_resolve_conflict_shared_fact_invalidates_project_scope(
            self, dbf, monkeypatch):
        """F7 端到端②：裁决共享事实的冲突，同样要按项目作用域失效。"""
        db, pid, sid = dbf
        fid = await _ins(db, pid=pid, sid="", value=OLD_Q,
                         conflict=[{"value": NEW_Q, "source": "B.docx",
                                    "confidence": 0.9, "is_simulated": False}])
        seen: list[tuple] = []

        async def _spy(d, scheme_id, project_id=""):
            seen.append((scheme_id, project_id))

        monkeypatch.setattr(gf, "_invalidate_fact_scope_cache", _spy)
        await gf.resolve_conflict(
            fact_id=fid, data={"value": NEW_Q}, scheme_id=sid, db=db)
        assert seen == [("", pid)], seen

    async def test_shared_fact_scope_check_still_rejects_other_project(
            self, dbf):
        """换出口不得放宽归属闸门：别的项目的方案仍被 409 拦下。"""
        from fastapi import HTTPException
        db, pid, sid = dbf
        fid = await _ins(db, pid=pid, sid="", name="项目经理", value="张伟")
        other_sid = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
            (other_sid, "other-project", "别的项目的方案"))
        await db.commit()
        with pytest.raises(HTTPException) as ei:
            await gf._assert_fact_in_scheme_scope(db, fid, other_sid)
        assert ei.value.status_code == 409
        # 本项目的方案则放行（共享事实可读可改）
        row = await gf._assert_fact_in_scheme_scope(db, fid, sid)
        assert row["id"] == fid

    # ---------------------------------------------------------------- 静态锁
    def test_scheme_project_sql_is_single_source(self):
        from app.services import facts_extractor as fe
        fe_blob = "\n".join(_module_code_strings(fe))
        assert fe_blob.count("SELECT project_id FROM schemes") == 1, \
            "反查 SQL 只允许出现在 SCHEME_PROJECT_ID_SQL 定义处"
        gf_blob = "\n".join(_module_code_strings(gf))
        assert "SELECT project_id FROM schemes" not in gf_blob, \
            "global_facts 不得再内联反查 SQL 副本"
        assert "SELECT project_id FROM global_facts" not in gf_blob, \
            "为取 project_id 而二次查 global_facts 的副本必须消失"

    def test_router_uses_the_single_exit_everywhere(self):
        assert _name_uses(gf, "_scheme_project_id") >= 7, \
            "反查出口至少在 7 处作用域/失效路径被消费（副本回退即红）"
        assert _name_uses(gf, "SCHEME_PROJECT_ID_SQL") >= 1
        assert _name_uses(gf, "row_field") >= 1

    def test_no_positional_project_id_extract_left(self):
        """位置索引取 project_id 的写法（``row[0]`` 系）不得在出口之外复发。"""
        import ast
        import inspect as _inspect
        bad: list[str] = []
        for node in ast.walk(ast.parse(_inspect.getsource(gf))):
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.slice, ast.Constant)
                    and node.slice.value == 0
                    and isinstance(node.value, ast.Name)
                    and node.value.id in {"prow", "srow", "scheme_row",
                                          "current", "row"}):
                bad.append(node.value.id)
        assert bad == [], f"残留位置索引取值：{bad}"
