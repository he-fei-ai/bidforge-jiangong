"""全局事实模块修复专项测试（2026-10-01）

覆盖本轮修复点的正向断言 + 「还原旧实现即失败」的反向断言：

1. ``facts_chapter_inject`` 死开关接线（``_chapter_inject_enabled`` 单一出口）
2. ``_invalidate_fact_scope_cache`` 项目级失效被首个空 id 行截断
3. 改名后九大章节归属 / 事实属性重派生（条目级 + 分组重建两条路径）
4. 手工事实新增路径的 INSERT 列清单收敛为单一事实源（三处共用 15 列）

A/B 反向验证：临时还原任一修复点（例如去掉 1318 行门控、把 318 行
``continue`` 改回 ``break``、删掉 ``_carry_dimensions`` 的 ``old_name`` 比较、
把三处调用改回手写 15 列 INSERT），本文件相应用例必须定向失败；恢复后全绿。
"""
from __future__ import annotations

import ast
import inspect
import json

from app.config import settings
from app.models import FactGroupIn, FactGroupUpdate, FactItem
from app.routers import global_facts as gf
from app.routers.sse_handlers import (
    _render_facts_text,
)
from app.services.facts_builder import _chapter_inject_enabled
from app.services.facts_classification import (
    classify_chapter_from_text,
    classify_fact_attr,
    # ✅ 2026-10-06（R45 · D1）：派生输入变更判据的唯一事实源
    derivation_inputs_changed,
)
from app.services.facts_extractor import normalize_key


def _insert_sql_tail() -> str:
    """取出手工新增 INSERT 的占位符部分（用于与列数比对）。"""
    head = "INSERT INTO global_facts ("
    assert gf.MANUAL_FACT_INSERT_SQL.startswith(head)
    return gf.MANUAL_FACT_INSERT_SQL.split("VALUES (", 1)[1].rstrip(")")


# =============================================================================
# Fix 1 · facts_chapter_inject 死开关
# =============================================================================
class TestChapterInjectSwitch:
    """开关必须真正控制「章节内事实前置」，且默认对齐既有实际行为。"""

    # 刻意把本章事实插在**中间**：能唯一区分「已前置」与「保持原序」
    # content 必须是真实事实行的 Markdown 形态（与 _build_fact_content 同口径），
    # 否则渲染结果不含列表标记，标题解析器拿不到任何条目。
    def _rows(self) -> list:
        return [
            ("基本信息", "基坑深度", "- **基坑深度**: 6.0m", 0.9, "overview"),
            ("工艺参数", "混凝土强度等级", "- **混凝土强度等级**: C30",
             0.9, "technique"),
            ("基本信息", "基坑平面尺寸", "- **基坑平面尺寸**: 30m×20m",
             0.9, "overview"),
        ]

    @staticmethod
    def _titles(text: str) -> list[str]:
        out = []
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("-"):
                body = s.lstrip("-").strip()
                out.append(body.split(":")[0].strip().strip("*").strip())
        return out

    def test_switch_on_prepends_current_chapter_facts(self, monkeypatch):
        monkeypatch.setattr(settings, "facts_chapter_inject", True)
        titles = self._titles(_render_facts_text(self._rows(), chapter="technique"))
        assert titles[0] == "混凝土强度等级"
        # 其余事实顺序保持不变（只是重排，不是筛选）
        assert titles[1:] == ["基坑深度", "基坑平面尺寸"]

    def test_switch_off_preserves_original_order(self, monkeypatch):
        """关掉开关必须回到「逐章注入全量事实、不排序」的旧顺序。"""
        monkeypatch.setattr(settings, "facts_chapter_inject", False)
        titles = self._titles(_render_facts_text(self._rows(), chapter="technique"))
        assert titles == ["基坑深度", "混凝土强度等级", "基坑平面尺寸"]

    def test_switch_off_never_drops_facts(self, monkeypatch):
        """重排是排序而非筛选：开关两种取值下事实条数必须一致。"""
        rows = self._rows()
        monkeypatch.setattr(settings, "facts_chapter_inject", True)
        n_on = len(self._titles(_render_facts_text(rows, chapter="technique")))
        monkeypatch.setattr(settings, "facts_chapter_inject", False)
        n_off = len(self._titles(_render_facts_text(rows, chapter="technique")))
        assert n_on == n_off == len(rows)

    def test_default_is_true_not_regressing(self):
        """默认值必须对齐 2026-09-27 起的实际行为，否则接通开关即等于关掉能力。"""
        assert settings.facts_chapter_inject is True
        assert _chapter_inject_enabled() is True

    def test_no_chapter_arg_never_reorders_regardless_of_switch(self, monkeypatch):
        for val in (True, False):
            monkeypatch.setattr(settings, "facts_chapter_inject", val)
            titles = self._titles(_render_facts_text(self._rows()))  # 不传 chapter
            assert titles == ["基坑深度", "混凝土强度等级", "基坑平面尺寸"]

    def test_render_reads_switch_through_single_exit(self):
        """静态护栏：渲染侧必须经 _chapter_inject_enabled() 门控，禁止裸读配置。

        用 AST 而非字符串匹配：函数注释里会提到配置项名（说明性文字），
        按字面量判定会误伤注释，AST 只看真实语法树，不会被注释绕开。
        """
        tree = ast.parse(inspect.getsource(_render_facts_text))
        helper_calls = 0
        settings_reads: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "_chapter_inject_enabled":
                helper_calls += 1
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "settings":
                settings_reads.append(node.attr)
        assert helper_calls >= 1, "渲染侧不再经 _chapter_inject_enabled() 门控"
        # 裸读 settings.facts_chapter_inject / getattr(settings, ...) 都是死开关形态
        assert "facts_chapter_inject" not in settings_reads
        assert not any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "getattr"
            and any(isinstance(a, ast.Constant)
                    and a.value == "facts_chapter_inject" for a in n.args)
            for n in ast.walk(tree)), "渲染侧绕过了单一出口直接读配置"

    def test_switch_helper_reads_named_config(self):
        """单一出口本身必须读的是这个配置项（改名即护栏失败）。"""
        src = inspect.getsource(_chapter_inject_enabled)
        assert "facts_chapter_inject" in src

    def test_switch_helper_fails_closed_to_true_on_config_error(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("配置层异常")

        monkeypatch.setattr(settings, "facts_chapter_inject", _boom)
        assert _chapter_inject_enabled() is True


# =============================================================================
# Fix 2 · 项目级缓存失效不再被首个空 id 行截断
# =============================================================================
class TestProjectScopeCacheInvalidationNotTruncated:
    """schemes.id 虽是主键，但 TEXT 主键不禁止 NULL：脏数据下必须跳过而非终止。"""

    async def _insert_scheme(self, db_conn, project_id: str, scheme_id):
        await db_conn.execute(
            "INSERT OR IGNORE INTO projects (id, name) VALUES (?, ?)",
            (project_id, "测试项目"))
        await db_conn.execute(
            "INSERT INTO schemes (id, project_id, name) VALUES (?, ?, ?)",
            (scheme_id, project_id, f"方案-{scheme_id or 'NULL'}"))
        await db_conn.commit()

    async def _fake_counter(self, monkeypatch, calls):
        async def fake_invalidate(db, sid, facts_touched=False):
            calls.append(sid)

        monkeypatch.setattr(gf, "invalidate_export_cache", fake_invalidate)

    async def test_null_id_row_does_not_truncate_loop(self, db_conn, monkeypatch):
        """NULL id 行排在中间时，其后的方案仍必须失效（旧实现 break 会漏掉）。"""
        calls: list[str] = []
        await self._fake_counter(monkeypatch, calls)
        pid = "proj-1"
        await self._insert_scheme(db_conn, pid, "s1")
        await self._insert_scheme(db_conn, pid, None)   # 脏数据
        await self._insert_scheme(db_conn, pid, "s2")

        await gf._invalidate_fact_scope_cache(db_conn, "", pid)
        assert calls == ["s1", "s2"], f"NULL id 行截断了后续方案失效: {calls}"

    async def test_null_id_row_first_still_invalidates_rest(self, db_conn, monkeypatch):
        """空 id 行恰好在最前时，后续方案仍必须被失效。

        这是旧实现 `if not sid: break` 的真正失血点：脏数据行排在第一时，
        整个项目下所有方案的导出缓存与 facts_updated_at 全部漏失效。
        （SQLite 的 SELECT id 按 rowid 排序且 NULL 排在末尾，无法用真实
        插入顺序构造，故直接给定 fetchall 结果，钉住「首个元素为空」这一
        判据分支。）
        """
        calls: list[str] = []
        await self._fake_counter(monkeypatch, calls)

        class _FakeCursor:
            """单元素游标：fetchall 一次返回给定行。"""

            def __init__(self, rows):
                self._rows = rows

            async def fetchall(self):
                return self._rows

        async def fake_execute(sql, *args):
            # 空 id 行排在最前、其后仍有正常方案：这是旧实现的失血点
            return _FakeCursor([[None], ["sA"]] if " FROM schemes " in sql else [])

        monkeypatch.setattr(db_conn, "execute", fake_execute)

        await gf._invalidate_fact_scope_cache(db_conn, "", "proj-null-first")
        assert calls == ["sA"], f"首个空 id 行截断了循环: {calls}"

    async def test_all_null_rows_are_silently_skipped(self, db_conn, monkeypatch):
        async def fake_invalidate(db, sid, facts_touched=False):
            raise AssertionError("空 id 行不应进入失效调用")

        monkeypatch.setattr(gf, "invalidate_export_cache", fake_invalidate)
        await self._insert_scheme(db_conn, "proj-2", None)
        await self._insert_scheme(db_conn, "proj-2", None)
        # 不抛异常即通过：脏数据不得让事实写操作整体失败
        await gf._invalidate_fact_scope_cache(db_conn, "", "proj-2")

    async def test_scheme_scoped_invalidates_single_scheme(self, db_conn, monkeypatch):
        calls: list[tuple] = []

        async def fake_invalidate(db, sid, facts_touched=False):
            calls.append((sid, facts_touched))

        monkeypatch.setattr(gf, "invalidate_export_cache", fake_invalidate)
        await gf._invalidate_fact_scope_cache(db_conn, "s9", "proj-3")
        assert calls == [("s9", True)]

    async def test_empty_project_id_is_noop(self, db_conn, monkeypatch):
        calls: list[str] = []
        await self._fake_counter(monkeypatch, calls)
        await gf._invalidate_fact_scope_cache(db_conn, "", "")
        assert calls == []


# =============================================================================
# Fix 3 · 改名后九大章节归属 / 事实属性重派生
# =============================================================================
# 用例名对的选择依据（2026-10-01 实测探测，保证断言真的能区分新旧实现）：
#   「混凝土强度等级」+ C30 + tech_param + parameter → chapter "overview"
#   「监测频率」    + C30 + tech_param + parameter → chapter "safety"（文本规则命中「监测」）
#   即 rename 让派生结果真正变化；若两值同为 "overview"，断言就无法区分修复前后。
class TestRenameRefreshesChapter:
    """name 变化同样是派生输入变化：chapter / fact_attr 必须重算。"""

    OLD_NAME = "混凝土强度等级"
    NEW_NAME = "监测频率"

    async def _seed_fact(self, db_conn, *, title=OLD_NAME,
                         content="- **混凝土强度等级**：C30",
                         category="tech_param", fact_type="parameter",
                         chapter="overview", fact_attr="qualitative",
                         scheme_id="", project_id="proj-x") -> str:
        """插入一条带历史章节标注的事实行，返回行 id。

        fact_attr 故意写成与当前派生结果不一致的 "qualitative"（实际派生为
        "quantitative"），以模拟「历史错标 + 派生输入已变」——此时若实现只沿用
        旧列值而重派生，断言即可区分。
        """
        fid = "f-rename-1"
        await db_conn.execute(
            "INSERT OR IGNORE INTO projects (id, name) VALUES (?, ?)",
            (project_id, "测试项目"))
        await db_conn.execute(
            "INSERT INTO global_facts (id, project_id, scheme_id, group_id, group_title,"
            " title, content, category, source_ref, is_simulated, confidence,"
            " is_resolved, has_conflict, conflict_keys, fact_key,"
            " chapter, fact_attr, fact_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0,"
            " 1.0, 1, 0, ?, ?, ?, ?, ?)",
            (fid, project_id, scheme_id, "g1", "工艺参数", title, content,
             category, json.dumps([{"file": "手动录入", "quote": ""}]),
             "", normalize_key(title), chapter, fact_attr, fact_type))
        await db_conn.commit()
        return fid

    @staticmethod
    async def _get_row(db_conn, fid: str) -> dict:
        cur = await db_conn.execute("SELECT * FROM global_facts WHERE id=?", (fid,))
        return dict(await cur.fetchone())

    async def test_item_update_rename_reclassifies_chapter(self, db_conn):
        """条目级更新：只改名（分类未变）也必须重算章节归属。"""
        fid = await self._seed_fact(db_conn)
        expected = classify_chapter_from_text(
            self.NEW_NAME, "C30", "tech_param", "parameter",
            normalize_key(self.NEW_NAME))
        assert expected != "overview", "用例名对必须让派生结果真正变化"

        n, _sid = await gf._apply_item_updates(
            db_conn, [{"fact_id": fid, "name": self.NEW_NAME}])
        assert n == 1
        row = await self._get_row(db_conn, fid)
        assert row["title"] == self.NEW_NAME
        assert row["content"] == "- **监测频率**: C30", "改名后 content 名称未同步"
        assert row["chapter"] == expected, "改名后章节归属仍是旧值"
        # fact_attr 也必须重派生（种子值是历史错标，正确结果为 quantitative）
        assert row["fact_attr"] == classify_fact_attr(self.NEW_NAME, "C30")

    async def test_item_update_same_name_keeps_stored_values(self, db_conn):
        """派生输入（分类/名称/**值**）全部未变时必须尊重库值，不得顺手重算。

        ⚠️ R45 修订：旧断言用「只改 value」来验证「输入未变」，
        但那恰好是 G1 缺陷——value 本身就是 classify_fact_attr 的输入，
        改值即派生输入变化。真正的「全未变」必须把值写成与库中一致的 C30。
        """
        fid = await self._seed_fact(db_conn, chapter="technique")
        n, _ = await gf._apply_item_updates(
            db_conn, [{"fact_id": fid, "value": "C30"}])
        assert n == 1
        row = await self._get_row(db_conn, fid)
        assert row["content"] == "- **混凝土强度等级**: C30"
        assert row["chapter"] == "technique", "输入未变却重算了章节归属"
        assert row["fact_attr"] == "qualitative", "输入未变却重算了事实属性"
        assert row["fact_key"] == normalize_key(self.OLD_NAME)

    async def test_item_update_value_change_reclassifies(self, db_conn):
        """⚠️ G1 收口（R45）：value 同样是派生输入——改值必须重算章节与属性。

        库里存着与派生结果不一致的历史标注（chapter="technique" /
        fact_attr="qualitative"）；把值从 C30 改成 C35 后，两者都必须
        回到派生值，否则会带着历史错标一直用到下一次重新提取。
        """
        fid = await self._seed_fact(db_conn, chapter="technique")
        n, _ = await gf._apply_item_updates(
            db_conn, [{"fact_id": fid, "value": "C35"}])
        assert n == 1
        row = await self._get_row(db_conn, fid)
        assert row["content"] == "- **混凝土强度等级**: C35"
        expected = classify_chapter_from_text(
            self.OLD_NAME, "C35", "tech_param", "parameter",
            normalize_key(self.OLD_NAME))
        assert row["chapter"] == expected, "改值后章节归属停在旧值"
        assert row["fact_attr"] == classify_fact_attr(self.OLD_NAME, "C35"), (
            "改值后事实属性停在旧值")
        assert row["fact_key"] == normalize_key(self.OLD_NAME)

    async def test_item_update_category_only_also_reclassifies(self, db_conn):
        """分类变化（既有能力）不能因本轮改名修复而退化。"""
        fid = await self._seed_fact(db_conn)
        n, _ = await gf._apply_item_updates(
            db_conn, [{"fact_id": fid, "category": "monitoring"}])
        assert n == 1
        row = await self._get_row(db_conn, fid)
        assert row["category"] == "monitoring"
        expected = classify_chapter_from_text(
            self.OLD_NAME, "C30", "monitoring", "parameter",
            normalize_key(self.OLD_NAME))
        assert expected == "safety", "用例前提：该分类应改变章节归属"
        assert row["chapter"] == expected

    async def test_new_fact_key_participates_in_derivation(self, db_conn):
        """改名后 fact_key 必须重算，且新键参与章节派生（旧实现传的是旧键）。"""
        fid = await self._seed_fact(db_conn)
        await gf._apply_item_updates(db_conn, [{"fact_id": fid, "name": self.NEW_NAME}])
        row = await self._get_row(db_conn, fid)
        assert row["fact_key"] == normalize_key(self.NEW_NAME)
        # 若实现误用旧键派生，结果会不同 —— 直接比对以新键为输入的派生值
        assert row["chapter"] == classify_chapter_from_text(
            self.NEW_NAME, "C30", "tech_param", "parameter",
            normalize_key(self.NEW_NAME))

    async def test_patch_rename_end_to_end(self, db_conn):
        """行级 PATCH 改标题（前端单条编辑弹窗）→ 章节归属端到端刷新。"""
        fid = await self._seed_fact(db_conn)
        out = await gf.update_fact(
            fid,
            FactGroupUpdate(id=fid, title=self.NEW_NAME),
            scheme_id="", db=db_conn)
        assert out.get("ok") is True
        row = await self._get_row(db_conn, fid)
        assert row["title"] == self.NEW_NAME
        assert row["chapter"] == "safety", "PATCH 改名后章节归属未刷新"
        assert row["fact_attr"] == classify_fact_attr(self.NEW_NAME, "C30")

    async def test_group_rebuild_reclassifies_on_category_change(self, db_conn):
        """分组重建路径：改组分类 → 组内各行章节归属重派生，溯源与键仍保留。"""
        await self._seed_fact(db_conn)
        out = await gf.update_fact(
            "g1",
            FactGroupUpdate(id="g1", content="- **混凝土强度等级**：C30",
                            category="monitoring"),
            scheme_id="", db=db_conn)
        assert out.get("ok") is True
        cur = await db_conn.execute(
            "SELECT title, category, chapter, fact_attr, fact_key, source_ref"
            " FROM global_facts WHERE group_id=?", ("g1",))
        rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 1
        row = rows[0]
        assert row["title"] == self.OLD_NAME
        assert row["category"] == "monitoring"
        assert row["chapter"] == "safety", "分组重建未按新分类重派生章节"
        assert row["fact_attr"] == classify_fact_attr(self.OLD_NAME, "C30")
        # 重派生不得顺带丢溯源与归一化键
        assert row["fact_key"] == normalize_key(self.OLD_NAME)
        assert json.loads(row["source_ref"]) == [{"file": "手动录入", "quote": ""}]

    def test_carry_dimensions_requires_all_three_inputs(self):
        """静态护栏：分组重建的「是否重派生」门控必须走单一事实源。

        ⚠️ R45 · D1 加固：旧锁断言的是「本函数内必须出现 category / name /
        value 三维字面量比较」—— 该断言本身固化了「门控写在本地」这一错误
        形态，正是三次漏改（2026-09-29 改分类 / 2026-10-01 改名 /
        2026-10-06 改值）的根因。现收敛为
        ``facts_classification.derivation_inputs_changed``，本锁改为断言
        「本函数必须调用单一出口」；三维完整性与「不得再写字面量比较」由
        test_facts_deep_audit_r45_20261006.py::TestDerivationGateSingleSource
        统一负责。
        """
        # ✅ R57：update_fact 不再直接调用 derivation_inputs_changed，
        #    判据 + 应用收敛到 _rederive_dimension_columns 单一出口。
        src = inspect.getsource(gf._rederive_dimension_columns)
        assert "derivation_inputs_changed(" in src, (
            "派生维度重算未调用唯一事实源 —— D1 分叉回流")
        # 判据本身必须覆盖三维（唯一出口只有一处，锁这一处即可全仓生效）
        gate_src = inspect.getsource(derivation_inputs_changed)
        for dim in ("old_category", "new_category", "old_name", "new_name",
                    "old_value", "new_value"):
            assert dim in gate_src, f"判据缺 {dim} 维度"
        # 旧值必须从库行 content 回解，而不是拿新值自比
        # ✅ R57：extract_value_from_markdown_line 调用在 _rederive_dimension_columns
        #    的调用方（update_fact / _apply_item_updates / resolve_conflict），
        #    而非 _rederive_dimension_columns 本身（它只接收 old_value 参数）。
        update_src = inspect.getsource(gf.update_fact)
        assert "extract_value_from_markdown_line(" in update_src, (
            "update_fact 未按旧行 content 回解出旧值")


# =============================================================================
# Fix 5 · 手工事实新增路径的 INSERT 列清单单一出口
# =============================================================================
class TestManualFactInsertSingleSource:
    """三条新增路径共用一份列清单，占位符由列数派生。"""

    def test_placeholder_count_derived_from_columns(self):
        cols = gf.MANUAL_FACT_INSERT_COLS
        assert _insert_sql_tail() == ",".join("?" for _ in cols)
        assert _insert_sql_tail().count("?") == len(cols)

    async def test_columns_match_db_schema(self, db_conn):
        """列清单必须是 global_facts 表真实存在的列，否则 INSERT 会 no such column。"""
        cur = await db_conn.execute("PRAGMA table_info(global_facts)")
        real = {r["name"] for r in await cur.fetchall()}
        missing = [c for c in gf.MANUAL_FACT_INSERT_COLS if c not in real]
        assert missing == [], f"列清单含不存在的列: {missing}"

    def test_manual_fact_row_positional_contract(self):
        row = gf._manual_fact_row(
            fid="id-1", pid="p-1", sid="s-1", group_id="g-1",
            group_title="组标题", name="基坑深度", content="- **基坑深度**: 6.0m",
            category="tech_param", source_file="招标文件", source_quote="深度6米")
        assert len(row) == len(gf.MANUAL_FACT_INSERT_COLS)
        got = dict(zip(gf.MANUAL_FACT_INSERT_COLS, row))
        assert got["id"] == "id-1" and got["project_id"] == "p-1"
        assert got["scheme_id"] == "s-1" and got["group_id"] == "g-1"
        assert got["group_title"] == "组标题"
        assert got["title"] == "基坑深度"
        assert got["content"] == "- **基坑深度**: 6.0m"
        assert got["category"] == "tech_param"
        assert got["has_conflict"] == 0 and got["conflict_keys"] == ""
        assert got["fact_key"] == normalize_key("基坑深度")
        assert json.loads(got["source_ref"]) == [
            {"file": "招标文件", "quote": "深度6米"}]

    def test_manual_fact_row_defaults_and_clamps(self):
        """类别兜底 other、置信度夹取、空名不产生空键。"""
        row = gf._manual_fact_row(
            fid="i", pid="p", sid="", group_id="g", group_title="t",
            name="基坑深度", content="c", category="   ",
            source_file="", source_quote="", confidence=9.0)
        got = dict(zip(gf.MANUAL_FACT_INSERT_COLS, row))
        assert got["category"] == "other"
        assert got["is_simulated"] == 0 and got["is_resolved"] == 1
        assert 0.0 <= got["confidence"] <= 1.0
        assert got["fact_key"], "空类别/空名也必须生成归一化键"

    def test_manual_fact_row_explicit_fact_key_wins(self):
        row = gf._manual_fact_row(
            fid="i", pid="p", sid="", group_id="g", group_title="t",
            name="基坑深度", content="c", category="tech_param",
            source_file="", fact_key="explicit_key")
        assert dict(zip(gf.MANUAL_FACT_INSERT_COLS, row))["fact_key"] == "explicit_key"

    async def test_create_fact_persists_manual_source_and_gate(self, db_conn):
        """端到端：手工新增写入「手动录入」来源标记，且模拟值闸门不变式成立。"""
        await db_conn.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)", ("p-1", "测试项目"))
        await db_conn.commit()
        gid = await gf.create_fact(
            FactGroupIn(title="施工参数", content="", items=[
                FactItem(name="基坑深度", value="6.0m", category="tech_param",
                         is_simulated=True, confidence=0.8),
            ]),
            project_id="p-1",
            db=db_conn,
        )
        assert gid["ok"] is True
        cur = await db_conn.execute(
            "SELECT * FROM global_facts WHERE group_id=?", (gid["id"],))
        rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 1
        row = rows[0]
        assert row["title"] == "基坑深度"
        assert row["fact_key"] == normalize_key("基坑深度")
        # 溯源标记必须落库（_is_protected 靠 source_ref 识别人工来源）
        assert json.loads(row["source_ref"]) == [{"file": "手动录入", "quote": ""}]
        # 模拟值闸门不变式：is_simulated=1 ⟹ is_resolved=0
        assert row["is_simulated"] == 1 and row["is_resolved"] == 0

    async def test_create_fact_legacy_single_line_splits_and_gates(self, db_conn):
        """旧版单条路径：多行 Markdown 逐行入库，模拟值闸门同样成立。"""
        await db_conn.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)", ("p-2", "测试项目"))
        await db_conn.commit()
        gid = await gf.create_fact(
            FactGroupIn(title="参数",
                        content="- **基坑深度**：6.0m\n- **混凝土强度等级**：C30",
                        category="tech_param"),
            project_id="p-2",
            db=db_conn,
        )
        cur = await db_conn.execute(
            "SELECT title, is_simulated, is_resolved, fact_key FROM global_facts"
            " WHERE group_id=?", (gid["id"],))
        rows = [dict(r) for r in await cur.fetchall()]
        assert sorted(r["title"] for r in rows) == ["基坑深度", "混凝土强度等级"]
        for r in rows:
            assert r["is_simulated"] == 0 and r["is_resolved"] == 1
            assert r["fact_key"] == normalize_key(r["title"])

    def test_no_hardcoded_manual_insert_columns_remain(self):
        """静态护栏：模块内不得再有「手写 15 列手工新增 INSERT」。"""
        tree = ast.parse(inspect.getsource(gf))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            val = node.value if isinstance(node.value, str) else ""
            # 手写 INSERT 必然同时出现列名清单与占位符串
            if ("INSERT INTO global_facts" in val and "conflict_keys" in val
                    and "?" in val):
                offenders.append(node.lineno)
        assert offenders == [], (
            f"仍存在手写手工新增 INSERT（应改走 MANUAL_FACT_INSERT_SQL）: 行 {offenders}")

    def test_all_three_creation_paths_use_single_sql(self):
        """create_fact 两条分支 + adjust_facts 的 add 分支都走单一出口。"""
        src = inspect.getsource(gf)
        assert src.count("MANUAL_FACT_INSERT_SQL") >= 4, (
            "SQL 常量定义 1 次 + 三处调用 3 次，实际 "
            f"{src.count('MANUAL_FACT_INSERT_SQL')} 次")
        assert src.count("_manual_fact_row(") >= 4, (
            "函数定义 1 次 + 三处调用 3 次，实际 "
            f"{src.count('_manual_fact_row(')} 次")




