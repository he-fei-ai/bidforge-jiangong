"""2026-09-27 全模块深度审查 · P0/P1 缺陷修复回归护栏（第二批：数据链）。

| 组 | 缺陷 | 修复位置 |
|---|---|---|
| T4 | 过期/模拟事实被当成确定值注入正文（SQL 漏 is_simulated/is_stale） | sse_handlers `_load_facts_rows` |
| T5 | 尾部事实整段消失（头部优先 break）→ 假性【待补充】 | sse_handlers `_render_facts_text` |
| T6 | 危大阈值判定用编造值/过期值 | global_facts `_load_fact_rows` |
| T8 | 内存 LRU 缓存键不含渲染器版本 | mermaid_renderer `_render_cache_key` |
| T9/T10 | 正文生成缺「方案名称主要施工内容」/ chapter 维度 | sse_handlers 正文上下文 |

护栏原则：反例必须**真的能跑通修复前的旧路径**，否则测试只是自证。
"""
import asyncio
import inspect


def _run(coro):
    # ✅ 2026-09-28：`asyncio.get_event_loop_policy().new_event_loop()` 属
    #    DeprecationWarning（Python 3.16 将移除 get_event_loop_policy 的该用法）；
    #    等价写法 `asyncio.new_event_loop()` 语义逐字一致（都用当前策略新建 loop）。
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _NullCur:
    """global_facts._load_fact_rows 里 `await cur.fetchall()` —— 必须是协程。"""


class _AsyncNullCur:
    async def fetchall(self):
        return []


# ============================================================
# T4 正文注入门控：模拟值 / 过期值不得进正文
# ============================================================

class TestFactsInjectGate:
    """T4：正文生成的事实门控必须与 facts_extractor 单一事实源完全一致。

    回归前 `_load_facts_rows` 的两条 SQL 都只写
    `has_conflict=0 AND is_resolved=1`，漏掉 `is_simulated=0` 与 `is_stale=0`
    —— AI 编造值与「已被重新提取取代」的过期值会被当成项目确定事实喂给正文。
    """

    def test_single_source_of_truth_contains_gate(self):
        from app.services.facts_extractor import _FACTS_INJECT_WHERE
        assert "is_simulated=0" in _FACTS_INJECT_WHERE
        assert "is_stale=0" in _FACTS_INJECT_WHERE

    def _capture_sql(self, with_project: bool) -> str:
        from app.routers.sse_handlers import _load_facts_rows
        seen: list[str] = []

        class _Db:
            async def execute(self, sql, params=()):
                s = str(sql)
                seen.append(s)
                if "FROM schemes" in s:
                    class _R:
                        async def fetchone(self_inner):
                            return ("p1",) if with_project else None
                    return _R()
                return _NullCur()

        _run(_load_facts_rows(_Db(), "s1"))
        return seen[-1]

    def test_project_branch_filtered(self):
        sql = self._capture_sql(True)
        assert "is_simulated=0" in sql, "正文注入 SQL 漏了 is_simulated 门控"
        assert "is_stale=0" in sql, "正文注入 SQL 漏了 is_stale 门控"

    def test_scheme_only_branch_filtered(self):
        """反例：两条分支都要过滤（回归前若只改一处，另一处仍漏）。"""
        sql = self._capture_sql(False)
        assert "is_simulated=0" in sql and "is_stale=0" in sql

    def test_reuses_single_source_constant(self):
        """不得另写一份字面量（避免再次分叉）。"""
        from app.routers.sse_handlers import _load_facts_rows
        assert "_FACTS_INJECT_WHERE" in inspect.getsource(_load_facts_rows)

    def test_missing_table_degrades_gracefully(self):
        """反例：global_facts 表缺失时必须降级为空，绝不阻断生成。"""
        from app.routers.sse_handlers import _load_facts_rows

        class _Db:
            async def execute(self, sql, params=()):
                raise RuntimeError("no such table: global_facts")

        assert _run(_load_facts_rows(_Db(), "s1")) == []

# ============================================================
# T5 事实预算：按比例分配，不得头部优先整段丢弃
# ============================================================

class TestFactsBudgetProportional:
    """T5：尾部事实整段消失 → AI 判定「事实缺失」→ 大量假性【待补充】。

    回归前是头部优先的 `break`：`total+len(fact) > max_total` 即跳出循环，
    后面的事实在提示词里完全不可见。修复后改为复用 `_allocate_char_budgets`
    按比例分配（与目录生成侧 `_budgeted_truncate_sections` 同一口径）。
    """

    @staticmethod
    def _rows(n, body_len, group="工程概况"):
        return [(group, f"事实{i}", "甲" * body_len, 0.9) for i in range(n)]

    def test_all_facts_survive_over_budget(self):
        from app.routers.sse_handlers import _render_facts_text
        out = _render_facts_text(self._rows(40, 300), max_total=2000, per_fact=300)
        assert out.count("甲") >= 40, "尾部事实被整段丢弃"

    def test_under_budget_is_byte_identical_to_legacy(self):
        """未超预算时必须与旧实现逐字一致（零回归）。"""
        from app.routers.sse_handlers import _render_facts_text
        rows = [("工程概况", "t0", "第一条内容", 0.9),
                ("工程概况", "t1", "第二条内容", 0.9),
                ("验收要求", "t2", "第三条内容", 0.9)]
        out = _render_facts_text(rows, max_total=6000, per_fact=300)
        assert out == ("### 工程概况\n第一条内容\n第二条内容\n"
                       "### 验收要求\n第三条内容\n")

    def test_group_header_never_truncated(self):
        """反例：不得产出「### 基坑支」这种半截组标题。"""
        from app.routers.sse_handlers import _render_facts_text
        out = _render_facts_text([("工程概况", "t", "甲" * 500)],
                                 max_total=6000, per_fact=300)
        assert "### 工程概况\n" in out

    def test_low_confidence_annotation_preserved(self):
        from app.routers.sse_handlers import (
            _render_facts_text, LOW_CONFIDENCE_THRESHOLD)
        rows = [("工程概况", "t", "基坑深度 12.5m",
                 LOW_CONFIDENCE_THRESHOLD - 0.1)]
        assert "低置信度" in _render_facts_text(rows, max_total=6000, per_fact=300)

    def test_legacy_3tuple_rows_still_supported(self):
        """反例：历史 3 元组行（无 confidence）不得崩。"""
        from app.routers.sse_handlers import _render_facts_text
        assert _render_facts_text([("工程概况", "t", "内容")], max_total=6000, per_fact=300) == \
            "### 工程概况\n内容\n"

    def test_empty_rows(self):
        from app.routers.sse_handlers import _render_facts_text
        assert _render_facts_text([], max_total=100, per_fact=10) == ""

    def test_chapter_preorder_keeps_all_facts(self):
        """chapter 前置是「重排」不是「筛选」——契约：绝不丢事实。"""
        from app.routers.sse_handlers import _render_facts_text
        rows = [("G1", "t0", "甲组内容", 0.9, "technique"),
                ("G2", "t1", "乙组内容", 0.9, "safety"),
                ("G3", "t2", "丙组内容", 0.9, "technique")]
        out = _render_facts_text(rows, max_total=6000, per_fact=300,
                                 chapter="technique")
        assert "甲组内容" in out and "乙组内容" in out and "丙组内容" in out
        assert out.index("甲组内容") < out.index("乙组内容")



    def test_total_stays_within_budget(self):
        from app.routers.sse_handlers import _render_facts_text
        out = _render_facts_text(self._rows(30, 400), max_total=1500, per_fact=300)
        assert len(out) <= 1500

    def test_unknown_chapter_is_noop(self):
        """反例：chapter 匹配不到时必须与不传完全一致（零回归）。"""
        from app.routers.sse_handlers import _render_facts_text
        rows = [("G1", "t0", "甲组内容", 0.9, "technique")]
        assert (_render_facts_text(rows, max_total=6000, per_fact=300, chapter="zzz")
                == _render_facts_text(rows, max_total=6000, per_fact=300))

# ============================================================
# T6 危大判定：不得使用模拟值 / 过期值
# ============================================================

class TestDangerCheckFiltering:
    """T6：「超过一定规模」是给监管看的确定性结论，不能用不确定数据算。"""

    def _sqls(self):
        import app.routers.global_facts as gf
        seen: list[str] = []

        class _Db:
            async def execute(self, sql, params=()):
                seen.append(str(sql))
                return _AsyncNullCur()

        async def _validate(*a, **k):
            return "s1", "p1"

        orig = gf._validate_fact_scope
        gf._validate_fact_scope = _validate
        try:
            _run(gf._load_fact_rows(_Db(), "s1", "p1", injectable_only=True))
            _run(gf._load_fact_rows(_Db(), "s1", "p1", injectable_only=False))
        finally:
            gf._validate_fact_scope = orig
        return seen

    def test_injectable_only_adds_gate(self):
        filtered, _plain = self._sqls()
        assert "is_simulated=0" in filtered and "is_stale=0" in filtered

    def test_admin_list_not_filtered_by_default(self):
        """反例：管理端列表必须仍能看到待裁决行供人工处理。"""
        _filtered, plain = self._sqls()
        assert "is_simulated=0" not in plain

    def test_danger_check_endpoint_uses_filtered_query(self):
        """反例：danger-check 必须传 injectable_only=True。"""
        import app.routers.global_facts as gf
        assert "injectable_only=True" in inspect.getsource(gf)


# ============================================================
# T8 内存 LRU 缓存键必须含渲染器版本
# ============================================================

class TestRenderCacheVersion:
    """T8：升级渲染器后内存 LRU 会命中旧 PNG，磁盘版本机制被完全绕过，
    旧图还会被「转正」写入新版本槽位，直到进程重启才恢复。"""

    def test_cache_key_contains_version(self):
        from app.services.ai import mermaid_renderer as mr
        key = mr._render_cache_key("graph TD\n A-->B", "flowchart",
                                   90, "天", 5, False, True)
        assert key[0] == f"v{mr._RENDERER_VERSION}"

    def test_version_change_invalidates_key(self, monkeypatch):
        from app.services.ai import mermaid_renderer as mr
        args = ("graph TD\n A-->B", "flowchart", 90, "天", 5, False, True)
        k1 = mr._render_cache_key(*args)
        monkeypatch.setattr(mr, "_RENDERER_VERSION", mr._RENDERER_VERSION + 1)
        assert mr._render_cache_key(*args) != k1

    def test_single_version_definition(self):
        """版本号只能定义一次（两处定义会隐式分叉）。"""
        from app.services.ai import mermaid_renderer as mr
        assert inspect.getsource(mr).count("_RENDERER_VERSION = ") == 1

    def test_lru_roundtrip_still_works(self):
        from io import BytesIO

        from PIL import Image
        from app.services.ai import mermaid_renderer as mr
        mr.clear_render_cache()
        key = mr._render_cache_key("graph TD\n A-->B", "flowchart",
                                   90, "天", 5, False, True)
        assert mr._render_cache_get(key) is None
        buf = BytesIO()
        Image.new("RGB", (400, 400), "white").save(buf, format="PNG")
        mr._render_cache_put(key, buf)
        got = mr._render_cache_get(key)
        assert got is not None and len(got.getvalue()) > 100
        mr.clear_render_cache()


# ============================================================
# T9 / T10 正文生成「四项依据」完整性
# ============================================================

class TestContentFourBasis:
    """T9：正文生成必须注入「专项方案名称包含的主要施工内容」。

    回归前：content_generation_system 模板写着「若下文给出
    【方案名称主要施工内容】，本章内容必须服务于其中对应的施工内容项」，
    但**正文生成链路从未注入该区块**（只有目录生成在用）—— AI 只能从方案
    名称字面自行推测本章写什么，这是「章节与方案名称脱节 / 大量【待补充】」
    的上游根因之一。

    T10：九大章节 chapter 维度此前也完全没接进正文（调用点未传 chapter）。
    """

    def test_scope_helper_is_shared_with_outline(self):
        """单一实现：正文与目录生成必须复用同一解析器（services/scheme_scope）。"""
        from app.routers.sse_handlers import _outline_construction_scope
        assert "app.services.scheme_scope" in inspect.getsource(
            _outline_construction_scope)

    def test_scope_helper_returns_text_for_real_scheme_name(self):
        """反例：确认它对真实方案名确实能拆出条目（否则注入是空转）。"""
        from app.routers.sse_handlers import _outline_construction_scope
        out = _outline_construction_scope(
            {"name": "深基坑开挖及支护专项施工方案", "type": "危大工程"})
        assert out, "真实方案名应能拆出主要施工内容"

    def test_scope_helper_empty_when_unparseable(self):
        from app.routers.sse_handlers import _outline_construction_scope
        assert _outline_construction_scope({"name": "", "type": ""}) == ""

    def test_chapter_key_lookup_is_total(self):
        """反例：未命中必须返回空串（不得抛异常阻断逐章生成）。"""
        from app.routers.sse_handlers import chapter_key_of_title
        assert chapter_key_of_title("") == ""
        assert chapter_key_of_title("毫不相干的标题") == ""
        assert chapter_key_of_title("施工安全保证措施") == "safety"

    def test_content_generation_passes_chapter(self):
        """T10：正文调用点必须传 chapter（九大章节分类接进正文的唯一通道）。"""
        from app.routers.sse_handlers import generate_content
        assert "chapter=chapter_key_of_title" in inspect.getsource(
            generate_content)