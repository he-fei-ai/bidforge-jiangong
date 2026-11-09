"""R207 · 审核与预检模块深度加固护栏（2026-11-09）。

日志驱动证据：logs/backend.log 2026-11-09 21:15:35 导出 409 明细出现两条同名
「正文均已通过审核（DLV-09）」；21:23:32 合规检查分 4 批送审；/defects 等 404
经核实为开关默认关闭（前端已显式降级），不属缺陷。

护栏三层：① 行为实证（缓存水位/漂移守卫/聚合 section_ids/命中数/子类标题）；
② 源码棘轮（SSE 收尾补链、提交权、三态接线、归一出口）；③ 跨端 parity + 字节体检。
"""

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
from pathlib import Path

import pytest
from app.routers import compliance as comp
from app.routers import export as ex
from app.routers import review_autofix as ra
from app.services import repair_agent
from app.services.audit_scoring import normalize_severity_text, score_findings

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "backend" / "app"
FRONTEND = ROOT / "frontend" / "src"


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _async_fetch_one(conn: sqlite3.Connection):
    async def _fetch(db, sql, params=(), *, what=""):  # noqa: ANN001, ARG001
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row is not None else None

    return _fetch


# ---------------------------------------------------------------------------
# A · 总检缓存水位（P1：跑完 AI 检查 TTL 内仍返回旧放行结论）
# ---------------------------------------------------------------------------

@pytest.fixture
def src_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for ddl in (
        "CREATE TABLE compliance_check (id TEXT, scheme_id TEXT)",
        "CREATE TABLE consistency_audit (id TEXT, scheme_id TEXT)",
        "CREATE TABLE consistency_conflicts (id TEXT, scheme_id TEXT, status TEXT)",
        "CREATE TABLE consistency_repairs (id TEXT, scheme_id TEXT)",
        "CREATE TABLE scheme_scoring_items (id TEXT)",
        "CREATE TABLE bid_analysis_items (id TEXT)",
        "CREATE TABLE global_facts (id TEXT)",
    ):
        conn.execute(ddl)
    conn.commit()
    yield conn
    conn.close()


@pytest.mark.asyncio
async def test_a1_sources_signature_tracks_all_source_writes(monkeypatch, src_conn):
    monkeypatch.setattr(comp.review_db, "fetch_one", _async_fetch_one(src_conn))
    base = await comp._sources_signature(None, "s1")
    assert "compliance_check=" in base and "consistency_conflicts=" in base

    src_conn.execute("INSERT INTO compliance_check VALUES ('c1','s1')")
    src_conn.commit()
    assert await comp._sources_signature(None, "s1") != base, "AI 合规结果写入必须让总检缓存失效"

    src_conn.execute("INSERT INTO consistency_conflicts VALUES ('x1','s1','pending')")
    src_conn.commit()
    with_pending = await comp._sources_signature(None, "s1")
    src_conn.execute("UPDATE consistency_conflicts SET status='accepted' WHERE id='x1'")
    src_conn.commit()
    assert await comp._sources_signature(None, "s1") != with_pending, "冲突状态 UPDATE 也必须被感知"

    src_conn.execute("INSERT INTO global_facts VALUES ('f1')")
    src_conn.commit()
    assert await comp._sources_signature(None, "s1") != with_pending


@pytest.mark.asyncio
async def test_a2_sources_signature_fail_soft_on_missing_table(monkeypatch, src_conn):
    src_conn.execute("DROP TABLE bid_analysis_items")
    src_conn.commit()
    monkeypatch.setattr(comp.review_db, "fetch_one", _async_fetch_one(src_conn))
    sig = await comp._sources_signature(None, "s1")
    assert "bid_analysis_items=?" in sig, "缺表不得抛异常，也不得假装有水位"
    assert "compliance_check=" in sig, "单路失败不得影响其它路"


@pytest.mark.asyncio
async def test_a3_overview_cache_recomputes_when_source_watermark_changes(monkeypatch):
    calls: list[str] = []
    watermark = {"v": "w1"}

    async def _fp(db, sid):  # noqa: ANN001, ARG001
        return "fp"

    async def _rh(db, sid):  # noqa: ANN001, ARG001
        return "rh"

    async def _facts(db, sid):  # noqa: ANN001, ARG001
        return "facts"

    async def _sources(db, sid):  # noqa: ANN001, ARG001
        return watermark["v"]

    async def _compute(db, sid, *, fingerprint=None, review_hash=None):  # noqa: ANN001, ARG001
        calls.append(sid)
        return {"total": 90, "released": True}

    monkeypatch.setattr(comp, "_content_fingerprint", _fp)
    monkeypatch.setattr(comp, "_review_state_hash", _rh)
    monkeypatch.setattr(comp, "_facts_signature", _facts)
    monkeypatch.setattr(comp, "_sources_signature", _sources)
    monkeypatch.setattr(comp, "_readiness_overview_compute", _compute)
    comp._OVERVIEW_RECENT.pop("r207-s1", None)
    try:
        first = await comp.readiness_overview("r207-s1", db=object())
        again = await comp.readiness_overview("r207-s1", db=object())
        assert first["cached"] is False and again["cached"] is True
        watermark["v"] = "w2"  # 等价于刚跑完 /check 或 /consistency-audit
        third = await comp.readiness_overview("r207-s1", db=object())
        assert third["cached"] is False, "来源写入水位变化后 TTL 内不得返回旧放行结论"
        assert len(calls) == 2
    finally:
        comp._OVERVIEW_RECENT.pop("r207-s1", None)


def test_a4_overview_and_preflight_both_use_watermark():
    src = _src("backend/app/routers/compliance.py")
    assert src.count("_sources_signature(db, scheme_id)") == 2, "/preflight 与 /overview 必须同时接线"
    assert "preflight_runs" not in src.split("_SOURCE_SIGNATURE_QUERIES")[1].split("async def _sources_signature")[0], (
        "总检自身写 preflight_runs，纳入水位会让缓存永不命中"
    )


@pytest.mark.asyncio
async def test_a5_preflight_summary_stale_on_review_state_change():
    row = {"content_fingerprint": "fp", "review_state_hash": "old"}
    assert await comp._run_is_stale(None, "s1", row, current_fingerprint="fp", current_review_hash="new") is True
    assert await comp._run_is_stale(None, "s1", {"content_fingerprint": "fp"}, current_fingerprint="fp") is False
    select_src = _src("backend/app/routers/export.py")
    assert "content_fingerprint, review_state_hash, created_at" in select_src, (
        "导出页摘要 SELECT 缺 review_state_hash 会让 stale 判定恒 False（与 /runs 分叉）"
    )


# ---------------------------------------------------------------------------
# B · 导出预检 ↔ 总检聚合（日志实证的 DLV-09 重复标题 / section_ids 恒空）
# ---------------------------------------------------------------------------

def test_b1_export_findings_expand_aggregate_section_ids_and_real_hit_count():
    findings = ex.export_issues_to_findings(
        [
            {"type": "review_pending", "count": 20, "section_ids": ["a", "b"]},
            {"type": "review_rejected", "count": 4, "section_ids": ["c"]},
        ]
    )
    assert len(findings) == 1
    f = findings[0]
    assert f["rule_id"] == "DLV-09"
    assert set(f["section_ids"]) == {"a", "b", "c"}, "R198 补的 section_ids 不得在下游映射恒空"
    assert f["section_id"] == "a"
    assert f["hit_count"] == 24 and f["count"] == 2, "命中数与聚合条数必须区分"
    stats = sum(int(x.get("hit_count") or x.get("count") or 0) for x in findings)
    assert stats == 24


def test_b2_high_issue_blockers_distinguish_same_rule_subtypes():
    issues = [
        {"type": "review_rejected", "count": 6},
        {"type": "review_missing", "count": 4},
    ]
    blockers = ex._export_high_issue_blockers(issues)
    assert len(blockers) == 2
    titles = {b.get("display_title") for b in blockers}
    assert any("章节审核未通过" in (t or "") for t in titles)
    assert any("缺少章节审核记录" in (t or "") for t in titles)
    resp = ex._export_blocked_response(blockers)
    detail = json.loads(resp.body.decode("utf-8"))["detail"]
    assert "章节审核未通过" in detail and "缺少章节审核记录" in detail
    assert detail.count("正文均已通过审核（DLV-09）") == 0, "不得再出现两条同名明细"


def test_b3_subtype_labels_parity_with_frontend():
    front = _src("frontend/src/pages/schemeWorkbench/exportShared.ts")
    label_block = front.split("export const EXPORT_ISSUE_LABEL")[1].split("};")[0]
    for key, label in ex._EXPORT_ISSUE_SUBTYPE_LABEL.items():
        assert f'{key}: "{label}"' in label_block, f"后端子类标签 {key} 与前端 EXPORT_ISSUE_LABEL 分叉"
    assert set(ex._EXPORT_ISSUE_SUBTYPE_LABEL) <= set(ex._EXPORT_ISSUE_RULE_MAP)


def test_b4_three_state_and_severity_wiring():
    src = _src("backend/app/routers/compliance.py")
    assert "crossref_check" in src and "_exp_undetermined" in src, "crossref 无法判定必须进 partial_sources"
    assert src.count("normalize_severity_text(") >= 4, "一致性审计/扫描两处严重度必须复用单一归一出口"
    assert normalize_severity_text("阻断") == "block" and normalize_severity_text("高") == "high"
    assert src.count('failed_sources.append("expert_review")') >= 1
    assert src.count('failed_sources.append("consistency")') >= 1
    assert "score_findings(findings, partial_sources=_preflight_partial)" in src
    assert "hit_count" in src, "总检导出预检命中数必须用真实命中数"


def test_b5_engine_error_forces_incomplete():
    result = score_findings(
        [{"rule_id": "X", "dimension": "deliverability", "severity": "low", "engine_error": True}],
        partial_sources=["program"],
    )
    assert result.incomplete is True and result.released is False


# ---------------------------------------------------------------------------
# C · 修复链写点冲突（漂移守卫 / 提交权 / SSE 收尾补链）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c1_persist_fixed_skips_drifted_section(monkeypatch):
    writes: list[str] = []
    resets: list[str] = []

    async def _fake_fetch(db, sql, params=(), *, what=""):  # noqa: ANN001, ARG001
        if "SELECT content FROM sections" in sql:
            return {"content": "USER_EDITED"}  # 修复期间用户手工编辑
        return {"word_budget": 100}

    async def _snapshot(db, sid, sections, **kwargs):  # noqa: ANN001, ARG001
        return "snap-r207"

    async def _write(db, sql, params=(), **kwargs):  # noqa: ANN001, ARG001
        writes.append(sql)
        return 1

    async def _reset(db, sid, section_id, **kwargs):  # noqa: ANN001, ARG001
        resets.append(section_id)
        return True

    async def _noop(*args, **kwargs):  # noqa: ANN001, ARG001
        return None

    monkeypatch.setattr(ra.review_db, "fetch_one", _fake_fetch)
    monkeypatch.setattr(ra.review_db, "exec_write", _write)
    # create_snapshot 在 _persist_fixed 内局部导入（services 层），patch 模块本身
    monkeypatch.setattr("app.services.repair_record.create_snapshot", _snapshot)
    monkeypatch.setattr(ra, "_guard_not_generating", lambda *a, **k: None)
    monkeypatch.setattr(ra, "reset_review_on_content_change", _reset)

    skipped: list[dict] = []
    pending = [("sec-r207", "ORIGINAL", "AI_REWRITE")]

    class _DB:
        async def commit(self):
            return None

    snap = await ra._persist_fixed(
        _DB(), scheme_id="s1", rule_id="R207", pending=pending, skipped_out=skipped
    )
    assert snap == "snap-r207"
    assert len(skipped) == 1 and skipped[0]["status"] == "content_changed"
    assert not any("UPDATE sections SET content=?" in s for s in writes), "漂移章节绝不能被整章覆盖"
    assert resets == [], "未写入的章节不得伪造「审核结论失效」留痕"


@pytest.mark.asyncio
async def test_c2_persist_fixed_unreadable_row_keeps_legacy_behavior(monkeypatch):
    """读不到/列形态异常 = 无法判定，保持旧行为（兼容历史批次与内存库桩）。"""

    async def _fake_fetch(db, sql, params=(), *, what=""):  # noqa: ANN001, ARG001
        if "SELECT content FROM sections" in sql:
            return None
        return {"word_budget": 100}

    async def _snapshot(db, sid, sections, **kwargs):  # noqa: ANN001, ARG001
        return "snap"

    async def _write(db, sql, params=(), **kwargs):  # noqa: ANN001, ARG001
        return 1

    async def _noop(*args, **kwargs):  # noqa: ANN001, ARG001
        return None

    monkeypatch.setattr(ra.review_db, "fetch_one", _fake_fetch)
    monkeypatch.setattr(ra.review_db, "exec_write", _write)
    # create_snapshot 在 _persist_fixed 内局部导入（services 层），patch 模块本身
    monkeypatch.setattr("app.services.repair_record.create_snapshot", _snapshot)
    monkeypatch.setattr(ra, "_guard_not_generating", lambda *a, **k: None)
    monkeypatch.setattr(ra, "reset_review_on_content_change", _noop)
    monkeypatch.setattr("app.routers._chart_pipeline.resync_inline_charts", _noop)
    monkeypatch.setattr("app.routers.sections.invalidate_consistency_scan_cache", _noop)
    monkeypatch.setattr(ra, "normalize_section_content_subheadings", _noop)

    skipped: list[dict] = []

    class _DB:
        async def commit(self):
            return None

    await ra._persist_fixed(
        _DB(), scheme_id="s1", rule_id="R207", pending=[("s", "ORIGINAL", "AI")], skipped_out=skipped
    )
    assert skipped == []


def test_c3_drift_guard_is_at_write_point_not_endpoint():
    src = inspect.getsource(ra._persist_fixed)
    assert "SELECT content FROM sections" in src
    assert src.index("SELECT content FROM sections") < src.index("UPDATE sections SET content=?")
    assert 'status": "content_changed"' in src or "content_changed" in src
    apply_src = inspect.getsource(ra.apply)
    assert "skipped_out=_drift_skipped" in apply_src, "/apply 必须透出跳过明细"
    confirm_src = inspect.getsource(ra.confirm)
    assert "_written = len(pending) - len(_drift_skipped)" in confirm_src
    assert '"repaired_sections": _written' in confirm_src, "全跳过不得谎报已修复章数"


def test_c4_repair_agent_commit_right_is_single_point():
    src = inspect.getsource(repair_agent.run_repair)
    assert "await db.commit()" not in src, "逐章修复不得各自提交（半生效态）"
    assert "save_repair(" in src, "提交权唯一归 save_repair"
    tree = ast.parse(src)
    assert any(isinstance(n, ast.AsyncFunctionDef) and n.name == "_repair_one" for n in ast.walk(tree))


def test_c5_sse_consistency_pipeline_complementary_wiring():
    src = _src("backend/app/routers/sse_handlers.py")
    block = src[src.index("_run_consistency_pipeline"): src.index("cons_runner = asyncio.create_task")]
    assert block.count("resync_fn=_chart_resync") == 2, "逐章/整卷两条分支都要重登记图表"
    assert "_make_chart_resync(scheme_id)" in block
    assert "reset_review_on_content_change(" in block, "收尾改正文必须退回审核并留痕"
    assert "invalidate_consistency_scan_cache(db, scheme_id)" in block


# ---------------------------------------------------------------------------
# D · 跨端 parity + 字节体检
# ---------------------------------------------------------------------------

def test_d1_source_label_numbering_parity():
    front = _src("frontend/src/components/review/ReadinessDashboard.tsx")
    assert 'numbering: "编号一致性"' in front
    back = _src("backend/app/routers/compliance.py")
    assert 'sources.append("numbering")' in back and 'failed_sources.append("numbering")' in back


def test_d2_frontend_wiring_ratchet():
    hook = _src("frontend/src/pages/schemeWorkbench/useReviewAudit.tsx")
    assert hook.count('msg.info("已取消当前检查")') == 3, "三个预检取消入口都要即时反馈"
    assert "restoreDetails" in hook and "detailNotRestored" in hook
    assert "crRollbackInFlightRef" in hook and "schemeIdRef.current !== sid" in hook
    assert "data.partial === true" in hook and "一致性审计覆盖不完整" in hook
    check = _src("frontend/src/pages/schemeWorkbench/useExportCheck.ts")
    assert "qualityInFlightRef" in check and "}, [id]);" in check
    assert "setCheckingQuality(false)" in check.split("} finally {")[1]
    page = _src("frontend/src/pages/SchemeWorkbenchPage.tsx")
    assert page.count("consistencyLatestAbortRef") >= 4
    api = _src("frontend/src/api/index.ts")
    assert "consistencyLatest: (schemeId: string, signal?: AbortSignal)" in api


@pytest.mark.parametrize(
    "rel",
    [
        "backend/app/routers/compliance.py",
        "backend/app/routers/export.py",
        "backend/app/routers/review_autofix.py",
        "backend/app/routers/sse_handlers.py",
        "backend/app/services/repair_agent.py",
    ],
)
def test_d3_backend_files_keep_crlf_and_clean_utf8(rel: str):
    raw = (ROOT / rel).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel}: BOM"
    assert b"\xff\xfd" not in raw and b"\xef\xbf\xbd" not in raw, f"{rel}: U+FFFD"
    assert raw.count(b"\r\n") > 100, f"{rel}: 行尾被整体 LF 化"
    assert raw.replace(b"\r\n", b"").count(b"\n") == 0, f"{rel}: 混合行尾"
