# -*- coding: utf-8 -*-
"""全局事实「可注入门控」跨模块口径 parity 护栏（2026-09-30，第九轮）

背景（AGENTS.md 反复出现的根因模式）：**同一业务判据在 2~3 处各自实现**。
`services/facts_extractor.py::_FACTS_INJECT_WHERE` 是「哪些全局事实可以注入
目录/正文/导出」的唯一事实源，四条件 fail-closed：

    has_conflict=0 AND is_resolved=1 AND is_simulated=0 AND is_stale=0

2026-09-27 已把「import 失败时的兜底常量」收敛到 `FACTS_INJECT_WHERE_FALLBACK`
并补了 `test_facts_inject_gate_failclosed_20260927.py`（只锁 2 个调用点：
`sse_handlers._load_facts_rows` 与 `global_facts._load_fact_rows`）。

本轮发现的**遗留分叉**（同根因，护栏没覆盖到的第三、四个实现）：

1. `services/placeholder_inventory.py::build_rerun_plan` 的 docstring 明写
   「可注入语料与生成侧同口径 …… （与 _render_facts_text 的过滤口径一致）」，
   实际 SQL 是 `WHERE scheme_id=? AND is_resolved=1 AND has_conflict=0`
   —— **漏掉 is_simulated=0 与 is_stale=0**。该 docstring 是**错误承诺**。

2. `services/input_coverage.py::build_inventory` 的 ok_cnt 判据
   `SUM(CASE WHEN has_conflict=0 AND is_resolved=1 THEN 1 ELSE 0 END)`
   同样只判两列。

两处共同造成的**用户可见后果**（同一场景、同一条数据）：

  一条事实 `is_resolved=1, has_conflict=0, is_simulated=1`（AI 编造值）或
  `is_stale=1`（来源资料已变化；`_mark_project_facts_stale` 批量置位时
  **不清 is_resolved**，故 resolved=1 且 stale=1 是可达状态）：

  * 生成侧：被 `_FACTS_INJECT_WHERE` 正确排除 → **不进正文/导出**（真实性红线）；
  * 待补充清单：被判为「可注入语料」→ 字段标 `fillable=true` → 章节判
    `rerunnable` → 前端提示「重跑本节即可消除占位」；
  * 用户照提示重跑 → 占位**原样还在**（语料里根本没有这条事实）。

即：**承诺可消除的占位，实际消除不了**，方向恰好是「让用户白跑一遍」。
这与 §4.11.2「JSON 失败哨兵 `{}` 被当成已完成 → 三重假绿」同源：
判定与被判定者不是同一口径，就产生「看起来成功、实际没做」。

护栏分三组：
  A. 静态：两处 SQL 不得硬编码旧宽松口径，必须复用唯一出口
     （`facts_extractor.get_facts_inject_where()`）；
  B. 行为：造 `is_stale=1` / `is_simulated=1` 的行，走真实 DB 验证
     `build_rerun_plan` 不把它们算作可注入语料；
  C. 反向：正常已确认事实仍必须判为可补齐（防止「为修 A 把门控删空」）。
"""
import re
import uuid
from pathlib import Path

import app as _app_pkg
import pytest
from app.db import get_conn, init_db
from app.services import facts_extractor, placeholder_inventory

CANONICAL = facts_extractor.get_facts_inject_where()

REQUIRED_CONDS = ("has_conflict=0", "is_resolved=1", "is_simulated=0", "is_stale=0")

# 旧的宽松口径：本次要消灭的对象
LEGACY_LOOSE_GATE = "is_resolved=1 AND has_conflict=0"


def _read(rel: str) -> str:
    root = Path(_app_pkg.__file__).resolve().parent
    return (root / rel).read_text(encoding="utf-8")


def _strip_doc_and_comments(src: str) -> str:
    """去掉 docstring 与行内注释，避免把「反面示例说明」误判为实现。"""
    try:
        import io
        import tokenize
        out: list[str] = []
        prev_end = (1, 0)
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.line.strip().startswith(
                    ('"""', "'''", 'r"""', "r'''")):
                continue
            if tok.start[0] > prev_end[0]:
                out.append("\n" * (tok.start[0] - prev_end[0]))
            out.append(tok.string)
            prev_end = tok.end
        return "".join(out)
    except Exception:  # pragma: no cover - 解析失败时退回朴素清洗
        return "\n".join(ln.split("#")[0] for ln in src.splitlines())


class TestCanonicalGateIntact:
    """C. 反向断言：唯一出口本身没被削弱"""

    def test_canonical_gate_has_all_four_conditions(self):
        for cond in REQUIRED_CONDS:
            assert cond in CANONICAL, f"唯一门控缺少条件: {cond}"


class TestInputCoverageGateParity:
    """A. input_coverage 的 ok_cnt 判据同样不得只判两列"""

    def test_ok_cnt_counts_all_four_conditions(self):
        code = _strip_doc_and_comments(_read("services/input_coverage.py"))
        loose = ("SUM(CASE WHEN has_conflict=0 AND is_resolved=1 "
                 "THEN 1 ELSE 0 END)")
        assert loose not in code, "input_coverage 的 ok_cnt 仍只判两列"

    def test_uses_single_source_accessor(self):
        code = _strip_doc_and_comments(_read("services/input_coverage.py"))
        assert "get_facts_inject_where" in code, \
            "应复用 facts_extractor.get_facts_inject_where() 单一出口"


class TestRerunPlanRejectsUninjectableFacts:
    """B. 行为级：造真实的 stale / simulated 行，验证不进可注入语料"""

    @pytest.fixture
    async def db(self, tmp_path, monkeypatch):
        import app.db as _appdb
        _appdb.DB_PATH = tmp_path / "fact-gate-parity.sqlite"
        await init_db()
        conn = await get_conn()
        yield conn
        await conn.close()

    @staticmethod
    async def _mk_scheme(conn):
        pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
        await conn.execute("INSERT INTO projects (id, name) VALUES (?,?)", (pid, "p"))
        await conn.execute(
            "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", (sid, pid, "s"))
        return pid, sid

    @staticmethod
    async def _mk_fact(conn, sid, *, title, resolved=1, conflict=0,
                       simulated=0, stale=0):
        fid = uuid.uuid4().hex
        await conn.execute(
            "INSERT INTO global_facts (id, project_id, scheme_id, category, "
            "group_title, title, content, is_resolved, has_conflict, "
            "is_simulated, is_stale) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fid, "", sid, "project_overview", "工程概况", title,
             f"内容-{title}", resolved, conflict, simulated, stale))
        return fid

    @staticmethod
    async def _mk_section(conn, sid, content):
        sec = uuid.uuid4().hex
        await conn.execute(
            "INSERT INTO sections (id, scheme_id, title, content) VALUES (?,?,?,?)",
            (sec, sid, "第一章", content))
        return sec

    @pytest.mark.asyncio
    async def test_stale_fact_not_counted_as_injectable_corpus(self, db):
        """is_stale=1（来源已变化）不得让占位被判为「可补齐」"""
        _, sid = await self._mk_scheme(db)
        await self._mk_section(db, sid, "基础埋深为【待补充：基础埋深】。")
        await self._mk_fact(db, sid, title="基础埋深 18m", stale=1)

        plan = await placeholder_inventory.build_rerun_plan(sid, db)
        by_field = {f["field"]: f for f in plan["fields"]}
        assert "基础埋深" in by_field, "前置条件：应扫出该占位字段"
        assert by_field["基础埋深"]["fillable"] is False, \
            "过期事实(is_stale=1)被算作可注入语料 → 承诺可消除却消除不了"
        assert plan["rerunnable_count"] == 0, \
            "含过期事实的章节不应被判为可重跑"

    @pytest.mark.asyncio
    async def test_simulated_fact_not_counted_as_injectable_corpus(self, db):
        """is_simulated=1（AI 编造值）同样不得算作可补齐"""
        _, sid = await self._mk_scheme(db)
        await self._mk_section(db, sid, "基础埋深为【待补充：基础埋深】。")
        await self._mk_fact(db, sid, title="基础埋深 18m", simulated=1)

        plan = await placeholder_inventory.build_rerun_plan(sid, db)
        by_field = {f["field"]: f for f in plan["fields"]}
        assert by_field["基础埋深"]["fillable"] is False, \
            "模拟值(is_simulated=1)被算作可注入语料 → 违反数据真实性红线"

    @pytest.mark.asyncio
    async def test_confirmed_normal_fact_still_fillable(self, db):
        """反向断言：正常已确认事实仍必须判为可补齐（防止把门控整体删空）"""
        _, sid = await self._mk_scheme(db)
        await self._mk_section(db, sid, "基础埋深为【待补充：基础埋深】。")
        await self._mk_fact(db, sid, title="基础埋深 18m")

        plan = await placeholder_inventory.build_rerun_plan(sid, db)
        by_field = {f["field"]: f for f in plan["fields"]}
        assert by_field["基础埋深"]["fillable"] is True
        assert plan["rerunnable_count"] == 1


class TestInputCoverageInventoryMatchesGate:
    """B. input_coverage 行为级：被过滤事实不得计为 available，且必须可见"""

    @pytest.fixture
    async def db(self, tmp_path, monkeypatch):
        import app.db as _appdb
        _appdb.DB_PATH = tmp_path / "fact-gate-parity-ic.sqlite"
        await init_db()
        conn = await get_conn()
        yield conn
        await conn.close()

    @staticmethod
    async def _seed(conn, **flags):
        pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
        await conn.execute("INSERT INTO projects (id, name) VALUES (?,?)", (pid, "p"))
        await conn.execute(
            "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", (sid, pid, "s"))
        await conn.execute(
            "INSERT INTO global_facts (id, project_id, scheme_id, category, "
            "group_title, title, content, is_resolved, has_conflict, "
            "is_simulated, is_stale) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, pid, sid, "project_overview", "工程概况",
             "基础埋深", "基础埋深 18m",
             flags.get("resolved", 1), flags.get("conflict", 0),
             flags.get("simulated", 0), flags.get("stale", 0)))
        return pid, sid

    @pytest.mark.asyncio
    async def test_stale_fact_not_available_but_visible(self, db):
        from app.services import input_coverage
        pid, sid = await self._seed(db, stale=1)
        inv = await input_coverage.build_inventory(db, pid, sid)
        facts = [e for e in inv.entries if e.source == input_coverage.SRC_FACTS]
        assert not [e for e in facts if e.status == "available"], \
            "过期事实被计为 available → 差集报告会假报「已调用」"
        stale = [e for e in facts if e.status == "filtered_stale"]
        assert stale, "过期事实在台账里必须可见（否则就是静默丢失）"

    @pytest.mark.asyncio
    async def test_simulated_fact_not_available_but_visible(self, db):
        from app.services import input_coverage
        pid, sid = await self._seed(db, simulated=1)
        inv = await input_coverage.build_inventory(db, pid, sid)
        facts = [e for e in inv.entries if e.source == input_coverage.SRC_FACTS]
        assert not [e for e in facts if e.status == "available"], \
            "模拟值被计为 available → 违反数据真实性红线"
        assert [e for e in facts if e.status == "filtered_simulated"], \
            "模拟值在台账里必须可见"

    @pytest.mark.asyncio
    async def test_normal_fact_still_available(self, db):
        """反向断言：正常事实仍必须计为 available（防止把门控删空）"""
        from app.services import input_coverage
        pid, sid = await self._seed(db)
        inv = await input_coverage.build_inventory(db, pid, sid)
        facts = [e for e in inv.entries if e.source == input_coverage.SRC_FACTS]
        assert [e for e in facts if e.status == "available"]


    def test_canonical_gate_is_not_legacy_loose(self):
        assert CANONICAL != LEGACY_LOOSE_GATE


class TestPlaceholderInventoryGateParity:
    """A. placeholder_inventory 不得硬编码旧宽松口径"""

    def test_source_has_no_legacy_loose_gate_literal(self):
        code = _strip_doc_and_comments(_read("services/placeholder_inventory.py"))
        assert "is_resolved=1 AND has_conflict=0" not in code, \
            "build_rerun_plan 仍硬编码旧宽松口径（漏 is_simulated / is_stale）"

    def test_uses_single_source_accessor(self):
        code = _strip_doc_and_comments(_read("services/placeholder_inventory.py"))
        assert "get_facts_inject_where" in code, \
            "应复用 facts_extractor.get_facts_inject_where() 单一出口"

    def test_docstring_no_longer_states_deprecated_gate(self):
        """修复前 docstring 把旧口径陈述为**现行契约**；现在不得再这么写。

        注意：只针对「规格声明句」断言，BUG 修复注释里**允许**引用旧写法
        作为「旧实现曾是这样」的证据（否则注释本身会被误判为回归）。
        """
        src = _read("services/placeholder_inventory.py")
        spec_claim = "：``is_resolved=1 AND has_conflict=0``（与 _render_facts_text"
        assert spec_claim not in src, \
            "docstring 仍把已废弃的旧口径陈述为现行契约"
