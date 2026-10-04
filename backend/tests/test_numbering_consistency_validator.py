"""编号一致性显式跨校验器（2026-09-26）回归测试。

覆盖：
  - 单章 / 方案级编号一致性校验（捕获 D4 类「落库正文 vs 目录编号」漂移）
  - GET /numbering-consistency 校验端点
  - POST /numbering-consistency/repair 修复端点（按当前 outline 重新规范化落库正文）
  - 导出前严格模式（numbering_consistency_strict）直接 409 阻断
  - 一致性报告摘要辅助函数
"""
import json

import pytest
from fastapi import HTTPException


async def _seed_scheme(db, pid="p1", sid="s1"):
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)", (pid, "P"))
    await db.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", (sid, pid, "S"))
    await db.commit()


def _outline(id_: str, level: int) -> str:
    return json.dumps({"id": id_, "level": level}, ensure_ascii=False)


class TestNumberingConsistencyValidator:
    async def test_detects_stale_content(self, db_conn):
        """落库正文的子标题编号与当前 outline 不一致 → 标记不一致。"""
        from app.services.numbering import validate_section_content_numbering
        await _seed_scheme(db_conn)
        # outline 为 "1"（第 1 章），但正文写死旧号 "3.1"
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, parent_id, "
            "level, sort_order, outline_json, content) VALUES "
            "('sec1','s1','p1','工程概况','','1',0,?, '## 3.1 总体安排\n正文\n')",
            (_outline("1", 1),))
        await db_conn.commit()
        rep = await validate_section_content_numbering(
            db_conn, "s1", "sec1", "## 3.1 总体安排\n正文\n")
        assert rep["consistent"] is False
        assert rep["changed"] is True
        assert rep["diffs"]  # 存在 diff 行

    async def test_consistent_when_normalized(self, db_conn):
        """落库正文已按当前 outline 规范化 → 一致。"""
        from app.services.numbering import validate_section_content_numbering
        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, parent_id, "
            "level, sort_order, outline_json, content) VALUES "
            "('sec1','s1','p1','工程概况','','1',0,?, '## 1.1 总体安排\n正文\n')",
            (_outline("1", 1),))
        await db_conn.commit()
        rep = await validate_section_content_numbering(
            db_conn, "s1", "sec1", "## 1.1 总体安排\n正文\n")
        assert rep["consistent"] is True
        assert rep["changed"] is False

    async def test_scheme_summary(self, db_conn):
        """方案级汇总：1 章漂移 → mismatched=1、consistent=False。"""
        from app.services.numbering import validate_scheme_numbering_consistency
        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, parent_id, "
            "level, sort_order, outline_json, content) VALUES "
            "('sec1','s1','p1','工程概况','','1',0,?, '## 3.1 总体安排\n正文\n')",
            (_outline("1", 1),))
        await db_conn.commit()
        rep = await validate_scheme_numbering_consistency(db_conn, "s1")
        assert rep["checked"] == 1
        assert rep["mismatched"] == 1
        assert rep["consistent"] is False

    async def test_get_endpoint(self, db_conn):
        """GET /numbering-consistency 端点返回方案级报告。"""
        from app.routers.sections import get_numbering_consistency
        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, parent_id, "
            "level, sort_order, outline_json, content) VALUES "
            "('sec1','s1','p1','工程概况','','1',0,?, '## 3.1 总体安排\n正文\n')",
            (_outline("1", 1),))
        await db_conn.commit()
        rep = await get_numbering_consistency("s1", db=db_conn)
        assert rep["mismatched"] == 1

    async def test_repair_endpoint(self, db_conn):
        """POST /numbering-consistency/repair 修复后内容更新且变为一致。"""
        from app.routers.sections import get_numbering_consistency, repair_numbering_consistency
        from app.services.numbering import validate_scheme_numbering_consistency
        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, parent_id, "
            "level, sort_order, outline_json, content) VALUES "
            "('sec1','s1','p1','工程概况','','1',0,?, '## 3.1 总体安排\n正文\n')",
            (_outline("1", 1),))
        await db_conn.commit()

        before = await validate_scheme_numbering_consistency(db_conn, "s1")
        assert before["mismatched"] == 1

        res = await repair_numbering_consistency("s1", db=db_conn)
        assert res["fixed"] == 1

        cur = await db_conn.execute("SELECT content FROM sections WHERE id='sec1'")
        fixed = (await cur.fetchone())["content"]
        assert "## 1.1 总体安排" in fixed, fixed

        after = await get_numbering_consistency("s1", db=db_conn)
        assert after["mismatched"] == 0


class TestExportStrictGuard:
    async def test_strict_guard_raises_on_drift(self, db_conn, monkeypatch):
        """导出前严格模式：落库正文与目录编号不一致 → 409 阻断。"""
        from app.config import settings
        from app.routers.export import export_docx
        monkeypatch.setattr(settings, "numbering_consistency_strict", True)

        await db_conn.execute(
            "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
            ("s1", "p1", "测试专项方案"))
        # outline "1" 但正文写死 "9.9"（漂移）；outline_json 必须有值（校验器读它算编号）
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, "
            "title, status, outline_json, content) VALUES (?,?,?,?,?,?,?,?,?)",
            ("c1", "s1", "", 0, 1, "第一章 总体概述", "generated",
             json.dumps({"id": "1", "level": 1}, ensure_ascii=False),
             "## 9.9 总体安排\n这里是第一章正文。\n"))
        await db_conn.commit()

        with pytest.raises(HTTPException) as exc:
            await export_docx("s1", {}, db=db_conn)
        assert exc.value.status_code == 409
        assert exc.value.detail.get("error") == "numbering_inconsistency"

    async def test_non_strict_guard_warns_not_raises(self, db_conn, monkeypatch):
        """导出前非严格模式（默认）：漂移仅告警、不阻断导出。"""
        from app.config import settings
        from app.routers.export import export_docx
        monkeypatch.setattr(settings, "numbering_consistency_strict", False)

        await db_conn.execute(
            "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
            ("s1", "p1", "测试专项方案"))
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, "
            "title, status, outline_json, content) VALUES (?,?,?,?,?,?,?,?,?)",
            ("c1", "s1", "", 0, 1, "第一章 总体概述", "generated",
             json.dumps({"id": "1", "level": 1}, ensure_ascii=False),
             "## 9.9 总体安排\n这里是第一章正文。\n"))
        await db_conn.commit()

        # 非严格模式不应抛异常（导出成稿仍由 _compute_subheading 重算保证正确）
        doc = await export_docx("s1", {}, db=db_conn)
        assert doc is not None

    async def test_pdf_strict_guard_raises_on_drift(self, db_conn, monkeypatch):
        """PDF 导出与 DOCX 共用同一守卫：严格模式下漂移 → 409（转换前抛出）。"""
        from app.config import settings
        from app.routers.export import export_pdf
        monkeypatch.setattr(settings, "numbering_consistency_strict", True)

        await db_conn.execute(
            "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
            ("s1", "p1", "测试专项方案"))
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, "
            "title, status, outline_json, content) VALUES (?,?,?,?,?,?,?,?,?)",
            ("c1", "s1", "", 0, 1, "第一章 总体概述", "generated",
             json.dumps({"id": "1", "level": 1}, ensure_ascii=False),
             "## 9.9 总体安排\n这里是第一章正文。\n"))
        await db_conn.commit()

        # 守卫在 _prepare_export / DOCX 生成 / PDF 转换之前执行，
        # 因此本测试不依赖任何转换工具（Word COM / docx2pdf / LibreOffice）
        with pytest.raises(HTTPException) as exc:
            await export_pdf("s1", {}, db=db_conn)
        assert exc.value.status_code == 409
        assert exc.value.detail.get("error") == "numbering_inconsistency"


class TestConsistencySummary:
    def test_summary_truncates(self):
        """_summarize_numbering_consistency 压缩报告为单行可读摘要。"""
        from app.routers.export import _summarize_numbering_consistency
        report = {
            "mismatched": 1,
            "sections": [
                {"section_id": "abcdef01", "consistent": False,
                 "diffs": [{"tag": "replace", "old": ["## 3.1 总体安排"],
                            "new": ["## 1.1 总体安排"]}]},
            ],
        }
        s = _summarize_numbering_consistency(report)
        assert "abcdef01" in s
        assert "1.1 总体安排" in s


class TestNumberingVersioning:
    """编号版本管理/回滚（2026-09-26）：修复建快照 → 回滚可撤销 → 版本可查。"""

    async def _seed_drifted(self, db_conn):
        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, parent_id, "
            "level, sort_order, outline_json, content) VALUES "
            "('sec1','s1','p1','工程概况','','1',0,?, '## 3.1 总体安排\n正文\n')",
            (_outline("1", 1),))
        await db_conn.commit()

    async def test_repair_creates_version_snapshot(self, db_conn):
        """编号修复返回 snapshot_id，且修复内容生效。"""
        from app.routers.sections import repair_numbering_consistency
        await self._seed_drifted(db_conn)
        res = await repair_numbering_consistency("s1", db=db_conn)
        assert res["fixed"] == 1
        assert res["snapshot_id"]
        cur = await db_conn.execute("SELECT content FROM sections WHERE id='sec1'")
        assert "## 1.1 总体安排" in (await cur.fetchone())["content"]

    async def test_repair_without_drift_has_no_snapshot(self, db_conn):
        """无漂移时修复不建快照（snapshot_id=None）。"""
        from app.routers.sections import repair_numbering_consistency
        await _seed_scheme(db_conn)
        res = await repair_numbering_consistency("s1", db=db_conn)
        assert res["fixed"] == 0
        assert res["snapshot_id"] is None

    async def test_rollback_restores_and_is_undoable(self, db_conn):
        """回滚恢复修复前正文；回滚产生的 undo 快照可再次回滚（撤销回滚）。"""
        from app.routers.sections import repair_numbering_consistency, rollback_numbering_version
        from app.services.numbering import validate_scheme_numbering_consistency
        await self._seed_drifted(db_conn)
        res = await repair_numbering_consistency("s1", db=db_conn)
        snap_id = res["snapshot_id"]

        # 回滚：正文回到漂移态
        rb = await rollback_numbering_version("s1", snap_id, db=db_conn)
        assert rb["restored"] == 1
        assert rb["undo_snapshot_id"]
        cur = await db_conn.execute("SELECT content FROM sections WHERE id='sec1'")
        assert "## 3.1 总体安排" in (await cur.fetchone())["content"]
        after = await validate_scheme_numbering_consistency(db_conn, "s1")
        assert after["mismatched"] == 1

        # 撤销回滚（undo 快照也是 numbering_rollback）：重新修复
        undo = await rollback_numbering_version("s1", rb["undo_snapshot_id"], db=db_conn)
        assert undo["restored"] == 1
        cur = await db_conn.execute("SELECT content FROM sections WHERE id='sec1'")
        assert "## 1.1 总体安排" in (await cur.fetchone())["content"]

    async def test_versions_list(self, db_conn):
        """版本列表按新→旧返回，type 隔离且只含 numbering_* 快照。"""
        from app.routers.sections import (
            list_numbering_versions,
            repair_numbering_consistency,
            rollback_numbering_version,
        )
        await self._seed_drifted(db_conn)
        res = await repair_numbering_consistency("s1", db=db_conn)
        rb = await rollback_numbering_version("s1", res["snapshot_id"], db=db_conn)
        lst = await list_numbering_versions("s1", db=db_conn)
        assert [v["type"] for v in lst["versions"]] == [
            "numbering_rollback", "numbering_repair"]
        assert lst["versions"][0]["snapshot_id"] == rb["undo_snapshot_id"]
        assert lst["versions"][1]["section_ids"] == ["sec1"]

    async def test_rollback_guards(self, db_conn):
        """回滚安全约束：不存在→404；跨方案/非 numbering 类型→400。"""
        from app.routers.sections import rollback_numbering_version
        from app.services.repair_record import create_snapshot
        from fastapi import HTTPException
        await self._seed_drifted(db_conn)

        with pytest.raises(HTTPException) as e404:
            await rollback_numbering_version("s1", "ver_missing", db=db_conn)
        assert e404.value.status_code == 404

        # 跨方案：快照属于 s2
        await db_conn.execute(
            "INSERT INTO schemes (id, project_id, name) VALUES ('s2','p1','S2')")
        await db_conn.commit()
        other = await create_snapshot(
            db_conn, "s2", [{"section_id": "sec1", "content_before": "x"}],
            snapshot_type="numbering_repair")
        with pytest.raises(HTTPException) as e400:
            await rollback_numbering_version("s1", other, db=db_conn)
        assert e400.value.status_code == 400

        # 非 numbering 类型（一致性修复快照走 /consistency/rollback）
        cr = await create_snapshot(
            db_conn, "s1", [{"section_id": "sec1", "content_before": "x"}],
            snapshot_type="consistency_repair")
        with pytest.raises(HTTPException) as e400b:
            await rollback_numbering_version("s1", cr, db=db_conn)
        assert e400b.value.status_code == 400
