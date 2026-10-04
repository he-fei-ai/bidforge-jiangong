"""七模块跨链路收口（R39 · 2026-10-04）回归护栏。

覆盖本轮修复的 9 个跨模块缺陷。每条断言都锚定**可观测行为**或**接线事实**，
避免"断言字符、不问语义"的假护栏。

  F1  P0  一致性冲突 ID 跨方案全局碰撞（consistency_scanner）
  F2  P1  /compliance/check 默认把空清单送进 AI（compliance）
  F3  P1  generate-facts 无同型任务重入守卫（sse_handlers + sections）
  F4  P1  ingest_parse_result / freshness 漏 R13 判空（doc_pipeline）
  F5  P1  提取完成后下游缓存不失效（bid_analysis）
  F8  P1  导出缓存 DOCX/PDF 的 cache_key 同值（export）
  F9  P2  目录整表重建不失效导出缓存（sections）
  F10 P2  list_facts docstring 写在函数体内（global_facts）
  F11 P1  merge_findings 去重时静默丢弃另一条链路的证据（audit_scoring）

设计约束（沿用本仓惯例）：零新增依赖、零数据迁移、零新增配置项。
"""
import re
import uuid
from pathlib import Path

import httpx
import pytest

import app.db as _appdb
from app.db import close_db, get_conn, init_db
from app.main import app

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_ROOT / "app"


def _src(rel: str) -> str:
    return (APP_DIR / rel).read_text(encoding="utf-8")


# ===========================================================================
# F1 · P0 一致性冲突 ID 跨方案全局碰撞
# ===========================================================================
class TestConflictIdSchemeScoped:
    def test_prefix_is_stable_and_scheme_scoped(self):
        from app.services.consistency_scanner import _conflict_id_prefix

        a1 = _conflict_id_prefix("scheme-A")
        a2 = _conflict_id_prefix("scheme-A")
        b1 = _conflict_id_prefix("scheme-B")
        assert a1 == a2, "同一方案的冲突 ID 前缀必须稳定"
        assert a1 != b1, "不同方案的前缀必须不同（否则仍会撞主键）"
        assert len(a1) == 7 and a1.endswith("-"), f"前缀形态异常: {a1!r}"

    def test_empty_scheme_id_keeps_legacy_format(self):
        from app.services.consistency_scanner import (
            _conflict_id_prefix, merge_conflicts)

        assert _conflict_id_prefix("") == ""
        rows = [{
            "conflict_type": "numeric", "topic": "项目总工期", "value": "120 日历天",
            "text": "t", "position": 1,
            "section_occurrences": [{"section_id": "S1", "section_title": "S1",
                                     "text": "t", "position": 0, "value": "120 日历天"}],
            "source": "ai_scan",
        }]
        out = merge_conflicts(rows, [])
        assert out[0]["id"] == "C001", (
            f"不传 scheme_id 时必须仍是 C001，实际 {out[0]['id']}")

    def test_merge_conflicts_with_scheme_id_is_scoped(self):
        from app.services.consistency_scanner import (
            _conflict_id_prefix, merge_conflicts)

        def _row(sid):
            return [{
                "conflict_type": "numeric", "topic": "项目总工期", "value": "120 日历天",
                "text": "t", "position": 1,
                "section_occurrences": [{"section_id": sid, "section_title": sid,
                                         "text": "t", "position": 0, "value": "120 日历天"}],
                "source": "ai_scan",
            }]

        out_a = merge_conflicts(_row("S1"), [], scheme_id="scheme-A")
        out_b = merge_conflicts(_row("S1"), [], scheme_id="scheme-B")
        assert out_a[0]["id"] != out_b[0]["id"], "不同方案的同序号冲突不得同 ID"
        assert out_a[0]["id"] == f"{_conflict_id_prefix('scheme-A')}C001"
        assert out_a[0]["id"].endswith("C001")

    async def test_persist_two_schemes_keeps_both(self, db_conn):
        from app.services.consistency_scanner import merge_conflicts, persist_conflicts

        def _row(sid):
            return [{
                "conflict_type": "numeric", "topic": "项目总工期", "value": "120 日历天",
                "text": f"{sid} 的正文", "position": 1,
                "section_occurrences": [{"section_id": f"sec-{sid}", "section_title": "ch1",
                                         "text": f"{sid} 的正文", "position": 0,
                                         "value": "120 日历天"}],
                "source": "ai_scan",
            }]

        await persist_conflicts(db_conn, "scheme-A", "scan-A",
                                merge_conflicts(_row("A"), [], scheme_id="scheme-A"))
        await persist_conflicts(db_conn, "scheme-B", "scan-B",
                                merge_conflicts(_row("B"), [], scheme_id="scheme-B"))

        cur = await db_conn.execute(
            "SELECT scheme_id, scan_id, occurrences FROM consistency_conflicts")
        rows = [dict(r) for r in await cur.fetchall()]
        by_scheme = {r["scheme_id"]: r for r in rows}
        assert set(by_scheme) == {"scheme-A", "scheme-B"}, (
            f"两方案的冲突必须各自存在，实际落库: {sorted(by_scheme)}")
        assert by_scheme["scheme-A"]["scan_id"] == "scan-A"
        assert by_scheme["scheme-B"]["scan_id"] == "scan-B"
        assert "B 的正文" not in (by_scheme["scheme-A"]["occurrences"] or "")

    def test_persist_upsert_updates_scheme_id(self):
        src = _src("services/consistency_scanner.py")
        m = re.search(r"ON CONFLICT\(id\) DO UPDATE SET(.{0,240})", src, re.S)
        assert m, "未找到 persist_conflicts 的 upsert 语句"
        assert "scheme_id=excluded.scheme_id" in m.group(1), (
            "DO UPDATE 必须更新 scheme_id")


# ===========================================================================
# F2 · P1 /compliance/check 默认把空清单送进 AI
# ===========================================================================
class TestComplianceCheckDefaultChecklist:
    @pytest.fixture
    async def cc_db(self, tmp_path, monkeypatch):
        original = _appdb.DB_PATH
        _appdb.DB_PATH = tmp_path / "cc-20261004.sqlite"
        await init_db()
        db = await get_conn()
        pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
                         (sid, pid, "s"))
        await db.commit()
        yield sid
        await close_db()
        _appdb.DB_PATH = original

    async def test_empty_body_falls_back_to_ai_rules(self, cc_db, monkeypatch):
        import app.routers.compliance as cc

        captured: dict = {}

        async def _fake_collect(messages, *a, **k):
            captured["system"] = messages[0]["content"]
            return {"results": []}, None

        async def _fake_facts(db, scheme_id):
            return "（无）"

        monkeypatch.setattr(cc, "collect_json_response", _fake_collect)
        monkeypatch.setattr(cc, "_load_facts_prompt_text", _fake_facts)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver",
                timeout=30.0) as client:
            resp = await client.post("/api/v1/compliance/check",
                                     json={"scheme_id": cc_db})
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}"
        system = captured.get("system", "")
        titles = [r.title for r in cc.ai_rules()]
        assert titles, "ai_rules() 不得为空"
        assert "[]" not in system, "提示词里仍出现空清单字面量"
        assert any(t in system for t in titles), (
            f"自由清单为空时必须回退 AI 规则全集，提示词尾部: {system[-300:]}")


# ===========================================================================
# F3 · P1 generate-facts 无同型任务重入守卫
# ===========================================================================
class TestFactsGenerationReentrancyGuard:
    def test_helper_detects_running_and_ignores_terminal(self):
        from app.routers.sections import facts_generation_in_progress
        import app.services.ai.task_registry as tr

        sid = "scheme-X"
        tr._tasks.clear()
        assert facts_generation_in_progress(sid) is None
        tr._tasks["t1"] = {"type": "facts_generation", "scheme_id": sid,
                           "status": "running"}
        assert facts_generation_in_progress(sid) == "running"
        tr._tasks["t1"]["status"] = "completed"
        assert facts_generation_in_progress(sid) is None, "终态任务不得阻塞重入"
        tr._tasks["t2"] = {"type": "facts_generation", "scheme_id": "other",
                           "status": "running"}
        assert facts_generation_in_progress(sid) is None, "其它方案的任务不得误伤"
        tr._tasks.clear()

    @pytest.fixture
    async def gf_db(self, tmp_path, monkeypatch):
        original = _appdb.DB_PATH
        _appdb.DB_PATH = tmp_path / "gf-20261004.sqlite"
        await init_db()
        db = await get_conn()
        pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
                         (sid, pid, "s"))
        await db.commit()
        yield sid
        await close_db()
        _appdb.DB_PATH = original

    async def test_second_request_rejected_with_409(self, gf_db):
        import app.services.ai.task_registry as tr

        tr._tasks.clear()
        tr._tasks["running-one"] = {"type": "facts_generation",
                                    "scheme_id": gf_db, "status": "running"}
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver",
                    timeout=30.0) as client:
                resp = await client.post(
                    f"/api/v1/sse/generate-facts/{gf_db}", json={})
            assert resp.status_code == 409, (
                f"同型任务在跑时重入必须 409，实际 {resp.status_code}")
        finally:
            tr._tasks.clear()

    async def test_no_running_task_is_allowed(self, gf_db):
        import app.services.ai.task_registry as tr

        tr._tasks.clear()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver",
                timeout=30.0) as client:
            resp = await client.post(f"/api/v1/sse/generate-facts/{gf_db}", json={})
        assert resp.status_code == 200, (
            f"无在跑任务时应正常放行，实际 {resp.status_code}")


# ===========================================================================
# F4 · P1 四层存储写路径漏 R13 判空
# ===========================================================================
class TestDocPipelineNoneCursorGuards:
    async def test_ingest_parse_result_survives_none_cursor(self, db_conn, monkeypatch):
        from app.services.doc_pipeline import pipeline

        store = pipeline.store
        for name in ("write_parsed_layer", "write_meta", "update_index",
                     "write_semantic_index"):
            if hasattr(store, name):
                monkeypatch.setattr(store, name, lambda *a, **k: None)
        monkeypatch.setattr(store, "read_meta", lambda *a, **k: {})
        if hasattr(store, "build_meta"):
            monkeypatch.setattr(store, "build_meta", lambda *a, **k: {})

        class _NoneOnceDb:
            def __init__(self, conn):
                self._c = conn
                self.none_hits = 0

            async def execute(self, sql, params=()):
                if "doc_chunks" in sql and "SELECT" in sql.upper():
                    self.none_hits += 1
                    return None
                return await self._c.execute(sql, params)

            async def executemany(self, sql, seq):
                return await self._c.executemany(sql, seq)

            async def commit(self):
                await self._c.commit()

        proxy = _NoneOnceDb(db_conn)
        try:
            res = await pipeline.ingest_parse_result(
                db=proxy, doc_id="doc-1", project_id="proj-1", file_name="a.txt",
                markdown="# 标题\n正文内容", page_count=1, parse_duration_s=0.1,
                parse_engine="test")
        except AttributeError as e:
            pytest.fail(f"db.execute 返回 None 时仍抛 AttributeError（R13 漏改）: {e}")
        assert proxy.none_hits >= 1, "变异守卫：本用例必须真的命中 None 分支"
        assert isinstance(res, dict)

    def test_freshness_write_path_guards_none(self):
        src = _src("routers/doc_pipeline.py")
        idx = src.find("async def document_freshness")
        assert idx > 0
        body = src[idx:idx + 3000]
        assert "UPDATE project_documents SET status=?" in body
        assert re.search(r"_\w+\s*=\s*await db\.execute\(\s*\"UPDATE project_documents",
                         body), "freshness 写路径未先把 execute 返回值接住"
        assert re.search(r"if\s+_\w+\s+is None", body), "freshness 写路径未判空"


# ===========================================================================
# F5 · P1 提取完成后下游缓存不失效
# ===========================================================================
class TestDownstreamInvalidationOnFinish:
    def test_both_finish_paths_call_invalidation(self):
        src = _src("routers/bid_analysis.py")
        calls = len(re.findall(r"_safe_invalidate_downstream\(", src))
        assert calls >= 3, (
            f"_safe_invalidate_downstream 出现 {calls} 次（应为 定义1 + 收尾2）")

    async def test_wrapper_is_fail_soft(self, db_conn, monkeypatch):
        from app.routers import bid_analysis as ba

        async def _boom(*a, **k):
            raise RuntimeError("缓存表缺失")

        monkeypatch.setattr(ba, "_invalidate_downstream_cache", _boom)
        await ba._safe_invalidate_downstream(db_conn, "proj-1", "scheme-1")

    async def test_wrapper_invokes_invalidation(self, db_conn, monkeypatch):
        from app.routers import bid_analysis as ba

        seen: list = []

        async def _spy(db, project_id, scheme_id=""):
            seen.append((project_id, scheme_id))
            return 0

        monkeypatch.setattr(ba, "_invalidate_downstream_cache", _spy)
        await ba._safe_invalidate_downstream(db_conn, "proj-1", "scheme-1")
        assert seen == [("proj-1", "scheme-1")]


# ===========================================================================
# F8 · P1 导出缓存 cache_key 同值
# ===========================================================================
class TestExportCacheKeyFormatScoped:
    def test_docx_and_pdf_cache_keys_differ(self):
        src = _src("routers/export.py")
        keys = re.findall(r"f\"\{scheme_id\}_\{config_hash\[:8\]\}([^\"]*)\"", src)
        assert "" in keys, "DOCX 的 cache_key 必须保持历史形态"
        assert any("pdf" in k for k in keys), "PDF 的 cache_key 必须带格式后缀"
        assert len(set(keys)) >= 2, f"两种格式的 cache_key 仍同值: {keys}"

    def test_pdf_content_hash_still_format_scoped(self):
        src = _src("routers/export.py")
        assert 'content_hash + "|fmt=pdf"' in src, "PDF 命中键丢失格式维度"


# ===========================================================================
# F9 · P2 目录整表重建不失效导出缓存
# ===========================================================================
class TestOutlineRebuildInvalidatesExportCache:
    def test_save_outline_invalidates_export_cache(self):
        src = _src("routers/sections.py")
        idx = src.find("async def _save_outline_to_db")
        assert idx > 0
        body = src[idx:idx + 40000]
        nxt = body.find("\nasync def ", 10)
        if nxt > 0:
            body = body[:nxt]
        assert "invalidate_consistency_scan_cache(db, scheme_id)" in body
        assert "invalidate_export_cache" in body, (
            "_save_outline_to_db 未失效导出缓存")


# ===========================================================================
# F10 · P2 list_facts docstring 写在函数体内
# ===========================================================================
class TestListFactsDocstring:
    def test_route_has_docstring(self):
        from app.routers.global_facts import list_facts

        assert list_facts.__doc__, (
            "list_facts.__doc__ 为 None：docstring 被写在函数体内")

    def test_no_stray_string_literal_after_param_normalization(self):
        src = _src("routers/global_facts.py")
        idx = src.find("async def list_facts")
        assert idx > 0
        body = src[idx:idx + 2500]
        tail = body[body.find("offset = 0"):]
        assert "查询全局事实" not in tail, "函数体内仍残留 docstring 副本"


# ===========================================================================
# F11 · P1 merge_findings 去重时静默丢弃证据
# ===========================================================================
class TestMergeFindingsKeepsEvidence:
    @staticmethod
    def _f(severity, evidence, section_ids=None, source="x"):
        return {
            "rule_id": "DLV-01", "dimension": "deliverability", "severity": severity,
            "title": "交付物缺失", "detail": "d", "evidence": list(evidence),
            "section_ids": list(section_ids or []), "source": source,
            "suggestion": "", "basis": "", "mode": "program",
        }

    def test_dropped_evidence_is_merged_not_discarded(self):
        from app.services.audit_scoring import merge_findings

        high = self._f("high", ["程序侧证据"], ["S1"], source="preflight")
        low = self._f("medium", ["导出侧第 1 章", "导出侧第 2 章"], ["S1", "S2"],
                      source="export_check")
        out = merge_findings([high], [low])
        assert len(out) == 1, "同 rule_id 仍必须去重为一条"
        kept = out[0]
        assert "程序侧证据" in kept["evidence"]
        for ev in ("导出侧第 1 章", "导出侧第 2 章"):
            assert ev in kept["evidence"], f"被丢弃链路的证据 {ev} 未并入"
        assert set(kept.get("section_ids") or []) == {"S1", "S2"}

    def test_reverse_order_same_result(self):
        from app.services.audit_scoring import merge_findings

        low = self._f("medium", ["低证据"], ["S9"], source="a")
        high = self._f("high", ["高证据"], ["S1"], source="b")
        out = merge_findings([low], [high])
        kept = out[0]
        assert kept["severity"] == "high"
        assert "低证据" in kept["evidence"], "顺序无关：低严重度先到也要并入"
        assert set(kept.get("section_ids") or []) == {"S1", "S9"}

    def test_merging_does_not_change_score(self):
        """合并只补证据，不得改变评分口径（score_findings 不读 count/evidence）。"""
        from app.services.audit_scoring import merge_findings, score_findings

        high = self._f("high", ["a"], ["S1"], source="preflight")
        low = self._f("medium", ["b"], ["S2"], source="export_check")
        merged = merge_findings([high], [low])
        only_high = merge_findings([high])
        assert score_findings(merged).total == score_findings(only_high).total
        assert score_findings(merged).grade == score_findings(only_high).grade

    def test_single_source_unchanged(self):
        from app.services.audit_scoring import merge_findings

        one = self._f("high", ["唯一证据"], ["S1"], source="preflight")
        out = merge_findings([one])
        assert out[0]["evidence"] == ["唯一证据"]
        assert "sources" not in out[0], "单来源不得凭空新增 sources 字段"
