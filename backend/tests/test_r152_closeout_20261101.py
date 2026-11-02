"""R152（2026-11-01）遗留问题与后续建议收口 · 护栏测试。

承接 R151 校准的遗留清单，本轮落地五项修复 + 一项归一：
  F1 numbering 正则前后端同步（字母枚举分支 a）/b)）—— 行为锁（后端侧）
  F2 ai_gateway.request_json 死参数 repair_key 移除（扫描器合法形态）
  F3 r145 生产库锚测试改造为动态重锚 + 缺锚 skip —— 源码锚
  F4 清洗脚本 --apply 编号重排 pass（编号漂移即使无 L5 行也修复）—— 子进程行为锁
  F5 三级字数预算级联（PATCH cascade_word_budget + 前端接线）—— 行为 + 接线锚
  F6 六个并行遗留 MIXED 行尾文件按 HEAD 基准归一 —— 自持性锁
"""
import inspect
import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent


# ============================================================
# F1 · 字母枚举分支（后端行为 + 前后端 parity 由既有测试承载）
# ============================================================
class TestAlphaEnumStrip:
    def test_backend_strips_alpha_enum(self):
        from app.services.numbering import strip_outline_numbering
        assert strip_outline_numbering("a）材料与环保控制参数") == "材料与环保控制参数"
        assert strip_outline_numbering("b)钢筋保护层") == "钢筋保护层"
        # 同族约束：非枚举形态不受影响（"A4纸" / "A 级防火"）
        assert strip_outline_numbering("A4纸堆放要求") == "A4纸堆放要求"

    def test_frontend_mirror_is_synced(self):
        """前端页面正则已带字母分支（F1 的另一侧；逐字符 parity 由
        test_import_outline_deep_fix_20260927 的 parity 用例锁定）。"""
        page = (BACKEND.parent / "frontend" / "src" / "pages"
                / "SchemeWorkbenchPage.tsx").read_text(encoding="utf-8")
        assert "a-zA-Z]{1,2}" in page, "前端 LEADING_NUMBER_RE 缺字母枚举分支"


# ============================================================
# F2 · ai_gateway.request_json 死参数移除
# ============================================================
class TestGatewayRequestJson:
    def test_repair_key_param_removed_and_generic_explicit(self):
        import app.services.ai.ai_gateway as gw
        params = inspect.signature(gw.request_json).parameters
        assert "repair_key" not in params, (
            "门面 repair_key 转发形参已按 R152 移除（零调用方 + 扫描器判漂移），不得回流")
        src = Path(gw.__file__).read_text(encoding="utf-8")
        assert "repair_key=GENERIC_REPAIR_KEY" in src
        assert "repair_key=repair_key or" not in src, "布尔默认写法是漂移信号形态"


# ============================================================
# F3 · r145 生产库锚测试已改造（动态重锚 + 缺锚 skip）
# ============================================================
class TestR145AnchorRework:
    def test_dynamic_reanchor_wired(self):
        src = (BACKEND / "tests" / "test_r145_content_export_20261029.py").read_text(
            encoding="utf-8")
        assert "954f14e9-2758-4eb9-8f9a-920e98b9f78f" in src, "原锚点 ID 保留（仍在时逐字原断言）"
        assert "pytest.skip" in src, "缺锚时必须 skip（数据易变 ≠ 回归信号）"
        assert "LIKE '%| ---%'" in src, "动态重锚查询在位"


# ============================================================
# F4 · 清洗脚本 --apply 编号重排 pass（子进程 + 临时副本行为锁）
# ============================================================
class TestCleanScriptRenumberPass:
    def _fresh_db(self, db: Path) -> None:
        """空库 + 生产同源建表（脚本假设真实 schema）。"""
        from app.schema_sql import SCHEMA_SQL
        conn = sqlite3.connect(str(db))
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        conn.close()

    def test_apply_fixes_numbering_even_without_deep_rows(self, tmp_path):
        """无 L5 行但有编号漂移时，--apply 必须执行重排（旧版是无声 no-op）。"""
        db = tmp_path / "drill.sqlite"
        self._fresh_db(db)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", ("p1", "演练"))
        conn.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
                     ("s1", "p1", "演练方案"))
        rows = [  # 一级编号漂移（存 3 应为 1），且子编号未承父系；无任何 L5 行
            ("d1", "", 1, "第一章 工程概况", json.dumps({"id": "3"})),
            ("d2", "d1", 2, "1.1 编制依据", json.dumps({"id": "1.1"})),
            ("d3", "d2", 3, "1.1.1 规范清单", json.dumps({"id": "1.1.1"})),
            ("d4", "d3", 4, "1.1.1.1 四级", json.dumps({"id": "1.1.1.1"})),
        ]
        for i, (_id, _p, _lv, _t, _oj) in enumerate(rows):
            conn.execute(
                "INSERT INTO sections(id, scheme_id, project_id, parent_id,"
                " title, level, sort_order, outline_json) VALUES(?,?,?,?,?,?,?,?)",
                (_id, "s1", "p1", _p or None, _t, _lv, i, _oj))
        conn.commit()
        conn.close()

        script = BACKEND / "_r151_clean_outline.py"
        r = subprocess.run(
            [sys.executable, str(script), "--apply", "--db", str(db),
             "--scheme", "s1"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, cwd=str(BACKEND))
        out = (r.stdout or "") + (r.stderr or "")
        assert "CLEANED" in out, f"应执行清洗: {out[-400:]}"

        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            "SELECT id, level, outline_json FROM sections"
            " WHERE scheme_id='s1' ORDER BY sort_order").fetchall()
        conn.close()
        ids = {rid: json.loads(oj)["id"] for rid, _lv, oj in rows}
        assert ids == {"d1": "1", "d2": "1.1", "d3": "1.1.1", "d4": "1.1.1.1"}, (
            f"编号必须按树位置重排（子承父系）: {ids}")
        levels = {rid: lv for rid, lv, _oj in rows}
        assert levels == {"d1": 1, "d2": 2, "d3": 3, "d4": 4}

    def test_dry_run_reports_renumber_intent(self, tmp_path):
        """dry-run 必须明确告知 --apply 将做编号重排（不再无声）。"""
        db = tmp_path / "drill2.sqlite"
        self._fresh_db(db)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", ("p1", "演练"))
        conn.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
                     ("s1", "p1", "演练"))
        for i, (_id, _p, _lv, _t, _oj) in enumerate([
                ("d1", None, 1, "第一章", json.dumps({"id": "3"})),
                ("d2", "d1", 2, "1.1 编制依据", json.dumps({"id": "1.1"}))]):
            conn.execute(
                "INSERT INTO sections(id, scheme_id, project_id, parent_id,"
                " title, level, sort_order, outline_json) VALUES(?,?,?,?,?,?,?,?)",
                (_id, "s1", "p1", _p, _t, _lv, i, _oj))
        conn.commit()
        conn.close()
        script = BACKEND / "_r151_clean_outline.py"
        r = subprocess.run(
            [sys.executable, str(script), "--db", str(db), "--scheme", "s1"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, cwd=str(BACKEND))
        assert "编号未承父系" in (r.stdout or ""), r.stdout


# ============================================================
# F5 · 三级字数预算级联
# ============================================================
@pytest.mark.asyncio
class TestCascadeWordBudget:
    async def _seed(self, db):
        pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db.execute(
            "INSERT INTO schemes(id, project_id, name) VALUES(?,?,?)",
            (sid, pid, "级联方案"))
        rows = [
            ("L1", "", 1, 0), ("L2a", "L1", 2, 1), ("L3a", "L2a", 3, 2),
            ("L4a", "L3a", 4, 3), ("L4b", "L3a", 4, 4),
        ]
        for _id, _p, _lv, _sort in rows:
            await db.execute(
                "INSERT INTO sections(id, scheme_id, project_id, parent_id,"
                " title, level, sort_order, word_budget)"
                " VALUES(?,?,?,?,?,?,?,1200)",
                (_id, sid, pid, _p or None, f"章节{_id}", _lv, _sort))
        await db.commit()
        return sid

    async def test_cascade_updates_all_descendants(self, db_conn):
        from app.models import SectionUpdate
        from app.routers.sections import update_section
        sid = await self._seed(db_conn)
        res = await update_section(
            sid, "L3a",
            SectionUpdate(word_budget=2000, cascade_word_budget=True),
            db_conn)
        assert res["ok"] is True and res.get("cascaded_sections") == 2, res
        cur = await db_conn.execute(
            "SELECT id, word_budget FROM sections WHERE scheme_id=? ORDER BY id",
            (sid,))
        wb = {r["id"]: r["word_budget"] for r in await cur.fetchall()}
        assert wb["L3a"] == 2000 and wb["L4a"] == 2000 and wb["L4b"] == 2000
        assert wb["L2a"] == 1200 and wb["L1"] == 1200, "父级不受级联影响"

    async def test_no_flag_keeps_legacy_behavior(self, db_conn):
        """不传 cascade 标志：既有调用（含全部既有测试）行为逐字节不变。"""
        from app.models import SectionUpdate
        from app.routers.sections import update_section
        sid = await self._seed(db_conn)
        res = await update_section(
            sid, "L3a", SectionUpdate(word_budget=1800), db_conn)
        assert res["ok"] is True and "cascaded_sections" not in res
        cur = await db_conn.execute(
            "SELECT id, word_budget FROM sections WHERE scheme_id=?", (sid,))
        wb = {r["id"]: r["word_budget"] for r in await cur.fetchall()}
        assert wb["L3a"] == 1800 and wb["L4a"] == 1200 and wb["L4b"] == 1200

    async def test_cascade_on_leaf_is_noop(self, db_conn):
        from app.models import SectionUpdate
        from app.routers.sections import update_section
        sid = await self._seed(db_conn)
        res = await update_section(
            sid, "L4a",
            SectionUpdate(word_budget=900, cascade_word_budget=True),
            db_conn)
        assert res["ok"] is True and "cascaded_sections" not in res
        cur = await db_conn.execute(
            "SELECT word_budget FROM sections WHERE id='L4a'")
        assert (await cur.fetchone())["word_budget"] == 900

    def test_frontend_wiring(self):
        page = (BACKEND.parent / "frontend" / "src" / "pages"
                / "SchemeWorkbenchPage.tsx").read_text(encoding="utf-8")
        assert "cascade_word_budget" in page, "前端三级改预算必须传级联标志"
        assert "node.level === 3" in page, "级联面必须是三级节点"
        assert "walkDescendants" in page, "本地树必须同步级联后代"


# ============================================================
# F6 · 行尾自持（本轮归一的六个文件不得回潮）
# ============================================================
class TestEolSelfConsistency:
    @pytest.mark.parametrize("rel", [
        "app/services/content_blocks.py",
        "app/services/content_format_contract.py",
        "tests/test_numbering_unification.py",
    ])
    def test_backend_files_self_consistent(self, rel):
        b = (BACKEND / rel).read_bytes()
        crlf = b.count(b"\r\n")
        lf = b.count(b"\n") - crlf
        cr = b.count(b"\r") - crlf
        assert cr == 0 and (crlf == 0 or lf == 0), f"{rel} 行尾混合: CRLF={crlf} LF={lf} CR={cr}"
