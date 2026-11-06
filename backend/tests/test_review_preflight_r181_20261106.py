"""R181（2026-11-06）· 审核与一致性遗留项守护测试。

覆盖本次落地的 6 项：
1. N-08：冲突 ID 改内容哈希 + 可重入幂等迁移（状态继承）；
2. B-06：拒绝回滚改为冲突级最小片段（不再整章回退）；
3. B-14：review_status 纳入缓存指纹（只改审核状态也必须失效）；
4. G10：/checklist 显式节点校验（重复 ID / 缺父节点 / 环）；
5. B-20 等结构性遗留的定性（已在 R179 闭环，此处用前端测试守护）；
6. 一致性分数「多批取最小」口径显式披露。

断言均锁定**行为**：任何一项退回旧实现都会变红（变异验证见报告）。
"""

from __future__ import annotations

import json

import pytest
from app.db import _migrate
from app.services.conflict_id import conflict_id, is_content_hash_id
from app.services.conflict_revert import revert_conflict_changes
from app.services.consistency_scanner import migrate_conflict_ids
from app.services.content_utils import validate_section_tree

SCHEME = "sch-r181"
PROJ = "prj-r181"
SEC_A = "sec-a"
SEC_B = "sec-b"


async def _seed_base(db) -> None:
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (PROJ, "P"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (SCHEME, PROJ, "S"))
    for sid, title, sort, parent in (
        (SEC_A, "工程概况", 1, ""),
        (SEC_B, "施工部署", 2, ""),
    ):
        await db.execute(
            "INSERT INTO sections(id,scheme_id,project_id,title,content,word_count,"
            "word_status,word_budget,status,level,sort_order,parent_id,review_status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, SCHEME, PROJ, title, "正文", 2, "ok", 100, "generated", 1, sort, parent, "pending"),
        )
    await db.commit()


async def _seed_conflict(db, cid: str, status: str, value: str = "120 日历天", scan_id: str = "scan-1") -> None:
    await db.execute(
        "INSERT INTO consistency_conflicts(id,scheme_id,scan_id,conflict_type,severity,topic,"
        "occurrences,authoritative_value,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            cid,
            SCHEME,
            scan_id,
            "numeric",
            "high",
            "项目总工期",
            json.dumps([{"section_id": SEC_A, "value": value}], ensure_ascii=False),
            "90 日历天",
            status,
            "2026-11-06T00:00:00",
        ),
    )


# ---------------------------------------------------------------------------
# 1. 冲突 ID：内容哈希
# ---------------------------------------------------------------------------


class TestConflictIdHash:
    def test_id_is_content_hash_not_ordinal(self):
        cid = conflict_id("", "numeric", "项目总工期", [{"section_id": "s1", "value": "120"}])
        assert is_content_hash_id(cid), cid
        assert not is_content_hash_id("C001"), "旧顺序号不得再被识别为内容哈希 ID"

    def test_same_identity_same_id_regardless_of_order(self):
        a = conflict_id(
            "s",
            "numeric",
            "工期",
            [
                {"section_id": "s1", "value": "120"},
                {"section_id": "s2", "value": "130"},
            ],
        )
        b = conflict_id(
            "s",
            "numeric",
            "工期",
            [
                {"section_id": "s2", "value": "130"},
                {"section_id": "s1", "value": "120"},
            ],
        )
        assert a == b, "身份集合相同（顺序不同）必须得到同一 ID"

    def test_different_identity_different_id(self):
        a = conflict_id("s", "numeric", "工期", [{"section_id": "s1", "value": "120"}])
        b = conflict_id("s", "numeric", "工期", [{"section_id": "s1", "value": "121"}])
        assert a != b

    def test_scheme_prefix_scopes_id(self):
        a = conflict_id("scheme-A", "numeric", "工期", [{"section_id": "s1", "value": "120"}])
        b = conflict_id("scheme-B", "numeric", "工期", [{"section_id": "s1", "value": "120"}])
        assert a != b
        assert a.split("-", 1)[1] == b.split("-", 1)[1]


# ---------------------------------------------------------------------------
# 2. 迁移：可重入、幂等、状态继承
# ---------------------------------------------------------------------------


class TestMigrateConflictIds:
    async def test_migrates_legacy_ids_and_inherits_status(self, db_conn):
        await _seed_base(db_conn)
        await _seed_conflict(db_conn, "C001", "accepted", "120 日历天")
        await _seed_conflict(db_conn, "C002", "pending", "130 日历天")
        await db_conn.execute(
            "INSERT INTO consistency_repairs(id,scheme_id,scan_id,mode,items,created_at) " "VALUES(?,?,?,?,?,?)",
            (
                "rep-1",
                SCHEME,
                "scan-1",
                "auto",
                json.dumps([{"conflict_id": "C001"}, {"conflict_id": "C002"}], ensure_ascii=False),
                "2026-11-06T00:00:00",
            ),
        )
        await db_conn.commit()

        stats = await migrate_conflict_ids(db_conn, SCHEME)
        assert stats["migrated"] == 2, stats
        assert stats["error"] == ""

        cur = await db_conn.execute(
            "SELECT id, status FROM consistency_conflicts WHERE scheme_id=? ORDER BY id", (SCHEME,)
        )
        rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 2
        assert all(is_content_hash_id(r["id"]) for r in rows), rows
        statuses = {r["status"] for r in rows}
        assert statuses == {"accepted", "pending"}, "身份可证相同 → 状态必须继承，不得重置"

        cur = await db_conn.execute("SELECT items FROM consistency_repairs WHERE id='rep-1'")
        items = json.loads((await cur.fetchone())["items"])
        assert all(is_content_hash_id(it["conflict_id"]) for it in items), items

    async def test_migration_is_idempotent(self, db_conn):
        await _seed_base(db_conn)
        await _seed_conflict(db_conn, "C001", "repaired")
        await db_conn.commit()
        first = await migrate_conflict_ids(db_conn, SCHEME)
        assert first["migrated"] == 1
        second = await migrate_conflict_ids(db_conn, SCHEME)
        assert second["migrated"] == 0 and second["merged"] == 0, "第二次必须零写入"
        cur = await db_conn.execute("SELECT COUNT(*) AS n FROM consistency_conflicts WHERE scheme_id=?", (SCHEME,))
        assert (await cur.fetchone())["n"] == 1

    async def test_unprovable_identity_resets_pending_with_reason(self, db_conn, caplog):
        """坏数据无法证明身份：重置 pending（不得继承），并记录原因。"""
        await _seed_base(db_conn)
        await _seed_conflict(db_conn, "C001", "accepted")
        await db_conn.execute("UPDATE consistency_conflicts SET occurrences='{bad json' WHERE id='C001'")
        await db_conn.commit()

        stats = await migrate_conflict_ids(db_conn, SCHEME)
        assert stats["reset"] == 1, stats
        cur = await db_conn.execute("SELECT id, status FROM consistency_conflicts WHERE id='C001'")
        row = dict(await cur.fetchone())
        assert row["id"] == "C001", "身份不可证 → 不迁移 ID，留给下一轮扫描重新生成"
        assert row["status"] == "pending", "身份不可证 → 必须重置 pending，不得继承 accepted"
        assert any("不是合法 JSON" in r.message for r in caplog.records), "必须记录重置原因"

    async def test_run_scan_invokes_migration_and_reports_stats(self, db_conn, monkeypatch):
        """生产接线：run_scan 必须真的调用迁移，而不只是函数存在。"""
        from app.services import consistency_scanner as scanner

        await _seed_base(db_conn)
        await db_conn.execute("DELETE FROM sections WHERE scheme_id=?", (SCHEME,))
        await db_conn.commit()
        called: list[str] = []

        async def _fake_migrate(db, scheme_id):
            called.append(scheme_id)
            return {"scanned": 0, "migrated": 0, "merged": 0, "reset": 0, "repairs_updated": 0, "error": ""}

        monkeypatch.setattr(scanner, "migrate_conflict_ids", _fake_migrate)
        result = await scanner.run_scan(
            db_conn,
            scheme_id=SCHEME,
            project_id="",
            scheme_name="测试方案",
            scheme_type="test",
            use_cache=False,
        )
        assert called == [SCHEME], "run_scan 必须在扫描前执行 ID 迁移"
        assert result["id_migration"]["scanned"] == 0

    async def test_duplicate_identity_merges_and_keeps_highest_status(self, db_conn):
        await _seed_base(db_conn)
        canon = conflict_id(SCHEME, "numeric", "项目总工期", [{"section_id": SEC_A, "value": "120 日历天"}])
        # 同一身份：一条已是哈希 ID（pending），一条旧顺序号（accepted）
        await _seed_conflict(db_conn, canon, "pending")
        await _seed_conflict(db_conn, "C009", "accepted")
        await db_conn.commit()

        stats = await migrate_conflict_ids(db_conn, SCHEME)
        assert stats["merged"] == 1, stats
        cur = await db_conn.execute("SELECT id, status FROM consistency_conflicts WHERE scheme_id=?", (SCHEME,))
        rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 1, "重复身份必须合并为一行"
        assert rows[0]["id"] == canon
        assert rows[0]["status"] == "accepted", "多行同一身份取状态等级最高（accepted > pending）"


# ---------------------------------------------------------------------------
# 3. review_status 指纹
# ---------------------------------------------------------------------------


class TestReviewStateFingerprint:
    async def test_hash_changes_when_only_review_status_changes(self, db_conn):
        from app.services.scheme_fingerprint import review_state_hash

        await _seed_base(db_conn)
        h1 = await review_state_hash(db_conn, SCHEME)
        await db_conn.execute("UPDATE sections SET review_status='approved' WHERE id=?", (SEC_A,))
        await db_conn.commit()
        h2 = await review_state_hash(db_conn, SCHEME)
        assert h1 and h2 and h1 != h2, "只改 review_status 也必须改变审核状态指纹"
        # 还原后必须回到原指纹（确定性，无时间戳/随机噪音）
        await db_conn.execute("UPDATE sections SET review_status='pending' WHERE id=?", (SEC_A,))
        await db_conn.commit()
        assert await review_state_hash(db_conn, SCHEME) == h1

    async def test_run_is_stale_when_review_hash_differs(self, db_conn):
        from app.routers.compliance import _content_fingerprint, _run_is_stale
        from app.services.scheme_fingerprint import review_state_hash

        await _seed_base(db_conn)
        fp = await _content_fingerprint(db_conn, SCHEME)
        r1 = await review_state_hash(db_conn, SCHEME)
        await db_conn.execute("UPDATE sections SET review_status='approved' WHERE id=?", (SEC_A,))
        await db_conn.commit()
        r2 = await review_state_hash(db_conn, SCHEME)
        assert fp == await _content_fingerprint(db_conn, SCHEME), "审核状态变化不得影响正文指纹"

        row = {"content_fingerprint": fp, "review_state_hash": r1}
        stale, fresh = True, False
        assert await _run_is_stale(db_conn, SCHEME, row, current_fingerprint=fp, current_review_hash=r2) is stale
        assert await _run_is_stale(db_conn, SCHEME, row, current_fingerprint=fp, current_review_hash=r1) is fresh
        # 历史行没有审核状态指纹 → fail-open，不得误报过期
        legacy = {"content_fingerprint": fp, "review_state_hash": ""}
        assert await _run_is_stale(db_conn, SCHEME, legacy, current_fingerprint=fp, current_review_hash=r2) is fresh

    async def test_overview_cache_invalidated_by_review_status_only(self, db_conn, monkeypatch):
        """只改 review_status，不动正文 → 总检进程内缓存也必须失效。"""
        from app.routers import compliance

        await _seed_base(db_conn)
        compliance._OVERVIEW_RECENT.pop(SCHEME, None)
        calls: list[str] = []

        async def _fake_compute(db, scheme_id, *, persist=True, fingerprint=None, review_hash=None):
            calls.append(str(review_hash or ""))
            return {
                "scheme_id": scheme_id,
                "review_state_hash": review_hash,
                "score": 80.0,
            }

        monkeypatch.setattr(compliance, "_readiness_overview_compute", _fake_compute)
        try:
            first = await compliance.readiness_overview(SCHEME, db=db_conn)
            second = await compliance.readiness_overview(SCHEME, db=db_conn)
            assert first["cached"] is False and second["cached"] is True
            await db_conn.execute("UPDATE sections SET review_status='approved' WHERE id=?", (SEC_A,))
            await db_conn.commit()
            third = await compliance.readiness_overview(SCHEME, db=db_conn)
            assert third["cached"] is False, "只改 review_status 也必须失效缓存"
            assert len(calls) == 2, calls
        finally:
            compliance._OVERVIEW_RECENT.pop(SCHEME, None)

    async def test_migration_adds_review_state_hash_column(self, db_conn):
        cur = await db_conn.execute("PRAGMA table_info(preflight_runs)")
        cols = {r["name"] for r in await cur.fetchall()}
        assert "review_state_hash" in cols, "迁移必须为历史库补列（可重入）"
        # 再跑一次迁移必须无副作用
        await _migrate(db_conn)
        cur = await db_conn.execute("PRAGMA table_info(preflight_runs)")
        assert "review_state_hash" in {r["name"] for r in await cur.fetchall()}


# ---------------------------------------------------------------------------
# 4. /checklist 显式树校验
# ---------------------------------------------------------------------------


class TestSectionTreeValidation:
    def test_normal_tree_is_dfs_ordered(self):
        sections = [
            {"id": "c", "parent_id": "a", "title": "C", "sort_order": 2},
            {"id": "a", "parent_id": "", "title": "A", "sort_order": 1},
            {"id": "b", "parent_id": "a", "title": "B", "sort_order": 1},
            {"id": "d", "parent_id": "", "title": "D", "sort_order": 0},
        ]
        ordered, issues = validate_section_tree(sections)
        assert [s["id"] for s in ordered] == ["d", "a", "b", "c"], "前序 DFS：先根，再按 sort_order 展开子节点"
        assert issues == []

    def test_duplicate_id_reports_and_emits_once(self):
        sections = [
            {"id": "a", "parent_id": "", "title": "A", "sort_order": 1},
            {"id": "a", "parent_id": "", "title": "A 重复", "sort_order": 2},
            {"id": "b", "parent_id": "a", "title": "B", "sort_order": 1},
        ]
        ordered, issues = validate_section_tree(sections)
        assert [s["id"] for s in ordered] == ["a", "b"], "重复 ID 只能出现一次（否则子节点会被渲染两遍）"
        assert any(i["code"] == "duplicate_id" and i["id"] == "a" and i["count"] == 2 for i in issues)

    def test_missing_parent_reports_and_keeps_node(self):
        sections = [{"id": "a", "parent_id": "ghost", "title": "A", "sort_order": 1}]
        ordered, issues = validate_section_tree(sections)
        assert [s["id"] for s in ordered] == ["a"], "缺父节点不得静默丢行"
        assert any(i["code"] == "missing_parent" and i["parent_id"] == "ghost" for i in issues)

    def test_cycle_reports_and_keeps_all_nodes(self):
        sections = [
            {"id": "a", "parent_id": "b", "title": "A", "sort_order": 1},
            {"id": "b", "parent_id": "a", "title": "B", "sort_order": 2},
            {"id": "c", "parent_id": "a", "title": "C", "sort_order": 3},
        ]
        ordered, issues = validate_section_tree(sections)
        assert sorted(s["id"] for s in ordered) == ["a", "b", "c"], "环上节点必须全部出现且仅一次"
        assert any(i["code"] == "cycle" and set(i["ids"]) == {"a", "b"} for i in issues)

    def test_self_parent_reports(self):
        sections = [{"id": "a", "parent_id": "a", "title": "A", "sort_order": 1}]
        ordered, issues = validate_section_tree(sections)
        assert [s["id"] for s in ordered] == ["a"]
        assert any(i["code"] == "self_parent" for i in issues)

    async def test_checklist_endpoint_surfaces_tree_issues(self, db_conn):
        from app.routers.review import review_checklist

        await _seed_base(db_conn)
        await db_conn.execute("UPDATE sections SET parent_id='ghost' WHERE id=?", (SEC_B,))
        await db_conn.commit()
        # 直接调用路由函数：ASGITransport 使用应用自己的全局连接，
        # 与 db_conn fixture 的临时库不是同一个，会读到空数据。
        body = await review_checklist(SCHEME, db=db_conn)
        assert body["tree_ok"] is False
        assert any(i["code"] == "missing_parent" for i in body["tree_issues"])
        assert {it["id"] for it in body["items"]} == {SEC_A, SEC_B}, "异常节点不得从清单里消失"


# ---------------------------------------------------------------------------
# 5. 冲突级最小范围回滚
# ---------------------------------------------------------------------------


class TestConflictLevelRollback:
    def test_reverts_only_the_conflict_value(self):
        before = "本工程总工期为 120 天，架体高度 50 m。备注：原文保留。"
        current = "本工程总工期为 90 天，架体高度 45 m。备注：原文保留。"
        text, reason = revert_conflict_changes(
            before,
            current,
            wrong_values=["120 天"],
            authoritative_value="90 天",
            preserve_values=["50 m", "45 m"],
        )
        assert reason == "reverted"
        assert "120 天" in text, "被拒绝的取值必须回到原文"
        assert "45 m" in text and "50 m" not in text, "同章其它已接受的修复不得被连坐回退"
        assert "备注：原文保留。" in text

    def test_not_located_when_no_attributable_hunk(self):
        text, reason = revert_conflict_changes("甲", "乙", wrong_values=["不存在的取值"])
        assert reason == "not_located"
        assert text == "乙", "无法归因时不得改动正文（更不得整章回退）"

    def test_unchanged_when_already_before(self):
        text, reason = revert_conflict_changes("原文", "原文", wrong_values=["原文"])
        assert (text, reason) == ("原文", "unchanged")

    async def _seed_repair(self, db, items, before, current):
        await _seed_base(db)
        await db.execute("UPDATE sections SET content=? WHERE id=?", (current, SEC_A))
        rid = "rep-r181"
        await db.execute(
            "INSERT INTO scheme_snapshots(id,scheme_id,type,sections,created_at) VALUES(?,?,?,?,?)",
            (
                "ver-r181",
                SCHEME,
                "consistency_repair",
                json.dumps([{"section_id": SEC_A, "content_before": before}], ensure_ascii=False),
                "2026-11-06T00:00:00",
            ),
        )
        await db.execute(
            "INSERT INTO consistency_repairs(id,scheme_id,scan_id,mode,items,snapshot_id,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                rid,
                SCHEME,
                "scan-1",
                "auto",
                json.dumps(items, ensure_ascii=False),
                "ver-r181",
                "pending_confirm",
                "2026-11-06T00:00:00",
            ),
        )
        await db.commit()
        return rid

    async def test_confirm_reject_only_reverts_that_conflict(self, db_conn):
        from app.routers.consistency_repair import confirm

        before = "本工程总工期为 120 天，架体高度 50 m。备注：原文保留。"
        current = "本工程总工期为 90 天，架体高度 45 m。备注：原文保留。"
        items = [
            {
                "conflict_id": "cid-duration",
                "section_id": SEC_A,
                "status": "repaired",
                "current_value": "120 天",
                "authoritative_value": "90 天",
            },
            {
                "conflict_id": "cid-height",
                "section_id": SEC_A,
                "status": "repaired",
                "current_value": "50 m",
                "authoritative_value": "45 m",
            },
        ]
        rid = await self._seed_repair(db_conn, items, before, current)

        result = await confirm(
            SCHEME,
            {"repair_id": rid, "accepted": ["cid-height"], "rejected": ["cid-duration"]},
            db=db_conn,
        )
        cur = await db_conn.execute("SELECT content FROM sections WHERE id=?", (SEC_A,))
        content = (await cur.fetchone())["content"]
        assert "120 天" in content, "被拒绝冲突必须回滚"
        assert "45 m" in content and "50 m" not in content, "整章回退会把已接受的另一处修复一并抹掉（本断言即护栏）"
        assert result["restored_sections"] == [SEC_A]
        assert result["rejected_not_restored"] == []
        entry = next(r for r in result["conflict_restore_results"] if r["conflict_id"] == "cid-duration")
        assert entry["scope"] == "conflict" and entry["restored"] is True

    async def test_confirm_legacy_items_fall_back_to_section_with_flag(self, db_conn):
        from app.routers.consistency_repair import confirm

        before = "修复前的原正文"
        current = "修复后的错误正文"
        rid = await self._seed_repair(
            db_conn,
            [{"conflict_id": "cid-legacy", "section_id": SEC_A, "status": "repaired"}],
            before,
            current,
        )
        result = await confirm(SCHEME, {"repair_id": rid, "rejected": ["cid-legacy"]}, db=db_conn)
        cur = await db_conn.execute("SELECT content FROM sections WHERE id=?", (SEC_A,))
        assert (await cur.fetchone())["content"] == before, "历史批次无取值元数据 → 仅允许显式整章降级"
        entry = result["conflict_restore_results"][0]
        assert entry["scope"] == "section_fallback"
        assert entry["reason"] == "missing_value_metadata", "降级必须显式记账，不得冒充冲突级回滚"


# ---------------------------------------------------------------------------
# 6. 一致性分数口径披露
# ---------------------------------------------------------------------------


class TestConsistencyScoreRule:
    async def test_latest_reports_min_across_batches_rule(self, db_conn):
        from app.routers.compliance import latest_consistency_audit

        await _seed_base(db_conn)
        await db_conn.execute(
            "INSERT INTO consistency_audit(id,project_id,scheme_id,score,issues,counts_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "audit-1",
                PROJ,
                SCHEME,
                82.0,
                "[]",
                json.dumps({"batches": 3, "covered_chars": 9000}),
                "2026-11-06T00:00:00",
            ),
        )
        await db_conn.commit()
        item = await latest_consistency_audit(SCHEME, db=db_conn)
        assert item["exists"] is True
        assert item["score_rule"] == "min_across_batches"
        assert "取各批最低分" in item["score_note"] and "3 批" in item["score_note"]

    def test_single_batch_note(self):
        from app.routers.compliance import _consistency_score_note

        note = _consistency_score_note(1)
        assert "1 批" in note and "取各批最低分" in note
