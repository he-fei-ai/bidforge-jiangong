"""全局事实「要求 vs 事实口径」value_kind · R80（2026-10-15）落地护栏。

P2-3（R77 §11.3 递延项）正式落地。设计要点：
  - value_kind ∈ ''（未区分，历史行缺省，读侧按 fact 处理）/ 'fact'（本方案
    口径）/ 'requirement'（招标/规范要求原文口径，正文引用须写「满足/响应
    要求」）。非法值一律归一 ''，绝不抛错阻断提取。
  - 迁移沿用仓库惯例：db._migrate 幂等补列 + schema_sql 建表语句同步；
    历史存量不回填。
  - 列位置：GLOBAL_FACTS_INSERT_COLS 中 value_kind 插在 is_stale 之前，
    保住两个既有契约（to_db_row()[-4:] == 九大章节四维、is_stale 先于四维）。
  - 注入渲染：requirement 行附加口径前缀（截断之后拼接）；fact/'' 行
    渲染与引入前逐字一致（行为不变性）。

运行：pytest tests/test_facts_value_kind_r80_20261015.py -q
"""
import ast
import os
import textwrap
import uuid

import pytest
import pytest_asyncio

from app.services.facts_extractor import (
    GLOBAL_FACTS_INSERT_COLS,
    FactItem,
    normalize_value_kind,
    _parse_fact_dict,
    VALUE_KIND_ALLOWED,
)

_FE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app", "services", "facts_extractor.py"))
_GF_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app", "routers", "global_facts.py"))
_SCHEMES_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app", "routers", "schemes.py"))
_DB_PATH_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app", "db.py"))
_SCHEMA_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app", "schema_sql.py"))


def _read(path: str) -> str:
    raw = open(path, "rb").read()
    if raw[:3] == b"\xef\xbb\xbf":
        raise AssertionError("BOM 不应出现：%s" % path)
    text = raw.decode("utf-8")
    assert "\ufffd" not in text, "U+FFFD 不应出现：%s" % path
    return text


# ---------------------------------------------------------------------------
# A 组 · 白名单归一（单一事实源）
# ---------------------------------------------------------------------------
class TestNormalizeValueKind:
    def test_allowed_values(self):
        assert VALUE_KIND_ALLOWED == ("fact", "requirement")

    def test_valid_passthrough(self):
        assert normalize_value_kind("fact") == "fact"
        assert normalize_value_kind("requirement") == "requirement"

    def test_case_and_whitespace_normalized(self):
        assert normalize_value_kind("  REQUIREMENT ") == "requirement"
        assert normalize_value_kind("Fact") == "fact"

    def test_illegal_becomes_empty(self):
        # AI 幻觉值 / 客户端脏值 / None —— 一律归一 ''，绝不抛错
        # （" fact " 这类仅含空白的合法值会被 strip 放行，属预期宽容口径）
        for bad in ("", None, "foo", "要求", " FACTS", 0, 1, [], {}):
            assert normalize_value_kind(bad) == "", repr(bad)


# ---------------------------------------------------------------------------
# B 组 · 落库对齐（to_db_row ↔ GLOBAL_FACTS_INSERT_COLS 逐位一致）
# ---------------------------------------------------------------------------
class TestDbRowAlignment:
    def test_row_len_matches_cols(self):
        it = FactItem(name="建设工期", value="450", key="total_duration",
                      category="schedule", value_kind="requirement")
        row = it.to_db_row("g", "p", "s", "t")
        assert len(row) == len(GLOBAL_FACTS_INSERT_COLS), (
            "to_db_row 列数必须与 GLOBAL_FACTS_INSERT_COLS 一一对应")

    def test_value_kind_position_and_value(self):
        it = FactItem(name="质保期", value="2", value_kind="REQUIREMENT")
        row = it.to_db_row("g", "p", "s", "t")
        idx = GLOBAL_FACTS_INSERT_COLS.index("value_kind")
        assert row[idx] == "requirement", "落库值必须是归一后的白名单值"

    def test_default_empty(self):
        it = FactItem(name="项目经理", value="张伟")
        row = it.to_db_row("g", "p", "s", "t")
        idx = GLOBAL_FACTS_INSERT_COLS.index("value_kind")
        assert row[idx] == ""

    def test_existing_tail_contract_not_broken(self):
        """既有契约回归：to_db_row()[-4:] 仍是九大章节四维、is_stale 先于四维。"""
        it = FactItem(name="x", value="y", chapter="plan", fact_attr="quantitative",
                      source_kind="bid_doc", is_shared=True)
        row = it.to_db_row("g", "p", "s", "t")
        assert row[-4:] == ("plan", "quantitative", "bid_doc", 1)
        stale_idx = GLOBAL_FACTS_INSERT_COLS.index("is_stale")
        vk_idx = GLOBAL_FACTS_INSERT_COLS.index("value_kind")
        assert vk_idx == stale_idx - 1, "value_kind 必须紧邻 is_stale 之前（契约位）"

    def test_cols_registered_in_schema(self):
        assert 'value_kind TEXT DEFAULT' in _read(_SCHEMA_PATH)
        assert '("global_facts", "value_kind", "TEXT DEFAULT \'\'")' in _read(_DB_PATH_FILE), (
            "db._migrate 补列清单必须登记 value_kind（旧库幂等补列）")


# ---------------------------------------------------------------------------
# C 组 · AI 输出解析
# ---------------------------------------------------------------------------
class TestParseFactDict:
    def test_requirement_kind_parsed(self):
        it = _parse_fact_dict({
            "name": "监测报警值", "value": "累计沉降30mm",
            "value_kind": "requirement"})
        assert it is not None and it.value_kind == "requirement"

    def test_illegal_kind_becomes_empty(self):
        it = _parse_fact_dict({"name": "x", "value": "y", "value_kind": "要求"})
        assert it is not None and it.value_kind == ""

    def test_missing_kind_defaults_empty(self):
        it = _parse_fact_dict({"name": "x", "value": "y"})
        assert it is not None and it.value_kind == ""

    def test_legacy_dict_kind_parsed(self):
        from app.services.facts_extractor import _parse_legacy_fact_dict
        it = _parse_legacy_fact_dict(
            {"name": "验收标准", "value": "GB 50300-2013",
             "value_kind": "requirement"})
        assert it is not None and it.value_kind == "requirement"


# ---------------------------------------------------------------------------
# D 组 · 手工/AI 新增路径（_manual_fact_row 三处共用出口）
# ---------------------------------------------------------------------------
class TestManualFactRow:
    def _row(self, **kw):
        from app.routers.global_facts import _manual_fact_row
        kw.setdefault("fid", "f")
        kw.setdefault("pid", "p")
        kw.setdefault("sid", "s")
        kw.setdefault("group_id", "g")
        kw.setdefault("group_title", "t")
        kw.setdefault("name", "建设工期")
        kw.setdefault("content", "- **建设工期**: 450 日历天")
        kw.setdefault("category", "schedule")
        kw.setdefault("source_file", "手动录入")
        return _manual_fact_row(**kw)

    def test_row_len_matches_cols(self):
        assert len(self._row()) == len(GLOBAL_FACTS_INSERT_COLS), (
            "_manual_fact_row 列数必须与列清单一一对应（占位符由列数派生）")

    def test_value_kind_default_empty(self):
        idx = GLOBAL_FACTS_INSERT_COLS.index("value_kind")
        assert self._row()[idx] == ""

    def test_value_kind_normalized(self):
        idx = GLOBAL_FACTS_INSERT_COLS.index("value_kind")
        assert self._row(value_kind="REQUIREMENT")[idx] == "requirement"
        assert self._row(value_kind="bogus")[idx] == ""


# ---------------------------------------------------------------------------
# E 组 · 注入渲染：requirement 前缀 + 行为不变性
# ---------------------------------------------------------------------------
_REQUIREMENT_PREFIX = "（要求口径：正文引用时须写为「满足/响应招标要求」，不得当作本方案既定值）"


class TestRenderFactsText:
    def _render(self, rows, **kw):
        from app.services.facts_builder import _render_facts_text
        return _render_facts_text(rows, **kw)

    def test_requirement_row_gets_prefix(self):
        rows = [("工期", "建设工期", "- **建设工期**: 不超过450日历天", 1.0, "plan",
                 "requirement")]
        out = self._render(rows, max_total=6000, per_fact=300)
        assert _REQUIREMENT_PREFIX in out
        assert "不超过450日历天" in out

    def test_fact_and_empty_rows_verbatim_unchanged(self):
        """行为不变性：fact / ''（历史行）渲染与引入前逐字一致。"""
        content = "- **项目经理**: 张伟"
        for vk in ("fact", ""):
            out = self._render([("人员", "项目经理", content, 1.0, "personnel", vk)])
            assert out.strip().endswith(content), out
            assert "要求口径" not in out

    def test_legacy_short_rows_still_render(self):
        """历史 3/4/5 元组行（测试 mock / 旧调用方）必须继续可用。"""
        for row in (("g", "t", "- **x**: 1",),
                    ("g", "t", "- **x**: 1", 1.0,),
                    ("g", "t", "- **x**: 1", 1.0, "plan",)):
            out = self._render([row])
            assert "- **x**: 1" in out
            assert "要求口径" not in out

    def test_prefix_after_truncation_not_eating_value(self):
        """前缀在 per_fact 截断之后拼接：事实值本身不被前缀挤掉。"""
        long_value = "值" * 300
        rows = [("g", "t", f"- **x**: {long_value}", 1.0, "plan", "requirement")]
        out = self._render(rows, max_total=6000, per_fact=300)
        assert _REQUIREMENT_PREFIX in out
        assert long_value[:50] in out, "截断后的事实值必须仍可见"


# ---------------------------------------------------------------------------
# F 组 · _load_facts_rows 集成（真实 DB：6 元组 + 门控仍生效）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestLoadFactsRowsIntegration:
    @pytest_asyncio.fixture
    async def ctx(self, tmp_path, monkeypatch):
        import app.db as appdb
        appdb.DB_PATH = tmp_path / "vk.sqlite"
        await appdb.init_db()
        db = await appdb.get_conn()
        pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db.execute(
            "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
        rows = [
            # (name, content, value_kind) —— 均过注入门控
            ("建设工期", "- **建设工期**: 不超过450日历天", "requirement"),
            ("项目经理", "- **项目经理**: 张伟", "fact"),
            ("塔吊型号", "- **塔吊型号**: QTZ80", ""),
        ]
        for nm, content, vk in rows:
            await db.execute(
                "INSERT INTO global_facts(id,project_id,scheme_id,group_id,"
                "group_title,title,content,category,is_resolved,value_kind) "
                "VALUES(?,?,?,?,?,?,?,?,1,?)",
                (uuid.uuid4().hex, pid, sid, "g1", "工期", nm, content,
                 "schedule", vk))
        await db.commit()
        yield db, sid, dict(zip(["工期", "项目经理", "塔吊型号"], rows))
        await appdb.close_db()

    async def test_rows_carry_value_kind(self, ctx):
        from app.services.facts_builder import _load_facts_rows
        db, sid, _ = ctx
        rows = await _load_facts_rows(db, sid)
        assert len(rows) == 3
        by_title = {r[1]: r for r in rows}
        assert by_title["建设工期"][5] == "requirement"
        assert by_title["项目经理"][5] == "fact"
        assert by_title["塔吊型号"][5] == ""

    async def test_gate_still_filters_unresolved(self, ctx):
        """回归：注入门控四条件不因加列放松。"""
        db, sid, _ = ctx
        await db.execute(
            "INSERT INTO global_facts(id,project_id,scheme_id,group_id,"
            "group_title,title,content,category,is_resolved,value_kind) "
            "VALUES(?,?,?,?,?,?,?,?,0,'fact')",  # is_resolved=0 → 必须被门控剔除
            (uuid.uuid4().hex, "", sid, "g1", "工期", "编造值", "- **编造值**: 999", "other"))
        await db.commit()
        from app.services.facts_builder import _load_facts_rows
        rows = await _load_facts_rows(db, sid)
        assert all(r[1] != "编造值" for r in rows)


# ---------------------------------------------------------------------------
# G 组 · 静态接线锁（调用形态，防「名字在场但接线被掏空」）
# ---------------------------------------------------------------------------
class TestStaticWiring:
    def test_load_select_includes_value_kind(self):
        src = _read(os.path.join(os.path.dirname(_GF_PATH), "..",
                                 "services", "facts_builder.py"))
        assert "COALESCE(value_kind,'') AS value_kind" in src, (
            "_load_facts_rows 的 SELECT 必须读取 value_kind 列")
        assert "_row_value_kind(_row) == \"requirement\"" in src, (
            "_render_facts_text 必须按 value_kind 分支拼接口径前缀")

    def test_schemes_copy_keeps_value_kind(self):
        """方案复制路径：列清单含 value_kind 且占位符数 == 列数。"""
        src = _read(_SCHEMES_PATH)
        seg = src[src.index("INSERT INTO global_facts"):]
        head = seg[:seg.index("VALUES")]
        cols = head[head.index("(") + 1:head.rindex(")")]
        # 源码是 Python 相邻字符串字面量拼接（含引号/换行/缩进），剥掉再切分
        cols = cols.replace('"', "").replace("\n", " ")
        col_names = [c.strip() for c in cols.split(",") if c.strip()]
        assert "value_kind" in col_names, "方案复制必须保留 value_kind 口径"
        assert col_names.index("value_kind") == col_names.index("norm_group") + 1
        # 占位符数与列数一致（防手写占位符错位复发）
        vals = seg[seg.index("VALUES"):]
        ph = vals[:vals.index(")")]
        assert ph.count("?") == len(col_names)

    def test_list_facts_returns_value_kind(self):
        src = _read(_GF_PATH)
        assert '"value_kind": r.get("value_kind") or ""' in src, (
            "list_facts 行构造必须回传 value_kind（前端徽标消费）")

    def test_manual_row_normalizes_value_kind(self):
        src = _read(_GF_PATH)
        assert "normalize_value_kind(value_kind)" in src, (
            "_manual_fact_row 必须经白名单归一，不得直落客户端脏值")

    def test_prompt_schema_declares_value_kind(self):
        src = _read(os.path.join(os.path.dirname(_GF_PATH), "..",
                                 "services", "ai", "prompts", "analysis.py"))
        assert src.count('"value_kind"') >= 2, (
            "主提取与 JSON 修复两处输出 Schema 都必须声明 value_kind")
        assert "value_kind 口径标注" in src, (
            "转写纪律段必须说明 fact/requirement 两档语义")
