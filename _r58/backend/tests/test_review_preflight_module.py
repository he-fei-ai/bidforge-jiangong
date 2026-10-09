"""审核与预检模块（review.py / compliance.py / preflight_engine.py /
audit_scoring.py / audit_rules.py）的回归锁测试。

覆盖的修复点（2026-09-21 深度审查）：

- review.py
    * batch_review_sections：补 scheme 存在性校验 / 截断告警 / skipped + not_found
    * submit_scheme_review：新增 section_review_check（软校验，向后兼容）
    * review_summary：新增 reviewed_sections / reviewed_progress
- compliance.py
    * compliance_check：补 scheme 404 / 单章节 c[:4000] 截断标记
    * expert_review：补 scheme 404 / outline + 附件上限
    * get_results：补 limit/offset 分页 + total
    * readiness_overview：批次查询改用 rowid 锚定（不再误聚合秒级时间戳）
    * _persist_run：except 记录日志 + rollback（不再静默 pass）
    * run_consistency_audit：单章节 3000 / 总长 50000 截断标记
- preflight_engine.py
    * CON-05 → CON-05-N 编号（修复 merge_findings 塌缩）
- audit_scoring.py
    * released 以 grade 为准（阻断项下不会出现 grade=C + released=True 矛盾）
    * unknown_dimension_count 新增字段
- audit_rules.py
    * RULE_VERSION 升至 1.1.0
- schema_sql.py
    * consistency_audit 注释修正（不再是"业务读写未实现"）
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

import app.db as _appdb
import pytest
from app.db import close_db, get_conn, init_db
from app.models import (
    ComplianceCheckIn,
    ExpertReviewIn,
    SchemeReviewIn,
    SectionReviewIn,
)
from app.routers.compliance import (
    _persist_run,
    compliance_check,
    expert_review,
    get_results,
    readiness_overview,
    run_consistency_audit,
)
from app.routers.review import (
    _ALLOWED_TRANSITIONS,
    batch_review_sections,
    review_summary,
    submit_scheme_review,
)
from app.services.audit_rules import RULE_VERSION, get_rule, rule_catalog
from app.services.audit_scoring import score_findings
from app.services.preflight_engine import PreflightContext, run_preflight
from fastapi import HTTPException


@pytest.fixture
async def db_ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "review-preflight.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    sid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status) VALUES(?,?,?,?)",
        (sid, pid, "s", "目录已确认"))
    await db.commit()
    yield db, pid, sid
    # ✅ 关闭全局连接：aiosqlite worker 为非守护线程，不关闭会阻塞解释器退出
    #   （表现为 pytest 全绿后进程挂起）。
    await close_db()


async def _insert_section(db, sid, title="第一章", content="", level=1,
                          parent_id="", word_count=0, review_status="",
                          sort_order=0, status="empty"):
    sec_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
        " description, level, status, word_count, word_budget, content,"
        " review_status, sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sec_id, sid, "", parent_id, title, "", level, status, word_count,
         0, content, review_status, sort_order))
    return sec_id


async def _insert_compliance_check(db, sid, check_type="compliance",
                                   result_obj=None, rule_id="",
                                   created_at=None):
    # ✅ BUG 修复（2026-09-21）：旧实现 `return db.execute(...)` —— async 函数里
    #    直接 return 一个协程对象不会被 await，INSERT 永远不执行（只产生
    #    "coroutine was never awaited" 警告）。下游断言 total==0 / ai_findings
    #    为空，看起来像产品缺陷，实际是夹具自身的假阴性。现改为 await。
    return await db.execute(
        "INSERT INTO compliance_check (id, scheme_id, check_type, rule_id, item,"
        " severity, result, suggestion, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, check_type, rule_id, "", "",
         json.dumps(result_obj or {}, ensure_ascii=False), "",
         created_at or datetime.now().isoformat(timespec="seconds")))


def _mk_section(title, content, word_count=None, status="generated",
                parent_id="", sid=None):
    return {
        "id": sid or uuid.uuid4().hex,
        "title": title,
        "content": content,
        "word_count": word_count if word_count is not None else len(content),
        "level": 1,
        "parent_id": parent_id,
        "status": status,
    }


# ===========================================================================
# 一、review.py —— 状态机 & 批量 & summary & submit
# ===========================================================================
def test_allowed_transitions_matrix_is_symmetric_and_complete():
    """状态机合法性矩阵：五个来源（含 '' 未纳入审核）都应有明确的目标集合。"""
    assert set(_ALLOWED_TRANSITIONS.keys()) == {"", "pending", "reviewing",
                                                 "approved", "rejected"}
    assert set(_ALLOWED_TRANSITIONS[""]) == {"pending", "reviewing",
                                              "approved", "rejected"}
    for _s in ("pending", "reviewing", "approved", "rejected"):
        assert len(_ALLOWED_TRANSITIONS[_s]) >= 3
    # 四个非空状态都应允许自流转（前端「再次通过」/「重置为待审核」按钮需要）
    for _s in ("pending", "reviewing", "approved", "rejected"):
        assert _s in _ALLOWED_TRANSITIONS[_s]
    # 首次审核（""）不允许自流转（无意义操作）
    assert "" not in _ALLOWED_TRANSITIONS[""]


async def test_review_section_updates_review_status_only(db_ctx):
    """章节审核只写 sections.review_status，不触碰 compile status。"""
    from app.routers.review import review_section
    db, _pid, sid = db_ctx
    sec_id = await _insert_section(db, sid, content="正文", word_count=100,
                                   status="generated")
    await db.commit()
    res = await review_section(sid, sec_id,
                               SectionReviewIn(to_status="approved",
                                               reviewer="张工",
                                               comment="已审阅"), db)
    assert res["ok"] is True
    cur = await db.execute("SELECT status, review_status FROM sections WHERE id=?",
                           (sec_id,))
    row = await cur.fetchone()
    assert row["status"] == "generated"
    assert row["review_status"] == "approved"


async def test_review_section_rejects_illegal_status(db_ctx):
    from app.routers.review import review_section
    db, _pid, sid = db_ctx
    sec_id = await _insert_section(db, sid, content="正文")
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await review_section(sid, sec_id,
                             SectionReviewIn(to_status="unknown", reviewer=""), db)
    assert exc.value.status_code == 400


async def test_review_section_not_found_returns_404(db_ctx):
    from app.routers.review import review_section
    db, _pid, sid = db_ctx
    with pytest.raises(HTTPException) as exc:
        await review_section(sid, "nonexistent",
                             SectionReviewIn(to_status="approved"), db)
    assert exc.value.status_code == 404


async def test_batch_review_sections_reports_skipped_and_not_found(db_ctx):
    """BUG 修复：批量审核返回 skipped / not_found / nochange 三类明细。"""
    db, _pid, sid = db_ctx
    s_already = await _insert_section(db, sid, title="A", review_status="approved")
    s_ok = await _insert_section(db, sid, title="B", review_status="rejected")
    # 脏数据回归：库里存在状态机不认识的历史值（如早期版本写入的 "draft"），
    # 必须被 skipped 明确报出，而不是静默跳过或抛异常。
    s_dirty = await _insert_section(db, sid, title="C", review_status="draft")
    await db.commit()
    res = await batch_review_sections(
        sid, {"section_ids": [s_already, s_ok, s_dirty, "nonexistent"],
              "to_status": "approved", "reviewer": "张工", "comment": ""}, db)
    assert res["ok"] is True
    assert res["changed"] == 1
    assert s_ok in res["changed_ids"]
    assert res["nochange"] == [s_already]
    assert s_dirty in res["skipped"]
    assert "nonexistent" in res["not_found"]
    assert res["total_ids"] == 4
    assert res["truncated"] is False
    # skipped 不计入 changed，但必须能被前端区分出来提示
    assert res["changed"] == len(res["changed_ids"]) == 1
    await db.commit()


async def test_batch_review_sections_404_on_missing_scheme(db_ctx):
    """BUG 修复：旧实现静默返回 changed=0；现补 404。"""
    db, _pid, _sid = db_ctx
    with pytest.raises(HTTPException) as exc:
        await batch_review_sections(
            "nonexistent-scheme",
            {"section_ids": ["a"], "to_status": "approved", "reviewer": "",
             "comment": ""}, db)
    assert exc.value.status_code == 404


async def test_batch_review_sections_truncates_with_flag(db_ctx):
    """BUG 修复：ids[:500] 静默截断改为显式 truncated=true + total_ids。"""
    db, _pid, sid = db_ctx
    ids = [uuid.uuid4().hex for _ in range(501)]
    res = await batch_review_sections(
        sid, {"section_ids": ids, "to_status": "approved", "reviewer": "张工",
              "comment": ""}, db)
    assert res["total_ids"] == 501
    assert res["truncated"] is True
    assert res["changed"] == 0


async def test_batch_review_sections_force_bypasses_state_machine(db_ctx):
    """force=True 时状态机不拦截，可用于「全部重置为待审核」。"""
    db, _pid, sid = db_ctx
    s1 = await _insert_section(db, sid, title="A", review_status="approved")
    await db.commit()
    res = await batch_review_sections(
        sid, {"section_ids": [s1], "to_status": "pending", "reviewer": "张工",
              "comment": "", "force": True}, db)
    assert res["changed"] == 1
    cur = await db.execute("SELECT review_status FROM sections WHERE id=?", (s1,))
    assert (await cur.fetchone())["review_status"] == "pending"


async def test_batch_review_sections_empty_ids_raises_400(db_ctx):
    db, _pid, sid = db_ctx
    with pytest.raises(HTTPException) as exc:
        await batch_review_sections(sid, {"section_ids": [], "to_status": "approved"}, db)
    assert exc.value.status_code == 400


async def test_review_summary_counts_all_four_states(db_ctx):
    """summary 分桶：未审 / 待审 / 通过 / 驳回四态独立计数。"""
    db, _pid, sid = db_ctx
    await _insert_section(db, sid, title="A", review_status="")
    await _insert_section(db, sid, title="B", review_status="pending")
    await _insert_section(db, sid, title="C", review_status="approved")
    await _insert_section(db, sid, title="D", review_status="rejected")
    await db.commit()
    summary = await review_summary(sid, db)
    assert summary["total_sections"] == 4
    assert summary["counts"]["approved"] == 1
    assert summary["counts"]["rejected"] == 1
    assert summary["counts"]["pending"] == 1
    assert summary["counts"][""] == 1
    assert summary["progress"] == 25.0
    assert summary["reviewed_sections"] == 3
    assert summary["reviewed_progress"] == 75.0
    assert summary["approved_progress"] == 25.0
    assert summary["reviewed_all"] is False


async def test_review_summary_reviewed_all_only_when_all_have_status(db_ctx):
    """reviewed_all 语义：所有非空 status 章节才 True（含 approved/rejected）。"""
    db, _pid, sid = db_ctx
    await _insert_section(db, sid, title="A", review_status="approved")
    await _insert_section(db, sid, title="B", review_status="rejected")
    await db.commit()
    summary = await review_summary(sid, db)
    assert summary["reviewed_all"] is True
    assert summary["approved_progress"] == 50.0


async def test_review_summary_404_on_missing_scheme(db_ctx):
    db, _pid, _sid = db_ctx
    with pytest.raises(HTTPException) as exc:
        await review_summary("nonexistent", db)
    assert exc.value.status_code == 404


async def _seed_fresh_preflight(db, sid, blocked=0, total=90.0, grade="A",
                                verdict="可直接交付", section_updated_at=None,
                                released=None, content_fingerprint="",
                                created_at=None):
    """塞一条「最新一次总检已通过且晚于正文变更」的记录，绕开 submit 的两道门控。

    ``section_updated_at`` 晚于预检时间时，可复现「正文变更后未重跑总检」的
    过期拦截分支（BUG#1 回归锁需要）。
    """
    # ✅ BUG 修复（2026-09-21）：旧实现 `return db.execute(...)` —— async 函数里
    #    直接 return 协程对象不会被 await，INSERT 根本不执行（只剩
    #    "coroutine was never awaited" 警告）。于是 submit_scheme_review 每次都
    #    落到「尚未执行审核预检」的 422，而测试报的是断言失败，根因被彻底掩盖。
    await db.execute(
        "INSERT INTO preflight_runs (id, scheme_id, content_fingerprint, rule_version, total, grade,"
        " verdict, released, blocked, counts, dimensions, findings, stats, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, content_fingerprint, RULE_VERSION, total, grade, verdict,
         (not blocked) if released is None else released, blocked, "{}", "[]", "[]", "{}",
         created_at or datetime.now().isoformat(timespec="seconds")))
    if section_updated_at:
        await db.execute(
            "UPDATE sections SET updated_at=? WHERE scheme_id=?",
            (section_updated_at, sid))


async def test_submit_scheme_review_returns_section_review_check(db_ctx):
    """BUG 修复：方案级 /submit 现在会在返回体里给出章节审核完整性明细。"""
    db, _pid, sid = db_ctx
    await _insert_section(db, sid, title="A", content="正文", status="generated")
    await _seed_fresh_preflight(db, sid)
    await db.commit()
    res = await submit_scheme_review(
        sid, SchemeReviewIn(to_status="approved", reviewer="张工"), db)
    assert res["ok"] is True
    assert "section_review_check" in res
    assert res["section_review_check"]["unreviewed_count"] == 1


async def test_submit_scheme_review_force_all_reviewed_blocks_with_422(db_ctx):
    """require_all_sections_reviewed=True 时，未审章节触发 422。"""
    db, _pid, sid = db_ctx
    await _insert_section(db, sid, title="A", content="正文", status="generated")
    await _seed_fresh_preflight(db, sid)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await submit_scheme_review(
            sid, SchemeReviewIn(to_status="approved", reviewer="张工",
                                require_all_sections_reviewed=True), db)
    assert exc.value.status_code == 422
    assert "未纳入审核" in exc.value.detail
    # 回滚后方案状态不应被写入
    cur = await db.execute("SELECT review_status FROM schemes WHERE id=?", (sid,))
    assert (await cur.fetchone())["review_status"] == ""


async def test_submit_scheme_review_force_all_reviewed_ok_when_all_reviewed(db_ctx):
    """require_all_sections_reviewed=True 且所有章节已审 → 通过。"""
    db, _pid, sid = db_ctx
    await _insert_section(db, sid, title="A", content="正文", status="generated",
                          review_status="approved")
    await _seed_fresh_preflight(db, sid)
    await db.commit()
    res = await submit_scheme_review(
        sid, SchemeReviewIn(to_status="approved", reviewer="张工",
                            require_all_sections_reviewed=True), db)
    assert res["ok"] is True
    assert res["section_review_check"]["unreviewed_count"] == 0


@pytest.mark.parametrize("section_status", ["reviewing", "rejected"])
async def test_submit_scheme_review_require_all_approved_rejects_reviewed_sections(
        db_ctx, section_status):
    """已纳入审核但仍在审核中/已驳回，不等于已通过，严格门禁必须拒绝。"""
    db, _pid, sid = db_ctx
    await _insert_section(db, sid, title="A", content="正文", status="generated",
                          review_status=section_status)
    await _seed_fresh_preflight(db, sid)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await submit_scheme_review(
            sid, SchemeReviewIn(to_status="approved", reviewer="张工",
                                require_all_sections_reviewed=True), db)
    assert exc.value.status_code == 422
    assert "未通过" in exc.value.detail


async def test_submit_scheme_review_rejects_stale_content_fingerprint(db_ctx):
    """时间戳未变化但正文指纹已变化时，也不能用旧总检结果放行。"""
    from app.routers.compliance import _content_fingerprint

    db, _pid, sid = db_ctx
    sec = await _insert_section(db, sid, title="A", content="原正文", status="generated",
                                review_status="approved")
    await db.execute("UPDATE sections SET updated_at='2026-01-01T00:00:00' WHERE id=?", (sec,))
    await db.commit()
    stored_fp = await _content_fingerprint(db, sid)
    await _seed_fresh_preflight(
        db, sid, content_fingerprint=stored_fp,
        created_at="2026-01-02T00:00:00", section_updated_at="2026-01-01T00:00:00")
    # 只改内容与缓存字数，不改 updated_at：确保命中的必须是内容指纹门禁。
    await db.execute("UPDATE sections SET content=?, word_count=? WHERE id=?",
                     ("变更后的正文", 6, sec))
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await submit_scheme_review(
            sid, SchemeReviewIn(to_status="approved", reviewer="张工",
                                require_all_sections_reviewed=True), db)
    assert exc.value.status_code == 422
    assert "过期" in exc.value.detail


async def test_submit_scheme_review_rejects_fresh_but_not_released_preflight(db_ctx):
    """最新总检未达到放行线时，方案级 approved 必须被门禁拒绝。"""
    db, _pid, sid = db_ctx
    await _insert_section(db, sid, title="A", content="正文", status="generated",
                          review_status="approved")
    await _seed_fresh_preflight(
        db, sid, blocked=0, released=0, total=69.9, grade="C", verdict="需整改后重新预检")
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await submit_scheme_review(
            sid, SchemeReviewIn(to_status="approved", reviewer="张工",
                                require_all_sections_reviewed=True), db)
    assert exc.value.status_code == 422
    assert "未达到放行条件" in exc.value.detail


# ===========================================================================
# 二、compliance.py —— 端点契约
# ===========================================================================
async def test_compliance_check_returns_404_on_missing_scheme(db_ctx, monkeypatch):
    """BUG 修复：/check 无 scheme 时补 404，不再走一次空 AI 调用。"""
    db, _pid, _sid = db_ctx

    async def _fake_collect_json_response(*_a, **_k):
        return {"results": []}, None

    monkeypatch.setattr("app.routers.compliance.collect_json_response",
                        _fake_collect_json_response)
    with pytest.raises(HTTPException) as exc:
        await compliance_check(ComplianceCheckIn(scheme_id="nonexistent"), db)
    assert exc.value.status_code == 404


async def test_expert_review_returns_404_on_missing_scheme(db_ctx, monkeypatch):
    """BUG 修复：/expert-review 无 scheme 时补 404。"""
    db, _pid, _sid = db_ctx

    async def _fake_collect_json_response(*_a, **_k):
        return {"score": 80}, None

    monkeypatch.setattr("app.routers.compliance.collect_json_response",
                        _fake_collect_json_response)
    with pytest.raises(HTTPException) as exc:
        await expert_review(ExpertReviewIn(scheme_id="nonexistent"), db)
    assert exc.value.status_code == 404


async def test_expert_review_rejects_too_many_outline_nodes(db_ctx, monkeypatch):
    """BUG 修复：outline 节点超过 EXPERT_OUTLINE_MAX_NODES 明确 400。"""
    from app.routers.compliance import EXPERT_OUTLINE_MAX_NODES
    db, _pid, sid = db_ctx
    for _i in range(EXPERT_OUTLINE_MAX_NODES + 1):
        await _insert_section(db, sid, title=f"S{_i}")
    await db.commit()

    async def _fake_collect_json_response(*_a, **_k):
        return {"score": 80}, None

    monkeypatch.setattr("app.routers.compliance.collect_json_response",
                        _fake_collect_json_response)
    with pytest.raises(HTTPException) as exc:
        await expert_review(ExpertReviewIn(scheme_id=sid), db)
    assert exc.value.status_code == 400
    assert "超过上限" in exc.value.detail


async def test_expert_review_rejects_too_many_attachments(db_ctx, monkeypatch):
    """BUG 修复：附件数量超过上限返回 400。"""
    from app.routers.compliance import EXPERT_ATTACH_MAX_COUNT
    db, _pid, sid = db_ctx
    await db.commit()

    async def _fake_collect_json_response(*_a, **_k):
        return {"score": 80}, None

    monkeypatch.setattr("app.routers.compliance.collect_json_response",
                        _fake_collect_json_response)
    too_many = ["a"] * (EXPERT_ATTACH_MAX_COUNT + 1)
    with pytest.raises(HTTPException) as exc:
        await expert_review(ExpertReviewIn(scheme_id=sid, attachments=too_many), db)
    assert exc.value.status_code == 400


async def test_expert_review_rejects_long_attachment(db_ctx, monkeypatch):
    """BUG 修复：单条附件长度超过上限返回 400。"""
    from app.routers.compliance import EXPERT_ATTACH_MAX_LEN
    db, _pid, sid = db_ctx
    await db.commit()

    async def _fake_collect_json_response(*_a, **_k):
        return {"score": 80}, None

    monkeypatch.setattr("app.routers.compliance.collect_json_response",
                        _fake_collect_json_response)
    too_long = "x" * (EXPERT_ATTACH_MAX_LEN + 1)
    with pytest.raises(HTTPException) as exc:
        await expert_review(ExpertReviewIn(scheme_id=sid,
                                           attachments=[too_long]), db)
    assert exc.value.status_code == 400


async def test_get_results_pagination_returns_total_and_limit(db_ctx):
    """BUG 修复：/results 补 limit/offset 分页，返回 total。"""
    db, _pid, sid = db_ctx
    for _i in range(5):
        await _insert_compliance_check(db, sid, result_obj={"hit": False})
    await db.commit()

    res = await get_results(sid, db=db)  # 默认 limit=50
    assert res["total"] == 5
    assert res["limit"] == 50
    assert res["offset"] == 0
    assert len(res["items"]) == 5

    res = await get_results(sid, limit=2, offset=1, db=db)
    assert res["total"] == 5
    assert len(res["items"]) == 2
    assert res["limit"] == 2
    assert res["offset"] == 1


async def test_get_results_clamps_limit_to_max(db_ctx):
    from app.routers.compliance import RESULTS_MAX_LIMIT
    db, _pid, sid = db_ctx
    await db.commit()
    res = await get_results(sid, limit=10_000, db=db)
    assert res["limit"] == RESULTS_MAX_LIMIT


async def test_get_results_filters_by_check_type(db_ctx):
    db, _pid, sid = db_ctx
    await _insert_compliance_check(db, sid, check_type="compliance")
    await _insert_compliance_check(db, sid, check_type="expert_review")
    await db.commit()

    res = await get_results(sid, check_type="compliance", db=db)
    assert res["total"] == 1
    assert res["items"][0]["check_type"] == "compliance"


async def test_readiness_overview_ignores_multiple_batches_correctly(db_ctx):
    """批次锚定验证：不同秒的两次 /check，overview 只取最新一批。"""
    db, _pid, sid = db_ctx
    await _insert_compliance_check(db, sid,
        result_obj={"hit": False, "rule_id": "CMP-01", "item": "A",
                    "severity": "high"},
        rule_id="CMP-01", created_at="2026-09-21 09:00:00")
    await _insert_compliance_check(db, sid,
        result_obj={"hit": False, "rule_id": "CMP-02", "item": "B",
                    "severity": "medium"},
        rule_id="CMP-02", created_at="2026-09-21 09:00:00")
    await _insert_compliance_check(db, sid,
        result_obj={"hit": False, "rule_id": "CMP-03", "item": "C",
                    "severity": "low"},
        rule_id="CMP-03", created_at="2026-09-21 10:00:00")
    await db.commit()

    payload = await readiness_overview(sid, db)
    ai_findings = [f for f in payload["findings"] if f.get("mode") == "ai"]
    ai_rule_ids = {f["rule_id"] for f in ai_findings}
    # 应只保留最新一批（CMP-03），不应混入 09:00 那批的 CMP-01/CMP-02
    assert "CMP-03" in ai_rule_ids
    assert "CMP-01" not in ai_rule_ids
    assert "CMP-02" not in ai_rule_ids


async def test_persist_run_never_raises_on_insert_failure(db_ctx, caplog):
    """BUG 修复：_persist_run 不再静默吞错；INSERT 失败要 rollback + 记录日志。"""
    import logging
    db, _pid, sid = db_ctx
    # 先插入一条以占位主键，后面用同 id 触发 PK 违反
    await db.execute(
        "INSERT INTO preflight_runs (id, scheme_id, rule_version, total, grade,"
        " verdict, released, blocked, counts, dimensions, findings, stats)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("dup-id", sid, RULE_VERSION, 0, "A", "", 1, 0,
         "{}", "[]", "[]", "{}"))
    await db.commit()
    # 用一个总是抛异常的 db wrapper，验证 _persist_run 内部吞掉异常并 log
    class _BoomDB:
        def __init__(self, _real):
            self._real = _real
        async def execute(self, *a, **k):
            raise RuntimeError("simulated disk full")
        async def commit(self):
            pass
        async def rollback(self):
            pass
    with caplog.at_level(logging.WARNING, logger="compliance"):
        await _persist_run(_BoomDB(db), sid,
                           {"total": 0, "grade": "D", "blocked": 1}, {})
    msgs = [r.getMessage() for r in caplog.records]
    assert any("preflight_runs 写入失败" in m for m in msgs)


# ===========================================================================
# 三、preflight_engine.py & audit_scoring.py
# ===========================================================================
def test_con_05_uses_unique_rule_id_per_duplicate_pair():
    """BUG 修复：多对重复章节各自独立的 rule_id，避免 merge_findings 塌缩。"""
    body = "基坑支护采用排桩加内支撑方案，桩径 800，桩长 20 米。" * 40
    sections = [_mk_section("第一章", body),
                _mk_section("第二章", body),
                _mk_section("第三章", body)]
    ctx = PreflightContext(scheme_id="s", scheme_name="s", scheme_type="基坑",
                           word_budget=0, sections=sections, charts=[])
    findings = run_preflight(ctx)
    con_05s = [f for f in findings if f["rule_id"].startswith("CON-05")]
    assert len(con_05s) >= 2
    rule_ids = [f["rule_id"] for f in con_05s]
    assert len(set(rule_ids)) == len(rule_ids)
    for rid in rule_ids:
        assert rid != "CON-05"
        assert rid.startswith("CON-05-")


def test_con_05_high_severity_when_near_identical():
    """近乎完全一致（>=95%）→ high 严重度。"""
    body = "基坑支护采用排桩加内支撑方案，桩径 800，桩长 20 米。" * 40
    ctx = PreflightContext(scheme_id="s", scheme_name="s", scheme_type="基坑",
                           word_budget=0, sections=[
        _mk_section("第一章", body),
        _mk_section("第二章", body),
    ], charts=[])
    findings = run_preflight(ctx)
    con_05s = [f for f in findings if f["rule_id"].startswith("CON-05")]
    assert len(con_05s) == 1
    assert con_05s[0]["severity"] == "high"


def test_score_findings_blocks_prevent_release_even_if_high_score():
    """BUG 修复：阻断项存在时不会出现 grade=C + released=True 的矛盾。"""
    findings = [
        {"rule_id": "STD-01", "dimension": "compliance", "severity": "block",
         "title": "废止标准", "detail": "引用了 GB 50134-2001（已废止）",
         "evidence": [], "section_id": "", "section_title": "",
         "suggestion": "替换为现行标准", "basis": "", "mode": "program"},
    ]
    res = score_findings(findings)
    assert res.blocked is True
    assert res.grade in ("C", "D")
    assert res.released is False


def test_score_findings_released_requires_grade_A_or_B():
    """released 的判定从 total >= 75 改为 grade ∈ {A, B}。"""
    res = score_findings([])
    assert res.total == 100.0
    assert res.grade == "A"
    assert res.released is True

    findings = [{"rule_id": "X", "dimension": "completeness",
                 "severity": "medium", "title": "", "detail": "",
                 "evidence": [], "section_id": "", "section_title": "",
                 "suggestion": "", "basis": "", "mode": "program"}]
    res = score_findings(findings)
    assert res.total >= 90.0
    assert res.grade == "A"
    assert res.released is True


def test_score_findings_unknown_dimension_falls_back_to_deliverability():
    """BUG 修复：未知 dimension 计入 deliverability 并统计 unknown_dimension_count。"""
    findings = [{"rule_id": "ZZZ-01", "dimension": "unknown_dim",
                 "severity": "low", "title": "", "detail": "", "evidence": [],
                 "section_id": "", "section_title": "", "suggestion": "",
                 "basis": "", "mode": "program"}]
    res = score_findings(findings)
    assert res.unknown_dimension_count == 1
    deliverability = [d for d in res.dimensions if d.key == "deliverability"][0]
    assert len(deliverability.findings) == 1


def test_score_findings_weight_normalization_total_bounded():
    """BUG 修复：权重和归一化后总分永远在 0-100 之间。"""
    res = score_findings([])
    assert 0.0 <= res.total <= 100.0
    assert res.total == 100.0

    many_blocks = [
        {"rule_id": f"B-{i}", "dimension": "completeness", "severity": "block",
         "title": "", "detail": "", "evidence": [], "section_id": "",
         "section_title": "", "suggestion": "", "basis": "",
         "mode": "program"}
        for i in range(10)
    ]
    res = score_findings(many_blocks)
    assert res.total >= 0.0


def test_rule_version_bumped_and_get_rule_fallback():
    """RULE_VERSION 已升至 1.4.0（CON-02/CON-03 死规则改判 + TRC-01 跨维度双扣修复）；get_rule 对带后缀 rule_id 返回 None（不 crash）。"""
    # RULE_VERSION 按注册表维护约定随规则增删改递增（此处 1.6.0 起含 DLV-13/DLV-14），
    # 故断言「不低于已知下界」而非硬钉某个具体值，避免每次新增规则都要改断言。
    assert tuple(int(x) for x in RULE_VERSION.split(".")[:2]) >= (1, 6)
    # get_rule 对派生编号仍返回 None（唯一事实源只登记基编号）；
    # 派生编号的回退解析由 preflight_engine._resolve_rule 负责，两者职责不混。
    assert get_rule("CON-05-1") is None
    assert get_rule("CON-05") is not None


def test_rule_catalog_contains_export_bridge_rules():
    """✅ G1：导出预检桥接用的四条规则已进注册表（唯一事实源）。

    导出预检的 issue 映射到 DLV-09~DLV-12，与程序化预检共用同一套
    rule_id 词表与维度权重，因此两处结论可以互相比较、不会口径漂移。
    """
    for rid in ("DLV-09", "DLV-10", "DLV-11", "DLV-12"):
        rule = get_rule(rid)
        assert rule is not None, f"{rid} 未在规则注册表中"
        assert rule.dimension == "deliverability"
        assert rule.mode == "program"


def test_rule_catalog_returns_all_non_deprecated_rules():
    rules = rule_catalog()
    assert len(rules) >= 30
    assert all("rule_id" in r for r in rules)
    assert all("dimension" in r for r in rules)


def test_run_preflight_empty_scheme_reports_deliverable_blocker():
    """零章节方案必须产生 DLV-01 阻断项，不能以空问题列表得到 A 级放行。"""
    ctx = PreflightContext(scheme_id="s", scheme_name="s", sections=[])
    findings = run_preflight(ctx)
    dlv_01 = [f for f in findings if f["rule_id"] == "DLV-01"]
    assert dlv_01
    assert dlv_01[0]["severity"] == "block"
    assert dlv_01[0]["suggestion"]


def test_run_preflight_detects_abolished_standard():
    """引用废止标准 → STD-01 block 级阻断项。"""
    sections = [_mk_section("编制依据",
                            # GB 50202-2002 在 standards_registry.ABOLISHED_STANDARDS
                            # 中确有登记（已由 GB 50202-2018 替代）；旧实现用的
                            # GB 50134-2001 并不在废止清单里，STD-01 永远不会命中。
                            "本方案编制依据 GB 50202-2002《建筑地基基础工程施工质量"
                            "验收规范》及 GB 50010-2010 相关条款。")]
    ctx = PreflightContext(scheme_id="s", scheme_name="s", scheme_type="结构",
                           word_budget=0, sections=sections, charts=[])
    findings = run_preflight(ctx)
    std_01 = [f for f in findings if f["rule_id"] == "STD-01"]
    assert std_01
    assert std_01[0]["severity"] == "block"


def test_run_preflight_detects_empty_leaf_sections():
    """空叶子章节 → DLV-01（high 严重度）。"""
    ctx = PreflightContext(scheme_id="s", scheme_name="s", word_budget=0,
                           sections=[_mk_section("第一章", "", status="empty")],
                           charts=[])
    findings = run_preflight(ctx)
    dlv_01 = [f for f in findings if f["rule_id"] == "DLV-01"]
    assert dlv_01
    assert dlv_01[0]["severity"] == "high"


def test_dlv07_detects_unclosed_tilde_fence():
    """✅ 2026-09-22：旧实现用 content.count("```")%2==1 只认 ```，漏掉 ~~~ 波浪围栏。
    现改用 find_unclosed_fences（CommonMark 口径），~~~ 未闭合也触发 DLV-07。
    """
    sections = [_mk_section("第一章", "~~~\n未完成的代码块", word_count=1000)]
    ctx = PreflightContext(scheme_id="s", scheme_name="s", word_budget=0,
                           sections=sections, charts=[])
    findings = run_preflight(ctx)
    dlv07 = [f for f in findings if f["rule_id"] == "DLV-07"]
    assert dlv07, "未闭合的 ~~~ 围栏必须触发 DLV-07（旧实现会漏检）"


def test_dlv07_no_false_positive_on_inline_backticks():
    """✅ 2026-09-22：正文内联 `code` 与字面 ``` 不应被奇偶计数误判为未闭合围栏。"""
    content = "用 `code` 内联代码；正文提到连续三个反引号 ``` 写法，均非围栏。"
    sections = [_mk_section("第一章", content, word_count=1000)]
    ctx = PreflightContext(scheme_id="s", scheme_name="s", word_budget=0,
                           sections=sections, charts=[])
    findings = run_preflight(ctx)
    assert not [f for f in findings if f["rule_id"] == "DLV-07"]


def test_dlv07_resolved_after_auto_fix():
    """✅ 2026-09-22：先 auto_fix_unclosed_fences 再预检，DLV-07 判据清零。"""
    from app.services.content_utils import auto_fix_unclosed_fences
    raw = "```mermaid\ngraph TD; A-->B"
    fixed, _ = auto_fix_unclosed_fences(raw)
    sections = [_mk_section("第一章", fixed, word_count=1000)]
    ctx = PreflightContext(scheme_id="s", scheme_name="s", word_budget=0,
                           sections=sections, charts=[])
    findings = run_preflight(ctx)
    assert not [f for f in findings if f["rule_id"] == "DLV-07"]