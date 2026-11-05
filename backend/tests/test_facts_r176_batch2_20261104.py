"""事实模块第二批修复护栏（R176 · 2026-11-04）。

覆盖本轮 3 个后端项 + 1 个前端项对应的后端契约：

  L6 / BUG-11  ``_render_facts_text`` 组标题超预算时未计入 audit_out →
               ``injected + dropped`` 覆盖不到输入行集（事实静默消失）
  L8 / BUG-13  ``/chapters`` 章条目缺 FactsChapterStat 契约键
               （facts / fields / covered / uncategorized）
  L2 / BUG-06  ``list_facts`` offset 越界未 clamp → 返回空页，
               界面表现为「事实全没了」；须回传实际生效 offset

纪律：每项都锚定**真实函数/真实路由**的返回，不锚定注释或自我声明。
"""

from __future__ import annotations

import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)


# =========================================================================
# L6 / BUG-11：注入预算审计账本恒等式
# =========================================================================
class TestInjectionLedger:
    """账本恒等式：``len(injected_titles) + len(budget_dropped) == rows_total``。

    该恒等式是「事实到底进了提示词没有」的唯一可信来源（R166 FIX-10 引入）。
    任何新增的丢弃分支若不写 audit_out，恒等式立刻被打破 —— 本组即为此设卡。
    """

    @staticmethod
    def _rows(n: int, *, conf=None, body_len: int = 30, groups: int = 1):
        rows = []
        for i in range(n):
            gt = f"组{i % groups}"
            rows.append((gt, f"事实{i}", "值" * body_len, conf))
        return rows

    @staticmethod
    def _audit(rows, **kw):
        from app.services.facts_builder import fact_injection_audit

        return fact_injection_audit(rows, **kw)

    def test_identity_across_budget_matrix(self):
        """预算从「绰绰有余」压到「远不够」：账本必须始终闭合。"""
        cases = [
            (1, {}),
            (3, {}),
            (12, {"max_total": 6000}),
            (12, {"max_total": 600}),
            (12, {"max_total": 120}),
            (30, {"max_total": 200, "per_fact": 50}),
            (30, {"max_total": 80, "per_fact": 20}),
            (6, {"max_total": 300, "per_fact": 200, "conf": 0.3}),
            (20, {"max_total": 400, "per_fact": 100, "groups": 5}),
        ]
        for n, kw in cases:
            conf = kw.pop("conf", None)
            groups = kw.pop("groups", 1)
            rows = self._rows(n, conf=conf, groups=groups)
            out = self._audit(rows, **kw)
            assert len(out["injected_titles"]) + len(out["budget_dropped"]) == out["rows_total"], (
                f"账本不闭合 n={n} kw={kw}: injected={len(out['injected_titles'])} "
                f"dropped={len(out['budget_dropped'])} total={out['rows_total']}"
            )
            # 同一条不得既算注入又算丢弃
            assert not (set(out["injected_titles"]) & set(out["budget_dropped"])), f"同一事实重复记账 n={n} kw={kw}"

    def test_group_header_overflow_is_audited(self):
        """BUG-11 反向用例：组标题挤爆预算的整行必须进 dropped。

        构造（预算 40 字）：组标题 8 字 + 低置信度前缀 21 字 ⇒ 第 1 行写入后
        total 已达上限，第 2 组标题 ``40 + 8 >= 40`` 必然进「超预算整组跳过」分支。
        修复前该行既不渲染、也不进 audit_out（静默消失）。
        """
        rows = [("AAA1", "事实1", "值" * 30, 0.3), ("BBB2", "事实2", "值" * 30, 0.3)]
        out = self._audit(rows, max_total=40, per_fact=300)
        assert out["budget_dropped"] == ["事实2"], out["budget_dropped"]
        assert out["injected_titles"] == ["事实1"], out["injected_titles"]
        # 该组标题不得以半截形态出现在正文（旧实现的另一处风险）
        assert "### BBB2" not in out["rendered_text"]
        assert out["rendered_text"].rstrip().endswith("值"), out["rendered_text"]

    def test_header_overflow_row_absent_from_rendered_text(self):
        """被 dropped 的组不得以半截标题出现在正文（留痕与实际渲染同源）。"""
        rows = [("AAA1", "事实1", "值" * 30, 0.3), ("BBB2", "事实2", "值" * 30, 0.3)]
        out = self._audit(rows, max_total=40, per_fact=300)
        text = out["rendered_text"]
        assert "### AAA1" in text
        assert "BBB2" not in text  # 整组跳过，且不是 "### BB" 半截形态
        assert out["budget_dropped"] == ["事实2"]


# =========================================================================
# L8 / BUG-13：/chapters 章条目契约（加法式）
# =========================================================================
_SEEDS = [
    ("项目经理", "张三", "personnel"),
    ("基坑开挖深度", "4.2m", "tech_param"),
    ("验收标准", "GB 50202-2018", "acceptance"),
]


async def _seed(db, project_id: str = "r176p", sid: str = "") -> None:
    from app.routers import global_facts as gf

    await db.execute("INSERT OR REPLACE INTO projects (id, name) VALUES (?, ?)", (project_id, "R176"))
    for i, (name, value, cat) in enumerate(_SEEDS):
        await db.execute(
            gf.MANUAL_FACT_INSERT_SQL,
            gf._manual_fact_row(
                fid=f"r176f{i}",
                pid=project_id,
                sid=sid,
                group_id="r176g",
                group_title="工程概况",
                name=name,
                content=gf._build_fact_content(name, value, False),
                category=cat,
                source_file="测试录入",
                is_simulated=False,
                confidence=1.0,
                is_resolved=True,
            ),
        )
    await db.commit()


class TestChaptersContract:
    """``/chapters`` 章条目必须齐备 FactsChapterStat 键，且旧键零删除。"""

    async def test_chapter_entries_carry_contract_keys(self, db_conn):
        from app.routers import global_facts as gf

        await _seed(db_conn)
        res = await gf.list_facts_by_chapters("", "r176p", db=db_conn)
        assert len(res["chapters"]) == 9
        for ch in res["chapters"]:
            for key in (
                "order",
                "key",
                "title",
                "count",
                "coverage",
                "missing_fields",
                "total_fields",
                "covered_fields",
                "shared_count",
                "items",
            ):
                assert key in ch, f"旧键 {key} 被删除（回归）"
            for key in ("facts", "fields", "covered", "uncategorized"):
                assert key in ch, f"缺 FactsChapterStat 键 {key}"

    async def test_chapter_key_values_are_consistent(self, db_conn):
        """新键不是占位：facts/count/items 三者同源，covered = len(covered_fields)。"""
        from app.routers import global_facts as gf

        await _seed(db_conn)
        res = await gf.list_facts_by_chapters("", "r176p", db=db_conn)
        for ch in res["chapters"]:
            assert ch["facts"] == len(ch["items"]), ch["key"]
            assert ch["facts"] == ch["count"], ch["key"]
            assert ch["covered"] == len(ch["covered_fields"]), ch["key"]
            assert isinstance(ch["covered"], int) and not isinstance(ch["covered"], bool)
            assert isinstance(ch["uncategorized"], int)
            assert ch["uncategorized"] <= ch["facts"], ch["key"]

    async def test_seeded_fact_lands_in_chapter_with_facts(self, db_conn):
        """防「形状对了但恒 0」：种子事实必须真的计入某章 facts。"""
        from app.routers import global_facts as gf

        await _seed(db_conn)
        res = await gf.list_facts_by_chapters("", "r176p", db=db_conn)
        assert sum(ch["facts"] for ch in res["chapters"]) >= len(_SEEDS)
        overview = next(ch for ch in res["chapters"] if ch["key"] == "overview")
        assert overview["facts"] >= 1


# =========================================================================
# L2 / BUG-06：offset 越界 clamp（后端半边）
# =========================================================================
async def _seed_groups(db, n_groups: int, project_id: str = "r176q", sid: str = "") -> None:
    from app.routers import global_facts as gf

    await db.execute("INSERT OR REPLACE INTO projects (id, name) VALUES (?, ?)", (project_id, "R176Q"))
    for g in range(n_groups):
        for k in range(2):
            await db.execute(
                gf.MANUAL_FACT_INSERT_SQL,
                gf._manual_fact_row(
                    fid=f"r176q{g}_{k}",
                    pid=project_id,
                    sid=sid,
                    group_id=f"r176qg{g}",
                    group_title=f"分组{g}",
                    name=f"事实{g}-{k}",
                    content=gf._build_fact_content(f"事实{g}-{k}", "值", False),
                    category="other",
                    source_file="测试录入",
                    is_simulated=False,
                    confidence=1.0,
                    is_resolved=True,
                ),
            )
    await db.commit()


class TestOffsetClamp:
    """切方案后前端沿用旧页码 → 越界 offset 必须落到最后一页并回传生效值。"""

    async def test_out_of_range_offset_clamps_to_last_page(self, db_conn):
        from app.routers import global_facts as gf

        await _seed_groups(db_conn, 5)
        res = await gf.list_facts("", "r176q", 2, 99, db=db_conn)
        pg = res["pagination"]
        assert pg["total_groups"] == 5
        assert pg["requested_offset"] == 99
        assert pg["offset"] == 4, pg  # 最后一页首行 = (5-1)//2*2
        assert pg["returned"] == 1, pg  # 不是空页
        assert len(res["groups"]) == 1

    async def test_in_range_offset_is_untouched(self, db_conn):
        """零回归：未越界时 offset 原样透传，前端既有分页逻辑不受影响。"""
        from app.routers import global_facts as gf

        await _seed_groups(db_conn, 5)
        res = await gf.list_facts("", "r176q", 2, 1, db=db_conn)
        pg = res["pagination"]
        assert pg["offset"] == 1 and pg["requested_offset"] == 1
        assert pg["returned"] == 2

    async def test_empty_scope_offset_zeroed(self, db_conn):
        from app.routers import global_facts as gf

        res = await gf.list_facts("", "r176q", 2, 5, db=db_conn)
        pg = res["pagination"]
        assert pg["offset"] == 0 and pg["returned"] == 0
        assert res["groups"] == []
