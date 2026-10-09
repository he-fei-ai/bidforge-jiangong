"""数据流审计主要风险修复回归测试（2026-09-23）

覆盖三项高优先级修复：
- P1：解析阶段被截断到 MAX_PARSED_CHARS 的文档，事实提取链必须能识别并告警
      （判定逻辑 _is_truncated 对「已达上限」与「历史 8 万上限」都返回 True）。
- P5：_save_outline_to_db(lock_roots=True) 把「整表重建」与「一级锁定」并入
      同一事务提交；roots_locked 随结果返回；lock_roots=False 不产生锁定副作用。
- P10：审计批量落库 _flush_audit_buffer 改走独立写事务连接 write_tx_conn，
      不再在全局共享连接 get_conn() 上 executemany+commit（防并发协程搭车提交）。
"""
import uuid

import pytest


# ============================================================
# 辅助数据
# ============================================================
async def _seed_scheme(db, sid="s1", pid="p1"):
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)",
                     (pid, "测试项目"))
    await db.execute("INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES (?,?,?)",
                     (sid, pid, "测试方案"))
    await db.commit()


# ============================================================
# P1：解析截断对下游可见
# ============================================================
class TestTruncationDetection:
    def test_doc_at_current_limit_is_truncated(self):
        """落库正文长度 == MAX_PARSED_CHARS 即视为已截断（写入侧 stored=text[:MAX]）。"""
        from app.routers.global_facts import MAX_PARSED_CHARS, _is_truncated
        assert _is_truncated(MAX_PARSED_CHARS) is True
        assert _is_truncated(MAX_PARSED_CHARS + 1) is True

    def test_doc_below_limit_not_truncated(self):
        from app.routers.global_facts import MAX_PARSED_CHARS, _is_truncated
        assert _is_truncated(MAX_PARSED_CHARS - 1) is False
        assert _is_truncated(12345) is False

    def test_legacy_limit_still_flagged(self):
        """早期按 8 万字截断的历史文档，长度恰等于旧上限也必须判为截断（可自愈重解析）。"""
        from app.routers.global_facts import _is_truncated
        assert _is_truncated(80_000) is True

    def test_generate_facts_side_predicate(self):
        """复刻 generate-facts 侧的筛选谓词：只挑出被截断文档的文件名。"""
        from app.routers.global_facts import MAX_PARSED_CHARS, _is_truncated
        parsed_docs = [
            ("完整资料.docx", "x" * 1000),
            ("超长招标.pdf", "y" * MAX_PARSED_CHARS),
            ("旧截断文档.doc", "z" * 80_000),
        ]
        trunc = [name for name, text in parsed_docs if _is_truncated(len(text))]
        assert trunc == ["超长招标.pdf", "旧截断文档.doc"]


# ============================================================
# P5：save-outline 一级锁定并入同一事务
# ============================================================
@pytest.mark.asyncio
class TestSaveOutlineLockRoots:
    async def test_lock_roots_sets_level1_in_single_call(self, db_conn):
        from app.routers.sections import _save_outline_to_db
        await _seed_scheme(db_conn)
        outline = [
            {"title": "第一章 工程概况", "children": [{"title": "1.1 依据"}]},
            {"title": "第二章 施工部署", "children": []},
        ]
        res = await _save_outline_to_db(db_conn, "s1", outline,
                                        source="测试", lock_roots=True)
        assert res.get("roots_locked") is True
        # 一级全部 locked=1
        cur = await db_conn.execute(
            "SELECT title, locked FROM sections WHERE scheme_id='s1' AND level=1"
            " ORDER BY sort_order")
        rows = await cur.fetchall()
        assert len(rows) == 2
        assert all(r["locked"] == 1 for r in rows)
        # 二级不应被锁定
        cur = await db_conn.execute(
            "SELECT locked FROM sections WHERE scheme_id='s1' AND level=2")
        sub = await cur.fetchall()
        assert sub and all(r["locked"] == 0 for r in sub)

    async def test_no_lock_roots_leaves_unlocked(self, db_conn):
        """默认 lock_roots=False：不产生锁定副作用（向后兼容）。"""
        from app.routers.sections import _save_outline_to_db
        await _seed_scheme(db_conn)
        res = await _save_outline_to_db(
            db_conn, "s1", [{"title": "第一章", "children": []}], source="测试")
        assert "roots_locked" not in res
        cur = await db_conn.execute(
            "SELECT locked FROM sections WHERE scheme_id='s1' AND level=1")
        assert all(r["locked"] == 0 for r in await cur.fetchall())

    async def test_endpoint_returns_roots_locked(self, db_conn):
        """save_outline 端点透传 lock_roots，roots_locked 来自单一事务提交后。"""
        from app.routers.sections import save_outline
        await _seed_scheme(db_conn, sid="s1")
        res = await save_outline("s1", {
            "outline": [{"title": "第一章 工程概况", "children": []}],
            "lock_roots": True,
        }, db_conn)
        assert res.get("roots_locked") is True
        cur = await db_conn.execute(
            "SELECT locked FROM sections WHERE scheme_id='s1' AND level=1")
        assert all(r["locked"] == 1 for r in await cur.fetchall())


# ============================================================
# P10：审计批量落库走独立写事务连接
# ============================================================
@pytest.mark.asyncio
class TestAuditBufferUsesWriteTx:
    async def test_flush_persists_via_write_tx_conn(self, db_conn, monkeypatch):
        """反例回归：_flush_audit_buffer 必须经 write_tx_conn（独立事务），
        且落库结果对同一测试连接可见（conftest 已把 write_tx_conn 指向内存库）。"""
        import app.services.ai.provider_factory as pf
        used = {"write_tx": 0}
        _orig_wtc = pf.write_tx_conn

        def _spy_write_tx_conn():
            used["write_tx"] += 1
            return _orig_wtc()

        monkeypatch.setattr(pf, "write_tx_conn", _spy_write_tx_conn)

        pf._audit_buffer.clear()
        pf._audit_last_flush["ts"] = 0.0
        # 手动塞入若干行，直接调用 flush（绕过阈值判定）
        for _ in range(4):
            pf._audit_buffer.append((
                uuid.uuid4().hex, "deepseek", "deepseek-chat", "chat",
                10, 20, 0, 1.0, 1, "", "content_draft"))
        await pf._flush_audit_buffer()

        assert used["write_tx"] == 1, "审计批量落库应通过 write_tx_conn 独立事务"
        cur = await db_conn.execute("SELECT COUNT(*) AS c FROM ai_audit_logs")
        assert (await cur.fetchone())["c"] == 4
        pf._audit_buffer.clear()

    async def test_flush_no_longer_uses_shared_get_conn(self, db_conn, monkeypatch):
        """确保不再回退到全局共享连接 get_conn 落库审计（防回归）。"""
        import app.services.ai.provider_factory as pf

        async def _boom_get_conn():
            raise AssertionError("审计落库不应再使用共享连接 get_conn()")

        monkeypatch.setattr(pf, "get_conn", _boom_get_conn)
        pf._audit_buffer.clear()
        pf._audit_last_flush["ts"] = 0.0
        pf._audit_buffer.append((
            uuid.uuid4().hex, "agnes", "m", "chat", 1, 1, 0, 0.5, 1, "", "outline_draft"))
        await pf._flush_audit_buffer()  # 不应抛断言
        cur = await db_conn.execute("SELECT COUNT(*) AS c FROM ai_audit_logs")
        assert (await cur.fetchone())["c"] == 1
        pf._audit_buffer.clear()
