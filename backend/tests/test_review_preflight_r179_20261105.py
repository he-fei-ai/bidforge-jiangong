"""R179（2026-11-05）审核与预检域回归：跨轮冲突状态错配 + 恢复结果三态记账。

覆盖两个已确认缺陷：

| 缺陷 | 旧行为 | 后果 |
|---|---|---|
| 冲突 id 每轮按顺序重编，upsert 无条件保留 status | 上一轮已 accepted 的 ``C001`` 被本轮另一处冲突继承 | 新问题直接挂旧结论，自动仲裁跳过，用户误以为已处理 |
| /consistency/confirm 把 rejected_sections 全量当 restored_sections 返回 | 快照缺失时正文没回滚，却宣称「已恢复原文」 | 用户误判可放心导出，正文仍是未被接受的修复稿 |

两条断言均锚定可观测行为（数据库行内容 / 接口返回），不锚定源码字符串。
"""

from app.routers.consistency_repair import confirm
from app.services import repair_record
from app.services.consistency_scanner import persist_conflicts
from app.services.content_utils import text_word_count, word_status_for

SCHEME = "sch-r179"
PROJ = "prj-r179"
SEC = "sec-r179"


def _conflict(
    cid: str,
    *,
    topic: str = "项目总工期",
    value: str = "120 日历天",
    conflict_type: str = "numeric",
    section_id: str = SEC,
) -> dict:
    return {
        "id": cid,
        "conflict_type": conflict_type,
        "severity": "high",
        "topic": topic,
        "occurrences": [
            {
                "section_id": section_id,
                "section_title": "工程概况",
                "text": f"本工程{topic}为 {value}",
                "position": 0,
                "value": value,
                "conflict_id": cid,
            }
        ],
    }


async def _seed_section(db, content: str = "本工程总工期为 120 日历天。") -> None:
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (PROJ, "P"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (SCHEME, PROJ, "S"))
    await db.execute(
        "INSERT INTO sections(id,scheme_id,project_id,title,content,word_count,"
        "word_status,word_budget,status,level,sort_order) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            SEC,
            SCHEME,
            PROJ,
            "工程概况",
            content,
            text_word_count(content),
            word_status_for(text_word_count(content), 200),
            200,
            "generated",
            1,
            0,
        ),
    )
    await db.commit()


async def _status_of(db, cid: str) -> tuple[str, str]:
    cur = await db.execute(
        "SELECT status, topic FROM consistency_conflicts WHERE id=? AND scheme_id=?",
        (cid, SCHEME),
    )
    row = await cur.fetchone()
    return (row["status"], row["topic"])


class TestConflictUpsertIdentity:
    """同一 id 只有在「问题身份」未变时才允许继承旧 status。"""

    async def test_same_identity_keeps_accepted_status(self, db_conn):
        await persist_conflicts(db_conn, SCHEME, "scan-1", [_conflict("C001")])
        await db_conn.execute(
            "UPDATE consistency_conflicts SET status='accepted' WHERE id=? AND scheme_id=?",
            ("C001", SCHEME),
        )
        await db_conn.commit()

        # 第二轮同样的问题，仅出现位置变化（position 不参与身份签名）
        again = _conflict("C001")
        again["occurrences"][0]["position"] = 99
        await persist_conflicts(db_conn, SCHEME, "scan-2", [again])

        status, topic = await _status_of(db_conn, "C001")
        assert status == "accepted", "同一问题的跨轮重扫必须保留已确认结论"
        assert topic == "项目总工期"

    async def test_identity_change_resets_status_and_topic(self, db_conn):
        await persist_conflicts(db_conn, SCHEME, "scan-1", [_conflict("C001")])
        await db_conn.execute(
            "UPDATE consistency_conflicts SET status='accepted' WHERE id=? AND scheme_id=?",
            ("C001", SCHEME),
        )
        await db_conn.commit()

        # 顺序重编后 C001 变成另一处问题：主题与取值都不同
        changed = _conflict("C001", topic="混凝土强度等级", value="C25")
        await persist_conflicts(db_conn, SCHEME, "scan-2", [changed])

        status, topic = await _status_of(db_conn, "C001")
        assert status == "pending", "身份变化后不得继承旧 accepted 状态"
        assert topic == "混凝土强度等级", "冲突主题必须随最新扫描刷新，禁止旧主题配新问题"

    async def test_same_id_in_other_scheme_resets_status(self, db_conn):
        await persist_conflicts(db_conn, SCHEME, "scan-1", [_conflict("C001")])
        await db_conn.execute(
            "UPDATE consistency_conflicts SET status='accepted' WHERE id=? AND scheme_id=?",
            ("C001", SCHEME),
        )
        await db_conn.commit()

        # 历史/自定义调用方可能生成全局流水号：命中他人行时收回本方案并重置结论
        await persist_conflicts(db_conn, "scheme-other", "scan-x", [_conflict("C001")])
        cur = await db_conn.execute("SELECT scheme_id, status FROM consistency_conflicts WHERE id=?", ("C001",))
        row = await cur.fetchone()
        assert row is not None, "冲突行必须被收回最新扫描的方案"
        assert row["scheme_id"] == "scheme-other"
        assert row["status"] == "pending", "跨方案收回不得继承原方案的 accepted 结论"


class TestConfirmRestoreAccounting:
    """快照缺失时不得再宣称「已恢复原文」。"""

    async def _repair(self, db, *, snapshot_id: str) -> dict:
        return await repair_record.save_repair(
            db,
            scheme_id=SCHEME,
            scan_id="S1",
            mode="auto",
            items=[{"conflict_id": "C1", "section_id": SEC, "status": "repaired", "problems": []}],
            snapshot_id=snapshot_id,
            total_conflicts=1,
            stats={"repaired": 1, "failed": 0, "skipped": 0},
        )

    async def test_missing_snapshot_reports_skipped_not_restored(self, db_conn):
        await _seed_section(db_conn)
        rep = await self._repair(db_conn, snapshot_id="snap-missing")

        result = await confirm(
            scheme_id=SCHEME,
            body={"repair_id": rep["repair_id"], "accepted": [], "rejected": ["C1"]},
            db=db_conn,
        )

        assert result["restored_sections"] == [], "快照缺失时正文未回滚，不得报已恢复"
        assert result["restore_skipped_sections"] == [SEC], "必须单独记账未回滚章节"
        assert result["rescan_required"] is False, "正文未变则无需重扫"

        cur = await db_conn.execute("SELECT content FROM sections WHERE id=?", (SEC,))
        row = await cur.fetchone()
        assert row["content"] == "本工程总工期为 120 日历天。", "快照缺失不得改动正文"

    async def test_present_snapshot_reports_restored(self, db_conn):
        await _seed_section(db_conn, content="修复后的错误正文")
        snap = await repair_record.create_snapshot(
            db_conn, SCHEME, [{"section_id": SEC, "content_before": "修复前的原正文"}]
        )
        rep = await self._repair(db_conn, snapshot_id=snap)

        result = await confirm(
            scheme_id=SCHEME,
            body={"repair_id": rep["repair_id"], "accepted": [], "rejected": ["C1"]},
            db=db_conn,
        )

        assert result["restored_sections"] == [SEC]
        assert result["restore_skipped_sections"] == []
        assert result["rescan_required"] is True

        cur = await db_conn.execute("SELECT content, word_count FROM sections WHERE id=?", (SEC,))
        row = await cur.fetchone()
        assert row["content"] == "修复前的原正文"
        assert row["word_count"] == text_word_count("修复前的原正文")

    async def test_accepted_only_does_not_report_restore(self, db_conn):
        await _seed_section(db_conn)
        snap = await repair_record.create_snapshot(
            db_conn, SCHEME, [{"section_id": SEC, "content_before": "修复前的原正文"}]
        )
        rep = await self._repair(db_conn, snapshot_id=snap)

        result = await confirm(
            scheme_id=SCHEME,
            body={"repair_id": rep["repair_id"], "accepted": ["C1"], "rejected": []},
            db=db_conn,
        )

        assert result["restored_sections"] == []
        assert result["restore_skipped_sections"] == []
        assert result["rescan_required"] is False
        assert result["accepted"] == ["C1"]


class TestConflictIdentityHelper:
    def test_identity_ignores_position_and_order(self):
        from app.services.consistency_scanner import _conflict_identity

        a = [
            {"section_id": "s2", "value": "B", "position": 9},
            {"section_id": "s1", "value": "A", "position": 1},
        ]
        b = [
            {"section_id": "s1", "value": "A", "position": 7},
            {"section_id": "s2", "value": "B", "position": 2},
        ]
        assert _conflict_identity("numeric", "工期", a) == _conflict_identity("numeric", "工期", b)

    def test_identity_changes_with_value(self):
        from app.services.consistency_scanner import _conflict_identity

        a = [{"section_id": "s1", "value": "A"}]
        b = [{"section_id": "s1", "value": "B"}]
        assert _conflict_identity("numeric", "工期", a) != _conflict_identity("numeric", "工期", b)
        assert _conflict_identity("numeric", "工期", a) != _conflict_identity("text", "工期", a)
