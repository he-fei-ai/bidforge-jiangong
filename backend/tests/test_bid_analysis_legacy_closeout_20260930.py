"""解析提取模块 · 遗留项收口护栏（2026-09-30 第十五轮）

第十一轮 §4.15.5 记录的遗留项，本轮**全部收口**：

============================  ==========================================
遗留项                          本轮落点
============================  ==========================================
P1 ``section_hint`` 拼进同一条    ``bid_analysis_service.build_system_messages``
    system 消息（与 docstring       —— 标段上下文作为**独立第二条 system
    声称「独立消息」不符）          消息**（对齐易标 buildTenderContextMessages）
P1 ``format_downstream_context``  ``_apply_downstream_budget``（两级预算裁剪）
    无预算上限
P2 生成链不读截断诊断              ``global_facts.load_parsed_docs`` 三层降级
    （目录 / 正文侧）
P2 提取项重跑级联失效不全          ``_invalidate_extraction_derived``（三处补齐）
============================  ==========================================

另记录两条**经核实后判定为非缺陷**的项（见 ``TestNotDefects``），
避免后续重复排查。
"""
from __future__ import annotations

import asyncio
import inspect
import sqlite3

import pytest

from app.routers import bid_analysis as ba
from app.routers import global_facts as gf
from app.routers import sse_handlers as sh
from app.services import bid_analysis_service as svc


# =========================================================================
# 一、P1 · 标段上下文改为独立第二条 system 消息（易标口径）
# =========================================================================
class TestSectionHintAsSeparateSystemMessage:
    def test_no_hints_returns_single_system_byte_identical(self):
        """无 hint → 单条 system，内容与 build_system_prompt 逐字一致。"""
        msgs = svc.build_system_messages()
        assert len(msgs) == 1
        assert msgs[0]["content"] == svc.build_system_prompt()
        assert msgs[0]["content"] == svc.STABLE_SYSTEM_PROMPT
        assert svc.build_system_messages("", "")[0]["content"] == \
            svc.STABLE_SYSTEM_PROMPT

    def test_section_hint_is_independent_second_system(self):
        """标段上下文必须是**独立第二条** system 消息。"""
        msgs = svc.build_system_messages("二标段")
        assert len(msgs) == 2
        assert all(m["role"] == "system" for m in msgs)
        # 第一条必须是原样通用提示词（标段上下文不得再拼进去）
        assert msgs[0]["content"] == svc.STABLE_SYSTEM_PROMPT
        assert "二标段" in msgs[1]["content"]
        assert "二标段" not in msgs[0]["content"]

    def test_classification_hint_stays_concatenated(self):
        """危大分类结论仍拼在第一条（它与通用纪律同属「提取要求」）。"""
        msgs = svc.build_system_messages("", "基坑工程")
        assert len(msgs) == 1
        assert "基坑工程" in msgs[0]["content"]
        assert msgs[0]["content"] == svc.build_system_prompt("", "基坑工程")

    def test_legacy_build_system_prompt_unchanged(self):
        """历史契约函数行为不变（既有单测锁定）。"""
        assert svc.build_system_prompt("") == svc.STABLE_SYSTEM_PROMPT
        out = svc.build_system_prompt("二标段")
        assert "【当前处理标段上下文】二标段" in out

    def test_both_call_sites_use_new_builder(self):
        """两处调用点（单项 + 合并）都必须走 build_system_messages。"""
        src = inspect.getsource(ba._run_single_call)
        assert "build_system_messages(" in src
        assert "build_system_prompt(" not in src
        assert "*system_msgs" in src
        src2 = inspect.getsource(ba._run_single_item)
        assert "build_system_messages(" in src2

    def test_import_declared(self):
        assert "build_system_messages" in inspect.getsource(ba)[:2000]


# =========================================================================
# 二、P1 · format_downstream_context 预算上限
# =========================================================================
class TestDownstreamBudget:
    def _items(self, n=12, size=9000):
        return {d["item_id"]: {"status": "success", "content": "A" * size,
                               "output_type": d.get("output_type", "markdown"),
                               "label": d["label"]}
                for d in svc.ANALYSIS_ITEMS[:n]}

    def test_budget_constants_positive(self):
        assert svc.DOWNSTREAM_CONTEXT_MAX_CHARS > 0
        assert svc.DOWNSTREAM_PER_ITEM_MAX_CHARS > 0

    def test_large_input_is_capped(self):
        out = svc.format_downstream_context(self._items())
        assert len(out) <= svc.DOWNSTREAM_CONTEXT_MAX_CHARS + 200

    def test_capping_is_transparent(self):
        """裁剪必须留下可见痕迹（用户/下游能知道「还有内容没下发」）。"""
        out = svc.format_downstream_context(self._items())
        assert "超出下发预算" in out or "本项已截断" in out

    def test_small_input_untouched(self):
        """小输入必须逐字不变（不引入无谓的裁剪标记）。"""
        items = {d["item_id"]: {"status": "success", "content": "内容X",
                               "output_type": d.get("output_type", "markdown"),
                               "label": d["label"]}
                 for d in svc.ANALYSIS_ITEMS[:2]}
        out = svc.format_downstream_context(items)
        assert "已截断" not in out and "超出下发预算" not in out
        assert "内容X" in out

    def test_first_sections_always_kept(self):
        """靠前的权威项必须完整保留（预算不够时先丢靠后的）。"""
        out = svc.format_downstream_context(self._items(n=6, size=5000))
        assert svc.ANALYSIS_ITEMS[0]["label"] in out

    def test_helper_direct(self):
        assert svc._apply_downstream_budget("") == ""
        short = "a" * 100
        assert svc._apply_downstream_budget(short) == short
        capped = svc._apply_downstream_budget("x" * 5000, max_chars=1000,
                                              per_item=0)
        assert len(capped) <= 1100


# =========================================================================
# 三、P2 · load_parsed_docs 读截断诊断（目录 / 正文侧）
# =========================================================================
def _db(with_diag=True):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE project_documents (id TEXT PRIMARY KEY, project_id TEXT,"
              " file_name TEXT, parsed_markdown TEXT, created_at TEXT DEFAULT '')")
    if with_diag:
        c.execute("ALTER TABLE project_documents ADD COLUMN parse_warnings TEXT DEFAULT ''")
        c.execute("ALTER TABLE project_documents ADD COLUMN parse_truncated INTEGER DEFAULT 0")
    c.commit()
    return c


_SEQ = [0]


def _add(c, name, text, warn="", trunc=0):
    """插入一行。用递增序号而非 hash（hash 受 PYTHONHASHSEED 影响、顺序不确定）。"""
    _SEQ[0] += 1
    c.execute("INSERT INTO project_documents(id,project_id,file_name,parsed_markdown,"
              "parse_warnings,parse_truncated) VALUES(?,?,?,?,?,?)",
              (f"r{_SEQ[0]}", "p", name, text, warn, trunc))
    c.commit()


class _Shim:
    class _Cur:
        def __init__(self, cur):
            self._c = cur

        async def fetchall(self):
            return self._c.fetchall()

        async def fetchone(self):
            return self._c.fetchone()

    def __init__(self, c):
        self._c = c

    async def execute(self, sql, params=()):
        return self._Cur(self._c.execute(sql, params))


class TestLoadParsedDocsTruncation:
    def test_backfills_truncated_out(self):
        """传 truncated_out 时，被截断文档必须回传。"""
        c = _db()
        _add(c, "a.md", "正文A")
        _add(c, "b.md", "正文B", "已截断", 1)
        out: list = []
        pairs = asyncio.run(gf.load_parsed_docs(_Shim(c), "p", None, out))
        assert [n for n, _t in pairs] == ["a.md", "b.md"]
        assert out == ["b.md"]

    def test_warns_even_without_out_param(self, caplog):
        """不传 truncated_out 也必须记 WARNING —— 可观测性不依赖调用方传参。"""
        c = _db()
        _add(c, "b.md", "正文", "已截断", 1)
        with caplog.at_level("WARNING", logger="app.routers.global_facts"):
            asyncio.run(gf.load_parsed_docs(_Shim(c), "p"))
        assert any("被截断的文档" in r.message for r in caplog.records)

    def test_no_truncation_no_warning(self, caplog):
        c = _db()
        _add(c, "a.md", "正文")
        with caplog.at_level("WARNING", logger="app.routers.global_facts"):
            asyncio.run(gf.load_parsed_docs(_Shim(c), "p"))
        assert not any("被截断的文档" in r.message for r in caplog.records)

    def test_fallback_when_diag_cols_absent(self):
        """旧库无诊断列 → 只取基础两列（= 引入前行为）。"""
        c = _db(with_diag=False)
        c.execute("INSERT INTO project_documents(id,project_id,file_name,parsed_markdown)"
                  " VALUES('1','p','a.md','正文A')")
        c.commit()
        out: list = []
        pairs = asyncio.run(gf.load_parsed_docs(_Shim(c), "p", None, out))
        assert pairs == [("a.md", "正文A")]
        assert out == []

    def test_truncation_does_not_drop_document(self):
        """截断**不阻止使用**：残缺依据仍比没有依据好。"""
        c = _db()
        _add(c, "b.md", "残缺正文", "已截断", 1)
        assert asyncio.run(gf.load_parsed_docs(_Shim(c), "p")) == \
            [("b.md", "残缺正文")]

    def test_limit_still_applied(self):
        c = _db()
        for i in range(5):
            _add(c, f"f{i}.md", "正文")
        assert len(asyncio.run(gf.load_parsed_docs(_Shim(c), "p", 2))) == 2

    def test_load_parsed_texts_passes_through(self):
        c = _db()
        _add(c, "b.md", "正文", "已截断", 1)
        out: list = []
        assert asyncio.run(gf.load_parsed_texts(_Shim(c), "p", 5, out)) == ["正文"]
        assert out == ["b.md"]

    def test_query_failure_degrades(self):
        class Boom:
            async def execute(self, *a, **k):
                raise RuntimeError("db down")
        assert asyncio.run(gf.load_parsed_docs(Boom(), "p")) == []



# =========================================================================
# 四、P2 · 提取项重跑的级联失效补齐
# =========================================================================
class TestInvalidationCascade:
    def _db(self):
        c = sqlite3.connect(":memory:")
        c.executescript("""
        CREATE TABLE consistency_scan_cache (scheme_id TEXT, payload TEXT);
        CREATE TABLE schemes (id TEXT PRIMARY KEY, facts_updated_at TEXT DEFAULT '');
        CREATE TABLE doc_extractions (extraction_id TEXT PRIMARY KEY,
            project_id TEXT, extract_type TEXT, status TEXT);
        INSERT INTO consistency_scan_cache VALUES ('s1','x'),('s1','y'),('s2','z');
        INSERT INTO schemes VALUES ('s1',''),('s2','');
        INSERT INTO doc_extractions VALUES ('e1','p1','project_info','success'),
                                          ('e2','p1','standards','success'),
                                          ('e3','p9','project_info','success');
        """)
        c.commit()

        class Cur:
            def __init__(self, cur):
                self.rowcount = max(cur.rowcount, 0)

        class DB:
            async def execute(self, sql, params=()):
                return Cur(c.execute(sql, params))

            async def commit(self):
                pass
        return c, DB()

    def test_clears_consistency_cache_for_target_only(self):
        c, db = self._db()
        asyncio.run(ba._invalidate_extraction_derived(db, "p1", ["s1"]))
        n1 = c.execute("SELECT COUNT(*) FROM consistency_scan_cache"
                       " WHERE scheme_id='s1'").fetchone()[0]
        n2 = c.execute("SELECT COUNT(*) FROM consistency_scan_cache"
                       " WHERE scheme_id='s2'").fetchone()[0]
        assert n1 == 0 and n2 == 1

    def test_advances_facts_timestamp_for_target_only(self):
        c, db = self._db()
        asyncio.run(ba._invalidate_extraction_derived(db, "p1", ["s1"]))
        s1 = c.execute("SELECT facts_updated_at FROM schemes WHERE id='s1'").fetchone()[0]
        s2 = c.execute("SELECT facts_updated_at FROM schemes WHERE id='s2'").fetchone()[0]
        assert s1, "目标方案必须推进时间戳（目录树据此派生「事实已变更」）"
        assert s2 == "", "非目标方案不得被波及"

    def test_marks_extractions_stale_scoped_by_project(self):
        c, db = self._db()
        asyncio.run(ba._invalidate_extraction_derived(db, "p1", ["s1"]))
        got = {r[0] for r in c.execute(
            "SELECT status FROM doc_extractions WHERE project_id='p1'")}
        other = c.execute("SELECT status FROM doc_extractions"
                          " WHERE project_id='p9'").fetchone()[0]
        assert got == {"stale"}
        assert other == "success", "其它项目不得被波及"

    def test_is_idempotent(self):
        """幂等：重复调用无副作用、不报错。"""
        c, db = self._db()
        asyncio.run(ba._invalidate_extraction_derived(db, "p1", ["s1"]))
        asyncio.run(ba._invalidate_extraction_derived(db, "p1", ["s1"]))

    def test_missing_table_is_fail_soft(self):
        """表缺失（测试库/迁移未跑）只记 WARNING，不抛。"""
        class DB:
            async def execute(self, *a, **k):
                raise RuntimeError("no such table")

            async def commit(self):
                pass
        asyncio.run(ba._invalidate_extraction_derived(DB(), "p1", ["s1"]))

    def test_cascade_called_from_invalidate(self):
        src = inspect.getsource(ba._invalidate_downstream_cache)
        assert "_invalidate_extraction_derived" in src

    def test_doc_extractions_has_no_updated_at_column(self):
        """防呆：SQL 里不得出现 doc_extractions 不存在的 ``updated_at`` 列。

        用 **AST 取字符串常量**而非源码文本匹配 —— 注释里为解释原因必然提到
        这个列名，纯文本匹配会把正确的说明当成违规（假失败）。
        """
        import ast
        from app import schema_sql
        block = schema_sql.SCHEMA_SQL
        i = block.index("CREATE TABLE IF NOT EXISTS doc_extractions")
        j = block.index("CREATE TABLE", i + 10) if "CREATE TABLE" in block[i + 10:] \
            else len(block)
        assert "updated_at" not in block[i:j], \
            "doc_extractions 竟有 updated_at 列，可放宽本护栏"
        tree = ast.parse(inspect.getsource(ba._invalidate_extraction_derived))
        sql_literals = [n.value for n in ast.walk(tree)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        bad = [s for s in sql_literals
               if "doc_extractions" in s and "updated_at" in s]
        assert not bad, f"SQL 引用了不存在的列：{bad}"


# =========================================================================
# 五、经核实判定为「非缺陷」的项（避免重复排查）
# =========================================================================
class TestNotDefects:
    def test_extract_layer_coverage_is_by_design(self):
        """「提取层物化只覆盖 5/18」并非缺陷。

        ``doc_storage.EXTRACT_TYPES`` 的 7 类里：
        · ``boq`` 是 RESERVED（预留容量，恒不映射）；
        · ``global_facts`` 由**全局事实表特供**，在 ``sync_extract_layer`` 里
          单独分支处理，**不依赖** ``_ITEM_TO_EXTRACT_TYPE``（见既有护栏
          ``test_doc_pipeline.py::TestExtractTypeCoverage`` 的 special_sources）；
        · 其余 5 类（project_info / engineering / design_params / geology /
          standards）**全部**已被解析项映射覆盖。
        所以「只有 5 个 item_id 有映射」是正确设计，不是漏接；要物化其余
        13 项需先新增提取类别（产品决策 + schema 语义变更）。
        """
        from app.services.doc_pipeline import doc_storage, pipeline
        special = set(doc_storage.RESERVED_EXTRACT_TYPES) | {"global_facts"}
        active = [t for t in doc_storage.EXTRACT_TYPES if t not in special]
        covered = {t for types in pipeline._ITEM_TO_EXTRACT_TYPE.values()
                   for t in types}
        assert set(active) == covered, (
            f"非预留、非特供的提取类别未全部被物化：{set(active) - covered}")
        assert "global_facts" not in covered, \
            "global_facts 走 sync_extract_layer 独立分支，不应出现在解析项映射里"

    def test_three_chain_limits_are_intentional(self):
        """目录 5 / 正文 3 / 事实无 LIMIT —— 各自的量级与职责不同。

        事实提取要的是**全量**依据（少一份就可能漏关键参数）；目录/正文只取
        摘要片段（超预算反而稀释指令）。三者不是「不一致」，而是按下游职责
        分别设定，4000 字的截断上限才是真正的护栏。
        """
        src = inspect.getsource(sh)
        assert "project_id, limit=5" in src
        assert "project_id, limit=3" in src

