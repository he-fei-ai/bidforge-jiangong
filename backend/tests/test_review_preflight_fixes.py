"""审核与预检模块 2026-09-23 深度审查修复的回归锁测试。

覆盖的修复点（配套 test_review_preflight_module.py 的既有回归锁）：

- preflight_engine.py
    * run_preflight 按 rule_id 去重（CMP-09 双发导致 /preflight 与 /overview
      同一内容两条链路分数不一致的 BUG）
    * preflight_stats 叶子集合外提（行为不变，附反例回归）
- audit_rules.py
    * CON-02 / CON-03 死规则改判 ai 通道；RULE_VERSION 升至 1.3.0
- compliance.py
    * get_results 改为 SQL 级 COUNT + LIMIT/OFFSET（分页语义不变）
    * /preflight 补 G2 并发锁 + 幂等缓存 + force 参数
    * /runs 与 /report 的 stale 判定此前因 SELECT 未取 content_fingerprint
      列而恒为 False（G3 失效），现补齐并验证「正文变更 → stale=True」
    * /check 的 rule_ids 全部无效时回退默认清单（不再把空清单送 AI）
- export.py
    * _readiness_preflight_summary 的 SELECT 补 content_fingerprint 列
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest

import app.db as _appdb
from app.db import close_db, get_conn, init_db
from app.models import ComplianceCheckIn
from app.routers import compliance as _compliance_mod
from app.routers.compliance import (
    _PREFLIGHT_RECENT, get_results, list_preflight_runs,
    readiness_overview, readiness_report, run_preflight_check,
)
from app.services.audit_rules import RULE_VERSION, ai_rules, get_rule
from app.services.audit_scoring import score_findings
from app.services.preflight_engine import PreflightContext, preflight_stats, run_preflight

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def db_ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "review-preflight-fixes.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    sid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status,word_budget) VALUES(?,?,?,?,0)",
        (sid, pid, "深基坑支护专项方案", "目录已确认"))
    await db.commit()
    # 模块级幂等缓存按用例隔离（不同 tmp_path 会生成不同 sid，双保险显式清理）
    _PREFLIGHT_RECENT.pop(sid, None)
    yield db, pid, sid
    _PREFLIGHT_RECENT.pop(sid, None)
    await close_db()


async def _insert_section(db, sid, title="第一章", content="", level=1,
                          parent_id="", word_count=0, sort_order=0):
    sec_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
        " description, level, status, word_count, word_budget, content,"
        " review_status, sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sec_id, sid, "", parent_id, title, "", level,
         "generated" if content else "empty",
         word_count or len(content), 0, content, "", sort_order))
    return sec_id


def _mk_ctx(sections, charts=None, word_budget=0):
    return PreflightContext(
        scheme_id="s", scheme_name="深基坑支护专项方案", scheme_type="基坑工程",
        word_budget=word_budget, sections=sections, charts=charts or [])


def _mk_section(title, content, sid=None, parent_id=""):
    return {
        "id": sid or uuid.uuid4().hex, "title": title, "content": content,
        "word_count": len(content), "level": 1, "parent_id": parent_id,
        "status": "generated" if content else "empty",
    }


# ===========================================================================
# 一、run_preflight 按 rule_id 去重（BUG-I）
# ===========================================================================
async def test_cmp09_double_fire_deduped_to_single_finding():
    """正文为空的计算书章节会被 CMP-09 双发（「正文为空」+「无计算过程」），
    去重后只应保留一条 —— 否则独立 /preflight 双扣 40 分，与 /overview 不同分。"""
    ctx = _mk_ctx([_mk_section("计算书及相关图纸", "")])
    findings = run_preflight(ctx)
    cmp09 = [f for f in findings if f.get("rule_id") == "CMP-09"]
    assert len(cmp09) == 1, f"CMP-09 应去重为 1 条，实际 {len(cmp09)} 条"
    # 保留的是首条（「正文为空」），而不是后到的同规则发现
    assert "正文为空" in cmp09[0]["detail"]


async def test_dedup_keeps_higher_severity_finding():
    """同 rule_id 重复时保留严重度更高的一条。"""
    def _fake_completeness(ctx):
        return [
            {"rule_id": "CMP-01", "dimension": "completeness", "severity": "medium",
             "title": "轻的", "detail": "", "evidence": [], "section_id": "",
             "section_title": "", "suggestion": "", "basis": "", "mode": "program"},
            {"rule_id": "CMP-01", "dimension": "completeness", "severity": "block",
             "title": "重的", "detail": "", "evidence": [], "section_id": "",
             "section_title": "", "suggestion": "", "basis": "", "mode": "program"},
        ]
    import app.services.preflight_engine as pe
    orig = pe.check_completeness
    try:
        pe.check_completeness = _fake_completeness
        findings = run_preflight(_mk_ctx([_mk_section("任意", "内容" * 100)]))
    finally:
        pe.check_completeness = orig
    cmp01 = [f for f in findings if f.get("rule_id") == "CMP-01"]
    assert len(cmp01) == 1
    assert cmp01[0]["severity"] == "block"


async def test_empty_rule_id_findings_not_deduped():
    """异常兜底行（rule_id 为空串）不参与去重，全部保留。"""
    def _boom(ctx):
        raise RuntimeError("模拟 checker 崩溃")
    import app.services.preflight_engine as pe
    orig = pe.check_standards
    try:
        # 两个位置都换成会崩的 checker → 产出两条 rule_id="" 的兜底行
        pe.check_standards = _boom
        findings = run_preflight(_mk_ctx([_mk_section("任意", "内容" * 100)]))
    finally:
        pe.check_standards = orig
    empties = [f for f in findings if not f.get("rule_id")]
    assert len(empties) == 1  # 只替换了一个 checker，应恰好一条兜底行
    assert empties[0]["severity"] == "low"


async def test_preflight_and_overview_score_consistent_on_cmp09(db_ctx):
    """同一内容：/preflight（单链路去重后）与 /overview（merge_findings 去重）
    的 CMP-09 扣分必须一致（BUG-I 的用户可见症状是两条链路分数不同）。"""
    db, _pid, sid = db_ctx
    await _insert_section(db, sid, title="计算书及相关图纸", content="")
    await _insert_section(db, sid, title="工程概况", content="概况" * 200)
    await db.commit()

    pre = await run_preflight_check(sid, db, force=True)
    ov = await readiness_overview(sid, db, force=True)
    pre_cmp09 = [f for f in pre["findings"] if f.get("rule_id") == "CMP-09"]
    ov_cmp09 = [f for f in ov["findings"] if f.get("rule_id") == "CMP-09"]
    assert len(pre_cmp09) == 1 and len(ov_cmp09) == 1, \
        "同一 CMP-09 缺陷在两条链路的发现清单中均应只计一次"


# ===========================================================================
# 二、CON-02 / CON-03 死规则改判（BUG-G）与版本号
# ===========================================================================
async def test_con02_con03_migrated_to_ai_mode():
    assert get_rule("CON-02").mode == "ai"
    assert get_rule("CON-03").mode == "ai"
    # 改判后应出现在 AI 规则清单里（前端 /check 清单来源）
    ids = {r.rule_id for r in ai_rules()}
    assert {"CON-02", "CON-03"} <= ids
    assert tuple(int(x) for x in RULE_VERSION.split(".")[:2]) >= (1, 6)


# ===========================================================================
# 三、get_results SQL 级分页（BUG-C）
# ===========================================================================
async def test_get_results_pagination_semantics(db_ctx):
    db, _pid, sid = db_ctx
    now = datetime.now().isoformat(timespec="seconds")
    for i in range(3):
        await db.execute(
            "INSERT INTO compliance_check (id, scheme_id, check_type, rule_id,"
            " item, severity, result, suggestion, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, sid, "compliance", f"CMP-{i:02d}", f"项{i}",
             "high", json.dumps({"results": []}), "", now))
    await db.execute(
        "INSERT INTO compliance_check (id, scheme_id, check_type, result, created_at)"
        " VALUES (?,?,?,?,?)",
        (uuid.uuid4().hex, sid, "expert_review", "{}", now))
    await db.commit()

    page1 = await get_results(sid, "", 2, 0, db)
    assert page1["total"] == 4
    assert len(page1["items"]) == 2
    page2 = await get_results(sid, "", 2, 2, db)
    assert len(page2["items"]) == 2
    ids = {i["id"] for i in page1["items"]} | {i["id"] for i in page2["items"]}
    assert len(ids) == 4, "两页应无重叠地覆盖全部行"
    # offset 越界 → 空列表但 total 不变
    page3 = await get_results(sid, "", 2, 99, db)
    assert page3["items"] == [] and page3["total"] == 4
    # check_type 过滤
    only_expert = await get_results(sid, "expert_review", 10, 0, db)
    assert only_expert["total"] == 1


# ===========================================================================
# 四、/preflight 幂等缓存 + /runs /report stale 判定（BUG-E / BUG-D2）
# ===========================================================================
async def test_preflight_idempotent_cache_and_force(db_ctx):
    db, _pid, sid = db_ctx
    await _insert_section(db, sid, title="工程概况", content="概况" * 300)
    await db.commit()

    first = await run_preflight_check(sid, db)
    assert first["cached"] is False
    second = await run_preflight_check(sid, db)
    assert second["cached"] is True, "同内容指纹 TTL 内应命中幂等缓存"
    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM preflight_runs WHERE scheme_id=?", (sid,))
    assert (await cur.fetchone())["n"] == 1, "命中缓存不应重复落库污染分数趋势"
    # force=true 强制重算并落库
    forced = await run_preflight_check(sid, db, force=True)
    assert forced["cached"] is False
    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM preflight_runs WHERE scheme_id=?", (sid,))
    assert (await cur.fetchone())["n"] == 2


async def test_runs_and_report_stale_detection(db_ctx):
    """回归锁：/runs 与 /report 的 stale 此前恒为 False（SELECT 未取
    content_fingerprint 列，_run_is_stale 取不到存储指纹）。"""
    db, _pid, sid = db_ctx
    sec = await _insert_section(db, sid, title="工程概况", content="概况" * 300)
    await db.commit()
    await run_preflight_check(sid, db, force=True)

    runs = await list_preflight_runs(sid, 10, db)
    assert runs["items"] and runs["items"][0]["stale"] is False

    # 正文变更后旧结论应判过期
    await db.execute("UPDATE sections SET content=?, word_count=? WHERE id=?",
                     ("彻底改过的正文" * 300, len("彻底改过的正文" * 300), sec))
    await db.commit()
    runs = await list_preflight_runs(sid, 10, db)
    assert runs["items"][0]["stale"] is True, "正文变更后历史结论必须标记为已过期"

    report = await readiness_report(sid, "json", db)
    assert report["stale"] is True, "/report 同样要能判出过期"


async def test_report_tolerates_null_total_and_malformed_json(db_ctx):
    """历史脏数据不能让整改报告 500；数值与集合字段均须安全降级。"""
    db, _pid, sid = db_ctx
    await db.execute(
        "INSERT INTO preflight_runs (id, scheme_id, total, grade, verdict, released, blocked,"
        " counts, dimensions, findings, stats, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, None, "", "历史记录", 0, 0,
         "not-json", "not-json", "not-json", "{}", "2026-01-01T00:00:00"))
    await db.commit()
    report = await readiness_report(sid, "json", db)
    assert report["total"] == 0
    assert report["findings"] == []
    assert report["dimensions"] == []
    assert report["stats"] == {}
    markdown = await readiness_report(sid, "markdown", db)
    assert "0.0 / 100" in markdown["content"]


async def test_export_summary_reads_fingerprint_column(db_ctx):
    """导出页摘要（G1 反向打通）也要能判出 stale —— 回归其 SELECT 缺列问题。"""
    from app.routers.export import _readiness_preflight_summary
    db, _pid, sid = db_ctx
    sec = await _insert_section(db, sid, title="工程概况", content="概况" * 300)
    await db.commit()
    await run_preflight_check(sid, db, force=True)
    summary = await _readiness_preflight_summary(sid, db)
    assert summary["has_run"] is True
    assert summary["stale"] is False
    await db.execute("UPDATE sections SET content=? WHERE id=?", ("x" * 5000, sec))
    await db.commit()
    summary = await _readiness_preflight_summary(sid, db)
    assert summary["stale"] is True


# ===========================================================================
# 五、/check rule_ids 全部无效时的回退（BUG-H）
# ===========================================================================
async def test_check_all_invalid_rule_ids_falls_back(db_ctx, monkeypatch):
    db, _pid, sid = db_ctx
    await _insert_section(db, sid, title="工程概况", content="概况" * 200)
    await db.commit()
    captured: dict = {}

    async def _fake_collect(messages, *_a, **_k):
        captured["prompt"] = messages[0]["content"]
        return {"results": []}, None

    monkeypatch.setattr(_compliance_mod, "collect_json_response", _fake_collect)
    # 全部无效的 rule_ids + 空 checklist → 必须回退为规则库 AI 全集，而非空清单
    await _compliance_mod.compliance_check(
        ComplianceCheckIn(scheme_id=sid, rule_ids=["NOPE-99", ""], checklist=[]),
        db)
    prompt = captured.get("prompt", "")
    assert prompt, "AI 提示词应被构造"
    assert "术语与工程名称前后一致" in prompt, "回退后应携带规则库 AI 清单（含 CON-02）"


# ===========================================================================
# 六、preflight_stats 叶子集合外提（行为回归，BUG-J）
# ===========================================================================
async def test_preflight_stats_leaf_count_unchanged():
    parent = uuid.uuid4().hex
    child = uuid.uuid4().hex
    ctx = _mk_ctx([
        _mk_section("父章", "", sid=parent),
        _mk_section("子章", "正文" * 100, sid=child, parent_id=parent),
    ])
    stats = preflight_stats(ctx)
    assert stats["section_count"] == 2
    assert stats["leaf_count"] == 1, "有子节点的章节不算叶子"
