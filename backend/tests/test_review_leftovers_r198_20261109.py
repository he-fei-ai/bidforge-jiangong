"""R198（2026-11-09）· 审核与预检模块遗留项收口护栏。

承接 R197「遗留问题与后续建议」的落地面（行为实证 + 源码锁）：
- T2  评分项覆盖并入就绪度总检（第七源，CMP-SC-1）：
      scoring.py::scoring_coverage_state 单一判据源 + compliance.py 接线；
      未启用评分项体系的方案总检结果逐字不变（向后兼容反例）；
- T3  聚合类导出预检 issue 补 section_ids（review 三态 + chart 两类）；
- T1' _export_issue_scan 与 export 初始化环已断（首触行为锁在 r86 文件内）；
- T5  三项 P3 加固：事实签名失败留痕 / 批次审计降级判据窄化 / 取值定位小数守卫。
"""

from __future__ import annotations

import uuid
from datetime import datetime

import app.db as _appdb
import pytest
from app.db import close_db, get_conn, init_db

# ✅ R198：asyncio 标记收在类级（模块级 pytestmark 会污染本文件末尾两个
#    纯函数测试，触发 PytestWarning 噪声）


@pytest.fixture
async def r198_db(tmp_path):
    _appdb.DB_PATH = tmp_path / "r198-leftovers.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status,word_budget)" " VALUES(?,?,?,?,0)",
        (sid, pid, "深基坑支护专项方案", "目录已确认"),
    )
    await db.commit()
    yield db, sid, pid
    await close_db()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


async def _add_section(db, sid, *, title="第一章 工程概况", content="", sort_order=0) -> str:
    sec_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
        " description, level, status, word_count, word_budget, content,"
        " review_status, sort_order)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            sec_id,
            sid,
            "",
            "",
            title,
            "",
            1,
            "generated" if content else "empty",
            len(content),
            0,
            content,
            "",
            sort_order,
        ),
    )
    return sec_id


async def _seed_scoring_run(db, pid: str, *, status: str = "success") -> None:
    await db.execute(
        "INSERT INTO scheme_scoring_extract_runs(id,project_id,status,docs_total,docs_ok,"
        "failed_sources,items_count,error,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, pid, status, 1, 1, "[]", 1, "", _now(), _now()),
    )


async def _seed_scoring_item(db, pid: str, name: str, score: str = "10") -> str:
    item_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO scheme_scoring_items(id,project_id,source_doc_id,source_name,item_no,"
        "item_name,score,score_raw,criteria,is_primary,page_ref,page_num,source_quote,status)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, pid, "", "招标文件", 1, name, float(score or 0), score, "", 1, "", None, "", "active"),
    )
    return item_id


# ===========================================================================
# T2：scoring_coverage_state（第七源单一判据源）
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.asyncio
class TestScoringCoverageState:
    async def test_no_extraction_run_is_not_applicable(self, r198_db):
        """未启用评分项体系（无提取运行记录）→ not_applicable（总检不得计分）。"""
        from app.routers.scoring import scoring_coverage_state

        db, sid, _pid = r198_db
        st = await scoring_coverage_state(db, sid)
        assert st["applicable"] is False
        assert st["status"] == "not_applicable"

    async def test_enabled_but_empty_items_is_undetermined(self, r198_db):
        from app.routers.scoring import scoring_coverage_state

        db, sid, pid = r198_db
        await _seed_scoring_run(db, pid)
        await db.commit()
        st = await scoring_coverage_state(db, sid)
        assert st["applicable"] is True
        assert st["status"] == "undetermined"
        assert st["undetermined_reason"] == "empty_items"

    async def test_missing_items_reported(self, r198_db):
        """体系启用且有评分项 → 返回覆盖明细（missing 项随 finding 送总检）。"""
        from app.routers.scoring import scoring_coverage_state

        db, sid, pid = r198_db
        await _seed_scoring_run(db, pid)
        await _seed_scoring_item(db, pid, "基坑监测方案完整性")
        await _add_section(db, sid, content="本工程采用地下连续墙支护， unrelated content。")
        await db.commit()
        st = await scoring_coverage_state(db, sid)
        assert st["applicable"] is True
        assert st["total_items"] == 1
        assert st["missing_count"] == 1, st
        assert "基坑监测方案完整性" in st["missing_names"][0]


# ===========================================================================
# T2：readiness 第七源接线
# ===========================================================================


@pytest.mark.asyncio
class TestReadinessSeventhSource:
    async def test_enabled_scheme_scores_with_coverage_finding(self, r198_db):
        """体系启用 + 有缺失项 → sources 出现 scoring_coverage + CMP-SC-1 finding。"""
        from app.routers.compliance import _readiness_overview_compute

        db, sid, pid = r198_db
        await _seed_scoring_run(db, pid)
        await _seed_scoring_item(db, pid, "基坑监测方案完整性")
        await _add_section(db, sid, content="本工程采用地下连续墙支护。")
        await db.commit()
        payload = await _readiness_overview_compute(db, sid, persist=False)
        assert "scoring_coverage" in payload["sources"], payload["sources"]
        sc_findings = [f for f in payload["findings"] if str(f.get("rule_id", "")).startswith("CMP-SC")]
        assert sc_findings, "缺失评分项必须产出 finding"
        assert sc_findings[0]["dimension"] == "completeness"
        assert "基坑监测方案完整性" in (sc_findings[0].get("detail") or "")

    async def test_disabled_scheme_unchanged(self, r198_db):
        """反例（向后兼容）：未启用评分项体系 → 不进 sources、无 CMP-SC finding。"""
        from app.routers.compliance import _readiness_overview_compute

        db, sid, _pid = r198_db
        await _add_section(db, sid, content="正文")
        await db.commit()
        payload = await _readiness_overview_compute(db, sid, persist=False)
        assert "scoring_coverage" not in payload["sources"], "未启用体系不得计入来源"
        assert not [f for f in payload["findings"] if str(f.get("rule_id", "")).startswith("CMP-SC")]

    async def test_enabled_but_undetermined_counts_partial(self, r198_db):
        """体系启用但提取失败 → partial_sources（released 不得为 True）。"""
        from app.routers.compliance import _readiness_overview_compute

        db, sid, pid = r198_db
        await _seed_scoring_run(db, pid, status="failed")
        await _add_section(db, sid, content="正文")
        await db.commit()
        payload = await _readiness_overview_compute(db, sid, persist=False)
        assert "scoring_coverage" in payload["partial_sources"], payload.get("partial_sources")


# ===========================================================================
# T3：聚合类导出预检 issue 补 section_ids
# ===========================================================================


@pytest.mark.asyncio
class TestAggregatedIssueSectionIds:
    async def test_review_and_chart_issues_carry_section_ids(self, r198_db):
        from app.routers.export import collect_export_issues

        db, sid, _pid = r198_db
        sec_a = await _add_section(db, sid, title="第一章", content="正文A", sort_order=1)
        sec_b = await _add_section(db, sid, title="第二章", content="正文B", sort_order=2)
        await db.execute(
            "UPDATE sections SET review_status='pending' WHERE id=?", (sec_a,)
        )
        await db.execute(
            "UPDATE sections SET review_status='rejected' WHERE id=?", (sec_b,)
        )
        await db.execute(
            "INSERT INTO chart_predictions(id,section_id,scheme_id,chart_type,needed,purpose,"
            "priority,status,data_json) VALUES(?,?,?,?,1,'',0,'failed','{}')",
            (uuid.uuid4().hex, sec_a, sid, "flowchart"),
        )
        await db.commit()
        out = await collect_export_issues(sid, db)
        by_type: dict[str, list] = {}
        for it in out.get("issues") or []:
            by_type.setdefault(it.get("type") or "", []).append(it)
        assert by_type.get("review_pending"), "场景必须产生 review_pending"
        assert by_type["review_pending"][0]["section_ids"] == [sec_a]
        assert by_type.get("review_rejected"), "场景必须产生 review_rejected"
        assert by_type["review_rejected"][0]["section_ids"] == [sec_b]
        assert by_type.get("chart_failed"), "场景必须产生 chart_failed"
        assert by_type["chart_failed"][0]["section_ids"] == [sec_a]


# ===========================================================================
# T5-1/2：事实签名留痕 + 批次审计降级判据窄化
# ===========================================================================


@pytest.mark.asyncio
class TestHardeningDetails:
    async def test_facts_signature_failure_logs_warning(self, r198_db, caplog):
        from unittest.mock import patch

        from app.routers import compliance as cc
        from app.services import review_db as rdb

        db, sid, _pid = r198_db

        async def _boom(*a, **k):
            raise RuntimeError("db closed")

        with patch.object(rdb, "fetch_one", _boom):
            sig = await cc._facts_signature(db, sid)
        assert sig == "", "失败降级为空签名（不阻断预检）"
        assert any("事实签名计算失败" in r.message for r in caplog.records), "必须留痕"

    async def test_batch_audit_downgrade_only_on_missing_column(self, r198_db, monkeypatch, caplog):
        """✅ R198：仅 sqlite 缺列才降级；其它异常不得静默换 INSERT 形态。"""
        import sqlite3

        from app.routers import compliance as cc
        from app.services import review_db as rdb

        db, _sid, _pid = r198_db
        calls: list[str] = []

        async def _fake_exec_write(db_, sql, params=(), *, what="", **kw):
            calls.append(sql)
            if len(calls) == 1:
                raise sqlite3.OperationalError("table ai_audit_logs has no column named error_class")
            return 1

        async def _explode(db_, sql, params=(), *, what="", **kw):
            calls.append(sql)
            raise ValueError("disk I/O error")

        monkeypatch.setattr(rdb, "exec_write", _fake_exec_write)
        await cc._record_batch_audit(db, index=1, total=1, chars=10, duration=0.1)
        assert len(calls) >= 2, "缺列必须降级写下一级 INSERT"

        calls.clear()
        monkeypatch.setattr(rdb, "exec_write", _explode)
        await cc._record_batch_audit(db, index=1, total=1, chars=10, duration=0.1)
        assert len(calls) == 1, "非缺列错误不得降级（旧实现按 'column' 子串误判）"
        assert any("批次审计写入失败" in r.message for r in caplog.records)


# ===========================================================================
# T5-3：取值定位小数守卫
# ===========================================================================


def test_value_pattern_no_decimal_false_positive():
    from app.services.review_autofix import _locate_value

    finding = {"evidence": ["90天（施工进度计划）"]}
    content = "总工期为 90.5 天（含雨天），另述 90 天为净工期。"
    targets = _locate_value(finding, [{"id": "s1", "title": "施工进度计划", "content": content}])
    hits = [t for t in targets]
    assert hits, "纯 90 天 必须被定位"
    for t in hits:
        assert not (t.get("text") or "").startswith("90."), f"小数 90.5 不得被 evidence=90天 命中：{t}"


def test_value_pattern_decimal_evidence_still_anchors():
    from app.services.review_autofix import _value_pattern

    rx = _value_pattern("120.5米")
    assert rx is not None
    assert rx.search("桩长 120.5米"), "evidence 本身含小数必须仍可定位"
    assert not rx.search("桩长 120米"), "小数 evidence 不得命中整数形态"
