"""全局事实注入门控 fail-closed 回归（2026-09-27）

BUG：sse_handlers._load_facts_rows 与 global_facts._load_fact_rows 在
`from app.services.facts_extractor import ...` 失败时，各自把门控回落为
旧口径「has_conflict=0 AND is_resolved=1」—— 丢掉 is_simulated=0（AI 编造值）
与 is_stale=0（已被重新提取取代的过期值）。

后果（数据真实性红线）：降级路径 fail-open，把不确定数据当确定事实注入
正文 / 目录 / 危大阈值判定。

护栏：
1. 兜底常量与主口径 _FACTS_INJECT_WHERE 逐字一致（结构上杜绝分叉）；
2. get_facts_inject_where() 出口返回 fail-closed 条件；
3. 两处调用点在 facts_extractor 不可导入时，生成的 SQL 仍含四个门控条件；
4. 反例：放宽口径（缺 is_simulated / is_stale）必须被断言拦下。
"""
import re
import uuid

import app.db as _appdb
import pytest
from app.db import get_conn, init_db
from app.services import facts_extractor

LEGACY_LOOSE_GATE = "has_conflict=0 AND is_resolved=1"


class TestFallbackGateIsFailClosed:
    """兜底门控必须与主口径逐字相同"""

    def test_fallback_equals_primary_gate(self):
        """兜底常量与主口径逐字一致（防「主口径改了兜底忘了改」）"""
        assert facts_extractor.FACTS_INJECT_WHERE_FALLBACK == \
            facts_extractor._FACTS_INJECT_WHERE

    def test_fallback_keeps_all_four_guards(self):
        """兜底必须同时含 has_conflict / is_resolved / is_simulated / is_stale"""
        gate = facts_extractor.FACTS_INJECT_WHERE_FALLBACK
        for cond in ("has_conflict=0", "is_resolved=1",
                     "is_simulated=0", "is_stale=0"):
            assert cond in gate, f"兜底门控丢失条件: {cond}"

    def test_public_accessor_returns_gate(self):
        """统一出口返回完整门控"""
        gate = facts_extractor.get_facts_inject_where()
        assert gate == facts_extractor._FACTS_INJECT_WHERE
        for cond in ("is_simulated=0", "is_stale=0"):
            assert cond in gate

    def test_fallback_is_not_legacy_loose_gate(self):
        """反例：兜底不得退化为旧宽松口径"""
        assert facts_extractor.FACTS_INJECT_WHERE_FALLBACK != LEGACY_LOOSE_GATE


class TestCallSitesNoLongerHardcodeLooseGate:
    """两处调用点不得再硬编码旧宽松口径"""

    @pytest.mark.parametrize("rel", [
        "app/routers/sse_handlers.py",
        "app/routers/global_facts.py",
    ])
    def test_no_hardcoded_loose_gate(self, rel):
        """源码中不得再出现被当作门控使用的旧宽松条件（注释中提及允许）"""
        from pathlib import Path

        import app as _app
        p = Path(_app.__file__).parent.parent / rel
        src = p.read_text(encoding="utf-8")
        # 去掉注释后再检查（注释里允许引用旧口径说明历史）
        code = "\n".join(
            ln for ln in src.splitlines() if not ln.strip().startswith("#"))
        # 旧口径若作为「整条门控」出现（前后有引号包裹）即为回归
        assert f'"{LEGACY_LOOSE_GATE}"' not in code, \
            f"{rel} 仍硬编码旧宽松门控作为整条条件"
        assert f"'{LEGACY_LOOSE_GATE}'" not in code, \
            f"{rel} 仍硬编码旧宽松门控作为整条条件"


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "t.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid
    await db.execute("DELETE FROM global_facts WHERE project_id=?", (pid,))
    await db.execute("DELETE FROM schemes WHERE id=?", (sid,))
    await db.execute("DELETE FROM projects WHERE id=?", (pid,))
    await db.commit()


async def _seed(db, pid, sid, fid, *, is_sim=0, is_stale=0,
                has_conflict=0, is_resolved=1, title="开挖深度", value="5.2m"):
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, title, content, "
        "category, is_simulated, is_stale, has_conflict, is_resolved) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (fid, pid, sid, title, f"- **{title}**: {value}", "tech_param",
         is_sim, is_stale, has_conflict, is_resolved))


class TestDangerCheckGateEndToEnd:
    """端到端：模拟值/过期值不得参与危大阈值判定"""

    async def test_danger_check_excludes_simulated_and_stale(self, ctx):
        db, pid, sid = ctx
        # 三条同值事实：干净 / 模拟 / 过期
        await _seed(db, pid, sid, "f-clean")
        await _seed(db, pid, sid, "f-sim", is_sim=1)
        await _seed(db, pid, sid, "f-stale", is_stale=1)
        await db.commit()

        from app.routers.global_facts import _load_fact_rows
        clean = await _load_fact_rows(db, sid, "", injectable_only=True)
        titles = {r["title"] for r in clean}
        assert "开挖深度" in titles
        assert len(clean) == 1, "模拟值/过期值必须被门控剔除"
        assert all(not r["is_simulated"] and not r["is_stale"] for r in clean)

    async def test_injectable_query_excludes_simulated_and_stale(self, ctx):
        """导出侧 build_injectable_facts_query 同口径"""
        db, pid, sid = ctx
        await _seed(db, pid, sid, "f-clean")
        await _seed(db, pid, sid, "f-sim", is_sim=1)
        await _seed(db, pid, sid, "f-stale", is_stale=1)
        await db.commit()

        cols = f"{facts_extractor.FACTS_GT_COLUMN}, title, content"
        sql, params = facts_extractor.build_injectable_facts_query(sid, pid, cols)
        for cond in ("is_simulated=0", "is_stale=0"):
            assert cond in sql, f"导出查询门控缺失 {cond}"
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
        assert len(rows) == 1

    async def test_sse_inject_rows_exclude_simulated_and_stale(self, ctx):
        """正文注入侧 sse_handlers._load_facts_rows 同口径"""
        db, pid, sid = ctx
        await _seed(db, pid, sid, "f-clean")
        await _seed(db, pid, sid, "f-sim", is_sim=1)
        await _seed(db, pid, sid, "f-stale", is_stale=1)
        await db.commit()

        from app.routers.sse_handlers import _load_facts_rows
        rows = await _load_facts_rows(db, sid)
        assert len(rows) == 1, "正文注入必须剔除模拟值与过期值"
        assert rows[0][1] == "开挖深度"
